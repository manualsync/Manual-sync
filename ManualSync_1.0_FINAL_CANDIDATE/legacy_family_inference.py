"""Isolated Milestone 3C engine: legacy multi-document family/revision/language
inference (folder / multi-file legacy onboarding).

Scope discipline:
- This module NEVER writes into a project's CCMS. It only proposes structure
  in memory (ephemeral) and, on explicit human confirmation, persists a
  lightweight cross-project lineage record to a plain JSON file per family
  (no graph database, no new runtime).
- It reuses existing, already-validated building blocks read-only:
  layout_engine.extract_layout / detect_document_language / detect_docx_language,
  legacy_doc_conversion.convert_legacy_doc_to_docx,
  authoring_ccms._looks_like_legacy_docx_heading (pure heading classifier).
  None of those modules are modified by this milestone.
- Evidence is always structured (type/role/value), never a raw numeric score
  surfaced to a user. A single strong signal is never sufficient on its own to
  create a *specific* relation (revision_of / language_variant_of) -- see
  classify_pair()/relation eligibility below for the exact AND-conditions.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from docx import Document as DocxDocument
from rapidfuzz.fuzz import ratio

from layout_engine import extract_layout, detect_document_language, detect_docx_language
from authoring_ccms import _looks_like_legacy_docx_heading
from legacy_doc_conversion import convert_legacy_doc_to_docx, LegacyDocConversionError
from idml_import import extract_idml_signals, detect_idml_language, IdmlImportError
from project_lifecycle import _atomic_write_json

SCHEMA_VERSION = 1
FAMILIES_DIRNAME = "LegacyFamilies"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".idml"}

EvidenceRole = Literal["strong", "weak", "conflict"]
RelationType = Literal["revision_of", "language_variant_of"]


class UnsupportedDocumentError(Exception):
    """Raised for a file extension outside SUPPORTED_SUFFIXES."""


# ---------------------------------------------------------------------------
# Structured evidence
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    type: str  # manual_code_match | title_match | revision_match | language_match | language_conflict | structure_match | metadata_match | duplicate_content
    role: EvidenceRole
    value: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "role": self.role, "value": self.value, "detail": self.detail}


# ---------------------------------------------------------------------------
# Revision normalization -- typed, conservative, never invents an order.
# ---------------------------------------------------------------------------

@dataclass
class RevisionSignal:
    scheme: str  # rev_numeric | rev_padded_numeric | dotted_version | letter | edition | date_full | date_month | unrecognized
    original: str | None
    normalized: Any  # int | tuple | None

# NOTE: boundaries are (?:^|[_\-.\s]) / (?=[_\-.\s]|$) rather than \b -- legacy
# filenames separate tokens with '_', which regex treats as a WORD character,
# so a bare \b immediately after/before '_' never matches (e.g. neither side
# of "Rev1" in "MachineA_Rev1_final" would match with plain \b). The trailing
# lookahead does not consume characters, so m.group(0) stays clean without
# needing _clean_match_text() on that side; the leading side is a consumed
# non-capturing prefix, cleaned up separately (see _clean_match_text).
_REV_NUMERIC_RE = re.compile(r"(?i)(?:^|[_\-.\s])Rev(?:ision)?\.?\s*0*([1-9][0-9]{0,2})(?=[_\-.\s]|$)")
_DOTTED_VERSION_RE = re.compile(r"(?i)(?:^|[_\-.\sv])([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?=[_\-.\s]|$)")
_REV_LETTER_RE = re.compile(r"(?i)(?:^|[_\-.\s])Rev(?:ision)?\.?\s*([A-Za-z])(?=[_\-.\s]|$)")
_EDITION_RE = re.compile(r"(?i)(?:^|[_\-.\s])Ed(?:ition)?\.?\s*0*([1-9][0-9]{0,2})(?=[_\-.\s]|$)")
_DATE_FULL_RE = re.compile(r"(?:^|[_\-.\s])(20\d{2})[-_](0[1-9]|1[0-2])[-_](0[1-9]|[12]\d|3[01])(?=[_\-.\s]|$)")
_DATE_MONTH_RE = re.compile(r"(?:^|[_\-.\s])(20\d{2})[-_](0[1-9]|1[0-2])(?=[_\-.\s]|$)")
# Filename-only, unlabeled fallbacks -- weaker evidence, only tried on the
# filename stem (never on arbitrary body/title text, far too ambiguous there).
_REV_PADDED_FILENAME_RE = re.compile(r"(?:^|[_\-])([0-9]{2})(?:[_\-.]|$)")
_REV_LETTER_FILENAME_RE = re.compile(r"(?:^|[_\-])([A-Za-z])(?:[_\-.]|$)")


def _clean_match_text(raw: str) -> str:
    return raw.lstrip("_-. \tVv")


def _labeled_revision(text: str) -> RevisionSignal | None:
    m = _REV_NUMERIC_RE.search(text)
    if m:
        return RevisionSignal("rev_numeric", _clean_match_text(m.group(0)), int(m.group(1)))
    m = _DOTTED_VERSION_RE.search(text)
    if m:
        parts = tuple(int(g) for g in m.groups() if g is not None)
        return RevisionSignal("dotted_version", _clean_match_text(m.group(0)), parts)
    m = _REV_LETTER_RE.search(text)
    if m:
        letter = m.group(1).upper()
        return RevisionSignal("letter", _clean_match_text(m.group(0)), ord(letter) - ord("A") + 1)
    m = _EDITION_RE.search(text)
    if m:
        return RevisionSignal("edition", _clean_match_text(m.group(0)), int(m.group(1)))
    m = _DATE_FULL_RE.search(text)
    if m:
        y, mo, d = m.groups()
        return RevisionSignal("date_full", _clean_match_text(m.group(0)), (int(y), int(mo), int(d)))
    m = _DATE_MONTH_RE.search(text)
    if m:
        y, mo = m.groups()
        return RevisionSignal("date_month", _clean_match_text(m.group(0)), (int(y), int(mo)))
    return None


def extract_revision(filename: str, title_line: str = "") -> RevisionSignal:
    """Filename takes precedence (legacy manuals almost always encode revision
    there); explicit/labeled patterns always outrank bare filename fallbacks;
    body/title text is only trusted for explicitly labeled patterns.
    """
    stem = Path(filename).stem
    sig = _labeled_revision(stem)
    if sig:
        return sig
    if title_line:
        sig = _labeled_revision(title_line)
        if sig:
            return sig
    m = _REV_PADDED_FILENAME_RE.search(stem)
    if m:
        return RevisionSignal("rev_padded_numeric", m.group(1), int(m.group(1)))
    m = _REV_LETTER_FILENAME_RE.search(stem)
    if m:
        letter = m.group(1).upper()
        return RevisionSignal("letter_filename", m.group(1), ord(letter) - ord("A") + 1)
    return RevisionSignal("unrecognized", None, None)


def revision_distinct(a: RevisionSignal, b: RevisionSignal) -> bool:
    """Both recognized AND not identical -- the minimum bar for 'these are
    distinct revisions', regardless of whether an order can be derived."""
    if a.scheme == "unrecognized" or b.scheme == "unrecognized":
        return False
    return not (a.scheme == b.scheme and a.normalized == b.normalized)


