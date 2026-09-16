"""Localization Foundation V1 — semantic keys, IT default, EN selectable.

The property this suite exists to defend is not "the words are right". It
is that **language is a presentation concern only**: the engine's
vocabulary, the persisted enums, the audit trail and every count must be
byte-identical whichever language is selected.

The concrete failure it pins was real. Applicability used to be recorded
by mapping a Radio's visible label back to an enum
(``{"Si applica": "APPLIES"}``). That works in exactly one language: in
English the Radio would show "Applies", the reverse lookup would miss, and
confirming a decision would silently do nothing. Translated text is never
a key here -- the Radio carries the enum and the label is display only.

Pure server-side: real projects built through the supported APIs, no
browser, no Gradio event loop.
"""
from __future__ import annotations

import json
import re
import contextvars
import string
import sys
import threading
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compliance_starter_packs as csp  # noqa: E402
import compliance_view as cv  # noqa: E402
import i18n  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def placeholders(text):
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


# =====================================================================
# 1. The catalogs
# =====================================================================
IT, EN = i18n.catalog_keys("it"), i18n.catalog_keys("en")

check("cat1: Italian is the declared default and the fallback",
      i18n.DEFAULT_LANGUAGE == "it", i18n.DEFAULT_LANGUAGE)
check("cat2: exactly Italian and English are supported",
      i18n.SUPPORTED_LANGUAGES == ("it", "en"), i18n.SUPPORTED_LANGUAGES)
check("cat3: every migrated key exists in BOTH catalogs -- no key is Italian-only",
      IT == EN, {"missing_in_en": sorted(IT - EN), "extra_in_en": sorted(EN - IT)})
check("cat4: the catalogs are not empty", len(IT) > 100, len(IT))
# "error.ERR_ACTOR_REQUIRED" is deliberate: an error key mirrors the
# domain's stable CODE exactly, so the two cannot drift apart. Everything
# else stays lowercase-dotted.
# A key is a lowercase dotted path, optionally ending in one stable
# SCREAMING_SNAKE identifier segment. That last form is how a domain
# CODE becomes a catalog key -- error.ERR_*, and since Gate 2B.1
# impact.reason.IMPACT_* / impact.limitation.LIMITATION_*. Allowing
# it does not weaken this check: what cat5 exists to forbid is a key
# like "Conformita" -- display text doubling as an identifier -- and
# an uppercase code is the precise opposite of that.
# The code segment may itself be qualified by one more lowercase segment.
# That is the ".one" singular-form convention already used by
# impact.count.<category>.one and reason.evidence.one, applied to a coded
# key: impactcenter.release.RELEASE_BLOCKED_STALE_TRANSLATIONS.one. Still
# not display text, so cat5 keeps catching what it was written to catch.
_KEY_SHAPE = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)*(\.[A-Z][A-Z0-9_]+)?(\.[a-z0-9_]+)?"
                        r"|error\.(unknown|ERR_[A-Z0-9_]+)")
check("cat5: no key is Italian text used as an identifier",
      all(_KEY_SHAPE.fullmatch(k) for k in IT),
      [k for k in sorted(IT) if not _KEY_SHAPE.fullmatch(k)][:6])
check("cat6: no catalog VALUE is blank in either language",
      all(i18n.translate(k, lg).strip() for k in IT for lg in ("it", "en")),
      [k for k in sorted(IT) if not i18n.translate(k, "en").strip()][:6])

mismatched = [k for k in sorted(IT)
              if placeholders(i18n.translate(k, "it")) != placeholders(i18n.translate(k, "en"))]
check("cat7: a string's placeholders are identical in both languages -- a translation "
      "cannot silently drop an interpolated value", not mismatched, mismatched)

_it_vals = {k: i18n.translate(k, "it") for k in IT}
_en_vals = {k: i18n.translate(k, "en") for k in IT}
check("cat8: English is a real translation, not a copy of Italian",
      sum(1 for k in IT if _it_vals[k] != _en_vals[k]) > len(IT) * 0.6,
      sum(1 for k in IT if _it_vals[k] == _en_vals[k]))

# =====================================================================
# 2. Fallback: deterministic, and never an exception
# =====================================================================
check("fb1: an unknown key returns the key itself, verbatim",
      i18n.translate("no.such.key", "en") == "no.such.key")
check("fb2: ...in every language, including the default",
      i18n.translate("no.such.key", "it") == "no.such.key")
