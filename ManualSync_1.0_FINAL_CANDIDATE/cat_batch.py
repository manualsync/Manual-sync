from __future__ import annotations

import json
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import pandas as pd

from cat_tm import (
    CAT_BUILD,
    TranslationMemory,
    apply_terminology_qa,
    build_tm_from_documents,
    cat_normalize,
    inline_tokens,
    load_terminology,
    match_segments,
    segments_from_layout,
    source_target_safe,
)

BATCH_BUILD = "0.15.0-cat-production-batch-20260810"

WORD_RE = re.compile(r"\b[\wÀ-ÖØ-öø-ÿ]+(?:[-_/][\wÀ-ÖØ-öø-ÿ]+)*\b", re.UNICODE)

DEFAULT_QA_PROFILE = {
    "name": "ManualSync Default",
    "terminology": True,
    "double_spaces": True,
    "brackets": True,
    "terminal_punctuation": False,
    "capitalization": False,
    "block_on_warnings": False,
    "technical_integrity": True,
}


def word_count(text: str) -> int:
    """Conservative source-word count for CAT analysis.

    Technical model codes such as ACS880-34 or R5_v2 count as one token.
    Pure punctuation does not count.
    """
    return len(WORD_RE.findall(str(text or "")))


def match_band(row: pd.Series | dict) -> str:
    category = str(row.get("match_category", ""))
    decision = str(row.get("decision", ""))
    score = float(row.get("linguistic_score", 0) or 0)
    if "TECHNICAL SAFE BLOCK" in category or decision == "BLOCCATO — REVISIONE TECNICA":
        return "TECHNICAL BLOCK"
    if category.startswith("CONTEXT MATCH"):
        return "CONTEXT"
    if category.startswith("100% MATCH"):
        return "100%"
    if category == "FUZZY MATCH":
        if score >= 95:
            return "95-99%"
        if score >= 85:
            return "85-94%"
        if score >= 75:
            return "75-84%"
        return "<75%"
    return "NEW"