def revision_same(a: RevisionSignal, b: RevisionSignal) -> bool:
    if a.scheme == "unrecognized" or b.scheme == "unrecognized":
        return False
    return a.scheme == b.scheme and a.normalized == b.normalized


_ORDERABLE_SCHEMES = {"rev_numeric", "rev_padded_numeric", "letter", "letter_filename", "edition", "date_full", "date_month"}


def revision_order(a: RevisionSignal, b: RevisionSignal) -> str | None:
    """Returns 'a', 'b' (which one is newer) or None when order cannot be
    derived unambiguously. Cross-scheme comparison NEVER orders automatically,
    even when both are individually recognized and distinct."""
    if a.scheme != b.scheme or a.scheme not in _ORDERABLE_SCHEMES:
        return None
    if a.normalized == b.normalized:
        return None
    if a.scheme == "dotted_version":
        return None  # dotted_version excluded from _ORDERABLE_SCHEMES on purpose; kept here as a safety no-op
    return "b" if b.normalized > a.normalized else "a"


def dotted_version_order(a: RevisionSignal, b: RevisionSignal) -> str | None:
    if a.scheme != "dotted_version" or b.scheme != "dotted_version":
        return None
    if len(a.normalized) != len(b.normalized) or a.normalized == b.normalized:
        return None
    return "b" if b.normalized > a.normalized else "a"


# ---------------------------------------------------------------------------
# Manual/product code extraction -- conservative, rejects on ambiguity.
# ---------------------------------------------------------------------------

_LANG_STRIP_RE = re.compile(r"(?i)[_\-.](en|it|de|fr|es)(?=[_\-.]|$)")
_STRIP_PATTERNS = [_REV_NUMERIC_RE, _DOTTED_VERSION_RE, _REV_LETTER_RE, _EDITION_RE, _DATE_FULL_RE, _DATE_MONTH_RE]


