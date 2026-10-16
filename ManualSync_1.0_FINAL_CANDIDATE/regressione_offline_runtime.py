"""Regression: ERYLOS starts and serves its interface with the internet refused.

Decision D2/D6 (CP2.2-D): the desktop is offline-first -- 0 automatic external
connections. CP2.4 (12/10/2026) found two violations no gate could see:
Gradio's telemetry (api.gradio.app, huggingface.co) from every process that
imported app, and a CDN script plus font-host preconnects in the page Gradio
serves. This suite runs both start paths with every non-loopback socket
REFUSED and RECORDED (network_guard.py) and requires zero attempts:

  D  the customer path, in process: manualsync_desktop.start_server(), exactly
     what the installed product runs before opening its window;
  P  the development path, as a child process: `python app.py`, the server
     every browser gate drives;
  H  the page both of them serve: no resource the browser would fetch from
     outside the app by itself.

Mutants prove each detector can fail: the guard must catch a deliberate
external lookup and connection, the HTML check must flag Gradio's original
template, and the stripping must remove what a future template could add.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_offline_runtime.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import os
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()      # before any product import: nothing may slip out first

import acceptance_fixture as fx  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:500]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:500]}]"))


def fetch(url: str, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ============================================================ G: the guard itself
try:
    socket.getaddrinfo("erylos-offline-gate.invalid", 443)
    lookup_refused = False
except PermissionError:
    lookup_refused = True
try:
    socket.create_connection(("192.0.2.1", 9), timeout=2)   # TEST-NET-1: refused before any packet
    connect_refused = False
except PermissionError:
    connect_refused = True
except OSError:
    connect_refused = False
caught = GUARD.snapshot()
check("G1 (mutant): a deliberate external lookup is refused and recorded",
      lookup_refused and any(a["host"] == "erylos-offline-gate.invalid" for a in caught), caught)
check("G2 (mutant): a deliberate external connection is refused and recorded",
      connect_refused and any(a["host"] == "192.0.2.1" for a in caught), caught)
local = socket.socket()
local.bind(("127.0.0.1", 0))
local.close()
check("G3: loopback stays allowed", True, "")
GUARD.clear()

# ============================================================ D: the customer start path
import manualsync_desktop as md  # noqa: E402  (after test_isolation: resolves the TEST home)

port = fx.free_port()
demo = None
d_html = d_config = ""
try:
    demo, url = md.start_server(port)        # prepare_runtime() first, as the launcher does
    ready = md.wait_http(url, timeout=60)
    d_html = fetch(url)
    d_config = fetch(url + "config")
    time.sleep(4)                            # background threads (version check, analytics) would fire now
finally:
    if demo is not None:
        demo.close()
d_attempts = GUARD.snapshot()

import app  # noqa: E402  (already imported by start_server)
import offline_frontend  # noqa: E402

check("D1: the customer path starts and answers", ready and "gradio" in d_html.lower(), url)
check("D2: Gradio telemetry is off explicitly -- on the Blocks AND in the environment",
      app.demo.analytics_enabled is False and os.environ.get("GRADIO_ANALYTICS_ENABLED") == "False",
      (app.demo.analytics_enabled, os.environ.get("GRADIO_ANALYTICS_ENABLED")))
check("D3: ZERO external connection attempts while starting and serving the interface",
      not d_attempts, d_attempts)
check("D4: the offline page template is installed", offline_frontend.STATE["installed"],
      offline_frontend.STATE)
check("H1: the page the customer gets loads nothing from outside the app",
      not network_guard.external_loads(d_html), network_guard.external_loads(d_html))
check("H2: the interface itself is still there (config served, ERYLOS components present)",
      len(d_config) > 1000 and "components" in d_config, len(d_config))

# ============================================================ M: the detectors can fail
from gradio import routes  # noqa: E402

loader = routes.templates.env.loader
original_source = loader.loaders[-1].get_source(routes.templates.env, offline_frontend.TEMPLATE)[0]
check("M1 (mutant): the HTML check flags Gradio's ORIGINAL template (CDN script + preconnects)",
      len(network_guard.external_loads(original_source)) >= 1, network_guard.external_loads(original_source))
future = (original_source
          + '\n<link rel="stylesheet" href="https://cdn.example.invalid/a.css">'
          + '\n<script src="//cdn.example.invalid/b.js"></script>'
          + '\n<link rel="dns-prefetch" href="https://cdn.example.invalid">'
          + '\n<script src="./assets/local.js"></script><link rel="icon" href="/favicon.ico">')
stripped, removed = offline_frontend.strip_external_resources(future)
check("M2 (mutant): resources a future template could add -- stylesheet, protocol-relative script, "
      "dns-prefetch -- are stripped too, and the stripped page passes the HTML check",
      not network_guard.external_loads(stripped) and len(removed) >= 3, removed)
check("M3: local resources survive the stripping",
      './assets/local.js' in stripped and 'href="/favicon.ico"' in stripped, "")

# ============================================================ P: the development start path (child)
work = Path(tempfile.mkdtemp(prefix="ms_offline_"))
(work / "sitecustomize.py").write_text(network_guard.sitecustomize_source(str(work / "net.jsonl")), encoding="utf-8")
saved = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = str(work) + (os.pathsep + saved if saved else "")
os.environ.pop("GRADIO_ANALYTICS_ENABLED", None)   # the child must switch it off by itself
p_html = ""
try:
    with fx.disposable_project("OFFLINE_RUNTIME") as (project, _facts):
        with fx.app_server(project) as purl:
            p_html = fetch(purl)
            time.sleep(4)
finally:
    if saved is None:
        os.environ.pop("PYTHONPATH", None)
    else:
        os.environ["PYTHONPATH"] = saved
    os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
p_attempts = network_guard.read_log(str(work / "net.jsonl"))
check("P1: `python app.py` (the server the browser gates drive) makes ZERO external attempts, "
      "with GRADIO_ANALYTICS_ENABLED unset in its environment", not p_attempts and bool(p_html), p_attempts)
check("P2: and its page loads nothing from outside the app", not network_guard.external_loads(p_html),
      network_guard.external_loads(p_html))

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_OFFLINE_RUNTIME.json").write_text(json.dumps(
    {"suite": "offline runtime", "tests": total, "pass": passed, "fail": total - passed,
     "removed_by_offline_frontend": offline_frontend.STATE["removed"],
     "customer_path_attempts": d_attempts, "dev_path_attempts": p_attempts}, ensure_ascii=False, indent=2),
    encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
