"""View model for the "Affidabilita traduzioni" panel. Pure: no gradio, no I/O.

Turns a TranslationReport into the panel's two HTML strings, resolving
every code through the ordinary catalog.

This is the only place translation prose lives. `translation_intelligence`
emits codes and params and no sentences -- including for the legacy
Italian strings it normalises out of the CAT engine -- so the same report
renders in Italian or English without the engine being duplicated or
modified. Same separation the codebase already uses for DomainError and
for the Impact panel.
"""
from __future__ import annotations

import ast
import html
import json
import re
from typing import Any

import pandas as pd

import i18n
import translation_intelligence as ti
import ux_components as ux

# A mark per status, drawn in the status colour by the stylesheet, and
# always followed by the word: colour alone is not an accessible signal.
# (Checkpoint 4: SVG marks from the shared set instead of emoji.)
_MISSING_MARK = ("<svg class='ms-ux-icon' viewBox='0 0 20 20' width='20' height='20' fill='none' "
                 "stroke='currentColor' stroke-width='1.8' stroke-dasharray='3 2.4' aria-hidden='true'>"
                 "<circle cx='10' cy='10' r='8'/></svg>")
_STATUS_DOT = {
    ti.STATUS_FRESH: ux.icon(ux.SUCCESS),
    ti.STATUS_STALE: ux.icon(ux.ERROR),
    ti.STATUS_MISSING: _MISSING_MARK,
}
_STATUS_CLASS = {
    ti.STATUS_FRESH: "ms-tr-fresh",
    ti.STATUS_STALE: "ms-tr-stale",
    ti.STATUS_MISSING: "ms-tr-missing",
}

# Order: what needs action first, then what is already fine.
_STATUS_ORDER = {ti.STATUS_STALE: 0, ti.STATUS_MISSING: 1, ti.STATUS_FRESH: 2}


def _t(key: str, **params: Any) -> str:
    return i18n.translate(key, None, **params)


def language_name(code: str) -> str:
    """Human language name, falling back to the code itself.

    Resolved on the BASE subtag ("de" for both de-DE and de-AT) so a
    project using a regional variant still reads as a language rather
    than a tag. Target languages are unbounded -- a project may carry any
    BCP-47 code -- so an unknown one renders as the code, which is honest
    and universal, rather than being guessed at or left blank.
    """
    raw = str(code or "").strip()
    if not raw:
        return ""
    for candidate in (raw, raw.split("-")[0].lower()):
        key = f"lang.name.{candidate}"
        name = _t(key)
        if name != key:
            return name
    return raw


def status_label(status: str) -> str:
    return _t(f"translation.status.{status}")


def reason_text(status: ti.LanguageStatus) -> str:
    return _t(f"translation.reason.{status.code}", **status.params)


def reuse_text(reason: ti.ReuseReason) -> str:
    return _t(f"translation.tm.{reason.code}", **reason.param_dict)


def sort_languages(report: ti.TranslationReport) -> list[ti.LanguageStatus]:
    return sorted(report.languages,
                  key=lambda l: (_STATUS_ORDER.get(l.status, 9), l.language))


# ----------------------------------------------------------------------
# Tier 1 — the always-visible panel
# ----------------------------------------------------------------------

def panel_html(report: ti.TranslationReport | None) -> str:
    if report is None:
        return (f"<div class='ms-tr-empty'>"
                f"{html.escape(_t('translation.panel.select_object'))}</div>")
    if not report.languages:
        return (f"<div class='ms-tr-empty'>"
                f"{html.escape(_t('translation.panel.no_languages'))}</div>")

    parts = [f"<div class='ms-tr-version'>"
             f"{html.escape(_t('translation.object.version', version=report.object_version))}</div>"]
    for lang in sort_languages(report):
        dot = _STATUS_DOT.get(lang.status, "")
        cls = _STATUS_CLASS.get(lang.status, "")
        heading = _t("translation.language.heading", language=language_name(lang.language))
        parts.append(
            f"<div class='ms-tr-lang {cls}'>"
            f"<div class='ms-tr-lang-name'>{html.escape(heading)}</div>"
            f"<div class='ms-tr-lang-status'>{dot} {html.escape(status_label(lang.status))}</div>"
            f"<div class='ms-tr-lang-why'>{html.escape(reason_text(lang))}</div>"
            f"</div>")
    return f"<div class='ms-tr-panel'>{''.join(parts)}</div>"


def tier2_state_html(report: ti.TranslationReport | None) -> str:
    """Why the impact analysis is or is not available -- never a stack trace."""
    if report is None or report.tier2_state == ti.TIER2_AVAILABLE:
        return ""
    return (f"<div class='ms-tr-note'>"
            f"{html.escape(_t(f'translation.tier2.{report.tier2_state}'))}</div>")