def extract_manual_code_from_filename(filename: str) -> str | None:
    stem = Path(filename).stem
    stripped = _LANG_STRIP_RE.sub("_", stem)
    for pat in _STRIP_PATTERNS:
        stripped = pat.sub("_", stripped)
    stripped = re.sub(r"[_\-.]+", " ", stripped).strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9]{1,29})\b", stripped)
    if not m:
        return None
    code = m.group(1)
    return None if code.isdigit() else code


def extract_manual_code_from_title(content_hint_line: str) -> str | None:
    """`content_hint_line` must be a genuine free-text line -- NOT a numbered
    chapter heading ("1 - Introduzione ...") -- see _content_hint_line().
    Guessing a code out of a chapter heading's words is unreliable (the code,
    if any, could be first, last, or absent entirely), so this deliberately
    only trusts a plain leading word, consistent with the conservative
    reject-on-ambiguity style used elsewhere (e.g. 3B.1 heading inference).
    """
    if not content_hint_line:
        return None
    m = re.match(r"^([A-Za-z][A-Za-z0-9\-]{2,29})\b", content_hint_line.strip())
    if not m:
        return None
    code = m.group(1)
    return None if code.isdigit() else code


# ---------------------------------------------------------------------------
# Per-candidate signal extraction (PDF / DOCX / legacy .doc)
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    path: str
    filename_original: str
    file_sha256: str
    file_size: int
    doc_kind: str  # pdf | docx | doc
    language_detected: str
    language_confidence: float
    title_line: str
    manual_code_filename: str | None
    manual_code_content: str | None
    headings: list[str]
    section_count: int
    revision: RevisionSignal
    metadata_title: str
    duplicate_of_paths: list[str] = field(default_factory=list)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_asset_id(sha256_hex: str) -> str:
    """Short, readable identifier derived from the hash. The full sha256_hex
    -- always preserved alongside it -- is the canonical content identity;
    any dedup/equality check MUST compare full hashes, never this prefix."""
    return f"ASSET-{sha256_hex[:16]}"


def _first_meaningful_line(lines: list[str]) -> str:
    for t in lines:
        t = re.sub(r"\s+", " ", t or "").strip()
        if len(t) >= 3:
            return t
    return ""


def _content_hint_line(all_lines: list[str], headings: list[str]) -> str:
    """First genuine free-text line (title-page style, e.g. 'LineControl
    v3.3.04') that is NOT itself a numbered chapter heading. Chapter headings
    ("1 - Introduzione ...") are structural markers, not product identity --
    guessing a manual/product code out of one is unreliable, so manual_code
    extraction from content is deliberately scoped to this line only, never
    to a heading."""
    heading_set = set(headings)
    for t in all_lines:
        if t not in heading_set:
            clean = re.sub(r"\s+", " ", t or "").strip()
            if len(clean) >= 3:
                return clean
    return ""


def _pdf_signals(path: Path) -> tuple[str, list[str], str, str]:
    """Returns (title_line, headings, metadata_title, content_hint_line)
    using only the already public/stable extract_layout() -- no new PDF
    parsing added here."""
    import fitz
    layout = extract_layout(path)
    headings = [tb.text.strip() for tb in layout.text_blocks if tb.heading and tb.text.strip()]
    all_lines = [tb.text for tb in sorted(layout.text_blocks, key=lambda t: (t.page, t.index))]
    content_hint_line = _content_hint_line(all_lines, headings)
    # Front-matter/product text (content_hint_line) is a far more distinctive
    # title-similarity signal than a generic chapter-1 heading -- almost every
    # manual has a heading literally called "Introduzione"/"Introduction",
    # which would otherwise make title_match fire between unrelated documents.
    title_line = content_hint_line or _first_meaningful_line(headings)
    metadata_title = ""
    try:
        doc = fitz.open(str(path))
        metadata_title = str((doc.metadata or {}).get("title") or "").strip()
        doc.close()
    except Exception:
        pass
    return title_line, headings, metadata_title, content_hint_line


def _docx_headings_and_title(doc: DocxDocument) -> tuple[str, list[str], str]:
    headings: list[str] = []
    all_lines: list[str] = []
    for p in doc.paragraphs:
        text = re.sub(r"\s+", " ", p.text or "").strip()
        if not text:
            continue
        all_lines.append(text)
        style = p.style.name if p.style else ""
        if style.startswith("Heading") or _looks_like_legacy_docx_heading(text):
            headings.append(text)
    content_hint_line = _content_hint_line(all_lines, headings)
    title_line = content_hint_line or _first_meaningful_line(headings)
    return title_line, headings, content_hint_line


