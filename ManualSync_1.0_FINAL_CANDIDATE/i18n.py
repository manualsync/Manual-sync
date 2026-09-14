"""Localization facility — semantic keys, Italian default, English selectable.

Deliberately small: two JSON catalogs, one resolver, no framework. gettext
would buy plural forms and .po tooling we do not need yet, and would put a
compilation step between editing a label and seeing it.

Three rules this module exists to enforce
-----------------------------------------
1. **Keys are semantic, never text.** ``nav.conformity``, not ``"Conformità"``.
   Text as a key means changing a word silently breaks every lookup, and it
   makes the default language structurally privileged.

2. **Business logic never depends on translated text.** The engine's own
   vocabulary (``COVERED``, ``APPLIES``, ``TO_VERIFY``, requirement ids,
   pack ids) is stable, persisted and untranslated. This module maps that
   vocabulary to words at the last possible moment, for display only. A
   translated label must never be used as a dictionary key, a database
   value, a comparison target or a service argument.

3. **Italian is the default and the fallback**, not a translation of
   English. ``it.json`` is the complete catalog; ``en.json`` mirrors it.

Fallback is explicit and deterministic
--------------------------------------
``translate(key, lang)`` resolves in exactly this order:

1. ``key`` in ``lang``'s catalog;
2. else ``key`` in the default (``it``) catalog;
3. else the key itself, returned verbatim.

Step 3 returns the key rather than an empty string or an exception: a
missing label then shows on screen as ``nav.conformity``, which is
obviously wrong, trivially greppable, and never blanks or crashes a page.
The suite asserts IT and EN hold identical key sets, so step 2 should never
fire in shipped code -- it is a safety net, not a strategy.

Placeholders use ``str.format`` syntax. A missing argument leaves the
placeholder intact (``{count}``) instead of raising, for the same reason:
one wrong label must not take down a page. The suite asserts that every
string's placeholders match between IT and EN, which is the real guard.

Language scope -- why a ContextVar and not a module global
----------------------------------------------------------
The selected language is **per session**, held in a ``ContextVar``.

The first cut of this module stored it in a module-level global that
``set_language()`` mutated. That is wrong for a server, and not subtly:
one Gradio process serves every connected browser, so a user switching to
English would change what the NEXT render produced for everyone else. The
bug needs two sessions to show up, which is exactly the kind that survives
single-user testing and appears in a demo.

A ContextVar is the right primitive here. Each request handler runs in its
own task (or in a worker thread with a COPY of the context), so a value
set inside one request cannot leak into another, and the default -- ``it``
-- is what any context that never sets it observes. There is no mutable
process-wide preference left.

The language is session-only for this gate: it is never written to project
data (a project's decisions must read identically in either language) and
there is no user-preference store to persist into.

``translate(key, lang)`` is pure and takes the language explicitly -- it
reads no ambient state at all, so two callers can resolve two different
languages simultaneously and tests need neither Gradio nor a server.
``t()`` is the thin wrapper over whatever the current context selected.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

DEFAULT_LANGUAGE = "it"
SUPPORTED_LANGUAGES = ("it", "en")

#: Shown in the language selector. Each language names itself, the way a
#: language picker should: someone looking for English should not have to
#: know the Italian word for it.
LANGUAGE_NAMES = {"it": "Italiano", "en": "English"}

LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_catalogs: dict[str, dict[str, str]] = {}

#: The language of the CURRENT context -- one Gradio request, one test, one
#: thread. Never shared between sessions. Read it through current_language()
#: and change it only through use_language()/set_language().
_active_language: ContextVar[str] = ContextVar("erylos_ui_language",
                                               default=DEFAULT_LANGUAGE)


class _Placeholders(dict):
    """Leaves an unsupplied placeholder visible instead of raising."""

    def __missing__(self, key):  # pragma: no cover - exercised via translate()
        return "{" + key + "}"


def normalize_language(code: str | None) -> str:
    """Map anything a caller might pass to a supported language code.

    Accepts ``it``, ``IT``, ``it-IT``, and the display names ``Italiano`` /
    ``English``, so a Gradio selector can hand its own visible value
    straight back without the caller maintaining a second mapping.
    Anything unrecognised resolves to the default rather than raising:
    picking a language is never worth an exception.
    """
    text = str(code or "").strip()
    if not text:
        return DEFAULT_LANGUAGE
    for lang, name in LANGUAGE_NAMES.items():
        if text.casefold() == name.casefold():
            return lang
    base = text.replace("_", "-").split("-", 1)[0].casefold()
    return base if base in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def load_catalog(lang: str) -> dict[str, str]:
    """The catalog for one language, read once and cached.

    A catalog file that is missing or unreadable yields an empty mapping
    rather than an exception, so a packaging mistake degrades to visible
    keys instead of an app that will not start.
    """
    lang = normalize_language(lang)
    cached = _catalogs.get(lang)
    if cached is not None:
        return cached
    path = LOCALES_DIR / f"{lang}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        catalog = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        catalog = {}
    _catalogs[lang] = catalog
    return catalog


def current_language() -> str:
    """The language selected in THIS context. ``it`` unless something set it."""
    return _active_language.get()


def translate(key: str, lang: str | None = None, **params) -> str:
    """Resolve one key.

    Pure with an explicit ``lang``: it reads no ambient state, so two
    sessions can resolve two languages at the same time.
    """
    lang = normalize_language(lang if lang is not None else current_language())
    text = load_catalog(lang).get(key)
    if text is None and lang != DEFAULT_LANGUAGE:
        text = load_catalog(DEFAULT_LANGUAGE).get(key)
    if text is None:
        text = key
    return text.format_map(_Placeholders(params)) if params else text


def t(key: str, **params) -> str:
    """Resolve one key in the language selected for this session."""
    return translate(key, current_language(), **params)


@contextmanager
def use_language(code: str | None):
    """Select a language for the duration of this block, then restore it.

    This is the form every UI entry point should use: the reset is not
    optional bookkeeping, it is what stops one request's choice from
    surviving into whatever else reuses the same context.
    """
    token = _active_language.set(normalize_language(code))
    try:
        yield current_language()
    finally:
        _active_language.reset(token)


def set_language(code: str | None) -> str:
    """Select the language for the CURRENT context only.

    Not a process-wide setting: a context that never calls this -- another
    browser session, another request -- keeps seeing the default. Prefer
    use_language() where a scope exists; this exists for tests and for
    entry points that own their whole context.
    """
    _active_language.set(normalize_language(code))
    return current_language()


def get_language() -> str:
    """Alias of current_language(), kept for call-site readability."""
    return current_language()


def has_key(key: str, lang: str | None = None) -> bool:
    """Whether a key is genuinely present, as opposed to falling back."""
    return key in load_catalog(lang if lang is not None else current_language())


def catalog_keys(lang: str | None = None) -> set[str]:
    return set(load_catalog(lang if lang is not None else current_language()))
