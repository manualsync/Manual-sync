"""Milestone 4B.2 regression suite: Professional Topic & Block Editor.

Tests authoring_view.py (the pure-Python view-model) directly, and the real
app.py `studio_*_ui` callbacks for everything that needs the full editor
flow (create without an ID, save/draft/conflict, review, revert) -- calling
the actual functions the product wires to Gradio, not a mock, matching the
same discipline established for the 4B.1 suite.

Covers all 27 acceptance items from the 4B.2 report, in order.

Backend/integration only -- no browser (see the separate browser-acceptance
cycles run for this milestone). Fixtures are entirely synthetic.
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
from authoring_service import AuthoringService, VersionConflictError, ArchivedObjectError, EDITABLE_TEXT_BLOCK_TYPES
import authoring_view as av
from project_lifecycle import create_project
import app
import i18n

TMP = Path(tempfile.mkdtemp(prefix="manualsync_authoring_editor_4b2_"))
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
    p = create_project(TMP, project_name=name, source_lang="en-US")
    repo = IndustrialRepository(p / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("SEED-TOPIC", "Seed Topic", "legacy body", status="Draft")
        repo.create_publication("PUB-MAIN", "Synthetic Manual", ["SEED-TOPIC"])
        repo.add_block("SEED-BLK", "SEED-TOPIC", "paragraph", position=1, text="Seed block.", status="Draft")
    finally:
        repo.close()
    return p


# ------------------------------------------------------------------
# 4C compatibility adapters: studio_save_ui/studio_save_draft_ui/
# studio_restore_draft_ui/studio_discard_draft_ui/studio_revert_ui all
# gained new REQUIRED (block_type, object_ref_id, ...table/figure field...)
# parameters when 4C added dedicated table/figure editors alongside the
# existing topic/text-block path. Every call below in THIS 4B.2 suite is
# topic/text-block only, so block_type/object_ref_id are always None here
# (exactly what the real UI leaves them at when a topic or a plain text
# block is open) and the table/figure field values are never touched by
# the code path being exercised. These thin wrappers keep every assertion
# body below byte-for-byte the same as before 4C -- only the call shape
# was adapted, proving the topic/text-block BEHAVIOR itself is unchanged.
# ------------------------------------------------------------------

def _save_ui(project_dir, pub, kind, object_id, expected_version, title_val, text_val, actor, show_archived):
    return app.studio_save_ui(project_dir, pub, kind, object_id, None, None, expected_version,
                              title_val, text_val, "", "", None, "", "", actor, show_archived)


def _save_draft_ui(project_dir, kind, object_id, expected_version, title_val, text_val, actor):
    return app.studio_save_draft_ui(project_dir, kind, object_id, None, None, expected_version,
                                    title_val, text_val, "", "", None, "", "", actor)


def _restore_draft_ui(project_dir, kind, object_id):
    return app.studio_restore_draft_ui(project_dir, kind, object_id, None, None)


def _discard_draft_ui(project_dir, pub, kind, object_id, show_archived):
    return app.studio_discard_draft_ui(project_dir, pub, kind, object_id, None, None, show_archived)


def _revert_ui(project_dir, pub, kind, object_id, version, show_archived, actor):
    return app.studio_revert_ui(project_dir, pub, kind, object_id, None, None, version, show_archived, actor)


# ------------------------------------------------------------------
# Group 1 (AC#1): outline publication ordinata.
# ------------------------------------------------------------------
p1 = _new_project("g01_outline_order")
repo1 = IndustrialRepository(p1 / "ManualSync_CCMS.sqlite")
repo1.create_topic("T1B", "Second Topic", "", status="Draft")
repo1.create_publication("PUB-1", "Manual 1", ["SEED-TOPIC", "T1B"])
repo1.add_block("T1B-BLK1", "T1B", "paragraph", position=1, text="B1", status="Draft")
repo1.add_block("T1B-BLK2", "T1B", "note", position=2, text="B2", status="Draft")
rows1 = av.outline_rows(repo1, "PUB-1")
check("g1: outline lists topics in publication order", [r["id"] for r in rows1 if r["kind"] == "topic"] == ["SEED-TOPIC", "T1B"], rows1)
check("g1: outline lists each topic's blocks in position order right after it",
      [r["id"] for r in rows1 if r["kind"] == "block" and r["topic_id"] == "T1B"] == ["T1B-BLK1", "T1B-BLK2"], rows1)
table1 = av.outline_table(rows1)
check("g1: outline_table produces one row per topic+block, friendly columns", len(table1) == len(rows1) and "Stato" in table1[0], table1[:1])
repo1.close()

# ------------------------------------------------------------------
# Group 2 (AC#2): create topic without asking for a technical ID.
# ------------------------------------------------------------------
p2 = _new_project("g02_create_topic_no_id")
repo2, svc2 = av.open_service(p2)
result2 = svc2.create_topic_in_publication("PUB-MAIN", "My New Topic", actor="Writer A")
check("g2: create_topic_in_publication generates the topic_id -- caller never supplies one", result2["topic_id"].startswith("TOPIC-"), result2)
check("g2: the new topic is placed in the publication (end, no after_topic_id given)", result2["position"] == 2, result2)
repo2.close()
# Same via the real UI callback: only a title is passed in, never an ID.
bundle2 = app.studio_new_topic_ui(str(p2), "PUB-MAIN", "Via UI Title", None, None, False, "Writer A")
check("g2: studio_new_topic_ui (real UI callback) creates a topic from a title alone and opens it",
      bundle2[app.ST_IDX_KIND] == "topic" and bundle2[app.ST_IDX_ID] and bundle2[app.ST_IDX_ID].startswith("TOPIC-"), bundle2[app.ST_IDX_SAVE_MSG])

# ------------------------------------------------------------------
# Group 3 (AC#3): rename topic.
# ------------------------------------------------------------------
p3 = _new_project("g03_rename_topic")
bundle3 = _save_ui(str(p3), "PUB-MAIN", "topic", "SEED-TOPIC", 1, "Renamed Title", "", "Writer A", False)
check("g3: rename via studio_save_ui succeeds", "✅" in bundle3[app.ST_IDX_SAVE_MSG], bundle3[app.ST_IDX_SAVE_MSG])
repo3, _ = av.open_service(p3)
check("g3: the live topic title is actually renamed", repo3.conn.execute(
    "SELECT title FROM topics WHERE topic_id='SEED-TOPIC'").fetchone()["title"] == "Renamed Title", None)
repo3.close()

# ------------------------------------------------------------------
# Group 4 (AC#4) + Group 5 (AC#5): reorder topic up/down, archive/restore.
# ------------------------------------------------------------------
p4 = _new_project("g04_reorder_archive_restore_topic")
repo4 = IndustrialRepository(p4 / "ManualSync_CCMS.sqlite")
repo4.create_topic("T4B", "Topic B", "", status="Draft")
repo4.create_publication("PUB-4", "Manual 4", ["SEED-TOPIC", "T4B"])
repo4.close()
bundle4 = app.studio_move_ui(str(p4), "PUB-4", "topic", "T4B", "up", False, "Writer A")
repo4b = IndustrialRepository(p4 / "ManualSync_CCMS.sqlite")
order4 = [r["topic_id"] for r in repo4b.conn.execute("SELECT topic_id FROM publication_topics WHERE publication_id='PUB-4' ORDER BY position")]
check("g4: move up via the real UI callback reorders the publication", order4 == ["T4B", "SEED-TOPIC"], order4)
repo4b.close()

bundle5a = app.studio_archive_ui(str(p4), "PUB-4", "topic", "T4B", False, "Writer A")
check("g5: archive via UI succeeds", "Archiviato" in bundle5a[app.ST_IDX_SAVE_MSG], bundle5a[app.ST_IDX_SAVE_MSG])
repo4c = IndustrialRepository(p4 / "ManualSync_CCMS.sqlite")
rows_active = av.outline_rows(repo4c, "PUB-4", include_archived=False)
check("g5: archived topic is excluded from the default (non-archived) outline", "T4B" not in [r["id"] for r in rows_active], rows_active)
repo4c.close()
bundle5b = app.studio_restore_obj_ui(str(p4), "PUB-4", "topic", "T4B", False, "Writer A")
check("g5: restore via UI succeeds", "Ripristinato" in bundle5b[app.ST_IDX_SAVE_MSG], bundle5b[app.ST_IDX_SAVE_MSG])
repo4d = IndustrialRepository(p4 / "ManualSync_CCMS.sqlite")
rows_active2 = av.outline_rows(repo4d, "PUB-4", include_archived=False)
check("g5: restored topic reappears in the outline", "T4B" in [r["id"] for r in rows_active2], rows_active2)
repo4d.close()

# ------------------------------------------------------------------
# Groups 6-9 (AC#6-9): create paragraph/note/warning/list_item blocks.
# ------------------------------------------------------------------
p6 = _new_project("g06_create_block_types")
created = {}
for bt in ("paragraph", "note", "warning", "list_item"):
    bundle = app.studio_new_block_ui(str(p6), "PUB-MAIN", "topic", "SEED-TOPIC", bt, False, "Writer A")
    check(f"g6-9: create {bt} block via the real UI callback succeeds and opens the new block",
          bundle[app.ST_IDX_KIND] == "block" and "✅" in bundle[app.ST_IDX_SAVE_MSG], bundle[app.ST_IDX_SAVE_MSG])
    created[bt] = bundle[app.ST_IDX_ID]
repo6, _ = av.open_service(p6)
for bt, bid in created.items():
    row = repo6.conn.execute("SELECT block_type FROM content_blocks WHERE block_id=?", (bid,)).fetchone()
    check(f"g6-9: {bt} block was actually created with the correct block_type", row["block_type"] == bt, dict(row))
repo6.close()


def _create_disallowed_type():
    repo, svc = av.open_service(p6)
    try:
        svc.create_block_in_topic("SEED-TOPIC", "heading", actor="Writer A")
    finally:
        repo.close()


check("g6-9: creating a NOT-yet-supported type (e.g. 'heading') is refused -- no new enum introduced this increment",
      _raises(_create_disallowed_type, ValueError), None)

# ------------------------------------------------------------------
# Group 10 (AC#10) + Group 11 (AC#11) + Group 12 (AC#12): edit/save,
# reorder, archive/restore blocks.
# ------------------------------------------------------------------
p10 = _new_project("g10_edit_reorder_archive_block")
repo10 = IndustrialRepository(p10 / "ManualSync_CCMS.sqlite")
repo10.add_block("BLK-B", "SEED-TOPIC", "note", position=2, text="Second block.", status="Draft")
repo10.close()
bundle10 = _save_ui(str(p10), "PUB-MAIN", "block", "SEED-BLK", 1, "", "Edited text.", "Writer A", False)
check("g10: edit/save block via the real UI callback succeeds", "✅" in bundle10[app.ST_IDX_SAVE_MSG], bundle10[app.ST_IDX_SAVE_MSG])
repo10b = IndustrialRepository(p10 / "ManualSync_CCMS.sqlite")
check("g10: the live block text is actually updated", repo10b.conn.execute(
    "SELECT text_content FROM content_blocks WHERE block_id='SEED-BLK'").fetchone()["text_content"] == "Edited text.", None)
repo10b.close()

bundle11 = app.studio_move_ui(str(p10), "PUB-MAIN", "block", "BLK-B", "up", False, "Writer A")
repo10c = IndustrialRepository(p10 / "ManualSync_CCMS.sqlite")
order11 = [r["block_id"] for r in repo10c.conn.execute("SELECT block_id FROM content_blocks WHERE topic_id='SEED-TOPIC' ORDER BY position")]
check("g11: move block up via the real UI callback reorders within the topic", order11 == ["BLK-B", "SEED-BLK"], order11)
repo10c.close()

bundle12a = app.studio_archive_ui(str(p10), "PUB-MAIN", "block", "BLK-B", False, "Writer A")
check("g12: archive block via UI succeeds", "Archiviato" in bundle12a[app.ST_IDX_SAVE_MSG], bundle12a[app.ST_IDX_SAVE_MSG])
bundle12b = app.studio_restore_obj_ui(str(p10), "PUB-MAIN", "block", "BLK-B", False, "Writer A")
check("g12: restore block via UI succeeds", "Ripristinato" in bundle12b[app.ST_IDX_SAVE_MSG], bundle12b[app.ST_IDX_SAVE_MSG])

# ------------------------------------------------------------------
# Group 13 (AC#13) + Group 14 (AC#14): draft recovery persists without a
# version bump, and reopening shows it's available.
# ------------------------------------------------------------------
p13 = _new_project("g13_draft_recovery")
msg13 = _save_draft_ui(str(p13), "block", "SEED-BLK", 1, "", "Unsaved draft text.", "Writer A")
check("g13: draft saved via the real UI callback confirms no new version", "nessuna nuova versione" in msg13, msg13)
repo13, svc13 = av.open_service(p13)
check("g13: the live block version is unchanged after a draft save", repo13.conn.execute(
    "SELECT version FROM content_blocks WHERE block_id='SEED-BLK'").fetchone()["version"] == 1, None)
vcount13 = repo13.conn.execute("SELECT COUNT(*) c FROM block_versions WHERE block_id='SEED-BLK'").fetchone()["c"]
check("g13: no block_versions row was inserted for the draft", vcount13 == 1, vcount13)
repo13.close()

repo13b, svc13b = av.open_service(p13)  # fresh instance
detail13 = av.block_detail(svc13b, "SEED-BLK")
check("g14: reopening the block (fresh service instance) shows the draft is available",
      detail13["has_draft"] and detail13["draft_notice"], detail13)
repo13b.close()

# ------------------------------------------------------------------
# Group 15 (AC#15): discard draft.
# ------------------------------------------------------------------
bundle15 = _discard_draft_ui(str(p13), "PUB-MAIN", "block", "SEED-BLK", False)
check("g15: discard draft via the real UI callback succeeds", "scartata" in bundle15[app.ST_IDX_SAVE_MSG], bundle15[app.ST_IDX_SAVE_MSG])
repo15, svc15 = av.open_service(p13)
check("g15: the draft is actually gone after discard", svc15.load_draft("block", "SEED-BLK") is None, None)
repo15.close()

# ------------------------------------------------------------------
# Group 16 (AC#16): Save from a draft state creates exactly ONE version.
# ------------------------------------------------------------------
p16 = _new_project("g16_save_from_draft_one_version")
_save_draft_ui(str(p16), "block", "SEED-BLK", 1, "", "Draft content.", "Writer A")
bundle16 = _save_ui(str(p16), "PUB-MAIN", "block", "SEED-BLK", 1, "", "Draft content.", "Writer A", False)
check("g16: Save succeeds", "✅" in bundle16[app.ST_IDX_SAVE_MSG], bundle16[app.ST_IDX_SAVE_MSG])
repo16, _ = av.open_service(p16)
vcount16 = repo16.conn.execute("SELECT COUNT(*) c FROM block_versions WHERE block_id='SEED-BLK'").fetchone()["c"]
check("g16: exactly 2 block_versions rows exist (v1 original + v2 saved) -- ONE new version from the draft save", vcount16 == 2, vcount16)
repo16.close()

# ------------------------------------------------------------------
# Group 17 (AC#17): a version conflict preserves the draft.
# ------------------------------------------------------------------
p17 = _new_project("g17_conflict_preserves_draft")
_save_ui(str(p17), "PUB-MAIN", "block", "SEED-BLK", 1, "", "First writer saved.", "Writer A", False)  # -> v2
_save_draft_ui(str(p17), "block", "SEED-BLK", 1, "", "Second writer's stale draft.", "Writer B")
bundle17 = _save_ui(str(p17), "PUB-MAIN", "block", "SEED-BLK", 1, "", "Second writer's stale attempt.", "Writer B", False)
check("g17: a stale Save (expected_version=1, live is now 2) surfaces the conflict message, not a crash",
      "modificato da un'altra sessione" in bundle17[app.ST_IDX_SAVE_MSG], bundle17[app.ST_IDX_SAVE_MSG])
repo17, svc17 = av.open_service(p17)
draft17 = svc17.load_draft("block", "SEED-BLK")
check("g17: the draft written before the conflicting Save attempt is still recoverable",
      draft17 is not None and draft17["payload"]["text"] == "Second writer's stale draft.", draft17)
repo17.close()

# ------------------------------------------------------------------
# Group 18 (AC#18): editing Approved content makes it visibly Draft again.
# ------------------------------------------------------------------
p18 = _new_project("g18_approved_edit_to_draft")
repo18 = IndustrialRepository(p18 / "ManualSync_CCMS.sqlite")
rid18_first = repo18.request_review("block", "SEED-BLK", requested_by="Writer A")
repo18.decide_review(rid18_first, decision="APPROVE", actor="Reviewer X")  # closes rid18_first; status -> Approved
# A second review opened directly against the backend (bypassing the
# editor's own Draft-only "Invia in revisione" gate, exactly as
# grounded_queue_ui or a future caller might) while the block is still
# Approved -- the OPEN task this edit must invalidate.
rid18_reopened = repo18.request_review("block", "SEED-BLK", requested_by="Writer A")
repo18.close()
bundle18 = _save_ui(str(p18), "PUB-MAIN", "block", "SEED-BLK", 1, "", "Edited after approval.", "Writer A", False)
check("g18: the status HTML reflects the Approved-content edit reverting to Draft",
      "tornato a Bozza" in bundle18[app.ST_IDX_SAVE_MSG], bundle18[app.ST_IDX_SAVE_MSG])
repo18b, svc18b = av.open_service(p18)
detail18 = av.block_detail(svc18b, "SEED-BLK")
check("g18: block_detail's status_label is now Bozza (Draft), not Approvato", detail18["status_label"] == "Bozza", detail18)
task18_first = repo18b.conn.execute("SELECT status,decision FROM review_tasks WHERE review_id=?", (rid18_first,)).fetchone()
check("g18: the FIRST, already-decided review stays exactly as it was (historical, never re-touched)",
      task18_first["status"] == "CLOSED" and task18_first["decision"] == "APPROVE", dict(task18_first))
task18_reopened = repo18b.conn.execute("SELECT status,decision FROM review_tasks WHERE review_id=?", (rid18_reopened,)).fetchone()
check("g18: the still-OPEN review riding the edited version is invalidated, not left dangling as if still valid",
      task18_reopened["status"] == "CLOSED" and task18_reopened["decision"] == "INVALIDATED_BY_EDIT", dict(task18_reopened))
repo18b.close()

# ------------------------------------------------------------------
# Group 19 (AC#19): review request from within the editor context.
# ------------------------------------------------------------------
p19 = _new_project("g19_review_from_editor")
bundle19 = app.studio_submit_review_ui(str(p19), "PUB-MAIN", "block", "SEED-BLK", False, "Writer A")
check("g19: submitting for review from the editor succeeds", "Inviato in revisione" in bundle19[app.ST_IDX_REVIEW_MSG], bundle19[app.ST_IDX_REVIEW_MSG])
repo19, svc19 = av.open_service(p19)
detail19 = av.block_detail(svc19, "SEED-BLK")
check("g19: the editor's own detail view shows the OPEN review", detail19["open_review"] is not None, detail19["open_review"])
repo19.close()

# ------------------------------------------------------------------
# Group 20 (AC#20): history list.
# ------------------------------------------------------------------
p20 = _new_project("g20_history_list")
_save_ui(str(p20), "PUB-MAIN", "block", "SEED-BLK", 1, "", "v2 text.", "Writer A", False)
repo20, svc20 = av.open_service(p20)
hist20 = av.block_history_rows(svc20, "SEED-BLK")
check("g20: history has both versions, newest first", [h["Versione"] for h in hist20] == [2, 1], hist20)
check("g20: history rows carry status-at-save-time and a preview", "Stato al salvataggio" in hist20[0] and "Anteprima testo" in hist20[0], hist20[0])
repo20.close()

# ------------------------------------------------------------------
# Group 21 (AC#21): revert.
# ------------------------------------------------------------------
p21 = _new_project("g21_revert")
_save_ui(str(p21), "PUB-MAIN", "block", "SEED-BLK", 1, "", "Changed text.", "Writer A", False)
bundle21 = _revert_ui(str(p21), "PUB-MAIN", "block", "SEED-BLK", 1, False, "Writer A")
check("g21: revert via the real UI callback succeeds", "ripristinata" in bundle21[app.ST_IDX_SAVE_MSG], bundle21[app.ST_IDX_SAVE_MSG])
repo21, _ = av.open_service(p21)
row21 = repo21.conn.execute("SELECT version,text_content FROM content_blocks WHERE block_id='SEED-BLK'").fetchone()
check("g21: revert produced a NEW version (3) with version-1 content restored", row21["version"] == 3 and row21["text_content"] == "Seed block.", dict(row21))
repo21.close()

# ------------------------------------------------------------------
# Group 22 (AC#22) + Group 23 (AC#23): search topic / search block.
# ------------------------------------------------------------------
p22 = _new_project("g22_search")
repo22 = IndustrialRepository(p22 / "ManualSync_CCMS.sqlite")
repo22.create_topic("T22-FINDME", "Findable Widget Topic", "", status="Draft")
repo22.create_publication("PUB-22", "Manual 22", ["SEED-TOPIC", "T22-FINDME"])
repo22.add_block("T22-BLK-FINDME", "SEED-TOPIC", "paragraph", position=2, text="A very specific searchable phrase.", status="Draft")
results_topic = av.search(repo22, "PUB-22", "Findable Widget")
check("g22: search finds a topic by title", any(r["kind"] == "topic" and r["id"] == "T22-FINDME" for r in results_topic), results_topic)
results_block = av.search(repo22, "PUB-22", "specific searchable phrase")
check("g23: search finds a block by its text content", any(r["kind"] == "block" and r["id"] == "T22-BLK-FINDME" for r in results_block), results_block)
repo22.close()

# ------------------------------------------------------------------
# Group 24 (AC#24): table/figure/component_ref are never corrupted by the
# text editor.
# ------------------------------------------------------------------
p24 = _new_project("g24_no_corruption_of_structural_blocks")
repo24 = IndustrialRepository(p24 / "ManualSync_CCMS.sqlite")
repo24.create_table("TBL-24", ["A", "B"], [["1", "2"]], title="A table", status="Approved")
repo24.add_block("BLK-TABLE-24", "SEED-TOPIC", "table", position=2, object_ref_id="TBL-24", status="Approved")
repo24.close()
repo24b, svc24b = av.open_service(p24)
detail24 = av.block_detail(svc24b, "BLK-TABLE-24")
check("g24: block_detail marks a table block as NOT editable here", detail24["editable"] is False, detail24)
check("g24: the type label is the honest 'available in a future increment' notice, not a blank editor",
      "prossimo incremento" in detail24["type_label"], detail24["type_label"])
repo24b.close()


def _try_corrupt_table_block():
    repo, svc = av.open_service(p24)
    try:
        svc.save_block("BLK-TABLE-24", text="corrupted via generic textarea", expected_version=1, actor="Writer A")
    finally:
        repo.close()


check("g24: AuthoringService itself refuses to save text onto a table block (defense in depth, not just a hidden UI control)",
      _raises(_try_corrupt_table_block, ValueError), None)
repo24c = IndustrialRepository(p24 / "ManualSync_CCMS.sqlite")
tbl_after = repo24c.conn.execute("SELECT rows_json FROM structured_tables WHERE table_id='TBL-24'").fetchone()
check("g24: the actual table data is untouched", '"1"' in tbl_after["rows_json"] and '"2"' in tbl_after["rows_json"], tbl_after["rows_json"])
repo24c.close()

# ------------------------------------------------------------------
# Group 25 (AC#25) + Group 26 (AC#26): standard mode needs no technical
# IDs; advanced details expose them.
# ------------------------------------------------------------------
check("g25: create_topic_in_publication's public signature never requires an id parameter (title + optional after_topic_id only)",
      "topic_id" not in AuthoringService.create_topic_in_publication.__code__.co_varnames[
          :AuthoringService.create_topic_in_publication.__code__.co_argcount], None)
p25 = _new_project("g25_standard_vs_advanced")
repo25, svc25 = av.open_service(p25)
detail25 = av.topic_detail(svc25, "SEED-TOPIC")
check("g25: topic_detail's friendly fields never require the caller to already know a raw ID beyond what was opened",
      "status_label" in detail25 and "title" in detail25, list(detail25.keys()))
tech_html25 = app._studio_tech_html_topic(detail25)
check("g26: the advanced-mode tech HTML DOES expose the raw topic_id", "SEED-TOPIC" in tech_html25, tech_html25)
repo25.close()

# ------------------------------------------------------------------
# Group 27 (AC#27): refresh/navigation preserve the correct persisted state.
# ------------------------------------------------------------------
p27 = _new_project("g27_refresh_persistence")
_save_ui(str(p27), "PUB-MAIN", "block", "SEED-BLK", 1, "", "Persisted across reload.", "Writer A", False)
bundle27_first = app.studio_open_ui(str(p27), "PUB-MAIN", "block:SEED-BLK", False)
bundle27_reloaded = app.studio_load_ui(str(p27), "PUB-MAIN", False)  # simulates a fresh tab-select / reload with nothing pre-opened
check("g27: a fresh load (no prior selection) still reflects the persisted structure correctly",
      "SEED-BLK" in [c[1].split(":")[-1] for c in bundle27_reloaded[app.ST_IDX_PICKER]["choices"]], None)
bundle27_reopen = app.studio_open_ui(str(p27), "PUB-MAIN", "block:SEED-BLK", False)
check("g27: reopening the same block after a simulated reload shows the correct persisted text",
      bundle27_reopen[app.ST_IDX_TEXT]["value"] == "Persisted across reload.", bundle27_reopen[app.ST_IDX_TEXT])


# ------------------------------------------------------------------
# Group 28 (standard-mode validation UX fix): predictable input gaps in the
# Studio "+ Nuovo topic" flow never surface as a generic gr.Error toast,
# never mutate anything, and never lose the current selection or what the
# user already typed. See app.py's _studio_noop_bundle().
# ------------------------------------------------------------------
p28 = _new_project("g28_predictable_validation_ux")
repo28, svc28 = av.open_service(p28)
count_before28 = repo28.conn.execute("SELECT COUNT(*) c FROM topics").fetchone()["c"]
repo28.close()

# Open an existing object first, so a validation failure on an UNRELATED
# action (creating a topic) can be checked to never disturb it.
opened28 = app.studio_open_ui(str(p28), "PUB-MAIN", "topic:SEED-TOPIC", False)
check("g28 setup: SEED-TOPIC is open before the validation attempts", opened28[app.ST_IDX_ID] == "SEED-TOPIC", opened28[app.ST_IDX_ID])

# 1) new topic without actor -> no mutation, no exception, inline guidance.
bundle28a = app.studio_new_topic_ui(str(p28), "PUB-MAIN", "A Title The User Typed", "topic", "SEED-TOPIC", False, "")
check("g28.1: empty actor produces plain inline guidance, not a raw exception dump or a '⛔' backend-rejection marker",
      bundle28a[app.ST_IDX_SAVE_MSG] == "Inserisci il tuo nome per continuare.", bundle28a[app.ST_IDX_SAVE_MSG])
repo28a = IndustrialRepository(p28 / "ManualSync_CCMS.sqlite")
check("g28.1: no topic was created in the DB", repo28a.conn.execute("SELECT COUNT(*) c FROM topics").fetchone()["c"] == count_before28, None)
repo28a.close()

# 2) new topic without title -> no mutation, no exception, inline guidance.
bundle28b = app.studio_new_topic_ui(str(p28), "PUB-MAIN", "   ", "topic", "SEED-TOPIC", False, "Writer A")
check("g28.2: empty title produces the expected inline guidance",
      bundle28b[app.ST_IDX_SAVE_MSG] == "Inserisci un titolo per il nuovo topic.", bundle28b[app.ST_IDX_SAVE_MSG])
repo28b = IndustrialRepository(p28 / "ManualSync_CCMS.sqlite")
check("g28.2: no topic was created in the DB", repo28b.conn.execute("SELECT COUNT(*) c FROM topics").fetchone()["c"] == count_before28, None)
repo28b.close()

# 3) neither validation-failure bundle is an exception/generic-error state:
#    both are normal, well-formed ST_OUT_FULL-length tuples the Gradio
#    wiring can render directly (studio_new_topic_ui returned instead of
#    raising).
check("g28.3: the empty-actor validation-failure bundle is a normal, full-length tuple, not an exception",
      len(bundle28a) == len(app.ST_OUT_FULL), (len(bundle28a), len(app.ST_OUT_FULL)))
check("g28.3: the empty-title validation-failure bundle is a normal, full-length tuple, not an exception",
      len(bundle28b) == len(app.ST_OUT_FULL), (len(bundle28b), len(app.ST_OUT_FULL)))

# 4) inputs already filled in / already selected are preserved: the State
#    outputs (current selection) are Gradio skip-markers (no "value" key),
#    so the client keeps st_kind/st_id exactly as they were -- and the
#    outline table is skipped too, so nothing is force-refreshed either.
for label, bundle in (("g28.1", bundle28a), ("g28.2", bundle28b)):
    check(f"{label}: st_kind output is a skip-marker (current selection preserved, not reset)",
          isinstance(bundle[app.ST_IDX_KIND], dict) and "value" not in bundle[app.ST_IDX_KIND], bundle[app.ST_IDX_KIND])
    check(f"{label}: st_id output is a skip-marker (current selection preserved, not reset)",
          isinstance(bundle[app.ST_IDX_ID], dict) and "value" not in bundle[app.ST_IDX_ID], bundle[app.ST_IDX_ID])
    check(f"{label}: the outline table output is a skip-marker (nothing changed, nothing to refresh)",
          isinstance(bundle[app.ST_IDX_TABLE], dict) and "value" not in bundle[app.ST_IDX_TABLE], bundle[app.ST_IDX_TABLE])

# 5) actor + title valid -> create PASS, exactly as before this fix.
bundle28c = app.studio_new_topic_ui(str(p28), "PUB-MAIN", "A Real New Topic", "topic", "SEED-TOPIC", False, "Writer A")
check("g28.5: valid actor+title creates the topic normally and opens it",
      bundle28c[app.ST_IDX_KIND] == "topic" and "✅" in bundle28c[app.ST_IDX_SAVE_MSG], bundle28c[app.ST_IDX_SAVE_MSG])
new_topic_id28 = bundle28c[app.ST_IDX_ID]
repo28c = IndustrialRepository(p28 / "ManualSync_CCMS.sqlite")
check("g28.5: exactly one new topic now exists (the two failed attempts created nothing)",
      repo28c.conn.execute("SELECT COUNT(*) c FROM topics").fetchone()["c"] == count_before28 + 1, None)

# 6) actor correctly audited.
audit28 = repo28c.conn.execute(
    "SELECT actor FROM audit_events WHERE object_id=? AND action='AUTHORING_CREATE'", (new_topic_id28,)
).fetchone()
check("g28.6: the audit event records the real actor the user typed, never a placeholder/blank", audit28["actor"] == "Writer A", dict(audit28))
repo28c.close()


# 7) the backend itself stays fail-closed for an empty actor when called
#    directly (bypassing the UI entirely) -- the UI fix must never have
#    loosened AuthoringService's own invariant.
def _direct_call_empty_actor():
    repo, svc = av.open_service(p28)
    try:
        svc.create_topic_in_publication("PUB-MAIN", "Should never be created", actor="")
    finally:
        repo.close()


check("g28.7: AuthoringService.create_topic_in_publication still refuses an empty actor when called directly (backend stays fail-closed)",
      _raises(_direct_call_empty_actor, ValueError), None)

# ------------------------------------------------------------------
# Group 29: the same predictable-validation pattern applied elsewhere in
# the Studio flow (submit-review reviewer, revert version selection,
# search-result selection) -- spot checks, not a full re-enumeration.
# ------------------------------------------------------------------
p29 = _new_project("g29_other_predictable_validations")
bundle29a = app.studio_submit_review_ui(str(p29), "PUB-MAIN", "block", "SEED-BLK", False, "")
check("g29: submit-for-review with an empty reviewer name gives inline guidance, not a gr.Error toast",
      bundle29a[app.ST_IDX_REVIEW_MSG] == "Inserisci il tuo nome per continuare.", bundle29a[app.ST_IDX_REVIEW_MSG])
repo29 = IndustrialRepository(p29 / "ManualSync_CCMS.sqlite")
check("g29: no review task was created", repo29.conn.execute("SELECT COUNT(*) c FROM review_tasks").fetchone()["c"] == 0, None)
repo29.close()

bundle29b = _revert_ui(str(p29), "PUB-MAIN", "block", "SEED-BLK", None, False, "Writer A")
check("g29: revert without a chosen version gives inline guidance, not a gr.Error toast",
      bundle29b[app.ST_IDX_SAVE_MSG] == "Seleziona la versione da ripristinare.", bundle29b[app.ST_IDX_SAVE_MSG])

bundle29c = app.studio_search_open_ui(str(p29), "PUB-MAIN", None, False)
check("g29: 'Apri risultato' with nothing picked gives inline guidance, not a gr.Error toast",
      bundle29c[app.ST_IDX_SAVE_MSG] == "Seleziona prima un risultato da aprire.", bundle29c[app.ST_IDX_SAVE_MSG])


def _dep_for(fn_name: str):
    """The wired dependency dict for a studio_*_ui-style callback, found by
    its Python function name (Gradio's default api_name for a plain
    function -- not a lambda). Lets these tests assert against the actual
    wired Blocks graph app.py builds, not a re-implementation of it."""
    for _fn_index, entry in app.demo.fns.items():
        fn = getattr(entry, "fn", None)
        if fn is not None and getattr(fn, "__name__", "") == fn_name:
            return app.demo.config["dependencies"][_fn_index]
    return None


def _deps_targeting(component) -> list:
    cid = component._id
    return [dep for dep in app.demo.config["dependencies"] if any(t[0] == cid for t in dep["targets"])]


def _deps_writing_to(component) -> list:
    cid = component._id
    return [dep for dep in app.demo.config["dependencies"] if cid in dep.get("outputs", [])]


# ------------------------------------------------------------------
# Group 30 (4B.2 Standard UX Clarity Gate): the Contenuti tab's single
# shared identity field, the publication-title header, the non-tabular
# standard outline, and the contextual empty state -- asserted against the
# actual wired app.demo Blocks graph, not just the pure-Python helpers.
# ------------------------------------------------------------------

# 1) exactly ONE identity field reaches the authoring actions.
#
#    Re-aimed by UX Gate 2A. The invariant this group has always
#    protected is "one identity field, plain language, feeding
#    actor_state" -- not the particular component that provides it.
#    4B.2 removed two duplicates (Studio's own st_actor, and the
#    source-approval c_simple_reviewer) and kept a tab-local c_actor.
#    Gate 2A removed that one too: it was a third copy of a question
#    the shell context bar already asks, sitting ~800px above the
#    workspace on a tab whose job is writing. ws_operator is now the
#    single field, and actor_state is fed from it.
check("g30.1: the old, second Studio 'Il tuo nome' field (st_actor) no longer exists", not hasattr(app, "st_actor"), None)
check("g30.1: the old source-approval 'Il tuo nome' field (c_simple_reviewer) no longer exists -- folded into the shared one",
      not hasattr(app, "c_simple_reviewer"), None)
check("g30.1: the tab-local duplicate identity field (c_actor) no longer exists -- folded into the shell operator",
      not hasattr(app, "c_actor"), None)
check("g30.1: the one remaining identity field is the shell workspace operator",
      app.ws_operator.label == i18n.translate("context.operator", "it"), app.ws_operator.label)
check("g30.1: it carries plain-language helper text, not 'actor'/'audit actor'/'user id' jargon",
      app.ws_operator.info == i18n.translate("context.operator.hint", "it")
      and not any(j in (app.ws_operator.info or "").lower() for j in ("actor", "user id", "audit actor")),
      app.ws_operator.info)
check("g30.1: actor_state is fed from that single field, so every mutation callback still sees one identity",
      any(dep.get("inputs") == [app.ws_operator._id] and dep.get("outputs") == [app.actor_state._id]
          for dep in app.demo.config["dependencies"]),
      None)

# 2) Authoring Studio actions are wired to that same shared field's
#    identity (checked against the real dependency graph, not just by
#    convention). Actor state consistency fix: every mutation callback
#    now reads actor_state (a gr.State synced from c_actor via .change()
#    -- proven the only trigger of .input()/.change()/.blur() that
#    actually reaches the backend for a plain Textbox in this Gradio
#    build), never c_actor directly -- see
#    regressione_authoring_tables_figures_4c.py groups 45-54 for the
#    full root-cause writeup and coverage. Still exactly one visible
#    "Il tuo nome" field; nothing here changes that.
dep_new_topic = _dep_for("studio_new_topic_ui")
check("g30.2: studio_new_topic_ui's wired inputs end with the shared actor_state (sourced from the single c_actor field)",
      dep_new_topic is not None and dep_new_topic["inputs"][-1] == app.actor_state._id, dep_new_topic)
dep_save = _dep_for("studio_save_ui")
check("g30.2: studio_save_ui's wired inputs include the shared actor_state",
      dep_save is not None and app.actor_state._id in dep_save["inputs"], dep_save)

# 3) source approval uses the same shared value where appropriate.
dep_approve = _dep_for("guided_approve_source_ui")
check("g30.3: 'Approva sorgente' (guided_approve_source_ui) is wired to the shared actor_state",
      dep_approve is not None and app.actor_state._id in dep_approve["inputs"], dep_approve)

# 4) a genuinely separate reviewer role keeps its own explicit "Revisore"
#    label -- never a second "Il tuo nome".
check("g30.4: the review-decide panel's reviewer field is explicitly labelled 'Revisore'", app.rv_actor.label == "Revisore", app.rv_actor.label)
check("g30.4: the raw-approval (advanced) reviewer field is labelled 'Revisore umano', not 'Il tuo nome'", app.c_reviewer.label == "Revisore umano", app.c_reviewer.label)
check("g30.4: the grounded-queue reviewer field is labelled 'Revisore'", app.g_reviewer.label == "Revisore", app.g_reviewer.label)

# 5) the publication title is visible in the Editor professionale header.
p30 = _new_project("g30_ux_clarity")
repo30 = IndustrialRepository(p30 / "ManualSync_CCMS.sqlite")
repo30.create_publication("PUB-30", "Manuale Pompa HX-400", ["SEED-TOPIC"])
repo30.close()
bundle30_load = app.studio_load_ui(str(p30), "PUB-30", False)
check("g30.5: the Studio header shows the REAL publication title, not the generic word 'Pubblicazione'",
      "Manuale Pompa HX-400" in bundle30_load[app.ST_IDX_PUB_HEADER], bundle30_load[app.ST_IDX_PUB_HEADER])

# 6) the standard outline (the picker/radio choices a normal user sees)
#    never exposes a raw technical id in its visible label.
repo30b, svc30b = av.open_service(p30)
rows30 = av.outline_rows(repo30b, "PUB-30")
choices30 = av.open_choices(rows30)
check("g30.6: no visible outline label contains a raw topic_id/block_id (only the hidden picker value does)",
      all(not c[0].startswith(("TOPIC-", "BLK-", "SEED-TOPIC")) for c in choices30), choices30)
repo30b.close()

# 7) the standard outline does not require the technical Dataframe as
#    navigation: the Dataframe is display-only (nothing is wired to open on
#    interacting with it), while the actual one-click opener is a Radio.
check("g30.7: st_outline_table (the technical Dataframe) has no interaction wired to it -- diagnostics only",
      len(_deps_targeting(app.st_outline_table)) == 0, None)
check("g30.7: st_outline_radio (the readable structure) is a gr.Radio, not a Dataframe", type(app.st_outline_radio).__name__ == "Radio", type(app.st_outline_radio).__name__)
check("g30.7: st_outline_radio is exactly what opens an item on a single interaction (one wired 'change' event)",
      len(_deps_targeting(app.st_outline_radio)) == 1, None)

# 8) a clear, simple empty state when nothing is selected -- and the
#    contextual action controls are actually hidden (visible=False on each
#    one directly, not just interactive=False), so there is no wall of
#    greyed-out buttons. (Checked per-button rather than via one shared
#    "group visible" slot -- an earlier version used a single dynamically
#    visible gr.Group for this, but Gradio 6.5.1 was found not to reliably
#    apply a child's own visible= update when nested inside a Group whose
#    own visibility does not change in that same response; see the comment
#    on _studio_full_bundle.)
check("g30.8: the empty-state message matches the required wording exactly",
      bundle30_load[app.ST_IDX_STATUS] == "<div class='ms-muted'>Seleziona un topic o un blocco dalla struttura a sinistra, oppure crea un nuovo topic.</div>",
      bundle30_load[app.ST_IDX_STATUS])
_ACTION_BTN_IDXS = [app.ST_IDX_SAVE_BTN, app.ST_IDX_DISCARD_EDITS_BTN, app.ST_IDX_MOVE_UP_BTN,
                    app.ST_IDX_MOVE_DOWN_BTN, app.ST_IDX_ARCHIVE_BTN, app.ST_IDX_RESTORE_BTN,
                    app.ST_IDX_SUBMIT_REVIEW_BTN]
check("g30.8: every action button is hidden (visible=False), not just disabled, when nothing is open",
      all(bundle30_load[i] == {"__type__": "update", "visible": False, "interactive": False} for i in _ACTION_BTN_IDXS),
      [bundle30_load[i] for i in _ACTION_BTN_IDXS])
bundle30_open = app.studio_open_ui(str(p30), "PUB-30", "topic:SEED-TOPIC", False)
check("g30.8: every action button becomes visible once something is open",
      all(bundle30_open[i].get("visible") is True for i in _ACTION_BTN_IDXS),
      [bundle30_open[i] for i in _ACTION_BTN_IDXS])
check("g30.8: opening a TOPIC shows 'Titolo' and hides 'Testo'",
      bundle30_open[app.ST_IDX_TITLE]["visible"] is True and bundle30_open[app.ST_IDX_TEXT]["visible"] is False,
      (bundle30_open[app.ST_IDX_TITLE], bundle30_open[app.ST_IDX_TEXT]))
bundle30_block = app.studio_new_block_ui(str(p30), "PUB-30", "topic", "SEED-TOPIC", "paragraph", False, "Writer A")
# NOTE: this confirms the DATA _studio_full_bundle sends is correct (it
# always was, even in the discarded gr.Group-based version -- the bug an
# isolated repro app confirmed was purely a Gradio 6.5.1 client-rendering
# issue: a Group whose own visibility does not change in a given response
# did not reliably apply a nested child's own visible= update, so the
# browser could show both fields at once despite a correct payload like
# this one). That rendering defect is only observable in a real browser --
# it is what the browser smoke test below re-verifies against the actual
# DOM, not this backend check.
check("g30.8: the bundle for a newly opened BLOCK correctly hides 'Titolo' and keeps the hidden canonical "
      "'Testo' Textbox hidden -- 4E.3 stabilization (Blocker 1) makes st_text_input a PERMANENTLY hidden "
      "canonical-text carrier; the standard editor surface the writer actually sees/edits is now "
      "st_rich_editable (a contenteditable div), which the next check confirms becomes visible instead",
      bundle30_block[app.ST_IDX_TITLE]["visible"] is False and bundle30_block[app.ST_IDX_TEXT]["visible"] is False,
      (bundle30_block[app.ST_IDX_TITLE], bundle30_block[app.ST_IDX_TEXT]))
check("g30.8b (4E.3): opening an editable BLOCK makes the standard contenteditable editor surface visible "
      "(st_rich_editable/st_rich_toolbar_row) -- the actual writer-facing surface, replacing the old raw "
      "Textbox as the standard editing UI",
      bundle30_block[app.ST_IDX_RICH_EDITABLE]["visible"] is True and
      bundle30_block[app.ST_IDX_RICH_TOOLBAR_ROW]["visible"] is True,
      (bundle30_block[app.ST_IDX_RICH_EDITABLE], bundle30_block[app.ST_IDX_RICH_TOOLBAR_ROW]))

# 9) actor session persistence across navigation: nothing in the entire
#    wired app ever writes a value INTO c_actor -- so no server response
#    (opening a different topic, a validation failure, switching tabs) can
#    ever reset what the user typed. This is the mechanism, not just an
#    assumption: a full page reload (F5) is a fresh browser session and is
#    documented as the one case that does start over, per Gradio's normal
#    per-session component state.
#    Gate 2A note: the operator field IS an output of exactly one event --
#    the language switch, which rewrites its label/info/placeholder. That
#    is not a value write, so the guarantee is unchanged; assert the
#    precise thing rather than the blunt "never an output", which would
#    now pass only by forbidding localization of the field.
_operator_writers = _deps_writing_to(app.ws_operator)
check("g30.9: exactly ONE wired event writes to the operator field",
      len(_operator_writers) == 1, _operator_writers)
check("g30.9: and that event is the language switch -- it writes to every LOC-registered component at once",
      len(_operator_writers[0].get("outputs", [])) == len(app.LOC),
      (len(_operator_writers[0].get("outputs", [])), len(app.LOC)))
_op_entry = next(builders for comp, builders in app.LOC._entries if comp is app.ws_operator)
check("g30.9: the language switch rewrites the operator's LABEL only -- it never emits a value, "
      "so switching IT<->EN cannot clear what the user typed",
      "value" not in _op_entry and set(_op_entry) <= {"label", "info", "placeholder"}, _op_entry)
with i18n.use_language("en"):
    _op_update = app.gr.update(**{p: (b() if callable(b) else app.t(b)) for p, b in _op_entry.items()})
check("g30.9: and the update it actually emits carries no value key",
      "value" not in {k for k, v in dict(_op_update).items() if v is not None}
      or dict(_op_update).get("value") is None, dict(_op_update))

# 10) every predictable validation in the Studio flow still returns a
#     normal, full-length bundle (never a generic gr.Error) after this
#     UX pass -- re-checked against the new (post-Group-removal) output
#     shape.
bundle30_noactor = app.studio_new_topic_ui(str(p30), "PUB-30", "A Title", "topic", "SEED-TOPIC", False, "")
check("g30.10: empty actor still gives inline guidance (not a gr.Error) after the layout changes",
      bundle30_noactor[app.ST_IDX_SAVE_MSG] == "Inserisci il tuo nome per continuare.", bundle30_noactor[app.ST_IDX_SAVE_MSG])
check("g30.10: the failure bundle is still the full, well-formed ST_OUT_FULL shape",
      len(bundle30_noactor) == len(app.ST_OUT_FULL), (len(bundle30_noactor), len(app.ST_OUT_FULL)))


print(json.dumps({"pass": sum(r["status"] == "PASS" for r in results), "tests": len(results)}))
if any(r["status"] != "PASS" for r in results):
    print(json.dumps(results, indent=2))
    sys.exit(1)
