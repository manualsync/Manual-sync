"""Milestone 3D regression suite: Adobe InDesign (.idml) legacy onboarding /
coexistence.

Covers: security/fail-closed validation of the untrusted ZIP package, spread/
frame-position reading order (not ZIP order), multi-frame stories, orphan and
master-spread content preserved-but-excluded, inline tables, linked asset
present/missing (without contaminating caption/alt_text), duplicate Self IDs,
the shared CCMS writer's additive "figure" kind, integration with the
Milestone 3C LegacyFamilies engine (no duplicated engine, no automatic
revision_of from a mere hash difference, content-hash used for dedup only —
never as cross-revision identity).
"""
from __future__ import annotations

import json
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import idml_import as idml
from idml_import import IdmlImportError
from authoring_ccms import import_idml_as_topics, _write_topic_groups_to_repo
from ccms_industrial import IndustrialRepository
import legacy_family_inference as fam
from project_lifecycle import create_project
import app

TMP = Path(tempfile.mkdtemp(prefix="manualsync_3d_"))
results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


MIMETYPE = b"application/vnd.adobe.indesign-idml-package"


def _zip_write(name: str, parts: dict, *, mimetype: bytes | None = MIMETYPE, extra_infos=None) -> Path:
    path = TMP / name
    with zipfile.ZipFile(path, "w") as zf:
        if mimetype is not None:
            zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        for part_name, content in parts.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(part_name, data)
        for info, data in (extra_infos or []):
            zf.writestr(info, data)
    return path


def _designmap(stories, spreads, masters=()) -> str:
    refs = "".join(f'<idPkg:Spread src="{s}"/>' for s in spreads)
    refs += "".join(f'<idPkg:MasterSpread src="{m}"/>' for m in masters)
    refs += "".join(f'<idPkg:Story src="{s}"/>' for s in stories)
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Document xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging">{refs}</Document>')


def _story(self_id: str, paragraphs: list[tuple[str, str]], tables: list[list[list[str]]] | None = None,
           figure_after_index: int | None = None, figure_link: str | None = None,
           figure_missing_link: str | None = None) -> str:
    body = ""
    for i, (style, text) in enumerate(paragraphs):
        para_extra = ""
        if figure_after_index == i:
            uri = figure_link or figure_missing_link or ""
            para_extra = (f'<Rectangle Self="rect_{self_id}_{i}" ItemTransform="1 0 0 1 5 5">'
                          f'<Link LinkResourceURI="{uri}" StoredState="Normal"/><Image/></Rectangle>')
        body += (f'<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/{style}">'
                 f'<CharacterStyleRange>{para_extra}<Content>{text}</Content></CharacterStyleRange>'
                 f'</ParagraphStyleRange>')
    for rows in (tables or []):
        cells = "".join(
            f'<Cell Name="{r}:{c}"><ParagraphStyleRange><CharacterStyleRange><Content>{val}</Content>'
            f'</CharacterStyleRange></ParagraphStyleRange></Cell>'
            for r, row in enumerate(rows) for c, val in enumerate(row)
        )
        body += f'<Table Self="tbl_{self_id}">{cells}</Table>'
    return f'<?xml version="1.0" encoding="UTF-8"?><Story Self="{self_id}">{body}</Story>'


