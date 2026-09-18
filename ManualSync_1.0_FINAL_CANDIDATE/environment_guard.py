from __future__ import annotations

import hashlib
import json
import platform
import sys
import tempfile
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPORT_JSON = BASE / "RUNTIME_SELF_TEST_FINAL.json"
REPORT_TXT = BASE / "RUNTIME_SELF_TEST_FINAL.txt"

# The lock that describes an environment able to RUN THE PRODUCT AND ITS
# TESTS. requirements.runtime.lock.txt is a superset: it adds pywebview,
# which only the packaged desktop shell needs and which a headless test
# environment deliberately does not install. Asserting the runtime lock
# here would make the guard refuse the project's own .venv.
BASE_LOCK = BASE / "requirements.lock.txt"
RUNTIME_LOCK = BASE / "requirements.runtime.lock.txt"

SUPPORTED_PYTHON = (3, 12)


def expected_packages(lock_path: Path | None = None) -> dict[str, str]:
    """The pinned runtime versions, read from the lock file.

    These used to be a dict written out here by hand, duplicating
    requirements.runtime.lock.txt. Two copies of the same seven versions
    is one copy too many: the guard would keep passing against versions
    the distribution no longer installs. The lock is the source; this
    reads it.

    Only ``name==version`` lines are honoured. Anything looser is not a
    pin and cannot be asserted, so it is skipped rather than guessed at.
    """
    path = lock_path or BASE_LOCK
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, ver = line.partition("==")
        name, ver = name.strip(), ver.strip()
        if name and ver:
            pins[name] = ver
    if not pins:
        raise RuntimeError(f"Nessuna versione bloccata leggibile da {path}")
    return pins


EXPECTED_PACKAGES = expected_packages()


def environment_problems() -> list[str]:
    """Everything wrong with THIS interpreter, as plain sentences.

    Kept separate from main() so build scripts, the packaging regression
    and any future runner can all ask the same question without running
    the full self-test (which imports the product and takes minutes).
    """
    problems: list[str] = []
    if sys.version_info[:2] != SUPPORTED_PYTHON:
        problems.append(
            f"Python {'.'.join(map(str, SUPPORTED_PYTHON))}.x richiesto, "
            f"in esecuzione {platform.python_version()} ({sys.executable})")
    for pkg, expected in EXPECTED_PACKAGES.items():
        try:
            actual = version(pkg)
        except PackageNotFoundError:
            problems.append(f"{pkg}: non installato (atteso {expected})")
            continue
        if actual != expected:
            problems.append(f"{pkg}: atteso {expected}, installato {actual}")
    return problems


def assert_supported_environment() -> None:
    """Refuse to continue on an interpreter the product is not built for.

    This exists because of a real and expensive misreading: an entire
    regression sweep was run on the system interpreter (Python 3.14,
    PyMuPDF 1.28) instead of the project environment (3.12, 1.26.7).
    PyMuPDF's table detector changed between those versions, so a suite
    that pins a table count reported a product regression that did not
    exist, and it was believed twice.

    An environment mismatch is not a test failure; it makes every test
    result meaningless. So this raises instead of returning a status.
    """
    problems = environment_problems()
    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise RuntimeError(
            "Ambiente non conforme: i risultati di build e test non sarebbero "
            f"significativi.\n{detail}\n"
            "  Usare l'ambiente del progetto (.venv) o reinstallare da "
            f"{BASE_LOCK.name}.")


EXPECTED_BENCHMARK_HASHES = {
    "benchmark/siemens/old_EN.pdf": "cac23898a21414ae313af1ba96ddad9f2917b81be5502dbcff7bfd4cf2a133ab",
    "benchmark/siemens/old_IT.pdf": "4719aebe721c96764032598040f3e50d411c41995315e1d4643bc0e9f711ab48",
    "benchmark/siemens/new_EN.pdf": "0f969c6cc925dfe1917ef8b8f376470fb7fbe8513fe355975376d405725fd21a",
    "benchmark/abb_acs880/old_EN.pdf": "b5066d835285951d2e61445c3f77b4f8b9d52cdafc5c8f26c6df987b140c268e",
    "benchmark/abb_acs880/old_IT.pdf": "f7b87d3115ed94715f3c5c41e3e28df5cd7f9e145054f7030c6c63bb27c1b5bc",
    "benchmark/abb_acs880/new_EN.pdf": "a46ed1e052875492a980c5aaca4965c7b142928d155da2701a99f3cd8c62a362",
    "benchmark/abb_acs580/old_EN.pdf": "05cf0cee911ade97d0e3d53a015ff5f103dc88e31293bfe7adc1e583ca6a58cf",
    "benchmark/abb_acs580/old_IT.pdf": "81748bcbec099c930dd0c43e90e99aa57e407305fd646d7e6c9d9cd102bf8161",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def add_result(results, name, ok, detail):
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})


