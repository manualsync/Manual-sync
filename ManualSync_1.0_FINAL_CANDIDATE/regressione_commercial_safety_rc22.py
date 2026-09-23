from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from ccms_industrial import IndustrialRepository
from linguistic_safety import cross_language_issue, is_nontranslatable_text
from release_manager import create_release_bundle
from translation_roundtrip import XLIFF_NS, inspect_translation_roundtrip, commit_translation_roundtrip
from translation_work_package import generate_translation_work_packages

BASE = Path(__file__).resolve().parent
results=[]

def check(name, cond, detail=""):
    ok=bool(cond); results.append({"test":name,"status":"PASS" if ok else "FAIL","detail":str(detail)})
    if not ok: raise AssertionError(f"{name}: {detail}")

check("Linguistic gate rejects exact IT->EN copy", bool(cross_language_issue("Premere il pulsante di arresto della macchina.", "Premere il pulsante di arresto della macchina.", "it-IT", "en-US")))
check("Linguistic gate allows invariant technical code", not cross_language_issue("C1", "C1", "it-IT", "en-US"))
check("Non-translatable classifier recognizes code", is_nontranslatable_text("ACS880-34"))
check("Non-translatable classifier keeps prose translatable", not is_nontranslatable_text("Premere il pulsante di arresto della macchina."))

