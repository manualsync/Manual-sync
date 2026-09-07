"""Milestone 4E.2: Publishing Preview -- a read-only, variant-aware
preview of what a publication would contain for a selected profile.

Pure Python -- no gradio import here, same contract as authoring_view.py,
so every function is testable without a UI runtime. This module NEVER
mutates authoring data: no INSERT/UPDATE/DELETE anywhere, no version
bump, no audit event, no draft, no fork, no reference-count change. It
is a strict read path, architecturally separate from authoring_service.
py's mutation path -- "authoring mutation path != preview read path".

Condition evaluation is NEVER reimplemented here. Every inclusion/
exclusion decision for a topic or a block placement goes through
AuthoringService.evaluate_element_with_profile() -- the exact, unchanged
4E.1 evaluator -- so "no condition -> included", "OR within one
dimension", "AND across dimensions", and "unresolved -> fail closed" are
all inherited automatically, with zero duplicated logic. This module
only ORCHESTRATES that evaluator across a publication's topics/blocks in
their stored order, resolves each block's CURRENT (never historical)
content for read-only rendering, and applies the topic-level PARENT
GATING rule described on build_preview_model().

Human-facing wording (condition summaries, exclusion reasons) reuses
authoring_view.evaluate_summary_text()/_compact_condition_suffix() --
the SAME strings 4E.1's Studio "Valuta contenuto con questo profilo" and
outline already produce -- so standard-mode users see one consistent
vocabulary across the whole product, never a second, slightly-different
copy invented for this milestone.
"""
from __future__ import annotations

import base64
import html as _html
from pathlib import Path
from typing import Any

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService
import authoring_view as av

# Block kinds this preview can safely render read-only. Anything else
# fails visibly (a per-item placeholder) rather than raising -- one
# unsupported/corrupt block must never take down the whole preview.
_TEXT_BLOCK_KINDS = {"paragraph", "note", "warning", "list_item"}

# Explicit extension allowlist, deliberately NOT the OS/`mimetypes`
# registry (which varies by machine and can be user-modified). SVG is
# deliberately excluded even though browsers render it as an image --
# an SVG can embed <script>/event-handler XSS, which base64-inlining it
# into this app's own HTML would hand a direct execution path.
_FIGURE_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

# Bound on how large a SINGLE figure this preview will inline as a data:
# URI. Chosen to keep the transient RAM cost of base64-encoding one
# figure (~1.33x its raw bytes, held only for the duration of building
# this one HTML response, never cached/persisted) comfortably small --
# documentation-manual figures are ordinarily well under this size;
# anything larger degrades to a safe placeholder rather than bloating
# the response or the server's working set.
_FIGURE_MAX_BYTES = 5 * 1024 * 1024

# Bound on the TOTAL raw image bytes this app will embed across ALL
# figures in ONE preview generation -- the per-image cap above only
# stops any single figure from being huge, it does nothing to stop many
# medium figures from adding up (e.g. 10 figures at ~5MB each would still
# be ~50MB raw / ~66MB of base64 text, transiently duplicated again as a
# Python string, then again in the HTML response, then again in the
# browser's own DOM -- multiple times over on a PC with limited usable
# RAM). 12MB is a conservative aggregate for a desktop documentation
# tool: enough for several ordinary figures in one preview, small enough
# that even the worst-case multiplication above (roughly 12MB raw -> 16MB
# base64 text -> held in a couple of places at once) stays a low-single-
# digit-MB-to-tens-of-MB transient cost, never an unbounded one no matter
# how many figures a publication contains.
_PREVIEW_TOTAL_IMAGE_BUDGET_BYTES = 12 * 1024 * 1024


