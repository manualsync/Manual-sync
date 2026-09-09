from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from cat_tm import CATSegment, TranslationMemory, cat_normalize, match_segments, split_sentences, technical_signature
from ccms_industrial import IndustrialRepository
from impact_economics import word_count
from language_codes import canonical_lang, canonical_langs
from linguistic_safety import is_nontranslatable_text
import rich_text

TRANSLATION_IMPACT_BUILD = "0.22.1-repeat-nontranslatable-intelligence-20260820"

# 4E.3 final translation blocker: a stable, code-matchable marker
# (never shown verbatim to a human) appended to a SourceUnit.source_ref
# whenever that unit's text is a rich block's VISIBLE-TEXT projection
# rather than its literal stored content -- i.e. whenever formatting was
# stripped rather than preserved. translation_work_package.py's
# fail-closed pre-flight check greps for this exact marker to decide
# whether to REFUSE creating a translation work package at all; kept
# separate from the human-readable phrase appended alongside it in
# _block_text_source_unit() so that wording can be edited freely without
# ever touching the detection logic.
RICH_FORMAT_UNSAFE_MARKER = "#rich-format-unsafe"


@dataclass(frozen=True)
class SourceUnit:
    unit_id: str
    object_kind: str
    object_id: str
    source_text: str
    source_ref: str


def _json_load(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return default


def _lang_base(lang: str) -> str:
    return str(lang or "").strip().replace("_", "-").casefold().split("-")[0]


def publication_revision_token(repo: IndustrialRepository, publication_id: str) -> int:
    """Stable integer token for the exact set of source object versions in a publication."""
    pub = repo.conn.execute("SELECT publication_id,source_lang FROM publications WHERE publication_id=?", (publication_id,)).fetchone()
    if not pub:
        raise KeyError(publication_id)
    rows = []
    for link in repo.conn.execute("SELECT topic_id,position FROM publication_topics WHERE publication_id=? ORDER BY position,topic_id", (publication_id,)):
        tid = str(link["topic_id"])
        t = repo.conn.execute("SELECT version,status FROM topics WHERE topic_id=?", (tid,)).fetchone()
        rows.append(["topic", tid, int(t["version"] if t else 0), str(t["status"] if t else "")])
        for b in repo.conn.execute("SELECT block_id,block_type,object_ref_id,version,status FROM content_blocks WHERE topic_id=? ORDER BY position,block_id", (tid,)):
            rows.append(["block", str(b["block_id"]), int(b["version"] or 0), str(b["status"] or "")])
            bt, oid = str(b["block_type"] or ""), str(b["object_ref_id"] or "")
            if bt == "table" and oid:
                r=repo.conn.execute("SELECT version,status FROM structured_tables WHERE table_id=?",(oid,)).fetchone()
                if r: rows.append(["table",oid,int(r["version"] or 0),str(r["status"] or "")])
            elif bt == "figure" and oid:
                r=repo.conn.execute("SELECT version,status FROM figures WHERE figure_id=?",(oid,)).fetchone()
                if r: rows.append(["figure",oid,int(r["version"] or 0),str(r["status"] or "")])
            elif bt == "component_ref" and oid:
                r=repo.conn.execute("SELECT version FROM components WHERE component_id=?",(oid,)).fetchone()
                if r: rows.append(["component",oid,int(r["version"] or 0),"N/A"])
    raw=json.dumps(rows,ensure_ascii=False,separators=(",",":"),sort_keys=False).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8],16)

def _publication_status(repo: IndustrialRepository, publication_id: str) -> str:
    linked=list(repo.conn.execute("SELECT topic_id FROM publication_topics WHERE publication_id=? ORDER BY position",(publication_id,)))
    if not linked:
        return "EMPTY"
    for link in linked:
        tid=str(link["topic_id"])
        t=repo.conn.execute("SELECT status FROM topics WHERE topic_id=?",(tid,)).fetchone()
        if not t or str(t["status"] or "") not in {"Approved","Published"}: return str(t["status"] if t else "MISSING")
        for b in repo.conn.execute("SELECT status FROM content_blocks WHERE topic_id=?",(tid,)):
            if str(b["status"] or "") not in {"Approved","Published"}: return str(b["status"] or "Draft")
    return "Approved"

