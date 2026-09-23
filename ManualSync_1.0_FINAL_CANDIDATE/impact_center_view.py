"""View model for the Impact Center. Pure: no gradio, no I/O.

Renders an ImpactCenterReport as the screen a customer is shown in a
demo. Four bands, in the order a person actually asks them:

    Cosa cambia                what this edit touches
    Cosa richiede attenzione   what somebody now has to look at
    Cosa e pronto              what is already fine
    Rilascio                   the verdict, on its own

The verdict is separate on purpose. Nested under "Cosa e pronto" it
produced the heading "Cosa e pronto" directly above "Pronto al rilascio:
NO", which is not a sentence to show a prospect.

Everything is resolved through the catalog. The domain layer emits codes
and params; this is the only place the sentences exist.

Vocabulary rule
---------------
The normal panels never use the engine's vocabulary. No "fuzzy", no
"source_version", no "object_version", no "work_bucket" -- those are
schema and CAT terms, and a customer reading them learns nothing except
that the product was built by someone else for someone else. Version
NUMBERS appear ("v12 -> v13") because a version number is a fact a
technical writer already understands; the field names do not.

Raw identifiers are equally absent: a requirement is named by its title,
a publication by its title, a topic by its title. The advanced section is
where ids and engine terms are allowed, because somebody debugging wants
exactly those.
"""
from __future__ import annotations

import html
from typing import Any

import i18n
import impact_center as ic
import impact_intelligence as ii
import translation_intelligence as ti
import translation_view as tv
import ux_components as ux

# Checkpoint 4: the shared SVG status marks instead of emoji; the status
# word always follows them.
_STATUS_ICON = {
    ti.STATUS_FRESH: ux.icon(ux.SUCCESS),
    ti.STATUS_STALE: ux.icon(ux.WARNING),
    ti.STATUS_MISSING: ux.icon(ux.ERROR),
}


def _release_note_text(note: "ic.Note") -> str:
    """One release blocker, in customer language.

    Two things happen here and nowhere else, because both are
    presentation concerns the domain layer must not carry:

    * singular agreement -- "1 traduzione", not "1 traduzioni". Follows
      the `.one` convention already used by impact_view and
      compliance_view: a dedicated key, never a plural rule invented in
      code for a language nobody checked.
    * language codes become language names -- the same "tedesca" the
      translations panel shows. `_release_impact` emits `de-DE` because a
      service that emitted "tedesca" would be emitting prose.
    """
    params = dict(note.param_dict)
    codes = params.get("languages")
    if codes:
        params["languages"] = ", ".join(
            tv.language_name(c.strip()) for c in str(codes).split(",") if c.strip())
    base = f"impactcenter.release.{note.code}"
    if params.get("n") == 1 and i18n.has_key(f"{base}.one"):
        return _t(f"{base}.one", **params)
    return _t(base, **params)


def _t(key: str, **params: Any) -> str:
    return i18n.translate(key, None, **params)


def _esc(value: Any) -> str:
    return html.escape(str(value))


# ----------------------------------------------------------------------
# 1 -- the changed content
# ----------------------------------------------------------------------

def header_html(report: ic.ImpactCenterReport | None) -> str:
    if report is None:
        return f"<div class='ms-ic-empty'>{_esc(_t('impactcenter.select'))}</div>"

    version = (_t("impactcenter.version.transition",
                  previous=report.previous_version, current=report.object_version)
               if report.previous_version else
               _t("impactcenter.version.single", current=report.object_version))
    who = report.last_modified_by or _t("impactcenter.author.unknown")
    when = report.last_modified_at[:10] if report.last_modified_at else "—"

    return (
        f"<div class='ms-ic-header'>"
        f"<div class='ms-ic-kicker'>{_esc(_t('impactcenter.kicker'))}</div>"
        f"<div class='ms-ic-object'>{_esc(report.object_label)}</div>"
        f"<div class='ms-ic-meta'>"
        f"<span class='ms-ic-badge'>{_esc(version)}</span>"
        f"<span>{_esc(_t('impactcenter.lastchange', who=who, when=when))}</span>"
        f"</div></div>")


# ----------------------------------------------------------------------
# 2 -- "Cosa cambia"
# ----------------------------------------------------------------------

def _ref_list(refs, empty_key: str) -> str:
    if not refs:
        return f"<div class='ms-ic-none'>{_esc(_t(empty_key))}</div>"
    items = "".join(
        f"<li>{_esc(r.label)}"
        + (f"<span class='ms-ic-detail'>{_esc(r.detail)}</span>" if r.detail else "")
        + "</li>"
        for r in refs)
    return f"<ul class='ms-ic-list'>{items}</ul>"