def _docx_signals(path: Path) -> tuple[str, list[str], str, str]:
    doc = DocxDocument(str(path))
    title_line, headings, content_hint_line = _docx_headings_and_title(doc)
    metadata_title = ""
    try:
        metadata_title = str(doc.core_properties.title or "").strip()
    except Exception:
        pass
    return title_line, headings, metadata_title, content_hint_line


def _idml_signals(path: Path) -> tuple[str, list[str], str, str]:
    """Same shape/contract as _pdf_signals/_docx_signals: (title_line,
    headings, metadata_title, content_hint_line). idml_import.py owns the
    ENTIRE fail-closed IDML parse (security validation, threading-aware
    reading order); this only reuses its raw ingredients through the same
    generic _content_hint_line/_first_meaningful_line helpers already used by
    every other format -- no new heading/title heuristic engine."""
    headings, all_lines, metadata_title = extract_idml_signals(path)
    content_hint_line = _content_hint_line(all_lines, headings)
    title_line = content_hint_line or _first_meaningful_line(headings)
    return title_line, headings, metadata_title, content_hint_line


def build_candidate(path: str | Path) -> Candidate:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedDocumentError(f"Formato non supportato: {path.name}")

    file_sha256 = sha256_file(path)
    file_size = path.stat().st_size

    if suffix == ".pdf":
        title_line, headings, metadata_title, content_hint_line = _pdf_signals(path)
        lang, conf, _basis = detect_document_language(path)
        doc_kind = "pdf"
    elif suffix == ".docx":
        title_line, headings, metadata_title, content_hint_line = _docx_signals(path)
        lang, conf, _basis = detect_docx_language(path)
        doc_kind = "docx"
    elif suffix == ".doc":  # reuse the 3B.1 isolated converter; never a second parser
        try:
            with convert_legacy_doc_to_docx(path) as temp_docx:
                title_line, headings, metadata_title, content_hint_line = _docx_signals(temp_docx)
                lang, conf, _basis = detect_docx_language(temp_docx)
        except LegacyDocConversionError as exc:
            raise UnsupportedDocumentError(
                f"Impossibile leggere {path.name}: documento Word legacy non convertibile."
            ) from exc
        doc_kind = "doc"
    else:  # .idml -- Milestone 3D, reuse idml_import's fail-closed parser; never a second engine
        try:
            title_line, headings, metadata_title, content_hint_line = _idml_signals(path)
            lang, conf, _basis = detect_idml_language(path)
        except IdmlImportError as exc:
            raise UnsupportedDocumentError(
                f"Impossibile leggere {path.name}: pacchetto IDML non valido o non supportato."
            ) from exc
        doc_kind = "idml"

    manual_code_filename = extract_manual_code_from_filename(path.name)
    manual_code_content = extract_manual_code_from_title(content_hint_line)

    revision = extract_revision(path.name, content_hint_line)

    return Candidate(
        path=str(path), filename_original=path.name, file_sha256=file_sha256, file_size=file_size,
        doc_kind=doc_kind, language_detected=lang, language_confidence=conf, title_line=title_line,
        manual_code_filename=manual_code_filename, manual_code_content=manual_code_content, headings=headings,
        section_count=len(headings), revision=revision, metadata_title=metadata_title,
    )


def dedupe_candidates(candidates: list[Candidate]) -> tuple[list[Candidate], list[dict]]:
    """Groups byte-identical files (full sha256 match) into one candidate.
    Returns (deduped, duplicate_groups) -- duplicate_groups records which
    filenames/paths collapsed into which surviving candidate (scenario G)."""
    by_hash: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_hash.setdefault(c.file_sha256, []).append(c)
    deduped: list[Candidate] = []
    duplicate_groups: list[dict] = []
    for file_hash, group in by_hash.items():
        primary = group[0]
        if len(group) > 1:
            primary.duplicate_of_paths = [g.path for g in group[1:]]
            duplicate_groups.append({
                "file_sha256": file_hash,
                "kept_path": primary.path,
                "duplicate_paths": [g.path for g in group[1:]],
            })
        deduped.append(primary)
    return deduped, duplicate_groups


# ---------------------------------------------------------------------------
# Pairwise classification -- strong/weak/conflict evidence, no numeric score.
# ---------------------------------------------------------------------------

