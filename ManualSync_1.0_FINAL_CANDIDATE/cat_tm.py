from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
from rapidfuzz.fuzz import ratio

from layout_engine import extract_layout, map_translation_blocks
from language_codes import canonical_lang

CAT_BUILD = "0.14.1-cat-code-safety-20260818"

# Keep signs on numbers. Decimal comma and decimal point are canonicalized.
NUM_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\s*\d+(?:[.,]\d+)?(?![A-Za-z0-9])")
# Technical model/device codes are intentionally case-sensitive.
# The previous case-insensitive pattern treated ordinary hyphenated prose such as
# "short-circuit" and "marine-type" as immutable model codes, which could block
# perfectly valid translations. Real device/model identifiers in controlled
# technical documentation are expected to preserve their uppercase/digit form.
MODEL_RE = re.compile(
    r"\b(?:R\d+(?:\s*[vV]\d+)?|[A-Z]{1,8}\d[A-Z0-9._/-]*|\d+[A-Z]+-[A-Z0-9._/-]+|[A-Z]{2,8}[-_][A-Z0-9._/-]+)\b"
)
UNIT_RE_CI = re.compile(
    r"(?<![A-Za-z])(?:%|°\s?[CF]|kHz|MHz|Hz|kW|mA|Nm|N·m|mm|cm|ms|bar|kg|rpm|r/min)(?![A-Za-z])",
    re.I,
)
UNIT_RE_CS = re.compile(r"(?<![A-Za-z])(?:A|V|W|m|s|g)(?![A-Za-z])")
SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=(?:[A-ZÀ-ÖØ-Þ0-9•▪‣]|\())")
ABBREVIATIONS = ("e.g.", "i.e.", "fig.", "no.", "nr.", "pag.", "p.", "etc.", "es.", "rif.")

# Inline tokens that must survive translation unchanged. This intentionally covers
# the most common technical placeholders encountered in XML/HTML, software strings
# and parameterized manuals without pretending to be a full XML parser.
INLINE_TOKEN_RE = re.compile(
    r"(?:</?[A-Za-z][^<>]{0,120}>|"
    r"\{\{[^{}]{1,120}\}\}|"
    r"\$\{[^{}]{1,120}\}|"
    r"\{[A-Za-z_][A-Za-z0-9_.:-]*\}|"
    r"%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[a-zA-Z]|"
    r"%\d+|"
    r"&[A-Za-z][A-Za-z0-9]+;|"
    r"\[\[[A-Za-z_][A-Za-z0-9_.:-]*\]\])"
)


def inline_tokens(text: str) -> tuple[str, ...]:
    """Return ordered inline tags/placeholders that must be preserved."""
    return tuple(m.group(0) for m in INLINE_TOKEN_RE.finditer(text or ""))


def inline_tokens_compatible(source: str, target: str) -> tuple[bool, str]:
    src = inline_tokens(source)
    tgt = inline_tokens(target)
    if Counter(src) != Counter(tgt):
        return False, f"tag/placeholder diversi: {list(src)} ≠ {list(tgt)}"
    return True, ""


@dataclass(frozen=True)
class TechnicalSignature:
    numbers: tuple[str, ...]
    codes: tuple[str, ...]
    units: tuple[str, ...]


@dataclass
class CATSegment:
    segment_id: str
    text: str
    normalized: str
    prev_normalized: str
    next_normalized: str
    page: int
    block_index: int
    heading: bool
    signature: TechnicalSignature


@dataclass
class TMEntry:
    source_lang: str
    target_lang: str
    source: str
    target: str
    source_norm: str
    prev_norm: str
    next_norm: str
    numbers_json: str
    codes_json: str
    units_json: str
    origin: str
    alignment_confidence: float
    approved: int
    created_at: str


def _num(value: str) -> str:
    value = re.sub(r"\s+", "", value).replace(",", ".")
    try:
        n = float(value)
        if n == 0:
            n = 0.0
        return f"{n:.12g}"
    except Exception:
        return value


def cat_normalize(text: str) -> str:
    raw = (text or "").replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl")
    for old, new in {"…": "...", "–": "-", "—": "-", "−": "-", "≥": ">=", "≤": "<="}.items():
        raw = raw.replace(old, new)
    raw = re.sub(r"(?<!\w)([-+])\s+(?=\d)", r"\1", raw)
    raw = re.sub(r"(?<=\d),(?=\d)", ".", raw)
    raw = raw.casefold()
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"[^\w%°./+<>=-]+", " ", raw, flags=re.UNICODE)
    return raw.strip()