def main() -> int:
    # Windows consoles default to a legacy codepage (e.g. cp1252) that cannot encode
    # characters used in some check details (e.g. "≠"); force UTF-8 stdio so the
    # final report print never crashes after all checks have already run and passed.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    results = []
    py_ok = sys.version_info[:2] == SUPPORTED_PYTHON
    add_result(results, f"Python {SUPPORTED_PYTHON[0]}.{SUPPORTED_PYTHON[1]}", py_ok,
               platform.python_version())

    for pkg, expected in EXPECTED_PACKAGES.items():
        try:
            actual = version(pkg)
            ok = actual == expected
        except PackageNotFoundError:
            actual = "NOT INSTALLED"
            ok = False
        add_result(results, f"Dependency {pkg}", ok, f"expected={expected}; actual={actual}")

    for rel, expected_hash in EXPECTED_BENCHMARK_HASHES.items():
        p = BASE / rel
        actual = sha256(p) if p.exists() else "MISSING"
        add_result(results, f"Fixture integrity {rel}", bool(expected_hash) and actual == expected_hash, actual)

    try:
        from layout_engine import ENGINE_BUILD, analyze_layouts, _cell_change
        add_result(results, "Technical Engine build", ENGINE_BUILD.startswith("0.11.6"), ENGINE_BUILD)
        for old, new in [("15.8", "25.27"), ("-2.9", "2.9"), ("-6", "6")]:
            decision = _cell_change(old, new)[2]
            add_result(results, f"Critical numeric gate {old}->{new}", decision == "BLOCCATO — REVISIONE TECNICA", decision)

        p = BASE / "benchmark" / "siemens"
        _, _, _, summary = analyze_layouts(p / "old_EN.pdf", p / "old_IT.pdf", p / "new_EN.pdf")
        expected = {
            "riusi_automatici_consentiti": 4,
            "riusi_celle_tabelle_consentiti": 5,
            "tabelle_vecchie": 2,
            "tabelle_nuove": 2,
        }
        for key, exp in expected.items():
            actual = summary.get(key)
            add_result(results, f"Siemens quick baseline {key}", actual == exp, f"expected={exp}; actual={actual}")
    except Exception as exc:
        add_result(results, "Technical Engine quick self-test", False, repr(exc))

    # v0.22 quick Translation Impact gate: actual TM-derived reuse, not manual percentages.
    try:
        from cat_tm import TranslationMemory
        from ccms_industrial import IndustrialRepository, create_industrial_demo
        from translation_impact import TRANSLATION_IMPACT_BUILD, analyze_translation_impact
        add_result(results, "Translation Impact build", TRANSLATION_IMPACT_BUILD.startswith("0.22."), TRANSLATION_IMPACT_BUILD)
        with tempfile.TemporaryDirectory(prefix="manualsync_v22_selftest_") as td:
            root = Path(td)
            demo = create_industrial_demo(root / "ccms")
            tm_path = root / "tm.sqlite"
            tm = TranslationMemory(tm_path)
            try:
                tm.add(
                    "en-US", "it-IT",
                    "Install the drive vertically on a rigid surface.",
                    "Installare il convertitore verticalmente su una superficie rigida.",
                    approved=True, tm_name="SelfTest", priority=100,
                )
            finally:
                tm.close()
            repo = IndustrialRepository(demo["repo"])
            try:
                impact = analyze_translation_impact(repo, "BLK-I-01", kind="block", tm_paths=[tm_path], require_approved=True, out_dir=root / "impact")
            finally:
                repo.close()
            langs = impact["language_summary"]
            ok = len(langs) == 1 and int(langs.iloc[0]["auto_reuse_words"]) > 0 and int(langs.iloc[0]["new_translation_words"]) == 0
            add_result(results, "Translation Impact CAT-derived reuse", ok, langs.to_dict(orient="records"))
    except Exception as exc:
        add_result(results, "Translation Impact quick self-test", False, repr(exc))

    # v0.23 quick work-package gate: AUTO_REUSE must stay outside translator XLIFF.
    try:
        from cat_tm import TranslationMemory
        from ccms_industrial import IndustrialRepository, create_industrial_demo
        from translation_work_package import WORK_PACKAGE_BUILD, generate_translation_work_packages, inspect_work_xliff
        add_result(results, "Translation Work Package build", WORK_PACKAGE_BUILD.startswith("0.23."), WORK_PACKAGE_BUILD)
        with tempfile.TemporaryDirectory(prefix="manualsync_v23_selftest_") as td:
            root = Path(td)
            demo = create_industrial_demo(root / "ccms")
            tm_path = root / "tm.sqlite"
            tm = TranslationMemory(tm_path)
            try:
                tm.add("en-US", "it-IT", "Maintain a clearance of 15 mm.", "Mantenere una distanza di 15 mm.", approved=True, tm_name="SelfTest", priority=100)
            finally:
                tm.close()
            repo = IndustrialRepository(demo["repo"])
            try:
                repo.update_block("BLK-I-01", text="Maintain a clearance of 20 mm.", status="Approved", source_refs=["SelfTest#EC"])
                pkg = generate_translation_work_packages(repo, "BLK-I-01", kind="block", tm_paths=[tm_path], fuzzy_threshold=70, require_approved=True, out_dir=root / "pkg")
            finally:
                repo.close()
            z = Path(pkg["language_zips"][0])
            import zipfile
            with zipfile.ZipFile(z) as zf:
                zf.extractall(root / "inspect")
            xlf = next((root / "inspect").glob("*_WORK_ONLY.xlf"))
            df = inspect_work_xliff(xlf)
            ok = len(df) == 1 and str(df.iloc[0]["work_bucket"]) == "TECHNICAL_REVIEW" and not str(df.iloc[0]["target"]).strip()
            add_result(results, "Translation Work Package technical target blank", ok, df.to_dict(orient="records"))
    except Exception as exc:
        add_result(results, "Translation Work Package quick self-test", False, repr(exc))

    # v0.24 quick round-trip gate: a wrong translated technical value must remain blocked.
    try:
        import xml.etree.ElementTree as ET
        import zipfile
        from cat_tm import TranslationMemory
        from ccms_industrial import IndustrialRepository, create_industrial_demo
        from translation_work_package import generate_translation_work_packages
        from translation_roundtrip import ROUNDTRIP_BUILD, XLIFF_NS, inspect_translation_roundtrip
        add_result(results, "Translation Round-Trip build", ROUNDTRIP_BUILD.startswith("0.24."), ROUNDTRIP_BUILD)
        with tempfile.TemporaryDirectory(prefix="manualsync_v24_selftest_") as td:
            root = Path(td)
            demo = create_industrial_demo(root / "ccms")
            tm_path = root / "tm.sqlite"
            tm = TranslationMemory(tm_path)
            try:
                tm.add("en-US", "it-IT", "Maintain a clearance of 15 mm.", "Mantenere una distanza di 15 mm.", approved=True, tm_name="SelfTest", priority=100)
            finally:
                tm.close()
            repo = IndustrialRepository(demo["repo"])
            try:
                repo.update_block("BLK-I-01", text="Maintain a clearance of 20 mm.", status="Approved", source_refs=["SelfTest#EC24"])
                pkg = generate_translation_work_packages(repo, "BLK-I-01", kind="block", tm_paths=[tm_path], fuzzy_threshold=70, require_approved=True, out_dir=root / "pkg")
                pzip = Path(pkg["language_zips"][0])
                with zipfile.ZipFile(pzip) as zf:
                    zf.extractall(root / "work")
                xlf = next((root / "work").glob("*_WORK_ONLY.xlf"))
                tree = ET.parse(xlf); ns = {"x": XLIFF_NS}
                seg = tree.getroot().find(".//x:segment", ns)
                seg.find("./x:target", ns).text = "Mantenere una distanza di 25 mm."
                returned = root / "returned_bad.xlf"; tree.write(returned, encoding="utf-8", xml_declaration=True)
                qa = inspect_translation_roundtrip(repo, pzip, returned, out_dir=root / "qa")
            finally:
                repo.close()
            ok = qa["summary"]["hard_fail_count"] == 1 and not qa["summary"]["commit_allowed_after_human_approval"]
            add_result(results, "Round-Trip wrong numeric value blocked", ok, qa["audit"].to_dict(orient="records"))
    except Exception as exc:
        add_result(results, "Translation Round-Trip quick self-test", False, repr(exc))

    # v0.25 quick workflow gate: package -> audited job -> legal state transitions.
    try:
        import zipfile
        from workflow_management import WORKFLOW_BUILD, WorkflowManager
        add_result(results, "Workflow / Job Management build", WORKFLOW_BUILD.startswith("0.25.0"), WORKFLOW_BUILD)
        with tempfile.TemporaryDirectory(prefix="manualsync_v25_selftest_") as td:
            root = Path(td)
            pkg = root / "package.zip"
            manifest = {
                "source": {"kind":"block","id":"BLK-SELF","version":1,"status":"Approved","source_lang":"en-US"},
                "target_lang":"it-IT",
                "counts": {"work_segments":1,"work_words":5,"linguistic_review_segments":0,"technical_review_segments":1,"new_translation_segments":0},
            }
            with zipfile.ZipFile(pkg, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("MANIFEST.json", json.dumps(manifest))
            wf = WorkflowManager(root / "project")
            try:
                jid = wf.create_job_from_package(pkg, actor="SelfTest")
                created_ok = wf.get_job(jid)["status"] == "READY_TO_SEND"
                wf.mark_sent(jid, actor="SelfTest")
                wf.mark_in_translation(jid, actor="SelfTest")
                state_ok = wf.get_job(jid)["status"] == "IN_TRANSLATION"
                events_ok = len(wf.events_table(jid)) == 3
            finally:
                wf.close()
            add_result(results, "Workflow audited state machine", created_ok and state_ok and events_ok, f"created={created_ok}; state={state_ok}; events={events_ok}")
    except Exception as exc:
        add_result(results, "Workflow quick self-test", False, repr(exc))

    # v0.26-v0.28 release/product lifecycle smoke tests.
    try:
        from project_lifecycle import PROJECT_BUILD, create_project
        from release_readiness import READINESS_BUILD
        from release_manager import RELEASE_BUILD
        add_result(results, "Project lifecycle build", PROJECT_BUILD.startswith("0.28.0"), PROJECT_BUILD)
        add_result(results, "Release Readiness build", READINESS_BUILD.startswith(("0.26.", "0.27.")), READINESS_BUILD)
        add_result(results, "Controlled Release build", RELEASE_BUILD.startswith(("0.27.", "0.28.")), RELEASE_BUILD)
        with tempfile.TemporaryDirectory(prefix="manualsync_final_selftest_") as td:
            p = create_project(Path(td), project_name="Runtime SelfTest", source_lang="en-US", target_langs=["it-IT"])
            add_result(results, "Final project bootstrap", (p / "ManualSync_CCMS.sqlite").exists() and (p / "ManualSync_Workflow.sqlite").exists(), p)
    except Exception as exc:
        add_result(results, "Final product lifecycle quick self-test", False, repr(exc))

    try:
        import app as product_app
        add_result(results, "Product UI import", str(product_app.PRODUCT_BUILD).startswith("1.0.0-"), product_app.PRODUCT_BUILD)
    except Exception as exc:
        add_result(results, "Product UI import", False, repr(exc))

    passed = sum(r["status"] == "PASS" for r in results)
    failed = len(results) - passed
    payload = {
        "build": str(getattr(product_app, "PRODUCT_BUILD", "1.0.0")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tests": len(results),
        "pass": passed,
        "fail": failed,
        "runtime_conformant": failed == 0,
        "results": results,
    }
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "MANUALSYNC 1.0 — RUNTIME SELF-TEST",
        "",
        *[f"{r['status']} | {r['test']} | {r['detail']}" for r in results],
        "",
        f"PASS: {passed}/{len(results)}",
        f"RUNTIME CONFORME: {'SI' if failed == 0 else 'NO'}",
    ]
    REPORT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