check("fb3: an unknown LANGUAGE falls back to Italian, it does not raise",
      i18n.translate("nav.conformity", "de") == i18n.translate("nav.conformity", "it"))
check("fb4: an empty/None language is the default",
      i18n.normalize_language(None) == "it" and i18n.normalize_language("") == "it")
check("fb5: regional and cased codes normalize",
      all(i18n.normalize_language(c) == "en" for c in ("en", "EN", "en-US", "en_GB")))
check("fb6: the selector's own display names normalize back to codes",
      i18n.normalize_language("English") == "en" and i18n.normalize_language("Italiano") == "it")
check("fb7: has_key distinguishes a real hit from a fallback",
      i18n.has_key("nav.conformity", "en") and not i18n.has_key("no.such.key", "en"))
check("fb8: a missing placeholder argument leaves the placeholder visible "
      "instead of raising",
      "{options}" in i18n.translate("conformity.applicability.choose_first", "it"))
check("fb9: a supplied placeholder is interpolated",
      i18n.translate("conformity.applicability.choose_first", "en", options="A / B")
      .endswith("(A / B) before confirming."),
      i18n.translate("conformity.applicability.choose_first", "en", options="A / B"))

# =====================================================================
# 2b. SESSION ISOLATION -- the property that makes this a foundation
#
# The first cut stored the selected language in a module-level global.
# One Gradio process serves every connected browser, so a user switching
# to English changed what the NEXT render produced for everyone. That
# needs two sessions to reproduce, which is exactly why it survived
# single-user testing. These tests exist so it cannot come back.
# =====================================================================
check("iso1: the module exposes no mutable process-global language",
      not hasattr(i18n, "_current_language"),
      [n for n in vars(i18n) if "current_language" in n])
check("iso2: the selection lives in a ContextVar",
      isinstance(vars(i18n)["_active_language"], contextvars.ContextVar))
check("iso3: its default is Italian, so a context that never chooses gets it",
      vars(i18n)["_active_language"].get() == "it")

# Two independent contexts, resolved concurrently.
_barrier = threading.Barrier(2, timeout=10)
_seen: dict[str, tuple[str, str]] = {}


def _resolve_in(lang):
    with i18n.use_language(lang):
        _barrier.wait()          # both threads are now INSIDE their own context
        _seen[lang] = (i18n.current_language(), i18n.t("nav.conformity"))


_threads = [threading.Thread(target=_resolve_in, args=(lg,)) for lg in ("it", "en")]
for _th in _threads:
    _th.start()
for _th in _threads:
    _th.join()

check("iso4: two concurrent sessions resolve DIFFERENT languages at the same time",
      _seen.get("it") == ("it", "Conformità") and _seen.get("en") == ("en", "Compliance"),
      _seen)
check("iso5: neither contaminated the other -- each saw only its own choice",
      _seen["it"][1] != _seen["en"][1], _seen)
check("iso6: and neither leaked into the context that started them",
      i18n.current_language() == "it" and i18n.t("nav.conformity") == "Conformità",
      i18n.current_language())

# Nesting restores, it does not stack up.
with i18n.use_language("en"):
    _outer = i18n.current_language()
    with i18n.use_language("it"):
        _inner = i18n.current_language()
    _after_inner = i18n.current_language()
check("iso7: use_language nests and restores exactly",
      (_outer, _inner, _after_inner) == ("en", "it", "en"),
      (_outer, _inner, _after_inner))
check("iso8: and the outermost context is back to the default afterwards",
      i18n.current_language() == "it")

# A set_language() inside a worker must not escape that worker.
def _set_and_exit():
    i18n.set_language("en")


_worker = threading.Thread(target=_set_and_exit)
_worker.start()
_worker.join()
check("iso9: set_language() in another thread cannot change this one",
      i18n.current_language() == "it", i18n.current_language())

check("iso10: translate() with an explicit language ignores the context entirely",
      i18n.translate("nav.conformity", "en") == "Compliance"
      and i18n.current_language() == "it")


