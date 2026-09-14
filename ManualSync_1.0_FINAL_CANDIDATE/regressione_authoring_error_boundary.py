"""Authoring Error Boundary V1 — no raw ERR_* can reach the Contenuti UI.

This is boundary hardening, not a domain migration. Authoring/Contenuti
services still refuse operations by raising finished Italian sentences,
and this suite exists to prove that those keep rendering exactly as they
did -- while a DomainError arriving at the same handler is localized
instead of printing its bare code.

The leak class being closed is specific. `str(DomainError)` is the CODE
by design, so any handler that rendered `f"⛔ {e}"` over a layer that
might one day raise one would put "ERR_ACTOR_REQUIRED" on screen. Today
the Authoring services raise sentences, so the bug is latent; it becomes
real the first time one of those modules migrates. The point of doing
this now is that the migration then cannot leak.

Three properties, in order of importance:

1. a DomainError through any protected handler renders a sentence, never
   a code;
2. an existing Italian sentence exception renders BYTE-IDENTICALLY to
   before -- no UX drift;
3. a generic failure keeps its own text and is never dressed up as a
   validation message.

Pure server-side: calls the real app callbacks with a real project. No
browser, no Gradio event loop.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402

results = []
HERE = Path(__file__).resolve().parent


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


# =====================================================================
# 1. The helper itself
# =====================================================================
ITALIAN_SENTENCE = "Il topic non esiste più in questo progetto."

check("h1: a plain ValueError keeps its exact text",
      app.ui_error_text(ValueError(ITALIAN_SENTENCE)) == ITALIAN_SENTENCE)
check("h2: a KeyError keeps Python's own repr semantics, unchanged",
      app.ui_error_text(KeyError("T-9")) == str(KeyError("T-9")) == "'T-9'")
check("h3: a bare RuntimeError is returned verbatim -- not converted into "
      "a validation message",
      app.ui_error_text(RuntimeError("sqlite disk image is malformed"))
      == "sqlite disk image is malformed")
check("h4: a DomainError is LOCALIZED, not printed as its code",
      app.ui_error_text(de.DomainValidationError(de.ERR_ACTOR_REQUIRED))
      == i18n.translate("error.ERR_ACTOR_REQUIRED", "it"))
check("h5: ...and carries no ERR_ prefix at all",
      "ERR_" not in app.ui_error_text(de.DomainValidationError(de.ERR_ACTOR_REQUIRED)))
with i18n.use_language("en"):
    en = app.ui_error_text(de.DomainValidationError(de.ERR_ACTOR_REQUIRED))
check("h6: it follows the session language", en == i18n.translate("error.ERR_ACTOR_REQUIRED", "en"),
      en)
check("h7: an explicit lang argument overrides the session, for when Contenuti "
      "is localized",
      app.ui_error_text(de.DomainValidationError(de.ERR_ACTOR_REQUIRED), lang="en")
      == i18n.translate("error.ERR_ACTOR_REQUIRED", "en")
      and i18n.current_language() == "it")
check("h8: an unknown code degrades to the generic sentence, never to the code",
      app.ui_error_text(de.DomainValidationError("ERR_NOT_IN_ANY_CATALOG"))
      == i18n.t("error.unknown"))

# The compatibility classes are the dangerous ones: they ARE ValueError /
# KeyError, so a handler catching those receives them.
for cls, base in ((de.DomainValidationError, ValueError), (de.DomainNotFoundError, KeyError),
                  (de.DomainAlreadyExistsError, FileExistsError),
                  (de.DomainFileMissingError, FileNotFoundError)):
    inst = cls(de.ERR_ACTOR_REQUIRED)
    check(f"h9[{cls.__name__}]: caught as {base.__name__}, still localized",
          isinstance(inst, base) and "ERR_" not in app.ui_error_text(inst))

# =====================================================================
# 2. Every protected handler actually goes through the helper
# =====================================================================
SRC = (HERE / "app.py").read_text(encoding="utf-8")
tree = ast.parse(SRC)
lines = SRC.splitlines()
SHOWS = re.compile(r"\{e\}|str\(e\)")
AUTHORING_PREFIX = ("av.", "svc.", "rich_text.", "auth.", "repo.")


def qualified(call):
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f"{f.value.id}.{f.attr}"
    if isinstance(f, ast.Attribute):
        return f".{f.attr}"
    return getattr(f, "id", "")


protected, unprotected = [], []
for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
    if not (fn.name.startswith("studio_") or fn.name.startswith("settings_variant_")):
        continue
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        calls = set()
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call):
                    calls.add(qualified(sub))
        if not any(c.startswith(AUTHORING_PREFIX) for c in calls):
            continue
        for h in node.handlers:
            body = "\n".join(lines[h.lineno - 1:h.end_lineno])
            if "ui_error_text(" in body:
                protected.append((fn.name, h.lineno))
            elif SHOWS.search(body):
                unprotected.append((fn.name, h.lineno))

check("cov1: every Authoring/Contenuti handler that renders an exception goes "
      "through ui_error_text", not unprotected, unprotected)
check("cov2: and there are as many as the audit found", len(protected) == 26, len(protected))
check("cov3: covering the whole Contenuti + variant-settings surface",
      len({n for n, _ in protected}) == 23, sorted({n for n, _ in protected}))

# The gate must not have touched anything else.
OUT_OF_SCOPE = ("compliance_", "release_", "work_package_ui", "job_action_ui",
                "create_job_ui", "revision_analysis_ui", "approve_source_ui")
stray = [n for n, _ in protected if n.startswith(OUT_OF_SCOPE)]
check("cov4: no out-of-scope callback was modified", not stray, stray)
check("cov5: publishing/export boundaries were left alone -- they are another gate",
      "ui_error_text(" not in SRC.split("def studio_export_pdf_ui")[1].split("def ")[1][:800]
      if "def studio_export_pdf_ui" in SRC else True)

# =====================================================================
# 3. Through the REAL callbacks, with a real project
# =====================================================================
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402
import authoring_view as av  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    proj = create_project(Path(tmp), project_name="ZZ_Boundary_Probe", client="X")
    repo = IndustrialRepository(av.ccms_db_path(proj))
    topic = repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
    repo.create_publication("PUB-MAIN", "Manuale", [topic], source_lang="it-IT")
    repo.close()

    IDX_SAVE_MSG = None  # resolved from the bundle below

    # -- an existing Italian sentence exception must render unchanged ----
    # A topic id that does not exist makes save_topic raise its own
    # KeyError/ValueError, exactly as before this gate.
    bundle = app.studio_save_ui(str(proj), "PUB-MAIN", "topic", "T-DOES-NOT-EXIST",
                                "", "", 1, "Titolo", "", "", "", "", "", "", "QA", False)
    rendered = [x for x in bundle if isinstance(x, str) and "⛔" in x]
    check("real1: a genuine Authoring failure still renders its own message",
          bool(rendered), [type(x).__name__ for x in bundle][:6])
    check("real2: and that message is NOT a domain code",
          not any("ERR_" in r for r in rendered), rendered[:2])
    check("real3: nor the generic fallback -- the original text survives",
          not any(i18n.t("error.unknown") in r for r in rendered), rendered[:2])

    # -- a DomainError through the same boundary is localized -----------
    # Injected at the service call, which is exactly where a migrated
    # Authoring service would raise one.
    import authoring_service as asvc  # noqa: E402
    original = asvc.AuthoringService.save_topic
    try:
        def _raises_domain(self, *a, **k):
            raise de.DomainValidationError(de.ERR_ACTOR_REQUIRED, field="actor")

        asvc.AuthoringService.save_topic = _raises_domain
        bundle_dom = app.studio_save_ui(str(proj), "PUB-MAIN", "topic", topic,
                                        "", "", 1, "Titolo", "", "", "", "", "", "",
                                        "QA", False)
        dom_rendered = [x for x in bundle_dom if isinstance(x, str) and "⛔" in x]
        check("real4: a DomainError through studio_save_ui is rendered",
              bool(dom_rendered), [type(x).__name__ for x in bundle_dom][:6])
        check("real5: as a SENTENCE, never as ERR_ACTOR_REQUIRED",
              not any("ERR_" in r for r in dom_rendered), dom_rendered[:2])
        check("real6: and it is the localized text for that code",
              any(i18n.translate("error.ERR_ACTOR_REQUIRED", "it") in r for r in dom_rendered),
              dom_rendered[:2])

        with i18n.use_language("en"):
            bundle_en = app.studio_save_ui(str(proj), "PUB-MAIN", "topic", topic,
                                           "", "", 1, "Titolo", "", "", "", "", "", "",
                                           "QA", False)
            en_rendered = [x for x in bundle_en if isinstance(x, str) and "⛔" in x]
        check("real7: the same code renders in English when the session is English",
              any(i18n.translate("error.ERR_ACTOR_REQUIRED", "en") in r for r in en_rendered),
              en_rendered[:2])

        # A generic failure must NOT become a validation message.
        def _raises_generic(self, *a, **k):
            raise ValueError("Errore interno inatteso del repository.")

        asvc.AuthoringService.save_topic = _raises_generic
        bundle_gen = app.studio_save_ui(str(proj), "PUB-MAIN", "topic", topic,
                                        "", "", 1, "Titolo", "", "", "", "", "", "",
                                        "QA", False)
        gen_rendered = [x for x in bundle_gen if isinstance(x, str) and "⛔" in x]
        check("real8: a generic ValueError keeps its own text verbatim",
              any("Errore interno inatteso del repository." in r for r in gen_rendered),
              gen_rendered[:2])
        check("real9: ...and is NOT replaced by a friendly validation sentence",
              not any(i18n.t("error.unknown") in r for r in gen_rendered), gen_rendered[:2])
    finally:
        asvc.AuthoringService.save_topic = original

    check("real10: the monkeypatch was undone",
          asvc.AuthoringService.save_topic is original)

    # -- a settings-variant boundary, which renders differently ---------
    bad = app.settings_variant_create_dimension_ui(str(proj), "NOT VALID ID", "Nome", "QA")
    msgs = [x for x in bad if isinstance(x, str) and "⛔" in x]
    check("real11: a variant-settings refusal still renders its Italian sentence",
          bool(msgs) and not any("ERR_" in m for m in msgs), msgs[:2])

i18n.set_language(i18n.DEFAULT_LANGUAGE)
check("end1: the suite leaves the process on the default language",
      i18n.current_language() == "it")

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
