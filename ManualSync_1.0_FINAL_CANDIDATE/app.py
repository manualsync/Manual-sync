from __future__ import annotations

import html
import json
import re
import shutil
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr
import pandas as pd

from layout_engine import ENGINE_BUILD, analyze_layouts, build_review_queue, save_analysis_outputs, detect_document_language, detect_docx_language
from authoring_ccms import import_pdf_as_topics, import_docx_as_topics, import_idml_as_topics
from legacy_doc_conversion import convert_legacy_doc_to_docx, LegacyDocConversionError
from idml_import import IdmlImportError
import legacy_family_inference as legacy_family
from ccms_industrial import IndustrialRepository, upgrade_legacy_topics_to_blocks
from authoring_service import AuthoringService, VersionConflictError, ArchivedObjectError, SharedObjectError, MissingAssetError
import authoring_view as av
from translation_work_package import generate_translation_work_packages
from cat_tm import TranslationMemory, source_target_safe
from workflow_management import WorkflowManager
from release_readiness import assess_release_readiness, export_readiness_report
from release_manager import create_release_bundle
from grounded_authoring import GroundingStore
from generative_orchestrator import GenerationLedger, GenerationRequest, GenerativeOrchestrator, SafeSourceComposer
from project_lifecycle import create_project, read_project, update_project, project_health
from support_diagnostics import system_health, create_support_bundle
import compliance_view as cv

from product_version import PRODUCT_BUILD
from language_codes import canonical_lang, canonical_langs

_LEGACY_DOC_FRIENDLY_ERROR = "Non siamo riusciti a leggere questo documento Word. Il file potrebbe essere danneggiato, protetto o in un formato non supportato."
_LEGACY_IDML_FRIENDLY_ERROR = "Non siamo riusciti a leggere questo pacchetto InDesign (.idml). Il file potrebbe essere danneggiato, incompleto o non essere un IDML valido."

BRAND_NAME = "ManualSync"  # Nome provvisorio; il rebrand cambierà questo solo punto UI.
BRAND_TAGLINE = "Technical Content Intelligence"
DATA_HOME = Path.home() / "ManualSyncData"
DATA_ROOT = DATA_HOME / "Projects"
ACTIVE_PROJECT_FILE = DATA_HOME / "active_project.json"
APP_LOG = DATA_HOME / "MANUALSYNC_APP.log"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

def _app_log(message: str, exc: BaseException | None = None) -> None:
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    stamp=datetime.now(timezone.utc).isoformat()
    with APP_LOG.open("a",encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")
        if exc is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)

def _valid_project_dir(value: str | Path | None) -> Path | None:
    if not value:
        return None
    p=Path(value)
    if not (p.exists() and p.is_dir() and (p/"ManualSync_Project.json").exists()):
        return None
    if not any((p/n).exists() for n in ("ManualSync_CCMS.sqlite","ManualSync_CCMS_v18.sqlite")):
        return None
    return p.resolve()

def _set_active_project(project_dir: str | Path | None) -> str:
    p=_valid_project_dir(project_dir)
    if p is None:
        return ""
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    payload=json.dumps({"project_dir":str(p)},ensure_ascii=False,indent=2)
    tmp=ACTIVE_PROJECT_FILE.with_suffix('.json.tmp')
    tmp.write_text(payload,encoding="utf-8")
    tmp.replace(ACTIVE_PROJECT_FILE)
    return str(p)

def _unique_restore_target(archive: str | Path) -> Path:
    stem=Path(archive).stem
    base=DATA_ROOT / f"{stem}_restored"
    if not base.exists(): return base
    i=2
    while True:
        candidate=DATA_ROOT / f"{stem}_restored_{i}"
        if not candidate.exists(): return candidate
        i+=1

def _load_active_project() -> str:
    # 1) Prefer the explicit pointer written at project creation/restore.
    try:
        if ACTIVE_PROJECT_FILE.exists():
            data=json.loads(ACTIVE_PROJECT_FILE.read_text(encoding="utf-8"))
            p=_valid_project_dir(data.get("project_dir"))
            if p is not None:
                return str(p)
    except Exception as e:
        _app_log("Active project pointer unreadable; falling back to project discovery", e)
    # 2) Upgrade path from RC1.5 and earlier: recover the most recently updated
    #    existing ManualSync project instead of forcing the user to recreate it.
    candidates=[]
    for d in DATA_ROOT.iterdir() if DATA_ROOT.exists() else []:
        if not d.is_dir() or not (d/"ManualSync_Project.json").exists():
            continue
        try:
            meta=json.loads((d/"ManualSync_Project.json").read_text(encoding="utf-8"))
            updated=str(meta.get("updated_at") or meta.get("created_at") or "")
        except Exception:
            updated=""
        candidates.append((updated,(d/"ManualSync_Project.json").stat().st_mtime,d.resolve()))
    if candidates:
        candidates.sort(key=lambda x:(x[0],x[1]),reverse=True)
        chosen=candidates[0][2]
        _set_active_project(chosen)
        return str(chosen)
    return ""

INITIAL_PROJECT = _load_active_project()

CSS = """
:root {
  --ui-bg:#f5f7fb; --ui-surface:#ffffff; --ui-surface-2:#f9fafb;
  --ui-ink:#101828; --ui-muted:#667085; --ui-line:#e4e7ec;
  --ui-primary:#2563eb; --ui-primary-hover:#1d4ed8; --ui-primary-soft:#eff6ff;
  --ui-brand:#f97316; --ui-brand-soft:#fff7ed;
  --ui-green:#079455; --ui-green-soft:#ecfdf3; --ui-red:#d92d20; --ui-red-soft:#fef3f2;
  --ui-amber:#dc6803; --ui-amber-soft:#fffaeb;
}
html,body{background:var(--ui-bg)!important}
body,.gradio-container{background:var(--ui-bg)!important;color:var(--ui-ink)!important}
.gradio-container{max-width:none!important;padding:0!important}
footer{display:none!important}
/* R23: sidebar unica. Nasconde in modo robusto la tab-strip tecnica di Gradio. */
.tab-nav,#workspace-tabs .tab-nav,#workspace-tabs>div>.tab-nav,#workspace-tabs [role="tablist"],#workspace-tabs button[role="tab"]{display:none!important;height:0!important;min-height:0!important;max-height:0!important;overflow:hidden!important;border:0!important;margin:0!important;padding:0!important;opacity:0!important;pointer-events:none!important}
/* R23.2: Gradio Tabs crea un menu overflow (ellipsis) quando la tab-strip e nascosta. Nascondiamo anche quel controllo tecnico. */
#workspace-tabs .overflow-menu{display:none!important;visibility:hidden!important;width:0!important;height:0!important;min-width:0!important;min-height:0!important;margin:0!important;padding:0!important;overflow:hidden!important;pointer-events:none!important}
#ms-sidebar{background:#fff!important;border-right:1px solid var(--ui-line)!important;padding:12px 10px!important;width:210px!important;min-width:210px!important;height:100vh!important;max-height:100vh!important;overflow:hidden!important;display:flex!important;flex-direction:column!important}
#ms-sidebar::-webkit-scrollbar,#ms-sidebar *::-webkit-scrollbar{display:none!important;width:0!important}
.ms-brand{display:flex;align-items:center;gap:10px;padding:3px 6px 14px}
.ms-logo{width:34px;height:34px;border-radius:11px;background:linear-gradient(135deg,#f97316,#fbbf24);display:flex;align-items:center;justify-content:center;color:#fff;font-weight:900;font-size:17px;box-shadow:0 6px 16px rgba(249,115,22,.18)}
.ms-brand-title{font-size:17px;font-weight:800;line-height:1.05}.ms-brand-sub{font-size:10px;color:#98a2b3;margin-top:4px;letter-spacing:.01em}
#ms-sidebar button{justify-content:flex-start!important;text-align:left!important;border:0!important;background:transparent!important;color:#344054!important;box-shadow:none!important;border-radius:10px!important;font-weight:650!important;padding:8px 9px!important;min-height:36px!important;font-size:12.5px!important}
#ms-sidebar button:hover{background:#f2f4f7!important;color:#101828!important;transform:none!important}
#ms-sidebar button.primary{background:var(--ui-primary-soft)!important;color:#175cd3!important;box-shadow:inset 3px 0 0 var(--ui-primary)!important;font-weight:800!important}
#ms-sidebar button.secondary{background:transparent!important;color:#344054!important}
.ms-side-section{font-size:9px;text-transform:uppercase;letter-spacing:.09em;color:#98a2b3;font-weight:800;padding:10px 8px 4px}
.ms-side-user{margin-top:14px;border-top:1px solid var(--ui-line);padding:14px 8px 2px;display:flex;gap:10px;align-items:center}.ms-avatar{width:32px;height:32px;border-radius:50%;background:#175cd3;color:white;display:flex;align-items:center;justify-content:center;font-weight:800}.ms-user-name{font-weight:700;font-size:12px}.ms-user-role{font-size:10px;color:var(--ui-muted)}
#workspace-tabs{padding:0 24px 28px!important;max-width:1460px!important;margin:0 auto!important;width:100%!important}
.ms-pagehead{display:flex;justify-content:space-between;align-items:flex-end;padding:16px 2px 11px;border-bottom:1px solid #eaecf0;margin-bottom:13px}.ms-pagehead h1{margin:0;font-size:26px;line-height:1.15;letter-spacing:-.025em}.ms-pagehead p{margin:5px 0 0;color:var(--ui-muted);font-size:13px}.ms-page-kicker{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:#98a2b3;font-weight:800;margin-bottom:5px}
.ms-panel{background:#fff;border:1px solid var(--ui-line);border-radius:15px;padding:18px;box-shadow:0 1px 2px rgba(16,24,40,.03)}.ms-panel + .ms-panel{margin-top:14px}.ms-section-title{font-size:15px;font-weight:800;margin:0 0 4px}.ms-section-sub{font-size:12px;color:var(--ui-muted);margin-bottom:14px}
.ms-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:12px 0}.ms-kpi{background:#fbfcfe;border:1px solid var(--ui-line);border-radius:12px;padding:10px 12px;min-height:76px}.ms-kpi-label{font-size:10.5px;color:var(--ui-muted);font-weight:750}.ms-kpi b{display:block;font-size:22px;line-height:1.1;margin-top:5px;letter-spacing:-.03em}.ms-kpi small{color:#98a2b3;font-size:9.5px}
.ms-project-card{background:#fff;border:1px solid var(--ui-line);border-left:4px solid var(--ui-brand);border-radius:16px;padding:16px 17px 13px;box-shadow:0 2px 10px rgba(16,24,40,.035)}.ms-project-top{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}.ms-project-card h2{font-size:19px;margin:5px 0 2px}.ms-eyebrow{display:inline-block;background:var(--ui-brand-soft);color:#c2410c;border:1px solid #fed7aa;border-radius:999px;padding:5px 8px;font-size:9px;font-weight:850;letter-spacing:.05em}.ms-meta{font-size:12px;color:var(--ui-muted)}.ms-path{font-size:10px;color:#98a2b3;margin-top:11px;padding-top:10px;border-top:1px solid #f2f4f7;word-break:break-all}
.ms-next{margin-top:10px;padding:11px 13px;border-radius:12px;background:var(--ui-brand-soft);border:1px solid #fed7aa;color:#9a3412;font-size:12px;display:flex;align-items:center;gap:10px}.ms-next b{font-weight:850}.ms-next-icon{width:26px;height:26px;border-radius:8px;background:#fff;border:1px solid #fed7aa;display:inline-flex;align-items:center;justify-content:center;flex:none}.ms-empty{padding:28px;text-align:center;color:var(--ui-muted)}
.ms-stepper{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:0 0 18px}.ms-step{display:flex;align-items:center;gap:8px;color:#98a2b3;font-size:11px;font-weight:750}.ms-step:after{content:"";height:1px;background:#e4e7ec;flex:1}.ms-step:last-child:after{display:none}.ms-step-num{width:23px;height:23px;border-radius:50%;display:flex;align-items:center;justify-content:center;border:1px solid #d0d5dd;background:#fff;font-size:10px}.ms-step.active{color:#175cd3}.ms-step.active .ms-step-num{background:var(--ui-primary-soft);border-color:#84adff;color:#175cd3}
.ms-ok{color:#067647;font-weight:700}.ms-block{color:#b42318;font-weight:700}.ms-muted{color:var(--ui-muted)}.ms-prod{padding:10px 12px;border-radius:10px;background:#fef2f2;border:1px solid #fecaca;color:#991b1b;margin:8px 0 12px}.ms-test{padding:10px 12px;border-radius:10px;background:var(--ui-primary-soft);border:1px solid #bfdbfe;color:#1e40af;margin:8px 0 12px}
/* R23: UX brand-ready, sidebar unica, header compatto e CTA gerarchiche. */
.ms-quick-grid{gap:12px!important}
.ms-quick-card{background:#fff!important;border:1px solid var(--ui-line)!important;border-radius:14px!important;padding:15px!important;box-shadow:0 1px 2px rgba(16,24,40,.02)!important;min-height:170px!important}
.ms-quick-card:hover{border-color:#b2ccff!important;box-shadow:0 5px 16px rgba(37,99,235,.08)!important}
.ms-quick-card.brand{border-color:#fed7aa!important;background:#fffbf7!important}
.ms-quick-icon{width:31px;height:31px;border-radius:9px;background:var(--ui-primary-soft);color:#175cd3;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:15px;margin-bottom:12px}
.ms-quick-card.brand .ms-quick-icon{background:var(--ui-brand-soft);color:#c2410c}
.ms-quick-title{font-size:14px;font-weight:800;color:#101828;margin-bottom:5px}
.ms-quick-desc{font-size:11px;line-height:1.45;color:var(--ui-muted);min-height:48px}
.ms-card-cta{margin-top:8px!important;min-height:34px!important;border-radius:9px!important;font-size:11px!important;font-weight:750!important}
.ms-pagebar{align-items:center!important;gap:14px!important}
.ms-pagebar .ms-pagehead{flex:1!important;margin-bottom:12px!important}
.ms-new-project-btn{max-width:132px!important;min-width:132px!important;min-height:36px!important;margin-top:8px!important;font-size:12px!important;padding:8px 12px!important}
.ms-create-project-panel{background:#fff!important;border:1px solid var(--ui-line)!important;border-radius:15px!important;padding:18px!important;margin-top:14px!important}
.ms-sidebar-context{margin-top:auto;padding:9px 6px 0;border-top:1px solid var(--ui-line)}
.ms-sidebar-context .ctx-label{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:#98a2b3;font-weight:800}
.ms-sidebar-context .ctx-name{font-size:10.5px;font-weight:800;color:#344054;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ms-sidebar-context .ctx-sub{font-size:8.5px;color:#98a2b3;margin-top:2px}
.ms-quick-head{padding:12px 2px 8px;margin-top:10px}.ms-quick-head .ms-section-sub{margin-bottom:0}.ms-settings-card{background:#fff;border:1px solid var(--ui-line);border-radius:14px;padding:16px}
button.primary{background:var(--ui-primary)!important;border-color:var(--ui-primary)!important;border-radius:10px!important}button.primary:hover{background:var(--ui-primary-hover)!important}button.secondary{border-radius:10px!important}.gradio-container table{font-size:12px!important}.gradio-container thead th{background:#f9fafb!important;color:#475467!important;font-weight:700!important}
/* Gli accordion di gestione restano secondari e non rubano spazio alla dashboard. */
.ms-secondary-note{font-size:11px;color:#98a2b3;margin-top:12px}
@media(max-width:1100px){#ms-sidebar{width:204px!important;min-width:204px!important}.ms-kpis{grid-template-columns:repeat(2,1fr)}#workspace-tabs{padding-left:20px!important;padding-right:20px!important}}
/* Required-field inline validation (e.g. "Il tuo nome" on legacy family confirm) --
   a plain form error, never a global gr.Error popup. */
.ms-field-invalid textarea,.ms-field-invalid input{border-color:var(--ui-red)!important;box-shadow:0 0 0 1px var(--ui-red) inset!important}
.ms-inline-error{color:var(--ui-red);font-size:11.5px;font-weight:650;margin:2px 2px 8px}
"""

def _ccms_db(project_dir: str | Path) -> Path:
    base=Path(project_dir)
    for n in ("ManualSync_CCMS.sqlite","ManualSync_CCMS_v18.sqlite"):
        p=base/n
        if p.exists(): return p
    raise FileNotFoundError("Repository CCMS non trovato")

def _unreviewed_topic_count(project_dir: str | Path) -> int:
    # Milestone 3A fix: the Recommended Next Action must reflect real project
    # state (imported-but-not-yet-approved content), not skip straight to
    # Release. Read-only query, no change to topic/approval logic itself.
    try:
        db=_ccms_db(project_dir)
    except FileNotFoundError:
        return 0
    repo=IndustrialRepository(db)
    try:
        return int(repo.conn.execute("SELECT COUNT(*) FROM topics WHERE status<>'Approved'").fetchone()[0])
    finally:
        repo.close()

def _project_card(project_dir: str | None) -> str:
    if not project_dir:
        return "<div class='ms-project-card ms-empty'><b>Nessun progetto aperto</b><br><span class='ms-muted'>Crea o riapri un progetto per iniziare.</span></div>"
    h=project_health(project_dir)
    open_jobs=max(0, int(h.get("total_jobs",0))-int(h.get("approved",0)))
    unreviewed=_unreviewed_topic_count(project_dir)
    if int(h.get("qa_blocked",0)) > 0:
        next_action="Apri <b>Job & fornitori</b> e correggi i job bloccati dal controllo qualità."
    elif open_jobs > 0:
        next_action="Apri <b>Job & fornitori</b> e completa i job di traduzione ancora aperti."
    elif int(h.get("publications",0)) <= 0:
        next_action="Apri <b>Redazione & contenuti</b> e importa/approva la sorgente strutturata."
    elif unreviewed > 0:
        next_action="Il contenuto importato non è ancora stato revisionato. <b>Rivedi il contenuto importato</b> prima di procedere."
    else:
        next_action="Il workflow operativo non ha blocchi aperti. Prossimo step: <b>Release → verifica readiness</b>."
    return f"""<div class='ms-project-card'><div class='ms-project-top'><div><span class='ms-eyebrow'>PROGETTO ATTIVO</span><h2>{h.get('project_name',Path(project_dir).name)}</h2><div class='ms-meta'>{h.get('client','')} · {h.get('manual_code','')} · Rev. {h.get('revision','')}</div></div></div><div class='ms-kpis'><div class='ms-kpi'><div class='ms-kpi-label'>Pubblicazioni</div><b>{h['publications']}</b><small>nel progetto</small></div><div class='ms-kpi'><div class='ms-kpi-label'>Blocchi</div><b>{h['blocks']}</b><small>contenuto strutturato</small></div><div class='ms-kpi'><div class='ms-kpi-label'>Job aperti</div><b>{open_jobs}</b><small>workflow traduzione</small></div><div class='ms-kpi'><div class='ms-kpi-label'>QA bloccati</div><b>{h['qa_blocked']}</b><small>richiedono intervento</small></div></div><div class='ms-next'><span class='ms-next-icon'>→</span><div><b>Azione consigliata</b><br>{next_action}</div></div><div class='ms-path'>Percorso progetto · {project_dir}</div></div>"""

def _sidebar_context(project_dir: str | None) -> str:
    if not project_dir:
        return "<div class='ms-sidebar-context'><div class='ctx-label'>Progetto</div><div class='ctx-name'>Nessun progetto attivo</div><div class='ctx-sub'>Workspace locale</div></div>"
    try:
        h=project_health(project_dir)
        name=h.get('project_name') or Path(project_dir).name
        client=h.get('client') or 'Workspace locale'
        return f"<div class='ms-sidebar-context'><div class='ctx-label'>Progetto attivo</div><div class='ctx-name'>{name}</div><div class='ctx-sub'>{client}</div></div>"
    except Exception:
        return f"<div class='ms-sidebar-context'><div class='ctx-label'>Progetto attivo</div><div class='ctx-name'>{Path(project_dir).name}</div><div class='ctx-sub'>Workspace locale</div></div>"

def _existing_project_by_name(project_name: str):
    """Return an existing ManualSync project with the same logical name.

    Project creation is intentionally idempotent at the UI boundary: a delayed or
    repeated click must never strand the user behind a duplicate-project error.
    """
    wanted=str(project_name or '').strip().casefold()
    if not wanted or not DATA_ROOT.exists():
        return None
    for candidate in DATA_ROOT.iterdir():
        if not candidate.is_dir():
            continue
        try:
            meta=read_project(candidate)
        except Exception:
            continue
        if str(meta.get('project_name') or '').strip().casefold() == wanted:
            return candidate
    return None

def create_project_ui(name, client, product, manual_code, revision, source_lang, target_langs, owner):
    if not str(name or '').strip(): raise gr.Error("Inserisci il nome del progetto.")
    langs=canonical_langs(target_langs)
    try:
        p=create_project(DATA_ROOT,project_name=name.strip(),client=client or '',product=product or '',manual_code=manual_code or '',revision=revision or '',source_lang=canonical_lang((source_lang or 'en-US').strip()),target_langs=langs,owner=owner or '')
        active=_set_active_project(p)
        return active, _project_card(active), _sidebar_context(active), f"✅ Progetto creato in **{p}**", gr.update(visible=False)
    except FileExistsError:
        existing=_existing_project_by_name(name)
        if existing is not None:
            active=_set_active_project(existing)
            return active, _project_card(active), _sidebar_context(active), f"ℹ️ Il progetto **{name.strip()}** esiste già ed è stato aperto. Nessun duplicato è stato creato.", gr.update(visible=False)
        raise gr.Error("Esiste già una cartella con questo nome ma non è un progetto ManualSync valido. Usa un nome diverso o verifica la cartella esistente.")
    except Exception as exc:
        _app_log("Project creation failed", exc)
        raise gr.Error("Impossibile creare il progetto. I dettagli tecnici sono stati registrati nel log di assistenza.")

def restore_project_ui(archive):
    if not archive: raise gr.Error("Carica un archivio progetto ManualSync.")
    try:
        target=_unique_restore_target(archive)
        restored=WorkflowManager.restore_project_archive(archive, target)
    except Exception as exc:
        _app_log("Project restore failed", exc)
        raise gr.Error("Backup non ripristinato: archivio non valido, incompleto o non integro. Nessun progetto parziale è stato mantenuto.")
    active=_set_active_project(restored)
    return active, _project_card(active), _sidebar_context(active), f"✅ Progetto verificato e riaperto in **{restored.name}**."

def diagnostics_ui(project_dir):
    try:
        report=system_health(DATA_HOME, project_dir or None)
        status="✅ Integrità locale verificata." if int(report.get('fail',0))==0 else "⚠️ Sono presenti controlli da verificare. Esporta la diagnostica per l'assistenza."
        rows=pd.DataFrame(report.get('checks',[]))
        return status, rows
    except Exception as exc:
        _app_log("System health check failed", exc)
        return "⚠️ Verifica integrità non completata. Esporta la diagnostica per l'assistenza.", pd.DataFrame()

def support_bundle_ui(project_dir):
    try:
        p=create_support_bundle(DATA_HOME, project_dir or None)
        return str(p), f"✅ Pacchetto diagnostico creato: **{p.name}**"
    except Exception as exc:
        _app_log("Support bundle failed", exc)
        raise gr.Error("Impossibile creare il pacchetto diagnostico. Consulta il log locale.")

def export_project_ui(project_dir):
    if not project_dir: raise gr.Error("Apri un progetto.")
    wf=WorkflowManager(project_dir)
    try: z=wf.export_project_archive()
    finally: wf.close()
    return str(z)

def refresh_ui(project_dir):
    return _project_card(project_dir)



def _friendly_review_queue(queue: pd.DataFrame) -> pd.DataFrame:
    """Present only decision-relevant fields in the product UI.

    The full engineering queue is still exported to CSV by the Technical Engine.
    """
    if queue is None or queue.empty:
        return pd.DataFrame(columns=["Priorità","Azione","Rischio","Tipo","Nuovo contenuto","Traduzione recuperata","Affidabilità allineamento (%)","Motivo","Pagina"])
    def col(name: str, default: str = ""):
        if name in queue.columns:
            return queue[name]
        return pd.Series([default] * len(queue), index=queue.index)
    out = pd.DataFrame({
        "Priorità": col("priorita"),
        "Azione": col("decisione"),
        "Rischio": col("rischio"),
        "Tipo": col("tipo"),
        "Nuovo contenuto": col("nuovo_originale"),
        "Traduzione recuperata": col("traduzione_recuperata"),
        "Affidabilità allineamento (%)": col("affidabilita_traduzione", 0.0),
        "Motivo": col("motivo"),
        "Pagina": col("pagina_nuova"),
    })
    return out