def _object_source_lang(repo: IndustrialRepository, changed_id: str, kind: str) -> str:
    if kind == "publication":
        row=repo.conn.execute("SELECT source_lang FROM publications WHERE publication_id=?",(changed_id,)).fetchone()
        if not row: raise KeyError(changed_id)
        return canonical_lang(str(row["source_lang"] or ""))
    if kind == "topic":
        row = repo.conn.execute("SELECT source_lang FROM topics WHERE topic_id=?", (changed_id,)).fetchone()
        if not row:
            raise KeyError(changed_id)
        return canonical_lang(str(row["source_lang"] or ""))
    if kind == "component":
        row = repo.conn.execute("SELECT source_lang FROM components WHERE component_id=?", (changed_id,)).fetchone()
        if not row:
            raise KeyError(changed_id)
        return canonical_lang(str(row["source_lang"] or ""))
    if kind == "block":
        row = repo.conn.execute(
            "SELECT t.source_lang FROM content_blocks b JOIN topics t ON t.topic_id=b.topic_id WHERE b.block_id=?",
            (changed_id,),
        ).fetchone()
        if not row:
            raise KeyError(changed_id)
        return canonical_lang(str(row["source_lang"] or ""))
    if kind in {"table", "figure"}:
        block_type = kind
        row = repo.conn.execute(
            """SELECT t.source_lang FROM content_blocks b
               JOIN topics t ON t.topic_id=b.topic_id
               WHERE b.block_type=? AND b.object_ref_id=? ORDER BY b.topic_id LIMIT 1""",
            (block_type, changed_id),
        ).fetchone()
        return canonical_lang(str(row["source_lang"] or "")) if row else ""
    raise ValueError(f"kind non supportato: {kind}")


def _object_meta(repo: IndustrialRepository, changed_id: str, kind: str) -> dict[str, Any]:
    if kind == "publication":
        row=repo.conn.execute("SELECT * FROM publications WHERE publication_id=?",(changed_id,)).fetchone()
        if not row: raise KeyError(changed_id)
        d=dict(row); d["source_lang"]=_object_source_lang(repo,changed_id,kind); d["version"]=publication_revision_token(repo,changed_id); d["status"]=_publication_status(repo,changed_id)
        return d
    table_by_kind = {
        "topic": ("topics", "topic_id"),
        "block": ("content_blocks", "block_id"),
        "table": ("structured_tables", "table_id"),
        "figure": ("figures", "figure_id"),
        "component": ("components", "component_id"),
    }
    if kind not in table_by_kind:
        raise ValueError(f"kind non supportato: {kind}")
    table, key = table_by_kind[kind]
    row = repo.conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (changed_id,)).fetchone()
    if not row:
        raise KeyError(changed_id)
    d = dict(row)
    d["source_lang"] = _object_source_lang(repo, changed_id, kind)
    return d


def _table_units(repo: IndustrialRepository, table_id: str, *, prefix: str = "") -> list[SourceUnit]:
    r = repo.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
    if not r:
        raise KeyError(table_id)
    base = prefix or table_id
    units: list[SourceUnit] = []
    for label, value in [("title", r["title"]), ("caption", r["caption"])]:
        text = str(value or "").strip()
        if text:
            units.append(SourceUnit(f"{base}:{label}", "table", table_id, text, f"{table_id}#{label}"))
    columns = _json_load(r["columns_json"], [])
    rows = _json_load(r["rows_json"], [])
    for ci, cell in enumerate(columns, start=1):
        text = str(cell or "").strip()
        if text:
            units.append(SourceUnit(f"{base}:header:{ci}", "table", table_id, text, f"{table_id}#header[{ci}]"))
    for ri, row in enumerate(rows, start=1):
        for ci, cell in enumerate(row, start=1):
            text = str(cell or "").strip()
            if text:
                units.append(SourceUnit(f"{base}:r{ri}c{ci}", "table", table_id, text, f"{table_id}#row[{ri}]col[{ci}]"))
    return units