def technical_signature(text: str) -> TechnicalSignature:
    raw = (text or "").replace("−", "-").replace("–", "-").replace("—", "-")
    numbers = tuple(_num(m.group(0)) for m in NUM_RE.finditer(raw))
    codes = tuple(dict.fromkeys(m.group(0).replace(" ", "").upper() for m in MODEL_RE.finditer(raw)))
    unit_values = [m.group(0) for m in UNIT_RE_CI.finditer(raw)] + [m.group(0) for m in UNIT_RE_CS.finditer(raw)]
    units = tuple(dict.fromkeys(re.sub(r"\s+", "", u).casefold() for u in unit_values))
    return TechnicalSignature(numbers=numbers, codes=codes, units=units)


def signatures_compatible(a: TechnicalSignature, b: TechnicalSignature) -> tuple[bool, str]:
    if Counter(a.numbers) != Counter(b.numbers):
        return False, f"valori numerici diversi: {list(a.numbers)} ≠ {list(b.numbers)}"
    if Counter(a.codes) != Counter(b.codes):
        return False, f"codici/modelli diversi: {list(a.codes)} ≠ {list(b.codes)}"
    if Counter(a.units) != Counter(b.units):
        return False, f"unità tecniche diverse: {list(a.units)} ≠ {list(b.units)}"
    return True, ""


def source_target_safe(source: str, target: str) -> tuple[bool, str]:
    """Conservative bilingual gate for numbers/codes.

    Units may be translated or reordered, so the hard bilingual gate checks numeric
    values and model/device codes. The source-to-new-source gate also checks units.
    """
    s = technical_signature(source)
    t = technical_signature(target)
    if Counter(s.numbers) != Counter(t.numbers):
        return False, f"numeri sorgente/traduzione non coerenti: {list(s.numbers)} ≠ {list(t.numbers)}"
    if Counter(s.codes) != Counter(t.codes):
        return False, f"codici sorgente/traduzione non coerenti: {list(s.codes)} ≠ {list(t.codes)}"
    inline_ok, inline_reason = inline_tokens_compatible(source, target)
    if not inline_ok:
        return False, inline_reason
    return True, ""


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    if len(text) < 160 and not re.search(r"[.!?]\s+[A-ZÀ-ÖØ-Þ]", text):
        return [text]
    protected = text
    replacements: dict[str, str] = {}
    for i, abbr in enumerate(ABBREVIATIONS):
        token = f"__ABBR{i}__"
        pattern = re.compile(re.escape(abbr), re.I)
        if pattern.search(protected):
            replacements[token] = abbr
            protected = pattern.sub(token, protected)
    parts = [p.strip() for p in SENTENCE_BOUNDARY.split(protected) if p.strip()]
    restored = []
    for part in parts:
        for token, abbr in replacements.items():
            part = part.replace(token, abbr)
        restored.append(part)
    return restored or [text]


def _aligned_pairs(source_text: str, target_text: str) -> list[tuple[str, str]]:
    ss = split_sentences(source_text)
    ts = split_sentences(target_text)
    if len(ss) == len(ts) and 0 < len(ss) <= 12:
        return list(zip(ss, ts))
    return [(re.sub(r"\s+", " ", source_text).strip(), re.sub(r"\s+", " ", target_text).strip())]


def segments_from_layout(pdf_path: str | Path, page_spec: str = "") -> list[CATSegment]:
    layout = extract_layout(pdf_path, page_spec)
    raw_segments: list[tuple[str, int, int, bool]] = []
    for block in layout.text_blocks:
        for sentence in split_sentences(block.text):
            if len(cat_normalize(sentence)) < 2:
                continue
            raw_segments.append((sentence, block.page, block.index, bool(block.heading)))
    result: list[CATSegment] = []
    norms = [cat_normalize(x[0]) for x in raw_segments]
    for i, (text, page, block_index, heading) in enumerate(raw_segments):
        result.append(CATSegment(
            segment_id=f"seg-{i+1:05d}",
            text=text,
            normalized=norms[i],
            prev_normalized=norms[i-1] if i > 0 else "",
            next_normalized=norms[i+1] if i + 1 < len(norms) else "",
            page=page + 1,
            block_index=block_index,
            heading=heading,
            signature=technical_signature(text),
        ))
    return result


class TranslationMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tm_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                source_norm TEXT NOT NULL,
                prev_norm TEXT DEFAULT '',
                next_norm TEXT DEFAULT '',
                numbers_json TEXT DEFAULT '[]',
                codes_json TEXT DEFAULT '[]',
                units_json TEXT DEFAULT '[]',
                origin TEXT DEFAULT '',
                alignment_confidence REAL DEFAULT 0,
                approved INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                tm_name TEXT DEFAULT 'Project TM',
                priority INTEGER DEFAULT 100,
                penalty REAL DEFAULT 0,
                UNIQUE(source_lang, target_lang, source_norm, target, prev_norm, next_norm, tm_name)
            )
        """)
        # Backward-compatible migration for v0.12/v0.13 project memories.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(tm_entries)")}
        if "tm_name" not in columns:
            self.conn.execute("ALTER TABLE tm_entries ADD COLUMN tm_name TEXT DEFAULT 'Project TM'")
        if "priority" not in columns:
            self.conn.execute("ALTER TABLE tm_entries ADD COLUMN priority INTEGER DEFAULT 100")
        if "penalty" not in columns:
            self.conn.execute("ALTER TABLE tm_entries ADD COLUMN penalty REAL DEFAULT 0")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_tm_norm ON tm_entries(source_lang,target_lang,source_norm)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_tm_profile ON tm_entries(tm_name,priority,penalty)")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def add(self, source_lang: str, target_lang: str, source: str, target: str,
            prev_norm: str = "", next_norm: str = "", origin: str = "",
            alignment_confidence: float = 100.0, approved: bool = True,
            tm_name: str = "Project TM", priority: int = 100, penalty: float = 0.0) -> int:
        source_lang = canonical_lang(source_lang)
        target_lang = canonical_lang(target_lang)
        sig = technical_signature(source)
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute("""
            INSERT OR IGNORE INTO tm_entries
            (source_lang,target_lang,source,target,source_norm,prev_norm,next_norm,numbers_json,codes_json,units_json,origin,alignment_confidence,approved,created_at,tm_name,priority,penalty)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            source_lang, target_lang, source.strip(), target.strip(), cat_normalize(source),
            prev_norm, next_norm, json.dumps(sig.numbers), json.dumps(sig.codes), json.dumps(sig.units),
            origin, float(alignment_confidence), 1 if approved else 0, now,
            (tm_name or "Project TM").strip(), int(priority), max(0.0, float(penalty)),
        ))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def entries(self, source_lang: str, target_lang: str, approved_only: bool = False) -> list[sqlite3.Row]:
        source_lang = canonical_lang(source_lang)
        target_lang = canonical_lang(target_lang)
        sql = "SELECT * FROM tm_entries WHERE REPLACE(source_lang,'_','-')=? COLLATE NOCASE AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE"
        args: list[object] = [source_lang, target_lang]
        if approved_only:
            sql += " AND approved=1"
        return list(self.conn.execute(sql, args))

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0])

    def profiles(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tm_name, priority, penalty, COUNT(*) AS entries FROM tm_entries GROUP BY tm_name,priority,penalty ORDER BY priority DESC, penalty ASC, tm_name"
        )
        return [dict(r) for r in rows]

    def import_tmx(self, tmx_path: str | Path, source_lang: str, target_lang: str, origin: str = "TMX import",
                   tm_name: str | None = None, priority: int = 50, penalty: float = 5.0) -> dict:
        root = ET.parse(tmx_path).getroot()
        xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
        added = 0
        skipped = 0
        rejected_unsafe = 0
        profile_name = tm_name or Path(tmx_path).stem
        for tu in root.findall(".//tu"):
            variants: dict[str, str] = {}
            for tuv in tu.findall("./tuv"):
                lang = tuv.attrib.get(xml_lang) or tuv.attrib.get("lang") or ""
                seg = tuv.find("./seg")
                if seg is None:
                    continue
                text = "".join(seg.itertext()).strip()
                variants[lang.casefold()] = text
            src = _lang_lookup(variants, source_lang)
            tgt = _lang_lookup(variants, target_lang)
            if not src or not tgt:
                skipped += 1
                continue
            ok, _ = source_target_safe(src, tgt)
            if not ok:
                rejected_unsafe += 1
            before = self.count()
            self.add(
                source_lang, target_lang, src, tgt, origin=origin, alignment_confidence=100, approved=ok,
                tm_name=profile_name, priority=priority, penalty=penalty,
            )
            added += self.count() - before
        return {
            "name": profile_name, "priority": int(priority), "penalty": float(penalty),
            "added": added, "skipped": skipped, "rejected_unsafe": rejected_unsafe, "total": self.count(),
        }

    def export_tmx(self, out_path: str | Path, source_lang: str, target_lang: str) -> Path:
        out_path = Path(out_path)
        root = ET.Element("tmx", version="1.4")
        ET.SubElement(root, "header", {
            "creationtool": "ManualSync",
            "creationtoolversion": CAT_BUILD,
            "segtype": "sentence",
            "o-tmf": "ManualSync SQLite TM",
            "adminlang": "en",
            "srclang": source_lang,
            "datatype": "PlainText",
        })
        body = ET.SubElement(root, "body")
        for row in self.entries(source_lang, target_lang):
            tu = ET.SubElement(body, "tu", {"tuid": str(row["id"])})
            properties = {
                "x-manualsync-origin": row["origin"],
                "x-manualsync-approved": "yes" if row["approved"] else "no",
                "x-manualsync-tm-name": row["tm_name"],
                "x-manualsync-priority": str(row["priority"]),
                "x-manualsync-penalty": str(row["penalty"]),
            }
            for prop_type, prop_value in properties.items():
                if prop_value != "":
                    prop = ET.SubElement(tu, "prop", {"type": prop_type})
                    prop.text = str(prop_value)
            for lang, text in ((source_lang, row["source"]), (target_lang, row["target"])):
                tuv = ET.SubElement(tu, "tuv", {"{http://www.w3.org/XML/1998/namespace}lang": lang})
                seg = ET.SubElement(tuv, "seg")
                seg.text = text
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
        return out_path

