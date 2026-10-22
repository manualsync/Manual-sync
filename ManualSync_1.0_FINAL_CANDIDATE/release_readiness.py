from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from ccms_industrial import IndustrialRepository
from compliance_intelligence import ComplianceService
from workflow_management import STATUS_LABELS, WorkflowManager
from product_version import PRODUCT_BUILD
from project_layout import ProjectLayout
from language_codes import canonical_lang, canonical_langs
from linguistic_safety import collect_cross_language_issues, cross_language_issue

READINESS_BUILD = "0.27.0-compliance-readiness-gate-3-5c-20260901"
ACCEPTED_TRANSLATION_STATUS = {"Reviewed", "Approved", "Published", "Confirmed", "NOT_REQUIRED"}
CLOSED_JOB_STATUS = {"APPROVED", "CANCELLED"}


def _ccms_db(project_dir: str | Path) -> Path:
    p = ProjectLayout(project_dir).find_ccms_db()
    if p is None:
        raise FileNotFoundError("Repository CCMS non trovato nel progetto")
    return p


def _langs(values: Iterable[str] | str | None) -> list[str]:
    return canonical_langs(values)


def _linguistic_readiness_issues(repo: IndustrialRepository, publication_id: str, source_lang: str, target_lang: str, *, profile_id: str | None = None, dimensions: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Return visible cross-language copy/mismatch issues before release creation."""
    source_lang = canonical_lang(source_lang)
    target_lang = canonical_lang(target_lang)
    if not source_lang or not target_lang or source_lang == target_lang:
        return []
    source_frame, _ = repo.assemble_structured_publication(publication_id, profile_id=profile_id, dimensions=dimensions, target_lang=None)
    target_frame, _ = repo.assemble_structured_publication(publication_id, profile_id=profile_id, dimensions=dimensions, target_lang=target_lang)
    issues: list[dict[str, str]] = []
    src_by_block = {str(r.get('block_id','')): r for _, r in source_frame.iterrows()}
    if not source_frame.empty and not target_frame.empty:
        src_titles = source_frame[['topic_id','topic_title']].drop_duplicates(subset=['topic_id'])
        tgt_titles = target_frame[['topic_id','topic_title']].drop_duplicates(subset=['topic_id'])
        tgt_title_map = {str(r['topic_id']): str(r['topic_title'] or '') for _, r in tgt_titles.iterrows()}
        for _, r in src_titles.iterrows():
            tid=str(r['topic_id']); src=str(r['topic_title'] or ''); tgt=tgt_title_map.get(tid,'')
            reason=cross_language_issue(src,tgt,source_lang,target_lang)
            if reason:
                issues.append({'topic_id':tid,'block_id':f'{tid}::title','block_type':'topic_title','object_ref_id':tid,'content_preview':src[:240],'reason':reason})
        for _, tr in target_frame.iterrows():
            bid=str(tr.get('block_id','')); sr=src_by_block.get(bid)
            if sr is None: continue
            pairs=collect_cross_language_issues(sr.get('content'), tr.get('content'), source_lang, target_lang, limit=4)
            for reason in pairs:
                issues.append({'topic_id':str(tr.get('topic_id','') or ''),'block_id':bid,'block_type':str(tr.get('block_type','') or ''),'object_ref_id':str(tr.get('object_ref_id','') or ''),'content_preview':str(sr.get('content',''))[:240],'reason':reason})
                if len(issues)>=50: return issues
    return issues


def assess_release_readiness(project_dir: str | Path, publication_id: str, *, target_langs: Iterable[str] | str | None = None,
                             profile_id: str | None = None, dimensions: dict[str, Any] | None = None,
                             require_grounded: bool = True, require_translation_reviewed: bool = True,
                             require_linguistic_plausibility: bool = False) -> dict[str, Any]:
    project_dir = Path(project_dir)
    repo = IndustrialRepository(_ccms_db(project_dir))
    wf = WorkflowManager(project_dir)
    try:
        pub = repo.conn.execute("SELECT * FROM publications WHERE publication_id=?", (publication_id,)).fetchone()
        if not pub:
            raise KeyError(publication_id)
        source_lang = canonical_lang(str(pub["source_lang"]))
        langs = _langs(target_langs)
        if not langs:
            langs = canonical_langs([r[0] for r in repo.conn.execute("SELECT DISTINCT target_lang FROM block_translations UNION SELECT DISTINCT target_lang FROM topic_translations UNION SELECT DISTINCT target_lang FROM component_translations UNION SELECT DISTINCT target_lang FROM structured_table_translations UNION SELECT DISTINCT target_lang FROM figure_translations") if r[0]])

        source_check = repo.validate_publication_for_release(publication_id, profile_id=profile_id, dimensions=dimensions, target_lang=None, strict_approval=True)
        source_frame = source_check["frame"]
        object_ids = set(source_frame.get("block_id", pd.Series(dtype=str)).astype(str).tolist())
        topic_ids = set(source_frame.get("topic_id", pd.Series(dtype=str)).astype(str).tolist())
        object_ids |= topic_ids
        for _, r in source_frame.iterrows():
            oid = str(r.get("object_ref_id", "") or "")
            if oid:
                object_ids.add(oid)

        open_reviews = repo.review_queue(status="OPEN")
        if not open_reviews.empty:
            open_reviews = open_reviews[open_reviews["object_id"].astype(str).isin(object_ids)]
        grounded_issues = 0
        if require_grounded and not source_frame.empty:
            grounded_issues = int((source_frame["evidence_status"].astype(str) != "SOURCE_GROUNDED").sum())

        jobs = wf.jobs_table()
        rows: list[dict[str, Any]] = []
        blocking_items: list[dict[str, Any]] = []
        for lang in [source_lang] + [x for x in langs if x != source_lang]:
            issues: list[str] = []
            missing = stale = unreviewed = 0
            if lang == source_lang:
                if not source_check["PASS"]:
                    issues.extend(source_check["issues"])
            else:
                chk = repo.validate_publication_for_release(publication_id, profile_id=profile_id, dimensions=dimensions, target_lang=lang, strict_approval=True)
                f = chk["frame"]
                missing = int(chk["summary"].get("missing_translations", 0))
                stale = int(chk["summary"].get("stale_translations", 0))
                issues.extend(chk["issues"])
                if require_translation_reviewed and not f.empty:
                    s = f["translation_status"].astype(str)
                    bad_mask = ~s.isin(ACCEPTED_TRANSLATION_STATUS | {"SOURCE"})
                    unreviewed_blocks = int(bad_mask.sum())
                    for _, br in f.loc[bad_mask].iterrows():
                        raw_content = br.get("content", "")
                        if isinstance(raw_content, (dict, list)):
                            preview = json.dumps(raw_content, ensure_ascii=False)
                        else:
                            preview = str(raw_content or "")
                        blocking_items.append({
                            "language": lang,
                            "topic_id": str(br.get("topic_id", "") or ""),
                            "block_id": str(br.get("block_id", "") or ""),
                            "block_type": str(br.get("block_type", "") or ""),
                            "object_ref_id": str(br.get("object_ref_id", "") or ""),
                            "translation_status": str(br.get("translation_status", "") or ""),
                            "content_preview": preview[:240],
                        })

                    unreviewed_titles = 0
                    if "topic_title_translation_status" in f.columns:
                        title_rows = f[["topic_id", "topic_title", "topic_title_translation_status"]].drop_duplicates(subset=["topic_id"])
                        title_bad = ~title_rows["topic_title_translation_status"].astype(str).isin(ACCEPTED_TRANSLATION_STATUS | {"SOURCE"})
                        unreviewed_titles = int(title_bad.sum())
                        for _, tr in title_rows.loc[title_bad].iterrows():
                            tid = str(tr.get("topic_id", "") or "")
                            blocking_items.append({
                                "language": lang,
                                "topic_id": tid,
                                "block_id": f"{tid}::title",
                                "block_type": "topic_title",
                                "object_ref_id": tid,
                                "translation_status": str(tr.get("topic_title_translation_status", "") or ""),
                                "content_preview": str(tr.get("topic_title", "") or "")[:240],
                            })
                    unreviewed = unreviewed_blocks + unreviewed_titles
                    if unreviewed:
                        issues.append(f"{unreviewed} traduzioni non revisionate")

                linguistic = _linguistic_readiness_issues(repo, publication_id, source_lang, lang, profile_id=profile_id, dimensions=dimensions) if require_linguistic_plausibility else []
                if linguistic:
                    issues.append(f"{len(linguistic)} anomalie linguistiche cross-language")
                    for li in linguistic:
                        blocking_items.append({
                            "language": lang,
                            "topic_id": li.get("topic_id", ""),
                            "block_id": li.get("block_id", ""),
                            "block_type": li.get("block_type", ""),
                            "object_ref_id": li.get("object_ref_id", ""),
                            "translation_status": "LINGUISTIC_QA_FAIL",
                            "content_preview": (li.get("reason", "") + ": " + li.get("content_preview", ""))[:240],
                        })

            lang_jobs = jobs[jobs["target_lang"].astype(str).map(canonical_lang) == lang] if not jobs.empty else pd.DataFrame()
            open_jobs = 0
            qa_blocked = 0
            overdue = 0
            if not lang_jobs.empty:
                open_jobs = int((~lang_jobs["status_label"].astype(str).isin(["Approvato", "Annullato"])).sum())
                # A CANCELLED job keeps the qa_status its last QA run left
                # behind -- typically BLOCKED, because cancelling is the only
                # way out of a job whose source changed after export. Counting
                # it here made the language unreleasable for good, with no
                # action anywhere able to clear it. Only jobs still in play
                # block; a live QA-blocked job still blocks exactly as before.
                live_jobs = lang_jobs[lang_jobs["status_label"].astype(str) != STATUS_LABELS["CANCELLED"]]
                qa_blocked = int((live_jobs["qa_status"].astype(str) == "BLOCKED").sum())
                overdue = int((lang_jobs["overdue"].astype(str) == "SI").sum())
            if open_jobs:
                issues.append(f"{open_jobs} job di traduzione ancora aperti")
            if qa_blocked:
                issues.append(f"{qa_blocked} job con QA bloccato")
            if overdue:
                issues.append(f"{overdue} job scaduti")
            if len(open_reviews):
                issues.append(f"{len(open_reviews)} review CCMS aperte")
            if grounded_issues:
                issues.append(f"{grounded_issues} blocchi senza grounding approvato")

            rows.append({
                "language": lang,
                "role": "SOURCE" if lang == source_lang else "TARGET",
                "status": "READY" if not issues else "BLOCKED",
                "missing_translations": missing,
                "stale_translations": stale,
                "unreviewed_translations": unreviewed,
                "open_jobs": open_jobs,
                "qa_blocked_jobs": qa_blocked,
                "overdue_jobs": overdue,
                "open_ccms_reviews": int(len(open_reviews)),
                "grounding_issues": grounded_issues,
                "issues": " | ".join(dict.fromkeys(issues)),
            })

        matrix = pd.DataFrame(rows)
        language_ready = bool(len(matrix) and (matrix["status"] == "READY").all())
        # Compliance is read once here, from the same canonical backend
        # function the Release UI and the immutable release snapshot both
        # use (ComplianceService.compliance_readiness_for_publication) --
        # never re-derived or duplicated. No ACTIVE scope for this
        # publication -> compliance_ready=True vacuously, so existing
        # readiness behavior for publications without Compliance is
        # unchanged. One or more ACTIVE scopes -> Compliance joins the gate
        # automatically; there is no flag here to opt out of it.
        compliance = ComplianceService(repo).compliance_readiness_for_publication(publication_id)
        ready = language_ready and compliance["compliance_ready"]
        target_matrix = matrix[matrix["role"] == "TARGET"] if not matrix.empty else pd.DataFrame()
        source_rows = matrix[matrix["role"] == "SOURCE"] if not matrix.empty else pd.DataFrame()
        blockers = pd.DataFrame(blocking_items, columns=["language","topic_id","block_id","block_type","object_ref_id","translation_status","content_preview"])
        return {
            "build": READINESS_BUILD,
            "product_build": PRODUCT_BUILD,
            "publication_id": publication_id,
            "source_lang": source_lang,
            "target_langs": [x for x in langs if x != source_lang],
            "release_ready": ready,
            "matrix": matrix,
            "blocking_items": blockers,
            "open_reviews": open_reviews,
            "compliance": compliance,
            "summary": {
                "languages_total": int(len(matrix)),
                "languages_ready": int((matrix["status"] == "READY").sum()),
                "languages_blocked": int((matrix["status"] == "BLOCKED").sum()),
                "target_languages_total": int(len(target_matrix)),
                "target_languages_ready": int((target_matrix["status"] == "READY").sum()) if not target_matrix.empty else 0,
                "target_languages_blocked": int((target_matrix["status"] == "BLOCKED").sum()) if not target_matrix.empty else 0,
                "source_ready": bool(not source_rows.empty and (source_rows["status"] == "READY").all()),
                "language_ready": language_ready,
                "compliance_applicable": compliance["compliance_applicable"],
                "compliance_ready": compliance["compliance_ready"],
                "release_ready": ready,
            },
        }
    finally:
        wf.close(); repo.close()


def export_readiness_report(result: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    csv = out / "RELEASE_READINESS_MATRIX.csv"
    js = out / "RELEASE_READINESS.json"
    result["matrix"].to_csv(csv, index=False, encoding="utf-8-sig")
    blockers_csv = out / "RELEASE_BLOCKING_ITEMS.csv"
    blockers = result.get("blocking_items", pd.DataFrame())
    if hasattr(blockers, "to_csv"):
        blockers.to_csv(blockers_csv, index=False, encoding="utf-8-sig")
    payload = {k:v for k,v in result.items() if k not in {"matrix","open_reviews","blocking_items"}}
    payload["matrix"] = result["matrix"].to_dict(orient="records")
    payload["blocking_items"] = blockers.to_dict(orient="records") if hasattr(blockers, "to_dict") else []
    payload["open_reviews"] = result["open_reviews"].to_dict(orient="records") if hasattr(result["open_reviews"], "to_dict") else []
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"csv": csv, "json": js, "blocking_csv": blockers_csv}
