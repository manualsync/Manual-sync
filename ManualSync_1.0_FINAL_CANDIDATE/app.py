from __future__ import annotations

import contextlib
import html
import json
import os
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
import publishing_preview as pp
import publishing_output as po
import impact_intelligence as ii
import impact_view as iv
import translation_intelligence as ti
import translation_view as tv
import export_links
import rich_text
from translation_work_package import generate_translation_work_packages, TranslationFormatNotSupportedError
from cat_tm import TranslationMemory, source_target_safe
from workflow_management import WorkflowManager
from release_readiness import assess_release_readiness, export_readiness_report
from release_manager import create_release_bundle
from grounded_authoring import GroundingStore
from generative_orchestrator import GenerationLedger, GenerationRequest, GenerativeOrchestrator, SafeSourceComposer
from project_lifecycle import create_project, read_project, update_project, project_health
from support_diagnostics import system_health, create_support_bundle
import compliance_view as cv
import compliance_starter_packs as csp

from product_version import PRODUCT_BUILD
from language_codes import canonical_lang, canonical_langs
import domain_errors as de
import i18n
from i18n import t

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

# ----------------------------------------------------------------------
# 4E.3 stabilization (Blocker 1): the standard-mode rich-text editor's
# client-side sync/paste-safety script, passed as `js_on_load` to the
# st_rich_editable gr.HTML component (see its declaration for the full
# architecture rationale). `js_on_load` runs whenever this HTML element
# (re)loads -- the same mechanism Gradio's OWN default HTML click-
# forwarding behavior relies on to survive value updates, per gradio's
# own documented default: "element.addEventListener('click', function()
# { trigger('click') });" -- `element` and `trigger` are free variables
# Gradio provides in this exact scope; this script deliberately does not
# call `trigger(...)` at all (no click on the editable area should ever
# fire a Python callback -- normal editing needs no server round trip).
#
# What it does:
#  1. Finds the real contenteditable div (#ms-rich-editable-inner) and
#     the hidden canonical-text carrier (#ms-rich-text-input textarea).
#  2. serializeRoot(): walks the (already-constrained) DOM -- only
#     B/STRONG, I/EM, CODE, A, BR, UL>LI, OL>LI, and plain text ever
#     appear there (server-rendered content, toolbar-driven execCommand
#     edits, or plain typing -- see paste handling below) -- and
#     produces the canonical markdown-like string, escaping every
#     literal occurrence of the grammar's own delimiter characters
#     (escapeLiteral mirrors rich_text.py's escape_literal() exactly:
#     backslash, *, _, `, [, ] ) so a writer's literal "M8*1.25" or
#     "MAX_PRESSURE_LIMIT" survives round-trip exactly, never
#     reinterpreted as formatting (Blocker 3). An unrecognized element
#     (should not normally occur) is flattened to its text content only
#     -- fail-safe, never a raw stray tag reaching canonical storage.
#  3. On 'input': pushes the freshly serialized canonical text into the
#     hidden textarea's value and dispatches a REAL 'input' DOM event on
#     it (bubbles:true) -- the exact mechanism already proven for the
#     toolbar buttons below: Gradio's frontend listens for genuine DOM
#     events on a component's own element, synthetic or not, and syncs
#     its tracked value accordingly.
#  4. On 'blur': re-syncs (belt and braces) and forwards a synthetic
#     'blur' event to the hidden textarea too, so the EXISTING
#     st_text_input.blur(studio_save_draft_ui, ...) draft-autosave
#     wiring keeps working completely unchanged in Python -- the
#     contenteditable losing focus now stands in for the old textarea
#     losing focus.
#  5. On 'paste' (Blocker 5): preventDefault()'s the native paste
#     entirely and inserts ONLY the clipboard's PLAIN-TEXT payload via
#     execCommand('insertText', ...) -- meaning pasted HTML from Word/a
#     browser/anywhere NEVER touches the DOM at all: no <span>/<div>/
#     <script>/style/class/browser metadata can ever appear, because no
#     HTML from the clipboard is ever read in the first place. This is
#     the simplest possible safe normalization layer the ticket asks
#     for -- unsupported formatting is, by construction, always
#     flattened to visible text, and nothing executable is ever
#     inserted.
#  6. One-time (per load) execCommand('defaultParagraphSeparator','br')
#     so pressing Enter inserts a flat <br> instead of the browser's
#     default nested <div>-wrapping behavior, keeping the DOM shape the
#     serializer expects to see.
# ----------------------------------------------------------------------
# 4E.3 Save Architecture Replacement.
#
# The previous "Save/Version Regression Fix" made Save a two-step
# Gradio chain: step 1 (fn=None, js=...) re-serialized the live DOM into
# the HIDDEN st_text_input Textbox's OWN output slot, step 2 (.then(...))
# read that Textbox as studio_save_ui's actual text input. That failed
# real-browser acceptance twice -- clicking "Salva contenuto" gave no
# reliable, visible confirmation and the version did not reliably
# advance. Per the ticket: stop patching that timing, remove the hidden-
# carrier architecture as Save's source of truth entirely.
#
# New architecture: st_rich_editable (this SAME gr.HTML component) is
# now the DIRECT Gradio input st_save_btn.click(studio_save_ui, ...)
# reads -- see its wiring below, a single ordinary dependency, no
# .then(), no second component, no synthetic event standing in for a
# real one. What makes that reliable is what THIS script does: it keeps
# st_rich_editable's own tracked `value` (via `props.value = ...`,
# Gradio's own documented "the value and other props can be edited
# through `props`" mechanism for gr.HTML's js_on_load) equal to the
# live contenteditable DOM's CURRENT outerHTML-equivalent wrapper --
# deliberately HTML, not the canonical markdown-like text, and
# deliberately only at discrete moments (toolbar click, paste, blur,
# and -- the one that makes Save correct regardless of whether any of
# the others already ran -- a DOCUMENT-level CAPTURE-PHASE click
# listener on the Save button itself). Two reasons value stays HTML
# here rather than canonical text:
#   1. gr.HTML's own html_template ("${value}" by default) can
#      re-substitute `value` into the live DOM's innerHTML on a value
#      change; if `value` were raw canonical text ("**bold**"), a
#      resubstitution would flash literal markup into the visible
#      editor -- exactly Blocker 1 (raw markup exposed) reintroduced,
#      every time a toolbar button or paste fired. HTML that already
#      matches what's displayed makes any such resubstitution a no-op
#      visually, whichever way Gradio 6.5.1 actually behaves here --
#      not verifiable without a real browser (see the ticket's own
#      report requirements), so the design does not depend on either
#      answer being true.
#   2. Capture-phase, not `.then()`: a browser's own event-dispatch
#      order (capture always precedes target/bubble for the SAME
#      event, a DOM spec guarantee, not a Gradio promise-chain the
#      real browser has now broken twice) is what guarantees this
#      write completes before Gradio's own bubble-phase click handler
#      gathers st_rich_editable's value for studio_save_ui's inputs --
#      no setTimeout, no second click, no blur requirement.
#
# rich_text.html_to_canonical() (Python, server-side) is the actual
# HTML->canonical-text conversion now -- the inverse of
# render_editable_html(), reusing escape_literal() directly (no second,
# JS-side copy of the escapable-character table to keep in parity
# anymore for the SAVE path). This script's job is therefore only ever
# "report the live DOM as HTML", never "compute canonical text" for
# Save purposes.
#
# msRichTextGetCanonical (canonical TEXT, not HTML) is kept, UNCHANGED,
# for exactly one remaining purpose out of this ticket's scope: the
# existing draft-autosave-on-blur path (_ST_DRAFT_INPUTS' st_text_input,
# saved via studio_save_draft_ui, restored via studio_restore_draft_ui)
# -- a single, synchronous, real 'blur' DOM event, not a cross-click
# race, so it was never the failure mode being replaced here, and
# leaving it untouched keeps its own already-validated restore/discard
# round-trip (draftfix1-7) exactly as proven.
_MS_RICH_EDITABLE_JS_ON_LOAD = r"""
window.msRichTextGetCanonical = function () {
  const root = document.querySelector('#ms-rich-editable-inner');
  if (!root) { return null; }
  const ESCAPABLE = ['\\', '*', '_', '`', '[', ']'];
  function escapeLiteral(s) {
    let out = '';
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      out += (ESCAPABLE.indexOf(ch) !== -1) ? ('\\' + ch) : ch;
    }
    return out;
  }
  function serializeChildren(el) {
    let out = '';
    el.childNodes.forEach(function (child) {
      if (child.nodeType === 3) {
        out += escapeLiteral(child.textContent || '');
      } else if (child.nodeType === 1) {
        const tag = child.tagName;
        if (tag === 'BR') { out += '\n'; }
        else if (tag === 'B' || tag === 'STRONG') { out += '**' + serializeChildren(child) + '**'; }
        else if (tag === 'I' || tag === 'EM') { out += '_' + serializeChildren(child) + '_'; }
        else if (tag === 'CODE') { out += '`' + escapeLiteral(child.textContent || '') + '`'; }
        else if (tag === 'A') { out += '[' + escapeLiteral(child.textContent || '') + '](' + (child.getAttribute('href') || '') + ')'; }
        else if (tag === 'DIV' || tag === 'P') { out += serializeChildren(child) + '\n'; }
        else { out += escapeLiteral(child.textContent || ''); }
      }
    });
    return out;
  }
  function serializeRoot() {
    const lists = root.querySelectorAll(':scope > ul, :scope > ol');
    if (lists.length === 1 && root.children.length === 1) {
      const list = lists[0];
      const ordered = list.tagName === 'OL';
      const liItems = Array.prototype.slice.call(list.querySelectorAll(':scope > li'));
      const items = liItems.map(function (li, i) {
        const itemText = serializeChildren(li).replace(/\n/g, ' ');
        return (ordered ? (i + 1) + '. ' : '- ') + itemText;
      });
      return items.join('\n');
    }
    return serializeChildren(root).replace(/\n+$/, '');
  }
  return serializeRoot();
};

// 4E.3 Save Architecture Replacement: st_rich_editable's OWN authoritative
// value sync. Redefined every time this js_on_load runs (mount or any
// later re-run Gradio's frontend performs) so it always closes over the
// CURRENT `props` reference for this component instance -- if js_on_load
// only ever runs once (mount), this still stays correct for the whole
// session; if it ever runs again, the redefinition simply replaces the
// prior closure, never leaving two stale copies competing.
window.__msRichEditableSaveSyncer = function () {
  const root = document.querySelector('#ms-rich-editable-inner');
  if (!root) { return; }
  props.value = "<div id='ms-rich-editable-inner' contenteditable='true' class='ms-rich-editable-surface'>"
    + root.innerHTML + "</div>";
};

// Installed ONCE (module-global guard -- this js_on_load can legitimately
// run more than once, but the app has exactly one st_rich_editable, so a
// second document-level listener would be a harmless but pointless
// duplicate, not a correctness bug -- guarded anyway for cleanliness).
// Capture phase is what makes this reliable: per the DOM event spec, a
// capture-phase listener on `document` always finishes running BEFORE
// the SAME click event reaches its target and bubbles into Gradio's own
// (bubble-phase, default) click handler on the Save button -- a hard
// ordering guarantee from the browser itself, not from Gradio's own
// `.then()` promise-chaining, which is exactly the mechanism that failed
// real-browser acceptance twice per this ticket.
if (!window.__msSaveSyncInstalled) {
  window.__msSaveSyncInstalled = true;
  document.addEventListener('click', function (ev) {
    const btn = (ev.target && ev.target.closest) ? ev.target.closest('#ms-save-btn') : null;
    if (btn && window.__msRichEditableSaveSyncer) { window.__msRichEditableSaveSyncer(); }
  }, true);
}

const root = element.querySelector('#ms-rich-editable-inner');
if (!root) { }
else {
  const hiddenWrap = document.querySelector('#ms-rich-text-input');
  const hidden = hiddenWrap ? hiddenWrap.querySelector('textarea') : null;
  function pushCanonical() {
    if (!hidden) { return; }
    const canonical = window.msRichTextGetCanonical();
    if (canonical === null || hidden.value === canonical) { return; }
    hidden.value = canonical;
    hidden.dispatchEvent(new Event('input', { bubbles: true }));
  }
  root.oninput = pushCanonical;
  root.onblur = function () {
    pushCanonical();
    if (window.__msRichEditableSaveSyncer) { window.__msRichEditableSaveSyncer(); }
    if (hidden) { hidden.dispatchEvent(new Event('blur', { bubbles: true })); }
  };
  root.onpaste = function (e) {
    e.preventDefault();
    const cd = e.clipboardData || window.clipboardData;
    const text = cd ? cd.getData('text/plain') : '';
    document.execCommand('insertText', false, text);
    if (window.__msRichEditableSaveSyncer) { window.__msRichEditableSaveSyncer(); }
  };
  try { document.execCommand('defaultParagraphSeparator', false, 'br'); } catch (e) { }
}
"""

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
#workspace-tabs>div>.tab-nav,#workspace-tabs>div>[role="tablist"]{display:none!important;height:0!important;min-height:0!important;max-height:0!important;overflow:hidden!important;border:0!important;margin:0!important;padding:0!important;opacity:0!important;pointer-events:none!important}
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
#workspace-tabs{padding:0 24px 28px!important;max-width:1860px!important;margin:0 auto!important;width:100%!important}
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
/* 4E.2: Publishing Preview -- read-only rendering, deliberately visually
   distinct (soft border, no editable-looking chrome) from the Studio
   editor panels right next to it. */