def _figure_units(repo: IndustrialRepository, figure_id: str, *, prefix: str = "") -> list[SourceUnit]:
    r = repo.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
    if not r:
        raise KeyError(figure_id)
    base = prefix or figure_id
    units: list[SourceUnit] = []
    for label in ["title", "caption", "alt_text"]:
        text = str(r[label] or "").strip()
        if text:
            units.append(SourceUnit(f"{base}:{label}", "figure", figure_id, text, f"{figure_id}#{label}"))
    return units


def _component_units(repo: IndustrialRepository, component_id: str, *, prefix: str = "") -> list[SourceUnit]:
    r = repo.conn.execute("SELECT * FROM components WHERE component_id=?", (component_id,)).fetchone()
    if not r:
        raise KeyError(component_id)
    base = prefix or component_id
    units: list[SourceUnit] = []
    title = str(r["title"] or "").strip()
    body = str(r["body"] or "").strip()
    if title:
        units.append(SourceUnit(f"{base}:title", "component", component_id, title, f"{component_id}#title"))
    if body:
        units.append(SourceUnit(f"{base}:body", "component", component_id, body, f"{component_id}#body"))
    return units


def _block_text_source_unit(block_row: Any, *, unit_id: str) -> "SourceUnit | None":
    """4E.3 stabilization (Blocker 4): the ONE place a text-kind block's
    content becomes a translation SourceUnit -- used by both the
    'publication'/'block'-kind branch and the 'topic'-kind branch below
    (previously two independent copies of the same three lines).

    A 'rich' block's raw text_content can contain this milestone's
    canonical markup ('**', '_', '`', '[]()' -- possibly backslash-
    escaped literals too) -- none of that may ever reach CAT/TM
    segmentation or a translator's screen as ordinary source text; a
    translator has no way to know '**' is formatting syntax rather than
    two literal asterisks, and round-tripping it back through
    _append_text_with_inline's OWN unrelated inline-placeholder
    tokenizer would silently swallow it as an opaque protected code,
    not a semantic mark. A rich block with genuine formatting
    (rich_text.has_rich_structure) is extracted here using its
    VISIBLE-TEXT projection instead (rich_text.plain_text() -- marks/
    URLs stripped) and its source_ref carries RICH_FORMAT_UNSAFE_MARKER
    -- this function itself stays a pure, read-only ANALYSIS/PREVIEW
    helper (used e.g. by impact-analysis reporting) and never authorizes
    anything by itself. The actual FAIL-CLOSED enforcement -- refusing
    to create a translation work package at all when this marker is
    present -- lives one layer up, in translation_work_package.
    generate_translation_work_packages()'s own pre-flight check, which
    runs and can raise BEFORE this function (or any file/directory) is
    ever touched for that operation. A 'plain' block (the vast majority,
    and every block with no genuine marks even if flagged 'rich') is
    extracted completely unchanged from pre-4E.3 behavior."""
    raw_text = str(block_row["text_content"] or "").strip()
    if not raw_text:
        return None
    text_format = block_row["text_format"] if "text_format" in block_row.keys() else "plain"
    version_ref = f"v{block_row['version']}"
    if text_format == "rich" and rich_text.has_rich_structure(raw_text):
        visible = rich_text.plain_text(raw_text, text_format)
        if not visible:
            return None
        return SourceUnit(unit_id, "block", unit_id, visible,
                          f"{unit_id}#{version_ref}{RICH_FORMAT_UNSAFE_MARKER} — formattazione rich non riportata: "
                          "verificare/riformattare dopo la traduzione")
    return SourceUnit(unit_id, "block", unit_id, raw_text, f"{unit_id}#{version_ref}")


