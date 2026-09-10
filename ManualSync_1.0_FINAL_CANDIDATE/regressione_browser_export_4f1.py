"""4F.1 BROWSER / E2E GATE -- export UX acceptance in real Chrome.

*** NOT part of the lightweight Python suite. *** Run at a 4F.1
checkpoint. Drives ONE instance of the already-installed system Chrome
against ONE already-running app server, one scenario at a time.

Why this exists
---------------
4F.1's first cut passed every server-side test and still failed manual
acceptance: the export returned a success banner plus a raw Gradio File
card, so the user had to work out that a filename widget was how you
open a manual, and the technical export path was printed in standard
mode. Those are UX facts that only a browser can confirm -- what a user
can SEE and CLICK -- which is exactly what this gate checks.

Reuses the CDP client from regressione_browser_save_4e3.py (stdlib-only,
no selenium/playwright, no browser download).

Prerequisites: app running on http://127.0.0.1:7860 with the
"4E3 Rich Text Manual Acceptance" project active.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_browser_export_4f1.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from regressione_browser_save_4e3 import Browser, APP_URL  # noqa: E402

import authoring_view as av  # noqa: E402

PROJECT = Path.home() / "ManualSyncData" / "Projects" / "4E3_Rich_Text_Manual_Acceptance"
PROFILE_LABEL = "EU Service"

results: list[dict] = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if not ok else ""))
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def _db_versions():
    repo, svc = av.open_service(str(PROJECT))
    try:
        return {row["block_id"]: row["version"] for row in
                repo.conn.execute("SELECT block_id, version FROM content_blocks")}
    finally:
        repo.close()


def _fetch(br, url: str) -> dict:
    """Fetches a URL from INSIDE the page (same origin, same session) and
    reports what the browser actually receives -- status, content type,
    disposition and the first bytes. This is how 'does Apri PDF really
    open the PDF?' is proven without needing to inspect a second tab."""
    js = f"""
      (async () => {{
        const r = await fetch({url!r});
        const buf = new Uint8Array(await r.arrayBuffer());
        let head = '';
        for (let i = 0; i < Math.min(8, buf.length); i++) head += String.fromCharCode(buf[i]);
        return JSON.stringify({{
          status: r.status,
          type: r.headers.get('content-type') || '',
          disposition: r.headers.get('content-disposition') || '',
          bytes: buf.length,
          head: head
        }});
      }})()
    """
    res = br.call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
    return json.loads(res["result"]["value"])


def _click_by_text(br, selector: str, text: str, *, contains: bool = False) -> bool:
    """Real mouse click on the element matching `text`.

    Deliberately a genuine Input.dispatchMouseEvent (via Browser.click)
    rather than a JS .click(): Gradio's Svelte components listen for real
    pointer events and simply IGNORE a synthetic click -- the dropdown
    stays aria-expanded="false", and a synthetic click on a sidebar nav
    button never fires its Python callback at all. Both were real
    false-negatives while building this gate."""
    match = ("e.textContent.includes(" + repr(text) + ")" if contains
             else "e.textContent.trim() === " + repr(text))
    found = br.js(f"""
      (() => {{
        document.querySelectorAll('[data-ms-e2e]').forEach(e => {{
          e.removeAttribute('id'); e.removeAttribute('data-ms-e2e'); }});
        const el = Array.from(document.querySelectorAll({selector!r}))
          .find(e => {match});
        if (!el) return false;
        el.id = 'ms-e2e-target'; el.setAttribute('data-ms-e2e', '1');
        return true;
      }})()
    """)
    if not found:
        return False
    br.click("#ms-e2e-target")
    return True


def _card_links(br) -> dict:
    return json.loads(br.js("""
      JSON.stringify(Array.from(document.querySelectorAll('.ms-export-card a.ms-export-btn'))
        .reduce((acc, a) => { acc[a.textContent.trim()] = {href: a.getAttribute('href'),
                                                           target: a.getAttribute('target') || '',
                                                           download: a.hasAttribute('download')}; return acc; }, {}))
    """))


