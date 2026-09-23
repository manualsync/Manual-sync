"""Test isolation -- no test may touch the user's real ManualSyncData.

Proves the three layers described in real_data_guard.py, and proves each
one with a mutant: the defect is reintroduced and the same check must catch
it. Every attempt to touch data is made against a DECOY data folder built
here (a pointer, a project with a WAL-mode database, logs) -- never against
the real one, so a broken guard could not damage anything.

  S  static: every suite imports test_isolation FIRST; no suite builds a
     path to the real folder; the browser fixture refuses to run without
     isolation; both runners fingerprint the real folder
  R  runtime: this process runs in a temporary home; importing app here
     resolves nothing real
  M  mutants, in child processes against the decoy:
     M1 a test that tries every kind of touch is stopped by the guard
     M2 without the guard the same test does damage -- and the runner's
        fingerprint names every changed file
     M3 importing app BEFORE test_isolation is refused
     M4 a browser suite with its isolation removed stops before creating
        any ZZ_ project
     M5 a child process started with the real home is refused
     M6 a real project is only ever used as a copy
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import real_data_guard as rdg  # noqa: E402

results: list[dict] = []
WORK = Path(tempfile.mkdtemp(prefix="ms_isolation_", dir=str(test_isolation.TEST_HOME)))


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)[:600]})
    print(("PASS" if ok else "FAIL"), "|", name, "" if ok else f"| {str(detail)[:600]}")
    if not ok:
        raise AssertionError(f"{name}: {detail}")


# ---------------------------------------------------------------- helpers

PRODUCT_MODULES = {p.stem for p in BASE.glob("*.py")} - {"test_isolation", "real_data_guard"}


def isolation_problems(source: str) -> list[str]:
    """What the static rule sees wrong in one suite's source (empty = compliant)."""
    tree = ast.parse(source)
    imports = []
    for node in tree.body:                                   # module level, in order
        if isinstance(node, ast.Import):
            imports += [(node.lineno, a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module != "__future__":
            imports.append((node.lineno, (node.module or "").split(".")[0]))
    if not any(m == "test_isolation" for _, m in imports):
        return ["no `import test_isolation`"]
    first = imports[0]
    if first[1] != "test_isolation":
        return [f"line {first[0]}: `{first[1]}` is imported before test_isolation"]
    return []


def real_path_literals(source: str) -> list[int]:
    return [source[:m.start()].count("\n") + 1
            for m in re.finditer(r"""Path\.home\(\)\s*/\s*["']ManualSyncData""", source)]


def make_decoy(name: str) -> Path:
    """A fake user home whose ManualSyncData looks like the real one."""
    home = WORK / name
    data = home / "ManualSyncData"
    proj = data / "Projects" / "Demo_Reale"
    proj.mkdir(parents=True)
    (proj / "ManualSync_Project.json").write_text('{"project_name": "Demo reale"}', encoding="utf-8")
    db = sqlite3.connect(str(proj / "ManualSync_CCMS.sqlite"))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE publications (publication_id TEXT, title TEXT)")
    db.execute("INSERT INTO publications VALUES ('PUB-MAIN', 'Manuale reale')")
    db.commit()
    db.close()
    (data / "active_project.json").write_text(json.dumps({"project_dir": str(proj)}, indent=2), encoding="utf-8")
    (data / "Logs").mkdir()
    (data / "Logs" / "ManualSync_Desktop.log").write_text("[old] line\n", encoding="utf-8")
    (data / "MANUALSYNC_APP.log").write_text("[old] line\n", encoding="utf-8")
    return home


def plain_env(home: Path, protected: Path | None = None) -> dict:
    """The environment of a user's shell whose home is `home` (no test variables)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ERYLOS_")}
    drive, tail = os.path.splitdrive(str(home))
    env.update(USERPROFILE=str(home), HOME=str(home), HOMEDRIVE=drive, HOMEPATH=tail,
               PYTHONIOENCODING="utf-8", PYTHONPATH=str(BASE), PYTHONDONTWRITEBYTECODE="1")
    if protected is not None:
        env[rdg.ENV_PROTECTED] = str(protected)
    return env


def run_child(code: str, env: dict, timeout: int = 180) -> subprocess.CompletedProcess:
    script = WORK / f"child_{len(list(WORK.glob('child_*.py')))}.py"
    script.write_text(code, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], cwd=str(WORK), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


ATTEMPTS = r'''
import json, os, shutil, sqlite3, subprocess, sys
from pathlib import Path
# the (decoy) real folder: as test_isolation recorded it, or the home's own when there is no isolation
D = Path(os.environ.get("ERYLOS_PROTECTED_DATA_HOME") or Path(os.environ["USERPROFILE"]) / "ManualSyncData")
P = D / "Projects" / "Demo_Reale"
def attempts():
    yield "scrivere un file", lambda: (D / "nuovo.txt").write_text("x")
    yield "aggiungere al log", lambda: open(D / "MANUALSYNC_APP.log", "a").write("x")
    yield "riscrivere il puntatore", lambda: (D / "active_project.json").write_text("{}")
    yield "leggere il puntatore", lambda: (D / "active_project.json").read_text()
    yield "aprire il database in lettura", lambda: (lambda c: (c.execute("SELECT * FROM publications").fetchall(), c.close()))(sqlite3.connect(str(P / "ManualSync_CCMS.sqlite")))
    yield "creare un progetto ZZ_", lambda: (D / "Projects" / "ZZ_MUTANT").mkdir()
    yield "cancellare un file", lambda: (D / "Logs" / "ManualSync_Desktop.log").unlink()
    yield "rinominare un file", lambda: os.replace(P / "ManualSync_Project.json", P / "x.json")
    yield "cancellare un progetto", lambda: shutil.rmtree(P)
    yield "elencare la cartella", lambda: os.listdir(D)
'''


# ================================================================ S: static rules
try:
    suites = sorted(BASE.glob("regressione_*.py"))
    offenders = {f.name: isolation_problems(f.read_text(encoding="utf-8")) for f in suites}
    offenders = {k: v for k, v in offenders.items() if v}
    check(f"S1: all {len(suites)} regression suites import test_isolation FIRST, before any other import",
          len(suites) >= 60 and not offenders, offenders)
    collectors = [f.name for f in suites if re.search(r"""globals\(\)\.items\(\) if k\.startswith\(["']test_["']\)""",
                                                          f.read_text(encoding="utf-8"))]
    clash = [n for n in collectors if re.search(r"^import test_isolation\s*(#|$)", (BASE / n).read_text(encoding="utf-8"), re.M)]
    check(f"S1c: the {len(collectors)} suites that run every global named test_* import the module under an alias "
          "(a bare `test_isolation` global was collected as a test and crashed regressione_commercial_build)",
          len(collectors) >= 8 and not clash, clash)
    impact_src = (BASE / "regressione_browser_impact_center.py").read_text(encoding="utf-8")
    no_iso = impact_src.replace(next(ln for ln in impact_src.splitlines() if ln.startswith("import test_isolation")) + "\n", "")
    swapped = "import app\n" + impact_src
    check("S1b (mutant): the rule catches a suite with no isolation, and one that imports app first",
          isolation_problems(no_iso) == ["no `import test_isolation`"]
          and isolation_problems(swapped) == ["line 1: `app` is imported before test_isolation"],
          (isolation_problems(no_iso), isolation_problems(swapped)))
    lits = {f.name: real_path_literals(f.read_text(encoding="utf-8")) for f in suites}
    lits = {k: v for k, v in lits.items() if v}
    # the pre-fix 4E1 line, assembled so that this file does not itself contain it
    old_4e1 = "FIXTURE_DIR = Path.home() / " + '"ManualSync' + 'Data" / "Projects" / "4E1_Manual_Acceptance"'
    check("S2: no suite builds a path to the real ManualSyncData (the 4E1 suite uses a copy)",
          not lits and real_path_literals(old_4e1) == [1]
          and 'test_isolation.copy_real_project("4E1_Manual_Acceptance")'
          in (BASE / "regressione_authoring_conditions_variants_4e1.py").read_text(encoding="utf-8"), lits)
    fx_src = (BASE / "acceptance_fixture.py").read_text(encoding="utf-8")
    check("S3: the browser fixture demands isolation BEFORE acceptance_guard resolves its paths",
          0 < fx_src.index('real_data_guard.require_isolated("acceptance_fixture")')
          < fx_src.index("import acceptance_guard as ag"), "")
    check("S3b: the gates' app-server log lives in the test home and is closed before removal (it used to be "
          "left, empty, in the repository after every run)",
          "Path(os.environ.get(real_data_guard.ENV_TEST_HOME) or Path.home()) / f\".acceptance_server_{port}.log\"" in fx_src
          and fx_src.index("log_handle.close()") < fx_src.index("log.unlink(missing_ok=True)")
          and "Path(__file__).with_name(f\".acceptance_server_" not in fx_src, "")
    runner = (BASE / "ESEGUI_REGRESSIONE.ps1").read_text(encoding="utf-8")
    i_snap, i_loop = runner.index("'--snapshot', $RealBefore"), runner.index("foreach ($suite in $Suites)")
    i_verify = runner.index("'--verify', $RealBefore")
    check("S4: ESEGUI_REGRESSIONE fingerprints the real folder before the suites, re-checks after, and fails the sweep",
          i_snap < i_loop < i_verify and "$RealDataChanged = ($rc -ne 0)" in runner
          and "if ($RealDataChanged) {" in runner and "exit 1" in runner[runner.index("if ($RealDataChanged) {"):],
          "")
    build = (BASE / "BUILD_WINDOWS.ps1").read_text(encoding="utf-8")
    i_home = build.index("$env:USERPROFILE = $BuildTestHome")
    firsts = [build.index(s) for s in ("Step 'Guardia ambiente", "Step 'Regressione completa", "Step 'Commercial acceptance sorgente'",
                                       "Step 'Smoke test sorgente", "Step 'Test del runtime portabile")]
    check("S5: the build moves the home to a temporary one BEFORE its first test, and verifies the real data before PASS",
          build.index("--snapshot $RealDataBefore") < i_home < min(firsts)
          and build.index("--verify $RealDataBefore") < build.index("status='PASS'")
          and "GetFolderPath('UserProfile')) 'ManualSyncData\\Logs" not in build, "")
    parse = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "foreach ($f in @('ESEGUI_REGRESSIONE.ps1','BUILD_WINDOWS.ps1')) { $t=$null; $e=$null; "
         "[void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path '%s' $f), [ref]$t, [ref]$e); "
         "Write-Output ($f + '=' + $e.Count) }" % str(BASE)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    check("S6: both runners still parse (checked by PowerShell's own parser, nothing executed)",
          "ESEGUI_REGRESSIONE.ps1=0" in parse.stdout and "BUILD_WINDOWS.ps1=0" in parse.stdout, parse.stdout + parse.stderr)

    # ============================================================ R: this process
    real = Path(os.environ[rdg.ENV_PROTECTED])
    check("R1: this suite runs in a temporary home; the real folder is known, protected, and not the home",
          Path.home() == test_isolation.TEST_HOME and rdg.guard_installed() and rdg.is_protected(real)
          and not rdg.is_protected(Path.home()) and real.name == "ManualSyncData"
          and os.environ.get(rdg.ENV_ISOLATED) == "1", (Path.home(), real))
    check("R1b: the temporary home is a Windows profile skeleton, not an empty folder (headless Chrome does not "
          "start with an empty USERPROFILE -- found by the first protected browser run)",
          all((test_isolation.TEST_HOME / s).is_dir() for s in ("AppData/Local/Temp", "AppData/Roaming", "Desktop",
                                                               "Documents", "Downloads")), test_isolation.TEST_HOME)
    save_src = (BASE / "regressione_browser_save_4e3.py").read_text(encoding="utf-8")
    fail_path = save_src[save_src.index("        else:", save_src.index("json/version")):save_src.index('raise RuntimeError("Chrome DevTools endpoint never came up")')]
    check("S7: a Chrome that fails to start is killed with its whole tree -- an orphan made the runner wait forever",
          '"taskkill", "/PID", str(self.proc.pid), "/T", "/F"' in fail_path, fail_path[-300:])
    import app  # noqa: E402
    import acceptance_fixture as fx  # noqa: E402
    check("R2: app imported after isolation resolves only the test home -- no real project is opened",
          str(app.DATA_HOME).startswith(str(test_isolation.TEST_HOME)) and app.INITIAL_PROJECT == ""
          and str(fx.ag.DATA_HOME).startswith(str(test_isolation.TEST_HOME)) and fx._pointer_raw() is None,
          (app.DATA_HOME, app.INITIAL_PROJECT, fx.ag.DATA_HOME))

    # ============================================================ M1 / M2: the guard, and the fingerprint
    home1 = make_decoy("decoy_guarded")
    before1 = rdg.snapshot(home1 / "ManualSyncData")
    guarded = run_child("import test_isolation\n" + ATTEMPTS + r'''
import real_data_guard as g
out = {}
for name, fn in attempts():
    try:
        fn(); out[name] = "ESEGUITO"
    except g.RealDataAccessError:
        out[name] = "BLOCCATO"
print(json.dumps(out))
''', plain_env(home1))
    outcome = json.loads(guarded.stdout.strip().splitlines()[-1]) if guarded.returncode == 0 else {}
    after1 = rdg.compare(before1, rdg.snapshot(home1 / "ManualSyncData"))
    check("M1: with the guard, every kind of touch on the data folder is refused (10/10) and nothing changes",
          len(outcome) == 10 and set(outcome.values()) == {"BLOCCATO"} and after1 == [],
          (outcome, after1, guarded.stderr[-800:]))

    home2 = make_decoy("decoy_unguarded")
    before2 = rdg.snapshot(home2 / "ManualSyncData")
    unguarded = run_child(ATTEMPTS + r'''
out = {}
for name, fn in attempts():
    try:
        fn(); out[name] = "ESEGUITO"
    except Exception as e:
        out[name] = type(e).__name__
print(json.dumps(out))
''', plain_env(home2))
    outcome2 = json.loads(unguarded.stdout.strip().splitlines()[-1]) if unguarded.returncode == 0 else {}
    changes = rdg.compare(before2, rdg.snapshot(home2 / "ManualSyncData"))
    expected = {"FILE CREATO: nuovo.txt", "FILE MODIFICATO (contenuto): MANUALSYNC_APP.log",
                "FILE MODIFICATO (contenuto): active_project.json", r"FILE CANCELLATO: Logs\ManualSync_Desktop.log",
                r"CARTELLA CREATA: Projects\ZZ_MUTANT", r"CARTELLA CANCELLATA: Projects\Demo_Reale"}
    check("M2 (mutant): WITHOUT the guard the same test damages the folder -- and the runner's fingerprint "
          "names every change", outcome2.get("scrivere un file") == "ESEGUITO" and expected <= set(changes),
          (outcome2, changes))
    home2b = make_decoy("decoy_wal_only")
    before2b = rdg.snapshot(home2b / "ManualSyncData")
    reader = run_child(r'''
import os, sqlite3
from pathlib import Path
p = Path(os.environ["USERPROFILE"]) / "ManualSyncData" / "Projects" / "Demo_Reale" / "ManualSync_CCMS.sqlite"
c = sqlite3.connect(str(p)); c.execute("SELECT * FROM publications").fetchall(); c.close()
''', plain_env(home2b))
    changes2b = rdg.compare(before2b, rdg.snapshot(home2b / "ManualSyncData"))
    check("M2b (mutant): even a READ-ONLY query leaves a trace (the 21/09 incident) -- and the fingerprint sees it",
          reader.returncode == 0 and r"CARTELLA TOCCATA (file creati o cancellati al suo interno, anche solo temporanei): Projects\Demo_Reale"
          in changes2b and not any(c.startswith("FILE MODIFICATO (contenuto)") for c in changes2b), changes2b)
    snap = WORK / "snap.json"
    cli_before = subprocess.run([sys.executable, str(BASE / "real_data_guard.py"), "--snapshot", str(snap),
                                 "--root", str(home1 / "ManualSyncData")], capture_output=True, text=True)
    cli_clean = subprocess.run([sys.executable, str(BASE / "real_data_guard.py"), "--verify", str(snap),
                                "--root", str(home1 / "ManualSyncData")], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    (home1 / "ManualSyncData" / "MANUALSYNC_APP.log").write_text("changed", encoding="utf-8")
    cli_dirty = subprocess.run([sys.executable, str(BASE / "real_data_guard.py"), "--verify", str(snap),
                                "--root", str(home1 / "ManualSyncData")], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    check("M2c: the runner command exits 0 on an untouched folder and 1 on a touched one, naming the file",
          cli_before.returncode == 0 and cli_clean.returncode == 0 and cli_dirty.returncode == 1
          and "FILE MODIFICATO (contenuto): MANUALSYNC_APP.log" in cli_dirty.stdout,
          (cli_clean.stdout, cli_dirty.stdout))

    # ============================================================ M3: import order
    home3 = make_decoy("decoy_order")
    early = run_child("import app\nimport test_isolation\nprint('NOT REFUSED')\n", plain_env(home3), timeout=300)
    check("M3 (mutant): a suite that imports app BEFORE test_isolation is refused at run time",
          early.returncode != 0 and "IsolationError" in early.stderr and "imported AFTER app" in early.stderr
          and "NOT REFUSED" not in early.stdout, early.stderr[-600:])
    home3b = make_decoy("decoy_order_ok")
    before3b = rdg.snapshot(home3b / "ManualSyncData")
    right = run_child("import test_isolation\nimport app\nprint('INITIAL=' + repr(app.INITIAL_PROJECT))\n",
                      plain_env(home3b), timeout=300)
    check("M3b: in the right order, importing app on a machine WITH a real project opens nothing of it",
          right.returncode == 0 and "INITIAL=''" in right.stdout
          and rdg.compare(before3b, rdg.snapshot(home3b / "ManualSyncData")) == [],
          (right.stdout[-300:], right.stderr[-600:]))

    # ============================================================ M4: a browser suite without isolation
    home4 = make_decoy("decoy_browser")
    before4 = rdg.snapshot(home4 / "ManualSyncData")
    mutant_dir = WORK / "mutant_browser"
    mutant_dir.mkdir()
    (mutant_dir / "regressione_browser_impact_center_mutant.py").write_text(no_iso, encoding="utf-8")
    env4 = plain_env(home4)
    m4 = subprocess.run([sys.executable, str(mutant_dir / "regressione_browser_impact_center_mutant.py")],
                        cwd=str(BASE), env=env4, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=300)
    zz = [p.name for p in (home4 / "ManualSyncData" / "Projects").iterdir() if p.name.startswith("ZZ_")]
    check("M4 (mutant): the impact-center browser suite with its isolation removed stops at once -- no ZZ_ "
          "project, no pointer switch, no browser",
          m4.returncode != 0 and "refuses to run outside the test isolation" in m4.stderr and not zz
          and "fixture ready" not in m4.stdout and "app server started" not in m4.stdout
          and rdg.compare(before4, rdg.snapshot(home4 / "ManualSyncData")) == [],
          (m4.returncode, zz, m4.stdout[-300:], m4.stderr[-600:]))
    ok4 = run_child("import test_isolation\nimport acceptance_fixture as fx\nprint('FIXTURE HOME', fx.ag.DATA_HOME)\n",
                    plain_env(home4), timeout=300)
    check("M4b: the same fixture, with isolation in place, loads and points at the temporary home",
          ok4.returncode == 0 and "erylos_test_home_" in ok4.stdout, (ok4.stdout[-300:], ok4.stderr[-600:]))

    # ============================================================ M5: child processes
    home5 = make_decoy("decoy_child")
    before5 = rdg.snapshot(home5 / "ManualSyncData")
    child5 = run_child(r'''
import test_isolation, os, subprocess, sys, json
import real_data_guard as g
real_home = os.environ["ERYLOS_PROTECTED_DATA_HOME"].rsplit(os.sep, 1)[0]
env_real = dict(os.environ, USERPROFILE=real_home, HOME=real_home)
out = {}
try:
    subprocess.run([sys.executable, "-c", "pass"], env=env_real); out["con la home reale"] = "AVVIATO"
except g.RealDataAccessError:
    out["con la home reale"] = "BLOCCATO"
r = subprocess.run([sys.executable, "-c", "from pathlib import Path; print(Path.home())"], capture_output=True, text=True)
out["con l'ambiente ereditato"] = "AVVIATO" if r.returncode == 0 and "erylos_test_home_" in r.stdout else r.stdout
print(json.dumps(out))
''', plain_env(home5))
    out5 = json.loads(child5.stdout.strip().splitlines()[-1]) if child5.returncode == 0 else {}
    check("M5: a child process started with the real home is refused; the normal (inherited) one runs in the test home",
          out5 == {"con la home reale": "BLOCCATO", "con l'ambiente ereditato": "AVVIATO"}
          and rdg.compare(before5, rdg.snapshot(home5 / "ManualSyncData")) == [], (out5, child5.stderr[-600:]))

    # ============================================================ M6: a real project, only as a copy
    home6 = make_decoy("decoy_copy")
    before6 = rdg.snapshot(home6 / "ManualSyncData")
    child6 = run_child(r'''
import test_isolation, json, sqlite3
import real_data_guard as g
c = test_isolation.copy_real_project("Demo_Reale")
con = sqlite3.connect(str(c / "ManualSync_CCMS.sqlite"))
rows = con.execute("SELECT title FROM publications").fetchall()
con.close()   # an open handle would keep Windows from removing the test home at exit
out = {"copia": str(c), "titolo": rows[0][0], "assente": test_isolation.copy_real_project("NON_ESISTE")}
try:
    (test_isolation.PROTECTED_DATA_HOME / "active_project.json").read_text(); out["lettura dopo la copia"] = "ESEGUITA"
except g.RealDataAccessError:
    out["lettura dopo la copia"] = "BLOCCATA"
import shutil
try:
    shutil.copytree(c, test_isolation.PROTECTED_DATA_HOME / "Projects" / "ZZ_BACK"); out["copia verso i dati reali"] = "ESEGUITA"
except g.RealDataAccessError:
    out["copia verso i dati reali"] = "BLOCCATA"
print(json.dumps(out))
''', plain_env(home6))
    out6 = json.loads(child6.stdout.strip().splitlines()[-1]) if child6.returncode == 0 else {}
    check("M6: a real project is used as a byte copy in the test home; reading the original afterwards, or copying "
          "back into it, is refused -- and the original is untouched",
          out6.get("titolo") == "Manuale reale" and "erylos_test_home_" in out6.get("copia", "")
          and out6.get("assente") is None and out6.get("lettura dopo la copia") == "BLOCCATA"
          and out6.get("copia verso i dati reali") == "BLOCCATA"
          and rdg.compare(before6, rdg.snapshot(home6 / "ManualSyncData")) == [], (out6, child6.stderr[-600:]))
except BaseException as _crash:
    if not isinstance(_crash, AssertionError):
        results.append({"test": "suite interrupted by an unexpected error", "status": "FAIL",
                        "detail": f"{type(_crash).__name__}: {_crash}"[:600]})
    raise
finally:
    summary = {"suite": "Test isolation -- the real ManualSyncData is never touched", "tests": len(results),
               "pass": sum(r["status"] == "PASS" for r in results),
               "fail": sum(r["status"] == "FAIL" for r in results), "results": results}
    (BASE / "RISULTATO_TEST_ISOLATION.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("suite", "tests", "pass", "fail")}, ensure_ascii=False))
    shutil.rmtree(WORK, ignore_errors=True)
