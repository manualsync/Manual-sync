from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def run_acceptance(report_dir: str | Path | None = None) -> dict:
    """Run a destructive-free commercial acceptance test in isolated temp folders.

    The test exercises the same application callback layer used by the UI, then
    independently verifies production linguistic fail-closed behavior. It never
    reads or modifies the user's real projects.
    """
    root = Path(tempfile.mkdtemp(prefix="manualsync-commercial-acceptance-"))
    fake_home = root / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    old_home = os.environ.get("HOME")
    old_userprofile = os.environ.get("USERPROFILE")
    os.environ["HOME"] = str(fake_home)
    os.environ["USERPROFILE"] = str(fake_home)

    results: list[dict] = []

    def check(name: str, condition, detail="") -> None:
        ok = bool(condition)
        results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)[:4000]})
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    try:
        import app
        from cat_tm import TranslationMemory
        from ccms_industrial import IndustrialRepository
        from release_manager import create_release_bundle
        from translation_roundtrip import XLIFF_NS
        from workflow_management import WorkflowManager

        # Isolate product-level storage after importing the packaged callback layer.
        app.DATA_HOME = root / "ManualSyncData"
        app.DATA_ROOT = app.DATA_HOME / "Projects"
        app.ACTIVE_PROJECT_FILE = app.DATA_HOME / "active_project.json"
        app.APP_LOG = app.DATA_HOME / "MANUALSYNC_APP.log"
        app.DATA_ROOT.mkdir(parents=True, exist_ok=True)

        created = app.create_project_ui(
            "Acceptance Pilot", "ACME", "Drive", "ACC-001", "A",
            "en-US", "it-IT", "Acceptance",
        )
        project = Path(created[0])
        check("Project create", project.exists() and (project / "ManualSync_Project.json").exists(), project)

        duplicate = app.create_project_ui(
            "Acceptance Pilot", "ACME", "Drive", "ACC-001", "A",
            "en-US", "it-IT", "Acceptance",
        )
        check("Project create idempotency", Path(duplicate[0]) == project, duplicate[3])

        repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
        try:
            repo.create_topic("T1", "Installation", "acceptance", status="Approved", source_refs=["SPEC#1"])
            repo.create_publication("PUB-MAIN", "Machine Manual", ["T1"], source_lang="en-US")
            repo.add_block("B1", "T1", "paragraph", position=1, text="Maintain a clearance of 20 mm.", status="Approved", source_refs=["SPEC#2"])
            repo.add_block("B2", "T1", "paragraph", position=2, text="Maintain a clearance of 20 mm.", status="Approved", source_refs=["SPEC#2"])
            repo.add_block("B3", "T1", "paragraph", position=3, text="ACS880-34", status="Approved", source_refs=["SPEC#3"])
        finally:
            repo.close()

        tm_path = root / "Legacy_IT.sqlite"
        tm = TranslationMemory(tm_path)
        try:
            tm.add(
                "en-US", "it-IT",
                "Maintain a clearance of 15 mm.",
                "Mantenere una distanza di 15 mm.",
                approved=True, tm_name="Acceptance", priority=100,
            )
        finally:
            tm.close()

        wp_msg, wp_table, master_zip, lang_zips = app.work_package_ui(
            str(project), "PUB-MAIN", "publication", [str(tm_path)]
        )
        lang_zip = Path(lang_zips[0])
        check("Translation package", lang_zip.exists() and Path(master_zip).exists(), wp_msg)

        work = root / "work"
        work.mkdir()
        with zipfile.ZipFile(lang_zip) as zf:
            zf.extractall(work)
        xlf = next(work.glob("*_WORK_ONLY.xlf"))
        tree = ET.parse(xlf)
        ns = {"x": XLIFF_NS}
        for seg in tree.getroot().findall(".//x:segment", ns):
            src = "".join(seg.find("./x:source", ns).itertext())
            target = seg.find("./x:target", ns)
            translations = {
                "Installation": "Installazione",
                "Maintain a clearance of 20 mm.": "Mantenere una distanza di 20 mm.",
            }
            target.text = translations.get(src, src)
            seg.set("state", "translated")
        returned = root / "returned_it.xlf"
        tree.write(returned, encoding="utf-8", xml_declaration=True)

        job_msg, _ = app.create_job_ui(str(project), str(lang_zip), "", "")
        wf = WorkflowManager(project)
        try:
            jobs = wf.jobs_table()
            jid = str(jobs.iloc[-1]["job_id"])
        finally:
            wf.close()
        check("Job create", bool(jid) and "creato" in job_msg.lower(), job_msg)

        app.job_action_ui(str(project), jid, "Segna inviato", None, None, False, "", False)
        app.job_action_ui(str(project), jid, "Segna in traduzione", None, None, False, "", False)
        invalid = app.job_action_ui(str(project), jid, "Segna inviato", None, None, False, "", False)
        wf = WorkflowManager(project)
        try:
            still_in_translation = wf.get_job(jid)["status"] == "IN_TRANSLATION"
        finally:
            wf.close()
        check(
            "Invalid job transition safely guided",
            still_in_translation and "non applicabile" in invalid[0].lower(),
            invalid[0],
        )
        qa_out = app.job_action_ui(
            str(project), jid, "Ricevi XLIFF + controllo qualità (QA)",
            str(returned), None, False, "", False,
        )
        check("Round-trip QA ready", "superato" in qa_out[0].lower(), qa_out[0])

        approved = app.job_action_ui(
            str(project), jid, "Approva traduzione", None, None, False,
            "Acceptance Reviewer", True,
        )
        check("Human approval commit", approved[4] is not None and approved[5] is not None, approved[0])

        readiness = app.readiness_ui(str(project), "PUB-MAIN", "it-IT", "")
        check("Readiness", "RELEASE PRONTA" in readiness[0], readiness[0])

        internal = app.release_ui(
            str(project), "PUB-MAIN", "it-IT", "Acceptance Release",
            "ACC-TEST", "", "", "Collaudo interno",
        )
        check("Internal release", internal[1] is not None and Path(internal[1]).exists(), internal[0])

        production = app.release_ui(
            str(project), "PUB-MAIN", "it-IT", "Acceptance Release",
            "ACC-PROD", "", "", "Produzione cliente",
        )
        check("Valid production release", production[1] is not None and Path(production[1]).exists(), production[0])

        backup = app.export_project_ui(str(project))
        check("Project backup", backup is not None and Path(backup).exists(), backup)
        health_msg, _ = app.diagnostics_ui(str(project))
        check("Diagnostics", "verificata" in health_msg.lower(), health_msg)
        restored = app.restore_project_ui(str(backup))
        check("Transactional restore", restored[0] and Path(restored[0]).exists(), restored[3])

        # Independent production safety fixture: a human-reviewed target copied
        # verbatim across different languages must never be deliverable. Build it
        # through the normal project callback so UI readiness is tested too.
        unsafe_created = app.create_project_ui(
            "Unsafe Production Pilot", "ACME", "Drive", "ACC-UNSAFE", "A",
            "it-IT", "en-US", "Acceptance",
        )
        unsafe = Path(unsafe_created[0])
        repo = IndustrialRepository(unsafe / "ManualSync_CCMS.sqlite")
        try:
            repo.create_topic("T1", "Manuale macchina", "acceptance", status="Approved", source_refs=["SPEC#1"])
            repo.create_publication("PUB-MAIN", "Manuale macchina", ["T1"], source_lang="it-IT")
            repo.add_block("B1", "T1", "paragraph", position=1, text="Premere il pulsante di arresto della macchina.", status="Approved", source_refs=["SPEC#2"])
            repo.set_translation("T1", "en-US", "Manuale macchina", target_title="Manuale macchina", status="Reviewed")
            repo.set_block_translation("B1", "en-US", "Premere il pulsante di arresto della macchina.", status="Reviewed")
        finally:
            repo.close()

        unsafe_readiness = app.readiness_ui(str(unsafe), "PUB-MAIN", "en-US", "", "Produzione cliente")
        blocking_df = getattr(unsafe_readiness[2], "get", lambda *a, **k: None)("value") if isinstance(unsafe_readiness[2], dict) else None
        check(
            "Production readiness linguistic fail-closed",
            "RELEASE BLOCCATA" in unsafe_readiness[0],
            unsafe_readiness[0],
        )

        blocked = False
        block_detail = ""
        try:
            create_release_bundle(
                unsafe, "PUB-MAIN", target_langs=["en-US"],
                released_by="Acceptance", release_id="ACC-UNSAFE",
                release_mode="production", out_dir=root / "unsafe-out",
            )
        except Exception as exc:
            blocked = True
            block_detail = str(exc)
        check("Production linguistic fail-closed", blocked and "linguistic" in block_detail.lower(), block_detail)

    except Exception as exc:
        if not any(r["status"] == "FAIL" for r in results):
            results.append({"test": "Unhandled acceptance exception", "status": "FAIL", "detail": traceback.format_exc()[:8000]})
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_userprofile

    passed = sum(r["status"] == "PASS" for r in results)
    failed = sum(r["status"] == "FAIL" for r in results)
    payload = {
        "build": "ManualSync 1.0 Final Candidate Commercial Acceptance",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "tests": len(results),
        "pass": passed,
        "fail": failed,
        "status": "PASS" if failed == 0 else "FAIL",
        "results": results,
    }

    out_dir = Path(report_dir) if report_dir else root / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "COMMERCIAL_ACCEPTANCE.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "COMMERCIAL_ACCEPTANCE.txt").write_text(
        "\n".join(f"{r['status']} | {r['test']} | {r['detail']}" for r in results)
        + f"\n\nPASS: {passed}/{len(results)}\nSTATUS: {payload['status']}\n",
        encoding="utf-8",
    )
    payload["report_dir"] = str(out_dir)
    # Keep report but remove all test projects/assets.
    if out_dir.is_relative_to(root):
        keep = Path(tempfile.mkdtemp(prefix="manualsync-commercial-acceptance-report-"))
        for name in ("COMMERCIAL_ACCEPTANCE.json", "COMMERCIAL_ACCEPTANCE.txt"):
            shutil.copy2(out_dir / name, keep / name)
        payload["report_dir"] = str(keep)
        shutil.rmtree(root, ignore_errors=True)
    else:
        shutil.rmtree(root, ignore_errors=True)
    return payload


def main() -> int:
    payload = run_acceptance()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
