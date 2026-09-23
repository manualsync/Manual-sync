"""UX Gate 2B.1 — Impact Intelligence regression suite.

Covers impact_intelligence.py (the composer) and impact_view.py (the
prose layer), against real projects built through the supported service
APIs. Fixtures are synthetic and disposable; the canonical demo is never
touched.

What this suite is for
----------------------
The service's whole value is that it reports what OTHER engines conclude.
So the tests that matter are the ones that would catch it reporting
something the engines did not say: a stale translation shown as current,
a count that disagrees with its own detail records, or a zero standing in
for "cannot determine".

The two version comparisons that carry the product claim -- translation
staleness and compliance evidence currency -- are mutation-tested: the
comparison is inverted in a copy of the module and the suite must fail.
A test that cannot fail is decoration.
"""
from __future__ import annotations
import test_isolation as _isolation  # noqa: E402,F401 -- aliased: this suite collects every global named test_* as a test. FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import authoring_view as av  # noqa: E402
import compliance_view as cv  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402
import impact_intelligence as ii  # noqa: E402
import impact_view as iv  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="manualsync_impact_2b1_"))
ACTOR = "Impact Suite"
_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))


# ------------------------------------------------------------------
# Fixture: one publication, two topics, a shared table, a translation,
# a compliance evidence link. Built through the real services.
# ------------------------------------------------------------------

def build(name: str) -> tuple[Path, dict]:
    project = create_project(TMP, project_name=name, source_lang="it-IT")
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    facts: dict = {}
    try:
        repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
        repo.create_topic("T-B", "Manutenzione", "", source_lang="it-IT", status="Draft")
        repo.create_publication("PUB-MAIN", "Manuale di prova", ["T-A", "T-B"], source_lang="it-IT")
    finally:
        repo.close()

    repo, auth = av.open_service(str(project))
    try:
        blk = auth.create_block_in_topic("T-A", "paragraph", actor=ACTOR,
                                         text="La macchina lavora profili in alluminio.")
        facts["block_id"] = blk["block_id"]
        # a table placed in BOTH topics -> genuinely shared
        tbl = auth.create_table_in_topic("T-A", actor=ACTOR, caption="Coppie di serraggio")
        facts["table_id"] = tbl["table_id"]
        facts["table_block_a"] = tbl["block_id"]
        second = repo.add_block("BLK-SHARED-B", "T-B", "table", position=1,
                                object_ref_id=tbl["table_id"], status="Draft")
        facts["table_block_b"] = second if isinstance(second, str) else "BLK-SHARED-B"
    finally:
        repo.close()
    return project, facts


def add_translation(project: Path, block_id: str, lang: str, source_version: int, status="Reviewed"):
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.conn.execute(
            """INSERT INTO block_translations(block_id,target_lang,source_version,target_text,status,updated_at)
               VALUES(?,?,?,?,?,datetime('now'))""",
            (block_id, lang, int(source_version), "Uebersetzung.", status))
        repo.conn.commit()
    finally:
        repo.close()


def add_evidence(project: Path, block_id: str, version: int) -> tuple[str, str]:
    """Link the block as evidence for a real requirement. Returns (req, scope)."""
    crepo, svc = cv.open_service(str(project))
    try:
        svc.create_instrument("INST-TEST", "TEST/1", actor=ACTOR, kind="directive",
                              title="Direttiva di prova")
        svc.create_requirement("REQ-1", "Requisito di prova", actor=ACTOR,
                               instrument_id="INST-TEST", article_ref="Art.1",
                               description="Descrizione")
        scope = svc.create_scope("SCOPE-1", publication_id="PUB-MAIN", title="Ambito di prova",
                                 jurisdiction="EU", actor=ACTOR)
        svc.add_scope_reference("SCOPE-1", ref_kind="instrument", ref_id="INST-TEST", actor=ACTOR)
        svc.set_applicability("SCOPE-1", "REQ-1", "APPLIES", actor=ACTOR, justification="test")
        svc.add_evidence(f"EV-{block_id}-v{version}", "block", actor=ACTOR,
                         object_id=block_id, object_version=int(version),
                         description="Evidenza di prova")
        svc.link_evidence_requirement(f"EV-{block_id}-v{version}", "REQ-1", actor=ACTOR)
        svc.set_assessment("SCOPE-1", "REQ-1", "COVERED", actor=ACTOR, comment="ok")
    finally:
        crepo.close()
    return "REQ-1", "SCOPE-1"


