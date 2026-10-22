"""Milestone 3D: local, stdlib-only ingestion of Adobe InDesign .idml packages
for legacy onboarding / coexistence.

Scope discipline (mirrors the 3B.1 .doc precedent):
- No Adobe InDesign, Adobe Server or COM automation dependency of any kind.
- IDML is a plain ZIP of UTF-8 XML parts; this module parses it directly with
  the standard library (zipfile + xml.etree.ElementTree) -- no new runtime
  dependency, no second CCMS-writing engine (see authoring_ccms.import_idml_as_topics,
  which feeds the SAME shared _write_topic_groups_to_repo() used by PDF/DOCX/DOC).
- This module is read-only / analysis-only: it never writes into a project's
  CCMS itself, and it never writes an IDML back out (no round-trip, no export).
- Fail-closed: the ENTIRE package is validated and parsed structurally before
  any caller can use the result to write anything. A single malformed
  structural XML part (designmap.xml, a referenced Spread, or a referenced
  Story) aborts the whole import -- see IdmlImportError below. A valid-XML
  construct we don't understand, an orphaned story, or a dangling reference
  is preserved and reported for human review instead, never silently dropped
  and never a crash.
"""
from __future__ import annotations

import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET

from language_signals import _filename_language, _score_text_language, _combine_language_signals

IDML_IMPORT_BUILD = "0.29.0-idml-legacy-onboarding-20260827"

EXPECTED_MIMETYPE = b"application/vnd.adobe.indesign-idml-package"

# Conservative caps for an untrusted, human-supplied package. A real IDML
# manual (even with a handful of copied-in linked images) is realistically a
# few MB to a few hundred MB -- these limits exist to fail closed on a
# maliciously or accidentally corrupt package, not to accommodate legitimate
# multi-gigabyte input.
MAX_ENTRIES = 20_000
MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024
MAX_ENTRY_UNCOMPRESSED = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 300  # applied only above a floor size, see _validate_zip_safety


