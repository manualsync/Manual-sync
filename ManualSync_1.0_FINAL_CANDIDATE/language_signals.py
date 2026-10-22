"""CP3.4: the language evidence of a document -- from its file name and from its words.

Moved VERBATIM out of layout_engine, where it was mixed with PyMuPDF code although it
never opens a file: _filename_language reads only the NAME (Path is used to take the
last path component, nothing touches the disk), _score_text_language counts
stop-words in a string, _combine_language_signals decides between the two.
Used by layout_engine.detect_document_language / detect_docx_language and by
idml_import. layout_engine still serves every name below, as the same objects.
"""
from __future__ import annotations

import re
from pathlib import Path

LANGUAGE_STOPWORDS = {
    "en": {"the", "and", "must", "may", "with", "for", "from", "mounting", "page", "control", "cabinet", "device", "converter", "inverter"},
    "it": {"il", "la", "e", "deve", "può", "con", "per", "dalla", "montaggio", "pagina", "quadro", "dispositivo", "convertitore"},
    "de": {"der", "die", "das", "und", "muss", "mit", "für", "seite", "gerät", "montage"},
    "fr": {"le", "la", "les", "et", "doit", "avec", "pour", "page", "appareil", "montage"},
    "es": {"el", "la", "los", "y", "debe", "con", "para", "página", "dispositivo", "montaje"},
}


def _filename_language(path: str | Path) -> tuple[str, float]:
    name = Path(path).name.upper()
    patterns = {
        "en": ["_EN_", "_EN-", "_EN.", "EN-US", "EN_GB"],
        "it": ["_IT_", "_IT-", "_IT.", "IT-IT"],
        "de": ["_DE_", "_DE-", "_DE.", "DE-DE"],
        "fr": ["_FR_", "_FR-", "_FR.", "FR-FR"],
        "es": ["_ES_", "_ES-", "_ES.", "ES-ES"],
    }
    for lang, pats in patterns.items():
        if any(p in name for p in pats):
            return lang, 0.99
    return "unknown", 0.0


def _score_text_language(text: str) -> tuple[str, float]:
    words = re.findall(r"[a-zà-ÿ]+", str(text or ""))
    counts = {lang: sum(1 for w in words if w in stop) for lang, stop in LANGUAGE_STOPWORDS.items()}
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    text_lang, top = ranked[0] if ranked else ("unknown", 0)
    second = ranked[1][1] if len(ranked) > 1 else 0
    text_conf = 0.0 if top < 3 else min(0.97, 0.55 + (top - second) / max(10, top) * 0.42)
    return text_lang, text_conf


def _combine_language_signals(file_lang: str, file_conf: float, text_lang: str, text_conf: float) -> tuple[str, float, str]:
    if file_lang != "unknown" and (text_lang == "unknown" or file_lang == text_lang):
        return file_lang, max(file_conf, text_conf), "nome file + contenuto" if text_lang == file_lang else "nome file"
    if text_lang != "unknown":
        return text_lang, text_conf, "contenuto"
    return file_lang, file_conf, "nome file" if file_lang != "unknown" else "non determinata"
