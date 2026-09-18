"""Temporary directories whose file has been handed to someone else.

The problem
-----------
Two exports build their deliverable in a fresh ``tempfile.mkdtemp()`` and
then give the path away:

* the project backup is returned to a ``gr.File``. Gradio copies it into
  its own cache after the callback returns, so the temp copy is useless
  from then on -- but the callback cannot delete it before returning it.
* the HTML package ZIP is served later, on demand, by the "Scarica"
  link that ``export_links`` resolves from an in-memory token.

Neither was ever deleted. Measured on a development machine: 210 backup
directories and ~100 HTML ZIP directories accumulated in %TEMP%, one per
export and one per acceptance run.

The policy
----------
A directory is *handed over* to a group with a ``keep`` bound. Only the
``keep`` most recent directories of that group survive; older ones are
deleted as soon as a newer one is handed over, and every survivor is
deleted when the process exits. So the number of leftovers is bounded
while the application runs and is zero after it closes normally.

``keep`` is chosen by the caller from the lifetime of what it handed out:

* backup: 1. Gradio runs an event's callbacks one at a time (its default
  concurrency limit is 1), so by the time the next backup is produced the
  previous one has already been copied into Gradio's cache.
* HTML ZIP: ``export_links._MAX_ENTRIES``. At most that many download
  tokens are alive, all among the most recent registrations, so keeping
  that many ZIPs can never delete one a live link still points to.

``discard()`` is for a directory that was never handed over: a build that
failed half way, or a ZIP whose link could not be created.

Nothing here changes what an export produces or where its final artifact
lives; it only bounds the lifetime of intermediate copies.
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from pathlib import Path

_lock = threading.Lock()
_groups: dict[str, list[Path]] = {}


def make_dir(prefix: str) -> Path:
    """A fresh temporary directory, exactly like ``tempfile.mkdtemp``."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def discard(path: str | Path | None) -> None:
    """Delete a temporary directory now, and forget it if it was tracked."""
    if not path:
        return
    target = Path(path)
    with _lock:
        for dirs in _groups.values():
            if target in dirs:
                dirs.remove(target)
    shutil.rmtree(target, ignore_errors=True)


def hand_over(path: str | Path, *, group: str, keep: int) -> None:
    """Track a directory whose file was just given out; trim the group to ``keep``."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    target = Path(path)
    with _lock:
        dirs = _groups.setdefault(group, [])
        if target in dirs:
            dirs.remove(target)
        dirs.append(target)
        stale = dirs[:-keep]
        del dirs[:-keep]
    for old in stale:
        shutil.rmtree(old, ignore_errors=True)


def tracked(group: str | None = None) -> list[Path]:
    """Directories still held, oldest first. For tests and diagnostics."""
    with _lock:
        if group is not None:
            return list(_groups.get(group, []))
        return [p for dirs in _groups.values() for p in dirs]


def release_all() -> None:
    """Delete every tracked directory. Registered to run at interpreter exit."""
    with _lock:
        dirs = [p for group in _groups.values() for p in group]
        _groups.clear()
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(release_all)
