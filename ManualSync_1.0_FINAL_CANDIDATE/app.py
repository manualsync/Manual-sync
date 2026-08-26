from __future__ import annotations

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
from authoring_ccms import import_pdf_as_topics, import_docx_as_topics
from ccms_industrial import IndustrialRepository, upgrade_legacy_topics_to_blocks
from translation_work_package import generate_translation_work_packages
from cat_tm import TranslationMemory, source_target_safe
from workflow_management import WorkflowManager
from release_readiness import assess_release_readiness, export_readiness_report
from release_manager import create_release_bundle
from grounded_authoring import GroundingStore
from generative_orchestrator import GenerationLedger, GenerationRequest, GenerativeOrchestrator, SafeSourceComposer
from project_lifecycle import create_project, read_project, update_project, project_health
from support_diagnostics import system_health, create_support_bundle

from product_version import PRODUCT_BUILD
from language_codes import canonical_lang, canonical_langs

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

def import_manual_ui(project_dir, pdf, source_lang, publication_id, title):
    if not project_dir or not pdf:
        raise gr.Error("Apri un progetto e carica il manuale corrente.")
    pub=(publication_id or 'PUB-MAIN').strip()
    source=(source_lang or read_project(project_dir).get('source_lang','en-US')).strip()
    srcdir=Path(project_dir)/'Sources'; srcdir.mkdir(exist_ok=True)
    copied=srcdir/Path(pdf).name; shutil.copy2(pdf,copied)
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        # Re-import is allowed only while the publication is still a migration draft.
        # This prevents duplicate/orphaned topic trees during iterative migration tests.
        if repo.conn.execute("SELECT 1 FROM publications WHERE publication_id=?",(pub,)).fetchone():
            linked=list(repo.conn.execute(
                "SELECT t.topic_id,t.status FROM topics t JOIN publication_topics pt ON pt.topic_id=t.topic_id WHERE pt.publication_id=?",
                (pub,)
            ))
            if linked and any(str(r["status"]).lower() != "migrated" for r in linked):
                raise ValueError("La pubblicazione contiene contenuti già revisionati/approvati. Usa un nuovo Publication ID per una nuova migrazione.")
            tids_old=[str(r["topic_id"]) for r in linked]
            table_ids=[]
            if tids_old:
                marks=",".join("?" for _ in tids_old)
                table_ids=[str(r[0]) for r in repo.conn.execute(
                    f"SELECT object_ref_id FROM content_blocks WHERE topic_id IN ({marks}) AND block_type='table' AND object_ref_id<>''",
                    tids_old
                )]
                repo.conn.execute("DELETE FROM publication_topics WHERE publication_id=?",(pub,))
                repo.conn.executemany("DELETE FROM topics WHERE topic_id=?",[(tid,) for tid in tids_old])
                if table_ids:
                    repo.conn.executemany("DELETE FROM structured_tables WHERE table_id=?",[(tid,) for tid in table_ids])
                repo.conn.commit()

        if copied.suffix.lower()=='.docx':
            try:
                imported=import_docx_as_topics(repo,copied,source_lang=source,id_prefix='DOC',source_ref_prefix=copied.name)
            except ValueError:
                raise
            except Exception as exc:
                raise gr.Error("Il documento Word non è valido o è danneggiato. Verifica il file e riprova.") from exc
            if imported.empty:
                raise ValueError("Nessun topic ricostruito dal documento Word.")
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
    msg=(f"✅ Manuale importato come draft in **{len(imported)} topic semantici** e **{table_count} tabelle strutturate**. "
         f"Header ricorrenti esclusi: **{dropped}**. La migrazione da PDF resta conservativa: "
         "prima dell’approvazione serve revisione umana.")
    return msg, topics, blocks

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
        if filename.lower().endswith(".docx"):
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
        raise gr.Error("Carica un file PDF o Word (.docx) per continuare.")
    name = (project_name or "").strip() or Path(pdf_file).stem or "Nuovo manuale"
    if Path(pdf_file).suffix.lower() == ".docx":
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

def guided_approve_source_ui(project_dir, reviewer, confirmed):
    # Milestone 3A: simplified approval entry point for the standard Contenuti view.
    # Reuses approve_source_ui exactly as already tested. `confirmed` is the
    # user's own explicit checkbox value — never hardcoded — so approve_source_ui's
    # existing fail-closed check ("Serve la conferma esplicita...") still applies.
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    msg = approve_source_ui(project_dir, "PUB-MAIN", reviewer, confirmed)
    return msg, _project_card(project_dir), _sidebar_context(project_dir), _simple_review_table_for_project(project_dir)