class _PreviewImageBudget:
    """Aggregate raw-byte budget for the figures embedded in ONE preview
    build. Deliberately request-scoped: build_preview_model() constructs
    exactly one of these per call and threads it through as a plain
    argument down to each figure resolution -- never a module-level /
    global counter, so nothing needs resetting between calls and
    concurrent or successive preview builds (different profiles,
    different users, different publications) can never interfere with
    each other's budget. Not persisted anywhere -- it lives only for the
    duration of building one HTML response."""
    __slots__ = ("remaining",)

    def __init__(self, total_bytes: int = _PREVIEW_TOTAL_IMAGE_BUDGET_BYTES) -> None:
        self.remaining = total_bytes

    def try_reserve(self, size: int) -> bool:
        """Atomically (single-threaded, no actual concurrency here)
        reserves `size` bytes if available, decrementing the remaining
        budget; returns False (and reserves nothing) if it would exceed
        what's left. Figures are resolved in stored topic/block order,
        so this is naturally first-come-first-served: figures already
        rendered before the budget ran out stay rendered."""
        if size > self.remaining:
            return False
        self.remaining -= size
        return True


def _figure_data_uri(project_dir: Path, file_path: str,
                      budget: "_PreviewImageBudget | None" = None) -> tuple[str | None, bool]:
    """A browser-consumable `data:` URI for a figure asset, or (None, ...)
    if it can't (or shouldn't) be served. Returns `(data_uri, budget_
    exhausted)` -- `budget_exhausted` is True ONLY when the asset is
    otherwise entirely valid (exists, in-root, supported type, under the
    per-image cap) but embedding it would exceed the aggregate preview
    budget; every other failure (missing, out-of-root, traversal,
    unsupported type, over the per-image cap) reports `(None, False)`.
    The caller uses this distinction purely for user-facing wording --
    "not loaded to limit memory use" is a materially different, less
    alarming message than "unavailable", and conflating the two would
    mislead a user into thinking a perfectly valid figure is broken or
    missing. `budget=None` (e.g. ad-hoc/test callers) skips the aggregate
    check entirely -- only the per-image cap applies.

    Root cause of the PRIOR bug (two iterations): the first attempt used
    a made-up `src='file/<path>'` (not a real Gradio route, hence a
    broken-image icon). The second attempt switched to Gradio's real
    `GET /gradio_api/file=<path>` route -- which *rendered* correctly,
    but that route's URL is, by construction, the resolved filesystem
    path itself (percent-encoded, slashes normalized): serving the image
    correctly and hiding its local path are mutually exclusive with that
    route, no matter how the path is spelled or encoded. Any URL-based
    route on this app is the same trap, since the browser has to be told
    *some* location to fetch, and this app has no second web server /
    CDN to front that location with an opaque address.

    A `data:` URI sidesteps the problem entirely: the image bytes travel
    inside the HTML response itself, so there is no filesystem-path
    string anywhere in what the browser receives -- not in the DOM, not
    in a network request URL, not in dev-tools.

    Defense in depth, unchanged from the previous version:
    1. `Path.resolve()` collapses `..` segments BEFORE the containment
       check, so traversal spellings can never satisfy `relative_to()`.
    2. Containment is checked against THIS project's own known
       materialization area (`<project_dir>/Assets/`), not just
       "somewhere under DATA_ROOT" -- tighter than Gradio's own
       `allowed_paths` check ever was.
    3. Only a fixed image-extension allowlist is read at all (no SVG).
    4. Files over `_FIGURE_MAX_BYTES` are refused rather than inlined.
    New for this version:
    5. Files that would exceed the AGGREGATE per-preview budget are also
       refused, even though they're individually valid and under the
       per-image cap -- reserved via `budget.try_reserve()` BEFORE the
       bytes are ever read off disk, so a budget-exhausted figure never
       costs a wasted read+encode."""
    if not file_path:
        return None, False
    try:
        abs_path = Path(file_path).resolve()
        assets_root = (Path(project_dir) / "Assets").resolve()
        abs_path.relative_to(assets_root)
    except (ValueError, OSError):
        return None, False
    if not abs_path.is_file():
        return None, False
    mime = _FIGURE_MIME_BY_EXT.get(abs_path.suffix.lower())
    if mime is None:
        return None, False
    try:
        size = abs_path.stat().st_size
        if size > _FIGURE_MAX_BYTES:
            return None, False
        if budget is not None and not budget.try_reserve(size):
            return None, True
        raw = abs_path.read_bytes()
    except OSError:
        return None, False
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"), False


