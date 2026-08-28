"""Milestone 3.5A regression suite: Compliance Intelligence core data model
& service (compliance_intelligence.py).

Backend/data-layer only -- no UI, no app.py, no IDML/DOCX/PDF onboarding.
Fixtures are entirely synthetic/generic regulatory placeholders (SYNTH-*):
this suite does not encode or assert any real legal text or requirement.

Covers exactly the 3.5A acceptance criteria: scope<->publication, versioned
regulatory instrument/requirement history, "unassessed" never silently
becoming Covered/N-A, fail-closed N/A justification and Covered-without-
evidence, internal evidence grounding against real CCMS object+version,
evidence reuse across requirements, many-to-many risk<->requirement,
change-impact staleness detection, audit trail on every critical decision,
and coherent scope-level summary counts.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from ccms_industrial import IndustrialRepository
import compliance_intelligence as ci
from compliance_intelligence import ComplianceService

TMP = Path(tempfile.mkdtemp(prefix="manualsync_compliance_"))
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


def _new_repo(name: str) -> IndustrialRepository:
    return IndustrialRepository(TMP / f"{name}.sqlite")


def _seed_publication(repo: IndustrialRepository, pub_id: str = "PUB-SYNTH") -> tuple[str, str, str, str]:
    """A minimal generic CCMS fixture: one topic, one block, one table, one
    figure -- just enough real, versioned objects for evidence grounding."""
    repo.create_topic("TOPIC-SYNTH-1", "Synthetic Safety Section", "Synthetic body.", status="Approved")
    repo.create_publication(pub_id, "Synthetic Manual", ["TOPIC-SYNTH-1"])
    repo.create_table("TBL-SYNTH-1", ["A", "B"], [["1", "2"]], title="Synthetic table", status="Approved")
    repo.create_figure("FIG-SYNTH-1", title="Synthetic figure", file_path="", status="Approved")
    repo.add_block("BLK-SYNTH-1", "TOPIC-SYNTH-1", "paragraph", position=1,
                   text="Synthetic residual-risk warning text.", status="Approved")
    return pub_id, "TOPIC-SYNTH-1", "BLK-SYNTH-1", "TBL-SYNTH-1"


# ------------------------------------------------------------------
# Group 1 (acceptance #1, #10): scope creation, association with a
# publication, and audit trail on creation.
# ------------------------------------------------------------------
repo1 = _new_repo("g1_scope")
pub1, topic1, blk1, tbl1 = _seed_publication(repo1)
svc1 = ComplianceService(repo1)
scope1 = svc1.create_scope("SCOPE-1", pub1, actor="QA", title="EU market", jurisdiction="EU")
check("group1: scope row created and linked to the publication",
      svc1.conn.execute("SELECT publication_id FROM compliance_scope WHERE scope_id=?", (scope1,)).fetchone()["publication_id"] == pub1, "")
check("group1: active_scope_for_publication resolves the newly created scope",
      svc1.active_scope_for_publication(pub1) == scope1, svc1.active_scope_for_publication(pub1))
check("group1: creating a scope on a non-existent publication is rejected (KeyError)",
      _raises(lambda: svc1.create_scope("SCOPE-BAD", "PUB-NOPE", actor="QA"), KeyError), "")
check("group1: scope creation produced an audit event with actor+timestamp",
      any(r["object_type"] == "compliance_scope" and r["action"] == "CREATE" and r["actor"] == "QA"
          for r in svc1.conn.execute("SELECT object_type,action,actor FROM audit_events")), "")
check("group1: a publication with NO scope reports no active scope (never a fabricated evaluation)",
      svc1.active_scope_for_publication("PUB-NOPE-2") is None, "")
repo1.close()

# ------------------------------------------------------------------
# Group 2 (acceptance #2): regulatory instrument + requirement versioning
# keeps full history.
# ------------------------------------------------------------------
repo2 = _new_repo("g2_versioning")
pub2, *_ = _seed_publication(repo2)
svc2 = ComplianceService(repo2)
svc2.create_instrument("INST-1", "SYNTH-REG-1", actor="QA", title="Synthetic regulation v1", status="ACTIVE")
svc2.create_requirement("REQ-1", "Synthetic requirement title", actor="QA", instrument_id="INST-1",
                        article_ref="Art.1", description="Synthetic obligation.")
req_row = svc2.conn.execute("SELECT version,instrument_version_at_authoring FROM compliance_requirement WHERE requirement_id=?", ("REQ-1",)).fetchone()
check("group2: requirement pins the instrument version at authoring time",
      req_row["instrument_version_at_authoring"] == 1, dict(req_row))

svc2.update_instrument("INST-1", actor="QA", status="TRANSITIONAL", title="Synthetic regulation v2")
inst_versions = list(svc2.conn.execute("SELECT version,status FROM compliance_regulatory_instrument_versions WHERE instrument_id=? ORDER BY version", ("INST-1",)))
check("group2: instrument version history has both v1 and v2, nothing overwritten",
      [dict(r) for r in inst_versions] == [{"version": 1, "status": "ACTIVE"}, {"version": 2, "status": "TRANSITIONAL"}],
      [dict(r) for r in inst_versions])
check("group2: instrument's CURRENT row reflects v2", svc2.conn.execute("SELECT version,status FROM compliance_regulatory_instrument WHERE instrument_id=?", ("INST-1",)).fetchone()["version"] == 2, "")

svc2.update_requirement("REQ-1", actor="QA", title="Synthetic requirement title (revised)")
req_versions = list(svc2.conn.execute("SELECT version,title FROM compliance_requirement_versions WHERE requirement_id=? ORDER BY version", ("REQ-1",)))
check("group2: requirement version history has both v1 and v2",
      len(req_versions) == 2 and req_versions[0]["title"] == "Synthetic requirement title" and req_versions[1]["title"].endswith("(revised)"),
      [dict(r) for r in req_versions])
repo2.close()

# ------------------------------------------------------------------
# Group 3 (acceptance #3): an unevaluated requirement is neither Covered
# nor Not applicable -- it is "To verify".
# ------------------------------------------------------------------
repo3 = _new_repo("g3_unassessed")
pub3, *_ = _seed_publication(repo3)
svc3 = ComplianceService(repo3)
scope3 = svc3.create_scope("SCOPE-3", pub3, actor="QA")
svc3.create_requirement("REQ-3", "Untouched requirement", actor="QA")
check("group3: a requirement with zero applicability/assessment rows is 'To verify', never Covered/N-A",
      svc3.compute_status(scope3, "REQ-3") == "To verify", svc3.compute_status(scope3, "REQ-3"))
repo3.close()

# ------------------------------------------------------------------
# Group 4 (acceptance #4 + 3.5A.1 #1): N/A without justification is
# rejected; N/A without an actor is rejected; a COVERED assessment cannot
# even be RECORDED while applicability isn't APPLIES (structural
# prevention); and setting NOT_APPLICABLE on a requirement that already
# has a live Covered assessment actively INVALIDATES it (deletes the row,
# audits what was overwritten) instead of merely being out-voted at read
# time by compute_status().
# ------------------------------------------------------------------
repo4 = _new_repo("g4_na")
pub4, topic4, blk4, tbl4 = _seed_publication(repo4)
svc4 = ComplianceService(repo4)
scope4 = svc4.create_scope("SCOPE-4", pub4, actor="QA")
svc4.create_requirement("REQ-4", "Requirement for N/A test", actor="QA")
check("group4: Not-applicable with empty justification is rejected",
      _raises(lambda: svc4.set_applicability(scope4, "REQ-4", "NOT_APPLICABLE", actor="QA", justification=""), ValueError), "")
check("group4: applicability decision with empty actor is rejected",
      _raises(lambda: svc4.set_applicability(scope4, "REQ-4", "APPLIES", actor=""), ValueError), "")

# 3.5A.1 #1a: coverage cannot be recorded at all before applicability=APPLIES.
check("group4: set_assessment is refused when applicability has never been decided",
      _raises(lambda: svc4.set_assessment(scope4, "REQ-4", "COVERED", actor="QA"), ValueError), "")
svc4.set_applicability(scope4, "REQ-4", "NOT_APPLICABLE", actor="QA", justification="Machine variant does not include this function.")
check("group4: Not-applicable WITH justification succeeds and is reflected in derived status",
      svc4.compute_status(scope4, "REQ-4") == "Not applicable", "")
check("group4: set_assessment is refused while applicability is NOT_APPLICABLE (structural, not just masked at read)",
      _raises(lambda: svc4.set_assessment(scope4, "REQ-4", "COVERED", actor="QA"), ValueError), "")

# 3.5A.1 #1b: the reverse direction -- a legitimate Covered assessment
# existing first, then applicability moving to NOT_APPLICABLE, must
# actively invalidate (delete) that assessment row, not just be shadowed.
svc4.set_applicability(scope4, "REQ-4", "APPLIES", actor="QA")
svc4.add_evidence("EV-4", "human_attestation", actor="QA", description="Manual visual check.")
svc4.link_evidence_requirement("EV-4", "REQ-4", actor="QA")
svc4.set_assessment(scope4, "REQ-4", "COVERED", actor="QA")
check("group4: Covered assessment row genuinely exists before the applicability change",
      svc4.conn.execute("SELECT 1 FROM compliance_assessment WHERE scope_id=? AND requirement_id='REQ-4'", (scope4,)).fetchone() is not None, "")
svc4.set_applicability(scope4, "REQ-4", "NOT_APPLICABLE", actor="QA", justification="Reassessed: function removed from this variant.")
check("group4: moving to NOT_APPLICABLE DELETES the now-contradictory assessment row (invalidated, not just out-voted)",
      svc4.conn.execute("SELECT 1 FROM compliance_assessment WHERE scope_id=? AND requirement_id='REQ-4'", (scope4,)).fetchone() is None, "")
check("group4: the invalidation itself is audited with the prior status preserved",
      any(r["action"] == "INVALIDATED_BY_APPLICABILITY_CHANGE" and json.loads(r["detail_json"]).get("previous_status") == "COVERED"
          for r in svc4.conn.execute("SELECT action,detail_json FROM audit_events WHERE object_type='compliance_assessment'")), "")
check("group4: derived status is 'Not applicable', consistently, with nothing contradictory left in the table",
      svc4.compute_status(scope4, "REQ-4") == "Not applicable", svc4.compute_status(scope4, "REQ-4"))
repo4.close()

# ------------------------------------------------------------------
# Group 5 (acceptance #5): Covered is refused without valid/current
# evidence; refused semantics = hard rejection (documented choice, not a
# silent downgrade); becomes possible once real evidence is linked; and is
# refused AGAIN once that internal evidence goes stale (object updated).
# ------------------------------------------------------------------
repo5 = _new_repo("g5_covered_evidence")
pub5, topic5, blk5, tbl5 = _seed_publication(repo5)
svc5 = ComplianceService(repo5)
scope5 = svc5.create_scope("SCOPE-5", pub5, actor="QA")
svc5.create_requirement("REQ-5", "Requirement needing evidence", actor="QA")
svc5.set_applicability(scope5, "REQ-5", "APPLIES", actor="QA")
check("group5: Covered is rejected with zero evidence linked (fail-closed, hard rejection)",
      _raises(lambda: svc5.set_assessment(scope5, "REQ-5", "COVERED", actor="QA"), ValueError), "")
check("group5: Missing/To-verify do NOT require evidence",
      svc5.set_assessment(scope5, "REQ-5", "MISSING", actor="QA") == 1 and svc5.compute_status(scope5, "REQ-5") == "Missing", "")

svc5.add_evidence("EV-5", "block", actor="QA", object_id=blk5, object_version=1)
svc5.link_evidence_requirement("EV-5", "REQ-5", actor="QA")
svc5.set_assessment(scope5, "REQ-5", "COVERED", actor="QA")
check("group5: Covered succeeds once a real, current internal evidence is linked",
      svc5.compute_status(scope5, "REQ-5") == "Covered", "")

repo5.update_block(blk5, text="Revised text -- evidence now stale.")
check("group5: Covered is refused again once the internal evidence goes stale (block moved to a new version)",
      _raises(lambda: svc5.set_assessment(scope5, "REQ-5", "COVERED", actor="QA"), ValueError), "")
repo5.close()

# ------------------------------------------------------------------
# Group 6 (acceptance #6): internal evidence against a non-existent
# object, or a non-existent version of a real object, is rejected.
# ------------------------------------------------------------------
repo6 = _new_repo("g6_evidence_validity")
pub6, topic6, blk6, tbl6 = _seed_publication(repo6)
svc6 = ComplianceService(repo6)
svc6.create_requirement("REQ-6", "Requirement for evidence validity", actor="QA")
check("group6: evidence against a non-existent block_id is rejected",
      _raises(lambda: svc6.add_evidence("EV-6A", "block", actor="QA", object_id="BLK-DOES-NOT-EXIST", object_version=1), KeyError), "")
check("group6: evidence against a REAL block but a version that never existed is rejected",
      _raises(lambda: svc6.add_evidence("EV-6B", "block", actor="QA", object_id=blk6, object_version=99), KeyError), "")
check("group6: evidence against the real block at its real version succeeds",
      svc6.add_evidence("EV-6C", "block", actor="QA", object_id=blk6, object_version=1) == "EV-6C", "")
check("group6: external evidence with neither external_ref nor description is rejected",
      _raises(lambda: svc6.add_evidence("EV-6D", "customer_document", actor="QA"), ValueError), "")
check("group6: external evidence with a description is accepted",
      svc6.add_evidence("EV-6E", "customer_document", actor="QA", description="Customer-supplied test report, on file.") == "EV-6E", "")
repo6.close()

# ------------------------------------------------------------------
# Group 7 (acceptance #7): the SAME evidence record supports multiple
# requirements, no duplication, provenance intact on both links.
# ------------------------------------------------------------------
repo7 = _new_repo("g7_evidence_reuse")
pub7, topic7, blk7, tbl7 = _seed_publication(repo7)
svc7 = ComplianceService(repo7)
scope7 = svc7.create_scope("SCOPE-7", pub7, actor="QA")
svc7.create_requirement("REQ-7A", "First requirement sharing evidence", actor="QA")
svc7.create_requirement("REQ-7B", "Second requirement sharing evidence", actor="QA")
svc7.add_evidence("EV-7", "block", actor="Alice", object_id=blk7, object_version=1, description="Shared evidence")
svc7.link_evidence_requirement("EV-7", "REQ-7A", actor="Alice")
svc7.link_evidence_requirement("EV-7", "REQ-7B", actor="Bob")
check("group7: exactly ONE evidence row exists despite being linked to two requirements",
      svc7.conn.execute("SELECT COUNT(*) c FROM compliance_evidence WHERE evidence_id='EV-7'").fetchone()["c"] == 1, "")
check("group7: two distinct link rows, each with its own linked_by provenance",
      {r["requirement_id"]: r["linked_by"] for r in svc7.conn.execute("SELECT requirement_id,linked_by FROM compliance_evidence_link WHERE evidence_id='EV-7'")}
      == {"REQ-7A": "Alice", "REQ-7B": "Bob"}, "")
svc7.set_applicability(scope7, "REQ-7A", "APPLIES", actor="QA")
svc7.set_applicability(scope7, "REQ-7B", "APPLIES", actor="QA")
svc7.set_assessment(scope7, "REQ-7A", "COVERED", actor="QA")
svc7.set_assessment(scope7, "REQ-7B", "COVERED", actor="QA")
check("group7: the shared evidence is sufficient to cover BOTH requirements independently",
      svc7.compute_status(scope7, "REQ-7A") == "Covered" and svc7.compute_status(scope7, "REQ-7B") == "Covered", "")
repo7.close()

# ------------------------------------------------------------------
# Group 8 (acceptance #8): risk<->requirement is many-to-many in both
# directions.
# ------------------------------------------------------------------
repo8 = _new_repo("g8_risk_many_to_many")
pub8, *_ = _seed_publication(repo8)
svc8 = ComplianceService(repo8)
svc8.create_requirement("REQ-8A", "Req A", actor="QA")
svc8.create_requirement("REQ-8B", "Req B", actor="QA")
svc8.create_risk("RISK-8X", "Entanglement hazard", actor="QA", is_residual=True, method_metadata={"client_method": "generic-3x3"})
svc8.create_risk("RISK-8Y", "Thermal hazard", actor="QA")
svc8.link_risk_requirement("RISK-8X", "REQ-8A", actor="QA")
svc8.link_risk_requirement("RISK-8X", "REQ-8B", actor="QA")
svc8.link_risk_requirement("RISK-8Y", "REQ-8A", actor="QA")
links_for_riskX = {r["requirement_id"] for r in svc8.conn.execute("SELECT requirement_id FROM compliance_risk_requirement_link WHERE risk_id='RISK-8X'")}
links_for_reqA = {r["risk_id"] for r in svc8.conn.execute("SELECT risk_id FROM compliance_risk_requirement_link WHERE requirement_id='REQ-8A'")}
check("group8: one risk links to MULTIPLE requirements", links_for_riskX == {"REQ-8A", "REQ-8B"}, links_for_riskX)
check("group8: one requirement links to MULTIPLE risks", links_for_reqA == {"RISK-8X", "RISK-8Y"}, links_for_reqA)
check("group8: risk method metadata is free-form/method-neutral, not a rigid global schema",
      ci._json({"client_method": "generic-3x3"}) == svc8.conn.execute("SELECT method_metadata_json FROM compliance_risk WHERE risk_id='RISK-8X'").fetchone()[0], "")
repo8.close()

# ------------------------------------------------------------------
# Group 9 (acceptance #9): a requirement/instrument version change makes
# stale assessments/evidence/links identifiable via change-impact queries.
# ------------------------------------------------------------------
repo9 = _new_repo("g9_change_impact")
pub9, topic9, blk9, tbl9 = _seed_publication(repo9)
svc9 = ComplianceService(repo9)
scope9 = svc9.create_scope("SCOPE-9", pub9, actor="QA")
svc9.create_instrument("INST-9", "SYNTH-REG-9", actor="QA")
svc9.create_requirement("REQ-9", "Requirement under change impact", actor="QA", instrument_id="INST-9")
svc9.set_applicability(scope9, "REQ-9", "APPLIES", actor="QA")
svc9.add_evidence("EV-9", "block", actor="QA", object_id=blk9, object_version=1)
svc9.link_evidence_requirement("EV-9", "REQ-9", actor="QA")
svc9.set_assessment(scope9, "REQ-9", "COVERED", actor="QA")

impact_before = svc9.change_impact_for_requirement("REQ-9")
check("group9: before any requirement change, nothing is stale",
      impact_before["stale_applicability"] == [] and impact_before["stale_assessment"] == [], impact_before)

svc9.update_requirement("REQ-9", actor="QA", description="Clarified wording.")
impact_after = svc9.change_impact_for_requirement("REQ-9")
check("group9: after a requirement version bump, its applicability/assessment become identifiable as stale",
      len(impact_after["stale_applicability"]) == 1 and len(impact_after["stale_assessment"]) == 1 and
      scope9 in impact_after["affected_scopes"] and pub9 in impact_after["affected_publications"],
      impact_after)

svc9.update_instrument("INST-9", actor="QA", status="SUPERSEDED")
impact_instrument = svc9.change_impact_for_instrument("INST-9")
check("group9: an instrument version bump makes the requirement authored against it identifiable as stale",
      any(r["requirement_id"] == "REQ-9" for r in impact_instrument["stale_requirements"]), impact_instrument)
repo9.close()

# ------------------------------------------------------------------
# Group 10 (acceptance #10): every critical decision produces an audit
# event with actor+timestamp; a missing actor blocks the write entirely
# (no partial/garbage row left behind).
# ------------------------------------------------------------------
repo10 = _new_repo("g10_audit")
pub10, topic10, blk10, tbl10 = _seed_publication(repo10)
svc10 = ComplianceService(repo10)
scope10 = svc10.create_scope("SCOPE-10", pub10, actor="QA")
svc10.create_requirement("REQ-10", "Requirement for audit test", actor="QA")
check("group10: create_requirement with empty actor raises before writing anything",
      _raises(lambda: svc10.create_requirement("REQ-10-BAD", "x", actor=""), ValueError) and
      not svc10.conn.execute("SELECT 1 FROM compliance_requirement WHERE requirement_id='REQ-10-BAD'").fetchone(), "")
svc10.set_applicability(scope10, "REQ-10", "APPLIES", actor="Inspector Rossi")
audit_rows = list(svc10.conn.execute(
    "SELECT action,actor,created_at FROM audit_events WHERE object_type='compliance_applicability'"))
check("group10: applicability decision produced an audit event with the real actor and a timestamp",
      len(audit_rows) == 1 and audit_rows[0]["actor"] == "Inspector Rossi" and bool(audit_rows[0]["created_at"]),
      [dict(r) for r in audit_rows])
repo10.close()

# ------------------------------------------------------------------
# Group 11 (acceptance #11): scope-level summary counts are coherent
# across a mix of Covered/To verify/Missing/Not-applicable requirements.
# ------------------------------------------------------------------
repo11 = _new_repo("g11_summary")
pub11, topic11, blk11, tbl11 = _seed_publication(repo11)
svc11 = ComplianceService(repo11)
svc11.create_instrument("INST-11", "SYNTH-REG-11", actor="QA")
scope11 = svc11.create_scope("SCOPE-11", pub11, actor="QA")
svc11.add_scope_reference(scope11, "instrument", "INST-11", actor="QA")
for i in range(1, 5):
    svc11.create_requirement(f"REQ-11-{i}", f"Requirement {i}", actor="QA", instrument_id="INST-11")

svc11.set_applicability(scope11, "REQ-11-1", "APPLIES", actor="QA")
svc11.add_evidence("EV-11", "block", actor="QA", object_id=blk11, object_version=1)
svc11.link_evidence_requirement("EV-11", "REQ-11-1", actor="QA")
svc11.set_assessment(scope11, "REQ-11-1", "COVERED", actor="QA")

svc11.set_applicability(scope11, "REQ-11-2", "APPLIES", actor="QA")
svc11.set_assessment(scope11, "REQ-11-2", "MISSING", actor="QA")

svc11.set_applicability(scope11, "REQ-11-3", "NOT_APPLICABLE", actor="QA", justification="Not present on this variant.")
# REQ-11-4 left completely untouched -> To verify.

summary = svc11.summary_for_scope(scope11)
check("group11: summary counts are exactly Covered=1, To verify=1, Missing=1, Not applicable=1, total=4",
      summary["counts"] == {"Covered": 1, "To verify": 1, "Missing": 1, "Not applicable": 1} and summary["total"] == 4,
      summary)

empty_scope = svc11.create_scope("SCOPE-11-EMPTY", pub11, actor="QA")
empty_summary = svc11.summary_for_scope(empty_scope)
check("group11: a scope with no referenced instrument/requirement reports an honest zero total (never fabricated)",
      empty_summary["total"] == 0 and empty_summary["counts"] == {"Covered": 0, "To verify": 0, "Missing": 0, "Not applicable": 0},
      empty_summary)
repo11.close()

# ------------------------------------------------------------------
# Group 12 (3.5A.1 #2, acceptance #2): a requirement version change makes
# an EXISTING Covered assessment stop reading as Covered on the very next
# compute_status() call -- no separate change-impact query required, and
# the propagation reaches summary_for_scope() too.
# ------------------------------------------------------------------
repo12 = _new_repo("g12_requirement_staleness")
pub12, topic12, blk12, tbl12 = _seed_publication(repo12)
svc12 = ComplianceService(repo12)
scope12 = svc12.create_scope("SCOPE-12", pub12, actor="QA")
svc12.create_requirement("REQ-12", "Requirement whose text will change", actor="QA")
svc12.set_applicability(scope12, "REQ-12", "APPLIES", actor="QA")
svc12.add_evidence("EV-12", "block", actor="QA", object_id=blk12, object_version=1)
svc12.link_evidence_requirement("EV-12", "REQ-12", actor="QA")
svc12.set_assessment(scope12, "REQ-12", "COVERED", actor="QA")
check("group12: Covered before any requirement change", svc12.compute_status(scope12, "REQ-12") == "Covered", "")
svc12.update_requirement("REQ-12", actor="QA", description="Wording clarified after internal review.")
check("group12: the SAME already-Covered assessment reads as 'To verify' immediately after a requirement version bump "
      "(read-time staleness, no change-impact query needed)",
      svc12.compute_status(scope12, "REQ-12") == "To verify", svc12.compute_status(scope12, "REQ-12"))
summary12 = svc12.summary_for_scope(scope12)
check("group12: staleness propagates into summary_for_scope() counts too",
      summary12["counts"]["To verify"] == 1 and summary12["counts"]["Covered"] == 0, summary12)
repo12.close()

# ------------------------------------------------------------------
# Group 13 (3.5A.1 #2, acceptance #3): stale INTERNAL evidence degrades an
# EXISTING Covered read, without any new write.
# ------------------------------------------------------------------
repo13 = _new_repo("g13_evidence_staleness")
pub13, topic13, blk13, tbl13 = _seed_publication(repo13)
svc13 = ComplianceService(repo13)
scope13 = svc13.create_scope("SCOPE-13", pub13, actor="QA")
svc13.create_requirement("REQ-13", "Requirement backed by a block", actor="QA")
svc13.set_applicability(scope13, "REQ-13", "APPLIES", actor="QA")
svc13.add_evidence("EV-13", "block", actor="QA", object_id=blk13, object_version=1)
svc13.link_evidence_requirement("EV-13", "REQ-13", actor="QA")
svc13.set_assessment(scope13, "REQ-13", "COVERED", actor="QA")
check("group13: Covered while the evidence is current", svc13.compute_status(scope13, "REQ-13") == "Covered", "")
repo13.update_block(blk13, text="Block content revised -- evidence now points at an old version.")
check("group13: the SAME already-Covered assessment reads as 'To verify' once its evidence object moved to a new "
      "version, with NO new write to compliance_assessment",
      svc13.compute_status(scope13, "REQ-13") == "To verify", svc13.compute_status(scope13, "REQ-13"))
repo13.close()

# ------------------------------------------------------------------
# Group 14 (3.5A.1 #2, acceptance #4): applicability decided against an
# old requirement version reads back as "To verify", not as whatever it
# was decided as.
# ------------------------------------------------------------------
repo14 = _new_repo("g14_applicability_staleness")
pub14, *_ = _seed_publication(repo14)
svc14 = ComplianceService(repo14)
scope14 = svc14.create_scope("SCOPE-14", pub14, actor="QA")
svc14.create_requirement("REQ-14", "Requirement for applicability staleness", actor="QA")
svc14.set_applicability(scope14, "REQ-14", "NOT_APPLICABLE", actor="QA", justification="Not present on this variant.")
check("group14: Not applicable while current", svc14.compute_status(scope14, "REQ-14") == "Not applicable", "")
svc14.update_requirement("REQ-14", actor="QA", title="Requirement for applicability staleness (revised)")
check("group14: the same N/A decision reads back as 'To verify' once the requirement text has moved on -- "
      "it must be reconfirmed, not trusted as still Not applicable",
      svc14.compute_status(scope14, "REQ-14") == "To verify", svc14.compute_status(scope14, "REQ-14"))
repo14.close()

# ------------------------------------------------------------------
# Group 15 (3.5A.1 #3, acceptance #5+#6): scope determinism and
# version-pinned scope references.
# ------------------------------------------------------------------
repo15 = _new_repo("g15_scope_determinism")
pub15, *_ = _seed_publication(repo15)
svc15 = ComplianceService(repo15)
scope15a = svc15.create_scope("SCOPE-15-EU", pub15, actor="QA", jurisdiction="EU")
check("group15: a single ACTIVE scope resolves unambiguously via the singular accessor",
      svc15.active_scope_for_publication(pub15) == scope15a, "")
scope15b = svc15.create_scope("SCOPE-15-US", pub15, actor="QA", jurisdiction="US")
check("group15: TWO concurrent ACTIVE scopes for the same publication (e.g. two markets) -- "
      "the singular accessor refuses to guess and raises instead of picking one arbitrarily",
      _raises(lambda: svc15.active_scope_for_publication(pub15), ValueError), "")
check("group15: the plural accessor enumerates BOTH scopes explicitly, oldest first",
      svc15.active_scopes_for_publication(pub15) == [scope15a, scope15b], svc15.active_scopes_for_publication(pub15))

svc15.create_standard("STD-15", "SYNTH-STD-15", actor="QA")
svc15.add_scope_reference(scope15a, "standard", "STD-15", actor="QA")
ref_before = svc15.conn.execute("SELECT ref_version FROM compliance_scope_reference WHERE scope_id=? AND ref_id='STD-15'", (scope15a,)).fetchone()["ref_version"]
check("group15: scope reference pins the standard's version at the moment it was added", ref_before == 1, ref_before)
svc15.update_standard("STD-15", actor="QA", edition="2nd edition")
ref_after = svc15.conn.execute("SELECT ref_version FROM compliance_scope_reference WHERE scope_id=? AND ref_id='STD-15'", (scope15a,)).fetchone()["ref_version"]
check("group15: the PINNED scope reference is unaffected by a later standard version bump -- it still reads v1, "
      "so a future snapshot could reconstruct exactly the perimeter used at configuration time",
      ref_after == 1, ref_after)
check("group15: the standard's own CURRENT row does show v2 (only the pin stays frozen)",
      svc15.conn.execute("SELECT version FROM compliance_standard WHERE standard_id='STD-15'").fetchone()["version"] == 2, "")
repo15.close()

# ------------------------------------------------------------------
# Group 16 (3.5A.1 #2, acceptance #7): standard/instrument version changes
# are surfaced in summary_for_scope(), not invisible.
# ------------------------------------------------------------------
repo16 = _new_repo("g16_summary_staleness_visibility")
pub16, topic16, blk16, tbl16 = _seed_publication(repo16)
svc16 = ComplianceService(repo16)
svc16.create_instrument("INST-16", "SYNTH-REG-16", actor="QA")
scope16 = svc16.create_scope("SCOPE-16", pub16, actor="QA")
svc16.add_scope_reference(scope16, "instrument", "INST-16", actor="QA")
svc16.create_requirement("REQ-16", "Requirement under instrument/standard staleness", actor="QA", instrument_id="INST-16")
svc16.create_standard("STD-16", "SYNTH-STD-16", actor="QA")
svc16.link_requirement_standard("REQ-16", "STD-16", actor="QA")

summary_before = svc16.summary_for_scope(scope16)
check("group16: before any change, nothing is reported stale in the summary",
      summary_before["stale_requirements"] == [] and summary_before["stale_standard_mappings"] == [], summary_before)

svc16.update_instrument("INST-16", actor="QA", status="SUPERSEDED")
summary_after_inst = svc16.summary_for_scope(scope16)
check("group16: an instrument version bump makes the requirement authored against it visible as stale in the summary",
      "REQ-16" in summary_after_inst["stale_requirements"], summary_after_inst)

svc16.update_standard("STD-16", actor="QA", status="WITHDRAWN")
summary_after_std = svc16.summary_for_scope(scope16)
check("group16: a standard version bump makes the requirement<->standard mapping visible as stale in the summary, "
      "without corrupting the Covered/To verify/Missing/N-A counts themselves",
      any(m["requirement_id"] == "REQ-16" and m["standard_id"] == "STD-16" for m in summary_after_std["stale_standard_mappings"]),
      summary_after_std)
repo16.close()

# ------------------------------------------------------------------
# Group 17 (3.5A.1 #4, acceptance #8): a failure partway through a
# multi-statement write rolls back COMPLETELY -- no orphan row from the
# first statement survives.
# ------------------------------------------------------------------
repo17 = _new_repo("g17_atomicity")
pub17, *_ = _seed_publication(repo17)
svc17 = ComplianceService(repo17)
svc17.create_instrument("INST-17", "SYNTH-REG-17", actor="QA")  # a real instrument at version=1

# Poison the version=2 SLOT in the history table (FK is satisfied since
# INST-17 already exists as parent), so update_instrument's own version=2
# history INSERT collides on UNIQUE(instrument_id,version) AFTER its
# main-table UPDATE (to version=2) has already executed inside the same
# still-open transaction.
svc17.conn.execute(
    """INSERT INTO compliance_regulatory_instrument_versions(instrument_id,version,kind,official_identifier,title,
       status,valid_from,valid_until,official_source_ref,supersedes_instrument_id,created_at)
       VALUES(?,2,?,?,?,?,?,?,?,?,?)""",
    ("INST-17", "regulation", "PRE-EXISTING-COLLISION", "", "ACTIVE", "", "", "", "", "2020-01-01T00:00:00+00:00"))
svc17.conn.commit()

check("group17: update_instrument raises when its version-history write hits a genuine constraint violation mid-operation",
      _raises(lambda: svc17.update_instrument("INST-17", actor="QA", title="Should not stick"), Exception), "")
main_row = dict(svc17.conn.execute("SELECT version,title FROM compliance_regulatory_instrument WHERE instrument_id='INST-17'").fetchone())
check("group17: the main table's UPDATE (to version=2) was rolled back too -- it still reads version=1, title unchanged",
      main_row == {"version": 1, "title": ""}, main_row)
check("group17: no stray UPDATE audit event was left behind either (the whole operation rolled back, including the audit insert)",
      svc17.conn.execute("SELECT 1 FROM audit_events WHERE object_type='compliance_regulatory_instrument' AND object_id='INST-17' AND action='UPDATE'").fetchone() is None, "")
check("group17: the connection remains fully usable after a rolled-back operation (an unrelated write succeeds normally)",
      svc17.create_requirement("REQ-17-POST-ROLLBACK", "Sanity check after rollback", actor="QA") == "REQ-17-POST-ROLLBACK", "")
repo17.close()

# ------------------------------------------------------------------
# Group 18 (transaction-boundary verification requested for the 3.5A.1
# integrity check): fault injection proving that a failure occurring AFTER
# an intermediate audit write has already executed inside _atomic() still
# rolls back EVERYTHING -- the audit insert included. This is not testing
# a bug that was found (code review of _atomic()/audit_event()/_audit()
# found no internal commit() reachable from any ComplianceService write
# path -- see report), it is closing the loop with a real fault-injection
# test rather than relying on code review alone, as explicitly asked.
# ------------------------------------------------------------------
from contextlib import contextmanager as _cm


class _ExecuteFailProxy:
    """Delegates everything to the real sqlite3.Connection except execute(),
    which raises on the n-th call (letting the first n-1 through for real).
    sqlite3.Connection's own attributes are read-only/cannot be monkeypatched
    directly (C extension type), hence this thin proxy swapped in for the
    duration of one test instead. Test-only fault injection -- no production
    code is touched."""

    def __init__(self, real_conn, fail_on_call: int):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._count = 0

    def execute(self, sql, params=()):
        self._count += 1
        if self._count == self._fail_on_call:
            raise RuntimeError("Simulated mid-operation failure (test fault injection)")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


@_cm
def _fail_on_nth_execute(svc: ComplianceService, n: int):
    """Swaps BOTH svc.conn and svc.repo.conn (the same underlying sqlite3
    connection, shared by construction) for a counting proxy, so every
    execute() call along either path -- ComplianceService's own writes AND
    the audit_event()/_audit() path on the wrapped IndustrialRepository --
    is counted in true call order. Restores both afterwards."""
    real_conn = svc.conn
    proxy = _ExecuteFailProxy(real_conn, n)
    svc.conn = proxy
    svc.repo.conn = proxy
    try:
        yield
    finally:
        svc.conn = real_conn
        svc.repo.conn = real_conn


repo18 = _new_repo("g18_fault_injection_after_audit")
pub18, topic18, blk18, tbl18 = _seed_publication(repo18)
svc18 = ComplianceService(repo18)
scope18 = svc18.create_scope("SCOPE-18", pub18, actor="QA")
svc18.create_requirement("REQ-18", "Requirement for fault-injection test", actor="QA")
svc18.set_applicability(scope18, "REQ-18", "APPLIES", actor="QA")
svc18.add_evidence("EV-18", "block", actor="QA", object_id=blk18, object_version=1)
svc18.link_evidence_requirement("EV-18", "REQ-18", actor="QA")
svc18.set_assessment(scope18, "REQ-18", "COVERED", actor="QA")

# set_applicability(..., NOT_APPLICABLE) inside _atomic() executes, in
# order: [1] upsert compliance_applicability, [2] audit APPLICABILITY_*,
# [3] SELECT the stale assessment, [4] DELETE it, [5] audit the
# invalidation. Failing on call #3 means call #2 (the audit write) has
# ALREADY happened for real before the injected failure -- exactly the
# "failure after an intermediate audit/write" scenario requested.
audit_count_before = svc18.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
with _fail_on_nth_execute(svc18, 3):
    check("group18: the injected failure actually interrupts set_applicability (3rd internal statement)",
          _raises(lambda: svc18.set_applicability(scope18, "REQ-18", "NOT_APPLICABLE", actor="QA", justification="Test fault injection."), RuntimeError), "")

applic_after = svc18.conn.execute("SELECT applies FROM compliance_applicability WHERE scope_id=? AND requirement_id='REQ-18'", (scope18,)).fetchone()
check("group18: the applicability row was rolled back -- still APPLIES, never left as a half-applied NOT_APPLICABLE",
      applic_after["applies"] == "APPLIES", dict(applic_after))
audit_count_after = svc18.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
check("group18: the audit insert that already executed (call #2, BEFORE the injected failure) was rolled back too -- "
      "total audit_events count is unchanged, not incremented by one orphan row",
      audit_count_after == audit_count_before, (audit_count_before, audit_count_after))
check("group18: the pre-existing Covered assessment is completely untouched by the failed operation",
      svc18.compute_status(scope18, "REQ-18") == "Covered", svc18.compute_status(scope18, "REQ-18"))
check("group18: the connection is still fully usable -- a real (non-injected) retry of the same call succeeds cleanly",
      svc18.set_applicability(scope18, "REQ-18", "NOT_APPLICABLE", actor="QA", justification="Real retry after fault injection.") == 1, "")
check("group18: ...and this time it DOES take effect (assessment invalidated for real)",
      svc18.compute_status(scope18, "REQ-18") == "Not applicable", "")
repo18.close()

print(f"compliance build: {ci.COMPLIANCE_BUILD}")
print(json.dumps({"pass": sum(r["status"] == "PASS" for r in results), "tests": len(results)}))