# ----------------------------------------------------------------------
# Tier 2 — "Analisi impatto traduzione"
# ----------------------------------------------------------------------

def impact_html(report: ti.TranslationReport | None) -> str:
    if report is None:
        return ""
    if report.tier2_state != ti.TIER2_AVAILABLE:
        return tier2_state_html(report)
    if not report.tier2:
        return f"<div class='ms-tr-note'>{html.escape(_t('translation.tier2.empty'))}</div>"

    parts = [f"<div class='ms-tr-note'>{html.escape(_t('translation.tier2.run_hint'))}</div>"]
    for lang in sorted(report.tier2, key=lambda t: t.language):
        figures = [
            _t("translation.tier2.segments", n=lang.segments),
            _t("translation.tier2.reusable", n=lang.reusable_segments),
            _t("translation.tier2.context", n=lang.context_segments),
            _t("translation.tier2.exact", n=lang.exact_segments),
            _t("translation.tier2.fuzzy", n=lang.fuzzy_segments),
            _t("translation.tier2.new", n=lang.new_segments),
            _t("translation.tier2.technical_block", n=lang.technical_block_segments),
        ]
        rows = "".join(f"<li>{html.escape(f)}</li>" for f in figures)
        body = (f"<div class='ms-tr-impact-lang'>"
                f"<div class='ms-tr-lang-name'>{html.escape(language_name(lang.language))}</div>"
                f"<ul class='ms-tr-figures'>{rows}</ul>")
        if not lang.tm_available:
            body += (f"<div class='ms-tr-note'>"
                     f"{html.escape(_t('translation.tier2.no_tm'))}</div>")
        if lang.reasons:
            why = "".join(
                f"<li>{html.escape(reuse_text(r))}"
                f"<span class='ms-tr-seg'>{html.escape(r.source_preview)}</span></li>"
                for r in lang.reasons)
            body += (f"<div class='ms-tr-why-title'>"
                     f"{html.escape(_t('translation.tier2.why'))}</div>"
                     f"<ul class='ms-tr-why'>{why}</ul>")
        parts.append(body + "</div>")
    return f"<div class='ms-tr-impact'>{''.join(parts)}</div>"


def render(report: ti.TranslationReport | None) -> tuple[str, str]:
    """(panel, tier2 availability note) for the always-visible area."""
    return panel_html(report), tier2_state_html(report)


# ======================================================================
# UX Phase 0 -- P0-4: what the quality check found, in plain words.
#
# The engine already records everything on the job: a structured summary
# (qa_summary_json, written by run_roundtrip_qa) and a per-segment audit.
# The UI used to show only "bloccato" next to a raw table, so a user whose
# job was blocked because the SOURCE changed saw zero failing segments and
# no reason at all. Here those facts become reasons, each with its remedy;
# the raw summary stays available, folded, as technical details.
# Nothing is decided here: the job status set by the engine is the truth.
# ======================================================================
JOB_ACTION_KEYS = {
    "Segna inviato": "sent", "Segna in traduzione": "in_translation",
    "Ricevi XLIFF + controllo qualità (QA)": "received", "Ricevi XLIFF + QA": "received",
    "Riesegui controllo qualità (QA)": "rerun", "Riesegui QA": "rerun",
    "Approva traduzione": "approved", "Annulla": "cancelled",
}


def _tn(key: str, n: int, **params: Any) -> str:
    """Catalog text with the `.one` singular variant when n == 1."""
    if n == 1 and i18n.has_key(f"{key}.one"):
        return _t(f"{key}.one", n=n, **params)
    return _t(key, n=n, **params)


def job_language(code: str) -> str:
    """'traduzione tedesca (de-DE)' / 'German translation (de-DE)'."""
    code = str(code or "").strip()
    return _t("ux.lang.translation", name=language_name(code), code=code) if code else ""


def job_state_label(status: str) -> str:
    key = f"ux.job.state.{status}"
    text = _t(key)
    return status if text == key else text


def _values(fragment: str) -> str:
    try:
        vals = ast.literal_eval(fragment)
        return ", ".join(str(v) for v in vals) if isinstance(vals, (list, tuple)) else str(vals)
    except (ValueError, SyntaxError):
        return fragment.strip("[] ")


# Segment categories: which remedy a failing segment calls for.
_TECHNICAL, _EMPTY, _LINGUISTIC, _MISSING, _TERMINOLOGY, _OTHER = (
    "technical", "empty", "linguistic", "missing", "terminology", "other")


