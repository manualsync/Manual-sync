from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from cat_batch import _append_text_with_inline, _read_text_with_inline
from ccms_industrial import IndustrialRepository
from translation_impact import analyze_translation_impact

WORK_PACKAGE_BUILD = "0.23.1-deduplicated-nontranslatable-work-package-20260820"
XLIFF_NS = "urn:oasis:names:tc:xliff:document:2.0"
ET.register_namespace("", XLIFF_NS)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or "unknown"


def _nonempty(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _add_note(notes: ET.Element, category: str, value: Any) -> None:
    text = _nonempty(value)
    if not text:
        return
    note = ET.SubElement(notes, f"{{{XLIFF_NS}}}note", {"category": category})
    note.text = text


def export_work_xliff(
    work_df: pd.DataFrame,
    out_path: str | Path,
    *,
    source_lang: str,
    target_lang: str,
    source_version: int,
    changed_kind: str,
    changed_id: str,
) -> Path:
    """Export only rows requiring human work.

    AUTO_REUSE rows are deliberately forbidden here. Technical-review rows always
    have an empty target; legacy TM output is kept only as an audit note.
    Linguistic-review rows may carry a safe CAT suggestion as target. NEW rows are empty.
    """
    out_path = Path(out_path)
    locked_buckets = {"AUTO_REUSE", "NOT_TRANSLATABLE", "INTRA_DOCUMENT_REUSE"}
    forbidden = work_df[work_df.get("work_bucket", pd.Series(dtype=str)).astype(str).isin(locked_buckets)]
    if not forbidden.empty:
        raise ValueError("Bucket locked non può entrare nel work XLIFF: AUTO_REUSE/NOT_TRANSLATABLE/INTRA_DOCUMENT_REUSE restano fuori dal pacchetto del traduttore.")

    root = ET.Element(f"{{{XLIFF_NS}}}xliff", {
        "version": "2.0", "srcLang": source_lang, "trgLang": target_lang,
    })
    file_el = ET.SubElement(root, f"{{{XLIFF_NS}}}file", {
        "id": "manualsync-work-package",
        "original": f"{changed_kind}:{changed_id}@v{source_version}",
    })

    for _, row in work_df.iterrows():
        bucket = _nonempty(row.get("work_bucket")) or "NEW_TRANSLATION"
        unit = ET.SubElement(file_el, f"{{{XLIFF_NS}}}unit", {"id": _nonempty(row.get("segment_id"))})
        notes = ET.SubElement(unit, f"{{{XLIFF_NS}}}notes")
        _add_note(notes, "manualsync-work-bucket", bucket)
        _add_note(notes, "manualsync-decision", row.get("decision"))
        _add_note(notes, "manualsync-match-category", row.get("match_category"))
        _add_note(notes, "manualsync-linguistic-score", row.get("linguistic_score"))
        _add_note(notes, "manualsync-source-ref", row.get("source_ref"))
        _add_note(notes, "manualsync-object", f"{row.get('object_kind','')}:{row.get('object_id','')}")
        _add_note(notes, "manualsync-technical-reason", row.get("technical_reason"))
        _add_note(notes, "manualsync-numbers", row.get("numbers"))
        _add_note(notes, "manualsync-codes", row.get("codes"))
        _add_note(notes, "manualsync-units", row.get("units"))
        _add_note(notes, "manualsync-tm", row.get("tm_name"))

        suggestion = _nonempty(row.get("suggested_translation"))
        if bucket == "TECHNICAL_REVIEW" and suggestion:
            # The old translation can help diagnosis but must never pre-populate target.
            _add_note(notes, "manualsync-legacy-translation-reference-do-not-reuse", suggestion)

        state = "initial"
        target_text = ""
        if bucket == "LINGUISTIC_REVIEW":
            target_text = suggestion
            state = "translated" if target_text else "initial"
        elif bucket in {"TECHNICAL_REVIEW", "NEW_TRANSLATION"}:
            target_text = ""
        else:
            raise ValueError(f"Bucket non esportabile nel work XLIFF: {bucket}")

        seg = ET.SubElement(unit, f"{{{XLIFF_NS}}}segment", {"state": state})
        src = ET.SubElement(seg, f"{{{XLIFF_NS}}}source")
        _append_text_with_inline(src, _nonempty(row.get("source")))
        tgt = ET.SubElement(seg, f"{{{XLIFF_NS}}}target")
        _append_text_with_inline(tgt, target_text)

    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    return out_path


def inspect_work_xliff(path: str | Path) -> pd.DataFrame:
    root = ET.parse(path).getroot()
    ns = {"x": XLIFF_NS}
    rows: list[dict[str, Any]] = []
    for unit in root.findall(".//x:unit", ns):
        notes = {
            str(n.attrib.get("category", "")): (n.text or "")
            for n in unit.findall("./x:notes/x:note", ns)
        }
        seg = unit.find("./x:segment", ns)
        if seg is None:
            continue
        src = seg.find("./x:source", ns)
        tgt = seg.find("./x:target", ns)
        rows.append({
            "segment_id": unit.attrib.get("id", ""),
            "source": _read_text_with_inline(src) if src is not None else "",
            "target": _read_text_with_inline(tgt) if tgt is not None else "",
            "state": seg.attrib.get("state", ""),
            "work_bucket": notes.get("manualsync-work-bucket", ""),
            "technical_reason": notes.get("manualsync-technical-reason", ""),
            "legacy_reference": notes.get("manualsync-legacy-translation-reference-do-not-reuse", ""),
        })
    return pd.DataFrame(rows)


def generate_translation_work_packages(
    repo: IndustrialRepository,
    changed_id: str,
    *,
    kind: str,
    tm_paths: Iterable[str | Path] = (),
    fuzzy_threshold: float = 75.0,
    target_langs: Iterable[str] = (),
    require_approved: bool = True,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="manualsync_v23_workpkg_"))
    if root.exists():
        root.mkdir(parents=True, exist_ok=True)
    else:
        root.mkdir(parents=True)

    impact_dir = root / "impact"
    impact = analyze_translation_impact(
        repo,
        changed_id,
        kind=kind,
        tm_paths=tm_paths,
        fuzzy_threshold=fuzzy_threshold,
        rate_card=None,
        target_langs=target_langs,
        require_approved=require_approved,
        out_dir=impact_dir,
    )
    overall = impact["overall"]
    detail = impact["detail"].copy()

    package_rows: list[dict[str, Any]] = []
    language_zip_paths: list[Path] = []

    for lang, lang_df in detail.groupby("target_lang", sort=True):
        lang = str(lang)
        safe_lang = _safe_name(lang)
        lang_dir = root / f"LANG_{safe_lang}"
        lang_dir.mkdir(parents=True, exist_ok=True)

        locked_buckets = {"AUTO_REUSE", "NOT_TRANSLATABLE", "INTRA_DOCUMENT_REUSE"}
        auto_df = lang_df[lang_df["work_bucket"].astype(str).isin(locked_buckets)].copy()
        work_df = lang_df[~lang_df["work_bucket"].astype(str).isin(locked_buckets)].copy()

        # Safety invariant: technical rows cannot carry a target into translator work.
        technical = work_df[work_df["work_bucket"].astype(str) == "TECHNICAL_REVIEW"]
        if technical["pretranslation"].fillna("").astype(str).str.strip().ne("").any():
            raise RuntimeError(f"Fail-closed: TECHNICAL_REVIEW contiene pretranslation per {lang}")

        xlf = export_work_xliff(
            work_df,
            lang_dir / f"ManualSync_{safe_lang}_WORK_ONLY.xlf",
            source_lang=str(overall["source_lang"]),
            target_lang=lang,
            source_version=int(overall["source_version"]),
            changed_kind=str(overall["changed_kind"]),
            changed_id=str(overall["changed_id"]),
        )
        queue_csv = lang_dir / "REVIEW_QUEUE.csv"
        locked_csv = lang_dir / "AUTO_REUSE_LOCKED.csv"
        work_df.to_csv(queue_csv, index=False, encoding="utf-8-sig")
        auto_df.to_csv(locked_csv, index=False, encoding="utf-8-sig")

        bucket_counts = work_df["work_bucket"].value_counts().to_dict()
        bucket_words = work_df.groupby("work_bucket")["word_count"].sum().astype(int).to_dict() if not work_df.empty else {}
        manifest = {
            "build": WORK_PACKAGE_BUILD,
            "source": {
                "kind": overall["changed_kind"],
                "id": overall["changed_id"],
                "version": overall["source_version"],
                "status": overall["source_status"],
                "source_lang": overall["source_lang"],
            },
            "target_lang": lang,
            "policy": {
                "auto_reuse_in_work_xliff": False,
                "nontranslatable_in_work_xliff": False,
                "intra_document_reuse_in_work_xliff": False,
                "technical_review_target_must_be_empty": True,
                "linguistic_review_safe_suggestion_allowed": True,
                "new_translation_target_empty": True,
                "human_approval_required": True,
            },
            "counts": {
                "source_segments": int(len(lang_df)),
                "work_segments": int(len(work_df)),
                "auto_reuse_locked_segments": int((auto_df["work_bucket"].astype(str) == "AUTO_REUSE").sum()) if not auto_df.empty else 0,
                "nontranslatable_locked_segments": int((auto_df["work_bucket"].astype(str) == "NOT_TRANSLATABLE").sum()) if not auto_df.empty else 0,
                "intra_document_reuse_locked_segments": int((auto_df["work_bucket"].astype(str) == "INTRA_DOCUMENT_REUSE").sum()) if not auto_df.empty else 0,
                "linguistic_review_segments": int(bucket_counts.get("LINGUISTIC_REVIEW", 0)),
                "technical_review_segments": int(bucket_counts.get("TECHNICAL_REVIEW", 0)),
                "new_translation_segments": int(bucket_counts.get("NEW_TRANSLATION", 0)),
                "work_words": int(work_df["word_count"].sum()) if not work_df.empty else 0,
                "auto_reuse_locked_words": int(auto_df.loc[auto_df["work_bucket"].astype(str) == "AUTO_REUSE", "word_count"].sum()) if not auto_df.empty else 0,
                "nontranslatable_locked_words": int(auto_df.loc[auto_df["work_bucket"].astype(str) == "NOT_TRANSLATABLE", "word_count"].sum()) if not auto_df.empty else 0,
                "intra_document_reuse_locked_words": int(auto_df.loc[auto_df["work_bucket"].astype(str) == "INTRA_DOCUMENT_REUSE", "word_count"].sum()) if not auto_df.empty else 0,
                "linguistic_review_words": int(bucket_words.get("LINGUISTIC_REVIEW", 0)),
                "technical_review_words": int(bucket_words.get("TECHNICAL_REVIEW", 0)),
                "new_translation_words": int(bucket_words.get("NEW_TRANSLATION", 0)),
            },
            "files": {},
        }
        manifest_path = lang_dir / "MANIFEST.json"
        # Hash the operational files; manifest itself is excluded from its own checksum.
        for p in [xlf, queue_csv, locked_csv]:
            manifest["files"][p.name] = {"sha256": _sha256(p), "bytes": p.stat().st_size}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        zip_path = root / f"ManualSync_{safe_lang}_TRANSLATION_WORK_PACKAGE.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in [xlf, queue_csv, locked_csv, manifest_path]:
                zf.write(p, arcname=p.name)
        language_zip_paths.append(zip_path)

        package_rows.append({
            "target_lang": lang,
            "work_segments": int(len(work_df)),
            "work_words": int(work_df["word_count"].sum()) if not work_df.empty else 0,
            "auto_reuse_locked_segments": int((auto_df["work_bucket"].astype(str) == "AUTO_REUSE").sum()) if not auto_df.empty else 0,
            "auto_reuse_locked_words": int(auto_df.loc[auto_df["work_bucket"].astype(str) == "AUTO_REUSE", "word_count"].sum()) if not auto_df.empty else 0,
            "nontranslatable_locked_segments": int((auto_df["work_bucket"].astype(str) == "NOT_TRANSLATABLE").sum()) if not auto_df.empty else 0,
            "nontranslatable_locked_words": int(auto_df.loc[auto_df["work_bucket"].astype(str) == "NOT_TRANSLATABLE", "word_count"].sum()) if not auto_df.empty else 0,
            "intra_document_reuse_locked_segments": int((auto_df["work_bucket"].astype(str) == "INTRA_DOCUMENT_REUSE").sum()) if not auto_df.empty else 0,
            "intra_document_reuse_locked_words": int(auto_df.loc[auto_df["work_bucket"].astype(str) == "INTRA_DOCUMENT_REUSE", "word_count"].sum()) if not auto_df.empty else 0,
            "linguistic_review": int(bucket_counts.get("LINGUISTIC_REVIEW", 0)),
            "technical_review": int(bucket_counts.get("TECHNICAL_REVIEW", 0)),
            "new_translation": int(bucket_counts.get("NEW_TRANSLATION", 0)),
            "package_zip": zip_path.name,
            "package_sha256": _sha256(zip_path),
        })

    packages_df = pd.DataFrame(package_rows)
    packages_csv = root / "TRANSLATION_WORK_PACKAGES.csv"
    packages_df.to_csv(packages_csv, index=False, encoding="utf-8-sig")

    master = {
        "build": WORK_PACKAGE_BUILD,
        "impact_build": overall.get("build"),
        "source": {
            "kind": overall["changed_kind"], "id": overall["changed_id"],
            "version": overall["source_version"], "status": overall["source_status"],
            "source_lang": overall["source_lang"],
        },
        "target_languages": int(len(packages_df)),
        "human_attention_words_across_languages": int(overall["human_attention_words_across_languages"]),
        "auto_reuse_words_across_languages": int(overall["auto_reuse_words_across_languages"]),
        "packages": package_rows,
    }
    master_json = root / "MASTER_MANIFEST.json"
    master_json.write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8")

    master_zip = root / "ManualSync_TRANSLATION_WORK_PACKAGES_MASTER.zip"
    with zipfile.ZipFile(master_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(master_json, arcname=master_json.name)
        zf.write(packages_csv, arcname=packages_csv.name)
        zf.write(impact["language_csv"], arcname="IMPACT/TRANSLATION_IMPACT_LANGUAGES.csv")
        zf.write(impact["detail_csv"], arcname="IMPACT/TRANSLATION_IMPACT_SEGMENTS.csv")
        zf.write(impact["summary_json"], arcname="IMPACT/TRANSLATION_IMPACT_SUMMARY.json")
        for p in language_zip_paths:
            zf.write(p, arcname=f"LANGUAGE_PACKAGES/{p.name}")

    return {
        "build": WORK_PACKAGE_BUILD,
        "overall": overall,
        "packages": packages_df,
        "packages_csv": packages_csv,
        "master_manifest": master_json,
        "master_zip": master_zip,
        "language_zips": language_zip_paths,
        "detail": detail,
    }
