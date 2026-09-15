"""Deterministic, disposable ZZ_ projects for the browser acceptance gates.

Why this exists
---------------
Both browser gates used to require a project that happened to exist on
one machine -- `4E3_Rich_Text_Manual_Acceptance`, and a manual named
"Manuale Pompa HX-400" carrying a variant profile called "EU Service".
Neither is in the repository. On any other checkout both suites failed
during setup, before asserting anything about the product, and that
failure looked exactly like a real regression.

For a Commercial Candidate path that is not acceptable: a gate that
cannot run proves nothing, and a gate that fails for environmental
reasons trains people to ignore it.

So each gate now builds its own project, from the same builder that
produces the canonical demo, and throws it away afterwards. Nothing is
hardcoded to a historical fixture: the suites read the publication
title, profile name and block ids back out of what they just created.

What this module guarantees
---------------------------
* the target is always disposable -- the name carries the ``ZZ_`` prefix
  ``acceptance_guard`` requires, and the guard is re-asked after the
  pointer is set rather than trusted;
* the starting data is deterministic -- it comes from
  ``canonical_demo.build_canonical_demo`` and is verified against that
  module's own documented baseline before a gate runs;
* the caller's active project is restored even when the gate raises;
* the disposable project is deleted once the pointer is safely back.

Set ``MS_KEEP_FIXTURE=1`` to keep the project for diagnosis; the path is
printed either way. Keeping it is rarely needed -- the fixture is
reproducible by re-running, which is the point of building it here.
"""
from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import acceptance_guard as ag
import authoring_view as av
import canonical_demo


class FixtureError(RuntimeError):
    """Raised when a disposable fixture cannot be built or activated."""


def _pointer_raw() -> str:
    """The active-project pointer file's current bytes, verbatim.

    Stored and restored as raw text rather than re-serialised: whatever
    else the app keeps in that file belongs to the app, not to us.
    """
    if not ag.ACTIVE_PROJECT_FILE.exists():
        raise FixtureError(f"No active-project pointer at {ag.ACTIVE_PROJECT_FILE}")
    return ag.ACTIVE_PROJECT_FILE.read_text(encoding="utf-8")