def _segment_problems(reason: str) -> list[tuple[str, str]]:
    """(category, plain sentence) for each '; '-joined engine reason. An
    unrecognised reason is kept verbatim -- never dropped."""
    out: list[tuple[str, str]] = []
    for piece in str(reason or "").split(";"):
        p = piece.strip()
        if not p:
            continue
        pair = re.search(r":\s*(\[.*?\])\s*≠\s*(\[.*?\])", p)
        if "segmento mancante" in p:
            out.append((_MISSING, _t("ux.qa.seg.missing_segment")))
        elif "sorgente XLIFF modificata" in p:
            out.append((_TECHNICAL, _t("ux.qa.seg.source_edited")))
        elif "target vuoto" in p:
            out.append((_EMPTY, _t("ux.qa.seg.empty")))
        elif p.startswith("numeri") and pair:
            out.append((_TECHNICAL, _t("ux.qa.seg.numbers", source=_values(pair.group(1)),
                                       target=_values(pair.group(2)))))
        elif p.startswith("codici") and pair:
            out.append((_TECHNICAL, _t("ux.qa.seg.codes", source=_values(pair.group(1)),
                                       target=_values(pair.group(2)))))
        elif "quasi identico" in p:
            out.append((_LINGUISTIC, _t("ux.qa.seg.near_identical")))
        elif "identico alla sorgente" in p:
            out.append((_LINGUISTIC, _t("ux.qa.seg.identical")))
        elif "stopword gate" in p:
            out.append((_LINGUISTIC, _t("ux.qa.seg.wrong_language")))
        else:
            out.append((_OTHER, _t("ux.qa.seg.other", reason=p)))
    return out


def qa_segments(audit: pd.DataFrame | None, strict: bool = False) -> tuple[pd.DataFrame, dict[str, int]]:
    """The failing segments as a readable table, plus how many segments
    fall into each remedy category."""
    cols = [_t("ux.qa.col.segment"), _t("ux.qa.col.source"), _t("ux.qa.col.target"), _t("ux.qa.col.problem")]
    counts = {k: 0 for k in (_TECHNICAL, _EMPTY, _LINGUISTIC, _MISSING, _TERMINOLOGY, _OTHER)}
    if audit is None or getattr(audit, "empty", True):
        return pd.DataFrame(columns=cols), counts
    rows = []
    for n, (_, r) in enumerate(audit.reset_index(drop=True).iterrows(), start=1):
        problems = _segment_problems(r.get("hard_reason", ""))
        term = str(r.get("terminology", "") or "").strip()
        if strict and term:
            problems.append((_TERMINOLOGY, _t("ux.qa.seg.terminology", detail=term)))
        if not problems:
            if str(r.get("status", "")) != "BLOCKED":
                continue
            problems = [(_OTHER, _t("ux.qa.seg.other", reason=str(r.get("status", ""))))]
        for cat in {c for c, _ in problems}:
            counts[cat] += 1
        rows.append([n, str(r.get("source", "")), str(r.get("target", "")), " · ".join(s for _, s in problems)])
    return pd.DataFrame(rows, columns=cols), counts


def qa_reasons(summary: dict, counts: dict[str, int], language: str) -> list[tuple[str, str]]:
    """Every reason the check did not pass, most fundamental first, each
    with its remedy."""
    out: list[tuple[str, str]] = []
    if summary and (not summary.get("source_version_ok", True) or not summary.get("source_snapshot_ok", True)):
        out.append((_t("ux.qa.reason.source_changed"), _t("ux.qa.todo.source_changed")))
    if summary and not summary.get("language_ok", True):
        out.append((_t("ux.qa.reason.language", language=language), _t("ux.qa.todo.language")))
    if summary and not summary.get("unit_set_ok", True):
        out.append((_t("ux.qa.reason.unit_set", missing=len(summary.get("missing_unit_ids") or []),
                       extra=len(summary.get("extra_unit_ids") or [])), _t("ux.qa.todo.unit_set")))
    for cat, reason_key in ((_EMPTY, "empty"), (_LINGUISTIC, "linguistic"), (_TECHNICAL, "technical"),
                            (_TERMINOLOGY, "terminology"), (_OTHER, "other_segments")):
        n = counts.get(cat, 0)
        if n:
            out.append((_tn(f"ux.qa.reason.{reason_key}", n, language=language), _t(f"ux.qa.todo.{reason_key}")))
    if not out:
        out.append((_t("ux.qa.reason.other"), _t("ux.qa.todo.other")))
    return out


def _summary(job: dict) -> dict:
    try:
        return json.loads(job.get("qa_summary_json") or "{}")
    except (TypeError, ValueError):
        return {}


_DETAIL_KEYS = ("global_blockers", "source", "target_lang", "segments", "hard_fail_count", "incomplete_count",
                "linguistic_fail_count", "terminology_issue_count", "missing_unit_ids", "extra_unit_ids")


