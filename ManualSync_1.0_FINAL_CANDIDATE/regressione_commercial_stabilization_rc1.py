from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from project_lifecycle import create_project, read_project, update_project
from workflow_management import WorkflowManager
from support_diagnostics import create_support_bundle, system_health
from language_codes import canonical_lang

BASE = Path(__file__).resolve().parent
OUT = BASE / "regressione_commercial_stabilization_rc1_out"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

results: list[dict[str, str]] = []

def check(name: str, condition, detail=""):
    ok = bool(condition)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

# 1) New project collision safety / manifest atomicity.
projects = OUT / "projects"
p = create_project(projects, project_name="Commercial Pilot", client="ACME", manual_code="M-1", source_lang="EN_us", target_langs=["it_it"])
check("Project source language canonical", read_project(p)["source_lang"] == "en-US", read_project(p))
check("Project target language canonical", read_project(p)["target_langs"] == ["it-IT"], read_project(p))
try:
    create_project(projects, project_name="Commercial Pilot")
    collision = False
except FileExistsError:
    collision = True
check("Duplicate project creation blocked", collision)
update_project(p, revision="R2", target_langs=["IT-it"])
check("Atomic project manifest remains valid", read_project(p)["revision"] == "R2" and read_project(p)["target_langs"] == ["it-IT"], read_project(p))
check("No temporary manifest left behind", not any(x.name.endswith(".tmp") for x in p.iterdir()), list(p.iterdir()))

# Add a small project file to make backup checks deterministic.
(p / "sample_note.txt").write_text("commercial stabilization", encoding="utf-8")
wf = WorkflowManager(p)
try:
    archive = wf.export_project_archive(OUT / "backup")
finally:
    wf.close()
check("Project backup created", archive.exists(), archive)

# 2) Transactional restore and exact archive contract.
restore_target = OUT / "restored"
restored = WorkflowManager.restore_project_archive(archive, restore_target)
check("Project restore created final target", restored == restore_target and (restored / "ManualSync_CCMS.sqlite").exists(), restored)
check("Restored payload checksum matches", sha256(restored / "sample_note.txt") == sha256(p / "sample_note.txt"))
try:
    WorkflowManager.restore_project_archive(archive, restore_target)
    repeated_restore_blocked = False
except FileExistsError:
    repeated_restore_blocked = True
check("Restore never merges into existing non-empty project", repeated_restore_blocked)