_TITLE_MATCH_THRESHOLD = 92.0  # internal only; never shown to the user
_STRUCTURE_COVERAGE_THRESHOLD = 0.6
_LANGUAGE_CONFIDENCE_FLOOR = 0.70  # same floor already used for guided-onboarding language confirmation
LANGUAGE_CONFIDENCE_FLOOR = _LANGUAGE_CONFIDENCE_FLOOR  # public alias for UI-layer gating (app.py)


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def _title_match(a: Candidate, b: Candidate) -> bool:
    ta, tb = _normalize_for_match(a.title_line), _normalize_for_match(b.title_line)
    if len(ta) < 4 or len(tb) < 4:
        return False
    return ratio(ta, tb) >= _TITLE_MATCH_THRESHOLD


def _structure_match(a: Candidate, b: Candidate) -> bool:
    if a.section_count < 2 or b.section_count < 2:
        return False
    norm_a = [_normalize_for_match(h) for h in a.headings]
    norm_b = [_normalize_for_match(h) for h in b.headings]
    common = sum(1 for h in norm_a if any(ratio(h, other) >= 85.0 for other in norm_b))
    coverage = common / max(len(norm_a), len(norm_b))
    return coverage >= _STRUCTURE_COVERAGE_THRESHOLD


@dataclass
class PairAssessment:
    evidence: list[Evidence]
    family_edge: bool  # non-conflicting strong positive -> eligible to cluster
    conflict: bool      # a strong signal contradicts another -> forces ambiguous
    revision_of_eligible: bool
    revision_of_order: str | None  # 'a' | 'b' | None (order still needs confirmation)
    language_variant_eligible: bool


def classify_pair(a: Candidate, b: Candidate) -> PairAssessment:
    evidence: list[Evidence] = []
    strong_positive = False
    conflict = False

    # A low-confidence detection on either side must never feed a relation
    # precondition -- same_language/different_language both stay False
    # (neither confirmed) rather than risk a wrong revision_of/language_variant_of.
    lang_reliable = a.language_confidence >= _LANGUAGE_CONFIDENCE_FLOOR and b.language_confidence >= _LANGUAGE_CONFIDENCE_FLOOR
    both_lang_known = lang_reliable and a.language_detected != "unknown" and b.language_detected != "unknown"
    same_language = both_lang_known and a.language_detected == b.language_detected
    different_language = both_lang_known and a.language_detected != b.language_detected

    # manual_code_match is evaluated on filename- and content-derived codes
    # SEPARATELY: a bare mismatch between two otherwise-unrelated documents is
    # not a conflict (most pairs in a folder will simply have different codes
    # and nothing else in common -- that must resolve to "unrelated" quietly,
    # not flood the user with false ambiguity). It only becomes a genuine
    # conflict (scenario E) when the filename suggests kinship but the more
    # reliable content-derived code contradicts it.
    fn_a, fn_b = (a.manual_code_filename or "").casefold(), (b.manual_code_filename or "").casefold()
    ct_a, ct_b = (a.manual_code_content or "").casefold(), (b.manual_code_content or "").casefold()
    filename_suggests_related = bool(fn_a) and bool(fn_b) and fn_a == fn_b
    content_available = bool(ct_a) and bool(ct_b)
    content_agrees = content_available and ct_a == ct_b
    content_disagrees = content_available and ct_a != ct_b

    if content_agrees:
        evidence.append(Evidence("manual_code_match", "strong", ct_a, "content"))
        strong_positive = True
    elif filename_suggests_related and content_disagrees:
        evidence.append(Evidence("manual_code_match", "conflict", f"filename:{fn_a} content:{ct_a}!={ct_b}"))
        conflict = True
    elif filename_suggests_related:
        evidence.append(Evidence("manual_code_match", "strong", fn_a, "filename"))
        strong_positive = True

    if _title_match(a, b):
        evidence.append(Evidence("title_match", "strong", a.title_line[:80]))
        strong_positive = True

    if _structure_match(a, b):
        evidence.append(Evidence("structure_match", "strong", f"{a.section_count} vs {b.section_count} sezioni"))
        strong_positive = True

    if same_language:
        evidence.append(Evidence("language_match", "weak", a.language_detected))

    rev_distinct = revision_distinct(a.revision, b.revision)
    rev_same = revision_same(a.revision, b.revision)
    if rev_distinct:
        evidence.append(Evidence("revision_match", "strong", f"{a.revision.original} != {b.revision.original}", "distinct"))
    elif rev_same:
        evidence.append(Evidence("revision_match", "strong", f"{a.revision.original} == {b.revision.original}", "same"))

    if a.metadata_title and b.metadata_title:
        if _normalize_for_match(a.metadata_title) == _normalize_for_match(b.metadata_title):
            evidence.append(Evidence("metadata_match", "weak", a.metadata_title[:80]))

    # NOTE on language_conflict: deliberately NOT auto-fired for "different
    # language + distinct revision" alone -- e.g. an EN Rev.2 vs an IT Rev.1
    # within the same family is not a contradiction, it simply has no direct
    # specific relation to each other (they relate transitively through the
    # IT Rev.2 member) and correctly falls into pending_pairs below. Reserved
    # for a genuine future case where signals actively contradict each other,
    # not merely "no relation applies to this particular pair".

    family_edge = strong_positive and not conflict

    revision_of_eligible = family_edge and same_language and rev_distinct
    order = None
    if revision_of_eligible:
        order = revision_order(a.revision, b.revision) or dotted_version_order(a.revision, b.revision)

    language_variant_eligible = family_edge and different_language and rev_same

    return PairAssessment(
        evidence=evidence, family_edge=family_edge, conflict=conflict,
        revision_of_eligible=revision_of_eligible, revision_of_order=order,
        language_variant_eligible=language_variant_eligible,
    )


