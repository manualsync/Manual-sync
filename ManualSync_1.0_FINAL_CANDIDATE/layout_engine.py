from __future__ import annotations

import base64
import html
import io
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pandas as pd
from docx import Document as DocxDocument
from PIL import Image
from rapidfuzz.fuzz import ratio


@dataclass
class TextBlock:
    index: int
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    normalized: str
    cat_normalized: str
    technical_values: tuple[str, ...]
    codes: tuple[str, ...]
    page_refs: tuple[str, ...]
    section_ref: str
    heading: bool
    font_size: float


@dataclass
class TableBlock:
    index: int
    page: int
    bbox: tuple[float, float, float, float]
    rows: list[list[str]]


@dataclass
class ImageBlock:
    index: int
    page: int
    bbox: tuple[float, float, float, float]
    width: int
    height: int
    extension: str
    dhash: int
    data: bytes


@dataclass
class DocumentLayout:
    page_count: int
    selected_pages: list[int]
    text_blocks: list[TextBlock]
    tables: list[TableBlock]
    images: list[ImageBlock]


TECH_UNIT = r"%|°\s?[cf]|khz|mhz|hz|kw|nm|mm|cm|ms|bar|kg|v|a|w|s|m"
SECTION_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)(?:\s|$)")
PAGE_REF_RE = re.compile(r"\b(?:page|pages|pagina|pagine|pag\.?|p\.)\s*\d+(?:\s*[-–]\s*\d+)?\b", re.I)
ENGINE_BUILD = "0.5.0-hierarchical-20260805"


def parse_page_spec(spec: str, total_pages: int) -> list[int]:
    if not spec or not spec.strip():
        if total_pages > 30:
            raise ValueError(f"Il PDF contiene {total_pages} pagine. Indica un intervallo, per esempio 21-22.")
        return list(range(total_pages))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            if start > end:
                start, end = end, start
            pages.update(range(start - 1, end))
        else:
            pages.add(int(part) - 1)
    selected = sorted(p for p in pages if 0 <= p < total_pages)
    if not selected:
        raise ValueError("L'intervallo pagine non è valido per questo PDF.")
    return selected


def extract_section_ref(text: str) -> str:
    flattened = text.replace("\n", " ").strip()
    match = SECTION_RE.match(flattened)
    return match.group(1) if match else ""


def extract_page_refs(text: str) -> tuple[str, ...]:
    return tuple(m.group(0).strip() for m in PAGE_REF_RE.finditer(text))


def _canonical_frame_ranges(text: str) -> str:
    # Treat English and Italian frame-size ranges as the same structural range.
    text = re.sub(
        r"\bda\s+(AA|AB|AC|AD|A|B|C|D|E)\s+a\s+(AA|AB|AC|AD|A|B|C|D|E)\b",
        lambda m: f"{m.group(1)}-{m.group(2)}",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(AA|AB|AC|AD|A|B|C|D|E)\s*(?:\.\.\.|to)\s*(AA|AB|AC|AD|A|B|C|D|E)\b",
        lambda m: f"{m.group(1)}-{m.group(2)}",
        text,
        flags=re.I,
    )
    return text


def strip_structural_noise(text: str) -> str:
    text = text.replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = PAGE_REF_RE.sub(" ", text)
    text = re.sub(r"^\s*[•·▪‣-]\s*", "", text)
    # Leading section number is structural context, not a technical value.
    text = re.sub(r"^\s*\d+(?:\.\d+)+\s*", "", text.replace("\n", " "))
    # Navigation/TOC artefact such as "Mechanical installation 3".
    if re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ '\-/]{3,80}\s+\d{1,2}", text.strip()):
        text = re.sub(r"\s+\d{1,2}\s*$", "", text)
    return _canonical_frame_ranges(text)


def normalize_cat(text: str) -> str:
    """CAT-style normalization: keeps textual page/section references and wording."""
    text = text.replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl").lower()
    text = re.sub(r"^\s*[•·▪‣-]\s*", "", text.replace("\n", " "))
    for old, new in {"…": "...", "–": "-", "—": "-", "≥": ">=", "≤": "<=", "−": "-"}.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w%°./+<>=-]+", " ", text, flags=re.UNICODE)
    return text.strip()


def normalize(text: str) -> str:
    """Semantic comparison normalization, separate from CAT match calculation."""
    text = strip_structural_noise(text).lower()
    for old, new in {"…": "...", "–": "-", "—": "-", "≥": ">=", "≤": "<=", "−": "-"}.items():
        text = text.replace(old, new)
    text = re.sub(r"\binverter\b", "converter", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w%°./+<>=-]+", " ", text, flags=re.UNICODE)
    return text.strip()


def extract_technical_values(text: str) -> tuple[str, ...]:
    cleaned = PAGE_REF_RE.sub(" ", text.lower())
    cleaned = re.sub(r"^\s*\d+(?:\.\d+)+\s*", "", cleaned.replace("\n", " "))
    values: list[str] = []
    # Values with units are technical. Plain page/section numbers are deliberately excluded.
    unit_pattern = rf"(?<!\w)[+-]?\d+(?:[.,]\d+)*\s*(?:{TECH_UNIT})\b"
    values.extend(re.sub(r"\s+", "", v) for v in re.findall(unit_pattern, cleaned, re.I))
    # Counts linked to fasteners/components, e.g. 2 x M4 or 4 screws.
    for m in re.finditer(r"\b(\d+)\s*[x×]\s*(M\d+)\b", cleaned, re.I):
        values.append(f"{m.group(1)}x{m.group(2).upper()}")
    for m in re.finditer(r"\b(\d+)\s+(?:screws?|viti|bolts?|bulloni|nuts?|dadi|washers?|rondelle)\b", cleaned, re.I):
        values.append(f"count:{m.group(1)}")
    return tuple(dict.fromkeys(values))


def _expand_alpha_range(start: str, end: str) -> list[str]:
    order = ["AA", "AB", "AC", "AD", "A", "B", "C", "D", "E", "F"]
    try:
        i, j = order.index(start), order.index(end)
        if i <= j:
            return order[i:j + 1]
    except ValueError:
        pass
    return [start, end]


def extract_codes(text: str) -> tuple[str, ...]:
    out = re.findall(r"\b(?:P\d{3,5}(?:\[[^\]]+\])?|R\d{3,5}|[A-Z]{2,8}[-_]\d{2,}[A-Z0-9._/-]*|M\d+)\b", text, re.I)
    lower = text.lower()
    if "frame size" in lower or "grandezz" in lower:
        upper = _canonical_frame_ranges(text.upper().replace("…", "...")).replace("DA ", "")
        for start, end in re.findall(r"\b(AA|AB|AC|AD|A|B|C|D|E)-(AA|AB|AC|AD|A|B|C|D|E)\b", upper):
            out.extend(_expand_alpha_range(start, end))
        # AA/AB and comma-separated explicit sizes.
        for token in re.findall(r"\b(?:AA|AB|AC|AD|A|B|C|D|E)\b", upper):
            out.append(token)
    return tuple(dict.fromkeys(x.upper() for x in out))


def bbox_overlap_ratio(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1]); x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    return inter / area


