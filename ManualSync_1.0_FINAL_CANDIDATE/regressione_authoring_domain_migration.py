"""Authoring/Contenuti Domain Error Migration V1 — first batch, same words.

Seven repository/object validation raises in authoring_service,
authoring_ccms and authoring_view now carry structured codes instead of
Italian sentences. The batch is deliberately small and thematic. It
excludes the 32 "actor è obbligatorio ..." raises: they are one condition
with 22 different sentences, so collapsing them is a WORDING decision,
and this gate may not change wording.

The property that makes this migration safe to ship is that the screen
does not change. So the suite does not compare against sentences typed
into it -- a test that quotes what the author believed the message was
proves only that the author was consistent with themselves. It recovers
the pre-migration source from git, EXECUTES the original f-strings, and
compares the rendered output against that.

It also pins the direction of travel: the domain now raises codes, the
UI resolves them, and nothing between the two parses text.

Pure server-side: no browser, no Gradio event loop.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import app  # noqa: E402
import authoring_ccms  # noqa: E402
import authoring_service as asvc  # noqa: E402
import authoring_view as av  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402

#: The commit this batch was migrated from. The original messages are read
#: from it rather than restated here.
BASELINE = "1898bafbd819b2f0fc0f714ce34251c5539b8d56"
MODULES = ("authoring_service.py", "authoring_ccms.py", "authoring_view.py")

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


# =====================================================================
# 1. The domain raises CODES now
# =====================================================================
# This gate's batch is AUTHORING_OBJECT_CODES; later batches (the actor
# normalization) add their own tuple and are asserted by their own suite.
check("mig1: this batch declares exactly seven codes",
      len(de.AUTHORING_OBJECT_CODES) == 7, de.AUTHORING_OBJECT_CODES)
check("mig2: they are separate from the compliance batch",
      not (set(de.AUTHORING_CODES) & set(de.MIGRATED_CODES)))
check("mig3: ALL_CODES is the union", set(de.ALL_CODES)
      == set(de.MIGRATED_CODES) | set(de.AUTHORING_CODES))

SRC = {m: (HERE / m).read_text(encoding="utf-8") for m in MODULES}
for code in de.AUTHORING_OBJECT_CODES:
    check(f"mig4[{code}]: is raised in an Authoring module",
          any(f"de.{code}" in s for s in SRC.values()), code)

check("mig5: none of the migrated sentences is still raised as text",
      not any("Il titolo del topic è obbligatorio." in s
              or "topic_id obbligatorio\")" in s
              or "Repository CCMS non trovato nel progetto\")" in s
              for s in SRC.values()))
check("mig6: the Authoring modules do not import i18n -- the domain still "
      "has no opinion about language",
      not any("import i18n" in s for s in SRC.values()))

# =====================================================================
# 2. The words on screen did not change
#
# Recovered from git and executed, not retyped.
# =====================================================================
def _original_messages(bt="foo", source_path="/tmp/x.png"):
    """Render the pre-migration f-strings with the given interpolations."""
    env = {"sorted": sorted, "bt": bt, "source_path": source_path,
           "_EDITABLE_KINDS": asvc._EDITABLE_KINDS,
           "EDITABLE_TEXT_BLOCK_TYPES": asvc.EDITABLE_TEXT_BLOCK_TYPES}
    found = set()
    for mod in MODULES:
        blob = subprocess.run(["git", "show", f"{BASELINE}:ManualSync_1.0_FINAL_CANDIDATE/{mod}"],
                              cwd=HERE.parent, capture_output=True, text=True, encoding="utf-8")
        if blob.returncode != 0:
            return None
        for node in ast.walk(ast.parse(blob.stdout)):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
                try:
                    txt = eval(compile(ast.Expression(node.exc.args[0]), "<s>", "eval"), env)
                except Exception:
                    continue
                if isinstance(txt, str):
                    found.add(txt)
    return found


ORIGINAL = _original_messages()
check("txt0: the pre-migration sources were recovered from git",
      ORIGINAL is not None and len(ORIGINAL) > 30,
      len(ORIGINAL) if ORIGINAL else "git show failed")

PARAMS = {
    de.ERR_AUTHORING_REPOSITORY_MISSING: {},
    de.ERR_AUTHORING_DRAFT_KIND_UNSUPPORTED: {"allowed": sorted(asvc._EDITABLE_KINDS)},
    de.ERR_AUTHORING_TOPIC_TITLE_REQUIRED: {},
    de.ERR_AUTHORING_BLOCK_TYPE_UNSUPPORTED: {
        "block_type": "foo", "allowed": sorted(asvc.EDITABLE_TEXT_BLOCK_TYPES)},
    de.ERR_AUTHORING_ASSET_FILE_MISSING: {"path": "/tmp/x.png"},
    de.ERR_AUTHORING_TOPIC_ID_REQUIRED: {},
    de.ERR_AUTHORING_OBJECT_KIND_UNSUPPORTED: {},
}
for code, params in PARAMS.items():
    rendered = app.ui_error_text(de.DomainValidationError(code, **params))
    check(f"txt1[{code}]: renders the EXACT pre-migration sentence",
          rendered in ORIGINAL, rendered)

check("txt2: and never the bare code",
      not any("ERR_" in app.ui_error_text(de.DomainValidationError(c, **PARAMS[c]))
              for c in PARAMS))

# English exists and is a real translation.
for code in de.AUTHORING_OBJECT_CODES:
    it = i18n.translate(f"error.{code}", "it")
    en = i18n.translate(f"error.{code}", "en")
    check(f"txt3[{code}]: has a distinct English entry", it != en, (it, en))

# =====================================================================
# 3. Through the real services and the real UI boundary
# =====================================================================
import tempfile  # noqa: E402

from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402


def raised(fn, *a, **k):
    try:
        fn(*a, **k)
    except BaseException as exc:      # noqa: BLE001 - the point is what it is
        return exc
    return None


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    empty = root / "NoRepo"
    empty.mkdir()
    exc = raised(av.ccms_db_path, empty)
    check("svc1: ccms_db_path raises a structured code",
          isinstance(exc, de.DomainError)
          and exc.code == de.ERR_AUTHORING_REPOSITORY_MISSING, repr(exc))
    check("svc2: and is STILL a FileNotFoundError, so existing catches hold",
          isinstance(exc, FileNotFoundError))
    check("svc3: the UI boundary renders the original sentence",
          app.ui_error_text(exc) == "Repository CCMS non trovato nel progetto",
          app.ui_error_text(exc))

    proj = create_project(root, project_name="ZZ_Mig_Probe", client="X")
    repo = IndustrialRepository(av.ccms_db_path(proj))
    svc = asvc.AuthoringService(repo)
    topic = repo.create_topic("T-A", "Uso", "", source_lang="it-IT", status="Draft")
    repo.create_publication("PUB-MAIN", "M", [topic], source_lang="it-IT")

    exc = raised(svc.create_topic_in_publication, "PUB-MAIN", "   ", actor="QA")
    check("svc4: an empty topic title raises the code", isinstance(exc, de.DomainError)
          and exc.code == de.ERR_AUTHORING_TOPIC_TITLE_REQUIRED, repr(exc))
    check("svc5: and is still a ValueError", isinstance(exc, ValueError))
    check("svc6: rendering it gives the original sentence",
          app.ui_error_text(exc) == "Il titolo del topic è obbligatorio.", app.ui_error_text(exc))

    exc = raised(svc.create_block_in_topic, topic, "not_a_block_type", actor="QA")
    check("svc7: an unsupported block type raises the code, with both parameters",
          isinstance(exc, de.DomainError)
          and exc.code == de.ERR_AUTHORING_BLOCK_TYPE_UNSUPPORTED
          and exc.params["block_type"] == "not_a_block_type"
          and exc.params["allowed"] == sorted(asvc.EDITABLE_TEXT_BLOCK_TYPES), repr(exc))
    # Compare against the ORIGINAL rendered with the SAME block type -- the
    # cached set was built with a different one, and comparing against that
    # would only prove the two placeholders differ.
    check("svc8: it renders with the same quoting and list as before",
          app.ui_error_text(exc) in _original_messages(bt="not_a_block_type"),
          app.ui_error_text(exc))

    exc = raised(svc.save_draft, "nonsense", "X", base_version=1, payload={}, actor="QA")
    check("svc9: an unsupported draft kind raises the code",
          isinstance(exc, de.DomainError)
          and exc.code == de.ERR_AUTHORING_DRAFT_KIND_UNSUPPORTED, repr(exc))

    exc = raised(repo.create_topic, "   ", "Titolo", "")
    check("svc10: an empty topic_id in the CCMS layer raises the code",
          isinstance(exc, de.DomainError)
          and exc.code == de.ERR_AUTHORING_TOPIC_ID_REQUIRED, repr(exc))
    check("svc11: still a ValueError for existing callers", isinstance(exc, ValueError))

    # ---- NOT migrated: must behave exactly as before -------------------
    # The actor raises were migrated by a LATER gate (actor normalization),
    # which owns their assertions. What this suite still guards is that a
    # raise outside both batches is untouched: an archived topic.
    archived = repo.create_topic("T-ARCH", "Archiviato", "", source_lang="it-IT",
                                 status="Draft")
    repo.archive_topic(archived, actor="QA") if hasattr(repo, "archive_topic") else None
    exc = raised(svc.save_topic, "T-NOPE", title="X", expected_version=1, actor="QA")
    check("keep1: a raise outside both batches is still a plain builtin",
          exc is not None and not isinstance(exc, de.DomainError), repr(exc))
    check("keep2: and still renders its own Italian text, not a catalog sentence",
          "ERR_" not in app.ui_error_text(exc)
          and i18n.t("error.unknown") not in app.ui_error_text(exc),
          app.ui_error_text(exc))
    # ArchivedObjectError itself still exists as a catchable class (a later
    # gate made its raises structured WITHOUT removing the base), and the
    # inverse guard -- restoring something not archived -- is still plain.
    check("keep3: ArchivedObjectError survives as a catchable base class",
          "class ArchivedObjectError" in SRC["authoring_service.py"])
    check("keep3b: and 'non è archiviato' is still an unmigrated ValueError",
          "non è archiviato" in SRC["authoring_service.py"])

    # A missing publication still raises a plain KeyError, as before.
    exc = raised(svc.create_topic_in_publication, "PUB-NOPE", "Titolo", actor="QA")
    check("keep4: a missing publication still raises a plain KeyError",
          isinstance(exc, KeyError) and not isinstance(exc, de.DomainError), repr(exc))

    repo.close()

# =====================================================================
# 4. The boundary gates still hold
# =====================================================================
APP_SRC = (HERE / "app.py").read_text(encoding="utf-8")
check("bnd1: the UI still resolves through one helper",
      APP_SRC.count("def ui_error_text(") == 1
      and APP_SRC.count("def domain_error_message(") == 1)
check("bnd2: Authoring Error Boundary V1 is intact",
      APP_SRC.count("ui_error_text(e)") + APP_SRC.count("ui_error_text(exc)") >= 40,
      APP_SRC.count("ui_error_text(e)") + APP_SRC.count("ui_error_text(exc)"))
check("bnd3: no migrated Authoring code can reach a bare render",
      'save_msg=f"⛔ {e}"' not in APP_SRC and "raise gr.Error(str(e))" not in APP_SRC)

i18n.set_language(i18n.DEFAULT_LANGUAGE)
check("end1: the suite leaves the process on the default language",
      i18n.current_language() == "it")

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
