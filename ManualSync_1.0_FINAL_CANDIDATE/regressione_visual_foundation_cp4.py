"""Checkpoint 4 (Fase 1) -- ERYLOS visual foundation.

Presentation only, so what is checked is what a person sees, taken from
the real stylesheet, the real Gradio configuration and the real display
formatters, on an isolated data home. Where a check guards something whose
absence would be silent, a mutant input that reintroduces the defect must
be caught by the same checker.

  T  design tokens and WCAG 2.2 AA contrast, computed
  F  typography: Plus Jakarta Sans / JetBrains Mono, 12px floor, 400-600
  B  brand: ERYLOS everywhere, no ManualSync / provisional brand / "M" logo
  S  sidebar: seven entries in words, one SVG icon family, no "Aiuto"
  H  header: brand, project, manual, operator, language -- in that order
  E  no emoji or character glyphs used as icons on the rebuilt surfaces
  C  component rules: buttons, uploads, tables, run-time noise
  D  table values in words; codes stay in the data and reports
  L  navigation behaviour unchanged by the restructuring
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import os
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ms_visual_cp4_home_"))
os.environ["HOME"] = str(_TEST_HOME)
os.environ["USERPROFILE"] = str(_TEST_HOME)

import pandas as pd  # noqa: E402

import app  # noqa: E402
import erylos_theme as theme  # noqa: E402
import i18n  # noqa: E402
import impact_center_view as icv  # noqa: E402
import translation_intelligence as ti  # noqa: E402
import translation_view as tv  # noqa: E402

results: list[dict] = []
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-⛿✀-➿⭐⭕←-⇿⌀-⏿■-◿]")


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)[:400]})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def cfg():
    return app.demo.get_config_file()


def comp_by_id(config):
    return {c["id"]: c for c in config["components"]}


def children(config, block):
    """Direct children ids of a block in the layout tree."""
    def find(node):
        if node.get("id") == block._id:
            return node
        for ch in node.get("children", []) or []:
            hit = find(ch)
            if hit:
                return hit
        return None
    node = find(config["layout"])
    return [c["id"] for c in (node or {}).get("children", []) or []]


def descendants(config, block):
    """Every id under a block, in reading order (Gradio wraps form fields in a 'form' node)."""
    out = []
    def walk(node):
        for ch in node.get("children", []) or []:
            out.append(ch["id"])
            walk(ch)
    def find(node):
        if node.get("id") == block._id:
            return node
        for ch in node.get("children", []) or []:
            hit = find(ch)
            if hit:
                return hit
        return None
    walk(find(config["layout"]) or {})
    return out


def glyph_labels(labels):
    """Entries whose label carries anything but words (letters, spaces, &, ')."""
    return [x for x in labels if not re.fullmatch(r"[^\W\d_]+(?:[ '&][^\W\d_]*)*", x.replace(" & ", " "))]


def font_size_zero_selectors(css):
    out = []
    for m in re.finditer(r"font-size:0(?![.\d])", css):
        start = css.rfind("{", 0, m.start())
        sel_start = max(css.rfind("}", 0, start), css.rfind("\n", 0, start))
        out.append(css[sel_start + 1:start].strip())
    return out


try:
    CSS = app.CSS
    T = theme.TOKENS

    # ============================================================ T: tokens, contrast
    required = {"primary": "#6A48F2", "graphite": "#15161C", "text": "#111318", "text-secondary": "#555B6E",
                "background": "#F7F8FA", "surface": "#FFFFFF", "accent": "#00807A", "brand": "#7C5CFF"}
    names = ["primary", "primary-hover", "primary-active", "primary-soft", "text", "text-secondary", "text-muted",
             "background", "surface", "border", "success", "warning", "error", "info", "focus"]
    check("T1: the token system names every role and keeps the approved base palette",
          all(n in T for n in names) and all(T[k].upper() == v for k, v in required.items()),
          {k: T.get(k) for k in required})
    low = [(fg, bg, round(theme.contrast(fg, bg), 2), need, what) for fg, bg, need, what in theme.CONTRAST_REQUIREMENTS
           if theme.contrast(fg, bg) < need]
    worst = min(theme.contrast(fg, bg) / need for fg, bg, need, _ in theme.CONTRAST_REQUIREMENTS)
    check(f"T2: all {len(theme.CONTRAST_REQUIREMENTS)} text / control pairs meet WCAG AA (4.5:1 text, 3:1 boundaries)",
          not low, low or f"worst margin x{worst:.2f}")
    check("T3 (mutant): the checker is a real WCAG computation -- 21:1, #777 on white 4.48, and the rejected "
          "first input border (#8B90A1 on the background, 2.99) all come out right",
          abs(theme.contrast("#000000", "#FFFFFF") - 21) < 0.01 and 4.47 < theme.contrast("#777777", "#FFFFFF") < 4.49
          and theme.contrast("#8B90A1", "background") < 3.0, theme.contrast("#8B90A1", "background"))
    loose = [CSS[max(0, m.start() - 30):m.start()] for m in re.finditer("#6A48F2", CSS)
             if not re.search(r"--[\w-]+:\s*$", CSS[max(0, m.start() - 30):m.start()])]
    info_rules = [r for r in re.findall(r"[^{}]*ms-ux-info[^{}]*\{[^}]*\}", CSS) if re.search("#(6A48F2|5634E0|7C5CFF)", r)]
    check("T4: the violet stays an accent -- no gradients, the primary colour only through tokens, and an "
          "information notice is blue, never the brand violet",
          "gradient(" not in CSS and not loose and not info_rules, (loose, info_rules[:2]))

    # ============================================================ F: typography
    check("F1: Plus Jakarta Sans for the interface, JetBrains Mono for codes -- loaded first, with a system fallback",
          CSS.startswith("@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono")
          and "Plus+Jakarta+Sans:wght@400;500;600" in CSS
          and f"--font:{theme.FONT_SANS}!important" in CSS and f"--font-mono:{theme.FONT_MONO}!important" in CSS
          and "'Segoe UI'" in theme.FONT_SANS, CSS[:160])
    sizes = [float(x) for x in re.findall(r"font-size:(\d+(?:\.\d+)?)px", CSS)]
    zero_ok = ("aria-dropeffect", "#ms-ui-lang", ".label-wrap .icon")
    zeros = [s_ for s_ in font_size_zero_selectors(CSS) if not any(k in s_ for k in zero_ok)]
    check("F2: no text under 12px -- the only font-size:0 are words replaced by the stylesheet's own "
          "(upload zone, IT/EN switch, accordion chevron)",
          min(sizes) >= 12 and not zeros, (min(sizes), zeros[:3]))
    weights = set(re.findall(r"font-weight:\s*([\w.]+)", CSS))
    check("F3: three weights only -- 400, 500, 600", weights <= {"400", "500", "600"}, sorted(weights))
    scale_ok = all(f"font-size:{s}px!important;line-height:{lh}px!important" in CSS
                   for s, lh in ((24, 32), (18, 26), (15, 22)))
    check("F4: the type scale is the approved one (H1 24/32, H2 18/26, H3 15/22, body 14/21, secondary 13/19, caption 12/16)",
          scale_ok and theme.TYPE_SCALE["body"][:2] == (14, 21) and theme.TYPE_SCALE["secondary"][:2] == (13, 19)
          and theme.TYPE_SCALE["caption"][:2] == (12, 16), theme.TYPE_SCALE)
    check("F5: a table reads in the interface font; monospace is kept for code and technical values only",
          ".table-wrap td span" in CSS and ".ms-code{font-family:var(--font-mono)" in CSS
          and "font-family:var(--font)!important" in CSS, "")

    # ============================================================ B: brand
    config = cfg()
    blob = json.dumps(config, ensure_ascii=False, default=lambda o: "")
    cats = {lg: json.loads((BASE / "locales" / f"{lg}.json").read_text(encoding="utf-8")) for lg in ("it", "en")}
    banned = [w for w in ("ManualSync", "Brand provvisorio", "provvisorio durante il rebrand", "Brand provisional")
              if w in blob or any(w in v for c in cats.values() for v in c.values())]
    check("B1: the interface says ERYLOS -- window title, brand constant, header; never ManualSync or a provisional brand",
          app.BRAND_NAME == "ERYLOS" and config["title"] == "ERYLOS" and not banned, banned)
    check("B2: no 'M' / 'MS' logo tile left anywhere (markup or stylesheet)",
          "ms-logo" not in CSS and "ms-logo" not in blob and "ms-brand-title" not in blob
          and not re.search(r">\s*MS?\s*</div>", blob), "")
    brand = BASE / "brand"
    variants = ["erylos-symbol.svg", "erylos-symbol-dark.svg", "erylos-symbol-mono.svg", "erylos-wordmark.svg",
                "erylos-horizontal.svg", "erylos-horizontal-dark.svg", "erylos-app-icon.svg"]
    geo = ["6,6 24,6 22,10.5 4,10.5", "10,14 28,14 26,18.5 8,18.5", "6,22 24,22 22,26.5 4,26.5"]
    bad = [v for v in variants if not (brand / v).exists()
           or ("symbol" in v or "horizontal" in v or "icon" in v) and not all(g in (brand / v).read_text(encoding="utf-8") for g in geo)]
    check("B3: the ERYLOS 'Strati' system ships every variant (symbol light/dark/mono, wordmark, horizontal "
          "light/dark, app icon) on one geometry", not bad, bad)
    png = (BASE / "manualsync.png").read_bytes()
    ico = (BASE / "manualsync.ico").read_bytes()
    w, h = struct.unpack(">II", png[16:24])
    n = struct.unpack("<H", ico[4:6])[0]
    sizes_ico = sorted({(ico[6 + 16 * i] or 256) for i in range(n)})
    check("B4: window/taskbar icons are the ERYLOS tile (PNG 256px; ICO 16-256 for the small sizes)",
          png[:8] == b"\x89PNG\r\n\x1a\n" and (w, h) == (256, 256) and {16, 32, 48, 256} <= set(sizes_ico)
          and "#15161C" in (brand / "erylos-app-icon.svg").read_text(encoding="utf-8"), ((w, h), sizes_ico))

    # ============================================================ S: sidebar
    side = children(config, app.nav_home.parent) if hasattr(app.nav_home, "parent") and app.nav_home.parent else []
    entries = [app.nav_home, app.nav_authoring, app.nav_impact, app.nav_translation, app.nav_compliance,
               app.nav_release, app.nav_settings]
    ids = [e.elem_id for e in entries]
    labels = {}
    for lg in ("it", "en"):
        with i18n.use_language(lg):
            labels[lg] = [app.t(k) for k in ("nav.overview", "nav.content", "nav.impact", "nav.translation",
                                                "nav.conformity", "nav.release", "nav.settings")]
    check("S1: seven entries, in the approved order and words (IT and EN)",
          labels["it"] == ["Panoramica", "Contenuti", "Versioni e impatto", "Traduzioni", "Conformità",
                           "Pubblicazione", "Impostazioni"]
          and len(labels["en"]) == 7 and all(labels["en"])
          and [c for c in side if c in {e._id for e in entries}] == [e._id for e in entries], (labels, side))
    src = (BASE / "app.py").read_text(encoding="utf-8")
    sidebar_src = src[src.index('with gr.Sidebar(width=224, elem_id="ms-sidebar"):'):src.index("_initial_projects, _initial_key")]
    check("S2: the entries are words only -- no ⌂ ◫ ✎ ◈ characters as icons -- and there is no 'Aiuto' entry",
          not glyph_labels(labels["it"] + labels["en"]) and "nav.help" not in sidebar_src
          and not EMOJI.search(sidebar_src), glyph_labels(labels["it"] + labels["en"]))
    check("S2b (mutant): a label that still carried a glyph would be caught",
          glyph_labels(["⌂   Panoramica", "Versioni e impatto", "Jobs & vendors"]) == ["⌂   Panoramica"], "")
    masks = [eid for eid in ids if f"#ms-sidebar #{eid}::before{{-webkit-mask-image:url(data:image/svg+xml;base64," not in CSS]
    check("S3: every entry draws its icon from ONE SVG family (24-grid, 1.8 stroke), in the entry's own colour",
          ids == list(theme.NAV_ICONS) and not masks
          and "background:currentColor" in CSS and all(theme.ICON_PATHS[v] for v in theme.NAV_ICONS.values()), masks)
    check("S4: the group titles are Lavoro / Sistema, and the active entry is violet-marked on graphite",
          cats["it"]["kicker.workflow"] == "Lavoro" and cats["it"]["kicker.system"].lower() == "sistema"
          and "#ms-sidebar button.primary{background:var(--e-graphite-active)" in CSS
          and "inset 3px 0 0 var(--e-brand-bright)" in CSS, cats["it"]["kicker.system"])

    # ============================================================ H: header
    comps = comp_by_id(config)
    bar = [c for c in config["components"] if "ms-ctxbar" in (c.get("props", {}).get("elem_classes") or [])]
    order = descendants(config, type("B", (), {"_id": bar[0]["id"]})) if bar else []
    first = comps.get(order[0], {}) if order else {}
    expect = [app.ws_project._id, app.cp_pub._id, app.ws_operator._id, app.ws_lang._id]
    check("H1: the header opens with the ERYLOS mark, then Progetto, Manuale, Operatore, Lingua",
          first.get("props", {}).get("elem_id") == "ms-brand-head" and "ERYLOS" in str(first.get("props", {}).get("value"))
          and [c for c in order if c in expect] == expect, [comps.get(c, {}).get("props", {}).get("elem_id") for c in order])
    with i18n.use_language("it"):
        pub_label = app.t("context.publication")
    check("H2: one bar of 56px, sticky, with explanations as tooltips (not paragraphs) and an IT | EN switch",
          "min-height:56px!important;height:56px" in CSS and "position:sticky!important" in CSS
          and '[data-testid="block-info"].has-info+div{display:none!important' in CSS
          and 'content:"IT"' in CSS and 'content:"EN"' in CSS and pub_label == "Manuale", pub_label)
    check("H3: Gradio's run-time readouts (processing | 0.4s, queue, ETA) are not shown",
          '[data-testid="status-tracker"] .progress-text' in CSS and "[data-testid=\"status-tracker\"] .eta-bar" in CSS, "")

    # ============================================================ E: glyph icons
    quick = [s_ for s_ in re.findall(r"<div class='ms-quick-icon'>(.*?)</div>", src)]
    check("E1: the four quick actions use the SVG family, not ↺ 文 ☑ ✓",
          len(quick) == 4 and all("theme.icon_svg(" in q for q in quick), quick)
    ready = app._compliance_detail_placeholder()[0]
    check("E2: the empty Conformità detail and the release verdict carry an SVG mark and the words",
          "<svg" in ready and not EMOJI.search(ready)
          and "{ux.icon(ux.SUCCESS)}RELEASE PRONTA" in src and "{ux.icon(ux.BLOCKED)}RELEASE BLOCCATA" in src
          and "✅ RELEASE PRONTA" not in src, "")
    marks = list(tv._STATUS_DOT.values()) + list(icv._STATUS_ICON.values())
    check("E3: translation and impact status marks are SVG in the status colour -- no 🟢 🔴 ⚪ ✅ ⚠️ ❌",
          all(m.startswith("<svg") for m in marks) and not any(EMOJI.search(m) for m in marks)
          and set(tv._STATUS_DOT) == {ti.STATUS_FRESH, ti.STATUS_STALE, ti.STATUS_MISSING}, "")
    check("E4: the rebuilt surfaces carry no emoji at all (whole initial interface)", not EMOJI.search(
        "".join(str(c.get("props", {}).get("value") or "") + str(c.get("props", {}).get("label") or "")
                for c in config["components"] if c.get("type") in ("html", "button", "markdown"))), "")

    # ============================================================ C: components
    check("C1: buttons 36px (32 compact, 40 page primary), radius 8, at least 88px, never page-wide",
          "min-height:36px!important" in CSS and "button.sm{min-height:32px!important" in CSS
          and "button.primary.lg{min-height:40px!important" in CSS and "min-width:88px!important" in CSS
          and "#workspace-tabs .column>button.primary" in CSS and "width:auto!important" in CSS, "")
    danger = [c["props"].get("value") for c in config["components"]
              if "ms-danger" in (c.get("props", {}).get("elem_classes") or [])]
    check("C2: destructive actions look destructive and keep their distance (Archivia, Scarta ...)",
          {"Archivia", "Scarta modifiche", "Scarta bozza"} <= set(danger)
          and "button.ms-danger{background:var(--e-surface)!important;border-color:var(--e-error-line)" in CSS, danger)
    unscoped = re.findall(r"button\[aria-dropeffect\](?!\.boundedheight)", theme.FOUNDATION_CSS)
    check("C3: the upload zone is compact and in the product's words, IT and EN -- and ONLY the upload zone "
          "(a dataframe is also a drop target and must keep its text)",
          not unscoped and 'content:"Trascina qui il file oppure fai clic per sceglierlo"' in CSS
          and 'html:lang(en) .gradio-container button[aria-dropeffect].boundedheight .wrap::after' in CSS
          and "document.documentElement.lang = " in app._SHELL_PERSIST_JS, unscoped)
    check("C3b (mutant): the unscoped selector that shrank the Conformità table is recognised",
          re.findall(r"button\[aria-dropeffect\](?!\.boundedheight)", ".x button[aria-dropeffect]{height:84px}"), "")
    check("C4: tables -- sticky header, 40px rows, horizontal scroll inside the table, never the page",
          ".table-wrap thead th{position:sticky!important" in CSS and "min-height:40px" in CSS
          and ".gradio-container .table-container,.gradio-container .table-wrap{max-width:100%!important;overflow-x:auto!important}" in CSS, "")
    check("C5: states -- visible focus, legible disabled, hover, selected",
          ":focus-visible" in CSS and "outline:2px solid var(--e-focus)" in CSS
          and "color:var(--e-disabled-text)!important" in CSS and "opacity:1!important" in CSS
          and "#ms-ui-lang label.selected{background:var(--e-primary-soft)" in CSS, "")
    check("C6: the pre-existing guarantees still hold (hidden tab strip, overflow menu, sticky panes, grid font)",
          '[role="tablist"]' in CSS and "#workspace-tabs .overflow-menu" in CSS and "visibility:hidden!important" in CSS
          and ".gradio-container:has(.ms-authoring-workspace){overflow:visible!important}" in CSS
          and ".ms-cp-grid .body-cell,.ms-cp-grid .body-cell span" in CSS, "")

    # ============================================================ D: table values
    ev = app._friendly_events(pd.DataFrame([
        {"job_id": "J", "event_type": "STATUS_CHANGED", "from_status": "SENT", "to_status": "QA_BLOCKED",
         "actor": "ManualSync user", "created_at": "2026-09-21T10:11:12.5+00:00", "detail_json": "{}"},
        {"job_id": "J", "event_type": "NEW_EVENT_TYPE", "from_status": "", "to_status": "READY_TO_SEND",
         "actor": "Anna", "created_at": "2026-09-21T09:00:00", "detail_json": "{}"}]))
    rows = ev.to_dict(orient="records")
    check("D1: job history in words -- no STATUS_CHANGED / QA_BLOCKED, minutes not microseconds, ERYLOS not ManualSync",
          rows[0]["Evento"] == "Cambio di stato" and "QA bloccato" in rows[0]["A"] and rows[0]["Da"] == "Inviato"
          and rows[0]["Operatore"] == "Utente ERYLOS" and rows[0]["Data"] == "2026-09-21 10:11"
          and "detail_json" not in ev.columns, rows[0])
    check("D1b (mutant): a code nobody translated is shown as it is -- never blanked out",
          rows[1]["Evento"] == "NEW_EVENT_TYPE" and rows[1]["Operatore"] == "Anna" and rows[1]["Da"] == "", rows[1])
    jobs = app._friendly_jobs(pd.DataFrame([
        {"job_id": "J1", "status_label": "QA bloccato", "target_lang": "de-DE", "vendor": "V", "due_date": "2026-01-01",
         "overdue": "SI", "work_words": 3, "technical_review_segments": 1, "qa_status": "BLOCKED"},
        {"job_id": "J2", "status_label": "Inviato", "target_lang": "fr-FR", "vendor": "V", "due_date": "",
         "overdue": "", "work_words": 5, "technical_review_segments": 0, "qa_status": ""}]))
    check("D2: jobs -- QA and overdue in words, every row kept",
          list(jobs["QA"]) == ["× Bloccato", ""] and list(jobs["Scaduto"]) == ["Sì", ""] and len(jobs) == 2,
          jobs.to_dict(orient="records"))
    mx = app._friendly_readiness_matrix(pd.DataFrame([{"language": "it-IT", "role": "TARGET", "status": "READY"},
                                                      {"language": "en-US", "role": "SOURCE", "status": "BLOCKED"}]))
    bl_frame = pd.DataFrame([{"language": "de-DE", "topic_id": "T-9", "block_id": "B-9", "block_type": "topic_title",
                              "object_ref_id": "T-9", "translation_status": "", "content_preview": "Installation"},
                             {"language": "de-DE", "topic_id": "T-9", "block_id": "B-10", "block_type": "paragraph",
                              "object_ref_id": "", "translation_status": "LINGUISTIC_QA_FAIL", "content_preview": "x"}])
    with i18n.use_language("it"):
        bl = app._friendly_blockers(bl_frame)
    with i18n.use_language("en"):
        bl_en = app._friendly_blockers(bl_frame)
    flat = json.dumps(bl.to_dict(orient="records"), ensure_ascii=False)
    check("D3: release readiness in words; the blockers say what is missing, identifiers stay in the report",
          list(mx["Ruolo"]) == ["Destinazione", "Sorgente"] and list(mx["Stato"]) == ["✓ Pronta", "× Bloccata"]
          and "T-9" not in flat and "B-9" not in flat and len(bl) == 2
          and list(bl.iloc[:, 3]) == ["× Traduzione mancante", "× Anomalia linguistica"]
          and list(bl_en.columns) == ["Language", "Content", "Type", "What is missing"], flat)
    new_keys = [k for k in cats["it"] if k.startswith("table.")]
    check("D4: every new table word exists in Italian and English",
          len(new_keys) >= 28 and all(k in cats["en"] and cats["en"][k] for k in new_keys), len(new_keys))

    # ============================================================ L: behaviour unchanged
    def selected(u):
        return u.get("selected") if isinstance(u, dict) else getattr(u, "selected", None)
    pages = ["home", "authoring", "impact", "revision", "translations", "jobs", "compliance", "release", "settings"]
    wrong = {p: selected(app._navigate(p)[0]) for p in pages if selected(app._navigate(p)[0]) != p}
    lit = {p: [i for i, u in zip(app._NAV_IDS, app._navigate(p)[1:]) if u.get("variant") == "primary"] for p in pages}
    check("L1: navigation still opens exactly the page asked for, and exactly one entry is lit on every page",
          not wrong and all(len(v) == 1 for v in lit.values())
          and lit["revision"] == ["impact"] and lit["jobs"] == ["translations"], (wrong, lit))
except BaseException as _crash:
    if not isinstance(_crash, AssertionError):
        results.append({"test": "suite interrupted by an unexpected error", "status": "FAIL",
                        "detail": f"{type(_crash).__name__}: {_crash}"[:400]})
    raise
finally:
    summary = {"suite": "Checkpoint 4 -- ERYLOS visual foundation", "tests": len(results),
               "pass": sum(r["status"] == "PASS" for r in results),
               "fail": sum(r["status"] == "FAIL" for r in results), "results": results}
    (BASE / "RISULTATO_VISUAL_FOUNDATION_CP4.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
    for r in results:
        print(("PASS" if r["status"] == "PASS" else "FAIL"), "|", r["test"], "" if r["status"] == "PASS" else "| " + r["detail"])
    print(json.dumps({k: summary[k] for k in ("suite", "tests", "pass", "fail")}, ensure_ascii=False))
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
