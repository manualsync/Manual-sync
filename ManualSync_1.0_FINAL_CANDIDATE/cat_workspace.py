from __future__ import annotations

import json
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd
from rapidfuzz.fuzz import ratio

from cat_tm import (
    CAT_BUILD,
    TranslationMemory,
    cat_normalize,
    export_xliff,
    inline_tokens,
    load_terminology,
    source_target_safe,
    terminology_suggestions,
)

WORKSPACE_BUILD = "0.15.0-cat-production-workspace-20260810"
STATUSES = {"Draft", "Reviewed", "Confirmed"}

DEFAULT_QA_CONFIG = {
    "terminology": True,
    "double_spaces": True,
    "brackets": True,
    "terminal_punctuation": False,
    "capitalization": False,
    "block_on_warnings": False,
    # Technical integrity is deliberately not user-disableable.
    "technical_integrity": True,
}


def normalize_qa_config(config: dict | None) -> dict:
    out = dict(DEFAULT_QA_CONFIG)
    if config:
        for key in out:
            if key in config:
                out[key] = bool(config[key])
    out["technical_integrity"] = True
    return out


def create_editor_dataframe(match_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in match_df.iterrows():
        target = str(row.get("pretranslation") or "")
        status = "Confirmed" if row.get("decision") == "RIUSO AUTOMATICO CONSENTITO" and target else "Draft"
        rows.append({
            "segment_id": row.get("segment_id"),
            "page": row.get("page"),
            "source": row.get("source", ""),
            "target": target,
            "status": status,
            "match_category": row.get("match_category", ""),
            "score": row.get("linguistic_score", 0),
            "raw_score": row.get("raw_linguistic_score", row.get("linguistic_score", 0)),
            "suggestion": row.get("suggested_translation", ""),
            "tm_name": row.get("tm_name", ""),
            "tm_penalty": row.get("tm_penalty", 0),
            "term_suggestions": row.get("term_suggestions", ""),
            "inline_tokens": row.get("inline_tokens", ""),
            "technical_safe": row.get("technical_safe", True),
            "technical_reason": row.get("technical_reason", ""),
            "terminology_qa": row.get("terminology_qa", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        from cat_batch import add_batch_metadata
        df = add_batch_metadata(df)
    return df


def _terminal_punctuation(text: str) -> str:
    m = re.search(r"([.!?:;])\s*$", (text or "").strip())
    return m.group(1) if m else ""


def _basic_qa(source: str, target: str, terminology: dict[str, set[str]], qa_config: dict | None = None) -> list[dict]:
    """Return structured QA issues.

    Critical technical integrity (numbers, model codes and inline placeholders) is
    intentionally non-disableable. Other linguistic/style checks are configurable.
    """
    cfg = normalize_qa_config(qa_config)
    issues: list[dict] = []
    if not target.strip():
        issues.append({"severity": "critical", "message": "traduzione vuota"})
        return issues

    safe, reason = source_target_safe(source, target)
    if not safe:
        issues.append({"severity": "critical", "message": reason})

    source_cf = source.casefold()
    target_cf = target.casefold()
    if cfg["terminology"]:
        for src_term, target_terms in terminology.items():
            if src_term in source_cf and not any(term in target_cf for term in target_terms):
                issues.append({
                    "severity": "warning",
                    "message": f"terminologia: {src_term} → {', '.join(sorted(target_terms))}",
                })
    if cfg["double_spaces"] and re.search(r"\s{2,}", target):
        issues.append({"severity": "warning", "message": "spazi doppi"})
    if cfg["brackets"]:
        if target.count("(") != target.count(")"):
            issues.append({"severity": "warning", "message": "parentesi non bilanciate"})
        if target.count("[") != target.count("]"):
            issues.append({"severity": "warning", "message": "parentesi quadre non bilanciate"})
        if target.count("{") != target.count("}"):
            issues.append({"severity": "warning", "message": "parentesi graffe non bilanciate"})
    if cfg["terminal_punctuation"]:
        sp = _terminal_punctuation(source)
        tp = _terminal_punctuation(target)
        if sp != tp:
            issues.append({"severity": "warning", "message": f"punteggiatura finale diversa: {sp or '∅'} → {tp or '∅'}"})
    if cfg["capitalization"]:
        s = source.strip()
        t = target.strip()
        if s and t and s[0].isalpha() and t[0].isalpha() and s[0].isupper() != t[0].isupper():
            issues.append({"severity": "warning", "message": "maiuscola/minuscola iniziale non coerente"})
    return issues


def qa_preview(source: str, target: str, terminology: dict[str, set[str]] | None = None,
               qa_config: dict | None = None) -> dict:
    issues = _basic_qa(source, target, terminology or {}, qa_config)
    critical = [x["message"] for x in issues if x["severity"] == "critical"]
    warnings = [x["message"] for x in issues if x["severity"] != "critical"]
    return {
        "status": "BLOCK" if critical else ("ISSUE" if warnings else "OK"),
        "critical": critical,
        "warnings": warnings,
        "inline_tokens_source": list(inline_tokens(source)),
        "inline_tokens_target": list(inline_tokens(target)),
    }


def save_workspace(editor_df: pd.DataFrame, project_dir: str | Path, source_lang: str, target_lang: str,
                   terminology_path: str | Path | None = None, qa_config: dict | None = None) -> dict[str, Any]:
    project_dir = Path(project_dir)
    tm_path = project_dir / "ManualSync_TM.sqlite"
    base_matches = pd.read_csv(project_dir / "CAT_MATCHES.csv").fillna("")
    terminology = load_terminology(terminology_path, source_lang, target_lang) if terminology_path else {}
    cfg = normalize_qa_config(qa_config)

    edited = editor_df.copy().fillna("")
    edited["segment_id"] = edited["segment_id"].astype(str)
    base_matches["segment_id"] = base_matches["segment_id"].astype(str)
    merged = base_matches.merge(
        edited[["segment_id", "target", "status"]], on="segment_id", how="left", suffixes=("", "_edit")
    )

    tm = TranslationMemory(tm_path)
    qa_rows = []
    confirmed_written = 0
    blocked_confirmations = 0
    try:
        for _, row in merged.iterrows():
            source = str(row.get("source", ""))
            target = str(row.get("target", ""))
            status = str(row.get("status", "Draft"))
            if status not in STATUSES:
                status = "Draft"
            issues = _basic_qa(source, target, terminology, cfg)
            critical = [x["message"] for x in issues if x["severity"] == "critical"]
            warnings = [x["message"] for x in issues if x["severity"] != "critical"]
            block = bool(critical) or (cfg["block_on_warnings"] and bool(warnings))
            qa_status = "BLOCK" if block and status == "Confirmed" else ("ISSUE" if issues else "OK")
            written = False
            if status == "Confirmed":
                if block:
                    blocked_confirmations += 1
                else:
                    tm.add(
                        source_lang, target_lang, source, target,
                        prev_norm=str(row.get("prev_context", "")),
                        next_norm=str(row.get("next_context", "")),
                        origin="ManualSync CAT Pro workspace",
                        alignment_confidence=100.0,
                        approved=True,
                        tm_name="Project Confirmed", priority=110, penalty=0,
                    )
                    confirmed_written += 1
                    written = True
            qa_rows.append({
                "segment_id": row.get("segment_id"),
                "source": source,
                "target": target,
                "status": status,
                "qa_status": qa_status,
                "critical_issues": "; ".join(critical),
                "warnings": "; ".join(warnings),
                "qa_issues": "; ".join([x["message"] for x in issues]),
                "term_suggestions": "; ".join(terminology_suggestions(source, terminology)),
                "source_inline_tokens": " | ".join(inline_tokens(source)),
                "target_inline_tokens": " | ".join(inline_tokens(target)),
                "written_to_tm": written,
            })
    finally:
        tm.close()

    qa_df = pd.DataFrame(qa_rows)
    qa_path = project_dir / "WORKSPACE_QA.csv"
    qa_df.to_csv(qa_path, index=False, encoding="utf-8-sig")

    editor_path = project_dir / "BILINGUAL_EDITOR.csv"
    edited.to_csv(editor_path, index=False, encoding="utf-8-sig")
    project_json = project_dir / "ManualSync_PROJECT.json"
    project_json.write_text(json.dumps({
        "workspace_build": WORKSPACE_BUILD,
        "cat_build": CAT_BUILD,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "qa_config": cfg,
        "segments": edited.to_dict(orient="records"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    qa_config_path = project_dir / "ManualSync_QA_PROFILE.json"
    qa_config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    export_df = base_matches.copy()
    targets = dict(zip(edited["segment_id"], edited["target"]))
    statuses = dict(zip(edited["segment_id"], edited["status"]))
    export_df["pretranslation"] = [targets.get(str(x), "") for x in export_df["segment_id"]]
    export_df["workspace_status"] = [statuses.get(str(x), "Draft") for x in export_df["segment_id"]]
    reviewed_xlf = export_xliff(export_df, project_dir / "ManualSync_REVIEWED.xlf", source_lang, target_lang)

    tm = TranslationMemory(tm_path)
    try:
        tmx = tm.export_tmx(project_dir / "ManualSync_TM_UPDATED.tmx", source_lang, target_lang)
        tm_entries = tm.count()
        tm_profiles = tm.profiles()
    finally:
        tm.close()

    package = project_dir / "ManualSync_PROJECT_PACKAGE.zip"
    package.unlink(missing_ok=True)
    include = [
        project_json, qa_config_path, editor_path, qa_path, reviewed_xlf, tmx, tm_path,
        project_dir / "CAT_MATCHES.csv", project_dir / "CAT_SUMMARY.json",
    ]
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in include:
            if p.exists():
                zf.write(p, arcname=p.name)

    summary = {
        "segments": len(edited),
        "confirmed_requested": int((edited["status"] == "Confirmed").sum()),
        "confirmed_written_to_tm": confirmed_written,
        "blocked_confirmations": blocked_confirmations,
        "qa_issues": int(qa_df["qa_status"].isin(["ISSUE", "BLOCK"]).sum()),
        "critical_segments": int(qa_df["critical_issues"].astype(str).ne("").sum()),
        "warning_segments": int(qa_df["warnings"].astype(str).ne("").sum()),
        "tm_entries_after_save": tm_entries,
        "tm_profiles": tm_profiles,
    }
    (project_dir / "WORKSPACE_SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "qa": qa_df,
        "summary": summary,
        "editor_csv": editor_path,
        "qa_csv": qa_path,
        "qa_profile": qa_config_path,
        "project_json": project_json,
        "reviewed_xliff": reviewed_xlf,
        "updated_tmx": tmx,
        "package": package,
    }


def focus_segment(editor_df: pd.DataFrame, index: int) -> dict:
    if editor_df is None or len(editor_df) == 0:
        return {"index": 0, "total": 0, "segment_id": "", "source": "", "target": "", "status": "Draft",
                "match_category": "", "score": 0, "suggestion": "", "term_suggestions": "", "tm_name": "", "tm_penalty": 0}
    idx = max(0, min(int(index or 0), len(editor_df) - 1))
    row = editor_df.iloc[idx]
    return {
        "index": idx,
        "total": len(editor_df),
        "segment_id": str(row.get("segment_id", "")),
        "source": str(row.get("source", "")),
        "target": str(row.get("target", "")),
        "status": str(row.get("status", "Draft")) if str(row.get("status", "Draft")) in STATUSES else "Draft",
        "match_category": str(row.get("match_category", "")),
        "score": row.get("score", 0),
        "suggestion": str(row.get("suggestion", "")),
        "term_suggestions": str(row.get("term_suggestions", "")),
        "tm_name": str(row.get("tm_name", "")),
        "tm_penalty": row.get("tm_penalty", 0),
        "inline_tokens": str(row.get("inline_tokens", "")),
        "technical_reason": str(row.get("technical_reason", "")),
    }


def update_focus_segment(editor_df: pd.DataFrame, index: int, target: str, status: str) -> pd.DataFrame:
    if editor_df is None or len(editor_df) == 0:
        return editor_df
    out = editor_df.copy()
    idx = max(0, min(int(index or 0), len(out) - 1))
    row_index = out.index[idx]
    # Locked automatic-reuse segments are protected from accidental overwrite.
    # A future UI unlock action may explicitly clear this flag first.
    if "locked" in out.columns and bool(out.at[row_index, "locked"]):
        return out
    out.at[row_index, "target"] = target or ""
    out.at[row_index, "status"] = status if status in STATUSES else "Draft"
    return out

def concordance_search(tm_path: str | Path, query: str, source_lang: str, target_lang: str, limit: int = 20) -> pd.DataFrame:
    query = (query or "").strip()
    if not query:
        return pd.DataFrame(columns=["score", "source", "target", "tm_name", "penalty", "origin", "approved"])
    tm = TranslationMemory(tm_path)
    try:
        entries = tm.entries(source_lang, target_lang, approved_only=False)
    finally:
        tm.close()
    qn = cat_normalize(query)
    rows = []
    for row in entries:
        source = str(row["source"])
        target = str(row["target"])
        sn = row["source_norm"]
        if qn in sn or query.casefold() in target.casefold():
            score = 100.0 if qn == sn else max(92.0, float(ratio(qn, sn)))
        else:
            score = float(ratio(qn, sn))
        if score < 45:
            continue
        penalty = float(row["penalty"]) if "penalty" in row.keys() else 0.0
        effective = max(0.0, score - penalty)
        rows.append({
            "score": round(effective, 1),
            "raw_score": round(score, 1),
            "source": source,
            "target": target,
            "tm_name": row["tm_name"] if "tm_name" in row.keys() else "Project TM",
            "penalty": penalty,
            "priority": int(row["priority"]) if "priority" in row.keys() else 100,
            "origin": row["origin"],
            "approved": bool(row["approved"]),
            "alignment_confidence": round(float(row["alignment_confidence"]), 1),
        })
    rows.sort(key=lambda x: (x["score"], x["approved"], x.get("priority", 0), x["alignment_confidence"]), reverse=True)
    return pd.DataFrame(rows[:limit])


def import_workspace_xliff(xliff_path: str | Path) -> pd.DataFrame:
    root = ET.parse(xliff_path).getroot()
    ns = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    def tag(name: str) -> str:
        return f"{{{ns}}}{name}" if ns else name
    rows = []
    for unit in root.iter(tag("unit")):
        unit_id = unit.attrib.get("id", "")
        segment = unit.find(f".//{tag('segment')}")
        if segment is None:
            continue
        source = segment.find(tag("source"))
        target = segment.find(tag("target"))
        target_text = "" if target is None else "".join(target.itertext()).strip()
        rows.append({
            "segment_id": unit_id,
            "page": "",
            "source": "" if source is None else "".join(source.itertext()).strip(),
            "target": target_text,
            "status": "Reviewed" if target_text else "Draft",
            "match_category": "IMPORTED XLIFF",
            "score": 0,
            "suggestion": "",
            "technical_safe": True,
            "technical_reason": "",
            "terminology_qa": "",
            "term_suggestions": "",
            "inline_tokens": " | ".join(inline_tokens("" if source is None else "".join(source.itertext()).strip())),
            "tm_name": "",
            "tm_penalty": 0,
            "raw_score": 0,
        })
    return pd.DataFrame(rows)
