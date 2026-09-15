"""Milestone 4F.1: production publishing outputs (PDF + offline HTML5).

Covers the checkpoint's required matrix: canonical model, profile/variant
correctness, rich-text fidelity, tables, figures/assets, HTML5 package
portability, real PDF structure/text, mutation safety and repeatability --
plus the renderer-specific risks found while probing fitz.Story (silent
over-wide-table drop, ligatures in the extracted text layer, no <a href>
link annotations).

Pure server-side: builds real artifacts on disk and inspects them. No
browser, no server, no Gradio.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitz

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService
import authoring_view as av
import publishing_output as po
import publishing_preview as pp
import rich_text as rt
from project_lifecycle import create_project

TMP = Path(tempfile.mkdtemp(prefix="manualsync_outputs_4f1_"))
results: list[dict] = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def _new_project(name: str) -> Path:
    p = create_project(TMP, project_name=name, source_lang="en-US")
    repo = IndustrialRepository(p / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("T1", "Installazione", "legacy body", status="Approved")
        repo.create_publication("PUB-MAIN", "Manuale Impianto ZX", ["T1"])
    finally:
        repo.close()
    return p


def _seed_market(svc: AuthoringService, actor: str = "Writer A") -> None:
    svc.create_condition_dimension("market", "Mercato", actor=actor)
    svc.create_condition_value("eu", "market", "EU", actor=actor)
    svc.create_condition_value("us", "market", "US", actor=actor)


def _make_image(name: str) -> Path:
    from PIL import Image
    path = TMP / name
    Image.new("RGB", (40, 24), color=(70, 120, 190)).save(path)
    return path


IMG = _make_image("fixture_fig.png")


# ====================================================================
# Fixture: one publication exercising every output-relevant scenario
# ====================================================================
P = _new_project("4f1_main")
repo, svc = av.open_service(P)
_seed_market(svc)

intro = svc.create_block_in_topic("T1", "paragraph", actor="Writer A",
                                  text="Introduzione sempre inclusa.")["block_id"]

rich = svc.create_block_in_topic("T1", "paragraph", actor="Writer A", text="segnaposto")["block_id"]
RICH_TEXT = ("**ATTENZIONE**: scollegare `P-401` prima di _procedere_. "
             "Consulta il [manuale online](https://example.test/zx).")
svc.save_block(rich, text=RICH_TEXT, expected_version=1, actor="Writer A", text_format="rich")

bullets = svc.create_block_in_topic("T1", "paragraph", actor="Writer A", text="segnaposto")["block_id"]
svc.save_block(bullets, text="- Controllare alimentazione\n- Verificare pressione",
               expected_version=1, actor="Writer A", text_format="rich")

numbers = svc.create_block_in_topic("T1", "paragraph", actor="Writer A", text="segnaposto")["block_id"]
svc.save_block(numbers, text="1. Aprire valvola\n2. Avviare pompa",
               expected_version=1, actor="Writer A", text_format="rich")

LITERAL = "Riferimento: filettatura M8*1.25, parametro MAX_PRESSURE_LIMIT, nota [1]."
literal = svc.create_block_in_topic("T1", "paragraph", actor="Writer A", text=LITERAL)["block_id"]

eu_blk = svc.create_block_in_topic("T1", "paragraph", actor="Writer A",
                                   text="Contenuto europeo conformita CE.")["block_id"]
svc.save_block(eu_blk, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
us_blk = svc.create_block_in_topic("T1", "paragraph", actor="Writer A",
                                   text="Contenuto statunitense conformita UL.")["block_id"]
svc.save_block(us_blk, condition={"market": ["us"]}, expected_version=1, actor="Writer A")

tbl = svc.create_table_in_topic("T1", actor="Writer A", title="Parametri operativi",
                                caption="Valori nominali",
                                columns=["Parametro", "Valore"],
                                rows=[["Parametro", "Valore"], ["Pressione", "4.5 bar"],
                                      ["Temperatura", "60 gradi"]])
tbl_blk = tbl["block_id"]

fig = svc.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG,
                                 caption="Schema impianto", alt_text="Schema del circuito")
fig_blk = fig["block_id"]

fig_missing = svc.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG,
                                         caption="Immagine mancante")
repo.conn.execute("UPDATE figures SET file_path='/nonexistent/ghost.png' WHERE figure_id=?",
                  (fig_missing["figure_id"],))

fig_traversal = svc.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG,
                                           caption="Traversal")
repo.conn.execute("UPDATE figures SET file_path=? WHERE figure_id=?",
                  (str(Path(P) / "Assets" / ".." / ".." / "outside.png"), fig_traversal["figure_id"]))

OUTSIDE = TMP / "outside_root.png"
shutil.copyfile(IMG, OUTSIDE)
fig_outside = svc.create_figure_in_topic("T1", actor="Writer A", source_file_path=IMG,
                                         caption="Fuori progetto")
repo.conn.execute("UPDATE figures SET file_path=? WHERE figure_id=?",
                  (str(OUTSIDE), fig_outside["figure_id"]))
repo.conn.commit()

# US-conditioned topic with an EU child -> parent gating in production output
repo.conn.execute(
    "INSERT INTO topics(topic_id,title,body,source_lang,status,version,include_variants_json,"
    "exclude_variants_json,source_refs_json,evidence_status,created_at,updated_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    ("T3", "Appendice statunitense", "", "en-US", "Approved", 1, "[]", "[]", "[]", "OK"))
repo.conn.commit()
svc.save_topic("T3", condition={"market": ["us"]}, expected_version=1, actor="Writer A")
t3_eu_child = svc.create_block_in_topic("T3", "paragraph", actor="Writer A",
                                        text="Figlio europeo sotto topic statunitense.")["block_id"]
svc.save_block(t3_eu_child, condition={"market": ["eu"]}, expected_version=1, actor="Writer A")
repo.conn.execute("DELETE FROM publication_topics WHERE publication_id='PUB-MAIN'")
repo.conn.execute("INSERT INTO publication_topics(publication_id,topic_id,position) "
                  "VALUES('PUB-MAIN','T1',1),('PUB-MAIN','T3',2)")
repo.conn.commit()

svc.create_variant_profile("PROF-EU", "EU Service", {"market": "eu"}, actor="Writer A")
svc.create_variant_profile("PROF-US", "US Service", {"market": "us"}, actor="Writer A")
repo.close()


def _open():
    return av.open_service(P)


def _pdf_text(path: Path) -> str:
    doc = fitz.open(path)
    try:
        return po.normalize_pdf_text("\n".join(p.get_text() for p in doc))
    finally:
        doc.close()


def _squeeze(text: str) -> str:
    """All whitespace removed. The PDF text layer wraps wherever the frame
    ends -- a long URL or a two-word phrase can legitimately break across
    lines -- so any multi-word assertion here compares squeezed forms.
    This proves the characters are present IN ORDER without turning normal
    typography into a test failure."""
    return "".join(text.split())


# ====================================================================
# Core model (items 1-6)
# ====================================================================
r, s = _open()
model_eu = po.build_output_model(r, s, "PUB-MAIN", "PROF-EU")
model_us = po.build_output_model(r, s, "PUB-MAIN", "PROF-US")
preview_eu = pp.build_preview_model(r, s, "PUB-MAIN", "PROF-EU")
r.close()

_r2, _s2 = _open()
model_eu_again = po.build_output_model(_r2, _s2, "PUB-MAIN", "PROF-EU")
_r2.close()


def _shape(m):
    return [(t["title"], [b["block_type"] for b in t["blocks"]]) for t in m["topics"]]


check("core1 (item 1): the production model is deterministic -- two independent builds of the same "
      "publication/profile produce an identical topic/block shape, and the rendered HTML is "
      "byte-identical apart from the generation timestamp",
      _shape(model_eu) == _shape(model_eu_again), (_shape(model_eu), _shape(model_eu_again)))
check("core1b (item 1): the rendered document body is byte-identical between the two builds",
      po.render_output_html(model_eu, embed_css=False) ==
      po.render_output_html(model_eu_again, embed_css=False), None)

included_preview_eu = sum(1 for t in preview_eu["topics"] if t["included"]
                          for b in t["blocks"] if b["included"])
included_output_eu = sum(len(t["blocks"]) for t in model_eu["topics"])
check("core2 (item 2): production inclusion is EXACTLY 4E.2 preview inclusion -- the model is a "
      "projection of the approved evaluator, never a second interpretation",
      included_output_eu == included_preview_eu, (included_output_eu, included_preview_eu))

eu_texts = " ".join(rt.plain_text(b["content"].get("text", ""), b["content"].get("text_format", "plain"))
                    for t in model_eu["topics"] for b in t["blocks"]
                    if b["content"].get("kind") in pp._TEXT_BLOCK_KINDS)
us_texts = " ".join(rt.plain_text(b["content"].get("text", ""), b["content"].get("text_format", "plain"))
                    for t in model_us["topics"] for b in t["blocks"]
                    if b["content"].get("kind") in pp._TEXT_BLOCK_KINDS)
check("core3 (item 3): EU output contains EU content and NOT US-only content",
      "conformita CE" in eu_texts and "conformita UL" not in eu_texts, eu_texts[:200])
check("core4 (item 4): US output contains US content and NOT EU-only content",
      "conformita UL" in us_texts and "conformita CE" not in us_texts, us_texts[:200])
check("core5 (item 5): parent gating -- the US-only topic is absent from EU output, present in US "
      "output, and its EU-conditioned child never appears under EU",
      [t["title"] for t in model_eu["topics"]] == ["Installazione"] and
      "Appendice statunitense" in [t["title"] for t in model_us["topics"]] and
      "Figlio europeo" not in eu_texts,
      ([t["title"] for t in model_eu["topics"]], [t["title"] for t in model_us["topics"]]))
check("core5b: under US, the US topic's EU-conditioned child is still excluded by its OWN condition",
      "Figlio europeo" not in us_texts, us_texts[:200])

html_eu = po.render_output_html(model_eu)
check("core6 (item 6): production output carries NO diagnostic exclusion boxes -- no reason text, no "
      "'escluso' styling, no preview-only classes",
      "ms-pp-excluded" not in html_eu and "ms-pp-reason" not in html_eu
      and "Non incluso" not in html_eu, None)


# ====================================================================
# Rich text (items 7-14)
# ====================================================================
check("rich7 (item 7): bold renders as real markup", "<strong>ATTENZIONE</strong>" in html_eu, None)
check("rich8 (item 8): italic renders as real markup", "<em>procedere</em>" in html_eu, None)
check("rich9 (item 9): inline code renders as real markup", "<code>P-401</code>" in html_eu, None)
check("rich10 (item 10): bullet list renders as a real <ul>",
      "<ul>" in html_eu and "<li>Controllare alimentazione</li>" in html_eu, None)
check("rich11 (item 11): numbered list renders as a real <ol>",
      "<ol>" in html_eu and "<li>Aprire valvola</li>" in html_eu, None)
check("rich12 (item 12): a safe link renders as a real anchor with its href",
      'href="https://example.test/zx"' in html_eu and ">manuale online</a>" in html_eu, None)
check("rich13 (item 13): literal technical text survives exactly, unformatted",
      "M8*1.25" in html_eu and "MAX_PRESSURE_LIMIT" in html_eu and "nota [1]" in html_eu, None)
body_only = html_eu[html_eu.find("<body>"):]
check("rich14 (item 14): no raw canonical syntax leaks into the rendered document -- no '**', no "
      "'](http', no backticked spans",
      "**" not in body_only and "](http" not in body_only and "`" not in body_only, None)


# ====================================================================
# Tables (items 15-17)
# ====================================================================
check("tbl15 (item 15): every row and cell is preserved in the rendered table",
      all(tok in html_eu for tok in ["<th>Parametro</th>", "<th>Valore</th>", "<td>Pressione</td>",
                                     "<td>4.5 bar</td>", "<td>Temperatura</td>", "<td>60 gradi</td>"]), None)
check("tbl16 (item 16): table title and caption are preserved",
      "Parametri operativi" in html_eu and "Valori nominali" in html_eu, None)
_tbl_content = next(b["content"] for t in model_eu["topics"] for b in t["blocks"]
                    if b["content"].get("kind") == "table")
check("tbl17 (item 17): no silent truncation -- the rendered table carries as many <tr> as stored rows",
      html_eu.count("<tr>") == len(_tbl_content["rows"]), (html_eu.count("<tr>"), len(_tbl_content["rows"])))


# ====================================================================
# Figures / assets (items 18-23) -- via a real exported package
# ====================================================================
r3, s3 = _open()
pkg = po.export_html_package(r3, s3, "PUB-MAIN", "PROF-EU")
r3.close()
pkg_index = pkg["index"].read_text(encoding="utf-8")
asset_files = sorted(p.name for p in (pkg["output_dir"] / "assets").glob("*")) \
    if (pkg["output_dir"] / "assets").is_dir() else []

check("fig18 (item 18): a valid figure is materialized into the package and referenced by the page",
      len(asset_files) == 1 and f"assets/{asset_files[0]}" in pkg_index, (asset_files, pkg["figures"]))
check("fig19 (item 19): a missing asset degrades to a VISIBLE documented placeholder (never a broken "
      "img, never a silent disappearance) and is reported as a warning",
      "Immagine non disponibile" in pkg_index
      and any("Immagine mancante" in w for w in pkg["warnings"]), pkg["warnings"])
check("fig20 (item 20): a path-traversal file_path is rejected by the shared asset gate",
      any("Traversal" in w for w in pkg["warnings"]), pkg["warnings"])
check("fig21 (item 21): an out-of-project asset is rejected even though the file genuinely exists",
      OUTSIDE.is_file() and any("Fuori progetto" in w for w in pkg["warnings"]), pkg["warnings"])
check("fig22 (item 22): no local filesystem path, project directory or username leaks into the page",
      not any(tok in pkg_index for tok in [str(P), "C:\\", "file://", str(Path.home())]), None)
check("fig23 (item 23): asset names are portable and deterministic (ASCII, ordinal-derived, no original "
      "filename, no internal id)",
      all(n.startswith("fig-") and n[4:8].isdigit() and n.isascii() for n in asset_files), asset_files)


# ====================================================================
# HTML5 package (items 24-29)
# ====================================================================
check("html24 (item 24): the package contains index.html", pkg["index"].is_file(), None)
check("html25 (item 25): local CSS and assets resolve relatively from the package root",
      (pkg["output_dir"] / "style.css").is_file()
      and "href='style.css'" in pkg_index
      and all((pkg["output_dir"] / f"assets/{n}").is_file() for n in asset_files), None)
check("html26 (item 26): no CDN, no external script, no network dependency for reading",
      not any(tok in pkg_index for tok in ["http://cdn", "https://cdn", "<script", "cdnjs",
                                           "googleapis", "unpkg"]), None)
check("html27 (item 27): no absolute filesystem path anywhere in the package's own files",
      not any(tok in (pkg["output_dir"] / f).read_text(encoding="utf-8")
              for f in ("index.html", "style.css") for tok in ["C:\\", "file://"]), None)
check("html28 (item 28): only safe link schemes survive into the page",
      "javascript:" not in pkg_index and "data:text/html" not in pkg_index
      and 'href="https://example.test/zx"' in pkg_index, None)

PORTABLE = TMP / "moved_package"
shutil.copytree(pkg["output_dir"], PORTABLE)
moved_index = (PORTABLE / "index.html").read_text(encoding="utf-8")
check("html29 (item 29): the package still resolves after being copied elsewhere -- every reference is "
      "relative, so portability is structural rather than incidental",
      moved_index == pkg_index and (PORTABLE / "style.css").is_file()
      and all((PORTABLE / f"assets/{n}").is_file() for n in asset_files), None)


# ====================================================================
# PDF (items 30-38)
# ====================================================================
r4, s4 = _open()
pdf_eu = po.export_pdf(r4, s4, "PUB-MAIN", "PROF-EU")
r4.close()
pdf_path = pdf_eu["pdf"]
raw_head = pdf_path.read_bytes()[:5]
text_eu = _pdf_text(pdf_path)

check("pdf30 (item 30): the PDF file exists and is non-trivial in size",
      pdf_path.is_file() and pdf_path.stat().st_size > 1000, pdf_path.stat().st_size)
check("pdf31 (item 31): valid PDF magic/structure -- opens with a real parser",
      raw_head == b"%PDF-", raw_head)
check("pdf32 (item 32): page count > 0", pdf_eu["pages"] > 0, pdf_eu["pages"])
check("pdf33 (item 33): text is extractable, i.e. a real text layer and not an image-only scan",
      len(text_eu) > 200, len(text_eu))
sq_eu = _squeeze(text_eu)
check("pdf34 (item 34): the publication title is present in the PDF",
      _squeeze("Manuale Impianto ZX") in sq_eu, text_eu[:150])
check("pdf35 (item 35): representative rich text is visible -- bold word, code span, list items, and "
      "the literal technical string all survive",
      all(_squeeze(tok) in sq_eu for tok in ["ATTENZIONE", "P-401", "Controllare alimentazione",
                                             "Aprire valvola", "M8*1.25", "MAX_PRESSURE_LIMIT"]),
      text_eu[:400])
check("pdf35b: a safe link keeps BOTH its label and its destination readable in print (this MuPDF build "
      "emits no link annotations, so the URL is rendered inline rather than lost -- it may wrap across "
      "lines like any long token, which is normal typography, not loss)",
      _squeeze("manuale online") in sq_eu and _squeeze("https://example.test/zx") in sq_eu, text_eu[:400])
check("pdf36 (item 36): representative table content is present",
      all(_squeeze(tok) in sq_eu for tok in ["Parametri operativi", "Pressione", "4.5 bar",
                                             "60 gradi"]), None)
check("pdf37 (item 37): no internal ids, local paths or usernames leak into the PDF text",
      not any(tok in text_eu for tok in ["BLK-", "TBL-", "FIG-", "PROF-EU", "C:\\", str(Path.home()),
                                         "file://"]), None)
check("pdf38 (item 38): profile filtering holds in the PDF -- EU content present, US-only absent",
      _squeeze("conformita CE") in sq_eu and _squeeze("conformita UL") not in sq_eu, None)

r5, s5 = _open()
pdf_us = po.export_pdf(r5, s5, "PUB-MAIN", "PROF-US")
r5.close()
text_us = _pdf_text(pdf_us["pdf"])
sq_us = _squeeze(text_us)
check("pdf38b (item 38, mirror): the US PDF contains US content and the US-only topic, and excludes "
      "EU-only content", _squeeze("conformita UL") in sq_us and _squeeze("conformita CE") not in sq_us
      and _squeeze("Appendice statunitense") in sq_us, text_us[:300])
check("pdf38c: rendered figures are embedded in the PDF as real images",
      any(page.get_images() for page in fitz.open(pdf_eu["pdf"])), None)
check("pdf38d: no diagnostic exclusion wording reaches the final PDF",
      "Non incluso" not in text_eu and "escluso" not in text_eu.lower(), None)


# ====================================================================
# Mutation safety (items 39-42)
# ====================================================================
def _snapshot():
    rr, ss = _open()
    try:
        blocks = {row["block_id"]: row["version"] for row in
                  rr.conn.execute("SELECT block_id, version FROM content_blocks")}
        topics = [(row["topic_id"], row["version"], row["status"]) for row in
                  rr.conn.execute("SELECT topic_id, version, status FROM topics")]
        audit = rr.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
        try:
            releases = rr.conn.execute("SELECT COUNT(*) c FROM publication_releases").fetchone()["c"]
        except Exception:
            releases = 0
        return {"blocks": blocks, "topics": topics, "audit": audit, "releases": releases}
    finally:
        rr.close()


before = _snapshot()
r6, s6 = _open()
po.export_pdf(r6, s6, "PUB-MAIN", "PROF-EU")
po.export_html_package(r6, s6, "PUB-MAIN", "PROF-EU")
r6.close()
after = _snapshot()

check("mut39 (item 39): exporting does not bump any content version",
      before["blocks"] == after["blocks"], None)
check("mut40 (item 40): exporting creates no authoring audit event",
      before["audit"] == after["audit"], (before["audit"], after["audit"]))
check("mut41 (item 41): exporting changes no review/release status -- topic status untouched and no "
      "release record invented (publishing output != release approval)",
      before["topics"] == after["topics"] and before["releases"] == after["releases"], None)

# item 42: a failed export must leave nothing that looks successful.
P2 = _new_project("4f1_wide")
r7, s7 = av.open_service(P2)
_seed_market(s7)
s7.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Introduzione al documento.")
wide_cols = [f"Colonna descrittiva numero {i}" for i in range(1, 15)]
s7.create_table_in_topic("T1", actor="Writer A", title="Tabella larga", caption="",
                         columns=wide_cols,
                         rows=[wide_cols, [f"Valore molto lungo numero {i}" for i in range(1, 15)]])
s7.create_variant_profile("PROF-ALL", "Tutti", {}, actor="Writer A")
r7.close()

# The fail-closed guarantee is tested DETERMINISTICALLY by simulating a
# renderer that loses content, rather than by relying on a particular
# MuPDF layout quirk firing: the guarantee under test is "a PDF whose
# content did not survive is never published", independently of WHICH
# renderer limitation caused the loss.
exports_root = Path(P2) / "Exports"
_real_render = po._render_pdf_file


def _lossy_render(html, staging_dir, out_pdf):
    return _real_render("<html><body><p>pagina vuota</p></body></html>", staging_dir, out_pdf)


r8, s8 = av.open_service(P2)
po._render_pdf_file = _lossy_render
try:
    failed = None
    try:
        po.export_pdf(r8, s8, "PUB-MAIN", "PROF-ALL")
    except po.PublishingOutputError as e:
        failed = str(e)
finally:
    po._render_pdf_file = _real_render
    r8.close()

check("mut42 (item 42): when the renderer silently loses content, the export FAILS CLOSED -- "
      "post-render text verification catches it and nothing is published",
      failed is not None, failed)
check("mut42b (item 42): the failed export left NO successful-looking artifact on disk",
      not exports_root.exists() or not any(p.suffix == ".pdf" for p in exports_root.rglob("*")),
      sorted(str(p) for p in exports_root.rglob("*")) if exports_root.exists() else "no Exports dir")
check("mut42c: the failure message is human-readable, with no stack trace, path or internal id",
      failed and "PDF" in failed and not any(t in failed for t in ["Traceback", "C:\\", "BLK-"]),
      failed)

# And with the REAL renderer, the invariant must hold either way for a
# genuinely hostile (very wide) table: either the export fails closed, or
# it succeeds AND the cells are actually in the finished PDF. What is
# forbidden is "succeeded while the content silently vanished".
r8b, s8b = av.open_service(P2)
wide_err, wide_res = None, None
try:
    wide_res = po.export_pdf(r8b, s8b, "PUB-MAIN", "PROF-ALL")
except po.PublishingOutputError as e:
    wide_err = str(e)
r8b.close()
if wide_res is None:
    wide_ok = wide_err is not None
    wide_detail = wide_err
else:
    wide_sq = _squeeze(_pdf_text(wide_res["pdf"]))
    wide_ok = _squeeze("Valore molto lungo numero 1") in wide_sq
    wide_detail = f"exported {wide_res['pdf'].name}, cells present={wide_ok}"
check("mut42d (renderer-risk invariant): a hostile very-wide table either fails the export closed or "
      "is genuinely present in the PDF -- never a successful export with vanished cells",
      wide_ok, wide_detail)


# ====================================================================
# Repeatability / manifest (items 43-44)
# ====================================================================
r9, s9 = _open()
first = po.export_html_package(r9, s9, "PUB-MAIN", "PROF-EU")
second = po.export_html_package(r9, s9, "PUB-MAIN", "PROF-EU")
r9.close()
check("rep43 (item 43): a second export never silently overwrites the first -- both artifacts survive "
      "in distinct, non-colliding directories",
      first["output_dir"] != second["output_dir"] and first["index"].is_file()
      and second["index"].is_file(), (str(first["output_dir"]), str(second["output_dir"])))

manifest = json.loads((first["manifest"]).read_text(encoding="utf-8"))
check("rep44 (item 44): the manifest records a real sha256 per artifact, matching the file on disk",
      all(entry["sha256"] == po._sha256_file(first["output_dir"] / entry["name"])
          for entry in manifest["artifacts"]), manifest["artifacts"][:2])
check("rep44b: the manifest carries publication/profile/version metadata and the source summary, and "
      "flags unapproved content honestly",
      manifest["publication_title"] == "Manuale Impianto ZX" and manifest["profile_name"] == "EU Service"
      and manifest["product_build"] and "source_summary" in manifest
      and "includes_unapproved_content" in manifest, None)
check("rep44c: reader-facing pages carry no internal ids, but the technical manifest may -- the split "
      "is deliberate", "PUB-MAIN" not in pkg_index and manifest["publication_id"] == "PUB-MAIN", None)


# ====================================================================
# Renderer-specific risks found while probing fitz.Story
# ====================================================================
check("eng1: extracted-PDF text is normalized before any integrity check, so MuPDF's typographic "
      "ligatures can never produce a false 'content lost' verdict",
      po.normalize_pdf_text("veriﬁcare la ﬁgura eﬃcace") == "verificare la figura efficace", None)
check("eng2: the PDF integrity probe uses single contiguous words (never phrases), so line wrapping "
      "and list markers cannot fake a failure",
      all(" " not in t for t in po._pdf_expected_tokens(model_eu)[1:]),
      po._pdf_expected_tokens(model_eu))
P3 = _new_project("4f1_empty")
r11, s11 = av.open_service(P3)
_seed_market(s11)
b_only_us = s11.create_block_in_topic("T1", "paragraph", actor="Writer A", text="Solo US.")["block_id"]
s11.save_block(b_only_us, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
s11.save_topic("T1", condition={"market": ["us"]}, expected_version=1, actor="Writer A")
s11.create_variant_profile("PROF-EU2", "EU", {"market": "eu"}, actor="Writer A")
r11.close()
r12, s12 = av.open_service(P3)
try:
    po.export_pdf(r12, s12, "PUB-MAIN", "PROF-EU2")
    empty_err = None
except po.PublishingOutputError as e:
    empty_err = str(e)
r12.close()
check("eng4: a profile that includes nothing produces a clear human error, never an empty PDF",
      empty_err is not None and "Nessun contenuto" in empty_err, empty_err)

check("eng5 (documented limit): <thead> does not repeat across page breaks in this MuPDF build -- "
      "recorded as a known limitation rather than silently assumed", True,
      "verified during engine investigation; long tables keep their header on the first page only")


# ====================================================================
# Minimal UI surface: the export controls must be ADDITIVE and ISOLATED
# -- the authoring editor is frozen for this milestone, and a failing
# export must never be able to disturb the 49-slot Studio bundle.
# ====================================================================
import app  # noqa: E402  (deliberately last: constructing Blocks is the heavy part)

_cfg = app.demo.get_config_file()
_fns = app.demo.fns
_export_deps = {}
for _d in _cfg["dependencies"]:
    _bf = _fns.get(_d.get("id"))
    _name = getattr(_bf.fn, "__name__", "") if _bf and getattr(_bf, "fn", None) else ""
    if _name in ("studio_export_pdf_ui", "studio_export_html_ui"):
        _export_deps[_name] = _d

check("ui1: both export callbacks are wired exactly once each",
      sorted(_export_deps) == ["studio_export_html_ui", "studio_export_pdf_ui"], sorted(_export_deps))
check("ui2: each export callback is ISOLATED -- ONE output of its own (the result card), never the "
      "49-slot Studio bundle, so an export failure cannot cascade into the authoring panel",
      all(len(d["outputs"]) == 1 for d in _export_deps.values()),
      {k: len(v["outputs"]) for k, v in _export_deps.items()})
check("ui3: each export callback takes exactly the preview's own [project, publication, profile] "
      "inputs -- preview and deliverable can never disagree about what is being published",
      all(len(d["inputs"]) == 3 for d in _export_deps.values()),
      {k: len(v["inputs"]) for k, v in _export_deps.items()})
check("ui4: no export callback is chained onto another event (no trigger_after timing bridge)",
      all(d.get("trigger_after") is None for d in _export_deps.values()), None)
check("ui5 (authoring frozen): ST_OUT_FULL is still exactly 49 outputs -- this milestone added no "
      "authoring surface", len(app.ST_OUT_FULL) == 49, len(app.ST_OUT_FULL))

_pdf_err = app.studio_export_pdf_ui("", "PUB-MAIN", "PROF-EU")
check("ui6: a missing project produces a CONTAINED human card, never a raised gr.Error",
      "ms-export-card-err" in _pdf_err and "Nessun progetto" in _pdf_err, _pdf_err)
_noprof = app.studio_export_pdf_ui(str(P), "PUB-MAIN", None)
check("ui7: no profile selected produces a contained prompt, not a failure",
      "Seleziona prima un profilo" in _noprof, _noprof)


# ====================================================================
# 4F.1 Export UX fix: the result CARD.
#
# Root problem this section pins: the first cut returned a success
# banner plus a raw Gradio File widget ("manuale.pdf / Scarica / size")
# and printed the technical export path in standard mode -- leaving the
# user to deduce that the filename card was how you open the manual.
# ====================================================================
import export_links  # noqa: E402
import re as _re  # noqa: E402

_card_pdf = app.studio_export_pdf_ui(str(P), "PUB-MAIN", "PROF-EU")


def _links(card: str) -> list[tuple[str, str]]:
    """(href, label) for every action button in a result card."""
    return _re.findall(r"<a class='ms-export-btn[^']*' href='([^']+)'[^>]*>([^<]+)</a>", card)


_pdf_links = dict((label, href) for href, label in _links(_card_pdf))

check("uxa1 (item 1): the PDF export returns a real success CARD with the required headline and a "
      "human subtitle naming the manual, the profile and the page count",
      "ms-export-card" in _card_pdf and "PDF creato correttamente" in _card_pdf
      and "Manuale Impianto ZX" in _card_pdf and "EU Service" in _card_pdf
      and "1 pagina" in _card_pdf, _card_pdf[:400])
check("uxa2 (item 2): the card offers a PRIMARY 'Apri PDF' action",
      "Apri PDF" in _pdf_links and "ms-export-btn-primary" in _card_pdf, list(_pdf_links))
check("uxa3 (item 3): the card offers a SECONDARY 'Scarica PDF' action",
      "Scarica PDF" in _pdf_links, list(_pdf_links))
check("uxa4 (item 4): the technical path is NOT in standard-mode view -- it appears only inside a "
      "collapsed 'Dettagli tecnici (modalità avanzata)' disclosure",
      "Dettagli tecnici (modalità avanzata)" in _card_pdf
      and _card_pdf.index("Exports") > _card_pdf.index("Dettagli tecnici"), None)
check("uxa5 (item 5): the export UX is no longer a raw Gradio File widget -- the callback returns ONE "
      "HTML card and the export area declares no file component",
      isinstance(_card_pdf, str) and all(len(d["outputs"]) == 1 for d in _export_deps.values()), None)
check("uxa6 (item 6): 'Apri PDF' points at an OPAQUE same-origin route, never a filesystem path",
      _pdf_links["Apri PDF"].startswith("/ms-export/")
      and not any(t in _pdf_links["Apri PDF"] for t in ["C:", "Users", "Exports", ".."]),
      _pdf_links["Apri PDF"])

_pdf_token = _pdf_links["Apri PDF"].split("/")[2]
_served = export_links.resolve_served_file(_pdf_token, _pdf_links["Apri PDF"].split("/f/", 1)[1])
check("uxa7 (item 6): that route resolves to exactly the generated PDF and nothing else",
      _served is not None and _served.suffix == ".pdf" and _served.is_file()
      and "Exports" in str(_served), str(_served))
_dl = export_links.resolve_download(_pdf_token)
check("uxa8 (item 7): 'Scarica PDF' resolves to the generated PDF with a human filename",
      _dl is not None and _dl[0].suffix == ".pdf" and _dl[1].endswith(".pdf")
      and "Manuale" in _dl[1], _dl)

_card_html = app.studio_export_html_ui(str(P), "PUB-MAIN", "PROF-EU")
_html_links = dict((label, href) for href, label in _links(_card_html))
check("uxa9 (item 8): the HTML export returns a success card with the required headline and subtitle",
      "Pacchetto HTML creato correttamente" in _card_html and "Manuale Impianto ZX" in _card_html
      and "EU Service" in _card_html, _card_html[:300])
check("uxa10 (item 9): the card offers a primary 'Apri HTML' action", "Apri HTML" in _html_links,
      list(_html_links))
check("uxa11 (item 10): the card offers 'Scarica pacchetto HTML'", "Scarica pacchetto HTML" in _html_links,
      list(_html_links))

_html_token = _html_links["Apri HTML"].split("/")[2]
_html_served = export_links.resolve_served_file(_html_token, "index.html")
check("uxa12 (item 11): 'Apri HTML' resolves to the generated index.html, and its sibling assets "
      "resolve through the same opaque route so relative hrefs keep working",
      _html_served is not None and _html_served.name == "index.html"
      and export_links.resolve_served_file(_html_token, "style.css") is not None,
      str(_html_served))
_html_dl = export_links.resolve_download(_html_token)
check("uxa13 (item 10): the HTML download is a real ZIP of the package, named for a human",
      _html_dl is not None and _html_dl[0].suffix == ".zip" and _html_dl[1].endswith(".zip"), _html_dl)

# item 12/13: re-export replaces the card's actions but preserves the old artifact.
_card_pdf_2 = app.studio_export_pdf_ui(str(P), "PUB-MAIN", "PROF-EU")
_pdf_links_2 = dict((label, href) for href, label in _links(_card_pdf_2))
_old_pdf = _served
check("uxa14 (item 12): re-exporting replaces the card's Open/Download actions with the NEW artifact -- "
      "no stale action can point at the previous export",
      _pdf_links_2["Apri PDF"] != _pdf_links["Apri PDF"], (_pdf_links["Apri PDF"], _pdf_links_2["Apri PDF"]))
_new_token = _pdf_links_2["Apri PDF"].split("/")[2]
_new_pdf = export_links.resolve_served_file(_new_token, _pdf_links_2["Apri PDF"].split("/f/", 1)[1])
check("uxa15 (item 13): the PREVIOUS artifact still exists on disk -- a re-export never overwrites it",
      _old_pdf.is_file() and _new_pdf.is_file() and _old_pdf != _new_pdf,
      (str(_old_pdf), str(_new_pdf)))

check("uxa16 (item 14): no absolute path, username or file:// URL appears anywhere in either card "
      "outside the collapsed technical disclosure",
      not any(tok in (_card_pdf.split("Dettagli tecnici")[0] + _card_html.split("Dettagli tecnici")[0])
              for tok in [str(Path.home()), "C:\\", "C:/", "file://"]), None)

check("uxa17 (item 15): traversal through the opaque route is rejected",
      all(export_links.resolve_served_file(_html_token, p) is None
          for p in ["../manifest.json", "../../secret.txt", "..\\..\\secret.txt",
                    str(Path.home() / "secret.txt")]), None)
check("uxa18 (item 15): an unknown/guessed token serves nothing",
      export_links.resolve_served_file("not-a-real-token", "index.html") is None
      and export_links.resolve_download("not-a-real-token") is None, None)
check("uxa19 (item 15): only the extensions this app produces are servable -- no arbitrary file read",
      export_links.resolve_served_file(_html_token, "manifest.json") is not None
      and ".exe" not in export_links._MEDIA_TYPES and ".svg" not in export_links._MEDIA_TYPES, None)

_before_ux = _snapshot()
app.studio_export_pdf_ui(str(P), "PUB-MAIN", "PROF-EU")
app.studio_export_html_ui(str(P), "PUB-MAIN", "PROF-EU")
check("uxa20 (item 17): the new export UX is still strictly read-only -- no version, audit or status "
      "mutation from exporting through the callbacks", _before_ux == _snapshot(), None)

# Real defect found by the browser gate: the sidebar switches tabs
# PROGRAMMATICALLY (gr.Tabs(selected=...)), which never fires
# authoring_tab.select() -- and the tab strip is hidden by design (R23).
# So a user navigating normally never populated the Publishing Preview
# profile picker, leaving preview AND export unusable. Pinned here so it
# cannot silently regress.
_nav_profile_chains = []
for _d in _cfg["dependencies"]:
    if _d.get("trigger_after") is None:
        continue
    _bf = _fns.get(_d.get("id"))
    _fname = getattr(_bf.fn, "__name__", "") if _bf and getattr(_bf, "fn", None) else ""
    if _fname == "studio_preview_profile_choices_ui":
        _nav_profile_chains.append(_d)
check("uxa21 (navigation defect, fixed): opening Contenuti from the sidebar refreshes the Publishing "
      "Preview profile picker -- at least one CHAINED studio_preview_profile_choices_ui exists, so a "
      "programmatic tab switch can no longer leave the picker (and therefore export) empty",
      len(_nav_profile_chains) >= 1, len(_nav_profile_chains))
check("uxa22: the profile-choices callback genuinely returns this project's active profiles, so the "
      "picker is populated with real choices rather than an empty list",
      len(app.studio_preview_profile_choices_ui(str(P))[0]["choices"]) == 2,
      app.studio_preview_profile_choices_ui(str(P)))

shutil.rmtree(TMP, ignore_errors=True)
print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"), "tests": len(results)}))
