"""Block and record every non-loopback network attempt of a Python process.

TEST / VERIFICATION INFRASTRUCTURE ONLY -- no product module imports this
file, so it is never part of the packaged application.

Why this exists
---------------
ERYLOS Desktop is offline-first (CP2.2-D, D2/D6): starting it, opening the
interface and using it must not contact anything but the local server. CP2.4
measured the opposite in two places -- Gradio's telemetry (api.gradio.app,
huggingface.co) from every test, and a CDN script requested by the page -- and
no gate would have noticed, because none blocked or counted external traffic.

What it provides
----------------
* ``install()`` -- a Python audit hook (PEP 578) on socket resolution and
  connection. Loopback passes; anything else is RECORDED and REFUSED with
  ``PermissionError`` (an ``OSError``, which network code already handles), so
  an offline machine is reproduced exactly and nothing ever leaves.
* ``sitecustomize_source(log)`` -- the same hook for a CHILD process (the app
  server a gate starts, or the packaged runtime): written as sitecustomize.py
  into a folder on the child's PYTHONPATH, it appends every attempt to
  ``log`` as one JSON line the moment it happens, so even a terminated child
  leaves its record behind.
* ``external_loads(html)`` -- which resources a browser would fetch by itself
  from outside the app when it loads ``html``.

An audit hook sees socket calls made through Python. Native code opening its
own sockets bypasses it; the browser-side gates (Chrome DevTools in
regressione_browser_fonts.py, WebView2 in verifica_webview2_offline.py)
cover the page.
"""
from __future__ import annotations

import json
import os
import threading
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

_EVENTS = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
           "socket.gethostbyname_ex", "socket.sendto")


def is_loopback(host: Any) -> bool:
    h = str(host or "").strip().strip("[]").lower()
    return (h in ("", "localhost", "::1", "0.0.0.0")
            or h.startswith("127.") or h.startswith("::ffff:127."))


def _destination(event: str, args: tuple) -> tuple[str, str]:
    """(host, printable destination) of one audited socket event."""
    if event in ("socket.connect", "socket.sendto"):
        addr = args[1]
        host = addr[0] if isinstance(addr, tuple) else addr
    else:
        addr = args[0] if len(args) == 1 else (args[0], args[1])
        host = args[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host), repr(addr)


class Recorder:
    """The attempts one process made; ``install()`` returns it."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self.attempts.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.attempts)


_RECORDER: Recorder | None = None


def install() -> Recorder:
    """Refuse and record every non-loopback socket attempt of THIS process.

    Idempotent: an audit hook cannot be removed, so the first Recorder is
    returned again on later calls.
    """
    global _RECORDER
    if _RECORDER is not None:
        return _RECORDER
    rec = Recorder()

    def _hook(event: str, args: tuple) -> None:
        if event not in _EVENTS:
            return
        host, dest = _destination(event, args)
        if is_loopback(host):
            return
        with rec._lock:
            rec.attempts.append({"event": event, "dest": dest, "host": host, "t": round(time.time(), 3),
                                 "thread": threading.current_thread().name})
        raise PermissionError(f"network_guard: external network refused ({event} {dest})")

    import sys
    sys.addaudithook(_hook)
    _RECORDER = rec
    return rec


def sitecustomize_source(log_path: str) -> str:
    """Source of a sitecustomize.py that installs the same refusal in a child."""
    return f'''"""Written by network_guard.py for one verification run -- never shipped."""
import json, os, sys, threading, time
_LOG = {log_path!r}
_EVENTS = {_EVENTS!r}
_lock = threading.Lock()
def _loop(h):
    h = str(h or "").strip().strip("[]").lower()
    return h in ("", "localhost", "::1", "0.0.0.0") or h.startswith("127.") or h.startswith("::ffff:127.")
def _hook(event, args):
    if event not in _EVENTS:
        return
    if event in ("socket.connect", "socket.sendto"):
        addr = args[1]; host = addr[0] if isinstance(addr, tuple) else addr
    else:
        addr = args[0] if len(args) == 1 else (args[0], args[1]); host = args[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if _loop(host):
        return
    rec = {{"event": event, "dest": repr(addr), "host": str(host), "t": round(time.time(), 3),
            "pid": os.getpid(), "script": os.path.basename(sys.argv[0]) if getattr(sys, "argv", None) else "",
            "thread": threading.current_thread().name}}
    try:
        with _lock, open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\\n")
    except Exception:
        pass
    raise PermissionError("network_guard: external network refused (%s %r)" % (event, addr))
sys.addaudithook(_hook)
'''


def read_log(log_path: str) -> list[dict[str, Any]]:
    """The attempts a child recorded (missing file = none)."""
    if not os.path.exists(log_path):
        return []
    out = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# rel values that make a browser connect or download on its own.
_FETCHING_RELS = {"preconnect", "dns-prefetch", "preload", "prefetch", "modulepreload", "stylesheet", "icon"}
_SRC_TAGS = {"script", "img", "iframe", "source", "video", "audio", "embed", "track"}


class _Loads(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SRC_TAGS and a.get("src"):
            self.urls.append(a["src"])
        elif tag == "link" and a.get("href") and set(a.get("rel", "").lower().split()) & _FETCHING_RELS:
            self.urls.append(a["href"])
        elif tag == "object" and a.get("data"):
            self.urls.append(a["data"])

    handle_startendtag = handle_starttag


def external_loads(html: str) -> list[str]:
    """Absolute, non-loopback URLs ``html`` makes a browser fetch by itself."""
    p = _Loads()
    p.feed(html)
    out = []
    for u in p.urls:
        parts = urlsplit(u.strip())
        absolute = parts.scheme in ("http", "https", "ws", "wss") or u.strip().startswith("//")
        if absolute and not is_loopback(parts.hostname):
            out.append(u.strip())
    return out
