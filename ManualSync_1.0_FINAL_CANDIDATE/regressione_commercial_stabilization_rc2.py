from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "regressione_commercial_stabilization_rc2_out"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

results: list[dict[str, str]] = []

def check(name: str, condition, detail=""):
    ok = bool(condition)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")

app_source = (BASE / "app.py").read_text(encoding="utf-8")
launcher_source = (BASE / "manualsync_launcher.py").read_text(encoding="utf-8")
desktop_source = (BASE / "manualsync_desktop.py").read_text(encoding="utf-8")
installer_source = (BASE / "INSTALLA_MANUALSYNC.ps1").read_text(encoding="utf-8")
validator_source = (BASE / "VERIFICA_INSTALLAZIONE_PILOTA.ps1").read_text(encoding="utf-8")
uninstaller_source = (BASE / "DISINSTALLA_MANUALSYNC.ps1").read_text(encoding="utf-8")
build_source = (BASE / "BUILD_WINDOWS.ps1").read_text(encoding="utf-8")

# RC1 incident 1: stale desktop shortcut arguments from RC1.9.
check("Installer deletes stale shortcut before recreation", "Remove-Item -LiteralPath $Path" in installer_source)
check("Installer explicitly clears shortcut arguments", "$Shortcut.Arguments = ''" in installer_source)
check("Installer validates shortcut target", "Assert-Shortcut" in installer_source and "Target collegamento non valido" in installer_source)
check("Installer validates shortcut working directory", "Working directory collegamento non valida" in installer_source)
check("Installer validates shortcut arguments", "Argomenti residui nel collegamento" in installer_source)
check("Pilot validator checks Desktop shortcut content", "Check-Shortcut 'Shortcut Desktop valido'" in validator_source)
check("Pilot validator checks Start Menu shortcut content", "Check-Shortcut 'Shortcut Start Menu valido'" in validator_source)
check("Pilot validator rejects non-empty arguments", "$argsOk = [string]::IsNullOrWhiteSpace" in validator_source)

# Defense in depth: an old .lnk must not kill the new launcher even before rewrite.
check("Launcher filters unsupported legacy arguments", "forwarded_args = [a for a in sys.argv[1:] if a in supported]" in launcher_source)
check("Desktop parser tolerates legacy unknown arguments", "parse_known_args()" in desktop_source and "Ignored legacy/unknown launcher arguments" in desktop_source)

# Lifecycle: ManualSync.exe remains alive while the actual desktop process is alive.
check("Launcher waits for desktop child", "return subprocess.call(" in launcher_source)
check("Launcher no longer fire-and-forgets desktop", "subprocess.Popen(" not in launcher_source)
check("Installer detects legacy pythonw ManualSync", "Get-CimInstance Win32_Process" in installer_source and "manualsync_desktop\\.py" in installer_source)
check("Uninstaller detects legacy pythonw ManualSync", "Get-CimInstance Win32_Process" in uninstaller_source and "manualsync_desktop\\.py" in uninstaller_source)

# RC1 incident 2: New Project panel was below quick actions and outside the initial viewport.
panel_pos = app_source.find('elem_id="new-project-panel"')
card_pos = app_source.find('project_card=gr.HTML(_project_card(INITIAL_PROJECT))')
quick_pos = app_source.find("gr.HTML(\"<div class='ms-quick-head'")
check("New project panel exists", panel_pos >= 0)
check("New project panel is before project card", 0 <= panel_pos < card_pos, (panel_pos, card_pos))
check("New project panel is before quick actions", 0 <= panel_pos < quick_pos, (panel_pos, quick_pos))
check("New project click scrolls form into view", "scroll_to_output=True" in app_source and "home_new_project.click" in app_source)
check("New project click hides progress chrome", 'show_progress="hidden"' in app_source)
check("Create-project event is single-flight", 'trigger_mode="once"' in app_source and 'concurrency_limit=1' in app_source)
check("Duplicate project create is idempotent", '_existing_project_by_name' in app_source and 'Nessun duplicato è stato creato' in app_source)