def _resolve_block_content(repo: IndustrialRepository, block_row: Any,
                           image_budget: "_PreviewImageBudget | None" = None, *,
                           resolve_figure_bytes: bool = True) -> dict[str, Any]:
    """Read-only resolution of a block's CURRENT content -- never a
    historical version, never mutates anything. Wrapped so a single
    corrupt/unexpected row can only ever degrade ITS OWN preview item,
    never the whole preview (see build_preview_model's per-block try/
    except at the call site for the outer safety net).

    `image_budget` is only consulted for figure blocks -- every other
    kind ignores it; see _figure_data_uri()/build_preview_model() for the
    aggregate-preview-image-budget rationale.

    `resolve_figure_bytes=False` (figure blocks only) skips reading the
    file and base64-encoding it entirely -- title/caption/alt_text are
    still returned (cheap, already-fetched row data, needed nowhere
    right now but harmless to keep), but asset_url is unconditionally
    None. The caller (build_preview_model) passes False for any figure
    that will NOT actually be embedded in the rendered preview (excluded
    by its own condition, or gated out by an excluded parent topic) --
    an excluded figure is never shown as an <img> either way (see
    _render_block_html: excluded blocks render only a reason, never
    their content), so resolving its bytes at all would be pure wasted
    disk I/O and RAM, and would wrongly consume the aggregate image
    budget on behalf of an image nobody will ever see."""
    bt = block_row["block_type"]
    if bt in _TEXT_BLOCK_KINDS:
        return {"kind": bt, "text": block_row["text_content"] or ""}
    if bt == "table":
        trow = repo.conn.execute(
            "SELECT * FROM structured_tables WHERE table_id=?", (block_row["object_ref_id"],)
        ).fetchone()
        if not trow:
            return {"kind": "table", "unavailable": True, "note": "Tabella non disponibile."}
        return {
            "kind": "table", "title": trow["title"], "caption": trow["caption"],
            "columns": av._load_json_safe(trow["columns_json"], []),
            "rows": av._load_json_safe(trow["rows_json"], []),
            "header_rows": int(trow["header_rows"]),
        }
    if bt == "figure":
        frow = repo.conn.execute(
            "SELECT * FROM figures WHERE figure_id=?", (block_row["object_ref_id"],)
        ).fetchone()
        if not frow:
            return {"kind": "figure", "unavailable": True, "note": "Immagine non disponibile."}
        if not resolve_figure_bytes:
            # Excluded / parent-gated -- never rendered as an <img>, so
            # never read from disk, never base64-encoded, never charged
            # against the aggregate image budget. asset_missing=True here
            # is purely a "no src to give the renderer" signal, same
            # shape as every other non-renderable case; it is never
            # actually reached by _render_block_html in the first place
            # (excluded blocks short-circuit to reason-only before
            # touching `content` at all), so no placeholder wording is
            # ever shown for this specific case.
            return {
                "kind": "figure", "title": frow["title"], "caption": frow["caption"],
                "alt_text": frow["alt_text"], "asset_url": None, "asset_missing": True,
                "asset_budget_skipped": False,
            }
        # asset_url is a bounded `data:` URI (image bytes inlined into
        # the HTML itself), never a filesystem path or a path-bearing
        # route -- see _figure_data_uri's own docstring for the full
        # root-cause/security rationale. None here covers "genuinely
        # missing", "outside the project's own approved Assets/ root",
        # "unsupported file type", "too large to inline" (single-image
        # cap), AND "would exceed this preview's aggregate image budget"
        # alike -- the renderer treats all of them the same way at the
        # <img>-tag level (a safe placeholder, never a broken/absent
        # <img>), but asset_budget_skipped distinguishes the last case so
        # the WORDING can differ (never call a valid image "unavailable"
        # just because memory-budget bookkeeping skipped it).
        asset_url, budget_exhausted = _figure_data_uri(
            Path(repo.path).parent, str(frow["file_path"] or ""), image_budget)
        return {
            "kind": "figure", "title": frow["title"], "caption": frow["caption"],
            "alt_text": frow["alt_text"], "asset_url": asset_url, "asset_missing": asset_url is None,
            "asset_budget_skipped": budget_exhausted,
        }
    if bt == "component_ref":
        crow = repo.conn.execute(
            "SELECT title, body FROM components WHERE component_id=?", (block_row["object_ref_id"],)
        ).fetchone()
        if not crow:
            return {
                "kind": "component_ref", "unavailable": True,
                "note": "Componente riusabile — anteprima contenuto non disponibile in questa versione.",
            }
        # 4D deliberately kept component_ref read-only/uneditable in
        # Studio -- this preserves that scope exactly: a plain read of
        # the component's current title/body, never an editor, never a
        # write path.
        return {"kind": "component_ref", "title": crow["title"], "text": crow["body"]}
    return {"kind": bt, "unsupported": True, "note": "Tipo di contenuto non supportato in anteprima."}