# ---------------------------------------------------------------------------
# Clustering (family membership) -- connected components over non-conflicting
# strong edges. A conflict edge vetoes merging that specific pair even if
# otherwise transitively reachable.
# ---------------------------------------------------------------------------

@dataclass
class FamilyProposal:
    members: list[Candidate]
    relations: list[dict]        # auto-eligible, specific relations (not yet confirmed)
    pending_pairs: list[dict]    # same family, no auto-eligible specific relation -> needs explicit human choice


@dataclass
class ProposalResult:
    families: list[FamilyProposal]
    ambiguous_pairs: list[dict]  # conflict-vetoed pairs, shown for explicit human review
    unrelated: list[Candidate]   # no family edge to anything
    duplicate_groups: list[dict]
    ignored: list[dict]          # {"path":..., "reason":...} for unsupported files
    all_candidates: list[Candidate] = field(default_factory=list)  # every deduped candidate, regardless of
    # cluster outcome -- lets a UI resolve full Candidate details (filename, revision, ...) for an
    # ambiguous_pairs entry (which only carries hashes/paths) when the human manually corrects it.


def cluster_candidates(candidates: list[Candidate]) -> tuple[list[FamilyProposal], list[dict], list[Candidate]]:
    n = len(candidates)
    assessments: dict[tuple[int, int], PairAssessment] = {}
    for i in range(n):
        for j in range(i + 1, n):
            assessments[(i, j)] = classify_pair(candidates[i], candidates[j])

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    ambiguous_pairs: list[dict] = []
    conflicted_indices: set[int] = set()
    for (i, j), a in assessments.items():
        if a.conflict:
            ambiguous_pairs.append({
                "asset_a": candidates[i].file_sha256, "asset_b": candidates[j].file_sha256,
                "path_a": candidates[i].path, "path_b": candidates[j].path,
                "evidence": [e.to_dict() for e in a.evidence],
            })
            conflicted_indices.add(i); conflicted_indices.add(j)
        elif a.family_edge:
            union(i, j)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)

    families: list[FamilyProposal] = []
    unrelated: list[Candidate] = []
    for members_idx in groups.values():
        if len(members_idx) == 1:
            # A candidate still awaiting a human decision on a conflicting pair
            # is "to verify", never "independent" -- those are two different,
            # mutually exclusive states. Excluding it here (not just in the UI)
            # keeps every consumer of ProposalResult seeing one consistent
            # classification instead of the same asset in two lists at once.
            if members_idx[0] not in conflicted_indices:
                unrelated.append(candidates[members_idx[0]])
            continue
        relations: list[dict] = []
        pending_pairs: list[dict] = []
        for a_pos in range(len(members_idx)):
            for b_pos in range(a_pos + 1, len(members_idx)):
                i, j = members_idx[a_pos], members_idx[b_pos]
                key = (i, j) if i < j else (j, i)
                assess = assessments[key]
                ca, cb = candidates[key[0]], candidates[key[1]]
                if assess.revision_of_eligible:
                    order = assess.revision_of_order
                    newer = None
                    if order == "a":
                        newer = ca.file_sha256
                    elif order == "b":
                        newer = cb.file_sha256
                    relations.append({
                        "relation_type": "revision_of",
                        "asset_a": ca.file_sha256, "asset_b": cb.file_sha256,
                        "order_confirmed": newer is not None,
                        "newer_asset_hint": newer,
                        "evidence": [e.to_dict() for e in assess.evidence],
                    })
                elif assess.language_variant_eligible:
                    relations.append({
                        "relation_type": "language_variant_of",
                        "asset_a": ca.file_sha256, "asset_b": cb.file_sha256,
                        "order_confirmed": False, "newer_asset_hint": None,
                        "evidence": [e.to_dict() for e in assess.evidence],
                    })
                elif assess.family_edge:
                    pending_pairs.append({
                        "asset_a": ca.file_sha256, "asset_b": cb.file_sha256,
                        "evidence": [e.to_dict() for e in assess.evidence],
                    })
        families.append(FamilyProposal(
            members=[candidates[i] for i in members_idx], relations=relations, pending_pairs=pending_pairs,
        ))

    return families, ambiguous_pairs, unrelated


