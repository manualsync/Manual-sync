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
            commits (frozen copy below); a SQL call put back into import_manual_ui is caught;
* S04x      (HARDENING-02A) S04 without the translations: the retirement with no human work at all;
* S17a-k    (HARDENING-02A, B5 characterization) one kind of human work on a migration draft, then a
            re-import: a Studio block / title edit (PDF, DOCX), a block created in Studio, an open review
            (PDF vs IDML), an archived topic, an autosave with nothing changed, IDML review decisions
            (database + JSON, JSON only), the guided onboarding with the same / an empty name (DLS-02),
            a same-name file refused after approval (DLS-03), an audit row older than the draft (as the
            re-import leaves it, and seeded directly), an orphan autosave, compliance evidence. Pinned as
            they behave TODAY, destructive included:
            what is deleted, what survives, what comes back under the same id or under a new one.

For each scenario the WHOLE content database (every table, every row, in rowid
order), the project files (Sources/, review JSON, manifest) and the returned
message/DataFrames are captured before and after. Only UTC timestamps, the
temporary folder and what is drawn from uuid4 (the IDML caption-candidate key, the
TOPIC-/BLK-/TBL- ids of what a user creates in Studio -- each replaced consistently,
so the links stay pinned) are normalized; the ids of imported content, statuses,
texts, relations and counts are not. Golden digests were pinned after three
identical runs; section M proves they are sensitive. Nothing is written outside a
temporary folder.

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
# HARDENING-02A: what a user creates in Studio gets its id from uuid4 too (authoring_service.py: TOPIC-/BLK-/TBL- +
# uuid4().hex[:10].upper()). Same consistent replacement, so an audit row that names a deleted block stays pinned to it.
# The ids of imported content (DOC-0001-B001, IDML-0001-2-F002, ...) never match and are never normalized.
_STUDIO_ID = re.compile(r"\b(TOPIC|BLK|TBL)-([0-9A-F]{10})\b")


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
    uids: dict[str, str] = {}
    text = _STUDIO_ID.sub(lambda m: m.group(1) + "-" + uids.setdefault(m.group(2), f"<UID{len(uids) + 1}>"), text)
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

# ============================================================ S04x / S17 (HARDENING-02A): human work, then a re-import
# B5 characterization: the behaviour TODAY, destructive included -- none of it is a desired behaviour. Each scenario
# imports a migration draft, does ONE kind of human work on it through the real entry point (AuthoringService, the
# review queue, the Studio autosave callback, the IDML review callback, ComplianceService, the guided onboarding) and
# re-imports. The guard (migration_draft_objects) reads only the TOPIC status and a Studio save keeps 'Migrated' (only
# Approved / In Review become Draft), so most of this work does not stop the re-import. Pinned: what the retirement
# deletes, what survives it (audit_events, review_tasks, authoring_drafts, compliance_evidence have no foreign key to
# the content), and what is re-created under the SAME id (PDF/DOCX: DOC-0001-B001 ...) or under a NEW one (IDML is
# imported before the retirement: IDML-0001-2 ...).
from authoring_service import AuthoringService  # noqa: E402
from compliance_intelligence import ComplianceService  # noqa: E402

ACTOR = "Redattore"
SRC_B001 = "Il presente manuale descrive il sistema idraulico."     # DOC-0001-B001 as imported from PDF_A
EDITED = "Testo corretto dal redattore."
SAVED = "Bozza salvata in locale (nessuna nuova versione creata)."   # studio_save_draft_ui on success


@contextlib.contextmanager
def repository(project: Path):
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        yield repo
    finally:
        repo.close()


def draft_project(name: str, source: Path, title: str = "Bozza") -> tuple[Path, dict]:
    """A new project holding one freshly imported migration draft, and its state."""
    project = new_project(name)
    outcome(app.import_manual_ui, str(project), str(source), "it-IT", "PUB-MAIN", title)
    return project, state(project)


def rerun(label: str, project: Path, draft: dict, action, *args, then=None):
    """run(), pinning also the draft as imported, the human work done on it and (`then`) a reading taken afterwards."""
    before = state(project)
    res = outcome(app.import_manual_ui, str(project), *[str(a) if isinstance(a, Path) else a for a in args])
    after = state(project)
    later = then() if then else None
    snap(label, {"draft": draft, "action": action, "before": before, "outcome": res, "after": after, "then": later})
    return res, before, after, later


def rows(st: dict, table: str, **match) -> list:
    return [r for r in st["db"].get(table, []) if all(r.get(k) == v for k, v in match.items())]


def versions(st: dict, table: str, col: str, object_id: str) -> list:
    return [r["version"] for r in rows(st, table, **{col: object_id})]


def actions(st: dict) -> list:
    return [(r["object_type"], r["object_id"], r["action"]) for r in st["db"].get("audit_events", [])]


def b001(st: dict) -> list:
    return rows(st, "content_blocks", block_id="DOC-0001-B001")


def review_items(st: dict) -> list:
    return [f[1] for f in st["files"] if f[0] == "ManualSync_IDML_Review_PUB-MAIN.json"][0]


def decisions(st: dict) -> list:
    return [(it["reason"], it["decision"], it["decided_by"]) for it in review_items(st)]


_LIVE = {"topic": ("topics", "topic_id"), "block": ("content_blocks", "block_id"),
         "table": ("structured_tables", "table_id"), "figure": ("figures", "figure_id")}