def build_preview_model(repo: IndustrialRepository, svc: AuthoringService, publication_id: str,
                        profile_id: str) -> dict[str, Any]:
    """The single, pure, read-only entry point for Publishing Preview.

    Raises KeyError if the publication or the profile does not exist --
    the caller (app.py) turns that into a fail-visible message; this
    function itself never silently substitutes a default.

    Parent gating rule: a topic's own inclusion is evaluated first. Every
    child block's OWN placement condition is still evaluated independently
    (carried in the model as `own_included`, useful for diagnostics/
    tests), but the block's `included` field -- the one the standard
    render reads -- is `topic_included AND own_included`. An excluded
    topic therefore gates out every one of its blocks in the rendered
    result even when a block's own condition would otherwise pass, per
    the ticket's explicit requirement.

    Read-only guarantee: this function issues SELECT statements only,
    through the same repo/svc connection every other read path uses --
    no cursor here ever executes INSERT/UPDATE/DELETE, and
    evaluate_element_with_profile() (the only AuthoringService method
    called) is itself a pure read (confirmed in the 4E.1 report/suite).

    Image budget: a fresh, request-scoped _PreviewImageBudget is created
    here -- once per call, never reused across calls -- and threaded down
    to every figure resolved while building THIS model. Topics/blocks are
    walked in their stored position order (unchanged), so figures are
    naturally embedded first-come-first-served: whichever ones fit inside
    the aggregate budget as it's encountered render normally, and only
    the ones that would push the running total over the cap fall back to
    the budget-exhausted placeholder -- earlier, already-embedded figures
    are never retroactively evicted or re-rendered.

    Budget accounting is scoped to what will ACTUALLY be embedded, not to
    every figure the publication happens to contain. The evaluation order
    per block is deliberately: (1) evaluate the topic's condition once,
    outside this loop, (2) evaluate the block's own condition, (3) AND
    them into `likely_included` -- effective inclusion after parent
    gating, computed BEFORE any figure content is touched -- and only
    THEN (4) resolve content, passing `resolve_figure_bytes=likely_
    included` into _resolve_block_content(). An excluded block (own
    condition fails) or a parent-gated one (its topic is excluded) is
    NEVER read off disk, NEVER base64-encoded, and NEVER reserved against
    the aggregate budget -- matching exactly what the renderer does with
    it downstream (an excluded block always renders as a compact reason,
    never its content, regardless of the "mostra contenuti esclusi"
    toggle). Without this ordering, an early excluded/parent-gated figure
    could silently starve the budget for a later INCLUDED one -- the
    single accounting flaw this ordering exists to prevent."""
    pub = repo.conn.execute("SELECT * FROM publications WHERE publication_id=?", (publication_id,)).fetchone()
    if not pub:
        raise KeyError(publication_id)
    profile = svc.get_variant_profile(profile_id)  # raises KeyError if missing

    topics_out: list[dict[str, Any]] = []
    counts = {"topics_included": 0, "topics_excluded": 0, "blocks_included": 0, "blocks_excluded": 0, "unresolved": 0}
    has_draft = False
    image_budget = _PreviewImageBudget()

    topic_links = repo.conn.execute(
        "SELECT topic_id FROM publication_topics WHERE publication_id=? ORDER BY position", (publication_id,)
    ).fetchall()
    for link in topic_links:
        tid = link["topic_id"]
        trow = repo.conn.execute("SELECT * FROM topics WHERE topic_id=?", (tid,)).fetchone()
        if not trow or trow["archived_at"]:
            # Archived topics never appear in a publication preview,
            # matching assemble_structured_publication's own convention.
            continue

        topic_eval = svc.evaluate_element_with_profile("topic", tid, profile_id)
        topic_included = bool(topic_eval["included"])
        if topic_eval.get("unresolved_dimensions"):
            counts["unresolved"] += 1
        counts["topics_included" if topic_included else "topics_excluded"] += 1
        if trow["status"] == "Draft":
            has_draft = True

        blocks_out: list[dict[str, Any]] = []
        block_rows = repo.conn.execute(
            "SELECT * FROM content_blocks WHERE topic_id=? AND archived_at IS NULL ORDER BY position,block_id",
            (tid,),
        ).fetchall()
        for brow in block_rows:
            bid = brow["block_id"]
            try:
                block_eval = svc.evaluate_element_with_profile("block", bid, profile_id)
                own_included = bool(block_eval["included"])
                block_unresolved = bool(block_eval.get("unresolved_dimensions"))
                reason = av.evaluate_summary_text(block_eval)
                # Condition evaluation (topic + block) happens BEFORE any
                # figure bytes are touched -- `likely_included` is exactly
                # the ticket's "effective inclusion after parent gating"
                # (the `failed` flag below can only ever narrow this
                # further, never widen it, so it's safe to decide image
                # I/O on this alone, ahead of knowing `failed`). This is
                # what stops an excluded/parent-gated figure from ever
                # reading its file or charging the aggregate image budget.
                likely_included = topic_included and own_included
                content = _resolve_block_content(repo, brow, image_budget,
                                                 resolve_figure_bytes=likely_included)
                failed = False
            except Exception as e:  # a single corrupt/unexpected block must never crash the preview
                own_included = False
                block_unresolved = False
                reason = "Impossibile valutare questo contenuto in anteprima."
                content = {"kind": brow["block_type"], "error": True, "note": str(e)}
                failed = True
            effective_included = topic_included and own_included and not failed
            if block_unresolved:
                counts["unresolved"] += 1
            counts["blocks_included" if effective_included else "blocks_excluded"] += 1
            if brow["status"] == "Draft":
                has_draft = True
            badge = av._compact_condition_suffix(svc, brow["condition_json"]).strip(" ·") or None
            blocks_out.append({
                "block_id": bid, "block_type": brow["block_type"], "status": brow["status"],
                "included": effective_included, "own_included": own_included,
                "gated_by_topic": (not topic_included) and own_included and not failed,
                "unresolved": block_unresolved, "reason": reason, "badge": badge, "content": content,
            })

        topic_badge = av._compact_condition_suffix(svc, trow["condition_json"]).strip(" ·") or None
        topics_out.append({
            "topic_id": tid, "title": trow["title"], "status": trow["status"],
            "included": topic_included, "unresolved": bool(topic_eval.get("unresolved_dimensions")),
            "reason": av.evaluate_summary_text(topic_eval), "badge": topic_badge, "blocks": blocks_out,
        })

    return {
        "publication_id": publication_id, "publication_title": pub["title"],
        "profile_id": profile_id, "profile_name": profile["name"], "profile_active": profile["active"],
        "topics": topics_out, "summary": counts, "has_draft_content": has_draft,
    }


