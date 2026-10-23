"""Translation Intelligence V1 — is this translation still reliable?

ERYLOS already computes a great deal about translations. The problem this
module solves is that almost none of it reaches a user: the full analysis
runs inside `generate_translation_work_packages`, which means the only
way to see it is to produce ZIP packages, and only for an approved
object. Everything in between -- match bands, the reason a memory reuse
was refused, per-language freshness -- is computed and thrown away.

This module composes what already exists into something answerable on
screen. It is a composer, not an engine:

    Tier 1   the *_translations.source_version comparison, the same one
             IndustrialRepository.assemble_structured_publication() uses,
             plus the project's configured target languages
    Tier 2   translation_impact.analyze_translation_impact(), called with
             out_dir pointed at a temporary directory

No CAT logic, no fuzzy algorithm, no scoring, no Translation Memory and
no technical signature is reimplemented or modified here. cat_tm.py is
not touched by this gate.

Read-only, verified
-------------------
`analyze_translation_impact` performs zero INSERT/UPDATE/DELETE and never
commits: it reads the repository and writes three CSV/JSON files into the
`out_dir` it is given. Given a temporary directory it leaves no trace in
the project at all. Tier 1 is plain SELECTs. Nothing here writes to the
database, bumps a version, creates a permanent file or emits an audit
event.

Legacy strings are classified, never rewritten
----------------------------------------------
The engine speaks Italian internally: `decision` carries
"RIUSO AUTOMATICO CONSENTITO", and `technical_reason` carries
"valori numerici diversi: [...] != [...]". Those are existing product
assets and this gate does not modify them.

Instead they are NORMALISED here into codes plus structured params, and
the prose is generated only from the i18n catalogs. That is what lets the
same engine answer in Italian and in English without being duplicated.

Where the engine already offers a structured field, the structured field
wins: `work_bucket` is used for the decision buckets rather than parsing
the Italian `decision` sentence, because a value that is already an
identifier should never be recovered from prose. Only `technical_reason`
is genuinely free text, and only its three known prefixes are classified,
with anything else falling through to a generic code rather than being
guessed at.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import project_services as ps
import domain_errors as de
from ccms_industrial import IndustrialRepository
from language_codes import canonical_lang, canonical_langs
from project_lifecycle import read_project

SUPPORTED_KINDS = ("topic", "block", "table", "figure")

_LIVE_TABLE = {
    "topic": ("topics", "topic_id", "title"),
    "block": ("content_blocks", "block_id", None),
    "table": ("structured_tables", "table_id", "title"),
    "figure": ("figures", "figure_id", "title"),
}
_TRANSLATION_TABLE = {
    "topic": ("topic_translations", "topic_id"),
    "block": ("block_translations", "block_id"),
    "table": ("structured_table_translations", "table_id"),
    "figure": ("figure_translations", "figure_id"),
}

# ----------------------------------------------------------------------
# Tier 1 — per-language reliability
# ----------------------------------------------------------------------
STATUS_FRESH = "FRESH"
STATUS_STALE = "STALE"
STATUS_MISSING = "MISSING"

TRANSLATION_CURRENT = "TRANSLATION_CURRENT"
TRANSLATION_SOURCE_VERSION_STALE = "TRANSLATION_SOURCE_VERSION_STALE"
TRANSLATION_MISSING_FOR_LANGUAGE = "TRANSLATION_MISSING_FOR_LANGUAGE"

_STATUS_CODE = {
    STATUS_FRESH: TRANSLATION_CURRENT,
    STATUS_STALE: TRANSLATION_SOURCE_VERSION_STALE,
    STATUS_MISSING: TRANSLATION_MISSING_FOR_LANGUAGE,
}

# Tier 2 availability -- explained, never raised as a technical error
TIER2_AVAILABLE = "TIER2_AVAILABLE"
TIER2_UNAVAILABLE_NOT_APPROVED = "TIER2_UNAVAILABLE_NOT_APPROVED"
TIER2_UNAVAILABLE_NO_TARGET_LANGS = "TIER2_UNAVAILABLE_NO_TARGET_LANGS"
TIER2_UNAVAILABLE_NO_CONTENT = "TIER2_UNAVAILABLE_NO_CONTENT"
TIER2_FAILED = "TIER2_FAILED"

# The engine's own gate, mirrored (never modified): analyze_translation_impact
# refuses anything outside these statuses.
_ANALYSABLE_STATUSES = frozenset({"Approved", "Published"})

# ----------------------------------------------------------------------
# Tier 2 — normalised memory-reuse vocabulary
# ----------------------------------------------------------------------
TM_REUSE_ALLOWED = "TM_REUSE_ALLOWED"
TM_REUSE_INTRA_DOCUMENT = "TM_REUSE_INTRA_DOCUMENT"
TM_NOT_TRANSLATABLE = "TM_NOT_TRANSLATABLE"
TM_LINGUISTIC_REVIEW = "TM_LINGUISTIC_REVIEW"
TM_NEW_TRANSLATION = "TM_NEW_TRANSLATION"
TM_BLOCKED_NUMERIC_MISMATCH = "TM_BLOCKED_NUMERIC_MISMATCH"
TM_BLOCKED_MODEL_CODE_MISMATCH = "TM_BLOCKED_MODEL_CODE_MISMATCH"
TM_BLOCKED_UNIT_MISMATCH = "TM_BLOCKED_UNIT_MISMATCH"
TM_BLOCKED_TECHNICAL_OTHER = "TM_BLOCKED_TECHNICAL_OTHER"
TM_NO_MEMORY_FOR_LANGUAGE = "TM_NO_MEMORY_FOR_LANGUAGE"

# work_bucket is a structured field on the engine's own detail frame.
_BUCKET_CODE = {
    "AUTO_REUSE": TM_REUSE_ALLOWED,
    "INTRA_DOCUMENT_REUSE": TM_REUSE_INTRA_DOCUMENT,
    "NOT_TRANSLATABLE": TM_NOT_TRANSLATABLE,
    "LINGUISTIC_REVIEW": TM_LINGUISTIC_REVIEW,
    "NEW_TRANSLATION": TM_NEW_TRANSLATION,
    "TECHNICAL_REVIEW": TM_BLOCKED_TECHNICAL_OTHER,  # refined below by reason
}

# match_category is also the engine's own vocabulary
MATCH_CONTEXT = "MATCH_CONTEXT"
MATCH_EXACT = "MATCH_EXACT"
MATCH_FUZZY = "MATCH_FUZZY"
MATCH_TECHNICAL_BLOCK = "MATCH_TECHNICAL_BLOCK"
MATCH_NEW = "MATCH_NEW"

# The three technical_reason prefixes cat_tm.signatures_compatible emits.
# Matched as prefixes, with the payload kept as a param. Anything else
# falls through to TM_BLOCKED_TECHNICAL_OTHER rather than being guessed.
_REASON_PREFIXES = (
    ("valori numerici diversi", TM_BLOCKED_NUMERIC_MISMATCH),
    ("codici/modelli diversi", TM_BLOCKED_MODEL_CODE_MISMATCH),
    ("unità tecniche diverse", TM_BLOCKED_UNIT_MISMATCH),
)


def classify_technical_reason(reason: str) -> tuple[str, dict[str, str]]:
    """Legacy engine sentence -> (code, params). Never modifies the engine.

    The engine writes, for example::

        valori numerici diversi: ['12'] != ['16']

    which is Italian prose with the evidence embedded. Classifying the
    prefix keeps the evidence (the two value lists) as structured params
    so the catalog can say the same thing in either language.
    """
    text = str(reason or "").strip()
    if not text:
        return TM_REUSE_ALLOWED, {}
    low = text.casefold()
    for prefix, code in _REASON_PREFIXES:
        if low.startswith(prefix.casefold()):
            payload = text[len(prefix):].lstrip(": ").strip()
            memory_values, content_values = _split_values(payload)
            # Order verified at cat_tm.py:502/555 -- the call is
            # signatures_compatible(_row_signature(candidate), seg.signature),
            # so the FIRST list is the memory entry's and the second is the
            # current content's. Naming them accurately matters: telling a
            # user the wrong way round which side carries which value would
            # make the explanation worse than no explanation.
            return code, {"detail": payload, "memory_values": memory_values,
                          "content_values": content_values}
    return TM_BLOCKED_TECHNICAL_OTHER, {"detail": text}


def _split_values(payload: str) -> tuple[str, str]:
    """'[12] != [16]' -> ('12', '16'). Best effort, never raises."""
    for sep in ("≠", "!="):
        if sep in payload:
            left, _, right = payload.partition(sep)
            return _clean_list(left), _clean_list(right)
    return _clean_list(payload), ""


def _clean_list(chunk: str) -> str:
    return re.sub(r"[\[\]'\"]", "", str(chunk)).strip()


def classify_match_category(category: str) -> str:
    cat = str(category or "").strip().upper()
    if cat.startswith("CONTEXT MATCH"):
        return MATCH_CONTEXT
    if cat.startswith("100% MATCH"):
        return MATCH_EXACT
    if cat.startswith("FUZZY MATCH"):
        return MATCH_FUZZY
    if cat.startswith("TECHNICAL SAFE BLOCK"):
        return MATCH_TECHNICAL_BLOCK
    return MATCH_NEW


# ----------------------------------------------------------------------
# Value objects
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class LanguageStatus:
    language: str
    status: str                      # FRESH | STALE | MISSING
    code: str
    current_version: int
    source_version: int | None = None
    translation_status: str = ""
    configured: bool = True          # declared in the project manifest

    @property
    def params(self) -> dict[str, Any]:
        out: dict[str, Any] = {"target_lang": self.language,
                               "current_version": self.current_version}
        if self.source_version is not None:
            out["source_version"] = self.source_version
        if self.translation_status:
            out["translation_status"] = self.translation_status
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"language": self.language, "status": self.status, "code": self.code,
                "current_version": self.current_version, "source_version": self.source_version,
                "translation_status": self.translation_status, "configured": self.configured}


@dataclass(frozen=True)
class ReuseReason:
    """One normalised explanation of why a memory reuse was or was not allowed."""
    code: str
    segment_id: str
    source_preview: str
    params: tuple[tuple[str, Any], ...] = ()

    @property
    def param_dict(self) -> dict[str, Any]:
        return dict(self.params)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "segment_id": self.segment_id,
                "source_preview": self.source_preview, "params": self.param_dict}


@dataclass(frozen=True)
class LanguageImpact:
    language: str
    segments: int
    words: int
    reusable_segments: int
    context_segments: int
    exact_segments: int
    fuzzy_segments: int
    new_segments: int
    technical_block_segments: int
    tm_available: bool
    reuse_pct: float
    reasons: tuple[ReuseReason, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"language": self.language, "segments": self.segments, "words": self.words,
                "reusable_segments": self.reusable_segments,
                "context_segments": self.context_segments,
                "exact_segments": self.exact_segments, "fuzzy_segments": self.fuzzy_segments,
                "new_segments": self.new_segments,
                "technical_block_segments": self.technical_block_segments,
                "tm_available": self.tm_available, "reuse_pct": self.reuse_pct,
                "reasons": [r.to_dict() for r in self.reasons]}


@dataclass(frozen=True)
class TranslationReport:
    object_type: str
    object_id: str
    object_version: int
    object_status: str
    object_label: str
    source_lang: str
    languages: tuple[LanguageStatus, ...]
    tier2_state: str
    tier2: tuple[LanguageImpact, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def tier2_available(self) -> bool:
        return self.tier2_state == TIER2_AVAILABLE

    def language(self, code: str) -> LanguageStatus | None:
        want = canonical_lang(code)
        return next((l for l in self.languages if canonical_lang(l.language) == want), None)

    def has_attention(self) -> bool:
        """Something that BECAME unreliable, i.e. a stale translation.

        Deliberately not counting MISSING. A language that was never
        translated is a fact about the project's progress, already visible
        in the per-language list and already counted by release readiness;
        treating it as an alert would light the panel up permanently on
        every new object and train the user to ignore it. STALE is
        different in kind: it means something that WAS correct no longer
        is, because the source moved underneath it.
        """
        return bool(self.counts.get("stale"))

    def to_dict(self) -> dict[str, Any]:
        return {"object_type": self.object_type, "object_id": self.object_id,
                "object_version": self.object_version, "object_status": self.object_status,
                "object_label": self.object_label, "source_lang": self.source_lang,
                "languages": [l.to_dict() for l in self.languages],
                "tier2_state": self.tier2_state,
                "tier2": [t.to_dict() for t in self.tier2],
                "counts": dict(self.counts)}


def summarize(languages: Iterable[LanguageStatus]) -> dict[str, int]:
    """Counts derived from the per-language detail, never maintained."""
    ls = tuple(languages)
    return {
        "configured": sum(1 for l in ls if l.configured),
        "present": sum(1 for l in ls if l.status != STATUS_MISSING),
        "fresh": sum(1 for l in ls if l.status == STATUS_FRESH),
        "stale": sum(1 for l in ls if l.status == STATUS_STALE),
        "missing": sum(1 for l in ls if l.status == STATUS_MISSING),
    }


# ----------------------------------------------------------------------
# Object resolution
# ----------------------------------------------------------------------

def detect_object_type(repo: IndustrialRepository, object_id: str) -> str | None:
    """Which of the four kinds owns this id, or None.

    Probing is deliberate rather than parsing the id prefix: ids are
    opaque and a prefix convention is not a guarantee the schema makes.
    """
    for kind, (table, key, _title) in _LIVE_TABLE.items():
        if repo.conn.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (object_id,)).fetchone():
            return kind
    return None


def _load_object(repo: IndustrialRepository, kind: str, object_id: str) -> dict[str, Any]:
    table, key, title_col = _LIVE_TABLE[kind]
    row = repo.conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (object_id,)).fetchone()
    if not row:
        raise de.DomainNotFoundError(de.ERR_CONTENT_OBJECT_NOT_FOUND,
                                     object_type=kind, object_id=object_id)
    data = dict(row)
    label = str(data.get(title_col) or "") if title_col else ""
    if not label:
        text = str(data.get("text_content") or "").strip()
        label = (text[:60] + "…") if len(text) > 60 else text
    return {"row": data, "label": label, "version": int(data.get("version") or 1),
            "status": str(data.get("status") or "")}


def _object_source_lang(repo: IndustrialRepository, kind: str, object_id: str,
                        row: dict[str, Any]) -> str:
    """The object's own source language, or its topic's, or the publication's."""
    direct = canonical_lang(str(row.get("source_lang") or ""))
    if direct:
        return direct
    topic_id = str(row.get("topic_id") or "")
    if kind == "topic":
        topic_id = object_id
    if topic_id:
        r = repo.conn.execute("SELECT source_lang FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if r and r["source_lang"]:
            return canonical_lang(str(r["source_lang"]))
    r = repo.conn.execute("SELECT source_lang FROM publications LIMIT 1").fetchone()
    return canonical_lang(str(r["source_lang"])) if r and r["source_lang"] else ""


# ----------------------------------------------------------------------
# Tier 1
# ----------------------------------------------------------------------

def _collect_languages(repo: IndustrialRepository, kind: str, object_id: str,
                       current_version: int, configured: list[str]) -> tuple[LanguageStatus, ...]:
    table, key = _TRANSLATION_TABLE[kind]
    present: dict[str, dict[str, Any]] = {}
    for r in repo.conn.execute(
            f"SELECT target_lang, source_version, status FROM {table} WHERE {key}=?",
            (object_id,)):
        present[canonical_lang(str(r["target_lang"]))] = {
            "source_version": int(r["source_version"]),
            "status": str(r["status"] or ""),
        }

    # Every configured language, plus any translated language the manifest
    # does not declare -- a translation that exists is a fact regardless of
    # whether the project still lists that language.
    order = list(dict.fromkeys(list(configured) + sorted(present)))
    out: list[LanguageStatus] = []
    for lang in order:
        if not lang:
            continue
        row = present.get(lang)
        if row is None:
            out.append(LanguageStatus(language=lang, status=STATUS_MISSING,
                                      code=_STATUS_CODE[STATUS_MISSING],
                                      current_version=current_version,
                                      configured=lang in configured))
            continue
        stale = int(row["source_version"]) != int(current_version)
        status = STATUS_STALE if stale else STATUS_FRESH
        out.append(LanguageStatus(language=lang, status=status, code=_STATUS_CODE[status],
                                  current_version=current_version,
                                  source_version=int(row["source_version"]),
                                  translation_status=row["status"],
                                  configured=lang in configured))
    return tuple(out)


# ----------------------------------------------------------------------
# Tier 2
# ----------------------------------------------------------------------

def _tier2_state(object_status: str, languages: tuple[LanguageStatus, ...],
                 has_text: bool) -> str:
    """Why the impact analysis can or cannot run -- as a code, not an error.

    Mirrors analyze_translation_impact's own preconditions without
    changing them. require_approved stays exactly as the engine defines
    it; this only explains the consequence to the user instead of letting
    a ValueError surface.
    """
    if object_status not in _ANALYSABLE_STATUSES:
        return TIER2_UNAVAILABLE_NOT_APPROVED
    if not languages:
        return TIER2_UNAVAILABLE_NO_TARGET_LANGS
    if not has_text:
        return TIER2_UNAVAILABLE_NO_CONTENT
    return TIER2_AVAILABLE


def _project_tm_paths(project_dir: Path) -> list[Path]:
    """Translation memories already present in the project. Read-only."""
    tm_dir = Path(project_dir) / "TranslationMemory"
    if not tm_dir.is_dir():
        return []
    return sorted(p for p in tm_dir.glob("*.sqlite") if p.is_file())


def _reasons_for_language(frame, limit: int = 12) -> tuple[ReuseReason, ...]:
    """Normalise the engine's per-segment verdicts into codes + params."""
    out: list[ReuseReason] = []
    for _, r in frame.iterrows():
        bucket = str(r.get("work_bucket") or "")
        reason_text = str(r.get("technical_reason") or "")
        tm_available = bool(r.get("tm_available"))
        if bucket == "TECHNICAL_REVIEW":
            # Only a genuine technical block gets its reason classified.
            code, params = classify_technical_reason(reason_text)
            if code == TM_REUSE_ALLOWED:
                code = TM_BLOCKED_TECHNICAL_OTHER
        elif not tm_available:
            # The engine fills technical_reason with "TM non disponibile per
            # questa lingua" when there is no memory at all. That is not a
            # technical block and must not be reported as one -- an earlier
            # cut classified it as TM_BLOCKED_TECHNICAL_OTHER, which told
            # the user a reuse had been refused when none was ever possible.
            code, params = TM_NO_MEMORY_FOR_LANGUAGE, {}
        else:
            code = _BUCKET_CODE.get(bucket, TM_NEW_TRANSLATION)
            params = {}
        source = str(r.get("source") or "")
        out.append(ReuseReason(
            code=code,
            segment_id=str(r.get("segment_id") or ""),
            source_preview=(source[:80] + "…") if len(source) > 80 else source,
            params=tuple(sorted((str(k), v) for k, v in params.items()))))
        if len(out) >= limit:
            break
    return tuple(out)


def _run_tier2(repo: IndustrialRepository, project_dir: Path, kind: str, object_id: str,
               languages: tuple[LanguageStatus, ...]) -> tuple[tuple[LanguageImpact, ...], str]:
    """Call the EXISTING engine with out_dir on a temp directory.

    analyze_translation_impact never writes to the database; pointing its
    output at a temporary directory means the project is untouched.
    """
    from translation_impact import analyze_translation_impact  # local: heavy import

    targets = [l.language for l in languages if l.language]
    tmp = Path(tempfile.mkdtemp(prefix="manualsync_translation_intel_"))
    # The engine writes a consolidated TM and three CSV/JSON reports into
    # tmp, and returns detail and language_summary as in-memory DataFrames.
    # Only those DataFrames are read below; the file paths it also returns
    # are never used. So tmp is removed in `finally`, which runs after the
    # return value has been built on every path -- success, the two early
    # returns, a failure the engine reports, and an unexpected exception
    # while reading the result (which still propagates, as before). It used
    # to be left behind on every "Analisi impatto traduzione".
    try:
        try:
            result = analyze_translation_impact(
                repo, object_id, kind=kind,
                tm_paths=[str(p) for p in _project_tm_paths(project_dir)],
                target_langs=targets, require_approved=True, out_dir=tmp)
        except Exception:
            # The engine fails closed on its own preconditions. Anything it
            # refuses is reported as an unavailable state, never as a stack
            # trace in a read-only panel.
            return (), TIER2_FAILED

        detail = result.get("detail")
        summary = result.get("language_summary")
        out: list[LanguageImpact] = []
        if summary is None or summary.empty:
            return (), TIER2_AVAILABLE
        for _, s in summary.iterrows():
            lang = canonical_lang(str(s.get("target_lang") or ""))
            rows = detail[detail["target_lang"].astype(str).map(canonical_lang) == lang] \
                if detail is not None and not detail.empty else None
            cats = (rows["match_category"].map(classify_match_category).tolist()
                    if rows is not None and not rows.empty and "match_category" in rows else [])
            buckets = (rows["work_bucket"].astype(str).tolist()
                       if rows is not None and not rows.empty and "work_bucket" in rows else [])
            out.append(LanguageImpact(
                language=lang,
                segments=int(s.get("segments") or 0),
                words=int(s.get("source_words") or 0),
                reusable_segments=sum(1 for b in buckets
                                      if b in ("AUTO_REUSE", "INTRA_DOCUMENT_REUSE", "NOT_TRANSLATABLE")),
                context_segments=cats.count(MATCH_CONTEXT),
                exact_segments=cats.count(MATCH_EXACT),
                fuzzy_segments=cats.count(MATCH_FUZZY),
                new_segments=cats.count(MATCH_NEW),
                technical_block_segments=sum(1 for b in buckets if b == "TECHNICAL_REVIEW"),
                tm_available=bool(s.get("tm_available")),
                reuse_pct=float(s.get("reuse_pct") or 0.0),
                reasons=_reasons_for_language(rows) if rows is not None and not rows.empty else ()))
        return tuple(out), TIER2_AVAILABLE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

def assess_translation_intelligence(project_dir: str | Path, object_id: str,
                                    language: str | None = None,
                                    object_type: str | None = None,
                                    deep: bool = False) -> TranslationReport:
    """Translation reliability for one content object. Read-only.

    `object_type` may be omitted: the four live tables are probed to
    resolve it. `language` narrows the report to one target language.
    `deep=True` additionally runs the existing impact engine -- the action
    the UI calls "Analisi impatto traduzione".
    """
    project_dir = Path(str(project_dir))
    object_id = str(object_id or "").strip()
    if not object_id:
        raise de.DomainNotFoundError(de.ERR_CONTENT_OBJECT_NOT_FOUND,
                                     object_type=str(object_type or ""), object_id=object_id)
    if object_type is not None:
        kind = str(object_type).strip().lower()
        if kind not in SUPPORTED_KINDS:
            raise de.DomainValidationError(de.ERR_TRANSLATION_KIND_UNSUPPORTED,
                                           object_type=object_type,
                                           allowed=", ".join(SUPPORTED_KINDS))
    else:
        kind = None

    repo, _auth = ps.open_authoring_service(str(project_dir))
    try:
        if kind is None:
            kind = detect_object_type(repo, object_id)
            if kind is None:
                raise de.DomainNotFoundError(de.ERR_CONTENT_OBJECT_NOT_FOUND,
                                             object_type="", object_id=object_id)
        obj = _load_object(repo, kind, object_id)
        version = obj["version"]
        source_lang = _object_source_lang(repo, kind, object_id, obj["row"])

        configured = [l for l in canonical_langs(
            read_project(project_dir).get("target_langs", []) or []) if l]
        languages = _collect_languages(repo, kind, object_id, version, configured)
        if language:
            want = canonical_lang(language)
            languages = tuple(l for l in languages if canonical_lang(l.language) == want)

        has_text = bool(str(obj["row"].get("text_content") or obj["row"].get("body")
                            or obj["row"].get("title") or obj["row"].get("caption") or "").strip())
        tier2_state = _tier2_state(obj["status"], languages, has_text)

        tier2: tuple[LanguageImpact, ...] = ()
        if deep and tier2_state == TIER2_AVAILABLE:
            tier2, tier2_state = _run_tier2(repo, project_dir, kind, object_id, languages)
    finally:
        repo.close()

    return TranslationReport(
        object_type=kind, object_id=object_id, object_version=version,
        object_status=obj["status"], object_label=obj["label"], source_lang=source_lang,
        languages=languages, tier2_state=tier2_state, tier2=tier2,
        counts=summarize(languages))