def save_block(project: Path, block_id: str, text: str):
    repo, auth = av.open_service(str(project))
    try:
        d = av.block_detail(auth, block_id)
        return auth.save_block(block_id, text=text, expected_version=int(d["version"]), actor=ACTOR)
    finally:
        repo.close()


def codes(report, domain=None) -> list[str]:
    return [r.code for r in report.records if domain is None or r.domain == domain]


# ==================================================================
# 1. shape, ordering, determinism
# ==================================================================

def test_deterministic_ordering_and_dedup():
    project, f = build("g1_order")
    rep_a = ii.assess_object_impact(str(project), "block", f["block_id"])
    rep_b = ii.assess_object_impact(str(project), "block", f["block_id"])
    check("two assessments of unchanged data are byte-identical",
          [r.to_dict() for r in rep_a.records] == [r.to_dict() for r in rep_b.records], "")
    keys = [r.sort_key for r in rep_a.records]
    check("records are returned in sorted order", keys == sorted(keys), "")
    check("records carry no duplicates", len(set(rep_a.records)) == len(rep_a.records), "")


def test_duplicate_suppression_is_real():
    """A table placed twice in the SAME topic must not double the topic."""
    project, f = build("g1_dedup")
    rep = ii.assess_object_impact(str(project), "table", f["table_id"])
    topic_records = [r for r in rep.records if r.code == ii.IMPACT_CONTENT_IN_TOPIC]
    check("each containing topic appears exactly once",
          len({r.ref_id for r in topic_records}) == len(topic_records),
          [r.ref_id for r in topic_records])
    pub_records = [r for r in rep.records if r.code == ii.IMPACT_CONTENT_IN_PUBLICATION]
    check("the publication appears once even though the table is placed twice",
          len(pub_records) == 1, [r.ref_id for r in pub_records])


def test_counts_derive_from_records():
    """The summary must be recomputable from the detail alone."""
    project, f = build("g1_counts")
    add_translation(project, f["block_id"], "de-DE", 1)
    rep = ii.assess_object_impact(str(project), "block", f["block_id"])
    check("summarize(records) reproduces the report's own counts",
          ii.summarize(rep.records) == rep.counts, (ii.summarize(rep.records), rep.counts))


# ==================================================================
# 2. zero vs unknown vs not-applicable
# ==================================================================

def test_zero_unknown_not_applicable_are_distinct():
    project, f = build("g2_states")
    blk = ii.assess_object_impact(str(project), "block", f["block_id"])
    tbl = ii.assess_object_impact(str(project), "table", f["table_id"])

    check("a text block reports reuse as NOT APPLICABLE, never 0",
          blk.state_of(ii.CAT_REUSE_LOCATIONS) == ii.STATE_NOT_APPLICABLE,
          blk.state_of(ii.CAT_REUSE_LOCATIONS))
    check("...and count() returns None for it, not 0",
          blk.count(ii.CAT_REUSE_LOCATIONS) is None, blk.count(ii.CAT_REUSE_LOCATIONS))

    check("a table reports profiles as UNDETERMINED (evaluate_element_with_profile "
          "accepts topic/block only)",
          tbl.state_of(ii.CAT_PROFILES_AFFECTED) == ii.STATE_UNDETERMINED,
          tbl.state_of(ii.CAT_PROFILES_AFFECTED))
    check("...and count() returns None for it, not 0",
          tbl.count(ii.CAT_PROFILES_AFFECTED) is None, tbl.count(ii.CAT_PROFILES_AFFECTED))

    check("a genuine zero stays a zero: no profiles configured means 0 affected, KNOWN",
          blk.state_of(ii.CAT_PROFILES_AFFECTED) == ii.STATE_KNOWN
          and blk.count(ii.CAT_PROFILES_AFFECTED) == 0,
          (blk.state_of(ii.CAT_PROFILES_AFFECTED), blk.count(ii.CAT_PROFILES_AFFECTED)))


