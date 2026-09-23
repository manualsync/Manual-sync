"""Commercial Ready — export hardening: no temporary residue.

What was wrong
--------------
Measured on a development machine before this change:

* 210 ``manualsync_v25_project_archive_*`` directories in %TEMP%. Every
  project backup from the UI -- and every ``--acceptance-smoke`` of the
  installed product -- left one behind.
* ~100 ``ms_html_zip_*`` directories: every HTML export left its ZIP.
* ``export_project_archive`` left a temporary directory AND a
  successful-looking archive behind when it failed half way, and opening
  the ZIP for writing truncated a previous good archive with the same name
  before the new one existed.

What this suite proves
----------------------
Every assertion counts directories in a PRIVATE temp root
(``tempfile.tempdir`` redirected), so it measures exactly what the code
under test creates and never touches the user's real %TEMP%.

* a successful export leaves at most the bounded number of intermediate
  copies while the app runs, and none after the process exits;
* a failed export leaves nothing: no temp directory, no partial archive,
  and a previous archive is intact;
* the HTML ZIP a live download link points to is never deleted;
* test helpers themselves leave no directory behind.

Mutation controls show the measurements can fail.
"""
from __future__ import annotations
import test_isolation as _isolation  # noqa: E402,F401 -- aliased: this suite collects every global named test_* as a test. FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# Private temp root BEFORE importing anything that may create temp dirs.
_REAL_TEMPDIR = tempfile.gettempdir()
PRIVATE = Path(tempfile.mkdtemp(prefix="ms_export_cleanup_suite_"))
tempfile.tempdir = str(PRIVATE)

import authoring_view as av  # noqa: E402
import export_links  # noqa: E402
import publishing_output as po  # noqa: E402
import temp_artifacts  # noqa: E402
import workflow_management as wm  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

ARCHIVE_PREFIX = "manualsync_v25_project_archive_"
HTML_ZIP_PREFIX = "ms_html_zip_"
HTML_STAGING_PREFIX = "ms_html_export_"

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))


def dirs(prefix: str) -> list[Path]:
    return sorted(p for p in PRIVATE.iterdir() if p.is_dir() and p.name.startswith(prefix))


DATA = PRIVATE / "data"
DATA.mkdir()


def new_project(name: str) -> Path:
    project = create_project(DATA, project_name=name, source_lang="it-IT")
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("T1", "Installazione", "corpo", status="Approved")
        repo.create_publication("PUB-MAIN", "Manuale Pompa PC-200", ["T1"])
    finally:
        repo.close()
    repo, svc = av.open_service(str(project))
    try:
        svc.create_condition_dimension("market", "Mercato", actor="Suite")
        svc.create_condition_value("eu", "market", "EU", actor="Suite")
        svc.create_variant_profile("PROF-EU", "EU Service", {"market": "eu"}, actor="Suite")
        svc.create_block_in_topic("T1", "paragraph", actor="Suite",
                                  text="Scollegare l'alimentazione prima dell'intervento.")
    finally:
        repo.close()
    return project


# ``import app`` builds the Gradio Blocks: done once, after the fixtures.
PROJECT = new_project("Temp_Cleanup")
import app  # noqa: E402


# ==================================================================
# 1. Project backup (export_project_archive / export_project_ui)
# ==================================================================

def test_backup_success_is_usable_and_bounded():
    before = len(dirs(ARCHIVE_PREFIX))
    paths = [Path(app.export_project_ui(str(PROJECT))) for _ in range(3)]
    check("each backup exists when it is handed to the UI", paths[-1].is_file(), paths[-1])
    names: list[str] = []
    if zipfile.is_zipfile(paths[-1]):
        with zipfile.ZipFile(paths[-1]) as zf:  # closed: an open handle blocks deletion on Windows
            names = zf.namelist()
    check("the backup is a real, restorable archive",
          "MANUALSYNC_PROJECT_MANIFEST.json" in names, names[:3])
    restored = wm.WorkflowManager.restore_project_archive(paths[-1], DATA / "restored_ok")
    check("...and restores transactionally", (Path(restored) / "ManualSync_CCMS.sqlite").is_file(),
          restored)
    check("three backups leave ONE temp directory, not three",
          len(dirs(ARCHIVE_PREFIX)) - before == 1, [d.name for d in dirs(ARCHIVE_PREFIX)])
    check("the one kept is the latest", paths[-1].parent in dirs(ARCHIVE_PREFIX), paths[-1].parent)
    check("older copies were removed", not paths[0].exists() and not paths[1].exists(), "")
    check("the backup keeps its final name and location",
          paths[-1].name.startswith("ManualSync_") and paths[-1].name.endswith("_PROJECT.zip")
          and paths[-1].parent.name.startswith(ARCHIVE_PREFIX), paths[-1])
    check("no .partial file anywhere after success",
          not list(PRIVATE.rglob("*.partial")), list(PRIVATE.rglob("*.partial"))[:3])