def add_batch_metadata(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().fillna("")
    if out.empty:
        for c in ["word_count", "match_band", "source_norm", "repetition_group", "repetition_count", "is_repetition", "locked"]:
            out[c] = []
        return out

    out["word_count"] = out["source"].astype(str).map(word_count)
    out["match_band"] = out.apply(match_band, axis=1)
    out["source_norm"] = out["source"].astype(str).map(cat_normalize)

    group_ids: dict[str, str] = {}
    group_counts = out["source_norm"].value_counts().to_dict()
    seen = 0
    for norm, count in sorted(group_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if norm and count > 1:
            seen += 1
            group_ids[norm] = f"REP-{seen:05d}"
    out["repetition_group"] = out["source_norm"].map(group_ids).fillna("")
    out["repetition_count"] = out["source_norm"].map(group_counts).fillna(1).astype(int)
    out["is_repetition"] = out["repetition_count"].gt(1)
    out["repetition_master"] = False
    for _, indices in out[out["is_repetition"]].groupby("source_norm").groups.items():
        first_idx = list(indices)[0]
        out.loc[first_idx, "repetition_master"] = True

    # Automatic Technical-Safe reuse is locked. A reviewer can unlock it in the
    # UI, but propagation functions will never overwrite locked rows silently.
    if "decision" in out.columns:
        out["locked"] = out["decision"].astype(str).eq("RIUSO AUTOMATICO CONSENTITO")
    elif "status" in out.columns:
        out["locked"] = out["status"].astype(str).eq("Confirmed")
    else:
        out["locked"] = False
    return out


def word_count_by_band(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["match_band", "segments", "words", "percent_words"])
    work = df.copy()
    if "word_count" not in work:
        work["word_count"] = work["source"].astype(str).map(word_count)
    if "match_band" not in work:
        work["match_band"] = work.apply(match_band, axis=1)
    grouped = work.groupby("match_band", dropna=False).agg(
        segments=("segment_id", "count"),
        words=("word_count", "sum"),
    ).reset_index()
    total = max(1, int(grouped["words"].sum()))
    grouped["percent_words"] = (grouped["words"] / total * 100).round(1)
    order = ["CONTEXT", "100%", "95-99%", "85-94%", "75-84%", "<75%", "TECHNICAL BLOCK", "NEW"]
    grouped["_order"] = grouped["match_band"].map({v: i for i, v in enumerate(order)}).fillna(999)
    return grouped.sort_values(["_order", "match_band"]).drop(columns="_order").reset_index(drop=True)


def repetition_report(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "source_norm" not in df:
        return pd.DataFrame(columns=["repetition_group", "occurrences", "words_each", "total_words", "documents", "source"])
    reps = df[df["repetition_count"].astype(int).gt(1)].copy()
    rows = []
    for norm, group in reps.groupby("source_norm"):
        first = group.iloc[0]
        docs = sorted(set(group.get("document", pd.Series(dtype=str)).astype(str))) if "document" in group else []
        rows.append({
            "repetition_group": first.get("repetition_group", ""),
            "occurrences": int(len(group)),
            "words_each": int(first.get("word_count", word_count(first.get("source", "")))),
            "total_words": int(group.get("word_count", pd.Series([0]*len(group))).astype(int).sum()),
            "documents": " | ".join(d for d in docs if d),
            "source": first.get("source", ""),
        })
    return pd.DataFrame(rows).sort_values(["total_words", "occurrences"], ascending=False).reset_index(drop=True) if rows else pd.DataFrame(columns=["repetition_group", "occurrences", "words_each", "total_words", "documents", "source"])


def propagate_repetition(editor_df: pd.DataFrame, segment_id: str, target: str, status: str = "Reviewed") -> tuple[pd.DataFrame, dict]:
    """Propagate one approved translation to identical source repetitions.

    Locked segments are never overwritten. The source must be exactly equal after
    CAT normalization, which also guarantees the same Technical Safe signature for
    literal repetitions.
    """
    out = editor_df.copy().fillna("")
    hit = out[out["segment_id"].astype(str).eq(str(segment_id))]
    if hit.empty:
        return out, {"updated": 0, "skipped_locked": 0, "reason": "segmento non trovato"}
    source = str(hit.iloc[0].get("source", ""))
    norm = cat_normalize(source)
    safe, reason = source_target_safe(source, str(target or ""))
    if not safe:
        return out, {"updated": 0, "skipped_locked": 0, "reason": reason}
    updated = 0
    skipped_locked = 0
    for idx, row in out.iterrows():
        if cat_normalize(str(row.get("source", ""))) != norm:
            continue
        if bool(row.get("locked", False)) and str(row.get("segment_id")) != str(segment_id):
            skipped_locked += 1
            continue
        out.at[idx, "target"] = str(target or "")
        if "status" in out.columns:
            out.at[idx, "status"] = status
        updated += 1
    return out, {"updated": updated, "skipped_locked": skipped_locked, "reason": ""}


def filter_segments(df: pd.DataFrame, *, status: str = "", band: str = "", locked: str = "", query: str = "") -> pd.DataFrame:
    out = df.copy()
    if status and "status" in out:
        out = out[out["status"].astype(str).eq(status)]
    if band and "match_band" in out:
        out = out[out["match_band"].astype(str).eq(band)]
    if locked in {"locked", "unlocked"} and "locked" in out:
        wanted = locked == "locked"
        out = out[out["locked"].astype(bool).eq(wanted)]
    q = str(query or "").strip().casefold()
    if q:
        mask = out.get("source", pd.Series("", index=out.index)).astype(str).str.casefold().str.contains(re.escape(q), regex=True)
        if "target" in out:
            mask = mask | out["target"].astype(str).str.casefold().str.contains(re.escape(q), regex=True)
        out = out[mask]
    return out.reset_index(drop=True)


def save_qa_profile(path: str | Path, config: dict, name: str = "ManualSync QA") -> Path:
    path = Path(path)
    payload = dict(DEFAULT_QA_PROFILE)
    payload.update({k: v for k, v in (config or {}).items() if k in payload})
    payload["name"] = str(name or payload.get("name") or "ManualSync QA")
    payload["technical_integrity"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_qa_profile(path: str | Path | None) -> dict:
    if not path:
        return dict(DEFAULT_QA_PROFILE)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    out = dict(DEFAULT_QA_PROFILE)
    for k in out:
        if k in payload:
            out[k] = payload[k]
    out["technical_integrity"] = True
    return out


def _append_text_with_inline(parent: ET.Element, text: str) -> None:
    """Serialize protected inline tokens as XLIFF 2.0 <ph> codes.

    Paired HTML/XML tags are kept as ordered protected codes. This intentionally
    favours lossless round-trip safety over attempting semantic HTML nesting.
    """
    text = str(text or "")
    tokens = list(re.finditer(r"(?:</?[A-Za-z][^<>]{0,120}>|\{\{[^{}]{1,120}\}\}|\$\{[^{}]{1,120}\}|\{[A-Za-z_][A-Za-z0-9_.:-]*\}|%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[a-zA-Z]|%\d+|&[A-Za-z][A-Za-z0-9]+;|\[\[[A-Za-z_][A-Za-z0-9_.:-]*\]\])", text))
    if not tokens:
        parent.text = text
        return
    cursor = 0
    last_child = None
    for i, m in enumerate(tokens, start=1):
        chunk = text[cursor:m.start()]
        if last_child is None:
            parent.text = (parent.text or "") + chunk
        else:
            last_child.tail = (last_child.tail or "") + chunk
        ph = ET.SubElement(parent, "{urn:oasis:names:tc:xliff:document:2.0}ph", {
            "id": str(i),
            "equiv": m.group(0),
            "disp": m.group(0),
        })
        last_child = ph
        cursor = m.end()
    tail = text[cursor:]
    if last_child is None:
        parent.text = (parent.text or "") + tail
    else:
        last_child.tail = (last_child.tail or "") + tail


def _read_text_with_inline(node: ET.Element) -> str:
    parts = [node.text or ""]
    for child in list(node):
        if child.tag.endswith("ph"):
            parts.append(child.attrib.get("equiv") or child.attrib.get("disp") or "")
        else:
            parts.append("".join(child.itertext()))
        parts.append(child.tail or "")
    return "".join(parts)


def export_xliff_inline(df: pd.DataFrame, out_path: str | Path, source_lang: str, target_lang: str) -> Path:
    out_path = Path(out_path)
    ns = "urn:oasis:names:tc:xliff:document:2.0"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}xliff", {"version": "2.0", "srcLang": source_lang, "trgLang": target_lang})
    file_el = ET.SubElement(root, f"{{{ns}}}file", {"id": "manualsync-batch"})
    for _, row in df.iterrows():
        unit_id = str(row.get("batch_segment_id") or row.get("segment_id"))
        unit = ET.SubElement(file_el, f"{{{ns}}}unit", {"id": unit_id})
        notes = ET.SubElement(unit, f"{{{ns}}}notes")
        note = ET.SubElement(notes, f"{{{ns}}}note", {"category": "manualsync-band"})
        note.text = str(row.get("match_band", ""))
        segment = ET.SubElement(unit, f"{{{ns}}}segment", {"state": "translated" if row.get("pretranslation") else "initial"})
        source = ET.SubElement(segment, f"{{{ns}}}source")
        _append_text_with_inline(source, str(row.get("source", "")))
        target = ET.SubElement(segment, f"{{{ns}}}target")
        _append_text_with_inline(target, str(row.get("pretranslation") or row.get("target") or ""))
    # Do not pretty-indent mixed-content XLIFF: indentation becomes literal text
    # around inline codes and would corrupt the segment on round-trip.
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    return out_path


def import_xliff_inline(path: str | Path) -> pd.DataFrame:
    root = ET.parse(path).getroot()
    ns = {"x": "urn:oasis:names:tc:xliff:document:2.0"}
    rows = []
    for unit in root.findall(".//x:unit", ns):
        segment = unit.find("./x:segment", ns)
        if segment is None:
            continue
        src = segment.find("./x:source", ns)
        tgt = segment.find("./x:target", ns)
        rows.append({
            "segment_id": unit.attrib.get("id", ""),
            "source": _read_text_with_inline(src) if src is not None else "",
            "target": _read_text_with_inline(tgt) if tgt is not None else "",
            "state": segment.attrib.get("state", ""),
        })
    return pd.DataFrame(rows)


def run_batch_pipeline(
    old_source_pdf: str | Path,
    old_translation_pdf: str | Path,
    new_source_pdfs: Iterable[str | Path],
    out_dir: str | Path,
    source_lang: str = "en-US",
    target_lang: str = "it-IT",
    old_pages: str = "",
    translation_pages: str = "",
    new_pages: str = "",
    terminology_path: str | Path | None = None,
    fuzzy_threshold: float = 75.0,
    external_tms: list[dict] | None = None,
) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tm_path = out_dir / "ManualSync_BATCH_TM.sqlite"
    tm_path.unlink(missing_ok=True)
    tm = TranslationMemory(tm_path)
    all_frames = []
    legacy_df = pd.DataFrame()
    import_results = []
    try:
        legacy_df = build_tm_from_documents(
            tm, old_source_pdf, old_translation_pdf, source_lang, target_lang,
            old_pages=old_pages, translation_pages=translation_pages,
            origin="batch legacy bilingual documents", tm_name="Project Legacy", priority=100, penalty=0,
        )
        for i, profile in enumerate(external_tms or []):
            path = profile.get("path")
            if path:
                import_results.append(tm.import_tmx(
                    path, source_lang, target_lang,
                    origin=str(profile.get("origin") or "External TMX"),
                    tm_name=str(profile.get("name") or Path(path).stem),
                    priority=int(profile.get("priority", max(10, 80-i*10))),
                    penalty=float(profile.get("penalty", i*5.0)),
                ))
        terminology = load_terminology(terminology_path, source_lang, target_lang)
        for doc_index, pdf in enumerate(list(new_source_pdfs), start=1):
            pdf = Path(pdf)
            segments = segments_from_layout(pdf, new_pages)
            df = match_segments(tm, segments, source_lang, target_lang, fuzzy_threshold=fuzzy_threshold)
            df = apply_terminology_qa(df, terminology)
            df.insert(0, "document", pdf.name)
            df.insert(1, "document_index", doc_index)
            df["batch_segment_id"] = [f"DOC{doc_index:03d}-{sid}" for sid in df["segment_id"].astype(str)]
            all_frames.append(df)
        combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
        combined = add_batch_metadata(combined)
        bands = word_count_by_band(combined)
        repetitions = repetition_report(combined)

        combined_path = out_dir / "BATCH_MATCHES.csv"
        bands_path = out_dir / "WORD_COUNT_BY_BAND.csv"
        repetitions_path = out_dir / "REPETITIONS.csv"
        legacy_path = out_dir / "TM_LEGACY_IMPORT_AUDIT.csv"
        combined.to_csv(combined_path, index=False, encoding="utf-8-sig")
        bands.to_csv(bands_path, index=False, encoding="utf-8-sig")
        repetitions.to_csv(repetitions_path, index=False, encoding="utf-8-sig")
        legacy_df.to_csv(legacy_path, index=False, encoding="utf-8-sig")

        inline_xlf = export_xliff_inline(combined, out_dir / "ManualSync_BATCH_INLINE.xlf", source_lang, target_lang)
        tmx = tm.export_tmx(out_dir / "ManualSync_BATCH_TM.tmx", source_lang, target_lang)
        qa_profile = save_qa_profile(out_dir / "ManualSync_QA_PROFILE.json", DEFAULT_QA_PROFILE, "ManualSync Batch Default")

        summary = {
            "batch_build": BATCH_BUILD,
            "cat_build": CAT_BUILD,
            "documents": len(list(new_source_pdfs)),
            "segments": int(len(combined)),
            "source_words": int(combined["word_count"].sum()) if not combined.empty else 0,
            "repetition_segments": int(combined["is_repetition"].sum()) if not combined.empty else 0,
            "repetition_groups": int(repetitions.shape[0]),
            "locked_segments": int(combined["locked"].sum()) if not combined.empty else 0,
            "tm_entries": tm.count(),
            "tm_profiles": tm.profiles(),
            "tm_imports": import_results,
            "word_bands": bands.to_dict(orient="records"),
        }
        (out_dir / "BATCH_SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        package = out_dir / "ManualSync_BATCH_PROJECT.zip"
        package.unlink(missing_ok=True)
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in out_dir.iterdir():
                if p.is_file() and p != package:
                    zf.write(p, arcname=p.name)

        return {
            "matches": combined,
            "bands": bands,
            "repetitions": repetitions,
            "summary": summary,
            "tm_sqlite": tm_path,
            "tmx": tmx,
            "xliff_inline": inline_xlf,
            "qa_profile": qa_profile,
            "matches_csv": combined_path,
            "bands_csv": bands_path,
            "repetitions_csv": repetitions_path,
            "package": package,
        }
    finally:
        tm.close()