def test_view_never_renders_unknown_as_zero():
    project, f = build("g2_view_states")
    tbl = ii.assess_object_impact(str(project), "table", f["table_id"])
    with i18n.use_language("it"):
        lines = dict((c, t) for c, t, _ in iv.summary_lines(tbl))
    text = lines.get(ii.CAT_PROFILES_AFFECTED, "")
    check("the undetermined profile category is shown as 'non determinabile'",
          i18n.translate("impact.unknown", "it") in text, text)
    check("...and never as '0'", not text.strip().startswith("0"), text)


# ==================================================================
# 3. translations — the headline claim
# ==================================================================

def test_translation_current_then_stale():
    project, f = build("g3_translation")
    blk = f["block_id"]
    add_translation(project, blk, "de-DE", 1)

    before = ii.assess_object_impact(str(project), "block", blk)
    check("a translation made from the live version reports CURRENT",
          ii.IMPACT_TRANSLATION_CURRENT in codes(before, ii.DOMAIN_TRANSLATION),
          codes(before, ii.DOMAIN_TRANSLATION))
    check("...and contributes 0 stale translations",
          before.count(ii.CAT_STALE_TRANSLATIONS) == 0, before.counts)
    check("...while still counting as a language present",
          before.count(ii.CAT_LANGUAGES_PRESENT) == 1, before.counts)

    save_block(project, blk, "Testo modificato.")
    after = ii.assess_object_impact(str(project), "block", blk)
    stale = [r for r in after.records if r.code == ii.IMPACT_TRANSLATION_SOURCE_VERSION_STALE]
    check("after a save the same translation reports STALE", len(stale) == 1,
          codes(after, ii.DOMAIN_TRANSLATION))
    check("...and the count follows", after.count(ii.CAT_STALE_TRANSLATIONS) == 1, after.counts)
    check("...and the record explains WHY, with both versions",
          stale and stale[0].param_dict.get("source_version") == 1
          and stale[0].param_dict.get("current_version") == 2,
          stale[0].param_dict if stale else None)
    check("...and names the language", stale and stale[0].ref_id == "de-DE",
          stale[0].ref_id if stale else None)
    check("has_attention flips on", after.has_attention(), "")


def test_translation_reason_is_localized_both_ways():
    project, f = build("g3_reason_lang")
    blk = f["block_id"]
    add_translation(project, blk, "de-DE", 1)
    save_block(project, blk, "Cambiato.")
    rep = ii.assess_object_impact(str(project), "block", blk)
    rec = next(r for r in rep.records if r.code == ii.IMPACT_TRANSLATION_SOURCE_VERSION_STALE)
    with i18n.use_language("it"):
        it_text = iv.reason_text(rec)
    with i18n.use_language("en"):
        en_text = iv.reason_text(rec)
    check("the reason renders in Italian", "versione" in it_text and "de-DE" in it_text, it_text)
    check("the reason renders in English", "version" in en_text and "de-DE" in en_text, en_text)
    check("IT and EN genuinely differ", it_text != en_text, (it_text, en_text))
    check("no catalog key leaks into either", "impact.reason" not in it_text + en_text, "")
    check("both carry the concrete versions", "1" in it_text and "2" in it_text, it_text)


# ==================================================================
# 4. compliance
# ==================================================================

