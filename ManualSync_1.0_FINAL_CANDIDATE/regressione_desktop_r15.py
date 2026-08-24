from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pandas as pd
from docx import Document

from ccms_industrial import IndustrialRepository
from release_manager import _validate_customer_docx, create_release_bundle
from product_version import PRODUCT_BUILD
from app import (
    _friendly_work_packages,
    _friendly_jobs,
    _friendly_vendors,
    _friendly_events,
    _friendly_readiness_matrix,
    _friendly_release_artifacts,
)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "regressione_desktop_r15_out"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

results = []
def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")

def make_project(path: Path, *, target_text: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    db = path / "ManualSync_CCMS.sqlite"
    repo = IndustrialRepository(db)
    repo.create_topic(
        "T1", "Electrical ratings", "Source body.",
        source_lang="en-US", status="Approved", source_refs=["SPEC#1"]
    )
    repo.create_publication("PUB1", "Demo Drive Manual", ["T1"], source_lang="en-US")
    repo.add_block(
        "B1", "T1", "paragraph", position=1,
        text="Install the drive vertically.", status="Approved", source_refs=["SPEC#1"]
    )
    repo.set_topic_title_translation("T1", "it-IT", "Valori nominali elettrici", status="Reviewed")
    repo.set_block_translation("B1", "it-IT", target_text, status="Reviewed")
    repo.close()
    return path

# ------------------------------------------------------------------
# 1) Production/test release safety
# ------------------------------------------------------------------
fixture_project = make_project(
    OUT / "fixture_project",
    target_text="TRADUZIONE DI COLLAUDO: Installare il convertitore verticalmente.",
)

fixture_out = OUT / "fixture_release"
fixture_out.mkdir()
production_blocked = False
production_error = ""
try:
    create_release_bundle(
        fixture_project, "PUB1", target_langs=["it-IT"],
        released_by="Regression R15", release_id="REL-PROD-BLOCK",
        release_mode="production", out_dir=fixture_out,
    )
except RuntimeError as e:
    production_blocked = True
    production_error = str(e)

check("Production release blocks ManualSync fixture marker", production_blocked, production_error)
check("Production block explains test fixture", "TRADUZIONE DI COLLAUDO" in production_error, production_error)

test_bundle = create_release_bundle(
    fixture_project, "PUB1", target_langs=["it-IT"],
    released_by="Regression R15", release_id="REL-TEST-PASS",
    release_mode="test", out_dir=OUT / "test_release",
)
test_zip = Path(test_bundle["release_zip"])
check("Internal test release with fixture marker is allowed", test_zip.exists() and test_zip.stat().st_size > 0, test_zip)

with zipfile.ZipFile(test_zip) as z:
    manifest = json.loads(z.read("RELEASE_MANIFEST.json"))
    check("Test release manifest is explicitly TEST", manifest.get("release_mode") == "TEST", manifest.get("release_mode"))
    check("Test release manifest uses R15 build", manifest.get("build") == PRODUCT_BUILD, manifest.get("build"))
    it_art = next(a for a in manifest["artifacts"] if a["language"] == "it-IT" and a["type"] == "DOCX")
    it_doc = OUT / "test_it.docx"
    it_doc.write_bytes(z.read(it_art["path"]))
    val = _validate_customer_docx(it_doc, release_mode="test")
    check("Test validator reports fixture hits", val["test_fixture_hits"] > 0, val)

normal_project = make_project(
    OUT / "normal_project",
    target_text="Installare il convertitore verticalmente.",
)
prod_bundle = create_release_bundle(
    normal_project, "PUB1", target_langs=["it-IT"],
    released_by="Regression R15", release_id="REL-PROD-PASS",
    release_mode="production", out_dir=OUT / "prod_release",
)
prod_zip = Path(prod_bundle["release_zip"])
check("Clean production release is allowed", prod_zip.exists() and prod_zip.stat().st_size > 0, prod_zip)

with zipfile.ZipFile(prod_zip) as z:
    manifest = json.loads(z.read("RELEASE_MANIFEST.json"))
    check("Production manifest is explicitly PRODUCTION", manifest.get("release_mode") == "PRODUCTION", manifest.get("release_mode"))
    names = set(z.namelist())
    declared = {"RELEASE_MANIFEST.json"}
    declared.update(x["path"] for x in manifest["artifacts"])
    declared.update(x["path"] for x in manifest["audit_files"])
    check("Production ZIP equals manifest scope", names == declared, {"names": sorted(names), "declared": sorted(declared)})
    check("Production artifact checksums valid", all(hashlib.sha256(z.read(x["path"])).hexdigest() == x["sha256"] for x in manifest["artifacts"]))
    check("Production audit checksums valid", all(hashlib.sha256(z.read(x["path"])).hexdigest() == x["sha256"] for x in manifest["audit_files"]))

# ------------------------------------------------------------------
# 2) Commercial UX: main tables must hide raw implementation fields
# ------------------------------------------------------------------
wp = pd.DataFrame([{
    "target_lang":"it-IT","work_segments":1993,"work_words":4112,
    "auto_reuse_locked_segments":66,"linguistic_review":5,
    "technical_review":59,"new_translation":1929,
    "package_zip":"internal.zip","package_sha256":"deadbeef",
}])
fw = _friendly_work_packages(wp)
check("Work package UI uses friendly Italian columns", list(fw.columns) == [
    "Lingua","Segmenti da lavorare","Parole","Riuso TM","Ripetizioni riusate","Non traducibili",
    "Revisione linguistica","Revisione tecnica","Nuova traduzione"
], list(fw.columns))
check("Work package UI hides package hash/path", "package_sha256" not in fw.columns and "package_zip" not in fw.columns, list(fw.columns))

jobs = pd.DataFrame([{
    "job_id":"JOB-1","status_label":"QA pronto","target_lang":"it-IT","vendor":"Vendor",
    "due_date":"2026-08-22","overdue":"","work_words":4112,
    "technical_review_segments":59,"qa_status":"PASS","source_version":123,
}])
fj = _friendly_jobs(jobs)
check("Jobs UI hides raw source/version fields", "source_version" not in fj.columns and "source_kind" not in fj.columns, list(fj.columns))
check("Jobs UI exposes decision fields", {"Job","Stato","Lingua","Fornitore","Scadenza","Parole","QA"}.issubset(fj.columns), list(fj.columns))

vendors = _friendly_vendors(pd.DataFrame([{"vendor_id":"V1","name":"Vendor","email":"v@example.com","active":1,"updated_at":"now","notes":"internal"}]))
check("Vendors UI hides internal notes", "notes" not in vendors.columns and "Fornitore" in vendors.columns, list(vendors.columns))

events = _friendly_events(pd.DataFrame([{"job_id":"J","event_type":"STATUS_CHANGED","from_status":"SENT","to_status":"QA_READY","actor":"A","created_at":"now","detail_json":"{}"}]))
check("Events UI hides detail_json", "detail_json" not in events.columns and "Evento" in events.columns, list(events.columns))

matrix = _friendly_readiness_matrix(pd.DataFrame([{
    "language":"it-IT","role":"TARGET","status":"READY","missing_translations":0,
    "stale_translations":0,"unreviewed_translations":0,"open_jobs":0,
    "qa_blocked_jobs":0,"overdue_jobs":0,"open_ccms_reviews":0,"grounding_issues":0,"issues":""
}]))
check("Readiness UI uses Italian labels", {"Lingua","Ruolo","Stato","Traduzioni mancanti","QA bloccati"}.issubset(matrix.columns), list(matrix.columns))

arts = _friendly_release_artifacts(pd.DataFrame([{"language":"it-IT","type":"DOCX","path":"ARTIFACTS/a.docx","sha256":"abc","bytes":2048}]))
check("Release artifact UI hides sha256", "sha256" not in arts.columns and list(arts.columns) == ["Lingua","Formato","File","Dimensione (KB)"], list(arts.columns))
check("Release artifact UI converts bytes to KB", float(arts.iloc[0]["Dimensione (KB)"]) == 2.0, arts.to_dict(orient="records"))

summary = {
    "build": PRODUCT_BUILD,
    "tests": len(results),
    "pass": sum(r["status"] == "PASS" for r in results),
    "fail": sum(r["status"] == "FAIL" for r in results),
    "results": results,
}
(ROOT / "RISULTATO_REGRESSIONE_DESKTOP_R15.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(ROOT / "RISULTATO_REGRESSIONE_DESKTOP_R15.txt").write_text(
    "\n".join(f"{r['status']} | {r['name']} | {r['detail']}" for r in results)
    + f"\n\nTOTAL {summary['pass']}/{summary['tests']} PASS\n",
    encoding="utf-8",
)
print(json.dumps({"tests": summary["tests"], "pass": summary["pass"], "fail": summary["fail"]}, indent=2))
