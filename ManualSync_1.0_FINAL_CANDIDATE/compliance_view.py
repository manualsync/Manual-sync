"""Milestone 3.5B: view-model adapter between ComplianceService
(compliance_intelligence.py) and the Gradio UI in app.py.

Pure Python -- no gradio import here, so every function is testable without
a UI runtime. This module never duplicates a business rule: the STATUS of a
requirement always comes from ComplianceService.compute_status(); the
COUNTS always come from ComplianceService.summary_for_scope(); a decision
is always validated by ComplianceService itself (set_applicability/
set_assessment/add_evidence/...), never re-validated here. The only thing
this module adds is presentation: friendly labels, an essential "why" for a
requirement's current status (re-reading the same signals compute_status()
already consulted, purely to explain, never to override), sort order, and
assembling read-only getters into one screen's worth of data.

CCMS_OBJECT candidate browsing (for "link an existing CCMS object as
evidence") reads topics/content_blocks/structured_tables/figures directly
via the shared IndustrialRepository connection -- a plain SELECT for
display, not a new business rule.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import domain_errors as de
from domain_errors import DomainFileMissingError, DomainValidationError
from ccms_industrial import IndustrialRepository
from compliance_intelligence import ComplianceService
import compliance_starter_packs as csp
import i18n

# Decision vocabularies. The CODES are the engine's persisted enums and are
# the values the UI actually carries: a Gradio Radio takes (label, value)
# pairs, so the widget shows a word and submits a code.
#
# The reverse mapping this replaces -- label -> code -- could not survive a
# language switch: the radio would show "Applies" while the lookup table
# only knew "Si applica", and a decision would silently fail to record.
# Translated text must never be a key into anything.
APPLICABILITY_CODES = ("APPLIES", "NOT_APPLICABLE", "TO_VERIFY")
COVERAGE_CODES = ("COVERED", "TO_VERIFY", "MISSING")

_APPLICABILITY_KEYS = {"APPLIES": "applicability.applies",
                       "NOT_APPLICABLE": "applicability.not_applicable",
                       "TO_VERIFY": "applicability.to_verify"}
_COVERAGE_KEYS = {"COVERED": "coverage.covered",
                  "TO_VERIFY": "coverage.to_verify",
                  "MISSING": "coverage.missing"}


def applicability_label(code: str, lang: str | None = None) -> str:
    key = _APPLICABILITY_KEYS.get(str(code or ""))
    return i18n.translate(key, lang) if key else str(code or "")


def coverage_label(code: str, lang: str | None = None) -> str:
    key = _COVERAGE_KEYS.get(str(code or ""))
    return i18n.translate(key, lang) if key else str(code or "")


def applicability_choices(lang: str | None = None) -> list[tuple[str, str]]:
    """(label, code) pairs for a Radio. Gradio submits the code."""
    return [(applicability_label(c, lang), c) for c in APPLICABILITY_CODES]


def coverage_choices(lang: str | None = None) -> list[tuple[str, str]]:
    return [(coverage_label(c, lang), c) for c in COVERAGE_CODES]


def applicability_labels(lang: str | None = None) -> dict[str, str]:
    return {c: applicability_label(c, lang) for c in APPLICABILITY_CODES}


def coverage_labels(lang: str | None = None) -> dict[str, str]:
    return {c: coverage_label(c, lang) for c in COVERAGE_CODES}

# Requirement list order: what needs a decision first floats to the top.
_STATUS_SORT_KEY = {"Missing": 0, "To verify": 1, "Covered": 2, "Not applicable": 3}

# PRESENTATION ONLY. compute_status() keeps returning its own vocabulary
# and the persisted enum (COVERED / TO_VERIFY / MISSING / NOT_APPLICABLE)
# is untouched -- this maps the engine's words to the language the user
# reads, at the last possible moment. An Italian product must not show an
# Italian user "To verify" next to "Da valutare" and expect them to
# understand these are the same kind of thing.
# Engine word -> catalog key. The LEFT side is the engine's stable
# vocabulary and never moves; only the right side is looked up per
# language. Nothing here is ever used as a dictionary key, a query value
# or a comparison target -- these are display words, produced last.
_STATUS_KEYS = {
    "Covered": "status.covered",
    "To verify": "status.to_verify",
    "Missing": "status.missing",
    "Not applicable": "status.not_applicable",
}

_SCOPE_STATUS_KEYS = {"ACTIVE": "status.active", "ARCHIVED": "status.archived",
                      "DRAFT": "status.draft"}


def status_labels(lang: str | None = None) -> dict[str, str]:
    """Engine status -> label, in one language."""
    return {status: i18n.translate(key, lang) for status, key in _STATUS_KEYS.items()}


def scope_status_labels(lang: str | None = None) -> dict[str, str]:
    return {code: i18n.translate(key, lang) for code, key in _SCOPE_STATUS_KEYS.items()}


def status_label(status: str, lang: str | None = None) -> str:
    """The label for an engine status. Unknown values pass through
    unchanged rather than being swallowed -- a status nobody translated
    should be visible, not silently blank."""
    key = _STATUS_KEYS.get(str(status or ""))
    return i18n.translate(key, lang) if key else str(status or "")

_EVIDENCE_KINDS = ("topic", "block", "table", "figure", "external_file",
                   "customer_document", "human_attestation")


def evidence_kind_labels(lang: str | None = None) -> dict[str, str]:
    return {k: i18n.translate(f"evidence.kind.{k}", lang) for k in _EVIDENCE_KINDS}


def ccms_db_path(project_dir: str | Path) -> Path:
    base = Path(project_dir)
    for name in ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"):
        p = base / name
        if p.exists():
            return p
    raise DomainFileMissingError(de.ERR_PROJECT_REPOSITORY_MISSING, project_dir=str(base))


def open_service(project_dir: str | Path) -> tuple[IndustrialRepository, ComplianceService]:
    repo = IndustrialRepository(ccms_db_path(project_dir))
    return repo, ComplianceService(repo)


def scope_options(svc: ComplianceService, publication_id: str) -> list[tuple[str, str]]:
    """(human label, scope_id) pairs for a scope picker, any status --
    letting the user SEE an archived scope is more honest than hiding it.
    Gradio's Dropdown convention for tuple choices is (label, value), in
    that order -- keep it that way, a swap here silently breaks value
    matching (Gradio raises "value not in choices")."""
    out = []
    for r in svc.scopes_for_publication(publication_id):
        name = r["title"] or r["jurisdiction"] or "Perimetro normativo"
        bits = [name]
        if r["jurisdiction"] and r["title"]:
            bits.append(str(r["jurisdiction"]))
        _code = str(r["status"] or "")
        status = (i18n.t(_SCOPE_STATUS_KEYS[_code]) if _code in _SCOPE_STATUS_KEYS
                  else _code)
        if status and str(r["status"]) != "ACTIVE":
            # Only a NON-active scope is worth annotating: tagging every
            # normal scope "(attivo)" is noise, but hiding that one is
            # archived would be a trap.
            bits.append(status)
        # The scope_id stays the VALUE (Gradio matches on it, and every
        # callback keys off it) but is no longer the label: an internal
        # identifier is not a name a user should have to read.
        out.append((" · ".join(bits), r["scope_id"]))
    return out


# PRESENTATION ONLY. The stored `official_identifier` is the instrument's
# canonical identifier and never changes -- it is what a pack declares and
# what provenance is traced against. This maps the EU act types to the
# Italian citation form the rest of the reference is already written in,
# so a requirement does not read "Regulation (EU) 2023/1230 Articolo 10,
# paragrafo 8" -- half English, half Italian, in an Italian product.
_ACT_TYPE_IT = (
    ("Regulation (EU)", "Regolamento (UE)"),
    ("Regulation (EC)", "Regolamento (CE)"),
    ("Directive (EU)", "Direttiva (UE)"),
    ("Directive (EC)", "Direttiva (CE)"),
)


def instrument_label(official_identifier: str) -> str:
    """The Italian display form of an instrument identifier.

    Unknown identifiers pass through unchanged rather than being guessed
    at: showing a citation nobody can find in the Gazzetta would be worse
    than showing it in English."""
    text = str(official_identifier or "")
    for en, it in _ACT_TYPE_IT:
        if text.startswith(en):
            return it + text[len(en):]
    return text


def _requirement_reference(svc: ComplianceService, req: dict[str, Any]) -> str:
    if not req.get("instrument_id"):
        return req.get("article_ref") or "—"
    inst = svc.instrument_row(req["instrument_id"])
    ident = instrument_label(inst["official_identifier"]) if inst else req["instrument_id"]
    art = req.get("article_ref") or ""
    return f"{ident} {art}".strip()


def _explain_status(svc: ComplianceService, scope_id: str, requirement_id: str, status: str) -> str:
    """Essential, human-readable reason for the CURRENT status. Re-reads the
    same rows compute_status() already consulted, purely to explain -- it
    never decides the status itself."""
    if status == "Covered":
        n = len(svc.evidence_for_requirement(requirement_id))
        return (i18n.t("reason.evidence.one", n=n) if n == 1
                else i18n.t("reason.evidence.many", n=n))
    if status == "Not applicable":
        ap = svc.applicability_row(scope_id, requirement_id)
        return (ap or {}).get("justification") or ""
    if status == "Missing":
        asm = svc.assessment_row(scope_id, requirement_id)
        comment = (asm or {}).get("comment") or ""
        return comment.strip() or i18n.t("reason.no_evidence")

    # "To verify" -- explain WHY, in the same order compute_status() checks.
    req = svc.requirement_row(requirement_id)
    ap = svc.applicability_row(scope_id, requirement_id)
    if not ap:
        return i18n.t("reason.applicability.not_assessed")
    if int(ap["requirement_version"]) != int(req["version"]):
        return i18n.t("reason.requirement_changed")
    if ap["applies"] == "TO_VERIFY":
        return i18n.t("reason.applicability.unconfirmed")
    if ap["applies"] == "APPLIES" and not svc.requirement_is_current(requirement_id):
        return i18n.t("reason.instrument_updated")
    asm = svc.assessment_row(scope_id, requirement_id)
    if not asm:
        return i18n.t("reason.coverage.not_assessed")
    if int(asm["requirement_version_at_assessment"]) != int(req["version"]):
        return i18n.t("reason.requirement_changed_after_assessment")
    if asm["status"] == "COVERED":
        return i18n.t("reason.evidence_stale")
    return i18n.t("status.to_verify")


# ---------------------------------------------------------------------
# 4F.2: "Copertura documentale" and "Da completare"
#
# Deliberately NOT called conformity anywhere. These functions report
# DOCUMENTATION COVERAGE -- which obligations have linked evidence and
# which still need work -- and keep three different things visibly
# separate, because collapsing them is exactly how a compliance tool
# starts lying to its user:
#   1. the legal source requirement (from the curated pack),
#   2. the human APPLICABILITY decision for this project,
#   3. the EVIDENCE coverage state.
# No function here ever concludes that anything is legally compliant.
# ---------------------------------------------------------------------

# The bucket IDENTIFIERS are internal and stable; only their labels are
# localized. _coverage_bucket() returns one of these strings and callers
# compare against them, so they must never become translated text.
BUCKET_KEYS = ("covered", "partial", "no_evidence", "not_applicable", "unassessed")


def bucket_labels(lang: str | None = None) -> dict[str, str]:
    return {k: i18n.translate(f"conformity.bucket.{k}", lang) for k in BUCKET_KEYS}


def _coverage_bucket(svc: ComplianceService, scope_id: str, requirement_id: str,
                     status: str) -> tuple[str, int]:
    """The ONE place a requirement is placed into a coverage bucket.

    Buckets are mutually exclusive and derived, never stored: applicability
    is asked first (an undecided requirement is "da valutare", never
    "missing"), then evidence. "Copertura parziale" is a real, derivable
    state -- evidence has been linked but no human has declared the
    requirement covered -- so it needs no new stored status and cannot be
    set automatically."""
    evidence_count = len(svc.evidence_for_requirement(requirement_id))
    ap = svc.applicability_row(scope_id, requirement_id)
    if not ap or ap["applies"] == "TO_VERIFY":
        return "unassessed", evidence_count
    if ap["applies"] == "NOT_APPLICABLE":
        return "not_applicable", evidence_count
    if status == "Covered":
        return "covered", evidence_count
    if evidence_count:
        return "partial", evidence_count
    return "no_evidence", evidence_count


def documentation_coverage(svc: ComplianceService, scope_id: str | None) -> dict[str, Any]:
    """Counts for the "Copertura documentale" view.

    The ratio is expressed over APPLICABLE requirements only (a
    requirement a human ruled out cannot sensibly count against
    coverage), and is labelled "Requisiti documentali con evidenza" --
    never a conformity percentage."""
    if not scope_id:
        return {"has_scope": False, "title": i18n.t("conformity.coverage.view_title")}
    buckets = {k: 0 for k in BUCKET_KEYS}
    needs_review = 0
    unverified = 0
    rows = []
    for rid in svc.requirements_in_scope(scope_id):
        status = svc.compute_status(scope_id, rid)
        bucket, evidence_count = _coverage_bucket(svc, scope_id, rid, status)
        buckets[bucket] += 1
        # A requirement whose legal wording has not been verified against
        # the official source must never pass silently as authoritative.
        # It is counted separately so the UI can show it, and the caller
        # can tell whether this scope's numbers rest entirely on verified
        # legal sources or not.
        req_row = svc.requirement_row(rid) or {}
        if (req_row.get("verification_status") or "") != "VERIFIED_OFFICIAL":
            unverified += 1
        # "Da rivedere" means a decision EXISTS but the engine downgraded
        # it (the requirement or its evidence moved underneath it). A
        # requirement nobody has assessed yet is already counted as "Da
        # valutare" -- counting it here too would make a freshly imported
        # pack read as though 30 decisions needed revisiting.
        if status == "To verify" and bucket != "unassessed":
            needs_review += 1
        rows.append({"requirement_id": rid, "status": status, "bucket": bucket,
                     "evidence_count": evidence_count})
    total = len(rows)
    applicable = buckets["covered"] + buckets["partial"] + buckets["no_evidence"]
    assessed = total - buckets["unassessed"]
    ratio = round(100 * buckets["covered"] / applicable) if applicable else None
    return {
        "has_scope": True,
        "title": i18n.t("conformity.coverage.view_title"),
        "scope": svc.scope_row(scope_id),
        "total": total,
        "assessed": assessed,
        "not_assessed": buckets["unassessed"],
        "applicable": applicable,
        "not_applicable": buckets["not_applicable"],
        "covered": buckets["covered"],
        "partial": buckets["partial"],
        "no_evidence": buckets["no_evidence"],
        "needs_review": needs_review,
        "unverified_requirements": unverified,
        "all_sources_verified": unverified == 0,
        "ratio_label": i18n.t("conformity.coverage.ratio_label"),
        "ratio_percent": ratio,
        "rows": rows,
    }


NEXT_STEP_KEYS = ("unassessed", "no_evidence", "partial", "review")


def next_step_label(step_key: str, lang: str | None = None) -> str:
    """The suggested next step. An unrecognised key falls back to "review",
    which is the only advice that is always safe: look at it again."""
    key = step_key if step_key in NEXT_STEP_KEYS else "review"
    return i18n.translate(f"conformity.next_step.{key}", lang)


def to_complete_rows(svc: ComplianceService, scope_id: str | None) -> list[dict[str, Any]]:
    """The "Da completare" working list -- the demo's key screen.

    Surfaces exactly what still needs a human: applicable requirements
    with no evidence, partially covered ones, requirements nobody has
    assessed yet, and anything the engine downgraded to "To verify"
    because the requirement or its evidence moved underneath a previous
    decision. Requirements ruled NOT APPLICABLE are excluded -- a human
    already decided them, so they are not gaps.

    Every row answers the five questions the ticket asks for: cosa
    richiede, riferimento ufficiale, stato nel progetto, evidenza
    collegata, prossimo passo. No row ever carries a legal conclusion."""
    if not scope_id:
        return []
    out = []
    for rid in svc.requirements_in_scope(scope_id):
        status = svc.compute_status(scope_id, rid)
        bucket, evidence_count = _coverage_bucket(svc, scope_id, rid, status)
        if bucket in ("covered", "not_applicable") and status != "To verify":
            continue
        if bucket == "not_applicable":
            continue
        req = svc.requirement_row(rid)
        step_key = "review" if (status == "To verify" and bucket not in ("unassessed",)) else bucket
        out.append({
            "requirement_id": rid,
            "title": req["title"],
            "summary": req.get("description") or "",
            "reference": _requirement_reference(svc, req),
            "source_url": req.get("source_url") or "",
            "status": status,
            "bucket": bucket,
            "bucket_label": i18n.t(f"conformity.bucket.{bucket}"),
            "evidence_count": evidence_count,
            "reason": _explain_status(svc, scope_id, rid, status),
            "next_step": next_step_label(step_key),
        })
    order = {"no_evidence": 0, "partial": 1, "unassessed": 2}
    out.sort(key=lambda r: (order.get(r["bucket"], 9), r["title"]))
    return out


def requirement_provenance(svc: ComplianceService, requirement_id: str) -> dict[str, Any]:
    """Where this requirement came from -- the answer to "da dove arriva
    questo requisito?". Returns human-facing provenance only; the caller
    never has to read raw columns."""
    req = svc.requirement_row(requirement_id)
    if not req:
        raise KeyError(requirement_id)
    inst = svc.instrument_row(req["instrument_id"]) if req.get("instrument_id") else None
    verification = req.get("verification_status") or ""
    return {
        "reference": _requirement_reference(svc, req),
        "article_ref": req.get("article_ref") or "",
        "instrument_title": (inst or {}).get("title") or "",
        "official_identifier": (inst or {}).get("official_identifier") or "",
        "applies_from": (inst or {}).get("valid_from") or "",
        "source_url": req.get("source_url") or (inst or {}).get("official_source_ref") or "",
        "source_lang": req.get("source_lang") or "",
        "reviewed_date": req.get("reviewed_date") or "",
        "pack_id": req.get("pack_id") or "",
        "pack_version": req.get("pack_version") or "",
        "provenance_type": req.get("provenance_type") or "",
        "verification_status": verification,
        "verified": verification == "VERIFIED_OFFICIAL",
    }


def dashboard(svc: ComplianceService, scope_id: str | None) -> dict[str, Any]:
    """The 4 guided counts + staleness signals, straight from
    summary_for_scope() -- never recomputed here. has_scope=False is the
    honest empty state: no active evaluation exists, not "0 requirements =
    all good"."""
    if not scope_id:
        return {"has_scope": False}
    summary = svc.summary_for_scope(scope_id)
    return {"has_scope": True, "scope": svc.scope_row(scope_id), **summary}


