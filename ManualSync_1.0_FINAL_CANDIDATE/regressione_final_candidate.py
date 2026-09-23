from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

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

import sys
sys.path.insert(0, str(BASE))
from product_version import PRODUCT_BUILD

build=(BASE/'BUILD_WINDOWS.ps1').read_text(encoding='utf-8')
installer=(BASE/'INSTALLA_MANUALSYNC.ps1').read_text(encoding='utf-8')
validator=(BASE/'VERIFICA_INSTALLAZIONE_PILOTA.ps1').read_text(encoding='utf-8')
launcher=(BASE/'manualsync_launcher.py').read_text(encoding='utf-8')
desktop=(BASE/'manualsync_desktop.py').read_text(encoding='utf-8')
app=(BASE/'app.py').read_text(encoding='utf-8')

check('Final setup name', 'ManualSync_Setup_FINAL_CANDIDATE.zip' in build)
check('Final portable name', 'ManualSync_Portable_FINAL_CANDIDATE.zip' in build)
# "Centralized" used to be asserted by requiring the literal build id in
# BUILD_WINDOWS.ps1 -- which is the opposite of centralized: it froze a
# second copy of the string and made the suite refuse any release that
# stopped duplicating it. Gate 1 made product_version.py the one source;
# this now checks that, and that no copy has come back.
check('Final build id centralized', '$BuildId = $BuildIdMatch.Groups[1].Value' in build
      and 'PRODUCT_BUILD' in build)
check('Final build id not restated in the build script',
      PRODUCT_BUILD not in build, PRODUCT_BUILD)
# The build no longer carries a file allowlist to grep. Packaging is now
# decided by packaging_payload.py, so ask IT what ships -- a stronger
# check than matching a quoted filename in a script.
import packaging_payload as _pp
check('Acceptance module packaged', 'commercial_acceptance.py' in _pp.payload_entries())
check('Locale catalogues packaged',
      {'locales/it.json', 'locales/en.json'} <= set(_pp.payload_entries()))
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
check('Pilot validator checks build identity', 'Build installata corretta' in validator
      and 'Get-ProductBuildId' in validator)
check('Pilot validator compares, rather than restating the build id',
      PRODUCT_BUILD not in validator, PRODUCT_BUILD)
check('Installer records a derived build id', PRODUCT_BUILD not in installer
      and 'Get-ProductBuildId' in installer, PRODUCT_BUILD)
check('Job invalid actions handled in-page', '_JOB_ACTIONS_BY_STATE' in app and 'Azione **{action' in app)
check('Vendor is explicitly optional', 'Vendor ID — opzionale' in app)
check('Release mode change invalidates prior action', '_release_mode_reset' in app and 'interactive=False' in app)
check('Production help mentions linguistic safety', 'anomalie linguistiche cross-language' in app)
check('Builder clears stale PASS/release before build', "status='BUILDING'" in build and "Remove-Item -Recurse -Force (Join-Path $Root 'release')" in build)
check('Builder validates/recreates stale build venv', "Rimuove ambiente di build non conforme" in build and "assert sys.version_info[:2]==(3,12)" in build)
# These used to be asserted by name, one line per suite, alongside the
# twenty-odd Steps the build enumerated by hand. The build now discovers
# every regressione_*.py, so naming two of them proves less than checking
# that discovery is what happens -- and that the suites still exist.
check('Builder discovers every regression suite', "-Filter 'regressione_*.py'" in build)
check('Builder refuses a suspiciously short suite list', 'Elenco suite sospetto' in build)
check('Builder excludes only the browser suites', 'regressione_browser_impact_center.py' in build)
for _suite in ('regressione_alignment_real_world.py', 'regressione_alignment_policy.py',
               'regressione_commercial_build.py'):
    check(f'Suite present for discovery: {_suite}', (BASE/_suite).exists())
check('Launcher disables runtime bytecode writes', 'PYTHONDONTWRITEBYTECODE' in launcher and 'PYTHONUTF8' in launcher)
check('Legacy alignment 80-89 is review-only', "penalty = 0.0 if conf >= 90.0" in app and "max(1.0, 90.0 - conf)" in app)
check('Review UI exposes alignment confidence', 'Affidabilità allineamento (%)' in app)

# text=True alone decodes the CHILD's output with the locale codepage (cp1252 on
# this machine), the same trap lines 8-12 avoid for our own stdio. Acceptance
# prints message codes rendered with markers like "ℹ️", whose bytes
# cp1252 cannot map: the decode blew up inside subprocess' reader thread, so the
# payload arrived empty and the detail silently fell back to the bare returncode.
proc=subprocess.run([sys.executable, str(BASE/'commercial_acceptance.py')], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
acceptance_detail = f"returncode={proc.returncode}"
acceptance_payload = None
try:
    acceptance_payload = json.loads(proc.stdout)
    payload = acceptance_payload
    acceptance_detail = f"{payload.get('pass', 0)}/{payload.get('tests', 0)} PASS; status={payload.get('status', 'UNKNOWN')}"
except Exception:
    if proc.stderr.strip():
        acceptance_detail += '; ' + proc.stderr.strip()[-500:]
check('Commercial acceptance executable', proc.returncode == 0, acceptance_detail)
# The decode failure never touched the assertion above -- returncode survives it --
# so the bug stayed invisible for two builds. Assert we actually READ the payload:
# a future decode regression now fails instead of quietly degrading the detail.
check('Commercial acceptance output decoded and parsed',
      isinstance(acceptance_payload, dict) and int(acceptance_payload.get('tests', 0)) > 0,
      acceptance_detail)
check('Commercial acceptance non-ASCII markers survive decoding',
      '�' not in proc.stdout,
      'nessun carattere di sostituzione' if '�' not in proc.stdout else 'output corrotto nel decode')

# The defect reproduced directly: a child emitting exactly the characters that
# broke it must round-trip intact. -X utf8 makes the child write UTF-8 the way
# commercial_acceptance.py does, independently of the console codepage.
_unicode_sample = 'ℹ️ già è ≠ ✅'
_u = subprocess.run([sys.executable, '-X', 'utf8', '-c',
                     f'import sys; sys.stdout.write({_unicode_sample!r}); sys.stderr.write({_unicode_sample!r})'],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
check('Child UTF-8 stdout decoded without loss', _u.stdout == _unicode_sample, repr(_u.stdout))
check('Child UTF-8 stderr decoded without loss', _u.stderr == _unicode_sample, repr(_u.stderr))

summary={'build':'ManualSync 1.0 FINAL CANDIDATE source gate','tests':len(results),'pass':sum(x['status']=='PASS' for x in results),'fail':sum(x['status']=='FAIL' for x in results),'results':results}
(BASE/'RISULTATO_FINAL_CANDIDATE.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_FINAL_CANDIDATE.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