def _lang_lookup(variants: dict[str, str], requested: str) -> str:
    req = canonical_lang(requested)
    canon = {canonical_lang(k): v for k, v in variants.items()}
    if req in canon:
        return canon[req]
    base = req.split("-")[0]
    for key, value in canon.items():
        if key.split("-")[0] == base:
            return value
    return ""


def build_tm_from_documents(tm: TranslationMemory, old_source_pdf: str | Path, old_translation_pdf: str | Path,
                            source_lang: str, target_lang: str, old_pages: str = "", translation_pages: str = "",
                            min_alignment_confidence: float = 80.0, origin: str = "legacy documents",
                            tm_name: str = "Project Legacy", priority: int = 100, penalty: float = 0.0) -> pd.DataFrame:
    source_layout = extract_layout(old_source_pdf, old_pages)
    target_layout = extract_layout(old_translation_pdf, translation_pages)
    mapping = map_translation_blocks(source_layout.text_blocks, target_layout.text_blocks)
    staged: list[dict] = []

    # First build sentence pairs, then derive context on the source side.
    pairs: list[tuple[str, str, float, int, int]] = []
    for source_index, mapped in sorted(mapping.items()):
        if source_index >= len(source_layout.text_blocks):
            continue
        if not mapped:
            continue
        translation = str(mapped[0] or "")
        confidence = float(mapped[1] if len(mapped) > 1 else 0.0)
        target_index = int(mapped[2] if len(mapped) > 2 else -1)
        source = source_layout.text_blocks[source_index].text
        if not source.strip() or not translation.strip():
            continue
        for s, t in _aligned_pairs(source, translation):
            pairs.append((s, t, confidence, source_index, target_index))

    norms = [cat_normalize(p[0]) for p in pairs]
    for i, (source, target, confidence, source_idx, target_idx) in enumerate(pairs):
        safe, reason = source_target_safe(source, target)
        approved = confidence >= min_alignment_confidence and safe
        prev_norm = norms[i-1] if i > 0 else ""
        next_norm = norms[i+1] if i + 1 < len(norms) else ""
        before = tm.count()
        tm.add(
            source_lang, target_lang, source, target,
            prev_norm=prev_norm, next_norm=next_norm,
            origin=origin, alignment_confidence=confidence, approved=approved,
            tm_name=tm_name, priority=priority, penalty=penalty,
        )
        inserted = tm.count() > before
        staged.append({
            "source": source,
            "target": target,
            "alignment_confidence": round(confidence, 1),
            "bilingual_safe": safe,
            "approved": approved,
            "reason": reason,
            "inserted": inserted,
            "source_block": source_idx,
            "target_block": target_idx,
            "tm_name": tm_name,
            "priority": priority,
            "penalty": penalty,
        })
    return pd.DataFrame(staged)


def _row_signature(row: sqlite3.Row) -> TechnicalSignature:
    return TechnicalSignature(
        tuple(json.loads(row["numbers_json"])),
        tuple(json.loads(row["codes_json"])),
        tuple(json.loads(row["units_json"])),
    )


def _candidate_profile(row: sqlite3.Row) -> tuple[str, int, float]:
    keys = set(row.keys())
    name = str(row["tm_name"] if "tm_name" in keys else "Project TM")
    priority = int(row["priority"] if "priority" in keys else 100)
    penalty = max(0.0, float(row["penalty"] if "penalty" in keys else 0.0))
    return name, priority, penalty