def analyze_paths(paths: list[str | Path]) -> ProposalResult:
    """Entry point for the guided UI. Fully ephemeral -- nothing is written
    to disk. Unsupported files are filtered out with a reason, never raise."""
    candidates: list[Candidate] = []
    ignored: list[dict] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            continue
        if p.suffix.lower() not in SUPPORTED_SUFFIXES:
            ignored.append({"path": str(p), "reason": f"Formato '{p.suffix}' non supportato (solo PDF, DOCX, DOC)."})
            continue
        try:
            candidates.append(build_candidate(p))
        except UnsupportedDocumentError as exc:
            ignored.append({"path": str(p), "reason": str(exc)})

    deduped, duplicate_groups = dedupe_candidates(candidates)
    families, ambiguous_pairs, unrelated = cluster_candidates(deduped)
    return ProposalResult(
        families=families, ambiguous_pairs=ambiguous_pairs, unrelated=unrelated,
        duplicate_groups=duplicate_groups, ignored=ignored, all_candidates=deduped,
    )


# ---------------------------------------------------------------------------
# Persistence -- LegacyFamilies/<family_id>.json. Only CONFIRMED state lives
# here. Proposals above are never written to disk.
# ---------------------------------------------------------------------------

def families_dir(data_home: str | Path) -> Path:
    return Path(data_home) / FAMILIES_DIRNAME


def family_json_path(data_home: str | Path, family_id: str) -> Path:
    return families_dir(data_home) / f"{family_id}.json"


def load_family(data_home: str | Path, family_id: str) -> dict | None:
    p = family_json_path(data_home, family_id)
    if not p.exists():
        return None
    import json
    return json.loads(p.read_text(encoding="utf-8"))


def find_family_for_asset(data_home: str | Path, file_sha256: str) -> str | None:
    d = families_dir(data_home)
    if not d.exists():
        return None
    import json
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if any(m.get("file_sha256") == file_sha256 for m in data.get("members", [])):
            return data.get("family_id")
    return None


def confirmed_language(language_detected: str, language_confidence: float) -> str | None:
    """A detected language only counts as confirmed once its confidence
    clears LANGUAGE_CONFIDENCE_FLOOR -- shared by member persistence
    (_candidate_member_dict) and by the UI layer, which must not offer a
    language-based human correction when either side's language is not
    reliably known (see app.py's ambiguous-item choice construction)."""
    if language_detected != "unknown" and language_confidence >= _LANGUAGE_CONFIDENCE_FLOOR:
        return language_detected
    return None


def _candidate_member_dict(c: Candidate) -> dict:
    # language_confirmed is deliberately NOT just a copy of language_detected:
    # it is only set when the detection confidence clears the same floor
    # already used elsewhere for language decisions (_LANGUAGE_CONFIDENCE_FLOOR).
    # A confirmed relation must never make a downstream consumer (TM/Translation
    # Intelligence) treat an unreliable language guess as validated -- see
    # is_language_variant_tm_safe(), which reads exactly this field.
    confirmed = confirmed_language(c.language_detected, c.language_confidence)
    return {
        "asset_id": derive_asset_id(c.file_sha256),
        "filename_original": c.filename_original,
        "source_path_at_confirmation": c.path,
        "file_sha256": c.file_sha256,
        "language_detected": c.language_detected,
        "language_confidence": c.language_confidence,
        "language_confirmed": confirmed,
        "revision_string_original": c.revision.original,
        "revision_normalized": c.revision.normalized if not isinstance(c.revision.normalized, tuple) else list(c.revision.normalized),
        "revision_scheme": c.revision.scheme,
        "project_dir": None,
    }


