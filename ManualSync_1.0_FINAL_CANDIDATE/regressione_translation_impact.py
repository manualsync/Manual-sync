"""CP5.3a regression: CHARACTERIZATION of translation_impact (the read side of the translation engine).

translation_impact.py reads the content database directly -- 25 SELECTs through
repo.conn, two of them with a table name chosen at run time -- plus 2 SELECTs on
Translation Memory files. A later move of that SQL into the repository (CP5.3b) has
to leave every observation below identical. This suite pins what the module does
TODAY (HEAD cf7dfa2), quirks included, on synthetic repositories only:

* T   publication_revision_token -- the token translation_roundtrip uses to tell
      whether a publication changed after a work package was exported: its exact
      value on a known repository, what changes it (versions, statuses, order, a
      dangling reference) and what does NOT (translations, text without a version
      bump, publication title, archived_at, objects outside the publication),
      plus the empty publication and the unknown id;
* U   extract_source_units for every kind (publication, topic with and without
      blocks, block of every type including rich/plain/empty, table, figure,
      component), kind normalization, and every error;
* M   the helpers behind it: _object_meta (both dynamic-SQL branches),
      _object_source_lang (incl. a table nobody references), _publication_status;
* A   _translation_assets: FRESH / STALE per kind (the second dynamic-SQL branch),
      and the component kind, which has no mapping (always empty);
* S   units_to_segments on a whole publication;
* TM  the Translation Memory side, kept apart from the content repository:
      _tm_pairs, discover_tm_languages (base-language matching), _consolidated_tm;
* I   analyze_translation_impact end to end (topic, publication, component), the
      files it writes, its fail-closed errors, and the default out_dir it creates
      and does not remove.

Only UTC timestamps and the temporary folder are normalized; ids, versions,
statuses, texts, tokens, scores and counts are not. Golden digests were pinned
after three identical runs; section MS proves they are sensitive.

Re-pin only after a reviewed, intentional behaviour change:
    ..\\.venv\\Scripts\\python.exe regressione_translation_impact.py --print-golden

Usage:  ..\\.venv\\Scripts\\python.exe regressione_translation_impact.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()

import pandas as pd  # noqa: E402

import translation_impact as tim  # noqa: E402
from cat_tm import TranslationMemory, cat_normalize  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402

PRINT_GOLDEN = "--print-golden" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="erylos_cp53a_impact_"))
_results: list[tuple[str, bool, str]] = []
_observed: dict[str, str] = {}


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:700]))
    if not PRINT_GOLDEN:
        print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:700]}]"))


# ------------------------------------------------------------ normalization: timestamps and the temp folder only
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?")


def plain(o):
    if isinstance(o, pd.DataFrame):
        return {"columns": [str(c) for c in o.columns], "rows": plain(o.to_dict(orient="records"))}
    if hasattr(o, "__dataclass_fields__"):
        return [[f, plain(getattr(o, f))] for f in o.__dataclass_fields__]
    if isinstance(o, dict):
        return [[plain(k), plain(v)] for k, v in o.items()]
    if isinstance(o, (list, tuple)):
        return [plain(x) for x in o]
    if isinstance(o, (set, frozenset)):
        return sorted(plain(x) for x in o)
    if o is pd.NA:
        return "<NA>"
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):     # numpy scalars
        return plain(o.item())
    if isinstance(o, float):
        return "NaN" if o != o else repr(o)
    if isinstance(o, Path):
        return str(o)
    return o


def normalized(o) -> str:
    text = json.dumps(plain(o), ensure_ascii=False, default=repr)
    for root in (str(TMP), str(test_isolation.TEST_HOME)):
        for form in (root.replace("\\", "\\\\"), root, root.replace("\\", "/")):
            text = text.replace(json.dumps(form, ensure_ascii=False)[1:-1], "<TMP>").replace(form, "<TMP>")
    return _TS.sub("<TS>", text)


def digest(o) -> str:
    return hashlib.sha256(normalized(o).encode("utf-8")).hexdigest()


def snap(label, o):
    _observed[label] = digest(o)


def outcome(fn, *a, **kw):
    try:
        return ("ok", fn(*a, **kw))
    except Exception as exc:  # noqa: BLE001 -- the observed behaviour IS the exception
        return ("raise", type(exc).__name__, str(exc))


# ------------------------------------------------------------ the synthetic content repository
_counter = [0]


def build(name: str | None = None) -> IndustrialRepository:
    """A fresh, fully deterministic repository (ids chosen here, never generated)."""
    _counter[0] += 1
    repo = IndustrialRepository(TMP / f"{name or 'repo'}_{_counter[0]:02d}.sqlite")
    repo.create_topic("T-A", "Uso previsto", "Il sistema è destinato all'uso industriale. Leggere il manuale.",
                      source_lang="it-IT", status="Approved")
    repo.set_translation("T-A", "en-US", "The system is intended for industrial use.", target_title="Intended use")
    repo.update_topic("T-A", body="Il sistema è destinato esclusivamente all'uso industriale. Leggere il manuale.")
    repo.set_translation("T-A", "de-DE", "Das System ist ausschließlich für den industriellen Einsatz bestimmt.")
    repo.create_topic("T-B", "Manutenzione", "", source_lang="it-IT", status="Approved")
    repo.create_component("C-1", "Avvertenza", "Scollegare l'alimentazione prima di intervenire.", source_lang="it-IT")
    repo.create_component("C-2", "Nota libera", "Componente non usato da nessun blocco.", source_lang="it-IT")
    repo.create_table("TB-1", ["Parametro", "Valore"], [["Pressione", "12 bar"], ["Tensione", "24 V"]],
                      title="Dati tecnici", caption="Valori nominali", status="Approved")
    repo.create_table("TB-2", ["Codice"], [["X-1"]], title="Tabella orfana", status="Approved")
    repo.create_table("TB-3", ["A"], [["1"]], title="Tabella poi rimossa", status="Approved")
    repo.create_figure("FG-1", title="Pompa", caption="Pompa idraulica", alt_text="pompa", status="Approved")
    blocks = [
        ("B-01", "paragraph", dict(text="La pressione massima ammessa è 12 bar.")),
        ("B-02", "table", dict(object_ref_id="TB-1")),
        ("B-03", "figure", dict(object_ref_id="FG-1")),
        ("B-04", "component_ref", dict(object_ref_id="C-1")),
        ("B-05", "paragraph", dict(text="Usare **sempre** i guanti di protezione.", text_format="rich")),
        ("B-06", "paragraph", dict(text="")),
        ("B-07", "paragraph", dict(text="Testo senza alcuna marcatura.", text_format="rich")),
        ("B-08", "paragraph", dict(text="La pressione massima ammessa è 12 bar.")),
        ("B-09", "paragraph", dict(text="24 V")),
    ]
    for pos, (bid, btype, kw) in enumerate(blocks, start=1):
        repo.add_block(bid, "T-B", btype, position=pos, status="Approved", **kw)
    repo.set_block_translation("B-01", "de-DE", "Der maximal zulässige Druck beträgt 12 bar.")
    repo.set_table_translation("TB-1", "fr-FR", caption="Valeurs nominales")
    repo.set_figure_translation("FG-1", "en-US", caption="Hydraulic pump", alt_text="pump")
    repo.set_component_translation("C-1", "en-US", "Disconnect the power supply.", target_title="Warning")
    repo.set_translation("T-B", "en-US", "", target_title="Maintenance")
    repo.create_topic("T-C", "Bozza", "Testo ancora in bozza.", source_lang="it-IT", status="Draft")
    repo.create_topic("T-D", "Riferimenti", "", source_lang="it-IT", status="Approved")
    repo.add_block("B-10", "T-D", "table", position=1, object_ref_id="TB-3", status="Approved")
    repo.create_publication("PUB-A", "Manuale completo", ["T-A", "T-B", "T-C"], source_lang="it-IT")
    repo.create_publication("PUB-OK", "Solo manutenzione", ["T-B"], source_lang="it-IT")
    repo.create_publication("PUB-D", "Riferimento rotto", ["T-D"], source_lang="it-IT")
    repo.create_publication("PUB-EMPTY", "Vuota", ["T-A"], source_lang="it-IT")
    # Test-side SQL to reach states the API does not produce: an empty publication, a block
    # pointing at a table that no longer exists, a topic whose stored language is not canonical.
    repo.conn.execute("DELETE FROM publication_topics WHERE publication_id='PUB-EMPTY'")
    repo.conn.execute("DELETE FROM structured_tables WHERE table_id='TB-3'")
    repo.conn.execute("UPDATE topics SET source_lang='it-it' WHERE topic_id='T-C'")
    repo.conn.commit()
    return repo


def units_of(units):
    return [(u.unit_id, u.object_kind, u.object_id, u.source_text, u.source_ref) for u in units]


# ============================================================ T: publication_revision_token
repo = build("token_base")
TOKEN_A = tim.publication_revision_token(repo, "PUB-A")
again = tim.publication_revision_token(repo, "PUB-A")
repo.close()
repo2 = build("token_rebuilt")
rebuilt = tim.publication_revision_token(repo2, "PUB-A")
repo2.close()
check("T1: the token is an int, identical on repeated calls and on an identically rebuilt repository",
      isinstance(TOKEN_A, int) and TOKEN_A == again == rebuilt, [TOKEN_A, again, rebuilt])
check("T1b: the token is the first 8 hex digits of sha256 -- a 32-bit value", 0 <= TOKEN_A < 2 ** 32, TOKEN_A)

MUTATIONS = {
    # name: (what it does, expected: does the PUB-A token change?)
    "topic_version": (lambda r: r.update_topic("T-A", body="Nuovo corpo."), True),
    "topic_status": (lambda r: r.conn.execute("UPDATE topics SET status='Published' WHERE topic_id='T-A'"), True),
    "block_version": (lambda r: r.update_block("B-01", text="Testo nuovo."), True),
    "block_status": (lambda r: r.conn.execute("UPDATE content_blocks SET status='Draft' WHERE block_id='B-01'"), True),
    "table_version": (lambda r: r.update_table("TB-1", caption="Nuova didascalia"), True),
    "figure_version": (lambda r: r.update_figure("FG-1", caption="Nuova didascalia"), True),
    "component_version": (lambda r: r.update_component("C-1", body="Nuovo corpo componente."), True),
    "publication_topic_order": (lambda r: r.replace_publication_topics("PUB-A", ["T-B", "T-A", "T-C"]), True),
    "block_order": (lambda r: r.conn.execute("UPDATE content_blocks SET position=CASE block_id WHEN 'B-01' THEN 2 "
                                             "WHEN 'B-02' THEN 1 ELSE position END WHERE topic_id='T-B'"), True),
    "block_added": (lambda r: r.add_block("B-11", "T-A", "paragraph", position=1, text="Nuovo blocco.",
                                          status="Approved"), True),
    "translation_added": (lambda r: r.set_translation("T-A", "fr-FR", "Le système est destiné..."), False),
    "text_without_version_bump": (lambda r: r.conn.execute("UPDATE topics SET body='Altro testo' WHERE topic_id='T-A'"), False),
    "publication_title": (lambda r: r.update_publication_header("PUB-A", "Titolo diverso", "en-US"), False),
    "block_archived_at": (lambda r: r.conn.execute("UPDATE content_blocks SET archived_at='2026-01-01T00:00:00+00:00' "
                                                   "WHERE block_id='B-01'"), False),
    "topic_archived_at": (lambda r: r.conn.execute("UPDATE topics SET archived_at='2026-01-01T00:00:00+00:00' "
                                                   "WHERE topic_id='T-A'"), False),
    "unreferenced_table_version": (lambda r: r.update_table("TB-2", caption="Cambiata"), False),
    "unreferenced_component_version": (lambda r: r.update_component("C-2", body="Cambiato"), False),
    "other_publication_topic": (lambda r: r.update_topic("T-D", body="Cambiato"), False),
}
token_matrix = {}
for name, (mutate, _expected) in MUTATIONS.items():
    r = build(f"mut_{name}")
    before = tim.publication_revision_token(r, "PUB-A")
    mutate(r)
    r.conn.commit()
    token_matrix[name] = {"before": before, "after": tim.publication_revision_token(r, "PUB-A")}
    r.close()
check("T2: every mutation starts from the same token (fixtures are identical)",
      {v["before"] for v in token_matrix.values()} == {TOKEN_A}, {k: v["before"] for k, v in token_matrix.items()})
_changes = {k: v["before"] != v["after"] for k, v in token_matrix.items()}
check("T3: versions, statuses, topic order, block order and an added block CHANGE the token",
      all(_changes[k] for k, (_, exp) in MUTATIONS.items() if exp), {k: _changes[k] for k, (_, e) in MUTATIONS.items() if e})
check("T4 (pinned): translations, text without a version bump, the publication title/language, archived_at and "
      "objects outside the publication do NOT change the token",
      not any(_changes[k] for k, (_, exp) in MUTATIONS.items() if not exp),
      {k: _changes[k] for k, (_, e) in MUTATIONS.items() if not e})

r = build("token_edges")
token_edges = {
    "PUB-D (block points at a table that no longer exists)": outcome(tim.publication_revision_token, r, "PUB-D"),
    "PUB-EMPTY (no topic)": outcome(tim.publication_revision_token, r, "PUB-EMPTY"),
    "PUB-OK": outcome(tim.publication_revision_token, r, "PUB-OK"),
    "unknown publication": outcome(tim.publication_revision_token, r, "PUB-NONE"),
}
_empty_token = int(hashlib.sha256(b"[]").hexdigest()[:8], 16)
check("T5: an empty publication gets the token of an empty list (sha256('[]')[:8])",
      token_edges["PUB-EMPTY (no topic)"] == ("ok", _empty_token), token_edges["PUB-EMPTY (no topic)"])
check("T6: a dangling table reference is skipped silently (the block counts, the missing table does not)",
      token_edges["PUB-D (block points at a table that no longer exists)"][0] == "ok", token_edges)
check("T7: an unknown publication raises KeyError", token_edges["unknown publication"][:2] == ("raise", "KeyError"),
      token_edges["unknown publication"])
r.close()
snap("T_publication_revision_token", {"PUB-A": TOKEN_A, "matrix": token_matrix, "edges": token_edges})

# ============================================================ U: extract_source_units
r = build("extract")
U_CASES = [
    ("publication", "PUB-OK"), ("publication", "PUB-A"),
    ("topic", "T-A"), ("topic", "T-B"), ("topic", "T-C"),
    ("block", "B-01"), ("block", "B-02"), ("block", "B-03"), ("block", "B-04"),
    ("block", "B-05"), ("block", "B-07"), ("block", "B-09"),
    ("table", "TB-1"), ("table", "TB-2"), ("figure", "FG-1"), ("component", "C-1"),
    ("  Topic  ", "T-A"),
]
extracted = {}
for kind, oid in U_CASES:
    res = outcome(tim.extract_source_units, r, oid, kind=kind)
    extracted[f"{kind}:{oid}"] = (res[0], units_of(res[1][0]), res[1][1]) if res[0] == "ok" else res
_u = {k: v[1] for k, v in extracted.items() if v[0] == "ok"}
check("U1: every listed kind extracts without error", all(v[0] == "ok" for v in extracted.values()),
      {k: v for k, v in extracted.items() if v[0] != "ok"})
check("U2: a topic without blocks falls back to its body, versioned (T-A#v2)",
      _u["topic:T-A"] == [("T-A:title", "topic_title", "T-A", "Uso previsto", "T-A#title"),
                          ("T-A", "topic", "T-A", "Il sistema è destinato esclusivamente all'uso industriale. "
                                                  "Leggere il manuale.", "T-A#v2")], _u["topic:T-A"])
check("U3: a topic with blocks: title, then each block in position order; the empty block yields nothing",
      [x[0] for x in _u["topic:T-B"]][:2] == ["T-B:title", "B-01"] and not any(x[0] == "B-06" for x in _u["topic:T-B"]),
      [x[0] for x in _u["topic:T-B"]])
check("U4: table, figure and component blocks become first-class units named after the BLOCK, referenced to the object",
      [x[0] for x in _u["block:B-02"]][:3] == ["B-02:title", "B-02:caption", "B-02:header:1"]
      and all(x[4].startswith("TB-1#") for x in _u["block:B-02"])
      and [x[0] for x in _u["block:B-03"]] == ["B-03:title", "B-03:caption", "B-03:alt_text"]
      and [x[0] for x in _u["block:B-04"]] == ["B-04:title", "B-04:body"], [_u["block:B-02"], _u["block:B-04"]])
check("U5: a rich block with real marks is extracted as visible text and carries RICH_FORMAT_UNSAFE_MARKER",
      _u["block:B-05"][0][3] == "Usare sempre i guanti di protezione."
      and tim.RICH_FORMAT_UNSAFE_MARKER in _u["block:B-05"][0][4], _u["block:B-05"])
check("U6: a block flagged rich but without marks is extracted unchanged, without the marker",
      _u["block:B-07"] == [("B-07", "block", "B-07", "Testo senza alcuna marcatura.", "B-07#v1")], _u["block:B-07"])
check("U7: a table extracted directly is named after the table itself", _u["table:TB-1"][0][0] == "TB-1:title",
      _u["table:TB-1"][:2])
check("U8: a publication concatenates its topics in position order (PUB-A: T-A, T-B, T-C)",
      [x[0] for x in _u["publication:PUB-A"] if x[1] == "topic_title"] == ["T-A:title", "T-B:title", "T-C:title"], "")
check("U9: the kind is stripped and lower-cased ('  Topic  ' == 'topic')", _u["  Topic  :T-A"] == _u["topic:T-A"], "")
extract_errors = {
    "unknown kind": outcome(tim.extract_source_units, r, "T-A", kind="glossary"),
    "empty kind": outcome(tim.extract_source_units, r, "T-A", kind=""),
    "missing topic": outcome(tim.extract_source_units, r, "T-NONE", kind="topic"),
    "missing block": outcome(tim.extract_source_units, r, "B-NONE", kind="block"),
    "missing table": outcome(tim.extract_source_units, r, "TB-NONE", kind="table"),
    "missing figure": outcome(tim.extract_source_units, r, "FG-NONE", kind="figure"),
    "missing component": outcome(tim.extract_source_units, r, "C-NONE", kind="component"),
    "missing publication": outcome(tim.extract_source_units, r, "PUB-NONE", kind="publication"),
    "empty publication": outcome(tim.extract_source_units, r, "PUB-EMPTY", kind="publication"),
    "empty block": outcome(tim.extract_source_units, r, "B-06", kind="block"),
    "dangling table block": outcome(tim.extract_source_units, r, "B-10", kind="block"),
    "publication with a dangling block": outcome(tim.extract_source_units, r, "PUB-D", kind="publication"),
}
check("U10: an unsupported or empty kind raises ValueError 'kind non supportato'",
      all(extract_errors[k][1] == "ValueError" and "kind non supportato" in extract_errors[k][2]
          for k in ("unknown kind", "empty kind")), [extract_errors["unknown kind"], extract_errors["empty kind"]])
check("U11: a missing object raises KeyError for every kind",
      all(extract_errors[k][1] == "KeyError" for k in extract_errors if k.startswith("missing")),
      {k: v for k, v in extract_errors.items() if k.startswith("missing")})
check("U12: an empty publication and an empty block raise ValueError with their own messages",
      extract_errors["empty publication"] == ("raise", "ValueError", "Pubblicazione vuota: PUB-EMPTY")
      and extract_errors["empty block"] == ("raise", "ValueError", "Nessun contenuto sorgente analizzabile per block:B-06"),
      [extract_errors["empty publication"], extract_errors["empty block"]])
check("U13 (pinned): a block pointing at a missing table raises KeyError -- directly and through its publication",
      extract_errors["dangling table block"][:2] == ("raise", "KeyError")
      and extract_errors["publication with a dangling block"][:2] == ("raise", "KeyError"),
      [extract_errors["dangling table block"], extract_errors["publication with a dangling block"]])
snap("U_extract_source_units", extracted)
snap("U_extract_errors", extract_errors)

# ============================================================ M: _object_meta, _object_source_lang, _publication_status
META_CASES = [("publication", "PUB-A"), ("publication", "PUB-OK"), ("topic", "T-A"), ("topic", "T-C"), ("block", "B-01"),
              ("table", "TB-1"), ("table", "TB-2"), ("figure", "FG-1"), ("component", "C-1")]
meta = {f"{k}:{i}": outcome(tim._object_meta, r, i, k) for k, i in META_CASES}
langs = {f"{k}:{i}": outcome(tim._object_source_lang, r, i, k) for k, i in META_CASES + [("glossary", "X")]
         + [(k, "X-NONE") for k in ("publication", "topic", "component", "block", "table", "figure")]}
pub_status = {p: tim._publication_status(r, p) for p in ("PUB-A", "PUB-OK", "PUB-D", "PUB-EMPTY", "PUB-NONE")}
check("M1: _object_meta returns the whole stored row (SELECT *) plus source_lang; a publication also gets the "
      "revision token as 'version' and its computed status",
      meta["topic:T-A"][1]["version"] == 2 and meta["topic:T-A"][1]["body"].startswith("Il sistema")
      and meta["publication:PUB-A"][1]["version"] == TOKEN_A and meta["publication:PUB-A"][1]["status"] == "Draft",
      [meta["topic:T-A"], meta["publication:PUB-A"]])
check("M2: the stored language is canonicalized on read ('it-it' -> 'it-IT')", langs["topic:T-C"] == ("ok", "it-IT"),
      langs["topic:T-C"])
check("M3 (pinned): a table/figure takes the language of the first referencing block; a table nobody references has ''",
      langs["table:TB-1"] == ("ok", "it-IT") and langs["table:TB-2"] == ("ok", ""), [langs["table:TB-1"], langs["table:TB-2"]])
check("M4: an unsupported kind raises ValueError in _object_source_lang",
      langs["glossary:X"][:2] == ("raise", "ValueError"), langs["glossary:X"])
check("M5: _publication_status: the first non-approved topic or block status wins; EMPTY when nothing is linked",
      pub_status == {"PUB-A": "Draft", "PUB-OK": "Approved", "PUB-D": "Approved", "PUB-EMPTY": "EMPTY", "PUB-NONE": "EMPTY"},
      pub_status)
snap("M_meta_language_status", {"meta": meta, "langs": langs, "publication_status": pub_status})

# ============================================================ A: _translation_assets
ASSET_CASES = [("topic", "T-A", 2), ("topic", "T-A", 1), ("block", "B-01", 1), ("table", "TB-1", 1), ("figure", "FG-1", 1),
               ("component", "C-1", 1), ("topic", "T-C", 1)]
assets = {f"{k}:{i}@v{v}": tim._translation_assets(r, i, k, v) for k, i, v in ASSET_CASES}
_ta = assets["topic:T-A@v2"]
check("A1: a translation made at the current version is FRESH, one made before an update is STALE (ordered by language)",
      _ta[["target_lang", "translation_status", "translation_source_version"]].values.tolist()
      == [["de-DE", "FRESH", 2], ["en-US", "STALE", 1]], _ta.to_dict(orient="records"))
check("A2: block, table and figure translations come from their own tables",
      [assets[k]["target_lang"].tolist() for k in ("block:B-01@v1", "table:TB-1@v1", "figure:FG-1@v1")]
      == [["de-DE"], ["fr-FR"], ["en-US"]], "")
check("A3 (pinned): the component kind has no mapping -- its translations are never reported",
      assets["component:C-1@v1"].empty and list(assets["component:C-1@v1"].columns)
      == ["target_lang", "translation_status", "translation_source_version", "translation_record_status"],
      assets["component:C-1@v1"])
snap("A_translation_assets", assets)

# ============================================================ S: units_to_segments
pub_units, _ = tim.extract_source_units(r, "PUB-OK", kind="publication")
segments, seg_meta = tim.units_to_segments(pub_units)
check("S1: segments are numbered impact-00001.. in unit order and every segment has its metadata row",
      [s.segment_id for s in segments][:2] == ["impact-00001", "impact-00002"] and len(segments) == len(seg_meta), len(segments))
snap("S_units_to_segments", {"segments": segments, "meta": seg_meta})
r.close()

# ============================================================ TM: the Translation Memory side (separate from the content repository)
TM_DIR = TMP / "tm"
TM_DIR.mkdir()


def make_tm(name: str, entries) -> Path:
    path = TM_DIR / name
    tm = TranslationMemory(path)
    try:
        for src_l, tgt_l, src, tgt, approved in entries:
            tm.add(src_l, tgt_l, src, tgt, "", "", "suite", 100.0, approved, name.split(".")[0], 0, 0.0)
    finally:
        tm.close()
    return path


TM_MAIN = make_tm("tm_main.sqlite", [
    ("it-IT", "en-US", "Manutenzione", "Maintenance", True),
    ("it-IT", "en-US", "La pressione massima ammessa è 10 bar.", "The maximum permitted pressure is 10 bar.", True),
    ("it", "de-DE", "Manutenzione", "Wartung", True),
    ("en-US", "fr-FR", "Maintenance", "Entretien", True),
])
TM_SECOND = make_tm("tm_second.sqlite", [
    ("it-CH", "EN-us", "Pompa idraulica", "Hydraulic pump", False),
    ("it-IT", "es-ES", "Pompa", "Bomba", True),
])
tm_side = {
    "pairs main": outcome(tim._tm_pairs, TM_MAIN),
    "pairs missing file": outcome(tim._tm_pairs, TM_DIR / "missing.sqlite"),
    "discover it-IT": outcome(tim.discover_tm_languages, [TM_MAIN, TM_SECOND, ""], "it-IT"),
    "discover en": outcome(tim.discover_tm_languages, [TM_MAIN, TM_SECOND], "en"),
}
check("TM1: _tm_pairs lists DISTINCT (source, target) pairs; a missing file raises FileNotFoundError",
      tm_side["pairs main"][0] == "ok" and tm_side["pairs missing file"][:2] == ("raise", "FileNotFoundError"), tm_side)
_disc = tm_side["discover it-IT"][1] if tm_side["discover it-IT"][0] == "ok" else {}
check("TM2: discover_tm_languages matches the BASE source language (it-IT ~ it ~ it-CH), canonicalizes targets, "
      "skips empty paths and other source languages",
      sorted(_disc) == ["de-DE", "en-US", "es-ES"] and "fr-FR" not in _disc
      and [p.name for p in _disc.get("en-US", [])] == ["tm_main.sqlite", "tm_second.sqlite"], _disc)
_cons_path = TM_DIR / "_consolidated.sqlite"
_cons_path.write_bytes(b"stale content that must be replaced")
cons = tim._consolidated_tm([TM_MAIN, TM_SECOND], "it-IT", "en-US", _cons_path)
try:
    _cons_rows = [dict(row) for row in cons.conn.execute("SELECT * FROM tm_entries ORDER BY rowid")]
finally:
    cons.close()
check("TM3: _consolidated_tm replaces the output file and keeps only entries of the base source language and the "
      "(case-insensitive) target, tm_name prefixed with the file stem",
      [(x["source"], x["tm_name"]) for x in _cons_rows]
      == [("Manutenzione", "tm_main:tm_main"), ("La pressione massima ammessa è 10 bar.", "tm_main:tm_main"),
          ("Pompa idraulica", "tm_second:tm_second")], [(x["source"], x["tm_name"]) for x in _cons_rows])
snap("TM_translation_memory_side", {"calls": tm_side, "consolidated": _cons_rows})

# ============================================================ I: analyze_translation_impact
RATES = pd.DataFrame([{"target_lang": l, "rate_new": 0.12, "rate_fuzzy": 0.08, "rate_reuse": 0.02, "rate_technical_block": 0.05}
                      for l in ("en-US", "de-DE", "es-ES", "fr-FR")])


def analysis(label, repo_obj, changed_id, **kw):
    out = TMP / "analysis" / label
    res = outcome(tim.analyze_translation_impact, repo_obj, changed_id, out_dir=out, **kw)
    if res[0] != "ok":      # what a failure leaves behind is part of the behaviour
        return res + (sorted(p.name for p in out.iterdir()) if out.exists() else None,)
    files = {p.name: (p.read_text(encoding="utf-8-sig") if p.suffix in (".csv", ".json") else "sqlite")
             for p in sorted(out.iterdir())}
    v = res[1]
    return ("ok", {"overall": v["overall"], "language_summary": v["language_summary"], "detail": v["detail"],
                   "impact_map": v["impact_map"], "paths": [v["detail_csv"], v["language_csv"], v["summary_json"]],
                   "files": files})


r = build("analysis")
i_topic = analysis("topic_T-B", r, "T-B", kind="topic", tm_paths=[TM_MAIN, TM_SECOND], target_langs=["fr-FR"],
                   rate_card=RATES)
_ov = i_topic[1]["overall"] if i_topic[0] == "ok" else {}
_ls = i_topic[1]["language_summary"] if i_topic[0] == "ok" else pd.DataFrame()
check("I1: the topic analysis covers every TM language + the CCMS translations + the requested ones, sorted",
      i_topic[0] == "ok" and _ls["target_lang"].tolist() == ["de-DE", "en-US", "es-ES", "fr-FR"], i_topic if i_topic[0] != "ok" else _ls)
check("I1b: overall reports the approved source, its version, language and the impact (2 publications use T-B)",
      _ov.get("source_status") == "Approved" and _ov.get("source_version") == 1 and _ov.get("source_lang") == "it-IT"
      and _ov.get("affected_publications") == 2 and _ov.get("cost_available") is True, _ov)
_det = i_topic[1]["detail"] if i_topic[0] == "ok" else pd.DataFrame()
check("I1c: the exact repetition inside the document and the technical value are recognised",
      "INTRA_DOCUMENT_REUSE" in set(_det["work_bucket"]) and "NOT_TRANSLATABLE" in set(_det["work_bucket"]),
      sorted(set(_det["work_bucket"])) if not _det.empty else _det)
check("I1d: the CCMS translation status is reported per language (T-B en-US exists, the others are MISSING)",
      dict(zip(_ls["target_lang"], _ls["ccms_translation_status"])) == {"de-DE": "MISSING", "en-US": "FRESH",
                                                                         "es-ES": "MISSING", "fr-FR": "MISSING"},
      dict(zip(_ls["target_lang"], _ls["ccms_translation_status"])) if not _ls.empty else _ls)
check("I1e: it writes the segments CSV, the languages CSV, the summary JSON and one consolidated TM per TM language",
      i_topic[0] == "ok" and sorted(i_topic[1]["files"]) == ["TRANSLATION_IMPACT_LANGUAGES.csv", "TRANSLATION_IMPACT_SEGMENTS.csv",
                                                             "TRANSLATION_IMPACT_SUMMARY.json", "_tm_de-DE.sqlite",
                                                             "_tm_en-US.sqlite", "_tm_es-ES.sqlite"],
      sorted(i_topic[1]["files"]) if i_topic[0] == "ok" else i_topic)
snap("I_analyze_topic", i_topic)

i_pub = analysis("publication_PUB-OK", r, "PUB-OK", kind="publication", target_langs=["en-US"])
check("I2: a publication analysis uses the revision token as source_version and lists its topics as the impact map",
      i_pub[0] == "ok" and i_pub[1]["overall"]["source_version"] == tim.publication_revision_token(r, "PUB-OK")
      and i_pub[1]["impact_map"]["topic_id"].tolist() == ["T-B"] and i_pub[1]["overall"]["cost_available"] is False,
      i_pub if i_pub[0] != "ok" else i_pub[1]["overall"])
snap("I_analyze_publication", i_pub)

i_comp = analysis("component_C-1", r, "C-1", kind="component", target_langs=["de-DE"])
check("I3 (pinned): a component is analysed although it has no status -- components skip the approval gate by KIND "
      "(source_status 'N/A') -- and its CCMS translation is reported MISSING (component translations are not read)",
      i_comp[0] == "ok" and i_comp[1]["overall"]["source_status"] == "N/A"
      and i_comp[1]["language_summary"]["ccms_translation_status"].tolist() == ["MISSING"],
      i_comp if i_comp[0] != "ok" else i_comp[1]["overall"])
snap("I_analyze_component", i_comp)

analysis_errors = {
    "empty changed_id": analysis("e1", r, "  ", kind="topic", target_langs=["en-US"]),
    "draft topic (fail-closed)": analysis("e2", r, "T-C", kind="topic", target_langs=["en-US"]),
    "draft topic, require_approved=False": analysis("e3", r, "T-C", kind="topic", target_langs=["en-US"],
                                                    require_approved=False),
    "no target language": analysis("e4", r, "B-07", kind="block"),          # B-07: no translation, no TM, none requested
    "incomplete rate card": analysis("e5", r, "T-A", kind="topic", tm_paths=[TM_MAIN],
                                     rate_card=RATES.drop(columns=["rate_fuzzy"])),
    "table nobody references (no language)": analysis("e6", r, "TB-2", kind="table", target_langs=["en-US"]),
    "missing TM file": analysis("e7", r, "T-A", kind="topic", tm_paths=[TM_DIR / "missing.sqlite"]),
}
check("I4: an empty changed_id raises ValueError 'changed_id obbligatorio' (before creating out_dir)",
      analysis_errors["empty changed_id"] == ("raise", "ValueError", "changed_id obbligatorio", None),
      analysis_errors["empty changed_id"])
check("I5: a non-approved source is refused (fail-closed) and accepted with require_approved=False",
      analysis_errors["draft topic (fail-closed)"][:2] == ("raise", "ValueError")
      and "status=Draft" in analysis_errors["draft topic (fail-closed)"][2]
      and analysis_errors["draft topic, require_approved=False"][0] == "ok", analysis_errors["draft topic (fail-closed)"])
check("I6: without any target language (no TM, no CCMS translation, none requested) it raises ValueError",
      analysis_errors["no target language"][:2] == ("raise", "ValueError")
      and "Nessuna lingua target" in analysis_errors["no target language"][2], analysis_errors["no target language"])
check("I7: an incomplete rate card raises ValueError naming the missing column",
      analysis_errors["incomplete rate card"][:3] == ("raise", "ValueError", "Rate card incompleta: rate_fuzzy"),
      analysis_errors["incomplete rate card"])
check("I7b (pinned): ... after out_dir was created and the consolidated TMs written -- they are left behind",
      analysis_errors["incomplete rate card"][3] == ["_tm_de-DE.sqlite", "_tm_en-US.sqlite"], analysis_errors["incomplete rate card"])
check("I8 (pinned): a table nobody references has no source language -> ValueError",
      analysis_errors["table nobody references (no language)"] == ("raise", "ValueError",
                                                                    "Lingua sorgente non determinabile dal CCMS", None),
      analysis_errors["table nobody references (no language)"])
check("I9: a TM path that does not exist raises FileNotFoundError",
      analysis_errors["missing TM file"][:2] == ("raise", "FileNotFoundError"), analysis_errors["missing TM file"])
snap("I_analyze_errors", analysis_errors)

# default out_dir: a new mkdtemp folder (prefix manualsync_v22_impact_) that is NOT removed -- kept inside TMP here
DEFAULT_TMP = TMP / "default_tempdir"
DEFAULT_TMP.mkdir()
_saved_tempdir = tempfile.tempdir
tempfile.tempdir = str(DEFAULT_TMP)
try:
    i_default = outcome(tim.analyze_translation_impact, r, "T-A", kind="topic", target_langs=["en-US"])
    _tempdir_during = tempfile.tempdir
finally:
    tempfile.tempdir = _saved_tempdir
left = sorted(p.name for p in DEFAULT_TMP.iterdir())
check("I10 (pinned): without out_dir it writes into a NEW mkdtemp folder 'manualsync_v22_impact_*' and leaves it there",
      i_default[0] == "ok" and len(left) == 1 and left[0].startswith("manualsync_v22_impact_")
      and sorted(p.name for p in (DEFAULT_TMP / left[0]).iterdir())
      == ["TRANSLATION_IMPACT_LANGUAGES.csv", "TRANSLATION_IMPACT_SEGMENTS.csv", "TRANSLATION_IMPACT_SUMMARY.json"],
      [i_default[:2] if i_default[0] != "ok" else "ok", left])
check("I10b: the module does not change tempfile.tempdir (it is still the redirected folder after the call)",
      _tempdir_during == str(DEFAULT_TMP), _tempdir_during)
_dflt_files = sorted(p.name for p in (DEFAULT_TMP / left[0]).iterdir()) if left else []
snap("I_analyze_default_out_dir", {"overall": i_default[1]["overall"] if i_default[0] == "ok" else i_default,
                                   "folders": [re.sub(r"manualsync_v22_impact_\w+", "manualsync_v22_impact_<RANDOM>", x)
                                               for x in left], "files": _dflt_files})
r.close()

# ============================================================ E: edge fixture (cases raised by the independent coverage review)
# Orders that disagree (position vs id vs insertion), position ties, archived rows, statuses at block level,
# shared objects with different languages, dangling figure/component, a link to a missing topic, every text
# block type, versions that differ between a block and its topic, and unit-builder corner cases.
def build_x() -> IndustrialRepository:
    _counter[0] += 1
    path = TMP / f"edge_{_counter[0]:02d}.sqlite"
    x = IndustrialRepository(path)
    # -- order: inserted T-5, T-9, T-1; ids T-1 < T-5 < T-9; positions T-9, T-1, T-5
    x.create_topic("T-5", "Quinto", "Corpo del topic archiviato.", source_lang="it-IT", status="Approved")
    x.create_topic("T-9", "Nono", "Corpo ignorato: il topic ha blocchi.", source_lang="it-IT", status="Approved")
    x.create_topic("T-1", "Primo", "", source_lang="it-IT", status="Approved")
    for bid, btype, pos, text in (("B-Z", "code", 3, "M8x1.25"), ("B-A", "warning", 1, "Pericolo: alta tensione presente."),
                                  ("B-Y", "list_item", 2, "Controllare il livello dell'olio."),   # inserted BEFORE B-M
                                  ("B-M", "note", 2, "Nota: verificare il serraggio."),
                                  ("B-Y2", "list_item", 4, "Controllare il livello dell'olio."),
                                  ("B-E1", "paragraph", 5, "Elettrovalvolapneumatica"),
                                  ("B-E2", "paragraph", 6, "Elettrovalvolapneumatica")):
        x.add_block(bid, "T-9", btype, position=pos, text=text, status="Approved")
    x.set_block_translation("B-A", "fr-FR", "Danger : haute tension présente.", status="Approved")
    x.update_block("B-A", text="Pericolo: alta tensione presente!")
    x.update_block("B-A", text="Pericolo: alta tensione presente.")            # B-A at v3, T-9 still v1
    x.set_block_translation("B-A", "de-DE", "Gefahr: Hochspannung vorhanden.", status="Approved")
    x.create_table("TB-AR", ["Voce"], [["Solo archiviata"]], title="Tabella archiviata", status="Approved")
    x.add_block("B-1A", "T-1", "paragraph", position=1, text="Blocco archiviato in bozza.", status="Draft")
    x.add_block("B-1B", "T-1", "paragraph", position=2, text="Blocco attivo.", status="Approved")
    x.add_block("B-1C", "T-1", "table", position=3, object_ref_id="TB-AR", status="Approved")
    x.create_publication("PUB-X", "Ordini diversi", ["T-9", "T-1", "T-5"], source_lang="it-IT")
    x.create_publication("PUB-TIE", "Posizioni pari", ["T-5", "T-1"], source_lang="it-IT")
    # -- statuses at block level, Published, a Draft table ignored
    x.create_topic("T-S9", "Stato alto", "", source_lang="it-IT", status="Approved")
    x.add_block("B-S0", "T-S9", "paragraph", position=3, text="Inserito per primo.", status="Draft")
    x.add_block("B-S1", "T-S9", "paragraph", position=1, text="In revisione.", status="In Review")
    x.add_block("B-S2", "T-S9", "paragraph", position=2, text="In bozza.", status="Draft")
    x.create_topic("T-S0", "Stato basso", "Bozza.", source_lang="it-IT", status="Draft")
    x.create_publication("PUB-ST", "Stato dai blocchi", ["T-S9", "T-S0"], source_lang="it-IT")
    x.create_topic("T-QB", "Pari B", "In revisione.", source_lang="it-IT", status="In Review")
    x.create_topic("T-QA", "Pari A", "In bozza.", source_lang="it-IT", status="Draft")
    x.create_publication("PUB-STIE", "Stati pari", ["T-QB", "T-QA"], source_lang="it-IT")
    x.create_topic("T-P", "Pubblicato", "", source_lang="it-IT", status="Published")
    x.add_block("B-P1", "T-P", "paragraph", position=1, text="Contenuto pubblicato.", status="Published")
    x.create_publication("PUB-PUB", "Pubblicata", ["T-P"], source_lang="it-IT")
    x.create_table("TB-D", ["K"], [["v"]], title="Tabella in bozza", status="Draft")
    x.create_topic("T-T", "Con tabella in bozza", "", source_lang="it-IT", status="Approved")
    x.add_block("B-T1", "T-T", "table", position=1, object_ref_id="TB-D", status="Approved")
    x.create_publication("PUB-TBL", "Tabella in bozza", ["T-T"], source_lang="it-IT")
    # -- languages: TB-L referenced from T-L (it-IT, created first) and T-0 (de-DE, created later, smaller id);
    #    a FIGURE with the same id TB-L referenced from A-FIG (fr-FR, the smallest id of all)
    x.create_table("TB-L", ["Colonna"], [["Valore"]], title="Tabella condivisa", status="Approved")
    x.set_table_translation("TB-L", "en-US", caption="Shared table")
    x.update_table("TB-L", caption="Didascalia aggiornata")
    x.create_topic("T-L", "Lingua italiana", "", source_lang="it-IT", status="Approved")
    x.add_block("B-L1", "T-L", "table", position=1, object_ref_id="TB-L", status="Approved")
    x.create_topic("T-0", "Deutsch", "", source_lang="de-DE", status="Approved")
    x.add_block("B-ZX", "T-0", "table", position=1, object_ref_id="TB-L", status="Approved")    # id after B-L1
    x.create_figure("TB-L", title="Figura omonima", caption="Stesso id di una tabella", status="Approved")
    x.create_topic("A-FIG", "Figure francesi", "", source_lang="fr-FR", status="Approved")
    x.add_block("B-AF", "A-FIG", "figure", position=1, object_ref_id="TB-L", status="Approved")
    x.create_figure("FG-ORPHAN", title="Figura orfana", status="Approved")
    # FG-SH is both a table and a figure: the FIGURE is referenced from T-M (fr-FR), the TABLE from A-TBL (es-ES,
    # smaller topic id) -- a figure lookup that forgot block_type would answer es-ES
    x.create_table("FG-SH", ["Z"], [["z"]], title="Tabella omonima", status="Approved")
    x.create_figure("FG-SH", title="Figura condivisa", status="Approved")
    x.create_topic("A-TBL", "Tabelle spagnole", "", source_lang="es-ES", status="Approved")
    x.add_block("B-ATB", "A-TBL", "table", position=1, object_ref_id="FG-SH", status="Approved")
    x.create_topic("T-M", "Figure francesi 2", "", source_lang="fr-FR", status="Approved")
    x.add_block("B-FSH", "T-M", "figure", position=1, object_ref_id="FG-SH", status="Approved")
    x.create_figure("FG-NA", title="Senza testo alternativo", caption="Didascalia", alt_text="", status="Approved")
    x.set_figure_translation("FG-NA", "es-ES", caption="Leyenda", status="Approved")
    x.add_block("B-NA", "T-L", "figure", position=2, object_ref_id="FG-NA", status="Approved")
    x.create_publication("PUB-EN", "Pubblicazione inglese", ["T-L"], source_lang="en-US")
    x.create_component("C-EN", "Warning", "Disconnect the power supply.", source_lang="en-US")
    # -- dangling figure and component; a link to a topic that does not exist
    x.create_figure("FG-GONE", title="Rimossa", status="Approved")
    x.create_component("C-GONE", "Rimosso", "Componente rimosso.", source_lang="it-IT")
    x.create_topic("T-DG", "Riferimenti rotti", "", source_lang="it-IT", status="Approved")
    x.add_block("B-DGF", "T-DG", "figure", position=1, object_ref_id="FG-GONE", status="Approved")
    x.add_block("B-DGC", "T-DG", "component_ref", position=2, object_ref_id="C-GONE", status="Approved")
    x.create_publication("PUB-DG", "Riferimenti rotti", ["T-DG"], source_lang="it-IT")
    x.create_publication("PUB-GHOST", "Topic fantasma", ["T-L"], source_lang="it-IT")
    # -- unit-builder corner cases
    x.create_topic("T-U", "Casi limite", "", source_lang="it-IT", status="Approved")
    x.add_block("B-PM", "T-U", "paragraph", position=1, text="Coppia M8*1.25 **non** formattata", status="Approved")
    x.add_block("B-RO", "T-U", "paragraph", position=2, text="** **", text_format="rich", status="Approved")
    x.add_block("B-SYM", "T-U", "paragraph", position=3, text="***", status="Approved")
    x.create_table("TB-E", ["", "Valore"], [["", "5"]], title="", caption="", status="Approved")
    x.add_block("B-TE", "T-U", "table", position=4, object_ref_id="TB-E", status="Approved")
    x.create_table("TB-M", ["X"], [["Y"]], title="Json rotto", status="Approved")
    x.create_component("C-NT", "", "Solo corpo.", source_lang="it-IT")
    x.create_topic("T-NT", "Titolo provvisorio", "Topic senza titolo.", source_lang="it-IT", status="Approved")
    x.create_topic("T-LONE", "Isolato", "Topic fuori da ogni pubblicazione.", source_lang="it-IT", status="Approved")
    x.set_translation("T-LONE", "en-US", "Topic outside any publication.")
    # -- stored languages that are not canonical (set below with raw SQL)
    x.create_topic("T-RAW", "Lingua grezza", "", source_lang="it-IT", status="Approved")
    x.create_table("TB-RAW", ["R"], [["r"]], title="Tabella lingua grezza", status="Approved")
    x.create_figure("FG-RAW", title="Figura lingua grezza", status="Approved")
    x.add_block("B-RW1", "T-RAW", "paragraph", position=1, text="Testo in lingua grezza.", status="Approved")
    x.add_block("B-RW2", "T-RAW", "table", position=2, object_ref_id="TB-RAW", status="Approved")
    x.add_block("B-RW3", "T-RAW", "figure", position=3, object_ref_id="FG-RAW", status="Approved")
    x.create_publication("PUB-RAW", "Lingua grezza", ["T-RAW"], source_lang="it-IT")
    x.create_component("C-RAW", "Raw", "Componente con lingua grezza.", source_lang="en-US")
    # -- archived topics and the publication status
    x.create_topic("T-AD", "Archiviato in bozza", "", source_lang="es-ES", status="Draft")
    x.create_table("TB-ADONLY", ["Q"], [["q"]], title="Solo nel topic archiviato", status="Approved")
    x.add_block("B-AD1", "T-AD", "table", position=1, object_ref_id="TB-ADONLY", status="Approved")
    x.create_publication("PUB-ARC", "Archiviato in bozza", ["T-P", "T-AD"], source_lang="it-IT")
    x.create_topic("T-AA", "Archiviato approvato", "Corpo.", source_lang="it-IT", status="Approved")
    x.create_publication("PUB-ARC2", "Solo archiviato", ["T-AA"], source_lang="it-IT")
    # -- non-ASCII ids and status for the token serialization (ensure_ascii=False)
    x.create_topic("T-Sicurezza-è", "Sicurezza", "", source_lang="it-IT", status="Approved")
    x.add_block("B-ü1", "T-Sicurezza-è", "paragraph", position=1, text="Attenzione.", status="Approved")
    x.create_publication("PUB-UTF", "Caratteri non ASCII", ["T-Sicurezza-è"], source_lang="it-IT")
    # test-side SQL for states the API does not produce
    x.conn.execute("UPDATE content_blocks SET archived_at='2026-01-01T00:00:00+00:00' WHERE block_id IN ('B-1A','B-1C')")
    x.conn.execute("UPDATE topics SET archived_at='2026-01-01T00:00:00+00:00' WHERE topic_id='T-5'")
    x.conn.execute("UPDATE publication_topics SET position=1 WHERE publication_id='PUB-TIE'")
    x.conn.execute("DELETE FROM figures WHERE figure_id='FG-GONE'")
    x.conn.execute("DELETE FROM components WHERE component_id='C-GONE'")
    x.conn.execute("UPDATE structured_tables SET columns_json='not json', rows_json='[1' WHERE table_id='TB-M'")
    x.conn.execute("UPDATE topics SET title='' WHERE topic_id='T-NT'")
    x.conn.execute("UPDATE publication_topics SET position=1 WHERE publication_id='PUB-STIE'")
    x.conn.execute("UPDATE topics SET source_lang='IT-it' WHERE topic_id='T-RAW'")
    x.conn.execute("UPDATE publications SET source_lang='IT_it' WHERE publication_id='PUB-RAW'")
    x.conn.execute("UPDATE components SET source_lang='en_us' WHERE component_id='C-RAW'")
    x.conn.execute("UPDATE topics SET archived_at='2026-01-01T00:00:00+00:00' WHERE topic_id IN ('T-AD','T-AA')")
    x.conn.execute("UPDATE content_blocks SET status='Apprové' WHERE block_id='B-ü1'")
    # legacy translation tags next to canonical ones (the set_* API would have canonicalized / removed them)
    x.conn.execute("INSERT INTO topic_translations(topic_id,target_lang,source_version,target_title,target_body,status,"
                   "updated_at) VALUES('T-LONE','EN_us',0,'','legacy','Approved','2026-01-01T00:00:00+00:00')")
    x.conn.execute("INSERT INTO block_translations(block_id,target_lang,source_version,target_text,status,updated_at) "
                   "VALUES('B-M','DE_de',1,'Hinweis (legacy).','Approved','2026-01-01T00:00:00+00:00')")
    x.conn.commit()
    x.close()
    raw = sqlite3.connect(path)                     # foreign keys are per connection and off by default
    raw.execute("INSERT INTO publication_topics(publication_id,topic_id,position) VALUES('PUB-GHOST','T-GHOST',2)")
    for bid, tid, btype, text, ref in (("B-ORPH", "A-ORPH", "table", "", "TB-L"),
                                       ("B-GH1", "T-GHOST", "paragraph", "Testo sotto un topic inesistente.", "")):
        raw.execute("INSERT INTO content_blocks(block_id,topic_id,block_type,position,text_content,object_ref_id,status,"
                    "created_at,updated_at) VALUES(?,?,?,1,?,?,'Approved','2026-01-01T00:00:00+00:00',"
                    "'2026-01-01T00:00:00+00:00')", (bid, tid, btype, text, ref))
    raw.commit()
    raw.close()
    return IndustrialRepository(path)


x = build_x()
x_token = {p: outcome(tim.publication_revision_token, x, p)
           for p in ("PUB-X", "PUB-TIE", "PUB-ST", "PUB-PUB", "PUB-TBL", "PUB-EN", "PUB-DG", "PUB-GHOST", "PUB-UTF",
                     "PUB-ARC", "PUB-STIE")}
_utf_rows = [["topic", "T-Sicurezza-è", int(x.conn.execute("SELECT version FROM topics WHERE topic_id='T-Sicurezza-è'").fetchone()[0]),
              "Approved"],
             ["block", "B-ü1", int(x.conn.execute("SELECT version FROM content_blocks WHERE block_id='B-ü1'").fetchone()[0]),
              "Apprové"]]


def _tok(rows, ascii_only):
    raw = json.dumps(rows, ensure_ascii=ascii_only, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


check("E1b: non-ASCII ids and status are serialized as UTF-8 (ensure_ascii=False) -- the token differs from the "
      "\\u-escaped form", x_token["PUB-UTF"] == ("ok", _tok(_utf_rows, False)) and _tok(_utf_rows, False) != _tok(_utf_rows, True),
      [x_token["PUB-UTF"], _tok(_utf_rows, False), _tok(_utf_rows, True)])
check("E1: dangling figure/component references and a link to a missing topic still produce a token",
      all(x_token[p][0] == "ok" for p in ("PUB-DG", "PUB-GHOST")), x_token)
X_UNITS = [("publication", "PUB-X"), ("publication", "PUB-TIE"), ("topic", "T-9"), ("topic", "T-1"), ("topic", "T-U"),
           ("topic", "T-NT"), ("block", "B-A"), ("block", "B-PM"), ("block", "B-RO"), ("table", "TB-E"),
           ("table", "TB-M"), ("figure", "FG-NA"), ("component", "C-NT"), ("block", "B-DGF"), ("block", "B-DGC"),
           ("publication", "PUB-DG"), ("publication", "PUB-GHOST"), ("block", "B-1A"), ("block", "B-ORPH"),
           ("block", "B-GH1")]
x_units = {}
for kind, oid in X_UNITS:
    res = outcome(tim.extract_source_units, x, oid, kind=kind)
    x_units[f"{kind}:{oid}"] = (res[0], units_of(res[1][0]), res[1][1]) if res[0] == "ok" else res
_xu = {k: [u[0] for u in v[1]] for k, v in x_units.items() if v[0] == "ok"}
_xt = {k: [u[0] for u in v[1] if u[1] == "topic_title"] for k, v in x_units.items() if v[0] == "ok"}   # topic titles only
check("E2: a publication follows position order (T-9, T-1, T-5) -- not id or insertion order -- and keeps the "
      "ARCHIVED topic T-5", _xt["publication:PUB-X"] == ["T-9:title", "T-1:title", "T-5:title"],
      _xu["publication:PUB-X"])
check("E3: blocks follow position, ties broken by block_id (B-A, B-M, B-Y, B-Z ...); every text block type is a unit; "
      "the topic body is ignored when the topic has blocks",
      _xu["topic:T-9"] == ["T-9:title", "B-A", "B-M", "B-Y", "B-Z", "B-Y2", "B-E1", "B-E2"], _xu["topic:T-9"])
check("E4: publication topics tied on position are broken by topic_id (PUB-TIE: T-1 before T-5)",
      _xt["publication:PUB-TIE"] == ["T-1:title", "T-5:title"], _xu["publication:PUB-TIE"])
check("E5 (pinned): archived blocks are NOT filtered -- B-1A and the table of archived B-1C are units of T-1",
      _xu["topic:T-1"][:2] == ["T-1:title", "B-1A"] and any(u.startswith("B-1C:") for u in _xu["topic:T-1"])
      and x_units["block:B-1A"][0] == "ok", _xu["topic:T-1"])
check("E6: a block's source_ref carries the BLOCK version (B-A#v3) while its topic is at v1",
      x_units["block:B-A"][1][0][4] == "B-A#v3" and x_units["topic:T-9"][2]["version"] == 1, x_units["block:B-A"])
check("E7: markup in a PLAIN block is kept verbatim, without the rich marker",
      x_units["block:B-PM"][1] == [("B-PM", "block", "B-PM", "Coppia M8*1.25 **non** formattata", "B-PM#v1")],
      x_units["block:B-PM"])
check("E7b (pinned quirk): a rich block with only markup ('** **') yields a unit whose visible text is a single "
      "SPACE, flagged with the rich marker (units_to_segments drops it later: no alphanumeric character)",
      x_units["block:B-RO"][0] == "ok" and [(u[3], tim.RICH_FORMAT_UNSAFE_MARKER in u[4]) for u in x_units["block:B-RO"][1]]
      == [(" ", True)], x_units["block:B-RO"])
check("E8: empty headers/cells/title/caption/alt_text/component title are skipped; malformed JSON falls back to []",
      _xu["table:TB-E"] == ["TB-E:header:2", "TB-E:r1c2"] and _xu["table:TB-M"] == ["TB-M:title"]
      and _xu["figure:FG-NA"] == ["FG-NA:title", "FG-NA:caption"] and _xu["component:C-NT"] == ["C-NT:body"]
      and _xu["topic:T-NT"] == ["T-NT"], [_xu.get(k) for k in ("table:TB-E", "table:TB-M", "figure:FG-NA", "component:C-NT", "topic:T-NT")])
check("E9: a block pointing at a missing figure/component raises KeyError, directly and through its publication",
      all(x_units[k][:2] == ("raise", "KeyError") for k in ("block:B-DGF", "block:B-DGC", "publication:PUB-DG")),
      [x_units[k] for k in ("block:B-DGF", "block:B-DGC", "publication:PUB-DG")])
check("E10: a publication linking a missing topic raises KeyError on extraction",
      x_units["publication:PUB-GHOST"][:2] == ("raise", "KeyError"), x_units["publication:PUB-GHOST"])
check("E10b (pinned): a block whose topic row is missing raises KeyError (the language JOIN finds no topic)",
      x_units["block:B-ORPH"][:2] == ("raise", "KeyError") and x_units["block:B-GH1"][:2] == ("raise", "KeyError"),
      [x_units["block:B-ORPH"], x_units["block:B-GH1"]])
x_status = {p: tim._publication_status(x, p) for p in ("PUB-X", "PUB-TIE", "PUB-ST", "PUB-PUB", "PUB-TBL", "PUB-GHOST",
                                                        "PUB-ARC", "PUB-ARC2", "PUB-STIE")}
check("E11: _publication_status: the first non-approved BLOCK of the first topic (PUB-ST -> 'In Review'); 'Published' "
      "counts as approved (-> 'Approved'); a Draft table is ignored; an archived Draft block still counts; a missing "
      "topic gives 'MISSING'",
      {k: x_status[k] for k in ("PUB-X", "PUB-TIE", "PUB-ST", "PUB-PUB", "PUB-TBL", "PUB-GHOST")}
      == {"PUB-X": "Draft", "PUB-TIE": "Draft", "PUB-ST": "In Review", "PUB-PUB": "Approved",
          "PUB-TBL": "Approved", "PUB-GHOST": "MISSING"}, x_status)
check("E11b: blocks are read in natural (position, rowid) order -- B-S0 (smaller id, inserted first, position 3, Draft) "
      "does not win over B-S1 (position 1, In Review)", x_status["PUB-ST"] == "In Review", x_status)
check("E11c (pinned): ARCHIVED topics count for the status: an archived Draft topic after a Published one -> 'Draft'; "
      "a publication of one archived Approved topic -> 'Approved' (not EMPTY/MISSING)",
      x_status["PUB-ARC"] == "Draft" and x_status["PUB-ARC2"] == "Approved", x_status)
x_langs = {f"{k}:{i}": outcome(tim._object_source_lang, x, i, k)
           for k, i in (("table", "TB-L"), ("figure", "TB-L"), ("figure", "FG-ORPHAN"), ("table", "TB-AR"),
                        ("block", "B-ZX"), ("publication", "PUB-EN"), ("component", "C-EN"), ("figure", "FG-SH"),
                        ("table", "FG-SH"), ("publication", "PUB-RAW"), ("component", "C-RAW"), ("block", "B-RW1"),
                        ("table", "TB-RAW"), ("figure", "FG-RAW"), ("table", "TB-ADONLY"), ("block", "B-ORPH"))}
x_meta = {f"{k}:{i}": outcome(tim._object_meta, x, i, k)
          for k, i in (("publication", "PUB-RAW"), ("component", "C-RAW"), ("block", "B-RW1"), ("table", "TB-RAW"),
                       ("figure", "FG-RAW"), ("block", "B-ORPH"))}
check("E12: a shared table takes the language of the referencing topic with the smallest topic_id (T-0, de-DE -- its "
      "block B-ZX sorts AFTER B-L1), ignores the FIGURE with the same id (A-FIG, fr-FR) and the referrer whose topic "
      "does not exist (A-ORPH); that figure gets fr-FR",
      x_langs["table:TB-L"] == ("ok", "de-DE") and x_langs["figure:TB-L"] == ("ok", "fr-FR"), x_langs)
check("E12b: the figure FG-SH takes fr-FR from its figure block, not es-ES from the TABLE block with the same id",
      x_langs["figure:FG-SH"] == ("ok", "fr-FR") and x_langs["table:FG-SH"] == ("ok", "es-ES"), x_langs)
check("E12c: stored non-canonical languages are canonicalized in every branch (publication 'IT_it', component 'en_us', "
      "block/table/figure via topic 'IT-it')",
      [x_langs[k] for k in ("publication:PUB-RAW", "component:C-RAW", "block:B-RW1", "table:TB-RAW", "figure:FG-RAW")]
      == [("ok", "it-IT"), ("ok", "en-US"), ("ok", "it-IT"), ("ok", "it-IT"), ("ok", "it-IT")], x_langs)
check("E12d (pinned): a table referenced only from an ARCHIVED topic takes that topic's language (es-ES); the orphan "
      "block's own language lookup raises KeyError",
      x_langs["table:TB-ADONLY"] == ("ok", "es-ES") and x_langs["block:B-ORPH"][:2] == ("raise", "KeyError"), x_langs)
check("E13: a block takes its topic's language, a publication and a component their own (not their topics')",
      x_langs["block:B-ZX"] == ("ok", "de-DE") and x_langs["publication:PUB-EN"] == ("ok", "en-US")
      and x_langs["component:C-EN"] == ("ok", "en-US") and x_langs["figure:FG-ORPHAN"] == ("ok", "")
      and x_langs["table:TB-AR"] == ("ok", "it-IT"), x_langs)
x_assets = {f"{k}:{i}@v{v}": tim._translation_assets(x, i, k, v)
            for k, i, v in (("block", "B-A", 3), ("table", "TB-L", 2), ("figure", "FG-NA", 1), ("topic", "T-LONE", 1),
                            ("block", "B-M", 1))}
check("E14: per-kind translations: several per object in language order, STALE after an update, record status kept",
      x_assets["block:B-A@v3"][["target_lang", "translation_status", "translation_record_status"]].values.tolist()
      == [["de-DE", "FRESH", "Approved"], ["fr-FR", "STALE", "Approved"]]
      and x_assets["table:TB-L@v2"]["translation_status"].tolist() == ["STALE"], x_assets["block:B-A@v3"])
check("E14b (pinned): a legacy tag is canonicalized on read and listed next to the canonical row, ordered by the RAW "
      "tag ('EN_us' before 'en-US', 'DE_de' alone)",
      x_assets["topic:T-LONE@v1"][["target_lang", "translation_status"]].values.tolist() == [["en-US", "STALE"], ["en-US", "FRESH"]]
      and x_assets["block:B-M@v1"]["target_lang"].tolist() == ["de-DE"], [x_assets["topic:T-LONE@v1"], x_assets["block:B-M@v1"]])
x_segs = tim.units_to_segments([u for u in tim.extract_source_units(x, "T-U", kind="topic")[0]])
check("E15: a unit without any alphanumeric character ('***') produces no segment",
      "***" not in x_segs[1]["source"].tolist(), x_segs[1]["source"].tolist())

# TM fixture for the edge analyses: a penalized exact match and a safe fuzzy match (LINGUISTIC_REVIEW),
# an approved exact match on a repeated long segment (AUTO_REUSE must not become INTRA_DOCUMENT_REUSE)
TM_X = make_tm("tm_edge.sqlite", [
    ("it-IT", "en-US", "Controllare il livello dell'olio.", "Check the oil level.", True),
    ("it-IT", "en-US", "Pericolo: alta tensione presente ora.", "Danger: high voltage present now.", True),
])
_tmx = TranslationMemory(TM_X)
try:
    _ctx_prev, _ctx_next = cat_normalize("Nota: verificare il serraggio."), cat_normalize("M8x1.25")
    _tmx.add("it-IT", "en-US", "Controllare il livello dell'olio.", "Check the oil level (in context).",
             _ctx_prev, _ctx_next, "ctx", 100.0, True, "context", 100, 0.0)
    _tmx.add("it-IT", "en-US", "Controllare il livello dell'olio.", "Check the oil level (swapped context).",
             _ctx_next, _ctx_prev, "ctx", 100.0, True, "context", 100, 0.0)
    _tmx.add("it-IT", "en-US", "Nota: verificare il serraggio.", "Note: check the tightening.", "", "", "", 55.0, True,
             "penalized", 7, 5.0)
finally:
    _tmx.close()
x_analysis = {
    "topic T-9": analysis("x_topic_T-9", x, "T-9", kind="topic", tm_paths=[TM_X], target_langs=["de-DE"], rate_card=RATES),
    "publication PUB-X": analysis("x_pub_PUB-X", x, "PUB-X", kind="publication", require_approved=False, target_langs=["en-US"]),
    "publication PUB-TIE": analysis("x_pub_PUB-TIE", x, "PUB-TIE", kind="publication", require_approved=False,
                                    target_langs=["en-US"]),
    "publication PUB-ST (refused)": analysis("x_pub_PUB-ST", x, "PUB-ST", kind="publication", target_langs=["en-US"]),
    "publication PUB-PUB (Published)": analysis("x_pub_PUB-PUB", x, "PUB-PUB", kind="publication", target_langs=["en-US"]),
    "block B-A": analysis("x_block_B-A", x, "B-A", kind="block"),
    "table TB-L": analysis("x_table_TB-L", x, "TB-L", kind="table"),
    "figure FG-NA": analysis("x_figure_FG-NA", x, "FG-NA", kind="figure"),
    "component '  Component '": analysis("x_comp", x, "C-EN", kind="  Component ", target_langs=["it-IT"]),
    "topic T-LONE": analysis("x_lone", x, "T-LONE", kind="topic", target_langs=["en-US"]),
    "block B-SYM (no segment)": analysis("x_sym", x, "B-SYM", kind="block", target_langs=["en-US"]),
    "topic T-P (Published)": analysis("x_topic_T-P", x, "T-P", kind="topic", target_langs=["en-US"]),
    "block B-P1 (Published)": analysis("x_block_B-P1", x, "B-P1", kind="block", target_langs=["en-US"]),
    "publication PUB-ARC (archived Draft topic)": analysis("x_pub_PUB-ARC", x, "PUB-ARC", kind="publication",
                                                           target_langs=["en-US"]),
    "topic T-LONE (legacy tag)": analysis("x_lone_tag", x, "T-LONE", kind="topic", target_langs=["en-US"]),
}
_x9 = x_analysis["topic T-9"]
_x9d = _x9[1]["detail"] if _x9[0] == "ok" else pd.DataFrame()
_x9en = _x9d[_x9d["target_lang"] == "en-US"] if not _x9d.empty else _x9d
check("E16: the penalized exact match and the safe fuzzy match land in LINGUISTIC_REVIEW, priced at rate_fuzzy",
      _x9[0] == "ok" and set(_x9en.loc[_x9en["work_bucket"] == "LINGUISTIC_REVIEW", "unit_id"]) == {"B-A", "B-M"}
      and set(_x9en.loc[_x9en["work_bucket"] == "LINGUISTIC_REVIEW", "rate_per_word"]) == {0.08},
      _x9en[["unit_id", "match_category", "work_bucket"]].values.tolist() if not _x9en.empty else _x9)
check("E16b: the TM entry whose context (prev/next) matches B-Y's neighbours gives CONTEXT MATCH; B-Y2 (other "
      "neighbours) gets a plain 100% MATCH",
      _x9[0] == "ok" and _x9en.loc[_x9en["unit_id"] == "B-Y", "match_category"].tolist() == ["CONTEXT MATCH"]
      and _x9en.loc[_x9en["unit_id"] == "B-Y2", "match_category"].tolist() == ["100% MATCH"],
      _x9en[["unit_id", "match_category"]].values.tolist() if not _x9en.empty else _x9)
check("E17: a repeated long segment with an approved exact TM match stays AUTO_REUSE twice; a repeated 24-letter word "
      "becomes INTRA_DOCUMENT_REUSE",
      _x9[0] == "ok" and _x9en.loc[_x9en["unit_id"].isin(["B-Y", "B-Y2"]), "work_bucket"].tolist() == ["AUTO_REUSE", "AUTO_REUSE"]
      and _x9en.loc[_x9en["unit_id"] == "B-E2", "work_bucket"].tolist() == ["INTRA_DOCUMENT_REUSE"],
      _x9en[["unit_id", "work_bucket"]].values.tolist() if not _x9en.empty else _x9)
_xp = x_analysis["publication PUB-X"]
check("E18: a multi-topic publication's impact map lists its topics in position order with their positions",
      _xp[0] == "ok" and _xp[1]["impact_map"][["topic_id", "position"]].values.tolist() == [["T-9", 1], ["T-1", 2], ["T-5", 3]]
      and _xp[1]["overall"]["affected_topics"] == 3, _xp if _xp[0] != "ok" else _xp[1]["impact_map"])
check("E19: a publication whose first non-approved block is In Review is refused with that status; a Published one passes",
      x_analysis["publication PUB-ST (refused)"][:2] == ("raise", "ValueError")
      and "status=In Review" in x_analysis["publication PUB-ST (refused)"][2]
      and x_analysis["publication PUB-PUB (Published)"][0] == "ok", x_analysis["publication PUB-ST (refused)"])
check("E20: block, table and figure analyses succeed; their CCMS translation status is reported (STALE included)",
      all(x_analysis[k][0] == "ok" for k in ("block B-A", "table TB-L", "figure FG-NA"))
      and dict(zip(x_analysis["block B-A"][1]["language_summary"]["target_lang"],
                   x_analysis["block B-A"][1]["language_summary"]["ccms_translation_status"])) == {"de-DE": "FRESH", "fr-FR": "STALE"}
      and x_analysis["table TB-L"][1]["overall"]["source_lang"] == "de-DE",
      {k: x_analysis[k][:3] for k in ("block B-A", "table TB-L", "figure FG-NA") if x_analysis[k][0] != "ok"})
check("E20b: an object whose OWN status is Published passes the gate (topic T-P, block B-P1)",
      all(x_analysis[k][0] == "ok" and x_analysis[k][1]["overall"]["source_status"] == "Published"
          for k in ("topic T-P (Published)", "block B-P1 (Published)")),
      {k: x_analysis[k][:3] for k in ("topic T-P (Published)", "block B-P1 (Published)") if x_analysis[k][0] != "ok"})
check("E20c (pinned): a publication with an archived Draft topic is refused (status=Draft)",
      x_analysis["publication PUB-ARC (archived Draft topic)"][:2] == ("raise", "ValueError")
      and "status=Draft" in x_analysis["publication PUB-ARC (archived Draft topic)"][2],
      x_analysis["publication PUB-ARC (archived Draft topic)"])
check("E20d (pinned): with a legacy 'EN_us' row next to 'en-US', the analysis reports one en-US language whose CCMS "
      "status comes from the LAST row in raw-tag order (FRESH)",
      x_analysis["topic T-LONE (legacy tag)"][0] == "ok"
      and x_analysis["topic T-LONE (legacy tag)"][1]["language_summary"][["target_lang", "ccms_translation_status"]]
      .values.tolist() == [["en-US", "FRESH"]], x_analysis["topic T-LONE (legacy tag)"][:3])
check("E21: analyze normalizes the kind ('  Component ' passes the approval gate as a component)",
      x_analysis["component '  Component '"][0] == "ok"
      and x_analysis["component '  Component '"][1]["overall"]["changed_kind"] == "component", x_analysis["component '  Component '"][:3])
check("E22: a topic in no publication: affected_publications 0, affected_topics 1 (fallback)",
      x_analysis["topic T-LONE"][0] == "ok" and (x_analysis["topic T-LONE"][1]["overall"]["affected_publications"],
                                                 x_analysis["topic T-LONE"][1]["overall"]["affected_topics"]) == (0, 1),
      x_analysis["topic T-LONE"][:3])
check("E23: a block whose only text has no alphanumeric character fails with 'Nessun segmento sorgente analizzabile'",
      x_analysis["block B-SYM (no segment)"][:3] == ("raise", "ValueError", "Nessun segmento sorgente analizzabile"),
      x_analysis["block B-SYM (no segment)"])
x.close()

# TM side with non-canonical stored tags (rows written with raw SQL, as legacy files may hold them) and non-TM files
TM_RAW = TM_DIR / "tm_raw.sqlite"
TranslationMemory(TM_RAW).close()                 # the real schema, then rows that add() would have canonicalized
_raw = sqlite3.connect(TM_RAW)
for sl, tl, src, tgt in (("IT_it", "EN_us", "Manutenzione", "Maintenance (legacy)"), ("it-IT", "de-de", "Pompa", "Pumpe"),
                         ("en_US", "fr-FR", "Maintenance", "Entretien")):
    _raw.execute("INSERT INTO tm_entries(source_lang,target_lang,source,target,source_norm,created_at,tm_name) "
                 "VALUES(?,?,?,?,?,?,?)", (sl, tl, src, tgt, src.casefold(), "2026-01-01T00:00:00+00:00", "legacy"))
_raw.commit()
_raw.close()
EMPTY_FILE = TM_DIR / "empty.sqlite"
EMPTY_FILE.write_bytes(b"")
NOT_TM = TM_DIR / "not_a_tm.sqlite"
_nt = sqlite3.connect(NOT_TM)
_nt.execute("CREATE TABLE other(x)")
_nt.commit()
_nt.close()
_nt_bytes, _empty_bytes = NOT_TM.read_bytes(), EMPTY_FILE.read_bytes()
_cons_raw = tim._consolidated_tm([TM_RAW, TM_MAIN], "it-IT", "en-US", TM_DIR / "_cons_raw.sqlite")
try:
    _cons_raw_rows = [dict(row) for row in _cons_raw.conn.execute("SELECT * FROM tm_entries ORDER BY rowid")]
finally:
    _cons_raw.close()
_cons_de = tim._consolidated_tm([TM_RAW], "it-IT", "de-DE", TM_DIR / "_cons_raw_de.sqlite")
try:
    _cons_de_rows = [dict(row) for row in _cons_de.conn.execute("SELECT * FROM tm_entries ORDER BY rowid")]
finally:
    _cons_de.close()
# a v0.12/v0.13 TM: no tm_name/priority/penalty columns, rollback journal, no indexes
TM_LEGACY = TM_DIR / "tm_legacy.sqlite"
_lg = sqlite3.connect(TM_LEGACY)
_lg.execute("PRAGMA journal_mode=DELETE")
_lg.execute("CREATE TABLE tm_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, source_lang TEXT NOT NULL, target_lang TEXT NOT NULL, "
            "source TEXT NOT NULL, target TEXT NOT NULL, source_norm TEXT NOT NULL, prev_norm TEXT DEFAULT '', next_norm TEXT DEFAULT '', "
            "numbers_json TEXT DEFAULT '[]', codes_json TEXT DEFAULT '[]', units_json TEXT DEFAULT '[]', origin TEXT DEFAULT '', "
            "alignment_confidence REAL DEFAULT 0, approved INTEGER DEFAULT 0, created_at TEXT NOT NULL)")
_lg.execute("INSERT INTO tm_entries(source_lang,target_lang,source,target,source_norm,origin,alignment_confidence,approved,created_at) "
            "VALUES('it-IT','en-US','Manutenzione','Maintenance (v0.12)','manutenzione','old',90,1,'2025-01-01T00:00:00+00:00')")
_lg.commit()
_lg.close()


def tm_schema(path):
    c = sqlite3.connect(path)
    try:
        return {"columns": [r[1] for r in c.execute("PRAGMA table_info(tm_entries)")],
                "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0],
                "indexes": sorted(r[1] for r in c.execute("PRAGMA index_list(tm_entries)"))}
    finally:
        c.close()


_legacy_before = tm_schema(TM_LEGACY)
_legacy_pairs = outcome(tim._tm_pairs, TM_LEGACY)
_legacy_after_pairs = tm_schema(TM_LEGACY)
_cons_lg = tim._consolidated_tm([TM_LEGACY], "it-IT", "en-US", TM_DIR / "_cons_legacy.sqlite")
try:
    _cons_lg_rows = [dict(row) for row in _cons_lg.conn.execute("SELECT * FROM tm_entries ORDER BY rowid")]
finally:
    _cons_lg.close()
_legacy_after_cons = tm_schema(TM_LEGACY)
x_tm = {
    "legacy schema before": _legacy_before, "legacy pairs": _legacy_pairs, "legacy schema after _tm_pairs": _legacy_after_pairs,
    "legacy consolidated": _cons_lg_rows, "legacy schema after _consolidated_tm": _legacy_after_cons,
    "consolidated raw it-IT->de-DE": _cons_de_rows,
    "pairs raw": outcome(tim._tm_pairs, TM_RAW),
    "discover raw it-IT": outcome(tim.discover_tm_languages, [TM_RAW], "it-IT"),
    "pairs empty file": outcome(tim._tm_pairs, EMPTY_FILE),
    "pairs file without tm_entries": outcome(tim._tm_pairs, NOT_TM),
    "consolidated raw+main it-IT->en-US": _cons_raw_rows,
}
check("E24: _tm_pairs returns the RAW stored tags; discovery canonicalizes them ('EN_us' -> 'en-US', 'de-de' -> 'de-DE') "
      "and matches the base source language of 'IT_it'",
      x_tm["pairs raw"][0] == "ok" and ("IT_it", "EN_us") in x_tm["pairs raw"][1]
      and x_tm["discover raw it-IT"][0] == "ok" and sorted(x_tm["discover raw it-IT"][1]) == ["de-DE", "en-US"], x_tm)
check("E25 (pinned): _consolidated_tm compares the target only case-insensitively -- 'EN_us' is NOT 'en-US', so the "
      "legacy row is dropped although discovery listed the file for en-US",
      not any(r["target"] == "Maintenance (legacy)" for r in _cons_raw_rows), [r["target"] for r in _cons_raw_rows])
check("E25b: the target comparison really is case-insensitive: the raw 'de-de' row is kept for de-DE",
      [r["target"] for r in _cons_de_rows] == ["Pumpe"], _cons_de_rows)
# a MIXED TM: the file qualifies for de-DE through its it->de row, but also holds an en->de row with the same target
TM_MIXED = make_tm("tm_mixed.sqlite", [("it-IT", "de-DE", "Avvertenza", "Warnhinweis", True),
                                       ("en-US", "de-DE", "Warning", "Warnung", True)])
_cons_mx = tim._consolidated_tm([TM_MIXED], "it-IT", "de-DE", TM_DIR / "_cons_mixed.sqlite")
try:
    _cons_mx_rows = [dict(row) for row in _cons_mx.conn.execute("SELECT * FROM tm_entries ORDER BY rowid")]
finally:
    _cons_mx.close()
check("E25e: the ROW-level source-language filter: in a mixed TM the en->de row is dropped, only the it->de row is "
      "consolidated (and re-labelled with the requested source language)",
      [(r["source"], r["source_lang"], r["target_lang"]) for r in _cons_mx_rows] == [("Avvertenza", "it-IT", "de-DE")],
      _cons_mx_rows)
check("E25c: _tm_pairs reads a legacy v0.12 TM without touching it (rollback journal, no new column, no index)",
      _legacy_pairs[0] == "ok" and _legacy_after_pairs == _legacy_before, [_legacy_before, _legacy_after_pairs])
check("E25d (pinned): _consolidated_tm opens each SOURCE TM through TranslationMemory, which MIGRATES a legacy file in "
      "place (adds tm_name/priority/penalty, switches to WAL, creates the two indexes); its rows come out with the "
      "column defaults (tm_name 'Project TM', priority 100)",
      _legacy_after_cons["columns"][-3:] == ["tm_name", "priority", "penalty"] and _legacy_after_cons["journal_mode"] == "wal"
      and _legacy_after_cons["indexes"] == ["ix_tm_norm", "ix_tm_profile"]
      and [(r["source"], r["tm_name"], r["priority"]) for r in _cons_lg_rows] == [("Manutenzione", "tm_legacy:Project TM", 100)],
      [_legacy_after_cons, [(r["source"], r["tm_name"], r["priority"]) for r in _cons_lg_rows]])
check("E27: an existing file that is not a TM raises sqlite3.OperationalError and is left byte-for-byte unchanged",
      x_tm["pairs empty file"][:2] == ("raise", "OperationalError") and x_tm["pairs file without tm_entries"][:2]
      == ("raise", "OperationalError") and NOT_TM.read_bytes() == _nt_bytes and EMPTY_FILE.read_bytes() == _empty_bytes, x_tm)
_cons_pen = tim._consolidated_tm([TM_X], "it-IT", "en-US", TM_DIR / "_cons_pen.sqlite")
try:
    _pen = [dict(row) for row in _cons_pen.conn.execute("SELECT * FROM tm_entries WHERE source=?", ("Nota: verificare il serraggio.",))]
finally:
    _cons_pen.close()
check("E26: a penalized TM entry keeps priority/penalty/alignment through consolidation; an empty origin becomes the file name",
      len(_pen) == 1 and (_pen[0]["priority"], _pen[0]["penalty"], _pen[0]["alignment_confidence"], _pen[0]["origin"],
                          _pen[0]["tm_name"]) == (7, 5.0, 55.0, "tm_edge.sqlite", "tm_edge:penalized"), _pen)
x_tm["consolidated penalized"] = _pen
_cons_ctx = tim._consolidated_tm([TM_X], "it-IT", "en-US", TM_DIR / "_cons_ctx.sqlite")
try:
    _ctx_rows = [dict(row) for row in _cons_ctx.conn.execute("SELECT * FROM tm_entries WHERE tm_name LIKE '%context' ORDER BY rowid")]
finally:
    _cons_ctx.close()
check("E26b: two entries differing only by context survive consolidation with their prev/next norms intact",
      [(r["prev_norm"], r["next_norm"]) for r in _ctx_rows] == [(_ctx_prev, _ctx_next), (_ctx_next, _ctx_prev)], _ctx_rows)
x_tm["consolidated context"] = _ctx_rows
x_tm["consolidated mixed it-IT->de-DE"] = _cons_mx_rows
snap("E_edge_token_units_status_language", {"token": x_token, "units": x_units, "status": x_status, "langs": x_langs,
                                            "meta": x_meta, "assets": x_assets, "segments": x_segs})
snap("E_edge_analyses", x_analysis)
snap("E_edge_translation_memory", x_tm)

# ============================================================ MS: the digests see what matters, and only that
tok_ref = {"PUB-A": TOKEN_A, "matrix": token_matrix, "edges": token_edges}


def changed(obj, fn) -> bool:
    m = copy.deepcopy(obj)
    fn(m)
    return digest(m) != digest(obj)


check("MS1: a token one unit off changes the T digest", changed(tok_ref, lambda m: m.update({"PUB-A": TOKEN_A + 1})), "")
check("MS2: a mutation that stops changing the token changes the T digest",
      changed(tok_ref, lambda m: m["matrix"]["topic_version"].update(after=TOKEN_A)), "")
check("MS3: the rich marker dropped from source_ref ALONE (same text) changes the U digest",
      changed(extracted, lambda m: m.__setitem__("block:B-05", ("ok", [("B-05", "block", "B-05",
                                                                        "Usare sempre i guanti di protezione.", "B-05#v1")],
                                                                 m["block:B-05"][2]))), "")
check("MS4: FRESH turned into STALE changes the A digest",
      changed({"a": assets["topic:T-A@v2"].to_dict(orient="records")},
              lambda m: m["a"][0].update(translation_status="STALE")), "")
check("MS5: a timestamp is normalized -- a different created_at alone leaves the M digest unchanged",
      not changed({"m": meta["topic:T-A"][1]}, lambda m: m["m"].update(created_at="2000-01-01T00:00:00+00:00")), "")
check("MS5b: ... while any other field (the body, the version) changes it",
      changed({"m": meta["topic:T-A"][1]}, lambda m: m["m"].update(body="altro"))
      and changed({"m": meta["topic:T-A"][1]}, lambda m: m["m"].update(version=3)), "")

check("N1: no network access attempted", not GUARD.snapshot(), GUARD.snapshot())

# Pinned at HEAD cf7dfa2 (CP5.3a) after three identical runs.
GOLDEN = {
    'A_translation_assets': '918e0c04a3addf3e260163f58d712bc9bae6e0e0bff31bbe7c710df2f09ba03f',
    'E_edge_analyses': '4f1a3dcb00761551993bcb364f4fd88b3a26836987428d8ca8bcca3268512f50',
    'E_edge_token_units_status_language': '83754bde4aaa57aa9d62e579c9ab7a2cf785853d53cf5a3951df2d406b5fdb0f',
    'E_edge_translation_memory': '8514ede7c32285cf4d35ea1fcfbd142461a9a611c67605821fe4edd3cb29bc85',
    'I_analyze_component': '5f7456292274673f732b29e515fc43bcad88d16083056794c4e790807a0756f6',
    'I_analyze_default_out_dir': 'b45d6f132961e6bbba4aae831aa6c61ac5cd1d472325d94f0776ba41ce654009',
    'I_analyze_errors': '809154bf96b6e1f2cc5abd919c31756a4a2e11b2a0c467b5b33e00731fc60608',
    'I_analyze_publication': '7b51d42bc3fc89b51788c6057bd7cfd8e5e98dffe4765ac4f119b41ce0d90684',
    'I_analyze_topic': 'ce30178bcc5023cdfddaf27957c6ac07350c113302a0483cb2f3e5061068f559',
    'M_meta_language_status': '8ec24dbef8668feb1aa9a35816e6a97efb850659c2a62728abf35b9d87c63b75',
    'S_units_to_segments': '61096cd602cf4e7b50962390aa2c5c65d7ed0c569a74e308d8e518e11fbc981c',
    'TM_translation_memory_side': 'f1b870502768ecb3033ef28d8213bd73b2315785e18ad33092258e85eeb3fdcf',
    'T_publication_revision_token': '5b6357759338873f4aaabba3d7a17b49a1fd5df8d5bfc2ddabe624777aa7a824',
    'U_extract_errors': '6b5a79eb66f631c80078eb0b132ea43a330a23cc5a1a4ef58dbf79167e446069',
    'U_extract_source_units': 'aa56d913cd76fa18df467e288a8183d2c952295c145e8da2c54236d8e7bf741b',
}

if PRINT_GOLDEN:
    print("GOLDEN = {")
    for k in sorted(_observed):
        print(f"    {k!r}: {_observed[k]!r},")
    print("}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

for label in sorted(_observed):
    check(f"SNAP {label}: identical to the CP5.3a baseline", GOLDEN.get(label) == _observed[label],
          f"expected {GOLDEN.get(label)} got {_observed[label]}")
check("SNAP: every pinned output was observed", set(GOLDEN) == set(_observed), sorted(set(GOLDEN) ^ set(_observed)))

shutil.rmtree(TMP, ignore_errors=True)
check("T_cleanup: the temporary fixtures were removed", not TMP.exists(), TMP)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_TRANSLATION_IMPACT.json").write_text(json.dumps({
    "suite": "translation impact (CP5.3a)", "tests": total, "pass": passed, "fail": total - passed, "digests": _observed,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
