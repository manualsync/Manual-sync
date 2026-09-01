from __future__ import annotations
import json, shutil, zipfile
from pathlib import Path

from ccms_industrial import IndustrialRepository, create_industrial_demo
from release_readiness import assess_release_readiness, READINESS_BUILD
from release_manager import create_release_bundle, RELEASE_BUILD
from project_lifecycle import create_project, read_project, project_health, PROJECT_BUILD

BASE=Path(__file__).resolve().parent
OUT=BASE/'regressione_v26_v28_out'
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir()
res=[]
def check(name,cond,detail=''):
    ok=bool(cond); res.append({'test':name,'status':'PASS' if ok else 'FAIL','detail':str(detail)})
    if not ok: raise AssertionError(f'{name}: {detail}')

check('Readiness build',READINESS_BUILD.startswith(('0.26.','0.27.')),READINESS_BUILD)
check('Release build',RELEASE_BUILD.startswith(('0.27.','0.28.')),RELEASE_BUILD)
check('Project lifecycle build',PROJECT_BUILD.startswith('0.28.0'),PROJECT_BUILD)

p=create_project(OUT/'projects',project_name='Demo Release',client='ACME',product='Drive',manual_code='MAN-001',revision='R2',source_lang='en-US',target_langs=['it-IT'],owner='Docs')
check('Project created', (p/'ManualSync_CCMS.sqlite').exists() and (p/'ManualSync_Workflow.sqlite').exists(),p)
check('Project metadata persists',read_project(p)['revision']=='R2',read_project(p))
check('Project health available',project_health(p)['total_jobs']==0,project_health(p))

# Build a release fixture in same project DB
repo=IndustrialRepository(p/'ManualSync_CCMS.sqlite')
try:
    repo.create_component('C1','Safety','Disconnect power and wait 15 minutes.',source_refs=['SAFE#1'])
    repo.create_topic('T1','Install','legacy',status='Approved',source_refs=['MAN#1'])
    repo.create_publication('PUB1','Install Manual',['T1'],source_lang='en-US')
    repo.add_block('B1','T1','paragraph',position=1,text='Install vertically.',status='Approved',source_refs=['MAN#1'])
    repo.add_block('B2','T1','component_ref',position=2,object_ref_id='C1',status='Approved',source_refs=['SAFE#1'])
    repo.set_block_translation('B1','it-IT','Installare verticalmente.',status='Reviewed')
    repo.set_topic_title_translation('T1','it-IT','Installazione',status='Reviewed')
finally: repo.close()

r1=assess_release_readiness(p,'PUB1',target_langs=['it-IT'])
check('Missing component translation blocks release',not r1['release_ready'] and int(r1['matrix'].loc[r1['matrix']['language']=='it-IT','missing_translations'].iloc[0])==1,r1['matrix'].to_dict(orient='records'))
repo=IndustrialRepository(p/'ManualSync_CCMS.sqlite')
try:
    repo.set_component_translation('C1','it-IT','Scollegare alimentazione e attendere 15 minuti.',target_title='Sicurezza',status='Reviewed')
finally: repo.close()
r2=assess_release_readiness(p,'PUB1',target_langs=['it-IT'])
check('Complete reviewed translation becomes READY',r2['release_ready'],r2['matrix'].to_dict(orient='records'))

bundle=create_release_bundle(p,'PUB1',target_langs=['it-IT'],released_by='Regression',release_id='REL-TEST',out_dir=OUT/'release')
check('Controlled release zip created',Path(bundle['release_zip']).exists(),bundle['release_zip'])
with zipfile.ZipFile(bundle['release_zip']) as zf:
    names=set(zf.namelist())
    check('Release bundle has manifest/readiness/artifacts','RELEASE_MANIFEST.json' in names and any(n.endswith('.docx') for n in names) and 'AUDIT/RELEASE_READINESS.json' in names,names)
repo=IndustrialRepository(p/'ManualSync_CCMS.sqlite')
try:
    n=int(repo.conn.execute("SELECT COUNT(*) FROM publication_releases WHERE release_id LIKE 'REL-TEST-%'").fetchone()[0])
    check('Immutable release snapshots stored',n==2,n)
finally: repo.close()

summary={'build':'v0.26-v0.28','tests':len(res),'pass':sum(x['status']=='PASS' for x in res),'fail':sum(x['status']=='FAIL' for x in res),'results':res}
(BASE/'RISULTATO_REGRESSIONE_RELEASE_V26_V28.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_REGRESSIONE_RELEASE_V26_V28.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in res)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