def _friendly_work_packages(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Lingua","Segmenti da lavorare","Parole","Riuso TM","Ripetizioni riusate","Non traducibili","Revisione linguistica","Revisione tecnica","Nuova traduzione"])
    def c(name, default=0):
        return frame[name] if name in frame.columns else pd.Series([default]*len(frame), index=frame.index)
    return pd.DataFrame({
        "Lingua": c("target_lang",""),
        "Segmenti da lavorare": c("work_segments"),
        "Parole": c("work_words"),
        "Riuso TM": c("auto_reuse_locked_segments"),
        "Ripetizioni riusate": c("intra_document_reuse_locked_segments"),
        "Non traducibili": c("nontranslatable_locked_segments"),
        "Revisione linguistica": c("linguistic_review"),
        "Revisione tecnica": c("technical_review"),
        "Nuova traduzione": c("new_translation"),
    })


def _friendly_jobs(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Job","Stato","Lingua","Fornitore","Scadenza","Scaduto","Parole","Revisione tecnica","QA"])
    def c(name, default=""):
        return frame[name] if name in frame.columns else pd.Series([default]*len(frame), index=frame.index)
    return pd.DataFrame({
        "Job": c("job_id"),
        "Stato": c("status_label"),
        "Lingua": c("target_lang"),
        "Fornitore": c("vendor"),
        "Scadenza": c("due_date"),
        "Scaduto": c("overdue"),
        "Parole": c("work_words",0),
        "Revisione tecnica": c("technical_review_segments",0),
        "QA": c("qa_status"),
    })


def _friendly_vendors(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Vendor ID","Fornitore","Email","Attivo","Aggiornato"])
    def c(name, default=""):
        return frame[name] if name in frame.columns else pd.Series([default]*len(frame), index=frame.index)
    return pd.DataFrame({
        "Vendor ID": c("vendor_id"),
        "Fornitore": c("name"),
        "Email": c("email"),
        "Attivo": c("active"),
        "Aggiornato": c("updated_at"),
    })


def _friendly_events(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Job","Evento","Da","A","Operatore","Data"])
    def c(name, default=""):
        return frame[name] if name in frame.columns else pd.Series([default]*len(frame), index=frame.index)
    return pd.DataFrame({
        "Job": c("job_id"),
        "Evento": c("event_type"),
        "Da": c("from_status"),
        "A": c("to_status"),
        "Operatore": c("actor"),
        "Data": c("created_at"),
    })


def _friendly_readiness_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    rename = {
        "language":"Lingua","role":"Ruolo","status":"Stato",
        "missing_translations":"Traduzioni mancanti","stale_translations":"Traduzioni stale",
        "unreviewed_translations":"Non revisionate","open_jobs":"Job aperti",
        "qa_blocked_jobs":"QA bloccati","overdue_jobs":"Job scaduti",
        "open_ccms_reviews":"Review CCMS","grounding_issues":"Problemi grounding","issues":"Problemi",
    }
    cols=[c for c in rename if c in frame.columns]
    return frame[cols].rename(columns=rename)


def _friendly_release_artifacts(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Lingua","Formato","File","Dimensione (KB)"])
    out=pd.DataFrame({
        "Lingua": frame["language"] if "language" in frame.columns else "",
        "Formato": frame["type"] if "type" in frame.columns else "",
        "File": frame["path"] if "path" in frame.columns else "",
        "Dimensione (KB)": (frame["bytes"]/1024).round(1) if "bytes" in frame.columns else 0,
    })
    return out


def revision_analysis_ui(project_dir, old_source, old_translation, new_source, full_new):
    # Never turn a normal workflow problem into four anonymous Gradio "Error"
    # placeholders. Return a readable diagnostic and persist the traceback.
    p=_valid_project_dir(project_dir)
    if p is None:
        msg="<div class='ms-card'><h3 class='ms-block'>Progetto non aperto</h3><p>Torna su <b>Home</b> e apri o crea un progetto. ManualSync non avvia analisi senza un progetto persistente.</p></div>"
        _app_log("Revision analysis blocked: no valid active project")
        return msg, pd.DataFrame(), None, None
    if not old_source or not old_translation or not new_source:
        msg="<div class='ms-card'><h3 class='ms-block'>File mancanti</h3><p>Carica vecchio originale, vecchia traduzione e nuovo originale.</p></div>"
        return msg, pd.DataFrame(), None, None
    try:
        _set_active_project(p)
        text_df, table_df, image_df, summary=analyze_layouts(old_source,old_translation,new_source,full_new_source_pdf=full_new or None)
        queue=build_review_queue(text_df)
        out=p/"Analyses"/Path(new_source).stem; out.mkdir(parents=True,exist_ok=True)
        paths=save_analysis_outputs(text_df,table_df,image_df,summary,out)
        reuse=int(summary.get('riusi_automatici_consentiti',summary.get('riusi_automatici',0)) or 0)
        technical=int(summary.get('modifiche_tecniche_testo',0) or 0)+int(summary.get('modifiche_tecniche_tabelle',0) or 0)
        newc=int(summary.get('contenuti_nuovi_testo',0) or 0)+int(summary.get('celle_tabelle_nuove',0) or 0)
        blocked=int(summary.get('riusi_bloccati_allineamento_bilingue',0) or 0)+int(summary.get('riusi_celle_tabelle_bloccati',0) or 0)
        friendly=f"""<div class='ms-card'><h3 style='margin-top:0'>Analisi completata</h3><div class='ms-kpis'><div class='ms-kpi'><span>Riuso sicuro</span><b>{reuse}</b></div><div class='ms-kpi'><span>Modifiche tecniche</span><b>{technical}</b></div><div class='ms-kpi'><span>Contenuti nuovi</span><b>{newc}</b></div><div class='ms-kpi'><span>Riusi bloccati</span><b>{blocked}</b></div></div><div class='ms-ok'>Technical Safe Match completato.</div></div>"""
        _app_log(f"Revision analysis PASS project={p.name} source={Path(new_source).name} queue_rows={len(queue)}")
        return friendly, _friendly_review_queue(queue), paths['rapporto'], paths['coda']
    except Exception as e:
        _app_log(f"Revision analysis FAILED project={p.name}", e)
        msg=f"<div class='ms-card'><h3 class='ms-block'>Analisi non completata</h3><p><b>{type(e).__name__}</b>: {str(e)}</p><p class='ms-muted'>Dettagli salvati in {APP_LOG}</p></div>"
        return msg, pd.DataFrame(), None, None

def _it_count(n: int, singular: str, plural: str) -> str:
    return f"**{n} {singular if n == 1 else plural}**"

_IDML_REVIEW_REASON_LABELS = {
    "master_spread_content": "Contenuto di pagina master (intestazione/piè di pagina ricorrente)",
    "orphan_story": "Testo non collegato a nessuna pagina del documento",
    "unsupported_construct": "Struttura del documento non supportata in questa versione",
    "duplicate_self_id": "Riferimento tecnico duplicato nel pacchetto",
    "ambiguous_asset": "Immagine collegata non identificata in modo univoco",
    "caption_candidate": "Possibile didascalia da confermare per una figura",
}
_IDML_REVIEW_MAX_ITEMS = 12
_IDML_ACCEPT_EXCLUSION = "Accetta esclusione dal contenuto principale"
_IDML_KEEP_SEPARATE = "Mantieni il testo separato (non è la didascalia della figura)"
_IDML_CONFIRM_CAPTION = "Conferma: è la didascalia della figura"
_IDML_LEAVE_UNRESOLVED = "Lascia l'immagine non risolta (resta segnalata come mancante)"

class IdmlReviewCorruptError(Exception):
    """Raised when a review state file exists but cannot be read/parsed as
    the expected shape. Milestone 3D constraint: this must NEVER be treated
    the same as "no review needed" -- a persistence error is not an empty
    review list."""

def _idml_review_path(project_dir, publication_id) -> Path:
    return Path(project_dir)/f"ManualSync_IDML_Review_{publication_id}.json"

def _write_idml_review_data(project_dir, publication_id, review_items) -> None:
    # Milestone 3D constraint: master-spread/orphan/unsupported/ambiguous/
    # caption-candidate items must be reviewable in the standard UI, not only
    # inside a JSON file a non-technical user would have to open. This file
    # stays the technical provenance (reason/detail keep Story/Spread ids for
    # advanced mode) AND the single source of truth for the blocking gate in
    # approve_source_ui below -- one file, not two, kept in sync by
    # construction. `decision` starts unset (None) -- "seen" is never the same
    # as "resolved"; see idml_review_confirm_ui.
    payload = [
        {"reason": r.get("reason", ""), "detail": r.get("detail", ""),
         "preview": r.get("text_preview", ""), "item_key": r.get("item_key", ""),
         "decision": None, "decided_by": "", "decided_at": ""}
        for r in review_items
    ]
    _idml_review_path(project_dir, publication_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def _idml_review_state(project_dir, publication_id) -> list[dict]:
    """Returns [] ONLY when there genuinely is no review file. If the file
    exists but cannot be read as a valid list of item dicts, raises
    IdmlReviewCorruptError -- a persistence error must never be silently
    reinterpreted as "nothing to review"."""
    path = _idml_review_path(project_dir, publication_id)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        items = json.loads(raw)
    except Exception as exc:
        raise IdmlReviewCorruptError(f"{path.name} non è leggibile o non è JSON valido: {exc}") from exc
    if not isinstance(items, list) or not all(isinstance(it, dict) and "reason" in it for it in items):
        raise IdmlReviewCorruptError(f"{path.name} non ha il formato atteso (elenco di elementi di revisione).")
    return items

def _idml_pending_review_items(project_dir, publication_id) -> list[dict]:
    # May raise IdmlReviewCorruptError -- callers (approve_source_ui,
    # idml_review_confirm_ui, _idml_review_panel_ui) must fail closed on it,
    # never catch-and-return [].
    items = _idml_review_state(project_dir, publication_id)
    return [it for it in items if it.get("decision") is None]

def _idml_review_choices_for(reason: str) -> list[str]:
    # No default/pre-selected choice is returned on purpose: a review decision
    # must always come from an explicit user click, even when only one choice
    # exists (e.g. "leave unresolved") -- see idml_review_confirm_ui, which
    # treats an untouched Radio (value None) as still pending, never decided.
    if reason == "caption_candidate":
        return [_IDML_KEEP_SEPARATE, _IDML_CONFIRM_CAPTION]
    if reason == "ambiguous_asset":
        return [_IDML_LEAVE_UNRESOLVED]
    return [_IDML_ACCEPT_EXCLUSION]

def _idml_review_panel_ui(project_dir, publication_id="PUB-MAIN"):
    hidden = (gr.update(visible=False), gr.update(visible=False)) + tuple(
        gr.update(visible=False) for _ in range(_IDML_REVIEW_MAX_ITEMS))
    if not project_dir:
        return hidden
    try:
        items = _idml_review_state(project_dir, publication_id)
    except IdmlReviewCorruptError as exc:
        error_html = (f"<div class='ms-prod'><b>Stato di revisione IDML non leggibile.</b><br>"
                      f"{html.escape(str(exc))}<br>L'approvazione di questa sorgente resta bloccata "
                      f"finché il file non viene ripristinato o l'import rieseguito.</div>")
        return (gr.update(value=error_html, visible=True), gr.update(visible=False)) + tuple(
            gr.update(visible=False) for _ in range(_IDML_REVIEW_MAX_ITEMS))
    pending = [it for it in items if it.get("decision") is None]
    if not pending:
        return hidden
    # This summary is rendered by a gr.HTML component (c_idml_review_html),
    # which does not interpret Markdown -- build it as plain HTML (no "**")
    # rather than reusing _it_count, whose "**n**" bold syntax is meant for
    # the gr.Markdown components elsewhere in this file.
    n_pending = len(pending)
    word = "elemento" if n_pending == 1 else "elementi"
    summary = (f"<div class='ms-prod'><b>{n_pending} {word} da verificare</b> "
               f"prima di poter approvare questa sorgente. Scegli una decisione per ciascuno qui sotto.</div>")
    radio_updates = []
    for i in range(_IDML_REVIEW_MAX_ITEMS):
        if i < len(pending):
            it = pending[i]
            choices = _idml_review_choices_for(it.get("reason", ""))
            label = (f"{_IDML_REVIEW_REASON_LABELS.get(it.get('reason',''), it.get('reason',''))} — "
                     f"{it.get('preview') or '(nessuna anteprima disponibile)'}")
            # value=None: no choice is ever pre-selected, not even when there
            # is only one option -- the user must click it explicitly.
            radio_updates.append(gr.update(label=label, choices=choices, value=None, visible=True))
        else:
            radio_updates.append(gr.update(visible=False))
    return (gr.update(value=summary, visible=True), gr.update(visible=True), *radio_updates)

def _apply_idml_caption_confirmation(project_dir, item_key: str, caption_text: str) -> None:
    if not item_key or not caption_text:
        return
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        # The candidate_key marker is attached to the FIGURE's own source_refs
        # (create_figure), not the content_blocks row that merely points at
        # it (add_block only carries the plain page/story ref) -- query the
        # figures table directly.
        marker = f"idml_caption_candidate_key={item_key}"
        row = repo.conn.execute(
            "SELECT figure_id FROM figures WHERE source_refs_json LIKE ?",
            (f"%{marker}%",),
        ).fetchone()
        if row and row[0]:
            repo.update_figure(row[0], caption=caption_text)
    finally:
        repo.close()

def idml_review_confirm_ui(project_dir, reviewer, *radio_values):
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    publication_id = "PUB-MAIN"
    try:
        items = _idml_review_state(project_dir, publication_id)
    except IdmlReviewCorruptError as exc:
        raise gr.Error(f"Stato di revisione IDML non leggibile: {exc}. Ripristina il file o riesegui l'importazione prima di continuare.") from exc
    pending = [it for it in items if it.get("decision") is None]
    if not pending:
        return _idml_review_panel_ui(project_dir, publication_id)
    if not str(reviewer or "").strip():
        # Acceptance fix: a missing reviewer name is a validation issue, not
        # a crash -- persisting per-item decisions without an identifiable
        # reviewer would break the audit trail (it["decided_by"] below), so
        # this must still block, but WITHOUT raising: this callback has 14
        # bound outputs (html + row + all Radios), and an exception here
        # left every one of them in an Error state, wiping the choices the
        # user had just made. Echo those choices back unchanged (never
        # rebuild via _idml_review_panel_ui, which resets every Radio to
        # value=None) and surface the requirement inline instead.
        warning_html = ("<div class='ms-prod'><b>Inserisci il tuo nome per registrare le decisioni di "
                         "revisione.</b> Le scelte già selezionate qui sotto restano come le hai lasciate.</div>")
        radio_keep = [
            gr.update(value=(radio_values[i] if i < len(radio_values) else None)) if i < len(pending)
            else gr.update()
            for i in range(_IDML_REVIEW_MAX_ITEMS)
        ]
        return (gr.update(value=warning_html, visible=True), gr.update(visible=True), *radio_keep)
    now = datetime.now(timezone.utc).isoformat()
    changed = False
    for i, it in enumerate(pending):
        if i >= len(radio_values):
            break
        choice = radio_values[i]
        if not choice:
            continue  # no decision made for this item yet -- stays pending
        reason = it.get("reason", "")
        if reason == "caption_candidate" and choice == _IDML_CONFIRM_CAPTION:
            _apply_idml_caption_confirmation(project_dir, it.get("item_key", ""), it.get("preview", ""))
            it["decision"] = "confirm_association"
        elif reason == "caption_candidate" and choice == _IDML_KEEP_SEPARATE:
            it["decision"] = "keep_separate"
        elif reason == "ambiguous_asset" and choice == _IDML_LEAVE_UNRESOLVED:
            it["decision"] = "leave_unresolved"
        elif choice == _IDML_ACCEPT_EXCLUSION:
            it["decision"] = "accepted_exclusion"
        else:
            continue
        it["decided_by"] = reviewer.strip()
        it["decided_at"] = now
        changed = True
    if changed:
        _idml_review_path(project_dir, publication_id).write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return _idml_review_panel_ui(project_dir, publication_id)

def import_manual_ui(project_dir, pdf, source_lang, publication_id, title):
    if not project_dir or not pdf:
        raise gr.Error("Apri un progetto e carica il manuale corrente.")
    pub=(publication_id or 'PUB-MAIN').strip()
    source=(source_lang or read_project(project_dir).get('source_lang','en-US')).strip()
    srcdir=Path(project_dir)/'Sources'; srcdir.mkdir(exist_ok=True)
    suffix=Path(pdf).suffix.lower()
    copied=srcdir/Path(pdf).name
    if suffix!='.idml':
        # Milestone 3D fail-closed hardening left this copy-then-process order
        # unchanged for PDF/DOCX/DOC (out of scope for this fix). The .idml
        # branch below defers this copy until after a full, successful parse
        # + CCMS write -- see there for why.
        shutil.copy2(pdf,copied)
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        # Re-import is allowed only while the publication is still a migration draft.
        # This prevents duplicate/orphaned topic trees during iterative migration tests.
        tids_old=[]; table_ids_old=[]; figure_ids_old=[]
        if repo.conn.execute("SELECT 1 FROM publications WHERE publication_id=?",(pub,)).fetchone():
            linked=list(repo.conn.execute(
                "SELECT t.topic_id,t.status FROM topics t JOIN publication_topics pt ON pt.topic_id=t.topic_id WHERE pt.publication_id=?",
                (pub,)
            ))
            if linked and any(str(r["status"]).lower() != "migrated" for r in linked):
                raise ValueError("La pubblicazione contiene contenuti già revisionati/approvati. Usa un nuovo Publication ID per una nuova migrazione.")
            tids_old=[str(r["topic_id"]) for r in linked]
            if tids_old:
                marks=",".join("?" for _ in tids_old)
                table_ids_old=[str(r[0]) for r in repo.conn.execute(
                    f"SELECT object_ref_id FROM content_blocks WHERE topic_id IN ({marks}) AND block_type='table' AND object_ref_id<>''",
                    tids_old
                )]
                figure_ids_old=[str(r[0]) for r in repo.conn.execute(
                    f"SELECT object_ref_id FROM content_blocks WHERE topic_id IN ({marks}) AND block_type='figure' AND object_ref_id<>''",
                    tids_old
                )]

        def _retire_old_draft():
            if tids_old:
                repo.conn.execute("DELETE FROM publication_topics WHERE publication_id=?",(pub,))
                repo.conn.executemany("DELETE FROM topics WHERE topic_id=?",[(tid,) for tid in tids_old])
                if table_ids_old:
                    repo.conn.executemany("DELETE FROM structured_tables WHERE table_id=?",[(tid,) for tid in table_ids_old])
                if figure_ids_old:
                    repo.conn.executemany("DELETE FROM figures WHERE figure_id=?",[(fid,) for fid in figure_ids_old])
                repo.conn.commit()

        if suffix!='.idml':
            # Unchanged pre-Milestone-3D behavior: the previous draft is
            # retired immediately, before the new import runs. PDF/DOCX/DOC
            # are explicitly out of scope for the non-destructive-re-import
            # fix below.
            _retire_old_draft()

        if suffix in ('.docx','.doc'):
            try:
                if suffix=='.doc':
                    # Milestone 3B.1: .doc is converted to a throwaway, isolated .docx
                    # via a local offline converter, then handed to the SAME 3B docx
                    # pipeline below. source_ref_prefix stays the original .doc name
                    # (copied.name) so provenance never shows the temporary .docx.
                    with convert_legacy_doc_to_docx(copied) as temp_docx:
                        imported=import_docx_as_topics(repo,temp_docx,source_lang=source,id_prefix='DOC',source_ref_prefix=copied.name)
                else:
                    imported=import_docx_as_topics(repo,copied,source_lang=source,id_prefix='DOC',source_ref_prefix=copied.name)
            except ValueError:
                raise
            except LegacyDocConversionError as exc:
                raise gr.Error(_LEGACY_DOC_FRIENDLY_ERROR) from exc
            except Exception as exc:
                raise gr.Error("Il documento Word non è valido o è danneggiato. Verifica il file e riprova.") from exc
            if imported.empty:
                raise ValueError("Nessun topic ricostruito dal documento Word.")
        elif suffix=='.idml':
            try:
                # Milestone 3D fail-closed hardening: parse/validate + write to
                # CCMS directly from the ORIGINAL uploaded file (pdf, not
                # copied) -- authoring_ccms.import_idml_as_topics owns staging
                # of any extracted assets and rolls back every CCMS row it
                # created on any failure. Nothing is copied into Sources/ and
                # no review data is written until this call has FULLY
                # succeeded, so an invalid or failing IDML leaves zero residue
                # in the project (no partial source copy, no assets dir, no
                # review data, no topic/block/table/figure row).
                imported=import_idml_as_topics(repo,pdf,source_lang=source,id_prefix='IDML',
                                                source_ref_prefix=copied.name,assets_dir=srcdir/f"{copied.stem}_assets")
            except ValueError:
                raise
            except IdmlImportError as exc:
                raise gr.Error(_LEGACY_IDML_FRIENDLY_ERROR) from exc
            except Exception as exc:
                raise gr.Error(_LEGACY_IDML_FRIENDLY_ERROR) from exc
            if imported.empty:
                raise ValueError("Nessuna sezione ricostruita dal pacchetto InDesign.")
            # Milestone 3D non-destructive re-import: the PREVIOUS draft (its
            # topics/blocks/tables/figures, source file, review state) is only
            # ever retired HERE, after the new IDML has been fully parsed AND
            # written to CCMS successfully. If the new file is corrupt or the
            # write fails, import_idml_as_topics already raised above -- this
            # line is never reached, and the old draft is left byte-for-byte
            # untouched (source copy, assets dir, review json, CCMS rows).
            _retire_old_draft()
            shutil.copy2(pdf,copied)
            # Always replace the review state (even with an empty list) so a
            # re-import that no longer produces any review item never leaves
            # stale, unrelated entries from the previous draft blocking
            # approval or showing up in "Elementi da verificare".
            review_items=imported.attrs.get("review_items") or []
            _write_idml_review_data(project_dir,pub,review_items)
        else:
            imported=import_pdf_as_topics(repo,copied,source_lang=source,id_prefix='DOC',source_ref_prefix=copied.name)
            if imported.empty:
                raise ValueError("Nessun topic ricostruito dal PDF.")
        tids=imported['topic_id'].astype(str).tolist()
        if repo.conn.execute("SELECT 1 FROM publications WHERE publication_id=?",(pub,)).fetchone():
            repo.replace_publication_topics(pub,tids)
            repo.conn.execute("UPDATE publications SET title=?,source_lang=? WHERE publication_id=?",
                              (title or Path(pdf).stem,source,pub))
            repo.conn.commit()
        else:
            repo.create_publication(pub,title or Path(pdf).stem,tids,source_lang=source)
        # Legacy converter is a no-op for topics already created with structured blocks.
        upgrade_legacy_topics_to_blocks(repo)
        topics=repo.topic_table(); blocks=repo.blocks_table()
        table_count=int(repo.conn.execute("SELECT COUNT(*) FROM structured_tables").fetchone()[0])
        dropped=int(imported.attrs.get("dropped_running_headers",0))
    finally:
        repo.close()
    if suffix=='.idml':
        figure_count=int(imported.attrs.get("figure_count",0))
        missing_assets=int(imported.attrs.get("missing_asset_count",0))
        review_count=len(imported.attrs.get("review_items") or [])
        msg=(f"✅ Manuale InDesign importato come draft in {_it_count(len(imported),'sezione','sezioni')}, "
             f"{_it_count(table_count,'tabella','tabelle')} e {_it_count(figure_count,'figura','figure')}. "
             f"{_it_count(missing_assets,'immagine collegata non trovata','immagini collegate non trovate')}. ")
        if review_count:
            msg+=(f"{_it_count(review_count,'elemento non inserito automaticamente nel flusso principale','elementi non inseriti automaticamente nel flusso principale')} "
                  f"— verifica richiesta nella sezione «Elementi da verificare» qui sotto prima di poter approvare la sorgente. ")
        msg+=("L'importazione da InDesign resta conservativa: contenuto e struttura sono stati letti, "
              "il layout originale NON viene ricostruito. Prima dell'approvazione serve revisione umana.")
    else:
        msg=(f"✅ Manuale importato come draft in **{len(imported)} topic semantici** e **{table_count} tabelle strutturate**. "
             f"Header ricorrenti esclusi: **{dropped}**. La migrazione da PDF resta conservativa: "
             "prima dell’approvazione serve revisione umana.")
    _stamp_legacy_family_if_known(project_dir, copied)
    return msg, topics, blocks

def _stamp_legacy_family_if_known(project_dir, source_path) -> None:
    # Milestone 3C: if this exact file (by content hash) already participates in a
    # human-confirmed legacy family/revision/language relationship, stamp only the
    # two lightweight pointer fields onto the project manifest -- never a copy of
    # the lineage itself (see docs/product/ManualSync_Prompt_Pack... 3C design:
    # target_langs/TM/Revision Analysis stay consumers, LegacyFamilies/*.json stays
    # the single source of truth). Best-effort and silent: must never affect the
    # outcome of an import unrelated to 3C.
    try:
        file_hash = legacy_family.sha256_file(source_path)
        family_id = legacy_family.find_family_for_asset(DATA_HOME, file_hash)
        if family_id:
            update_project(project_dir, legacy_family_id=family_id, legacy_asset_id=legacy_family.derive_asset_id(file_hash))
    except Exception as exc:
        _app_log("Legacy family stamping skipped (non-fatal)", exc)

_GUIDED_LANG_CHOICES = ["Italiano (it-IT)", "English (en-US)", "Deutsch (de-DE)", "Français (fr-FR)", "Español (es-ES)"]
_GUIDED_LANG_DISPLAY_TO_CODE = {"Italiano (it-IT)": "it-IT", "English (en-US)": "en-US", "Deutsch (de-DE)": "de-DE", "Français (fr-FR)": "fr-FR", "Español (es-ES)": "es-ES"}
_GUIDED_LANG_DETECTED_TO_CODE = {"it": "it-IT", "en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES"}
_GUIDED_LANG_CONFIDENCE_MIN = 0.70  # same threshold already used by validate_language_combination
_GUIDED_BLOCK_TYPE_LABEL = {"paragraph": "Testo", "list_item": "Elenco", "note": "Nota", "warning": "Avviso", "table": "Tabella"}
_GUIDED_STATUS_LABEL = {"Migrated": "Da rivedere", "Approved": "Approvato"}

def _table_dims_lookup(project_dir) -> dict[str, str]:
    # Reads the SAME structured_tables data already used by the technical view
    # (no second backend) so the standard table can show a real, comprehensible
    # preview instead of a blank cell for table blocks.
    if not project_dir:
        return {}
    try:
        db=_ccms_db(project_dir)
    except FileNotFoundError:
        return {}
    repo=IndustrialRepository(db)
    try:
        rows=repo.conn.execute("SELECT table_id, columns_json, rows_json FROM structured_tables").fetchall()
    finally:
        repo.close()
    out={}
    for table_id, columns_json, rows_json in rows:
        try:
            cols=json.loads(columns_json or "[]")
            data_rows=json.loads(rows_json or "[]")
        except Exception:
            cols, data_rows = [], []
        ncols=len(cols) if cols else (len(data_rows[0]) if data_rows else 0)
        nrows=len(data_rows)
        first_cells=[str(c).strip() for c in (data_rows[0][:3] if data_rows else []) if str(c).strip()]
        label=f"Tabella — {nrows} righe × {ncols} colonne"
        if first_cells:
            label += f" (es. {', '.join(first_cells)}...)"
        out[str(table_id)]=label
    return out

def _simple_review_table(topics_df, blocks_df, table_dims=None) -> pd.DataFrame:
    # Milestone 3A: pure transform of the SAME topics/blocks data already shown
    # in "Dettagli tecnici (modalità avanzata)" — no second backend, no new
    # query here. Fewer, friendlier columns; no technical IDs/metadata.
    cols=["Sezione","Tipo di contenuto","Anteprima del testo","Pagina di origine","Stato"]
    if blocks_df is None or blocks_df.empty:
        return pd.DataFrame(columns=cols)
    table_dims=table_dims or {}
    title_by_topic=dict(zip(topics_df["topic_id"],topics_df["title"])) if topics_df is not None and not topics_df.empty else {}
    def page_of(refs):
        # Milestone 3B: DOCX has no real page numbers — the same "#page=N" ref
        # format is reused internally (no writer changes needed), but a .docx
        # source shows "Sezione N" here instead of a misleading bare page number.
        m=re.search(r"([^/\\#]+)#page=(\d+)",str(refs or ""))
        if not m:
            return None
        filename,num=m.group(1),m.group(2)
        # Milestone 3B.1: .doc provenance is deliberately kept as the original
        # .doc filename (not the temporary converted .docx), so this must
        # recognize both -- .doc has no real page concept either.
        if filename.lower().endswith((".docx",".doc")):
            return f"Sezione {num}"
        return int(num)
    def preview(text):
        t=re.sub(r"\s+"," ",str(text or "")).strip()
        return (t[:117]+"...") if len(t)>120 else t
    rows=[]
    for _,r in blocks_df.iterrows():
        block_type=str(r["block_type"])
        if block_type=="table":
            preview_text=table_dims.get(str(r.get("object_ref_id","") or ""), "Tabella importata")
        else:
            preview_text=preview(r["text"])
        rows.append({
            "Sezione": title_by_topic.get(r["topic_id"], r["topic_id"]),
            "Tipo di contenuto": _GUIDED_BLOCK_TYPE_LABEL.get(block_type, block_type),
            "Anteprima del testo": preview_text,
            "Pagina di origine": page_of(r["source_refs"]),
            "Stato": _GUIDED_STATUS_LABEL.get(str(r["status"]), str(r["status"])),
        })
    return pd.DataFrame(rows, columns=cols)

def _simple_review_table_for_project(project_dir) -> pd.DataFrame:
    try:
        db=_ccms_db(project_dir)
    except FileNotFoundError:
        return _simple_review_table(None, None)
    repo=IndustrialRepository(db)
    try:
        topics=repo.topic_table(); blocks=repo.blocks_table()
    finally:
        repo.close()
    return _simple_review_table(topics, blocks, _table_dims_lookup(project_dir))

def _simple_review_table_live_ui(project_dir, topics_df, blocks_df) -> pd.DataFrame:
    # Wrapper for the c_blocks.change(...) wiring below, which only has
    # Gradio component values (no project_dir) — fetches the same table
    # dimensions lookup so live updates (guided import or manual "Importa
    # come draft") show real table previews too, not just the initial render.
    return _simple_review_table(topics_df, blocks_df, _table_dims_lookup(project_dir))

def _guided_create_and_import(name, pdf_file, source_lang):
    # Milestone 3A: guided legacy onboarding. Pure orchestration — no new domain
    # logic. Reuses create_project_ui/import_manual_ui exactly as already tested.
    active, _stale_card, sidebar, _create_msg, _panel = create_project_ui(name, "", "", "", "", source_lang, "", "")
    import_msg, topics, blocks = import_manual_ui(active, pdf_file, source_lang, "PUB-MAIN", name)
    # The card returned by create_project_ui is pre-import (0 publications/blocks);
    # recompute it now so the Home reflects what was actually just imported.
    card = _project_card(active)
    result = f"{import_msg}\n\nProssimo passo: rivedi il contenuto importato prima di procedere."
    return active, card, sidebar, result, topics, blocks

def _guided_success_outputs(name, pdf_file, source_lang):
    active, card, sidebar, result, topics, blocks = _guided_create_and_import(name, pdf_file, source_lang)
    return (
        active, card, sidebar, result,
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=True),
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
        topics, blocks, pdf_file, source_lang, "PUB-MAIN", name,
    )

def _guided_ask_language_outputs():
    return (
        gr.update(), gr.update(), gr.update(),
        "Non riconosco con sicurezza la lingua del documento. Scegli quella corretta qui sotto per continuare.",
        gr.update(), gr.update(visible=True), gr.update(visible=False),
        gr.update(visible=True), gr.update(visible=True), gr.update(visible=True),
        gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
    )

def guided_new_project_from_pdf_ui(project_name, pdf_file):
    if not pdf_file:
        raise gr.Error("Carica un file PDF o Word (.docx o .doc) per continuare.")
    name = (project_name or "").strip() or Path(pdf_file).stem or "Nuovo manuale"
    suffix = Path(pdf_file).suffix.lower()
    if suffix == ".doc":
        # Same isolated conversion used by import_manual_ui; pdf_file (the
        # original .doc) is what flows onward through the guided outputs, so
        # provenance is unaffected by this language-detection-only conversion.
        try:
            with convert_legacy_doc_to_docx(pdf_file) as temp_docx:
                detected, confidence, _basis = detect_docx_language(temp_docx)
        except LegacyDocConversionError as exc:
            raise gr.Error(_LEGACY_DOC_FRIENDLY_ERROR) from exc
    elif suffix == ".docx":
        detected, confidence, _basis = detect_docx_language(pdf_file)
    else:
        detected, confidence, _basis = detect_document_language(pdf_file)
    source_lang = _GUIDED_LANG_DETECTED_TO_CODE.get(detected)
    if source_lang and confidence >= _GUIDED_LANG_CONFIDENCE_MIN:
        return _guided_success_outputs(name, pdf_file, source_lang)
    return _guided_ask_language_outputs()

def guided_confirm_language_ui(project_name, pdf_file, lang_choice):
    if not pdf_file:
        raise gr.Error("Carica un file PDF per continuare.")
    source_lang = _GUIDED_LANG_DISPLAY_TO_CODE.get(lang_choice or "")
    if not source_lang:
        raise gr.Error("Scegli la lingua del documento per continuare.")
    name = (project_name or "").strip() or Path(pdf_file).stem or "Nuovo manuale"
    return _guided_success_outputs(name, pdf_file, source_lang)

_LEGACY_FAMILY_EVIDENCE_LABELS = {
    "manual_code_match": "stesso codice manuale/prodotto",
    "title_match": "titolo del documento molto simile",
    "revision_match": "revisione riconosciuta",
    "language_match": "stessa lingua",
    "language_conflict": "lingue e revisioni in conflitto",
    "structure_match": "struttura delle sezioni molto simile",
    "metadata_match": "metadati del documento coincidenti",
    "duplicate_content": "contenuto identico",
    "human_override": "decisione umana esplicita",
}
_LEGACY_MAX_REVIEW_ITEMS = 24  # fixed-size Gradio row pool; see legacy_multi_analyze_ui
_LEGACY_NOT_RELATED = "Non sono correlati"
_LEGACY_CONFIRM = "Conferma"
_LEGACY_PENDING_LINK = "Collega alla stessa famiglia (nessuna revisione/lingua specifica)"
_LEGACY_NAME_REQUIRED_MSG = "Inserisci il tuo nome per continuare."

def _legacy_name_field_clear() -> tuple:
    # Required-field validation must read as an ordinary form error, not a
    # global gr.Error popup: clears the red border + inline message once a
    # name is present, without touching anything else (selection, summary).
    return gr.update(elem_classes=[]), gr.update(value="", visible=False)

def _legacy_name_field_invalid() -> tuple:
    return gr.update(elem_classes=["ms-field-invalid"]), gr.update(value=_LEGACY_NAME_REQUIRED_MSG, visible=True)

def _legacy_name_field_live_validate(name):
    # Fires on every keystroke so the error disappears the moment a valid
    # name is typed, with no need to click Conferma selezione again first.
    if str(name or "").strip():
        return _legacy_name_field_clear()
    return gr.update(), gr.update()

def _legacy_order_choice(older, newer) -> str:
    # UX fix: a concise, visually-distinguishable "A -> B" beats two long,
    # near-identical "X e' precedente a Y" / "Y e' precedente a X" strings
    # that differ only in which filename comes first -- easy to misread.
    # Used identically when building choices AND when matching the selection
    # back at confirm time, so the two can never drift apart.
    return f"{older.filename_original} → {newer.filename_original}"

def _legacy_evidence_phrases(evidence_list) -> str:
    # Milestone 3C: evidence stays structured (type/role/value) in the engine;
    # only this UI layer turns it into plain language. Never a raw score.
    seen: list[str] = []
    for e in evidence_list or []:
        label = _LEGACY_FAMILY_EVIDENCE_LABELS.get(e.get("type"), e.get("type"))
        if label not in seen:
            seen.append(label)
    return ", ".join(seen) if seen else "nessuna evidenza specifica"

def _legacy_build_review_items(result) -> list[dict]:
    """One actionable item per relation/pending-pair/ambiguous-pair. Each item
    carries its own explicit choice set -- there is no "confirm as-is" option
    for a revision_of whose order isn't already unambiguous (acceptance gap
    #2), pending same-family pairs are only linked if explicitly chosen
    (gap #3: the UI must not promise a membership it won't persist), and every
    ambiguous pair gets a real correction path, not just a warning (gap #1).
    """
    items: list[dict] = []
    for fi, fam_proposal in enumerate(result.families):
        members_by_hash = {m.file_sha256: m for m in fam_proposal.members}
        for ri, rel in enumerate(fam_proposal.relations):
            a = members_by_hash.get(rel["asset_a"]); b = members_by_hash.get(rel["asset_b"])
            if not a or not b:
                continue
            evidence_txt = _legacy_evidence_phrases(rel["evidence"])
            if rel["relation_type"] == "revision_of":
                newer_hash = rel.get("newer_asset_hint")
                if rel.get("order_confirmed") and newer_hash in members_by_hash:
                    newer = members_by_hash[newer_hash]; older = b if newer is a else a
                    label = f"Revisione: {older.filename_original} → {newer.filename_original} ({evidence_txt})"
                    choices = [_LEGACY_CONFIRM, _LEGACY_NOT_RELATED]
                    default = _LEGACY_CONFIRM
                else:
                    order_a = _legacy_order_choice(a, b)
                    order_b = _legacy_order_choice(b, a)
                    label = f"Se sono revisioni, scegli l'ordine corretto — {a.filename_original} / {b.filename_original} ({evidence_txt})"
                    choices = [_LEGACY_NOT_RELATED, order_a, order_b]
                    default = _LEGACY_NOT_RELATED  # no order may ever be assumed
            else:
                label = f"Variante linguistica: {a.filename_original} ↔ {b.filename_original} ({evidence_txt})"
                choices = [_LEGACY_CONFIRM, _LEGACY_NOT_RELATED]
                default = _LEGACY_CONFIRM
            items.append({"kind": "relation", "family_index": fi, "relation": rel,
                          "a": a, "b": b, "label": label, "choices": choices, "default": default})
        for pend in fam_proposal.pending_pairs:
            a = members_by_hash.get(pend["asset_a"]); b = members_by_hash.get(pend["asset_b"])
            if not a or not b:
                continue
            label = f"Stessa famiglia, relazione non chiara: {a.filename_original} ↔ {b.filename_original} ({_legacy_evidence_phrases(pend['evidence'])})"
            items.append({"kind": "pending", "family_index": fi, "pending": pend,
                          "a": a, "b": b, "label": label,
                          "choices": [_LEGACY_PENDING_LINK, _LEGACY_NOT_RELATED], "default": _LEGACY_NOT_RELATED})
    all_by_hash = {c.file_sha256: c for c in result.all_candidates}
    for ap in result.ambiguous_pairs:
        a = all_by_hash.get(ap["asset_a"]); b = all_by_hash.get(ap["asset_b"])
        if not a or not b:
            continue
        evidence_txt = _legacy_evidence_phrases(ap["evidence"])
        label = f"Da verificare: {a.filename_original} e {b.filename_original} — segnali in conflitto ({evidence_txt})"
        correct_rev_ab = _legacy_order_choice(a, b)
        correct_rev_ba = _legacy_order_choice(b, a)
        choices = [f"{_LEGACY_NOT_RELATED} (consigliato)", correct_rev_ab, correct_rev_ba]
        # "sono la stessa revisione in lingue diverse" makes a language claim --
        # it must never be offered unless that claim is actually plausible.
        # Both languages confirmed and equal -> the claim is false on its face.
        # Either language unconfirmed -> 3C has no explicit language-correction
        # step to pair with this choice, so it stays unoffered rather than let
        # a human override create a linguistically impossible relation.
        lang_a = legacy_family.confirmed_language(a.language_detected, a.language_confidence)
        lang_b = legacy_family.confirmed_language(b.language_detected, b.language_confidence)
        if lang_a and lang_b and lang_a != lang_b:
            choices.append("Correggi: sono la stessa revisione in lingue diverse")
        items.append({"kind": "ambiguous", "a": a, "b": b, "label": label, "evidence": ap["evidence"],
                      "choices": choices, "default": f"{_LEGACY_NOT_RELATED} (consigliato)"})
    return items

def _render_legacy_family_overview(result, review_items) -> str:
    lines: list[str] = []
    if review_items:
        noun = "elemento" if len(review_items) == 1 else "elementi"
        lines.append(f"### {len(review_items)} {noun} da rivedere qui sotto")
        if len(review_items) > _LEGACY_MAX_REVIEW_ITEMS:
            lines.append(f"⚠️ Troppi elementi per una sola analisi (mostrati i primi {_LEGACY_MAX_REVIEW_ITEMS}). Analizza un sottoinsieme alla volta per i rimanenti.")
    if result.duplicate_groups:
        lines.append("### Copie identiche rilevate")
        for dg in result.duplicate_groups:
            kept = Path(dg["kept_path"]).name
            dups = ", ".join(Path(p).name for p in dg["duplicate_paths"])
            lines.append(f"- **{dups}** risulta identico a **{kept}** (stesso contenuto): trattato come un solo documento, non come una revisione.")
    if result.unrelated:
        names = ", ".join(sorted(c.filename_original for c in result.unrelated))
        lines.append(f"### Documenti indipendenti\n{names} — non risultano collegati a nessun altro documento caricato.")
    if result.ignored:
        lines.append("### File non considerati")
        for ig in result.ignored:
            lines.append(f"- **{Path(ig['path']).name}**: {ig['reason']}")
    if not lines:
        return "Nessun documento analizzato."
    return "\n".join(lines)

def legacy_multi_analyze_ui(files_multi, files_dir):
    paths: list[str] = []
    for group in (files_multi, files_dir):
        if not group:
            continue
        paths.extend(group if isinstance(group, list) else [group])
    if not paths:
        raise gr.Error("Seleziona almeno un file oppure una cartella con i tuoi manuali.")
    result = legacy_family.analyze_paths(paths)
    review_items = _legacy_build_review_items(result)[:_LEGACY_MAX_REVIEW_ITEMS]
    summary_md = _render_legacy_family_overview(result, review_items)
    radio_updates = [
        gr.update(label=item["label"], choices=item["choices"], value=item["default"], visible=True)
        if i < len(review_items) else gr.update(visible=False)
        for i, item in enumerate(review_items + [None] * (_LEGACY_MAX_REVIEW_ITEMS - len(review_items)))
    ]
    return [(result, review_items), summary_md, gr.update(visible=bool(review_items))] + radio_updates

def legacy_multi_confirm_ui(state, confirmed_by, *radio_values):
    # Nothing was written to disk before this click (analysis above is fully
    # ephemeral) -- this is the single point where a human decision produces
    # the persistent LegacyFamilies/<family_id>.json lineage record.
    if not state:
        raise gr.Error("Esegui prima l'analisi dei documenti.")
    result, review_items = state
    if not str(confirmed_by or "").strip():
        # Ordinary required-field validation, not an exceptional condition:
        # no gr.Error, nothing persisted, selections/summary/row untouched --
        # only the name field itself is flagged inline.
        return (gr.update(), gr.update()) + _legacy_name_field_invalid() \
            + tuple(gr.update() for _ in range(_LEGACY_MAX_REVIEW_ITEMS))
    confirmed_by = str(confirmed_by).strip()

    groups: dict[object, dict] = {}  # group key -> {"candidates": {hash: Candidate}, "relations": [...]}
    any_decision = False
    for item, choice in zip(review_items, radio_values):
        if choice is None or choice.startswith(_LEGACY_NOT_RELATED):
            continue
        any_decision = True
        group_key = item.get("family_index", ("pair", item["a"].file_sha256, item["b"].file_sha256))
        bucket = groups.setdefault(group_key, {"candidates": {}, "relations": []})
        bucket["candidates"][item["a"].file_sha256] = item["a"]
        bucket["candidates"][item["b"].file_sha256] = item["b"]

        if item["kind"] == "relation":
            # Original automatic evidence is copied, never replaced -- a human
            # order decision is recorded as an ADDITIONAL human_override
            # evidence entry, never disguised as manual_code_match/language_match
            # (those types must only ever represent signals the engine actually
            # observed).
            rel = dict(item["relation"])
            rel["evidence"] = list(rel.get("evidence") or [])
            if rel["relation_type"] == "revision_of" and not rel.get("order_confirmed"):
                # The human, not the algorithm, is resolving the order here --
                # exactly the case acceptance gap #2 requires to be explicit.
                order_desc = None
                if choice == _legacy_order_choice(item["a"], item["b"]):
                    rel["order_confirmed"] = True; rel["newer_asset_hint"] = item["b"].file_sha256
                    order_desc = f"{item['a'].filename_original} -> {item['b'].filename_original}"
                elif choice == _legacy_order_choice(item["b"], item["a"]):
                    rel["order_confirmed"] = True; rel["newer_asset_hint"] = item["a"].file_sha256
                    order_desc = f"{item['b'].filename_original} -> {item['a'].filename_original}"
                if order_desc:
                    rel["evidence"].append({
                        "type": "human_override", "role": "strong",
                        "value": "relation_type=revision_of; decision=order_chosen_by_human",
                        "detail": f"confirmed_by={confirmed_by}; order_chosen={order_desc}",
                    })
            bucket["relations"].append(rel)
        elif item["kind"] == "pending":
            # Membership-only: candidates already added above, no relation
            # persisted -- matches exactly what the UI now promises (gap #3).
            pass
        elif item["kind"] == "ambiguous":
            # The original CONFLICTING automatic evidence is preserved as-is
            # (never rewritten to look consistent) and a human_override entry
            # is appended documenting the human decision that overrides it.
            original_evidence = list(item.get("evidence") or [])
            if choice == _legacy_order_choice(item["a"], item["b"]) or choice == _legacy_order_choice(item["b"], item["a"]):
                older, newer = (item["a"], item["b"]) if choice == _legacy_order_choice(item["a"], item["b"]) else (item["b"], item["a"])
                override = {
                    "type": "human_override", "role": "strong",
                    "value": "relation_type=revision_of; decision=" + choice,
                    "detail": f"confirmed_by={confirmed_by}; order_chosen={older.filename_original} -> {newer.filename_original}",
                }
                bucket["relations"].append({
                    "relation_type": "revision_of", "asset_a": older.file_sha256, "asset_b": newer.file_sha256,
                    "order_confirmed": True, "newer_asset_hint": newer.file_sha256,
                    "evidence": original_evidence + [override],
                })
            elif choice.startswith("Correggi: sono la stessa revisione"):
                override = {
                    "type": "human_override", "role": "strong",
                    "value": "relation_type=language_variant_of; decision=" + choice,
                    "detail": f"confirmed_by={confirmed_by}",
                }
                bucket["relations"].append({
                    "relation_type": "language_variant_of", "asset_a": item["a"].file_sha256, "asset_b": item["b"].file_sha256,
                    "order_confirmed": False, "newer_asset_hint": None,
                    "evidence": original_evidence + [override],
                })

    if not any_decision:
        return ("Nessuna relazione confermata: i documenti verranno trattati come indipendenti tra loro.",
                gr.update(visible=False)) + _legacy_name_field_clear() \
            + tuple(gr.update(visible=False) for _ in range(_LEGACY_MAX_REVIEW_ITEMS))

    summaries = []
    for bucket in groups.values():
        data = legacy_family.persist_confirmation(
            DATA_HOME, candidates=list(bucket["candidates"].values()), relations=bucket["relations"], confirmed_by=confirmed_by,
        )
        summaries.append(f"✅ Famiglia **{data['family_id']}** salvata: **{len(data['relations'])}** relazioni, **{len(data['members'])}** documenti collegati.")
    summaries.append(
        "Per creare i progetti, usa **“Ho già dei manuali: importali”** per ciascun documento: "
        "se un file era coinvolto in una relazione confermata, il progetto lo riconoscerà automaticamente."
    )
    return ("\n\n".join(summaries), gr.update(visible=False)) + _legacy_name_field_clear() \
        + tuple(gr.update(visible=False) for _ in range(_LEGACY_MAX_REVIEW_ITEMS))

def onboarding_back_ui(project_dir):
    # UX acceptance fix: every onboarding sub-panel opened from the initial
    # three-way choice (empty project / single legacy import / multi-doc
    # family import) must offer a visible way back to that choice, without a
    # browser refresh and without creating/persisting anything. Re-showing
    # onboarding_choice_panel unconditionally would be wrong when this panel
    # was instead opened via the "+ Nuovo progetto" quick action while a
    # project is already active (that panel must stay hidden per its own
    # project_state.change wiring) -- so this mirrors that same condition.
    return gr.update(visible=False), gr.update(visible=not bool(project_dir))

def guided_approve_source_ui(project_dir, reviewer, confirmed):
    # Milestone 3A: simplified approval entry point for the standard Contenuti view.
    # Reuses approve_source_ui exactly as already tested. `confirmed` is the
    # user's own explicit checkbox value — never hardcoded — so approve_source_ui's
    # existing fail-closed check ("Serve la conferma esplicita...") still applies.
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    try:
        msg = approve_source_ui(project_dir, "PUB-MAIN", reviewer, confirmed)
    except gr.Error as exc:
        # Acceptance fix: a blocked approval (pending review items, missing
        # confirmation, corrupt review state...) is an expected fail-closed
        # outcome, not a crash. approve_source_ui never writes anything
        # before raising here, so project/sidebar/table are genuinely
        # unchanged -- refresh them normally and surface the block inline,
        # next to the approval button, instead of letting the exception
        # propagate and leave this callback's other outputs in an error
        # state.
        msg = f"⛔ {exc}"
    return msg, _project_card(project_dir), _sidebar_context(project_dir), _simple_review_table_for_project(project_dir)

def approve_source_ui(project_dir, publication_id, reviewer, confirmed):
    if not confirmed: raise gr.Error("Serve la conferma esplicita della revisione umana.")
    try:
        pending=_idml_pending_review_items(project_dir, publication_id)
    except IdmlReviewCorruptError as exc:
        raise gr.Error(f"Impossibile approvare: lo stato di revisione IDML non è leggibile o è corrotto "
                        f"({exc}). Ripristina il file o riesegui l'importazione prima di approvare.") from exc
    if pending:
        raise gr.Error(f"Ci sono {len(pending)} elementi da verificare prima di poter approvare questa sorgente. "
                        "Prendi una decisione per ciascuno nella sezione «Elementi da verificare» qui sopra.")
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        # 4B.1 fix: bulk_approve_publication() (AuthoringService) also
        # reconciles/closes any OPEN review_tasks it renders moot -- the
        # legacy bulk-SQL approval used to leave those orphaned OPEN
        # forever, which readiness's review_queue(OPEN) gate could then
        # keep seeing even after "Approva sorgente" was clicked.
        result = AuthoringService(repo).bulk_approve_publication(publication_id, actor=reviewer or 'Human reviewer')
    except ValueError as e:
        raise gr.Error(str(e))
    finally: repo.close()
    extra = f" {result['review_tasks_closed']} task di revisione in sospeso sono stati riconciliati." if result.get('review_tasks_closed') else ""
    return f"✅ Sorgente approvata dal revisore umano.{extra}"

def review_queue_ui(project_dir):
    if not project_dir: return pd.DataFrame()
    repo=IndustrialRepository(_ccms_db(project_dir))
    try: return repo.review_queue(status='OPEN')
    finally: repo.close()

def review_decide_ui(project_dir, review_id, decision, actor, comment):
    # 4B.1 fix: this is the first real, product-served path that calls
    # decide_review() -- the 4A audit found it was only ever invoked from
    # app_engineering.py, a module the shipped launcher never imports, so
    # a review request opened via grounded_queue_ui could never actually
    # be closed through the proper mechanism.
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not review_id: raise gr.Error("Review ID obbligatorio.")
    if not str(actor or '').strip(): raise gr.Error("Il revisore è obbligatorio per registrare una decisione.")
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        new_status = repo.decide_review(int(review_id), decision=decision, actor=actor, comment=comment or '')
        queue = repo.review_queue(status='OPEN')
    except (KeyError, ValueError) as e:
        raise gr.Error(str(e))
    finally: repo.close()
    return f"✅ Review #{int(review_id)} decisa: nuovo stato **{new_status}**.", queue

def grounding_ingest_ui(project_dir, source_pdf, source_text, document_id, title):
    if not project_dir or not document_id: raise gr.Error("Progetto e Document ID sono obbligatori.")
    if not source_pdf and not str(source_text or '').strip(): raise gr.Error("Carica un PDF o incolla una fonte.")
    store=GroundingStore(Path(project_dir)/'ManualSync_Grounding.sqlite')
    try:
        if source_pdf:
            srcdir=Path(project_dir)/'Evidence'; srcdir.mkdir(exist_ok=True); copied=srcdir/Path(source_pdf).name; shutil.copy2(source_pdf,copied)
            chunks=store.ingest_pdf(document_id.strip(),title or document_id,copied)
        else: chunks=store.ingest_text(document_id.strip(),title or document_id,source_text,source_ref_prefix=document_id.strip())
    finally: store.close()
    return f"✅ Fonte **{document_id}** indicizzata in {len(chunks)} evidenze verificabili.",chunks

def grounded_generate_ui(project_dir, instruction, mode):
    if not project_dir or not instruction: raise gr.Error("Progetto e richiesta sono obbligatori.")
    gp=Path(project_dir)/'ManualSync_Grounding.sqlite'
    if not gp.exists(): raise gr.Error("Prima indicizza almeno una fonte approvata.")
    store=GroundingStore(gp); ledger=GenerationLedger(Path(project_dir)/'ManualSync_Generation.sqlite')
    try:
        orch=GenerativeOrchestrator(store,ledger,SafeSourceComposer())
        r=orch.propose(GenerationRequest(instruction=instruction,mode=mode or 'procedure'))
        v=r.get('validation',{})
        conflicts=len(v.get('request_conflicts',[]) or [])
        unsupported=len(v.get('unsupported_request_claims',[]) or [])
        source='PASS' if v.get('source_support_pass') else 'FAIL'
        relevance='PASS' if v.get('query_relevance_pass') else 'FAIL'
        if v.get('PASS'):
            decision='✅ **Decisione: REVIEW REQUIRED** — la proposta è supportata dalle fonti ma richiede approvazione umana.'
        elif conflicts or unsupported:
            decision=f'⛔ **Decisione: BLOCKED** — conflitti tecnici: **{conflicts}** · claim tecnici non supportati: **{unsupported}**.'
        elif r.get('status')=='NEEDS_SOURCE':
            decision='⛔ **Decisione: BLOCKED** — nessuna evidenza sufficientemente pertinente trovata.'
        else:
            decision='⛔ **Decisione: BLOCKED** — grounding/relevance gate non superato.'
        msg=(f"{decision}  \n**Source support:** {source} · **Query relevance:** {relevance} · "
             f"**Technical conflicts:** {conflicts} · **Unsupported request claims:** {unsupported}  \n"
             f"Proposal ID: **{r.get('proposal_id','')}**")
        return msg,r.get('draft',''),r.get('evidence',pd.DataFrame()),r.get('claims',pd.DataFrame()),r.get('proposal_id','')
    finally: ledger.close(); store.close()

def grounded_queue_ui(project_dir, proposal_id, topic_id, block_id, block_type, position, reviewer):
    if not all([project_dir,proposal_id,topic_id,block_id]): raise gr.Error("Proposal, Topic ID e Block ID sono obbligatori.")
    ledger=GenerationLedger(Path(project_dir)/'ManualSync_Generation.sqlite')
    try: row=ledger.conn.execute("SELECT * FROM generation_proposals WHERE proposal_id=?",(proposal_id,)).fetchone()
    finally: ledger.close()
    if not row or row['status']!='GROUNDING_PASS': raise gr.Error("La proposta non ha superato il grounding gate.")
    validation=json.loads(row['validation_json']); refs=validation.get('source_refs',[])
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        repo.add_block(block_id.strip(),topic_id.strip(),block_type or 'paragraph',position=int(position or 1),text=row['draft'],status='Draft',source_refs=refs,evidence_status='SOURCE_GROUNDED')
        rid=repo.request_review('block',block_id.strip(),requested_by='ManualSync AI',assigned_to=reviewer or '',comment=f"Grounded proposal {proposal_id}; human approval required")
    except Exception as e: raise gr.Error(str(e))
    finally: repo.close()
    return f"✅ Proposta inserita come **Draft/In Review**. Review #{rid}. Nessun contenuto AI è stato approvato automaticamente."

def approved_objects_ui(project_dir):
    if not project_dir: return pd.DataFrame()
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        blocks=repo.blocks_table()
        return blocks[blocks['status'].astype(str).isin(['Approved','Published'])] if not blocks.empty else blocks
    finally: repo.close()

# ============================================================================
# Milestone 4B.2: Professional Topic & Block Editor ("Studio"), built on the
# 4B.1 AuthoringService foundation. Every rule (CAS save, draft recovery,
# status invalidation, reorder, archive/restore, revert) lives in
# authoring_service.py; every presentation decision (labels, outline
# flattening, breadcrumbs, history rows) lives in authoring_view.py -- these
# _studio_*_ui functions are thin Gradio-formatting wrappers, exactly the
# same discipline established for Compliance (3.5B) and Studio's own review
# panel (4B.1). A failed action NEVER raises gr.Error on this multi-output
# panel (would corrupt sibling outputs) -- always an inline message inside
# the same rebuilt bundle.
# ============================================================================
def _studio_status_html(detail: dict) -> str:
    parts = [f"<div class='ms-card'><b>{html.escape(detail['status_label'])}</b> · versione {detail['version']}"]
    if detail.get("archived"):
        parts.append(" · <span class='ms-block'>Archiviato</span>")
    parts.append("</div>")
    if detail.get("draft_notice"):
        parts.append(f"<div class='ms-prod' style='margin-top:6px'>📝 {html.escape(detail['draft_notice'])}</div>")
    if detail.get("open_review_notice"):
        parts.append(f"<div class='ms-prod' style='margin-top:6px'>👁 {html.escape(detail['open_review_notice'])}</div>")
    return "".join(parts)

def _studio_tech_html_topic(detail: dict) -> str:
    refs = ", ".join(detail.get("source_refs") or []) or "—"
    return (f"<div class='ms-muted' style='font-size:12px'>topic_id: <code>{html.escape(detail['topic_id'])}</code><br>"
            f"source_refs: {html.escape(refs)}<br>archived_at: {html.escape(str(detail.get('archived_at') or '—'))}</div>")

def _studio_tech_html_block(detail: dict) -> str:
    refs = ", ".join(detail.get("source_refs") or []) or "—"
    return (f"<div class='ms-muted' style='font-size:12px'>block_id: <code>{html.escape(detail['block_id'])}</code> · "
            f"block_type: <code>{html.escape(detail['block_type'])}</code><br>topic_id: <code>{html.escape(detail['topic_id'])}</code><br>"
            f"source_refs: {html.escape(refs)}<br>archived_at: {html.escape(str(detail.get('archived_at') or '—'))}</div>")

def _studio_tech_html_shared_object(repo, object_type: str, object_id: str, tfdetail: dict, block_detail_: dict) -> str:
    refs = ", ".join(tfdetail.get("source_refs") or []) or "—"
    id_label = "table_id" if object_type == "table" else "figure_id"
    extra = f"file_path: <code>{html.escape(str(tfdetail.get('file_path') or '—'))}</code><br>" if object_type == "figure" else ""
    impact = av.impact_summary(repo, object_type, object_id)
    impact_html = ("usato in: " + ", ".join(html.escape(t) for t in impact)) if impact else "usato in un solo topic"
    return (f"<div class='ms-muted' style='font-size:12px'>{id_label}: <code>{html.escape(object_id)}</code> · "
            f"block_id (contenitore): <code>{html.escape(block_detail_['block_id'])}</code><br>{extra}"
            f"source_refs: {html.escape(refs)}<br>reference_count: {tfdetail['reference_count']} — {impact_html}</div>")

_ST_EMPTY_TABLE_COLS = ["Elemento", "Tipo", "Stato", "Versione", "Archiviato"]

def _studio_pub_header_html(repo, publication_id: str) -> str:
    # UX clarity fix: "Editor professionale" alone never told the user WHICH
    # manual they were looking at -- the static header just said the generic
    # word "Pubblicazione". This shows the real, current publication title.
    row = repo.conn.execute("SELECT title FROM publications WHERE publication_id=?", (publication_id,)).fetchone()
    if not row:
        return f"<div class='ms-muted'>Nessuna publication «{html.escape(publication_id)}».</div>"
    return f"<div class='ms-prod'>Pubblicazione: <b>{html.escape(row['title'])}</b></div>"

def _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, *, save_msg="", review_msg="",
                        shared_confirmed=False, shared_impact_token=None, diff_output=""):
    # UX clarity fix (and a real Gradio 6.5.1 bug worked around here): the
    # "nothing selected -> hide the action controls, not just grey them
    # out" requirement was first tried as a single gr.Group(visible=...)
    # wrapping the whole action area. Reproduced against a minimal isolated
    # repro app: a component's OWN visible=False update does not reliably
    # reach the browser when it is nested inside a Group whose own
    # visibility does not change in that same response (confirmed both in
    # this app and in the standalone repro). So instead, every action
    # button/field gets its OWN visible= set directly here (alongside its
    # existing interactive=), exactly like title_upd/text_upd already did
    # correctly all along -- no nested dynamic Group anywhere, including
    # for the new 4C table/figure editor fields below.
    repo, svc = av.open_service(project_dir)
    try:
        publication_id = (publication_id or "").strip() or "PUB-MAIN"
        pub_header = _studio_pub_header_html(repo, publication_id)
        # 4D Shared Table Unlock Editing Fix: st_table_grid was a SINGLE
        # gr.Dataframe whose `interactive` prop got toggled False->True
        # dynamically (shared/unconfirmed -> confirmed via 'Aggiorna
        # ovunque'). Diagnosed live: Gradio 6.5.1's virtualized Dataframe
        # (<svelte-virtual-table-viewport>) DOES apply that toggle
        # correctly when it receives a complete, real pointer+mouse event
        # sequence -- but relying on dynamic interactive-toggling of an
        # ALREADY-MOUNTED virtualized grid component is inherently more
        # fragile than never doing it at all (undocumented third-party
        # behavior, sensitive to exactly which events the browser/input
        # path delivers). Root-caused and fixed the robust way the ticket
        # itself pre-authorized: TWO mutually exclusive components --
        # st_table_grid_view (permanently interactive=False, shown while
        # read-only/undecided) and st_table_grid_edit (permanently
        # interactive=True, shown once editable) -- swapped only via
        # `visible=`, the exact same proven mechanism already used
        # everywhere else in this function. Neither component's own
        # `interactive` value is EVER toggled after creation.
        #
        # 4D freeze fix: hiding a twin must NEVER also push it a new
        # value/row_count/column_count. The previous version cleared the
        # twin being hidden to an empty df via _fixed_grid_update() in the
        # very same response that populated the OTHER twin with real data
        # and flipped ITS visibility on -- i.e. both virtualized Dataframe
        # components underwent a simultaneous visible+shape+value mutation
        # in one Gradio update batch. Reproduced live: exactly this
        # double-mutation (confirmed shared table -> "Aggiorna ovunque")
        # is what left the browser tab unresponsive with several elements
        # stuck "processing" -- consistent with the virtualized grid's
        # frontend (svelte-virtual-table-viewport) not tolerating two of
        # its own instances being resized/repopulated at once. A hidden
        # twin's stale value is never read (Save only reads st_table_
        # grid_edit; the view twin is display-only) and gets overwritten
        # with fresh data the next time it is legitimately shown, so there
        # is nothing to gain from clearing it -- only a component update to
        # avoid. `hidden_grid` therefore carries `visible=False` alone.
        hidden_grid = gr.update(visible=False)
        hidden_field = gr.update(value="", visible=False)
        hidden_btn = gr.update(visible=False, interactive=False)
        hidden_notice = gr.update(value="", visible=False)
        if not repo.conn.execute("SELECT 1 FROM publications WHERE publication_id=?", (publication_id,)).fetchone():
            empty = pd.DataFrame(columns=_ST_EMPTY_TABLE_COLS)
            return (gr.update(value=empty), gr.update(choices=[], value=None), None, None, None,
                    "Pubblicazione", f"<div class='ms-muted'>Nessuna publication «{html.escape(publication_id)}».</div>",
                    hidden_field, hidden_field, hidden_notice,
                    hidden_btn, hidden_btn, gr.update(visible=False), gr.update(visible=False),
                    hidden_btn, hidden_btn, hidden_btn, hidden_btn,
                    hidden_btn, review_msg, gr.update(visible=False), "", pd.DataFrame(), save_msg,
                    pub_header,
                    None, None,
                    hidden_field, hidden_field, hidden_grid, hidden_grid,
                    gr.update(value=None, visible=False), hidden_field, hidden_field, gr.update(visible=False),
                    hidden_notice, gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
                    False, None, hidden_notice, hidden_btn, hidden_btn, gr.update(visible=False), diff_output)

        rows = av.outline_rows(repo, publication_id, include_archived=bool(show_archived))
        table = pd.DataFrame(av.outline_table(rows)) if rows else pd.DataFrame(columns=_ST_EMPTY_TABLE_COLS)
        picker_choices = av.open_choices(rows)
        picker_value = f"{kind}:{object_id}" if kind and object_id else None
        if picker_value not in [v for _, v in picker_choices]:
            picker_value, kind, object_id = None, None, None

        if not kind or not object_id:
            return (gr.update(value=table), gr.update(choices=picker_choices, value=None), None, None, None,
                    "Pubblicazione", "<div class='ms-muted'>Seleziona un topic o un blocco dalla struttura a sinistra, oppure crea un nuovo topic.</div>",
                    hidden_field, hidden_field, hidden_notice,
                    hidden_btn, hidden_btn, gr.update(visible=False), gr.update(visible=False),
                    hidden_btn, hidden_btn, hidden_btn, hidden_btn,
                    hidden_btn, review_msg, gr.update(visible=False), "", pd.DataFrame(), save_msg,
                    pub_header,
                    None, None,
                    hidden_field, hidden_field, hidden_grid, hidden_grid,
                    gr.update(value=None, visible=False), hidden_field, hidden_field, gr.update(visible=False),
                    hidden_notice, gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
                    False, None, hidden_notice, hidden_btn, hidden_btn, gr.update(visible=False), diff_output)

        breadcrumb = av.breadcrumb(rows, kind, object_id)
        block_type = None
        object_ref_id = None
        table_title_upd = hidden_field
        table_caption_upd = hidden_field
        table_grid_view_upd = hidden_grid
        table_grid_edit_upd = hidden_grid
        figure_preview_upd = gr.update(value=None, visible=False)
        figure_caption_upd = hidden_field
        figure_alt_upd = hidden_field
        figure_replace_row_visible = False
        shared_notice_upd = hidden_notice
        new_table_row_visible = False
        new_figure_row_visible = False
        table_rowcol_row_visible = False
        # 4D: reuse-aware shared-object decision UX defaults -- overridden
        # below only inside the is_table/is_figure branches, where
        # tdetail["is_shared"]/fdetail["is_shared"] (AuthoringService.
        # get_reuse_impact()'s block-level-precise count, via table_detail/
        # figure_detail) actually applies. shared_confirmed only ever comes
        # from the explicit "Aggiorna ovunque" button callback below --
        # every OTHER navigation path (open/refresh/save/revert/fork) calls
        # this function without it, which defaults it back to False, so a
        # fresh edit session on a still-shared object always requires the
        # decision again (deliberate, never sticky across saves).
        reuse_impact_upd = hidden_notice
        update_everywhere_btn_visible = False
        fork_local_btn_visible = False

        if kind == "topic":
            detail = av.topic_detail(svc, object_id)
            status_detail = detail
            title_upd = gr.update(value=detail["title"], visible=True, interactive=not detail["archived"])
            text_upd = gr.update(value="", visible=False)
            readonly_notice = hidden_notice
            can_edit = not detail["archived"]
            version_for_save = detail["version"]
            history = pd.DataFrame(av.topic_history_rows(svc, object_id))
            tech_html = _studio_tech_html_topic(detail)
            new_block_row_visible = not detail["archived"]
            new_table_row_visible = not detail["archived"]
            new_figure_row_visible = not detail["archived"]
            submit_review_visible = True
        else:
            detail = av.block_detail(svc, object_id)
            status_detail = detail
            block_type = detail["block_type"]
            editable_text = detail["editable"]
            is_table = block_type == "table"
            is_figure = block_type == "figure"
            title_upd = hidden_field
            text_upd = gr.update(value=detail["text"], visible=editable_text, interactive=(editable_text and not detail["archived"]))
            readonly_notice = hidden_notice
            new_block_row_visible = False
            submit_review_visible = not (is_table or is_figure)

            if is_table:
                object_ref_id = detail["object_ref_id"]
                tdetail = av.table_detail(svc, object_ref_id)
                status_detail = tdetail
                version_for_save = tdetail["version"]
                can_edit = not detail["archived"] and (not tdetail["is_shared"] or shared_confirmed)
                cols = tdetail["columns"] or ["Colonna 1"]
                grid_rows = tdetail["rows"] if tdetail["rows"] else [["" for _ in cols]]
                table_title_upd = gr.update(value=tdetail["title"], visible=True, interactive=can_edit)
                table_caption_upd = gr.update(value=tdetail["caption"], visible=True, interactive=can_edit)
                grid_df = pd.DataFrame(grid_rows, columns=[str(c) for c in cols])
                # Shared Table Unlock Editing Fix: st_table_grid_view
                # (interactive=False, never toggled) shows the SAME data
                # read-only while can_edit is False; st_table_grid_edit
                # (interactive=True, never toggled) shows it once
                # editable. Exactly one of the two is ever visible.
                # interactive= is re-affirmed explicitly on every push
                # (False for the view twin, True for the edit twin) --
                # always the SAME fixed value each component was created
                # with, never toggled across renders, but pushed
                # explicitly so the update graph is self-verifying rather
                # than relying on an implicit/unstated component default.
                #
                # 4D freeze fix: the twin being HIDDEN gets `hidden_grid`
                # (visible=False only, no value/row_count/column_count) --
                # never a fresh _fixed_grid_update() -- so only the twin
                # becoming/staying VISIBLE undergoes a value+shape mutation
                # in this response. See the `hidden_grid` comment above for
                # why pushing both twins at once (one clearing, one
                # populating) is what produced the browser freeze.
                if can_edit:
                    table_grid_view_upd = hidden_grid
                    table_grid_edit_upd = _fixed_grid_update(grid_df, visible=True, interactive=True)
                else:
                    table_grid_view_upd = _fixed_grid_update(grid_df, visible=True, interactive=False)
                    table_grid_edit_upd = hidden_grid
                # Real Gradio 6.5.1 bug found in browser stabilization: the
                # Dataframe's own built-in right-click "Add row"/"Add
                # column" context-menu mutates the visible grid but does
                # NOT reliably update the value Gradio submits on a
                # subsequent event (confirmed with a clean single-cycle
                # test against this exact app: DOM showed 5 rows, Save
                # still persisted only the original 4). These dedicated
                # buttons are the supported, tested mechanism -- they
                # round-trip through Python, so the value st_table_grid
                # holds afterward is exactly what gets saved.
                table_rowcol_row_visible = can_edit
                if tdetail["is_shared"]:
                    ris = tdetail["reuse_impact_summary"]
                    if shared_confirmed:
                        shared_notice_upd = gr.update(
                            value=f"🔗 Modalità «Aggiorna ovunque» attiva — il salvataggio aggiornerà tutti i "
                                  f"{tdetail['reference_count']} riferimenti.", visible=True)
                    else:
                        shared_notice_upd = gr.update(value=f"🔗 {tdetail['shared_notice']}", visible=True)
                        update_everywhere_btn_visible = not detail["archived"]
                        fork_local_btn_visible = not detail["archived"]
                    lines = [ris["usage_line"]] + ris["topic_lines"] + ris["notes"]
                    reuse_impact_upd = gr.update(value="  \n".join(x for x in lines if x), visible=True)
                elif detail["archived"]:
                    readonly_notice = gr.update(value=f"⛔ {av.archived_message('block', object_id)}", visible=True)
                history = pd.DataFrame(av.table_history_rows(svc, object_ref_id))
                tech_html = _studio_tech_html_shared_object(repo, "table", object_ref_id, tdetail, detail)
            elif is_figure:
                object_ref_id = detail["object_ref_id"]
                fdetail = av.figure_detail(svc, object_ref_id)
                status_detail = fdetail
                version_for_save = fdetail["version"]
                can_edit = not detail["archived"] and (not fdetail["is_shared"] or shared_confirmed)
                asset_ok = bool(fdetail["file_path"]) and not fdetail["asset_missing"]
                figure_preview_upd = gr.update(value=(fdetail["file_path"] if asset_ok else None), visible=True)
                figure_caption_upd = gr.update(value=fdetail["caption"], visible=True, interactive=can_edit)
                figure_alt_upd = gr.update(value=fdetail["alt_text"], visible=True, interactive=can_edit)
                figure_replace_row_visible = can_edit
                if fdetail["is_shared"]:
                    ris = fdetail["reuse_impact_summary"]
                    if shared_confirmed:
                        shared_notice_upd = gr.update(
                            value=f"🔗 Modalità «Aggiorna ovunque» attiva — il salvataggio aggiornerà tutti i "
                                  f"{fdetail['reference_count']} riferimenti.", visible=True)
                    else:
                        shared_notice_upd = gr.update(value=f"🔗 {fdetail['shared_notice']}", visible=True)
                        update_everywhere_btn_visible = not detail["archived"]
                        fork_local_btn_visible = not detail["archived"]
                    lines = [ris["usage_line"]] + ris["topic_lines"] + ris["notes"]
                    reuse_impact_upd = gr.update(value="  \n".join(x for x in lines if x), visible=True)
                elif detail["archived"]:
                    readonly_notice = gr.update(value=f"⛔ {av.archived_message('block', object_id)}", visible=True)
                elif fdetail["asset_missing"]:
                    readonly_notice = gr.update(value="🖼️ Immagine non disponibile.", visible=True)
                history = pd.DataFrame(av.figure_history_rows(svc, object_ref_id))
                tech_html = _studio_tech_html_shared_object(repo, "figure", object_ref_id, fdetail, detail)
            else:
                version_for_save = detail["version"]
                # 4D Blocker B (component scope decision, Option 1):
                # component_ref gets real reuse-impact reporting -- it is
                # genuinely block-level-precise, via the SAME get_
                # reuse_impact()/reuse_impact_summary() primitives table/
                # figure use -- but NEVER the decision buttons or an
                # unlocked editor: can_edit stays False unconditionally
                # (editable_text is always False for component_ref, since
                # it is deliberately excluded from EDITABLE_TEXT_BLOCK_
                # TYPES/DEDICATED_EDITOR_BLOCK_TYPES), and update_
                # everywhere_btn_visible/fork_local_btn_visible are never
                # set True in this branch. There is no editor for a
                # decision to unlock, so no decision is offered -- shown
                # instead as a plain, explicit "not yet editable" notice
                # plus the real usage count, never silence about either.
                if block_type == "component_ref" and detail.get("object_ref_id"):
                    object_ref_id = detail["object_ref_id"]
                    cimpact = svc.get_reuse_impact("component", object_ref_id)
                    notice = "ℹ️ Componente riusabile — non ancora editabile in questo Studio."
                    if cimpact["is_shared"]:
                        cris = av.reuse_impact_summary(cimpact)
                        notice += f" 🔗 Utilizzato in {cimpact['reference_count']} punti."
                        lines = [cris["usage_line"]] + cris["topic_lines"] + cris["notes"]
                        reuse_impact_upd = gr.update(value="  \n".join(x for x in lines if x), visible=True)
                    readonly_notice = gr.update(value=notice, visible=True)
                else:
                    readonly_notice = gr.update(value=("" if editable_text else f"ℹ️ {detail['type_label']}"), visible=not editable_text)
                can_edit = editable_text and not detail["archived"]
                history = pd.DataFrame(av.block_history_rows(svc, object_id))
                tech_html = _studio_tech_html_block(detail)

        status_html = _studio_status_html(status_detail)
        can_move = not detail["archived"]
        can_archive = not detail["archived"]
        can_restore = detail["archived"]
        can_submit_review = bool(detail.get("can_submit_for_review"))

        return (
            gr.update(value=table), gr.update(choices=picker_choices, value=picker_value),
            kind, object_id, version_for_save,
            breadcrumb, status_html, title_upd, text_upd, readonly_notice,
            gr.update(visible=True, interactive=can_edit), gr.update(visible=True, interactive=can_edit),
            gr.update(visible=status_detail["has_draft"]), gr.update(visible=status_detail["has_draft"]),
            gr.update(visible=True, interactive=can_move), gr.update(visible=True, interactive=can_move),
            gr.update(visible=True, interactive=can_archive), gr.update(visible=True, interactive=can_restore),
            gr.update(visible=submit_review_visible, interactive=can_submit_review), review_msg,
            gr.update(visible=new_block_row_visible),
            tech_html, history, save_msg,
            pub_header,
            block_type, object_ref_id,
            table_title_upd, table_caption_upd, table_grid_view_upd, table_grid_edit_upd,
            figure_preview_upd, figure_caption_upd, figure_alt_upd, gr.update(visible=figure_replace_row_visible),
            shared_notice_upd, gr.update(visible=new_table_row_visible), gr.update(visible=new_figure_row_visible),
            gr.update(visible=table_rowcol_row_visible),
            shared_confirmed, shared_impact_token, reuse_impact_upd,
            gr.update(visible=update_everywhere_btn_visible, interactive=update_everywhere_btn_visible),
            gr.update(visible=fork_local_btn_visible, interactive=fork_local_btn_visible),
            gr.update(visible=True), diff_output,
        )
    finally:
        repo.close()

ST_IDX_TABLE, ST_IDX_PICKER, ST_IDX_KIND, ST_IDX_ID, ST_IDX_VERSION, ST_IDX_BREADCRUMB, ST_IDX_STATUS, \
    ST_IDX_TITLE, ST_IDX_TEXT, ST_IDX_READONLY, ST_IDX_SAVE_BTN, ST_IDX_DISCARD_EDITS_BTN, ST_IDX_RESTORE_DRAFT_BTN, \
    ST_IDX_DISCARD_DRAFT_BTN, ST_IDX_MOVE_UP_BTN, ST_IDX_MOVE_DOWN_BTN, ST_IDX_ARCHIVE_BTN, ST_IDX_RESTORE_BTN, \
    ST_IDX_SUBMIT_REVIEW_BTN, ST_IDX_REVIEW_MSG, ST_IDX_NEW_BLOCK_ROW, ST_IDX_TECH, ST_IDX_HISTORY, ST_IDX_SAVE_MSG, \
    ST_IDX_PUB_HEADER, \
    ST_IDX_BLOCK_TYPE, ST_IDX_OBJECT_REF_ID, \
    ST_IDX_TABLE_TITLE, ST_IDX_TABLE_CAPTION, ST_IDX_TABLE_GRID_VIEW, ST_IDX_TABLE_GRID_EDIT, \
    ST_IDX_FIGURE_PREVIEW, ST_IDX_FIGURE_CAPTION, ST_IDX_FIGURE_ALT, ST_IDX_FIGURE_REPLACE_ROW, \
    ST_IDX_SHARED_NOTICE, ST_IDX_NEW_TABLE_ROW, ST_IDX_NEW_FIGURE_ROW, \
    ST_IDX_TABLE_ROWCOL_ROW, \
    ST_IDX_SHARED_CONFIRMED, ST_IDX_SHARED_IMPACT_TOKEN, ST_IDX_REUSE_IMPACT, \
    ST_IDX_UPDATE_EVERYWHERE_BTN, ST_IDX_FORK_LOCAL_BTN, \
    ST_IDX_COMPARE_ROW, ST_IDX_DIFF_OUTPUT = range(46)

def _studio_noop_bundle(*, save_msg: str = "", review_msg: str = "") -> tuple:
    # Standard-mode validation UX fix: a predictable, user-correctable input
    # gap (missing actor name, missing title, nothing selected for an action
    # that needs a selection) is guidance, not a system error -- it must
    # never raise gr.Error (which pops a global red toast and, on this
    # multi-output panel, would otherwise force a full _studio_full_bundle
    # re-render from the DB, silently discarding whatever the user had just
    # typed in a title/text/caption/grid field). gr.update() with no args,
    # returned for every one of the 46 outputs, touches nothing: verified
    # against Gradio 6.5.1's own dispatch code (blocks.py) that a State
    # output (st_kind/st_id/st_version/st_block_type/st_object_ref_id/
    # st_shared_confirmed/st_shared_impact_token) receiving an update-dict
    # with no "value" key is left at its previous value rather than being
    # overwritten.
    out = [gr.update() for _ in range(46)]
    out[ST_IDX_SAVE_MSG] = save_msg
    out[ST_IDX_REVIEW_MSG] = review_msg
    return tuple(out)


def studio_load_ui(project_dir, publication_id, show_archived):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    return _studio_full_bundle(project_dir, publication_id, None, None, show_archived)

def studio_open_ui(project_dir, publication_id, picker_value, show_archived):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    kind, object_id = av.parse_open_value(picker_value)
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)

def studio_refresh_ui(project_dir, publication_id, kind, object_id, show_archived):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)

def _studio_draft_target(kind, object_id, block_type, object_ref_id):
    # 4C: a table/figure's editable CONTENT lives on the table_id/figure_id
    # row, with its OWN version counter -- distinct from the wrapping
    # content_blocks row's own version/position. Drafts (and saves) for a
    # table/figure block must key off the table_id/figure_id
    # (object_ref_id), never the block_id -- otherwise a draft saved here
    # could never be found by load_table_for_edit/load_figure_for_edit,
    # which look drafts up by (kind, table_id/figure_id).
    if kind == "topic":
        return "topic", object_id
    if block_type == "table":
        return "table", object_ref_id
    if block_type == "figure":
        return "figure", object_ref_id
    return "block", object_id

def _grid_to_columns_rows(df):
    if df is None:
        return [], []
    try:
        cols = [str(c) for c in df.columns.tolist()]
    except Exception:
        return [], []
    rows = df.fillna("").astype(str).values.tolist()
    return cols, rows

def _fixed_grid_update(df, **kwargs):
    # 4C integrity gate: Gradio 6.5.1's Dataframe has a real data-loss bug
    # (see _table_rowcol_op) -- its own built-in right-click "Add row" /
    # "Add column" / "Delete row" / "Delete column" context menu mutates
    # the grid client-side without reliably updating the value Save reads.
    # The new `row_limits`/`column_limits` kwargs that would be the
    # "correct" way to cap this are explicitly unimplemented in this
    # Gradio version (components/dataframe.py: "not yet implemented",
    # warns and is otherwise a no-op). The OLD `row_count`/`column_count`
    # tuple form ("fixed" mode) is still what the shipped frontend bundle
    # actually honors -- empirically verified (isolated repro app): with
    # row_count=(n,"fixed") + column_count=(m,"fixed") the right-click
    # menu renders with ZERO structural items (no Add/Delete row/column
    # at all), cell editing keeps working normally, and a SEPARATE
    # Dataframe elsewhere in the same app with no row_count/column_count
    # override keeps its full menu -- this is a per-component prop, not a
    # global behavior change. Recomputed on every push (open/add-row/
    # remove-row/restore-draft/...) from the row/column counts actually
    # being pushed, so "fixed" always tracks the real current shape
    # instead of freezing the table at whatever size it happened to be
    # when first opened.
    n_rows = max(1, len(df.index)) if df is not None else 1
    n_cols = max(1, len(df.columns)) if df is not None else 1
    return gr.update(value=df, row_count=(n_rows, "fixed"), column_count=(n_cols, "fixed"), **kwargs)

def _studio_draft_payload(draft_kind, title_val, text_val, table_title_val, table_caption_val, table_grid_val,
                          figure_caption_val, figure_alt_val):
    if draft_kind == "topic":
        return {"title": title_val}
    if draft_kind == "table":
        cols, rows = _grid_to_columns_rows(table_grid_val)
        return {"title": table_title_val, "caption": table_caption_val, "columns": cols, "rows": rows}
    if draft_kind == "figure":
        return {"caption": figure_caption_val, "alt_text": figure_alt_val}
    return {"text": text_val}

def studio_save_draft_ui(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                         title_val, text_val, table_title_val, table_caption_val, table_grid_val,
                         figure_caption_val, figure_alt_val, actor):
    # Blur/change-triggered recovery save -- NEVER calls save_topic/
    # save_block/save_table/save_figure, so this can never create a CCMS
    # content version (4B.1 invariant, unchanged for 4C).
    if not project_dir or not kind or not object_id or expected_version is None:
        return ""
    draft_kind, draft_id = _studio_draft_target(kind, object_id, block_type, object_ref_id)
    if not draft_id:
        return ""
    repo, svc = av.open_service(project_dir)
    try:
        svc.save_draft(draft_kind, draft_id, base_version=int(expected_version),
                       payload=_studio_draft_payload(draft_kind, title_val, text_val, table_title_val,
                                                     table_caption_val, table_grid_val, figure_caption_val, figure_alt_val),
                       actor=(actor or "").strip() or "Autore sconosciuto")
        return "💾 Bozza salvata in locale (nessuna nuova versione creata)."
    except Exception:
        return ""
    finally:
        repo.close()

def _table_rowcol_op(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                     table_title_val, table_caption_val, table_grid_val, actor, op):
    # Root-cause fix for a real Gradio 6.5.1 bug found in 4C browser
    # stabilization: the Dataframe's built-in right-click "Add row"/"Add
    # column" context menu visibly mutates the grid but does not reliably
    # update the value Gradio submits as an input on a later event -- a
    # clean, single, DB-verified repro against this exact table showed
    # Save persisting only the pre-existing rows after a context-menu
    # "Add row below". These functions replace it: they read the CURRENT
    # submitted grid (which genuine cell edits/.input() DO update
    # correctly -- only the native row/column insertion is unreliable),
    # compute the new grid in Python, and push it back via
    # gr.update(value=...), so the very next Save reads the correct value.
    cols, rows = _grid_to_columns_rows(table_grid_val)
    if op == "add_row":
        if not cols:
            return gr.update(), "Aggiungi almeno una colonna."
        rows = rows + [["" for _ in cols]]
    elif op == "remove_row":
        if len(rows) <= 1:
            return gr.update(), "La tabella deve avere almeno una riga."
        rows = rows[:-1]
    elif op == "add_col":
        n = len(cols) + 1
        new_name = f"Colonna {n}"
        while new_name in cols:
            n += 1
            new_name = f"Colonna {n}"
        cols = cols + [new_name]
        rows = [list(r) + [""] for r in rows] if rows else [[""]]
    else:  # remove_col
        if len(cols) <= 1:
            return gr.update(), "Aggiungi almeno una colonna."
        cols = cols[:-1]
        rows = [r[:-1] for r in rows]
    new_df = pd.DataFrame(rows, columns=[str(c) for c in cols])
    msg = ""
    if project_dir and kind and object_id and expected_version is not None:
        draft_kind, draft_id = _studio_draft_target(kind, object_id, block_type, object_ref_id)
        if draft_id:
            repo, svc = av.open_service(project_dir)
            try:
                svc.save_draft(draft_kind, draft_id, base_version=int(expected_version),
                               payload=_studio_draft_payload(draft_kind, "", "", table_title_val,
                                                             table_caption_val, new_df, "", ""),
                               actor=(actor or "").strip() or "Autore sconosciuto")
                msg = "💾 Bozza salvata in locale (nessuna nuova versione creata)."
            except Exception:
                msg = ""
            finally:
                repo.close()
    return _fixed_grid_update(new_df), msg

def studio_table_add_row_ui(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                            table_title_val, table_caption_val, table_grid_val, actor):
    return _table_rowcol_op(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                            table_title_val, table_caption_val, table_grid_val, actor, "add_row")

def studio_table_remove_row_ui(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                               table_title_val, table_caption_val, table_grid_val, actor):
    return _table_rowcol_op(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                            table_title_val, table_caption_val, table_grid_val, actor, "remove_row")

def studio_table_add_col_ui(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                            table_title_val, table_caption_val, table_grid_val, actor):
    return _table_rowcol_op(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                            table_title_val, table_caption_val, table_grid_val, actor, "add_col")

def studio_table_remove_col_ui(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                               table_title_val, table_caption_val, table_grid_val, actor):
    return _table_rowcol_op(project_dir, kind, object_id, block_type, object_ref_id, expected_version,
                            table_title_val, table_caption_val, table_grid_val, actor, "remove_col")

def studio_restore_draft_ui(project_dir, kind, object_id, block_type, object_ref_id):
    # Outputs: [st_title_input, st_text_input, st_table_title, st_table_caption,
    #           st_table_grid_edit, st_figure_caption, st_figure_alt,
    #           st_restore_draft_btn, st_discard_draft_btn]
    draft_kind, draft_id = _studio_draft_target(kind, object_id, block_type, object_ref_id)
    hide2 = (gr.update(visible=False), gr.update(visible=False))
    noop7 = (gr.update(),) * 7
    if not draft_id:
        return noop7 + hide2
    repo, svc = av.open_service(project_dir)
    try:
        draft = svc.load_draft(draft_kind, draft_id)
    finally:
        repo.close()
    if not draft:
        return noop7 + hide2
    payload = draft["payload"]
    if draft_kind == "topic":
        return (gr.update(value=payload.get("title", "")), gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update()) + hide2
    if draft_kind == "table":
        cols = payload.get("columns") or ["Colonna 1"]
        rows = payload.get("rows") or [["" for _ in cols]]
        grid_df = pd.DataFrame(rows, columns=[str(c) for c in cols])
        return (gr.update(), gr.update(), gr.update(value=payload.get("title", "")),
                gr.update(value=payload.get("caption", "")), _fixed_grid_update(grid_df),
                gr.update(), gr.update()) + hide2
    if draft_kind == "figure":
        return (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(value=payload.get("caption", "")), gr.update(value=payload.get("alt_text", ""))) + hide2
    return (gr.update(), gr.update(value=payload.get("text", "")), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update()) + hide2

def studio_discard_draft_ui(project_dir, publication_id, kind, object_id, block_type, object_ref_id, show_archived):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    draft_kind, draft_id = _studio_draft_target(kind, object_id, block_type, object_ref_id)
    repo, svc = av.open_service(project_dir)
    try:
        if draft_id:
            svc.discard_draft(draft_kind, draft_id)
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg="Bozza scartata.")

def studio_discard_edits_ui(project_dir, publication_id, kind, object_id, show_archived):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)

