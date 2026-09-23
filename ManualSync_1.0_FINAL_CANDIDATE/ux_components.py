"""Shared UI building blocks: notices and status badges. Pure: no gradio, no I/O.

One component for every message the user has to read and act on, so that a
blocked translation package, a failed quality check and a missing publishing
profile all look and read the same way:

    [icon] STATUS WORD
           Title                      <- what is happening
           Explanation                <- what it means
           - item / - item            <- which things are involved
           What to do: ...            <- what the user can do
           > Technical details        <- only now, folded, the raw detail

Two rules the audit made non-negotiable:
  * the meaning never rests on colour alone -- every notice carries a status
    WORD and a drawn icon (inline SVG, never an emoji, so it renders the same
    on every system and a screen reader reads the word);
  * the technical reason is never discarded, only moved: it goes into the
    folded "Technical details", not into the headline.

Callers pass already-translated strings (i18n.t); everything is escaped
here, so a content title or an engine message can never inject markup.
"""
from __future__ import annotations

import html
from typing import Any, Iterable

import i18n

BLOCKED = "blocked"
WARNING = "warning"
SUCCESS = "success"
INFO = "info"
ERROR = "error"
KINDS = (BLOCKED, WARNING, SUCCESS, INFO, ERROR)

# 20px, stroked with currentColor so the icon takes the status colour from
# CSS. Each shape is distinct on its own (bar, triangle, check, "i", cross),
# so the kinds stay distinguishable even in greyscale.
_ICONS = {
    BLOCKED: '<circle cx="10" cy="10" r="8"/><line x1="5.5" y1="10" x2="14.5" y2="10"/>',
    WARNING: '<path d="M10 2.8 18.2 17H1.8Z"/><line x1="10" y1="8" x2="10" y2="11.8"/>'
             '<circle cx="10" cy="14.6" r=".5" fill="currentColor"/>',
    SUCCESS: '<circle cx="10" cy="10" r="8"/><polyline points="6.2,10.4 8.8,13 13.8,7.6"/>',
    INFO: '<circle cx="10" cy="10" r="8"/><line x1="10" y1="9" x2="10" y2="14"/>'
          '<circle cx="10" cy="6.2" r=".5" fill="currentColor"/>',
    ERROR: '<circle cx="10" cy="10" r="8"/><line x1="7" y1="7" x2="13" y2="13"/>'
           '<line x1="13" y1="7" x2="7" y2="13"/>',
}


def _t(key: str, **params: Any) -> str:
    return i18n.translate(key, None, **params)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def icon(kind: str) -> str:
    return ("<svg class='ms-ux-icon' viewBox='0 0 20 20' width='20' height='20' fill='none' "
            "stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round' "
            f"aria-hidden='true'>{_ICONS[kind]}</svg>")


def status_word(kind: str) -> str:
    return _t(f"ux.status.{kind}")


def status_badge(kind: str, label: str | None = None) -> str:
    """A compact status marker for tables and headers: icon + word."""
    if kind not in KINDS:
        raise ValueError(f"unknown status kind: {kind!r}")
    return (f"<span class='ms-ux-badge ms-ux-{kind}'>{icon(kind)}"
            f"<span>{_esc(label or status_word(kind))}</span></span>")


def _item_html(item: str | tuple[str, str]) -> str:
    if isinstance(item, tuple):
        headline, todo = item
        return (f"<li><span class='ms-ux-item-head'>{_esc(headline)}</span>"
                + (f"<span class='ms-ux-item-todo'>{_esc(todo)}</span>" if todo else "") + "</li>")
    return f"<li>{_esc(item)}</li>"


def notice(kind: str, title: str, body: str = "", *, items: Iterable[str | tuple[str, str]] = (),
           ordered: bool = False, action: str = "", details: str = "") -> str:
    """The standard notice.

    `items` are plain strings, or (headline, what-to-do) pairs when each
    listed thing has its own remedy -- e.g. the reasons a quality check
    failed. `ordered` numbers them. `action` is the one overall "what to do"
    sentence. `details` is the raw technical reason, shown folded."""
    if kind not in KINDS:
        raise ValueError(f"unknown notice kind: {kind!r}")
    parts = [
        f"<div class='ms-ux-notice ms-ux-{kind}' role='{'alert' if kind in (BLOCKED, ERROR) else 'status'}'>",
        f"<div class='ms-ux-notice-icon'>{icon(kind)}</div><div class='ms-ux-notice-body'>",
        f"<div class='ms-ux-notice-status'>{_esc(status_word(kind))}</div>",
        f"<div class='ms-ux-notice-title'>{_esc(title)}</div>",
    ]
    if body:
        parts.append(f"<p class='ms-ux-notice-text'>{_esc(body)}</p>")
    listed = [x for x in items if (x if isinstance(x, tuple) else str(x).strip())]
    if listed:
        tag = "ol" if ordered else "ul"
        parts.append(f"<{tag} class='ms-ux-notice-list'>" + "".join(_item_html(x) for x in listed) + f"</{tag}>")
    if action:
        parts.append(f"<p class='ms-ux-notice-action'><strong>{_esc(_t('ux.what_to_do'))}</strong> {_esc(action)}</p>")
    if details:
        parts.append(f"<details class='ms-ux-details'><summary>{_esc(_t('ux.technical_details'))}</summary>"
                     f"<pre>{_esc(details)}</pre></details>")
    parts.append("</div></div>")
    return "".join(parts)
