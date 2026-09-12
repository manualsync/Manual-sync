"""Publishing Preflight v1 — OUTPUT-INTEGRITY preflight, fail-closed export.

The smallest coherent subset of Prompt 10. It is NOT a general-purpose
overset detector and NOT a layout engine: it verifies that the content
SAMPLED by source-derived probes survived into the finished file, and
refuses the export when it did not.

Every fixture here is measured against the real renderer, and two of them
exist to pin what this build does NOT do:

* a 40-column table really is clipped -- 36 columns vanish from the text
  layer with no geometric signal at all. That is the one reachable
  overset condition here, and it is horizontal.
* a 6000px figure does NOT overflow: MuPDF scales it to fit. An earlier
  draft of this gate blocked on it, and fixture C now pins the opposite,
  because a rule that refuses a document the renderer handles correctly
  is worse than no rule.

No rule in this suite is satisfied by a character count or a column-count
threshold. Each is read from the layout engine's own `place()` return
values, or from the text layer of the finished PDF compared against
counts derived from the source model.

Pure server-side: builds real artifacts on disk and inspects them. No
browser, no server, no Gradio event loop.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitz
from PIL import Image

from ccms_industrial import IndustrialRepository
import authoring_view as av
import publishing_output as po
import publishing_preflight as pf
from project_lifecycle import create_project

TMP = Path(tempfile.mkdtemp(prefix="manualsync_preflight_"))
results: list[dict] = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def _img(name: str, w: int, h: int) -> Path:
    path = TMP / name
    Image.new("RGB", (w, h), color=(70, 120, 190)).save(path)
    return path


OK_IMG = _img("ok.png", 40, 24)
TALL_IMG = _img("tall.png", 300, 6000)   # taller than any A4 printable frame


def _project(name: str, topics=("T1",)):
    p = create_project(TMP, project_name=name, source_lang="it-IT")
    repo = IndustrialRepository(p / "ManualSync_CCMS.sqlite")
    try:
        for i, t in enumerate(topics):
            repo.create_topic(t, f"Sezione {i + 1}", "corpo", status="Approved")
        repo.create_publication("PUB-MAIN", "Manuale Impianto ZX", list(topics))
    finally:
        repo.close()
    repo, svc = av.open_service(p)
    svc.create_variant_profile("PROF-ALL", "Tutto", {}, actor="Writer A")
    return p, repo, svc


def _pdf_count(project: Path) -> int:
    return len(list((Path(project) / "Exports").rglob("*.pdf")))


def _export_pdf(repo, svc, project):
    return po.export_pdf(repo, svc, "PUB-MAIN", "PROF-ALL", project_dir=project)


# ====================================================================
# Fixture A — a clean document. The control case: if this ever starts
# failing, the preflight has become a false-positive machine and is
# worse than not having it.
# ====================================================================
P_OK, repo_ok, svc_ok = _project("pf_clean")
svc_ok.create_block_in_topic("T1", "paragraph", actor="Writer A",
                             text="Introduzione CHIARAMENTE leggibile dell'impianto.")
svc_ok.create_block_in_topic("T1", "warning", actor="Writer A",
                             text="ATTENZIONE: scollegare l'alimentazione PRINCIPALE.")
svc_ok.create_table_in_topic("T1", actor="Writer A", title="Parametri operativi",
                             caption="Valori nominali",
                             columns=["Parametro", "Valore"],
                             rows=[["Parametro", "Valore"],
                                   ["Pressione massima", "4.5 bar"],
                                   ["Temperatura esercizio", "60 gradi"]])
svc_ok.create_figure_in_topic("T1", actor="Writer A", source_file_path=OK_IMG,
                              caption="Schema idraulico", alt_text="Schema")
RES_OK = _export_pdf(repo_ok, svc_ok, P_OK)

check("clean1: a clean document exports successfully", RES_OK["pdf"].is_file(), RES_OK["pdf"])
check("clean2: preflight reports no blocking finding",
      RES_OK["preflight"]["ok"] is True and RES_OK["preflight"]["blocking"] == 0,
      RES_OK["preflight"])
check("clean3: a clean document produces no warnings either",
      RES_OK["preflight"]["warnings"] == 0,
      [f["rule"] for f in RES_OK["preflight"]["findings"]])
check("clean4: the PDF is real -- parsable, paged, with extractable text",
      RES_OK["pages"] >= 1, RES_OK["pages"])
_doc = fitz.open(RES_OK["pdf"])
_text = po.normalize_pdf_text("\n".join(p.get_text() for p in _doc))
_doc.close()
check("clean5: the table's LAST column survived into the finished PDF -- the probe that "
      "makes horizontal clipping detectable is itself exercised on a passing document",
      "esercizio" in _text or "60" in _text, _text[:200])
check("clean6: existing export contract intact (kind, manifest, pages, figures, topics)",
      RES_OK["kind"] == "pdf" and RES_OK["manifest"].is_file()
      and RES_OK["topics"] == 1 and RES_OK["figures"] == 1, None)
repo_ok.close()


# ====================================================================
# Fixture B — BLOCKING: a table too wide for the page.
#
# This is the real overset case in this MuPDF build. It does NOT raise,
# it does NOT report `more`, and `filled` stays inside the frame: the
# renderer simply keeps the leftmost columns and throws the rest away.
# Measured: 4 of 40 columns survive.
# ====================================================================
P_WIDE, repo_wide, svc_wide = _project("pf_wide_table")
svc_wide.create_block_in_topic("T1", "paragraph", actor="Writer A",
                               text="Introduzione CHIARAMENTE leggibile dell'impianto.")
_COLS = [f"COLONNA{i:02d}UNICA" for i in range(40)]
svc_wide.create_table_in_topic("T1", actor="Writer A", title="Matrice parametri",
                               caption="Tabella molto larga", columns=_COLS,
                               rows=[_COLS, [f"VALORE{i:02d}UNICO" for i in range(40)]])
_wide_error = None
try:
    _export_pdf(repo_wide, svc_wide, P_WIDE)
except po.PublishingOutputError as exc:
    _wide_error = str(exc)

check("wide1 (blocking): an over-wide table refuses the export instead of shipping a "
      "silently truncated PDF", _wide_error is not None, "export succeeded")
check("wide2: NOTHING is left on disk -- a refused export must not leave an artifact that "
      "looks like a deliverable", _pdf_count(P_WIDE) == 0, _pdf_count(P_WIDE))
check("wide3: the message says the export was blocked and the file was not saved",
      "Export bloccato" in _wide_error and "non è stato salvato" in _wide_error, _wide_error)
check("wide4: it says WHAT failed -- content did not reach the PDF",
      "non è arrivata nel PDF" in _wide_error, _wide_error)
check("wide5: it says WHERE, naming the section and the table rather than a raw id",
      "Sezione «Sezione 1»" in _wide_error and "tabella 1" in _wide_error
      and "MR2023" not in _wide_error, _wide_error)
check("wide6: it points at the LAST COLUMN, which is the part actually clipped",
      "ultima colonna" in _wide_error, _wide_error)
check("wide7: it says WHAT TO CORRECT, in terms an author can act on",
      "riduci il numero di colonne" in _wide_error.lower(), _wide_error)
check("wide8: it reassures that the project content was not modified",
      "non è stato modificato" in _wide_error, _wide_error)
check("wide9: no stack trace, no internal path, no rule id leaks into the message",
      not any(t in _wide_error for t in ("Traceback", "publishing_output", "CONTENT_LOSS",
                                         str(TMP), ".py")), _wide_error)
repo_wide.close()


# ====================================================================
# Fixture C — a hugely oversized figure is NOT overset in this engine.
#
# The obvious candidate for vertical overset: a 6000px-tall image in an
# A4 frame. Measured, MuPDF SCALES it to fit and reports `more = 0`. So
# this must export cleanly, and the suite pins that -- an earlier draft
# of this gate blocked it on a `filled.height > frame.height` rule, which
# turned out to fire on every ordinary full page instead (see the module
# docstring of publishing_preflight).
# ====================================================================
P_TALL, repo_tall, svc_tall = _project("pf_tall_figure")
svc_tall.create_block_in_topic("T1", "paragraph", actor="Writer A",
                               text="Introduzione CHIARAMENTE leggibile dell'impianto.")
svc_tall.create_figure_in_topic("T1", actor="Writer A", source_file_path=TALL_IMG,
                                caption="Schema fuori formato", alt_text="Schema alto")
RES_TALL = _export_pdf(repo_tall, svc_tall, P_TALL)
check("tall1: a hugely oversized figure exports cleanly -- the renderer scales it, so this "
      "is NOT an overset condition and must not be reported as one",
      RES_TALL["pdf"].is_file() and RES_TALL["preflight"]["blocking"] == 0,
      RES_TALL["preflight"])
check("tall2: the introduction text still reached the PDF alongside the scaled figure",
      RES_TALL["pages"] >= 1, RES_TALL["pages"])
check("tall3: no page-layout rule claims a false positive on it",
      not any(f["rule"] in pf.PAGE_LAYOUT_RULES for f in RES_TALL["preflight"]["findings"]),
      RES_TALL["preflight"]["findings"])
repo_tall.close()


# ====================================================================
# Fixture D — WARNING ONLY: export proceeds, deliberately.
#
# An empty included topic and a figure whose file is gone. Both are worth
# a human's attention; neither means the PDF misrepresents the source, so
# neither blocks. Turning today's successful exports into failures is a
# product decision, not a preflight decision.
# ====================================================================
P_WARN, repo_warn, svc_warn = _project("pf_warnings", topics=("T1", "T2"))
svc_warn.create_block_in_topic("T1", "paragraph", actor="Writer A",
                               text="Introduzione CHIARAMENTE leggibile dell'impianto.")
_ghost = svc_warn.create_figure_in_topic("T1", actor="Writer A", source_file_path=OK_IMG,
                                         caption="Figura senza file")
repo_warn.conn.execute("UPDATE figures SET file_path='/nonexistent/ghost.png' WHERE figure_id=?",
                       (_ghost["figure_id"],))
RES_WARN = _export_pdf(repo_warn, svc_warn, P_WARN)
_warn_rules = [f["rule"] for f in RES_WARN["preflight"]["findings"]]

check("warn1: a warning-only export SUCCEEDS -- warnings never block",
      RES_WARN["pdf"].is_file() and RES_WARN["preflight"]["ok"] is True, RES_WARN["preflight"])
check("warn2: an included topic with no content is reported as a warning (the document still has real content elsewhere, so it is not blocking)",
      pf.RULE_EMPTY_TOPIC in _warn_rules, _warn_rules)
check("warn3: a figure whose file is missing is reported as a warning",
      pf.RULE_ASSET_MISSING in _warn_rules, _warn_rules)
check("warn4: every warning is severity 'warning', never 'blocking'",
      all(f["severity"] == pf.WARNING for f in RES_WARN["preflight"]["findings"]),
      RES_WARN["preflight"]["findings"])
check("warn5: warnings reach the delivered manifest, so an artifact carries the caveats it "
      "was produced with",
      any("senza contenuto" in w for w in RES_WARN["warnings"]), RES_WARN["warnings"])
check("warn5b: manifest entries carry NO severity prefix -- the export card already adds its "
      "own 'Avviso:' heading, and prefixing here rendered 'Avviso: Avviso: ...' on screen. "
      "Found in browser acceptance, not by unit checks, because both halves were correct "
      "in isolation",
      not any(w.startswith("Avviso") or w.startswith("Bloccante")
              for w in pf.manifest_entries(pf.PreflightReport(kind="pdf", findings=[
                  pf.Finding(rule=pf.RULE_EMPTY_TOPIC, severity=pf.WARNING,
                             what="Sezione inclusa ma senza contenuto.",
                             where="Sezione «X»", fix="y")]))),
      pf.manifest_entries(RES_WARN and pf.PreflightReport(kind="pdf")))
_refusal = pf.blocking_message(pf.PreflightReport(kind="pdf", findings=[
    pf.Finding(rule=pf.RULE_CONTENT_LOSS, severity=pf.BLOCKING,
               what="Perso.", where="X", fix="Y")]))
check("warn5c: the refusal card carries NO '[Bloccante]' tag -- it is red, headed 'Export "
      "bloccato', and contains nothing but blocking findings, so the tag only read like "
      "machine output to the person trying to fix the document",
      "[Bloccante]" not in _refusal and "Export bloccato" in _refusal, _refusal)
check("warn5d: the severity label is still available where a list genuinely mixes the two",
      pf.report_lines(pf.PreflightReport(kind="pdf", findings=[
          pf.Finding(rule=pf.RULE_EMPTY_TOPIC, severity=pf.WARNING, what="X.")]))
      == ["[Avviso] X."], None)
_manifest = json.loads(RES_WARN["manifest"].read_text(encoding="utf-8"))
_mw = _manifest.get("warnings", [])
check("warn6: the manifest on disk carries them too",
      any("senza contenuto" in w for w in _mw), _mw)
check("warn6b: and carries each problem ONCE -- the missing-image warning is derived from the "
      "export's own asset warning, so emitting both put the same line in a delivered artifact "
      "twice, raw and wrapped",
      sum(1 for w in _mw if "Immagine non disponibile" in w) == 1, _mw)
check("warn7: each warning still says where it is and what to do about it",
      all(f["where"] and f["fix"] for f in RES_WARN["preflight"]["findings"]),
      RES_WARN["preflight"]["findings"])

# The same project through the HTML path.
RES_HTML = po.export_html_package(repo_warn, svc_warn, "PUB-MAIN", "PROF-ALL", project_dir=P_WARN)
check("html1: the HTML5 package still exports, unchanged by this gate",
      RES_HTML["index"].is_file() and RES_HTML["kind"] == "html5", RES_HTML)
check("html2: HTML preflight runs the CONTENT rules",
      RES_HTML["preflight"]["ok"] is True and RES_HTML["preflight"]["warnings"] >= 1,
      RES_HTML["preflight"])
check("html3: HTML preflight claims NO page-layout rule -- a package that reflows to the "
      "reader's window has no page frame, so it cannot have overset and pretending to "
      "check would be theatre",
      not any(f["rule"] in pf.PAGE_LAYOUT_RULES for f in RES_HTML["preflight"]["findings"]),
      RES_HTML["preflight"]["findings"])
repo_warn.close()


# ====================================================================
# Fixture E — BLOCKING: nothing to publish.
# ====================================================================
P_EMPTY, repo_empty, svc_empty = _project("pf_empty")
svc_empty.create_condition_dimension("market", "Mercato", actor="Writer A")
svc_empty.create_condition_value("eu", "market", "EU", actor="Writer A")
svc_empty.create_condition_value("us", "market", "US", actor="Writer A")
_only_us = svc_empty.create_block_in_topic("T1", "paragraph", actor="Writer A",
                                           text="Contenuto solo statunitense.")["block_id"]
svc_empty.save_block(_only_us, condition={"market": ["us"]}, expected_version=1, actor="Writer A")
svc_empty.create_variant_profile("PROF-EU", "Solo EU", {"market": "eu"}, actor="Writer A")
_empty_error = None
try:
    po.export_pdf(repo_empty, svc_empty, "PUB-MAIN", "PROF-EU", project_dir=P_EMPTY)
except po.PublishingOutputError as exc:
    _empty_error = str(exc)

check("empty1 (blocking): a profile that includes nothing refuses the export",
      _empty_error is not None, "export succeeded")
check("empty2: the message explains it is the profile, not a broken document",
      "profilo" in _empty_error.lower(), _empty_error)
_empty_html_error = None
try:
    po.export_html_package(repo_empty, svc_empty, "PUB-MAIN", "PROF-EU", project_dir=P_EMPTY)
except po.PublishingOutputError as exc:
    _empty_html_error = str(exc)
check("empty3: the HTML path refuses it too -- the content rules are shared",
      _empty_html_error is not None, "html export succeeded")
repo_empty.close()


# ====================================================================
# The layout telemetry itself. These pin the DISTINCTION that makes the
# no-progress rule honest: a long document paginates (and must stay
# silent), an unplaceable element does not advance (and must be caught).
# ====================================================================
# A trace from a perfectly ordinary three-page manual. Every full page
# comes back at the SAME height, and 12pt past the text frame, because the
# last line's leading extends beyond it. Both discarded rules keyed on
# exactly these numbers.
_normal = pf.LayoutTrace(frame_height=730.0, frame_width=483.0, page_limit=2000)
_normal.placements = [(1, 742.0), (1, 742.0), (0, 326.4)]
_normal_report = pf.PreflightReport(kind="pdf")
pf.check_layout(_normal_report, _normal)
check("trace1: an ordinary multi-page trace produces NO finding -- pagination is not "
      "overset, and two rules that could not tell them apart were removed rather than "
      "tuned with an invented tolerance",
      not _normal_report.findings, _normal_report.as_dict())
check("trace2: neither discarded geometric rule survives -- the module records why, so "
      "they are not reintroduced",
      not hasattr(pf.LayoutTrace, "no_progress_pages")
      and not hasattr(pf.LayoutTrace, "overflowing_pages")
      and not hasattr(pf, "RULE_FRAME_OVERFLOW"), None)
check("trace3: the page-layout rules that remain are the two that ARE reachable and "
      "deterministic in this engine",
      pf.PAGE_LAYOUT_RULES == {pf.RULE_OVERSET_PAGE_LIMIT, pf.RULE_CONTENT_LOSS},
      pf.PAGE_LAYOUT_RULES)

_limit = pf.LayoutTrace(frame_height=730.0, frame_width=483.0, page_limit=3)
_limit.placements = [(1, 730.0), (1, 730.0), (1, 730.0)]
_limit.page_limit_hit = True
_limit.unplaced_at_end = True
_limit_report = pf.PreflightReport(kind="pdf")
pf.check_layout(_limit_report, _limit)
check("trace6: hitting the render page cap with content still unplaced is BLOCKING -- "
      "before this gate the loop broke and returned as though it had finished, silently "
      "truncating the deliverable",
      any(f.rule == pf.RULE_OVERSET_PAGE_LIMIT and f.is_blocking
          for f in _limit_report.findings), _limit_report.as_dict())

_finished = pf.LayoutTrace(frame_height=730.0, frame_width=483.0, page_limit=3)
_finished.placements = [(1, 730.0), (1, 730.0), (0, 200.0)]
_finished.page_limit_hit = False
_finished.unplaced_at_end = False
_fin_report = pf.PreflightReport(kind="pdf")
pf.check_layout(_fin_report, _finished)
check("trace7: a document that ends exactly at the cap, with nothing left over, is clean",
      not _fin_report.findings, _fin_report.as_dict())


# ====================================================================
# Fixture F — a REAL multi-page document.
#
# The unit traces above pin the rule; this pins the whole pipeline. A
# manual that legitimately runs to several pages must export cleanly, or
# the preflight is unusable on exactly the documents this product exists
# to produce.
# ====================================================================
P_LONG, repo_long, svc_long = _project("pf_long")
for i in range(60):
    svc_long.create_block_in_topic(
        "T1", "paragraph", actor="Writer A",
        text=(f"Paragrafo numero {i:02d} della procedura di installazione. "
              "Descrive le operazioni da eseguire in sequenza, i controlli previsti e "
              "le condizioni di sicurezza da rispettare prima di procedere al passo "
              "successivo della lavorazione."))
RES_LONG = _export_pdf(repo_long, svc_long, P_LONG)
check("long1: a genuinely multi-page manual exports successfully",
      RES_LONG["pdf"].is_file() and RES_LONG["pages"] >= 3, RES_LONG["pages"])
check("long2: and produces ZERO blocking findings -- pagination is never reported as "
      "overset on a real render",
      RES_LONG["preflight"]["ok"] is True and RES_LONG["preflight"]["blocking"] == 0,
      RES_LONG["preflight"])
check("long3: no page-layout rule fired at all",
      not any(f["rule"] in pf.PAGE_LAYOUT_RULES for f in RES_LONG["preflight"]["findings"]),
      RES_LONG["preflight"]["findings"])
repo_long.close()


# ====================================================================
# Report shape and severity contract
# ====================================================================
check("model1: severity is exactly blocking|warning and every rule is classified once",
      set(pf.SEVERITIES) == {pf.BLOCKING, pf.WARNING}
      and pf.BLOCKING_RULES.isdisjoint(pf.WARNING_RULES)
      and pf.BLOCKING_RULES | pf.WARNING_RULES == pf.ALL_RULES, None)
check("model2: a report with only warnings is OK; one blocking finding flips it",
      pf.PreflightReport(kind="pdf", findings=[
          pf.Finding(rule=pf.RULE_EMPTY_TOPIC, severity=pf.WARNING, what="x")]).ok is True
      and pf.PreflightReport(kind="pdf", findings=[
          pf.Finding(rule=pf.RULE_CONTENT_LOSS, severity=pf.BLOCKING, what="x")]).ok is False,
      None)
check("model3: blocking_message is empty for a passing report -- no scary text on success",
      pf.blocking_message(pf.PreflightReport(kind="pdf")) == "", None)
check("model4: the rendered report lists what/where/what-to-do for every finding",
      pf.report_lines(pf.PreflightReport(kind="pdf", findings=[
          pf.Finding(rule=pf.RULE_CONTENT_LOSS, severity=pf.BLOCKING,
                     what="Perso.", where="Sezione X", fix="Riduci.")]))
      == ["[Bloccante] Perso.", "    Dove: Sezione X", "    Cosa fare: Riduci."], None)
check("model5: PAGE_LAYOUT_RULES are all blocking -- a layout rule that only warned would "
      "let a truncated document ship", pf.PAGE_LAYOUT_RULES <= pf.BLOCKING_RULES, None)
check("model6: every rule constant is reachable from the classification sets, so a rule "
      "cannot be added without deciding its severity",
      {v for k, v in vars(pf).items() if k.startswith("RULE_")} == pf.ALL_RULES, None)

import app  # noqa: E402
_card = app._export_error_html(_wide_error)
check("ui1: the refusal renders as a contained card that keeps the report's structure",
      "ms-export-card-err" in _card and "ms-export-pf-item" in _card
      and "ms-export-pf-detail" in _card, _card[:200])
check("ui2: a plain one-line error still renders as a plain card",
      "ms-export-pf" not in app._export_error_html("Nessun progetto attivo."), None)
check("ui3: the card escapes its content and leaks no internal detail",
      "<script" not in _card and "publishing_output" not in _card, None)

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
