"""Impact Center V1 — one answer to "what did this change break?".

An ORCHESTRATOR, not an engine. It calls two services that already exist
and already own their rules, adds the one fact neither of them reports
(who last changed the object, and when), and groups the result the way a
customer thinks about it rather than the way the schema stores it.

    impact_intelligence.assess_object_impact()
        publications, topics, reuse, variants, review, compliance,
        releases that predate the current version
    translation_intelligence.assess_translation_intelligence()
        per-language FRESH / STALE / MISSING, and on request the
        normalised reasons a memory reuse was refused

Nothing about staleness, coverage, reuse or variants is decided here.
Every verdict is the owning service's; this module arranges them.

On release readiness
--------------------
This does NOT call release_readiness.assess_release_readiness(), and the
reason is a constraint rather than a preference: that function
constructs a WorkflowManager, whose __init__ CREATES
ManualSync_Workflow.sqlite and an assets directory on a project that has
never used workflows. This view must not write anything, so it cannot
call it.

What it reports instead is narrower and honestly named: the release
impact OF THIS CHANGE -- stale translations, reopened requirements and
unreviewed content attributable to this object. The full publication
readiness audit stays where it belongs, in Rilascio. Answering "is the
whole manual releasable" here would duplicate an engine this gate is
told not to duplicate.

Read-only
---------
Both composed services are read-only, and this adds only SELECTs. No
table is created, no row written, no version bumped, no audit event
emitted, no file left behind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import project_services as ps
import impact_intelligence as ii
import translation_intelligence as ti
from ccms_industrial import IndustrialRepository
from project_layout import ProjectLayout

SUPPORTED_KINDS = ii.SUPPORTED_KINDS

# Actions that represent a human changing the content, as opposed to
# reading it or moving it. Used to answer "last modified by".
_MODIFYING_ACTIONS = ("SAVE", "CREATE", "REVERT", "RESTORE", "ARCHIVE",
                      "AUTHORING_SAVE", "AUTHORING_CREATE", "AUTHORING_REVERT")

# ----------------------------------------------------------------------
# Attention taxonomy -- the three buckets the view presents
# ----------------------------------------------------------------------
BUCKET_CHANGES = "changes"        # what this object touches
BUCKET_ATTENTION = "attention"    # what a human must now look at
BUCKET_READY = "ready"            # what is already fine

# Release-impact reasons, as codes
RELEASE_BLOCKED_STALE_TRANSLATIONS = "RELEASE_BLOCKED_STALE_TRANSLATIONS"
RELEASE_BLOCKED_MISSING_TRANSLATIONS = "RELEASE_BLOCKED_MISSING_TRANSLATIONS"
RELEASE_BLOCKED_REQUIREMENTS_REOPENED = "RELEASE_BLOCKED_REQUIREMENTS_REOPENED"
RELEASE_BLOCKED_CONTENT_NOT_REVIEWED = "RELEASE_BLOCKED_CONTENT_NOT_REVIEWED"
RELEASE_BLOCKED_OPEN_REVIEW = "RELEASE_BLOCKED_OPEN_REVIEW"
RELEASE_READY_FOR_THIS_CHANGE = "RELEASE_READY_FOR_THIS_CHANGE"

# A content object is considered reviewed once it reaches one of these.
_REVIEWED_STATUSES = frozenset({"Approved", "Published"})


@dataclass(frozen=True)
class Ref:
    """One named thing this change touches. Never a bare identifier."""
    kind: str
    ref_id: str
    label: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref_id": self.ref_id,
                "label": self.label, "detail": self.detail}


@dataclass(frozen=True)
class Note:
    """One coded statement, rendered by the view through the catalog."""
    code: str
    params: tuple[tuple[str, Any], ...] = ()

    @property
    def param_dict(self) -> dict[str, Any]:
        return dict(self.params)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "params": self.param_dict}


def _note(code: str, **params: Any) -> Note:
    return Note(code=code, params=tuple(sorted((str(k), v) for k, v in params.items())))


@dataclass(frozen=True)
class ImpactCenterReport:
    # 1 -- the changed content
    object_type: str
    object_id: str
    object_label: str
    object_version: int
    object_status: str
    previous_version: int | None
    last_modified_at: str
    last_modified_by: str

    # 2 -- documentation impact
    publications: tuple[Ref, ...]
    topics: tuple[Ref, ...]
    reuse_locations: tuple[Ref, ...]
    profiles: tuple[Ref, ...]
    reuse_supported: bool
    profiles_determinable: bool

    # 3 -- translation impact
    languages: tuple[ti.LanguageStatus, ...]
    translation_reasons: tuple[ti.ReuseReason, ...]
    translation_deep_state: str
    # Whether the user ASKED for the translation analysis. Without this the
    # view cannot tell "not requested" from "requested and unavailable",
    # and the second case rendered as nothing at all: ticking the box
    # appeared to do nothing on Draft content, which is precisely when a
    # person most needs to be told why.
    translation_detail_requested: bool

    # 4 -- compliance impact
    requirements: tuple[Ref, ...]
    requirements_reopened: tuple[Ref, ...]

    # 5 -- release impact of this change
    release_ready: bool
    release_notes: tuple[Note, ...]

    # the underlying reports, for the advanced section
    impact: ii.ImpactReport
    translation: ti.TranslationReport

    @property
    def attention_count(self) -> int:
        return (sum(1 for l in self.languages if l.status == ti.STATUS_STALE)
                + len(self.requirements_reopened)
                + (0 if self.object_status in _REVIEWED_STATUSES else 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type, "object_id": self.object_id,
            "object_label": self.object_label, "object_version": self.object_version,
            "object_status": self.object_status, "previous_version": self.previous_version,
            "last_modified_at": self.last_modified_at, "last_modified_by": self.last_modified_by,
            "publications": [r.to_dict() for r in self.publications],
            "topics": [r.to_dict() for r in self.topics],
            "reuse_locations": [r.to_dict() for r in self.reuse_locations],
            "profiles": [r.to_dict() for r in self.profiles],
            "reuse_supported": self.reuse_supported,
            "profiles_determinable": self.profiles_determinable,
            "languages": [l.to_dict() for l in self.languages],
            "translation_reasons": [r.to_dict() for r in self.translation_reasons],
            "translation_deep_state": self.translation_deep_state,
            "translation_detail_requested": self.translation_detail_requested,
            "requirements": [r.to_dict() for r in self.requirements],
            "requirements_reopened": [r.to_dict() for r in self.requirements_reopened],
            "release_ready": self.release_ready,
            "release_notes": [n.to_dict() for n in self.release_notes],
        }


# ----------------------------------------------------------------------
# The one fact neither service reports
# ----------------------------------------------------------------------

def _last_modification(repo: IndustrialRepository, object_id: str) -> tuple[str, str]:
    """(when, who) of the last human change, from the audit trail.

    content_blocks has no author column and block_versions has none
    either, so the only record of WHO is audit_events. Reading it here is
    composition, not a new model.
    """
    row = repo.conn.execute(
        "SELECT actor, action, created_at FROM audit_events WHERE object_id=? "
        "ORDER BY event_id DESC", (object_id,)).fetchall()
    for r in row:
        if any(tok in str(r["action"] or "").upper() for tok in _MODIFYING_ACTIONS):
            return str(r["created_at"] or ""), str(r["actor"] or "")
    if row:
        return str(row[0]["created_at"] or ""), str(row[0]["actor"] or "")
    return "", ""


def _previous_version(repo: IndustrialRepository, kind: str, object_id: str,
                      current: int) -> int | None:
    """The version immediately before the current one, if one is stored."""
    table_map = {"topic": ("topic_versions", "topic_id"),
                 "block": ("block_versions", "block_id"),
                 "table": ("structured_table_versions", "table_id"),
                 "figure": ("figure_versions", "figure_id")}
    table, key = table_map[kind]
    r = repo.conn.execute(
        f"SELECT MAX(version) v FROM {table} WHERE {key}=? AND version < ?",
        (object_id, int(current))).fetchone()
    return int(r["v"]) if r and r["v"] is not None else None


# ----------------------------------------------------------------------
# Regrouping the two reports
# ----------------------------------------------------------------------

def _refs_from_impact(report: ii.ImpactReport) -> dict[str, tuple[Ref, ...]]:
    pubs, topics, reuse, profiles, reqs, reopened = [], [], [], [], [], []
    for r in report.records:
        p = r.param_dict
        if r.code == ii.IMPACT_CONTENT_IN_PUBLICATION:
            pubs.append(Ref("publication", r.ref_id,
                            str(p.get("publication_title") or r.ref_id)))
        elif r.code == ii.IMPACT_CONTENT_IN_TOPIC:
            topics.append(Ref("topic", r.ref_id, str(p.get("topic_title") or r.ref_id)))
        elif r.code == ii.IMPACT_CONTENT_REUSED_IN_PLACEMENT:
            reuse.append(Ref("placement", r.ref_id,
                             str(p.get("topic_title") or r.ref_id)))
        elif r.code == ii.IMPACT_PROFILE_INCLUDES:
            profiles.append(Ref("profile", r.ref_id, str(p.get("profile_name") or r.ref_id)))
        elif r.code in (ii.IMPACT_COMPLIANCE_EVIDENCE_CURRENT,
                        ii.IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE):
            ref = Ref("requirement", r.ref_id,
                      str(p.get("requirement_title") or r.ref_id),
                      detail=str(p.get("article_ref") or ""))
            reqs.append(ref)
            if r.code == ii.IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE:
                reopened.append(ref)

    def uniq(items: list[Ref]) -> tuple[Ref, ...]:
        seen, out = set(), []
        for it in items:
            if it.ref_id in seen:
                continue
            seen.add(it.ref_id)
            out.append(it)
        return tuple(sorted(out, key=lambda r: (r.label.casefold(), r.ref_id)))

    return {"publications": uniq(pubs), "topics": uniq(topics), "reuse": uniq(reuse),
            "profiles": uniq(profiles), "requirements": uniq(reqs),
            "reopened": uniq(reopened)}


def _release_impact(languages: tuple[ti.LanguageStatus, ...],
                    reopened: tuple[Ref, ...], object_status: str,
                    open_reviews: int) -> tuple[bool, tuple[Note, ...]]:
    """Release impact OF THIS CHANGE. Deliberately not a readiness audit.

    Each blocker corresponds to something this object actually caused, so
    the answer is attributable: a user can act on every line.
    """
    notes: list[Note] = []
    stale = [l for l in languages if l.status == ti.STATUS_STALE]
    missing = [l for l in languages if l.status == ti.STATUS_MISSING]
    if stale:
        notes.append(_note(RELEASE_BLOCKED_STALE_TRANSLATIONS, n=len(stale),
                           languages=", ".join(l.language for l in stale)))
    if missing:
        notes.append(_note(RELEASE_BLOCKED_MISSING_TRANSLATIONS, n=len(missing),
                           languages=", ".join(l.language for l in missing)))
    if reopened:
        notes.append(_note(RELEASE_BLOCKED_REQUIREMENTS_REOPENED, n=len(reopened)))
    if object_status not in _REVIEWED_STATUSES:
        notes.append(_note(RELEASE_BLOCKED_CONTENT_NOT_REVIEWED, status=object_status))
    if open_reviews:
        notes.append(_note(RELEASE_BLOCKED_OPEN_REVIEW, n=open_reviews))

    ready = not notes
    if ready:
        notes.append(_note(RELEASE_READY_FOR_THIS_CHANGE))
    return ready, tuple(notes)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

def assess_impact_center(project_dir: str | Path, object_id: str,
                         object_type: str | None = None,
                         include_translation_detail: bool = False) -> ImpactCenterReport:
    """Everything the Impact Center shows, for one changed object.

    `include_translation_detail` additionally runs the translation impact
    analysis, which is what fills in the reasons a memory reuse was
    refused. It is off by default because it runs the CAT engine.
    """
    project_dir = Path(str(project_dir))
    object_id = str(object_id or "").strip()

    # Resolve the kind once, through the translation service's prober, so
    # both underlying calls agree on what they are looking at.
    repo, _auth = ps.open_authoring_service(str(project_dir))
    try:
        kind = (str(object_type).strip().lower() if object_type
                else ti.detect_object_type(repo, object_id))
    finally:
        repo.close()
    if kind is None:
        import domain_errors as de
        raise de.DomainNotFoundError(de.ERR_CONTENT_OBJECT_NOT_FOUND,
                                     object_type="", object_id=object_id)

    impact = ii.assess_object_impact(project_dir, kind, object_id)
    translation = ti.assess_translation_intelligence(
        project_dir, object_id, object_type=kind, deep=include_translation_detail)

    repo = IndustrialRepository(ProjectLayout(project_dir).ccms_db)
    try:
        when, who = _last_modification(repo, object_id)
        previous = _previous_version(repo, kind, object_id, impact.object_version)
    finally:
        repo.close()

    refs = _refs_from_impact(impact)
    reasons: list[ti.ReuseReason] = []
    for lang_impact in translation.tier2:
        reasons.extend(lang_impact.reasons)

    open_reviews = impact.counts.get(ii.CAT_OPEN_REVIEWS, 0) or 0
    ready, notes = _release_impact(translation.languages, refs["reopened"],
                                   impact.object_status, open_reviews)

    return ImpactCenterReport(
        object_type=kind, object_id=object_id, object_label=impact.object_label,
        object_version=impact.object_version, object_status=impact.object_status,
        previous_version=previous, last_modified_at=when, last_modified_by=who,
        publications=refs["publications"], topics=refs["topics"],
        reuse_locations=refs["reuse"], profiles=refs["profiles"],
        reuse_supported=impact.state_of(ii.CAT_REUSE_LOCATIONS) == ii.STATE_KNOWN,
        profiles_determinable=impact.state_of(ii.CAT_PROFILES_AFFECTED) == ii.STATE_KNOWN,
        languages=translation.languages, translation_reasons=tuple(reasons),
        translation_deep_state=translation.tier2_state,
        translation_detail_requested=bool(include_translation_detail),
        requirements=refs["requirements"], requirements_reopened=refs["reopened"],
        release_ready=ready, release_notes=notes,
        impact=impact, translation=translation)


def changed_objects(project_dir: str | Path, limit: int = 40) -> list[tuple[str, str]]:
    """(label, object_id) of recently changed content, newest first.

    The picker's contents. Built from audit_events so the list is exactly
    "what somebody has been working on", which is how a user thinks about
    finding the change they want to inspect -- not an alphabetical dump of
    every object in the manual.
    """
    repo = IndustrialRepository(ProjectLayout(str(project_dir)).ccms_db)
    out: list[tuple[str, str]] = []
    try:
        seen: set[str] = set()
        for r in repo.conn.execute(
                "SELECT object_id, object_type, MAX(event_id) e FROM audit_events "
                "WHERE object_type IN ('topic','block','table','figure') "
                "GROUP BY object_id ORDER BY e DESC"):
            oid = str(r["object_id"] or "")
            if not oid or oid in seen:
                continue
            kind = str(r["object_type"] or "")
            if kind not in SUPPORTED_KINDS:
                continue
            table, key, title_col = ii._LIVE_TABLE[kind]
            row = repo.conn.execute(
                f"SELECT * FROM {table} WHERE {key}=?", (oid,)).fetchone()
            if not row:
                continue
            data = dict(row)
            label = str(data.get(title_col) or "") if title_col else ""
            if not label:
                text = str(data.get("text_content") or "").strip()
                label = (text[:58] + "…") if len(text) > 58 else text
            seen.add(oid)
            out.append((f"{label}  —  v{data.get('version') or 1}", oid))
            if len(out) >= limit:
                break
    finally:
        repo.close()
    return out