def match_segments(tm: TranslationMemory, segments: list[CATSegment], source_lang: str, target_lang: str,
                   fuzzy_threshold: float = 75.0) -> pd.DataFrame:
    """Match source segments across one or more TM profiles.

    TM penalties reduce the effective linguistic score. An exact linguistic match with
    a non-zero penalty is deliberately downgraded to a suggestion, never automatic
    reuse. Technical compatibility remains a hard gate above every TM score.
    """
    entries = tm.entries(source_lang, target_lang, approved_only=False)
    by_norm: dict[str, list[sqlite3.Row]] = {}
    approved: list[sqlite3.Row] = []
    for row in entries:
        by_norm.setdefault(row["source_norm"], []).append(row)
        if row["approved"]:
            approved.append(row)

    rows: list[dict] = []
    for seg in segments:
        category = "NEW"
        score = 0.0
        raw_score = 0.0
        candidate_source = ""
        candidate_target = ""
        decision = "TRADUZIONE NECESSARIA"
        technical_safe = True
        technical_reason = ""
        entry_id: Optional[int] = None
        origin = ""
        alignment_confidence = 0.0
        tm_name = ""
        tm_priority = 0
        tm_penalty = 0.0
        chosen: Optional[sqlite3.Row] = None

        exact_candidates = list(by_norm.get(seg.normalized, []))
        # Prefer a safe/approved exact match. Only if no such candidate exists do we
        # surface a blocked exact candidate for diagnosis.
        safe_exact: list[tuple[float, int, float, sqlite3.Row]] = []
        blocked_exact: list[tuple[int, float, sqlite3.Row, str]] = []
        for cand in exact_candidates:
            name, priority, penalty = _candidate_profile(cand)
            sig = _row_signature(cand)
            safe_source, reason_source = signatures_compatible(sig, seg.signature)
            safe_bilingual, reason_bilingual = source_target_safe(cand["source"], cand["target"])
            if safe_source and safe_bilingual and cand["approved"]:
                safe_exact.append((100.0 - penalty, priority, float(cand["alignment_confidence"]), cand))
            else:
                blocked_exact.append((priority, float(cand["alignment_confidence"]), cand,
                                      reason_source or reason_bilingual or "entry non approvata"))

        if safe_exact:
            safe_exact.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
            effective, _, _, cand = safe_exact[0]
            chosen = cand
            tm_name, tm_priority, tm_penalty = _candidate_profile(cand)
            raw_score = 100.0
            score = round(max(0.0, effective), 1)
            same_context = (
                cand["prev_norm"] == seg.prev_normalized
                and cand["next_norm"] == seg.next_normalized
                and (seg.prev_normalized or seg.next_normalized)
            )
            if tm_penalty <= 0:
                category = "CONTEXT MATCH" if same_context else "100% MATCH"
                decision = "RIUSO AUTOMATICO CONSENTITO"
            else:
                category = "CONTEXT MATCH — PENALIZZATO" if same_context else "100% MATCH — PENALIZZATO"
                decision = "SUGGERIMENTO — REVISIONE OBBLIGATORIA"
            technical_safe = True

        elif blocked_exact:
            blocked_exact.sort(key=lambda x: (x[0], x[1]), reverse=True)
            _, _, cand, reason = blocked_exact[0]
            chosen = cand
            tm_name, tm_priority, tm_penalty = _candidate_profile(cand)
            raw_score = 100.0
            score = round(max(0.0, 100.0 - tm_penalty), 1)
            category = "TECHNICAL SAFE BLOCK"
            decision = "BLOCCATO — REVISIONE TECNICA"
            technical_safe = False
            technical_reason = reason

        if chosen is None and approved:
            ranked = []
            for cand in approved:
                name, priority, penalty = _candidate_profile(cand)
                raw = float(ratio(seg.normalized, cand["source_norm"]))
                effective = max(0.0, raw - penalty)
                ranked.append((effective, raw, priority, float(cand["alignment_confidence"]), cand))
            ranked.sort(key=lambda x: (x[0], x[2], x[3], x[1]), reverse=True)
            if ranked and ranked[0][0] >= fuzzy_threshold:
                effective, raw, _, _, cand = ranked[0]
                chosen = cand
                tm_name, tm_priority, tm_penalty = _candidate_profile(cand)
                sig = _row_signature(cand)
                safe_source, reason_source = signatures_compatible(sig, seg.signature)
                safe_bilingual, reason_bilingual = source_target_safe(cand["source"], cand["target"])
                raw_score = round(raw, 1)
                score = round(effective, 1)
                if safe_source and safe_bilingual:
                    category = "FUZZY MATCH"
                    decision = "SUGGERIMENTO — REVISIONE OBBLIGATORIA"
                    technical_safe = True
                else:
                    category = "TECHNICAL SAFE BLOCK"
                    decision = "BLOCCATO — REVISIONE TECNICA"
                    technical_safe = False
                    technical_reason = reason_source or reason_bilingual

        if chosen is not None:
            entry_id = int(chosen["id"])
            candidate_source = chosen["source"]
            candidate_target = chosen["target"]
            origin = chosen["origin"]
            alignment_confidence = float(chosen["alignment_confidence"])
            if not tm_name:
                tm_name, tm_priority, tm_penalty = _candidate_profile(chosen)

        rows.append({
            "segment_id": seg.segment_id,
            "page": seg.page,
            "source": seg.text,
            "match_category": category,
            "linguistic_score": score,
            "raw_linguistic_score": raw_score,
            "technical_safe": technical_safe,
            "technical_reason": technical_reason,
            "decision": decision,
            "pretranslation": candidate_target if decision == "RIUSO AUTOMATICO CONSENTITO" else "",
            "suggested_translation": candidate_target if chosen is not None else "",
            "tm_source": candidate_source,
            "tm_entry_id": entry_id,
            "tm_origin": origin,
            "tm_name": tm_name,
            "tm_priority": tm_priority,
            "tm_penalty": tm_penalty,
            "tm_alignment_confidence": round(alignment_confidence, 1),
            "numbers": " | ".join(seg.signature.numbers),
            "codes": " | ".join(seg.signature.codes),
            "units": " | ".join(seg.signature.units),
            "inline_tokens": " | ".join(inline_tokens(seg.text)),
            "prev_context": seg.prev_normalized,
            "next_context": seg.next_normalized,
        })
    return pd.DataFrame(rows)

