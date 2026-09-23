from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)
import json, shutil, zipfile, xml.etree.ElementTree as ET
from pathlib import Path

from project_lifecycle import create_project
from ccms_industrial import IndustrialRepository
from cat_tm import TranslationMemory
from translation_work_package import generate_translation_work_packages
from translation_roundtrip import XLIFF_NS
from workflow_management import WorkflowManager
from release_readiness import assess_release_readiness
from release_manager import create_release_bundle

BASE=Path(__file__).resolve().parent; OUT=BASE/'regressione_e2e_v1_out'
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir(); results=[]
def check(name,cond,detail=''):
    ok=bool(cond); results.append({'test':name,'status':'PASS' if ok else 'FAIL','detail':str(detail)})
    if not ok: raise AssertionError(f'{name}: {detail}')

project=create_project(OUT/'projects',project_name='E2E Manual',client='Industrial Client',manual_code='M-100',revision='R2',source_lang='en-US',target_langs=['it-IT'],owner='Docs')
repo=IndustrialRepository(project/'ManualSync_CCMS.sqlite')
try:
    repo.create_topic('T1','Installation','legacy',status='Approved',source_refs=['SPEC#1'])
    repo.create_publication('PUB-MAIN','Machine M-100 Manual',['T1'],source_lang='en-US')
    repo.add_block('B1','T1','paragraph',position=1,text='Maintain a clearance of 20 mm.',status='Approved',source_refs=['EC#R2'])
finally: repo.close()
check('Project and source publication ready', (project/'ManualSync_CCMS.sqlite').exists(), project)

oldtm=OUT/'Legacy_IT.sqlite'; tm=TranslationMemory(oldtm)
try: tm.add('en-US','it-IT','Maintain a clearance of 15 mm.','Mantenere una distanza di 15 mm.',approved=True,tm_name='Legacy',priority=100)
finally: tm.close()
repo=IndustrialRepository(project/'ManualSync_CCMS.sqlite')
try: pkg=generate_translation_work_packages(repo,'PUB-MAIN',kind='publication',tm_paths=[oldtm],fuzzy_threshold=70,target_langs=['it-IT'],require_approved=True,out_dir=project/'Packages')
finally: repo.close()
lang_zip=Path(pkg['language_zips'][0]); check('Technical change produces controlled work package',lang_zip.exists(),lang_zip)

extract=OUT/'work'; extract.mkdir()
with zipfile.ZipFile(lang_zip) as zf: zf.extractall(extract)
xlf=next(extract.glob('*_WORK_ONLY.xlf')); tree=ET.parse(xlf); ns={'x':XLIFF_NS}
for seg in tree.getroot().findall('.//x:segment',ns):
    source=''.join(seg.find('./x:source',ns).itertext())
    target={'Installation':'Installazione','Maintain a clearance of 20 mm.':'Mantenere una distanza di 20 mm.'}.get(source, source)
    seg.find('./x:target',ns).text=target; seg.set('state','translated')
returned=OUT/'returned_it.xlf'; tree.write(returned,encoding='utf-8',xml_declaration=True)

wf=WorkflowManager(project)
try:
    wf.upsert_vendor('LSP1','Vendor','vendor@example.test')
    jid=wf.create_job_from_package(lang_zip,vendor_id='LSP1',actor='E2E')
    wf.mark_sent(jid,actor='E2E'); wf.mark_in_translation(jid,actor='E2E'); wf.register_returned_xliff(jid,returned,actor='E2E')
    qa=wf.run_roundtrip_qa(jid,project/'ManualSync_CCMS.sqlite',actor='E2E')
    check('Returned translation passes hard QA',qa['job']['status']=='QA_READY',qa['qa']['summary'])
    approved=wf.approve_job(jid,project/'ManualSync_CCMS.sqlite',reviewer='E2E Reviewer',human_approved=True)
    check('Human-approved roundtrip closes job',approved['job']['status']=='APPROVED',approved['job'])
finally: wf.close()

ready=assess_release_readiness(project,'PUB-MAIN',target_langs=['it-IT'])
check('Multilingual release readiness passes',ready['release_ready'],ready['matrix'].to_dict(orient='records'))
rel=create_release_bundle(project,'PUB-MAIN',target_langs=['it-IT'],released_by='E2E Release Manager',release_id='RC-E2E',out_dir=project/'Releases')
check('Final controlled release generated',Path(rel['release_zip']).exists(),rel['release_zip'])
with zipfile.ZipFile(rel['release_zip']) as zf:
    names=set(zf.namelist()); check('Release includes source + Italian artifact and audit',sum(n.endswith('.docx') for n in names)==2 and 'AUDIT/RELEASE_READINESS.json' in names,names)
wf=WorkflowManager(project)
try:
    archive=wf.export_project_archive(project/'Backups')
finally: wf.close()
check('Final project backup generated',archive.exists(),archive)

summary={'build':'ManualSync 1.0 RC1 E2E','tests':len(results),'pass':sum(x['status']=='PASS' for x in results),'fail':sum(x['status']=='FAIL' for x in results),'results':results,'final_release':str(rel['release_zip']),'project_backup':str(archive)}
(BASE/'RISULTATO_REGRESSIONE_E2E_V1.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_REGRESSIONE_E2E_V1.txt').write_text('MANUALSYNC 1.0 RC1 — END TO END\n\n'+'\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
