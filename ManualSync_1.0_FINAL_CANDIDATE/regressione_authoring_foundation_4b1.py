"""Milestone 4B.1 regression suite: Authoring Foundation & Review Repair.

Tests AuthoringService directly (the real service, not a mock), plus the
two app.py UI callbacks that fix the 4A-audit review-lifecycle bug
(review_decide_ui / approve_source_ui via AuthoringService.bulk_approve_publication).

Covers all 22 acceptance criteria from the 4B.1 report, in order, plus a
dedicated regression test for the exact bug found in the 4A audit:
decide_review() was never reachable from the product path app.py actually
serves, and bulk source approval left OPEN review_tasks orphaned forever.

Backend/integration only -- no browser. Fixtures are entirely synthetic.
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService, VersionConflictError, ArchivedObjectError
from project_lifecycle import create_project
from release_readiness import assess_release_readiness
import app

TMP = Path(tempfile.mkdtemp(prefix="manualsync_authoring_4b1_"))
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


def _seed_topic_with_blocks(repo: IndustrialRepository, *, topic_id="TOPIC-1", n_blocks=2) -> list[str]:
    repo.create_topic(topic_id, "Synthetic Topic", "legacy body", status="Draft",
                      source_refs=["legacy#page=3"])
    block_ids = []
    for i in range(1, n_blocks + 1):
        bid = f"{topic_id}-BLK-{i}"
        repo.add_block(bid, topic_id, "paragraph", position=i, text=f"Synthetic block {i}.", status="Draft")
        block_ids.append(bid)
    return block_ids


def _seed_publication(repo: IndustrialRepository, *, publication_id="PUB-1", topic_ids=None) -> str:
    topic_ids = topic_ids or ["TOPIC-1"]
    repo.create_publication(publication_id, "Synthetic Manual", topic_ids)
    return publication_id


# ------------------------------------------------------------------
# Group 1 (AC#1): draft recovery does NOT create a content version.
# ------------------------------------------------------------------
repo1 = _new_repo("g01_draft_no_version")
repo1.create_topic("T1", "Original title", "Original body", status="Draft")
svc1 = AuthoringService(repo1)
svc1.save_draft("topic", "T1", base_version=1, payload={"title": "Draft title", "body": "Draft body"}, actor="Writer A")
row = repo1.conn.execute("SELECT version FROM topics WHERE topic_id='T1'").fetchone()
check("g1: draft save does not bump the live topic's version", int(row["version"]) == 1, row["version"])
vcount = repo1.conn.execute("SELECT COUNT(*) c FROM topic_versions WHERE topic_id='T1'").fetchone()["c"]
check("g1: draft save does not insert a topic_versions row", vcount == 1, vcount)

# ------------------------------------------------------------------
# Group 2 (AC#2): draft survives a new instance/process.
# ------------------------------------------------------------------
repo1.close()
repo1b = _new_repo("g01_draft_no_version")  # reopen same DB file, fresh connection/service
svc1b = AuthoringService(repo1b)
draft = svc1b.load_draft("topic", "T1")
check("g2: draft survives a brand new IndustrialRepository/AuthoringService instance",
      draft is not None and draft["payload"]["title"] == "Draft title", draft)
repo1b.close()

# ------------------------------------------------------------------
# Group 3 (AC#3) + Group 4 (AC#4): Save from draft creates exactly ONE new
# content version, and the draft is deleted only after success.
# ------------------------------------------------------------------
repo3 = _new_repo("g03_save_from_draft")
repo3.create_topic("T3", "Original title", "Original body", status="Draft")
svc3 = AuthoringService(repo3)
svc3.save_draft("topic", "T3", base_version=1, payload={"title": "New title"}, actor="Writer A")
result3 = svc3.save_topic("T3", title="New title", expected_version=1, actor="Writer A")
check("g3: Save produced exactly version 2 (one new version)", result3["version"] == 2, result3)
vcount3 = repo3.conn.execute("SELECT COUNT(*) c FROM topic_versions WHERE topic_id='T3'").fetchone()["c"]
check("g3: exactly 2 topic_versions rows exist after one Save (v1 original + v2 saved)", vcount3 == 2, vcount3)
check("g4: the draft is deleted after a successful Save", svc3.load_draft("topic", "T3") is None, "still present")

# ------------------------------------------------------------------
# Group 5 (AC#5) + Group 6 (AC#6): atomic optimistic concurrency.
# ------------------------------------------------------------------
repo5 = _new_repo("g05_concurrency")
repo5.create_topic("T5", "Original title", "Original body", status="Draft")
svc5 = AuthoringService(repo5)
ok_save = svc5.save_topic("T5", title="First writer wins", expected_version=1, actor="Writer A")
check("g5: first save with the correct expected_version succeeds", ok_save["version"] == 2, ok_save)

# Writer B's local draft still targets base_version=1 (they haven't seen
# Writer A's save yet) -- written AFTER Writer A's successful save so this
# test isolates exactly what AC#6 asks: does a FAILED save preserve a
# draft, not "does an unrelated successful save by someone else preserve
# a draft written before it" (a single-slot-per-object draft is correctly
# superseded by any successful save of that object -- see g4).
svc5.save_draft("topic", "T5", base_version=1, payload={"title": "Someone's edit"}, actor="Writer B")
row5_before = dict(repo5.conn.execute("SELECT * FROM topics WHERE topic_id='T5'").fetchone())
vcount5_before = repo5.conn.execute("SELECT COUNT(*) c FROM topic_versions WHERE topic_id='T5'").fetchone()["c"]


def _stale_save():
    svc5.save_topic("T5", title="Second writer (stale)", expected_version=1, actor="Writer B")


check("g5: a second save with the now-STALE expected_version=1 is rejected atomically",
      _raises(_stale_save, VersionConflictError), "did not raise VersionConflictError")

row5_after = dict(repo5.conn.execute("SELECT * FROM topics WHERE topic_id='T5'").fetchone())
vcount5_after = repo5.conn.execute("SELECT COUNT(*) c FROM topic_versions WHERE topic_id='T5'").fetchone()["c"]
check("g6: the conflicting save left the live topic row completely unaltered", row5_before == row5_after,
      (row5_before, row5_after))
check("g6: the conflicting save inserted NO new topic_versions row", vcount5_before == vcount5_after,
      (vcount5_before, vcount5_after))
draft5 = svc5.load_draft("topic", "T5")
check("g6: a FAILED save does not delete the draft (still recoverable)",
      draft5 is not None and draft5["payload"]["title"] == "Someone's edit", draft5)

# ------------------------------------------------------------------
# Group 7 (AC#7) + Group 8 (AC#8): edit of Approved/In Review -> Draft.
# Group 9 (AC#9): the OPEN review task riding the old version is invalidated.
# ------------------------------------------------------------------
repo7 = _new_repo("g07_status_invalidation")
repo7.create_topic("T7", "Title", "Body", status="Draft")
svc7 = AuthoringService(repo7)
rid7 = repo7.request_review("topic", "T7", requested_by="Writer A")
check("g8 setup: request_review put the topic In Review", repo7.conn.execute(
    "SELECT status FROM topics WHERE topic_id='T7'").fetchone()["status"] == "In Review", None)
r7_edit_in_review = svc7.save_topic("T7", body="Edited while In Review", expected_version=1, actor="Writer A")
check("g8: editing a topic that was In Review reverts it to Draft", r7_edit_in_review["status"] == "Draft", r7_edit_in_review)
task7 = repo7.conn.execute("SELECT status,decision FROM review_tasks WHERE review_id=?", (rid7,)).fetchone()
check("g9: the OPEN review task riding the edited version is CLOSED, not left dangling", task7["status"] == "CLOSED", dict(task7))
check("g9: the review task is closed with the INVALIDATED_BY_EDIT semantic", task7["decision"] == "INVALIDATED_BY_EDIT", dict(task7))

repo7.create_topic("T7B", "Title", "Body", status="Draft")
rid7b = repo7.request_review("topic", "T7B", requested_by="Writer A")
repo7.decide_review(rid7b, decision="APPROVE", actor="Reviewer X")
check("g7 setup: topic is now Approved", repo7.conn.execute(
    "SELECT status FROM topics WHERE topic_id='T7B'").fetchone()["status"] == "Approved", None)
r7b_edit_approved = svc7.save_topic("T7B", body="Edited after approval", expected_version=1, actor="Writer A")
check("g7: editing an Approved topic reverts it to Draft", r7b_edit_approved["status"] == "Draft", r7b_edit_approved)

# ------------------------------------------------------------------
# Group 10 (AC#10) + dedicated review-lifecycle bug regression test:
# request_review -> decide_review works from the real product path served
# by app.py (the 4A audit found decide_review was only ever wired in
# app_engineering.py, a module the shipped launcher never imports).
# ------------------------------------------------------------------
proj10 = TMP / "g10_project"
proj10.mkdir(exist_ok=True)
db10 = proj10 / "ManualSync_CCMS.sqlite"
repo10 = IndustrialRepository(db10)
repo10.create_topic("T10", "Title", "Body", status="Draft")
rid10 = repo10.request_review("topic", "T10", requested_by="Writer A")
repo10.close()

msg10, queue10_after = app.review_decide_ui(str(proj10), rid10, "APPROVE", "Reviewer X", "looks good")
check("g10 / review-bug regression: app.review_decide_ui() (the real product-served callback) "
      "successfully calls decide_review() and reports the new status",
      "Approved" in msg10, msg10)
check("g10 / review-bug regression: after deciding via the real UI path, the OPEN review queue no longer contains it",
      queue10_after.empty or rid10 not in queue10_after.get("review_id", []).tolist(), queue10_after)
repo10b = IndustrialRepository(db10)
check("g10: the topic's status is actually Approved in the CCMS after the UI-driven decision",
      repo10b.conn.execute("SELECT status FROM topics WHERE topic_id='T10'").fetchone()["status"] == "Approved", None)
repo10b.close()

# Reject path + actor-required guard, same real UI function.
proj10r = TMP / "g10_project_reject"
proj10r.mkdir(exist_ok=True)
db10r = proj10r / "ManualSync_CCMS.sqlite"
repo10r = IndustrialRepository(db10r)
repo10r.create_topic("T10R", "Title", "Body", status="Draft")
rid10r = repo10r.request_review("topic", "T10R", requested_by="Writer A")
repo10r.close()
msg10r, _ = app.review_decide_ui(str(proj10r), rid10r, "REJECT", "Reviewer X", "needs rework")
check("g10: REJECT via the real UI path reports the object back to Draft", "Draft" in msg10r, msg10r)


def _decide_without_actor():
    app.review_decide_ui(str(proj10r), rid10r, "APPROVE", "", "")


check("g10: review_decide_ui refuses (fail-closed) when no reviewer/actor is given",
      _raises(_decide_without_actor, Exception), "did not raise")

# ------------------------------------------------------------------
# Group 11 (AC#11) + review-lifecycle bug regression: bulk approval must
# not leave an OPEN review task orphaned -- this is the exact failure mode
# the 4A audit found (approve_source_ui's raw-SQL bulk UPDATE bypassed
# review_tasks entirely).
# ------------------------------------------------------------------
proj11 = TMP / "g11_project"
proj11.mkdir(exist_ok=True)
db11 = proj11 / "ManualSync_CCMS.sqlite"
repo11 = IndustrialRepository(db11)
repo11.create_topic("T11", "Title", "Body", status="Draft")
repo11.add_block("T11-BLK-1", "T11", "paragraph", position=1, text="Some grounded text.", status="Draft")
repo11.create_publication("PUB-MAIN", "Synthetic Manual", ["T11"])
rid11 = repo11.request_review("block", "T11-BLK-1", requested_by="ManualSync AI", comment="Grounded proposal")
open_before = repo11.review_queue(status="OPEN")
check("g11 setup: one OPEN review task exists before bulk approval", len(open_before) == 1, open_before)
repo11.close()

msg11 = app.approve_source_ui(str(proj11), "PUB-MAIN", "Human reviewer", True)
check("g11: approve_source_ui (the real bulk-approval UI path) succeeds", "approvata" in msg11, msg11)
check("g11 / review-bug regression: bulk approval message reports the reconciled review task",
      "riconciliat" in msg11, msg11)

repo11b = IndustrialRepository(db11)
open_after = repo11b.review_queue(status="OPEN")
check("g11 / review-bug regression: NO orphaned OPEN review task remains after bulk approval "
      "(this is the exact bug the 4A audit found)", len(open_after) == 0, open_after)
closed_task = repo11b.conn.execute("SELECT status,decision FROM review_tasks WHERE review_id=?", (rid11,)).fetchone()
check("g11: the review task was actually closed (not deleted) with a traceable decision",
      closed_task["status"] == "CLOSED" and closed_task["decision"] == "APPROVED_VIA_BULK", dict(closed_task))
check("g11: bulk approval still marks content Approved", repo11b.conn.execute(
    "SELECT status FROM topics WHERE topic_id='T11'").fetchone()["status"] == "Approved", None)
repo11b.close()

# ------------------------------------------------------------------
# Group 12 (AC#12) + Group 13 (AC#13) + Group 14 (AC#14): reorder blocks.
# ------------------------------------------------------------------
repo12 = _new_repo("g12_reorder_blocks")
bids12 = _seed_topic_with_blocks(repo12, topic_id="T12", n_blocks=3)
svc12 = AuthoringService(repo12)
new_order = list(reversed(bids12))
result12 = svc12.reorder_blocks("T12", new_order, expected_block_ids=bids12, actor="Writer A")
check("g12: a valid reorder is accepted and returned", result12 == new_order, result12)
positions = {r["block_id"]: r["position"] for r in repo12.conn.execute(
    "SELECT block_id,position FROM content_blocks WHERE topic_id='T12'")}
check("g12: the new order is actually persisted as sequential positions",
      [k for k, v in sorted(positions.items(), key=lambda kv: kv[1])] == new_order, positions)

check("g13: a reorder with a duplicated block id is rejected",
      _raises(lambda: svc12.reorder_blocks("T12", [new_order[0], new_order[0], new_order[2]],
                                           expected_block_ids=new_order, actor="Writer A"), ValueError), None)
check("g13: a reorder missing one active block is rejected",
      _raises(lambda: svc12.reorder_blocks("T12", new_order[:2], expected_block_ids=new_order, actor="Writer A"), ValueError), None)
repo12.create_topic("T12-OTHER", "Other", "Body", status="Draft")
repo12.add_block("T12-OTHER-BLK", "T12-OTHER", "paragraph", position=1, text="Foreign block.", status="Draft")
check("g13: a reorder that includes a block belonging to ANOTHER topic is rejected",
      _raises(lambda: svc12.reorder_blocks("T12", new_order + ["T12-OTHER-BLK"],
                                           expected_block_ids=new_order, actor="Writer A"), ValueError), None)

# g14: stale expected_block_ids (structure changed since the caller last read it)
svc12.reorder_blocks("T12", bids12, expected_block_ids=new_order, actor="Writer A")  # restore original order


def _stale_reorder():
    svc12.reorder_blocks("T12", new_order, expected_block_ids=new_order, actor="Writer A")


check("g14: a reorder whose expected_block_ids no longer matches the live structure is rejected atomically",
      _raises(_stale_reorder, VersionConflictError), "did not raise VersionConflictError")
positions_after_conflict = {r["block_id"]: r["position"] for r in repo12.conn.execute(
    "SELECT block_id,position FROM content_blocks WHERE topic_id='T12'")}
check("g14: the rejected stale reorder left positions unchanged",
      positions_after_conflict == {r["block_id"]: r["position"] for r in repo12.conn.execute(
          "SELECT block_id,position FROM content_blocks WHERE topic_id='T12'")}, None)

# ------------------------------------------------------------------
# Group 15 (AC#15): reorder of publication topic order is audited.
# ------------------------------------------------------------------
repo15 = _new_repo("g15_reorder_topics")
repo15.create_topic("T15A", "A", "Body", status="Draft")
repo15.create_topic("T15B", "B", "Body", status="Draft")
repo15.create_topic("T15C", "C", "Body", status="Draft")
repo15.create_publication("PUB-15", "Manual", ["T15A", "T15B", "T15C"])
svc15 = AuthoringService(repo15)
before15 = ["T15A", "T15B", "T15C"]
after15 = ["T15C", "T15A", "T15B"]
svc15.reorder_publication_topics("PUB-15", after15, expected_topic_ids=before15, actor="Writer A")
persisted15 = [r["topic_id"] for r in repo15.conn.execute(
    "SELECT topic_id FROM publication_topics WHERE publication_id='PUB-15' ORDER BY position")]
check("g15: publication topic reorder persists atomically", persisted15 == after15, persisted15)
audit15 = repo15.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='publication' AND action='REORDER_TOPICS' AND object_id='PUB-15'"
).fetchall()
check("g15: the topic reorder is audited (before/after)", len(audit15) == 1, audit15)
check("g15: the audit detail records the correct before/after order",
      '"T15C"' in audit15[0]["detail_json"] and '"T15A"' in audit15[0]["detail_json"], audit15[0]["detail_json"])

# ------------------------------------------------------------------
# Group 16 (AC#16) + Group 17 (AC#17): archive/restore block excludes/
# reinserts from assembly.
# ------------------------------------------------------------------
repo16 = _new_repo("g16_archive_block")
bids16 = _seed_topic_with_blocks(repo16, topic_id="T16", n_blocks=2)
_seed_publication(repo16, publication_id="PUB-16", topic_ids=["T16"])
svc16 = AuthoringService(repo16)
frame_before, _ = repo16.assemble_structured_publication("PUB-16")
check("g16 setup: both blocks appear in assembly before archiving", len(frame_before) == 2, len(frame_before))
svc16.archive_block(bids16[0], actor="Writer A")
frame_after_archive, _ = repo16.assemble_structured_publication("PUB-16")
check("g16: an archived block is excluded from publication assembly",
      len(frame_after_archive) == 1 and bids16[0] not in frame_after_archive["block_id"].tolist(), frame_after_archive["block_id"].tolist())
svc16.restore_block(bids16[0], actor="Writer A")
frame_after_restore, _ = repo16.assemble_structured_publication("PUB-16")
check("g17: restoring the block reinserts it into publication assembly",
      len(frame_after_restore) == 2 and bids16[0] in frame_after_restore["block_id"].tolist(), frame_after_restore["block_id"].tolist())

check("archive: attempting to save an archived block is refused (ArchivedObjectError), not silently allowed",
      _raises(lambda: (svc16.archive_block(bids16[1], actor="Writer A"),
                       svc16.save_block(bids16[1], text="edit while archived", expected_version=1, actor="Writer A"))[1],
              ArchivedObjectError), None)

# ------------------------------------------------------------------
# Group 18 (AC#18): archive topic excludes it from publication assembly.
# ------------------------------------------------------------------
repo18 = _new_repo("g18_archive_topic")
_seed_topic_with_blocks(repo18, topic_id="T18A", n_blocks=1)
repo18.create_topic("T18B", "Second topic", "Body", status="Draft")
repo18.add_block("T18B-BLK-1", "T18B", "paragraph", position=1, text="Second topic content.", status="Draft")
_seed_publication(repo18, publication_id="PUB-18", topic_ids=["T18A", "T18B"])
svc18 = AuthoringService(repo18)
frame18_before, _ = repo18.assemble_structured_publication("PUB-18")
check("g18 setup: both topics contribute blocks before archiving", set(frame18_before["topic_id"]) == {"T18A", "T18B"}, None)
svc18.archive_topic("T18A", actor="Writer A")
frame18_after, _ = repo18.assemble_structured_publication("PUB-18")
check("g18: an archived topic is excluded entirely from publication assembly",
      set(frame18_after["topic_id"]) == {"T18B"}, set(frame18_after["topic_id"]))

# ------------------------------------------------------------------
# Group 19 (AC#19): revert creates a NEW version, never alters history.
# ------------------------------------------------------------------
repo19 = _new_repo("g19_revert")
repo19.create_topic("T19", "V1 title", "V1 body", status="Draft")
svc19 = AuthoringService(repo19)
svc19.save_topic("T19", title="V2 title", body="V2 body", expected_version=1, actor="Writer A")
hist_v1_before = dict(repo19.conn.execute("SELECT * FROM topic_versions WHERE topic_id='T19' AND version=1").fetchone())
hist_v2_before = dict(repo19.conn.execute("SELECT * FROM topic_versions WHERE topic_id='T19' AND version=2").fetchone())
revert_result = svc19.revert_topic_to_version("T19", 1, actor="Writer A")
check("g19: revert produces a NEW version (3), not a rewrite of version 1", revert_result["new_version"] == 3, revert_result)
live19 = dict(repo19.conn.execute("SELECT * FROM topics WHERE topic_id='T19'").fetchone())
check("g19: the live topic now carries version 1's CONTENT under the new version number",
      live19["title"] == "V1 title" and live19["version"] == 3, live19)
hist_v1_after = dict(repo19.conn.execute("SELECT * FROM topic_versions WHERE topic_id='T19' AND version=1").fetchone())
hist_v2_after = dict(repo19.conn.execute("SELECT * FROM topic_versions WHERE topic_id='T19' AND version=2").fetchone())
check("g19: the historical version-1 row is byte-identical (never rewritten)", hist_v1_before == hist_v1_after, None)
check("g19: the historical version-2 row is byte-identical (never rewritten)", hist_v2_before == hist_v2_after, None)

# ------------------------------------------------------------------
# Group 20 (AC#20): provenance/source_refs are not zeroed on an unrelated edit.
# ------------------------------------------------------------------
repo20 = _new_repo("g20_provenance")
repo20.create_topic("T20", "Title", "Body", status="Draft", source_refs=["legacy_import.pdf#page=7"])
svc20 = AuthoringService(repo20)
svc20.save_topic("T20", title="New title only", expected_version=1, actor="Writer A")
row20 = repo20.conn.execute("SELECT source_refs_json FROM topics WHERE topic_id='T20'").fetchone()
check("g20: source_refs are preserved when a save doesn't touch them explicitly",
      "legacy_import.pdf#page=7" in row20["source_refs_json"], row20["source_refs_json"])

# ------------------------------------------------------------------
# Group 21 (AC#21): every authoring operation is actor-audited.
# ------------------------------------------------------------------
repo21 = _new_repo("g21_audit")
repo21.create_topic("T21", "Title", "Body", status="Draft")
repo21.add_block("T21-BLK-1", "T21", "paragraph", position=1, text="Block.", status="Draft")
repo21.create_publication("PUB-21", "Manual", ["T21"])
svc21 = AuthoringService(repo21)
svc21.save_topic("T21", title="Edited", expected_version=1, actor="Actor-Save-Topic")
svc21.save_block("T21-BLK-1", text="Edited block", expected_version=1, actor="Actor-Save-Block")
svc21.archive_topic("T21", actor="Actor-Archive")
svc21.restore_topic("T21", actor="Actor-Restore")
svc21.reorder_blocks("T21", ["T21-BLK-1"], expected_block_ids=["T21-BLK-1"], actor="Actor-Reorder-Blocks")
svc21.reorder_publication_topics("PUB-21", ["T21"], expected_topic_ids=["T21"], actor="Actor-Reorder-Topics")
svc21.revert_topic_to_version("T21", 1, actor="Actor-Revert")
for action, actor in [
    ("AUTHORING_SAVE", "Actor-Save-Topic"), ("AUTHORING_SAVE", "Actor-Save-Block"),
    ("ARCHIVE", "Actor-Archive"), ("RESTORE", "Actor-Restore"),
    ("REORDER_BLOCKS", "Actor-Reorder-Blocks"), ("REORDER_TOPICS", "Actor-Reorder-Topics"),
    ("REVERT_TO_VERSION", "Actor-Revert"),
]:
    hit = repo21.conn.execute("SELECT 1 FROM audit_events WHERE action=? AND actor=?", (action, actor)).fetchone()
    check(f"g21: {action} by {actor} is audited", hit is not None, (action, actor))


def _save_without_actor():
    svc21.save_topic("T21", title="No actor", expected_version=3, actor="")


check("g21: save_topic refuses (fail-closed) without an actor", _raises(_save_without_actor, ValueError), None)

# ==================================================================
# 4B.1 FINAL INTEGRITY GATE -- additional groups 23-28
# ==================================================================

# ------------------------------------------------------------------
# Group 23 (Integrity Gate #1a): archive -> reorder -> restore BLOCK
# determinism. Archived B must not collide with the renumbered active
# range when restored.
# ------------------------------------------------------------------
repo23 = _new_repo("g23_archive_reorder_restore_block")
bids23 = _seed_topic_with_blocks(repo23, topic_id="T23", n_blocks=3)  # A,B,C at positions 1,2,3
A23, B23, C23 = bids23
svc23 = AuthoringService(repo23)
svc23.archive_block(B23, actor="Writer A")
active_before_reorder = [r["block_id"] for r in repo23.conn.execute(
    "SELECT block_id FROM content_blocks WHERE topic_id='T23' AND archived_at IS NULL ORDER BY position,block_id")]
check("g23 setup: archived block is excluded from the active set used for reorder", B23 not in active_before_reorder, active_before_reorder)
svc23.reorder_blocks("T23", [C23, A23], expected_block_ids=active_before_reorder, actor="Writer A")
restore_result23 = svc23.restore_block(B23, actor="Writer A")
positions23 = {r["block_id"]: r["position"] for r in repo23.conn.execute(
    "SELECT block_id,position FROM content_blocks WHERE topic_id='T23' AND archived_at IS NULL")}
check("g23: no duplicate positions among active blocks after archive->reorder->restore",
      len(set(positions23.values())) == len(positions23), positions23)
check("g23: restored block B is appended strictly after the reordered active blocks (deterministic 'append at end')",
      positions23[B23] > positions23[C23] and positions23[B23] > positions23[A23], positions23)
final_order23 = [bid for bid, _ in sorted(positions23.items(), key=lambda kv: kv[1])]
check("g23: final active order is exactly [C, A, B] as designed", final_order23 == [C23, A23, B23], final_order23)
repo23.close()
repo23b = _new_repo("g23_archive_reorder_restore_block")  # reopen: same file, fresh connection
positions23b = {r["block_id"]: r["position"] for r in repo23b.conn.execute(
    "SELECT block_id,position FROM content_blocks WHERE topic_id='T23' AND archived_at IS NULL")}
check("g23: the order is stable after a brand new instance/process reopens the same project",
      positions23b == positions23, (positions23, positions23b))
repo23b.close()

# ------------------------------------------------------------------
# Group 24 (Integrity Gate #1b): archive -> reorder -> restore TOPIC
# (publication order) determinism, same guarantees as blocks.
# ------------------------------------------------------------------
repo24 = _new_repo("g24_archive_reorder_restore_topic")
for tid in ("T24X", "T24Y", "T24Z"):
    repo24.create_topic(tid, tid, "Body", status="Draft")
repo24.create_publication("PUB-24", "Manual", ["T24X", "T24Y", "T24Z"])  # positions 1,2,3
svc24 = AuthoringService(repo24)
svc24.archive_topic("T24Y", actor="Writer A")


def _reorder_including_archived():
    svc24.reorder_publication_topics("PUB-24", ["T24Z", "T24X", "T24Y"], expected_topic_ids=["T24X", "T24Z"], actor="Writer A")


check("g24: reorder_publication_topics rejects an order that still includes the archived topic",
      _raises(_reorder_including_archived, ValueError), None)
active_before24 = [r["topic_id"] for r in repo24.conn.execute(
    """SELECT pt.topic_id FROM publication_topics pt JOIN topics t ON t.topic_id=pt.topic_id
       WHERE pt.publication_id='PUB-24' AND t.archived_at IS NULL ORDER BY pt.position""")]
svc24.reorder_publication_topics("PUB-24", ["T24Z", "T24X"], expected_topic_ids=active_before24, actor="Writer A")
svc24.restore_topic("T24Y", actor="Writer A")
positions24 = {r["topic_id"]: r["position"] for r in repo24.conn.execute(
    "SELECT topic_id,position FROM publication_topics WHERE publication_id='PUB-24'")}
check("g24: no duplicate positions among publication topics after archive->reorder->restore",
      len(set(positions24.values())) == len(positions24), positions24)
final_order24 = [tid for tid, _ in sorted(positions24.items(), key=lambda kv: kv[1])]
check("g24: final publication topic order is exactly [Z, X, Y] as designed", final_order24 == ["T24Z", "T24X", "T24Y"], final_order24)
repo24.close()
repo24b = _new_repo("g24_archive_reorder_restore_topic")
positions24b = {r["topic_id"]: r["position"] for r in repo24b.conn.execute(
    "SELECT topic_id,position FROM publication_topics WHERE publication_id='PUB-24'")}
check("g24: the publication order is stable after a brand new instance/process reopens the same project",
      positions24b == positions24, (positions24, positions24b))
repo24b.close()

# ------------------------------------------------------------------
# Group 25 (Integrity Gate #2): archived content is excluded from
# readiness too, not only assembly -- a lingering OPEN review task on an
# archived object must not block a release, must not be silently
# destroyed (it reappears on restore), and archived unapproved content
# must not count as an "unapproved block" blocker either.
# ------------------------------------------------------------------
proj25 = create_project(TMP, project_name="g25_readiness_archive", source_lang="en-US")
repo25 = IndustrialRepository(proj25 / "ManualSync_CCMS.sqlite")
repo25.create_topic("T25A", "Approved topic", "Body", status="Draft")
repo25.add_block("T25A-BLK", "T25A", "paragraph", position=1, text="Approved content.", status="Draft")
repo25.create_topic("T25B", "Unapproved topic", "Body", status="Draft")
repo25.add_block("T25B-BLK", "T25B", "paragraph", position=1, text="Unapproved content.", status="Draft")
repo25.create_publication("PUB-25", "Manual", ["T25A", "T25B"])
svc25 = AuthoringService(repo25)
svc25.bulk_approve_publication("PUB-25", actor="Human reviewer")  # approves BOTH topics/blocks
# T25B is deliberately reopened for review AFTER the bulk approval, so it
# carries both an OPEN review task AND (once reverted by the edit-
# invalidation rule) a non-Approved status -- a realistic "still being
# worked on" object that a release must not silently ignore while active.
rid25 = repo25.request_review("block", "T25B-BLK", requested_by="Writer A")
repo25.close()

repo25b = IndustrialRepository(proj25 / "ManualSync_CCMS.sqlite")
check("g25 setup: T25B-BLK has an OPEN review task before archiving",
      repo25b.conn.execute("SELECT status FROM review_tasks WHERE review_id=?", (rid25,)).fetchone()["status"] == "OPEN", None)
repo25b.close()

r25_before = assess_release_readiness(proj25, "PUB-25", target_langs=[])
source_row25_before = r25_before["matrix"][r25_before["matrix"]["role"] == "SOURCE"].iloc[0]
check("g25: BEFORE archiving, the active unapproved/in-review topic blocks readiness",
      r25_before["release_ready"] is False and int(source_row25_before["open_ccms_reviews"]) > 0, dict(source_row25_before))

repo25c = IndustrialRepository(proj25 / "ManualSync_CCMS.sqlite")
svc25c = AuthoringService(repo25c)
svc25c.archive_topic("T25B", actor="Writer A")
repo25c.close()

r25_archived = assess_release_readiness(proj25, "PUB-25", target_langs=[])
source_row25_archived = r25_archived["matrix"][r25_archived["matrix"]["role"] == "SOURCE"].iloc[0]
check("g25 / Integrity Gate #2: AFTER archiving, the OPEN review no longer blocks readiness "
      "(archived content is excluded from the same assembled frame readiness reads)",
      int(source_row25_archived["open_ccms_reviews"]) == 0, dict(source_row25_archived))
check("g25 / Integrity Gate #2: AFTER archiving, the publication is release_ready",
      r25_archived["release_ready"] is True, r25_archived["summary"])

repo25d = IndustrialRepository(proj25 / "ManualSync_CCMS.sqlite")
check("g25: the review task itself is untouched (still OPEN, never force-closed) while archived -- "
      "readiness ignores it because the object is excluded, it is not silently resolved",
      repo25d.conn.execute("SELECT status FROM review_tasks WHERE review_id=?", (rid25,)).fetchone()["status"] == "OPEN", None)
svc25d = AuthoringService(repo25d)
svc25d.restore_topic("T25B", actor="Writer A")
repo25d.close()

r25_restored = assess_release_readiness(proj25, "PUB-25", target_langs=[])
source_row25_restored = r25_restored["matrix"][r25_restored["matrix"]["role"] == "SOURCE"].iloc[0]
check("g25: AFTER restoring, the SAME still-OPEN review task blocks readiness again -- "
      "nothing was silently lost while the topic was archived",
      r25_restored["release_ready"] is False and int(source_row25_restored["open_ccms_reviews"]) > 0, dict(source_row25_restored))

# ------------------------------------------------------------------
# Group 26 (Integrity Gate #3): content-version vs workflow-status
# semantics, made explicit and tested. request_review/decide_review must
# NEVER create a new content version; *_versions.status is frozen at
# save-time and must never be read as authoritative workflow history --
# only the live `status` column + audit_events/review_tasks are.
# ------------------------------------------------------------------
repo26 = _new_repo("g26_version_vs_status")
repo26.create_topic("T26", "Title", "Body", status="Draft")  # v1, status Draft
rid26 = repo26.request_review("topic", "T26", requested_by="Writer A")
repo26.decide_review(rid26, decision="APPROVE", actor="Reviewer X", comment="looks good")
live26 = repo26.conn.execute("SELECT version,status FROM topics WHERE topic_id='T26'").fetchone()
check("g26: request_review + decide_review NEVER bump the content version", int(live26["version"]) == 1, dict(live26))
check("g26: the live status IS updated to Approved (authoritative, current workflow state)",
      live26["status"] == "Approved", dict(live26))
vcount26 = repo26.conn.execute("SELECT COUNT(*) c FROM topic_versions WHERE topic_id='T26'").fetchone()["c"]
check("g26: exactly ONE topic_versions row exists -- review/approval created no version noise", vcount26 == 1, vcount26)
frozen_status26 = repo26.conn.execute("SELECT status FROM topic_versions WHERE topic_id='T26' AND version=1").fetchone()["status"]
check("g26 / Integrity Gate #3: topic_versions.status for v1 is frozen at 'Draft' (the status AT SAVE TIME) -- "
      "it does NOT reflect the later Approved decision, proving *_versions.status must never be read as "
      "authoritative workflow/review history", frozen_status26 == "Draft", frozen_status26)
task26 = repo26.conn.execute("SELECT status,decision,comment FROM review_tasks WHERE review_id=?", (rid26,)).fetchone()
check("g26: the actual decision is correctly reconstructable from review_tasks (CLOSED/APPROVE/comment)",
      task26["status"] == "CLOSED" and task26["decision"] == "APPROVE" and task26["comment"] == "looks good", dict(task26))
audit26 = repo26.conn.execute("SELECT actor FROM audit_events WHERE object_type='topic' AND object_id='T26' AND action='REVIEW_APPROVE'").fetchone()
check("g26: the actor of the decision is correctly reconstructable from audit_events",
      audit26["actor"] == "Reviewer X", dict(audit26) if audit26 else None)

# ------------------------------------------------------------------
# Group 27 (Integrity Gate #4): bulk_approve_publication reconciliation --
# OPEN task on a topic, OPEN task on a block, multiple OPEN tasks for
# different objects, and a pre-existing CLOSED historical task that must
# be left completely untouched.
# ------------------------------------------------------------------
repo27 = _new_repo("g27_bulk_reconciliation")
repo27.create_topic("T27", "Title", "Body", status="Draft")
repo27.add_block("T27-BLK1", "T27", "paragraph", position=1, text="Block 1.", status="Draft")
repo27.add_block("T27-BLK2", "T27", "paragraph", position=2, text="Block 2.", status="Draft")
repo27.create_publication("PUB-27", "Manual", ["T27"])
rid27_topic = repo27.request_review("topic", "T27", requested_by="Writer A")  # OPEN on a TOPIC
rid27_blk1 = repo27.request_review("block", "T27-BLK1", requested_by="Writer A")  # OPEN on a BLOCK (different object)
rid27_blk2_old = repo27.request_review("block", "T27-BLK2", requested_by="Writer A")
repo27.decide_review(rid27_blk2_old, decision="REJECT", actor="Reviewer X", comment="historical rejection")  # pre-existing CLOSED
closed27_before = dict(repo27.conn.execute("SELECT * FROM review_tasks WHERE review_id=?", (rid27_blk2_old,)).fetchone())

svc27 = AuthoringService(repo27)
result27 = svc27.bulk_approve_publication("PUB-27", actor="Human reviewer")
check("g27: bulk approval reports exactly 2 review tasks reconciled (topic + block1; NOT the historical closed one)",
      result27["review_tasks_closed"] == 2, result27)
open27_after = repo27.review_queue(status="OPEN")
check("g27 / Integrity Gate #4: no OPEN task remains for any approved object after bulk approval", len(open27_after) == 0, open27_after)

task27_topic = repo27.conn.execute("SELECT status,decision FROM review_tasks WHERE review_id=?", (rid27_topic,)).fetchone()
task27_blk1 = repo27.conn.execute("SELECT status,decision FROM review_tasks WHERE review_id=?", (rid27_blk1,)).fetchone()
check("g27: the topic's OPEN task was closed with an explicit APPROVED_VIA_BULK decision",
      task27_topic["status"] == "CLOSED" and task27_topic["decision"] == "APPROVED_VIA_BULK", dict(task27_topic))
check("g27: the block's OPEN task was closed with an explicit APPROVED_VIA_BULK decision",
      task27_blk1["status"] == "CLOSED" and task27_blk1["decision"] == "APPROVED_VIA_BULK", dict(task27_blk1))
check("g27: actor/audit exist for each task closed via bulk approval", repo27.conn.execute(
    "SELECT COUNT(*) c FROM audit_events WHERE action='REVIEW_APPROVED_VIA_BULK' AND actor='Human reviewer'"
).fetchone()["c"] == 2, None)

closed27_after = dict(repo27.conn.execute("SELECT * FROM review_tasks WHERE review_id=?", (rid27_blk2_old,)).fetchone())
check("g27 / Integrity Gate #4: the PRE-EXISTING historical CLOSED task is byte-identical after bulk approval "
      "(never re-touched, semantica storica preservata)", closed27_before == closed27_after, (closed27_before, closed27_after))

# ------------------------------------------------------------------
# Group 28 (Integrity Gate #6): draft single-slot behavior under two
# sessions is fail-closed and explicit.
# ------------------------------------------------------------------
repo28 = _new_repo("g28_draft_single_slot")
repo28.create_topic("T28", "Title", "Body", status="Draft")
svc28 = AuthoringService(repo28)
svc28.save_draft("topic", "T28", base_version=1, payload={"title": "A's edit"}, actor="Writer A", session_id="sess-A")
svc28.save_draft("topic", "T28", base_version=1, payload={"title": "B's edit"}, actor="Writer B", session_id="sess-B")
current_draft28 = svc28.load_draft("topic", "T28")
check("g28: the most recently written draft is the one recoverable (B's, overwriting A's)",
      current_draft28["session_id"] == "sess-B" and current_draft28["payload"]["title"] == "B's edit", current_draft28)

# Session A successfully saves (content hadn't actually changed yet, so
# expected_version=1 is still correct) -- this clears the shared draft
# slot BY DESIGN: the content has now moved to v2, so ANY draft still
# targeting v1 (whether it started as A's or, as here, was since
# overwritten by B's) is stale relative to the live object and must not
# be silently offered back as "recoverable" over newer saved content.
save28 = svc28.save_topic("T28", title="A's actual save", expected_version=1, actor="Writer A")
check("g28: a SUCCESSFUL save clears the shared draft slot (by design -- see docstring)",
      save28["version"] == 2 and svc28.load_draft("topic", "T28") is None, save28)

# Now the concurrency-protection scenario itself: A saves first (live
# moves to v2), THEN B (unaware, still thinks v1) writes/refreshes their
# OWN recovery draft, THEN B attempts a stale Save.
repo28b = _new_repo("g28b_stale_save_preserves_other_session_draft")
repo28b.create_topic("T28B", "Title", "Body", status="Draft")
svc28b = AuthoringService(repo28b)
svc28b.save_topic("T28B", title="A wins", expected_version=1, actor="Writer A")  # -> v2
svc28b.save_draft("topic", "T28B", base_version=1, payload={"title": "B recovery (stale base)"}, actor="Writer B", session_id="sess-B")


def _stale_save_28b():
    svc28b.save_topic("T28B", title="B's stale attempt", expected_version=1, actor="Writer B")


check("g28 / Integrity Gate #6: B's stale Save (expected_version=1, live is now 2) is rejected",
      _raises(_stale_save_28b, VersionConflictError), None)
draft28b_after = svc28b.load_draft("topic", "T28B")
check("g28 / Integrity Gate #6: B's stale/failed Save did NOT delete B's own (or anyone else's) recovery draft",
      draft28b_after is not None and draft28b_after["payload"]["title"] == "B recovery (stale base)", draft28b_after)
check("g28: the surviving draft's session_id/actor correctly identify who it came from",
      draft28b_after["session_id"] == "sess-B" and draft28b_after["actor"] == "Writer B", draft28b_after)
live28b = repo28b.conn.execute("SELECT title,version FROM topics WHERE topic_id='T28B'").fetchone()
check("g28 / Integrity Gate #6: the CAS on content protected the live object -- "
      "B's stale attempt never touched A's successfully saved content",
      live28b["title"] == "A wins" and int(live28b["version"]) == 2, dict(live28b))


print(json.dumps({"pass": sum(r["status"] == "PASS" for r in results), "tests": len(results)}))
if any(r["status"] != "PASS" for r in results):
    print(json.dumps(results, indent=2))
    sys.exit(1)