def test_backup_to_caller_directory_is_unchanged():
    target = DATA / "Backups"
    before = {d.name for d in dirs(ARCHIVE_PREFIX)}
    archive = export_with(wm.WorkflowManager, target)
    check("with an explicit out_dir the archive lands exactly there",
          archive.parent == target and archive.is_file(), archive)
    check("...and no temp directory is created for it",
          {d.name for d in dirs(ARCHIVE_PREFIX)} == before, "")


def export_with(manager_cls, out_dir=None):
    """Export and ALWAYS close the manager.

    An unclosed WorkflowManager keeps ManualSync_Workflow.sqlite open, and
    Windows refuses to delete an open file: the first version of this suite
    passed every check and then left its own temp root behind because of it.
    """
    mgr = manager_cls(PROJECT)
    try:
        return mgr.export_project_archive(out_dir)
    finally:
        mgr.close()


def _failing_write(fail_after: int):
    real_write = zipfile.ZipFile.write
    state = {"n": 0}

    def write(self, *a, **k):
        state["n"] += 1
        if state["n"] > fail_after:
            raise OSError("disco pieno (simulato)")
        return real_write(self, *a, **k)
    return real_write, write


def test_backup_failure_while_zipping_leaves_nothing():
    before = {d.name for d in dirs(ARCHIVE_PREFIX)}
    tracked_before = temp_artifacts.tracked("project_backup")
    real, failing = _failing_write(fail_after=1)
    zipfile.ZipFile.write = failing
    raised = False
    try:
        app.export_project_ui(str(PROJECT))
    except OSError:
        raised = True
    finally:
        zipfile.ZipFile.write = real
    after = {d.name for d in dirs(ARCHIVE_PREFIX)}
    check("a failure while writing the archive is reported, not swallowed", raised, "")
    check("...and its temp directory is removed", after == before, sorted(after - before))
    check("...and no partial archive exists anywhere",
          not list(PRIVATE.rglob("*.partial")), list(PRIVATE.rglob("*.partial"))[:3])
    check("...and the UI did not start tracking a directory that no longer exists",
          temp_artifacts.tracked("project_backup") == tracked_before, "")


def test_backup_failure_before_zipping_leaves_nothing():
    before = {d.name for d in dirs(ARCHIVE_PREFIX)}
    real_sha = wm._sha256

    def boom(_):
        raise PermissionError("file bloccato (simulato)")
    wm._sha256 = boom
    raised = False
    try:
        export_with(wm.WorkflowManager)
    except PermissionError:
        raised = True
    finally:
        wm._sha256 = real_sha
    check("a failure while hashing project files is reported", raised, "")
    check("...and the temp directory created for it is removed",
          {d.name for d in dirs(ARCHIVE_PREFIX)} == before, "")


def test_failed_backup_does_not_destroy_the_previous_one():
    target = DATA / "Backups_previous"
    good = export_with(wm.WorkflowManager, target)
    good_bytes = good.read_bytes()
    real, failing = _failing_write(fail_after=1)
    zipfile.ZipFile.write = failing
    try:
        export_with(wm.WorkflowManager, target)
    except OSError:
        pass
    finally:
        zipfile.ZipFile.write = real
    check("the previous archive with the same name is intact after a failed export",
          good.is_file() and good.read_bytes() == good_bytes, good)
    check("...no partial file is left beside it", not list(target.glob("*.partial")), "")
    check("...and the caller's directory is not removed", target.is_dir(), target)


# ==================================================================
# 2. HTML export (studio_export_html_ui)
# ==================================================================

def _token_of(card: str) -> str:
    marker = "/ms-export/"
    i = card.index(marker) + len(marker)
    return card[i:card.index("/", i)]


def test_html_export_success_keeps_only_what_a_live_link_needs():
    export_links._reset_for_tests()
    before = len(dirs(HTML_ZIP_PREFIX))
    card = app.studio_export_html_ui(str(PROJECT), "PUB-MAIN", "PROF-EU")
    check("the HTML export still succeeds", "Pacchetto HTML creato correttamente" in card, card[:200])
    token = _token_of(card)
    dl = export_links.resolve_download(token)
    check("its download link resolves to a ZIP that exists", dl is not None and dl[0].is_file(), dl)
    check("exactly one ZIP directory is held for that link",
          len(dirs(HTML_ZIP_PREFIX)) - before == 1, [d.name for d in dirs(HTML_ZIP_PREFIX)])
    check("no staging directory is left by the package build", not dirs(HTML_STAGING_PREFIX),
          [d.name for d in dirs(HTML_STAGING_PREFIX)])
    exports = list(PROJECT.rglob("index.html"))
    check("the canonical exported folder stays in the project", bool(exports), "")