def changes_html(report: ic.ImpactCenterReport | None) -> str:
    if report is None:
        return ""
    blocks = [
        (_t("impactcenter.changes.publications"), report.publications,
         "impactcenter.changes.publications.none"),
        (_t("impactcenter.changes.topics"), report.topics,
         "impactcenter.changes.topics.none"),
    ]
    if report.reuse_supported:
        blocks.append((_t("impactcenter.changes.reuse"), report.reuse_locations,
                       "impactcenter.changes.reuse.none"))
    if report.profiles_determinable:
        blocks.append((_t("impactcenter.changes.profiles"), report.profiles,
                       "impactcenter.changes.profiles.none"))

    parts = []
    for title, refs, empty_key in blocks:
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>{_esc(title)}"
                     f"<span class='ms-ic-count'>{len(refs)}</span></div>"
                     f"{_ref_list(refs, empty_key)}</div>")
    return (f"<div class='ms-ic-section'>"
            f"<h3 class='ms-ic-h'>{_esc(_t('impactcenter.section.changes'))}</h3>"
            f"<div class='ms-ic-cols'>{''.join(parts)}</div></div>")


# ----------------------------------------------------------------------
# 3 + 4 -- "Cosa richiede attenzione"
# ----------------------------------------------------------------------

def _language_row(lang: ti.LanguageStatus) -> str:
    icon = _STATUS_ICON.get(lang.status, "")
    name = tv.language_name(lang.language)
    return (f"<div class='ms-ic-lang ms-ic-{lang.status.lower()}'>"
            f"<div class='ms-ic-lang-head'>{icon} "
            f"{_esc(_t('impactcenter.language.row', language=name, status=tv.status_label(lang.status)))}"
            f"</div>"
            f"<div class='ms-ic-why'>{_esc(tv.reason_text(lang))}</div></div>")


def attention_html(report: ic.ImpactCenterReport | None) -> str:
    if report is None:
        return ""
    parts: list[str] = []

    stale = [l for l in report.languages if l.status == ti.STATUS_STALE]
    missing = [l for l in report.languages if l.status == ti.STATUS_MISSING]
    if stale or missing:
        rows = "".join(_language_row(l) for l in stale + missing)
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>"
                     f"{_esc(_t('impactcenter.attention.translations'))}"
                     f"<span class='ms-ic-count'>{len(stale) + len(missing)}</span></div>"
                     f"{rows}</div>")

    # the memory-reuse explanations, when the analysis has been run
    blocked = [r for r in report.translation_reasons
               if r.code in (ti.TM_BLOCKED_NUMERIC_MISMATCH,
                             ti.TM_BLOCKED_MODEL_CODE_MISMATCH,
                             ti.TM_BLOCKED_UNIT_MISMATCH,
                             ti.TM_BLOCKED_TECHNICAL_OTHER)]
    if blocked:
        rows = "".join(
            f"<li>{_esc(tv.reuse_text(r))}"
            f"<span class='ms-ic-detail'>{_esc(r.source_preview)}</span></li>"
            for r in blocked)
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>"
                     f"{_esc(_t('impactcenter.attention.reuse_blocked'))}"
                     f"<span class='ms-ic-count'>{len(blocked)}</span></div>"
                     f"<ul class='ms-ic-list'>{rows}</ul></div>")
    elif (report.translation_detail_requested
          and report.translation_deep_state != ti.TIER2_AVAILABLE):
        # The analysis was asked for and could not run. Saying nothing
        # makes the checkbox look broken -- the browser gate caught
        # exactly that on Draft content. The catalog already owns these
        # sentences, and none of them is a technical error message.
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>"
                     f"{_esc(_t('translation.tier2.title'))}</div>"
                     f"<div class='ms-ic-why'>"
                     f"{_esc(_t(f'translation.tier2.{report.translation_deep_state}'))}"
                     f"</div></div>")

    if report.requirements_reopened:
        rows = "".join(
            f"<li>{_esc(r.label)}"
            f"<span class='ms-ic-detail'>{_esc(_t('impactcenter.attention.requirement_why'))}</span>"
            f"</li>" for r in report.requirements_reopened)
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>"
                     f"{_esc(_t('impactcenter.attention.requirements'))}"
                     f"<span class='ms-ic-count'>{len(report.requirements_reopened)}</span></div>"
                     f"<ul class='ms-ic-list'>{rows}</ul></div>")

    if report.object_status not in ic._REVIEWED_STATUSES:
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>"
                     f"{_esc(_t('impactcenter.attention.review'))}</div>"
                     f"<div class='ms-ic-why'>"
                     f"{_esc(_t('impactcenter.attention.review_why'))}</div></div>")

    if not parts:
        return (f"<div class='ms-ic-section ms-ic-allclear'>"
                f"<h3 class='ms-ic-h'>{_esc(_t('impactcenter.section.attention'))}</h3>"
                f"<div class='ms-ic-none'>{_esc(_t('impactcenter.attention.none'))}</div></div>")
    return (f"<div class='ms-ic-section ms-ic-warn'>"
            f"<h3 class='ms-ic-h'>{_esc(_t('impactcenter.section.attention'))}"
            f"<span class='ms-ic-count ms-ic-count-warn'>{report.attention_count}</span></h3>"
            f"{''.join(parts)}</div>")


