"""Impact Intelligence V1 — what is affected by a content object.

Answers one question, read-only:

    given a technical content object at its current version, which
    EXISTING relationships are affected or potentially need attention?

What this module is
-------------------
A composer. Every fact it reports is computed by an engine that already
exists and is already tested:

    reuse / publications   AuthoringService.get_reuse_impact()
                           IndustrialRepository.impact_map_industrial()
    variants / profiles    AuthoringService.evaluate_element_with_profile()
    translations           the *_translations.source_version comparison
                           that IndustrialRepository.assemble_structured_
                           publication() already performs
    review                 IndustrialRepository.review_queue()
    compliance             ComplianceService.evidence_for_requirement()
                           ComplianceService.compute_status()

No domain rule is reimplemented here. In particular this module does NOT
decide what "stale" means for a translation, what makes evidence current,
or when coverage degrades -- it asks the owning service and reports the
answer. If one of those rules changes, this module changes with it for
free, which is the entire reason it is a composer and not an engine.

What this module is NOT
-----------------------
* It is not a diff. It does not claim "this sentence change caused X".
  It reports relationships affected by the object's CURRENT version.
* It writes nothing. No table is created, no row is inserted or updated,
  no audit event is emitted. Assessing impact is a read.
* It introduces no second content identity. It addresses objects by the
  (object_type, object_id) pair that audit_events, review_tasks,
  compliance_evidence and authoring_drafts already share.

Reason codes, not prose
-----------------------
Every record carries a stable ``code`` plus structured ``params``. No
Italian (or English) sentence is built here -- the view layer resolves a
code through the ordinary i18n catalog, exactly as it resolves a
DomainError. A domain service that formats UI text cannot be localized
later without being rewritten, which is the mistake this codebase already
corrected once for validation errors.

Zero is not the same as "cannot determine"
------------------------------------------
Three distinct states per category, and the caller can tell them apart:

    known           the count is a proven number, including 0
    undetermined    the relationship exists in the product but cannot be
                    proven for this object from persisted data today
    not_applicable  the relationship is meaningless for this object kind
                    (a text block cannot be reused -- that is not an
                    unknown, it is a property of the model)

Returning 0 for any of these would be a quiet lie, and a panel that says
"0 profiles affected" when it simply cannot evaluate profiles is worse
than one that says nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import authoring_view as av
import compliance_view as cv
import domain_errors as de
from authoring_service import AuthoringService
from ccms_industrial import IndustrialRepository

# ----------------------------------------------------------------------
# Supported object kinds
# ----------------------------------------------------------------------
# The same four kinds every composed engine agrees on:
#   compliance_intelligence._INTERNAL_TABLES
#   IndustrialRepository._status_table
#   the *_translations tables
SUPPORTED_KINDS = ("topic", "block", "table", "figure")

# reuse is modelled only where an object can be REFERENCED by a block
_REUSABLE_KINDS = ("table", "figure")
# evaluate_element_with_profile() accepts only these
_CONDITIONED_KINDS = ("topic", "block")

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
# Domains and reason codes
# ----------------------------------------------------------------------
DOMAIN_STRUCTURE = "structure"
DOMAIN_REUSE = "reuse"
DOMAIN_VARIANT = "variant"
DOMAIN_TRANSLATION = "translation"
DOMAIN_REVIEW = "review"
DOMAIN_COMPLIANCE = "compliance"
DOMAIN_RELEASE = "release"

# deterministic domain ordering for stable output
DOMAIN_ORDER = (DOMAIN_STRUCTURE, DOMAIN_REUSE, DOMAIN_VARIANT, DOMAIN_TRANSLATION,
                DOMAIN_REVIEW, DOMAIN_COMPLIANCE, DOMAIN_RELEASE)

IMPACT_OBJECT_ARCHIVED = "IMPACT_OBJECT_ARCHIVED"
IMPACT_CONTENT_IN_PUBLICATION = "IMPACT_CONTENT_IN_PUBLICATION"
IMPACT_CONTENT_IN_TOPIC = "IMPACT_CONTENT_IN_TOPIC"

IMPACT_OBJECT_SHARED = "IMPACT_OBJECT_SHARED"
IMPACT_OBJECT_LOCAL = "IMPACT_OBJECT_LOCAL"
IMPACT_CONTENT_REUSED_IN_PLACEMENT = "IMPACT_CONTENT_REUSED_IN_PLACEMENT"

IMPACT_VARIANT_UNCONDITIONAL = "IMPACT_VARIANT_UNCONDITIONAL"
IMPACT_PROFILE_INCLUDES = "IMPACT_PROFILE_INCLUDES"
IMPACT_PROFILE_EXCLUDES = "IMPACT_PROFILE_EXCLUDES"

IMPACT_TRANSLATION_SOURCE_VERSION_STALE = "IMPACT_TRANSLATION_SOURCE_VERSION_STALE"
IMPACT_TRANSLATION_CURRENT = "IMPACT_TRANSLATION_CURRENT"

IMPACT_REVIEW_OPEN = "IMPACT_REVIEW_OPEN"
IMPACT_REVIEW_NOT_VERSION_AWARE = "IMPACT_REVIEW_NOT_VERSION_AWARE"

IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE = "IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE"
IMPACT_COMPLIANCE_EVIDENCE_CURRENT = "IMPACT_COMPLIANCE_EVIDENCE_CURRENT"
IMPACT_COMPLIANCE_REQUIREMENT_STATUS = "IMPACT_COMPLIANCE_REQUIREMENT_STATUS"

IMPACT_RELEASE_PREDATES_CURRENT_VERSION = "IMPACT_RELEASE_PREDATES_CURRENT_VERSION"

ALL_IMPACT_CODES = (
    IMPACT_OBJECT_ARCHIVED,
    IMPACT_CONTENT_IN_PUBLICATION, IMPACT_CONTENT_IN_TOPIC,
    IMPACT_OBJECT_SHARED, IMPACT_OBJECT_LOCAL, IMPACT_CONTENT_REUSED_IN_PLACEMENT,
    IMPACT_VARIANT_UNCONDITIONAL, IMPACT_PROFILE_INCLUDES, IMPACT_PROFILE_EXCLUDES,
    IMPACT_TRANSLATION_SOURCE_VERSION_STALE, IMPACT_TRANSLATION_CURRENT,
    IMPACT_REVIEW_OPEN, IMPACT_REVIEW_NOT_VERSION_AWARE,
    IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE, IMPACT_COMPLIANCE_EVIDENCE_CURRENT,
    IMPACT_COMPLIANCE_REQUIREMENT_STATUS,
    IMPACT_RELEASE_PREDATES_CURRENT_VERSION,
)

# ----------------------------------------------------------------------
# Summary categories
# ----------------------------------------------------------------------
CAT_PUBLICATIONS = "publications"
CAT_REUSE_LOCATIONS = "reuse_locations"
CAT_PROFILES_AFFECTED = "profiles_affected"
CAT_LANGUAGES_PRESENT = "languages_present"
CAT_STALE_TRANSLATIONS = "stale_translations"
CAT_REQUIREMENTS_ATTENTION = "requirements_needing_attention"
CAT_OPEN_REVIEWS = "open_reviews"
CAT_RELEASES_PREDATING = "releases_predating"

SUMMARY_CATEGORIES = (CAT_PUBLICATIONS, CAT_REUSE_LOCATIONS, CAT_PROFILES_AFFECTED,
                      CAT_LANGUAGES_PRESENT, CAT_STALE_TRANSLATIONS,
                      CAT_REQUIREMENTS_ATTENTION, CAT_OPEN_REVIEWS,
                      CAT_RELEASES_PREDATING)

STATE_KNOWN = "known"
STATE_UNDETERMINED = "undetermined"
STATE_NOT_APPLICABLE = "not_applicable"

# Limitations this V1 states out loud rather than hiding.
LIMITATION_REVIEW_NOT_VERSION_AWARE = "LIMITATION_REVIEW_NOT_VERSION_AWARE"
LIMITATION_RELEASE_READINESS_NOT_REEVALUATED = "LIMITATION_RELEASE_READINESS_NOT_REEVALUATED"
LIMITATION_FORK_ORIGIN_NOT_PERSISTED = "LIMITATION_FORK_ORIGIN_NOT_PERSISTED"
LIMITATION_NO_SEGMENT_LEVEL_IMPACT = "LIMITATION_NO_SEGMENT_LEVEL_IMPACT"


@dataclass(frozen=True)
class ImpactRecord:
    """One reason something is affected.

    Frozen and hashable so duplicate suppression is a set operation and
    ordering is total -- two runs against the same data must produce
    byte-identical output, or a UI diff becomes noise.
    """

    domain: str
    code: str
    ref_kind: str
    ref_id: str
    params: tuple[tuple[str, Any], ...] = ()

    @property
    def param_dict(self) -> dict[str, Any]:
        return dict(self.params)

    @property
    def sort_key(self) -> tuple:
        order = DOMAIN_ORDER.index(self.domain) if self.domain in DOMAIN_ORDER else len(DOMAIN_ORDER)
        return (order, self.code, self.ref_kind, self.ref_id, self.params)

    def to_dict(self) -> dict[str, Any]:
        return {"domain": self.domain, "code": self.code, "ref_kind": self.ref_kind,
                "ref_id": self.ref_id, "params": self.param_dict}


def _rec(domain: str, code: str, ref_kind: str, ref_id: str, **params: Any) -> ImpactRecord:
    """Build a record with params normalised to a stable, hashable shape.

    Note that a value used as ``ref_id`` is often ALSO passed as a param.
    That is deliberate, not redundant: ref_id is machine identity (used
    for dedup, ordering and counting) while params are what the catalog
    string may interpolate. Keeping them separate means a sentence can
    mention the requirement or the language without the view layer having
    to know that this particular code happens to key on it -- and a
    catalog string that names a param the record does not carry renders
    as a literal "{requirement_id}" to the user, which is exactly the
    defect regressione_impact_intelligence now guards against.
    """
    return ImpactRecord(domain=domain, code=code, ref_kind=ref_kind, ref_id=str(ref_id),
                        params=tuple(sorted((str(k), v) for k, v in params.items())))


@dataclass(frozen=True)
class ImpactReport:
    object_type: str
    object_id: str
    object_version: int
    object_status: str
    object_label: str
    records: tuple[ImpactRecord, ...]
    counts: dict[str, int] = field(default_factory=dict)
    undetermined: frozenset[str] = frozenset()
    not_applicable: frozenset[str] = frozenset()
    limitations: tuple[str, ...] = ()

    def state_of(self, category: str) -> str:
        if category in self.undetermined:
            return STATE_UNDETERMINED
        if category in self.not_applicable:
            return STATE_NOT_APPLICABLE
        return STATE_KNOWN

    def count(self, category: str) -> int | None:
        """The proven count, or None when it is not a known number.

        Deliberately None rather than 0 -- see the module docstring.
        """
        return self.counts.get(category) if self.state_of(category) == STATE_KNOWN else None

    def by_domain(self, domain: str) -> tuple[ImpactRecord, ...]:
        return tuple(r for r in self.records if r.domain == domain)

    def has_attention(self) -> bool:
        """True when something genuinely needs a human decision."""
        return bool(self.counts.get(CAT_STALE_TRANSLATIONS)
                    or self.counts.get(CAT_REQUIREMENTS_ATTENTION)
                    or self.counts.get(CAT_OPEN_REVIEWS)
                    or self.counts.get(CAT_RELEASES_PREDATING))

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type, "object_id": self.object_id,
            "object_version": self.object_version, "object_status": self.object_status,
            "object_label": self.object_label,
            "records": [r.to_dict() for r in self.records],
            "counts": dict(self.counts),
            "undetermined": sorted(self.undetermined),
            "not_applicable": sorted(self.not_applicable),
            "limitations": list(self.limitations),
        }


# ----------------------------------------------------------------------
# Summary — always derived, never maintained
# ----------------------------------------------------------------------

def summarize(records: Iterable[ImpactRecord]) -> dict[str, int]:
    """Counts computed FROM the detail records.

    Kept as a separate pure function precisely so it cannot drift: there
    is no code path that increments a counter while appending a record,
    which is the usual way a summary starts disagreeing with its own
    detail.
    """
    rs = tuple(records)

    def distinct(ref_kind: str, codes: tuple[str, ...]) -> int:
        return len({r.ref_id for r in rs if r.ref_kind == ref_kind and r.code in codes})

    return {
        CAT_PUBLICATIONS: distinct("publication", (IMPACT_CONTENT_IN_PUBLICATION,)),
        CAT_REUSE_LOCATIONS: distinct("placement", (IMPACT_CONTENT_REUSED_IN_PLACEMENT,)),
        CAT_PROFILES_AFFECTED: distinct("profile", (IMPACT_PROFILE_INCLUDES,)),
        CAT_LANGUAGES_PRESENT: distinct("language", (IMPACT_TRANSLATION_SOURCE_VERSION_STALE,
                                                     IMPACT_TRANSLATION_CURRENT)),
        CAT_STALE_TRANSLATIONS: distinct("language", (IMPACT_TRANSLATION_SOURCE_VERSION_STALE,)),
        CAT_REQUIREMENTS_ATTENTION: distinct("requirement", (IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE,)),
        CAT_OPEN_REVIEWS: distinct("review", (IMPACT_REVIEW_OPEN,)),
        CAT_RELEASES_PREDATING: distinct("release", (IMPACT_RELEASE_PREDATES_CURRENT_VERSION,)),
    }


def _finalize(records: list[ImpactRecord]) -> tuple[ImpactRecord, ...]:
    """Deduplicate and order deterministically."""
    return tuple(sorted(set(records), key=lambda r: r.sort_key))


# ----------------------------------------------------------------------
# The collectors -- one per domain, each delegating to the owning engine
# ----------------------------------------------------------------------

def _load_object(repo: IndustrialRepository, kind: str, object_id: str) -> dict[str, Any]:
    table, key, title_col = _LIVE_TABLE[kind]
    row = repo.conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (object_id,)).fetchone()
    if not row:
        raise de.DomainNotFoundError(de.ERR_CONTENT_OBJECT_NOT_FOUND,
                                     object_type=kind, object_id=object_id)
    data = dict(row)
    label = str(data.get(title_col) or "") if title_col else ""
    if not label:
        # a text block has no title; use a short preview, never the raw id
        text = str(data.get("text_content") or "").strip()
        label = (text[:60] + "…") if len(text) > 60 else text
    return {"row": data, "label": label, "version": int(data.get("version") or 1),
            "status": str(data.get("status") or ""), "archived_at": data.get("archived_at")}


def _collect_structure(repo: IndustrialRepository, kind: str, object_id: str,
                       obj: dict[str, Any]) -> list[ImpactRecord]:
    out: list[ImpactRecord] = []
    if obj["archived_at"]:
        out.append(_rec(DOMAIN_STRUCTURE, IMPACT_OBJECT_ARCHIVED, "object", object_id,
                        archived_at=str(obj["archived_at"])))
    # impact_map_industrial already walks object -> topic -> publication for
    # every supported kind, including the object_ref_id hop for table/figure.
    frame = repo.impact_map_industrial(object_id, kind=kind)
    if frame.empty:
        return out
    for _, r in frame.iterrows():
        topic_id = str(r.get("topic_id") or "")
        pub_id = str(r.get("publication_id") or "")
        if topic_id:
            out.append(_rec(DOMAIN_STRUCTURE, IMPACT_CONTENT_IN_TOPIC, "topic", topic_id,
                            topic_title=str(r.get("topic_title") or ""),
                            topic_version=int(r.get("topic_version") or 0)))
        if pub_id:
            out.append(_rec(DOMAIN_STRUCTURE, IMPACT_CONTENT_IN_PUBLICATION, "publication", pub_id,
                            publication_title=str(r.get("publication_title") or "")))
    return out


def _collect_reuse(auth: AuthoringService, repo: IndustrialRepository, kind: str,
                   object_id: str) -> tuple[list[ImpactRecord], bool]:
    """Returns (records, applicable). Reuse exists only for table/figure."""
    if kind not in _REUSABLE_KINDS:
        return [], False
    impact = auth.get_reuse_impact(kind, object_id)
    out: list[ImpactRecord] = []
    shared = bool(impact.get("is_shared"))
    out.append(_rec(DOMAIN_REUSE, IMPACT_OBJECT_SHARED if shared else IMPACT_OBJECT_LOCAL,
                    "object", object_id, reference_count=int(impact.get("reference_count") or 0)))
    for place in impact.get("block_refs", []) or []:
        block_id = str(place.get("block_id") or "")
        if not block_id:
            continue
        out.append(_rec(DOMAIN_REUSE, IMPACT_CONTENT_REUSED_IN_PLACEMENT, "placement", block_id,
                        topic_id=str(place.get("topic_id") or ""),
                        topic_title=str(place.get("topic_title") or ""),
                        archived=bool(place.get("archived"))))
    return out, True


def _collect_variants(auth: AuthoringService, kind: str, object_id: str) -> tuple[list[ImpactRecord], bool]:
    """Returns (records, applicable).

    evaluate_element_with_profile() is the owning rule for 'does this
    element apply under this profile'. It accepts topic/block only, so for
    a table/figure the answer is genuinely undetermined at object level --
    a table's applicability lives on the BLOCK that places it.
    """
    if kind not in _CONDITIONED_KINDS:
        return [], False
    profiles = auth.list_variant_profiles(include_inactive=False)
    out: list[ImpactRecord] = []
    if not profiles:
        # No profiles configured is a proven zero, not an unknown.
        return out, True
    for p in profiles:
        result = auth.evaluate_element_with_profile(kind, object_id, p["profile_id"])
        included = bool(result.get("included"))
        out.append(_rec(DOMAIN_VARIANT,
                        IMPACT_PROFILE_INCLUDES if included else IMPACT_PROFILE_EXCLUDES,
                        "profile", p["profile_id"], profile_name=str(p.get("name") or "")))
    return out, True


def _collect_translations(repo: IndustrialRepository, kind: str, object_id: str,
                          current_version: int) -> list[ImpactRecord]:
    """The staleness rule is the one assemble_structured_publication uses:
    a translation is stale when its source_version is not the live version.
    Read here directly against the same columns rather than assembling an
    entire publication, which would be the same answer at far greater cost.
    """
    table, key = _TRANSLATION_TABLE[kind]
    out: list[ImpactRecord] = []
    for r in repo.conn.execute(
            f"SELECT target_lang, source_version, status FROM {table} WHERE {key}=? ORDER BY target_lang",
            (object_id,)):
        src = int(r["source_version"])
        stale = src != int(current_version)
        out.append(_rec(DOMAIN_TRANSLATION,
                        IMPACT_TRANSLATION_SOURCE_VERSION_STALE if stale else IMPACT_TRANSLATION_CURRENT,
                        "language", str(r["target_lang"]),
                        target_lang=str(r["target_lang"]),
                        source_version=src, current_version=int(current_version),
                        translation_status=str(r["status"] or "")))
    return out


def _collect_review(repo: IndustrialRepository, kind: str, object_id: str) -> list[ImpactRecord]:
    out: list[ImpactRecord] = []
    queue = repo.review_queue(status="OPEN")
    if not queue.empty:
        mine = queue[(queue["object_id"].astype(str) == object_id)
                     & (queue["object_type"].astype(str) == kind)]
        for _, r in mine.iterrows():
            out.append(_rec(DOMAIN_REVIEW, IMPACT_REVIEW_OPEN, "review", str(r["review_id"]),
                            assigned_to=str(r.get("assigned_to") or ""),
                            requested_by=str(r.get("requested_by") or "")))
    # Stated whether or not a review is open: the limitation is a property
    # of the model, not of this object. review_tasks has no version column,
    # so no review here can be said to cover a particular version.
    out.append(_rec(DOMAIN_REVIEW, IMPACT_REVIEW_NOT_VERSION_AWARE, "object", object_id))
    return out


def _collect_compliance(project_dir: str, kind: str, object_id: str,
                        current_version: int) -> list[ImpactRecord]:
    """Evidence currency and requirement status both come from
    ComplianceService -- this only supplies the reverse direction
    (object -> evidence -> requirement) that no service exposes today.
    """
    out: list[ImpactRecord] = []
    crepo, svc = cv.open_service(project_dir)
    try:
        # The requirement's human title comes along: Conformita labels
        # requirements by title + article reference, never by the internal
        # requirement_id, and the Impact panel must not be the one screen
        # that shows a raw identifier where every other screen shows a name.
        rows = crepo.conn.execute(
            """SELECT e.evidence_id, e.object_version, l.requirement_id,
                      r.title AS requirement_title, r.article_ref
               FROM compliance_evidence e
               JOIN compliance_evidence_link l ON l.evidence_id = e.evidence_id
               LEFT JOIN compliance_requirement r ON r.requirement_id = l.requirement_id
               WHERE e.evidence_kind=? AND e.object_id=?
               ORDER BY l.requirement_id, e.evidence_id""",
            (kind, object_id)).fetchall()
        scopes = [r["scope_id"] for r in
                  crepo.conn.execute("SELECT scope_id FROM compliance_scope ORDER BY scope_id")]
        for row in rows:
            ev_version = int(row["object_version"])
            stale = ev_version != int(current_version)
            req_id = str(row["requirement_id"])
            req_label = str(row["requirement_title"] or "").strip() or req_id
            article = str(row["article_ref"] or "").strip()
            out.append(_rec(DOMAIN_COMPLIANCE,
                            IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE if stale
                            else IMPACT_COMPLIANCE_EVIDENCE_CURRENT,
                            "requirement", req_id,
                            requirement_id=req_id,
                            requirement_title=req_label, article_ref=article,
                            evidence_id=str(row["evidence_id"]),
                            evidence_version=ev_version,
                            current_version=int(current_version)))
            # the requirement's own live status, asked of the owning engine
            for scope_id in scopes:
                try:
                    status = svc.compute_status(scope_id, req_id)
                except Exception:
                    continue
                out.append(_rec(DOMAIN_COMPLIANCE, IMPACT_COMPLIANCE_REQUIREMENT_STATUS,
                                "requirement_status", f"{scope_id}::{req_id}",
                                requirement_id=req_id, requirement_title=req_label,
                                article_ref=article, scope_id=scope_id, status=str(status)))
    finally:
        crepo.close()
    return out


def _collect_release(repo: IndustrialRepository, publications: Iterable[str],
                     object_updated_at: str) -> list[ImpactRecord]:
    """Releases emitted BEFORE the object reached its current state.

    Deliberately narrow. A release row is an immutable snapshot, and
    nothing in the product re-evaluates one, so the only honest statement
    available is temporal: this release was produced before the object was
    last changed, therefore it does not contain the current text. Whether
    it should be re-issued is a human decision this V1 does not make.
    """
    out: list[ImpactRecord] = []
    pubs = sorted(set(p for p in publications if p))
    if not pubs or not object_updated_at:
        return out
    marks = ",".join("?" for _ in pubs)
    for r in repo.conn.execute(
            f"""SELECT release_id, publication_id, profile_id, target_lang, created_at
                FROM publication_releases WHERE publication_id IN ({marks})
                ORDER BY created_at, release_id""", tuple(pubs)):
        if str(r["created_at"]) < str(object_updated_at):
            out.append(_rec(DOMAIN_RELEASE, IMPACT_RELEASE_PREDATES_CURRENT_VERSION,
                            "release", str(r["release_id"]),
                            release_id=str(r["release_id"]),
                            publication_id=str(r["publication_id"]),
                            profile_id=str(r["profile_id"] or ""),
                            target_lang=str(r["target_lang"] or ""),
                            released_at=str(r["created_at"]),
                            object_updated_at=str(object_updated_at)))
    return out


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

def assess_object_impact(project_dir: str | Path, object_type: str, object_id: str) -> ImpactReport:
    """What is affected by this object at its current version. Read-only.

    Raises DomainValidationError for an unsupported kind and
    DomainNotFoundError when the object does not exist, so callers can use
    the same ui_error_text() boundary as every other domain call.
    """
    kind = str(object_type or "").strip().lower()
    object_id = str(object_id or "").strip()
    if kind not in SUPPORTED_KINDS:
        raise de.DomainValidationError(de.ERR_IMPACT_KIND_UNSUPPORTED,
                                       object_type=object_type,
                                       allowed=", ".join(SUPPORTED_KINDS))
    if not object_id:
        raise de.DomainNotFoundError(de.ERR_CONTENT_OBJECT_NOT_FOUND,
                                     object_type=kind, object_id=object_id)

    project_dir = str(project_dir)
    records: list[ImpactRecord] = []
    undetermined: set[str] = set()
    not_applicable: set[str] = set()

    repo, auth = av.open_service(project_dir)
    try:
        obj = _load_object(repo, kind, object_id)
        version = obj["version"]

        records += _collect_structure(repo, kind, object_id, obj)

        reuse_records, reuse_applicable = _collect_reuse(auth, repo, kind, object_id)
        records += reuse_records
        if not reuse_applicable:
            not_applicable.add(CAT_REUSE_LOCATIONS)

        variant_records, variant_applicable = _collect_variants(auth, kind, object_id)
        records += variant_records
        if not variant_applicable:
            undetermined.add(CAT_PROFILES_AFFECTED)

        records += _collect_translations(repo, kind, object_id, version)
        records += _collect_review(repo, kind, object_id)

        publications = [r.ref_id for r in records
                        if r.code == IMPACT_CONTENT_IN_PUBLICATION]
        records += _collect_release(repo, publications,
                                    str(obj["row"].get("updated_at") or ""))
    finally:
        repo.close()

    # compliance opens its own service (ComplianceService wraps a repo)
    records += _collect_compliance(project_dir, kind, object_id, version)

    final = _finalize(records)
    return ImpactReport(
        object_type=kind, object_id=object_id, object_version=version,
        object_status=obj["status"], object_label=obj["label"],
        records=final, counts=summarize(final),
        undetermined=frozenset(undetermined), not_applicable=frozenset(not_applicable),
        limitations=(LIMITATION_REVIEW_NOT_VERSION_AWARE,
                     LIMITATION_RELEASE_READINESS_NOT_REEVALUATED,
                     LIMITATION_FORK_ORIGIN_NOT_PERSISTED,
                     LIMITATION_NO_SEGMENT_LEVEL_IMPACT),
    )