def test_html_zips_are_bounded_by_live_links():
    """With room for 2 links, 3 exports keep 2 ZIPs -- the two still reachable."""
    export_links._reset_for_tests()
    for d in temp_artifacts.tracked("html_export_zip"):
        temp_artifacts.discard(d)
    real_max = export_links._MAX_ENTRIES
    export_links._MAX_ENTRIES = 2
    try:
        tokens = [_token_of(app.studio_export_html_ui(str(PROJECT), "PUB-MAIN", "PROF-EU"))
                  for _ in range(3)]
    finally:
        export_links._MAX_ENTRIES = real_max
    live = [t for t in tokens if export_links.resolve_download(t)]
    check("three exports with two link slots keep two ZIP directories",
          len(temp_artifacts.tracked("html_export_zip")) == 2 and len(dirs(HTML_ZIP_PREFIX)) == 2,
          [d.name for d in dirs(HTML_ZIP_PREFIX)])
    check("every link still registered can download its ZIP",
          all(export_links.resolve_download(t) for t in export_links._REGISTRY), "")
    check("the two most recent links are the live ones", live == tokens[1:], (tokens, live))


def test_html_zip_failure_cleans_up_and_export_still_succeeds():
    export_links._reset_for_tests()
    before = {d.name for d in dirs(HTML_ZIP_PREFIX)}
    real = shutil.make_archive

    def boom(*a, **k):
        raise OSError("zip non scrivibile (simulato)")
    shutil.make_archive = boom
    try:
        card = app.studio_export_html_ui(str(PROJECT), "PUB-MAIN", "PROF-EU")
    finally:
        shutil.make_archive = real
    check("a failed ZIP does not fail the export (behaviour unchanged)",
          "Pacchetto HTML creato correttamente" in card, card[:200])
    check("...and its temp directory is removed", {d.name for d in dirs(HTML_ZIP_PREFIX)} == before,
          sorted({d.name for d in dirs(HTML_ZIP_PREFIX)} - before))


def test_html_link_unavailable_cleans_up():
    before = {d.name for d in dirs(HTML_ZIP_PREFIX)}
    real = app._ensure_export_routes
    app._ensure_export_routes = lambda: False
    try:
        card = app.studio_export_html_ui(str(PROJECT), "PUB-MAIN", "PROF-EU")
    finally:
        app._ensure_export_routes = real
    check("without link routes the user gets the same error as before",
          "collegamento per aprirlo non" in card, card[:200])
    check("...and the ZIP nobody can reach is removed",
          {d.name for d in dirs(HTML_ZIP_PREFIX)} == before, "")


def test_html_package_failure_leaves_no_staging():
    before_zip = {d.name for d in dirs(HTML_ZIP_PREFIX)}
    real = po.materialize_assets

    def boom(*a, **k):
        raise OSError("asset illeggibile (simulato)")
    po.materialize_assets = boom
    try:
        card = app.studio_export_html_ui(str(PROJECT), "PUB-MAIN", "PROF-EU")
    finally:
        po.materialize_assets = real
    check("a failure inside the package build reports an error card",
          "ms-export" in card and "Pacchetto HTML creato correttamente" not in card, card[:200])
    check("...no staging directory is left", not dirs(HTML_STAGING_PREFIX), "")
    check("...and no ZIP directory is created", {d.name for d in dirs(HTML_ZIP_PREFIX)} == before_zip, "")


# ==================================================================
# 3. Process exit releases everything that was held
# ==================================================================