class IdmlImportError(Exception):
    """Fail-closed outcome of IDML validation/parsing. Never raised after any
    CCMS write has started -- see authoring_ccms.import_idml_as_topics."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _validate_zip_safety(path: Path) -> zipfile.ZipFile:
    """Open + validate an untrusted .idml as a ZIP. Never extracts to disk --
    every part is read straight into memory via ZipFile.read(), which removes
    path-traversal as a concern for the read path entirely (nothing is ever
    written under an attacker-chosen relative path)."""
    if not zipfile.is_zipfile(path):
        raise IdmlImportError("Il file non è un pacchetto IDML (ZIP) leggibile.")
    zf = zipfile.ZipFile(path, "r")
    try:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise IdmlImportError("Pacchetto IDML non valido: numero di file eccessivo.")
        total_uncompressed = 0
        for info in infos:
            pp = PurePosixPath(info.filename)
            if pp.is_absolute() or ".." in pp.parts:
                raise IdmlImportError("Pacchetto IDML non sicuro: percorso non consentito nell'archivio.")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise IdmlImportError("Pacchetto IDML non sicuro: collegamento simbolico rilevato.")
            size = max(0, int(info.file_size))
            if size > MAX_ENTRY_UNCOMPRESSED:
                raise IdmlImportError("Pacchetto IDML non valido: una voce supera la dimensione massima consentita.")
            comp = max(1, int(info.compress_size))
            if size > 1_000_000 and (size / comp) > MAX_COMPRESSION_RATIO:
                raise IdmlImportError("Pacchetto IDML non valido: rapporto di compressione sospetto.")
            total_uncompressed += size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
            raise IdmlImportError("Pacchetto IDML non valido: dimensione espansa complessiva eccessiva.")
        names = set(zf.namelist())
        if "mimetype" not in names:
            raise IdmlImportError("Pacchetto IDML non valido: parte 'mimetype' mancante.")
        if zf.read("mimetype").strip() != EXPECTED_MIMETYPE:
            raise IdmlImportError("Il file non è un pacchetto IDML valido (mimetype inatteso).")
        if "designmap.xml" not in names:
            raise IdmlImportError("Pacchetto IDML non valido: 'designmap.xml' mancante.")
    except IdmlImportError:
        zf.close()
        raise
    except Exception as exc:
        zf.close()
        raise IdmlImportError("Pacchetto IDML non leggibile o corrotto.") from exc
    return zf


_HOSTILE_XML_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _reject_hostile_xml(data: bytes, part_name: str) -> None:
    """xml.etree.ElementTree does not resolve EXTERNAL entities, but it does
    not disable DOCTYPE/internal-entity processing either -- an internal
    entity expansion ("billion laughs") needs no external resource at all.
    Explicit, textual, pre-parse rejection: fail-closed rather than relying on
    an assumption about parser internals."""
    if _HOSTILE_XML_RE.search(data):
        raise IdmlImportError(f"Pacchetto IDML non sicuro: costrutto XML non consentito (DOCTYPE/ENTITY) in {part_name}")


def _safe_xml(zf: zipfile.ZipFile, name: str, *, required: bool = True) -> ET.Element:
    try:
        data = zf.read(name)
    except KeyError as exc:
        if required:
            raise IdmlImportError(f"Parte del pacchetto IDML dichiarata ma mancante: {name}") from exc
        raise
    _reject_hostile_xml(data, name)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise IdmlImportError(f"XML strutturale non valido nel pacchetto IDML: {name}") from exc


def _designmap_refs(root: ET.Element) -> tuple[list[str], list[str], list[str]]:
    stories: list[str] = []
    spreads: list[str] = []
    masters: list[str] = []
    for el in root.iter():
        src = el.attrib.get("src")
        if not src:
            continue
        tag = _local(el.tag)
        if tag == "Story":
            stories.append(src)
        elif tag == "Spread":
            spreads.append(src)
        elif tag == "MasterSpread":
            masters.append(src)
    return stories, spreads, masters


def _item_transform_xy(el: ET.Element) -> tuple[float, float]:
    parts = (el.attrib.get("ItemTransform") or "").split()
    if len(parts) >= 6:
        try:
            return float(parts[4]), float(parts[5])
        except ValueError:
            pass
    return 0.0, 0.0


_HEADING_STYLE_RE = re.compile(r"head|title|titolo|chapter|capitolo", re.IGNORECASE)
_WARNING_STYLE_RE = re.compile(r"warning|avvertenza|danger|pericolo|caution|attenzione", re.IGNORECASE)
_NOTE_STYLE_RE = re.compile(r"\bnote\b|\bnota\b", re.IGNORECASE)
_CAPTION_STYLE_RE = re.compile(r"caption|didascalia", re.IGNORECASE)
_LIST_STYLE_RE = re.compile(r"list|elenco|bullet", re.IGNORECASE)

_FIGURE_SHAPE_TAGS = ("Rectangle", "Polygon", "Oval", "GraphicLine")
_FIGURE_CONTENT_TAGS = ("Image", "PDF", "EPS")


def _role_from_style(style_name: str) -> str:
    s = style_name or ""
    if _HEADING_STYLE_RE.search(s):
        return "heading"
    if _WARNING_STYLE_RE.search(s):
        return "warning"
    if _NOTE_STYLE_RE.search(s):
        return "note"
    if _CAPTION_STYLE_RE.search(s):
        return "caption"
    if _LIST_STYLE_RE.search(s):
        return "list_item"
    return "paragraph"


def _fingerprint_entry(*, heading: str, role: str, story_id: str | None, order_index: int,
                        context_prev: str, text_sample: str) -> str:
    """Milestone 3D constraint: content-hash identity (used for dedup/exact-
    reuse/unchanged-detection only, see parse_idml callers) is NOT the same as
    cross-revision identity. This composite, structured fingerprint -- never a
    single opaque score -- is attached to every emitted block's source_refs so
    a FUTURE change/revision-intelligence consumer can attempt confidence-based
    matching across two different .idml revisions. No such matching is
    implemented or attempted by this milestone."""
    payload = {
        "heading": heading, "role": role, "story_id": story_id or "",
        "order_index": order_index, "context_prev": (context_prev or "")[:120],
        "text_sample": (text_sample or "")[:120],
    }
    return "idml_fingerprint=" + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _paragraph_text(p: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(c.text or "" for c in p.iter("Content"))).strip()


def _story_preview_text(story_root: ET.Element, limit: int = 160) -> str:
    """Short, genuinely-readable preview of a story excluded from the primary
    flow (orphan/master) -- real document text, never a Story/Spread ID, so a
    non-technical user can recognize what it is without opening any JSON."""
    parts: list[str] = []
    total = 0
    for p in story_root.iter("ParagraphStyleRange"):
        t = _paragraph_text(p)
        if t:
            parts.append(t)
            total += len(t)
        if total >= limit:
            break
    text = " ".join(parts).strip()
    return (text[:limit] + "…") if len(text) > limit else (text or "(nessun testo leggibile)")


def _figure_from_shape(shape_el: ET.Element, *, anchored: bool) -> dict[str, Any] | None:
    link_el = shape_el.find(".//Link")
    has_content = any(shape_el.find(f".//{tag}") is not None for tag in _FIGURE_CONTENT_TAGS)
    if link_el is None and not has_content:
        return None  # a plain rectangle/line with no image content at all
    if link_el is not None:
        link_uri = link_el.attrib.get("LinkResourceURI") or None
        link_status = link_el.attrib.get("StoredState", "unknown")
        asset_source = "linked"
    else:
        # A shape with genuine image content but no Link element: real embedded
        # binary data may exist in some IDML variants, but this version does
        # not attempt to decode/extract it -- see docs/reports note in the
        # final report. Never fabricated as if it were imported.
        link_uri = None
        link_status = "unknown"
        asset_source = "embedded_unresolved"
    x, y = _item_transform_xy(shape_el)
    return {
        "self": shape_el.attrib.get("Self", ""),
        "asset_source": asset_source,
        "link_uri": link_uri,
        "link_status": link_status,
        "anchored": anchored,
        "x": x, "y": y,
    }


def _extract_table_rows(table_el: ET.Element) -> list[list[str]]:
    grid: dict[tuple[int, int], str] = {}
    fallback_rows: list[list[str]] = []
    max_row = max_col = -1
    name_re = re.compile(r"^(\d+):(\d+)$")
    for cell in table_el.iter("Cell"):
        text = _paragraph_text(cell)
        m = name_re.match(cell.attrib.get("Name", ""))
        if m:
            r, c = int(m.group(1)), int(m.group(2))
            grid[(r, c)] = text
            max_row = max(max_row, r)
            max_col = max(max_col, c)
        else:
            fallback_rows.append([text])
    rows: list[list[str]] = []
    if grid:
        rows = [[grid.get((r, c), "") for c in range(max_col + 1)] for r in range(max_row + 1)]
    rows.extend(fallback_rows)
    return rows


_STORY_KNOWN_CHILDREN = {"ParagraphStyleRange", "Table"}


def _story_items(story_root: ET.Element) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (items, unsupported_construct_tags) for ONE story, in the
    story's own document order -- this order is always reliable regardless of
    frame threading complexity, since it is simply the Story XML's own child
    sequence."""
    items: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for child in story_root:
        tag = _local(child.tag)
        if tag == "ParagraphStyleRange":
            style = child.attrib.get("AppliedParagraphStyle", "")
            text = _paragraph_text(child)
            anchored_figures = []
            for shape in child.iter():
                if shape is child or _local(shape.tag) not in _FIGURE_SHAPE_TAGS:
                    continue
                fig = _figure_from_shape(shape, anchored=True)
                if fig:
                    anchored_figures.append(fig)
            if text or anchored_figures:
                items.append({"kind": "paragraph", "text": text, "role": _role_from_style(style),
                              "anchored_figures": anchored_figures})
        elif tag == "Table":
            items.append({"kind": "table", "rows": _extract_table_rows(child)})
        else:
            unsupported.append(tag)
    return items, unsupported