def studio_save_ui(project_dir, publication_id, kind, object_id, block_type, object_ref_id, expected_version,
                   title_val, text_val, table_title_val, table_caption_val, table_grid_val,
                   figure_caption_val, figure_alt_val, actor, show_archived, shared_confirmed=False,
                   shared_impact_token=None):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    repo, svc = av.open_service(project_dir)
    try:
        if kind == "topic":
            result = svc.save_topic(object_id, title=title_val, expected_version=int(expected_version), actor=actor)
        elif block_type == "table":
            cols, rows = _grid_to_columns_rows(table_grid_val)
            if not cols:
                repo.close()
                return _studio_noop_bundle(save_msg="Aggiungi almeno una colonna.")
            result = svc.save_table(object_ref_id, title=table_title_val, caption=table_caption_val,
                                    columns=cols, rows=rows, expected_version=int(expected_version), actor=actor,
                                    confirm_shared=bool(shared_confirmed), expected_impact_token=shared_impact_token)
        elif block_type == "figure":
            result = svc.save_figure(object_ref_id, caption=figure_caption_val, alt_text=figure_alt_val,
                                     expected_version=int(expected_version), actor=actor,
                                     confirm_shared=bool(shared_confirmed), expected_impact_token=shared_impact_token)
        else:
            result = svc.save_block(object_id, text=text_val, expected_version=int(expected_version), actor=actor)
        msg = f"✅ Salvato — versione {result['version']}."
        if result.get("previous_status") in ("Approved", "In Review"):
            msg += " Era Approvato/In revisione: è tornato a Bozza."
            if result.get("review_tasks_invalidated"):
                msg += " La review aperta precedente è stata invalidata."
    except VersionConflictError:
        repo.close()
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {av.conflict_message()}")
    except (ArchivedObjectError, SharedObjectError) as e:
        repo.close()
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    except (KeyError, ValueError) as e:
        repo.close()
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=msg)

