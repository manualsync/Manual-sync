from __future__ import annotations

from typing import Iterable


def canonical_lang(value: str | None) -> str:
    """Canonicalize a BCP-47-like language tag without rejecting legacy input.

    Examples:
      it-it -> it-IT
      IT_it -> it-IT
      zh-hant-tw -> zh-Hant-TW
      en -> en

    Empty values remain empty. Extensions/variants are preserved with conservative
    casing so existing project metadata is not discarded.
    """
    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return ""
    parts = [p for p in raw.split("-") if p]
    if not parts:
        return ""

    out: list[str] = [parts[0].lower()]
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():          # ISO 15924 script
            out.append(p.title())
        elif (len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit()):
            out.append(p.upper())                # region
        else:
            out.append(p.lower())                # variant/extension, conservative
    return "-".join(out)


def canonical_langs(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.replace(";", ",").split(",")
    result = {canonical_lang(v) for v in values if str(v or "").strip()}
    result.discard("")
    return sorted(result)
