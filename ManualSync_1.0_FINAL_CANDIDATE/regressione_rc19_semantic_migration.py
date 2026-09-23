import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)
from pathlib import Path
import json, re, shutil, tempfile
from ccms_industrial import IndustrialRepository
from authoring_ccms import import_pdf_as_topics

BASE=Path(__file__).resolve().parent
PDF=BASE/'benchmark'/'abb_acs880'/'new_EN.pdf'
checks=[]
def check(name, ok, detail=''):
    checks.append({'test':name,'status':'PASS' if ok else 'FAIL','detail':str(detail)})

with tempfile.TemporaryDirectory(prefix='ms_rc19_migration_') as td:
    root=Path(td)
    repo=IndustrialRepository(root/'ccms.sqlite')
    try:
        frame=import_pdf_as_topics(repo,PDF,source_lang='en-US',id_prefix='DOC',source_ref_prefix='new_EN.pdf')
        topics=repo.topic_table(); blocks=repo.blocks_table()
        titles=topics['title'].astype(str).tolist()
        table_count=repo.conn.execute('SELECT COUNT(*) FROM structured_tables').fetchone()[0]
        table_blocks=repo.conn.execute("SELECT COUNT(*) FROM content_blocks WHERE block_type='table'").fetchone()[0]
        all_rows=[]
        for r in repo.conn.execute('SELECT rows_json FROM structured_tables'):
            all_rows.extend(json.loads(r[0]))
        flat='\n'.join(' | '.join(map(str,row)) for row in all_rows)
        body='\n'.join(blocks['text'].astype(str).tolist()) if 'text' in blocks.columns else ''
        check('ABB semantic topic count',len(topics)==19,len(topics))
        check('ABB structured table count',table_count==21,table_count)
        check('ABB table blocks linked',table_blocks==21,table_blocks)
        check('No numeric-only topic titles',not any(re.fullmatch(r'[\d\s.,/+×-]+',t) for t in titles),titles)
        check('No running headers as topics',not any(re.fullmatch(r'(?:Technical data\s+\d+|\d+\s+Technical data)',t,re.I) for t in titles),titles)
        check('No formula topic titles',not any(('=' in t or '·' in t) for t in titles),titles)
        check('Key heading Electrical ratings preserved','Electrical ratings' in titles,titles)
        check('Key heading Fuses IEC preserved','Fuses (IEC)' in titles,titles)
        check('Key ABB table cell preserved','442A-3' in flat and '621' in flat,'442A-3 / 621')
        check('Running page header excluded from prose','178 Technical data' not in body and 'Technical data 179' not in body,'headers filtered')
        check('Migration attrs report 13 running headers',int(frame.attrs.get('dropped_running_headers',-1))==13,frame.attrs)
    finally:
        repo.close()

out={'build':'1.0.0-rc1.9-semantic-pdf-migration-20260812','checks':checks,'pass':all(c['status']=='PASS' for c in checks)}
(BASE/'RISULTATO_REGRESSIONE_RC1_9.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_REGRESSIONE_RC1_9.txt').write_text('\n'.join(f"{c['status']} | {c['test']} | {c['detail']}" for c in checks)+f"\nOVERALL: {'PASS' if out['pass'] else 'FAIL'}\n",encoding='utf-8')
print((BASE/'RISULTATO_REGRESSIONE_RC1_9.txt').read_text())
raise SystemExit(0 if out['pass'] else 1)