def extract_source_units(repo: IndustrialRepository, changed_id: str, *, kind: str) -> tuple[list[SourceUnit], dict[str, Any]]:
    """Extract current approved source content at the smallest practical unit.

    For topics, structured blocks are preferred. Table cells, figure captions and
    component bodies are first-class units so technical numbers/codes are never
    hidden inside a coarse topic-level word count.
    """
    kind = str(kind or "").strip().lower()
    meta = _object_meta(repo, changed_id, kind)
    units: list[SourceUnit] = []

    if kind == "publication":
        links=list(repo.conn.execute("SELECT topic_id FROM publication_topics WHERE publication_id=? ORDER BY position,topic_id",(changed_id,)))
        if not links: raise ValueError(f"Pubblicazione vuota: {changed_id}")
        for link in links:
            topic_units,_=extract_source_units(repo,str(link["topic_id"]),kind="topic")
            units.extend(topic_units)

    elif kind == "block":
        r = repo.conn.execute("SELECT * FROM content_blocks WHERE block_id=?", (changed_id,)).fetchone()
        bt = str(r["block_type"])
        if bt == "table":
            units.extend(_table_units(repo, str(r["object_ref_id"]), prefix=changed_id))
        elif bt == "figure":
            units.extend(_figure_units(repo, str(r["object_ref_id"]), prefix=changed_id))
        elif bt == "component_ref":
            units.extend(_component_units(repo, str(r["object_ref_id"]), prefix=changed_id))
        else:
            unit = _block_text_source_unit(r, unit_id=changed_id)
            if unit is not None:
                units.append(unit)

    elif kind == "topic":
        title = str(meta.get("title") or "").strip()
        if title:
            units.append(SourceUnit(f"{changed_id}:title", "topic_title", changed_id, title, f"{changed_id}#title"))
        blocks = list(repo.conn.execute("SELECT * FROM content_blocks WHERE topic_id=? ORDER BY position,block_id", (changed_id,)))
        if blocks:
            for b in blocks:
                bid = str(b["block_id"])
                bt = str(b["block_type"])
                if bt == "table":
                    units.extend(_table_units(repo, str(b["object_ref_id"]), prefix=bid))
                elif bt == "figure":
                    units.extend(_figure_units(repo, str(b["object_ref_id"]), prefix=bid))
                elif bt == "component_ref":
                    units.extend(_component_units(repo, str(b["object_ref_id"]), prefix=bid))
                else:
                    unit = _block_text_source_unit(b, unit_id=bid)
                    if unit is not None:
                        units.append(unit)
        else:
            text = str(meta.get("body") or "").strip()
            if text:
                units.append(SourceUnit(changed_id, "topic", changed_id, text, f"{changed_id}#v{meta.get('version',1)}"))

    elif kind == "table":
        units.extend(_table_units(repo, changed_id))
    elif kind == "figure":
        units.extend(_figure_units(repo, changed_id))
    elif kind == "component":
        units.extend(_component_units(repo, changed_id))

    if not units:
        raise ValueError(f"Nessun contenuto sorgente analizzabile per {kind}:{changed_id}")
    return units, meta


def units_to_segments(units: Iterable[SourceUnit]) -> tuple[list[CATSegment], pd.DataFrame]:
    staged: list[dict[str, Any]] = []
    for unit in units:
        sentences = split_sentences(unit.source_text) or [unit.source_text]
        for si, sentence in enumerate(sentences, start=1):
            text = str(sentence or "").strip()
            normalized = cat_normalize(text)
            # R11: keep meaningful one-character alphanumeric units (e.g. "1", "A").
            # They may be language-invariant but must still remain inside the controlled
            # translation/QA coverage instead of disappearing from the work package.
            if not normalized or not any(ch.isalnum() for ch in normalized):
                continue
            staged.append({
                "unit_id": unit.unit_id,
                "object_kind": unit.object_kind,
                "object_id": unit.object_id,
                "source_ref": unit.source_ref,
                "source": text,
                "sentence_index": si,
            })
    norms = [cat_normalize(x["source"]) for x in staged]
    segments: list[CATSegment] = []
    meta_rows: list[dict[str, Any]] = []
    for i, row in enumerate(staged):
        sid = f"impact-{i+1:05d}"
        text = row["source"]
        segments.append(CATSegment(
            segment_id=sid,
            text=text,
            normalized=norms[i],
            prev_normalized=norms[i-1] if i > 0 else "",
            next_normalized=norms[i+1] if i + 1 < len(norms) else "",
            page=0,
            block_index=i,
            heading=False,
            signature=technical_signature(text),
        ))
        meta_rows.append({**row, "segment_id": sid, "word_count": word_count(text)})
    return segments, pd.DataFrame(meta_rows)