def requirement_list_rows(svc: ComplianceService, scope_id: str) -> list[dict[str, Any]]:
    rows = []
    for rid in svc.requirements_in_scope(scope_id):
        req = svc.requirement_row(rid)
        status = svc.compute_status(scope_id, rid)
        rows.append({
            "requirement_id": rid,
            "title": req["title"],
            "reference": _requirement_reference(svc, req),
            # Short form for a LIST scoped to one instrument: repeating
            # "Regolamento (UE) 2023/1230" on all 41 rows spends the whole
            # column on a constant and pushes every row to three lines.
            # The full citation stays on the requirement detail.
            "reference_short": req.get("article_ref") or _requirement_reference(svc, req),
            "status": status,
            "reason": _explain_status(svc, scope_id, rid, status),
            "has_evidence": bool(svc.evidence_for_requirement(rid)),
        })
    rows.sort(key=lambda r: (_STATUS_SORT_KEY.get(r["status"], 9), r["title"]))
    return rows


def requirement_detail(svc: ComplianceService, scope_id: str, requirement_id: str) -> dict[str, Any]:
    req = svc.requirement_row(requirement_id)
    if not req:
        raise KeyError(requirement_id)
    status = svc.compute_status(scope_id, requirement_id)
    ap = svc.applicability_row(scope_id, requirement_id)
    return {
        "requirement": req,
        "reference": _requirement_reference(svc, req),
        "status": status,
        "reason": _explain_status(svc, scope_id, requirement_id, status),
        "requirement_is_current": svc.requirement_is_current(requirement_id),
        "instrument": svc.instrument_row(req["instrument_id"]) if req.get("instrument_id") else None,
        "standards": svc.standards_for_requirement(requirement_id),
        "applicability": ap,
        "can_decide_coverage": bool(ap and ap["applies"] == "APPLIES"),
        "assessment": svc.assessment_row(scope_id, requirement_id),
        "evidence": svc.evidence_for_requirement(requirement_id),
    }