def test_compliance_current_then_stale():
    project, f = build("g4_compliance")
    blk = f["block_id"]
    req, scope = add_evidence(project, blk, 1)

    before = ii.assess_object_impact(str(project), "block", blk)
    check("evidence citing the live version reports CURRENT",
          ii.IMPACT_COMPLIANCE_EVIDENCE_CURRENT in codes(before, ii.DOMAIN_COMPLIANCE),
          codes(before, ii.DOMAIN_COMPLIANCE))
    check("...so 0 requirements need attention",
          before.count(ii.CAT_REQUIREMENTS_ATTENTION) == 0, before.counts)
    status_before = [r.param_dict.get("status") for r in before.records
                     if r.code == ii.IMPACT_COMPLIANCE_REQUIREMENT_STATUS]
    check("the requirement's live status is reported as Covered",
          "Covered" in status_before, status_before)

    save_block(project, blk, "Testo cambiato dopo l'evidenza.")
    after = ii.assess_object_impact(str(project), "block", blk)
    stale = [r for r in after.records if r.code == ii.IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE]
    check("after a save the evidence reports STALE", len(stale) == 1,
          codes(after, ii.DOMAIN_COMPLIANCE))
    check("...naming the requirement", stale and stale[0].ref_id == req, "")
    check("...and both versions", stale and stale[0].param_dict.get("evidence_version") == 1
          and stale[0].param_dict.get("current_version") == 2,
          stale[0].param_dict if stale else None)
    check("...and the count follows", after.count(ii.CAT_REQUIREMENTS_ATTENTION) == 1, after.counts)
    status_after = [r.param_dict.get("status") for r in after.records
                    if r.code == ii.IMPACT_COMPLIANCE_REQUIREMENT_STATUS]
    check("the requirement's live status degrades to 'To verify' -- reported, not computed here",
          "To verify" in status_after, status_after)


# ==================================================================
# 5. reuse / publication traversal
# ==================================================================

def test_reuse_traversal_returns_every_location():
    project, f = build("g5_reuse")
    rep = ii.assess_object_impact(str(project), "table", f["table_id"])
    placements = {r.ref_id for r in rep.records if r.code == ii.IMPACT_CONTENT_REUSED_IN_PLACEMENT}
    check("both placements of the shared table are returned", len(placements) == 2, placements)
    check("...and the count matches", rep.count(ii.CAT_REUSE_LOCATIONS) == 2, rep.counts)
    check("the object is reported as SHARED",
          ii.IMPACT_OBJECT_SHARED in codes(rep, ii.DOMAIN_REUSE), codes(rep, ii.DOMAIN_REUSE))
    topics = {r.ref_id for r in rep.records if r.code == ii.IMPACT_CONTENT_IN_TOPIC}
    check("both containing topics are reached", topics == {"T-A", "T-B"}, topics)
    check("the publication is reached once", rep.count(ii.CAT_PUBLICATIONS) == 1, rep.counts)


def test_local_object_reports_local():
    project, f = build("g5_local")
    repo, auth = av.open_service(str(project))
    try:
        solo = auth.create_table_in_topic("T-B", actor=ACTOR, caption="Solo qui")
    finally:
        repo.close()
    rep = ii.assess_object_impact(str(project), "table", solo["table_id"])
    check("a table used once reports LOCAL, not SHARED",
          ii.IMPACT_OBJECT_LOCAL in codes(rep, ii.DOMAIN_REUSE), codes(rep, ii.DOMAIN_REUSE))
    check("...with exactly one reuse location", rep.count(ii.CAT_REUSE_LOCATIONS) == 1, rep.counts)


def test_block_reaches_its_publication():
    project, f = build("g5_block_pub")
    rep = ii.assess_object_impact(str(project), "block", f["block_id"])
    check("a block reaches its publication", rep.count(ii.CAT_PUBLICATIONS) == 1, rep.counts)
    check("...and its topic",
          any(r.ref_id == "T-A" for r in rep.records if r.code == ii.IMPACT_CONTENT_IN_TOPIC), "")