def dangling(st: dict) -> dict:
    """Human-work rows whose topic/block/table/figure no longer exists (they have no foreign key to it)."""
    live = {kind: set(ids(st, table, col)) for kind, (table, col) in _LIVE.items()}
    return {table: [(r[kcol], r["object_id"]) for r in st["db"].get(table, [])
                    if r[kcol] in live and r["object_id"] not in live[r[kcol]]]
            for table, kcol in (("audit_events", "object_type"), ("review_tasks", "object_type"),
                                ("authoring_drafts", "kind"), ("compliance_evidence", "evidence_kind"))}


NO_DANGLING = {"audit_events": [], "review_tasks": [], "authoring_drafts": [], "compliance_evidence": []}

# ------------------------------------------------------------ S04x: S04 without the translations (no human work)
p04x, d04x = draft_project("S04x Reimport no translations", IDML_A, "Bozza IDML")
r04x, b04x, a04x, _ = rerun("S04x_pdf_reimport_over_idml_draft_no_translations", p04x, d04x, None,
                            PDF_B, "it-IT", "PUB-MAIN", "Revisione B")
_TRANSLATIONS = ("topic_translations", "block_translations", "structured_table_translations", "figure_translations")
check("S04x: an IDML draft with NO translation (no human work at all) is retired by a PDF re-import exactly as in S04 -- "
      "topic, table, figure DELETEd, blocks and version history cascaded, no orphan row",
      r04x[0] == "ok" and ids(b04x, "topics", "topic_id") == ["IDML-0001"]
      and ids(a04x, "topics", "topic_id") == ["DOC-0001", "DOC-0002", "DOC-0003"]
      and count(a04x, "structured_tables") == 0 and count(a04x, "figures") == 0 and count(b04x, "figure_versions") > 0
      and not any(r["topic_id"] == "IDML-0001" for r in a04x["db"]["content_blocks"])
      and all(not v for v in a04x["orphans"].values()), a04x["orphans"])
check("S04x-b: no translation row before or after -- the case a B5 guard must keep accepting",
      all(count(st, t) == 0 for st in (b04x, a04x) for t in _TRANSLATIONS), "")
_MANIFEST = "ManualSync_Project.json"                                 # holds the project name, which differs
check("S04x-c: after the re-import nothing tells S04 from S04x -- same database, orphans, files (bar the project "
      "manifest) and result: S04's four translations leave no trace at all",
      count(b04, "topic_translations") == 1 and normalized(a04x["db"]) == normalized(a04["db"])
      and normalized(a04x["orphans"]) == normalized(a04["orphans"]) and normalized(r04x) == normalized(r04)
      and normalized([f for f in a04x["files"] if f[0] != _MANIFEST]) == normalized([f for f in a04["files"] if f[0] != _MANIFEST]),
      "")

# ------------------------------------------------------------ S17a: a Studio block edit (PDF, DOCX)
p17a, d17a = draft_project("S17a block edit", PDF_A)
with repository(p17a) as repo:
    act17a = outcome(AuthoringService(repo).save_block, "DOC-0001-B001", text=EDITED, expected_version=1, actor=ACTOR)
r17a, b17a, a17a, _ = rerun("S17a_block_edit_then_pdf_reimport", p17a, d17a, act17a, PDF_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17a: a Studio save is human work -- DOC-0001-B001 goes to v2 with the new text (block_versions [1, 2]) and an "
      "AUTHORING_SAVE audit row, yet block and topics stay 'Migrated'",
      act17a[0] == "ok" and [(r["version"], r["text_content"], r["status"]) for r in b001(b17a)] == [(2, EDITED, "Migrated")]
      and versions(b17a, "block_versions", "block_id", "DOC-0001-B001") == [1, 2]
      and actions(b17a) == [("block", "DOC-0001-B001", "AUTHORING_SAVE")]
      and {r["status"] for r in b17a["db"]["topics"]} == {"Migrated"}, act17a)
check("S17a-b: ... and the PDF re-import is ACCEPTED: the edited block is deleted and re-created under the SAME id, at "
      "v1, with the source text; v2 and its version row are gone",
      r17a[0] == "ok" and [(r["version"], r["text_content"]) for r in b001(a17a)] == [(1, SRC_B001)]
      and versions(a17a, "block_versions", "block_id", "DOC-0001-B001") == [1]
      and ids(a17a, "content_blocks", "block_id") == ids(d17a, "content_blocks", "block_id"), [b001(a17a), message(r17a)])
check("S17a-c: the AUTHORING_SAVE row survives and, the id being reused, now names the re-created block (re-bound by id, "
      "not dangling)", actions(a17a) == actions(b17a) and dangling(a17a) == NO_DANGLING, dangling(a17a))

p17a2, d17a2 = draft_project("S17a2 block edit DOCX", DOCX_A)
_para17a2 = [r["block_id"] for r in d17a2["db"]["content_blocks"] if r["block_type"] == "paragraph"][0]
with repository(p17a2) as repo:
    act17a2 = outcome(AuthoringService(repo).save_block, _para17a2, text=EDITED, expected_version=1, actor=ACTOR)
r17a2, b17a2, a17a2, _ = rerun("S17a2_block_edit_then_docx_reimport", p17a2, d17a2, act17a2,
                               DOCX_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17a2: DOCX behaves like PDF -- retired before a deterministic parse, the edited paragraph comes back under the "
      "same id at v1 with the source text; every id is reused",
      act17a2[0] == "ok" and r17a2[0] == "ok" and versions(b17a2, "block_versions", "block_id", _para17a2) == [1, 2]
      and [(r["version"], r["text_content"]) for r in rows(a17a2, "content_blocks", block_id=_para17a2)]
      == [(1, rows(d17a2, "content_blocks", block_id=_para17a2)[0]["text_content"])]
      and ids(a17a2, "content_blocks", "block_id") == ids(d17a2, "content_blocks", "block_id")
      and ids(a17a2, "structured_tables", "table_id") == ids(d17a2, "structured_tables", "table_id"),
      [_para17a2, rows(a17a2, "content_blocks", block_id=_para17a2)])

