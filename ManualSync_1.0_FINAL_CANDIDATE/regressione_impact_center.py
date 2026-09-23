"""ERYLOS Impact Center V1 — regression suite.

Covers impact_center.py (the orchestrator) and impact_center_view.py (the
customer-facing prose), against real projects built through the supported
service APIs. Fixtures are synthetic and disposable; the canonical demo is
never touched.

What this suite is for
----------------------
The Impact Center makes no decisions of its own -- it arranges verdicts
from two services that do. So the tests worth writing are the ones that
would catch it MISARRANGING them: a reopened requirement filed under
"ready", a release called releasable while a translation is stale, a
schema term leaking into a screen meant for a customer, or a view that
quietly writes something.

The release verdict is mutation-tested, because "Pronto al rilascio: SI"
is the single most consequential sentence the product prints.
"""
from __future__ import annotations
import test_isolation as _isolation  # noqa: E402,F401 -- aliased: this suite collects every global named test_* as a test. FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import re
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import authoring_view as av  # noqa: E402
import compliance_view as cv  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402
import impact_center as ic  # noqa: E402
import impact_center_view as icv  # noqa: E402
import translation_intelligence as ti  # noqa: E402
from cat_tm import TranslationMemory  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project, update_project  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="manualsync_impact_center_"))
ACTOR = "Impact Center Suite"
SENTENCE = "La pressione massima ammessa è 12 bar."
TM_VARIANT = "La pressione massima ammessa è 16 bar."

# The engine vocabulary that must never reach a customer-facing panel.
BANNED = ("fuzzy", "source_version", "object_version", "work_bucket")

_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))


# ------------------------------------------------------------------
# Fixture
# ------------------------------------------------------------------

def build(name: str, *, langs=("de-DE", "fr-FR"), approve=False,
          with_tm=False, with_evidence=False) -> tuple[Path, dict]:
    project = create_project(TMP, project_name=name, source_lang="it-IT")
    update_project(project, target_langs=list(langs))
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
        repo.create_topic("T-B", "Manutenzione", "", source_lang="it-IT", status="Draft")
        repo.create_publication("PUB-MAIN", "Manuale Pompa PC-200", ["T-A", "T-B"],
                                source_lang="it-IT")
    finally:
        repo.close()

    repo, auth = av.open_service(str(project))
    facts: dict = {}
    try:
        blk = auth.create_block_in_topic("T-A", "paragraph", actor=ACTOR, text=SENTENCE)
        facts["block_id"] = blk["block_id"]
        tbl = auth.create_table_in_topic("T-A", actor=ACTOR, caption="Coppie di serraggio")
        facts["table_id"] = tbl["table_id"]
        repo.add_block("BLK-SHARED-B", "T-B", "table", position=1,
                       object_ref_id=tbl["table_id"], status="Draft")
    finally:
        repo.close()

    if approve:
        repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
        try:
            rid = repo.request_review("block", facts["block_id"], requested_by=ACTOR,
                                      assigned_to=ACTOR)
            repo.decide_review(rid, decision="APPROVE", actor=ACTOR)
        finally:
            repo.close()

    if with_tm:
        tm_dir = project / "TranslationMemory"
        tm_dir.mkdir(parents=True, exist_ok=True)
        tm = TranslationMemory(tm_dir / "suite.sqlite")
        try:
            tm.add("it-IT", "de-DE", TM_VARIANT,
                   "Der maximal zulaessige Druck betraegt 16 bar.",
                   origin="suite", alignment_confidence=100.0, approved=True)
        finally:
            tm.close()

    if with_evidence:
        crepo, svc = cv.open_service(str(project))
        try:
            svc.create_instrument("INST-T", "TEST/1", actor=ACTOR, kind="directive",
                                  title="Direttiva di prova")
            svc.create_requirement("REQ-1", "Avvertenze sull'uso scorretto", actor=ACTOR,
                                   instrument_id="INST-T", article_ref="Art. 1.7.4",
                                   description="Descrizione")
            svc.create_scope("SCOPE-1", publication_id="PUB-MAIN", title="Ambito",
                             jurisdiction="EU", actor=ACTOR)
            svc.add_scope_reference("SCOPE-1", ref_kind="instrument", ref_id="INST-T", actor=ACTOR)
            svc.set_applicability("SCOPE-1", "REQ-1", "APPLIES", actor=ACTOR, justification="t")
            v = int(crepo.conn.execute("SELECT version FROM content_blocks WHERE block_id=?",
                                       (facts["block_id"],)).fetchone()["version"])
            svc.add_evidence(f"EV-{facts['block_id']}-v{v}", "block", actor=ACTOR,
                             object_id=facts["block_id"], object_version=v,
                             description="Evidenza")
            svc.link_evidence_requirement(f"EV-{facts['block_id']}-v{v}", "REQ-1", actor=ACTOR)
            svc.set_assessment("SCOPE-1", "REQ-1", "COVERED", actor=ACTOR, comment="ok")
        finally:
            crepo.close()
    return project, facts


