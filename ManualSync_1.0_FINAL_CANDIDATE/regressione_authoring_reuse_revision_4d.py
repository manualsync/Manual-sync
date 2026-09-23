"""Milestone 4D regression suite: Reuse-Aware Authoring & Revision
Intelligence.

Tests authoring_service.py (get_reuse_impact, confirm_shared on save/
revert, fork_table_local/fork_figure_local, get_*_version), authoring_
view.py (diff_text/diff_table/diff_figure, reuse_impact_summary, the
outline's reuse-count suffix), and the real app.py `studio_*_ui` callbacks
for the shared-object decision UX (Aggiorna ovunque / Crea una copia
locale) and revision compare -- calling the actual functions the product
wires to Gradio, not a mock, matching the discipline established for the
4B.1/4B.2/4C suites.

Covers 45 items: reuse impact primitive, shared-object save/revert
(fail-closed and confirmed), fork atomicity/TOCTOU/audit trail for both
tables and figures, text/table/figure diff, version-history accessors,
and the outline reuse indicator -- backend and the real UI callbacks
together.

Backend/integration only -- no browser (see the separate browser-
acceptance cycles run for this milestone). Fixtures are entirely
synthetic, including a tiny generated PNG for every figure test.
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
import pandas as pd

TMP = Path(tempfile.mkdtemp(prefix="manualsync_authoring_reuse_revision_4d_"))
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
        repo.create_topic("T1", "Topic One", "legacy body", status="Draft")
        repo.create_topic("T2", "Topic Two", "legacy body", status="Draft")
        repo.create_publication("PUB-MAIN", "Synthetic Manual", ["T1", "T2"])
    finally:
        repo.close()
    return p


def _make_test_image(name: str) -> Path:
    from PIL import Image
    path = TMP / name
    Image.new("RGB", (12, 8), color=(90, 140, 200)).save(path)
    return path


IMG1 = _make_test_image("fixture_image_1.png")


def _share_table(repo: IndustrialRepository, svc: AuthoringService):
    res = svc.create_table_in_topic("T1", actor="Writer A", caption="Shared table",
                                    columns=["A", "B"], rows=[["1", "2"]])
    table_id, block1 = res["table_id"], res["block_id"]
    block2 = f"BLK-SHARE-{table_id[-6:]}"
    repo.add_block(block2, "T2", "table", position=1, object_ref_id=table_id, status="Draft")
    return table_id, block1, block2


def _share_figure(repo: IndustrialRepository, svc: AuthoringService):
    res = svc.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Shared figure")
    figure_id, block1 = res["figure_id"], res["block_id"]
    block2 = f"BLK-FSHARE-{figure_id[-6:]}"
    repo.add_block(block2, "T2", "figure", position=1, object_ref_id=figure_id, status="Draft")
    return figure_id, block1, block2


# ====================================================================
# get_reuse_impact -- the block-level-precise primitive (items 1-6)
# ====================================================================
p1 = _new_project("d01_06_reuse_impact")
repo1, svc1 = av.open_service(p1)
t1, b1a, b1b = _share_table(repo1, svc1)
impact1 = svc1.get_reuse_impact("table", t1)
check("d1: get_reuse_impact reference_count == 2 for a table used by 2 blocks", impact1["reference_count"] == 2, impact1)
check("d2: get_reuse_impact is_shared == True", impact1["is_shared"] is True, impact1)
check("d3: get_reuse_impact lists both topics, deduplicated", sorted(t["topic_id"] for t in impact1["topics"]) == ["T1", "T2"], impact1["topics"])
check("d4: get_reuse_impact lists the 1 shared publication (both topics belong to PUB-MAIN)",
      len(impact1["publications"]) == 1 and impact1["publications"][0]["publication_id"] == "PUB-MAIN", impact1["publications"])
check("d5: get_reuse_impact archived_reference_count is 0 when no referencing block is archived",
      impact1["archived_reference_count"] == 0, impact1)
svc1.archive_block(b1b, actor="Writer A")
impact1b = svc1.get_reuse_impact("table", t1)
check("d6: get_reuse_impact archived_reference_count reflects an archived referencing block",
      impact1b["archived_reference_count"] == 1 and impact1b["reference_count"] == 2, impact1b)
repo1.close()

# A non-shared object has reference_count 1, is_shared False.
p1c = _new_project("d_nonshared_impact")
repo1c, svc1c = av.open_service(p1c)
res1c = svc1c.create_table_in_topic("T1", actor="Writer A", columns=["A"], rows=[["x"]])
impact1c = svc1c.get_reuse_impact("table", res1c["table_id"])
check("d7: get_reuse_impact reference_count == 1 and is_shared == False for a non-shared table",
      impact1c["reference_count"] == 1 and impact1c["is_shared"] is False, impact1c)
repo1c.close()

# ====================================================================
# save_table: fail-closed vs. confirm_shared=True ("Aggiorna ovunque")
# (items 8-13)
# ====================================================================
p2 = _new_project("d08_13_table_shared_save")
repo2, svc2 = av.open_service(p2)
t2, b2a, b2b = _share_table(repo2, svc2)
check("d8: save_table without confirm_shared is fail-closed (SharedObjectError)",
      _raises(lambda: svc2.save_table(t2, caption="x", expected_version=1, actor="Writer A"), SharedObjectError), None)
live2 = repo2.conn.execute("SELECT version,caption FROM structured_tables WHERE table_id=?", (t2,)).fetchone()
check("d9: the shared table is genuinely untouched after the fail-closed attempt", live2["version"] == 1, dict(live2))
token2 = svc2.get_reuse_impact("table", t2)["impact_token"]
r2 = svc2.save_table(t2, caption="Edited everywhere", expected_version=1, actor="Writer A",
                     confirm_shared=True, expected_impact_token=token2)
check("d10: save_table WITH confirm_shared=True + a fresh impact_token succeeds and bumps the version",
      r2["version"] == 2, r2)
audit2 = json.loads(repo2.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='table' AND object_id=? AND action='AUTHORING_SAVE' ORDER BY event_id DESC LIMIT 1",
    (t2,)).fetchone()["detail_json"])
check("d11: the audit trail records decision='shared_update' and the real impact_count",
      audit2.get("decision") == "shared_update" and audit2.get("impact_count") == 2, audit2)
after2 = repo2.conn.execute("SELECT caption FROM structured_tables WHERE table_id=?", (t2,)).fetchone()
check("d12: 'Aggiorna ovunque' really changed the ONE shared object both blocks point at",
      after2["caption"] == "Edited everywhere", dict(after2))
b2a_ref = repo2.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b2a,)).fetchone()["object_ref_id"]
b2b_ref = repo2.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b2b,)).fetchone()["object_ref_id"]
check("d13: both blocks still reference the SAME table_id after a shared update (no accidental fork)",
      b2a_ref == t2 and b2b_ref == t2, (b2a_ref, b2b_ref))
repo2.close()

# ====================================================================
# revert_table_to_version: fail-closed vs. confirm_shared (items 14-15)
# ====================================================================
p3 = _new_project("d14_15_table_shared_revert")
repo3, svc3 = av.open_service(p3)
t3, b3a, b3b = _share_table(repo3, svc3)
check("d14: revert_table_to_version without confirm_shared is fail-closed on a shared table",
      _raises(lambda: svc3.revert_table_to_version(t3, 1, actor="Writer A"), SharedObjectError), None)
token3a = svc3.get_reuse_impact("table", t3)["impact_token"]
svc3.save_table(t3, caption="v2", expected_version=1, actor="Writer A",
                confirm_shared=True, expected_impact_token=token3a)
token3b = svc3.get_reuse_impact("table", t3)["impact_token"]
r3 = svc3.revert_table_to_version(t3, 1, actor="Writer A",
                                  confirm_shared=True, expected_impact_token=token3b)
audit3 = json.loads(repo3.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='table' AND object_id=? AND action='REVERT_TO_VERSION' ORDER BY event_id DESC LIMIT 1",
    (t3,)).fetchone()["detail_json"])
check("d15: revert WITH confirm_shared=True succeeds and records decision='shared_update' in the audit trail",
      r3["status"] in ("Draft",) and audit3.get("decision") == "shared_update", (r3, audit3))
repo3.close()

# ====================================================================
# fork_table_local: atomicity, isolation, TOCTOU, audit trail (16-24)
# ====================================================================
p4 = _new_project("d16_24_table_fork")
repo4, svc4 = av.open_service(p4)
t4, b4a, b4b = _share_table(repo4, svc4)
fork4 = svc4.fork_table_local(t4, block_id=b4b, expected_version=1, actor="Writer A")
new_t4 = fork4["table_id"]
check("d16: fork_table_local returns a NEW table_id, different from the original", new_t4 != t4, fork4)
b4a_ref = repo4.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b4a,)).fetchone()["object_ref_id"]
b4b_ref = repo4.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b4b,)).fetchone()["object_ref_id"]
check("d17: the OTHER block (not chosen for the fork) still references the ORIGINAL table -- untouched",
      b4a_ref == t4, b4a_ref)
check("d18: the chosen block now references the NEW forked table", b4b_ref == new_t4, b4b_ref)
impact4 = svc4.get_reuse_impact("table", t4)
check("d19: the original table's reference_count drops to 1 (no longer shared) after the fork",
      impact4["reference_count"] == 1 and impact4["is_shared"] is False, impact4)
new_row4 = repo4.conn.execute("SELECT title,caption,columns_json,rows_json FROM structured_tables WHERE table_id=?", (new_t4,)).fetchone()
orig_row4 = repo4.conn.execute("SELECT title,caption,columns_json,rows_json FROM structured_tables WHERE table_id=?", (t4,)).fetchone()
check("d20: the fork starts as a content-identical copy of the shared object at fork time",
      dict(new_row4) == dict(orig_row4), (dict(new_row4), dict(orig_row4)))
check("d21: fork_table_local fails closed (VersionConflictError) on a stale expected_version",
      _raises(lambda: svc4.fork_table_local(new_t4, block_id=b4b, expected_version=99, actor="Writer A"), VersionConflictError), None)
try:
    svc4.fork_table_local(new_t4, block_id=b4b, expected_version=99, actor="Writer A")
except VersionConflictError as e:
    check("d22: the TOCTOU fork message is the exact required Italian text",
          "L'elemento è cambiato dopo l'anteprima dell'impatto" in str(e), str(e))
audit_new4 = [json.loads(r["detail_json"]) for r in repo4.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='table' AND object_id=? AND action='AUTHORING_FORK_CREATED'", (new_t4,))]
audit_orig4 = [json.loads(r["detail_json"]) for r in repo4.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='table' AND object_id=? AND action='AUTHORING_FORKED'", (t4,))]
audit_blk4 = [json.loads(r["detail_json"]) for r in repo4.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='block' AND object_id=? AND action='REWIRED_TO_LOCAL_FORK'", (b4b,))]
check("d23: the fork's full audit/provenance trail is present (AUTHORING_FORK_CREATED + AUTHORING_FORKED + REWIRED_TO_LOCAL_FORK)",
      len(audit_new4) == 1 and audit_new4[0]["forked_from_object_id"] == t4 and
      len(audit_orig4) == 1 and audit_orig4[0]["new_object_id"] == new_t4 and
      len(audit_blk4) == 1 and audit_blk4[0]["to_object_id"] == new_t4, (audit_new4, audit_orig4, audit_blk4))
r4save = svc4.save_table(new_t4, caption="local only", expected_version=1, actor="Writer A")
check("d24: the fork is independently editable afterward -- no SharedObjectError", r4save["version"] == 2, r4save)
repo4.close()

# ====================================================================
# Figures: impact/save/fork mirror (items 25-31)
# ====================================================================
p5 = _new_project("d25_31_figure_reuse")
repo5, svc5 = av.open_service(p5)
f5, b5a, b5b = _share_figure(repo5, svc5)
impact5 = svc5.get_reuse_impact("figure", f5)
check("d25: get_reuse_impact works identically for a shared figure (reference_count == 2)", impact5["reference_count"] == 2, impact5)
check("d26: save_figure without confirm_shared is fail-closed on a shared figure",
      _raises(lambda: svc5.save_figure(f5, caption="x", expected_version=1, actor="Writer A"), SharedObjectError), None)
ftoken5 = svc5.get_reuse_impact("figure", f5)["impact_token"]
r5 = svc5.save_figure(f5, caption="Edited everywhere", expected_version=1, actor="Writer A",
                      confirm_shared=True, expected_impact_token=ftoken5)
check("d27: save_figure WITH confirm_shared=True succeeds", r5["version"] == 2, r5)
orig_path5 = repo5.conn.execute("SELECT file_path FROM figures WHERE figure_id=?", (f5,)).fetchone()["file_path"]
fork5 = svc5.fork_figure_local(f5, block_id=b5b, expected_version=2, actor="Writer A")
new_f5 = fork5["figure_id"]
fork_path5 = repo5.conn.execute("SELECT file_path FROM figures WHERE figure_id=?", (new_f5,)).fetchone()["file_path"]
check("d28: fork_figure_local reuses the SAME file_path as the original -- no byte copy", fork_path5 == orig_path5, (fork_path5, orig_path5))
b5a_ref = repo5.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b5a,)).fetchone()["object_ref_id"]
check("d29: the other block keeps pointing at the original figure after the fork", b5a_ref == f5, b5a_ref)
check("d30: fork_figure_local fails closed on a stale expected_version",
      _raises(lambda: svc5.fork_figure_local(new_f5, block_id=b5b, expected_version=99, actor="Writer A"), VersionConflictError), None)
check("d31: the forked figure's own reference_count is 1 (independently editable)",
      svc5.get_reuse_impact("figure", new_f5)["reference_count"] == 1, None)
repo5.close()

# ====================================================================
# Diff primitives (items 32-40)
# ====================================================================
d32 = av.diff_text("il gatto nero dorme", "il gatto bianco dorme profondamente")
check("d32: diff_text detects word-level additions and reports a non-empty Italian summary",
      d32["changed"] is True and d32["words_added"] > 0 and d32["summary"], d32)
d33 = av.diff_text("stesso testo identico", "stesso testo identico")
check("d33: diff_text reports no change for identical text", d33["changed"] is False, d33)

old_t = {"title": "T", "caption": "C", "columns": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]}
new_t_colsrows = {"title": "T", "caption": "C", "columns": ["A", "B", "C"], "rows": [["1", "2", "9"], ["3", "4", "9"], ["5", "6", "7"]]}
d34 = av.diff_table(old_t, new_t_colsrows)
check("d34: diff_table detects an added column", d34["columns_added"] == 1, d34)
check("d35: diff_table detects an added row", d34["rows_added"] == 1, d34)
check("d36: diff_table counts the individual modified cells on aligned rows", d34["cells_changed"] >= 2, d34)
new_t_removed = {"title": "T", "caption": "C", "columns": ["A"], "rows": [["1"]]}
d37 = av.diff_table(old_t, new_t_removed)
check("d37: diff_table detects a removed column and a removed row", d37["columns_removed"] == 1 and d37["rows_removed"] == 1, d37)
d38 = av.diff_table(old_t, dict(old_t))
check("d38: diff_table reports no change for an identical table", d38["changed"] is False, d38)

old_f = {"title": "T", "caption": "C", "file_path": "/a/img1.png", "alt_text": "alt"}
new_f = {"title": "T", "caption": "C2", "file_path": "/a/img2.png", "alt_text": "alt"}
d39 = av.diff_figure(old_f, new_f)
check("d39: diff_figure detects the image replacement and the caption change ('Immagine sostituita')",
      d39["image_changed"] is True and d39["caption_changed"] is True and "Immagine sostituita" in d39["summary"], d39)
d40 = av.diff_figure(old_f, dict(old_f))
check("d40: diff_figure reports no change for an identical figure", d40["changed"] is False, d40)

# ====================================================================
# Version-history accessors used to compute the diffs above (items 41-44)
# ====================================================================
p6 = _new_project("d41_44_version_accessors")
repo6, svc6 = av.open_service(p6)
t6 = svc6.create_table_in_topic("T1", actor="Writer A", caption="v1", columns=["A"], rows=[["1"]])["table_id"]
svc6.save_table(t6, caption="v2", columns=["A"], rows=[["2"]], expected_version=1, actor="Writer A")
v6_1 = svc6.get_table_version(t6, 1)
check("d41: get_table_version returns the EXACT historical content of version 1, not the live row",
      v6_1["caption"] == "v1" and v6_1["rows"] == [["1"]], v6_1)

f6 = svc6.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="fig-v1")["figure_id"]
svc6.save_figure(f6, caption="fig-v2", expected_version=1, actor="Writer A")
v6f_1 = svc6.get_figure_version(f6, 1)
check("d42: get_figure_version returns the exact historical caption of version 1", v6f_1["caption"] == "fig-v1", v6f_1)

blk6 = svc6.create_block_in_topic("T2", "paragraph", actor="Writer A", text="v1 text")["block_id"]
svc6.save_block(blk6, text="v2 text", expected_version=1, actor="Writer A")
v6b_1 = svc6.get_block_version(blk6, 1)
check("d43: get_block_version returns the exact historical text of version 1", v6b_1["text"] == "v1 text", v6b_1)

svc6.save_topic("T1", title="Topic One Renamed", expected_version=1, actor="Writer A")
v6t_1 = svc6.get_topic_version("T1", 1)
check("d44: get_topic_version returns the exact historical title of version 1", v6t_1["title"] == "Topic One", v6t_1)
repo6.close()

# ====================================================================
# Outline reuse indicator (item 45)
# ====================================================================
p7 = _new_project("d45_outline_reuse_suffix")
repo7, svc7 = av.open_service(p7)
t7, b7a, b7b = _share_table(repo7, svc7)
rows7 = av.outline_rows(repo7, "PUB-MAIN", include_archived=False)
b7a_row = next(r for r in rows7 if r["id"] == b7a)
check("d45: the outline appends '· riutilizzata N volte' to a shared table's preview, absent for a non-shared one",
      "riutilizzata 2 volte" in b7a_row["preview"], b7a_row["preview"])
# d45c: browser-stabilization bug -- outline_rows() used to display the
# WRAPPING BLOCK's own structural version (content_blocks.version) for a
# table/figure row instead of the OBJECT's real content version
# (structured_tables.version), which silently diverges the moment a block
# is rewired without its content changing -- fork_table_local() bumps the
# block's version on rewire while the freshly forked table itself starts
# at content version 1, so the outline showed e.g. "Versione 2" for a
# table that had never actually been edited. The outline's "Versione"
# column must always mean the SAME thing as table_detail/figure_detail's
# own version (what the editor header and revert operate on).
fork7 = svc7.fork_table_local(t7, block_id=b7b, expected_version=1, actor="Writer A")
rows7_after_fork = av.outline_rows(repo7, "PUB-MAIN", include_archived=False)
b7b_row = next(r for r in rows7_after_fork if r["id"] == b7b)
check("d45b: after a fork, the outline shows the FORKED TABLE's real content version (1), "
      "not the rewired block's own bumped structural version (2)",
      b7b_row["version"] == 1, b7b_row)
b7a_row_after = next(r for r in rows7_after_fork if r["id"] == b7a)
check("d45c: the untouched original block's table row still shows the original table's real content version (1)",
      b7a_row_after["version"] == 1, b7a_row_after)

res7_solo = svc7.create_table_in_topic("T1", actor="Writer A", caption="Solo table", columns=["A"], rows=[["1"]])
rows7b = av.outline_rows(repo7, "PUB-MAIN", include_archived=False)
solo_row = next(r for r in rows7b if r["id"] == res7_solo["block_id"])
check("d45b: a non-shared table's outline preview carries NO reuse suffix", "riutilizzata" not in solo_row["preview"], solo_row["preview"])
repo7.close()

# ====================================================================
# Real app.py UI callbacks -- the shared-object decision panel, fork
# button, and revision compare (integration-level, items already backed
# by the service-level checks above; verifying the WIRING here).
# ====================================================================
p8 = _new_project("d_ui_shared_decision")
repo8, svc8 = av.open_service(p8)
t8, b8a, b8b = _share_table(repo8, svc8)
repo8.close()

bundle8 = app.studio_open_ui(str(p8), "PUB-MAIN", f"block:{b8a}", False)
check("UI-1: opening a shared table shows the shared notice and BOTH decision buttons",
      bundle8[app.ST_IDX_SHARED_NOTICE]["visible"] and bundle8[app.ST_IDX_UPDATE_EVERYWHERE_BTN]["visible"] and
      bundle8[app.ST_IDX_FORK_LOCAL_BTN]["visible"], bundle8[app.ST_IDX_SHARED_NOTICE])
check("UI-2: the reuse impact panel lists the usage count", "2 punti" in bundle8[app.ST_IDX_REUSE_IMPACT]["value"], bundle8[app.ST_IDX_REUSE_IMPACT])
# 4D Shared Table Unlock Editing Fix: two mutually exclusive grid
# components (never a dynamically-toggled `interactive` on one shared
# component) -- before a decision, only the permanently-read-only
# st_table_grid_view is visible; st_table_grid_edit stays hidden.
check("UI-3: before a decision, the READ-ONLY table grid twin is shown (not the editable one)",
      bundle8[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is True and bundle8[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is False,
      (bundle8[app.ST_IDX_TABLE_GRID_VIEW], bundle8[app.ST_IDX_TABLE_GRID_EDIT]))

confirmed8 = app.studio_confirm_shared_update_ui(str(p8), "PUB-MAIN", "block", b8a, False)
check("UI-4: 'Aggiorna ovunque' swaps to the EDITABLE table grid twin (view hides, edit shows+interactive)",
      confirmed8[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is True and
      confirmed8[app.ST_IDX_TABLE_GRID_EDIT]["interactive"] is True and
      confirmed8[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is False,
      (confirmed8[app.ST_IDX_TABLE_GRID_VIEW], confirmed8[app.ST_IDX_TABLE_GRID_EDIT]))
check("UI-5: 'Aggiorna ovunque' hides both decision buttons (decision already made)",
      not confirmed8[app.ST_IDX_UPDATE_EVERYWHERE_BTN]["visible"] and not confirmed8[app.ST_IDX_FORK_LOCAL_BTN]["visible"],
      (confirmed8[app.ST_IDX_UPDATE_EVERYWHERE_BTN], confirmed8[app.ST_IDX_FORK_LOCAL_BTN]))
token8 = confirmed8[app.ST_IDX_SHARED_IMPACT_TOKEN]
check("UI-5b: 'Aggiorna ovunque' captures a real (non-empty) impact token", bool(token8), token8)

save8 = app.studio_save_ui(str(p8), "PUB-MAIN", "block", b8a, "table", t8, 1, "", "", "Shared table",
                           "Edited via UI", pd.DataFrame([["1", "9"]], columns=["A", "B"]), "", "",
                           "Writer A", False, True, token8)
check("UI-6: Save WITH the confirmed shared_confirmed=True state AND its captured token succeeds",
      "✅" in save8[app.ST_IDX_SAVE_MSG], save8[app.ST_IDX_SAVE_MSG])
check("UI-7: shared_confirmed resets to False after a successful save (must re-decide next time)",
      save8[app.ST_IDX_SHARED_CONFIRMED] is False, save8[app.ST_IDX_SHARED_CONFIRMED])

bundle8b = app.studio_open_ui(str(p8), "PUB-MAIN", f"block:{b8b}", False)
fork8 = app.studio_fork_local_ui(str(p8), "PUB-MAIN", "block", b8b, "table", t8,
                                 bundle8b[app.ST_IDX_VERSION], False, "Writer A")
check("UI-8: 'Crea una copia locale' via the real callback succeeds", "✅" in fork8[app.ST_IDX_SAVE_MSG], fork8[app.ST_IDX_SAVE_MSG])
check("UI-9: after the fork, the decision buttons hide (this block's table is no longer shared)",
      fork8[app.ST_IDX_UPDATE_EVERYWHERE_BTN]["visible"] is False, fork8[app.ST_IDX_UPDATE_EVERYWHERE_BTN])
repo8b, _ = av.open_service(p8)
b8a_ref_after = repo8b.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b8a,)).fetchone()["object_ref_id"]
check("UI-10: the OTHER block's reference is untouched by the fork (still points at the original table)",
      b8a_ref_after == t8, b8a_ref_after)
repo8b.close()

diff8 = app.studio_compare_prev_ui(str(p8), "block", b8a, "table", t8, 2)
check("UI-11: 'Confronta con versione precedente' via the real callback produces a non-error diff summary",
      "⛔" not in diff8 and diff8.strip() != "", diff8)

diff8_missing = app.studio_compare_ui(str(p8), "block", b8a, "table", t8, 2, 999)
check("UI-12: comparing against a non-existent version fails closed with a clear message, not a crash",
      "⛔" in diff8_missing, diff8_missing)

# Non-shared save/revert paths remain completely unaffected by the new
# confirm_shared parameter (regression -- must default harmlessly to False).
p9 = _new_project("d_nonshared_regression")
repo9, svc9 = av.open_service(p9)
t9 = svc9.create_table_in_topic("T1", actor="Writer A", columns=["A"], rows=[["1"]])["table_id"]
r9 = svc9.save_table(t9, caption="fine", expected_version=1, actor="Writer A")
check("UI-13: a NON-shared table saves normally with no confirm_shared needed (default False is harmless)",
      r9["version"] == 2, r9)
r9b = svc9.revert_table_to_version(t9, 1, actor="Writer A")
check("UI-14: a NON-shared table reverts normally with no confirm_shared needed", r9b["status"] in ("Draft",), r9b)
repo9.close()

# Archived-block guard still takes precedence over the shared-object guard
# (unchanged 4C behavior -- confirm_shared does not bypass it).
p10 = _new_project("d_archived_precedence")
repo10, svc10 = av.open_service(p10)
t10, b10a, b10b = _share_table(repo10, svc10)
svc10.archive_block(b10a, actor="Writer A")
check("UI-15: an archived referencing block still blocks editing even with confirm_shared=True "
      "(ArchivedObjectError takes precedence over the shared-object guard)",
      _raises(lambda: svc10.save_table(t10, caption="x", expected_version=1, actor="Writer A", confirm_shared=True),
              ArchivedObjectError), None)
repo10.close()

# ====================================================================
# Impact-reference-set drift (4D Final Integrity Gate, Blocker A) -- the
# exact reference SET the user previewed via 'Aggiorna ovunque' must still
# match at Save time, not just the object's own expected_version. Add-
# reference and remove-reference drift both exercised, backend AND via
# the real app.py UI callbacks.
# ====================================================================
p11 = _new_project("d_impact_drift_add_reference")
repo11, svc11 = av.open_service(p11)
t11, b11a, b11b = _share_table(repo11, svc11)
repo11.close()

confirmed11 = app.studio_confirm_shared_update_ui(str(p11), "PUB-MAIN", "block", b11a, False)
token11 = confirmed11[app.ST_IDX_SHARED_IMPACT_TOKEN]
check("drift-1: a real impact token is captured on confirm", bool(token11), token11)

# A third reference is added to the SAME table AFTER the preview/confirm
# above -- the object's own version is still untouched.
repo11b, svc11b = av.open_service(p11)
repo11b.create_topic("T3", "Topic Three", "body", status="Draft")
repo11b.add_block("BLK-SHARE3", "T3", "table", position=1, object_ref_id=t11, status="Draft")
repo11b.close()

save11_stale = app.studio_save_ui(str(p11), "PUB-MAIN", "block", b11a, "table", t11, 1, "", "", "Shared table",
                                  "Should not apply", pd.DataFrame([["1", "2"]], columns=["A", "B"]), "", "",
                                  "Writer A", False, True, token11)
check("drift-2: Save with the now-stale (2-ref) token fails closed after a 3rd reference was added",
      "⛔" in save11_stale[app.ST_IDX_SAVE_MSG] and "cambiati dopo l'anteprima" in save11_stale[app.ST_IDX_SAVE_MSG],
      save11_stale[app.ST_IDX_SAVE_MSG])
repo11c, _ = av.open_service(p11)
live11 = repo11c.conn.execute("SELECT version,caption FROM structured_tables WHERE table_id=?", (t11,)).fetchone()
repo11c.close()
check("drift-3: no version bump and no content mutation from the failed add-reference-drift attempt",
      live11["version"] == 1 and live11["caption"] == "Shared table", dict(live11))

# Re-confirm (fresh preview -> fresh token that reflects all 3 refs now) --
# "table shared update after refreshed preview": Save must then succeed.
reconfirmed11 = app.studio_confirm_shared_update_ui(str(p11), "PUB-MAIN", "block", b11a, False)
fresh_token11 = reconfirmed11[app.ST_IDX_SHARED_IMPACT_TOKEN]
check("drift-4: the refreshed token differs from the stale one", fresh_token11 != token11, (token11, fresh_token11))
save11_fresh = app.studio_save_ui(str(p11), "PUB-MAIN", "block", b11a, "table", t11, 1, "", "", "Shared table",
                                  "Applied after refresh", pd.DataFrame([["1", "2"]], columns=["A", "B"]), "", "",
                                  "Writer A", False, True, fresh_token11)
check("drift-5: Save WITH the refreshed token succeeds (table shared update after refreshed preview)",
      "✅" in save11_fresh[app.ST_IDX_SAVE_MSG], save11_fresh[app.ST_IDX_SAVE_MSG])
repo11d, _ = av.open_service(p11)
audit11 = json.loads(repo11d.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='table' AND object_id=? AND action='AUTHORING_SAVE' ORDER BY event_id DESC LIMIT 1",
    (t11,)).fetchone()["detail_json"])
repo11d.close()
check("drift-6: audit impact_count correctly reflects the refreshed 3-reference count", audit11.get("impact_count") == 3, audit11)

# ---- REMOVE-reference drift (table) ----
p12 = _new_project("d_impact_drift_remove_reference")
repo12, svc12 = av.open_service(p12)
t12, b12a, b12b = _share_table(repo12, svc12)
repo12.close()
confirmed12 = app.studio_confirm_shared_update_ui(str(p12), "PUB-MAIN", "block", b12a, False)
token12 = confirmed12[app.ST_IDX_SHARED_IMPACT_TOKEN]
# Another session forks b12b away -- the reference set genuinely shrinks
# from 2 to 1, object version still untouched.
repo12b, svc12b = av.open_service(p12)
svc12b.fork_table_local(t12, block_id=b12b, expected_version=1, actor="Altro Utente")
repo12b.close()
save12_stale = app.studio_save_ui(str(p12), "PUB-MAIN", "block", b12a, "table", t12, 1, "", "", "Shared table",
                                  "Should not apply", pd.DataFrame([["1", "2"]], columns=["A", "B"]), "", "",
                                  "Writer A", False, True, token12)
check("drift-7: Save with the stale (pre-removal) token fails closed after a reference was forked away",
      "⛔" in save12_stale[app.ST_IDX_SAVE_MSG] and "cambiati dopo l'anteprima" in save12_stale[app.ST_IDX_SAVE_MSG],
      save12_stale[app.ST_IDX_SAVE_MSG])
repo12c, _ = av.open_service(p12)
live12 = repo12c.conn.execute("SELECT version,caption FROM structured_tables WHERE table_id=?", (t12,)).fetchone()
repo12c.close()
check("drift-8: no version bump and no content mutation from the failed remove-reference-drift attempt",
      live12["version"] == 1 and live12["caption"] == "Shared table", dict(live12))

# ---- ADD-reference drift (figure), mirrors drift-1..drift-6 ----
p13 = _new_project("d_impact_drift_figure")
repo13, svc13 = av.open_service(p13)
f13, b13a, b13b = _share_figure(repo13, svc13)
repo13.close()
confirmed13 = app.studio_confirm_shared_update_ui(str(p13), "PUB-MAIN", "block", b13a, False)
ftoken13 = confirmed13[app.ST_IDX_SHARED_IMPACT_TOKEN]
check("drift-9 (figure): a real impact token is captured on confirm", bool(ftoken13), ftoken13)
repo13b, svc13b = av.open_service(p13)
repo13b.create_topic("T3", "Topic Three", "body", status="Draft")
repo13b.add_block("BLK-FSHARE3", "T3", "figure", position=1, object_ref_id=f13, status="Draft")
repo13b.close()
save13_stale = app.studio_save_ui(str(p13), "PUB-MAIN", "block", b13a, "figure", f13, 1, "", "", "", "",
                                  pd.DataFrame(), "Should not apply", "",
                                  "Writer A", False, True, ftoken13)
check("drift-10 (figure): Save with the now-stale token fails closed after a 3rd reference was added",
      "⛔" in save13_stale[app.ST_IDX_SAVE_MSG] and "cambiati dopo l'anteprima" in save13_stale[app.ST_IDX_SAVE_MSG],
      save13_stale[app.ST_IDX_SAVE_MSG])
reconfirmed13 = app.studio_confirm_shared_update_ui(str(p13), "PUB-MAIN", "block", b13a, False)
fresh_ftoken13 = reconfirmed13[app.ST_IDX_SHARED_IMPACT_TOKEN]
save13_fresh = app.studio_save_ui(str(p13), "PUB-MAIN", "block", b13a, "figure", f13, 1, "", "", "", "",
                                  pd.DataFrame(), "Applied after refresh", "",
                                  "Writer A", False, True, fresh_ftoken13)
check("drift-11 (figure): Save WITH the refreshed token succeeds (figure shared update after refreshed preview)",
      "✅" in save13_fresh[app.ST_IDX_SAVE_MSG], save13_fresh[app.ST_IDX_SAVE_MSG])
repo13c, _ = av.open_service(p13)
audit13 = json.loads(repo13c.conn.execute(
    "SELECT detail_json FROM audit_events WHERE object_type='figure' AND object_id=? AND action='AUTHORING_SAVE' ORDER BY event_id DESC LIMIT 1",
    (f13,)).fetchone()["detail_json"])
repo13c.close()
check("drift-12 (figure): audit impact_count correctly reflects the refreshed 3-reference count",
      audit13.get("impact_count") == 3, audit13)

# ====================================================================
# component/component_ref scope decision (4D Final Integrity Gate,
# Blocker B, Option 1): get_reuse_impact() supports "component" -- real,
# block-level-precise reporting -- but there is NO save_component()/
# editor, so a component_ref block must NEVER present as editable and
# must NEVER offer 'Aggiorna ovunque'/'Crea una copia locale' (there is
# no editor to apply them to). The outline and the block's readonly
# notice still surface the real reuse impact.
# ====================================================================
p14 = _new_project("d_component_scope")
repo14, svc14 = av.open_service(p14)
repo14.create_component("CMP-1", "Warning block", "Attenzione: superficie calda.")
repo14.add_block("BLK-C1", "T1", "component_ref", position=1, object_ref_id="CMP-1", status="Draft")
repo14.add_block("BLK-C2", "T2", "component_ref", position=1, object_ref_id="CMP-1", status="Draft")
repo14.create_component("CMP-2", "Solo component", "Solo body")
repo14.add_block("BLK-C3", "T1", "component_ref", position=2, object_ref_id="CMP-2", status="Draft")

impact14 = svc14.get_reuse_impact("component", "CMP-1")
check("comp-1: get_reuse_impact('component', ...) reference_count == 2 for a shared component",
      impact14["reference_count"] == 2, impact14)
check("comp-2: get_reuse_impact('component', ...) is_shared == True", impact14["is_shared"] is True, impact14)
check("comp-3: get_reuse_impact('component', ...) lists both referencing topics",
      sorted(t["topic_id"] for t in impact14["topics"]) == ["T1", "T2"], impact14["topics"])
check("comp-4: get_reuse_impact('component', ...) produces a real impact_token (same primitive as table/figure)",
      bool(impact14["impact_token"]), impact14)

impact14_solo = svc14.get_reuse_impact("component", "CMP-2")
check("comp-5: a non-shared component correctly reports is_shared == False", impact14_solo["is_shared"] is False, impact14_solo)

rows14 = av.outline_rows(repo14, "PUB-MAIN", include_archived=False)
c1_row = next(r for r in rows14 if r["id"] == "BLK-C1")
c3_row = next(r for r in rows14 if r["id"] == "BLK-C3")
check("comp-6: the outline surfaces the real reuse count for a shared component_ref",
      "riutilizzata 2 volte" in c1_row["preview"], c1_row["preview"])
check("comp-7: a NON-shared component_ref carries no reuse suffix", "riutilizzata" not in c3_row["preview"], c3_row["preview"])
check("comp-8: component_ref is NEVER marked editable in the outline, shared or not",
      c1_row["editable"] is False and c3_row["editable"] is False, (c1_row, c3_row))
repo14.close()

bundle14 = app.studio_open_ui(str(p14), "PUB-MAIN", "block:BLK-C1", False)
check("comp-9: the real Studio UI never presents a shared component_ref as editable (Save not interactive)",
      not bundle14[app.ST_IDX_SAVE_BTN]["interactive"], bundle14[app.ST_IDX_SAVE_BTN])
check("comp-10: no 'Aggiorna ovunque' button is EVER shown for a component_ref block (no editor to apply it to)",
      bundle14[app.ST_IDX_UPDATE_EVERYWHERE_BTN]["visible"] is False, bundle14[app.ST_IDX_UPDATE_EVERYWHERE_BTN])
check("comp-11: no 'Crea una copia locale' button is EVER shown for a component_ref block",
      bundle14[app.ST_IDX_FORK_LOCAL_BTN]["visible"] is False, bundle14[app.ST_IDX_FORK_LOCAL_BTN])
check("comp-12: the readonly notice explicitly states the component is not yet editable in this Studio",
      "non ancora editabile" in bundle14[app.ST_IDX_READONLY]["value"], bundle14[app.ST_IDX_READONLY])
check("comp-13: the real usage count is surfaced somewhere in the panel (notice or reuse-impact markdown), never silently hidden",
      "2 punti" in bundle14[app.ST_IDX_READONLY]["value"] or "2 punti" in bundle14[app.ST_IDX_REUSE_IMPACT]["value"],
      (bundle14[app.ST_IDX_READONLY], bundle14[app.ST_IDX_REUSE_IMPACT]))

# BACKLOG NOTE (documented, not an omission): a component referenced only
# via an inline {{component:ID}} token embedded directly in a topic body
# (no component_ref block at all -- see ContentRepository.resolve_
# components()) is NOT counted by get_reuse_impact("component", ...): that
# is a distinct, text-substitution reuse mechanism, already surfaced only
# through the existing advanced-mode impact_summary()/impact_map_
# industrial(kind="component"). Folding both mechanisms into one block-
# level reference count would misrepresent both. A genuine component-
# authoring Studio editor (needed to make 'Aggiorna ovunque'/'Crea una
# copia locale' meaningful for components) remains explicitly out of
# scope for 4D/4E and belongs to a future dedicated milestone.

# ====================================================================
# Revision diff sanity: at least one NON-empty table diff and one
# NON-empty figure diff, through the real compare callback (4D Final
# Integrity Gate requirement).
# ====================================================================
p15 = _new_project("d_nonempty_diff")
repo15, svc15 = av.open_service(p15)
t15 = svc15.create_table_in_topic("T1", actor="Writer A", caption="v1", columns=["A", "B"], rows=[["1", "2"]])["table_id"]
svc15.save_table(t15, columns=["A", "B"], rows=[["1", "CHANGED"]], expected_version=1, actor="Writer A")
b15 = repo15.conn.execute("SELECT block_id FROM content_blocks WHERE block_type='table' AND object_ref_id=?", (t15,)).fetchone()["block_id"]
diff15 = app.studio_compare_prev_ui(str(p15), "block", b15, "table", t15, 2)
check("diff-1: a real, non-empty table cell edit produces a non-empty compare summary via the real UI callback",
      "⛔" not in diff15, diff15)
check("diff-2: the table diff summary reports at least '1 cella modificata'", "1 cella modificata" in diff15, diff15)

f15 = svc15.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="v1 caption")["figure_id"]
svc15.save_figure(f15, caption="v2 caption", expected_version=1, actor="Writer A")
bf15 = repo15.conn.execute("SELECT block_id FROM content_blocks WHERE block_type='figure' AND object_ref_id=?", (f15,)).fetchone()["block_id"]
diff15f = app.studio_compare_prev_ui(str(p15), "block", bf15, "figure", f15, 2)
check("diff-3: a real caption edit produces a non-empty figure compare summary via the real UI callback",
      "⛔" not in diff15f, diff15f)
check("diff-4: the figure diff summary names the changed field ('didascalia modificata')",
      "didascalia modificata" in diff15f.lower(), diff15f)
repo15.close()

# ====================================================================
# 4D Final Integrity Gate follow-up: Shared Table Unlock Editing Fix.
# Root cause: st_table_grid used to be a SINGLE gr.Dataframe whose
# `interactive` prop was toggled False->True dynamically (shared/
# unconfirmed -> confirmed). Verified live against the real, mounted
# Gradio 6.5.1 component that a complete pointer+mouse event sequence DOES
# make that toggle work -- but depending on dynamic interactive-toggling
# of an already-mounted virtualized Dataframe is inherently fragile
# (undocumented third-party behavior). Fixed the robust way: TWO mutually
# exclusive components (st_table_grid_view, permanently interactive=False;
# st_table_grid_edit, permanently interactive=True), swapped only via
# `visible=`. This section verifies the REAL component-graph configuration
# the fix produces -- not just a can_edit=True string -- across every
# transition: pre-decision, post-'Aggiorna ovunque', post-fork, post-save/
# navigation reset, native-menu neutralization, and actor_state stability.
# ====================================================================
p16 = _new_project("d_unlock_editing_fix")
repo16, svc16 = av.open_service(p16)
t16, b16a, b16b = _share_table(repo16, svc16)
repo16.close()

# 1) before decision: view visible+non-interactive, edit hidden.
bundle16 = app.studio_open_ui(str(p16), "PUB-MAIN", f"block:{b16a}", False)
check("unlock-1: pre-decision -- view grid visible, non-interactive; edit grid hidden",
      bundle16[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is True and
      bundle16[app.ST_IDX_TABLE_GRID_VIEW].get("interactive") is False and
      bundle16[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is False,
      (bundle16[app.ST_IDX_TABLE_GRID_VIEW], bundle16[app.ST_IDX_TABLE_GRID_EDIT]))
check("unlock-1b: pre-decision -- native structural menu stays neutralized on the visible (view) grid",
      bundle16[app.ST_IDX_TABLE_GRID_VIEW]["row_count"][1] == "fixed" and
      bundle16[app.ST_IDX_TABLE_GRID_VIEW]["column_count"][1] == "fixed",
      bundle16[app.ST_IDX_TABLE_GRID_VIEW])

# 2) after 'Aggiorna ovunque': edit visible+interactive, view hidden, real data present.
confirmed16 = app.studio_confirm_shared_update_ui(str(p16), "PUB-MAIN", "block", b16a, False)
check("unlock-2: post-'Aggiorna ovunque' -- edit grid visible+interactive, view grid hidden",
      confirmed16[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is True and
      confirmed16[app.ST_IDX_TABLE_GRID_EDIT].get("interactive") is True and
      confirmed16[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is False,
      (confirmed16[app.ST_IDX_TABLE_GRID_VIEW], confirmed16[app.ST_IDX_TABLE_GRID_EDIT]))
check("unlock-2b: post-'Aggiorna ovunque' -- native structural menu stays neutralized on the now-visible edit grid",
      confirmed16[app.ST_IDX_TABLE_GRID_EDIT]["row_count"][1] == "fixed" and
      confirmed16[app.ST_IDX_TABLE_GRID_EDIT]["column_count"][1] == "fixed",
      confirmed16[app.ST_IDX_TABLE_GRID_EDIT])
check("unlock-2c: post-'Aggiorna ovunque' -- the edit grid carries the real current cell data (fixture: columns A/B, row 1/2)",
      confirmed16[app.ST_IDX_TABLE_GRID_EDIT]["value"].values.tolist() == [["1", "2"]],
      confirmed16[app.ST_IDX_TABLE_GRID_EDIT]["value"])

# Real end-to-end edit through the actual Save path (the reported symptom:
# decision accepted, Save active, but the grid itself never became usable).
token16 = confirmed16[app.ST_IDX_SHARED_IMPACT_TOKEN]
edited_df = pd.DataFrame([["1", "2-edited"]], columns=["A", "B"])
save16 = app.studio_save_ui(str(p16), "PUB-MAIN", "block", b16a, "table", t16, 1, "", "", "",
                            "Shared table", edited_df, "", "", "Alessandro", False, True, token16)
check("unlock-3: the real edited cell value saves successfully (version 2)", "versione 2" in save16[app.ST_IDX_SAVE_MSG], save16[app.ST_IDX_SAVE_MSG])
repo16b, svc16b = av.open_service(p16)
d16 = svc16b.load_table_for_edit(t16)
check("unlock-3b: the saved table genuinely contains the new cell value ('2-edited')",
      d16["rows"] == [["1", "2-edited"]], d16["rows"])
b16b_ref = repo16b.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b16b,)).fetchone()["object_ref_id"]
check("unlock-3c: the SECOND reference points at the SAME table_id (propagation, not a fork)", b16b_ref == t16, b16b_ref)
repo16b.close()

# 3) reopen the second reference: value propagated, and (item 7) the
# shared decision resets -- view visible again, edit hidden, until the
# user re-confirms for THIS reference.
bundle16b = app.studio_open_ui(str(p16), "PUB-MAIN", f"block:{b16b}", False)
check("unlock-4: the second reference shows the propagated value ('2-edited')",
      bundle16b[app.ST_IDX_TABLE_GRID_VIEW]["value"].values.tolist() == [["1", "2-edited"]],
      bundle16b[app.ST_IDX_TABLE_GRID_VIEW]["value"])
check("unlock-5 (item 7): after navigation/reopen, the shared decision resets -- view shown, edit hidden again",
      bundle16b[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is True and bundle16b[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is False,
      (bundle16b[app.ST_IDX_TABLE_GRID_VIEW], bundle16b[app.ST_IDX_TABLE_GRID_EDIT]))
check("unlock-5b (item 9): actor_state is untouched by any of the above -- studio_open_ui never reads/writes it, "
      "it is purely a UI State round-tripped by the caller (Gradio wiring), not mutated by any backend call",
      "actor_state" not in app.studio_open_ui.__code__.co_varnames[:app.studio_open_ui.__code__.co_argcount], None)

# 4) local fork: (item 3) immediately editable, no further decision needed.
confirmed16c = app.studio_confirm_shared_update_ui(str(p16), "PUB-MAIN", "block", b16b, False)
fork16 = app.studio_fork_local_ui(str(p16), "PUB-MAIN", "block", b16b, "table", t16,
                                  confirmed16c[app.ST_IDX_VERSION], False, "Alessandro")
check("unlock-6 (item 3): immediately after 'Crea una copia locale', the edit grid is visible+interactive "
      "with NO decision panel needed (fork's own object is never shared)",
      fork16[app.ST_IDX_TABLE_GRID_EDIT]["visible"] is True and
      fork16[app.ST_IDX_TABLE_GRID_EDIT].get("interactive") is True and
      fork16[app.ST_IDX_TABLE_GRID_VIEW]["visible"] is False and
      fork16[app.ST_IDX_UPDATE_EVERYWHERE_BTN]["visible"] is False,
      (fork16[app.ST_IDX_TABLE_GRID_VIEW], fork16[app.ST_IDX_TABLE_GRID_EDIT], fork16[app.ST_IDX_UPDATE_EVERYWHERE_BTN]))

# Real edit-then-save on the fork, verifying isolation (item 5).
repo16c, _ = av.open_service(p16)
fork_table_id = repo16c.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b16b,)).fetchone()["object_ref_id"]
repo16c.close()
check("unlock-7: the fork produced a genuinely NEW table_id, distinct from the shared original", fork_table_id != t16, fork_table_id)
fork_edit_df = pd.DataFrame([["1", "9-fork-only"]], columns=["A", "B"])
save16fork = app.studio_save_ui(str(p16), "PUB-MAIN", "block", b16b, "table", fork_table_id, 1, "", "", "",
                                "Shared table", fork_edit_df, "", "", "Alessandro", False, False, None)
check("unlock-8: saving the fork's real edited cell succeeds with NO confirm_shared/token needed (not shared)",
      "versione 2" in save16fork[app.ST_IDX_SAVE_MSG], save16fork[app.ST_IDX_SAVE_MSG])
repo16d, svc16d = av.open_service(p16)
d16_orig = svc16d.load_table_for_edit(t16)
check("unlock-9 (item 5): the ORIGINAL shared table is untouched by the fork's own edit -- still '2-edited', not '9-fork-only'",
      d16_orig["rows"] == [["1", "2-edited"]], d16_orig["rows"])
repo16d.close()

# ====================================================================
# Local Fork Isolation regression (Lightweight Fix ticket): the FULL
# open -> fork -> edit -> save chain, replayed EXACTLY as Gradio's own
# event wiring drives it -- each call's inputs are the PREVIOUS call's
# bundle-returned State values (ST_IDX_OBJECT_REF_ID/ST_IDX_VERSION/
# ST_IDX_KIND/ST_IDX_ID), never hand-picked -- so this genuinely proves
# the UI callback chain propagates the fork's own object id forward, not
# just that fork_table_local()/fork_figure_local() are correct in
# isolation. Reproduces the manual-acceptance flow verbatim: a table
# already bumped to '4,5 bar' via a prior 'Aggiorna ovunque', then forked
# and edited to '5 bar'.
# ====================================================================
p17 = _new_project("d_fork_isolation_full_chain")
repo17, svc17 = av.open_service(p17)
t17, b17a, b17b = _share_table(repo17, svc17)
tok17 = svc17.get_reuse_impact("table", t17)["impact_token"]
svc17.save_table(t17, rows=[["1", "4,5-shared"]], expected_version=1, actor="Writer A",
                 confirm_shared=True, expected_impact_token=tok17)
repo17.close()

open17 = app.studio_open_ui(str(p17), "PUB-MAIN", f"block:{b17a}", False)
check("fork-iso-1: opening block A shows the shared object_ref_id (the original table)",
      open17[app.ST_IDX_OBJECT_REF_ID] == t17, (open17[app.ST_IDX_OBJECT_REF_ID], t17))

fork17 = app.studio_fork_local_ui(str(p17), "PUB-MAIN", open17[app.ST_IDX_KIND], open17[app.ST_IDX_ID],
                                  open17[app.ST_IDX_BLOCK_TYPE], open17[app.ST_IDX_OBJECT_REF_ID],
                                  open17[app.ST_IDX_VERSION], False, "Writer A")
fork17_id = fork17[app.ST_IDX_OBJECT_REF_ID]
check("fork-iso-2 (item 10): the fork UI callback returns a NEW object_ref_id, distinct from the original",
      fork17_id != t17 and fork17_id is not None, (fork17_id, t17))
check("fork-iso-3: the fork's own edit grid opens pre-populated with the CURRENT shared content ('4,5-shared')",
      fork17[app.ST_IDX_TABLE_GRID_EDIT]["value"].values.tolist() == [["1", "4,5-shared"]],
      fork17[app.ST_IDX_TABLE_GRID_EDIT]["value"])

edit17_df = pd.DataFrame([["1", "5-fork"]], columns=["A", "B"])
save17 = app.studio_save_ui(str(p17), "PUB-MAIN", fork17[app.ST_IDX_KIND], fork17[app.ST_IDX_ID],
                            fork17[app.ST_IDX_BLOCK_TYPE], fork17[app.ST_IDX_OBJECT_REF_ID],
                            fork17[app.ST_IDX_VERSION], "", "", "", "", edit17_df, "", "",
                            "Writer A", False, False, None)
check("fork-iso-4 (item 11): the Save callback -- fed the fork bundle's own object_ref_id, exactly as Gradio "
      "would -- saves successfully with NO confirm_shared/token (the fork is not shared)",
      "versione 2" in save17[app.ST_IDX_SAVE_MSG], save17[app.ST_IDX_SAVE_MSG])

repo17b, svc17b = av.open_service(p17)
orig17_after = repo17b.conn.execute("SELECT version,rows_json FROM structured_tables WHERE table_id=?", (t17,)).fetchone()
fork17_after = repo17b.conn.execute("SELECT version,rows_json FROM structured_tables WHERE table_id=?", (fork17_id,)).fetchone()
check("fork-iso-5 (item 5, DB proof): the ORIGINAL table is untouched -- still '4,5-shared', version unchanged (2)",
      json.loads(orig17_after["rows_json"]) == [["1", "4,5-shared"]] and orig17_after["version"] == 2,
      dict(orig17_after))
check("fork-iso-6 (item 4, DB proof): the FORK genuinely diverged -- '5-fork', its own version bumped to 2",
      json.loads(fork17_after["rows_json"]) == [["1", "5-fork"]] and fork17_after["version"] == 2,
      dict(fork17_after))

b17a_ref = repo17b.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b17a,)).fetchone()["object_ref_id"]
b17b_ref = repo17b.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b17b,)).fetchone()["object_ref_id"]
check("fork-iso-7 (items 1-3): block A points at the fork, block B still points at the original -- exactly the "
      "semantics the ticket specifies (A -> TBL-FORK, B -> TBL-ORIGINAL)",
      b17a_ref == fork17_id and b17b_ref == t17, (b17a_ref, b17b_ref))

impact17_orig = svc17b.get_reuse_impact("table", t17)
impact17_fork = svc17b.get_reuse_impact("table", fork17_id)
check("fork-iso-8 (item 6): original reference_count == 1 after the fork", impact17_orig["reference_count"] == 1, impact17_orig)
check("fork-iso-9 (item 7): fork reference_count == 1", impact17_fork["reference_count"] == 1, impact17_fork)

hist_orig17 = svc17b.table_history(t17)
hist_fork17 = svc17b.table_history(fork17_id)
check("fork-iso-10 (item 8): the original's history stops at 2 entries (creation + the pre-fork 'Aggiorna ovunque' "
      "save) -- never re-saved after the fork, so it never grows a 3rd entry",
      len(hist_orig17) == 2, hist_orig17)
check("fork-iso-10b (item 8): the fork has its OWN, independent 2-entry history (v1 at fork time, v2 after its "
      "own edit) -- NOT sharing the original's history at all",
      len(hist_fork17) == 2, hist_fork17)

audit17 = [dict(r) for r in repo17b.conn.execute(
    "SELECT object_type,object_id,action,detail_json FROM audit_events WHERE object_id IN (?,?,?) ORDER BY event_id",
    (t17, fork17_id, b17a))]
actions17 = [(a["object_type"], a["action"]) for a in audit17]
check("fork-iso-11 (item 9): provenance/lineage preserved -- AUTHORING_FORK_CREATED + AUTHORING_FORKED + "
      "REWIRED_TO_LOCAL_FORK all present in the audit trail",
      ("table", "AUTHORING_FORK_CREATED") in actions17 and ("table", "AUTHORING_FORKED") in actions17 and
      ("block", "REWIRED_TO_LOCAL_FORK") in actions17, actions17)
repo17b.close()

# ---- Figure mirror of the same full-chain isolation proof ----
p18 = _new_project("d_fork_isolation_figure")
repo18, svc18 = av.open_service(p18)
f18, b18a, b18b = _share_figure(repo18, svc18)
ftok18 = svc18.get_reuse_impact("figure", f18)["impact_token"]
svc18.save_figure(f18, caption="4,5-shared", expected_version=1, actor="Writer A",
                  confirm_shared=True, expected_impact_token=ftok18)
repo18.close()

open18 = app.studio_open_ui(str(p18), "PUB-MAIN", f"block:{b18a}", False)
fork18 = app.studio_fork_local_ui(str(p18), "PUB-MAIN", open18[app.ST_IDX_KIND], open18[app.ST_IDX_ID],
                                  open18[app.ST_IDX_BLOCK_TYPE], open18[app.ST_IDX_OBJECT_REF_ID],
                                  open18[app.ST_IDX_VERSION], False, "Writer A")
fork18_id = fork18[app.ST_IDX_OBJECT_REF_ID]
check("fork-iso-fig-1: figure fork UI callback returns a NEW object_ref_id", fork18_id != f18, (fork18_id, f18))
save18 = app.studio_save_ui(str(p18), "PUB-MAIN", fork18[app.ST_IDX_KIND], fork18[app.ST_IDX_ID],
                            fork18[app.ST_IDX_BLOCK_TYPE], fork18[app.ST_IDX_OBJECT_REF_ID],
                            fork18[app.ST_IDX_VERSION], "", "", "", "", pd.DataFrame(), "5-fork", "",
                            "Writer A", False, False, None)
check("fork-iso-fig-2: saving the figure fork's caption succeeds with no confirm_shared/token", "versione 2" in save18[app.ST_IDX_SAVE_MSG], save18[app.ST_IDX_SAVE_MSG])
repo18b, svc18b = av.open_service(p18)
orig18_after = repo18b.conn.execute("SELECT caption,version FROM figures WHERE figure_id=?", (f18,)).fetchone()
fork18_after = repo18b.conn.execute("SELECT caption,version FROM figures WHERE figure_id=?", (fork18_id,)).fetchone()
check("fork-iso-fig-3: the ORIGINAL figure is untouched -- still '4,5-shared', version unchanged (2)", dict(orig18_after) == {"caption": "4,5-shared", "version": 2}, dict(orig18_after))
check("fork-iso-fig-4: the FORK genuinely diverged -- caption '5-fork', version 2", dict(fork18_after) == {"caption": "5-fork", "version": 2}, dict(fork18_after))
b18b_ref = repo18b.conn.execute("SELECT object_ref_id FROM content_blocks WHERE block_id=?", (b18b,)).fetchone()["object_ref_id"]
check("fork-iso-fig-5: block B (never touched) still points at the original figure", b18b_ref == f18, b18b_ref)
repo18b.close()

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"), "tests": len(results)}))
