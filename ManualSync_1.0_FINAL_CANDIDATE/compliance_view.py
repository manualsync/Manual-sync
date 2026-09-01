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

from ccms_industrial import IndustrialRepository
from compliance_intelligence import ComplianceService

APPLIES_LABELS = {
    "APPLIES": "Si applica",
    "NOT_APPLICABLE": "Non applicabile",
    "TO_VERIFY": "Da verificare",
}
APPLIES_LABELS_REVERSE = {v: k for k, v in APPLIES_LABELS.items()}

COVERAGE_LABELS = {
    "COVERED": "Coperto",
    "TO_VERIFY": "Da verificare",
    "MISSING": "Mancante",
}
COVERAGE_LABELS_REVERSE = {v: k for k, v in COVERAGE_LABELS.items()}

# Requirement list order: what needs a decision first floats to the top.
_STATUS_SORT_KEY = {"Missing": 0, "To verify": 1, "Covered": 2, "Not applicable": 3}

_EVIDENCE_KIND_LABELS = {
    "topic": "Sezione", "block": "Blocco", "table": "Tabella", "figure": "Figura",
    "external_file": "File esterno", "customer_document": "Documento cliente",
    "human_attestation": "Attestazione umana",
}


def ccms_db_path(project_dir: str | Path) -> Path:
    base = Path(project_dir)
    for name in ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"):
        p = base / name
        if p.exists():
            return p
    raise FileNotFoundError("Repository CCMS non trovato nel progetto")


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
        label = f"{r['scope_id']} — {r['title'] or r['jurisdiction'] or 'Scope'} ({r['status']})"
        out.append((label, r["scope_id"]))
    return out


def _requirement_reference(svc: ComplianceService, req: dict[str, Any]) -> str:
    if not req.get("instrument_id"):
        return req.get("article_ref") or "—"
    inst = svc.instrument_row(req["instrument_id"])
    ident = inst["official_identifier"] if inst else req["instrument_id"]
    art = req.get("article_ref") or ""
    return f"{ident} {art}".strip()


def _explain_status(svc: ComplianceService, scope_id: str, requirement_id: str, status: str) -> str:
    """Essential, human-readable reason for the CURRENT status. Re-reads the
    same rows compute_status() already consulted, purely to explain -- it
    never decides the status itself."""
    if status == "Covered":
        n = len(svc.evidence_for_requirement(requirement_id))
        return f"{n} evidenza collegata" if n == 1 else f"{n} evidenze collegate"
    if status == "Not applicable":
        ap = svc.applicability_row(scope_id, requirement_id)
        return (ap or {}).get("justification") or ""
    if status == "Missing":
        asm = svc.assessment_row(scope_id, requirement_id)
        comment = (asm or {}).get("comment") or ""
        return comment.strip() or "Nessuna evidenza ancora individuata"

    # "To verify" -- explain WHY, in the same order compute_status() checks.
    req = svc.requirement_row(requirement_id)
    ap = svc.applicability_row(scope_id, requirement_id)
    if not ap:
        return "Applicabilità non ancora valutata"
    if int(ap["requirement_version"]) != int(req["version"]):
        return "Il testo del requisito è cambiato dopo l'ultima decisione: da riconfermare"
    if ap["applies"] == "TO_VERIFY":
        return "Applicabilità ancora da confermare"
    if ap["applies"] == "APPLIES" and not svc.requirement_is_current(requirement_id):
        return "Il riferimento normativo collegato è stato aggiornato: da rivedere"
    asm = svc.assessment_row(scope_id, requirement_id)
    if not asm:
        return "Copertura non ancora valutata"
    if int(asm["requirement_version_at_assessment"]) != int(req["version"]):
        return "Il testo del requisito è cambiato dopo l'ultima valutazione: da rivedere"
    if asm["status"] == "COVERED":
        return "L'evidenza collegata non è più aggiornata: da rivedere"
    return "Da verificare"


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
    kind_label = _EVIDENCE_KIND_LABELS.get(item["evidence_kind"], item["evidence_kind"])
    if item["evidence_kind"] in ("external_file", "customer_document", "human_attestation"):
        body = item.get("description") or item.get("external_ref") or "(senza descrizione)"
    else:
        body = f"{item['object_id']} (v{item['object_version']})"
    freshness = "" if item.get("is_current") else " — NON PIÙ AGGIORNATA"
    return f"{kind_label} — {body}{freshness}"