def add_translation(project: Path, block_id: str, lang: str, source_version: int):
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.conn.execute(
            """INSERT INTO block_translations(block_id,target_lang,source_version,target_text,status,updated_at)
               VALUES(?,?,?,?,?,datetime('now'))""",
            (block_id, lang, int(source_version), "Uebersetzung.", "Reviewed"))
        repo.conn.commit()
    finally:
        repo.close()


def version_of(project: Path, block_id: str) -> int:
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        return int(repo.conn.execute("SELECT version FROM content_blocks WHERE block_id=?",
                                     (block_id,)).fetchone()["version"])
    finally:
        repo.close()


def save_block(project: Path, block_id: str, text: str):
    repo, auth = av.open_service(str(project))
    try:
        d = av.block_detail(auth, block_id)
        return auth.save_block(block_id, text=text, expected_version=int(d["version"]),
                               actor=ACTOR)
    finally:
        repo.close()


def rendered(report, lang: str) -> str:
    """Every customer-facing panel, as TEXT -- what a reader actually sees.

    Tags removed and entities resolved, because the view escapes its
    output: an assertion against raw HTML looks for "l'approvazione" and
    finds "l&#x27;approvazione", so a correct string reads as a failure
    (and, worse, a missing string could read as a pass).
    """
    import html as _html
    with i18n.use_language(lang):
        header, changes, attention, ready, _adv = icv.render(report)
    markup = header + changes + attention + ready
    return _html.unescape(re.sub(r"<[^>]+>", " ", markup))


# ==================================================================
# 1. The changed content
# ==================================================================

def test_identifies_the_changed_object():
    project, f = build("g1_object")
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    check("the object is identified by kind", rep.object_type == "block", rep.object_type)
    check("...at its current version", rep.object_version == 2, rep.object_version)
    check("...with the previous version reported", rep.previous_version == 1,
          rep.previous_version)
    check("...labelled by its text, not its id",
          rep.object_id not in rep.object_label and "pressione" in rep.object_label.lower(),
          rep.object_label)
    check("the last modification is attributed", rep.last_modified_by == ACTOR,
          rep.last_modified_by)
    check("...and dated", len(rep.last_modified_at) >= 10, rep.last_modified_at)


def test_object_type_auto_and_explicit():
    project, f = build("g1_kind")
    auto = ic.assess_impact_center(str(project), f["block_id"])
    explicit = ic.assess_impact_center(str(project), f["block_id"], object_type="block")
    check("the kind is detected when not given", auto.object_type == "block", auto.object_type)
    check("explicit and auto agree", auto.to_dict() == explicit.to_dict(), "")


def test_missing_object_raises():
    project, f = build("g1_missing")
    try:
        ic.assess_impact_center(str(project), "NOPE-404")
        check("an unknown object raises", False, "no exception")
    except de.DomainNotFoundError as exc:
        check("an unknown object raises DomainNotFoundError",
              exc.code == de.ERR_CONTENT_OBJECT_NOT_FOUND, exc.code)


# ==================================================================
# 2. Documentation impact
# ==================================================================

def test_documentation_impact():
    project, f = build("g2_docs")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    check("the manual is reported", len(rep.publications) == 1, rep.publications)
    check("...by title, never by id",
          rep.publications and rep.publications[0].label == "Manuale Pompa PC-200",
          rep.publications[0].label if rep.publications else None)
    check("the chapter is reported by title",
          any(r.label == "Uso previsto" for r in rep.topics), [r.label for r in rep.topics])
    check("a text block reports reuse as not supported", rep.reuse_supported is False,
          rep.reuse_supported)


