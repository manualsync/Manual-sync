"""Protection of the user's REAL data folder during every test.

TEST INFRASTRUCTURE ONLY -- no product module imports this file, so it is
never part of the packaged application.

Why this exists
---------------
On 21/09/2026 a read-only investigation found that a normal regression run
touched ``C:\\Users\\<user>\\ManualSyncData``: the three browser gates
created and deleted ``ZZ_`` projects there and rewrote
``active_project.json``; thirty suites imported ``app`` with the real home,
so ``app`` read the real pointer and opened the real project's databases
(WAL files created and deleted next to them); one suite opened the real
``4E1_Manual_Acceptance`` project in place; the launcher smoke tests
appended to the real logs. No project content changed, but the folder was
written to, and a future schema change would have migrated a real database
during an ordinary test run.

Three layers now make that impossible, and each one fails closed:

1. ``test_isolation`` (imported FIRST by every suite) moves HOME/USERPROFILE
   to a private temporary home, so nothing resolves the real folder.
2. ``install_guard`` adds a Python audit hook that REFUSES, inside the test
   process, any write, delete, rename, mkdir, directory listing, SQLite
   connection -- and any plain read outside an explicit copy window -- on
   the protected folder, and refuses to start a child process whose home
   would be the real one.
3. The regression runners take a fingerprint of the protected folder
   before the run and compare it afterwards (``--snapshot`` / ``--verify``):
   any file created, deleted or changed -- or any folder whose entries
   changed, even transiently -- turns the whole run into a FAIL that names
   the file.

The protected folder is ``$ERYLOS_PROTECTED_DATA_HOME`` when set (the
runners and every isolated process set it, so child processes inherit the
REAL location even though their own home is a temporary one); otherwise it
is ``<home>/ManualSyncData`` computed before any isolation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

ENV_PROTECTED = "ERYLOS_PROTECTED_DATA_HOME"
ENV_ISOLATED = "ERYLOS_TEST_ISOLATION"
ENV_TEST_HOME = "ERYLOS_TEST_HOME"
DATA_DIR_NAME = "ManualSyncData"


class RealDataAccessError(PermissionError):
    """A test tried to touch the user's real data folder."""


class IsolationError(RuntimeError):
    """A test is not running inside the temporary test home."""


# ---------------------------------------------------------------- the protected location

def protected_data_home() -> Path:
    """The user's REAL data folder -- to be computed before isolating."""
    value = os.environ.get(ENV_PROTECTED)
    if value:
        return Path(value)
    return Path.home() / DATA_DIR_NAME


def _norm(value) -> str | None:
    """A comparable absolute path, or None for fds, memory DBs and non-paths."""
    if value is None or isinstance(value, int):
        return None
    try:
        text = os.fsdecode(os.fspath(value))
    except TypeError:
        return None
    if text.startswith("file:"):                      # sqlite URI
        text = text[5:].split("?", 1)[0]
        if re.match(r"^/[A-Za-z]:", text):
            text = text[1:]
    if not text or text == ":memory:":
        return None
    return os.path.normcase(os.path.abspath(text))


def home_data_dir(env) -> str | None:
    """Where a process started with `env` would put its ManualSyncData."""
    upper = {str(k).upper(): str(v) for k, v in dict(env).items()}
    home = upper.get("USERPROFILE")
    if not home and upper.get("HOMEDRIVE") and upper.get("HOMEPATH"):
        home = upper["HOMEDRIVE"] + upper["HOMEPATH"]
    if not home:
        return None
    return _norm(Path(home) / DATA_DIR_NAME)


# ---------------------------------------------------------------- layer 2: the audit hook

_ROOT: str | None = None
_READ_WINDOW = False

# event -> positions of the path arguments
_PATH_ARGS = {
    "open": (0,), "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.rename": (0, 1),
    "os.truncate": (0,), "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,), "os.link": (0, 1),
    "os.symlink": (0, 1), "os.listdir": (0,), "os.scandir": (0,), "glob.glob": (0,), "glob.glob/2": (0,),
    "shutil.copyfile": (0, 1), "shutil.copytree": (0, 1), "shutil.copymode": (0, 1), "shutil.copystat": (0, 1),
    "shutil.rmtree": (0,), "shutil.move": (0, 1), "sqlite3.connect": (0,),
}
_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | getattr(os, "O_EXCL", 0))
_LISTING = {"os.listdir", "os.scandir", "glob.glob", "glob.glob/2"}
_COPY_FROM = {"shutil.copyfile", "shutil.copytree", "shutil.copymode", "shutil.copystat"}


def is_protected(path) -> bool:
    p = _norm(path)
    return bool(_ROOT and p and (p == _ROOT or p.startswith(_ROOT + os.sep)))


def _is_read_open(args) -> bool:
    mode = args[1] if len(args) > 1 else None
    if isinstance(mode, str):
        return not (set(mode) & set("wax+"))
    flags = args[2] if len(args) > 2 else 0
    return not (int(flags or 0) & _WRITE_FLAGS)


def _refuse(event, path, why="") -> None:
    raise RealDataAccessError(
        f"REAL DATA PROTECTED: a test tried '{event}' on {path}{(' -- ' + why) if why else ''}. "
        f"Tests must use the temporary test home, never the real {DATA_DIR_NAME}.")


