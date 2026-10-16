"""BROWSER / E2E GATE -- the interface really paints with the fonts we ship.

*** NOT part of the lightweight Python suite. *** It owns a disposable ZZ_
project, its own app server and one headless Chrome, and it loads the
product with fonts.googleapis.com and fonts.gstatic.com BLOCKED and the
cache disabled: the machine of a customer without internet.

regressione_font_locali.py proves the files are shipped and declared. Only a
browser can prove the rest, and only these measurements can tell a loaded
font from a fallback that looks similar in a screenshot:

  * the five faces are loaded, from the data the page itself carries;
  * a heading measures exactly as Plus Jakarta Sans and NOT as Segoe UI --
    the fallback the interface used until today when offline;
  * a technical value measures as JetBrains Mono and not as Consolas;
  * nothing was requested from a font server, and the page raised no error;
  * (CP2.5-A) the page requested NOTHING outside the app at all. Every request
    the page made is read from the DevTools Network domain of the page
    itself, so Chrome's own background traffic -- which is not ERYLOS -- is
    not counted; a deliberate external probe proves the check can fail.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_browser_fonts.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import base64
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import acceptance_fixture as fx  # noqa: E402
from acceptance_guard import overlay_snapshot, report_delta  # noqa: E402
from regressione_browser_save_4e3 import Browser  # noqa: E402

FONT_SERVERS = ["*fonts.googleapis.com*", "*fonts.gstatic.com*"]
PROBE_HOST = "erylos-offline-gate.invalid"     # reserved TLD: blocked here, never resolvable anyway


def page_requests(events) -> list[str]:
    """Every URL the page itself asked for (DevTools Network domain of the page target)."""
    return [e["params"]["request"]["url"] for e in events
            if e.get("method") == "Network.requestWillBeSent" and e.get("params", {}).get("request")]


def external(urls) -> list[str]:
    """The ones that left the app: anything but loopback, data:, blob: and about:."""
    from urllib.parse import urlsplit
    out = []
    for u in urls:
        parts = urlsplit(u)
        if parts.scheme in ("data", "blob", "about"):
            continue
        if (parts.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
            out.append(u)
    return out
SHOT = BASE / "RISULTATO_FONT_LOCALI_UI"
_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:400]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:400]}]"))


# A face declared with font-display:swap is fetched at its FIRST use: a
# measurement taken while that is still in flight reads the fallback and
# calls a healthy product broken. WARMUP asks for every face and the gate
# waits for document.fonts to settle before measuring anything.
WARMUP = ("['400 28px \"Plus Jakarta Sans\"','500 28px \"Plus Jakarta Sans\"','600 28px \"Plus Jakarta Sans\"',"
          "'400 28px \"JetBrains Mono\"','500 28px \"JetBrains Mono\"'].forEach(f => document.fonts.load(f)); true")

MEASURE = r"""
(() => {
  const text = 'Pompa centrifuga P-200 — Panoramica 0123';
  const code = 'MR2023-A10-7 · 45 Nm';
  const span = (family, weight, content, size) => {   // size matters: the same text at a
    const s = document.createElement('span');          // different size measures differently
    s.textContent = content;
    s.style.cssText = 'position:absolute;left:-9999px;white-space:pre;font-size:' + (size || '28px')
      + ';font-weight:' + weight + ';font-family:' + family;
    document.body.appendChild(s);
    const w = Math.round(s.getBoundingClientRect().width * 100) / 100;
    s.remove();
    return w;
  };
  const faces = [...document.fonts].map(f => `${f.family} ${f.weight} ${f.status}`);
  const h1 = document.querySelector('.ms-pagehead h1');
  return {
    facesLoaded: faces.filter(f => /Jakarta|JetBrains/.test(f)),
    pageOpened: !!document.querySelector('#ms-sidebar button') && !!h1,
    h1Family: h1 ? getComputedStyle(h1).fontFamily : null,
    codeElement: (() => {                         // the product's own technical value
      const c = document.querySelector('.ms-code');
      if (!c) return null;
      const t = c.textContent;
      const cs = getComputedStyle(c);
      return {testo: t, larghezza: Math.round(c.getBoundingClientRect().width * 100) / 100,
              conJetBrains: span("'JetBrains Mono'", cs.fontWeight, t, cs.fontSize),
              conConsolas: span("Consolas", cs.fontWeight, t, cs.fontSize),
              dimensione: cs.fontSize, famiglia: cs.fontFamily.split(",")[0]};
    })(),
    widths: {
      interfaccia: span("'Plus Jakarta Sans','Segoe UI',system-ui,sans-serif", 600, text),
      soloJakarta: span("'Plus Jakarta Sans'", 600, text),
      soloSegoeUI: span("'Segoe UI'", 600, text),
      fontInesistente: span("'NoSuchFont12345'", 600, text),
      codici: span("'JetBrains Mono',Consolas,monospace", 500, code),
      soloJetBrains: span("'JetBrains Mono'", 500, code),
      soloConsolas: span("Consolas", 500, code),
    },
    fontServerRequests: performance.getEntriesByType('resource')
        .map(r => r.name).filter(n => /fonts\.(googleapis|gstatic)\.com/.test(n)),
    pageErrors: window.__erylosErrors || [],
  };
})()
"""


def main() -> int:
    print("=" * 78)
    print("BROWSER ACCEPTANCE — ERYLOS SENZA INTERNET: I FONT SONO NEL PRODOTTO")
    print("=" * 78)
    SHOT.mkdir(exist_ok=True)
    with fx.disposable_project("BROWSER_FONTS") as (project, facts):
        # This gate only looks at the interface. The same tripwire every
        # mutating harness uses proves it: the project must come out of the
        # run exactly as it went in.
        overlay_before = overlay_snapshot(project)
        with fx.app_server(project) as url:
            b = Browser()
            try:
                b.call("Network.enable")
                b.call("Network.setCacheDisabled", {"cacheDisabled": True})
                b.call("Network.setBlockedURLs", {"urls": FONT_SERVERS + [f"*{PROBE_HOST}*"]})
                b.call("Page.addScriptToEvaluateOnNewDocument", {"source":
                       "window.__erylosErrors=[];addEventListener('error',e=>window.__erylosErrors."
                       "push(String(e.message||e.type)));"})
                t0 = time.time()
                b.call("Page.navigate", {"url": url})
                b.wait_for("!!document.querySelector('#ms-sidebar button')", timeout=90,
                           label="interfaccia aperta senza internet")
                opened = round(time.time() - t0, 1)
                time.sleep(2)
                b.js("(() => { const n = [...document.querySelectorAll('#ms-sidebar button')]"
                     ".find(x => x.textContent.trim() === 'Impostazioni'); if (n) n.click(); return true; })()")
                time.sleep(4)                      # Impostazioni carries the build number in .ms-code
                b.js(WARMUP)
                b.wait_for("document.fonts.status === 'loaded'", timeout=30, label="font caricati")
                m = b.js(MEASURE)
                (SHOT / "senza_internet.png").write_bytes(
                    base64.b64decode(b.call("Page.captureScreenshot", {"format": "png"}, timeout=90)["data"]))
                # Everything the page asked for while opening and being used.
                # Taken BEFORE the probe below, which is the only request
                # allowed to leave -- and only to prove this list can catch one.
                requested = page_requests(b.events)
                b.js(f"fetch('https://{PROBE_HOST}/probe').catch(() => 0); true")
                time.sleep(1.5)
                b.js("1")                          # drain the events the probe produced
                probe_seen = [u for u in external(page_requests(b.events)) if PROBE_HOST in u]
            finally:
                b.close()
        delta = report_delta(overlay_before, overlay_snapshot(project))

    check("il progetto non viene toccato da questo test", not any(delta.values()),
          {k: v for k, v in delta.items() if v})
    w = m["widths"]
    loaded = {f.rsplit(" ", 1)[0] for f in m["facesLoaded"] if f.endswith("loaded")}
    check("l'interfaccia si apre con i domini dei font bloccati", m["pageOpened"], (opened, m["h1Family"]))
    check("le cinque famiglie/pesi che il prodotto porta con sé sono caricate dalla pagina stessa",
          {"Plus Jakarta Sans 400", "Plus Jakarta Sans 500", "Plus Jakarta Sans 600",
           "JetBrains Mono 400", "JetBrains Mono 500"} <= loaded, sorted(m["facesLoaded"]))
    check("il metodo di misura distingue davvero un font presente da uno assente",
          w["soloJakarta"] != w["fontInesistente"] and w["soloJetBrains"] != w["fontInesistente"]
          and w["soloSegoeUI"] != w["fontInesistente"], w)
    check("i titoli sono disegnati con Plus Jakarta Sans", w["interfaccia"] == w["soloJakarta"], w)
    check("NON viene usato il ripiego Segoe UI", w["interfaccia"] != w["soloSegoeUI"], w)
    check("i valori tecnici sono disegnati con JetBrains Mono", w["codici"] == w["soloJetBrains"], w)
    check("NON viene usato il ripiego Consolas", w["codici"] != w["soloConsolas"], w)
    ce = m["codeElement"]
    check("anche un valore tecnico REALE del prodotto (la versione, in Impostazioni) e' in JetBrains Mono, "
          "non in Consolas",
          ce and ce["larghezza"] == ce["conJetBrains"] and ce["larghezza"] != ce["conConsolas"], ce)
    check("nessuna richiesta a un server di font", not m["fontServerRequests"], m["fontServerRequests"])
    check("nessun errore nella pagina", not m["pageErrors"], m["pageErrors"])
    check("la pagina ha fatto richieste, e il gate le ha viste (non una lista vuota per difetto)",
          len(requested) > 5, len(requested))
    check("NESSUNA richiesta della pagina verso un host esterno (CDN, font, telemetria): solo l'app locale",
          not external(requested), external(requested))
    check("(mutant) una richiesta esterna deliberata viene vista dal gate -- il controllo sopra puo' fallire",
          bool(probe_seen), probe_seen)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    payload = {"suite": "Browser gate -- local fonts, no internet", "tests": total, "pass": passed,
               "fail": total - passed, "misure": m}
    (BASE / "RISULTATO_BROWSER_FONTS.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
    print("\n" + "=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}\n        {detail}")
    print(f"  {passed}/{total} passed")
    print(f"  schermata: {SHOT / 'senza_internet.png'}")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
