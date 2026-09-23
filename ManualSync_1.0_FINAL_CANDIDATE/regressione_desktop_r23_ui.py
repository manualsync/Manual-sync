import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)
from pathlib import Path
import importlib.util

ROOT=Path(__file__).resolve().parent
src=(ROOT/'app.py').read_text(encoding='utf-8')
checks=[]
def ck(name, cond, detail=''):
    checks.append((name,bool(cond),detail))

ck('Footer Gradio disabilitato', 'footer_links=[]' in src)
# Localization Foundation V1: the label comes from the catalog (and the
# section is now "Panoramica"/"Overview"), so this checks what the test
# actually cares about -- the landing entry is the one built active.
ck('Home inizialmente attiva',
   'nav_home=LOC.button(lambda: f"⌂   {t(' in src and 'variant="primary")' in
   src.split('nav_home=LOC.button(')[1][:90])
ck('Navigazione usa stato active dinamico', "gr.update(variant='primary' if item==tab_id else 'secondary')" in src)
# Milestone 3.5B added the Compliance tab/nav entry to the shipped, checkpointed
# product (published at e2fde61) -- this expectation was stale against that
# real, approved navigation, not the other way around. Updated to match the
# actual current _NAV_IDS in app.py, verified by direct grep, not guessed.
ck('Tutte le sezioni sidebar partecipano allo stato', "_NAV_IDS=['home','revision','authoring','compliance','translations','jobs','release','settings']" in src)
ck('Quick actions sincronizzano sidebar', "home_release.click(lambda:_navigate('release')" in src and "home_translation.click(lambda:_navigate('translations')" in src)
ck('Stile sidebar active presente', '#ms-sidebar button.primary' in src and 'inset 3px 0 0 var(--ui-primary)' in src)
ck('Tab strip tecnica resta nascosta', '[role="tablist"]' in src and 'display:none!important' in src)
ck('Menu overflow ellipsis Gradio nascosto', '#workspace-tabs .overflow-menu' in src and 'visibility:hidden!important' in src)
ck('Core release invariato collegato', 'create_release_bundle' in src)
ck('Canonicalizzazione lingua invariata collegata', 'canonical_langs' in src)

spec=importlib.util.spec_from_file_location('app',ROOT/'app.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
ck('App importa', hasattr(mod,'demo'))
ck('Brand ancora centralizzato', hasattr(mod,'BRAND_NAME'))

for name,status,detail in checks:
    print(('PASS' if status else 'FAIL'), '|', name, ('| '+detail if detail else ''))
print(f'R23.2 UI gate: {sum(x[1] for x in checks)}/{len(checks)} PASS')
if not all(x[1] for x in checks):
    raise SystemExit(1)
