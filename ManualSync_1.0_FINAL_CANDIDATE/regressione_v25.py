from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from cat_tm import TranslationMemory
from ccms_industrial import IndustrialRepository, create_industrial_demo
from translation_work_package import generate_translation_work_packages
from translation_roundtrip import XLIFF_NS
from workflow_management import WORKFLOW_BUILD, WorkflowManager

BASE = Path(__file__).resolve().parent
OUT = BASE / "regressione_v25_out"
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir()
results = []


def check(name, condition, detail=""):
    ok = bool(condition)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def extract_xlf(pkg_zip: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pkg_zip) as zf:
        zf.extractall(dest)
    return next(dest.glob("*_WORK_ONLY.xlf"))


def translated(src_xlf: Path, out_xlf: Path, target: str):
    tree = ET.parse(src_xlf); root = tree.getroot(); ns = {"x": XLIFF_NS}
    for seg in root.findall(".//x:segment", ns):
        tgt = seg.find("./x:target", ns)
        tgt.text = target
        seg.set("state", "translated")
    tree.write(out_xlf, encoding="utf-8", xml_declaration=True)


check("Workflow build", WORKFLOW_BUILD.startswith("0.25.0"), WORKFLOW_BUILD)
project = OUT / "project"; project.mkdir()
demo = create_industrial_demo(project)
ccms_db = Path(demo["repo"])
repo = IndustrialRepository(ccms_db)
try:
    tm_path = OUT / "TM_IT.sqlite"
    tm = TranslationMemory(tm_path)
    try:
        tm.add("en-US", "it-IT", "Maintain a clearance of 15 mm.", "Mantenere una distanza di 15 mm.", approved=True, tm_name="Legacy", priority=100)
    finally:
        tm.close()
    repo.update_block("BLK-I-01", text="Maintain a clearance of 20 mm.", status="Approved", source_refs=["EC#25"])
    pkg = generate_translation_work_packages(repo, "BLK-I-01", kind="block", tm_paths=[tm_path], fuzzy_threshold=70, require_approved=True, out_dir=OUT / "pkg")
finally:
    repo.close()
package_zip = Path(pkg["language_zips"][0])
original_xlf = extract_xlf(package_zip, OUT / "original")

