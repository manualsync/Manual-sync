from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from language_codes import canonical_lang, canonical_langs
from project_lifecycle import create_project, read_project
from ccms_industrial import IndustrialRepository
from release_readiness import assess_release_readiness
from release_manager import create_release_bundle
from translation_impact import analyze_translation_impact

BASE = Path(__file__).resolve().parent
OUT = BASE / 'regressione_desktop_r18_out'
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

results = []
def check(name, cond, detail=''):
    ok = bool(cond)
    results.append({'test': name, 'status': 'PASS' if ok else 'FAIL', 'detail': str(detail)})
    if not ok:
        raise AssertionError(f'{name}: {detail}')

# 1. Canonicalizer itself.
expected = {
    'it-it': 'it-IT',
    'IT_it': 'it-IT',
    'it-IT': 'it-IT',
    'EN_us': 'en-US',
    'zh-hant-tw': 'zh-Hant-TW',
    'pt-br': 'pt-BR',
}
for raw, exp in expected.items():
    check(f'Canonical language {raw}', canonical_lang(raw) == exp, canonical_lang(raw))
check('Canonical language list deduplicates variants', canonical_langs(['it-it','IT_it','it-IT']) == ['it-IT'], canonical_langs(['it-it','IT_it','it-IT']))

# 2. Project metadata is canonicalized on write.
project = create_project(
    OUT/'projects', project_name='R18 Mixed Case', client='Regression', product='Drive',
    manual_code='R18', revision='1', source_lang='EN_us', target_langs=['it-it','IT_it'], owner='QA'
)
meta = read_project(project)
check('Project source language canonical', meta['source_lang'] == 'en-US', meta)
check('Project target languages canonical and deduplicated', meta['target_langs'] == ['it-IT'], meta)

# 3. Build a publication containing every first-class translation type.
repo = IndustrialRepository(project/'ManualSync_CCMS.sqlite')
try:
    repo.create_topic('T1', 'Electrical ratings', 'legacy body', source_lang='EN_us', status='Approved', source_refs=['SRC#T1'])
    repo.create_table('TAB1', ['Parameter','Value'], [['Rated power','10 kW']], title='Ratings table', caption='Rated values', status='Approved', source_refs=['SRC#TAB'])
    repo.create_figure('FIG1', title='Cooling fan', caption='Cooling fan position', file_path='fan.png', alt_text='Cooling fan position', status='Approved', source_refs=['SRC#FIG'])
    repo.create_component('CMP1', 'Safety warning', 'Disconnect power before service.', source_lang='en-US', source_refs=['SRC#CMP'])
    repo.create_publication('PUB1', 'Mixed Case Manual', ['T1'], source_lang='EN_us')
    repo.add_block('B1','T1','paragraph',position=1,text='Install the drive vertically.',status='Approved',source_refs=['SRC#B1'])
    repo.add_block('B2','T1','table',position=2,object_ref_id='TAB1',status='Approved',source_refs=['SRC#B2'])
    repo.add_block('B3','T1','figure',position=3,object_ref_id='FIG1',status='Approved',source_refs=['SRC#B3'])
    repo.add_block('B4','T1','component_ref',position=4,object_ref_id='CMP1',status='Approved',source_refs=['SRC#B4'])

    # Writes with deliberately inconsistent user casing must be stored canonically.
    repo.set_topic_title_translation('T1','IT_it','Valori nominali elettrici',status='Reviewed')
    repo.set_block_translation('B1','it-it','Installare il convertitore verticalmente.',status='Reviewed')
    repo.set_table_translation('TAB1','IT-it',caption='Valori nominali',columns=['Parametro','Valore'],rows=[['Potenza nominale','10 kW']],status='Reviewed')
    repo.set_figure_translation('FIG1','it_it',caption='Posizione della ventola di raffreddamento',alt_text='Posizione della ventola di raffreddamento',status='Reviewed')
    repo.set_component_translation('CMP1','IT-IT','Scollegare l’alimentazione prima della manutenzione.',target_title='Avvertenza di sicurezza',status='Reviewed')

    stored = {}
    for table in ('topic_translations','block_translations','structured_table_translations','figure_translations','component_translations'):
        stored[table] = sorted({str(r[0]) for r in repo.conn.execute(f'SELECT DISTINCT target_lang FROM {table}')})
    check('All new translation writes use canonical it-IT', all(v == ['it-IT'] for v in stored.values()), stored)

    # Simulate a legacy database containing mixed-case language tags without using the new setters.
    repo.conn.execute("UPDATE topic_translations SET target_lang='IT_it' WHERE topic_id='T1'")
    repo.conn.execute("UPDATE block_translations SET target_lang='IT-it' WHERE block_id='B1'")
    repo.conn.execute("UPDATE structured_table_translations SET target_lang='it-it' WHERE table_id='TAB1'")
    repo.conn.execute("UPDATE figure_translations SET target_lang='IT_IT' WHERE figure_id='FIG1'")
    repo.conn.execute("UPDATE component_translations SET target_lang='it-It' WHERE component_id='CMP1'")
    repo.conn.commit()
