"""Every regression suite imports this module FIRST -- before any product module.

TEST INFRASTRUCTURE ONLY (see real_data_guard.py for the incident that made
it necessary). Importing it:

* records where the user's REAL ManualSyncData is, and exports it as
  $ERYLOS_PROTECTED_DATA_HOME so child processes know it too;
* moves HOME / USERPROFILE (and HOMEDRIVE / HOMEPATH) to a private
  temporary home, so ``app``, ``acceptance_guard``, ``manualsync_desktop``
  and every child process they start resolve their data folder there;
* installs the real-data guard: from here on this process cannot write,
  delete, list, open a database in -- or start a child process against --
  the real folder;
* refuses to run if a module that resolves the data folder at import time
  (``app`` and friends) was imported before it, because that module has
  already looked at the real folder.

``copy_real_project(name)`` is the one sanctioned way for a test to use a
real project: a byte copy into the test home, never the original.

The temporary home is removed at exit, unless MS_KEEP_FIXTURE=1 (kept for
diagnosis, its path printed).
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import real_data_guard as _guard

# Modules that resolve the data folder when they are imported.
HOME_AT_IMPORT = ("app", "acceptance_guard", "acceptance_fixture", "manualsync_desktop", "commercial_acceptance")

_early = [m for m in HOME_AT_IMPORT if m in sys.modules]
if _early:
    raise _guard.IsolationError(
        f"test_isolation imported AFTER {', '.join(_early)}: that module has already resolved the REAL data "
        f"folder. Move `import test_isolation` above every product import.")

PROTECTED_DATA_HOME = _guard.protected_data_home()          # before the home moves
os.environ[_guard.ENV_PROTECTED] = str(PROTECTED_DATA_HOME)

TEST_HOME = Path(tempfile.mkdtemp(prefix="erylos_test_home_"))
# A Windows profile skeleton, not an empty folder: with USERPROFILE pointing
# at an empty directory headless Chrome (driven by the browser gates) never
# opens its DevTools port -- measured 22/09/2026: 30 s timeout, vs 0.6 s with
# these folders present.
for _sub in ("AppData/Local/Temp", "AppData/Roaming", "Desktop", "Documents", "Downloads"):
    (TEST_HOME / _sub).mkdir(parents=True, exist_ok=True)
_drive, _tail = os.path.splitdrive(str(TEST_HOME))
os.environ["HOME"] = str(TEST_HOME)
os.environ["USERPROFILE"] = str(TEST_HOME)
os.environ["HOMEDRIVE"] = _drive
os.environ["HOMEPATH"] = _tail
os.environ[_guard.ENV_TEST_HOME] = str(TEST_HOME)
os.environ[_guard.ENV_ISOLATED] = "1"

_guard.install_guard(PROTECTED_DATA_HOME)

if _guard.is_protected(Path.home() / _guard.DATA_DIR_NAME):      # pragma: no cover - defensive
    raise _guard.IsolationError(f"home still resolves to the real data folder: {Path.home()}")


def copy_real_project(name: str) -> Path | None:
    """A byte copy of the real project `name` inside the test home, or None if it does not exist."""
    src = PROTECTED_DATA_HOME / "Projects" / name
    if not src.is_dir():
        return None
    dst = TEST_HOME / "real_project_copies" / name
    if dst.exists():
        shutil.rmtree(dst)
    return _guard.copy_protected_tree(src, dst)


def _cleanup() -> None:
    if os.environ.get("MS_KEEP_FIXTURE"):
        print(f"test home kept for diagnosis: {TEST_HOME}")
        return
    shutil.rmtree(TEST_HOME, ignore_errors=True)


atexit.register(_cleanup)