def _badge_html(text: str | None, *, warn: bool = False) -> str:
    if not text:
        return ""
    cls = "ms-pp-badge-warn" if warn else "ms-pp-badge"
    return f" <span class='{cls}'>{_html.escape(text)}</span>"


def _render_block_html(block: dict[str, Any], *, show_excluded: bool) -> str:
    content = block["content"]
    # Deliberately NOT authoring_view.block_type_label(): that function's
    # `editable=False` branch appends a STUDIO-EDITOR-specific suffix
    # ("— modifica disponibile nel prossimo incremento"), which has no
    # meaning in a read-only preview (nothing here is ever editable,
    # regardless of block type) and would confusingly appear on perfectly
    # ordinary paragraph text. Preview always wants the PLAIN kind label.
    kind_label = av.BLOCK_TYPE_LABELS.get(block["block_type"], block["block_type"])
    status_html = (f" <span class='ms-pp-status'>{_html.escape(av.status_label(block['status']))}</span>"
                   if block["status"] != "Approved" else "")
    if not block["included"]:
        if not show_excluded:
            return ""
        reason = block["reason"]
        if block.get("gated_by_topic"):
            reason = "Non incluso — il topic che lo contiene non è incluso in questo profilo."
        return (f"<div class='ms-pp-block ms-pp-excluded'><div class='ms-pp-block-head'>{kind_label}"
                f"{status_html}</div><div class='ms-pp-reason'>{_html.escape(reason)}</div></div>")

    badge_html = _badge_html(block.get("badge"))
    body = ""
    if content.get("error") or content.get("unavailable") or content.get("unsupported"):
        body = f"<div class='ms-pp-note'>{_html.escape(content.get('note', 'Contenuto non disponibile.'))}</div>"
    elif content["kind"] in _TEXT_BLOCK_KINDS:
        body = f"<p>{_html.escape(content['text'])}</p>"
    elif content["kind"] == "table":
        cols = content["columns"] or []
        rows = content["rows"] or []
        header_rows = content.get("header_rows", 1)
        title_html = f"<div class='ms-pp-table-title'>{_html.escape(content['title'])}</div>" if content.get("title") else ""
        caption_html = f"<div class='ms-pp-table-caption'>{_html.escape(content['caption'])}</div>" if content.get("caption") else ""
        thead = ""
        tbody_rows = rows
        if header_rows and rows:
            thead = "<thead><tr>" + "".join(f"<th>{_html.escape(str(c))}</th>" for c in rows[0]) + "</tr></thead>"
            tbody_rows = rows[1:]
        tbody = "".join(
            "<tr>" + "".join(f"<td>{_html.escape(str(c))}</td>" for c in r) + "</tr>" for r in tbody_rows
        )
        body = (f"{title_html}{caption_html}<table class='ms-pp-table'>{thead}<tbody>{tbody}</tbody></table>"
                if cols or rows else "<div class='ms-pp-note'>Tabella vuota.</div>")
    elif content["kind"] == "figure":
        caption_html = f"<div class='ms-pp-figure-caption'>{_html.escape(content['caption'])}</div>" if content.get("caption") else ""
        if content["asset_missing"] or not content.get("asset_url"):
            # Deliberately NO <img> tag at all here -- a missing src (or
            # one pointing nowhere real) is exactly what produced the
            # browser's broken-image icon in the original bug. Alt text
            # stays visible as part of the placeholder note so it's never
            # silently lost even when the image itself can't render.
            alt_note = f" ({_html.escape(content['alt_text'])})" if content.get("alt_text") else ""
            if content.get("asset_budget_skipped"):
                # This image is genuinely valid (exists, in-root,
                # supported type, under the per-image cap) -- it was only
                # skipped because embedding it would have exceeded this
                # preview's aggregate image budget. Calling it "non
                # disponibile" here would be misleading (the user would
                # reasonably read that as "broken/missing"), so this case
                # gets its own, honest wording instead. Never any raw
                # byte counts in this standard-mode text.
                body = f"<div class='ms-pp-note'>Immagine non caricata nell’anteprima per limitare l’uso di memoria{alt_note}.</div>{caption_html}"
            else:
                body = f"<div class='ms-pp-note'>Immagine non disponibile{alt_note}.</div>{caption_html}"
        else:
            alt = _html.escape(content.get("alt_text") or content.get("caption") or "")
            body = f"<img class='ms-pp-figure' src='{_html.escape(content['asset_url'])}' alt='{alt}'>{caption_html}"
    elif content["kind"] == "component_ref":
        title_html = f"<div class='ms-pp-table-title'>{_html.escape(content['title'])}</div>" if content.get("title") else ""
        body = f"{title_html}<p>{_html.escape(content['text'])}</p>"

    return f"<div class='ms-pp-block'><div class='ms-pp-block-head'>{kind_label}{status_html}{badge_html}</div>{body}</div>"