@dataclass
class ParsedIdml:
    groups: list[dict[str, Any]]
    review_items: list[dict[str, Any]]
    figure_total: int
    missing_asset_total: int
    ambiguous_asset_total: int
    table_total: int
    headings: list[str]
    all_lines: list[str]
    metadata_title: str


def _metadata_title(zf: zipfile.ZipFile) -> str:
    # Best-effort only: XMP/RDF metadata is optional and cosmetic, never part
    # of fail-closed structural validation.
    if "META-INF/metadata.xml" not in zf.namelist():
        return ""
    data = zf.read("META-INF/metadata.xml")
    # Cosmetic/optional part -- but a hostile DOCTYPE/ENTITY here must still
    # fail closed (never a soft spot just because the part is non-essential).
    _reject_hostile_xml(data, "META-INF/metadata.xml")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    for el in root.iter():
        if _local(el.tag) == "title":
            text = "".join(t for t in el.itertext()).strip()
            if text:
                return text
    return ""


def _links_index(zf: zipfile.ZipFile) -> tuple[set[str], dict[str, list[str]]]:
    """(all Links/ part names, basename -> [full zip-internal paths]).
    A basename can legitimately collide across subfolders -- collapsing it to
    a single winner would silently pick an arbitrary file. See _resolve_link."""
    names = {n for n in zf.namelist() if n.startswith("Links/") and not n.endswith("/")}
    by_basename: dict[str, list[str]] = {}
    for n in names:
        by_basename.setdefault(PurePosixPath(n).name, []).append(n)
    return names, by_basename


