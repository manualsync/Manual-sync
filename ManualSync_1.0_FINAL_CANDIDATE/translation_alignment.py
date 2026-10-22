"""CP3.4: structural alignment of a source document with its legacy translation.

Moved VERBATIM out of layout_engine (the PDF layout engine), where these functions
were mixed with PyMuPDF code although they never touch a PDF: given text blocks
already extracted from the two documents, they pair each source block with its
translation using only structure -- section, order, heading flag, codes, technical
values, length and the agreement of neighbouring pairs. No file, no database, no
randomness; same input, same output.

map_translation_blocks is the LIVE definition (layout_engine defined it twice; the
second one, moved here, is the one that runs). layout_engine still serves every
name below, as the same objects. Blocks are layout_engine.TextBlock instances;
the annotations name that type, but -- with postponed evaluation -- it is never
needed at run time, so this module does not import the PDF engine.
"""
from __future__ import annotations


def _set_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def _section_groups(blocks: list[TextBlock]) -> dict[str, list[TextBlock]]:
    groups: dict[str, list[TextBlock]] = {}; current = "__PRE__"
    for block in blocks:
        if block.section_ref:
            current = block.section_ref
        groups.setdefault(current, []).append(block)
    return groups


def translation_pair_score(source: TextBlock, target: TextBlock, source_count: int, target_count: int) -> float:
    score = 0.0
    if source.section_ref and target.section_ref:
        score += 0.50 if source.section_ref == target.section_ref else -0.75
    score += 0.16 if source.heading == target.heading else -0.14
    score += 0.22 * _set_overlap(source.technical_values, target.technical_values)
    score += 0.30 * _set_overlap(source.codes, target.codes)
    progress = max(0.0, 1 - abs(source.index / max(1, source_count - 1) - target.index / max(1, target_count - 1)))
    score += 0.22 * progress
    ls, lt = max(1, len(source.normalized)), max(1, len(target.normalized))
    score += 0.08 * min(ls, lt) / max(ls, lt)
    return max(0.0, min(1.0, score))


def _monotonic_generic(old: list, new: list, score_fn, minimum_score: float, gap_cost: float = 0.30) -> tuple[dict[int, tuple[int, float]], list[int]]:
    m, n = len(old), len(new)
    dp = [[0.0] * (n + 1) for _ in range(m + 1)]
    choice = [[""] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1): dp[i][0] = dp[i - 1][0] - gap_cost; choice[i][0] = "D"
    for j in range(1, n + 1): dp[0][j] = dp[0][j - 1] - gap_cost; choice[0][j] = "I"
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            score = score_fn(old[i - 1], new[j - 1], m, n)
            options = {
                "M": dp[i - 1][j - 1] + (score if score >= minimum_score else -0.95),
                "D": dp[i - 1][j] - gap_cost,
                "I": dp[i][j - 1] - gap_cost,
            }
            best = max(("M", "D", "I"), key=lambda k: (options[k], {"M": 2, "D": 1, "I": 0}[k]))
            dp[i][j] = options[best]; choice[i][j] = best
    result: dict[int, tuple[int, float]] = {}; used_old: set[int] = set(); i, j = m, n
    while i > 0 or j > 0:
        c = choice[i][j]
        if c == "M":
            score = score_fn(old[i - 1], new[j - 1], m, n)
            if score >= minimum_score:
                result[j - 1] = (i - 1, score); used_old.add(i - 1)
            i -= 1; j -= 1
        elif c == "D": i -= 1
        else: j -= 1
    return result, sorted(set(range(m)) - used_old)


def map_translation_blocks(old_source: list[TextBlock], old_translation: list[TextBlock]) -> dict[int, tuple[str, float, int]]:
    """Align source and legacy translation conservatively, then strengthen confidence
    only when order, local position and adjacent pairs agree.

    This is not a cross-lingual semantic guess. It is structural evidence based on
    the bilingual document's section, order, codes, values and neighboring pairs.
    """
    if not old_source or not old_translation:
        return {}
    source_groups = _section_groups(old_source)
    target_groups = _section_groups(old_translation)
    result: dict[int, tuple[str, float, int]] = {}

    for section, source_group in source_groups.items():
        target_group = target_groups.get(section, [])
        if not target_group:
            continue
        mapping, _ = _monotonic_generic(
            source_group,
            target_group,
            translation_pair_score,
            minimum_score=0.34,
            gap_cost=0.28,
        )
        # mapping is target-local-index -> (source-local-index, raw score)
        reverse = {source_i: (target_i, score) for target_i, (source_i, score) in mapping.items()}
        ordered_sources = sorted(reverse)
        for local_source_idx in ordered_sources:
            local_target_idx, raw_score = reverse[local_source_idx]
            source_block = source_group[local_source_idx]
            target_block = target_group[local_target_idx]

            source_progress = local_source_idx / max(1, len(source_group) - 1)
            target_progress = local_target_idx / max(1, len(target_group) - 1)
            position_support = max(0.0, 1.0 - abs(source_progress - target_progress))

            neighbor_votes = []
            prev_pair = reverse.get(local_source_idx - 1)
            if prev_pair:
                neighbor_votes.append(1.0 if prev_pair[0] == local_target_idx - 1 else 0.0)
            next_pair = reverse.get(local_source_idx + 1)
            if next_pair:
                neighbor_votes.append(1.0 if next_pair[0] == local_target_idx + 1 else 0.0)
            neighbor_support = sum(neighbor_votes) / len(neighbor_votes) if neighbor_votes else position_support

            # Raw structural score remains dominant. Order consensus can raise a
            # correct short heading above the reuse threshold, but cannot rescue a
            # structurally implausible mapping by itself.
            confidence = 100.0 * (
                0.55 * raw_score
                + 0.30 * position_support
                + 0.15 * neighbor_support
            )
            if source_block.section_ref and target_block.section_ref and source_block.section_ref != target_block.section_ref:
                confidence = min(confidence, 59.0)
            confidence = min(100.0, round(confidence, 1))
            result[source_block.index] = (target_block.text, confidence, target_block.index)
    return result
