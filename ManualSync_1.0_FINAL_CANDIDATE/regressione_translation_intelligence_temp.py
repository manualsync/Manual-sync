"""Translation Intelligence Tier 2 — temporary directory is always removed.

The defect
----------
``translation_intelligence._run_tier2`` points the existing impact engine
at a fresh ``tempfile.mkdtemp()``. The engine writes a consolidated TM and
three CSV/JSON reports there and returns ``detail`` and
``language_summary`` as in-memory DataFrames. The directory was never
removed: every "Analisi impatto traduzione" -- from the Traduzioni panel
and from the Impact Center -- left one behind. A full regression sweep
left six.

The fix wraps the body in ``try/finally``. Nothing else changed: this
suite proves the directory is created and used, removed on every path,
and that every report is byte-for-byte what the previous version
produced.

The previous version is the one committed in 3967f1f, loaded from git
under a unique module name and run against the same project.

All temp activity is measured in a private root (``tempfile.tempdir``
redirected); the user's real %TEMP% is never counted or touched.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PRIVATE = Path(tempfile.mkdtemp(prefix="ms_ti_temp_suite_"))
tempfile.tempdir = str(PRIVATE)

import authoring_view as av  # noqa: E402
import translation_impact  # noqa: E402
import translation_intelligence as ti  # noqa: E402
from cat_tm import TranslationMemory  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project, update_project  # noqa: E402

PREFIX = "manualsync_translation_intel_"
ENGINE_DEFAULT_PREFIX = "manualsync_v22_impact_"
BASELINE_COMMIT = "3967f1f"
ACTOR = "Suite TI temp"
SENTENCE = "La pressione massima ammessa è 12 bar."

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))


def temp_dirs(prefix: str = PREFIX) -> list[Path]:
    return sorted(p for p in PRIVATE.iterdir() if p.is_dir() and p.name.startswith(prefix))


# ------------------------------------------------------------------
# Fixture: one approved block with a numeric-mismatch TM for de-DE,
# no TM for fr-FR, and a stale de-DE translation for Tier 1.
# ------------------------------------------------------------------

def build() -> tuple[Path, dict]:
    data = PRIVATE / "data"
    data.mkdir(exist_ok=True)
    project = create_project(data, project_name="TI_Temp", source_lang="it-IT")
    update_project(project, target_langs=["de-DE", "fr-FR"])
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
        repo.create_publication("PUB-MAIN", "Manuale Pompa PC-200", ["T-A"], source_lang="it-IT")
    finally:
        repo.close()

    repo, auth = av.open_service(str(project))
    try:
        approved = auth.create_block_in_topic("T-A", "paragraph", actor=ACTOR, text=SENTENCE)["block_id"]
        draft = auth.create_block_in_topic("T-A", "paragraph", actor=ACTOR,
                                           text="Bozza non approvata.")["block_id"]
    finally:
        repo.close()

    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        rid = repo.request_review("block", approved, requested_by=ACTOR, assigned_to=ACTOR)
        repo.decide_review(rid, decision="APPROVE", actor=ACTOR)
        # A translation made against version 0 of the block: stale in Tier 1.
        repo.conn.execute(
            """INSERT INTO block_translations(block_id,target_lang,source_version,target_text,status,updated_at)
               VALUES(?,?,?,?,?,datetime('now'))""",
            (approved, "de-DE", 0, "Alte Uebersetzung.", "Reviewed"))
        repo.conn.commit()
    finally:
        repo.close()

    tm_dir = project / "TranslationMemory"
    tm_dir.mkdir(parents=True, exist_ok=True)
    tm = TranslationMemory(tm_dir / "suite.sqlite")
    try:
        tm.add("it-IT", "de-DE", "La pressione massima ammessa è 16 bar.",
               "Der maximal zulaessige Druck betraegt 16 bar.",
               origin="suite", alignment_confidence=100.0, approved=True)
    finally:
        tm.close()
    return project, {"approved": approved, "draft": draft}


PROJECT, IDS = build()


def load_baseline():
    """translation_intelligence.py as committed in BASELINE_COMMIT."""
    proc = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:ManualSync_1.0_FINAL_CANDIDATE/translation_intelligence.py"],
        cwd=str(BASE.parent), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"git show failed: {proc.stderr.strip()}")
    return _load_source(proc.stdout, "_ti_baseline")


def _load_source(source: str, stem: str):
    work = Path(tempfile.mkdtemp(prefix="ms_ti_module_"))
    name = f"{stem}_{id(work)}"
    path = work / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves its class's module through sys.modules while the
    # module is executing, so the copy must be registered for that long.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
        shutil.rmtree(work, ignore_errors=True)
    return mod


class EngineSpy:
    """Wraps translation_impact.analyze_translation_impact.

    _run_tier2 imports the engine inside the function, from the module, so
    replacing the module attribute is what it will call.
    """

    def __init__(self, *, fail: bool = False, empty_summary: bool = False):
        self.fail = fail
        self.empty_summary = empty_summary
        self.out_dir: Path | None = None
        self.existed_during_call = False
        self.files_written: list[str] = []
        self._real = translation_impact.analyze_translation_impact

    def __call__(self, *args, **kwargs):
        self.out_dir = Path(kwargs["out_dir"])
        self.existed_during_call = self.out_dir.is_dir()
        if self.fail:
            (self.out_dir / "partial_output.csv").write_text("x", encoding="utf-8")
            self.files_written = sorted(p.name for p in self.out_dir.iterdir())
            raise RuntimeError("motore di impatto: errore simulato")
        result = self._real(*args, **kwargs)
        self.files_written = sorted(p.name for p in self.out_dir.iterdir())
        if self.empty_summary:
            result = dict(result, language_summary=result["language_summary"].iloc[0:0])
        return result

    def __enter__(self):
        translation_impact.analyze_translation_impact = self
        return self

    def __exit__(self, *exc):
        translation_impact.analyze_translation_impact = self._real
        return False


# ==================================================================
# 1. The directory is created and really used
# ==================================================================

def test_tier2_creates_and_uses_a_temp_directory():
    with EngineSpy() as spy:
        report = ti.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    check("Tier 2 ran", report.tier2_state == ti.TIER2_AVAILABLE, report.tier2_state)
    check("the engine was given a temp directory", spy.out_dir is not None, spy.out_dir)
    check("...with the Translation Intelligence prefix",
          spy.out_dir is not None and spy.out_dir.name.startswith(PREFIX), spy.out_dir)
    check("...in the temp root", spy.out_dir is not None and spy.out_dir.parent == PRIVATE,
          spy.out_dir)
    check("...that existed while the engine ran", spy.existed_during_call, "")
    check("the engine wrote its reports there",
          {"TRANSLATION_IMPACT_SEGMENTS.csv", "TRANSLATION_IMPACT_LANGUAGES.csv",
           "TRANSLATION_IMPACT_SUMMARY.json"} <= set(spy.files_written), spy.files_written)
    check("...and the consolidated TM for the language that has one",
          any(n.startswith("_tm_") and n.endswith(".sqlite") for n in spy.files_written),
          spy.files_written)


# ==================================================================
# 2. Removed after a successful run
# ==================================================================

def test_removed_after_success():
    before = set(temp_dirs())
    with EngineSpy() as spy:
        report = ti.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    check("the result is complete", report.tier2_state == ti.TIER2_AVAILABLE and bool(report.tier2),
          report.tier2_state)
    check("the temp directory no longer exists", spy.out_dir is not None and not spy.out_dir.exists(),
          spy.out_dir)
    check("no Translation Intelligence temp directory is left", set(temp_dirs()) == before,
          [p.name for p in temp_dirs()])
    check("the engine did not fall back to its own default temp directory",
          not temp_dirs(ENGINE_DEFAULT_PREFIX), [p.name for p in temp_dirs(ENGINE_DEFAULT_PREFIX)])


def test_removed_after_the_empty_summary_early_return():
    with EngineSpy(empty_summary=True) as spy:
        report = ti.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    check("an empty summary still reports Tier 2 available with no rows",
          report.tier2_state == ti.TIER2_AVAILABLE and report.tier2 == (), report.tier2_state)
    check("...and the temp directory is removed on that early return",
          spy.out_dir is not None and not spy.out_dir.exists(), spy.out_dir)


# ==================================================================
# 3. Removed when something fails
# ==================================================================

def test_removed_when_the_engine_fails():
    with EngineSpy(fail=True) as spy:
        report = ti.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    check("an engine failure is still reported as TIER2_FAILED (unchanged)",
          report.tier2_state == ti.TIER2_FAILED and report.tier2 == (), report.tier2_state)
    check("the engine had written partial output before failing",
          "partial_output.csv" in spy.files_written, spy.files_written)
    check("...and the directory with that partial output is removed",
          spy.out_dir is not None and not spy.out_dir.exists(), spy.out_dir)


def test_removed_when_reading_the_result_fails():
    """An exception after the engine returned still propagates -- and cleans up."""
    real = ti._reasons_for_language

    def boom(rows):
        raise KeyError("colonna attesa assente (simulato)")
    ti._reasons_for_language = boom
    raised = None
    try:
        with EngineSpy() as spy:
            ti.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    except KeyError as exc:
        raised = exc
    finally:
        ti._reasons_for_language = real
    check("the exception propagates to the caller, as before", raised is not None, raised)
    check("...and the temp directory is removed anyway",
          spy.out_dir is not None and not spy.out_dir.exists(), spy.out_dir)

    baseline = load_baseline()
    real_b = baseline._reasons_for_language
    baseline._reasons_for_language = boom
    raised_b = None
    try:
        baseline.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    except KeyError as exc:
        raised_b = exc
    finally:
        baseline._reasons_for_language = real_b
    check("(control) the previous version propagated the same exception type",
          type(raised_b) is type(raised), (type(raised_b), type(raised)))
    for leftover in temp_dirs():   # the previous version leaves its directory: tidy it
        shutil.rmtree(leftover, ignore_errors=True)


# ==================================================================
# 4. No regression on Tier 1 / Tier 2 results
# ==================================================================

def test_results_identical_to_the_previous_version():
    baseline = load_baseline()
    scenarios = [
        ("Tier 1 only", dict(object_id=IDS["approved"], deep=False)),
        ("Tier 1 + Tier 2", dict(object_id=IDS["approved"], deep=True)),
        ("Tier 2 narrowed to de-DE", dict(object_id=IDS["approved"], deep=True, language="de-DE")),
        ("explicit object type", dict(object_id=IDS["approved"], deep=True, object_type="block")),
        ("Draft object (Tier 2 unavailable)", dict(object_id=IDS["draft"], deep=True)),
    ]
    for label, kwargs in scenarios:
        new = ti.assess_translation_intelligence(PROJECT, **kwargs).to_dict()
        old = baseline.assess_translation_intelligence(PROJECT, **kwargs).to_dict()
        check(f"[{label}] report identical to {BASELINE_COMMIT}", new == old,
              {"new": new.get("tier2_state"), "old": old.get("tier2_state")})
    for leftover in temp_dirs():   # created by the previous version only
        shutil.rmtree(leftover, ignore_errors=True)

    full = ti.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    langs = {l.language: l for l in full.languages}
    check("(non-vacuous) Tier 1 sees the stale de-DE translation",
          langs.get("de-DE") is not None and langs["de-DE"].status == ti.STATUS_STALE,
          {k: v.status for k, v in langs.items()})
    check("(non-vacuous) Tier 1 sees fr-FR as missing",
          langs.get("fr-FR") is not None and langs["fr-FR"].status == ti.STATUS_MISSING, "")
    tier2 = {t.language: t for t in full.tier2}
    de_codes = [r.code for r in tier2["de-DE"].reasons] if "de-DE" in tier2 else []
    check("(non-vacuous) Tier 2 blocks the de-DE reuse on the numeric mismatch",
          ti.TM_BLOCKED_NUMERIC_MISMATCH in de_codes, de_codes)
    check("(non-vacuous) Tier 2 reports fr-FR without a translation memory",
          "fr-FR" in tier2 and tier2["fr-FR"].tm_available is False,
          {k: v.tm_available for k, v in tier2.items()})
    check("and that run left no temp directory", not temp_dirs(), [p.name for p in temp_dirs()])


# ==================================================================
# 5. Mutation: the check can fail
# ==================================================================

def test_mutation_without_finally_cleanup_is_detected():
    source = (BASE / "translation_intelligence.py").read_text(encoding="utf-8")
    anchor = "    finally:\n        shutil.rmtree(tmp, ignore_errors=True)\n"
    check("the cleanup anchor exists exactly once", source.count(anchor) == 1, source.count(anchor))
    mutant = _load_source(source.replace(anchor, "    finally:\n        pass\n"), "_ti_mutant")
    before = set(temp_dirs())
    mutant.assess_translation_intelligence(PROJECT, IDS["approved"], deep=True)
    leaked = sorted(set(temp_dirs()) - before)
    check("[mutant] Tier 2 without the finally cleanup LEAKS and is detected", len(leaked) == 1,
          [p.name for p in leaked])
    for p in leaked:
        shutil.rmtree(p, ignore_errors=True)


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
        tempfile.tempdir = None
        shutil.rmtree(PRIVATE, ignore_errors=True)
    root_removed = not PRIVATE.exists()
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("=" * 78)
    print("REGRESSIONE — TRANSLATION INTELLIGENCE TEMP CLEANUP")
    print("=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print(f"\n  {passed}/{total} passed")
    print(f"  suite temp root removed: {root_removed}")
    print("=" * 78)
    return passed == total and root_removed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
