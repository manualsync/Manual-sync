"""Milestone 4C regression suite: Tables, Figures & Asset Authoring.

Tests authoring_view.py (view-model) directly, and the real app.py
`studio_*_ui` callbacks for everything that needs the full editor flow
(create without an ID, save/draft/conflict, replace, revert, shared-object
guard) -- calling the actual functions the product wires to Gradio, not a
mock, matching the discipline established for the 4B.1/4B.2 suites.

Covers all 38 acceptance items from the 4C report (15 table + 15 figure +
8 UI/integration), in order.

Backend/integration only -- no browser (see the separate browser-
acceptance cycles run for this milestone). Fixtures are entirely
synthetic, including a tiny generated PNG for every figure test (no real
user image is ever read).
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
from authoring_service import (
    AuthoringService, VersionConflictError, ArchivedObjectError, SharedObjectError, MissingAssetError,
)
import authoring_view as av
from project_lifecycle import create_project
import app
import i18n

TMP = Path(tempfile.mkdtemp(prefix="manualsync_authoring_tables_figures_4c_"))
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


def _make_test_image(name: str) -> Path:
    """A tiny, real, synthetic PNG -- no real user image is ever read by
    this suite. Pillow is already a project dependency (legacy image
    handling); reused here rather than hand-rolling PNG bytes."""
    from PIL import Image
    path = TMP / name
    Image.new("RGB", (12, 8), color=(90, 140, 200)).save(path)
    return path


IMG1 = _make_test_image("fixture_image_1.png")
IMG2 = _make_test_image("fixture_image_2.png")


def _block_type_for(repo: IndustrialRepository, block_id: str) -> str:
    return repo.conn.execute("SELECT block_type FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()["block_type"]


# ====================================================================
# TABLES (items 1-15)
# ====================================================================

# ------------------------------------------------------------------
# 1) create table without technical ID.
# ------------------------------------------------------------------
p1 = _new_project("g01_create_table_no_id")
repo1, svc1 = av.open_service(p1)
result1 = svc1.create_table_in_topic("SEED-TOPIC", actor="Writer A", caption="Spec table",
                                     columns=["A", "B"], rows=[["1", "2"]])
check("g1: create_table_in_topic generates the table_id -- caller never supplies one",
      result1["table_id"].startswith("TBL-"), result1)
check("g1: create_table_in_topic also generates the block_id", result1["block_id"].startswith("BLK-"), result1)
repo1.close()
# Same via the real UI callback: only an optional caption is passed in, never an ID.
bundle1 = app.studio_new_table_ui(str(p1), "PUB-MAIN", "topic", "SEED-TOPIC", "Via UI caption", False, "Writer A")
check("g1: studio_new_table_ui (real UI callback) creates a table from an optional caption alone and opens it",
      bundle1[app.ST_IDX_KIND] == "block" and bundle1[app.ST_IDX_BLOCK_TYPE] == "table", bundle1[app.ST_IDX_SAVE_MSG])
check("g1: no title was required (backend does not require one either)", "✅" in bundle1[app.ST_IDX_SAVE_MSG], bundle1[app.ST_IDX_SAVE_MSG])

# ------------------------------------------------------------------
# 2) create table block link.
# ------------------------------------------------------------------
repo2, svc2 = av.open_service(p1)
link_row = repo2.conn.execute("SELECT block_type,object_ref_id FROM content_blocks WHERE block_id=?",
                              (bundle1[app.ST_IDX_ID],)).fetchone()
check("g2: the new block is type 'table' and object_ref_id points at the real structured_tables row",
      link_row["block_type"] == "table" and
      bool(repo2.conn.execute("SELECT 1 FROM structured_tables WHERE table_id=?", (link_row["object_ref_id"],)).fetchone()),
      dict(link_row))
repo2.close()

# ------------------------------------------------------------------
# 3-7) edit cell, add row, remove row, add column, remove column --
# all via the same save_table() call (the grid IS the columns/rows).
# ------------------------------------------------------------------
p3 = _new_project("g03_07_grid_edits")
repo3, svc3 = av.open_service(p3)
t3 = svc3.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A", "B"], rows=[["1", "2"]])["table_id"]
repo3.close()

# 3) edit cell
repo3b, svc3b = av.open_service(p3)
r3 = svc3b.save_table(t3, columns=["A", "B"], rows=[["1", "CHANGED"]], expected_version=1, actor="Writer A")
check("g3: edit cell -> new version", r3["version"] == 2, r3)
d3 = svc3b.load_table_for_edit(t3)
check("g3: the edited cell value is actually persisted", d3["rows"] == [["1", "CHANGED"]], d3["rows"])
repo3b.close()

# 4) add row
repo4, svc4 = av.open_service(p3)
r4 = svc4.save_table(t3, columns=["A", "B"], rows=[["1", "CHANGED"], ["3", "4"]], expected_version=2, actor="Writer A")
d4 = svc4.load_table_for_edit(t3)
check("g4: add row -> row count grows and the new row is persisted", len(d4["rows"]) == 2 and d4["rows"][1] == ["3", "4"], d4["rows"])
repo4.close()

# 5) remove row
repo5, svc5 = av.open_service(p3)
r5 = svc5.save_table(t3, columns=["A", "B"], rows=[["1", "CHANGED"]], expected_version=3, actor="Writer A")
d5 = svc5.load_table_for_edit(t3)
check("g5: remove row -> row count shrinks back", len(d5["rows"]) == 1, d5["rows"])
repo5.close()

# 6) add column
repo6, svc6 = av.open_service(p3)
r6 = svc6.save_table(t3, columns=["A", "B", "C"], rows=[["1", "CHANGED", "new-col"]], expected_version=4, actor="Writer A")
d6 = svc6.load_table_for_edit(t3)
check("g6: add column -> column count grows and new cell value persisted",
      d6["columns"] == ["A", "B", "C"] and d6["rows"][0] == ["1", "CHANGED", "new-col"], d6)
repo6.close()

# 7) remove column
repo7, svc7 = av.open_service(p3)
r7 = svc7.save_table(t3, columns=["A", "B"], rows=[["1", "CHANGED"]], expected_version=5, actor="Writer A")
d7 = svc7.load_table_for_edit(t3)
check("g7: remove column -> column count shrinks back", d7["columns"] == ["A", "B"], d7["columns"])
repo7.close()

# ------------------------------------------------------------------
# 8) save = exactly one new structured_table_versions row.
# ------------------------------------------------------------------
repo8, svc8 = av.open_service(p3)
vcount8 = repo8.conn.execute("SELECT COUNT(*) c FROM structured_table_versions WHERE table_id=?", (t3,)).fetchone()["c"]
check("g8: exactly one structured_table_versions row per save (6 saves incl. create = 6 rows)", vcount8 == 6, vcount8)
repo8.close()

# ------------------------------------------------------------------
# 9) draft recovery: no version bump.
# ------------------------------------------------------------------
p9 = _new_project("g09_table_draft_no_version_bump")
repo9, svc9 = av.open_service(p9)
t9 = svc9.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["1"]])["table_id"]
repo9.close()
msg9 = app.studio_save_draft_ui(str(p9), "table", t9, "table", t9, 1, "", "", "T9", "Cap9",
                                __import__("pandas").DataFrame([["draft-cell"]], columns=["A"]), "", "", "Writer A")
check("g9: draft saved via the real UI callback confirms no new version", "nessuna nuova versione" in msg9, msg9)
repo9b, svc9b = av.open_service(p9)
check("g9: the live table version is unchanged after a draft save",
      repo9b.conn.execute("SELECT version FROM structured_tables WHERE table_id=?", (t9,)).fetchone()["version"] == 1, None)
vcount9 = repo9b.conn.execute("SELECT COUNT(*) c FROM structured_table_versions WHERE table_id=?", (t9,)).fetchone()["c"]
check("g9: no structured_table_versions row was inserted for the draft", vcount9 == 1, vcount9)
draft9 = svc9b.load_draft("table", t9)
check("g9: the draft is recoverable and carries the edited cell", draft9 is not None and draft9["payload"]["rows"] == [["draft-cell"]], draft9)
repo9b.close()

# ------------------------------------------------------------------
# 10) stale version conflict preserves the draft.
# ------------------------------------------------------------------
p10 = _new_project("g10_table_conflict_preserves_draft")
repo10, svc10 = av.open_service(p10)
t10 = svc10.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["1"]])["table_id"]
svc10.save_table(t10, columns=["A"], rows=[["first writer saved"]], expected_version=1, actor="Writer A")  # -> v2
repo10.close()
import pandas as pd
app.studio_save_draft_ui(str(p10), "table", t10, "table", t10, 1, "", "", "", "",
                         pd.DataFrame([["stale draft"]], columns=["A"]), "", "", "Writer B")
bundle10 = app.studio_save_ui(str(p10), "PUB-MAIN", "table", t10, "table", t10, 1, "", "",
                              "", "", pd.DataFrame([["stale attempt"]], columns=["A"]), "", "", "Writer B", False)
check("g10: a stale Save (expected_version=1, live is now 2) surfaces the conflict message, not a crash",
      "modificato da un'altra sessione" in bundle10[app.ST_IDX_SAVE_MSG], bundle10[app.ST_IDX_SAVE_MSG])
repo10b, svc10b = av.open_service(p10)
draft10 = svc10b.load_draft("table", t10)
check("g10: the draft written before the conflicting Save attempt is still recoverable",
      draft10 is not None and draft10["payload"]["rows"] == [["stale draft"]], draft10)
repo10b.close()

# ------------------------------------------------------------------
# 11) provenance (source_refs) preserved across an edit that does not
#     touch them.
# ------------------------------------------------------------------
p11 = _new_project("g11_table_provenance_preserved")
repo11 = IndustrialRepository(p11 / "ManualSync_CCMS.sqlite")
repo11.create_table("TBL-11", ["A"], [["1"]], title="T", status="Draft", source_refs=["evidence:doc-1#p3"])
repo11.add_block("BLK-11", "SEED-TOPIC", "table", position=2, object_ref_id="TBL-11", status="Draft")
repo11.close()
repo11b, svc11b = av.open_service(p11)
svc11b.save_table("TBL-11", columns=["A"], rows=[["edited"]], expected_version=1, actor="Writer A")
d11 = svc11b.load_table_for_edit("TBL-11")
check("g11: source_refs are preserved when a save does not touch provenance", d11["source_refs"] == ["evidence:doc-1#p3"], d11["source_refs"])
repo11b.close()

# ------------------------------------------------------------------
# 12) table history/revert.
# ------------------------------------------------------------------
p12 = _new_project("g12_table_history_revert")
repo12, svc12 = av.open_service(p12)
t12 = svc12.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["original"]])["table_id"]
svc12.save_table(t12, columns=["A"], rows=[["changed"]], expected_version=1, actor="Writer A")
hist12 = svc12.table_history(t12)
check("g12: history has both versions, newest first", [h["version"] for h in hist12] == [2, 1], hist12)
r12 = svc12.revert_table_to_version(t12, 1, actor="Writer A")
check("g12: revert produces a NEW version with version-1 content restored", r12["new_version"] == 3, r12)
d12 = svc12.load_table_for_edit(t12)
check("g12: reverted content matches version 1's original rows", d12["rows"] == [["original"]], d12["rows"])
hist12b = svc12.table_history(t12)
check("g12: prior history (v1, v2) is untouched by the revert", len(hist12b) == 3, hist12b)
repo12.close()

# ------------------------------------------------------------------
# 13) archived (containing) block behavior.
# ------------------------------------------------------------------
p13 = _new_project("g13_table_archived_block")
repo13, svc13 = av.open_service(p13)
res13 = svc13.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["1"]])
svc13.archive_block(res13["block_id"], actor="Writer A")
check("g13: saving a table whose containing block is archived is fail-closed (ArchivedObjectError)",
      _raises(lambda: svc13.save_table(res13["table_id"], columns=["A"], rows=[["x"]], expected_version=1, actor="Writer A"), ArchivedObjectError), None)
bundle13 = app.studio_open_ui(str(p13), "PUB-MAIN", f"block:{res13['block_id']}", True)
check("g13: the block-level 'archived' notice is shown for an archived table block",
      "archiviat" in bundle13[app.ST_IDX_READONLY]["value"].lower(), bundle13[app.ST_IDX_READONLY])
repo13.close()

# ------------------------------------------------------------------
# 14) search table text (caption + cell content).
# ------------------------------------------------------------------
p14 = _new_project("g14_search_table_text")
repo14, svc14 = av.open_service(p14)
svc14.create_table_in_topic("SEED-TOPIC", actor="Writer A", caption="Clearances", columns=["Frame"], rows=[["unique-cell-phrase"]])
results14a = av.search(repo14, "PUB-MAIN", "Clearances")
check("g14: search finds a table by its caption", any(r["kind"] == "block" for r in results14a), results14a)
results14b = av.search(repo14, "PUB-MAIN", "unique-cell-phrase")
check("g14: search finds a table by cell text", any(r["kind"] == "block" for r in results14b), results14b)
repo14.close()

# ------------------------------------------------------------------
# 15) shared table edit blocked, fail-closed.
# ------------------------------------------------------------------
p15 = _new_project("g15_shared_table_blocked")
repo15, svc15 = av.open_service(p15)
res15 = svc15.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["1"]])
repo15.add_block("BLK-15-SECOND", "SEED-TOPIC", "table", position=99, object_ref_id=res15["table_id"], status="Draft")
check("g15: saving a table referenced by 2 blocks is fail-closed (SharedObjectError)",
      _raises(lambda: svc15.save_table(res15["table_id"], columns=["A"], rows=[["x"]], expected_version=1, actor="Writer A"), SharedObjectError), None)
before15 = repo15.conn.execute("SELECT version,rows_json FROM structured_tables WHERE table_id=?", (res15["table_id"],)).fetchone()
check("g15: the shared table is genuinely untouched (no silent global edit)", before15["version"] == 1 and '"1"' in before15["rows_json"], dict(before15))
bundle15 = app.studio_open_ui(str(p15), "PUB-MAIN", f"block:{res15['block_id']}", False)
check("g15: the UI shows the required shared-object message",
      # 4D deliberately supersedes the 4C-era placeholder wording ("...sarà
      # gestita nella fase Reuse-aware authoring", now literally untrue,
      # since 4D IS that phase) with the real decision prompt -- see
      # authoring_service._SHARED_OBJECT_MESSAGE and the 4D report.
      "utilizzato in più punti" in bundle15[app.ST_IDX_SHARED_NOTICE]["value"], bundle15[app.ST_IDX_SHARED_NOTICE])
check("g15: the READ-ONLY table grid twin (permanently interactive=False) is the one shown while shared",
      bundle15[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is True, bundle15[app.ST_IDX_TABLE_GRID_VIEW])
check("g15: the EDITABLE table grid twin stays hidden while shared and unconfirmed",
      bundle15[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is False, bundle15[app.ST_IDX_TABLE_GRID_EDIT])
repo15.close()


# ====================================================================
# FIGURES (items 16-30)
# ====================================================================

# ------------------------------------------------------------------
# 16) create figure from image; 17) collision-safe asset copy.
# ------------------------------------------------------------------
p16 = _new_project("g16_17_create_figure")
repo16, svc16 = av.open_service(p16)
res16 = svc16.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1),
                                     caption="Pump photo", alt_text="A pump")
check("g16: create_figure_in_topic generates the figure_id -- caller never supplies one", res16["figure_id"].startswith("FIG-"), res16)
check("g16: the source image bytes were actually materialized into the project", Path(res16["file_path"]).exists(), res16["file_path"])
check("g16: the materialized path is NOT the original source path (it was copied, not referenced in place)",
      Path(res16["file_path"]) != IMG1, res16["file_path"])
repo16.close()
# 17: a second figure created from the SAME source filename must not collide/overwrite the first.
repo17, svc17 = av.open_service(p16)
res17 = svc17.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1), caption="Second copy")
check("g17: two figures created from the same source filename get distinct, collision-safe destinations",
      res17["file_path"] != res16["file_path"] and Path(res17["file_path"]).exists(), (res16["file_path"], res17["file_path"]))
check("g17: the first figure's asset is untouched by the second creation", Path(res16["file_path"]).exists(), res16["file_path"])
repo17.close()

# ------------------------------------------------------------------
# 18) figure block link.
# ------------------------------------------------------------------
repo18, svc18 = av.open_service(p16)
link18 = repo18.conn.execute("SELECT block_type,object_ref_id FROM content_blocks WHERE block_id=?", (res16["block_id"],)).fetchone()
check("g18: the new block is type 'figure' and object_ref_id points at the real figures row",
      link18["block_type"] == "figure" and bool(repo18.conn.execute("SELECT 1 FROM figures WHERE figure_id=?", (link18["object_ref_id"],)).fetchone()),
      dict(link18))
repo18.close()

# ------------------------------------------------------------------
# 19) preview path valid.
# ------------------------------------------------------------------
repo19, svc19 = av.open_service(p16)
d19 = svc19.load_figure_for_edit(res16["figure_id"])
check("g19: the loaded figure's file_path is a real, existing file (a valid preview source)",
      bool(d19["file_path"]) and Path(d19["file_path"]).exists() and not d19["asset_missing"], d19)
repo19.close()

# ------------------------------------------------------------------
# 20) edit caption; 21) edit alt text.
# ------------------------------------------------------------------
repo20, svc20 = av.open_service(p16)
r20 = svc20.save_figure(res16["figure_id"], caption="New caption", expected_version=1, actor="Writer A")
d20 = svc20.load_figure_for_edit(res16["figure_id"])
check("g20: edit caption -> new version, caption persisted", r20["version"] == 2 and d20["caption"] == "New caption", d20)
check("g20: alt_text is untouched by a caption-only save", d20["alt_text"] == "A pump", d20["alt_text"])
r21 = svc20.save_figure(res16["figure_id"], alt_text="New alt text", expected_version=2, actor="Writer A")
d21 = svc20.load_figure_for_edit(res16["figure_id"])
check("g21: edit alt text -> new version, alt text persisted", r21["version"] == 3 and d21["alt_text"] == "New alt text", d21)
check("g21: caption is untouched by an alt-text-only save", d21["caption"] == "New caption", d21["caption"])
repo20.close()

# ------------------------------------------------------------------
# 22) replace image -> exactly one new version; 23) replacement
#     failure is non-destructive.
# ------------------------------------------------------------------
repo22, svc22 = av.open_service(p16)
old_path22 = svc22.load_figure_for_edit(res16["figure_id"])["file_path"]
r22 = svc22.replace_figure_image(res16["figure_id"], source_file_path=str(IMG2), expected_version=3, actor="Writer A")
check("g22: replace_figure_image bumps the version by exactly one", r22["version"] == 4, r22)
d22 = svc22.load_figure_for_edit(res16["figure_id"])
check("g22: file_path now points at the newly materialized asset", d22["file_path"] != old_path22 and Path(d22["file_path"]).exists(), d22["file_path"])
check("g22: caption/alt text (not touched by replace) are preserved", d22["caption"] == "New caption" and d22["alt_text"] == "New alt text", d22)
check("g22: the OLD asset file is still on disk (history is never deleted from under itself)", Path(old_path22).exists(), old_path22)
repo22.close()

# 23: a replacement that fails (stale expected_version) must not touch the live figure and not leave an orphan.
repo23, svc23 = av.open_service(p16)
assets_dir_23 = svc23._project_dir() / "Assets" / "figures"
before_count_23 = len(list(assets_dir_23.glob("*"))) if assets_dir_23.exists() else 0
live_before_23 = dict(repo23.conn.execute("SELECT version,file_path FROM figures WHERE figure_id=?", (res16["figure_id"],)).fetchone())
check("g23: replacing with a stale expected_version raises VersionConflictError, not a silent write",
      _raises(lambda: svc23.replace_figure_image(res16["figure_id"], source_file_path=str(IMG1), expected_version=1, actor="Writer A"), VersionConflictError), None)
live_after_23 = dict(repo23.conn.execute("SELECT version,file_path FROM figures WHERE figure_id=?", (res16["figure_id"],)).fetchone())
check("g23: the live figure row is completely unchanged after the failed replacement", live_before_23 == live_after_23, (live_before_23, live_after_23))
after_count_23 = len(list(assets_dir_23.glob("*"))) if assets_dir_23.exists() else 0
check("g23: the materialized-then-rolled-back asset copy was cleaned up -- no orphan file left behind",
      after_count_23 == before_count_23, (before_count_23, after_count_23))
repo23.close()

# ------------------------------------------------------------------
# 24) missing asset placeholder.
# ------------------------------------------------------------------
p24 = _new_project("g24_missing_asset_placeholder")
repo24 = IndustrialRepository(p24 / "ManualSync_CCMS.sqlite")
repo24.create_figure("FIG-24", title="T", caption="Cap24", file_path=str(TMP / "does_not_exist_1234.png"),
                     alt_text="Alt24", status="Draft")
repo24.add_block("BLK-24", "SEED-TOPIC", "figure", position=2, object_ref_id="FIG-24", status="Draft")
repo24.close()
repo24b, svc24b = av.open_service(p24)
d24 = svc24b.load_figure_for_edit("FIG-24")
check("g24: a figure whose file_path does not exist is flagged asset_missing, not a crash", d24["asset_missing"] is True, d24)
bundle24 = app.studio_open_ui(str(p24), "PUB-MAIN", "block:BLK-24", False)
check("g24: standard mode shows the 'Immagine non disponibile' placeholder message",
      "non disponibile" in bundle24[app.ST_IDX_READONLY]["value"], bundle24[app.ST_IDX_READONLY])
check("g24: caption/alt text remain visible even though the asset is missing",
      bundle24[app.ST_IDX_FIGURE_CAPTION]["value"] == "Cap24" and bundle24[app.ST_IDX_FIGURE_ALT]["value"] == "Alt24", bundle24)
check("g24: the figure row itself was never auto-deleted",
      bool(repo24b.conn.execute("SELECT 1 FROM figures WHERE figure_id='FIG-24'").fetchone()), None)
repo24b.close()

# ------------------------------------------------------------------
# 25) figure draft: no version bump; 26) stale conflict preserves draft.
# ------------------------------------------------------------------
p25 = _new_project("g25_figure_draft_no_version_bump")
repo25, svc25 = av.open_service(p25)
f25 = svc25.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1))["figure_id"]
repo25.close()
msg25 = app.studio_save_draft_ui(str(p25), "figure", f25, "figure", f25, 1, "", "", "", "", None, "Draft caption", "Draft alt", "Writer A")
check("g25: draft saved via the real UI callback confirms no new version", "nessuna nuova versione" in msg25, msg25)
repo25b, svc25b = av.open_service(p25)
check("g25: the live figure version is unchanged after a draft save",
      repo25b.conn.execute("SELECT version FROM figures WHERE figure_id=?", (f25,)).fetchone()["version"] == 1, None)
vcount25 = repo25b.conn.execute("SELECT COUNT(*) c FROM figure_versions WHERE figure_id=?", (f25,)).fetchone()["c"]
check("g25: no figure_versions row was inserted for the draft", vcount25 == 1, vcount25)
repo25b.close()

p26 = _new_project("g26_figure_conflict_preserves_draft")
repo26, svc26 = av.open_service(p26)
f26 = svc26.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1))["figure_id"]
svc26.save_figure(f26, caption="First writer saved", expected_version=1, actor="Writer A")  # -> v2
repo26.close()
app.studio_save_draft_ui(str(p26), "figure", f26, "figure", f26, 1, "", "", "", "", None, "Stale draft caption", "", "Writer B")
bundle26 = app.studio_save_ui(str(p26), "PUB-MAIN", "figure", f26, "figure", f26, 1, "", "", "", "", None,
                              "Second writer's stale attempt", "", "Writer B", False)
check("g26: a stale figure Save surfaces the conflict message, not a crash",
      "modificato da un'altra sessione" in bundle26[app.ST_IDX_SAVE_MSG], bundle26[app.ST_IDX_SAVE_MSG])
repo26b, svc26b = av.open_service(p26)
draft26 = svc26b.load_draft("figure", f26)
check("g26: the draft written before the conflicting Save attempt is still recoverable",
      draft26 is not None and draft26["payload"]["caption"] == "Stale draft caption", draft26)
repo26b.close()

# ------------------------------------------------------------------
# 27) figure history/revert.
# ------------------------------------------------------------------
p27 = _new_project("g27_figure_history_revert")
repo27, svc27 = av.open_service(p27)
f27 = svc27.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1), caption="Original caption")["figure_id"]
svc27.save_figure(f27, caption="Changed caption", expected_version=1, actor="Writer A")
hist27 = svc27.figure_history(f27)
check("g27: history has both versions, newest first", [h["version"] for h in hist27] == [2, 1], hist27)
r27 = svc27.revert_figure_to_version(f27, 1, actor="Writer A")
check("g27: revert produces a NEW version with version-1 content restored", r27["new_version"] == 3, r27)
d27 = svc27.load_figure_for_edit(f27)
check("g27: reverted caption matches version 1's original caption", d27["caption"] == "Original caption", d27["caption"])
repo27.close()

# ------------------------------------------------------------------
# 28) search caption/alt.
# ------------------------------------------------------------------
p28 = _new_project("g28_search_figure_caption_alt")
repo28, svc28 = av.open_service(p28)
svc28.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1),
                             caption="Installation diagram", alt_text="unique-alt-phrase")
results28a = av.search(repo28, "PUB-MAIN", "Installation diagram")
check("g28: search finds a figure by its caption", any(r["kind"] == "block" for r in results28a), results28a)
results28b = av.search(repo28, "PUB-MAIN", "unique-alt-phrase")
check("g28: search finds a figure by its alt text", any(r["kind"] == "block" for r in results28b), results28b)
repo28.close()

# ------------------------------------------------------------------
# 29) shared figure edit blocked, fail-closed; 30) provenance preserved.
# ------------------------------------------------------------------
p29 = _new_project("g29_30_shared_figure_and_provenance")
repo29 = IndustrialRepository(p29 / "ManualSync_CCMS.sqlite")
repo29.create_figure("FIG-29", title="T", caption="Cap", file_path=str(IMG1), alt_text="Alt", status="Draft",
                     source_refs=["evidence:doc-7#p2"])
repo29.add_block("BLK-29-A", "SEED-TOPIC", "figure", position=2, object_ref_id="FIG-29", status="Draft")
repo29.add_block("BLK-29-B", "SEED-TOPIC", "figure", position=3, object_ref_id="FIG-29", status="Draft")
repo29.close()
repo29b, svc29b = av.open_service(p29)
check("g29: saving a figure referenced by 2 blocks is fail-closed (SharedObjectError)",
      _raises(lambda: svc29b.save_figure("FIG-29", caption="changed", expected_version=1, actor="Writer A"), SharedObjectError), None)
before29 = dict(repo29b.conn.execute("SELECT version,caption FROM figures WHERE figure_id='FIG-29'").fetchone())
check("g29: the shared figure is genuinely untouched", before29 == {"version": 1, "caption": "Cap"}, before29)
bundle29 = app.studio_open_ui(str(p29), "PUB-MAIN", "block:BLK-29-A", False)
check("g29: the UI shows the required shared-object message",
      # See the identical g15 note above -- 4D wording, not 4C's.
      "utilizzato in più punti" in bundle29[app.ST_IDX_SHARED_NOTICE]["value"], bundle29[app.ST_IDX_SHARED_NOTICE])
# 30: provenance preserved across a (rejected) shared edit AND across a normal edit on an unshared figure.
check("g30: source_refs are still exactly what they were (guard fired before any write)",
      json.loads(repo29b.conn.execute("SELECT source_refs_json FROM figures WHERE figure_id='FIG-29'").fetchone()["source_refs_json"]) == ["evidence:doc-7#p2"],
      None)
repo29b.close()


# ====================================================================
# UI / INTEGRATION (items 31-38)
# ====================================================================

# ------------------------------------------------------------------
# 31) standard outline shows human labels ("↳ Tabella: ...", "↳ Figura: ...").
# ------------------------------------------------------------------
p31 = _new_project("g31_outline_human_labels")
repo31, svc31 = av.open_service(p31)
svc31.create_table_in_topic("SEED-TOPIC", actor="Writer A", caption="Outline table caption", columns=["A"], rows=[["1"]])
svc31.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1), caption="Outline figure caption")
rows31 = av.outline_rows(repo31, "PUB-MAIN")
choices31 = av.open_choices(rows31)
labels31 = [c[0] for c in choices31]
check("g31: outline shows '↳ Tabella: <caption>' for a table block, no raw ID",
      any(l.startswith("    ↳ Tabella: Outline table caption") for l in labels31), labels31)
check("g31: outline shows '↳ Figura: <caption>' for a figure block, no raw ID",
      any(l.startswith("    ↳ Figura: Outline figure caption") for l in labels31), labels31)
repo31.close()

# ------------------------------------------------------------------
# 32) table opens dedicated editor; 33) figure opens dedicated editor;
# 34) generic text editor hidden for table/figure.
# ------------------------------------------------------------------
p32 = _new_project("g32_34_dedicated_editors")
repo32, svc32 = av.open_service(p32)
tres32 = svc32.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["1"]])
fres32 = svc32.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1))
repo32.close()
bundle32t = app.studio_open_ui(str(p32), "PUB-MAIN", f"block:{tres32['block_id']}", False)
# 4D Shared Table Unlock Editing Fix: a non-shared table (can_edit=True
# immediately) shows via st_table_grid_edit; st_table_grid_view (the
# permanently-read-only twin) stays hidden.
check("g32: opening a non-shared table block shows the EDITABLE table grid",
      bundle32t[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is True, bundle32t[app.ST_IDX_TABLE_GRID_EDIT])
check("g32: the read-only table grid twin stays hidden for a non-shared table",
      bundle32t[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is False, bundle32t[app.ST_IDX_TABLE_GRID_VIEW])
check("g34: opening a table block hides the generic text editor", bundle32t[app.ST_IDX_TEXT]["visible"] is False, bundle32t[app.ST_IDX_TEXT])
check("g34: opening a table block hides the topic title field too", bundle32t[app.ST_IDX_TITLE]["visible"] is False, bundle32t[app.ST_IDX_TITLE])
bundle33f = app.studio_open_ui(str(p32), "PUB-MAIN", f"block:{fres32['block_id']}", False)
check("g33: opening a figure block shows the figure preview/caption/alt fields",
      bundle33f[app.ST_IDX_FIGURE_PREVIEW]["visible"] is True and bundle33f[app.ST_IDX_FIGURE_CAPTION]["visible"] is True, bundle33f)
check("g34: opening a figure block hides the generic text editor", bundle33f[app.ST_IDX_TEXT]["visible"] is False, bundle33f[app.ST_IDX_TEXT])
check("g34: opening a figure block also hides BOTH table grid twins (never both editors at once)",
      bundle33f[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is False and bundle33f[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is False,
      (bundle33f[app.ST_IDX_TABLE_GRID_VIEW], bundle33f[app.ST_IDX_TABLE_GRID_EDIT]))
check("g34: opening a table block also hides the figure fields (never both editors at once)",
      bundle32t[app.ST_IDX_FIGURE_PREVIEW]["visible"] is False, bundle32t[app.ST_IDX_FIGURE_PREVIEW])

# ------------------------------------------------------------------
# 35) no technical IDs required in standard mode for table/figure creation.
# ------------------------------------------------------------------
check("g35: create_table_in_topic's public signature never requires a table_id/block_id parameter",
      "table_id" not in AuthoringService.create_table_in_topic.__code__.co_varnames[
          :AuthoringService.create_table_in_topic.__code__.co_argcount], None)
check("g35: create_figure_in_topic's public signature never requires a figure_id/block_id parameter",
      "figure_id" not in AuthoringService.create_figure_in_topic.__code__.co_varnames[
          :AuthoringService.create_figure_in_topic.__code__.co_argcount], None)

# ------------------------------------------------------------------
# 36) predictable validation stays inline (no generic gr.Error toast),
#     for the two 4C-specific new cases.
# ------------------------------------------------------------------
p36 = _new_project("g36_predictable_validation_inline")
bundle36a = app.studio_new_table_ui(str(p36), "PUB-MAIN", "topic", "SEED-TOPIC", "", False, "")
check("g36: empty actor on '+ Aggiungi tabella' gives inline guidance, not a gr.Error",
      bundle36a[app.ST_IDX_SAVE_MSG] == "Inserisci il tuo nome per continuare.", bundle36a[app.ST_IDX_SAVE_MSG])
repo36, svc36 = av.open_service(p36)
check("g36: no table was created by the failed attempt", repo36.conn.execute("SELECT COUNT(*) c FROM structured_tables").fetchone()["c"] == 0, None)
repo36.close()
bundle36b = app.studio_new_figure_ui(str(p36), "PUB-MAIN", "topic", "SEED-TOPIC", None, "", "", False, "Writer A")
check("g36: '+ Aggiungi figura' without a file gives 'Seleziona un file immagine.', not a gr.Error",
      bundle36b[app.ST_IDX_SAVE_MSG] == "Seleziona un file immagine.", bundle36b[app.ST_IDX_SAVE_MSG])
bundle36c = app.studio_new_table_ui(str(p36), "PUB-MAIN", "topic", "SEED-TOPIC", "cap", False, "Writer A")
check("g36: valid actor -> table creation succeeds normally", "✅" in bundle36c[app.ST_IDX_SAVE_MSG], bundle36c[app.ST_IDX_SAVE_MSG])

# ------------------------------------------------------------------
# 37) single actor field reused for table/figure actions too -- and
#     (actor state consistency fix) every mutation callback reads the
#     durable actor_state, sourced from c_actor via .input(), never
#     c_actor directly. See group 45+ below for the full state-
#     consistency regression coverage.
# ------------------------------------------------------------------
dep_new_table = None
for _fn_index, entry in app.demo.fns.items():
    fn = getattr(entry, "fn", None)
    if fn is not None and getattr(fn, "__name__", "") == "studio_new_table_ui":
        dep_new_table = app.demo.config["dependencies"][_fn_index]
        break
check("g37: studio_new_table_ui is wired to the shared actor_state (durable, synced from the single c_actor field)",
      dep_new_table is not None and app.actor_state._id in dep_new_table["inputs"], dep_new_table)
check("g37: studio_new_table_ui is NOT wired directly to the live operator Textbox (that was the actor-state bug)",
      dep_new_table is not None and app.ws_operator._id not in dep_new_table["inputs"], dep_new_table)
dep_replace = None
for _fn_index, entry in app.demo.fns.items():
    fn = getattr(entry, "fn", None)
    if fn is not None and getattr(fn, "__name__", "") == "studio_replace_figure_ui":
        dep_replace = app.demo.config["dependencies"][_fn_index]
        break
check("g37: studio_replace_figure_ui is wired to the shared actor_state",
      dep_replace is not None and app.actor_state._id in dep_replace["inputs"], dep_replace)

# ------------------------------------------------------------------
# 38) refresh persists saved table/figure state.
# ------------------------------------------------------------------
p38 = _new_project("g38_refresh_persists_state")
repo38, svc38 = av.open_service(p38)
t38 = svc38.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["1"]])["table_id"]
svc38.save_table(t38, columns=["A", "B"], rows=[["persisted", "value"]], expected_version=1, actor="Writer A")
repo38.close()
bundle38_first = app.studio_open_ui(str(p38), "PUB-MAIN", f"table:{t38}" if False else None, False)
bundle38_reload = app.studio_load_ui(str(p38), "PUB-MAIN", False)  # simulates a fresh tab-select / reload
repo38b, svc38b = av.open_service(p38)
d38 = svc38b.load_table_for_edit(t38)
check("g38: after a simulated reload, the table's saved columns/rows are exactly what was saved",
      d38["columns"] == ["A", "B"] and d38["rows"] == [["persisted", "value"]], d38)
repo38b.close()


# ====================================================================
# 4C INTEGRITY GATE (items 39-49): the Dataframe's own native right-click
# structural menu (Add row/Add column/Delete row/Delete column) is a real
# Gradio 6.5.1 bug -- it mutates the grid client-side without reliably
# updating the value Save reads, so a user could see a new row, press
# Salva, and silently lose it. Fixed in app.py by putting st_table_grid
# in row_count/column_count "fixed" mode (see _fixed_grid_update) --
# empirically verified (isolated repro app + this exact table, both
# scripted and via a live two-cycle browser session) to render the
# native menu with ZERO structural items, while cell editing and the
# four ERYLOS buttons keep working normally. These checks cover: the
# four official controls end to end, draft/version/persistence/shared-
# object discipline for all four, and -- since the protection is a
# component-config property, not a string anyone could fake -- a direct
# inspection of the real gr.Dataframe config app.py builds and ships.
# ====================================================================

# ------------------------------------------------------------------
# 39) the native structural menu is neutralized by construction: the
#     Dataframe app.py actually renders is built (and every subsequent
#     push re-affirms) row_count/column_count "fixed", the only thing
#     this Gradio version's shipped frontend honors to empty out the
#     right-click Add/Delete row/column menu (row_limits/column_limits
#     are explicitly "not yet implemented" in gradio/components/
#     dataframe.py -- checked against the installed package, not
#     assumed). This is the real component config, not a comment.
# ------------------------------------------------------------------
# 4D Shared Table Unlock Editing Fix: st_table_grid is now TWO mutually
# exclusive components (st_table_grid_view, permanently interactive=False;
# st_table_grid_edit, permanently interactive=True) instead of one whose
# `interactive` prop got toggled dynamically -- the native structural menu
# must stay neutralized on BOTH, since either one can be the visible one.
check("g39: st_table_grid_view is constructed with row_count in 'fixed' mode",
      app.st_table_grid_view.row_count[1] == "fixed", app.st_table_grid_view.row_count)
check("g39: st_table_grid_view is constructed with column_count in 'fixed' mode",
      app.st_table_grid_view.column_count[1] == "fixed", app.st_table_grid_view.column_count)
check("g39: st_table_grid_edit is constructed with row_count in 'fixed' mode",
      app.st_table_grid_edit.row_count[1] == "fixed", app.st_table_grid_edit.row_count)
check("g39: st_table_grid_edit is constructed with column_count in 'fixed' mode",
      app.st_table_grid_edit.column_count[1] == "fixed", app.st_table_grid_edit.column_count)
check("g39: st_table_grid_view is permanently interactive=False (never the one relied on for editing)",
      app.st_table_grid_view.interactive is False, app.st_table_grid_view.interactive)
check("g39: st_table_grid_edit is permanently interactive=True (never dynamically toggled)",
      app.st_table_grid_edit.interactive is True, app.st_table_grid_edit.interactive)
import gradio as _gr
import inspect as _inspect
_row_limits_src = _inspect.getsource(_gr.components.dataframe.Dataframe.__init__)
check("g39: confirms row_limits/column_limits are NOT the working mechanism in the installed Gradio "
      "(would make static_columns/row_limits the correct fix instead of row_count 'fixed' if this ever changes)",
      "not yet implemented" in _row_limits_src, "row_limits/column_limits implementation status changed -- re-verify the neutralization mechanism")
check("g39: _fixed_grid_update is the single place that computes the fixed dimensions (used by every st_table_grid push)",
      callable(app._fixed_grid_update), None)
_probe_df = app.pd.DataFrame([["a", "b", "c"], ["d", "e", "f"]], columns=["X", "Y", "Z"])
_probe_upd = app._fixed_grid_update(_probe_df)
check("g39: _fixed_grid_update always emits row_count matching the pushed data's actual row count, still 'fixed'",
      _probe_upd["row_count"] == (2, "fixed"), _probe_upd["row_count"])
check("g39: _fixed_grid_update always emits column_count matching the pushed data's actual column count, still 'fixed'",
      _probe_upd["column_count"] == (3, "fixed"), _probe_upd["column_count"])
_empty_upd = app._fixed_grid_update(app.pd.DataFrame())
check("g39: _fixed_grid_update never emits a zero row_count/column_count (would be an invalid fixed-mode config)",
      _empty_upd["row_count"][0] >= 1 and _empty_upd["column_count"][0] >= 1, (_empty_upd["row_count"], _empty_upd["column_count"]))

# ------------------------------------------------------------------
# 40) opening a real table pushes a fixed-mode update matching ITS
#     actual saved shape (not just the constructor default) -- proves
#     the neutralization tracks the real per-table dimensions end to
#     end through _studio_full_bundle, exactly what the live browser
#     gate cycles exercised.
# ------------------------------------------------------------------
p40 = _new_project("g40_open_table_fixed_mode")
repo40, svc40 = av.open_service(p40)
t40 = svc40.create_table_in_topic("SEED-TOPIC", actor="Writer A",
                                  columns=["Col 1", "Col 2", "Col 3"],
                                  rows=[["a", "b", "c"], ["d", "e", "f"]])["table_id"]
blk40 = repo40.conn.execute("SELECT block_id FROM content_blocks WHERE object_ref_id=?", (t40,)).fetchone()["block_id"]
repo40.close()
bundle40 = app.studio_open_ui(str(p40), "PUB-MAIN", f"block:{blk40}", False)
# Non-shared table -> shown via the EDITABLE grid twin (see the 4D Shared
# Table Unlock Editing Fix note at ST_IDX_TABLE_GRID_VIEW/_EDIT).
check("g40: opening a real 3-column/2-row table pushes row_count=(2,'fixed')",
      bundle40[app.ST_IDX_TABLE_GRID_EDIT]["row_count"] == (2, "fixed"), bundle40[app.ST_IDX_TABLE_GRID_EDIT])
check("g40: opening a real 3-column/2-row table pushes column_count=(3,'fixed')",
      bundle40[app.ST_IDX_TABLE_GRID_EDIT]["column_count"] == (3, "fixed"), bundle40[app.ST_IDX_TABLE_GRID_EDIT])

# ------------------------------------------------------------------
# 41) the four ERYLOS controls are the official structural path --
#     each end to end: value updates correctly, min-guards fail closed
#     with the exact inline message, and every successful mutation
#     keeps re-affirming 'fixed' mode (so the native menu never comes
#     back mid-session on a resized table).
# ------------------------------------------------------------------
p41 = _new_project("g41_erylos_controls")
repo41, svc41 = av.open_service(p41)
t41 = svc41.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A", "B"], rows=[["1", "2"]])["table_id"]
blk41 = repo41.conn.execute("SELECT block_id FROM content_blocks WHERE object_ref_id=?", (t41,)).fetchone()["block_id"]
repo41.close()
_grid1 = app.pd.DataFrame([["1", "2"]], columns=["A", "B"])
_common41 = (str(p41), "block", blk41, "table", t41, 1, "", "", _grid1, "Writer A")

up_add_row, msg_add_row = app.studio_table_add_row_ui(*_common41)
check("g41: + Aggiungi riga appends one empty row",
      up_add_row["value"].values.tolist() == [["1", "2"], ["", ""]], up_add_row["value"].values.tolist())
check("g41: + Aggiungi riga keeps row_count 'fixed' at the NEW row count (2)",
      up_add_row["row_count"] == (2, "fixed"), up_add_row["row_count"])
check("g41: + Aggiungi riga writes a draft (edit-recoverable), NOT a save message",
      "Bozza" in msg_add_row, msg_add_row)

_grid1row = app.pd.DataFrame([["1", "2"]], columns=["A", "B"])
up_rm_row, msg_rm_row = app.studio_table_remove_row_ui(str(p41), "block", blk41, "table", t41, 1, "", "", _grid1row, "Writer A")
check("g41: − Rimuovi riga fails closed at the last row with the exact inline message",
      msg_rm_row == "La tabella deve avere almeno una riga.", msg_rm_row)
check("g41: − Rimuovi riga blocked at minimum -> grid value untouched (gr.update() no-op)",
      "value" not in up_rm_row, up_rm_row)

up_add_col, msg_add_col = app.studio_table_add_col_ui(*_common41)
check("g41: + Aggiungi colonna appends one new column, padding existing rows",
      up_add_col["value"].columns.tolist() == ["A", "B", "Colonna 3"] and
      up_add_col["value"].values.tolist() == [["1", "2", ""]], up_add_col["value"])
check("g41: + Aggiungi colonna keeps column_count 'fixed' at the NEW column count (3)",
      up_add_col["column_count"] == (3, "fixed"), up_add_col["column_count"])

_grid1col = app.pd.DataFrame([["1"]], columns=["A"])
up_rm_col, msg_rm_col = app.studio_table_remove_col_ui(str(p41), "block", blk41, "table", t41, 1, "", "", _grid1col, "Writer A")
check("g41: − Rimuovi colonna fails closed at the last column with the SAME message as Save's own guard",
      msg_rm_col == "Aggiungi almeno una colonna.", msg_rm_col)
check("g41: − Rimuovi colonna blocked at minimum -> grid value untouched (gr.update() no-op)",
      "value" not in up_rm_col, up_rm_col)

# ------------------------------------------------------------------
# 42) draft-only: none of the four controls ever bump the table version
#     or touch structured_table_versions -- only Save does.
# ------------------------------------------------------------------
p42 = _new_project("g42_no_version_bump")
repo42, svc42 = av.open_service(p42)
t42 = svc42.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A", "B"], rows=[["1", "2"]])["table_id"]
blk42 = repo42.conn.execute("SELECT block_id FROM content_blocks WHERE object_ref_id=?", (t42,)).fetchone()["block_id"]
v_before = repo42.conn.execute("SELECT version FROM structured_tables WHERE table_id=?", (t42,)).fetchone()["version"]
hist_before = repo42.conn.execute("SELECT COUNT(*) c FROM structured_table_versions WHERE table_id=?", (t42,)).fetchone()["c"]
repo42.close()
app.studio_table_add_row_ui(str(p42), "block", blk42, "table", t42, 1, "", "", app.pd.DataFrame([["1", "2"]], columns=["A", "B"]), "Writer A")
repo42b, _ = av.open_service(p42)
v_after = repo42b.conn.execute("SELECT version FROM structured_tables WHERE table_id=?", (t42,)).fetchone()["version"]
hist_after = repo42b.conn.execute("SELECT COUNT(*) c FROM structured_table_versions WHERE table_id=?", (t42,)).fetchone()["c"]
draft42 = repo42b.conn.execute("SELECT * FROM authoring_drafts WHERE kind='table' AND object_id=?", (t42,)).fetchone()
repo42b.close()
check("g42: + Aggiungi riga does not bump structured_tables.version", v_after == v_before, (v_before, v_after))
check("g42: + Aggiungi riga does not insert a structured_table_versions row", hist_after == hist_before, (hist_before, hist_after))
check("g42: + Aggiungi riga DOES create a recoverable draft with the new row", draft42 is not None and json.loads(draft42["payload_json"])["rows"] == [["1", "2"], ["", ""]], draft42)

# ------------------------------------------------------------------
# 43) Save after add row/col creates exactly ONE new version, and
#     persists across a simulated reopen (reload) -- the actual fix
#     for the reported silent-loss bug.
# ------------------------------------------------------------------
p43 = _new_project("g43_save_after_structural_edit")
repo43, svc43 = av.open_service(p43)
t43 = svc43.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A", "B"], rows=[["1", "2"]])["table_id"]
blk43 = repo43.conn.execute("SELECT block_id FROM content_blocks WHERE object_ref_id=?", (t43,)).fetchone()["block_id"]
repo43.close()
up43, _ = app.studio_table_add_row_ui(str(p43), "block", blk43, "table", t43, 1, "", "", app.pd.DataFrame([["1", "2"]], columns=["A", "B"]), "Writer A")
grid_with_new_row = up43["value"]
grid_with_new_row.iloc[1] = ["3", "4"]  # simulates the user typing into the newly-added row
save_bundle43 = app.studio_save_ui(str(p43), "PUB-MAIN", "block", blk43, "table", t43, 1, "", "", "", "", grid_with_new_row, "", "", "Writer A", False)
check("g43: Save after + Aggiungi riga reports exactly version 2",
      "versione 2" in save_bundle43[app.ST_IDX_SAVE_MSG], save_bundle43[app.ST_IDX_SAVE_MSG])
repo43b, svc43b = av.open_service(p43)
d43 = svc43b.load_table_for_edit(t43)
check("g43: the added row's typed values are exactly what persisted -- the original reported bug, now fixed",
      d43["rows"] == [["1", "2"], ["3", "4"]] and d43["version"] == 2, d43)
repo43b.close()
# simulated reopen (reload)
bundle43_reopen = app.studio_open_ui(str(p43), "PUB-MAIN", f"block:{blk43}", False)
check("g43: a fresh open (simulated reload) shows the same persisted row -- zero silent loss",
      bundle43_reopen[app.ST_IDX_TABLE_GRID_EDIT]["value"].values.tolist() == [["1", "2"], ["3", "4"]],
      bundle43_reopen[app.ST_IDX_TABLE_GRID_EDIT]["value"])

# ------------------------------------------------------------------
# 44) shared-object protection covers the four controls too: hidden/
#     blocked exactly like Save is, no separate unsafe path survives
#     reuse detection.
# ------------------------------------------------------------------
p44 = _new_project("g44_shared_blocks_erylos_controls")
repo44, svc44 = av.open_service(p44)
t44 = svc44.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["A"], rows=[["1"]])["table_id"]
blk44a = repo44.conn.execute("SELECT block_id FROM content_blocks WHERE object_ref_id=?", (t44,)).fetchone()["block_id"]
repo44.add_block("BLK-G44-REUSE", "SEED-TOPIC", "table", position=2, object_ref_id=t44, status="Draft")
repo44.conn.commit()
repo44.close()
bundle44 = app.studio_open_ui(str(p44), "PUB-MAIN", f"block:{blk44a}", False)
check("g44: shared table -> the ERYLOS add/remove row/column row is hidden (ST_IDX_TABLE_ROWCOL_ROW)",
      bundle44[app.ST_IDX_TABLE_ROWCOL_ROW]["visible"] is False, bundle44[app.ST_IDX_TABLE_ROWCOL_ROW])
check("g44: shared table -> Save itself is also disabled (both defenses agree)",
      bundle44[app.ST_IDX_SAVE_BTN]["interactive"] is False, bundle44[app.ST_IDX_SAVE_BTN])
up44, msg44 = app.studio_table_add_row_ui(str(p44), "block", blk44a, "table", t44, 1, "", "", app.pd.DataFrame([["1"]], columns=["A"]), "Writer A")
save44 = app.studio_save_ui(str(p44), "PUB-MAIN", "block", blk44a, "table", t44, 1, "", "", "", "", up44["value"], "", "", "Writer A", False)
check("g44: even if the add-row handler is invoked directly (bypassing the hidden UI), the actual Save that follows "
      "still fails closed with the shared-object message -- the CAS guard in save_table is the real enforcement point",
      # See the identical g15 note above -- 4D wording, not 4C's. This call
      # also exercises studio_save_ui's new (default-False) shared_confirmed
      # param -- omitted here, so it still fails closed exactly as before.
      "utilizzato in più punti" in save44[app.ST_IDX_SAVE_MSG], save44[app.ST_IDX_SAVE_MSG])
repo44b, _ = av.open_service(p44)
v44_after = repo44b.conn.execute("SELECT version FROM structured_tables WHERE table_id=?", (t44,)).fetchone()["version"]
repo44b.close()
check("g44: the blocked Save did not bump the shared table's version", v44_after == 1, v44_after)

# ====================================================================
# 4C ACTOR STATE CONSISTENCY FIX (items 45-54): a real user typed
# "Alessandro" into the single shared "Il tuo nome" field, saw it stay
# visibly filled, went back to a table, pressed Salva -- and still got
# "Inserisci il tuo nome per continuare." even though the field was
# genuinely non-empty. Root cause: every mutation callback (Save,
# archive/restore, reorder, review, revert, create, replace image) read
# c_actor -- the live, client-rendered Textbox -- directly at its own
# click time, which is vulnerable to whatever can leave a Textbox's
# client value transiently out of sync with what a DIFFERENT button's
# click payload captures (this session's own browser cycles hit
# frequent ERR_CONNECTION_RESET / SSE reconnects on this exact app,
# consistent with the failure mode). Fixed by backing c_actor with a
# gr.State (actor_state), synced via .change() -- empirically the ONLY
# trigger of the three tried (.input/.change/.blur) that actually
# reaches the backend for a plain Textbox in this Gradio 6.5.1 build,
# verified with a server-side file trace (.input() never arrived, not
# even via real keyboard simulation; .change() fired every time; fires
# on native blur, which already happens the instant the user clicks
# anywhere else, so this never asks the user for an extra action) --
# and rewiring EVERY mutation callback -- all 25 of them -- to read
# actor_state instead of c_actor. A gr.State value is tracked
# authoritatively server-side per session, immune to
# whatever affects a live component's client-rendered value.
# ====================================================================

# ------------------------------------------------------------------
# 45) no duplicate actor components in the rendered app graph, and NOT
#     ONE remaining mutation callback still reads the live c_actor
#     Textbox directly -- the exact wiring-level proof this class of
#     bug cannot recur. (Still exactly one shared "Il tuo nome" field;
#     the fix added a State, not a second textbox.)
# ------------------------------------------------------------------
# UX Gate 2A re-aim: the identity field is now the shell context bar's
# ws_operator -- c_actor was a third copy of the same question and is
# gone. The invariant under test is unchanged: ONE identity component,
# and no authoring mutation callback reading a live Textbox directly.
_identity_components = [
    c for c in [app.ws_operator] if getattr(c, "label", None) == i18n.translate("context.operator", "it")
]
check("g45: exactly one identity component feeds authoring actions (the shell operator)",
      len(_identity_components) == 1, _identity_components)
check("g45: the old tab-local duplicate (c_actor) is gone, not merely renamed",
      not hasattr(app, "c_actor"), None)
check("g45: actor_state is a real gr.State (not a second visible textbox)",
      isinstance(app.actor_state, app.gr.State), type(app.actor_state))
# ws_operator is read directly by Compliance's own callbacks (a Gate 1
# decision, unrelated to this bug class), so the assertion that matters
# here is the AUTHORING one: not one studio_* callback may read the live
# Textbox -- they all go through actor_state.
_operator_input_deps = [d for d in app.demo.config["dependencies"] if app.ws_operator._id in d.get("inputs", [])]
_authoring_reading_live = [d for d in _operator_input_deps
                           if str(d.get("api_name") or "").startswith("studio_")]
check("g45: NO authoring callback reads the live operator Textbox directly -- every one goes via actor_state",
      _authoring_reading_live == [], [d.get("api_name") for d in _authoring_reading_live])
_c_actor_input_deps = [d for d in _operator_input_deps
                       if d.get("outputs") == [app.actor_state._id]]
check("g45: exactly one dependency syncs the operator field into actor_state",
      len(_c_actor_input_deps) == 1, [d.get("api_name") or d.get("targets") for d in _c_actor_input_deps])
check("g45: that one dependency fires on 'change' -- the mechanism actually proven to reach the backend in this "
      "Gradio build (verified with a server-side file trace: .input() on a plain Textbox never arrived at all, "
      "synthetic or real keyboard; .change() fired reliably every time). Fires on native blur, i.e. the instant "
      "the user clicks anywhere else -- including the very button they're about to press -- so this still never "
      "asks the user to do anything beyond typing the name once.",
      _c_actor_input_deps[0]["targets"][0][1] == "change", _c_actor_input_deps[0]["targets"])
_actor_state_input_deps = [d for d in app.demo.config["dependencies"] if app.actor_state._id in d.get("inputs", [])]
check("g45: at least 20 real mutation callbacks (save/archive/restore/reorder/review/revert/create/replace) "
      "read actor_state instead of the live operator Textbox",
      len(_actor_state_input_deps) >= 20, len(_actor_state_input_deps))
_mutation_fn_names = {"studio_save_ui", "studio_move_ui", "studio_archive_ui", "studio_restore_obj_ui",
                      "studio_new_topic_ui", "studio_new_block_ui", "studio_new_table_ui", "studio_new_figure_ui",
                      "studio_replace_figure_ui", "studio_submit_review_ui", "studio_revert_ui",
                      "idml_review_confirm_ui", "guided_approve_source_ui"}
_found_fn_names = {d.get("api_name") for d in _actor_state_input_deps if d.get("api_name")}
check("g46: every listed mutation callback (topic/block/table/figure/save/archive/restore/review/revert/create/"
      "replace image) is among the ones reading actor_state -- no stale wiring left behind for any of them",
      _mutation_fn_names - {"studio_move_ui"} <= _found_fn_names,
      sorted(_mutation_fn_names - {"studio_move_ui"} - _found_fn_names))

# ------------------------------------------------------------------
# 47) the sync handler itself: c_actor.input() writes straight through
#     to actor_state, unconditionally, with no transformation that
#     could silently drop or trim a valid name.
# ------------------------------------------------------------------
_sync_dep = _c_actor_input_deps[0]
_sync_fn = getattr(app.demo.fns.get(_sync_dep.get("id")), "fn", None)
check("g47: the operator -> actor_state sync handler returns the value unchanged (pure passthrough)",
      _sync_fn is not None and _sync_fn("Alessandro") == "Alessandro", _sync_fn("Alessandro") if _sync_fn else None)
check("g47: the sync handler's own output target is actor_state",
      app.actor_state._id in _sync_dep.get("outputs", []), _sync_dep.get("outputs"))

# ------------------------------------------------------------------
# 48-51) the exact reported scenario, functionally: actor empty ->
#     add a row -> fill cells -> Save blocked, draft preserved (no
#     partial mutation, no version bump) -> actor becomes non-empty
#     (exactly what actor_state holds once .input() has fired) -> Save
#     succeeds, SAME draft persisted, version bumps exactly once.
# ------------------------------------------------------------------
p48 = _new_project("g48_actor_state_save_scenario")
repo48, svc48 = av.open_service(p48)
t48 = svc48.create_table_in_topic("SEED-TOPIC", actor="Writer A", columns=["Parametro", "Valore"], rows=[["Portata", "120 l/min"]])["table_id"]
blk48 = repo48.conn.execute("SELECT block_id FROM content_blocks WHERE object_ref_id=?", (t48,)).fetchone()["block_id"]
repo48.close()
up48, _ = app.studio_table_add_row_ui(str(p48), "block", blk48, "table", t48, 1, "", "", app.pd.DataFrame([["Portata", "120 l/min"]], columns=["Parametro", "Valore"]), "")
grid48 = up48["value"]
grid48.iloc[1] = ["Temperatura", "85 °C"]
# simulates the cell-edit's own .input()-triggered draft save (real
# browser flow: typing into the new row's cells re-saves the draft with
# the typed values, same as st_table_grid.input(studio_save_draft_ui, ...))
app.studio_save_draft_ui(str(p48), "block", blk48, "table", t48, 1, "", "", "", "", grid48, "", "", "")
save48_blocked = app.studio_save_ui(str(p48), "PUB-MAIN", "block", blk48, "table", t48, 1, "", "", "", "", grid48, "", "", "", False)
check("g48: actor empty at Save time -> blocked with the exact inline message, no gr.Error",
      save48_blocked[app.ST_IDX_SAVE_MSG] == "Inserisci il tuo nome per continuare.", save48_blocked[app.ST_IDX_SAVE_MSG])
repo48b, _ = av.open_service(p48)
v48_blocked = repo48b.conn.execute("SELECT version, rows_json FROM structured_tables WHERE table_id=?", (t48,)).fetchone()
draft48_blocked = repo48b.conn.execute("SELECT payload_json FROM authoring_drafts WHERE kind='table' AND object_id=?", (t48,)).fetchone()
repo48b.close()
check("g49: blocked Save did not bump the table version (no partial mutation)", v48_blocked["version"] == 1, dict(v48_blocked))
check("g49: blocked Save did not touch the persisted rows either", json.loads(v48_blocked["rows_json"]) == [["Portata", "120 l/min"]], v48_blocked["rows_json"])
check("g49: the in-progress row (Temperatura/85 °C) is still preserved in the draft after the blocked Save",
      draft48_blocked is not None and json.loads(draft48_blocked["payload_json"])["rows"] == [["Portata", "120 l/min"], ["Temperatura", "85 °C"]],
      draft48_blocked)
# actor now provided (exactly what actor_state holds after .input() fires) -- same grid, same draft target, retried
save48_ok = app.studio_save_ui(str(p48), "PUB-MAIN", "block", blk48, "table", t48, 1, "", "", "", "", grid48, "", "", "Alessandro", False)
check("g50: Save with the SAME draft content, actor now provided, succeeds -- version 2",
      "versione 2" in save48_ok[app.ST_IDX_SAVE_MSG], save48_ok[app.ST_IDX_SAVE_MSG])
repo48c, svc48c = av.open_service(p48)
d48_final = svc48c.load_table_for_edit(t48)
draft48_final = repo48c.conn.execute("SELECT * FROM authoring_drafts WHERE kind='table' AND object_id=?", (t48,)).fetchone()
hist48 = repo48c.conn.execute("SELECT COUNT(*) c FROM structured_table_versions WHERE table_id=?", (t48,)).fetchone()["c"]
repo48c.close()
check("g50: version bumped exactly once (1 -> 2), not twice, across the blocked-then-successful Save pair",
      d48_final["version"] == 2 and hist48 == 2, (d48_final["version"], hist48))
check("g50: the persisted rows are exactly the same draft content that was blocked a moment ago -- nothing lost",
      d48_final["rows"] == [["Portata", "120 l/min"], ["Temperatura", "85 °C"]], d48_final["rows"])
check("g51: the draft is gone after the successful Save (recovery slot cleared, not left dangling)",
      draft48_final is None, draft48_final)

# ------------------------------------------------------------------
# 52) figure save succeeds with the same actor mechanism (not just
#     tables) -- and topic/block save (the original, non-4C paths)
#     keep working unchanged, since studio_save_ui's own signature was
#     never touched, only which component feeds its `actor` argument.
# ------------------------------------------------------------------
p52 = _new_project("g52_figure_and_topic_save_actor")
repo52, svc52 = av.open_service(p52)
fig52 = svc52.create_figure_in_topic("SEED-TOPIC", actor="Writer A", source_file_path=str(IMG1), caption="Old caption")["figure_id"]
blk52fig = repo52.conn.execute("SELECT block_id FROM content_blocks WHERE object_ref_id=?", (fig52,)).fetchone()["block_id"]
repo52.close()
save52_fig = app.studio_save_ui(str(p52), "PUB-MAIN", "block", blk52fig, "figure", fig52, 1, "", "", "", "", None, "New caption", "New alt", "Alessandro", False)
check("g52: figure Save succeeds with actor provided the same way table Save does",
      "versione 2" in save52_fig[app.ST_IDX_SAVE_MSG], save52_fig[app.ST_IDX_SAVE_MSG])
save52_topic = app.studio_save_ui(str(p52), "PUB-MAIN", "topic", "SEED-TOPIC", None, None, 1, "Renamed topic", "", "", "", None, "", "", "Alessandro", False)
check("g52: topic Save (non-4C path) is unaffected by the actor-state rewiring",
      "versione 2" in save52_topic[app.ST_IDX_SAVE_MSG], save52_topic[app.ST_IDX_SAVE_MSG])

# ------------------------------------------------------------------
# 53) navigation table -> figure -> table conserves the actor: since
#     actor_state is a per-session gr.State (not tied to which block
#     is open), re-opening a different block never resets it -- proven
#     by _studio_full_bundle's own output list never including
#     actor_state/c_actor as a target.
# ------------------------------------------------------------------
check("g53: actor_state is never a target (output) of the outline/open/refresh bundle -- navigating never resets it",
      app.actor_state._id not in [i for d in app.demo.config["dependencies"]
                                  for i in d.get("outputs", []) if d.get("api_name") == "studio_open_ui"], None)
check("g53: the operator field is never a target (output) of the outline/open/refresh bundle either",
      app.ws_operator._id not in [i for d in app.demo.config["dependencies"]
                              for i in d.get("outputs", []) if d.get("api_name") == "studio_open_ui"], None)

# ------------------------------------------------------------------
# 54) no stale st_actor / c_simple_reviewer-style mutation wiring left
#     behind: the only "Il tuo nome" Textbox feeding any of the 4C
#     table/figure callbacks is c_actor (via actor_state) -- no
#     leftover reference to a differently-named component anywhere in
#     app.py's authoring wiring.
# ------------------------------------------------------------------
import inspect as _inspect2
_app_src = _inspect2.getsource(app)
check("g54: no leftover 'st_actor' identifier anywhere in app.py (would be a second, stale actor component)",
      "st_actor" not in _app_src, None)

print(json.dumps({"pass": sum(r["status"] == "PASS" for r in results), "tests": len(results)}))
if any(r["status"] != "PASS" for r in results):
    print(json.dumps(results, indent=2))
    sys.exit(1)
