"""Domain Error Semantics V1 — a refusal carries a code, not a sentence.

The coupling this removes was concrete: services refused operations by
raising ready-made Italian text::

    raise ValueError("actor è obbligatorio per registrare questa decisione.")

With the UI in English that sentence is simply wrong, and the only ways to
use it were to show it untranslated or to parse it -- and parsing message
text breaks the moment a word changes. So the domain now says WHICH RULE
fired; the UI decides what words to use.

What this suite defends:

* the same condition yields the same code in either language, and only
  the presentation text differs;
* parameters survive from the raise site to the sentence;
* no domain or service module imports i18n -- a domain module that could
  reach a catalog would eventually use one;
* nothing branches on translated text, and no translated text is stored;
* both catalogs carry every migrated code;
* an unknown code degrades to ONE safe sentence, never to a raw code and
  never to a Python exception message.

Pure server-side: real projects through the supported APIs, no browser.
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import json
import sqlite3
import string
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compliance_starter_packs as csp  # noqa: E402
import compliance_view as cv  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


HERE = Path(__file__).resolve().parent


def _catches(cls, base):
    """Does `except <base>` still catch this class? Asked, not assumed."""
    try:
        raise cls("ERR_SAMPLE")
    except base:
        return True
    except Exception:
        return False


def placeholders(text):
    """The {names} a catalog message expects."""
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}

# =====================================================================
# 1. The error model
# =====================================================================
e = de.DomainValidationError(de.ERR_ACTOR_REQUIRED, field="actor")
check("mod1: an error carries a stable code", e.code == "ERR_ACTOR_REQUIRED")
check("mod2: and structured parameters", e.params == {"field": "actor"})
check("mod3: str() is the CODE, never a sentence -- text here is what the model removes",
      str(e) == "ERR_ACTOR_REQUIRED")
check("mod4: repr() adds the parameters, for diagnostics",
      repr(e) == "DomainValidationError('ERR_ACTOR_REQUIRED', field='actor')", repr(e))
check("mod5: serialization is deterministic -- params sorted, no identity",
      de.DomainValidationError("X", b=2, a=1).as_dict() == {"code": "X", "params": {"a": 1, "b": 2}}
      and json.dumps(de.DomainValidationError("X", b=2, a=1).as_dict(), sort_keys=True)
      == json.dumps(de.DomainValidationError("X", a=1, b=2).as_dict(), sort_keys=True))
check("mod6: two errors with the same code and params are equal",
      de.DomainValidationError("X", a=1) == de.DomainValidationError("X", a=1)
      and de.DomainValidationError("X", a=1) != de.DomainValidationError("X", a=2))
check("mod7: a subclass default code applies when none is given",
      de.DomainNotFoundError().code == "ERR_NOT_FOUND")

# Exception chaining survives.
try:
    try:
        raise sqlite3.OperationalError("no such table")
    except sqlite3.OperationalError as cause:
        raise de.DomainValidationError("ERR_X") from cause
except de.DomainError as wrapped:
    chained = isinstance(wrapped.__cause__, sqlite3.OperationalError)
check("mod8: original exceptions can be chained and survive", chained)

# Compatibility bases -- deliberate, see domain_errors.__doc__.
check("compat1: a validation error is still a ValueError",
      isinstance(de.DomainValidationError("X"), ValueError))
check("compat2: a not-found error is still a KeyError, and does not quote its message",
      isinstance(de.DomainNotFoundError("X"), KeyError) and str(de.DomainNotFoundError("X")) == "X")
check("compat3: an already-exists error is still a FileExistsError",
      isinstance(de.DomainAlreadyExistsError("X"), FileExistsError)
      and str(de.DomainAlreadyExistsError("X")) == "X")
check("compat4: a missing-file error is still a FileNotFoundError",
      isinstance(de.DomainFileMissingError("X"), FileNotFoundError)
      and str(de.DomainFileMissingError("X")) == "X")

# The dual bases must not disturb the contract. OSError in particular
# rewrites args/str on its own, so each class is checked, not assumed.
COMPAT = [(de.DomainValidationError, ValueError), (de.DomainNotFoundError, KeyError),
          (de.DomainAlreadyExistsError, FileExistsError),
          (de.DomainFileMissingError, FileNotFoundError)]
for cls, base in COMPAT:
    inst = cls("ERR_SAMPLE", who="unit", n=2)
    check(f"compat5[{cls.__name__}]: isinstance of {base.__name__} and of DomainError",
          isinstance(inst, base) and isinstance(inst, de.DomainError))
    check(f"compat6[{cls.__name__}]: code, params, str and repr stay deterministic",
          inst.code == "ERR_SAMPLE" and inst.params == {"who": "unit", "n": 2}
          and str(inst) == "ERR_SAMPLE"
          and repr(inst) == f"{cls.__name__}('ERR_SAMPLE', n=2, who='unit')"
          and inst.as_dict() == {"code": "ERR_SAMPLE", "params": {"n": 2, "who": "unit"}},
          (str(inst), repr(inst), inst.as_dict()))
    check(f"compat7[{cls.__name__}]: an except clause on the builtin still catches it",
          _catches(cls, base))

# =====================================================================
# 2. The domain layer stays independent of language
# =====================================================================
DOMAIN_MODULES = ["domain_errors.py", "compliance_intelligence.py", "ccms_industrial.py",
                  "authoring_service.py", "project_lifecycle.py", "compliance_starter_packs.py",
                  "workflow_management.py", "release_manager.py", "publishing_output.py"]
importers = []
for name in DOMAIN_MODULES:
    tree = ast.parse((HERE / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "i18n" for a in node.names):
            importers.append(name)
        if isinstance(node, ast.ImportFrom) and node.module == "i18n":
            importers.append(name)
check("dom1: no domain/service module imports i18n", not importers, importers)

err_src = (HERE / "domain_errors.py").read_text(encoding="utf-8")
check("dom2: the error model itself imports nothing but typing",
      "import i18n" not in err_src and "import gradio" not in err_src
      and "locales" not in err_src)
check("dom3: no migrated service calls t() or translate()",
      not any("i18n.t(" in (HERE / m).read_text(encoding="utf-8")
              or "translate(" in (HERE / m).read_text(encoding="utf-8")
              for m in ("compliance_intelligence.py", "project_lifecycle.py")))

# =====================================================================
# 3. Same condition -> same code, in either language
# =====================================================================
ACTOR = "QA"
with tempfile.TemporaryDirectory() as tmp:
    proj = create_project(Path(tmp), project_name="ZZ_Err_Probe", client="X")
    repo = IndustrialRepository(cv.ccms_db_path(proj))
    topic = repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
    repo.create_publication("PUB-MAIN", "Manuale", [topic], source_lang="it-IT")
    svc = cv.ComplianceService(repo)
    pack = csp.get_pack(csp.MACHINERY_DOC_PACK["pack_id"])
    scope = "SCOPE-ERR"
    svc.create_scope(scope, "PUB-MAIN", actor=ACTOR, title=pack["name"], jurisdiction="EU")
    svc.import_requirement_pack(pack, scope_id=scope, actor=ACTOR,
                                groups=[csp.GROUP_FULL_MACHINERY])
    RID = "MR2023-A3-17441-INTENDED-USE"

    def capture(fn):
        try:
            fn()
        except de.DomainError as exc:
            return exc
        return None

    CONDITIONS = {
        de.ERR_ACTOR_REQUIRED:
            lambda: svc.set_applicability(scope, RID, "APPLIES", actor=""),
        de.ERR_JUSTIFICATION_REQUIRED:
            lambda: svc.set_applicability(scope, RID, "NOT_APPLICABLE", actor=ACTOR),
        de.ERR_COVERAGE_REQUIRES_APPLIES:
            lambda: svc.set_assessment(scope, "MR2023-A3-17442-PPE", "COVERED", actor=ACTOR),
        de.ERR_EVIDENCE_OBJECT_NOT_FOUND:
            lambda: svc.add_evidence("EV-X", "topic", actor=ACTOR, object_id="T-NOPE",
                                     object_version=7),
        de.ERR_REQUIREMENT_NOT_FOUND:
            lambda: svc.link_evidence_requirement("EV-REAL", "MR-NOPE", actor=ACTOR),
        # A project where the pack was never imported -- the real shape of
        # this refusal, not a contrived argument to a service that has it.
        de.ERR_PACK_NOT_INSTALLED:
            lambda: cv.apply_pack_update(bare_svc, pack, scope_id="SCOPE-BARE", actor=ACTOR),
    }
    svc.add_evidence("EV-REAL", "topic", actor=ACTOR, object_id=topic,
                     object_version=svc._object_current_version("topic", topic))

    bare_proj = create_project(Path(tmp), project_name="ZZ_Err_Bare", client="X")
    bare_repo = IndustrialRepository(cv.ccms_db_path(bare_proj))
    bare_svc = cv.ComplianceService(bare_repo)

    raised_it, raised_en = {}, {}
    for code, fn in CONDITIONS.items():
        with i18n.use_language("it"):
            raised_it[code] = capture(fn)
        with i18n.use_language("en"):
            raised_en[code] = capture(fn)

    for code in CONDITIONS:
        check(f"code[{code}]: the condition raises it",
              raised_it[code] is not None and raised_it[code].code == code,
              repr(raised_it[code]))
    check("lang1: the SAME condition yields the SAME code in Italian and English",
          all(raised_it[c].as_dict() == raised_en[c].as_dict() for c in CONDITIONS),
          {c: (raised_it[c].as_dict(), raised_en[c].as_dict()) for c in CONDITIONS
           if raised_it[c].as_dict() != raised_en[c].as_dict()})
    check("lang2: parameters survive intact from the raise site",
          raised_it[de.ERR_EVIDENCE_OBJECT_NOT_FOUND].params
          == {"kind": "topic", "object_id": "T-NOPE", "version": 7},
          raised_it[de.ERR_EVIDENCE_OBJECT_NOT_FOUND].params)
    check("lang3: no raised message contains Italian prose any more",
          all(str(raised_it[c]) == c for c in CONDITIONS),
          {c: str(raised_it[c]) for c in CONDITIONS if str(raised_it[c]) != c})

    # A refused operation must not have written anything.
    counts = lambda: svc.conn.execute(  # noqa: E731
        "SELECT (SELECT COUNT(*) FROM compliance_applicability)"
        " || '/' || (SELECT COUNT(*) FROM compliance_assessment)").fetchone()[0]
    before = counts()
    for fn in CONDITIONS.values():
        capture(fn)
    check("fail1: a refused operation records nothing", counts() == before, (before, counts()))
    bare_repo.close()

    # No translated text may be persisted.
    audit_blob = json.dumps([dict(zip([c[0] for c in r.description], r)) if False else list(r)
                             for r in svc.conn.execute("SELECT * FROM audit_events")],
                            ensure_ascii=False, default=str)
    sentences = [i18n.translate(f"error.{c}", "it") for c in de.MIGRATED_CODES]
    check("persist1: no localized error sentence is stored in the audit trail",
          not any(s[:28] in audit_blob for s in sentences), None)
    repo.close()

# =====================================================================
# 4. The localization boundary
# =====================================================================
missing = [(c, lg) for c in de.MIGRATED_CODES for lg in i18n.SUPPORTED_LANGUAGES
           if not i18n.has_key(f"error.{c}", lg)]
check("cat1: both catalogs carry every migrated code", not missing, missing)
check("cat2: and a generic fallback sentence",
      all(i18n.has_key("error.unknown", lg) for lg in i18n.SUPPORTED_LANGUAGES))

it_texts = {c: i18n.translate(f"error.{c}", "it") for c in de.MIGRATED_CODES}
en_texts = {c: i18n.translate(f"error.{c}", "en") for c in de.MIGRATED_CODES}
check("cat3: every error sentence differs between the two languages",
      all(it_texts[c] != en_texts[c] for c in de.MIGRATED_CODES),
      [c for c in de.MIGRATED_CODES if it_texts[c] == en_texts[c]])
check("cat4: no error sentence leaks the code itself to the user",
      not any(c in it_texts[c] or c in en_texts[c] for c in de.MIGRATED_CODES),
      [c for c in de.MIGRATED_CODES if c in it_texts[c] or c in en_texts[c]])
check("cat5: parameterised sentences interpolate safely",
      "T-9" in i18n.translate("error.ERR_EVIDENCE_OBJECT_NOT_FOUND", "en",
                              kind="topic", object_id="T-9", version=3),
      i18n.translate("error.ERR_EVIDENCE_OBJECT_NOT_FOUND", "en",
                     kind="topic", object_id="T-9", version=3))

# ---- catalog / code consistency -------------------------------------
# An error key whose placeholders do not match the params the raise site
# actually emits renders as a literal "{object_id}" on screen. Nothing
# else in the suite would catch that, because both halves look fine on
# their own.
import re as _re  # noqa: E402

CATALOG_ERROR_KEYS = {k for lg in i18n.SUPPORTED_LANGUAGES for k in i18n.catalog_keys(lg)
                      if k.startswith("error.")}
# ALL_CODES, not MIGRATED_CODES: later gates add their own batches (the
# Authoring one is already here), and their catalog entries are not orphans.
declared = {f"error.{c}" for c in de.ALL_CODES} | {"error.unknown"}
check("cons1: no orphan error key -- every error.* entry maps to a declared code",
      CATALOG_ERROR_KEYS == declared,
      {"orphans": sorted(CATALOG_ERROR_KEYS - declared),
       "missing": sorted(declared - CATALOG_ERROR_KEYS)})
check("cons2: MIGRATED_CODES has no duplicates",
      len(de.MIGRATED_CODES) == len(set(de.MIGRATED_CODES)))
check("cons3: no two codes resolve to the same sentence -- which would mean two "
      "codes for one condition",
      len({i18n.translate(f"error.{c}", "it") for c in de.ALL_CODES})
      == len(de.ALL_CODES),
      [t_ for t_ in {i18n.translate(f"error.{c}", "it") for c in de.ALL_CODES}
       if [i18n.translate(f"error.{c}", "it") for c in de.ALL_CODES].count(t_) > 1])

# Placeholders a catalog message needs, vs params the raise sites emit.
EMITTED = {
    de.ERR_ACTOR_REQUIRED: {"field"},
    de.ERR_JUSTIFICATION_REQUIRED: {"requirement_id"},
    de.ERR_COVERAGE_REQUIRES_APPLIES: {"requirement_id"},
    de.ERR_COVERED_REQUIRES_EVIDENCE: {"requirement_id"},
    de.ERR_EVIDENCE_OBJECT_NOT_FOUND: {"kind", "object_id", "version"},
    de.ERR_EVIDENCE_NOT_FOUND: {"evidence_id"},
    de.ERR_REQUIREMENT_NOT_FOUND: {"requirement_id"},
    de.ERR_EVIDENCE_EXTERNAL_INCOMPLETE: {"kind"},
    de.ERR_EVIDENCE_INTERNAL_INCOMPLETE: {"kind"},
    de.ERR_PACK_NOT_INSTALLED: {"pack_id"},
    de.ERR_PACK_NO_REQUIREMENTS_SELECTED: set(),
    de.ERR_PROJECT_EXISTS: {"project_name"},
    de.ERR_PROJECT_MANIFEST_INVALID: {"path"},
    de.ERR_PROJECT_REPOSITORY_MISSING: {"project_dir"},
}
bad = []
for code, emitted in EMITTED.items():
    for lg in i18n.SUPPORTED_LANGUAGES:
        needed = placeholders(i18n.translate(f"error.{code}", lg))
        if not needed <= emitted:
            bad.append((code, lg, sorted(needed - emitted)))
check("cons4: every placeholder a message uses is actually emitted by its raise site",
      not bad, bad)

# And the raise sites really emit what EMITTED claims.
_src_ci = (HERE / "compliance_intelligence.py").read_text(encoding="utf-8")
_src_cv = (HERE / "compliance_view.py").read_text(encoding="utf-8")
_src_pl = (HERE / "project_lifecycle.py").read_text(encoding="utf-8")
_src_ps = (HERE / "project_services.py").read_text(encoding="utf-8")   # CP5: raises ERR_PROJECT_REPOSITORY_MISSING
_all_src = _src_ci + _src_cv + _src_pl + _src_ps
missing_sites = [c for c in de.MIGRATED_CODES if f"de.{c}" not in _all_src]
check("cons5: every declared code is actually raised somewhere in the migrated services",
      not missing_sites, missing_sites)

check("cons6: error.unknown is deterministic and exposes no internal text",
      i18n.translate("error.unknown", "it") == i18n.translate("error.unknown", "it")
      and not any(w in i18n.translate("error.unknown", lg).lower()
                  for lg in i18n.SUPPORTED_LANGUAGES
                  for w in ("exception", "traceback", "error:", "sqlite", "valueerror")),
      [i18n.translate("error.unknown", lg) for lg in i18n.SUPPORTED_LANGUAGES])
check("cons7: error.unknown takes no placeholders, so it can never render a stray {name}",
      not placeholders(i18n.translate("error.unknown", "it"))
      and not placeholders(i18n.translate("error.unknown", "en")))

import app  # noqa: E402

with i18n.use_language("it"):
    it_msg = app.domain_error_message(de.DomainValidationError(de.ERR_ACTOR_REQUIRED))
with i18n.use_language("en"):
    en_msg = app.domain_error_message(de.DomainValidationError(de.ERR_ACTOR_REQUIRED))
check("ui1: the resolver renders Italian and English for one code",
      it_msg == it_texts[de.ERR_ACTOR_REQUIRED] and en_msg == en_texts[de.ERR_ACTOR_REQUIRED],
      (it_msg, en_msg))
check("ui2: an UNKNOWN code degrades to the generic sentence, not to the raw code",
      app.domain_error_message(de.DomainValidationError("ERR_NEVER_HEARD_OF_IT"))
      == i18n.translate("error.unknown", "en" if i18n.current_language() == "en" else "it"),
      app.domain_error_message(de.DomainValidationError("ERR_NEVER_HEARD_OF_IT")))
check("ui3: a non-domain failure also degrades to the generic sentence",
      app.domain_error_message(RuntimeError("sqlite disk image is malformed"))
      == i18n.t("error.unknown"))
check("ui4: ...and never leaks the original exception text",
      "sqlite" not in app.domain_error_message(RuntimeError("sqlite disk image is malformed")))
check("ui5: technical identification stays on the diagnostic path only",
      "ERR_ACTOR_REQUIRED" in app.domain_error_detail(
          de.DomainValidationError(de.ERR_ACTOR_REQUIRED, field="actor"))
      and "ERR_ACTOR_REQUIRED" not in it_msg)

# =====================================================================
# 5. No migrated callback branches on translated text
# =====================================================================
app_src = (HERE / "app.py").read_text(encoding="utf-8")
check("ui6: the Conformità callbacks resolve codes rather than printing str(exc)",
      app_src.count('msg = "⛔ " + domain_error_message(exc)') == 4
      and 'msg = f"⛔ {exc}"' not in app_src.split("def compliance_decide_applicability_ui")[1]
      .split("def compliance_pack_preview_ui")[0])
check("ui7: the resolver is the single place a code becomes words",
      app_src.count("def domain_error_message(") == 1)
check("ui8: no migrated callback compares against a translated sentence",
      not any(t_ in app_src for t_ in (it_texts[de.ERR_COVERED_REQUIRES_EVIDENCE],
                                       en_texts[de.ERR_COVERED_REQUIRES_EVIDENCE])))

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