def evidence_candidates(repo: IndustrialRepository) -> list[dict[str, Any]]:
    """Readable list of existing CCMS objects (topic/block/table/figure) a
    human can pick from to link as evidence. Plain SELECT for display --
    not a new business rule."""
    out: list[dict[str, Any]] = []
    for r in repo.conn.execute("SELECT topic_id,title,version,status FROM topics ORDER BY topic_id"):
        out.append({"kind": "topic", "object_id": r["topic_id"], "version": int(r["version"]),
                    "label": f"Sezione — {r['title']} ({r['status']})"})
    for r in repo.conn.execute("SELECT block_id,block_type,text_content,version,status FROM content_blocks ORDER BY block_id"):
        preview = (r["text_content"] or "").strip().replace("\n", " ")[:70]
        out.append({"kind": "block", "object_id": r["block_id"], "version": int(r["version"]),
                    "label": f"Blocco ({r['block_type']}) — {preview or r['block_id']} ({r['status']})"})
    for r in repo.conn.execute("SELECT table_id,title,caption,version,status FROM structured_tables ORDER BY table_id"):
        out.append({"kind": "table", "object_id": r["table_id"], "version": int(r["version"]),
                    "label": f"Tabella — {r['title'] or r['caption'] or r['table_id']} ({r['status']})"})
    for r in repo.conn.execute("SELECT figure_id,title,caption,version,status FROM figures ORDER BY figure_id"):
        out.append({"kind": "figure", "object_id": r["figure_id"], "version": int(r["version"]),
                    "label": f"Figura — {r['title'] or r['caption'] or r['figure_id']} ({r['status']})"})
    return out


