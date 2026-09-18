"""Gate 1 — Production Build & Environment Hardening.

Pins the packaging contract, not the product. Every check here exists
because a specific way of shipping a broken package was possible before
this gate.

The failure this suite is really about
--------------------------------------
BUILD_WINDOWS.ps1 carried a hand-written list of files to copy into the
distribution. Between Gate 2A and Impact Center the product grew
twenty-one modules and a ``locales`` directory; the list grew nothing.
Two outcomes, and only one of them is safe:

* a missing MODULE stops the build. ``app.py`` cannot import, the smoke
  test refuses, no package is produced. Loud.
* a missing DATA directory does not. ``i18n.load_catalog`` degrades to an
  empty catalogue *by design*, so resource-check, headless smoke and
  commercial acceptance all pass. The customer installs successfully and
  every label reads ``nav.conformity``.

So the payload is derived (packaging_payload.py) and verified statically.
The tests below check that it stays derived: a returning hand-written
allowlist is itself the regression.

Run it on the project environment. It will tell you if you have not.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import environment_guard as eg  # noqa: E402
import packaging_payload as pp  # noqa: E402

BUILD = BASE / "BUILD_WINDOWS.ps1"
INSTALLER = BASE / "INSTALLA_MANUALSYNC.ps1"
VALIDATOR = BASE / "VERIFICA_INSTALLAZIONE_PILOTA.ps1"
UNINSTALLER = BASE / "DISINSTALLA_MANUALSYNC.ps1"

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))


# ==================================================================
# 1. Environment — the precondition for every other result
# ==================================================================

def test_environment_is_the_locked_one():
    problems = eg.environment_problems()
    check("this interpreter matches the lock", not problems, problems)
    check("the reference lock is the base one, not the runtime superset",
          eg.BASE_LOCK.name == "requirements.lock.txt", eg.BASE_LOCK.name)


def test_expected_packages_are_read_not_duplicated():
    """The guard used to restate seven versions the lock already pinned."""
    source = (BASE / "environment_guard.py").read_text(encoding="utf-8")
    literals = re.findall(r'"(?:gradio|pandas|numpy|PyMuPDF|Pillow|rapidfuzz)"\s*:\s*"[\d.]+"',
                          source)
    check("no pinned version is hard-coded in the guard", not literals, literals)
    from_lock = eg.expected_packages()
    declared = {}
    for raw in eg.BASE_LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and "==" in line and not line.startswith("#"):
            name, _, ver = line.partition("==")
            declared[name.strip()] = ver.strip()
    check("the guard's pins ARE the lock's pins", from_lock == declared,
          {"guard": from_lock, "lock": declared})


def test_guard_refuses_a_wrong_environment():
    """A guard that cannot fail protects nothing."""
    original = eg.EXPECTED_PACKAGES
    try:
        eg.EXPECTED_PACKAGES = dict(original, **{"gradio": "0.0.0-impossible"})
        problems = eg.environment_problems()
        check("a wrong pinned version is detected",
              any("gradio" in p for p in problems), problems)
        raised = False
        try:
            eg.assert_supported_environment()
        except RuntimeError:
            raised = True
        check("...and assert_supported_environment refuses", raised, "")
    finally:
        eg.EXPECTED_PACKAGES = original
    check("the real environment is still accepted afterwards",
          not eg.environment_problems(), eg.environment_problems())


# ==================================================================
# 2. The payload is derived from the code
# ==================================================================

# The entry points are established here from evidence OUTSIDE
# packaging_payload.py, so a mutation of that module cannot also move the
# goalposts this suite measures it against. The launcher executes
# app/manualsync_desktop.py; that module builds the UI from app and runs
# commercial_acceptance for --acceptance-smoke.
def _independent_entry_points() -> tuple[str, ...]:
    launcher = (BASE / "manualsync_launcher.py").read_text(encoding="utf-8")
    m = re.search(r'app\s*/\s*"([a-z_]+)\.py"', launcher)
    if not m:
        raise AssertionError("cannot find the script the launcher executes")
    return (m.group(1), "app", "commercial_acceptance")


def _independent_closure(entry_points) -> set[str]:
    """Written separately from packaging_payload.runtime_modules on purpose."""
    local = {p.stem for p in BASE.glob("*.py")}
    seen: set[str] = set()
    stack = list(entry_points)
    while stack:
        mod = stack.pop()
        if mod in seen or mod not in local:
            continue
        seen.add(mod)
        for node in ast.walk(ast.parse((BASE / f"{mod}.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                stack.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                stack.append(node.module.split(".")[0])
    return {f"{m}.py" for m in seen} - {"manualsync_launcher.py"}


def closure_problems(entries: set[str]) -> list[str]:
    needed = _independent_closure(_independent_entry_points())
    return sorted(needed - entries)


def locale_problems(entries: set[str]) -> list[str]:
    on_disk = {p.relative_to(BASE).as_posix() for p in (BASE / "locales").glob("*.json")}
    required = {"locales/it.json", "locales/en.json"} | on_disk
    return sorted(required - entries)


def test_payload_covers_every_runtime_import():
    """The exact defect: modules reachable from an entry point but unshipped."""
    check("the launcher's real entry point is manualsync_desktop",
          _independent_entry_points()[0] == "manualsync_desktop", _independent_entry_points())
    missing = closure_problems(set(pp.payload_entries()))
    check("every module reachable from an entry point is in the payload",
          not missing, missing)
    needed = _independent_closure(_independent_entry_points())
    check("the closure is not trivially small", len(needed) > 40, len(needed))
    check("no dynamic import escapes the static closure",
          not [m for m in pp.runtime_modules()
               if re.search(r"import_module\(|__import__\(|spec_from_file_location",
                            (BASE / m).read_text(encoding="utf-8"))], "")


_mutant_seq = 0


def _load_payload_mutant(old: str, new: str):
    """A mutated COPY of packaging_payload, under a unique module name.

    Same-length edits let Python reuse a cached .pyc if the file name is
    reused, which once made an earlier mutation suite pass vacuously; and
    the shipped module is never written to.
    """
    global _mutant_seq
    import importlib.util
    source = (BASE / "packaging_payload.py").read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation anchor not unique: {old!r}"
    _mutant_seq += 1
    name = f"_pp_mutant_{_mutant_seq}_{id(source)}"
    # The copy is needed only while it is being imported: once executed the
    # module lives in memory. It used to be left behind -- sixteen
    # ms_pp_mutant_* directories after a day of green runs.
    tmp = Path(tempfile.mkdtemp(prefix="ms_pp_mutant_"))
    path = tmp / f"{name}.py"
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
        shutil.rmtree(tmp, ignore_errors=True)
    mod.BASE = BASE          # the copy lives in a temp dir; point it back
    return mod


def test_mutation_payload_resolver_has_teeth():
    """Break packaging_payload three ways; each break must be caught."""
    m1 = _load_payload_mutant('RUNTIME_DATA_DIRS = ("locales",)', "RUNTIME_DATA_DIRS = ()")
    check("[mutant] dropping locales from the payload is DETECTED",
          bool(locale_problems(set(m1.payload_entries()))), "")

    # Shortening ENTRY_POINTS is NOT used as a mutant: manualsync_desktop
    # imports app and commercial_acceptance inside functions, which the AST
    # walk reaches anyway, so that mutant is equivalent and would "survive"
    # for a correct reason. Breaking the traversal itself is not equivalent.
    m3 = _load_payload_mutant(
        "            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:\n"
        "                stack.append(node.module.split(\".\")[0])",
        "            elif False:\n                pass")
    missing3 = closure_problems(set(m3.payload_entries()))
    check("[mutant] a resolver that ignores `from x import y` is DETECTED",
          bool(missing3), missing3[:5])
    check("[mutant] ...and the report names real modules",
          any(x.endswith(".py") for x in missing3), missing3[:3])
    check("(control) the unmutated resolver passes the same checks",
          not closure_problems(set(pp.payload_entries()))
          and not locale_problems(set(pp.payload_entries())), "")


def test_staged_payload_imports_on_its_own():
    """Import the product FROM A STAGED PAYLOAD, not from the source tree.

    A static closure proves the list; this proves the list is enough to
    run. The staged directory is the only source on sys.path, which is
    how dist/ManualSync/app is laid out on a customer machine.
    """
    # Remove EXACTLY the source directory from sys.path, compared as resolved
    # paths. The first version dropped every entry whose text contained the
    # product folder name -- which also removed the build's own site-packages,
    # because BUILD_WINDOWS.ps1 creates .buildvenv INSIDE that folder. It
    # passed on the developer venv and failed the real build with
    # "No module named 'gradio'": the test was measuring where the
    # interpreter lived, not what the payload contained.
    probe = (
        "import sys, json\n"
        "from pathlib import Path\n"
        "_source = Path(sys.argv[1]).resolve()\n"
        "sys.path = [p for p in sys.path if Path(p or '.').resolve() != _source]\n"
        "import app, manualsync_desktop, commercial_acceptance, i18n\n"
        "print(json.dumps({'app_file': app.__file__, "
        "'catalog_it': len(i18n.catalog_keys('it')), "
        "'catalog_en': len(i18n.catalog_keys('en'))}))\n")
    env = dict(__import__("os").environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    with tempfile.TemporaryDirectory(prefix="ms_stage_import_") as td:
        stage = Path(td) / "app"
        for rel in pp.payload_entries():
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE / rel, dst)

        def run_probe():
            return subprocess.run([sys.executable, "-c", probe, str(BASE)], cwd=str(stage), env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=300)

        proc = run_probe()
        check("the staged payload imports app, desktop shell and acceptance",
              proc.returncode == 0, proc.stderr[-400:])
        info = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
        check("...from the staged copy, not the source tree",
              str(info.get("app_file", "")).startswith(str(stage)), info.get("app_file"))
        expected = len(__import__("i18n").catalog_keys("it"))
        check("...with the full Italian catalogue loaded",
              info.get("catalog_it") == expected, (info.get("catalog_it"), expected))
        check("...and the full English catalogue",
              info.get("catalog_en") == expected, (info.get("catalog_en"), expected))

        # Mutation 1: no locales. The import SUCCEEDS -- which is the whole
        # reason the static check exists -- but the catalogue is empty.
        shutil.rmtree(stage / "locales")
        proc = run_probe()
        info = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
        check("[mutant] without locales/ the product STILL imports (silent failure mode)",
              proc.returncode == 0, proc.stderr[-300:])
        check("[mutant] ...with an empty catalogue: only the static check can catch this",
              info.get("catalog_it") == 0, info.get("catalog_it"))

        # Mutation 2: a missing module fails the import outright.
        (stage / "impact_center.py").unlink()
        proc = run_probe()
        check("[mutant] without impact_center.py the import FAILS",
              proc.returncode != 0 and "impact_center" in proc.stderr, proc.stderr[-300:])


def test_payload_includes_the_modules_this_gate_was_written_for():
    entries = set(pp.payload_entries())
    for mod in ("i18n.py", "domain_errors.py", "impact_center.py",
                "impact_center_view.py", "impact_intelligence.py", "impact_view.py",
                "translation_intelligence.py", "translation_view.py",
                "authoring_service.py", "authoring_view.py",
                "compliance_view.py", "compliance_intelligence.py",
                "compliance_starter_packs.py", "rich_text.py",
                "publishing_output.py", "publishing_preview.py",
                "publishing_preflight.py"):
        check(f"payload ships {mod}", mod in entries, sorted(entries)[:3])


def test_payload_includes_the_locale_catalogues():
    problems = locale_problems(set(pp.payload_entries()))
    check("every locale catalogue on disk is in the payload", not problems, problems)


def test_payload_excludes_what_must_not_ship():
    entries = set(pp.payload_entries())
    check("no regression suite is packaged",
          not [e for e in entries if e.startswith("regressione_")], "")
    check("the frozen launcher source is not packaged too",
          "manualsync_launcher.py" not in entries,
          "PyInstaller already freezes it into ManualSync.exe")
    check("the build-time payload resolver is not packaged",
          "packaging_payload.py" not in entries, "")
    check("the separate engineering app is not packaged",
          "app_engineering.py" not in entries, "")


def test_every_declared_entry_exists():
    check("every payload entry exists in this checkout",
          not pp.missing_sources(), pp.missing_sources())


def test_verify_detects_an_incomplete_payload():
    """Mutation: a payload missing locales must be REFUSED.

    This is the case no runtime smoke test can catch, so if this check
    ever passes vacuously the gate is worthless.
    """
    with tempfile.TemporaryDirectory(prefix="ms_payload_probe_") as td:
        stage = Path(td) / "app"
        stage.mkdir(parents=True)
        for rel in pp.payload_entries():
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE / rel, dst)
        check("a complete payload verifies", not pp.verify(stage), pp.verify(stage))

        shutil.rmtree(stage / "locales")
        gaps = pp.verify(stage)
        check("removing locales/ is DETECTED", bool(gaps), gaps)
        check("...and named in the report",
              any(g.startswith("locales/") for g in gaps), gaps)

        (stage / "impact_center.py").unlink()
        gaps = pp.verify(stage)
        check("removing a module is DETECTED", "impact_center.py" in gaps, gaps)


def test_cli_contract_used_by_the_build():
    for args, label in ((["--print"], "--print"), (["--explain"], "--explain")):
        proc = subprocess.run([sys.executable, str(BASE / "packaging_payload.py"), *args],
                              capture_output=True, text=True, encoding="utf-8")
        check(f"packaging_payload.py {label} exits 0", proc.returncode == 0, proc.stderr[:200])
    proc = subprocess.run(
        [sys.executable, str(BASE / "packaging_payload.py"), "--print"],
        capture_output=True, text=True, encoding="utf-8")
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    check("--print emits one path per line, matching the API",
          sorted(lines) == pp.payload_entries(), len(lines))
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [sys.executable, str(BASE / "packaging_payload.py"), "--verify", td],
            capture_output=True, text=True, encoding="utf-8")
        check("--verify exits non-zero on an empty directory", proc.returncode != 0,
              proc.returncode)


def test_installed_resource_check_covers_the_catalogues():
    """--resource-check runs on the customer machine, after installation.

    The static payload check protects the BUILD. This protects an
    INSTALLATION that was damaged afterwards -- the one place where a
    missing catalogue would otherwise still start cleanly.
    """
    import manualsync_desktop as md
    lines = md.packaged_resource_check()
    for lang in ("it", "en"):
        hit = [l for l in lines if l.startswith(f"locales/{lang}.json")]
        check(f"resource check reports locales/{lang}.json", bool(hit), lines[:3])
        check(f"...and finds it in this checkout", bool(hit) and "exists=True" in hit[0], hit)

    original = md.resource_path
    with tempfile.TemporaryDirectory(prefix="ms_no_locales_") as td:
        md.resource_path = lambda name: Path(td) / name
        try:
            damaged = md.packaged_resource_check()
        finally:
            md.resource_path = original
    missing = [l for l in damaged if l.startswith("locales/") and "exists=False" in l]
    check("an installation without locales/ is reported as missing",
          len(missing) == 2, missing)
    check("...in the exact form run_resource_check fails on",
          all("exists=False" in l for l in missing), missing)


# ==================================================================
# 3. The build script uses the derived payload, not a list
# ==================================================================

def _code_only(ps: str) -> str:
    """PowerShell with comment lines removed.

    Positional checks must compare EXECUTED lines. An earlier version of
    the guard-ordering check matched a comment that merely named a suite
    and passed on position rather than on meaning.
    """
    return "\n".join(l for l in ps.splitlines() if not l.lstrip().startswith("#"))


def build_script_problems(build: str) -> list[str]:
    code = _code_only(build)
    problems: list[str] = []
    if re.search(r"\$RuntimeFiles\s*=\s*@\(", code):
        problems.append("hand-written $RuntimeFiles allowlist is back")
    if "packaging_payload.py') --print" not in code:
        problems.append("payload not derived from packaging_payload --print")
    if "packaging_payload.py') --verify" not in code:
        problems.append("no static payload verification")
    elif "--resource-check" in code and code.index("--verify") > code.index("--resource-check"):
        problems.append("payload verified only after a smoke test could mask the gap")
    if "assert_supported_environment" not in code:
        problems.append("environment not asserted")
    elif "-Filter 'regressione_*.py'" in code and \
            code.index("assert_supported_environment") > code.index("-Filter 'regressione_*.py'"):
        problems.append("environment asserted after the regression suites")
    if "-Filter 'regressione_*.py'" not in code:
        problems.append("regression suites not discovered")
    if re.search(r"Join-Path \$Root 'regressione_[a-z0-9_]+\.py'\)", code):
        problems.append("a regression suite is listed by name again")
    if re.search(r"^\s*& \$BuildPython \$suite", code, re.M):
        problems.append("suites run via & (NativeCommandError kills the loop under redirection)")
    return problems


def version_problems(text: str, literal: str, *, must_derive: bool = True) -> list[str]:
    code = _code_only(text)
    problems = []
    if literal in code:
        problems.append(f"build id literal {literal!r} restated")
    if must_derive and "PRODUCT_BUILD" not in code:
        problems.append("build id not derived from product_version.py")
    return problems


def test_build_script_contract():
    problems = build_script_problems(BUILD.read_text(encoding="utf-8"))
    check("BUILD_WINDOWS.ps1 meets the packaging contract", not problems, problems)
    build = BUILD.read_text(encoding="utf-8")
    check("browser suites are excluded explicitly",
          "regressione_browser_impact_center.py" in build, "")
    check("a suspiciously short suite list fails the build",
          "Elenco suite sospetto" in build, "")


def test_mutation_build_script_contract_has_teeth():
    """Each defect this gate removed, re-injected into a copy of the script."""
    real = BUILD.read_text(encoding="utf-8")
    code = real

    def mutated(label, text):
        problems = build_script_problems(text)
        check(f"[mutant] {label} is DETECTED", bool(problems), label)

    mutated("a returning $RuntimeFiles allowlist",
            code.replace("$PayloadList = & $BuildPython",
                         "$RuntimeFiles = @('app.py')\n$PayloadList = & $BuildPython", 1))
    verify_line = next(l for l in code.splitlines() if "packaging_payload.py') --verify" in l)
    mutated("a removed static --verify step", code.replace(verify_line, "", 1))
    guard_line = next(l for l in code.splitlines()
                      if "assert_supported_environment" in l and not l.lstrip().startswith("#"))
    moved = code.replace(guard_line + "\n", "", 1)
    moved = moved.replace("Write-Host \"Regressione completa:",
                          guard_line + "\nWrite-Host \"Regressione completa:", 1)
    mutated("the guard moved after the suites", moved)
    mutated("a suite listed by name again",
            code.replace("Step 'Commercial acceptance sorgente'",
                         "& $BuildPython (Join-Path $Root 'regressione_v23.py')\n"
                         "Step 'Commercial acceptance sorgente'", 1))
    mutated("suites executed with & again",
            code.replace("$rc = Invoke-PythonLogged $BuildPython @($suite.FullName)",
                         "& $BuildPython $suite.FullName", 1))
    check("(control) the real script produces no problem", not build_script_problems(real), "")
    check("(control) a commented-out allowlist is NOT a problem",
          not build_script_problems(real + "\n# $RuntimeFiles = @('app.py')\n"), "")


# ==================================================================
# 4. One build id
# ==================================================================

def test_build_id_has_a_single_source():
    from product_version import PRODUCT_BUILD
    for path in (BUILD, INSTALLER, VALIDATOR):
        problems = version_problems(path.read_text(encoding="utf-8"), PRODUCT_BUILD)
        check(f"{path.name} derives the build id and never restates it", not problems, problems)
    for path in (UNINSTALLER, BASE / "LEGGIMI_INSTALLAZIONE_PILOTA.txt",
                 BASE / "ESEGUI_REGRESSIONE.ps1", BASE / "VERIFICA_CICLO_INSTALLAZIONE.ps1"):
        problems = version_problems(path.read_text(encoding="utf-8"), PRODUCT_BUILD,
                                    must_derive=False)
        check(f"{path.name} does not restate the build id", not problems, problems)


def test_mutation_version_check_has_teeth():
    from product_version import PRODUCT_BUILD
    installer = INSTALLER.read_text(encoding="utf-8")
    restated = installer.replace("build = (Get-ProductBuildId (Join-Path $InstallDir 'app'))",
                                 f"build = '{PRODUCT_BUILD}'", 1)
    check("[mutant] a restated build id in the installer is DETECTED",
          bool(version_problems(restated, PRODUCT_BUILD)), "")
    no_derivation = "\n".join(l for l in installer.splitlines() if "PRODUCT_BUILD" not in l)
    check("[mutant] an installer that stopped deriving the id is DETECTED",
          bool(version_problems(no_derivation, PRODUCT_BUILD)), "")
    check("(control) a comment quoting the id is NOT a restatement",
          not version_problems(installer + f"\n# e.g. {PRODUCT_BUILD}\n", PRODUCT_BUILD), "")


def test_runners_cannot_be_killed_by_a_traceback():
    """Both suite runners go through Start-Process with redirected streams."""
    for path in (BASE / "ESEGUI_REGRESSIONE.ps1", BUILD):
        code = _code_only(path.read_text(encoding="utf-8"))
        check(f"{path.name} defines Invoke-PythonLogged",
              "function Invoke-PythonLogged" in code, "")
        check(f"{path.name} runs suites through it",
              "Invoke-PythonLogged $" in code and "@($suite.FullName)" in code, "")
        check(f"{path.name} never runs a suite with a bare &",
              not re.search(r"^\s*& \$\w*Python \$suite", code, re.M), "")
    runner = _code_only((BASE / "ESEGUI_REGRESSIONE.ps1").read_text(encoding="utf-8"))
    check("the day-to-day runner asserts the environment before any suite",
          runner.index("assert_supported_environment") < runner.index("foreach ($suite"), "")
    check("the day-to-day runner uses the project interpreter, not PATH",
          ".venv\\Scripts\\python.exe" in runner, "")


def test_new_and_installer_scripts_are_ascii():
    """Windows PowerShell 5.1 reads a .ps1 without BOM as ANSI, not UTF-8.

    None of these scripts has a BOM. A non-ASCII character in one of them
    is silently re-decoded: in VERIFICA_CICLO_INSTALLAZIONE.ps1 an "a
    grave" inside the generated Python probe would have become two
    characters, and the probe would have reported the installed UI as
    untranslated. BUILD_WINDOWS.ps1 is excluded: it predates this gate and
    carries em dashes in six Step labels, which only affect log cosmetics.
    """
    for name in ("INSTALLA_MANUALSYNC.ps1", "VERIFICA_INSTALLAZIONE_PILOTA.ps1",
                 "DISINSTALLA_MANUALSYNC.ps1", "ESEGUI_REGRESSIONE.ps1",
                 "VERIFICA_CICLO_INSTALLAZIONE.ps1"):
        raw = (BASE / name).read_bytes()
        bad = [i + 1 for i, line in enumerate(raw.splitlines()) if any(b > 127 for b in line)]
        check(f"{name} is pure ASCII (read as ANSI by PowerShell 5.1)", not bad, bad[:5])
    build = BUILD.read_text(encoding="utf-8")
    added_labels = ("Guardia ambiente", "Regressione completa", "payload derivato")
    offending = [l for l in build.splitlines()
                 if any(a in l for a in added_labels) and any(ord(c) > 127 for c in l)]
    check("Step labels added by Gate 1 add no non-ASCII to the build log", not offending, offending)


def manifest_path_problems(ps: str) -> list[str]:
    """Wildcard Test-Path on a manifest-derived $path is the defect."""
    code = _code_only(ps)
    problems = []
    if re.search(r"Test-Path\s+\$path\b", code):
        problems.append("Test-Path $path without -LiteralPath")
    if "Test-Path -LiteralPath $path" not in code:
        problems.append("manifest loop does not test paths literally")
    return problems


def test_manifest_checks_use_literal_paths():
    """The first real Windows build of this candidate stopped here.

    python-docx ships docx/templates/default-docx-template/[Content_Types].xml.
    For Test-Path without -LiteralPath, [ and ] are a wildcard character
    class, so a file that exists reports as absent. The same line was in
    the build's ZIP validation, the installer's manifest check and the
    pilot validator: the installer would have refused to install on the
    customer's machine. It never surfaced before because the build had
    never got this far -- the missing modules stopped it earlier.
    """
    import importlib.util as _u
    docx_root = Path(_u.find_spec("docx").origin).parent
    bracketed = docx_root / "templates" / "default-docx-template" / "[Content_Types].xml"
    check("(fact) the locked runtime really ships a file with [ ] in its name",
          bracketed.exists(), bracketed)

    for path in (BUILD, INSTALLER, VALIDATOR):
        problems = manifest_path_problems(path.read_text(encoding="utf-8"))
        check(f"{path.name} checks manifest entries with -LiteralPath", not problems, problems)

    installer = INSTALLER.read_text(encoding="utf-8")
    mutant = installer.replace("Test-Path -LiteralPath $path", "Test-Path $path", 1)
    check("[mutant] a wildcard Test-Path in the installer is DETECTED",
          bool(manifest_path_problems(mutant)), "")

    # Behaviour, not text: ask PowerShell itself.
    with tempfile.TemporaryDirectory(prefix="ms_literal_") as td:
        target = Path(td) / "[Content_Types].xml"
        target.write_text("<x/>", encoding="utf-8")
        ps = (f"$p = '{target}'; "
              "Write-Output ((Test-Path $p).ToString() + ',' + (Test-Path -LiteralPath $p).ToString())")
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        answer = proc.stdout.strip()
        check("(behaviour) PowerShell's plain Test-Path misses a [bracketed] file",
              answer.startswith("False,"), answer)
        check("(behaviour) -LiteralPath finds it", answer.endswith(",True"), answer)


def test_install_cycle_script_is_not_shipped():
    build = _code_only(BUILD.read_text(encoding="utf-8"))
    stage_list = re.search(r"foreach \(\$name in @\(([^)]*)\)\)", build)
    check("the installer stage file list is found", stage_list is not None, "")
    check("VERIFICA_CICLO_INSTALLAZIONE.ps1 is a development tool, not shipped",
          stage_list and "VERIFICA_CICLO_INSTALLAZIONE" not in stage_list.group(1), "")
    check("ESEGUI_REGRESSIONE.ps1 is not shipped either",
          stage_list and "ESEGUI_REGRESSIONE" not in stage_list.group(1), "")
    check("neither is in the application payload",
          not {"VERIFICA_CICLO_INSTALLAZIONE.ps1", "ESEGUI_REGRESSIONE.ps1"}
          & set(pp.payload_entries()), "")


def test_build_id_is_readable_the_way_powershell_reads_it():
    """The scripts parse product_version.py with this exact regex."""
    source = (BASE / "product_version.py").read_text(encoding="utf-8")
    m = re.search(r"""PRODUCT_BUILD\s*=\s*["']([^"']+)["']""", source)
    from product_version import PRODUCT_BUILD
    check("the regex the scripts use finds the build id", m is not None, "")
    check("...and finds the same value Python imports",
          m and m.group(1) == PRODUCT_BUILD, m.group(1) if m else None)


def test_version_file_agrees_with_the_module():
    from product_version import PRODUCT_BUILD
    version_txt = (BASE / "VERSION.txt").read_text(encoding="utf-8")
    check("VERSION.txt states the same build as product_version.py",
          PRODUCT_BUILD in version_txt, version_txt.splitlines()[:2])


# ==================================================================
# 5. Installer contract preserved
# ==================================================================

def test_installer_contract_intact():
    installer = INSTALLER.read_text(encoding="utf-8")
    validator = VALIDATOR.read_text(encoding="utf-8")
    uninstaller = UNINSTALLER.read_text(encoding="utf-8")
    check("installer validates the payload manifest",
          "Assert-PayloadManifest" in installer, "")
    check("installer prepares a rollback", "rollback" in installer.lower(), "")
    check("installer runs the acceptance smoke",
          "--acceptance-smoke" in installer, "")
    check("validator hashes the installed payload",
          "Get-FileHash" in validator, "")
    check("validator runs all three packaged switches",
          all(sw in validator for sw in ("--resource-check", "--headless-smoke",
                                         "--acceptance-smoke")), "")
    check("uninstaller preserves project data",
          "NON sono stati eliminati" in uninstaller, "")


def test_installer_compares_two_independent_sources():
    """The build-id check must compare, not restate.

    Before this gate the validator compared the installer's recorded build
    against a literal copied from the same release notes -- two copies of
    one string, which proves nothing.
    """
    validator = VALIDATOR.read_text(encoding="utf-8")
    check("the validator reads the build id from the installed payload",
          "Get-ProductBuildId" in validator, "")
    check("...and compares it against INSTALLATION.json",
          "$meta.build" in validator and "$ExpectedBuild" in validator, "")


def _block(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i) + len(end)]


def test_sandbox_resolver_is_identical_in_every_script():
    """The three scripts ship separately, so the resolver is duplicated.

    Duplication is the existing idiom here (process detection already is),
    but a duplicate that drifts is how an installer and its validator end
    up looking in different places. This makes drift a test failure.
    """
    blocks = {}
    for path in (INSTALLER, VALIDATOR, UNINSTALLER):
        text = path.read_text(encoding="utf-8")
        check(f"{path.name} accepts -SandboxRoot",
              "param([string]$SandboxRoot = '')" in text, "")
        blocks[path.name] = _block(text, "if ($SandboxRoot) {",
                                   "$DesktopRoot = [Environment]::GetFolderPath('Desktop')\n}")
    check("the sandbox resolver is byte-identical in all three scripts",
          len(set(blocks.values())) == 1, list(blocks))
    reader = {}
    for path in (INSTALLER, VALIDATOR):
        reader[path.name] = _block(path.read_text(encoding="utf-8"),
                                   "function Get-ProductBuildId", "return $m.Groups[1].Value\n}")
    check("Get-ProductBuildId is byte-identical in installer and validator",
          len(set(reader.values())) == 1, list(reader))


def test_default_behaviour_still_targets_the_real_profile():
    """Without -SandboxRoot -- how the customer's .cmd calls it -- nothing moves."""
    for path in (INSTALLER, VALIDATOR, UNINSTALLER):
        text = path.read_text(encoding="utf-8")
        default = _block(text, "} else {", "$DesktopRoot = [Environment]::GetFolderPath('Desktop')")
        for token in ("$env:LOCALAPPDATA", "$env:USERPROFILE", "$env:APPDATA"):
            check(f"{path.name} default branch uses {token}", token in default, "")
        outside = text.replace(_block(text, "if ($SandboxRoot) {",
                                      "$DesktopRoot = [Environment]::GetFolderPath('Desktop')\n}"), "")
        leaks = [t for t in ("$env:LOCALAPPDATA", "$env:APPDATA",
                             "GetFolderPath('Desktop')") if t in outside]
        check(f"{path.name} resolves no profile path outside the resolver", not leaks, leaks)
    for name in ("INSTALLA_MANUALSYNC.cmd", "VERIFICA_INSTALLAZIONE_PILOTA.cmd",
                 "DISINSTALLA_MANUALSYNC.cmd"):
        cmd = (BASE / name).read_text(encoding="utf-8")
        check(f"{name} (what the customer runs) passes no sandbox",
              "SandboxRoot" not in cmd, "")


def test_uninstaller_does_not_claim_success_it_did_not_achieve():
    text = UNINSTALLER.read_text(encoding="utf-8")
    check("the uninstaller checks what is left after removal",
          "$Leftover" in text and "Test-Path" in text, "")
    check("...before printing success",
          text.index("$Leftover") < text.index("'ManualSync disinstallato.'"), "")
    check("...and exits non-zero when something could not be removed",
          "exit 1" in _block(text, "if ($Leftover.Count -gt 0) {", "exit 1"), "")


def run():
    fns = sorted((v for k, v in globals().items() if k.startswith("test_")),
                 key=lambda f: f.__code__.co_firstlineno)
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            check(f"{fn.__name__} raised", False, f"{type(exc).__name__}: {exc}")
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("=" * 78)
    print("REGRESSIONE — COMMERCIAL BUILD (Gate 1)")
    print("=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print(f"\n  {passed}/{total} passed")
    print("=" * 78)
    out = {"tests": total, "pass": passed, "fail": total - passed,
           "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d}
                       for n, ok, d in _results]}
    (BASE / "RISULTATO_COMMERCIAL_BUILD.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
