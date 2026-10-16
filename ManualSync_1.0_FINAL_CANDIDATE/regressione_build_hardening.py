"""Regression: the Windows build installs exactly what was verified, and ships no dead weight.

CP2.5-A.1. Two properties of BUILD_WINDOWS.ps1 that used to be missing:

* Reproducible dependencies. The lock pinned only the direct packages, with no
  hash; pip upgraded itself to the latest release of the day and resolved every
  indirect dependency at build time, so two builds of the same commit could ship
  different code. Now pip/setuptools, the build environment and the customer
  runtime are installed from complete, hashed locks (--require-hashes --no-deps)
  on Python 3.12.10 exactly.
* A minimal runtime. The build copied a whole CPython install and pip's
  console-script launchers; CP2.5-A proved 18 CLI launchers, pip*.exe, Doc and
  Lib\\test unused (they even pointed at the build machine's .buildvenv). The
  build now removes them BEFORE the first runtime smoke test.

Static checks on the scripts and lock files -- no network, no build. Mutants
prove each checker can fail.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_build_hardening.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:400]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:400]}]"))


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_ENTRY = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)\s+--hash=sha256:([0-9a-f]{64})(?:\s+#.*)?$")


def lock_problems(text: str) -> list[str]:
    """Lines of a hashed lock that are not `name==version --hash=sha256:<64 hex>`."""
    bad, seen = [], set()
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = _ENTRY.match(s)
        if not m:
            bad.append(s[:80])
        elif norm(m.group(1)) in seen:
            bad.append(f"duplicate {m.group(1)}")
        else:
            seen.add(norm(m.group(1)))
    return bad


def pins(text: str) -> dict[str, str]:
    out = {}
    for ln in text.splitlines():
        s = ln.split("#", 1)[0].strip()
        if "==" in s:
            name, _, rest = s.partition("==")
            out[norm(name)] = rest.split()[0]
    return out


def read(name: str) -> str:
    return (BASE / name).read_text(encoding="utf-8")


boot, build, runtime = (read(f"requirements.{n}.hashes.txt") for n in ("bootstrap", "build", "runtime"))
script = read("BUILD_WINDOWS.ps1")
code = "\n".join(ln for ln in script.splitlines() if not ln.lstrip().startswith("#"))

# ============================================================ L: the locks
check("L1: every line of the three locks is an exact pin with a sha256 hash, no duplicates",
      not (lock_problems(boot) + lock_problems(build) + lock_problems(runtime)),
      lock_problems(boot) + lock_problems(build) + lock_problems(runtime))
check("L1b (mutant): a pin without hash, or a loose pin, is caught",
      len(lock_problems("gradio==6.5.1\nnumpy>=2 --hash=sha256:" + "a" * 64)) == 2,
      lock_problems("gradio==6.5.1\nnumpy>=2 --hash=sha256:" + "a" * 64))
pb, pbuild, prt = pins(boot), pins(build), pins(runtime)
check("L2: pip and setuptools are pinned in the bootstrap lock", "pip" in pb and "setuptools" in pb, pb)
for direct, full, label in (("requirements.runtime.lock.txt", prt, "runtime"),
                            ("requirements.desktop.lock.txt", {**pbuild, **pb}, "build"),
                            ("requirements.lock.txt", prt, "runtime (test lock)")):
    d = pins(read(direct))
    wrong = {k: (v, full.get(k)) for k, v in d.items() if full.get(k) != v}
    check(f"L3: every direct pin of {direct} is in the {label} lock at the SAME version", not wrong, wrong)
mismatch = {k: (v, pbuild.get(k)) for k, v in prt.items() if pbuild.get(k) != v}
check("L4: the runtime lock is a subset of the build lock at identical versions", not mismatch, mismatch)
check("L5: the indirect dependencies are locked too (runtime 63 packages, build environment complete)",
      len(prt) >= 60 and len(pbuild) + len(pb) >= len(prt) + 5, (len(prt), len(pbuild), len(pb)))

# ============================================================ B: the build script
installs = [ln.strip() for ln in code.splitlines() if re.search(r"-m pip install|pip\.exe.* install|\$Pip install", ln)]
unhashed = [ln for ln in installs if "--require-hashes" not in ln or "--no-deps" not in ln]
check("B1: every pip install in BUILD_WINDOWS.ps1 uses --require-hashes --no-deps", installs and not unhashed, unhashed)
check("B2: pip is no longer upgraded to the latest release, and no unhashed lock is installed",
      "--upgrade pip" not in code and not re.search(r"install[^\n]*requirements\.(desktop|runtime)?\.?lock\.txt", code), "")
check("B2b (mutant): the B1 rule flags an unhashed install",
      [ln for ln in ["& $BuildPython -m pip install -r x.txt"] if "--require-hashes" not in ln] != [], "")
check("B3: the build requires Python 3.12.10 exactly (base interpreter and build environment)",
      code.count("sys.version_info[:3]==(3,12,10)") >= 2, "")
check("B4: a build environment is reused only if it was installed from these exact locks",
      "ERYLOS_BUILD_LOCK.txt" in code and "$BuildLockId" in code and "-not $LockMatches" in code, "")
check("B5: the source-only package is built against the pinned setuptools (no build isolation)",
      code.count("--no-build-isolation") >= 2, "")

# ============================================================ M: runtime minimality
prune = re.search(r"\$RuntimePrune\s*=\s*@\((.*?)\)\s*\n", code)
targets = re.findall(r"Join-Path \$(\w+) '([^']+)'", prune.group(1)) if prune else []
check("M1: exactly the four reductions proven safe in CP2.5-A are applied -- nothing else",
      sorted(targets) == sorted([("RuntimeSite", "bin"), ("Runtime", "Scripts"), ("Runtime", "Doc"),
                                 ("Runtime", "Lib\\test")]), targets)
i_prune, i_smoke = code.find("$RuntimePrune"), code.find("--resource-check")
check("M2: the reduction happens BEFORE the first runtime smoke test -- the validated runtime is the shipped one",
      0 < i_prune < i_smoke, (i_prune, i_smoke))
check("M3: the build fails if a reduced path or a CLI launcher is still there",
      "Riduzione del runtime fallita" in code and "Entry point CLI ancora presenti" in code, "")

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_BUILD_HARDENING.json").write_text(json.dumps(
    {"suite": "build hardening", "tests": total, "pass": passed, "fail": total - passed,
     "locked": {"bootstrap": len(pb), "build": len(pbuild), "runtime": len(prt)}}, indent=2), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
