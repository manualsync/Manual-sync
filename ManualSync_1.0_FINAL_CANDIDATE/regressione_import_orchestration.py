"""CP5.2a regression: CHARACTERIZATION of the import orchestration in app.import_manual_ui.

import_manual_ui (app.py) is a UI callback that orchestrates the migration import: it
refuses a re-import once the publication holds reviewed content, RETIRES the previous
migration draft with DELETEs on publication_topics, topics, structured_tables and
figures (content_blocks, translations and versions go with them through ON DELETE
CASCADE), updates `publications`, copies the uploaded file into Sources/ and writes the
IDML review state. Until CP5.2b it issued that SQL itself; CP5.2b-1 moved the 11 SQL
calls, verbatim, into IndustrialRepository (migration_draft_objects,
retire_migration_draft, publication_exists, update_publication_header,
structured_table_count) and left the orchestration -- order, the two commits, every
exception -- where it was. This suite pinned the behaviour at HEAD d4f7c45, BEFORE
the move, quirks included; every observation below stayed identical across it:

* S01-S03   new import: PDF, DOCX, IDML (table, figure, orphan story -> review item);
* S04       PDF re-import over an IDML draft that carries translations: what the
            retirement deletes, what the cascade takes, what is left behind;
* S05/S06   DOCX over DOCX (publication title/language UPDATE), IDML over IDML
            (the non-destructive path: retired only after the new parse succeeded);
* S07       refusal: a topic no longer 'Migrated' (Approved, and also Draft) blocks the
            re-import -- ValueError, database untouched, but the PDF is already in Sources/;
* S08-S11   failures: corrupt PDF, empty PDF, corrupt DOCX run AFTER the old draft was
            retired (the publication is left without topics -- pinned, not fixed); a
            corrupt IDML leaves the old draft byte-for-byte untouched;
* S12       .doc through the converter seam (success and failure);
* S13/S14   the gr.Error guard and the argument defaults (id, language, title);
* S15       the guided onboarding (_guided_create_and_import) that reuses the same path;
* S16       (CP5.2b-0) the publication title/language asymmetry: the FIRST import goes through
            create_publication (title stripped, language canonicalized: "it-it" -> "it-IT"),
            a RE-import through the inline UPDATE (both stored as received) -- pinned so that
            moving that UPDATE cannot "unify" the two paths silently;
* R1-R4     (CP5.2b-1) the structure after the move: import_manual_ui issues no SQL, calls the
            five IndustrialRepository methods, the draft is still retired at the same two points,
            and those methods hold exactly the 11 original statements, in order, with the two
            commits (frozen copy below); a SQL call put back into import_manual_ui is caught.

For each scenario the WHOLE content database (every table, every row, in rowid
order), the project files (Sources/, review JSON, manifest) and the returned
message/DataFrames are captured before and after. Only UTC timestamps and the
temporary folder are normalized; ids, statuses, texts, relations and counts are
not. Golden digests were pinned after three identical runs; section M proves they
are sensitive. Nothing is written outside a temporary folder.

Re-pin only after a reviewed, intentional behaviour change:
    ..\\.venv\\Scripts\\python.exe regressione_import_orchestration.py --print-golden

Usage:  ..\\.venv\\Scripts\\python.exe regressione_import_orchestration.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import contextlib
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()

import fitz  # noqa: E402
from docx import Document  # noqa: E402

import app  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from legacy_doc_conversion import LegacyDocConversionError  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

PRINT_GOLDEN = "--print-golden" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="erylos_cp52a_import_"))
INPUTS = TMP / "inputs"
INPUTS.mkdir()
_results: list[tuple[str, bool, str]] = []
_observed: dict[str, str] = {}


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:700]))
    if not PRINT_GOLDEN:
        print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:700]}]"))


# ------------------------------------------------------------ normalization: timestamps, the temp folder, random keys
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?")
# idml_import draws the caption-candidate key from uuid4 (idml_import.py, candidate_key = uuid4().hex[:12]).
# It is written twice -- in the figure's source_refs and as the review item's item_key -- so it is
# replaced CONSISTENTLY (same key -> same placeholder): the figure <-> review-item link stays pinned.
_CAPTION_KEY = re.compile(r'(idml_caption_candidate_key=|\["item_key", ")([0-9a-f]{12})')   # plain(): dicts are [k, v] pairs


def plain(o):
    if hasattr(o, "to_dict") and hasattr(o, "columns"):          # pandas DataFrame
        return {"columns": [str(c) for c in o.columns], "rows": plain(o.to_dict(orient="records"))}
    if isinstance(o, dict):
        return [[plain(k), plain(v)] for k, v in o.items()]
    if isinstance(o, (list, tuple)):
        return [plain(x) for x in o]
    if isinstance(o, float):
        return "NaN" if o != o else repr(o)
    if isinstance(o, Path):
        return str(o)
    return o


def normalized(o) -> str:
    text = json.dumps(plain(o), ensure_ascii=False, default=repr)
    for root in (str(TMP), str(test_isolation.TEST_HOME)):
        for form in (root.replace("\\", "\\\\"), root, root.replace("\\", "/")):
            text = text.replace(json.dumps(form)[1:-1], "<TMP>").replace(form, "<TMP>")
    keys: dict[str, str] = {}
    text = _CAPTION_KEY.sub(lambda m: m.group(1) + keys.setdefault(m.group(2), f"<CKEY{len(keys) + 1}>"), text)
    return _TS.sub("<TS>", text)


def digest(o) -> str:
    return hashlib.sha256(normalized(o).encode("utf-8")).hexdigest()


def snap(label, o):
    _observed[label] = digest(o)


# ------------------------------------------------------------ synthetic inputs (deterministic content)
def make_pdf(name: str, sections) -> Path:
    path = INPUTS / name
    doc = fitz.open()
    for title, paras in sections:
        page = doc.new_page(width=595, height=842)
        if title:
            page.insert_text((50, 80), title, fontsize=18, fontname="hebo")
        for i, p in enumerate(paras):
            page.insert_text((50, 120 + 20 * i), p, fontsize=10, fontname="helv")
    doc.save(str(path), garbage=4, deflate=True, no_new_id=True)
    doc.close()
    return path


def make_docx(name: str, chapters) -> Path:
    path = INPUTS / name
    d = Document()
    for title, para, table in chapters:
        d.add_heading(title, level=1)
        d.add_paragraph(para)
        if table:
            t = d.add_table(rows=len(table), cols=len(table[0]))
            for r, row in enumerate(table):
                for c, val in enumerate(row):
                    t.cell(r, c).text = val
    d.save(str(path))
    return path


_MIMETYPE = b"application/vnd.adobe.indesign-idml-package"


def _para(style, text, extra=""):
    return (f'<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/{style}"><CharacterStyleRange>{extra}'
            f'<Content>{text}</Content></CharacterStyleRange></ParagraphStyleRange>')


def make_idml(name: str, heading: str, body: str, *, table, orphan: bool) -> Path:
    path = INPUTS / name
    figure = ('<Rectangle Self="r1" ItemTransform="1 0 0 1 5 5"><Link LinkResourceURI="file:///C:/nonexistent/pump.png" '
              'StoredState="Normal"/><Image/></Rectangle>')
    cells = "".join(f'<Cell Name="{r}:{c}">{_para("Body Text", v)}</Cell>' for r, row in enumerate(table) for c, v in enumerate(row))
    story = (f'<?xml version="1.0" encoding="UTF-8"?><Story Self="s1">{_para("Heading 1", heading)}{_para("Body Text", body)}'
             f'{_para("Body Text", "", figure)}{_para("Caption", "Didascalia pompa idraulica")}'
             f'<Table Self="tbl_s1">{cells}</Table></Story>')
    stories = ["Stories/Story_s1.xml"] + (["Stories/Story_o1.xml"] if orphan else [])
    designmap = ('<?xml version="1.0" encoding="UTF-8"?><Document xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging">'
                 '<idPkg:Spread src="Spreads/Spread_a.xml"/>' + "".join(f'<idPkg:Story src="{s}"/>' for s in stories) + '</Document>')
    parts = {"designmap.xml": designmap,
             "Spreads/Spread_a.xml": ('<?xml version="1.0" encoding="UTF-8"?><Spread Self="a">'
                                      '<TextFrame Self="tf1" ParentStory="s1" ItemTransform="1 0 0 1 0 0"/></Spread>'),
             "Stories/Story_s1.xml": story}
    if orphan:
        parts["Stories/Story_o1.xml"] = ('<?xml version="1.0" encoding="UTF-8"?><Story Self="o1">'
                                         f'{_para("Body Text", "Testo mai collegato a una pagina.")}</Story>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype", (2020, 1, 1, 0, 0, 0)), _MIMETYPE, compress_type=zipfile.ZIP_STORED)
        for part, content in parts.items():
            zf.writestr(zipfile.ZipInfo(part, (2020, 1, 1, 0, 0, 0)), content.encode("utf-8"))
    return path


PDF_A = make_pdf("Manuale_A.pdf", [("1 Introduzione", ["Il presente manuale descrive il sistema idraulico.",
                                                         "Leggere con attenzione prima dell'uso."]),
                                     ("2 Manutenzione", ["Scollegare l'alimentazione prima di ogni intervento.",
                                                         "Pressione massima ammessa 12 bar."])])
PDF_B = make_pdf("Manuale_B.pdf", [("1 Introduzione", ["Revisione B del manuale."]),
                                     ("2 Uso previsto", ["Uso esclusivamente industriale."]),
                                     ("3 Smaltimento", ["Smaltire secondo le norme locali."])])
PDF_EMPTY = make_pdf("Vuoto.pdf", [("", [])])
PDF_CORRUPT = INPUTS / "Rotto.pdf"
PDF_CORRUPT.write_bytes(b"%PDF-1.4 questo non e' un PDF")
DOCX_A = make_docx("Manuale_A.docx", [("1 Introduzione", "Testo introduttivo del capitolo uno.",
                                        [["Parametro", "Valore"], ["Tensione", "24 V"]]),
                                       ("2 Manutenzione", "Scollegare l'alimentazione.", None)])
DOCX_B = make_docx("Manuale_B.docx", [("1 Panoramica", "Testo della revisione B.",
                                        [["Grandezza", "Limite"], ["Pressione", "16 bar"], ["Temperatura", "60 C"]])])
DOCX_CORRUPT = INPUTS / "Rotto.docx"
DOCX_CORRUPT.write_bytes(b"questo non e' un docx")
DOC_LEGACY = INPUTS / "Legacy.doc"
DOC_LEGACY.write_bytes(b"segnaposto .doc (convertitore simulato)")
IDML_A = make_idml("Manuale_A.idml", "1 Introduzione", "Il presente manuale descrive il sistema idraulico.",
                   table=[["Parametro", "Valore"], ["Pressione", "10 bar"]], orphan=True)
IDML_B = make_idml("Manuale_B.idml", "1 Panoramica", "Revisione B del pacchetto InDesign.",
                   table=[["Grandezza", "Limite"], ["Portata", "40 l/min"]], orphan=False)
IDML_CORRUPT = INPUTS / "Rotto.idml"
IDML_CORRUPT.write_bytes(b"PK questo non e' un IDML")
INPUT_SHA = {hashlib.sha256(p.read_bytes()).hexdigest(): p.name for p in INPUTS.iterdir()}


# ------------------------------------------------------------ observation: database, files, orphans
def new_project(name: str) -> Path:
    return create_project(TMP / "projects", project_name=name, source_lang="it-IT")


def db_rows(project: Path) -> dict:
    con = sqlite3.connect(project / "ManualSync_CCMS.sqlite")
    con.row_factory = sqlite3.Row
    out = {}
    try:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            out[name] = [dict(r) for r in con.execute(f"SELECT * FROM {name} ORDER BY rowid")]
    finally:
        con.close()
    return out


ORPHAN_LINKS = [  # (child table, child column, parent table, parent column)
    ("content_blocks", "topic_id", "topics", "topic_id"),
    ("publication_topics", "topic_id", "topics", "topic_id"),
    ("publication_topics", "publication_id", "publications", "publication_id"),
    ("topic_translations", "topic_id", "topics", "topic_id"),
    ("block_translations", "block_id", "content_blocks", "block_id"),
    ("structured_table_translations", "table_id", "structured_tables", "table_id"),
    ("figure_translations", "figure_id", "figures", "figure_id"),
    ("topic_versions", "topic_id", "topics", "topic_id"),
    ("block_versions", "block_id", "content_blocks", "block_id"),
    ("structured_table_versions", "table_id", "structured_tables", "table_id"),
    ("figure_versions", "figure_id", "figures", "figure_id"),
]


def orphans(project: Path) -> dict:
    """Rows whose parent is gone, plus tables/figures no block points at."""
    con = sqlite3.connect(project / "ManualSync_CCMS.sqlite")
    out = {}
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for child, col, parent, pcol in ORPHAN_LINKS:
            if child in tables and parent in tables:
                out[f"{child}.{col}->{parent}"] = [r[0] for r in con.execute(
                    f"SELECT {col} FROM {child} WHERE {col} NOT IN (SELECT {pcol} FROM {parent}) ORDER BY rowid")]
        for kind, table, idcol in (("table", "structured_tables", "table_id"), ("figure", "figures", "figure_id")):
            out[f"{table} not referenced by a block"] = [r[0] for r in con.execute(
                f"SELECT {idcol} FROM {table} WHERE {idcol} NOT IN "
                f"(SELECT object_ref_id FROM content_blocks WHERE block_type=?) ORDER BY rowid", (kind,))]
    finally:
        con.close()
    return out


def files(project: Path) -> list:
    """Every project file: databases by name, uploaded copies by WHICH input they are, JSON by content."""
    out = []
    for p in sorted(project.rglob("*")):
        rel = p.relative_to(project).as_posix()
        if p.is_dir():
            out.append([rel + "/", "dir"])
        elif p.suffix == ".sqlite":
            out.append([rel, "sqlite"])
        elif p.suffix == ".json":
            out.append([rel, json.loads(p.read_text(encoding="utf-8"))])
        else:
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            out.append([rel, f"copy of {INPUT_SHA[sha]}" if sha in INPUT_SHA else f"sha256:{sha}"])
    return out


def state(project: Path) -> dict:
    return {"db": db_rows(project), "orphans": orphans(project), "files": files(project)}


def outcome(fn, *a, **kw):
    try:
        res = fn(*a, **kw)
        return ("ok", res)
    except Exception as exc:  # noqa: BLE001 -- the observed behaviour IS the exception
        return ("raise", type(exc).__name__, str(getattr(exc, "message", None) or exc))


def run(label: str, project: Path, *args):
    """import_manual_ui(project, *args): outcome + state before/after, pinned under `label`."""
    before = state(project)
    res = outcome(app.import_manual_ui, str(project), *[str(a) if isinstance(a, Path) else a for a in args])
    after = state(project)
    snap(label, {"before": before, "outcome": res, "after": after})
    return res, before, after


def count(st: dict, table: str) -> int:
    return len(st["db"].get(table, []))


def ids(st: dict, table: str, col: str) -> list:
    return [r[col] for r in st["db"].get(table, [])]


def message(res) -> str:
    return res[1][0] if res[0] == "ok" else res[2]


# ============================================================ S01-S03: new import
p01 = new_project("S01 PDF")
r01, b01, a01 = run("S01_pdf_new", p01, PDF_A, "it-IT", "PUB-MAIN", "Manuale PDF")
check("S01: a new PDF import creates the publication, one topic per section, one block per paragraph (status Migrated)",
      r01[0] == "ok" and ids(a01, "topics", "topic_id") == ["DOC-0001", "DOC-0002"] and count(a01, "content_blocks") == 4
      and {r["status"] for r in a01["db"]["topics"] + a01["db"]["content_blocks"]} == {"Migrated"}
      and [(r["publication_id"], r["title"], r["source_lang"]) for r in a01["db"]["publications"]]
      == [("PUB-MAIN", "Manuale PDF", "it-IT")], a01["db"].get("topics"))
check("S01b: the message is the PDF one, with the counts; topics/blocks DataFrames are returned",
      r01[0] == "ok" and message(r01).startswith("✅ Manuale importato come draft in **2 topic semantici** e **0 tabelle strutturate**")
      and len(r01[1][1]) == 2 and len(r01[1][2]) == 4, message(r01))
check("S01c: the uploaded PDF is copied into Sources/ byte-for-byte; nothing else is added to the project",
      ["Sources/Manuale_A.pdf", "copy of Manuale_A.pdf"] in a01["files"]
      and [f[0] for f in a01["files"] if f not in b01["files"]] == ["Sources/", "Sources/Manuale_A.pdf"],
      [f for f in a01["files"] if f not in b01["files"]])

p02 = new_project("S02 DOCX")
r02, b02, a02 = run("S02_docx_new", p02, DOCX_A, "it-IT", "PUB-MAIN", "Manuale DOCX")
check("S02: a new DOCX import creates topics, blocks and the structured table referenced by a table block",
      r02[0] == "ok" and count(a02, "topics") == 2 and ids(a02, "structured_tables", "table_id") == ["DOC-0001-T001"]
      and any(b["block_type"] == "table" and b["object_ref_id"] == "DOC-0001-T001" for b in a02["db"]["content_blocks"]),
      a02["db"].get("structured_tables"))

p03 = new_project("S03 IDML")
r03, b03, a03 = run("S03_idml_new", p03, IDML_A, "it-IT", "PUB-MAIN", "Manuale IDML")
review03 = [f for f in a03["files"] if f[0] == "ManualSync_IDML_Review_PUB-MAIN.json"]
check("S03: a new IDML import creates the section topic, the table, the figure (missing link) and their blocks",
      r03[0] == "ok" and ids(a03, "topics", "topic_id") == ["IDML-0001"] and count(a03, "structured_tables") == 1
      and count(a03, "figures") == 1 and message(r03).startswith("✅ Manuale InDesign importato come draft"), message(r03))
check("S03b: the orphan story and the caption candidate become two pending review items in "
      "ManualSync_IDML_Review_PUB-MAIN.json",
      review03 and [it.get("reason") for it in review03[0][1]] == ["orphan_story", "caption_candidate"]
      and all(it.get("decision") is None for it in review03[0][1]), review03)
_fig_key03 = [ref.split("=", 1)[1] for r in a03["db"].get("figures", []) for ref in json.loads(r["source_refs_json"])
              if ref.startswith("idml_caption_candidate_key=")]
check("S03d: the caption-candidate review item points at the figure through the same (random) key",
      review03 and _fig_key03 == [it["item_key"] for it in review03[0][1] if it.get("reason") == "caption_candidate"],
      [_fig_key03, review03])
check("S03c: the IDML is copied into Sources/ only after the successful import",
      ["Sources/Manuale_A.idml", "copy of Manuale_A.idml"] in a03["files"], a03["files"])

# ============================================================ S04: PDF re-import over an IDML draft carrying translations
p04 = new_project("S04 Reimport")
outcome(app.import_manual_ui, str(p04), str(IDML_A), "it-IT", "PUB-MAIN", "Bozza IDML")
repo = IndustrialRepository(p04 / "ManualSync_CCMS.sqlite")
try:
    _blk = repo.conn.execute("SELECT block_id FROM content_blocks WHERE block_type='paragraph' ORDER BY rowid").fetchone()[0]
    _tbl = repo.conn.execute("SELECT table_id FROM structured_tables").fetchone()[0]
    _fig = repo.conn.execute("SELECT figure_id FROM figures").fetchone()[0]
    repo.set_translation("IDML-0001", "en-US", "Translated body", target_title="Introduction")
    repo.set_block_translation(_blk, "en-US", "This manual describes the hydraulic system.")
    repo.set_table_translation(_tbl, "en-US", caption="Parameters")
    repo.set_figure_translation(_fig, "en-US", caption="Hydraulic pump", alt_text="pump")
finally:
    repo.close()
r04, b04, a04 = run("S04_pdf_reimport_over_idml_draft", p04, PDF_B, "it-IT", "PUB-MAIN", "Revisione B")
check("S04: the IDML draft is retired -- its topic, table and figure are DELETEd and the new PDF topics replace them",
      r04[0] == "ok" and ids(b04, "topics", "topic_id") == ["IDML-0001"]
      and ids(a04, "topics", "topic_id") == ["DOC-0001", "DOC-0002", "DOC-0003"]
      and count(a04, "structured_tables") == 0 and count(a04, "figures") == 0
      and [r["topic_id"] for r in a04["db"]["publication_topics"]] == ["DOC-0001", "DOC-0002", "DOC-0003"], a04["orphans"])
check("S04b: the cascade takes the blocks and every translation of the retired draft (none left behind)",
      not any(r["topic_id"] == "IDML-0001" for r in a04["db"]["content_blocks"])
      and count(b04, "topic_translations") == 1 and count(a04, "topic_translations") == 0
      and count(a04, "block_translations") == 0 and count(a04, "structured_table_translations") == 0
      and count(a04, "figure_translations") == 0, {k: count(a04, k) for k in ("topic_translations", "block_translations")})
check("S04c: publications is UPDATEd in place (same row, new title), not recreated",
      [(r["publication_id"], r["title"]) for r in a04["db"]["publications"]] == [("PUB-MAIN", "Revisione B")]
      and a04["db"]["publications"][0]["created_at"] == b04["db"]["publications"][0]["created_at"], a04["db"]["publications"])
check("S04d: the previous IDML source and its review JSON stay in the project next to the new PDF",
      ["Sources/Manuale_A.idml", "copy of Manuale_A.idml"] in a04["files"]
      and ["Sources/Manuale_B.pdf", "copy of Manuale_B.pdf"] in a04["files"]
      and any(f[0] == "ManualSync_IDML_Review_PUB-MAIN.json" for f in a04["files"]), [f[0] for f in a04["files"]])
check("S04e: the retirement leaves no orphan row of any kind -- the cascade removes even the version history "
      "of the retired topic, blocks, table and figure",
      all(not v for v in a04["orphans"].values()) and count(b04, "figure_versions") > 0
      and not any(r["topic_id"] == "IDML-0001" for r in a04["db"].get("topic_versions", [])),
      {k: v for k, v in a04["orphans"].items() if v})

# ============================================================ S05/S06: DOCX over DOCX, IDML over IDML
p05 = new_project("S05 DOCX twice")
outcome(app.import_manual_ui, str(p05), str(DOCX_A), "it-IT", "PUB-MAIN", "Prima")
r05, b05, a05 = run("S05_docx_reimport_title_and_language", p05, DOCX_B, "en-US", "PUB-MAIN", "")
check("S05: DOCX over DOCX: the old table is replaced; title falls back to the file stem, language is updated",
      r05[0] == "ok" and ids(b05, "structured_tables", "table_id") == ["DOC-0001-T001"]
      and [json.loads(r["rows_json"]) for r in a05["db"]["structured_tables"]]
      == [[["Grandezza", "Limite"], ["Pressione", "16 bar"], ["Temperatura", "60 C"]]]
      and [(r["title"], r["source_lang"]) for r in a05["db"]["publications"]] == [("Manuale_B", "en-US")],
      a05["db"]["publications"])

p06 = new_project("S06 IDML twice")
outcome(app.import_manual_ui, str(p06), str(IDML_A), "it-IT", "PUB-MAIN", "Prima")
r06, b06, a06 = run("S06_idml_reimport_over_idml", p06, IDML_B, "it-IT", "PUB-MAIN", "Seconda")
review06_before = [f[1] for f in b06["files"] if f[0] == "ManualSync_IDML_Review_PUB-MAIN.json"]
review06 = [f[1] for f in a06["files"] if f[0] == "ManualSync_IDML_Review_PUB-MAIN.json"]
check("S06: IDML over IDML: retired after the new parse; the review JSON is REPLACED by the new file's items "
      "(the old orphan item is gone, the caption candidate is a new item)",
      r06[0] == "ok" and [[it.get("reason") for it in r] for r in review06] == [["caption_candidate"]]
      and review06[0][0]["item_key"] not in {it["item_key"] for it in review06_before[0]}
      and [json.loads(r["rows_json"]) for r in a06["db"]["structured_tables"]]
      == [[["Grandezza", "Limite"], ["Portata", "40 l/min"]]], [review06_before, review06])

# ============================================================ S07: refusal once content is no longer a migration draft
for tag, label, status in (("S07a", "S07_refused_approved", "Approved"), ("S07b", "S07b_refused_draft", "Draft")):
    p07 = new_project(label)
    outcome(app.import_manual_ui, str(p07), str(PDF_A), "it-IT", "PUB-MAIN", "Originale")
    con = sqlite3.connect(p07 / "ManualSync_CCMS.sqlite")
    con.execute("UPDATE topics SET status=? WHERE topic_id='DOC-0002'", (status,))   # simulates reviewed content
    con.commit()
    con.close()
    r07, b07, a07 = run(label, p07, PDF_B, "it-IT", "PUB-MAIN", "Tentativo")
    check(f"{tag}: a topic in status {status!r} refuses the re-import with ValueError and the exact message",
          r07 == ("raise", "ValueError", "La pubblicazione contiene contenuti già revisionati/approvati. "
                                         "Usa un nuovo Publication ID per una nuova migrazione."), r07)
    check(f"{tag}b: the database is untouched by the refusal", a07["db"] == b07["db"], "")
    check(f"{tag}c: ... but the refused PDF was already copied into Sources/ (copy happens first)",
          ["Sources/Manuale_B.pdf", "copy of Manuale_B.pdf"] in a07["files"]
          and ["Sources/Manuale_B.pdf", "copy of Manuale_B.pdf"] not in b07["files"], [f[0] for f in a07["files"]])
p07i = new_project("S07c IDML refused")
outcome(app.import_manual_ui, str(p07i), str(IDML_A), "it-IT", "PUB-MAIN", "Originale")
con = sqlite3.connect(p07i / "ManualSync_CCMS.sqlite")
con.execute("UPDATE topics SET status='Approved'")
con.commit()
con.close()
r07i, b07i, a07i = run("S07c_refused_idml", p07i, IDML_B, "it-IT", "PUB-MAIN", "Tentativo")
check("S07c: an IDML re-import is refused the same way, and nothing is copied (the IDML copy comes after the import)",
      r07i[1] == "ValueError" and a07i == b07i, r07i)

# ============================================================ S08-S11: failures during the re-import
p08 = new_project("S08 corrupt PDF")
outcome(app.import_manual_ui, str(p08), str(PDF_A), "it-IT", "PUB-MAIN", "Originale")
r08, b08, a08 = run("S08_corrupt_pdf_after_retire", p08, PDF_CORRUPT, "it-IT", "PUB-MAIN", "Rotto")
check("S08: a corrupt PDF raises the raw PyMuPDF error (not a gr.Error)", r08[0] == "raise" and r08[1] == "FileDataError", r08)
check("S08b (pinned quirk): the old draft was retired BEFORE the parse -- the publication is left with no topic",
      count(b08, "topics") == 2 and count(a08, "topics") == 0 and count(a08, "publication_topics") == 0
      and count(a08, "publications") == 1, {t: count(a08, t) for t in ("publications", "topics", "content_blocks")})
r08b, b08b, a08b = run("S08c_good_pdf_after_data_loss", p08, PDF_B, "it-IT", "PUB-MAIN", "Ripristino")
check("S08c: a publication with no linked topic accepts the next import (nothing to refuse)",
      r08b[0] == "ok" and count(a08b, "topics") == 3, message(r08b))

p09 = new_project("S09 empty PDF")
outcome(app.import_manual_ui, str(p09), str(PDF_A), "it-IT", "PUB-MAIN", "Originale")
r09, b09, a09 = run("S09_empty_pdf_after_retire", p09, PDF_EMPTY, "it-IT", "PUB-MAIN", "Vuoto")
check("S09: an empty PDF raises ValueError 'Nessun topic ricostruito dal PDF.' after the old draft was retired",
      r09 == ("raise", "ValueError", "Nessun topic ricostruito dal PDF.") and count(a09, "topics") == 0, r09)

p10 = new_project("S10 corrupt DOCX")
outcome(app.import_manual_ui, str(p10), str(DOCX_A), "it-IT", "PUB-MAIN", "Originale")
r10, b10, a10 = run("S10_corrupt_docx_after_retire", p10, DOCX_CORRUPT, "it-IT", "PUB-MAIN", "Rotto")
check("S10: a corrupt DOCX raises gr.Error with the Word message; the old draft is already gone",
      r10 == ("raise", "Error", "Il documento Word non è valido o è danneggiato. Verifica il file e riprova.")
      and count(b10, "topics") == 2 and count(a10, "topics") == 0 and count(a10, "structured_tables") == 0, r10)

p11 = new_project("S11 corrupt IDML")
outcome(app.import_manual_ui, str(p11), str(IDML_A), "it-IT", "PUB-MAIN", "Originale")
r11, b11, a11 = run("S11_corrupt_idml_keeps_draft", p11, IDML_CORRUPT, "it-IT", "PUB-MAIN", "Rotto")
check("S11: a corrupt IDML raises gr.Error with the InDesign message",
      r11[:2] == ("raise", "Error") and r11[2] == app._LEGACY_IDML_FRIENDLY_ERROR, r11)
check("S11b: ... and leaves the previous draft, its files and its review state exactly as they were", a11 == b11, "")

# ============================================================ S12: .doc through the converter seam
_orig_convert = app.convert_legacy_doc_to_docx


@contextlib.contextmanager
def _fake_convert_ok(_src):
    yield DOCX_A


def _fake_convert_fail(_src):
    raise LegacyDocConversionError("simulated converter failure")


try:
    app.convert_legacy_doc_to_docx = _fake_convert_ok
    p12 = new_project("S12 DOC")
    r12, b12, a12 = run("S12_doc_converted", p12, DOC_LEGACY, "it-IT", "PUB-MAIN", "Legacy")
    check("S12: a .doc goes through the converter and the DOCX pipeline; provenance names the .doc, not the temp file",
          r12[0] == "ok" and count(a12, "topics") == 2
          and all("Legacy.doc" in r["source_refs_json"] for r in a12["db"]["topics"])
          and ["Sources/Legacy.doc", "copy of Legacy.doc"] in a12["files"], [r["source_refs_json"] for r in a12["db"]["topics"]])
    app.convert_legacy_doc_to_docx = _fake_convert_fail
    r12b, b12b, a12b = run("S12b_doc_converter_failure", p12, DOC_LEGACY, "it-IT", "PUB-MAIN", "Legacy")
    check("S12b: a converter failure raises gr.Error with the .doc message -- after the old draft was retired",
          r12b == ("raise", "Error", app._LEGACY_DOC_FRIENDLY_ERROR) and count(a12b, "topics") == 0, r12b)
finally:
    app.convert_legacy_doc_to_docx = _orig_convert
check("S12c: the converter seam is restored", app.convert_legacy_doc_to_docx is _orig_convert, "")

# ============================================================ S13/S14: the guard and the defaults
p13 = new_project("S13 guard")
guard = {"no project": outcome(app.import_manual_ui, "", str(PDF_A), "it-IT", "PUB-MAIN", "x"),
         "no file": outcome(app.import_manual_ui, str(p13), None, "it-IT", "PUB-MAIN", "x")}
check("S13: without project or file -> gr.Error 'Apri un progetto e carica il manuale corrente.', nothing created",
      all(v == ("raise", "Error", "Apri un progetto e carica il manuale corrente.") for v in guard.values())
      and not (p13 / "Sources").exists(), guard)
snap("S13_guard", {"outcome": guard, "after": state(p13)})

p14 = new_project("S14 defaults")
r14, b14, a14 = run("S14_defaults", p14, PDF_A, None, None, None)
check("S14: publication id None -> PUB-MAIN, language None -> the project's source_lang, title None -> file stem",
      r14[0] == "ok" and [(r["publication_id"], r["title"], r["source_lang"]) for r in a14["db"]["publications"]]
      == [("PUB-MAIN", "Manuale_A", "it-IT")], a14["db"].get("publications"))
p14b = new_project("S14b padded id")
r14b, b14b, a14b = run("S14b_padded_publication_id", p14b, PDF_A, " en-US ", "  PUB-X  ", "Titolo")
check("S14b: publication id and language are stripped",
      [(r["publication_id"], r["source_lang"]) for r in a14b["db"]["publications"]] == [("PUB-X", "en-US")],
      a14b["db"].get("publications"))

# ============================================================ S15: guided onboarding -> same path
_orig_root = app.DATA_ROOT
try:
    app.DATA_ROOT = TMP / "onboarding_root"
    app.DATA_ROOT.mkdir()
    ob = outcome(app._guided_create_and_import, "Onboarding Test", str(PDF_A), "it-IT")
    ok15 = ob[0] == "ok"
    p15 = Path(ob[1][0]) if ok15 else None                  # `active`: the project folder create_project_ui made
    st15 = state(p15) if ok15 and p15.is_dir() else None
    check("S15a: the onboarding project is created under DATA_ROOT", ok15 and p15.parent == app.DATA_ROOT,
          ob[1][0] if ok15 else ob)
    check("S15: the guided onboarding creates the project and imports through import_manual_ui (same PUB-MAIN result)",
          ok15 and st15 is not None and ids(st15, "topics", "topic_id") == ["DOC-0001", "DOC-0002"]
          and [(r["publication_id"], r["title"]) for r in st15["db"]["publications"]] == [("PUB-MAIN", "Onboarding Test")],
          ob if not ok15 else st15["db"].get("publications"))
    check("S15b: the guided result is the import message followed by the next-step sentence",
          ok15 and ob[1][3].startswith("✅ Manuale importato come draft in **2 topic semantici**")
          and ob[1][3].endswith("Prossimo passo: rivedi il contenuto importato prima di procedere."), ob[1][3] if ok15 else ob)
    snap("S15_guided_onboarding", {"result": ob[1][3] if ok15 else ob, "topics": ob[1][4] if ok15 else None,
                                   "blocks": ob[1][5] if ok15 else None, "after": st15})
finally:
    app.DATA_ROOT = _orig_root
check("S15c: app.DATA_ROOT is restored", app.DATA_ROOT == _orig_root, "")

# ============================================================ S16 (CP5.2b-0): title / language, first import vs re-import
p16 = new_project("S16 asymmetry")
b16a = state(p16)
r16a = outcome(app.import_manual_ui, str(p16), str(PDF_A), "it-it", "PUB-MAIN", "  Titolo  ")
a16a = state(p16)
r16b = outcome(app.import_manual_ui, str(p16), str(PDF_B), "it-it", "PUB-MAIN", "  Titolo  ")
a16b = state(p16)


def _pub(st):
    return [(r["publication_id"], r["title"], r["source_lang"]) for r in st["db"].get("publications", [])]


check("S16: first import (create_publication): the title is stripped and 'it-it' is canonicalized to 'it-IT'",
      r16a[0] == "ok" and _pub(a16a) == [("PUB-MAIN", "Titolo", "it-IT")], _pub(a16a))
check("S16b (pinned asymmetry): re-import (inline UPDATE): title and language are stored as received",
      r16b[0] == "ok" and _pub(a16b) == [("PUB-MAIN", "  Titolo  ", "it-it")], _pub(a16b))
check("S16c: the topics are canonicalized on both paths (only the publication row differs)",
      {r["source_lang"] for r in a16a["db"]["topics"]} == {"it-IT"} == {r["source_lang"] for r in a16b["db"]["topics"]},
      [{r["source_lang"] for r in a16a["db"]["topics"]}, {r["source_lang"] for r in a16b["db"]["topics"]}])
snap("S16_publication_title_language_asymmetry",
     {"before": b16a, "first": {"outcome": r16a, "after": a16a}, "reimport": {"outcome": r16b, "after": a16b}})

# ============================================================ M: the digests see what matters, and only that
base04 = {"before": b04, "outcome": r04, "after": a04}
ref = digest(base04)


def mutated(fn) -> str:
    m = copy.deepcopy(base04)
    fn(m)
    return digest(m)


check("M1: a different status on one topic changes the digest",
      mutated(lambda m: m["after"]["db"]["topics"][0].update(status="Approved")) != ref, "")
check("M2: one row more or less (e.g. a block the retirement forgot) changes the digest",
      mutated(lambda m: m["after"]["db"]["content_blocks"].pop()) != ref, "")
check("M3: a different relation (publication_topics position) changes the digest",
      mutated(lambda m: m["after"]["db"]["publication_topics"][0].update(position=99)) != ref, "")
check("M4: a different text changes the digest (content is never normalized)",
      mutated(lambda m: m["after"]["db"]["content_blocks"][0].update(text_content="altro")) != ref, "")
check("M5: a leftover file or a changed review item changes the digest",
      mutated(lambda m: m["after"]["files"].append(["Sources/x.pdf", "sha256:0"])) != ref, "")
check("M6: only the timestamps are normalized -- changing created_at alone leaves the digest unchanged",
      mutated(lambda m: m["after"]["db"]["topics"][0].update(created_at="2000-01-01T00:00:00+00:00")) == ref, "")
base03 = {"before": b03, "outcome": r03, "after": a03}
ref03 = digest(base03)


def _replace_everywhere(o, old: str, new: str):
    """The same observation with one random key swapped for another in EVERY place it appears."""
    if hasattr(o, "to_dict") and hasattr(o, "columns"):
        return o.map(lambda v: v.replace(old, new) if isinstance(v, str) else v)
    if isinstance(o, dict):
        return {k: _replace_everywhere(v, old, new) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return type(o)(_replace_everywhere(v, old, new) for v in o)
    return o.replace(old, new) if isinstance(o, str) else o


_key03 = [it["item_key"] for f in a03["files"] if f[0] == "ManualSync_IDML_Review_PUB-MAIN.json" for it in f[1]
          if it.get("reason") == "caption_candidate"][0]
m7a = _replace_everywhere(base03, _key03, "0123456789ab")
m7b = copy.deepcopy(base03)
[it for f in m7b["after"]["files"] if f[0] == "ManualSync_IDML_Review_PUB-MAIN.json" for it in f[1]
 if it.get("reason") == "caption_candidate"][0]["item_key"] = "0123456789ab"   # the review item alone
check("M7: another random caption key, used consistently, leaves the digest unchanged (it is run-dependent)",
      digest(m7a) == ref03, "")
check("M7b: ... but a review item that no longer points at its figure changes the digest (the link is pinned)",
      digest(m7b) != ref03, "")

base16 = {"before": b16a, "first": {"outcome": r16a, "after": a16a}, "reimport": {"outcome": r16b, "after": a16b}}
m8 = copy.deepcopy(base16)
m8["reimport"]["after"]["db"]["publications"][0].update(title="Titolo", source_lang="it-IT")
check("M8: a re-import that stripped the title and canonicalized the language like the first import "
      "(the two paths 'unified') changes the S16 digest", digest(m8) != digest(base16), "")

# ============================================================ R (CP5.2b-1): the SQL lives in IndustrialRepository
import ast  # noqa: E402

_SQL_CALLS = {"execute", "executemany", "executescript", "read_sql_query", "read_sql", "read_sql_table"}
# The 11 statements of app.import_manual_ui at f5d9f48 (before the move), as source text, in order.
ORIGINAL_SQL = [
    ("execute", '"SELECT 1 FROM publications WHERE publication_id=?"'),
    ("execute", '"SELECT t.topic_id,t.status FROM topics t JOIN publication_topics pt ON pt.topic_id=t.topic_id WHERE pt.publication_id=?"'),
    ("execute", 'f"SELECT object_ref_id FROM content_blocks WHERE topic_id IN ({marks}) AND block_type=\'table\' AND object_ref_id<>\'\'"'),
    ("execute", 'f"SELECT object_ref_id FROM content_blocks WHERE topic_id IN ({marks}) AND block_type=\'figure\' AND object_ref_id<>\'\'"'),
    ("execute", '"DELETE FROM publication_topics WHERE publication_id=?"'),
    ("executemany", '"DELETE FROM topics WHERE topic_id=?"'),
    ("executemany", '"DELETE FROM structured_tables WHERE table_id=?"'),
    ("executemany", '"DELETE FROM figures WHERE figure_id=?"'),
    ("execute", '"SELECT 1 FROM publications WHERE publication_id=?"'),
    ("execute", '"UPDATE publications SET title=?,source_lang=? WHERE publication_id=?"'),
    ("execute", '"SELECT COUNT(*) FROM structured_tables"'),
]
REPO_METHODS = ["migration_draft_objects", "retire_migration_draft", "publication_exists",
                "update_publication_header", "structured_table_count"]          # in the order of ORIGINAL_SQL
APP_SRC = (BASE / "app.py").read_text(encoding="utf-8")
CCMS_SRC = (BASE / "ccms_industrial.py").read_text(encoding="utf-8")


def _function(src: str, name: str, cls: str | None = None) -> ast.FunctionDef:
    body = ast.parse(src).body
    if cls:
        body = [n for n in body if isinstance(n, ast.ClassDef) and n.name == cls][0].body
    return [n for n in body if isinstance(n, ast.FunctionDef) and n.name == name][0]


def sql_calls(fn: ast.FunctionDef, src: str) -> list[tuple[str, str]]:
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in _SQL_CALLS and n.args]
    return [(n.func.attr, ast.get_source_segment(src, n.args[0])) for n in sorted(calls, key=lambda n: (n.lineno, n.col_offset))]


def commits(fn: ast.FunctionDef) -> int:
    return sum(1 for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "commit")


IMU = _function(APP_SRC, "import_manual_ui")
check("R1: import_manual_ui issues no SQL itself any more (no execute*/read_sql* call in its body)",
      sql_calls(IMU, APP_SRC) == [] and commits(IMU) == 0, [sql_calls(IMU, APP_SRC), commits(IMU)])
_repo_calls = {n.func.attr for n in ast.walk(IMU) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "repo"}
check("R2: the five IndustrialRepository methods exist and import_manual_ui calls each of them",
      all(callable(getattr(IndustrialRepository, m, None)) for m in REPO_METHODS) and set(REPO_METHODS) <= _repo_calls,
      sorted(_repo_calls))
_imu_src = ast.get_source_segment(APP_SRC, IMU)
_retire_calls = [m.start() for m in re.finditer(r"(?<!def )_retire_old_draft\(\)", _imu_src)]   # calls, not the def
check("R2b: the draft is still retired at the same two points -- before the PDF/DOCX/DOC parse, after the IDML import",
      len(_retire_calls) == 2
      and _retire_calls[0] < _imu_src.index("if suffix in ('.docx','.doc'):") < _imu_src.index("imported=import_idml_as_topics(")
      < _retire_calls[1] < _imu_src.index("shutil.copy2(pdf,copied)\n            # Always replace the review state"),
      _retire_calls)
_moved = [c for m in REPO_METHODS for c in sql_calls(_function(CCMS_SRC, m, "IndustrialRepository"), CCMS_SRC)]
check("R3: the five methods hold exactly the 11 original statements, same text, same call kind, same order",
      _moved == ORIGINAL_SQL, _moved)
_commits = {m: commits(_function(CCMS_SRC, m, "IndustrialRepository")) for m in REPO_METHODS}
check("R3b: the two commits are where they were -- one after the retirement, one after the publication UPDATE",
      _commits == {"migration_draft_objects": 0, "retire_migration_draft": 1, "publication_exists": 0,
                   "update_publication_header": 1, "structured_table_count": 0}, _commits)
check("R3c: the refusal is still the same ValueError text",
      "raise ValueError(\"La pubblicazione contiene contenuti già revisionati/approvati. Usa un nuovo Publication ID "
      "per una nuova migrazione.\")" in ast.get_source_segment(CCMS_SRC, _function(CCMS_SRC, "migration_draft_objects",
                                                                                  "IndustrialRepository")), "")
_anchor = "    repo=IndustrialRepository(_ccms_db(project_dir))\n"
_mut = _imu_src.replace(_anchor, _anchor + "    repo.conn.execute(\"DELETE FROM topics\")\n") if _imu_src.count(_anchor) == 1 else ""
check("R4: a SQL call put back into import_manual_ui is caught by R1",
      bool(_mut) and sql_calls(ast.parse(_mut).body[0], _mut) == [("execute", '"DELETE FROM topics"')], bool(_mut))

check("N1: no network access attempted", not GUARD.snapshot(), GUARD.snapshot())

# Pinned at HEAD d4f7c45 (CP5.2a) after three identical runs; S16 added at 02652e5 (CP5.2b-0), same procedure.
GOLDEN = {
    'S01_pdf_new': '0a6cefe0be16b1b58a52e36d528b3ccb3969a562eff9cec2284a289de98a972b',
    'S02_docx_new': '1d19b535de0b302151cf915b985875d1f6768e2937bc0b83a4f68bc876d9b54a',
    'S03_idml_new': 'f652f258996ab1ad9b681351d35d8ae9f729125344a83d271105e8ab8c214a4f',
    'S04_pdf_reimport_over_idml_draft': '101921c358caa04c64d1e61a8cf8ad1107f38ee0686b97e1139f1fca9ff01668',
    'S05_docx_reimport_title_and_language': 'b01b967d636ed275d5a2b44834563b60751c7f474d0e75d160543863460af7f2',
    'S06_idml_reimport_over_idml': 'aadad84354c2eeb341a63284149bbee7d371094a70914329fd65a692091cccc2',
    'S07_refused_approved': 'b356cbcd36814454a78d6c07aecc87ca546c08e8565a3cff02d6680d8076dcf1',
    'S07b_refused_draft': 'ed4c2b306a32e4ac52f0c804eb6001f8138a73fd59c56eac65adf61152342e6f',
    'S07c_refused_idml': 'f05073cf56ce1cfc02b7668b86e78d1aa8c087a9ae81739aaa47a0b44e44f6b7',
    'S08_corrupt_pdf_after_retire': '4325345e1b0c174c4331d2b6f1039c2bad095fb8cf315259b66d4eaf9e4f87e5',
    'S08c_good_pdf_after_data_loss': '8e885fc43dc4e212e7cb6445484b81452975cb49913438f77fdb20f03a34b864',
    'S09_empty_pdf_after_retire': 'c8e396683cfd653cf1c7b417cfb0c40c5d68925da4a443c2476324c2968a8485',
    'S10_corrupt_docx_after_retire': 'f485916d53cec81132a7246c547ed5eef0a25b4ae3b24304def3030ae02dc3e3',
    'S11_corrupt_idml_keeps_draft': 'ed5d15d735ac980e4da25b51fdd71802632fd4d2642438abd18fb0d9340a74e3',
    'S12_doc_converted': 'c978c52ea464560005ce950f215e4122fa283255a7f6ed66bebe8ed881ee4ae6',
    'S12b_doc_converter_failure': '3a67d63dbcf5db30f1998bcbf8454fb85f9e18b504168adc348fed23909ec306',
    'S13_guard': '0c2e51718b0b94c63563ee6250ffee880a2e972af4a1dd13a244052be5e24e49',
    'S14_defaults': '841893688e8f78f75cad7a9c67e8d8974d769e2809c0b18e6ba100ef3954aea6',
    'S14b_padded_publication_id': '54a14ba292b3ce8fe48f3193adf14e5e5c6ad7f8ca23440bb33168c8d46dc1b4',
    'S15_guided_onboarding': 'e0a6f69c73a601c59dc42ff64f72abe91e44fb33db329877b3cb50aafb114f0c',
    'S16_publication_title_language_asymmetry': '0d33da9ba132522f1aaf3ce96af62967b8c4a237e78899fd605b1689166c04ba',
}

if PRINT_GOLDEN:
    print("GOLDEN = {")
    for k in sorted(_observed):
        print(f"    {k!r}: {_observed[k]!r},")
    print("}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

for label in sorted(_observed):
    check(f"SNAP {label}: identical to the CP5.2a baseline", GOLDEN.get(label) == _observed[label],
          f"expected {GOLDEN.get(label)} got {_observed[label]}")
check("SNAP: every pinned output was observed", set(GOLDEN) == set(_observed), sorted(set(GOLDEN) ^ set(_observed)))

shutil.rmtree(TMP, ignore_errors=True)
check("T1: the temporary fixtures were removed", not TMP.exists(), TMP)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_IMPORT_ORCHESTRATION.json").write_text(json.dumps({
    "suite": "import orchestration (CP5.2a)", "tests": total, "pass": passed, "fail": total - passed, "digests": _observed,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