def is_language_variant_tm_safe(member_a: dict, member_b: dict) -> bool:
    """Fail-closed gate for a future TM/Translation Intelligence consumer.

    A confirmed language_variant_of relation (auto-detected OR manually
    corrected by a human -- see the human_override evidence appended in the
    UI layer) is NEVER by itself sufficient to treat the pair as a validated
    source/target seed. This must be checked independently before any such
    use: both languages must be confirmed (not merely detected) with
    sufficient confidence, and must actually differ. "Relazione confermata"
    is not the same fact as "lingua automaticamente validata".
    """
    for m in (member_a, member_b):
        confirmed = m.get("language_confirmed")
        if not confirmed or confirmed == "unknown":
            return False
        confidence = m.get("language_confidence")
        if confidence is None or confidence < _LANGUAGE_CONFIDENCE_FLOOR:
            return False
    return member_a.get("language_confirmed") != member_b.get("language_confirmed")


def persist_confirmation(
    data_home: str | Path, *, candidates: list[Candidate], relations: list[dict],
    confirmed_by: str, family_id: str | None = None,
) -> dict:
    """Writes/updates exactly one family JSON. Idempotent: re-confirming the
    same relation does not duplicate it. Atomic write (same helper used by
    project manifests)."""
    if not confirmed_by or not str(confirmed_by).strip():
        raise ValueError("confirmed_by è obbligatorio per persistere una relazione confermata.")

    if family_id is None:
        for c in candidates:
            existing = find_family_for_asset(data_home, c.file_sha256)
            if existing:
                family_id = existing
                break
    if family_id is None:
        family_id = f"FAM-{uuid4().hex[:12]}"

    existing_data = load_family(data_home, family_id) or {
        "schema_version": SCHEMA_VERSION,
        "family_id": family_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "members": [],
        "relations": [],
    }

    known_hashes = {m["file_sha256"] for m in existing_data["members"]}
    by_hash = {c.file_sha256: c for c in candidates}
    for c in candidates:
        if c.file_sha256 not in known_hashes:
            existing_data["members"].append(_candidate_member_dict(c))
            known_hashes.add(c.file_sha256)

    existing_relation_keys = {
        (r["relation_type"], frozenset((r["asset_a_hash"], r["asset_b_hash"])))
        for r in existing_data["relations"]
    }
    now = datetime.now(timezone.utc).isoformat()
    for rel in relations:
        asset_a_hash, asset_b_hash = rel["asset_a"], rel["asset_b"]
        key = (rel["relation_type"], frozenset((asset_a_hash, asset_b_hash)))
        if key in existing_relation_keys:
            continue
        existing_data["relations"].append({
            "relation_id": f"REL-{uuid4().hex[:12]}",
            "relation_type": rel["relation_type"],
            "asset_a": derive_asset_id(asset_a_hash),
            "asset_b": derive_asset_id(asset_b_hash),
            "asset_a_hash": asset_a_hash,
            "asset_b_hash": asset_b_hash,
            "order_confirmed": bool(rel.get("order_confirmed")),
            "newer_asset_hint": derive_asset_id(rel["newer_asset_hint"]) if rel.get("newer_asset_hint") else None,
            "evidence": rel.get("evidence", []),
            "confirmed_by": confirmed_by,
            "confirmed_at": now,
        })
        existing_relation_keys.add(key)

    existing_data["schema_version"] = SCHEMA_VERSION
    existing_data["family_id"] = family_id
    existing_data["updated_at"] = now

    families_dir(data_home).mkdir(parents=True, exist_ok=True)
    _atomic_write_json(family_json_path(data_home, family_id), existing_data)
    return existing_data


def snapshot_lineage_for_release(data_home: str | Path, asset_hashes: list[str]) -> dict:
    """Serializable, self-contained lineage snapshot for the given assets --
    for a future release manifest to embed/reference. Does not write anything;
    the caller decides where to store it. Hash-stamped so a historical release
    never changes meaning if LegacyFamilies/<family_id>.json is edited later.
    """
    import json
    seen_families: dict[str, dict] = {}
    for h in asset_hashes:
        fam_id = find_family_for_asset(data_home, h)
        if fam_id and fam_id not in seen_families:
            data = load_family(data_home, fam_id)
            if data:
                seen_families[fam_id] = data

    relevant_members = []
    relevant_relations = []
    asset_set = set(asset_hashes)
    for data in seen_families.values():
        for m in data["members"]:
            if m["file_sha256"] in asset_set:
                relevant_members.append(m)
        for r in data["relations"]:
            if r["asset_a_hash"] in asset_set or r["asset_b_hash"] in asset_set:
                relevant_relations.append(r)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_taken_at": datetime.now(timezone.utc).isoformat(),
        "requested_asset_hashes": list(asset_hashes),
        "members": relevant_members,
        "relations": relevant_relations,
    }
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    snapshot["snapshot_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return snapshot