# ==================================================================
# 6. variants / profiles
# ==================================================================

def test_profile_inclusion_and_exclusion():
    project, f = build("g6_profiles")
    repo, auth = av.open_service(str(project))
    try:
        auth.create_condition_dimension("mercato", "Mercato", actor=ACTOR)
        auth.create_condition_value("eu", "mercato", "Europa", actor=ACTOR)
        auth.create_condition_value("us", "mercato", "USA", actor=ACTOR)
        auth.create_variant_profile("P-EU", "Mercato Europa", {"mercato": "eu"}, actor=ACTOR)
        auth.create_variant_profile("P-US", "Mercato USA", {"mercato": "us"}, actor=ACTOR)
        d = av.block_detail(auth, f["block_id"])
        auth.save_block(f["block_id"], condition={"mercato": ["eu"]},
                        expected_version=int(d["version"]), actor=ACTOR)
    finally:
        repo.close()
    rep = ii.assess_object_impact(str(project), "block", f["block_id"])
    inc = {r.ref_id for r in rep.records if r.code == ii.IMPACT_PROFILE_INCLUDES}
    exc = {r.ref_id for r in rep.records if r.code == ii.IMPACT_PROFILE_EXCLUDES}
    check("the EU profile includes the EU-conditioned block", inc == {"P-EU"}, inc)
    check("the US profile excludes it", exc == {"P-US"}, exc)
    check("only INCLUDED profiles are counted as affected",
          rep.count(ii.CAT_PROFILES_AFFECTED) == 1, rep.counts)
    check("the profile state is KNOWN for a block",
          rep.state_of(ii.CAT_PROFILES_AFFECTED) == ii.STATE_KNOWN, "")


# ==================================================================
# 7. review — including the limitation
# ==================================================================

def test_review_state_and_limitation():
    project, f = build("g7_review")
    blk = f["block_id"]
    rep = ii.assess_object_impact(str(project), "block", blk)
    check("with no review open the count is a KNOWN zero",
          rep.state_of(ii.CAT_OPEN_REVIEWS) == ii.STATE_KNOWN
          and rep.count(ii.CAT_OPEN_REVIEWS) == 0, rep.counts)
    check("the version-awareness limitation is stated even when no review exists",
          ii.IMPACT_REVIEW_NOT_VERSION_AWARE in codes(rep, ii.DOMAIN_REVIEW),
          codes(rep, ii.DOMAIN_REVIEW))
    check("...and is listed among the report's declared limitations",
          ii.LIMITATION_REVIEW_NOT_VERSION_AWARE in rep.limitations, rep.limitations)

    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.request_review("block", blk, requested_by=ACTOR, assigned_to="Revisore")
    finally:
        repo.close()
    rep2 = ii.assess_object_impact(str(project), "block", blk)
    check("an open review is reported", rep2.count(ii.CAT_OPEN_REVIEWS) == 1, rep2.counts)
    open_rec = [r for r in rep2.records if r.code == ii.IMPACT_REVIEW_OPEN]
    check("...naming the assignee",
          open_rec and open_rec[0].param_dict.get("assigned_to") == "Revisore",
          open_rec[0].param_dict if open_rec else None)


# ==================================================================
# 8. error paths and edge objects
# ==================================================================

def test_missing_object_raises_domain_error():
    project, f = build("g8_missing")
    try:
        ii.assess_object_impact(str(project), "block", "BLK-DOES-NOT-EXIST")
        check("a missing object raises", False, "no exception")
    except de.DomainNotFoundError as exc:
        check("a missing object raises DomainNotFoundError with a code",
              exc.code == de.ERR_CONTENT_OBJECT_NOT_FOUND, exc.code)
        with i18n.use_language("en"):
            msg = i18n.translate(f"error.{exc.code}", None, **exc.params)
        check("...that resolves to English prose, not a bare code",
              "not found" in msg.lower() and "ERR_" not in msg, msg)


