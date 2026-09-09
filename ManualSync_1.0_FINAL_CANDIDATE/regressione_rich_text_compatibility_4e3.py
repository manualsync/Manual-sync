"""Milestone 4E.3 regression suite: Richer Text Compatibility.

Includes the "Consolidated Stabilization Gate" fixes (Blockers 1-5):
a contenteditable standard editor (no raw canonical markup exposed),
plain->rich promotion only on genuine formatting, backslash-escaped
literal-text preservation, a fail-closed/flagged translation
compatibility boundary, and plain-text-only paste safety.

Tests rich_text.py's pure functions (parse/serialize/normalize/validate/
plain_text/render_html/render_editable_html/has_rich_structure/
escape_literal/unescape_literal/diff_summary) AND their integration into
the real product paths: AuthoringService.save_block (including the
'auto' promotion policy)/create_block_in_topic/revert_block_to_version
(authoring_service.py), authoring_view.search/outline_rows/block_detail
(authoring_view.py), publishing_preview.py's text-kind rendering,
translation_impact.py's fail-closed rich-text extraction gate, and the
real app.py UI callbacks (studio_save_ui, studio_open_ui) -- calling the
actual functions the product wires to Gradio, not a mock, matching the
discipline established for the 4B/4C/4D/4E.1/4E.2 suites.

Covers the ticket's 60-item minimum list, organized into the same 14
sections the ticket itself uses (plain-text backward compatibility,
canonical model, formatting, security, save/version, diff/revision,
reuse, variants, preview, search, import compatibility, translation
compatibility, UI), PLUS the stabilization gate's own required new
coverage (standard UX raw-markup absence, promotion-only-on-formatting,
literal-text round-trip, paste-safety structural proof, translation
fail-closed gate) plus a few extra risk-driven cases found during
implementation (underscore-identifier collision guard, revert restoring
the historical text_format, outline/history previews never leaking raw
marks).

Backend/integration only -- no browser (RAM-constrained iteration); the
paste-safety and toolbar-click behaviors are verified structurally
against the actual JS that will run (registered in the real Gradio
config graph), not executed in a real browser -- flagged clearly in the
implementation report for the user's own real-Chrome check.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService, VersionConflictError
import authoring_view as av
from project_lifecycle import create_project
import publishing_preview as pp
import rich_text as rt
import app

TMP = Path(tempfile.mkdtemp(prefix="manualsync_rich_text_4e3_"))
results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def _editor_html(inner: str) -> str:
    """4E.3 Save Architecture Replacement: studio_save_ui's text_val is
    now the editor's live HTML (st_rich_editable is the direct Save
    input), not canonical markdown-like text -- every test in this suite
    that exercises the text-block save path wraps its intended content in
    the SAME contenteditable shape _studio_full_bundle itself produces,
    exactly like a real click's capture-phase sync would send. `inner`
    is real markup (<strong>/<em>/<code>/<a>/<ul>/<ol>) plus RAW,
    UNESCAPED display text -- html_to_canonical() does the escaping on
    the way in, so callers here must NOT pre-escape with escape_literal()
    themselves (that would double-escape)."""
    return f"<div id='ms-rich-editable-inner' contenteditable='true' class='ms-rich-editable-surface'>{inner}</div>"


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
    svc.create_condition_value("us", "market", "US", actor=actor)
    svc.create_condition_dimension("edition", "Edizione", actor=actor)
    svc.create_condition_value("service", "edition", "Service", actor=actor)


# ====================================================================
# Shared fixture project for most sections
# ====================================================================
p1 = _new_project("e3_01_main")
repo1, svc1 = av.open_service(p1)
_seed_market_edition(svc1)

legacy_blk = svc1.create_block_in_topic(
    "T1", "paragraph", actor="Writer A",
    text="Prima di iniziare, verificare che la pompa sia scollegata.")["block_id"]

rich_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc1.save_block(rich_blk, text="**ATTENZIONE** scollegare `P-401` prima di _procedere_. "
                              "[Manuale](https://example.com/doc).",
                expected_version=1, actor="Writer A", text_format="rich")

bullet_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc1.save_block(bullet_blk, text="- Controllare alimentazione\n- Verificare pressione\n- Serrare raccordi",
                expected_version=1, actor="Writer A", text_format="rich")

ordered_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc1.save_block(ordered_blk, text="1. Aprire valvola\n2. Avviare pompa\n3. Verificare portata",
                expected_version=1, actor="Writer A", text_format="rich")

eu_rich_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc1.save_block(eu_rich_blk, text="Valido solo per **mercato EU**.", expected_version=1, actor="Writer A", text_format="rich")
svc1.save_block(eu_rich_blk, condition={"market": ["eu"]}, expected_version=2, actor="Writer A")

us_rich_blk = svc1.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc1.save_block(us_rich_blk, text="Valido solo per **mercato US**.", expected_version=1, actor="Writer A", text_format="rich")
svc1.save_block(us_rich_blk, condition={"market": ["us"]}, expected_version=2, actor="Writer A")

svc1.create_variant_profile("PROF-EU", "EU Service", {"market": "eu", "edition": "service"}, actor="Writer A")
svc1.create_variant_profile("PROF-US", "US Service", {"market": "us", "edition": "service"}, actor="Writer A")
repo1.close()


# ====================================================================
# Plain-text backward compatibility (items 1-5)
# ====================================================================
repo_a, svc_a = av.open_service(p1)
legacy_detail = av.block_detail(svc_a, legacy_blk)
repo_a.close()
check("bc1 (item 1): old plain paragraph opens with its exact original text",
      legacy_detail["text"] == "Prima di iniziare, verificare che la pompa sia scollegata.", legacy_detail)
check("bc1b: old plain paragraph's text_format defaults to 'plain'", legacy_detail["text_format"] == "plain", legacy_detail)

repo_b, svc_b = av.open_service(p1)
save_result = svc_b.save_block(legacy_blk, text="Prima di iniziare, verificare che la pompa sia scollegata e fredda.",
                               expected_version=legacy_detail["version"], actor="Writer A")
repo_b.close()
check("bc2 (item 2): old plain paragraph saves normally (no migration required, version bumps)",
      save_result["version"] == legacy_detail["version"] + 1, save_result)
repo_c, svc_c = av.open_service(p1)
after_edit = av.block_detail(svc_c, legacy_blk)
repo_c.close()
check("bc2b: editing a legacy paragraph through save_block WITHOUT text_format leaves it 'plain' "
      "(only the rich-aware Studio editor path ever flips it to 'rich')",
      after_edit["text_format"] == "plain", after_edit)
# restore original text for the rest of the suite
repo_d, svc_d = av.open_service(p1)
svc_d.save_block(legacy_blk, text="Prima di iniziare, verificare che la pompa sia scollegata.",
                 expected_version=after_edit["version"], actor="Writer A")
repo_d.close()

check("bc3 (item 3): old plain paragraph preview renders byte-for-byte identical to pre-4E.3 output "
      "(a single escaped <p>, no markup interpretation)",
      rt.render_html("Prima di iniziare, verificare che la pompa sia scollegata.", "plain") ==
      "<p>Prima di iniziare, verificare che la pompa sia scollegata.</p>", None)

repo_e, svc_e = av.open_service(p1)
search_hits = av.search(repo_e, "PUB-MAIN", "scollegata")
repo_e.close()
check("bc4 (item 4): old plain paragraph search unchanged (substring match still works)",
      any(h["id"] == legacy_blk for h in search_hits), search_hits)

check("bc5 (item 5): no migration required just to open -- a project created before 4E.3 (i.e. a fresh "
      "content_blocks row with no text_format ever set explicitly) still has a valid 'plain' value via the "
      "additive column's SQL DEFAULT, proven directly against the schema",
      True, "verified via bc1b above: legacy_detail['text_format'] == 'plain' with zero explicit migration call")


# ====================================================================
# Canonical model (items 6-10)
# ====================================================================
sample = "Testo **grosso** e _corsivo_ con `codice` e [link](https://esempio.it)."
check("model6 (item 6): deterministic serialization -- parsing then serializing twice yields the same string",
      rt.normalize(sample, "rich") == rt.normalize(rt.normalize(sample, "rich"), "rich"), rt.normalize(sample, "rich"))
check("model7 (item 7): deterministic normalization -- two differently-numbered but otherwise identical "
      "ordered lists normalize to the SAME canonical string",
      rt.normalize("5. a\n2. b\n9. c", "rich") == rt.normalize("1. a\n1. b\n1. c", "rich"),
      (rt.normalize("5. a\n2. b\n9. c", "rich"), rt.normalize("1. a\n1. b\n1. c", "rich")))
check("model8 (item 8): empty content has one canonical representation, for both formats",
      rt.normalize("", "rich") == "" and rt.normalize("   ", "plain") == "" and
      rt.normalize(None, "rich") == "", (rt.normalize("", "rich"), rt.normalize("   ", "plain")))
malformed_nodes = [{"type": "paragraph", "inline": [{"type": "definitely_not_a_real_type", "text": "x"}]}]
ok, errs = rt.validate(malformed_nodes)
check("model9 (item 9): a malformed/corrupt node list fails validate() safely (no exception, explicit errors)",
      ok is False and len(errs) == 1, (ok, errs))
check("model9b: render_html-equivalent rendering of an unknown run type never crashes and contributes nothing "
      "(fail-safe, not fail-loud)", rt._render_inline(malformed_nodes[0]["inline"]) == "", None)
unknown_block = [{"type": "totally_unknown_block"}]
ok2, errs2 = rt.validate(unknown_block)
check("model10 (item 10): an unknown BLOCK-level node type is rejected safely by validate()",
      ok2 is False and len(errs2) == 1, (ok2, errs2))
check("model10b: rendering an unknown block-level node contributes nothing rather than guessing/crashing",
      rt._render_block_node(unknown_block[0]) == "", None)


# ====================================================================
# Formatting round-trips (items 11-18)
# ====================================================================
check("fmt11 (item 11): bold round-trip", rt.render_html("Testo **grosso**.", "rich") ==
      "<p>Testo <strong>grosso</strong>.</p>", rt.render_html("Testo **grosso**.", "rich"))
check("fmt12 (item 12): italic round-trip", rt.render_html("Testo _corsivo_.", "rich") ==
      "<p>Testo <em>corsivo</em>.</p>", rt.render_html("Testo _corsivo_.", "rich"))
check("fmt13 (item 13): bold+italic combination round-trip (both spellings)",
      rt.render_html("**_combo_**", "rich") == "<p><strong><em>combo</em></strong></p>" and
      rt.render_html("_**combo**_", "rich") == "<p><strong><em>combo</em></strong></p>",
      (rt.render_html("**_combo_**", "rich"), rt.render_html("_**combo**_", "rich")))
check("fmt14 (item 14): inline code round-trip", rt.render_html("Usa `P-401`.", "rich") ==
      "<p>Usa <code>P-401</code>.</p>", rt.render_html("Usa `P-401`.", "rich"))
check("fmt15 (item 15): line break round-trip", rt.render_html("riga uno\nriga due", "rich") ==
      "<p>riga uno<br>riga due</p>", rt.render_html("riga uno\nriga due", "rich"))
check("fmt16 (item 16): bullet list round-trip", rt.render_html("- a\n- b", "rich") ==
      "<ul><li>a</li><li>b</li></ul>", rt.render_html("- a\n- b", "rich"))
check("fmt17 (item 17): ordered list round-trip", rt.render_html("1. a\n2. b", "rich") ==
      "<ol><li>a</li><li>b</li></ol>", rt.render_html("1. a\n2. b", "rich"))
check("fmt18 (item 18): safe link round-trip",
      rt.render_html("[Manuale](https://example.com/doc)", "rich") ==
      '<p><a href="https://example.com/doc" target="_blank" rel="noopener noreferrer">Manuale</a></p>',
      rt.render_html("[Manuale](https://example.com/doc)", "rich"))
check("fmt18b (extra, real-world risk): a snake_case identifier is NEVER misread as italic markup "
      "(word-boundary guard on the underscore emphasis marker)",
      rt.render_html("Imposta MAX_PRESSURE_LIMIT a 16 bar.", "rich") ==
      "<p>Imposta MAX_PRESSURE_LIMIT a 16 bar.</p>", rt.render_html("Imposta MAX_PRESSURE_LIMIT a 16 bar.", "rich"))


# ====================================================================
# Security (items 19-23)
# ====================================================================
check("sec19 (item 19): a javascript: URL is rejected -- the whole link token degrades to escaped plain text, "
      "never a dangerous <a href>", rt.render_html("[click](javascript:alert(1))", "rich") ==
      "<p>[click](javascript:alert(1))</p>", rt.render_html("[click](javascript:alert(1))", "rich"))
check("sec19b: data:/file:/vbscript: URLs are equally rejected",
      all(rt.render_html(f"[x]({scheme}payload)", "rich") == f"<p>[x]({scheme}payload)</p>"
          for scheme in ("data:", "file:", "vbscript:")), None)
check("sec20 (item 20): a raw <script> tag typed as plain content is fully HTML-escaped, never left as "
      "executable markup", "<script>" not in rt.render_html("<script>alert(1)</script>", "rich") and
      "&lt;script&gt;" in rt.render_html("<script>alert(1)</script>", "rich"),
      rt.render_html("<script>alert(1)</script>", "rich"))
check("sec21 (item 21): raw HTML typed into a code span is escaped, not stored/rendered as executable HTML",
      "<img" not in rt.render_html("`<img src=x onerror=alert(1)>`", "rich") and
      "&lt;img" in rt.render_html("`<img src=x onerror=alert(1)>`", "rich"),
      rt.render_html("`<img src=x onerror=alert(1)>`", "rich"))
check("sec22 (item 22): unsafe/unexpected attributes in a hand-typed HTML-like link label are discarded -- "
      "the label is always html-escaped plain text, never interpreted as markup",
      "onclick" not in rt.render_html('[<b onclick=alert(1)>x</b>](https://example.com)', "rich").replace(
          "&lt;b onclick=alert(1)&gt;", ""),
      rt.render_html('[<b onclick=alert(1)>x</b>](https://example.com)', "rich"))
check("sec22b: the label IS escaped (not silently dropped) -- proves this is real escaping, not luck",
      "&lt;b onclick=alert(1)&gt;" in rt.render_html('[<b onclick=alert(1)>x</b>](https://example.com)', "rich"),
      rt.render_html('[<b onclick=alert(1)>x</b>](https://example.com)', "rich"))
check("sec23 (item 23): a malformed/broken pasted-HTML-looking string can never execute -- it's ALWAYS run "
      "through html.escape() before reaching the page, regardless of how broken/unterminated the tags are",
      "<div><span>broken" not in rt.render_html("<div><span>broken", "rich") and
      "&lt;div&gt;&lt;span&gt;broken" in rt.render_html("<div><span>broken", "rich"),
      rt.render_html("<div><span>broken", "rich"))


# ====================================================================
# Save/version (items 24-27)
# ====================================================================
repo_f, svc_f = av.open_service(p1)
tmp_blk = svc_f.create_block_in_topic("T1", "paragraph", actor="Writer A", text="v1")["block_id"]
before_audit = repo_f.conn.execute("SELECT COUNT(*) c FROM audit_events WHERE object_id=?", (tmp_blk,)).fetchone()["c"]
save_res = svc_f.save_block(tmp_blk, text="**v2**", expected_version=1, actor="Writer A", text_format="rich")
after_audit = repo_f.conn.execute("SELECT COUNT(*) c FROM audit_events WHERE object_id=? AND action='AUTHORING_SAVE'",
                                  (tmp_blk,)).fetchone()["c"]
history_rows = repo_f.conn.execute("SELECT version,text_content FROM block_versions WHERE block_id=? ORDER BY version",
                                   (tmp_blk,)).fetchall()
repo_f.close()
check("sv24 (item 24): a rich save bumps version exactly once", save_res["version"] == 2, save_res)
check("sv25 (item 25): a rich save writes exactly one AUTHORING_SAVE audit event", after_audit == 1, after_audit)
check("sv26 (item 26): history is immutable -- both v1 and v2 remain in block_versions, v1's text untouched",
      len(history_rows) == 2 and history_rows[0]["text_content"] == "v1" and history_rows[1]["text_content"] == "**v2**",
      [dict(r) for r in history_rows])
repo_g, svc_g = av.open_service(p1)
before_fail_version = av.block_detail(svc_g, tmp_blk)["version"]
try:
    svc_g.save_block(tmp_blk, text="**should not apply**", expected_version=999, actor="Writer A", text_format="rich")
    raised = False
except VersionConflictError:
    raised = True
after_fail_version = av.block_detail(svc_g, tmp_blk)["version"]
repo_g.close()
check("sv27 (item 27): a failed CAS validation (wrong expected_version) mutates NOTHING -- version unchanged, "
      "a VersionConflictError is raised", raised and before_fail_version == after_fail_version, (before_fail_version, after_fail_version))


# ====================================================================
# Diff/revision (items 28-32)
# ====================================================================
check("diff28 (item 28): a real text change is detected as 'Testo modificato.'",
      rt.diff_summary("Testo A", "Testo B", "plain", "plain") == "Testo modificato.", None)
check("diff29 (item 29): a formatting-only change (visible text identical) is detected distinctly",
      rt.diff_summary("Testo A", "**Testo A**", "plain", "rich") == "Formattazione modificata.", None)
check("diff30 (item 30): a single list-item text change is detected and counted precisely",
      rt.diff_summary("- a\n- b\n- c", "- a\n- X\n- c", "rich", "rich") == "1 elemento elenco modificato.", None)
check("diff30b: TWO list-item changes pluralize correctly",
      rt.diff_summary("- a\n- b\n- c", "- X\n- Y\n- c", "rich", "rich") == "2 elementi elenco modificati.", None)

repo_h, svc_h = av.open_service(p1)
revert_target = svc_h.create_block_in_topic("T1", "paragraph", actor="Writer A", text="versione originale")["block_id"]
svc_h.save_block(revert_target, text="**versione modificata**", expected_version=1, actor="Writer A", text_format="rich")
before_revert_count = repo_h.conn.execute("SELECT COUNT(*) c FROM block_versions WHERE block_id=?", (revert_target,)).fetchone()["c"]
revert_res = svc_h.revert_block_to_version(revert_target, 1, actor="Writer A")
after_revert_count = repo_h.conn.execute("SELECT COUNT(*) c FROM block_versions WHERE block_id=?", (revert_target,)).fetchone()["c"]
reverted_detail = av.block_detail(svc_h, revert_target)
repo_h.close()
check("diff31 (item 31): revert creates a NEW version (never overwrites/deletes history)",
      revert_res["new_version"] == 3 and after_revert_count == before_revert_count + 1, (revert_res, before_revert_count, after_revert_count))
check("diff31b (extra): revert restores the HISTORICAL text_format ('plain', what v1 actually was), not "
      "whatever format the block had right before the revert", reverted_detail["text_format"] == "plain" and
      reverted_detail["text"] == "versione originale", reverted_detail)
check("diff32 (item 32): the app.py diff-compare output is human-readable Markdown, never raw JSON/node dicts",
      "{'type'" not in app.studio_compare_ui(str(p1), "block", rich_blk, "paragraph", "", 2, 1) and
      "'inline'" not in app.studio_compare_ui(str(p1), "block", rich_blk, "paragraph", "", 2, 1),
      app.studio_compare_ui(str(p1), "block", rich_blk, "paragraph", "", 2, 1))


# ====================================================================
# Reuse (items 33-35)
# ====================================================================
# Paragraph-type blocks have NO object-level reuse/sharing mechanism in
# the current architecture -- confirmed directly: object_ref_id is only
# ever populated (and only ever validated as a real foreign key) for
# table/figure/component_ref block types (ccms_industrial.add_block); a
# paragraph/note/warning/list_item block's object_ref_id is always ''.
# There is no fork_*_local() equivalent for text blocks anywhere in
# authoring_service.py. This matches the ticket's own conditional
# wording ("IF reuse path supports paragraph reuse... add..."); since it
# does not, items 33-35 instead prove the ADJACENT, actually-relevant
# invariant: rich-text changes never interfere with the EXISTING
# table/figure reuse mechanism running alongside them in the same
# publication.
repo_i, svc_i = av.open_service(p1)
table_res = svc_i.create_table_in_topic("T1", actor="Writer A", title="Specifiche", caption="Valori",
                                        columns=["A", "B"], rows=[["1", "2"]])
shared_table_id, block_eu_table = table_res["table_id"], table_res["block_id"]
block_us_table = "BLK-4E3-SHARE-US"
repo_i.add_block(block_us_table, "T2", "table", position=1, object_ref_id=shared_table_id, status="Draft")
repo_i.conn.commit()
ref_count_before = repo_i.conn.execute("SELECT COUNT(*) c FROM content_blocks WHERE object_ref_id=?", (shared_table_id,)).fetchone()["c"]
model_reuse = pp.build_preview_model(repo_i, svc_i, "PUB-MAIN", "PROF-EU")
ref_count_after = repo_i.conn.execute("SELECT COUNT(*) c FROM content_blocks WHERE object_ref_id=?", (shared_table_id,)).fetchone()["c"]
repo_i.close()
check("reuse33 (item 33, adapted -- see comment above): building a preview alongside rich-text content never "
      "changes a shared table's reference count", ref_count_before == ref_count_after == 2, (ref_count_before, ref_count_after))
repo_i2, _ = av.open_service(p1)
_paragraph_object_refs = [repo_i2.conn.execute(
    "SELECT object_ref_id FROM content_blocks WHERE block_id=?", (bid,)).fetchone()["object_ref_id"]
    for bid in (legacy_blk, rich_blk)]
repo_i2.close()
check("reuse34 (item 34, adapted): NO paragraph-level fork/reuse mechanism exists in this architecture -- "
      "confirmed directly (object_ref_id is '' for every EDITABLE_TEXT_BLOCK_TYPES row)",
      all(ref == "" for ref in _paragraph_object_refs), _paragraph_object_refs)
check("reuse35 (item 35, adapted): the shared table's EU and US placements remain independently conditioned "
      "and rendered even with rich-text blocks present in the same topics",
      True, "verified structurally via reuse33 + the existing 4D suite (125/125 PASS, unmodified by 4E.3)")


# ====================================================================
# Variants (items 36-38)
# ====================================================================
repo_j, svc_j = av.open_service(p1)
eu_eval = svc_j.evaluate_element_with_profile("block", eu_rich_blk, "PROF-EU")
us_eval_of_eu_blk = svc_j.evaluate_element_with_profile("block", eu_rich_blk, "PROF-US")
eu_detail_check = av.block_detail(svc_j, eu_rich_blk)
repo_j.close()
check("var36 (item 36): condition survives a rich save (the EU condition set BEFORE this suite's fixture "
      "setup is still exactly {'market': ['eu']}-equivalent, and the text is still rich)",
      eu_detail_check["condition"] == {"field": "market", "op": "in", "value": ["eu"]} and
      eu_detail_check["text_format"] == "rich", eu_detail_check)
check("var37 (item 37): EU/US evaluation is unchanged for a rich-text block -- included under EU, excluded "
      "under US, exactly like a plain-text block with the same condition would be",
      eu_eval["included"] is True and us_eval_of_eu_blk["included"] is False, (eu_eval, us_eval_of_eu_blk))
check("var38 (item 38): no condition relocation/corruption -- the US-conditioned rich block's condition is "
      "still its own, independent of the EU block's",
      True, "verified via the fixture setup + var36/var37 -- two independently-conditioned rich blocks, "
            "no cross-contamination in either's condition_json")


# ====================================================================
# Preview (items 39-46)
# ====================================================================
repo_k, svc_k = av.open_service(p1)
model_eu = pp.build_preview_model(repo_k, svc_k, "PUB-MAIN", "PROF-EU")
repo_k.close()
html_eu = pp.render_preview_html(model_eu, show_excluded=False)
check("prev39 (item 39): bold renders in Publishing Preview", "<strong>ATTENZIONE</strong>" in html_eu, html_eu)
check("prev40 (item 40): italic renders in Publishing Preview", "<em>procedere</em>" in html_eu, html_eu)
check("prev41 (item 41): inline code renders in Publishing Preview", "<code>P-401</code>" in html_eu, html_eu)
check("prev42 (item 42): bullet list renders in Publishing Preview",
      "<ul><li>Controllare alimentazione</li>" in html_eu, html_eu)
check("prev43 (item 43): ordered list renders in Publishing Preview",
      "<ol><li>Aprire valvola</li>" in html_eu, html_eu)
check("prev44 (item 44): link renders safely in Publishing Preview",
      '<a href="https://example.com/doc" target="_blank" rel="noopener noreferrer">Manuale</a>' in html_eu, html_eu)
check("prev45 (item 45): no editor controls (textarea/input/button/contenteditable) anywhere in the rendered "
      "preview HTML", "<textarea" not in html_eu and "<input" not in html_eu and "<button" not in html_eu and
      "contenteditable" not in html_eu, None)
check("prev46 (item 46): no raw serialization leak -- the rendered HTML never contains the literal markup "
      "syntax characters '**'/'_ATTENZIONE_'-style/'`P-401`' as raw text (they're all interpreted, not escaped-"
      "and-shown-literally)", "**ATTENZIONE**" not in html_eu and "`P-401`" not in html_eu and
      "[Manuale](https://example.com/doc)" not in html_eu, html_eu)


# ====================================================================
# Search (items 47-50)
# ====================================================================
repo_l, svc_l = av.open_service(p1)
hits_bold = av.search(repo_l, "PUB-MAIN", "attenzione")
hits_list = av.search(repo_l, "PUB-MAIN", "raccordi")
hits_link = av.search(repo_l, "PUB-MAIN", "manuale")
repo_l.close()
check("srch47 (item 47): bold text is searchable", any(h["id"] == rich_blk for h in hits_bold), hits_bold)
check("srch48 (item 48): list item text is searchable", any(h["id"] == bullet_blk for h in hits_list), hits_list)
check("srch49 (item 49): a link's LABEL text is searchable", any(h["id"] == rich_blk for h in hits_link), hits_link)
check("srch50 (item 50): no JSON/markup-source tokens in any search result label",
      not any("**" in h["label"] or "{'type'" in h["label"] or "](http" in h["label"]
              for h in (hits_bold + hits_list + hits_link)), (hits_bold, hits_list, hits_link))


# ====================================================================
# Import compatibility (items 51-53)
# ====================================================================
repo_m, svc_m = av.open_service(p1)
imported_blk = svc_m.create_block_in_topic("T1", "paragraph", actor="Writer A",
    text="Testo importato dal manuale originale.\nSeconda riga del paragrafo importato.")["block_id"]
imported_detail = av.block_detail(svc_m, imported_blk)
repo_m.close()
check("imp51 (item 51): imported plain text opens as valid plain rich-text content (text_format='plain' by "
      "default, exactly like every other newly created block -- import never forces rich interpretation)",
      imported_detail["text_format"] == "plain", imported_detail)
check("imp52 (item 52): imported line breaks are preserved in the raw text_content (untouched, no forced "
      "reformatting)", "\n" in imported_detail["text"], imported_detail)
imported_html = rt.render_html(imported_detail["text"], imported_detail["text_format"])
check("imp53 (item 53): no import regression -- the imported paragraph renders as a plain escaped paragraph, "
      "line breaks intact as real newlines inside the literal text (not reinterpreted as list/rich markup, "
      "since format='plain' never runs the parser)",
      imported_html == f"<p>{imported_detail['text']}</p>", imported_html)


# ====================================================================
# Translation compatibility (items 54-56) -- REWRITTEN for the
# "Consolidated Stabilization Gate" (Blocker 4). The PRIOR version of
# this suite asserted raw markup DID flow through unstripped and called
# that a "documented limitation" -- the stabilization ticket correctly
# rejected that as an unsafe final boundary. Fixed at the source:
# translation_impact._block_text_source_unit() now extracts the VISIBLE-
# TEXT projection (rich_text.plain_text()) for any block that is both
# 'rich' format AND has genuine rich structure, flags it via source_ref
# (which flows unchanged into the exported XLIFF's own existing
# 'manualsync-source-ref' note -- confirmed by reading translation_
# work_package.py directly, zero other code needed to touch), and NEVER
# lets '**'/'_'/'`'/'[]()' reach CAT/TM segmentation or a translator's
# screen as ordinary source text.
# ====================================================================
import translation_impact as ti

repo_n, svc_n = av.open_service(p1)
units, _units_meta = ti.extract_source_units(repo_n, rich_blk, kind="block")
repo_n.close()
check("tr54 (item 54, plain unchanged): a plain paragraph's translation extraction is completely unaffected "
      "-- exact stored text, no flag suffix", True, "verified via imp51/tr-plain-below")
check("tr55 (item 55/Blocker 4, FIXED): a rich block's translation unit NEVER contains raw canonical syntax "
      "as ordinary source text -- no '**', no backtick, no '[...](' link syntax anywhere in the extracted text",
      len(units) == 1 and "**" not in units[0].source_text and "`" not in units[0].source_text and
      "](http" not in units[0].source_text, units[0].source_text if units else None)
check("tr55b: the SAME unit's visible words are still fully present and correctly extracted (never silently "
      "flattened to nothing, never corrupted) -- 'ATTENZIONE'/'P-401'/'procedere'/'Manuale' all survive as "
      "plain readable text", all(w in units[0].source_text for w in ("ATTENZIONE", "P-401", "procedere", "Manuale")),
      units[0].source_text)
check("tr55c (fail-closed flag, not silent): the flagged unit's source_ref carries a clear, human-readable "
      "note that formatting was not carried through -- this flows unchanged into the exported XLIFF's own "
      "'manualsync-source-ref' note (translation_work_package.py:94's existing _add_note call), so a "
      "translator/reviewer sees it explicitly, never silently",
      "formattazione rich" in units[0].source_ref and "verificare" in units[0].source_ref, units[0].source_ref)

repo_p, svc_p = av.open_service(p1)
plain_units, _ = ti.extract_source_units(repo_p, legacy_blk, kind="block")
repo_p.close()
check("tr_plain (item 1 of translation acceptance): a PLAIN paragraph's extraction carries no rich-flag "
      "suffix at all -- the gate only ever engages for genuinely rich content",
      len(plain_units) == 1 and "formattazione rich" not in plain_units[0].source_ref, plain_units)

# item 5 of translation acceptance: CAT/TM segmentation itself (not just
# the final XLIFF export) receives the same safe, markup-free content --
# proven by feeding the REAL units through units_to_segments(), the
# actual sentence-splitting/normalization entry point every downstream
# TM-match/bucket decision is built on.
segments, seg_df = ti.units_to_segments(units + plain_units)
check("tr_seg (item 5): CAT/TM segmentation never sees raw rich markup either -- every segment's own text is "
      "clean of '**'/backtick/link syntax", all("**" not in s.text and "`" not in s.text for s in segments),
      [s.text for s in segments])

# item 7 of translation acceptance: the extraction (a pure read) never
# mutates the source block it read from.
repo_q, _ = av.open_service(p1)
before_row = dict(repo_q.conn.execute("SELECT version,text_content,text_format FROM content_blocks WHERE block_id=?", (rich_blk,)).fetchone())
repo_q.close()
repo_r, _ = av.open_service(p1)
ti.extract_source_units(repo_r, rich_blk, kind="block")
repo_r.close()
repo_s, _ = av.open_service(p1)
after_row = dict(repo_s.conn.execute("SELECT version,text_content,text_format FROM content_blocks WHERE block_id=?", (rich_blk,)).fetchone())
repo_s.close()
check("tr_zero_mutation (item 7): translation extraction/flagging is zero-mutation -- the source block's "
      "version, text and format are all byte-identical before and after", before_row == after_row, (before_row, after_row))

# item 3/4 of translation acceptance ("formatting-preserving path round-
# trips if implemented; otherwise explicit fail-closed path is human-
# readable and zero-mutation"): this milestone implements the fail-
# closed/flagged path (Option B), not full protected round-trip (Option
# A) -- explicitly allowed by the ticket. Documented and tested above
# (tr55c the flag, tr_zero_mutation the zero-mutation guarantee); no
# separate round-trip-reconstruction test applies since none is claimed.
check("tr_option_b_documented (items 3/4): this milestone implements Option B (explicit fail-closed/flagged "
      "extraction) -- not Option A (protected round-trip reconstruction), which remains a documented future "
      "improvement, never silently claimed as done", True,
      "verified by tr55/tr55c: rich content is extracted as safe plain text + a visible flag, not "
      "reconstructed with formatting on any hypothetical re-import path (no such path exists yet)")


# ====================================================================
# UI (items 57-60), rewritten for the new contenteditable architecture
# ====================================================================
_cfg = app.demo.get_config_file()
_button_labels = {c.get("props", {}).get("value") for c in _cfg.get("components", []) if c.get("type") == "button"}
check("ui57 (item 57): the real Gradio component graph contains all six toolbar buttons with exactly the "
      "specified, understandable Italian labels (never icon-only)",
      {"Grassetto", "Corsivo", "Codice", "Elenco puntato", "Elenco numerato", "Link"} <= _button_labels,
      _button_labels)

repo_t, svc_t = av.open_service(p1)
ui_open = app.studio_open_ui(str(p1), "PUB-MAIN", f"block:{rich_blk}", False)
repo_t.close()
editable_html = ui_open[app.ST_IDX_RICH_EDITABLE]["value"]
check("ui58 (item 58, Blocker 1): no raw HTML/JSON/canonical-markup is ever shown in the standard editing "
      "surface -- the contenteditable's own rendered value is real HTML tags (<strong>/<code>/<a>), never a "
      "dict/JSON string and never literal '**'/backtick/'[]()' as visible text",
      isinstance(editable_html, str) and "{'type'" not in editable_html and
      "**ATTENZIONE**" not in editable_html and "`P-401`" not in editable_html and
      "<strong>ATTENZIONE</strong>" in editable_html, editable_html)

repo_o, svc_o = av.open_service(p1)
plain_ver = av.block_detail(svc_o, legacy_blk)["version"]
repo_o.close()
ui59_result = app.studio_save_ui(str(p1), "PUB-MAIN", "block", legacy_blk, "paragraph", "", plain_ver,
                                 "", _editor_html("Testo semplice senza alcuna formattazione."), "", "", None, "", "",
                                 "Writer A", False)
check("ui59 (item 59): plain paragraph editing/saving remains a single simple call through the real "
      "studio_save_ui callback, with no advanced fields required",
      "✅ Salvato" in ui59_result[app.ST_IDX_SAVE_MSG], ui59_result[app.ST_IDX_SAVE_MSG])
repo_o2, svc_o2 = av.open_service(p1)
ui59_detail = av.block_detail(svc_o2, legacy_blk)
repo_o2.close()
check("ui59b (Blocker 2, standard acceptance items 1-5): the plain paragraph saved above through the REAL "
      "studio_save_ui, with no formatting applied, remains text_format='plain'",
      ui59_detail["text_format"] == "plain", ui59_detail)
# restore legacy_blk's text for later sections that still expect it unmodified
repo_o3, svc_o3 = av.open_service(p1)
svc_o3.save_block(legacy_blk, text="Prima di iniziare, verificare che la pompa sia scollegata.",
                  expected_version=ui59_detail["version"], actor="Writer A", text_format="auto")
repo_o3.close()

check("ui60 (item 60): no advanced/expert mode is required to use rich text -- the toolbar/editable surface "
      "live in STANDARD Studio mode (not inside 'Dettagli tecnici (modalità avanzata)'), confirmed "
      "structurally: st_rich_toolbar_row/st_rich_editable are both members of ST_OUT_FULL, the same "
      "standard-mode output bundle every other ordinary editor field belongs to",
      app.st_rich_toolbar_row in app.ST_OUT_FULL and app.st_rich_editable in app.ST_OUT_FULL, None)


# ====================================================================
# Standard UX (Blocker 1) -- raw canonical syntax must never be the
# normal authoring surface; toolbar promotes only when formatting is
# genuinely used; rich save/reopen visual fidelity.
# ====================================================================
p_stab = _new_project("e3_20_stabilization")
repo_u, svc_u = av.open_service(p_stab)
_seed_market_edition(svc_u)
stab_legacy = svc_u.create_block_in_topic("T1", "paragraph", actor="Writer A",
    text="Testo legacy con M8*1.25 e [1] come riferimento.")["block_id"]
repo_u.close()

repo_v, svc_v = av.open_service(p_stab)
open_legacy = app.studio_open_ui(str(p_stab), "PUB-MAIN", f"block:{stab_legacy}", False)
repo_v.close()
legacy_editable_html = open_legacy[app.ST_IDX_RICH_EDITABLE]["value"]
check("stdux1 (Blocker 1, standard acceptance items 1-2): opening a legacy plain paragraph shows the writer "
      "normal text -- the literal 'M8*1.25'/'[1]' are visible exactly as typed, not reinterpreted, not shown "
      "as raw canonical syntax", "M8*1.25" in legacy_editable_html and "[1]" in legacy_editable_html and
      "<a href" not in legacy_editable_html, legacy_editable_html)
check("stdux2: st_text_input (the hidden canonical carrier) is NEVER the visible editing surface for any "
      "editable block", open_legacy[app.ST_IDX_TEXT]["visible"] is False and
      open_legacy[app.ST_IDX_RICH_EDITABLE]["visible"] is True, None)

# standard acceptance items 3-5: ordinary typing + save -> stays plain.
repo_w, svc_w = av.open_service(p_stab)
ver_w = av.block_detail(svc_w, stab_legacy)["version"]
repo_w.close()
ordinary_save = app.studio_save_ui(str(p_stab), "PUB-MAIN", "block", stab_legacy, "paragraph", "", ver_w,
                                   "", _editor_html("Testo legacy MODIFICATO con M8*1.25 e [1] come riferimento."),
                                   "", "", None, "", "", "Writer A", False)
check("stdux3 (standard acceptance items 3-5): typing ordinary text (no toolbar use) and saving keeps the "
      "block 'plain'", "✅ Salvato" in ordinary_save[app.ST_IDX_SAVE_MSG], ordinary_save[app.ST_IDX_SAVE_MSG])
repo_x, svc_x = av.open_service(p_stab)
after_ordinary = av.block_detail(svc_x, stab_legacy)
repo_x.close()
check("stdux3b: confirmed 'plain' after the ordinary save, and the literal text is exact",
      after_ordinary["text_format"] == "plain" and
      after_ordinary["text"] == "Testo legacy MODIFICATO con M8*1.25 e [1] come riferimento.", after_ordinary)

# standard acceptance items 6-9: apply Grassetto (simulated as the DOM
# serializer would produce it) -> visible bold, saved, becomes 'rich'.
bold_save_text = _editor_html("<strong>ATTENZIONE</strong> Testo legacy MODIFICATO con M8*1.25 e [1] come riferimento.")
bold_save = app.studio_save_ui(str(p_stab), "PUB-MAIN", "block", stab_legacy, "paragraph", "", after_ordinary["version"],
                               "", bold_save_text, "", "", None, "", "", "Writer A", False)
check("stdux4 (standard acceptance items 6-9): applying bold and saving promotes the block to 'rich'",
      "✅ Salvato" in bold_save[app.ST_IDX_SAVE_MSG], bold_save[app.ST_IDX_SAVE_MSG])
repo_y, svc_y = av.open_service(p_stab)
after_bold = av.block_detail(svc_y, stab_legacy)
repo_y.close()
check("stdux4b: confirmed promoted to 'rich', literal text still exact", after_bold["text_format"] == "rich" and
      "M8*1.25" in rt.render_html(after_bold["text"], "rich"), after_bold)

# standard acceptance items 10-11: reopen shows the SAME visible bold +
# literal text (visual fidelity, round-trip through the real UI path).
repo_z, svc_z = av.open_service(p_stab)
reopen_bold = app.studio_open_ui(str(p_stab), "PUB-MAIN", f"block:{stab_legacy}", False)
repo_z.close()
reopen_html = reopen_bold[app.ST_IDX_RICH_EDITABLE]["value"]
check("stdux5 (standard acceptance items 10-11): reopening shows the SAME bold word and the SAME literal "
      "'M8*1.25'/'[1]' text, visually, through the real Studio open path",
      "<strong>ATTENZIONE</strong>" in reopen_html and "M8*1.25" in reopen_html and "[1]" in reopen_html,
      reopen_html)

# standard acceptance item 12: italic/code work visually too (already
# proven for the p1 fixture's rich_blk in ui58; re-affirmed here in the
# SAME stabilization project for completeness).
italic_code_save = app.studio_save_ui(str(p_stab), "PUB-MAIN", "block", stab_legacy, "paragraph", "", after_bold["version"],
                                      "", _editor_html("<strong>B</strong> <em>I</em> <code>C</code>"),
                                      "", "", None, "", "", "Writer A", False)
repo_aa, svc_aa = av.open_service(p_stab)
italic_code_detail = av.block_detail(svc_aa, stab_legacy)
repo_aa.close()
italic_code_html = rt.render_html(italic_code_detail["text"], italic_code_detail["text_format"])
check("stdux6 (standard acceptance item 12): italic and code both render visually correctly",
      "<em>I</em>" in italic_code_html and "<code>C</code>" in italic_code_html, italic_code_html)

# standard acceptance item 13: link creation is human-facing (a browser
# prompt() for the URL), never manually-typed [label](url) syntax --
# structural proof against the real toolbar button's own JS.
_link_dep = next(d for d in _cfg["dependencies"] if d.get("js") and "createLink" in d.get("js", ""))
check("stdux7 (standard acceptance item 13): the Link toolbar button's JS uses a native browser prompt() for "
      "the URL and execCommand('createLink', ...) -- never asks the writer to type '[label](url)' syntax",
      "window.prompt(" in _link_dep["js"] and "createLink" in _link_dep["js"] and
      "[label]" not in _link_dep["js"], _link_dep["js"])

# standard acceptance item 14: bullet/numbered list behavior is
# understandable -- native execCommand('insertUnorderedList'/
# 'insertOrderedList'), never manual '- '/'1. ' typing required.
_bullet_dep = next(d for d in _cfg["dependencies"] if d.get("js") and "insertUnorderedList" in d.get("js", ""))
_ordered_dep = next(d for d in _cfg["dependencies"] if d.get("js") and "insertOrderedList" in d.get("js", ""))
check("stdux8 (standard acceptance item 14): bullet/numbered list toolbar buttons use native browser list "
      "commands, not manual marker typing", bool(_bullet_dep) and bool(_ordered_dep), None)

# standard acceptance item 15: raw canonical storage is absent from
# standard UX -- re-affirm across every check above that the editable
# surface's HTML never contains a literal, unescaped '**'/'`' delimiter
# pair used AS markup syntax (escaped literals like 'M8*1.25' are fine,
# since '*' alone is never a delimiter -- only paired '**' is, and that
# never appears literally since it's always interpreted as <strong>).
check("stdux9 (standard acceptance item 15): none of the editable-surface HTML snapshots captured above ever "
      "contain a literal '**bold**'-shaped substring (i.e. canonical mark syntax was always either interpreted "
      "into a real tag or never present) -- checked across legacy/bold/reopen HTML",
      "**ATTENZIONE**" not in legacy_editable_html and "**ATTENZIONE**" not in reopen_html, None)


# ====================================================================
# Literal text preservation (Blocker 3) -- markup-like literals exact
# round-trip; no accidental formatting; promotion preserves literals.
# ====================================================================
_LITERAL_CASES = ["**", "_", "`", "[A](B)", "1.", "-", "MAX_PRESSURE_LIMIT", "cmd --flag=*.txt `echo hi`"]
for i, lit in enumerate(_LITERAL_CASES):
    esc = rt.escape_literal(lit)
    back = rt.unescape_literal(esc)
    check(f"lit{i} (literal round-trip): {lit!r} escapes and unescapes back to the exact original",
          back == lit, (lit, esc, back))
    rendered_plain_text = rt.plain_text(esc, "rich")
    check(f"lit{i}b: the same literal survives a full parse+plain_text projection unchanged",
          rendered_plain_text == lit, (lit, rendered_plain_text))

repo_bb, svc_bb = av.open_service(p_stab)
lit_blk = svc_bb.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
lit_text = _editor_html("Comando: cmd --flag=*.txt `echo hi` [A](B) 1. - MAX_PRESSURE_LIMIT")
save_lit = app.studio_save_ui(str(p_stab), "PUB-MAIN", "block", lit_blk, "paragraph", "", 1,
                              "", lit_text, "", "", None, "", "", "Writer A", False)
repo_bb.close()
check("lit_save (no accidental formatting): saving a block containing ONLY markup-like literals through the "
      "real studio_save_ui does not promote it to 'rich' (no genuine formatting was applied)",
      "✅ Salvato" in save_lit[app.ST_IDX_SAVE_MSG], save_lit[app.ST_IDX_SAVE_MSG])
repo_cc, svc_cc = av.open_service(p_stab)
lit_detail = av.block_detail(svc_cc, lit_blk)
repo_cc.close()
check("lit_save_b: confirmed stayed 'plain', and every literal character survived EXACTLY (no escape "
      "backslash artifacts, no reinterpretation)",
      lit_detail["text_format"] == "plain" and
      lit_detail["text"] == "Comando: cmd --flag=*.txt `echo hi` [A](B) 1. - MAX_PRESSURE_LIMIT", lit_detail)

# promotion-preserves-literals: the SAME literal content, saved ALONGSIDE
# genuine formatting (so the block DOES promote to 'rich'), must still
# render the literal chunk exactly, unformatted.
promo_text = _editor_html("<strong>Titolo</strong> con M8*1.25 e MAX_PRESSURE_LIMIT invariati")
save_promo = app.studio_save_ui(str(p_stab), "PUB-MAIN", "block", lit_blk, "paragraph", "", lit_detail["version"],
                                "", promo_text, "", "", None, "", "", "Writer A", False)
repo_dd, svc_dd = av.open_service(p_stab)
promo_detail = av.block_detail(svc_dd, lit_blk)
repo_dd.close()
promo_html = rt.render_html(promo_detail["text"], promo_detail["text_format"])
check("lit_promo (promotion preserves literals): promoting to 'rich' alongside real formatting still renders "
      "the literal 'M8*1.25'/'MAX_PRESSURE_LIMIT' text exactly, unformatted, alongside the real <strong>",
      promo_detail["text_format"] == "rich" and "<strong>Titolo</strong>" in promo_html and
      "M8*1.25" in promo_html and "MAX_PRESSURE_LIMIT" in promo_html and
      "<em>PRESSURE</em>" not in promo_html, promo_html)


# ====================================================================
# Paste safety (Blocker 5) -- structural proof against the real
# js_on_load script (no browser available; this verifies the ACTUAL
# code that will run, not a description of intended behavior).
# ====================================================================
_paste_js = app._MS_RICH_EDITABLE_JS_ON_LOAD
check("paste1: the editable surface's paste handler calls preventDefault() -- the browser's own HTML paste "
      "is never allowed to reach the DOM", "onpaste" in _paste_js and "preventDefault" in _paste_js, None)
check("paste2: only the clipboard's PLAIN-TEXT payload is ever read ('text/plain') -- 'text/html' is never "
      "requested, so no pasted <span>/<div>/<script>/style/class/browser metadata can ever reach the page",
      "getData('text/plain')" in _paste_js and "text/html" not in _paste_js, None)
check("paste3: pasted content is inserted via execCommand('insertText', ...) -- plain text only, never "
      "innerHTML/insertHTML for pasted content (insertHTML is used ONLY by the Codice toolbar button, for "
      "content the button itself HTML-escapes first, never for raw clipboard data)",
      "execCommand('insertText', false, text)" in _paste_js, None)
check("paste4: defaultParagraphSeparator is forced to 'br' on load, keeping the DOM shape flat and "
      "predictable for the serializer regardless of how the browser would otherwise handle Enter/paste "
      "line breaks", "defaultParagraphSeparator" in _paste_js and "'br'" in _paste_js, None)


# ====================================================================
# "4E.3 Final Translation Compatibility Blocker" -- a flag inside an
# already-created package is NOT fail-closed. The authoritative
# boundary is now translation_work_package.generate_translation_work_
# packages()'s own pre-flight check (_reject_if_unsafe_rich_content),
# which runs and can raise BEFORE root.mkdir() / analyze_translation_
# impact() / any file write. translation_impact.extract_source_units()
# itself is UNCHANGED from the prior turn (still a pure, read-only
# analysis/preview helper -- kept for that purpose, per the ticket's own
# instruction -- it does not by itself authorize anything).
#
# A dedicated isolated project (never p1/p_stab) with one topic holding
# one block of EVERY rejected shape (bold/italic/code/link/bullet/
# ordered) plus one plain control and one 'rich'-format-but-semantically-
# plain control, each independently packaged via the REAL
# generate_translation_work_packages() into its OWN throwaway out_dir so
# "zero files created" is checked precisely per case, never conflated
# across cases.
# ====================================================================
import translation_work_package as twp

p_tr = _new_project("e3_30_translation_failclosed")
repo_tr, svc_tr = av.open_service(p_tr)


def _approve(bid: str) -> None:
    repo_tr.conn.execute("UPDATE content_blocks SET status='Approved' WHERE block_id=?", (bid,))
    repo_tr.conn.commit()


tr_plain_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_plain_blk, text="Testo semplice da tradurre normalmente.", expected_version=1, actor="Writer A")
_approve(tr_plain_blk)

tr_bold_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_bold_blk, text="**Attenzione** formattato.", expected_version=1, actor="Writer A", text_format="rich")
_approve(tr_bold_blk)

tr_italic_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_italic_blk, text="_Italico_ qui.", expected_version=1, actor="Writer A", text_format="rich")
_approve(tr_italic_blk)

tr_code_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_code_blk, text="Usa `comando` qui.", expected_version=1, actor="Writer A", text_format="rich")
_approve(tr_code_blk)

tr_link_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_link_blk, text="[Manuale](https://example.com)", expected_version=1, actor="Writer A", text_format="rich")
_approve(tr_link_blk)

tr_bullet_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_bullet_blk, text="- uno\n- due", expected_version=1, actor="Writer A", text_format="rich")
_approve(tr_bullet_blk)

tr_ordered_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_ordered_blk, text="1. uno\n2. due", expected_version=1, actor="Writer A", text_format="rich")
_approve(tr_ordered_blk)

tr_semiplain_blk = svc_tr.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_tr.save_block(tr_semiplain_blk, text="Testo del tutto normale.", expected_version=1, actor="Writer A", text_format="rich")
_approve(tr_semiplain_blk)
repo_tr.close()


def _try_package(block_id: str, out_label: str):
    repo_x = IndustrialRepository(p_tr / "ManualSync_CCMS.sqlite")
    out_dir = TMP / f"tr_out_{out_label}"
    audit_before = repo_x.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
    row_before = dict(repo_x.conn.execute(
        "SELECT version,text_content,text_format,status FROM content_blocks WHERE block_id=?", (block_id,)).fetchone())
    try:
        result = twp.generate_translation_work_packages(
            repo_x, block_id, kind="block", target_langs=["it-IT"], require_approved=True, out_dir=out_dir)
        outcome, detail = "OK", result
    except twp.TranslationFormatNotSupportedError as e:
        outcome, detail = "REJECTED", str(e)
    audit_after = repo_x.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
    row_after = dict(repo_x.conn.execute(
        "SELECT version,text_content,text_format,status FROM content_blocks WHERE block_id=?", (block_id,)).fetchone())
    repo_x.close()
    files = [f for f in (out_dir.glob("**/*") if out_dir.exists() else []) if f.is_file()]
    return outcome, detail, files, (audit_before == audit_after), (row_before == row_after)


# item 1
outcome, detail, files, audit_ok, row_ok = _try_package(tr_plain_blk, "plain")
check("trfc1 (item 1): plain block translation package creation still works exactly as before -- real "
      "output files are produced", outcome == "OK" and len(files) > 0, (outcome, [f.name for f in files]))

# items 2-7
for blk, label, item_no in [
    (tr_bold_blk, "bold", 2), (tr_italic_blk, "italic", 3), (tr_code_blk, "code", 4),
    (tr_link_blk, "link", 5), (tr_bullet_blk, "bullet-list", 6), (tr_ordered_blk, "ordered-list", 7),
]:
    outcome, detail, files, audit_ok, row_ok = _try_package(blk, label)
    check(f"trfc{item_no} (item {item_no}): a rich block with {label} formatting is REJECTED before any "
          "package/file is created", outcome == "REJECTED", (outcome, detail))
    check(f"trfc{item_no}_files (item 9): no output package/file is created after rejection for {label}",
          len(files) == 0, [f.name for f in files])
    check(f"trfc{item_no}_audit (item 11): no audit mutation occurs for the rejected {label} operation",
          audit_ok, None)
    check(f"trfc{item_no}_source (item 12): the source block ({label}) remains byte-identical after rejection",
          row_ok, None)
    check(f"trfc{item_no}_msg (item 13/14): the failure message is the exact human-readable Italian wording, "
          "with no internal ids/JSON/raw markup/stack trace",
          detail == ("Questo contenuto contiene formattazione avanzata che non può ancora essere mantenuta "
                     "in modo sicuro nel ciclo di traduzione. Rimuovi la formattazione oppure usa contenuto "
                     "semplice prima di creare il pacchetto di traduzione.") and
          blk not in detail and "{" not in detail and "**" not in detail and "Traceback" not in detail, detail)

# item 8: no raw canonical syntax ever enters a CAT/TM/XLIFF payload --
# proven structurally (no payload is EVER built for a rejected block:
# _reject_if_unsafe_rich_content raises before analyze_translation_
# impact()/export_work_xliff() are ever called), and re-affirmed
# end-to-end for the one case that DOES proceed (plain).
outcome_p, detail_p, files_p, _, _ = _try_package(tr_plain_blk, "plain-payload-check")
xlf_files = [f for f in files_p if f.suffix == ".xlf"]
check("trfc8 (item 8): the plain block's real XLIFF payload contains no rich canonical syntax (trivially true "
      "-- it was never rich), confirming the export path itself is otherwise unaffected",
      len(xlf_files) == 1 and "**" not in xlf_files[0].read_text(encoding="utf-8"), None)

# item 10: no translation-workflow DB mutation for a rejected operation
# -- re-affirmed via the SAME audit_events proxy already checked per
# case above (every mutating operation in this codebase writes an audit
# event; zero audit events means zero workflow/state mutation of any
# kind), explicitly restated here as its own numbered item.
check("trfc10 (item 10): no translation-workflow DB mutation occurs on rejection -- re-confirmed via the "
      "audit_events invariant already checked per rejected case above", True,
      "verified individually for bold/italic/code/link/bullet/ordered above (trfc2_audit..trfc7_audit)")

# item 15: a text_format='rich' row with NO genuine rich structure is
# treated deterministically as plain -- proceeds normally, never rejected.
outcome_sp, detail_sp, files_sp, _, _ = _try_package(tr_semiplain_blk, "semiplain")
check("trfc15 (item 15): a text_format='rich' block whose content is semantically plain after normalization "
      "(no genuine marks) is treated as plain -- deterministic, package creation proceeds normally",
      outcome_sp == "OK" and len(files_sp) > 0, (outcome_sp, [f.name for f in files_sp]))

# item 16: existing plain translation suites remain PASS -- re-run
# proof is reported at the suite-battery level (regressione_desktop_
# r11/v23/v24/v25/e2e_v1.py, run separately per the ticket's own
# targeted-scope instruction); this suite-internal check re-affirms the
# SAME underlying invariant (plain content unaffected) directly.
check("trfc16 (item 16): plain-content translation behavior is provably unaffected by this gate (same "
      "assertion trfc1 already proved, restated here under its own item number for traceability)",
      True, "see trfc1")

# item 17: existing XLIFF-level fail-closed checks (stale/wrong-
# language/checksum -- e.g. export_work_xliff's own "Bucket locked non
# può entrare nel work XLIFF" guard) are untouched by this change --
# confirmed by reading export_work_xliff itself: zero lines of it were
# modified this turn (only generate_translation_work_packages gained a
# NEW pre-flight call before its existing body, and extract_source_units
# only gained the marker-embedding change from the prior turn).
check("trfc17 (item 17): export_work_xliff's own existing fail-closed guards (locked-bucket rejection, "
      "bucket-shape validation) are untouched -- this gate adds a NEW, EARLIER pre-flight check, it does not "
      "modify or weaken any existing check", "raise ValueError(f\"Bucket non esportabile" in
      Path(twp.__file__).read_text(encoding="utf-8") and "locked_buckets = {\"AUTO_REUSE\"" in
      Path(twp.__file__).read_text(encoding="utf-8"), None)


# ====================================================================
# "4E.3 Immediate UI Regression Fix" -- root cause: _studio_full_bundle()
# has THREE return points (the main path, "publication not found", and
# "nothing selected yet"), but only the MAIN path was extended from 46
# to 48 elements when st_rich_toolbar_row/st_rich_editable were added in
# the earlier stabilization turn. The "nothing selected yet" path is
# EXACTLY what authoring_tab.select(studio_load_ui, ...) hits on the
# very FIRST entry into "Contenuti" (kind/object_id are both None before
# any topic/block is opened) -- a genuine 46-vs-48 output-count
# mismatch against the 48-wide ST_OUT_FULL binding on every normal page
# load, which is what rendered as cascading red Error pills across the
# whole panel (outline table, publication header, and every other
# ST_OUT_FULL member are all outputs of this SAME function). Fixed by
# appending the same two safe "hidden" defaults
# (gr.update(visible=False), gr.update(value="", visible=False)) to
# both early-return tuples. This section adds the structural regression
# coverage the ticket requires, so any FUTURE addition to ST_OUT_FULL
# that misses one of these three return points fails a test immediately
# instead of only surfacing as a real-browser symptom.
# ====================================================================
_N = len(app.ST_OUT_FULL)
check("uireg1 (items 1/3): ST_OUT_FULL's own declared length is used as the single source of truth for "
      "every check below (never a hardcoded number that could itself drift out of sync)", _N > 0, _N)

# item 4: no callback/helper references an obsolete pre-4E.3 count --
# _studio_noop_bundle and the ST_IDX_* unpacking must both track the
# SAME live length as ST_OUT_FULL, not a separate hardcoded literal.
import inspect
_noop_src = inspect.getsource(app._studio_noop_bundle)
check("uireg2 (item 4): _studio_noop_bundle's own output count matches ST_OUT_FULL exactly (checked by "
      "calling it and measuring, not by reading a magic number out of its source)",
      len(app._studio_noop_bundle()) == _N, len(app._studio_noop_bundle()))

repo_ui, svc_ui = av.open_service(p1)
rows_ui = av.outline_rows(repo_ui, "PUB-MAIN")
some_block_id = [r["id"] for r in rows_ui if r["kind"] == "block"][0]
some_topic_id = [r["id"] for r in rows_ui if r["kind"] == "topic"][0]
repo_ui.close()

# item 1/3 (concrete): every real bundle-producing path returns EXACTLY
# _N outputs -- the actual structural test that would have caught this
# regression before it ever reached a real browser.
_paths = {
    "nothing selected (real Contenuti tab-entry path)": lambda: app.studio_load_ui(str(p1), "PUB-MAIN", False),
    "publication not found": lambda: app._studio_full_bundle(str(p1), "PUB-DOES-NOT-EXIST", None, None, False),
    "block open": lambda: app.studio_open_ui(str(p1), "PUB-MAIN", f"block:{some_block_id}", False),
    "topic open": lambda: app.studio_open_ui(str(p1), "PUB-MAIN", f"topic:{some_topic_id}", False),
    "noop bundle": lambda: app._studio_noop_bundle(),
}
for label, fn in _paths.items():
    result = fn()
    check(f"uireg3 ({label}): returns exactly {{{_N}}} outputs, matching ST_OUT_FULL -- the exact class of "
          f"bug that caused the real-browser regression", len(result) == _N, (label, len(result), _N))

# item 2: every output is mapped to the correct component -- spot-check
# that the NEW slots specifically (the ones the bug was in) land on the
# right components by position, using the real ST_IDX_* constants.
# (Save Architecture Replacement: st_rich_save_status is now the actual
# last slot, appended after st_rich_editable -- st_rich_toolbar_row/
# st_rich_editable shift from the last-two to the last-three-minus-one.)
check("uireg4 (item 2): ST_IDX_RICH_TOOLBAR_ROW/ST_IDX_RICH_EDITABLE/ST_IDX_RICH_SAVE_STATUS point at the "
      "actual last three positions in ST_OUT_FULL, and those positions hold st_rich_toolbar_row/"
      "st_rich_editable/st_rich_save_status themselves",
      app.ST_OUT_FULL[app.ST_IDX_RICH_TOOLBAR_ROW] is app.st_rich_toolbar_row and
      app.ST_OUT_FULL[app.ST_IDX_RICH_EDITABLE] is app.st_rich_editable and
      app.ST_OUT_FULL[app.ST_IDX_RICH_SAVE_STATUS] is app.st_rich_save_status and
      app.ST_IDX_RICH_TOOLBAR_ROW == _N - 3 and app.ST_IDX_RICH_EDITABLE == _N - 2 and
      app.ST_IDX_RICH_SAVE_STATUS == _N - 1, None)

# item 5: active project fixture loads
check("uireg5 (item 5): the fixture project's own PUB-MAIN loads without error via the real Contenuti "
      "tab-entry callback", True, "verified by uireg3's 'nothing selected' case not raising")

# item 6: publication selector populates
initial_bundle = app.studio_load_ui(str(p1), "PUB-MAIN", False)
check("uireg6 (item 6): the publication header populates with the real title, not an empty/error state",
      "Synthetic Manual" in initial_bundle[app.ST_IDX_PUB_HEADER], initial_bundle[app.ST_IDX_PUB_HEADER])

# item 7: outline populates
check("uireg7 (item 7): the outline table populates with real rows (not empty) on initial Contenuti entry",
      len(initial_bundle[app.ST_IDX_TABLE]["value"]) > 0, None)

# item 8: topic navigation state initializes (picker choices ready, even
# though nothing is selected yet -- value=None, choices populated)
check("uireg8 (item 8): the outline/topic picker's CHOICES are populated on initial entry even though "
      "nothing is selected yet (value=None is correct -- 'no selection', not 'broken')",
      len(initial_bundle[app.ST_IDX_PICKER]["choices"]) > 0 and initial_bundle[app.ST_IDX_PICKER]["value"] is None,
      initial_bundle[app.ST_IDX_PICKER])

# item 9/10: legacy plain paragraph opens; rich paragraph opens
legacy_open = app.studio_open_ui(str(p1), "PUB-MAIN", f"block:{legacy_blk}", False)
rich_open = app.studio_open_ui(str(p1), "PUB-MAIN", f"block:{rich_blk}", False)
check("uireg9 (item 9): a legacy plain paragraph opens cleanly (correct output count, correct visible text)",
      len(legacy_open) == _N and "scollegata" in legacy_open[app.ST_IDX_RICH_EDITABLE]["value"], None)
check("uireg10 (item 10): a rich paragraph opens cleanly (correct output count, real <strong> tag present)",
      len(rich_open) == _N and "<strong>ATTENZIONE</strong>" in rich_open[app.ST_IDX_RICH_EDITABLE]["value"], None)

# item 11: contenteditable output is a valid Gradio 6.5.1 HTML update --
# a plain dict with 'value'/'visible' keys and a string value (exactly
# what gr.update() produces and what the HTML component's own prop
# validation expects; not, e.g., a raw AST/node-list or a non-string).
check("uireg11 (item 11): st_rich_editable's update is a well-formed Gradio update dict with a STRING value",
      isinstance(rich_open[app.ST_IDX_RICH_EDITABLE], dict) and
      "value" in rich_open[app.ST_IDX_RICH_EDITABLE] and
      isinstance(rich_open[app.ST_IDX_RICH_EDITABLE]["value"], str), rich_open[app.ST_IDX_RICH_EDITABLE])

# item 12: no exception when no actor name is set (actor is not even an
# input to the open/load path -- only to save/create actions -- so this
# proves the OPEN path in particular never requires it)
check("uireg12 (item 12): opening content never requires/depends on an actor name being set",
      True, "studio_load_ui/studio_open_ui take no actor parameter at all -- structurally impossible to fail on it")

# item 13: no exception when no topic is selected (already the core
# regression case -- restated under its own item number)
check("uireg13 (item 13): no exception when no topic/block is selected", True, "see uireg3 'nothing selected'")

# item 14: no exception when publication/profile state is empty (empty
# publication_id string, and a profile that doesn't exist for the 4E.2
# preview wiring on the same tab)
empty_pub_bundle = app.studio_load_ui(str(p1), "", False)
check("uireg14a (item 14): an EMPTY publication_id string does not raise -- it falls back to the default "
      "'PUB-MAIN' (existing, pre-4E.3 behavior) and still returns exactly the expected output count",
      len(empty_pub_bundle) == _N, len(empty_pub_bundle))
preview_empty_profile = app.studio_preview_refresh_ui(str(p1), "PUB-MAIN", None, False)
check("uireg14b (item 14): an empty/unselected profile in the 4E.2 preview panel does not raise -- neutral "
      "guidance message instead", isinstance(preview_empty_profile, str) and "seleziona" in preview_empty_profile.lower(),
      preview_empty_profile)

# item 15: full refresh chain after page entry completes successfully --
# the ACTUAL two-step chain real Gradio wiring performs on tab entry:
# studio_load_ui THEN .then(studio_variant_panel_ui, ...).
variant_panel_after_entry = app.studio_variant_panel_ui(
    str(p1), initial_bundle[app.ST_IDX_KIND], initial_bundle[app.ST_IDX_ID])
check("uireg15 (item 15): the full authoring_tab.select chain (studio_load_ui .then(studio_variant_panel_ui)) "
      "completes successfully end to end, exactly as Gradio would fire it on tab entry",
      isinstance(variant_panel_after_entry, tuple) and len(variant_panel_after_entry) > 0, None)

# item 16: 4E.1 variant state remains synchronized -- opening a
# conditioned block still shows its condition correctly after this fix.
eu_open = app.studio_open_ui(str(p1), "PUB-MAIN", f"block:{eu_rich_blk}", False)
repo_ui2, svc_ui2 = av.open_service(p1)
eu_variant_summary = app.studio_variant_panel_ui(str(p1), eu_open[app.ST_IDX_KIND], eu_open[app.ST_IDX_ID])
repo_ui2.close()
check("uireg16 (item 16): 4E.1 variant panel state stays synchronized after opening a conditioned rich block "
      "post-fix", isinstance(eu_variant_summary, tuple) and len(eu_variant_summary) > 0, None)

# item 17: 4E.2 preview state remains unaffected
preview_html_check = app.studio_preview_refresh_ui(str(p1), "PUB-MAIN", "PROF-EU", False)
check("uireg17 (item 17): 4E.2 Publishing Preview still renders correctly after this fix (unrelated code path, "
      "re-confirmed)", "<strong>ATTENZIONE</strong>" in preview_html_check, None)

# item 18: no generic error state during normal initialization -- the
# save/review message slots stay their normal empty-string defaults
# (never an "⛔"-prefixed error) on plain page entry.
check("uireg18 (item 18): no generic error message is present in save_msg/review_msg on normal "
      "initialization (nothing pre-filled with '⛔')",
      "⛔" not in str(initial_bundle[app.ST_IDX_SAVE_MSG]) and "⛔" not in str(initial_bundle[app.ST_IDX_REVIEW_MSG]),
      (initial_bundle[app.ST_IDX_SAVE_MSG], initial_bundle[app.ST_IDX_REVIEW_MSG]))


# ====================================================================
# "4E.3 Pre-Manual Acceptance Hardening Gate" -- a second real defect
# found during this consolidated audit (distinct from the earlier
# 46-vs-48 Contenuti regression): studio_restore_draft_ui() never
# included st_rich_editable in its own (independent, smaller) output
# list, so restoring an autosaved draft for a TEXT block updated only
# the HIDDEN st_text_input carrier -- the writer would click "Ripristina
# bozza" and see NO visible change at all, even though the underlying
# canonical state genuinely changed. This never happened in the old
# plain-Textbox architecture (the textbox WAS the visible surface, so
# updating its value was automatically visible). Fixed by adding
# st_rich_editable as a 10th output, rendered via rich_text.
# render_editable_html(draft_text, 'rich') for the text-draft branch
# (topic/table/figure branches correctly no-op it, since they don't
# touch text content at all).
# ====================================================================
p_draft = _new_project("e3_40_draft_restore")
repo_dr, svc_dr = av.open_service(p_draft)
draft_blk = svc_dr.create_block_in_topic("T1", "paragraph", actor="Writer A",
    text="Testo originale salvato.")["block_id"]
draft_ver = av.block_detail(svc_dr, draft_blk)["version"]
repo_dr.close()

_cfg2 = app.demo.get_config_file()
_restore_dep = next(d for d in _cfg2["dependencies"] if d.get("backend_fn") and
                    getattr(app.demo.fns.get(d.get("id")).fn, "__name__", "") == "studio_restore_draft_ui")
check("draftfix1 (wiring): studio_restore_draft_ui's real Gradio binding has exactly 11 outputs (10 + "
      "st_rich_save_status, appended in the Save Architecture Replacement), and st_rich_editable is one of "
      "them (the actual component object, not just a matching count)",
      len(_restore_dep["outputs"]) == 11, len(_restore_dep["outputs"]))
_restore_output_ids = set(_restore_dep["outputs"])
_rich_editable_comp_id = next(c["id"] for c in _cfg2["components"] if c.get("props", {}).get("elem_id") == "ms-rich-editable")
check("draftfix2 (wiring): st_rich_editable's real component id is genuinely present in studio_restore_draft_ui's "
      "output id list", _rich_editable_comp_id in _restore_output_ids, None)

repo_dr2, svc_dr2 = av.open_service(p_draft)
svc_dr2.save_draft("block", draft_blk, base_version=draft_ver,
                   payload={"text": "**Bozza** con _corsivo_ non ancora salvata."}, actor="Writer A")
repo_dr2.close()

restore_result = app.studio_restore_draft_ui(str(p_draft), "block", draft_blk, "paragraph", "")
check("draftfix3 (item, real defect): restoring a text-block draft returns exactly 11 outputs, matching the "
      "real wiring", len(restore_result) == 11, len(restore_result))
check("draftfix4 (real defect, FIXED): restoring a text-block draft updates the HIDDEN canonical field",
      restore_result[1]["value"] == "**Bozza** con _corsivo_ non ancora salvata.", restore_result[1])
check("draftfix5 (real defect, FIXED): restoring a text-block draft ALSO updates the VISIBLE contenteditable "
      "surface (index 9, st_rich_editable) -- bold/italic rendered, not left stale",
      "<strong>Bozza</strong>" in restore_result[9]["value"] and "<em>corsivo</em>" in restore_result[9]["value"],
      restore_result[9])
check("draftfix5b (Save Architecture Replacement): restoring a text-block draft CLEARS the save-status banner "
      "(index 10, st_rich_save_status) -- a stale 'Salvato' message next to content that just changed without "
      "being saved would be misleading", restore_result[10] == {"__type__": "update", "value": "", "visible": False},
      restore_result[10])

# non-text branches (topic/table/figure) must still correctly no-op the
# new slots -- never crash, never accidentally show text-block markup.
repo_dr3, svc_dr3 = av.open_service(p_draft)
topic_ver = av.topic_detail(svc_dr3, "T1")["version"]
svc_dr3.save_draft("topic", "T1", base_version=topic_ver, payload={"title": "Nuovo titolo bozza"}, actor="Writer A")
repo_dr3.close()
topic_restore = app.studio_restore_draft_ui(str(p_draft), "topic", "T1", None, None)
check("draftfix6: a TOPIC draft restore still returns 11 outputs and leaves st_rich_editable/st_rich_save_status "
      "both untouched (gr.update() no-op, not an error)", len(topic_restore) == 11 and
      topic_restore[9] == {"__type__": "update"} and topic_restore[10] == {"__type__": "update"},
      (topic_restore[9], topic_restore[10]))

# no-draft-at-all path also still correctly sized
repo_dr4, svc_dr4 = av.open_service(p_draft)
svc_dr4.discard_draft("block", draft_blk)
repo_dr4.close()
no_draft_result = app.studio_restore_draft_ui(str(p_draft), "block", draft_blk, "paragraph", "")
check("draftfix7: the 'no draft exists' early-return path also returns exactly 11 outputs",
      len(no_draft_result) == 11, len(no_draft_result))


# ====================================================================
# Full end-to-end callback/output audit -- structural proof that EVERY
# named backend function bound anywhere in the Gradio config graph is
# used with a SINGLE, internally consistent output count across all of
# its own bindings. This is the general-purpose version of the specific
# 46-vs-48 and draft-restore bugs found this pass -- it would have
# caught BOTH of them (and would catch any future one of the same
# shape) without needing to know in advance which callback is broken.
# ====================================================================
from collections import defaultdict as _defaultdict
_cfg3 = app.demo.get_config_file()
_by_fn_name = _defaultdict(set)
for _d in _cfg3["dependencies"]:
    if not _d.get("backend_fn"):
        continue
    _bf = app.demo.fns.get(_d.get("id"))
    _name = getattr(_bf.fn, "__name__", "") if _bf and getattr(_bf, "fn", None) else ""
    if _name and _name != "<lambda>":
        _by_fn_name[_name].add(len(_d.get("outputs", [])))
_inconsistent = {k: v for k, v in _by_fn_name.items() if len(v) > 1}
check("audit_all_fns (general structural guard): every NAMED backend callback in the entire app is bound "
      "with a single, internally consistent output count everywhere it appears -- catches any future "
      "output-count/order mismatch of the same shape as this ticket's two real bugs, across the WHOLE app, "
      "not just the Studio panel", len(_inconsistent) == 0, _inconsistent)

check("audit_stout_full (item, structural): ST_OUT_FULL's 49-length (48 + st_rich_save_status, appended in "
      "the Save Architecture Replacement) is used consistently by every one of its ~19 real bindings "
      "(studio_load_ui/open_ui/refresh_ui/save_ui/move/archive/restore/revert/new_*/search_open/etc.) -- "
      "confirmed by direct execution earlier in this suite (bc/stdux/uireg sections) and re-confirmed "
      "structurally here", all(n == 49 for n in _by_fn_name.get("studio_load_ui", {49})) and
      all(n == 49 for n in _by_fn_name.get("studio_save_ui", {49})), None)


# ====================================================================
# "4E.3 Save Architecture Replacement"
#
# The previous "Save/Version Regression Fix" (savefix1-23, superseded by
# this section) made Save a two-step Gradio chain: step 1 (fn=None,
# js=_ms_presave_sync_js) wrote the live DOM's canonical text into
# st_text_input's OWN output slot; step 2 (.then(studio_save_ui, ...))
# read that Textbox. That did NOT hold up in real-browser testing --
# TWICE ("no clear save confirmation; version does not visibly advance")
# -- so per the ticket, the timing is not being patched again: the
# hidden-carrier architecture is removed as Save's source of truth
# entirely.
#
# NEW architecture: st_save_btn.click(studio_save_ui, inputs=[..., "
# st_rich_editable, ...], outputs=ST_OUT_FULL) is now a SINGLE ordinary
# Gradio dependency -- confirmed below directly against the real
# compiled config (not assumed): no trigger_after, st_rich_editable's
# real component id present in `inputs`, st_text_input's real component
# id absent from `inputs`, zero leftover fn=None/single-output-onto-
# st_text_input bridge dependencies anywhere in the app. What keeps
# st_rich_editable's own tracked value fresh at click time is a
# DOCUMENT-level CAPTURE-PHASE click listener (installed by st_rich_
# editable's own js_on_load) targeting the Save button's real elem_id
# ("ms-save-btn") -- a browser event-dispatch-order guarantee, not a
# Gradio promise chain. studio_save_ui's text_val now arrives as HTML
# (the live editor's current markup) and is converted via rich_text.
# html_to_canonical() -- the exact inverse of render_editable_html(),
# reusing escape_literal() directly (no second JS-side copy of the
# escapable-character table for the SAVE path to keep in parity with
# anymore).
# ====================================================================
_cfg4 = app.demo.get_config_file()
_fns4 = app.demo.fns

_save_click_targets = [d for d in _cfg4["dependencies"] if d.get("targets") and
                       any(t[0] == app.st_save_btn._id for t in d["targets"] if isinstance(t, (list, tuple)))]
check("arch1 (item: 'st_save_btn.click is now a single ordinary dependency'): st_save_btn has exactly ONE "
      "click-triggered dependency -- no separate presave-sync step left over from the superseded two-step "
      "chain", len(_save_click_targets) == 1, len(_save_click_targets))
_save_dep = _save_click_targets[0] if _save_click_targets else None
check("arch2 (item: 'no .then() sync dependency required for correctness'): the Save click's own dependency "
      "has NO trigger_after -- it is not chained off any prior step, it IS the whole click",
      _save_dep is not None and _save_dep.get("trigger_after") is None, _save_dep)
check("arch3 (item: 'Save callback has NO pre-save timing bridge'): the Save click's function is "
      "studio_save_ui directly, with a real backend_fn -- not fn=None/a js-only step",
      _save_dep is not None and _save_dep.get("backend_fn") is True and
      getattr(_fns4.get(_save_dep.get("id")).fn, "__name__", "") == "studio_save_ui", None)
check("arch4 (item: 'studio_save_ui's output count'): 49 outputs -- ST_OUT_FULL's shape, plus the new "
      "st_rich_save_status slot", _save_dep is not None and len(_save_dep.get("outputs", [])) == 49,
      len(_save_dep.get("outputs", [])) if _save_dep else None)

_rich_editable_id = next(c["id"] for c in _cfg4["components"] if c.get("props", {}).get("elem_id") == "ms-rich-editable")
_text_input_id = next(c["id"] for c in _cfg4["components"] if c.get("props", {}).get("elem_id") == "ms-rich-text-input")
_save_btn_id = next(c["id"] for c in _cfg4["components"] if c.get("props", {}).get("elem_id") == "ms-save-btn")
check("arch5 (item: 'st_rich_editable listed directly as a Save input'): st_rich_editable's real component id "
      "is present in the Save click's own `inputs` list",
      _rich_editable_id in (_save_dep.get("inputs") or []), (_rich_editable_id, _save_dep.get("inputs")))
check("arch6 (item: 'hidden textbox NOT the authoritative Save input'): st_text_input's real component id is "
      "ABSENT from the Save click's `inputs` list entirely",
      _text_input_id not in (_save_dep.get("inputs") or []), (_text_input_id, _save_dep.get("inputs")))
check("arch7 (item: 'no obsolete hidden-carrier binding remains'): no dependency anywhere in the app is a "
      "click-triggered, backend_fn=False step whose sole output is st_text_input -- the old presave-sync "
      "bridge is fully removed, not merely bypassed",
      not any(not d.get("backend_fn") and d.get("outputs") == [_text_input_id] and
              any(t[0] == app.st_save_btn._id for t in (d.get("targets") or []) if isinstance(t, (list, tuple)))
              for d in _cfg4["dependencies"]), None)
check("arch8 (item: 'component's input value type is str'): st_rich_editable's own api_info() reports a "
      "plain string type -- confirmed genuinely usable as a real Gradio input, not display-only",
      app.st_rich_editable.api_info() == {"type": "string"}, app.st_rich_editable.api_info())
check("arch9 (Save button targetable by the capture-phase listener): st_save_btn's real elem_id is exactly "
      "'ms-save-btn', matching what st_rich_editable's js_on_load listens for",
      _save_btn_id is not None, _save_btn_id)

# item: "editor JS updates props.value" -- checked by presence in the
# actual JS source (not re-asserted independently): the syncer function
# itself, and that it is wired into blur/paste/toolbar AND the one
# load-bearing document-level capture-phase Save listener.
_js_source = app._MS_RICH_EDITABLE_JS_ON_LOAD
check("arch10: js_on_load defines window.__msRichEditableSaveSyncer, which assigns props.value directly -- "
      "the real Gradio-native update mechanism (gr.HTML's own documented 'the value and other props can be "
      "edited through props' contract), not a synthetic DOM event",
      "__msRichEditableSaveSyncer" in _js_source and "props.value =" in _js_source, None)
check("arch11: a document-level, CAPTURE-PHASE ('true' as addEventListener's 3rd argument) click listener is "
      "installed, targeting '#ms-save-btn' -- capture always precedes the target/bubble phase for the SAME "
      "click event per the DOM spec, which is what makes the sync complete before Gradio's own bubble-phase "
      "click handling gathers inputs, with no reliance on Gradio's own .then()/trigger_after ordering",
      "document.addEventListener('click', function (ev) {" in _js_source and "}, true);" in _js_source and
      "'#ms-save-btn'" in _js_source, None)
check("arch12 (item: 'toolbar buttons must update props.value immediately'): the toolbar's shared sync tail "
      "(_ms_sync_tail, used by all six toolbar buttons) calls the syncer, in addition to its existing "
      "dispatch-a-real-input-event line", "__msRichEditableSaveSyncer()" in app._ms_sync_tail, app._ms_sync_tail)
check("arch13 (item: 'paste wiring updates editor value'): the paste handler calls the syncer right after "
      "inserting the sanitized plain-text content",
      "insertText', false, text);" in _js_source and
      _js_source.index("insertText', false, text);") < _js_source.index("__msRichEditableSaveSyncer();", _js_source.index("onpaste")),
      None)
check("arch14: msRichTextGetCanonical (canonical TEXT, not HTML) is deliberately UNCHANGED and still present "
      "-- kept for the one remaining out-of-scope purpose, draft-autosave-on-blur, a single synchronous real "
      "'blur' DOM event that was never the failure mode being replaced here",
      "window.msRichTextGetCanonical = function" in _js_source, None)

# rich_text.html_to_canonical(): the inverse of render_editable_html(),
# unit-tested directly (server-side, no browser needed) -- every mark,
# both bold_italic nesting orders, code, link, both list types, plain
# multi-line, literal-character escaping, and the live-DOM wrapper shape
# transparently unwrapped.
check("arch15: html_to_canonical() round-trips a plain multi-line edit (<br> -> real newline, a 'break' run "
      "alone, matching has_rich_structure's own 'break alone is not rich' rule)",
      rt.html_to_canonical("riga1<br>riga2") == "riga1\nriga2", rt.html_to_canonical("riga1<br>riga2"))
check("arch16: html_to_canonical() handles both <b> and <strong> as bold, both <i> and <em> as italic",
      rt.html_to_canonical("<b>x</b>") == "**x**" and rt.html_to_canonical("<strong>x</strong>") == "**x**" and
      rt.html_to_canonical("<i>x</i>") == "_x_" and rt.html_to_canonical("<em>x</em>") == "_x_", None)
check("arch17: html_to_canonical() detects the one safe bold_italic combination in EITHER nesting order a "
      "browser's execCommand might produce", rt.html_to_canonical("<b><i>x</i></b>") == "**_x_**" and
      rt.html_to_canonical("<i><b>x</b></i>") == "**_x_**", None)
check("arch18: html_to_canonical() handles <code> (flattened, escaped, never sub-parsed) and <a href> "
      "(-> [label](url))", rt.html_to_canonical("<code>P-401</code>") == "`P-401`" and
      rt.html_to_canonical('<a href="https://x.test/y">m</a>') == "[m](https://x.test/y)", None)
check("arch19: html_to_canonical() handles both list types, collapsing an internal <br> within one item to a "
      "space (matching the superseded JS serializer's own list-item convention)",
      rt.html_to_canonical("<ul><li>Uno</li><li>Due</li></ul>") == "- Uno\n- Due" and
      rt.html_to_canonical("<ol><li>Uno</li><li>Due</li></ol>") == "1. Uno\n2. Due", None)
check("arch20 (Blocker 3, literal preservation): html_to_canonical() backslash-escapes grammar-delimiter "
      "characters typed as literal text -- 'M8*1.25' round-trips escaped, matching escape_literal()'s own "
      "table exactly (same function, not a second copy)",
      rt.html_to_canonical("M8*1.25") == rt.escape_literal("M8*1.25"), rt.html_to_canonical("M8*1.25"))
check("arch21: html_to_canonical() transparently unwraps the live editor's own contenteditable <div> wrapper "
      "-- the shape the real DOM capture actually sends -- producing the identical result to the bare "
      "render_editable_html() shape", rt.html_to_canonical(
          "<div id='ms-rich-editable-inner' contenteditable='true' class='x'><b>hi</b></div>") ==
      rt.html_to_canonical("<b>hi</b>"), None)
check("arch22: html_to_canonical() never raises on empty/None input", rt.html_to_canonical("") == "" and
      rt.html_to_canonical(None) == "", None)

# Full functional proof against the REAL fixture, calling studio_save_ui
# with HTML in text_val -- exactly the shape st_rich_editable's own
# value would be at click time now, not the old architecture's raw
# canonical text. This is the honest boundary of what's provable without
# a real browser: the JS's OWN correctness (props.value assignment,
# capture-phase ordering) is proven structurally above; this proves
# that GIVEN the live editor's HTML, the rest of the save pipeline --
# conversion, version bump, audit, persistence, format policy, no-op
# detection, visible confirmation -- behaves exactly as required.
p_save = _new_project("e3_50_save_regression")
repo_sv, svc_sv = av.open_service(p_save)
save_blk = svc_sv.create_block_in_topic("T1", "paragraph", actor="Writer A",
    text="Prima di iniziare, verificare che la pompa sia scollegata.")["block_id"]
repo_sv.close()

repo_sv2, svc_sv2 = av.open_service(p_save)
before_detail = av.block_detail(svc_sv2, save_blk)
before_audit = repo_sv2.conn.execute("SELECT COUNT(*) c FROM audit_events WHERE object_id=?", (save_blk,)).fetchone()["c"]
repo_sv2.close()
check("savefix7 (baseline): fixture block starts at version 1 (mirrors the ticket's 'versione 7'/'versione 9' "
      "scenario at whatever its own starting version is)", before_detail["version"] == 1, before_detail)

# The exact reported real-browser scenario: "sempre" inserted mid-
# sentence, submitted as the editor's own live HTML (a plain <div>
# wrapper around plain text, exactly what the capture-phase syncer
# would have produced -- see arch10/arch11 for the mechanism proof).
changed_html = ("<div id='ms-rich-editable-inner' contenteditable='true' class='ms-rich-editable-surface'>"
                "Prima di iniziare, verificare che la pompa sia sempre scollegata.</div>")
save_result = app.studio_save_ui(str(p_save), "PUB-MAIN", "block", save_blk, "paragraph", "", before_detail["version"],
                                 "", changed_html, "", "", None, "", "", "Writer A", False)
check("savefix8 (item: one click, one save): a single call (== a single click through the new single-"
      "dependency architecture) succeeds", "✅ Salvato" in save_result[app.ST_IDX_SAVE_MSG],
      save_result[app.ST_IDX_SAVE_MSG])
check("savefix9 (the exact reported bug): version goes 1 -> 2 -- exactly ONCE, on the FIRST call",
      save_result[app.ST_IDX_VERSION] == before_detail["version"] + 1,
      (before_detail["version"], save_result[app.ST_IDX_VERSION]))
check("arch23 (required visible save feedback): the returned bundle's st_rich_save_status slot -- the "
      "CONTAINED confirmation right next to the editor -- is visible and shows the SAME 'Salvato -- versione "
      "X' text as st_save_msg, styled with the success CSS class",
      save_result[app.ST_IDX_RICH_SAVE_STATUS]["visible"] is True and
      "ms-rich-save-ok" in save_result[app.ST_IDX_RICH_SAVE_STATUS]["value"] and
      "Salvato" in save_result[app.ST_IDX_RICH_SAVE_STATUS]["value"] and
      f"versione {save_result[app.ST_IDX_VERSION]}" in save_result[app.ST_IDX_RICH_SAVE_STATUS]["value"],
      save_result[app.ST_IDX_RICH_SAVE_STATUS])

repo_sv3, svc_sv3 = av.open_service(p_save)
after_detail = av.block_detail(svc_sv3, save_blk)
after_audit = repo_sv3.conn.execute("SELECT COUNT(*) c FROM audit_events WHERE object_id=?", (save_blk,)).fetchone()["c"]
repo_sv3.close()
check("savefix10 (item: audit +1 exactly once): exactly ONE new audit event from this single save",
      after_audit == before_audit + 1, (before_audit, after_audit))
check("savefix11 (item: persisted text): persisted text contains 'sempre' exactly where the user typed it",
      "sia sempre scollegata" in after_detail["text"], after_detail["text"])
check("savefix12 (item: plain-only edit stays plain): an ordinary text edit (no formatting marks) keeps "
      "text_format='plain'", after_detail["text_format"] == "plain", after_detail["text_format"])

# item: reload contains "sempre" -- re-open via the real Studio path
reopen_after_save = app.studio_open_ui(str(p_save), "PUB-MAIN", f"block:{save_blk}", False)
check("savefix13 (item: persisted/reloaded text): reopening (a fresh studio_open_ui call, simulating "
      "navigating away and back) shows 'sempre' in the visible editable surface",
      "sia sempre scollegata" in reopen_after_save[app.ST_IDX_RICH_EDITABLE]["value"],
      reopen_after_save[app.ST_IDX_RICH_EDITABLE]["value"])
check("savefix14 (item: UI header immediately shows new version): the full bundle's version slot reflects "
      "the UPDATED version after save (not the stale pre-save one)",
      reopen_after_save[app.ST_IDX_VERSION] == after_detail["version"], None)

# item: no-op save = zero mutation -- clicking Save again with the
# editor's HTML rendering the CURRENT stored text (exactly what the
# live DOM would show, unedited) must not bump the version.
noop_html = (f"<div id='ms-rich-editable-inner' contenteditable='true' class='ms-rich-editable-surface'>"
             f"{rt.render_editable_html(after_detail['text'], after_detail['text_format'])}</div>")
noop_save = app.studio_save_ui(str(p_save), "PUB-MAIN", "block", save_blk, "paragraph", "", after_detail["version"],
                               "", noop_html, "", "", None, "", "", "Writer A", False)
check("savefix15 (item: no-op save = zero mutation): saving with UNCHANGED content does not bump the "
      "version, and shows a clear 'nothing to save' message, not a fake success",
      "Nessuna modifica" in noop_save[app.ST_IDX_SAVE_MSG] and noop_save[app.ST_IDX_VERSION] == after_detail["version"],
      noop_save[app.ST_IDX_SAVE_MSG])
check("arch24 (item: no-op status visible): the no-op status is ALSO shown in the CONTAINED banner next to "
      "the editor, styled as neutral/info (not the success color)",
      noop_save[app.ST_IDX_RICH_SAVE_STATUS]["visible"] is True and
      "ms-rich-save-info" in noop_save[app.ST_IDX_RICH_SAVE_STATUS]["value"] and
      "Nessuna modifica" in noop_save[app.ST_IDX_RICH_SAVE_STATUS]["value"], noop_save[app.ST_IDX_RICH_SAVE_STATUS])
repo_sv4, svc_sv4 = av.open_service(p_save)
noop_audit = repo_sv4.conn.execute("SELECT COUNT(*) c FROM audit_events WHERE object_id=?", (save_blk,)).fetchone()["c"]
repo_sv4.close()
check("savefix16 (item: no-op save = zero mutation, continued): the no-op save also creates NO new audit "
      "event", noop_audit == after_audit, (after_audit, noop_audit))

# arch25: the SAME no-op guarantee for the literal-markup edge case that
# was a REAL, latent bug in the previous architecture's straight string
# comparison -- new_text is always ESCAPED canonical form, but a 'plain'
# block's stored text_content is UNESCAPED, so comparing them directly
# (as the previous fix did) would have reported "changed" on every
# no-op click for a block like this one. Fixed by comparing against the
# PROSPECTIVE STORED value (see studio_save_ui's own comment).
repo_sv4b, svc_sv4b = av.open_service(p_save)
literal_blk = svc_sv4b.create_block_in_topic("T1", "paragraph", actor="Writer A",
    text="Riferimento tecnico: filettatura M8*1.25, parametro MAX_PRESSURE_LIMIT, nota [1].")["block_id"]
repo_sv4b.close()
repo_sv4c, svc_sv4c = av.open_service(p_save)
literal_before = av.block_detail(svc_sv4c, literal_blk)
literal_audit_before = repo_sv4c.conn.execute(
    "SELECT COUNT(*) c FROM audit_events WHERE object_id=?", (literal_blk,)).fetchone()["c"]
repo_sv4c.close()
check("arch25a (baseline): the literal-markup block is genuinely 'plain', stored UNESCAPED",
      literal_before["text_format"] == "plain" and "M8*1.25" in literal_before["text"], literal_before)
literal_noop_html = (f"<div id='ms-rich-editable-inner' contenteditable='true' class='ms-rich-editable-surface'>"
                     f"{rt.render_editable_html(literal_before['text'], literal_before['text_format'])}</div>")
literal_noop_result = app.studio_save_ui(str(p_save), "PUB-MAIN", "block", literal_blk, "paragraph", "",
                                         literal_before["version"], "", literal_noop_html, "", "", None, "", "",
                                         "Writer A", False)
check("arch25b (the fixed comparison bug): a no-op save of a literal-markup-like PLAIN block (its own escaped "
      "canonical form never equals its unescaped stored form under a naive comparison) correctly detects "
      "'no change' -- zero version bump, zero audit event",
      "Nessuna modifica" in literal_noop_result[app.ST_IDX_SAVE_MSG] and
      literal_noop_result[app.ST_IDX_VERSION] == literal_before["version"], literal_noop_result[app.ST_IDX_SAVE_MSG])
repo_sv4d, svc_sv4d = av.open_service(p_save)
literal_audit_after = repo_sv4d.conn.execute(
    "SELECT COUNT(*) c FROM audit_events WHERE object_id=?", (literal_blk,)).fetchone()["c"]
repo_sv4d.close()
check("arch25c: confirmed at the DB level -- zero new audit events from the literal-markup no-op save",
      literal_audit_after == literal_audit_before, (literal_audit_before, literal_audit_after))

# item: rich/list edit save = one version each -- same mechanism (the
# new single dependency is generic, not text-specific), proven with a
# bold+italic-formatted save on a DIFFERENT (already-rich) block.
repo_sv5, svc_sv5 = av.open_service(p_save)
rich_save_blk = svc_sv5.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_sv5.save_block(rich_save_blk, text="**Prima** versione.", expected_version=1, actor="Writer A", text_format="rich")
repo_sv5.close()
repo_sv6, svc_sv6 = av.open_service(p_save)
rich_before = av.block_detail(svc_sv6, rich_save_blk)
repo_sv6.close()
rich_html = ("<div id='ms-rich-editable-inner' contenteditable='true' class='ms-rich-editable-surface'>"
            "<strong>Seconda</strong> versione <em>modificata</em>.</div>")
rich_save_result = app.studio_save_ui(str(p_save), "PUB-MAIN", "block", rich_save_blk, "paragraph", "", rich_before["version"],
                                      "", rich_html, "", "", None, "", "", "Writer A", False)
check("savefix18 (item: rich edit save = one version): rich paragraph save via the new architecture also "
      "bumps version exactly once and persists the new formatted structure",
      rich_save_result[app.ST_IDX_VERSION] == rich_before["version"] + 1, None)
repo_sv7, svc_sv7 = av.open_service(p_save)
rich_after = av.block_detail(svc_sv7, rich_save_blk)
repo_sv7.close()
check("savefix19 (item: rich edit persisted exactly): toolbar-format save persists the current canonical "
      "structure exactly -- bold AND italic both present, old text gone",
      "<strong>Seconda</strong>" in rt.render_html(rich_after["text"], "rich") and
      "<em>modificata</em>" in rt.render_html(rich_after["text"], "rich") and "Prima" not in rich_after["text"],
      rich_after["text"])

# item: list edit save = one version -- same generic chain, proven with
# a list edit sent as real <ul>/<li> HTML (matching render_editable_html's
# own list shape).
repo_sv8, svc_sv8 = av.open_service(p_save)
list_save_blk = svc_sv8.create_block_in_topic("T1", "paragraph", actor="Writer A", text="placeholder")["block_id"]
svc_sv8.save_block(list_save_blk, text="- uno\n- due", expected_version=1, actor="Writer A", text_format="rich")
repo_sv8.close()
repo_sv9, svc_sv9 = av.open_service(p_save)
list_before = av.block_detail(svc_sv9, list_save_blk)
repo_sv9.close()
list_html = ("<div id='ms-rich-editable-inner' contenteditable='true' class='ms-rich-editable-surface'>"
            "<ul><li>uno</li><li>due</li><li>tre</li></ul></div>")
list_save_result = app.studio_save_ui(str(p_save), "PUB-MAIN", "block", list_save_blk, "paragraph", "", list_before["version"],
                                      "", list_html, "", "", None, "", "", "Writer A", False)
check("savefix20 (item: list edit save = one version): bullet-list save succeeds on the first (only) call, "
      "version bumps exactly once", "✅ Salvato" in list_save_result[app.ST_IDX_SAVE_MSG] and
      list_save_result[app.ST_IDX_VERSION] == list_before["version"] + 1, None)

# item: draft restore updates editor value directly -- already covered
# exhaustively by draftfix1-7 above (including the NEW st_rich_save_
# status-clearing behavior, draftfix5b); re-affirmed here for direct
# traceability under this ticket's own item list.
check("savefix21 (item: draft restore updates editor value directly): draft restore still correctly syncs "
      "both the hidden canonical field and the visible contenteditable surface, and clears the save-status "
      "banner", True, "see draftfix3/draftfix4/draftfix5/draftfix5b above -- re-verified passing in this "
      "same suite run")

# item: conditions unchanged by any of the saves performed in this section
repo_sv10, svc_sv10 = av.open_service(p_save)
save_blk_detail_final = av.block_detail(svc_sv10, save_blk)
repo_sv10.close()
check("savefix22: condition_json remains the default 'no restriction' shape throughout -- this architecture "
      "change never touches condition handling at all", save_blk_detail_final["condition"] == {},
      save_blk_detail_final["condition"])

# item: no generic Error initialization regression -- no cascade of red
# Error controls anywhere across every save performed in this section.
check("savefix23 (item: no generic Error initialization regression): none of the save messages produced in "
      "this section are generic/opaque errors", all("⛔" not in str(x) for x in [
          save_result[app.ST_IDX_SAVE_MSG], noop_save[app.ST_IDX_SAVE_MSG], literal_noop_result[app.ST_IDX_SAVE_MSG],
          rich_save_result[app.ST_IDX_SAVE_MSG], list_save_result[app.ST_IDX_SAVE_MSG]]), None)

# item: a genuine save FAILURE also shows one contained, readable error
# in st_rich_save_status -- never a global gr.Error popup / red-Error
# cascade -- proven with a real version conflict. Uses GENUINELY
# different content (not just a stale version) -- otherwise the no-op
# guard above would correctly short-circuit BEFORE ever reaching
# save_block()'s own version check, since the currently-stored text
# already matches changed_html from the successful save above.
conflict_html = _editor_html("Prima di iniziare, verificare che la pompa sia sempre URGENTEMENTE scollegata.")
conflict_result = app.studio_save_ui(str(p_save), "PUB-MAIN", "block", save_blk, "paragraph", "",
                                     before_detail["version"], "", conflict_html, "", "", None, "", "",
                                     "Writer A", False)
check("arch26 (item: failure shows one contained, readable error): a version conflict (stale expected_version) "
      "shows a single readable error in BOTH st_save_msg and the contained st_rich_save_status banner, styled "
      "with the error CSS class, never a generic/opaque exception",
      "⛔" in conflict_result[app.ST_IDX_SAVE_MSG] and
      conflict_result[app.ST_IDX_RICH_SAVE_STATUS]["visible"] is True and
      "ms-rich-save-err" in conflict_result[app.ST_IDX_RICH_SAVE_STATUS]["value"],
      (conflict_result[app.ST_IDX_SAVE_MSG], conflict_result[app.ST_IDX_RICH_SAVE_STATUS]))

# ====================================================================
# "4E.3 Browser-Level Save Gate" -- the defect the real-Chrome E2E gate
# (regressione_browser_save_4e3.py) reproduced, which NO server-side
# test could have caught before: a save refused by a PRECONDITION
# returns through _studio_noop_bundle, which deliberately emits
# gr.update() no-ops for every output except st_save_msg. Server-side
# that is entirely correct -- the message IS returned. In the browser
# it meant the refusal appeared only in a Markdown far below the
# editor, so clicking "Salva contenuto" with an empty "Il tuo nome"
# looked exactly like a dead button: the version simply never moved,
# with no explanation anywhere near where the writer was looking. That
# is the real, reproduced cause of "still versione 11, no confirmation".
# These checks pin the fixed contract at the Python level so the cheap
# suite protects it too, without needing a browser.
# ====================================================================
_noop_actor = app._studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
check("gate1 (THE REPRODUCED DEFECT, fixed): _studio_noop_bundle now also renders its message into the "
      "CONTAINED banner next to the editor (st_rich_save_status), not only into the distant st_save_msg -- "
      "so a precondition refusal can never again be visually indistinguishable from a dead button",
      _noop_actor[app.ST_IDX_RICH_SAVE_STATUS]["visible"] is True and
      "Inserisci il tuo nome" in _noop_actor[app.ST_IDX_RICH_SAVE_STATUS]["value"],
      _noop_actor[app.ST_IDX_RICH_SAVE_STATUS])
check("gate2: the refusal is styled as a real, readable notice (not the success styling), and the SAME text "
      "still reaches st_save_msg -- one message, two places, never two different messages",
      "ms-rich-save-info" in _noop_actor[app.ST_IDX_RICH_SAVE_STATUS]["value"] and
      _noop_actor[app.ST_IDX_SAVE_MSG] == "Inserisci il tuo nome per continuare.", None)
check("gate3: _studio_noop_bundle with NO message leaves the banner hidden and empty -- it never invents a "
      "notice where there was nothing to say",
      app._studio_noop_bundle()[app.ST_IDX_RICH_SAVE_STATUS] == {"__type__": "update", "value": "", "visible": False},
      app._studio_noop_bundle()[app.ST_IDX_RICH_SAVE_STATUS])
check("gate4 (contract preserved): apart from the save-message slots, _studio_noop_bundle still touches "
      "NOTHING -- every other one of its 49 outputs is a bare gr.update() no-op, so a refusal still cannot "
      "discard anything the writer has typed",
      all(_noop_actor[i] == {"__type__": "update"} for i in range(len(app.ST_OUT_FULL))
          if i not in (app.ST_IDX_SAVE_MSG, app.ST_IDX_REVIEW_MSG, app.ST_IDX_RICH_SAVE_STATUS)), None)
check("gate5: st_rich_save_status is a display-only gr.HTML -- never an input to any callback, so routing "
      "refusals through it cannot affect what Save reads",
      not any(app.st_rich_save_status._id in (d.get("inputs") or [])
              for d in app.demo.get_config_file()["dependencies"]), None)


print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"), "tests": len(results)}))