def qa_notice(job: dict, audit: pd.DataFrame | None) -> tuple[str, pd.DataFrame]:
    """The notice for a job after a quality check, and the readable table
    of its failing segments."""
    summary = _summary(job)
    language = job_language(job.get("target_lang", ""))
    table, counts = qa_segments(audit, bool(summary.get("strict_terminology")))
    details = (json.dumps({k: summary[k] for k in _DETAIL_KEYS if k in summary}, ensure_ascii=False, indent=2)
               if summary else "")
    if str(job.get("status")) == "QA_BLOCKED" or str(job.get("qa_status")) == "BLOCKED":
        reasons = qa_reasons(summary, counts, language)
        return (ux.notice(ux.BLOCKED, _t("ux.qa.blocked.title", language=language), _t("ux.qa.blocked.body"),
                          items=reasons, ordered=len(reasons) > 1, details=details), table)
    return (ux.notice(ux.SUCCESS, _t("ux.qa.ready.title", language=language), _t("ux.qa.ready.body"),
                      action=_t("ux.qa.ready.todo"), details=details), table)


def _next_step(status: str) -> str:
    return _t(f"ux.job.next.{status}") if i18n.has_key(f"ux.job.next.{status}") else ""


def job_result_notice(action: str, job: dict) -> str:
    """After an action went through: what happened and what comes next."""
    key = JOB_ACTION_KEYS.get(action, "generic")
    language = job_language(job.get("target_lang", ""))
    body = _t("ux.job.done.approved.body") if key == "approved" else ""
    return ux.notice(ux.INFO if key == "cancelled" else ux.SUCCESS, _t(f"ux.job.done.{key}", language=language),
                     body, action=_next_step(str(job.get("status", ""))))


def job_not_available_notice(action: str, job: dict) -> str:
    status = str(job.get("status", ""))
    return ux.notice(ux.WARNING, _t("ux.job.not_available.title"),
                     _t("ux.job.not_available.body", language=job_language(job.get("target_lang", "")),
                        state=job_state_label(status), action=action),
                     action=_next_step(status))


# ======================================================================
# UX Phase 0 -- P0-1: why translation packages cannot be generated.
# The engine stays fail-closed; these only say, in words, what it refused
# and what the user can do about it. The facts come from
# project_view.unapproved_for_packages, pinned to the engine's own gate.
# ======================================================================
def content_state_label(status: str) -> str:
    # The engine's status ("In Review") becomes a code-shaped key segment
    # (IN_REVIEW), the form the catalogue allows for stable identifiers.
    key = "ux.content.state." + str(status or "").strip().upper().replace(" ", "_")
    text = _t(key)
    return status if text == key else text


def _item_line(item: Any) -> str:
    state = content_state_label(item.status)
    if getattr(item, "archived", False):
        state = f"{state} · {_t('ux.content.archived')}"
    kind = item.kind if item.kind in ("topic", "block") else "other"
    return _t(f"ux.pkg.item.{kind}", label=item.label, state=state)


def package_no_langs_notice() -> str:
    return ux.notice(ux.BLOCKED, _t("ux.pkg.no_langs.title"), _t("ux.pkg.no_langs.body"),
                     action=_t("ux.pkg.no_langs.todo"))


def package_unapproved_notice(items: list[Any], engine_message: str = "") -> str:
    if len(items) == 1 and items[0].status == "EMPTY":
        return ux.notice(ux.BLOCKED, _t("ux.pkg.empty.title"), _t("ux.pkg.empty.body"), details=engine_message)
    body = _t("ux.pkg.unapproved.body")
    if any(getattr(i, "archived", False) for i in items):
        body = f"{body} {_t('ux.pkg.unapproved.archived')}"
    return ux.notice(ux.BLOCKED, _tn("ux.pkg.unapproved.title", len(items)), body,
                     items=[_item_line(i) for i in items], action=_t("ux.pkg.unapproved.todo"),
                     details=engine_message)


def package_not_found_notice(object_id: str) -> str:
    return ux.notice(ux.WARNING, _t("ux.pkg.not_found.title"), _t("ux.pkg.not_found.body", object=object_id))


def package_done_notice(zips: list[str], where: str = "", details: str = "") -> str:
    langs = []
    for z in zips:
        m = re.search(r"ManualSync_([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)_TRANSLATION_WORK_PACKAGE", str(z))
        langs.append(job_language(m.group(1)) if m else str(z))
    items = langs + ([_t("ux.pkg.done.where", path=where)] if where else [])
    return ux.notice(ux.SUCCESS, _tn("ux.pkg.done.title", len(zips)), _t("ux.pkg.done.body"),
                     items=items, details=details)


def package_error_notice(message: str) -> str:
    return ux.notice(ux.ERROR, _t("ux.pkg.error.title"), _t("ux.pkg.error.body"), details=message)


def job_cancel_confirm_notice(job: dict) -> str:
    return ux.notice(ux.WARNING, _t("ux.job.cancel.confirm.title",
                                    language=job_language(job.get("target_lang", ""))),
                     _t("ux.job.cancel.confirm.body"), action=_t("ux.job.cancel.confirm.todo"))