def test_unsupported_kind_raises_domain_error():
    project, f = build("g8_kind")
    try:
        ii.assess_object_impact(str(project), "component", f["block_id"])
        check("an unsupported kind raises", False, "no exception")
    except de.DomainValidationError as exc:
        check("an unsupported kind raises DomainValidationError with a code",
              exc.code == de.ERR_IMPACT_KIND_UNSUPPORTED, exc.code)
        check("...and names the supported kinds",
              "topic" in str(exc.params.get("allowed", "")), exc.params)


def test_archived_object_still_assessable():
    project, f = build("g8_archived")
    blk = f["block_id"]
    repo, auth = av.open_service(str(project))
    try:
        auth.archive_block(blk, actor=ACTOR)
    finally:
        repo.close()
    rep = ii.assess_object_impact(str(project), "block", blk)
    check("an archived object still returns a report", rep.object_id == blk, "")
    check("...and says so explicitly",
          ii.IMPACT_OBJECT_ARCHIVED in codes(rep, ii.DOMAIN_STRUCTURE),
          codes(rep, ii.DOMAIN_STRUCTURE))


# ==================================================================
# 9. no mutation
# ==================================================================

def _fingerprint(project: Path) -> tuple:
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        out = []
        for t in ("content_blocks", "topics", "block_versions", "block_translations",
                  "review_tasks", "audit_events", "compliance_evidence",
                  "compliance_assessment", "structured_tables", "authoring_drafts"):
            rows = repo.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            out.append((t, rows))
        vers = repo.conn.execute(
            "SELECT block_id, version, text_content, status FROM content_blocks ORDER BY block_id"
        ).fetchall()
        return (tuple(out), tuple(tuple(r) for r in vers))
    finally:
        repo.close()


def test_assessment_mutates_nothing():
    project, f = build("g9_readonly")
    add_translation(project, f["block_id"], "de-DE", 1)
    add_evidence(project, f["block_id"], 1)
    before = _fingerprint(project)
    for kind, oid in (("block", f["block_id"]), ("table", f["table_id"]),
                      ("topic", "T-A"), ("topic", "T-B")):
        ii.assess_object_impact(str(project), kind, oid)
    after = _fingerprint(project)
    check("assessing impact changes no row, no version, no audit event",
          before == after, [(a, b) for a, b in zip(before[0], after[0]) if a != b])


# ==================================================================
# 10. view layer
# ==================================================================

def test_view_renders_no_keys_and_no_mixed_language():
    project, f = build("g10_view")
    blk = f["block_id"]
    add_translation(project, blk, "de-DE", 1)
    add_evidence(project, blk, 1)
    save_block(project, blk, "Cambiato.")
    rep = ii.assess_object_impact(str(project), "block", blk)
    for lang, marker_absent in (("it", "Translations"), ("en", "Traduzioni")):
        with i18n.use_language(lang):
            compact, details = iv.render(rep)
        blob = compact + details
        check(f"[{lang}] no catalog key is rendered",
              "impact." not in blob and "IMPACT_" not in blob, blob[:160])
        check(f"[{lang}] the other language's heading is absent", marker_absent not in blob,
              blob[:160])
        check(f"[{lang}] the panel is non-empty", len(blob) > 60, len(blob))