# Actual project creation callback on a clean isolated HOME.
with tempfile.TemporaryDirectory(prefix="msrc2-home-") as td:
    env = os.environ.copy()
    env["HOME"] = td
    env["USERPROFILE"] = td
    code = r'''
import json
from pathlib import Path
import app
out = app.create_project_ui("RC2 Pilot Project", "Client", "Drive", "MAN-1", "R1", "EN_us", "it_it, de-de", "Owner")
p = Path(out[0])
assert p.exists()
assert (p / "ManualSync_Project.json").exists()
meta=json.loads((p/"ManualSync_Project.json").read_text(encoding="utf-8"))
assert meta["source_lang"] == "en-US"
assert meta["target_langs"] == ["de-DE", "it-IT"]
assert "RC2 Pilot Project" in out[1]
print(p)
'''
    proc = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env, capture_output=True, text=True)
    check("Create-project UI callback works on clean data home", proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:])

# Runtime must actually tolerate the stale positional argument that broke RC1.
proc = subprocess.run([sys.executable, str(BASE / "manualsync_desktop.py"), "legacy_desktop_launcher.py", "--headless-smoke"], cwd=BASE, capture_output=True, text=True)
check("Desktop runtime ignores stale legacy argument and still starts", proc.returncode == 0 and "PASS http://127.0.0.1:" in proc.stdout, (proc.stdout + proc.stderr)[-1500:])

# Packaged desktop must use same footer policy as direct source launch.
check("Packaged desktop disables Gradio footer links", "footer_links=[]" in desktop_source)

# RC1 incident 3: extraction path was amplified by a very long setup filename and build-tree nesting.
check("Customer setup ZIP name is short", "ManualSync_Setup_FINAL_CANDIDATE.zip" in build_source)
check("Portable ZIP name is short", "ManualSync_Portable_FINAL_CANDIDATE.zip" in build_source)
check("Build confines cache cleanup to app payload", "$DistApp = Join-Path $Dist 'app'" in build_source and "Ripulisce solo cache applicative" in build_source)
check("Build never recursively cleans the entire runtime payload", "Get-ChildItem -LiteralPath $Dist -File -Recurse" not in build_source)
check("Build revalidates resource check after cleanup", "Resource check post-cleanup" in build_source and "$ResAfterCleanup" in build_source)
check("Build revalidates smoke after cleanup", "Smoke test post-cleanup" in build_source and "$SmokeAfterCleanup" in build_source)
check("Exact installer payload is resource-checked before ZIP", "Verifica il payload installer esatto prima dello ZIP" in build_source and "$InstallerPayloadResource" in build_source)
check("Exact installer payload is smoke-tested before ZIP", "$InstallerPayloadSmoke" in build_source)
check("Build enforces internal ZIP path guard", "Path ZIP max interno" in build_source and "Length -gt 160" in build_source)
check("Build emits setup SHA256", "ManualSync_Setup_FINAL_CANDIDATE.sha256.txt" in build_source and "Get-FileHash" in build_source)

# Installation rollback/data preservation still present after the RC2 edits.
check("Installer keeps rollback", ".ManualSync.previous." in installer_source and "avvio rollback" in installer_source)
check("Installer preserves ManualSyncData", "Remove-Item -Recurse -Force $DataDir" not in installer_source)
check("Uninstaller preserves ManualSyncData", "Remove-Item -Recurse -Force $DataDir" not in uninstaller_source)

# The full RC1 commercial regression is executed as an independent preceding
# gate by BUILD_WINDOWS.ps1; RC2 intentionally avoids nesting it a second time.
check("RC1 commercial regression remains wired into builder", "regressione_commercial_stabilization_rc1.py" in build_source)

summary = {
    "build": "ManualSync 1.0 Commercial Stabilization RC2.2",
    "tests": len(results),
    "pass": sum(r["status"] == "PASS" for r in results),
    "fail": sum(r["status"] == "FAIL" for r in results),
    "results": results,
}
(BASE / "RISULTATO_COMMERCIAL_STABILIZATION_RC2.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(BASE / "RISULTATO_COMMERCIAL_STABILIZATION_RC2.txt").write_text(
    "MANUALSYNC 1.0 — COMMERCIAL STABILIZATION RC2\n\n"
    + "\n".join(f"{r['status']} | {r['test']} | {r['detail']}" for r in results)
    + f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",
    encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, indent=2))
