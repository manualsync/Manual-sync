from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

from language_codes import canonical_lang

# Small, dependency-free language fingerprints. They are deliberately conservative:
# they only block when the evidence is strong, while exact cross-language copies are
# always blocked for genuinely linguistic content.
_STOPWORDS = {
    "it": {"il","lo","la","i","gli","le","di","del","della","dei","delle","e","che","un","una","per","con","in","su","al","alla","nel","nella","sono","si","non","questo","questa"},
    "en": {"the","a","an","of","and","that","to","for","with","in","on","is","are","this","these","not","from","by","as","be","can"},
    "de": {"der","die","das","ein","eine","und","von","zu","für","mit","in","auf","ist","sind","nicht","diese","dieser","den","dem","des"},
    "fr": {"le","la","les","un","une","de","du","des","et","que","pour","avec","dans","sur","est","sont","ce","cette","pas","par"},
    "es": {"el","la","los","las","un","una","de","del","y","que","para","con","en","sobre","es","son","este","esta","no","por"},
}

_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.:/+\-]*$")


def _norm(text: Any) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _words(text: Any) -> list[str]:
    return [w.casefold() for w in _WORD_RE.findall(str(text or ""))]


def is_nontranslatable_text(text: Any) -> bool:
    """Return True only for obvious codes/numbers/symbol-only labels.

    Short technical identifiers such as C1, ACS880-34, 95.15, %, mm, HMI remain
    allowed to be identical across languages and must not trigger linguistic QA.
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    words = _words(raw)
    alpha_chars = sum(ch.isalpha() for ch in raw)
    if alpha_chars == 0:
        return True
    # One/two compact tokens that look like technical identifiers or abbreviations.
    compact = re.sub(r"\s+", "", raw)
    if len(words) <= 2 and len(compact) <= 24 and _CODE_RE.fullmatch(compact):
        has_digit = any(ch.isdigit() for ch in compact)
        has_code_sep = any(ch in "_.:/+-" for ch in compact)
        if has_digit or (raw.upper() == raw and has_code_sep):
            return True
    # Very short labels/acronyms are safer treated as not translatable than blocked.
    if len(words) == 1 and alpha_chars <= 4 and raw.upper() == raw:
        return True
    return False


def _lang_root(lang: Any) -> str:
    c = canonical_lang(str(lang or ""))
    return c.split("-")[0].casefold() if c else ""


def _stopword_score(text: Any, lang: Any) -> tuple[int, int]:
    root = _lang_root(lang)
    stop = _STOPWORDS.get(root, set())
    words = _words(text)
    return sum(w in stop for w in words), len(words)


def cross_language_issue(source: Any, target: Any, source_lang: Any, target_lang: Any) -> str:
    """Return a fail-closed linguistic reason, or empty string when acceptable.

    This is intentionally a plausibility gate, not a translation-quality scorer.
    It catches the commercial-safety failures that structural XLIFF QA cannot:
    source copied into a different-language target and strong target-language mismatch.
    """
    src = str(source or "").strip()
    tgt = str(target or "").strip()
    sl = canonical_lang(str(source_lang or ""))
    tl = canonical_lang(str(target_lang or ""))
    if not src or not tgt or not sl or not tl or sl == tl:
        return ""
    if is_nontranslatable_text(src):
        return ""

    ns, nt = _norm(src), _norm(tgt)
    words = _words(src)
    alpha = sum(ch.isalpha() for ch in src)

    # Exact copied linguistic content is never a valid cross-language translation.
    if ns == nt and (len(words) >= 3 or alpha >= 16):
        return f"target identico alla sorgente nonostante lingue diverse ({sl}→{tl})"

    # Near-identical long content is also suspicious. Keep threshold intentionally high
    # to avoid blocking legitimate technical strings containing many preserved tokens.
    if alpha >= 45:
        ratio = SequenceMatcher(None, ns, nt).ratio()
        if ratio >= 0.985:
            return f"target quasi identico alla sorgente ({ratio:.1%}) con lingue diverse ({sl}→{tl})"

    # Conservative stopword mismatch for the five common European languages above.
    src_hits, src_n = _stopword_score(tgt, sl)
    tgt_hits, tgt_n = _stopword_score(tgt, tl)
    if src_n >= 8 and tgt_n >= 8 and _lang_root(sl) in _STOPWORDS and _lang_root(tl) in _STOPWORDS:
        if src_hits >= 3 and src_hits >= max(3, tgt_hits * 2 + 1):
            return f"target linguisticamente più compatibile con {sl} che con {tl} (stopword gate)"
    return ""


def iter_text_pairs(source: Any, target: Any) -> Iterable[tuple[str, str]]:
    """Yield structurally corresponding visible strings from scalar/list/dict content."""
    if isinstance(source, str) or isinstance(target, str):
        yield str(source or ""), str(target or "")
        return
    if isinstance(source, dict) and isinstance(target, dict):
        for key in source.keys() & target.keys():
            if key in {"file_path", "table_id", "figure_id", "version", "status"}:
                continue
            yield from iter_text_pairs(source.get(key), target.get(key))
        return
    if isinstance(source, (list, tuple)) and isinstance(target, (list, tuple)):
        for s, t in zip(source, target):
            yield from iter_text_pairs(s, t)
        return


def collect_cross_language_issues(source: Any, target: Any, source_lang: Any, target_lang: Any, *, limit: int = 20) -> list[str]:
    issues: list[str] = []
    for s, t in iter_text_pairs(source, target):
        reason = cross_language_issue(s, t, source_lang, target_lang)
        if reason:
            preview = re.sub(r"\s+", " ", str(s)).strip()[:120]
            issues.append(f"{reason}: {preview}")
            if len(issues) >= limit:
                break
    return issues