# ------------------------------------------------------------ S17b: a Studio title edit
p17b, d17b = draft_project("S17b title edit", PDF_A)
with repository(p17b) as repo:
    act17b = outcome(AuthoringService(repo).save_topic, "DOC-0001", title="Introduzione e avvertenze",
                     expected_version=1, actor=ACTOR)
r17b, b17b, a17b, _ = rerun("S17b_title_edit_then_pdf_reimport", p17b, d17b, act17b, PDF_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17b: a title edit takes topic DOC-0001 to v2 (topic_versions [1, 2]) and leaves it 'Migrated' -- the very "
      "status the guard reads",
      act17b[0] == "ok" and [(r["version"], r["title"], r["status"]) for r in rows(b17b, "topics", topic_id="DOC-0001")]
      == [(2, "Introduzione e avvertenze", "Migrated")]
      and versions(b17b, "topic_versions", "topic_id", "DOC-0001") == [1, 2], act17b)
check("S17b-b: the re-import is ACCEPTED and restores the source title at v1; the AUTHORING_SAVE row survives",
      r17b[0] == "ok" and [(r["version"], r["title"]) for r in rows(a17b, "topics", topic_id="DOC-0001")]
      == [(1, rows(d17b, "topics", topic_id="DOC-0001")[0]["title"])]
      and versions(a17b, "topic_versions", "topic_id", "DOC-0001") == [1]
      and actions(a17b) == [("topic", "DOC-0001", "AUTHORING_SAVE")], rows(a17b, "topics", topic_id="DOC-0001"))

# ------------------------------------------------------------ S17c: a block created in Studio
p17c, d17c = draft_project("S17c Studio block", PDF_A)
with repository(p17c) as repo:
    act17c = outcome(AuthoringService(repo).create_block_in_topic, "DOC-0001", "paragraph", actor=ACTOR,
                     text="Avvertenza aggiunta dal redattore.")
r17c, b17c, a17c, _ = rerun("S17c_studio_block_then_pdf_reimport", p17c, d17c, act17c, PDF_A, "it-IT", "PUB-MAIN", "Bozza")
_new17c = act17c[1]["block_id"] if act17c[0] == "ok" else ""
check("S17c: a block created in Studio gets a random id (BLK-<10 hex>) and status 'Draft' in a topic still 'Migrated'",
      bool(re.fullmatch(r"BLK-[0-9A-F]{10}", _new17c))
      and [(r["topic_id"], r["status"]) for r in rows(b17c, "content_blocks", block_id=_new17c)] == [("DOC-0001", "Draft")]
      and [r["status"] for r in rows(b17c, "topics", topic_id="DOC-0001")] == ["Migrated"]
      and [a[2] for a in actions(b17c)] == ["AUTHORING_CREATE", "ADD_BLOCK"], [act17c, actions(b17c)])
check("S17c-b: the re-import is ACCEPTED (block statuses are not read) and the new block is gone for good: the "
      "re-created topic holds only the source blocks",
      r17c[0] == "ok" and not rows(a17c, "content_blocks", block_id=_new17c)
      and ids(a17c, "content_blocks", "block_id") == ids(d17c, "content_blocks", "block_id"), message(r17c))
check("S17c-c: its AUTHORING_CREATE row is left DANGLING (a random id is never re-created); the topic's ADD_BLOCK row "
      "survives as well", actions(a17c) == actions(b17c)
      and dangling(a17c) == {**NO_DANGLING, "audit_events": [("block", _new17c)]}, dangling(a17c))

# ------------------------------------------------------------ S17d: an open review on a block -- PDF vs IDML
p17d, d17d = draft_project("S17d review PDF", PDF_A)
with repository(p17d) as repo:
    act17d = outcome(repo.request_review, "block", "DOC-0001-B001", requested_by=ACTOR, assigned_to="Revisore")
r17d, b17d, a17d, _ = rerun("S17d_review_block_then_pdf_reimport", p17d, d17d, act17d, PDF_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17d: a review request puts the BLOCK 'In Review' with an OPEN task, its topic stays 'Migrated' -- so the PDF "
      "re-import is ACCEPTED",
      act17d == ("ok", 1) and [r["status"] for r in b001(b17d)] == ["In Review"]
      and [(r["object_id"], r["status"]) for r in b17d["db"]["review_tasks"]] == [("DOC-0001-B001", "OPEN")]
      and [r["status"] for r in b17d["db"]["topics"]] == ["Migrated", "Migrated"] and r17d[0] == "ok",
      [act17d, message(r17d)])
check("S17d-b: PDF: the block is re-created under the same id as 'Migrated'; the OPEN task survives and is re-bound by "
      "id to the re-created block",
      [r["status"] for r in b001(a17d)] == ["Migrated"] and a17d["db"]["review_tasks"] == b17d["db"]["review_tasks"]
      and dangling(a17d) == NO_DANGLING, [b001(a17d), dangling(a17d)])

p17d2, d17d2 = draft_project("S17d2 review IDML", IDML_A)
with repository(p17d2) as repo:
    act17d2 = outcome(repo.request_review, "block", "IDML-0001-B001", requested_by=ACTOR, assigned_to="Revisore")