# ----------------------------------------------------------------------
# 5 -- "Cosa e pronto" + release
# ----------------------------------------------------------------------

def ready_html(report: ic.ImpactCenterReport | None) -> str:
    if report is None:
        return ""
    parts: list[str] = []

    fresh = [l for l in report.languages if l.status == ti.STATUS_FRESH]
    if fresh:
        rows = "".join(_language_row(l) for l in fresh)
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>"
                     f"{_esc(_t('impactcenter.ready.translations'))}"
                     f"<span class='ms-ic-count'>{len(fresh)}</span></div>{rows}</div>")

    covered = [r for r in report.requirements if r not in report.requirements_reopened]
    if covered:
        rows = "".join(f"<li>{_esc(r.label)}</li>" for r in covered)
        parts.append(f"<div class='ms-ic-group'>"
                     f"<div class='ms-ic-group-title'>"
                     f"{_esc(_t('impactcenter.ready.requirements'))}"
                     f"<span class='ms-ic-count'>{len(covered)}</span></div>"
                     f"<ul class='ms-ic-list'>{rows}</ul></div>")

    body = "".join(parts) if parts else (
        f"<div class='ms-ic-none'>{_esc(_t('impactcenter.ready.none'))}</div>")
    ready_card = (f"<div class='ms-ic-section'>"
                  f"<h3 class='ms-ic-h'>{_esc(_t('impactcenter.section.ready'))}</h3>"
                  f"{body}</div>")

    # The verdict is its OWN card, not a group inside "Cosa e pronto".
    # It was nested there first, and the browser gate rendered the exact
    # sentence a customer must never read: the heading "Cosa e pronto"
    # immediately above "Pronto al rilascio: NO".
    verdict_key = ("impactcenter.release.ready" if report.release_ready
                   else "impactcenter.release.blocked")
    verdict_cls = "ms-ic-verdict-ok" if report.release_ready else "ms-ic-verdict-no"
    section_cls = "ms-ic-allclear" if report.release_ready else "ms-ic-warn"
    notes = "".join(f"<li>{_esc(_release_note_text(n))}</li>"
                    for n in report.release_notes)
    release_card = (
        f"<div class='ms-ic-section {section_cls}'>"
        f"<h3 class='ms-ic-h'>{_esc(_t('impactcenter.section.release'))}</h3>"
        f"<div class='ms-ic-verdict {verdict_cls}'>{_esc(_t(verdict_key))}</div>"
        f"<ul class='ms-ic-list'>{notes}</ul>"
        f"<div class='ms-ic-scope'>{_esc(_t('impactcenter.release.scope_note'))}</div></div>")
    return ready_card + release_card


# ----------------------------------------------------------------------
# Advanced -- the only place engine vocabulary is allowed
# ----------------------------------------------------------------------

def advanced_html(report: ic.ImpactCenterReport | None) -> str:
    if report is None:
        return ""
    rows = [
        (_t("impactcenter.adv.object"), f"{report.object_type} / {report.object_id}"),
        (_t("impactcenter.adv.version"), str(report.object_version)),
        (_t("impactcenter.adv.status"), report.object_status),
        (_t("impactcenter.adv.source_lang"), report.translation.source_lang or "—"),
    ]
    meta = "".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)

    detail = "".join(
        f"<li><code>{_esc(r.code)}</code> {_esc(r.ref_kind)}:{_esc(r.ref_id)} "
        f"{_esc(r.param_dict)}</li>" for r in report.impact.records)

    langs = "".join(
        f"<li><code>{_esc(l.code)}</code> {_esc(l.language)} "
        f"source_version={_esc(l.source_version)} current_version={_esc(l.current_version)}</li>"
        for l in report.languages)

    limits = "".join(f"<li>{_esc(_t(f'impact.limitation.{c}'))}</li>"
                     for c in report.impact.limitations)

    return (f"<div class='ms-ic-adv'>"
            f"<table class='ms-ic-adv-table'>{meta}</table>"
            f"<div class='ms-ic-group-title'>{_esc(_t('impactcenter.adv.records'))}</div>"
            f"<ul class='ms-ic-adv-list'>{detail}</ul>"
            f"<div class='ms-ic-group-title'>{_esc(_t('impactcenter.adv.languages'))}</div>"
            f"<ul class='ms-ic-adv-list'>{langs}</ul>"
            f"<div class='ms-ic-group-title'>{_esc(_t('impact.limitations.title'))}</div>"
            f"<ul class='ms-ic-adv-list'>{limits}</ul></div>")


def render(report: ic.ImpactCenterReport | None) -> tuple[str, str, str, str, str]:
    """(header, changes, attention, ready, advanced)."""
    return (header_html(report), changes_html(report), attention_html(report),
            ready_html(report), advanced_html(report))