finally:
    repo.close()

# 4. Exact user bug from screenshot: lower-case it-it must still resolve legacy translations.
for requested in ('it-it','IT-it','it_IT','it-IT'):
    r = assess_release_readiness(project,'PUB1',target_langs=[requested])
    target = r['matrix'][r['matrix']['role']=='TARGET'].iloc[0].to_dict()
    check(f'Readiness READY for input {requested}', r['release_ready'], r['matrix'].to_dict(orient='records'))
    check(f'Readiness displays canonical language for {requested}', target['language'] == 'it-IT', target)
    check(f'No false missing translations for {requested}', int(target['missing_translations']) == 0, target)
    check(f'No false unreviewed translations for {requested}', int(target['unreviewed_translations']) == 0, target)
    check(f'No blocker rows for {requested}', r['blocking_items'].empty, r['blocking_items'].to_dict(orient='records'))

# 5. Rewriting a legacy alias must collapse it to one canonical row, not create logical duplicates.
repo = IndustrialRepository(project/'ManualSync_CCMS.sqlite')
try:
    repo.set_topic_title_translation('T1','it-it','Valori nominali elettrici',status='Reviewed')
    repo.set_block_translation('B1','IT_it','Installare il convertitore verticalmente.',status='Reviewed')
    repo.set_table_translation('TAB1','it_it',caption='Valori nominali',columns=['Parametro','Valore'],rows=[['Potenza nominale','10 kW']],status='Reviewed')
    repo.set_figure_translation('FIG1','IT-it',caption='Posizione della ventola di raffreddamento',alt_text='Posizione della ventola di raffreddamento',status='Reviewed')
    repo.set_component_translation('CMP1','it-it','Scollegare l’alimentazione prima della manutenzione.',target_title='Avvertenza di sicurezza',status='Reviewed')
    logical_counts = {}
    for table, key, oid in [
        ('topic_translations','topic_id','T1'),('block_translations','block_id','B1'),
        ('structured_table_translations','table_id','TAB1'),('figure_translations','figure_id','FIG1'),
        ('component_translations','component_id','CMP1')]:
        rows=list(repo.conn.execute(f"SELECT target_lang FROM {table} WHERE {key}=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE",(oid,'it-IT')))
        logical_counts[table]=[str(r[0]) for r in rows]
finally:
    repo.close()
check('Legacy aliases collapse to one canonical translation row on write', all(v == ['it-IT'] for v in logical_counts.values()), logical_counts)

# 6. Translation Impact also canonicalizes requested languages and reads legacy case variants.
repo = IndustrialRepository(project/'ManualSync_CCMS.sqlite')
try:
    impact = analyze_translation_impact(repo,'PUB1',kind='publication',target_langs=['IT_it'],require_approved=True,out_dir=OUT/'impact')
finally:
    repo.close()
impact_langs = sorted(set(impact['detail']['target_lang'].astype(str).tolist()))
check('Translation Impact canonical target language', impact_langs == ['it-IT'], impact_langs)

# 7. Full release with lower-case UI input must produce canonical target artifacts, not false blockers.
release = create_release_bundle(
    project,'PUB1',target_langs=['it-it'],released_by='R18 Regression',
    release_id='REL-R18-MIXEDCASE',release_mode='production',out_dir=OUT/'release'
)
check('Release with lower-case input succeeds', Path(release['release_zip']).exists(), release['release_zip'])
check('Release target artifact language canonical', 'it-IT' in set(release['artifacts']['language'].astype(str)), release['artifacts'].to_dict(orient='records'))
with zipfile.ZipFile(release['release_zip'],'r') as z:
    names = z.namelist()
    manifest = json.loads(z.read('RELEASE_MANIFEST.json').decode('utf-8'))
check('Release ZIP uses canonical it-IT filename', any('_it-IT_' in n for n in names), names)
manifest_targets=[r['language'] for r in manifest.get('languages',[]) if r.get('role')=='TARGET']
check('Manifest target language canonical', manifest_targets == ['it-IT'], manifest_targets)

summary = {
    'build': '1.0.0-desktop-r18-language-canonicalization-persistent-state-hardening-20260818',
    'tests': len(results),
    'pass': sum(x['status']=='PASS' for x in results),
    'fail': sum(x['status']=='FAIL' for x in results),
    'results': results,
}
(BASE/'RISULTATO_REGRESSIONE_DESKTOP_R18.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_REGRESSIONE_DESKTOP_R18.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)+f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