def _tm_pairs(path: str | Path) -> list[tuple[str, str]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    conn = sqlite3.connect(p)
    try:
        rows = conn.execute("SELECT DISTINCT source_lang,target_lang FROM tm_entries ORDER BY source_lang,target_lang").fetchall()
        return [(str(a), str(b)) for a, b in rows]
    finally:
        conn.close()


def discover_tm_languages(tm_paths: Iterable[str | Path], source_lang: str) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for raw in tm_paths:
        if not raw:
            continue
        p = Path(raw)
        for src, tgt in _tm_pairs(p):
            if _lang_base(src) == _lang_base(source_lang):
                result.setdefault(canonical_lang(tgt), []).append(p)
    return result


def _consolidated_tm(paths: list[Path], source_lang: str, target_lang: str, out_path: Path) -> TranslationMemory:
    out_path.unlink(missing_ok=True)
    dest = TranslationMemory(out_path)
    for p in paths:
        src_tm = TranslationMemory(p)
        try:
            # Read all entries so en/en-US variants can be consolidated by base language.
            rows = list(src_tm.conn.execute("SELECT * FROM tm_entries"))
            for row in rows:
                if _lang_base(row["source_lang"]) != _lang_base(source_lang) or str(row["target_lang"]).casefold() != str(target_lang).casefold():
                    continue
                dest.add(
                    source_lang, target_lang, row["source"], row["target"],
                    prev_norm=str(row["prev_norm"] or ""), next_norm=str(row["next_norm"] or ""),
                    origin=str(row["origin"] or p.name), alignment_confidence=float(row["alignment_confidence"] or 0),
                    approved=bool(row["approved"]), tm_name=f"{p.stem}:{row['tm_name']}",
                    priority=int(row["priority"] or 0), penalty=float(row["penalty"] or 0),
                )
        finally:
            src_tm.close()
    return dest


def _translation_assets(repo: IndustrialRepository, changed_id: str, kind: str, current_version: int) -> pd.DataFrame:
    mappings = {
        "topic": ("topic_translations", "topic_id"),
        "block": ("block_translations", "block_id"),
        "table": ("structured_table_translations", "table_id"),
        "figure": ("figure_translations", "figure_id"),
    }
    if kind not in mappings:
        return pd.DataFrame(columns=["target_lang","translation_status","translation_source_version","translation_record_status"])
    table, key = mappings[kind]
    rows = []
    for r in repo.conn.execute(f"SELECT target_lang,source_version,status FROM {table} WHERE {key}=? ORDER BY target_lang", (changed_id,)):
        source_version = int(r["source_version"])
        rows.append({
            "target_lang": canonical_lang(str(r["target_lang"])),
            "translation_status": "FRESH" if source_version == current_version else "STALE",
            "translation_source_version": source_version,
            "translation_record_status": str(r["status"]),
        })
    return pd.DataFrame(rows)


def _decision_bucket(row: pd.Series) -> str:
    decision = str(row.get("decision", ""))
    category = str(row.get("match_category", ""))
    if decision == "RIUSO AUTOMATICO CONSENTITO":
        return "AUTO_REUSE"
    if decision == "BLOCCATO — REVISIONE TECNICA" or category == "TECHNICAL SAFE BLOCK":
        return "TECHNICAL_REVIEW"
    if "REVISIONE OBBLIGATORIA" in decision or category.startswith("FUZZY") or "PENALIZZATO" in category:
        return "LINGUISTIC_REVIEW"
    return "NEW_TRANSLATION"


def _rate_lookup(rate_card: pd.DataFrame | None) -> dict[str, dict[str, float]]:
    if rate_card is None or rate_card.empty:
        return {}
    required = ["target_lang","rate_new","rate_fuzzy","rate_reuse","rate_technical_block"]
    missing = [c for c in required if c not in rate_card.columns]
    if missing:
        raise ValueError("Rate card incompleta: " + ", ".join(missing))
    out: dict[str, dict[str, float]] = {}
    for _, r in rate_card.fillna(0).iterrows():
        lang = str(r["target_lang"] or "").strip()
        if not lang:
            continue
        out[lang.casefold()] = {
            "AUTO_REUSE": max(0.0, float(r["rate_reuse"])),
            "LINGUISTIC_REVIEW": max(0.0, float(r["rate_fuzzy"])),
            "TECHNICAL_REVIEW": max(0.0, float(r["rate_technical_block"])),
            "NEW_TRANSLATION": max(0.0, float(r["rate_new"])),
        }
    return out


def analyze_translation_impact(
    repo: IndustrialRepository,
    changed_id: str,
    *,
    kind: str,
    tm_paths: Iterable[str | Path] = (),
    fuzzy_threshold: float = 75.0,
    rate_card: pd.DataFrame | None = None,
    target_langs: Iterable[str] = (),
    require_approved: bool = True,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    kind = str(kind or "").strip().lower()
    changed_id = str(changed_id or "").strip()
    if not changed_id:
        raise ValueError("changed_id obbligatorio")

    units, meta = extract_source_units(repo, changed_id, kind=kind)
    current_version = int(meta.get("version", 1) or 1)
    source_status = str(meta.get("status", "N/A") or "N/A")
    if require_approved and kind != "component" and source_status not in {"Approved", "Published"}:
        raise ValueError(f"Oggetto non approvato: status={source_status}. Translation Impact opera fail-closed sul contenuto ufficiale.")
    source_lang = canonical_lang(str(meta.get("source_lang") or ""))
    if not source_lang:
        raise ValueError("Lingua sorgente non determinabile dal CCMS")

    segments, segment_meta = units_to_segments(units)
    if not segments:
        raise ValueError("Nessun segmento sorgente analizzabile")

    tm_paths_clean = [Path(x) for x in tm_paths if x]
    tm_by_lang = discover_tm_languages(tm_paths_clean, source_lang)
    existing_assets = _translation_assets(repo, changed_id, kind, current_version)
    existing_langs = set(canonical_langs(existing_assets["target_lang"].astype(str).tolist())) if not existing_assets.empty else set()
    requested_langs=set(canonical_langs(target_langs))
    target_langs = sorted(existing_langs | set(tm_by_lang) | requested_langs)
    if not target_langs:
        raise ValueError("Nessuna lingua target trovata: collega almeno una TM ManualSync o una traduzione CCMS esistente.")

    tmp_root = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="manualsync_v22_impact_"))
    tmp_root.mkdir(parents=True, exist_ok=True)
    all_rows: list[pd.DataFrame] = []

    for target_lang in target_langs:
        paths = tm_by_lang.get(target_lang, [])
        if paths:
            consolidated = _consolidated_tm(paths, source_lang, target_lang, tmp_root / f"_tm_{target_lang.replace('/','_')}.sqlite")
            try:
                matched = match_segments(consolidated, segments, source_lang, target_lang, fuzzy_threshold=float(fuzzy_threshold))
            finally:
                consolidated.close()
            frame = matched.merge(segment_meta, on=["segment_id","source"], how="left")
            frame["tm_available"] = True
        else:
            frame = segment_meta.copy()
            frame["match_category"] = "NO TM"
            frame["linguistic_score"] = 0.0
            frame["raw_linguistic_score"] = 0.0
            frame["technical_safe"] = pd.NA
            frame["technical_reason"] = "TM non disponibile per questa lingua"
            frame["decision"] = "TRADUZIONE / ANALISI NECESSARIA"
            frame["pretranslation"] = ""
            frame["suggested_translation"] = ""
            frame["tm_name"] = ""
            frame["tm_available"] = False
        frame["target_lang"] = target_lang
        frame["work_bucket"] = frame.apply(_decision_bucket, axis=1)
        frame["repeat_of_segment_id"] = ""

        # Commercial efficiency: obvious codes/numbers/symbol labels do not belong
        # in a translator workfile. They remain controlled in the package snapshot
        # and are propagated unchanged during round-trip commit.
        nontrans_mask = frame["source"].map(is_nontranslatable_text)
        if nontrans_mask.any():
            frame.loc[nontrans_mask, "work_bucket"] = "NOT_TRANSLATABLE"
            frame.loc[nontrans_mask, "decision"] = "NON TRADUCIBILE — PRESERVA IDENTICO"
            frame.loc[nontrans_mask, "match_category"] = "NOT_TRANSLATABLE"
            frame.loc[nontrans_mask, "pretranslation"] = frame.loc[nontrans_mask, "source"]
            frame.loc[nontrans_mask, "suggested_translation"] = frame.loc[nontrans_mask, "source"]
            frame.loc[nontrans_mask, "technical_reason"] = "codice/numero/simbolo o identificativo tecnico invariabile"

        # Exact intra-document repetitions are translated once. Only substantial
        # linguistic strings are deduplicated; short labels stay independent to
        # avoid context-sensitive false reuse. The locked duplicate later inherits
        # the canonical segment target during round-trip.
        canonical_by_norm: dict[str, str] = {}
        for idx, row in frame.iterrows():
            if str(row.get("work_bucket", "")) in {"AUTO_REUSE", "NOT_TRANSLATABLE"}:
                continue
            text = str(row.get("source", "") or "").strip()
            norm = cat_normalize(text)
            alpha = sum(ch.isalpha() for ch in text)
            if not norm or (len(text.split()) < 3 and alpha < 16):
                continue
            first_sid = canonical_by_norm.get(norm)
            if not first_sid:
                canonical_by_norm[norm] = str(row.get("segment_id", ""))
                continue
            frame.at[idx, "work_bucket"] = "INTRA_DOCUMENT_REUSE"
            frame.at[idx, "decision"] = "RIUSO INTRA-DOCUMENTO — EREDITA TARGET DAL PRIMO SEGMENTO"
            frame.at[idx, "match_category"] = "EXACT_REPEAT"
            frame.at[idx, "repeat_of_segment_id"] = first_sid
            frame.at[idx, "pretranslation"] = ""
            frame.at[idx, "suggested_translation"] = ""
            frame.at[idx, "technical_reason"] = f"ripetizione esatta del segmento {first_sid}"

        all_rows.append(frame)

    detail = pd.concat(all_rows, ignore_index=True)
    asset_map = {canonical_lang(str(r["target_lang"])): dict(r) for _, r in existing_assets.iterrows()} if not existing_assets.empty else {}
    detail["ccms_translation_status"] = detail["target_lang"].map(lambda x: asset_map.get(canonical_lang(str(x)), {}).get("translation_status", "MISSING"))
    detail["ccms_translation_source_version"] = detail["target_lang"].map(lambda x: asset_map.get(canonical_lang(str(x)), {}).get("translation_source_version", pd.NA))
    detail["current_source_version"] = current_version

    rates = _rate_lookup(rate_card)
    detail["rate_per_word"] = detail.apply(lambda r: 0.0 if str(r["work_bucket"]) in {"NOT_TRANSLATABLE","INTRA_DOCUMENT_REUSE"} else rates.get(str(r["target_lang"]).casefold(), {}).get(str(r["work_bucket"]), pd.NA), axis=1)
    detail["estimated_cost"] = pd.to_numeric(detail["rate_per_word"], errors="coerce") * pd.to_numeric(detail["word_count"], errors="coerce").fillna(0)

    summary_rows: list[dict[str, Any]] = []
    for lang, g in detail.groupby("target_lang", sort=True):
        words = int(g["word_count"].sum())
        bucket_words = {b: int(g.loc[g["work_bucket"] == b, "word_count"].sum()) for b in ["AUTO_REUSE","LINGUISTIC_REVIEW","TECHNICAL_REVIEW","NEW_TRANSLATION","NOT_TRANSLATABLE","INTRA_DOCUMENT_REUSE"]}
        cost_known = g.loc[g["work_bucket"].isin(["AUTO_REUSE","LINGUISTIC_REVIEW","TECHNICAL_REVIEW","NEW_TRANSLATION"]), "estimated_cost"].notna().all()
        summary_rows.append({
            "target_lang": lang,
            "segments": int(len(g)),
            "source_words": words,
            "auto_reuse_words": bucket_words["AUTO_REUSE"],
            "linguistic_review_words": bucket_words["LINGUISTIC_REVIEW"],
            "technical_review_words": bucket_words["TECHNICAL_REVIEW"],
            "new_translation_words": bucket_words["NEW_TRANSLATION"],
            "nontranslatable_words": bucket_words["NOT_TRANSLATABLE"],
            "intra_document_reuse_words": bucket_words["INTRA_DOCUMENT_REUSE"],
            "human_attention_words": bucket_words["LINGUISTIC_REVIEW"] + bucket_words["TECHNICAL_REVIEW"] + bucket_words["NEW_TRANSLATION"],
            "reuse_pct": round(((bucket_words["AUTO_REUSE"] + bucket_words["INTRA_DOCUMENT_REUSE"] + bucket_words["NOT_TRANSLATABLE"]) / words * 100) if words else 0.0, 1),
            "tm_available": bool(g["tm_available"].any()),
            "ccms_translation_status": str(g["ccms_translation_status"].iloc[0]),
            "estimated_cost": round(float(g["estimated_cost"].sum()), 2) if cost_known else pd.NA,
        })
    language_summary = pd.DataFrame(summary_rows)

    if kind == "publication":
        rows=[]
        for link in repo.conn.execute("SELECT topic_id,position FROM publication_topics WHERE publication_id=? ORDER BY position",(changed_id,)):
            rows.append({"changed_kind":kind,"changed_id":changed_id,"topic_id":str(link["topic_id"]),"publication_id":changed_id,"position":int(link["position"])})
        impact_map=pd.DataFrame(rows)
    else:
        impact_map = repo.impact_map_industrial(changed_id, kind=kind)
    affected_publications = int(impact_map["publication_id"].astype(str).nunique()) if not impact_map.empty else (1 if kind=="publication" else 0)
    affected_topics = int(impact_map["topic_id"].astype(str).nunique()) if not impact_map.empty else (1 if kind == "topic" else 0)
    total_source_words_once = int(segment_meta["word_count"].sum())
    total_human_words = int(language_summary["human_attention_words"].sum()) if not language_summary.empty else 0
    total_reuse_words = int(language_summary["auto_reuse_words"].sum()) if not language_summary.empty else 0
    rates_complete = bool(not language_summary.empty and language_summary["estimated_cost"].notna().all())
    total_cost = round(float(pd.to_numeric(language_summary["estimated_cost"], errors="coerce").sum()), 2) if rates_complete else None

    overall = {
        "build": TRANSLATION_IMPACT_BUILD,
        "changed_kind": kind,
        "changed_id": changed_id,
        "source_status": source_status,
        "source_version": current_version,
        "source_lang": source_lang,
        "source_units": len(units),
        "source_segments": len(segments),
        "source_words": total_source_words_once,
        "target_languages": len(target_langs),
        "affected_topics": affected_topics,
        "affected_publications": affected_publications,
        "auto_reuse_words_across_languages": total_reuse_words,
        "human_attention_words_across_languages": total_human_words,
        "estimated_cost": total_cost,
        "cost_available": rates_complete,
        "tm_files": [str(p) for p in tm_paths_clean],
    }

    detail_path = tmp_root / "TRANSLATION_IMPACT_SEGMENTS.csv"
    summary_path = tmp_root / "TRANSLATION_IMPACT_LANGUAGES.csv"
    json_path = tmp_root / "TRANSLATION_IMPACT_SUMMARY.json"
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    language_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    json_path.write_text(json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "overall": overall,
        "language_summary": language_summary,
        "detail": detail,
        "impact_map": impact_map,
        "detail_csv": detail_path,
        "language_csv": summary_path,
        "summary_json": json_path,
    }
