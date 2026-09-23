"""UX Gate 2A -- page-head localization regression.

Pins the fix for a real defect: `_page_head_html` was defined TWICE in
app.py. The second definition took the subtitle as literal display text
and shadowed the first, so all six call sites -- every one of which
passes a catalog key like `page.content.subtitle` -- rendered that key
verbatim to the user. Six section pages printed a localization key where
their one-line description belonged, in both languages.

These tests fail against the pre-fix source and pass after it.

Scope is deliberately narrow: this file proves the page head resolves,
not that the rest of the UI is localized.
"""
import test_isolation as _isolation  # noqa: E402,F401 -- aliased: this suite collects every global named test_* as a test. FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)
import ast
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
import i18n  # noqa: E402

APP_PATH = Path(app.__file__)

# Every (kicker, title, subtitle) triple app.py actually renders. A new
# section page belongs in this list: that is what puts its head under the
# key-leak and language-switch checks below, and the count assertions
# then keep the list honest rather than being a number to bump.
PAGE_HEADS = [
    ("kicker.workflow", "page.revision.title", "page.revision.subtitle"),
    ("kicker.workflow", "page.content.title", "page.content.subtitle"),
    ("kicker.workflow", "page.impact.title", "page.impact.subtitle"),
    ("kicker.workflow", "page.translation.title", "page.translation.subtitle"),
    ("kicker.workflow", "page.jobs.title", "page.jobs.subtitle"),
    ("kicker.workflow", "page.release.title", "page.release.subtitle"),
    ("kicker.system", "page.settings.title", "page.settings.subtitle"),
]

# A localization key that leaked into rendered output looks like this.
KEY_SHAPED = re.compile(r"\b(?:page|kicker|nav|context|ui|err)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*\b")

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


# ---------------------------------------------------------------- 1. one definition only

def test_single_definition():
    """Only one authoritative `_page_head_html` may exist in app.py."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    defs = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_page_head_html"]
    check("exactly one module-level _page_head_html definition",
          len(defs) == 1, f"found {len(defs)} at lines {[d.lineno for d in defs]}")

    # and nothing rebinds the name anywhere else
    rebinds = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_page_head_html":
                    rebinds.append(node.lineno)
    check("no assignment rebinds _page_head_html", not rebinds, f"rebound at {rebinds}")


def test_signature_is_all_keys():
    """The surviving implementation must take three KEYS, not display text."""
    params = list(inspect.signature(app._page_head_html).parameters)
    check("signature is (kicker_key, title_key, subtitle_key)",
          params == ["kicker_key", "title_key", "subtitle_key"], str(params))

    src = inspect.getsource(app._page_head_html)
    # the subtitle must go through the translator, not be interpolated raw
    check("subtitle is translated, not interpolated verbatim",
          "{t(subtitle_key)}" in src and "{subtitle}" not in src, "")


# ---------------------------------------------------------------- 2. no key leaks to the user

def test_no_key_rendered_literally():
    """No page head, in any supported language, may display a raw key."""
    for lang in i18n.SUPPORTED_LANGUAGES:
        with i18n.use_language(lang):
            for kicker, title, subtitle in PAGE_HEADS:
                html = app._page_head_html(kicker, title, subtitle)
                leaked = KEY_SHAPED.findall(html)
                check(f"[{lang}] {title}: no localization key in rendered head",
                      not leaked, f"leaked {leaked}")


def test_subtitle_specifically_resolves():
    """The exact defect: `page.*.subtitle` must not survive into output."""
    for lang in i18n.SUPPORTED_LANGUAGES:
        with i18n.use_language(lang):
            for kicker, title, subtitle in PAGE_HEADS:
                html = app._page_head_html(kicker, title, subtitle)
                check(f"[{lang}] {subtitle} does not appear verbatim",
                      subtitle not in html, html[:120])
                # and the resolved text is actually present
                expected = i18n.translate(subtitle, lang)
                check(f"[{lang}] {subtitle} resolves into the <p>",
                      f"<p>{expected}</p>" in html, html[:160])


# ---------------------------------------------------------------- 3. IT and EN both correct

def test_it_and_en_differ_and_resolve():
    """Both languages must resolve, and to genuinely different text."""
    for kicker, title, subtitle in PAGE_HEADS:
        with i18n.use_language("it"):
            it_html = app._page_head_html(kicker, title, subtitle)
        with i18n.use_language("en"):
            en_html = app._page_head_html(kicker, title, subtitle)
        check(f"{title}: IT and EN heads differ", it_html != en_html, "")

        it_title = i18n.translate(title, "it")
        en_title = i18n.translate(title, "en")
        check(f"{title}: IT title present", f"<h1>{it_title}</h1>" in it_html, it_html[:120])
        check(f"{title}: EN title present", f"<h1>{en_title}</h1>" in en_html, en_html[:120])


def test_catalog_backs_every_call_site():
    """Every key the six call sites pass must exist in BOTH catalogs.

    Without this, deleting the shadowing definition would merely move the
    bug: `t()` falls back to returning the key itself when it is absent.
    """
    for triple in PAGE_HEADS:
        for key in triple:
            for lang in ("it", "en"):
                resolved = i18n.translate(key, lang)
                check(f"catalog[{lang}] defines {key}",
                      resolved != key and resolved.strip() != "", repr(resolved))


def test_call_sites_pass_keys_not_text():
    """Guard the other half: a future call site must pass keys too."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    sites = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_page_head_html"):
            sites.append(node)
    check("every page head is still wired", len(sites) == len(PAGE_HEADS),
          f"found {len(sites)}")
    for node in sites:
        args = [a.value if isinstance(a, ast.Constant) else None for a in node.args]
        check(f"line {node.lineno}: all three args are literal keys",
              len(args) == 3 and all(isinstance(a, str) and "." in a and " " not in a
                                     for a in args),
              str(args))


# ---------------------------------------------------------------- 4. language switch rebuilds it

def test_language_switch_rebuilds_head():
    """The head is registered with LOC so a switch re-renders it."""
    src = APP_PATH.read_text(encoding="utf-8")
    wired = re.findall(r"head_\w+\s*=\s*LOC\.html\(lambda: _page_head_html\(", src)
    check("every page head is registered through LOC.html",
          len(wired) == len(PAGE_HEADS), f"found {len(wired)}")


def run():
    for fn in sorted((v for k, v in globals().items() if k.startswith("test_")),
                     key=lambda f: f.__code__.co_firstlineno):
        try:
            fn()
        except Exception as exc:  # a crashing test is a failing test
            check(f"{fn.__name__} raised", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("=" * 78)
    print("REGRESSIONE — PAGE HEAD LOCALIZATION (UX Gate 2A)")
    print("=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print(f"\n  {passed}/{total} passed")
    print("=" * 78)
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