def main():
    versions_before = _db_versions()
    br = Browser()
    try:
        br.call("Page.enable")
        br.call("Runtime.enable")
        br.call("Page.navigate", {"url": APP_URL})
        br.wait_for("document.querySelectorAll('button').length > 3", timeout=60, label="app mounted")
        time.sleep(2)
        # Real mouse click: a synthetic one does not fire the sidebar's
        # Python callback, so the tab (and the profile picker it
        # populates) would silently never initialize.
        check("e2e_nav (setup): the Contenuti tab can be opened from the sidebar",
              _click_by_text(br, "button", "Contenuti", contains=True), None)
        br.wait_for("!!document.querySelector('input[aria-label=\"Profilo\"]')",
                    timeout=45, label="Contenuti loaded")
        time.sleep(3)

        # ---- select the EU Service profile in the preview's own picker ----
        # The preview's profile picker is a Gradio Dropdown: a text input
        # labelled "Profilo" whose options only exist in the DOM once it
        # is opened, so this is a real two-step user interaction.
        # The choices arrive from a CHAINED callback after navigation, so
        # the dropdown can legitimately be open-but-empty for a moment;
        # retry rather than racing it.
        picked, opened = False, None
        for _attempt in range(8):
            br.click('input[aria-label="Profilo"]')
            time.sleep(1.2)
            opened = br.js("""(document.querySelector('input[aria-label="Profilo"]')||{})
                              .getAttribute('aria-expanded')""")
            option_sel = f'li[aria-label="{PROFILE_LABEL}"]'
            if br.js(f"!!document.querySelector({json.dumps(option_sel)})"):
                br.click(option_sel)
                picked = True
                break
            time.sleep(1.5)
        time.sleep(3)
        check("e2e0 (setup): the EU Service profile can be selected in the preview picker",
              picked, (opened, picked))

        # ================= PDF =================
        _click_by_text(br, "button", "Esporta PDF")
        br.wait_for("!!document.querySelector('.ms-export-card')", timeout=90, label="PDF result card")
        time.sleep(1)
        card_text = br.js("(document.querySelector('.ms-export-card')||{}).innerText")
        links = _card_links(br)

        check("e2e1 (PDF, item 3): a real result CARD appears with the success headline and a human "
              "subtitle -- not a bare banner", "PDF creato correttamente" in card_text
              and "Manuale Pompa HX-400" in card_text and "EU Service" in card_text, card_text)
        check("e2e2 (PDF, item 4): an 'Apri PDF' action is visibly present as the primary button",
              "Apri PDF" in links and br.js(
                  "!!document.querySelector('.ms-export-card a.ms-export-btn-primary')"), list(links))
        check("e2e3 (PDF, item 6): a 'Scarica PDF' action is present as the secondary button",
              "Scarica PDF" in links and links["Scarica PDF"]["download"], list(links))
        check("e2e4 (PDF, item 7): no technical path is visible in standard mode -- the export path is "
              "only inside the collapsed 'Dettagli tecnici' disclosure, and no absolute path is shown",
              "Exports" not in card_text.split("Dettagli tecnici")[0]
              and not any(t in card_text for t in ["C:\\", "C:/", "file://", str(Path.home())]),
              card_text)
        check("e2e5 (PDF): the open action opens in a NEW TAB and points at an opaque route, never a "
              "filesystem path", links["Apri PDF"]["target"] == "_blank"
              and links["Apri PDF"]["href"].startswith("/ms-export/")
              and "Users" not in links["Apri PDF"]["href"], links["Apri PDF"])

        opened_pdf = _fetch(br, links["Apri PDF"]["href"])
        check("e2e6 (PDF, item 5): clicking 'Apri PDF' really serves the generated PDF for reading -- "
              "200, application/pdf, real %PDF bytes, rendered inline (no attachment disposition)",
              opened_pdf["status"] == 200 and "application/pdf" in opened_pdf["type"]
              and opened_pdf["head"].startswith("%PDF") and "attachment" not in opened_pdf["disposition"],
              opened_pdf)
        downloaded_pdf = _fetch(br, links["Scarica PDF"]["href"])
        check("e2e7 (PDF, item 6): 'Scarica PDF' returns the same generated PDF as a download",
              downloaded_pdf["status"] == 200 and downloaded_pdf["head"].startswith("%PDF")
              and "attachment" in downloaded_pdf["disposition"]
              and ".pdf" in downloaded_pdf["disposition"], downloaded_pdf)

        pdf_href_first = links["Apri PDF"]["href"]

        # ================= HTML =================
        _click_by_text(br, "button", "Esporta HTML")
        br.wait_for("(document.querySelector('.ms-export-card')||{}).innerText.includes('HTML')",
                    timeout=90, label="HTML result card")
        time.sleep(1)
        card_html_text = br.js("(document.querySelector('.ms-export-card')||{}).innerText")
        html_links = _card_links(br)

        check("e2e8 (HTML, item 2): a result card appears with the HTML success headline and subtitle",
              "Pacchetto HTML creato correttamente" in card_html_text
              and "Manuale Pompa HX-400" in card_html_text and "EU Service" in card_html_text,
              card_html_text)
        check("e2e9 (HTML, item 3): an 'Apri HTML' primary action is present",
              "Apri HTML" in html_links, list(html_links))
        check("e2e10 (HTML, item 5): a 'Scarica pacchetto HTML' action is present",
              "Scarica pacchetto HTML" in html_links, list(html_links))
        check("e2e11 (HTML, item 6): no technical path visible in standard mode",
              "Exports" not in card_html_text.split("Dettagli tecnici")[0]
              and not any(t in card_html_text for t in ["C:\\", "C:/", "file://", str(Path.home())]),
              card_html_text)

        opened_html = _fetch(br, html_links["Apri HTML"]["href"])
        check("e2e12 (HTML, item 4): clicking 'Apri HTML' really serves the generated index.html",
              opened_html["status"] == 200 and "text/html" in opened_html["type"]
              and opened_html["bytes"] > 200, opened_html)
        css_href = html_links["Apri HTML"]["href"].rsplit("/", 1)[0] + "/style.css"
        opened_css = _fetch(br, css_href)
        check("e2e13 (HTML): the package's RELATIVE assets resolve through the same opaque route, so "
              "the opened page is actually styled rather than raw text",
              opened_css["status"] == 200 and "text/css" in opened_css["type"], opened_css)
        downloaded_zip = _fetch(br, html_links["Scarica pacchetto HTML"]["href"])
        check("e2e14 (HTML, item 5): 'Scarica pacchetto HTML' returns a real ZIP as a download",
              downloaded_zip["status"] == 200 and downloaded_zip["head"].startswith("PK")
              and "attachment" in downloaded_zip["disposition"]
              and ".zip" in downloaded_zip["disposition"], downloaded_zip)

        # ============ re-export: no stale actions, old artifact kept ============
        _click_by_text(br, "button", "Esporta PDF")
        br.wait_for("(document.querySelector('.ms-export-card')||{}).innerText.includes('PDF creato')",
                    timeout=90, label="second PDF card")
        time.sleep(1)
        links2 = _card_links(br)
        check("e2e15 (re-export): the card's actions now point at the NEWEST export, never a stale one",
              links2["Apri PDF"]["href"] != pdf_href_first, (pdf_href_first, links2["Apri PDF"]["href"]))
        still_old = _fetch(br, pdf_href_first)
        check("e2e16 (re-export): the PREVIOUS artifact is still intact on disk -- a re-export never "
              "overwrote it", still_old["status"] == 200 and still_old["head"].startswith("%PDF"),
              still_old)
        newest = _fetch(br, links2["Apri PDF"]["href"])
        check("e2e17 (re-export): the newest action serves a valid PDF too",
              newest["status"] == 200 and newest["head"].startswith("%PDF"), newest)

        # ============ security ============
        traversal = _fetch(br, links2["Apri PDF"]["href"].rsplit("/f/", 1)[0] + "/f/../../../secret.txt")
        check("e2e18 (security): traversal through the opaque route is refused",
              traversal["status"] in (403, 404), traversal)
        unknown = _fetch(br, "/ms-export/definitely-not-a-token/f/index.html")
        check("e2e19 (security): an unknown token serves nothing", unknown["status"] == 404, unknown)
        check("e2e20 (security): no absolute path or username appears anywhere in the visible page",
              not any(t in br.js("document.body.innerText")
                      for t in ["C:\\Users", "C:/Users", str(Path.home())]), None)
    finally:
        br.close()

    check("e2e21 (item 17): exporting through the UI mutated no content version",
          _db_versions() == versions_before, None)

    print("\n" + json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                             "tests": len(results)}))


if __name__ == "__main__":
    main()
