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

import html
from typing import Any

import i18n
import translation_intelligence as ti

# A colour per status. Deliberately also a word: colour alone is not an
# accessible signal, and a screen reader gets the word from the label.
_STATUS_DOT = {
    ti.STATUS_FRESH: "\U0001F7E2",    # green
    ti.STATUS_STALE: "\U0001F534",    # red
    ti.STATUS_MISSING: "⚪",      # white
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