def test_no_reason_renders_an_unsubstituted_placeholder():
    """Every param a catalog sentence names must be carried by the record.

    This is the guard for a defect the unit tests missed and a browser
    run exposed: three codes rendered a literal "{requirement_id}" /
    "{release_id}" to the user, because those values were stored only as
    the record's ref_id and never passed as params. Asserting "no catalog
    KEY leaks" did not catch it -- the key resolved fine, it was the
    interpolation that failed.

    Checked two ways, so neither can rot: statically over the whole
    catalog (catches a code no fixture happens to produce) and
    dynamically over every record a rich fixture actually emits.
    """
    import json as _json
    import re as _re

    # --- static: catalog params vs the params each _rec() call supplies
    catalog = _json.loads((BASE / "locales" / "it.json").read_text(encoding="utf-8"))
    src = (BASE / "impact_intelligence.py").read_text(encoding="utf-8")
    supplied: dict[str, set] = {}
    for m in _re.finditer(r"_rec\(\s*DOMAIN_\w+,\s*(IMPACT_\w+)(.*?)\)\)", src, _re.S):
        supplied.setdefault(m.group(1), set()).update(_re.findall(r"(\w+)\s*=", m.group(2)))
    ternary = r"_rec\(\s*DOMAIN_\w+,\s*\n?\s*(IMPACT_\w+)\s+if .*?else\s+(IMPACT_\w+)\s*,(.*?)\)\)"
    for m in _re.finditer(ternary, src, _re.S):
        for code in (m.group(1), m.group(2)):
            supplied.setdefault(code, set()).update(_re.findall(r"(\w+)\s*=", m.group(3)))
    missing = {}
    for key, text in catalog.items():
        if not key.startswith("impact.reason."):
            continue
        code = key[len("impact.reason."):]
        gap = set(_re.findall(r"\{(\w+)\}", text)) - supplied.get(code, set())
        if gap:
            missing[code] = sorted(gap)
    check("every impact.reason.* catalog param is supplied by its record", not missing, missing)

    # --- dynamic: nothing a real fixture emits renders with a brace left
    project, f = build("g10_placeholders")
    blk = f["block_id"]
    add_translation(project, blk, "de-DE", 1)
    add_evidence(project, blk, 1)
    save_block(project, blk, "Cambiato.")
    reports = [ii.assess_object_impact(str(project), "block", blk),
               ii.assess_object_impact(str(project), "table", f["table_id"]),
               ii.assess_object_impact(str(project), "topic", "T-A")]
    seen = set()
    for lang in ("it", "en"):
        with i18n.use_language(lang):
            for rep in reports:
                for rec in rep.records:
                    text = iv.reason_text(rec)
                    seen.add(rec.code)
                    check(f"[{lang}] {rec.code} renders with no leftover placeholder",
                          "{" not in text and "}" not in text, text)
    check("the dynamic sweep exercised a meaningful share of the codes",
          len(seen) >= 6, sorted(seen))


def test_view_empty_state():
    with i18n.use_language("it"):
        compact, details = iv.render(None)
    check("no open object renders the neutral prompt, not an error",
          "ms-impact-empty" in compact and details == "", (compact, details))


def test_attention_marking():
    project, f = build("g10_attention")
    blk = f["block_id"]
    add_translation(project, blk, "de-DE", 1)
    save_block(project, blk, "Cambiato.")
    rep = ii.assess_object_impact(str(project), "block", blk)
    with i18n.use_language("it"):
        lines = iv.summary_lines(rep)
        compact = iv.compact_html(rep)
    attention = [c for c, _t, a in lines if a]
    check("the stale translation line is marked as needing attention",
          ii.CAT_STALE_TRANSLATIONS in attention, attention)
    check("the publication line is NOT marked as needing attention",
          ii.CAT_PUBLICATIONS not in attention, attention)
    check("the compact html carries the attention class",
          "ms-impact-attention" in compact, "")


# ==================================================================
# 11. MUTATION TESTS — do the version comparisons have teeth?
# ==================================================================

_mutant_seq = 0