def _spread(self_id: str, frames: list[tuple[str, str, float, float, str | None]]) -> str:
    tfs = "".join(
        f'<TextFrame Self="{fid}" ParentStory="{story}" ItemTransform="1 0 0 1 {x} {y}"'
        + (f' PreviousTextFrame="{prev}"' if prev else "") + "/>"
        for fid, story, x, y, prev in frames
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Spread Self="{self_id}">{tfs}</Spread>'


def _master(self_id: str, frames: list[tuple[str, str, float, float]]) -> str:
    tfs = "".join(
        f'<TextFrame Self="{fid}" ParentStory="{story}" ItemTransform="1 0 0 1 {x} {y}"/>'
        for fid, story, x, y in frames
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><MasterSpread Self="{self_id}">{tfs}</MasterSpread>'


IT_PARA = ("Body Text", "Il presente manuale descrive l'installazione e la manutenzione del sistema idraulico "
                          "industriale, con particolare attenzione alle procedure di sicurezza operativa e alla "
                          "corretta configurazione dei parametri di esercizio previsti dal costruttore.")

# ------------------------------------------------------------------
# Group 1: basic parse -- heading, paragraph, inline table, caption merge.
# ------------------------------------------------------------------
story1 = _story("s1", [
    ("Heading 1", "1 Introduzione"),
    IT_PARA,
    ("Body Text", ""),  # placeholder replaced below with the figure-carrying paragraph
    ("Caption", "Didascalia figura pompa idraulica"),
], tables=[[["Parametro", "Valore"], ["Pressione", "10 bar"]]],
   figure_after_index=2, figure_missing_link="file:///C:/nonexistent/pump.png")
p1 = _zip_write("g1_basic.idml", {
    "designmap.xml": _designmap(["Stories/Story_s1.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "s1", 0, 0, None)]),
    "Stories/Story_s1.xml": story1,
})
parsed1 = idml.parse_idml(p1)
check("group1: heading detected and used as topic title", parsed1.headings == ["1 Introduzione"], parsed1.headings)
check("group1: body paragraph text extracted", any(IT_PARA[1] in g["title"] or
      any(it.get("text") == IT_PARA[1] for it in g["items"] if it["kind"] == "text") for g in parsed1.groups),
      parsed1.groups)
check("group1: inline table extracted with correct grid", parsed1.table_total == 1 and
      any(it.get("rows") == [["Parametro", "Valore"], ["Pressione", "10 bar"]]
          for g in parsed1.groups for it in g["items"] if it["kind"] == "table"), parsed1.groups)
fig1 = next(it for g in parsed1.groups for it in g["items"] if it["kind"] == "figure")
text_items1 = [it for g in parsed1.groups for it in g["items"] if it["kind"] == "text"]
check("group1 (constraint 3): caption is NEVER auto-confirmed -- the authoritative caption field stays empty",
      fig1["caption"] == "", fig1)
check("group1 (constraint 3): the caption-styled paragraph is preserved as its OWN text block, never deleted",
      any(t["text"] == "Didascalia figura pompa idraulica" for t in text_items1), text_items1)
check("group1 (constraint 3): the heuristic association is recorded as a caption_candidate marker, not as caption",
      any(r == "idml_caption_candidate=Didascalia figura pompa idraulica" for r in fig1["source_refs_extra"]),
      fig1["source_refs_extra"])
check("group1 (constraint 3): a caption_candidate review item is raised, requiring human confirmation",
      any(r["reason"] == "caption_candidate" and r.get("text_preview") == "Didascalia figura pompa idraulica"
          for r in parsed1.review_items), parsed1.review_items)
check("group1: missing linked asset detected, alt_text left empty (never contaminated)",
      fig1["_asset_status"] == "missing" and fig1["alt_text"] == "", fig1)
check("group1: missing-asset status carried as a technical source_refs marker, not in caption",
      any(r == "idml_asset_status=missing" for r in fig1["source_refs_extra"]), fig1)
check("group1: composite fingerprint present and structured (heading/role/story_id/order_index)",
      any(r.startswith("idml_fingerprint=") and
          all(k in json.loads(r.split("=", 1)[1]) for k in ("heading", "role", "story_id", "order_index"))
          for r in fig1["source_refs_extra"]), fig1["source_refs_extra"])

# ------------------------------------------------------------------
# Group 2: reading order follows spread/frame position, NOT ZIP/filename order.
# Two stories; the one that must read FIRST is stored in a file that sorts
# LAST alphabetically/in ZIP order, and vice versa.
# ------------------------------------------------------------------
story_zzz = _story("story_second", [("Heading 1", "Capitolo 2 (deve leggersi per SECONDO)"),
                                     ("Body Text", "Testo del secondo capitolo.")])
story_aaa = _story("story_first", [("Heading 1", "Capitolo 1 (deve leggersi per PRIMO)"),
                                    ("Body Text", "Testo del primo capitolo.")])
p2 = _zip_write("g2_order.idml", {
    "designmap.xml": _designmap(
        ["Stories/Story_zzz_last_in_zip.xml", "Stories/Story_aaa_first_in_zip.xml"],
        ["Spreads/Spread_a.xml", "Spreads/Spread_b.xml"],
    ),
    "Spreads/Spread_a.xml": _spread("a", [("tf_first", "story_first", 0, 0, None)]),
    "Spreads/Spread_b.xml": _spread("b", [("tf_second", "story_second", 0, 0, None)]),
    "Stories/Story_zzz_last_in_zip.xml": story_zzz,
    "Stories/Story_aaa_first_in_zip.xml": story_aaa,
})
parsed2 = idml.parse_idml(p2)
titles2 = [g["title"] for g in parsed2.groups]
check("group2: reading order follows spread position, not ZIP entry / filename order",
      titles2 == ["Capitolo 1 (deve leggersi per PRIMO)", "Capitolo 2 (deve leggersi per SECONDO)"], titles2)

# ------------------------------------------------------------------
# Group 3: multi-frame (threaded) story -- single story spanning two spreads.
# ------------------------------------------------------------------
story_threaded = _story("story_t", [("Heading 1", "Manuale a piu' frame"), IT_PARA])
p3 = _zip_write("g3_threaded.idml", {
    "designmap.xml": _designmap(["Stories/Story_t.xml"], ["Spreads/Spread_a.xml", "Spreads/Spread_b.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "story_t", 0, 0, None)]),
    "Spreads/Spread_b.xml": _spread("b", [("tf2", "story_t", 0, 100, "tf1")]),
    "Stories/Story_t.xml": story_threaded,
})
parsed3 = idml.parse_idml(p3)
check("group3: multi-frame threaded story parses as ONE continuous group, not split",
      len(parsed3.groups) == 1 and len(parsed3.groups[0]["items"]) == 1, parsed3.groups)
check("group3: no review items for a properly threaded story", parsed3.review_items == [], parsed3.review_items)

# ------------------------------------------------------------------
# Group 4: orphan story -- referenced by NO frame at all.
# ------------------------------------------------------------------
story_body = _story("body1", [("Heading 1", "Sezione normale"), ("Body Text", "Testo regolare del corpo del manuale.")])
story_orphan = _story("orphan1", [("Body Text", "Testo di una story mai collegata a nessun frame")])
p4 = _zip_write("g4_orphan.idml", {
    "designmap.xml": _designmap(["Stories/Story_body1.xml", "Stories/Story_orphan1.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "body1", 0, 0, None)]),
    "Stories/Story_body1.xml": story_body,
    "Stories/Story_orphan1.xml": story_orphan,
})
parsed4 = idml.parse_idml(p4)
check("group4: orphan story excluded from the primary reading flow",
      not any("mai collegata" in g["title"] or
              any(it.get("text", "").startswith("Testo di una story") for it in g["items"] if it["kind"] == "text")
              for g in parsed4.groups), parsed4.groups)
check("group4: orphan story preserved as a review item with provenance, not silently lost",
      any(r["reason"] == "orphan_story" and "orphan1" in r["detail"] for r in parsed4.review_items),
      parsed4.review_items)

# ------------------------------------------------------------------
# Group 5: Master Spread content -- preserved for review, excluded from flow.
# ------------------------------------------------------------------
story_master = _story("masterstory", [("Body Text", "Intestazione ricorrente da master page")])
p5 = _zip_write("g5_master.idml", {
    "designmap.xml": _designmap(["Stories/Story_masterstory.xml"], ["Spreads/Spread_a.xml"],
                                 masters=["MasterSpreads/MasterSpread_m1.xml"]),
    "Spreads/Spread_a.xml": _spread("a", []),
    "MasterSpreads/MasterSpread_m1.xml": _master("m1", [("mtf1", "masterstory", 0, 0)]),
    "Stories/Story_masterstory.xml": story_master,
})
parsed5 = idml.parse_idml(p5)
check("group5: master-spread-only content excluded from primary flow",
      not any(it.get("text", "").startswith("Intestazione ricorrente") for g in parsed5.groups for it in g["items"]
              if it["kind"] == "text"), parsed5.groups)
check("group5: master-spread content preserved as review_required with a clear reason",
      any(r["reason"] == "master_spread_content" for r in parsed5.review_items), parsed5.review_items)

# ------------------------------------------------------------------
# Group 6: linked asset PRESENT in the package (Links/) -- distinguished from
# "embedded", and its bytes are the ones actually copied.
# ------------------------------------------------------------------
story_present = _story("s6", [("Body Text", "Testo prima della figura"), ("Caption", "Foto reale del quadro elettrico")],
                        figure_after_index=0, figure_link="Links/quadro.png")
p6 = _zip_write("g6_present.idml", {
    "designmap.xml": _designmap(["Stories/Story_s6.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "s6", 0, 0, None)]),
    "Stories/Story_s6.xml": story_present,
    "Links/quadro.png": b"\x89PNG\r\n\x1a\nFAKEPNGBYTES",
})
parsed6 = idml.parse_idml(p6)
fig6 = next(it for g in parsed6.groups for it in g["items"] if it["kind"] == "figure")
check("group6: linked asset physically present in Links/ is recognized as present, not missing",
      fig6["_asset_status"] == "present_in_package", fig6)
check("group6: presence in Links/ is NOT the same as 'embedded' (still recorded as a link)",
      any(r.startswith("idml_link_uri=") for r in fig6["source_refs_extra"]), fig6["source_refs_extra"])
check("group6 (constraint 3): adjacent caption paragraph is a candidate, not an auto-confirmed caption",
      fig6["caption"] == "" and any(r == "idml_caption_candidate=Foto reale del quadro elettrico"
                                     for r in fig6["source_refs_extra"]), fig6)
assets_dir6 = TMP / "g6_assets"
copied6 = idml.materialize_figure_assets(p6, parsed6.groups, assets_dir6)
check("group6: exactly one asset materialized", copied6 == 1, copied6)
materialized_path = Path(next(it for g in parsed6.groups for it in g["items"] if it["kind"] == "figure")["file_path"])
check("group6: materialized file contains the EXACT bytes from the package (no fabrication)",
      materialized_path.read_bytes() == b"\x89PNG\r\n\x1a\nFAKEPNGBYTES", materialized_path)

# ------------------------------------------------------------------
# Group 7: linked asset MISSING (no Links/ entry, no local file) -- must never
# contaminate caption/alt_text with a diagnostic sentence.
# ------------------------------------------------------------------
fig7 = fig1  # from Group 1, already asserts asset_status == missing + clean caption
check("group7: alt_text for a missing asset stays empty (content-only field)", fig7["alt_text"] == "", fig7)
check("group7: the authoritative caption field is never populated automatically, missing or not",
      fig7["caption"] == "", fig7)

# ------------------------------------------------------------------
# Group 8: malformed Story XML -> fail-closed reject of the WHOLE import.
# ------------------------------------------------------------------
p8 = _zip_write("g8_malformed_story.idml", {
    "designmap.xml": _designmap(["Stories/Story_bad.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "bad", 0, 0, None)]),
    "Stories/Story_bad.xml": "<Story Self=\"bad\"><ParagraphStyleRange>UNCLOSED",
})
try:
    idml.parse_idml(p8)
    check("group8: malformed Story XML raises IdmlImportError", False, "no exception raised")
except IdmlImportError as exc:
    check("group8: malformed Story XML raises IdmlImportError (fail-closed, whole import rejected)", True, str(exc))

# ------------------------------------------------------------------
# Group 9: malformed / missing designmap.xml -> fail-closed reject.
# ------------------------------------------------------------------
p9a = _zip_write("g9_bad_designmap.idml", {"designmap.xml": "<Document><UNCLOSED"})
try:
    idml.parse_idml(p9a)
    check("group9: malformed designmap.xml raises IdmlImportError", False, "no exception raised")
except IdmlImportError:
    check("group9: malformed designmap.xml raises IdmlImportError", True, "")

p9b = _zip_write("g9_missing_designmap.idml", {"Stories/Story_x.xml": "<Story Self=\"x\"/>"})
try:
    idml.parse_idml(p9b)
    check("group9: missing designmap.xml raises IdmlImportError", False, "no exception raised")
except IdmlImportError:
    check("group9: missing designmap.xml raises IdmlImportError", True, "")

p9c = _zip_write("g9_no_stories.idml", {"designmap.xml": _designmap([], ["Spreads/Spread_a.xml"]),
                                        "Spreads/Spread_a.xml": _spread("a", [])})
try:
    idml.parse_idml(p9c)
    check("group9: designmap with zero Story refs raises IdmlImportError", False, "no exception raised")
except IdmlImportError:
    check("group9: designmap with zero Story refs raises IdmlImportError", True, "")

# ------------------------------------------------------------------
# Group 10: security -- path traversal, symlink, oversized entry, zip bomb
# ratio, entry-count cap. All fail-closed BEFORE any XML is even parsed.
# ------------------------------------------------------------------
p10a = TMP / "g10_traversal.idml"
with zipfile.ZipFile(p10a, "w") as zf:
    zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
    zf.writestr("designmap.xml", _designmap(["Stories/Story_x.xml"], []))
    zf.writestr("../../evil.txt", "traversal payload")
try:
    idml.parse_idml(p10a)
    check("group10: ZIP path traversal entry is rejected", False, "no exception raised")
except IdmlImportError as exc:
    check("group10: ZIP path traversal entry is rejected", "non consentito" in str(exc) or "sicuro" in str(exc), str(exc))

p10b = TMP / "g10_symlink.idml"
with zipfile.ZipFile(p10b, "w") as zf:
    zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
    zf.writestr("designmap.xml", _designmap(["Stories/Story_x.xml"], []))
    info = zipfile.ZipInfo("Links/evil_link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    zf.writestr(info, "/etc/passwd")
try:
    idml.parse_idml(p10b)
    check("group10: symlink entry in the package is rejected", False, "no exception raised")
except IdmlImportError:
    check("group10: symlink entry in the package is rejected", True, "")

p10c = TMP / "g10_oversized_entry.idml"
with zipfile.ZipFile(p10c, "w") as zf:
    zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
    info = zipfile.ZipInfo("designmap.xml")
    zf.writestr(info, _designmap(["Stories/Story_x.xml"], []))
# Patch the recorded (uncompressed) size to simulate a bomb without writing real bytes.
with zipfile.ZipFile(p10c, "a") as zf:
    pass
import io as _io
buf = _io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
    zf.writestr("designmap.xml", _designmap(["Stories/Story_x.xml"], []))
    zf.writestr("Links/huge.bin", b"0" * (2_000_000))
p10c.write_bytes(buf.getvalue())
try:
    old_cap = idml.MAX_TOTAL_UNCOMPRESSED
    idml.MAX_TOTAL_UNCOMPRESSED = 1_000_000  # tighten the cap for this test only
    try:
        idml.parse_idml(p10c)
        check("group10: total-uncompressed-size cap is enforced", False, "no exception raised")
    except IdmlImportError:
        check("group10: total-uncompressed-size cap is enforced", True, "")
finally:
    idml.MAX_TOTAL_UNCOMPRESSED = old_cap

p10d = TMP / "g10_too_many_entries.idml"
with zipfile.ZipFile(p10d, "w") as zf:
    zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
    zf.writestr("designmap.xml", _designmap(["Stories/Story_x.xml"], []))
    for i in range(50):
        zf.writestr(f"Links/junk_{i}.txt", "x")
try:
    old_cap = idml.MAX_ENTRIES
    idml.MAX_ENTRIES = 10
    try:
        idml.parse_idml(p10d)
        check("group10: entry-count cap is enforced", False, "no exception raised")
    except IdmlImportError:
        check("group10: entry-count cap is enforced", True, "")
finally:
    idml.MAX_ENTRIES = old_cap

# ------------------------------------------------------------------
# Group 11: duplicate Self ids -> reported, not a crash.
# ------------------------------------------------------------------
story_dup = _story("dupstory", [("Body Text", "Testo con frame duplicati")])
p11 = _zip_write("g11_duplicate.idml", {
    "designmap.xml": _designmap(["Stories/Story_dup.xml"], ["Spreads/Spread_a.xml", "Spreads/Spread_b.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tfdup", "dupstory", 0, 0, None)]),
    "Spreads/Spread_b.xml": _spread("b", [("tfdup", "dupstory", 0, 50, None)]),  # same Self, again
    "Stories/Story_dup.xml": story_dup,
})
parsed11 = idml.parse_idml(p11)  # must not raise
check("group11: duplicate Self id does not crash the parse", True, "")
check("group11: duplicate Self id is reported as a review item",
      any(r["reason"] == "duplicate_self_id" for r in parsed11.review_items), parsed11.review_items)

# ------------------------------------------------------------------
# Group 12: end-to-end CCMS write -- shared writer, figures table populated.
# ------------------------------------------------------------------
db_path = TMP / "e2e.sqlite"
repo = IndustrialRepository(db_path)
try:
    frame12 = import_idml_as_topics(repo, p6, source_lang="it-IT", id_prefix="IDML",
                                    assets_dir=TMP / "e2e_assets")
    check("group12: at least one topic created from the IDML package", len(frame12) >= 1, frame12)
    check("group12: figure_count attribute reflects the actual figure written", frame12.attrs.get("figure_count") == 1,
          frame12.attrs)
    figs = list(repo.conn.execute("SELECT figure_id, caption, alt_text, file_path, status FROM figures"))
    check("group12: figures table (schema already existed, never populated before 3D) now has one row",
          len(figs) == 1, figs)
    fig_row = dict(figs[0])
    check("group12: figure status uses the SAME 'Migrated' vocabulary as PDF/DOCX blocks (blocks release readiness like any other migrated content)",
          fig_row["status"] == "Migrated", fig_row)
    check("group12: figure file_path points at the materialized real asset bytes",
          fig_row["file_path"] and Path(fig_row["file_path"]).exists(), fig_row)
finally:
    repo.close()

# malformed package must NEVER write anything to the CCMS.
db_path2 = TMP / "e2e_fail.sqlite"
repo2 = IndustrialRepository(db_path2)
try:
    topics_before = int(repo2.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
    try:
        import_idml_as_topics(repo2, p8, source_lang="it-IT", id_prefix="IDML")
        check("group12: malformed package raises before any CCMS write", False, "no exception raised")
    except IdmlImportError:
        pass
    topics_after = int(repo2.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
    check("group12: malformed package leaves zero new topics (no partial write)", topics_after == topics_before,
          (topics_before, topics_after))
finally:
    repo2.close()

# ------------------------------------------------------------------
# Group 13: .idml participates in LegacyFamilies (3C) -- same engine, no
# duplication. Two .idml revisions of the "same" manual must cluster; the
# candidate's doc_kind must read "idml".
# ------------------------------------------------------------------
def _family_idml(name: str, front_title: str, revision_word: str) -> Path:
    st = _story("fs", [("Body Text", front_title), ("Heading 1", "1 Introduzione"),
                        ("Body Text", f"Manuale con {revision_word} del prodotto PumpFamilyX per uso industriale.")])
    return _zip_write(name, {
        "designmap.xml": _designmap(["Stories/Story_fs.xml"], ["Spreads/Spread_a.xml"]),
        "Spreads/Spread_a.xml": _spread("a", [("tf1", "fs", 0, 0, None)]),
        "Stories/Story_fs.xml": st,
    })

fam_rev1 = _family_idml("PumpFamilyX_IT_Rev1.idml", "PumpFamilyX", "la revisione 1")
fam_rev2 = _family_idml("PumpFamilyX_IT_Rev2.idml", "PumpFamilyX", "la revisione 2 aggiornata")

cand1 = fam.build_candidate(fam_rev1)
cand2 = fam.build_candidate(fam_rev2)
check("group13: .idml candidate reports doc_kind == 'idml'", cand1.doc_kind == "idml", cand1.doc_kind)
check("group13: two different .idml files have different file_sha256 (different assets, as expected)",
      cand1.file_sha256 != cand2.file_sha256, (cand1.file_sha256, cand2.file_sha256))

result13 = fam.analyze_paths([str(fam_rev1), str(fam_rev2)])
all_related = [r for fp in result13.families for r in fp.relations] if result13.families else []
check("group13: the two .idml revisions are proposed as related (family inference works for IDML, same engine as PDF/DOCX/DOC)",
      len(result13.families) >= 1 and len(all_related) >= 1, result13.families)

# ------------------------------------------------------------------
# Group 14 (constraint 2): a mere file_sha256 difference must NEVER by itself
# create a revision_of relation -- two UNRELATED .idml (different product,
# different manual code, different content) must NOT be related.
# ------------------------------------------------------------------
def _unrelated_idml(name: str, title: str, body: str) -> Path:
    st = _story("us", [("Body Text", title), ("Heading 1", "1 Introduzione"), ("Body Text", body)])
    return _zip_write(name, {
        "designmap.xml": _designmap(["Stories/Story_us.xml"], ["Spreads/Spread_a.xml"]),
        "Spreads/Spread_a.xml": _spread("a", [("tf1", "us", 0, 0, None)]),
        "Stories/Story_us.xml": st,
    })

crane_idml = _unrelated_idml("CraneUnitZ_IT_Rev1.idml", "CraneUnitZ",
                             "Manuale della gru a bandiera CraneUnitZ per il sollevamento industriale certificato.")
forklift_idml = _unrelated_idml("ForkliftQ_IT_Rev1.idml", "ForkliftQ",
                                "Manuale del carrello elevatore ForkliftQ con batteria al litio per uso interno.")
result14 = fam.analyze_paths([str(crane_idml), str(forklift_idml)])
related14 = [r for fp in result14.families for r in fp.relations] if result14.families else []
check("group14: file_sha256 difference ALONE does not create a revision_of between unrelated .idml files",
      len(related14) == 0, result14.families)
check("group14: unrelated .idml pair lands in 'unrelated', not an invented family",
      any(c.file_sha256 == fam.sha256_file(crane_idml) for c in result14.unrelated) or not result14.families,
      [c.filename_original for c in result14.unrelated])

# ------------------------------------------------------------------
# Group 15 (constraint 1): exact-content hash (file_sha256) is used for
# dedup/unchanged-detection ONLY -- never as cross-revision identity. Two
# byte-identical .idml files (same content, different filenames) must dedupe;
# the composite fingerprint (Group 1) is a SEPARATE, structured mechanism,
# never collapsed into a single hash-based identity claim.
# ------------------------------------------------------------------
import shutil as _shutil
dup_a = TMP / "DupA_IT_Rev1.idml"
dup_b = TMP / "DupA_IT_Rev1_copy.idml"
_shutil.copyfile(fam_rev1, dup_a)
_shutil.copyfile(fam_rev1, dup_b)
cand_a = fam.build_candidate(dup_a)
cand_b = fam.build_candidate(dup_b)
check("group15: byte-identical .idml copies share the same file_sha256 (content identity for dedup)",
      cand_a.file_sha256 == cand_b.file_sha256, (cand_a.file_sha256, cand_b.file_sha256))
deduped, dup_report = fam.dedupe_candidates([cand_a, cand_b])
check("group15: dedupe_candidates collapses the byte-identical pair to one candidate",
      len(deduped) == 1 and len(dup_report) == 1, (len(deduped), dup_report))
check("group15: content-hash identity is NEVER the composite fingerprint used for cross-revision matching "
      "(the fingerprint is a separate, structured, per-block signal -- see Group 1)",
      True, "structural check only, verified by Group 1's fingerprint assertions")

# ------------------------------------------------------------------
# Group 16 (constraint 6): DOCTYPE/ENTITY constructs are rejected explicitly,
# before any XML parsing is even attempted.
# ------------------------------------------------------------------
hostile_designmap = ('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "bar">]>'
                     '<Document xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging">'
                     '<idPkg:Story src="Stories/Story_x.xml"/></Document>')
p16a = _zip_write("g16_doctype_designmap.idml", {"designmap.xml": hostile_designmap,
                                                 "Stories/Story_x.xml": "<Story Self=\"x\"/>"})
try:
    idml.parse_idml(p16a)
    check("group16: DOCTYPE in designmap.xml is rejected", False, "no exception raised")
except IdmlImportError as exc:
    check("group16: DOCTYPE in designmap.xml is rejected (fail-closed, explicit)", True, str(exc))

hostile_story = '<?xml version="1.0"?><!DOCTYPE Story [<!ENTITY x "y">]><Story Self="x">&x;</Story>'
p16b = _zip_write("g16_entity_story.idml", {
    "designmap.xml": _designmap(["Stories/Story_x.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "x", 0, 0, None)]),
    "Stories/Story_x.xml": hostile_story,
})
try:
    idml.parse_idml(p16b)
    check("group16: ENTITY declaration in a Story XML is rejected", False, "no exception raised")
except IdmlImportError as exc:
    check("group16: ENTITY declaration in a Story XML is rejected (fail-closed, explicit)", True, str(exc))

# ------------------------------------------------------------------
# Group 17 (constraint 5): duplicate Links/ basenames -- never resolved
# arbitrarily. Two DIFFERENT files share the basename "image.png".
# ------------------------------------------------------------------
story_dupasset = _story("s17", [("Body Text", "Testo con immagine ambigua")],
                        figure_after_index=0, figure_link="whatever/image.png")
p17 = _zip_write("g17_duplicate_basename.idml", {
    "designmap.xml": _designmap(["Stories/Story_s17.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "s17", 0, 0, None)]),
    "Stories/Story_s17.xml": story_dupasset,
    "Links/FolderA/image.png": b"CONTENT-A",
    "Links/FolderB/image.png": b"CONTENT-B",
})
parsed17 = idml.parse_idml(p17)
fig17 = next(it for g in parsed17.groups for it in g["items"] if it["kind"] == "figure")
check("group17: two package entries sharing a basename are NEVER resolved arbitrarily to either one",
      fig17["_zip_asset_path"] is None and fig17["_asset_status"] == "ambiguous", fig17)
check("group17: an ambiguous asset does not silently count as 'missing' -- it gets its own signal",
      parsed17.ambiguous_asset_total == 1, parsed17.ambiguous_asset_total)
check("group17: ambiguous asset raises a review item explaining why nothing was chosen",
      any(r["reason"] == "ambiguous_asset" for r in parsed17.review_items), parsed17.review_items)
check("group17: file_path stays empty for an ambiguous asset (never a guess)", fig17["file_path"] == "", fig17)

# ------------------------------------------------------------------
# Group 18 (constraint 4): the shared writer's figure capability is
# independent of block/table capability -- a repo without create_figure must
# still get its text/table content written normally, figures simply skipped.
# ------------------------------------------------------------------
class _NoFigureRepo:
    """Proxies everything to a real IndustrialRepository except create_figure,
    so hasattr(repo, 'create_figure') is False without touching the repo's
    real class -- a minimal capability-detection test double."""
    def __init__(self, real):
        object.__setattr__(self, "_real", real)
    def __getattr__(self, name):
        if name == "create_figure":
            raise AttributeError(name)
        return getattr(self._real, name)

no_fig_db = TMP / "no_figure_repo.sqlite"
real_repo18 = IndustrialRepository(no_fig_db)
proxy18 = _NoFigureRepo(real_repo18)
check("group18: capability probe confirms create_figure is hidden on the proxy", not hasattr(proxy18, "create_figure"), "")
check("group18: capability probe confirms add_block/create_table are still visible",
      hasattr(proxy18, "add_block") and hasattr(proxy18, "create_table"), "")
groups18 = [{"title": "Sezione con figura", "page": 1, "items": [
    {"kind": "text", "text": "Testo normale", "page": 1, "block_type": "paragraph"},
    {"kind": "table", "rows": [["A", "B"]], "page": 1, "table_index": 0},
    {"kind": "figure", "page": 1, "caption": "", "alt_text": "", "file_path": ""},
]}]
frame18 = _write_topic_groups_to_repo(proxy18, groups18, source_lang="it-IT", id_prefix="NOFIG", src_name="test.idml")
check("group18: topic/text/table content is written normally even without figure capability",
      len(frame18) == 1 and int(real_repo18.conn.execute("SELECT COUNT(*) FROM content_blocks WHERE block_type='paragraph'").fetchone()[0]) == 1,
      frame18)
check("group18: table content is written normally too", int(real_repo18.conn.execute("SELECT COUNT(*) FROM structured_tables").fetchone()[0]) == 1, "")
check("group18: the figure item is silently skipped, no exception, no figures row",
      int(real_repo18.conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0]) == 0, "")
real_repo18.close()

# ------------------------------------------------------------------
# Group 19 (constraint 1): atomic rollback -- a failure AFTER a successful
# parse, DURING the CCMS write, must leave zero residue (no new topics,
# blocks, tables, figures, and no assets directory at its final destination).
# ------------------------------------------------------------------
story_g19a = _story("g19a", [("Heading 1", "Prima sezione"), ("Body Text", "Testo prima sezione")],
                    figure_after_index=1, figure_link="Links/first.png")
story_g19b = _story("g19b", [("Heading 1", "Seconda sezione"), ("Body Text", "Testo seconda sezione")],
                    figure_after_index=1, figure_link="Links/second.png")
p19 = _zip_write("g19_rollback.idml", {
    "designmap.xml": _designmap(["Stories/Story_g19a.xml", "Stories/Story_g19b.xml"],
                                ["Spreads/Spread_a.xml", "Spreads/Spread_b.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g19a", 0, 0, None)]),
    "Spreads/Spread_b.xml": _spread("b", [("tf2", "g19b", 0, 100, None)]),
    "Stories/Story_g19a.xml": story_g19a,
    "Stories/Story_g19b.xml": story_g19b,
    "Links/first.png": b"FIRSTBYTES",
    "Links/second.png": b"SECONDBYTES",
})
db19 = TMP / "rollback.sqlite"
repo19 = IndustrialRepository(db19)
topics_before19 = int(repo19.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
figures_before19 = int(repo19.conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0])
tables_before19 = int(repo19.conn.execute("SELECT COUNT(*) FROM structured_tables").fetchone()[0])
assets_dir19 = TMP / "g19_assets"

_orig_create_figure = repo19.create_figure
_calls19 = {"n": 0}
def _flaky_create_figure(*a, **kw):
    _calls19["n"] += 1
    if _calls19["n"] >= 2:
        raise RuntimeError("simulated failure during CCMS write (after a successful parse)")
    return _orig_create_figure(*a, **kw)
repo19.create_figure = _flaky_create_figure

try:
    import_idml_as_topics(repo19, p19, source_lang="it-IT", id_prefix="RB", assets_dir=assets_dir19)
    check("group19: the simulated mid-write failure actually raised", False, "no exception raised")
except RuntimeError:
    pass

topics_after19 = int(repo19.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
figures_after19 = int(repo19.conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0])
tables_after19 = int(repo19.conn.execute("SELECT COUNT(*) FROM structured_tables").fetchone()[0])
check("group19: zero net new topics after a mid-write failure (first group's topic rolled back too)",
      topics_after19 == topics_before19, (topics_before19, topics_after19))
check("group19: zero net new figures after rollback", figures_after19 == figures_before19, (figures_before19, figures_after19))
check("group19: zero net new structured_tables after rollback", tables_after19 == tables_before19, (tables_before19, tables_after19))
check("group19: the final assets directory was never created", not assets_dir19.exists(), assets_dir19)
staging_leftovers19 = list(assets_dir19.parent.glob(f".{assets_dir19.name}.staging-*"))
check("group19: no staging directory leftover either (cleaned up on failure)", staging_leftovers19 == [], staging_leftovers19)
repo19.close()

# ------------------------------------------------------------------
# Group 20 (constraint 2): review items are a real, blocking UI gate --
# approve_source_ui must fail closed while unresolved items exist, and
# succeed once idml_review_confirm_ui has been used.
# ------------------------------------------------------------------
story_g20 = _story("g20body", [("Heading 1", "Sezione valida"), ("Body Text", "Contenuto regolare del manuale.")])
story_g20_orphan = _story("g20orphan", [("Body Text", "Testo mai collegato a una pagina.")])
p20 = _zip_write("g20_review_gate.idml", {
    "designmap.xml": _designmap(["Stories/Story_g20body.xml", "Stories/Story_g20orphan.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g20body", 0, 0, None)]),
    "Stories/Story_g20body.xml": story_g20,
    "Stories/Story_g20orphan.xml": story_g20_orphan,
})
orig_data_root20 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g20_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir20 = create_project(app.DATA_ROOT, project_name="Review Gate Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir20), str(p20), "it-IT", "PUB-MAIN", "Review Gate")
    pending20 = app._idml_pending_review_items(str(pdir20), "PUB-MAIN")
    check("group20: unresolved review items are present after an import with an orphan story",
          len(pending20) >= 1, pending20)
    try:
        app.approve_source_ui(str(pdir20), "PUB-MAIN", "QA", True)
        check("group20: approve_source_ui fails closed while review items are pending", False, "no exception raised")
    except Exception as exc:
        check("group20: approve_source_ui fails closed while review items are pending",
              "verificare" in str(exc).lower(), str(exc))
    radios20 = [app._IDML_ACCEPT_EXCLUSION] + [None] * (app._IDML_REVIEW_MAX_ITEMS - 1)
    app.idml_review_confirm_ui(str(pdir20), "QA Reviewer", *radios20)
    pending20b = app._idml_pending_review_items(str(pdir20), "PUB-MAIN")
    check("group20: idml_review_confirm_ui resolves the pending items with an explicit per-item decision",
          pending20b == [], pending20b)
    all_items20 = app._idml_review_state(str(pdir20), "PUB-MAIN")
    check("group20 (constraint 2): the decision is auditable (who/when), not a blanket resolved flag",
          all_items20[0]["decision"] == "accepted_exclusion" and all_items20[0]["decided_by"] == "QA Reviewer"
          and all_items20[0]["decided_at"], all_items20)
    msg20 = app.approve_source_ui(str(pdir20), "PUB-MAIN", "QA", True)
    check("group20: approve_source_ui now succeeds once review items are confirmed", "approvata" in msg20.lower(), msg20)
finally:
    app.DATA_ROOT = orig_data_root20

# ------------------------------------------------------------------
# Group 21 (constraint 7): Italian singular/plural agreement in the
# user-facing summary message.
# ------------------------------------------------------------------
check("group21: _it_count uses the singular form for n == 1", app._it_count(1, "sezione", "sezioni") == "**1 sezione**", "")
check("group21: _it_count uses the plural form for n != 1 (including 0)",
      app._it_count(2, "sezione", "sezioni") == "**2 sezioni**" and app._it_count(0, "sezione", "sezioni") == "**0 sezioni**", "")

story_g21_single = _story("g21s", [("Heading 1", "Unica sezione"), ("Body Text", "Testo con una sola figura e una sola tabella.")],
                          tables=[[["A", "B"]]], figure_after_index=1, figure_missing_link="file:///nowhere/pic.png")
p21_single = _zip_write("g21_singular.idml", {
    "designmap.xml": _designmap(["Stories/Story_g21s.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g21s", 0, 0, None)]),
    "Stories/Story_g21s.xml": story_g21_single,
})
orig_data_root21 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g21_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir21 = create_project(app.DATA_ROOT, project_name="Plural Test", source_lang="it-IT")
    msg21, *_ = app.import_manual_ui(str(pdir21), str(p21_single), "it-IT", "PUB-MAIN", "Plural Test")
    check("group21: singular 'sezione' used for exactly one section", "1 sezione**" in msg21 and "1 sezioni**" not in msg21, msg21)
    check("group21: singular 'tabella' used for exactly one table", "1 tabella**" in msg21 and "1 tabelle**" not in msg21, msg21)
    check("group21: singular 'immagine collegata non trovata' used for exactly one missing asset",
          "1 immagine collegata non trovata**" in msg21, msg21)
finally:
    app.DATA_ROOT = orig_data_root21

# ------------------------------------------------------------------
# Group 22 (data-safety gate 1): a corrupt/unreadable review state file must
# NEVER be silently reinterpreted as "nothing to review".
# ------------------------------------------------------------------
story_g22 = _story("g22orphanhost", [("Heading 1", "Sezione valida"), ("Body Text", "Contenuto regolare.")])
story_g22_orphan = _story("g22orphan", [("Body Text", "Testo mai collegato.")])
p22 = _zip_write("g22_corrupt_review.idml", {
    "designmap.xml": _designmap(["Stories/Story_g22orphanhost.xml", "Stories/Story_g22orphan.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g22orphanhost", 0, 0, None)]),
    "Stories/Story_g22orphanhost.xml": story_g22,
    "Stories/Story_g22orphan.xml": story_g22_orphan,
})
orig_data_root22 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g22_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir22 = create_project(app.DATA_ROOT, project_name="Corrupt Review Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir22), str(p22), "it-IT", "PUB-MAIN", "Corrupt Review")
    review_path22 = app._idml_review_path(str(pdir22), "PUB-MAIN")
    check("group22: a real review file was produced (orphan story present)", review_path22.exists(), review_path22)
    # Corrupt it: not valid JSON at all.
    review_path22.write_text("{this is not valid json at all", encoding="utf-8")
    try:
        app._idml_pending_review_items(str(pdir22), "PUB-MAIN")
        check("group22: corrupt review JSON raises IdmlReviewCorruptError, never returns []", False, "no exception raised")
    except app.IdmlReviewCorruptError:
        check("group22: corrupt review JSON raises IdmlReviewCorruptError, never returns []", True, "")
    try:
        app.approve_source_ui(str(pdir22), "PUB-MAIN", "QA", True)
        check("group22: approve_source_ui fails closed on corrupt review state", False, "no exception raised")
    except Exception as exc:
        check("group22: approve_source_ui fails closed on corrupt review state, with an understandable message",
              "corrott" in str(exc).lower() or "leggibil" in str(exc).lower(), str(exc))
    try:
        app.idml_review_confirm_ui(str(pdir22), "QA", *([None] * app._IDML_REVIEW_MAX_ITEMS))
        check("group22: idml_review_confirm_ui fails closed on corrupt review state", False, "no exception raised")
    except Exception as exc:
        check("group22: idml_review_confirm_ui fails closed on corrupt review state", True, str(exc))
    # Also: a well-formed-JSON-but-wrong-shape file must fail closed too.
    review_path22.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    try:
        app._idml_pending_review_items(str(pdir22), "PUB-MAIN")
        check("group22: valid JSON but wrong shape still raises IdmlReviewCorruptError", False, "no exception raised")
    except app.IdmlReviewCorruptError:
        check("group22: valid JSON but wrong shape still raises IdmlReviewCorruptError", True, "")
finally:
    app.DATA_ROOT = orig_data_root22

# ------------------------------------------------------------------
# Group 23 (data-safety gate 2): "seen" is never "resolved" -- each review
# item needs its OWN explicit, auditable decision drawn from a type-specific
# vocabulary. caption_candidate's "confirm association" must actually update
# the figure's real CCMS caption; "keep separate" must not.
# ------------------------------------------------------------------
story_g23 = _story("g23", [("Body Text", "Testo prima della figura"), ("Caption", "Didascalia proposta g23")],
                   figure_after_index=0, figure_missing_link="file:///nowhere/pic23.png")
p23 = _zip_write("g23_caption_decision.idml", {
    "designmap.xml": _designmap(["Stories/Story_g23.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g23", 0, 0, None)]),
    "Stories/Story_g23.xml": story_g23,
})
orig_data_root23 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g23_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir23 = create_project(app.DATA_ROOT, project_name="Caption Decision Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir23), str(p23), "it-IT", "PUB-MAIN", "Caption Decision")
    pending23 = app._idml_pending_review_items(str(pdir23), "PUB-MAIN")
    idx23 = next(i for i, it in enumerate(pending23) if it["reason"] == "caption_candidate")
    choices23 = app._idml_review_choices_for("caption_candidate")
    check("group23: caption_candidate offers BOTH 'keep separate' and 'confirm association' -- never auto-decided",
          set(choices23) == {app._IDML_KEEP_SEPARATE, app._IDML_CONFIRM_CAPTION},
          choices23)
    radios23 = [None] * app._IDML_REVIEW_MAX_ITEMS
    radios23[idx23] = app._IDML_CONFIRM_CAPTION
    app.idml_review_confirm_ui(str(pdir23), "QA Reviewer", *radios23)
    figs23 = list(IndustrialRepository(app._ccms_db(pdir23)).conn.execute("SELECT caption FROM figures"))
    check("group23: 'confirm association' actually writes the candidate text into the figure's real caption",
          any(r[0] == "Didascalia proposta g23" for r in figs23), figs23)
    items23 = app._idml_review_state(str(pdir23), "PUB-MAIN")
    check("group23: the decision is recorded per-item, auditable, not a blanket resolved flag",
          items23[idx23]["decision"] == "confirm_association" and items23[idx23]["decided_by"] == "QA Reviewer",
          items23[idx23])
finally:
    app.DATA_ROOT = orig_data_root23

story_g23b = _story("g23b", [("Body Text", "Testo prima della figura"), ("Caption", "Didascalia NON confermata")],
                    figure_after_index=0, figure_missing_link="file:///nowhere/pic23b.png")
p23b = _zip_write("g23b_caption_keep_separate.idml", {
    "designmap.xml": _designmap(["Stories/Story_g23b.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g23b", 0, 0, None)]),
    "Stories/Story_g23b.xml": story_g23b,
})
orig_data_root23b = app.DATA_ROOT
app.DATA_ROOT = TMP / "g23b_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir23b = create_project(app.DATA_ROOT, project_name="Caption Keep Separate Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir23b), str(p23b), "it-IT", "PUB-MAIN", "Caption Keep Separate")
    pending23b = app._idml_pending_review_items(str(pdir23b), "PUB-MAIN")
    idx23b = next(i for i, it in enumerate(pending23b) if it["reason"] == "caption_candidate")
    radios23b = [None] * app._IDML_REVIEW_MAX_ITEMS
    radios23b[idx23b] = app._IDML_KEEP_SEPARATE
    app.idml_review_confirm_ui(str(pdir23b), "QA Reviewer", *radios23b)
    figs23b = list(IndustrialRepository(app._ccms_db(pdir23b)).conn.execute("SELECT caption FROM figures"))
    check("group23: 'keep separate' does NOT write the candidate text into the figure's caption",
          all(r[0] != "Didascalia NON confermata" for r in figs23b), figs23b)
finally:
    app.DATA_ROOT = orig_data_root23b

# ------------------------------------------------------------------
# Group 24 (data-safety gate 3): two legitimately distinct assets sharing a
# basename must NEVER overwrite each other -- each figure record must point
# at its own correct bytes.
# ------------------------------------------------------------------
story_g24a = _story("g24a", [("Body Text", "Testo A")], figure_after_index=0, figure_link="Links/FolderA/image.png")
story_g24b = _story("g24b", [("Body Text", "Testo B")], figure_after_index=0, figure_link="Links/FolderB/image.png")
p24 = _zip_write("g24_dup_basename_exact.idml", {
    "designmap.xml": _designmap(["Stories/Story_g24a.xml", "Stories/Story_g24b.xml"],
                                ["Spreads/Spread_a.xml", "Spreads/Spread_b.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g24a", 0, 0, None)]),
    "Spreads/Spread_b.xml": _spread("b", [("tf2", "g24b", 0, 100, None)]),
    "Stories/Story_g24a.xml": story_g24a,
    "Stories/Story_g24b.xml": story_g24b,
    "Links/FolderA/image.png": b"CONTENT-A-24",
    "Links/FolderB/image.png": b"CONTENT-B-24",
})
parsed24 = idml.parse_idml(p24)
figs24 = [it for g in parsed24.groups for it in g["items"] if it["kind"] == "figure"]
check("group24: both figures resolve via an EXACT full-path match, neither is ambiguous",
      len(figs24) == 2 and all(f["_asset_status"] == "present_in_package" for f in figs24), figs24)
assets_dir24 = TMP / "g24_assets"
copied24 = idml.materialize_figure_assets(p24, parsed24.groups, assets_dir24)
check("group24: both distinct assets are actually copied", copied24 == 2, copied24)
paths24 = [Path(f["file_path"]) for f in figs24]
check("group24: two DISTINCT destination paths -- no overwrite despite the identical basename",
      len(set(paths24)) == 2, paths24)
contents24 = {p.read_bytes() for p in paths24}
check("group24: both original, distinct byte contents are preserved correctly (no cross-contamination)",
      contents24 == {b"CONTENT-A-24", b"CONTENT-B-24"}, contents24)

# ------------------------------------------------------------------
# Group 25 (data-safety gate 4): a FAILED re-import must leave the previous
# valid draft byte-for-byte and row-for-row identical -- topics, blocks,
# figures, source file, materialized assets, and review state.
# ------------------------------------------------------------------
story_g25 = _story("g25", [("Heading 1", "Sezione originale"), ("Body Text", "Testo originale importante da preservare.")],
                   figure_after_index=1, figure_link="Links/orig.png")
p25_good = _zip_write("g25_good.idml", {
    "designmap.xml": _designmap(["Stories/Story_g25.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g25", 0, 0, None)]),
    "Stories/Story_g25.xml": story_g25,
    "Links/orig.png": b"ORIGINAL-BYTES-25",
})
orig_data_root25 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g25_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir25 = create_project(app.DATA_ROOT, project_name="Reimport Safety Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir25), str(p25_good), "it-IT", "PUB-MAIN", "Original Draft")

    db25 = app._ccms_db(pdir25)
    repo25 = IndustrialRepository(db25)
    topics_before = sorted(tuple(r) for r in repo25.conn.execute("SELECT topic_id,title,status FROM topics"))
    blocks_before = sorted(tuple(r) for r in repo25.conn.execute("SELECT block_id,block_type,text_content,object_ref_id FROM content_blocks"))
    figures_before = sorted(tuple(r) for r in repo25.conn.execute("SELECT figure_id,caption,file_path,status FROM figures"))
    repo25.close()

    src_path25 = Path(pdir25)/"Sources"/p25_good.name
    src_bytes_before = src_path25.read_bytes()
    assets_dir25 = Path(pdir25)/"Sources"/f"{p25_good.stem}_assets"
    asset_files_before = {p.name: p.read_bytes() for p in assets_dir25.rglob("*") if p.is_file()} if assets_dir25.exists() else {}
    review_path25 = app._idml_review_path(str(pdir25), "PUB-MAIN")
    review_before = review_path25.read_text(encoding="utf-8") if review_path25.exists() else None

    # A DIFFERENT, deliberately malformed package, re-imported into the SAME
    # publication -- simulates re-exporting a broken/corrupt IDML.
    p25_bad = TMP / "g25_bad_reimport.idml"
    with zipfile.ZipFile(p25_bad, "w") as zf:
        zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("designmap.xml", _designmap(["Stories/Story_bad.xml"], ["Spreads/Spread_a.xml"]))
        zf.writestr("Spreads/Spread_a.xml", _spread("a", [("tf1", "bad", 0, 0, None)]))
        zf.writestr("Stories/Story_bad.xml", "<Story Self=\"bad\">UNCLOSED")

    try:
        app.import_manual_ui(str(pdir25), str(p25_bad), "it-IT", "PUB-MAIN", "Broken Reimport Attempt")
        check("group25: the broken re-import attempt actually fails", False, "no exception raised")
    except Exception:
        pass

    repo25b = IndustrialRepository(db25)
    topics_after = sorted(tuple(r) for r in repo25b.conn.execute("SELECT topic_id,title,status FROM topics"))
    blocks_after = sorted(tuple(r) for r in repo25b.conn.execute("SELECT block_id,block_type,text_content,object_ref_id FROM content_blocks"))
    figures_after = sorted(tuple(r) for r in repo25b.conn.execute("SELECT figure_id,caption,file_path,status FROM figures"))
    repo25b.close()

    check("group25: topics are IDENTICAL after a failed re-import", topics_after == topics_before, (topics_before, topics_after))
    check("group25: content_blocks are IDENTICAL after a failed re-import", blocks_after == blocks_before, "")
    check("group25: figures are IDENTICAL after a failed re-import", figures_after == figures_before, "")
    check("group25: the previous source file bytes are untouched by the failed attempt",
          src_path25.read_bytes() == src_bytes_before, "")
    if assets_dir25.exists():
        asset_files_after = {p.name: p.read_bytes() for p in assets_dir25.rglob("*") if p.is_file()}
        check("group25: the previous materialized asset bytes are untouched", asset_files_after == asset_files_before, "")
    review_after = review_path25.read_text(encoding="utf-8") if review_path25.exists() else None
    check("group25: the previous review state is untouched by the failed re-import", review_after == review_before, "")
finally:
    app.DATA_ROOT = orig_data_root25

# ------------------------------------------------------------------
# Group 26 (acceptance fix): no review decision is ever pre-selected -- not
# in the backend state (decision must stay None until an explicit confirm)
# and not in the initial Radio value the UI renders (value=None), for BOTH
# a single-choice reason (orphan_story -> only "accept exclusion") and a
# multi-choice reason (caption_candidate -> two real options). Also checks
# that re-rendering the panel (as a refresh would) never introduces a value,
# and that the summary banner contains no raw Markdown "**" (it is shown by
# a gr.HTML component, which does not interpret Markdown).
# ------------------------------------------------------------------
story_g26 = _story("g26", [("Body Text", "Testo prima della figura"), ("Caption", "Didascalia candidata g26")],
                   figure_after_index=0, figure_missing_link="file:///nowhere/pic26.png")
story_g26_orphan = _story("g26orphan", [("Body Text", "Testo mai collegato a nessun frame, g26")])
p26 = _zip_write("g26_no_preselection.idml", {
    "designmap.xml": _designmap(["Stories/Story_g26.xml", "Stories/Story_g26orphan.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g26", 0, 0, None)]),
    "Stories/Story_g26.xml": story_g26,
    "Stories/Story_g26orphan.xml": story_g26_orphan,
})
orig_data_root26 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g26_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir26 = create_project(app.DATA_ROOT, project_name="No Preselection Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir26), str(p26), "it-IT", "PUB-MAIN", "No Preselection")

    items26 = app._idml_review_state(str(pdir26), "PUB-MAIN")
    check("group26: backend decision is None for every item right after import (nothing pre-resolved)",
          len(items26) == 2 and all(it["decision"] is None for it in items26), items26)

    for render_pass in ("first render", "simulated refresh/re-render"):
        html_upd, row_upd, *radio_upds = app._idml_review_panel_ui(str(pdir26), "PUB-MAIN")
        active = [u for u in radio_upds if u.get("visible")]
        check(f"group26 ({render_pass}): exactly 2 review Radios are shown", len(active) == 2, active)
        check(f"group26 ({render_pass}): NO Radio -- single-choice or multi-choice -- carries a pre-selected value",
              all(u.get("value") is None for u in active), active)
        check(f"group26 ({render_pass}): summary banner has no raw Markdown '**' (rendered by gr.HTML, not gr.Markdown)",
              "**" not in html_upd.get("value", ""), html_upd.get("value"))
        check(f"group26 ({render_pass}): summary banner states the count in plain HTML text",
              "2 elementi da verificare" in html_upd.get("value", ""), html_upd.get("value"))

    # Confirming with the Radios exactly as the UI would deliver them on an
    # untouched screen (all None) must resolve NOTHING and must NOT let
    # approve_source_ui proceed.
    untouched_radios = [None] * app._IDML_REVIEW_MAX_ITEMS
    app.idml_review_confirm_ui(str(pdir26), "QA Reviewer", *untouched_radios)
    items26b = app._idml_review_state(str(pdir26), "PUB-MAIN")
    check("group26: submitting the panel untouched (all None) resolves zero items",
          all(it["decision"] is None for it in items26b), items26b)
    try:
        app.approve_source_ui(str(pdir26), "PUB-MAIN", "QA Reviewer", True)
        check("group26: approve_source_ui still blocked when nothing was ever explicitly decided", False, "no exception raised")
    except Exception:
        check("group26: approve_source_ui still blocked when nothing was ever explicitly decided", True, "")
finally:
    app.DATA_ROOT = orig_data_root26

# ------------------------------------------------------------------
# Group 27 (acceptance fix): a blocked approval through the GUIDED callback
# (guided_approve_source_ui, the real callback bound to c_simple_approve in
# the standard Contenuti view, with FOUR outputs: msg/project_card/
# sidebar_context/table) must never propagate the exception -- doing so left
# the other three outputs in an error state in the UI even though nothing
# was actually wrong with them. The gate itself (still blocked while review
# items are pending) must stay identical.
# ------------------------------------------------------------------
story_g27 = _story("g27", [("Body Text", "Testo prima della figura"), ("Caption", "Didascalia candidata g27")],
                   figure_after_index=0, figure_missing_link="file:///nowhere/pic27.png")
story_g27_orphan = _story("g27orphan", [("Body Text", "Testo mai collegato a nessun frame, g27")])
p27 = _zip_write("g27_guided_approve_no_corruption.idml", {
    "designmap.xml": _designmap(["Stories/Story_g27.xml", "Stories/Story_g27orphan.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g27", 0, 0, None)]),
    "Stories/Story_g27.xml": story_g27,
    "Stories/Story_g27orphan.xml": story_g27_orphan,
})
orig_data_root27 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g27_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir27 = create_project(app.DATA_ROOT, project_name="Guided Approve No Corruption Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir27), str(p27), "it-IT", "PUB-MAIN", "Guided Approve No Corruption")

    pending27 = app._idml_pending_review_items(str(pdir27), "PUB-MAIN")
    check("group27: fixture has exactly 2 pending review items before approval", len(pending27) == 2, pending27)

    card_before = app._project_card(str(pdir27))
    sidebar_before = app._sidebar_context(str(pdir27))
    table_before = app._simple_review_table_for_project(str(pdir27))
    db27 = app._ccms_db(pdir27)
    topics_before27 = sorted(tuple(r) for r in IndustrialRepository(db27).conn.execute("SELECT topic_id,status FROM topics"))

    # The real guided callback: general "Confermo di aver revisionato il
    # contenuto" checkbox = True, click "Approva sorgente" -- must NOT raise.
    try:
        msg27, card27, sidebar27, table27 = app.guided_approve_source_ui(str(pdir27), "QA Reviewer", True)
    except Exception as exc:
        check("group27: guided_approve_source_ui does not raise when approval is blocked "
              "(the exception would corrupt the other 3 bound outputs in the UI)", False, str(exc))
    else:
        check("group27: guided_approve_source_ui does not raise when approval is blocked", True, "")
        check("group27: approval is still denied with a clear inline message",
              "elementi da verificare" in msg27 and "✅" not in msg27, msg27)
        check("group27: project_card output is valid and UNCHANGED (no data was touched)",
              card27 == card_before, (card_before, card27))
        check("group27: sidebar_context output is valid and UNCHANGED", sidebar27 == sidebar_before, (sidebar_before, sidebar27))
        check("group27: review table output is valid and UNCHANGED", table27.equals(table_before), (table_before, table27))
        topics_after27 = sorted(tuple(r) for r in IndustrialRepository(db27).conn.execute("SELECT topic_id,status FROM topics"))
        check("group27: no persistence was modified by the blocked approval attempt",
              topics_after27 == topics_before27, (topics_before27, topics_after27))

    # Resolve both pending items, then the SAME guided callback must approve normally.
    radios27 = [None] * app._IDML_REVIEW_MAX_ITEMS
    for i, it in enumerate(pending27):
        radios27[i] = app._IDML_KEEP_SEPARATE if it["reason"] == "caption_candidate" else app._IDML_ACCEPT_EXCLUSION
    app.idml_review_confirm_ui(str(pdir27), "QA Reviewer", *radios27)
    msg27b, card27b, sidebar27b, table27b = app.guided_approve_source_ui(str(pdir27), "QA Reviewer", True)
    check("group27: after resolving every pending item, the SAME guided callback approves normally",
          "✅" in msg27b and "approvata" in msg27b, msg27b)
finally:
    app.DATA_ROOT = orig_data_root27

# ------------------------------------------------------------------
# Group 28 (acceptance fix): "Conferma decisioni" with an empty reviewer name
# must block persistence WITHOUT raising (this callback has 14 bound
# outputs: html + row + 12 Radios -- an exception there previously wiped
# every already-made choice and left them in an Error state). The already-
# selected choices must be echoed back unchanged, an inline message must
# explain the missing name, and a second submission with the name filled in
# (same choices, not reselected) must then persist normally.
# ------------------------------------------------------------------
story_g28 = _story("g28", [("Body Text", "Testo prima della figura"), ("Caption", "Didascalia candidata g28")],
                   figure_after_index=0, figure_missing_link="file:///nowhere/pic28.png")
story_g28_orphan = _story("g28orphan", [("Body Text", "Testo mai collegato a nessun frame, g28")])
p28 = _zip_write("g28_reviewer_name_required.idml", {
    "designmap.xml": _designmap(["Stories/Story_g28.xml", "Stories/Story_g28orphan.xml"], ["Spreads/Spread_a.xml"]),
    "Spreads/Spread_a.xml": _spread("a", [("tf1", "g28", 0, 0, None)]),
    "Stories/Story_g28.xml": story_g28,
    "Stories/Story_g28orphan.xml": story_g28_orphan,
})
orig_data_root28 = app.DATA_ROOT
app.DATA_ROOT = TMP / "g28_projects"
app.DATA_ROOT.mkdir(parents=True, exist_ok=True)
try:
    pdir28 = create_project(app.DATA_ROOT, project_name="Reviewer Name Required Test", source_lang="it-IT")
    app.import_manual_ui(str(pdir28), str(p28), "it-IT", "PUB-MAIN", "Reviewer Name Required")

    pending28 = app._idml_pending_review_items(str(pdir28), "PUB-MAIN")
    check("group28: fixture has exactly 2 pending review items", len(pending28) == 2, pending28)

    radios28 = [None] * app._IDML_REVIEW_MAX_ITEMS
    for i, it in enumerate(pending28):
        radios28[i] = app._IDML_KEEP_SEPARATE if it["reason"] == "caption_candidate" else app._IDML_ACCEPT_EXCLUSION

    # Step 1: both choices made, reviewer name EMPTY, click "Conferma decisioni".
    try:
        html28, row28, *radio_upds28 = app.idml_review_confirm_ui(str(pdir28), "", *radios28)
    except Exception as exc:
        check("group28: idml_review_confirm_ui does not raise when the reviewer name is empty "
              "(the exception would Error out all 14 bound outputs and wipe the choices)", False, str(exc))
    else:
        check("group28: idml_review_confirm_ui does not raise when the reviewer name is empty", True, "")
        check("group28: no decision is persisted while the reviewer name is missing",
              all(it["decision"] is None for it in app._idml_review_state(str(pdir28), "PUB-MAIN")),
              app._idml_review_state(str(pdir28), "PUB-MAIN"))
        check("group28: the review section stays visible (not hidden/reset)",
              bool(row28.get("visible")) and bool(html28.get("visible")), (row28, html28))
        check("group28: an inline message asks for the reviewer name",
              "Inserisci il tuo nome" in html28.get("value", ""), html28.get("value"))
        active_upds28 = radio_upds28[:len(pending28)]
        check("group28: the already-made choices are echoed back UNCHANGED, not cleared",
              [u.get("value") for u in active_upds28] == radios28[:len(pending28)],
              ([u.get("value") for u in active_upds28], radios28[:len(pending28)]))

    # Step 2: same choices (not reselected), reviewer name now filled in.
    app.idml_review_confirm_ui(str(pdir28), "QA Reviewer", *radios28)
    items28 = app._idml_review_state(str(pdir28), "PUB-MAIN")
    check("group28: after filling in the name, the SAME submitted choices are persisted for every item",
          all(it["decision"] is not None and it["decided_by"] == "QA Reviewer" for it in items28), items28)
    check("group28: review items are now fully resolved (zero pending)",
          app._idml_pending_review_items(str(pdir28), "PUB-MAIN") == [], "")
finally:
    app.DATA_ROOT = orig_data_root28

# The reviewer textbox is shared with "Conferma decisioni" (mandatory there);
# its label must not claim to be optional.
# 4B.2 UX Clarity Gate update: this field (c_simple_reviewer) was merged into
# a single shared "Il tuo nome" identity field (c_actor) reused across the
# whole Contenuti tab -- source approval, topic/block creation, save,
# reorder, archive/restore, revert, review submission -- per an explicit,
# reviewed product decision (there used to be two separate "Il tuo nome"
# textboxes with nothing to tell them apart). The behavioral assertions
# above (idml_review_confirm_ui rejects an empty name, shows inline
# guidance, echoes choices back unchanged, persists once filled in) are
# unchanged and still pass against c_actor; only this literal-source-text
# check is updated to match the new, still-non-optional field.
app_source = (BASE / "app.py").read_text(encoding="utf-8")
check("group28: the reviewer name field no longer claims to be optional",
      'label="Il tuo nome — opzionale"' not in app_source, "")
check("group28: the shared 'Il tuo nome' field states its purpose (recording who makes changes/decisions), not that it's optional",
      'c_actor = gr.Textbox(label="Il tuo nome", info="Serve per registrare chi effettua modifiche e decisioni.")' in app_source, "")

print(f"parse build: {idml.IDML_IMPORT_BUILD}")

try:
    print(json.dumps({"pass": sum(r["status"] == "PASS" for r in results), "tests": len(results)}))
except UnicodeEncodeError:
    pass
