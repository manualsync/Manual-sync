from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Windows consoles default to a legacy codepage (e.g. cp1252) that cannot encode
# characters used in some check details; force UTF-8 stdio so the final report
# print never crashes after all checks have already run and passed.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

# Isolate the callback regression from any real ManualSyncData on the builder PC.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="manualsync-ui-home-"))
os.environ["HOME"] = str(_TEST_HOME)
os.environ["USERPROFILE"] = str(_TEST_HOME)

import app
from cat_tm import TranslationMemory
from ccms_industrial import IndustrialRepository
from translation_roundtrip import XLIFF_NS
from workflow_management import WorkflowManager

OUT = Path(tempfile.mkdtemp(prefix="manualsync-ui-callbacks-"))
results=[]

def check(name, cond, detail=""):
    ok=bool(cond); results.append({"test":name,"status":"PASS" if ok else "FAIL","detail":str(detail)})
    if not ok: raise AssertionError(f"{name}: {detail}")

# Force the app callback layer to a clean, isolated product data home.
app.DATA_HOME = OUT / "ManualSyncData"
app.DATA_ROOT = app.DATA_HOME / "Projects"
app.ACTIVE_PROJECT_FILE = app.DATA_HOME / "active_project.json"
app.APP_LOG = app.DATA_HOME / "MANUALSYNC_APP.log"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)

created = app.create_project_ui("UI Callback Pilot", "ACME", "Drive", "MAN-UI", "R2", "en-US", "it-IT", "Docs")
project = Path(created[0])
check("UI create project returns active workspace", project.exists() and (project / "ManualSync_Project.json").exists(), project)
check("UI create project updates card", "UI Callback Pilot" in created[1] and "PROGETTO ATTIVO" in created[1], created[1][:300])

# A repeated/delayed Create click must be idempotent: reopen the existing project
# instead of surfacing a sticky duplicate-project Gradio error.
created_again = app.create_project_ui("UI Callback Pilot", "ACME", "Drive", "MAN-UI", "R2", "en-US", "it-IT", "Docs")
check("UI duplicate create is idempotent", Path(created_again[0]) == project and "esiste già" in created_again[3].lower() and "aperto" in created_again[3].lower(), created_again[3])
check("UI duplicate create does not duplicate workspace", len([x for x in app.DATA_ROOT.iterdir() if x.is_dir()]) == 1, list(app.DATA_ROOT.iterdir()))

# Build an approved source fixture in the project CCMS.
repo=IndustrialRepository(project/'ManualSync_CCMS.sqlite')
try:
    repo.create_topic('T1','Installation','legacy',status='Approved',source_refs=['SPEC#1'])
    repo.create_publication('PUB-MAIN','Machine Manual',['T1'],source_lang='en-US')
    repo.add_block('B1','T1','paragraph',position=1,text='Maintain a clearance of 20 mm.',status='Approved',source_refs=['SPEC#2'])
finally: repo.close()

oldtm=OUT/'Legacy_IT.sqlite'; tm=TranslationMemory(oldtm)
try:
    tm.add('en-US','it-IT','Maintain a clearance of 15 mm.','Mantenere una distanza di 15 mm.',approved=True,tm_name='Legacy',priority=100)
finally: tm.close()

wp_msg, wp_table, master_zip, lang_zips = app.work_package_ui(str(project),'PUB-MAIN','publication',[str(oldtm)])
lang_zip=Path(lang_zips[0])
check("UI translation package callback creates language ZIP", lang_zip.exists() and Path(master_zip).exists(), wp_msg)

# Simulate a vendor returned translation.
extract=OUT/'work'; extract.mkdir()
with zipfile.ZipFile(lang_zip) as zf: zf.extractall(extract)
xlf=next(extract.glob('*_WORK_ONLY.xlf'))
tree=ET.parse(xlf); ns={'x':XLIFF_NS}
for seg in tree.getroot().findall('.//x:segment',ns):
    src=''.join(seg.find('./x:source',ns).itertext())
    tgt={'Installation':'Installazione','Maintain a clearance of 20 mm.':'Mantenere una distanza di 20 mm.'}.get(src,src)
    seg.find('./x:target',ns).text=tgt; seg.set('state','translated')
returned=OUT/'returned_it.xlf'; tree.write(returned,encoding='utf-8',xml_declaration=True)

app.vendor_ui(str(project),'LSP1','Vendor','vendor@example.test')
job_msg, _ = app.create_job_ui(str(project),str(lang_zip),'LSP1','')
wf=WorkflowManager(project)
try:
    jobs=wf.jobs_table(); jid=str(jobs.iloc[-1]['job_id'])
finally: wf.close()
check("UI job creation callback creates job", jid and 'creato' in job_msg.lower(), job_msg)

app.job_action_ui(str(project),jid,'Segna inviato',None,None,False,'',False)
app.job_action_ui(str(project),jid,'Segna in traduzione',None,None,False,'',False)
qa_out=app.job_action_ui(str(project),jid,'Ricevi XLIFF + controllo qualità (QA)',str(returned),None,False,'',False)
check("UI round-trip QA callback reaches QA ready", 'superato' in qa_out[0].lower(), qa_out[0])
approved=app.job_action_ui(str(project),jid,'Approva traduzione',None,None,False,'UI Reviewer',True)
check("UI approval callback commits translation", approved[4] is not None and approved[5] is not None, approved[0])

readiness=app.readiness_ui(str(project),'PUB-MAIN','it-IT','')
check("UI readiness callback reports release ready", 'RELEASE PRONTA' in readiness[0], readiness[0])
release=app.release_ui(str(project),'PUB-MAIN','it-IT','UI Release Manager','RC2-UI-CALLBACK','','','Collaudo interno')
check("UI release callback creates controlled ZIP", release[1] is not None and Path(release[1]).exists(), release[0])

backup=app.export_project_ui(str(project))
check("UI backup callback creates project archive", backup is not None and Path(backup).exists(), backup)
health_msg, health_table=app.diagnostics_ui(str(project))
check("UI diagnostics callback passes", 'verificata' in health_msg.lower(), health_msg)
restored=app.restore_project_ui(str(backup))
check("UI restore callback reopens verified project", restored[0] and Path(restored[0]).exists() and 'riaperto' in restored[3].lower(), restored[3])

summary={"build":"ManualSync 1.0 Commercial Stabilization RC2.2 - UI Callback E2E","tests":len(results),"pass":sum(x['status']=='PASS' for x in results),"fail":sum(x['status']=='FAIL' for x in results),"results":results}
(Path(__file__).resolve().parent/'RISULTATO_UI_CALLBACKS_RC2.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(Path(__file__).resolve().parent/'RISULTATO_UI_CALLBACKS_RC2.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
shutil.rmtree(OUT, ignore_errors=True)
shutil.rmtree(_TEST_HOME, ignore_errors=True)
