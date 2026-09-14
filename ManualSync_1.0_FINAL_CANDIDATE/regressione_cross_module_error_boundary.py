"""Cross-Module UI Error Boundary V1 — no raw ERR_* anywhere in app.py.

Authoring Error Boundary V1 protected the Contenuti surface. This closes
the rest: IDML review, source approval, review decisions, grounded
authoring, publishing preview/export, translation work packages and the
job flows.

The risk is not uniform, and this suite records that rather than flatten
it. Eight of the fifteen handlers catch a BUILTIN base -- Exception,
ValueError, KeyError -- and DomainValidationError *is* a ValueError,
DomainNotFoundError *is* a KeyError. Those can receive a migrated error
today and would print its bare code. The other seven catch only a bespoke
class (IdmlReviewCorruptError, PublishingOutputError,
TranslationFormatNotSupportedError, gr.Error) and cannot; they are routed
through the same helper anyway, because "every handler that renders an
exception goes through ui_error_text" is an invariant a test can check,
whereas "the eight that happen to catch builtins" is a list someone has
to maintain correctly forever.

For all fifteen the non-domain path is a pass-through, so wording is
byte-identical to before. That is the property most of this suite checks.

Pure server-side: no browser, no Gradio event loop.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402

results = []
HERE = Path(__file__).resolve().parent
SRC = (HERE / "app.py").read_text(encoding="utf-8")
LINES = SRC.splitlines()
TREE = ast.parse(SRC)


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


# =====================================================================
# 1. No handler anywhere in app.py renders a bare exception
# =====================================================================
SHOWS = re.compile(r"\{e\}|\{exc\}|str\(e\)|str\(exc\)")
HARDENED = re.compile(r"ui_error_text\(|domain_error_message\(")

bare, hardened = [], []
for fn in [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)]:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            body = "\n".join(LINES[h.lineno - 1:h.end_lineno])
            if HARDENED.search(body):
                hardened.append((fn.name, h.lineno))
            elif SHOWS.search(body):
                bare.append((fn.name, h.lineno))

check("inv1: NO app.py handler renders a bare exception any more", not bare, bare)
check("inv2: the protected set spans both gates", len(hardened) >= 41, len(hardened))

THIS_GATE = {"revision_analysis_ui", "_idml_review_state", "_idml_review_panel_ui",
             "idml_review_confirm_ui", "guided_approve_source_ui", "approve_source_ui",
             "review_decide_ui", "grounded_queue_ui", "studio_preview_refresh_ui",
             "studio_export_pdf_ui", "studio_export_html_ui", "work_package_ui",
             "create_job_ui", "job_action_ui"}
covered = {n for n, _ in hardened}
check("inv3: every callback named in this gate is protected",
      THIS_GATE <= covered, sorted(THIS_GATE - covered))

# Authoring Error Boundary V1 must still hold.
AUTHORING = {n for n, _ in hardened
             if n.startswith("studio_") or n.startswith("settings_variant_")}
check("inv4: Authoring Error Boundary V1 is intact -- its callbacks are still protected",
      len(AUTHORING) >= 23, sorted(AUTHORING))
check("inv5: and no Contenuti handler regressed to a bare render",
      not [n for n, _ in bare
           if n.startswith("studio_") or n.startswith("settings_variant_")], bare)

# =====================================================================
# 2. The eight handlers that can genuinely receive a DomainError
# =====================================================================
DOMAIN_BASES = {"ValueError", "KeyError", "FileExistsError", "FileNotFoundError",
                "OSError", "LookupError", "Exception", "BaseException"}
at_risk = []
for fn in [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)]:
    if fn.name not in THIS_GATE:
        continue
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            if h.type is None:
                names = {"BaseException"}
            elif isinstance(h.type, ast.Tuple):
                names = {ast.unparse(x) for x in h.type.elts}
            else:
                names = {ast.unparse(h.type)}
            # Only handlers that actually RENDER the exception matter here.
            # A handler catching KeyError to return a fixed sentence cannot
            # leak anything, whatever it catches.
            body = chr(10).join(LINES[h.lineno - 1:h.end_lineno])
            if (names & DOMAIN_BASES) and HARDENED.search(body):
                at_risk.append((fn.name, h.lineno, sorted(names & DOMAIN_BASES)))

check("risk1: the audit's eight at-risk handlers are present and accounted for",
      len(at_risk) == 8, [(n, l) for n, l, _ in at_risk])
check("risk2: every at-risk handler is hardened",
      all((n, l) in hardened for n, l, _ in at_risk),
      [(n, l) for n, l, _ in at_risk if (n, l) not in hardened])
check("risk3: the compatibility bases really are what makes them reachable",
      {b for _, _, bs in at_risk for b in bs} <= DOMAIN_BASES,
      sorted({b for _, _, bs in at_risk for b in bs}))

# The bases are not theoretical: confirm the classes genuinely subclass them.
check("risk4: DomainValidationError is caught by 'except ValueError'",
      isinstance(de.DomainValidationError("X"), ValueError))
check("risk5: DomainNotFoundError is caught by 'except KeyError'",
      isinstance(de.DomainNotFoundError("X"), KeyError))

# =====================================================================
# 3. Behaviour: localized for domain, byte-identical for everything else
# =====================================================================
ITALIAN = "Il pacchetto lingua non è valido: manca il MANIFEST."
check("beh1: a non-domain exception keeps its exact text",
      app.ui_error_text(ValueError(ITALIAN)) == ITALIAN)
check("beh2: a bespoke gate exception is a pass-through too",
      app.ui_error_text(RuntimeError("Release PRODUZIONE bloccata: marker di collaudo."))
      == "Release PRODUZIONE bloccata: marker di collaudo.")
check("beh3: a DomainError is localized, never printed as its code",
      "ERR_" not in app.ui_error_text(de.DomainValidationError(de.ERR_ACTOR_REQUIRED))
      and app.ui_error_text(de.DomainValidationError(de.ERR_ACTOR_REQUIRED))
      == i18n.translate("error.ERR_ACTOR_REQUIRED", "it"))
check("beh4: a generic failure is NOT converted into a validation message",
      app.ui_error_text(RuntimeError("sqlite disk image is malformed"))
      == "sqlite disk image is malformed")

# The wrapper sentences these handlers add must survive untouched.
WRAPPERS = ["Anteprima non generabile: ", "Job non creato: ",
            "Stato di revisione IDML non leggibile: ",
            "non è leggibile o non è JSON valido: "]
for w in WRAPPERS:
    check(f"beh5[{w.strip()[:34]}]: the handler's own wording is unchanged", w in SRC, w)

# =====================================================================
# 4. Through the real callbacks
# =====================================================================
class _Boom(ValueError):
    """Stands in for a not-yet-migrated service raising an Italian sentence."""


def _rendered(fn, *a, **k):
    try:
        out = fn(*a, **k)
    except Exception as exc:          # gr.Error and friends carry the text
        return str(getattr(exc, "message", None) or exc)
    if isinstance(out, tuple):
        return " || ".join(str(x) for x in out if isinstance(x, str))
    return str(out)


# job_action_ui: except Exception -> gr.Error(ui_error_text(e))
import workflow_management as wm  # noqa: E402

# Inject INSIDE the try: WorkflowManager itself is constructed before it,
# so patching the constructor would test the uncaught path instead of the
# handler -- which is exactly the mistake this comment exists to prevent.
original = wm.WorkflowManager.get_job
try:
    def _raise_domain(self, *a, **k):
        raise de.DomainValidationError(de.ERR_ACTOR_REQUIRED, field="actor")

    wm.WorkflowManager.get_job = _raise_domain
    shown = _rendered(app.job_action_ui, "/nowhere", 1, "approve", None, "", False, "QA", True)
    check("real1: a DomainError through job_action_ui renders a sentence",
          "ERR_" not in shown, shown[:120])
    check("real2: and it is the localized text for that code",
          i18n.translate("error.ERR_ACTOR_REQUIRED", "it") in shown, shown[:120])

    with i18n.use_language("en"):
        shown_en = _rendered(app.job_action_ui, "/nowhere", 1, "approve", None, "", False, "QA", True)
    check("real3: the same code renders in English when the session is English",
          i18n.translate("error.ERR_ACTOR_REQUIRED", "en") in shown_en, shown_en[:120])

    def _raise_italian(self, *a, **k):
        raise _Boom(ITALIAN)

    wm.WorkflowManager.get_job = _raise_italian
    shown_it = _rendered(app.job_action_ui, "/nowhere", 1, "approve", None, "", False, "QA", True)
    check("real4: an existing Italian sentence still renders verbatim",
          ITALIAN in shown_it, shown_it[:120])
    check("real5: ...and is not replaced by the generic fallback",
          i18n.t("error.unknown") not in shown_it, shown_it[:120])
finally:
    wm.WorkflowManager.get_job = original
check("real6: the monkeypatch was undone", wm.WorkflowManager.get_job is original)

# create_job_ui renders in-page rather than raising, with its own wrapper.
original_create = wm.WorkflowManager.create_job_from_package
try:
    wm.WorkflowManager.create_job_from_package = _raise_domain
    out = app.create_job_ui("/nowhere", "pkg.zip", "Vendor", "2026-12-31")
    msg = " || ".join(str(x) for x in out if isinstance(x, str))
    check("real7: create_job_ui keeps its 'Job non creato' wrapper",
          "Job non creato" in msg, msg[:120])
    check("real8: and carries a sentence inside it, not a code",
          "ERR_" not in msg, msg[:120])
finally:
    wm.WorkflowManager.create_job_from_package = original_create

i18n.set_language(i18n.DEFAULT_LANGUAGE)
check("end1: the suite leaves the process on the default language",
      i18n.current_language() == "it")

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