def test_shared_object_reports_every_reuse_location():
    project, f = build("g2_reuse")
    rep = ic.assess_impact_center(str(project), f["table_id"], object_type="table")
    check("reuse is supported for a table", rep.reuse_supported is True, "")
    check("both placements are reported", len(rep.reuse_locations) == 2,
          [r.label for r in rep.reuse_locations])
    check("both chapters are reached", len(rep.topics) == 2, [r.label for r in rep.topics])
    check("profiles are not determinable for a table",
          rep.profiles_determinable is False, rep.profiles_determinable)


def test_variant_profiles_reported():
    project, f = build("g2_variants")
    repo, auth = av.open_service(str(project))
    try:
        auth.create_condition_dimension("mercato", "Mercato", actor=ACTOR)
        auth.create_condition_value("eu", "mercato", "Europa", actor=ACTOR)
        auth.create_variant_profile("P-EU", "Mercato Europa", {"mercato": "eu"}, actor=ACTOR)
        d = av.block_detail(auth, f["block_id"])
        auth.save_block(f["block_id"], condition={"mercato": ["eu"]},
                        expected_version=int(d["version"]), actor=ACTOR)
    finally:
        repo.close()
    rep = ic.assess_impact_center(str(project), f["block_id"])
    check("the affected variant is named", [r.label for r in rep.profiles] == ["Mercato Europa"],
          [r.label for r in rep.profiles])
    check("profiles are determinable for a block", rep.profiles_determinable is True, "")


# ==================================================================
# 3. Translation impact
# ==================================================================

def test_translation_states_are_grouped_correctly():
    project, f = build("g3_translations")
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    got = {l.language: l.status for l in rep.languages}
    check("the edited source makes German stale", got.get("de-DE") == ti.STATUS_STALE, got)
    check("the untranslated language is missing", got.get("fr-FR") == ti.STATUS_MISSING, got)

    it_text = rendered(rep, "it")
    check("the stale language appears under 'needs attention'",
          "Cosa richiede attenzione" in it_text and "tedesca" in it_text, "")
    check("...and NOT under 'what is ready'",
          it_text.index("Cosa richiede attenzione") < it_text.index("Cosa è pronto"), "")
    check("the reason is human-readable",
          "modificat" in it_text.lower(), "")


def test_fresh_translation_is_reported_as_ready():
    project, f = build("g3_fresh")
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    rep = ic.assess_impact_center(str(project), f["block_id"])
    check("German is fresh",
          rep.translation.language("de-DE").status == ti.STATUS_FRESH, "")
    it_text = rendered(rep, "it")
    ready_part = it_text[it_text.index("Cosa è pronto"):]
    check("the fresh language appears in the ready section",
          "tedesca" in ready_part, ready_part[:180])


def test_blocked_reuse_reason_surfaces_when_requested():
    project, f = build("g3_blocked", approve=True, with_tm=True)
    shallow = ic.assess_impact_center(str(project), f["block_id"])
    check("without the translation analysis there are no reuse reasons",
          shallow.translation_reasons == (), shallow.translation_reasons)
    deep = ic.assess_impact_center(str(project), f["block_id"],
                                   include_translation_detail=True)
    codes = [r.code for r in deep.translation_reasons]
    check("with it, the numeric block is explained",
          ti.TM_BLOCKED_NUMERIC_MISMATCH in codes, codes)
    it_text = rendered(deep, "it")
    check("the explanation reaches the screen",
          "Riutilizzo bloccato" in it_text, it_text[-400:])
    check("...naming the memory value and the content value",
          "16" in it_text and "12" in it_text, "")


def test_requested_translation_analysis_that_cannot_run_says_so():
    """A Draft object cannot be analysed. The screen must say why.

    Not saying it is worse than it sounds: the checkbox produces no
    visible change at all, so the feature reads as broken exactly when
    the user is on the content most likely to be a draft.
    """
    project, f = build("g3_draft_deep")          # never approved
    rep = ic.assess_impact_center(str(project), f["block_id"],
                                  include_translation_detail=True)
    check("the report records that the analysis was requested",
          rep.translation_detail_requested is True, "")
    check("...and that it could not run, with the reason",
          rep.translation_deep_state == ti.TIER2_UNAVAILABLE_NOT_APPROVED,
          rep.translation_deep_state)
    it_text = rendered(rep, "it")
    check("the screen explains it in the customer's words",
          "dopo l'approvazione del contenuto" in it_text, it_text[-400:])
    check("...and shows no technical error",
          "TIER2" not in it_text and "Traceback" not in it_text, "")


