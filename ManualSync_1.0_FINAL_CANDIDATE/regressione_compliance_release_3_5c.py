"""Milestone 3.5C regression suite: Compliance Readiness & Release Snapshot.

Covers the integration of Compliance Intelligence (3.5A/3.5A.1) into the
readiness/release engine (release_readiness.py, release_manager.py,
ccms_industrial.IndustrialRepository.release_publication): a publication
with no active Compliance scope must behave exactly as before 3.5C; a
publication with one or more ACTIVE scopes must have Compliance join the
release gate automatically (no opt-out flag); a published release must
freeze a hash-verifiable Compliance snapshot; and the gate must hold no
matter which code path creates the release (the UI's create_release_bundle
or a direct call to the lower-level release_publication primitive).

Backend/integration only -- no browser. Fixtures are entirely synthetic
(SYNTH-*/INST-*/REQ-*): this suite does not encode or assert any real
regulatory text.
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from ccms_industrial import IndustrialRepository
from compliance_intelligence import ComplianceService
from project_lifecycle import create_project
from release_readiness import assess_release_readiness
from release_manager import create_release_bundle
import app

TMP = Path(tempfile.mkdtemp(prefix="manualsync_compliance_release_3_5c_"))
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


def _new_project(name: str) -> Path:
    """One project, minimal approved source content, no target languages --
    keeps the translation/readiness matrix trivially READY so every FAIL in
    this suite is attributable to Compliance, never to translation state."""
    p = create_project(TMP, project_name=name, source_lang="en-US")
    repo = IndustrialRepository(p / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("TOPIC-1", "Synthetic Section", "Synthetic body.", status="Approved")
        repo.create_publication("PUB-MAIN", "Synthetic Manual", ["TOPIC-1"], source_lang="en-US")
        repo.add_block("BLK-1", "TOPIC-1", "paragraph", position=1, text="Synthetic text.", status="Approved")
    finally:
        repo.close()
    return p


def _ready_fixture(name: str) -> tuple[Path, str, dict]:
    """One project, one ACTIVE scope, with exactly one valid justified N/A
    requirement and one valid Covered (current evidence) requirement --
    both non-blocking, so compliance_ready is True out of the box."""
    p = _new_project(name)
    repo = IndustrialRepository(p / "ManualSync_CCMS.sqlite")
    svc = ComplianceService(repo)
    try:
        svc.create_instrument("INST-OK", "SYNTH-REG-OK", actor="QA", title="Synthetic regulation")
        scope_id = svc.create_scope(f"SCOPE-{name}", "PUB-MAIN", actor="QA", jurisdiction="EU (sintetico)")
        svc.add_scope_reference(scope_id, "instrument", "INST-OK", actor="QA")

        svc.create_requirement("REQ-NA", "Not applicable requirement", actor="QA", instrument_id="INST-OK")
        svc.set_applicability(scope_id, "REQ-NA", "NOT_APPLICABLE", actor="QA",
                              justification="Not relevant to this synthetic product variant.")

        svc.create_requirement("REQ-OK", "Covered requirement", actor="QA", instrument_id="INST-OK")
        svc.set_applicability(scope_id, "REQ-OK", "APPLIES", actor="QA")
        svc.add_evidence("EV-OK", "block", actor="QA", object_id="BLK-1", object_version=1)
        svc.link_evidence_requirement("EV-OK", "REQ-OK", actor="QA")
        svc.set_assessment(scope_id, "REQ-OK", "COVERED", actor="QA")
    finally:
        repo.close()
    return p, scope_id, {"na": "REQ-NA", "ok": "REQ-OK"}


def _expect_blocked(project_dir: Path, release_id: str, out_subdir: str) -> str | None:
    """Attempt a release through the real release_manager entrypoint;
    returns the exception message on failure, None if it unexpectedly
    succeeded."""
    try:
        create_release_bundle(project_dir, "PUB-MAIN", target_langs=[], released_by="QA Regression",
                              release_id=release_id, out_dir=TMP / out_subdir)
        return None
    except ValueError as e:
        return str(e)


def _snapshot_row(project_dir: Path, release_id: str) -> dict:
    repo = IndustrialRepository(project_dir / "ManualSync_CCMS.sqlite")
    try:
        row = repo.conn.execute(
            "SELECT snapshot_json, snapshot_sha256 FROM publication_releases WHERE release_id=?", (release_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        repo.close()


# ------------------------------------------------------------------
# Group 1 (fixture #1): no active Compliance scope -> existing
# readiness/release behavior stays exactly as it was before 3.5C.
# ------------------------------------------------------------------
p1 = _new_project("g01_no_scope")
r1 = assess_release_readiness(p1, "PUB-MAIN")
check("g1: no active scope -> compliance_applicable is False", r1["compliance"]["compliance_applicable"] is False, r1["compliance"])
check("g1: no active scope -> compliance_ready is vacuously True (no artificial blocker)", r1["compliance"]["compliance_ready"] is True, r1["compliance"])
check("g1: overall release_ready driven only by the (unaffected) language matrix",
      r1["release_ready"] == r1["summary"]["language_ready"], r1["summary"])
bundle1 = create_release_bundle(p1, "PUB-MAIN", target_langs=[], released_by="QA Regression", release_id="REL-G1", out_dir=TMP / "g01_out")
check("g1: release with no Compliance scope succeeds unaffected", Path(bundle1["release_zip"]).exists(), bundle1["release_zip"])
row1 = _snapshot_row(p1, "REL-G1-en-US")
snap1 = json.loads(row1["snapshot_json"])
check("g1: the (non-applicable) Compliance state is still frozen into the snapshot for future audit",
      snap1["compliance"]["compliance_applicable"] is False, snap1["compliance"])

# ------------------------------------------------------------------
# Group 2 (fixture #2): active scope with a Missing requirement blocks the
# release, and Compliance auto-joins the gate with no opt-out.
# ------------------------------------------------------------------
p2 = _new_project("g02_missing_blocks")
repo = IndustrialRepository(p2 / "ManualSync_CCMS.sqlite")
svc = ComplianceService(repo)
svc.create_instrument("INST-2", "SYNTH-REG-2", actor="QA")
scope2 = svc.create_scope("SCOPE-2", "PUB-MAIN", actor="QA", jurisdiction="EU")
svc.add_scope_reference(scope2, "instrument", "INST-2", actor="QA")
svc.create_requirement("REQ-M", "Missing requirement", actor="QA", instrument_id="INST-2")
svc.set_applicability(scope2, "REQ-M", "APPLIES", actor="QA")
svc.set_assessment(scope2, "REQ-M", "MISSING", actor="QA", comment="No evidence yet.")
repo.close()

r2 = assess_release_readiness(p2, "PUB-MAIN")
check("g2: active scope automatically joins the gate (compliance_applicable True, no flag needed)",
      r2["compliance"]["compliance_applicable"] is True, r2["compliance"])
check("g2: a Missing requirement makes the publication not compliance_ready", r2["compliance"]["compliance_ready"] is False, r2["compliance"])
check("g2: overall release_ready is False even though the language matrix is READY",
      r2["release_ready"] is False and r2["summary"]["language_ready"] is True, r2["summary"])
check("g2: the Missing requirement is a readable blocking item with provenance",
      any(b["scope_id"] == "SCOPE-2" and b["requirement_id"] == "REQ-M" and b["status"] == "Missing"
          for b in r2["compliance"]["blocking_items"]), r2["compliance"]["blocking_items"])
msg2 = _expect_blocked(p2, "REL-G2", "g02_out")
check("g2: release_manager refuses to create a release while Compliance is blocked", msg2 is not None, msg2)
check("g2: the blocked-release error message names Compliance (not a silent/empty failure)", msg2 and "Compliance" in msg2, msg2)

# ------------------------------------------------------------------
# Group 3 (fixture #3): active scope with an unassessed ("To verify")
# requirement blocks the release.
# ------------------------------------------------------------------
p3 = _new_project("g03_to_verify_blocks")
repo = IndustrialRepository(p3 / "ManualSync_CCMS.sqlite")
svc = ComplianceService(repo)
svc.create_instrument("INST-3", "SYNTH-REG-3", actor="QA")
scope3 = svc.create_scope("SCOPE-3", "PUB-MAIN", actor="QA", jurisdiction="EU")
svc.add_scope_reference(scope3, "instrument", "INST-3", actor="QA")
svc.create_requirement("REQ-TV", "Unassessed requirement", actor="QA", instrument_id="INST-3")
svc.set_applicability(scope3, "REQ-TV", "APPLIES", actor="QA")
repo.close()

r3 = assess_release_readiness(p3, "PUB-MAIN")
check("g3: an unassessed (never decided) requirement blocks release", r3["compliance"]["compliance_ready"] is False, r3["compliance"])
check("g3: blocking item reports status 'To verify'",
      any(b["requirement_id"] == "REQ-TV" and b["status"] == "To verify" for b in r3["compliance"]["blocking_items"]),
      r3["compliance"]["blocking_items"])
msg3 = _expect_blocked(p3, "REL-G3", "g03_out")
check("g3: release blocked for the unassessed requirement", msg3 is not None, msg3)

# ------------------------------------------------------------------
# Group 4 (fixture #4): valid N/A (justified) + valid Covered (current
# evidence) -> compliance-ready, release succeeds.
# ------------------------------------------------------------------
p4, scope4, ids4 = _ready_fixture("g04_valid_na_and_covered")
r4 = assess_release_readiness(p4, "PUB-MAIN")
check("g4: justified N/A + current-evidence Covered -> compliance_ready True", r4["compliance"]["compliance_ready"] is True, r4["compliance"])
check("g4: no blocking items", r4["compliance"]["blocking_items"] == [], r4["compliance"]["blocking_items"])
bundle4 = create_release_bundle(p4, "PUB-MAIN", target_langs=[], released_by="QA Regression", release_id="REL-G4", out_dir=TMP / "g04_out")
check("g4: release succeeds when Compliance is ready", Path(bundle4["release_zip"]).exists(), bundle4["release_zip"])

# ------------------------------------------------------------------
# Group 5 (fixture #5): evidence becomes stale (backing block updated to a
# newer version after the Covered assessment) -> blocks the release.
# ------------------------------------------------------------------
p5, scope5, ids5 = _ready_fixture("g05_stale_evidence_blocks")
r5_before = assess_release_readiness(p5, "PUB-MAIN")
check("g5: starts compliance_ready", r5_before["compliance"]["compliance_ready"] is True, r5_before["compliance"])
repo = IndustrialRepository(p5 / "ManualSync_CCMS.sqlite")
repo.update_block("BLK-1", text="Synthetic text (revised after the Covered assessment).")
repo.close()
r5_after = assess_release_readiness(p5, "PUB-MAIN")
check("g5: stale evidence (backing object moved to a newer version) degrades Covered -> blocked",
      r5_after["compliance"]["compliance_ready"] is False, r5_after["compliance"])
check("g5: the stale requirement reads as 'To verify', not silently still Covered",
      any(b["requirement_id"] == ids5["ok"] and b["status"] == "To verify" for b in r5_after["compliance"]["blocking_items"]),
      r5_after["compliance"]["blocking_items"])
msg5 = _expect_blocked(p5, "REL-G5", "g05_out")
check("g5: release blocked once evidence went stale", msg5 is not None, msg5)

# ------------------------------------------------------------------
# Group 6 (fixture #6): two active scopes on the same publication ->
# deterministic aggregate semantics; a block in either scope blocks the
# whole release; provenance correctly attributes the blocker to its scope.
# ------------------------------------------------------------------
p6 = _new_project("g06_multi_scope")
repo = IndustrialRepository(p6 / "ManualSync_CCMS.sqlite")
svc = ComplianceService(repo)
# Two DISTINCT instruments -- requirements_in_scope() reaches a requirement
# through the instrument its scope references, so sharing one instrument
# across scopes would make each scope see the other's requirements too.
# Separate instruments keep the two scopes' requirement sets independent,
# matching how concurrent per-market scopes are actually used.
svc.create_instrument("INST-6A", "SYNTH-REG-6A", actor="QA")
svc.create_instrument("INST-6B", "SYNTH-REG-6B", actor="QA")
scope6a = svc.create_scope("SCOPE-6A", "PUB-MAIN", actor="QA", jurisdiction="EU")
svc.add_scope_reference(scope6a, "instrument", "INST-6A", actor="QA")
scope6b = svc.create_scope("SCOPE-6B", "PUB-MAIN", actor="QA", jurisdiction="US")
svc.add_scope_reference(scope6b, "instrument", "INST-6B", actor="QA")

svc.create_requirement("REQ-6A-NA", "A - not applicable", actor="QA", instrument_id="INST-6A")
svc.set_applicability(scope6a, "REQ-6A-NA", "NOT_APPLICABLE", actor="QA", justification="Out of scope for market A.")
svc.create_requirement("REQ-6A-OK", "A - covered", actor="QA", instrument_id="INST-6A")
svc.set_applicability(scope6a, "REQ-6A-OK", "APPLIES", actor="QA")
svc.add_evidence("EV-6A", "block", actor="QA", object_id="BLK-1", object_version=1)
svc.link_evidence_requirement("EV-6A", "REQ-6A-OK", actor="QA")
svc.set_assessment(scope6a, "REQ-6A-OK", "COVERED", actor="QA")

svc.create_requirement("REQ-6B-MISS", "B - missing", actor="QA", instrument_id="INST-6B")
svc.set_applicability(scope6b, "REQ-6B-MISS", "APPLIES", actor="QA")
svc.set_assessment(scope6b, "REQ-6B-MISS", "MISSING", actor="QA")
repo.close()

r6a = assess_release_readiness(p6, "PUB-MAIN")
r6b = assess_release_readiness(p6, "PUB-MAIN")
check("g6: two active scopes are both counted", r6a["compliance"]["active_scope_count"] == 2, r6a["compliance"])
check("g6: multi-scope evaluation is deterministic across repeated calls (same scope order)",
      [s["scope_id"] for s in r6a["compliance"]["scopes"]] == [s["scope_id"] for s in r6b["compliance"]["scopes"]] == ["SCOPE-6A", "SCOPE-6B"],
      ([s["scope_id"] for s in r6a["compliance"]["scopes"]], [s["scope_id"] for s in r6b["compliance"]["scopes"]]))
check("g6: one blocked scope makes the whole publication not compliance_ready", r6a["compliance"]["compliance_ready"] is False, r6a["compliance"])
check("g6: the blocking item is correctly attributed to SCOPE-6B only",
      all(b["scope_id"] == "SCOPE-6B" for b in r6a["compliance"]["blocking_items"])
      and any(b["requirement_id"] == "REQ-6B-MISS" for b in r6a["compliance"]["blocking_items"]),
      r6a["compliance"]["blocking_items"])
msg6 = _expect_blocked(p6, "REL-G6-BLOCKED", "g06_out")
check("g6: release blocked while scope B still has a Missing requirement", msg6 is not None, msg6)

repo = IndustrialRepository(p6 / "ManualSync_CCMS.sqlite")
svc = ComplianceService(repo)
svc.add_evidence("EV-6B", "block", actor="QA", object_id="BLK-1", object_version=1)
svc.link_evidence_requirement("EV-6B", "REQ-6B-MISS", actor="QA")
svc.set_assessment(scope6b, "REQ-6B-MISS", "COVERED", actor="QA")
repo.close()
r6c = assess_release_readiness(p6, "PUB-MAIN")
check("g6: fixing scope B makes the whole publication compliance_ready again", r6c["compliance"]["compliance_ready"] is True, r6c["compliance"])
bundle6 = create_release_bundle(p6, "PUB-MAIN", target_langs=[], released_by="QA Regression", release_id="REL-G6-OK", out_dir=TMP / "g06_out2")
row6 = _snapshot_row(p6, "REL-G6-OK-en-US")
snap6 = json.loads(row6["snapshot_json"])
check("g6: both scopes are embedded in the frozen release snapshot",
      {s["scope_id"] for s in snap6["compliance"]["scopes"]} == {"SCOPE-6A", "SCOPE-6B"}, snap6["compliance"])

# ------------------------------------------------------------------
# Group 7 (fixture #7): release PASS -> Compliance snapshot present and
# verifiable through the existing snapshot_json/snapshot_sha256 mechanism.
# ------------------------------------------------------------------
p7, scope7, ids7 = _ready_fixture("g07_release_snapshot_hash")
create_release_bundle(p7, "PUB-MAIN", target_langs=[], released_by="QA Regression", release_id="REL-G7", out_dir=TMP / "g07_out")
row7 = _snapshot_row(p7, "REL-G7-en-US")
check("g7: an immutable release row was written", row7 is not None, row7)
snap7 = json.loads(row7["snapshot_json"])
check("g7: the snapshot embeds the Compliance section", "compliance" in snap7 and snap7["compliance"]["compliance_applicable"] is True, snap7.get("compliance"))
check("g7: the frozen Compliance section reports the release as ready", snap7["compliance"]["compliance_ready"] is True, snap7["compliance"])
repo7 = IndustrialRepository(p7 / "ManualSync_CCMS.sqlite")
verify7 = repo7.verify_release_snapshot("REL-G7-en-US")
repo7.close()
check("g7: the real product verify_release_snapshot() primitive confirms integrity (not a hand-rolled test-only hash check)",
      verify7["verified"] is True, verify7)

# ------------------------------------------------------------------
# Group 8 (fixture #8): a later requirement edit must NOT change the
# historical release row or its hash; a NEW readiness check may change.
# ------------------------------------------------------------------
p8, scope8, ids8 = _ready_fixture("g08_immutable_after_change")
create_release_bundle(p8, "PUB-MAIN", target_langs=[], released_by="QA Regression", release_id="REL-G8", out_dir=TMP / "g08_out")
row8_before = _snapshot_row(p8, "REL-G8-en-US")

repo = IndustrialRepository(p8 / "ManualSync_CCMS.sqlite")
svc = ComplianceService(repo)
svc.update_requirement(ids8["ok"], actor="QA", title="Covered requirement (title changed after release)")
repo.close()

row8_after = _snapshot_row(p8, "REL-G8-en-US")
check("g8: the historical release row's snapshot_json is byte-identical after a later requirement edit",
      row8_before["snapshot_json"] == row8_after["snapshot_json"], "unchanged" if row8_before["snapshot_json"] == row8_after["snapshot_json"] else "CHANGED")
check("g8: the historical release row's hash is unchanged", row8_before["snapshot_sha256"] == row8_after["snapshot_sha256"], row8_after["snapshot_sha256"])

r8_new = assess_release_readiness(p8, "PUB-MAIN")
check("g8: a FRESH readiness check now sees the edited requirement as no longer ready (version pinning went stale)",
      r8_new["compliance"]["compliance_ready"] is False, r8_new["compliance"])

# ------------------------------------------------------------------
# Group 9 (fixture #9): tampering the stored snapshot must be caught by
# the real verify_release_snapshot() product primitive -- not just
# demonstrated by recomputing a hash inside the test itself.
# ------------------------------------------------------------------
p9, scope9, ids9 = _ready_fixture("g09_tamper_detection")
create_release_bundle(p9, "PUB-MAIN", target_langs=[], released_by="QA Regression", release_id="REL-G9", out_dir=TMP / "g09_out")
repo9 = IndustrialRepository(p9 / "ManualSync_CCMS.sqlite")
verify9_before = repo9.verify_release_snapshot("REL-G9-en-US")
check("g9: the untouched release verifies PASS before any tampering", verify9_before["verified"] is True, verify9_before)

row9 = repo9.conn.execute("SELECT snapshot_json FROM publication_releases WHERE release_id=?", ("REL-G9-en-US",)).fetchone()
tampered = row9["snapshot_json"].replace('"Covered"', '"COVERED_TAMPERED"', 1)
check("g9: the tamper actually altered the stored text (sanity check)", tampered != row9["snapshot_json"], None)
repo9.conn.execute("UPDATE publication_releases SET snapshot_json=? WHERE release_id=?", (tampered, "REL-G9-en-US"))
repo9.conn.commit()

verify9_after = repo9.verify_release_snapshot("REL-G9-en-US")
repo9.close()
check("g9: verify_release_snapshot() reports verified=False against a tampered row (real product primitive, not test-only)",
      verify9_after["verified"] is False, verify9_after)
check("g9: verify_release_snapshot() still reports the originally-expected hash for a human to compare",
      verify9_after["expected_sha256"] == verify9_before["actual_sha256"], (verify9_after, verify9_before))

# ------------------------------------------------------------------
# Group 10: the low-level primitive itself (IndustrialRepository.
# release_publication, called directly with commit=True, bypassing
# release_manager.create_release_bundle entirely) must still self-gate.
# ------------------------------------------------------------------
p10, scope10, ids10 = _ready_fixture("g10_direct_call_cannot_bypass")
repo = IndustrialRepository(p10 / "ManualSync_CCMS.sqlite")
svc = ComplianceService(repo)
svc.create_requirement("REQ-BLOCK-10", "Blocking requirement", actor="QA", instrument_id="INST-OK")
svc.set_applicability(scope10, "REQ-BLOCK-10", "APPLIES", actor="QA")
repo.close()

repo10 = IndustrialRepository(p10 / "ManualSync_CCMS.sqlite")


def _direct_call():
    repo10.release_publication("PUB-MAIN", release_id="REL-G10-DIRECT", released_by="Attempted Direct Bypass", commit=True)


direct_blocked = _raises(_direct_call, ValueError)
repo10.close()
check("g10: calling the low-level release_publication() directly (bypassing create_release_bundle) still refuses when Compliance is blocked",
      direct_blocked, direct_blocked)
check("g10: no publication_releases row was written by the refused direct call", _snapshot_row(p10, "REL-G10-DIRECT") is None, None)

# ------------------------------------------------------------------
# Group 11: the Release-tab UI readiness bundle stays non-destructive when
# Compliance blocks the release (fail-closed, no exception, no corrupted
# sibling outputs -- the same lesson as the 3.5B Compliance decision UI).
# ------------------------------------------------------------------
p11, scope11, ids11 = _ready_fixture("g11_ui_non_destructive")
repo = IndustrialRepository(p11 / "ManualSync_CCMS.sqlite")
svc = ComplianceService(repo)
svc.create_requirement("REQ-BLOCK-11", "Blocking requirement", actor="QA", instrument_id="INST-OK")
svc.set_applicability(scope11, "REQ-BLOCK-11", "APPLIES", actor="QA")
repo.close()

ui_result = app.readiness_ui(str(p11), "PUB-MAIN", [], "", "Collaudo interno")
check("g11: readiness_ui() returns its normal 5-item bundle without raising, even though Compliance blocks the release",
      isinstance(ui_result, tuple) and len(ui_result) == 5, (type(ui_result), len(ui_result) if isinstance(ui_result, tuple) else None))
msg_html = ui_result[0]
check("g11: the readiness card reports RELEASE BLOCCATA", "RELEASE BLOCCATA" in msg_html, msg_html)
check("g11: the readiness card reports Compliance richiede intervento", "richiede intervento" in msg_html, msg_html)
check("g11: the readiness card never claims a certification/false green checkmark for Compliance",
      "Conformità: pronta" not in msg_html, msg_html)
release_button_update = ui_result[4]
check("g11: the Release button stays disabled while Compliance blocks the release",
      release_button_update.get("interactive") is False, release_button_update)

# Sanity: the same UI path on the READY fixture (group 4) shows the
# positive Compliance state and re-enables the Release button.
ui_ready = app.readiness_ui(str(p4), "PUB-MAIN", [], "", "Collaudo interno")
check("g11: the ready fixture's readiness card reports Conformità: pronta", "Conformità: pronta" in ui_ready[0], ui_ready[0])
check("g11: the ready fixture's readiness card never claims a legal certification",
      not any(w in ui_ready[0] for w in ("certificato", "conforme automaticamente", "CE verificato", "compliance garantita")),
      ui_ready[0])
check("g11: the Release button is enabled once Compliance (and translation) are both ready",
      ui_ready[4].get("interactive") is True, ui_ready[4])

# ------------------------------------------------------------------
# Group 12 (3.5C integrity gate, TOCTOU fault-injection acceptance test):
# a Compliance write attempted from a SEPARATE connection while
# create_release_bundle holds its BEGIN IMMEDIATE lock -- i.e. exactly
# inside the window between the authoritative locked readiness read and
# the final commit -- must be unable to land. It must block against
# SQLite's write lock, never succeed silently inside that window.
#
# ComplianceService.compliance_readiness_for_publication() is called
# exactly twice per create_release_bundle run: once as the advisory
# pre-check at the top (before BEGIN IMMEDIATE), and once as the
# authoritative locked read right after it (see release_manager.py). This
# monkeypatch fires the concurrent write attempt on the SECOND call --
# i.e. from inside the lock -- and restores the original method
# immediately after, success or failure.
# ------------------------------------------------------------------
p12, scope12, ids12 = _ready_fixture("g12_toctou_concurrent_write_blocked")
db12 = str(p12 / "ManualSync_CCMS.sqlite")

concurrent_attempt: dict = {}


def _attempt_concurrent_compliance_write():
    conn2 = sqlite3.connect(db12, timeout=0.3)
    try:
        conn2.execute("BEGIN IMMEDIATE")
        conn2.execute("UPDATE compliance_assessment SET status='MISSING' WHERE requirement_id=?", (ids12["ok"],))
        conn2.commit()
        concurrent_attempt["outcome"] = "SUCCEEDED"
    except sqlite3.OperationalError as e:
        concurrent_attempt["outcome"] = "BLOCKED"
        concurrent_attempt["detail"] = str(e)
    finally:
        conn2.close()


_orig_readiness_method = ComplianceService.compliance_readiness_for_publication
_call_count = {"n": 0}


def _patched_readiness_method(self, publication_id):
    _call_count["n"] += 1
    result = _orig_readiness_method(self, publication_id)
    if _call_count["n"] == 2:
        t = threading.Thread(target=_attempt_concurrent_compliance_write)
        t.start()
        t.join(timeout=2.0)
    return result


ComplianceService.compliance_readiness_for_publication = _patched_readiness_method
try:
    bundle12 = create_release_bundle(p12, "PUB-MAIN", target_langs=[], released_by="QA Regression",
                                     release_id="REL-G12", out_dir=TMP / "g12_out")
finally:
    ComplianceService.compliance_readiness_for_publication = _orig_readiness_method

check("g12: compliance_readiness_for_publication() was called exactly twice (advisory + authoritative-locked), as designed",
      _call_count["n"] == 2, _call_count)
check("g12: a concurrent Compliance write attempted exactly inside the BEGIN IMMEDIATE window was BLOCKED, not silently applied",
      concurrent_attempt.get("outcome") == "BLOCKED", concurrent_attempt)
check("g12: the release itself still succeeded (the lock was released cleanly after commit)",
      Path(bundle12["release_zip"]).exists(), bundle12.get("release_zip"))

row12 = _snapshot_row(p12, "REL-G12-en-US")
snap12 = json.loads(row12["snapshot_json"])
check("g12: the frozen snapshot reflects the pre-attempt READY state -- the concurrent write never landed inside the gate/commit window",
      snap12["compliance"]["compliance_ready"] is True, snap12["compliance"])

# The blocked write was not silently lost -- retried now that the lock is
# free, it succeeds, and a FRESH readiness call correctly picks it up.
conn2 = sqlite3.connect(db12)
conn2.execute("UPDATE compliance_assessment SET status='MISSING' WHERE requirement_id=?", (ids12["ok"],))
conn2.commit()
conn2.close()
r12_after = assess_release_readiness(p12, "PUB-MAIN")
check("g12: retried after the lock is released, the write succeeds and a fresh readiness call sees it",
      r12_after["compliance"]["compliance_ready"] is False, r12_after["compliance"])


print(json.dumps({"pass": sum(r["status"] == "PASS" for r in results), "tests": len(results)}))
if any(r["status"] != "PASS" for r in results):
    print(json.dumps(results, indent=2))
    sys.exit(1)
