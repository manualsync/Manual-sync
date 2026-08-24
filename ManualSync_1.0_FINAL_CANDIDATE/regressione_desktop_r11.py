from __future__ import annotations
import json, shutil
from pathlib import Path

from project_lifecycle import create_project
from ccms_industrial import IndustrialRepository
from release_readiness import assess_release_readiness, export_readiness_report, READINESS_BUILD
from translation_impact import SourceUnit, units_to_segments

BASE=Path(__file__).resolve().parent
OUT=BASE/'regressione_desktop_r11_out'
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir()
res=[]
def check(name, cond, detail=''):
    ok=bool(cond); res.append({'test':name,'status':'PASS' if ok else 'FAIL','detail':str(detail)})
    if not ok: raise AssertionError(f'{name}: {detail}')

check('R11/R12 readiness lineage', READINESS_BUILD.startswith('0.26.'), READINESS_BUILD)

# Regression 1: single-character meaningful technical units must not disappear.
segments, meta = units_to_segments([
    SourceUnit('U1','block','B1','1','B1#v1'),
    SourceUnit('U2','block','B2','A','B2#v1'),
    SourceUnit('U3','block','B3','-','B3#v1'),
])
seg_sources=[s.text for s in segments]
check('Single digit kept in CAT coverage', '1' in seg_sources, seg_sources)
check('Single letter kept in CAT coverage', 'A' in seg_sources, seg_sources)
check('Punctuation-only unit excluded', '-' not in seg_sources, seg_sources)

# Regression 2: source-invariant blocks do not create false missing translations.
p=create_project(OUT/'projects',project_name='R11 Readiness',client='Test',product='Drive',manual_code='R11',revision='1',source_lang='en-US',target_langs=['it-IT'],owner='QA')
repo=IndustrialRepository(p/'ManualSync_CCMS.sqlite')
try:
    repo.create_topic('T1','Ratings','legacy',status='Approved',source_refs=['SRC#1'])
    repo.create_publication('PUB1','R11 Fixture',['T1'],source_lang='en-US')
    repo.add_block('B-TEXT','T1','paragraph',position=1,text='Install vertically.',status='Approved',source_refs=['SRC#1'])
    repo.add_block('B-NUM','T1','paragraph',position=2,text='1',status='Approved',source_refs=['SRC#2'])
    repo.add_block('B-LABEL','T1','paragraph',position=3,text='A',status='Approved',source_refs=['SRC#3'])
    repo.add_block('B-MISS','T1','paragraph',position=4,text='Cooling fan replacement.',status='Approved',source_refs=['SRC#4'])
    repo.set_block_translation('B-TEXT','it-IT','Installare verticalmente.',status='Reviewed')
    repo.set_topic_title_translation('T1','it-IT','Valori nominali',status='Reviewed')
finally:
    repo.close()

r1=assess_release_readiness(p,'PUB1',target_langs=['it-IT'])
row=r1['matrix'].loc[r1['matrix']['language']=='it-IT'].iloc[0]
check('Only real linguistic missing block counted', int(row['missing_translations'])==1, r1['matrix'].to_dict(orient='records'))
check('Only real blocker is unreviewed', int(row['unreviewed_translations'])==1, r1['matrix'].to_dict(orient='records'))
blockers=r1['blocking_items'].to_dict(orient='records')
check('Blocking audit exposes exact object ID', len(blockers)==1 and blockers[0]['block_id']=='B-MISS', blockers)

repo=IndustrialRepository(p/'ManualSync_CCMS.sqlite')
try:
    repo.set_block_translation('B-MISS','it-IT','Sostituzione della ventola di raffreddamento.',status='Reviewed')
finally:
    repo.close()
r2=assess_release_readiness(p,'PUB1',target_langs=['it-IT'])
check('Readiness becomes READY after real blocker resolved', r2['release_ready'], r2['matrix'].to_dict(orient='records'))
check('No blocker rows remain', r2['blocking_items'].empty, r2['blocking_items'].to_dict(orient='records'))
files=export_readiness_report(r2,OUT/'audit')
payload=json.loads(Path(files['json']).read_text(encoding='utf-8'))
check('Audit JSON contains blocker detail schema', 'blocking_items' in payload, payload.keys())
check('Summary separates source and target languages', payload['summary'].get('target_languages_total')==1 and payload['summary'].get('source_ready') is True, payload['summary'])

summary={'build':'1.0.0-desktop-r12-translation-completeness-release-hardening-20260817','tests':len(res),'pass':sum(x['status']=='PASS' for x in res),'fail':sum(x['status']=='FAIL' for x in res),'results':res}
(BASE/'RISULTATO_REGRESSIONE_DESKTOP_R11.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_REGRESSIONE_DESKTOP_R11.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in res)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