def dhash_bytes(data: bytes, size: int = 16) -> int:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("L").resize((size + 1, size))
        pixels = list(im.getdata())
    value = 0
    for y in range(size):
        row = pixels[y * (size + 1):(y + 1) * (size + 1)]
        for x in range(size):
            value = (value << 1) | int(row[x] > row[x + 1])
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def extract_layout(pdf_path: str | Path, page_spec: str = "") -> DocumentLayout:
    path = Path(pdf_path)
    doc = fitz.open(str(path))
    selected = parse_page_spec(page_spec, len(doc))
    text_blocks: list[TextBlock] = []
    tables: list[TableBlock] = []
    images: list[ImageBlock] = []

    for rel_page, page_index in enumerate(selected):
        page = doc[page_index]
        page_height = page.rect.height
        page_tables = []
        try:
            page_tables = page.find_tables().tables
        except Exception:
            page_tables = []

        table_bboxes: list[tuple[float, float, float, float]] = []
        for table in page_tables:
            rows = [[(cell or "").strip() for cell in row] for row in table.extract()]
            rows = [row for row in rows if any(cell for cell in row)]
            if not rows:
                continue
            bbox = tuple(float(v) for v in table.bbox)
            table_bboxes.append(bbox)
            tables.append(TableBlock(len(tables), rel_page, bbox, rows))

        page_dict = page.get_text("dict", sort=True)
        body_sizes: list[float] = []
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        body_sizes.append(float(span.get("size", 0.0)))
        median_size = sorted(body_sizes)[len(body_sizes) // 2] if body_sizes else 9.0

        for block in page_dict.get("blocks", []):
            block_type = block.get("type")
            bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
            if block_type == 1 and block.get("image"):
                try:
                    data = bytes(block["image"])
                    images.append(ImageBlock(len(images), rel_page, bbox, int(block.get("width", 0)), int(block.get("height", 0)), str(block.get("ext", "png")), dhash_bytes(data), data))
                except Exception:
                    pass
                continue
            if block_type != 0:
                continue
            lines: list[str] = []; sizes: list[float] = []; fonts: list[str] = []
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = "".join(span.get("text", "") for span in spans).strip()
                if line_text:
                    lines.append(line_text)
                for span in spans:
                    sizes.append(float(span.get("size", 0.0))); fonts.append(str(span.get("font", "")))
            text = "\n".join(lines).strip()
            if not text:
                continue
            max_size = max(sizes) if sizes else median_size
            if bbox[1] < 25 and max_size <= median_size * 1.08:
                continue
            if bbox[1] > page_height - 110:
                continue
            if re.fullmatch(r"\d+", text.strip()):
                continue
            if any(bbox_overlap_ratio(bbox, tb) > 0.35 for tb in table_bboxes):
                continue
            is_bold = any("bold" in f.lower() for f in fonts)
            short = len(text) <= 120 and len(lines) <= 3
            heading = short and (max_size >= median_size * 1.12 or is_bold or bool(SECTION_RE.match(text.replace("\n", " "))))
            text_blocks.append(TextBlock(
                len(text_blocks), rel_page, bbox, text, normalize(text), normalize_cat(text), extract_technical_values(text),
                extract_codes(text), extract_page_refs(text), extract_section_ref(text), heading, round(max_size, 2)
            ))

    # Remove duplicated running headers. A top-margin block containing the same section id as a later heading is navigation chrome.
    remove_ids: set[int] = set()
    by_page: dict[int, list[TextBlock]] = {}
    for block in text_blocks:
        by_page.setdefault(block.page, []).append(block)
    for blocks in by_page.values():
        for block in blocks:
            if block.bbox[1] >= 75:
                continue
            for later in blocks:
                if later.bbox[1] <= max(75, block.bbox[1] + 12):
                    continue
                same_section = bool(block.section_ref and block.section_ref == later.section_ref)
                same_text = ratio(block.normalized, later.normalized) >= 88
                contains_later = bool(later.normalized and later.normalized in block.normalized)
                chapter_running_header = bool(not block.section_ref and len(block.normalized) < 80 and later.heading and later.section_ref)
                if same_section or same_text or contains_later or chapter_running_header:
                    remove_ids.add(block.index)
                    break
    text_blocks = [b for b in text_blocks if b.index not in remove_ids]
    for idx, block in enumerate(text_blocks):
        block.index = idx
    page_count = len(doc)
    doc.close()
    return DocumentLayout(page_count, selected, text_blocks, tables, images)


def trim_layout_from_anchor(layout: DocumentLayout, anchor: str = "") -> DocumentLayout:
    if not anchor or not anchor.strip():
        return layout
    target = anchor.strip()
    candidates = [b for b in layout.text_blocks if b.section_ref == target]
    if not candidates:
        return layout
    first = min(candidates, key=lambda b: (b.page, b.bbox[1]))
    def after(page: int, y: float) -> bool:
        return page > first.page or (page == first.page and y >= first.bbox[1] - 1)
    texts = [b for b in layout.text_blocks if after(b.page, b.bbox[1])]
    tables = [t for t in layout.tables if after(t.page, t.bbox[1])]
    images = [i for i in layout.images if after(i.page, i.bbox[1])]
    for idx, b in enumerate(texts): b.index = idx
    for idx, t in enumerate(tables): t.index = idx
    for idx, i in enumerate(images): i.index = idx
    return DocumentLayout(layout.page_count, layout.selected_pages, texts, tables, images)


# CP3.4: the structural translation-alignment helpers moved, verbatim, to translation_alignment
# (pure: no PDF, no I/O). Imported here, at the first helper's original position, so that
# every function of this module keeps resolving them exactly as before.
from translation_alignment import _set_overlap, _section_groups, translation_pair_score, _monotonic_generic


def text_pair_score(old: TextBlock, new: TextBlock, old_count: int, new_count: int) -> float:
    """Score used only after deterministic exact/semantic anchors have been fixed."""
    if old.cat_normalized and old.cat_normalized == new.cat_normalized:
        return 1.0
    if old.normalized and old.normalized == new.normalized:
        return 0.995

    cat_lexical = ratio(old.cat_normalized, new.cat_normalized) / 100.0
    semantic_lexical = ratio(old.normalized, new.normalized) / 100.0
    lexical = max(cat_lexical, semantic_lexical)

    score = lexical * 0.78
    score += 0.10 if old.heading == new.heading else -0.22

    if old.section_ref and new.section_ref:
        score += 0.18 if old.section_ref == new.section_ref else -0.58
    elif bool(old.section_ref) != bool(new.section_ref):
        score -= 0.10

    score += 0.05 * _set_overlap(old.technical_values, new.technical_values)
    score += 0.08 * _set_overlap(old.codes, new.codes)

    # Progress is only a weak tie-breaker. It must never override an exact text match.
    progress_old = old.index / max(1, old_count - 1)
    progress_new = new.index / max(1, new_count - 1)
    score += 0.04 * max(0.0, 1 - abs(progress_old - progress_new))
    return max(0.0, min(1.0, score))


def _lcs_pairs(old: list[TextBlock], new: list[TextBlock], key_fn) -> list[tuple[int, int]]:
    """Stable longest-common-subsequence anchors for deterministic matches."""
    m, n = len(old), len(new)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        old_key = key_fn(old[i])
        for j in range(n - 1, -1, -1):
            if old_key and old_key == key_fn(new[j]):
                dp[i][j] = 1 + dp[i + 1][j + 1]
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < m and j < n:
        if key_fn(old[i]) and key_fn(old[i]) == key_fn(new[j]):
            pairs.append((i, j)); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _gap_ranges(anchors: list[tuple[int, int]], old_count: int, new_count: int):
    previous_old = previous_new = -1
    for old_idx, new_idx in anchors + [(old_count, new_count)]:
        yield previous_old + 1, old_idx, previous_new + 1, new_idx
        previous_old, previous_new = old_idx, new_idx


def _align_gap(
    old: list[TextBlock],
    new: list[TextBlock],
    old_offset: int,
    new_offset: int,
    minimum_score: float = 0.56,
    gap_cost: float = 0.31,
) -> dict[int, tuple[int, float, str]]:
    """Needleman-Wunsch alignment inside one gap bounded by deterministic anchors."""
    m, n = len(old), len(new)
    if not m or not n:
        return {}
    dp = [[0.0] * (n + 1) for _ in range(m + 1)]
    choice = [[""] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dp[i][0] = dp[i - 1][0] - gap_cost; choice[i][0] = "D"
    for j in range(1, n + 1):
        dp[0][j] = dp[0][j - 1] - gap_cost; choice[0][j] = "I"
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            score = text_pair_score(old[i - 1], new[j - 1], m, n)
            match_gain = (score * 1.35) if score >= minimum_score else -0.90
            options = {
                "M": dp[i - 1][j - 1] + match_gain,
                "D": dp[i - 1][j] - gap_cost,
                "I": dp[i][j - 1] - gap_cost,
            }
            # Stable tie break: match > deletion > insertion.
            best = max(("M", "D", "I"), key=lambda k: (options[k], {"M": 2, "D": 1, "I": 0}[k]))
            dp[i][j] = options[best]; choice[i][j] = best
    result: dict[int, tuple[int, float, str]] = {}
    i, j = m, n
    while i > 0 or j > 0:
        c = choice[i][j]
        if c == "M":
            score = text_pair_score(old[i - 1], new[j - 1], m, n)
            if score >= minimum_score:
                result[new_offset + j - 1] = (old_offset + i - 1, score, "SEQUENTIAL_FUZZY")
            i -= 1; j -= 1
        elif c == "D":
            i -= 1
        else:
            j -= 1
    return result


def hierarchical_align_text(old: list[TextBlock], new: list[TextBlock]) -> tuple[dict[int, tuple[int, float, str]], list[int]]:
    """
    Alignment hierarchy:
    1) exact CAT-normalized anchors;
    2) semantic-exact anchors inside the remaining gaps;
    3) order-preserving fuzzy alignment only inside the residual gaps.
    """
    mapping: dict[int, tuple[int, float, str]] = {}

    cat_pairs = _lcs_pairs(old, new, lambda b: b.cat_normalized)
    for oi, ni in cat_pairs:
        mapping[ni] = (oi, 1.0, "CAT_EXACT")

    # Add semantic-exact anchors only inside CAT gaps, preserving global order.
    semantic_pairs: list[tuple[int, int]] = []
    for os, oe, ns, ne in _gap_ranges(cat_pairs, len(old), len(new)):
        local = _lcs_pairs(old[os:oe], new[ns:ne], lambda b: b.normalized)
        semantic_pairs.extend((os + oi, ns + ni) for oi, ni in local)
    for oi, ni in semantic_pairs:
        mapping[ni] = (oi, 0.995, "SEMANTIC_EXACT")

    all_anchors = sorted((oi, ni) for ni, (oi, _, _) in mapping.items())
    for os, oe, ns, ne in _gap_ranges(all_anchors, len(old), len(new)):
        mapping.update(_align_gap(old[os:oe], new[ns:ne], os, ns))

    used_old = {oi for oi, _, _ in mapping.values()}
    deleted = sorted(set(range(len(old))) - used_old)
    return mapping, deleted


def monotonic_align(old: list, new: list, score_fn, minimum_score: float, gap_cost: float = 0.30) -> tuple[dict[int, tuple[int, float]], list[int]]:
    """Backward-compatible generic alignment used by tables and translation mapping."""
    return _monotonic_generic(old, new, score_fn, minimum_score, gap_cost)


def map_translation_blocks(old_source: list[TextBlock], old_translation: list[TextBlock]) -> dict[int, tuple[str, float, int]]:
    if not old_source or not old_translation:
        return {}
    source_groups = _section_groups(old_source); target_groups = _section_groups(old_translation)
    result: dict[int, tuple[str, float, int]] = {}
    for section, source_group in source_groups.items():
        target_group = target_groups.get(section, [])
        if not target_group:
            continue
        mapping, _ = _monotonic_generic(source_group, target_group, translation_pair_score, minimum_score=0.34, gap_cost=0.28)
        for local_target_idx, (local_source_idx, score) in mapping.items():
            source_block = source_group[local_source_idx]; target_block = target_group[local_target_idx]
            confidence = min(100.0, round(score * 100, 1))
            result[source_block.index] = (target_block.text, confidence, target_block.index)
    return result


def match_category(
    old: TextBlock,
    new: TextBlock,
    mapping: dict[int, tuple[int, float, str]],
    new_index: int,
    old_blocks: list[TextBlock],
    new_blocks: list[TextBlock],
) -> tuple[str, float]:
    lexical = min(100.0, round(ratio(old.cat_normalized, new.cat_normalized), 1))
    if old.cat_normalized == new.cat_normalized:
        previous_is_same_context = False
        if new_index > 0 and new_index - 1 in mapping:
            previous_old, _, previous_method = mapping[new_index - 1]
            previous_is_same_context = (
                previous_old == old.index - 1
                and previous_method == "CAT_EXACT"
                and old_blocks[previous_old].cat_normalized == new_blocks[new_index - 1].cat_normalized
            )
        return ("CONTEXT MATCH" if previous_is_same_context else "100% MATCH"), 100.0
    if lexical >= 95: return "FUZZY 95-99", lexical
    if lexical >= 85: return "FUZZY 85-94", lexical
    if lexical >= 75: return "FUZZY 75-84", lexical
    return "FUZZY <75", lexical


def classify_change(old: TextBlock, new: TextBlock) -> tuple[str, str, str]:
    same_values = set(old.technical_values) == set(new.technical_values)
    same_codes = set(old.codes) == set(new.codes)
    same_technical = same_values and same_codes

    if old.cat_normalized == new.cat_normalized and same_technical:
        if old.page_refs != new.page_refs or old.section_ref != new.section_ref:
            return "RIFERIMENTO AGGIORNATO", "BASSO", "RIUSA TESTO; AGGIORNA RIFERIMENTO"
        return "INVARIATO", "BASSO", "RIUSA SENZA RITRADURRE"

    if old.normalized == new.normalized and same_technical:
        if old.page_refs != new.page_refs:
            return "RIFERIMENTO AGGIORNATO", "BASSO", "RIUSA TESTO; AGGIORNA RIFERIMENTO PAGINA"
        return "VARIAZIONE TERMINOLOGICA", "MEDIO", "PROPONI RIUSO; VERIFICA TERMINOLOGIA"

    if old.page_refs != new.page_refs and old.normalized == new.normalized:
        return "RIFERIMENTO AGGIORNATO", "BASSO", "RIUSA TESTO; AGGIORNA RIFERIMENTO PAGINA"

    if not same_technical:
        if (old.heading or new.heading) and old.section_ref and old.section_ref == new.section_ref and same_values:
            return "CONTESTO TECNICO MODIFICATO", "ALTO", "BLOCCA; VERIFICA SPOSTAMENTO/VARIANTI"
        return "MODIFICA TECNICA", "ALTO", "BLOCCA, AGGIORNA E REVISIONA"

    lexical = ratio(old.cat_normalized, new.cat_normalized) / 100.0
    if old.page_refs != new.page_refs and ratio(old.normalized, new.normalized) >= 98:
        return "RIFERIMENTO AGGIORNATO", "BASSO", "RIUSA TESTO; AGGIORNA RIFERIMENTO PAGINA"
    if lexical >= 0.88:
        return "RIFORMULATO", "MEDIO", "PROPONI RIUSO, CONTROLLO RAPIDO"
    return "MODIFICATO", "MEDIO", "RITRADUCI / REVISIONA"


def compare_text(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    old, new = old_layout.text_blocks, new_layout.text_blocks
    mapping, deleted = hierarchical_align_text(old, new)
    translations = map_translation_blocks(old, old_translation_layout.text_blocks)
    rows: list[dict] = []
    for n in new:
        if n.index not in mapping:
            rows.append({
                "indice_vecchio": "", "indice_nuovo": n.index, "metodo_abbinamento": "UNMATCHED",
                "stato": "NUOVO", "rischio": "ALTO" if n.technical_values or n.codes else "MEDIO",
                "categoria_match": "NEW", "similarita_linguistica": 0.0, "affidabilita_abbinamento": 0.0,
                "pagina_vecchia": "", "pagina_nuova": n.page + 1, "vecchio_originale": "", "nuovo_originale": n.text,
                "vecchia_traduzione": "", "affidabilita_traduzione": 0.0,
                "valori_tecnici_vecchi": "", "valori_tecnici_nuovi": ", ".join(n.technical_values),
                "codici_vecchi": "", "codici_nuovi": ", ".join(n.codes), "riferimenti_vecchi": "", "riferimenti_nuovi": ", ".join(n.page_refs),
                "azione": "DA TRADURRE E REVISIONARE",
            })
            continue
        oi, align_score, method = mapping[n.index]; o = old[oi]
        status, risk, action = classify_change(o, n)
        category, lexical = match_category(o, n, mapping, n.index, old, new)
        tr_text, tr_conf, _ = translations.get(oi, ("", 0.0, -1))
        if tr_conf < 60:
            tr_text = ""
            action += "; ALLINEAMENTO TRADUZIONE DA VERIFICARE"
        rows.append({
            "indice_vecchio": oi, "indice_nuovo": n.index, "metodo_abbinamento": method,
            "stato": status, "rischio": risk, "categoria_match": category, "similarita_linguistica": lexical,
            "affidabilita_abbinamento": min(100.0, round(align_score * 100, 1)),
            "pagina_vecchia": o.page + 1, "pagina_nuova": n.page + 1, "vecchio_originale": o.text, "nuovo_originale": n.text,
            "vecchia_traduzione": tr_text, "affidabilita_traduzione": tr_conf,
            "valori_tecnici_vecchi": ", ".join(o.technical_values), "valori_tecnici_nuovi": ", ".join(n.technical_values),
            "codici_vecchi": ", ".join(o.codes), "codici_nuovi": ", ".join(n.codes),
            "riferimenti_vecchi": ", ".join(o.page_refs), "riferimenti_nuovi": ", ".join(n.page_refs), "azione": action,
        })
    for oi in deleted:
        o = old[oi]; tr_text, tr_conf, _ = translations.get(oi, ("", 0.0, -1))
        rows.append({
            "indice_vecchio": oi, "indice_nuovo": "", "metodo_abbinamento": "UNMATCHED",
            "stato": "ELIMINATO", "rischio": "MEDIO", "categoria_match": "DELETED", "similarita_linguistica": 0.0, "affidabilita_abbinamento": 0.0,
            "pagina_vecchia": o.page + 1, "pagina_nuova": "", "vecchio_originale": o.text, "nuovo_originale": "",
            "vecchia_traduzione": tr_text if tr_conf >= 60 else "", "affidabilita_traduzione": tr_conf,
            "valori_tecnici_vecchi": ", ".join(o.technical_values), "valori_tecnici_nuovi": "", "codici_vecchi": ", ".join(o.codes), "codici_nuovi": "",
            "riferimenti_vecchi": ", ".join(o.page_refs), "riferimenti_nuovi": "", "azione": "NON RIPORTARE",
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["similarita_linguistica"] = df["similarita_linguistica"].clip(lower=0, upper=100)
        df["affidabilita_abbinamento"] = df["affidabilita_abbinamento"].clip(lower=0, upper=100)
    return df



def row_text(row: list[str]) -> str:
    return " | ".join(cell.strip() for cell in row if cell.strip())


def row_concept(label: str) -> str:
    s = normalize(label)
    if re.search(r"\btop\b|in alto|parte superiore", s): return "top"
    if re.search(r"\bbottom\b|in basso|parte inferiore", s): return "bottom"
    if re.search(r"side|lateral|laterale|vista laterale", s): return "lateral"
    if "frame size" in s or "grandezza costruttiva" in s: return "frame"
    if "dimension" in s or "dimensioni" in s: return "dimensions"
    return "other"


def table_signature(table: TableBlock) -> str:
    return " | ".join(row_text(row) for row in table.rows[:2])


def table_pair_score(a: TableBlock, b: TableBlock, old_count: int, new_count: int) -> float:
    lexical = ratio(normalize(table_signature(a)), normalize(table_signature(b))) / 100.0
    progress = max(0.0, 1 - abs(a.index / max(1, old_count - 1) - b.index / max(1, new_count - 1)))
    return min(1.0, lexical * 0.78 + progress * 0.22)


def align_tables(old: list[TableBlock], new: list[TableBlock]) -> tuple[dict[int, tuple[int, float]], list[int]]:
    return monotonic_align(old, new, table_pair_score, minimum_score=0.28, gap_cost=0.35)


def row_pair_score(o: list[str], n: list[str], old_count: int, new_count: int, oi: int = 0, ni: int = 0) -> float:
    otext, ntext = row_text(o), row_text(n)
    lex = ratio(normalize(otext), normalize(ntext)) / 100.0
    label = ratio(normalize(o[0] if o else ""), normalize(n[0] if n else "")) / 100.0
    tech = _set_overlap(extract_technical_values(otext), extract_technical_values(ntext))
    codes = _set_overlap(extract_codes(otext), extract_codes(ntext))
    oc, nc = row_concept(o[0] if o else ""), row_concept(n[0] if n else "")
    concept = 1.0 if oc == nc and oc != "other" else 0.0
    return min(1.0, lex * 0.35 + label * 0.25 + tech * 0.12 + codes * 0.13 + concept * 0.25)


def align_rows(old_rows: list[list[str]], new_rows: list[list[str]]) -> tuple[dict[int, tuple[int, float]], list[int]]:
    def fn(o, n, oc, nc):
        return row_pair_score(o, n, oc, nc)
    return monotonic_align(old_rows, new_rows, fn, minimum_score=0.28, gap_cost=0.34)


def classify_table_change(old_text: str, new_text: str, old_label: str, new_label: str) -> tuple[str, str, str]:
    old_norm, new_norm = normalize(old_text), normalize(new_text)
    old_values, new_values = set(extract_technical_values(old_text)), set(extract_technical_values(new_text))
    old_codes, new_codes = set(extract_codes(old_text)), set(extract_codes(new_text))
    if old_norm == new_norm and old_values == new_values and old_codes == new_codes:
        return "INVARIATO", "BASSO", "RIUSA SENZA RITRADURRE"
    if old_values != new_values or old_codes != new_codes:
        return "MODIFICA TECNICA", "ALTO", "BLOCCA, AGGIORNA E REVISIONA"
    if row_concept(old_label) == row_concept(new_label) and row_concept(old_label) != "other":
        return "RIFORMULATO", "MEDIO", "PROPONI RIUSO, CONTROLLO RAPIDO"
    if ratio(old_norm, new_norm) >= 88:
        return "RIFORMULATO", "MEDIO", "PROPONI RIUSO, CONTROLLO RAPIDO"
    return "MODIFICATO", "MEDIO", "RITRADUCI / REVISIONA"


def map_translation_tables(old_source: list[TableBlock], old_translation: list[TableBlock]) -> dict[tuple[int, int], tuple[str, float]]:
    result: dict[tuple[int, int], tuple[str, float]] = {}
    table_mapping, _ = align_tables(old_source, old_translation)
    for target_ti, (source_ti, table_score) in table_mapping.items():
        source_table, target_table = old_source[source_ti], old_translation[target_ti]
        row_mapping, _ = align_rows(source_table.rows, target_table.rows)
        for target_ri, (source_ri, row_score) in row_mapping.items():
            confidence = min(100.0, round((0.45 * table_score + 0.55 * row_score) * 100, 1))
            result[(source_ti, source_ri)] = (row_text(target_table.rows[target_ri]), confidence)
    return result


def compare_tables(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    old, new = old_layout.tables, new_layout.tables
    mapping, deleted_tables = align_tables(old, new)
    translations = map_translation_tables(old, old_translation_layout.tables)
    rows: list[dict] = []
    for new_table in new:
        if new_table.index not in mapping:
            for new_ri, new_row in enumerate(new_table.rows):
                text = row_text(new_row)
                rows.append({"tabella_vecchia": "", "tabella_nuova": new_table.index + 1, "riga": new_ri + 1, "stato": "NUOVO", "rischio": "ALTO" if extract_technical_values(text) or extract_codes(text) else "MEDIO", "vecchia_riga": "", "nuova_riga": text, "vecchia_traduzione": "", "affidabilita_traduzione": 0.0, "valori_tecnici_vecchi": "", "valori_tecnici_nuovi": ", ".join(extract_technical_values(text)), "codici_vecchi": "", "codici_nuovi": ", ".join(extract_codes(text)), "azione": "TRADUCI E REVISIONA"})
            continue
        old_ti, _ = mapping[new_table.index]; old_table = old[old_ti]
        row_mapping, deleted_rows = align_rows(old_table.rows, new_table.rows)
        for new_ri, new_row in enumerate(new_table.rows):
            new_text = row_text(new_row)
            if new_ri not in row_mapping:
                rows.append({"tabella_vecchia": old_ti + 1, "tabella_nuova": new_table.index + 1, "riga": new_ri + 1, "stato": "NUOVO", "rischio": "ALTO" if extract_technical_values(new_text) or extract_codes(new_text) else "MEDIO", "vecchia_riga": "", "nuova_riga": new_text, "vecchia_traduzione": "", "affidabilita_traduzione": 0.0, "valori_tecnici_vecchi": "", "valori_tecnici_nuovi": ", ".join(extract_technical_values(new_text)), "codici_vecchi": "", "codici_nuovi": ", ".join(extract_codes(new_text)), "azione": "TRADUCI E REVISIONA"})
                continue
            old_ri, _ = row_mapping[new_ri]; old_text = row_text(old_table.rows[old_ri])
            status, risk, action = classify_table_change(old_text, new_text, old_table.rows[old_ri][0] if old_table.rows[old_ri] else "", new_row[0] if new_row else "")
            tr_text, tr_conf = translations.get((old_ti, old_ri), ("", 0.0))
            if tr_conf < 55:
                tr_text = ""; action += "; ALLINEAMENTO TRADUZIONE DA VERIFICARE"
            rows.append({"tabella_vecchia": old_ti + 1, "tabella_nuova": new_table.index + 1, "riga": new_ri + 1, "stato": status, "rischio": risk, "vecchia_riga": old_text, "nuova_riga": new_text, "vecchia_traduzione": tr_text, "affidabilita_traduzione": tr_conf, "valori_tecnici_vecchi": ", ".join(extract_technical_values(old_text)), "valori_tecnici_nuovi": ", ".join(extract_technical_values(new_text)), "codici_vecchi": ", ".join(extract_codes(old_text)), "codici_nuovi": ", ".join(extract_codes(new_text)), "azione": action})
        for old_ri in deleted_rows:
            old_text = row_text(old_table.rows[old_ri]); tr_text, tr_conf = translations.get((old_ti, old_ri), ("", 0.0))
            rows.append({"tabella_vecchia": old_ti + 1, "tabella_nuova": new_table.index + 1, "riga": "", "stato": "ELIMINATO", "rischio": "MEDIO", "vecchia_riga": old_text, "nuova_riga": "", "vecchia_traduzione": tr_text if tr_conf >= 55 else "", "affidabilita_traduzione": tr_conf, "valori_tecnici_vecchi": ", ".join(extract_technical_values(old_text)), "valori_tecnici_nuovi": "", "codici_vecchi": ", ".join(extract_codes(old_text)), "codici_nuovi": "", "azione": "NON RIPORTARE"})
    for old_ti in deleted_tables:
        for old_ri, old_row in enumerate(old[old_ti].rows):
            old_text = row_text(old_row); tr_text, tr_conf = translations.get((old_ti, old_ri), ("", 0.0))
            rows.append({"tabella_vecchia": old_ti + 1, "tabella_nuova": "", "riga": "", "stato": "ELIMINATO", "rischio": "MEDIO", "vecchia_riga": old_text, "nuova_riga": "", "vecchia_traduzione": tr_text if tr_conf >= 55 else "", "affidabilita_traduzione": tr_conf, "valori_tecnici_vecchi": ", ".join(extract_technical_values(old_text)), "valori_tecnici_nuovi": "", "codici_vecchi": ", ".join(extract_codes(old_text)), "codici_nuovi": "", "azione": "NON RIPORTARE"})
    return pd.DataFrame(rows)


def compare_images(old_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    old, new = old_layout.images, new_layout.images
    candidates: list[tuple[float, int, int, int]] = []
    for o in old:
        for n in new:
            distance = hamming(o.dhash, n.dhash)
            ar_o = o.width / max(1, o.height); ar_n = n.width / max(1, n.height)
            score = distance + abs(math.log(max(0.01, ar_o) / max(0.01, ar_n))) * 35
            candidates.append((score, o.index, n.index, distance))
    candidates.sort(); used_o: set[int] = set(); used_n: set[int] = set(); mapping: dict[int, tuple[int, float, int]] = {}
    for score, oi, ni, distance in candidates:
        if score > 48 or oi in used_o or ni in used_n: continue
        used_o.add(oi); used_n.add(ni); mapping[ni] = (oi, score, distance)
    rows: list[dict] = []
    for n in new:
        if n.index not in mapping:
            rows.append({"stato": "NUOVA FIGURA", "rischio": "MEDIO", "pagina_vecchia": "", "pagina_nuova": n.page + 1, "distanza_hash": "", "dimensioni_vecchie": "", "dimensioni_nuove": f"{n.width}x{n.height}", "posizione_vecchia": "", "posizione_nuova": ", ".join(f"{v:.1f}" for v in n.bbox), "azione": "VERIFICA, DIDASCALIA E TRADUZIONE", "vecchia_immagine": "", "nuova_immagine": base64.b64encode(n.data).decode("ascii"), "estensione_vecchia": "", "estensione_nuova": n.extension})
            continue
        oi, _, distance = mapping[n.index]; o = old[oi]
        same_visual = distance <= 8
        same_page = o.page == n.page
        old_center = ((o.bbox[0] + o.bbox[2]) / 2, (o.bbox[1] + o.bbox[3]) / 2)
        new_center = ((n.bbox[0] + n.bbox[2]) / 2, (n.bbox[1] + n.bbox[3]) / 2)
        movement = math.dist(old_center, new_center)
        if same_visual and same_page and movement <= 45:
            status, risk, action = "FIGURA INVARIATA", "BASSO", "RIUSA"
        elif same_visual:
            status, risk, action = "FIGURA RICOLLOCATA", "BASSO", "RIUSA; VERIFICA POSIZIONE/DIDASCALIA"
        else:
            status, risk, action = "FIGURA MODIFICATA", "MEDIO", "CONTROLLO VISIVO AFFIANCATO"
        rows.append({"stato": status, "rischio": risk, "pagina_vecchia": o.page + 1, "pagina_nuova": n.page + 1, "distanza_hash": distance, "dimensioni_vecchie": f"{o.width}x{o.height}", "dimensioni_nuove": f"{n.width}x{n.height}", "posizione_vecchia": ", ".join(f"{v:.1f}" for v in o.bbox), "posizione_nuova": ", ".join(f"{v:.1f}" for v in n.bbox), "azione": action, "vecchia_immagine": base64.b64encode(o.data).decode("ascii"), "nuova_immagine": base64.b64encode(n.data).decode("ascii"), "estensione_vecchia": o.extension, "estensione_nuova": n.extension})
    for o in old:
        if o.index in used_o: continue
        rows.append({"stato": "FIGURA ELIMINATA", "rischio": "MEDIO", "pagina_vecchia": o.page + 1, "pagina_nuova": "", "distanza_hash": "", "dimensioni_vecchie": f"{o.width}x{o.height}", "dimensioni_nuove": "", "posizione_vecchia": ", ".join(f"{v:.1f}" for v in o.bbox), "posizione_nuova": "", "azione": "NON RIPORTARE", "vecchia_immagine": base64.b64encode(o.data).decode("ascii"), "nuova_immagine": "", "estensione_vecchia": o.extension, "estensione_nuova": ""})
    return pd.DataFrame(rows)


def analyze_layouts(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path, old_pages: str = "", translation_pages: str = "", new_pages: str = "", start_anchor: str = "") -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    old_layout = trim_layout_from_anchor(extract_layout(old_source_pdf, old_pages), start_anchor)
    old_translation_layout = trim_layout_from_anchor(extract_layout(old_translation_pdf, translation_pages), start_anchor)
    new_layout = trim_layout_from_anchor(extract_layout(new_source_pdf, new_pages), start_anchor)
    text_df = compare_text(old_layout, old_translation_layout, new_layout)
    table_df = compare_tables(old_layout, old_translation_layout, new_layout)
    image_df = compare_images(old_layout, new_layout)
    low_translation = int(((text_df.get("affidabilita_traduzione", pd.Series(dtype=float)) < 60) & text_df.get("vecchio_originale", pd.Series(dtype=str)).fillna("").ne("")).sum()) if not text_df.empty else 0
    technical_text_states = ["MODIFICA TECNICA", "CONTESTO TECNICO MODIFICATO"]
    summary = {
        "build_motore": ENGINE_BUILD,
        "pagine_vecchie": len(old_layout.selected_pages), "pagine_nuove": len(new_layout.selected_pages),
        "blocchi_testo_vecchi": len(old_layout.text_blocks), "blocchi_testo_nuovi": len(new_layout.text_blocks),
        "match_cat_esatti": int(text_df["metodo_abbinamento"].eq("CAT_EXACT").sum()) if not text_df.empty else 0,
        "match_semantici_esatti": int(text_df["metodo_abbinamento"].eq("SEMANTIC_EXACT").sum()) if not text_df.empty else 0,
        "match_fuzzy_sequenziali": int(text_df["metodo_abbinamento"].eq("SEQUENTIAL_FUZZY").sum()) if not text_df.empty else 0,
        "tabelle_vecchie": len(old_layout.tables), "tabelle_nuove": len(new_layout.tables),
        "immagini_vecchie": len(old_layout.images), "immagini_nuove": len(new_layout.images),
        "modifiche_tecniche_testo": int(text_df["stato"].isin(technical_text_states).sum()) if not text_df.empty else 0,
        "contenuti_nuovi_testo": int((text_df["stato"] == "NUOVO").sum()) if not text_df.empty else 0,
        "contenuti_eliminati_testo": int((text_df["stato"] == "ELIMINATO").sum()) if not text_df.empty else 0,
        "allineamenti_traduzione_da_verificare": low_translation,
        "modifiche_tecniche_tabelle": int((table_df["stato"] == "MODIFICA TECNICA").sum()) if not table_df.empty else 0,
        "figure_nuove_o_modificate": int(image_df["stato"].isin(["NUOVA FIGURA", "FIGURA MODIFICATA"]).sum()) if not image_df.empty else 0,
    }
    return text_df, table_df, image_df, summary


def _status_color(status: str) -> str:
    return {"INVARIATO": "#DCFCE7", "CONTEXT MATCH": "#DCFCE7", "VARIAZIONE TERMINOLOGICA": "#E0F2FE", "RIFERIMENTO AGGIORNATO": "#E0F2FE", "RIFORMULATO": "#E0F2FE", "CONTESTO TECNICO MODIFICATO": "#FDE68A", "MODIFICA TECNICA": "#FDE68A", "MODIFICATO": "#FED7AA", "NUOVO": "#DBEAFE", "ELIMINATO": "#FECACA", "FIGURA INVARIATA": "#DCFCE7", "FIGURA RICOLLOCATA": "#E0F2FE", "FIGURA MODIFICATA": "#FDE68A", "NUOVA FIGURA": "#DBEAFE", "FIGURA ELIMINATA": "#FECACA"}.get(status, "#FFFFFF")


def dataframe_table_html(df: pd.DataFrame, image_mode: bool = False) -> str:
    if df.empty: return "<p>Nessun elemento rilevato.</p>"
    visible_columns = list(df.columns) if not image_mode else [c for c in df.columns if c not in {"vecchia_immagine", "nuova_immagine", "estensione_vecchia", "estensione_nuova"}]
    headers = "".join(f"<th>{html.escape(str(c))}</th>" for c in visible_columns)
    if image_mode: headers += "<th>Anteprima vecchia</th><th>Anteprima nuova</th>"
    rows = []
    for _, row in df.iterrows():
        status = str(row.get("stato", "")); cells = []
        for col in visible_columns:
            style = f"background:{_status_color(status)};font-weight:700" if col == "stato" else ""
            cells.append(f"<td style='{style}'>{html.escape(str(row[col]))}</td>")
        if image_mode:
            old_img, new_img = row.get("vecchia_immagine", ""), row.get("nuova_immagine", "")
            old_ext, new_ext = row.get("estensione_vecchia", "png") or "png", row.get("estensione_nuova", "png") or "png"
            cells.append(f"<td>{f'<img src=\"data:image/{old_ext};base64,{old_img}\">' if old_img else ''}</td>")
            cells.append(f"<td>{f'<img src=\"data:image/{new_ext};base64,{new_img}\">' if new_img else ''}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def build_html_report(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, title: str = "ManualSync v0.5") -> str:
    metrics = "".join(f"<div class='metric'><strong>{html.escape(k.replace('_', ' ').title())}</strong><br>{v}</div>" for k, v in summary.items())
    return f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>
body{{font-family:Arial,sans-serif;margin:0;color:#0f172a;background:#f8fafc}}header{{background:#0f172a;color:white;padding:26px 34px}}main{{padding:24px 34px}}.metrics{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 24px}}.metric{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:10px 14px;min-width:190px}}h2{{margin-top:34px}}.note{{background:#fef3c7;border-radius:10px;padding:14px}}.table-wrap{{overflow:auto;max-height:780px;border:1px solid #cbd5e1}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white}}th,td{{border:1px solid #cbd5e1;padding:8px;vertical-align:top;min-width:120px}}th{{background:#0f766e;color:white;position:sticky;top:0;z-index:1}}td img{{max-width:300px;max-height:220px}}</style></head><body><header><h1>{html.escape(title)}</h1><p>Allineamento gerarchico CAT + controllo delle modifiche tecniche in testo, tabelle e figure.</p></header><main><div class='metrics'>{metrics}</div><p class='note'><b>Regola v0.5:</b> numeri di pagina e numerazioni di sezione non sono valori tecnici. Le vecchie traduzioni vengono mostrate solo quando l'allineamento strutturale supera la soglia minima.</p><h2>1. Testo</h2>{dataframe_table_html(text_df)}<h2>2. Tabelle</h2>{dataframe_table_html(table_df)}<h2>3. Figure</h2>{dataframe_table_html(image_df, image_mode=True)}</main></body></html>"""


def save_analysis_outputs(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, output_dir: str | Path, prefix: str = "manualsync_v05") -> dict[str, str]:
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    text_csv = out / f"{prefix}_testo.csv"; table_csv = out / f"{prefix}_tabelle.csv"; images_csv = out / f"{prefix}_figure.csv"
    text_df.to_csv(text_csv, index=False, encoding="utf-8-sig"); table_df.to_csv(table_csv, index=False, encoding="utf-8-sig")
    image_public = image_df.drop(columns=["vecchia_immagine", "nuova_immagine", "estensione_vecchia", "estensione_nuova"], errors="ignore")
    image_public.to_csv(images_csv, index=False, encoding="utf-8-sig")
    summary_path = out / f"{prefix}_sintesi.json"; summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = out / f"{prefix}_rapporto.html"; report_path.write_text(build_html_report(text_df, table_df, image_df, summary), encoding="utf-8")
    return {"testo": str(text_csv), "tabelle": str(table_csv), "figure": str(images_csv), "sintesi": str(summary_path), "rapporto": str(report_path)}


# ============================================================================
# ManualSync v0.6 overrides — conservative decision layer
# ============================================================================
ENGINE_BUILD = "0.6.0-decision-safety-20260805"

# CP3.4: the language evidence (file name, stop-words, how they combine) moved verbatim to
# language_signals (pure: no PDF, no I/O); detect_document_language / detect_docx_language below
# and idml_import use it from there.
from language_signals import LANGUAGE_STOPWORDS, _filename_language, _score_text_language, _combine_language_signals


def detect_document_language(path: str | Path, sample_pages: int = 3) -> tuple[str, float, str]:
    file_lang, file_conf = _filename_language(path)
    try:
        doc = fitz.open(str(path))
        chunks = []
        for i in range(min(sample_pages, len(doc))):
            chunks.append(doc[i].get_text("text"))
        doc.close()
        text = " ".join(chunks).lower()
    except Exception:
        text = ""
    text_lang, text_conf = _score_text_language(text)
    return _combine_language_signals(file_lang, file_conf, text_lang, text_conf)


def detect_docx_language(path: str | Path, sample_paragraphs: int = 200) -> tuple[str, float, str]:
    """Same detection principle as detect_document_language, for Word sources.

    DOCX has no page-rendered text stream like a PDF, so this reads the first
    N paragraphs via python-docx instead of PyMuPDF. The scoring/combination
    logic is shared (not duplicated) with the PDF path.
    """
    file_lang, file_conf = _filename_language(path)
    try:
        doc = DocxDocument(str(path))
        chunks = [p.text for p in doc.paragraphs[:sample_paragraphs] if p.text]
        text = " ".join(chunks).lower()
    except Exception:
        text = ""
    text_lang, text_conf = _score_text_language(text)
    return _combine_language_signals(file_lang, file_conf, text_lang, text_conf)


def validate_language_combination(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path) -> dict:
    old_lang, old_conf, old_basis = detect_document_language(old_source_pdf)
    tr_lang, tr_conf, tr_basis = detect_document_language(old_translation_pdf)
    new_lang, new_conf, new_basis = detect_document_language(new_source_pdf)
    if old_lang != "unknown" and new_lang != "unknown" and old_lang != new_lang and min(old_conf, new_conf) >= 0.70:
        raise ValueError(
            "Lingue non coerenti: il vecchio originale risulta "
            f"'{old_lang.upper()}', mentre il nuovo originale risulta '{new_lang.upper()}'. "
            "I due originali devono essere nella stessa lingua. Controlla soprattutto il terzo file."
        )
    if old_lang != "unknown" and tr_lang != "unknown" and old_lang == tr_lang and min(old_conf, tr_conf) >= 0.85:
        raise ValueError(
            "La vecchia traduzione sembra essere nella stessa lingua del vecchio originale "
            f"({old_lang.upper()}). Carica la traduzione nella lingua di destinazione."
        )
    return {
        "lingua_vecchio_originale": old_lang.upper(),
        "lingua_vecchia_traduzione": tr_lang.upper(),
        "lingua_nuovo_originale": new_lang.upper(),
        "affidabilita_lingua_vecchio": round(old_conf * 100, 1),
        "affidabilita_lingua_traduzione": round(tr_conf * 100, 1),
        "affidabilita_lingua_nuovo": round(new_conf * 100, 1),
        "base_rilevamento_lingue": f"vecchio: {old_basis}; traduzione: {tr_basis}; nuovo: {new_basis}",
    }


def load_terminology_equivalences(path: str | Path | None) -> set[tuple[str, str]]:
    approved: set[tuple[str, str]] = set()
    if not path:
        return approved
    try:
        df = pd.read_csv(path, sep=None, engine="python")
    except Exception as exc:
        raise ValueError(f"Glossario equivalenze non leggibile: {exc}") from exc
    normalized_cols = {str(c).strip().lower(): c for c in df.columns}
    old_col = next((normalized_cols[c] for c in ["termine_precedente", "termine_vecchio", "old_term", "source_old"] if c in normalized_cols), None)
    new_col = next((normalized_cols[c] for c in ["termine_nuovo", "new_term", "source_new"] if c in normalized_cols), None)
    approved_col = next((normalized_cols[c] for c in ["approvato", "approved"] if c in normalized_cols), None)
    if old_col is None or new_col is None:
        raise ValueError("Il glossario deve contenere le colonne 'termine_precedente' e 'termine_nuovo'.")
    for _, row in df.iterrows():
        old_term = normalize_cat(str(row.get(old_col, "") or ""))
        new_term = normalize_cat(str(row.get(new_col, "") or ""))
        approved_value = str(row.get(approved_col, "si") if approved_col else "si").strip().lower()
        if old_term and new_term and approved_value in {"si", "sì", "yes", "true", "1", "approvato"}:
            approved.add((old_term, new_term))
            approved.add((new_term, old_term))
    return approved


def terminology_change_approved(old_text: str, new_text: str, approved_pairs: set[tuple[str, str]]) -> bool:
    old_words = normalize_cat(old_text).split()
    new_words = normalize_cat(new_text).split()
    if not approved_pairs or len(old_words) != len(new_words):
        return False
    differences = [(o, n) for o, n in zip(old_words, new_words) if o != n]
    return bool(differences) and all((o, n) in approved_pairs for o, n in differences)


def hierarchical_align_text(old: list[TextBlock], new: list[TextBlock]) -> tuple[dict[int, tuple[int, float, str]], list[int]]:
    """v0.6: deterministic CAT anchors, normalized-equivalence anchors, then sequential fuzzy."""
    mapping: dict[int, tuple[int, float, str]] = {}
    cat_pairs = _lcs_pairs(old, new, lambda b: b.cat_normalized)
    for oi, ni in cat_pairs:
        mapping[ni] = (oi, 1.0, "CAT_EXACT")
    normalized_pairs: list[tuple[int, int]] = []
    for os, oe, ns, ne in _gap_ranges(cat_pairs, len(old), len(new)):
        local = _lcs_pairs(old[os:oe], new[ns:ne], lambda b: b.normalized)
        normalized_pairs.extend((os + oi, ns + ni) for oi, ni in local)
    for oi, ni in normalized_pairs:
        mapping[ni] = (oi, 0.995, "NORMALIZED_EQUIVALENCE")
    all_anchors = sorted((oi, ni) for ni, (oi, _, _) in mapping.items())
    for os, oe, ns, ne in _gap_ranges(all_anchors, len(old), len(new)):
        mapping.update(_align_gap(old[os:oe], new[ns:ne], os, ns))
    used_old = {oi for oi, _, _ in mapping.values()}
    return mapping, sorted(set(range(len(old))) - used_old)


def _global_search_blocks(pdf_path: str | Path) -> list[dict]:
    doc = fitz.open(str(pdf_path))
    output: list[dict] = []
    for page_index, page in enumerate(doc):
        for block in page.get_text("blocks", sort=True):
            if len(block) < 5:
                continue
            text = str(block[4] or "").strip()
            if not text or re.fullmatch(r"\d+", text):
                continue
            output.append({
                "page": page_index + 1,
                "text": text,
                "cat": normalize_cat(text),
                "semantic": normalize(text),
            })
    doc.close()
    return output


def search_unmatched_globally(new_source_pdf: str | Path, old_blocks: list[TextBlock], unmatched_old: list[int], selected_pages: list[int]) -> dict[int, dict]:
    if not unmatched_old:
        return {}
    candidates = _global_search_blocks(new_source_pdf)
    selected_one_based = {p + 1 for p in selected_pages}
    results: dict[int, dict] = {}
    for oi in unmatched_old:
        old = old_blocks[oi]
        best = None
        for candidate in candidates:
            if not candidate["cat"]:
                continue
            if old.cat_normalized == candidate["cat"]:
                score, method = 100.0, "CAT_EXACT_GLOBAL"
            elif old.normalized == candidate["semantic"]:
                score, method = 99.0, "NORMALIZED_EQUIVALENCE_GLOBAL"
            else:
                score = max(ratio(old.cat_normalized, candidate["cat"]), ratio(old.normalized, candidate["semantic"]))
                method = "FUZZY_GLOBAL"
            if best is None or score > best["score"]:
                best = {**candidate, "score": float(score), "method": method}
        if best and best["score"] >= 92.0 and best["page"] not in selected_one_based:
            results[oi] = best
    return results


def _decision_for_row(status: str, category: str, method: str, tr_conf: float, terminology_approved: bool) -> tuple[str, str, str]:
    if status == "INVARIATO" and method == "CAT_EXACT" and tr_conf >= 80:
        return "RIUSO AUTOMATICO CONSENTITO", "NO", "100% CAT match, nessuna variazione tecnica e traduzione allineata con affidabilità ≥80%."
    if status == "INVARIATO":
        return "CONTROLLO TRADUZIONE", "SÌ", "Il testo sorgente è invariato, ma la vecchia traduzione non raggiunge la soglia per il riuso automatico."
    if status.startswith("VARIAZIONE TERMINOLOGICA"):
        if terminology_approved:
            return "RIUSO PROPOSTO CON CONTROLLO RAPIDO", "SÌ", "Equivalenza terminologica presente nel glossario approvato; confermare il contesto."
        return "REVISIONE TERMINOLOGICA OBBLIGATORIA", "SÌ", "Somiglianza normalizzata, ma nessuna equivalenza terminologica approvata è stata fornita."
    if status == "RIFERIMENTO AGGIORNATO":
        return "AGGIORNA RIFERIMENTO E REVISIONA", "SÌ", "Il testo è riutilizzabile solo dopo l'aggiornamento del riferimento pagina/sezione."
    if status in {"MODIFICA TECNICA", "CONTESTO TECNICO MODIFICATO"}:
        return "BLOCCATO — REVISIONE TECNICA", "SÌ", "Sono cambiati valori, codici, varianti o contesto tecnico."
    if status in {"RIFORMULATO", "MODIFICATO"}:
        return "REVISIONE LINGUISTICA OBBLIGATORIA", "SÌ", f"Il match è {category}; non è ammesso il riuso automatico."
    if status == "NUOVO":
        return "TRADUZIONE E REVISIONE", "SÌ", "Nessun segmento precedente è stato abbinato."
    if status == "SPOSTATO NEL DOCUMENTO":
        return "VERIFICA NUOVA POSIZIONE E CONTESTO", "SÌ", "Il contenuto è stato trovato fuori dall'intervallo selezionato."
    if status == "NON TROVATO NEL NUOVO AMBITO":
        return "CERCA NEL DOCUMENTO COMPLETO", "SÌ", "Il contenuto non è stato trovato nel file/ambito analizzato; non può essere dichiarato eliminato automaticamente."
    return "REVISIONE", "SÌ", "Decisione prudenziale del prototipo."


def compare_text(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout, new_source_pdf: str | Path | None = None, approved_terms: set[tuple[str, str]] | None = None) -> pd.DataFrame:
    approved_terms = approved_terms or set()
    old, new = old_layout.text_blocks, new_layout.text_blocks
    mapping, unmatched_old = hierarchical_align_text(old, new)
    translations = map_translation_blocks(old, old_translation_layout.text_blocks)
    global_hits = search_unmatched_globally(new_source_pdf, old, unmatched_old, new_layout.selected_pages) if new_source_pdf else {}
    rows: list[dict] = []
    for n in new:
        if n.index not in mapping:
            status = "NUOVO"
            decision, review, reason = _decision_for_row(status, "NEW", "UNMATCHED", 0.0, False)
            rows.append({
                "stato": status, "rischio": "ALTO" if n.technical_values or n.codes else "MEDIO", "categoria_match": "NEW",
                "metodo_abbinamento": "UNMATCHED", "decisione_riuso": decision, "richiede_revisione": review, "motivo_revisione": reason,
                "vecchio_originale": "— Nessun contenuto precedente abbinato —", "nuovo_originale": n.text, "vecchia_traduzione": "",
                "similarita_linguistica": 0.0, "affidabilita_abbinamento": 0.0, "affidabilita_traduzione": 0.0,
                "indice_vecchio": "", "indice_nuovo": n.index, "pagina_vecchia": "", "pagina_nuova": n.page + 1,
                "valori_tecnici_vecchi": "", "valori_tecnici_nuovi": ", ".join(n.technical_values), "codici_vecchi": "", "codici_nuovi": ", ".join(n.codes),
                "riferimenti_vecchi": "", "riferimenti_nuovi": ", ".join(n.page_refs), "terminologia_approvata": "N/A", "azione": decision,
            })
            continue
        oi, align_score, method = mapping[n.index]
        o = old[oi]
        status, risk, _ = classify_change(o, n)
        category, lexical = match_category(o, n, mapping, n.index, old, new)
        tr_text, tr_conf, _ = translations.get(oi, ("", 0.0, -1))
        approved = terminology_change_approved(o.text, n.text, approved_terms)
        if method == "NORMALIZED_EQUIVALENCE" and status.startswith("VARIAZIONE TERMINOLOGICA"):
            status = "VARIAZIONE TERMINOLOGICA APPROVATA" if approved else "VARIAZIONE TERMINOLOGICA NON APPROVATA"
        decision, review, reason = _decision_for_row(status, category, method, tr_conf, approved)
        if tr_conf < 60:
            tr_text = ""
            reason += " La vecchia traduzione è nascosta perché l'allineamento è inferiore al 60%."
        rows.append({
            "stato": status, "rischio": risk, "categoria_match": category, "metodo_abbinamento": method,
            "decisione_riuso": decision, "richiede_revisione": review, "motivo_revisione": reason,
            "vecchio_originale": o.text, "nuovo_originale": n.text, "vecchia_traduzione": tr_text,
            "similarita_linguistica": lexical, "affidabilita_abbinamento": min(100.0, round(align_score * 100, 1)), "affidabilita_traduzione": tr_conf,
            "indice_vecchio": oi, "indice_nuovo": n.index, "pagina_vecchia": o.page + 1, "pagina_nuova": n.page + 1,
            "valori_tecnici_vecchi": ", ".join(o.technical_values), "valori_tecnici_nuovi": ", ".join(n.technical_values),
            "codici_vecchi": ", ".join(o.codes), "codici_nuovi": ", ".join(n.codes), "riferimenti_vecchi": ", ".join(o.page_refs), "riferimenti_nuovi": ", ".join(n.page_refs),
            "terminologia_approvata": "SÌ" if approved else ("NO" if method == "NORMALIZED_EQUIVALENCE" else "N/A"), "azione": decision,
        })
    for oi in unmatched_old:
        o = old[oi]
        tr_text, tr_conf, _ = translations.get(oi, ("", 0.0, -1))
        hit = global_hits.get(oi)
        if hit:
            status = "SPOSTATO NEL DOCUMENTO"
            new_text = hit["text"]
            new_page = hit["page"]
            method = hit["method"]
            similarity = round(hit["score"], 1)
            category = "100% MATCH GLOBALE" if similarity == 100 else "MATCH GLOBALE DA VERIFICARE"
        else:
            status = "NON TROVATO NEL NUOVO AMBITO"
            new_text = "— Non trovato nel file/intervallo analizzato —"
            new_page = ""
            method = "GLOBAL_SEARCH_NO_MATCH"
            similarity = 0.0
            category = "NOT FOUND IN SCOPE"
        decision, review, reason = _decision_for_row(status, category, method, tr_conf, False)
        rows.append({
            "stato": status, "rischio": "MEDIO", "categoria_match": category, "metodo_abbinamento": method,
            "decisione_riuso": decision, "richiede_revisione": review, "motivo_revisione": reason,
            "vecchio_originale": o.text, "nuovo_originale": new_text, "vecchia_traduzione": tr_text if tr_conf >= 60 else "",
            "similarita_linguistica": similarity, "affidabilita_abbinamento": similarity, "affidabilita_traduzione": tr_conf,
            "indice_vecchio": oi, "indice_nuovo": "", "pagina_vecchia": o.page + 1, "pagina_nuova": new_page,
            "valori_tecnici_vecchi": ", ".join(o.technical_values), "valori_tecnici_nuovi": "", "codici_vecchi": ", ".join(o.codes), "codici_nuovi": "",
            "riferimenti_vecchi": ", ".join(o.page_refs), "riferimenti_nuovi": "", "terminologia_approvata": "N/A", "azione": decision,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["similarita_linguistica"] = df["similarita_linguistica"].clip(lower=0, upper=100)
        df["affidabilita_abbinamento"] = df["affidabilita_abbinamento"].clip(lower=0, upper=100)
    return df


def analyze_layouts(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path, old_pages: str = "", translation_pages: str = "", new_pages: str = "", start_anchor: str = "", terminology_glossary: str | Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    language_summary = validate_language_combination(old_source_pdf, old_translation_pdf, new_source_pdf)
    approved_terms = load_terminology_equivalences(terminology_glossary)
    old_layout = trim_layout_from_anchor(extract_layout(old_source_pdf, old_pages), start_anchor)
    old_translation_layout = trim_layout_from_anchor(extract_layout(old_translation_pdf, translation_pages), start_anchor)
    new_layout = trim_layout_from_anchor(extract_layout(new_source_pdf, new_pages), start_anchor)
    text_df = compare_text(old_layout, old_translation_layout, new_layout, new_source_pdf=new_source_pdf, approved_terms=approved_terms)
    table_df = compare_tables(old_layout, old_translation_layout, new_layout)
    image_df = compare_images(old_layout, new_layout)
    if not image_df.empty:
        image_df["stato"] = image_df["stato"].replace({"FIGURA ELIMINATA": "FIGURA NON TROVATA NELL'AMBITO"})
        image_df["azione"] = image_df["azione"].replace({"NON RIPORTARE": "CERCA NEL DOCUMENTO COMPLETO / VERIFICA SPOSTAMENTO"})
    review_count = int(text_df["richiede_revisione"].eq("SÌ").sum()) if not text_df.empty else 0
    low_translation = int(((text_df["affidabilita_traduzione"] < 80) & text_df["vecchio_originale"].fillna("").ne("")).sum()) if not text_df.empty else 0
    technical_text_states = ["MODIFICA TECNICA", "CONTESTO TECNICO MODIFICATO"]
    summary = {
        "build_motore": ENGINE_BUILD,
        **language_summary,
        "equivalenze_terminologiche_approvate": len(approved_terms) // 2,
        "pagine_vecchie": len(old_layout.selected_pages), "pagine_nuove": len(new_layout.selected_pages),
        "blocchi_testo_vecchi": len(old_layout.text_blocks), "blocchi_testo_nuovi": len(new_layout.text_blocks),
        "match_cat_esatti": int(text_df["metodo_abbinamento"].eq("CAT_EXACT").sum()) if not text_df.empty else 0,
        "equivalenze_normalizzate_da_revisionare": int(text_df["metodo_abbinamento"].eq("NORMALIZED_EQUIVALENCE").sum()) if not text_df.empty else 0,
        "match_fuzzy_sequenziali": int(text_df["metodo_abbinamento"].eq("SEQUENTIAL_FUZZY").sum()) if not text_df.empty else 0,
        "riusi_automatici_consentiti": int(text_df["decisione_riuso"].eq("RIUSO AUTOMATICO CONSENTITO").sum()) if not text_df.empty else 0,
        "elementi_testo_da_revisionare": review_count,
        "allineamenti_traduzione_sotto_80": low_translation,
        "modifiche_tecniche_testo": int(text_df["stato"].isin(technical_text_states).sum()) if not text_df.empty else 0,
        "contenuti_nuovi_testo": int(text_df["stato"].eq("NUOVO").sum()) if not text_df.empty else 0,
        "contenuti_spostati_trovati_globalmente": int(text_df["stato"].eq("SPOSTATO NEL DOCUMENTO").sum()) if not text_df.empty else 0,
        "contenuti_non_trovati_nel_nuovo_ambito": int(text_df["stato"].eq("NON TROVATO NEL NUOVO AMBITO").sum()) if not text_df.empty else 0,
        "tabelle_vecchie": len(old_layout.tables), "tabelle_nuove": len(new_layout.tables),
        "immagini_vecchie": len(old_layout.images), "immagini_nuove": len(new_layout.images),
        "modifiche_tecniche_tabelle": int((table_df["stato"] == "MODIFICA TECNICA").sum()) if not table_df.empty else 0,
        "figure_nuove_o_modificate": int(image_df["stato"].isin(["NUOVA FIGURA", "FIGURA MODIFICATA"]).sum()) if not image_df.empty else 0,
    }
    return text_df, table_df, image_df, summary


def _status_color(status: str) -> str:
    return {
        "INVARIATO": "#DCFCE7", "VARIAZIONE TERMINOLOGICA APPROVATA": "#E0F2FE", "VARIAZIONE TERMINOLOGICA NON APPROVATA": "#FDE68A",
        "RIFERIMENTO AGGIORNATO": "#E0F2FE", "RIFORMULATO": "#E0F2FE", "CONTESTO TECNICO MODIFICATO": "#FDE68A",
        "MODIFICA TECNICA": "#FDE68A", "MODIFICATO": "#FED7AA", "NUOVO": "#DBEAFE", "SPOSTATO NEL DOCUMENTO": "#E0F2FE",
        "NON TROVATO NEL NUOVO AMBITO": "#FECACA", "FIGURA INVARIATA": "#DCFCE7", "FIGURA RICOLLOCATA": "#E0F2FE",
        "FIGURA MODIFICATA": "#FDE68A", "NUOVA FIGURA": "#DBEAFE", "FIGURA NON TROVATA NELL'AMBITO": "#FECACA",
    }.get(status, "#FFFFFF")


def build_html_report(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, title: str = "ManualSync v0.6") -> str:
    metrics = "".join(f"<div class='metric'><strong>{html.escape(k.replace('_', ' ').title())}</strong><br>{html.escape(str(v))}</div>" for k, v in summary.items())
    return f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>
body{{font-family:Arial,sans-serif;margin:0;color:#0f172a;background:#f8fafc}}header{{background:#0f172a;color:white;padding:26px 34px}}main{{padding:24px 34px}}.metrics{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 24px}}.metric{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:10px 14px;min-width:190px}}h2{{margin-top:34px}}.note{{background:#fef3c7;border-radius:10px;padding:14px}}.table-wrap{{overflow:auto;max-height:780px;border:1px solid #cbd5e1}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white}}th,td{{border:1px solid #cbd5e1;padding:8px;vertical-align:top;min-width:140px}}th{{background:#0f766e;color:white;position:sticky;top:0;z-index:1}}td img{{max-width:300px;max-height:220px}}</style></head><body><header><h1>{html.escape(title)}</h1><p>Decisioni conservative: riuso automatico solo con 100% CAT match, dati tecnici invariati e traduzione affidabile.</p></header><main><div class='metrics'>{metrics}</div><p class='note'><b>Regola v0.6:</b> un contenuto non trovato nell'ambito analizzato non viene dichiarato eliminato. Le equivalenze terminologiche non diventano exact match e richiedono revisione salvo glossario approvato.</p><h2>1. Testo</h2>{dataframe_table_html(text_df)}<h2>2. Tabelle</h2>{dataframe_table_html(table_df)}<h2>3. Figure</h2>{dataframe_table_html(image_df, image_mode=True)}</main></body></html>"""


def save_analysis_outputs(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, output_dir: str | Path, prefix: str = "manualsync_v06") -> dict[str, str]:
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    text_csv = out / f"{prefix}_testo.csv"; table_csv = out / f"{prefix}_tabelle.csv"; images_csv = out / f"{prefix}_figure.csv"
    text_df.to_csv(text_csv, index=False, encoding="utf-8-sig"); table_df.to_csv(table_csv, index=False, encoding="utf-8-sig")
    image_public = image_df.drop(columns=["vecchia_immagine", "nuova_immagine", "estensione_vecchia", "estensione_nuova"], errors="ignore")
    image_public.to_csv(images_csv, index=False, encoding="utf-8-sig")
    summary_path = out / f"{prefix}_sintesi.json"; summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = out / f"{prefix}_rapporto.html"; report_path.write_text(build_html_report(text_df, table_df, image_df, summary), encoding="utf-8")
    return {"testo": str(text_csv), "tabelle": str(table_csv), "figure": str(images_csv), "sintesi": str(summary_path), "rapporto": str(report_path)}

# ============================================================================
# ManualSync v0.7 overrides — bilingual alignment + review cockpit
# ============================================================================
ENGINE_BUILD = "0.7.0-review-cockpit-20260805"


# CP3.4: the live map_translation_blocks (this, its last definition) moved verbatim to
# translation_alignment; imported at this exact position so the earlier, shadowed
# definition above is still replaced at the same point of the module.
from translation_alignment import map_translation_blocks


def _priority_for_decision(row: pd.Series) -> tuple[int, str]:
    decision = str(row.get("decisione_riuso", ""))
    status = str(row.get("stato", ""))
    if "BLOCCATO" in decision or status in {"MODIFICA TECNICA", "CONTESTO TECNICO MODIFICATO"}:
        return 1, "1 — BLOCCO TECNICO"
    if status == "NUOVO" or "TRADUZIONE E REVISIONE" in decision:
        return 2, "2 — NUOVO DA TRADURRE"
    if "OBBLIGATORIA" in decision or "CONTROLLO TRADUZIONE" in decision:
        return 3, "3 — REVISIONE OBBLIGATORIA"
    if status in {"SPOSTATO NEL DOCUMENTO", "NON TROVATO NEL NUOVO AMBITO"}:
        return 4, "4 — RICERCA / CONTESTO"
    if "AGGIORNA RIFERIMENTO" in decision or "CONTROLLO RAPIDO" in decision:
        return 5, "5 — CONTROLLO RAPIDO"
    if "RIUSO AUTOMATICO" in decision:
        return 6, "6 — RIUSO SICURO"
    return 5, "5 — DA CONTROLLARE"


def build_review_queue(text_df: pd.DataFrame) -> pd.DataFrame:
    if text_df.empty:
        return pd.DataFrame()
    rows = []
    for _, row in text_df.iterrows():
        rank, priority = _priority_for_decision(row)
        rows.append({
            "_ordine": rank,
            "priorita": priority,
            "decisione": row.get("decisione_riuso", ""),
            "stato": row.get("stato", ""),
            "rischio": row.get("rischio", ""),
            "match": row.get("categoria_match", ""),
            "vecchio_originale": row.get("vecchio_originale", ""),
            "nuovo_originale": row.get("nuovo_originale", ""),
            "traduzione_recuperata": row.get("vecchia_traduzione", ""),
            "motivo": row.get("motivo_revisione", ""),
            "affidabilita_traduzione": row.get("affidabilita_traduzione", 0.0),
            "pagina_vecchia": row.get("pagina_vecchia", ""),
            "pagina_nuova": row.get("pagina_nuova", ""),
        })
    queue = pd.DataFrame(rows).sort_values(
        ["_ordine", "pagina_nuova", "pagina_vecchia"],
        kind="stable",
        na_position="last",
    ).drop(columns=["_ordine"])
    return queue.reset_index(drop=True)


def analyze_layouts(
    old_source_pdf: str | Path,
    old_translation_pdf: str | Path,
    new_source_pdf: str | Path,
    old_pages: str = "",
    translation_pages: str = "",
    new_pages: str = "",
    start_anchor: str = "",
    terminology_glossary: str | Path | None = None,
    full_new_source_pdf: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    language_summary = validate_language_combination(old_source_pdf, old_translation_pdf, new_source_pdf)
    if full_new_source_pdf:
        full_lang, full_conf, _ = detect_document_language(full_new_source_pdf)
        if full_lang != language_summary["lingua_nuovo_originale"].lower():
            raise ValueError(
                "Il manuale completo opzionale non sembra essere nella stessa lingua del nuovo originale. "
                f"Rilevato: {full_lang.upper()} invece di {language_summary['lingua_nuovo_originale']}."
            )
    approved_terms = load_terminology_equivalences(terminology_glossary)
    old_layout = trim_layout_from_anchor(extract_layout(old_source_pdf, old_pages), start_anchor)
    old_translation_layout = trim_layout_from_anchor(extract_layout(old_translation_pdf, translation_pages), start_anchor)
    new_layout = trim_layout_from_anchor(extract_layout(new_source_pdf, new_pages), start_anchor)
    global_pdf = full_new_source_pdf or new_source_pdf
    text_df = compare_text(
        old_layout,
        old_translation_layout,
        new_layout,
        new_source_pdf=global_pdf,
        approved_terms=approved_terms,
    )
    table_df = compare_tables(old_layout, old_translation_layout, new_layout)
    image_df = compare_images(old_layout, new_layout)

    review_count = int(text_df["richiede_revisione"].astype(str).str.upper().eq("SÌ").sum()) if not text_df.empty else 0
    if not text_df.empty:
        has_legacy_source = ~text_df.get("stato", pd.Series(dtype=str)).isin(["NUOVO"])
        low_translation = int((has_legacy_source & (text_df.get("affidabilita_traduzione", pd.Series(dtype=float)) < 80)).sum())
    else:
        low_translation = 0
    technical_text_states = {"MODIFICA TECNICA", "CONTESTO TECNICO MODIFICATO"}
    queue = build_review_queue(text_df)
    summary = {
        "build_motore": ENGINE_BUILD,
        **language_summary,
        "equivalenze_terminologiche_approvate": len(approved_terms),
        "ambito_ricerca_globale": "MANUALE COMPLETO OPZIONALE" if full_new_source_pdf else "SOLO FILE CARICATO",
        "pagine_vecchie": old_layout.page_count,
        "pagine_nuove": new_layout.page_count,
        "blocchi_testo_vecchi": len(old_layout.text_blocks),
        "blocchi_testo_nuovi": len(new_layout.text_blocks),
        "match_cat_esatti": int(text_df["metodo_abbinamento"].eq("CAT_EXACT").sum()) if not text_df.empty else 0,
        "equivalenze_normalizzate_da_revisionare": int(text_df["metodo_abbinamento"].eq("NORMALIZED_EQUIVALENCE").sum()) if not text_df.empty else 0,
        "match_fuzzy_sequenziali": int(text_df["metodo_abbinamento"].eq("SEQUENTIAL_FUZZY").sum()) if not text_df.empty else 0,
        "riusi_automatici_consentiti": int(text_df["decisione_riuso"].eq("RIUSO AUTOMATICO CONSENTITO").sum()) if not text_df.empty else 0,
        "elementi_testo_da_revisionare": review_count,
        "allineamenti_traduzione_sotto_80": low_translation,
        "modifiche_tecniche_testo": int(text_df["stato"].isin(technical_text_states).sum()) if not text_df.empty else 0,
        "contenuti_nuovi_testo": int(text_df["stato"].eq("NUOVO").sum()) if not text_df.empty else 0,
        "contenuti_spostati_trovati_globalmente": int(text_df["stato"].eq("SPOSTATO NEL DOCUMENTO").sum()) if not text_df.empty else 0,
        "contenuti_non_trovati_nel_nuovo_ambito": int(text_df["stato"].eq("NON TROVATO NEL NUOVO AMBITO").sum()) if not text_df.empty else 0,
        "tabelle_vecchie": len(old_layout.tables),
        "tabelle_nuove": len(new_layout.tables),
        "immagini_vecchie": len(old_layout.images),
        "immagini_nuove": len(new_layout.images),
        "modifiche_tecniche_tabelle": int((table_df["stato"] == "MODIFICA TECNICA").sum()) if not table_df.empty else 0,
        "figure_nuove_o_modificate": int(image_df["stato"].isin(["NUOVA FIGURA", "FIGURA MODIFICATA"]).sum()) if not image_df.empty else 0,
        "azioni_prioritarie": int(queue["priorita"].astype(str).str.startswith(("1", "2", "3")).sum()) if not queue.empty else 0,
    }
    return text_df, table_df, image_df, summary


def build_html_report(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, title: str = "ManualSync v0.7") -> str:
    queue = build_review_queue(text_df)
    metrics = "".join(
        f"<div class='metric'><strong>{html.escape(k.replace('_', ' ').title())}</strong><br>{html.escape(str(v))}</div>"
        for k, v in summary.items()
    )
    return f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>
body{{font-family:Arial,sans-serif;margin:0;color:#0f172a;background:#f8fafc}}header{{background:#0f172a;color:white;padding:26px 34px}}main{{padding:24px 34px}}.metrics{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 24px}}.metric{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:10px 14px;min-width:190px}}h2{{margin-top:34px}}.note{{background:#fef3c7;border-radius:10px;padding:14px}}.table-wrap{{overflow:auto;max-height:780px;border:1px solid #cbd5e1}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white}}th,td{{border:1px solid #cbd5e1;padding:8px;vertical-align:top;min-width:140px}}th{{background:#0f766e;color:white;position:sticky;top:0;z-index:1}}td img{{max-width:300px;max-height:220px}}</style></head><body><header><h1>{html.escape(title)}</h1><p>Coda operativa prima, dettagli tecnici dopo. Il riuso automatico resta limitato ai casi dimostrati sicuri.</p></header><main><div class='metrics'>{metrics}</div><p class='note'><b>Regola v0.7:</b> l'affidabilità della vecchia traduzione usa anche la coerenza sequenziale del documento bilingue. Un manuale completo opzionale può essere usato per cercare contenuti spostati fuori dall'estratto.</p><h2>1. Coda di lavoro</h2>{dataframe_table_html(queue)}<h2>2. Dettaglio CAT e tecnico</h2>{dataframe_table_html(text_df)}<h2>3. Tabelle</h2>{dataframe_table_html(table_df)}<h2>4. Figure</h2>{dataframe_table_html(image_df, image_mode=True)}</main></body></html>"""


def save_analysis_outputs(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, output_dir: str | Path, prefix: str = "manualsync_v07") -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    queue = build_review_queue(text_df)
    queue_csv = out / f"{prefix}_coda_lavoro.csv"
    text_csv = out / f"{prefix}_dettaglio_testo.csv"
    table_csv = out / f"{prefix}_tabelle.csv"
    images_csv = out / f"{prefix}_figure.csv"
    queue.to_csv(queue_csv, index=False, encoding="utf-8-sig")
    text_df.to_csv(text_csv, index=False, encoding="utf-8-sig")
    table_df.to_csv(table_csv, index=False, encoding="utf-8-sig")
    image_public = image_df.drop(columns=["vecchia_immagine", "nuova_immagine", "estensione_vecchia", "estensione_nuova"], errors="ignore")
    image_public.to_csv(images_csv, index=False, encoding="utf-8-sig")
    summary_path = out / f"{prefix}_sintesi.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = out / f"{prefix}_rapporto.html"
    report_path.write_text(build_html_report(text_df, table_df, image_df, summary), encoding="utf-8")
    return {
        "coda": str(queue_csv),
        "testo": str(text_csv),
        "tabelle": str(table_csv),
        "figure": str(images_csv),
        "sintesi": str(summary_path),
        "rapporto": str(report_path),
    }

# ============================================================================
# ManualSync v0.9 overrides — structural document intelligence
# ============================================================================
from collections import Counter, defaultdict
import numpy as np

ENGINE_BUILD = "0.8.0-structural-intelligence-20260805"

_extract_layout_v07 = extract_layout
_map_translation_blocks_v07 = map_translation_blocks
_validate_language_combination_v07 = validate_language_combination
_load_terminology_equivalences_v07 = load_terminology_equivalences
_terminology_change_approved_v07 = terminology_change_approved

_SAFETY_CRITICAL = re.compile(
    r"\b(?:danger|pericolo|electric shock|folgorazione|explosion|esplosione|arc flash|arco elettrico|"
    r"death|morte|serious injury|lesioni gravi|wait\s+\d+\s+minutes?|attendere\s+\d+\s+minut)\b",
    re.I,
)
_SAFETY_WARNING = re.compile(
    r"\b(?:warning|avvertenza|caution|attenzione|hazard|rischio|injury|lesion|safety|sicurezza)\b",
    re.I,
)
_TECH_CONCEPTS = {
    "cooling": {"cooling", "raffreddamento", "heatsink", "dissipatore", "fan", "ventola", "air flow", "flusso d'aria"},
    "mounting": {"mounting", "montaggio", "clearance", "spazio libero", "distance", "distanza", "vertical", "verticale", "horizontal", "orizzontale"},
    "protection": {"protection", "protezione", "ip20", "ip54", "enclosure", "involucro", "cabinet", "quadro"},
    "electrical": {"voltage", "tensione", "current", "corrente", "frequency", "frequenza", "power", "potenza"},
    "fastening": {"screw", "vite", "washer", "rondella", "bolt", "bullone", "tighten", "stringere"},
}
_GENERIC_GLOBAL = {
    "warning", "danger", "caution", "notice", "description", "mounting conditions",
    "40 c 104 f", "50 c 122 f", "60 c 140 f", "motors", "sf",
}


def _text_tokens(text: str) -> list[str]:
    return re.findall(r"[\w°%]+", normalize_cat(text), re.UNICODE)


def _safety_level(text: str) -> int:
    if _SAFETY_CRITICAL.search(text or ""):
        return 3
    if _SAFETY_WARNING.search(text or ""):
        return 2
    return 0


def _tech_concept_set(text: str) -> set[str]:
    low = (text or "").casefold()
    return {name for name, words in _TECH_CONCEPTS.items() if any(word in low for word in words)}


def _mostly_graphic_value(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return False
    if re.fullmatch(r"(?:in\s*=\s*)?\d+(?:[.,]\d+)?\s*%", t, re.I):
        return True
    if re.fullmatch(r"(?:\d+(?:[.,]\d+)?\s*(?:k?hz|°\s?[cf]|%)(?:\s*[-–]?\s*)?)+", t, re.I):
        return True
    if re.fullmatch(r"(?:\d+\s*kHz\s*){2,}(?:SF)?", t, re.I):
        return True
    if t.upper() == "SF":
        return True
    if re.fullmatch(r"ATV\d+[A-Z0-9•()._-]*(?:\.\.\.|…)[A-Z0-9•()._-]+", t, re.I):
        return True
    return False


def _toc_like(text: str) -> bool:
    t = (text or "").strip()
    if re.search(r"what.?s in this chapter|contenuto del capitolo|indice", t, re.I):
        return True
    if len(re.findall(r"\.{4,}\s*\d+", t)) >= 2:
        return True
    return False


def _running_header_like(block: TextBlock, page_text: str) -> bool:
    if block.bbox[1] >= 36:
        return False
    low = block.text.casefold()
    product = any(x in low for x in ["variable speed drives", "variatori di velocità", "sinamics"])
    section = any(x in low for x in ["mounting conditions", "condizioni di montaggio", "derating curves", "curve di declassamento", "power dissipated"])
    return product and section


def _classify_structure(block: TextBlock, page_meta: dict) -> str:
    text = block.text.strip()
    if _running_header_like(block, page_meta.get("text", "")):
        return "RUNNING_HEADER"
    if _toc_like(text):
        return "TOC_NAVIGATION"
    if _safety_level(text) >= 2:
        return "SAFETY"
    if re.match(r"^\s*[•·▪‣◦-]", text):
        return "BULLET"
    visual_rich = page_meta.get("drawings", 0) > 20 or page_meta.get("images", 0) > 0
    page_low = page_meta.get("text", "").casefold()
    if visual_rich and (_mostly_graphic_value(text) or ("derating" in page_low and re.fullmatch(r"ATV\S+", text, re.I))):
        return "GRAPHIC_LABEL"
    if block.heading:
        return "HEADING"
    return "PARAGRAPH"


def extract_layout(pdf_path: str | Path, page_spec: str = "") -> DocumentLayout:
    """v0.8: retain document structure and remove navigation / chart-axis noise from CAT segmentation."""
    layout = _extract_layout_v07(pdf_path, page_spec)
    doc = fitz.open(str(pdf_path))
    page_meta: dict[int, dict] = {}
    try:
        for rel, original_page in enumerate(layout.selected_pages):
            page = doc[original_page]
            page_meta[rel] = {
                "drawings": len(page.get_drawings()),
                "images": len(page.get_images(full=True)),
                "text": page.get_text("text"),
            }
    finally:
        doc.close()

    kept: list[TextBlock] = []
    excluded: list[dict] = []
    counts = Counter()
    for block in layout.text_blocks:
        structure = _classify_structure(block, page_meta.get(block.page, {}))
        setattr(block, "structure_type", structure)
        counts[structure] += 1
        if structure in {"RUNNING_HEADER", "TOC_NAVIGATION", "GRAPHIC_LABEL"}:
            excluded.append({"page": block.page + 1, "type": structure, "text": block.text})
            continue
        kept.append(block)
    for idx, block in enumerate(kept):
        block.index = idx
    layout.text_blocks = kept
    setattr(layout, "excluded_blocks", excluded)
    setattr(layout, "structure_counts", dict(counts))
    setattr(layout, "page_meta", page_meta)
    return layout


def text_pair_score(old: TextBlock, new: TextBlock, old_count: int, new_count: int) -> float:
    if old.cat_normalized and old.cat_normalized == new.cat_normalized:
        return 1.0
    if old.normalized and old.normalized == new.normalized:
        return 0.995
    lexical = max(ratio(old.cat_normalized, new.cat_normalized), ratio(old.normalized, new.normalized)) / 100.0
    old_type = getattr(old, "structure_type", "PARAGRAPH")
    new_type = getattr(new, "structure_type", "PARAGRAPH")
    compatible = old_type == new_type or {old_type, new_type} <= {"PARAGRAPH", "BULLET"}
    score = lexical * 0.82
    score += 0.08 if compatible else -0.30
    if old.heading != new.heading:
        score -= 0.15
    if _safety_level(old.text) != _safety_level(new.text):
        score -= 0.28
    if old.section_ref and new.section_ref:
        score += 0.12 if old.section_ref == new.section_ref else -0.30
    score += 0.05 * _set_overlap(old.technical_values, new.technical_values)
    score += 0.07 * _set_overlap(old.codes, new.codes)
    po = old.index / max(1, old_count - 1)
    pn = new.index / max(1, new_count - 1)
    score += 0.03 * max(0.0, 1 - abs(po - pn))
    return max(0.0, min(1.0, score))


def _align_gap_v08(old: list[TextBlock], new: list[TextBlock], old_offset: int, new_offset: int, minimum_score: float = 0.68, gap_cost: float = 0.27) -> dict[int, tuple[int, float, str]]:
    m, n = len(old), len(new)
    if not m or not n:
        return {}
    dp = [[0.0] * (n + 1) for _ in range(m + 1)]
    choice = [[""] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dp[i][0] = dp[i - 1][0] - gap_cost; choice[i][0] = "D"
    for j in range(1, n + 1):
        dp[0][j] = dp[0][j - 1] - gap_cost; choice[0][j] = "I"
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            s = text_pair_score(old[i-1], new[j-1], m, n)
            lexical = max(ratio(old[i-1].cat_normalized, new[j-1].cat_normalized), ratio(old[i-1].normalized, new[j-1].normalized)) / 100.0
            allowed = s >= minimum_score and lexical >= 0.55
            gain = s * 1.35 if allowed else -0.95
            opts = {"M": dp[i-1][j-1] + gain, "D": dp[i-1][j] - gap_cost, "I": dp[i][j-1] - gap_cost}
            best = max(("M", "D", "I"), key=lambda k: (opts[k], {"M": 2, "D": 1, "I": 0}[k]))
            dp[i][j] = opts[best]; choice[i][j] = best
    result = {}
    i, j = m, n
    while i > 0 or j > 0:
        c = choice[i][j]
        if c == "M":
            s = text_pair_score(old[i-1], new[j-1], m, n)
            lexical = max(ratio(old[i-1].cat_normalized, new[j-1].cat_normalized), ratio(old[i-1].normalized, new[j-1].normalized)) / 100.0
            if s >= minimum_score and lexical >= 0.55:
                result[new_offset + j - 1] = (old_offset + i - 1, s, "SEQUENTIAL_FUZZY")
            i -= 1; j -= 1
        elif c == "D":
            i -= 1
        else:
            j -= 1
    return result


def hierarchical_align_text(old: list[TextBlock], new: list[TextBlock]) -> tuple[dict[int, tuple[int, float, str]], list[int]]:
    mapping: dict[int, tuple[int, float, str]] = {}
    cat_pairs = _lcs_pairs(old, new, lambda b: b.cat_normalized)
    for oi, ni in cat_pairs:
        mapping[ni] = (oi, 1.0, "CAT_EXACT")
    normalized_pairs: list[tuple[int, int]] = []
    for os, oe, ns, ne in _gap_ranges(cat_pairs, len(old), len(new)):
        local = _lcs_pairs(old[os:oe], new[ns:ne], lambda b: b.normalized)
        normalized_pairs.extend((os + oi, ns + ni) for oi, ni in local)
    for oi, ni in normalized_pairs:
        mapping[ni] = (oi, 0.995, "NORMALIZED_EQUIVALENCE")
    anchors = sorted((oi, ni) for ni, (oi, _, _) in mapping.items())
    for os, oe, ns, ne in _gap_ranges(anchors, len(old), len(new)):
        mapping.update(_align_gap_v08(old[os:oe], new[ns:ne], os, ns))
    used = {oi for oi, _, _ in mapping.values()}
    return mapping, sorted(set(range(len(old))) - used)


def _global_search_blocks(pdf_path: str | Path) -> list[dict]:
    doc = fitz.open(str(pdf_path))
    raw: list[dict] = []
    try:
        for page_index, page in enumerate(doc):
            meta = {"drawings": len(page.get_drawings()), "images": len(page.get_images(full=True)), "text": page.get_text("text")}
            blocks = []
            for b in page.get_text("blocks", sort=True):
                if len(b) < 5:
                    continue
                x0, y0, x1, y1, text = b[:5]
                text = str(text or "").strip()
                if not text or y1 > page.rect.height - 100 or re.fullmatch(r"\d+", text):
                    continue
                dummy = TextBlock(0, page_index, (x0,y0,x1,y1), text, normalize(text), normalize_cat(text), extract_technical_values(text), extract_codes(text), extract_page_refs(text), extract_section_ref(text), False, 9.0)
                structure = _classify_structure(dummy, meta)
                if structure in {"RUNNING_HEADER", "TOC_NAVIGATION", "GRAPHIC_LABEL"}:
                    continue
                blocks.append({"page": page_index+1, "text": text, "cat": dummy.cat_normalized, "semantic": dummy.normalized, "structure": structure})
            raw.extend(blocks)
    finally:
        doc.close()
    for i, item in enumerate(raw):
        item["prev"] = raw[i-1]["semantic"] if i > 0 and raw[i-1]["page"] in {item["page"], item["page"]-1} else ""
        item["next"] = raw[i+1]["semantic"] if i+1 < len(raw) and raw[i+1]["page"] in {item["page"], item["page"]+1} else ""
    return raw


def search_unmatched_globally(new_source_pdf: str | Path, old_blocks: list[TextBlock], unmatched_old: list[int], selected_pages: list[int]) -> dict[int, dict]:
    if not unmatched_old:
        return {}
    candidates = _global_search_blocks(new_source_pdf)
    selected = {p+1 for p in selected_pages}
    results = {}
    for oi in unmatched_old:
        old = old_blocks[oi]
        exact = [c for c in candidates if c["cat"] and c["cat"] == old.cat_normalized and c["page"] not in selected]
        semantic = [c for c in candidates if c["semantic"] and c["semantic"] == old.normalized and c["page"] not in selected]
        pool = exact or semantic
        method = "CAT_EXACT_GLOBAL" if exact else "NORMALIZED_EQUIVALENCE_GLOBAL"
        if not pool:
            fuzzy = []
            for c in candidates:
                if c["page"] in selected:
                    continue
                score = max(ratio(old.cat_normalized, c["cat"]), ratio(old.normalized, c["semantic"]))
                if score >= 95:
                    fuzzy.append((float(score), c))
            fuzzy.sort(key=lambda x: x[0], reverse=True)
            if fuzzy:
                best_score, best = fuzzy[0]
                second = fuzzy[1][0] if len(fuzzy) > 1 else 0.0
                certainty = "HIGH" if best_score >= 98 and best_score-second >= 5 else "AMBIGUOUS"
                results[oi] = {**best, "score": best_score, "method": "FUZZY_GLOBAL", "occurrences": len(fuzzy), "certainty": certainty, "context_score": 0.0}
            continue
        prev_old = old_blocks[oi-1].normalized if oi > 0 else ""
        next_old = old_blocks[oi+1].normalized if oi+1 < len(old_blocks) else ""
        ranked = []
        for c in pool:
            context = 0.0
            if prev_old and c.get("prev") == prev_old: context += 0.5
            if next_old and c.get("next") == next_old: context += 0.5
            ranked.append((context, c))
        ranked.sort(key=lambda x: x[0], reverse=True)
        context, best = ranked[0]
        generic = len(_text_tokens(old.text)) <= 3 or old.normalized in _GENERIC_GLOBAL or _safety_level(old.text) >= 2 and len(_text_tokens(old.text)) <= 2
        if len(pool) == 1 and not generic:
            certainty = "HIGH"
        elif context >= 0.5 and (len(ranked) == 1 or context > ranked[1][0]):
            certainty = "HIGH"
        else:
            certainty = "AMBIGUOUS"
        results[oi] = {**best, "score": 100.0 if exact else 99.0, "method": method, "occurrences": len(pool), "certainty": certainty, "context_score": context}
    return results


def _technical_change(old: TextBlock, new: TextBlock) -> bool:
    if set(old.technical_values) != set(new.technical_values) or set(old.codes) != set(new.codes):
        return True
    oc, nc = _tech_concept_set(old.text), _tech_concept_set(new.text)
    return bool(oc != nc and (oc or nc))


def _decision_v08(status: str, category: str, method: str, tr_conf: float, approved: bool, safety_level: int = 0) -> tuple[str, str, str]:
    if status == "INVARIATO" and method == "CAT_EXACT" and tr_conf >= 80:
        return "RIUSO AUTOMATICO CONSENTITO", "NO", "100% CAT match, dati tecnici invariati e traduzione allineata ≥80%."
    if status == "INVARIATO":
        return "CONTROLLO TRADUZIONE", "SÌ", "Sorgente invariata, ma allineamento della vecchia traduzione insufficiente."
    if status == "NUOVO CONTENUTO DI SICUREZZA":
        return "BLOCCATO — TRADUZIONE E REVISIONE SICUREZZA", "SÌ", "Nuovo contenuto di sicurezza: revisione tecnica e linguistica obbligatoria."
    if status == "MODIFICA DI SICUREZZA":
        return "BLOCCATO — REVISIONE SICUREZZA", "SÌ", "Avvertenza, conseguenza o parametro di sicurezza modificato."
    if status in {"MODIFICA TECNICA", "CONTESTO TECNICO MODIFICATO"}:
        return "BLOCCATO — REVISIONE TECNICA", "SÌ", "Sono cambiati valori, codici o concetti tecnici."
    if status.startswith("VARIAZIONE TERMINOLOGICA"):
        return ("RIUSO PROPOSTO CON CONTROLLO RAPIDO", "SÌ", "Equivalenza approvata: confermare il contesto.") if approved else ("REVISIONE TERMINOLOGICA OBBLIGATORIA", "SÌ", "Equivalenza non presente nel glossario approvato.")
    if status == "RIFERIMENTO AGGIORNATO":
        return "AGGIORNA RIFERIMENTO E REVISIONA", "SÌ", "Riferimento pagina/sezione modificato."
    if status in {"RIFORMULATO", "MODIFICATO"}:
        return "REVISIONE LINGUISTICA OBBLIGATORIA", "SÌ", f"Match {category}: riuso automatico non ammesso."
    if status == "NUOVO":
        return "TRADUZIONE E REVISIONE", "SÌ", "Nessun segmento precedente affidabile."
    if status == "SPOSTATO NEL DOCUMENTO":
        return "VERIFICA NUOVA POSIZIONE E CONTESTO", "SÌ", "Corrispondenza globale unica o confermata dal contesto."
    if status == "TROVATO ALTROVE — AMBIGUO":
        return "VERIFICA OCCORRENZA E CONTESTO", "SÌ", "La stessa stringa compare più volte: lo spostamento non è dimostrato."
    if status == "NON TROVATO NEL NUOVO AMBITO":
        return "CERCA / CONFERMA RIMOZIONE", "SÌ", "Contenuto non trovato; non viene dichiarato eliminato automaticamente."
    return "REVISIONE", "SÌ", "Decisione prudenziale."


def compare_text(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout, new_source_pdf: str | Path | None = None, approved_terms: set[tuple[str, str]] | None = None) -> pd.DataFrame:
    approved_terms = approved_terms or set()
    old, new = old_layout.text_blocks, new_layout.text_blocks
    mapping, unmatched_old = hierarchical_align_text(old, new)
    translations = _map_translation_blocks_v07(old, old_translation_layout.text_blocks)
    global_hits = search_unmatched_globally(new_source_pdf, old, unmatched_old, new_layout.selected_pages) if new_source_pdf else {}
    rows = []
    for n in new:
        n_type = getattr(n, "structure_type", "PARAGRAPH")
        if n.index not in mapping:
            sl = _safety_level(n.text)
            status = "NUOVO CONTENUTO DI SICUREZZA" if sl >= 2 else "NUOVO"
            risk = "CRITICO" if sl >= 3 else ("ALTO" if sl >= 2 or n.technical_values or n.codes else "MEDIO")
            decision, review, reason = _decision_v08(status, "NEW", "UNMATCHED", 0.0, False, sl)
            rows.append({
                "tipo_strutturale": n_type, "stato": status, "rischio": risk, "categoria_match": "NEW", "metodo_abbinamento": "UNMATCHED",
                "decisione_riuso": decision, "richiede_revisione": review, "motivo_revisione": reason,
                "vecchio_originale": "— Nessun contenuto precedente abbinato —", "nuovo_originale": n.text, "vecchia_traduzione": "",
                "similarita_linguistica": 0.0, "affidabilita_abbinamento": 0.0, "affidabilita_traduzione": 0.0,
                "indice_vecchio": "", "indice_nuovo": n.index, "pagina_vecchia": "", "pagina_nuova": n.page+1,
                "valori_tecnici_vecchi": "", "valori_tecnici_nuovi": ", ".join(n.technical_values), "codici_vecchi": "", "codici_nuovi": ", ".join(n.codes),
                "riferimenti_vecchi": "", "riferimenti_nuovi": ", ".join(n.page_refs), "terminologia_approvata": "N/A", "unicita_globale": "N/A", "azione": decision,
            })
            continue
        oi, align_score, method = mapping[n.index]
        o = old[oi]
        o_type = getattr(o, "structure_type", "PARAGRAPH")
        status, risk, _ = classify_change(o, n)
        category, lexical = match_category(o, n, mapping, n.index, old, new)
        old_safety, new_safety = _safety_level(o.text), _safety_level(n.text)
        if (old_safety or new_safety) and o.cat_normalized != n.cat_normalized:
            status, risk = "MODIFICA DI SICUREZZA", "CRITICO" if max(old_safety, new_safety) >= 3 else "ALTO"
        elif method not in {"CAT_EXACT"} and _technical_change(o, n):
            status, risk = "MODIFICA TECNICA", "ALTO"
        tr_text, tr_conf, _ = translations.get(oi, ("", 0.0, -1))
        approved = _terminology_change_approved_v07(o.text, n.text, approved_terms)
        if method == "NORMALIZED_EQUIVALENCE" and status.startswith("VARIAZIONE TERMINOLOGICA"):
            status = "VARIAZIONE TERMINOLOGICA APPROVATA" if approved else "VARIAZIONE TERMINOLOGICA NON APPROVATA"
        decision, review, reason = _decision_v08(status, category, method, tr_conf, approved, max(old_safety, new_safety))
        if tr_conf < 60:
            tr_text = ""; reason += " Traduzione nascosta: allineamento <60%."
        rows.append({
            "tipo_strutturale": f"{o_type}→{n_type}", "stato": status, "rischio": risk, "categoria_match": category, "metodo_abbinamento": method,
            "decisione_riuso": decision, "richiede_revisione": review, "motivo_revisione": reason,
            "vecchio_originale": o.text, "nuovo_originale": n.text, "vecchia_traduzione": tr_text,
            "similarita_linguistica": lexical, "affidabilita_abbinamento": min(100.0, round(align_score*100,1)), "affidabilita_traduzione": tr_conf,
            "indice_vecchio": oi, "indice_nuovo": n.index, "pagina_vecchia": o.page+1, "pagina_nuova": n.page+1,
            "valori_tecnici_vecchi": ", ".join(o.technical_values), "valori_tecnici_nuovi": ", ".join(n.technical_values),
            "codici_vecchi": ", ".join(o.codes), "codici_nuovi": ", ".join(n.codes), "riferimenti_vecchi": ", ".join(o.page_refs), "riferimenti_nuovi": ", ".join(n.page_refs),
            "terminologia_approvata": "SÌ" if approved else ("NO" if method == "NORMALIZED_EQUIVALENCE" else "N/A"), "unicita_globale": "N/A", "azione": decision,
        })
    for oi in unmatched_old:
        o = old[oi]
        tr_text, tr_conf, _ = translations.get(oi, ("", 0.0, -1))
        hit = global_hits.get(oi)
        if hit and hit.get("certainty") == "HIGH":
            status, new_text, new_page = "SPOSTATO NEL DOCUMENTO", hit["text"], hit["page"]
            method, similarity, category = hit["method"], round(hit["score"],1), "100% MATCH GLOBALE" if hit["score"] >= 99 else "MATCH GLOBALE"
        elif hit:
            status, new_text, new_page = "TROVATO ALTROVE — AMBIGUO", hit["text"], hit["page"]
            method, similarity, category = hit["method"], round(hit["score"],1), "MATCH GLOBALE AMBIGUO"
        else:
            status, new_text, new_page = "NON TROVATO NEL NUOVO AMBITO", "— Non trovato nel file/intervallo analizzato —", ""
            method, similarity, category = "GLOBAL_SEARCH_NO_MATCH", 0.0, "NOT FOUND IN SCOPE"
        decision, review, reason = _decision_v08(status, category, method, tr_conf, False, _safety_level(o.text))
        rows.append({
            "tipo_strutturale": getattr(o,"structure_type","PARAGRAPH"), "stato": status, "rischio": "ALTO" if _safety_level(o.text)>=2 else "MEDIO",
            "categoria_match": category, "metodo_abbinamento": method, "decisione_riuso": decision, "richiede_revisione": review, "motivo_revisione": reason,
            "vecchio_originale": o.text, "nuovo_originale": new_text, "vecchia_traduzione": tr_text if tr_conf>=60 else "",
            "similarita_linguistica": similarity, "affidabilita_abbinamento": similarity, "affidabilita_traduzione": tr_conf,
            "indice_vecchio": oi, "indice_nuovo": "", "pagina_vecchia": o.page+1, "pagina_nuova": new_page,
            "valori_tecnici_vecchi": ", ".join(o.technical_values), "valori_tecnici_nuovi": "", "codici_vecchi": ", ".join(o.codes), "codici_nuovi": "",
            "riferimenti_vecchi": ", ".join(o.page_refs), "riferimenti_nuovi": "", "terminologia_approvata": "N/A",
            "unicita_globale": (f"{hit.get('occurrences',0)} occorrenze; contesto {hit.get('context_score',0):.1f}" if hit else "0 occorrenze"), "azione": decision,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        for col in ["similarita_linguistica","affidabilita_abbinamento","affidabilita_traduzione"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).clip(0,100)
    return df


def _cell_diffs(old_row: list[str], new_row: list[str]) -> str:
    width = max(len(old_row), len(new_row))
    diffs = []
    for i in range(width):
        ov = old_row[i].strip() if i < len(old_row) else ""
        nv = new_row[i].strip() if i < len(new_row) else ""
        if normalize_cat(ov) != normalize_cat(nv):
            diffs.append(f"C{i+1}: {ov or '∅'} → {nv or '∅'}")
    return " | ".join(diffs)


def compare_tables(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    old, new = old_layout.tables, new_layout.tables
    table_map, unmatched_new = align_tables(old, new)
    translation_map = map_translation_tables(old, old_translation_layout.tables)
    used_old = set()
    rows = []
    for ni, nt in enumerate(new):
        if ni not in table_map:
            header = " | ".join(nt.rows[0]) if nt.rows else ""
            rows.append({"tabella_vecchia":"", "tabella_nuova":ni+1, "riga":"TABELLA", "stato":"NUOVA TABELLA", "rischio":"ALTO" if _safety_level(header)>=2 else "MEDIO", "vecchia_riga":"", "nuova_riga":header, "celle_modificate":"Intera tabella nuova", "vecchia_traduzione":"", "affidabilita_traduzione":0.0, "azione":"REVISIONE STRUTTURA E TRADUZIONE"})
            continue
        oi, _ = table_map[ni]; used_old.add(oi); ot = old[oi]
        row_map, unmatched_rows = align_rows(ot.rows, nt.rows)
        used_old_rows = set()
        for nri, nr in enumerate(nt.rows):
            if nri not in row_map:
                ntxt=row_text(nr); sl=_safety_level(ntxt)
                rows.append({"tabella_vecchia":oi+1,"tabella_nuova":ni+1,"riga":nri+1,"stato":"NUOVA RIGA DI SICUREZZA" if sl>=2 else "NUOVA RIGA","rischio":"CRITICO" if sl>=3 else ("ALTO" if sl>=2 or extract_technical_values(ntxt) or extract_codes(ntxt) else "MEDIO"),"vecchia_riga":"","nuova_riga":ntxt,"celle_modificate":"Intera riga nuova","vecchia_traduzione":"","affidabilita_traduzione":0.0,"azione":"BLOCCATO — REVISIONE SICUREZZA" if sl>=2 else "TRADUZIONE E REVISIONE"})
                continue
            ori,_=row_map[nri]; used_old_rows.add(ori); orow=ot.rows[ori]; otxt=row_text(orow); ntxt=row_text(nr)
            sl=max(_safety_level(otxt),_safety_level(ntxt)); exact=normalize_cat(otxt)==normalize_cat(ntxt)
            if exact: status,risk,action="INVARIATO","BASSO","RIUSO STRUTTURA"
            elif sl>=2: status,risk,action="MODIFICA DI SICUREZZA","CRITICO" if sl>=3 else "ALTO","BLOCCATO — REVISIONE SICUREZZA"
            elif set(extract_technical_values(otxt))!=set(extract_technical_values(ntxt)) or set(extract_codes(otxt))!=set(extract_codes(ntxt)):
                status,risk,action="MODIFICA TECNICA","ALTO","BLOCCATO — REVISIONE TECNICA"
            else: status,risk,action="MODIFICATO","MEDIO","REVISIONE"
            tr,conf=translation_map.get((oi,ori),("",0.0))
            rows.append({"tabella_vecchia":oi+1,"tabella_nuova":ni+1,"riga":nri+1,"stato":status,"rischio":risk,"vecchia_riga":otxt,"nuova_riga":ntxt,"celle_modificate":_cell_diffs(orow,nr),"vecchia_traduzione":tr if conf>=60 else "","affidabilita_traduzione":conf,"azione":action})
        for ori, orow in enumerate(ot.rows):
            if ori not in used_old_rows:
                rows.append({"tabella_vecchia":oi+1,"tabella_nuova":ni+1,"riga":f"old-{ori+1}","stato":"RIGA NON TROVATA NELL'AMBITO","rischio":"ALTO" if _safety_level(row_text(orow))>=2 else "MEDIO","vecchia_riga":row_text(orow),"nuova_riga":"","celle_modificate":"Riga non abbinata","vecchia_traduzione":"","affidabilita_traduzione":0.0,"azione":"VERIFICA RIMOZIONE / SPOSTAMENTO"})
    for oi, ot in enumerate(old):
        if oi not in used_old:
            header=" | ".join(ot.rows[0]) if ot.rows else ""
            rows.append({"tabella_vecchia":oi+1,"tabella_nuova":"","riga":"TABELLA","stato":"TABELLA NON TROVATA NELL'AMBITO","rischio":"ALTO" if _safety_level(header)>=2 else "MEDIO","vecchia_riga":header,"nuova_riga":"","celle_modificate":"Intera tabella non abbinata","vecchia_traduzione":"","affidabilita_traduzione":0.0,"azione":"VERIFICA RIMOZIONE / SPOSTAMENTO"})
    return pd.DataFrame(rows)


def _page_render_data(pdf_path: str | Path, selected_pages: list[int]) -> list[dict]:
    doc=fitz.open(str(pdf_path)); out=[]
    try:
        for rel, pi in enumerate(selected_pages):
            page=doc[pi]
            pix=page.get_pixmap(matrix=fitz.Matrix(1.15,1.15), alpha=False)
            data=pix.tobytes("png")
            text=page.get_text("text")
            meaningful=[]
            for b in page.get_text("blocks", sort=True):
                if len(b)<5: continue
                x0,y0,x1,y1,t=b[:5]; t=str(t or '').strip()
                if not t or y0<35 or y1>page.rect.height-100 or _toc_like(t) or _mostly_graphic_value(t): continue
                meaningful.append(t)
            sig=" ".join(meaningful[:3])
            out.append({"rel":rel,"page":rel+1,"signature":normalize(sig),"text":text,"data":data,"dhash":dhash_bytes(data,16),"drawings":len(page.get_drawings()),"raster_images":len(page.get_images(full=True))})
    finally: doc.close()
    return out


def _align_visual_pages(old_pages: list[dict], new_pages: list[dict]) -> tuple[dict[int,tuple[int,float]],set[int]]:
    # Monotonic alignment with page-heading similarity; insertions are allowed.
    m,n=len(old_pages),len(new_pages); gap=0.25
    dp=[[0.0]*(n+1) for _ in range(m+1)]; ch=[[""]*(n+1) for _ in range(m+1)]
    for i in range(1,m+1): dp[i][0]=dp[i-1][0]-gap; ch[i][0]="D"
    for j in range(1,n+1): dp[0][j]=dp[0][j-1]-gap; ch[0][j]="I"
    for i in range(1,m+1):
        for j in range(1,n+1):
            s=ratio(old_pages[i-1]["signature"],new_pages[j-1]["signature"])/100.0 if old_pages[i-1]["signature"] and new_pages[j-1]["signature"] else 0.0
            opts={"M":dp[i-1][j-1]+(s if s>=0.35 else -0.6),"D":dp[i-1][j]-gap,"I":dp[i][j-1]-gap}
            best=max(opts,key=opts.get); dp[i][j]=opts[best]; ch[i][j]=best
    mp={}; used=set(); i,j=m,n
    while i>0 or j>0:
        c=ch[i][j]
        if c=="M":
            s=ratio(old_pages[i-1]["signature"],new_pages[j-1]["signature"])/100.0
            if s>=0.35: mp[j-1]=(i-1,s); used.add(i-1)
            i-=1;j-=1
        elif c=="D": i-=1
        else: j-=1
    return mp,used


def compare_images(old_layout: DocumentLayout, new_layout: DocumentLayout, old_pdf: str | Path | None = None, new_pdf: str | Path | None = None) -> pd.DataFrame:
    if not old_pdf or not new_pdf:
        return pd.DataFrame()
    oldp=_page_render_data(old_pdf,old_layout.selected_pages); newp=_page_render_data(new_pdf,new_layout.selected_pages)
    mp,used=_align_visual_pages(oldp,newp); rows=[]
    for ni,npag in enumerate(newp):
        if ni not in mp:
            rows.append({"stato":"NUOVA PAGINA / AREA VISIVA","rischio":"MEDIO","pagina_vecchia":"","pagina_nuova":npag["page"],"similarita_titolo":0.0,"distanza_visuale":"","modalita_vecchia":"","modalita_nuova":"VETTORIALE" if npag["drawings"]>20 else "RASTER/MISTA","azione":"CONTROLLO VISIVO","vecchia_immagine":"","nuova_immagine":base64.b64encode(npag["data"]).decode(),"estensione_vecchia":"png","estensione_nuova":"png"})
            continue
        oi,s=mp[ni]; opag=oldp[oi]; dist=hamming(opag["dhash"],npag["dhash"]); maxbits=256; sim=100*(1-dist/maxbits)
        status="PAGINA VISIVA INVARIATA" if sim>=94 else ("PAGINA VISIVA MODIFICATA" if sim<88 else "PAGINA VISIVA DA CONTROLLARE")
        rows.append({"stato":status,"rischio":"BASSO" if status.endswith("INVARIATA") else "MEDIO","pagina_vecchia":opag["page"],"pagina_nuova":npag["page"],"similarita_titolo":round(s*100,1),"distanza_visuale":round(sim,1),"modalita_vecchia":"VETTORIALE" if opag["drawings"]>20 else "RASTER/MISTA","modalita_nuova":"VETTORIALE" if npag["drawings"]>20 else "RASTER/MISTA","azione":"RIUSA" if status.endswith("INVARIATA") else "CONTROLLO VISIVO AFFIANCATO","vecchia_immagine":base64.b64encode(opag["data"]).decode(),"nuova_immagine":base64.b64encode(npag["data"]).decode(),"estensione_vecchia":"png","estensione_nuova":"png"})
    for oi,opag in enumerate(oldp):
        if oi not in used:
            rows.append({"stato":"PAGINA / AREA VISIVA NON ABBINATA","rischio":"MEDIO","pagina_vecchia":opag["page"],"pagina_nuova":"","similarita_titolo":0.0,"distanza_visuale":"","modalita_vecchia":"VETTORIALE" if opag["drawings"]>20 else "RASTER/MISTA","modalita_nuova":"","azione":"VERIFICA SPOSTAMENTO O RIMOZIONE","vecchia_immagine":base64.b64encode(opag["data"]).decode(),"nuova_immagine":"","estensione_vecchia":"png","estensione_nuova":"png"})
    return pd.DataFrame(rows)


def _priority_for_decision(row: pd.Series) -> tuple[int,str]:
    decision=str(row.get("decisione_riuso","")); status=str(row.get("stato","")); risk=str(row.get("rischio",""))
    if risk=="CRITICO" or "SICUREZZA" in status or "SICUREZZA" in decision:
        return 0,"0 — SICUREZZA CRITICA"
    if "BLOCCATO" in decision or status in {"MODIFICA TECNICA","CONTESTO TECNICO MODIFICATO"}:
        return 1,"1 — BLOCCO TECNICO"
    if status in {"NUOVO","NUOVO CONTENUTO DI SICUREZZA"} or "TRADUZIONE E REVISIONE" in decision:
        return 2,"2 — NUOVO DA TRADURRE"
    if "OBBLIGATORIA" in decision or "CONTROLLO TRADUZIONE" in decision:
        return 3,"3 — REVISIONE OBBLIGATORIA"
    if status in {"SPOSTATO NEL DOCUMENTO","TROVATO ALTROVE — AMBIGUO","NON TROVATO NEL NUOVO AMBITO"}:
        return 4,"4 — RICERCA / CONTESTO"
    if "AGGIORNA RIFERIMENTO" in decision or "CONTROLLO RAPIDO" in decision:
        return 5,"5 — CONTROLLO RAPIDO"
    if "RIUSO AUTOMATICO" in decision:
        return 6,"6 — RIUSO SICURO"
    return 5,"5 — DA CONTROLLARE"


def build_review_queue(text_df: pd.DataFrame) -> pd.DataFrame:
    if text_df.empty: return pd.DataFrame()
    rows=[]
    for _,row in text_df.iterrows():
        rank,priority=_priority_for_decision(row)
        rows.append({"_ordine":rank,"priorita":priority,"decisione":row.get("decisione_riuso",""),"stato":row.get("stato",""),"rischio":row.get("rischio",""),"tipo":row.get("tipo_strutturale",""),"match":row.get("categoria_match",""),"vecchio_originale":row.get("vecchio_originale",""),"nuovo_originale":row.get("nuovo_originale",""),"traduzione_recuperata":row.get("vecchia_traduzione",""),"motivo":row.get("motivo_revisione",""),"affidabilita_traduzione":row.get("affidabilita_traduzione",0.0),"unicita_globale":row.get("unicita_globale",""),"pagina_vecchia":row.get("pagina_vecchia",""),"pagina_nuova":row.get("pagina_nuova","")})
    return pd.DataFrame(rows).sort_values(["_ordine","pagina_nuova","pagina_vecchia"],kind="stable",na_position="last").drop(columns=["_ordine"]).reset_index(drop=True)


def analyze_layouts(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path, old_pages: str = "", translation_pages: str = "", new_pages: str = "", start_anchor: str = "", terminology_glossary: str | Path | None = None, full_new_source_pdf: str | Path | None = None) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame,dict]:
    language_summary=_validate_language_combination_v07(old_source_pdf,old_translation_pdf,new_source_pdf)
    if full_new_source_pdf:
        flang,_,_=detect_document_language(full_new_source_pdf)
        if flang != language_summary["lingua_nuovo_originale"].lower():
            raise ValueError("Il manuale completo opzionale non è nella stessa lingua del nuovo originale.")
    approved=_load_terminology_equivalences_v07(terminology_glossary)
    old_layout=trim_layout_from_anchor(extract_layout(old_source_pdf,old_pages),start_anchor)
    tr_layout=trim_layout_from_anchor(extract_layout(old_translation_pdf,translation_pages),start_anchor)
    new_layout=trim_layout_from_anchor(extract_layout(new_source_pdf,new_pages),start_anchor)
    text_df=compare_text(old_layout,tr_layout,new_layout,new_source_pdf=full_new_source_pdf or new_source_pdf,approved_terms=approved)
    table_df=compare_tables(old_layout,tr_layout,new_layout)
    image_df=compare_images(old_layout,new_layout,old_source_pdf,new_source_pdf)
    queue=build_review_queue(text_df)
    review_count=int(text_df["richiede_revisione"].astype(str).str.upper().eq("SÌ").sum()) if not text_df.empty else 0
    summary={
        "build_motore":ENGINE_BUILD,**language_summary,"equivalenze_terminologiche_approvate":len(approved),
        "ambito_ricerca_globale":"MANUALE COMPLETO" if full_new_source_pdf else "SOLO FILE CARICATO",
        "pagine_vecchie":len(old_layout.selected_pages),"pagine_nuove":len(new_layout.selected_pages),
        "blocchi_testo_vecchi":len(old_layout.text_blocks),"blocchi_testo_nuovi":len(new_layout.text_blocks),
        "elementi_strutturali_esclusi_vecchi":len(getattr(old_layout,"excluded_blocks",[])),"elementi_strutturali_esclusi_nuovi":len(getattr(new_layout,"excluded_blocks",[])),
        "match_cat_esatti":int(text_df["metodo_abbinamento"].eq("CAT_EXACT").sum()) if not text_df.empty else 0,
        "match_fuzzy_sequenziali":int(text_df["metodo_abbinamento"].eq("SEQUENTIAL_FUZZY").sum()) if not text_df.empty else 0,
        "riusi_automatici_consentiti":int(text_df["decisione_riuso"].eq("RIUSO AUTOMATICO CONSENTITO").sum()) if not text_df.empty else 0,
        "elementi_testo_da_revisionare":review_count,
        "modifiche_sicurezza_testo":int(text_df["stato"].isin(["MODIFICA DI SICUREZZA","NUOVO CONTENUTO DI SICUREZZA"]).sum()) if not text_df.empty else 0,
        "modifiche_tecniche_testo":int(text_df["stato"].isin(["MODIFICA TECNICA","CONTESTO TECNICO MODIFICATO"]).sum()) if not text_df.empty else 0,
        "contenuti_nuovi_testo":int(text_df["stato"].isin(["NUOVO","NUOVO CONTENUTO DI SICUREZZA"]).sum()) if not text_df.empty else 0,
        "contenuti_spostati_certi":int(text_df["stato"].eq("SPOSTATO NEL DOCUMENTO").sum()) if not text_df.empty else 0,
        "corrispondenze_globali_ambigue":int(text_df["stato"].eq("TROVATO ALTROVE — AMBIGUO").sum()) if not text_df.empty else 0,
        "contenuti_non_trovati":int(text_df["stato"].eq("NON TROVATO NEL NUOVO AMBITO").sum()) if not text_df.empty else 0,
        "tabelle_vecchie":len(old_layout.tables),"tabelle_nuove":len(new_layout.tables),
        "modifiche_sicurezza_tabelle":int(table_df["stato"].isin(["MODIFICA DI SICUREZZA","NUOVA RIGA DI SICUREZZA"]).sum()) if not table_df.empty else 0,
        "modifiche_tecniche_tabelle":int(table_df["stato"].eq("MODIFICA TECNICA").sum()) if not table_df.empty else 0,
        "pagine_visuali_vecchie":len(old_layout.selected_pages),"pagine_visuali_nuove":len(new_layout.selected_pages),
        "pagine_visuali_modificate":int(image_df["stato"].isin(["PAGINA VISIVA MODIFICATA","PAGINA VISIVA DA CONTROLLARE","NUOVA PAGINA / AREA VISIVA"]).sum()) if not image_df.empty else 0,
        "azioni_prioritarie":int(queue["priorita"].astype(str).str.startswith(("0","1","2","3")).sum()) if not queue.empty else 0,
    }
    return text_df,table_df,image_df,summary


def _status_color(status: str) -> str:
    return {
        "INVARIATO":"#DCFCE7","VARIAZIONE TERMINOLOGICA APPROVATA":"#E0F2FE","VARIAZIONE TERMINOLOGICA NON APPROVATA":"#E0F2FE",
        "RIFERIMENTO AGGIORNATO":"#E0F2FE","RIFORMULATO":"#E0F2FE","MODIFICA TECNICA":"#FDE68A","CONTESTO TECNICO MODIFICATO":"#FDE68A",
        "MODIFICA DI SICUREZZA":"#FDA4AF","NUOVO CONTENUTO DI SICUREZZA":"#FDA4AF","MODIFICATO":"#FED7AA","NUOVO":"#DBEAFE",
        "SPOSTATO NEL DOCUMENTO":"#DBEAFE","TROVATO ALTROVE — AMBIGUO":"#FEF3C7","NON TROVATO NEL NUOVO AMBITO":"#FECACA",
        "PAGINA VISIVA INVARIATA":"#DCFCE7","PAGINA VISIVA MODIFICATA":"#FDE68A","PAGINA VISIVA DA CONTROLLARE":"#FEF3C7","NUOVA PAGINA / AREA VISIVA":"#DBEAFE",
    }.get(status,"#FFFFFF")


def build_html_report(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, title: str = "ManualSync v0.9") -> str:
    queue=build_review_queue(text_df)
    metrics="".join(f"<div class='metric'><strong>{html.escape(k.replace('_',' ').title())}</strong><br>{html.escape(str(v))}</div>" for k,v in summary.items())
    return f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>
body{{font-family:Arial,sans-serif;margin:0;color:#0f172a;background:#f8fafc}}header{{background:#0f172a;color:white;padding:26px 34px}}main{{padding:24px 34px}}.metrics{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 24px}}.metric{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:10px 14px;min-width:190px}}h2{{margin-top:34px}}.note{{background:#fef3c7;border-radius:10px;padding:14px}}.table-wrap{{overflow:auto;max-height:800px;border:1px solid #cbd5e1}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white}}th,td{{border:1px solid #cbd5e1;padding:8px;vertical-align:top;min-width:140px}}th{{background:#0f766e;color:white;position:sticky;top:0;z-index:1}}td img{{max-width:360px;max-height:280px}}</style></head><body><header><h1>{html.escape(title)}</h1><p>Segmentazione strutturale, ricerca globale con unicità, controllo sicurezza e rendering dei contenuti vettoriali.</p></header><main><div class='metrics'>{metrics}</div><p class='note'><b>Regola v0.9:</b> indici, intestazioni e valori isolati dei grafici non entrano nella coda di traduzione. Un match globale è definito spostamento solo se unico o confermato dal contesto. Le figure vettoriali sono confrontate tramite rendering pagina/area, non dichiarate eliminate perché prive di immagini raster.</p><h2>1. Coda di lavoro</h2>{dataframe_table_html(queue)}<h2>2. Dettaglio CAT, tecnico e sicurezza</h2>{dataframe_table_html(text_df)}<h2>3. Tabelle cella per cella</h2>{dataframe_table_html(table_df)}<h2>4. Confronto visuale renderizzato</h2>{dataframe_table_html(image_df,image_mode=True)}</main></body></html>"""


def save_analysis_outputs(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, output_dir: str | Path, prefix: str = "manualsync_v08") -> dict[str,str]:
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True);queue=build_review_queue(text_df)
    q=out/f"{prefix}_coda_lavoro.csv";t=out/f"{prefix}_dettaglio_testo.csv";tb=out/f"{prefix}_tabelle.csv";im=out/f"{prefix}_visuali.csv"
    queue.to_csv(q,index=False,encoding="utf-8-sig");text_df.to_csv(t,index=False,encoding="utf-8-sig");table_df.to_csv(tb,index=False,encoding="utf-8-sig")
    image_df.drop(columns=["vecchia_immagine","nuova_immagine","estensione_vecchia","estensione_nuova"],errors="ignore").to_csv(im,index=False,encoding="utf-8-sig")
    s=out/f"{prefix}_sintesi.json";s.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    r=out/f"{prefix}_rapporto.html";r.write_text(build_html_report(text_df,table_df,image_df,summary),encoding="utf-8")
    return {"coda":str(q),"testo":str(t),"tabelle":str(tb),"figure":str(im),"sintesi":str(s),"rapporto":str(r)}

# v0.8.1 regression fixes: chart labels, new technical content, strict table pairing.
ENGINE_BUILD = "0.8.1-structural-intelligence-20260805"

_mostly_graphic_value_v080 = _mostly_graphic_value

def _mostly_graphic_value(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if _mostly_graphic_value_v080(t):
        return True
    if re.fullmatch(r"\d+(?:[.,]\d+)?\s*°\s*C\s*\(\s*\d+(?:[.,]\d+)?\s*°\s*F\s*\)(?:\s*-\s*Mounting type.*)?", t, re.I):
        return True
    if re.fullmatch(r"\d+(?:[.,]\d+)?\s*°\s*C\s*\(\s*\d+(?:[.,]\d+)?\s*°\s*F\s*\)(?:\s*-\s*Tipo di montaggio.*)?", t, re.I):
        return True
    return False

_extract_layout_v080 = extract_layout

def extract_layout(pdf_path: str | Path, page_spec: str = "") -> DocumentLayout:
    # Re-run classification after the extended chart-label recognizer.
    layout = _extract_layout_v07(pdf_path, page_spec)
    doc = fitz.open(str(pdf_path)); page_meta={}
    try:
        for rel,pi in enumerate(layout.selected_pages):
            p=doc[pi]; page_meta[rel]={"drawings":len(p.get_drawings()),"images":len(p.get_images(full=True)),"text":p.get_text("text")}
    finally: doc.close()
    kept=[]; excluded=[]; counts=Counter()
    for block in layout.text_blocks:
        st=_classify_structure(block,page_meta.get(block.page,{})); setattr(block,"structure_type",st); counts[st]+=1
        if st in {"RUNNING_HEADER","TOC_NAVIGATION","GRAPHIC_LABEL"}:
            excluded.append({"page":block.page+1,"type":st,"text":block.text}); continue
        kept.append(block)
    for i,b in enumerate(kept): b.index=i
    layout.text_blocks=kept; setattr(layout,"excluded_blocks",excluded); setattr(layout,"structure_counts",dict(counts)); setattr(layout,"page_meta",page_meta)
    return layout

_compare_text_v080 = compare_text

def compare_text(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout, new_source_pdf: str | Path | None = None, approved_terms: set[tuple[str, str]] | None = None) -> pd.DataFrame:
    df = _compare_text_v080(old_layout, old_translation_layout, new_layout, new_source_pdf, approved_terms)
    if df.empty:
        return df
    # New technical prose must not remain medium-risk merely because it has no numeric value.
    for idx,row in df.iterrows():
        if row.get("stato") != "NUOVO":
            continue
        typ=str(row.get("tipo_strutturale",""))
        text=str(row.get("nuovo_originale",""))
        if typ in {"PARAGRAPH","BULLET"} and _tech_concept_set(text):
            df.at[idx,"stato"]="NUOVO CONTENUTO TECNICO"
            df.at[idx,"rischio"]="ALTO"
            df.at[idx,"decisione_riuso"]="BLOCCATO — TRADUZIONE E REVISIONE TECNICA"
            df.at[idx,"richiede_revisione"]="SÌ"
            df.at[idx,"motivo_revisione"]="Nuovo contenuto con concetti tecnici: traduzione e validazione tecnica obbligatorie."
            df.at[idx,"azione"]="BLOCCATO — TRADUZIONE E REVISIONE TECNICA"
    return df


def _table_header(table: TableBlock) -> str:
    return row_text(table.rows[0]) if table.rows else ""


def _strict_table_mapping(old: list[TableBlock], new: list[TableBlock]) -> tuple[dict[int,tuple[int,float]], set[int]]:
    candidates=[]
    for ni,nt in enumerate(new):
        nh=_table_header(nt); ncols=max((len(r) for r in nt.rows),default=0)
        for oi,ot in enumerate(old):
            oh=_table_header(ot); ocols=max((len(r) for r in ot.rows),default=0)
            lex=max(ratio(normalize_cat(oh),normalize_cat(nh)),ratio(normalize(oh),normalize(nh)))/100.0
            col_sim=1-abs(ocols-ncols)/max(1,ocols,ncols)
            row_sim=min(len(ot.rows),len(nt.rows))/max(1,len(ot.rows),len(nt.rows))
            safety_bonus=0.15 if _safety_level(oh)>=2 and _safety_level(nh)>=2 else 0.0
            score=0.68*lex+0.22*col_sim+0.10*row_sim+safety_bonus
            candidates.append((score,ni,oi,lex,col_sim))
    candidates.sort(reverse=True)
    mapping={}; used=set()
    for score,ni,oi,lex,col_sim in candidates:
        if ni in mapping or oi in used: continue
        # Require a genuinely related header; column similarity alone is insufficient.
        if score < 0.64 or lex < 0.62 or col_sim < 0.45: continue
        mapping[ni]=(oi,score); used.add(oi)
    return mapping,used


def compare_tables(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    old,new=old_layout.tables,new_layout.tables
    table_map,used_old=_strict_table_mapping(old,new)
    translation_map=map_translation_tables(old,old_translation_layout.tables)
    rows=[]
    for ni,nt in enumerate(new):
        if ni not in table_map:
            header=_table_header(nt)
            rows.append({"tabella_vecchia":"","tabella_nuova":ni+1,"riga":"TABELLA","stato":"NUOVA TABELLA","rischio":"ALTO" if _safety_level(header)>=2 else "MEDIO","vecchia_riga":"","nuova_riga":header,"celle_modificate":f"Tabella nuova: {len(nt.rows)} righe, {max((len(r) for r in nt.rows),default=0)} colonne","vecchia_traduzione":"","affidabilita_traduzione":0.0,"azione":"REVISIONE STRUTTURA E TRADUZIONE"})
            continue
        oi,_=table_map[ni];ot=old[oi]
        row_map,_=align_rows(ot.rows,nt.rows);used_rows=set()
        for nri,nr in enumerate(nt.rows):
            if nri not in row_map:
                ntxt=row_text(nr);sl=_safety_level(ntxt)
                rows.append({"tabella_vecchia":oi+1,"tabella_nuova":ni+1,"riga":nri+1,"stato":"NUOVA RIGA DI SICUREZZA" if sl>=2 else "NUOVA RIGA","rischio":"CRITICO" if sl>=3 else ("ALTO" if sl>=2 or extract_technical_values(ntxt) or extract_codes(ntxt) else "MEDIO"),"vecchia_riga":"","nuova_riga":ntxt,"celle_modificate":"Intera riga nuova","vecchia_traduzione":"","affidabilita_traduzione":0.0,"azione":"BLOCCATO — REVISIONE SICUREZZA" if sl>=2 else "TRADUZIONE E REVISIONE"});continue
            ori,_=row_map[nri];used_rows.add(ori);orow=ot.rows[ori];otxt=row_text(orow);ntxt=row_text(nr);sl=max(_safety_level(otxt),_safety_level(ntxt));exact=normalize_cat(otxt)==normalize_cat(ntxt)
            if exact:status,risk,action="INVARIATO","BASSO","RIUSO STRUTTURA"
            elif sl>=2:status,risk,action="MODIFICA DI SICUREZZA","CRITICO" if sl>=3 else "ALTO","BLOCCATO — REVISIONE SICUREZZA"
            elif set(extract_technical_values(otxt))!=set(extract_technical_values(ntxt)) or set(extract_codes(otxt))!=set(extract_codes(ntxt)):status,risk,action="MODIFICA TECNICA","ALTO","BLOCCATO — REVISIONE TECNICA"
            else:status,risk,action="MODIFICATO","MEDIO","REVISIONE"
            tr,conf=translation_map.get((oi,ori),("",0.0))
            rows.append({"tabella_vecchia":oi+1,"tabella_nuova":ni+1,"riga":nri+1,"stato":status,"rischio":risk,"vecchia_riga":otxt,"nuova_riga":ntxt,"celle_modificate":_cell_diffs(orow,nr),"vecchia_traduzione":tr if conf>=60 else "","affidabilita_traduzione":conf,"azione":action})
        for ori,orow in enumerate(ot.rows):
            if ori not in used_rows:
                rows.append({"tabella_vecchia":oi+1,"tabella_nuova":ni+1,"riga":f"old-{ori+1}","stato":"RIGA NON TROVATA NELL'AMBITO","rischio":"ALTO" if _safety_level(row_text(orow))>=2 else "MEDIO","vecchia_riga":row_text(orow),"nuova_riga":"","celle_modificate":"Riga non abbinata","vecchia_traduzione":"","affidabilita_traduzione":0.0,"azione":"VERIFICA RIMOZIONE / SPOSTAMENTO"})
    for oi,ot in enumerate(old):
        if oi not in used_old:
            rows.append({"tabella_vecchia":oi+1,"tabella_nuova":"","riga":"TABELLA","stato":"TABELLA NON TROVATA NELL'AMBITO","rischio":"ALTO" if _safety_level(_table_header(ot))>=2 else "MEDIO","vecchia_riga":_table_header(ot),"nuova_riga":"","celle_modificate":f"Tabella non abbinata: {len(ot.rows)} righe","vecchia_traduzione":"","affidabilita_traduzione":0.0,"azione":"VERIFICA RIMOZIONE / SPOSTAMENTO"})
    return pd.DataFrame(rows)

# v0.8.2 conservative global uniqueness and corrected summary counters.
ENGINE_BUILD = "0.8.2-structural-intelligence-20260805"
_search_unmatched_globally_v081 = search_unmatched_globally

def search_unmatched_globally(new_source_pdf: str | Path, old_blocks: list[TextBlock], unmatched_old: list[int], selected_pages: list[int]) -> dict[int, dict]:
    results = _search_unmatched_globally_v081(new_source_pdf, old_blocks, unmatched_old, selected_pages)
    for oi, hit in results.items():
        occurrences = int(hit.get("occurrences", 0) or 0)
        context = float(hit.get("context_score", 0.0) or 0.0)
        if occurrences > 1 and context < 1.0:
            hit["certainty"] = "AMBIGUOUS"
        if len(_text_tokens(old_blocks[oi].text)) <= 3 and occurrences > 1:
            hit["certainty"] = "AMBIGUOUS"
    return results

_analyze_layouts_v081 = analyze_layouts

def analyze_layouts(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path, old_pages: str = "", translation_pages: str = "", new_pages: str = "", start_anchor: str = "", terminology_glossary: str | Path | None = None, full_new_source_pdf: str | Path | None = None) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame,dict]:
    text_df, table_df, image_df, summary = _analyze_layouts_v081(
        old_source_pdf, old_translation_pdf, new_source_pdf,
        old_pages, translation_pages, new_pages, start_anchor,
        terminology_glossary, full_new_source_pdf,
    )
    summary["build_motore"] = ENGINE_BUILD
    if not text_df.empty:
        summary["contenuti_nuovi_testo"] = int(text_df["stato"].isin(["NUOVO","NUOVO CONTENUTO TECNICO","NUOVO CONTENUTO DI SICUREZZA"]).sum())
        summary["nuovi_contenuti_tecnici"] = int(text_df["stato"].eq("NUOVO CONTENUTO TECNICO").sum())
        summary["contenuti_spostati_certi"] = int(text_df["stato"].eq("SPOSTATO NEL DOCUMENTO").sum())
        summary["corrispondenze_globali_ambigue"] = int(text_df["stato"].eq("TROVATO ALTROVE — AMBIGUO").sum())
    return text_df, table_df, image_df, summary


# ============================================================================
# ManualSync v0.9 overrides — section-aware alignment and bilingual safety gate
# ============================================================================
ENGINE_BUILD = "0.9.0-section-aware-20260805"

_normalize_v082 = normalize
_extract_layout_v082 = extract_layout
_hierarchical_align_text_v082 = hierarchical_align_text
_compare_text_v082 = compare_text
_compare_tables_v082 = compare_tables
_analyze_layouts_v082 = analyze_layouts

_LANGUAGE_NAV_CODES = {
    "EN","DA","DE","ES","FI","FR","IT","NL","PL","BR","RU","SV","TR","UK",
    "PT","CS","HU","RO","EL","ZH","JA","KO","NO","ET","LV","LT","SK","SL","HR","BG"
}
_LANGUAGE_FOOTER_RE = re.compile(
    r"^\s*\d+\s+(?:EN|IT|DE|FR|ES|DA|FI|NL|PL|BR|RU|SV|TR|UK)\s*[–—-]\s*.+?(?:instructions|istruzioni|manual|guida)\s*$",
    re.I,
)
_INTERNAL_STEP_RE = re.compile(r"^\s*\d{1,3}[.)]\s+\S")
_ALNUM_CODE_RE = re.compile(r"\b(?=[A-Z0-9-]{2,}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b", re.I)


def normalize(text: str) -> str:
    """v0.9 semantic normalization: ignores layout-driven numbering and word wrapping."""
    raw = (text or "").replace("\u00ad", "")
    # Remove PDF line-break hyphenation and canonicalize in-word hyphens for semantic comparison.
    raw = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", raw)
    raw = re.sub(r"^\s*\d{1,3}[.)]\s+(?=\D)", "", raw)
    normalized = _normalize_v082(raw)
    normalized = re.sub(r"(?<=\w)-(?=\w)", "", normalized)
    return normalized


def _split_internal_numbered_blocks(layout: DocumentLayout) -> DocumentLayout:
    """Split a text block when a numbered heading starts after unrelated text."""
    split_blocks: list[TextBlock] = []
    for block in layout.text_blocks:
        lines = [line.strip() for line in block.text.splitlines() if line.strip()]
        boundaries = [i for i, line in enumerate(lines) if i > 0 and _INTERNAL_STEP_RE.match(line)]
        if not boundaries:
            split_blocks.append(block)
            continue
        starts = [0] + boundaries
        ends = boundaries + [len(lines)]
        total = max(1, len(lines))
        y0, y1 = block.bbox[1], block.bbox[3]
        for part_index, (a, b) in enumerate(zip(starts, ends)):
            text = "\n".join(lines[a:b]).strip()
            if not text:
                continue
            py0 = y0 + (y1-y0) * a/total
            py1 = y0 + (y1-y0) * b/total
            heading = bool(_INTERNAL_STEP_RE.match(lines[a])) or (part_index == 0 and block.heading)
            nb = TextBlock(
                index=len(split_blocks),
                page=block.page,
                bbox=(block.bbox[0], py0, block.bbox[2], py1),
                text=text,
                normalized=normalize(text),
                cat_normalized=normalize_cat(text),
                technical_values=extract_technical_values(text),
                codes=extract_codes(text),
                page_refs=extract_page_refs(text),
                section_ref=extract_section_ref(text),
                heading=heading,
                font_size=block.font_size,
            )
            setattr(nb, "structure_type", "HEADING" if heading else getattr(block, "structure_type", "PARAGRAPH"))
            split_blocks.append(nb)
    layout.text_blocks = split_blocks
    return layout


def _nav_noise_pages(blocks: list[TextBlock]) -> set[int]:
    by_page: dict[int, set[str]] = defaultdict(set)
    for b in blocks:
        token = re.sub(r"\s+", "", b.text.upper())
        if token in _LANGUAGE_NAV_CODES:
            by_page[b.page].add(token)
    return {page for page, codes in by_page.items() if len(codes) >= 4}


def _graphic_token_block(text: str) -> bool:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if not compact:
        return True
    tokens = re.findall(r"\S+", compact)
    if len(tokens) <= 10:
        symbolish = sum(bool(re.fullmatch(r"(?:×?\d+[A-Z]?|[A-Z]\d+|[-–—]|[A-Z]{1,3})", t)) for t in tokens)
        if tokens and symbolish / len(tokens) >= 0.8:
            return True
    return False


def extract_layout(pdf_path: str | Path, page_spec: str = "") -> DocumentLayout:
    """v0.9: remove multilingual navigation chrome and split embedded headings."""
    layout = _extract_layout_v082(pdf_path, page_spec)
    layout = _split_internal_numbered_blocks(layout)
    nav_pages = _nav_noise_pages(layout.text_blocks)
    _doc_v09 = fitz.open(str(pdf_path))
    try:
        page_widths = {rel: float(_doc_v09[orig].rect.width) for rel, orig in enumerate(layout.selected_pages)}
    finally:
        _doc_v09.close()
    kept: list[TextBlock] = []
    excluded = list(getattr(layout, "excluded_blocks", []))
    counts = Counter(getattr(layout, "structure_counts", {}))
    for block in layout.text_blocks:
        token = re.sub(r"\s+", "", block.text.upper())
        reason = ""
        page_width = page_widths.get(block.page, 0.0)
        marginal_code = token in _LANGUAGE_NAV_CODES and (
            block.bbox[0] < 28 or (page_width and block.bbox[2] > page_width - 28)
        )
        if (block.page in nav_pages and token in _LANGUAGE_NAV_CODES) or marginal_code:
            reason = "LANGUAGE_NAVIGATION"
        elif _LANGUAGE_FOOTER_RE.match(re.sub(r"\s+", " ", block.text.strip())):
            reason = "RUNNING_FOOTER"
        elif _graphic_token_block(block.text) and getattr(block, "structure_type", "") not in {"SAFETY","HEADING"}:
            page_meta = getattr(layout, "page_meta", {}).get(block.page, {})
            if page_meta.get("drawings", 0) > 10:
                reason = "GRAPHIC_TOKEN"
        if reason:
            excluded.append({"page": block.page + 1, "type": reason, "text": block.text})
            counts[reason] += 1
            continue
        block.index = len(kept)
        kept.append(block)
    layout.text_blocks = kept
    setattr(layout, "excluded_blocks", excluded)
    setattr(layout, "structure_counts", dict(counts))
    return layout


def _alignment_cat_key(block: TextBlock) -> str:
    key = block.cat_normalized
    if block.heading or _INTERNAL_STEP_RE.match(block.text.strip()):
        key = re.sub(r"^\s*\d{1,3}[.)]\s+", "", key)
    return key.strip()


def _alignment_semantic_key(block: TextBlock) -> str:
    key = block.normalized
    if block.heading or _INTERNAL_STEP_RE.match(block.text.strip()):
        key = re.sub(r"^\s*\d{1,3}[.)]\s+", "", key)
    return key.strip()


def hierarchical_align_text(old: list[TextBlock], new: list[TextBlock]) -> tuple[dict[int, tuple[int, float, str]], list[int]]:
    """v0.9 deterministic alignment also treats changed step numbers as structural context."""
    mapping: dict[int, tuple[int, float, str]] = {}
    cat_pairs = _lcs_pairs(old, new, _alignment_cat_key)
    for oi, ni in cat_pairs:
        method = "CAT_EXACT" if old[oi].cat_normalized == new[ni].cat_normalized else "STRUCTURAL_EXACT"
        mapping[ni] = (oi, 1.0, method)

    semantic_pairs: list[tuple[int, int]] = []
    for os, oe, ns, ne in _gap_ranges(cat_pairs, len(old), len(new)):
        local = _lcs_pairs(old[os:oe], new[ns:ne], _alignment_semantic_key)
        semantic_pairs.extend((os + oi, ns + ni) for oi, ni in local)
    for oi, ni in semantic_pairs:
        if ni not in mapping:
            mapping[ni] = (oi, 0.995, "NORMALIZED_EQUIVALENCE")

    anchors = sorted((oi, ni) for ni, (oi, _, _) in mapping.items())
    for os, oe, ns, ne in _gap_ranges(anchors, len(old), len(new)):
        mapping.update(_align_gap_v08(old[os:oe], new[ns:ne], os, ns))
    used_old = {oi for oi, _, _ in mapping.values()}
    return mapping, sorted(set(range(len(old))) - used_old)


def _translation_alignment_suspicious(source_text: str, translation_text: str) -> tuple[bool, str]:
    if not source_text or not translation_text:
        return True, "Traduzione assente."
    source_chars = max(1, len(re.sub(r"\s+", " ", source_text.strip())))
    target_chars = len(re.sub(r"\s+", " ", translation_text.strip()))
    ratio_len = target_chars / source_chars
    source_codes = set(_ALNUM_CODE_RE.findall(source_text.upper()))
    target_codes = set(_ALNUM_CODE_RE.findall(translation_text.upper()))
    introduced_codes = target_codes - source_codes
    source_words = len(re.findall(r"\b\w+\b", source_text))
    if source_words <= 10 and ratio_len > 2.2 and introduced_codes:
        return True, f"Traduzione sproporzionata e introduce codici/valori non presenti: {', '.join(sorted(introduced_codes))}."
    if ratio_len < 0.35 or ratio_len > 3.2:
        return True, f"Rapporto di lunghezza bilingue anomalo: {ratio_len:.2f}."
    return False, ""


def compare_text(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout, new_source_pdf: str | Path | None = None, approved_terms: set[tuple[str, str]] | None = None) -> pd.DataFrame:
    df = _compare_text_v082(old_layout, old_translation_layout, new_layout, new_source_pdf, approved_terms)
    if df.empty:
        return df
    blocked = 0
    for idx, row in df.iterrows():
        if row.get("decisione_riuso") != "RIUSO AUTOMATICO CONSENTITO":
            continue
        suspicious, reason = _translation_alignment_suspicious(
            str(row.get("vecchio_originale", "")),
            str(row.get("vecchia_traduzione", "")),
        )
        if suspicious:
            blocked += 1
            df.at[idx, "decisione_riuso"] = "BLOCCATO — CONTROLLO ALLINEAMENTO BILINGUE"
            df.at[idx, "richiede_revisione"] = "SÌ"
            df.at[idx, "rischio"] = "ALTO"
            df.at[idx, "motivo_revisione"] = (
                "Il testo sorgente è invariato, ma l'abbinamento con la vecchia traduzione è sospetto. " + reason
            )
            df.at[idx, "azione"] = "BLOCCATO — CONTROLLO ALLINEAMENTO BILINGUE"
    setattr(df, "_v09_bilingual_blocks", blocked)
    return df


def _semantic_row_equal(old_text: str, new_text: str) -> bool:
    return bool(normalize(old_text)) and normalize(old_text) == normalize(new_text)


def compare_tables(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    df = _compare_tables_v082(old_layout, old_translation_layout, new_layout)
    if df.empty:
        return df
    for idx, row in df.iterrows():
        if row.get("stato") != "MODIFICATO":
            continue
        old_text, new_text = str(row.get("vecchia_riga", "")), str(row.get("nuova_riga", ""))
        if _semantic_row_equal(old_text, new_text):
            df.at[idx, "stato"] = "INVARIATO — SOLO IMPAGINAZIONE"
            df.at[idx, "rischio"] = "BASSO"
            df.at[idx, "azione"] = "RIUSO STRUTTURA"
            df.at[idx, "celle_modificate"] = "Differenze dovute a sillabazione o ritorni a capo."
    return df


def analyze_layouts(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path, old_pages: str = "", translation_pages: str = "", new_pages: str = "", start_anchor: str = "", terminology_glossary: str | Path | None = None, full_new_source_pdf: str | Path | None = None) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame,dict]:
    text_df, table_df, image_df, summary = _analyze_layouts_v082(
        old_source_pdf, old_translation_pdf, new_source_pdf,
        old_pages, translation_pages, new_pages, start_anchor,
        terminology_glossary, full_new_source_pdf,
    )
    summary["build_motore"] = ENGINE_BUILD
    summary["riusi_bloccati_allineamento_bilingue"] = int(
        text_df["decisione_riuso"].eq("BLOCCATO — CONTROLLO ALLINEAMENTO BILINGUE").sum()
    ) if not text_df.empty else 0
    summary["riusi_automatici_consentiti"] = int(
        text_df["decisione_riuso"].eq("RIUSO AUTOMATICO CONSENTITO").sum()
    ) if not text_df.empty else 0
    summary["elementi_strutturali_esclusi_vecchi"] = len(getattr(extract_layout(old_source_pdf, old_pages), "excluded_blocks", []))
    summary["elementi_strutturali_esclusi_nuovi"] = len(getattr(extract_layout(new_source_pdf, new_pages), "excluded_blocks", []))
    return text_df, table_df, image_df, summary


# ============================================================================
# ManualSync v0.10 overrides — geometric table reconstruction and cell audit
# ============================================================================
ENGINE_BUILD = "0.10.0-cell-intelligence-20260805"

_extract_layout_v09 = extract_layout
_compare_tables_v09 = compare_tables
_analyze_layouts_v09 = analyze_layouts
_build_html_report_v09 = build_html_report

_TABLE_NAV_CODES = _LANGUAGE_NAV_CODES
_TABLE_DEVICE_CODE_RE = re.compile(r"\bR\d+(?:\s*v\d+)?\b", re.I)
_TABLE_RANGE_RE = re.compile(r"\bR\d+\s*(?:…|\.\.\.|[-–—])\s*R\d+\b", re.I)
_TABLE_SYSTEM_CODE_RE = re.compile(r"\b(?:EMC|VAR|TN-S|TN-C|TN-C-S|TT|IT|AC|DC|IEC|IP\d{2})\b", re.I)
_TABLE_TECHNICAL_TERMS_RE = re.compile(
    r"\b(?:residual current device|ground leakage|interruttore differenziale|corrente di dispersione|"
    r"grounded|grounding|messa a terra|screw|screws|vite|viti|varistor|varistore|filter|filtro|"
    r"frame size|telaio|supply system|sistema di alimentazione)\b",
    re.I,
)


def _clean_table_cell(text: str) -> str:
    raw = (text or "").replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl")
    lines = []
    for line in raw.splitlines():
        compact = re.sub(r"\s+", " ", line).strip()
        if not compact:
            continue
        token = re.sub(r"\s+", "", compact.upper())
        if token in _TABLE_NAV_CODES:
            continue
        lines.append(compact)
    raw = "\n".join(lines)
    # Join words split only because of line wrapping (un-\ngrounded -> ungrounded).
    raw = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", raw)
    raw = re.sub(r"\s*\n\s*", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"\s+([,.;:)])", r"\1", raw)
    raw = re.sub(r"([(])\s+", r"\1", raw)
    raw = raw.replace(". VAR", " VAR")
    return raw


def _page_lines_for_table(page: fitz.Page, bbox: tuple[float, float, float, float]) -> list[tuple[float,float,float,float,str]]:
    out: list[tuple[float,float,float,float,str]] = []
    x0b, y0b, x1b, y1b = bbox
    page_dict = page.get_text("dict", sort=True)
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s.get("text", "") for s in spans).strip()
            lb = line.get("bbox") or (
                min(float(s["bbox"][0]) for s in spans),
                min(float(s["bbox"][1]) for s in spans),
                max(float(s["bbox"][2]) for s in spans),
                max(float(s["bbox"][3]) for s in spans),
            )
            x0, y0, x1, y1 = map(float, lb)
            cy = (y0 + y1) / 2
            if cy < y0b - 3 or cy > y1b + 3 or x1 < x0b - 9 or x0 > x1b + 4:
                continue
            out.append((x0, y0, x1, y1, text))
    return out


def _cluster_x_starts(values: list[float], tolerance: float = 4.0) -> list[tuple[float,int]]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [(sum(group)/len(group), len(group)) for group in groups]


def _reference_column_starts(table) -> list[float]:
    # A merged first row may contain only one cell. Choose the row with most cells.
    best_cells = []
    for row in getattr(table, "rows", []):
        cells = [cell for cell in row.cells if cell is not None]
        if len(cells) > len(best_cells):
            best_cells = cells
    if len(best_cells) >= getattr(table, "col_count", 0):
        return [float(cell[0]) for cell in best_cells[:table.col_count]]
    # Fallback to global cell left edges.
    starts = sorted({round(float(cell[0]), 3) for cell in getattr(table, "cells", []) if cell is not None})
    return starts[:getattr(table, "col_count", len(starts))]


def _choose_column_starts(candidates: list[tuple[float,int]], expected: list[float], count: int) -> list[float]:
    if count <= 0:
        return []
    candidates = sorted(candidates)
    if len(candidates) < count:
        return expected[:count]
    from functools import lru_cache

    @lru_cache(None)
    def solve(column: int, start: int):
        if column == count:
            return 0.0, ()
        remaining = count - column
        best = (float("inf"), ())
        for idx in range(start, len(candidates) - remaining + 1):
            x, frequency = candidates[idx]
            reference = expected[column] if column < len(expected) else x
            cost = abs(x - reference) / 50.0 - 0.18 * math.log1p(frequency)
            tail_cost, tail = solve(column + 1, idx + 1)
            if cost + tail_cost < best[0]:
                best = (cost + tail_cost, (idx,) + tail)
        return best

    _, path = solve(0, 0)
    if len(path) != count:
        return expected[:count]
    return [candidates[idx][0] for idx in path]


def _reconstruct_table(page: fitz.Page, table, index: int, rel_page: int) -> TableBlock:
    bbox = tuple(float(v) for v in table.bbox)
    line_items = _page_lines_for_table(page, bbox)
    col_count = int(getattr(table, "col_count", 0) or 0)
    expected = _reference_column_starts(table)
    candidates = _cluster_x_starts([item[0] for item in line_items])
    starts = _choose_column_starts(candidates, expected, col_count)
    if len(starts) != col_count:
        rows = [[_clean_table_cell(cell or "") for cell in row] for row in table.extract()]
        rows = [row for row in rows if any(row)]
        return TableBlock(index, rel_page, bbox, rows)

    midpoints = [(starts[i] + starts[i+1]) / 2 for i in range(len(starts)-1)]
    rebuilt: list[list[str]] = []
    for row in table.rows:
        valid_cells = [cell for cell in row.cells if cell is not None]
        if not valid_cells:
            continue
        row_y0 = min(float(cell[1]) for cell in valid_cells)
        row_y1 = max(float(cell[3]) for cell in valid_cells)
        row_lines = [
            item for item in line_items
            if row_y0 - 1 <= (item[1] + item[3]) / 2 <= row_y1 + 1
        ]
        row_lines.sort(key=lambda item: (item[1], item[0]))
        cells: list[list[str]] = [[] for _ in range(col_count)]
        for item in row_lines:
            column = 0
            while column < len(midpoints) and item[0] >= midpoints[column]:
                column += 1
            cells[column].append(item[4])
        cleaned = [_clean_table_cell("\n".join(parts)) for parts in cells]
        if any(cleaned):
            rebuilt.append(cleaned)
    return TableBlock(index, rel_page, bbox, rebuilt)


def _reconstruct_tables_geometrically(pdf_path: str | Path, selected_pages: list[int]) -> list[TableBlock]:
    doc = fitz.open(str(pdf_path))
    rebuilt: list[TableBlock] = []
    try:
        for rel_page, page_index in enumerate(selected_pages):
            page = doc[page_index]
            try:
                tables = page.find_tables().tables
            except Exception:
                tables = []
            for table in tables:
                rebuilt.append(_reconstruct_table(page, table, len(rebuilt), rel_page))
    finally:
        doc.close()
    return rebuilt


def extract_layout(pdf_path: str | Path, page_spec: str = "") -> DocumentLayout:
    layout = _extract_layout_v09(pdf_path, page_spec)
    rebuilt = _reconstruct_tables_geometrically(pdf_path, layout.selected_pages)
    if rebuilt:
        layout.tables = rebuilt
    setattr(layout, "tables_reconstructed_geometrically", len(rebuilt))
    return layout


def _table_semantic(text: str) -> str:
    return normalize(_clean_table_cell(text))


def _table_cat(text: str) -> str:
    return normalize_cat(_clean_table_cell(text))


def _table_tokens(text: str) -> tuple[str, ...]:
    cleaned = _clean_table_cell(text)
    tokens = set(extract_technical_values(cleaned))
    tokens.update(extract_codes(cleaned))
    tokens.update(x.upper().replace(" ", "") for x in _TABLE_RANGE_RE.findall(cleaned))
    tokens.update(re.sub(r"\s+", "", x.upper()) for x in _TABLE_DEVICE_CODE_RE.findall(cleaned))
    tokens.update(x.upper() for x in _TABLE_SYSTEM_CODE_RE.findall(cleaned))
    return tuple(sorted(tokens))


def _table_cell_is_technical(text: str) -> bool:
    return bool(_table_tokens(text) or _TABLE_TECHNICAL_TERMS_RE.search(_clean_table_cell(text)))


def _row_key(row: list[str]) -> str:
    if not row:
        return ""
    first = _clean_table_cell(row[0])
    ranges = _TABLE_RANGE_RE.findall(first)
    devices = _TABLE_DEVICE_CODE_RE.findall(first)
    if ranges or devices:
        return "|".join(sorted(set(re.sub(r"\s+", "", x.upper()) for x in ranges + devices)))
    return _table_semantic(first)


def _align_table_rows(old_rows: list[list[str]], new_rows: list[list[str]]) -> tuple[dict[int,int], set[int]]:
    mapping: dict[int,int] = {}
    used_old: set[int] = set()
    old_keys: dict[str,list[int]] = defaultdict(list)
    for oi, row in enumerate(old_rows):
        key = _row_key(row)
        if key:
            old_keys[key].append(oi)
    # Unique structural keys first.
    for ni, row in enumerate(new_rows):
        key = _row_key(row)
        candidates = [oi for oi in old_keys.get(key, []) if oi not in used_old]
        if len(candidates) == 1:
            mapping[ni] = candidates[0]
            used_old.add(candidates[0])
    # Header row by position when table shape is compatible.
    if old_rows and new_rows and 0 not in mapping and 0 not in used_old:
        mapping[0] = 0
        used_old.add(0)
    # Monotonic semantic fallback.
    last_old = -1
    for ni in range(len(new_rows)):
        if ni in mapping:
            last_old = max(last_old, mapping[ni])
            continue
        best = None
        new_text = row_text(new_rows[ni])
        for oi in range(last_old + 1, len(old_rows)):
            if oi in used_old:
                continue
            score = ratio(_table_semantic(row_text(old_rows[oi])), _table_semantic(new_text))
            if best is None or score > best[0]:
                best = (score, oi)
        if best and best[0] >= 62:
            mapping[ni] = best[1]
            used_old.add(best[1])
            last_old = best[1]
    return mapping, set(range(len(old_rows))) - used_old


def _align_translation_table(old_table: TableBlock, translation_tables: list[TableBlock]) -> tuple[TableBlock | None, float]:
    best: tuple[float, TableBlock | None] = (-1.0, None)
    for table in translation_tables:
        row_score = 1 - abs(len(old_table.rows) - len(table.rows)) / max(1, len(old_table.rows), len(table.rows))
        old_cols = max((len(r) for r in old_table.rows), default=0)
        tr_cols = max((len(r) for r in table.rows), default=0)
        col_score = 1 - abs(old_cols - tr_cols) / max(1, old_cols, tr_cols)
        page_score = 1.0 if old_table.page == table.page else max(0.0, 1 - abs(old_table.page-table.page)*0.25)
        score = 0.45*row_score + 0.4*col_score + 0.15*page_score
        if score > best[0]:
            best = (score, table)
    return best[1], max(0.0, min(100.0, best[0]*100))


def _translation_row_map(old_rows: list[list[str]], tr_rows: list[list[str]]) -> dict[int, tuple[int,float]]:
    result: dict[int, tuple[int,float]] = {}
    tr_keys: dict[str,list[int]] = defaultdict(list)
    for ti, row in enumerate(tr_rows):
        key = _row_key(row)
        if key:
            tr_keys[key].append(ti)
    used: set[int] = set()
    for oi, row in enumerate(old_rows):
        key = _row_key(row)
        candidates = [ti for ti in tr_keys.get(key, []) if ti not in used]
        if len(candidates) == 1:
            result[oi] = (candidates[0], 98.0)
            used.add(candidates[0])
    if old_rows and tr_rows and 0 not in result:
        result[0] = (0, 90.0)
        used.add(0)
    # Same-order fallback is valid only when table shape is highly compatible.
    if len(old_rows) == len(tr_rows):
        for oi in range(len(old_rows)):
            if oi not in result and oi not in used:
                result[oi] = (oi, 86.0)
                used.add(oi)
    return result


def _cell_headers(table: TableBlock, column: int) -> str:
    if not table.rows or column >= len(table.rows[0]):
        return ""
    return _clean_table_cell(table.rows[0][column])


def _cell_change(old_cell: str, new_cell: str) -> tuple[str,str,str,str,float]:
    old_clean, new_clean = _clean_table_cell(old_cell), _clean_table_cell(new_cell)
    if not old_clean and new_clean:
        safety = _safety_level(new_clean)
        if safety >= 2:
            return "NUOVA CELLA DI SICUREZZA", "CRITICO" if safety >= 3 else "ALTO", "BLOCCATO — REVISIONE SICUREZZA", "NEW", 0.0
        if _table_cell_is_technical(new_clean):
            return "NUOVA CELLA TECNICA", "ALTO", "BLOCCATO — REVISIONE TECNICA", "NEW", 0.0
        return "NUOVA CELLA", "MEDIO", "TRADUZIONE E REVISIONE", "NEW", 0.0
    if old_clean and not new_clean:
        return "CELLA NON TROVATA", "MEDIO", "VERIFICA RIMOZIONE / SPOSTAMENTO", "NOT FOUND", 0.0
    cat_score = ratio(_table_cat(old_clean), _table_cat(new_clean))
    if _table_cat(old_clean) == _table_cat(new_clean):
        return "INVARIATO", "BASSO", "RIUSO CELLA CONSENTITO", "100% MATCH", 100.0
    if _table_semantic(old_clean) == _table_semantic(new_clean):
        return "INVARIATO — SOLO IMPAGINAZIONE", "BASSO", "RIUSO CELLA CONSENTITO", "CONTEXT MATCH", 100.0
    safety = max(_safety_level(old_clean), _safety_level(new_clean))
    if safety >= 2:
        return "MODIFICA DI SICUREZZA", "CRITICO" if safety >= 3 else "ALTO", "BLOCCATO — REVISIONE SICUREZZA", "SAFETY CHANGE", round(cat_score,1)
    if set(_table_tokens(old_clean)) != set(_table_tokens(new_clean)):
        return "MODIFICA TECNICA", "ALTO", "BLOCCATO — REVISIONE TECNICA", "TECHNICAL CHANGE", round(cat_score,1)
    return "MODIFICATO", "MEDIO", "REVISIONE LINGUISTICA", "FUZZY", round(cat_score,1)


def compare_tables(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    old_tables, new_tables = old_layout.tables, new_layout.tables
    table_map, _ = align_tables(old_tables, new_tables)
    used_old_tables: set[int] = set()
    result: list[dict] = []

    for new_index, new_table in enumerate(new_tables):
        if new_index not in table_map:
            for new_row_index, new_row in enumerate(new_table.rows):
                for column, new_cell in enumerate(new_row):
                    if not _clean_table_cell(new_cell):
                        continue
                    status, risk, action, match, score = _cell_change("", new_cell)
                    result.append({
                        "tabella_vecchia":"", "tabella_nuova":new_index+1,
                        "riga_vecchia":"", "riga_nuova":new_row_index+1, "colonna":column+1,
                        "chiave_riga":_row_key(new_row), "intestazione_vecchia":"", "intestazione_nuova":_cell_headers(new_table,column),
                        "stato":status, "rischio":risk, "categoria_match":match, "similarita":score,
                        "vecchia_cella":"", "nuova_cella":_clean_table_cell(new_cell), "vecchia_traduzione_cella":"",
                        "affidabilita_traduzione":0.0, "valori_codici_vecchi":"", "valori_codici_nuovi":", ".join(_table_tokens(new_cell)),
                        "decisione_cella":action, "motivo":"Tabella o riga non abbinata nel vecchio documento.",
                    })
            continue

        old_index, _ = table_map[new_index]
        used_old_tables.add(old_index)
        old_table = old_tables[old_index]
        row_map, unmatched_old_rows = _align_table_rows(old_table.rows, new_table.rows)
        translation_table, table_translation_conf = _align_translation_table(old_table, old_translation_layout.tables)
        tr_row_map = _translation_row_map(old_table.rows, translation_table.rows) if translation_table else {}

        for new_row_index, new_row in enumerate(new_table.rows):
            if new_row_index not in row_map:
                for column, new_cell in enumerate(new_row):
                    if not _clean_table_cell(new_cell):
                        continue
                    status, risk, action, match, score = _cell_change("", new_cell)
                    result.append({
                        "tabella_vecchia":old_index+1, "tabella_nuova":new_index+1,
                        "riga_vecchia":"", "riga_nuova":new_row_index+1, "colonna":column+1,
                        "chiave_riga":_row_key(new_row), "intestazione_vecchia":"", "intestazione_nuova":_cell_headers(new_table,column),
                        "stato":status, "rischio":risk, "categoria_match":match, "similarita":score,
                        "vecchia_cella":"", "nuova_cella":_clean_table_cell(new_cell), "vecchia_traduzione_cella":"",
                        "affidabilita_traduzione":0.0, "valori_codici_vecchi":"", "valori_codici_nuovi":", ".join(_table_tokens(new_cell)),
                        "decisione_cella":action, "motivo":"Nuova riga o cella nella tabella abbinata.",
                    })
                continue

            old_row_index = row_map[new_row_index]
            old_row = old_table.rows[old_row_index]
            translation_row = []
            translation_conf = 0.0
            if translation_table and old_row_index in tr_row_map:
                tr_index, row_conf = tr_row_map[old_row_index]
                if tr_index < len(translation_table.rows):
                    translation_row = translation_table.rows[tr_index]
                    translation_conf = min(table_translation_conf, row_conf)
            width = max(len(old_row), len(new_row))
            for column in range(width):
                old_cell = old_row[column] if column < len(old_row) else ""
                new_cell = new_row[column] if column < len(new_row) else ""
                if not _clean_table_cell(old_cell) and not _clean_table_cell(new_cell):
                    continue
                tr_cell = translation_row[column] if column < len(translation_row) else ""
                status, risk, action, match, score = _cell_change(old_cell, new_cell)
                suspicious = False
                suspicious_reason = ""
                if action == "RIUSO CELLA CONSENTITO":
                    suspicious, suspicious_reason = _translation_alignment_suspicious(_clean_table_cell(old_cell), _clean_table_cell(tr_cell))
                    if not tr_cell or translation_conf < 80 or suspicious:
                        action = "BLOCCATO — CONTROLLO TRADUZIONE CELLA"
                        risk = "ALTO" if suspicious else "MEDIO"
                reason = "Confronto cella per cella."
                if suspicious_reason:
                    reason += " " + suspicious_reason
                elif action == "BLOCCATO — CONTROLLO TRADUZIONE CELLA":
                    reason += f" Affidabilità traduzione {translation_conf:.1f}%."
                result.append({
                    "tabella_vecchia":old_index+1, "tabella_nuova":new_index+1,
                    "riga_vecchia":old_row_index+1, "riga_nuova":new_row_index+1, "colonna":column+1,
                    "chiave_riga":_row_key(new_row) or _row_key(old_row),
                    "intestazione_vecchia":_cell_headers(old_table,column), "intestazione_nuova":_cell_headers(new_table,column),
                    "stato":status, "rischio":risk, "categoria_match":match, "similarita":score,
                    "vecchia_cella":_clean_table_cell(old_cell), "nuova_cella":_clean_table_cell(new_cell),
                    "vecchia_traduzione_cella":_clean_table_cell(tr_cell) if translation_conf >= 60 else "",
                    "affidabilita_traduzione":round(float(translation_conf),1),
                    "valori_codici_vecchi":", ".join(_table_tokens(old_cell)), "valori_codici_nuovi":", ".join(_table_tokens(new_cell)),
                    "decisione_cella":action, "motivo":reason,
                })

        for old_row_index in sorted(unmatched_old_rows):
            old_row = old_table.rows[old_row_index]
            for column, old_cell in enumerate(old_row):
                if not _clean_table_cell(old_cell):
                    continue
                result.append({
                    "tabella_vecchia":old_index+1, "tabella_nuova":new_index+1,
                    "riga_vecchia":old_row_index+1, "riga_nuova":"", "colonna":column+1,
                    "chiave_riga":_row_key(old_row), "intestazione_vecchia":_cell_headers(old_table,column), "intestazione_nuova":_cell_headers(new_table,column),
                    "stato":"CELLA NON TROVATA", "rischio":"MEDIO", "categoria_match":"NOT FOUND", "similarita":0.0,
                    "vecchia_cella":_clean_table_cell(old_cell), "nuova_cella":"", "vecchia_traduzione_cella":"",
                    "affidabilita_traduzione":0.0, "valori_codici_vecchi":", ".join(_table_tokens(old_cell)), "valori_codici_nuovi":"",
                    "decisione_cella":"VERIFICA RIMOZIONE / SPOSTAMENTO", "motivo":"Riga vecchia non abbinata nella nuova tabella.",
                })

    for old_index, old_table in enumerate(old_tables):
        if old_index in used_old_tables:
            continue
        for old_row_index, old_row in enumerate(old_table.rows):
            for column, old_cell in enumerate(old_row):
                if not _clean_table_cell(old_cell):
                    continue
                result.append({
                    "tabella_vecchia":old_index+1, "tabella_nuova":"", "riga_vecchia":old_row_index+1, "riga_nuova":"", "colonna":column+1,
                    "chiave_riga":_row_key(old_row), "intestazione_vecchia":_cell_headers(old_table,column), "intestazione_nuova":"",
                    "stato":"TABELLA NON TROVATA", "rischio":"MEDIO", "categoria_match":"NOT FOUND", "similarita":0.0,
                    "vecchia_cella":_clean_table_cell(old_cell), "nuova_cella":"", "vecchia_traduzione_cella":"",
                    "affidabilita_traduzione":0.0, "valori_codici_vecchi":", ".join(_table_tokens(old_cell)), "valori_codici_nuovi":"",
                    "decisione_cella":"VERIFICA RIMOZIONE / SPOSTAMENTO", "motivo":"Intera tabella vecchia non abbinata.",
                })

    df = pd.DataFrame(result)
    if not df.empty:
        df["similarita"] = pd.to_numeric(df["similarita"], errors="coerce").fillna(0).clip(0,100)
        df["affidabilita_traduzione"] = pd.to_numeric(df["affidabilita_traduzione"], errors="coerce").fillna(0).clip(0,100)
    return df


def analyze_layouts(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path, old_pages: str = "", translation_pages: str = "", new_pages: str = "", start_anchor: str = "", terminology_glossary: str | Path | None = None, full_new_source_pdf: str | Path | None = None) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame,dict]:
    text_df, table_df, image_df, summary = _analyze_layouts_v09(
        old_source_pdf, old_translation_pdf, new_source_pdf,
        old_pages, translation_pages, new_pages, start_anchor,
        terminology_glossary, full_new_source_pdf,
    )
    summary["build_motore"] = ENGINE_BUILD
    if not table_df.empty:
        summary["celle_tabelle_invariate"] = int(table_df["stato"].isin(["INVARIATO","INVARIATO — SOLO IMPAGINAZIONE"]).sum())
        summary["celle_tabelle_tecniche"] = int(table_df["stato"].isin(["MODIFICA TECNICA","NUOVA CELLA TECNICA"]).sum())
        summary["celle_tabelle_sicurezza"] = int(table_df["stato"].isin(["MODIFICA DI SICUREZZA","NUOVA CELLA DI SICUREZZA"]).sum())
        summary["celle_tabelle_nuove"] = int(table_df["stato"].str.startswith("NUOVA CELLA", na=False).sum())
        summary["riusi_celle_tabelle_consentiti"] = int(table_df["decisione_cella"].eq("RIUSO CELLA CONSENTITO").sum())
        summary["riusi_celle_tabelle_bloccati"] = int(table_df["decisione_cella"].eq("BLOCCATO — CONTROLLO TRADUZIONE CELLA").sum())
        summary["modifiche_sicurezza_tabelle"] = summary["celle_tabelle_sicurezza"]
        summary["modifiche_tecniche_tabelle"] = summary["celle_tabelle_tecniche"]
    else:
        for key in ["celle_tabelle_invariate","celle_tabelle_tecniche","celle_tabelle_sicurezza","celle_tabelle_nuove","riusi_celle_tabelle_consentiti","riusi_celle_tabelle_bloccati"]:
            summary[key] = 0
    summary["tabelle_ricostruite_geometricamente_vecchie"] = len(extract_layout(old_source_pdf, old_pages).tables)
    summary["tabelle_ricostruite_geometricamente_nuove"] = len(extract_layout(new_source_pdf, new_pages).tables)
    return text_df, table_df, image_df, summary


def build_html_report(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, title: str = "ManualSync v0.10") -> str:
    queue = build_review_queue(text_df)
    metrics = "".join(f"<div class='metric'><b>{html.escape(str(k).replace('_',' ').title())}</b><br>{html.escape(str(v))}</div>" for k,v in summary.items())
    return f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><title>{html.escape(title)}</title><style>
body{{font-family:Arial,sans-serif;margin:0;color:#0f172a;background:#f8fafc}}header{{background:#0f172a;color:white;padding:26px 34px}}main{{padding:24px 34px}}.metrics{{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 24px}}.metric{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:10px 14px;min-width:190px}}h2{{margin-top:34px}}.note{{background:#fef3c7;border-radius:10px;padding:14px}}.table-wrap{{overflow:auto;max-height:780px;border:1px solid #cbd5e1}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white}}th,td{{border:1px solid #cbd5e1;padding:8px;vertical-align:top;min-width:140px}}th{{background:#0f766e;color:white;position:sticky;top:0;z-index:1}}td img{{max-width:300px;max-height:220px}}</style></head><body><header><h1>{html.escape(title)}</h1><p>Ricostruzione geometrica delle tabelle e confronto cella per cella.</p></header><main><div class='metrics'>{metrics}</div><p class='note'><b>Regola v0.10:</b> una riga non è più trattata come un unico segmento. Ogni cella viene ricostruita dalle coordinate del PDF, confrontata separatamente e bloccata se cambiano codici, valori, requisiti di sicurezza o se la traduzione associata è dubbia.</p><h2>1. Coda di lavoro</h2>{dataframe_table_html(queue)}<h2>2. Dettaglio CAT e tecnico</h2>{dataframe_table_html(text_df)}<h2>3. Tabelle — audit cella per cella</h2>{dataframe_table_html(table_df)}<h2>4. Visuali renderizzati</h2>{dataframe_table_html(image_df, image_mode=True)}</main></body></html>"""

# v0.10.1 patch: deterministic mapping between source tables and their old translations.
def _translation_table_mapping(old_tables: list[TableBlock], translation_tables: list[TableBlock]) -> dict[int, tuple[TableBlock,float]]:
    mapping: dict[int, tuple[TableBlock,float]] = {}
    if len(old_tables) == len(translation_tables):
        for index, (old_table, tr_table) in enumerate(zip(old_tables, translation_tables)):
            old_cols = max((len(row) for row in old_table.rows), default=0)
            tr_cols = max((len(row) for row in tr_table.rows), default=0)
            shape = 1 - (
                abs(len(old_table.rows)-len(tr_table.rows)) + abs(old_cols-tr_cols)
            ) / max(1, len(old_table.rows)+old_cols, len(tr_table.rows)+tr_cols)
            mapping[index] = (tr_table, max(80.0, min(99.0, shape*100)))
        return mapping
    used: set[int] = set()
    for old_index, old_table in enumerate(old_tables):
        best = None
        for tr_index, tr_table in enumerate(translation_tables):
            if tr_index in used:
                continue
            candidate, score = _align_translation_table(old_table, [tr_table])
            if candidate is not None and (best is None or score > best[0]):
                best = (score, tr_index, candidate)
        if best:
            mapping[old_index] = (best[2], best[0])
            used.add(best[1])
    return mapping

_compare_tables_v10_pre_translation_patch = compare_tables

# Replace only the translation-table selection while preserving cell comparison logic.
def compare_tables(old_layout: DocumentLayout, old_translation_layout: DocumentLayout, new_layout: DocumentLayout) -> pd.DataFrame:
    old_tables, new_tables = old_layout.tables, new_layout.tables
    table_map, _ = align_tables(old_tables, new_tables)
    translation_table_map = _translation_table_mapping(old_tables, old_translation_layout.tables)
    used_old_tables: set[int] = set()
    result: list[dict] = []

    def add_new_cell(old_index, new_index, new_row_index, column, new_row, new_cell, reason):
        status, risk, action, match, score = _cell_change("", new_cell)
        result.append({
            "tabella_vecchia":"" if old_index is None else old_index+1,
            "tabella_nuova":new_index+1,
            "riga_vecchia":"", "riga_nuova":new_row_index+1, "colonna":column+1,
            "chiave_riga":_row_key(new_row), "intestazione_vecchia":"",
            "intestazione_nuova":_cell_headers(new_tables[new_index],column),
            "stato":status, "rischio":risk, "categoria_match":match, "similarita":score,
            "vecchia_cella":"", "nuova_cella":_clean_table_cell(new_cell), "vecchia_traduzione_cella":"",
            "affidabilita_traduzione":0.0, "valori_codici_vecchi":"",
            "valori_codici_nuovi":", ".join(_table_tokens(new_cell)), "decisione_cella":action, "motivo":reason,
        })

    for new_index, new_table in enumerate(new_tables):
        if new_index not in table_map:
            for new_row_index, new_row in enumerate(new_table.rows):
                for column, new_cell in enumerate(new_row):
                    if _clean_table_cell(new_cell):
                        add_new_cell(None,new_index,new_row_index,column,new_row,new_cell,"Tabella non abbinata nel vecchio documento.")
            continue

        old_index, _ = table_map[new_index]
        used_old_tables.add(old_index)
        old_table = old_tables[old_index]
        row_map, unmatched_old_rows = _align_table_rows(old_table.rows, new_table.rows)
        translation_table, table_translation_conf = translation_table_map.get(old_index, (None,0.0))
        tr_row_map = _translation_row_map(old_table.rows, translation_table.rows) if translation_table else {}

        for new_row_index, new_row in enumerate(new_table.rows):
            if new_row_index not in row_map:
                for column, new_cell in enumerate(new_row):
                    if _clean_table_cell(new_cell):
                        add_new_cell(old_index,new_index,new_row_index,column,new_row,new_cell,"Nuova riga o cella nella tabella abbinata.")
                continue
            old_row_index = row_map[new_row_index]
            old_row = old_table.rows[old_row_index]
            translation_row, translation_conf = [], 0.0
            if translation_table and old_row_index in tr_row_map:
                tr_index, row_conf = tr_row_map[old_row_index]
                if tr_index < len(translation_table.rows):
                    translation_row = translation_table.rows[tr_index]
                    translation_conf = min(table_translation_conf,row_conf)
            width=max(len(old_row),len(new_row))
            for column in range(width):
                old_cell=old_row[column] if column<len(old_row) else ""
                new_cell=new_row[column] if column<len(new_row) else ""
                if not _clean_table_cell(old_cell) and not _clean_table_cell(new_cell):
                    continue
                tr_cell=translation_row[column] if column<len(translation_row) else ""
                status,risk,action,match,score=_cell_change(old_cell,new_cell)
                suspicious=False; suspicious_reason=""
                if action=="RIUSO CELLA CONSENTITO":
                    suspicious,suspicious_reason=_translation_alignment_suspicious(_clean_table_cell(old_cell),_clean_table_cell(tr_cell))
                    if not tr_cell or translation_conf<80 or suspicious:
                        action="BLOCCATO — CONTROLLO TRADUZIONE CELLA"; risk="ALTO" if suspicious else "MEDIO"
                reason="Confronto cella per cella."
                if suspicious_reason: reason+=" "+suspicious_reason
                elif action=="BLOCCATO — CONTROLLO TRADUZIONE CELLA": reason+=f" Affidabilità traduzione {translation_conf:.1f}%."
                result.append({
                    "tabella_vecchia":old_index+1,"tabella_nuova":new_index+1,
                    "riga_vecchia":old_row_index+1,"riga_nuova":new_row_index+1,"colonna":column+1,
                    "chiave_riga":_row_key(new_row) or _row_key(old_row),
                    "intestazione_vecchia":_cell_headers(old_table,column),"intestazione_nuova":_cell_headers(new_table,column),
                    "stato":status,"rischio":risk,"categoria_match":match,"similarita":score,
                    "vecchia_cella":_clean_table_cell(old_cell),"nuova_cella":_clean_table_cell(new_cell),
                    "vecchia_traduzione_cella":_clean_table_cell(tr_cell) if translation_conf>=60 else "",
                    "affidabilita_traduzione":round(float(translation_conf),1),
                    "valori_codici_vecchi":", ".join(_table_tokens(old_cell)),"valori_codici_nuovi":", ".join(_table_tokens(new_cell)),
                    "decisione_cella":action,"motivo":reason,
                })
        for old_row_index in sorted(unmatched_old_rows):
            old_row=old_table.rows[old_row_index]
            for column,old_cell in enumerate(old_row):
                if not _clean_table_cell(old_cell): continue
                result.append({"tabella_vecchia":old_index+1,"tabella_nuova":new_index+1,"riga_vecchia":old_row_index+1,"riga_nuova":"","colonna":column+1,"chiave_riga":_row_key(old_row),"intestazione_vecchia":_cell_headers(old_table,column),"intestazione_nuova":_cell_headers(new_table,column),"stato":"CELLA NON TROVATA","rischio":"MEDIO","categoria_match":"NOT FOUND","similarita":0.0,"vecchia_cella":_clean_table_cell(old_cell),"nuova_cella":"","vecchia_traduzione_cella":"","affidabilita_traduzione":0.0,"valori_codici_vecchi":", ".join(_table_tokens(old_cell)),"valori_codici_nuovi":"","decisione_cella":"VERIFICA RIMOZIONE / SPOSTAMENTO","motivo":"Riga vecchia non abbinata nella nuova tabella."})

    for old_index,old_table in enumerate(old_tables):
        if old_index in used_old_tables: continue
        for old_row_index,old_row in enumerate(old_table.rows):
            for column,old_cell in enumerate(old_row):
                if not _clean_table_cell(old_cell): continue
                result.append({"tabella_vecchia":old_index+1,"tabella_nuova":"","riga_vecchia":old_row_index+1,"riga_nuova":"","colonna":column+1,"chiave_riga":_row_key(old_row),"intestazione_vecchia":_cell_headers(old_table,column),"intestazione_nuova":"","stato":"TABELLA NON TROVATA","rischio":"MEDIO","categoria_match":"NOT FOUND","similarita":0.0,"vecchia_cella":_clean_table_cell(old_cell),"nuova_cella":"","vecchia_traduzione_cella":"","affidabilita_traduzione":0.0,"valori_codici_vecchi":", ".join(_table_tokens(old_cell)),"valori_codici_nuovi":"","decisione_cella":"VERIFICA RIMOZIONE / SPOSTAMENTO","motivo":"Intera tabella vecchia non abbinata."})
    df=pd.DataFrame(result)
    if not df.empty:
        df["similarita"]=pd.to_numeric(df["similarita"],errors="coerce").fillna(0).clip(0,100)
        df["affidabilita_traduzione"]=pd.to_numeric(df["affidabilita_traduzione"],errors="coerce").fillna(0).clip(0,100)
    return df

# Footnote markers with or without a separating space are layout-equivalent.
def _table_semantic(text: str) -> str:
    key=normalize(_clean_table_cell(text))
    key=re.sub(r"(?<=\D)\s+(?=\d+\))","",key)
    return key

# v0.10.2: normalize spacing around table footnote markers before punctuation stripping.
def _table_semantic(text: str) -> str:
    raw=_clean_table_cell(text)
    raw=re.sub(r"(?<=\D)\s+(?=\d+\))","",raw)
    return normalize(raw)

_save_analysis_outputs_v08 = save_analysis_outputs

def save_analysis_outputs(text_df: pd.DataFrame, table_df: pd.DataFrame, image_df: pd.DataFrame, summary: dict, output_dir: str | Path, prefix: str = "manualsync_v10") -> dict[str,str]:
    return _save_analysis_outputs_v08(text_df, table_df, image_df, summary, output_dir, prefix)

# v0.10.3: direct analysis pipeline; avoids the historical wrapper chain.
def analyze_layouts(old_source_pdf: str | Path, old_translation_pdf: str | Path, new_source_pdf: str | Path, old_pages: str = "", translation_pages: str = "", new_pages: str = "", start_anchor: str = "", terminology_glossary: str | Path | None = None, full_new_source_pdf: str | Path | None = None) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame,dict]:
    language_summary=_validate_language_combination_v07(old_source_pdf,old_translation_pdf,new_source_pdf)
    if full_new_source_pdf:
        flang,_,_=detect_document_language(full_new_source_pdf)
        if flang != language_summary["lingua_nuovo_originale"].lower():
            raise ValueError("Il manuale completo opzionale non è nella stessa lingua del nuovo originale.")
    approved=_load_terminology_equivalences_v07(terminology_glossary)
    old_layout=trim_layout_from_anchor(extract_layout(old_source_pdf,old_pages),start_anchor)
    tr_layout=trim_layout_from_anchor(extract_layout(old_translation_pdf,translation_pages),start_anchor)
    new_layout=trim_layout_from_anchor(extract_layout(new_source_pdf,new_pages),start_anchor)
    text_df=compare_text(old_layout,tr_layout,new_layout,new_source_pdf=full_new_source_pdf or new_source_pdf,approved_terms=approved)
    table_df=compare_tables(old_layout,tr_layout,new_layout)
    image_df=compare_images(old_layout,new_layout,old_source_pdf,new_source_pdf)
    queue=build_review_queue(text_df)
    review_count=int(text_df["richiede_revisione"].astype(str).str.upper().eq("SÌ").sum()) if not text_df.empty else 0
    summary={
        "build_motore":ENGINE_BUILD,**language_summary,"equivalenze_terminologiche_approvate":len(approved),
        "ambito_ricerca_globale":"MANUALE COMPLETO" if full_new_source_pdf else "SOLO FILE CARICATO",
        "pagine_vecchie":len(old_layout.selected_pages),"pagine_nuove":len(new_layout.selected_pages),
        "blocchi_testo_vecchi":len(old_layout.text_blocks),"blocchi_testo_nuovi":len(new_layout.text_blocks),
        "elementi_strutturali_esclusi_vecchi":len(getattr(old_layout,"excluded_blocks",[])),"elementi_strutturali_esclusi_nuovi":len(getattr(new_layout,"excluded_blocks",[])),
        "match_cat_esatti":int(text_df["metodo_abbinamento"].eq("CAT_EXACT").sum()) if not text_df.empty else 0,
        "match_fuzzy_sequenziali":int(text_df["metodo_abbinamento"].eq("SEQUENTIAL_FUZZY").sum()) if not text_df.empty else 0,
        "riusi_automatici_consentiti":int(text_df["decisione_riuso"].eq("RIUSO AUTOMATICO CONSENTITO").sum()) if not text_df.empty else 0,
        "riusi_bloccati_allineamento_bilingue":int(text_df["decisione_riuso"].eq("BLOCCATO — CONTROLLO ALLINEAMENTO BILINGUE").sum()) if not text_df.empty else 0,
        "elementi_testo_da_revisionare":review_count,
        "modifiche_sicurezza_testo":int(text_df["stato"].isin(["MODIFICA DI SICUREZZA","NUOVO CONTENUTO DI SICUREZZA"]).sum()) if not text_df.empty else 0,
        "modifiche_tecniche_testo":int(text_df["stato"].isin(["MODIFICA TECNICA","CONTESTO TECNICO MODIFICATO"]).sum()) if not text_df.empty else 0,
        "contenuti_nuovi_testo":int(text_df["stato"].isin(["NUOVO","NUOVO CONTENUTO TECNICO","NUOVO CONTENUTO DI SICUREZZA"]).sum()) if not text_df.empty else 0,
        "contenuti_spostati_certi":int(text_df["stato"].eq("SPOSTATO NEL DOCUMENTO").sum()) if not text_df.empty else 0,
        "corrispondenze_globali_ambigue":int(text_df["stato"].eq("TROVATO ALTROVE — AMBIGUO").sum()) if not text_df.empty else 0,
        "contenuti_non_trovati":int(text_df["stato"].eq("NON TROVATO NEL NUOVO AMBITO").sum()) if not text_df.empty else 0,
        "tabelle_vecchie":len(old_layout.tables),"tabelle_nuove":len(new_layout.tables),
        "pagine_visuali_vecchie":len(old_layout.selected_pages),"pagine_visuali_nuove":len(new_layout.selected_pages),
        "pagine_visuali_modificate":int(image_df["stato"].isin(["PAGINA VISIVA MODIFICATA","PAGINA VISIVA DA CONTROLLARE","NUOVA PAGINA / AREA VISIVA"]).sum()) if not image_df.empty else 0,
        "azioni_prioritarie":int(queue["priorita"].astype(str).str.startswith(("0","1","2","3")).sum()) if not queue.empty else 0,
        "nuovi_contenuti_tecnici":int(text_df["stato"].eq("NUOVO CONTENUTO TECNICO").sum()) if not text_df.empty else 0,
    }
    if not table_df.empty:
        summary.update({
            "celle_tabelle_invariate":int(table_df["stato"].isin(["INVARIATO","INVARIATO — SOLO IMPAGINAZIONE"]).sum()),
            "celle_tabelle_tecniche":int(table_df["stato"].isin(["MODIFICA TECNICA","NUOVA CELLA TECNICA"]).sum()),
            "celle_tabelle_sicurezza":int(table_df["stato"].isin(["MODIFICA DI SICUREZZA","NUOVA CELLA DI SICUREZZA"]).sum()),
            "celle_tabelle_nuove":int(table_df["stato"].str.startswith("NUOVA CELLA",na=False).sum()),
            "riusi_celle_tabelle_consentiti":int(table_df["decisione_cella"].eq("RIUSO CELLA CONSENTITO").sum()),
            "riusi_celle_tabelle_bloccati":int(table_df["decisione_cella"].eq("BLOCCATO — CONTROLLO TRADUZIONE CELLA").sum()),
        })
    else:
        summary.update({"celle_tabelle_invariate":0,"celle_tabelle_tecniche":0,"celle_tabelle_sicurezza":0,"celle_tabelle_nuove":0,"riusi_celle_tabelle_consentiti":0,"riusi_celle_tabelle_bloccati":0})
    summary["modifiche_sicurezza_tabelle"]=summary["celle_tabelle_sicurezza"]
    summary["modifiche_tecniche_tabelle"]=summary["celle_tabelle_tecniche"]
    summary["tabelle_ricostruite_geometricamente_vecchie"]=len(old_layout.tables)
    summary["tabelle_ricostruite_geometricamente_nuove"]=len(new_layout.tables)
    return text_df,table_df,image_df,summary

# Final build identifier after the direct-pipeline and translation-table patches.
ENGINE_BUILD = "0.10.3-cell-intelligence-20260805"

# ============================================================================
# ManualSync v0.11 overrides — Table Safe Alignment
# Fixes validated by ABB ACS880-34 Rev C -> Rev E stress benchmark.
# ============================================================================

_V11_NUM_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:[.,]\d+)?(?![A-Za-z0-9])")
_V11_MODEL_RE = re.compile(r"\b\d{2,4}A-\d\b", re.I)
_V11_SYMBOL_RE = re.compile(
    r"\b(?:UN|IN|PN|ILD|IHD|IMAX|I1|I2|PHD|PLD|SN|KW|KVA|HZ|KHZ|MHZ|MM|CM|M|KG|LB|HP|IEC|EMC|AC|DC)\b",
    re.I,
)


def _v11_number(value: str) -> str:
    raw = value.strip().replace(",", ".")
    try:
        number = float(raw)
        if number == 0:
            number = 0.0
        return f"{number:.12g}"
    except Exception:
        return raw


def _v11_numeric_tokens(text: str) -> tuple[str, ...]:
    return tuple(_v11_number(x) for x in _V11_NUM_RE.findall(_clean_table_cell(text)))


def _v11_table_semantic(text: str) -> str:
    """Table-specific semantic key.

    Generic document normalization deliberately removes leading decimal-like
    section numbers. That is unsafe for table cells because 15.8 and 25.27 are
    measurements, not headings. Preserve numbers and canonicalize decimal comma.
    """
    raw = _clean_table_cell(text).replace("\u00ad", "").lower()
    for old, new in {"…": "...", "–": "-", "—": "-", "≥": ">=", "≤": "<=", "−": "-"}.items():
        raw = raw.replace(old, new)
    raw = re.sub(r"(?<=\d),(?=\d)", ".", raw)
    raw = re.sub(r"(?<=\D)\s+(?=\d+\))", "", raw)
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"[^\w%°./+<>=-]+", " ", raw, flags=re.UNICODE)
    return raw.strip()


# Override the v0.10 table semantic key. Numeric values are now retained.
def _table_semantic(text: str) -> str:
    return _v11_table_semantic(text)


def _v11_anchor_weights(table: TableBlock) -> dict[str, float]:
    weights: dict[str, float] = {}
    text = " ".join(_clean_table_cell(cell) for row in table.rows for cell in row if _clean_table_cell(cell))

    for code in _V11_MODEL_RE.findall(text):
        weights[f"MODEL:{code.upper()}"] = 6.0
    for token in _table_tokens(text):
        weights[f"TECH:{token.upper()}"] = max(weights.get(f"TECH:{token.upper()}", 0.0), 3.0)
    for symbol in _V11_SYMBOL_RE.findall(text):
        weights[f"SYM:{symbol.upper()}"] = max(weights.get(f"SYM:{symbol.upper()}", 0.0), 1.25)
    for number in _v11_numeric_tokens(text):
        weights[f"NUM:{number}"] = max(weights.get(f"NUM:{number}", 0.0), 1.0)
    return weights


def _v11_translation_table_score(old_table: TableBlock, tr_table: TableBlock) -> float:
    old_a = _v11_anchor_weights(old_table)
    tr_a = _v11_anchor_weights(tr_table)
    keys = set(old_a) | set(tr_a)
    common = set(old_a) & set(tr_a)
    intersection = sum(min(old_a[k], tr_a[k]) for k in common)
    union = sum(max(old_a.get(k, 0.0), tr_a.get(k, 0.0)) for k in keys)
    min_total = max(1.0, min(sum(old_a.values()), sum(tr_a.values())))
    jaccard = intersection / union if union else 0.0
    coverage = intersection / min_total

    old_rows = len(old_table.rows)
    tr_rows = len(tr_table.rows)
    old_cols = max((len(r) for r in old_table.rows), default=0)
    tr_cols = max((len(r) for r in tr_table.rows), default=0)
    row_score = 1 - abs(old_rows - tr_rows) / max(1, old_rows, tr_rows)
    col_score = 1 - abs(old_cols - tr_cols) / max(1, old_cols, tr_cols)
    page_score = max(0.0, 1 - abs(old_table.page - tr_table.page) * 0.15)

    # Technical anchors dominate; geometry is supporting evidence only.
    return max(0.0, min(1.0, 0.45*jaccard + 0.25*coverage + 0.12*row_score + 0.12*col_score + 0.06*page_score))


def _translation_table_mapping(old_tables: list[TableBlock], translation_tables: list[TableBlock]) -> dict[int, tuple[TableBlock, float]]:
    """One-to-one conservative source↔legacy-translation table mapping.

    v0.10.1 used positional zip when counts matched and shape-only greedy matching
    otherwise. The ABB stress test showed that this can map unrelated tables.
    v0.11 ranks language-invariant technical anchors (model codes, values, units)
    and refuses weak or ambiguous candidates instead of guessing.
    """
    candidates: list[tuple[float, int, int, float]] = []
    for oi, old_table in enumerate(old_tables):
        scores = []
        for ti, tr_table in enumerate(translation_tables):
            score = _v11_translation_table_score(old_table, tr_table)
            scores.append((score, ti))
        scores.sort(reverse=True)
        best = scores[0][0] if scores else 0.0
        second = scores[1][0] if len(scores) > 1 else 0.0
        for score, ti in scores:
            # Strong candidates can compete globally. Medium candidates are kept
            # only when clearly better than alternatives.
            if score >= 0.72 or (score >= 0.62 and best - second >= 0.10 and score == best):
                ambiguity = max(0.0, best - second) if score == best else 0.0
                candidates.append((score, oi, ti, ambiguity))

    # Assign the most convincing pairs first; one source and one translation table
    # can appear only once.
    candidates.sort(key=lambda x: (x[0], x[3]), reverse=True)
    used_old: set[int] = set()
    used_tr: set[int] = set()
    mapping: dict[int, tuple[TableBlock, float]] = {}
    for score, oi, ti, ambiguity in candidates:
        if oi in used_old or ti in used_tr:
            continue
        # For sub-0.90 mappings require meaningful anchor evidence. This leaves
        # partial/continuation tables unmatched rather than attaching them by shape.
        anchors_old = _v11_anchor_weights(old_tables[oi])
        anchors_tr = _v11_anchor_weights(translation_tables[ti])
        common = set(anchors_old) & set(anchors_tr)
        if score < 0.90 and len(common) < 3:
            continue
        confidence = min(99.5, score * 100)
        mapping[oi] = (translation_tables[ti], confidence)
        used_old.add(oi)
        used_tr.add(ti)
    return mapping


def _v11_source_translation_numeric_safe(source_text: str, translation_text: str) -> tuple[bool, str]:
    source_nums = _v11_numeric_tokens(source_text)
    target_nums = _v11_numeric_tokens(translation_text)
    if source_nums or target_nums:
        if source_nums != target_nums:
            return False, (
                "I valori numerici della traduzione non coincidono con il sorgente: "
                f"sorgente={list(source_nums)}, traduzione={list(target_nums)}."
            )
    # Model/device codes must survive translation unchanged.
    source_models = {x.upper() for x in _V11_MODEL_RE.findall(source_text)}
    target_models = {x.upper() for x in _V11_MODEL_RE.findall(translation_text)}
    if source_models != target_models:
        return False, (
            "I codici modello della traduzione non coincidono con il sorgente: "
            f"sorgente={sorted(source_models)}, traduzione={sorted(target_models)}."
        )
    return True, ""


_translation_alignment_suspicious_v10 = _translation_alignment_suspicious

def _translation_alignment_suspicious(source_text: str, translation_text: str) -> tuple[bool, str]:
    suspicious, reason = _translation_alignment_suspicious_v10(source_text, translation_text)
    if suspicious:
        return suspicious, reason
    numeric_ok, numeric_reason = _v11_source_translation_numeric_safe(source_text, translation_text)
    if not numeric_ok:
        return True, numeric_reason
    return False, ""


def _cell_change(old_cell: str, new_cell: str) -> tuple[str, str, str, str, float]:
    old_clean, new_clean = _clean_table_cell(old_cell), _clean_table_cell(new_cell)
    if not old_clean and new_clean:
        safety = _safety_level(new_clean)
        if safety >= 2:
            return "NUOVA CELLA DI SICUREZZA", "CRITICO" if safety >= 3 else "ALTO", "BLOCCATO — REVISIONE SICUREZZA", "NEW", 0.0
        if _table_cell_is_technical(new_clean) or _v11_numeric_tokens(new_clean):
            return "NUOVA CELLA TECNICA", "ALTO", "BLOCCATO — REVISIONE TECNICA", "NEW", 0.0
        return "NUOVA CELLA", "MEDIO", "TRADUZIONE E REVISIONE", "NEW", 0.0
    if old_clean and not new_clean:
        return "CELLA NON TROVATA", "MEDIO", "VERIFICA RIMOZIONE / SPOSTAMENTO", "NOT FOUND", 0.0

    old_cat = _table_cat(old_clean)
    new_cat = _table_cat(new_clean)
    cat_score = ratio(old_cat, new_cat)
    if old_cat == new_cat:
        return "INVARIATO", "BASSO", "RIUSO CELLA CONSENTITO", "100% MATCH", 100.0

    old_sem = _table_semantic(old_clean)
    new_sem = _table_semantic(new_clean)
    # Never treat two empty semantic keys as a context match.
    if old_sem and new_sem and old_sem == new_sem:
        return "INVARIATO — SOLO IMPAGINAZIONE", "BASSO", "RIUSO CELLA CONSENTITO", "CONTEXT MATCH", 100.0

    safety = max(_safety_level(old_clean), _safety_level(new_clean))
    if safety >= 2:
        return "MODIFICA DI SICUREZZA", "CRITICO" if safety >= 3 else "ALTO", "BLOCCATO — REVISIONE SICUREZZA", "SAFETY CHANGE", round(cat_score, 1)

    old_nums = _v11_numeric_tokens(old_clean)
    new_nums = _v11_numeric_tokens(new_clean)
    if old_nums != new_nums and (old_nums or new_nums):
        return "MODIFICA TECNICA", "ALTO", "BLOCCATO — REVISIONE TECNICA", "TECHNICAL NUMERIC CHANGE", round(cat_score, 1)

    if set(_table_tokens(old_clean)) != set(_table_tokens(new_clean)):
        return "MODIFICA TECNICA", "ALTO", "BLOCCATO — REVISIONE TECNICA", "TECHNICAL CHANGE", round(cat_score, 1)
    return "MODIFICATO", "MEDIO", "REVISIONE LINGUISTICA", "FUZZY", round(cat_score, 1)


# The v0.10 compare_tables function resolves these helpers at call time, therefore
# the safer semantic, table mapping, bilingual gate and cell classifier above apply
# without duplicating the complete comparison pipeline.
ENGINE_BUILD = "0.11.0-table-safe-alignment-20260808"

# v0.11.1 — logical multi-page table stitching.
_extract_layout_v11_premerge = extract_layout

def _v11_header_row_count(table: TableBlock) -> int:
    count = 0
    for row in table.rows[:8]:
        first = _clean_table_cell(row[0] if row else "")
        # First voltage group or product/model row marks the start of data.
        if re.search(r"\bU[Nn]\s*=\s*\d", first) or _V11_MODEL_RE.search(first) or re.fullmatch(r"R\d+(?:\s*v\d+)?", first, re.I):
            break
        count += 1
    return count


def _v11_can_stitch_tables(a: TableBlock, b: TableBlock) -> bool:
    if b.page != a.page + 1:
        return False
    if not a.rows or not b.rows:
        return False
    ac = max((len(r) for r in a.rows), default=0)
    bc = max((len(r) for r in b.rows), default=0)
    if abs(ac - bc) > 2:
        return False
    ah = _table_semantic(row_text(a.rows[0]))
    bh = _table_semantic(row_text(b.rows[0]))
    if not ah or not bh:
        return False
    header_similarity = ratio(ah, bh)
    # Repeated multi-page headers are normally nearly identical. Permit a little
    # geometric damage (e.g. two adjacent cells merged by PDF extraction).
    return header_similarity >= 84


def _v11_stitch_tables(tables: list[TableBlock]) -> list[TableBlock]:
    if not tables:
        return []
    out: list[TableBlock] = []
    i = 0
    while i < len(tables):
        current = tables[i]
        rows = [list(r) for r in current.rows]
        first_page = current.page
        bbox = current.bbox
        j = i + 1
        while j < len(tables) and _v11_can_stitch_tables(
            TableBlock(index=current.index, page=tables[j-1].page, bbox=bbox, rows=rows),
            tables[j],
        ):
            nxt = tables[j]
            header_rows = _v11_header_row_count(nxt)
            # Validate that the repeated header prefix really resembles the first
            # table header area before dropping it.
            first_header_rows = _v11_header_row_count(current)
            compare_n = min(header_rows, first_header_rows, 5)
            if compare_n <= 0:
                break
            similarities = [
                ratio(_table_semantic(row_text(current.rows[k])), _table_semantic(row_text(nxt.rows[k])))
                for k in range(compare_n)
            ]
            if sum(similarities) / len(similarities) < 82:
                break
            rows.extend([list(r) for r in nxt.rows[header_rows:]])
            j += 1
        out.append(TableBlock(index=len(out), page=first_page, bbox=bbox, rows=rows))
        i = j
    return out


def extract_layout(pdf_path: str | Path, page_spec: str = "") -> DocumentLayout:
    layout = _extract_layout_v11_premerge(pdf_path, page_spec)
    stitched = _v11_stitch_tables(layout.tables)
    setattr(layout, "physical_table_count", len(layout.tables))
    setattr(layout, "logical_table_count", len(stitched))
    layout.tables = stitched
    return layout

ENGINE_BUILD = "0.11.1-table-safe-alignment-20260808"

# v0.11.2 — conservative old-source ↔ new-source table mapping.
def align_tables(old: list[TableBlock], new: list[TableBlock]) -> tuple[dict[int, tuple[int, float]], list[int]]:
    candidates: list[tuple[float, int, int]] = []
    for oi, old_table in enumerate(old):
        for ni, new_table in enumerate(new):
            score = _v11_translation_table_score(old_table, new_table)
            if score >= 0.68:
                candidates.append((score, oi, ni))
    candidates.sort(reverse=True)
    used_old: set[int] = set()
    used_new: set[int] = set()
    mapping: dict[int, tuple[int, float]] = {}
    for score, oi, ni in candidates:
        if oi in used_old or ni in used_new:
            continue
        # Refuse low-anchor coincidences. New revisions may change values, so the
        # threshold is slightly lower than source↔translation matching, but shape
        # alone can never force a pair.
        old_a = _v11_anchor_weights(old[oi])
        new_a = _v11_anchor_weights(new[ni])
        common = set(old_a) & set(new_a)
        if score < 0.80 and len(common) < 4:
            continue
        mapping[ni] = (oi, score)
        used_old.add(oi)
        used_new.add(ni)
    return mapping, sorted(set(range(len(old))) - used_old)

ENGINE_BUILD = "0.11.2-table-safe-alignment-20260808"

# v0.11.3 — preserve leading minus sign in table CAT keys.
def _table_cat(text: str) -> str:
    # Table cells must never pass through normalize_cat's leading '-' bullet
    # stripping because a minus sign is a technical value.
    return _v11_table_semantic(text)

ENGINE_BUILD = "0.11.3-table-safe-alignment-20260808"

# v0.11.4 — recover safe deterministic pairing when source/translation table
# sequences have the same length AND each indexed pair has independent anchor evidence.
_translation_table_mapping_v113 = _translation_table_mapping

def _translation_table_mapping(old_tables: list[TableBlock], translation_tables: list[TableBlock]) -> dict[int, tuple[TableBlock, float]]:
    if old_tables and len(old_tables) == len(translation_tables):
        indexed: dict[int, tuple[TableBlock, float]] = {}
        all_plausible = True
        for i, (old_table, tr_table) in enumerate(zip(old_tables, translation_tables)):
            score = _v11_translation_table_score(old_table, tr_table)
            alternatives = sorted(
                (_v11_translation_table_score(old_table, candidate) for candidate in translation_tables),
                reverse=True,
            )
            best = alternatives[0] if alternatives else 0.0
            # Index alone is never enough. Require anchor/shape evidence and ensure
            # another table is not materially better.
            if score < 0.65 or best - score > 0.08:
                all_plausible = False
                break
            indexed[i] = (tr_table, max(90.0, min(99.5, score * 100)))
        if all_plausible:
            return indexed
    return _translation_table_mapping_v113(old_tables, translation_tables)

ENGINE_BUILD = "0.11.4-table-safe-alignment-20260808"

# v0.11.5 — layout hyphens between letters are ignorable; numeric minus signs are not.
def _v11_table_semantic(text: str) -> str:
    raw = _clean_table_cell(text).replace("\u00ad", "").lower()
    for old, new in {"…": "...", "–": "-", "—": "-", "≥": ">=", "≤": "<=", "−": "-"}.items():
        raw = raw.replace(old, new)
    raw = re.sub(r"(?<=\d),(?=\d)", ".", raw)
    raw = re.sub(r"(?<=[A-Za-zÀ-ÿ])-(?=[A-Za-zÀ-ÿ])", "", raw)
    raw = re.sub(r"(?<=\D)\s+(?=\d+\))", "", raw)
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"[^\w%°./+<>=-]+", " ", raw, flags=re.UNICODE)
    return raw.strip()

def _table_semantic(text: str) -> str:
    return _v11_table_semantic(text)

def _table_cat(text: str) -> str:
    return _v11_table_semantic(text)

ENGINE_BUILD = "0.11.5-table-safe-alignment-20260808"

# v0.11.6 — canonicalize optional spaces after numeric signs (- 5 == -5).
_V11_NUM_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\s*\d+(?:[.,]\d+)?(?![A-Za-z0-9])")

def _v11_number(value: str) -> str:
    raw = re.sub(r"\s+", "", value.strip()).replace(",", ".")
    try:
        number = float(raw)
        if number == 0:
            number = 0.0
        return f"{number:.12g}"
    except Exception:
        return raw


def _v11_table_semantic(text: str) -> str:
    raw = _clean_table_cell(text).replace("\u00ad", "").lower()
    for old, new in {"…": "...", "–": "-", "—": "-", "≥": ">=", "≤": "<=", "−": "-"}.items():
        raw = raw.replace(old, new)
    raw = re.sub(r"(?<!\w)([-+])\s+(?=\d)", r"\1", raw)
    raw = re.sub(r"(?<=\d),(?=\d)", ".", raw)
    raw = re.sub(r"(?<=[A-Za-zÀ-ÿ])-(?=[A-Za-zÀ-ÿ])", "", raw)
    raw = re.sub(r"(?<=\D)\s+(?=\d+\))", "", raw)
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"[^\w%°./+<>=-]+", " ", raw, flags=re.UNICODE)
    return raw.strip()


def _table_semantic(text: str) -> str:
    return _v11_table_semantic(text)


def _table_cat(text: str) -> str:
    return _v11_table_semantic(text)

ENGINE_BUILD = "0.11.6-table-safe-alignment-20260808"