def test_unrequested_analysis_stays_silent():
    project, f = build("g3_draft_quiet")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    check("not asking for the analysis says nothing about it",
          rep.translation_detail_requested is False, "")
    check("...and the screen does not mention it",
          "dopo l'approvazione del contenuto" not in rendered(rep, "it"), "")


# ==================================================================
# 4. Compliance impact
# ==================================================================

def test_compliance_requirement_reopens_and_is_grouped_as_attention():
    project, f = build("g4_compliance", with_evidence=True)
    before = ic.assess_impact_center(str(project), f["block_id"])
    check("the linked requirement is reported", len(before.requirements) == 1,
          [r.label for r in before.requirements])
    check("...by title, not by id",
          before.requirements[0].label == "Avvertenze sull'uso scorretto",
          before.requirements[0].label)
    check("...and is not reopened before the change",
          before.requirements_reopened == (), before.requirements_reopened)
    ready_before = rendered(before, "it")
    check("it sits in the ready section before the change",
          "Avvertenze" in ready_before[ready_before.index("Cosa è pronto"):], "")

    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    after = ic.assess_impact_center(str(project), f["block_id"])
    check("after the change the requirement is reopened",
          len(after.requirements_reopened) == 1, after.requirements_reopened)
    text = rendered(after, "it")
    attention = text[text.index("Cosa richiede attenzione"):text.index("Cosa è pronto")]
    check("...and moves into the attention section", "Avvertenze" in attention, attention[:200])
    check("...with a plain-language reason",
          "versione precedente" in attention, attention[:260])


# ==================================================================
# 5. Release impact
# ==================================================================

def test_release_blocked_by_stale_translation():
    project, f = build("g5_release_stale", approve=True)
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    check("the release verdict is NO", rep.release_ready is False, rep.release_ready)
    codes = [n.code for n in rep.release_notes]
    check("...because of the stale translation",
          ic.RELEASE_BLOCKED_STALE_TRANSLATIONS in codes, codes)
    check("...and because the edit reset the content to Draft",
          ic.RELEASE_BLOCKED_CONTENT_NOT_REVIEWED in codes, codes)
    text = rendered(rep, "it")
    check("the verdict is printed in plain language",
          "Pronto al rilascio: NO" in text, "")


def test_release_ready_when_nothing_is_outstanding():
    project, f = build("g5_release_ok", langs=("de-DE",), approve=True)
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    rep = ic.assess_impact_center(str(project), f["block_id"])
    check("the release verdict is YES", rep.release_ready is True,
          [n.code for n in rep.release_notes])
    check("...with the positive note", [n.code for n in rep.release_notes]
          == [ic.RELEASE_READY_FOR_THIS_CHANGE], [n.code for n in rep.release_notes])
    text = rendered(rep, "it")
    check("the verdict is printed", "Pronto al rilascio: SI" in text, "")
    check("the scope of the verdict is stated, not implied",
          "Rilascio" in text and "modifica" in text.lower(), "")


def test_the_verdict_has_its_own_heading():
    """"Cosa e pronto" must never be the heading above "Pronto: NO"."""
    project, f = build("g5_band", approve=True)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    with i18n.use_language("it"):
        ready = icv.ready_html(rep)
    check("the verdict sits in a card of its own",
          ready.count("ms-ic-section") == 2, ready.count("ms-ic-section"))
    check("...headed 'Rilascio'", "Rilascio</h3>" in ready, ready[-300:])
    check("...and 'Cosa e pronto' does not contain the verdict",
          ready.index("Cosa è pronto") < ready.index("Rilascio</h3>") < ready.index(
              "Pronto al rilascio"), "")


def test_release_notes_agree_and_use_language_names():
    project, f = build("g5_agreement", langs=("de-DE", "fr-FR"), approve=True)
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    with i18n.use_language("it"):
        ready = icv.ready_html(rep)
    check("one stale translation reads '1 traduzione', not '1 traduzioni'",
          "1 traduzione da aggiornare" in ready, ready[-360:])
    check("one untranslated language reads '1 lingua'",
          "1 lingua non ancora tradotta" in ready, ready[-360:])
    check("languages are named, not coded",
          "tedesca" in ready and "francese" in ready, ready[-360:])
    check("...and the raw codes are gone", "de-DE" not in ready and "fr-FR" not in ready,
          ready[-360:])
    with i18n.use_language("en"):
        ready_en = icv.ready_html(rep)
    check("[en] the singular form is used too",
          "1 translation to update" in ready_en, ready_en[-300:])
    check("[en] languages are named", "German" in ready_en or "german" in ready_en.lower(),
          ready_en[-300:])


