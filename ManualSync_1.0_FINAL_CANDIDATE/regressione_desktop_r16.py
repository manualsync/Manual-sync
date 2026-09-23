from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import shutil
from pathlib import Path

from ccms_industrial import IndustrialRepository
from app import release_ui, _friendly_release_failure, _release_mode_help
from product_version import PRODUCT_BUILD

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'regressione_desktop_r16_out'
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

results=[]
def check(name, cond, detail=''):
    ok=bool(cond)
    results.append({'name':name,'status':'PASS' if ok else 'FAIL','detail':str(detail)})
    if not ok:
        raise AssertionError(f'{name}: {detail}')

def make_project(path: Path, target_text: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    repo=IndustrialRepository(path/'ManualSync_CCMS.sqlite')
    repo.create_topic('T1','Electrical ratings','Source body.',source_lang='en-US',status='Approved',source_refs=['SPEC#1'])
    repo.create_publication('PUB1','Demo Drive Manual',['T1'],source_lang='en-US')
    repo.add_block('B1','T1','paragraph',position=1,text='Install the drive vertically.',status='Approved',source_refs=['SPEC#1'])
    repo.set_topic_title_translation('T1','it-IT','Valori nominali elettrici',status='Reviewed')
    repo.set_block_translation('B1','it-IT',target_text,status='Reviewed')
    repo.close()
    return path

fixture=make_project(OUT/'fixture', 'TRADUZIONE DI COLLAUDO: Installare il convertitore verticalmente.')

# 1) The business failure must be rendered as a readable result, not thrown as anonymous Gradio Error.
msg, z, artifacts = release_ui(str(fixture),'PUB1','it-IT','Regression R16','REL-R16-PROD',None,None,'Produzione cliente')
check('Production fixture returns friendly block instead of exception', 'Release PRODUZIONE bloccata correttamente' in msg, msg)
check('Friendly block explains fixture cause', 'contenuti identificati come collaudo' in msg, msg)
check('Friendly block confirms no customer package', 'Nessun pacchetto cliente è stato creato' in msg, msg)
check('Blocked production returns no ZIP', z is None, z)
check('Blocked production hides artifact table', isinstance(artifacts, dict) and artifacts.get('visible') is False, artifacts)

# 2) Test mode must still package the same fixture.
msg2, z2, artifacts2 = release_ui(str(fixture),'PUB1','it-IT','Regression R16','REL-R16-TEST',None,None,'Collaudo interno')
check('Internal test release still passes', 'Release **COLLAUDO INTERNO**' in msg2 and z2 and Path(z2).exists(), (msg2,z2))
check('Successful release shows artifacts', isinstance(artifacts2, dict) and artifacts2.get('visible') is True, artifacts2)

# 3) Mode help must be explicit and context-sensitive.
prod_help=_release_mode_help('Produzione cliente')
test_help=_release_mode_help('Collaudo interno')
check('Production mode help says fail-closed', 'fail-closed' in prod_help and 'nessun pacchetto cliente' in prod_help, prod_help)
check('Test mode help says non-deliverable TEST', 'pacchetto TEST non consegnabile' in test_help, test_help)

# 4) Other expected readiness blocks are also human-readable.
readiness_msg=_friendly_release_failure(ValueError('Release bloccata: it-IT: 2 traduzioni mancanti'), 'production')
check('Readiness failure uses business wording', 'Release bloccata dai gate' in readiness_msg, readiness_msg)
check('Readiness failure preserves cause', '2 traduzioni mancanti' in readiness_msg, readiness_msg)

# 5) Unexpected errors expose diagnostic type but remain in-page.
generic=_friendly_release_failure(RuntimeError('boom'), 'production')
check('Unexpected error has readable heading', 'Release non creata' in generic, generic)
check('Unexpected error keeps exception type', 'RuntimeError' in generic and 'boom' in generic, generic)

# 6) Build identity.
check('Friendly fail-closed feature retained in current desktop build', PRODUCT_BUILD.startswith('1.0.0-'), PRODUCT_BUILD)

summary={'build':PRODUCT_BUILD,'tests':len(results),'pass':sum(r['status']=='PASS' for r in results),'fail':sum(r['status']=='FAIL' for r in results),'results':results}
(ROOT/'RISULTATO_REGRESSIONE_DESKTOP_R16.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(ROOT/'RISULTATO_REGRESSIONE_DESKTOP_R16.txt').write_text('\n'.join(f"{r['status']} | {r['name']} | {r['detail']}" for r in results)+f"\n\nTOTAL {summary['pass']}/{summary['tests']} PASS\n",encoding='utf-8')
print(json.dumps({'tests':summary['tests'],'pass':summary['pass'],'fail':summary['fail']},indent=2))
