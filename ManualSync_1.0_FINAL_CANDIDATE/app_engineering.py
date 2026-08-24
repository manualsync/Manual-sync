from __future__ import annotations

import tempfile
from pathlib import Path

import gradio as gr

from layout_engine import ENGINE_BUILD, analyze_layouts, build_review_queue, save_analysis_outputs
from cat_tm import CAT_BUILD, load_terminology, run_cat_pipeline
from cat_batch import BATCH_BUILD, run_batch_pipeline
from asset_intelligence import ASSET_BUILD, DEFAULT_RATES, run_asset_intelligence
from authoring_ccms import AUTHORING_BUILD, ContentRepository, import_pdf_as_topics, export_publication_html
from ccms_industrial import INDUSTRIAL_BUILD, IndustrialRepository, upgrade_legacy_topics_to_blocks
from publishing_bridges import PUBLISHING_BUILD, export_publication_docx, patch_idml_template
from grounded_authoring import GROUNDING_BUILD, GroundingStore
from generative_orchestrator import GENERATION_BUILD, GenerationLedger, GenerationRequest, GenerativeOrchestrator, provider_from_settings
from impact_economics import IMPACT_BUILD, build_change_workload, apply_leverage, estimate_cost, impact_summary
from idml_hardening import IDML_HARDENING_BUILD, inspect_idml, patch_idml_slots, audit_idml_roundtrip
from translation_impact import TRANSLATION_IMPACT_BUILD, analyze_translation_impact
from translation_work_package import WORK_PACKAGE_BUILD, generate_translation_work_packages
from translation_roundtrip import ROUNDTRIP_BUILD, inspect_translation_roundtrip, commit_translation_roundtrip
from workflow_management import WORKFLOW_BUILD, WorkflowManager
from cat_workspace import (
    WORKSPACE_BUILD, concordance_search, create_editor_dataframe, focus_segment,
    qa_preview, save_workspace, update_focus_segment,
)

DETAIL_COLUMNS = [
    "tipo_strutturale", "stato", "rischio", "categoria_match", "metodo_abbinamento",
    "decisione_riuso", "richiede_revisione", "motivo_revisione",
    "vecchio_originale", "nuovo_originale", "vecchia_traduzione",
    "similarita_linguistica", "affidabilita_abbinamento", "affidabilita_traduzione",
    "pagina_vecchia", "pagina_nuova", "valori_tecnici_vecchi", "valori_tecnici_nuovi",
    "codici_vecchi", "codici_nuovi", "riferimenti_vecchi", "riferimenti_nuovi",
    "terminologia_approvata", "unicita_globale",
]

CAT_VISIBLE = [
    "segment_id", "page", "source", "match_category", "raw_linguistic_score", "linguistic_score",
    "tm_name", "tm_priority", "tm_penalty", "technical_safe", "decision",
    "pretranslation", "suggested_translation", "term_suggestions", "inline_tokens",
    "technical_reason", "terminology_status", "terminology_qa",
]