def test_release_notes_plural_above_one():
    project, f = build("g5_plural", langs=("de-DE", "fr-FR", "es-ES"), approve=True)
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    add_translation(project, f["block_id"], "fr-FR", v)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    with i18n.use_language("it"):
        ready = icv.ready_html(rep)
    check("two stale translations read '2 traduzioni'",
          "2 traduzioni da aggiornare" in ready, ready[-360:])


def test_release_blocked_by_missing_translation():
    project, f = build("g5_release_missing", approve=True)
    rep = ic.assess_impact_center(str(project), f["block_id"])
    codes = [n.code for n in rep.release_notes]
    check("an untranslated language blocks the release",
          ic.RELEASE_BLOCKED_MISSING_TRANSLATIONS in codes, codes)
    check("stale and missing are reported separately",
          ic.RELEASE_BLOCKED_STALE_TRANSLATIONS not in codes, codes)


# ==================================================================
# 6. Customer language — the vocabulary rule
# ==================================================================

def test_no_engine_vocabulary_in_customer_panels():
    project, f = build("g6_vocab", approve=True, with_tm=True, with_evidence=True)
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"],
                                  include_translation_detail=True)
    for lang in ("it", "en"):
        text = rendered(rep, lang)
        flat = re.sub(r"<[^>]+>", " ", text).lower()
        for banned in BANNED:
            check(f"[{lang}] the customer panels never say {banned!r}",
                  banned not in flat, flat[:160])
        check(f"[{lang}] no catalog key is rendered",
              "impactcenter." not in text and "translation.reason" not in text, text[:160])
        check(f"[{lang}] no unsubstituted placeholder",
              not re.search(r"\{[a-z_]+\}", text), text[:160])
        check(f"[{lang}] no raw block id is shown",
              f["block_id"] not in text, "")


def test_advanced_section_is_where_technical_detail_lives():
    project, f = build("g6_advanced")
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    with i18n.use_language("it"):
        adv = icv.advanced_html(rep)
    check("the advanced section carries the object id", f["block_id"] in adv, "")
    check("...and the engine's own codes", "IMPACT_" in adv, adv[:160])
    check("...and the schema terms the panels must not use",
          "source_version" in adv, adv[:200])
    check("...and states the known limitations", "revision" in adv.lower(), "")


def test_it_and_en_differ():
    project, f = build("g6_langs")
    rep = ic.assess_impact_center(str(project), f["block_id"])
    it_text, en_text = rendered(rep, "it"), rendered(rep, "en")
    check("the two languages genuinely differ", it_text != en_text, "")
    check("IT uses Italian section headings", "Cosa cambia" in it_text, it_text[:120])
    check("EN uses English section headings", "What changes" in en_text, en_text[:120])
    check("no Italian heading survives in EN", "Cosa cambia" not in en_text, "")


def test_empty_state():
    with i18n.use_language("it"):
        header, changes, attention, ready, adv = icv.render(None)
    check("no selection gives a neutral prompt", "ms-ic-empty" in header, header)
    check("...and no empty sections", changes == "" and attention == "" and ready == "", "")


# ==================================================================
# 7. Read-only
# ==================================================================

