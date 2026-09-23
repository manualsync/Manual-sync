"""The interface fonts ship WITH the product -- no font server is ever called.

ERYLOS is installed software: it must look the same on a machine with no
internet, and opening it must not tell a third party that a customer is
working. Until 23/09/2026 the stylesheet pulled Plus Jakarta Sans and
JetBrains Mono from fonts.googleapis.com at every start; offline the
interface silently fell back to Segoe UI.

  L  the five files are the ones verified at download time -- exact bytes
  C  the stylesheet declares them from those files, and names no font server
  P  all seven files (fonts + both licences) reach the customer package
  M  mutants: a missing file, a re-introduced Google reference and a payload
     that forgot fonts/ are each caught here

What this suite cannot prove is that the browser really paints with them:
that is regressione_browser_fonts.py, which loads the product with the font
domains blocked and measures the text.
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import hashlib
import importlib.util
import json
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import erylos_theme as theme  # noqa: E402
import packaging_payload as payload  # noqa: E402

# The files as verified on 23/09/2026 against the authors' own repositories
# (tokotype/PlusJakartaSans 18d1cd2f, JetBrains/JetBrainsMono 19371302):
# family, style and weight were read out of each binary before it was
# accepted. Pinning the bytes here means a swapped, truncated or re-encoded
# file is a failure, not a surprise in a customer's hands. Updating a font
# on purpose means updating this table on purpose.
EXPECTED = {
    "PlusJakartaSans-Regular.woff2": (41576, "f282de8041ff1231d64336ab69ecea03f2a31c127fe509ec7b99814ff2fb9b41"),
    "PlusJakartaSans-Medium.woff2": (42528, "82230f155c78b9de1a3773bab92f6a90b23c702410133fe7fd656fb2750c5f03"),
    "PlusJakartaSans-SemiBold.woff2": (42356, "16387125b045457b8d49123d31bcc143e696cf9e867831f8ae41eb244b11106e"),
    "JetBrainsMono-Regular.woff2": (92380, "f1a7a03672cdd494ce0d5543fac6e4360fe22403c6de297fdc2e55a815f7baff"),
    "JetBrainsMono-Medium.woff2": (94284, "fe1c9099e3ea4123a6216d8a4a31970a2cfc6e9d80056da165012f406c83e219"),
}
LICENCES = ("OFL-PlusJakartaSans.txt", "OFL-JetBrainsMono.txt")
FONT_SERVERS = ("fonts.googleapis.com", "fonts.gstatic.com")

results: list[dict] = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)[:400]})
    print(("PASS" if ok else "FAIL"), "|", name, "" if ok else f"| {str(detail)[:400]}")
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def theme_with_fonts_dir(fonts_dir: Path, tag: str):
    """A copy of erylos_theme reading its fonts from somewhere else (mutant helper)."""
    src = (BASE / "erylos_theme.py").read_text(encoding="utf-8").replace(
        'FONTS_DIR = Path(__file__).resolve().parent / "fonts"',
        f'FONTS_DIR = Path(r"{fonts_dir}")')
    mod_path = Path(tempfile.mkdtemp()) / f"erylos_theme_{tag}.py"
    mod_path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(mod_path.stem, mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    fonts_dir = BASE / "fonts"

    # ============================================================ L: the files
    present = sorted(p.name for p in fonts_dir.iterdir()) if fonts_dir.is_dir() else []
    check("L1: fonts/ ships exactly the five web fonts and the two licences, nothing else",
          sorted(present) == sorted(list(EXPECTED) + list(LICENCES)), present)
    wrong = {n: (p.stat().st_size, sha256(p)) for n in EXPECTED
             for p in [fonts_dir / n] if (p.stat().st_size, sha256(p)) != EXPECTED[n]}
    check("L2: every font file is byte-for-byte the one verified against the authors' repositories",
          not wrong, wrong)
    not_woff2 = [n for n in EXPECTED if (fonts_dir / n).read_bytes()[:4] != b"wOF2"]
    flavours = {n: struct.unpack(">4s", (fonts_dir / n).read_bytes()[4:8])[0] for n in EXPECTED}
    check("L3: each one is a real WOFF2 web font (signature and TrueType flavour), not a renamed file",
          not not_woff2 and set(flavours.values()) == {b"\x00\x01\x00\x00"}, (not_woff2, flavours))
    lic = {n: (fonts_dir / n).read_text(encoding="utf-8") for n in LICENCES}
    bad_lic = [n for n, t in lic.items()
               if "SIL OPEN FONT LICENSE Version 1.1" not in t or not t.startswith("Copyright 2020 The ")]
    check("L4: both licences are the SIL Open Font License 1.1 with their copyright line",
          not bad_lic and "Plus Jakarta Sans Project Authors" in lic["OFL-PlusJakartaSans.txt"]
          and "JetBrains Mono Project Authors" in lic["OFL-JetBrainsMono.txt"], bad_lic)

    # ============================================================ C: the stylesheet
    faces = re.findall(r"@font-face\{font-family:'([^']+)';font-style:normal;font-weight:(\d+);"
                       r"font-display:swap;src:url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\) format\('woff2'\)\}",
                       theme.FONT_FACES)
    declared = [(f, int(w)) for f, w, _ in faces]
    check("L5: the stylesheet declares the five families and weights the design uses",
          declared == [("Plus Jakarta Sans", 400), ("Plus Jakarta Sans", 500), ("Plus Jakarta Sans", 600),
                       ("JetBrains Mono", 400), ("JetBrains Mono", 500)], declared)
    import base64 as _b64
    embedded_wrong = [name for (_, _, b64), name in zip(faces, EXPECTED)
                      if hashlib.sha256(_b64.b64decode(b64)).hexdigest() != EXPECTED[name][1]]
    check("L6: what the browser receives IS the shipped file, byte for byte", not embedded_wrong, embedded_wrong)
    check("L7: the font stacks are unchanged -- the same families, with the system fallback behind them",
          theme.FONT_SANS == "'Plus Jakarta Sans','Segoe UI',system-ui,-apple-system,sans-serif"
          and theme.FONT_MONO == "'JetBrains Mono',Consolas,'Cascadia Mono',ui-monospace,monospace",
          (theme.FONT_SANS, theme.FONT_MONO))

    import app  # noqa: E402
    called = [s for s in FONT_SERVERS if s in app.CSS]
    check("C1: the stylesheet the product serves calls no font server, and carries no remote @import",
          not called and "@import url(http" not in app.CSS.replace("'", "").replace('"', "")
          and app.CSS.startswith("@font-face{font-family:'Plus Jakarta Sans'"), called or app.CSS[:80])
    product_files = [p for p in BASE.glob("*.py") if not p.name.startswith("regressione_")]
    offenders = {p.name: [s for s in FONT_SERVERS if s in p.read_text(encoding="utf-8")] for p in product_files}
    offenders = {k: v for k, v in offenders.items() if v}
    check("C2: no product module names a font server any more", not offenders, offenders)

    # ============================================================ P: the package
    entries = payload.payload_entries()
    expected_paths = [f"fonts/{n}" for n in list(EXPECTED) + list(LICENCES)]
    missing = [e for e in expected_paths if e not in entries]
    check("P1: all seven files -- the five fonts AND both licences -- are in the customer package",
          not missing and not payload.missing_sources(), (missing, payload.missing_sources()))
    stage = Path(tempfile.mkdtemp()) / "payload"
    for rel in entries:
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BASE / rel, stage / rel)
    check("P2: a materialised package passes the build's own payload verification",
          not payload.verify(stage), payload.verify(stage))
    shutil.rmtree(stage / "fonts", ignore_errors=True)
    check("P2b (mutant): a package that forgot fonts/ is refused by that same verification, "
          "naming every missing file", sorted(payload.verify(stage)) == sorted(expected_paths),
          payload.verify(stage))

    # ============================================================ M: mutants
    empty = Path(tempfile.mkdtemp())
    mutant = theme_with_fonts_dir(empty, "missing")
    check("M1 (mutant): with the font files gone the theme reports every one of them missing and embeds none "
          "-- which L1/L2 above would fail on",
          sorted(mutant.MISSING_FONT_FILES) == sorted(EXPECTED) and mutant.FONT_FACES == "",
          mutant.MISSING_FONT_FILES)
    one_missing = Path(tempfile.mkdtemp())
    for n in EXPECTED:
        if n != "JetBrainsMono-Medium.woff2":
            shutil.copy2(fonts_dir / n, one_missing / n)
    mutant2 = theme_with_fonts_dir(one_missing, "partial")
    check("M2 (mutant): one missing file is reported by name and only the remaining four are embedded",
          mutant2.MISSING_FONT_FILES == ["JetBrainsMono-Medium.woff2"]
          and mutant2.FONT_FACES.count("@font-face") == 4, mutant2.MISSING_FONT_FILES)
    check("M3 (mutant): the C1 rule recognises a re-introduced Google Fonts import",
          [s for s in FONT_SERVERS
           if s in "@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans');"] ==
          ["fonts.googleapis.com"], "")
except BaseException as _crash:
    if not isinstance(_crash, AssertionError):
        results.append({"test": "suite interrupted by an unexpected error", "status": "FAIL",
                        "detail": f"{type(_crash).__name__}: {_crash}"[:400]})
    raise
finally:
    summary = {"suite": "Local interface fonts -- no font server is called", "tests": len(results),
               "pass": sum(r["status"] == "PASS" for r in results),
               "fail": sum(r["status"] == "FAIL" for r in results), "results": results}
    (BASE / "RISULTATO_FONT_LOCALI.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("suite", "tests", "pass", "fail")}, ensure_ascii=False))