def link_ccms_object_as_evidence(svc: ComplianceService, kind: str, object_id: str, object_version: int,
                                 requirement_id: str, *, actor: str, description: str = "") -> str:
    """Evidence reuse (acceptance #7 of 3.5A): the same CCMS object linked
    twice (to the same or a different requirement) reuses the SAME evidence
    record, deterministically keyed by kind+object_id+version, instead of
    creating a duplicate. add_evidence() itself still owns the actual
    validation (object must really exist at that version)."""
    evidence_id = f"EV-{kind}-{object_id}-v{object_version}"
    if not svc.conn.execute("SELECT 1 FROM compliance_evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
        svc.add_evidence(evidence_id, kind, actor=actor, object_id=object_id, object_version=object_version,
                         description=description)
    svc.link_evidence_requirement(evidence_id, requirement_id, actor=actor)
    return evidence_id


def evidence_label(item: dict[str, Any]) -> str:
    _kind = item["evidence_kind"]
    kind_label = (i18n.t(f"evidence.kind.{_kind}") if _kind in _EVIDENCE_KINDS else _kind)
    if item["evidence_kind"] in ("external_file", "customer_document", "human_attestation"):
        body = item.get("description") or item.get("external_ref") or "(senza descrizione)"
    else:
        body = f"{item['object_id']} (v{item['object_version']})"
    freshness = "" if item.get("is_current") else " — NON PIÙ AGGIORNATA"
    return f"{kind_label} — {body}{freshness}"

# ---------------------------------------------------------------------
# Pack update: detection, preview, apply
#
# This module is the ONLY layer that knows about both the curated pack
# content and the project engine, so the diff is computed here and handed
# to the engine. The engine stays content-agnostic and there is exactly
# one diff implementation (compliance_starter_packs.pack_diff).
# ---------------------------------------------------------------------

UPDATE_AVAILABLE_TITLE = "Aggiornamento del pacchetto disponibile"
UPDATE_APPLIED_MESSAGE = "Pacchetto aggiornato correttamente"
UPDATE_NONE_MESSAGE = "Il pacchetto è già aggiornato all'ultima revisione verificata."

# What each change type is called for a user who has never read the word
# "diff" in their life.
CHANGE_KINDS = ("added", "changed", "retired")


def change_labels(lang: str | None = None) -> dict[str, str]:
    return {k: i18n.translate(f"pack.change.{k}", lang) for k in CHANGE_KINDS}

_CHANGE_EXPLANATION = {
    "added": "Nuovo obbligo documentale introdotto nel pacchetto.",
    "substantive": ("L'obbligo richiede ora di documentare qualcosa che prima non era indicato: "
                    "la decisione precedente torna in «Da verificare»."),
    "editorial": ("Testo allineato alla terminologia ufficiale del regolamento. "
                  "L'obbligo non cambia: le decisioni del progetto restano valide."),
    "retired": "Obbligo non più presente nella versione corrente del pacchetto.",
}


def pack_update_status(svc: ComplianceService, pack: dict[str, Any]) -> dict[str, Any]:
    """Is there a newer revision of an already-imported pack?

    Purely local: an integer comparison against what the project stored,
    never a network call. A project that has not imported this pack at
    all gets `installed=False` and no update notice -- there is nothing to
    update, and offering one would be noise."""
    state = svc.installed_pack_state(pack["pack_id"])
    if not state.get("installed"):
        return {"installed": False, "update_available": False,
                "pack_name": pack.get("name", ""), "title": UPDATE_AVAILABLE_TITLE}

    installed = svc.installed_pack_requirements(pack["pack_id"])
    groups = csp.installed_groups(installed)
    diff = csp.pack_diff(pack, installed, groups=groups)
    available_content = int(pack.get("content_version", 0))
    current_content = int(state.get("content_version", 0))
    has_changes = bool(diff["added"] or diff["changed"] or diff["retired"])
    return {
        "installed": True,
        "title": UPDATE_AVAILABLE_TITLE,
        "pack_name": pack.get("name", ""),
        "pack_id": pack["pack_id"],
        # Both a revision move AND an actual content difference are
        # required. A revision bump with nothing to apply is not an
        # update, and content differences with no revision move would
        # mean the pack module was edited without versioning it.
        "update_available": has_changes and available_content > current_content,
        "current_pack_version": state.get("pack_version", ""),
        "available_pack_version": pack.get("pack_version", ""),
        "current_content_version": current_content,
        "available_content_version": available_content,
        "groups": [csp.APPLICABILITY_GROUPS.get(g, g) for g in groups],
        "source_version": pack.get("source_version", ""),
        "added": len(diff["added"]),
        "changed": len(diff["changed"]),
        "changed_substantive": len(diff["changed_substantive"]),
        "changed_editorial": len(diff["changed_editorial"]),
        "retired": len(diff["retired"]),
        "unaffected": len(diff["unaffected"]),
        "diff": diff,
    }


def pack_update_preview(svc: ComplianceService, pack: dict[str, Any]) -> dict[str, Any]:
    """Human-readable preview of what applying the update would do.

    One row per affected obligation with its title, its legal reference
    and what kind of change it is -- never a raw before/after dump, which
    nobody outside this repository can read."""
    status = pack_update_status(svc, pack)
    if not status["installed"]:
        return {"installed": False, "rows": []}
    diff = status["diff"]
    by_id = {r["requirement_id"]: r for r in pack["requirements"]}
    installed_by_id = {r["requirement_id"]: r for r in svc.installed_pack_requirements(pack["pack_id"])}

    rows: list[dict[str, Any]] = []
    for rid in diff["added"]:
        req = by_id[rid]
        rows.append({"title": req["title"], "reference": req["article_ref"],
                     "change": i18n.t("pack.change.added"),
                     "detail": _CHANGE_EXPLANATION["added"]})
    for rid in diff["changed_substantive"]:
        req = by_id[rid]
        rows.append({"title": req["title"], "reference": req["article_ref"],
                     "change": i18n.t("pack.change.changed"),
                     "detail": _CHANGE_EXPLANATION["substantive"]})
    for rid in diff["changed_editorial"]:
        req = by_id[rid]
        rows.append({"title": req["title"], "reference": req["article_ref"],
                     "change": i18n.t("pack.change.changed"),
                     "detail": _CHANGE_EXPLANATION["editorial"]})
    for rid in diff["retired"]:
        cur = installed_by_id.get(rid, {})
        rows.append({"title": cur.get("title", ""), "reference": cur.get("article_ref", ""),
                     "change": i18n.t("pack.change.retired"),
                     "detail": _CHANGE_EXPLANATION["retired"]})
    return {
        "installed": True,
        "update_available": status["update_available"],
        "pack_name": status["pack_name"],
        "current_pack_version": status["current_pack_version"],
        "available_pack_version": status["available_pack_version"],
        "source_version": status["source_version"],
        "counts": {"added": status["added"], "changed": status["changed"],
                   "retired": status["retired"], "unaffected": status["unaffected"]},
        "rows": rows,
    }


def apply_pack_update(svc: ComplianceService, pack: dict[str, Any], scope_id: str,
                      actor: str) -> dict[str, Any]:
    """Applies the update through the engine, with the diff computed here.

    Recomputed rather than trusting a diff the UI may have cached: the
    preview a user looked at ten minutes ago must never be what actually
    gets written."""
    status = pack_update_status(svc, pack)
    if not status["installed"]:
        raise DomainValidationError(de.ERR_PACK_NOT_INSTALLED, pack_id=pack["pack_id"])
    result = svc.update_requirement_pack(pack, scope_id=scope_id, actor=actor, diff=status["diff"])
    result["message"] = (UPDATE_NONE_MESSAGE if result["changed_nothing"]
                         else UPDATE_APPLIED_MESSAGE)
    return result


# ---------------------------------------------------------------------
# Backwards-compatible module attributes.
#
# These used to be plain dicts/strings evaluated at import time. They are
# still read as attributes by app.py and by the regression suites, but a
# value frozen at import cannot follow a language switch -- so they are
# resolved on access instead, in whatever language is selected now.
# PEP 562 module __getattr__ is only consulted for names that are NOT
# defined normally, which is exactly the behaviour wanted here.
# ---------------------------------------------------------------------
_DYNAMIC_ATTRS = {
    "STATUS_LABELS": status_labels,
    "SCOPE_STATUS_LABELS": scope_status_labels,
    "APPLIES_LABELS": applicability_labels,
    "COVERAGE_LABELS": coverage_labels,
    "CHANGE_LABELS": change_labels,
    "BUCKET_LABELS": bucket_labels,
    "EVIDENCE_KIND_LABELS": evidence_kind_labels,
    "COVERAGE_VIEW_TITLE": lambda: i18n.t("conformity.coverage.view_title"),
    "COVERAGE_RATIO_LABEL": lambda: i18n.t("conformity.coverage.ratio_label"),
}


def __getattr__(name: str):
    resolver = _DYNAMIC_ATTRS.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()