def _fingerprint(project: Path) -> tuple:
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        counts = tuple((t, repo.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"])
                       for t in ("content_blocks", "block_versions", "block_translations",
                                 "topics", "audit_events", "review_tasks",
                                 "compliance_assessment", "compliance_evidence",
                                 "authoring_drafts", "structured_tables"))
        rows = tuple(tuple(r) for r in repo.conn.execute(
            "SELECT block_id,version,text_content,status FROM content_blocks ORDER BY block_id"))
        return counts, rows
    finally:
        repo.close()


def _run_every_read(project, f):
    for deep in (False, True):
        ic.assess_impact_center(str(project), f["block_id"], include_translation_detail=deep)
        ic.assess_impact_center(str(project), f["table_id"], object_type="table",
                                include_translation_detail=deep)
    ic.changed_objects(str(project))


def test_the_view_writes_nothing():
    project, f = build("g7_readonly", approve=True, with_tm=True, with_evidence=True)
    add_translation(project, f["block_id"], "de-DE", version_of(project, f["block_id"]))
    before = _fingerprint(project)
    files_before = {p.name for p in project.iterdir()}
    _run_every_read(project, f)
    after = _fingerprint(project)
    check("no row, version, status or audit event changed", before == after,
          [(a, b) for a, b in zip(before[0], after[0]) if a != b])
    check("no new file or directory appeared in the project",
          {p.name for p in project.iterdir()} == files_before,
          {p.name for p in project.iterdir()} ^ files_before)
    check("no translation package was produced",
          not (project / "TranslationPackages").exists(), "")


def test_the_view_does_not_construct_the_workflow_manager():
    """Pins the decision NOT to call assess_release_readiness from this view.

    That service builds a WorkflowManager, whose constructor creates
    ManualSync_Workflow.sqlite and the assets directory when they are
    absent -- measured, not assumed. create_project makes both, so the
    only way to observe the difference is to remove them first.
    """
    project, f = build("g7_no_workflow", approve=True, with_evidence=True)
    wf_db = project / "ManualSync_Workflow.sqlite"
    wf_assets = project / "ManualSync_Workflow_Assets"
    import shutil as _sh
    wf_db.unlink()
    _sh.rmtree(wf_assets)
    _run_every_read(project, f)
    check("the view does not create the workflow database", not wf_db.exists(),
          "assess_release_readiness would have created it")
    check("...nor the workflow assets directory", not wf_assets.exists(), "")

    from workflow_management import WorkflowManager
    WorkflowManager(project)
    check("(control) constructing it really does create them",
          wf_db.exists() and wf_assets.exists(),
          "if this fails the test above proves nothing")


# ==================================================================
# 8. The picker
# ==================================================================

def test_changed_objects_lists_recent_work_newest_first():
    project, f = build("g8_picker")
    save_block(project, f["block_id"], SENTENCE + " Primo.")
    choices = ic.changed_objects(str(project))
    ids = [oid for _label, oid in choices]
    check("the edited block is offered", f["block_id"] in ids, ids)
    check("the most recently touched object comes first", ids[0] == f["block_id"], ids)
    check("entries are labelled, never bare ids",
          all(label and label != oid for label, oid in choices), choices[:2])
    check("no duplicate object appears", len(ids) == len(set(ids)), ids)


# ==================================================================
# 9. Mutation — does the release verdict have teeth?
# ==================================================================

_seq = 0


def _load_mutant(old: str, new: str):
    """Load a mutated COPY; the shipped file is never written to."""
    global _seq
    import importlib.util
    source = (BASE / "impact_center.py").read_text(encoding="utf-8")
    assert source.count(old) == 1, f"anchor not unique: {old!r}"
    _seq += 1
    name = f"_ic_mutant_{_seq}"
    path = TMP / f"{name}.py"
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.pop(name, None)


def test_mutation_release_verdict_has_teeth():
    project, f = build("g9_mut", approve=True)
    v = version_of(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    mutant = _load_mutant("    stale = [l for l in languages if l.status == ti.STATUS_STALE]",
                          "    stale = []")
    rep = mutant.assess_impact_center(str(project), f["block_id"])
    codes = [n.code for n in rep.release_notes]
    check("ignoring stale translations is DETECTED in the release verdict",
          mutant.RELEASE_BLOCKED_STALE_TRANSLATIONS not in codes, codes)
    real = ic.assess_impact_center(str(project), f["block_id"])
    check("the real module still blocks on it",
          ic.RELEASE_BLOCKED_STALE_TRANSLATIONS in [n.code for n in real.release_notes], "")


def test_mutation_reopened_requirement_has_teeth():
    project, f = build("g9_mut_req", with_evidence=True)
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    mutant = _load_mutant(
        "            if r.code == ii.IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE:\n"
        "                reopened.append(ref)",
        "            if False:\n                reopened.append(ref)")
    rep = mutant.assess_impact_center(str(project), f["block_id"])
    check("failing to notice a reopened requirement is DETECTED",
          rep.requirements_reopened == (), rep.requirements_reopened)
    real = ic.assess_impact_center(str(project), f["block_id"])
    check("the real module still reports it", len(real.requirements_reopened) == 1, "")


def run():
    fns = sorted((v for k, v in globals().items() if k.startswith("test_")),
                 key=lambda f: f.__code__.co_firstlineno)
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            check(f"{fn.__name__} raised", False, f"{type(exc).__name__}: {exc}")
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("=" * 78)
    print("REGRESSIONE — ERYLOS IMPACT CENTER V1")
    print("=" * 78)
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}")
            if detail:
                print(f"        {detail}")
    print(f"\n  {passed}/{total} passed")
    print("=" * 78)
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
