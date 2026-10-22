"""CP3.2 regression: ProjectLayout resolves exactly the paths the product used before.

project_layout.py became the single place that knows the data home and the
content-database file of a project. This suite pins that it produces the
SAME paths the duplicated code produced (including the legacy
ManualSync_CCMS_v18.sqlite fallback and its lookup order), that it is pure
(no file created, no network, standard library only), and that every module
migrated to it still behaves exactly as before -- same returned path, same
exception type, same message or error code.

All fixtures live in temporary folders inside the isolated test home.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_project_layout.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()      # before any product import: any non-loopback socket is refused and recorded

import project_layout as pl  # noqa: E402
from project_layout import ProjectLayout  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="erylos_cp32_layout_"))
_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:600]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:600]}]"))


def outcome(fn, *args):
    """('ok', value) or ('raise', exception type name, message, code) -- what a caller observes."""
    try:
        return ("ok", fn(*args))
    except Exception as exc:  # noqa: BLE001 -- the observed behaviour IS the exception
        return ("raise", type(exc).__name__, str(exc), getattr(exc, "code", None))


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# The duplicated pre-CP3.2 lookup, verbatim in behaviour (search order, legacy fallback).
def legacy_lookup(project_dir) -> Path | None:
    base = Path(project_dir)
    for name in ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"):
        if (base / name).exists():
            return base / name
    return None


# ------------------------------------------------------------ fixtures: the four on-disk states
def project(name: str, *files: str) -> Path:
    d = TMP / "projects" / name
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_bytes(b"")
    return d


P_NONE = project("none")
P_CURRENT = project("current", "ManualSync_CCMS.sqlite")
P_LEGACY = project("legacy", "ManualSync_CCMS_v18.sqlite")
P_BOTH = project("both", "ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite")
STATES = {"none": P_NONE, "current": P_CURRENT, "legacy": P_LEGACY, "both": P_BOTH}

# ============================================================ 1. names and data home
check("L1: the file and folder names are exactly the ones on customers' disks",
      (pl.DATA_DIR_NAME, pl.PROJECTS_DIR_NAME, pl.CCMS_DB_NAME, pl.LEGACY_CCMS_DB_NAME)
      == ("ManualSyncData", "Projects", "ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"), "")
check("L2: lookup order is current name first, legacy second",
      pl.CCMS_DB_CANDIDATES == ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"), pl.CCMS_DB_CANDIDATES)
known_home = TMP / "home_x"
check("L3: given a known home, the data home is <home>/ManualSyncData and Projects below it",
      pl.default_data_home(known_home) == known_home / "ManualSyncData"
      and pl.projects_dir(pl.default_data_home(known_home)) == known_home / "ManualSyncData" / "Projects", "")
# (compared through the isolated home: suites may not spell the real-data path, see regressione_test_isolation S2)
check("L4: without a home it is <Path.home()>/ManualSyncData -- what the old inline expression produced",
      Path.home() == test_isolation.TEST_HOME
      and pl.default_data_home() == test_isolation.TEST_HOME / pl.DATA_DIR_NAME, pl.default_data_home())
check("L5: the default data home here is the isolated TEST home, never the real one",
      pl.default_data_home().parent == test_isolation.TEST_HOME, pl.default_data_home())
check("L6: str and Path homes give the same result", pl.default_data_home(str(known_home)) == pl.default_data_home(known_home), "")

# ============================================================ 2. ProjectLayout paths
check("P1: ccms_db is <project>/ManualSync_CCMS.sqlite, as `Path(project_dir) / name` was",
      all(ProjectLayout(d).ccms_db == Path(d) / "ManualSync_CCMS.sqlite" for d in STATES.values()), "")
check("P2: str and Path roots are equivalent; the root is kept as a Path",
      ProjectLayout(str(P_CURRENT)) == ProjectLayout(P_CURRENT) and isinstance(ProjectLayout(str(P_CURRENT)).root, Path), "")
rel = ProjectLayout("rel/proj")
check("P3: a relative root stays relative and is never resolved against the cwd",
      rel.ccms_db == Path("rel/proj/ManualSync_CCMS.sqlite") and not rel.ccms_db.is_absolute(), rel.ccms_db)
check("P4: the same input always yields the same path (deterministic, hashable, immutable)",
      ProjectLayout(P_BOTH).find_ccms_db() == ProjectLayout(P_BOTH).find_ccms_db()
      and hash(ProjectLayout(P_BOTH)) == hash(ProjectLayout(str(P_BOTH))), "")
try:
    ProjectLayout(None)  # type: ignore[arg-type]
    none_root = "accepted"
except TypeError:
    none_root = "TypeError"
check("P5: a None root fails like `Path(None)` did (TypeError), never a silent default", none_root == "TypeError", none_root)
found = {k: ProjectLayout(d).find_ccms_db() for k, d in STATES.items()}
check("P6: find_ccms_db: none -> None, current -> current, legacy -> legacy, both -> current",
      found == {"none": None, "current": P_CURRENT / "ManualSync_CCMS.sqlite",
                "legacy": P_LEGACY / "ManualSync_CCMS_v18.sqlite", "both": P_BOTH / "ManualSync_CCMS.sqlite"}, found)
check("P7: find_ccms_db is identical to the old duplicated lookup in every state",
      all(ProjectLayout(d).find_ccms_db() == legacy_lookup(d) for d in STATES.values()), "")
check("P8: has_ccms_db is False only when neither file exists",
      {k: ProjectLayout(d).has_ccms_db() for k, d in STATES.items()} == {"none": False, "current": True, "legacy": True, "both": True}, "")

# ============================================================ 3. purity: nothing created, nothing opened
before = tree(TMP)
ghost = TMP / "does" / "not" / "exist"
g = ProjectLayout(ghost)
_ = (g.ccms_db, g.find_ccms_db(), g.has_ccms_db(), pl.default_data_home(TMP / "ghost_home"),
     pl.projects_dir(TMP / "ghost_home"), [ProjectLayout(d).find_ccms_db() for d in STATES.values()])
check("S1: constructing layouts and resolving paths creates, removes or changes nothing on disk",
      tree(TMP) == before and not ghost.exists() and not (TMP / "ghost_home").exists(), sorted(set(tree(TMP)) ^ set(before)))
check("S2: every fixture database is still the 0-byte file written above (nothing opened as SQLite)",
      all(p.stat().st_size == 0 for d in STATES.values() for p in d.iterdir()), "")
src = (BASE / "project_layout.py").read_text(encoding="utf-8")
t = ast.parse(src)
imported = {a.name for n in ast.walk(t) if isinstance(n, ast.Import) for a in n.names} | \
           {n.module for n in ast.walk(t) if isinstance(n, ast.ImportFrom) and n.module}
check("S3: project_layout imports only the standard library it needs (dataclasses, pathlib)",
      imported <= {"__future__", "dataclasses", "pathlib"}, imported)
calls = {n.func.attr for n in ast.walk(t) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
check("S4: project_layout never creates, writes, opens, lists or deletes (static)",
      not calls & {"mkdir", "makedirs", "write_text", "write_bytes", "open", "unlink", "rmdir", "iterdir", "glob",
                   "rglob", "connect", "execute", "touch", "rename", "replace"}, calls)
check("S5: project_layout has no import-time statement besides constants and definitions",
      all(isinstance(s, (ast.ImportFrom, ast.Import, ast.FunctionDef, ast.ClassDef, ast.Assign))
          or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in t.body)
      and not any(isinstance(s, ast.Assign) and isinstance(s.value, ast.Call) for s in t.body), "")

# ============================================================ 4. the migrated callers behave as before
import authoring_view as av  # noqa: E402
import compliance_view as cv  # noqa: E402
import domain_errors as de  # noqa: E402
import project_view as pv  # noqa: E402
import shell_view as sv  # noqa: E402

EXPECTED = {
    # helper: (value when found, outcome when missing)
    "authoring_view.ccms_db_path": (av.ccms_db_path, ("raise", "DomainFileMissingError", de.ERR_AUTHORING_REPOSITORY_MISSING)),
    "compliance_view.ccms_db_path": (cv.ccms_db_path, ("raise", "DomainFileMissingError", de.ERR_PROJECT_REPOSITORY_MISSING)),
    "project_view.ccms_db": (pv.ccms_db, ("raise", "FileNotFoundError", "Repository CCMS non trovato nel progetto")),
}
for label, (fn, missing) in EXPECTED.items():
    got = {k: outcome(fn, d) for k, d in STATES.items()}
    ok_found = all(got[k] == ("ok", legacy_lookup(STATES[k])) for k in ("current", "legacy", "both"))
    m = got["none"]
    ok_missing = m[0] == "raise" and m[1] == missing[1] and (missing[2] in (m[2], m[3]))
    check(f"C-{label}: same path in every state and the same {missing[1]} ({missing[2]}) when missing",
          ok_found and ok_missing, got)
check("C-shell_view.is_project_dir: needs the manifest AND a content database (either name)",
      [sv.is_project_dir(d) for d in (P_NONE, P_CURRENT, P_LEGACY, P_BOTH)] == [False, False, False, False]
      and all(sv.is_project_dir(project(f"m_{k}", "ManualSync_Project.json", *f)) == exp for k, f, exp in (
          ("none", (), False), ("cur", ("ManualSync_CCMS.sqlite",), True), ("leg", ("ManualSync_CCMS_v18.sqlite",), True))), "")
check("C-shell_view.content_count: no database -> 0, without creating one",
      sv.content_count(P_NONE) == 0 and not (P_NONE / "ManualSync_CCMS.sqlite").exists(), sv.content_count(P_NONE))

import project_lifecycle  # noqa: E402
import release_readiness  # noqa: E402

created = project_lifecycle.create_project(TMP / "lc", project_name="Layout Check", source_lang="it-IT")
check("C-project_lifecycle.create_project: the new project's database is <project>/ManualSync_CCMS.sqlite",
      (created / "ManualSync_CCMS.sqlite").is_file() and not (created / "ManualSync_CCMS_v18.sqlite").exists()
      and ProjectLayout(created).find_ccms_db() == created / "ManualSync_CCMS.sqlite", sorted(p.name for p in created.iterdir()))
health = project_lifecycle.project_health(created)
check("C-project_lifecycle.project_health reads the counts through the resolved database",
      (health["publications"], health["topics"], health["blocks"]) == (0, 0, 0), health)
rr = outcome(release_readiness.assess_release_readiness, P_NONE, "PUB")
check("C-release_readiness: a project without database still fails with FileNotFoundError and the same message",
      rr[:3] == ("raise", "FileNotFoundError", "Repository CCMS non trovato nel progetto"), rr)
import release_manager  # noqa: E402

rm = outcome(lambda d: release_manager.create_release_bundle(d, "PUB", target_langs=["en-US"], released_by="Layout Suite",
                                                             out_dir=TMP / "release_out"), P_NONE)
# Observed before CP3.2: create_release_bundle runs the readiness gate first, so a missing
# database surfaces with release_readiness's message, before release_manager's own lookup.
check("C-release_manager: a project without database still fails first in the readiness gate (same type and message)",
      rm[:3] == ("raise", "FileNotFoundError", "Repository CCMS non trovato nel progetto"), rm)

import app  # noqa: E402  (after test_isolation: resolves its data home inside the test home)

got = {k: outcome(app._ccms_db, d) for k, d in STATES.items()}
check("C-app._ccms_db: same path in every state, FileNotFoundError('Repository CCMS non trovato') when missing",
      all(got[k] == ("ok", legacy_lookup(STATES[k])) for k in ("current", "legacy", "both"))
      and got["none"][:3] == ("raise", "FileNotFoundError", "Repository CCMS non trovato"), got)
valid = {k: app._valid_project_dir(project(f"v_{k}", "ManualSync_Project.json", *f)) is not None for k, f in (
    ("none", ()), ("cur", ("ManualSync_CCMS.sqlite",)), ("leg", ("ManualSync_CCMS_v18.sqlite",)))}
check("C-app._valid_project_dir: a project needs the manifest and either database name",
      valid == {"none": False, "cur": True, "leg": True} and app._valid_project_dir(None) is None, valid)
check("C-app.DATA_HOME / DATA_ROOT: <home>/ManualSyncData and its Projects folder (public names kept)",
      app.DATA_HOME == test_isolation.TEST_HOME / pl.DATA_DIR_NAME
      and app.DATA_ROOT == test_isolation.TEST_HOME / pl.DATA_DIR_NAME / pl.PROJECTS_DIR_NAME, app.DATA_HOME)

# ============================================================ 5. no network, no stray files
check("N1: no network access attempted during the whole suite", not GUARD.snapshot(), GUARD.snapshot())

shutil.rmtree(TMP, ignore_errors=True)
check("T1: the temporary fixtures were removed", not TMP.exists(), TMP)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_PROJECT_LAYOUT.json").write_text(json.dumps({
    "suite": "project layout (CP3.2)", "tests": total, "pass": passed, "fail": total - passed,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