# Tamper an archive by adding an undeclared payload file: must fail and leave no target.
extra_zip = OUT / "backup_extra.zip"
shutil.copy2(archive, extra_zip)
with zipfile.ZipFile(extra_zip, "a", compression=zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("UNDECLARED.txt", "should fail")
extra_target = OUT / "restore_extra"
try:
    WorkflowManager.restore_project_archive(extra_zip, extra_target)
    extra_rejected = False
except ValueError:
    extra_rejected = True
check("Undeclared file in backup rejected", extra_rejected)
check("Failed restore leaves no partial project", not extra_target.exists(), extra_target)

# Tamper a declared file without updating manifest: checksum must catch it and no target remains.
bad_zip = OUT / "backup_bad_hash.zip"
with zipfile.ZipFile(archive, "r") as zin, zipfile.ZipFile(bad_zip, "w", compression=zipfile.ZIP_DEFLATED) as zout:
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename == "sample_note.txt":
            data = b"tampered"
        zout.writestr(info, data)
bad_target = OUT / "restore_bad_hash"
try:
    WorkflowManager.restore_project_archive(bad_zip, bad_target)
    bad_hash_rejected = False
except ValueError:
    bad_hash_rejected = True
check("Backup checksum tampering rejected", bad_hash_rejected)
check("Checksum failure leaves no partial project", not bad_target.exists(), bad_target)

# SQLite integrity check must still catch corruption even if attacker recomputes manifest checksum/size.
corrupt_zip = OUT / "backup_corrupt_db.zip"
with zipfile.ZipFile(archive, "r") as zin:
    manifest = json.loads(zin.read("MANUALSYNC_PROJECT_MANIFEST.json"))
    files = {i.filename: zin.read(i.filename) for i in zin.infolist() if i.filename != "MANUALSYNC_PROJECT_MANIFEST.json"}
ccms_name = "ManualSync_CCMS.sqlite"
corrupt = bytearray(files[ccms_name])
# Destroy the SQLite header while keeping a deterministic file.
corrupt[:16] = b"NOT_A_SQLITE_DB!!"
files[ccms_name] = bytes(corrupt)
for meta in manifest["files"]:
    if meta["path"] == ccms_name:
        meta["bytes"] = len(files[ccms_name])
        meta["sha256"] = hashlib.sha256(files[ccms_name]).hexdigest()
with zipfile.ZipFile(corrupt_zip, "w", compression=zipfile.ZIP_DEFLATED) as zout:
    zout.writestr("MANUALSYNC_PROJECT_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    for name, data in files.items():
        zout.writestr(name, data)
corrupt_target = OUT / "restore_corrupt_db"
try:
    WorkflowManager.restore_project_archive(corrupt_zip, corrupt_target)
    db_corruption_rejected = False
except Exception:
    db_corruption_rejected = True
check("Corrupt SQLite backup rejected after checksum validation", db_corruption_rejected)
check("SQLite integrity failure leaves no partial project", not corrupt_target.exists(), corrupt_target)

# 3) Diagnostics: useful but no customer content/database disclosure.
data_home = OUT / "data_home"
(data_home / "Logs").mkdir(parents=True)
(data_home / "MANUALSYNC_APP.log").write_text("test log", encoding="utf-8")
health = system_health(data_home, p)
check("System health returns project checks", health["fail"] == 0, health["checks"])
support = create_support_bundle(data_home, p, OUT / "support")
with zipfile.ZipFile(support) as zf:
    support_names = zf.namelist()
check("Support bundle includes diagnostics", "DIAGNOSTICS.json" in support_names, support_names)
for forbidden in (".sqlite", ".pdf", ".xlf", ".xliff", ".docx"):
    check(f"Support bundle excludes customer content {forbidden}", not any(n.lower().endswith(forbidden) for n in support_names), support_names)

# 4) Commercial UI/server safety contracts.
app_source = (BASE / "app.py").read_text(encoding="utf-8")
desktop_source = (BASE / "manualsync_desktop.py").read_text(encoding="utf-8")
check("Direct app hides raw Gradio server errors", "show_error=False" in app_source)
check("Desktop runtime hides raw Gradio server errors", "show_error=False" in desktop_source)
check("Settings exposes integrity check", "Verifica integrità locale" in app_source)
check("Settings exposes support diagnostics", "Esporta diagnostica assistenza" in app_source)

# 5) Installer/upgrade contract (static safety gates; Windows execution is builder-gated separately).
installer = (BASE / "INSTALLA_MANUALSYNC.ps1").read_text(encoding="utf-8")
uninstaller = (BASE / "DISINSTALLA_MANUALSYNC.ps1").read_text(encoding="utf-8")
check("Installer preflight resource check", "--resource-check" in installer)
check("Installer preflight smoke test", "--headless-smoke" in installer)
check("Installer has previous-version rollback", ".ManualSync.previous." in installer and "avvio rollback" in installer)
check("Installer refuses update while app runs", "Get-Process -Name 'ManualSync'" in installer)
check("Uninstaller preserves ManualSyncData", "ManualSyncData" in uninstaller and "Remove-Item -Recurse -Force $DataDir" not in uninstaller)
check("Installer does not delete ManualSyncData", "Remove-Item -Recurse -Force $DataDir" not in installer)

# 6) Run the full E2E scenario three consecutive times. Each run rebuilds its own fixture.
for idx in range(1, 4):
    proc = subprocess.run([sys.executable, str(BASE / "regressione_e2e_v1.py")], cwd=BASE, capture_output=True, text=True)
    detail = (proc.stdout + "\n" + proc.stderr)[-1500:]
    check(f"End-to-end consecutive cycle {idx}/3", proc.returncode == 0, detail)

summary = {
    "build": "ManualSync 1.0 Commercial Stabilization RC1",
    "tests": len(results),
    "pass": sum(r["status"] == "PASS" for r in results),
    "fail": sum(r["status"] == "FAIL" for r in results),
    "results": results,
}
(BASE / "RISULTATO_COMMERCIAL_STABILIZATION_RC1.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(BASE / "RISULTATO_COMMERCIAL_STABILIZATION_RC1.txt").write_text(
    "MANUALSYNC 1.0 — COMMERCIAL STABILIZATION RC1\n\n"
    + "\n".join(f"{r['status']} | {r['test']} | {r['detail']}" for r in results)
    + f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",
    encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, indent=2))
