"""Milestone 4E.3: Richer Text Compatibility -- a constrained, deterministic
rich-text layer for paragraph-like content (paragraph/note/warning blocks).

Pure Python -- no gradio import here, same "model != UI wiring" contract as
authoring_view.py/publishing_preview.py, so every function is testable
without a UI runtime.

## Architecture decision (see the 4E.3 implementation report for the full
## trade-off writeup)

The canonical representation is a CONSTRAINED MARKDOWN-LIKE STRING, stored
as-is in the existing `content_blocks.text_content` TEXT column -- NOT a
separate JSON/AST persisted to disk, and NOT a DB schema replacement.
Rationale: five independent subsystems (editor Textbox, save/CAS, word-diff,
substring search, translation-unit extraction) already treat `text_content`
as an opaque plain string; keeping the canonical form itself a plain string
means all five keep working with zero or minimal changes, instead of every
one of them needing to learn a new node-tree shape. This module's parse()/
serialize() pair is the ONLY place that ever turns the string into a
transient in-memory node list -- that list is never what gets persisted.

A NEW additive column, `content_blocks.text_format` ('plain' | 'rich',
DEFAULT 'plain'), tells every reader whether THIS block's text_content is
safe to run through the markup-interpreting parser at all. This is the
answer to a real correctness risk: existing manual text can contain
characters that look like markup but are not -- "M8*1.25" (thread pitch),
"MAX_PRESSURE_LIMIT" (a parameter name), "[1]" (a footnote marker). Blindly
re-interpreting EVERY existing paragraph as rich text the moment this
module shipped would silently reformat real technical content the writer
never asked to be reformatted -- exactly what the milestone's own
"no old paragraph may become unreadable or silently reformatted" rule
forbids. So: 'plain' (the default for every row that existed before this
column did, and for every block never re-saved through the new rich-aware
editor) always renders via simple HTML-escaping, byte-for-byte identical to
this app's pre-4E.3 behavior -- the parser below never even runs over it.
Only 'rich' (set explicitly by AuthoringService.save_block() the moment a
human saves through the rich-aware Studio editor -- a visible, intentional
action, with the SAME text visible in that editor's own live preview pane
at that exact moment) is ever run through the interpreting parser.

## Canonical grammar

Block-level (applied to a whole text_content string, 'rich' format only):
  - every non-blank line matches `^- <text>`            -> one bullet_list
  - every non-blank line matches `^<digits>. <text>`     -> one ordered_list
  - anything else                                        -> one paragraph
    (internal '\\n' characters become explicit `break` nodes; no partial/
    ambiguous interpretation -- a block is EITHER entirely a list or
    entirely a paragraph, never a silent mix, which keeps parsing total,
    unambiguous and trivially deterministic)

Inline (applied within one paragraph's text, or within one list item):
  - `` `code` ``          -> code span (never sub-parsed further)
  - `**_text_**` / `_**text**_` -> bold_italic (the one explicitly
    required "safe combination")
  - `**text**`          -> bold
  - `_text_`            -> italic (guarded by word-boundary lookaround so
                           it does NOT fire inside an identifier like
                           `MAX_PRESSURE_LIMIT` -- see _INLINE_TOKEN_RE)
  - `[label](url)`      -> link (url validated against a safe-scheme
                           allowlist; an unsafe/invalid url degrades the
                           WHOLE token to literal plain text, never a
                           dangerous or a silently-dropped link)
  - anything else        -> literal text

No arbitrary nesting: a link's label is plain text (never sub-parsed for
bold/italic/code inside it); list items are flat (no nested sub-lists).
This is a deliberate scope limit -- conservative, unambiguous, and matches
the ticket's own "avoid arbitrary nesting unless clearly safe" guidance.
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser
from typing import Any

# ---------------------------------------------------------------------
# Grammar constants
# ---------------------------------------------------------------------

_BULLET_RE = re.compile(r"^-\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+\.\s+(.*)$")

# ---------------------------------------------------------------------
# Literal-character escaping (4E.3 stabilization, Blocker 3).
#
# Technical manuals routinely contain characters that are ALSO this
# grammar's markup delimiters -- "M8*1.25" (thread pitch), "[1]"
# (footnote), "MAX_PRESSURE_LIMIT" (a parameter), backticked shell
# fragments containing their own backticks. A backslash-escape (the same
# convention real Markdown uses) is the deterministic mechanism that
# lets a writer's LITERAL text survive round-trip exactly, without
# collapsing the grammar into a full recursive AST. escape_literal() is
# applied by the client-side DOM->canonical serializer (app.py's
# js_on_load snippet) to every literal text run it writes -- a writer
# using the visual editor never types a backslash themselves; the
# escaping is purely an internal storage-safety detail, invisible in
# both the visual editor and every rendered view. unescape_literal() is
# the exact inverse, used both by the parser (so an escaped char
# contributes its LITERAL value, never triggers markup) and by
# AuthoringService.save_block's plain-format storage path (a save that
# turns out to introduce no genuine rich structure stores the UNESCAPED
# text, so 'plain' format -- which never runs this parser -- never
# displays a stray backslash).
_ESCAPABLE_CHARS = "\\*_`[]"


def escape_literal(s: str) -> str:
    return "".join(("\\" + ch) if ch in _ESCAPABLE_CHARS else ch for ch in (s or ""))


def unescape_literal(s: str) -> str:
    out = []
    i = 0
    text = s or ""
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in _ESCAPABLE_CHARS:
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


# Alternation order IS priority order: at any given start position, the
# engine tries the escape sequence, then code, then the two bold_italic
# spellings, then bold, then italic, then link -- whichever matches
# first (leftmost-then-priority) wins. Non-greedy `.+?` captures always
# take the SHORTEST valid span, so "**a** and **b**" correctly yields
# two separate bold runs, not one spanning both. Italic's `_..._`
# carries an extra word-boundary lookaround (bold's `**` does not need
# the same guard: '*' essentially never appears inside real technical
# identifiers, unlike '_') so a snake_case-style identifier such as
# MAX_PRESSURE_LIMIT is never misread as italic markup around
# "PRESSURE". Code's capture allows internal escaped backticks
# (`(?:\\.|[^`\\])*`) so a code span can itself contain a literal
# backtick without prematurely closing.
_INLINE_TOKEN_RE = re.compile(
    r"\\(?P<esc>[\\*_`\[\]])"
    r"|`(?P<code>(?:\\.|[^`\\])*)`"
    r"|\*\*_(?P<boldital1>.+?)_\*\*"
    r"|_\*\*(?P<boldital2>.+?)\*\*_"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|(?<!\w)_(?!\s)(?P<italic>.+?)(?<!\s)_(?!\w)"
    r"|\[(?P<linklabel>[^\]]*)\]\((?P<linkurl>[^)]*)\)"
)

_SAFE_URL_SCHEMES = ("http://", "https://", "mailto:")

_KNOWN_BLOCK_TYPES = {"paragraph", "bullet_list", "ordered_list"}
_KNOWN_INLINE_TYPES = {"text", "bold", "italic", "bold_italic", "code", "link", "break"}

_CANONICAL_EMPTY: list[dict[str, Any]] = [{"type": "paragraph", "inline": []}]


# ---------------------------------------------------------------------
# Link URL safety
# ---------------------------------------------------------------------

def _safe_link_url(url: str) -> str | None:
    """Returns the URL unchanged if it's safe to render as a real <a
    href>, else None (caller degrades the whole link token to plain
    text). Only http/https/mailto pass -- javascript:, data:, file:,
    vbscript: and every unrecognized/relative/schemeless value are
    rejected outright. mailto is allowed deliberately: technical manuals
    routinely need a support-contact link, and mailto: carries no
    script-execution risk."""
    u = (url or "").strip()
    if not u:
        return None
    if any(ord(c) < 0x20 for c in u):  # control chars: reject outright
        return None
    if not u.lower().startswith(_SAFE_URL_SCHEMES):
        return None
    return u


# ---------------------------------------------------------------------
# parse: canonical string -> node list
# ---------------------------------------------------------------------

def _parse_inline(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    runs: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        if m.start() > pos:
            runs.append({"type": "text", "text": text[pos:m.start()]})
        if m.group("esc") is not None:
            # A backslash-escaped delimiter contributes its LITERAL
            # character, merged into plain text -- never a mark.
            runs.append({"type": "text", "text": m.group("esc")})
        elif m.group("code") is not None:
            runs.append({"type": "code", "text": unescape_literal(m.group("code"))})
        elif m.group("boldital1") is not None:
            runs.append({"type": "bold_italic", "text": unescape_literal(m.group("boldital1"))})
        elif m.group("boldital2") is not None:
            runs.append({"type": "bold_italic", "text": unescape_literal(m.group("boldital2"))})
        elif m.group("bold") is not None:
            runs.append({"type": "bold", "text": unescape_literal(m.group("bold"))})
        elif m.group("italic") is not None:
            runs.append({"type": "italic", "text": unescape_literal(m.group("italic"))})
        else:
            label = unescape_literal(m.group("linklabel") or "")
            url = m.group("linkurl") or ""
            safe_url = _safe_link_url(url)
            if safe_url is None:
                # Fail safe: the ENTIRE "[label](url)" substring becomes
                # literal text -- never a dangerous <a href>, never
                # silently dropped from the visible content.
                runs.append({"type": "text", "text": m.group(0)})
            else:
                runs.append({"type": "link", "label": label, "url": safe_url})
        pos = m.end()
    if pos < len(text):
        runs.append({"type": "text", "text": text[pos:]})
    return runs


def parse(raw: str | None) -> list[dict[str, Any]]:
    """Canonical markdown-like string -> node list. Total (never raises)
    and deterministic: the same string always parses to the same nodes.
    An empty/whitespace-only string parses to _CANONICAL_EMPTY -- the one
    canonical representation of "no content", per the milestone's
    invariant list."""
    text = raw or ""
    lines = text.split("\n")
    non_blank = [ln for ln in lines if ln.strip() != ""]
    if not non_blank:
        return [dict(_CANONICAL_EMPTY[0])]
    if all(_BULLET_RE.match(ln) for ln in non_blank):
        items = [_parse_inline(_BULLET_RE.match(ln).group(1)) for ln in non_blank]
        return [{"type": "bullet_list", "items": items}]
    if all(_ORDERED_RE.match(ln) for ln in non_blank):
        items = [_parse_inline(_ORDERED_RE.match(ln).group(1)) for ln in non_blank]
        return [{"type": "ordered_list", "items": items}]
    inline: list[dict[str, Any]] = []
    for i, ln in enumerate(lines):
        if i > 0:
            inline.append({"type": "break"})
        inline.extend(_parse_inline(ln))
    return [{"type": "paragraph", "inline": inline}]


# ---------------------------------------------------------------------
# serialize: node list -> canonical string (also used by normalize())
# ---------------------------------------------------------------------

def _serialize_inline(runs: list[dict[str, Any]]) -> str:
    # Every literal text piece is escape_literal()'d on the way out --
    # the exact inverse of unescape_literal() applied on the way in (see
    # _parse_inline) -- so serialize(parse(x)) round-trips byte-for-byte
    # even when x contains characters that are ALSO grammar delimiters.
    parts = []
    for r in runs:
        rt = r.get("type")
        if rt == "text":
            parts.append(escape_literal(str(r.get("text", ""))))
        elif rt == "bold":
            parts.append(f"**{escape_literal(str(r.get('text', '')))}**")
        elif rt == "italic":
            parts.append(f"_{escape_literal(str(r.get('text', '')))}_")
        elif rt == "bold_italic":
            parts.append(f"**_{escape_literal(str(r.get('text', '')))}_**")
        elif rt == "code":
            parts.append(f"`{escape_literal(str(r.get('text', '')))}`")
        elif rt == "link":
            parts.append(f"[{escape_literal(str(r.get('label', '')))}]({r.get('url', '')})")
        elif rt == "break":
            parts.append("\n")
        # unknown run type: contribute nothing (fail safe, never raise)
    return "".join(parts)


def serialize(nodes: list[dict[str, Any]] | None) -> str:
    """Node list -> canonical markdown-like string. Re-numbers ordered
    lists 1,2,3,... regardless of what digits were originally typed --
    this is precisely what makes two differently-numbered-but-otherwise-
    identical ordered lists normalize to the SAME canonical string."""
    parts = []
    for n in (nodes or []):
        t = n.get("type")
        if t == "paragraph":
            parts.append(_serialize_inline(n.get("inline", [])))
        elif t == "bullet_list":
            parts.append("\n".join(f"- {_serialize_inline(item)}" for item in n.get("items", [])))
        elif t == "ordered_list":
            parts.append("\n".join(f"{i + 1}. {_serialize_inline(item)}"
                                   for i, item in enumerate(n.get("items", []))))
        # unknown block type: contribute nothing (fail safe)
    return "\n".join(parts)


def normalize(raw: str | None, fmt: str = "plain") -> str:
    """Canonical string for equality/comparison purposes. 'plain' format
    is never markup-interpreted (matches render_html's own fail-closed
    behavior for legacy content) -- normalization there is just a
    trailing/leading-whitespace trim, the same trim save_block() already
    applies. 'rich' format normalizes via parse()+serialize(), which is
    idempotent: normalize(normalize(x, 'rich'), 'rich') == normalize(x,
    'rich') for any x, and two semantically-equivalent rich strings (e.g.
    differently-numbered but otherwise identical ordered lists) normalize
    to byte-identical output."""
    if fmt != "rich":
        return (raw or "").strip()
    try:
        return serialize(parse(raw))
    except Exception:
        return (raw or "").strip()


# ---------------------------------------------------------------------
# validate: defensive check for a (possibly hand-built / corrupt) node
# list -- NOT normally needed for parse()'s own output (parse() is total
# and always produces valid nodes), but used to prove "unknown node
# rejected safely" / "malformed model fails safely" against a
# deliberately-corrupted node list, and available for any future caller
# that builds nodes some other way.
# ---------------------------------------------------------------------

def _validate_inline(runs: Any) -> list[str]:
    if not isinstance(runs, list):
        return ["contenuto inline non valido: non è una lista"]
    errors = []
    for r in runs:
        if not isinstance(r, dict) or r.get("type") not in _KNOWN_INLINE_TYPES:
            errors.append(f"elemento inline sconosciuto: {r!r}")
    return errors


def validate(nodes: Any) -> tuple[bool, list[str]]:
    if not isinstance(nodes, list):
        return False, ["documento non valido: la radice non è una lista di blocchi"]
    errors: list[str] = []
    for n in nodes:
        if not isinstance(n, dict) or n.get("type") not in _KNOWN_BLOCK_TYPES:
            errors.append(f"blocco sconosciuto: {n!r}")
            continue
        if n["type"] == "paragraph":
            errors.extend(_validate_inline(n.get("inline", [])))
        else:
            items = n.get("items")
            if not isinstance(items, list):
                errors.append(f"elenco senza items validi: {n!r}")
            else:
                for item in items:
                    errors.extend(_validate_inline(item))
    return (len(errors) == 0), errors


def has_rich_structure(raw: str | None) -> bool:
    """4E.3 stabilization (Blocker 2): True only if `raw` -- parsed as
    'rich' -- contains at least one GENUINE rich structure: a mark
    (bold/italic/bold_italic/code/link) anywhere, or being a list at
    all. A plain paragraph containing only literal text and/or line
    breaks (a `break` run) returns False -- multi-line plain text is a
    pre-existing capability of the ordinary editor and must never, on
    its own, promote a block from 'plain' to 'rich'. This is the single
    source of truth AuthoringService.save_block's 'auto' text_format
    policy uses: promotion happens if and only if this returns True for
    the text being saved."""
    try:
        nodes = parse(raw)
    except Exception:
        return False
    if len(nodes) != 1:
        return True
    node = nodes[0]
    if node.get("type") != "paragraph":
        return True
    return any(r.get("type") not in ("text", "break") for r in node.get("inline", []))


# ---------------------------------------------------------------------
# plain_text: visible-text projection (search, translation, diff-
# equivalence). Marks/URLs are stripped; only what a reader would
# actually SEE survives -- a link contributes its label (not its url),
# a break becomes a single space (so words either side of a line break
# never accidentally concatenate in a search/diff comparison).
# ---------------------------------------------------------------------

def _plain_inline(runs: list[dict[str, Any]]) -> str:
    parts = []
    for r in runs:
        rt = r.get("type")
        if rt == "break":
            parts.append(" ")
        elif rt == "link":
            parts.append(str(r.get("label") or r.get("url") or ""))
        elif "text" in r:
            parts.append(str(r.get("text", "")))
        # unknown run type: contributes nothing
    return "".join(parts)


def plain_text(raw: str | None, fmt: str = "plain") -> str:
    if fmt != "rich":
        return raw or ""
    try:
        nodes = parse(raw)
    except Exception:
        return raw or ""
    pieces = []
    for n in nodes:
        t = n.get("type")
        if t == "paragraph":
            pieces.append(_plain_inline(n.get("inline", [])))
        elif t in ("bullet_list", "ordered_list"):
            pieces.append(" ".join(_plain_inline(item) for item in n.get("items", [])))
    return " ".join(p for p in pieces if p)


# ---------------------------------------------------------------------
# render_html: safe, read-only HTML for both the live editor preview and
# Publishing Preview (single source of truth for both). Only ever emits
# a fixed set of known-safe tags; every piece of user-originated text
# goes through html.escape(); an unsafe link degrades to plain text (see
# _parse_inline); an unknown/corrupt node or run type contributes
# nothing rather than guessing or crashing.
# ---------------------------------------------------------------------

def _render_inline(runs: list[dict[str, Any]]) -> str:
    parts = []
    for r in runs:
        rt = r.get("type")
        if rt == "text":
            parts.append(_html.escape(str(r.get("text", ""))))
        elif rt == "bold":
            parts.append(f"<strong>{_html.escape(str(r.get('text', '')))}</strong>")
        elif rt == "italic":
            parts.append(f"<em>{_html.escape(str(r.get('text', '')))}</em>")
        elif rt == "bold_italic":
            parts.append(f"<strong><em>{_html.escape(str(r.get('text', '')))}</em></strong>")
        elif rt == "code":
            parts.append(f"<code>{_html.escape(str(r.get('text', '')))}</code>")
        elif rt == "link":
            label = _html.escape(str(r.get("label") or r.get("url") or ""))
            url = _html.escape(str(r.get("url", "")), quote=True)
            parts.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>')
        elif rt == "break":
            parts.append("<br>")
        # unknown run type: fail safe, render nothing
    return "".join(parts)


def _render_block_node(node: dict[str, Any]) -> str:
    t = node.get("type")
    if t == "paragraph":
        return f"<p>{_render_inline(node.get('inline', []))}</p>"
    if t == "bullet_list":
        items = "".join(f"<li>{_render_inline(item)}</li>" for item in node.get("items", []))
        return f"<ul>{items}</ul>"
    if t == "ordered_list":
        items = "".join(f"<li>{_render_inline(item)}</li>" for item in node.get("items", []))
        return f"<ol>{items}</ol>"
    return ""  # unknown block type: fail safe, render nothing


def render_html(raw: str | None, fmt: str = "plain") -> str:
    """The ONE rendering path used by both the live Studio editor preview
    and Publishing Preview -- deliberately the same function, so the
    writer's live preview and the reader's actual preview can never
    silently diverge. 'plain' format (every legacy row, and any block
    never re-saved through the rich-aware editor) is byte-for-byte
    identical to this app's pre-4E.3 rendering: a single escaped <p>,
    marks never interpreted. Never raises -- a corrupt/unparseable
    'rich' string falls back to the same literal-escape rendering rather
    than crashing the whole Studio/preview."""
    text = raw or ""
    if fmt != "rich":
        return f"<p>{_html.escape(text)}</p>"
    try:
        nodes = parse(text)
    except Exception:
        return f"<p>{_html.escape(text)}</p>"
    return "".join(_render_block_node(n) for n in nodes)


def render_editable_html(raw: str | None, fmt: str = "plain") -> str:
    """4E.3 stabilization (Blocker 1): what goes INSIDE the standard
    editor's contenteditable surface -- deliberately NOT wrapped in <p>
    (the contenteditable element itself is the implicit single
    paragraph; a block is always exactly one paragraph or one list, see
    the module docstring), and for a list, exactly the <ul>/<ol> markup
    the client-side DOM->canonical serializer (app.py's js_on_load
    snippet) expects to find as the sole child -- client and server
    agree on one shape. Same parser as render_html(), so 'rich' content
    with a literal escaped character still displays correctly (a
    faithfully unescaped literal), never a broken/leftover backslash;
    'plain' content is never parsed at all, matching render_html()'s own
    fail-closed default."""
    text = raw or ""
    if fmt != "rich":
        return _html.escape(text)
    try:
        nodes = parse(text)
    except Exception:
        return _html.escape(text)
    if len(nodes) == 1 and nodes[0].get("type") in ("bullet_list", "ordered_list"):
        return _render_block_node(nodes[0])
    if len(nodes) == 1 and nodes[0].get("type") == "paragraph":
        return _render_inline(nodes[0].get("inline", []))
    return "".join(_render_block_node(n) for n in nodes)  # defensive fallback, not normally reached


# ---------------------------------------------------------------------
# html_to_canonical: the INVERSE of render_editable_html() -- the live
# contenteditable surface's current HTML (browser DOM, serialized to a
# string by the client) -> canonical markdown-like string. This is the
# 4E.3 Save Architecture Replacement's server-side half of the
# hidden-textbox removal: the browser no longer computes canonical text
# itself (that used to duplicate this module's escaping rules in JS,
# see the now-superseded msRichTextGetCanonical); it only ever reports
# its own live HTML, and THIS function -- reusing escape_literal, the
# SAME single source of truth every other canonical-text producer in
# this module already uses -- turns that into the exact same canonical
# form parse()/serialize() work with. Accepts either the bare shape
# render_editable_html() itself returns (inline content, or a lone
# <ul>/<ol>) or that same content wrapped in the editor's own
# contenteditable <div> (what the live DOM capture actually sends) --
# a single optional outer <div> is transparently unwrapped first.
# Only the constrained tag set this editor's toolbar/paste logic can
# ever produce is interpreted (B/STRONG, I/EM, CODE, A, BR, UL>LI,
# OL>LI, DIV/P as a defensive paragraph-break fallback); anything else
# -- and any parse failure -- degrades to its own escaped text content
# rather than raising, the same fail-safe posture as every other
# function in this module.
# ---------------------------------------------------------------------

_BOLD_TAGS = {"b", "strong"}
_ITALIC_TAGS = {"i", "em"}


class _EditableHTMLParser(HTMLParser):
    """Builds a tiny node-list tree ({'tag':..., 'children':[...]} /
    {'tag':'#text','text':...} / {'tag':'br'}) from the editor's
    constrained HTML. Deliberately minimal -- no attribute handling
    beyond <a href>, no self-closing-tag inference beyond <br> -- this
    only ever needs to read back what this SAME editor's own execCommand/
    paste-insertion/server-rendered markup can produce."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[dict[str, Any]] = []
        self._stack: list[list[dict[str, Any]]] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._stack[-1].append({"tag": "br"})
            return
        node: dict[str, Any] = {"tag": tag, "attrs": dict(attrs), "children": []}
        self._stack[-1].append(node)
        self._stack.append(node["children"])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._stack[-1].append({"tag": "br"})
        # any other self-closed tag: contributes nothing (fail safe)

    def handle_endtag(self, tag: str) -> None:
        if tag == "br":
            return
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._stack[-1].append({"tag": "#text", "text": data})


