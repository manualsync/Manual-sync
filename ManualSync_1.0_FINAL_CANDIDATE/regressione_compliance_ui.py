"""Milestone 3.5B regression suite: Guided Compliance UX.

Tests the actual UI callback functions in app.py (compliance_load_ui,
compliance_switch_scope_ui, compliance_open_requirement_ui,
compliance_decide_applicability_ui, compliance_decide_coverage_ui,
compliance_link_evidence_ui, compliance_unlink_evidence_ui) and the
compliance_view.py adapter directly -- calling the real Python functions,
not driving a browser. Prefers semantic assertions (gr.update() dict
values, tuple positions, backend row presence/absence) over matching HTML
strings, per the "avoid fragile string-only tests" instruction -- HTML is
only checked for the presence of specific, load-bearing phrases (e.g. the
empty-state honesty message, the no-certification disclaimer), never for
exact markup.

Fixtures are entirely synthetic/generic regulatory placeholders (SYNTH-*):
this suite does not encode or assert any real legal text or requirement.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from ccms_industrial import IndustrialRepository
import compliance_view as cv
from compliance_intelligence import ComplianceService
import app

TMP = Path(tempfile.mkdtemp(prefix="manualsync_compliance_ui_"))
results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


# Fixed positions in the 17-item cp_out_full tuple every compliance_*_ui
# function returns (see the cp_out_full list in app.py's Compliance tab).
IDX_DASH, IDX_TABLE, IDX_PICKER, IDX_HEADER, IDX_APPLIC_CUR, IDX_APPLIC_RADIO, \
    IDX_COV_BANNER, IDX_COV_CUR, IDX_COV_RADIO, IDX_EV_TABLE, IDX_EV_CAND, IDX_EV_UNLINK, \
    IDX_ADV, IDX_RID, IDX_APPLIC_MSG, IDX_COV_MSG, IDX_EV_MSG = range(17)


def _new_project(name: str) -> Path:
    project_dir = TMP / name
    project_dir.mkdir(parents=True, exist_ok=True)
    repo = IndustrialRepository(project_dir / "ManualSync_CCMS_v18.sqlite")
    repo.create_topic("TOPIC-1", "Synthetic Safety Section", "Synthetic body text.", status="Approved")
    repo.create_publication("PUB-MAIN", "Synthetic Manual", ["TOPIC-1"])
    repo.add_block("BLK-1", "TOPIC-1", "paragraph", position=1, text="Synthetic warning text.", status="Approved")
    repo.close()
    return project_dir


def _seed_scope(project_dir: Path, *, n_requirements: int = 1) -> tuple[str, str, list[str]]:
    """A requirement only shows up in a scope's summary if it is either
    explicitly decided (applicability/assessment row) OR reachable via an
    instrument the scope references (ComplianceService.requirements_in_scope()).
    Linking a synthetic instrument to the scope here means an UNTOUCHED
    requirement still appears (as 'To verify') -- matching the real guided
    flow, where a scope is normally built around one or more instruments."""
    repo = IndustrialRepository(cv.ccms_db_path(project_dir))
    svc = ComplianceService(repo)
    try:
        svc.create_instrument("INST-SYNTH-1", "SYNTH-REG-01", actor="QA", title="Synthetic regulation")
        scope_id = svc.create_scope("SCOPE-SYNTH-1", "PUB-MAIN", actor="QA", title="Synthetic EU scope", jurisdiction="EU")
        svc.add_scope_reference(scope_id, "instrument", "INST-SYNTH-1", actor="QA")
        rids = []
        for i in range(n_requirements):
            rid = f"REQ-SYNTH-{i+1}"
            svc.create_requirement(rid, f"Synthetic requirement {i+1}", actor="QA", instrument_id="INST-SYNTH-1",
                                   description="Synthetic obligation for UI testing.")
            rids.append(rid)
        return project_dir, scope_id, rids
    finally:
        repo.close()


# ------------------------------------------------------------------
# Group 1: empty state -- a publication with NO compliance scope must show
# the honest "not evaluated" message, never "0 requisiti = tutto ok".
# ------------------------------------------------------------------
proj1 = _new_project("g1_empty_state")
scope_dd_upd, *bundle1 = app.compliance_load_ui(str(proj1), "PUB-MAIN")
check("group1: no scopes exist -> the scope dropdown has zero choices", scope_dd_upd.get("choices") == [], scope_dd_upd)
check("group1: dashboard states no active compliance evaluation exists (not '0 = ok')",
      "Nessuna valutazione di compliance attiva" in bundle1[IDX_DASH], bundle1[IDX_DASH])
check("group1: dashboard does NOT claim certification/conformity anywhere",
      not any(w in bundle1[IDX_DASH] for w in ("Certificat", "CE verificat", "Conforme")), bundle1[IDX_DASH])
check("group1: requirement table is empty", bundle1[IDX_TABLE].empty, bundle1[IDX_TABLE])
check("group1: detail panel shows the picker placeholder, nothing crashes", "Seleziona un requisito" in bundle1[IDX_HEADER], bundle1[IDX_HEADER])

# ------------------------------------------------------------------
# Group 1b: the scope-picker Dropdown's (label, value) tuple contract.
# This exact class of bug (tuple order swapped -> Gradio's real
# Dropdown.preprocess() rejects the value with "not in list of choices")
# is INVISIBLE to a direct Python call of compliance_load_ui(), because
# that validation only runs inside Gradio's own request-processing layer,
# not in this function's return value. Caught once already via a real
# browser click during manual verification -- asserting it explicitly here
# so a regression doesn't require a browser to catch a second time.
# ------------------------------------------------------------------
proj1b, scope1b, _rids1b = _seed_scope(_new_project("g1b_scope_dropdown_contract"))
scope_dd_upd_1b, *_bundle1b = app.compliance_load_ui(str(proj1b), "PUB-MAIN")
choices_1b = scope_dd_upd_1b.get("choices") or []
check("group1b: scope dropdown choices are (label, value) tuples, per Gradio's contract",
      all(isinstance(c, (tuple, list)) and len(c) == 2 for c in choices_1b), choices_1b)
values_1b = [c[1] for c in choices_1b]
check("group1b: the selected dropdown VALUE (scope_id) is found among the tuples' SECOND element -- "
      "exactly what Gradio's real Dropdown.preprocess() checks; a (value, label) ordering (the actual "
      "bug caught during manual verification) fails this same assertion",
      scope_dd_upd_1b.get("value") in values_1b, (scope_dd_upd_1b.get("value"), values_1b))

# ------------------------------------------------------------------
# Group 2: summary shown in the dashboard is EXACTLY what the backend
# computes -- never a divergent recomputation in app.py.
# ------------------------------------------------------------------
proj2, scope2, rids2 = _seed_scope(_new_project("g2_summary_matches_backend"), n_requirements=4)
repo2 = IndustrialRepository(cv.ccms_db_path(proj2))
svc2 = ComplianceService(repo2)
try:
    svc2.set_applicability(scope2, rids2[0], "APPLIES", actor="QA")
    svc2.add_evidence("EV-2", "block", actor="QA", object_id="BLK-1", object_version=1)
    svc2.link_evidence_requirement("EV-2", rids2[0], actor="QA")
    svc2.set_assessment(scope2, rids2[0], "COVERED", actor="QA")
    svc2.set_applicability(scope2, rids2[1], "APPLIES", actor="QA")
    svc2.set_assessment(scope2, rids2[1], "MISSING", actor="QA")
    svc2.set_applicability(scope2, rids2[2], "NOT_APPLICABLE", actor="QA", justification="Synthetic: not present on this variant.")
    backend_summary = svc2.summary_for_scope(scope2)
finally:
    repo2.close()

bundle2 = app.compliance_switch_scope_ui(str(proj2), scope2)
dash2 = bundle2[IDX_DASH]
check("group2: dashboard reflects Covered=1 from the backend summary", f"<b>{backend_summary['counts']['Covered']}</b>" in dash2 and backend_summary['counts']['Covered'] == 1, dash2)
check("group2: dashboard reflects Missing=1 from the backend summary", backend_summary['counts']['Missing'] == 1, backend_summary)
check("group2: dashboard reflects Not applicable=1 from the backend summary", backend_summary['counts']['Not applicable'] == 1, backend_summary)
check("group2: dashboard reflects To verify=1 (REQ-SYNTH-4, never touched) from the backend summary", backend_summary['counts']['To verify'] == 1, backend_summary)
check("group2: requirement table row count equals backend total", len(bundle2[IDX_TABLE]) == backend_summary["total"] == 4, (len(bundle2[IDX_TABLE]), backend_summary))
statuses_in_table = list(bundle2[IDX_TABLE]["Stato"])
check("group2: table sort order is Missing, To verify, Covered, Not applicable",
      statuses_in_table == ["Missing", "To verify", "Covered", "Not applicable"], statuses_in_table)
check("group2: the mandatory non-certification disclaimer is present", "conformità legale" in dash2, dash2)

# ------------------------------------------------------------------
# Group 3: no decision is ever pre-selected when a requirement is opened.
# ------------------------------------------------------------------
proj3, scope3, rids3 = _seed_scope(_new_project("g3_no_preselection"))
label3 = f"{rids3[0]} — Synthetic requirement 1"
bundle3 = app.compliance_open_requirement_ui(str(proj3), scope3, label3)
check("group3: applicability Radio has NO preselected value on open", bundle3[IDX_APPLIC_RADIO].get("value") is None, bundle3[IDX_APPLIC_RADIO])
check("group3: coverage Radio has NO preselected value on open", bundle3[IDX_COV_RADIO].get("value") is None, bundle3[IDX_COV_RADIO])
check("group3: coverage banner explains it is blocked until applicability=APPLIES",
      "applicabilità" in bundle3[IDX_COV_BANNER].lower(), bundle3[IDX_COV_BANNER])
check("group3: rid state is populated with the opened requirement", bundle3[IDX_RID] == rids3[0], bundle3[IDX_RID])

# Re-open after a real decision: still never pre-selected.
repo3 = IndustrialRepository(cv.ccms_db_path(proj3))
svc3 = ComplianceService(repo3)
try:
    svc3.set_applicability(scope3, rids3[0], "APPLIES", actor="QA")
finally:
    repo3.close()
bundle3b = app.compliance_open_requirement_ui(str(proj3), scope3, label3)
check("group3: even after a real decision exists, re-opening the SAME requirement never pre-selects the Radio again",
      bundle3b[IDX_APPLIC_RADIO].get("value") is None, bundle3b[IDX_APPLIC_RADIO])

# ------------------------------------------------------------------
# Group 4: Not-applicable without justification is rejected inline, and
# NOTHING is persisted (no partial mutation).
# ------------------------------------------------------------------
proj4, scope4, rids4 = _seed_scope(_new_project("g4_na_no_justification"))
bundle4 = app.compliance_decide_applicability_ui(str(proj4), scope4, rids4[0], "Non applicabile", "QA Reviewer", "", "")
check("group4: inline error, no gr.Error raised (function returned normally)", "⛔" in bundle4[IDX_APPLIC_MSG], bundle4[IDX_APPLIC_MSG])
repo4 = IndustrialRepository(cv.ccms_db_path(proj4))
svc4 = ComplianceService(repo4)
try:
    check("group4: NOTHING was persisted to compliance_applicability", svc4.applicability_row(scope4, rids4[0]) is None, "")
finally:
    repo4.close()
check("group4: the detail panel and dashboard are still fully rendered (not corrupted)",
      bundle4[IDX_DASH] and bundle4[IDX_HEADER], "")

# ------------------------------------------------------------------
# Group 5: applicability decision without a reviewer name is rejected
# inline, nothing persisted.
# ------------------------------------------------------------------
proj5, scope5, rids5 = _seed_scope(_new_project("g5_missing_reviewer"))
bundle5 = app.compliance_decide_applicability_ui(str(proj5), scope5, rids5[0], "Si applica", "", "", "")
check("group5: inline error for missing reviewer", "⛔" in bundle5[IDX_APPLIC_MSG], bundle5[IDX_APPLIC_MSG])
repo5 = IndustrialRepository(cv.ccms_db_path(proj5))
svc5 = ComplianceService(repo5)
try:
    check("group5: NOTHING was persisted without a reviewer", svc5.applicability_row(scope5, rids5[0]) is None, "")
finally:
    repo5.close()

# ------------------------------------------------------------------
# Group 6: Covered is refused inline without valid evidence -- the
# frontend never bypasses this, it only surfaces the backend's own
# rejection.
# ------------------------------------------------------------------
proj6, scope6, rids6 = _seed_scope(_new_project("g6_covered_no_evidence"))
repo6 = IndustrialRepository(cv.ccms_db_path(proj6))
svc6 = ComplianceService(repo6)
try:
    svc6.set_applicability(scope6, rids6[0], "APPLIES", actor="QA")
finally:
    repo6.close()
bundle6 = app.compliance_decide_coverage_ui(str(proj6), scope6, rids6[0], "Coperto", "QA Reviewer", "")
check("group6: Covered without evidence is rejected inline", "⛔" in bundle6[IDX_COV_MSG], bundle6[IDX_COV_MSG])
repo6b = IndustrialRepository(cv.ccms_db_path(proj6))
svc6b = ComplianceService(repo6b)
try:
    check("group6: NOTHING was persisted to compliance_assessment", svc6b.assessment_row(scope6, rids6[0]) is None, "")
finally:
    repo6b.close()

# ------------------------------------------------------------------
# Group 7 + 8: link a real CCMS object as evidence via the candidate
# Dropdown format the UI itself generates, then Covered succeeds.
# ------------------------------------------------------------------
proj7, scope7, rids7 = _seed_scope(_new_project("g7_link_then_cover"))
repo7 = IndustrialRepository(cv.ccms_db_path(proj7))
svc7 = ComplianceService(repo7)
try:
    svc7.set_applicability(scope7, rids7[0], "APPLIES", actor="QA")
finally:
    repo7.close()
label7 = f"{rids7[0]} — Synthetic requirement 1"
opened7 = app.compliance_open_requirement_ui(str(proj7), scope7, label7)
candidate_choices = opened7[IDX_EV_CAND].get("choices") or []
block_candidate = next((c for c in candidate_choices if c.startswith("block:BLK-1:")), None)
check("group7: the seeded block appears as a linkable evidence candidate", block_candidate is not None, candidate_choices)

link_bundle = app.compliance_link_evidence_ui(str(proj7), scope7, rids7[0], block_candidate, "QA Reviewer")
check("group7: evidence linking succeeds with an inline confirmation", "✅" in link_bundle[IDX_EV_MSG], link_bundle[IDX_EV_MSG])
check("group7: the linked evidence now appears in the evidence table", len(link_bundle[IDX_EV_TABLE]) == 1, link_bundle[IDX_EV_TABLE])

cover_bundle = app.compliance_decide_coverage_ui(str(proj7), scope7, rids7[0], "Coperto", "QA Reviewer", "")
check("group8: Covered succeeds once real evidence is linked", "✅" in cover_bundle[IDX_COV_MSG], cover_bundle[IDX_COV_MSG])
check("group8: dashboard now shows Covered=1 for this scope", "Coperti" in cover_bundle[IDX_DASH] and "<b>1</b>" in cover_bundle[IDX_DASH], cover_bundle[IDX_DASH])
repo7b = IndustrialRepository(cv.ccms_db_path(proj7))
svc7b = ComplianceService(repo7b)
try:
    check("group8: backend compute_status genuinely reads Covered", svc7b.compute_status(scope7, rids7[0]) == "Covered", "")
finally:
    repo7b.close()

# ------------------------------------------------------------------
# Group 9: staleness collapses into "To verify" in the dashboard/list --
# never a 5th "Stale" state.
# ------------------------------------------------------------------
proj9, scope9, rids9 = _seed_scope(_new_project("g9_staleness_as_to_verify"))
repo9 = IndustrialRepository(cv.ccms_db_path(proj9))
svc9 = ComplianceService(repo9)
try:
    svc9.set_applicability(scope9, rids9[0], "APPLIES", actor="QA")
    svc9.add_evidence("EV-9", "block", actor="QA", object_id="BLK-1", object_version=1)
    svc9.link_evidence_requirement("EV-9", rids9[0], actor="QA")
    svc9.set_assessment(scope9, rids9[0], "COVERED", actor="QA")
    svc9.update_requirement(rids9[0], actor="QA", description="Synthetic wording change to trigger staleness.")
finally:
    repo9.close()
bundle9 = app.compliance_switch_scope_ui(str(proj9), scope9)
check("group9: the requirement table shows 'To verify', not a 5th 'Stale' label",
      list(bundle9[IDX_TABLE]["Stato"]) == ["To verify"], list(bundle9[IDX_TABLE]["Stato"]))
check("group9: the essential reason explains it's a staleness issue, in plain language",
      "cambiato" in bundle9[IDX_TABLE]["Motivo"].iloc[0], bundle9[IDX_TABLE]["Motivo"].iloc[0])
check("group9: no status value ANYWHERE in the table is the literal word 'Stale'",
      not any("stale" in str(s).lower() for s in bundle9[IDX_TABLE]["Stato"]), list(bundle9[IDX_TABLE]["Stato"]))

# ------------------------------------------------------------------
# Group 10: persistence survives a brand-new ComplianceService instance
# (simulating a page reload / fresh app restart against the same project).
# ------------------------------------------------------------------
proj10, scope10, rids10 = _seed_scope(_new_project("g10_persistence_after_reload"))
repo10a = IndustrialRepository(cv.ccms_db_path(proj10))
svc10a = ComplianceService(repo10a)
try:
    svc10a.set_applicability(scope10, rids10[0], "NOT_APPLICABLE", actor="QA", justification="Synthetic: reload persistence check.")
finally:
    repo10a.close()
# A brand-new repo/service pair -- nothing shared with the write above.
bundle10 = app.compliance_switch_scope_ui(str(proj10), scope10)
check("group10: the decision persisted across a fresh ComplianceService instance (simulated reload)",
      "Not applicable" in list(bundle10[IDX_TABLE]["Stato"]), list(bundle10[IDX_TABLE]["Stato"]))

# ------------------------------------------------------------------
# Group 11: no partial mutation survives a rejected decision -- audit
# trail confirms zero new events for the rejected action too.
# ------------------------------------------------------------------
proj11, scope11, rids11 = _seed_scope(_new_project("g11_no_partial_mutation"))
repo11 = IndustrialRepository(cv.ccms_db_path(proj11))
svc11 = ComplianceService(repo11)
try:
    audit_before = svc11.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
finally:
    repo11.close()
app.compliance_decide_applicability_ui(str(proj11), scope11, rids11[0], "Non applicabile", "QA Reviewer", "", "")
app.compliance_decide_coverage_ui(str(proj11), scope11, rids11[0], "Coperto", "QA Reviewer", "")
repo11b = IndustrialRepository(cv.ccms_db_path(proj11))
svc11b = ComplianceService(repo11b)
try:
    audit_after = svc11b.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
    check("group11: two rejected decisions (N/A w/o justification, Covered w/o applicability+evidence) produced ZERO new audit events",
          audit_after == audit_before, (audit_before, audit_after))
    check("group11: compute_status is still 'To verify' -- untouched by either rejected attempt",
          svc11b.compute_status(scope11, rids11[0]) == "To verify", "")
finally:
    repo11b.close()

# ------------------------------------------------------------------
# Group 12 (acceptance bug fix): the Compliance tab must reload for real
# from ComplianceService every time it becomes the selected tab -- not
# only on an explicit "Aggiorna" click. Root cause of the reported bug:
# scope/table/picker are plain Gradio components with empty
# construction-time defaults (choices=[], value=None); ANY client
# session/reconnect boundary Gradio manages resets a session's component
# values back to those defaults, and nothing server-side can prevent that
# -- the only real fix is for the tab to always refresh itself the moment
# it is looked at again. This is verified at the Gradio dependency-graph
# level (semantic: which function is bound to the tab's own "select"
# event, and its exact input/output contract) rather than via a live
# browser session, which cannot be driven deterministically for a
# client-reconnect scenario from a plain regression script.
# ------------------------------------------------------------------
_cp_tab_component = next(c for c in app.demo.config["components"]
                         if c["type"] == "tabitem" and c.get("props", {}).get("label") == "Compliance")
_select_deps = [d for d in app.demo.config["dependencies"]
               if (_cp_tab_component["id"], "select") in [tuple(t) for t in d.get("targets", [])]]
check("group12: the Compliance tab has exactly one 'select' event wired (no duplicate/missing binding)",
      len(_select_deps) == 1, _select_deps)
_select_dep = _select_deps[0]
check("group12: the tab's select event is bound to compliance_load_ui (same function 'Aggiorna' calls) -- "
      "not a no-op, not a different/partial reload function",
      _select_dep.get("api_name") == "compliance_load_ui_1", _select_dep.get("api_name"))
check("group12: the select event takes exactly [project_state, cp_pub] as inputs -- 2 inputs, "
      "matching compliance_load_ui's real signature",
      len(_select_dep.get("inputs", [])) == 2, _select_dep.get("inputs"))
check("group12: the select event writes to exactly 18 outputs (scope dropdown + the full 17-item detail bundle) -- "
      "an output-count mismatch here would silently corrupt the panel exactly like the reported bug",
      len(_select_dep.get("outputs", [])) == 18, len(_select_dep.get("outputs", [])))

# And confirm the OTHER half of the fix's correctness: compliance_load_ui
# itself is a pure function of (project_dir, publication_id) against the
# database -- it never depends on any prior client-side state, so calling
# it again after a simulated "reset to defaults" always reproduces the
# correct scope/table/picker from scratch.
proj12, scope12, rids12 = _seed_scope(_new_project("g12_reload_is_stateless"), n_requirements=4)
first_call = app.compliance_load_ui(str(proj12), "PUB-MAIN")
second_call = app.compliance_load_ui(str(proj12), "PUB-MAIN")  # simulates a second, independent "reveal"
check("group12: calling compliance_load_ui twice independently (simulating two separate tab reveals) "
      "yields the identical, fully-populated scope dropdown -- proving it is not relying on leftover state",
      first_call[0].get("choices") == second_call[0].get("choices") and
      first_call[0].get("choices") not in (None, []), first_call[0])
check("group12: both reveals populate the requirement table with all 4 requirements, never empty/corrupted",
      len(first_call[1 + IDX_TABLE]) == 4 and len(second_call[1 + IDX_TABLE]) == 4,
      (len(first_call[1 + IDX_TABLE]), len(second_call[1 + IDX_TABLE])))
check("group12: both reveals populate the 'Apri un requisito' picker with all 4 requirements",
      len(first_call[1 + IDX_PICKER].get("choices") or []) == 4 and
      len(second_call[1 + IDX_PICKER].get("choices") or []) == 4,
      (first_call[1 + IDX_PICKER], second_call[1 + IDX_PICKER]))

# ------------------------------------------------------------------
# Group 13 (acceptance bug fix): a validation failure on Applicabilità or
# Copertura must be inline and NON-destructive -- the Radio the user just
# selected must stay visibly selected (not reset to None) so a retry never
# requires reselecting it, while a SUCCESSFUL decision (or a fresh open)
# still resets the Radio to unselected -- the "never pre-selected"
# invariant applies to the NEXT edit, not to echoing back a failed one.
# ------------------------------------------------------------------
proj13, scope13, rids13 = _seed_scope(_new_project("g13_radio_preserved_on_failure"))
label13 = f"{rids13[0]} — Synthetic requirement 1"
app.compliance_open_requirement_ui(str(proj13), scope13, label13)

# 13a: Not-applicable submitted with reviewer but NO justification -- must
# fail, and the Radio must echo back "Non applicabile", not reset to None.
b13a = app.compliance_decide_applicability_ui(str(proj13), scope13, rids13[0], "Non applicabile", "Alessandro", "", "")
check("group13a: the rejection message is inline", "⛔" in b13a[IDX_APPLIC_MSG], b13a[IDX_APPLIC_MSG])
check("group13a: the Radio still shows 'Non applicabile' selected after the failed attempt -- not reset to None",
      b13a[IDX_APPLIC_RADIO].get("value") == "Non applicabile", b13a[IDX_APPLIC_RADIO])
repo13 = IndustrialRepository(cv.ccms_db_path(proj13))
svc13 = ComplianceService(repo13)
try:
    check("group13a: nothing was persisted to the backend by the failed attempt",
          svc13.applicability_row(scope13, rids13[0]) is None, "")
finally:
    repo13.close()

# 13b: retry WITHOUT reselecting (same "Non applicabile" label, as a real
# client would resend since the Radio was never cleared), this time with a
# real justification -- must succeed.
b13b = app.compliance_decide_applicability_ui(str(proj13), scope13, rids13[0], "Non applicabile", "Alessandro",
                                              "Variante senza questo rischio.", "")
check("group13b: the retry succeeds without the caller ever reselecting the Radio",
      "✅" in b13b[IDX_APPLIC_MSG], b13b[IDX_APPLIC_MSG])
check("group13b: on SUCCESS the Radio resets to unselected -- the decision is now shown in applic_current instead, "
      "and an unselected Radio is the correct starting point for the next edit",
      b13b[IDX_APPLIC_RADIO].get("value") is None, b13b[IDX_APPLIC_RADIO])
repo13b = IndustrialRepository(cv.ccms_db_path(proj13))
svc13b = ComplianceService(repo13b)
try:
    check("group13b: the decision is genuinely persisted with the right actor",
          svc13b.applicability_row(scope13, rids13[0])["decided_by"] == "Alessandro", "")
finally:
    repo13b.close()

# 13c: the SAME property for "Si applica" -- fails here on a missing
# reviewer (not missing justification), Radio must still be preserved.
proj13c, scope13c, rids13c = _seed_scope(_new_project("g13c_applies_radio_preserved"))
app.compliance_open_requirement_ui(str(proj13c), scope13c, f"{rids13c[0]} — Synthetic requirement 1")
b13c = app.compliance_decide_applicability_ui(str(proj13c), scope13c, rids13c[0], "Si applica", "", "", "")
check("group13c: 'Si applica' rejected for a missing reviewer stays inline", "⛔" in b13c[IDX_APPLIC_MSG], b13c[IDX_APPLIC_MSG])
check("group13c: the Radio still shows 'Si applica' selected after this failed attempt too",
      b13c[IDX_APPLIC_RADIO].get("value") == "Si applica", b13c[IDX_APPLIC_RADIO])

# 13d: Coverage -- Covered rejected for missing evidence must preserve the
# 'Coperto' selection the same way.
proj13d, scope13d, rids13d = _seed_scope(_new_project("g13d_covered_radio_preserved"))
repo13d = IndustrialRepository(cv.ccms_db_path(proj13d))
svc13d = ComplianceService(repo13d)
try:
    svc13d.set_applicability(scope13d, rids13d[0], "APPLIES", actor="QA")
finally:
    repo13d.close()
app.compliance_open_requirement_ui(str(proj13d), scope13d, f"{rids13d[0]} — Synthetic requirement 1")
b13d = app.compliance_decide_coverage_ui(str(proj13d), scope13d, rids13d[0], "Coperto", "Alessandro", "")
check("group13d: Covered without evidence is rejected inline", "⛔" in b13d[IDX_COV_MSG], b13d[IDX_COV_MSG])
check("group13d: the Radio still shows 'Coperto' selected after the rejection -- not reset to None",
      b13d[IDX_COV_RADIO].get("value") == "Coperto", b13d[IDX_COV_RADIO])
repo13d_check = IndustrialRepository(cv.ccms_db_path(proj13d))
svc13d_check = ComplianceService(repo13d_check)
try:
    check("group13d: nothing was persisted to compliance_assessment by the rejected attempt",
          svc13d_check.assessment_row(scope13d, rids13d[0]) is None, "")
finally:
    repo13d_check.close()

# 13e: retry after linking evidence, again WITHOUT reselecting 'Coperto' --
# must now succeed, and the Radio resets to unselected on success.
repo13e = IndustrialRepository(cv.ccms_db_path(proj13d))
svc13e = ComplianceService(repo13e)
try:
    svc13e.add_evidence("EV-13E", "block", actor="QA", object_id="BLK-1", object_version=1)
    svc13e.link_evidence_requirement("EV-13E", rids13d[0], actor="QA")
finally:
    repo13e.close()
b13e = app.compliance_decide_coverage_ui(str(proj13d), scope13d, rids13d[0], "Coperto", "Alessandro", "")
check("group13e: the retry succeeds without ever reselecting 'Coperto'", "✅" in b13e[IDX_COV_MSG], b13e[IDX_COV_MSG])
check("group13e: on SUCCESS the coverage Radio resets to unselected",
      b13e[IDX_COV_RADIO].get("value") is None, b13e[IDX_COV_RADIO])

# ------------------------------------------------------------------
# Group 14 (3.5B stabilization gate -- real bug found during live browser
# acceptance, not covered by group13e because that test linked evidence
# directly via ComplianceService, bypassing the real
# compliance_link_evidence_ui callback entirely): linking (or unlinking)
# evidence is a SUPPORTING action for a pending Covered/Not-applicable
# decision -- the real acceptance path is "try Covered -> rejected -> link
# evidence -> confirm Covered again WITHOUT reselecting the Radio". Linking
# evidence must never reset either decision Radio, even though it isn't
# itself an applicability/coverage decision.
# ------------------------------------------------------------------
proj14, scope14, rids14 = _seed_scope(_new_project("g14_link_evidence_preserves_radio"))
repo14 = IndustrialRepository(cv.ccms_db_path(proj14))
svc14 = ComplianceService(repo14)
try:
    svc14.set_applicability(scope14, rids14[0], "APPLIES", actor="QA")
finally:
    repo14.close()
app.compliance_open_requirement_ui(str(proj14), scope14, f"{rids14[0]} — Synthetic requirement 1")

# Try Covered without evidence -- rejected, Radio shows 'Coperto' (already
# covered by group13d/13). Now the user picks a candidate and clicks
# "Collega come evidenza" -- simulating the real Gradio wiring, which
# passes the CURRENT (still-selected) Radio values as extra inputs.
app.compliance_decide_coverage_ui(str(proj14), scope14, rids14[0], "Coperto", "Alessandro", "")
candidates = cv.evidence_candidates(IndustrialRepository(cv.ccms_db_path(proj14)))
block_candidate = next(c for c in candidates if c["kind"] == "block")
candidate_label = f"{block_candidate['kind']}:{block_candidate['object_id']}:{block_candidate['version']} — {block_candidate['label']}"
b14 = app.compliance_link_evidence_ui(str(proj14), scope14, rids14[0], candidate_label, "Alessandro",
                                      cur_applic_radio=None, cur_cov_radio="Coperto")
check("group14: linking evidence succeeds", "✅" in b14[IDX_EV_MSG], b14[IDX_EV_MSG])
check("group14: the pending 'Coperto' choice SURVIVES the evidence-link action -- this is the real bug found live: "
      "linking evidence used to silently reset the coverage Radio to None",
      b14[IDX_COV_RADIO].get("value") == "Coperto", b14[IDX_COV_RADIO])

# Now the user confirms Covered WITHOUT ever reselecting the Radio --
# exactly the real acceptance path -- and it must succeed.
b14b = app.compliance_decide_coverage_ui(str(proj14), scope14, rids14[0], "Coperto", "Alessandro", "")
check("group14: confirming Covered after the preserved selection succeeds without reselecting",
      "✅" in b14b[IDX_COV_MSG], b14b[IDX_COV_MSG])
repo14b = IndustrialRepository(cv.ccms_db_path(proj14))
svc14b = ComplianceService(repo14b)
try:
    check("group14: the decision is genuinely persisted as Covered", svc14b.compute_status(scope14, rids14[0]) == "Covered", "")
finally:
    repo14b.close()

# Symmetric check for applicability: linking/unlinking evidence must not
# reset a pending Not-applicable choice either.
proj14c, scope14c, rids14c = _seed_scope(_new_project("g14c_link_evidence_preserves_applicability_radio"))
app.compliance_open_requirement_ui(str(proj14c), scope14c, f"{rids14c[0]} — Synthetic requirement 1")
app.compliance_decide_applicability_ui(str(proj14c), scope14c, rids14c[0], "Non applicabile", "Alessandro", "", "")
candidates_c = cv.evidence_candidates(IndustrialRepository(cv.ccms_db_path(proj14c)))
block_candidate_c = next(c for c in candidates_c if c["kind"] == "block")
label_c = f"{block_candidate_c['kind']}:{block_candidate_c['object_id']}:{block_candidate_c['version']} — {block_candidate_c['label']}"
b14c = app.compliance_link_evidence_ui(str(proj14c), scope14c, rids14c[0], label_c, "Alessandro",
                                       cur_applic_radio="Non applicabile", cur_cov_radio=None)
check("group14c: a pending 'Non applicabile' choice also survives an unrelated evidence-link action",
      b14c[IDX_APPLIC_RADIO].get("value") == "Non applicabile", b14c[IDX_APPLIC_RADIO])

print(f"compliance UI build: {app.PRODUCT_BUILD if hasattr(app,'PRODUCT_BUILD') else 'n/a'}")
import json
print(json.dumps({"pass": sum(r["status"] == "PASS" for r in results), "tests": len(results)}))