def _point_at(project_dir: Path) -> None:
    ag.ACTIVE_PROJECT_FILE.write_text(
        json.dumps({"project_dir": str(project_dir)}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def build(suffix: str) -> Path:
    """A fresh ``ZZ_<suffix>`` project with the canonical demo's content.

    Reuses the canonical demo builder rather than duplicating fixture
    logic: it already produces a coherent small manual (three topics,
    real blocks, one publication) plus the compliance overlay, and it
    ships with a documented baseline we can assert against.
    """
    name = f"{ag.DISPOSABLE_PREFIX}{suffix}"
    if not name.startswith(ag.DISPOSABLE_PREFIX):
        raise FixtureError(f"fixture name must be disposable: {name}")
    dest = ag.DATA_ROOT / name
    if dest.exists():
        shutil.rmtree(dest)
    ag.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    canonical_demo.build_canonical_demo(ag.DATA_ROOT, name=name)
    if not dest.exists():
        raise FixtureError(f"builder did not produce {dest}")
    return dest


def verify_deterministic(project_dir: Path) -> dict[str, Any]:
    """Assert the freshly built project matches the documented baseline.

    A gate that starts from drifted data reports drift as a product
    regression. Measuring here means a fixture problem is named as a
    fixture problem, at setup, before any browser work.
    """
    measured = canonical_demo.measure(project_dir)
    if not canonical_demo.matches_baseline(measured):
        raise FixtureError(
            "freshly built fixture does not match canonical_demo.BASELINE -- "
            f"measured {measured!r}")
    return measured


def publication_title(project_dir: Path) -> str:
    """The fixture's own publication title, read back from the database."""
    repo, _svc = av.open_service(str(project_dir))
    try:
        row = repo.conn.execute(
            "SELECT title FROM publications WHERE publication_id=?",
            (canonical_demo.PUBLICATION_ID,)).fetchone()
    finally:
        repo.close()
    if not row:
        raise FixtureError(f"fixture has no publication {canonical_demo.PUBLICATION_ID}")
    return row["title"]


@contextmanager
def disposable_project(suffix: str, *, seed: Callable[[Path], dict] | None = None):
    """Build, activate, yield ``(project_dir, facts)``, then restore and delete.

    ``seed`` receives the project directory and returns whatever facts the
    gate needs to know about what it created -- block ids, a profile name.
    Those come back to the caller instead of being hardcoded, which is
    what stops the next fixture rename from breaking the gate.
    """
    previous = _pointer_raw()
    project = build(suffix)
    facts: dict[str, Any] = {}
    try:
        _point_at(project)
        # Ask the guard rather than trusting our own naming: this is the
        # same check every mutating harness runs, and it is the one that
        # would stop a mistake here from reaching a real project.
        confirmed = ag.require_disposable_target()
        if Path(confirmed).resolve() != project.resolve():
            raise FixtureError(f"pointer verification failed: {confirmed} != {project}")
        baseline = verify_deterministic(project)
        facts["project_dir"] = project
        facts["baseline"] = baseline
        facts["publication_id"] = canonical_demo.PUBLICATION_ID
        facts["publication_title"] = publication_title(project)
        if seed is not None:
            facts.update(seed(project) or {})
        print(f"fixture ready: {project.name}  (publication: {facts['publication_title']!r})")
        yield project, facts
    finally:
        # Restore the caller's project FIRST. Deleting a directory the
        # pointer still references would leave the app pointing at
        # nothing, which is a worse failure than leaving a stale fixture.
        restored = False
        try:
            ag.ACTIVE_PROJECT_FILE.write_text(previous, encoding="utf-8")
            restored = True
            print(f"active project restored: {ag.active_project_dir().name}")
        except Exception as exc:  # pragma: no cover - reported, never swallowed
            print(f"WARNING: could not restore the active-project pointer: {exc}")
        if restored and not os.environ.get("MS_KEEP_FIXTURE"):
            # Windows keeps the sqlite file locked for a moment after the
            # app server exits and after the last repository connection is
            # closed, so a single rmtree loses the race and leaves the
            # fixture behind. Retry briefly rather than reporting a
            # cleanup failure that is really just timing.
            import time
            last = None
            for attempt in range(10):
                try:
                    shutil.rmtree(project)
                    last = None
                    break
                except OSError as exc:
                    last = exc
                    time.sleep(0.5)
            if last is None:
                print(f"disposable fixture deleted: {project.name}")
            else:
                print(f"WARNING: could not delete {project}: {last}")
        else:
            print(f"disposable fixture kept at: {project}")


# ----------------------------------------------------------------------
# Owning the server
# ----------------------------------------------------------------------
# A gate that builds its own project must also start its own app, because
# app.py resolves the active project ONCE at import (INITIAL_PROJECT). A
# server that is already running is pinned to whatever project it started
# on, so repointing the pointer underneath it would leave the gate
# driving the wrong data while looking perfectly healthy.
#
# Each gate therefore launches its own instance on its own free port. As
# a side effect, running a gate no longer disturbs (or is disturbed by) an
# app the user already has open on 7860.

def free_port() -> int:
    """An ephemeral port the OS says is free right now."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def app_server(project_dir: Path, *, timeout: float = 90.0):
    """Start app.py against `project_dir`, yield its base URL, stop it.

    The pointer must already name `project_dir` -- this only starts the
    process that will read it. Waits for the port to actually answer
    rather than sleeping a guessed interval, and fails loudly with the
    server's own output if it never comes up.
    """
    import socket
    import subprocess
    import sys
    import time

    port = free_port()
    env = dict(os.environ, MS_APP_PORT=str(port), MS_NO_BROWSER="1",
               PYTHONIOENCODING="utf-8")
    log = Path(__file__).with_name(f".acceptance_server_{port}.log")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("app.py"))],
        cwd=str(Path(__file__).resolve().parent), env=env,
        stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/"
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise FixtureError(
                    f"app server exited early (code {proc.returncode}); log:\n"
                    + log.read_text(encoding='utf-8', errors='replace')[-2000:])
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.5)
        else:
            raise FixtureError(f"app server did not answer on {port} within {timeout}s")
        time.sleep(2.0)  # let Gradio finish mounting before the first click
        print(f"app server started on {url} (project: {project_dir.name})")
        yield url
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        try:
            log.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"app server on port {port} stopped")