r17d2, b17d2, a17d2, _ = rerun("S17d2_review_block_then_idml_reimport", p17d2, d17d2, act17d2,
                               IDML_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17d2: IDML: the re-import is ACCEPTED too, but the new draft is imported BEFORE the old one is retired, so it "
      "gets NEW ids (IDML-0001-2...): the OPEN task and its REQUEST_REVIEW row are left DANGLING",
      act17d2 == ("ok", 1) and r17d2[0] == "ok" and ids(a17d2, "topics", "topic_id") == ["IDML-0001-2"]
      and ids(a17d2, "content_blocks", "block_id")[0] == "IDML-0001-2-B001"
      and dangling(a17d2) == {**NO_DANGLING, "audit_events": [("block", "IDML-0001-B001")],
                              "review_tasks": [("block", "IDML-0001-B001")]}, dangling(a17d2))

# ------------------------------------------------------------ S17e: an archived topic
p17e, d17e = draft_project("S17e archived topic", PDF_A)
with repository(p17e) as repo:
    act17e = outcome(AuthoringService(repo).archive_topic, "DOC-0002", actor=ACTOR)
r17e, b17e, a17e, _ = rerun("S17e_archived_topic_then_pdf_reimport", p17e, d17e, act17e, PDF_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17e: archiving sets archived_at; the topic stays 'Migrated' and linked to the publication; the re-import is "
      "ACCEPTED",
      act17e == ("ok", None) and [(bool(r["archived_at"]), r["status"]) for r in rows(b17e, "topics", topic_id="DOC-0002")]
      == [(True, "Migrated")] and "DOC-0002" in [r["topic_id"] for r in b17e["db"]["publication_topics"]]
      and r17e[0] == "ok", [act17e, message(r17e)])
check("S17e-b: the archive is undone -- DOC-0002 is re-created ACTIVE (archived_at NULL) in the publication; the "
      "ARCHIVE audit row survives",
      [r["archived_at"] for r in rows(a17e, "topics", topic_id="DOC-0002")] == [None]
      and [r["topic_id"] for r in a17e["db"]["publication_topics"]] == ["DOC-0001", "DOC-0002"]
      and actions(a17e) == [("topic", "DOC-0002", "ARCHIVE")], rows(a17e, "topics", topic_id="DOC-0002"))

# ------------------------------------------------------------ S17f: an autosave with no human change
p17f, d17f = draft_project("S17f autosave", PDF_A)
act17f = outcome(app.studio_save_draft_ui, str(p17f), "block", "DOC-0001-B001", "paragraph", "", 1,
                 "", SRC_B001, "", "", None, "", "", ACTOR)                       # a blur with nothing changed
r17f, b17f, a17f, _ = rerun("S17f_autosave_no_change_then_pdf_reimport", p17f, d17f, act17f,
                            PDF_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17f: a Studio blur with nothing changed still writes an autosave row (dirty=1, the unchanged text); no version, "
      "no audit row, no status change",
      act17f == ("ok", SAVED)
      and [(r["kind"], r["object_id"], r["base_version"], json.loads(r["payload_json"]), r["dirty"])
           for r in b17f["db"]["authoring_drafts"]] == [("block", "DOC-0001-B001", 1, {"text": SRC_B001}, 1)]
      and b17f["db"]["content_blocks"] == d17f["db"]["content_blocks"] and not b17f["db"]["audit_events"], act17f)
check("S17f-b: the re-import is ACCEPTED; the autosave row survives untouched, keyed on the re-created block",
      r17f[0] == "ok" and a17f["db"]["authoring_drafts"] == b17f["db"]["authoring_drafts"]
      and dangling(a17f) == NO_DANGLING, message(r17f))

# ------------------------------------------------------------ S17g: IDML review decisions (database + JSON, JSON only)
p17g, d17g = draft_project("S17g caption confirmed", IDML_A)
act17g = outcome(app.idml_review_confirm_ui, str(p17g), "QA",
                 *[app._IDML_CONFIRM_CAPTION if it["reason"] == "caption_candidate" else app._IDML_ACCEPT_EXCLUSION
                   for it in review_items(d17g)])
r17g, b17g, a17g, _ = rerun("S17g_idml_caption_confirmed_then_idml_reimport", p17g, d17g, act17g,
                            IDML_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17g: confirming the caption is human work on BOTH sides: figure v2 with the caption (figure_versions [1, 2]) "
      "and explicit decisions in the review JSON (decided_by 'QA')",
      act17g[0] == "ok" and [(r["version"], r["caption"]) for r in b17g["db"]["figures"]] == [(2, "Didascalia pompa idraulica")]
      and [r["version"] for r in b17g["db"]["figure_versions"]] == [1, 2]
      and decisions(b17g) == [("orphan_story", "accepted_exclusion", "QA"), ("caption_candidate", "confirm_association", "QA")],
      [b17g["db"]["figures"], decisions(b17g)])
check("S17g-b: the IDML re-import is ACCEPTED (figure statuses are not read): the confirmed figure is deleted, the new "
      "one (new id) is v1 with no caption, and the JSON is REPLACED by undecided items under a new key",
      r17g[0] == "ok" and [(r["figure_id"], r["version"], r["caption"]) for r in a17g["db"]["figures"]]
      == [("IDML-0001-2-F002", 1, "")] and decisions(a17g) == [("orphan_story", None, ""), ("caption_candidate", None, "")]
      and review_items(a17g)[1]["item_key"] != review_items(b17g)[1]["item_key"], [a17g["db"]["figures"], decisions(a17g)])