# =====================================================================
# 3. Internal enums and keys are untouched by language
# =====================================================================
ENGINE_STATUSES = ("Covered", "To verify", "Missing", "Not applicable")
for lang in ("it", "en"):
    i18n.set_language(lang)
    check(f"enum1[{lang}]: the applicability enum is unchanged",
          cv.APPLICABILITY_CODES == ("APPLIES", "NOT_APPLICABLE", "TO_VERIFY"),
          cv.APPLICABILITY_CODES)
    check(f"enum2[{lang}]: the coverage enum is unchanged",
          cv.COVERAGE_CODES == ("COVERED", "TO_VERIFY", "MISSING"), cv.COVERAGE_CODES)
    check(f"enum3[{lang}]: the Radio submits ENUM CODES, never words",
          [v for _, v in cv.applicability_choices()] == list(cv.APPLICABILITY_CODES)
          and [v for _, v in cv.coverage_choices()] == list(cv.COVERAGE_CODES),
          cv.applicability_choices())
    check(f"enum4[{lang}]: coverage bucket identifiers are unchanged",
          cv.BUCKET_KEYS == ("covered", "partial", "no_evidence", "not_applicable", "unassessed"),
          cv.BUCKET_KEYS)
    check(f"enum5[{lang}]: status label lookup is keyed on the ENGINE's words",
          set(cv.status_labels()) == set(ENGINE_STATUSES), sorted(cv.status_labels()))
    check(f"enum6[{lang}]: an unknown status passes through untranslated rather than "
          f"vanishing", cv.status_label("Brand New") == "Brand New")

i18n.set_language("it")
check("enum7: the same engine status yields DIFFERENT words per language",
      cv.status_label("To verify", "it") == "Da verificare"
      and cv.status_label("To verify", "en") == "To verify")
check("enum8: ...and the same CODE yields different words too",
      cv.applicability_label("APPLIES", "it") == "Si applica"
      and cv.applicability_label("APPLIES", "en") == "Applies")

# A translated label must never be usable as a lookup key anywhere.
check("key1: no label -> code reverse map survives on the view module",
      not hasattr(cv, "APPLIES_LABELS_REVERSE") and not hasattr(cv, "COVERAGE_LABELS_REVERSE"))
src = (Path(__file__).parent / "app.py").read_text(encoding="utf-8")
check("key2: app.py no longer reverse-maps a visible label to an enum",
      "LABELS_REVERSE" not in src)
check("key3: the filter tokens the callbacks branch on are NOT catalog values",
      all(tok not in set(_en_vals.values()) or tok in set(_it_vals.values())
          for tok in ("Da completare", "Tutti", "Coperti", "Non applicabili")))