.ms-pp-summary{background:var(--ui-surface-2);border:1px solid var(--ui-line);border-radius:12px;padding:12px 14px;font-size:12.5px;line-height:1.7;margin:10px 0}
.ms-pp-warning{background:var(--ui-amber-soft);border:1px solid #fde68a;color:#93450a;border-radius:12px;padding:12px 14px;font-size:12.5px;line-height:1.7;margin:10px 0}
.ms-pp-note-line{color:var(--ui-muted);font-size:11.5px;margin:2px 2px 10px}
.ms-pp-topic{border:1px solid var(--ui-line);border-radius:14px;padding:14px 16px;margin:12px 0;background:#fff}
.ms-pp-topic h2{font-size:16px;margin:0 0 8px;font-weight:800}
.ms-pp-topic.ms-pp-excluded{background:var(--ui-surface-2);opacity:.85}
.ms-pp-block{border-top:1px solid #f2f4f7;padding:10px 2px}
.ms-pp-block:first-of-type{border-top:0}
.ms-pp-block-head{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:#98a2b3;font-weight:750;margin-bottom:4px}
.ms-pp-block p{margin:2px 0;font-size:13px;line-height:1.6}
.ms-pp-badge{display:inline-block;background:var(--ui-primary-soft);color:#175cd3;border-radius:999px;padding:2px 8px;font-size:10px;font-weight:700;margin-left:6px}
.ms-pp-badge-warn{display:inline-block;background:var(--ui-amber-soft);color:#93450a;border-radius:999px;padding:2px 8px;font-size:10px;font-weight:700;margin-left:6px}
.ms-pp-status{display:inline-block;color:#98a2b3;font-size:10px;font-weight:700;margin-left:6px}
.ms-pp-reason{color:#93450a;font-size:12px;background:var(--ui-amber-soft);border-radius:8px;padding:6px 9px;margin-top:4px}
.ms-pp-note{color:var(--ui-muted);font-size:12px;font-style:italic}
.ms-pp-table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
.ms-pp-table th,.ms-pp-table td{border:1px solid var(--ui-line);padding:6px 9px;text-align:left}
.ms-pp-table th{background:var(--ui-surface-2);font-weight:750}
.ms-pp-table-title{font-weight:750;font-size:13px;margin-top:4px}
.ms-pp-table-caption{color:var(--ui-muted);font-size:11.5px;margin-bottom:2px}
.ms-pp-figure{max-width:100%;border-radius:8px;margin-top:6px;display:block}
.ms-pp-figure-caption{color:var(--ui-muted);font-size:11.5px;margin-top:4px}
/* 4F.1 export result card: the deliverable's own small product surface.
   Two real actions (open / download) instead of a filename card, so the
   user never has to work out where to click. Failure uses the same card
   shape in an error tone -- always contained, never a global toast. */
.ms-export-card{background:var(--ui-green-soft);border:1px solid #a6f4c5;border-radius:12px;
  padding:14px 16px;margin:10px 0 4px}
.ms-export-card-err{background:var(--ui-red-soft);border-color:#fecaca}
/* Publishing preflight: a refusal has to be readable, so each finding and
   its "dove / cosa fare" lines keep their own shape instead of collapsing
   into one paragraph. */
.ms-export-pf{margin-top:8px;font-size:12px;line-height:1.55}
.ms-export-pf-item{font-weight:600;margin-top:7px}
.ms-export-pf-detail{padding-left:14px;color:var(--ui-muted)}
.ms-export-headline{font-size:14px;font-weight:800;color:#067647}
.ms-export-card-err .ms-export-headline{color:#b42318}
.ms-export-subtitle{color:#475467;font-size:12.5px;margin-top:3px}
.ms-export-warn{background:var(--ui-amber-soft);border:1px solid #fde68a;color:#93450a;border-radius:8px;
  padding:6px 9px;font-size:11.5px;margin-top:8px}
.ms-export-actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:12px}
.ms-export-btn{display:inline-block;border-radius:9px;padding:8px 16px;font-size:12.5px;font-weight:750;
  text-decoration:none!important;border:1px solid transparent;cursor:pointer}
.ms-export-btn-primary{background:var(--ui-primary);color:#fff!important;border-color:var(--ui-primary)}
.ms-export-btn-primary:hover{background:var(--ui-primary-hover)}
.ms-export-btn-secondary{background:#fff;color:#344054!important;border-color:#d0d5dd}
.ms-export-btn-secondary:hover{background:#f9fafb}
.ms-export-tech{margin-top:11px;font-size:11px;color:var(--ui-muted)}
.ms-export-tech summary{cursor:pointer;color:#98a2b3}
.ms-export-tech div{margin-top:5px;word-break:break-all;font-family:ui-monospace,Consolas,monospace}
/* 4F.2 compliance starter pack: source-first presentation. The pack's
   provenance (fonte, versione, revisione, data di applicazione) is shown
   BEFORE the import button, and the "not automatic certification" notice
   is never optional chrome. */
.ms-cp-pack{background:var(--ui-surface-2);border:1px solid var(--ui-line);border-radius:12px;padding:14px 16px;margin:8px 0}
.ms-cp-pack-title{font-size:14px;font-weight:800;color:#101828}
.ms-cp-pack-sub{color:var(--ui-muted);font-size:12px;margin-top:4px}
.ms-cp-pack-meta{width:100%;border-collapse:collapse;margin:10px 0 4px;font-size:12px}
.ms-cp-pack-meta td{padding:4px 6px;border-top:1px solid var(--ui-line);vertical-align:top}
.ms-cp-pack-meta td:first-child{color:var(--ui-muted);width:190px}
.ms-cp-pack ul{margin:4px 0 0 18px;font-size:12px}
.ms-cp-notice{background:var(--ui-primary-soft);border:1px solid #bfdbfe;color:#1e40af;border-radius:9px;
  padding:8px 11px;font-size:11.5px;line-height:1.5;margin-top:10px}
.ms-cp-notice-warn{background:var(--ui-amber-soft);border-color:#fde68a;color:#93450a}
.ms-cp-packbar{display:flex;flex-wrap:wrap;gap:18px;align-items:flex-start;background:var(--ui-surface-2);
  border:1px solid var(--ui-line);border-radius:10px;padding:10px 14px;font-size:12px;margin-top:6px}
.ms-cp-packbar>div{min-width:150px}
.ms-cp-packbar-label{color:var(--ui-muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;
  margin-bottom:2px}
/* 4F.2 polish: the two compliance tables read as product tables, not as
   debug dumps. Gradio's dataframe defaults to a tabular/monospace-ish
   cell font and refuses to wrap, which is what forced a horizontal
   scrollbar at normal desktop width -- text now wraps inside its column
   and the column widths are declared, so the table fits 1920px without
   losing a single column. */
.ms-cp-grid table{font-family:inherit!important;font-variant-numeric:normal!important;
  table-layout:fixed!important;width:100%!important;
  /* Gradio makes the TABLE itself the scroll container. Reserving the
     vertical scrollbar gutter keeps the percentage column widths
     resolving against the visible width, so a long list does not gain a
     phantom horizontal scrollbar the moment it starts scrolling. */
  scrollbar-gutter:stable!important}
.ms-cp-grid td,.ms-cp-grid th{font-family:inherit!important;font-size:12.5px!important;
  line-height:1.45!important;white-space:normal!important;word-break:normal;overflow-wrap:anywhere;
  vertical-align:top;padding:7px 10px!important}
.ms-cp-grid th{font-weight:600;color:var(--ui-muted);text-transform:none;letter-spacing:0}
.ms-cp-grid td>*{white-space:normal!important}
/* Gradio renders TWO tables per dataframe: an outer one, and the real
   data table inside <svelte-virtual-table-viewport> which is itself the
   scroll container (overflow:auto + max-height). That inner table's own
   VERTICAL scrollbar takes ~9px out of its content box, but the column
   percentages resolve against the full border-box width -- and that 9px
   residue is painted as a real 10px-tall HORIZONTAL scrollbar under the
   table. Clipping it loses nothing: table-layout is fixed and the cells
   wrap, so the columns simply wrap a few pixels earlier instead of a
   scrollbar appearing under five columns that already fit. */
.ms-cp-grid table.table{overflow-x:hidden!important}
/* ...and only the HEADER of that pair is a real <table>. Gradio 6.5 paints
   the BODY as a virtualised list of divs (.virtual-row/.body-cell), so
   every rule above written against td/tr styles the header and the one
   empty spacer row only -- which is why the requirement list was still
   rendering in Gradio's monospace data font while its header was not. */
/* NOT font-family:inherit here: the virtualised body sits inside a Gradio
   container that itself declares the monospace data font, so "inherit"
   resolves back to monospace. Name Gradio's own body-font variable. */
.ms-cp-grid .body-cell,.ms-cp-grid .body-cell span,.ms-cp-grid .cell-wrap{
  font-family:var(--font)!important;font-size:12.5px!important;line-height:1.45!important;
  font-variant-numeric:normal!important;white-space:normal!important;overflow-wrap:anywhere}
.ms-cp-grid .body-cell{vertical-align:top;padding:7px 10px!important}
/* A clickable row has to look clickable, or nobody clicks it. */
.ms-cp-grid-click .virtual-row{cursor:pointer}
.ms-cp-grid-click .virtual-row:hover .body-cell{background:var(--ui-primary-soft)}
/* An empty status/preview panel must not reserve a band of blank page
   between the pack strip and "Copertura documentale". Gradio wraps HTML
   output in a padded container that is never :empty even with no content,
   so this keys off the cards that actually get rendered. */
.ms-cp-collapsible{display:none!important}
.ms-cp-collapsible:has(.ms-export-card),
.ms-cp-collapsible:has(.ms-cp-notice),
.ms-cp-collapsible:has(.ms-cp-table){display:block!important}
/* UX Gate 1 — persistent workspace context bar. Sits above the tabs, so
   project / publication / operator are answered once and stay answered. */
.ms-ctxbar{align-items:flex-end!important;gap:18px!important;background:var(--ui-surface);
  border:1px solid var(--ui-line);border-radius:12px;padding:10px 16px;margin:0 0 10px}
.ms-ctx-project{min-width:220px;font-size:13px;line-height:1.35}
.ms-ctx-label{color:var(--ui-muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em}
.ms-ctx-sub{color:var(--ui-muted);font-size:11.5px}
/* Status filter reads as a segmented control, not a form question. */
.ms-cp-filter .wrap{gap:4px!important}
.ms-cp-filter label{font-size:12px!important;padding:4px 10px!important}
/* The requirement detail column keeps its own scroll, so a long detail
   never pushes the list out of view -- the whole point of the gate. */
.ms-cp-detail{max-height:78vh;overflow-y:auto}
/* Header pieces, sized so the requirement list starts inside the first
   screen at 1920x1080 instead of at y=1093. */
.ms-cp-topbar{align-items:flex-end!important;gap:14px!important}
.ms-pagehead-tight{margin:0!important;padding:0!important}
.ms-pagehead-tight h1{margin:0!important;font-size:20px!important}
.ms-cp-statusbar{display:flex;flex-wrap:wrap;gap:20px;align-items:baseline;
  background:var(--ui-surface-2);border:1px solid var(--ui-line);border-radius:10px;
  padding:8px 14px;font-size:12px}
.ms-cp-stat span{color:var(--ui-muted);margin-right:5px}
.ms-cp-stat b{font-size:13px}
.ms-cp-packline{font-size:12px;color:var(--ui-muted);padding:2px 2px 0}
.ms-cp-packline b{color:var(--ui-text)}
.ms-cp-listhint{margin:0 0 4px!important}
/* Empty detail state. Without this the decision form rendered its radios,
   textboxes and buttons at full height with nothing selected -- 743px of
   blank surface that read as a broken screen rather than an invitation.
   Collapsing it in CSS keeps the positional output bundle untouched,
   which several callbacks and a structural guard depend on. */
.ms-cp-detail:has(.ms-cp-empty) .ms-cp-detail-form{display:none!important}
.ms-cp-detail:has(.ms-cp-empty){min-height:0!important}
.ms-cp-empty{display:flex;gap:12px;align-items:center;color:var(--ui-muted);
  font-size:13px;padding:26px 18px;border:1px dashed var(--ui-line);border-radius:10px;
  background:var(--ui-surface-2)}
.ms-cp-empty-icon{font-size:20px;opacity:.55}
/* Language switch: a setting, not a workflow step -- small, quiet, last. */
.ms-lang{margin:6px 10px 2px!important}
.ms-lang label{font-size:11.5px!important;padding:3px 8px!important}
.ms-lang .wrap{gap:4px!important}
.ms-cp-listbar{align-items:center!important;gap:10px!important;margin-bottom:4px}
/* Sub-tabs inside Conformità need an explicit rule for TWO reasons, both
   found by measuring the live page rather than by reading the markup:
   1. the app suppresses Gradio's own tab bar at the top level in favour
      of the custom sidebar, and that rule was written unscoped;
   2. Gradio 6.5.1 puts `.tab-container.visually-hidden` on a tab strip it
      decides does not fit, expecting its `.overflow-menu` to take over --
      and this app hides `.overflow-menu` globally too. The net effect was
      a tab strip 1px tall: clickable by script, invisible to a human.
   Scoping (1) is the whole fix. Gradio renders TWO tab containers per tab
   strip -- a real one and a `.visually-hidden` ghost it measures widths
   against -- so un-hiding `.visually-hidden` as well made every sub-tab
   label appear twice on screen. The ghost must stay hidden. */
#cp-subtabs .tab-container.visually-hidden{display:none!important}
#cp-subtabs .tab-nav{display:flex!important;gap:2px;height:auto!important;max-height:none!important;
  opacity:1!important;pointer-events:auto!important;overflow:visible!important;
  border-bottom:1px solid var(--ui-line)!important;margin:0 0 10px!important}
#cp-subtabs button[role="tab"]{display:inline-block!important;height:auto!important;
  max-height:none!important;opacity:1!important;pointer-events:auto!important;
  padding:7px 14px!important;font-size:13px!important;font-weight:600}
.ms-cp-table{width:100%;border-collapse:collapse;margin:10px 0 2px;font-size:11.5px;display:block;
  max-height:320px;overflow:auto}
.ms-cp-table th{text-align:left;color:var(--ui-muted);font-weight:600;padding:4px 6px;
  border-bottom:1px solid var(--ui-line);position:sticky;top:0;background:var(--ui-surface)}
.ms-cp-table td{padding:4px 6px;border-top:1px solid var(--ui-line);vertical-align:top}
.ms-cp-table td:nth-child(2){color:var(--ui-muted);white-space:nowrap}
.ms-cp-table td:nth-child(3){white-space:nowrap;font-weight:600}
/* 4E.3: rich-text marks, shared look between the standard editor's own
   contenteditable surface (.ms-rich-editable-surface) and Publishing
   Preview (.ms-pp-block) -- deliberately understated (no arbitrary
   colors/fonts per the ticket's own non-goals), just enough contrast to
   read as "formatted", not "raw markup". */
.ms-pp-block code,.ms-rich-editable-surface code{background:var(--ui-surface-2);border:1px solid var(--ui-line);
  border-radius:5px;padding:1px 5px;font-size:12px;font-family:ui-monospace,Consolas,monospace}
.ms-pp-block ul,.ms-pp-block ol,.ms-rich-editable-surface ul,.ms-rich-editable-surface ol{margin:4px 0;padding-left:22px}
.ms-pp-block li,.ms-rich-editable-surface li{font-size:13px;line-height:1.6;margin:2px 0}
.ms-pp-block a,.ms-rich-editable-surface a{color:var(--ui-primary);text-decoration:underline}
/* The editable surface itself is styled to look like an ordinary text
   field (border/padding/focus ring) -- the writer edits VISUALLY here,
   never a raw-markup textarea; :empty shows a muted placeholder exactly
   like a normal empty Textbox would. */
.ms-rich-editable-surface{border:1px solid var(--ui-line);border-radius:10px;padding:9px 12px;margin:4px 0 10px;
  background:#fff;min-height:120px;font-size:13px;line-height:1.6;outline:none;white-space:pre-wrap}
.ms-rich-editable-surface:focus{border-color:var(--ui-primary);box-shadow:0 0 0 1px var(--ui-primary)}
.ms-rich-editable-surface:empty::before{content:"Scrivi qui...";color:var(--ui-muted);font-style:italic}
.ms-rich-toolbar{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 4px}
.ms-rich-toolbar button{font-size:12px!important;padding:4px 10px!important;min-width:0!important}
/* 4E.3 Save Architecture Replacement: the unmistakable, CONTAINED save
   confirmation right next to the editor (not relying on the writer to
   notice the version number changing elsewhere on the page) -- styled
   per-outcome so a failure reads as clearly different from a success,
   never a generic/anonymous message box. */
.ms-rich-save-status{border-radius:10px;padding:8px 12px;margin:2px 0 10px;font-size:12.5px;font-weight:650;line-height:1.5}
.ms-rich-save-ok{background:var(--ui-green-soft);border:1px solid #a6f4c5;color:#067647}
.ms-rich-save-err{background:var(--ui-red-soft);border:1px solid #fecaca;color:#b42318}
.ms-rich-save-info{background:var(--ui-surface-2);border:1px solid var(--ui-line);color:var(--ui-muted)}

/* ================================================================
   UX Gate 2A -- the three-zone authoring workspace.

   Left and right panes stick while the centre scrolls, so the manual
   structure stays reachable while writing instead of scrolling off the
   top (measured: the outline used to leave the viewport before the
   Save button came into it).

   position:sticky needs one override to work here. .gradio-container
   carries overflow:hidden, and an ancestor with a non-visible overflow
   becomes the sticky element's scroll container -- so the pane stuck
   to a box that never scrolls, i.e. did not stick at all. Verified in
   the live browser during the discovery gate: with overflow:visible
   restored on the container, a pane held at 160px from the top at
   scroll offset 900. Restricted to :has() of the workspace so the rule
   cannot change scrolling anywhere else in the app; browsers without
   :has() simply get the current non-sticky behaviour.
   ================================================================ */
.gradio-container:has(.ms-authoring-workspace){overflow:visible!important}
/* Reclaim the dead vertical space above the workspace. Measured on the
   recomposed page: 559px separated the top of the viewport from the top
   of the panes, of which only 231px was actual content (context bar +
   page head) -- the rest was stacked default gaps between Gradio blocks
   that carry no information. Scoped to the tab that has the workspace,
   so no other page's rhythm changes. */
.tabitem:has(.ms-authoring-workspace)>.ms-pagehead,
.tabitem:has(.ms-authoring-workspace) .ms-pagehead{
  padding-top:2px!important;padding-bottom:2px!important;margin-bottom:0!important}
.tabitem:has(.ms-authoring-workspace) .ms-pagehead h1{font-size:21px!important}
/* Gradio wraps every gr.HTML in a .html-container with 10px of vertical
   padding. On the page head that is 20px spent directly above the
   workspace; zero it for that one block only. */
.tabitem:has(.ms-authoring-workspace) .html-container:has(.ms-pagehead){
  padding-top:0!important;padding-bottom:0!important}
.tabitem:has(.ms-authoring-workspace)>.column>*{margin-top:0!important}
.tabitem:has(.ms-authoring-workspace)>.column{gap:6px!important}
.tabitem:has(.ms-authoring-workspace){padding-top:0!important;margin-top:0!important}
.ms-authoring-workspace{margin-top:0!important}
/* The open object's identity and state read as one line, not two. */
.ms-editor-contextline{align-items:center!important;gap:10px!important;margin:0!important}
.ms-editor-contextline>*{margin:0!important;min-width:0}
/* One line means one line: strip the block padding Gradio adds around
   each child so the pair does not occupy two rows' worth of height. */
.ms-editor-contextline .html-container,
.ms-editor-contextline .prose,
.ms-editor-contextline .block{padding-top:0!important;padding-bottom:0!important;margin:0!important;border:0!important}
.ms-editor-contextline p{margin:0!important;line-height:1.35!important}
/* The toolbar sits directly on top of the writing surface -- it is part
   of the editor, not a band above it. */
.ms-pane-centre .ms-rich-toolbar{margin-bottom:0!important;padding-bottom:0!important}
.ms-authoring-workspace{align-items:flex-start!important;gap:16px!important}
.ms-authoring-workspace>.ms-pane{min-width:0}
.ms-pane-left,.ms-pane-right{
  position:sticky;top:12px;align-self:flex-start;
  max-height:calc(100vh - 28px);overflow-y:auto;overflow-x:hidden;
  padding-right:6px}
/* The centre pane must NOT be sticky: it is the one that scrolls. */
.ms-pane-centre{min-height:60vh}
/* The editor is the dominant surface, not a 120px slot inside a page
   of configuration. */
.ms-pane-centre .ms-rich-editable-inner,
.ms-pane-centre #ms-rich-editable-inner{min-height:520px!important}
/* Only the BODY editor grows. A first cut of this rule targeted every
   textarea in the centre pane, which also caught a topic's single-line
   "Titolo" field and rendered it 320px tall -- found by measuring a
   topic rather than a block. #ms-rich-text-input is st_text_input, the
   canonical body-text carrier; the title keeps its natural height. */
.ms-pane-centre #ms-rich-text-input textarea{min-height:320px!important}
/* UX Gate 2B.1 -- the Impact panel. Compact by construction: it informs
   without competing with the editor, which is the constraint the whole
   three-zone layout exists to protect. */
.ms-impact-compact{display:flex;flex-direction:column;gap:3px;margin:2px 0 8px}
.ms-impact-line{font-size:12.5px;line-height:1.45;color:var(--ui-ink);
  padding:3px 8px;border-radius:7px;background:var(--ui-surface-2);
  border:1px solid var(--ui-line)}
/* Only things a human may have to act on are marked. "3 publications" is
   context; "2 translations to check" is a decision waiting to happen. */
.ms-impact-attention{background:var(--ui-amber-soft,#fffaeb);border-color:#fde68a;
  color:#92400e;font-weight:650}
.ms-impact-empty{font-size:12.5px;color:var(--ui-muted);padding:3px 2px}
.ms-impact-details{font-size:12px;color:var(--ui-ink)}
.ms-impact-group{margin-bottom:10px}
.ms-impact-group-title{font-weight:750;font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;color:#98a2b3;margin-bottom:3px}
.ms-impact-details ul{margin:0;padding-left:16px}
.ms-impact-details li{margin:2px 0;line-height:1.45}
.ms-impact-limits{border-top:1px solid var(--ui-line);padding-top:8px;margin-top:4px}
.ms-impact-limits li{color:var(--ui-muted)}

/* UX Gate 2B.2 -- Affidabilita traduzioni. One block per language,
   status stated in words as well as colour: colour alone is not an
   accessible signal. */
.ms-tr-panel{display:flex;flex-direction:column;gap:6px;margin:2px 0 8px}
.ms-tr-version{font-size:11px;color:var(--ui-muted);margin-bottom:2px}
.ms-tr-lang{padding:6px 9px;border-radius:8px;border:1px solid var(--ui-line);
  background:var(--ui-surface-2)}
.ms-tr-lang-name{font-size:12.5px;font-weight:700;margin-bottom:1px}
.ms-tr-lang-status{font-size:12.5px;font-weight:650;margin-bottom:2px}
.ms-tr-lang-why{font-size:11.5px;color:var(--ui-muted);line-height:1.4}
.ms-tr-stale{background:var(--ui-red-soft,#fef3f2);border-color:#fecaca}
.ms-tr-stale .ms-tr-lang-status{color:#b42318}
.ms-tr-fresh .ms-tr-lang-status{color:#067647}
.ms-tr-missing .ms-tr-lang-status{color:var(--ui-muted)}
.ms-tr-empty,.ms-tr-note{font-size:11.5px;color:var(--ui-muted);padding:3px 2px;line-height:1.45}
.ms-tr-impact{font-size:12px;margin-top:6px}
.ms-tr-impact-lang{border-top:1px solid var(--ui-line);padding-top:7px;margin-top:7px}
.ms-tr-figures,.ms-tr-why{margin:3px 0 0;padding-left:16px}
.ms-tr-figures li,.ms-tr-why li{margin:1px 0;line-height:1.4}
.ms-tr-why-title{font-weight:750;font-size:10.5px;text-transform:uppercase;
  letter-spacing:.05em;color:#98a2b3;margin-top:6px}
.ms-tr-seg{display:block;font-size:10.5px;color:var(--ui-muted);font-style:italic}
/* the stepper now says where each step lives */
.ms-step-where{display:block;font-size:10px;font-weight:500;color:var(--ui-muted);
  text-transform:none;letter-spacing:0;margin-top:1px}

/* Quiet scrollbars in the side panes -- they are navigation, not
   content, and a heavy native bar reads as a second page. */
.ms-pane-left::-webkit-scrollbar,.ms-pane-right::-webkit-scrollbar{width:8px}
.ms-pane-left::-webkit-scrollbar-thumb,.ms-pane-right::-webkit-scrollbar-thumb{
  background:#e4e7ec;border-radius:8px}
/* The structure list is the primary navigation: give it room and make
   each row read as a target rather than a form option. */
.ms-pane-left .wrap[data-testid="radio-group"]{gap:1px!important}
.ms-pane-left label{cursor:pointer}
@media (max-width:1400px){
  /* Below this the three panes stop being three panes; let them stack
     rather than squeeze the editor under its usable width. */
  .ms-pane-left,.ms-pane-right{position:static;max-height:none;overflow:visible}
}
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
    return f"""<div class='ms-project-card'><div class='ms-project-top'><div><span class='ms-eyebrow'>{t('context.active_project')}</span><h2>{h.get('project_name',Path(project_dir).name)}</h2><div class='ms-meta'>{_project_meta_line(h)}</div></div></div><div class='ms-kpis'><div class='ms-kpi'><div class='ms-kpi-label'>Pubblicazioni</div><b>{h['publications']}</b><small>nel progetto</small></div><div class='ms-kpi'><div class='ms-kpi-label'>Blocchi</div><b>{h['blocks']}</b><small>contenuto strutturato</small></div><div class='ms-kpi'><div class='ms-kpi-label'>Job aperti</div><b>{open_jobs}</b><small>workflow traduzione</small></div><div class='ms-kpi'><div class='ms-kpi-label'>QA bloccati</div><b>{h['qa_blocked']}</b><small>richiedono intervento</small></div></div><div class='ms-next'><span class='ms-next-icon'>→</span><div><b>Azione consigliata</b><br>{next_action}</div></div></div>"""

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

# ======================================================================
# Domain error -> sentence. The ONLY place a code becomes words.
#
# Services raise a stable code with structured parameters and no opinion
# about language (see domain_errors.py). This resolves one, in whatever
# language the session selected, through the same catalog every other
# label goes through.
#
# Three rules it enforces:
#   * an unknown code, or a failure that is not a DomainError at all,
#     resolves to ONE generic sentence -- never to the raw code, and never
#     to a Python exception message. i18n's own fallback returns the key
#     verbatim, which is right for a label and wrong for an error, so this
#     checks has_key() explicitly rather than relying on it.
#   * parameters go through t(**params), which interpolates via the safe
#     mapping -- a missing one stays visible as {name} instead of raising.
#   * the technical detail is not thrown away, it is logged. Users get a
#     sentence; diagnostics get the class, the code and the params.
# ======================================================================
def domain_error_message(exc: BaseException) -> str:
    """The user-facing sentence for a failure, in the session's language."""
    code = getattr(exc, "code", None)
    params = getattr(exc, "params", None)
    if not isinstance(exc, de.DomainError) or not code:
        _app_log(f"unresolved failure surfaced to the UI: {type(exc).__name__}", exc)
        return t("error.unknown")
    key = f"error.{code}"
    if not i18n.has_key(key):
        _app_log(f"domain error with no catalog entry: {code!r} {params!r}")
        return t("error.unknown")
    return t(key, **(params or {}))


def ui_error_text(exc: BaseException, lang: str | None = None) -> str:
    """Message for an exception at a boundary that is NOT yet migrated.

    Authoring/Contenuti services still refuse operations by raising
    finished Italian sentences, and those must keep rendering exactly as
    they do today -- this gate hardens the boundary, it does not migrate
    the domain. But the same handlers also sit downstream of code that
    HAS been migrated, so a DomainError can arrive here; printing it
    would put a bare "ERR_..." on screen.

    So: a DomainError is localized, anything else is returned verbatim.
    A generic failure is never dressed up as a validation message -- it
    keeps its own text, unchanged.

    `lang` is accepted so that when Contenuti is localized these call
    sites gain the session language by passing an argument, rather than
    needing this decision revisited.
    """
    if not isinstance(exc, de.DomainError):
        return str(exc)
    if lang is None:
        return domain_error_message(exc)
    with i18n.use_language(lang):
        return domain_error_message(exc)


def domain_error_detail(exc: BaseException) -> str:
    """Technical identification, for advanced/diagnostic surfaces only.

    Deliberately NOT shown in the normal flow: a stable user never needs
    to read ERR_COVERED_REQUIRES_EVIDENCE to understand that they have to
    link evidence first.
    """
    if isinstance(exc, de.DomainError):
        return repr(exc)
    return f"{type(exc).__name__}: {exc}"


def _page_head_html(kicker_key: str, title_key: str, subtitle_key: str) -> str:
    """One section page head: kicker, title, one-line subtitle.

    Every migrated section head is built here so the language switch can
    rebuild it with exactly the markup it was created with -- two copies
    of this string would drift the first time one of them changed.

    All three arguments are catalog KEYS, never display text. A second
    definition of this function used to follow, taking the subtitle as
    literal text for sections whose body copy was not yet migrated; it
    shadowed this one, so every one of the six call sites -- all of
    which pass `page.<section>.subtitle` -- printed the key verbatim.
    The catalog has had every one of those subtitles in `it` and `en`
    for some time, so the literal-subtitle variant had no remaining
    caller and is gone."""
    return (f"<div class='ms-pagehead'><div><div class='ms-page-kicker'>{t(kicker_key)}</div>"
            f"<h1>{t(title_key)}</h1><p>{t(subtitle_key)}</p></div></div>")


def _section_head_html(title_key: str, sub_key: str | None = None) -> str:
    """One in-page section heading, built from catalog KEYS.

    UX Gate 2A: every section title the recomposed Contenuti workspace
    moved or introduced goes through here. The markup is the same
    ms-section-title / ms-section-sub pair those headings already used
    as hand-written gr.HTML literals; the difference is that the text is
    now a key, so the language switch rebuilds it like everything else
    registered with LOC."""
    head = f"<div class='ms-section-title'>{t(title_key)}</div>"
    if sub_key:
        head += f"<div class='ms-section-sub'>{t(sub_key)}</div>"
    return head


def _project_meta_line(h: dict) -> str:
    """Client · code · revision, with the separators dropped for whatever
    is missing. It used to render as a bare "· · Rev." on any project
    without that metadata -- punctuation around nothing."""
    bits = [str(h.get("client") or "").strip(), str(h.get("manual_code") or "").strip()]
    rev = str(h.get("revision") or "").strip()
    if rev:
        bits.append(f"Rev. {rev}")
    return " · ".join(b for b in bits if b)


def _workspace_context_html(project_dir: str | None) -> str:
    """The project half of the persistent context bar.

    Human name only. The filesystem path is deliberately NOT here: it was
    printed in full on Home as an absolute filesystem path, which is the
    same class of internal detail the export cards were cleaned of in 4F.1."""
    if not project_dir:
        return (f"<div class='ms-ctx-project'><div class='ms-ctx-label'>{t('context.project')}</div>"
                f"<b>{t('context.no_project')}</b></div>")
    try:
        h = project_health(project_dir)
        name = h.get("project_name") or Path(project_dir).name
        client = h.get("client") or ""
    except Exception:
        name, client = Path(project_dir).name, ""
    sub_line = f"<div class='ms-ctx-sub'>{html.escape(str(client))}</div>" if client else ""
    return (f"<div class='ms-ctx-project'><div class='ms-ctx-label'>{t('context.project')}</div>"
            f"<b>{html.escape(str(name))}</b>{sub_line}</div>")


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
        msg=f"<div class='ms-card'><h3 class='ms-block'>Analisi non completata</h3><p><b>{type(e).__name__}</b>: {ui_error_text(e)}</p><p class='ms-muted'>Dettagli salvati in {APP_LOG}</p></div>"
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
        raise IdmlReviewCorruptError(f"{path.name} non è leggibile o non è JSON valido: {ui_error_text(exc)}") from exc
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
                      f"{html.escape(ui_error_text(exc))}<br>L'approvazione di questa sorgente resta bloccata "
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
        raise gr.Error(f"Stato di revisione IDML non leggibile: {ui_error_text(exc)}. Ripristina il file o riesegui l'importazione prima di continuare.") from exc
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
        msg = f"⛔ {ui_error_text(exc)}"
    return msg, _project_card(project_dir), _sidebar_context(project_dir), _simple_review_table_for_project(project_dir)

def approve_source_ui(project_dir, publication_id, reviewer, confirmed):
    if not confirmed: raise gr.Error("Serve la conferma esplicita della revisione umana.")
    try:
        pending=_idml_pending_review_items(project_dir, publication_id)
    except IdmlReviewCorruptError as exc:
        raise gr.Error(f"Impossibile approvare: lo stato di revisione IDML non è leggibile o è corrotto "
                        f"({ui_error_text(exc)}). Ripristina il file o riesegui l'importazione prima di approvare.") from exc
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
        raise gr.Error(ui_error_text(e))
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
        raise gr.Error(ui_error_text(e))
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
    except Exception as e: raise gr.Error(ui_error_text(e))
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


def _rich_save_status_update(save_msg: str):
    """4E.3 Browser-Level Save Gate: the ONE place that renders the
    contained save-status banner shown right next to the editor.

    Root cause this exists to fix (reproduced in real Chrome, automated:
    see regressione_browser_save_4e3.py): every PRECONDITION refusal --
    missing actor name, nothing selected, a table with no columns --
    returns through _studio_noop_bundle, which deliberately emits
    gr.update() no-ops for all 49 outputs except st_save_msg. That left
    the refusal visible ONLY in the st_save_msg Markdown far below the
    editor, so a writer who clicked "Salva contenuto" with an empty "Il
    tuo nome" field saw the version simply not move, with no explanation
    anywhere near where they were looking -- exactly the reported
    "no confirmation, still versione 11" symptom. The save pipeline
    itself was working correctly the whole time; only the feedback was
    invisible. Both bundle builders now route their message through
    here, so success, no-op and refusal are equally impossible to miss.
    """
    if not save_msg:
        return gr.update(value="", visible=False)
    cls = ("ms-rich-save-ok" if save_msg.startswith("✅")
           else "ms-rich-save-err" if save_msg.startswith("⛔")
           else "ms-rich-save-info")
    return gr.update(value=f"<div class='ms-rich-save-status {cls}'>{html.escape(save_msg)}</div>", visible=True)

def _studio_pub_header_html(repo, publication_id: str) -> str:
    # UX clarity fix: "Editor professionale" alone never told the user WHICH
    # manual they were looking at -- the static header just said the generic
    # word "Pubblicazione". This shows the real, current publication title.
    #
    # UX Gate 2A: this header moved into the left context pane (it answers
    # "which manual am I in", which is navigation context, not a caption
    # for the editor) and so it is a MOVED control -- its two strings go
    # through the catalog like every other label in the workspace. The
    # publication TITLE itself is project data and is never translated.
    row = repo.conn.execute("SELECT title FROM publications WHERE publication_id=?", (publication_id,)).fetchone()
    if not row:
        return (f"<div class='ms-muted'>"
                f"{t('content.pubheader.missing', publication_id=html.escape(publication_id))}</div>")
    return (f"<div class='ms-prod'>{t('content.pubheader.label')}: "
            f"<b>{html.escape(row['title'])}</b></div>")

def studio_context_relabel_ui(project_dir, publication_id, lang):
    """Re-render the Contenuti context header in the newly chosen language.

    UX Gate 2A. st_pub_header is data-driven HTML ("Pubblicazione: <title>"),
    so LOC cannot rebuild it from a catalog key alone -- it needs the live
    project and publication. Without this it kept the previous language's
    label until the next studio refresh.

    Deliberately the NARROWEST possible output: this one display-only
    element. It must not touch the editor. Re-running the full studio
    bundle here would rebuild st_text_input/st_rich_editable from persisted
    state and so discard whatever the author has typed but not yet saved --
    switching language must never cost someone their in-progress paragraph.
    """
    if not project_dir:
        return gr.update()
    with i18n.use_language(lang):
        repo, _svc = av.open_service(project_dir)
        try:
            return _studio_pub_header_html(repo, (publication_id or "").strip() or "PUB-MAIN")
        finally:
            repo.close()


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
            # 4E.3 UI regression fix (46->48) plus 4E.3 Save Architecture
            # Replacement (48->49, st_rich_save_status appended): every
            # early-return tuple in this function must keep matching
            # ST_OUT_FULL's actual width exactly, or the whole panel
            # renders as cascading red Error controls on any path that
            # reaches it (see studio_load_ui's own regression coverage).
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
                    False, None, hidden_notice, hidden_btn, hidden_btn, gr.update(visible=False), diff_output,
                    gr.update(visible=False), gr.update(value="", visible=False), gr.update(value="", visible=False))

        # service= passed so the outline shows a compact, human condition
        # differentiator (e.g. '· Mercato EU') instead of the generic
        # '· variante' every conditioned row used to share -- see
        # _compact_condition_suffix in authoring_view.py.
        rows = av.outline_rows(repo, publication_id, include_archived=bool(show_archived), service=svc)
        table = pd.DataFrame(av.outline_table(rows)) if rows else pd.DataFrame(columns=_ST_EMPTY_TABLE_COLS)
        picker_choices = av.open_choices(rows)
        picker_value = f"{kind}:{object_id}" if kind and object_id else None
        if picker_value not in [v for _, v in picker_choices]:
            picker_value, kind, object_id = None, None, None

        if not kind or not object_id:
            # THIS is the path every normal "Contenuti" tab entry hits
            # FIRST (nothing selected yet -- kind/object_id are None on
            # initial load). Same width-parity requirement as the
            # "publication not found" early return above (46->48, now
            # 48->49 with st_rich_save_status appended) -- keep both in
            # lockstep with ST_OUT_FULL's actual length.
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
                    False, None, hidden_notice, hidden_btn, hidden_btn, gr.update(visible=False), diff_output,
                    gr.update(visible=False), gr.update(value="", visible=False), gr.update(value="", visible=False))

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
        # 4E.3 rich-text toolbar/editable surface -- default hidden (topic,
        # table, figure, component_ref selections all keep this default);
        # the block/editable-text branch below is the only place that
        # turns it on.
        rich_toolbar_visible = False
        rich_editable_upd = gr.update(value="", visible=False)
        # 4E.3 Save Architecture Replacement: the unmistakable, CONTAINED
        # save confirmation next to the editor -- default hidden/empty;
        # only ever shown right after a save/save-attempt (studio_save_ui
        # passes save_msg through _studio_full_bundle), for a text block.
        rich_save_status_upd = gr.update(value="", visible=False)

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
            # st_text_input is now PERMANENTLY hidden/non-interactive --
            # it only ever carries the canonical text as a value other
            # callbacks (save, draft-autosave) read; the writer never
            # sees or types into it directly (see its own declaration
            # comment for the full rationale).
            text_upd = gr.update(value=detail["text"], visible=False, interactive=False)
            rich_toolbar_visible = editable_text and not detail["archived"]
            # What the writer actually sees/edits: rich_text.
            # render_editable_html() -- bare inline HTML (no <p> wrapper;
            # the contenteditable div itself is the implicit paragraph)
            # for prose, or the real <ul>/<ol> for a list -- built from
            # the CURRENTLY SAVED text/text_format, exactly what
            # Publishing Preview would show for this same content.
            _cid_container = "ms-rich-editable-inner"
            rich_editable_upd = gr.update(
                value=(f"<div id='{_cid_container}' contenteditable='true' "
                       f"class='ms-rich-editable-surface'>"
                       f"{rich_text.render_editable_html(detail['text'], detail.get('text_format', 'plain'))}"
                       f"</div>") if editable_text else "",
                visible=editable_text,
            )
            # 4E.3 Save Architecture Replacement: the ticket's explicit UX
            # requirement -- "the user could not even tell whether Save
            # did anything" -- an unmistakable, CONTAINED confirmation
            # right next to the editor, not relying on the writer to spot
            # the version number changing elsewhere on the page. Same
            # save_msg text st_save_msg (further down the panel) already
            # shows -- one message, shown in two places -- styled by
            # outcome (success/no-op/error) via a CSS class chosen here,
            # never a global gr.Error popup, so a save failure shows one
            # contained, readable line rather than a cascade of red Error
            # controls.
            if editable_text and save_msg:
                rich_save_status_upd = _rich_save_status_update(save_msg)
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
            gr.update(visible=rich_toolbar_visible), rich_editable_upd, rich_save_status_upd,
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
    ST_IDX_COMPARE_ROW, ST_IDX_DIFF_OUTPUT, \
    ST_IDX_RICH_TOOLBAR_ROW, ST_IDX_RICH_EDITABLE, ST_IDX_RICH_SAVE_STATUS = range(49)

def _studio_noop_bundle(*, save_msg: str = "", review_msg: str = "") -> tuple:
    # Standard-mode validation UX fix: a predictable, user-correctable input
    # gap (missing actor name, missing title, nothing selected for an action
    # that needs a selection) is guidance, not a system error -- it must
    # never raise gr.Error (which pops a global red toast and, on this
    # multi-output panel, would otherwise force a full _studio_full_bundle
    # re-render from the DB, silently discarding whatever the user had just
    # typed in a title/text/caption/grid field). gr.update() with no args,
    # returned for every one of the 49 outputs, touches nothing: verified
    # against Gradio 6.5.1's own dispatch code (blocks.py) that a State
    # output (st_kind/st_id/st_version/st_block_type/st_object_ref_id/
    # st_shared_confirmed/st_shared_impact_token) receiving an update-dict
    # with no "value" key is left at its previous value rather than being
    # overwritten. (46 -> 48 in 4E.3: st_rich_toolbar_row/st_rich_editable
    # appended; 48 -> 49 in the Save Architecture Replacement:
    # st_rich_save_status appended -- all at the end of
    # _studio_full_bundle's own return tuple.)
    out = [gr.update() for _ in range(49)]
    out[ST_IDX_SAVE_MSG] = save_msg
    out[ST_IDX_REVIEW_MSG] = review_msg
    # 4E.3 Browser-Level Save Gate: the ONE deliberate exception to this
    # function's "touch nothing" contract. A precondition refusal
    # ("Inserisci il tuo nome per continuare.", "Seleziona prima un
    # elemento.", "Aggiungi almeno una colonna.") reached the user ONLY
    # through st_save_msg, far below the editor -- which is why a real
    # save refusal looked identical to "nothing happened at all" in the
    # browser. Writing the SAME message into the contained banner costs
    # nothing (it is a display-only gr.HTML, never an input, never a
    # State, so it cannot discard anything the user has typed) and makes
    # every refusal visible exactly where the writer is already looking.
    out[ST_IDX_RICH_SAVE_STATUS] = _rich_save_status_update(save_msg)
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
    #           st_restore_draft_btn, st_discard_draft_btn, st_rich_editable,
    #           st_rich_save_status]
    #
    # 4E.3 hardening: st_rich_editable (the VISIBLE contenteditable
    # surface) was missing from this callback's outputs entirely --
    # restoring a text-block draft updated only the HIDDEN st_text_input
    # carrier, so the writer would click "Ripristina bozza" and see no
    # visible change at all (the old plain-Textbox architecture didn't
    # have this gap, since st_text_input WAS the visible surface then).
    # Rendered as 'rich' unconditionally here, matching this whole
    # editor's established convention that an in-progress/being-edited
    # text stream is always shown WYSIWYG-interpreted (the actual
    # STORED text_format is only ever decided at save time, by
    # save_block's 'auto' policy -- restoring a draft is not a save).
    #
    # 4E.3 Save Architecture Replacement: st_rich_save_status (the "Salvato
    # -- versione X" banner next to the editor) is explicitly CLEARED when
    # a text draft is restored -- a stale success banner sitting next to
    # content that is now DIFFERENT from what it describes (the just-
    # restored, unsaved draft) would be misleading. Every other branch
    # (topic/table/figure, and the two no-draft early returns) leaves it
    # untouched, matching st_rich_editable's own existing no-op there --
    # the rich editor/status pair isn't shown for those kinds anyway.
    draft_kind, draft_id = _studio_draft_target(kind, object_id, block_type, object_ref_id)
    hide2 = (gr.update(visible=False), gr.update(visible=False))
    noop7 = (gr.update(),) * 7
    noop_rich = (gr.update(), gr.update())
    if not draft_id:
        return noop7 + hide2 + noop_rich
    repo, svc = av.open_service(project_dir)
    try:
        draft = svc.load_draft(draft_kind, draft_id)
    finally:
        repo.close()
    if not draft:
        return noop7 + hide2 + noop_rich
    payload = draft["payload"]
    if draft_kind == "topic":
        return (gr.update(value=payload.get("title", "")), gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update()) + hide2 + noop_rich
    if draft_kind == "table":
        cols = payload.get("columns") or ["Colonna 1"]
        rows = payload.get("rows") or [["" for _ in cols]]
        grid_df = pd.DataFrame(rows, columns=[str(c) for c in cols])
        return (gr.update(), gr.update(), gr.update(value=payload.get("title", "")),
                gr.update(value=payload.get("caption", "")), _fixed_grid_update(grid_df),
                gr.update(), gr.update()) + hide2 + noop_rich
    if draft_kind == "figure":
        return (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(value=payload.get("caption", "")), gr.update(value=payload.get("alt_text", ""))) + hide2 + noop_rich
    draft_text = payload.get("text", "")
    draft_editable_html = (f"<div id='ms-rich-editable-inner' contenteditable='true' "
                           f"class='ms-rich-editable-surface'>{rich_text.render_editable_html(draft_text, 'rich')}</div>")
    return (gr.update(), gr.update(value=draft_text), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update()) + hide2 + (gr.update(value=draft_editable_html),
                                                  gr.update(value="", visible=False))

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
            # 4E.3 Save Architecture Replacement: text_val is now the
            # editor's live HTML (st_rich_editable is the direct input,
            # see the wiring's own comment for why) -- convert to the same
            # canonical markdown-like string save_block()/has_rich_
            # structure() have always worked with. html_to_canonical() is
            # the exact inverse of render_editable_html(), reusing
            # escape_literal() directly, so a real edit round-trips
            # byte-for-byte the same way it always has.
            new_text = rich_text.html_to_canonical(text_val).strip()
            # "when content did not change: do not create a fake version
            # bump". AuthoringService.save_block() itself bumps the
            # version UNCONDITIONALLY on every real invocation (by design,
            # for every OTHER caller in this codebase -- deliberately left
            # untouched here to avoid any blast radius on 4B/4C/4D/4E.1
            # behavior/tests that rely on that exact semantic). This is a
            # narrow, UI-layer-only guard specific to the rich-text save
            # path.
            #
            # Compares against the PROSPECTIVE STORED value, not the raw
            # incoming string -- fixes a real bug the straight comparison
            # had: new_text is always in ESCAPED canonical form (backslash-
            # protected '*_`[]', mirroring escape_literal()), but a 'plain'
            # block's stored text_content is the UNESCAPED literal text
            # (save_block's own plain-storage path unescapes on write,
            # since 'plain' rendering never unescapes on read -- see
            # rich_text.py's module docstring). A block like the literal-
            # markup example ("M8*1.25", stored unescaped) would otherwise
            # NEVER match its own escaped round-trip and would fake-bump
            # its version on every no-op click. Replicating save_block's
            # own "auto" decision here (has_rich_structure on the
            # candidate text, once already-rich never demotes) means this
            # comparison is against exactly what would actually get
            # written, for both plain and rich blocks alike.
            row = repo.conn.execute(
                "SELECT text_content, text_format FROM content_blocks WHERE block_id=?", (object_id,)).fetchone()
            current_text = str(row["text_content"]) if row else None
            current_format = str(row["text_format"]) if row and row["text_format"] else "plain"
            if current_text is not None:
                would_be_rich = current_format == "rich" or rich_text.has_rich_structure(new_text)
                effective_new = new_text if would_be_rich else rich_text.unescape_literal(new_text)
                if effective_new == current_text:
                    repo.close()
                    return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived,
                                               save_msg="Nessuna modifica da salvare.")
            # 4E.3 stabilization (Blocker 2): "auto" -- NOT an
            # unconditional "rich" -- promotes 'plain'->'rich' only when
            # AuthoringService.save_block determines the saved text
            # genuinely introduces rich structure (rich_text.
            # has_rich_structure: a real mark, a link, or a list); an
            # ordinary text edit with no formatting applied leaves a
            # 'plain' block exactly 'plain'. An already-'rich' block
            # always stays 'rich' (no silent demotion either). See
            # save_block's own "auto" branch for the full policy and the
            # matching literal-text unescape-on-plain-storage fix.
            # save_block's guard already rejects `text` entirely for
            # non-EDITABLE_TEXT_BLOCK_TYPES (table/figure/component_ref),
            # so this never mislabels something that was never text-
            # editable in the first place.
            result = svc.save_block(object_id, text=new_text, expected_version=int(expected_version), actor=actor,
                                    text_format="auto")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
    except (KeyError, ValueError) as e:
        repo.close()
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, review_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        return _studio_full_bundle(project_dir, publication_id, kind, object_id, show_archived, save_msg=f"⛔ {ui_error_text(e)}")
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
        # 4E.3: av.diff_text() itself is UNCHANGED (still the plain word-
        # level SequenceMatcher diff that backs the highlighted body
        # below) -- only the ONE-LINE SUMMARY is upgraded here, via
        # rich_text.diff_summary(), to distinguish "Formattazione
        # modificata" (visible text identical, only marks changed) and
        # "N elementi elenco modificati" from the generic "Testo
        # modificato" av.diff_text() would otherwise always say for a
        # rich-text block. d["summary"] itself is left exactly as
        # av.diff_text() computed it, still driving the word-added/
        # removed-count line inside _render_diff_text_markdown(d)'s body.
        rich_summary = rich_text.diff_summary(old["text"], live["text"],
                                              old.get("text_format", "plain"), live.get("text_format", "plain"))
        header = f"**Confronto: versione {cv} → versione corrente ({live['version']})** — {rich_summary}"
        return header + "\n\n" + _render_diff_text_markdown(d)
    except KeyError:
        return f"⛔ Versione {compare_version} non trovata."
    except Exception as e:
        return f"⛔ {ui_error_text(e)}"
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


# ----------------------------------------------------------------
# 4E.1: Conditions & Variants -- Studio "Varianti" panel + minimal
# dimension/value/profile management (Impostazioni > Varianti).
#
# Deliberately wired INDEPENDENTLY of _studio_full_bundle/ST_OUT_FULL
# (the ~30-callsite shared bundle that drives the rest of the Studio
# panel) rather than folded into it: st_id and st_version are already
# outputs of every one of those callsites, so attaching this panel's own
# refresh to st_id.change()/st_version.change() below picks up every
# navigation and save path for free, without touching that already very
# large, carefully-tuned tuple contract at all. Editing conditions here
# always goes through AuthoringService.save_topic/save_block's own CAS,
# reading the object's OWN current version fresh at save time rather
# than trusting st_version as an input -- st_version's meaning for an
# open table/figure BLOCK is the referenced table/figure's own version
# (see _studio_full_bundle's version_for_save), not the wrapping block's
# own content_blocks.version that a condition save actually CASes
# against, so the two must never be confused.
# ----------------------------------------------------------------

_VARIANT_ROWS = 4
# summary, panel visible, dim x N, val x N, profile dd, eval clear, and
# (UX Gate 2A) the shared profile_state carried back out so the variant
# profile survives opening the next object and stays in step with the
# same selector in Rilascio.
_VARIANT_PANEL_OUT_COUNT = 2 + 2 * _VARIANT_ROWS + 3


def _variant_dimension_choices(svc: AuthoringService) -> list[tuple[str, str]]:
    return [("(nessuna)", "")] + [(d["name"], d["dimension_id"]) for d in svc.list_condition_dimensions(include_inactive=False)]


def _variant_value_choices(svc: AuthoringService, dimension_id: str | None) -> list[tuple[str, str]]:
    if not dimension_id:
        return []
    return [(v["label"], v["value_id"]) for v in svc.list_condition_values(dimension_id, include_inactive=False)]


def _variant_profile_choices(svc: AuthoringService) -> list[tuple[str, str]]:
    return [(p["name"], p["profile_id"]) for p in svc.list_variant_profiles(include_inactive=False)]


def _variant_rows_from_condition(dimensions: list[dict]) -> list[tuple[str, list[str]]]:
    """[(dimension_id, [value_id,...]), ...] padded/truncated to _VARIANT_ROWS."""
    out = [(d["dimension_id"], [v["value_id"] for v in d["values"]]) for d in dimensions]
    return out[:_VARIANT_ROWS] + [("", [])] * max(0, _VARIANT_ROWS - len(out))


def _keep_profile(choices: list[tuple[str, str]], current) -> str | None:
    """The still-valid half of a profile selection, else None.

    UX Gate 2A: the profile is now session state shared with Rilascio's
    preview/export, so a panel refresh must not silently clear it -- a
    user who picked "Mercato IT" and then opened the next block would
    otherwise be evaluating against nothing without being told. A profile
    that genuinely disappeared (deactivated, renamed away, different
    project) is still dropped: keeping a dangling id would make the two
    surfaces disagree again, in the one direction that matters."""
    valid = {pid for _label, pid in choices}
    return current if current in valid else None


def _variant_panel_bundle(svc: AuthoringService, detail: dict[str, Any],
                          current_profile=None) -> tuple:
    dim_choices = _variant_dimension_choices(svc)
    rows = _variant_rows_from_condition(detail.get("condition_dimensions", []))
    dim_updates = []
    val_updates = []
    for dim_id, value_ids in rows:
        dim_updates.append(gr.update(choices=dim_choices, value=(dim_id or None)))
        val_updates.append(gr.update(choices=_variant_value_choices(svc, dim_id), value=value_ids))
    profile_choices = _variant_profile_choices(svc)
    kept = _keep_profile(profile_choices, current_profile)
    return (detail.get("condition_summary", ""), gr.update(visible=True), *dim_updates, *val_updates,
            gr.update(choices=profile_choices, value=kept), "", kept)


def studio_impact_panel_ui(project_dir, kind, object_id, lang=None):
    """UX Gate 2B.1: the Impatto / Impact panel for the open object.

    Strictly read-only. assess_object_impact() writes nothing, and this
    callback adds nothing of its own -- viewing impact must never be a
    reason for project data to change.

    `lang` is an input purely so the language switch can re-render the
    panel: the report is codes+params, so the same report produces
    Italian or English depending on the language active for THIS request.
    use_language() scopes it to this call, exactly as the compliance
    callbacks do, so one session's choice never decides another's.

    A failure here must not take the workspace down with it: impact is
    contextual information, not part of writing or saving. Anything
    unexpected degrades to the neutral empty panel rather than raising
    into the editor's own event chain.
    """
    empty = iv.render(None)
    if not project_dir or kind not in ii.SUPPORTED_KINDS or not object_id:
        return empty
    try:
        with i18n.use_language(lang) if lang else contextlib.nullcontext():
            report = ii.assess_object_impact(project_dir, kind, object_id)
            return iv.render(report)
    except de.DomainError:
        # object genuinely gone (deleted/archived away under us)
        return empty
    except Exception:
        return empty


def _translation_stepper_html() -> str:
    """The Traduzioni workflow, told honestly.

    The old stepper advertised four steps as if all four lived on this
    tab; only the first does. Rather than move working features around to
    satisfy a graphic, each step now says where it actually happens --
    the workflow becomes coherent without relocating anything.
    """
    steps = (("translation.step.1", "translation.step.here"),
             ("translation.step.2", "translation.step.in_jobs"),
             ("translation.step.3", "translation.step.in_jobs"),
             ("translation.step.4", "translation.step.in_release"))
    out = []
    for idx, (label, where) in enumerate(steps, start=1):
        active = " active" if idx == 1 else ""
        out.append(f"<div class='ms-step{active}'><span class='ms-step-num'>{idx}</span>"
                   f"{html.escape(t(label))}"
                   f"<span class='ms-step-where'>{html.escape(t(where))}</span></div>")
    return f"<div class='ms-stepper'>{''.join(out)}</div>"


def translation_panel_ui(project_dir, kind, object_id, lang=None):
    """UX Gate 2B.2 Tier 1: translation reliability for the open object.

    Read-only. assess_translation_intelligence writes nothing, and this
    adds nothing of its own. `lang` is an input so the language switch
    re-renders the panel: the report is codes+params, so the same report
    speaks Italian or English depending on the language active for THIS
    request.
    """
    empty = tv.render(None)
    if not project_dir or not object_id or kind not in ti.SUPPORTED_KINDS:
        return (*empty, "")
    try:
        with i18n.use_language(lang) if lang else contextlib.nullcontext():
            report = ti.assess_translation_intelligence(project_dir, object_id,
                                                        object_type=kind)
            panel, note = tv.render(report)
            return panel, note, ""
    except de.DomainError:
        return (*empty, "")
    except Exception:
        return (*empty, "")


def translation_impact_ui(project_dir, kind, object_id, lang=None):
    """UX Gate 2B.2 Tier 2: "Analisi impatto traduzione", on demand.

    Runs the EXISTING translation_impact engine with its output directed
    at a temporary directory, so no package is generated and the project
    is not touched. An object the engine refuses (not approved) produces
    a plain explanation, never a technical error.
    """
    if not project_dir or not object_id or kind not in ti.SUPPORTED_KINDS:
        return ""
    try:
        with i18n.use_language(lang) if lang else contextlib.nullcontext():
            report = ti.assess_translation_intelligence(project_dir, object_id,
                                                        object_type=kind, deep=True)
            return tv.impact_html(report)
    except de.DomainError as exc:
        return f"<div class='ms-tr-note'>{html.escape(ui_error_text(exc, lang))}</div>"
    except Exception:
        with i18n.use_language(lang) if lang else contextlib.nullcontext():
            return f"<div class='ms-tr-note'>{html.escape(t('translation.tier2.TIER2_FAILED'))}</div>"


def studio_variant_panel_ui(project_dir, kind, object_id, current_profile=None):
    """Refreshes the Studio 'Varianti' section: human summary, the
    _VARIANT_ROWS dimension/value picker rows pre-filled from the
    currently open topic/block's own condition, and the profile choices
    for the evaluator. Triggered by st_id.change()/st_version.change()
    -- see the module note above for why.

    UX Gate 2A: also carries the shared profile through. It takes the
    current profile_state in and returns it back out (dropped only if
    that profile no longer exists), so opening the next object keeps the
    profile the author is working against instead of resetting it."""
    if not project_dir or kind not in ("topic", "block") or not object_id:
        dim_updates = [gr.update(choices=[], value=None) for _ in range(_VARIANT_ROWS)]
        val_updates = [gr.update(choices=[], value=[]) for _ in range(_VARIANT_ROWS)]
        return ("", gr.update(visible=False), *dim_updates, *val_updates,
                gr.update(choices=[], value=None), "", current_profile)
    repo, svc = av.open_service(project_dir)
    try:
        detail = av.topic_detail(svc, object_id) if kind == "topic" else av.block_detail(svc, object_id)
        return _variant_panel_bundle(svc, detail, current_profile)
    finally:
        repo.close()


def studio_variant_dimension_pick_ui(project_dir, kind, object_id, dimension_id):
    """One dimension picker row's Criterio changed: repopulate that row's
    own Valori picker with the choices for the NEWLY picked dimension.

    Deliberately SELF-HEALING rather than a blind "always clear to []":
    wired via .input() specifically so this should only ever fire on a
    genuine user pick, never on the programmatic value pushes studio_
    variant_panel_ui/studio_variant_save_ui make when pre-filling a row
    from a just-opened/just-saved condition -- but a real-browser
    regression (switching between two conditioned placements that happen
    to share the same dimension, e.g. two 'Mercato' table placements)
    found the Varianti summary updating correctly while the Valori picker
    stayed on the PREVIOUSLY selected placement's value. The panel bundle
    itself was verified byte-for-byte correct in that exact scenario (see
    the 4E.1 report), so the only remaining explanation is this .input()
    cascade firing after all and overwriting the just-applied value --
    something this exact Gradio 6.5.1 build's .input() reliability was
    never actually verified for on gr.Dropdown specifically (the
    established local precedent, .input() vs .change() on gr.Textbox,
    does NOT transfer automatically to a different component type).

    Rather than depend on pinning down that exact frontend timing (not
    verifiable without a browser this session), this is made incapable of
    ever producing a WRONG answer regardless of when/why it fires: it now
    re-reads the CURRENTLY selected element's own persisted condition and
    returns whatever value is ALREADY stored for the picked dimension (or
    an empty selection only if that dimension genuinely isn't part of the
    current condition yet, e.g. the user is adding a brand new row). A
    spurious re-fire during a navigation-triggered panel refresh can then
    only ever reproduce the SAME correct value the fresh bundle already
    set -- never clear it, never leave a stale value from a DIFFERENT,
    previously selected element."""
    if not project_dir or not dimension_id:
        return gr.update(choices=[], value=[])
    repo, svc = av.open_service(project_dir)
    try:
        choices = _variant_value_choices(svc, dimension_id)
        current_values: list[str] = []
        try:
            if kind == "topic" and object_id:
                condition = svc.load_topic_for_edit(object_id).get("condition") or {}
            elif kind == "block" and object_id:
                condition = svc.load_block_for_edit(object_id).get("condition") or {}
            else:
                condition = {}
            for dim in svc.describe_condition(condition).get("dimensions", []):
                if dim["dimension_id"] == dimension_id:
                    current_values = [v["value_id"] for v in dim["values"]]
                    break
        except KeyError:
            # Stale/no-longer-existing selection at cascade time -- a
            # background repopulation, never user-facing enough to
            # justify a gr.Error modal; fall back to an empty selection
            # (the "adding a brand new condition" default) rather than
            # crash this row's picker.
            current_values = []
        return gr.update(choices=choices, value=current_values)
    finally:
        repo.close()


def studio_variant_save_ui(project_dir, kind, object_id, block_type, actor, *dim_and_val_values):
    """Fail-visible by construction: EVERY failure path here raises
    gr.Error(...) rather than quietly returning an unchanged bundle with
    just a Markdown message. This app launches with show_error=False (an
    existing, app-wide setting, deliberately left untouched), which
    silently swallows an ordinary exception -- visible only in the server
    terminal, never in the browser. gr.Error is the one exception type
    Gradio ALWAYS surfaces to the user regardless of that flag (see
    gradio.utils.error_payload: `show_error = show_error or isinstance
    (error, AppError)`, and gr.Error IS an AppError).

    Deliberately declares ONLY [st_variant_save_msg, st_version] as its
    OWN Gradio outputs -- not the whole panel. A real-browser regression
    found that when this function used to also declare the panel's 8
    picker Dropdowns + visibility + eval-profile as outputs (to refresh
    them inline on success), a missing-actor gr.Error flagged ALL of
    those components with Gradio's frontend error-state indicator, even
    though nothing about any of them was actually wrong -- the whole
    panel looked broken from one small, expected validation failure. The
    full panel refresh (summary, visibility, pickers, eval profile) is
    instead chained via .then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS) on the wiring below --
    the SAME mechanism already used for every other Studio navigation
    event -- which re-reads whatever is CURRENTLY persisted regardless of
    whether this save just succeeded or failed, so a failed save leaves
    the panel showing the unchanged prior state (never a "successful-
    looking" stale display), and a successful one shows the new state,
    through one single, already-proven code path -- not two."""
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if kind not in ("topic", "block") or not object_id:
        raise gr.Error("Seleziona prima un elemento.")
    if not str(actor or "").strip():
        raise gr.Error("Inserisci il tuo nome (in alto, «La tua identità») per continuare.")
    dim_values = dim_and_val_values[:_VARIANT_ROWS]
    val_values = dim_and_val_values[_VARIANT_ROWS:]
    condition: dict[str, list[str]] = {}
    for dim_id, values in zip(dim_values, val_values):
        if dim_id and values:
            condition[dim_id] = list(values)
    repo, svc = av.open_service(project_dir)
    try:
        if kind == "topic":
            current_version = svc.load_topic_for_edit(object_id)["version"]
            result = svc.save_topic(object_id, condition=condition, expected_version=current_version, actor=actor)
            version_out = result["version"]
        else:
            current_version = svc.load_block_for_edit(object_id)["version"]
            result = svc.save_block(object_id, condition=condition, expected_version=current_version, actor=actor)
            # For a table/figure block, st_version tracks the REFERENCED
            # table/figure's own version -- completely untouched by this
            # save (only content_blocks.condition_json/version changed),
            # so leave it alone. For any other block type, st_version IS
            # this block's own version, kept in sync.
            version_out = result["version"] if block_type not in ("table", "figure") else gr.update()
    except VersionConflictError:
        raise gr.Error(av.conflict_message())
    except ArchivedObjectError as e:
        raise gr.Error(ui_error_text(e))
    except (KeyError, ValueError) as e:
        raise gr.Error(ui_error_text(e))
    except Exception as e:
        # Catch-all: an unanticipated DB/service error must still surface
        # visibly (see the docstring above) rather than vanish into the
        # terminal-only log show_error=False produces for a bare exception.
        raise gr.Error(f"Salvataggio varianti non riuscito: {ui_error_text(e)}")
    finally:
        repo.close()
    return "✅ Varianti salvate.", version_out


def studio_variant_evaluate_ui(project_dir, kind, object_id, profile_id):
    if not project_dir or kind not in ("topic", "block") or not object_id:
        return "Apri prima un topic o un blocco."
    if not profile_id:
        return "Seleziona un profilo da valutare."
    repo, svc = av.open_service(project_dir)
    try:
        result = svc.evaluate_element_with_profile(kind, object_id, profile_id)
        return av.evaluate_summary_text(result)
    except KeyError as e:
        return f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()


# ----------------------------------------------------------------
# 4E.2 Publishing Preview -- read-only, variant-aware preview of what a
# publication would contain for a selected profile. All actual model-
# building/rendering logic lives in publishing_preview.py (zero gradio
# imports, testable standalone); these two functions are thin Gradio
# wiring only, matching the app.py = "thin wiring, zero business logic"
# contract already established for the rest of the Studio.
# ----------------------------------------------------------------

def studio_preview_profile_choices_ui(project_dir, current_profile=None):
    """(label, profile_id) choices for a variant-profile picker -- ACTIVE
    profiles only (an inactive one is never offered as a NEW selection),
    human names only, never raw ids.

    UX Gate 2A: this is now the ONE choice builder for both surfaces that
    ask "which variant profile?" -- the Contenuti evaluator and Rilascio's
    preview/export. It used to exist twice, as this function and as
    _variant_profile_choices(), with byte-identical bodies feeding two
    unrelated gr.Dropdowns both labelled "Profilo".

    Returns (dropdown_update, profile_id). A still-valid selection is
    PRESERVED rather than reset -- the selection is shared session state
    now, so clearing it here would silently desynchronise the other
    surface. A profile that no longer exists is still dropped, which is
    what makes switching project safe."""
    if not project_dir:
        return gr.update(choices=[], value=None), None
    repo, svc = av.open_service(project_dir)
    try:
        choices = [(p["name"], p["profile_id"]) for p in svc.list_variant_profiles(include_inactive=False)]
    finally:
        repo.close()
    kept = _keep_profile(choices, current_profile)
    return gr.update(choices=choices, value=kept), kept


def studio_preview_refresh_ui(project_dir, publication_id, profile_id, show_excluded):
    """Builds and renders the preview -- a pure read path (publishing_
    preview.build_preview_model() never issues a write), so this callback
    itself never mutates authoring data, never bumps a version, never
    writes an audit event. A missing publication/profile (e.g. the
    selected profile was deactivated/renamed away mid-session) fails
    VISIBLY via gr.Error rather than silently showing a stale or
    misleading preview -- the ticket's own 'fail visibly and require
    refresh' requirement for a profile that disappears during the
    session."""
    if not project_dir:
        raise gr.Error("Nessun progetto attivo.")
    if not profile_id:
        return "<div class='ms-pp-note-line'>Seleziona un profilo per vedere quali contenuti verranno inclusi.</div>"
    repo, svc = av.open_service(project_dir)
    try:
        model = pp.build_preview_model(repo, svc, (publication_id or "").strip() or "PUB-MAIN", profile_id)
        return pp.render_preview_html(model, show_excluded=bool(show_excluded))
    except KeyError:
        raise gr.Error("Impossibile generare l'anteprima: la publication o il profilo selezionato non sono "
                       "più disponibili. Aggiorna la struttura o l'elenco profili e riprova.")
    except Exception as e:
        raise gr.Error(f"Anteprima non generabile: {ui_error_text(e)}")
    finally:
        repo.close()


# ----------------------------------------------------------------
# 4F.1 Production publishing outputs -- the smallest additive export UI
# that exposes publishing_output.py's real deliverables. Deliberately
# ISOLATED callbacks with their own two small outputs: they never touch
# ST_OUT_FULL, so a failed export can never disturb the authoring panel
# (the 49-slot bundle is exactly where a partial-update bug becomes a
# cascade of red Error controls -- see the 4E.3 UI regression).
#
# Read-only, like the preview they sit under: exporting a deliverable is
# NOT a release decision. No version bump, no audit event, no status
# change -- proven by regressione_publishing_outputs_4f1.py's mutation
# suite, not merely asserted here.
# ----------------------------------------------------------------

def _export_error_html(message: str) -> str:
    """A failed export is a contained, human-readable card in this
    section -- never a global red toast, never a stack trace.

    A preflight refusal arrives as several lines (what failed, where, what
    to change). Rendering it as one run-on sentence would waste exactly
    the information the user needs, so the structure is preserved: the
    first line is the headline, the rest become the body."""
    text = str(message or "")
    head, _, rest = text.partition("\n")
    body = ""
    if rest.strip():
        lines = []
        for raw in rest.split("\n"):
            stripped = raw.strip()
            if not stripped:
                continue
            cls = "ms-export-pf-detail" if raw.startswith("    ") else "ms-export-pf-item"
            lines.append(f"<div class='{cls}'>{html.escape(stripped)}</div>")
        body = f"<div class='ms-export-pf'>{''.join(lines)}</div>"
    return (f"<div class='ms-export-card ms-export-card-err'>"
            f"<div class='ms-export-headline'>{html.escape(head)}</div>{body}</div>")


def _export_result_card(*, headline: str, subtitle: str, open_url: str, open_label: str,
                        download_url: str, download_label: str, tech_detail: str,
                        warnings: str = "") -> str:
    """The product-grade export result card.

    Root UX problem this replaces: the first cut returned a success
    banner plus a raw Gradio File widget ("manuale.pdf / Scarica / 12 kB")
    and printed the technical path in standard mode -- leaving the user to
    work out that the filename card was the way to open the deliverable.

    Here the two things a user actually wants ARE the interface: a primary
    "Apri" button and a secondary "Scarica" button, both real links to
    opaque same-origin routes (see export_links.py -- no filesystem path
    reaches the page). The technical path stays collapsed inside
    "Dettagli tecnici (modalità avanzata)", matching where every other
    technical detail lives in this app."""
    warn_html = (f"<div class='ms-export-warn'>{html.escape(warnings)}</div>" if warnings else "")
    tech_html = (f"<details class='ms-export-tech'><summary>Dettagli tecnici (modalità avanzata)</summary>"
                 f"<div>{html.escape(tech_detail)}</div></details>" if tech_detail else "")
    return (
        "<div class='ms-export-card'>"
        f"<div class='ms-export-headline'>{html.escape(headline)}</div>"
        f"<div class='ms-export-subtitle'>{html.escape(subtitle)}</div>"
        f"{warn_html}"
        "<div class='ms-export-actions'>"
        f"<a class='ms-export-btn ms-export-btn-primary' href='{html.escape(open_url)}' "
        f"target='_blank' rel='noopener'>{html.escape(open_label)}</a>"
        f"<a class='ms-export-btn ms-export-btn-secondary' href='{html.escape(download_url)}' "
        f"download>{html.escape(download_label)}</a>"
        "</div>"
        f"{tech_html}"
        "</div>"
    )


def _ensure_export_routes() -> bool:
    """Installs the opaque artifact routes on the already-running Gradio
    FastAPI app, once. Called from the export callbacks because by then
    the server necessarily exists -- which keeps both launch paths
    (app.py's blocking launch and manualsync_desktop's) untouched."""
    try:
        return export_links.install_routes(getattr(demo, "app", None))
    except Exception as e:  # never let a link-route problem break an export
        _app_log("Export link routes unavailable", e)
        return False


def _export_human_location(project_dir, path: Path) -> str:
    """Where the artifact landed, expressed RELATIVE to the project the
    user already chose -- enough to find the file, without printing their
    home directory or username into the UI."""
    try:
        return str(Path(path).relative_to(Path(project_dir)))
    except (ValueError, TypeError):
        return Path(path).name


def _export_warnings_detail(warnings: list[str]) -> str:
    if not warnings:
        return ""
    head = "Avviso: " if len(warnings) == 1 else f"{len(warnings)} avvisi: "
    return head + " · ".join(warnings[:4]) + (" …" if len(warnings) > 4 else "")


def _it_pages(n: int) -> str:
    return "1 pagina" if n == 1 else f"{n} pagine"


def studio_export_pdf_ui(project_dir, publication_id, profile_id):
    """Single output: the export result card. Never raises gr.Error -- an
    export problem is a contained, human-readable card in this section,
    never a global red toast over the whole app."""
    if not project_dir:
        return _export_error_html("Nessun progetto attivo.")
    if not profile_id:
        return _export_error_html("Seleziona prima un profilo.")
    repo, svc = av.open_service(project_dir)
    try:
        res = po.export_pdf(repo, svc, (publication_id or "").strip() or "PUB-MAIN", profile_id)
    except po.PublishingOutputError as e:
        return _export_error_html(ui_error_text(e))
    except KeyError:
        return _export_error_html("La publication o il profilo selezionato non sono più disponibili.")
    except Exception as e:
        _app_log("PDF export failed", e)
        return _export_error_html("Non è stato possibile creare il PDF.")
    finally:
        repo.close()

    if not _ensure_export_routes():
        return _export_error_html("PDF creato, ma il collegamento per aprirlo non è disponibile. "
                                  "Riavvia l'applicazione e riprova.")
    token = export_links.register_export(
        root=res["output_dir"], primary_rel=res["pdf"].name, download_path=res["pdf"],
        download_name=f"{po._slug(res['publication_title'])}-{po._slug(res['profile_name'])}.pdf")
    return _export_result_card(
        headline="PDF creato correttamente",
        subtitle=f"{res['publication_title']} · {res['profile_name']} · {_it_pages(res['pages'])}",
        open_url=export_links.open_url(token), open_label="Apri PDF",
        download_url=export_links.download_url(token), download_label="Scarica PDF",
        tech_detail=_export_human_location(project_dir, res["pdf"]),
        warnings=_export_warnings_detail(res["warnings"]))


def studio_export_html_ui(project_dir, publication_id, profile_id):
    """Single output: the export result card. The canonical artifact is
    the portable FOLDER (index.html + style.css + assets/ + manifest);
    "Apri HTML" serves index.html from that folder through the opaque
    route (so its relative style.css/assets hrefs resolve), and "Scarica"
    hands over a ZIP of exactly that folder, built in a temp directory so
    the export is never polluted by its own archive."""
    if not project_dir:
        return _export_error_html("Nessun progetto attivo.")
    if not profile_id:
        return _export_error_html("Seleziona prima un profilo.")
    repo, svc = av.open_service(project_dir)
    try:
        res = po.export_html_package(repo, svc, (publication_id or "").strip() or "PUB-MAIN", profile_id)
    except po.PublishingOutputError as e:
        return _export_error_html(ui_error_text(e))
    except KeyError:
        return _export_error_html("La publication o il profilo selezionato non sono più disponibili.")
    except Exception as e:
        _app_log("HTML export failed", e)
        return _export_error_html("Non è stato possibile creare il pacchetto HTML.")
    finally:
        repo.close()

    zip_path = None
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ms_html_zip_"))
        zip_path = shutil.make_archive(str(tmp_dir / "pacchetto-html"), "zip",
                                       root_dir=str(res["output_dir"]))
    except Exception as e:
        _app_log("HTML package zip failed (package itself is valid)", e)
    if not _ensure_export_routes():
        return _export_error_html("Pacchetto HTML creato, ma il collegamento per aprirlo non è "
                                  "disponibile. Riavvia l'applicazione e riprova.")
    token = export_links.register_export(
        root=res["output_dir"], primary_rel="index.html", download_path=zip_path,
        download_name=f"{po._slug(res['publication_title'])}-{po._slug(res['profile_name'])}.zip")
    return _export_result_card(
        headline="Pacchetto HTML creato correttamente",
        subtitle=f"{res['publication_title']} · {res['profile_name']}",
        open_url=export_links.open_url(token), open_label="Apri HTML",
        download_url=export_links.download_url(token), download_label="Scarica pacchetto HTML",
        tech_detail=_export_human_location(project_dir, res["output_dir"]),
        warnings=_export_warnings_detail(res["warnings"]))


# ---- Impostazioni > Varianti: minimal dimension/value/profile management ----

def settings_variant_dimensions_table_ui(project_dir):
    cols = ["Id", "Nome", "Attiva", "In uso"]
    if not project_dir:
        return pd.DataFrame(columns=cols)
    repo, svc = av.open_service(project_dir)
    try:
        rows = svc.list_condition_dimensions()
    finally:
        repo.close()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{"Id": r["dimension_id"], "Nome": r["name"], "Attiva": "Sì" if r["active"] else "No",
                          "In uso": "Sì" if r["referenced"] else "No"} for r in rows])


def settings_variant_create_dimension_ui(project_dir, dimension_id, name, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_dimensions_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    repo, svc = av.open_service(project_dir)
    try:
        svc.create_condition_dimension(dimension_id, name, actor=actor)
        msg = "✅ Dimensione creata."
    except ValueError as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_dimensions_table_ui(project_dir), msg


def settings_variant_rename_dimension_ui(project_dir, dimension_id, new_name, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_dimensions_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    repo, svc = av.open_service(project_dir)
    try:
        svc.rename_condition_dimension(dimension_id, name=new_name, actor=actor)
        msg = "✅ Dimensione rinominata."
    except (KeyError, ValueError) as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_dimensions_table_ui(project_dir), msg


def settings_variant_set_dimension_active_ui(project_dir, dimension_id, active, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_dimensions_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    repo, svc = av.open_service(project_dir)
    try:
        svc.set_condition_dimension_active(dimension_id, bool(active), actor=actor)
        msg = "✅ Dimensione aggiornata."
    except (KeyError, ValueError) as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_dimensions_table_ui(project_dir), msg


def settings_variant_values_table_ui(project_dir):
    cols = ["Id", "Dimensione", "Etichetta", "Attivo", "In uso"]
    if not project_dir:
        return pd.DataFrame(columns=cols)
    repo, svc = av.open_service(project_dir)
    try:
        rows = svc.list_condition_values()
    finally:
        repo.close()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{"Id": r["value_id"], "Dimensione": r["dimension_id"], "Etichetta": r["label"],
                          "Attivo": "Sì" if r["active"] else "No", "In uso": "Sì" if r["referenced"] else "No"} for r in rows])


def settings_variant_create_value_ui(project_dir, dimension_id, value_id, label, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_values_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    repo, svc = av.open_service(project_dir)
    try:
        svc.create_condition_value(value_id, dimension_id, label, actor=actor)
        msg = "✅ Valore creato."
    except (KeyError, ValueError) as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_values_table_ui(project_dir), msg


def settings_variant_rename_value_ui(project_dir, value_id, new_label, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_values_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    repo, svc = av.open_service(project_dir)
    try:
        svc.rename_condition_value(value_id, label=new_label, actor=actor)
        msg = "✅ Valore rinominato."
    except (KeyError, ValueError) as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_values_table_ui(project_dir), msg


def settings_variant_set_value_active_ui(project_dir, value_id, active, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_values_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    repo, svc = av.open_service(project_dir)
    try:
        svc.set_condition_value_active(value_id, bool(active), actor=actor)
        msg = "✅ Valore aggiornato."
    except (KeyError, ValueError) as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_values_table_ui(project_dir), msg


def _format_profile_config(dims: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(dims.items())) or "(nessuna)"


def settings_variant_profiles_table_ui(project_dir):
    cols = ["Id", "Nome", "Configurazione", "Attivo"]
    if not project_dir:
        return pd.DataFrame(columns=cols)
    repo, svc = av.open_service(project_dir)
    try:
        rows = svc.list_variant_profiles()
    finally:
        repo.close()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{"Id": r["profile_id"], "Nome": r["name"], "Configurazione": _format_profile_config(r["dimensions"]),
                          "Attivo": "Sì" if r["active"] else "No"} for r in rows])


def _parse_profile_config(text: str) -> dict[str, str]:
    """One 'dimension_id=value_id' pair per line -- the minimal, explicit
    text form the ticket's 'minimal profile management UI' calls for,
    validated server-side (unknown dimension/value -> ValueError, never
    silently dropped) by AuthoringService.create_variant_profile itself."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def settings_variant_create_profile_ui(project_dir, profile_id, name, config_text, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_profiles_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    dims = _parse_profile_config(config_text)
    repo, svc = av.open_service(project_dir)
    try:
        svc.create_variant_profile(profile_id, name, dims, actor=actor)
        msg = "✅ Profilo creato."
    except ValueError as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_profiles_table_ui(project_dir), msg


def settings_variant_set_profile_active_ui(project_dir, profile_id, active, actor):
    if not project_dir: raise gr.Error("Nessun progetto attivo.")
    if not str(actor or "").strip():
        return settings_variant_profiles_table_ui(project_dir), "Inserisci il tuo nome (tab Redazione & contenuti) per continuare."
    repo, svc = av.open_service(project_dir)
    try:
        svc.set_variant_profile_active(profile_id, bool(active), actor=actor)
        msg = "✅ Profilo aggiornato."
    except (KeyError, ValueError) as e:
        msg = f"⛔ {ui_error_text(e)}"
    finally:
        repo.close()
    return settings_variant_profiles_table_ui(project_dir), msg


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
    try:
        result=generate_translation_work_packages(repo,object_id.strip(),kind=kind,tm_paths=tms,target_langs=target_langs,require_approved=True,out_dir=out)
    except TranslationFormatNotSupportedError as e:
        # 4E.3 final translation blocker: this is the ONLY exception this
        # call can raise where the message is ALREADY the final, safe,
        # human-readable Italian text with zero internal ids/JSON/raw
        # markup/stack trace -- so it is shown verbatim via gr.Error (which
        # always surfaces regardless of the app's show_error=False launch
        # setting, unlike an uncaught exception which would not).
        # ui_error_text() is a pass-through for it: this class is not a
        # DomainError, so the wording below is byte-identical to before.
        raise gr.Error(ui_error_text(e))
    finally:
        repo.close()
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
            msg=f"⛔ Job non creato: **{ui_error_text(exc)}**"
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
    except Exception as e: raise gr.Error(ui_error_text(e))
    finally: wf.close()

def _compliance_readiness_html(compliance: dict | None) -> str:
    """3.5C: minimal Release-tab integration for the Compliance gate --
    read-only formatting of the SAME canonical dict release_readiness.py /
    the release snapshot use, never a second Compliance dashboard. No
    active scope -> a neutral note, never a false green checkmark."""
    if not compliance or not compliance.get("compliance_applicable"):
        return "<div class='ms-muted' style='margin-top:10px;font-size:12px'>Conformità non configurata per questa pubblicazione.</div>"
    c = compliance["counts"]
    status = ("<span class='ms-ok'>Conformità: pronta</span>" if compliance["compliance_ready"]
              else "<span class='ms-block'>Conformità: richiede intervento</span>")
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
        "<div class='ms-muted' style='margin-top:6px;font-size:12px'>Stato rispetto ai perimetri normativi "
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
        msg += " Snapshot di conformità del perimetro attivo incluso e hashato."
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
# 4F.2: the raw requirement_id column was dropped from this standard-mode
# table. It was redundant next to Titolo + Riferimento, and once a curated
# pack supplies the ids ("MR2023-A3-17442-TOOLS") it read as an internal
# identifier rather than as information a documentation manager needs.
# The human-facing legal reference stays, which is what an auditor
# actually asks for.
_COMPLIANCE_TABLE_COLS = ["Titolo", "Riferimento", "Stato", "Motivo", "Evidenza"]
# UX Gate 1: the WORKING list is narrower than the full table. "Motivo"
# explains a status in prose and belongs beside the decision controls, not
# in a column the user scans -- it was also the widest column, which is
# what made the old full-width table unreadable at list size.
_COMPLIANCE_LIST_KEYS = ("conformity.col.title", "conformity.col.reference",
                         "conformity.col.status", "conformity.col.evidence")


def compliance_list_cols(lang=None):
    """Header labels for the working list.

    This DataFrame is a RENDERING PAYLOAD, not a data source: it is built
    here and handed straight to Gradio. Its column names are display text
    and so are localized -- and the row dicts below are keyed from this
    same list, so headers and keys can never drift apart."""
    return [i18n.translate(k, lang) for k in _COMPLIANCE_LIST_KEYS]
# Filter TOKENS. Internal identifiers that happen to read as Italian
# words -- exactly like the engine's own "Covered"/"To verify". They are
# what the callbacks branch on and what the Radio submits, so they are
# never translated; only their displayed labels are.
CP_FILTER_TO_COMPLETE = "Da completare"
CP_FILTER_ALL = "Tutti"
CP_FILTER_COVERED = "Coperti"
CP_FILTER_NOT_APPLICABLE = "Non applicabili"
CP_FILTER_DEFAULT = CP_FILTER_TO_COMPLETE
_COMPLIANCE_FILTERS = (CP_FILTER_TO_COMPLETE, CP_FILTER_ALL,
                       CP_FILTER_COVERED, CP_FILTER_NOT_APPLICABLE)
_CP_FILTER_KEYS = {
    CP_FILTER_TO_COMPLETE: "conformity.filter.to_complete",
    CP_FILTER_ALL: "conformity.filter.all",
    CP_FILTER_COVERED: "conformity.filter.covered",
    CP_FILTER_NOT_APPLICABLE: "conformity.filter.not_applicable",
}


def cp_filter_choices(lang=None):
    """(label, token) pairs for the working-list filter Radio."""
    return [(i18n.translate(key, lang), token) for token, key in _CP_FILTER_KEYS.items()]
_COMPLIANCE_EVIDENCE_KEYS = ("conformity.evidence.col.kind", "conformity.evidence.col.reference",
                             "conformity.evidence.col.current", "conformity.evidence.col.linked_by")


def compliance_evidence_cols(lang=None):
    """Header labels for the linked-evidence table (a rendering payload,
    like the working list -- see compliance_list_cols)."""
    return [i18n.translate(k, lang) for k in _COMPLIANCE_EVIDENCE_KEYS]
_COMPLIANCE_DISCLAIMER = (
    "<div class='ms-muted' style='margin-top:8px;font-size:12px'>Stato della documentazione rispetto al perimetro "
    "configurato: non è una dichiarazione di conformità legale, certificazione o marcatura CE. La responsabilità "
    "finale resta del produttore/organizzazione competente.</div>"
)

def _compliance_status_badge(status: str) -> str:
    # Colour keys off the ENGINE status; only the visible text is
    # translated, so the persisted vocabulary stays the single source of
    # truth and nothing downstream has to parse Italian.
    color = _COMPLIANCE_STATUS_COLOR.get(status, "#475467")
    return (f"<span style='color:{color};font-weight:700'>"
            f"{html.escape(cv.status_label(status))}</span>")

def _compliance_dashboard_html(d: dict) -> str:
    if not d.get("has_scope"):
        return ("<div class='ms-prod'><b>Nessuna valutazione di compliance attiva per questa pubblicazione.</b><br>"
                "ManualSync non sta confrontando questa pubblicazione con nessun perimetro normativo al momento — "
                "non significa che sia tutto a posto, significa che non è stata ancora impostata una valutazione. "
                "Seleziona uno scope compliance esistente (o creane uno in modalità avanzata) per iniziare.</div>")
    c = d["counts"]; scope = d.get("scope") or {}
    title = scope.get("title") or scope.get("scope_id") or "Scope"
    jurisdiction = f" · {scope['jurisdiction']}" if scope.get("jurisdiction") else ""
    # UX Gate 1: one compact strip, not a card. The numbers and the
    # non-certification disclaimer are unchanged -- only the chrome around
    # them went, because 185px of header is 185px the requirement list
    # does not get on a 1080px screen.
    parts = [
        "<div class='ms-cp-statusbar'>",
        f"<div class='ms-cp-stat'><span>{t('conformity.stat.scope')}</span>"
        f"<b>{html.escape(str(title))}{html.escape(jurisdiction)}</b></div>",
        f"<div class='ms-cp-stat'><span>{t('conformity.stat.covered')}</span><b>{c['Covered']}</b></div>",
        f"<div class='ms-cp-stat'><span>{t('conformity.stat.to_verify')}</span><b>{c['To verify']}</b></div>",
        f"<div class='ms-cp-stat'><span>{t('conformity.stat.missing')}</span><b>{c['Missing']}</b></div>",
        f"<div class='ms-cp-stat'><span>{t('conformity.stat.not_applicable')}</span>"
        f"<b>{c['Not applicable']}</b></div>",
        f"<div class='ms-cp-stat'><span>{t('conformity.stat.total')}</span><b>{d['total']}</b></div>",
        "</div>",
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
        parts.append("<div class='ms-prod' style='margin-top:6px'>" + " · ".join(stale_bits) + "</div>")
    parts.append(_COMPLIANCE_DISCLAIMER)
    return "".join(parts)

def _compliance_requirement_table(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_COMPLIANCE_TABLE_COLS)
    return pd.DataFrame([{
        "Titolo": r["title"], "Riferimento": r["reference"],
        "Stato": cv.status_label(r["status"]), "Motivo": r["reason"],
        "Evidenza": "Sì" if r["has_evidence"] else "No",
    } for r in rows], columns=_COMPLIANCE_TABLE_COLS)

def _requirement_dropdown_choices(rows: list[dict]) -> list[tuple[str, str]]:
    """(human title, requirement_id) pairs.

    Gradio's convention for tuple choices is (label, value), in that
    order. The requirement_id stays the VALUE -- every callback and the
    audit trail key off it -- but it is no longer shown: an internal
    identifier like "MR2023-A3-17442-MISUSE-WARNINGS" is not a name a
    documentation manager should have to read past to find the title."""
    return [(r["title"], r["requirement_id"]) for r in rows]

def _requirement_label_for(rows: list[dict], rid: str | None) -> str | None:
    """The dropdown's selected VALUE, which is the requirement_id itself.
    Returns None when the id is not in the current list, so Gradio is
    never handed a value that is not among its choices."""
    if not rid:
        return None
    for r in rows:
        if r["requirement_id"] == rid:
            return rid
    return None

def _parse_requirement_id_from_label(label: str | None) -> str:
    return (label or "").split(" — ", 1)[0].strip()

def _fmt_ts(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ") or "—"

def _compliance_detail_placeholder() -> tuple:
    return (
        f"<div class='ms-cp-empty'><div class='ms-cp-empty-icon'>☑</div>"
        f"<div>{t('conformity.detail.empty')}</div></div>",
        "<div class='ms-muted'>—</div>", gr.update(value=None),
        "", "<div class='ms-muted'>—</div>", gr.update(value=None),
        pd.DataFrame(columns=compliance_evidence_cols()),
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
        applic_current = (f"<div class='ms-muted'>{t('conformity.current_decision')} <b>{cv.applicability_label(ap['applies'])}</b>{just}"
                          f"<br>Deciso da <b>{html.escape(ap['decided_by'])}</b> il {_fmt_ts(ap['decided_at'])}</div>")
    else:
        applic_current = f"<div class='ms-muted'>{t('conformity.applicability.not_assessed')}</div>"

    cov_banner = "" if detail["can_decide_coverage"] else (
        "<div class='ms-prod'>La copertura può essere decisa solo dopo aver impostato applicabilità = "
        "«Si applica» qui sopra.</div>")
    asm = detail["assessment"]
    if asm:
        cm = f" — {html.escape(asm['comment'])}" if asm.get("comment") else ""
        cov_current = (f"<div class='ms-muted'>{t('conformity.current_decision')} <b>{cv.coverage_label(asm['status'])}</b>{cm}"
                       f"<br>Deciso da <b>{html.escape(asm['decided_by'])}</b> il {_fmt_ts(asm['decided_at'])}</div>")
    else:
        cov_current = f"<div class='ms-muted'>{t('conformity.coverage.not_assessed')}</div>"

    evidence_rows = detail["evidence"]
    evidence_df = pd.DataFrame([{
        "Tipo": {"topic": "Sezione", "block": "Blocco", "table": "Tabella", "figure": "Figura",
                "external_file": "File esterno", "customer_document": "Documento cliente",
                "human_attestation": "Attestazione umana"}.get(e["evidence_kind"], e["evidence_kind"]),
        "Riferimento": e["object_id"] or e.get("external_ref") or e.get("description") or "—",
        "Aggiornata": "Sì" if e["is_current"] else "No",
        "Collegata da": e["linked_by"],
    } for e in evidence_rows]) if evidence_rows else pd.DataFrame(columns=compliance_evidence_cols())

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
        pd.DataFrame(columns=compliance_list_cols()),
        gr.update(choices=[], value=None),
        *_compliance_detail_placeholder(),
    )

def _compliance_full_bundle(svc, repo, scope_id: str | None, rid: str | None, *, applic_msg: str = "",
                            cov_msg: str = "", evidence_msg: str = "",
                            applic_radio_value: str | None = None, cov_radio_value: str | None = None) -> tuple:
    dash = _compliance_dashboard_html(cv.dashboard(svc, scope_id))
    rows = cv.requirement_list_rows(svc, scope_id) if scope_id else []
    # The bundle refreshes the WORKING list after any decision, using the
    # default filter. Every binding that can change a status chains
    # compliance_list_ui afterwards with the filter the user actually has
    # selected, so their choice wins -- this only guarantees the list is
    # never left stale by a decision.
    table = (_compliance_list_table(_compliance_filtered_rows(svc, scope_id, "", CP_FILTER_DEFAULT))
             if scope_id else pd.DataFrame(columns=compliance_list_cols()))
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

# ----------------------------------------------------------------
# 4F.2 Compliance starter pack -- ISOLATED callbacks with their own small
# outputs. Deliberately NOT folded into _compliance_full_bundle: that
# positional bundle is exactly the shape that turns one partial update
# into a cascade of red Error controls (see the 4E.3 UI regression), and
# this milestone's brief is explicit about not adding another one.
#
# Everything here is documentation TRACEABILITY. No callback in this
# section ever produces a legal verdict: it shows which obligation
# exists, where it comes from, what a human decided, and what evidence
# is linked.
# ----------------------------------------------------------------

def _pack_preview_html(preview: dict) -> str:
    groups = " · ".join(f"{k}: {v}" for k, v in preview["groups"].items())
    std = "".join(
        f"<li>{html.escape(s['designation'])} — {html.escape(s['publisher'])} "
        f"<span class='ms-muted'>(riferimento facoltativo, testo non incluso)</span></li>"
        for s in preview["standards"])
    notice = ""
    if preview["verification_notice"]:
        notice = (f"<div class='ms-cp-notice ms-cp-notice-warn'>"
                  f"{html.escape(preview['verification_notice'])}</div>")
    return (
        f"<div class='ms-cp-pack'>"
        f"<div class='ms-cp-pack-title'>{html.escape(preview['name'])}</div>"
        f"<div class='ms-cp-pack-sub'>{html.escape(preview['summary'])}</div>"
        f"<table class='ms-cp-pack-meta'>"
        f"<tr><td>Fonte ufficiale</td><td>{html.escape(preview['official_identifier'])} — "
        f"<a href='{html.escape(preview['source_url'])}' target='_blank' rel='noopener'>testo ufficiale</a></td></tr>"
        f"<tr><td>Versione pacchetto</td><td>{html.escape(preview['pack_version'])}</td></tr>"
        f"<tr><td>Testo verificato</td><td>{html.escape(preview['source_version'])} "
        f"<span class='ms-muted'>(consolidato {html.escape(preview['source_consolidated_date'])})</span></td></tr>"
        f"<tr><td>Verificato il</td><td>{html.escape(preview['verified_date'])}</td></tr>"
        f"<tr><td>Si applica dal</td><td>{html.escape(preview['applies_from'])}"
        f"<div class='ms-muted' style='font-size:11px'>{html.escape(preview['applies_from_note'])}</div>"
        f"</td></tr>"
        f"<tr><td>Requisiti inclusi</td><td>{preview['requirement_count']} ({html.escape(groups)})</td></tr>"
        f"</table>"
        f"<div class='ms-cp-pack-sub'>Norme di riferimento collegate:</div><ul>{std}</ul>"
        f"{notice}"
        f"<div class='ms-cp-notice'>{html.escape(preview['disclaimer'])}</div>"
        f"</div>"
    )


def _coverage_html(cov: dict) -> str:
    if not cov.get("has_scope"):
        return ("<div class='ms-cp-notice'>Aggiungi un pacchetto di requisiti al progetto per vedere "
                "la copertura documentale.</div>")
    ratio = (f"{cov['ratio_percent']}%" if cov["ratio_percent"] is not None else "—")
    cells = [
        ("Requisiti totali", cov["total"]), ("Valutati", cov["assessed"]),
        ("Da valutare", cov["not_assessed"]), ("Applicabili", cov["applicable"]),
        ("Non applicabili", cov["not_applicable"]), ("Coperti da evidenza", cov["covered"]),
        ("Copertura parziale", cov["partial"]), ("Applicabili senza evidenza", cov["no_evidence"]),
        ("Da rivedere", cov["needs_review"]),
    ]
    kpis = "".join(
        f"<div class='ms-kpi'><div class='ms-kpi-label'>{html.escape(label)}</div><b>{value}</b></div>"
        for label, value in cells)
    # Sources that have not been verified against the official legal text
    # are called out here rather than being folded silently into the
    # numbers above.
    src = ("<div class='ms-cp-notice'>Tutti i requisiti di questo ambito derivano da fonti legali "
           "verificate sul testo ufficiale consolidato.</div>" if cov.get("all_sources_verified")
           else f"<div class='ms-cp-notice ms-cp-notice-warn'>{cov.get('unverified_requirements', 0)} "
                "requisiti derivano da fonti non ancora verificate sul testo ufficiale: valutali "
                "separatamente prima di considerarli autorevoli.</div>")
    return (
        f"<div class='ms-section-title'>{html.escape(cov['title'])}</div>"
        f"<div class='ms-section-sub'>{html.escape(cov['ratio_label'])}: <b>{ratio}</b> "
        f"(sui requisiti giudicati applicabili)</div>"
        f"<div class='ms-kpis'>{kpis}</div>"
        f"{src}"
        f"<div class='ms-cp-notice'>{html.escape(csp.PACK_DISCLAIMER)}</div>"
    )


def _gap_dataframe(rows: list[dict]) -> pd.DataFrame:
    cols = ["Requisito", "Riferimento ufficiale", "Stato", "Evidenze", "Prossimo passo"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{
        "Requisito": r["title"], "Riferimento ufficiale": r["reference"],
        "Stato": r["bucket_label"], "Evidenze": r["evidence_count"],
        "Prossimo passo": r["next_step"],
    } for r in rows], columns=cols)


def compliance_pack_choices_ui():
    return gr.update(choices=[(p["name"], p["pack_id"]) for p in csp.available_packs()], value=None)


def compliance_pack_preview_ui(pack_id):
    if not pack_id:
        return "<div class='ms-cp-notice'>Seleziona un pacchetto per vederne la fonte e il contenuto.</div>"
    try:
        return _pack_preview_html(csp.pack_preview(csp.get_pack(pack_id)))
    except KeyError:
        return "<div class='ms-cp-notice'>Pacchetto non disponibile.</div>"


def compliance_coverage_ui(project_dir, scope_id, lang=None):
    """Read-only: builds the coverage view and the gap list. Never writes."""
    # The session's language, from the ws_lang selector. use_language()
    # scopes it to THIS request and restores it on the way out, so the
    # words one session picked never decide another session's render.
    with i18n.use_language(lang):
        if not project_dir or not scope_id:
            return _coverage_html({"has_scope": False}), _gap_dataframe([])
        repo, svc = cv.open_service(project_dir)
        try:
            return _coverage_html(cv.documentation_coverage(svc, scope_id)), \
                _gap_dataframe(cv.to_complete_rows(svc, scope_id))
        finally:
            repo.close()


def compliance_import_pack_ui(project_dir, publication_id, scope_id, pack_id, group_labels, actor):
    """Explicit "Aggiungi pacchetto al progetto". Creates a scope for the
    publication if none is selected yet, so a first-time project is one
    click away from a usable checklist. Idempotent and atomic by
    construction (see ComplianceService.import_requirement_pack)."""
    if not project_dir:
        return (_export_error_html("Nessun progetto attivo."), gr.update(), gr.update(), gr.update())
    if not pack_id:
        return (_export_error_html("Seleziona prima un pacchetto."), gr.update(), gr.update(), gr.update())
    if not str(actor or "").strip():
        return (_export_error_html("Inserisci il tuo nome: la decisione viene registrata."),
                gr.update(), gr.update(), gr.update())
    groups = [k for k, label in csp.APPLICABILITY_GROUPS.items() if label in (group_labels or [])]
    if not groups:
        return (_export_error_html("Seleziona almeno un ambito di applicabilità."),
                gr.update(), gr.update(), gr.update())
    pub = (publication_id or "PUB-MAIN").strip() or "PUB-MAIN"
    repo, svc = cv.open_service(project_dir)
    try:
        pack = csp.get_pack(pack_id)
        target_scope = scope_id
        if not target_scope:
            target_scope = f"SCOPE-{csp.MACHINERY_DOC_PACK['pack_id']}"
            if not svc.scope_row(target_scope):
                svc.create_scope(target_scope, pub, actor=actor, title=pack["name"], jurisdiction="EU")
        result = svc.import_requirement_pack(pack, scope_id=target_scope, actor=actor, groups=groups)
        opts = cv.scope_options(svc, pub)
        cov_html, gaps = (_coverage_html(cv.documentation_coverage(svc, target_scope)),
                          _gap_dataframe(cv.to_complete_rows(svc, target_scope)))
    except KeyError:
        return (_export_error_html("Pubblicazione o pacchetto non disponibili in questo progetto."),
                gr.update(), gr.update(), gr.update())
    except ValueError as e:
        return (_export_error_html(domain_error_message(e)),
                gr.update(), gr.update(), gr.update())
    except Exception as e:
        _app_log("Compliance pack import failed", e)
        return (_export_error_html("Non è stato possibile aggiungere il pacchetto al progetto."),
                gr.update(), gr.update(), gr.update())
    finally:
        repo.close()
    added, present = len(result["added"]), len(result["already_present"])
    detail = (f"{added} requisiti aggiunti" if added else "Nessun nuovo requisito")
    if present:
        detail += f" · {present} già presenti (nessun duplicato)"
    return (
        f"<div class='ms-export-card'><div class='ms-export-headline'>Pacchetto aggiunto al progetto</div>"
        f"<div class='ms-export-subtitle'>{html.escape(pack['name'])} · {html.escape(detail)}</div>"
        f"<div class='ms-cp-notice'>{html.escape(csp.PACK_DISCLAIMER)}</div></div>",
        gr.update(choices=opts, value=target_scope), cov_html, gaps,
    )


def _pack_update_notice_html(status: dict) -> str:
    """The contained "an update is available" banner.

    Shown only when there is genuinely something to apply, because a
    permanent badge is a badge nobody reads."""
    if not status.get("update_available"):
        return ""
    bits = []
    if status["added"]:
        bits.append(f"{status['added']} requisiti aggiunti")
    if status["changed"]:
        bits.append(f"{status['changed']} requisiti aggiornati")
    if status["retired"]:
        bits.append(f"{status['retired']} requisiti ritirati")
    bits.append(f"{status['unaffected']} invariati")
    return (
        "<div class='ms-cp-notice ms-cp-notice-warn'>"
        f"<b>{html.escape(cv.UPDATE_AVAILABLE_TITLE)}</b><br>"
        f"{html.escape(status['pack_name'])} · versione nel progetto "
        f"{html.escape(status['current_pack_version'] or '—')} → disponibile "
        f"{html.escape(status['available_pack_version'] or '—')}<br>"
        f"{html.escape(' · '.join(bits))}</div>"
    )


def _pack_update_preview_html(preview: dict) -> str:
    """Human-readable preview: one line per affected obligation, with the
    legal reference and what kind of change it is. Never a raw diff."""
    if not preview.get("installed"):
        return ("<div class='ms-cp-notice'>Il pacchetto non è ancora presente in questo progetto.</div>")
    if not preview.get("update_available"):
        return f"<div class='ms-cp-notice'>{html.escape(cv.UPDATE_NONE_MESSAGE)}</div>"
    c = preview["counts"]
    head = (
        "<div class='ms-export-card'>"
        f"<div class='ms-export-headline'>Anteprima aggiornamento</div>"
        f"<div class='ms-export-subtitle'>{html.escape(preview['pack_name'])} · "
        f"{html.escape(preview['current_pack_version'] or '—')} → "
        f"{html.escape(preview['available_pack_version'] or '—')} · "
        f"testo verificato {html.escape(preview['source_version'] or '—')}</div>"
        f"<div class='ms-export-subtitle'>{c['added']} aggiunti · {c['changed']} aggiornati · "
        f"{c['retired']} ritirati · {c['unaffected']} invariati</div>"
    )
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(r['title'])}</td>"
        f"<td>{html.escape(r['reference'])}</td>"
        f"<td>{html.escape(r['change'])}</td>"
        f"<td>{html.escape(r['detail'])}</td>"
        "</tr>" for r in preview["rows"])
    return (head + "<table class='ms-cp-table'><thead><tr><th>Requisito</th><th>Riferimento</th>"
            "<th>Modifica</th><th>Effetto</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def _pack_state_html(status: dict) -> str:
    """The always-visible "which pack am I on, and is it current?" strip.

    Answers, without the user opening anything: which pack is active,
    which version, which official text it was verified against, and
    whether a newer revision is waiting. Before this existed the version
    was only reachable by opening a setup panel and picking the pack out
    of a dropdown -- which is not an answer a user should have to hunt
    for."""
    if not status.get("installed"):
        return ("<div class='ms-cp-notice'><b>Nessun pacchetto di requisiti in questo progetto.</b><br>"
                "Apri «Aggiungi un pacchetto di requisiti» in fondo a questa pagina per partire da un "
                "elenco curato di obblighi documentali.</div>")
    # One line in the header. The full provenance (verified source text,
    # applicability scope) moved to "Aggiornamenti normativi", where the
    # question it answers is actually being asked.
    head = (
        "<div class='ms-cp-packline'>"
        f"<span class='ms-cp-packbar-label'>{t('conformity.pack.line_label')}</span> "
        f"<b>{html.escape(status['pack_name'])}</b>"
        f" · v{html.escape(status['current_pack_version'] or '—')}"
        f" · {html.escape(' · '.join(status['groups']) or '—')}"
        "</div>"
    )
    if status.get("update_available"):
        return head + _pack_update_notice_html(status)
    return head


def _pack_provenance_html(status: dict) -> str:
    """Full provenance for the 'Aggiornamenti normativi' tab: which text,
    which consolidated version, which scope. Not in the working header --
    nobody reads a CELEX identifier while deciding applicability -- but
    never hidden either, because it is the answer to 'why should I trust
    this obligation?'."""
    if not status.get("installed"):
        return ""
    rows = [
        ("Pacchetto", status.get("pack_name", "")),
        ("Versione nel progetto", status.get("current_pack_version") or "—"),
        ("Ambito", " · ".join(status.get("groups") or []) or "—"),
        ("Testo ufficiale verificato", status.get("source_version") or "—"),
    ]
    cells = "".join(
        f"<div><div class='ms-cp-packbar-label'>{html.escape(k)}</div>"
        f"<b>{html.escape(str(v))}</b></div>" for k, v in rows)
    return f"<div class='ms-cp-packbar'>{cells}</div>"


def compliance_pack_state_ui(project_dir, lang=None):
    """Read-only. Renders the pack strip AND decides whether the update
    actions are shown at all.

    The update buttons stay hidden until there is genuinely something to
    apply: a permanently visible "Aggiorna pacchetto" invites a click
    that does nothing, and makes package administration look like the
    main task on a screen whose main task is documentation coverage."""
    # The session's language, from the ws_lang selector. use_language()
    # scopes it to THIS request and restores it on the way out, so the
    # words one session picked never decide another session's render.
    with i18n.use_language(lang):
        if not project_dir:
            return "", gr.update(visible=False), ""
        repo, svc = cv.open_service(project_dir)
        try:
            status = cv.pack_update_status(svc, csp.MACHINERY_DOC_PACK)
            return (_pack_state_html(status),
                    gr.update(visible=bool(status.get("update_available"))),
                    _pack_provenance_html(status))
        except Exception as e:
            # A failed status CHECK must never block the compliance screen.
            _app_log("Pack state check failed", e)
            return "", gr.update(visible=False), ""
        finally:
            repo.close()


def compliance_pack_update_preview_ui(project_dir):
    """Explicit "Visualizza aggiornamento". Read-only."""
    if not project_dir:
        return _export_error_html("Nessun progetto attivo.")
    repo, svc = cv.open_service(project_dir)
    try:
        return _pack_update_preview_html(cv.pack_update_preview(svc, csp.MACHINERY_DOC_PACK))
    except Exception as e:
        _app_log("Pack update preview failed", e)
        return _export_error_html("Non è stato possibile calcolare l'anteprima dell'aggiornamento.")
    finally:
        repo.close()


def compliance_pack_update_apply_ui(project_dir, scope_id, actor):
    """Explicit "Aggiorna pacchetto".

    Returns the success card, the refreshed preview panel (which now
    reports the pack is current), and the refreshed coverage/gap views,
    so the whole screen is consistent immediately afterwards. The pack
    strip and the visibility of the update row are refreshed by the
    chained compliance_pack_state_ui."""
    if not project_dir:
        return (_export_error_html("Nessun progetto attivo."), gr.update(), gr.update(), gr.update())
    if not scope_id:
        return (_export_error_html("Seleziona prima uno scope compliance."),
                gr.update(), gr.update(), gr.update())
    if not str(actor or "").strip():
        return (_export_error_html("Inserisci il tuo nome: l'aggiornamento viene registrato."),
                gr.update(), gr.update(), gr.update())
    repo, svc = cv.open_service(project_dir)
    try:
        result = cv.apply_pack_update(svc, csp.MACHINERY_DOC_PACK, scope_id, actor)
        preview_html = _pack_update_preview_html(cv.pack_update_preview(svc, csp.MACHINERY_DOC_PACK))
        cov_html = _coverage_html(cv.documentation_coverage(svc, scope_id))
        gaps = _gap_dataframe(cv.to_complete_rows(svc, scope_id))
    except (KeyError, ValueError) as e:
        # cv.apply_pack_update raises a DomainError (ERR_PACK_NOT_INSTALLED),
        # whose str() is the bare CODE by design. Printing it here put
        # "ERR_PACK_NOT_INSTALLED" on screen; resolve it like every other
        # migrated boundary. A non-domain KeyError/ValueError still lands in
        # the generic sentence rather than being dressed up as validation.
        return (_export_error_html(domain_error_message(e)),
                gr.update(), gr.update(), gr.update())
    except Exception as e:
        _app_log("Pack update failed", e)
        return (_export_error_html("Non è stato possibile aggiornare il pacchetto."),
                gr.update(), gr.update(), gr.update())
    finally:
        repo.close()
    bits = []
    if result["added"]:
        bits.append(f"{len(result['added'])} aggiunti")
    if result["updated_editorial"]:
        bits.append(f"{len(result['updated_editorial'])} allineati al testo ufficiale")
    if result["updated_substantive"]:
        bits.append(f"{len(result['updated_substantive'])} da riverificare")
    if result["retired"]:
        bits.append(f"{len(result['retired'])} ritirati")
    detail = " · ".join(bits) if bits else "Nessuna modifica da applicare"
    note = ("<div class='ms-cp-notice'>Le decisioni di applicabilità e le evidenze del progetto sono "
            "state mantenute. I requisiti il cui obbligo è cambiato tornano in «Da verificare».</div>"
            if result["updated_substantive"] else
            "<div class='ms-cp-notice'>Le decisioni di applicabilità e le evidenze del progetto sono "
            "state mantenute.</div>")
    return (
        f"<div class='ms-export-card'><div class='ms-export-headline'>{html.escape(result['message'])}</div>"
        f"<div class='ms-export-subtitle'>{html.escape(csp.MACHINERY_DOC_PACK['name'])} · "
        f"versione {html.escape(result['pack_version'])} · {html.escape(detail)}</div>"
        f"{note}</div>",
        preview_html, cov_html, gaps,
    )


def compliance_load_ui(project_dir, publication_id, lang=None):
    # The session's language, from the ws_lang selector. use_language()
    # scopes it to THIS request and restores it on the way out, so the
    # words one session picked never decide another session's render.
    with i18n.use_language(lang):
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

def compliance_switch_scope_ui(project_dir, scope_id, lang=None):
    # The session's language, from the ws_lang selector. use_language()
    # scopes it to THIS request and restores it on the way out, so the
    # words one session picked never decide another session's render.
    with i18n.use_language(lang):
        if not project_dir:
            return _compliance_full_bundle_empty()
        repo, svc = cv.open_service(project_dir)
        try:
            return _compliance_full_bundle(svc, repo, scope_id, None)
        finally:
            repo.close()

def compliance_open_requirement_ui(project_dir, scope_id, requirement_label, lang=None):
    # The session's language, from the ws_lang selector. use_language()
    # scopes it to THIS request and restores it on the way out, so the
    # words one session picked never decide another session's render.
    with i18n.use_language(lang):
        rid = _parse_requirement_id_from_label(requirement_label)
        if not project_dir:
            return _compliance_full_bundle_empty()
        repo, svc = cv.open_service(project_dir)
        try:
            return _compliance_full_bundle(svc, repo, scope_id, rid or None)
        finally:
            repo.close()

def _compliance_list_table(rows: list[dict]) -> pd.DataFrame:
    """The four-column working list."""
    cols = compliance_list_cols()
    if not rows:
        return pd.DataFrame(columns=cols)
    yes, no = t("conformity.value.yes"), t("conformity.value.no")
    return pd.DataFrame([dict(zip(cols, (
        r["title"], r.get("reference_short") or r["reference"],
        cv.status_label(r["status"]), yes if r["has_evidence"] else no,
    ))) for r in rows], columns=cols)


def _compliance_filtered_rows(svc, scope_id, search, status_filter):
    """Rows for the working list, filtered and searched.

    Pure presentation over cv.requirement_list_rows: no engine call is
    added and no status is recomputed here. "Da completare" reuses
    cv.to_complete_rows so the list and the Panoramica queue can never
    disagree about what still needs a human."""
    rows = cv.requirement_list_rows(svc, scope_id)
    f = str(status_filter or CP_FILTER_DEFAULT)
    if f == CP_FILTER_TO_COMPLETE:
        pending = {r["requirement_id"] for r in cv.to_complete_rows(svc, scope_id)}
        rows = [r for r in rows if r["requirement_id"] in pending]
    elif f == CP_FILTER_COVERED:
        rows = [r for r in rows if r["status"] == "Covered"]
    elif f == CP_FILTER_NOT_APPLICABLE:
        rows = [r for r in rows if r["status"] == "Not applicable"]
    q = str(search or "").strip().lower()
    if q:
        rows = [r for r in rows
                if q in (r["title"] or "").lower() or q in (r["reference"] or "").lower()]
    return rows


def compliance_list_ui(project_dir, scope_id, search, status_filter, lang=None):
    """Read-only. Rebuilds the working list for the current search/filter."""
    # The session's language, from the ws_lang selector. use_language()
    # scopes it to THIS request and restores it on the way out, so the
    # words one session picked never decide another session's render.
    with i18n.use_language(lang):
        if not project_dir or not scope_id:
            return pd.DataFrame(columns=compliance_list_cols())
        repo, svc = cv.open_service(project_dir)
        try:
            return _compliance_list_table(_compliance_filtered_rows(svc, scope_id, search, status_filter))
        except Exception as e:
            _app_log("Compliance list filter failed", e)
            return pd.DataFrame(columns=compliance_list_cols())
        finally:
            repo.close()


def compliance_open_requirement_row_ui(project_dir, scope_id, search, status_filter,
                                       lang=None, evt: gr.SelectData = None):
    """Opens the requirement whose ROW was clicked in the "Requisiti"
    table.

    The table is the thing a user is already looking at, so clicking a
    line in it is the obvious way to open that line. The row index is
    resolved against a freshly computed list rather than against whatever
    the dataframe happens to hold, so a stale client-side table can never
    open the wrong requirement -- it just opens nothing."""
    # The session's language, from the ws_lang selector. use_language()
    # scopes it to THIS request and restores it on the way out, so the
    # words one session picked never decide another session's render.
    with i18n.use_language(lang):
        if not project_dir or not scope_id or evt is None or evt.index is None:
            return _compliance_full_bundle_empty()
        row_index = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        repo, svc = cv.open_service(project_dir)
        try:
            rows = _compliance_filtered_rows(svc, scope_id, search, status_filter)
            if not isinstance(row_index, int) or not (0 <= row_index < len(rows)):
                return _compliance_full_bundle(svc, repo, scope_id, None)
            return _compliance_full_bundle(svc, repo, scope_id, rows[row_index]["requirement_id"])
        finally:
            repo.close()


def compliance_decide_applicability_ui(project_dir, scope_id, rid, applies_label, reviewer, justification, comment,
                                       cur_cov_radio=None, lang=None):
    # The session's language, so a refusal is worded for whoever is
    # looking at it. Without this the resolver would fall back to the
    # default and an English session would read an Italian refusal.
    with i18n.use_language(lang):
        if not project_dir:
            raise gr.Error("Nessun progetto attivo.")
        repo, svc = cv.open_service(project_dir)
        try:
            msg = ""
            failed = False
            if scope_id and rid:
                # The Radio carries the engine code, not the visible word, so
                # this works identically in every language and no translated
                # string is ever handed to the service.
                applies_code = applies_label if applies_label in cv.APPLICABILITY_CODES else ""
                if not applies_code:
                    msg = "⛔ " + t("conformity.applicability.choose_first",
                                    options=" / ".join(cv.applicability_labels().values()))
                    failed = True
                else:
                    try:
                        svc.set_applicability(scope_id, rid, applies_code, actor=reviewer or "",
                                              justification=justification or "", comment=comment or "")
                        msg = "✅ Decisione registrata."
                    except Exception as exc:
                        msg = "⛔ " + domain_error_message(exc)
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

def compliance_decide_coverage_ui(project_dir, scope_id, rid, coverage_label, reviewer, comment,
                                  cur_applic_radio=None, lang=None):
    # The session's language, so a refusal is worded for whoever is
    # looking at it. Without this the resolver would fall back to the
    # default and an English session would read an Italian refusal.
    with i18n.use_language(lang):
        if not project_dir:
            raise gr.Error("Nessun progetto attivo.")
        repo, svc = cv.open_service(project_dir)
        try:
            msg = ""
            failed = False
            if scope_id and rid:
                cov_code = coverage_label if coverage_label in cv.COVERAGE_CODES else ""
                if not cov_code:
                    msg = "⛔ " + t("conformity.coverage.choose_first",
                                    options=" / ".join(cv.coverage_labels().values()))
                    failed = True
                else:
                    try:
                        svc.set_assessment(scope_id, rid, cov_code, actor=reviewer or "", comment=comment or "")
                        msg = "✅ Decisione registrata."
                    except Exception as exc:
                        msg = "⛔ " + domain_error_message(exc)
                        failed = True
            # Same non-destructive-on-failure fix as applicability, see there --
            # and the same "don't touch the OTHER radio" symmetry.
            return _compliance_full_bundle(svc, repo, scope_id, rid, cov_msg=msg,
                                           cov_radio_value=coverage_label if failed else None,
                                           applic_radio_value=cur_applic_radio)
        finally:
            repo.close()

def compliance_link_evidence_ui(project_dir, scope_id, rid, candidate_label, reviewer,
                                cur_applic_radio=None, cur_cov_radio=None, lang=None):
    # The session's language, so a refusal is worded for whoever is
    # looking at it. Without this the resolver would fall back to the
    # default and an English session would read an Italian refusal.
    with i18n.use_language(lang):
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
                        msg = "⛔ " + domain_error_message(exc)
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
                                  cur_applic_radio=None, cur_cov_radio=None, lang=None):
    # The session's language, so a refusal is worded for whoever is
    # looking at it. Without this the resolver would fall back to the
    # default and an English session would read an Italian refusal.
    with i18n.use_language(lang):
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
                        msg = "⛔ " + domain_error_message(exc)
            # Same symmetry as link: unlinking must never reset a pending
            # applicability/coverage choice either.
            return _compliance_full_bundle(svc, repo, scope_id, rid, evidence_msg=msg,
                                           applic_radio_value=cur_applic_radio, cov_radio_value=cur_cov_radio)
        finally:
            repo.close()


# ======================================================================
# Localized-component registry.
#
# Gradio bakes labels into the app config when the Blocks tree is built,
# so a language change reaches the screen only as an explicit update per
# component. That part is unavoidable. What IS avoidable -- and what this
# replaces -- is maintaining that by hand as TWO parallel lists: one of
# updates and one of components, in matching order. Sixty-eight entries
# already needed an AST check to prove the two agreed; at the ~300 the
# remaining sections would bring, a silent off-by-one is a certainty.
#
# So there is one list. components() and updates() are both derived from
# it, in the same order, and cannot disagree. Registering a component is
# one call at its definition site; the switch callback never grows again.
#
# A builder is either a catalog KEY (resolved with t()) or a callable
# returning the finished value -- the second form is for HTML blocks and
# for labels with an icon prefix.
# ======================================================================
class LocalizedUI:
    """Components whose visible text comes from the catalog."""

    def __init__(self):
        self._entries: list[tuple[object, dict]] = []

    def add(self, component, **builders):
        """Register `component`; returns it, so it reads as a wrapper."""
        self._entries.append((component, builders))
        return component

    def html(self, builder, **kwargs):
        """A gr.HTML whose whole body is rebuilt from `builder()`."""
        return self.add(gr.HTML(builder(), **kwargs), value=builder)

    def button(self, builder, **kwargs):
        """A gr.Button whose caption is rebuilt from `builder()`."""
        return self.add(gr.Button(builder(), **kwargs), value=builder)

    def components(self) -> list:
        return [component for component, _ in self._entries]

    def updates(self) -> list:
        out = []
        for _, builders in self._entries:
            out.append(gr.update(**{prop: (build() if callable(build) else t(build))
                                    for prop, build in builders.items()}))
        return out

    def __len__(self) -> int:
        return len(self._entries)


LOC = LocalizedUI()


def switch_language_ui(code):
    """Apply a language to every registered component.

    The language is scoped to THIS request: use_language() restores it on
    the way out, so one browser session's choice can never decide what
    another session's next render says.
    """
    with i18n.use_language(code):
        return LOC.updates()


with gr.Blocks(title=BRAND_NAME) as demo:
    project_state=gr.State(INITIAL_PROJECT)
    # ================================================================
    # UX Gate 2A — single sources of truth for the two coordinates that
    # were being asked for twice on the same screen.
    #
    # actor_state: WHO is editing. Authoring's mutation callbacks all
    #   read this State (never a Textbox directly -- see the 4B.2 note
    #   at its former declaration for the sync reason). It used to be
    #   fed by a second "Il tuo nome" field declared inside Contenuti,
    #   sitting 800px above a context bar that already asks for exactly
    #   the same thing. Now fed from ws_operator below, which is the
    #   one place the question is asked.
    #
    # profile_state: WHICH variant profile. Contenuti's per-object
    #   evaluator and the publishing preview/export used to own two
    #   independent gr.Dropdowns, both labelled "Profilo", both built
    #   from the byte-identical comprehension
    #       [(p["name"], p["profile_id"])
    #        for p in svc.list_variant_profiles(include_inactive=False)]
    #   and both passing the result to a variant-profile service call.
    #   Same concept, two controls, no link: a user could evaluate a
    #   block against one profile and export against another with
    #   nothing on screen saying so. Both selectors now read and write
    #   this State, so they cannot diverge.
    # ================================================================
    actor_state = gr.State("")
    profile_state = gr.State(None)
    with gr.Sidebar(width=236, elem_id="ms-sidebar"):
        gr.HTML(f"<div class='ms-brand'><div class='ms-logo'>M</div><div><div class='ms-brand-title'>{BRAND_NAME}</div><div class='ms-brand-sub'>Brand provvisorio · Technical Content Intelligence</div></div></div>")
        nav_home=LOC.button(lambda: f"⌂   {t('nav.overview')}",variant="primary")
        side_workflow=LOC.html(lambda: f"<div class='ms-side-section'>{t('kicker.workflow')}</div>")
        nav_revision=LOC.button(lambda: f"◫   {t('nav.revision')}",variant="secondary")
        nav_authoring=LOC.button(lambda: f"✎   {t('nav.content')}",variant="secondary")
        nav_compliance=LOC.button(lambda: f"☑   {t('nav.conformity')}",variant="secondary")
        nav_translation=LOC.button(lambda: f"⇄   {t('nav.translation')}",variant="secondary")
        nav_jobs=LOC.button(lambda: f"▣   {t('nav.jobs')}",variant="secondary")
        nav_release=LOC.button(lambda: f"✓   {t('nav.release')}",variant="secondary")
        side_system=LOC.html(lambda: f"<div class='ms-side-section'>{t('kicker.system')}</div>")
        nav_settings=LOC.button(lambda: f"⚙   {t('nav.settings')}",variant="secondary")
        nav_help=LOC.button(lambda: f"?   {t('nav.help')}", interactive=False)
        # Discreet, and deliberately the LAST thing in the sidebar: a
        # language switch is a setting, not a workflow step. One language
        # at a time -- never a bilingual interface.
        ws_lang=gr.Radio([(i18n.LANGUAGE_NAMES[c], c) for c in i18n.SUPPORTED_LANGUAGES],
                         value=i18n.get_language(), show_label=False,
                         elem_classes=["ms-lang"], container=False)
        sidebar_context=gr.HTML(_sidebar_context(INITIAL_PROJECT))

    # ================================================================
    # UX Gate 1 — persistent workspace context.
    #
    # Three coordinates that operational pages kept asking for again and
    # again, hoisted to shell level so they are chosen ONCE.
    #
    # What is here, and why only these three:
    #   * Progetto      — read-only display of the active project. Already
    #                     persisted on disk; shown so the user always knows
    #                     which project an action will affect.
    #   * Pubblicazione — the manual being worked on. Genuine cross-cutting
    #                     context: Conformità, publishing and release all
    #                     key off it.
    #   * Operatore     — WHO is working. Every compliance decision is
    #                     audited with an actor, and in a single-user
    #                     desktop product that actor is the same person all
    #                     day. It was being re-typed in three separate
    #                     panels inside Conformità alone.
    #
    # What is deliberately NOT here:
    #   * Profilo di variante — it looks like context but is not: it is a
    #     RENDERING choice that selects which variant to preview or
    #     publish, and it has no meaning at all in Conformità (which keys
    #     off a normative perimeter, not a variant). Putting it in the
    #     shell would imply it affects every screen. It stays in the
    #     publishing area where it actually applies.
    #   * Revisore umano (Lavorazioni) / Release manager (Rilascio) —
    #     these are ROLE-SPECIFIC approvers on explicit human-approval
    #     gates, not "who is using the app". Collapsing them into one
    #     workspace identity would let a QA approval inherit an identity
    #     the user never re-affirmed for that decision. They keep their
    #     own fields.
    #
    # Persistence: project persists (it already did). Publication and
    # operator are SESSION state and are never written to disk -- an audit
    # identity restored silently at the next launch could attribute a new
    # user's decisions to whoever used the machine last.
    # ================================================================
    with gr.Row(elem_classes=["ms-ctxbar"]):
        ws_project = LOC.add(gr.HTML(_workspace_context_html(INITIAL_PROJECT)),
                             value=lambda: _workspace_context_html(_load_active_project()))
        cp_pub = LOC.add(gr.Textbox(label=t("context.publication"), value="PUB-MAIN", scale=1,
                                    info=t("context.publication.hint")),
                         label="context.publication", info="context.publication.hint")
        ws_operator = LOC.add(gr.Textbox(label=t("context.operator"), value="", scale=1,
                                         placeholder=t("context.operator.placeholder"),
                                         info=t("context.operator.hint")),
                              label="context.operator", info="context.operator.hint",
                              placeholder="context.operator.placeholder")
    # UX Gate 2A: the shell operator now feeds Authoring's actor_state.
    # .change() rather than .input() for the reason established in 4B.2
    # against this exact Gradio build: .input() on a plain gr.Textbox
    # never reached the backend, .change() fired every time -- and
    # .change()'s "also fires on programmatic pushes" caveat is harmless
    # here because ws_operator is only ever an input, never an output.
    ws_operator.change(lambda v: v, [ws_operator], [actor_state])

    with gr.Tabs(selected="home", elem_id="workspace-tabs") as workspace_tabs:
        with gr.Tab(t("nav.overview"), id="home") as tab_home:
            LOC.add(tab_home, label="nav.overview")
            with gr.Row(elem_classes=["ms-pagebar"]):
                head_home=LOC.html(lambda: f"<div class='ms-pagehead'><div>"
                        f"<div class='ms-page-kicker'>{t('kicker.workspace')}</div>"
                        f"<h1>{t('page.overview.title')}</h1><p>{t('home.subtitle')}</p></div></div>")
                home_new_project=LOC.add(gr.Button(t("home.new_project"),variant="primary",
                                                   elem_classes=["ms-new-project-btn"]),
                                     value="home.new_project")
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
                    home_revision=LOC.add(gr.Button(t("home.cta.revision"),elem_classes=["ms-card-cta"]), value="home.cta.revision")
                with gr.Column(elem_classes=["ms-quick-card"]):
                    gr.HTML("<div class='ms-quick-icon'>文</div><div class='ms-quick-title'>Genera pacchetto lingua</div><div class='ms-quick-desc'>Prepara XLIFF e code di revisione per le lingue target.</div>")
                    home_translation=LOC.add(gr.Button(t("home.cta.translation"),elem_classes=["ms-card-cta"]), value="home.cta.translation")
                # 4F.2 UX: Compliance had no entry point from the project
                # workspace itself -- only the sidebar. A user who did not
                # already know the area existed had no reason to open it.
                with gr.Column(elem_classes=["ms-quick-card"]):
                    gr.HTML("<div class='ms-quick-icon'>☑</div><div class='ms-quick-title'>Copertura documentale</div><div class='ms-quick-desc'>Requisiti applicabili, evidenze collegate e cosa manca ancora.</div>")
                    home_compliance=LOC.add(gr.Button(t("home.cta.conformity"),elem_classes=["ms-card-cta"]), value="home.cta.conformity")
                with gr.Column(elem_classes=["ms-quick-card","brand"]):
                    gr.HTML("<div class='ms-quick-icon'>✓</div><div class='ms-quick-title'>Verifica readiness</div><div class='ms-quick-desc'>Controlla i gate e prepara una release controllata.</div>")
                    home_release=LOC.add(gr.Button(t("home.cta.release"),elem_classes=["ms-card-cta"]), value="home.cta.release")
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

        with gr.Tab(t("nav.revision"),id="revision") as tab_revision:
            LOC.add(tab_revision, label="nav.revision")
            head_revision=LOC.html(lambda: _page_head_html("kicker.workflow", "page.revision.title", "page.revision.subtitle"))
            with gr.Row(): a_old=gr.File(label="Vecchio originale PDF",type='filepath'); a_tr=gr.File(label="Vecchia traduzione PDF",type='filepath'); a_new=gr.File(label="Nuovo originale PDF",type='filepath')
            a_full=gr.File(label="Nuovo manuale completo — opzionale",type='filepath'); a_btn=gr.Button("Analizza revisione",variant='primary'); a_summary=gr.HTML(); a_queue=gr.Dataframe(interactive=False,wrap=True,max_height=600)
            with gr.Row(): a_report=gr.File(label="Rapporto"); a_queue_file=gr.File(label="Coda di lavoro")
            a_btn.click(revision_analysis_ui,[project_state,a_old,a_tr,a_new,a_full],[a_summary,a_queue,a_report,a_queue_file])

            # ------------------------------------------------------------
            # UX Gate 2A: the grounded-proposal review queue, moved here
            # from Contenuti. Deciding on someone else's proposal is a
            # REVIEW act, not an authoring one -- it was three accordions
            # deep inside the writing surface (Dettagli tecnici > Coda di
            # revisione) on a tab whose job is trova -> apri -> modifica
            # -> salva. Behaviour is untouched: same callbacks, same
            # decide_review() gate, same fail-closed Draft/In Review hold,
            # and the reviewer keeps its own explicit identity field
            # rather than inheriting the workspace operator.
            # ------------------------------------------------------------
            with gr.Accordion(t("revision.queue.section"), open=False) as rv_accordion:
                LOC.add(rv_accordion, label="revision.queue.section")
                LOC.add(gr.Markdown(t("revision.queue.hint")), value="revision.queue.hint")
                rv_refresh=gr.Button("Aggiorna coda"); rv_queue=gr.Dataframe(interactive=False,max_height=300)
                with gr.Row(): rv_review_id=gr.Number(label="Review ID",precision=0); rv_decision=gr.Radio(["APPROVE","REJECT"],value="APPROVE",label="Decisione"); rv_actor=gr.Textbox(label="Revisore")
                rv_comment=gr.Textbox(label="Commento — opzionale")
                rv_decide=gr.Button("Decidi",variant="primary"); rv_msg=gr.Markdown()
                rv_refresh.click(review_queue_ui,[project_state],[rv_queue])
                rv_decide.click(review_decide_ui,[project_state,rv_review_id,rv_decision,rv_actor,rv_comment],[rv_msg,rv_queue])

        with gr.Tab(t("nav.content"),id="authoring") as authoring_tab:
            LOC.add(authoring_tab, label="nav.content")
            head_content=LOC.html(lambda: _page_head_html("kicker.workflow", "page.content.title", "page.content.subtitle"))

            # UX Gate 2A: the "La tua identità" / "Il tuo nome" field that
            # used to open this tab is gone. It was a second copy of the
            # shell context bar's Operatore field, asked for again ~800px
            # below where the user had already answered it -- one of the
            # duplicated labels the discovery measured on this page. The
            # backend actor requirement is untouched and still fail-closed:
            # every mutation callback below reads actor_state exactly as
            # before, and actor_state is now fed from ws_operator. Distinct
            # reviewer roles (review-decide, grounded queue) keep their own
            # explicit "Revisore" fields rather than inheriting this one.


            # ----------------------------------------------------------------
            # Milestone 4B.2: Professional Topic & Block Editor ("Studio").
            # Component LAYOUT only here (kept visually prominent, right where
            # a technical writer lands on this tab); the .click()/.blur()
            # WIRING is deferred to just below c_pub's own definition further
            # down this same Blocks scope, since the Studio reuses that exact
            # Publication ID field -- "non inventare una seconda nozione di
            # documento aperto" -- rather than declare a second one here.
            # ----------------------------------------------------------------
            # UX Gate 2A: the "Editor professionale" heading is gone. It
            # labelled a section that is now the entire centre of the
            # screen -- a caption for the obvious, costing 41px directly
            # above the writing surface. st_pub_header (which manual am I
            # in) moved into the left context pane, where the rest of the
            # navigation context lives; it is still driven by exactly the
            # same ST_OUT_FULL slot.
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
            # ================================================================
            # UX Gate 2A — the three-zone authoring workspace.
            #
            #   left   (ms-pane-left)   navigate: structure, search, creation
            #   centre (ms-pane-centre) write: the open object and Save
            #   right  (ms-pane-right)  context: what is true of THIS object
            #
            # The zones are plain gr.Column with elem_classes; sticky
            # positioning and per-pane scrolling are CSS (see ms-pane-* in
            # the stylesheet). No custom DOM, no JS layout: the discovery
            # gate verified in the live browser that this is enough, and a
            # custom document tree is deliberately deferred.
            # ================================================================
            with gr.Row(elem_classes=["ms-authoring-workspace"]):
                with gr.Column(scale=2, elem_classes=["ms-pane", "ms-pane-left"]):
                    st_pub_header = gr.HTML()
                    LOC.add(gr.HTML(_section_head_html("content.pane.structure")),
                            value=lambda: _section_head_html("content.pane.structure"))
                    # UX clarity fix: replaces the old technical Dataframe as
                    # the primary standard-mode navigation -- a real document
                    # tree (topic rows, blocks indented beneath with human
                    # type labels + a short preview, no technical IDs, no
                    # horizontal scrollbar), one click to open. The Dataframe
                    # is kept for diagnostics only, in the right pane's
                    # "Dettagli tecnici (modalità avanzata)".
                    st_outline_radio = gr.Radio(choices=[], show_label=False, value=None)
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
                    # UX Gate 2A: topic prev/next belongs with navigation.
                    # It used to sit at the top of the centre pane, above
                    # the title and the editor, pushing the writing surface
                    # down by its own height on every object -- a movement
                    # control charging rent on the writing area.
                    with gr.Row():
                        st_prev_btn = gr.Button("‹ Topic precedente")
                        st_next_btn = gr.Button("Topic successivo ›")
                with gr.Column(scale=5, elem_classes=["ms-pane", "ms-pane-centre"]):
                    # Side by side, not stacked: together they are one line
                    # of "what is open and what state is it in", and stacking
                    # them spent 40px of the space above the editor saying it
                    # twice over.
                    with gr.Row(elem_classes=["ms-editor-contextline"]):
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
                    st_title_input = gr.Textbox(label="Titolo", visible=False)
                    # ----------------------------------------------------------------
                    # 4E.3 stabilization (Blocker 1): the writer-facing standard
                    # editor is now a lightweight `contenteditable` surface
                    # (st_rich_editable, a gr.HTML component whose rendered value
                    # embeds a real `contenteditable="true"` div) -- NOT a plain
                    # Textbox exposing canonical markdown syntax as the normal
                    # authoring surface. Gradio 6.5.1 has no native rich-text/
                    # WYSIWYG component (confirmed against the installed package --
                    # gr.Markdown is display-only, gr.Textbox is plain), so a full
                    # custom editor framework would be the only alternative -- this
                    # instead reuses the BROWSER's own native contenteditable +
                    # document.execCommand (bold/italic/lists are real native
                    # browser commands; only "code span" and "safe link" need a
                    # few extra lines of JS), which is the lowest-risk path that
                    # still gives a genuine visual (not markup-typing) surface.
                    #
                    # st_text_input remains in the graph but is now PERMANENTLY
                    # hidden -- purely the canonical-text "wire" between the
                    # contenteditable's live DOM state and Python (studio_save_ui,
                    # draft autosave). A hidden REAL Textbox (with a real
                    # <textarea> Gradio's frontend binds to) is the proven
                    # mechanism already used for state relay in this app: JS sets
                    # its .value and dispatches a genuine 'input'/'blur' DOM event,
                    # and Gradio's own frontend picks the change up exactly as it
                    # would for a real keystroke -- no new mechanism, same one
                    # already validated for the toolbar buttons below.
                    st_text_input = gr.Textbox(label="Testo", lines=10, visible=False, elem_id="ms-rich-text-input")
                    # Toolbar: full-word Italian labels (never icon-only) so a
                    # tooltip isn't even load-bearing for understanding what a
                    # button does. Each button targets the contenteditable's
                    # SELECTION directly (execCommand, or a small manual insertion
                    # for Codice/Link, which execCommand has no native command
                    # for) -- the writer never manages **/_/`` syntax tokens
                    # themselves; that's an internal storage detail invisible here.
                    with gr.Row(elem_classes=["ms-rich-toolbar"], visible=False) as st_rich_toolbar_row:
                        st_rich_bold_btn = gr.Button("Grassetto", size="sm")
                        st_rich_italic_btn = gr.Button("Corsivo", size="sm")
                        st_rich_code_btn = gr.Button("Codice", size="sm")
                        st_rich_bullet_btn = gr.Button("Elenco puntato", size="sm")
                        st_rich_ordered_btn = gr.Button("Elenco numerato", size="sm")
                        st_rich_link_btn = gr.Button("Link", size="sm")
                    # The contenteditable surface itself. Its rendered `value`
                    # (set fresh in _studio_full_bundle from rich_text.
                    # render_editable_html(), the SAME renderer Publishing Preview
                    # is built on) is what the writer sees and edits DIRECTLY --
                    # bold text looks bold, a list looks like a list, a link is a
                    # real clickable-looking anchor, never raw '**'/'`'/'[]()'.
                    # js_on_load (Gradio's own documented "runs when this HTML
                    # element (re)loads" hook -- the same mechanism Gradio's OWN
                    # default HTML click-forwarding relies on to survive re-
                    # renders) wires the live DOM->canonical sync, plain-text-only
                    # paste, and blur-forwarding -- see _MS_RICH_EDITABLE_JS_ON_LOAD
                    # below, next to the toolbar wiring, for the full script and
                    # rationale.
                    st_rich_editable = gr.HTML(visible=False, elem_classes=["ms-rich-editable"], elem_id="ms-rich-editable",
                                               js_on_load=_MS_RICH_EDITABLE_JS_ON_LOAD)
                    # 4E.3 Save Architecture Replacement: an unmistakable,
                    # CONTAINED confirmation right next to the editor itself
                    # -- not relying on the writer to notice the version
                    # number changing elsewhere on the page. Shares the
                    # exact same save_msg text _studio_full_bundle already
                    # computes for st_save_msg further down (success/no-op/
                    # error), so there is one message, shown in two places;
                    # styled per-outcome (ok/err/info) via a CSS class
                    # chosen in Python, never a global gr.Error popup.
                    st_rich_save_status = gr.HTML(visible=False, elem_classes=["ms-rich-save-status-wrap"])
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

                    # UX Gate 2A: the Varianti panel that used to sit here,
                    # between the editor and Save, now lives in the right
                    # contextual pane. It was measured wedged into the
                    # 944px gap a user had to scroll across to save what
                    # they had just typed.
                    #
                    # What deliberately stays HERE, next to Save: the
                    # shared-object decision. "Aggiorna ovunque" is a
                    # precondition of this Save (it unlocks in-place
                    # editing and Save performs the audited override), so
                    # moving it into a side pane would separate a gate
                    # from the action it gates.
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
                        # 4E.1 stabilization (item G): renamed from the bare
                        # "Salva" -- once the Varianti panel's OWN "Salva
                        # varianti" button existed right above this one in
                        # the same panel, a user could reasonably assume one
                        # depends on the other. "Salva contenuto" makes the
                        # split unambiguous: this one persists title/text/
                        # table/figure content; "Salva varianti" persists
                        # only where the element is visible. Neither button
                        # requires the other to be clicked.
                        # elem_id="ms-save-btn": the document-level capture-
                        # phase click listener installed by st_rich_editable's
                        # js_on_load (see _MS_RICH_EDITABLE_JS_ON_LOAD) targets
                        # this exact id to sync the live editor DOM into
                        # st_rich_editable's own value BEFORE Gradio's own
                        # click handling gathers inputs for studio_save_ui.
                        st_save_btn = gr.Button("Salva contenuto", variant="primary", visible=False, interactive=False,
                                                elem_id="ms-save-btn")
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

                # ====================================================
                # UX Gate 2A — right pane: contextual detail.
                #
                # Deliberately minimal for this gate. Everything here is
                # about the object that is currently open, and nothing
                # here is required to write and save. Grounded Authoring
                # is PRESERVED but not redesigned: 2B integrates it as a
                # source-anchored proposal system. It is simply no longer
                # buried two accordions deep inside a third.
                # ====================================================
                with gr.Column(scale=2, elem_classes=["ms-pane", "ms-pane-right"]):
                    LOC.add(gr.HTML(_section_head_html("content.pane.detail")),
                            value=lambda: _section_head_html("content.pane.detail"))

                    # 4E.1: "Varianti" -- where this topic/block is visible.
                    # Non-technical by design (ticket UX acceptance
                    # criterion): only dimension/value NAMES ever appear
                    # here, never ids/JSON/boolean syntax. Independently
                    # wired (see the module note above studio_variant_
                    # panel_ui) rather than folded into ST_OUT_FULL/
                    # _studio_full_bundle.
                    #
                    # gr.Row(visible=...) toggled as a WHOLE, never
                    # gr.Group -- this codebase's own documented Gradio
                    # 6.5.1 finding is that a child's independent visible=
                    # update is unreliable when nested inside a Group whose
                    # OWN visibility stays static in that response; Row is
                    # the pattern already proven safe here instead.
                    with gr.Row(visible=False) as st_variant_panel:
                        with gr.Column():
                            LOC.add(gr.HTML(_section_head_html("content.variants.title",
                                                               "content.variants.sub")),
                                    value=lambda: _section_head_html("content.variants.title",
                                                                     "content.variants.sub"))
                            st_variant_summary = gr.Markdown()
                            st_variant_dim_dd = []
                            st_variant_val_dd = []
                            for _vr in range(_VARIANT_ROWS):
                                with gr.Row():
                                    # 4E.1 stabilization (item 4.2): "Criterio"
                                    # over "Dimensione" -- avoids any hint of
                                    # technical/mathematical jargon for a
                                    # first-time author; the values label
                                    # states the OR-multiselect behavior in
                                    # plain language instead of "OPPURE".
                                    st_variant_dim_dd.append(
                                        LOC.add(gr.Dropdown(choices=[], value=None,
                                                            label=t("content.variants.criterion"), scale=1),
                                                label="content.variants.criterion"))
                                    st_variant_val_dd.append(
                                        LOC.add(gr.Dropdown(choices=[], value=[], multiselect=True,
                                                            label=t("content.variants.values"), scale=2),
                                                label="content.variants.values"))
                            st_variant_save_btn = LOC.button(lambda: t("content.variants.save"))
                            st_variant_save_msg = gr.Markdown()
                            LOC.add(gr.HTML(_section_head_html("content.variants.evaluate.sub")),
                                    value=lambda: _section_head_html("content.variants.evaluate.sub"))
                            # UX Gate 2A: THE variant profile selector for
                            # this workspace. Formerly one of two controls
                            # both labelled "Profilo" -- this one and the
                            # publishing preview's -- with no link between
                            # them. Both now read and write profile_state,
                            # and the preview/export half lives in Rilascio.
                            st_variant_eval_profile = LOC.add(
                                gr.Dropdown(choices=[], value=None,
                                            label=t("content.profile.label"),
                                            info=t("content.profile.hint")),
                                label="content.profile.label", info="content.profile.hint")
                            st_variant_eval_btn = LOC.button(lambda: t("content.variants.evaluate"))
                            st_variant_eval_result = gr.Markdown()

                    # ========================================
                    # UX Gate 2B.1 -- Impatto / Impact.
                    #
                    # Placed directly under the pane heading, above the
                    # variant editor and the advanced accordions: it is
                    # the one thing in this pane that can tell the author
                    # something they did not already know about the
                    # object they have open.
                    #
                    # Compact by default, details behind an accordion, so
                    # it informs without competing with the editor -- the
                    # constraint the whole 2A layout exists to protect.
                    # ========================================
                    LOC.add(gr.HTML(_section_head_html("impact.section", "impact.hint")),
                            value=lambda: _section_head_html("impact.section", "impact.hint"))
                    st_impact_compact = gr.HTML(iv.compact_html(None))
                    with gr.Accordion(t("impact.details"), open=False) as st_impact_accordion:
                        LOC.add(st_impact_accordion, label="impact.details")
                        st_impact_details = gr.HTML("")

                    # ========================================
                    # UX Gate 2B.2 -- Affidabilita traduzioni.
                    #
                    # Answers, for the object the author has open, the one
                    # question the product could not answer before: is the
                    # German translation still reliable after this change?
                    # Tier 1 is plain SELECTs and always available; the
                    # impact analysis is an explicit action because it runs
                    # the CAT engine.
                    # ========================================
                    LOC.add(gr.HTML(_section_head_html("translation.panel.title",
                                                       "translation.panel.hint")),
                            value=lambda: _section_head_html("translation.panel.title",
                                                             "translation.panel.hint"))
                    st_tr_panel = gr.HTML(tv.panel_html(None))
                    st_tr_note = gr.HTML("")
                    st_tr_impact_btn = LOC.button(lambda: t("translation.tier2.action"),
                                                  size="sm")
                    st_tr_impact = gr.HTML("")

                    with gr.Accordion(t("content.tech.section"), open=False) as st_tech_accordion:
                        LOC.add(st_tech_accordion, label="content.tech.section")
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

                    # Grounded Authoring: preserved verbatim, relocated.
                    # It used to sit inside "Dettagli tecnici" > "AI
                    # Grounded Authoring" -- two accordions deep, under a
                    # third section -- which is why the discovery gate
                    # measured it as effectively invisible. Gate 2B turns
                    # it into a source-anchored proposal panel; this gate
                    # only stops it being unreachable.
                    with gr.Accordion(t("content.grounded.section"), open=False) as g_accordion:
                        LOC.add(g_accordion, label="content.grounded.section")
                        LOC.add(gr.Markdown(t("content.grounded.hint")), value="content.grounded.hint")
                        with gr.Row():
                            g_pdf = gr.File(label='Fonte tecnica PDF', type='filepath')
                            g_text = gr.Textbox(label='Oppure fonte testuale', lines=4)
                        with gr.Row():
                            g_doc = gr.Textbox(label='Document ID')
                            g_title = gr.Textbox(label='Titolo fonte')
                        g_ingest = gr.Button('Indicizza fonte')
                        g_ingest_msg = gr.Markdown()
                        g_chunks = gr.Dataframe(interactive=False, max_height=350)
                        with gr.Row():
                            g_instruction = gr.Textbox(label='Richiesta')
                            g_mode = gr.Dropdown(['paragraph', 'procedure', 'warning', 'summary', 'rewrite'],
                                                 value='paragraph', label='Tipo')
                        g_generate = gr.Button('Genera proposta grounded', variant='primary')
                        g_msg = gr.Markdown()
                        g_draft = gr.Textbox(label='Proposta', lines=8)
                        g_evidence = gr.Dataframe(interactive=False, max_height=350)
                        g_claims = gr.Dataframe(interactive=False)
                        g_pid = gr.Textbox(label='Proposal ID')
                        with gr.Row():
                            g_topic = gr.Textbox(label='Topic ID')
                            g_block = gr.Textbox(label='Nuovo Block ID')
                            g_btype = gr.Dropdown(['paragraph', 'warning', 'note', 'list_item'],
                                                  value='paragraph', label='Tipo blocco')
                        with gr.Row():
                            g_pos = gr.Number(label='Posizione', value=1, precision=0)
                            g_reviewer = gr.Textbox(label='Revisore')
                        g_queue = gr.Button('Invia in review umana')
                        g_queue_msg = gr.Markdown()
                        g_ingest.click(grounding_ingest_ui, [project_state, g_pdf, g_text, g_doc, g_title],
                                       [g_ingest_msg, g_chunks])
                        g_generate.click(grounded_generate_ui, [project_state, g_instruction, g_mode],
                                         [g_msg, g_draft, g_evidence, g_claims, g_pid])
                        g_queue.click(grounded_queue_ui,
                                      [project_state, g_pid, g_topic, g_block, g_btype, g_pos, g_reviewer],
                                      [g_queue_msg])

            # UX Gate 2A: source import and approval moved OFF the daily
            # writing surface. These happen once per manual; they used to
            # occupy the first screen of a tab whose real job is the
            # trova -> apri -> modifica -> salva loop. Nothing is deleted --
            # the standard-mode review table, the IDML items-to-check gate
            # and both approval paths are all here, one click away.
            with gr.Accordion(t("content.import.section"), open=False):
                LOC.add(gr.Markdown(t("content.import.hint")), value="content.import.hint")
                with gr.Row():
                    c_pdf=gr.File(label="Manuale corrente PDF",type='filepath')
                    c_lang=gr.Textbox(label="Lingua sorgente",value='en-US')
                    c_pub=gr.Textbox(label="Publication ID",value='PUB-MAIN')
                    c_title=gr.Textbox(label="Titolo")
                c_import=gr.Button("Importa come draft",variant='primary')
                c_msg=gr.Markdown()
                # Milestone 3A: standard view. Same topics/blocks data as the
                # technical dataframes below (no second backend) -- just
                # friendlier columns, no IDs, no re-import button.
                LOC.add(gr.HTML(_section_head_html("content.import.review.title",
                                                   "content.import.review.sub")),
                        value=lambda: _section_head_html("content.import.review.title",
                                                         "content.import.review.sub"))
                c_simple_table=gr.Dataframe(interactive=False,wrap=True,max_height=500)
                # Milestone 3D constraint: master-spread/orphan/unsupported/
                # ambiguous-asset/caption-candidate items from an IDML import are
                # shown here in plain language (no Story/Spread ids, no JSON to
                # open) and BLOCK "Approva sorgente" until acknowledged.
                LOC.add(gr.HTML(_section_head_html("content.import.check.title")),
                        value=lambda: _section_head_html("content.import.check.title"))
                c_idml_review_html=gr.HTML(visible=False)
                with gr.Row(visible=False) as c_idml_review_row:
                    with gr.Column():
                        c_idml_review_radios=[gr.Radio(visible=False) for _ in range(_IDML_REVIEW_MAX_ITEMS)]
                        c_idml_review_confirm=gr.Button("Conferma decisioni")
                c_simple_confirm=gr.Checkbox(label="Confermo di aver revisionato il contenuto")
                c_simple_approve=gr.Button("Approva sorgente",variant="primary")
                c_simple_msg=gr.Markdown()
                c_topics=gr.Dataframe(interactive=False)
                c_blocks=gr.Dataframe(interactive=False,max_height=500)
                with gr.Row():
                    c_reviewer=gr.Textbox(label="Revisore umano")
                    c_confirm=gr.Checkbox(label="Confermo di aver revisionato la sorgente")
                c_approve=gr.Button("Approva sorgente")
                c_approve_msg=gr.Markdown()
                c_refresh=gr.Button("Mostra oggetti approvati")
                c_approved=gr.Dataframe(interactive=False,max_height=450)
                c_import.click(import_manual_ui,[project_state,c_pdf,c_lang,c_pub,c_title],[c_msg,c_topics,c_blocks])
                c_approve.click(approve_source_ui,[project_state,c_pub,c_reviewer,c_confirm],[c_approve_msg])
                c_refresh.click(approved_objects_ui,[project_state],[c_approved])

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
                          st_compare_row, st_diff_output,
                          st_rich_toolbar_row, st_rich_editable, st_rich_save_status]

            # 4E.1 Varianti panel outputs. Root-caused live-browser bug fix:
            # this used to be refreshed via st_id.change()/st_version.change()
            # (a gr.State "changed as an output of another event" cascade).
            # That cascade is real in the Gradio 6.5.1 Python backend (see
            # Blocks.get_state_ids_to_track/"changed_state_ids" in
            # gradio/blocks.py) but the resulting `changed_state_ids` list is
            # ONLY ever produced server-side -- nothing in routes.py/
            # queueing.py consumes it, so firing the cascade is entirely up
            # to frontend JS reacting to that field. In a real browser this
            # never actually made the panel visible: the Varianti section
            # never appeared after selecting a topic, even though every
            # ST_OUT_FULL-driven field right next to it (title, Save button,
            # etc.) worked correctly. Fixed the guaranteed way already used
            # everywhere else in this Studio panel: VARIANT_PANEL_OUT is now
            # chained via `.then(...)` onto every event that already outputs
            # ST_OUT_FULL below -- `.then()` runs unconditionally after its
            # trigger in the same response cycle, no client-side value-diff
            # detection involved, so the panel's visibility is guaranteed to
            # be recomputed every time the rest of the panel is.
            # UX Gate 2A: profile_state is both an INPUT and an OUTPUT of
            # the panel refresh -- it carries the shared variant profile
            # through every object open so the selection survives, and it
            # is re-emitted (possibly dropped, if that profile no longer
            # exists) so Rilascio's picker sees the same value.
            VARIANT_PANEL_OUT = [st_variant_summary, st_variant_panel, *st_variant_dim_dd, *st_variant_val_dd,
                                 st_variant_eval_profile, st_variant_eval_result, profile_state]
            _VARIANT_THEN_ARGS = (studio_variant_panel_ui,
                                  [project_state, st_kind, st_id, profile_state], VARIANT_PANEL_OUT)

            # UX Gate 2B.1: the Impact panel refreshes off the SAME events,
            # chained the same way. Deliberately NOT st_id.change()/
            # st_version.change(): this build does not fire a gr.State
            # change reliably, which is exactly why the Varianti panel was
            # moved to .then() chaining in 4E.1 (see the note above). A
            # panel that silently fails to refresh is worse than no panel,
            # because the number on screen would then be about a different
            # object than the one open.
            #
            # ws_lang is an input so the panel re-renders in the language
            # active for the request; the report itself is codes+params
            # and carries no prose.
            IMPACT_OUT = [st_impact_compact, st_impact_details]
            _IMPACT_THEN_ARGS = (studio_impact_panel_ui,
                                 [project_state, st_kind, st_id, ws_lang], IMPACT_OUT)

            # UX Gate 2B.2: the translation panel refreshes off the same
            # events, chained the same way and for the same reason -- a
            # gr.State change does not fire reliably in this build.
            # Tier 1 only: the impact analysis stays an explicit action,
            # because it runs the CAT engine and must not fire on every
            # selection.
            TRANSLATION_OUT = [st_tr_panel, st_tr_note, st_tr_impact]
            _TRANSLATION_THEN_ARGS = (translation_panel_ui,
                                      [project_state, st_kind, st_id, ws_lang], TRANSLATION_OUT)

            authoring_tab.select(studio_load_ui, [project_state, c_pub, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_refresh_btn.click(studio_refresh_ui, [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_show_archived.change(studio_refresh_ui, [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_outline_radio.change(studio_open_ui, [project_state, c_pub, st_outline_radio, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)

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

            # 4E.3 stabilization: rich-text toolbar, rewritten to operate on
            # the CONTENTEDITABLE surface's real selection via the browser's
            # own native document.execCommand -- bold/italic/lists are real
            # native commands; Codice/Link have no native command and get a
            # few extra lines each. Every snippet: (1) focuses the editable
            # div so execCommand has something to act on, (2) runs the
            # command, (3) dispatches a real 'input' event as a defensive
            # belt-and-braces sync trigger (execCommand already fires a
            # native 'input' event on the contenteditable in every modern
            # browser, so js_on_load's own root.oninput normally handles
            # the sync -- this dispatch just guarantees it even if that
            # native firing were ever inconsistent). Every snippet bails
            # out harmlessly via `if (!root) return;` if the editable
            # surface isn't found (e.g. a non-text block is selected).
            _MS_ROOT_LOOKUP = "const root = document.querySelector('#ms-rich-editable-inner'); if (!root) return; root.focus();"
            # 4E.3 Save Architecture Replacement (ticket item: "toolbar
            # buttons must update props.value immediately after each
            # formatting operation"): the same window.__msRichEditableSaveSyncer
            # defined in _MS_RICH_EDITABLE_JS_ON_LOAD (writes the live DOM's
            # current HTML into st_rich_editable's own tracked value) now
            # also runs right after every toolbar command -- in addition to,
            # not instead of, the pre-save capture-phase sync, which stays
            # the one load-bearing guarantee for Save's own correctness.
            _ms_sync_tail = ("root.dispatchEvent(new Event('input', { bubbles: true }));"
                             "if (window.__msRichEditableSaveSyncer) { window.__msRichEditableSaveSyncer(); }")
            _ms_bold_js = "() => {" + _MS_ROOT_LOOKUP + "document.execCommand('bold');" + _ms_sync_tail + "}"
            _ms_italic_js = "() => {" + _MS_ROOT_LOOKUP + "document.execCommand('italic');" + _ms_sync_tail + "}"
            _ms_code_js = (
                "() => {" + _MS_ROOT_LOOKUP +
                "const sel = window.getSelection(); const text = (sel && sel.toString()) ? sel.toString() : 'codice';"
                "const esc = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');"
                "document.execCommand('insertHTML', false, '<code>' + esc + '</code>');" + _ms_sync_tail + "}"
            )
            _ms_bullet_js = "() => {" + _MS_ROOT_LOOKUP + "document.execCommand('insertUnorderedList');" + _ms_sync_tail + "}"
            _ms_ordered_js = "() => {" + _MS_ROOT_LOOKUP + "document.execCommand('insertOrderedList');" + _ms_sync_tail + "}"
            _ms_link_js = (
                "() => {" + _MS_ROOT_LOOKUP +
                "const sel = window.getSelection();"
                "if (!sel || !sel.toString()) { window.alert('Seleziona il testo che vuoi trasformare in link, poi premi di nuovo \"Link\".'); return; }"
                "const url = window.prompt('Indirizzo del link (es. https://esempio.it):', 'https://');"
                "if (!url) return;"
                "const lower = url.toLowerCase();"
                "if (lower.indexOf('http://') !== 0 && lower.indexOf('https://') !== 0 && lower.indexOf('mailto:') !== 0) {"
                "  window.alert('Indirizzo non valido: sono ammessi solo link http://, https:// o mailto:.'); return;"
                "}"
                "document.execCommand('createLink', false, url);" + _ms_sync_tail + "}"
            )
            st_rich_bold_btn.click(fn=None, inputs=[], outputs=[], js=_ms_bold_js)
            st_rich_italic_btn.click(fn=None, inputs=[], outputs=[], js=_ms_italic_js)
            st_rich_code_btn.click(fn=None, inputs=[], outputs=[], js=_ms_code_js)
            st_rich_bullet_btn.click(fn=None, inputs=[], outputs=[], js=_ms_bullet_js)
            st_rich_ordered_btn.click(fn=None, inputs=[], outputs=[], js=_ms_ordered_js)
            st_rich_link_btn.click(fn=None, inputs=[], outputs=[], js=_ms_link_js)

            # 4E.3 Save Architecture Replacement.
            #
            # The previous two-step `.then()` chain (fn=None+js= writing
            # into hidden st_text_input's own output slot, THEN a chained
            # studio_save_ui reading that Textbox) failed real-browser
            # acceptance twice: no reliable visible confirmation, version
            # not reliably advancing. Per the ticket, that timing is not
            # being patched again -- the hidden-carrier architecture is
            # removed as Save's source of truth entirely.
            #
            # st_save_btn.click is now a SINGLE, ordinary Gradio dependency
            # -- no js=, no fn=None step, no .then() bridging two components.
            # st_rich_editable is listed DIRECTLY in `inputs`, exactly like
            # any other interactive component: Gradio gathers whatever its
            # frontend-tracked `value` currently is at click time. What
            # guarantees that value is fresh is st_rich_editable's OWN
            # js_on_load (see _MS_RICH_EDITABLE_JS_ON_LOAD): a document-level
            # CAPTURE-PHASE click listener on this exact button (elem_id=
            # "ms-save-btn") writes the live contenteditable DOM's current
            # HTML into props.value SYNCHRONOUSLY, before this SAME click
            # event reaches Gradio's own bubble-phase handler -- a hard
            # ordering guarantee from the browser's own event-dispatch spec,
            # not from a Gradio promise-chain.
            #
            # studio_save_ui's text_val parameter therefore now receives
            # HTML (the live editor's current markup), not canonical
            # markdown-like text -- it converts via rich_text.
            # html_to_canonical() itself (the inverse of render_editable_
            # html()) before using it, so every OTHER caller of
            # studio_save_ui's signature (there are none besides this
            # wiring) is unaffected and the conversion lives in exactly one
            # place.
            st_save_btn.click(
                studio_save_ui,
                [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id, st_version,
                 st_title_input, st_rich_editable, st_table_title, st_table_caption, st_table_grid_edit,
                 st_figure_caption, st_figure_alt, actor_state, st_show_archived, st_shared_confirmed,
                 st_shared_impact_token],
                ST_OUT_FULL
            ).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)

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
                                        st_figure_caption, st_figure_alt, st_restore_draft_btn, st_discard_draft_btn,
                                        st_rich_editable, st_rich_save_status])
            st_discard_draft_btn.click(studio_discard_draft_ui,
                                       [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_discard_edits_btn.click(studio_discard_edits_ui, [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            # st_save_btn's own wiring is now the two-step guaranteed-order
            # chain declared earlier, right after the rich-text toolbar
            # buttons (see the "4E.3 Save/Version Regression Fix" comment
            # there) -- this used to be a single .click(studio_save_ui, ...)
            # here; kept as a comment, not a second binding, to avoid two
            # competing click handlers on the same button.

            # 4D reuse-aware shared-object decision -- see
            # studio_confirm_shared_update_ui/studio_fork_local_ui.
            st_update_everywhere_btn.click(studio_confirm_shared_update_ui,
                                           [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_fork_local_btn.click(studio_fork_local_ui,
                                    [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id,
                                     st_version, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)

            st_move_up_btn.click(lambda pd_, pub, k, i, sa, ac: studio_move_ui(pd_, pub, k, i, "up", sa, ac),
                                 [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_move_down_btn.click(lambda pd_, pub, k, i, sa, ac: studio_move_ui(pd_, pub, k, i, "down", sa, ac),
                                   [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_archive_btn.click(studio_archive_ui, [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_restore_btn.click(studio_restore_obj_ui, [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_prev_btn.click(lambda pd_, pub, k, i, sa: studio_next_prev_ui(pd_, pub, k, i, "prev", sa),
                              [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_next_btn.click(lambda pd_, pub, k, i, sa: studio_next_prev_ui(pd_, pub, k, i, "next", sa),
                              [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)

            st_new_topic_btn.click(studio_new_topic_ui, [project_state, c_pub, st_new_topic_title, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_new_block_btn.click(studio_new_block_ui, [project_state, c_pub, st_kind, st_id, st_new_block_type, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_new_table_btn.click(studio_new_table_ui, [project_state, c_pub, st_kind, st_id, st_new_table_caption, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_new_figure_btn.click(studio_new_figure_ui,
                                    [project_state, c_pub, st_kind, st_id, st_new_figure_file, st_new_figure_caption,
                                     st_new_figure_alt, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_figure_replace_btn.click(studio_replace_figure_ui,
                                        [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id,
                                         st_version, st_figure_replace_file, st_show_archived, actor_state,
                                         st_shared_confirmed, st_shared_impact_token], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_submit_review_btn.click(studio_submit_review_ui, [project_state, c_pub, st_kind, st_id, st_show_archived, actor_state], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            st_revert_btn.click(studio_revert_ui,
                                [project_state, c_pub, st_kind, st_id, st_block_type, st_object_ref_id,
                                 st_revert_version, st_show_archived, actor_state, st_shared_confirmed,
                                 st_shared_impact_token], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)

            # 4D revision compare -- read-only, no ST_OUT_FULL re-render
            # (see studio_compare_ui): only st_diff_output changes.
            st_compare_btn.click(studio_compare_ui,
                                 [project_state, st_kind, st_id, st_block_type, st_object_ref_id,
                                  st_version, st_compare_version], [st_diff_output])
            st_compare_prev_btn.click(studio_compare_prev_ui,
                                      [project_state, st_kind, st_id, st_block_type, st_object_ref_id, st_version],
                                      [st_diff_output])

            st_search_btn.click(studio_search_ui, [project_state, c_pub, st_search_box], [st_search_picker, st_search_open_btn, st_search_msg])
            st_search_open_btn.click(studio_search_open_ui, [project_state, c_pub, st_search_picker, st_show_archived], ST_OUT_FULL).then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)

            # 4E.1 Varianti panel: per-row dimension picker cascade + the
            # panel's own Save/Evaluate buttons. The panel's own visibility/
            # summary refresh is chained via .then(*_VARIANT_THEN_ARGS) onto
            # every ST_OUT_FULL-outputting event above (see VARIANT_PANEL_OUT/
            # _VARIANT_THEN_ARGS just above ST_OUT_FULL's own wiring) rather
            # than a separate state-change cascade -- see the comment there
            # for why.
            # st_kind/st_id are now also inputs here (not just project_state
            # + the picked dimension) so this cascade is self-healing --
            # see studio_variant_dimension_pick_ui's own docstring for why.
            for _vi in range(_VARIANT_ROWS):
                st_variant_dim_dd[_vi].input(studio_variant_dimension_pick_ui,
                                             [project_state, st_kind, st_id, st_variant_dim_dd[_vi]],
                                             [st_variant_val_dd[_vi]])
            # Deliberately a SMALL, dedicated output list (just the save
            # message + st_version), NOT VARIANT_PANEL_OUT -- see
            # studio_variant_save_ui's own docstring for why (a missing-
            # actor gr.Error used to flag all 8 picker Dropdowns + panel
            # visibility + eval profile as "errored" in the browser, since
            # every declared output of a raising function gets Gradio's
            # frontend error-state indicator).
            #
            # TWO independent chains off the SAME click, not one:
            #  - .then(*_VARIANT_THEN_ARGS): ALWAYS runs (success or
            #    failure) -- refreshes just the Varianti panel itself
            #    (summary/pickers/visibility) from whatever is currently
            #    persisted, which is correct either way (unchanged on
            #    failure, updated on success).
            #  - .success(studio_refresh_ui, ..., ST_OUT_FULL): runs ONLY
            #    if the save actually succeeded (Dependency.success() sets
            #    trigger_only_on_success=True internally -- confirmed via
            #    gradio/events.py) -- rebuilds the REST of the visible
            #    Studio state a successful condition save also changes:
            #    the selected-element header ("Bozza · versione N"), the
            #    outline table/radio (so the just-saved condition's
            #    compact suffix appears immediately), etc. A manual
            #    acceptance run found these staying stale after a
            #    successful "Salva varianti" -- the panel-only .then()
            #    above was never wired to ALSO rebuild ST_OUT_FULL, so
            #    nothing ever refreshed the header/outline on this path
            #    (they were previously only ever rebuilt by navigation or
            #    by the generic "Salva"/"Aggiorna struttura" -- neither of
            #    which "Salva varianti" is required to also trigger). This
            #    is exactly the same studio_refresh_ui call "Aggiorna
            #    struttura" itself makes, just success-gated so a FAILED
            #    save never re-renders (and can't reintroduce the earlier
            #    cascading-Error problem, since it plainly doesn't run).
            _variant_save_event = st_variant_save_btn.click(
                studio_variant_save_ui,
                [project_state, st_kind, st_id, st_block_type, actor_state,
                 *st_variant_dim_dd, *st_variant_val_dd],
                [st_variant_save_msg, st_version])
            _variant_save_event.then(*_VARIANT_THEN_ARGS).then(*_IMPACT_THEN_ARGS).then(*_TRANSLATION_THEN_ARGS)
            _variant_save_event.success(studio_refresh_ui,
                                        [project_state, c_pub, st_kind, st_id, st_show_archived], ST_OUT_FULL)
            # Tier 2 is explicit and read-only: it runs the existing engine
            # with its output on a temp directory, so no package is produced
            # and nothing in the project changes.
            st_tr_impact_btn.click(translation_impact_ui,
                                   [project_state, st_kind, st_id, ws_lang], [st_tr_impact])
            st_variant_eval_btn.click(studio_variant_evaluate_ui,
                                      [project_state, st_kind, st_id, st_variant_eval_profile], [st_variant_eval_result])

        with gr.Tab(t("nav.conformity"), id="compliance") as cp_tab:
            LOC.add(cp_tab, label="nav.conformity")
            # ============================================================
            # UX Gate 1 — Conformità as the reference workspace.
            #
            # The page this replaces was 3260px / 3 full screens: the
            # requirement list sat at y=1614 and its detail panel at
            # y=2273, so deciding one requirement scrolled the list off
            # the screen and moving to the next meant scrolling back up
            # ~1200px. Everything below keeps ONE requirement as the
            # working object and the list permanently beside it.
            #
            # Publication and operator identity are NOT asked here any
            # more: they are workspace context, chosen once in the shell
            # bar above the tabs.
            # ============================================================
            with gr.Row(elem_classes=["ms-cp-topbar"]):
                cp_head=LOC.html(lambda: f"<div class='ms-pagehead ms-pagehead-tight'><div>"
                        f"<div class='ms-page-kicker'>{t('kicker.workflow')}</div>"
                        f"<h1>{t('page.conformity.title')}</h1></div></div>")
                cp_scope = LOC.add(gr.Dropdown(label=t("conformity.scope.label"), choices=[],
                                               value=None, scale=2),
                                   label="conformity.scope.label")
                cp_reload = LOC.add(gr.Button(t("action.recalculate"), variant="secondary", scale=1),
                                    value="action.recalculate")

            # --- Header: the essential state, always visible -----------
            # Deliberately only four things: how much is covered, what
            # needs attention, which package is active, and an update
            # alert ONLY when there is something to apply. The update
            # ACTIONS live in this same strip rather than in the tab
            # below, so an alert and the button that answers it are never
            # separated.
            cp_dashboard = gr.HTML()
            cp_pack_state = gr.HTML()
            with gr.Row(visible=False) as cp_pack_update_row:
                cp_pack_update_preview_btn = LOC.add(gr.Button(t("action.preview_update"), scale=1),
                                                     value="action.preview_update")
                cp_pack_update_apply_btn = LOC.add(
                    gr.Button(t("action.apply_update"), variant="primary", scale=1),
                    value="action.apply_update")

            with gr.Tabs(elem_id="cp-subtabs"):
                # --------------------------------------------------------
                # Requisiti — the daily work, and therefore the landing
                # view. List on the left, detail on the right, both
                # visible at the same time at 1920px.
                # --------------------------------------------------------
                with gr.Tab(t("conformity.tab.requirements"), id="cp_requirements") as cp_tab_req:
                    LOC.add(cp_tab_req, label="conformity.tab.requirements")
                    with gr.Row():
                        with gr.Column(scale=5, min_width=520):
                            cp_listhint=LOC.html(
                                lambda: f"<div class='ms-section-sub ms-cp-listhint'>"
                                        f"{t('conformity.list.hint')}</div>")
                            with gr.Row(elem_classes=["ms-cp-listbar"]):
                                cp_search = LOC.add(
                                    gr.Textbox(label=t("action.search"), scale=2,
                                               placeholder=t("conformity.list.search_placeholder"),
                                               show_label=False),
                                    label="action.search",
                                    placeholder="conformity.list.search_placeholder")
                                # (label, value) pairs: the submitted VALUE is the stable
                                # internal filter token, so switching language
                                # changes the word and never the filter.
                                cp_filter = LOC.add(
                                    gr.Radio(cp_filter_choices(), value=CP_FILTER_DEFAULT,
                                             show_label=False, scale=3,
                                             elem_classes=["ms-cp-filter"]),
                                    choices=cp_filter_choices)
                            # The working list. "Da completare" is the
                            # default because it is the only view that
                            # answers "what should I do now?" -- browsing
                            # all 41 requirements is the exception, not
                            # the daily case.
                            cp_table = gr.Dataframe(interactive=False, wrap=True, max_height=620,
                                                    elem_classes=["ms-cp-grid", "ms-cp-grid-click"],
                                                    column_widths=["40%", "25%", "19%", "16%"])
                            cp_req_picker = LOC.add(
                                gr.Dropdown(label=t("conformity.requirement.label"), choices=[],
                                            value=None, visible=False),
                                label="conformity.requirement.label")
                        with gr.Column(scale=6, min_width=560, elem_classes=["ms-panel", "ms-cp-detail"]):
                            cp_header = gr.HTML()
                            with gr.Column(elem_classes=["ms-cp-detail-form"]):
                                with gr.Row():
                                    with gr.Column():
                                        cp_applic_title=LOC.html(
                                            lambda: f"<div class='ms-section-title'>"
                                                    f"{t('conformity.applicability.title')}</div>")
                                        cp_applic_current = gr.HTML()
                                        cp_applic_radio = LOC.add(
                                            gr.Radio(cv.applicability_choices(),
                                                     label=t("field.decision"), value=None),
                                            choices=cv.applicability_choices,
                                            label="field.decision")
                                        cp_applic_justification = LOC.add(
                                            gr.Textbox(label=t("conformity.applicability.justification")),
                                            label="conformity.applicability.justification")
                                        cp_applic_comment = LOC.add(gr.Textbox(label=t("field.comment.optional")),
                                                                    label="field.comment.optional")
                                        cp_applic_confirm = LOC.add(
                                            gr.Button(t("conformity.applicability.confirm"),
                                                      variant="primary"),
                                            value="conformity.applicability.confirm")
                                        cp_applic_msg = gr.Markdown()
                                    with gr.Column():
                                        cp_cov_title=LOC.html(
                                            lambda: f"<div class='ms-section-title'>"
                                                    f"{t('conformity.coverage.title')}</div>")
                                        cp_cov_banner = gr.HTML()
                                        cp_cov_current = gr.HTML()
                                        cp_cov_radio = LOC.add(
                                            gr.Radio(cv.coverage_choices(),
                                                     label=t("field.decision"), value=None),
                                            choices=cv.coverage_choices, label="field.decision")
                                        cp_cov_comment = LOC.add(gr.Textbox(label=t("field.comment.optional")),
                                                                 label="field.comment.optional")
                                        cp_cov_confirm = LOC.add(
                                            gr.Button(t("conformity.coverage.confirm"),
                                                      variant="primary"),
                                            value="conformity.coverage.confirm")
                                        cp_cov_msg = gr.Markdown()
                                cp_evidence_title=LOC.html(
                                    lambda: f"<div class='ms-section-title'>"
                                            f"{t('conformity.evidence.title')}</div>"
                                            f"<div class='ms-section-sub'>"
                                            f"{t('conformity.evidence.hint')}</div>")
                                cp_evidence_table = gr.Dataframe(interactive=False, wrap=True, max_height=200,
                                                                 elem_classes=["ms-cp-grid"])
                                with gr.Row():
                                    cp_evidence_candidates = LOC.add(
                                        gr.Dropdown(label=t("conformity.evidence.link_label"),
                                                    choices=[], value=None),
                                        label="conformity.evidence.link_label")
                                    cp_evidence_link_btn = LOC.add(gr.Button(t("conformity.evidence.link_button")),
                                                                   value="conformity.evidence.link_button")
                                with gr.Row():
                                    cp_evidence_unlink_dd = LOC.add(
                                        gr.Dropdown(label=t("conformity.evidence.unlink_label"),
                                                    choices=[], value=None),
                                        label="conformity.evidence.unlink_label")
                                    cp_evidence_unlink_btn = LOC.add(gr.Button(t("conformity.evidence.unlink_button")),
                                                                     value="conformity.evidence.unlink_button")
                                cp_evidence_msg = gr.Markdown()
                                # Provenance, internal ids and version detail:
                                # genuinely useful when answering "where does
                                # this obligation come from?", noise the rest
                                # of the time.
                                with gr.Accordion(t("conformity.advanced"), open=False) as cp_advanced_acc:
                                    LOC.add(cp_advanced_acc, label="conformity.advanced")
                                    cp_advanced = gr.HTML()

                # --------------------------------------------------------
                # Panoramica — the fuller coverage picture. Separate from
                # the working view because reading the numbers and acting
                # on a requirement are different tasks: mixing them is
                # what made the old page three screens long.
                # --------------------------------------------------------
                with gr.Tab(t("conformity.tab.overview"), id="cp_overview") as cp_tab_overview:
                    LOC.add(cp_tab_overview, label="conformity.tab.overview")
                    cp_coverage = gr.HTML()
                    cp_gaps_title=LOC.html(
                        lambda: f"<div class='ms-section-title'>"
                                f"{t('conformity.overview.to_complete')}</div>"
                                f"<div class='ms-section-sub'>"
                                f"{t('conformity.overview.to_complete_hint')}</div>")
                    cp_gaps = gr.Dataframe(interactive=False, wrap=True, max_height=420,
                                           elem_classes=["ms-cp-grid"],
                                           column_widths=["30%", "22%", "14%", "8%", "26%"])

                # --------------------------------------------------------
                # Aggiornamenti normativi — the pack as a versioned
                # source. Rarely needed, never during daily assessment,
                # so it is one click away rather than on the work screen.
                # --------------------------------------------------------
                with gr.Tab(t("conformity.tab.updates"), id="cp_updates") as cp_tab_updates:
                    LOC.add(cp_tab_updates, label="conformity.tab.updates")
                    cp_updates_hint=LOC.html(
                        lambda: f"<div class='ms-section-sub'>{t('conformity.updates.hint')}</div>")
                    cp_pack_provenance = gr.HTML()
                    cp_pack_update_preview = gr.HTML(elem_classes=["ms-cp-collapsible"])
                    cp_pack_update_status = gr.HTML(elem_classes=["ms-cp-collapsible"])
                    # Setup, collapsed: a pack NEVER installs itself into a
                    # project, and the user sees source, version, review
                    # date and scope size before importing. Once a pack is
                    # installed this is not a daily task.
                    with gr.Accordion(t("conformity.pack.add_title"), open=False) as cp_pack_acc:
                        LOC.add(cp_pack_acc, label="conformity.pack.add_title")
                        cp_pack_add_hint=LOC.html(
                            lambda: f"<div class='ms-section-sub'>{t('conformity.pack.add_hint')}</div>")
                        with gr.Row():
                            cp_pack_picker = LOC.add(
                                gr.Dropdown(label=t("conformity.pack.available"), choices=[],
                                            value=None, scale=2),
                                label="conformity.pack.available")
                            cp_pack_groups = LOC.add(
                                gr.CheckboxGroup(
                                    choices=list(csp.APPLICABILITY_GROUPS.values()),
                                    value=[csp.APPLICABILITY_GROUPS[csp.GROUP_FULL_MACHINERY]],
                                    label=t("conformity.pack.groups"), scale=2),
                                label="conformity.pack.groups")
                        cp_pack_preview = gr.HTML()
                        cp_pack_import = LOC.add(gr.Button(t("action.add_pack"), variant="primary"),
                                                 value="action.add_pack")
                        cp_pack_status = gr.HTML()
            cp_open_rid = gr.State("")

            cp_out_full = [cp_dashboard, cp_table, cp_req_picker, cp_header, cp_applic_current, cp_applic_radio,
                          cp_cov_banner, cp_cov_current, cp_cov_radio, cp_evidence_table, cp_evidence_candidates,
                          cp_evidence_unlink_dd, cp_advanced, cp_open_rid, cp_applic_msg, cp_cov_msg, cp_evidence_msg]
            cp_reload.click(compliance_load_ui, [project_state, cp_pub, ws_lang], [cp_scope, *cp_out_full]).then(
                compliance_coverage_ui, [project_state, cp_scope, ws_lang], [cp_coverage, cp_gaps]).then(
                compliance_pack_state_ui, [project_state, ws_lang], [cp_pack_state, cp_pack_update_row, cp_pack_provenance]).then(
                    compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            cp_scope.change(compliance_switch_scope_ui, [project_state, cp_scope, ws_lang], cp_out_full).then(
                compliance_coverage_ui, [project_state, cp_scope, ws_lang], [cp_coverage, cp_gaps]).then(
                compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            # 4F.2 pack wiring -- isolated callbacks, isolated outputs.
            cp_pack_picker.change(compliance_pack_preview_ui, [cp_pack_picker], [cp_pack_preview])
            cp_pack_update_preview_btn.click(compliance_pack_update_preview_ui, [project_state],
                                             [cp_pack_update_preview])
            cp_pack_update_apply_btn.click(
                compliance_pack_update_apply_ui, [project_state, cp_scope, ws_operator],
                [cp_pack_update_status, cp_pack_update_preview, cp_coverage, cp_gaps],
                trigger_mode="once", concurrency_limit=1).then(
                # Re-reads the pack state, so the strip shows the new active
                # version and the update row hides itself once applied.
                compliance_pack_state_ui, [project_state, ws_lang], [cp_pack_state, cp_pack_update_row, cp_pack_provenance]).then(
                    compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table).then(
                # Found by the browser gate: refreshing only coverage/gaps left
                # the "Requisiti" table and the open requirement showing the
                # PRE-update titles until the user happened to press "Aggiorna".
                # An update whose whole purpose is to correct wording must not
                # leave the old wording on screen looking current.
                compliance_switch_scope_ui, [project_state, cp_scope, ws_lang], cp_out_full)
            cp_pack_import.click(
                compliance_import_pack_ui,
                [project_state, cp_pub, cp_scope, cp_pack_picker, cp_pack_groups, ws_operator],
                [cp_pack_status, cp_scope, cp_coverage, cp_gaps]).then(
                compliance_pack_state_ui, [project_state, ws_lang], [cp_pack_state, cp_pack_update_row, cp_pack_provenance]).then(
                    compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            cp_req_picker.change(compliance_open_requirement_ui, [project_state, cp_scope, cp_req_picker, ws_lang], cp_out_full)
            cp_table.select(compliance_open_requirement_row_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_out_full).then(
                compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            # Search and filter drive ONLY the list; the open requirement
            # stays open, which is what keeps the working context alive
            # while the user narrows the list around it.
            cp_search.change(compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table, show_progress="hidden")
            cp_filter.change(compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table, show_progress="hidden")
            cp_applic_confirm.click(compliance_decide_applicability_ui,
                                    [project_state, cp_scope, cp_open_rid, cp_applic_radio, ws_operator,
                                     cp_applic_justification, cp_applic_comment, cp_cov_radio, ws_lang], cp_out_full).then(
                compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            cp_cov_confirm.click(compliance_decide_coverage_ui,
                                 [project_state, cp_scope, cp_open_rid, cp_cov_radio, ws_operator, cp_cov_comment,
                                  cp_applic_radio, ws_lang],
                                 cp_out_full).then(
                compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            cp_evidence_link_btn.click(compliance_link_evidence_ui,
                                       [project_state, cp_scope, cp_open_rid, cp_evidence_candidates, ws_operator,
                                        cp_applic_radio, cp_cov_radio, ws_lang],
                                       cp_out_full).then(
                compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            cp_evidence_unlink_btn.click(compliance_unlink_evidence_ui,
                                         [project_state, cp_scope, cp_open_rid, cp_evidence_unlink_dd,
                                          ws_operator, cp_applic_radio, cp_cov_radio, ws_lang], cp_out_full).then(
                compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
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
            # Deliberately ONE select binding, extended by .then(): 3.5B's
            # own structural guard requires exactly one 'select' event on
            # this tab, and a second binding (rather than a chain) would
            # be precisely the duplicate-wiring class it exists to catch.
            cp_tab.select(compliance_load_ui, [project_state, cp_pub, ws_lang], [cp_scope, *cp_out_full]).then(
                compliance_coverage_ui, [project_state, cp_scope, ws_lang], [cp_coverage, cp_gaps]).then(
                compliance_pack_choices_ui, None, [cp_pack_picker]).then(
                compliance_pack_state_ui, [project_state, ws_lang], [cp_pack_state, cp_pack_update_row, cp_pack_provenance]).then(
                    compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)
            # Same programmatic-navigation trap fixed in 4F.1 for the
            # Publishing Preview picker: the sidebar switches tabs via
            # gr.Tabs(selected=...), which never fires cp_tab.select, so
            # the pack list and coverage must also be refreshed from the
            # navigation itself or they stay empty for a normal user.
            for _cp_entry in (nav_compliance, home_compliance):
                _cp_entry.click(lambda: None, None, None).then(
                    compliance_pack_choices_ui, None, [cp_pack_picker]).then(
                    compliance_load_ui, [project_state, cp_pub, ws_lang], [cp_scope, *cp_out_full]).then(
                    compliance_coverage_ui, [project_state, cp_scope, ws_lang], [cp_coverage, cp_gaps]).then(
                    compliance_pack_state_ui, [project_state, ws_lang], [cp_pack_state, cp_pack_update_row, cp_pack_provenance]).then(
                    compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table)

        with gr.Tab(t("nav.translation"),id="translations") as tab_translations:
            LOC.add(tab_translations, label="nav.translation")
            head_translation=LOC.html(lambda: _page_head_html("kicker.workflow", "page.translation.title", "page.translation.subtitle"))
            LOC.add(gr.HTML(_translation_stepper_html()), value=_translation_stepper_html)
            gr.HTML("<div class='ms-panel'><div class='ms-section-title'>Pacchetti lingua</div><div class='ms-section-sub'>Riuso TM, ripetizioni esatte e codici non traducibili restano fuori dal workfile; solo il lavoro linguistico reale entra nell’XLIFF controllato.</div></div>")
            with gr.Row(): t_kind=gr.Dropdown(['publication','topic','block','table','figure','component'],value='publication',label='Tipo'); t_id=gr.Textbox(label='Object ID'); t_tms=gr.File(label='TM SQLite esterna — opzionale',type='filepath',file_count='multiple')
            t_btn=gr.Button('Genera pacchetti lingua',variant='primary'); t_msg=gr.Markdown(); t_table=gr.Dataframe(interactive=False,wrap=True,visible=False)

            # ========================================
            # UX Gate 2B.2 -- Affidabilita traduzioni, on the Traduzioni tab.
            #
            # The same panel the Contenuti workspace shows for the object
            # being edited, here driven by the object this tab is already
            # pointed at. Two surfaces, one service: the reliability of a
            # translation is the same fact whether you are writing the
            # content or preparing its language packages, and answering it
            # only in one place would mean the translator has to go and
            # open the editor to find out.
            #
            # Read-only, and deliberately BEFORE the package generator: the
            # whole point of this gate is that you no longer have to
            # produce ZIPs to learn what state the translations are in.
            # ========================================
            LOC.add(gr.HTML(_section_head_html("translation.panel.title",
                                               "translation.panel.hint")),
                    value=lambda: _section_head_html("translation.panel.title",
                                                     "translation.panel.hint"))
            tr_panel = gr.HTML(tv.panel_html(None))
            tr_note = gr.HTML("")
            with gr.Row():
                tr_refresh_btn = LOC.button(lambda: t("translation.panel.refresh"), size="sm")
                tr_impact_btn = LOC.button(lambda: t("translation.tier2.action"), size="sm")
            tr_impact = gr.HTML("")
            tr_refresh_btn.click(translation_panel_ui, [project_state, t_kind, t_id, ws_lang],
                                 [tr_panel, tr_note, tr_impact])
            tr_impact_btn.click(translation_impact_ui, [project_state, t_kind, t_id, ws_lang],
                                [tr_impact])
            t_id.change(translation_panel_ui, [project_state, t_kind, t_id, ws_lang],
                        [tr_panel, tr_note, tr_impact])
            with gr.Row(): t_master=gr.File(label='Master package'); t_files=gr.Files(label='Pacchetti lingua')
            t_btn.click(work_package_ui,[project_state,t_id,t_kind,t_tms],[t_msg,t_table,t_master,t_files])

        with gr.Tab(t("nav.jobs"),id="jobs") as tab_jobs:
            LOC.add(tab_jobs, label="nav.jobs")
            head_jobs=LOC.html(lambda: _page_head_html("kicker.workflow", "page.jobs.title", "page.jobs.subtitle"))
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

        with gr.Tab(t("nav.release"),id="release") as tab_release:
            LOC.add(tab_release, label="nav.release")
            head_release=LOC.html(lambda: _page_head_html("kicker.workflow", "page.release.title", "page.release.subtitle"))
            gr.HTML("<div class='ms-stepper'><div class='ms-step active'><span class='ms-step-num'>1</span>Readiness</div><div class='ms-step'><span class='ms-step-num'>2</span>Dettagli release</div><div class='ms-step'><span class='ms-step-num'>3</span>Genera pacchetto</div><div class='ms-step'><span class='ms-step-num'>4</span>Riepilogo</div></div>")
            with gr.Row(): r_pub=gr.Textbox(label='Publication ID',value='PUB-MAIN'); r_langs=gr.Textbox(label='Lingue target',placeholder='it-IT, de-DE'); r_profile=gr.Textbox(label='Profilo — opzionale')
            r_check=gr.Button('Verifica readiness',variant='primary'); r_summary=gr.HTML(); r_matrix=gr.Dataframe(interactive=False,wrap=True,visible=False); r_blockers=gr.Dataframe(label='Elementi che bloccano la release',interactive=False,wrap=True,visible=False); r_json=gr.File(label='Readiness audit')
            gr.HTML("<div class='ms-panel' style='margin-top:14px'><div class='ms-section-title'>Crea release controllata</div><div class='ms-section-sub'>Scegli modalità, responsabile e identificativo. I gate R18 restano invariati.</div></div>")
            r_mode=gr.Radio(['Collaudo interno','Produzione cliente'],value='Collaudo interno',label='Tipo release'); r_mode_help=gr.HTML(_release_mode_help('Collaudo interno'))
            with gr.Row(): r_by=gr.Textbox(label='Release manager'); r_id=gr.Textbox(label='Release ID — opzionale'); r_idml=gr.File(label='Template IDML — opzionale',type='filepath',file_types=['.idml'])
            r_release=gr.Button('Crea release',variant='primary',interactive=False); r_msg=gr.Markdown(); r_zip=gr.File(label='Release package'); r_artifacts=gr.Dataframe(interactive=False,visible=False)
            r_check.click(readiness_ui,[project_state,r_pub,r_langs,r_profile,r_mode],[r_summary,r_matrix,r_blockers,r_json,r_release]); r_mode.change(_release_mode_reset,[r_mode],[r_mode_help,r_release,r_msg,r_zip,r_artifacts]); r_release.click(release_ui,[project_state,r_pub,r_langs,r_by,r_id,r_profile,r_idml,r_mode],[r_msg,r_zip,r_artifacts])

            # ================================================================
            # UX Gate 2A: publishing preview + production export, moved here
            # from Contenuti.
            #
            # 4E.2 put the preview on the authoring tab as "normal workflow,
            # not an expert-only feature", and that reasoning still holds --
            # but the workflow it belongs to is the release cycle, which is
            # this tab, not the per-object writing loop. On Contenuti it was
            # measured sitting 1200px below the editor, behind the Save the
            # author actually came for, carrying the second of the two
            # "Profilo" selectors.
            #
            # Read-only by construction: no save/edit control anywhere in
            # this block, all rendering from publishing_preview.py's pure
            # model+HTML functions. Export is isolated: its own callbacks,
            # its own status card, never ST_OUT_FULL.
            # ================================================================
            LOC.add(gr.HTML(_section_head_html("release.preview.title", "release.preview.sub")),
                    value=lambda: _section_head_html("release.preview.title", "release.preview.sub"))
            with gr.Row():
                # The SAME variant profile the Contenuti evaluator uses --
                # both bound to profile_state, so what you evaluate a block
                # against and what you export can no longer disagree.
                pv_profile = LOC.add(gr.Dropdown(choices=[], value=None,
                                                 label=t("content.profile.label"),
                                                 info=t("content.profile.hint"), scale=2),
                                     label="content.profile.label", info="content.profile.hint")
                pv_refresh_btn = LOC.button(lambda: t("release.preview.refresh"), scale=1)
                pv_show_excluded = LOC.add(gr.Checkbox(label=t("release.preview.show_excluded"),
                                                       value=False, scale=1),
                                           label="release.preview.show_excluded")
            pv_output = LOC.add(gr.HTML(f"<div class='ms-pp-note-line'>{t('release.preview.empty')}</div>"),
                                value=lambda: f"<div class='ms-pp-note-line'>{t('release.preview.empty')}</div>")
            LOC.add(gr.HTML(f"<div class='ms-section-sub' style='margin-top:14px'>{t('release.export.sub')}</div>"),
                    value=lambda: f"<div class='ms-section-sub' style='margin-top:14px'>{t('release.export.sub')}</div>")
            with gr.Row():
                pv_export_pdf_btn = gr.Button("Esporta PDF", variant="primary", scale=1)
                pv_export_html_btn = gr.Button("Esporta HTML", scale=1)
            # One HTML output, nothing else: the export RESULT CARD (see
            # _export_result_card). Deliberately NOT a gr.File widget --
            # a filename card with a generic "Scarica" is exactly the UX
            # that failed manual acceptance, because "how do I open it?"
            # had no visible answer.
            pv_export_status = gr.HTML()

            # ============================================================
            # UX Gate 2A — variant profile: ONE value, two views.
            #
            # Contenuti's evaluator and Rilascio's preview/export both
            # answer "which variant profile am I working against?". They
            # used to own two unlinked gr.Dropdowns, both labelled
            # "Profilo", both filled from the same query -- so a user
            # could evaluate a block against one and export against
            # another with nothing on screen to say so.
            #
            # The fix is a single profile_state:
            #   * picking in EITHER selector writes the state;
            #   * entering EITHER tab re-reads the state into that tab's
            #     selector (studio_preview_profile_choices_ui preserves a
            #     still-valid selection rather than resetting it);
            #   * a project switch reloads the list and drops a profile
            #     that no longer exists, in both places.
            # Divergence is therefore not something the user can reach.
            # ============================================================
            st_variant_eval_profile.change(lambda v: v, [st_variant_eval_profile], [profile_state])
            pv_profile.change(lambda v: v, [pv_profile], [profile_state])

            tab_release.select(studio_preview_profile_choices_ui,
                               [project_state, profile_state], [pv_profile, profile_state])
            project_state.change(studio_preview_profile_choices_ui,
                                 [project_state, profile_state], [pv_profile, profile_state])

            # Preview refreshes on its own button, on a profile pick, and
            # on toggling "Mostra contenuti esclusi"; each independently
            # wired with no shared mutable state, matching the read-only,
            # side-effect-free nature of the whole feature.
            pv_refresh_btn.click(studio_preview_refresh_ui, [project_state, c_pub, pv_profile, pv_show_excluded], [pv_output])
            pv_profile.change(studio_preview_refresh_ui, [project_state, c_pub, pv_profile, pv_show_excluded], [pv_output])
            pv_show_excluded.change(studio_preview_refresh_ui, [project_state, c_pub, pv_profile, pv_show_excluded], [pv_output])
            # 4F.1: production exports -- isolated callbacks, isolated
            # outputs, same [project, publication, profile] inputs the
            # preview above already uses, so preview and deliverable can
            # never disagree about WHAT is being published.
            pv_export_pdf_btn.click(studio_export_pdf_ui, [project_state, c_pub, pv_profile],
                                    [pv_export_status])
            pv_export_html_btn.click(studio_export_html_ui, [project_state, c_pub, pv_profile],
                                     [pv_export_status])

        with gr.Tab(t("nav.settings"),id="settings") as tab_settings:
            LOC.add(tab_settings, label="nav.settings")
            head_settings=LOC.html(lambda: _page_head_html("kicker.system", "page.settings.title", "page.settings.subtitle"))
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

            # 4E.1: minimal dimension/value/profile registry management.
            # Deliberately explicit-button-refreshed (no live cascade),
            # matching this tab's own existing style (p_refresh/p_health
            # above) rather than the Studio panel's reactive wiring --
            # simpler and lower-risk for an admin-style screen. Reuses
            # actor_state (set once in "Redazione & contenuti") rather than
            # asking for a name a second time in this tab.
            gr.HTML("<div class='ms-section-title'>Varianti — dimensioni, valori e profili</div>"
                    "<div class='ms-section-sub'>Dimensioni e valori (es. Mercato = EU/US) usati per condizionare "
                    "topic e blocchi nell'editor «Redazione & contenuti»; i profili combinano un valore per "
                    "dimensione per valutare o pubblicare una variante specifica.</div>")
            with gr.Row():
                with gr.Column(elem_classes=["ms-settings-card"]):
                    gr.Markdown("**Dimensioni**")
                    v_dim_table = gr.Dataframe(interactive=False, max_height=220)
                    v_dim_refresh = gr.Button("Aggiorna elenco dimensioni")
                    with gr.Row():
                        v_dim_id = gr.Textbox(label="Id (es. market)")
                        v_dim_name = gr.Textbox(label="Nome (es. Mercato)")
                    v_dim_create_btn = gr.Button("+ Crea dimensione", variant="primary")
                    with gr.Row():
                        v_dim_target_id = gr.Textbox(label="Id dimensione esistente")
                        v_dim_new_name = gr.Textbox(label="Nuovo nome")
                    with gr.Row():
                        v_dim_rename_btn = gr.Button("Rinomina")
                        v_dim_deactivate_btn = gr.Button("Disattiva")
                        v_dim_activate_btn = gr.Button("Riattiva")
                    v_dim_msg = gr.Markdown()
                with gr.Column(elem_classes=["ms-settings-card"]):
                    gr.Markdown("**Valori**")
                    v_val_table = gr.Dataframe(interactive=False, max_height=220)
                    v_val_refresh = gr.Button("Aggiorna elenco valori")
                    with gr.Row():
                        v_val_dim_id = gr.Textbox(label="Id dimensione")
                        v_val_id = gr.Textbox(label="Id valore (es. eu)")
                        v_val_label = gr.Textbox(label="Etichetta (es. EU)")
                    v_val_create_btn = gr.Button("+ Crea valore", variant="primary")
                    with gr.Row():
                        v_val_target_id = gr.Textbox(label="Id valore esistente")
                        v_val_new_label = gr.Textbox(label="Nuova etichetta")
                    with gr.Row():
                        v_val_rename_btn = gr.Button("Rinomina")
                        v_val_deactivate_btn = gr.Button("Disattiva")
                        v_val_activate_btn = gr.Button("Riattiva")
                    v_val_msg = gr.Markdown()
            with gr.Row():
                with gr.Column(elem_classes=["ms-settings-card"]):
                    gr.Markdown("**Profili di variante**")
                    v_prof_table = gr.Dataframe(interactive=False, max_height=220)
                    v_prof_refresh = gr.Button("Aggiorna elenco profili")
                    with gr.Row():
                        v_prof_id = gr.Textbox(label="Id profilo")
                        v_prof_name = gr.Textbox(label="Nome profilo")
                    v_prof_config = gr.Textbox(label="Configurazione — una coppia dimensione=valore per riga",
                                               lines=3, placeholder="market=eu\nedition=service")
                    v_prof_create_btn = gr.Button("+ Crea profilo", variant="primary")
                    with gr.Row():
                        v_prof_target_id = gr.Textbox(label="Id profilo esistente")
                        v_prof_deactivate_btn = gr.Button("Disattiva")
                        v_prof_activate_btn = gr.Button("Riattiva")
                    v_prof_msg = gr.Markdown()
            v_dim_refresh.click(settings_variant_dimensions_table_ui, [project_state], [v_dim_table])
            v_dim_create_btn.click(settings_variant_create_dimension_ui,
                                   [project_state, v_dim_id, v_dim_name, actor_state], [v_dim_table, v_dim_msg])
            v_dim_rename_btn.click(settings_variant_rename_dimension_ui,
                                   [project_state, v_dim_target_id, v_dim_new_name, actor_state], [v_dim_table, v_dim_msg])
            v_dim_deactivate_btn.click(lambda pd_, did, ac: settings_variant_set_dimension_active_ui(pd_, did, False, ac),
                                       [project_state, v_dim_target_id, actor_state], [v_dim_table, v_dim_msg])
            v_dim_activate_btn.click(lambda pd_, did, ac: settings_variant_set_dimension_active_ui(pd_, did, True, ac),
                                     [project_state, v_dim_target_id, actor_state], [v_dim_table, v_dim_msg])
            v_val_refresh.click(settings_variant_values_table_ui, [project_state], [v_val_table])
            v_val_create_btn.click(settings_variant_create_value_ui,
                                   [project_state, v_val_dim_id, v_val_id, v_val_label, actor_state], [v_val_table, v_val_msg])
            v_val_rename_btn.click(settings_variant_rename_value_ui,
                                   [project_state, v_val_target_id, v_val_new_label, actor_state], [v_val_table, v_val_msg])
            v_val_deactivate_btn.click(lambda pd_, vid, ac: settings_variant_set_value_active_ui(pd_, vid, False, ac),
                                       [project_state, v_val_target_id, actor_state], [v_val_table, v_val_msg])
            v_val_activate_btn.click(lambda pd_, vid, ac: settings_variant_set_value_active_ui(pd_, vid, True, ac),
                                     [project_state, v_val_target_id, actor_state], [v_val_table, v_val_msg])
            v_prof_refresh.click(settings_variant_profiles_table_ui, [project_state], [v_prof_table])
            v_prof_create_btn.click(settings_variant_create_profile_ui,
                                    [project_state, v_prof_id, v_prof_name, v_prof_config, actor_state], [v_prof_table, v_prof_msg])
            v_prof_deactivate_btn.click(lambda pd_, pid, ac: settings_variant_set_profile_active_ui(pd_, pid, False, ac),
                                        [project_state, v_prof_target_id, actor_state], [v_prof_table, v_prof_msg])
            v_prof_activate_btn.click(lambda pd_, pid, ac: settings_variant_set_profile_active_ui(pd_, pid, True, ac),
                                      [project_state, v_prof_target_id, actor_state], [v_prof_table, v_prof_msg])

    _NAV_IDS=['home','revision','authoring','compliance','translations','jobs','release','settings']
    _NAV_BUTTONS=[nav_home,nav_revision,nav_authoring,nav_compliance,nav_translation,nav_jobs,nav_release,nav_settings]
    def _navigate(tab_id: str):
        updates=[gr.update(variant='primary' if item==tab_id else 'secondary') for item in _NAV_IDS]
        return (gr.Tabs(selected=tab_id), *updates)
    _nav_outputs=[workspace_tabs,*_NAV_BUTTONS]
    nav_home.click(lambda:_navigate('home'),None,_nav_outputs)
    nav_revision.click(lambda:_navigate('revision'),None,_nav_outputs)
    # 4F.1 export-UX fix (real defect, found by the browser gate): the
    # sidebar switches tabs PROGRAMMATICALLY via gr.Tabs(selected=...),
    # which does NOT fire authoring_tab.select() -- and the tab strip
    # itself is hidden by design (R23), so a user navigating normally
    # never triggered studio_preview_profile_choices_ui at all. The
    # Publishing Preview profile picker therefore stayed EMPTY, making
    # both the preview and the new PDF/HTML export unusable unless the
    # project happened to change in that session. Refreshing the picker
    # on the navigation itself is the fix, chained (never merged into the
    # nav callback) so navigation keeps working even if the project has
    # no profiles yet.
    # UX Gate 2A: the profile picker this chain used to refill lives in
    # Rilascio now, so navigating to Contenuti refills the CONTENUTI one.
    nav_authoring.click(lambda:_navigate('authoring'),None,_nav_outputs).then(
        studio_preview_profile_choices_ui,[project_state,profile_state],
        [st_variant_eval_profile,profile_state])
    nav_compliance.click(lambda:_navigate('compliance'),None,_nav_outputs)
    nav_translation.click(lambda:_navigate('translations'),None,_nav_outputs)
    nav_jobs.click(lambda:_navigate('jobs'),None,_nav_outputs)
    nav_release.click(lambda:_navigate('release'),None,_nav_outputs).then(
        studio_preview_profile_choices_ui,[project_state,profile_state],[pv_profile,profile_state])
    nav_settings.click(lambda:_navigate('settings'),None,_nav_outputs)
    home_revision.click(lambda:_navigate('revision'),None,_nav_outputs)
    home_translation.click(lambda:_navigate('translations'),None,_nav_outputs)
    home_release.click(lambda:_navigate('release'),None,_nav_outputs).then(
        studio_preview_profile_choices_ui,[project_state,profile_state],[pv_profile,profile_state])
    home_compliance.click(lambda:_navigate('compliance'),None,_nav_outputs)
    # Milestone 3A: "Rivedi il contenuto importato" opens the Contenuti tab, whose
    # DEFAULT view (c_simple_table) is now the friendly standard view — the
    # technical dataframes live only inside the collapsed "modalità avanzata"
    # accordion, so they're never the primary call-to-action.
    lg_review.click(lambda:(*_navigate('authoring'),gr.update(visible=False)),None,[*_nav_outputs,legacy_onboarding_panel]).then(
        studio_preview_profile_choices_ui,[project_state,profile_state],
        [st_variant_eval_profile,profile_state])
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

    # Static labels first, then a re-render of the Conformità workspace.
    # The second step is not optional: the working list, the status bar and
    # the requirement detail are CALLBACK OUTPUTS, so their words are
    # whatever the last render produced. Without this the sidebar would
    # switch to English above a table still headed "Titolo / Stato".
    # Static labels first, then a re-render of the Conformità workspace.
    # The second step is not optional: the working list, the status bar and
    # the requirement detail are CALLBACK OUTPUTS, so their words are
    # whatever the last render produced. Without this the sidebar would
    # switch to English above a table still headed "Titolo / Stato".
    #
    # ws_lang is passed as an INPUT to each step. That is what makes the
    # switch session-scoped: a Gradio component's value belongs to the
    # session that set it, so each request renders in its own language and
    # no shared variable decides it for everybody.
    ws_lang.change(switch_language_ui, ws_lang, LOC.components(),
                   trigger_mode="once", concurrency_limit=1).then(
        compliance_switch_scope_ui, [project_state, cp_scope, ws_lang], cp_out_full).then(
        compliance_pack_state_ui, [project_state, ws_lang],
        [cp_pack_state, cp_pack_update_row, cp_pack_provenance]).then(
        compliance_list_ui, [project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_table).then(
        compliance_coverage_ui, [project_state, cp_scope, ws_lang], [cp_coverage, cp_gaps]).then(
        # UX Gate 2A: the same treatment Compliance's data-driven panels
        # already get -- one display-only element, never the editor.
        studio_context_relabel_ui, [project_state, c_pub, ws_lang], [st_pub_header]).then(
        # UX Gate 2B.1: the Impact panel is data-driven too -- its records
        # are codes, so re-rendering them is the whole translation step.
        # Read-only, and it touches no editor slot.
        studio_impact_panel_ui, [project_state, st_kind, st_id, ws_lang],
        [st_impact_compact, st_impact_details]).then(
        # UX Gate 2B.2: same treatment -- codes and params, so re-rendering
        # them IS the translation step. Tier 2 output is cleared rather
        # than silently left in the previous language.
        translation_panel_ui, [project_state, st_kind, st_id, ws_lang],
        [st_tr_panel, st_tr_note, st_tr_impact])

if __name__=='__main__':
    # MS_APP_PORT / MS_NO_BROWSER: launch overrides for automated
    # acceptance only. Defaults are exactly the previous behaviour
    # (the standard local address and port, opens a browser), so a normal
    # double-click start is
    # unchanged.
    #
    # They exist because the two browser gates must own the server they
    # drive: the active project is read ONCE at import (INITIAL_PROJECT),
    # so a gate that builds a disposable project cannot repoint a server
    # that is already running -- it would silently exercise whatever
    # project that server happened to start on. Each gate now starts its
    # own instance on its own free port, which also means running one
    # cannot disturb an app the user already has open on 7860.
    _port = int(os.environ.get("MS_APP_PORT", "7860"))
    _open_browser = not os.environ.get("MS_NO_BROWSER")
    demo.launch(server_name='127.0.0.1',server_port=_port,inbrowser=_open_browser,show_error=False,theme=gr.themes.Base(),css=CSS,favicon_path=str(Path(__file__).with_name('manualsync.png')) if Path(__file__).with_name('manualsync.png').exists() else None,allowed_paths=[str(DATA_ROOT.resolve())],footer_links=[])
