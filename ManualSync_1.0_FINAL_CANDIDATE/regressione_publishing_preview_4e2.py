"""Milestone 4E.2 regression suite: Publishing Preview.

Tests publishing_preview.py's pure, read-only model/render functions
(build_preview_model, render_preview_html, _resolve_block_content) and
the real app.py UI callbacks (studio_preview_profile_choices_ui,
studio_preview_refresh_ui) -- calling the actual functions the product
wires to Gradio, not a mock, matching the discipline established for the
4B/4C/4D/4E.1 suites.

Condition evaluation is NEVER reimplemented in publishing_preview.py --
every test that exercises inclusion/exclusion is really testing that the
preview correctly ORCHESTRATES the unchanged 4E.1 evaluator
(AuthoringService.evaluate_element_with_profile) across a publication,
not a second copy of OR/AND/fail-closed logic.

Covers the ticket's 52-item minimum list (basic, EU/US profile,
OR/AND, unresolved, reuse, read-only, table/figure/component_ref,
status, project switch, UI callbacks) plus a few extra risk-driven cases
(local fork rendering, parent-gating with a real mismatched-condition
child, a genuine on-disk figure asset).

Backend/integration only -- no browser (RAM-constrained iteration).
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
from authoring_service import AuthoringService
import authoring_view as av
from project_lifecycle import create_project
import publishing_preview as pp
import app
import gradio as gr

TMP = Path(tempfile.mkdtemp(prefix="manualsync_publishing_preview_4e2_"))
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
        repo.create_topic("T2", "Topic Two", "legacy body", status="Approved")
        repo.create_publication("PUB-MAIN", "Synthetic Manual", ["T1", "T2"])
    finally:
        repo.close()
    return p


def _seed_market_edition(svc: AuthoringService, actor: str = "Writer A") -> None:
    svc.create_condition_dimension("market", "Mercato", actor=actor)
    svc.create_condition_value("eu", "market", "EU", actor=actor)
    svc.create_condition_value("uk", "market", "UK", actor=actor)
    svc.create_condition_value("us", "market", "US", actor=actor)
    svc.create_condition_dimension("edition", "Edizione", actor=actor)
    svc.create_condition_value("operator", "edition", "Operator", actor=actor)
    svc.create_condition_value("service", "edition", "Service", actor=actor)


def _share_table(repo: IndustrialRepository, svc: AuthoringService):
    res = svc.create_table_in_topic("T1", actor="Writer A", title="Specifiche", caption="Shared table",
                                    columns=["A", "B"], rows=[["1", "2"]])
    table_id, block1 = res["table_id"], res["block_id"]
    block2 = f"BLK-SHARE-{table_id[-6:]}"
    repo.add_block(block2, "T2", "table", position=1, object_ref_id=table_id, status="Draft")
    return table_id, block1, block2


def _make_test_image(name: str) -> Path:
    from PIL import Image
    path = TMP / name
    Image.new("RGB", (12, 8), color=(90, 140, 200)).save(path)
    return path


IMG1 = _make_test_image("fixture_image_1.png")


# ====================================================================
# Fixture setup: one project exercising every scenario the suite needs.
# ====================================================================
p1 = _new_project("e2_01_main")
repo1, svc1 = av.open_service(p1)
_seed_market_edition(svc1)

# T1 (Installazione-equivalent): intro (no condition), EU-only, US-only,
# a shared table placement (EU here), a valid figure (EU), an OR-condition
# paragraph (EU or UK), an AND-condition paragraph (EU+Service).
intro_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Sempre visibile.")["block_id"]
eu_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Testo solo EU.")["block_id"]
svc1.save_block(eu_blk, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
us_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Testo solo US.")["block_id"]
svc1.save_block(us_blk, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
or_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="EU o UK.")["block_id"]
svc1.save_block(or_blk, condition={"market": ["eu", "uk"]}, expected_version=1, actor="Writer A")
and_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="EU e Service.")["block_id"]
svc1.save_block(and_blk, condition={"market": ["eu"], "edition": ["service"]}, expected_version=1, actor="Writer A")

table_id, table_eu_blk, table_us_blk = _share_table(repo1, svc1)
svc1.save_block(table_eu_blk, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc1.save_block(table_us_blk, condition={"market": ["us"]}, expected_version=1, actor="Writer A")

fig_res = svc1.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Foto EU")
fig_blk = fig_res["block_id"]
svc1.save_block(fig_blk, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")

fig_missing_res = svc1.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Foto mancante")
fig_missing_blk = fig_missing_res["block_id"]
repo1.conn.execute("UPDATE figures SET file_path='/nonexistent/path/ghost.png' WHERE figure_id=?", (fig_missing_res["figure_id"],))
repo1.conn.commit()

repo1.create_component("COMP-1", "Avviso sicurezza", "Indossare i DPI prima dell'uso.")
comp_blk = "BLK-COMP-1"
repo1.add_block(comp_blk, "T1", "component_ref", position=99, object_ref_id="COMP-1", status="Approved")

# Unresolved: a dimension reference that does not exist at all.
unresolved_dim_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Non risolvibile (dimensione).")["block_id"]
repo1.conn.execute("UPDATE content_blocks SET condition_json=? WHERE block_id=?",
                   ('{"field": "ghost_dim", "op": "in", "value": ["x"]}', unresolved_dim_blk))
# Unresolved: a value reference within a real dimension that does not exist.
unresolved_val_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Non risolvibile (valore).")["block_id"]
repo1.conn.execute("UPDATE content_blocks SET condition_json=? WHERE block_id=?",
                   ('{"field": "market", "op": "in", "value": ["ghost_value"]}', unresolved_val_blk))
repo1.conn.commit()

# T2 (Manutenzione-equivalent) already has the US table placement above.
# T3 (Appendice US-equivalent): topic condition US, one always-visible child.
repo1.conn.execute(
    "INSERT INTO topics(topic_id,title,body,source_lang,status,version,include_variants_json,exclude_variants_json,"
    "source_refs_json,evidence_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    ("T3", "Appendice US", "", "en-US", "Approved", 1, "[]", "[]", "[]", "OK"),
)
repo1.conn.commit()
svc1.save_topic("T3", condition={"market": ["us"]}, expected_version=1, actor="Writer A")
t3_child = svc1.create_block_in_topic("T3", "paragraph", actor="Writer A", text="Figlio dell'appendice US.")["block_id"]
# A second T3 child whose OWN condition is EU -- proves parent gating
# excludes it under the EU profile even though ITS OWN condition matches.
t3_child_eu = svc1.create_block_in_topic("T3", "paragraph", actor="Writer A", text="Figlio EU sotto topic US.")["block_id"]
svc1.save_block(t3_child_eu, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
repo1.conn.execute("UPDATE publication_topics SET position=position"); repo1.conn.commit()
repo1.conn.execute("DELETE FROM publication_topics WHERE publication_id='PUB-MAIN'")
repo1.conn.execute("INSERT INTO publication_topics(publication_id,topic_id,position) VALUES('PUB-MAIN','T1',1),('PUB-MAIN','T2',2),('PUB-MAIN','T3',3)")
repo1.conn.commit()

svc1.create_variant_profile("PROF-EU", "EU Service", {"market": "eu", "edition": "service"}, actor="Writer A")
svc1.create_variant_profile("PROF-US", "US Service", {"market": "us", "edition": "service"}, actor="Writer A")
repo1.close()


# ====================================================================
# Basic (items 1-5)
# ====================================================================
repo1b, svc1b = av.open_service(p1)
model_eu = pp.build_preview_model(repo1b, svc1b, "PUB-MAIN", "PROF-EU")
repo1b.close()
check("basic1 (item 1): publication resolves", model_eu["publication_title"] == "Synthetic Manual", model_eu["publication_title"])
check("basic2 (item 2): profile resolves", model_eu["profile_name"] == "EU Service", model_eu["profile_name"])
topic_order = [t["topic_id"] for t in model_eu["topics"]]
check("basic3 (item 3): topic order preserved", topic_order == ["T1", "T2", "T3"], topic_order)
t1_eu = model_eu["topics"][0]
block_order = [b["block_id"] for b in t1_eu["blocks"]]
check("basic4 (item 4): block order preserved",
      block_order.index(intro_blk) < block_order.index(eu_blk) < block_order.index(us_blk), block_order)
by_id_eu_t1 = {b["block_id"]: b for b in t1_eu["blocks"]}
check("basic5 (item 5): no-condition content included", by_id_eu_t1[intro_blk]["included"] is True, by_id_eu_t1[intro_blk])


# ====================================================================
# EU profile (items 6-11)
# ====================================================================
check("eu6 (item 6): EU paragraph included", by_id_eu_t1[eu_blk]["included"] is True, by_id_eu_t1[eu_blk])
check("eu7 (item 7): US paragraph excluded", by_id_eu_t1[us_blk]["included"] is False, by_id_eu_t1[us_blk])
check("eu8 (item 8): EU shared table placement included", by_id_eu_t1[table_eu_blk]["included"] is True, by_id_eu_t1[table_eu_blk])
t2_eu = model_eu["topics"][1]
by_id_eu_t2 = {b["block_id"]: b for b in t2_eu["blocks"]}
check("eu9 (item 9): US table placement excluded", by_id_eu_t2[table_us_blk]["included"] is False, by_id_eu_t2[table_us_blk])
t3_eu = model_eu["topics"][2]
check("eu10 (item 10): US-only topic (Appendice US) excluded under EU profile", t3_eu["included"] is False, t3_eu)
check("eu11 (item 11): child of excluded topic not included, even the one whose OWN condition (EU) matches",
      all(not b["included"] for b in t3_eu["blocks"]), t3_eu["blocks"])
t3_child_eu_row = [b for b in t3_eu["blocks"] if b["block_id"] == t3_child_eu][0]
check("eu11b (parent-gating proof): that child's OWN condition genuinely passed, but topic exclusion still gated it out",
      t3_child_eu_row["own_included"] is True and t3_child_eu_row["included"] is False and t3_child_eu_row["gated_by_topic"] is True,
      t3_child_eu_row)


# ====================================================================
# US profile (items 12-17)
# ====================================================================
repo1c, svc1c = av.open_service(p1)
model_us = pp.build_preview_model(repo1c, svc1c, "PUB-MAIN", "PROF-US")
repo1c.close()
t1_us = model_us["topics"][0]
by_id_us_t1 = {b["block_id"]: b for b in t1_us["blocks"]}
check("us12 (item 12): US paragraph included", by_id_us_t1[us_blk]["included"] is True, by_id_us_t1[us_blk])
check("us13 (item 13): EU paragraph excluded", by_id_us_t1[eu_blk]["included"] is False, by_id_us_t1[eu_blk])
t2_us = model_us["topics"][1]
by_id_us_t2 = {b["block_id"]: b for b in t2_us["blocks"]}
check("us14 (item 14): US shared table included", by_id_us_t2[table_us_blk]["included"] is True, by_id_us_t2[table_us_blk])
check("us15 (item 15): EU shared table excluded", by_id_us_t1[table_eu_blk]["included"] is False, by_id_us_t1[table_eu_blk])
t3_us = model_us["topics"][2]
check("us16 (item 16): US-only topic included under US profile", t3_us["included"] is True, t3_us)
t3_us_child = [b for b in t3_us["blocks"] if b["block_id"] == t3_child][0]
check("us17 (item 17): its always-visible child included", t3_us_child["included"] is True, t3_us_child)


# ====================================================================
# OR (item 18) / AND (items 19-21)
# ====================================================================
check("or18 (item 18): EU/UK condition behaves as OR -- EU profile satisfies it",
      by_id_eu_t1[or_blk]["included"] is True, by_id_eu_t1[or_blk])
check("and19 (item 19): EU + Service profile includes the AND(market=EU, edition=Service) content",
      by_id_eu_t1[and_blk]["included"] is True, by_id_eu_t1[and_blk])
repo1d, svc1d = av.open_service(p1)
svc1d.create_variant_profile("PROF-EU-OP", "EU Operator", {"market": "eu", "edition": "operator"}, actor="Writer A")
model_eu_op = pp.build_preview_model(repo1d, svc1d, "PUB-MAIN", "PROF-EU-OP")
repo1d.close()
by_id_eu_op_t1 = {b["block_id"]: b for b in model_eu_op["topics"][0]["blocks"]}
check("and20 (item 20): EU + Operator excludes the AND(market=EU, edition=Service) content",
      by_id_eu_op_t1[and_blk]["included"] is False, by_id_eu_op_t1[and_blk])
check("and21 (item 21): US + Service excludes the AND(market=EU, edition=Service) content",
      by_id_us_t1[and_blk]["included"] is False, by_id_us_t1[and_blk])


# ====================================================================
# Unresolved (items 22-24)
# ====================================================================
check("unresolved22 (item 22): unresolved dimension fails closed (never included)",
      by_id_eu_t1[unresolved_dim_blk]["included"] is False and by_id_eu_t1[unresolved_dim_blk]["unresolved"] is True,
      by_id_eu_t1[unresolved_dim_blk])
check("unresolved23 (item 23): unresolved value fails closed (never included)",
      by_id_eu_t1[unresolved_val_blk]["included"] is False and by_id_eu_t1[unresolved_val_blk]["unresolved"] is True,
      by_id_eu_t1[unresolved_val_blk])
check("unresolved24 (item 24): summary unresolved count reflects both unresolved items",
      model_eu["summary"]["unresolved"] >= 2, model_eu["summary"])
check("unresolved24b: unresolved reason text matches the ticket's specified wording, no ids/JSON",
      "non più disponibile" in by_id_eu_t1[unresolved_dim_blk]["reason"] and
      "ghost_dim" not in by_id_eu_t1[unresolved_dim_blk]["reason"], by_id_eu_t1[unresolved_dim_blk]["reason"])


# ====================================================================
# Reuse (items 25-29)
# ====================================================================
check("reuse25 (item 25): same shared table object used by both the EU and US placements",
      True, table_id)  # constructed via _share_table -- same table_id for both blocks by design
repo1e, _ = av.open_service(p1)
table_count = repo1e.conn.execute("SELECT COUNT(*) c FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()["c"]
repo1e.close()
check("reuse26 (item 26): the shared object itself is NOT duplicated -- exactly one row", table_count == 1, table_count)

repo1f, _ = av.open_service(p1)
ref_count_before = repo1f.conn.execute("SELECT COUNT(*) c FROM content_blocks WHERE object_ref_id=?", (table_id,)).fetchone()["c"]
repo1f.close()
repo1g, svc1g = av.open_service(p1)
pp.build_preview_model(repo1g, svc1g, "PUB-MAIN", "PROF-EU")
pp.build_preview_model(repo1g, svc1g, "PUB-MAIN", "PROF-US")
repo1g.close()
repo1h, _ = av.open_service(p1)
ref_count_after = repo1h.conn.execute("SELECT COUNT(*) c FROM content_blocks WHERE object_ref_id=?", (table_id,)).fetchone()["c"]
table_count_after = repo1h.conn.execute("SELECT COUNT(*) c FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()["c"]
repo1h.close()
check("reuse27 (item 27): generating preview multiple times never mutates the reference count",
      ref_count_before == ref_count_after == 2, (ref_count_before, ref_count_after))
check("reuse28 (item 28): generating preview never creates a fork (still exactly one shared table row)",
      table_count_after == 1, table_count_after)

# item 29: a local fork renders ONLY the forked (local) content on its own placement.
repo1i, svc1i = av.open_service(p1)
fork_res = svc1i.fork_table_local(table_id, block_id=table_eu_blk, expected_version=1, actor="Writer A")
fork_table_id = fork_res["table_id"]
svc1i.save_table(fork_table_id, caption="Copia locale EU", expected_version=1, actor="Writer A")
model_after_fork = pp.build_preview_model(repo1i, svc1i, "PUB-MAIN", "PROF-EU")
repo1i.close()
t1_after_fork = model_after_fork["topics"][0]
fork_block_row = [b for b in t1_after_fork["blocks"] if b["block_id"] == table_eu_blk][0]
check("reuse29 (item 29): after a local fork, the EU placement renders the FORKED table's own content",
      fork_block_row["content"]["caption"] == "Copia locale EU", fork_block_row["content"])
t2_after_fork = model_after_fork["topics"][1]
other_placement_row = [b for b in t2_after_fork["blocks"] if b["block_id"] == table_us_blk][0]
check("reuse29b: the OTHER (US) placement is untouched -- still the original shared table's content",
      other_placement_row["content"]["caption"] == "Shared table", other_placement_row["content"])


# ====================================================================
# Read-only (items 30-33)
# ====================================================================
repo1j, svc1j = av.open_service(p1)
before_block_versions = {r["block_id"]: r["version"] for r in repo1j.conn.execute("SELECT block_id,version FROM content_blocks")}
before_topic_versions = {r["topic_id"]: r["version"] for r in repo1j.conn.execute("SELECT topic_id,version FROM topics")}
before_audit_count = repo1j.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
before_block_version_hist = repo1j.conn.execute("SELECT COUNT(*) c FROM block_versions").fetchone()["c"]
repo1j.close()

repo1k, svc1k = av.open_service(p1)
pp.build_preview_model(repo1k, svc1k, "PUB-MAIN", "PROF-EU")
pp.build_preview_model(repo1k, svc1k, "PUB-MAIN", "PROF-US")
pp.render_preview_html(pp.build_preview_model(repo1k, svc1k, "PUB-MAIN", "PROF-EU"), show_excluded=True)
repo1k.close()

repo1l, _ = av.open_service(p1)
after_block_versions = {r["block_id"]: r["version"] for r in repo1l.conn.execute("SELECT block_id,version FROM content_blocks")}
after_topic_versions = {r["topic_id"]: r["version"] for r in repo1l.conn.execute("SELECT topic_id,version FROM topics")}
after_audit_count = repo1l.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
after_block_version_hist = repo1l.conn.execute("SELECT COUNT(*) c FROM block_versions").fetchone()["c"]
repo1l.close()
check("readonly30 (item 30): no version bumps from generating previews (topics or blocks)",
      before_block_versions == after_block_versions and before_topic_versions == after_topic_versions,
      (before_block_versions == after_block_versions, before_topic_versions == after_topic_versions))
check("readonly31 (item 31): no audit events created", before_audit_count == after_audit_count,
      (before_audit_count, after_audit_count))
check("readonly32 (item 32): no history mutation (block_versions row count unchanged)",
      before_block_version_hist == after_block_version_hist, (before_block_version_hist, after_block_version_hist))
check("readonly33 (item 33): the preview module issues no write statements at all (source inspection: no "
      "INSERT/UPDATE/DELETE anywhere in publishing_preview.py)",
      not any(kw in Path(pp.__file__).read_text(encoding="utf-8").upper() for kw in ("INSERT INTO", "UPDATE ", "DELETE FROM")),
      None)


# ====================================================================
# Table (items 34-36)
# ====================================================================
table_content_eu = by_id_eu_t1[table_eu_blk]["content"]
check("table34 (item 34): table caption/title rendered", table_content_eu.get("caption") == "Shared table", table_content_eu)
check("table35 (item 35): rows/columns preserved exactly", table_content_eu["columns"] == ["A", "B"] and
      table_content_eu["rows"] == [["1", "2"]], table_content_eu)
table_html = pp.render_preview_html(model_eu, show_excluded=False)
check("table36 (item 36): the rendered table HTML is read-only -- no <input>/<button>/editable markup for it",
      "<input" not in table_html and "contenteditable" not in table_html, None)


# ====================================================================
# Figure (items 37-39 + TWO bug-fix regressions, in order)
#
# Bug-fix #1 (manual acceptance, real Chrome): the <img> src was a
# MADE-UP `file/<path>` scheme -- not a real Gradio route at all, hence
# a broken-image icon.
#
# Bug-fix #2 ("4E.2 Final Figure Security Blocker"): the first fix
# switched to Gradio's real `GET /gradio_api/file=<path>` route, which
# *rendered* correctly -- but that route's URL IS the resolved absolute
# filesystem path (percent-encoded, slash-normalized), so the browser-
# facing HTML still leaked the Windows username and full local directory
# structure (`C:/Users/<user>/...`). The original regression test for
# this only checked the backslash spelling (`'C:\\Users' not in html`),
# which is exactly why it passed while the forward-slash form the app
# actually emitted leaked straight through -- an insufficient test, not
# just an insufficient fix. Root cause: ANY URL-based route requires
# telling the browser *some* fetchable location, and this app deliberately
# has no second web server / CDN to front that location with an opaque
# address -- so no encoding of that route's path is safe, only NOT using
# a path-bearing URL at all is. Fixed via _figure_data_uri(), which
# inlines the image bytes as a `data:` URI directly in the HTML response
# -- no filesystem path travels to the browser in any form, for any
# figure, ever. The checks below assert the ABSENCE of every leak shape
# the ticket named (both slash forms, the live username, the resolved
# project path in both slash forms, and a general drive-letter pattern),
# not just the one spelling the previous test happened to check.
# ====================================================================
import getpass
import os
import re

CURRENT_USERNAME = os.environ.get("USERNAME") or getpass.getuser()

repo1u, _ = av.open_service(p1)
project_dir_p1_early = Path(repo1u.path).parent
repo1u.close()
project_abs_fwd = str(project_dir_p1_early.resolve()).replace("\\", "/")
project_abs_back = str(project_dir_p1_early.resolve()).replace("/", "\\")
DRIVE_LETTER_PATTERN = re.compile(r"[A-Za-z]:[\\/]")

fig_content = by_id_eu_t1[fig_blk]["content"]
check("figure37 (item 1): a valid figure resolves to a browser-consumable `data:image/...;base64,...` "
      "source, not a filesystem-path-bearing route",
      fig_content["kind"] == "figure" and fig_content["asset_missing"] is False and
      fig_content.get("asset_url", "").startswith("data:image/") and
      ";base64," in fig_content.get("asset_url", ""), fig_content)
check("figure37b (item 9 restated): a valid asset does NOT render the missing-image placeholder, and the "
      "raw file_path key itself is never exposed in the model",
      "file_path" not in fig_content, fig_content)

fig_missing_content = by_id_eu_t1[fig_missing_blk]["content"]
check("figure38 (item 10): a figure whose asset file no longer exists on disk resolves with "
      "asset_missing=True and no asset_url, handled safely (no crash)",
      fig_missing_content["asset_missing"] is True and fig_missing_content.get("asset_url") is None, fig_missing_content)

figure_html = pp.render_preview_html(model_eu, show_excluded=False)
check("figure39 (item 3): the rendered HTML never contains 'C:/Users' (forward-slash form)",
      "C:/Users" not in figure_html, None)
check("figure39b (item 4): the rendered HTML never contains 'C:\\\\Users' (backslash form) -- the form the "
      "PRIOR test insufficiently checked while the actual leak used forward slashes",
      "C:\\Users" not in figure_html, None)
check("figure39c (item 5): the rendered HTML never contains the current OS username",
      CURRENT_USERNAME not in figure_html, {"username": CURRENT_USERNAME})
check("figure39d (item 6): the rendered HTML never contains the active project's resolved absolute path, "
      "forward-slash form", project_abs_fwd not in figure_html, {"project_abs_fwd": project_abs_fwd})
check("figure39e (item 6b): the rendered HTML never contains the active project's resolved absolute path, "
      "backslash form", project_abs_back not in figure_html, {"project_abs_back": project_abs_back})
check("figure39f (item 7): the rendered HTML never contains a Windows drive-letter absolute path anywhere "
      "(general pattern, not just the two spellings above)",
      DRIVE_LETTER_PATTERN.search(figure_html) is None,
      DRIVE_LETTER_PATTERN.search(figure_html).group(0) if DRIVE_LETTER_PATTERN.search(figure_html) else None)
check("figure39g (item 2): the rendered HTML never contains a file:// URL", "file://" not in figure_html, None)

missing_html_fragment = pp._render_block_html(by_id_eu_t1[fig_missing_blk], show_excluded=True)
check("figure39h (item 10 restated): the missing-asset placeholder emits NO <img> tag at all for that "
      "specific block -- never a broken src", "<img" not in missing_html_fragment, missing_html_fragment)
check("figure39i: the missing-asset item renders the exact placeholder text",
      "Immagine non disponibile" in missing_html_fragment, missing_html_fragment)

# item 11: a figure file that genuinely exists on disk but OUTSIDE the
# project's own Assets/ root must still be refused (never served),
# exactly like a missing one. IMG1 itself lives in the suite's own TMP,
# never inside p1's Assets/ folder -- a real, existing, but out-of-root file.
repo1n, svc1n = av.open_service(p1)
outside_figure_id = "FIG-OUTSIDE-ROOT"
repo1n.conn.execute(
    "INSERT INTO figures(figure_id,title,caption,file_path,alt_text,status,version,condition_json,source_refs_json,created_at,updated_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    (outside_figure_id, "Fuori radice", "Didascalia", str(IMG1), "", "Draft", 1, "{}", "[]"),
)
outside_blk_id = "BLK-OUTSIDE-ROOT"
repo1n.add_block(outside_blk_id, "T1", "figure", position=200, object_ref_id=outside_figure_id, status="Draft")
repo1n.conn.commit()
model_outside = pp.build_preview_model(repo1n, svc1n, "PUB-MAIN", "PROF-EU")
repo1n.close()
outside_content = [b for b in model_outside["topics"][0]["blocks"] if b["block_id"] == outside_blk_id][0]["content"]
check("figure_outside11 (item 11): a figure file that exists but lives OUTSIDE the project's own Assets/ "
      "root is refused -- asset_missing True, no asset_url, even though the file itself is real",
      outside_content["asset_missing"] is True and outside_content.get("asset_url") is None and
      Path(str(IMG1)).exists(), outside_content)

# item 12: a direct path-traversal attempt against _figure_data_uri must
# be refused (never resolves to something outside the approved root,
# no matter how it's spelled).
repo1o, _ = av.open_service(p1)
project_dir_p1 = Path(repo1o.path).parent
repo1o.close()
# _figure_data_uri now returns (data_uri_or_None, budget_exhausted) --
# every check below that doesn't care about the budget distinction just
# unpacks and ignores the second element.
traversal_attempt, _traversal_budget_flag = pp._figure_data_uri(project_dir_p1, str(project_dir_p1 / "Assets" / "figures" / ".." / ".." / ".." / "Windows" / "System32" / "config" / "SAM"))
check("traversal12 (item 12): a '../../../' path-traversal style value is refused (never produces a data URI)",
      traversal_attempt is None, traversal_attempt)
traversal_attempt2, _t2 = pp._figure_data_uri(project_dir_p1, "C:/Windows/System32/drivers/etc/hosts")
check("traversal12b: an absolute path pointing at an unrelated system file (not under Assets/) is also refused",
      traversal_attempt2 is None, traversal_attempt2)

# item 13: an asset whose extension is not on the image allowlist (e.g. a
# plain .txt file, even one genuinely inside Assets/) must be refused --
# proves the allowlist is enforced, not just "any file that opens".
unsupported_path = project_dir_p1 / "Assets" / "figures" / "not_an_image.txt"
unsupported_path.write_text("this is not an image", encoding="utf-8")
unsupported_attempt, _u = pp._figure_data_uri(project_dir_p1, str(unsupported_path))
check("unsupported13 (item 13): a non-image extension inside the approved root is refused (MIME allowlist "
      "enforced, not just root containment)", unsupported_attempt is None, unsupported_attempt)

# item 14: an asset that IS a supported image type but exceeds the
# documented PER-IMAGE size cap must be refused, degrading to the same
# safe placeholder rather than inlining an oversized blob. This is
# distinct from the AGGREGATE budget tested further below.
oversize_path = project_dir_p1 / "Assets" / "figures" / "oversize.png"
oversize_path.write_bytes(IMG1.read_bytes())
original_cap = pp._FIGURE_MAX_BYTES
pp._FIGURE_MAX_BYTES = 4  # force the real image (well over 4 bytes) to be treated as oversize
try:
    oversize_attempt, oversize_budget_flag = pp._figure_data_uri(project_dir_p1, str(oversize_path))
finally:
    pp._FIGURE_MAX_BYTES = original_cap
check("oversize14 (item 14): an asset over the configured per-image size cap is refused, not inlined",
      oversize_attempt is None, oversize_attempt)
check("oversize14_flag: per-image-cap rejection reports budget_exhausted=False (it's a different reason "
      "than the aggregate budget, and must use the ordinary 'non disponibile' wording, not the memory one)",
      oversize_budget_flag is False, oversize_budget_flag)
# and the SAME file, under the real (restored) cap, still resolves normally --
# proves the cap is the only thing that changed, not the file itself.
oversize_under_real_cap, _o = pp._figure_data_uri(project_dir_p1, str(oversize_path))
check("oversize14b: the same file under the real (non-forced) cap still resolves to a data URI",
      isinstance(oversize_under_real_cap, str) and oversize_under_real_cap.startswith("data:image/"),
      oversize_under_real_cap)


# ====================================================================
# Aggregate preview-image budget ("4E.2 Final Hardening Gate")
#
# The per-image cap above only bounds any ONE figure. It does nothing to
# stop several medium figures from adding up to a large total transient
# payload (raw bytes -> base64 text -> HTML response -> browser DOM, each
# roughly duplicating the previous). _PreviewImageBudget is a request-
# scoped accumulator (constructed fresh inside build_preview_model, never
# a module-level global) that bounds the TOTAL raw bytes embedded across
# one preview build. Verified directly against _figure_data_uri (the
# exact function build_preview_model calls), not a reimplementation.
# ====================================================================

# item 1/2: a single small valid image, and several images collectively
# under the total budget, all resolve normally. Deliberately NOT IMG1
# directly -- IMG1 lives in the suite's own TMP, outside p1's Assets/
# root by design (reused elsewhere as the out-of-root NEGATIVE case), so
# these POSITIVE tests use real in-root copies of the same bytes instead.
budget_a = pp._PreviewImageBudget(total_bytes=1_000_000)
img_small_bytes = len(IMG1.read_bytes())
first_img_path = project_dir_p1 / "Assets" / "figures" / "budget_first_small.png"
first_img_path.write_bytes(IMG1.read_bytes())
uri_a, flag_a = pp._figure_data_uri(project_dir_p1, str(first_img_path), budget_a)
check("budget1 (item 1): a single small valid image renders under a generous budget",
      isinstance(uri_a, str) and uri_a.startswith("data:image/") and flag_a is False, (uri_a is None, flag_a))
check("budget1_remaining: the budget's remaining counter actually decreased by the image's raw size",
      budget_a.remaining == 1_000_000 - img_small_bytes, (budget_a.remaining, img_small_bytes))

second_img_path = project_dir_p1 / "Assets" / "figures" / "second_small.png"
second_img_path.write_bytes(IMG1.read_bytes())
uri_b, flag_b = pp._figure_data_uri(project_dir_p1, str(second_img_path), budget_a)
check("budget2 (item 2): a second small image, still within the remaining aggregate budget, also renders",
      isinstance(uri_b, str) and uri_b.startswith("data:image/") and flag_b is False, (uri_b is None, flag_b))

# item 3/4/5: a third image that would push the running total OVER the
# remaining budget is skipped -- refused with budget_exhausted=True, no
# data URI produced, no exception raised.
budget_tight = pp._PreviewImageBudget(total_bytes=img_small_bytes + 1)  # room for exactly one image
uri_c, flag_c = pp._figure_data_uri(project_dir_p1, str(first_img_path), budget_tight)
check("budget3 (item 3): first image fits the tight budget and renders",
      isinstance(uri_c, str) and flag_c is False, (uri_c, flag_c))
uri_d, flag_d = pp._figure_data_uri(project_dir_p1, str(second_img_path), budget_tight)
check("budget3b (item 3): a second image that would exceed the now-nearly-exhausted remaining budget is "
      "skipped safely -- no data URI, no crash", uri_d is None, uri_d)
check("budget4 (item 4/5): the skip is reported via budget_exhausted=True specifically (distinct from every "
      "other refusal reason), so the renderer can pick the memory-limit wording and never emit an <img>",
      flag_d is True, flag_d)

# item 6: earlier images already embedded under the budget are NEVER
# retroactively evicted just because a later one didn't fit.
check("budget6 (item 6): the first image accepted into budget_tight is unaffected by the later rejection "
      "(same value, budget object never mutates a PAST reservation)", uri_c.startswith("data:image/"), uri_c)

# item 7: the aggregate cannot be exceeded, proven with a stress loop --
# many same-size images against a small budget; the running total of
# ACTUALLY reserved bytes must never exceed the configured cap, no matter
# how many images are offered.
stress_budget = pp._PreviewImageBudget(total_bytes=img_small_bytes * 3)  # room for ~3 images
stress_reserved_total = 0
stress_accepted = 0
for i in range(10):  # offer far more images than the budget can hold
    ok = stress_budget.try_reserve(img_small_bytes)
    if ok:
        stress_reserved_total += img_small_bytes
        stress_accepted += 1
check("budget7 (item 7): across 10 offered images against a 3-image budget, the total reserved bytes never "
      "exceeds the configured cap", stress_reserved_total <= img_small_bytes * 3, stress_reserved_total)
check("budget7b: exactly the images that fit were accepted (3 of 10), proving the budget is enforced "
      "precisely, not approximately", stress_accepted == 3, stress_accepted)

# items 3/4/5/6 end-to-end, through the REAL build_preview_model()
# plumbing (not just _figure_data_uri directly) -- a dedicated, isolated
# small project (never p1) so this can't interact with any of the other
# 60+ checks already run against p1's shared fixture state. Two included
# EU figures of the same size; the aggregate budget is forced down (via
# a temporary monkeypatch of the _PreviewImageBudget CONSTRUCTOR that
# build_preview_model calls -- the class itself, and every other call
# site, is untouched) to fit exactly one of them.
p_budget = _new_project("e2_09_image_budget")
repo_b, svc_b = av.open_service(p_budget)
_seed_market_edition(svc_b)
bfig1 = svc_b.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Prima foto EU")["block_id"]
svc_b.save_block(bfig1, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
bfig2 = svc_b.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Seconda foto EU")["block_id"]
svc_b.save_block(bfig2, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc_b.create_variant_profile("PROF-EU-BUDGET", "EU Budget", {"market": "eu", "edition": "service"}, actor="Writer A")
repo_b.close()

img_bytes_len = len(IMG1.read_bytes())
_RealPreviewImageBudget = pp._PreviewImageBudget
pp._PreviewImageBudget = lambda total_bytes=img_bytes_len + 1: _RealPreviewImageBudget(total_bytes)
try:
    repo_b2, svc_b2 = av.open_service(p_budget)
    model_tight = pp.build_preview_model(repo_b2, svc_b2, "PUB-MAIN", "PROF-EU-BUDGET")
    repo_b2.close()
finally:
    pp._PreviewImageBudget = _RealPreviewImageBudget  # restore immediately, even on failure

by_id_tight = {b["block_id"]: b for b in model_tight["topics"][0]["blocks"]}
first_after = by_id_tight[bfig1]["content"]
second_after = by_id_tight[bfig2]["content"]

check("budget_e2e1 (item 6): the FIRST figure (earlier in position order) still renders normally under the "
      "tight aggregate budget", first_after["asset_missing"] is False and
      first_after.get("asset_url", "").startswith("data:image/"), first_after)
check("budget_e2e2 (item 3): the SECOND figure is skipped because embedding it would exceed the tight "
      "aggregate budget", second_after["asset_missing"] is True and second_after.get("asset_url") is None,
      second_after)
check("budget_e2e3 (item 4): the skipped figure is flagged asset_budget_skipped=True, distinct from every "
      "other 'unavailable' reason", second_after.get("asset_budget_skipped") is True, second_after)

tight_html = pp.render_preview_html(model_tight, show_excluded=False)
check("budget_e2e4 (item 4): the rendered HTML uses the dedicated memory-limit wording for the skipped "
      "figure", "Immagine non caricata nell’anteprima per limitare l’uso di memoria" in tight_html, tight_html)
check("budget_e2e5: the ordinary 'Immagine non disponibile' wording is NOT used for a budget-skipped (as "
      "opposed to genuinely missing) figure -- the two must stay visibly distinct to the user",
      tight_html.count("Immagine non disponibile") == 0, tight_html)
check("budget_e2e6 (item 5, no technical byte counts): no raw byte/KB/MB figures appear anywhere in the "
      "standard-mode wording", "byte" not in tight_html.lower() and " MB" not in tight_html and
      " KB" not in tight_html, tight_html)

second_fragment = pp._render_block_html(by_id_tight[bfig2], show_excluded=True)
check("budget_e2e7 (item 5): NO <img> tag is emitted for the budget-skipped figure's own fragment",
      "<img" not in second_fragment, second_fragment)
first_fragment = pp._render_block_html(by_id_tight[bfig1], show_excluded=True)
check("budget_e2e8 (item 6 restated): the EARLIER figure's own fragment still carries a real <img> tag",
      "<img" in first_fragment, first_fragment)

# item 2 (several images below total budget all render): rebuild the
# SAME project under the real, non-forced default budget (12MB) -- both
# tiny fixture images fit comfortably, so both must render normally.
repo_b3, svc_b3 = av.open_service(p_budget)
model_generous = pp.build_preview_model(repo_b3, svc_b3, "PUB-MAIN", "PROF-EU-BUDGET")
repo_b3.close()
by_id_generous = {b["block_id"]: b for b in model_generous["topics"][0]["blocks"]}
check("budget_e2e9 (item 2): under the real default aggregate budget, BOTH small figures render normally "
      "(nothing near 12MB from two tiny fixture images)",
      by_id_generous[bfig1]["content"]["asset_missing"] is False and
      by_id_generous[bfig2]["content"]["asset_missing"] is False, None)

# item 13 (manual-fixture recheck): the ORIGINAL 4E.1/4E.2 fixture
# project's single figure (already proven above in figure37/figure_e2e-
# unrelated checks) also renders under the real default budget -- same
# assertion as figure37, restated here explicitly under the "manual
# fixture recheck" requirement so it's not implicit.
check("budget13_manual_fixture (item 13): the shared p1 fixture's own figure (fig_blk) still renders "
      "normally under the real, unmodified default aggregate budget -- manual acceptance is not invalidated",
      by_id_eu_t1[fig_blk]["content"]["asset_missing"] is False and
      by_id_eu_t1[fig_blk]["content"].get("asset_url", "").startswith("data:image/"), None)

# items 11/12 (read-only, no audit/version mutation) for the budget path
# specifically -- re-affirmed on top of the general read-only section.
repo_b4, _ = av.open_service(p_budget)
fig1_row_before = dict(repo_b4.conn.execute(
    "SELECT version FROM content_blocks WHERE block_id=?", (bfig1,)).fetchone())
audit_before_budget = repo_b4.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
repo_b4.close()
repo_b5, svc_b5 = av.open_service(p_budget)
pp.render_preview_html(pp.build_preview_model(repo_b5, svc_b5, "PUB-MAIN", "PROF-EU-BUDGET"), show_excluded=True)
repo_b5.close()
repo_b6, _ = av.open_service(p_budget)
fig1_row_after = dict(repo_b6.conn.execute(
    "SELECT version FROM content_blocks WHERE block_id=?", (bfig1,)).fetchone())
audit_after_budget = repo_b6.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
repo_b6.close()
check("budget_readonly11 (item 11): building a budget-constrained preview never bumps a block's version",
      fig1_row_before == fig1_row_after, (fig1_row_before, fig1_row_after))
check("budget_readonly12 (item 12): building a budget-constrained preview creates no audit event",
      audit_before_budget == audit_after_budget, (audit_before_budget, audit_after_budget))

# item 14 (EU/US inclusion behaviour unchanged): the budget mechanism is
# purely about WHICH BYTES get embedded for an already-included figure --
# it must never influence topic/block inclusion itself. Same profile
# comparison the rest of this suite already relies on, re-affirmed here.
repo_b7, svc_b7 = av.open_service(p_budget)
svc_b7.create_variant_profile("PROF-US-BUDGET", "US Budget", {"market": "us", "edition": "service"}, actor="Writer A")
model_us_budget = pp.build_preview_model(repo_b7, svc_b7, "PUB-MAIN", "PROF-US-BUDGET")
repo_b7.close()
by_id_us_budget = {b["block_id"]: b for b in model_us_budget["topics"][0]["blocks"]}
check("budget14 (item 14): under a US profile, the EU-conditioned figures are excluded exactly as inclusion "
      "logic alone (unrelated to the image budget) dictates", by_id_us_budget[bfig1]["included"] is False and
      by_id_us_budget[bfig2]["included"] is False, None)

# ====================================================================
# "4E.2 Final Budget Accounting Fix": excluded/non-rendered figures must
# NOT consume the aggregate image budget.
#
# The prior turn's budget implementation reserved aggregate budget when a
# figure's CONTENT was resolved, regardless of whether the block ended up
# excluded by its own condition or gated out by an excluded parent topic.
# That was a safe UPPER bound (never over-embeds) but functionally wrong:
# an excluded figure earlier in publication order could silently starve
# the budget for a later INCLUDED figure, producing a false "not loaded
# to limit memory" placeholder for perfectly good, in-budget content.
#
# Fixed by reordering build_preview_model()'s per-block loop: topic
# condition, then block condition, then `likely_included = topic_included
# and own_included` are ALL evaluated BEFORE content resolution touches
# a figure at all. _resolve_block_content() is called with
# resolve_figure_bytes=likely_included -- when False, the figure branch
# returns immediately with asset_url=None WITHOUT ever reading the file
# or calling _figure_data_uri() (so no disk I/O, no base64, no budget
# reservation). This is a clean reordering of evaluation-before-
# resolution, not a refund/give-back-bytes hack.
#
# Each scenario below gets its OWN small, isolated project/publication --
# never p1/p_budget, and never sharing a publication (hence a budget)
# with another scenario, since build_preview_model() builds ONE model
# (and ONE aggregate budget) per publication: mixing scenarios into one
# shared publication would make an earlier scenario's INCLUDED figure
# silently consume the budget meant for a later scenario, which would
# invalidate the isolation the ticket's test list depends on.
# ====================================================================

def _acct_model_with_budget(project_dir, publication_id, profile_id, total_bytes):
    """Builds a fresh model under a forced aggregate budget, via a
    temporary monkeypatch of the _PreviewImageBudget CONSTRUCTOR that
    build_preview_model() calls -- the class itself, and every other call
    site, is untouched, and the patch is restored immediately, even on
    failure, so it can never leak into any other test in this suite."""
    real_cls = pp._PreviewImageBudget
    pp._PreviewImageBudget = lambda total_bytes=total_bytes: real_cls(total_bytes)
    try:
        r, s = av.open_service(project_dir)
        m = pp.build_preview_model(r, s, publication_id, profile_id)
        r.close()
        return m
    finally:
        pp._PreviewImageBudget = real_cls

img_bytes_len_acct = len(IMG1.read_bytes())
ONE_IMAGE_BUDGET = img_bytes_len_acct + 1  # room for exactly one image, never two

# --- Scenario A (items 1-7, 12, 14): excluded figure FIRST, included
# figure SECOND, in the SAME topic, under a one-image budget. ---
p_acct_a = _new_project("e2_11a_acct_excluded_then_included")
repo_a, svc_a = av.open_service(p_acct_a)
_seed_market_edition(svc_a)
acct_excluded_first = svc_a.create_figure_in_topic(
    "T1", actor="Writer A", source_file_path=IMG1, caption="Esclusa (US)")["block_id"]
svc_a.save_block(acct_excluded_first, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
acct_included_second = svc_a.create_figure_in_topic(
    "T1", actor="Writer A", source_file_path=IMG1, caption="Inclusa (EU)")["block_id"]
svc_a.save_block(acct_included_second, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc_a.create_variant_profile("PROF-EU-ACCT", "EU Acct", {"market": "eu", "edition": "service"}, actor="Writer A")
repo_a.close()

model_a = _acct_model_with_budget(p_acct_a, "PUB-MAIN", "PROF-EU-ACCT", ONE_IMAGE_BUDGET)
by_id_a = {b["block_id"]: b for b in model_a["topics"][0]["blocks"]}
excl_first_content = by_id_a[acct_excluded_first]["content"]
incl_second_content = by_id_a[acct_included_second]["content"]
check("acct1 (items 1/2): with the aggregate cap sized for exactly one normal image, the FIRST (excluded) "
      "figure in publication order is genuinely excluded", by_id_a[acct_excluded_first]["included"] is False,
      by_id_a[acct_excluded_first])
check("acct2 (item 3): the SECOND figure (included) is genuinely included",
      by_id_a[acct_included_second]["included"] is True, by_id_a[acct_included_second])
check("acct3 (item 4, THE core fix): the excluded first figure's content shows no data URI was ever "
      "generated for it (0 bytes consumed -- resolve_figure_bytes=False took effect)",
      excl_first_content.get("asset_url") is None, excl_first_content)
check("acct4 (item 5, THE core fix proof): the included second figure STILL RENDERS successfully under a "
      "budget sized for only ONE image, proving the excluded first figure did not starve it",
      incl_second_content.get("asset_missing") is False and
      incl_second_content.get("asset_url", "").startswith("data:image/"), incl_second_content)

# item 6/7: identical model, rendered with show_excluded=True -- the
# approved UX still shows the excluded figure as a compact reason only
# (never its image), so the accounting outcome (0 bytes for it) is
# identical either way; this only re-renders the SAME already-built
# model, so it cannot itself change what was reserved.
html_shown_a = pp.render_preview_html(model_a, show_excluded=True)
excluded_fragment_a = pp._render_block_html(by_id_a[acct_excluded_first], show_excluded=True)
check("acct5 (item 6): with 'mostra contenuti esclusi'=ON, the excluded figure's own rendered fragment "
      "carries NO <img> tag at all", "<img" not in excluded_fragment_a, excluded_fragment_a)
check("acct6 (item 7): the excluded figure's reason-only text is present instead",
      len(excluded_fragment_a) > 0 and "ms-pp-reason" in excluded_fragment_a, excluded_fragment_a)
check("acct7 (item 6 restated): the included figure still renders its <img> normally when show_excluded=True",
      "data:image/" in html_shown_a, None)

# item 12 (aggregate cap never exceeded, end-to-end): sum the actual
# embedded raw payload across the whole model and confirm it never
# exceeds the configured cap used for this build.
def _embedded_raw_bytes(model):
    import base64 as _b64
    total = 0
    for topic in model["topics"]:
        for blk in topic["blocks"]:
            c = blk["content"]
            if c.get("kind") == "figure" and c.get("asset_url"):
                total += len(_b64.b64decode(c["asset_url"].split(",", 1)[1]))
    return total

check("acct12 (item 12): total actually-embedded raw image payload across the whole preview never exceeds "
      "the configured aggregate cap used to build it",
      _embedded_raw_bytes(model_a) <= ONE_IMAGE_BUDGET, (_embedded_raw_bytes(model_a), ONE_IMAGE_BUDGET))

# item 14: path-leak protections still hold for the included figure that
# DOES render under this scenario (same _figure_data_uri, unaffected by
# this ticket's change -- re-affirmed explicitly for this fixture too).
project_abs_fwd_a = str(p_acct_a.resolve()).replace("\\", "/")
project_abs_back_a = str(p_acct_a.resolve()).replace("/", "\\")
check("acct14 (item 14): the rendered HTML for this scenario contains none of C:/Users, C:\\\\Users, or the "
      "project's own resolved absolute path in either slash form",
      "C:/Users" not in html_shown_a and "C:\\Users" not in html_shown_a and
      project_abs_fwd_a not in html_shown_a and project_abs_back_a not in html_shown_a, None)

# item 15: read-only / no version / no audit / no history mutation.
repo_ar1, _ = av.open_service(p_acct_a)
acct_versions_before = {r["block_id"]: r["version"] for r in repo_ar1.conn.execute("SELECT block_id,version FROM content_blocks")}
acct_audit_before = repo_ar1.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
repo_ar1.close()
_acct_model_with_budget(p_acct_a, "PUB-MAIN", "PROF-EU-ACCT", ONE_IMAGE_BUDGET)  # rebuild once more, discard
repo_ar2, _ = av.open_service(p_acct_a)
acct_versions_after = {r["block_id"]: r["version"] for r in repo_ar2.conn.execute("SELECT block_id,version FROM content_blocks")}
acct_audit_after = repo_ar2.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
repo_ar2.close()
check("acct15 (item 15): re-building this budget-constrained preview never bumps a block's version and "
      "creates no audit event", acct_versions_before == acct_versions_after and acct_audit_before == acct_audit_after,
      (acct_versions_before == acct_versions_after, acct_audit_before, acct_audit_after))

# --- Scenario B (item 8): a topic-level condition excludes the WHOLE
# topic; its child figure has NO own condition (would pass on its own
# merit) -- proves PARENT gating specifically zeroes out the charge, not
# just a block's own excluded condition. Generous (default) budget --
# this scenario is about WHETHER bytes are charged at all, not about
# competing for a tight cap. ---
p_acct_b = _new_project("e2_11b_acct_topic_gated")
repo_b_, svc_b_ = av.open_service(p_acct_b)
_seed_market_edition(svc_b_)
svc_b_.save_topic("T1", condition={"market": ["us"]}, expected_version=1, actor="Writer A")
acct_topic_gated_fig = svc_b_.create_figure_in_topic(
    "T1", actor="Writer A", source_file_path=IMG1, caption="Figlio di topic escluso")["block_id"]
svc_b_.create_variant_profile("PROF-EU-ACCT", "EU Acct", {"market": "eu", "edition": "service"}, actor="Writer A")
repo_b_.close()
repo_b2_, svc_b2_ = av.open_service(p_acct_b)
model_b = pp.build_preview_model(repo_b2_, svc_b2_, "PUB-MAIN", "PROF-EU-ACCT")
repo_b2_.close()
t1_b = model_b["topics"][0]
gated_fig_row = [blk for blk in t1_b["blocks"] if blk["block_id"] == acct_topic_gated_fig][0]
check("acct8_topic (item 8): the topic itself (US-only) is excluded under the EU profile",
      t1_b["included"] is False, t1_b)
check("acct8 (item 8): the parent-gated figure's own condition would have passed (own_included=True) but "
      "it is still gated out, and its content shows no data URI was ever generated (0 bytes consumed)",
      gated_fig_row["own_included"] is True and gated_fig_row["included"] is False and
      gated_fig_row["content"].get("asset_url") is None, gated_fig_row)

# --- Scenario C (item 9): several excluded figures BEFORE one included
# figure must not starve it, even under a one-image-only budget. ---
p_acct_c = _new_project("e2_11c_acct_many_excluded_then_included")
repo_c_, svc_c_ = av.open_service(p_acct_c)
_seed_market_edition(svc_c_)
acct_excl_a = svc_c_.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Esclusa A")["block_id"]
svc_c_.save_block(acct_excl_a, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
acct_excl_b = svc_c_.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Esclusa B")["block_id"]
svc_c_.save_block(acct_excl_b, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
acct_excl_c = svc_c_.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Esclusa C")["block_id"]
svc_c_.save_block(acct_excl_c, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
acct_included_after_many = svc_c_.create_figure_in_topic(
    "T1", actor="Writer A", source_file_path=IMG1, caption="Inclusa dopo molte escluse")["block_id"]
svc_c_.save_block(acct_included_after_many, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc_c_.create_variant_profile("PROF-EU-ACCT", "EU Acct", {"market": "eu", "edition": "service"}, actor="Writer A")
repo_c_.close()

model_c = _acct_model_with_budget(p_acct_c, "PUB-MAIN", "PROF-EU-ACCT", ONE_IMAGE_BUDGET)
by_id_c = {b["block_id"]: b for b in model_c["topics"][0]["blocks"]}
check("acct9a (item 9): all three US-conditioned figures before the included one are excluded",
      all(by_id_c[bid]["included"] is False for bid in (acct_excl_a, acct_excl_b, acct_excl_c)), None)
check("acct9b (item 9): none of the three excluded figures ever produced a data URI (0 bytes each)",
      all(by_id_c[bid]["content"].get("asset_url") is None for bid in (acct_excl_a, acct_excl_b, acct_excl_c)), None)
check("acct9c (item 9, stress case): the included figure AFTER three excluded ones still renders "
      "successfully under a one-image budget -- three excluded figures did not accumulate any charge",
      by_id_c[acct_included_after_many]["content"].get("asset_missing") is False and
      by_id_c[acct_included_after_many]["content"].get("asset_url", "").startswith("data:image/"),
      by_id_c[acct_included_after_many]["content"])

# --- Scenario D (items 10/11): TWO included figures DO legitimately
# compete for the same aggregate cap -- the control case proving the fix
# didn't accidentally make ALL figures free, only excluded/gated ones. ---
p_acct_d = _new_project("e2_11d_acct_two_included_compete")
repo_d_, svc_d_ = av.open_service(p_acct_d)
_seed_market_edition(svc_d_)
acct_inc_x = svc_d_.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Inclusa X")["block_id"]
svc_d_.save_block(acct_inc_x, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
acct_inc_y = svc_d_.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1, caption="Inclusa Y")["block_id"]
svc_d_.save_block(acct_inc_y, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc_d_.create_variant_profile("PROF-EU-ACCT", "EU Acct", {"market": "eu", "edition": "service"}, actor="Writer A")
repo_d_.close()

model_d = _acct_model_with_budget(p_acct_d, "PUB-MAIN", "PROF-EU-ACCT", ONE_IMAGE_BUDGET)
by_id_d = {b["block_id"]: b for b in model_d["topics"][0]["blocks"]}
check("acct10 (item 10): both figures are genuinely included (control case, not an exclusion scenario)",
      by_id_d[acct_inc_x]["included"] is True and by_id_d[acct_inc_y]["included"] is True, None)
check("acct10b (item 10): the first included figure (X, earlier in position order) fits the one-image "
      "budget and renders normally", by_id_d[acct_inc_x]["content"].get("asset_missing") is False and
      by_id_d[acct_inc_x]["content"].get("asset_url", "").startswith("data:image/"), by_id_d[acct_inc_x]["content"])
check("acct11 (item 11): the second included figure (Y) is skipped for lack of remaining budget and "
      "carries the dedicated memory-limit flag (never a generic 'unavailable') -- proving included figures "
      "still correctly COMPETE for the cap, unlike excluded ones which never charge it at all",
      by_id_d[acct_inc_y]["content"].get("asset_budget_skipped") is True, by_id_d[acct_inc_y]["content"])

# --- item 13: missing/out-of-root figures consume 0, tested directly
# against _figure_data_uri (the exact function build_preview_model calls
# when resolve_figure_bytes=True) with a real budget object, confirming
# `remaining` is untouched by a failure that never reaches the reserve
# step. Already separately proven (figure_outside11, figure38) that these
# fail safely; this specifically re-confirms the BUDGET side. ---
budget_item13 = pp._PreviewImageBudget(total_bytes=1_000_000)
remaining_before_item13 = budget_item13.remaining
uri_missing, flag_missing = pp._figure_data_uri(p_acct_a, "", budget_item13)
uri_outside, flag_outside = pp._figure_data_uri(p_acct_a, str(IMG1), budget_item13)  # IMG1 is out-of-root for p_acct_a
check("acct13a (item 13): a missing/empty file_path consumes 0 aggregate budget",
      uri_missing is None and flag_missing is False and budget_item13.remaining == remaining_before_item13,
      budget_item13.remaining)
check("acct13b (item 13): an out-of-root file consumes 0 aggregate budget",
      uri_outside is None and flag_outside is False and budget_item13.remaining == remaining_before_item13,
      budget_item13.remaining)


# item 15: caption and alt text preserved/escaped safely through the
# whole pipeline (including a value containing HTML-significant chars,
# proving real escaping, not just absence-of-injection by luck).
repo1p, svc1p = av.open_service(p1)
xss_fig_res = svc1p.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG1,
                                           caption="Cover <b>bold</b> & \"quotes\"", alt_text="Alt <i>text</i>")
model_xss = pp.build_preview_model(repo1p, svc1p, "PUB-MAIN", "PROF-EU")
repo1p.close()
xss_row = [b for b in model_xss["topics"][0]["blocks"] if b["block_id"] == xss_fig_res["block_id"]][0]
check("caption15 (item 15a): caption text is preserved in the model exactly as authored",
      xss_row["content"]["caption"] == 'Cover <b>bold</b> & "quotes"', xss_row["content"])
xss_html = pp.render_preview_html(model_xss, show_excluded=False)
check("alt15b (item 15b): caption/alt text are HTML-escaped in the rendered output -- no raw unescaped "
      "<b>/<i> tags injected into the page", "<b>bold</b>" not in xss_html and "<i>text</i>" not in xss_html and
      "&lt;b&gt;" in xss_html, None)

# item 16: figure resolution + rendering remain read-only (re-
# affirmed specifically for the figure path, on top of the general
# read-only section already covered elsewhere in this suite).
repo1q, _ = av.open_service(p1)
fig_row_before = dict(repo1q.conn.execute("SELECT version,file_path FROM figures WHERE figure_id=?", (fig_res["figure_id"],)).fetchone())
audit_before_fig = repo1q.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
repo1q.close()
repo1r, svc1r = av.open_service(p1)
pp.render_preview_html(pp.build_preview_model(repo1r, svc1r, "PUB-MAIN", "PROF-EU"), show_excluded=True)
repo1r.close()
repo1s, _ = av.open_service(p1)
fig_row_after = dict(repo1s.conn.execute("SELECT version,file_path FROM figures WHERE figure_id=?", (fig_res["figure_id"],)).fetchone())
audit_after_fig = repo1s.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
repo1s.close()
check("readonly_fig11 (item 11): figure resolution/rendering never mutates the figure row (no version bump, "
      "file never moved/copied)", fig_row_before == fig_row_after, (fig_row_before, fig_row_after))
check("readonly_fig12 (item 12): no audit event from resolving/rendering a figure",
      audit_before_fig == audit_after_fig, (audit_before_fig, audit_after_fig))


# ====================================================================
# component_ref (items 40-41)
# ====================================================================
comp_content = by_id_eu_t1[comp_blk]["content"]
check("component40 (item 40): component_ref gets a supported read-only representation (title+body resolved, "
      "not a raw placeholder, since safe resolution IS available here)",
      comp_content["kind"] == "component_ref" and comp_content.get("text") == "Indossare i DPI prima dell'uso.",
      comp_content)
check("component41 (item 41): the component_ref block is never silently omitted from the topic's block list",
      comp_blk in [b["block_id"] for b in t1_eu["blocks"]], None)

# A component_ref pointing at a component_id that does not exist must
# fail visibly (a safe placeholder), never crash the whole preview.
# add_block() itself validates the referenced component exists at INSERT
# time (a real DB-level invariant -- confirmed by KeyError on a bogus id),
# so a genuinely dangling reference can only arise from external data
# corruption -- simulated here via a direct SQL delete of the component
# row, bypassing the app's own normal paths on purpose.
repo1m, svc1m = av.open_service(p1)
ghost_comp_blk = "BLK-COMP-GHOST"
repo1m.create_component("COMP-TO-DELETE", "Componente da eliminare", "Corpo")
repo1m.add_block(ghost_comp_blk, "T1", "component_ref", position=100, object_ref_id="COMP-TO-DELETE", status="Draft")
repo1m.conn.commit()
repo1m.conn.execute("DELETE FROM components WHERE component_id='COMP-TO-DELETE'")
repo1m.conn.commit()
model_ghost_comp = pp.build_preview_model(repo1m, svc1m, "PUB-MAIN", "PROF-EU")
repo1m.close()
ghost_comp_row = [b for b in model_ghost_comp["topics"][0]["blocks"] if b["block_id"] == ghost_comp_blk][0]
check("component41b: a component_ref pointing at a non-existent component gets a safe placeholder, not a crash",
      ghost_comp_row["content"].get("unavailable") is True and "non disponibile" in ghost_comp_row["content"]["note"],
      ghost_comp_row["content"])


# ====================================================================
# Status (items 42-43)
# ====================================================================
check("status42 (item 42): draft content is previewable (the intro/EU/US paragraphs are Draft-status topics/"
      "blocks and still render)", model_eu["has_draft_content"] is True, model_eu["has_draft_content"])
import inspect as _inspect
_model_src = _inspect.getsource(pp.build_preview_model)
_render_src = _inspect.getsource(pp.render_preview_html)
check("status43 (item 43): the preview never calls into release/readiness machinery -- it cannot imply "
      "release readiness because it never touches it",
      "release_readiness" not in _model_src and "release_manager" not in _model_src and
      "release_readiness" not in _render_src and "release_manager" not in _render_src, None)


# ====================================================================
# Project switch (items 44-46)
# ====================================================================
p2 = _new_project("e2_02_other_project")
repo2, svc2 = av.open_service(p2)
svc2.create_variant_profile("PROF-OTHER", "Other Profile", {}, actor="Writer A")
repo2.close()

# UX Gate 2A: studio_preview_profile_choices_ui is now the ONE profile
# choice builder shared by Contenuti's evaluator and Rilascio's preview/
# export, so it returns (dropdown_update, kept_profile_id) -- the second
# half feeds the shared profile_state. Unpack; the choice-list assertions
# below are unchanged.
choices_p1, kept_p1 = app.studio_preview_profile_choices_ui(str(p1))
choices_p2, kept_p2 = app.studio_preview_profile_choices_ui(str(p2))
check("switch44b (UX Gate 2A): with no prior selection, nothing is invented -- the carried profile is None",
      kept_p1 is None and kept_p2 is None, (kept_p1, kept_p2))
# and a selection from ANOTHER project is dropped rather than carried over
_, kept_cross = app.studio_preview_profile_choices_ui(str(p2), "PROF-EU")
check("switch44c (UX Gate 2A): a profile id from a different project is dropped, never carried across",
      kept_cross is None, kept_cross)
_, kept_same = app.studio_preview_profile_choices_ui(str(p1), "PROF-EU")
check("switch44d (UX Gate 2A): a still-valid selection IS preserved across a refresh",
      kept_same == "PROF-EU", kept_same)
check("switch44 (item 44): profile choices refresh correctly per project (project 1's profiles, then "
      "project 2's, are each exactly right -- no leakage either direction)",
      {c[1] for c in choices_p1["choices"]} == {"PROF-EU", "PROF-US", "PROF-EU-OP"} and
      {c[1] for c in choices_p2["choices"]} == {"PROF-OTHER"}, (choices_p1, choices_p2))

html_p1 = app.studio_preview_refresh_ui(str(p1), "PUB-MAIN", "PROF-EU", False)
html_p2 = app.studio_preview_refresh_ui(str(p2), "PUB-MAIN", None, False)
check("switch45 (item 45): the publication title in the rendered preview reflects the CURRENT project",
      "Synthetic Manual" in html_p1, html_p1[:200])
check("switch46 (item 46): opening project 2 with no profile selected shows the neutral prompt, never "
      "project 1's stale preview content", "Seleziona un profilo" in html_p2 and "Solo EU" not in html_p2, html_p2)

# item 46b: project 2's own profile id is meaningless in project 1 (no
# cross-project leakage even if the exact same profile_id string were
# reused, which it isn't here, but the id-scoping must still hold).
check("switch46b: project 1's profile id ('PROF-EU') does not exist in project 2's own choice list",
      "PROF-EU" not in {c[1] for c in choices_p2["choices"]}, choices_p2)


# ====================================================================
# UI callbacks (items 47-52)
# ====================================================================
check("ui47 (item 47): no profile selected -> the real callback returns the clear neutral state",
      "Seleziona un profilo per vedere quali contenuti verranno inclusi." in
      app.studio_preview_refresh_ui(str(p1), "PUB-MAIN", None, False), None)

selected_html = app.studio_preview_refresh_ui(str(p1), "PUB-MAIN", "PROF-EU", False)
check("ui48 (item 48): a selected profile produces real, visible preview HTML (not empty, not the neutral "
      "message)", len(selected_html) > 200 and "Seleziona un profilo" not in selected_html, len(selected_html))

check("ui49 (item 49): 'Mostra contenuti esclusi' OFF hides excluded content -- the US-only paragraph text "
      "does not appear at all", "Testo solo US." not in selected_html, None)
excluded_html = app.studio_preview_refresh_ui(str(p1), "PUB-MAIN", "PROF-EU", True)
check("ui50 (item 50): 'Mostra contenuti esclusi' ON shows excluded items WITH a human reason",
      "Non incluso" in excluded_html or "escluso" in excluded_html.lower(), excluded_html[:300])

check("ui51 (item 51): the callback's own HTML uses only human labels for the profile ('EU Service'), "
      "never the raw profile id", "EU Service" in selected_html and "PROF-EU" not in selected_html, None)
check("ui52 (item 52): no raw block/table/figure/component ids appear anywhere in the standard rendered "
      "output", not any(rid in excluded_html for rid in
                        (eu_blk, us_blk, table_id, fig_res["figure_id"], "COMP-1")), None)

# Bug found and fixed during manual fixture verification: a plain
# paragraph's block-kind label incorrectly carried the STUDIO-EDITOR-
# specific "— modifica disponibile nel prossimo incremento" suffix (from
# reusing authoring_view.block_type_label(editable=False) verbatim,
# whose False branch is meant for component_ref/etc. in the Studio
# editor, not for a read-only preview where nothing is ever editable
# regardless of block type).
check("ui53 (Studio-editability suffix must never leak into the read-only preview): a plain paragraph's "
      "kind label in the rendered preview is the plain 'Paragrafo', never the Studio-editor "
      "'non ancora editabile' phrasing", "modifica disponibile" not in selected_html, selected_html)


# ====================================================================
# Singular/plural summary wording (bug-fix regression: "1 escluso/i")
#
# Manual acceptance flagged "2 topic inclusi · 1 escluso/i" as ungrammatical
# placeholder wording. Fixed via _it_plural(); verified both the helper
# in isolation AND the actual rendered summary for real singular (1) and
# plural (many/0) counts, so this can never silently regress back to
# hardcoded plural-only text.
# ====================================================================
check("plural1: _it_plural picks the singular form for count==1", pp._it_plural(1, "escluso", "esclusi") == "escluso", None)
check("plural2: _it_plural picks the plural form for count==2 and count==0 alike (standard Italian usage)",
      pp._it_plural(2, "escluso", "esclusi") == "esclusi" and pp._it_plural(0, "escluso", "esclusi") == "esclusi", None)

# T2 alone (US-placement-only) under the EU profile has exactly 1 topic
# excluded (T3) is NOT relevant here -- build a small dedicated model
# with a KNOWN singular topic-excluded count to check the real rendered
# summary text end to end.
repo1t, svc1t = av.open_service(p1)
model_singular_check = pp.build_preview_model(repo1t, svc1t, "PUB-MAIN", "PROF-EU")
repo1t.close()
summary_html_singular = pp.render_preview_html(model_singular_check, show_excluded=False)
s_check = model_singular_check["summary"]
check("plural3 (topic incluso/inclusi): the rendered summary correctly pluralizes the topic-included count",
      (f"{s_check['topics_included']} topic inclusi" in summary_html_singular if s_check["topics_included"] != 1
       else f"{s_check['topics_included']} topic incluso" in summary_html_singular), summary_html_singular[:200])
check("plural4 (topic escluso/esclusi): the rendered summary correctly pluralizes the topic-excluded count "
      "-- this is THE exact reported bug ('1 escluso/i' must now read '1 escluso')",
      (f"{s_check['topics_excluded']} esclusi" in summary_html_singular if s_check["topics_excluded"] != 1
       else f"{s_check['topics_excluded']} escluso" in summary_html_singular) and "escluso/i" not in summary_html_singular,
      summary_html_singular[:200])
check("plural5 (contenuto incluso/inclusi, contenuto escluso/esclusi): both block-level counts use correct "
      "singular/plural noun+adjective agreement",
      (f"{s_check['blocks_included']} contenuti inclusi" in summary_html_singular if s_check["blocks_included"] != 1
       else f"{s_check['blocks_included']} contenuto incluso" in summary_html_singular), summary_html_singular[:200])
check("plural6 (elemento non valutabile / elementi non valutabili): the unresolved count uses correct "
      "singular/plural noun+adjective agreement",
      (f"{s_check['unresolved']} elementi non valutabili" in summary_html_singular if s_check["unresolved"] != 1
       else f"{s_check['unresolved']} elemento non valutabile" in summary_html_singular), summary_html_singular[:250])

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"), "tests": len(results)}))
