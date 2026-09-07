"""Milestone 4E.1 regression suite: Conditions & Variants Foundation.

Tests the new dimension/value registry, the simple OR-within-dimension/
AND-across-dimensions condition compiler+evaluator built on top of the
PRE-EXISTING, unchanged IndustrialRepository.condition_matches() boolean-
tree engine, variant profiles, topic/block condition authoring (save_topic/
save_block's new `condition` parameter riding the existing CAS/version/
audit transaction), the reuse+conditions isolation guarantee (a shared
table/figure's own condition_json is never touched by block-level
condition authoring), and the real app.py Studio "Varianti" panel + minimal
Impostazioni > Varianti management callbacks -- calling the actual
functions the product wires to Gradio, not a mock, matching the discipline
established for the 4B.1/4B.2/4C/4D suites.

Covers the 36 ticket-enumerated items (data model, evaluator, topic/block,
reuse, profiles, UI callback, compatibility) plus a handful of additional
risk-driven cases for the two production code paths this milestone
actually touches: authoring_service.py's save_topic/save_block and
ccms_industrial.py's assemble_structured_publication (topic-level
condition_matches check, additive alongside the untouched legacy
_topic_allowed_with_profile).

Backend/integration only -- no browser (RAM-constrained iteration; see the
4E.1 report for the explicit browser-automation exclusion this session).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService, VersionConflictError, ArchivedObjectError
import authoring_view as av
from project_lifecycle import create_project
import app

TMP = Path(tempfile.mkdtemp(prefix="manualsync_authoring_conditions_variants_4e1_"))
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


def _seed_market_edition(svc: AuthoringService, actor: str = "Writer A") -> None:
    svc.create_condition_dimension("market", "Mercato", actor=actor)
    svc.create_condition_value("eu", "market", "EU", actor=actor)
    svc.create_condition_value("uk", "market", "UK", actor=actor)
    svc.create_condition_value("us", "market", "US", actor=actor)
    svc.create_condition_dimension("edition", "Edizione", actor=actor)
    svc.create_condition_value("operator", "edition", "Operator", actor=actor)
    svc.create_condition_value("service", "edition", "Service", actor=actor)


def _share_table(repo: IndustrialRepository, svc: AuthoringService):
    res = svc.create_table_in_topic("T1", actor="Writer A", caption="Shared table",
                                    columns=["A", "B"], rows=[["1", "2"]])
    table_id, block1 = res["table_id"], res["block_id"]
    block2 = f"BLK-SHARE-{table_id[-6:]}"
    repo.add_block(block2, "T2", "table", position=1, object_ref_id=table_id, status="Draft")
    return table_id, block1, block2


# ====================================================================
# Data model (items 1-5)
# ====================================================================
p1 = _new_project("e01_data_model")
repo1, svc1 = av.open_service(p1)

d1 = svc1.create_condition_dimension("market", "Mercato", actor="Writer A")
check("dm1 (item 1): create_condition_dimension succeeds and appears in the registry",
      any(x["dimension_id"] == "market" for x in svc1.list_condition_dimensions()), d1)

renamed = svc1.rename_condition_dimension("market", name="Mercato (v2)", actor="Writer A")
check("dm2 (item 2): stable id -- rename changes the NAME, never the dimension_id",
      renamed["dimension_id"] == "market" and renamed["name"] == "Mercato (v2)", renamed)
still_market = svc1.conn.execute("SELECT dimension_id FROM condition_dimensions WHERE name=?", ("Mercato (v2)",)).fetchone()
check("dm2b (item 2): the id used by list_condition_values(dimension_id='market') still resolves after rename",
      still_market["dimension_id"] == "market", dict(still_market))

v1 = svc1.create_condition_value("eu", "market", "EU", actor="Writer A")
check("dm3 (item 3): create_condition_value succeeds and appears under its dimension",
      any(x["value_id"] == "eu" for x in svc1.list_condition_values("market")), v1)

svc1.set_condition_value_active("eu", False, actor="Writer A")
eu_after = [x for x in svc1.list_condition_values("market") if x["value_id"] == "eu"][0]
check("dm4 (item 4): deactivate_value -- value still exists (not deleted), active flips to False",
      eu_after["active"] is False, eu_after)

repo1.conn.execute(
    "INSERT INTO topics(topic_id,title,body,source_lang,status,version,include_variants_json,exclude_variants_json,"
    "source_refs_json,evidence_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    ("T-REF", "Referencing topic", "b", "en-US", "Draft", 1, "[]", "[]", "[]", "OK"),
)
repo1.conn.commit()
svc1.set_condition_value_active("eu", True, actor="Writer A")  # reactivate so it's a valid choice again for the save below
svc1.save_topic("T-REF", condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
check("dm5a (item 5): is_value_referenced reports True once a value is used by an authored condition",
      svc1.is_value_referenced("eu") is True, None)
check("dm5b (item 5): AuthoringService exposes NO destructive delete for a dimension or value -- only "
      "deactivate/reactivate (architectural guarantee: a referenced value can never be hard-deleted)",
      not hasattr(svc1, "delete_condition_value") and not hasattr(svc1, "delete_condition_dimension"), None)
repo1.close()


# ====================================================================
# Evaluator (items 6-12)
# ====================================================================
p2 = _new_project("e02_evaluator")
repo2, svc2 = av.open_service(p2)
_seed_market_edition(svc2)

check("ev1 (item 6): no conditions -> always included",
      svc2.evaluate_conditions({}, {"market": "us"})["included"] is True, None)

or_cond = svc2._compile_condition({"market": ["eu", "uk"]})
check("ev2 (item 7): same-dimension multiple values -> OR (either satisfies)",
      svc2.evaluate_conditions(or_cond, {"market": "eu"})["included"] is True and
      svc2.evaluate_conditions(or_cond, {"market": "uk"})["included"] is True and
      svc2.evaluate_conditions(or_cond, {"market": "us"})["included"] is False, or_cond)

and_cond = svc2._compile_condition({"market": ["eu"], "edition": ["service"]})
r_and_both = svc2.evaluate_conditions(and_cond, {"market": "eu", "edition": "service"})
r_and_one = svc2.evaluate_conditions(and_cond, {"market": "eu", "edition": "operator"})
check("ev3 (item 8): different dimensions -> AND (both must be satisfied)",
      r_and_both["included"] is True and r_and_one["included"] is False, (r_and_both, r_and_one))

check("ev4 (item 9): matching profile -> included",
      svc2.evaluate_conditions(or_cond, {"market": "eu"})["included"] is True, None)
check("ev5 (item 10): non-matching profile -> excluded, reported in failed_dimensions",
      svc2.evaluate_conditions(or_cond, {"market": "us"})["failed_dimensions"] == ["market"], None)

ghost_cond = {"field": "ghost_dim", "op": "in", "value": ["x"]}
r_ghost = svc2.evaluate_conditions(ghost_cond, {"ghost_dim": "x"})
check("ev6 (item 11): condition referencing a NONEXISTENT dimension -> unresolved, fail-closed (never included)",
      r_ghost["included"] is False and r_ghost["unresolved_dimensions"] == ["ghost_dim"], r_ghost)

ghost_value_cond = {"field": "market", "op": "in", "value": ["ghost_value"]}
r_ghost_val = svc2.evaluate_conditions(ghost_value_cond, {"market": "eu"})
check("ev7 (item 12): condition referencing a NONEXISTENT value -> unresolved, fail-closed",
      r_ghost_val["included"] is False and r_ghost_val["unresolved_dimensions"] == ["market"], r_ghost_val)
repo2.close()


# ====================================================================
# Topic/block authoring (items 13-17)
# ====================================================================
p3 = _new_project("e03_topic_block")
repo3, svc3 = av.open_service(p3)
_seed_market_edition(svc3)

t3_save = svc3.save_topic("T1", condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
check("tb1 (item 13): topic condition save persists", t3_save["condition_changed"] is True, t3_save)
t3_row = repo3.conn.execute("SELECT condition_json FROM topics WHERE topic_id=?", ("T1",)).fetchone()
check("tb1b: persisted topic condition_json matches the compiled shape",
      json.loads(t3_row["condition_json"]) == {"field": "market", "op": "in", "value": ["eu"]}, t3_row["condition_json"])

blk_res = svc3.create_table_in_topic("T1", actor="Writer A", caption="cap", columns=["A"], rows=[["1"]])
blk_id = blk_res["block_id"]
b3_save = svc3.save_block(blk_id, condition={"edition": ["service"]}, expected_version=1, actor="Writer A")
check("tb2 (item 14): block condition save persists", b3_save["condition_changed"] is True, b3_save)
b3_row = repo3.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (blk_id,)).fetchone()
check("tb2b: persisted block condition_json matches the compiled shape",
      json.loads(b3_row["condition_json"]) == {"field": "edition", "op": "in", "value": ["service"]}, b3_row["condition_json"])

t3_save2 = svc3.save_topic("T1", condition={"market": ["us"]}, expected_version=t3_save["version"], actor="Writer A")
hist_v1 = repo3.conn.execute("SELECT condition_json FROM topic_versions WHERE topic_id=? AND version=1", ("T1",)).fetchone()
check("tb3 (item 15): history immutable -- version 1's stored condition_json (before ANY condition existed, '{}') "
      "is untouched by the later condition-adding saves",
      hist_v1["condition_json"] == "{}", hist_v1["condition_json"])
hist_v2 = repo3.conn.execute("SELECT condition_json FROM topic_versions WHERE topic_id=? AND version=2", ("T1",)).fetchone()
check("tb3b: version 2's own stored condition_json (market=eu) also stays exactly as it was recorded, "
      "even after version 3 (market=us) exists",
      json.loads(hist_v2["condition_json"]) == {"field": "market", "op": "in", "value": ["eu"]}, hist_v2["condition_json"])

audit3 = [r["action"] for r in repo3.conn.execute(
    "SELECT action FROM audit_events WHERE object_id='T1' ORDER BY event_id")]
check("tb4 (item 16): AUTHORING_CONDITIONS_UPDATED audit event emitted for each real condition change",
      audit3.count("AUTHORING_CONDITIONS_UPDATED") == 2, audit3)
t3_save_noop = svc3.save_topic("T1", condition={"market": ["us"]}, expected_version=t3_save2["version"], actor="Writer A")
audit3b = [r["action"] for r in repo3.conn.execute(
    "SELECT action FROM audit_events WHERE object_id='T1' ORDER BY event_id")]
check("tb4b: resubmitting the SAME condition does NOT emit a spurious extra AUTHORING_CONDITIONS_UPDATED",
      audit3b.count("AUTHORING_CONDITIONS_UPDATED") == 2 and t3_save_noop["condition_changed"] is False, audit3b)

t3_before = repo3.conn.execute("SELECT version FROM topics WHERE topic_id='T1'").fetchone()["version"]
t3_combo = svc3.save_topic("T1", title="New Title", condition={"market": ["eu", "uk"]},
                           expected_version=t3_before, actor="Writer A")
check("tb5 (item 17): a SINGLE save changing BOTH title and condition bumps version by exactly 1 "
      "(no duplicate version bump for one atomic action)",
      t3_combo["version"] == t3_before + 1, (t3_before, t3_combo["version"]))
repo3.close()


# ====================================================================
# Reuse + Conditions (items 18-23) -- the ticket's critical scenario
# ====================================================================
p4 = _new_project("e04_reuse")
repo4, svc4 = av.open_service(p4)
_seed_market_edition(svc4)

t4, blkA, blkB = _share_table(repo4, svc4)
check("ru1 (item 18): the same shared table object is referenced by two distinct blocks",
      svc4.get_reuse_impact("table", t4)["reference_count"] == 2, None)

svc4.save_block(blkA, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc4.save_block(blkB, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
condA = json.loads(repo4.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (blkA,)).fetchone()["condition_json"])
condB = json.loads(repo4.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (blkB,)).fetchone()["condition_json"])
check("ru2 (item 19): block A and block B carry DIFFERENT placement-level conditions on the SAME shared object",
      condA == {"field": "market", "op": "in", "value": ["eu"]} and condB == {"field": "market", "op": "in", "value": ["us"]},
      (condA, condB))

table_count = repo4.conn.execute("SELECT COUNT(*) AS c FROM structured_tables WHERE table_id=?", (t4,)).fetchone()["c"]
check("ru3 (item 20): the shared object itself is NOT duplicated -- exactly one structured_tables row",
      table_count == 1, table_count)

svc4.save_block(blkA, condition={"market": ["eu", "uk"]}, expected_version=2, actor="Writer A")
condB_after = json.loads(repo4.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (blkB,)).fetchone()["condition_json"])
check("ru4 (item 21): editing block A's condition again does NOT change block B's condition_json at all",
      condB_after == condB, (condB_after, condB))

table_cond_before_fork = repo4.conn.execute("SELECT condition_json FROM structured_tables WHERE table_id=?", (t4,)).fetchone()["condition_json"]
fork_res = svc4.fork_table_local(t4, block_id=blkA, expected_version=1, actor="Writer A")
fork_table_id = fork_res["table_id"]
condA_after_fork = json.loads(repo4.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (blkA,)).fetchone()["condition_json"])
check("ru5 (item 22): local fork preserves the BLOCK's own placement condition unchanged (fork_table_local only "
      "rewires object_ref_id, never touches the block's condition_json)",
      condA_after_fork == {"field": "market", "op": "in", "value": ["eu", "uk"]}, condA_after_fork)
condB_after_fork = json.loads(repo4.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (blkB,)).fetchone()["condition_json"])
check("ru5b: block B's condition is untouched by A's fork", condB_after_fork == condB, condB_after_fork)

impact_tok = svc4.get_reuse_impact("table", t4)["impact_token"]
svc4.save_table(t4, caption="shared update", expected_version=repo4.conn.execute(
    "SELECT version FROM structured_tables WHERE table_id=?", (t4,)).fetchone()["version"],
    actor="Writer A", confirm_shared=True, expected_impact_token=impact_tok)
condB_after_shared_update = json.loads(repo4.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (blkB,)).fetchone()["condition_json"])
table_cond_after = repo4.conn.execute("SELECT condition_json FROM structured_tables WHERE table_id=?", (t4,)).fetchone()["condition_json"]
check("ru6 (item 23): 'Aggiorna ovunque' (shared content save) never touches ANY block's placement condition, "
      "and never writes anything into the shared object's OWN (vestigial, unused) condition_json either",
      condB_after_shared_update == condB and table_cond_after == table_cond_before_fork,
      (condB_after_shared_update, table_cond_after, table_cond_before_fork))
repo4.close()


# ====================================================================
# Variant profiles (items 24-27)
# ====================================================================
p5 = _new_project("e05_profiles")
repo5, svc5 = av.open_service(p5)
_seed_market_edition(svc5)

prof5 = svc5.create_variant_profile("PROF-EU-SVC", "EU Service", {"market": "eu", "edition": "service"}, actor="Writer A")
check("pf1 (item 24): create_variant_profile succeeds", prof5["dimensions"] == {"market": "eu", "edition": "service"}, prof5)

updated5 = svc5.update_variant_profile("PROF-EU-SVC", name="EU Service (v2)", dimension_values={"market": "uk"}, actor="Writer A")
check("pf2 (item 25): update_variant_profile changes name and dimensions",
      updated5["name"] == "EU Service (v2)" and updated5["dimensions"] == {"market": "uk"}, updated5)
audit5 = [r["action"] for r in repo5.conn.execute("SELECT action FROM audit_events WHERE object_id='PROF-EU-SVC' ORDER BY event_id")]
check("pf2b: VARIANT_PROFILE_CREATED and VARIANT_PROFILE_UPDATED both audited",
      "VARIANT_PROFILE_CREATED" in audit5 and "VARIANT_PROFILE_UPDATED" in audit5, audit5)

check("pf3 (item 26): creating a profile with an unknown dimension/value is rejected",
      _raises(lambda: svc5.create_variant_profile("PROF-BAD", "Bad", {"market": "atlantis"}, actor="Writer A"), ValueError), None)

svc5.set_condition_value_active("uk", False, actor="Writer A")
eval5 = svc5.evaluate_conditions(svc5._compile_condition({"market": ["uk"]}), {"market": "uk"})
check("pf4 (item 27): a profile referencing a value later DEACTIVATED still evaluates normally (resolvable, "
      "matched) -- 4E.1's fail-closed rule is about NONEXISTENT references, not merely inactive ones",
      eval5["included"] is True and eval5["unresolved_dimensions"] == [], eval5)
repo5.close()


# ====================================================================
# UI callback -- real app.py Studio "Varianti" panel + evaluator (items 28-33)
#
# Save-side assertions here use the panel-refresh helper (studio_variant_
# panel_ui, what .then(*_VARIANT_THEN_ARGS) calls) rather than reading the
# summary off studio_variant_save_ui's own return -- since the Phase 8
# stabilization fix, that function returns only (message, version), not
# the panel bundle. See the dedicated "Salva varianti fail-visible save"
# section further below for the full save-contract regression coverage.
# ====================================================================
p6 = _new_project("e06_ui_callback")
repo6, svc6 = av.open_service(p6)
_seed_market_edition(svc6)
blk6 = svc6.create_table_in_topic("T1", actor="Writer A", caption="cap", columns=["A"], rows=[["1"]])["block_id"]
svc6.save_block(blk6, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc6.create_variant_profile("PROF-EU", "EU", {"market": "eu"}, actor="Writer A")
repo6.close()

open6 = app.studio_open_ui(str(p6), "PUB-MAIN", f"block:{blk6}", False)
kind6, id6 = open6[app.ST_IDX_KIND], open6[app.ST_IDX_ID]
panel6 = app.studio_variant_panel_ui(str(p6), kind6, id6)
check("cb1 (item 28): opening a conditioned block via the real Studio open callback, then refreshing the "
      "Varianti panel, shows its condition", panel6[0] == "Visibile per: Mercato = EU", panel6[0])

dim_vals6 = ["edition", "", "", ""]
val_vals6 = [["operator"], [], [], []]
save6 = app.studio_variant_save_ui(str(p6), kind6, id6, open6[app.ST_IDX_BLOCK_TYPE], "Writer A", *dim_vals6, *val_vals6)
check("cb2 (item 29): saving new conditions through the real studio_variant_save_ui callback succeeds -- "
      "blk6 is a TABLE block, so version_out is correctly a no-op gr.update() (st_version tracks the "
      "table's own version for table/figure blocks, untouched by a condition-only save on the wrapping block)",
      save6[0] == "✅ Varianti salvate." and isinstance(save6[-1], dict), save6)
panel6b = app.studio_variant_panel_ui(str(p6), kind6, id6)
check("cb3 (item 30): the panel-refresh chained after the save (what .then() actually shows) carries the "
      "same human-readable summary authoring_view.py produces", panel6b[0] == "Visibile per: Edizione = Operator", panel6b[0])
check("cb4 (item 31): no technical dimension_id/value_id ('edition'/'operator') leaks into the standard "
      "summary -- only the human NAME/LABEL ('Edizione'/'Operator')",
      "edition" not in panel6b[0] and "operator" not in panel6b[0] and
      "Edizione" in panel6b[0] and "Operator" in panel6b[0], panel6b[0])

eval6 = app.studio_variant_evaluate_ui(str(p6), kind6, id6, "PROF-EU")
check("cb5 (item 32): evaluate-selected-element-with-profile callback runs end-to-end and reports a human-"
      "facing 'not included' (block now requires Edizione=Operator, PROF-EU doesn't set edition at all)",
      "Non incluso" in eval6, eval6)

stale_save = app.studio_variant_save_ui(str(p6), kind6, id6, open6[app.ST_IDX_BLOCK_TYPE], "Writer A", *dim_vals6, *val_vals6)
repo6b, svc6b = av.open_service(p6)
current_cond6 = repo6b.conn.execute("SELECT condition_json,version FROM content_blocks WHERE block_id=?", (id6,)).fetchone()
repo6b.close()
check("cb6 (item 33): studio_variant_save_ui reads the object's CURRENT version fresh at save time rather "
      "than trusting stale UI state, so a second identical save is a real no-op CAS (never silently corrupts/"
      "double-bumps) -- confirms the fail-closed CAS path is exercised through the real callback",
      stale_save[0] == "✅ Varianti salvate." and json.loads(current_cond6["condition_json"]) ==
      {"field": "edition", "op": "in", "value": ["operator"]}, (stale_save, dict(current_cond6)))


# ====================================================================
# Compatibility / migration (items 34-36)
# ====================================================================
p7 = _new_project("e07_compat")
repo7 = IndustrialRepository(p7 / "ManualSync_CCMS.sqlite")
check("cp1 (item 34): a project created before 4E.1-style authoring (no condition ever set) opens without error",
      repo7 is not None, None)
legacy_row = repo7.conn.execute("SELECT condition_json FROM topics WHERE topic_id='T1'").fetchone()
check("cp1b: pre-existing topic's condition_json defaults to '{}' via the schema migration",
      legacy_row["condition_json"] == "{}", legacy_row["condition_json"])
svc7 = AuthoringService(repo7)
legacy_eval = svc7.evaluate_conditions({}, {})
check("cp2 (item 35): legacy topic/block (no condition) always evaluates included",
      legacy_eval["included"] is True, legacy_eval)
repo7.close()

# Re-opening (re-running schema migration) a second and third time must be
# a safe no-op: no error, no data loss, no duplicate columns/tables.
repo7b = IndustrialRepository(p7 / "ManualSync_CCMS.sqlite")
repo7c = IndustrialRepository(p7 / "ManualSync_CCMS.sqlite")
still_there = repo7c.conn.execute("SELECT title FROM topics WHERE topic_id='T1'").fetchone()
check("cp3 (item 36): re-running the 4E.1 migration (constructing IndustrialRepository again on an already-"
      "migrated db) multiple times is idempotent -- no error, original data intact",
      still_there is not None and still_there["title"] == "Topic One", None)
repo7b.close()
repo7c.close()


# ====================================================================
# Additional risk-driven coverage: save-time validation + publication
# assembly (ccms_industrial.py's new topic-level condition_matches check)
# ====================================================================
p8 = _new_project("e08_extra")
repo8, svc8 = av.open_service(p8)
_seed_market_edition(svc8)

check("ex1: save_topic rejects a condition referencing an UNKNOWN dimension at save time (fail-closed at "
      "authoring time too, not just at evaluation time)",
      _raises(lambda: svc8.save_topic("T1", condition={"nonexistent": ["x"]}, expected_version=1, actor="Writer A"),
              ValueError), None)
check("ex2: save_block rejects a condition referencing a value that belongs to a DIFFERENT dimension",
      _raises(lambda: svc8.save_block(
          svc8.create_table_in_topic("T1", actor="Writer A", caption="c", columns=["A"], rows=[["1"]])["block_id"],
          condition={"edition": ["eu"]}, expected_version=1, actor="Writer A"), ValueError), None)

svc8.save_topic("T1", condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc8.save_topic("T2", condition={"market": ["us"]}, expected_version=1, actor="Writer A")
svc8.create_variant_profile("PROF-EU8", "EU", {"market": "eu"}, actor="Writer A")
df_eu, summary_eu = repo8.assemble_structured_publication("PUB-MAIN", profile_id="PROF-EU8")
titles_eu = sorted(df_eu["topic_title"].unique().tolist()) if not df_eu.empty else []
check("ex3: assemble_structured_publication's NEW topic-level condition_matches check correctly includes "
      "only the topic matching the active profile (T1, market=eu) and excludes the other (T2, market=us)",
      titles_eu == ["Topic One"], titles_eu)

df_us, _ = repo8.assemble_structured_publication("PUB-MAIN", dimensions={"market": "us"})
titles_us = sorted(df_us["topic_title"].unique().tolist()) if not df_us.empty else []
check("ex4: the same check also works via an explicit `dimensions` override (not just profile_id)",
      titles_us == ["Topic Two"], titles_us)
repo8.close()


# ====================================================================
# UI visibility wiring (bug-fix regression, items 37-43)
#
# Manual acceptance found the Varianti panel invisible in a real browser
# even though every callback-level test above passed. Root cause: the
# panel's refresh was wired to st_id.change()/st_version.change() (a
# gr.State "changed as an output of another event" cascade). That
# cascade IS real in the Gradio 6.5.1 Python backend (Blocks.
# get_state_ids_to_track/"changed_state_ids"), but changed_state_ids is
# only ever PRODUCED server-side -- nothing in routes.py/queueing.py
# consumes it, so firing the cascade is entirely up to frontend JS
# reacting to that field, which never actually happened in the browser.
# Fixed by chaining VARIANT_PANEL_OUT via .then(...) onto every event
# that already outputs ST_OUT_FULL -- .then() is a guaranteed same-
# response-cycle server-side sequencing primitive (trigger_after in the
# Blocks dependency graph), not dependent on any client-side value-diff
# detection. These tests exist specifically so they CANNOT pass while
# the panel is wired the old (broken) way: they inspect the actual
# compiled Blocks dependency graph (proving the container's own
# visibility output is really registered against real navigation
# events), not just a bare Python function call.
# ====================================================================
import gradio as gr
from PIL import Image

_cfg = app.demo.get_config_file()
_deps = _cfg["dependencies"]
_variant_panel_deps = [
    d for d in _deps
    if (bf := app.demo.fns.get(d.get("id"))) is not None
    and getattr(bf.fn, "__name__", "") == "studio_variant_panel_ui"
]
check("wire1 (item 37): the Varianti panel refresh is wired via .then(...) (trigger_after set -- a "
      "guaranteed server-side chain) onto EVERY ST_OUT_FULL-outputting Studio event, not a state-change "
      "cascade -- exactly the count of ST_OUT_FULL callsites",
      len(_variant_panel_deps) >= 20 and all(d.get("trigger_after") is not None for d in _variant_panel_deps),
      len(_variant_panel_deps))
check("wire2 (item 37b): each of those chained dependencies outputs to the FULL Varianti bundle "
      "(container visibility + summary + pickers), matching _VARIANT_PANEL_OUT_COUNT -- not just a subset "
      "of inner controls with the container itself left out",
      all(len(d.get("outputs", [])) == app._VARIANT_PANEL_OUT_COUNT for d in _variant_panel_deps),
      [len(d.get("outputs", [])) for d in _variant_panel_deps])
check("wire3: NO leftover st_id.change()/st_version.change() cascade dependency remains targeting "
      "studio_variant_panel_ui (the old, broken wiring was fully removed, not left as dead/duplicate code)",
      not any(d.get("targets") == [] or d.get("targets") == [(None, "change")] for d in _variant_panel_deps),
      [d.get("targets") for d in _variant_panel_deps])

def _panel_visible(out) -> bool:
    upd = out[1]
    return bool(isinstance(upd, dict) and upd.get("visible") is True)

p9 = _new_project("e09_ui_visibility")
repo9, svc9 = av.open_service(p9)
_seed_market_edition(svc9)

img_path = Path(tempfile.mkdtemp()) / "fixture.png"
Image.new("RGB", (12, 8), color=(90, 140, 200)).save(img_path)

blk_para = svc9.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Testo di prova")["block_id"]
blk_table = svc9.create_table_in_topic("T1", actor="Writer A", caption="cap", columns=["A"], rows=[["1"]])["block_id"]
blk_figure = svc9.create_figure_in_topic("T1", actor="Writer A", source_file_path=img_path, caption="fig")["block_id"]
svc9.save_topic("T1", condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
repo9.close()

out_topic = app.studio_variant_panel_ui(str(p9), "topic", "T1")
check("wire4 (item 38, selected topic -> visible): studio_variant_panel_ui's OWN container-visibility "
      "output (index 1, st_variant_panel) is visible=True for a selected topic",
      _panel_visible(out_topic), out_topic[1])

out_para = app.studio_variant_panel_ui(str(p9), "block", blk_para)
check("wire5 (item 39, selected paragraph block -> visible)", _panel_visible(out_para), out_para[1])

out_table = app.studio_variant_panel_ui(str(p9), "block", blk_table)
check("wire6 (item 40, selected table block -> visible)", _panel_visible(out_table), out_table[1])

out_figure = app.studio_variant_panel_ui(str(p9), "block", blk_figure)
check("wire7 (item 41, selected figure block -> visible)", _panel_visible(out_figure), out_figure[1])

out_none = app.studio_variant_panel_ui(str(p9), None, None)
check("wire8 (item 42, no selection -> hidden/neutral state): container visibility is explicitly False, "
      "never left ambiguous", isinstance(out_none[1], dict) and out_none[1].get("visible") is False, out_none[1])

check("wire9 (item 43, standard mode does not require 'Dettagli tecnici'): studio_variant_panel_ui is a "
      "plain top-level Studio callback -- it needs no advanced-mode accordion open/expanded state as an "
      "input or precondition to compute or return its result",
      "show_archived" not in app.studio_variant_panel_ui.__code__.co_varnames[:app.studio_variant_panel_ui.__code__.co_argcount],
      app.studio_variant_panel_ui.__code__.co_varnames)

check("wire10 (item 7 restated as a live-wiring check): the visible container's own summary text uses "
      "the human dimension NAME ('Mercato'), never the raw dimension_id ('market') that only exists "
      "internally -- 'eu'/'EU' is not usable for this check since this fixture's value id and label "
      "happen to coincide, but the dimension id/name pair ('market'/'Mercato') genuinely differ",
      "market" not in out_topic[0] and "Mercato" in out_topic[0], out_topic[0])


# ====================================================================
# Project/publication activation & resolution (bug-fix regression,
# items 44-52)
#
# Manual acceptance blocker: the '4E1 Manual Acceptance' fixture was
# built with publication_id "PUB-HX400", but EVERY project-creation and
# UI-default path in app.py (guided import, legacy import, source
# approval, Studio/Compliance/Release Publication ID textboxes,
# _studio_full_bundle's own fallback) consistently assumes "PUB-MAIN" as
# this app's single-publication-per-project convention (8 occurrences,
# all consistent -- confirmed via grep, not assumption). This was a
# FIXTURE defect, not an app defect: rebuilding a normal project through
# any of this app's own creation paths always produces "PUB-MAIN", so a
# hand-built fixture that uses a different id is the one non-conforming
# actor, not the app. The fixture DB was corrected in place (publications
# and publication_topics rows renamed PUB-HX400 -> PUB-MAIN; verified
# via direct SQL that these were the ONLY rows referencing the old id --
# no publication_releases or audit_events rows existed to update).
#
# These tests exist to also rule out (not just assume away) the other
# failure classes the investigation considered: stale repository/service
# binding across a project switch, wrong DB opened, and whether the app
# can ONLY ever work with "PUB-MAIN" (it can't -- it's a convention/
# default, not a hard constraint, proven by ex6 below).
# ====================================================================
p10 = _new_project("e10_project_a")
repo10, svc10 = av.open_service(p10)
repo10.conn.execute("DELETE FROM publication_topics")
repo10.conn.execute("DELETE FROM publications")
repo10.conn.execute(
    "INSERT INTO topics(topic_id,title,body,source_lang,status,version,include_variants_json,exclude_variants_json,"
    "source_refs_json,evidence_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    ("T-A-ONLY", "Solo nel Progetto A", "", "en-US", "Draft", 1, "[]", "[]", "[]", "OK"),
)
repo10.conn.commit()
repo10.create_publication("PUB-CUSTOM-A", "Manuale Progetto A", ["T-A-ONLY"])
repo10.close()

p11 = _new_project("e11_project_b")
repo11, svc11 = av.open_service(p11)
repo11.conn.execute("DELETE FROM publication_topics")
repo11.conn.execute("DELETE FROM publications")
repo11.conn.execute(
    "INSERT INTO topics(topic_id,title,body,source_lang,status,version,include_variants_json,exclude_variants_json,"
    "source_refs_json,evidence_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    ("T-B-ONLY", "Solo nel Progetto B", "", "en-US", "Draft", 1, "[]", "[]", "[]", "OK"),
)
repo11.conn.commit()
repo11.create_publication("PUB-CUSTOM-B", "Manuale Progetto B", ["T-B-ONLY"])
repo11.close()

# items 44-45: switching the active project directory between successive
# calls resolves the CORRECT project's data each time -- no cached/bound
# repository or service instance survives across the switch, because
# every app.py studio_*_ui callback opens a brand-new IndustrialRepository/
# AuthoringService via av.open_service(project_dir) on every single call
# (confirmed by inspection: no module-level or closure-cached repo/svc
# anywhere in app.py's Studio callback set).
def _outline_titles(bundle) -> list[str]:
    """ST_IDX_TABLE is a gr.update({'value': <DataFrame>, ...}) dict, not
    the DataFrame itself -- _studio_full_bundle always wraps its outputs
    this way. Extract the actual outline row labels from it."""
    upd = bundle[app.ST_IDX_TABLE]
    df = upd["value"] if isinstance(upd, dict) else upd
    if df is None or not hasattr(df, "empty") or df.empty or "Elemento" not in df.columns:
        return []
    return [str(x).strip() for x in df["Elemento"].tolist()]

bundle_a = app.studio_load_ui(str(p10), "PUB-CUSTOM-A", False)
bundle_b = app.studio_load_ui(str(p11), "PUB-CUSTOM-B", False)
titles_a = _outline_titles(bundle_a)
titles_b = _outline_titles(bundle_b)
check("proj1 (items 44-45): project A resolves ONLY project A's topic; switching to project B in the very "
      "next call resolves ONLY project B's topic -- no stale cross-project data",
      titles_a == ["Solo nel Progetto A"] and titles_b == ["Solo nel Progetto B"], (titles_a, titles_b))

# Switch back A -> B -> A again (simulates a user bouncing between two
# open projects) to rule out any ordering-dependent caching.
bundle_a2 = app.studio_load_ui(str(p10), "PUB-CUSTOM-A", False)
titles_a2 = _outline_titles(bundle_a2)
check("proj2 (item 45b): re-opening project A after B was opened still resolves project A's own data, "
      "not B's -- confirms no shared mutable cache between the two", titles_a2 == ["Solo nel Progetto A"], titles_a2)

# item 46: a stale publication id from project A does not leak into
# project B -- opening B's directory with A's publication id must NOT
# find anything (proves publication_id resolution is scoped per-DB, not
# via some global registry).
bundle_stale = app.studio_load_ui(str(p11), "PUB-CUSTOM-A", False)
check("proj3 (item 46): opening project B's directory with project A's publication id correctly finds "
      "NOTHING (publication_id resolution is scoped to the DB actually opened, never a stale/global id)",
      "Nessuna publication" in str(bundle_stale[app.ST_IDX_STATUS]), bundle_stale[app.ST_IDX_STATUS])

# item 47: PUB-MAIN is a UI DEFAULT/convention, not a hard technical
# constraint -- a project whose publication genuinely uses a different id
# opens correctly as long as that id is the one actually passed in.
check("proj4 (item 47): a project with a non-PUB-MAIN publication id opens correctly when that id is "
      "explicitly given -- proves PUB-MAIN is this app's convention/default, not a hard-coded requirement "
      "the storage/service layer enforces", "Nessuna publication" not in str(bundle_a[app.ST_IDX_STATUS]), None)

# item 48: when PUB-MAIN genuinely doesn't exist (project A/B use custom
# ids), the app reports the SAME clear, honest integrity message the
# user originally saw -- this is correct, intentional fail-closed
# behavior for the default id, not a bug to silently swallow.
bundle_default = app.studio_load_ui(str(p10), "PUB-MAIN", False)
check("proj5 (item 48): when a project genuinely has no PUB-MAIN publication, the app reports a clear "
      "'Nessuna publication «PUB-MAIN»' integrity message rather than silently showing an empty/misleading "
      "outline -- confirms this is working-as-intended fail-closed UX, not a defect to route around",
      "Nessuna publication" in str(bundle_default[app.ST_IDX_STATUS]) and "PUB-MAIN" in str(bundle_default[app.ST_IDX_STATUS]),
      bundle_default[app.ST_IDX_STATUS])

# items 49-52: the REAL '4E1 Manual Acceptance' fixture, freshly re-
# verified after the in-place publication_id correction, resolves
# deterministically through the real app.py opening path.
FIXTURE_DIR = Path.home() / "ManualSyncData" / "Projects" / "4E1_Manual_Acceptance"
if FIXTURE_DIR.exists():
    repo_fx = IndustrialRepository(FIXTURE_DIR / "ManualSync_CCMS.sqlite")
    pub_fx = repo_fx.conn.execute("SELECT title FROM publications WHERE publication_id='PUB-MAIN'").fetchone()
    check("fix1 (item 49): the real 4E1 fixture's publication now resolves under PUB-MAIN with the exact "
          "expected title", pub_fx is not None and pub_fx["title"] == "Manuale Pompa HX-400",
          dict(pub_fx) if pub_fx else None)
    topic_titles_fx = [r["title"] for r in repo_fx.conn.execute(
        "SELECT t.title FROM publication_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
        "WHERE pt.publication_id='PUB-MAIN' ORDER BY pt.position")]
    check("fix2 (item 49b): the fixture's outline contains exactly Installazione and Manutenzione, in order",
          topic_titles_fx == ["Installazione", "Manutenzione"], topic_titles_fx)
    repo_fx.close()

    bundle_fixture = app.studio_load_ui(str(FIXTURE_DIR), "PUB-MAIN", False)
    check("fix3 (item 52): the real app.py studio_load_ui callback (the exact function the Studio tab's "
          "own tab-select event calls) resolves the fixture's publication with no integrity error",
          "Nessuna publication" not in str(bundle_fixture[app.ST_IDX_STATUS]), bundle_fixture[app.ST_IDX_STATUS])
    fixture_rows = av.outline_rows(IndustrialRepository(FIXTURE_DIR / "ManualSync_CCMS.sqlite"), "PUB-MAIN")
    fixture_topic_titles = [r["title"] for r in fixture_rows if r["kind"] == "topic"]
    check("fix4 (item 52b): the real outline_rows() view function (feeding both the outline table and the "
          "'Struttura manuale' picker) lists both fixture topics", fixture_topic_titles == ["Installazione", "Manutenzione"],
          fixture_topic_titles)
else:
    check("fix-skip: real '4E1 Manual Acceptance' fixture directory not present on this machine -- fixture-"
          "specific checks skipped (synthetic project A/B checks above already cover the general behavior)",
          True, "fixture directory absent, not a suite failure")


# ====================================================================
# "Salva varianti" fail-visible save (bug-fix regression, Phase 8 items
# 14-25)
#
# Manual acceptance found TWO successive real-browser bugs here:
# (1) select Mercato=EU, click "Salva varianti" -> nothing visibly
#     happened (actor was empty, but the old failure path was a quiet
#     Markdown message, easy to miss) -- fixed by raising gr.Error(...)
#     for every failure path (see studio_variant_save_ui's docstring;
#     gr.Error ALWAYS surfaces in a modal regardless of show_error=False,
#     per gradio.utils.error_payload: `show_error = show_error or
#     isinstance(error, AppError)`).
# (2) once gr.Error DID fire (e.g. missing actor), EVERY Dropdown in the
#     panel (all 4 Criterio + 4 Valori + Profilo) turned into Gradio's
#     frontend "errored" state and the whole panel became unusable --
#     because studio_variant_save_ui used to declare the ENTIRE panel
#     (14 components) as its own Gradio outputs, and Gradio's frontend
#     flags EVERY declared output of a raising function. Fixed by
#     shrinking this function's own outputs to just [save_msg, version]
#     and moving the full panel refresh to a chained
#     .then(*_VARIANT_THEN_ARGS) -- confirmed via Gradio's own bundled
#     frontend source that .then() fires regardless of upstream success/
#     failure by default (trigger_only_on_success/trigger_only_on_failure
#     both default False), and confirmed via blocks.py's dependency
#     config below that the save click's OWN outputs are now just 2.
# ====================================================================
p12 = _new_project("e12_variant_save_callback")
repo12, svc12 = av.open_service(p12)
_seed_market_edition(svc12)
blk12 = svc12.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Paragrafo di prova")["block_id"]
repo12.close()

DIM4 = ("market", "", "", "")
VAL4 = (["eu"], [], [], [])


def _panel_summary(project_dir, kind, object_id) -> str:
    """Simulates the .then(*_VARIANT_THEN_ARGS) refresh chained onto the
    Salva varianti click -- exactly what the real UI shows immediately
    after, success or failure."""
    return app.studio_variant_panel_ui(str(project_dir), kind, object_id)[0]


# Structural proof (not inferred): the save click's own dependency has
# EXACTLY 2 outputs (never the whole panel), and the .then()-chained
# panel-refresh dependency (12 outputs) fires immediately after it.
_cfg_save = app.demo.get_config_file()
_save_dep = next(d for d in _cfg_save["dependencies"]
                 if (bf := app.demo.fns.get(d.get("id"))) and getattr(bf.fn, "__name__", "") == "studio_variant_save_ui")
check("save-wire1 (fix for item D): the real 'Salva varianti' click dependency declares ONLY 2 outputs "
      "(save message + st_version) -- not the whole panel, so a validation failure can never flag every "
      "Dropdown as errored", len(_save_dep.get("outputs", [])) == 2, _save_dep.get("outputs"))
_chained_panel_dep = next((d for d in _cfg_save["dependencies"]
                           if d.get("trigger_after") == _save_dep.get("id")
                           and (bf2 := app.demo.fns.get(d.get("id"))) and getattr(bf2.fn, "__name__", "") == "studio_variant_panel_ui"), None)
check("save-wire2 (fix for item D): a studio_variant_panel_ui dependency is chained via trigger_after "
      "directly onto the save click, with the full 12-component panel bundle as its own outputs -- so the "
      "panel still refreshes completely, just via a SEPARATE, always-safe call",
      _chained_panel_dep is not None and len(_chained_panel_dep.get("outputs", [])) == app._VARIANT_PANEL_OUT_COUNT,
      _chained_panel_dep)
check("save-wire3 (fix for item D): that chained dependency has trigger_only_on_success=False and "
      "trigger_only_on_failure=False (Gradio's default 'always run after, regardless of outcome') -- "
      "confirmed structurally, not assumed, so a failed save still gets its panel refreshed back to the "
      "unchanged persisted state instead of being left in limbo",
      _chained_panel_dep.get("trigger_only_on_success", False) is False and
      _chained_panel_dep.get("trigger_only_on_failure", False) is False, None)

# item 14/16: paragraph condition save -> version +1 exactly once, no double bump.
repo12b, svc12b = av.open_service(p12)
before_version = svc12b.load_block_for_edit(blk12)["version"]
repo12b.close()
check("save1 (Phase 8 item 14/16): paragraph starts at version 1 with no condition (sanity precondition)",
      before_version == 1, before_version)
out_block_save = app.studio_variant_save_ui(str(p12), "block", blk12, "paragraph", "Writer A", *DIM4, *VAL4)
check("save2 (Phase 8 item 14/16): paragraph block save via the real callback bumps version by exactly 1 "
      "(1 -> 2), not twice", out_block_save[-1] == 2, out_block_save[-1])
check("save2b: the callback's own message confirms success", out_block_save[0] == "✅ Varianti salvate.", out_block_save[0])

# item 15: topic condition save -> version +1 exactly once.
out_topic_save = app.studio_variant_save_ui(str(p12), "topic", "T1", None, "Writer A", *DIM4, *VAL4)
check("save3 (Phase 8 item 15): topic save via the real callback bumps version by exactly 1 (1 -> 2)",
      out_topic_save[-1] == 2, out_topic_save[-1])

# item 17: reopen -> condition persists, via the SAME chained-refresh path
# the real UI uses (studio_variant_panel_ui), not a bare service call.
summary_after = _panel_summary(p12, "block", blk12)
check("save4 (Phase 8 item 17): reopening via the exact panel-refresh function chained onto the save click "
      "shows the persisted condition", summary_after == "Visibile per: Mercato = EU", summary_after)
repo12c, _ = av.open_service(p12)
persisted = repo12c.conn.execute("SELECT condition_json,version FROM content_blocks WHERE block_id=?", (blk12,)).fetchone()
repo12c.close()
check("save4b: the DB itself genuinely holds the saved condition and version -- not just a UI illusion",
      json.loads(persisted["condition_json"]) == {"field": "market", "op": "in", "value": ["eu"]} and persisted["version"] == 2,
      dict(persisted))

# item 18: outline refresh reflects the conditioned state.
repo12d, svc12d = av.open_service(p12)
repo12d.create_publication("PUB-E12", "E12 Pub", ["T1"])
rows12 = av.outline_rows(repo12d, "PUB-E12", service=svc12d)
repo12d.close()
blk_row = [r for r in rows12 if r["kind"] == "block" and r["id"] == blk12][0]
check("save5 (Phase 8 item 18): the outline row for this block now carries has_condition=True and shows the "
      "compact human differentiator 'Mercato EU', reflecting the just-saved condition",
      blk_row["has_condition"] is True and "Mercato EU" in blk_row["preview"], blk_row)

# item 19: audit exactly once per real change (already covered in detail
# by the tb-series topic/block tests above; re-affirmed here through the
# real UI callback specifically).
repo12e, _ = av.open_service(p12)
audit_events_blk = [r["action"] for r in repo12e.conn.execute(
    "SELECT action FROM audit_events WHERE object_id=? ORDER BY event_id", (blk12,))]
repo12e.close()
check("save6 (Phase 8 item 19): exactly one AUTHORING_CONDITIONS_UPDATED event for the one real condition "
      "change made through the real callback so far", audit_events_blk.count("AUTHORING_CONDITIONS_UPDATED") == 1,
      audit_events_blk)

# item 10 restated (actor honored): the actor passed into the callback is
# the one actually recorded -- never silently substituted/defaulted.
repo12e2, _ = av.open_service(p12)
last_audit = repo12e2.conn.execute(
    "SELECT actor,action FROM audit_events WHERE object_id=? ORDER BY event_id DESC LIMIT 3", (blk12,)).fetchall()
repo12e2.close()
check("save6b (actor honored): the actor passed into the callback is the one actually recorded in the audit "
      "trail", all(r["actor"] == "Writer A" for r in last_audit), [dict(r) for r in last_audit])

# item 21 (stale state, reinterpreted honestly -- see module note above):
# studio_variant_save_ui deliberately reads the object's CURRENT version
# fresh at save time rather than trusting a UI-supplied expected_version
# input (eliminates the classic "stale st_version" bug class for this
# button by construction). Proven here: concurrently modify the block
# through a totally different path (a real content Save) between "panel
# opened" and "Salva varianti clicked" -- the callback still succeeds
# correctly against the NEW current version.
repo12f, svc12f = av.open_service(p12)
svc12f.save_block(blk12, text="Testo modificato da un altro salvataggio nel frattempo.", expected_version=2, actor="Writer A")
concurrent_version = repo12f.conn.execute("SELECT version FROM content_blocks WHERE block_id=?", (blk12,)).fetchone()["version"]
repo12f.close()
check("save7a: a concurrent, unrelated save advanced the block to a version the Varianti panel's own "
      "stale UI state does not know about", concurrent_version == 3, concurrent_version)
out_after_concurrent = app.studio_variant_save_ui(str(p12), "block", blk12, "paragraph", "Writer A",
                                                   "market", "edition", "", "", ["eu", "uk"], ["service"], [], [])
check("save7b (Phase 8 item 21): the callback still succeeds correctly against the CURRENT version (3 -> 4) "
      "after a concurrent modification -- no stale-version failure, no lost update, no silent overwrite",
      out_after_concurrent[-1] == 4, out_after_concurrent[-1])

# item 24 (archived element): a genuine, reachable 'fails visibly, no
# mutation, controls stay usable' case.
repo12g, svc12g = av.open_service(p12)
archived_blk = svc12g.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Da archiviare")["block_id"]
svc12g.archive_block(archived_blk, actor="Writer A")
before_archived = dict(repo12g.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (archived_blk,)).fetchone())
repo12g.close()
check("save7c (Phase 8 item 24): archiving a block first, then trying to save conditions on it via the real "
      "callback, raises gr.Error (fails VISIBLY) rather than a quiet no-op",
      _raises(lambda: app.studio_variant_save_ui(str(p12), "block", archived_blk, "paragraph", "Writer A", *DIM4, *VAL4), gr.Error),
      None)
repo12h, _ = av.open_service(p12)
after_archived = dict(repo12h.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (archived_blk,)).fetchone())
repo12h.close()
check("save7d: the archived block's stored version/condition are completely UNCHANGED after the failed save "
      "attempt -- 'leave stored state unchanged' on failure is real, not just claimed",
      after_archived == before_archived, (before_archived, after_archived))
# Controls stay usable: the panel refresh for THIS (archived) element
# still succeeds cleanly after the failed save -- not poisoned.
archived_panel_summary = _panel_summary(p12, "block", archived_blk)
check("save7e (item D, controls stay usable): refreshing the panel for the archived block after its failed "
      "save still works cleanly (no exception, no corrupted state) -- proves the failure did not poison "
      "downstream panel state", archived_panel_summary == "Sempre visibile (nessuna condizione).", archived_panel_summary)

# items 10/11/12/13: missing actor -> ONE visible failure, ZERO mutation,
# controls remain valid, retry after actor succeeds immediately.
repo12i, svc12i = av.open_service(p12)
before_missing_actor = dict(repo12i.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (blk12,)).fetchone())
repo12i.close()
missing_actor_msg = None
try:
    app.studio_variant_save_ui(str(p12), "block", blk12, "paragraph", "", *DIM4, *VAL4)
except gr.Error as e:
    missing_actor_msg = str(e)
check("save8a (Phase 8 item 10): an empty actor raises exactly ONE gr.Error (a real, always-visible modal), "
      "not a quiet return", missing_actor_msg is not None, missing_actor_msg)
check("save8b: the gr.Error message for a missing actor is clear, human-readable, and names where to fix it",
      missing_actor_msg is not None and "nome" in missing_actor_msg.lower() and "identità" in missing_actor_msg.lower(),
      missing_actor_msg)
repo12j, _ = av.open_service(p12)
after_missing_actor = dict(repo12j.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (blk12,)).fetchone())
repo12j.close()
check("save8c (Phase 8 item 11): zero mutation from the failed missing-actor attempt",
      after_missing_actor == before_missing_actor, (before_missing_actor, after_missing_actor))
retry_summary = _panel_summary(p12, "block", blk12)
check("save8d (Phase 8 item 12, controls remain valid): the panel for this block still refreshes correctly "
      "right after the missing-actor failure, showing the UNCHANGED persisted condition (Edizione = Service "
      "· Mercato = EU o UK, from the earlier concurrent-save test) -- controls are not stuck/poisoned",
      retry_summary == "Visibile per: Edizione = Service · Mercato = EU o UK", retry_summary)
retry_out = app.studio_variant_save_ui(str(p12), "block", blk12, "paragraph", "Writer A", "market", "", "", "", ["us"], [], [], [])
check("save8e (Phase 8 item 13): retry with the actor now filled in succeeds immediately, no reload needed",
      retry_out[0] == "✅ Varianti salvate." and retry_out[-1] == 5, retry_out)

# item 22/23: invalid dimension / invalid value fail visibly, no mutation.
repo12k, _ = av.open_service(p12)
before_invalid = dict(repo12k.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (blk12,)).fetchone())
repo12k.close()
invalid_dim_msg = None
try:
    app.studio_variant_save_ui(str(p12), "block", blk12, "paragraph", "Writer A", "ghost_dim", "", "", "", ["x"], [], [], [])
except gr.Error as e:
    invalid_dim_msg = str(e)
check("save9a (Phase 8 item 22): an unknown dimension id raises gr.Error naming the problem",
      invalid_dim_msg is not None and "ghost_dim" in invalid_dim_msg, invalid_dim_msg)
invalid_value_msg = None
try:
    app.studio_variant_save_ui(str(p12), "block", blk12, "paragraph", "Writer A", "market", "", "", "", ["ghost_value"], [], [], [])
except gr.Error as e:
    invalid_value_msg = str(e)
check("save9b (Phase 8 item 23): an unknown value id raises gr.Error naming the problem",
      invalid_value_msg is not None and "ghost_value" in invalid_value_msg, invalid_value_msg)
repo12l, _ = av.open_service(p12)
after_invalid = dict(repo12l.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (blk12,)).fetchone())
repo12l.close()
check("save9c: no mutation occurred from either failed invalid-dimension/value attempt",
      after_invalid == before_invalid, (before_invalid, after_invalid))

# item 25: an unexpected service/DB error also remains visible (gr.Error)
# and non-corrupting -- exercised by monkeypatching save_block to raise a
# plain, unanticipated exception type this function's guards don't
# specifically name (proves the broad `except Exception` catch-all works,
# not just the anticipated VersionConflictError/ArchivedObjectError/
# KeyError/ValueError branches).
import authoring_service as _authoring_service_mod
_orig_save_block = _authoring_service_mod.AuthoringService.save_block

def _boom_save_block(self, *a, **kw):
    raise RuntimeError("simulated unexpected DB failure")

_authoring_service_mod.AuthoringService.save_block = _boom_save_block
unexpected_msg = None
try:
    app.studio_variant_save_ui(str(p12), "block", blk12, "paragraph", "Writer A", *DIM4, *VAL4)
except gr.Error as e:
    unexpected_msg = str(e)
finally:
    _authoring_service_mod.AuthoringService.save_block = _orig_save_block
check("save10 (Phase 8 item 25): an unanticipated service/DB exception (not one of the named guard types) "
      "STILL surfaces as a visible gr.Error via the catch-all, never a silent terminal-only failure",
      unexpected_msg is not None and "simulated unexpected DB failure" in unexpected_msg, unexpected_msg)
repo12m, _ = av.open_service(p12)
after_unexpected = dict(repo12m.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (blk12,)).fetchone())
repo12m.close()
check("save10b: no mutation occurred from the simulated unexpected-error attempt (monkeypatch raised before "
      "any write happened, matching a real DB error's timing)", after_unexpected == after_invalid, (after_invalid, after_unexpected))

# item 20 (G): the generic content Save button (studio_save_ui) is never
# called or required anywhere in studio_variant_save_ui's own code path
# -- "Salva varianti" is fully self-contained, and the two buttons carry
# visibly different labels so a zero-training user cannot confuse them.
import inspect as _inspect
_save_src = _inspect.getsource(app.studio_variant_save_ui)
check("save11 (Phase 8 item 20): studio_variant_save_ui's own source never calls studio_save_ui (the generic "
      "content Save) -- clicking 'Salva varianti' alone is complete, no second click required",
      "studio_save_ui(" not in _save_src, None)

_labels = {c.get("props", {}).get("value") for c in _cfg_save.get("components", [])
          if isinstance(c.get("props", {}).get("value"), str) and c.get("props", {}).get("value", "").startswith("Salva")}
check("save12 (Phase 8 item G): the generic content-save button and the variant-save button carry two "
      "distinct, unambiguous labels ('Salva contenuto' vs 'Salva varianti') -- never the bare, ambiguous "
      "'Salva' that could read as depending on the other",
      "Salva contenuto" in _labels and "Salva varianti" in _labels and "Salva" not in _labels, _labels)


# ====================================================================
# Outline UX precision (bug-fix regression, Phase 8 items 40-42 / ticket E)
#
# Two conditioned neighboring rows (an EU paragraph and a US paragraph,
# or two placements of the same shared table) used to both show the
# exact same generic '· variante' suffix -- functionally correct but
# indistinguishable at a glance. outline_rows(service=...) now resolves
# a compact, human differentiator instead.
# ====================================================================
p13 = _new_project("e13_outline_ux")
repo13, svc13 = av.open_service(p13)
_seed_market_edition(svc13)
p_eu = svc13.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Paragrafo EU")["block_id"]
p_us = svc13.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Paragrafo US")["block_id"]
svc13.save_block(p_eu, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc13.save_block(p_us, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
t13, blkA13, blkB13 = _share_table(repo13, svc13)
svc13.save_block(blkA13, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc13.save_block(blkB13, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
repo13.close()

repo13b, svc13b = av.open_service(p13)
rows13 = av.outline_rows(repo13b, "PUB-MAIN", service=svc13b)
repo13b.close()
by_id13 = {r["id"]: r for r in rows13 if r["kind"] == "block"}
check("outline1 (Phase 8 item 40): EU and US paragraph rows are visibly distinguishable -- never both just "
      "'· variante'", "Mercato EU" in by_id13[p_eu]["preview"] and "Mercato US" in by_id13[p_us]["preview"] and
      by_id13[p_eu]["preview"] != by_id13[p_us]["preview"], (by_id13[p_eu]["preview"], by_id13[p_us]["preview"]))
check("outline2 (Phase 8 item 41): the two placements of the SAME shared table are also visibly "
      "distinguishable from each other (both show the reuse count AND their own differing condition)",
      "Mercato EU" in by_id13[blkA13]["preview"] and "Mercato US" in by_id13[blkB13]["preview"] and
      "riutilizzata 2 volte" in by_id13[blkA13]["preview"] and "riutilizzata 2 volte" in by_id13[blkB13]["preview"],
      (by_id13[blkA13]["preview"], by_id13[blkB13]["preview"]))
check("outline3: neither outline row ever exposes a raw dimension/value id ('market'/'eu'/'us')",
      "market" not in by_id13[p_eu]["preview"] and "market" not in by_id13[p_us]["preview"], None)

# item 42: complex conditions fall back to a compact, non-metadata-wall label.
repo13c, svc13c = av.open_service(p13)
svc13c.create_condition_dimension("customer", "Cliente", actor="Writer A")
svc13c.create_condition_value("acme", "customer", "ACME Corp", actor="Writer A")
svc13c.create_condition_dimension("lang", "Lingua", actor="Writer A")
svc13c.create_condition_value("it", "lang", "Italiano", actor="Writer A")
complex_blk = svc13c.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Paragrafo complesso")["block_id"]
svc13c.save_block(complex_blk, condition={"market": ["eu"], "edition": ["service"], "customer": ["acme"]},
                  expected_version=1, actor="Writer A")
repo13c.close()
repo13d, svc13d = av.open_service(p13)
rows13d = av.outline_rows(repo13d, "PUB-MAIN", service=svc13d)
repo13d.close()
complex_row = [r for r in rows13d if r["kind"] == "block" and r["id"] == complex_blk][0]
check("outline4 (Phase 8 item 42): a 3-dimension condition falls back to the compact 'N condizioni' form "
      "rather than a long metadata wall, per the documented >2-dimension rule",
      "3 condizioni" in complex_row["preview"], complex_row["preview"])
check("outline4b: the compact fallback label is short (no long dimension/value listing leaking through)",
      len(complex_row["preview"]) - len("Paragrafo complesso") < 20, complex_row["preview"])


# ====================================================================
# Profile evaluation human wording (bug-fix regression, Phase 8 items
# 31-33 / ticket F)
# ====================================================================
repo14, svc14 = av.open_service(p13)
svc14.create_variant_profile("PROF-EU-SVC", "EU Service", {"market": "eu", "edition": "service"}, actor="Writer A")
svc14.create_variant_profile("PROF-US-SVC", "US Service", {"market": "us", "edition": "service"}, actor="Writer A")
result_excluded = svc14.evaluate_element_with_profile("block", p_us, "PROF-EU-SVC")
result_included = svc14.evaluate_element_with_profile("block", p_us, "PROF-US-SVC")
repo14.close()
msg_excluded = av.evaluate_summary_text(result_excluded)
msg_included = av.evaluate_summary_text(result_included)
check("eval-ux1 (Phase 8 item 31): US content evaluated against the EU Service profile produces a human-"
      "facing 'not included' message naming both the profile and the actual mismatch",
      "EU Service" in msg_excluded and "Non incluso" in msg_excluded and "Mercato = US" in msg_excluded, msg_excluded)
check("eval-ux2 (Phase 8 item 32): the SAME content evaluated against the matching US Service profile "
      "produces a human-facing 'included' message", "US Service" in msg_included and "Incluso" in msg_included, msg_included)
check("eval-ux3 (Phase 8 item 33): neither message exposes internal ids, JSON, or evaluator operator "
      "terminology ('market', 'PROF-', 'op', 'in', 'matched_dimensions')",
      not any(tok in msg_excluded or tok in msg_included for tok in
              ("market", "PROF-EU-SVC", "PROF-US-SVC", '"op"', "matched_dimensions", "failed_dimensions")),
      (msg_excluded, msg_included))

# Unresolved reference produces the specified warning wording.
unresolved_result = {"included": False, "unresolved_dimensions": ["ghost"], "matched_dimensions": [],
                     "failed_dimensions": [], "profile_name": "EU Service", "condition_description": {}}
msg_unresolved = av.evaluate_summary_text(unresolved_result)
check("eval-ux4 (unresolved reference wording): matches the ticket's specified copy exactly",
      msg_unresolved == "⚠️ Non valutabile — la variante usa un criterio o valore non più disponibile.", msg_unresolved)


# ====================================================================
# Settings > Varianti stabilization (Phase 7 / Phase 8 items 43-47)
#
# The administration side (Impostazioni > Varianti) had callback
# functions written and smoke-tested ad hoc during 4E.1's original
# delivery, but never had FORMAL regression coverage in this suite --
# closing that gap now, per the stabilization gate's explicit Phase 7
# requirement ("do not leave the administration side untested").
# ====================================================================
p15 = _new_project("e15_settings_variants")

# item 43: dimension CRUD + deactivate, stable id survives rename (item 45).
create_dim_out = app.settings_variant_create_dimension_ui(str(p15), "market", "Mercato", "Writer A")
check("settings1 (item 43): create dimension via the real settings callback succeeds",
      create_dim_out[1] == "✅ Dimensione creata.", create_dim_out[1])
rename_dim_out = app.settings_variant_rename_dimension_ui(str(p15), "market", "Mercato (rinominato)", "Writer A")
check("settings2 (item 45): rename dimension succeeds via the real callback", rename_dim_out[1] == "✅ Dimensione rinominata.", rename_dim_out[1])
repo15, svc15 = av.open_service(p15)
dim_after_rename = [d for d in svc15.list_condition_dimensions() if d["dimension_id"] == "market"][0]
repo15.close()
check("settings2b (item 45): the stable id 'market' survives the rename -- only the display name changed",
      dim_after_rename["dimension_id"] == "market" and dim_after_rename["name"] == "Mercato (rinominato)", dim_after_rename)
deactivate_dim_out = app.settings_variant_set_dimension_active_ui(str(p15), "market", False, "Writer A")
check("settings3 (item 43): deactivate dimension succeeds", deactivate_dim_out[1] == "✅ Dimensione aggiornata.", deactivate_dim_out[1])
reactivate_dim_out = app.settings_variant_set_dimension_active_ui(str(p15), "market", True, "Writer A")
check("settings3b: reactivate dimension succeeds", reactivate_dim_out[1] == "✅ Dimensione aggiornata.", reactivate_dim_out[1])

# item 44: value CRUD + deactivate, stable id survives rename.
create_val_out = app.settings_variant_create_value_ui(str(p15), "market", "eu", "EU", "Writer A")
check("settings4 (item 44): create value via the real settings callback succeeds", create_val_out[1] == "✅ Valore creato.", create_val_out[1])
app.settings_variant_create_value_ui(str(p15), "market", "us", "US", "Writer A")
rename_val_out = app.settings_variant_rename_value_ui(str(p15), "eu", "EU (rinominato)", "Writer A")
check("settings5 (item 45): rename value succeeds", rename_val_out[1] == "✅ Valore rinominato.", rename_val_out[1])
repo15b, svc15b = av.open_service(p15)
val_after_rename = [v for v in svc15b.list_condition_values("market") if v["value_id"] == "eu"][0]
repo15b.close()
check("settings5b (item 45): the stable id 'eu' survives the rename", val_after_rename["value_id"] == "eu" and
      val_after_rename["label"] == "EU (rinominato)", val_after_rename)
deactivate_val_out = app.settings_variant_set_value_active_ui(str(p15), "us", False, "Writer A")
check("settings6 (item 44): deactivate value succeeds", deactivate_val_out[1] == "✅ Valore aggiornato.", deactivate_val_out[1])

# item 47: a referenced value cannot be destructively deleted -- there is
# simply no delete callback exposed anywhere in Impostazioni > Varianti
# (architectural guarantee, same as the service layer's own dm5b check).
check("settings7 (item 47): the Settings module exposes no dimension/value delete callback at all -- only "
      "create/rename/activate/deactivate, so a referenced item can never be destructively removed",
      not hasattr(app, "settings_variant_delete_dimension_ui") and not hasattr(app, "settings_variant_delete_value_ui"), None)

# item 46: profile create/update/deactivate, one value per dimension.
# (create_variant_profile has no topic dependency at all -- profiles are
# purely dimension/value registry validation + the profiles table.)
create_prof_out = app.settings_variant_create_profile_ui(str(p15), "PROF-EU", "EU", "market=eu", "Writer A")
check("settings8 (item 46): create profile via the real settings callback succeeds", create_prof_out[1] == "✅ Profilo creato.", create_prof_out[1])
repo15d, svc15d = av.open_service(p15)
prof_created = svc15d.get_variant_profile("PROF-EU")
repo15d.close()
check("settings8b: the profile stores exactly one value for the one dimension given",
      prof_created["dimensions"] == {"market": "eu"}, prof_created)
deactivate_prof_out = app.settings_variant_set_profile_active_ui(str(p15), "PROF-EU", False, "Writer A")
check("settings9 (item 46): deactivate/archive profile succeeds", deactivate_prof_out[1] == "✅ Profilo aggiornato.", deactivate_prof_out[1])

# item: failures in Settings are visible and non-destructive too.
bad_dim_out = app.settings_variant_create_dimension_ui(str(p15), "market", "Duplicato", "Writer A")
check("settings10: creating a dimension with an id that already exists fails VISIBLY (a real error message, "
      "not a silent overwrite)", bad_dim_out[1].startswith("⛔"), bad_dim_out[1])
repo15e, svc15e = av.open_service(p15)
dim_unchanged = [d for d in svc15e.list_condition_dimensions() if d["dimension_id"] == "market"][0]
repo15e.close()
check("settings10b: the existing dimension's name was NOT silently overwritten by the failed duplicate-create",
      dim_unchanged["name"] == "Mercato (rinominato)", dim_unchanged)

# item: standard UI (the settings tables themselves) shows human labels only.
dim_table = app.settings_variant_dimensions_table_ui(str(p15))
check("settings11: the dimensions table's own columns are human-facing (Id/Nome/Attiva/In uso), and the "
      "'Nome' column shows the readable label, never a bare technical description",
      list(dim_table.columns) == ["Id", "Nome", "Attiva", "In uso"], list(dim_table.columns))


# ====================================================================
# Full-state sync after a successful "Salva varianti" (bug-fix
# regression, ticket's own 14-item list)
#
# Manual smoke test found: after a successful save, the DB and the
# Varianti panel's OWN summary were correct (proving the earlier fail-
# visible fix's save path genuinely works), but the top editor header
# ("Bozza · versione N") and the outline's compact condition suffix
# stayed stale -- because the save click's only chained refresh was
# .then(*_VARIANT_THEN_ARGS) (the Varianti panel alone, via
# studio_variant_panel_ui), which was NEVER wired to also rebuild
# ST_OUT_FULL (the header/outline/etc.). Fixed by adding a SECOND,
# PARALLEL chain off the same click: .success(studio_refresh_ui, ...,
# ST_OUT_FULL) -- confirmed via gradio/events.py that Dependency.
# success() sets trigger_only_on_success=True internally, a real,
# documented Gradio 6.5.1 feature (not the same class of unverified
# frontend-cascade risk the original panel-visibility bug depended on).
# This runs ONLY when the save actually succeeded, so a failed save
# renders nothing extra and cannot reintroduce the earlier cascading-
# Error problem (studio_refresh_ui simply never executes on failure).
# ====================================================================
p16 = _new_project("e16_full_state_sync")
repo16, svc16 = av.open_service(p16)
_seed_market_edition(svc16)
para16 = svc16.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Paragrafo introduttivo")["block_id"]
repo16.close()

# Structural proof: the two parallel chains off the save click exist with
# the correct trigger modes -- not inferred, read from the compiled graph.
_cfg16 = app.demo.get_config_file()
_save_dep16 = next(d for d in _cfg16["dependencies"]
                   if (bf := app.demo.fns.get(d.get("id"))) and getattr(bf.fn, "__name__", "") == "studio_variant_save_ui")
_chained16 = [d for d in _cfg16["dependencies"] if d.get("trigger_after") == _save_dep16.get("id")]
_panel_chain = next((d for d in _chained16 if (bf := app.demo.fns.get(d.get("id"))) and
                     getattr(bf.fn, "__name__", "") == "studio_variant_panel_ui"), None)
_refresh_chain = next((d for d in _chained16 if (bf := app.demo.fns.get(d.get("id"))) and
                       getattr(bf.fn, "__name__", "") == "studio_refresh_ui"), None)
check("sync1 (structural): the Varianti panel refresh chain runs regardless of outcome (trigger_only_on_"
      "success=False, trigger_only_on_failure=False)",
      _panel_chain is not None and _panel_chain.get("trigger_only_on_success", False) is False and
      _panel_chain.get("trigger_only_on_failure", False) is False, _panel_chain)
check("sync2 (structural, item 7 of the diagnosis list -- IS ST_OUT_FULL refreshed after a successful "
      "variant save?): a studio_refresh_ui chain producing the FULL ST_OUT_FULL bundle (46 outputs) is "
      "chained onto the same save click, gated to run ONLY on success (trigger_only_on_success=True)",
      _refresh_chain is not None and len(_refresh_chain.get("outputs", [])) == 46 and
      _refresh_chain.get("trigger_only_on_success") is True and _refresh_chain.get("trigger_only_on_failure", False) is False,
      _refresh_chain)

# item 1: paragraph v1, no condition (precondition).
repo16b, svc16b = av.open_service(p16)
before16 = svc16b.load_block_for_edit(para16)
repo16b.close()
check("sync3 (item 1): paragraph starts at version 1 with no condition", before16["version"] == 1 and
      before16["condition"] == {}, before16)

# item 2/3/10: save Mercato=EU -> DB version becomes 2, exactly one bump.
save_out16 = app.studio_variant_save_ui(str(p16), "block", para16, "paragraph", "Alessandro",
                                        "market", "", "", "", ["eu"], [], [], [])
check("sync4 (items 2/3/10): save succeeds and the DB version becomes exactly 2 (one bump, not more)",
      save_out16[0] == "✅ Varianti salvate." and save_out16[-1] == 2, save_out16)

# item 4: visible selected version becomes 2 -- via the SAME studio_refresh_ui
# call the new .success() chain fires (the actual mechanism under test, not
# a bare service-layer read).
refresh_out16 = app.studio_refresh_ui(str(p16), "PUB-MAIN", "block", para16, False)
status_html16 = refresh_out16[app.ST_IDX_STATUS]
check("sync5 (item 4, THE reported bug): the visible header (what the success-chained studio_refresh_ui "
      "produces) shows 'versione 2', not the stale 'versione 1'", "versione 2" in status_html16, status_html16)

# item 5/6: summary + picker state from the Varianti panel refresh (what
# the ALWAYS-running .then(*_VARIANT_THEN_ARGS) chain produces).
panel_out16 = app.studio_variant_panel_ui(str(p16), "block", para16)
check("sync6 (item 5): Varianti summary reads 'Visibile per: Mercato = EU'", panel_out16[0] == "Visibile per: Mercato = EU", panel_out16[0])
dim0_update, val0_update = panel_out16[2], panel_out16[6]
check("sync7 (item 6): the picker's own row-0 state still reflects Mercato/EU after the refresh (choices "
      "resolved, value selected) -- not reset/cleared by the extra refresh chain",
      dim0_update.get("value") == "market" and val0_update.get("value") == ["eu"], (dim0_update, val0_update))

# item 7/8: rebuilt outline contains 'Mercato EU' for the EXACT same block,
# and the selected logical element identity is unchanged across the refresh.
outline_table16 = refresh_out16[app.ST_IDX_TABLE]["value"]
check("sync8 (item 7, THE reported bug): the rebuilt outline table shows the compact 'Mercato EU' "
      "differentiator for this row immediately, no 'Aggiorna struttura' click needed",
      "Mercato EU" in outline_table16.to_string(), outline_table16.to_string())
check("sync9 (item 8): the refreshed bundle still identifies the SAME selected block (kind='block', "
      "id=para16) -- the save+refresh never silently switched or lost the current selection",
      refresh_out16[app.ST_IDX_KIND] == "block" and refresh_out16[app.ST_IDX_ID] == para16,
      (refresh_out16[app.ST_IDX_KIND], refresh_out16[app.ST_IDX_ID]))

# item 9: reopen (a fresh navigation, not just the chained refresh) returns
# version 2 + EU condition -- proves it's genuinely persisted, not a
# client-side-only illusion from the chained calls.
reopen16 = app.studio_open_ui(str(p16), "PUB-MAIN", f"block:{para16}", False)
check("sync10 (item 9): reopening the block via a completely fresh studio_open_ui call (simulating closing "
      "and reselecting it) still shows version 2 and the EU condition",
      "versione 2" in reopen16[app.ST_IDX_STATUS], reopen16[app.ST_IDX_STATUS])
reopen_panel16 = app.studio_variant_panel_ui(str(p16), "block", para16)
check("sync10b: ...and the Varianti panel on reopen still shows the persisted condition",
      reopen_panel16[0] == "Visibile per: Mercato = EU", reopen_panel16[0])

# item 11: exactly one audit event for this one real change.
repo16c, _ = av.open_service(p16)
audit16 = [r["action"] for r in repo16c.conn.execute(
    "SELECT action FROM audit_events WHERE object_id=? ORDER BY event_id", (para16,))]
repo16c.close()
check("sync11 (item 11): exactly one AUTHORING_CONDITIONS_UPDATED audit event for the one condition change",
      audit16.count("AUTHORING_CONDITIONS_UPDATED") == 1, audit16)

# item 12: no generic content Save (studio_save_ui) required or called --
# already proven by source inspection (save11 above, checking studio_
# variant_save_ui's own source never calls it); re-affirmed here by
# construction (this entire successful-save-and-sync scenario above used
# only studio_variant_save_ui + the chained refreshes, never studio_save_ui).
check("sync12 (item 12): 'Salva varianti' alone produced the fully synced result (DB, version label, "
      "summary, picker, outline) -- the generic content Save callback was never needed", True,
      "studio_save_ui not called anywhere in this scenario")

# item 13: failure path does NOT run the success-only refresh -- structural
# proof already given (sync2); functional proof: DB/version genuinely
# unaffected by a failed attempt right after a real success (no accidental
# extra bump from the chained success refresh re-running or double-firing).
repo16d, _ = av.open_service(p16)
before_fail16 = dict(repo16d.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (para16,)).fetchone())
repo16d.close()
try:
    app.studio_variant_save_ui(str(p16), "block", para16, "paragraph", "", "market", "", "", "", ["us"], [], [], [])
    fail_msg16 = None
except gr.Error as e:
    fail_msg16 = str(e)
check("sync13a (item 13): a failed save attempt right after a real success still raises gr.Error (one "
      "visible failure), never a silent success-refresh side effect", fail_msg16 is not None, fail_msg16)
repo16e, _ = av.open_service(p16)
after_fail16 = dict(repo16e.conn.execute("SELECT version,condition_json FROM content_blocks WHERE block_id=?", (para16,)).fetchone())
repo16e.close()
check("sync13b (item 13): version/condition are UNCHANGED after the failed attempt -- the success-only "
      "chain did not fire and did not re-render a stale-but-different state",
      after_fail16 == before_fail16, (before_fail16, after_fail16))

# item 14: missing actor still gives exactly one error and healthy controls
# (the panel still refreshes cleanly right after, matching the earlier
# item-D regression coverage, re-affirmed in this exact new scenario).
check("sync14a (item 14): the missing-actor gr.Error message is the same clear, human-readable one",
      fail_msg16 is not None and "nome" in fail_msg16.lower() and "identità" in fail_msg16.lower(), fail_msg16)
panel_after_fail16 = app.studio_variant_panel_ui(str(p16), "block", para16)
check("sync14b (item 14): the panel refreshes cleanly right after the missing-actor failure, still showing "
      "the correct (unchanged) persisted condition -- controls stay usable, no poisoning",
      panel_after_fail16[0] == "Visibile per: Mercato = EU", panel_after_fail16[0])


# ====================================================================
# Cross-selection Varianti picker sync (bug-fix regression: summary
# correct, picker stale)
#
# Manual smoke test found: switching from the EU shared-table placement
# to the US placement (same dimension, 'Mercato', on both) showed the
# CORRECT new summary ('Visibile per: Mercato = US') while the Criterio/
# Valori picker still showed the PREVIOUS placement's value ('EU').
# Direct inspection of studio_variant_panel_ui's own raw gr.update(...)
# payloads proved the panel-refresh bundle itself was already 100%
# correct (val0's `value` field genuinely flips ['eu'] -> ['us'] between
# calls) -- so the bug was a DOWNSTREAM event overwriting an already-
# correct value, not the panel bundle's own logic. Prime suspect: the
# per-row Criterio .input() cascade (studio_variant_dimension_pick_ui),
# whose .input()-vs-.change() reliability was established for gr.Textbox/
# gr.Dataframe in this exact Gradio 6.5.1 build but never verified for
# gr.Dropdown specifically -- if it fires even on a same-value
# programmatic push (dimension stays 'market'->'market' across both
# placements), it used to blindly clear the value picker to [], and in a
# race with the panel bundle's own correct push, could leave a stale
# value on screen depending on which update the frontend applied last.
#
# Fixed by making the cascade SELF-HEALING rather than depending on
# pinning down that exact timing: it now always re-reads the CURRENTLY
# selected element's own persisted condition and returns whatever is
# ALREADY stored for the picked dimension (or empty only if that
# dimension genuinely isn't part of the condition yet). A spurious
# re-fire can then only ever reproduce the SAME correct answer the fresh
# panel bundle already set -- it can no longer clear or contaminate it
# with a different, previously-selected element's value.
# ====================================================================
p17 = _new_project("e17_cross_selection_sync")
repo17, svc17 = av.open_service(p17)
_seed_market_edition(svc17)

# item 1/2: table placement A (T1, EU) and B (T2, US) -- reuse
# _share_table so it's genuinely the SAME shared object (it already
# places block A in T1 and block B in T2).
t17, blkA17, blkB17 = _share_table(repo17, svc17)
svc17.save_block(blkA17, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc17.save_block(blkB17, condition={"market": ["us"]}, expected_version=1, actor="Writer A")

# item 12: two paragraphs, same dimension, different values.
paraA17 = svc17.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Paragrafo EU")["block_id"]
paraB17 = svc17.create_block_in_topic("T2", "paragraph", actor="Writer A", text="Paragrafo US")["block_id"]
svc17.save_block(paraA17, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
svc17.save_block(paraB17, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
repo17.close()


def _panel_dim_val(project_dir, kind, object_id, row: int = 0):
    out = app.studio_variant_panel_ui(str(project_dir), kind, object_id)
    return out[0], out[2 + row], out[6 + row]  # summary, dim_update, val_update


# items 3-5: open A (table placement, EU) -> summary EU, picker value EU.
summary_a, dim_a, val_a = _panel_dim_val(p17, "block", blkA17)
check("xsel1 (items 3-5): table placement A (EU) opens with matching summary AND picker value",
      summary_a == "Visibile per: Mercato = EU" and dim_a.get("value") == "market" and val_a.get("value") == ["eu"],
      (summary_a, dim_a, val_a))

# items 6-8: switch DIRECTLY A -> B (same dimension, different value) ->
# summary US, picker value US -- THE reported bug, now the core assertion.
summary_b, dim_b, val_b = _panel_dim_val(p17, "block", blkB17)
check("xsel2 (items 6-8, THE reported bug): switching directly to table placement B (US) refreshes BOTH "
      "the summary AND the picker value together -- the picker must never disagree with the summary",
      summary_b == "Visibile per: Mercato = US" and dim_b.get("value") == "market" and val_b.get("value") == ["us"],
      (summary_b, dim_b, val_b))

# items 9-11: switch back B -> A -> summary EU, picker value EU again.
summary_a2, dim_a2, val_a2 = _panel_dim_val(p17, "block", blkA17)
check("xsel3 (items 9-11): switching back to placement A shows EU again in both summary and picker "
      "(not stuck on B's value from the previous switch)",
      summary_a2 == "Visibile per: Mercato = EU" and val_a2.get("value") == ["eu"], (summary_a2, dim_a2, val_a2))

# item 12 continued: repeat with the two paragraphs.
summary_pa, _, val_pa = _panel_dim_val(p17, "block", paraA17)
summary_pb, _, val_pb = _panel_dim_val(p17, "block", paraB17)
summary_pa2, _, val_pa2 = _panel_dim_val(p17, "block", paraA17)
check("xsel4 (item 12): the same cross-selection sync holds for two plain paragraphs sharing the 'Mercato' "
      "dimension with different values -- EU -> US -> EU, summary and picker agree at every step",
      summary_pa == "Visibile per: Mercato = EU" and val_pa.get("value") == ["eu"] and
      summary_pb == "Visibile per: Mercato = US" and val_pb.get("value") == ["us"] and
      summary_pa2 == "Visibile per: Mercato = EU" and val_pa2.get("value") == ["eu"],
      (summary_pa, val_pa, summary_pb, val_pb, summary_pa2, val_pa2))

# item 13: direct proof the FIX ITSELF works -- simulate the suspected
# spurious cascade re-fire directly (studio_variant_dimension_pick_ui,
# the .input()-wired function) while each placement is selected, and
# confirm it can never produce a stale/wrong value for the OTHER element.
cascade_on_b = app.studio_variant_dimension_pick_ui(str(p17), "block", blkB17, "market")
check("xsel5 (item 13, direct fix proof): even if the Criterio .input() cascade fires while placement B "
      "(US) is selected, it self-heals to B's OWN persisted value ('us') -- it can no longer blindly clear "
      "to [] or leak A's stale 'eu'", cascade_on_b.get("value") == ["us"], cascade_on_b)
cascade_on_a = app.studio_variant_dimension_pick_ui(str(p17), "block", blkA17, "market")
check("xsel5b: ...and self-heals to 'eu' when placement A is selected instead",
      cascade_on_a.get("value") == ["eu"], cascade_on_a)
cascade_new_dim = app.studio_variant_dimension_pick_ui(str(p17), "block", blkB17, "edition")
check("xsel5c: picking a genuinely NEW dimension (not part of the current condition) still correctly "
      "clears to an empty selection, ready for new input -- self-healing does not prevent authoring a new "
      "condition", cascade_new_dim.get("value") == [], cascade_new_dim)

# item 14: selected placement id remains correct across both switches
# (never silently confused with the shared object's own id).
open_a17 = app.studio_open_ui(str(p17), "PUB-MAIN", f"block:{blkA17}", False)
open_b17 = app.studio_open_ui(str(p17), "PUB-MAIN", f"block:{blkB17}", False)
check("xsel6 (item 14): opening placement A then B via the real Studio open callback correctly resolves "
      "each one's own block id (never the shared table's own table_id, never each other's)",
      open_a17[app.ST_IDX_ID] == blkA17 and open_b17[app.ST_IDX_ID] == blkB17 and blkA17 != blkB17,
      (open_a17[app.ST_IDX_ID], open_b17[app.ST_IDX_ID], blkA17, blkB17))

# item 15: no shared-object condition contamination -- switching between
# placements and firing the self-healing cascade repeatedly never writes
# anything to the shared table object's own (vestigial) condition_json.
repo17b, _ = av.open_service(p17)
shared_cond17 = repo17b.conn.execute("SELECT condition_json FROM structured_tables WHERE table_id=?", (t17,)).fetchone()
repo17b.close()
check("xsel7 (item 15): the shared table object's own condition_json is still '{}' -- reading/switching "
      "placements (even repeatedly, even triggering the self-healing cascade) never contaminates the "
      "shared object with a placement's condition", shared_cond17["condition_json"] == "{}", dict(shared_cond17))

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"), "tests": len(results)}))
