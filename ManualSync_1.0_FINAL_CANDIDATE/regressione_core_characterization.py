"""CP3.1 regression: characterization of the components that will become erylos_core.

A characterization test does not say what the behaviour SHOULD be -- it pins
what it IS at the CP3.1 baseline (commit ae2d7b2), so that every extraction
step from CP3.2 on (moving modules, adding ports, splitting the repository)
can prove it changed nothing. Quirks are pinned too, on purpose: fixing one
is a product decision for its own checkpoint, not a side effect of a move.

How
---
* Every fixture is synthetic and lives in a temporary folder (inside the
  isolated test home), removed at the end. No canonical demo, no real data,
  no network. The only child process is a 10-line local JSON echo used to
  exercise the ExternalJsonCommandProvider contract.
* Each observed output is normalised -- timestamps -> <TS>, generated ids
  (BLK-XXXXXXXXXX, uuid, 32-hex) -> <ID1>, <ID2>... per output, temp paths ->
  <TMP> -- serialised as sorted JSON and compared with a pinned sha256
  (GOLDEN). Readable assertions on the key fields sit next to every digest,
  so a failure says WHAT moved, and the digest says THAT something moved.
* The run is deterministic: two consecutive runs produced byte-identical
  normalised output before the digests were pinned.

Re-pin only after a reviewed, intentional behaviour change:
    ..\\.venv\\Scripts\\python.exe regressione_core_characterization.py --print-golden

Usage:  ..\\.venv\\Scripts\\python.exe regressione_core_characterization.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import dataclasses
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

import domain_errors as de  # noqa: E402
import language_codes as lc  # noqa: E402
import linguistic_safety as ls  # noqa: E402
import rich_text as rt  # noqa: E402
import publishing_preflight as pf  # noqa: E402
import impact_intelligence as ii  # noqa: E402
import translation_intelligence as ti  # noqa: E402
import publishing_preview as pp  # noqa: E402
import release_readiness  # noqa: E402
import generative_orchestrator as go  # noqa: E402
from authoring_service import AuthoringService, VersionConflictError  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from compliance_intelligence import ComplianceService  # noqa: E402
from grounded_authoring import GroundingStore  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

PRINT_GOLDEN = "--print-golden" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="erylos_cp31_char_"))
ACTOR = "Char Suite"
_results: list[tuple[str, bool, str]] = []
_observed: dict[str, str] = {}


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:600]))
    if not PRINT_GOLDEN:
        print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:600]}]"))


# ------------------------------------------------------------------ normalisation
def plain(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return plain(dataclasses.asdict(o))
    if isinstance(o, pd.DataFrame):
        return plain(o.to_dict(orient="records"))
    if isinstance(o, dict):
        return {str(k): plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [plain(x) for x in o]
    if isinstance(o, (set, frozenset)):
        return sorted(plain(x) for x in o)
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "keys") and hasattr(o, "__getitem__"):        # sqlite3.Row
        return {k: plain(o[k]) for k in o.keys()}
    if isinstance(o, float) and o != o:
        return "NaN"
    return o


TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")
ID_RE = re.compile(r"\b[A-Z]{2,}-[0-9A-F]{8,}\b|\b[0-9a-f]{32}\b"
                   r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def normalized(o) -> str:
    text = json.dumps(plain(o), ensure_ascii=False, sort_keys=True, default=str)
    for root in (str(TMP), str(test_isolation.TEST_HOME)):
        text = text.replace(json.dumps(root)[1:-1], "<TMP>").replace(root, "<TMP>")
    ids: dict[str, str] = {}
    return ID_RE.sub(lambda m: ids.setdefault(m.group(0), f"<ID{len(ids) + 1}>"), TS_RE.sub("<TS>", text))


def snap(label: str, o) -> str:
    """Pin the normalised output `o` under `label` (digest compared at the end)."""
    text = normalized(o)
    _observed[label] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text


# =================================================================== 1. domain_errors
codes = list(de.ALL_CODES)
check("DE1: 29 migrated/authoring/impact/translation codes, all distinct, ERR_UNKNOWN outside them",
      len(codes) == 29 and len(set(codes)) == 29 and de.ERR_UNKNOWN not in codes, len(codes))
try:
    raise de.DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
except de.DomainError as e:
    exc = e
check("DE2: DomainValidationError carries its code and is still a ValueError (callers catch ValueError)",
      getattr(exc, "code", None) == de.ERR_AUTHORING_ACTOR_REQUIRED and isinstance(exc, ValueError), exc)
check("DE3: the not-found / exists / file-missing errors keep their builtin bases",
      issubclass(de.DomainNotFoundError, KeyError) and issubclass(de.DomainAlreadyExistsError, FileExistsError)
      and issubclass(de.DomainFileMissingError, FileNotFoundError), "")
snap("domain_errors", {"codes": sorted(codes), "exc": [type(exc).__name__, exc.code, str(exc)]})

# =================================================================== 2. language_codes
lang_in = ["it", "IT_it", "en", "en-gb", "de_DE", "", None, "zz"]
lang_out = [lc.canonical_lang(x) for x in lang_in]
check("LC1: canonical_lang normalises region/case/underscore and leaves bare or unknown codes alone",
      lang_out == ["it", "it-IT", "en", "en-GB", "de-DE", "", "", "zz"], lang_out)
check("LC2: canonical_langs splits ', ;' lists, dedups and sorts",
      lc.canonical_langs("it, en-US;de") == ["de", "en-US", "it"], lc.canonical_langs("it, en-US;de"))
snap("language_codes", [lang_out, lc.canonical_langs("it, en-US;de"), lc.canonical_langs(["en_us", "EN-us"])])

# =================================================================== 3. linguistic_safety
nt = [ls.is_nontranslatable_text(x) for x in ["M8", "IP65", "Avvertenza", "", "230 V"]]
check("LS1: codes/numbers/empty are non-translatable, words are not", nt == [True, True, False, True, True], nt)
issue = ls.cross_language_issue("Warning: hot surface", "Warning: hot surface", "en-US", "it-IT")
check("LS2: an untranslated target across different languages is reported (Italian message)",
      "target identico alla sorgente" in issue, issue)
check("LS3: same text, same language: no issue", ls.cross_language_issue("Ciao", "Ciao", "it-IT", "it-IT") == "",
      ls.cross_language_issue("Ciao", "Ciao", "it-IT", "it-IT"))
snap("linguistic_safety", [nt, issue, ls.collect_cross_language_issues(
    "The machine stops.", "The machine stops.", "en-US", "de-DE")])

# =================================================================== 4. rich_text
RAW = "**Avvertenza**: non *aprire*\n\n- uno\n- due\n\n1. primo\n2. secondo [link](https://example.com) `M8`"
html = rt.render_html(RAW, "rich")
check("RT1: rich markup renders as strong/code/safe link; lists stay inline text in one paragraph",
      html.startswith("<p><strong>Avvertenza</strong>") and 'rel="noopener noreferrer"' in html
      and "<code>M8</code>" in html and "<ul>" not in html, html)
check("RT2: plain_text drops the markup", rt.plain_text(RAW, "rich").startswith("Avvertenza: non"), rt.plain_text(RAW, "rich"))
check("RT3: html_to_canonical keeps bold/italic/breaks and turns <script> into inert text",
      rt.html_to_canonical("<p><b>Bold</b> and <i>it</i><br>x</p><script>alert(1)</script>") == "**Bold** and _it_\nx\nalert(1)",
      rt.html_to_canonical("<p><b>Bold</b> and <i>it</i><br>x</p><script>alert(1)</script>"))
check("RT4: parse() output validates", rt.validate(rt.parse(RAW)) == (True, []), rt.validate(rt.parse(RAW)))
snap("rich_text", {"parse": rt.parse(RAW), "html": html, "plain": rt.plain_text(RAW, "rich"),
                   "normalize": rt.normalize(RAW, "rich"), "editable": rt.render_editable_html(RAW, "rich"),
                   "h2c": rt.html_to_canonical("<p><b>Bold</b> and <i>it</i><br>x</p><script>alert(1)</script>"),
                   "diff": rt.diff_summary("**a** b", "**a** c"), "rich?": rt.has_rich_structure(RAW)})

# =================================================================== 5. publishing_preflight
empty = pf.check_model({"topics": [], "publication_title": "P"}, kind="pdf")
only_titles = pf.check_model({"topics": [{"title": "A", "blocks": []}], "publication_title": "P",
                              "profile_name": "X"}, kind="pdf")
one_empty = pf.check_model({"topics": [{"title": "A", "blocks": [{}]}, {"title": "B", "blocks": []}]}, kind="html5")
check("PF1: no topics -> blocking EMPTY_PUBLICATION, export refused",
      not empty.ok and [f.rule for f in empty.findings] == [pf.RULE_EMPTY_PUBLICATION], empty.as_dict())
check("PF2: only empty topics -> blocking EMPTY_PUBLICATION", not only_titles.ok, only_titles.as_dict())
check("PF3: one empty topic among others -> WARNING EMPTY_TOPIC, export allowed",
      one_empty.ok and [f.rule for f in one_empty.findings] == [pf.RULE_EMPTY_TOPIC], one_empty.as_dict())
rep = pf.PreflightReport(kind="pdf")
pf.check_layout(rep, pf.LayoutTrace(frame_height=700.0, placements=[(1, 700.0)] * 3, page_limit_hit=True,
                                    page_limit=3, unplaced_at_end=True))
pf.check_assets(rep, ["figura 2: file assente"])
pf.check_content_survived(rep, [{"where": "Sezione «Dati»", "token": "x"}, {"where": "y", "token": "z"}])
check("PF4: overset + lost content are blocking, a missing asset is only a warning",
      len(rep.blocking) == 2 and len(rep.warnings) == 1
      and {f.rule for f in rep.findings} == {pf.RULE_OVERSET_PAGE_LIMIT, pf.RULE_ASSET_MISSING, pf.RULE_CONTENT_LOSS},
      rep.as_dict())
snap("publishing_preflight", {"empty": empty.as_dict(), "titles": only_titles.as_dict(), "one": one_empty.as_dict(),
                              "layout": rep.as_dict(), "lines": pf.report_lines(rep), "msg": pf.blocking_message(rep),
                              "manifest": pf.manifest_entries(rep)})

# =================================================================== fixture project (synthetic)
project = create_project(TMP, project_name="Char", source_lang="it-IT", target_langs=["en-US"])
check("PL1: create_project lays out CCMS + Workflow databases, manifest and assets folder",
      sorted(p.name for p in project.iterdir()) == ["ManualSync_CCMS.sqlite", "ManualSync_Project.json",
                                                    "ManualSync_Workflow.sqlite", "ManualSync_Workflow_Assets"],
      sorted(p.name for p in project.iterdir()))
repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
repo.create_topic("T-B", "Manutenzione", "", source_lang="it-IT", status="Draft")
repo.create_publication("PUB-MAIN", "Manuale di prova", ["T-A", "T-B"], source_lang="it-IT")

# =================================================================== 6. authoring_service
svc = AuthoringService(repo)
b1 = svc.create_block_in_topic("T-A", "paragraph", actor=ACTOR, text="La macchina lavora profili in alluminio.")
b2 = svc.create_block_in_topic("T-B", "warning", actor=ACTOR, text="Scollegare l'alimentazione prima della manutenzione.")
saved = svc.save_block(b1["block_id"], text="La macchina lavora profili in **alluminio**.", expected_version=1, actor=ACTOR)
check("AS1: save with the right expected_version creates version 2 and stays Draft",
      saved["version"] == 2 and saved["status"] == "Draft" and saved["review_tasks_invalidated"] == 0, saved)
try:
    svc.save_block(b1["block_id"], text="x", expected_version=1, actor=ACTOR)
    conflict = None
except VersionConflictError as e:
    conflict = str(e)
check("AS2: a stale expected_version raises VersionConflictError (optimistic concurrency)", conflict, conflict)
try:
    svc.save_block(b1["block_id"], text="x", expected_version=2, actor="  ")
    no_actor = None
except de.DomainValidationError as e:
    no_actor = e.code
check("AS3: a blank actor is refused with ERR_AUTHORING_ACTOR_REQUIRED", no_actor == de.ERR_AUTHORING_ACTOR_REQUIRED, no_actor)
svc.save_draft("block", b1["block_id"], base_version=2, payload={"text": "bozza"}, actor=ACTOR)
draft = svc.load_draft("block", b1["block_id"])
check("AS4: a draft is kept outside the version history",
      draft and len(svc.block_history(b1["block_id"])) == 2, [draft, svc.block_history(b1["block_id"])])
svc.discard_draft("block", b1["block_id"])
svc.create_condition_dimension("model", "Modello", actor=ACTOR)
svc.create_condition_value("mod_a", "model", "Modello A", actor=ACTOR)
svc.create_condition_value("mod_b", "model", "Modello B", actor=ACTOR)
prof = svc.create_variant_profile("prof_a", "Profilo A", {"model": "mod_a"}, actor=ACTOR)
cond_saved = svc.save_block(b2["block_id"], condition={"model": ["mod_b"]}, expected_version=1, actor=ACTOR)
ev = [svc.evaluate_conditions(c, {"model": "mod_a"}) for c in (
    {"field": "model", "op": "in", "value": ["mod_a"]}, {"field": "model", "op": "in", "value": ["mod_b"]},
    {"field": "nope", "op": "in", "value": ["x"]}, None, {"any": []})]
check("AS5: conditions: match -> included, mismatch -> excluded, unknown dimension -> unresolved (fail-closed), "
      "none -> included, any/not -> 'too complex' and excluded",
      [e["included"] for e in ev] == [True, False, False, True, False] and ev[2]["unresolved_dimensions"] == ["nope"],
      ev)
svc.archive_block(b2["block_id"], actor=ACTOR)
restored = svc.restore_block(b2["block_id"], actor=ACTOR)
reverted = svc.revert_block_to_version(b1["block_id"], 1, actor=ACTOR)
check("AS6: revert never rewrites history -- it creates version 3 with version 1's text",
      reverted.get("new_version") == 3 and reverted.get("reverted_to_content_of_version") == 1 and svc.get_block_version(b1["block_id"], 3)["text"] == "La macchina lavora profili in alluminio.",
      reverted)
svc.save_block(b1["block_id"], text="La macchina lavora profili in **alluminio**.", expected_version=3, actor=ACTOR)
bulk = svc.bulk_approve_publication("PUB-MAIN", actor=ACTOR)
check("AS7: bulk approval approves both topics", bulk == {"review_tasks_closed": 0, "topics_approved": 2}, bulk)
snap("authoring_service", {"b1": b1, "saved": saved, "conflict": conflict, "draft": draft, "profile": prof,
                           "cond_saved": cond_saved, "eval": ev, "restored": restored, "reverted": reverted,
                           "history": svc.block_history(b1["block_id"]), "bulk": bulk,
                           "dims": svc.list_condition_dimensions(), "values": svc.list_condition_values()})

# =================================================================== 7. compliance_intelligence
comp = ComplianceService(repo)
comp.create_instrument("INST-TEST", "TEST/1", actor=ACTOR, kind="directive", title="Direttiva di prova")
comp.create_requirement("REQ-1", "Requisito di prova", actor=ACTOR, instrument_id="INST-TEST", article_ref="Art.1",
                        description="d")
comp.create_scope("SCOPE-1", publication_id="PUB-MAIN", title="Ambito", jurisdiction="EU", actor=ACTOR)
comp.add_scope_reference("SCOPE-1", ref_kind="instrument", ref_id="INST-TEST", actor=ACTOR)
comp.set_applicability("SCOPE-1", "REQ-1", "APPLIES", actor=ACTOR, justification="test")
status0 = comp.compute_status("SCOPE-1", "REQ-1")
ready0 = comp.compliance_readiness_for_publication("PUB-MAIN")
try:
    comp.set_assessment("SCOPE-1", "REQ-1", "COVERED", actor=ACTOR, comment="senza evidenza")
    covered_without_evidence = None
except de.DomainError as e:
    covered_without_evidence = e.code
comp.add_evidence("EV-1", "block", actor=ACTOR, object_id=b1["block_id"], object_version=4, description="Evidenza")
comp.link_evidence_requirement("EV-1", "REQ-1", actor=ACTOR)
comp.set_assessment("SCOPE-1", "REQ-1", "COVERED", actor=ACTOR, comment="ok")
status1 = comp.compute_status("SCOPE-1", "REQ-1")
ready1 = comp.compliance_readiness_for_publication("PUB-MAIN")
check("CI1: an applicable requirement without assessment is 'To verify' and blocks readiness",
      status0 == "To verify" and ready0["compliance_ready"] is False and len(ready0["blocking_items"]) == 1, ready0["counts"])
check("CI2: COVERED without evidence is refused (ERR_COVERED_REQUIRES_EVIDENCE)",
      covered_without_evidence == de.ERR_COVERED_REQUIRES_EVIDENCE, covered_without_evidence)
check("CI3: with linked evidence + COVERED the publication is compliance-ready",
      status1 == "Covered" and ready1["compliance_ready"] is True and not ready1["blocking_items"], ready1["counts"])
snap("compliance_intelligence", {"s0": status0, "r0": ready0, "s1": status1, "r1": ready1,
                                 "summary": comp.summary_for_scope("SCOPE-1"),
                                 "impact": comp.change_impact_for_requirement("REQ-1")})
repo.close()

# =================================================================== 8. impact_intelligence
impact = ii.assess_object_impact(str(project), "block", b1["block_id"])
codes_seen = [r.code for r in impact.records]
check("II1: a block's impact names its publication, topic, profile and the CURRENT compliance evidence",
      {"IMPACT_CONTENT_IN_PUBLICATION", "IMPACT_CONTENT_IN_TOPIC", "IMPACT_PROFILE_INCLUDES",
       "IMPACT_COMPLIANCE_EVIDENCE_CURRENT"} <= set(codes_seen) and impact.counts["publications"] == 1, codes_seen)
snap("impact_intelligence", impact)

# =================================================================== 9. translation_intelligence
trans = ti.assess_translation_intelligence(str(project), b1["block_id"])
check("TI1: the configured target language with no translation is MISSING",
      trans.counts == {"configured": 1, "fresh": 0, "missing": 1, "present": 0, "stale": 0}
      and trans.languages[0].status == "MISSING", trans.counts)
snap("translation_intelligence", trans)

# =================================================================== 10. release_readiness
ready = release_readiness.assess_release_readiness(str(project), "PUB-MAIN", target_langs=["en-US"])
check("RR1: release readiness FAILS on missing en-US translations while compliance is ready",
      ready["release_ready"] is False and ready["compliance"]["compliance_ready"] is True
      and ready["summary"]["source_ready"] is True and ready["summary"]["target_languages_blocked"] == 1
      and {i["translation_status"] for i in plain(ready["blocking_items"])} == {"MISSING"}, ready["summary"])
snap("release_readiness", ready)

# =================================================================== 11. publishing_preview
repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
svc = AuthoringService(repo)
model = pp.build_preview_model(repo, svc, "PUB-MAIN", "prof_a")
check("PP1: profile prof_a includes the unconditioned block and excludes the mod_b-only warning",
      model["summary"] == {"blocks_excluded": 1, "blocks_included": 1, "topics_excluded": 0, "topics_included": 2,
                           "unresolved": 0}, model["summary"])
try:
    pp.build_preview_model(repo, svc, "PUB-MAIN", "nope")
    missing_profile = None
except KeyError as e:
    missing_profile = type(e).__name__
check("PP2: an unknown profile raises KeyError (never a silent default)", missing_profile == "KeyError", missing_profile)
preview_html = pp.render_preview_html(model)
snap("publishing_preview", {"model": model, "html": preview_html, "html_excluded": pp.render_preview_html(model, show_excluded=True)})
repo.close()

# =================================================================== 12. generative_orchestrator (+ grounding)
gs = GroundingStore(TMP / "ManualSync_Grounding.sqlite")
gs.ingest_text("DOC1", "Manuale", "Prima della manutenzione scollegare l'alimentazione. Attendere 5 minuti per la "
                                  "scarica dei condensatori. Coppia di serraggio M8: 25 Nm.")
ledger = go.GenerationLedger(TMP / "ManualSync_Generation.sqlite")
orch = go.GenerativeOrchestrator(gs, ledger, go.SafeSourceComposer())
proc = orch.propose(go.GenerationRequest(instruction="Come scollegare l'alimentazione prima della manutenzione?",
                                         mode="procedure"))
para = orch.propose(go.GenerationRequest(instruction="Coppia di serraggio M8?", mode="paragraph"))
none = orch.propose(go.GenerationRequest(instruction="Qual e il colore del logo aziendale?", mode="paragraph"))
check("GO1: the shipped provider is the offline SafeSourceComposer; settings default to it",
      orch.provider.name == "safe-source-composer"
      and isinstance(go.provider_from_settings(""), go.SafeSourceComposer), orch.provider.name)
check("GO2 (pinned quirk): procedure mode adds 'Step 1:' and the number '1' is not in the source, so the "
      "grounding gate BLOCKS an otherwise extractive draft",
      proc["status"] == "BLOCKED" and proc["draft"].startswith("Step 1:") and proc["validation"]["PASS"] is False,
      [proc["status"], proc["draft"]])
check("GO3: paragraph mode on a supported question passes the gate with a citation",
      para["validation"]["PASS"] is True and para["citations"], [para["status"], para["draft"], para["citations"]])
check("GO4: no evidence -> NEEDS_SOURCE, nothing recorded", none["status"] == "NEEDS_SOURCE" and none["proposal_id"] == "",
      none["status"])
try:
    go.ExternalJsonCommandProvider("")
    empty_cmd = None
except ValueError as e:
    empty_cmd = str(e)
check("GO5: an external provider without command is refused", empty_cmd == "Comando provider vuoto", empty_cmd)
echo = TMP / "echo_provider.py"
echo.write_text("import json, sys\np = json.load(sys.stdin)\n"
                "ev = p.get('evidence', [])\n"
                "print(json.dumps({'draft': ev[0]['text'] if ev else '', 'citations': [e['chunk_id'] for e in ev[:1]]}))\n",
                encoding="utf-8")
ext = go.ExternalJsonCommandProvider(f"{Path(sys.executable).as_posix()} -I {echo.as_posix()}", timeout=60)
ext_res = go.GenerativeOrchestrator(gs, ledger, ext).propose(go.GenerationRequest(instruction="Coppia di serraggio M8?",
                                                                                   mode="paragraph"))
check("GO6: the external JSON-command contract: stdin payload -> stdout draft/citations -> same grounding gate",
      ext_res["provider"] == "external-json-command" and ext_res["validation"]["PASS"] is True, ext_res["status"])
proposals = ledger.conn.execute("SELECT COUNT(*) FROM generation_proposals").fetchone()[0]
check("GO7: every gated proposal is recorded in the ledger (3: procedure, paragraph, external)", proposals == 3, proposals)
snap("generative_orchestrator", {"proc": proc, "para": para, "none": none, "ext": ext_res,
                                 "uncertainty": gs.uncertainty_report("Coppia di serraggio M8?")})
gs.close()
ledger.close()

# =================================================================== golden digests
# Pinned at commit ae2d7b2 (CP3.1); three consecutive runs produced these exact values.
GOLDEN = {
    'authoring_service': 'ff423b2d7aa6f4570891c429fbd6eb39feb1f6572b1f28d5bad77bb4ce840ce1',
    'compliance_intelligence': '70863019742673eab699801e76db5ce89d11234804f14a1b3b6f1605a0da36eb',
    'domain_errors': '81ef8404c48d5c0a4192e699b706a2c9d935b5289a2d676f3832443b499c3251',
    'generative_orchestrator': '1339f664ad83f25eadca44f8d1e9748e5b3612626adc927336b776812e3be740',
    'impact_intelligence': '2d2807bed563aa11b379e6a3ca2b2c1e611097cb3fd5cb1a0d12c39eec1e2554',
    'language_codes': '3ec0e66dd475bf152cae2794e4312bd34778e772582a02c34b8fc9c98cfff513',
    'linguistic_safety': 'd722b6618d8ebb89dde30b4bd33b7a40647c1555560ce045692c1f191cdc90a2',
    'publishing_preflight': '836cb94f6585a6eff2cc38ff4a840ba5acbd61b6abc8bf9d7fe6d85a19aeb0dc',
    'publishing_preview': '95de8c9ef083eafc233858fbc353d3253c183b5dfb62dc1ded33267df2b5b926',
    'release_readiness': '3cf566cbf0eefd71c73f5d46315dacd4fa14385b62121daf5506c3b3e742904e',
    'rich_text': '965e9593c33eaba71cddace91e51020e5c1da1445ffb1958d2a8a1421fe6b69e',
    'translation_intelligence': '6fc7db7d85907f088f929229e3076b8fa6b4c6773a3f3adbfb658f1b5be30db8',
}

if PRINT_GOLDEN:
    print("GOLDEN = {")
    for k in sorted(_observed):
        print(f"    {k!r}: {_observed[k]!r},")
    print("}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

for label in sorted(_observed):
    check(f"SNAP {label}: normalised output identical to the CP3.1 baseline",
          GOLDEN.get(label) == _observed[label], f"expected {GOLDEN.get(label)} got {_observed[label]}")
check("SNAP: every pinned component was observed", set(GOLDEN) == set(_observed), sorted(set(GOLDEN) ^ set(_observed)))

# The snapshot can fail, and normalisation hides only what is meant to vary.
_probe = {"block_id": "BLK-0123456789", "text": "Scollegare", "updated_at": "2026-01-01T10:00:00+00:00", "version": 2}
_d = lambda o: hashlib.sha256(normalized(o).encode("utf-8")).hexdigest()  # noqa: E731
check("N1: a changed text, version or status changes the digest",
      len({_d(_probe), _d({**_probe, "text": "Collegare"}), _d({**_probe, "version": 3})}) == 3, "")
check("N2: a different timestamp or generated id does NOT change the digest (determinism)",
      _d(_probe) == _d({**_probe, "updated_at": "2027-05-05T01:02:03.4+00:00", "block_id": "BLK-FEDCBA9876"}), "")
check("N3: a changed observed output is caught against GOLDEN (mutant on the real impact report)",
      _d({**plain(impact), "object_status": "Draft"}) != GOLDEN["impact_intelligence"], "")

shutil.rmtree(TMP, ignore_errors=True)
check("CL1: the synthetic fixtures were removed", not TMP.exists(), TMP)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_CORE_CHARACTERIZATION.json").write_text(json.dumps({
    "suite": "core characterization (CP3.1)", "tests": total, "pass": passed, "fail": total - passed,
    "digests": _observed,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