def _html_text_content(node: dict[str, Any]) -> str:
    """Flattens a node (and its children) to plain text -- used for CODE
    (never sub-parsed further, matching the grammar) and as the
    fail-safe fallback for an unrecognized tag. A <br> inside contributes
    a real newline (list-item callers collapse it to a space themselves,
    exactly like the superseded JS serializer did)."""
    if node.get("tag") == "#text":
        return str(node.get("text", ""))
    if node.get("tag") == "br":
        return "\n"
    return "".join(_html_text_content(c) for c in node.get("children", []))


def _html_nodes_to_canonical(nodes: list[dict[str, Any]]) -> str:
    parts = []
    for n in nodes:
        tag = n.get("tag")
        if tag == "#text":
            parts.append(escape_literal(str(n.get("text", ""))))
        elif tag == "br":
            parts.append("\n")
        elif tag in _BOLD_TAGS or tag in _ITALIC_TAGS:
            children = n.get("children", [])
            other = _ITALIC_TAGS if tag in _BOLD_TAGS else _BOLD_TAGS
            # The one safe nested combination the grammar supports,
            # either nesting order (browsers differ on which they
            # produce for a bold+italic selection): a SOLE child that is
            # the other mark, wrapping nothing else alongside it.
            if len(children) == 1 and children[0].get("tag") in other:
                inner = _html_nodes_to_canonical(children[0].get("children", []))
                parts.append(f"**_{inner}_**")
            else:
                inner = _html_nodes_to_canonical(children)
                parts.append(f"**{inner}**" if tag in _BOLD_TAGS else f"_{inner}_")
        elif tag == "code":
            parts.append(f"`{escape_literal(_html_text_content(n))}`")
        elif tag == "a":
            href = str((n.get("attrs") or {}).get("href") or "")
            label = escape_literal(_html_text_content(n))
            parts.append(f"[{label}]({href})")
        elif tag in ("div", "p"):
            parts.append(_html_nodes_to_canonical(n.get("children", [])))
            parts.append("\n")
        elif tag is None:
            continue
        else:
            # unrecognized tag (ul/ol/li appearing somewhere unexpected,
            # or anything else): fail safe, flatten to escaped text only.
            parts.append(escape_literal(_html_text_content(n)))
    return "".join(parts)