def _resolve_link(link_uri: str | None, link_names: set[str], by_basename: dict[str, list[str]]) -> tuple[str | None, str]:
    """Resolve a Link's LinkResourceURI against what is physically present in
    the package. Returns (zip_asset_path_or_None, status) with status in
    {"present_in_package", "missing", "ambiguous"}.

    Preference order: an exact normalized-path match against the package
    (rare, but the most reliable signal when available) beats a basename-only
    match. If more than one Links/ entry shares the same basename, the asset
    is reported ambiguous -- never resolved arbitrarily to either candidate.
    """
    if not link_uri:
        return None, "missing"
    norm = link_uri.replace("\\", "/")
    lower = norm.lower()
    if lower.startswith("file:///"):
        norm = norm[8:]
    elif lower.startswith("file://"):
        norm = norm[7:]
    norm = norm.lstrip("/")
    if norm in link_names:
        return norm, "present_in_package"
    basename = PurePosixPath(norm).name
    candidates = by_basename.get(basename, [])
    if len(candidates) == 1:
        return candidates[0], "present_in_package"
    if len(candidates) > 1:
        return None, "ambiguous"
    return None, "missing"


def parse_idml(path: str | Path) -> ParsedIdml:
    """The single, fail-closed IDML parse used by BOTH the CCMS importer
    (authoring_ccms.import_idml_as_topics) and the 3C candidate builder
    (legacy_family_inference._idml_signals / detect_idml_language) -- one
    parser, never duplicated."""
    path = Path(path)
    zf = _validate_zip_safety(path)
    try:
        designmap_root = _safe_xml(zf, "designmap.xml")
        story_paths, spread_paths, master_paths = _designmap_refs(designmap_root)
        if not story_paths:
            raise IdmlImportError("Il pacchetto IDML non dichiara alcuna Story.")
        # Every declared part must exist and parse -- a single structurally
        # broken part fails the WHOLE import, before anything is built.
        story_roots = {p: _safe_xml(zf, p) for p in story_paths}
        spread_roots = [(p, _safe_xml(zf, p)) for p in spread_paths]
        master_roots = [(p, _safe_xml(zf, p)) for p in master_paths]

        link_names, link_names_by_basename = _links_index(zf)

        # ---- frame graph: Self -> frame record, separately for body/master ----
        frames: dict[str, dict[str, Any]] = {}
        duplicate_selfs: list[str] = []
        loose_figures: list[dict[str, Any]] = []  # non-anchored, frame-placed images

        def _index_spread(spread_index: int, root: ET.Element, is_master: bool) -> None:
            for tf in root.iter("TextFrame"):
                self_id = tf.attrib.get("Self", "")
                if self_id in frames:
                    duplicate_selfs.append(self_id)
                    continue
                x, y = _item_transform_xy(tf)
                frames[self_id] = {
                    "self": self_id,
                    "parent_story": tf.attrib.get("ParentStory", ""),
                    "prev": tf.attrib.get("PreviousTextFrame") or None,
                    "next": tf.attrib.get("NextTextFrame") or None,
                    "spread_index": spread_index,
                    "x": x, "y": y,
                    "is_master": is_master,
                }
            for shape in root.iter():
                if _local(shape.tag) not in _FIGURE_SHAPE_TAGS:
                    continue
                # Skip shapes that are themselves inside a TextFrame's paragraph
                # content (those are handled as anchored figures via _story_items).
                fig = _figure_from_shape(shape, anchored=False)
                if fig:
                    fig["spread_index"] = spread_index
                    fig["is_master"] = is_master
                    loose_figures.append(fig)

        for i, (_p, root) in enumerate(spread_roots):
            _index_spread(i, root, is_master=False)
        for i, (_p, root) in enumerate(master_roots):
            _index_spread(len(spread_roots) + i, root, is_master=True)

        # ---- group frames by story ----
        frames_by_story: dict[str, list[dict[str, Any]]] = {}
        for f in frames.values():
            frames_by_story.setdefault(f["parent_story"], []).append(f)

        review_items: list[dict[str, Any]] = []
        for self_id in duplicate_selfs:
            review_items.append({"reason": "duplicate_self_id", "detail": self_id, "text_preview": ""})

        events: list[dict[str, Any]] = []  # merged, sortable story/figure events

        for story_path, story_root in story_roots.items():
            story_self = story_root.attrib.get("Self", story_path)
            story_frames = frames_by_story.get(story_self, [])
            body_frames = [f for f in story_frames if not f["is_master"]]
            master_frames = [f for f in story_frames if f["is_master"]]

            if body_frames:
                # A Story's own paragraph sequence is already one flat, reliable
                # stream regardless of how many frames it threads through (that
                # is simply how IDML stories work) -- what matters for ordering
                # DIFFERENT stories against each other is only "where does this
                # story visually start". We deliberately use the geometrically
                # first frame among the story's own frames (by spread order,
                # then position) rather than walking PreviousTextFrame/
                # NextTextFrame: two different stories never interleave
                # paragraph-by-paragraph, so a per-story start position is
                # sufficient and more robust against a broken/cyclic thread
                # chain than a chain walk would be.
                start = min(body_frames, key=lambda f: (f["spread_index"], f["y"], f["x"]))
                order_key = (start["spread_index"], start["y"], start["x"])
                items, unsupported = _story_items(story_root)
                for tag in unsupported:
                    review_items.append({"reason": "unsupported_construct", "detail": f"{tag} in story {story_self}",
                                          "text_preview": f"Costrutto: {tag}"})
                events.append({"kind": "story", "order_key": order_key, "items": items, "story_id": story_self})
            elif master_frames:
                review_items.append({"reason": "master_spread_content", "detail": f"story {story_self}",
                                      "text_preview": _story_preview_text(story_root)})
            else:
                review_items.append({"reason": "orphan_story", "detail": f"story {story_self} non collegata ad alcun frame",
                                      "text_preview": _story_preview_text(story_root)})

        for fig in loose_figures:
            if fig["is_master"]:
                review_items.append({"reason": "master_spread_content", "detail": f"figura {fig.get('self','')}",
                                      "text_preview": "(figura di pagina master)"})
                continue
            events.append({"kind": "figure_event", "order_key": (fig["spread_index"], fig["y"], fig["x"]), "figure": fig})

        events.sort(key=lambda e: e["order_key"])

        # ---- build groups (same shape consumed by _write_topic_groups_to_repo) ----
        groups: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        section_no = 0
        headings: list[str] = []
        all_lines: list[str] = []
        figure_total = 0
        missing_asset_total = 0
        ambiguous_asset_total = 0
        table_total = 0
        order_index = 0
        prev_text_sample = ""

        def ensure_current() -> dict[str, Any]:
            nonlocal current, section_no
            if current is None:
                section_no += 1
                current = {"title": "Introduction", "items": [], "page": section_no}
            return current

        def resolve_figure(fig: dict[str, Any], *, page: int, story_id: str | None) -> dict[str, Any]:
            nonlocal missing_asset_total, ambiguous_asset_total, order_index, prev_text_sample
            link_uri = fig.get("link_uri")
            if fig["asset_source"] == "linked":
                zip_asset_path, asset_status = _resolve_link(link_uri, link_names, link_names_by_basename)
                if asset_status == "missing":
                    missing_asset_total += 1
                elif asset_status == "ambiguous":
                    ambiguous_asset_total += 1
                    basename = PurePosixPath(link_uri).name if link_uri else ""
                    review_items.append({
                        "reason": "ambiguous_asset",
                        "detail": f"link_uri={link_uri}; candidates={link_names_by_basename.get(basename, [])}",
                        "text_preview": f"Più file compatibili trovati per «{basename}»: nessuno scelto automaticamente.",
                    })
            else:
                zip_asset_path, asset_status = None, "embedded_unresolved"
                missing_asset_total += 1
            extra_refs = []
            if link_uri:
                extra_refs.append(f"idml_link_uri={link_uri}")
            extra_refs.append(f"idml_asset_status={asset_status}")
            extra_refs.append(f"idml_link_status={fig.get('link_status','unknown')}")
            order_index += 1
            extra_refs.append(_fingerprint_entry(
                heading=(current["title"] if current else ""), role="figure", story_id=story_id,
                order_index=order_index, context_prev=prev_text_sample, text_sample="",
            ))
            prev_text_sample = "[figure]"
            return {
                "kind": "figure", "page": page, "caption": "", "alt_text": "",
                # file_path stays "" here -- parse_idml() never touches the
                # filesystem beyond reading the .idml itself. When the asset is
                # present_in_package, _zip_asset_path names the exact part to
                # extract; materialize_figure_assets() (called by
                # authoring_ccms.import_idml_as_topics, which knows the
                # project's Sources directory) fills in file_path before the
                # CCMS write happens. Never fabricated otherwise.
                "file_path": "",
                "_zip_asset_path": zip_asset_path,
                "_asset_status": asset_status,
                "source_refs_extra": extra_refs,
            }

        for ev in events:
            if ev["kind"] == "figure_event":
                g = ensure_current()
                g["items"].append(resolve_figure(ev["figure"], page=g["page"], story_id=None))
                figure_total += 1
                continue
            # story event: walk its own items in order
            story_id = ev["story_id"]
            for it in ev["items"]:
                if it["kind"] == "table":
                    g = ensure_current()
                    table_index = sum(1 for x in g["items"] if x["kind"] == "table")
                    order_index += 1
                    table_sample = " | ".join(" ".join(r) for r in it["rows"][:1])
                    fp = _fingerprint_entry(heading=g["title"], role="table", story_id=story_id,
                                            order_index=order_index, context_prev=prev_text_sample,
                                            text_sample=table_sample)
                    g["items"].append({"kind": "table", "rows": it["rows"], "page": g["page"], "table_index": table_index,
                                        "source_refs_extra": [fp]})
                    table_total += 1
                    prev_text_sample = table_sample
                    continue
                # paragraph
                text, role = it["text"], it["role"]
                if role == "heading" and text:
                    if current and current["items"]:
                        groups.append(current)
                    section_no += 1
                    current = {"title": text[:180], "items": [], "page": section_no}
                    headings.append(text)
                    all_lines.append(text)
                    order_index += 1
                    prev_text_sample = text
                else:
                    if text:
                        g = ensure_current()
                        block_type = "paragraph" if role in ("caption",) else role
                        order_index += 1
                        fp = _fingerprint_entry(heading=g["title"], role=role, story_id=story_id,
                                                order_index=order_index, context_prev=prev_text_sample,
                                                text_sample=text)
                        # Milestone 3D constraint: proximity/style is a HEURISTIC,
                        # never a confirmed IDML link between a caption and a
                        # figure -- the paragraph is ALWAYS kept as its own text
                        # block (nothing ever disappears because of a layout
                        # guess); a caption-styled paragraph right after a figure
                        # additionally registers a caption_candidate marker on
                        # that figure (never the authoritative caption/alt_text
                        # field) plus a review item requiring human confirmation.
                        g["items"].append({"kind": "text", "text": text, "page": g["page"], "block_type": block_type,
                                            "source_refs_extra": [fp]})
                        if role == "caption" and len(g["items"]) >= 2 and g["items"][-2]["kind"] == "figure":
                            fig_item = g["items"][-2]
                            candidate_key = uuid4().hex[:12]
                            fig_item["source_refs_extra"].append(f"idml_caption_candidate={text}")
                            fig_item["source_refs_extra"].append(f"idml_caption_candidate_key={candidate_key}")
                            review_items.append({
                                "reason": "caption_candidate",
                                "detail": f"story {story_id}; candidate for figure at order_index={order_index}",
                                "text_preview": text,
                                "item_key": candidate_key,
                            })
                        all_lines.append(text)
                        prev_text_sample = text
                for fig in it.get("anchored_figures", []) or []:
                    g = ensure_current()
                    g["items"].append(resolve_figure(fig, page=g["page"], story_id=story_id))
                    figure_total += 1

        if current and current["items"]:
            groups.append(current)

        return ParsedIdml(
            groups=groups, review_items=review_items, figure_total=figure_total,
            missing_asset_total=missing_asset_total, ambiguous_asset_total=ambiguous_asset_total,
            table_total=table_total,
            headings=headings, all_lines=all_lines, metadata_title=_metadata_title(zf),
        )
    finally:
        zf.close()