def load_terminology(path: str | Path | None, source_lang: str, target_lang: str) -> dict[str, set[str]]:
    if not path:
        return {}
    path = Path(path)
    if path.suffix.casefold() == ".csv":
        df = pd.read_csv(path).fillna("")
        columns = {c.casefold(): c for c in df.columns}
        src_col = columns.get("source") or columns.get("sorgente") or list(df.columns)[0]
        tgt_col = columns.get("target") or columns.get("traduzione") or (list(df.columns)[1] if len(df.columns) > 1 else src_col)
        out: dict[str, set[str]] = {}
        for _, row in df.iterrows():
            src = str(row[src_col]).strip()
            tgt = str(row[tgt_col]).strip()
            if src and tgt:
                out.setdefault(src.casefold(), set()).add(tgt.casefold())
        return out
    if path.suffix.casefold() in {".tbx", ".xml"}:
        root = ET.parse(path).getroot()
        terms: dict[str, set[str]] = {}
        # Namespace-agnostic traversal.
        for entry in root.iter():
            if entry.tag.split("}")[-1] not in {"termEntry", "conceptEntry"}:
                continue
            lang_terms: dict[str, list[str]] = {}
            for langset in list(entry):
                if langset.tag.split("}")[-1] not in {"langSet", "langSec"}:
                    continue
                lang = langset.attrib.get("{http://www.w3.org/XML/1998/namespace}lang", "")
                vals = []
                for node in langset.iter():
                    if node.tag.split("}")[-1] == "term" and node.text:
                        vals.append(node.text.strip())
                if vals:
                    lang_terms[lang.casefold()] = vals
            srcs = _lang_terms_lookup(lang_terms, source_lang)
            tgts = _lang_terms_lookup(lang_terms, target_lang)
            for src in srcs:
                if src and tgts:
                    terms.setdefault(src.casefold(), set()).update(t.casefold() for t in tgts if t)
        return terms
    return {}


def _lang_terms_lookup(values: dict[str, list[str]], requested: str) -> list[str]:
    req = requested.casefold()
    if req in values:
        return values[req]
    base = req.split("-")[0]
    out = []
    for lang, vals in values.items():
        if lang.split("-")[0] == base:
            out.extend(vals)
    return out


def terminology_suggestions(source: str, terminology: dict[str, set[str]]) -> list[str]:
    source_cf = (source or "").casefold()
    suggestions = []
    for src_term, target_terms in terminology.items():
        # Word-boundary-ish matching for plain terms, literal matching for technical terms.
        if src_term in source_cf:
            suggestions.append(f"{src_term} → {', '.join(sorted(target_terms))}")
    return suggestions


def apply_terminology_qa(match_df: pd.DataFrame, terminology: dict[str, set[str]]) -> pd.DataFrame:
    if match_df.empty:
        return match_df
    out = match_df.copy()
    issues = []
    suggestions_col = []
    for _, row in out.iterrows():
        source_raw = str(row.get("source", ""))
        source = source_raw.casefold()
        target = str(row.get("pretranslation") or row.get("suggested_translation") or "").casefold()
        local = []
        suggestions = terminology_suggestions(source_raw, terminology)
        if target:
            for src_term, target_terms in terminology.items():
                if src_term in source and not any(t in target for t in target_terms):
                    local.append(f"{src_term} → atteso uno tra: {', '.join(sorted(target_terms))}")
        issues.append("; ".join(local))
        suggestions_col.append("; ".join(suggestions))
    out["terminology_qa"] = issues
    out["terminology_status"] = ["ISSUE" if x else "OK" for x in issues]
    out["term_suggestions"] = suggestions_col
    return out