def test_exit_releases_everything():
    """A real child process: backup + HTML export, then a normal exit."""
    child_tmp = PRIVATE / "child_tmp"
    child_tmp.mkdir()
    probe = (
        "import sys, json, tempfile\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import app\n"
        "backup = Path(app.export_project_ui(sys.argv[2]))\n"
        "card = app.studio_export_html_ui(sys.argv[2], 'PUB-MAIN', 'PROF-EU')\n"
        "root = Path(tempfile.gettempdir())\n"
        "held = sorted(p.name for p in root.iterdir() if p.is_dir())\n"
        "print(json.dumps({'backup_exists': backup.is_file(), 'html_ok': 'creato correttamente' in card,"
        " 'held': held}))\n")
    env = dict(os.environ, TEMP=str(child_tmp), TMP=str(child_tmp), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", probe, str(BASE), str(PROJECT)], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    info = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
    check("the child process exported a backup and an HTML package",
          proc.returncode == 0 and info.get("backup_exists") and info.get("html_ok"),
          proc.stderr[-400:])
    check("while running, it held the backup and the HTML ZIP",
          any(n.startswith(ARCHIVE_PREFIX) for n in info.get("held", []))
          and any(n.startswith(HTML_ZIP_PREFIX) for n in info.get("held", [])), info.get("held"))
    left = sorted(p.name for p in child_tmp.iterdir()
                  if p.name.startswith((ARCHIVE_PREFIX, HTML_ZIP_PREFIX, HTML_STAGING_PREFIX)))
    check("after it exited, none of them is left", not left, left)


# ==================================================================
# 4. Test helpers leave nothing behind
# ==================================================================

def test_packaging_mutation_helper_leaves_no_directory():
    import regressione_commercial_build as rcb
    before = {p.name for p in PRIVATE.iterdir() if p.name.startswith("ms_pp_mutant_")}
    rcb._load_payload_mutant('RUNTIME_DATA_DIRS = ("locales", "fonts")', "RUNTIME_DATA_DIRS = ()")
    after = {p.name for p in PRIVATE.iterdir() if p.name.startswith("ms_pp_mutant_")}
    check("loading a packaging mutant leaves no ms_pp_mutant_ directory", after == before,
          sorted(after - before))


def test_regression_runner_leaves_nothing_after_a_green_sweep():
    text = (BASE / "ESEGUI_REGRESSIONE.ps1").read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    check("suites run with TEMP redirected into the run directory",
          "$env:TEMP = $RunTemp" in code and "$env:TMP = $RunTemp" in code, "")
    check("...and the real TEMP is restored afterwards", "$env:TEMP = $SavedTemp" in code, "")
    green = code[code.index("SWEEP VERDE"):code.index("exit 0", code.index("SWEEP VERDE"))]
    check("a green sweep removes the whole run directory",
          "Remove-Item -LiteralPath $RunDir -Recurse" in green, green[:200])
    check("a failing sweep keeps it for diagnosis",
          "conservati per la diagnosi: $RunDir" in code, "")
    check("leftovers are counted and shown, so leaks stay visible",
          "$Leftovers" in code and "Temporanei lasciati dalle suite" in code, "")


# ==================================================================
# 5. Mutation controls: the measurements can fail
# ==================================================================

def _load_copy(module_file: str, old: str, new: str):
    source = (BASE / module_file).read_text(encoding="utf-8")
    assert source.count(old) == 1, f"anchor not unique in {module_file}: {old!r}"
    work = Path(tempfile.mkdtemp(prefix="ms_cleanup_mutant_"))
    name = f"_cleanup_mutant_{Path(module_file).stem}_{id(work)}"
    path = work / f"{name}.py"
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return mod


def test_mutation_bounded_policy_has_teeth():
    mutant = _load_copy("temp_artifacts.py", "        stale = dirs[:-keep]\n        del dirs[:-keep]\n",
                        "        stale = []\n")
    made = [mutant.make_dir("ms_mutant_hand_over_") for _ in range(3)]
    for d in made:
        mutant.hand_over(d, group="g", keep=1)
    remaining = [d for d in made if d.exists()]
    check("[mutant] a hand_over that never trims is DETECTED (3 directories left, bound 1)",
          len(remaining) == 3, len(remaining))
    mutant.release_all()
    check("(control) release_all still clears what the mutant held",
          not any(d.exists() for d in made), "")


def test_mutation_archive_failure_cleanup_has_teeth():
    mutant = _load_copy(
        "workflow_management.py",
        "            if created_out:\n                shutil.rmtree(out, ignore_errors=True)\n",
        "            pass\n")
    before = {d.name for d in dirs(ARCHIVE_PREFIX)}
    real, failing = _failing_write(fail_after=1)
    zipfile.ZipFile.write = failing
    try:
        export_with(mutant.WorkflowManager)
    except OSError:
        pass
    finally:
        zipfile.ZipFile.write = real
    leaked = sorted({d.name for d in dirs(ARCHIVE_PREFIX)} - before)
    check("[mutant] an export that skips cleanup on failure is DETECTED", len(leaked) == 1, leaked)
    for name in leaked:
        shutil.rmtree(PRIVATE / name, ignore_errors=True)


def run():
    fns = sorted((v for k, v in globals().items() if k.startswith("test_")),
                 key=lambda f: f.__code__.co_firstlineno)
    try:
        for fn in fns:
            try:
                fn()
            except Exception as exc:
                check(f"{fn.__name__} raised", False, f"{type(exc).__name__}: {exc}")
    finally:
        temp_artifacts.release_all()
        tempfile.tempdir = None
        shutil.rmtree(PRIVATE, ignore_errors=True)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    check_private_gone = not PRIVATE.exists()
    print("=" * 78)
    print("REGRESSIONE — EXPORT TEMP CLEANUP (Commercial Ready)")
    print("=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print(f"\n  {passed}/{total} passed")
    print(f"  suite temp root removed: {check_private_gone}")
    print("=" * 78)
    return passed == total and check_private_gone


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
