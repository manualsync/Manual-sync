"""CP4 regression: SAFETY characterization of the table-cell change gate (_cell_change family).

_cell_change decides, cell by cell, whether a translated table cell may be reused
or must be BLOCKED for technical or safety review. Until CP4 no regression suite
exercised it (only environment_guard.main(), which runs at BUILD time): a change to
this gate could pass with a fully green sweep. This suite pins what the gate does
TODAY (HEAD 0889892) -- it judges nothing, it fixes the current behaviour, quirks
included, so that any future change has to be deliberate.

Why the family cannot simply be moved (proved below, not just stated)
----------------------------------------------------------------------
layout_engine was extended release after release by REDEFINING names further down
the file (v0.10, v0.11.0 ... v0.11.6). Functions look their helpers up by name at
CALL time, so the behaviour is the composition of the LAST binding of every name:
_v11_numeric_tokens, defined once, uses the _V11_NUM_RE and _v11_number redefined
after it. Hence:

* LB1 pins which definition of every name is live (line, number of definitions);
* LB2 proves compare_tables -- the live one and the older one still alive through
  an alias -- resolve _cell_change at call time;
* LB3 rebuilds the same functions bound to the helpers of an earlier release (in
  memory) and shows the verdict changes on a pinned case: moving a function without
  ALL its final helpers changes the safety decision;
* LB4 recompiles the module in memory with an assignment placed before / after the
  last `def _table_semantic`: placed before, it is silently overwritten -- an import
  put in the wrong place would be ignored without any error;
* LB5 no module-level statement captures a family name (no alias to worry about).

Everything else: a matrix of ~170 synthetic cell pairs and inputs through every
function of the family, compare_tables on five synthetic table scenarios, and
analyze_layouts on three synthetic PDFs generated here (deterministic bytes).
Section M proves the golden digests are sensitive: putting an earlier release's
numeric regex back makes them fail. Golden digests were pinned after three
identical runs. Nothing is written outside a temporary folder.

Re-pin only after a reviewed, intentional behaviour change:
    ..\\.venv\\Scripts\\python.exe regressione_table_change_safety.py --print-golden

Usage:  ..\\.venv\\Scripts\\python.exe regressione_table_change_safety.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import __future__
import ast
import hashlib
import json
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()

import fitz  # noqa: E402
import pandas as pd  # noqa: E402

import layout_engine as le  # noqa: E402

PRINT_GOLDEN = "--print-golden" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="erylos_cp4_tables_"))
LE_SRC = (BASE / "layout_engine.py").read_text(encoding="utf-8")
LE_TREE = ast.parse(LE_SRC)
_results: list[tuple[str, bool, str]] = []
_observed: dict[str, str] = {}


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:700]))
    if not PRINT_GOLDEN:
        print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:700]}]"))


def plain(o):
    if isinstance(o, pd.DataFrame):
        return {"columns": [str(c) for c in o.columns], "dtypes": [str(t) for t in o.dtypes],
                "rows": plain(o.to_dict(orient="records"))}
    if isinstance(o, dict):
        return [[plain(k), plain(v)] for k, v in o.items()]
    if isinstance(o, (list, tuple)):
        return [plain(x) for x in o]
    if isinstance(o, (set, frozenset)):
        return sorted(plain(x) for x in o)
    if isinstance(o, float):
        return "NaN" if o != o else repr(o)
    if isinstance(o, (bytes, bytearray)):
        return "sha256:" + hashlib.sha256(bytes(o)).hexdigest()
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):      # numpy scalars
        return plain(o.item())
    return o


def digest(o) -> str:
    text = json.dumps(plain(o), ensure_ascii=False, default=repr)
    for root in (str(TMP), str(test_isolation.TEST_HOME)):
        for form in (root.replace("\\", "\\\\"), root, root.replace("\\", "/")):
            text = text.replace(json.dumps(form)[1:-1], "<TMP>").replace(form, "<TMP>")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snap(label, o):
    _observed[label] = digest(o)


def outcome(fn, *a):
    try:
        return ("ok", fn(*a))
    except Exception as exc:  # noqa: BLE001 -- the observed behaviour IS the exception
        return ("raise", type(exc).__name__, str(exc))


def defs_of(name):
    return [st for st in LE_TREE.body if isinstance(st, ast.FunctionDef) and st.name == name]


def assigns_of(name):
    return [st for st in LE_TREE.body if isinstance(st, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in st.targets)]


def source_of(node):
    return "\n".join(LE_SRC.splitlines()[node.lineno - 1:node.end_lineno])


BLOCKED_TECH = "BLOCCATO — REVISIONE TECNICA"
BLOCKED_SAFETY = "BLOCCATO — REVISIONE SICUREZZA"
REUSE = "RIUSO CELLA CONSENTITO"

# =================================================================== LB1: the live version of every name
# name -> (line of the LIVE definition, number of module-level bindings of the name)
ACTIVE = {
    "_cell_change": (3204, 2), "_table_semantic": (3438, 6), "_table_cat": (3442, 4),
    "_v11_table_semantic": (3425, 3), "_v11_numeric_tokens": (3061, 1), "_v11_number": (3414, 2),
    "_safety_level": (1370, 1), "_table_tokens": (2543, 1), "_table_cell_is_technical": (2553, 1),
    "_clean_table_cell": (2371, 1), "compare_tables": (2865, 6), "analyze_layouts": (2979, 8),
}
observed_active = {n: (getattr(le, n).__code__.co_firstlineno, len(defs_of(n))) for n in ACTIVE}
check("LB1: the live definition of each family name (line) and its number of definitions are the pinned ones",
      observed_active == ACTIVE, {n: v for n, v in observed_active.items() if v != ACTIVE[n]})
num_re = assigns_of("_V11_NUM_RE")
check("LB1b: _V11_NUM_RE is bound twice and the live pattern is the v0.11.6 one (optional space after the sign)",
      [a.lineno for a in num_re] == [3042, 3412] and le._V11_NUM_RE.pattern == r"(?<![A-Za-z0-9])[-+]?\s*\d+(?:[.,]\d+)?(?![A-Za-z0-9])",
      ([a.lineno for a in num_re], le._V11_NUM_RE.pattern))

# =================================================================== the case matrix
PAIRS = [
    # A. new cells
    ("", "Tensione nominale"), ("", "F001"), ("", "M8"), ("", "25"), ("", "400 V"), ("", "25 Nm"), ("", "50 Hz"),
    ("", "Avvertenza: superficie calda"), ("", "PERICOLO tensione pericolosa"), ("", "WARNING hot surface"),
    ("", "DANGER high voltage"), ("", "Attenzione"), ("", "IP54"), ("", "-2.9"),
    # B. removed cells
    ("Tensione nominale", ""), ("F001", ""), ("PERICOLO tensione", ""), ("25 Nm", ""),
    # C. unchanged
    ("Tensione nominale", "Tensione nominale"), ("Vite M8", "Vite  M8"), ("Coppia", "COPPIA"),
    ("Tensio­ne", "Tensione"), ("400 V", "400 V"), ("  F001 ", "F001"),
    # D. typography / pagination only
    ("Valore…", "Valore..."), ("10–20", "10-20"), ("10—20", "10-20"), ("≥ 5", ">= 5"), ("≤ 5", "<= 5"),
    ("Sicu-rezza", "Sicurezza"), ("2,5", "2.5"), ("Nota (1)", "Nota(1)"), ("Nota 1)", "Nota1)"), ("−5", "-5"),
    # E. numbers
    ("15.8", "25.27"), ("-2.9", "2.9"), ("- 5", "-5"), ("-6", "6"), ("0", "-0"), ("+5", "5"), ("5", "5.0"),
    ("3.14159265358979", "3.14159265358978"), ("1,5", "1.5"), ("400", "400"), ("400", "401"), ("12.50", "12.5"),
    ("Valore 10", "Valore 20"), ("R2", "R3"), ("X12", "X13"), ("10 - 20", "10-20"), ("1e3", "1000"),
    # F. technical codes
    ("F001", "F002"), ("P01.01", "P01.02"), ("A1…A5", "A1…A6"), ("ACS880-01", "ACS880-04"), ("K1, K2", "K1, K3"),
    ("Codice F001 attivo", "Codice F001 attivo."), ("IP54", "IP 54"), ("IP54", "IP55"),
    # G. units
    ("25 Nm", "25 Ncm"), ("25 Nm", "30 Nm"), ("400 V", "400 kV"), ("50 Hz", "60 Hz"), ("Nm", "N·m"), ("°C", "°F"),
    ("45 °C", "45°C"), ("10 %", "10%"),
    # H. references
    ("vedere pagina 12", "vedere pagina 14"), ("vedi sezione 3.2", "vedi sezione 3.4"), ("Fig. 5", "Fig. 6"),
    ("Tabella 2", "Tabella 3"), ("p. 12", "p. 12"),
    # I. safety levels
    ("Avvertenza", "Avvertenza"), ("Tensione residua", "PERICOLO tensione residua"),
    ("PERICOLO tensione residua", "Tensione residua"), ("Attenzione: caldo", "Attenzione: molto caldo"),
    ("WARNING", "CAUTION"), ("DANGER", "DANGER!"), ("Nota", "Avvertenza"), ("Rischio di scossa", "Rischio di ustione"),
    # J. empty cells
    ("", ""), (" ", ""), ("", " "), ("  ", "   "), ("-", "-"), ("-", ""),
    # K. empty semantic keys
    ("—", "…"), ("…", "—"), ("*", "**"), ("()", "[]"), ("/", "\\"),
    # L. normalization edge cases
    ("±5", "+5"), ("5 µs", "5 us"), ("10%", "10 %"), ("°", "º"), ("A\nB", "A B"), ("Tab\tella", "Tab ella"),
    ("ÀÉÎ", "àéî"), ("x²", "x2"), ("½", "0.5"),
]
CELLS = sorted({c for p in PAIRS for c in p} | {"Coppia M8 25 Nm", "Codice errore F001 e F002", "UN = 400 V",
                                                 "R1 v2", "ILD 12 A", "Tensione 230 V AC 50 Hz"})

verdicts = [le._cell_change(o, n) for o, n in PAIRS]
check("C1: every verdict is a 5-tuple (status, risk, action, match, float score)",
      all(isinstance(v, tuple) and len(v) == 5 and isinstance(v[4], float) for v in verdicts), verdicts[:2])
case = dict(zip(PAIRS, verdicts))
check("C2: the build-time numeric gates are BLOCKED for technical review (15.8->25.27, -2.9->2.9, -6->6)",
      all(case[p][2] == BLOCKED_TECH for p in [("15.8", "25.27"), ("-2.9", "2.9"), ("-6", "6")]),
      {p: case[p] for p in [("15.8", "25.27"), ("-2.9", "2.9"), ("-6", "6")]})
check("C3: '- 5' -> '-5' is unchanged (v0.11.6 sign-space canonicalisation) -- reuse allowed",
      case[("- 5", "-5")][:3] == ("INVARIATO", "BASSO", REUSE), case[("- 5", "-5")])
check("C4: a new or changed safety cell is BLOCKED for safety review",
      case[("", "PERICOLO tensione pericolosa")][2] == BLOCKED_SAFETY
      and case[("Tensione residua", "PERICOLO tensione residua")][2] == BLOCKED_SAFETY,
      [case[("", "PERICOLO tensione pericolosa")], case[("Tensione residua", "PERICOLO tensione residua")]])
check("C5 (pinned quirk): two empty cells are 'INVARIATO 100% MATCH'", case[("", "")][:4] ==
      ("INVARIATO", "BASSO", REUSE, "100% MATCH"), case[("", "")])
check("C6 (pinned quirk): 'IP54' -> 'IP 54' is BLOCKED as a numeric change (54 is a token only when spaced)",
      case[("IP54", "IP 54")][2] == BLOCKED_TECH, case[("IP54", "IP 54")])
check("C7: two cells with EMPTY semantic keys are never a 'CONTEXT MATCH'",
      all(case[p][3] != "CONTEXT MATCH" for p in [("—", "…"), ("…", "—"), ("*", "**"), ("()", "[]")]),
      {p: case[p] for p in [("—", "…"), ("*", "**")]})
snap("_cell_change", list(zip(PAIRS, verdicts)))
for fn_name in ("_clean_table_cell", "_table_semantic", "_table_cat", "_v11_table_semantic", "_v11_numeric_tokens",
                "_safety_level", "_table_tokens", "_table_cell_is_technical"):
    snap(fn_name, [(c, getattr(le, fn_name)(c)) for c in CELLS])
NUMS = ["5", "-5", "- 5", "+5", "0", "-0", "2,5", "2.50", "1e3", "abc", "", " 7 ", "3.14159265358979"]
snap("_v11_number", [(x, le._v11_number(x)) for x in NUMS])
snap("_V11_NUM_RE", [le._V11_NUM_RE.pattern] + [(c, le._V11_NUM_RE.findall(c)) for c in CELLS])
check("C8: _safety_level is 3 for danger, 2 for warning, 0 otherwise -- on the pinned words",
      (le._safety_level("PERICOLO"), le._safety_level("Avvertenza"), le._safety_level("Tensione")) in
      {(3, 2, 0)}, (le._safety_level("PERICOLO"), le._safety_level("Avvertenza"), le._safety_level("Tensione")))
errors = {n: outcome(getattr(le, n), None) for n in ("_clean_table_cell", "_table_semantic", "_table_cat",
                                                      "_v11_numeric_tokens", "_v11_number", "_safety_level",
                                                      "_table_tokens", "_table_cell_is_technical")}
errors["_cell_change(None, 'x')"] = outcome(le._cell_change, None, "x")
errors["_cell_change('x', None)"] = outcome(le._cell_change, "x", None)
snap("errors", errors)

# =================================================================== I1: compare_tables on synthetic layouts
def tb(i, rows):
    return le.TableBlock(i, 0, (50.0, 100.0, 500.0, 400.0), [list(r) for r in rows])


def lay(*tables):
    return le.DocumentLayout(1, [0], [], [tb(i, t) for i, t in enumerate(tables)], [])


OLD = [["Codice", "Parametro", "Valore", "Unita"],
       ["P01.01", "Tensione nominale", "400", "V"], ["P01.02", "Frequenza", "50", "Hz"],
       ["P02.10", "Coppia M8", "15.8", "Nm"], ["P02.11", "Coppia M10", "32", "Nm"],
       ["P03.05", "Temperatura minima", "-2.9", "°C"], ["P03.06", "Temperatura massima", "45", "°C"],
       ["P04.01", "Offset", "- 5", "mV"], ["P04.02", "Corrente", "12.5", "A"],
       ["P05.01", "Avvertenza", "Tensione residua", "-"], ["P05.02", "Grado IP", "IP54", "-"]]
TR = [["Code", "Parameter", "Value", "Unit"]] + [[r[0], r[1] + " (EN)", r[2], r[3]] for r in OLD[1:]]
NEW = [list(r) for r in OLD]
NEW[3][2], NEW[5][2], NEW[7][2], NEW[9][2], NEW[10][2] = "25.27", "2.9", "-5", "PERICOLO tensione residua", "IP 54"
NEW_ROWS = [r for r in OLD if r[0] != "P04.02"] + [["P06.01", "Tempo di rampa", "10", "s"]]
SCENARIOS = {
    "S1_identical": (lay(OLD), lay(TR), lay(OLD)),
    "S2_changed_values": (lay(OLD), lay(TR), lay(NEW)),
    "S3_row_added_removed": (lay(OLD), lay(TR), lay(NEW_ROWS)),
    "S4a_new_table_only": (lay(), lay(), lay(NEW)),
    "S4b_old_table_removed": (lay(OLD), lay(TR), lay()),
    "S5_no_translation": (lay(OLD), lay(), lay(NEW)),
}
frames = {k: le.compare_tables(*v) for k, v in SCENARIOS.items()}
s1, s2 = frames["S1_identical"], frames["S2_changed_values"]
check("I1a: identical tables -> every cell 'RIUSO CELLA CONSENTITO'", len(s1) == 44 and set(s1["decisione_cella"]) == {REUSE},
      sorted(set(s1["decisione_cella"])))
blocked = dict(zip(s2["nuova_cella"], s2["decisione_cella"]))
check("I1b: changed values -> 25.27, 2.9 and 'IP 54' BLOCKED technical, the safety cell BLOCKED safety, '-5' reused",
      blocked.get("25.27") == BLOCKED_TECH and blocked.get("2.9") == BLOCKED_TECH and blocked.get("IP 54") == BLOCKED_TECH
      and blocked.get("PERICOLO tensione residua") == BLOCKED_SAFETY and blocked.get("-5") == REUSE,
      {k: blocked.get(k) for k in ("25.27", "2.9", "IP 54", "PERICOLO tensione residua", "-5")})
check("I1c: the 20 output columns, in order", list(s2.columns) == [
    "tabella_vecchia", "tabella_nuova", "riga_vecchia", "riga_nuova", "colonna", "chiave_riga", "intestazione_vecchia",
    "intestazione_nuova", "stato", "rischio", "categoria_match", "similarita", "vecchia_cella", "nuova_cella",
    "vecchia_traduzione_cella", "affidabilita_traduzione", "valori_codici_vecchi", "valori_codici_nuovi",
    "decisione_cella", "motivo"], list(s2.columns))
for k, f in frames.items():
    snap(f"compare_tables_{k}", f)
check("I1d: the old compare_tables kept alive by an alias still runs on the same inputs",
      isinstance(le._compare_tables_v10_pre_translation_patch(*SCENARIOS["S2_changed_values"]), pd.DataFrame), "")


# =================================================================== I2: analyze_layouts on synthetic PDFs
def make_pdf(path, title, paras, rows, *, img_rgb):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 60
    page.insert_text((50, y), title, fontsize=16, fontname="helv")
    y += 30
    for p in paras:
        page.insert_text((50, y), p, fontsize=10, fontname="helv")
        y += 18
    top, x0, colw, rowh = y + 10, 50, [80, 170, 160, 80], 22
    for r in range(len(rows) + 1):
        page.draw_line((x0, top + r * rowh), (x0 + sum(colw), top + r * rowh), width=0.8)
    x = x0
    for c in range(len(colw) + 1):
        page.draw_line((x, top), (x, top + len(rows) * rowh), width=0.8)
        x += colw[c] if c < len(colw) else 0
    for r, row in enumerate(rows):
        x = x0
        for c, cell in enumerate(row):
            page.insert_text((x + 4, top + r * rowh + 15), cell, fontsize=9, fontname="helv")
            x += colw[c]
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, img_rgb)
    page.insert_image(fitz.Rect(400, 720, 480, 800), pixmap=pix)
    doc.save(str(path), garbage=4, deflate=True, no_new_id=True)
    doc.close()


P_OLD, P_TR, P_NEW = TMP / "old_IT.pdf", TMP / "old_EN.pdf", TMP / "new_IT.pdf"
make_pdf(P_OLD, "Manuale tecnico", ["Installare il dispositivo nel quadro.", "Scollegare l'alimentazione."], OLD,
         img_rgb=(50, 100, 200))
make_pdf(P_TR, "Technical manual", ["Install the device in the cabinet.", "Disconnect the supply."], TR,
         img_rgb=(50, 100, 200))
make_pdf(P_NEW, "Manuale tecnico", ["Installare il dispositivo nel quadro.", "Scollegare sempre l'alimentazione."], NEW,
         img_rgb=(200, 50, 50))
pdf_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (P_OLD, P_TR, P_NEW)]
detected = [len(le.extract_layout(p).tables) for p in (P_OLD, P_TR, P_NEW)]
check("I2a: the synthetic PDFs are byte-deterministic and each has one detected table", detected == [1, 1, 1], detected)
text_df, table_df, image_df, summary = le.analyze_layouts(P_OLD, P_TR, P_NEW)
check("I2b: analyze_layouts pairs the two table revisions and blocks the changed cells",
      summary.get("tabelle_vecchie") == 1 and summary.get("tabelle_nuove") == 1
      and (table_df["decisione_cella"] == BLOCKED_TECH).sum() >= 2, {k: summary.get(k) for k in (
          "tabelle_vecchie", "tabelle_nuove", "celle_tabelle_sicurezza", "celle_tabelle_tecniche",
          "riusi_celle_tabelle_consentiti")})
snap("synthetic_pdf_bytes", pdf_hashes)
snap("analyze_layouts_summary", summary)
snap("analyze_layouts_table_df", table_df)
snap("analyze_layouts_text_df", text_df)
snap("analyze_layouts_image_df", image_df)

# =================================================================== I3: the build-time engine gates, now in the sweep
check("I3: environment_guard's numeric gates hold here too (they used to run only at build time)",
      all(le._cell_change(o, n)[2] == BLOCKED_TECH for o, n in [("15.8", "25.27"), ("-2.9", "2.9"), ("-6", "6")]), "")

# =================================================================== LB2: helpers resolved at CALL time
calls = []
real = le._cell_change
le._cell_change = lambda o, n: (calls.append((o, n)), real(o, n))[1]
try:
    le.compare_tables(*SCENARIOS["S2_changed_values"])
    live_calls = len(calls)
    calls.clear()
    le._compare_tables_v10_pre_translation_patch(*SCENARIOS["S2_changed_values"])
    alias_calls = len(calls)
finally:
    le._cell_change = real
check("LB2: compare_tables -- live AND the aliased older version -- look _cell_change up at call time",
      live_calls > 0 and alias_calls > 0 and le._cell_change is real, (live_calls, alias_calls))


# =================================================================== LB3: same code, earlier helpers -> different verdict
def older(name, version_index):
    nodes = defs_of(name) or assigns_of(name)
    return source_of(nodes[version_index])


sandbox = dict(vars(le))
flags = __future__.annotations.compiler_flag
for name, idx in [("_V11_NUM_RE", 0), ("_v11_number", 0), ("_v11_table_semantic", 1), ("_table_semantic", 4),
                  ("_table_cat", 2)]:
    exec(compile(older(name, idx), f"<{name}-older>", "exec", flags=flags, dont_inherit=True), sandbox)
for name in ("_v11_numeric_tokens", "_cell_change"):        # the SAME live code, now bound to the older helpers
    exec(compile(source_of(defs_of(name)[-1]), f"<{name}-live>", "exec", flags=flags, dont_inherit=True), sandbox)
hazard_pair = ("- 5", "-5")
moved_wrong = sandbox["_cell_change"](*hazard_pair)
check("LB3: the live _cell_change bound to the previous release's helpers BLOCKS '- 5'->'-5' that the product "
      "reuses: moving it without every final helper changes a safety decision",
      le._cell_change(*hazard_pair)[2] == REUSE and moved_wrong[2] == BLOCKED_TECH, [le._cell_change(*hazard_pair), moved_wrong])
check("LB3b: the sandbox did not touch the real module", le._V11_NUM_RE.pattern.find(r"\s*") > 0 and
      le._table_semantic.__code__.co_firstlineno == 3438, "")

# =================================================================== LB4: binding order -- an early import is overwritten
def run_with(sentinel_after_line: int):
    lines = LE_SRC.splitlines()
    lines.insert(sentinel_after_line, "_table_semantic = '__CP4_SENTINEL__'")
    probe = types.ModuleType("layout_engine_cp4_probe")       # @dataclass needs its module in sys.modules
    probe.__file__ = str(BASE / "layout_engine.py")
    sys.modules[probe.__name__] = probe
    try:
        exec(compile("\n".join(lines), "<layout_engine-probe>", "exec", dont_inherit=True), probe.__dict__)
        return probe.__dict__["_table_semantic"]
    finally:
        del sys.modules[probe.__name__]


last_def = defs_of("_table_semantic")[-1]
before = run_with(last_def.lineno - 1)                  # placed just BEFORE the last `def _table_semantic`
after = run_with(last_def.end_lineno)                   # placed right AFTER it
check("LB4: a binding placed before the last `def _table_semantic` is silently overwritten by it; placed after, it wins",
      callable(before) and before.__code__.co_firstlineno == last_def.lineno + 1 and after == "__CP4_SENTINEL__",
      [getattr(before, "__code__", before), after])

# =================================================================== LB5: no alias captures the family
family = set(ACTIVE) | {"_V11_NUM_RE"}
reads = [(st.lineno, n.id) for st in LE_TREE.body if not isinstance(st, (ast.FunctionDef, ast.ClassDef))
         for n in ast.walk(st) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
         and n.id in family - {"compare_tables", "analyze_layouts"}]
check("LB5: no module-level statement reads a cell-gate helper (no alias captures an older version)", not reads, reads)

# =================================================================== M: the golden digests are sensitive
first_golden = dict(_observed)
old_re = re.compile(ast.literal_eval(assigns_of("_V11_NUM_RE")[0].value.args[0]))
saved = le._V11_NUM_RE
le._V11_NUM_RE = old_re
try:
    mutated = digest(list(zip(PAIRS, [le._cell_change(o, n) for o, n in PAIRS])))
    mutated_tokens = digest([(c, le._v11_numeric_tokens(c)) for c in CELLS])
finally:
    le._V11_NUM_RE = saved
check("M1: putting the v0.11.0 numeric regex back changes the pinned digests (_cell_change, _v11_numeric_tokens)",
      mutated != first_golden["_cell_change"] or mutated_tokens != first_golden["_v11_numeric_tokens"],
      [mutated[:12], first_golden["_cell_change"][:12]])
check("M2: the real regex is restored", le._V11_NUM_RE is saved, "")

check("N1: no network access attempted", not GUARD.snapshot(), GUARD.snapshot())

# Pinned at HEAD 0889892 (CP4) after three identical runs. Two equalities are expected, not
# accidents: _table_semantic / _table_cat / _v11_table_semantic share a digest (both live versions
# delegate to _v11_table_semantic L3425), and analyze_layouts' table_df equals compare_tables S2
# (the synthetic PDFs carry exactly the S2 tables).
GOLDEN = {
    '_V11_NUM_RE': '6922d3b3921cb57372d4ae11ffe8dcd4de2242ff97e838b95ea31e122a4485f7',
    '_cell_change': 'fb2da7b3ed86b743988f16a75e500fd08e3898dc1247b947050f0e21fe4dede4',
    '_clean_table_cell': '4749fe0ff68ee0786f873ed681678ae14b2867ae36a93149d1fe66c5e1e481e5',
    '_safety_level': 'caf1d05818992b46e91723aaeb8f3f4ade17f8be5db909f20d9b6f568ad5723d',
    '_table_cat': '8325ac43f97d3976878a9d3523c952e8cc200a749f0908f6534819b89bc441bc',
    '_table_cell_is_technical': '1bc812ee26c7cfefb6a57345bf272e1e4fc1ed99a68bc424e9861438a3ee2d3c',
    '_table_semantic': '8325ac43f97d3976878a9d3523c952e8cc200a749f0908f6534819b89bc441bc',
    '_table_tokens': 'd5d4f4befb376c9928897537666b73c42a51c179cbaf90af1e56f21b290c358d',
    '_v11_number': '5162bde59bae6469e92f275234f7ffd570fd3084a9513ada8131f0832e1d8054',
    '_v11_numeric_tokens': '641e43c448577e58c8dae608db354fb419f6166558547aec1d3d774ed6cff074',
    '_v11_table_semantic': '8325ac43f97d3976878a9d3523c952e8cc200a749f0908f6534819b89bc441bc',
    'analyze_layouts_image_df': 'ec82bee561c4bd66cd709c30144092e73ffe8db8e0f4c58b548d34c2243065f8',
    'analyze_layouts_summary': '37ae724a866430cc9139d96a7a6bb4dfb222ef3f7749c459fe2fbdfa527ba673',
    'analyze_layouts_table_df': '5cf15c83d924e4b065d310915c4d76e0b4c5e1559b1e9c140c4828b54ae433c1',
    'analyze_layouts_text_df': '99232fcf131ddf1059ad8e021dfc04fbfa4bb30c28a8adebc1622ed672e8d1a3',
    'compare_tables_S1_identical': 'a01499fb0d59178b78233f4c76680d837118fa6d54727f1648cbb5347e6d4d5b',
    'compare_tables_S2_changed_values': '5cf15c83d924e4b065d310915c4d76e0b4c5e1559b1e9c140c4828b54ae433c1',
    'compare_tables_S3_row_added_removed': 'bd1f8c2e0aa08c5dd0c1bc8fed62a163d223313d8aee230b9586ad42f3dfa008',
    'compare_tables_S4a_new_table_only': 'ad1644f66d082a3750f91e00f77e18a3f8f64a261b3874de4c25d3065af378bf',
    'compare_tables_S4b_old_table_removed': 'ce0e87cbdc80e41bdb85cc4023d26daee28c7df7ddb8edd1b3adf02f655a8923',
    'compare_tables_S5_no_translation': '80aa5f5549c53aa03d6b03f830ec5d20e8de2cfd9ffbf2b371466ba8fe2ec05d',
    'errors': '861d7233d2d2b0be838909c8c9ba41ced0b5568b73fb8d6ad7d0bdda8b9a3270',
    'synthetic_pdf_bytes': 'b1f3f3ca134f59d95d285f285cd25f61c7c39bd8b054e98cd6dffdab5eca0f81',
}

if PRINT_GOLDEN:
    print("GOLDEN = {")
    for k in sorted(_observed):
        print(f"    {k!r}: {_observed[k]!r},")
    print("}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

for label in sorted(_observed):
    check(f"SNAP {label}: identical to the CP4 baseline", GOLDEN.get(label) == _observed[label],
          f"expected {GOLDEN.get(label)} got {_observed[label]}")
check("SNAP: every pinned output was observed", set(GOLDEN) == set(_observed), sorted(set(GOLDEN) ^ set(_observed)))

shutil.rmtree(TMP, ignore_errors=True)
check("T1: the temporary fixtures were removed", not TMP.exists(), TMP)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_TABLE_CHANGE_SAFETY.json").write_text(json.dumps({
    "suite": "table change safety (CP4)", "tests": total, "pass": passed, "fail": total - passed, "digests": _observed,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