p17g2, d17g2 = draft_project("S17g2 JSON-only decisions", IDML_A)
act17g2 = outcome(app.idml_review_confirm_ui, str(p17g2), "QA",
                  *[app._IDML_KEEP_SEPARATE if it["reason"] == "caption_candidate" else app._IDML_ACCEPT_EXCLUSION
                    for it in review_items(d17g2)])
r17g2, b17g2, a17g2, _ = rerun("S17g2_idml_json_only_decisions_then_idml_reimport", p17g2, d17g2, act17g2,
                               IDML_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17g2: 'keep separate' and 'accept exclusion' are explicit decisions that live ONLY in the review JSON -- the "
      "database is untouched (figure still v1, no audit row)",
      act17g2[0] == "ok" and b17g2["db"] == d17g2["db"]
      and decisions(b17g2) == [("orphan_story", "accepted_exclusion", "QA"), ("caption_candidate", "keep_separate", "QA")],
      decisions(b17g2))
check("S17g2-b: what tells an explicit decision from the file the import writes: decision set, decided_by and "
      "decided_at filled -- in the generated file every item is None / '' / ''",
      all(it["decision"] is None and it["decided_by"] == "" and it["decided_at"] == "" for it in review_items(d17g2))
      and all(it["decision"] and it["decided_by"] and it["decided_at"] for it in review_items(b17g2)), review_items(d17g2))
check("S17g2-c: the IDML re-import is ACCEPTED and the decisions are lost: the JSON is replaced by undecided items",
      r17g2[0] == "ok" and decisions(a17g2) == [("orphan_story", None, ""), ("caption_candidate", None, "")], decisions(a17g2))

# ------------------------------------------------------------ S17h: the guided onboarding (DLS-02), same / empty name
_orig_root17 = app.DATA_ROOT
try:
    app.DATA_ROOT = TMP / "onboarding_root_17h"
    app.DATA_ROOT.mkdir()
    g17h1 = outcome(app._guided_create_and_import, "Manuale Pompa", str(PDF_A), "it-IT")
    p17h = Path(g17h1[1][0])
    d17h = state(p17h)
    with repository(p17h) as repo:
        act17h = outcome(AuthoringService(repo).save_block, "DOC-0001-B001", text=EDITED, expected_version=1, actor=ACTOR)
    b17h = state(p17h)
    g17h2 = outcome(app._guided_create_and_import, "Manuale Pompa", str(PDF_B), "it-IT")
    a17h = state(p17h)
    dirs17h = sorted(p.name for p in app.DATA_ROOT.iterdir() if p.is_dir())
    snap("S17h_guided_same_name_reimport",
         {"first": [g17h1[0], g17h1[1][0], g17h1[1][3]], "draft": d17h, "action": act17h, "before": b17h,
          "second": [g17h2[0], g17h2[1][0], g17h2[1][3]] if g17h2[0] == "ok" else g17h2, "after": a17h, "projects": dirs17h})

    app.DATA_ROOT = TMP / "onboarding_root_17h2"
    app.DATA_ROOT.mkdir()
    g17h3 = outcome(app.guided_confirm_language_ui, "", str(PDF_A), "Italiano (it-IT)")
    p17h2 = Path(g17h3[1][0])
    d17h2 = state(p17h2)
    with repository(p17h2) as repo:
        act17h2 = outcome(AuthoringService(repo).save_block, "DOC-0001-B001", text=EDITED, expected_version=1, actor=ACTOR)
    b17h2 = state(p17h2)
    g17h4 = outcome(app.guided_confirm_language_ui, "   ", str(PDF_A), "Italiano (it-IT)")
    a17h2 = state(p17h2)
    dirs17h2 = sorted(p.name for p in app.DATA_ROOT.iterdir() if p.is_dir())
    snap("S17h2_guided_blank_name_reimport",
         {"first": [g17h3[0], g17h3[1][0], g17h3[1][3], g17h3[1][-1]], "draft": d17h2, "action": act17h2, "before": b17h2,
          "second": [g17h4[0], g17h4[1][0], g17h4[1][3], g17h4[1][-1]] if g17h4[0] == "ok" else g17h4,
          "after": a17h2, "projects": dirs17h2})
finally:
    app.DATA_ROOT = _orig_root17
check("S17h: the guided onboarding run twice with the SAME name reopens the existing project (no second folder) and "
      "re-imports into its PUB-MAIN: the Studio edit made in between is lost, the new file replaces the draft",
      g17h1[0] == "ok" and act17h[0] == "ok" and g17h2[0] == "ok" and g17h2[1][0] == g17h1[1][0] and dirs17h == [p17h.name]
      and versions(b17h, "block_versions", "block_id", "DOC-0001-B001") == [1, 2]
      and [(r["version"], r["text_content"]) for r in b001(a17h)] == [(1, "Revisione B del manuale.")]
      and ids(a17h, "topics", "topic_id") == ["DOC-0001", "DOC-0002", "DOC-0003"], [g17h2[:1], dirs17h, b001(a17h)])
check("S17h-b: nothing tells the user an existing project was reused: the second result is the plain import message "
      "(create_project_ui's 'esiste già' message is discarded)",
      g17h2[0] == "ok" and g17h2[1][3].startswith("✅ Manuale importato come draft") and "esiste già" not in g17h2[1][3],
      g17h2[1][3] if g17h2[0] == "ok" else g17h2)