def _it_plural(count: int, singular: str, plural: str) -> str:
    """Italian singular/plural agreement for the preview summary -- count
    == 1 takes the singular form, every other count (including 0) takes
    the plural, matching standard Italian usage ('0 elementi', '1
    elemento', '2 elementi')."""
    return singular if count == 1 else plural


def render_preview_html(model: dict[str, Any], *, show_excluded: bool = False) -> str:
    """Pure string formatting from an already-built preview model -- zero
    DB access, zero business logic, so it can be unit-tested against a
    hand-built model too. Never shows raw ids/JSON/condition operators;
    every label goes through authoring_view's own established human-
    facing vocabulary (status_label/block_type_label/evaluate_summary_
    text/_compact_condition_suffix)."""
    parts: list[str] = []
    s = model["summary"]
    parts.append(
        "<div class='ms-pagehead'><div><div class='ms-page-kicker'>Anteprima</div>"
        f"<h1>Anteprima pubblicazione</h1><p>{_html.escape(model['publication_title'])}"
        f" · Profilo: {_html.escape(model['profile_name'])}</p></div></div>"
    )
    if not model["profile_active"]:
        parts.append(
            "<div class='ms-pp-warning'>Il profilo selezionato è archiviato ma è ancora usato per "
            "questa anteprima — i risultati restano validi finché non ne scegli un altro.</div>"
        )
    unresolved_class = "ms-pp-warning" if s["unresolved"] else "ms-pp-summary"
    topics_inc_word = _it_plural(s["topics_included"], "incluso", "inclusi")
    topics_exc_word = _it_plural(s["topics_excluded"], "escluso", "esclusi")
    blocks_inc_noun = _it_plural(s["blocks_included"], "contenuto", "contenuti")
    blocks_inc_word = _it_plural(s["blocks_included"], "incluso", "inclusi")
    blocks_exc_word = _it_plural(s["blocks_excluded"], "escluso", "esclusi")
    unresolved_noun = _it_plural(s["unresolved"], "elemento", "elementi")
    unresolved_word = _it_plural(s["unresolved"], "non valutabile", "non valutabili")
    parts.append(
        f"<div class='{unresolved_class}'>{s['topics_included']} topic {topics_inc_word} · "
        f"{s['topics_excluded']} {topics_exc_word}"
        f"<br>{s['blocks_included']} {blocks_inc_noun} {blocks_inc_word} · {s['blocks_excluded']} {blocks_exc_word}"
        f"<br>{s['unresolved']} {unresolved_noun} {unresolved_word}</div>"
    )
    if model["has_draft_content"]:
        parts.append("<div class='ms-pp-note-line'>Questa anteprima include contenuti non ancora approvati.</div>")
    parts.append("<div class='ms-pp-note-line'>Anteprima basata sullo stato corrente del contenuto.</div>")

    for topic in model["topics"]:
        if not topic["included"] and not show_excluded:
            continue
        badge_html = _badge_html(topic.get("badge"))
        status_html = (f" <span class='ms-pp-status'>{_html.escape(av.status_label(topic['status']))}</span>"
                       if topic["status"] != "Approved" else "")
        if not topic["included"]:
            parts.append(
                f"<div class='ms-pp-topic ms-pp-excluded'><h2>{_html.escape(topic['title'])}{status_html}</h2>"
                f"<div class='ms-pp-reason'>{_html.escape(topic['reason'])}</div></div>"
            )
            continue  # parent gate: an excluded topic's children are never rendered, included or not
        block_html = "".join(_render_block_html(b, show_excluded=show_excluded) for b in topic["blocks"])
        parts.append(f"<div class='ms-pp-topic'><h2>{_html.escape(topic['title'])}{status_html}{badge_html}</h2>{block_html}</div>")

    return "".join(parts)
