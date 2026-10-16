"""Regression: the browser gates find Chrome on any Windows machine.

CP2.4 (12/10/2026) ran the four browser gates on a clean machine where Chrome
lives in Program Files (x86). They started a constant path under Program
Files and all four failed with FileNotFoundError -- an environment difference
that looked like four product failures. chrome_locator.py replaced the
constant; this suite pins its resolution order with fake installs in
temporary folders, so it needs no real Chrome and never touches one.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_chrome_locator.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import chrome_locator as cl  # noqa: E402
import packaging_payload  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:400]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:400]}]"))


def fake_install(root: Path) -> Path:
    exe = root / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ fake chrome for the locator test")
    return exe


def raises(fn):
    try:
        fn()
    except cl.ChromeNotFoundError as e:
        return str(e)
    return None


tmp = Path(tempfile.mkdtemp(prefix="ms_chrome_locator_"))
pf, pf86, local = tmp / "ProgramFiles", tmp / "ProgramFilesX86", tmp / "LocalAppData"
no_registry = lambda: []  # noqa: E731

# ---------------------------------------------------------------- order
x86_exe = fake_install(pf86)
env_x86_only = {"ProgramFiles": str(pf), "ProgramFiles(x86)": str(pf86), "LOCALAPPDATA": str(local)}
check("L1: Chrome only in Program Files (x86) -- the CP2.4 machine -- is found",
      cl.find_chrome(env_x86_only, no_registry) == x86_exe, cl.candidates(env_x86_only, no_registry))

pf_exe = fake_install(pf)
check("L2: with both installs present, Program Files wins over Program Files (x86)",
      cl.find_chrome(env_x86_only, no_registry) == pf_exe, cl.candidates(env_x86_only, no_registry))

user_only = tmp / "user_only"
user_exe = fake_install(user_only / "LocalAppData")
env_user = {"ProgramFiles": str(user_only / "none1"), "ProgramFiles(x86)": str(user_only / "none2"),
            "LOCALAPPDATA": str(user_only / "LocalAppData")}
check("L3: a per-user install (%LOCALAPPDATA%) is found when there is no machine-wide one",
      cl.find_chrome(env_user, no_registry) == user_exe, cl.candidates(env_user, no_registry))

reg_exe = fake_install(tmp / "Relocated")
env_none = {"ProgramFiles": str(tmp / "n1"), "ProgramFiles(x86)": str(tmp / "n2"), "LOCALAPPDATA": str(tmp / "n3")}
check("L4: the App Paths registration is used when no standard location has Chrome",
      cl.find_chrome(env_none, lambda: [str(reg_exe)]) == reg_exe, "")

# ---------------------------------------------------------------- explicit variable
explicit = fake_install(tmp / "Explicit")
check("L5: ERYLOS_CHROME, when set, wins over every standard location",
      cl.find_chrome(dict(env_x86_only, ERYLOS_CHROME=str(explicit)), no_registry) == explicit, "")
msg = raises(lambda: cl.find_chrome(dict(env_x86_only, ERYLOS_CHROME=str(tmp / "missing.exe")), no_registry))
check("L6 (mutant): a wrong ERYLOS_CHROME is an error naming the variable -- never a silent fallback to "
      "another Chrome, even though one is installed",
      msg is not None and "ERYLOS_CHROME" in msg, msg)

# ---------------------------------------------------------------- nothing installed
msg = raises(lambda: cl.find_chrome(env_none, no_registry))
check("L7 (mutant): with no Chrome anywhere the error lists every location tried and says how to fix it",
      msg is not None and all(str(p) in msg for p in cl.candidates(env_none, no_registry))
      and "ERYLOS_CHROME" in msg, msg)
check("L8: the error type is still a FileNotFoundError, so existing handlers keep working",
      issubclass(cl.ChromeNotFoundError, FileNotFoundError), "")

# ---------------------------------------------------------------- the harness and the package
harness = (BASE / "regressione_browser_save_4e3.py").read_text(encoding="utf-8")
check("H1: the shared browser harness no longer starts a fixed Chrome path",
      "Program Files\\Google" not in harness and "chrome_locator.find_chrome()" in harness, "")
real = None
try:
    real = cl.find_chrome()
    real_ok = real.is_file()
except cl.ChromeNotFoundError as e:
    real_ok, real = True, f"not installed here (clear error): {str(e).splitlines()[0]}"
check("H2: on this machine Chrome resolves to an existing file, or the error is the clear one",
      real_ok, real)
check("H3: the locator is test infrastructure -- it is not in the customer package",
      "chrome_locator.py" not in packaging_payload.payload_entries(), "")

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_CHROME_LOCATOR.json").write_text(
    json.dumps({"suite": "chrome_locator", "tests": total, "pass": passed, "fail": total - passed,
                "chrome_on_this_machine": str(real)}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