check("S17h2: an EMPTY name (blank, then spaces) falls back to the file stem, so the same file lands in the same "
      "project again and is re-imported over the edit",
      g17h3[0] == "ok" and g17h4[0] == "ok" and g17h3[1][-1] == g17h4[1][-1] == "Manuale_A"
      and g17h4[1][0] == g17h3[1][0] and dirs17h2 == [p17h2.name] and act17h2[0] == "ok"
      and versions(b17h2, "block_versions", "block_id", "DOC-0001-B001") == [1, 2]
      and [(r["version"], r["text_content"]) for r in b001(a17h2)] == [(1, SRC_B001)], [dirs17h2, b001(a17h2)])
check("S17h-c: app.DATA_ROOT is restored", app.DATA_ROOT == _orig_root17, "")

# ------------------------------------------------------------ S17i: a second revision with the SAME file name (DLS-03)
(INPUTS / "rev1").mkdir()
(INPUTS / "rev2").mkdir()
PDF_C1 = make_pdf("rev1/Manuale_C.pdf", [("1 Sicurezza", ["Revisione 1: indossare i guanti."])])
PDF_C2 = make_pdf("rev2/Manuale_C.pdf", [("1 Sicurezza", ["Revisione 2: indossare guanti e occhiali."])])
for _pdf in (PDF_C1, PDF_C2):                    # files() names an uploaded copy by WHICH input it is
    INPUT_SHA[hashlib.sha256(_pdf.read_bytes()).hexdigest()] = _pdf.relative_to(INPUTS).as_posix()
p17i, d17i = draft_project("S17i same-name file", PDF_C1)
with repository(p17i) as repo:
    act17i = outcome(AuthoringService(repo).bulk_approve_publication, "PUB-MAIN", actor="Revisore")
r17i, b17i, a17i, _ = rerun("S17i_same_name_file_refused_overwrites_source", p17i, d17i, act17i,
                            PDF_C2, "it-IT", "PUB-MAIN", "Revisione 2")


def source_copy(st: dict, rel: str) -> list:
    return [f[1] for f in st["files"] if f[0] == rel]


check("S17i: after a human approval the next revision under the SAME file name is refused (ValueError) and the "
      "database is untouched",
      act17i == ("ok", {"topics_approved": 1, "review_tasks_closed": 0}) and r17i[:2] == ("raise", "ValueError")
      and a17i["db"] == b17i["db"], [act17i, r17i])
check("S17i-b (DLS-03): ... but the refused file has already OVERWRITTEN Sources/Manuale_C.pdf: the approved content now "
      "sits next to a copy of the revision that was refused",
      source_copy(b17i, "Sources/Manuale_C.pdf") == ["copy of rev1/Manuale_C.pdf"]
      and source_copy(a17i, "Sources/Manuale_C.pdf") == ["copy of rev2/Manuale_C.pdf"], source_copy(a17i, "Sources/Manuale_C.pdf"))

# ------------------------------------------------------------ S17j: an audit row older than the draft it now names
p17j, d17j = draft_project("S17j audit before draft", PDF_A)
with repository(p17j) as repo:
    act17j = outcome(AuthoringService(repo).save_block, "DOC-0001-B001", text=EDITED, expected_version=1, actor=ACTOR)
outcome(app.import_manual_ui, str(p17j), str(PDF_A), "it-IT", "PUB-MAIN", "Bozza")       # destructive, as in S17a
r17j, b17j, a17j, _ = rerun("S17j_audit_before_draft_then_pdf_reimport", p17j, d17j, act17j,
                            PDF_B, "it-IT", "PUB-MAIN", "Revisione B")
_save_at17j = [r["created_at"] for r in b17j["db"]["audit_events"] if r["action"] == "AUTHORING_SAVE"]
check("S17j: after one destructive re-import the AUTHORING_SAVE row is OLDER than the block it names (audit created_at "
      "< block created_at, block back at v1): read by id alone it looks like work on the CURRENT draft",
      act17j[0] == "ok" and len(_save_at17j) == 1 and _save_at17j[0] < b001(b17j)[0]["created_at"]
      and versions(b17j, "block_versions", "block_id", "DOC-0001-B001") == [1] and dangling(b17j) == NO_DANGLING,
      [_save_at17j, b001(b17j)])
check("S17j-b: today it changes nothing -- the next re-import (another revision) is ACCEPTED and the row survives again",
      r17j[0] == "ok" and actions(a17j) == actions(b17j) == [("block", "DOC-0001-B001", "AUTHORING_SAVE")]
      and [r["text_content"] for r in b001(a17j)] == ["Revisione B del manuale."], [message(r17j), actions(a17j)])

# The same stale row, written directly (the shape S17j leaves behind, as in a project re-imported before B5): it does not
# depend on the destructive re-import S17j needs to produce it, so it still describes that state once that changes.
p17j2, d17j2 = draft_project("S17j2 seeded audit before draft", PDF_A)
con = sqlite3.connect(p17j2 / "ManualSync_CCMS.sqlite")
con.execute("INSERT INTO audit_events(object_type,object_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            ("block", "DOC-0001-B001", "AUTHORING_SAVE", ACTOR,
             json.dumps({"from_version": 1, "to_version": 2}, sort_keys=True), "2000-01-01T00:00:00+00:00"))
con.commit()
con.close()
r17j2, b17j2, a17j2, _ = rerun("S17j2_seeded_audit_before_draft_then_pdf_reimport", p17j2, d17j2,
                               "seeded: AUTHORING_SAVE on DOC-0001-B001 dated 2000-01-01", PDF_B, "it-IT", "PUB-MAIN",
                               "Revisione B")
