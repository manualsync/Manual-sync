from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Windows consoles default to a legacy codepage (e.g. cp1252) that cannot encode
# characters used in some check details (e.g. "≠", "✅"); force UTF-8 stdio so the
# final report print never crashes after all checks have already run and passed.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
results=[]

def check(name, cond, detail=''):
    ok=bool(cond)
    results.append({'test':name,'status':'PASS' if ok else 'FAIL','detail':str(detail)})
    if not ok:
        raise AssertionError(f'{name}: {detail}')

build=(BASE/'BUILD_WINDOWS.ps1').read_text(encoding='utf-8')
installer=(BASE/'INSTALLA_MANUALSYNC.ps1').read_text(encoding='utf-8')
validator=(BASE/'VERIFICA_INSTALLAZIONE_PILOTA.ps1').read_text(encoding='utf-8')
launcher=(BASE/'manualsync_launcher.py').read_text(encoding='utf-8')
desktop=(BASE/'manualsync_desktop.py').read_text(encoding='utf-8')
app=(BASE/'app.py').read_text(encoding='utf-8')

check('Final setup name', 'ManualSync_Setup_FINAL_CANDIDATE.zip' in build)
check('Final portable name', 'ManualSync_Portable_FINAL_CANDIDATE.zip' in build)
check('Final build id centralized', "$BuildId = '1.0.0-final-candidate-20260822'" in build)
check('Acceptance module packaged', "'commercial_acceptance.py'" in build)
check('Launcher forwards acceptance smoke', '--acceptance-smoke' in launcher)
check('Desktop exposes acceptance smoke', '--acceptance-smoke' in desktop and 'run_acceptance_smoke' in desktop)
check('Exact setup ZIP is re-extracted', 'ExtractToDirectory($InstallerZip, $ZipProbe)' in build)
check('Exact extracted ZIP resource checked', "@('--resource-check','--headless-smoke','--acceptance-smoke')" in build)
check('Payload SHA256 manifest generated', 'PAYLOAD_MANIFEST_SHA256.json' in build and 'Get-FileHash' in build)
check('Installer validates source payload manifest', 'Assert-PayloadManifest $Payload $PayloadManifest' in installer)
check('Installer validates staging payload manifest', 'Assert-PayloadManifest $Stage $PayloadManifest' in installer)
check('Installer validates exact installed tree after atomic swap', 'Assert-PayloadManifest $InstallDir $PayloadManifest' in installer)
check('Installer final resource check', "$FinalResource" in installer and '--resource-check' in installer)
check('Installer final acceptance', "$FinalAcceptance" in installer and '--acceptance-smoke' in installer)
check('Pilot validator hashes installed payload', 'Check-PayloadManifest' in validator and 'Get-FileHash' in validator)
check('Pilot validator runs acceptance', 'Commercial acceptance installato' in validator and '--acceptance-smoke' in validator)
check('Pilot validator checks build identity', 'Build installata corretta' in validator and '1.0.0-final-candidate-20260822' in validator)
check('Job invalid actions handled in-page', '_JOB_ACTIONS_BY_STATE' in app and 'Azione **{action' in app)
check('Vendor is explicitly optional', 'Vendor ID — opzionale' in app)
check('Release mode change invalidates prior action', '_release_mode_reset' in app and 'interactive=False' in app)
check('Production help mentions linguistic safety', 'anomalie linguistiche cross-language' in app)
check('Builder clears stale PASS/release before build', "status='BUILDING'" in build and "Remove-Item -Recurse -Force (Join-Path $Root 'release')" in build)
check('Builder validates/recreates stale build venv', "Rimuove ambiente di build non conforme" in build and "assert sys.version_info[:2]==(3,12)" in build)
check('Builder runs real industrial alignment gate', 'regressione_alignment_real_world.py' in build)
check('Builder runs alignment confidence policy gate', 'regressione_alignment_policy.py' in build)
check('Launcher disables runtime bytecode writes', 'PYTHONDONTWRITEBYTECODE' in launcher and 'PYTHONUTF8' in launcher)
check('Legacy alignment 80-89 is review-only', "penalty = 0.0 if conf >= 90.0" in app and "max(1.0, 90.0 - conf)" in app)
check('Review UI exposes alignment confidence', 'Affidabilità allineamento (%)' in app)

proc=subprocess.run([sys.executable, str(BASE/'commercial_acceptance.py')], capture_output=True, text=True, timeout=90)
acceptance_detail = f"returncode={proc.returncode}"
try:
    payload = json.loads(proc.stdout)
    acceptance_detail = f"{payload.get('pass', 0)}/{payload.get('tests', 0)} PASS; status={payload.get('status', 'UNKNOWN')}"
except Exception:
    if proc.stderr.strip():
        acceptance_detail += '; ' + proc.stderr.strip()[-500:]
check('Commercial acceptance executable', proc.returncode == 0, acceptance_detail)

summary={'build':'ManualSync 1.0 FINAL CANDIDATE source gate','tests':len(results),'pass':sum(x['status']=='PASS' for x in results),'fail':sum(x['status']=='FAIL' for x in results),'results':results}
(BASE/'RISULTATO_FINAL_CANDIDATE.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_FINAL_CANDIDATE.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