def export_xliff(match_df: pd.DataFrame, out_path: str | Path, source_lang: str, target_lang: str) -> Path:
    out_path = Path(out_path)
    ns = "urn:oasis:names:tc:xliff:document:2.0"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}xliff", {"version": "2.0", "srcLang": source_lang, "trgLang": target_lang})
    file_el = ET.SubElement(root, f"{{{ns}}}file", {"id": "manualsync-workfile"})
    for _, row in match_df.iterrows():
        unit = ET.SubElement(file_el, f"{{{ns}}}unit", {"id": str(row["segment_id"])})
        notes = ET.SubElement(unit, f"{{{ns}}}notes")
        note = ET.SubElement(notes, f"{{{ns}}}note", {"category": "manualsync-match"})
        note.text = f"{row['match_category']} | {row['decision']} | {row['linguistic_score']}%"
        if row.get("technical_reason"):
            note2 = ET.SubElement(notes, f"{{{ns}}}note", {"category": "manualsync-technical-safe"})
            note2.text = str(row["technical_reason"])
        if row.get("tm_name"):
            note3 = ET.SubElement(notes, f"{{{ns}}}note", {"category": "manualsync-tm-profile"})
            note3.text = f"{row.get('tm_name')} | penalty={row.get('tm_penalty', 0)} | priority={row.get('tm_priority', 0)}"
        token_text = str(row.get("inline_tokens") or "")
        if token_text:
            note4 = ET.SubElement(notes, f"{{{ns}}}note", {"category": "manualsync-inline-tokens"})
            note4.text = token_text
        term_text = str(row.get("term_suggestions") or "")
        if term_text:
            note5 = ET.SubElement(notes, f"{{{ns}}}note", {"category": "manualsync-terminology-suggestions"})
            note5.text = term_text
        seg_attrs = {"state": "translated" if row.get("pretranslation") else "initial"}
        segment = ET.SubElement(unit, f"{{{ns}}}segment", seg_attrs)
        src = ET.SubElement(segment, f"{{{ns}}}source")
        src.text = str(row["source"])
        tgt = ET.SubElement(segment, f"{{{ns}}}target")
        tgt.text = str(row.get("pretranslation") or "")
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    return out_path


def create_srx_profile(out_path: str | Path, source_lang: str) -> Path:
    out_path = Path(out_path)
    ns = "http://www.lisa.org/srx20"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}srx", {"version": "2.0"})
    header = ET.SubElement(root, f"{{{ns}}}header", {"segmentsubflows": "yes", "cascade": "yes"})
    body = ET.SubElement(root, f"{{{ns}}}body")
    rules = ET.SubElement(body, f"{{{ns}}}languagerules")
    langrule = ET.SubElement(rules, f"{{{ns}}}languagerule", {"languagerulename": "ManualSyncDefault"})
    # Keep decimals together.
    rule = ET.SubElement(langrule, f"{{{ns}}}rule", {"break": "no"})
    ET.SubElement(rule, f"{{{ns}}}beforebreak").text = r"\d\."
    ET.SubElement(rule, f"{{{ns}}}afterbreak").text = r"\d"
    # Standard sentence break.
    rule = ET.SubElement(langrule, f"{{{ns}}}rule", {"break": "yes"})
    ET.SubElement(rule, f"{{{ns}}}beforebreak").text = r"[.!?]"
    ET.SubElement(rule, f"{{{ns}}}afterbreak").text = r"\s+[A-ZÀ-ÖØ-Þ0-9]"
    maprules = ET.SubElement(body, f"{{{ns}}}maprules")
    ET.SubElement(maprules, f"{{{ns}}}languagemap", {"languagepattern": source_lang.split("-")[0] + ".*", "languagerulename": "ManualSyncDefault"})
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    return out_path