def html_to_canonical(html_str: str | None) -> str:
    """Live editor DOM (as an HTML string) -> canonical markdown-like
    string. Never raises -- a parse failure degrades to the HTML with
    tags stripped and its own text escaped, rather than losing the
    writer's edit or crashing Save."""
    text = html_str or ""
    try:
        parser = _EditableHTMLParser()
        parser.feed(text)
        nodes = parser.root
    except Exception:
        return escape_literal(_html.unescape(re.sub(r"<[^>]*>", "", text)))
    # Transparently unwrap a single outer <div>/<span> container (the
    # editor's own contenteditable wrapper, if the caller sent the full
    # live-DOM capture rather than the bare render_editable_html() shape).
    if len(nodes) == 1 and nodes[0].get("tag") in ("div", "span"):
        nodes = nodes[0].get("children", [])
    list_nodes = [n for n in nodes if n.get("tag") in ("ul", "ol")]
    if len(list_nodes) == 1 and len(nodes) == 1:
        list_node = list_nodes[0]
        ordered = list_node.get("tag") == "ol"
        items = [c for c in list_node.get("children", []) if c.get("tag") == "li"]
        lines = []
        for i, li in enumerate(items):
            item_text = _html_nodes_to_canonical(li.get("children", [])).replace("\n", " ")
            prefix = f"{i + 1}. " if ordered else "- "
            lines.append(prefix + item_text)
        return "\n".join(lines)
    return _html_nodes_to_canonical(nodes).rstrip("\n")


