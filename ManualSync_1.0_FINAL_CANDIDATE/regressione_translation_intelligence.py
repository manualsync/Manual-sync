"""UX Gate 2B.2 — Translation Intelligence regression suite.

Covers translation_intelligence.py (the composer) and translation_view.py
(the prose layer) against real projects built through the supported
service APIs. Fixtures are synthetic and disposable; the canonical demo is
never touched.

What matters here
-----------------
The service's value is that it reports what the CAT engine concludes
without changing it. So the tests that earn their place are the ones that
would catch it saying something the engine did not: a stale translation
shown as reliable, a language with no memory reported as a blocked reuse,
or a legacy Italian engine string leaking into an English screen.

The FRESH/STALE comparison and the legacy-string classifier are
mutation-tested against a loaded copy of the module, so a test that
cannot fail is not mistaken for a test that passes.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import authoring_view as av  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402
import translation_intelligence as ti  # noqa: E402
import translation_view as tv  # noqa: E402
from cat_tm import TranslationMemory  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project, update_project  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="manualsync_tr_intel_2b2_"))
ACTOR = "Translation Suite"
SENTENCE = "La pressione massima ammessa è 12 bar."
TM_VARIANT = "La pressione massima ammessa è 16 bar."
_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)))


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def build(name: str, *, langs=("de-DE", "fr-FR"), approve=False, with_tm=False,
          text: str = SENTENCE) -> tuple[Path, dict]:
    project = create_project(TMP, project_name=name, source_lang="it-IT")
    update_project(project, target_langs=list(langs))
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
        repo.create_publication("PUB-MAIN", "Manuale di prova", ["T-A"], source_lang="it-IT")
    finally:
        repo.close()
    repo, auth = av.open_service(str(project))
    try:
        blk = auth.create_block_in_topic("T-A", "paragraph", actor=ACTOR, text=text)
    finally:
        repo.close()
    if approve:
        repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
        try:
            rid = repo.request_review("block", blk["block_id"], requested_by=ACTOR, assigned_to=ACTOR)
            repo.decide_review(rid, decision="APPROVE", actor=ACTOR)
        finally:
            repo.close()
    if with_tm:
        tm_dir = project / "TranslationMemory"
        tm_dir.mkdir(parents=True, exist_ok=True)
        tm = TranslationMemory(tm_dir / "suite.sqlite")
        try:
            tm.add("it-IT", "de-DE", TM_VARIANT, "Der maximal zulaessige Druck betraegt 16 bar.",
                   origin="suite", alignment_confidence=100.0, approved=True)
        finally:
            tm.close()
    return project, {"block_id": blk["block_id"]}


def add_translation(project: Path, block_id: str, lang: str, source_version: int,
                    status: str = "Reviewed"):
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        repo.conn.execute(
            """INSERT INTO block_translations(block_id,target_lang,source_version,target_text,status,updated_at)
               VALUES(?,?,?,?,?,datetime('now'))""",
            (block_id, lang, int(source_version), "Uebersetzung.", status))
        repo.conn.commit()
    finally:
        repo.close()


def current_version(project: Path, block_id: str) -> int:
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
        return auth.save_block(block_id, text=text, expected_version=int(d["version"]), actor=ACTOR)
    finally:
        repo.close()


# ==================================================================
# 1. Tier 1 — FRESH / STALE / MISSING
# ==================================================================

def test_fresh_translation():
    project, f = build("g1_fresh")
    v = current_version(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    de_ = rep.language("de-DE")
    check("a translation from the live version is FRESH", de_ and de_.status == ti.STATUS_FRESH,
          de_.status if de_ else None)
    check("...with the CURRENT code", de_ and de_.code == ti.TRANSLATION_CURRENT, "")
    check("...carrying both versions",
          de_ and de_.source_version == v and de_.current_version == v, "")
    check("counts agree", rep.counts["fresh"] == 1 and rep.counts["stale"] == 0, rep.counts)
    check("a fresh translation needs no attention", not rep.has_attention(), rep.counts)
    check("...even though another language is still untranslated",
          rep.counts["missing"] == 1, rep.counts)


def test_source_edit_makes_it_stale():
    project, f = build("g1_stale")
    v = current_version(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Verificare prima dell'uso.")
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    de_ = rep.language("de-DE")
    check("after a source edit the translation is STALE", de_ and de_.status == ti.STATUS_STALE,
          de_.status if de_ else None)
    check("...with the STALE code", de_ and de_.code == ti.TRANSLATION_SOURCE_VERSION_STALE, "")
    check("...and both versions explain why",
          de_ and de_.source_version == v and de_.current_version == v + 1,
          (de_.source_version, de_.current_version) if de_ else None)
    check("counts follow", rep.counts["stale"] == 1 and rep.counts["fresh"] == 0, rep.counts)
    check("attention is flagged", rep.has_attention(), rep.counts)


def test_missing_translation():
    project, f = build("g1_missing")
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    check("a configured language with no translation is MISSING",
          all(l.status == ti.STATUS_MISSING for l in rep.languages),
          [(l.language, l.status) for l in rep.languages])
    check("...with the MISSING code",
          all(l.code == ti.TRANSLATION_MISSING_FOR_LANGUAGE for l in rep.languages), "")
    check("...and no source version to report",
          all(l.source_version is None for l in rep.languages), "")
    check("MISSING is distinct from STALE in the counts",
          rep.counts["missing"] == 2 and rep.counts["stale"] == 0, rep.counts)


def test_multiple_languages_mixed():
    project, f = build("g1_mixed", langs=("de-DE", "fr-FR", "es-ES"))
    # Translate French at v1, then move the source so French is genuinely
    # behind. An earlier version used max(1, v-1) with v==1, which is still
    # 1 -- the row was FRESH and the test was asserting nothing.
    add_translation(project, f["block_id"], "fr-FR", current_version(project, f["block_id"]))
    save_block(project, f["block_id"], SENTENCE + " Aggiornato.")
    v = current_version(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)          # fresh at the new version
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    got = {l.language: l.status for l in rep.languages}
    check("three configured languages are all reported", len(rep.languages) == 3, got)
    check("German FRESH", got.get("de-DE") == ti.STATUS_FRESH, got)
    check("French STALE", got.get("fr-FR") == ti.STATUS_STALE, got)
    check("Spanish MISSING", got.get("es-ES") == ti.STATUS_MISSING, got)
    check("counts derive from the detail", ti.summarize(rep.languages) == rep.counts, rep.counts)


def test_language_filter():
    project, f = build("g1_filter", langs=("de-DE", "fr-FR"))
    rep = ti.assess_translation_intelligence(str(project), f["block_id"], language="de-DE")
    check("language= narrows the report to one language",
          [l.language for l in rep.languages] == ["de-DE"], [l.language for l in rep.languages])


def test_untracked_language_still_reported():
    """A translation that exists is a fact, even if the manifest dropped it."""
    project, f = build("g1_untracked", langs=("de-DE",))
    v = current_version(project, f["block_id"])
    add_translation(project, f["block_id"], "pl-PL", v)
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    pl = rep.language("pl-PL")
    check("a translated language absent from the manifest is still reported", pl is not None, "")
    check("...and marked as not configured", pl is not None and pl.configured is False,
          pl.configured if pl else None)
    check("...while the configured one keeps its flag",
          rep.language("de-DE").configured is True, "")


# ==================================================================
# 2. object_type: explicit and automatic
# ==================================================================

def test_object_type_explicit_and_auto():
    project, f = build("g2_kind")
    auto = ti.assess_translation_intelligence(str(project), f["block_id"])
    explicit = ti.assess_translation_intelligence(str(project), f["block_id"], object_type="block")
    check("auto-detected object_type resolves to block", auto.object_type == "block",
          auto.object_type)
    check("explicit and auto produce identical reports", auto.to_dict() == explicit.to_dict(), "")


def test_object_type_for_topic():
    project, f = build("g2_topic")
    rep = ti.assess_translation_intelligence(str(project), "T-A")
    check("a topic is detected and assessed", rep.object_type == "topic", rep.object_type)
    check("...and reports its configured languages", len(rep.languages) == 2,
          [l.language for l in rep.languages])


def test_missing_object_and_bad_kind():
    project, f = build("g2_errors")
    try:
        ti.assess_translation_intelligence(str(project), "NOPE-404")
        check("an unknown object raises", False, "no exception")
    except de.DomainNotFoundError as exc:
        check("an unknown object raises DomainNotFoundError",
              exc.code == de.ERR_CONTENT_OBJECT_NOT_FOUND, exc.code)
        with i18n.use_language("en"):
            msg = i18n.translate(f"error.{exc.code}", None, **exc.params)
        check("...resolving to English prose", "not found" in msg.lower() and "ERR_" not in msg, msg)
    try:
        ti.assess_translation_intelligence(str(project), f["block_id"], object_type="component")
        check("an unsupported kind raises", False, "no exception")
    except de.DomainValidationError as exc:
        check("an unsupported kind raises DomainValidationError",
              exc.code == de.ERR_TRANSLATION_KIND_UNSUPPORTED, exc.code)


# ==================================================================
# 3. Tier 2 availability — explained, never an error
# ==================================================================

def test_tier2_blocked_for_draft():
    project, f = build("g3_draft")          # not approved
    rep = ti.assess_translation_intelligence(str(project), f["block_id"], deep=True)
    check("a Draft object cannot run the impact analysis",
          rep.tier2_state == ti.TIER2_UNAVAILABLE_NOT_APPROVED, rep.tier2_state)
    check("...and tier2_available says so", rep.tier2_available is False, "")
    check("...but Tier 1 still works", len(rep.languages) == 2, "")
    with i18n.use_language("it"):
        note = tv.tier2_state_html(rep)
    check("the user gets a plain explanation, not an error",
          "approvazione" in note and "Error" not in note and "Traceback" not in note, note[:90])


def test_tier2_available_for_approved():
    project, f = build("g3_approved", approve=True, with_tm=True)
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    check("an approved object can run the impact analysis",
          rep.tier2_state == ti.TIER2_AVAILABLE, rep.tier2_state)
    check("deep=False does not run it", rep.tier2 == (), rep.tier2)


def test_tier2_runs_and_explains_a_block():
    """The commercial differentiator: WHY a reuse was refused."""
    project, f = build("g3_deep", approve=True, with_tm=True)
    rep = ti.assess_translation_intelligence(str(project), f["block_id"], deep=True)
    check("deep=True returns per-language impact", len(rep.tier2) >= 1,
          [t.language for t in rep.tier2])
    de_ = next((t for t in rep.tier2 if t.language == "de-DE"), None)
    check("German is analysed", de_ is not None, "")
    check("...with a memory available", de_ is not None and de_.tm_available, "")
    check("...and one technically blocked segment",
          de_ is not None and de_.technical_block_segments == 1,
          de_.technical_block_segments if de_ else None)
    reasons = [r.code for r in (de_.reasons if de_ else ())]
    check("the block is explained as a NUMERIC mismatch",
          ti.TM_BLOCKED_NUMERIC_MISMATCH in reasons, reasons)
    rec = next((r for r in de_.reasons if r.code == ti.TM_BLOCKED_NUMERIC_MISMATCH), None)
    check("...naming the memory's value and the content's",
          rec is not None and rec.param_dict.get("memory_values") == "16"
          and rec.param_dict.get("content_values") == "12",
          rec.param_dict if rec else None)


def test_tier2_language_without_memory_is_not_a_block():
    """A language with no TM must not be reported as a refused reuse."""
    project, f = build("g3_notm", approve=True, with_tm=True)
    rep = ti.assess_translation_intelligence(str(project), f["block_id"], deep=True)
    fr = next((t for t in rep.tier2 if t.language == "fr-FR"), None)
    check("French is analysed", fr is not None, [t.language for t in rep.tier2])
    check("...with no memory available", fr is not None and not fr.tm_available, "")
    check("...and zero technical blocks", fr is not None and fr.technical_block_segments == 0,
          fr.technical_block_segments if fr else None)
    codes = [r.code for r in (fr.reasons if fr else ())]
    check("...reported as NO MEMORY, never as a blocked reuse",
          ti.TM_NO_MEMORY_FOR_LANGUAGE in codes
          and ti.TM_BLOCKED_TECHNICAL_OTHER not in codes, codes)


# ==================================================================
# 4. The legacy-string classifier
# ==================================================================

def test_classifier_maps_every_known_prefix():
    cases = [
        ("valori numerici diversi: ['12'] ≠ ['16']", ti.TM_BLOCKED_NUMERIC_MISMATCH, "12", "16"),
        ("codici/modelli diversi: ['PC-200'] ≠ ['PC-300']",
         ti.TM_BLOCKED_MODEL_CODE_MISMATCH, "PC-200", "PC-300"),
        ("unità tecniche diverse: ['bar'] ≠ ['psi']",
         ti.TM_BLOCKED_UNIT_MISMATCH, "bar", "psi"),
    ]
    for text, want, mem, con in cases:
        code, params = ti.classify_technical_reason(text)
        check(f"{want} is recognised", code == want, code)
        check(f"{want} keeps the values as params",
              params.get("memory_values") == mem and params.get("content_values") == con, params)
    code, _ = ti.classify_technical_reason("qualcosa che il motore non ha mai detto")
    check("an unknown reason falls through rather than being guessed",
          code == ti.TM_BLOCKED_TECHNICAL_OTHER, code)
    code, _ = ti.classify_technical_reason("")
    check("no reason at all means reuse was allowed", code == ti.TM_REUSE_ALLOWED, code)


def test_match_category_classifier():
    for raw, want in (("CONTEXT MATCH", ti.MATCH_CONTEXT),
                      ("CONTEXT MATCH — PENALIZZATO", ti.MATCH_CONTEXT),
                      ("100% MATCH", ti.MATCH_EXACT),
                      ("100% MATCH — PENALIZZATO", ti.MATCH_EXACT),
                      ("FUZZY MATCH", ti.MATCH_FUZZY),
                      ("TECHNICAL SAFE BLOCK", ti.MATCH_TECHNICAL_BLOCK),
                      ("NEW", ti.MATCH_NEW),
                      ("NO TM", ti.MATCH_NEW)):
        check(f"{raw!r} -> {want}", ti.classify_match_category(raw) == want,
              ti.classify_match_category(raw))


# ==================================================================
# 5. No writes
# ==================================================================

def _fingerprint(project: Path) -> tuple:
    repo = IndustrialRepository(project / "ManualSync_CCMS.sqlite")
    try:
        counts = tuple((t, repo.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"])
                       for t in ("content_blocks", "block_versions", "block_translations",
                                 "topics", "topic_versions", "audit_events", "review_tasks",
                                 "authoring_drafts"))
        rows = tuple(tuple(r) for r in repo.conn.execute(
            "SELECT block_id,version,text_content,status FROM content_blocks ORDER BY block_id"))
        return counts, rows
    finally:
        repo.close()


def test_assessment_writes_nothing():
    project, f = build("g5_readonly", approve=True, with_tm=True)
    v = current_version(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    before = _fingerprint(project)
    files_before = {p.name for p in (project / "TranslationMemory").glob("*")}
    ti.assess_translation_intelligence(str(project), f["block_id"])
    ti.assess_translation_intelligence(str(project), f["block_id"], deep=True)
    ti.assess_translation_intelligence(str(project), "T-A", deep=True)
    after = _fingerprint(project)
    check("no row, version, audit event or draft changed", before == after,
          [(a, b) for a, b in zip(before[0], after[0]) if a != b])
    check("the translation memory directory is untouched",
          {p.name for p in (project / "TranslationMemory").glob("*")} == files_before, "")
    check("no package directory was created",
          not (project / "TranslationPackages").exists(), "")


# ==================================================================
# 6. View layer — IT/EN, no keys
# ==================================================================

def test_view_renders_both_languages_without_keys():
    project, f = build("g6_view", approve=True, with_tm=True)
    v = current_version(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Cambiato.")
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    for lang, foreign in (("it", "translation"), ("en", "Traduzione")):
        with i18n.use_language(lang):
            panel, note = tv.render(rep)
        blob = panel + note
        check(f"[{lang}] no catalog key is rendered",
              "translation." not in blob and "TRANSLATION_" not in blob, blob[:140])
        check(f"[{lang}] no unsubstituted placeholder", "{" not in blob and "}" not in blob,
              blob[:140])
        check(f"[{lang}] the other language's wording is absent", foreign not in blob, blob[:140])
    with i18n.use_language("it"):
        it_panel = tv.panel_html(rep)
    with i18n.use_language("en"):
        en_panel = tv.panel_html(rep)
    check("IT and EN genuinely differ", it_panel != en_panel, "")
    check("IT names the language in Italian", "tedesca" in it_panel, it_panel[:160])
    check("EN names it in English", "German" in en_panel, en_panel[:160])


def test_view_renders_tier2_without_keys():
    project, f = build("g6_view_deep", approve=True, with_tm=True)
    rep = ti.assess_translation_intelligence(str(project), f["block_id"], deep=True)
    for lang in ("it", "en"):
        with i18n.use_language(lang):
            blob = tv.impact_html(rep)
        check(f"[{lang}] tier 2 renders", len(blob) > 80, len(blob))
        check(f"[{lang}] no catalog key", "translation.tm." not in blob and "TM_" not in blob,
              blob[:140])
        check(f"[{lang}] no unsubstituted placeholder", "{" not in blob and "}" not in blob,
              blob[:140])
    with i18n.use_language("it"):
        it_blob = tv.impact_html(rep)
    with i18n.use_language("en"):
        en_blob = tv.impact_html(rep)
    check("the blocked reuse is explained in Italian",
          "Riutilizzo bloccato" in it_blob, it_blob[:200])
    check("...and in English", "Reuse blocked" in en_blob, en_blob[:200])
    check("...naming the two values in both", "16" in it_blob and "12" in it_blob, "")


def test_view_empty_and_no_languages():
    with i18n.use_language("it"):
        panel, note = tv.render(None)
    check("no open object gives the neutral prompt", "ms-tr-empty" in panel and note == "", "")
    project, f = build("g6_nolang", langs=())
    rep = ti.assess_translation_intelligence(str(project), f["block_id"])
    with i18n.use_language("it"):
        panel = tv.panel_html(rep)
    check("a project with no target language says so", "ms-tr-empty" in panel, panel[:120])


def test_unknown_language_falls_back_to_its_code():
    with i18n.use_language("it"):
        check("an unmapped language renders as its code",
              tv.language_name("zz-ZZ") == "zz-ZZ", tv.language_name("zz-ZZ"))
        check("a regional variant resolves on its base subtag",
              tv.language_name("de-AT") == "tedesca", tv.language_name("de-AT"))


# ==================================================================
# 7. MUTATION TESTS
# ==================================================================

_mutant_seq = 0


def _load_mutant(old: str, new: str):
    """Load a mutated COPY. The shipped file is never written to."""
    global _mutant_seq
    import importlib.util
    source = (BASE / "translation_intelligence.py").read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation anchor not unique: {old!r}"
    _mutant_seq += 1
    name = f"_tr_mutant_{_mutant_seq}"
    path = TMP / f"{name}.py"
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mutant = importlib.util.module_from_spec(spec)
    sys.modules[name] = mutant
    try:
        spec.loader.exec_module(mutant)
        return mutant
    finally:
        sys.modules.pop(name, None)


def test_mutation_staleness_has_teeth():
    project, f = build("g7_mut_stale")
    v = current_version(project, f["block_id"])
    add_translation(project, f["block_id"], "de-DE", v)
    save_block(project, f["block_id"], SENTENCE + " Cambiato.")
    mutant = _load_mutant("        stale = int(row[\"source_version\"]) != int(current_version)",
                          "        stale = int(row[\"source_version\"]) == int(current_version)")
    rep = mutant.assess_translation_intelligence(str(project), f["block_id"])
    de_ = next((l for l in rep.languages if l.language == "de-DE"), None)
    check("inverting the staleness comparison is DETECTED (it stops reporting STALE)",
          de_ is not None and de_.status != mutant.STATUS_STALE, de_.status if de_ else None)
    real = ti.assess_translation_intelligence(str(project), f["block_id"])
    check("the real module still reports STALE",
          real.language("de-DE").status == ti.STATUS_STALE, "")


def test_mutation_classifier_has_teeth():
    mutant = _load_mutant('    ("valori numerici diversi", TM_BLOCKED_NUMERIC_MISMATCH),',
                          '    ("XXvalori numerici diversi", TM_BLOCKED_NUMERIC_MISMATCH),')
    code, _ = mutant.classify_technical_reason("valori numerici diversi: ['12'] ≠ ['16']")
    check("breaking the numeric prefix is DETECTED",
          code != mutant.TM_BLOCKED_NUMERIC_MISMATCH, code)
    code2, _ = ti.classify_technical_reason("valori numerici diversi: ['12'] ≠ ['16']")
    check("the real module still classifies it", code2 == ti.TM_BLOCKED_NUMERIC_MISMATCH, code2)


def test_mutation_missing_vs_stale_has_teeth():
    project, f = build("g7_mut_missing")
    mutant = _load_mutant("            out.append(LanguageStatus(language=lang, status=STATUS_MISSING,",
                          "            out.append(LanguageStatus(language=lang, status=STATUS_FRESH,")
    rep = mutant.assess_translation_intelligence(str(project), f["block_id"])
    check("reporting a missing translation as present is DETECTED",
          any(l.status != mutant.STATUS_MISSING for l in rep.languages),
          [(l.language, l.status) for l in rep.languages])
    real = ti.assess_translation_intelligence(str(project), f["block_id"])
    check("the real module still reports MISSING",
          all(l.status == ti.STATUS_MISSING for l in real.languages), "")


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
    print("REGRESSIONE — TRANSLATION INTELLIGENCE (UX Gate 2B.2)")
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