def materialize_figure_assets(idml_path: str | Path, groups: list[dict[str, Any]], physical_dir: str | Path,
                              *, record_dir: str | Path | None = None) -> int:
    """Copy bytes for every figure item whose asset is present_in_package
    (the ONLY case where we have real, complete, correctly-named bytes sitting
    at a known location) into physical_dir, patching item["file_path"] in
    place. A missing/embedded_unresolved figure is left with file_path="" --
    never fabricated. Re-validates the package (cheap, already proven fast) so
    this second pass is fail-closed exactly like the first.

    record_dir (defaults to physical_dir) lets a caller write bytes into a
    STAGING location while recording the eventual FINAL path into the CCMS
    row -- see authoring_ccms.import_idml_as_topics's staging/rollback: the
    real assets_dir is only ever created by an atomic rename once the whole
    CCMS write has succeeded, so a failed import never leaves a half-populated
    assets directory at its final destination.

    Returns the number of assets actually copied.
    """
    physical_dir = Path(physical_dir)
    record_dir = Path(record_dir) if record_dir is not None else physical_dir
    zip_paths = {
        item["_zip_asset_path"]
        for group in groups for item in group["items"]
        if item.get("kind") == "figure" and item.get("_zip_asset_path")
    }
    if not zip_paths:
        return 0
    physical_dir.mkdir(parents=True, exist_ok=True)
    zf = _validate_zip_safety(Path(idml_path))
    copied = 0
    try:
        written: dict[str, str] = {}
        for zip_path in zip_paths:
            data = zf.read(zip_path)
            # Two legitimately distinct package entries (e.g. Links/A/image.png
            # and Links/B/image.png) can share a basename -- collapsing both to
            # physical_dir/image.png would silently overwrite one with the
            # other. Preserve the path relative to Links/ (already validated
            # traversal/symlink-safe by _validate_zip_safety, since zip_path
            # always comes from that same namelist) so every entry gets its
            # own, deterministically unique destination.
            rel = PurePosixPath(zip_path)
            try:
                rel = rel.relative_to("Links")
            except ValueError:
                rel = PurePosixPath(rel.name)
            rel_parts = rel.parts
            dest = physical_dir.joinpath(*rel_parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            written[zip_path] = str(Path(record_dir).joinpath(*rel_parts))
            copied += 1
    finally:
        zf.close()
    for group in groups:
        for item in group["items"]:
            if item.get("kind") == "figure" and item.get("_zip_asset_path") in written:
                item["file_path"] = written[item["_zip_asset_path"]]
    return copied


def extract_idml_signals(path: str | Path) -> tuple[list[str], list[str], str]:
    """(headings, all_lines, metadata_title) for the 3C candidate builder.
    Deliberately does NOT compute title_line/content_hint_line itself -- that
    logic (_content_hint_line/_first_meaningful_line) already exists in
    legacy_family_inference.py and is reused there unchanged, exactly like the
    PDF/DOCX signal functions already do."""
    parsed = parse_idml(path)
    return parsed.headings, parsed.all_lines, parsed.metadata_title


def detect_idml_language(path: str | Path) -> tuple[str, float, str]:
    """Same detection principle as detect_document_language/detect_docx_language
    -- reuses the SAME format-agnostic scoring/combination helpers, no new
    language-detection logic."""
    file_lang, file_conf = _filename_language(path)
    try:
        parsed = parse_idml(path)
        text = " ".join(parsed.all_lines).lower()
    except IdmlImportError:
        text = ""
    text_lang, text_conf = _score_text_language(text)
    return _combine_language_signals(file_lang, file_conf, text_lang, text_conf)
