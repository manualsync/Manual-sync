from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
from pathlib import Path
from layout_engine import TextBlock, hierarchical_align_text, map_translation_blocks, normalize, normalize_cat, extract_technical_values, extract_codes

BASE=Path(__file__).resolve().parent
results=[]

def block(idx:int, text:str, section:str='', heading:bool=False)->TextBlock:
    return TextBlock(
        index=idx,page=idx//12,bbox=(0.0,float(idx%12)*40.0,500.0,float(idx%12)*40.0+30.0),
        text=text,normalized=normalize(text),cat_normalized=normalize_cat(text),
        technical_values=extract_technical_values(text),codes=extract_codes(text),page_refs=(),
        section_ref=section,heading=heading,font_size=12.0 if heading else 9.0,
    )

def check(name,cond,detail=''):
    ok=bool(cond); results.append({'test':name,'status':'PASS' if ok else 'FAIL','detail':str(detail)})
    if not ok: raise AssertionError(f'{name}: {detail}')

# 100-block revision with realistic insertions/deletions and five small edits.
old=[]
for i in range(100):
    sec=f'{i//10+1}.1' if i%10==0 else ''
    heading=(i%10==0)
    txt=(f'{sec} Section {i//10+1}' if heading else f'Procedure step STEP-{i:03d}: verify safety interlock and maintain clearance of {20+(i%3)*5} mm before operation.')
    old.append(block(i,txt,sec,heading))

deleted={17,54,88}
modified={12,33,47,71,95}
new=[]; expected={}
for oi,b in enumerate(old):
    if oi in deleted: continue
    if oi in {8,29,63,82,97}:
        ni=len(new); new.append(block(ni,f'New maintenance note NOTE-{oi:03d}: inspect protective cover before operation.'))
    text=b.text
    if oi in modified:
        text=text.replace('verify safety interlock','verify the safety interlock carefully')
    ni=len(new); expected[ni]=oi
    new.append(block(ni,text,b.section_ref,b.heading))

mapping, deleted_found=hierarchical_align_text(old,new)
correct=sum(1 for ni,oi in expected.items() if ni in mapping and mapping[ni][0]==oi)
coverage=100.0*correct/max(1,len(expected))
check('Revision alignment coverage >=95%',coverage>=95.0,f'{correct}/{len(expected)} = {coverage:.1f}%')
check('Deleted blocks identified',set(deleted).issubset(set(deleted_found)),deleted_found)
check('Modified blocks remain aligned',all(any(ni in mapping and mapping[ni][0]==oi for ni,x in expected.items() if x==oi) for oi in modified),mapping)

# Old source -> old translation structural alignment: 10 sections, preserved order,
# translated text length varies and lexical content is intentionally different.
source=[]; target=[]
translations={}
for i in range(100):
    sec=f'{i//10+1}.1' if i%10==0 else ''
    heading=(i%10==0)
    st=(f'{sec} Section {i//10+1}' if heading else f'Procedure STEP-{i:03d} requires clearance {20+(i%3)*5} mm and safety verification before operation.')
    tt=(f'{sec} Sezione {i//10+1}' if heading else f'La procedura STEP-{i:03d} richiede distanza {20+(i%3)*5} mm e verifica di sicurezza prima del funzionamento.')
    source.append(block(i,st,sec,heading)); target.append(block(i,tt,sec,heading)); translations[i]=tt
mapped=map_translation_blocks(source,target)
correct_tr=sum(1 for i in range(100) if i in mapped and mapped[i][0]==translations[i])
tr_coverage=100.0*correct_tr/100.0
check('Legacy bilingual structural alignment >=95%',tr_coverage>=95.0,f'{correct_tr}/100 = {tr_coverage:.1f}%')
low=sum(1 for _,conf,_ in mapped.values() if conf<60)
check('Legacy bilingual low-confidence rate <=5%',low<=5,f'low={low}; mapped={len(mapped)}')

summary={'build':'ManualSync Final Candidate synthetic alignment benchmark','tests':len(results),'pass':sum(x['status']=='PASS' for x in results),'fail':sum(x['status']=='FAIL' for x in results),'revision_alignment_percent':round(coverage,1),'legacy_bilingual_alignment_percent':round(tr_coverage,1),'results':results,'note':'Synthetic controlled benchmark only; real customer-document benchmark is still required before marketing a 95% claim.'}
(BASE/'RISULTATO_ALIGNMENT_BENCHMARK.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'RISULTATO_ALIGNMENT_BENCHMARK.txt').write_text('\n'.join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)+f"\n\nRevision alignment: {coverage:.1f}%\nLegacy bilingual alignment: {tr_coverage:.1f}%\n",encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
