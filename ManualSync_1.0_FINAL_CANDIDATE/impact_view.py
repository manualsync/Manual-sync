"""View model for the Impact panel. Pure: no gradio, no I/O.

Turns an ImpactReport into the two strings the right contextual pane
shows — a compact summary and an expandable detail body — resolving every
code through the ordinary catalog.

This is where impact prose lives, and the only place it lives.
`impact_intelligence` deliberately emits codes and params and no
sentences, so the same report can be rendered in Italian or English
without the domain service knowing either exists. Same separation the
codebase already uses for DomainError: the engine names the situation,
the view says it in words.

Follows compliance_view's precedent of importing i18n directly rather
than taking a translate callable: i18n is a leaf module with no gradio
dependency, so a view module can use it and stay testable standalone.
"""
from __future__ import annotations

import html
from typing import Any, Iterable

import i18n
import impact_intelligence as ii

# The order the panel presents domains in. Structure first (where am I),
# then the things that may need action, then the archival note.
_DOMAIN_DISPLAY_ORDER = (
    ii.DOMAIN_STRUCTURE, ii.DOMAIN_REUSE, ii.DOMAIN_VARIANT,
    ii.DOMAIN_TRANSLATION, ii.DOMAIN_COMPLIANCE, ii.DOMAIN_REVIEW, ii.DOMAIN_RELEASE,
)

# Which summary lines get the "needs attention" treatment. Only things a
# human may have to act on -- "3 publications" is context, not a warning.
_ATTENTION_CATEGORIES = (
    ii.CAT_STALE_TRANSLATIONS,
    ii.CAT_REQUIREMENTS_ATTENTION,
    ii.CAT_OPEN_REVIEWS,
    ii.CAT_RELEASES_PREDATING,
)

# Presentation order of the compact summary: what matters first.
_SUMMARY_ORDER = (
    ii.CAT_STALE_TRANSLATIONS,
    ii.CAT_REQUIREMENTS_ATTENTION,
    ii.CAT_OPEN_REVIEWS,
    ii.CAT_RELEASES_PREDATING,
    ii.CAT_PUBLICATIONS,
    ii.CAT_REUSE_LOCATIONS,
    ii.CAT_PROFILES_AFFECTED,
    ii.CAT_LANGUAGES_PRESENT,
)


def _t(key: str, **params: Any) -> str:
    return i18n.translate(key, None, **params)


def count_label(category: str, n: int) -> str:
    """'1 pubblicazione' / '3 pubblicazioni', from the catalog.

    Singular gets its own key rather than a plural rule: Italian and
    English agree here, and a two-form lookup is honest about the fact
    that this module does not implement pluralisation for languages that
    need more forms. When such a language is added, the catalog grows a
    key; this function does not grow a rule.
    """
    if n == 1:
        one = _t(f"impact.count.{category}.one")
        if one != f"impact.count.{category}.one":
            return one
    return _t(f"impact.count.{category}", n=n)


def reason_text(record: ii.ImpactRecord) -> str:
    """The human sentence for one impact record."""
    return _t(f"impact.reason.{record.code}", **record.param_dict)


def summary_lines(report: ii.ImpactReport) -> list[tuple[str, str, bool]]:
    """(category, text, needs_attention) for the compact view.

    A category that is unknown or not applicable is reported as such
    instead of being silently dropped or shown as zero -- the whole point
    of keeping those states distinct in the service.
    """
    out: list[tuple[str, str, bool]] = []
    for cat in _SUMMARY_ORDER:
        state = report.state_of(cat)
        if state == ii.STATE_NOT_APPLICABLE:
            continue  # meaningless for this kind: saying nothing is correct
        if state == ii.STATE_UNDETERMINED:
            # Said out loud, never rendered as a zero: "Profiles affected:
            # cannot be determined" is information; "0 profiles affected"
            # would be a false statement about the content.
            out.append((cat, f"{_t(f'impact.category.{cat}')}: {_t('impact.unknown')}", False))
            continue
        n = report.counts.get(cat, 0)
        if n == 0 and cat not in _ATTENTION_CATEGORIES:
            continue  # a plain zero of a context category is not worth a line
        if n == 0:
            continue
        out.append((cat, count_label(cat, n), cat in _ATTENTION_CATEGORIES))
    return out


def compact_html(report: ii.ImpactReport | None) -> str:
    """The always-visible part of the panel."""
    if report is None:
        return f"<div class='ms-impact-empty'>{html.escape(_t('impact.select_object'))}</div>"
    lines = summary_lines(report)
    if not lines:
        return f"<div class='ms-impact-empty'>{html.escape(_t('impact.none'))}</div>"
    parts = []
    for _cat, text, attention in lines:
        cls = "ms-impact-line ms-impact-attention" if attention else "ms-impact-line"
        parts.append(f"<div class='{cls}'>{html.escape(text)}</div>")
    return f"<div class='ms-impact-compact'>{''.join(parts)}</div>"


def details_html(report: ii.ImpactReport | None) -> str:
    """The expandable part: every record, grouped by domain, with its WHY."""
    if report is None:
        return ""
    parts: list[str] = []
    for domain in _DOMAIN_DISPLAY_ORDER:
        records = [r for r in report.by_domain(domain)
                   if r.code != ii.IMPACT_REVIEW_NOT_VERSION_AWARE]
        if not records:
            continue
        heading = html.escape(_t(f"impact.domain.{domain}"))
        items = "".join(
            f"<li>{html.escape(reason_text(r))}</li>" for r in records)
        parts.append(f"<div class='ms-impact-group'><div class='ms-impact-group-title'>"
                     f"{heading}</div><ul>{items}</ul></div>")

    if report.limitations:
        lim = "".join(
            f"<li>{html.escape(_t(f'impact.limitation.{code}'))}</li>"
            for code in report.limitations)
        parts.append(f"<div class='ms-impact-limits'><div class='ms-impact-group-title'>"
                     f"{html.escape(_t('impact.limitations.title'))}</div><ul>{lim}</ul></div>")
    return f"<div class='ms-impact-details'>{''.join(parts)}</div>"


def render(report: ii.ImpactReport | None) -> tuple[str, str]:
    """(compact, details) — what the Gradio layer pushes into two HTML slots."""
    return compact_html(report), details_html(report)