def approve_source_ui(project_dir, publication_id, reviewer, confirmed):
    if not confirmed: raise gr.Error("Serve la conferma esplicita della revisione umana.")
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        tids=[r[0] for r in repo.conn.execute("SELECT topic_id FROM publication_topics WHERE publication_id=?",(publication_id,))]
        if not tids: raise ValueError("Pubblicazione non trovata o vuota.")
        for tid in tids:
            repo.conn.execute("UPDATE topics SET status='Approved' WHERE topic_id=?",(tid,))
            repo.conn.execute("UPDATE content_blocks SET status='Approved' WHERE topic_id=?",(tid,))
        repo._audit('publication',publication_id,'SOURCE_HUMAN_APPROVED',reviewer or 'Human reviewer',{'topics':len(tids)})
        repo.conn.commit()
    finally: repo.close()
    return "✅ Sorgente approvata dal revisore umano."

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
    return (
        f"<div class='ms-card'><h3>{msg}</h3><p>{detail}</p></div>",
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
    return msg, str(r['release_zip']), gr.update(value=_friendly_release_artifacts(r['artifacts']), visible=True)

with gr.Blocks(title=BRAND_NAME) as demo:
    project_state=gr.State(INITIAL_PROJECT)
    with gr.Sidebar(width=236, elem_id="ms-sidebar"):
        gr.HTML(f"<div class='ms-brand'><div class='ms-logo'>M</div><div><div class='ms-brand-title'>{BRAND_NAME}</div><div class='ms-brand-sub'>Brand provvisorio · Technical Content Intelligence</div></div></div>")
        nav_home=gr.Button("⌂   Home",variant="primary")
        gr.HTML("<div class='ms-side-section'>Workflow</div>")
        nav_revision=gr.Button("◫   Revisione",variant="secondary")
        nav_authoring=gr.Button("✎   Contenuti",variant="secondary")
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
            with gr.Column(visible=False,elem_id="guided-legacy-onboarding-panel",elem_classes=["ms-panel"]) as legacy_onboarding_panel:
                gr.HTML("<div class='ms-section-title'>Importa quello che hai già</div><div class='ms-section-sub'>Carica un PDF o un documento Word: creiamo il progetto e leggiamo il documento per te.</div>")
                lg_name=gr.Textbox(label="Nome progetto — opzionale, lo ricaviamo dal file se lo lasci vuoto")
                lg_pdf=gr.File(label="Il tuo manuale (PDF o Word .docx)",type='filepath',file_types=['.pdf','.docx'])
                with gr.Row():
                    gr.Button("InDesign (.idml) — non ancora disponibile in questo percorso guidato",interactive=False)
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

        with gr.Tab("Redazione & contenuti",id="authoring"):
            gr.HTML("<div class='ms-pagehead'><div><div class='ms-page-kicker'>Workflow</div><h1>Redazione & contenuti</h1><p>Repository strutturato e redazione AI grounded con revisione umana.</p></div></div>")
            # Milestone 3A: standard view, shown by default. Same topics/blocks data as
            # the technical dataframes below (no second backend) — just friendlier
            # columns. No ID/metadata/re-import button in this default view.
            gr.HTML("<div class='ms-section-title'>Contenuto importato</div><div class='ms-section-sub'>Queste sono le sezioni trovate nel documento. Per i dettagli tecnici, apri “Dettagli tecnici (modalità avanzata)” qui sotto.</div>")
            c_simple_table=gr.Dataframe(interactive=False,wrap=True,max_height=500)
            c_simple_confirm=gr.Checkbox(label="Confermo di aver revisionato il contenuto")
            with gr.Row(): c_simple_reviewer=gr.Textbox(label="Il tuo nome — opzionale"); c_simple_approve=gr.Button("Approva sorgente",variant="primary")
            c_simple_msg=gr.Markdown()
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

    _NAV_IDS=['home','revision','authoring','translations','jobs','release','settings']
    _NAV_BUTTONS=[nav_home,nav_revision,nav_authoring,nav_translation,nav_jobs,nav_release,nav_settings]
    def _navigate(tab_id: str):
        updates=[gr.update(variant='primary' if item==tab_id else 'secondary') for item in _NAV_IDS]
        return (gr.Tabs(selected=tab_id), *updates)
    _nav_outputs=[workspace_tabs,*_NAV_BUTTONS]
    nav_home.click(lambda:_navigate('home'),None,_nav_outputs)
    nav_revision.click(lambda:_navigate('revision'),None,_nav_outputs)
    nav_authoring.click(lambda:_navigate('authoring'),None,_nav_outputs)
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
    c_simple_approve.click(guided_approve_source_ui,[project_state,c_simple_reviewer,c_simple_confirm],[c_simple_msg,project_card,sidebar_context,c_simple_table],trigger_mode="once",concurrency_limit=1)
    _guided_outputs=[project_state,project_card,sidebar_context,p_msg,
                     onboarding_choice_panel,legacy_onboarding_panel,lg_review,
                     lg_lang_prompt,lg_lang_pick,lg_lang_confirm,
                     c_topics,c_blocks,c_pdf,c_lang,c_pub,c_title]
    lg_submit.click(guided_new_project_from_pdf_ui,[lg_name,lg_pdf],_guided_outputs,scroll_to_output=True,trigger_mode="once",concurrency_limit=1)
    lg_lang_confirm.click(guided_confirm_language_ui,[lg_name,lg_pdf,lg_lang_pick],_guided_outputs,scroll_to_output=True,trigger_mode="once",concurrency_limit=1)

if __name__=='__main__':
    demo.launch(server_name='127.0.0.1',server_port=7860,inbrowser=True,show_error=False,theme=gr.themes.Base(),css=CSS,favicon_path=str(Path(__file__).with_name('manualsync.png')) if Path(__file__).with_name('manualsync.png').exists() else None,allowed_paths=[str(DATA_ROOT.resolve())],footer_links=[])
