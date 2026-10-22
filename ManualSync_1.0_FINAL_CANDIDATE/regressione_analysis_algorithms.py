"""CP3.4 regression: the pure analysis algorithms taken out of layout_engine behave exactly as before.

CP3.4 moved two families of PURE algorithms out of layout_engine (the PDF layout
engine, which loads PyMuPDF, PIL and python-docx) into modules with one job each:

* translation_alignment -- structural alignment of a source document's text blocks
  with its legacy translation (map_translation_blocks and the four helpers it runs
  on), used by the translation memory (cat_tm);
* language_signals -- the document-language evidence from a file name and from
  stop-words, and how the two are combined (used by idml_import and by
  layout_engine's own detect_document_language / detect_docx_language).

The digests below were pinned BEFORE the move, against layout_engine's live
definitions (the last binding of each name -- layout_engine redefines some names
several times; only the bound one runs). After the move the same names are still
served by layout_engine, as the very same function objects.

All inputs are synthetic and built here; nothing is read from disk except the
modules themselves.

Re-pin only after a reviewed, intentional behaviour change:
    ..\\.venv\\Scripts\\python.exe regressione_analysis_algorithms.py --print-golden

Usage:  ..\\.venv\\Scripts\\python.exe regressione_analysis_algorithms.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()

import layout_engine as le  # noqa: E402

PRINT_GOLDEN = "--print-golden" in sys.argv
_results: list[tuple[str, bool, str]] = []
_observed: dict[str, str] = {}


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:600]))
    if not PRINT_GOLDEN:
        print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:600]}]"))


def plain(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return plain(dataclasses.asdict(o))
    if isinstance(o, dict):
        return [[plain(k), plain(v)] for k, v in o.items()]      # keeps key ORDER and key TYPES
    if isinstance(o, (list, tuple)):
        return [plain(x) for x in o]
    if isinstance(o, (set, frozenset)):
        return sorted(plain(x) for x in o)
    if isinstance(o, float):
        return repr(o)                                             # exact float, not a rounded rendering
    return o


def snap(label, o):
    text = json.dumps(plain(o), ensure_ascii=False, default=repr)
    _observed[label] = hashlib.sha256(text.encode("utf-8")).hexdigest()


def outcome(fn, *a, **k):
    try:
        return ("ok", fn(*a, **k))
    except Exception as exc:  # noqa: BLE001 -- the observed behaviour IS the exception
        return ("raise", type(exc).__name__, str(exc))


# ------------------------------------------------------------------ synthetic blocks
def block(i, text, *, page=1, section="", heading=False, values=(), codes=(), size=9.0):
    return le.TextBlock(index=i, page=page, bbox=(10.0, 10.0 + i, 500.0, 20.0 + i), text=text,
                        normalized=text.lower(), cat_normalized=text.lower(), technical_values=tuple(values),
                        codes=tuple(codes), page_refs=(), section_ref=section, heading=heading, font_size=size)


SRC = [
    block(0, "Safety instructions", section="1", heading=True, size=14.0),
    block(1, "Disconnect the supply before opening the cabinet.", section="1", codes=("F001",)),
    block(2, "Wait 5 minutes for the DC link to discharge.", section="1", values=("5 min",)),
    block(3, "Mechanical installation", section="2", heading=True, size=14.0),
    block(4, "Tighten the M8 screws to 25 Nm.", section="2", values=("25 nm",), codes=("M8",)),
    block(5, "Keep 100 mm free space above the drive.", section="2", values=("100 mm",)),
    block(6, "Electrical installation", section="3", heading=True, size=14.0),
    block(7, "Connect PE before L1, L2, L3.", section="3", codes=("PE", "L1", "L2", "L3")),
]
TGT = [
    block(0, "Istruzioni di sicurezza", section="1", heading=True, size=14.0),
    block(1, "Scollegare l'alimentazione prima di aprire il quadro.", section="1", codes=("F001",)),
    block(2, "Attendere 5 minuti per la scarica del DC link.", section="1", values=("5 min",)),
    block(3, "Installazione meccanica", section="2", heading=True, size=14.0),
    block(4, "Serrare le viti M8 a 25 Nm.", section="2", values=("25 nm",), codes=("M8",)),
    block(5, "Lasciare 100 mm di spazio libero sopra l'azionamento.", section="2", values=("100 mm",)),
    block(6, "Installazione elettrica", section="3", heading=True, size=14.0),
    block(7, "Collegare PE prima di L1, L2, L3.", section="3", codes=("PE", "L1", "L2", "L3")),
]
TGT_EXTRA = TGT[:3] + [block(30, "Nota aggiuntiva solo nella traduzione.", section="1")] + TGT[3:]
TGT_MISSING = [b for b in TGT if b.index != 4]
TGT_SWAPPED = TGT[:4] + [TGT[5], TGT[4]] + TGT[6:]
TGT_OTHER_SECTIONS = [dataclasses.replace(b, section_ref="9." + b.section_ref) for b in TGT]
NO_SECTIONS_S = [dataclasses.replace(b, section_ref="") for b in SRC]
NO_SECTIONS_T = [dataclasses.replace(b, section_ref="") for b in TGT]
BIG_S = [block(i, f"Step {i}: check value {i * 3} V on terminal X{i % 7}.", section=str(1 + i // 10),
               values=(f"{i * 3} v",), codes=(f"X{i % 7}",), heading=(i % 10 == 0)) for i in range(40)]
BIG_T = [block(i, f"Passo {i}: verificare {i * 3} V sul morsetto X{i % 7}.", section=str(1 + i // 10),
               values=(f"{i * 3} v",), codes=(f"X{i % 7}",), heading=(i % 10 == 0)) for i in range(40) if i % 13 != 5]

# =================================================================== translation alignment (pinned before CP3.4)
mtb = le.map_translation_blocks
cases = {
    "identical": (SRC, TGT), "extra_target": (SRC, TGT_EXTRA), "missing_target": (SRC, TGT_MISSING),
    "swapped": (SRC, TGT_SWAPPED), "other_sections": (SRC, TGT_OTHER_SECTIONS),
    "no_sections": (NO_SECTIONS_S, NO_SECTIONS_T), "big": (BIG_S, BIG_T),
    "empty_source": ([], TGT), "empty_target": (SRC, []), "none_source": (None, TGT),
}
results = {k: outcome(mtb, s, t) for k, (s, t) in cases.items()}
check("T1: identical structure maps every source block to its own translation, in order, with types kept",
      results["identical"][0] == "ok" and sorted(results["identical"][1]) == list(range(8))
      and all(isinstance(v[1], float) and v[2] == k for k, v in results["identical"][1].items()), results["identical"])
check("T2: empty or missing input gives an empty mapping (no exception)",
      all(results[k] == ("ok", {}) for k in ("empty_source", "empty_target", "none_source")),
      {k: results[k] for k in ("empty_source", "empty_target", "none_source")})
check("T3: no common section -> nothing mapped", results["other_sections"] == ("ok", {}), results["other_sections"])
snap("map_translation_blocks", results)
snap("_set_overlap", [le._set_overlap(a, b) for a, b in [((), ()), (("a",), ()), (("a", "b"), ("b", "c")),
                                                         (("x",), ("x",)), (("a", "a"), ("a",))]])
snap("_section_groups", {k: [list(le._section_groups(v)), [[b.index for b in g] for g in le._section_groups(v).values()]]
                         for k, v in {"src": SRC, "none": NO_SECTIONS_S, "big": BIG_S}.items()})
snap("translation_pair_score", [le.translation_pair_score(s, t, len(SRC), len(TGT)) for s in SRC for t in TGT])
snap("_monotonic_generic", [
    le._monotonic_generic([1, 2, 3, 4], [1, 3, 4, 5], lambda a, b, m, n: 1.0 if a == b else 0.0, 0.5),
    le._monotonic_generic([], [1], lambda a, b, m, n: 1.0, 0.5),
    le._monotonic_generic([1, 2], [], lambda a, b, m, n: 1.0, 0.5),
    le._monotonic_generic(list("abcde"), list("aXcYe"), lambda a, b, m, n: 0.9 if a == b else 0.1, 0.34, gap_cost=0.28),
])
errors_t = {
    "bad_block": outcome(mtb, [object()], TGT),
    "overlap_none": outcome(le._set_overlap, None, ("a",)),
    "pair_bad": outcome(le.translation_pair_score, object(), TGT[0], 1, 1),
}
check("T4: malformed input raises (AttributeError / TypeError), never a silent mapping",
      all(v[0] == "raise" for v in errors_t.values()), errors_t)
snap("translation_errors", errors_t)

# =================================================================== language signals (pinned before CP3.4)
names = ["Manual_EN_v2.pdf", "manual_it.idml", "MANUAL_DE-DE.docx", "guide_fr_.pdf", "x_ES.pdf", "plain.pdf",
         "EN-US manual.pdf", "Handbuch_DEU.pdf", "", "folder/sub/Doc_IT_final.idml"]
snap("_filename_language", [le._filename_language(n) for n in names] + [le._filename_language(Path("A_EN_.pdf"))])
texts = [
    "The device must be mounted in the control cabinet with the converter and the inverter page.",
    "Il dispositivo deve essere montato con il quadro per la pagina del convertitore.",
    "Der Gerät muss mit der Montage und die Seite für das Gerät.",
    "Le appareil doit être monté avec la page pour les et montage.",
    "El dispositivo debe montarse con la página para los y montaje.",
    "", "12345 678", "the il der le el", "THE AND MUST",
]
snap("_score_text_language", [le._score_text_language(t) for t in texts] + [le._score_text_language(None)])
combos = [("en", 0.99, "en", 0.8), ("en", 0.99, "it", 0.8), ("unknown", 0.0, "de", 0.7), ("unknown", 0.0, "unknown", 0.0),
          ("fr", 0.99, "unknown", 0.0), ("it", 0.5, "it", 0.97)]
snap("_combine_language_signals", [le._combine_language_signals(*c) for c in combos])
snap("LANGUAGE_STOPWORDS", {k: sorted(v) for k, v in le.LANGUAGE_STOPWORDS.items()})
check("L1: the file name wins when the content agrees or is unknown; the content decides otherwise",
      le._combine_language_signals("en", 0.99, "en", 0.8) == ("en", 0.99, "nome file + contenuto")
      and le._combine_language_signals("en", 0.99, "it", 0.8) == ("it", 0.8, "contenuto"), "")
errors_l = {"filename_none": outcome(le._filename_language, None), "filename_int": outcome(le._filename_language, 3)}
check("L2: a non-path file name raises TypeError", all(v[:2] == ("raise", "TypeError") for v in errors_l.values()), errors_l)
snap("language_errors", errors_l)

# =================================================================== the CP3.4 boundary
MODULES = {"translation_alignment": ["map_translation_blocks", "translation_pair_score", "_section_groups",
                                     "_set_overlap", "_monotonic_generic"],
           "language_signals": ["LANGUAGE_STOPWORDS", "_filename_language", "_score_text_language",
                                "_combine_language_signals"]}
present = all((BASE / f"{m}.py").is_file() for m in MODULES)
check("B1: the two pure-algorithm modules exist (translation_alignment.py, language_signals.py)", present,
      {m: (BASE / f"{m}.py").is_file() for m in MODULES})
HEAVY = ("fitz", "pymupdf", "PIL", "docx", "numpy", "pandas", "layout_engine", "sqlite3", "gradio", "fastapi",
         "requests", "socket", "webview", "rapidfuzz", "app")
if present:
    for m, names_ in MODULES.items():
        r = subprocess.run([sys.executable, "-I", "-c",
                            "import sys, importlib; sys.path.insert(0, sys.argv[1]); importlib.import_module(sys.argv[2]); "
                            "print(','.join(x for x in sys.argv[3].split(',') if x in sys.modules))",
                            str(BASE), m, ",".join(HEAVY)], capture_output=True, text=True, timeout=120)
        check(f"B2-{m}: importable alone, loading no PDF/imaging/DOCX/numeric stack, no layout_engine, SQL, UI or network",
              r.returncode == 0 and r.stdout.strip() == "", [r.returncode, r.stdout.strip(), r.stderr[-300:]])
        t = ast.parse((BASE / f"{m}.py").read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(t) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module.split(".")[0] for n in ast.walk(t) if isinstance(n, ast.ImportFrom) and n.module}
        check(f"B3-{m}: imports only the standard library it needs", imported <= {"__future__", "re", "pathlib"}, imported)
        mod = __import__(m)
        same = {n: getattr(le, n) is getattr(mod, n) for n in names_}
        check(f"B4-{m}: layout_engine still serves these names -- as the very same objects", all(same.values()), same)
    import cat_tm  # noqa: E402
    import translation_alignment  # noqa: E402

    check("B5: the translation memory uses translation_alignment directly",
          getattr(cat_tm, "map_translation_blocks", None) is translation_alignment.map_translation_blocks,
          getattr(cat_tm, "map_translation_blocks", None))
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); import idml_import; "
                        "print(','.join(x for x in ('fitz','PIL','docx','layout_engine') if x in sys.modules))",
                        str(BASE)], capture_output=True, text=True, timeout=120)
    check("B6: idml_import no longer loads layout_engine / PyMuPDF / PIL / docx to get its language signals",
          r.returncode == 0 and r.stdout.strip() == "", [r.returncode, r.stdout.strip(), r.stderr[-300:]])
else:
    for n in ("B2", "B3", "B4", "B5", "B6"):
        check(f"{n}: (modules missing)", False, "translation_alignment.py / language_signals.py not present")

check("N1: no network access attempted", not GUARD.snapshot(), GUARD.snapshot())

# Pinned before the CP3.4 move (HEAD dc9eacf), against layout_engine's live definitions; three runs, identical.
GOLDEN = {
    'LANGUAGE_STOPWORDS': '8eb2e09786c5d79fea5f92965cf120d390aa8dac5993ebd9eb202678764cc581',
    '_combine_language_signals': '5a798dbfd4a7414b624564629e63c06b5d4f5bdf327b0a89dd4becdb7be5e1e6',
    '_filename_language': '94b569ef6a15dcde0b35468d0eea5b7b0b8836a82b1ee90886348b7430153577',
    '_monotonic_generic': '2947c6f87867a7fb7d24d37bafac21ae6d29d42ef7de6929402546480334aed2',
    '_score_text_language': '0b61ca20164a4510e82a33dceba41c08abdb6ab34acb5a0dc4c3886cb91a2398',
    '_section_groups': 'b2008b854f9de07d483855d04a24596686bb85b92facbd1c3220d902fff54ef1',
    '_set_overlap': '41aa815f43759e81aa1437b4e535e46659d3a117538b8e1a07203f30252c3bf5',
    'language_errors': '3a0416c1d2ec9f0a8283955c64e1b8ed3c8e416521f2060743fe7c12186d4718',
    'map_translation_blocks': 'd603b5b396af67cf2bcdfd799d0eba471755f688ec84bba79cbe434a8c07c8cb',
    'translation_errors': '9b5ee3d473dac072db2dc1c234b8d976d571bbb1b008f88e17c05bae3ac862d5',
    'translation_pair_score': 'dd81ef0c68d4fe9235fc32fd7eb0816537bc91716f0ac69c9668a4ca4920d948',
}

if PRINT_GOLDEN:
    print("GOLDEN = {")
    for k in sorted(_observed):
        print(f"    {k!r}: {_observed[k]!r},")
    print("}")
    sys.exit(0)

for label in sorted(_observed):
    check(f"SNAP {label}: output identical to the pre-CP3.4 baseline", GOLDEN.get(label) == _observed[label],
          f"expected {GOLDEN.get(label)} got {_observed[label]}")
check("SNAP: every pinned output was observed", set(GOLDEN) == set(_observed), sorted(set(GOLDEN) ^ set(_observed)))

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_ANALYSIS_ALGORITHMS.json").write_text(json.dumps({
    "suite": "analysis algorithms (CP3.4)", "tests": total, "pass": passed, "fail": total - passed, "digests": _observed,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