def build_cat_report(match_df: pd.DataFrame, tm_count: int, out_path: str | Path, source_lang: str, target_lang: str) -> tuple[Path, dict]:
    out_path = Path(out_path)
    total = len(match_df)
    counts = match_df["match_category"].value_counts().to_dict() if total else {}
    auto = int(match_df["decision"].eq("RIUSO AUTOMATICO CONSENTITO").sum()) if total else 0
    blocked = int(match_df["decision"].eq("BLOCCATO — REVISIONE TECNICA").sum()) if total else 0
    fuzzy = int(match_df["match_category"].eq("FUZZY MATCH").sum()) if total else 0
    new = int(match_df["match_category"].eq("NEW").sum()) if total else 0
    words = int(match_df["source"].astype(str).str.split().str.len().sum()) if total else 0
    auto_words = int(match_df.loc[match_df["decision"].eq("RIUSO AUTOMATICO CONSENTITO"), "source"].astype(str).str.split().str.len().sum()) if total else 0
    summary = {
        "build_cat": CAT_BUILD,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "tm_entries": tm_count,
        "segments": total,
        "context_matches": int(counts.get("CONTEXT MATCH", 0)),
        "exact_100_matches": int(counts.get("100% MATCH", 0)),
        "fuzzy_matches": fuzzy,
        "technical_safe_blocks": int(counts.get("TECHNICAL SAFE BLOCK", 0)),
        "new_segments": new,
        "automatic_reuses": auto,
        "automatic_reuse_segment_pct": round(auto / total * 100, 1) if total else 0.0,
        "automatic_reuse_word_pct": round(auto_words / words * 100, 1) if words else 0.0,
        "source_words": words,
        "auto_reused_words": auto_words,
    }
    cards = "".join(f"<div class='card'><b>{html.escape(k.replace('_',' ').title())}</b><br>{html.escape(str(v))}</div>" for k, v in summary.items())
    visible_cols = [c for c in ["segment_id","page","source","match_category","raw_linguistic_score","linguistic_score","tm_name","tm_penalty","technical_safe","decision","pretranslation","suggested_translation","term_suggestions","inline_tokens","technical_reason","terminology_status","terminology_qa"] if c in match_df.columns]
    table = match_df[visible_cols].to_html(index=False, escape=True) if total else "<p>Nessun segmento.</p>"
    out_path.write_text(f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><title>ManualSync CAT/TM</title>
<style>body{{font-family:Arial,sans-serif;margin:0;background:#f8fafc;color:#0f172a}}header{{background:#0f172a;color:white;padding:28px}}main{{padding:26px}}.cards{{display:flex;flex-wrap:wrap;gap:10px}}.card{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:12px;min-width:170px}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white;margin-top:20px}}th,td{{border:1px solid #cbd5e1;padding:7px;vertical-align:top}}th{{background:#0f766e;color:white;position:sticky;top:0}}</style></head><body><header><h1>ManualSync v0.14 — CAT Pro</h1><p>Multi-TM + Context/100%/Fuzzy + Technical Safe Match + terminologia + integrità placeholder</p></header><main><div class='cards'>{cards}</div>{table}</main></body></html>""", encoding="utf-8")
    return out_path, summary


def run_cat_pipeline(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path,
                     out_dir: str | Path, source_lang: str = "en-US", target_lang: str = "it-IT",
                     old_pages: str = "", translation_pages: str = "", new_pages: str = "",
                     import_tmx_path: str | Path | None = None, terminology_path: str | Path | None = None,
                     fuzzy_threshold: float = 75.0, external_tms: list[dict] | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tm_path = out_dir / "ManualSync_TM.sqlite"
    if tm_path.exists():
        tm_path.unlink()
    tm = TranslationMemory(tm_path)
    try:
        legacy_df = build_tm_from_documents(
            tm, old_source_pdf, old_translation_pdf, source_lang, target_lang,
            old_pages=old_pages, translation_pages=translation_pages,
            origin="legacy bilingual documents", tm_name="Project Legacy", priority=100, penalty=0,
        )
        import_results: list[dict] = []
        if import_tmx_path:
            import_results.append(tm.import_tmx(
                import_tmx_path, source_lang, target_lang,
                origin="TMX import", tm_name=Path(import_tmx_path).stem, priority=85, penalty=5.0,
            ))
        for i, profile in enumerate(external_tms or []):
            path = profile.get("path")
            if not path:
                continue
            import_results.append(tm.import_tmx(
                path, source_lang, target_lang,
                origin=str(profile.get("origin") or "External TMX"),
                tm_name=str(profile.get("name") or Path(path).stem),
                priority=int(profile.get("priority", max(10, 80 - i * 10))),
                penalty=float(profile.get("penalty", i * 5.0)),
            ))

        segments = segments_from_layout(new_source_pdf, new_pages)
        match_df = match_segments(tm, segments, source_lang, target_lang, fuzzy_threshold=fuzzy_threshold)
        terminology = load_terminology(terminology_path, source_lang, target_lang)
        match_df = apply_terminology_qa(match_df, terminology)

        match_csv = out_dir / "CAT_MATCHES.csv"
        legacy_csv = out_dir / "TM_LEGACY_IMPORT_AUDIT.csv"
        match_df.to_csv(match_csv, index=False, encoding="utf-8-sig")
        legacy_df.to_csv(legacy_csv, index=False, encoding="utf-8-sig")
        tmx_path = tm.export_tmx(out_dir / "ManualSync_TM.tmx", source_lang, target_lang)
        xliff_path = export_xliff(match_df, out_dir / "ManualSync_WORKFILE.xlf", source_lang, target_lang)
        srx_path = create_srx_profile(out_dir / "ManualSync_SEGMENTATION.srx", source_lang)
        report_path, summary = build_cat_report(match_df, tm.count(), out_dir / "ManualSync_CAT_REPORT.html", source_lang, target_lang)
        summary["tmx_imports"] = import_results
        summary["tm_profiles"] = tm.profiles()
        summary["inline_token_segments"] = int(match_df["inline_tokens"].astype(str).ne("").sum()) if "inline_tokens" in match_df else 0
        summary["terminology_suggestion_segments"] = int(match_df["term_suggestions"].astype(str).ne("").sum()) if "term_suggestions" in match_df else 0
        (out_dir / "CAT_SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "matches": match_df,
            "legacy_audit": legacy_df,
            "summary": summary,
            "tm_sqlite": tm_path,
            "tmx": tmx_path,
            "xliff": xliff_path,
            "srx": srx_path,
            "report": report_path,
            "matches_csv": match_csv,
            "legacy_csv": legacy_csv,
        }
    finally:
        tm.close()

