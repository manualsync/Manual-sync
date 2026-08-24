from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from cat_batch import BATCH_BUILD, match_band, word_count, word_count_by_band
from cat_tm import source_target_safe

ASSET_BUILD = "0.16.0-language-asset-cost-intelligence-20260810"

DEFAULT_RATES = {
    "NEW": 0.14,
    "<75%": 0.14,
    "75-84%": 0.105,
    "85-94%": 0.084,
    "95-99%": 0.056,
    "100%": 0.028,
    "CONTEXT": 0.014,
    "TECHNICAL BLOCK": 0.14,
}


def audit_tm(tm_path: str | Path) -> tuple[pd.DataFrame, dict]:
    tm_path = Path(tm_path)
    conn = sqlite3.connect(tm_path)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM tm_entries ORDER BY source_lang,target_lang,source_norm,priority DESC,penalty ASC,id"))
    conn.close()

    if not rows:
        empty = pd.DataFrame(columns=["issue_type","severity","source_lang","target_lang","source","target","tm_name","details"])
        return empty, {
            "total_entries": 0, "unique_sources": 0, "conflicting_sources": 0,
            "unsafe_entries": 0, "unapproved_entries": 0, "low_confidence_entries": 0,
            "duplicate_cross_profile_entries": 0, "health_score": 100.0,
        }

    groups: dict[tuple[str,str,str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[(r["source_lang"], r["target_lang"], r["source_norm"])].append(r)

    issues: list[dict[str, Any]] = []
    conflicting_sources = 0
    duplicate_cross_profile = 0
    unsafe_entries = 0
    unapproved_entries = 0
    low_confidence_entries = 0

    for key, group in groups.items():
        targets = defaultdict(list)
        for r in group:
            targets[str(r["target"]).strip()].append(r)
        if len(targets) > 1:
            conflicting_sources += 1
            detail = " | ".join(f"{t} [{len(rs)}]" for t, rs in targets.items())
            exemplar = group[0]
            issues.append({
                "issue_type": "CONFLICTING_TRANSLATIONS",
                "severity": "HIGH",
                "source_lang": exemplar["source_lang"], "target_lang": exemplar["target_lang"],
                "source": exemplar["source"], "target": "",
                "tm_name": " | ".join(sorted(set(str(x["tm_name"]) for x in group))),
                "details": detail,
            })
        for target, same_target_rows in targets.items():
            profiles = set(str(x["tm_name"]) for x in same_target_rows)
            if len(same_target_rows) > 1 and len(profiles) > 1:
                duplicate_cross_profile += len(same_target_rows) - 1
                exemplar = same_target_rows[0]
                issues.append({
                    "issue_type": "DUPLICATE_CROSS_PROFILE",
                    "severity": "LOW",
                    "source_lang": exemplar["source_lang"], "target_lang": exemplar["target_lang"],
                    "source": exemplar["source"], "target": target,
                    "tm_name": " | ".join(sorted(profiles)),
                    "details": f"stessa coppia presente in {len(profiles)} profili",
                })

    for r in rows:
        safe, reason = source_target_safe(str(r["source"]), str(r["target"]))
        if not safe:
            unsafe_entries += 1
            issues.append({
                "issue_type": "TECHNICAL_UNSAFE",
                "severity": "CRITICAL",
                "source_lang": r["source_lang"], "target_lang": r["target_lang"],
                "source": r["source"], "target": r["target"], "tm_name": r["tm_name"],
                "details": reason,
            })
        if not int(r["approved"] or 0):
            unapproved_entries += 1
            issues.append({
                "issue_type": "UNAPPROVED",
                "severity": "MEDIUM",
                "source_lang": r["source_lang"], "target_lang": r["target_lang"],
                "source": r["source"], "target": r["target"], "tm_name": r["tm_name"],
                "details": "entry non approvata per riuso automatico",
            })
        conf = float(r["alignment_confidence"] or 0)
        if conf < 80:
            low_confidence_entries += 1
            issues.append({
                "issue_type": "LOW_ALIGNMENT_CONFIDENCE",
                "severity": "MEDIUM",
                "source_lang": r["source_lang"], "target_lang": r["target_lang"],
                "source": r["source"], "target": r["target"], "tm_name": r["tm_name"],
                "details": f"alignment confidence {conf:.1f}%",
            })

    issue_df = pd.DataFrame(issues)
    if not issue_df.empty:
        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        issue_df["_s"] = issue_df["severity"].map(severity_order).fillna(9)
        issue_df = issue_df.sort_values(["_s","issue_type","source"]).drop(columns="_s").reset_index(drop=True)

    n = max(1, len(rows))
    # Transparent, deliberately conservative score. Conflicts are source-level,
    # unsafe entries are entry-level and weighted most heavily.
    penalty = (
        unsafe_entries * 8
        + conflicting_sources * 5
        + unapproved_entries * 2
        + low_confidence_entries * 1.5
        + duplicate_cross_profile * 0.25
    ) / n * 100
    health_score = round(max(0.0, 100.0 - penalty), 1)
    summary = {
        "total_entries": len(rows),
        "unique_sources": len(groups),
        "conflicting_sources": conflicting_sources,
        "unsafe_entries": unsafe_entries,
        "unapproved_entries": unapproved_entries,
        "low_confidence_entries": low_confidence_entries,
        "duplicate_cross_profile_entries": duplicate_cross_profile,
        "health_score": health_score,
    }
    return issue_df, summary


def cost_analysis(match_df: pd.DataFrame, rates: dict[str, float] | None = None) -> tuple[pd.DataFrame, dict]:
    rates_use = dict(DEFAULT_RATES)
    if rates:
        for k, v in rates.items():
            if k in rates_use:
                rates_use[k] = max(0.0, float(v))

    df = match_df.copy().fillna("")
    if "word_count" not in df:
        df["word_count"] = df["source"].astype(str).map(word_count)
    if "match_band" not in df:
        df["match_band"] = df.apply(match_band, axis=1)

    rows = []
    total_words = int(df["word_count"].astype(int).sum()) if not df.empty else 0
    baseline_rate = rates_use["NEW"]
    baseline_cost = total_words * baseline_rate
    projected_cost = 0.0
    for band, group in df.groupby("match_band"):
        words = int(group["word_count"].astype(int).sum())
        rate = float(rates_use.get(str(band), baseline_rate))
        cost = words * rate
        projected_cost += cost
        rows.append({
            "match_band": band,
            "segments": int(len(group)),
            "words": words,
            "rate_per_word": round(rate, 4),
            "projected_cost": round(cost, 2),
            "all_new_cost_same_words": round(words * baseline_rate, 2),
            "saving": round(words * baseline_rate - cost, 2),
        })
    breakdown = pd.DataFrame(rows)
    if not breakdown.empty:
        order = ["CONTEXT","100%","95-99%","85-94%","75-84%","<75%","TECHNICAL BLOCK","NEW"]
        breakdown["_o"] = breakdown["match_band"].map({v:i for i,v in enumerate(order)}).fillna(999)
        breakdown = breakdown.sort_values(["_o","match_band"]).drop(columns="_o").reset_index(drop=True)
    savings = baseline_cost - projected_cost
    summary = {
        "source_words": total_words,
        "baseline_all_new_cost": round(baseline_cost, 2),
        "projected_cost": round(projected_cost, 2),
        "estimated_saving": round(savings, 2),
        "estimated_saving_pct": round((savings / baseline_cost * 100), 1) if baseline_cost else 0.0,
        "rates": rates_use,
    }
    return breakdown, summary


def leverage_analysis(match_df: pd.DataFrame) -> dict:
    df = match_df.copy().fillna("")
    if "word_count" not in df:
        df["word_count"] = df["source"].astype(str).map(word_count)
    if "match_band" not in df:
        df["match_band"] = df.apply(match_band, axis=1)
    total_words = int(df["word_count"].sum()) if not df.empty else 0
    auto_mask = df.get("decision", pd.Series("", index=df.index)).astype(str).eq("RIUSO AUTOMATICO CONSENTITO")
    auto_words = int(df.loc[auto_mask, "word_count"].sum()) if not df.empty else 0
    repetition_mask = df.get("is_repetition", pd.Series(False, index=df.index)).astype(bool)
    repetition_words = int(df.loc[repetition_mask, "word_count"].sum()) if not df.empty else 0
    unique_repeat_groups = int(df.loc[repetition_mask, "repetition_group"].astype(str).nunique()) if "repetition_group" in df else 0
    return {
        "total_words": total_words,
        "automatic_reuse_words": auto_words,
        "automatic_reuse_pct": round(auto_words / total_words * 100, 1) if total_words else 0.0,
        "repetition_words": repetition_words,
        "repetition_groups": unique_repeat_groups,
        "technical_block_words": int(df.loc[df["match_band"].eq("TECHNICAL BLOCK"), "word_count"].sum()) if not df.empty else 0,
        "new_words": int(df.loc[df["match_band"].eq("NEW"), "word_count"].sum()) if not df.empty else 0,
    }


def vendor_ranking(vendor_df: pd.DataFrame, match_df: pd.DataFrame) -> pd.DataFrame:
    """Transparent vendor comparison using supplied commercial data only.

    Required: vendor. Optional rate columns are named rate_CONTEXT, rate_100,
    rate_95_99, rate_85_94, rate_75_84, rate_NEW, rate_TECHNICAL_BLOCK.
    quality_score, delivery_score and risk_score are user/vendor-assessment inputs
    in the 0..100 range; ManualSync does not invent them.
    """
    if vendor_df is None or vendor_df.empty:
        return pd.DataFrame(columns=["vendor","estimated_cost","quality_score","delivery_score","risk_score","value_index"])
    df = match_df.copy().fillna("")
    if "word_count" not in df:
        df["word_count"] = df["source"].astype(str).map(word_count)
    if "match_band" not in df:
        df["match_band"] = df.apply(match_band, axis=1)
    words = df.groupby("match_band")["word_count"].sum().to_dict()

    col_map = {
        "CONTEXT": "rate_CONTEXT", "100%": "rate_100", "95-99%": "rate_95_99",
        "85-94%": "rate_85_94", "75-84%": "rate_75_84", "<75%": "rate_NEW",
        "NEW": "rate_NEW", "TECHNICAL BLOCK": "rate_TECHNICAL_BLOCK",
    }
    rows = []
    for _, v in vendor_df.iterrows():
        name = str(v.get("vendor", "")).strip()
        if not name:
            continue
        new_rate = float(v.get("rate_NEW", DEFAULT_RATES["NEW"]) or DEFAULT_RATES["NEW"])
        cost = 0.0
        for band, count in words.items():
            col = col_map.get(str(band), "rate_NEW")
            rate = float(v.get(col, new_rate) or new_rate)
            cost += float(count) * rate
        quality = min(100.0, max(0.0, float(v.get("quality_score", 70) or 70)))
        delivery = min(100.0, max(0.0, float(v.get("delivery_score", 70) or 70)))
        risk = min(100.0, max(0.0, float(v.get("risk_score", 30) or 30)))
        rows.append({
            "vendor": name,
            "estimated_cost": round(cost, 2),
            "quality_score": quality,
            "delivery_score": delivery,
            "risk_score": risk,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    min_cost = max(0.01, float(out["estimated_cost"].min()))
    out["cost_score"] = (min_cost / out["estimated_cost"].clip(lower=0.01) * 100).clip(upper=100).round(1)
    # Explicit formula: quality 40%, cost 30%, delivery 20%, inverse risk 10%.
    out["value_index"] = (
        out["quality_score"] * 0.40 + out["cost_score"] * 0.30
        + out["delivery_score"] * 0.20 + (100 - out["risk_score"]) * 0.10
    ).round(1)
    return out.sort_values(["value_index","quality_score","estimated_cost"], ascending=[False,False,True]).reset_index(drop=True)


def build_asset_report(tm_summary: dict, leverage: dict, cost_summary: dict, issue_df: pd.DataFrame,
                       cost_df: pd.DataFrame, vendor_df: pd.DataFrame, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    cards = {
        "TM Health": f"{tm_summary.get('health_score', 0)} / 100",
        "TM entries": tm_summary.get("total_entries", 0),
        "TM conflicts": tm_summary.get("conflicting_sources", 0),
        "Unsafe TM entries": tm_summary.get("unsafe_entries", 0),
        "Source words": leverage.get("total_words", 0),
        "Auto reuse": f"{leverage.get('automatic_reuse_pct', 0)}%",
        "Baseline cost": cost_summary.get("baseline_all_new_cost", 0),
        "Projected cost": cost_summary.get("projected_cost", 0),
        "Estimated saving": f"{cost_summary.get('estimated_saving', 0)} ({cost_summary.get('estimated_saving_pct', 0)}%)",
    }
    cards_html = "".join(f"<div class='card'><b>{k}</b><br>{v}</div>" for k,v in cards.items())
    issue_html = issue_df.to_html(index=False, escape=True) if not issue_df.empty else "<p>Nessuna anomalia TM rilevata.</p>"
    cost_html = cost_df.to_html(index=False, escape=True) if not cost_df.empty else "<p>Nessun volume.</p>"
    vendor_html = vendor_df.to_html(index=False, escape=True) if vendor_df is not None and not vendor_df.empty else "<p>Nessun dato fornitore caricato.</p>"
    out_path.write_text(f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><title>ManualSync Asset & Cost Intelligence</title>
<style>body{{font-family:Arial,sans-serif;margin:0;background:#f8fafc;color:#0f172a}}header{{background:#0f172a;color:#fff;padding:28px}}main{{padding:26px}}.cards{{display:flex;gap:10px;flex-wrap:wrap}}.card{{background:#fff;border:1px solid #cbd5e1;border-radius:10px;padding:12px;min-width:180px}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white;margin:16px 0 30px}}th,td{{border:1px solid #cbd5e1;padding:7px;vertical-align:top}}th{{background:#0f766e;color:white}}</style></head><body><header><h1>ManualSync — Language Asset & Cost Intelligence</h1><p>Audit TM, leverage, volume economico e confronto fornitori basato su dati espliciti.</p></header><main><div class='cards'>{cards_html}</div><h2>Cost breakdown</h2>{cost_html}<h2>TM issues</h2>{issue_html}<h2>Vendor comparison</h2>{vendor_html}</main></body></html>""", encoding="utf-8")
    return out_path


def run_asset_intelligence(tm_path: str | Path, matches_csv: str | Path, out_dir: str | Path,
                           rates: dict[str,float] | None = None, vendor_csv: str | Path | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    matches = pd.read_csv(matches_csv).fillna("")
    issues, tm_summary = audit_tm(tm_path)
    cost_df, cost_summary = cost_analysis(matches, rates)
    leverage = leverage_analysis(matches)
    vendors = pd.read_csv(vendor_csv).fillna("") if vendor_csv else pd.DataFrame()
    vendor_result = vendor_ranking(vendors, matches) if not vendors.empty else pd.DataFrame()

    issue_path = out_dir / "TM_HEALTH_ISSUES.csv"
    cost_path = out_dir / "COST_BREAKDOWN.csv"
    vendor_path = out_dir / "VENDOR_RANKING.csv"
    summary_path = out_dir / "ASSET_COST_SUMMARY.json"
    issues.to_csv(issue_path, index=False, encoding="utf-8-sig")
    cost_df.to_csv(cost_path, index=False, encoding="utf-8-sig")
    vendor_result.to_csv(vendor_path, index=False, encoding="utf-8-sig")
    payload = {
        "asset_build": ASSET_BUILD,
        "batch_build": BATCH_BUILD,
        "tm_health": tm_summary,
        "leverage": leverage,
        "cost": cost_summary,
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report = build_asset_report(tm_summary, leverage, cost_summary, issues, cost_df, vendor_result, out_dir / "ASSET_COST_REPORT.html")
    return {
        "issues": issues,
        "tm_summary": tm_summary,
        "cost_breakdown": cost_df,
        "cost_summary": cost_summary,
        "leverage": leverage,
        "vendors": vendor_result,
        "report": report,
        "issues_csv": issue_path,
        "cost_csv": cost_path,
        "vendor_csv": vendor_path,
        "summary_json": summary_path,
    }
