"""BROWSER / E2E GATE -- ERYLOS Impact Center V1 acceptance.

*** NOT part of the lightweight Python suite. *** Run it at an Impact
Center checkpoint. It owns everything it needs: it builds its own
disposable ``ZZ_`` project, points the app at it through
``acceptance_guard``, starts its own app server on its own free port,
drives one headless Chrome, then restores the pointer and deletes the
fixture.

Why a browser gate for a read-only screen
-----------------------------------------
The Python suite proves the orchestrator groups findings correctly. It
cannot prove the customer ever sees them. Three things are only true in
a browser:

  * the panels are reachable at all -- the tab populates its picker from
    ``tab_impact.select``, and this build's ``gr.State.change`` does not
    fire reliably, so the wiring is the risk, not the logic;
  * a finding lands in the RIGHT section -- asserted per elem_id
    container, because "the word appears on the page" would pass even if
    a stale translation were filed under "Cosa e pronto";
  * the vocabulary rule holds in RENDERED text -- catalogue inspection
    misses prose assembled at runtime, which is exactly how an English
    "Fuzzy matches" survived a catalogue review.

Scenario: seed a translated, approved block -> edit it -> the screen
must show the manual and chapter it touches, move the now-stale language
and the reopened requirement into "Cosa richiede attenzione", print
"Pronto al rilascio: NO", and change nothing in the database.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_browser_impact_center.py
        MS_KEEP_FIXTURE=1 keeps the project for manual inspection.
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance_fixture as fx  # noqa: E402
import authoring_view as av  # noqa: E402
import compliance_view as cv  # noqa: E402
from acceptance_guard import overlay_snapshot, report_delta  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
# The Chrome driver is shared with the 4E.3 gate rather than copied.
# Importing that module is safe: it resolves its own project only in
# main(), so it never trips the guard on import.
from regressione_browser_save_4e3 import Browser  # noqa: E402

ACTOR = "Impact Center Browser Gate"
TARGET_LANG = "de-DE"
EDIT_SUFFIX = " Verificare prima di ogni intervento."

SHOT_DIR = Path(__file__).with_name("RISULTATO_IMPACT_CENTER_UI")
BANNED = ("fuzzy", "source_version", "object_version", "work_bucket")

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if not cond else ""))


# ----------------------------------------------------------------------
# Fixture seeding -- through the services, so the rows carry real
# versions and real audit history.
# ----------------------------------------------------------------------

def seed(project_dir: Path) -> dict:
    project = Path(project_dir)
    facts: dict = {}

    repo, svc = av.open_service(str(project))
    try:
        topic = repo.conn.execute(
            "SELECT topic_id, title FROM topics ORDER BY topic_id LIMIT 1").fetchone()
        facts["topic_id"] = topic["topic_id"]
        facts["topic_title"] = topic["title"]
        blk = svc.create_block_in_topic(
            facts["topic_id"], "paragraph", actor=ACTOR,
            text="La pressione massima ammessa e 12 bar.", text_format="plain")
        facts["block_id"] = blk["block_id"]
    finally:
        repo.close()

    # Approve it, so the pre-edit state is genuinely releasable and the
    # gate measures the effect of the EDIT, not of a draft.
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        rid = repo.request_review("block", facts["block_id"], requested_by=ACTOR,
                                  assigned_to=ACTOR)
        repo.decide_review(rid, decision="APPROVE", actor=ACTOR)
        version = int(repo.conn.execute(
            "SELECT version FROM content_blocks WHERE block_id=?",
            (facts["block_id"],)).fetchone()["version"])
        repo.conn.execute(
            """INSERT INTO block_translations(block_id,target_lang,source_version,
                                              target_text,status,updated_at)
               VALUES(?,?,?,?,?,datetime('now'))""",
            (facts["block_id"], TARGET_LANG, version,
             "Der maximal zulaessige Druck betraegt 12 bar.", "Reviewed"))
        repo.conn.commit()
        facts["translated_at_version"] = version
    finally:
        repo.close()

    # A compliance requirement whose evidence points at that version, so
    # the edit reopens it.
    crepo, csvc = cv.open_service(str(project))
    try:
        csvc.create_instrument("INST-ICG", "TEST/ICG", actor=ACTOR, kind="directive",
                               title="Direttiva di prova")
        csvc.create_requirement("REQ-ICG", "Avvertenze sull'uso scorretto", actor=ACTOR,
                                instrument_id="INST-ICG", article_ref="Art. 1.7.4",
                                description="Requisito di prova")
        pub_id = facts_publication(crepo)
        csvc.create_scope("SCOPE-ICG", publication_id=pub_id, title="Ambito di prova",
                          jurisdiction="EU", actor=ACTOR)
        csvc.add_scope_reference("SCOPE-ICG", ref_kind="instrument", ref_id="INST-ICG",
                                 actor=ACTOR)
        csvc.set_applicability("SCOPE-ICG", "REQ-ICG", "APPLIES", actor=ACTOR,
                               justification="prova")
        ev = f"EV-{facts['block_id']}-v{facts['translated_at_version']}"
        csvc.add_evidence(ev, "block", actor=ACTOR, object_id=facts["block_id"],
                          object_version=facts["translated_at_version"],
                          description="Evidenza di prova")
        csvc.link_evidence_requirement(ev, "REQ-ICG", actor=ACTOR)
        csvc.set_assessment("SCOPE-ICG", "REQ-ICG", "COVERED", actor=ACTOR, comment="ok")
        facts["requirement_title"] = "Avvertenze sull'uso scorretto"
    finally:
        crepo.close()
    return facts


def facts_publication(repo) -> str:
    row = repo.conn.execute("SELECT publication_id FROM publications LIMIT 1").fetchone()
    return row["publication_id"]


def edit_the_block(project: Path, block_id: str) -> int:
    """The change whose impact the screen must explain."""
    repo, svc = av.open_service(str(project))
    try:
        d = av.block_detail(svc, block_id)
        svc.save_block(block_id, text=d["text"] + EDIT_SUFFIX,
                       expected_version=int(d["version"]), actor=ACTOR)
        return int(av.block_detail(svc, block_id)["version"])
    finally:
        repo.close()


def db_fingerprint(project: Path) -> tuple:
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        counts = tuple((tbl, repo.conn.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()["c"])
                       for tbl in ("content_blocks", "block_versions", "block_translations",
                                   "topics", "audit_events", "review_tasks",
                                   "compliance_assessment", "compliance_evidence",
                                   "authoring_drafts"))
        rows = tuple(tuple(r) for r in repo.conn.execute(
            "SELECT block_id,version,status FROM content_blocks ORDER BY block_id"))
        return counts, rows
    finally:
        repo.close()


# ----------------------------------------------------------------------
# Browser helpers
# ----------------------------------------------------------------------

# "Rilascio" immediately followed by the verdict: the band must be its
# own heading, not a group tucked under "Cosa e pronto".
RELEASE_BAND = re.compile(r"Rilascio\s+Pronto al rilascio")

SECTIONS = {"header": "#ms-ic-header", "changes": "#ms-ic-changes",
            "attention": "#ms-ic-attention", "ready": "#ms-ic-ready",
            "advanced": "#ms-ic-advanced"}


def wait_idle(b: Browser, timeout: float = 60.0):
    """Wait until no Gradio event is in flight.

    While one is, Gradio paints a progress badge INSIDE the very
    components this gate reads -- an empty panel reads back as "0.2s".
    That is how the empty-state assertion first failed against a screen
    that was behaving correctly, and how it would later have passed by
    luck. Idle is a precondition for every panel read here.
    """
    b.wait_for("""
      (() => document.querySelectorAll(
        '.progress-text, .generating, .pending, [aria-busy="true"]').length === 0)()
    """, timeout=timeout, label="gradio idle")


def section_text(b: Browser, key: str) -> str:
    return b.js(f"""
      (() => {{
        const el = document.querySelector({SECTIONS[key]!r});
        return el ? (el.innerText || '') : '';
      }})()
    """) or ""


def click_visible_text(b: Browser, needle: str, *, exact: bool = False):
    """Click the first VISIBLE control whose caption matches, by real mouse.

    Visibility is the point. Gradio renders each tab caption three times:
    a zero-height measuring copy, a hidden overflow-menu copy, and -- only
    when the strip is wide enough -- the real one. A querySelector-and-
    .click() hits the measuring copy and silently does nothing, which is
    how this gate first "passed" its navigation step while sitting on
    Panoramica.
    """
    box = b.js(f"""
      (() => {{
        const wanted = {needle!r};
        const hit = [...document.querySelectorAll('button')].find(x => {{
          const txt = (x.innerText || '').trim();
          const ok = {'txt === wanted' if exact else 'txt.includes(wanted)'};
          if (!ok) return false;
          const r = x.getBoundingClientRect();
          return r.width > 4 && r.height > 4;
        }});
        if (!hit) return null;
        hit.scrollIntoView({{block:'center'}});
        const r = hit.getBoundingClientRect();
        return {{x: r.left + r.width/2, y: r.top + r.height/2, txt: (hit.innerText||'').trim()}};
      }})()
    """)
    if not box:
        raise RuntimeError(f"no visible control matching {needle!r}")
    for typ in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent",
               {"type": typ, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1})
    return box["txt"]


def open_impact_tab(b: Browser):
    """Navigate the way the product intends: the sidebar entry.

    Deliberately NOT the tab strip. The sidebar button is always visible,
    and clicking it exercises the nav_impact -> workspace_tabs chain that
    the Impact Center actually ships with.
    """
    # Checkpoint 4: the sidebar entry is the "Versioni e impatto" section,
    # which opens on this page.
    click_visible_text(b, "Versioni e impatto")
    b.wait_for("!!document.querySelector('#ms-ic-object')", timeout=40,
               label="impact tab rendered")
    wait_idle(b)


def picker_options(b: Browser) -> list:
    """The options currently mounted. Call open_picker() first."""
    return b.js("""
      [...document.querySelectorAll('#ms-ic-object li')]
        .map(x => (x.innerText||x.textContent||'').trim()).filter(Boolean)
    """) or []


def open_picker(b: Browser):
    """Open the dropdown with a real mouse click and wait for its list.

    Gradio's dropdown mounts its options only while open, so reading them
    straight after a synthetic focus finds an empty list -- which is how
    this gate first reported "the picker lists nothing" about a picker
    that was working.
    """
    box = b.js("""
      (() => {
        const i = document.querySelector('#ms-ic-object input');
        if (!i) return null;
        i.scrollIntoView({block:'center'});
        const r = i.getBoundingClientRect();
        return {x: r.left + r.width/2, y: r.top + r.height/2};
      })()
    """)
    if not box:
        raise RuntimeError("the impact picker is not on the page")
    for typ in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent",
               {"type": typ, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1})
    b.wait_for("document.querySelectorAll('#ms-ic-object li').length > 0",
               timeout=20, label="picker options mounted")


def pick_option_containing(b: Browser, needle: str) -> bool:
    """Select an option by real mouse event, not element.click().

    Gradio's list items respond to pointer events; a synthetic .click()
    highlights nothing and leaves the value unchanged, so the panels never
    refresh and the failure looks like a broken analysis.
    """
    box = b.js(f"""
      (() => {{
        const hit = [...document.querySelectorAll('#ms-ic-object li')]
          .find(x => (x.innerText||x.textContent||'').includes({needle!r}));
        if (!hit) return null;
        hit.scrollIntoView({{block:'center'}});
        const r = hit.getBoundingClientRect();
        return {{x: r.left + r.width/2, y: r.top + r.height/2}};
      }})()
    """)
    if not box:
        return False
    for typ in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent",
               {"type": typ, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1})
    return True


def screenshot(b: Browser, name: str):
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = b.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    import base64
    path = SHOT_DIR / f"{name}.png"
    path.write_bytes(base64.b64decode(data["data"]))
    print(f"    screenshot: {path.name}")
    return path


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------

def run_scenarios(b: Browser, url: str, project: Path, facts: dict):
    b.call("Page.enable")
    b.call("Page.navigate", {"url": url})
    b.wait_for("!!document.querySelector('#workspace-tabs')", timeout=60,
               label="app shell loaded")
    time.sleep(2.0)

    # --- 1. the screen is reachable and the picker is populated --------
    open_impact_tab(b)
    check("the Versioni e impatto section opens on Impact and renders its picker",
          bool(b.js("!!document.querySelector('#ms-ic-object')")))

    open_picker(b)
    options = picker_options(b)
    check("the picker lists recently changed content", len(options) > 0, options[:3])
    check("entries read as content, not as identifiers",
          all(not re.fullmatch(r"(BLK|TOP|TBL|FIG)-[-\w]+", o) for o in options), options[:3])

    wait_idle(b)
    empty_header = section_text(b, "header")
    check("before a selection the screen invites one, with no empty sections",
          empty_header.strip() != "" and section_text(b, "changes").strip() == "",
          empty_header[:80])
    check("the read-only promise is on screen",
          "non modifica nulla" in (b.js(
              "document.querySelector('.ms-ic-readonly')?.innerText || ''") or "").lower())

    before = db_fingerprint(project)

    # --- 2. selecting the changed block explains the impact ------------
    picked = pick_option_containing(b, "pressione massima")
    check("the edited block can be selected from the picker", picked)
    b.wait_for("(document.querySelector('#ms-ic-changes')?.innerText||'').length > 0",
               timeout=40, label="impact rendered")
    wait_idle(b)

    header = section_text(b, "header")
    changes = section_text(b, "changes")
    attention = section_text(b, "attention")
    ready = section_text(b, "ready")

    check("the header names the changed content",
          "pressione massima" in header.lower(), header[:120])
    check("...and shows the version it moved to",
          re.search(r"v\d+\s*→\s*v\d+", header) is not None, header[:120])
    check("...and attributes the change to whoever made it",
          ACTOR in header, header[:160])

    check("'Cosa cambia' names the manual", facts["publication_title"] in changes,
          changes[:200])
    check("...and the chapter", facts["topic_title"] in changes, changes[:200])

    check("'Cosa richiede attenzione' reports the now-stale translation",
          "tedesc" in attention.lower(), attention[:240])
    # The #ms-ic-ready slot holds TWO cards now: "Cosa e pronto" and the
    # release verdict. The verdict legitimately names the stale language
    # ("1 traduzione da aggiornare (tedesca)"), so the claim being tested
    # -- a stale language must not be listed as ready -- is about the
    # first card alone.
    ready_card = ready.split("Rilascio")[0]
    check("...and NOT listed under 'Cosa e pronto'",
          "tedesc" not in ready_card.lower(), ready_card[:200])
    check("...which says so plainly instead of sitting empty",
          "Niente da segnalare" in ready_card, ready_card[:200])
    check("...and the reopened requirement", "Avvertenze" in attention, attention[:240])
    check("...explaining why, in plain language",
          "versione precedente" in attention.lower(), attention[:240])

    check("the release verdict is printed and is NO",
          "Pronto al rilascio: NO" in ready, ready[:200])
    check("...under its own heading, NOT under 'Cosa e pronto'",
          re.search(RELEASE_BAND, ready) is not None, ready[-260:])
    check("...counting in agreement: '1 traduzione', not '1 traduzioni'",
          "1 traduzione da aggiornare" in ready, ready[-300:])
    check("...naming the language the way the rest of the screen does",
          "tedesca" in ready and "de-DE" not in ready, ready[-300:])
    check("...with its scope stated, not implied",
          "questa modifica" in ready.lower(), ready[:260])

    screenshot(b, "01_impatto_modifica")

    # --- 3. the vocabulary rule, in rendered text ----------------------
    for key in ("header", "changes", "attention", "ready"):
        flat = section_text(b, key).lower()
        for banned in BANNED:
            check(f"[{key}] the panel never says {banned!r}", banned not in flat,
                  flat[:120])
        check(f"[{key}] no catalogue key is showing",
              "impactcenter." not in flat and "translation.reason" not in flat, flat[:120])
        check(f"[{key}] no unsubstituted placeholder",
              re.search(r"\{[a-z_]+\}", flat) is None, flat[:120])

    # --- 4. technical detail exists, but only in the advanced section ---
    opened = b.js("""
      (() => {
        const el = [...document.querySelectorAll('.label-wrap, button, span')]
          .find(x => (x.innerText||'').trim().startsWith('Dettagli tecnici'));
        if (!el) return false;
        el.scrollIntoView({block:'center'});
        const r = el.getBoundingClientRect();
        el.click();
        return {w: r.width, h: r.height};
      })()
    """)
    check("the technical details section can be opened", bool(opened), opened)
    wait_idle(b)
    adv = section_text(b, "advanced")
    check("...and it is where the identifiers live",
          facts["block_id"] in adv, adv[:200])
    check("...and where the schema terms are allowed",
          "source_version" in adv, adv[:240])
    screenshot(b, "02_dettagli_tecnici")

    # --- 5. the translation analysis is opt-in -------------------------
    check("the translation analysis is off by default",
          b.js("document.querySelector('#ms-ic-deep input')?.checked === false"))
    b.click("#ms-ic-deep input")
    b.click("#ms-ic-analyse")
    b.wait_for("(document.querySelector('#ms-ic-attention')?.innerText||'').length > 0",
               timeout=90, label="translation analysis rendered")
    wait_idle(b, timeout=120)
    deep_attention = section_text(b, "attention")
    check("with it on, the attention section still renders",
          len(deep_attention.strip()) > 0, deep_attention[:120])
    # The subject block is Draft (the edit reset it), so the analysis
    # cannot run. Ticking the box must not look like a no-op.
    check("...and the screen says why the analysis could not run",
          "dopo l'approvazione del contenuto" in deep_attention, deep_attention[-300:])
    check("...without showing a technical error",
          "TIER2" not in deep_attention and "Traceback" not in deep_attention,
          deep_attention[-200:])
    for banned in BANNED:
        check(f"[translation analysis] still never says {banned!r}",
              banned not in deep_attention.lower(), deep_attention[:140])
    screenshot(b, "03_analisi_traduzioni")

    # --- 6. nothing was written ----------------------------------------
    after = db_fingerprint(project)
    check("browsing the screen changed no row and wrote no audit event",
          before == after,
          [(a, c) for a, c in zip(before[0], after[0]) if a != c])
    # (The workflow database is created by create_project, not by this
    # screen. That the screen never creates one is pinned in the Python
    # suite, which can delete it first; asserting it here would be vacuous.)


def main() -> int:
    print("=" * 78)
    print("BROWSER ACCEPTANCE — ERYLOS IMPACT CENTER V1")
    print("=" * 78)
    with fx.disposable_project("IMPACT_CENTER", seed=seed) as (project, facts):
        new_version = edit_the_block(project, facts["block_id"])
        print(f"subject block {facts['block_id']} edited -> v{new_version} "
              f"(translated at v{facts['translated_at_version']})")
        # Snapshot AFTER the seeding edit: the delta reported below is the
        # browser run's alone, and for a read-only screen it must be empty.
        overlay_before = overlay_snapshot(project)
        with fx.app_server(project) as url:
            b = Browser()
            try:
                run_scenarios(b, url, project, facts)
            finally:
                b.close()
        delta = report_delta(overlay_before, overlay_snapshot(project))
        check("the overlay delta for the whole browser run is empty",
              not any(delta.values()), {k: v for k, v in delta.items() if v})

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print(f"  {passed}/{total} passed")
    print(f"  screenshots: {SHOT_DIR}")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
