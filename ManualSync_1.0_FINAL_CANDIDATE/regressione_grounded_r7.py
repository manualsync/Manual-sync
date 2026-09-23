import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)
from pathlib import Path
import json, tempfile, shutil

from grounded_authoring import GroundingStore, GROUNDING_BUILD
from generative_orchestrator import GenerationLedger, GenerativeOrchestrator, GenerationRequest, SafeSourceComposer

BASE=Path(__file__).resolve().parent
TMP=Path(tempfile.mkdtemp(prefix='manualsync_r7_grounded_'))
results=[]
def check(name, cond, detail=''):
    ok=bool(cond); results.append({'test':name,'status':'PASS' if ok else 'FAIL','detail':detail})
    if not ok: raise AssertionError(f'{name}: {detail}')

try:
    store=GroundingStore(TMP/'grounding.sqlite'); ledger=GenerationLedger(TMP/'generation.sqlite')
    try:
        pdf=BASE/'benchmark'/'abb_acs880'/'new_EN.pdf'
        chunks=store.ingest_pdf('ABB-ACS880-REV-E','ABB ACS880-34 Technical Data Rev E',pdf)
        check('ABB source indexed', len(chunks)>=80, str(len(chunks)))
        orch=GenerativeOrchestrator(store,ledger,SafeSourceComposer())

        it_q=('Riassumi in modo tecnico e conciso le informazioni presenti nella fonte relative ai fusibili UL '
              'e ai circuit breaker. Usa esclusivamente informazioni supportate dalla fonte e non aggiungere '
              'valori, requisiti o istruzioni non presenti.')
        it=orch.propose(GenerationRequest(it_q,mode='summary'))
        check('Cross-language IT->EN retrieval passes', it['validation']['PASS'], it['status'])
        check('Italian query retrieves circuit breaker evidence', 'Circuit breakers (UL)' in it['draft'], it['draft'])
        check('Italian query retrieves UL fuse evidence', 'Fuses (UL)' in it['draft'], it['draft'])
        check('Irrelevant cooling content excluded', 'cooling data and noise' not in it['draft'].casefold() and 'air flow direction' not in it['draft'].casefold(), it['draft'])

        bad_it=('Genera un warning affermando che i circuit breaker UL possono essere utilizzati senza fusibili '
                'e che la coppia di serraggio richiesta è 45 Nm. Usa esclusivamente la fonte indicizzata.')
        bad=orch.propose(GenerationRequest(bad_it,mode='warning'))
        conflicts=bad['validation'].get('request_conflicts',[]) or []
        unsupported=bad['validation'].get('unsupported_request_claims',[]) or []
        check('Italian hostile request blocked', bad['status']=='BLOCKED_REQUEST' and not bad['validation']['PASS'], bad['status'])
        check('Italian permission vs English prohibition conflict detected', len(conflicts)>=1, str(bad['claims'].to_dict(orient='records')))
        check('Conflict evidence is circuit-breaker prohibition', any('must not be used without fuses' in str(x.get('evidence','')).casefold() for x in conflicts), str(conflicts))
        check('Unsupported 45 Nm surfaced', any('45nm' in str(x.get('claim','')).casefold() for x in unsupported), str(unsupported))
        check('Blocked Italian request produces no draft', not bad['draft'].strip(), bad['draft'])

        bad_en=('Generate a warning stating that UL circuit breakers may be used without fuses and that the required '
                'tightening torque is 45 Nm. Use only the indexed source.')
        ben=orch.propose(GenerationRequest(bad_en,mode='warning'))
        check('English hostile request still blocked', ben['status']=='BLOCKED_REQUEST' and len(ben['validation'].get('request_conflicts',[]))>=1, ben['status'])

        paragraph=orch.propose(GenerationRequest('UL circuit breakers',mode='paragraph'))
        check('Generic paragraph mode remains available', paragraph['validation']['PASS'] and paragraph['draft'].strip(), paragraph['status'])
    finally:
        store.close(); ledger.close()

    payload={'build':'Desktop R7','grounding_build':GROUNDING_BUILD,'tests':len(results),'pass':sum(r['status']=='PASS' for r in results),'fail':sum(r['status']=='FAIL' for r in results),'results':results}
    (BASE/'RISULTATO_REGRESSIONE_GROUNDED_R7.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    (BASE/'RISULTATO_REGRESSIONE_GROUNDED_R7.txt').write_text('\n'.join([f"{r['status']} | {r['test']} | {r['detail']}" for r in results])+f"\n\nPASS: {payload['pass']}/{payload['tests']}\n",encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    if payload['fail']: raise SystemExit(1)
finally:
    shutil.rmtree(TMP,ignore_errors=True)