def studio_move_ui(project_dir, publication_id, kind, object_id, direction, show_archived, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    repo, svc = av.open_service(project_dir)
    try:
        if kind == "topic":
            rows = av.outline_rows(repo, publication_id, include_archived=False)
            ids = av.topic_ids_in_order(rows)
            if object_id not in ids:
                return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg="⛔ Elemento non più attivo.")
            i = ids.index(object_id)
            j = i - 1 if direction == "up" else i + 1
            if j < 0 or j >= len(ids):
                return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)
            new_order = list(ids); new_order[i], new_order[j] = new_order[j], new_order[i]
            svc.reorder_publication_topics(publication_id, new_order, expected_topic_ids=ids, actor=actor)
        elif kind == "block":
            detail = av.block_detail(svc, object_id)
            topic_id = detail["topic_id"]
            rows = av.outline_rows(repo, publication_id, include_archived=False)
            ids = av.block_ids_in_order(rows, topic_id)
            if object_id not in ids:
                return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg="⛔ Elemento non più attivo.")
            i = ids.index(object_id)
            j = i - 1 if direction == "up" else i + 1
            if j < 0 or j >= len(ids):
                return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)
            new_order = list(ids); new_order[i], new_order[j] = new_order[j], new_order[i]
            svc.reorder_blocks(topic_id, new_order, expected_block_ids=ids, actor=actor)
    except (KeyError, ValueError, VersionConflictError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)