wf = WorkflowManager(project)
try:
    check("Workflow DB created", wf.db_path.exists(), wf.db_path)
    wf.set_project_metadata("ACS880 Rev E", "Regression Client", "Documentation Team")
    meta = wf.project_metadata()
    check("Project metadata persisted", meta["title"] == "ACS880 Rev E" and meta["client"] == "Regression Client", meta)

    vendors = wf.upsert_vendor("LSP-01", "Translation Vendor", "lsp@example.test", "Regression vendor")
    check("Vendor registry persists supplier", len(vendors) == 1 and vendors.iloc[0]["vendor_id"] == "LSP-01", vendors.to_dict(orient="records"))

    job_id = wf.create_job_from_package(package_zip, vendor_id="LSP-01", due_date="2000-01-01", actor="Regression")
    job = wf.get_job(job_id)
    check("Job created as READY_TO_SEND", job["status"] == "READY_TO_SEND", job)
    check("Outbound package copied persistently", Path(job["package_path"]).exists() and Path(job["package_path"]).parent.name == "outbound", job["package_path"])
    check("Job inherits technical work counts", job["technical_review_segments"] == 1 and job["work_segments"] == 1, job)
    check("Overdue flag is computed", wf.jobs_table().iloc[0]["overdue"] == "SI", wf.jobs_table().to_dict(orient="records"))

    invalid = False
    try:
        wf.transition(job_id, "APPROVED", actor="Regression")
    except ValueError:
        invalid = True
    check("Impossible status jump is blocked", invalid, "READY_TO_SEND -> APPROVED must fail")

    wf.mark_sent(job_id, actor="Regression")
    check("READY_TO_SEND -> SENT", wf.get_job(job_id)["status"] == "SENT", wf.get_job(job_id))
    wf.mark_in_translation(job_id, actor="Regression")
    check("SENT -> IN_TRANSLATION", wf.get_job(job_id)["status"] == "IN_TRANSLATION", wf.get_job(job_id))

    bad = OUT / "returned_bad.xlf"; translated(original_xlf, bad, "Mantenere una distanza di 25 mm.")
    wf.register_returned_xliff(job_id, bad, actor="Regression")
    check("Returned XLIFF -> RECEIVED", wf.get_job(job_id)["status"] == "RECEIVED" and Path(wf.get_job(job_id)["returned_xliff_path"]).exists(), wf.get_job(job_id))
    badqa = wf.run_roundtrip_qa(job_id, ccms_db, actor="Regression")
    check("Wrong technical value -> QA_BLOCKED", badqa["job"]["status"] == "QA_BLOCKED" and badqa["job"]["qa_status"] == "BLOCKED", badqa["qa"]["summary"])

    blocked_approve = False
    try:
        wf.approve_job(job_id, ccms_db, reviewer="Regression Reviewer", human_approved=True)
    except ValueError:
        blocked_approve = True
    check("QA-blocked job cannot be approved", blocked_approve, wf.get_job(job_id))

    good = OUT / "returned_good.xlf"; translated(original_xlf, good, "Mantenere una distanza di 20 mm.")
    wf.register_returned_xliff(job_id, good, actor="Regression")
    check("Corrected return reopens QA from blocked", wf.get_job(job_id)["status"] == "RECEIVED", wf.get_job(job_id))
    goodqa = wf.run_roundtrip_qa(job_id, ccms_db, actor="Regression")
    check("Correct return -> QA_READY", goodqa["job"]["status"] == "QA_READY" and goodqa["job"]["qa_status"] == "PASS", goodqa["qa"]["summary"])

    human_block = False
    try:
        wf.approve_job(job_id, ccms_db, reviewer="Regression Reviewer", human_approved=False)
    except ValueError:
        human_block = True
    check("Explicit human approval remains mandatory", human_block, "must fail closed")

    approved = wf.approve_job(job_id, ccms_db, reviewer="Regression Reviewer", human_approved=True)
    final = approved["job"]
    check("QA_READY -> APPROVED", final["status"] == "APPROVED" and bool(final["approved_at"]), final)
    check("Approved job links TM and approval archive", Path(final["confirmed_tm_path"]).exists() and Path(final["approval_zip"]).exists(), final)
    events = wf.events_table(job_id)
    check("Workflow audit records complete lifecycle", len(events) >= 8 and "STATUS_CHANGED" in set(events["event_type"]), events.to_dict(orient="records"))
    snap = wf.export_snapshot(OUT / "snapshot")
    check("Workflow snapshot exports jobs/audit/summary", all(Path(x).exists() for x in snap.values()), snap)
    archive = wf.export_project_archive(OUT / "archive")
    check("Project archive is generated", archive.exists() and archive.suffix == ".zip", archive)
    restored = WorkflowManager.restore_project_archive(archive, OUT / "restored")
    check("Project archive restores CCMS and workflow DB", (restored / "ManualSync_Workflow.sqlite").exists() and any((restored / n).exists() for n in ["ManualSync_CCMS.sqlite","ManualSync_CCMS_v18.sqlite"]), restored)
    wf2 = WorkflowManager(restored)
    try:
        check("Restored workflow preserves approved job", len(wf2.jobs_table()) == 1 and wf2.get_job(job_id)["status"] == "APPROVED", wf2.jobs_table().to_dict(orient="records"))
    finally:
        wf2.close()
finally:
    wf.close()

summary = {"build":"v0.25","tests":len(results),"pass":sum(r["status"]=="PASS" for r in results),"fail":sum(r["status"]=="FAIL" for r in results),"results":results}
(BASE / "RISULTATO_REGRESSIONE_WORKFLOW_V25.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(BASE / "RISULTATO_REGRESSIONE_WORKFLOW_V25.txt").write_text("MANUALSYNC v0.25 — WORKFLOW & JOB MANAGEMENT\n\n" + "\n".join(f"{r['status']} | {r['test']} | {r['detail']}" for r in results) + f"\n\nPASS: {summary['pass']}/{summary['tests']}\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