def _hook(event, args) -> None:
    if event == "subprocess.Popen":
        env = args[3] if len(args) > 3 and args[3] is not None else os.environ
        child = home_data_dir(env)
        if _ROOT and child and child == _ROOT:
            _refuse(event, child, "the child process would run with the real home")
        return
    positions = _PATH_ARGS.get(event)
    if positions is None:
        return
    hits = [(i, args[i]) for i in positions if i < len(args) and is_protected(args[i])]
    if not hits:
        return
    if event == "sqlite3.connect":
        _refuse(event, hits[0][1], "opening a database creates -wal/-shm/journal files even to read")
    if _READ_WINDOW:
        if event == "open" and _is_read_open(args):
            return
        if event in _LISTING:
            return
        if event in _COPY_FROM and all(i == 0 for i, _ in hits):
            return
    _refuse(event, hits[0][1])


def install_guard(protected: Path) -> None:
    """Refuse, for the rest of this process, every touch of `protected`."""
    global _ROOT
    root = _norm(protected)
    if _ROOT is not None:
        if root != _ROOT:
            raise IsolationError(f"guard already protects {_ROOT}, cannot switch to {root}")
        return
    _ROOT = root
    sys.addaudithook(_hook)


def guard_installed() -> bool:
    return _ROOT is not None


@contextmanager
def read_window():
    """Plain reads (and copies FROM the protected folder) allowed; nothing else."""
    global _READ_WINDOW
    _READ_WINDOW = True
    try:
        yield
    finally:
        _READ_WINDOW = False


def copy_protected_tree(src: Path, dst: Path) -> Path:
    """Byte copy of a folder inside the protected area to a place outside it."""
    if not is_protected(src):
        raise IsolationError(f"not a protected source: {src}")
    if is_protected(dst):
        _refuse("copy", dst, "a copy may only go OUT of the protected folder")
    with read_window():
        shutil.copytree(src, dst)
    return dst


def require_isolated(who: str) -> None:
    """Stop `who` unless this process runs in the temporary test home with the guard on."""
    problems = []
    if os.environ.get(ENV_ISOLATED) != "1":
        problems.append(f"{ENV_ISOLATED} is not set")
    if not guard_installed():
        problems.append("the real-data guard is not installed in this process")
    elif _norm(Path.home() / DATA_DIR_NAME) == _ROOT:
        problems.append("the home is still the real one")
    if problems:
        raise IsolationError(
            f"{who} refuses to run outside the test isolation ({'; '.join(problems)}). "
            f"Put `import test_isolation` BEFORE any product import in the suite.")


# ---------------------------------------------------------------- layer 3: fingerprints

def snapshot(root: Path) -> dict:
    """Every file (size, mtime, sha256) and every folder (mtime) under `root`. Read-only."""
    root = Path(root)
    state = {"root": str(root), "exists": root.exists(), "files": {}, "dirs": {}}
    if not root.exists():
        return state
    for d, _dirs, files in os.walk(root):
        rel_d = os.path.relpath(d, root)
        state["dirs"][rel_d] = os.stat(d).st_mtime_ns
        for name in files:
            p = Path(d) / name
            st = p.stat()
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            state["files"][str(p.relative_to(root))] = [st.st_size, st.st_mtime_ns, h.hexdigest()]
    return state


def compare(before: dict, after: dict) -> list[str]:
    """Human-readable changes between two snapshots; empty means untouched."""
    out = []
    if before.get("exists") != after.get("exists"):
        out.append(f"CARTELLA {'CREATA' if after.get('exists') else 'CANCELLATA'}: {after.get('root')}")
    fb, fa = before.get("files", {}), after.get("files", {})
    db, da = before.get("dirs", {}), after.get("dirs", {})
    for k in sorted(set(fa) - set(fb)):
        out.append(f"FILE CREATO: {k}")
    for k in sorted(set(fb) - set(fa)):
        out.append(f"FILE CANCELLATO: {k}")
    for k in sorted(set(fa) & set(fb)):
        if fa[k][2] != fb[k][2] or fa[k][0] != fb[k][0]:
            out.append(f"FILE MODIFICATO (contenuto): {k}")
        elif fa[k][1] != fb[k][1]:
            out.append(f"FILE MODIFICATO (data di modifica): {k}")
    for k in sorted(set(da) - set(db)):
        out.append(f"CARTELLA CREATA: {k}")
    for k in sorted(set(db) - set(da)):
        out.append(f"CARTELLA CANCELLATA: {k}")
    for k in sorted(set(da) & set(db)):
        if da[k] != db[k]:
            out.append(f"CARTELLA TOCCATA (file creati o cancellati al suo interno, anche solo temporanei): {k}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fingerprint of the real ManualSyncData around a test run.")
    ap.add_argument("--root", help="protected folder (default: $%s or ~/%s)" % (ENV_PROTECTED, DATA_DIR_NAME))
    ap.add_argument("--snapshot", metavar="OUT_JSON")
    ap.add_argument("--verify", metavar="BEFORE_JSON")
    ap.add_argument("--report", metavar="OUT_TXT")
    ap.add_argument("--print-protected", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root) if a.root else protected_data_home()
    if a.print_protected:
        print(root)
        return 0
    if a.snapshot:
        state = snapshot(root)
        Path(a.snapshot).write_text(json.dumps(state), encoding="utf-8")
        print(f"dati reali: impronta registrata ({len(state['files'])} file, {len(state['dirs'])} cartelle) -- {root}")
        return 0
    if a.verify:
        before = json.loads(Path(a.verify).read_text(encoding="utf-8"))
        if Path(before.get("root", "")) != root:
            print(f"FAIL: impronta di {before.get('root')} confrontata con {root}")
            return 2
        changes = compare(before, snapshot(root))
        text = ("DATI REALI INVARIATI: " + str(root)) if not changes else (
            f"DATI REALI MODIFICATI ({len(changes)}): {root}\n" + "\n".join("  " + c for c in changes))
        print(text)
        if a.report:
            Path(a.report).write_text(text + "\n", encoding="utf-8")
        return 0 if not changes else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