def studio_archive_ui(project_dir, publication_id, kind, object_id, show_archived, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    repo, svc = av.open_service(project_dir)
    try:
        if kind == "topic": svc.archive_topic(object_id, actor=actor)
        else: svc.archive_block(object_id, actor=actor)
    except (KeyError, ValueError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg="✅ Archiviato.")

def studio_restore_obj_ui(project_dir, publication_id, kind, object_id, show_archived, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    repo, svc = av.open_service(project_dir)
    try:
        if kind == "topic": svc.restore_topic(object_id, actor=actor)
        else: svc.restore_block(object_id, actor=actor)
    except (KeyError, ValueError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg="✅ Ripristinato.")

def studio_new_topic_ui(project_dir, publication_id, new_title, kind, object_id, show_archived, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    if not str(new_title or "").strip(): return _studio_noop_bundle(save_msg="Inserisci un titolo per il nuovo topic.")
    repo, svc = av.open_service(project_dir)
    try:
        after_topic_id = object_id if kind == "topic" else (av.block_detail(svc, object_id)["topic_id"] if kind == "block" else None)
        result = svc.create_topic_in_publication(publication_id, new_title, actor=actor, after_topic_id=after_topic_id)
    except (KeyError, ValueError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, "topic", result["topic_id"], show_archived, save_msg="✅ Topic creato.")

def studio_new_block_ui(project_dir, publication_id, kind, object_id, block_type, show_archived, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if kind != "topic" or not object_id: return _studio_noop_bundle(save_msg="Apri prima un topic a cui aggiungere il blocco.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    repo, svc = av.open_service(project_dir)
    try:
        result = svc.create_block_in_topic(object_id, block_type, actor=actor, text="")
    except (KeyError, ValueError, ArchivedObjectError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, "block", result["block_id"], show_archived, save_msg="✅ Blocco aggiunto.")

def studio_new_table_ui(project_dir, publication_id, kind, object_id, caption, show_archived, actor):
    # Deliberately asks for nothing but an OPTIONAL caption -- "se il titolo
    # non è obbligatorio backend, non renderlo obbligatorio arbitrariamente"
    # (create_table_in_topic() itself requires no title/caption). Columns/
    # rows start at a small, sensible default (2 columns, 1 empty row) the
    # user immediately edits via the grid itself once it opens -- no
    # separate "define your columns" step, matching how "+ Nuovo topic"
    # only ever asks for a title and nothing else.
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if kind != "topic" or not object_id: return _studio_noop_bundle(save_msg="Apri prima un topic a cui aggiungere la tabella.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    repo, svc = av.open_service(project_dir)
    try:
        result = svc.create_table_in_topic(object_id, actor=actor, caption=(caption or "").strip(),
                                           columns=["Colonna 1", "Colonna 2"], rows=[["", ""]])
    except (KeyError, ValueError, ArchivedObjectError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, "block", result["block_id"], show_archived, save_msg="✅ Tabella creata.")

def studio_new_figure_ui(project_dir, publication_id, kind, object_id, image_file, caption, alt_text, show_archived, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if kind != "topic" or not object_id: return _studio_noop_bundle(save_msg="Apri prima un topic a cui aggiungere la figura.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    if not image_file: return _studio_noop_bundle(save_msg="Seleziona un file immagine.")
    repo, svc = av.open_service(project_dir)
    try:
        result = svc.create_figure_in_topic(object_id, actor=actor, source_file_path=image_file,
                                            caption=(caption or "").strip(), alt_text=(alt_text or "").strip())
    except (KeyError, ValueError, ArchivedObjectError, FileNotFoundError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, "block", result["block_id"], show_archived, save_msg="✅ Figura aggiunta.")

def studio_replace_figure_ui(project_dir, publication_id, kind, object_id, block_type, object_ref_id,
                             expected_version, image_file, show_archived, actor, shared_confirmed=False,
                             shared_impact_token=None):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if kind != "block" or block_type != "figure" or not object_ref_id:
        return _studio_noop_bundle(save_msg="Apri prima una figura per sostituire l'immagine.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    if not image_file: return _studio_noop_bundle(save_msg="Seleziona un file immagine.")
    repo, svc = av.open_service(project_dir)
    try:
        result = svc.replace_figure_image(object_ref_id, source_file_path=image_file,
                                          expected_version=int(expected_version), actor=actor,
                                          confirm_shared=bool(shared_confirmed), expected_impact_token=shared_impact_token)
        msg = f"✅ Immagine sostituita — versione {result['version']}."
    except VersionConflictError:
        repo.close()
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {av.conflict_message()}")
    except (KeyError, ValueError, ArchivedObjectError, SharedObjectError, FileNotFoundError) as e:
        repo.close()
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=msg)

def studio_submit_review_ui(project_dir, publication_id, kind, object_id, show_archived, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(review_msg="Seleziona prima un elemento.")
    if not str(actor or "").strip(): return _studio_noop_bundle(review_msg="Inserisci il tuo nome per continuare.")
    repo, svc = av.open_service(project_dir)
    try:
        rid = repo.request_review(kind, object_id, requested_by=actor)
        review_msg = f"✅ Inviato in revisione — Review #{rid}."
    except (KeyError, ValueError) as e:
        repo.close()
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, review_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, review_msg=review_msg)

def studio_revert_ui(project_dir, publication_id, kind, object_id, block_type, object_ref_id, version, show_archived,
                     actor, shared_confirmed=False, shared_impact_token=None):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    if version is None: return _studio_noop_bundle(save_msg="Seleziona la versione da ripristinare.")
    repo, svc = av.open_service(project_dir)
    try:
        if kind == "topic": svc.revert_topic_to_version(object_id, int(version), actor=actor)
        elif block_type == "table":
            svc.revert_table_to_version(object_ref_id, int(version), actor=actor, confirm_shared=bool(shared_confirmed),
                                        expected_impact_token=shared_impact_token)
        elif block_type == "figure":
            svc.revert_figure_to_version(object_ref_id, int(version), actor=actor, confirm_shared=bool(shared_confirmed),
                                         expected_impact_token=shared_impact_token)
        else: svc.revert_block_to_version(object_id, int(version), actor=actor)
    except (KeyError, ValueError, ArchivedObjectError, SharedObjectError, MissingAssetError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg="✅ Versione ripristinata (creata una nuova versione corrente).")

def studio_confirm_shared_update_ui(project_dir, publication_id, kind, object_id, show_archived):
    # 4D "Aggiorna ovunque": records the user's explicit choice (as a
    # transient UI State, never persisted) to edit a shared table/figure in
    # place. Does NOT save anything by itself -- it only unlocks the
    # editor fields (can_edit becomes True in _studio_full_bundle) so the
    # normal Save button, now passing confirm_shared=True, performs the
    # actual audited override of the fail-closed shared-object guard.
    #
    # 4D integrity gate: this is also the ONLY moment the impact snapshot
    # token is captured -- right here, freshly re-read from the live DB,
    # at the exact instant the user is shown (and implicitly confirms) the
    # current reference set. Save/revert re-verify this token against a
    # freshly re-read impact at write time (see _guard_table_or_figure_
    # editable): if anything in that set changed in between -- one more
    # block referencing the object, or one fewer -- the token no longer
    # matches and the write fails closed, even though expected_version
    # alone might still look unchanged.
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    repo, svc = av.open_service(project_dir)
    try:
        detail = av.block_detail(svc, object_id) if kind == "block" else None
        token = None
        if detail is not None and detail["block_type"] in ("table", "figure"):
            token = svc.get_reuse_impact(detail["block_type"], detail["object_ref_id"])["impact_token"]
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived,
                               shared_confirmed=True, shared_impact_token=token)

def studio_fork_local_ui(project_dir, publication_id, kind, object_id, block_type, object_ref_id, version,
                         show_archived, actor):
    # 4D "Crea una copia locale": unlike "Aggiorna ovunque", this is not a
    # two-step unlock -- the fork itself is the entire action, and it
    # completes immediately (AuthoringService.fork_table_local/
    # fork_figure_local is a single atomic transaction). After it returns,
    # re-opening the SAME block (object_id is the wrapping content_blocks
    # id, unchanged by a fork) picks up its new, now-independent
    # object_ref_id automatically -- no special-case re-render needed.
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not kind or not object_id: return _studio_noop_bundle(save_msg="Seleziona prima un elemento.")
    if not str(actor or "").strip(): return _studio_noop_bundle(save_msg="Inserisci il tuo nome per continuare.")
    if block_type not in ("table", "figure") or not object_ref_id or version is None:
        return _studio_noop_bundle(save_msg="Apri prima una tabella o una figura condivisa.")
    repo, svc = av.open_service(project_dir)
    try:
        if block_type == "table":
            svc.fork_table_local(object_ref_id, block_id=object_id, expected_version=int(version), actor=actor)
        else:
            svc.fork_figure_local(object_ref_id, block_id=object_id, expected_version=int(version), actor=actor)
    except (KeyError, ValueError, ArchivedObjectError, VersionConflictError) as e:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {e}")
    finally:
        repo.close()
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived,
                               save_msg="✅ Copia locale creata — questo blocco ora usa una versione indipendente, "
                                        "gli altri riferimenti restano invariati.")

def _render_diff_text_markdown(diff: dict) -> str:
    parts = []
    for seg in diff.get("segments", []):
        t = seg.get("text", "")
        if not t:
            continue
        if seg["tag"] == "insert":
            parts.append(f"**{t}**")
        elif seg["tag"] == "delete":
            parts.append(f"~~{t}~~")
        else:
            parts.append(t)
    body = " ".join(parts)
    return f"{diff['summary']}\n\n{body}" if body else diff["summary"]

def studio_compare_ui(project_dir, kind, object_id, block_type, object_ref_id, version, compare_version):
    # 4D revision compare: pure read, never mutates anything -- diffs the
    # CURRENT live content against one historical version, using
    # AuthoringService's new get_*_version() accessors and authoring_view's
    # diff_text/diff_table/diff_figure. This same function backs both
    # "Confronta con versione" (explicit version number) and "Confronta con
    # versione precedente" (studio_compare_prev_ui below, which just
    # supplies version-1) -- one comparison primitive, not two.
    if not project_dir or not kind or not object_id:
        return "Seleziona prima un elemento."
    if compare_version is None:
        return "Inserisci la versione con cui confrontare."
    repo, svc = av.open_service(project_dir)
    try:
        cv = int(compare_version)
        if cv < 1:
            return "⛔ La versione deve essere almeno 1."
        if kind == "topic":
            old = svc.get_topic_version(object_id, cv)
            live = svc.load_topic_for_edit(object_id)
            d = av.diff_text(old["body"], live["body"])
            title_note = "" if old["title"] == live["title"] else " (titolo modificato)"
            return f"**Confronto: versione {cv} → versione corrente ({live['version']}){title_note}**\n\n" + _render_diff_text_markdown(d)
        if block_type == "table":
            old = svc.get_table_version(object_ref_id, cv)
            live = svc.load_table_for_edit(object_ref_id)
            d = av.diff_table(old, live)
            return f"**Confronto: versione {cv} → versione corrente ({live['version']})**\n\n{d['summary']}"
        if block_type == "figure":
            old = svc.get_figure_version(object_ref_id, cv)
            live = svc.load_figure_for_edit(object_ref_id)
            d = av.diff_figure(old, live)
            return f"**Confronto: versione {cv} → versione corrente ({live['version']})**\n\n{d['summary']}"
        old = svc.get_block_version(object_id, cv)
        live = svc.load_block_for_edit(object_id)
        d = av.diff_text(old["text"], live["text"])
        return f"**Confronto: versione {cv} → versione corrente ({live['version']})**\n\n" + _render_diff_text_markdown(d)
    except KeyError:
        return f"⛔ Versione {compare_version} non trovata."
    except Exception as e:
        return f"⛔ {e}"
    finally:
        repo.close()

def studio_compare_prev_ui(project_dir, kind, object_id, block_type, object_ref_id, version):
    if version is None or not kind or not object_id:
        return "Seleziona prima un elemento."
    return studio_compare_ui(project_dir, kind, object_id, block_type, object_ref_id, version, int(version) - 1)

def studio_search_ui(project_dir, publication_id, query):
    if not project_dir:
        return gr.update(choices=[], value=None, visible=False), gr.update(visible=False), "Nessun progetto attivo."
    repo, svc = av.open_service(project_dir)
    try:
        results = av.search(repo, publication_id, query)
    finally:
        repo.close()
    if not results:
        msg = "Nessun risultato." if (query or "").strip() else ""
        return gr.update(choices=[], value=None, visible=False), gr.update(visible=False), msg
    choices = [(f"{r['label']}  ({r['context']})", f"{r['kind']}:{r['id']}") for r in results]
    return gr.update(choices=choices, value=None, visible=True), gr.update(visible=True), f"{len(results)} risultati."

def studio_search_open_ui(project_dir, publication_id, picked_value, show_archived):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not picked_value: return _studio_noop_bundle(save_msg="Seleziona prima un risultato da aprire.")
    kind, object_id = av.parse_open_value(picked_value)
    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)

def studio_next_prev_ui(project_dir, publication_id, kind, object_id, direction, show_archived):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if kind != "topic" or not object_id: return _studio_noop_bundle(save_msg="Apri prima un topic per navigare.")
    repo, svc = av.open_service(project_dir)
    try:
        rows = av.outline_rows(repo, publication_id, include_archived=False)
        prev_id, next_id = av.next_previous_topic(rows, object_id)
    finally:
        repo.close()
    target = prev_id if direction == "prev" else next_id
    if not target:
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived)
    return _studio_full_bundle(project_dir, publication_id, "topic", target, show_archived)

def _auto_project_legacy_tm(project_dir: str | Path) -> Path | None:
    """Build/reuse a project TM from the bilingual evidence already produced by Revision Analysis.

    This removes the product-UI requirement to ask a normal user for a SQLite TM file.
    For now automatic legacy-TM construction is enabled when the project has one target
    language, or when the analysis language can be matched unambiguously to one target.
    """
    project_dir=Path(project_dir)
    meta=read_project(project_dir)
    targets=[str(x).strip() for x in meta.get('target_langs',[]) if str(x).strip()]
    if not targets:
        return None
    analyses=sorted((project_dir/'Analyses').glob('**/*_dettaglio_testo.csv'), key=lambda p:p.stat().st_mtime, reverse=True) if (project_dir/'Analyses').exists() else []
    if not analyses:
        return None
    csv_path=analyses[0]
    target=''
    summaries=sorted(csv_path.parent.glob('*_sintesi.json'))
    if summaries:
        try:
            sm=json.loads(summaries[0].read_text(encoding='utf-8'))
            detected=str(sm.get('lingua_vecchia_traduzione') or '').strip().replace('_','-').casefold()
            base=detected.split('-')[0]
            hits=[x for x in targets if x.replace('_','-').casefold().split('-')[0]==base]
            if len(hits)==1: target=hits[0]
        except Exception:
            pass
    if not target and len(targets)==1:
        target=targets[0]
    if not target:
        return None
    tm_dir=project_dir/'TranslationMemory'; tm_dir.mkdir(parents=True,exist_ok=True)
    tm_path=tm_dir/'ManualSync_PROJECT_LEGACY_TM.sqlite'
    # Refresh only when the analysis is newer than the derived TM.
    if tm_path.exists() and tm_path.stat().st_mtime >= csv_path.stat().st_mtime:
        return tm_path
    tm_path.unlink(missing_ok=True)
    frame=pd.read_csv(csv_path,encoding='utf-8-sig').fillna('')
    tm=TranslationMemory(tm_path)
    added=0
    try:
        for _,r in frame.iterrows():
            src=str(r.get('vecchio_originale','')).strip(); tgt=str(r.get('vecchia_traduzione','')).strip()
            try: conf=float(r.get('affidabilita_traduzione',0) or 0)
            except Exception: conf=0
            if not src or not tgt or src.startswith('—') or conf < 80:
                continue
            safe,_=source_target_safe(src,tgt)
            if not safe:
                continue
            # Automatic pre-alignment is deliberately confidence-tiered:
            # >=90 can participate in exact automatic reuse; 80-89 remains an
            # approved legacy suggestion but carries a non-zero penalty, which
            # the CAT matcher always downgrades to mandatory human review.
            penalty = 0.0 if conf >= 90.0 else round(max(1.0, 90.0 - conf), 1)
            tm.add(str(meta.get('source_lang','en-US')),target,src,tgt,origin='ManualSync revision analysis',alignment_confidence=conf,approved=True,tm_name='Project Legacy',priority=100,penalty=penalty)
            added+=1
    finally:
        tm.close()
    _app_log(f'Project legacy TM refreshed target={target} rows={added} path={tm_path}')
    return tm_path

def work_package_ui(project_dir, object_id, kind, tm_files):
    if not project_dir or not object_id: raise gr.Error("Seleziona/indica un oggetto approvato.")
    tms=list(tm_files) if isinstance(tm_files,(list,tuple)) else ([tm_files] if tm_files else [])
    auto_tm=_auto_project_legacy_tm(project_dir)
    if auto_tm and str(auto_tm) not in [str(x) for x in tms]: tms.insert(0,str(auto_tm))
    target_langs=canonical_langs(read_project(project_dir).get('target_langs',[]) or [])
    out=Path(project_dir)/'TranslationPackages'/f"{kind}_{object_id}"; out.mkdir(parents=True,exist_ok=True)
    repo=IndustrialRepository(_ccms_db(project_dir))
    try: result=generate_translation_work_packages(repo,object_id.strip(),kind=kind,tm_paths=tms,target_langs=target_langs,require_approved=True,out_dir=out)
    finally: repo.close()
    tm_note=(f" TM di progetto usata automaticamente: **{Path(auto_tm).name}**." if auto_tm else " Nessuna TM legacy automatica disponibile: il pacchetto usa comunque le lingue target del progetto.")

    # Desktop safety net: do not depend only on the embedded webview download handler.
    # Every generated ZIP is also copied automatically into the user's Downloads folder.
    export_root = Path.home() / "Downloads" / "ManualSyncExports"
    try:
        export_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        export_root = DATA_HOME / "Exports"
        export_root.mkdir(parents=True, exist_ok=True)
    safe_object = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(object_id)) or "package"
    export_dir = export_root / f"{kind}_{safe_object}"
    export_dir.mkdir(parents=True, exist_ok=True)
    exported=[]
    try:
        master_src=Path(result['master_zip'])
        master_dst=export_dir/master_src.name
        shutil.copy2(master_src,master_dst)
        exported.append(master_dst)
        for z in result['language_zips']:
            src=Path(z); dst=export_dir/src.name
            shutil.copy2(src,dst)
            exported.append(dst)
    except Exception as e:
        _app_log("Automatic translation package export failed",e)

    export_note=(f" Pacchetti copiati anche automaticamente in **{export_dir}**." if exported else " I pacchetti restano disponibili nel progetto; copia automatica in Download non riuscita.")
    friendly=_friendly_work_packages(result['packages'])
    return f"✅ Creati **{len(result['language_zips'])} pacchetti lingua**.{tm_note}{export_note}", gr.update(value=friendly, visible=not friendly.empty), str(result['master_zip']), [str(x) for x in result['language_zips']]

def workflow_refresh_ui(project_dir):
    if not project_dir: raise gr.Error("Apri un progetto.")
    wf=WorkflowManager(project_dir)
    try:
        jobs, vendors, events = _friendly_jobs(wf.jobs_table()), _friendly_vendors(wf.vendors_table()), _friendly_events(wf.events_table())
        return gr.update(value=jobs, visible=not jobs.empty), gr.update(value=vendors, visible=not vendors.empty), gr.update(value=events, visible=not events.empty)
    finally: wf.close()

def vendor_ui(project_dir, vid, name, email):
    wf=WorkflowManager(project_dir)
    try: t=_friendly_vendors(wf.upsert_vendor(vid,name,email or ''))
    finally: wf.close()
    return gr.update(value=t, visible=not t.empty)

def create_job_ui(project_dir, package, vendor, due):
    if not package: raise gr.Error("Carica un pacchetto lingua.")
    if not project_dir: raise gr.Error("Apri un progetto.")
    wf=WorkflowManager(project_dir)
    try:
        try:
            jid=wf.create_job_from_package(package,vendor_id=(vendor or '').strip(),due_date=due or '',actor='ManualSync user')
            msg=f"✅ Job **{jid}** creato."
        except Exception as exc:
            # Keep operational validation visible in-page instead of a transient
            # Gradio toast that can be missed when the user is scrolled lower.
            msg=f"⛔ Job non creato: **{str(exc)}**"
        jobs=_friendly_jobs(wf.jobs_table())
        return msg, gr.update(value=jobs, visible=not jobs.empty)
    finally: wf.close()

_JOB_ACTIONS_BY_STATE = {
    'READY_TO_SEND': {'Segna inviato','Annulla'},
    'SENT': {'Segna in traduzione','Ricevi XLIFF + controllo qualità (QA)','Ricevi XLIFF + QA','Annulla'},
    'IN_TRANSLATION': {'Ricevi XLIFF + controllo qualità (QA)','Ricevi XLIFF + QA','Annulla'},
    'QA_BLOCKED': {'Riesegui controllo qualità (QA)','Riesegui QA','Annulla'},
    'QA_READY': {'Approva traduzione','Annulla'},
    'APPROVED': set(),
    'CANCELLED': set(),
}

def _job_state_hint(status):
    return {
        'READY_TO_SEND':'prossimo passo: **Segna inviato**',
        'SENT':'prossimo passo: **Segna in traduzione** oppure registra direttamente l’XLIFF restituito',
        'IN_TRANSLATION':'prossimo passo: **Ricevi XLIFF + controllo qualità (QA)**',
        'QA_BLOCKED':'prossimo passo: correggi l’XLIFF e usa **Riesegui controllo qualità (QA)**',
        'QA_READY':'prossimo passo: revisore + conferma umana + **Approva traduzione**',
        'APPROVED':'job completato',
        'CANCELLED':'job annullato',
    }.get(str(status or ''), 'verifica lo stato del job')

def job_action_ui(project_dir, job_id, action, returned_xliff, terminology, strict, reviewer, human_approved):
    if not project_dir or not job_id: raise gr.Error("Project e Job ID sono obbligatori.")
    wf=WorkflowManager(project_dir)
    qa_table=pd.DataFrame(); approval=None; tm=None
    try:
        current=wf.get_job(job_id)
        allowed=_JOB_ACTIONS_BY_STATE.get(str(current.get('status') or ''), set())
        if action not in allowed:
            jobs, events = _friendly_jobs(wf.jobs_table()), _friendly_events(wf.events_table(job_id))
            label=str(current.get('status_label') or current.get('status') or '')
            msg=(f"ℹ️ Azione **{action or 'non selezionata'}** non applicabile allo stato **{label}**; "
                 f"{_job_state_hint(current.get('status'))}.")
            return msg,gr.update(value=jobs, visible=not jobs.empty),gr.update(value=events, visible=not events.empty),gr.update(value=qa_table, visible=False),approval,tm
        if action=='Segna inviato':
            wf.mark_sent(job_id,actor='ManualSync user'); msg='✅ Job segnato come inviato.'
        elif action=='Segna in traduzione':
            wf.mark_in_translation(job_id,actor='ManualSync user'); msg='✅ Job in traduzione.'
        elif action in ('Ricevi XLIFF + QA','Ricevi XLIFF + controllo qualità (QA)'):
            if not returned_xliff: raise ValueError('Carica l’XLIFF restituito.')
            wf.register_returned_xliff(job_id,returned_xliff,actor='ManualSync user')
            q=wf.run_roundtrip_qa(job_id,_ccms_db(project_dir),terminology_path=terminology or None,strict_terminology=bool(strict),actor='ManualSync user')
            qa_table=q['qa']['audit']; msg='✅ Controllo qualità (QA) superato: pronto per approvazione.' if q['job']['status']=='QA_READY' else '⛔ Controllo qualità (QA) bloccato: correggere la traduzione.'
        elif action in ('Riesegui QA','Riesegui controllo qualità (QA)'):
            q=wf.rerun_roundtrip_qa(
                job_id,_ccms_db(project_dir),returned_xliff=returned_xliff or None,
                terminology_path=terminology or None,strict_terminology=bool(strict),actor='ManualSync user'
            )
            qa_table=q['qa']['audit']; msg='✅ Controllo qualità (QA) superato: correzione accettata, pronto per approvazione.' if q['job']['status']=='QA_READY' else '⛔ Controllo qualità (QA) ancora bloccato: restano errori da correggere.'
        elif action=='Approva traduzione':
            r=wf.approve_job(job_id,_ccms_db(project_dir),reviewer=reviewer or 'Human reviewer',human_approved=bool(human_approved))
            approval=str(r['commit']['approval_zip']); tm=str(r['commit']['tm_path']); msg='✅ Traduzione approvata: TM/CCMS aggiornati.'
        elif action=='Annulla':
            wf.transition(job_id,'CANCELLED',actor='ManualSync user'); msg='Job annullato.'
        else: raise ValueError('Seleziona un’azione.')
        qa_update = gr.update(value=qa_table, visible=not qa_table.empty)
        jobs, events = _friendly_jobs(wf.jobs_table()), _friendly_events(wf.events_table(job_id))
        return msg,gr.update(value=jobs, visible=not jobs.empty),gr.update(value=events, visible=not events.empty),qa_update,approval,tm
    except Exception as e: raise gr.Error(str(e))
    finally: wf.close()

def _compliance_readiness_html(compliance: dict | None) -> str:
    """3.5C: minimal Release-tab integration for the Compliance gate --
    read-only formatting of the SAME canonical dict release_readiness.py /
    the release snapshot use, never a second Compliance dashboard. No
    active scope -> a neutral note, never a false green checkmark."""
    if not compliance or not compliance.get("compliance_applicable"):
        return "<div class='ms-muted' style='margin-top:10px;font-size:12px'>Compliance non configurata per questa publication.</div>"
    c = compliance["counts"]
    status = ("<span class='ms-ok'>Compliance: pronta</span>" if compliance["compliance_ready"]
              else "<span class='ms-block'>Compliance: richiede intervento</span>")
    counts_line = (f"Coperti {c['Covered']} · Da verificare {c['To verify']} · Mancanti {c['Missing']} · "
                   f"Non applicabili {c['Not applicable']} ({compliance['active_scope_count']} scope attivi)")
    items = compliance.get("blocking_items") or []
    blockers_html = ""
    if items:
        rows = "".join(
            f"<li>{html.escape(b['scope_id'])} / {html.escape(b['requirement_id'])} — "
            f"{html.escape(b['title'])} ({html.escape(b['status'])})</li>" for b in items[:20])
        more = f"<li class='ms-muted'>… e altri {len(items) - 20}</li>" if len(items) > 20 else ""
        blockers_html = f"<ul style='margin:6px 0 0 18px;padding:0'>{rows}{more}</ul>"
    return (
        f"<div style='margin-top:10px'><b>{status}</b><br>"
        f"<span class='ms-muted'>{counts_line}</span>{blockers_html}"
        "<div class='ms-muted' style='margin-top:6px;font-size:12px'>Stato rispetto agli scope Compliance "
        "configurati: non è una dichiarazione di conformità legale o certificazione.</div></div>"
    )

def readiness_ui(project_dir, publication_id, target_langs, profile_id, release_mode_label="Collaudo interno"):
    require_linguistic = not str(release_mode_label or "").startswith("Collaudo")
    langs=canonical_langs(target_langs)
    if not langs: langs=canonical_langs(read_project(project_dir).get('target_langs',[]))
    r=assess_release_readiness(project_dir,publication_id,target_langs=langs,profile_id=(profile_id or '').strip() or None,require_linguistic_plausibility=require_linguistic)
    out=Path(project_dir)/'ReleaseReadiness'; files=export_readiness_report(r,out)
    msg="<span class='ms-ok'>✅ RELEASE PRONTA</span>" if r['release_ready'] else "<span class='ms-block'>⛔ RELEASE BLOCCATA</span>"
    sm=r['summary']
    source='pronta' if sm.get('source_ready') else 'bloccata'
    detail=f"Lingua sorgente: <b>{source}</b> · Lingue target pronte: <b>{sm.get('target_languages_ready',0)}/{sm.get('target_languages_total',0)}</b>."
    if not r['release_ready'] and not r['blocking_items'].empty:
        detail += f" · Elementi traduzione da risolvere: <b>{len(r['blocking_items'])}</b>."
    compliance_html = _compliance_readiness_html(r.get('compliance'))
    return (
        f"<div class='ms-card'><h3>{msg}</h3><p>{detail}</p>{compliance_html}</div>",
        gr.update(value=_friendly_readiness_matrix(r['matrix']), visible=True),
        gr.update(value=r['blocking_items'], visible=not r['blocking_items'].empty),
        str(files['json']),
        gr.update(interactive=bool(r['release_ready']))
    )

def _release_mode_help(release_mode_label):
    mode='test' if str(release_mode_label or '').startswith('Collaudo') else 'production'
    if mode == 'test':
        return "<div class='ms-card'><b>Collaudo interno:</b> crea un pacchetto TEST non consegnabile. I gate strutturali restano attivi, ma i marker ManualSync di collaudo sono ammessi.</div>"
    return "<div class='ms-prod'><b>Produzione cliente:</b> fail-closed. Marker di collaudo, anomalie linguistiche cross-language o incoerenze tecniche bloccano la release; nessun pacchetto cliente viene creato.</div>"


def _release_mode_reset(release_mode_label):
    # Switching TEST/PRODUCTION invalidates the previous release action/result.
    # This avoids showing a green internal-test result while Production is selected
    # and forces a fresh readiness confirmation before the button is enabled again.
    return (
        _release_mode_help(release_mode_label),
        gr.update(interactive=False),
        "",
        None,
        gr.update(value=pd.DataFrame(), visible=False),
    )


def _friendly_release_failure(exc: Exception, mode: str) -> str:
    text=str(exc or '').strip() or type(exc).__name__
    low=text.casefold()
    if mode == 'production' and (
        'traduzione di collaudo' in low or 'marker di collaudo' in low
        or 'controllo linguistico' in low or 'anomalie linguistiche' in low
        or 'linguistic' in low or 'source-copied' in low or 'source copied' in low
    ):
        return (
            "<div class='ms-card'><h3 class='ms-block'>⛔ Release PRODUZIONE bloccata correttamente</h3>"
            "<p><b>Motivo:</b> i gate di sicurezza hanno rilevato contenuti identificati come collaudo o una traduzione linguisticamente non plausibile per la lingua target.</p>"
            "<p><b>Nessun pacchetto cliente è stato creato.</b></p>"
            "<p>Correggi/approva una traduzione reale, riesegui il controllo qualità e poi la readiness PRODUZIONE. "
            "Per benchmark e fixture, usa <b>Collaudo interno</b>.</p></div>"
        )
    if 'Release ID già utilizzato o riservato' in text:
        return (
            "<div class='ms-card'><h3 class='ms-block'>Release ID già utilizzato</h3>"
            "<p>Questo identificativo appartiene già a snapshot immutabili di un tentativo precedente e non può essere sovrascritto.</p>"
            "<p><b>Nessun nuovo dato è stato scritto.</b> Scegli un nuovo Release ID e riprova.</p></div>"
        )
    if 'Release bloccata:' in text:
        return f"<div class='ms-card'><h3 class='ms-block'>⛔ Release bloccata dai gate</h3><p>{text}</p><p>Risolvi gli elementi indicati nella readiness e riprova.</p></div>"
    return (
        "<div class='ms-card'><h3 class='ms-block'>Release non creata</h3>"
        f"<p><b>{type(exc).__name__}</b>: {text}</p>"
        f"<p class='ms-muted'>Dettagli tecnici registrati in {APP_LOG}</p></div>"
    )


def release_ui(project_dir, publication_id, target_langs, released_by, release_id, profile_id, idml_template, release_mode_label):
    langs=canonical_langs(target_langs)
    if not langs: langs=canonical_langs(read_project(project_dir).get('target_langs',[]))
    out=Path(project_dir)/'Releases'; out.mkdir(exist_ok=True)
    mode='test' if str(release_mode_label or '').startswith('Collaudo') else 'production'
    try:
        r=create_release_bundle(project_dir,publication_id,target_langs=langs,released_by=released_by or 'Release Manager',release_id=(release_id or '').strip() or None,profile_id=(profile_id or '').strip() or None,idml_template=idml_template or None,release_mode=mode,out_dir=out)
    except Exception as e:
        _app_log(f"Release blocked/failed mode={mode} publication={publication_id}", e)
        return _friendly_release_failure(e, mode), None, gr.update(value=pd.DataFrame(), visible=False)
    mode_label='COLLAUDO INTERNO' if mode=='test' else 'PRODUZIONE CLIENTE'
    msg=f"✅ Release **{mode_label}** **{r['release_id']}** creata. Snapshot immutabili e checksum inclusi."
    if r.get('readiness', {}).get('compliance', {}).get('compliance_applicable'):
        msg += " Snapshot Compliance dello scope attivo incluso e hashato."
    return msg, str(r['release_zip']), gr.update(value=_friendly_release_artifacts(r['artifacts']), visible=True)

# ============================================================================
# Milestone 3.5B: Guided Compliance UX.
# Every status/count/validation decision is read from compliance_view.py /
# ComplianceService -- nothing here recomputes a business rule. This layer
# only formats: HTML cards, DataFrame columns, Dropdown labels, and which
# Gradio component gets which message. A failed decision NEVER raises
# gr.Error (that would corrupt sibling outputs on this multi-output tab,
# same lesson learned in Milestone 3D's IDML review panel) -- it always
# comes back as an inline "⛔ ..." message in the relevant Markdown, with
# the dashboard/list/detail re-rendered from whatever is ACTUALLY
# persisted (i.e. unchanged, since ComplianceService validates before any
# write).
# ============================================================================
_COMPLIANCE_STATUS_COLOR = {"Covered": "#067647", "To verify": "#b54708", "Missing": "#b42318", "Not applicable": "#475467"}
_COMPLIANCE_TABLE_COLS = ["Requisito", "Titolo", "Riferimento", "Stato", "Motivo", "Evidenza"]
_COMPLIANCE_EVIDENCE_COLS = ["Tipo", "Riferimento", "Aggiornata", "Collegata da"]
_COMPLIANCE_DISCLAIMER = (
    "<div class='ms-muted' style='margin-top:8px;font-size:12px'>Stato della documentazione rispetto al perimetro "
    "configurato: non è una dichiarazione di conformità legale, certificazione o marcatura CE. La responsabilità "
    "finale resta del produttore/organizzazione competente.</div>"
)

def _compliance_status_badge(status: str) -> str:
    color = _COMPLIANCE_STATUS_COLOR.get(status, "#475467")
    return f"<span style='color:{color};font-weight:700'>{html.escape(status)}</span>"

def _compliance_dashboard_html(d: dict) -> str:
    if not d.get("has_scope"):
        return ("<div class='ms-prod'><b>Nessuna valutazione di compliance attiva per questa pubblicazione.</b><br>"
                "ManualSync non sta confrontando questa pubblicazione con nessun perimetro normativo al momento — "
                "non significa che sia tutto a posto, significa che non è stata ancora impostata una valutazione. "
                "Seleziona uno scope compliance esistente (o creane uno in modalità avanzata) per iniziare.</div>")
    c = d["counts"]; scope = d.get("scope") or {}
    title = scope.get("title") or scope.get("scope_id") or "Scope"
    jurisdiction = f" · {scope['jurisdiction']}" if scope.get("jurisdiction") else ""
    parts = [
        f"<div class='ms-card'><h3 style='margin-top:0'>{html.escape(str(title))}{html.escape(jurisdiction)}</h3>",
        "<div class='ms-kpis'>",
        f"<div class='ms-kpi'><span>Coperti</span><b>{c['Covered']}</b></div>",
        f"<div class='ms-kpi'><span>Da verificare</span><b>{c['To verify']}</b></div>",
        f"<div class='ms-kpi'><span>Mancanti</span><b>{c['Missing']}</b></div>",
        f"<div class='ms-kpi'><span>Non applicabili</span><b>{c['Not applicable']}</b></div>",
        "</div>",
        f"<div class='ms-muted'>Requisiti nel perimetro: {d['total']}</div>",
    ]
    stale_bits = []
    if d.get("stale_requirements"):
        n = len(d["stale_requirements"])
        stale_bits.append(f"{n} requisito da riesaminare (riferimento normativo aggiornato)" if n == 1
                          else f"{n} requisiti da riesaminare (riferimento normativo aggiornato)")
    if d.get("stale_standard_mappings"):
        n = len(d["stale_standard_mappings"])
        stale_bits.append(f"{n} collegamento a uno standard da riconfermare (versione aggiornata)" if n == 1
                          else f"{n} collegamenti a standard da riconfermare (versione aggiornata)")
    if stale_bits:
        parts.append("<div class='ms-prod' style='margin-top:10px'>" + " · ".join(stale_bits) + "</div>")
    parts.append(_COMPLIANCE_DISCLAIMER)
    parts.append("</div>")
    return "".join(parts)

def _compliance_requirement_table(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_COMPLIANCE_TABLE_COLS)
    return pd.DataFrame([{
        "Requisito": r["requirement_id"], "Titolo": r["title"], "Riferimento": r["reference"],
        "Stato": r["status"], "Motivo": r["reason"], "Evidenza": "Sì" if r["has_evidence"] else "No",
    } for r in rows])

def _requirement_dropdown_choices(rows: list[dict]) -> list[str]:
    return [f"{r['requirement_id']} — {r['title']}" for r in rows]

def _requirement_label_for(rows: list[dict], rid: str | None) -> str | None:
    if not rid:
        return None
    for r in rows:
        if r["requirement_id"] == rid:
            return f"{r['requirement_id']} — {r['title']}"
    return None

def _parse_requirement_id_from_label(label: str | None) -> str:
    return (label or "").split(" — ", 1)[0].strip()

def _fmt_ts(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ") or "—"

def _compliance_detail_placeholder() -> tuple:
    return (
        "<div class='ms-muted'>Seleziona un requisito dalla lista qui sopra per vederne il dettaglio.</div>",
        "<div class='ms-muted'>—</div>", gr.update(value=None),
        "", "<div class='ms-muted'>—</div>", gr.update(value=None),
        pd.DataFrame(columns=_COMPLIANCE_EVIDENCE_COLS),
        gr.update(choices=[], value=None), gr.update(choices=[], value=None),
        "", "", "", "", "",
    )

def _compliance_render_open_requirement(svc, repo, scope_id: str, rid: str, *, applic_msg: str = "",
                                        cov_msg: str = "", evidence_msg: str = "",
                                        applic_radio_value: str | None = None,
                                        cov_radio_value: str | None = None) -> tuple:
    detail = cv.requirement_detail(svc, scope_id, rid)
    req = detail["requirement"]
    badge = _compliance_status_badge(detail["status"])
    warn = "" if detail["requirement_is_current"] else (
        "<div class='ms-prod'>Il riferimento normativo di questo requisito è stato aggiornato dopo l'ultima "
        "stesura: verifica che il testo sia ancora corretto.</div>")
    header_html = (
        f"<div class='ms-card'><h3 style='margin-top:0'>{html.escape(req['title'])} &nbsp;{badge}</h3>"
        f"<div class='ms-muted'>{html.escape(detail['reference'])}</div>"
        f"<p>{html.escape(req.get('description') or '(nessuna descrizione)')}</p>"
        f"<div class='ms-muted' style='font-size:12px'>{html.escape(detail['reason'])}</div>{warn}</div>"
    )

    ap = detail["applicability"]
    if ap:
        just = f" — {html.escape(ap['justification'])}" if ap.get("justification") else ""
        applic_current = (f"<div class='ms-muted'>Decisione attuale: <b>{cv.APPLIES_LABELS.get(ap['applies'], ap['applies'])}</b>{just}"
                          f"<br>Deciso da <b>{html.escape(ap['decided_by'])}</b> il {_fmt_ts(ap['decided_at'])}</div>")
    else:
        applic_current = "<div class='ms-muted'>Applicabilità non ancora valutata.</div>"

    cov_banner = "" if detail["can_decide_coverage"] else (
        "<div class='ms-prod'>La copertura può essere decisa solo dopo aver impostato applicabilità = "
        "«Si applica» qui sopra.</div>")
    asm = detail["assessment"]
    if asm:
        cm = f" — {html.escape(asm['comment'])}" if asm.get("comment") else ""
        cov_current = (f"<div class='ms-muted'>Decisione attuale: <b>{cv.COVERAGE_LABELS.get(asm['status'], asm['status'])}</b>{cm}"
                       f"<br>Deciso da <b>{html.escape(asm['decided_by'])}</b> il {_fmt_ts(asm['decided_at'])}</div>")
    else:
        cov_current = "<div class='ms-muted'>Copertura non ancora valutata.</div>"

    evidence_rows = detail["evidence"]
    evidence_df = pd.DataFrame([{
        "Tipo": {"topic": "Sezione", "block": "Blocco", "table": "Tabella", "figure": "Figura",
                "external_file": "File esterno", "customer_document": "Documento cliente",
                "human_attestation": "Attestazione umana"}.get(e["evidence_kind"], e["evidence_kind"]),
        "Riferimento": e["object_id"] or e.get("external_ref") or e.get("description") or "—",
        "Aggiornata": "Sì" if e["is_current"] else "No",
        "Collegata da": e["linked_by"],
    } for e in evidence_rows]) if evidence_rows else pd.DataFrame(columns=_COMPLIANCE_EVIDENCE_COLS)

    candidates = cv.evidence_candidates(repo)
    candidate_choices = [f"{c['kind']}:{c['object_id']}:{c['version']} — {c['label']}" for c in candidates]
    unlink_choices = [f"{e['evidence_id']} — {cv.evidence_label(e)}" for e in evidence_rows]

    inst = detail["instrument"]
    inst_line = f"{inst['official_identifier']} (v{inst['version']})" if inst else "—"
    std_lines = "; ".join(f"{s['designation']} (v{s['standard_version']})" for s in detail["standards"]) or "—"
    adv = (f"<div class='ms-muted' style='font-size:12px'>requirement_id: {html.escape(rid)} · version: {req['version']} · "
          f"status: {req['status']} · instrument: {html.escape(inst_line)} · standard mapping: {html.escape(std_lines)} · "
          f"scope_id: {html.escape(scope_id)}</div>")

    return (
        header_html, applic_current, gr.update(value=applic_radio_value), cov_banner, cov_current,
        gr.update(value=cov_radio_value),
        evidence_df, gr.update(choices=candidate_choices, value=None), gr.update(choices=unlink_choices, value=None),
        adv, rid, applic_msg, cov_msg, evidence_msg,
    )

def _compliance_full_bundle_empty() -> tuple:
    return (
        _compliance_dashboard_html({"has_scope": False}),
        pd.DataFrame(columns=_COMPLIANCE_TABLE_COLS),
        gr.update(choices=[], value=None),
        *_compliance_detail_placeholder(),
    )

def _compliance_full_bundle(svc, repo, scope_id: str | None, rid: str | None, *, applic_msg: str = "",
                            cov_msg: str = "", evidence_msg: str = "",
                            applic_radio_value: str | None = None, cov_radio_value: str | None = None) -> tuple:
    dash = _compliance_dashboard_html(cv.dashboard(svc, scope_id))
    rows = cv.requirement_list_rows(svc, scope_id) if scope_id else []
    table = _compliance_requirement_table(rows)
    dd = gr.update(choices=_requirement_dropdown_choices(rows), value=_requirement_label_for(rows, rid))
    if scope_id and rid:
        try:
            detail = _compliance_render_open_requirement(svc, repo, scope_id, rid, applic_msg=applic_msg,
                                                          cov_msg=cov_msg, evidence_msg=evidence_msg,
                                                          applic_radio_value=applic_radio_value,
                                                          cov_radio_value=cov_radio_value)
        except KeyError:
            detail = _compliance_detail_placeholder()
    else:
        detail = _compliance_detail_placeholder()
    return (dash, table, dd, *detail)

def compliance_load_ui(project_dir, publication_id):
    if not project_dir:
        return (gr.update(choices=[], value=None), *_compliance_full_bundle_empty())
    repo, svc = cv.open_service(project_dir)
    try:
        opts = cv.scope_options(svc, (publication_id or "PUB-MAIN").strip())
        scope_id = opts[0][1] if opts else None  # opts are (label, value) tuples -- value is the scope_id
        bundle = _compliance_full_bundle(svc, repo, scope_id, None)
        return (gr.update(choices=opts, value=scope_id), *bundle)
    finally:
        repo.close()

def compliance_switch_scope_ui(project_dir, scope_id):
    if not project_dir:
        return _compliance_full_bundle_empty()
    repo, svc = cv.open_service(project_dir)
    try:
        return _compliance_full_bundle(svc, repo, scope_id, None)
    finally:
        repo.close()

def compliance_open_requirement_ui(project_dir, scope_id, requirement_label):
    rid = _parse_requirement_id_from_label(requirement_label)
    if not project_dir:
        return _compliance_full_bundle_empty()
    repo, svc = cv.open_service(project_dir)
    try:
        return _compliance_full_bundle(svc, repo, scope_id, rid or None)
    finally:
        repo.close()

def compliance_decide_applicability_ui(project_dir, scope_id, rid, applies_label, reviewer, justification, comment,
                                       cur_cov_radio=None):
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    repo, svc = cv.open_service(project_dir)
    try:
        msg = ""
        failed = False
        if scope_id and rid:
            applies_code = cv.APPLIES_LABELS_REVERSE.get(applies_label or "")
            if not applies_code:
                msg = "⛔ Seleziona un'opzione (Si applica / Non applicabile / Da verificare) prima di confermare."
                failed = True
            else:
                try:
                    svc.set_applicability(scope_id, rid, applies_code, actor=reviewer or "",
                                          justification=justification or "", comment=comment or "")
                    msg = "✅ Decisione registrata."
                except Exception as exc:
                    msg = f"⛔ {exc}"
                    failed = True
        # Acceptance fix: a validation failure (e.g. missing justification,
        # missing reviewer) must be inline and non-destructive -- the choice
        # the user just made stays visibly selected so they can add what's
        # missing and re-confirm without reselecting it. On SUCCESS (or when
        # nothing was submitted) the Radio still resets to unselected: the
        # decision just taken is shown in applic_current above it, and an
        # unselected Radio is the correct starting point for the NEXT edit
        # -- same "never pre-selected" invariant as a fresh open. The
        # COVERAGE radio is untouched by an applicability action either way
        # -- echo back whatever it currently shows (found to reset to None
        # otherwise, wiping an unrelated pending choice during 3.5B
        # stabilization acceptance).
        return _compliance_full_bundle(svc, repo, scope_id, rid, applic_msg=msg,
                                       applic_radio_value=applies_label if failed else None,
                                       cov_radio_value=cur_cov_radio)
    finally:
        repo.close()

def compliance_decide_coverage_ui(project_dir, scope_id, rid, coverage_label, reviewer, comment, cur_applic_radio=None):
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    repo, svc = cv.open_service(project_dir)
    try:
        msg = ""
        failed = False
        if scope_id and rid:
            cov_code = cv.COVERAGE_LABELS_REVERSE.get(coverage_label or "")
            if not cov_code:
                msg = "⛔ Seleziona un'opzione (Coperto / Da verificare / Mancante) prima di confermare."
                failed = True
            else:
                try:
                    svc.set_assessment(scope_id, rid, cov_code, actor=reviewer or "", comment=comment or "")
                    msg = "✅ Decisione registrata."
                except Exception as exc:
                    msg = f"⛔ {exc}"
                    failed = True
        # Same non-destructive-on-failure fix as applicability, see there --
        # and the same "don't touch the OTHER radio" symmetry.
        return _compliance_full_bundle(svc, repo, scope_id, rid, cov_msg=msg,
                                       cov_radio_value=coverage_label if failed else None,
                                       applic_radio_value=cur_applic_radio)
    finally:
        repo.close()

def compliance_link_evidence_ui(project_dir, scope_id, rid, candidate_label, reviewer,
                                cur_applic_radio=None, cur_cov_radio=None):
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    repo, svc = cv.open_service(project_dir)
    try:
        msg = ""
        if scope_id and rid:
            if not candidate_label:
                msg = "⛔ Scegli un contenuto dalla lista prima di collegarlo come evidenza."
            elif not str(reviewer or "").strip():
                msg = "⛔ Indica il tuo nome per registrare il collegamento dell'evidenza."
            else:
                try:
                    head = candidate_label.split(" — ", 1)[0]
                    kind, object_id, version_str = head.split(":", 2)
                    cv.link_ccms_object_as_evidence(svc, kind, object_id, int(version_str), rid, actor=reviewer)
                    msg = "✅ Evidenza collegata."
                except Exception as exc:
                    msg = f"⛔ {exc}"
        # Acceptance fix: linking evidence is a SUPPORTING action for a
        # pending Covered decision (the acceptance path is: try Covered ->
        # rejected -> link evidence -> confirm Covered again WITHOUT
        # reselecting). It must never reset either decision Radio -- echo
        # back exactly what the panel currently shows for both.
        return _compliance_full_bundle(svc, repo, scope_id, rid, evidence_msg=msg,
                                       applic_radio_value=cur_applic_radio, cov_radio_value=cur_cov_radio)
    finally:
        repo.close()

def compliance_unlink_evidence_ui(project_dir, scope_id, rid, unlink_label, reviewer,
                                  cur_applic_radio=None, cur_cov_radio=None):
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    repo, svc = cv.open_service(project_dir)
    try:
        msg = ""
        if scope_id and rid:
            if not unlink_label:
                msg = "⛔ Scegli quale evidenza scollegare."
            elif not str(reviewer or "").strip():
                msg = "⛔ Indica il tuo nome per registrare lo scollegamento."
            else:
                evidence_id = unlink_label.split(" — ", 1)[0].strip()
                try:
                    ok = svc.unlink_evidence_requirement(evidence_id, rid, actor=reviewer)
                    msg = "✅ Evidenza scollegata." if ok else "⛔ Collegamento non trovato (forse già rimosso)."
                except Exception as exc:
                    msg = f"⛔ {exc}"
        # Same symmetry as link: unlinking must never reset a pending
        # applicability/coverage choice either.
        return _compliance_full_bundle(svc, repo, scope_id, rid, evidence_msg=msg,
                                       applic_radio_value=cur_applic_radio, cov_radio_value=cur_cov_radio)
    finally:
        repo.close()

with gr.Blocks(title=BRAND_NAME) as demo:
    project_state=gr.State(INITIAL_PROJECT)
    with gr.Sidebar(width=236, elem_id="ms-sidebar"):
        gr.HTML(f"<div class='ms-brand'><div class='ms-logo'>M</div><div><div class='ms-brand-title'>{BRAND_NAME}</div><div class='ms-brand-sub'>Brand provvisorio · Technical Content Intelligence</div></div></div>")
        nav_home=gr.Button("⌂   Home",variant="primary")
        gr.HTML("<div class='ms-side-section'>Workflow</div>")
        nav_revision=gr.Button("◫   Revisione",variant="secondary")
        nav_authoring=gr.Button("✎   Contenuti",variant="secondary")
        nav_compliance=gr.Button("☑   Compliance",variant="secondary")
        nav_translation=gr.Button("⇄   Traduzioni",variant="secondary")
        nav_jobs=gr.Button("▣   Job & fornitori",variant="secondary")
        nav_release=gr.Button("✓   Release",variant="secondary")
        gr.HTML("<div class='ms-side-section'>Sistema</div>")
        nav_settings=gr.Button("⚙   Impostazioni",variant="secondary")
        gr.Button("?   Aiuto", interactive=False)
        sidebar_context=gr.HTML(_sidebar_context(INITIAL_PROJECT))

    with gr.Tabs(selected="home", elem_id="workspace-tabs") as workspace_tabs:
        with gr.Tab("Home", id="home"):
            with gr.Row(elem_classes=["ms-pagebar"]):
                gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workspace</div><h1>Home</h1><p>Panoramica del progetto e prossime attività.</p></div></div>")
                home_new_project=gr.Button("+ Nuovo progetto",variant="primary",elem_classes=["ms-new-project-btn"])
            # Milestone 3A: guided first-run entry point. Purely additive — shown only
            # when no project is active yet (state-driven, see project_state.change below).
            # Does not replace or hide the existing "+ Nuovo progetto" advanced path.
            with gr.Column(visible=not bool(INITIAL_PROJECT),elem_id="onboarding-choice-panel",elem_classes=["ms-panel"]) as onboarding_choice_panel:
                gr.HTML("<div class='ms-section-title'>Benvenuto</div><div class='ms-section-sub'>Non hai ancora un progetto. Non serve sapere cosa sono XML, DITA o XLIFF per iniziare.</div>")
                with gr.Row():
                    choice_empty=gr.Button("Crea un progetto vuoto",variant="secondary")
                    choice_legacy=gr.Button("Ho già dei manuali: importali",variant="primary")
                    choice_multi=gr.Button("Ho più manuali o revisioni",variant="secondary")
            # Milestone 3C: multi-document/folder legacy intake. Fully separate from the
            # single-document guided path above -- this only detects and, on explicit
            # human confirmation, persists family/revision/language lineage. It does not
            # create projects itself; project creation still goes through the existing
            # "Ho già dei manuali" path, which auto-recognizes an already-confirmed file
            # via _stamp_legacy_family_if_known.
            with gr.Column(visible=False,elem_id="legacy-multi-doc-panel",elem_classes=["ms-panel"]) as legacy_multi_panel:
                gr.HTML("<div class='ms-section-title'>Ho più manuali o revisioni</div><div class='ms-section-sub'>Carica più documenti (PDF, Word .docx/.doc) o una cartella intera: proviamo a capire quali sono revisioni successive o lingue diverse dello stesso manuale.</div>")
                lm_back=gr.Button("← Cambia scelta",size="sm")
                with gr.Row():
                    lm_files=gr.File(label="Seleziona più file",type='filepath',file_count='multiple',file_types=['.pdf','.docx','.doc','.idml'])
                    lm_dir=gr.File(label="Oppure seleziona una cartella",type='filepath',file_count='directory')
                lm_analyze=gr.Button("Analizza",variant="primary")
                lm_summary=gr.Markdown()
                lm_state=gr.State()
                # Milestone 3C acceptance fix: a fixed pool of Radio rows, one per
                # reviewable item (relation / pending-family-link / ambiguous pair).
                # Each row's own choice set is set at analysis time in
                # legacy_multi_analyze_ui — there is deliberately no generic
                # "confirm everything" checkbox: an order-ambiguous revision_of
                # forces an explicit "A before B"/"B before A" choice (no order is
                # ever assumed), an ambiguous pair gets a real "Correggi" option to
                # assign the correct relation type, and a pending same-family pair
                # is only linked if explicitly chosen here (matching exactly what
                # gets persisted).
                lm_item_radios=[gr.Radio(visible=False) for _ in range(_LEGACY_MAX_REVIEW_ITEMS)]
                with gr.Row(visible=False) as lm_confirm_row:
                    with gr.Column():
                        lm_confirmed_by=gr.Textbox(label="Il tuo nome *",placeholder="Necessario per confermare")
                        lm_name_error=gr.HTML(visible=False,elem_classes=["ms-inline-error"])
                    # UX fix: a single decision flow -- selecting "Non sono
                    # correlati" and clicking this same button already confirms
                    # independence without persisting anything (see
                    # legacy_multi_confirm_ui's any_decision handling below). A
                    # separate "Nessuna relazione correlata" button duplicated
                    # this exact outcome via a second path and had already
                    # caused a real user mistake -- removed.
                    lm_confirm=gr.Button("Conferma selezione",variant="primary")
            with gr.Column(visible=False,elem_id="guided-legacy-onboarding-panel",elem_classes=["ms-panel"]) as legacy_onboarding_panel:
                gr.HTML("<div class='ms-section-title'>Importa quello che hai già</div><div class='ms-section-sub'>Carica un PDF o un documento Word: creiamo il progetto e leggiamo il documento per te.</div>")
                lg_back=gr.Button("← Cambia scelta",size="sm")
                lg_name=gr.Textbox(label="Nome progetto — opzionale, lo ricaviamo dal file se lo lasci vuoto")
                lg_pdf=gr.File(label="Il tuo manuale (PDF, Word .docx/.doc o InDesign .idml)",type='filepath',file_types=['.pdf','.docx','.doc','.idml'])
                lg_submit=gr.Button("Importa e crea progetto",variant="primary")
                lg_lang_prompt=gr.Markdown(visible=False)
                lg_lang_pick=gr.Radio(_GUIDED_LANG_CHOICES,label="Lingua del documento",visible=False)
                lg_lang_confirm=gr.Button("Conferma e importa",variant="primary",visible=False)
                lg_review=gr.Button("Rivedi il contenuto importato",variant="primary",visible=False)
            # RC2: il form di creazione e immediatamente sotto il comando che lo apre.
            # In RC1 il pannello era dopo le Quick Actions: il click funzionava, ma su una
            # finestra desktop standard il form appariva fuori viewport e sembrava non reagire.
            with gr.Column(visible=False,elem_id="new-project-panel",elem_classes=["ms-create-project-panel"]) as new_project_panel:
                gr.HTML("<div class='ms-section-title'>Nuovo progetto</div><div class='ms-section-sub'>Crea un nuovo workspace senza lasciare la Home.</div>")
                np_back=gr.Button("← Cambia scelta",size="sm")
                p_name=gr.Textbox(label="Nome progetto")
                with gr.Row(): p_client=gr.Textbox(label="Cliente / Business unit"); p_product=gr.Textbox(label="Prodotto / macchina")
                with gr.Row(): p_code=gr.Textbox(label="Codice manuale"); p_rev=gr.Textbox(label="Revisione")
                with gr.Row(): p_source=gr.Textbox(label="Lingua sorgente",value="en-US"); p_targets=gr.Textbox(label="Lingue target",placeholder="it-IT, de-DE, fr-FR")
                p_owner=gr.Textbox(label="Responsabile")
                with gr.Row():
                    p_create=gr.Button("Crea progetto",variant="primary")
                    p_cancel=gr.Button("Annulla")
                p_msg=gr.Markdown()
            project_card=gr.HTML(_project_card(INITIAL_PROJECT))
            gr.HTML("<div class='ms-quick-head'><div class='ms-section-title'>Azioni rapide</div><div class='ms-section-sub'>Apri direttamente le attività più frequenti.</div></div>")
            with gr.Row(elem_classes=["ms-quick-grid"]):
                with gr.Column(elem_classes=["ms-quick-card"]):
                    gr.HTML("<div class='ms-quick-icon'>↺</div><div class='ms-quick-title'>Analizza revisione</div><div class='ms-quick-desc'>Confronta originale precedente, traduzione e nuova revisione.</div>")
                    home_revision=gr.Button("Apri Revisione",elem_classes=["ms-card-cta"])
                with gr.Column(elem_classes=["ms-quick-card"]):
                    gr.HTML("<div class='ms-quick-icon'>文</div><div class='ms-quick-title'>Genera pacchetto lingua</div><div class='ms-quick-desc'>Prepara XLIFF e code di revisione per le lingue target.</div>")
                    home_translation=gr.Button("Vai a Traduzioni",elem_classes=["ms-card-cta"])
                with gr.Column(elem_classes=["ms-quick-card","brand"]):
                    gr.HTML("<div class='ms-quick-icon'>✓</div><div class='ms-quick-title'>Verifica readiness</div><div class='ms-quick-desc'>Controlla i gate e prepara una release controllata.</div>")
                    home_release=gr.Button("Vai a Release",elem_classes=["ms-card-cta"])
            home_new_project.click(lambda:(gr.update(visible=True),""),None,[new_project_panel,p_msg],scroll_to_output=True,show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            p_cancel.click(lambda:(gr.update(visible=False),""),None,[new_project_panel,p_msg],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            p_create.click(create_project_ui,[p_name,p_client,p_product,p_code,p_rev,p_source,p_targets,p_owner],[project_state,project_card,sidebar_context,p_msg,new_project_panel],scroll_to_output=True,show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            # Milestone 3A: guided onboarding wiring (additive; components defined above).
            choice_empty.click(lambda:(gr.update(visible=False),gr.update(visible=True),""),None,[onboarding_choice_panel,new_project_panel,p_msg],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            choice_legacy.click(lambda:(gr.update(visible=False),gr.update(visible=True)),None,[onboarding_choice_panel,legacy_onboarding_panel],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            # Milestone 3C wiring (additive; components defined above).
            choice_multi.click(lambda:(gr.update(visible=False),gr.update(visible=True)),None,[onboarding_choice_panel,legacy_multi_panel],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            # NOTE: no scroll_to_output here (unlike other guided-flow buttons) --
            # auto-scrolling the page right as the review radios render risks
            # shifting them out from under a user who starts clicking as soon as
            # results appear, silently leaving the radio on its default choice
            # ("Non sono correlati") even though the user believes they selected
            # something else. Reproduced live: an identically-timed click landed
            # on the wrong element purely because of this auto-scroll.
            lm_analyze.click(legacy_multi_analyze_ui,[lm_files,lm_dir],[lm_state,lm_summary,lm_confirm_row]+lm_item_radios,trigger_mode="once",concurrency_limit=1)
            lm_confirm.click(legacy_multi_confirm_ui,[lm_state,lm_confirmed_by]+lm_item_radios,[lm_summary,lm_confirm_row,lm_confirmed_by,lm_name_error]+lm_item_radios,trigger_mode="once",concurrency_limit=1)
            # Clears the inline required-field error the moment a valid name is
            # typed, without waiting for another Conferma selezione click.
            lm_confirmed_by.input(_legacy_name_field_live_validate,[lm_confirmed_by],[lm_confirmed_by,lm_name_error],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            # UX acceptance fix: visible way back to the initial three-way choice
            # from every onboarding sub-panel, with no refresh and no persistence.
            np_back.click(onboarding_back_ui,[project_state],[new_project_panel,onboarding_choice_panel],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            lg_back.click(onboarding_back_ui,[project_state],[legacy_onboarding_panel,onboarding_choice_panel],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            lm_back.click(onboarding_back_ui,[project_state],[legacy_multi_panel,onboarding_choice_panel],show_progress="hidden",trigger_mode="once",concurrency_limit=1)
            # lg_submit/lg_lang_confirm are wired at the bottom of the file: they populate
            # the "Redazione & contenuti" tab fields (c_topics/c_blocks/...), which are
            # defined later in this Blocks graph.
            # Keeps the welcome panel hidden once any path (guided, empty-form, or restore) sets an active project.
            project_state.change(lambda pd:gr.update(visible=not bool(pd)),[project_state],[onboarding_choice_panel])

        with gr.Tab("Analizza revisione",id="revision"):
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workflow</div><h1>Analizza revisione</h1><p>Confronta revisioni e separa riuso sicuro, modifiche tecniche e nuovo lavoro.</p></div></div>")
            with gr.Row(): a_old=gr.File(label="Vecchio originale PDF",type='filepath'); a_tr=gr.File(label="Vecchia traduzione PDF",type='filepath'); a_new=gr.File(label="Nuovo originale PDF",type='filepath')
            a_full=gr.File(label="Nuovo manuale completo — opzionale",type='filepath'); a_btn=gr.Button("Analizza revisione",variant='primary'); a_summary=gr.HTML(); a_queue=gr.Dataframe(interactive=False,wrap=True,max_height=600)
            with gr.Row(): a_report=gr.File(label="Rapporto"); a_queue_file=gr.File(label="Coda di lavoro")
            a_btn.click(revision_analysis_ui,[project_state,a_old,a_tr,a_new,a_full],[a_summary,a_queue,a_report,a_queue_file])

        with gr.Tab("Redazione & contenuti",id="authoring") as authoring_tab:
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workflow</div><h1>Redazione & contenuti</h1><p>Repository strutturato e redazione AI grounded con revisione umana.</p></div></div>")

            # UX clarity fix: ONE shared identity field for the whole tab,
            # shown first, before any action that can modify or approve
            # content (source approval, topic/block creation, save, reorder,
            # archive/restore, revert, review submission). There used to be
            # two separate "Il tuo nome" textboxes -- one here, one inside
            # the Studio panel below -- with nothing to tell them apart; a
            # real user hit exactly that during manual acceptance. The
            # backend actor requirement is untouched and stays fail-closed
            # either way; a distinct reviewer role (review-decide, grounded
            # queue) keeps its own explicit "Revisore" label rather than
            # reusing or duplicating this one.
            gr.HTML("<div class='ms-section-title'>La tua identità</div>")
            c_actor = gr.Textbox(label="Il tuo nome", info="Serve per registrare chi effettua modifiche e decisioni.")
            # Actor state consistency fix: a real user typed a name here,
            # saw it stay visibly filled, went back to a table and pressed
            # Salva -- and still got "Inserisci il tuo nome per
            # continuare." even though the field was genuinely non-empty.
            # Every mutation callback below now reads actor_state (this
            # State), never c_actor directly: a gr.State value is tracked
            # authoritatively server-side per session and isn't subject to
            # whatever can leave a live Textbox's client-rendered value
            # transiently out of sync with what a DIFFERENT button's click
            # payload captures at that instant.
            #
            # Sync trigger: .change(), not .input() -- empirically verified
            # against this exact Gradio 6.5.1 build (isolated file-based
            # server-side trace, both synthetic and real-keyboard typing):
            # .input() on a plain gr.Textbox never reached the backend at
            # all in this build, while .change() fired reliably every time.
            # This isn't a UX regression: c_actor is never a programmatic
            # update target anywhere in this app (confirmed -- it only ever
            # appears as an input, never an output), so .change()'s usual
            # "also fires on programmatic pushes" caveat (the reason
            # st_table_grid deliberately uses .input() instead) doesn't
            # apply here -- there is nothing programmatic to filter out.
            # A Textbox's change event fires on native blur, which already
            # happens automatically the instant the user clicks anywhere
            # else (including the very button they're about to press) --
            # so this never asks the user to do anything beyond typing the
            # name once and continuing normally.
            actor_state = gr.State("")
            c_actor.change(lambda v: v, [c_actor], [actor_state])

            # Milestone 3A: standard view, shown by default. Same topics/blocks data as
            # the technical dataframes below (no second backend) — just friendlier
            # columns. No ID/metadata/re-import button in this default view.
            gr.HTML("<div class='ms-section-title'>Revisione del contenuto importato</div><div class='ms-section-sub'>Controlla il contenuto importato prima di approvarlo come sorgente. Per i dettagli tecnici, apri “Dettagli tecnici (modalità avanzata)” qui sotto.</div>")
            c_simple_table=gr.Dataframe(interactive=False,wrap=True,max_height=500)
            # Milestone 3D constraint: master-spread/orphan/unsupported/ambiguous-
            # asset/caption-candidate items from an IDML import are shown here in
            # plain language (no Story/Spread ids, no JSON to open) and BLOCK
            # "Approva sorgente" below until explicitly acknowledged.
            gr.HTML("<div class='ms-section-title'>Elementi da verificare</div>")
            c_idml_review_html=gr.HTML(visible=False)
            with gr.Row(visible=False) as c_idml_review_row:
                with gr.Column():
                    c_idml_review_radios=[gr.Radio(visible=False) for _ in range(_IDML_REVIEW_MAX_ITEMS)]
                    c_idml_review_confirm=gr.Button("Conferma decisioni")
            c_simple_confirm=gr.Checkbox(label="Confermo di aver revisionato il contenuto")
            c_simple_approve=gr.Button("Approva sorgente",variant="primary")
            c_simple_msg=gr.Markdown()

            # ----------------------------------------------------------------
            # Milestone 4B.2: Professional Topic & Block Editor ("Studio").
            # Component LAYOUT only here (kept visually prominent, right where
            # a technical writer lands on this tab); the .click()/.blur()
            # WIRING is deferred to just below c_pub's own definition further
            # down this same Blocks scope, since the Studio reuses that exact
            # Publication ID field -- "non inventare una seconda nozione di
            # documento aperto" -- rather than declare a second one here.
            # ----------------------------------------------------------------
            gr.HTML("<div class='ms-section-title'>Editor professionale</div>")
            st_pub_header = gr.HTML()
            st_kind = gr.State(None); st_id = gr.State(None); st_version = gr.State(None)
            # 4C: a table/figure block's editable CONTENT lives on a
            # separate table_id/figure_id row (own version counter) --
            # st_block_type tells Save/Revert/Replace which dedicated
            # editor is open, st_object_ref_id carries that table_id/
            # figure_id (st_id stays the wrapping BLOCK's id, used by
            # Move/Archive/Restore exactly as for any other block).
            st_block_type = gr.State(None); st_object_ref_id = gr.State(None)
            # 4D: transient, per-edit-session record of the "Aggiorna
            # ovunque" decision on a shared table/figure -- never persisted
            # (see studio_confirm_shared_update_ui), always reset to False
            # by every navigation/save/revert/fork call path that does not
            # explicitly set it.
            st_shared_confirmed = gr.State(False)
            # 4D integrity gate: the impact snapshot token captured at the
            # exact moment "Aggiorna ovunque" was clicked (see studio_
            # confirm_shared_update_ui) -- re-verified against a fresh
            # get_reuse_impact() read at Save/Revert time, so a reference
            # added or removed after the user's preview fails the write
            # closed even when the object's own version hasn't changed.
            # Same reset lifecycle as st_shared_confirmed.
            st_shared_impact_token = gr.State(None)
            with gr.Row():
                with gr.Column(scale=2):
                    # UX clarity fix: replaces the old technical Dataframe as
                    # the primary standard-mode navigation -- a real document
                    # tree (topic rows, blocks indented beneath with human
                    # type labels + a short preview, no technical IDs, no
                    # horizontal scrollbar), one click to open. The Dataframe
                    # is kept for diagnostics only, moved into "Dettagli
                    # tecnici (modalità avanzata)" below.
                    st_outline_radio = gr.Radio(choices=[], label="Struttura manuale", value=None)
                    with gr.Row():
                        st_new_topic_title = gr.Textbox(label="Titolo nuovo topic", placeholder="Titolo del nuovo topic…", scale=3)
                        st_new_topic_btn = gr.Button("+ Nuovo topic", scale=1)
                    with gr.Row(visible=False) as st_new_table_row:
                        st_new_table_caption = gr.Textbox(label="Didascalia nuova tabella — opzionale", scale=3)
                        st_new_table_btn = gr.Button("+ Aggiungi tabella", scale=1)
                    with gr.Row(visible=False) as st_new_figure_row:
                        st_new_figure_file = gr.Image(label="Nuova figura — file immagine", type="filepath", scale=2)
                        with gr.Column(scale=2):
                            st_new_figure_caption = gr.Textbox(label="Didascalia — opzionale")
                            st_new_figure_alt = gr.Textbox(label="Testo alternativo — consigliato")
                            st_new_figure_btn = gr.Button("+ Aggiungi figura")
                    st_show_archived = gr.Checkbox(label="Mostra elementi archiviati", value=False)
                    st_refresh_btn = gr.Button("Aggiorna struttura")
                    st_search_box = gr.Textbox(label="Cerca nel manuale", placeholder="Titolo topic o testo di un blocco…")
                    st_search_btn = gr.Button("Cerca")
                    st_search_msg = gr.Markdown()
                    st_search_picker = gr.Dropdown(label="Risultati", choices=[], visible=False)
                    st_search_open_btn = gr.Button("Apri risultato", visible=False)
                with gr.Column(scale=3):
                    st_breadcrumb = gr.Markdown("Pubblicazione")
                    st_status_html = gr.HTML()
                    # UX clarity fix: with nothing selected, only the empty-state
                    # message above (from st_status_html) is shown -- every
                    # action control below carries its OWN visible= (not just
                    # interactive=), set together with interactive= in
                    # _studio_full_bundle, so the whole area disappears rather
                    # than showing as a wall of greyed-out buttons. (A first
                    # version wrapped this area in a single dynamically-visible
                    # gr.Group instead; reproduced against a minimal isolated
                    # repro app that Gradio 6.5.1 does not reliably apply a
                    # child's own visible= update when nested inside a Group
                    # whose own visibility does not change in that same
                    # response, so title/text ended up both showing at once --
                    # each control's visible= is set directly here instead.
                    # Same reasoning applies to the 4C table/figure fields
                    # below: each one carries its own visible=, never a
                    # shared wrapping Group.)
                    with gr.Row():
                        st_prev_btn = gr.Button("‹ Topic precedente")
                        st_next_btn = gr.Button("Topic successivo ›")
                    st_title_input = gr.Textbox(label="Titolo", visible=False)
                    st_text_input = gr.Textbox(label="Testo", lines=10, visible=False)
                    # 4C table editor -- reuses structured_tables/
                    # structured_table_versions unchanged (see
                    # AuthoringService.save_table); a plain interactive
                    # Dataframe gives cell edit, add/remove row, add/remove
                    # column and header-rename for free (Gradio 6.5.1's own
                    # built-in grid controls), with no spreadsheet formulas/
                    # sorting/merge-cells layered on top.
                    st_table_title = gr.Textbox(label="Titolo tabella", visible=False)
                    st_table_caption = gr.Textbox(label="Didascalia", visible=False)
                    # row_count/column_count "fixed" -- not the deprecated
                    # "let you cap size" use case, but the only mechanism
                    # this Gradio version actually honors to empty out the
                    # Dataframe's own unsafe right-click structural menu
                    # (see _fixed_grid_update). Kept in sync on every push,
                    # on BOTH components below.
                    #
                    # 4D Shared Table Unlock Editing Fix: TWO mutually
                    # exclusive Dataframe components instead of one whose
                    # `interactive` prop gets toggled dynamically. Root
                    # cause: Gradio 6.5.1's virtualized Dataframe DOES
                    # apply a live interactive=False->True change (verified
                    # directly against the mounted component), but that is
                    # exactly the kind of undocumented, third-party dynamic-
                    # prop behavior this codebase's own discipline (see the
                    # row/column context-menu bug just below) already
                    # treats as too fragile to depend on. Each component's
                    # own `interactive` is fixed for its entire lifetime;
                    # only `visible=` ever toggles, the same proven pattern
                    # already used for every other field in this panel.
                    st_table_grid_view = gr.Dataframe(interactive=False, type="pandas", visible=False, wrap=True,
                                                      row_count=(1, "fixed"), column_count=(2, "fixed"))
                    st_table_grid_edit = gr.Dataframe(interactive=True, type="pandas", visible=False, wrap=True,
                                                      row_count=(1, "fixed"), column_count=(2, "fixed"))
                    # The grid's own built-in right-click "Add row"/"Add
                    # column" context menu is a real, confirmed Gradio
                    # 6.5.1 reliability gap (see _table_rowcol_op above):
                    # it mutates the visible grid without reliably
                    # updating what Save reads. These 4 buttons are the
                    # supported way to add/remove rows and columns --
                    # each round-trips through Python, so Save always
                    # sees exactly what they produced.
                    with gr.Row(visible=False) as st_table_rowcol_row:
                        st_table_add_row_btn = gr.Button("+ Aggiungi riga")
                        st_table_remove_row_btn = gr.Button("− Rimuovi riga")
                        st_table_add_col_btn = gr.Button("+ Aggiungi colonna")
                        st_table_remove_col_btn = gr.Button("− Rimuovi colonna")
                    # 4C figure editor -- reuses figures/figure_versions
                    # unchanged (see AuthoringService.save_figure/
                    # replace_figure_image); preview is read-only, caption/
                    # alt text are the only inline-editable fields, "Sostituisci
                    # immagine" is a distinct action that creates a new
                    # figure version rather than silently overwriting bytes.
                    st_figure_preview = gr.Image(label="Anteprima", type="filepath", interactive=False, visible=False)
                    st_figure_caption = gr.Textbox(label="Didascalia", visible=False)
                    st_figure_alt = gr.Textbox(label="Testo alternativo", visible=False)
                    with gr.Row(visible=False) as st_figure_replace_row:
                        st_figure_replace_file = gr.Image(label="Sostituisci immagine — carica un nuovo file", type="filepath", scale=2)
                        st_figure_replace_btn = gr.Button("Sostituisci immagine", scale=1)
                    st_readonly_notice = gr.Markdown(visible=False)
                    st_shared_notice = gr.Markdown(visible=False)
                    # 4D: the reuse-aware shared-object decision UX -- shown
                    # only while a shared table/figure is open and the
                    # decision has not yet been made this edit session
                    # (st_shared_notice above already carries the fixed
                    # header text; this list is the "readable usage list").
                    # "Aggiorna ovunque" unlocks in-place editing (Save then
                    # performs the audited override); "Crea una copia
                    # locale" acts immediately, forking just this block.
                    st_reuse_impact_md = gr.Markdown(visible=False)
                    with gr.Row():
                        st_update_everywhere_btn = gr.Button("Aggiorna ovunque", visible=False)
                        st_fork_local_btn = gr.Button("Crea una copia locale", visible=False)
                    st_restore_draft_btn = gr.Button("Ripristina bozza", visible=False)
                    st_discard_draft_btn = gr.Button("Scarta bozza", visible=False)
                    with gr.Row():
                        st_save_btn = gr.Button("Salva", variant="primary", visible=False, interactive=False)
                        st_discard_edits_btn = gr.Button("Scarta modifiche", visible=False, interactive=False)
                    st_save_msg = gr.Markdown()
                    with gr.Row():
                        st_move_up_btn = gr.Button("↑ Sposta su", visible=False, interactive=False)
                        st_move_down_btn = gr.Button("↓ Sposta giù", visible=False, interactive=False)
                        st_archive_btn = gr.Button("Archivia", visible=False, interactive=False)
                        st_restore_btn = gr.Button("Ripristina", visible=False, interactive=False)
                    st_submit_review_btn = gr.Button("Invia in revisione", visible=False, interactive=False)
                    st_review_msg = gr.Markdown()
                    with gr.Row(visible=False) as st_new_block_row:
                        st_new_block_type = gr.Dropdown(["paragraph", "note", "warning", "list_item"], value="paragraph", label="Nuovo blocco — tipo")
                        st_new_block_btn = gr.Button("+ Aggiungi blocco in fondo al topic")
                    with gr.Accordion("Dettagli tecnici (modalità avanzata)", open=False):
                        st_tech_html = gr.HTML()
                        gr.Markdown("**Struttura (tabella tecnica, per diagnostica)**")
                        st_outline_table = gr.Dataframe(interactive=False, wrap=True, max_height=360)
                        gr.Markdown("**Storico versioni**")
                        st_history_table = gr.Dataframe(interactive=False, max_height=240)
                        # 4D: revision compare -- read-only, never mutates
                        # anything (see studio_compare_ui). "Confronta con
                        # versione precedente" is the always-available
                        # one-click case (diffs against version-1, doubling
                        # as the pre-revert preview); the number field lets
                        # you compare against any other historical version.
                        with gr.Row(visible=False) as st_compare_row:
                            st_compare_version = gr.Number(label="Confronta con versione", precision=0)
                            st_compare_btn = gr.Button("Confronta")
                            st_compare_prev_btn = gr.Button("Confronta con versione precedente")
                        st_diff_output = gr.Markdown()
                        with gr.Row():
                            st_revert_version = gr.Number(label="Versione da ripristinare", precision=0)
                            st_revert_btn = gr.Button("Ripristina questa versione")

            with gr.Accordion("Dettagli tecnici (modalità avanzata)",open=False):
                with gr.Row(): c_pdf=gr.File(label="Manuale corrente PDF",type='filepath'); c_lang=gr.Textbox(label="Lingua sorgente",value='en-US'); c_pub=gr.Textbox(label="Publication ID",value='PUB-MAIN'); c_title=gr.Textbox(label="Titolo")
                c_import=gr.Button("Importa come draft",variant='primary'); c_msg=gr.Markdown(); c_topics=gr.Dataframe(interactive=False); c_blocks=gr.Dataframe(interactive=False,max_height=500)
                with gr.Row(): c_reviewer=gr.Textbox(label="Revisore umano"); c_confirm=gr.Checkbox(label="Confermo di aver revisionato la sorgente")
                c_approve=gr.Button("Approva sorgente"); c_approve_msg=gr.Markdown(); c_refresh=gr.Button("Mostra oggetti approvati"); c_approved=gr.Dataframe(interactive=False,max_height=450)
                c_import.click(import_manual_ui,[project_state,c_pdf,c_lang,c_pub,c_title],[c_msg,c_topics,c_blocks]); c_approve.click(approve_source_ui,[project_state,c_pub,c_reviewer,c_confirm],[c_approve_msg]); c_refresh.click(approved_objects_ui,[project_state],[c_approved])
                with gr.Accordion("AI Grounded Authoring",open=False):
                    gr.Markdown("Le proposte possono usare soltanto fonti indicizzate; i conflitti o le affermazioni non supportate vengono bloccati.")
                    with gr.Row(): g_pdf=gr.File(label='Fonte tecnica PDF',type='filepath'); g_text=gr.Textbox(label='Oppure fonte testuale',lines=4); g_doc=gr.Textbox(label='Document ID'); g_title=gr.Textbox(label='Titolo fonte')
                    g_ingest=gr.Button('Indicizza fonte'); g_ingest_msg=gr.Markdown(); g_chunks=gr.Dataframe(interactive=False,max_height=350)
                    with gr.Row(): g_instruction=gr.Textbox(label='Richiesta'); g_mode=gr.Dropdown(['paragraph','procedure','warning','summary','rewrite'],value='paragraph',label='Tipo')
                    g_generate=gr.Button('Genera proposta grounded',variant='primary'); g_msg=gr.Markdown(); g_draft=gr.Textbox(label='Proposta',lines=8); g_evidence=gr.Dataframe(interactive=False,max_height=350); g_claims=gr.Dataframe(interactive=False); g_pid=gr.Textbox(label='Proposal ID')
                    with gr.Row(): g_topic=gr.Textbox(label='Topic ID'); g_block=gr.Textbox(label='Nuovo Block ID'); g_btype=gr.Dropdown(['paragraph','warning','note','list_item'],value='paragraph',label='Tipo blocco'); g_pos=gr.Number(label='Posizione',value=1,precision=0); g_reviewer=gr.Textbox(label='Revisore')
                    g_queue=gr.Button('Invia in review umana'); g_queue_msg=gr.Markdown(); g_ingest.click(grounding_ingest_ui,[project_state,g_pdf,g_text,g_doc,g_title],[g_ingest_msg,g_chunks]); g_generate.click(grounded_generate_ui,[project_state,g_instruction,g_mode],[g_msg,g_draft,g_evidence,g_claims,g_pid]); g_queue.click(grounded_queue_ui,[project_state,g_pid,g_topic,g_block,g_btype,g_pos,g_reviewer],[g_queue_msg])
                with gr.Accordion("Coda di revisione",open=False):
                    gr.Markdown("Le proposte in Draft/In Review restano bloccate finché un revisore umano non decide qui. (Milestone 4B.1: prima UI reale collegata a decide_review().)")
                    rv_refresh=gr.Button("Aggiorna coda"); rv_queue=gr.Dataframe(interactive=False,max_height=300)
                    with gr.Row(): rv_review_id=gr.Number(label="Review ID",precision=0); rv_decision=gr.Radio(["APPROVE","REJECT"],value="APPROVE",label="Decisione"); rv_actor=gr.Textbox(label="Revisore")
                    rv_comment=gr.Textbox(label="Commento — opzionale")
                    rv_decide=gr.Button("Decidi",variant="primary"); rv_msg=gr.Markdown()
                    rv_refresh.click(review_queue_ui,[project_state],[rv_queue])
                    rv_decide.click(review_decide_ui,[project_state,rv_review_id,rv_decision,rv_actor,rv_comment],[rv_msg,rv_queue])

            # ----------------------------------------------------------------
            # Milestone 4B.2 Studio wiring -- deferred to here so it can reuse
            # c_pub (the Publication ID field already defined above in this
            # same tab's advanced section) instead of declaring a second
            # "which document is open" concept.
            # ----------------------------------------------------------------
            ST_OUT_FULL = [st_outline_table, st_outline_radio, st_kind, st_id, st_version, st_breadcrumb, st_status_html,
                          st_title_input, st_text_input, st_readonly_notice, st_save_btn, st_discard_edits_btn,
                          st_restore_draft_btn, st_discard_draft_btn, st_move_up_btn, st_move_down_btn,
                          st_archive_btn, st_restore_btn, st_submit_review_btn, st_review_msg, st_new_block_row,
                          st_tech_html, st_history_table, st_save_msg, st_pub_header,
                          st_block_type, st_object_ref_id,
                          st_table_title, st_table_caption, st_table_grid_view, st_table_grid_edit,
                          st_figure_preview, st_figure_caption, st_figure_alt, st_figure_replace_row,
                          st_shared_notice, st_new_table_row, st_new_figure_row, st_table_rowcol_row,
                          st_shared_confirmed, st_shared_impact_token, st_reuse_impact_md, st_update_everywhere_btn, st_fork_local_btn,
                          st_compare_row, st_diff_output]
            authoring_tab.select(studio_load_ui, [project_state, c_pub, st_show_archived], ST_OUT_FULL)
            st_refresh_btn.click(studio_refresh_ui, [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL)
            st_show_archived.change(studio_refresh_ui, [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL)
            st_outline_radio.change(studio_open_ui, [project_state, c_pub, st_outline_radio, st_show_archived], ST_OUT_FULL)

            # 4C: draft recovery now also covers the table/figure editors --
            # any of these fields blurring/changing saves a draft the same
            # blur-triggered way title/text already did, keyed by
            # (kind/block_type -> table_id/figure_id when applicable, see
            # _studio_draft_target()), never creating a CCMS version.
            _ST_DRAFT_INPUTS = [project_state, st_kind, st_id, st_block_type, st_object_ref_id, st_version,
                                st_title_input, st_text_input, st_table_title, st_table_caption, st_table_grid_edit,
                                st_figure_caption, st_figure_alt, actor_state]
            st_title_input.blur(studio_save_draft_ui, _ST_DRAFT_INPUTS, [st_save_msg])
            st_text_input.blur(studio_save_draft_ui, _ST_DRAFT_INPUTS, [st_save_msg])
            st_table_title.blur(studio_save_draft_ui, _ST_DRAFT_INPUTS, [st_save_msg])
            st_table_caption.blur(studio_save_draft_ui, _ST_DRAFT_INPUTS, [st_save_msg])
            # .input() (not .change()): Gradio fires .change() for BOTH a
            # genuine user edit AND a programmatic gr.update(value=...) --
            # confirmed the hard way (a freshly created/opened table's own
            # grid-population push was silently writing a spurious draft
            # via .change()). .input() fires ONLY on a real user edit, the
            # same guarantee .blur() already gives every other draft-saved
            # field here. Bound to st_table_grid_edit only -- st_table_
            # grid_view is permanently interactive=False, so it never fires
            # .input() at all.
            st_table_grid_edit.input(studio_save_draft_ui, _ST_DRAFT_INPUTS, [st_save_msg])
            st_figure_caption.blur(studio_save_draft_ui, _ST_DRAFT_INPUTS, [st_save_msg])
            st_figure_alt.blur(studio_save_draft_ui, _ST_DRAFT_INPUTS, [st_save_msg])

            # Add/remove row/column -- see _table_rowcol_op: replaces
            # reliance on the Dataframe's own native right-click menu,
            # which does not reliably sync to what Save submits. Only ever
            # relevant while st_table_grid_edit is the visible one (these
            # buttons are only shown when can_edit is True).
            _TABLE_ROWCOL_INPUTS = [project_state, st_kind, st_id, st_block_type, st_object_ref_id, st_version,
                                    st_table_title, st_table_caption, st_table_grid_edit, actor_state]
            st_table_add_row_btn.click(studio_table_add_row_ui, _TABLE_ROWCOL_INPUTS, [st_table_grid_edit, st_save_msg])
            st_table_remove_row_btn.click(studio_table_remove_row_ui, _TABLE_ROWCOL_INPUTS, [st_table_grid_edit, st_save_msg])
            st_table_add_col_btn.click(studio_table_add_col_ui, _TABLE_ROWCOL_INPUTS, [st_table_grid_edit, st_save_msg])
            st_table_remove_col_btn.click(studio_table_remove_col_ui, _TABLE_ROWCOL_INPUTS, [st_table_grid_edit, st_save_msg])

            st_restore_draft_btn.click(studio_restore_draft_ui, [project_state, st_kind, st_id, st_block_type, st_object_ref_id],
                                       [st_title_input, st_text_input, st_table_title, st_table_caption, st_table_grid_edit,
                                        st_figure_caption, st_figure_alt, st_restore_draft_btn, st_discard_draft_btn])
            st_discard_draft_btn.click(studio_discard_draft_ui,
                                       [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id, st_show_archived], ST_OUT_FULL)
            st_discard_edits_btn.click(studio_discard_edits_ui, [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL)
            st_save_btn.click(studio_save_ui,
                              [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id, st_version,
                               st_title_input, st_text_input, st_table_title, st_table_caption, st_table_grid_edit,
                               st_figure_caption, st_figure_alt, actor_state, st_show_archived, st_shared_confirmed,
                               st_shared_impact_token], ST_OUT_FULL)

            # 4D reuse-aware shared-object decision -- see
            # studio_confirm_shared_update_ui/studio_fork_local_ui.
            st_update_everywhere_btn.click(studio_confirm_shared_update_ui,
                                           [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL)
            st_fork_local_btn.click(studio_fork_local_ui,
                                    [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id,
                                     st_version, st_show_archived, actor_state], ST_OUT_FULL)

            st_move_up_btn.click(lambda pd_, pub, k, i, sa, ac: studio_move_ui(pd_, pub, k, i, "up", sa, ac),
                                 [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL)
            st_move_down_btn.click(lambda pd_, pub, k, i, sa, ac: studio_move_ui(pd_, pub, k, i, "down", sa, ac),
                                   [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL)
            st_archive_btn.click(studio_archive_ui, [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL)
            st_restore_btn.click(studio_restore_obj_ui, [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL)
            st_prev_btn.click(lambda pd_, pub, k, i, sa: studio_next_prev_ui(pd_, pub, k, i, "prev", sa),
                              [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL)
            st_next_btn.click(lambda pd_, pub, k, i, sa: studio_next_prev_ui(pd_, pub, k, i, "next", sa),
                              [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL)

            st_new_topic_btn.click(studio_new_topic_ui, [project_state, c_pub, st_new_topic_title, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL)
            st_new_block_btn.click(studio_new_block_ui, [project_state, c_pub, st_kind, st_id, st_new_block_type, st_show_archived, actor_state], ST_OUT_FULL)
            st_new_table_btn.click(studio_new_table_ui, [project_state, c_pub, st_kind, st_id, st_new_table_caption, st_show_archived, actor_state], ST_OUT_FULL)
            st_new_figure_btn.click(studio_new_figure_ui,
                                    [project_state, c_pub, st_kind, st_id, st_new_figure_file, st_new_figure_caption,
                                     st_new_figure_alt, st_show_archived, actor_state], ST_OUT_FULL)
            st_figure_replace_btn.click(studio_replace_figure_ui,
                                        [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id,
                                         st_version, st_figure_replace_file, st_show_archived, actor_state,
                                         st_shared_confirmed, st_shared_impact_token], ST_OUT_FULL)
            st_submit_review_btn.click(studio_submit_review_ui, [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL)
            st_revert_btn.click(studio_revert_ui,
                                [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id,
                                 st_revert_version, st_show_archived, actor_state, st_shared_confirmed,
                                 st_shared_impact_token], ST_OUT_FULL)

            # 4D revision compare -- read-only, no ST_OUT_FULL re-render
            # (see studio_compare_ui): only st_diff_output changes.
            st_compare_btn.click(studio_compare_ui,
                                 [project_state, st_kind, st_id, st_block_type, st_object_ref_id,
                                  st_version, st_compare_version], [st_diff_output])
            st_compare_prev_btn.click(studio_compare_prev_ui,
                                      [project_state, st_kind, st_id, st_block_type, st_object_ref_id, st_version],
                                      [st_diff_output])

            st_search_btn.click(studio_search_ui, [project_state, c_pub, st_search_box], [st_search_picker, st_search_open_btn, st_search_msg])
            st_search_open_btn.click(studio_search_open_ui, [project_state, c_pub, st_search_picker, st_show_archived], ST_OUT_FULL)

        with gr.Tab("Compliance", id="compliance") as cp_tab:
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workflow</div><h1>Compliance</h1>"
                    "<p>Stato della documentazione rispetto ai requisiti normativi configurati per questa "
                    "pubblicazione — mai una dichiarazione automatica di conformità.</p></div></div>")
            with gr.Row():
                cp_pub = gr.Textbox(label="Publication ID", value="PUB-MAIN")
                cp_scope = gr.Dropdown(label="Scope compliance", choices=[], value=None)
                cp_reload = gr.Button("Aggiorna", variant="primary")
            cp_dashboard = gr.HTML()
            gr.HTML("<div class='ms-section-title'>Requisiti</div><div class='ms-section-sub'>Ordinati per priorità: "
                    "prima ciò che manca, poi ciò che va verificato.</div>")
            cp_table = gr.Dataframe(interactive=False, wrap=True, max_height=380)
            cp_req_picker = gr.Dropdown(label="Apri un requisito", choices=[], value=None)
            with gr.Column(elem_classes=["ms-panel"]):
                cp_header = gr.HTML()
                with gr.Row():
                    with gr.Column():
                        gr.HTML("<div class='ms-section-title'>Applicabilità</div>")
                        cp_applic_current = gr.HTML()
                        cp_applic_radio = gr.Radio(["Si applica", "Non applicabile", "Da verificare"],
                                                   label="Decisione", value=None)
                        cp_applic_justification = gr.Textbox(label="Motivazione — obbligatoria solo per «Non applicabile»")
                        cp_applic_reviewer = gr.Textbox(label="Il tuo nome")
                        cp_applic_comment = gr.Textbox(label="Commento — opzionale")
                        cp_applic_confirm = gr.Button("Conferma applicabilità", variant="primary")
                        cp_applic_msg = gr.Markdown()
                    with gr.Column():
                        gr.HTML("<div class='ms-section-title'>Copertura</div>")
                        cp_cov_banner = gr.HTML()
                        cp_cov_current = gr.HTML()
                        cp_cov_radio = gr.Radio(["Coperto", "Da verificare", "Mancante"], label="Decisione", value=None)
                        cp_cov_reviewer = gr.Textbox(label="Il tuo nome")
                        cp_cov_comment = gr.Textbox(label="Commento — opzionale")
                        cp_cov_confirm = gr.Button("Conferma copertura", variant="primary")
                        cp_cov_msg = gr.Markdown()
                gr.HTML("<div class='ms-section-title'>Evidenze collegate</div><div class='ms-section-sub'>Contenuto "
                        "ManualSync che dimostra la copertura di questo requisito.</div>")
                cp_evidence_table = gr.Dataframe(interactive=False, wrap=True, max_height=260)
                with gr.Row():
                    cp_evidence_candidates = gr.Dropdown(label="Collega un contenuto esistente come evidenza",
                                                         choices=[], value=None)
                    cp_evidence_reviewer = gr.Textbox(label="Il tuo nome")
                    cp_evidence_link_btn = gr.Button("Collega come evidenza")
                with gr.Row():
                    cp_evidence_unlink_dd = gr.Dropdown(label="Scollega un'evidenza già collegata", choices=[], value=None)
                    cp_evidence_unlink_reviewer = gr.Textbox(label="Il tuo nome")
                    cp_evidence_unlink_btn = gr.Button("Scollega evidenza")
                cp_evidence_msg = gr.Markdown()
                with gr.Accordion("Dettagli tecnici (modalità avanzata)", open=False):
                    cp_advanced = gr.HTML()
            cp_open_rid = gr.State("")

            cp_out_full = [cp_dashboard, cp_table, cp_req_picker, cp_header, cp_applic_current, cp_applic_radio,
                          cp_cov_banner, cp_cov_current, cp_cov_radio, cp_evidence_table, cp_evidence_candidates,
                          cp_evidence_unlink_dd, cp_advanced, cp_open_rid, cp_applic_msg, cp_cov_msg, cp_evidence_msg]
            cp_reload.click(compliance_load_ui, [project_state, cp_pub], [cp_scope, *cp_out_full])
            cp_scope.change(compliance_switch_scope_ui, [project_state, cp_scope], cp_out_full)
            cp_req_picker.change(compliance_open_requirement_ui, [project_state, cp_scope, cp_req_picker], cp_out_full)
            cp_applic_confirm.click(compliance_decide_applicability_ui,
                                    [project_state, cp_scope, cp_open_rid, cp_applic_radio, cp_applic_reviewer,
                                     cp_applic_justification, cp_applic_comment, cp_cov_radio], cp_out_full)
            cp_cov_confirm.click(compliance_decide_coverage_ui,
                                 [project_state, cp_scope, cp_open_rid, cp_cov_radio, cp_cov_reviewer, cp_cov_comment,
                                  cp_applic_radio],
                                 cp_out_full)
            cp_evidence_link_btn.click(compliance_link_evidence_ui,
                                       [project_state, cp_scope, cp_open_rid, cp_evidence_candidates, cp_evidence_reviewer,
                                        cp_applic_radio, cp_cov_radio],
                                       cp_out_full)
            cp_evidence_unlink_btn.click(compliance_unlink_evidence_ui,
                                         [project_state, cp_scope, cp_open_rid, cp_evidence_unlink_dd,
                                          cp_evidence_unlink_reviewer, cp_applic_radio, cp_cov_radio], cp_out_full)
            # Acceptance fix: this tab must never depend on the user remembering
            # to press "Aggiorna". Whenever it becomes the selected tab -- first
            # open, Home -> Compliance, or after a client reconnect that resets
            # this session's component values back to their construction-time
            # defaults (empty scope/table/picker) -- reload for real from
            # ComplianceService, the same call "Aggiorna" makes. This is what
            # actually fixes "scope/table/picker revert to empty defaults after
            # navigating away and back": that reset happens to the CLIENT-SIDE
            # component values on any session/reconnect boundary Gradio itself
            # manages: nothing here can preserve last-shown data across that
            # boundary, so the tab must always refresh itself the moment it is
            # looked at again, rather than trusting stale client state.
            cp_tab.select(compliance_load_ui, [project_state, cp_pub], [cp_scope, *cp_out_full])

        with gr.Tab("Traduzioni",id="translations"):
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workflow</div><h1>Traduzioni</h1><p>Pacchetti lingua, traduzioni, QA e passaggio ai fornitori.</p></div></div><div class='ms-stepper'><div class='ms-step active'><span class='ms-step-num'>1</span>Pacchetti</div><div class='ms-step'><span class='ms-step-num'>2</span>Traduzioni</div><div class='ms-step'><span class='ms-step-num'>3</span>QA</div><div class='ms-step'><span class='ms-step-num'>4</span>Pronto per release</div></div>")
            gr.HTML("<div class='ms-panel'><div class='ms-section-title'>Pacchetti lingua</div><div class='ms-section-sub'>Riuso TM, ripetizioni esatte e codici non traducibili restano fuori dal workfile; solo il lavoro linguistico reale entra nell’XLIFF controllato.</div></div>")
            with gr.Row(): t_kind=gr.Dropdown(['publication','topic','block','table','figure','component'],value='publication',label='Tipo'); t_id=gr.Textbox(label='Object ID'); t_tms=gr.File(label='TM SQLite esterna — opzionale',type='filepath',file_count='multiple')
            t_btn=gr.Button('Genera pacchetti lingua',variant='primary'); t_msg=gr.Markdown(); t_table=gr.Dataframe(interactive=False,wrap=True,visible=False)
            with gr.Row(): t_master=gr.File(label='Master package'); t_files=gr.Files(label='Pacchetti lingua')
            t_btn.click(work_package_ui,[project_state,t_id,t_kind,t_tms],[t_msg,t_table,t_master,t_files])

        with gr.Tab("Job & fornitori",id="jobs"):
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workflow</div><h1>Job & fornitori</h1><p>Invio, ritorno XLIFF, controllo qualità e approvazione umana.</p></div></div>")
            with gr.Accordion("Fornitori",open=False):
                with gr.Row(): w_vid=gr.Textbox(label='Vendor ID'); w_name=gr.Textbox(label='Fornitore'); w_email=gr.Textbox(label='Email'); w_vsave=gr.Button('Salva fornitore')
                w_vendors=gr.Dataframe(interactive=False,visible=False); w_vsave.click(vendor_ui,[project_state,w_vid,w_name,w_email],[w_vendors])
            with gr.Row(): w_pkg=gr.File(label='Pacchetto lingua',type='filepath',file_types=['.zip']); w_vendor=gr.Textbox(label='Vendor ID — opzionale (solo se registrato)'); w_due=gr.Textbox(label='Scadenza YYYY-MM-DD')
            with gr.Row(): w_create=gr.Button('Crea job',variant='primary'); w_refresh=gr.Button('Aggiorna stato')
            w_msg=gr.Markdown(); w_jobs=gr.Dataframe(interactive=False,max_height=550,visible=False); w_events=gr.Dataframe(interactive=False,max_height=400,visible=False); w_create.click(create_job_ui,[project_state,w_pkg,w_vendor,w_due],[w_msg,w_jobs]); w_refresh.click(workflow_refresh_ui,[project_state],[w_jobs,w_vendors,w_events])
            gr.HTML("<div class='ms-panel' style='margin-top:14px'><div class='ms-section-title'>Avanza un job</div><div class='ms-section-sub'>QA = controllo qualità. Traduzioni incomplete o tecnicamente incoerenti vengono bloccate.</div></div>")
            with gr.Row(): w_job=gr.Textbox(label='Job ID'); w_action=gr.Dropdown(['Segna inviato','Segna in traduzione','Ricevi XLIFF + controllo qualità (QA)','Riesegui controllo qualità (QA)','Approva traduzione','Annulla'],label='Azione')
            with gr.Row(): w_return=gr.File(label='XLIFF restituito',type='filepath',file_types=['.xlf','.xliff']); w_term=gr.File(label='Terminologia approvata — opzionale',type='filepath',file_types=['.csv','.tbx','.xml']); w_strict=gr.Checkbox(label='Terminologia strict')
            with gr.Row(): w_reviewer=gr.Textbox(label='Revisore umano'); w_human=gr.Checkbox(label='Confermo approvazione umana'); w_apply=gr.Button('Esegui azione',variant='primary')
            w_action_msg=gr.Markdown(); w_qa=gr.Dataframe(interactive=False,max_height=450,visible=False); w_approval=gr.File(label='Approval audit'); w_tm=gr.File(label='Confirmed TM'); w_apply.click(job_action_ui,[project_state,w_job,w_action,w_return,w_term,w_strict,w_reviewer,w_human],[w_action_msg,w_jobs,w_events,w_qa,w_approval,w_tm])

        with gr.Tab("Release",id="release"):
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workflow</div><h1>Release</h1><p>Verifica la readiness e crea una release controllata.</p></div></div><div class='ms-stepper'><div class='ms-step active'><span class='ms-step-num'>1</span>Readiness</div><div class='ms-step'><span class='ms-step-num'>2</span>Dettagli release</div><div class='ms-step'><span class='ms-step-num'>3</span>Genera pacchetto</div><div class='ms-step'><span class='ms-step-num'>4</span>Riepilogo</div></div>")
            with gr.Row(): r_pub=gr.Textbox(label='Publication ID',value='PUB-MAIN'); r_langs=gr.Textbox(label='Lingue target',placeholder='it-IT, de-DE'); r_profile=gr.Textbox(label='Profilo — opzionale')
            r_check=gr.Button('Verifica readiness',variant='primary'); r_summary=gr.HTML(); r_matrix=gr.Dataframe(interactive=False,wrap=True,visible=False); r_blockers=gr.Dataframe(label='Elementi che bloccano la release',interactive=False,wrap=True,visible=False); r_json=gr.File(label='Readiness audit')
            gr.HTML("<div class='ms-panel' style='margin-top:14px'><div class='ms-section-title'>Crea release controllata</div><div class='ms-section-sub'>Scegli modalità, responsabile e identificativo. I gate R18 restano invariati.</div></div>")
            r_mode=gr.Radio(['Collaudo interno','Produzione cliente'],value='Collaudo interno',label='Tipo release'); r_mode_help=gr.HTML(_release_mode_help('Collaudo interno'))
            with gr.Row(): r_by=gr.Textbox(label='Release manager'); r_id=gr.Textbox(label='Release ID — opzionale'); r_idml=gr.File(label='Template IDML — opzionale',type='filepath',file_types=['.idml'])
            r_release=gr.Button('Crea release',variant='primary',interactive=False); r_msg=gr.Markdown(); r_zip=gr.File(label='Release package'); r_artifacts=gr.Dataframe(interactive=False,visible=False)
            r_check.click(readiness_ui,[project_state,r_pub,r_langs,r_profile,r_mode],[r_summary,r_matrix,r_blockers,r_json,r_release]); r_mode.change(_release_mode_reset,[r_mode],[r_mode_help,r_release,r_msg,r_zip,r_artifacts]); r_release.click(release_ui,[project_state,r_pub,r_langs,r_by,r_id,r_profile,r_idml,r_mode],[r_msg,r_zip,r_artifacts])

        with gr.Tab("Impostazioni",id="settings"):
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Sistema</div><h1>Impostazioni</h1><p>Gestione progetto, backup e informazioni della build.</p></div></div>")
            with gr.Row():
                with gr.Column(elem_classes=["ms-settings-card"]):
                    gr.HTML("<div class='ms-section-title'>Backup e ripristino</div><div class='ms-section-sub'>Esporta il progetto attivo o riapri un archivio ManualSync verificato.</div>")
                    p_archive=gr.File(label="Archivio progetto .zip",type="filepath",file_types=['.zip'])
                    with gr.Row():
                        p_restore=gr.Button("Riapri archivio")
                        p_export=gr.Button("Esporta backup progetto",variant="primary")
                    p_export_file=gr.File(label="Backup")
                    p_settings_msg=gr.Markdown()
                with gr.Column(elem_classes=["ms-settings-card"]):
                    gr.HTML(f"<div class='ms-section-title'>Informazioni prodotto</div><div class='ms-section-sub'>Il nome è ancora provvisorio durante il rebrand.</div><div style='font-size:12px;color:#475467;line-height:1.7'><b>{BRAND_NAME}</b><br>{BRAND_TAGLINE}<br><span style='color:#98a2b3'>Build: {PRODUCT_BUILD}</span><br><span style='color:#98a2b3'>Core baseline: R18</span></div>")
                    p_refresh=gr.Button("Aggiorna dashboard")
                    p_health=gr.Button("Verifica integrità locale")
                    p_health_msg=gr.Markdown()
                    p_health_table=gr.Dataframe(interactive=False,visible=True)
                    p_support=gr.Button("Esporta diagnostica assistenza")
                    p_support_file=gr.File(label="Pacchetto diagnostico")
                    p_support_msg=gr.Markdown()
            p_restore.click(restore_project_ui,[p_archive],[project_state,project_card,sidebar_context,p_settings_msg])
            p_export.click(export_project_ui,[project_state],[p_export_file])
            p_refresh.click(refresh_ui,[project_state],[project_card])
            p_health.click(diagnostics_ui,[project_state],[p_health_msg,p_health_table])
            p_support.click(support_bundle_ui,[project_state],[p_support_file,p_support_msg])

    _NAV_IDS=['home','revision','authoring','compliance','translations','jobs','release','settings']
    _NAV_BUTTONS=[nav_home,nav_revision,nav_authoring,nav_compliance,nav_translation,nav_jobs,nav_release,nav_settings]
    def _navigate(tab_id: str):
        updates=[gr.update(variant='primary' if item==tab_id else 'secondary') for item in _NAV_IDS]
        return (gr.Tabs(selected=tab_id), *updates)
    _nav_outputs=[workspace_tabs,*_NAV_BUTTONS]
    nav_home.click(lambda:_navigate('home'),None,_nav_outputs)
    nav_revision.click(lambda:_navigate('revision'),None,_nav_outputs)
    nav_authoring.click(lambda:_navigate('authoring'),None,_nav_outputs)
    nav_compliance.click(lambda:_navigate('compliance'),None,_nav_outputs)
    nav_translation.click(lambda:_navigate('translations'),None,_nav_outputs)
    nav_jobs.click(lambda:_navigate('jobs'),None,_nav_outputs)
    nav_release.click(lambda:_navigate('release'),None,_nav_outputs)
    nav_settings.click(lambda:_navigate('settings'),None,_nav_outputs)
    home_revision.click(lambda:_navigate('revision'),None,_nav_outputs)
    home_translation.click(lambda:_navigate('translations'),None,_nav_outputs)
    home_release.click(lambda:_navigate('release'),None,_nav_outputs)
    # Milestone 3A: "Rivedi il contenuto importato" opens the Contenuti tab, whose
    # DEFAULT view (c_simple_table) is now the friendly standard view — the
    # technical dataframes live only inside the collapsed "modalità avanzata"
    # accordion, so they're never the primary call-to-action.
    lg_review.click(lambda:(*_navigate('authoring'),gr.update(visible=False)),None,[*_nav_outputs,legacy_onboarding_panel])
    # Keeps the standard review table in sync with the SAME topics/blocks data
    # shown in "modalità avanzata" — fires after either the guided import or the
    # manual "Importa come draft" click, since both set c_blocks. No second backend.
    c_blocks.change(_simple_review_table_live_ui,[project_state,c_topics,c_blocks],[c_simple_table])
    # Same trigger as c_simple_table above (fires after guided import or manual
    # "Importa come draft") -- keeps "Elementi da verificare" in sync with
    # whatever the most recent import actually produced.
    c_blocks.change(_idml_review_panel_ui,[project_state],[c_idml_review_html,c_idml_review_row]+c_idml_review_radios)
    c_idml_review_confirm.click(idml_review_confirm_ui,[project_state,actor_state]+c_idml_review_radios,
                                 [c_idml_review_html,c_idml_review_row]+c_idml_review_radios,
                                 trigger_mode="once",concurrency_limit=1)
    c_simple_approve.click(guided_approve_source_ui,[project_state,actor_state,c_simple_confirm],[c_simple_msg,project_card,sidebar_context,c_simple_table],trigger_mode="once",concurrency_limit=1)
    _guided_outputs=[project_state,project_card,sidebar_context,p_msg,
                     onboarding_choice_panel,legacy_onboarding_panel,lg_review,
                     lg_lang_prompt,lg_lang_pick,lg_lang_confirm,
                     c_topics,c_blocks,c_pdf,c_lang,c_pub,c_title]
    lg_submit.click(guided_new_project_from_pdf_ui,[lg_name,lg_pdf],_guided_outputs,scroll_to_output=True,trigger_mode="once",concurrency_limit=1)
    lg_lang_confirm.click(guided_confirm_language_ui,[lg_name,lg_pdf,lg_lang_pick],_guided_outputs,scroll_to_output=True,trigger_mode="once",concurrency_limit=1)

if __name__=='__main__':
    demo.launch(server_name='127.0.0.1',server_port=7860,inbrowser=True,show_error=False,theme=gr.themes.Base(),css=CSS,favicon_path=str(Path(__file__).with_name('manualsync.png')) if Path(__file__).with_name('manualsync.png').exists() else None,allowed_paths=[str(DATA_ROOT.resolve())],footer_links=[])