check("S17j2: a seeded AUTHORING_SAVE row dated before the draft (block still v1, never edited in this draft) does "
      "not stop the re-import: ACCEPTED, the row survives",
      r17j2[0] == "ok" and actions(a17j2) == actions(b17j2) == [("block", "DOC-0001-B001", "AUTHORING_SAVE")]
      and b17j2["db"]["audit_events"][0]["created_at"] < b001(b17j2)[0]["created_at"]
      and versions(b17j2, "block_versions", "block_id", "DOC-0001-B001") == [1]
      and [r["text_content"] for r in b001(a17j2)] == ["Revisione B del manuale."], [message(r17j2), actions(a17j2)])

# ------------------------------------------------------------ S17j-bis: an orphan autosave (IDML ids alternate)
p17jb, d17jb = draft_project("S17j-bis orphan autosave", IDML_A)
act17jb = outcome(app.studio_save_draft_ui, str(p17jb), "block", "IDML-0001-B001", "paragraph", "", 1,
                  "", "Bozza mai salvata dal redattore.", "", "", None, "", "", ACTOR)
r17jb, b17jb, a17jb, _ = rerun("S17jbis_orphan_autosave_after_idml_reimport", p17jb, d17jb, act17jb,
                               IDML_A, "it-IT", "PUB-MAIN", "Bozza")
r17jb2, _b17jb2, a17jb2, _ = rerun("S17jbis2_orphan_autosave_rebound_by_next_idml_reimport", p17jb, a17jb, None,
                                   IDML_A, "it-IT", "PUB-MAIN", "Bozza")
check("S17j-bis: an unsaved autosave does not stop an IDML re-import; the new draft gets new ids, so the autosave row "
      "is left DANGLING on IDML-0001-B001",
      act17jb == ("ok", SAVED) and r17jb[0] == "ok" and ids(a17jb, "topics", "topic_id") == ["IDML-0001-2"]
      and dangling(a17jb) == {**NO_DANGLING, "authoring_drafts": [("block", "IDML-0001-B001")]}, dangling(a17jb))
check("S17j-bis-b: the next IDML re-import alternates back to IDML-0001 (IDML-0001-2 is taken), so the same stale "
      "autosave is silently RE-BOUND to a new block whose version matches its base_version",
      r17jb2[0] == "ok" and ids(a17jb2, "topics", "topic_id") == ["IDML-0001"] and dangling(a17jb2) == NO_DANGLING
      and [(r["object_id"], r["base_version"], json.loads(r["payload_json"])) for r in a17jb2["db"]["authoring_drafts"]]
      == [("IDML-0001-B001", 1, {"text": "Bozza mai salvata dal redattore."})]
      and versions(a17jb2, "block_versions", "block_id", "IDML-0001-B001") == [1], [ids(a17jb2, "topics", "topic_id"),
                                                                                     dangling(a17jb2)])

# ------------------------------------------------------------ S17k: compliance evidence on a draft block
def _evidence_on_b001(project: Path):
    with repository(project) as repo:
        cs = ComplianceService(repo)
        cs.create_instrument("INS-1", "2006/42/CE", actor="Compliance", title="Direttiva macchine")
        cs.create_requirement("REQ-1", "Istruzioni per l'uso", actor="Compliance", instrument_id="INS-1")
        cs.create_scope("SC-1", "PUB-MAIN", actor="Compliance")
        cs.add_scope_reference("SC-1", "instrument", "INS-1", actor="Compliance")
        cs.set_applicability("SC-1", "REQ-1", "APPLIES", actor="Compliance")
        cs.add_evidence("EV-1", "block", actor="Compliance", object_id="DOC-0001-B001", object_version=1)
        cs.link_evidence_requirement("EV-1", "REQ-1", actor="Compliance")
        cs.set_assessment("SC-1", "REQ-1", "COVERED", actor="Compliance")
        return cs.evidence_for_requirement("REQ-1")


def _evidence_judgment(project: Path):
    with repository(project) as repo:
        return ComplianceService(repo).evidence_for_requirement("REQ-1")


p17k, d17k = draft_project("S17k compliance evidence", PDF_A)
act17k = outcome(_evidence_on_b001, p17k)
r17k, b17k, a17k, j17k = rerun("S17k_compliance_evidence_then_pdf_reimport", p17k, d17k, act17k,
                               PDF_B, "it-IT", "PUB-MAIN", "Revisione B", then=lambda: _evidence_judgment(p17k))
check("S17k: block evidence (DOC-0001-B001 v1, the imported text) backs a COVERED assessment",
      act17k[0] == "ok" and [(e["object_id"], e["object_version"], e["is_current"]) for e in act17k[1]]
      == [("DOC-0001-B001", 1, True)] and [r["status"] for r in b17k["db"]["compliance_assessment"]] == ["COVERED"], act17k)
check("S17k-b: a PDF re-import of ANOTHER revision is ACCEPTED (evidence is not read by the guard) and replaces the "
      "block's text", r17k[0] == "ok" and [(r["version"], r["text_content"]) for r in b001(a17k)]
      == [(1, "Revisione B del manuale.")], [message(r17k), b001(a17k)])
check("S17k-c: ... and the evidence still judges itself CURRENT (same id, same version 1): the assessment stays COVERED "
      "on a text nobody assessed",
      [(e["object_id"], e["object_version"], e["is_current"]) for e in j17k] == [("DOC-0001-B001", 1, True)]
      and [r["status"] for r in a17k["db"]["compliance_assessment"]] == ["COVERED"]
      and a17k["db"]["compliance_evidence"] == b17k["db"]["compliance_evidence"] and dangling(a17k) == NO_DANGLING, j17k)

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

