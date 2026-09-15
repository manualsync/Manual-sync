"""4E.3 BROWSER / E2E GATE -- real-Chrome Save acceptance.

*** THIS IS NOT PART OF THE LIGHTWEIGHT PYTHON SUITE. ***
Run it only at a 4E.3 checkpoint / release candidate. It drives ONE
instance of the ALREADY-INSTALLED system Chrome against ONE already-
running app server, one scenario at a time, sequentially.

Why this exists
---------------
Three consecutive Save fixes passed every server-side Python suite and
still failed real-browser acceptance. The reason, once reproduced
automatically here, was NOT value propagation (that worked): a save
refused by a precondition -- an empty "Il tuo nome" -- returned through
_studio_noop_bundle, which by design emits gr.update() no-ops for every
output except st_save_msg. The refusal was therefore visible only in a
Markdown far below the editor, so in the browser a refused save was
indistinguishable from "the button did nothing": the version simply
never moved. No server-side test could ever have caught that, because
server-side every one of those callbacks returned exactly the right
values -- the defect lived entirely in WHERE the user could see them.

Prerequisites (deliberately NOT automated -- one server, one browser):
  * the app already running on http://127.0.0.1:7860
  * the active project = a DISPOSABLE clone, named "ZZ_..."
    This harness edits content. acceptance_guard refuses to let it run
    against the canonical demo or any other real project, and reports the
    overlay delta it wrote. Build a clone with canonical_demo.py.
  * Chrome installed at the standard Windows location

Usage:  ..\\.venv\\Scripts\\python.exe regressione_browser_save_4e3.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from acceptance_guard import overlay_snapshot, report_delta
import acceptance_fixture as fx  # noqa: E402
import authoring_view as av  # noqa: E402

APP_URL = "http://127.0.0.1:7860/"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# Resolved at RUN time by the acceptance guard -- see main(). It used to
# be hardcoded to 4E3_Rich_Text_Manual_Acceptance, and that is precisely how
# a reference demo acquired an extra decision nobody asked for: a harness
# that edits content pointed at the project everyone else treated as a
# baseline. This harness writes block versions and audit events, so it may
# only run against a disposable clone.
#
# Resolved in main() rather than here so that merely IMPORTING this module
# -- which regressione_browser_export_4f1.py does, for Browser and APP_URL
# -- never trips the guard.
PROJECT = ""
# Resolved from the fixture this gate builds for itself -- see seed_blocks().
# These used to be the ids of two blocks inside a hand-made project named
# 4E3_Rich_Text_Manual_Acceptance that exists on no checkout but one. The
# gate then failed during setup everywhere else, which is indistinguishable
# from a real regression and is why it stopped being trusted.
PLAIN_BLK = ""
RICH_BLK = ""
BASELINE_TEXT = "Prima di iniziare, verificare che la pompa sia scollegata."
EDITED_TEXT = "Prima di iniziare, verificare che la pompa sia sempre scollegata."
# The rich fixture's documented baseline. Restored EXACTLY (never by
# string-surgery on whatever the previous run left behind): Chrome's
# execCommand('bold') legitimately extends a selection over the trailing
# space, so bolding "scollegare" yields "**scollegare **" -- a brittle
# replace("**scollegare**", ...) silently misses that, leaves the word
# bold, and the NEXT run's Grassetto click then TOGGLES IT OFF and fails.
# Resetting both fixtures to a fixed baseline before every run makes this
# gate idempotent and order-independent.
RICH_BASELINE = ("**ATTENZIONE**: scollegare l'alimentazione al connettore `P-401` prima di "
                 "_procedere con la manutenzione_. Consulta il [manuale online](https://example.com/hx400).")
ACTOR = "Redattore Tecnico"

results: list[dict] = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if not ok else ""))
    if not ok:
        raise AssertionError(f"{name}: {detail}")


# ---------------------------------------------------------------------
# Minimal Chrome DevTools Protocol client -- stdlib only (no selenium,
# no playwright, no websocket package, no browser download).
# ---------------------------------------------------------------------
class _WS:
    def __init__(self, url: str, timeout: float = 30.0):
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port_s = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port_s or "80")), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed early")
            buf += chunk
        head, _, remainder = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket handshake failed: {head!r}")
        self._buf = bytearray(remainder)

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_text(self, text):
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", n))
        mask = os.urandom(4)
        header.extend(mask)
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv_text(self):
        chunks = []
        while True:
            b0, b1 = self._read(2)
            fin, opcode, masked, length = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else b""
            data = self._read(length)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:
                hdr = bytearray([0x8A, 0x80 | len(data)])
                m = os.urandom(4)
                hdr.extend(m)
                self.sock.sendall(bytes(hdr) + bytes(b ^ m[i % 4] for i, b in enumerate(data)))
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise RuntimeError("websocket closed by peer")
            chunks.append(data)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class Browser:
    def __init__(self, port=9222):
        self.port = port
        self.profile = tempfile.mkdtemp(prefix="ms_e2e_profile_")
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--disable-background-networking", "--disable-sync",
             "--disable-gpu", "--window-size=1400,1200", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                    json.load(r)
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("Chrome DevTools endpoint never came up")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
            page = [t for t in json.load(r) if t.get("type") == "page"][0]
        self.ws = _WS(page["webSocketDebuggerUrl"])
        self._id = 0

    def call(self, method, params=None, timeout=60.0):
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def js(self, expression):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS error: {json.dumps(res['exceptionDetails'])[:400]}")
        return res.get("result", {}).get("value")

    def click(self, selector):
        box = self.js(f"""
          (() => {{
            const el = document.querySelector({selector!r});
            if (!el) return null;
            el.scrollIntoView({{block:'center'}});
            const r = el.getBoundingClientRect();
            return {{x: r.left + r.width/2, y: r.top + r.height/2, w: r.width}};
          }})()
        """)
        if not box or not box["w"]:
            raise RuntimeError(f"not clickable: {selector} ({box})")
        for typ in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent",
                      {"type": typ, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1})

    def type_text(self, text):
        self.call("Input.insertText", {"text": text})

    def wait_for(self, expression, timeout=25.0, label=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expression):
                return True
            time.sleep(0.4)
        raise TimeoutError(f"timed out waiting for {label or expression}")

    def close(self):
        self.ws.close()
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------
# DB helpers -- the browser proves what the USER sees; these prove what
# was actually persisted. Both must agree for a PASS.
# ---------------------------------------------------------------------
def db_state(block_id=None):
    # Resolved at CALL time, not at def time: PLAIN_BLK is now filled in
    # by the fixture seed, and a default argument would have captured the
    # empty placeholder once, when this module was imported.
    block_id = block_id or PLAIN_BLK
    repo, svc = av.open_service(PROJECT)
    d = av.block_detail(svc, block_id)
    audit = repo.conn.execute("SELECT COUNT(*) c FROM audit_events WHERE object_id=?",
                              (block_id,)).fetchone()["c"]
    repo.close()
    return {"version": d["version"], "format": d["text_format"], "text": d["text"], "audit": audit}


def seed_blocks(project_dir):
    """Create this gate's two subject blocks inside the disposable project.

    One plain paragraph and one rich paragraph, because the scenarios
    below exercise both the plain path and the markdown/rich path. They
    are created through AuthoringService, the same way the product
    creates a block, so the rows carry a real version and real audit
    history rather than being written straight into sqlite.
    """
    repo, svc = av.open_service(str(project_dir))
    try:
        topic_id = repo.conn.execute(
            "SELECT topic_id FROM topics ORDER BY topic_id LIMIT 1").fetchone()["topic_id"]
        plain = svc.create_block_in_topic(topic_id, "paragraph", actor=ACTOR,
                                          text=BASELINE_TEXT, text_format="plain")
        rich = svc.create_block_in_topic(topic_id, "paragraph", actor=ACTOR,
                                         text=RICH_BASELINE, text_format="rich")
    finally:
        repo.close()
    return {"plain_block": plain["block_id"], "rich_block": rich["block_id"],
            "topic_id": topic_id}


def reset_fixture():
    """Puts BOTH fixture blocks back to their documented baselines, so
    this gate is idempotent and order-independent -- it can be re-run any
    number of times, and a scenario that aborts half-way never poisons
    the next run (or the human's manual retest)."""
    repo, svc = av.open_service(PROJECT)
    cur = av.block_detail(svc, PLAIN_BLK)
    if cur["text"] != BASELINE_TEXT or cur["text_format"] != "plain":
        svc.save_block(PLAIN_BLK, text=BASELINE_TEXT, expected_version=cur["version"],
                       actor=ACTOR, text_format="plain")
    rcur = av.block_detail(svc, RICH_BLK)
    if rcur["text"] != RICH_BASELINE or rcur["text_format"] != "rich":
        svc.save_block(RICH_BLK, text=RICH_BASELINE, expected_version=rcur["version"],
                       actor=ACTOR, text_format="rich")
    repo.close()
    return db_state()


VERSION_RE = "(document.body.innerText.match(/versione\\s+(\\d+)/i) || [])[1]"
BANNER = "(document.querySelector('.ms-rich-save-status')||{}).innerText"


def open_block(br, block_id):
    br.js(f"""
      (() => {{
        const r = Array.from(document.querySelectorAll('input[type=radio]'))
                       .find(x => x.value === 'block:{block_id}');
        r.closest('label').click(); return true;
      }})()
    """)
    br.wait_for("!!document.querySelector('#ms-rich-editable-inner')", label="editor mounted")
    time.sleep(1.5)


def main():
    """Build a disposable project, run the gate against it, throw it away.

    The whole run lives inside fx.disposable_project(), which sets and
    then RE-VERIFIES the active-project pointer through acceptance_guard,
    proves the starting data matches canonical_demo's documented
    baseline, restores the caller's project on the way out and only then
    deletes the fixture.
    """
    global PROJECT, PLAIN_BLK, RICH_BLK
    global APP_URL
    with fx.disposable_project("Browser_Save_4E3", seed=seed_blocks) as (target, facts):
        PROJECT = str(target)
        PLAIN_BLK = facts["plain_block"]
        RICH_BLK = facts["rich_block"]
        print(f"fixture blocks: plain={PLAIN_BLK} rich={RICH_BLK}")
        # Own the server too: app.py reads the active project once at
        # import, so an already-running instance would be pinned to a
        # different project and this gate would pass or fail against
        # data it never created.
        with fx.app_server(target) as url:
            APP_URL = url
            _run(overlay_snapshot(target))


def _run(overlay_before):
    baseline = reset_fixture()
    print("=" * 78)
    print(f"FIXTURE BASELINE: version={baseline['version']} format={baseline['format']} "
          f"audit={baseline['audit']} text={baseline['text']!r}")
    print("=" * 78)
    check("e2e0 (fixture): the plain paragraph starts at the documented baseline text, text_format='plain'",
          baseline["text"] == BASELINE_TEXT and baseline["format"] == "plain", baseline)

    br = Browser()
    try:
        br.call("Page.enable")
        br.call("Runtime.enable")
        br.call("Page.navigate", {"url": APP_URL})
        br.wait_for("document.querySelectorAll('button').length > 3", timeout=60, label="app mounted")
        time.sleep(2)
        br.js("Array.from(document.querySelectorAll('button')).find(x=>x.textContent.includes('Contenuti')).click()")
        br.wait_for("document.querySelectorAll('input[type=radio]').length > 3", label="outline loaded")
        time.sleep(2)

        # ------------------------------------------------------------------
        # SCENARIO A -- the REPRODUCED defect: Save refused because "Il tuo
        # nome" is empty must be visible RIGHT NEXT TO THE EDITOR, not only
        # in a Markdown far below it. Before the fix this returned a
        # bundle of pure no-ops for every slot but st_save_msg, so the
        # browser showed nothing at all near the editor and the version
        # silently stayed put -- indistinguishable from a dead button.
        # ------------------------------------------------------------------
        open_block(br, PLAIN_BLK)
        start_version = int(br.js(VERSION_RE))
        check("e2eA1: the opened paragraph's visible header matches the DB version",
              start_version == baseline["version"], (start_version, baseline["version"]))
        br.click("#ms-save-btn")
        br.wait_for(f"{BANNER} && {BANNER}.length > 0", label="refusal banner")
        refusal = br.js(BANNER)
        check("e2eA2 (THE FIXED DEFECT): saving with an empty 'Il tuo nome' shows a contained, human-readable "
              "refusal in the banner next to the editor -- never a silent no-op",
              "Inserisci il tuo nome" in (refusal or ""), refusal)
        after_refusal = db_state()
        check("e2eA3: the refused save mutated nothing (version and audit unchanged)",
              after_refusal["version"] == baseline["version"] and after_refusal["audit"] == baseline["audit"],
              after_refusal)
        check("e2eA4: no red Error control anywhere on the page after the refusal",
              not br.js("document.body.innerText.includes('Error')"), None)

        # ------------------------------------------------------------------
        # SCENARIO B -- the ticket's exact acceptance scenario, with the
        # identity field filled in the ordinary way a writer would.
        # ------------------------------------------------------------------
        # UX Gate 2A: the identity field is the shell context bar's
        # "Operatore". The tab-local "Il tuo nome" copy was removed --
        # it asked the same question the context bar already asks. Same
        # field, same fail-closed rule, new address.
        br.js("""
          (() => {
            const inp = Array.from(document.querySelectorAll(
                '.ms-ctxbar input[type=text], .ms-ctxbar textarea')).pop();
            inp.focus(); return true;
          })()
        """)
        br.type_text(ACTOR)
        br.js("""
          (() => {
            Array.from(document.querySelectorAll(
                '.ms-ctxbar input[type=text], .ms-ctxbar textarea')).pop().blur();
            return true;
          })()
        """)
        time.sleep(2)
        check("e2eB0: the identity field visibly holds the typed name",
              br.js("""(() => Array.from(document.querySelectorAll(
                       '.ms-ctxbar input[type=text], .ms-ctxbar textarea')).pop().value)()""") == ACTOR, None)

        open_block(br, PLAIN_BLK)
        before = db_state()
        vis_version_before = int(br.js(VERSION_RE))
        check("e2eB1: header shows the baseline version before editing",
              vis_version_before == before["version"], (vis_version_before, before))
        check("e2eB2: the visible editor shows the baseline sentence, with no raw markup",
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText") == BASELINE_TEXT
              and "**" not in (br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerHTML") or ""),
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText"))

        # real typing: caret placed before "scollegata", then "sempre "
        br.js("""
          (() => {
            const root = document.querySelector('#ms-rich-editable-inner');
            root.focus();
            const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let node = null;
            while (w.nextNode()) { if (w.currentNode.textContent.includes('scollegata')) { node = w.currentNode; break; } }
            const i = node.textContent.indexOf('scollegata');
            const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i);
            const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); return true;
          })()
        """)
        br.type_text("sempre ")
        time.sleep(0.8)
        check("e2eB3: the typed word appears in the visible editor DOM",
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText") == EDITED_TEXT,
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText"))

        # ONE click, no blur first, no second click, no refresh
        br.click("#ms-save-btn")
        br.wait_for(f"{BANNER} && {BANNER}.includes('Salvato')", timeout=30, label="save confirmation")
        banner = br.js(BANNER)
        vis_version_after = int(br.js(VERSION_RE))
        after = db_state()

        check("e2eB4 (visible status): the contained banner next to the editor shows 'Salvato — versione N+1'",
              f"Salvato — versione {before['version'] + 1}" in (banner or ""), banner)
        check("e2eB5 (visible header): the header version incremented to N+1 in the same click",
              vis_version_after == before["version"] + 1, (vis_version_before, vis_version_after))
        check("e2eB6 (DB text): the persisted text contains the typed word",
              after["text"] == EDITED_TEXT, after["text"])
        check("e2eB7 (DB version): incremented exactly once", after["version"] == before["version"] + 1,
              (before["version"], after["version"]))
        check("e2eB8 (DB audit): incremented exactly once", after["audit"] == before["audit"] + 1,
              (before["audit"], after["audit"]))
        check("e2eB9 (format policy): a plain-only edit keeps text_format='plain'",
              after["format"] == "plain", after["format"])
        check("e2eB10 (editor still correct): the editor still visibly contains the edited sentence",
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText") == EDITED_TEXT, None)
        check("e2eB11: no red Error control anywhere after the save",
              not br.js("document.body.innerText.includes('Error')"), None)
        check("e2eB12: no raw canonical markup leaked into the visible editor",
              "**" not in (br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerHTML") or ""), None)

        # ------------------------------------------------------------------
        # SCENARIO C -- immediate second click, nothing changed.
        # ------------------------------------------------------------------
        br.click("#ms-save-btn")
        br.wait_for(f"{BANNER} && {BANNER}.includes('Nessuna modifica')", timeout=30, label="no-op status")
        noop_banner = br.js(BANNER)
        noop_db = db_state()
        check("e2eC1 (no-op status): a second click with nothing changed shows 'Nessuna modifica da salvare.'",
              "Nessuna modifica da salvare." in (noop_banner or ""), noop_banner)
        check("e2eC2 (no-op = zero mutation): version and audit both unchanged",
              noop_db["version"] == after["version"] and noop_db["audit"] == after["audit"], noop_db)
        check("e2eC3: no error shown for the no-op save",
              not br.js("document.body.innerText.includes('Error')"), None)

        # ------------------------------------------------------------------
        # SCENARIO D -- rich-format sanity: the plain-save fix must not have
        # repaired plain text while breaking formatting.
        # ------------------------------------------------------------------
        rich_before = db_state(RICH_BLK)
        open_block(br, RICH_BLK)
        check("e2eD1: the rich paragraph opens showing REAL formatting, never raw markup",
              br.js("!!document.querySelector('#ms-rich-editable-inner strong')")
              and "**" not in (br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerHTML") or ""),
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerHTML"))
        check("e2eD1b (precondition): the word about to be bolded is NOT already bold -- otherwise the "
              "toolbar click would TOGGLE IT OFF and the test would assert the wrong thing",
              not br.js("""(() => Array.from(document.querySelectorAll('#ms-rich-editable-inner b, #ms-rich-editable-inner strong'))
                       .some(e => e.textContent.includes('scollegare')))()"""),
              rich_before["text"])
        # select one ordinary (currently unformatted) word, then press Grassetto
        br.js("""
          (() => {
            const root = document.querySelector('#ms-rich-editable-inner');
            root.focus();
            const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let node = null;
            while (w.nextNode()) { if (w.currentNode.textContent.includes('scollegare')) { node = w.currentNode; break; } }
            const i = node.textContent.indexOf('scollegare');
            const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 'scollegare'.length);
            const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); return true;
          })()
        """)
        br.js("Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='Grassetto').click()")
        time.sleep(1.5)
        check("e2eD2: the toolbar visually bolded the selected word in the DOM",
              br.js("""(() => Array.from(document.querySelectorAll('#ms-rich-editable-inner b, #ms-rich-editable-inner strong'))
                       .some(e => e.textContent.includes('scollegare')))()"""), None)
        br.click("#ms-save-btn")
        br.wait_for(f"{BANNER} && {BANNER}.includes('Salvato')", timeout=30, label="rich save confirmation")
        rich_after = db_state(RICH_BLK)
        check("e2eD3: the rich save bumped the version exactly once",
              rich_after["version"] == rich_before["version"] + 1, (rich_before["version"], rich_after["version"]))
        # Chrome's execCommand legitimately extends a bold over the
        # trailing space, so the canonical form is "**scollegare**" OR
        # "**scollegare **" -- both are valid, both render identically.
        check("e2eD4: the newly bolded word persisted as real canonical formatting",
              "**scollegare" in rich_after["text"], rich_after["text"])
        check("e2eD5: the block stayed 'rich'", rich_after["format"] == "rich", rich_after["format"])
        # reopen and confirm it still renders as formatting, not markup
        open_block(br, PLAIN_BLK)
        open_block(br, RICH_BLK)
        check("e2eD6: reopening the rich paragraph still shows bold visually, with no '**' visible",
              br.js("""(() => Array.from(document.querySelectorAll('#ms-rich-editable-inner b, #ms-rich-editable-inner strong'))
                       .some(e => e.textContent.includes('scollegare')))()""")
              and "**" not in (br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText") or ""), None)

        # reopening the PLAIN paragraph still shows the saved edit
        open_block(br, PLAIN_BLK)
        check("e2eB13 (reopen): reopening the plain paragraph still shows the saved sentence",
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText") == EDITED_TEXT,
              br.js("(document.querySelector('#ms-rich-editable-inner')||{}).innerText"))
    finally:
        # ALWAYS put the fixture back, even if a scenario aborted half-way
        # -- a failed run must never leave edited content behind for the
        # next run or for the human's manual retest.
        br.close()
        reset_fixture()

    final_plain, final_rich = db_state(PLAIN_BLK), db_state(RICH_BLK)
    print("\nRESTORED FIXTURE:")
    print(f"  {PLAIN_BLK}: version={final_plain['version']} format={final_plain['format']} text={final_plain['text']!r}")
    print(f"  {RICH_BLK}: version={final_rich['version']} format={final_rich['format']}")
    check("e2eZ (handoff): BOTH fixture paragraphs are restored to their documented baselines",
          final_plain["text"] == BASELINE_TEXT and final_plain["format"] == "plain"
          and final_rich["text"] == RICH_BASELINE and final_rich["format"] == "rich",
          (final_plain, final_rich))

    # What this run actually wrote. It is NOT zero -- this harness edits
    # content on purpose -- but it must be visible, and reset_fixture()
    # must have put the content back.
    report_delta(overlay_before, overlay_snapshot(Path(PROJECT)))

    print("\n" + json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"), "tests": len(results)}))


if __name__ == "__main__":
    main()
