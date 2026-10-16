"""The page Gradio serves, without its automatic calls to the internet.

ERYLOS Desktop is offline-first (CP2.2-D, decisions D2/D6): opening the
interface must not contact any third party. Gradio 6.5.1's page template
(gradio/templates/frontend/index.html, rendered by gradio.routes for "/")
carries two things meant for Gradio's own hosted use:

* an external ``<script src="https://.../iframeResizer.contentWindow.min.js"
  async>``. It only matters when a Gradio app is embedded in a foreign page's
  iframe (as on Hugging Face Spaces). CP2.4 measured it fetched at every page
  load, without an integrity attribute, and executed in the app's origin;
* ``<link rel="preconnect">`` hints to the font host Gradio's themes can use.

ERYLOS is never embedded in a third-party page and ships its own fonts, so
neither is needed. Before the first page is served, ``install()`` gives
``gradio.routes.templates`` a loader that returns -- for frontend/index.html
only -- Gradio's own template minus every resource the browser would load
from outside the app by itself: ``<script>`` with an absolute ``src``, and
``<link>`` with a fetching ``rel`` (preconnect, dns-prefetch, preload,
prefetch, modulepreload, stylesheet, icon) and an absolute ``href``. The
template is otherwise byte-for-byte Gradio's: same placeholders, same files
on disk, nothing patched in site-packages.

The rule is generic on purpose. Naming hosts here would let the next one a
Gradio upgrade adds slip through (and regressione_font_locali C2 forbids font
hosts anywhere in product code). Metadata such as ``<meta content=...>`` is
left alone: a browser does not fetch it.

Never at the expense of the interface: if the template cannot be read, no
override is installed -- Gradio serves its own page -- and ``STATE`` records
why. The offline gates (regressione_offline_runtime.py and the WebView2
verification) then fail loudly; the page is never broken to make them pass.
"""
from __future__ import annotations

import re
from typing import Any

TEMPLATE = "frontend/index.html"

# rel values that make a browser open a connection or download something.
_FETCHING_RELS = frozenset({"preconnect", "dns-prefetch", "preload", "prefetch",
                            "modulepreload", "stylesheet", "icon"})
_ABSOLUTE = re.compile(r"^\s*(?:https?:)?//", re.IGNORECASE)
_SCRIPT_WITH_SRC = re.compile(r"<script\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1[^>]*>\s*</script\s*>",
                              re.IGNORECASE | re.DOTALL)
_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE | re.DOTALL)
_REL = re.compile(r"\brel\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_HREF = re.compile(r"\bhref\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)

#: What install() did, for the offline gates and for diagnosis.
STATE: dict[str, Any] = {"installed": False, "removed": [], "error": None}


def strip_external_resources(html: str) -> tuple[str, list[str]]:
    """``html`` without auto-loaded external resources, and what was removed."""
    removed: list[str] = []

    def _script(m: re.Match) -> str:
        if _ABSOLUTE.match(m.group(2)):
            removed.append(f"script {m.group(2)}")
            return ""
        return m.group(0)

    def _link(m: re.Match) -> str:
        tag = m.group(0)
        rel, href = _REL.search(tag), _HREF.search(tag)
        rels = set(rel.group(2).lower().split()) if rel else set()
        if href and _ABSOLUTE.match(href.group(2)) and rels & _FETCHING_RELS:
            removed.append(f"link[{' '.join(sorted(rels))}] {href.group(2)}")
            return ""
        return tag

    html = _SCRIPT_WITH_SRC.sub(_script, html)
    html = _LINK.sub(_link, html)
    return html, removed


def install() -> bool:
    """Serve the offline page template from now on. Idempotent; never raises."""
    if STATE["installed"]:
        return True
    try:
        from gradio import routes
        from jinja2 import ChoiceLoader, DictLoader

        env = routes.templates.env
        original = env.loader
        source, _filename, _uptodate = original.get_source(env, TEMPLATE)
        cleaned, removed = strip_external_resources(source)
        env.loader = ChoiceLoader([DictLoader({TEMPLATE: cleaned}), original])
        STATE.update(installed=True, removed=removed, error=None)
        return True
    except Exception as exc:  # the interface must still open
        STATE.update(installed=False, removed=[], error=f"{type(exc).__name__}: {exc}")
        return False