# =====================================================================
# 4. A real project: switching language changes words, never data
# =====================================================================
ACTOR = "QA"
with tempfile.TemporaryDirectory() as tmp:
    proj = create_project(Path(tmp), project_name="ZZ_Loc_Probe", client="X")
    repo = IndustrialRepository(cv.ccms_db_path(proj))
    t_a = repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
    repo.create_publication("PUB-MAIN", "Manuale", [t_a], source_lang="it-IT")
    svc = cv.ComplianceService(repo)
    pack = csp.get_pack(csp.MACHINERY_DOC_PACK["pack_id"])
    scope = "SCOPE-LOC"
    svc.create_scope(scope, "PUB-MAIN", actor=ACTOR, title=pack["name"], jurisdiction="EU")
    svc.import_requirement_pack(pack, scope_id=scope, actor=ACTOR,
                                groups=[csp.GROUP_FULL_MACHINERY])
    rid = "MR2023-A3-17441-INTENDED-USE"
    svc.set_applicability(scope, rid, "APPLIES", actor=ACTOR)
    cv.link_ccms_object_as_evidence(svc, "topic", t_a, svc._object_current_version("topic", t_a),
                                    rid, actor=ACTOR)
    svc.set_assessment(scope, rid, "COVERED", actor=ACTOR, comment="ok")

    def snapshot():
        q = lambda s: svc.conn.execute(s).fetchone()[0]
        return {
            "applicability": q("SELECT COUNT(*) FROM compliance_applicability"),
            "assessments": q("SELECT COUNT(*) FROM compliance_assessment"),
            "evidence": q("SELECT COUNT(*) FROM compliance_evidence"),
            "audit": q("SELECT COUNT(*) FROM audit_events"),
            "applies_value": q(f"SELECT applies FROM compliance_applicability "
                               f"WHERE requirement_id='{rid}'"),
            "status_value": q(f"SELECT status FROM compliance_assessment "
                              f"WHERE requirement_id='{rid}'"),
        }

    i18n.set_language("it")
    before = snapshot()
    it_status = {r: svc.compute_status(scope, r) for r in svc.requirements_in_scope(scope)}
    it_cov = cv.documentation_coverage(svc, scope)
    it_rows = cv.requirement_list_rows(svc, scope)

    i18n.set_language("en")
    after = snapshot()
    en_status = {r: svc.compute_status(scope, r) for r in svc.requirements_in_scope(scope)}
    en_cov = cv.documentation_coverage(svc, scope)
    en_rows = cv.requirement_list_rows(svc, scope)

    check("data1: switching language wrote nothing to the project", before == after,
          {"before": before, "after": after})
    check("data2: the persisted enum values are the engine's, not words",
          after["applies_value"] == "APPLIES" and after["status_value"] == "COVERED", after)
    check("data3: compute_status is byte-identical in both languages -- the status ENGINE "
          "does not know what language the UI is in", it_status == en_status,
          [k for k in it_status if it_status[k] != en_status[k]][:5])
    check("data4: every coverage COUNT is identical",
          all(it_cov[k] == en_cov[k] for k in
              ("total", "assessed", "not_assessed", "applicable", "not_applicable",
               "covered", "partial", "no_evidence", "needs_review", "ratio_percent")),
          {k: (it_cov[k], en_cov[k]) for k in ("total", "covered", "ratio_percent")})
    check("data5: ...while the coverage LABELS did change",
          it_cov["ratio_label"] != en_cov["ratio_label"]
          and it_cov["title"] != en_cov["title"], (it_cov["title"], en_cov["title"]))
    check("data6: the requirement list returns the same requirements, in the same order",
          [r["requirement_id"] for r in it_rows] == [r["requirement_id"] for r in en_rows])
    check("data7: ...carrying the same engine status per requirement",
          [r["status"] for r in it_rows] == [r["status"] for r in en_rows])
    check("data8: bucket identifiers do not translate; only their labels do",
          [r["bucket"] for r in cv.to_complete_rows(svc, scope)]
          == [r["bucket"] for r in (i18n.set_language("it"),
                                    cv.to_complete_rows(svc, scope))[1]])

    i18n.set_language("en")
    en_gaps = cv.to_complete_rows(svc, scope)
    i18n.set_language("it")
    it_gaps = cv.to_complete_rows(svc, scope)
    check("data9: the Panoramica queue holds the same requirements in both languages",
          [g["requirement_id"] for g in it_gaps] == [g["requirement_id"] for g in en_gaps])
    check("data10: ...with translated next-step advice",
          it_gaps and en_gaps and it_gaps[0]["next_step"] != en_gaps[0]["next_step"],
          (it_gaps[0]["next_step"], en_gaps[0]["next_step"]) if it_gaps else None)

    # Verified legal content is NOT localization.
    i18n.set_language("en")
    en_titles = [r["title"] for r in cv.requirement_list_rows(svc, scope)]
    i18n.set_language("it")
    it_titles = [r["title"] for r in cv.requirement_list_rows(svc, scope)]
    check("legal1: requirement titles are pack content and are NOT translated -- an English "
          "pack would need independent verification against the EN consolidated text, "
          "not a translation", en_titles == it_titles)
    repo.close()

# =====================================================================
# 5. The localized-component registry
#
# Components and updates used to be TWO hand-maintained lists that had to
# stay in the same order; at 68 entries that already needed an AST check
# to prove they agreed. One list now derives both, so the ordering bug is
# not merely absent, it is unrepresentable.
# =====================================================================
import app  # noqa: E402

check("reg1: the registry holds every localized component",
      len(app.LOC) > 60, len(app.LOC))
check("reg2: components and updates are the same length, by construction",
      len(app.LOC.components()) == len(app.LOC.updates()) == len(app.LOC))
check("reg3: there is no second hand-maintained target list left",
      not hasattr(app, "_language_switch_targets"))
check("reg4: every registered component is a real Gradio block",
      all(hasattr(c, "_id") or hasattr(c, "elem_id") for c in app.LOC.components()))

with i18n.use_language("it"):
    _it_updates = [dict(u) for u in app.LOC.updates()]
with i18n.use_language("en"):
    _en_updates = [dict(u) for u in app.LOC.updates()]
_differing = sum(1 for a, b in zip(_it_updates, _en_updates) if a != b)
check("reg5: switching language changes most registered components",
      _differing > len(app.LOC) * 0.7, (_differing, len(app.LOC)))
check("reg6: an update only ever carries presentation props -- never a value that "
      "could reach the engine",
      all(set(u) <= {"value", "label", "info", "placeholder", "choices", "__type__"}
          for u in _en_updates),
      sorted({k for u in _en_updates for k in u}))

i18n.set_language(i18n.DEFAULT_LANGUAGE)
check("end1: the suite leaves the process on the default language",
      i18n.get_language() == "it")

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
