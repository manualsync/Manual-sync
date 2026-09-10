"""Milestone 4F.1: production publishing outputs -- a real PDF and a real
portable offline HTML5 package produced from the SAME publication/profile
semantics Publishing Preview already uses.

Pure Python -- no gradio import here, same contract as publishing_preview.py
and authoring_view.py, so every function is testable without a UI runtime.
Like the preview module this is a strict READ path: it never issues an
INSERT/UPDATE/DELETE, never bumps a version, never writes an audit event
and never changes review/release status. Exporting a deliverable is not a
release decision -- see export_* docstrings.

## One model, two renderers (the "no parallel engine" rule)

    publication + profile
        -> publishing_preview.build_preview_model(figure_mode="path")   [4E.2, unchanged semantics]
        -> build_output_model()          production projection: included content ONLY
        -> render_output_html()          ONE canonical HTML representation
             |-> HTML5 package (index.html + style.css + assets/ + manifest.json)
             `-> PDF (fitz.Story renders that same HTML)

Condition/variant evaluation is never reimplemented here. build_output_model
consumes the 4E.2 preview model verbatim and only *projects* it, so
"no condition -> included", OR-within-dimension, AND-across-dimensions,
fail-closed-on-unresolved and topic parent gating are all inherited. The one
deliberate difference from preview is editorial, not semantic: a production
deliverable contains only what is published -- excluded topics/blocks and
their diagnostic "why was this excluded" reason boxes belong to the preview
screen and are dropped entirely here.

## PDF engine

fitz.Story + fitz.DocumentWriter (PyMuPDF), already a pinned runtime
dependency of this app (layout_engine.py/grounded_authoring.py import it).
No new dependency, no browser automation runtime, no headless Chrome in the
installer. Verified against the pinned build (PyMuPDF 1.26.7 / MuPDF
1.26.12): real selectable text, bold/italic/monospace faces, <ul>/<ol>,
<table> with borders, embedded raster images and HTML entities all render.

Two verified engine limits are handled explicitly rather than hidden:
  * <thead> does NOT repeat across page breaks in this MuPDF build, so a
    long table's header appears on its first page only.
  * a table too wide for the text frame is dropped by the renderer WITHOUT
    raising -- silent content loss. _verify_pdf_content() therefore
    re-extracts the finished PDF's text and fails the export closed if
    representative content is missing, so a corrupt deliverable can never
    be handed over looking successful.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import shutil
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService
import authoring_view as av
import publishing_preview as pp
import rich_text
from product_version import PRODUCT_BUILD, PRODUCT_VERSION

# Aggregate on-disk budget for the assets copied into ONE exported
# package. Deliberately larger than the preview's in-RAM base64 budget
# (_PREVIEW_TOTAL_IMAGE_BUDGET_BYTES): these are plain file copies,
# streamed via shutil.copyfile, never held in memory, so the constraint
# is package portability rather than RAM. Assets beyond the budget are
# reported as warnings, never silently dropped.
_EXPORT_TOTAL_ASSET_BUDGET_BYTES = 64 * 1024 * 1024

# MuPDF renders text with typographic ligatures (fi -> U+FB01 etc). The
# glyphs are correct and the text stays selectable, but a naive substring
# search over the extracted layer would miss them, so every internal
# verification normalizes first. Reader-facing search in mainstream PDF
# viewers normalizes these too; this table exists so THIS module's own
# integrity check can never produce a false "content lost" verdict.
_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
              "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}

_PDF_PAGE_LIMIT = 2000  # hard stop against a pathological non-terminating layout


def normalize_pdf_text(text: str) -> str:
    """Extracted-PDF text in a form safe to substring-search: ligatures
    expanded, unicode normalized, whitespace collapsed."""
    out = unicodedata.normalize("NFKC", text or "")
    for lig, plain in _LIGATURES.items():
        out = out.replace(lig, plain)
    return " ".join(out.split())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug(text: str, *, fallback: str = "output", max_len: int = 60) -> str:
    """Filesystem-safe, ASCII-only, deterministic slug. Used for export
    FOLDER names (publication/profile), never for reader-facing text."""
    norm = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    keep = [(c if (c.isalnum() or c in "-_") else "-") for c in norm]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug[:max_len].strip("-") or fallback)


# ---------------------------------------------------------------------
# Production model: the preview model, projected to publishable content
# ---------------------------------------------------------------------

def build_output_model(repo: IndustrialRepository, svc: AuthoringService,
                       publication_id: str, profile_id: str) -> dict[str, Any]:
    """Included-content-only projection of the 4E.2 preview model.

    Raises KeyError (from build_preview_model) if the publication or the
    profile does not exist -- never silently substitutes a default.

    Everything about WHAT is included is inherited; this function decides
    only what a published document should CONTAIN: excluded topics and
    excluded/parent-gated blocks are dropped outright, along with the
    diagnostic exclusion reasons that belong to the preview screen. The
    original counts are preserved under `summary` so an export manifest
    can still record how much was filtered out for this profile.

    figure_mode="path": figures come back as validated absolute paths
    (same containment/allowlist/size gate the preview applies) instead of
    base64 data: URIs, because exports copy the file rather than inline
    it -- see publishing_preview._resolve_figure_asset."""
    preview = pp.build_preview_model(repo, svc, publication_id, profile_id, figure_mode="path")

    topics_out: list[dict[str, Any]] = []
    for topic in preview["topics"]:
        if not topic["included"]:
            # Parent gating: an excluded topic takes ALL of its blocks out
            # of the deliverable with it, including any whose own
            # condition would have passed -- the same rule 4E.1/4E.2
            # already apply, inherited rather than re-implemented.
            continue
        blocks_out = [
            {"block_type": b["block_type"], "content": b["content"]}
            for b in topic["blocks"] if b["included"]
        ]
        topics_out.append({"topic_id": topic["topic_id"], "title": topic["title"], "blocks": blocks_out})

    return {
        "publication_id": preview["publication_id"],
        "publication_title": preview["publication_title"],
        "profile_id": preview["profile_id"],
        "profile_name": preview["profile_name"],
        "topics": topics_out,
        "summary": preview["summary"],
        "has_draft_content": preview["has_draft_content"],
        "generated_at": _now_iso(),
        "product_version": PRODUCT_VERSION,
        "product_build": PRODUCT_BUILD,
    }


# ---------------------------------------------------------------------
# Canonical HTML representation (shared by the HTML5 package and the PDF)
# ---------------------------------------------------------------------

_PACKAGE_CSS = """
body { font-family: 'Helvetica', 'Arial', sans-serif; font-size: 11pt; line-height: 1.55;
       color: #1a1a1a; margin: 0 auto; max-width: 46em; padding: 2em 1.5em 4em; }
h1.ms-doc-title { font-size: 22pt; margin: 0 0 .15em; }
.ms-doc-meta { color: #555; font-size: 9.5pt; margin: 0 0 2em; padding-bottom: .8em;
               border-bottom: 1px solid #ccc; }
h2.ms-topic { font-size: 15pt; margin: 1.8em 0 .6em; padding-bottom: .2em;
              border-bottom: 1px solid #e0e0e0; }
p { margin: .5em 0; }
code { font-family: 'Courier', monospace; font-size: 10pt; background: #f2f2f2;
       border: 1px solid #e0e0e0; padding: 0 3px; }
ul, ol { margin: .5em 0 .5em 1.4em; padding-left: .6em; }
li { margin: .2em 0; }
a { color: #14539a; }
table.ms-table { border-collapse: collapse; width: 100%; margin: .5em 0 1em; }
table.ms-table th, table.ms-table td { border: 1px solid #999; padding: 5px 7px;
                                       text-align: left; font-size: 9.5pt; vertical-align: top; }
table.ms-table th { background: #eeeeee; font-weight: bold; }
.ms-table-title { font-weight: bold; margin-top: .8em; }
.ms-table-caption { color: #555; font-size: 9.5pt; margin-bottom: .2em; }
.ms-figure { margin: .8em 0; }
.ms-figure img { max-width: 100%; }
.ms-figure-caption { color: #555; font-size: 9.5pt; margin-top: .3em; }
.ms-missing { border: 1px solid #c9a227; background: #fdf6e3; color: #7a5c00;
              padding: .5em .7em; font-size: 9.5pt; margin: .5em 0; }
@media print { body { max-width: none; padding: 0; } a { color: #000; } }
"""

# The PDF stylesheet is a deliberately reduced variant: MuPDF's Story CSS
# support is a subset (no max-width/margin auto centering, no @media), and
# page geometry is supplied by the DocumentWriter frame instead.
_PDF_CSS = """
body { font-family: sans-serif; font-size: 10pt; line-height: 1.45; color: #111; }
h1.ms-doc-title { font-size: 19pt; margin: 0 0 4pt; }
.ms-doc-meta { color: #555; font-size: 8.5pt; margin: 0 0 14pt; }
h2.ms-topic { font-size: 13pt; margin: 14pt 0 5pt; }
p { margin: 4pt 0; }
code { font-family: monospace; font-size: 9pt; background: #f2f2f2; }
ul, ol { margin: 4pt 0 4pt 14pt; }
li { margin: 2pt 0; }
table.ms-table { border-collapse: collapse; width: 100%; }
table.ms-table th, table.ms-table td { border: 1px solid #999; padding: 3pt; text-align: left;
                                       font-size: 8.5pt; }
table.ms-table th { background: #eeeeee; }
.ms-table-title { font-weight: bold; margin-top: 6pt; }
.ms-table-caption { color: #555; font-size: 8.5pt; }
.ms-figure-caption { color: #555; font-size: 8.5pt; }
.ms-missing { border: 1px solid #c9a227; background: #fdf6e3; color: #7a5c00;
              padding: 4pt; font-size: 8.5pt; }
"""


def _render_text_block(content: dict[str, Any], *, for_pdf: bool) -> str:
    """4E.3 rich text -> safe HTML via the SAME rich_text.render_html() the
    editor and the preview use. In PDF mode safe links additionally show
    their URL in parentheses: this MuPDF build does not emit link
    annotations from <a href>, so without it a printed manual would show
    a label pointing nowhere. Unsafe schemes were already degraded to
    literal text by rich_text's own parser and never reach here as links."""
    html = rich_text.render_html(content.get("text", ""), content.get("text_format", "plain"))
    if for_pdf:
        import re as _re

        def _inline_url(m: "_re.Match[str]") -> str:
            return f"{m.group(2)} ({m.group(1)})"

        html = _re.sub(r"<a href=\"([^\"]+)\"[^>]*>(.*?)</a>", _inline_url, html, flags=_re.S)
    return html


def _render_table_block(content: dict[str, Any]) -> str:
    """Header/body structure preserved exactly as stored: header_rows
    decides whether the first stored row is a <thead>. Every cell is
    HTML-escaped. No row or cell is ever dropped or truncated here."""
    if content.get("unavailable"):
        return f"<div class='ms-missing'>{_html.escape(content.get('note', 'Tabella non disponibile.'))}</div>"
    rows = content.get("rows") or []
    cols = content.get("columns") or []
    title = (f"<div class='ms-table-title'>{_html.escape(str(content['title']))}</div>"
             if content.get("title") else "")
    caption = (f"<div class='ms-table-caption'>{_html.escape(str(content['caption']))}</div>"
               if content.get("caption") else "")
    if not rows and not cols:
        return f"{title}{caption}<div class='ms-missing'>Tabella vuota.</div>"
    thead, body_rows = "", rows
    if content.get("header_rows") and rows:
        thead = ("<thead><tr>" + "".join(f"<th>{_html.escape(str(c))}</th>" for c in rows[0])
                 + "</tr></thead>")
        body_rows = rows[1:]
    tbody = "".join("<tr>" + "".join(f"<td>{_html.escape(str(c))}</td>" for c in r) + "</tr>"
                    for r in body_rows)
    return f"{title}{caption}<table class='ms-table'>{thead}<tbody>{tbody}</tbody></table>"


def _render_figure_block(content: dict[str, Any], asset_href: str | None) -> str:
    """A figure renders as a real <img> only when its asset survived the
    security gate AND was materialized into the package. Otherwise it
    degrades to a VISIBLE, documented placeholder that keeps the alt text
    and caption -- never a broken <img>, never a silent disappearance
    (the export's manifest and its returned `warnings` record it too)."""
    caption = (f"<div class='ms-figure-caption'>{_html.escape(str(content['caption']))}</div>"
               if content.get("caption") else "")
    if not asset_href:
        alt = f" ({_html.escape(str(content['alt_text']))})" if content.get("alt_text") else ""
        return (f"<div class='ms-figure'><div class='ms-missing'>Immagine non disponibile"
                f"{alt}.</div>{caption}</div>")
    alt = _html.escape(str(content.get("alt_text") or content.get("caption") or ""))
    return (f"<div class='ms-figure'><img src='{_html.escape(asset_href)}' alt='{alt}'>"
            f"{caption}</div>")


def render_output_html(model: dict[str, Any], *, asset_hrefs: dict[int, str] | None = None,
                       for_pdf: bool = False, embed_css: bool = True) -> str:
    """The ONE canonical HTML representation of a production output.

    `asset_hrefs` maps a figure block's position index (see
    materialize_assets) to the relative href it should use -- identical
    for both consumers ("assets/fig-0001.png"), which is exactly why the
    PDF renderer can be handed the same HTML with an fitz.Archive rooted
    at the staging directory.

    Reader-facing output carries the publication title, the profile name,
    the generation timestamp and the product version. It never carries
    internal ids, filesystem paths, usernames or raw canonical markup."""
    hrefs = asset_hrefs or {}
    parts: list[str] = []
    meta_bits = [f"Profilo: {_html.escape(model['profile_name'])}",
                 f"Generato il {_html.escape(model['generated_at'][:10])}",
                 _html.escape(model["product_version"])]
    parts.append(f"<h1 class='ms-doc-title'>{_html.escape(model['publication_title'])}</h1>")
    parts.append(f"<div class='ms-doc-meta'>{' &middot; '.join(meta_bits)}</div>")

    fig_index = 0
    for topic in model["topics"]:
        parts.append(f"<h2 class='ms-topic'>{_html.escape(topic['title'])}</h2>")
        for block in topic["blocks"]:
            content = block["content"]
            kind = content.get("kind")
            if content.get("error") or content.get("unsupported"):
                parts.append(f"<div class='ms-missing'>"
                             f"{_html.escape(content.get('note', 'Contenuto non disponibile.'))}</div>")
            elif kind in pp._TEXT_BLOCK_KINDS:
                parts.append(_render_text_block(content, for_pdf=for_pdf))
            elif kind == "table":
                parts.append(_render_table_block(content))
            elif kind == "figure":
                parts.append(_render_figure_block(content, hrefs.get(fig_index)))
                fig_index += 1
            elif kind == "component_ref":
                if content.get("unavailable"):
                    parts.append(f"<div class='ms-missing'>{_html.escape(content.get('note', ''))}</div>")
                else:
                    if content.get("title"):
                        parts.append(f"<div class='ms-table-title'>{_html.escape(str(content['title']))}</div>")
                    parts.append(f"<p>{_html.escape(str(content.get('text') or ''))}</p>")
    body = "".join(parts)
    if not embed_css:
        return body
    css = _PDF_CSS if for_pdf else _PACKAGE_CSS
    style = f"<style>{css}</style>" if for_pdf else "<link rel='stylesheet' href='style.css'>"
    return (f"<!DOCTYPE html><html lang='it'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{_html.escape(model['publication_title'])}</title>{style}</head>"
            f"<body>{body}</body></html>")


# ---------------------------------------------------------------------
# Asset materialization
# ---------------------------------------------------------------------

def materialize_assets(model: dict[str, Any], staging_dir: Path) -> tuple[dict[int, str], list[str]]:
    """Copies every included figure's validated asset into
    `<staging>/assets/` under a deterministic, collision-free, ASCII-only
    name derived from document order (`fig-0001.png`) -- never the
    original filename, never an internal id, so the package leaks neither
    the local path nor database identifiers.

    Returns (href map keyed by figure-block ordinal, warnings). Files are
    copied with shutil.copyfile (streamed, never fully buffered in RAM)
    and charged against an aggregate on-disk budget; anything refused is
    reported as a warning and rendered as a visible placeholder rather
    than silently dropped."""
    assets_dir = staging_dir / "assets"
    hrefs: dict[int, str] = {}
    warnings: list[str] = []
    remaining = _EXPORT_TOTAL_ASSET_BUDGET_BYTES
    fig_index = 0
    for topic in model["topics"]:
        for block in topic["blocks"]:
            content = block["content"]
            if content.get("kind") != "figure":
                continue
            idx = fig_index
            fig_index += 1
            src = content.get("asset_path")
            label = content.get("caption") or content.get("title") or f"figura {idx + 1}"
            if not src:
                warnings.append(f"Immagine non disponibile: {label}")
                continue
            src_path = Path(src)
            try:
                size = src_path.stat().st_size
            except OSError:
                warnings.append(f"Immagine non leggibile: {label}")
                continue
            if size > remaining:
                warnings.append(f"Immagine esclusa dal pacchetto per limite di dimensione: {label}")
                continue
            assets_dir.mkdir(parents=True, exist_ok=True)
            name = f"fig-{idx + 1:04d}{src_path.suffix.lower()}"
            try:
                shutil.copyfile(src_path, assets_dir / name)
            except OSError:
                warnings.append(f"Immagine non copiabile: {label}")
                continue
            remaining -= size
            hrefs[idx] = f"assets/{name}"
    return hrefs, warnings


# ---------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------

def _render_pdf_file(html: str, staging_dir: Path, out_pdf: Path) -> int:
    """HTML -> PDF via fitz.Story. The Archive is rooted at the staging
    directory so the SAME `assets/fig-0001.png` hrefs the HTML5 package
    uses resolve here too -- one HTML, two consumers, and no original
    filesystem path is ever handed to the renderer."""
    archive = fitz.Archive(str(staging_dir))
    story = fitz.Story(html=html, archive=archive)
    writer = fitz.DocumentWriter(str(out_pdf))
    mediabox = fitz.paper_rect("A4")
    frame = mediabox + (56, 56, -56, -56)  # ~20mm margins
    pages, more = 0, 1
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(frame)
        story.draw(device)
        writer.end_page()
        pages += 1
        if pages >= _PDF_PAGE_LIMIT:
            break
    writer.close()
    return pages


def _distinctive_word(text: str) -> str | None:
    """The longest single word (>=5 chars) in `text`.

    Deliberately a SINGLE word, not a phrase: the PDF text layer breaks
    lines wherever the frame wraps and inserts list markers ("• ")
    between items, so any multi-word probe risks reporting content as
    "lost" purely because it wrapped. A single word is atomic under
    wrapping in this renderer (no hyphenation), which keeps the integrity
    check sensitive to REAL loss (a dropped table/paragraph takes all of
    its words with it) and free of false alarms."""
    stripped = [w.strip(".,;:!?()[]{}«»\"'—–") for w in (text or "").split()]
    # Only fully alphanumeric candidates: the probe must appear VERBATIM
    # in the extracted layer, so a word carrying internal punctuation
    # ("P-401", "M8*1.25") is skipped rather than silently altered.
    candidates = [w for w in stripped if len(w) >= 5 and w.isalnum()]
    return max(candidates, key=len) if candidates else None


def _pdf_expected_tokens(model: dict[str, Any], *, limit: int = 24) -> list[str]:
    """Representative visible strings that MUST survive into the finished
    PDF: the publication title, every topic heading, and one distinctive
    word per content block (including one per table, whose cells vanish
    entirely when MuPDF drops an over-wide table). Used by
    _verify_pdf_content to catch silent renderer content loss."""
    # Headings go through the same single-word probe: a long title wraps
    # across lines in the text layer, so probing the whole phrase would
    # report a perfectly good PDF as broken.
    tokens: list[str] = []
    title_probe = _distinctive_word(model["publication_title"]) or model["publication_title"]
    tokens.append(title_probe)
    for topic in model["topics"]:
        topic_probe = _distinctive_word(topic["title"])
        if topic_probe:
            tokens.append(topic_probe)
        for block in topic["blocks"]:
            if len(tokens) >= limit:
                return tokens[:limit]
            content = block["content"]
            kind = content.get("kind")
            probe: str | None = None
            if kind in pp._TEXT_BLOCK_KINDS:
                probe = _distinctive_word(rich_text.plain_text(
                    content.get("text", ""), content.get("text_format", "plain")))
            elif kind == "table" and content.get("rows"):
                flat = " ".join(str(c) for row in content["rows"] for c in row)
                probe = _distinctive_word(flat)
            if probe:
                tokens.append(probe)
    return tokens[:limit]


def _verify_pdf_content(pdf_path: Path, model: dict[str, Any]) -> tuple[bool, list[str]]:
    """Re-opens the finished PDF and proves the deliverable is real:
    parsable, non-empty, with extractable text that still contains the
    representative content. Returns (ok, missing_tokens). This is what
    turns MuPDF's silent-drop failure modes into a fail-closed export."""
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return False, ["PDF non apribile"]
    try:
        if doc.page_count < 1:
            return False, ["PDF senza pagine"]
        text = normalize_pdf_text("\n".join(page.get_text() for page in doc))
    finally:
        doc.close()
    if not text.strip():
        return False, ["PDF senza testo estraibile"]
    missing = [t for t in _pdf_expected_tokens(model) if normalize_pdf_text(t) not in text]
    return (not missing), missing


# ---------------------------------------------------------------------
# Export lifecycle
# ---------------------------------------------------------------------

class PublishingOutputError(RuntimeError):
    """Human-facing export failure. app.py shows str(e) as a contained
    message -- never a stack trace, never an internal id or path."""


def export_root_for(project_dir: str | Path, model: dict[str, Any]) -> Path:
    """`<project>/Exports/<publication>/<profile>/` -- project-owned, so a
    deliverable travels with the project and never lands in a user
    directory this app does not own."""
    return (Path(project_dir) / "Exports" / _slug(model["publication_title"], fallback="pubblicazione")
            / _slug(model["profile_name"], fallback="profilo"))


def _unique_target_dir(root: Path, stamp: str) -> Path:
    """Never silently overwrites a previous artifact: a second export in
    the same second becomes `<stamp>-2`, `<stamp>-3`, ..."""
    candidate = root / stamp
    n = 2
    while candidate.exists():
        candidate = root / f"{stamp}-{n}"
        n += 1
    return candidate


def _write_manifest(target: Path, model: dict[str, Any], kind: str,
                    artifacts: list[Path], warnings: list[str]) -> Path:
    """Machine-readable technical metadata (reusing release_manager's own
    sha256-per-file convention so a later release can ingest these
    artifacts without a second checksum scheme). Stays a sibling FILE --
    never embedded in the reader-facing page."""
    manifest = {
        "kind": kind,
        "publication_id": model["publication_id"],
        "publication_title": model["publication_title"],
        "profile_id": model["profile_id"],
        "profile_name": model["profile_name"],
        "generated_at": model["generated_at"],
        "product_version": model["product_version"],
        "product_build": model["product_build"],
        "includes_unapproved_content": bool(model["has_draft_content"]),
        "source_summary": model["summary"],
        "warnings": warnings,
        # `name` is the artifact's path RELATIVE to this manifest's own
        # directory (e.g. "assets/fig-0001.png"), never a bare basename:
        # a bare name would be ambiguous for packaged assets and would
        # make the checksum unverifiable by anyone reading the manifest.
        # Relative also keeps the manifest portable -- no absolute path
        # ever enters a published artifact.
        "artifacts": [
            {"name": a.relative_to(target).as_posix(), "bytes": a.stat().st_size,
             "sha256": _sha256_file(a)}
            for a in artifacts if a.is_file()
        ],
    }
    path = target / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def export_html_package(repo: IndustrialRepository, svc: AuthoringService, publication_id: str,
                        profile_id: str, *, project_dir: str | Path | None = None) -> dict[str, Any]:
    """Portable offline HTML5 package: index.html + style.css + assets/ +
    manifest.json. Opens with no network, no CDN, no JavaScript, and stays
    valid after the folder is copied anywhere (every href is relative).

    Built in a temp staging directory and moved into place only once
    complete, so an interrupted or failed export never leaves a
    successful-looking partial artifact behind."""
    project_dir = Path(project_dir) if project_dir else Path(repo.path).parent
    model = build_output_model(repo, svc, publication_id, profile_id)
    root = export_root_for(project_dir, model)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    staging = Path(tempfile.mkdtemp(prefix="ms_html_export_"))
    try:
        hrefs, warnings = materialize_assets(model, staging)
        (staging / "index.html").write_text(
            render_output_html(model, asset_hrefs=hrefs, for_pdf=False), encoding="utf-8")
        (staging / "style.css").write_text(_PACKAGE_CSS, encoding="utf-8")
        artifacts = [staging / "index.html", staging / "style.css"]
        artifacts += sorted((staging / "assets").glob("*")) if (staging / "assets").is_dir() else []
        _write_manifest(staging, model, "html5", artifacts, warnings)

        root.mkdir(parents=True, exist_ok=True)
        target = _unique_target_dir(root, stamp)
        shutil.move(str(staging), str(target))  # atomic-enough finalize: nothing partial is ever named
        staging = None  # moved
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    return {"kind": "html5", "output_dir": target, "index": target / "index.html",
            "manifest": target / "manifest.json", "warnings": warnings,
            "figures": len(hrefs), "topics": len(model["topics"]),
            "publication_title": model["publication_title"], "profile_name": model["profile_name"]}


def export_pdf(repo: IndustrialRepository, svc: AuthoringService, publication_id: str,
               profile_id: str, *, project_dir: str | Path | None = None) -> dict[str, Any]:
    """Production PDF: real text (selectable/searchable), real fonts, real
    tables, embedded figures. Rendered from the SAME canonical HTML the
    HTML5 package uses.

    Fails CLOSED: the finished file is re-opened and its extracted text
    verified against representative content before it is moved into the
    project. If the renderer silently dropped content (a table too wide
    for the frame is the known case in this MuPDF build), the staged file
    is discarded and PublishingOutputError is raised -- a corrupt
    deliverable is never handed over looking successful."""
    project_dir = Path(project_dir) if project_dir else Path(repo.path).parent
    model = build_output_model(repo, svc, publication_id, profile_id)
    if not model["topics"]:
        raise PublishingOutputError(
            "Nessun contenuto incluso per questo profilo: non è possibile creare il PDF.")
    root = export_root_for(project_dir, model)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    staging = Path(tempfile.mkdtemp(prefix="ms_pdf_export_"))
    try:
        hrefs, warnings = materialize_assets(model, staging)
        html = render_output_html(model, asset_hrefs=hrefs, for_pdf=True)
        staged_pdf = staging / "manuale.pdf"
        try:
            pages = _render_pdf_file(html, staging, staged_pdf)
        except Exception as exc:  # renderer failure -> nothing is published
            raise PublishingOutputError(
                "Non è stato possibile generare il PDF con il contenuto corrente.") from exc
        ok, missing = _verify_pdf_content(staged_pdf, model)
        if not ok:
            raise PublishingOutputError(
                "Il PDF generato non conteneva tutto il contenuto previsto e non è stato salvato. "
                "Controlla le tabelle molto larghe e riprova.")

        root.mkdir(parents=True, exist_ok=True)
        target = _unique_target_dir(root, stamp)
        target.mkdir(parents=True)
        final_pdf = target / "manuale.pdf"
        shutil.move(str(staged_pdf), str(final_pdf))
        _write_manifest(target, model, "pdf", [final_pdf], warnings)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {"kind": "pdf", "output_dir": target, "pdf": final_pdf,
            "manifest": target / "manifest.json", "warnings": warnings, "pages": pages,
            "figures": len(hrefs), "topics": len(model["topics"]),
            "publication_title": model["publication_title"], "profile_name": model["profile_name"]}
