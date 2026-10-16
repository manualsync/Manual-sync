"""Regression: ManualSync.exe ends with the exit status its Python child ended with.

CP2.4 (12/10/2026), build #1: the gate "ManualSync.exe --headless-smoke"
failed with "exit -1" after ~3 s, with nothing in any log, and passed 3/3
when repeated. CP2.5-A found why the code said nothing: the launcher did
`raise SystemExit(subprocess.call(...))`, and subprocess reports a Windows
status as an UNSIGNED number. A child ended by the system (crash 0xC0000005,
closed console / Ctrl+C 0xC000013A, ...) returns >= 2**31, SystemExit cannot
convert that to a C long on Windows, and the process exits with -1 instead.
manualsync_launcher.exit_status() maps it to the signed 32-bit value, which
Windows reports as the very same status.

The end-to-end checks run the launcher's exact pattern -- a parent that calls a
child and raises SystemExit with what the child returned -- with a child that
the system terminates with STATUS_CONTROL_C_EXIT.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_launcher_exit_status.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import manualsync_launcher as launcher  # noqa: E402  (no side effect: main() runs only as __main__)

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:400]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:400]}]"))


STATUS_CONTROL_C_EXIT = 0xC000013A
CHILD = ("import ctypes; k = ctypes.windll.kernel32; "
         f"k.TerminateProcess(ctypes.c_void_p(k.GetCurrentProcess()), {STATUS_CONTROL_C_EXIT})")


def parent_exit(use_exit_status: bool) -> int:
    """Exit status (unsigned, as Windows reports it) of a parent shaped like the launcher."""
    wrap = "launcher.exit_status(rc)" if use_exit_status else "rc"
    parent = (f"import subprocess, sys; sys.path.insert(0, {str(BASE)!r}); import manualsync_launcher as launcher; "
              f"rc = subprocess.call([sys.executable, '-I', '-c', {CHILD!r}]); raise SystemExit({wrap})")
    return subprocess.run([sys.executable, "-I", "-c", parent], capture_output=True).returncode


check("E1: ordinary statuses are unchanged",
      [launcher.exit_status(c) for c in (0, 1, 2, 0x7FFFFFFF)] == [0, 1, 2, 0x7FFFFFFF], "")
check("E2: a system status >= 2**31 becomes the signed 32-bit value with the same bits",
      launcher.exit_status(STATUS_CONTROL_C_EXIT) == STATUS_CONTROL_C_EXIT - (1 << 32)
      and (launcher.exit_status(STATUS_CONTROL_C_EXIT) & 0xFFFFFFFF) == STATUS_CONTROL_C_EXIT, "")
fixed = parent_exit(True)
check("E3: end to end -- the parent ends with the child's real status 0xC000013A",
      fixed == STATUS_CONTROL_C_EXIT, hex(fixed))
legacy = parent_exit(False)
check("E4 (mutant): the old pattern loses it and ends with -1 (0xFFFFFFFF) -- the CP2.4 symptom",
      legacy == 0xFFFFFFFF, hex(legacy))
src = (BASE / "manualsync_launcher.py").read_text(encoding="utf-8")
check("E5: the launcher's single exit point passes every status through exit_status() "
      "(diagnostic switches, desktop session, incomplete installation)",
      [ln.strip() for ln in src.splitlines() if ln.strip().startswith("raise SystemExit(")]
      == ["raise SystemExit(exit_status(main()))"], "")

calls = [ln.strip() for ln in src.splitlines() if "subprocess.call(" in ln and not ln.strip().startswith("#")]
check("E6 (CP2.5-A.1): the diagnostic child runs without a console window, like the desktop child -- "
      "no console of its own, so no console event can end a build or installer gate",
      "return subprocess.call(cmd, cwd=str(app), env=env, creationflags=creationflags)" in src
      and src.count("creationflags=creationflags") == 2
      and 'getattr(subprocess, "CREATE_NO_WINDOW", 0)' in src, calls)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_LAUNCHER_EXIT_STATUS.json").write_text(json.dumps(
    {"suite": "launcher exit status", "tests": total, "pass": passed, "fail": total - passed,
     "fixed_parent_exit": hex(fixed), "legacy_parent_exit": hex(legacy)}, indent=2), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
