"""UX Gate 2A — Authoring Workspace Core regression suite.

Covers the interaction-architecture changes only. Authoring BUSINESS rules
(versioning, draft autosave/recovery, archive/restore, shared objects,
table/figure model, review/audit, publication model) are unchanged by this
gate and stay covered by their own suites -- 4B.1, 4B.2, 4C, 4D, 4E.1,
4E.3 -- which this gate re-ran in full.

What is pinned here:

  1. ONE variant profile. Two controls used to ask "which profile?" with
     no link between them; this proves they cannot diverge.
  2. ONE operator identity, feeding every authoring mutation.
  3. The three-zone structure: left navigates, centre writes, right
     gives context.
  4. Non-daily surfaces really left the writing surface, and really
     arrived somewhere -- nothing was deleted.
  5. Every new or moved user-facing control is localized, IT and EN.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import app  # noqa: E402
import i18n  # noqa: E402
import authoring_view as av  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="manualsync_gate2a_"))
_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))


APP_SRC = (BASE / "app.py").read_text(encoding="utf-8")
DEPS = app.demo.config["dependencies"]


def deps_reading(component):
    return [d for d in DEPS if component._id in d.get("inputs", [])]


def deps_writing(component):
    return [d for d in DEPS if component._id in d.get("outputs", [])]


def _new_project(name):
    p = create_project(TMP, project_name=name, source_lang="en-US")
    repo = IndustrialRepository(p / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("T-ONE", "Primo topic", "", status="Draft")
        repo.create_publication("PUB-MAIN", "Manuale sintetico", ["T-ONE"])
        repo.add_block("BLK-ONE", "T-ONE", "paragraph", position=1, text="Testo.", status="Draft")
    finally:
        repo.close()
    return p


# ==================================================================
# 1. ONE variant profile — the two controls cannot diverge
# ==================================================================

def test_single_profile_state_exists():
    check("profile_state is a real gr.State", isinstance(app.profile_state, app.gr.State),
          type(app.profile_state))


def test_exactly_two_profile_selectors_both_bound():
    """Two views are allowed; two unlinked SOURCES OF TRUTH are not."""
    both = [app.st_variant_eval_profile, app.pv_profile]
    for comp, who in zip(both, ("Contenuti evaluator", "Rilascio preview")):
        writes = [d for d in deps_reading(comp) if app.profile_state._id in d.get("outputs", [])]
        check(f"{who}: picking a profile writes profile_state", len(writes) >= 1,
              [d.get("api_name") for d in writes])
        reads = [d for d in deps_writing(comp) if app.profile_state._id in d.get("inputs", [])]
        check(f"{who}: is refreshed FROM profile_state, so it shows the shared value", len(reads) >= 1,
              [d.get("api_name") for d in reads])


def test_no_second_profile_selector_in_contenuti():
    """The acceptance criterion: one profile control on the daily screen."""
    labelled = []
    for name in dir(app):
        obj = getattr(app, name, None)
        label = getattr(obj, "label", None)
        if isinstance(label, str) and label.strip().lower() in ("profilo", "variant profile",
                                                               "profilo di variante"):
            labelled.append(name)
    # pv_profile lives in Rilascio; st_variant_eval_profile in Contenuti.
    check("exactly two profile-labelled controls exist app-wide, one per tab",
          sorted(labelled) == ["pv_profile", "st_variant_eval_profile"], sorted(labelled))
    check("the bare label 'Profilo' is gone -- both now say which profile they mean",
          'label="Profilo"' not in APP_SRC and "label='Profilo'" not in APP_SRC, "")


def test_choice_builder_is_shared():
    """Both selectors must be filled by the same function, not two copies."""
    check("studio_preview_profile_choices_ui is the shared choice builder",
          callable(app.studio_preview_profile_choices_ui), "")
    # Gradio suffixes the api_name of each additional wiring of the same
    # function (..._1, ..._2), so match the family, not one exact string.
    fills = [d for d in DEPS
             if str(d.get("api_name") or "").startswith("studio_preview_profile_choices_ui")]
    targets = {i for d in fills for i in d.get("outputs", [])}
    check("it fills the Contenuti selector", app.st_variant_eval_profile._id in targets, "")
    check("it fills the Rilascio selector", app.pv_profile._id in targets, "")
    check("and it writes the shared state on every one of those paths",
          all(app.profile_state._id in d.get("outputs", []) for d in fills),
          [d.get("outputs") for d in fills])


def test_selection_survives_opening_next_object():
    """The divergence bug, functionally: the panel refresh must not silently
    reset the profile while the other surface keeps showing it."""
    p = _new_project("g2a_profile_survives")
    repo, svc = av.open_service(p)
    try:
        svc.create_variant_profile("PROF-A", "Profilo A", {}, actor="Writer")
        svc.create_variant_profile("PROF-B", "Profilo B", {}, actor="Writer")
    finally:
        repo.close()

    bundle = app.studio_variant_panel_ui(str(p), "block", "BLK-ONE", "PROF-A")
    check("opening an object keeps the chosen profile (panel bundle carries it back)",
          bundle[-1] == "PROF-A", bundle[-1])
    dd_update = bundle[-3]
    check("and the selector itself still shows it, rather than resetting to None",
          dict(dd_update).get("value") == "PROF-A", dict(dd_update))

    # the other surface, asked independently, agrees
    _upd, kept = app.studio_preview_profile_choices_ui(str(p), "PROF-A")
    check("the Rilascio selector, refreshed from the same state, shows the same profile",
          kept == "PROF-A", kept)


def test_vanished_profile_is_dropped_everywhere():
    """A profile that no longer exists must fall out of BOTH surfaces --
    keeping a dangling id is how they would silently disagree again."""
    p = _new_project("g2a_profile_vanishes")
    repo, svc = av.open_service(p)
    try:
        svc.create_variant_profile("PROF-REAL", "Profilo reale", {}, actor="Writer")
    finally:
        repo.close()
    bundle = app.studio_variant_panel_ui(str(p), "block", "BLK-ONE", "PROF-GONE")
    check("Contenuti drops a profile id that does not exist", bundle[-1] is None, bundle[-1])
    _upd, kept = app.studio_preview_profile_choices_ui(str(p), "PROF-GONE")
    check("Rilascio drops it too", kept is None, kept)
    check("a still-valid one is kept by both",
          app.studio_variant_panel_ui(str(p), "block", "BLK-ONE", "PROF-REAL")[-1] == "PROF-REAL"
          and app.studio_preview_profile_choices_ui(str(p), "PROF-REAL")[1] == "PROF-REAL", "")


def test_no_project_leakage():
    p1 = _new_project("g2a_leak_1")
    p2 = _new_project("g2a_leak_2")
    repo, svc = av.open_service(p1)
    try:
        svc.create_variant_profile("PROF-P1", "Solo P1", {}, actor="Writer")
    finally:
        repo.close()
    _upd, kept = app.studio_preview_profile_choices_ui(str(p2), "PROF-P1")
    check("switching project drops a profile belonging to the previous one", kept is None, kept)


# ==================================================================
# 2. ONE operator identity
# ==================================================================

def test_single_operator_identity():
    check("the tab-local duplicate identity field is gone", not hasattr(app, "c_actor"), "")
    check("actor_state is fed by exactly one sync dependency",
          len([d for d in deps_reading(app.ws_operator)
               if d.get("outputs") == [app.actor_state._id]]) == 1, "")
    authoring_reading_live = [d for d in deps_reading(app.ws_operator)
                              if str(d.get("api_name") or "").startswith("studio_")]
    check("no authoring callback reads the live operator Textbox directly",
          authoring_reading_live == [], [d.get("api_name") for d in authoring_reading_live])
    check("many mutation callbacks read actor_state",
          len(deps_reading(app.actor_state)) >= 20, len(deps_reading(app.actor_state)))


def test_actor_requirement_still_fail_closed():
    """Interaction architecture changed; the fail-closed rule did not."""
    p = _new_project("g2a_actor_failclosed")
    bundle = app.studio_new_topic_ui(str(p), "PUB-MAIN", "Un titolo", None, None, False, "")
    check("an empty operator still blocks the write with inline guidance",
          bundle[app.ST_IDX_SAVE_MSG] == "Inserisci il tuo nome per continuare.",
          bundle[app.ST_IDX_SAVE_MSG])


# ==================================================================
# 3. The three zones
# ==================================================================

def test_three_zones_declared():
    for cls in ("ms-authoring-workspace", "ms-pane-left", "ms-pane-centre", "ms-pane-right"):
        check(f"the workspace declares {cls}", cls in APP_SRC, "")


def test_pane_css_present():
    for rule in ("position:sticky",
                 ".gradio-container:has(.ms-authoring-workspace){overflow:visible!important}",
                 "overflow-y:auto"):
        check(f"CSS carries {rule!r} for the side panes", rule in APP_SRC, "")


def test_editor_is_in_the_centre_and_save_next_to_it():
    """Structural, not pixel: Save must be declared inside the centre pane,
    after the editor and before the right pane opens."""
    centre = APP_SRC.index('"ms-pane-centre"')
    right = APP_SRC.index('"ms-pane-right"')
    editor = APP_SRC.index("st_rich_editable = gr.HTML(")
    save = APP_SRC.index('st_save_btn = gr.Button(')
    check("the rich editor is inside the centre pane", centre < editor < right, (centre, editor, right))
    check("Save is inside the centre pane too", centre < save < right, (centre, save, right))
    check("Save comes after the editor, not before it", editor < save, (editor, save))
    variants = APP_SRC.index("as st_variant_panel:")
    check("the variants panel no longer sits between the editor and Save",
          not (editor < variants < save), (editor, variants, save))
    check("the variants panel is in the right pane", variants > right, (variants, right))


# ==================================================================
# 4. Moved, not deleted
# ==================================================================

MOVED = [
    # (component, must still exist, where it must now be declared)
    ("pv_profile", "ms-pane-right"),
    ("pv_export_pdf_btn", None),
    ("pv_export_html_btn", None),
    ("pv_output", None),
    ("rv_queue", None),
    ("rv_decide", None),
    ("g_generate", None),
    ("g_queue", None),
    ("c_import", None),
    ("c_simple_approve", None),
]


def test_nothing_was_deleted():
    for name, _ in MOVED:
        check(f"{name} still exists after the move", hasattr(app, name), "")


def test_preview_and_export_left_contenuti():
    """They must be declared after the Rilascio tab opens."""
    release_tab = APP_SRC.index('with gr.Tab(t("nav.release"),id="release")')
    for name in ("pv_profile", "pv_refresh_btn", "pv_output",
                 "pv_export_pdf_btn", "pv_export_html_btn", "pv_export_status"):
        pos = APP_SRC.index(f"{name} = ")
        check(f"{name} is declared in Rilascio, not Contenuti", pos > release_tab, (pos, release_tab))


def test_review_queue_moved_to_revisione():
    revision_tab = APP_SRC.index('with gr.Tab(t("nav.revision"),id="revision")')
    content_tab = APP_SRC.index('with gr.Tab(t("nav.content"),id="authoring")')
    for name in ("rv_refresh=", "rv_queue=", "rv_decide="):
        pos = APP_SRC.index(name)
        check(f"{name.rstrip('=')} is declared in Revisione, before Contenuti opens",
              revision_tab < pos < content_tab, (revision_tab, pos, content_tab))


def test_review_decision_semantics_unchanged():
    """The queue moved; the decision gate did not change."""
    dep = next((d for d in DEPS if d.get("api_name") == "review_decide_ui"), None)
    check("review_decide_ui is still wired", dep is not None, "")
    check("the reviewer keeps its own identity field, not the workspace operator",
          dep is not None and app.actor_state._id not in dep.get("inputs", [])
          and app.rv_actor._id in dep.get("inputs", []), dep)


def test_grounded_authoring_reachable_not_redesigned():
    """Gate 2A only stops it being buried; 2B integrates it."""
    right = APP_SRC.index('"ms-pane-right"')
    pos = APP_SRC.index("g_ingest = gr.Button(")
    check("Grounded Authoring is declared in the right contextual pane", pos > right, (pos, right))
    # exactly one accordion deep now, not three
    check("it is no longer nested inside 'Dettagli tecnici'",
          "with gr.Accordion(\"AI Grounded Authoring\",open=False)" not in APP_SRC, "")
    for fn in ("grounding_ingest_ui", "grounded_generate_ui", "grounded_queue_ui"):
        check(f"{fn} is still wired -- behaviour preserved",
              any(d.get("api_name") == fn for d in DEPS), "")


def test_import_moved_behind_an_accordion():
    content_tab = APP_SRC.index('with gr.Tab(t("nav.content"),id="authoring")')
    # the class name also appears in the stylesheet near the top of the
    # file -- anchor on the layout Row itself, inside the tab.
    workspace = APP_SRC.index('gr.Row(elem_classes=["ms-authoring-workspace"])')
    pos = APP_SRC.index("c_import=gr.Button(")
    # It must be out of the daily path entirely: inside the Contenuti tab,
    # but BELOW the workspace so it cannot push the editor down the page,
    # and behind a collapsed accordion so it costs no vertical space.
    check("source import is inside the Contenuti tab", pos > content_tab, (content_tab, pos))
    check("source import is declared BELOW the workspace, so it never pushes the editor down",
          pos > workspace, (pos, workspace))
    check("and it is behind an accordion, not on the open page",
          't("content.import.section")' in APP_SRC, "")


# ==================================================================
# 5. Localization of new and moved controls
# ==================================================================

NEW_KEYS = [
    "content.import.section", "content.import.hint", "content.import.review.title",
    "content.import.review.sub", "content.import.check.title",
    "content.pane.structure", "content.pane.detail",
    "content.variants.title", "content.variants.sub", "content.variants.criterion",
    "content.variants.values", "content.variants.save", "content.variants.evaluate.sub",
    "content.variants.evaluate", "content.profile.label", "content.profile.hint",
    "content.tech.section", "content.grounded.section", "content.grounded.hint",
    "release.preview.title", "release.preview.sub", "release.preview.refresh",
    "release.preview.show_excluded", "release.preview.empty", "release.export.sub",
    "revision.queue.section", "revision.queue.hint",
]


def test_every_new_key_exists_in_both_catalogs():
    for key in NEW_KEYS:
        for lang in ("it", "en"):
            val = i18n.translate(key, lang)
            check(f"catalog[{lang}] defines {key}", val != key and val.strip() != "", repr(val))


def test_it_and_en_differ_where_they_should():
    """A key whose IT and EN are identical is usually an untranslated
    placeholder. 'AI Grounded Authoring' is a genuine proper noun."""
    same_ok = {"content.grounded.section"}
    for key in NEW_KEYS:
        if key in same_ok:
            continue
        it_v, en_v = i18n.translate(key, "it"), i18n.translate(key, "en")
        check(f"{key}: EN is a real translation, not the Italian string", it_v != en_v,
              (it_v[:40], en_v[:40]))


def test_moved_controls_are_loc_registered():
    registered = {id(c) for c in app.LOC.components()}
    for name in ("st_variant_eval_profile", "pv_profile", "pv_show_excluded",
                 "st_variant_save_btn", "st_variant_eval_btn", "pv_refresh_btn"):
        comp = getattr(app, name)
        check(f"{name} is registered for language switching", id(comp) in registered, name)


def test_no_new_hardcoded_italian_label_in_the_workspace():
    """The zone headings and the profile control must not carry Italian
    literals -- they go through the catalog."""
    # Only LABEL uses matter. These strings still appear in comments that
    # explain the history of the widgets, which is documentation, not UI.
    #
    # Note the Contenuti heading is matched in full: Impostazioni has its
    # own, different "Varianti — dimensioni, valori e profili" section for
    # variant ADMINISTRATION. That one did not move in this gate and is
    # localized with the Impostazioni redesign, not here.
    for literal in ('label="Profilo"',
                    'gr.Button("Salva varianti")',
                    "gr.Accordion(\"Coda di revisione\"",
                    "<div class='ms-section-title'>Varianti — dove usare questo contenuto</div>",
                    "<div class='ms-section-title'>Anteprima pubblicazione</div>"):
        check(f"no hardcoded {literal} is used as a label", literal not in APP_SRC, "")


def test_language_switch_covers_the_new_controls():
    with i18n.use_language("en"):
        updates = app.LOC.updates()
    check("the language switch emits one update per registered component",
          len(updates) == len(app.LOC), (len(updates), len(app.LOC)))
    en_texts = " ".join(str(dict(u).get("label") or dict(u).get("value") or "") for u in updates)
    check("switching to EN really produces the English profile label",
          i18n.translate("content.profile.label", "en") in en_texts, "")
    check("and the English structure heading", i18n.translate("content.pane.structure", "en") in en_texts, "")


def run():
    for fn in sorted((v for k, v in globals().items() if k.startswith("test_")),
                     key=lambda f: f.__code__.co_firstlineno):
        try:
            fn()
        except Exception as exc:
            check(f"{fn.__name__} raised", False, f"{type(exc).__name__}: {exc}")
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("=" * 78)
    print("REGRESSIONE — UX GATE 2A: AUTHORING WORKSPACE CORE")
    print("=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print(f"\n  {passed}/{total} passed")
    print("=" * 78)
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