base17c = {"draft": d17c, "action": act17c, "before": b17c, "outcome": r17c, "after": a17c, "then": None}
ref17c = digest(base17c)
m9a = _replace_everywhere(base17c, _new17c, "BLK-0123456789")
m9b = copy.deepcopy(base17c)
[r for r in m9b["after"]["db"]["audit_events"] if r["object_type"] == "block"][0]["object_id"] = "BLK-0123456789"
check("M9: another random Studio id (BLK-<10 hex>), used consistently, leaves the S17c digest unchanged "
      "(it is run-dependent)", ref17c == _observed.get("S17c_studio_block_then_pdf_reimport") and _new17c != "BLK-0123456789"
      and digest(m9a) == ref17c, "")
check("M9b: ... but an audit row that no longer names the block it was written for changes the digest",
      digest(m9b) != ref17c, "")
base17a = {"draft": d17a, "action": act17a, "before": b17a, "outcome": r17a, "after": a17a, "then": None}
m10 = copy.deepcopy(base17a)
b001(m10["after"])[0].update(version=2, text_content=EDITED)
check("M10: a re-import that KEPT the Studio edit (v2, edited text) changes the S17a digest",
      digest(base17a) == _observed.get("S17a_block_edit_then_pdf_reimport") and digest(m10) != digest(base17a), "")
base17k = {"draft": d17k, "action": act17k, "before": b17k, "outcome": r17k, "after": a17k, "then": j17k}
m11 = copy.deepcopy(base17k)
m11["then"][0]["is_current"] = False
check("M11: evidence judged NOT current after the re-import changes the S17k digest",
      digest(base17k) == _observed.get("S17k_compliance_evidence_then_pdf_reimport") and digest(m11) != digest(base17k), "")

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

# Pinned at HEAD d4f7c45 (CP5.2a) after three identical runs; S16 added at 02652e5 (CP5.2b-0), same procedure;
# S04x/S17 added on f9eb474 (HARDENING-02A, B5 characterization), same procedure, the 21 digests above unchanged.
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
    'S04x_pdf_reimport_over_idml_draft_no_translations': '4490ce2535a1753d184526e5c6aaa031e5fff6be81263fa3063127510ff86d28',
    'S17a2_block_edit_then_docx_reimport': 'a9e929ec9e058eec70e072a400e7f95cf2a908d55ac943dbde32e08637921f53',
    'S17a_block_edit_then_pdf_reimport': 'b734a2474215dffa82470c71ccb67f490a2e95c4629cad48ed1e09085c22397b',
    'S17b_title_edit_then_pdf_reimport': 'fed9f52cc3f42c0e3f42f8c5b8b41720b33f2c9def72cd104ec6092841a8a27e',
    'S17c_studio_block_then_pdf_reimport': '045769fa6be583864ddbd47f5d5f2306aa3305fceffe26937ce5c882586c6218',
    'S17d2_review_block_then_idml_reimport': '8c772d5b2ea618cc671ef02e9640779247302513fdee42f0c2adb2178de06047',
    'S17d_review_block_then_pdf_reimport': '6d0ffe7327eced42a88883fc020070718759b4c77bd7a5de20a5d834b26443d2',
    'S17e_archived_topic_then_pdf_reimport': '70399622e7e3a7d80a6c266e07adfb400ff40237713904dbcb35c9e7965acd75',
    'S17f_autosave_no_change_then_pdf_reimport': 'b6cdaaf160ef3fb3268d37c3dd503f9f8fee08ec8caa8878db474e56c32a7b5b',
    'S17g2_idml_json_only_decisions_then_idml_reimport': 'a36ed868d417b52ac0da66a89084f8872d12353ac35b7515b0438f75bce0894a',
    'S17g_idml_caption_confirmed_then_idml_reimport': '72555adce739bd7a6106168a9f8c5f5868edd29fab4d022cb135ffa395caa776',
    'S17h2_guided_blank_name_reimport': '19a6c7b8220206bf203bbf361d92f07c92f37740916bff3d9bf8afd8cd73c6ff',
    'S17h_guided_same_name_reimport': 'bc22f6d5b5922145e3244fab54325868080a50f23de68797f471ba73eddd76f6',
    'S17i_same_name_file_refused_overwrites_source': 'ceef59e5030d79007fe0ce519c50eebbf78154f898d7c3867cb0d2edcadfcee2',
    'S17j_audit_before_draft_then_pdf_reimport': '08a5f161bfa5e1a037c924cf8b08a5e4f074224e314d41e82b11fda70feda766',
    'S17j2_seeded_audit_before_draft_then_pdf_reimport': '4870e41777e5cde5f8a2f89c3834ceb68f2e747d5c9fc33f971f21afb9c4be6c',
    'S17jbis2_orphan_autosave_rebound_by_next_idml_reimport': '7acf8e1dd31eb69152f108804e9a8d48ec0193b0828e4b6ecf74a5246ec3ee8e',
    'S17jbis_orphan_autosave_after_idml_reimport': 'cb7e0ed8708f9c4e606dab64b2d9ac1dc0b945ad480552ab0355967cdd5105c1',
    'S17k_compliance_evidence_then_pdf_reimport': '0c9b69d4d8a1c184d69bf6e6b3555b052efa7f8e9d070c2057a535a9403ad078',
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
