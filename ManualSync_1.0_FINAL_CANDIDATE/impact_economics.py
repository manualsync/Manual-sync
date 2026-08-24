from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ccms_industrial import IndustrialRepository

IMPACT_BUILD = "0.20.0-change-impact-economics-20260810"


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\wÀ-ÿ'-]+\b", str(text or ""), flags=re.UNICODE))


def _langs(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


def build_change_workload(repo: IndustrialRepository, changed_id: str, *, kind: str) -> pd.DataFrame:
    """Expand CCMS impact to translation/review workload.

    Costs are deliberately not invented here. This function calculates affected
    publications, languages and source words only. A separate explicit rate card
    is required for monetary estimates.
    """
    impact = repo.impact_map_industrial(changed_id, kind=kind)
    if impact.empty:
        return pd.DataFrame(columns=[
            "changed_kind","changed_id","topic_id","publication_id","target_lang",
            "source_words","work_type","translation_status","risk_reason",
        ])

    rows: list[dict[str, Any]] = []
    for _, item in impact.iterrows():
        topic_id = str(item.get("topic_id", ""))
        blocks = repo.blocks_table(topic_id)
        topic_words = int(sum(word_count(x) for x in blocks.get("text_content", pd.Series(dtype=str)).astype(str))) if not blocks.empty else 0
        if topic_words == 0:
            t = repo.conn.execute("SELECT body FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            topic_words = word_count(t["body"] if t else "")

        languages = _langs(item.get("languages", ""))
        # The generic impact map reports topic-level translations. For a changed
        # first-class object, also inspect its own translation table so block/table/
        # figure changes do not lose language impact simply because no legacy
        # topic_translation row exists.
        extra_langs = set()
        if kind == "block":
            for r in repo.conn.execute("SELECT target_lang FROM block_translations WHERE block_id=?", (changed_id,)):
                extra_langs.add(str(r["target_lang"]))
        elif kind == "table":
            for r in repo.conn.execute("SELECT target_lang FROM structured_table_translations WHERE table_id=?", (changed_id,)):
                extra_langs.add(str(r["target_lang"]))
        elif kind == "figure":
            for r in repo.conn.execute("SELECT target_lang FROM figure_translations WHERE figure_id=?", (changed_id,)):
                extra_langs.add(str(r["target_lang"]))
        elif kind == "topic":
            for r in repo.conn.execute("SELECT target_lang FROM topic_translations WHERE topic_id=?", (changed_id,)):
                extra_langs.add(str(r["target_lang"]))
        languages = sorted(set(languages) | {x for x in extra_langs if x})
        if not languages:
            # Impact can still matter before first translation exists.
            languages = [""]
        for lang in languages:
            work_type = "RETRANSLATE/REVIEW"
            translation_status = "BECOMES_STALE_IF_CHANGE_APPROVED" if lang else "NO_TARGET_LANGUAGE_YET"
            rows.append({
                "changed_kind": kind,
                "changed_id": changed_id,
                "topic_id": topic_id,
                "topic_title": item.get("topic_title", ""),
                "publication_id": item.get("publication_id", ""),
                "publication_title": item.get("publication_title", ""),
                "target_lang": lang,
                "source_words": topic_words,
                "work_type": work_type,
                "translation_status": translation_status,
                "risk_reason": "Source change propagates to an already published/translated topic" if lang else "Source change affects publication",
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["changed_kind","changed_id","topic_id","publication_id","target_lang"]).reset_index(drop=True)
    return df


def apply_leverage(workload: pd.DataFrame, leverage: pd.DataFrame | None = None) -> pd.DataFrame:
    """Attach CAT leverage supplied by a real CAT analysis.

    leverage columns: target_lang, reuse_pct, fuzzy_pct, technical_block_pct.
    Percentages are clamped and normalized conservatively; remaining words are NEW.
    """
    df = workload.copy()
    if df.empty:
        return df
    if leverage is None or leverage.empty:
        df["reuse_pct"] = 0.0
        df["fuzzy_pct"] = 0.0
        df["technical_block_pct"] = 0.0
    else:
        lev = leverage.copy().fillna(0)
        lev["target_lang"] = lev["target_lang"].astype(str)
        df = df.merge(lev[[c for c in ["target_lang","reuse_pct","fuzzy_pct","technical_block_pct"] if c in lev.columns]], on="target_lang", how="left")
        for c in ["reuse_pct","fuzzy_pct","technical_block_pct"]:
            if c not in df: df[c] = 0.0
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).clip(0, 100)
    total_pct = df[["reuse_pct","fuzzy_pct","technical_block_pct"]].sum(axis=1)
    scale = pd.Series(1.0, index=df.index)
    over = total_pct > 100
    scale.loc[over] = 100 / total_pct.loc[over].replace(0, 1)
    for c in ["reuse_pct","fuzzy_pct","technical_block_pct"]:
        df[c] = (df[c] * scale).round(2)
    df["new_pct"] = (100 - df[["reuse_pct","fuzzy_pct","technical_block_pct"]].sum(axis=1)).clip(0, 100).round(2)
    for prefix, pctcol in [("reuse","reuse_pct"),("fuzzy","fuzzy_pct"),("technical_block","technical_block_pct"),("new","new_pct")]:
        df[f"{prefix}_words"] = (df["source_words"] * df[pctcol] / 100).round().astype(int)
    return df


def estimate_cost(workload: pd.DataFrame, rate_card: pd.DataFrame | None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Estimate cost only from user-supplied rates.

    Required rate-card columns: target_lang, rate_new, rate_fuzzy,
    rate_reuse, rate_technical_block. No hidden/default commercial prices are used.
    """
    df = workload.copy()
    if df.empty:
        return df, {"estimated_cost":0.0,"cost_available":False,"reason":"no-impact"}
    if rate_card is None or rate_card.empty:
        df["estimated_cost"] = pd.NA
        return df, {"estimated_cost":None,"cost_available":False,"reason":"rate-card-required"}

    card = rate_card.copy().fillna(0)
    required = ["target_lang","rate_new","rate_fuzzy","rate_reuse","rate_technical_block"]
    missing = [c for c in required if c not in card.columns]
    if missing:
        raise ValueError("Rate card incompleta: " + ", ".join(missing))
    card["target_lang"] = card["target_lang"].astype(str)
    for c in required[1:]:
        card[c] = pd.to_numeric(card[c], errors="coerce").fillna(0).clip(lower=0)
    df = df.merge(card[required], on="target_lang", how="left")
    rates_missing = df[required[1:]].isna().any(axis=1)
    for c in required[1:]:
        df[c] = df[c].fillna(0)
    df["estimated_cost"] = (
        df.get("new_words", 0) * df["rate_new"]
        + df.get("fuzzy_words", 0) * df["rate_fuzzy"]
        + df.get("reuse_words", 0) * df["rate_reuse"]
        + df.get("technical_block_words", 0) * df["rate_technical_block"]
    ).round(2)
    df.loc[rates_missing, "estimated_cost"] = pd.NA
    known = pd.to_numeric(df["estimated_cost"], errors="coerce")
    summary = {
        "estimated_cost": round(float(known.sum(skipna=True)), 2),
        "cost_available": bool((~rates_missing).any()),
        "rows_without_rate": int(rates_missing.sum()),
        "affected_publications": int(df["publication_id"].astype(str).nunique()),
        "affected_languages": int(df.loc[df["target_lang"].astype(str).ne(""), "target_lang"].nunique()),
        "affected_source_words": int(df["source_words"].sum()),
    }
    return df, summary


def impact_summary(workload: pd.DataFrame) -> dict[str, Any]:
    if workload.empty:
        return {"affected_topics":0,"affected_publications":0,"affected_languages":0,"source_words":0}
    return {
        "affected_topics": int(workload["topic_id"].astype(str).nunique()),
        "affected_publications": int(workload["publication_id"].astype(str).nunique()),
        "affected_languages": int(workload.loc[workload["target_lang"].astype(str).ne(""), "target_lang"].nunique()),
        "source_words": int(workload["source_words"].sum()),
    }