# ---------------------------------------------------------------------
# diff_summary: human, format-aware summary layered ON TOP of
# authoring_view.diff_text()'s existing word-level diff (that function is
# NOT modified -- its highlighted-segment output is still used as-is for
# the detailed body; this only replaces/refines the one-line SUMMARY
# for text-kind blocks, using plain-text-equivalence to detect a pure
# formatting-only change, then a list-item-level check for the single
# most common rich-text edit shape).
# ---------------------------------------------------------------------

def diff_summary(old_raw: str | None, new_raw: str | None,
                 old_fmt: str = "plain", new_fmt: str = "plain") -> str:
    old_raw = old_raw or ""
    new_raw = new_raw or ""
    if old_raw == new_raw and old_fmt == new_fmt:
        return "Nessuna modifica."
    old_plain = plain_text(old_raw, old_fmt)
    new_plain = plain_text(new_raw, new_fmt)
    if old_plain == new_plain:
        return "Formattazione modificata."
    try:
        old_nodes = parse(old_raw) if old_fmt == "rich" else [{"type": "paragraph", "inline": [{"type": "text", "text": old_raw}]}]
        new_nodes = parse(new_raw) if new_fmt == "rich" else [{"type": "paragraph", "inline": [{"type": "text", "text": new_raw}]}]
        if (len(old_nodes) == 1 and len(new_nodes) == 1 and
                old_nodes[0].get("type") in ("bullet_list", "ordered_list") and
                old_nodes[0].get("type") == new_nodes[0].get("type")):
            old_items = old_nodes[0].get("items", [])
            new_items = new_nodes[0].get("items", [])
            if len(old_items) == len(new_items) and old_items:
                changed = sum(1 for a, b in zip(old_items, new_items) if _serialize_inline(a) != _serialize_inline(b))
                if changed:
                    noun = "elemento elenco modificato" if changed == 1 else "elementi elenco modificati"
                    return f"{changed} {noun}."
    except Exception:
        pass
    return "Testo modificato."