with tempfile.TemporaryDirectory(prefix="msrc22-safety-") as td:
    root=Path(td); project=root/'project'; project.mkdir()
    db=project/'ManualSync_CCMS.sqlite'; repo=IndustrialRepository(db)
    try:
        repo.create_topic('T1','Manuale macchina','legacy',status='Approved',source_refs=['SPEC#1'])
        repo.create_publication('PUB-MAIN','Manuale macchina',['T1'],source_lang='it-IT')
        repeated='Premere il pulsante di arresto della macchina prima della manutenzione.'
        repo.add_block('B1','T1','paragraph',position=1,text=repeated,status='Approved',source_refs=['SPEC#2'])
        repo.add_block('B2','T1','paragraph',position=2,text=repeated,status='Approved',source_refs=['SPEC#2'])
        repo.add_block('B3','T1','paragraph',position=3,text='C1',status='Approved',source_refs=['SPEC#3'])
        repo.add_block('B4','T1','paragraph',position=4,text='La macchina deve essere arrestata in sicurezza.',status='Approved',source_refs=['SPEC#4'])

        pkg=generate_translation_work_packages(repo,'PUB-MAIN',kind='publication',target_langs=['en-US'],out_dir=root/'pkg')
        detail=pkg['detail']
        check("Exact repetition classified as intra-document reuse", int((detail['work_bucket'].astype(str)=='INTRA_DOCUMENT_REUSE').sum()) >= 1, detail[['segment_id','source','work_bucket']].to_dict('records'))
        check("Technical code classified as NOT_TRANSLATABLE", int((detail['work_bucket'].astype(str)=='NOT_TRANSLATABLE').sum()) >= 1, detail[['segment_id','source','work_bucket']].to_dict('records'))
        lang_zip=Path(pkg['language_zips'][0])
        with zipfile.ZipFile(lang_zip) as zf:
            locked_text=zf.read('AUTO_REUSE_LOCKED.csv').decode('utf-8-sig')
            xlf_name=next(n for n in zf.namelist() if n.endswith('_WORK_ONLY.xlf'))
            xlf_bytes=zf.read(xlf_name)
        check("Locked package contains repeat bucket", 'INTRA_DOCUMENT_REUSE' in locked_text)
        check("Locked package contains non-translatable bucket", 'NOT_TRANSLATABLE' in locked_text)

        # Fake vendor return: target equals source. Must fail QA for linguistic prose.
        fake=root/'fake_return.xlf'; fake.write_bytes(xlf_bytes)
        tree=ET.parse(fake); ns={'x':XLIFF_NS}
        for seg in tree.getroot().findall('.//x:segment',ns):
            src=''.join(seg.find('./x:source',ns).itertext())
            tgt=seg.find('./x:target',ns); tgt.text=src; seg.set('state','translated')
        tree.write(fake,encoding='utf-8',xml_declaration=True)
        fake_qa=inspect_translation_roundtrip(repo,lang_zip,fake,out_dir=root/'fakeqa')
        check("Round-trip QA blocks source-copied target", not fake_qa['summary']['commit_allowed_after_human_approval'], fake_qa['summary'])
        check("Round-trip QA records linguistic failures", int(fake_qa['summary'].get('linguistic_fail_count',0)) >= 1, fake_qa['summary'])

        # Valid synthetic English return. One canonical repeated source is translated;
        # locked duplicates must inherit that target at commit.
        good=root/'good_return.xlf'; good.write_bytes(xlf_bytes)
        tree=ET.parse(good)
        counter=0
        for seg in tree.getroot().findall('.//x:segment',ns):
            counter += 1
            tgt=seg.find('./x:target',ns)
            tgt.text="This is an approved English technical translation for maintenance instructions."
            seg.set('state','translated')
        tree.write(good,encoding='utf-8',xml_declaration=True)
        good_qa=inspect_translation_roundtrip(repo,lang_zip,good,out_dir=root/'goodqa')
        check("Synthetic English return passes linguistic plausibility gate", good_qa['summary']['commit_allowed_after_human_approval'], good_qa['summary'])
        committed=commit_translation_roundtrip(repo,project,lang_zip,good,human_approved=True,reviewer='RC2.2 regression',out_dir=root/'commit')
        b1=repo.conn.execute("SELECT target_text FROM block_translations WHERE block_id='B1' AND target_lang='en-US'").fetchone()
        b2=repo.conn.execute("SELECT target_text FROM block_translations WHERE block_id='B2' AND target_lang='en-US'").fetchone()
        check("Intra-document repeated block receives propagated target", b1 and b2 and str(b1['target_text']) == str(b2['target_text']), (dict(b1) if b1 else None, dict(b2) if b2 else None))
        code=repo.conn.execute("SELECT target_text FROM block_translations WHERE block_id='B3' AND target_lang='en-US'").fetchone()
        check("Non-translatable code is preserved unchanged", code and str(code['target_text']) == 'C1', dict(code) if code else None)
    finally:
        repo.close()

    # Separate production fixture with human-reviewed but linguistically invalid target.
    p2=root/'production'; p2.mkdir(); repo=IndustrialRepository(p2/'ManualSync_CCMS.sqlite')
    try:
        repo.create_topic('T1','Manuale macchina','legacy',status='Approved',source_refs=['SPEC#1'])
        repo.create_publication('PUB-MAIN','Manuale macchina',['T1'],source_lang='it-IT')
        repo.add_block('B1','T1','paragraph',position=1,text='Premere il pulsante di arresto della macchina.',status='Approved',source_refs=['SPEC#2'])
        repo.set_translation('T1','en-US','legacy',target_title='Manuale macchina',status='Reviewed')
        repo.set_block_translation('B1','en-US','Premere il pulsante di arresto della macchina.',status='Reviewed')
    finally:
        repo.close()
    blocked=False; err=''
    try:
        create_release_bundle(p2,'PUB-MAIN',target_langs=['en-US'],released_by='Regression',release_id='RC22-PROD-FAKE',release_mode='production',out_dir=root/'prod_out')
    except Exception as e:
        blocked=True; err=str(e)
    check("Production release independently blocks fake cross-language target", blocked and ('controllo linguistico' in err.lower() or 'anomalie linguistiche' in err.lower()), err)
    internal=create_release_bundle(p2,'PUB-MAIN',target_langs=['en-US'],released_by='Regression',release_id='RC22-TEST-FAKE',release_mode='test',out_dir=root/'test_out')
    check("Internal test release can still package controlled fixture", Path(internal['release_zip']).exists(), internal.get('release_zip'))

summary={"build":"ManualSync 1.0 Commercial Stabilization RC2.2 — Commercial Linguistic Safety","tests":len(results),"pass":sum(x['status']=='PASS' for x in results),"fail":sum(x['status']=='FAIL' for x in results),"results":results}
(BASE/'RISULTATO_COMMERCIAL_SAFETY_RC22.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_COMMERCIAL_SAFETY_RC22.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
