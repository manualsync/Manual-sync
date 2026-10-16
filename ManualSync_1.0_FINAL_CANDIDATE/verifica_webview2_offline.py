"""Verify the REAL desktop runtime offline: ManualSync.exe + pywebview + Microsoft WebView2.

VERIFICATION TOOL, run after BUILD_WINDOWS.ps1 -- not a regression suite (it
opens the real application window) and not part of the customer package.

Why this exists
---------------
The customer's interface is rendered by WebView2, not by the Chrome the
browser gates drive. CP2.4 could show a CDN request only in headless Chrome;
whether the shipped runtime reaches the internet had to be measured in the
engine the customer actually runs. This tool starts dist\\ManualSync\\
ManualSync.exe exactly as the desktop shortcut does (no switches) and checks:

  * the interface opens with every host name except 127.0.0.1 unresolvable
    for WebView2 -- the machine of a customer without internet;
  * the page requests NOTHING outside the app (read from the WebView2 page's
    own Network domain, so Edge/WebView2 background services are not counted);
  * the ERYLOS Python process makes NO external attempt (network_guard's
    refusal, injected through a sitecustomize on PYTHONPATH);
  * the ERYLOS fonts are loaded from the package, and the console shows no
    error or exception;
  * after the application is closed nothing of it keeps running, and the
    real ManualSyncData is untouched.

A deliberate external request from the page (refused) proves the request list
can catch one. Exit code 0 only if every check passes.

Usage:  <python 3.12> verifica_webview2_offline.py [--dist DIR] [--port 9333] [--report FILE]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402
import real_data_guard  # noqa: E402

PROBE_HOST = "erylos-offline-gate.invalid"
FACES = ["400 28px \"Plus Jakarta Sans\"", "500 28px \"Plus Jakarta Sans\"", "600 28px \"Plus Jakarta Sans\"",
         "400 28px \"JetBrains Mono\"", "500 28px \"JetBrains Mono\""]


class _Socket:
    """Minimal RFC 6455 text client for the WebView2 DevTools endpoint (stdlib only)."""

    def __init__(self, url: str, timeout: float = 30.0):
        hostport, _, path = url[len("ws://"):].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise RuntimeError(f"websocket handshake refused: {head[:120]!r}")
        self._buf = bytearray(rest)

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b1 & 0x80:
                mask = self._read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
            else:
                payload = self._read(n)
            if (b0 & 0x0F) in (0x1, 0x0):
                parts.append(payload)
                if b0 & 0x80:
                    return b"".join(parts).decode("utf-8", errors="replace")
            elif (b0 & 0x0F) == 0x8:
                raise RuntimeError("websocket closed by peer")


class Page:
    """One DevTools session on the WebView2 page; every event is kept, in order."""

    def __init__(self, ws_url: str):
        self.ws = _Socket(ws_url)
        self.events: list[dict] = []
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(method)

    def js(self, expression: str, await_promise: bool = False):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": await_promise})
        if "exceptionDetails" in res:
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:300])
        return res.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js(expression):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False


def requested_urls(events) -> list[str]:
    return [e["params"]["request"]["url"] for e in events
            if e.get("method") == "Network.requestWillBeSent" and e.get("params", {}).get("request")]


def external(urls) -> list[str]:
    out = []
    for u in urls:
        p = urlsplit(u)
        if p.scheme in ("data", "blob", "about"):
            continue
        if not network_guard.is_loopback(p.hostname):
            out.append(u)
    return out


def console_errors(events) -> list[str]:
    out = []
    for e in events:
        m, p = e.get("method"), e.get("params", {})
        if m == "Runtime.exceptionThrown":
            out.append("exception: " + str(p.get("exceptionDetails", {}).get("text", ""))[:200])
        elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            out.append("console.error: " + " ".join(str(a.get("value", a.get("description", "")))
                                                    for a in p.get("args", []))[:200])
        elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
            out.append("log: " + str(p["entry"].get("text", ""))[:200] + " " + str(p["entry"].get("url", ""))[:100])
    return out


def residual(markers: list[str]) -> list[str]:
    """Processes whose command line still carries one of our run's markers."""
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine)\" }"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    return [line[:160] for line in ps.splitlines() if any(m.lower() in line.lower() for m in markers)
            and "verifica_webview2_offline" not in line and "Get-CimInstance" not in line]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(BASE / "dist" / "ManualSync"))
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--report", default=str(BASE / "RISULTATO_WEBVIEW2_OFFLINE.json"))
    ap.add_argument("--netlog", default="", help="also write WebView2's engine-level net log here and list the "
                    "external hosts in it (Edge background services included -- informational, not a gate)")
    args = ap.parse_args()
    dist = Path(args.dist).resolve()
    exe = dist / "ManualSync.exe"
    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), str(detail)[:600]))
        print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:600]}]"))

    real_root = Path(os.environ.get("USERPROFILE") or Path.home()) / real_data_guard.DATA_DIR_NAME
    before = real_data_guard.snapshot(real_root)
    work = Path(tempfile.mkdtemp(prefix="ms_webview2_offline_"))
    home = work / "home"
    for sub in ("AppData/Local/Temp", "AppData/Roaming", "Desktop", "Documents", "Downloads"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    guard_dir, guard_log = work / "guard", work / "python_net.jsonl"
    guard_dir.mkdir()
    (guard_dir / "sitecustomize.py").write_text(network_guard.sitecustomize_source(str(guard_log)), encoding="utf-8")
    drive, tail = os.path.splitdrive(str(home))
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), HOMEDRIVE=drive, HOMEPATH=tail,
               LOCALAPPDATA=str(home / "AppData" / "Local"), APPDATA=str(home / "AppData" / "Roaming"),
               TEMP=str(home / "AppData" / "Local" / "Temp"), TMP=str(home / "AppData" / "Local" / "Temp"),
               PYTHONPATH=str(guard_dir),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=(f"--remote-debugging-port={args.port} "
                                                      "--host-resolver-rules=\"MAP * ~NOTFOUND , EXCLUDE 127.0.0.1\""
                                                      + (f" --log-net-log=\"{args.netlog}\"" if args.netlog else "")))
    env.pop("GRADIO_ANALYTICS_ENABLED", None)   # the product must switch it off by itself

    print(f"Avvio del runtime reale: {exe}")
    proc = subprocess.Popen([str(exe)], cwd=str(dist), env=env)
    page = None
    facts: dict = {}
    try:
        target, deadline = None, time.time() + 150
        while time.time() < deadline and target is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/list", timeout=2) as r:
                    target = next((t for t in json.load(r) if t.get("type") == "page"
                                   and str(t.get("url", "")).startswith("http://127.0.0.1")), None)
            except Exception:
                pass
            if target is None:
                time.sleep(0.5)
        check("S1: il runtime reale parte e WebView2 apre l'interfaccia ERYLOS su 127.0.0.1",
              target is not None, f"exit={proc.poll()}")
        if target is None:
            return 1
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json/version", timeout=5) as r:
            facts["engine"] = json.load(r)
        page = Page(target["webSocketDebuggerUrl"])
        for m in ("Network.enable", "Runtime.enable", "Log.enable", "Page.enable"):
            page.call(m)
        page.call("Network.setCacheDisabled", {"cacheDisabled": True})
        page.call("Network.setBlockedURLs", {"urls": [f"*{PROBE_HOST}*"]})
        # Reload only once pywebview's own navigation to the app has finished.
        # Reloading earlier hits WebView2's initial about:blank document and
        # cancels that navigation: the window then stays blank for good
        # (measured while writing this tool -- a harness defect, not ERYLOS).
        first_load = page.wait_for("location.href.startsWith('http://127.0.0.1') "
                                   "&& !!document.querySelector('#ms-sidebar button')", 150)
        facts["first_load_complete"] = first_load
        page.js("window.__erylosBeforeReload = true")   # marks the old document
        mark = len(page.events)
        t0 = time.time()
        page.call("Page.reload", {"ignoreCache": True})
        opened = page.wait_for("!window.__erylosBeforeReload && location.href.startsWith('http://127.0.0.1') "
                               "&& !!document.querySelector('#ms-sidebar button')", 120)
        facts["opened_in_s"] = round(time.time() - t0, 1)
        time.sleep(3)
        page.js("[" + ",".join(json.dumps(f) for f in FACES) + "].forEach(f => document.fonts.load(f)); true")
        page.wait_for("document.fonts.status === 'loaded'", 30)
        facts["fonts"] = page.js("[...document.fonts].filter(f => /Jakarta|JetBrains/.test(f.family))"
                                 ".map(f => f.family + ' ' + f.weight + ' ' + f.status)")
        facts["h1_font"] = page.js("(() => { const h = document.querySelector('.ms-pagehead h1');"
                                   " return h ? getComputedStyle(h).fontFamily : null; })()")
        time.sleep(4)
        page.js("1")
        urls = requested_urls(page.events[mark:])          # one complete load, from the reload on
        errors = console_errors(page.events[mark:])
        page.js(f"fetch('https://{PROBE_HOST}/probe').catch(() => 0); true")
        time.sleep(1.5)
        page.js("1")
        probe = [u for u in external(requested_urls(page.events)) if PROBE_HOST in u]
        loaded = {f.rsplit(" ", 1)[0] for f in (facts["fonts"] or []) if f.endswith("loaded")}

        check("S2: e' davvero WebView2 (Edge), non Chrome", "Edg" in str(facts["engine"].get("Browser", "")),
              facts["engine"].get("Browser"))
        check("S3: l'interfaccia si carica con la rete esterna irraggiungibile (avvio e ricaricamento completo)",
              first_load and opened, (facts.get("first_load_complete"), facts.get("opened_in_s")))
        check("N1: la pagina ha fatto richieste e il controllo le ha viste", len(urls) > 5, len(urls))
        check("N2: NESSUNA richiesta della pagina verso host esterni (CDN, font, telemetria)",
              not external(urls), external(urls))
        check("N3 (mutant): una richiesta esterna deliberata viene vista -- N2 puo' fallire", bool(probe), probe)
        check("F1: i cinque font ERYLOS sono caricati dal pacchetto",
              {"Plus Jakarta Sans 400", "Plus Jakarta Sans 500", "Plus Jakarta Sans 600",
               "JetBrains Mono 400", "JetBrains Mono 500"} <= loaded, facts.get("fonts"))
        check("F2: i titoli usano Plus Jakarta Sans", "Plus Jakarta Sans" in str(facts.get("h1_font")),
              facts.get("h1_font"))
        check("C1: nessun errore o eccezione nella console", not errors, errors)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
        time.sleep(3)
    if args.netlog and Path(args.netlog).exists():
        import re
        text = Path(args.netlog).read_text(encoding="utf-8", errors="replace")
        facts["engine_netlog_external_hosts"] = sorted(
            {h for h in re.findall(r"(?:https?|wss?)://([A-Za-z0-9.-]+)", text) if not network_guard.is_loopback(h)})
    python_attempts = network_guard.read_log(str(guard_log))
    left = residual([str(home), f"--remote-debugging-port={args.port}", str(dist)])
    delta = real_data_guard.compare(before, real_data_guard.snapshot(real_root))
    check("P1: il processo Python di ERYLOS non tenta NESSUNA connessione esterna", not python_attempts,
          python_attempts)
    check("R1: chiusa l'applicazione, nessun suo processo resta attivo", not left, left)
    check("D1: ManualSyncData reale invariata", not delta, delta)

    passed = sum(1 for _, ok, _ in results if ok)
    payload = {"tool": "verifica_webview2_offline", "dist": str(dist), "tests": len(results), "pass": passed,
               "fail": len(results) - passed, "facts": facts, "python_attempts": python_attempts,
               "results": [{"check": n, "ok": ok, "detail": d} for n, ok, d in results]}
    Path(args.report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  {passed}/{len(results)} passed  --  report: {args.report}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