def run_analysis(old_source, old_translation, new_source, full_new_source, old_pages, translation_pages, new_pages, start_anchor, terminology_glossary):
    if not old_source or not old_translation or not new_source:
        raise gr.Error("Carica i tre PDF richiesti.")
    try:
        text_df, table_df, image_df, summary = analyze_layouts(
            old_source,
            old_translation,
            new_source,
            old_pages=old_pages or "",
            translation_pages=translation_pages or "",
            new_pages=new_pages or "",
            start_anchor=start_anchor or "",
            terminology_glossary=terminology_glossary or None,
            full_new_source_pdf=full_new_source or None,
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc

    queue = build_review_queue(text_df)
    temp_dir = Path(tempfile.mkdtemp(prefix="manualsync_v12_tech_"))
    paths = save_analysis_outputs(text_df, table_df, image_df, summary, temp_dir)
    summary_md = "### Technical Intelligence\n" + "  \n".join(
        f"**{key.replace('_', ' ').title()}:** {value}" for key, value in summary.items()
    )
    detail = text_df[[c for c in DETAIL_COLUMNS if c in text_df.columns]].copy()
    image_visible = image_df.drop(
        columns=["vecchia_immagine", "nuova_immagine", "estensione_vecchia", "estensione_nuova"],
        errors="ignore",
    )
    return (
        summary_md,
        queue,
        detail,
        table_df,
        image_visible,
        paths["rapporto"],
        paths["coda"],
        paths["testo"],
        paths["tabelle"],
        paths["figure"],
    )


def run_cat(old_source, old_translation, new_source, source_lang, target_lang,
            old_pages, translation_pages, new_pages,
            tm_primary, penalty_primary, tm_secondary, penalty_secondary, tm_tertiary, penalty_tertiary,
            terminology, fuzzy_threshold):
    if not old_source or not old_translation or not new_source:
        raise gr.Error("Carica vecchio originale, vecchia traduzione e nuovo originale.")
    out = Path(tempfile.mkdtemp(prefix="manualsync_v14_cat_"))
    external_tms = []
    for path, name, priority, penalty in [
        (tm_primary, "TM esterna primaria", 90, penalty_primary),
        (tm_secondary, "TM esterna secondaria", 80, penalty_secondary),
        (tm_tertiary, "TM esterna terziaria", 70, penalty_tertiary),
    ]:
        if path:
            external_tms.append({
                "path": path, "name": name, "priority": priority,
                "penalty": float(penalty or 0), "origin": "TMX esterna",
            })
    try:
        result = run_cat_pipeline(
            old_source,
            old_translation,
            new_source,
            out,
            source_lang=(source_lang or "en-US").strip(),
            target_lang=(target_lang or "it-IT").strip(),
            old_pages=old_pages or "",
            translation_pages=translation_pages or "",
            new_pages=new_pages or "",
            terminology_path=terminology or None,
            fuzzy_threshold=float(fuzzy_threshold or 75),
            external_tms=external_tms,
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc

    summary = result["summary"]
    simple_summary = {k: v for k, v in summary.items() if k not in {"tmx_imports", "tm_profiles"}}
    summary_md = "### CAT Pro / Translation Memory\n" + "  \n".join(
        f"**{key.replace('_', ' ').title()}:** {value}" for key, value in simple_summary.items()
    )
    if summary.get("tm_profiles"):
        summary_md += "\n\n**TM attive:**\n" + "\n".join(
            f"- {x['tm_name']} · priorità {x['priority']} · penalità {x['penalty']} · {x['entries']} TU"
            for x in summary["tm_profiles"]
        )
    matches = result["matches"][[c for c in CAT_VISIBLE if c in result["matches"].columns]].copy()
    editor = create_editor_dataframe(result["matches"])
    focus = focus_segment(editor, 0)
    focus_meta = _focus_metadata(focus)
    return (
        summary_md,
        matches,
        editor,
        result["legacy_audit"],
        str(result["report"]),
        str(result["tm_sqlite"]),
        str(result["tmx"]),
        str(result["xliff"]),
        str(result["srx"]),
        str(result["matches_csv"]),
        str(result["legacy_csv"]),
        str(out),
        1 if focus["total"] else 0,
        focus["source"], focus["target"], focus["status"],
        focus["suggestion"], focus["term_suggestions"], focus_meta,
    )



def run_batch_ui(old_source, old_translation, new_sources, source_lang, target_lang,
                 old_pages, translation_pages, new_pages, terminology, fuzzy_threshold):
    if not old_source or not old_translation or not new_sources:
        raise gr.Error("Carica vecchio originale, vecchia traduzione e almeno un nuovo originale.")
    files = new_sources if isinstance(new_sources, (list, tuple)) else [new_sources]
    out = Path(tempfile.mkdtemp(prefix="manualsync_v15_batch_"))
    try:
        result = run_batch_pipeline(
            old_source, old_translation, files, out,
            source_lang=(source_lang or "en-US").strip(),
            target_lang=(target_lang or "it-IT").strip(),
            old_pages=old_pages or "", translation_pages=translation_pages or "", new_pages=new_pages or "",
            terminology_path=terminology or None,
            fuzzy_threshold=float(fuzzy_threshold or 75),
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    summary = result["summary"]
    summary_md = "### CAT Production Batch\n" + "  \n".join(
        f"**{k.replace('_',' ').title()}:** {v}" for k,v in summary.items() if k not in {"tm_profiles","tm_imports","word_bands"}
    )
    return (
        summary_md, result["matches"], result["bands"], result["repetitions"],
        str(result["tm_sqlite"]), str(result["tmx"]), str(result["xliff_inline"]),
        str(result["qa_profile"]), str(result["matches_csv"]), str(result["bands_csv"]),
        str(result["repetitions_csv"]), str(result["package"]), str(out),
    )


def run_asset_ui(tm_path, matches_csv, vendor_csv,
                 rate_new, rate_context, rate_100, rate_95, rate_85, rate_75, rate_tech):
    if not tm_path or not matches_csv:
        raise gr.Error("Carica una TM SQLite ManualSync e il CSV dei match/batch.")
    out = Path(tempfile.mkdtemp(prefix="manualsync_v16_asset_"))
    rates = {
        "NEW": float(rate_new), "<75%": float(rate_new), "CONTEXT": float(rate_context),
        "100%": float(rate_100), "95-99%": float(rate_95), "85-94%": float(rate_85),
        "75-84%": float(rate_75), "TECHNICAL BLOCK": float(rate_tech),
    }
    try:
        result = run_asset_intelligence(tm_path, matches_csv, out, rates=rates, vendor_csv=vendor_csv or None)
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    t = result["tm_summary"]
    c = result["cost_summary"]
    l = result["leverage"]
    summary = (
        "### Language Asset & Cost Intelligence\n"
        f"**TM Health:** {t['health_score']}/100 · **Entries:** {t['total_entries']} · "
        f"**Conflitti:** {t['conflicting_sources']} · **Unsafe:** {t['unsafe_entries']}  \n"
        f"**Parole sorgente:** {l['total_words']} · **Riuso automatico:** {l['automatic_reuse_pct']}%  \n"
        f"**Costo tutto-new:** € {c['baseline_all_new_cost']:.2f} · **Costo stimato:** € {c['projected_cost']:.2f} · "
        f"**Risparmio stimato:** € {c['estimated_saving']:.2f} ({c['estimated_saving_pct']}%)"
    )
    return (
        summary, result["issues"], result["cost_breakdown"], result["vendors"],
        str(result["report"]), str(result["issues_csv"]), str(result["cost_csv"]),
        str(result["vendor_csv"]), str(result["summary_json"]),
    )



def run_authoring_import(pdf_path, source_lang, page_spec, publication_title):
    if not pdf_path:
        raise gr.Error("Carica un manuale PDF da migrare nel repository strutturato.")
    out = Path(tempfile.mkdtemp(prefix="manualsync_v18_ccms_"))
    db = out / "ManualSync_CCMS.sqlite"
    repo = ContentRepository(db)
    try:
        imported = import_pdf_as_topics(
            repo, pdf_path, source_lang=(source_lang or "en-US").strip(), page_spec=page_spec or "",
            id_prefix="MIG", source_ref_prefix=Path(pdf_path).name,
        )
        if imported.empty:
            raise gr.Error("Non sono riuscito a ricostruire topic strutturali dal PDF selezionato.")
        topic_ids = imported["topic_id"].astype(str).tolist()
        repo.create_publication("PUB-MIGRATED", publication_title or Path(pdf_path).stem, topic_ids, source_lang=(source_lang or "en-US").strip())
        html_path, summary = export_publication_html(repo, "PUB-MIGRATED", out / "ManualSync_PUBLICATION.html")
        topics = repo.topic_table()
        components = repo.component_table()
    finally:
        repo.close()
    summary_md = (
        "### Structured Authoring / CCMS\n"
        f"**Topic migrati:** {len(imported)} · **Pubblicazione:** {summary['title']}  \n"
        "Ogni topic conserva il riferimento alla pagina sorgente. La migrazione da PDF è dichiaratamente conservativa: "
        "ManualSync non finge che il PDF contenga la struttura semantica originale."
    )
    return summary_md, topics, components, str(db), str(html_path), str(out)


def authoring_add_component_ui(project_dir, component_id, title, body, source_ref):
    if not project_dir:
        raise gr.Error("Prima crea o importa un progetto authoring.")
    if not component_id or not body:
        raise gr.Error("ID componente e contenuto sono obbligatori.")
    db = Path(project_dir) / "ManualSync_CCMS.sqlite"
    repo = ContentRepository(db)
    try:
        try:
            repo.create_component(component_id.strip(), title or component_id, body, source_refs=[source_ref] if source_ref else [])
        except Exception:
            repo.update_component(component_id.strip(), title=title or None, body=body, source_refs=[source_ref] if source_ref else None)
        table = repo.component_table()
    finally:
        repo.close()
    return table, f"Componente **{component_id}** salvato. Nei topic usa `{{{{component:{component_id}}}}}` per riutilizzarlo."


def authoring_update_topic_ui(project_dir, topic_id, title, body, include_variants, exclude_variants, source_ref):
    if not project_dir or not topic_id:
        raise gr.Error("Progetto e topic sono obbligatori.")
    db = Path(project_dir) / "ManualSync_CCMS.sqlite"
    inc = [x.strip() for x in str(include_variants or "").split(",") if x.strip()]
    exc = [x.strip() for x in str(exclude_variants or "").split(",") if x.strip()]
    repo = ContentRepository(db)
    try:
        repo.update_topic(topic_id.strip(), title=title or None, body=body or None,
                          include_variants=inc, exclude_variants=exc,
                          source_refs=[source_ref] if source_ref else None)
        topics = repo.topic_table()
        history = repo.history(topic_id.strip(), kind="topic")
    finally:
        repo.close()
    return topics, history, f"Topic **{topic_id}** aggiornato con nuova versione immutabile."


def authoring_impact_ui(project_dir, changed_id, kind):
    if not project_dir or not changed_id:
        raise gr.Error("Indica progetto e ID modificato.")
    repo = ContentRepository(Path(project_dir) / "ManualSync_CCMS.sqlite")
    try:
        return repo.impact_map(changed_id.strip(), kind=kind)
    finally:
        repo.close()



def _ccms_db(project_dir):
    base = Path(project_dir)
    for name in ["ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"]:
        candidate = base / name
        if candidate.exists():
            return candidate
    raise gr.Error("Repository CCMS non trovato nel progetto.")


def ccms_upgrade_blocks_ui(project_dir):
    if not project_dir:
        raise gr.Error("Prima crea o importa un progetto authoring.")
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        result = upgrade_legacy_topics_to_blocks(repo)
        blocks = repo.blocks_table()
    finally:
        repo.close()
    return blocks, f"Migrazione block-level completata: **{int(result['created_blocks'].sum()) if len(result) else 0} blocchi creati**."


def ccms_profile_ui(project_dir, profile_id, title, dimensions_json):
    if not project_dir or not profile_id:
        raise gr.Error("Progetto e Profile ID sono obbligatori.")
    try:
        dimensions = __import__('json').loads(dimensions_json or '{}')
    except Exception as exc:
        raise gr.Error(f"JSON profilo non valido: {exc}")
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        try:
            repo.create_profile(profile_id.strip(), title or profile_id, dimensions)
        except Exception:
            repo.update_profile(profile_id.strip(), dimensions, title=title or None)
        frame, summary = repo.assemble_structured_publication('PUB-MIGRATED', profile_id=profile_id.strip())
    except KeyError:
        frame = repo.blocks_table()
        summary = {'blocks': len(frame), 'excluded_by_conditions': 0}
    finally:
        repo.close()
    return frame, f"Profilo **{profile_id}** salvato · blocchi risultanti: **{summary.get('blocks', len(frame))}** · esclusi: **{summary.get('excluded_by_conditions',0)}**."


def ccms_review_request_ui(project_dir, object_type, object_id, requested_by, assigned_to, comment):
    if not project_dir or not object_id:
        raise gr.Error("Progetto e Object ID sono obbligatori.")
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        rid = repo.request_review(object_type, object_id.strip(), requested_by=requested_by or 'author', assigned_to=assigned_to or '', comment=comment or '')
        queue = repo.review_queue()
    finally:
        repo.close()
    return queue, f"Review **#{rid}** aperta per `{object_type}:{object_id}`."


def ccms_review_decide_ui(project_dir, review_id, decision, actor, comment):
    if not project_dir or not review_id:
        raise gr.Error("Progetto e Review ID sono obbligatori.")
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        status = repo.decide_review(int(review_id), decision=decision, actor=actor or 'reviewer', comment=comment or '')
        queue = repo.review_queue()
    finally:
        repo.close()
    return queue, f"Review **#{int(review_id)}** chiusa · nuovo stato: **{status}**."


def ccms_publish_ui(project_dir, publication_id, profile_id, target_lang, idml_template):
    if not project_dir or not publication_id:
        raise gr.Error("Progetto e Publication ID sono obbligatori.")
    out = Path(tempfile.mkdtemp(prefix='manualsync_v18_publish_'))
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        docx, summary = export_publication_docx(repo, publication_id.strip(), out/'ManualSync_PUBLICATION.docx', profile_id=profile_id or None, target_lang=target_lang or None)
        idml_out = None
        idml_note = "IDML non generato: carica un template .idml esportato da InDesign con placeholder [[MANUALSYNC_CONTENT]]."
        if idml_template:
            idml_out, manifest = patch_idml_template(repo, publication_id.strip(), idml_template, out/'ManualSync_PUBLICATION.idml', profile_id=profile_id or None, target_lang=target_lang or None)
            idml_note = f"IDML bridge generato: {manifest['inserted_paragraphs']} paragrafi iniettati nel template."
    finally:
        repo.close()
    return str(docx), (str(idml_out) if idml_out else None), f"**Word generato.** {idml_note}  \nBlocchi pubblicati: **{summary.get('blocks',0)}**."


def ccms_impact_industrial_ui(project_dir, changed_id, kind):
    if not project_dir or not changed_id:
        raise gr.Error("Indica progetto e ID modificato.")
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        return repo.impact_map_industrial(changed_id.strip(), kind=kind)
    finally:
        repo.close()


def grounded_ingest_ui(source_pdf, source_text, document_id, title):
    if not document_id:
        raise gr.Error("Document ID obbligatorio.")
    if not source_pdf and not str(source_text or '').strip():
        raise gr.Error("Carica un PDF oppure incolla una fonte testuale.")
    out = Path(tempfile.mkdtemp(prefix='manualsync_v19_grounding_'))
    store = GroundingStore(out/'ManualSync_Grounding.sqlite')
    try:
        if source_pdf:
            chunks = store.ingest_pdf(document_id.strip(), title or document_id, source_pdf)
        else:
            chunks = store.ingest_text(document_id.strip(), title or document_id, source_text, source_ref_prefix=document_id.strip())
    finally:
        store.close()
    return chunks, str(out), f"Fonte **{document_id}** indicizzata in **{len(chunks)} chunk verificabili**."


def grounded_retrieve_ui(ground_dir, query):
    if not ground_dir or not query:
        raise gr.Error("Prima indicizza una fonte e inserisci una richiesta.")
    store = GroundingStore(Path(ground_dir)/'ManualSync_Grounding.sqlite')
    try:
        hits = store.retrieve(query, top_k=8)
        draft = store.extractive_draft(query, top_k=4, min_score=25)
        uncertainty = store.uncertainty_report(query, top_k=5)
    finally:
        store.close()
    citations = ",".join(draft.get('citations', []))
    msg = f"**Uncertainty:** {uncertainty['uncertainty']} · best support: **{uncertainty['best_support_score']}**. Il draft è estrattivo: non inventa wording non presente nelle fonti."
    return hits, draft.get('draft',''), citations, msg


def grounded_validate_ui(ground_dir, draft, citations):
    if not ground_dir or not draft:
        raise gr.Error("Grounding store e draft sono obbligatori.")
    ids=[x.strip() for x in str(citations or '').split(',') if x.strip()]
    store=GroundingStore(Path(ground_dir)/'ManualSync_Grounding.sqlite')
    try:
        result=store.validate_draft(draft,ids)
    finally:
        store.close()
    msg = "✅ **GROUNDING PASS**" if result['PASS'] else "⛔ **GROUNDING FAIL — il contenuto non può essere approvato/inserito automaticamente.**"
    return result['claims'], msg


def grounded_add_block_ui(ground_dir, project_dir, topic_id, block_id, position, draft, citations):
    if not ground_dir or not project_dir or not topic_id or not block_id:
        raise gr.Error("Grounding store, progetto, Topic ID e Block ID sono obbligatori.")
    ids=[x.strip() for x in str(citations or '').split(',') if x.strip()]
    store=GroundingStore(Path(ground_dir)/'ManualSync_Grounding.sqlite')
    repo=IndustrialRepository(_ccms_db(project_dir))
    try:
        store.add_grounded_block(repo,block_id=block_id.strip(),topic_id=topic_id.strip(),block_type='paragraph',position=int(position or 1),draft=draft,cited_chunk_ids=ids,status='Draft')
        blocks=repo.blocks_table(topic_id.strip())
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        store.close(); repo.close()
    return blocks, f"Blocco **{block_id}** inserito come `SOURCE_GROUNDED` in Draft. Deve ancora passare il workflow di review tecnica."



def generative_propose_ui(ground_dir, instruction, mode, provider_name, external_command):
    if not ground_dir or not instruction:
        raise gr.Error("Indicizza prima le fonti e inserisci una richiesta di authoring.")
    ground_path = Path(ground_dir) / 'ManualSync_Grounding.sqlite'
    if not ground_path.exists():
        raise gr.Error("Grounding store non trovato.")
    store = GroundingStore(ground_path)
    ledger = GenerationLedger(Path(ground_dir) / 'ManualSync_Generation.sqlite')
    try:
        provider = provider_from_settings(provider_name, external_command or '')
        orch = GenerativeOrchestrator(store, ledger, provider)
        result = orch.propose(GenerationRequest(str(instruction), mode=str(mode or 'procedure'), min_retrieval_score=20, min_claim_support=55))
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        store.close(); ledger.close()
    citations = ','.join(result.get('citations', []))
    status = result.get('status', '')
    if status == 'GROUNDING_PASS_REVIEW_REQUIRED':
        msg = f"✅ **GROUNDING PASS — REVIEW UMANA OBBLIGATORIA** · Provider: `{result.get('provider')}` · Proposal: `{result.get('proposal_id')}`"
    elif status == 'NEEDS_SOURCE':
        msg = "⚠️ **NEEDS SOURCE** — non ci sono evidenze sufficienti per generare una proposta."
    else:
        msg = f"⛔ **BLOCKED** — la proposta non può entrare nel CCMS. Provider: `{result.get('provider')}`"
    return result.get('evidence'), result.get('draft',''), citations, result.get('claims'), msg, result.get('proposal_id','')


def generative_queue_ui(ground_dir, project_dir, topic_id, block_id, position, draft, citations, proposal_id, assigned_to):
    if not ground_dir or not project_dir or not topic_id or not block_id or not draft:
        raise gr.Error("Grounding store, progetto CCMS, Topic ID, Block ID e draft sono obbligatori.")
    ids=[x.strip() for x in str(citations or '').split(',') if x.strip()]
    store = GroundingStore(Path(ground_dir)/'ManualSync_Grounding.sqlite')
    ledger = GenerationLedger(Path(ground_dir)/'ManualSync_Generation.sqlite')
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        validation = store.validate_draft(draft, ids)
        result = {
            'proposal_id': str(proposal_id or ''), 'provider':'ui-provider', 'draft':draft,
            'citations':ids, 'validation':validation,
        }
        orch = GenerativeOrchestrator(store, ledger)
        queued = orch.queue_into_ccms(
            result, repo, block_id=block_id.strip(), topic_id=topic_id.strip(), position=int(position or 1),
            assigned_to=str(assigned_to or '').strip(),
        )
        blocks = repo.blocks_table(topic_id.strip())
        queue = repo.review_queue(status='OPEN')
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        repo.close(); store.close(); ledger.close()
    return blocks, queue, f"✅ Blocco **{queued['block_id']}** inserito come `SOURCE_GROUNDED` e messo **In Review**. Non è approvato automaticamente."


def impact_economics_ui(project_dir, changed_id, kind, leverage_df, rate_card_df):
    if not project_dir or not changed_id:
        raise gr.Error("Progetto CCMS e ID modificato sono obbligatori.")
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        workload = build_change_workload(repo, str(changed_id).strip(), kind=str(kind or 'block'))
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        repo.close()
    lev = leverage_df if isinstance(leverage_df, __import__('pandas').DataFrame) else __import__('pandas').DataFrame(leverage_df or [])
    rates = rate_card_df if isinstance(rate_card_df, __import__('pandas').DataFrame) else __import__('pandas').DataFrame(rate_card_df or [])
    leveraged = apply_leverage(workload, lev if not lev.empty else None)
    try:
        costed, cost = estimate_cost(leveraged, rates if not rates.empty else None)
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    summary = impact_summary(workload)
    parts = [
        f"**Topic coinvolti:** {summary['affected_topics']}",
        f"**Pubblicazioni coinvolte:** {summary['affected_publications']}",
        f"**Lingue coinvolte:** {summary['affected_languages']}",
        f"**Parole sorgente coinvolte:** {summary['source_words']}",
    ]
    if cost.get('cost_available'):
        parts.append(f"**Costo stimato dalle tariffe fornite:** {cost['estimated_cost']:.2f}")
    else:
        parts.append("**Costo:** non calcolato — serve una rate card esplicita; ManualSync non inventa tariffe.")
    return costed, '  \n'.join(parts)



def translation_impact_ui(project_dir, cat_project_dir, batch_project_dir, extra_tms, changed_id, kind, fuzzy_threshold, rate_card_df):
    if not project_dir or not changed_id:
        raise gr.Error("Prima crea/apri un progetto CCMS e indica l'oggetto modificato.")
    tm_paths = []
    for state_dir, names in [
        (cat_project_dir, ["ManualSync_TM.sqlite"]),
        (batch_project_dir, ["ManualSync_BATCH_TM.sqlite"]),
    ]:
        if state_dir:
            for name in names:
                candidate = Path(state_dir) / name
                if candidate.exists():
                    tm_paths.append(candidate)
    if extra_tms:
        values = extra_tms if isinstance(extra_tms, (list, tuple)) else [extra_tms]
        tm_paths.extend(Path(x) for x in values if x)
    # Deduplicate without changing priority/order.
    seen = set(); tm_paths = [x for x in tm_paths if not (str(x.resolve()) in seen or seen.add(str(x.resolve())))]
    rates = rate_card_df if isinstance(rate_card_df, __import__('pandas').DataFrame) else __import__('pandas').DataFrame(rate_card_df or [])
    if not rates.empty and "target_lang" in rates.columns:
        rates = rates[rates["target_lang"].astype(str).str.strip().ne("")].copy()
    repo = IndustrialRepository(_ccms_db(project_dir))
    out = Path(tempfile.mkdtemp(prefix="manualsync_v22_translation_impact_"))
    try:
        result = analyze_translation_impact(
            repo, str(changed_id).strip(), kind=str(kind or "block"), tm_paths=tm_paths,
            fuzzy_threshold=float(fuzzy_threshold or 75), rate_card=rates if not rates.empty else None,
            require_approved=True, out_dir=out,
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        repo.close()
    o = result["overall"]
    parts = [
        "### Translation Impact Intelligence",
        f"**Oggetto ufficiale:** `{o['changed_kind']}:{o['changed_id']}` · versione **{o['source_version']}** · stato **{o['source_status']}**",
        f"**Lingua sorgente:** {o['source_lang']}",
        f"**Lingue target analizzate:** {o['target_languages']}",
        f"**Pubblicazioni coinvolte:** {o['affected_publications']}",
        f"**Parole sorgente effettive:** {o['source_words']}",
        f"**Parole riutilizzabili automaticamente (somma lingue):** {o['auto_reuse_words_across_languages']}",
        f"**Parole che richiedono intervento umano (somma lingue):** {o['human_attention_words_across_languages']}",
    ]
    if o.get("cost_available"):
        parts.append(f"**Costo stimato dalle sole tariffe fornite:** {o['estimated_cost']:.2f}")
    else:
        parts.append("**Costo:** non calcolato. ManualSync non inventa tariffe; puoi aggiungere una rate card opzionale.")
    if not tm_paths:
        parts.append("⚠️ Nessuna TM collegata: il sistema può rilevare le lingue già presenti nel CCMS, ma non può calcolare leverage CAT reale senza Translation Memory.")
    return (
        "  \n".join(parts), result["language_summary"], result["detail"], result["impact_map"],
        str(result["language_csv"]), str(result["detail_csv"]), str(result["summary_json"]),
    )



def translation_work_package_ui(project_dir, cat_project_dir, batch_project_dir, extra_tms, changed_id, kind, fuzzy_threshold):
    if not project_dir or not changed_id:
        raise gr.Error("Prima crea/apri un progetto CCMS e indica l'oggetto modificato.")
    tm_paths = []
    for state_dir, names in [
        (cat_project_dir, ["ManualSync_TM.sqlite"]),
        (batch_project_dir, ["ManualSync_BATCH_TM.sqlite"]),
    ]:
        if state_dir:
            for name in names:
                candidate = Path(state_dir) / name
                if candidate.exists():
                    tm_paths.append(candidate)
    if extra_tms:
        values = extra_tms if isinstance(extra_tms, (list, tuple)) else [extra_tms]
        tm_paths.extend(Path(x) for x in values if x)
    seen = set(); tm_paths = [x for x in tm_paths if not (str(x.resolve()) in seen or seen.add(str(x.resolve())))]
    repo = IndustrialRepository(_ccms_db(project_dir))
    out = Path(tempfile.mkdtemp(prefix="manualsync_v23_work_packages_"))
    try:
        result = generate_translation_work_packages(
            repo, str(changed_id).strip(), kind=str(kind or "block"),
            tm_paths=tm_paths, fuzzy_threshold=float(fuzzy_threshold or 75),
            require_approved=True, out_dir=out,
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        repo.close()
    packages = result["packages"]
    o = result["overall"]
    work_segments = int(packages["work_segments"].sum()) if not packages.empty else 0
    locked_segments = int(packages["auto_reuse_locked_segments"].sum()) if not packages.empty else 0
    parts = [
        "### Translation Work Package",
        f"**Oggetto ufficiale:** `{o['changed_kind']}:{o['changed_id']}` · versione **{o['source_version']}** · stato **{o['source_status']}**",
        f"**Lingue pacchettizzate:** {len(packages)}",
        f"**Segmenti inviati al lavoro umano:** {work_segments}",
        f"**Segmenti AUTO_REUSE esclusi e locked:** {locked_segments}",
        "**Policy:** i Technical Safe Block entrano con target vuoto; i riusi automatici non vengono inviati al traduttore; nessun pacchetto approva automaticamente una traduzione.",
    ]
    return (
        "  \n".join(parts), packages, str(result["master_zip"]),
        [str(x) for x in result["language_zips"]], str(result["packages_csv"]), str(result["master_manifest"]),
    )



def translation_roundtrip_qa_ui(project_dir, package_zip, returned_xlf, terminology_file, strict_terminology):
    if not project_dir or not package_zip or not returned_xlf:
        raise gr.Error("Progetto CCMS, pacchetto lingua v0.23 e XLIFF restituito sono obbligatori.")
    repo = IndustrialRepository(_ccms_db(project_dir))
    out = Path(tempfile.mkdtemp(prefix="manualsync_v24_roundtrip_qa_"))
    try:
        result = inspect_translation_roundtrip(
            repo, package_zip, returned_xlf, terminology_path=terminology_file or None,
            strict_terminology=bool(strict_terminology), out_dir=out,
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        repo.close()
    q = result["summary"]
    status = "✅ PRONTO PER APPROVAZIONE UMANA" if q["commit_allowed_after_human_approval"] else "⛔ BLOCCATO"
    blockers = " | ".join(q["global_blockers"]) if q["global_blockers"] else "nessuno"
    msg = (
        f"### Translation Round-Trip QA — {status}  \n"
        f"**Oggetto:** `{q['source']['kind']}:{q['source']['id']}` · source v{q['source']['version_current']} · **{q['source']['source_lang']} → {q['target_lang']}**  \n"
        f"**Segmenti controllati:** {q['segments']} · **Hard QA FAIL:** {q['hard_fail_count']} · **Terminologia:** {q['terminology_issue_count']} issue  \n"
        f"**Snapshot sorgente:** {'OK' if q['source_snapshot_ok'] else 'CAMBIATO'} · **Unit XLIFF:** {'OK' if q['unit_set_ok'] else 'NON COERENTI'} · **Lingue:** {'OK' if q['language_ok'] else 'NON COERENTI'}  \n"
        f"**Blocker:** {blockers}  \n"
        "Numeri, codici e placeholder sono hard gate non disattivabili. Anche con QA PASS serve comunque approvazione umana esplicita prima del commit."
    )
    return msg, result["audit"], str(result["audit_csv"]), str(result["summary_json"])


def translation_roundtrip_commit_ui(project_dir, package_zip, returned_xlf, terminology_file, strict_terminology, reviewer, human_approved):
    if not project_dir or not package_zip or not returned_xlf:
        raise gr.Error("Progetto CCMS, pacchetto lingua v0.23 e XLIFF restituito sono obbligatori.")
    if not human_approved:
        raise gr.Error("Spunta la conferma di revisione umana prima di registrare la traduzione.")
    repo = IndustrialRepository(_ccms_db(project_dir))
    out = Path(tempfile.mkdtemp(prefix="manualsync_v24_roundtrip_commit_"))
    try:
        result = commit_translation_roundtrip(
            repo, project_dir, package_zip, returned_xlf, human_approved=True,
            terminology_path=terminology_file or None, strict_terminology=bool(strict_terminology),
            reviewer=(reviewer or "Human reviewer").strip(), out_dir=out,
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        repo.close()
    q = result["summary"]
    msg = (
        "### ✅ Round-trip approvato e registrato  \n"
        f"**{q['source']['source_lang']} → {q['target_lang']}** · `{q['source']['kind']}:{q['source']['id']}` · source v{q['source']['version_current']}  \n"
        f"**TM confermata:** `{Path(q['tm_path']).name}` · nuove entry: **{q['tm_entries_added']}**  \n"
        f"**Revisore:** {q['reviewer']}  \n"
        "Il commit è stato rieseguito in fail-closed subito prima della scrittura in TM/CCMS; nessun QA precedente viene considerato sufficiente da solo."
    )
    return msg, str(result["tm_path"]), str(result["approval_zip"]), str(result["commit_json"]), result["ccms_commits"]



def _workflow_dashboard_md(manager: WorkflowManager) -> str:
    d = manager.dashboard()
    meta = manager.project_metadata()
    return (
        "### Workflow & Job Management\n"
        f"**Progetto:** {meta.get('title','ManualSync Project')}"
        + (f" · **Cliente:** {meta.get('client')}" if meta.get('client') else "")
        + (f" · **Owner:** {meta.get('owner')}" if meta.get('owner') else "")
        + "  \n"
        f"**Da inviare:** {d['ready_to_send']} · **In traduzione:** {d['in_translation']} · "
        f"**Ricevuti / QA:** {d['received_or_qa']} · **QA bloccati:** {d['qa_blocked']} · **Approvati:** {d['approved']}  \n"
        f"**Parole di lavoro ancora aperte:** {d['open_work_words']} · **Job scaduti:** {d['overdue']}"
    )


def workflow_refresh_ui(project_dir):
    if not project_dir:
        raise gr.Error("Prima crea/apri un progetto CCMS.")
    manager = WorkflowManager(project_dir)
    out = Path(tempfile.mkdtemp(prefix="manualsync_v25_workflow_export_"))
    try:
        files = manager.export_snapshot(out)
        return (
            _workflow_dashboard_md(manager), manager.jobs_table(), manager.vendors_table(), manager.events_table(),
            str(files["jobs"]), str(files["events"]), str(files["summary"]),
        )
    finally:
        manager.close()



def workflow_export_project_ui(project_dir):
    if not project_dir:
        raise gr.Error("Prima crea/apri un progetto CCMS.")
    manager = WorkflowManager(project_dir)
    try:
        archive = manager.export_project_archive()
        return str(archive), f"✅ Archivio progetto creato: **{Path(archive).name}**. Contiene CCMS, workflow, asset job, audit e TM presenti nel progetto."
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        manager.close()


def workflow_restore_project_ui(project_archive):
    if not project_archive:
        raise gr.Error("Carica un archivio progetto ManualSync.")
    try:
        restored = WorkflowManager.restore_project_archive(project_archive)
        manager = WorkflowManager(restored)
        try:
            msg = _workflow_dashboard_md(manager) + "\n\n✅ **Progetto ripristinato e impostato come progetto attivo.**"
            return str(restored), msg, manager.jobs_table(), manager.vendors_table(), manager.events_table()
        finally:
            manager.close()
    except Exception as exc:
        raise gr.Error(str(exc)) from exc

def workflow_project_metadata_ui(project_dir, title, client, owner):
    if not project_dir:
        raise gr.Error("Prima crea/apri un progetto CCMS.")
    manager = WorkflowManager(project_dir)
    try:
        manager.set_project_metadata(title or "ManualSync Project", client or "", owner or "")
        return _workflow_dashboard_md(manager), "Metadati progetto salvati."
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        manager.close()


def workflow_vendor_ui(project_dir, vendor_id, name, email, notes):
    if not project_dir:
        raise gr.Error("Prima crea/apri un progetto CCMS.")
    manager = WorkflowManager(project_dir)
    try:
        table = manager.upsert_vendor(vendor_id, name, email or "", notes or "")
        return table, f"Fornitore **{vendor_id}** salvato."
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        manager.close()


def workflow_create_job_ui(project_dir, package_zip, vendor_id, due_date, notes, actor):
    if not project_dir or not package_zip:
        raise gr.Error("Progetto CCMS e pacchetto lingua v0.23 sono obbligatori.")
    manager = WorkflowManager(project_dir)
    try:
        job_id = manager.create_job_from_package(
            package_zip, vendor_id=vendor_id or "", due_date=due_date or "", notes=notes or "", actor=actor or "workflow-user"
        )
        msg = _workflow_dashboard_md(manager) + f"\n\n✅ Job **`{job_id}`** creato nello stato **Da inviare**. Il pacchetto è stato copiato nell'archivio persistente del progetto."
        return msg, manager.jobs_table(), manager.events_table()
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        manager.close()


def workflow_job_action_ui(project_dir, job_id, action, actor, returned_xlf, terminology_file, strict_terminology, reviewer, human_approved):
    if not project_dir or not job_id:
        raise gr.Error("Progetto CCMS e Job ID sono obbligatori.")
    manager = WorkflowManager(project_dir)
    qa_frame = __import__('pandas').DataFrame()
    approval_zip = None
    tm_path = None
    try:
        action = str(action or "").strip()
        if action == "Segna come inviato":
            manager.mark_sent(job_id, actor=actor or "workflow-user")
            note = "Job segnato come **Inviato**."
        elif action == "Segna in traduzione":
            manager.mark_in_translation(job_id, actor=actor or "workflow-user")
            note = "Job segnato come **In traduzione**."
        elif action == "Registra XLIFF + esegui QA":
            if not returned_xlf:
                raise ValueError("Carica l'XLIFF restituito dal traduttore.")
            manager.register_returned_xliff(job_id, returned_xlf, actor=actor or "workflow-user")
            qa = manager.run_roundtrip_qa(
                job_id, _ccms_db(project_dir), terminology_path=terminology_file or None,
                strict_terminology=bool(strict_terminology), actor=actor or "workflow-user",
            )
            qa_frame = qa["qa"]["audit"]
            q = qa["qa"]["summary"]
            note = ("✅ QA PASS — job pronto per approvazione umana." if q["commit_allowed_after_human_approval"]
                    else "⛔ QA BLOCCATO — correggere la traduzione e registrare un nuovo XLIFF.")
        elif action == "Riesegui QA":
            job = manager.get_job(job_id)
            if job["status"] == "QA_BLOCKED":
                raise ValueError("Dopo un QA bloccato devi registrare un nuovo XLIFF corretto, non rieseguire lo stesso file.")
            qa = manager.run_roundtrip_qa(
                job_id, _ccms_db(project_dir), terminology_path=terminology_file or None,
                strict_terminology=bool(strict_terminology), actor=actor or "workflow-user",
            )
            qa_frame = qa["qa"]["audit"]
            q = qa["qa"]["summary"]
            note = "✅ QA PASS." if q["commit_allowed_after_human_approval"] else "⛔ QA BLOCCATO."
        elif action == "Approva e chiudi job":
            result = manager.approve_job(
                job_id, _ccms_db(project_dir), reviewer=reviewer or actor or "Human reviewer", human_approved=bool(human_approved)
            )
            approval_zip = str(result["commit"]["approval_zip"])
            tm_path = str(result["commit"]["tm_path"])
            note = "✅ Traduzione approvata, TM/CCMS aggiornati e job chiuso come **Approvato**."
        elif action == "Annulla job":
            manager.transition(job_id, "CANCELLED", actor=actor or "workflow-user")
            note = "Job annullato."
        else:
            raise ValueError("Seleziona un'azione valida.")
        return _workflow_dashboard_md(manager) + "\n\n" + note, manager.jobs_table(), manager.events_table(job_id), qa_frame, approval_zip, tm_path
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        manager.close()


def idml_preflight_ui(template_idml):
    if not template_idml:
        raise gr.Error("Carica un template IDML esportato da InDesign.")
    try:
        pre = inspect_idml(template_idml)
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    slots = __import__('pandas').DataFrame(pre.get('slots', []))
    msg = (
        f"**Preflight:** {'PASS' if pre.get('preflight_pass') else 'FAIL'} · "
        f"Stories: **{pre.get('story_count',0)}** · Slot: **{pre.get('slot_count',0)}** · "
        f"Spreads preservati: **{pre.get('spread_count',0)}** · Link parts: **{pre.get('link_parts',0)}**"
    )
    return slots, msg


def idml_export_v21_ui(project_dir, publication_id, template_idml, slot_map_json, style_map_json, profile_id, target_lang):
    if not project_dir or not publication_id or not template_idml:
        raise gr.Error("Progetto CCMS, Publication ID e template IDML sono obbligatori.")
    import json as _json
    try:
        slot_map = _json.loads(slot_map_json or '{}')
        style_map = _json.loads(style_map_json or '{}')
    except Exception as exc:
        raise gr.Error("Slot map / style map devono essere JSON validi.") from exc
    outdir = Path(tempfile.mkdtemp(prefix='manualsync_v21_idml_'))
    repo = IndustrialRepository(_ccms_db(project_dir))
    try:
        out, manifest = patch_idml_slots(
            repo, publication_id.strip(), template_idml, outdir/'ManualSync_PUBLICATION_v21.idml',
            profile_id=(profile_id or '').strip() or None, target_lang=(target_lang or '').strip() or None,
            slot_map=slot_map or None, style_map=style_map or None,
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    finally:
        repo.close()
    msg = (f"✅ **IDML bridge creato** · Stories modificate: **{len(manifest.get('changed_stories',[]))}** · "
           f"Parti non toccate byte-preserved: **{manifest.get('untouched_parts_preserved')}**. "
           "Tabelle native e geometry delle figure restano template-driven e non vengono inventate da ManualSync.")
    return str(out), msg


def idml_roundtrip_ui(idml_file):
    if not idml_file:
        raise gr.Error("Carica un IDML generato da ManualSync v0.21.")
    try:
        frame = audit_idml_roundtrip(idml_file)
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    changed = int((frame['status'].astype(str)!='UNCHANGED').sum()) if not frame.empty else 0
    msg = f"**Round-trip audit:** {changed} contenuti mappati modificati/mancanti su {len(frame)}."
    return frame, msg

def _qa_config(terminology_check, double_spaces, brackets, punctuation, capitalization, block_warnings):
    return {
        "terminology": bool(terminology_check),
        "double_spaces": bool(double_spaces),
        "brackets": bool(brackets),
        "terminal_punctuation": bool(punctuation),
        "capitalization": bool(capitalization),
        "block_on_warnings": bool(block_warnings),
        "technical_integrity": True,
    }


def _focus_metadata(info: dict) -> str:
    if not info.get("total"):
        return "Nessun segmento caricato."
    return (
        f"**Segmento {info['index'] + 1}/{info['total']} · {info['segment_id']}**  \n"
        f"Match: **{info['match_category']}** · Score: **{info['score']}** · "
        f"TM: **{info['tm_name'] or '—'}** · Penalità: **{info['tm_penalty']}**  \n"
        f"Tag/placeholder: `{info.get('inline_tokens') or 'nessuno'}`  \n"
        f"Technical Safe: {info.get('technical_reason') or 'OK'}"
    )


def focus_load_ui(editor_df, one_based_index):
    idx = max(0, int(one_based_index or 1) - 1)
    info = focus_segment(editor_df, idx)
    return info["index"] + 1 if info["total"] else 0, info["source"], info["target"], info["status"], info["suggestion"], info["term_suggestions"], _focus_metadata(info)


def focus_nav_ui(editor_df, one_based_index, delta):
    current = max(0, int(one_based_index or 1) - 1)
    return focus_load_ui(editor_df, current + int(delta) + 1)


def focus_save_ui(editor_df, one_based_index, target, status, terminology, source_lang, target_lang,
                  terminology_check, double_spaces, brackets, punctuation, capitalization, block_warnings):
    idx = max(0, int(one_based_index or 1) - 1)
    updated = update_focus_segment(editor_df, idx, target, status)
    info = focus_segment(updated, idx)
    terms = load_terminology(terminology, (source_lang or "en-US").strip(), (target_lang or "it-IT").strip()) if terminology else {}
    preview = qa_preview(
        info["source"], info["target"], terms,
        _qa_config(terminology_check, double_spaces, brackets, punctuation, capitalization, block_warnings),
    )
    qa_md = f"**QA segmento:** {preview['status']}"
    if preview["critical"]:
        qa_md += "  \n**Bloccanti:** " + "; ".join(preview["critical"])
    if preview["warnings"]:
        qa_md += "  \n**Avvisi:** " + "; ".join(preview["warnings"])
    if preview["status"] == "OK":
        qa_md += "  \nNessuna anomalia rilevata."
    return updated, qa_md, _focus_metadata(info)

def save_workspace_ui(editor_df, project_dir, source_lang, target_lang, terminology,
                      terminology_check, double_spaces, brackets, punctuation, capitalization, block_warnings):
    if not project_dir:
        raise gr.Error("Prima crea il progetto CAT con il pulsante 'Crea TM e pretraduci'.")
    try:
        result = save_workspace(
            editor_df, project_dir,
            (source_lang or "en-US").strip(),
            (target_lang or "it-IT").strip(),
            terminology_path=terminology or None,
            qa_config=_qa_config(terminology_check, double_spaces, brackets, punctuation, capitalization, block_warnings),
        )
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    summary = result["summary"]
    summary_md = "### Salvataggio e QA\n" + "  \n".join(
        f"**{key.replace('_',' ').title()}:** {value}" for key, value in summary.items()
    )
    return (
        summary_md, result["qa"],
        str(result["reviewed_xliff"]), str(result["updated_tmx"]),
        str(result["qa_csv"]), str(result["project_json"]), str(result["package"]),
    )

def concordance_ui(query, project_dir, source_lang, target_lang):
    if not project_dir:
        raise gr.Error("Prima crea la Translation Memory del progetto.")
    tm_path = Path(project_dir) / "ManualSync_TM.sqlite"
    if not tm_path.exists():
        raise gr.Error("Translation Memory non trovata.")
    return concordance_search(tm_path, query, (source_lang or "en-US").strip(), (target_lang or "it-IT").strip())


with gr.Blocks(title="ManualSync v0.25") as demo:
    gr.Markdown(
        f"""
        # ManualSync v0.25 — Unified Technical Documentation Platform
        **Technical Engine:** `{ENGINE_BUILD}`  
        **CAT Engine:** `{CAT_BUILD}`  
        **Workspace:** `{WORKSPACE_BUILD}`  
        **Batch Engine:** `{BATCH_BUILD}`  
        **Asset Intelligence:** `{ASSET_BUILD}`  
        **Authoring / CCMS foundation:** `{AUTHORING_BUILD}`  
        **CCMS Industrial:** `{INDUSTRIAL_BUILD}`  
        **Publishing bridges:** `{PUBLISHING_BUILD}`  
        **Grounded Authoring Guard:** `{GROUNDING_BUILD}`  \n        **Generative Orchestrator:** `{GENERATION_BUILD}`  \n        **Change Impact Economics:** `{IMPACT_BUILD}`  \n        **IDML Hardening:** `{IDML_HARDENING_BUILD}`  \n        **Translation Impact Intelligence:** `{TRANSLATION_IMPACT_BUILD}`  \n        **Translation Work Package:** `{WORK_PACKAGE_BUILD}`  \n        **Translation Round-Trip QA:** `{ROUNDTRIP_BUILD}`  \n        **Workflow / Job Management:** `{WORKFLOW_BUILD}`

        La v0.25 aggiunge il **workflow operativo persistente** sopra il round-trip v0.24: job per lingua, fornitore, scadenza, pacchetto outbound, XLIFF ricevuto, QA, approvazione e audit. Il runtime v0.21.1 resta riproducibile e fail-closed; nessuna traduzione viene approvata automaticamente.  
        I controlli tecnici critici su **numeri, codici e placeholder non sono disattivabili**.
        """
    )

    with gr.Tabs():
        with gr.Tab("1 · Technical Intelligence"):
            gr.Markdown("Confronto revisioni, sicurezza, tabelle, figure e coda tecnica — motore v0.11.6 invariato.")
            with gr.Row():
                old_source = gr.File(label="1. Vecchio originale PDF", type="filepath")
                old_translation = gr.File(label="2. Vecchia traduzione PDF", type="filepath")
                new_source = gr.File(label="3. Nuovo originale PDF / estratto", type="filepath")
            full_new_source = gr.File(
                label="Nuovo manuale completo — opzionale, per ricerca globale",
                type="filepath", file_types=[".pdf"],
            )
            with gr.Row():
                old_pages = gr.Textbox(label="Pagine vecchio originale")
                translation_pages = gr.Textbox(label="Pagine vecchia traduzione")
                new_pages = gr.Textbox(label="Pagine nuovo originale")
            start_anchor = gr.Textbox(label="Sezione iniziale opzionale", placeholder="es. 3.1")
            terminology_glossary = gr.File(
                label="Glossario equivalenze approvate — CSV opzionale",
                type="filepath", file_types=[".csv"],
            )
            analyze = gr.Button("Analizza con Technical Safe Match", variant="primary")
            tech_summary = gr.Markdown()
            with gr.Tabs():
                with gr.Tab("Coda di lavoro"):
                    queue_table = gr.Dataframe(interactive=False, wrap=True, max_height=760)
                with gr.Tab("Dettaglio CAT e tecnico"):
                    detail_table = gr.Dataframe(interactive=False, wrap=True, max_height=720)
                with gr.Tab("Tabelle"):
                    table_table = gr.Dataframe(interactive=False, wrap=True, max_height=650)
                with gr.Tab("Visuali"):
                    image_table = gr.Dataframe(interactive=False, wrap=True, max_height=650)
            with gr.Row():
                report_download = gr.File(label="Rapporto HTML")
                queue_download = gr.File(label="CSV coda")
                detail_download = gr.File(label="CSV testo")
                table_download = gr.File(label="CSV tabelle")
                image_download = gr.File(label="CSV figure")
            analyze.click(
                fn=run_analysis,
                inputs=[old_source, old_translation, new_source, full_new_source,
                        old_pages, translation_pages, new_pages, start_anchor, terminology_glossary],
                outputs=[tech_summary, queue_table, detail_table, table_table, image_table,
                         report_download, queue_download, detail_download, table_download, image_download],
            )

        with gr.Tab("2 · CAT Pro / Translation Memory"):
            gr.Markdown(
                """
                Il **Project Legacy** resta la memoria principale. Puoi aggiungere fino a tre TMX esterne: la penalità abbassa il punteggio effettivo e impedisce che una memoria meno affidabile diventi automaticamente un 100% riutilizzabile.  
                Un match linguistico non può mai superare il **Technical Safe Match**.
                """
            )
            with gr.Row():
                cat_old_source = gr.File(label="1. Vecchio originale PDF", type="filepath")
                cat_old_translation = gr.File(label="2. Vecchia traduzione PDF", type="filepath")
                cat_new_source = gr.File(label="3. Nuovo originale PDF", type="filepath")
            with gr.Row():
                source_lang = gr.Textbox(label="Lingua sorgente", value="en-US")
                target_lang = gr.Textbox(label="Lingua destinazione", value="it-IT")
                fuzzy_threshold = gr.Slider(50, 99, value=75, step=1, label="Soglia fuzzy effettiva")
            with gr.Row():
                cat_old_pages = gr.Textbox(label="Pagine vecchio originale")
                cat_translation_pages = gr.Textbox(label="Pagine vecchia traduzione")
                cat_new_pages = gr.Textbox(label="Pagine nuovo originale")

            gr.Markdown("### Translation Memory esterne")
            with gr.Row():
                tm_primary = gr.File(label="TMX primaria — opzionale", type="filepath", file_types=[".tmx", ".xml"])
                penalty_primary = gr.Slider(0, 30, value=0, step=1, label="Penalità primaria")
            with gr.Row():
                tm_secondary = gr.File(label="TMX secondaria — opzionale", type="filepath", file_types=[".tmx", ".xml"])
                penalty_secondary = gr.Slider(0, 30, value=5, step=1, label="Penalità secondaria")
            with gr.Row():
                tm_tertiary = gr.File(label="TMX terziaria — opzionale", type="filepath", file_types=[".tmx", ".xml"])
                penalty_tertiary = gr.Slider(0, 30, value=10, step=1, label="Penalità terziaria")
            terminology = gr.File(
                label="Terminologia CSV/TBX — opzionale",
                type="filepath", file_types=[".csv", ".tbx", ".xml"],
            )

            with gr.Accordion("Profilo QA", open=False):
                gr.Markdown("**Sempre attivi e non disattivabili:** coerenza numeri, codici/modelli e tag/placeholder.")
                with gr.Row():
                    qa_terminology = gr.Checkbox(value=True, label="Controllo terminologia")
                    qa_double_spaces = gr.Checkbox(value=True, label="Spazi doppi")
                    qa_brackets = gr.Checkbox(value=True, label="Parentesi bilanciate")
                with gr.Row():
                    qa_punctuation = gr.Checkbox(value=False, label="Punteggiatura finale")
                    qa_capitalization = gr.Checkbox(value=False, label="Maiuscola iniziale")
                    qa_block_warnings = gr.Checkbox(value=False, label="Blocca Confirmed anche sugli avvisi")

            cat_run = gr.Button("Crea TM, applica priorità e pretraduci", variant="primary")
            cat_summary = gr.Markdown()
            project_dir_state = gr.State("")

            with gr.Tabs():
                with gr.Tab("Segmenti e match"):
                    cat_matches = gr.Dataframe(interactive=False, wrap=True, max_height=760)

                with gr.Tab("Editor segmento"):
                    gr.Markdown("Lavora su un segmento alla volta. **Salva segmento** aggiorna la griglia; **Salva progetto** esegue il QA globale e scrive in TM solo i `Confirmed` ammessi.")
                    with gr.Row():
                        focus_index = gr.Number(label="Segmento", value=0, precision=0)
                        focus_prev = gr.Button("← Precedente")
                        focus_load = gr.Button("Carica")
                        focus_next = gr.Button("Successivo →")
                    focus_meta = gr.Markdown("Nessun segmento caricato.")
                    focus_source = gr.Textbox(label="Sorgente", lines=7, interactive=False)
                    focus_suggestion = gr.Textbox(label="Suggerimento TM", lines=5, interactive=False)
                    focus_terms = gr.Textbox(label="Terminologia suggerita", lines=2, interactive=False)
                    focus_target = gr.Textbox(label="Traduzione", lines=7)
                    focus_status = gr.Dropdown(choices=["Draft", "Reviewed", "Confirmed"], value="Draft", label="Stato")
                    focus_save = gr.Button("Salva segmento + QA rapido", variant="primary")
                    focus_qa = gr.Markdown()

                with gr.Tab("Griglia bilingue"):
                    gr.Markdown("Vista completa modificabile. Le colonne aggiuntive mostrano TM origine, penalità, terminologia e placeholder.")
                    bilingual_editor = gr.Dataframe(interactive=True, wrap=True, max_height=760, type="pandas")

                with gr.Tab("QA e salvataggio"):
                    save_workspace_btn = gr.Button("Salva progetto, esegui QA e aggiorna TM", variant="primary")
                    workspace_summary = gr.Markdown()
                    workspace_qa = gr.Dataframe(interactive=False, wrap=True, max_height=620)

                with gr.Tab("Concordance"):
                    with gr.Row():
                        concordance_query = gr.Textbox(label="Cerca nella TM", placeholder="es. mounting clearance")
                        concordance_btn = gr.Button("Cerca")
                    concordance_results = gr.Dataframe(interactive=False, wrap=True, max_height=600)

                with gr.Tab("Audit TM legacy"):
                    legacy_audit = gr.Dataframe(interactive=False, wrap=True, max_height=700)

            with gr.Row():
                cat_report = gr.File(label="Rapporto CAT HTML")
                sqlite_tm = gr.File(label="TM locale SQLite")
                tmx_out = gr.File(label="TMX esportata")
                xliff_out = gr.File(label="XLIFF workfile")
                srx_out = gr.File(label="Profilo SRX")
            with gr.Row():
                matches_csv = gr.File(label="CSV match")
                legacy_csv = gr.File(label="CSV audit import legacy")
            with gr.Row():
                reviewed_xliff = gr.File(label="XLIFF revisionato")
                updated_tmx = gr.File(label="TMX aggiornata")
                workspace_qa_csv = gr.File(label="QA workspace")
                project_json = gr.File(label="Progetto JSON")
                project_package = gr.File(label="Pacchetto progetto ZIP")

            cat_run.click(
                fn=run_cat,
                inputs=[
                    cat_old_source, cat_old_translation, cat_new_source,
                    source_lang, target_lang,
                    cat_old_pages, cat_translation_pages, cat_new_pages,
                    tm_primary, penalty_primary, tm_secondary, penalty_secondary, tm_tertiary, penalty_tertiary,
                    terminology, fuzzy_threshold,
                ],
                outputs=[
                    cat_summary, cat_matches, bilingual_editor, legacy_audit,
                    cat_report, sqlite_tm, tmx_out, xliff_out, srx_out,
                    matches_csv, legacy_csv, project_dir_state,
                    focus_index, focus_source, focus_target, focus_status,
                    focus_suggestion, focus_terms, focus_meta,
                ],
            )

            focus_load.click(
                fn=focus_load_ui,
                inputs=[bilingual_editor, focus_index],
                outputs=[focus_index, focus_source, focus_target, focus_status, focus_suggestion, focus_terms, focus_meta],
            )
            focus_prev.click(
                fn=lambda df, i: focus_nav_ui(df, i, -1),
                inputs=[bilingual_editor, focus_index],
                outputs=[focus_index, focus_source, focus_target, focus_status, focus_suggestion, focus_terms, focus_meta],
            )
            focus_next.click(
                fn=lambda df, i: focus_nav_ui(df, i, 1),
                inputs=[bilingual_editor, focus_index],
                outputs=[focus_index, focus_source, focus_target, focus_status, focus_suggestion, focus_terms, focus_meta],
            )
            focus_save.click(
                fn=focus_save_ui,
                inputs=[
                    bilingual_editor, focus_index, focus_target, focus_status, terminology, source_lang, target_lang,
                    qa_terminology, qa_double_spaces, qa_brackets, qa_punctuation, qa_capitalization, qa_block_warnings,
                ],
                outputs=[bilingual_editor, focus_qa, focus_meta],
            )

            save_workspace_btn.click(
                fn=save_workspace_ui,
                inputs=[
                    bilingual_editor, project_dir_state, source_lang, target_lang, terminology,
                    qa_terminology, qa_double_spaces, qa_brackets, qa_punctuation, qa_capitalization, qa_block_warnings,
                ],
                outputs=[workspace_summary, workspace_qa, reviewed_xliff, updated_tmx, workspace_qa_csv, project_json, project_package],
            )
            concordance_btn.click(
                fn=concordance_ui,
                inputs=[concordance_query, project_dir_state, source_lang, target_lang],
                outputs=[concordance_results],
            )

        with gr.Tab("3 · CAT Production Batch"):
            gr.Markdown(
                """
                Elaborazione **multifile** con una sola TM di progetto. ManualSync calcola match band, word-count, ripetizioni cross-documento, segmenti locked e genera un **XLIFF 2.0 con inline codes protetti**.
                """
            )
            with gr.Row():
                batch_old_source = gr.File(label="Vecchio originale PDF", type="filepath")
                batch_old_translation = gr.File(label="Vecchia traduzione PDF", type="filepath")
                batch_new_sources = gr.File(label="Nuovi originali PDF — multipli", type="filepath", file_count="multiple", file_types=[".pdf"])
            with gr.Row():
                batch_src_lang = gr.Textbox(value="en-US", label="Lingua sorgente")
                batch_tgt_lang = gr.Textbox(value="it-IT", label="Lingua destinazione")
                batch_fuzzy = gr.Slider(50, 99, value=75, step=1, label="Soglia fuzzy")
            with gr.Row():
                batch_old_pages = gr.Textbox(label="Pagine vecchio originale")
                batch_translation_pages = gr.Textbox(label="Pagine vecchia traduzione")
                batch_new_pages = gr.Textbox(label="Pagine nuovi originali")
            batch_terminology = gr.File(label="Terminologia CSV/TBX — opzionale", type="filepath", file_types=[".csv", ".tbx", ".xml"])
            batch_run = gr.Button("Crea progetto CAT batch", variant="primary")
            batch_summary = gr.Markdown()
            batch_project_state = gr.State("")
            with gr.Tabs():
                with gr.Tab("Tutti i segmenti"):
                    batch_matches = gr.Dataframe(interactive=False, wrap=True, max_height=720)
                with gr.Tab("Word count / match bands"):
                    batch_bands = gr.Dataframe(interactive=False, wrap=True)
                with gr.Tab("Ripetizioni"):
                    batch_repetitions = gr.Dataframe(interactive=False, wrap=True, max_height=620)
            with gr.Row():
                batch_tm_sqlite = gr.File(label="TM batch SQLite")
                batch_tmx = gr.File(label="TMX")
                batch_xlf = gr.File(label="XLIFF inline")
                batch_qa_profile = gr.File(label="QA profile JSON")
            with gr.Row():
                batch_matches_csv = gr.File(label="Batch matches CSV")
                batch_bands_csv = gr.File(label="Word bands CSV")
                batch_rep_csv = gr.File(label="Ripetizioni CSV")
                batch_package = gr.File(label="Pacchetto batch ZIP")
            batch_run.click(
                fn=run_batch_ui,
                inputs=[batch_old_source, batch_old_translation, batch_new_sources, batch_src_lang, batch_tgt_lang,
                        batch_old_pages, batch_translation_pages, batch_new_pages, batch_terminology, batch_fuzzy],
                outputs=[batch_summary, batch_matches, batch_bands, batch_repetitions,
                         batch_tm_sqlite, batch_tmx, batch_xlf, batch_qa_profile,
                         batch_matches_csv, batch_bands_csv, batch_rep_csv, batch_package, batch_project_state],
            )

        with gr.Tab("4 · Asset & Cost Intelligence"):
            gr.Markdown(
                """
                Primo pilastro **Aqrate-style**: audit delle Translation Memory, conflitti, entry tecnicamente unsafe, leverage reale e simulazione economica per match band. Le tariffe sono **input del cliente**, non valori inventati da ManualSync.
                """
            )
            with gr.Row():
                asset_tm = gr.File(label="ManualSync TM SQLite", type="filepath")
                asset_matches = gr.File(label="CAT/BATCH matches CSV", type="filepath", file_types=[".csv"])
                asset_vendors = gr.File(label="Vendor rates CSV — opzionale", type="filepath", file_types=[".csv"])
            with gr.Accordion("Tariffe €/parola", open=False):
                with gr.Row():
                    r_new = gr.Number(value=0.14, label="New / <75%")
                    r_context = gr.Number(value=0.014, label="Context")
                    r_100 = gr.Number(value=0.028, label="100%")
                    r_95 = gr.Number(value=0.056, label="95–99%")
                with gr.Row():
                    r_85 = gr.Number(value=0.084, label="85–94%")
                    r_75 = gr.Number(value=0.105, label="75–84%")
                    r_tech = gr.Number(value=0.14, label="Technical block")
            asset_run = gr.Button("Analizza asset, leverage e costi", variant="primary")
            asset_summary = gr.Markdown()
            with gr.Tabs():
                with gr.Tab("TM Health"):
                    asset_issues = gr.Dataframe(interactive=False, wrap=True, max_height=650)
                with gr.Tab("Cost breakdown"):
                    asset_cost = gr.Dataframe(interactive=False, wrap=True)
                with gr.Tab("Vendor ranking"):
                    asset_vendor_table = gr.Dataframe(interactive=False, wrap=True)
            with gr.Row():
                asset_report = gr.File(label="Rapporto HTML")
                asset_issues_csv = gr.File(label="TM issues CSV")
                asset_cost_csv = gr.File(label="Cost breakdown CSV")
                asset_vendor_csv = gr.File(label="Vendor ranking CSV")
                asset_json = gr.File(label="Summary JSON")
            asset_run.click(
                fn=run_asset_ui,
                inputs=[asset_tm, asset_matches, asset_vendors, r_new, r_context, r_100, r_95, r_85, r_75, r_tech],
                outputs=[asset_summary, asset_issues, asset_cost, asset_vendor_table,
                         asset_report, asset_issues_csv, asset_cost_csv, asset_vendor_csv, asset_json],
            )

        with gr.Tab("5 · Structured Authoring / CCMS"):
            gr.Markdown(
                """
                Fondazione **Paligo-style**, senza fingere una compatibilità completa: repository locale di **topic**, componenti riusabili, version history immutabile, varianti prodotto, traduzioni per topic, publication assembly e **change-impact map**.
                """
            )
            with gr.Row():
                author_pdf = gr.File(label="Manuale PDF legacy da migrare", type="filepath", file_types=[".pdf"])
                author_lang = gr.Textbox(value="en-US", label="Lingua sorgente")
                author_pages = gr.Textbox(label="Pagine opzionali")
                author_pub_title = gr.Textbox(label="Titolo pubblicazione")
            author_import_btn = gr.Button("Migra PDF in topic strutturati", variant="primary")
            author_summary = gr.Markdown()
            author_project_state = gr.State("")
            with gr.Tabs():
                with gr.Tab("Topic"):
                    author_topics = gr.Dataframe(interactive=False, wrap=True, max_height=650)
                with gr.Tab("Componenti riusabili"):
                    author_components = gr.Dataframe(interactive=False, wrap=True)
                    with gr.Row():
                        component_id = gr.Textbox(label="Component ID", placeholder="SAFETY-POWER")
                        component_title = gr.Textbox(label="Titolo")
                        component_source_ref = gr.Textbox(label="Source reference")
                    component_body = gr.Textbox(label="Contenuto componente", lines=6)
                    component_save = gr.Button("Salva componente")
                    component_message = gr.Markdown()
                with gr.Tab("Versiona topic"):
                    topic_id_edit = gr.Textbox(label="Topic ID")
                    topic_title_edit = gr.Textbox(label="Titolo nuovo — opzionale")
                    topic_body_edit = gr.Textbox(label="Contenuto nuovo — opzionale", lines=8)
                    with gr.Row():
                        topic_include = gr.Textbox(label="Varianti incluse, separate da virgola")
                        topic_exclude = gr.Textbox(label="Varianti escluse, separate da virgola")
                        topic_source_ref = gr.Textbox(label="Source reference")
                    topic_update_btn = gr.Button("Crea nuova versione topic")
                    topic_update_message = gr.Markdown()
                    topic_history = gr.Dataframe(interactive=False, wrap=True)
                with gr.Tab("Impact map"):
                    with gr.Row():
                        impact_id = gr.Textbox(label="Topic o Component ID")
                        impact_kind = gr.Dropdown(choices=["topic","component"], value="topic", label="Tipo")
                        impact_btn = gr.Button("Calcola impatto")
                    impact_table = gr.Dataframe(interactive=False, wrap=True)
            with gr.Accordion("CCMS Industrial v0.18 · blocchi, workflow e publishing", open=True):
                ccms_upgrade = gr.Button("Converti topic legacy in blocchi strutturati")
                ccms_upgrade_msg = gr.Markdown()
                ccms_blocks = gr.Dataframe(interactive=False, wrap=True, max_height=500)
                with gr.Row():
                    ccms_profile_id = gr.Textbox(label="Profile ID", placeholder="EU-R5V2-SERVICE")
                    ccms_profile_title = gr.Textbox(label="Titolo profilo")
                ccms_profile_json = gr.Textbox(label="Dimensioni profilo JSON", value='{"product":"R5v2","market":"EU","audience":"service","features":["EMC"]}', lines=3)
                ccms_profile_save = gr.Button("Salva / applica profilo")
                ccms_profile_msg = gr.Markdown()
                with gr.Tab("Workflow review"):
                    with gr.Row():
                        review_type = gr.Dropdown(choices=["topic","block","table","figure"], value="block", label="Tipo oggetto")
                        review_object = gr.Textbox(label="Object ID")
                        review_requested = gr.Textbox(value="author", label="Richiesto da")
                        review_assignee = gr.Textbox(label="Assegnato a")
                    review_comment = gr.Textbox(label="Commento")
                    review_open = gr.Button("Apri review")
                    with gr.Row():
                        review_id = gr.Number(label="Review ID", precision=0)
                        review_decision = gr.Dropdown(choices=["APPROVE","REJECT"], value="APPROVE", label="Decisione")
                        review_actor = gr.Textbox(value="technical-reviewer", label="Reviewer")
                    review_close = gr.Button("Chiudi review")
                    review_msg = gr.Markdown()
                    review_queue = gr.Dataframe(interactive=False, wrap=True)
                with gr.Tab("Publishing Word / IDML"):
                    with gr.Row():
                        pub_id_v18 = gr.Textbox(value="PUB-MIGRATED", label="Publication ID")
                        pub_profile_v18 = gr.Textbox(label="Profile ID opzionale")
                        pub_lang_v18 = gr.Textbox(label="Target language opzionale", placeholder="it-IT")
                    idml_template_v18 = gr.File(label="Template IDML opzionale", type="filepath", file_types=[".idml"])
                    publish_v18 = gr.Button("Genera Word + bridge IDML", variant="primary")
                    publish_msg_v18 = gr.Markdown()
                    with gr.Row():
                        word_out_v18 = gr.File(label="Word DOCX")
                        idml_out_v18 = gr.File(label="IDML patched")
                with gr.Tab("Impact map industriale"):
                    with gr.Row():
                        impact_id_v18 = gr.Textbox(label="Object ID")
                        impact_kind_v18 = gr.Dropdown(choices=["topic","block","table","figure","component"], value="block", label="Tipo")
                        impact_btn_v18 = gr.Button("Calcola impatto esteso")
                    impact_table_v18 = gr.Dataframe(interactive=False, wrap=True)

            with gr.Row():
                author_db = gr.File(label="CCMS SQLite")
                author_html = gr.File(label="Publication preview HTML")

            author_import_btn.click(
                fn=run_authoring_import,
                inputs=[author_pdf, author_lang, author_pages, author_pub_title],
                outputs=[author_summary, author_topics, author_components, author_db, author_html, author_project_state],
            )
            component_save.click(
                fn=authoring_add_component_ui,
                inputs=[author_project_state, component_id, component_title, component_body, component_source_ref],
                outputs=[author_components, component_message],
            )
            topic_update_btn.click(
                fn=authoring_update_topic_ui,
                inputs=[author_project_state, topic_id_edit, topic_title_edit, topic_body_edit, topic_include, topic_exclude, topic_source_ref],
                outputs=[author_topics, topic_history, topic_update_message],
            )
            ccms_upgrade.click(
                fn=ccms_upgrade_blocks_ui,
                inputs=[author_project_state],
                outputs=[ccms_blocks, ccms_upgrade_msg],
            )
            ccms_profile_save.click(
                fn=ccms_profile_ui,
                inputs=[author_project_state, ccms_profile_id, ccms_profile_title, ccms_profile_json],
                outputs=[ccms_blocks, ccms_profile_msg],
            )
            review_open.click(
                fn=ccms_review_request_ui,
                inputs=[author_project_state, review_type, review_object, review_requested, review_assignee, review_comment],
                outputs=[review_queue, review_msg],
            )
            review_close.click(
                fn=ccms_review_decide_ui,
                inputs=[author_project_state, review_id, review_decision, review_actor, review_comment],
                outputs=[review_queue, review_msg],
            )
            publish_v18.click(
                fn=ccms_publish_ui,
                inputs=[author_project_state, pub_id_v18, pub_profile_v18, pub_lang_v18, idml_template_v18],
                outputs=[word_out_v18, idml_out_v18, publish_msg_v18],
            )
            impact_btn_v18.click(
                fn=ccms_impact_industrial_ui,
                inputs=[author_project_state, impact_id_v18, impact_kind_v18],
                outputs=[impact_table_v18],
            )
            impact_btn.click(
                fn=authoring_impact_ui,
                inputs=[author_project_state, impact_id, impact_kind],
                outputs=[impact_table],
            )

        with gr.Tab("6 · Source-Grounded Authoring"):
            gr.Markdown(
                """
                **v0.19 — guardrail prima della generazione AI.** ManualSync indicizza fonti approvate, recupera evidenze, crea un draft **estrattivo** e verifica claim per claim. Numeri, codici e unità non supportati bloccano l'inserimento nel CCMS.
                """
            )
            ground_state = gr.State("")
            with gr.Row():
                ground_pdf = gr.File(label="Fonte PDF approvata", type="filepath", file_types=[".pdf"])
                ground_doc_id = gr.Textbox(label="Document ID", placeholder="ENG-SPEC-41")
                ground_title = gr.Textbox(label="Titolo fonte")
            ground_text = gr.Textbox(label="Oppure fonte testuale", lines=5)
            ground_ingest = gr.Button("Indicizza fonte", variant="primary")
            ground_ingest_msg = gr.Markdown()
            ground_chunks = gr.Dataframe(interactive=False, wrap=True, max_height=350)
            ground_query = gr.Textbox(label="Cosa vuoi redigere / verificare?", placeholder="R5 v2 top clearance EMC screw EU")
            ground_retrieve = gr.Button("Trova evidenze + draft estrattivo")
            ground_uncertainty = gr.Markdown()
            ground_hits = gr.Dataframe(interactive=False, wrap=True, max_height=350)
            ground_draft = gr.Textbox(label="Draft", lines=8)
            ground_citations = gr.Textbox(label="Chunk IDs citati")
            ground_validate = gr.Button("Verifica grounding")
            ground_validation_msg = gr.Markdown()
            ground_claims = gr.Dataframe(interactive=False, wrap=True)
            with gr.Accordion("Inserisci nel CCMS solo se PASS", open=False):
                with gr.Row():
                    ground_topic = gr.Textbox(label="Topic ID")
                    ground_block_id = gr.Textbox(label="Nuovo Block ID")
                    ground_position = gr.Number(value=1, label="Posizione", precision=0)
                ground_add = gr.Button("Inserisci blocco SOURCE_GROUNDED")
                ground_add_msg = gr.Markdown()
                ground_blocks = gr.Dataframe(interactive=False, wrap=True)

            ground_ingest.click(
                fn=grounded_ingest_ui,
                inputs=[ground_pdf, ground_text, ground_doc_id, ground_title],
                outputs=[ground_chunks, ground_state, ground_ingest_msg],
            )
            ground_retrieve.click(
                fn=grounded_retrieve_ui,
                inputs=[ground_state, ground_query],
                outputs=[ground_hits, ground_draft, ground_citations, ground_uncertainty],
            )
            ground_validate.click(
                fn=grounded_validate_ui,
                inputs=[ground_state, ground_draft, ground_citations],
                outputs=[ground_claims, ground_validation_msg],
            )
            ground_add.click(
                fn=grounded_add_block_ui,
                inputs=[ground_state, author_project_state, ground_topic, ground_block_id, ground_position, ground_draft, ground_citations],
                outputs=[ground_blocks, ground_add_msg],
            )

        with gr.Tab("7 · Generative Orchestrator + Impact"): 
            gr.Markdown(
                """
                **v0.20 — AI sotto controllo, non AI libera.** Il provider riceve solo evidenze recuperate da ManualSync. Le citazioni vengono bloccate al contesto consentito; numeri, codici e unità non supportati fanno fallire il grounding. Un contenuto PASS viene inserito nel CCMS soltanto come **In Review**.

                Il provider predefinito è un composer offline conservativo. È disponibile anche un adapter **External JSON Command** per collegare in seguito un LLM locale o remoto senza hard-wire del vendor.
                """
            )
            gen_state = gr.State("")
            with gr.Row():
                gen_instruction = gr.Textbox(label="Richiesta di authoring", placeholder="Crea una procedura per R5 v2 usando soltanto le fonti approvate")
                gen_mode = gr.Dropdown(["procedure","warning","rewrite","summary"], value="procedure", label="Tipo")
            with gr.Row():
                gen_provider = gr.Dropdown(["safe-source-composer","external-json-command"], value="safe-source-composer", label="Provider")
                gen_command = gr.Textbox(label="Comando provider esterno (opzionale)", placeholder="python my_llm_adapter.py")
            gen_run = gr.Button("Genera proposta vincolata alle fonti", variant="primary")
            gen_msg = gr.Markdown()
            gen_evidence = gr.Dataframe(interactive=False, wrap=True, max_height=300, label="Evidenze consegnate al provider")
            gen_draft = gr.Textbox(label="Proposta", lines=8)
            gen_citations = gr.Textbox(label="Chunk IDs citati")
            gen_claims = gr.Dataframe(interactive=False, wrap=True, label="Grounding claim-by-claim")
            with gr.Accordion("Invia al workflow CCMS", open=False):
                with gr.Row():
                    gen_topic = gr.Textbox(label="Topic ID")
                    gen_block = gr.Textbox(label="Nuovo Block ID")
                    gen_pos = gr.Number(value=1, label="Posizione", precision=0)
                    gen_reviewer = gr.Textbox(label="Revisore tecnico")
                gen_queue = gr.Button("Inserisci come In Review")
                gen_queue_msg = gr.Markdown()
                gen_blocks = gr.Dataframe(interactive=False, wrap=True)
                gen_review_queue = gr.Dataframe(interactive=False, wrap=True)

            gr.Markdown("### Change Impact → lingue → workload → costi")
            with gr.Row():
                impact_id20 = gr.Textbox(label="ID modificato", placeholder="BLK-I-01")
                impact_kind20 = gr.Dropdown(["block","topic","table","figure","component"], value="block", label="Tipo oggetto")
            leverage20 = gr.Dataframe(
                headers=["target_lang","reuse_pct","fuzzy_pct","technical_block_pct"],
                value=[["it-IT",0,0,0]], interactive=True, label="Leverage CAT reale/opzionale (%)"
            )
            rates20 = gr.Dataframe(
                headers=["target_lang","rate_new","rate_fuzzy","rate_reuse","rate_technical_block"],
                value=[["it-IT",0,0,0,0]], interactive=True, label="Rate card esplicita (nessuna tariffa inventata)"
            )
            impact_run20 = gr.Button("Calcola impatto e carico", variant="primary")
            impact_msg20 = gr.Markdown()
            impact_df20 = gr.Dataframe(interactive=False, wrap=True)

            gen_run.click(
                fn=generative_propose_ui,
                inputs=[ground_state, gen_instruction, gen_mode, gen_provider, gen_command],
                outputs=[gen_evidence, gen_draft, gen_citations, gen_claims, gen_msg, gen_state],
            )
            gen_queue.click(
                fn=generative_queue_ui,
                inputs=[ground_state, author_project_state, gen_topic, gen_block, gen_pos, gen_draft, gen_citations, gen_state, gen_reviewer],
                outputs=[gen_blocks, gen_review_queue, gen_queue_msg],
            )
            impact_run20.click(
                fn=impact_economics_ui,
                inputs=[author_project_state, impact_id20, impact_kind20, leverage20, rates20],
                outputs=[impact_df20, impact_msg20],
            )

        with gr.Tab("8 · InDesign / IDML Bridge"): 
            gr.Markdown(
                """
                **v0.21 — template-driven, non layout inventato.** ManualSync analizza un IDML esportato da InDesign, trova gli slot `[[MS:SLOT:nome]]`, inserisce i topic/blocchi nelle Stories designate e conserva byte-per-byte le parti del package che non deve modificare. Un manifest permette di verificare cosa è cambiato dopo un round-trip umano in InDesign.
                """
            )
            idml_template21 = gr.File(label="Template IDML InDesign", type="filepath", file_types=[".idml"])
            idml_pre_btn21 = gr.Button("Esegui preflight IDML")
            idml_pre_msg21 = gr.Markdown()
            idml_slots21 = gr.Dataframe(interactive=False, wrap=True, label="Slot trovati")
            with gr.Row():
                idml_pub21 = gr.Textbox(label="Publication ID", value="PUB-ACS")
                idml_profile21 = gr.Textbox(label="Profile ID (opzionale)")
                idml_lang21 = gr.Textbox(label="Lingua target (opzionale)")
            idml_slotmap21 = gr.Textbox(label="Slot map JSON", value='{"INSTALL":"INSTALL-001","SERVICE":"SERVICE-001"}', lines=3)
            idml_stylemap21 = gr.Textbox(label="Style map JSON", value='{"heading":"ParagraphStyle/MS Heading","body":"ParagraphStyle/MS Body","warning":"ParagraphStyle/MS Warning","note":"ParagraphStyle/MS Note"}', lines=3)
            idml_export_btn21 = gr.Button("Genera IDML dal CCMS", variant="primary")
            idml_export_msg21 = gr.Markdown()
            idml_out21 = gr.File(label="IDML generato")
            gr.Markdown("### Audit dopo modifica in InDesign")
            idml_roundtrip_file21 = gr.File(label="IDML rientrato da InDesign", type="filepath", file_types=[".idml"])
            idml_audit_btn21 = gr.Button("Verifica round-trip")
            idml_audit_msg21 = gr.Markdown()
            idml_audit_df21 = gr.Dataframe(interactive=False, wrap=True)

            idml_pre_btn21.click(fn=idml_preflight_ui, inputs=[idml_template21], outputs=[idml_slots21,idml_pre_msg21])
            idml_export_btn21.click(
                fn=idml_export_v21_ui,
                inputs=[author_project_state,idml_pub21,idml_template21,idml_slotmap21,idml_stylemap21,idml_profile21,idml_lang21],
                outputs=[idml_out21,idml_export_msg21],
            )
            idml_audit_btn21.click(fn=idml_roundtrip_ui, inputs=[idml_roundtrip_file21], outputs=[idml_audit_df21,idml_audit_msg21])

        with gr.Tab("9 · Translation Impact Intelligence"):
            gr.Markdown(
                """
                **v0.22 — dalla modifica approvata al lavoro di traduzione reale.** Seleziona un blocco/topic/tabella/figura già approvato nel CCMS. ManualSync usa direttamente le TM create nelle schede CAT oppure TM SQLite aggiuntive, esegue il Technical Safe Match segmento per segmento e calcola cosa è davvero riutilizzabile, cosa richiede revisione tecnica/linguistica e cosa è nuovo.

                Non devi più inserire percentuali di leverage a mano. Le tariffe restano facoltative e devono essere fornite esplicitamente.
                """
            )
            with gr.Row():
                ti_kind = gr.Dropdown(["block","topic","table","figure","component"], value="block", label="Tipo oggetto modificato")
                ti_id = gr.Textbox(label="ID oggetto approvato", placeholder="BLK-I-01")
                ti_fuzzy = gr.Slider(50, 99, value=75, step=1, label="Soglia fuzzy")
            ti_extra_tms = gr.File(label="TM ManualSync SQLite aggiuntive — opzionale / multilanguage", type="filepath", file_count="multiple", file_types=[".sqlite", ".db"])
            gr.Markdown("Le TM dell'ultimo progetto **CAT Pro** e **CAT Production Batch** vengono collegate automaticamente quando disponibili.")
            ti_rates = gr.Dataframe(
                headers=["target_lang","rate_new","rate_fuzzy","rate_reuse","rate_technical_block"],
                value=[["",0,0,0,0]], interactive=True,
                label="Rate card opzionale — €/parola (nessuna tariffa predefinita)",
            )
            ti_run = gr.Button("Calcola Translation Impact reale", variant="primary")
            ti_summary = gr.Markdown()
            with gr.Tabs():
                with gr.Tab("Riepilogo per lingua"):
                    ti_langs = gr.Dataframe(interactive=False, wrap=True)
                with gr.Tab("Segmenti / Technical Safe Match"):
                    ti_detail = gr.Dataframe(interactive=False, wrap=True, max_height=720)
                with gr.Tab("Pubblicazioni coinvolte"):
                    ti_map = gr.Dataframe(interactive=False, wrap=True)
            with gr.Row():
                ti_lang_csv = gr.File(label="CSV riepilogo lingue")
                ti_detail_csv = gr.File(label="CSV segmenti")
                ti_json = gr.File(label="Summary JSON")
            ti_run.click(
                fn=translation_impact_ui,
                inputs=[author_project_state, project_dir_state, batch_project_state, ti_extra_tms, ti_id, ti_kind, ti_fuzzy, ti_rates],
                outputs=[ti_summary, ti_langs, ti_detail, ti_map, ti_lang_csv, ti_detail_csv, ti_json],
            )

        with gr.Tab("10 · Translation Work Package"): 
            gr.Markdown(
                """
                **v0.23 — dall'Impact al file operativo da mandare al traduttore.** ManualSync genera un pacchetto XLIFF 2.0 separato per ogni lingua. Nel workfile entrano solo **LINGUISTIC_REVIEW**, **TECHNICAL_REVIEW** e **NEW_TRANSLATION**.

                Gli **AUTO_REUSE** sicuri vengono esclusi dal lavoro umano e registrati come locked. I **TECHNICAL_REVIEW** hanno sempre target vuoto: la vecchia traduzione può apparire solo come riferimento auditabile e non può essere riutilizzata automaticamente.
                """
            )
            with gr.Row():
                wp_kind = gr.Dropdown(["block","topic","table","figure","component"], value="block", label="Tipo oggetto approvato")
                wp_id = gr.Textbox(label="ID oggetto approvato", placeholder="BLK-I-01")
                wp_fuzzy = gr.Slider(50, 99, value=75, step=1, label="Soglia fuzzy")
            wp_extra_tms = gr.File(label="TM ManualSync SQLite aggiuntive — opzionale / multilanguage", type="filepath", file_count="multiple", file_types=[".sqlite", ".db"])
            gr.Markdown("Le TM dell'ultimo progetto **CAT Pro** e **CAT Production Batch** vengono collegate automaticamente quando disponibili.")
            wp_run = gr.Button("Genera pacchetti XLIFF per i traduttori", variant="primary")
            wp_summary = gr.Markdown()
            wp_table = gr.Dataframe(interactive=False, wrap=True)
            wp_master = gr.File(label="MASTER ZIP — tutti i pacchetti lingua")
            wp_lang_files = gr.File(label="Pacchetti ZIP per lingua", file_count="multiple")
            with gr.Row():
                wp_csv = gr.File(label="Indice pacchetti CSV")
                wp_manifest = gr.File(label="Master Manifest JSON")
            wp_run.click(
                fn=translation_work_package_ui,
                inputs=[author_project_state, project_dir_state, batch_project_state, wp_extra_tms, wp_id, wp_kind, wp_fuzzy],
                outputs=[wp_summary, wp_table, wp_master, wp_lang_files, wp_csv, wp_manifest],
            )

        with gr.Tab("11 · Translation Round-Trip QA"):
            gr.Markdown(
                """
                **v0.24 — il ritorno controllato dal traduttore.** Carica il pacchetto lingua ZIP generato dalla v0.23 e l'XLIFF 2.0 restituito. ManualSync verifica che il sorgente non sia cambiato, che non manchino/compaiano unit, che la lingua sia corretta e che **numeri, codici e placeholder** siano rimasti integri.

                Il QA **non approva nulla da solo**. Se tutti gli hard gate sono PASS, un revisore umano deve confermare esplicitamente il commit; solo allora ManualSync aggiorna la **Project Confirmed TM** e le traduzioni CCMS supportate.
                """
            )
            with gr.Row():
                rt_pkg = gr.File(label="Pacchetto lingua v0.23 (.zip)", type="filepath", file_types=[".zip"])
                rt_xlf = gr.File(label="XLIFF restituito dal traduttore (.xlf)", type="filepath", file_types=[".xlf", ".xliff"])
            with gr.Row():
                rt_term = gr.File(label="Terminologia approvata CSV/TBX — opzionale", type="filepath", file_types=[".csv", ".tbx", ".xml"])
                rt_strict = gr.Checkbox(value=False, label="Terminologia strict — blocca il commit se non rispettata")
            rt_qa = gr.Button("Esegui Round-Trip QA", variant="primary")
            rt_summary = gr.Markdown()
            rt_audit = gr.Dataframe(interactive=False, wrap=True, max_height=720)
            with gr.Row():
                rt_audit_csv = gr.File(label="QA segmenti CSV")
                rt_summary_json = gr.File(label="QA summary JSON")
            rt_qa.click(
                fn=translation_roundtrip_qa_ui,
                inputs=[author_project_state, rt_pkg, rt_xlf, rt_term, rt_strict],
                outputs=[rt_summary, rt_audit, rt_audit_csv, rt_summary_json],
            )
            gr.Markdown("### Approvazione umana e commit")
            with gr.Row():
                rt_reviewer = gr.Textbox(label="Revisore", placeholder="Nome / ruolo del revisore")
                rt_human = gr.Checkbox(value=False, label="Confermo che un revisore umano ha approvato la traduzione")
            rt_commit = gr.Button("Approva e registra in TM / CCMS")
            rt_commit_summary = gr.Markdown()
            rt_ccms = gr.Dataframe(interactive=False, wrap=True, label="Commit CCMS")
            with gr.Row():
                rt_tm = gr.File(label="Project Confirmed TM")
                rt_approval_zip = gr.File(label="Round-Trip Approval ZIP")
                rt_commit_json = gr.File(label="Commit audit JSON")
            rt_commit.click(
                fn=translation_roundtrip_commit_ui,
                inputs=[author_project_state, rt_pkg, rt_xlf, rt_term, rt_strict, rt_reviewer, rt_human],
                outputs=[rt_commit_summary, rt_tm, rt_approval_zip, rt_commit_json, rt_ccms],
            )

        with gr.Tab("12 · Workflow & Job Management"):
            gr.Markdown(
                """
                **v0.25 — il flusso aziendale sopra i motori tecnici.** Un job nasce da un pacchetto lingua v0.23 e segue una macchina a stati auditabile: **Da inviare → Inviato → In traduzione → Ricevuto → QA → Approvato**.

                Pacchetto outbound, XLIFF restituito, QA v0.24, approvazione, TM e audit vengono conservati nel progetto. Le transizioni impossibili sono bloccate: non si può, per esempio, approvare un job che non abbia prima superato il QA.
                """
            )
            wf_refresh = gr.Button("Aggiorna dashboard", variant="primary")
            wf_dashboard = gr.Markdown()
            with gr.Accordion("Salva / riapri progetto ManualSync", open=True):
                gr.Markdown("Esporta l'intero workspace in un archivio riapribile. Questo evita di dipendere dalla cartella temporanea della sessione.")
                with gr.Row():
                    wf_export_project = gr.Button("Esporta progetto completo")
                    wf_project_archive = gr.File(label="Archivio progetto ManualSync")
                wf_archive_msg = gr.Markdown()
                with gr.Row():
                    wf_restore_archive = gr.File(label="Riapri archivio progetto (.zip)", type="filepath", file_types=[".zip"])
                    wf_restore_btn = gr.Button("Riapri e imposta come progetto attivo")
                wf_restore_msg = gr.Markdown()
            with gr.Accordion("Progetto", open=False):
                with gr.Row():
                    wf_title = gr.Textbox(label="Titolo progetto", placeholder="Manuale macchina X — Rev. 2026")
                    wf_client = gr.Textbox(label="Cliente / Business unit")
                    wf_owner = gr.Textbox(label="Owner")
                wf_meta_save = gr.Button("Salva dati progetto")
                wf_meta_msg = gr.Markdown()
            with gr.Accordion("Fornitori", open=False):
                with gr.Row():
                    wf_vendor_id = gr.Textbox(label="Vendor ID", placeholder="LSP-01")
                    wf_vendor_name = gr.Textbox(label="Nome fornitore")
                    wf_vendor_email = gr.Textbox(label="Email")
                wf_vendor_notes = gr.Textbox(label="Note")
                wf_vendor_save = gr.Button("Salva fornitore")
                wf_vendor_msg = gr.Markdown()
                wf_vendors = gr.Dataframe(interactive=False, wrap=True)
            gr.Markdown("### Crea job dal pacchetto lingua")
            with gr.Row():
                wf_package = gr.File(label="Pacchetto lingua v0.23 (.zip)", type="filepath", file_types=[".zip"])
                wf_vendor = gr.Textbox(label="Vendor ID — opzionale")
                wf_due = gr.Textbox(label="Scadenza — opzionale", placeholder="YYYY-MM-DD")
            with gr.Row():
                wf_actor = gr.Textbox(label="Operatore", value="workflow-user")
                wf_notes = gr.Textbox(label="Note job")
            wf_create = gr.Button("Crea job — Da inviare", variant="primary")
            gr.Markdown("### Gestisci job")
            with gr.Row():
                wf_job_id = gr.Textbox(label="Job ID")
                wf_action = gr.Dropdown(
                    choices=["Segna come inviato","Segna in traduzione","Registra XLIFF + esegui QA","Riesegui QA","Approva e chiudi job","Annulla job"],
                    label="Azione",
                )
            with gr.Row():
                wf_returned = gr.File(label="XLIFF restituito — quando richiesto", type="filepath", file_types=[".xlf", ".xliff"])
                wf_term = gr.File(label="Terminologia approvata — opzionale", type="filepath", file_types=[".csv", ".tbx", ".xml"])
                wf_strict = gr.Checkbox(value=False, label="Terminologia strict durante QA")
            with gr.Row():
                wf_reviewer = gr.Textbox(label="Revisore umano")
                wf_human = gr.Checkbox(value=False, label="Confermo approvazione umana — solo per chiusura job")
            wf_apply = gr.Button("Esegui azione")
            wf_jobs = gr.Dataframe(interactive=False, wrap=True, max_height=560, label="Job")
            with gr.Tabs():
                with gr.Tab("Audit eventi"):
                    wf_events = gr.Dataframe(interactive=False, wrap=True, max_height=500)
                with gr.Tab("QA ultimo job"):
                    wf_qa = gr.Dataframe(interactive=False, wrap=True, max_height=600)
            with gr.Row():
                wf_approval = gr.File(label="Approval ZIP")
                wf_tm = gr.File(label="Confirmed TM")
            with gr.Row():
                wf_jobs_csv = gr.File(label="Workflow jobs CSV")
                wf_audit_csv = gr.File(label="Workflow audit CSV")
                wf_summary_json = gr.File(label="Workflow summary JSON")

            wf_export_project.click(fn=workflow_export_project_ui, inputs=[author_project_state], outputs=[wf_project_archive,wf_archive_msg])
            wf_restore_btn.click(
                fn=workflow_restore_project_ui, inputs=[wf_restore_archive],
                outputs=[author_project_state,wf_restore_msg,wf_jobs,wf_vendors,wf_events],
            )
            wf_refresh.click(
                fn=workflow_refresh_ui, inputs=[author_project_state],
                outputs=[wf_dashboard,wf_jobs,wf_vendors,wf_events,wf_jobs_csv,wf_audit_csv,wf_summary_json],
            )
            wf_meta_save.click(fn=workflow_project_metadata_ui, inputs=[author_project_state,wf_title,wf_client,wf_owner], outputs=[wf_dashboard,wf_meta_msg])
            wf_vendor_save.click(fn=workflow_vendor_ui, inputs=[author_project_state,wf_vendor_id,wf_vendor_name,wf_vendor_email,wf_vendor_notes], outputs=[wf_vendors,wf_vendor_msg])
            wf_create.click(
                fn=workflow_create_job_ui, inputs=[author_project_state,wf_package,wf_vendor,wf_due,wf_notes,wf_actor],
                outputs=[wf_dashboard,wf_jobs,wf_events],
            )
            wf_apply.click(
                fn=workflow_job_action_ui,
                inputs=[author_project_state,wf_job_id,wf_action,wf_actor,wf_returned,wf_term,wf_strict,wf_reviewer,wf_human],
                outputs=[wf_dashboard,wf_jobs,wf_events,wf_qa,wf_approval,wf_tm],
            )

    gr.Markdown(
        "**Stato v0.25:** Workflow & Job Management collega pacchetti, fornitori, stati, scadenze, XLIFF restituiti, QA v0.24 e approvazione finale in un audit persistente. Le transizioni sono fail-closed e il core tecnico v0.11→v0.24 resta separato."
    )


if __name__ == "__main__":
    demo.launch(inbrowser=True, server_name="127.0.0.1", show_error=True)