def _mutate_module_and_run(old: str, new: str, probe) -> bool:
    """Load a MUTATED COPY of the module and run `probe` against it.

    Returns True when the probe reports the mutation was DETECTED, i.e.
    the assertion the suite relies on would genuinely have failed.

    The shipped file is never written to. An earlier version of this
    helper patched impact_intelligence.py in place and reloaded it; two
    things were wrong with that. A crash between write and restore would
    have left the product source mutated on disk. And both mutations here
    swap one operator for another of the SAME LENGTH (``!=`` for ``==``),
    so the file's size never changed -- with Windows' coarse mtime
    granularity, Python happily reused the cached .pyc and re-imported
    the ORIGINAL module. The mutation tests then passed while testing
    nothing at all, which is precisely the failure mode mutation testing
    exists to prevent.

    Loading a uniquely-named copy sidesteps both: new filename, no cache
    to go stale, and the real module untouched.
    """
    global _mutant_seq
    import importlib.util
    source = (BASE / "impact_intelligence.py").read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation anchor not unique: {old!r}"
    _mutant_seq += 1
    name = f"_impact_mutant_{_mutant_seq}"
    path = TMP / f"{name}.py"
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mutant = importlib.util.module_from_spec(spec)
    sys.modules[name] = mutant
    try:
        spec.loader.exec_module(mutant)
        return probe(mutant)
    finally:
        sys.modules.pop(name, None)


def test_mutation_translation_staleness_has_teeth():
    project, f = build("g11_mut_translation")
    blk = f["block_id"]
    add_translation(project, blk, "de-DE", 1)
    save_block(project, blk, "Cambiato.")

    def probe(mod):
        rep = mod.assess_object_impact(str(project), "block", blk)
        # with the comparison inverted, the STALE translation reads CURRENT
        return mod.IMPACT_TRANSLATION_SOURCE_VERSION_STALE not in [r.code for r in rep.records]

    detected = _mutate_module_and_run(
        "        stale = src != int(current_version)",
        "        stale = src == int(current_version)",
        probe)
    check("inverting the translation staleness comparison breaks the STALE result "
          "(so the test that asserts it has teeth)", detected, "")

    # and the real module still reports it correctly after restore
    rep = ii.assess_object_impact(str(project), "block", blk)
    check("the module is restored and still reports STALE",
          ii.IMPACT_TRANSLATION_SOURCE_VERSION_STALE in [r.code for r in rep.records], "")


def test_mutation_compliance_currency_has_teeth():
    project, f = build("g11_mut_compliance")
    blk = f["block_id"]
    add_evidence(project, blk, 1)
    save_block(project, blk, "Cambiato.")

    def probe(mod):
        rep = mod.assess_object_impact(str(project), "block", blk)
        return mod.IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE not in [r.code for r in rep.records]

    detected = _mutate_module_and_run(
        "            stale = ev_version != int(current_version)",
        "            stale = ev_version == int(current_version)",
        probe)
    check("inverting the evidence currency comparison breaks the STALE result", detected, "")

    rep = ii.assess_object_impact(str(project), "block", blk)
    check("the module is restored and still reports evidence STALE",
          ii.IMPACT_COMPLIANCE_EVIDENCE_VERSION_STALE in [r.code for r in rep.records], "")


def test_mutation_summary_cannot_drift_from_records():
    """If summarize() stopped reading the records, the suite must notice."""
    project, f = build("g11_mut_summary")
    add_translation(project, f["block_id"], "de-DE", 1)
    save_block(project, f["block_id"], "Cambiato.")

    def probe(mod):
        rep = mod.assess_object_impact(str(project), "block", f["block_id"])
        return mod.summarize(rep.records) != rep.counts or rep.counts.get("stale_translations") != 1

    detected = _mutate_module_and_run(
        '        CAT_STALE_TRANSLATIONS: distinct("language", (IMPACT_TRANSLATION_SOURCE_VERSION_STALE,)),',
        '        CAT_STALE_TRANSLATIONS: 0,',
        probe)
    check("hard-coding a count instead of deriving it is detected", detected, "")


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
    print("REGRESSIONE — IMPACT INTELLIGENCE (UX Gate 2B.1)")
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
