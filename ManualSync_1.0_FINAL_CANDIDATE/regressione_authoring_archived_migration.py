"""Authoring Archived Object Error Migration V1 — structured, still catchable.

Ten raises told the user that something was archived. Nine said it about
the object being edited; one said it about the BLOCK that contains it.
That difference matters and survives as two codes, because the remedy is
not the same: in the second case the user must restore something they did
not select.

The nine collapse, because their six trailing clauses only named the
button just pressed -- "prima di un revert", "prima di aggiungere una
tabella" -- while the remedy was identical every time. Wording follows
the product decision: "contenuto", not the internal topic/blocco labels.

The load-bearing property is compatibility. Eight app.py handlers and four
suites catch ArchivedObjectError BY NAME. ArchivedContentError inherits
both it and DomainError, so those handlers keep catching it while the UI
can resolve it into either language. This suite proves both halves, and
proves it against the REAL handler tuples in app.py rather than by
asserting the class hierarchy alone.

Pure server-side: no browser, no Gradio event loop.
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import app  # noqa: E402
import authoring_service as asvc  # noqa: E402
import authoring_view as av  # noqa: E402
import domain_errors as de  # noqa: E402
import i18n  # noqa: E402
from authoring_service import ArchivedContentError, ArchivedObjectError  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

results = []
SRC = (HERE / "authoring_service.py").read_text(encoding="utf-8")
APP_SRC = (HERE / "app.py").read_text(encoding="utf-8")


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def raised(fn, *a, **k):
    try:
        fn(*a, **k)
    except BaseException as exc:      # noqa: BLE001
        return exc
    return None


# =====================================================================
# 1. Compatibility — the half that could silently break production
# =====================================================================
e = ArchivedContentError(de.ERR_AUTHORING_CONTENT_ARCHIVED, object_id="T-9")
check("cmp1: it is still an ArchivedObjectError", isinstance(e, ArchivedObjectError))
check("cmp2: and it is a DomainError", isinstance(e, de.DomainError))
check("cmp3: str() is the code, not a sentence", str(e) == de.ERR_AUTHORING_CONTENT_ARCHIVED)
check("cmp4: repr() carries the parameters",
      repr(e) == "ArchivedContentError('ERR_AUTHORING_CONTENT_ARCHIVED', object_id='T-9')",
      repr(e))
check("cmp5: the MRO puts DomainError first, so its __str__ wins",
      [c.__name__ for c in type(e).__mro__][:3]
      == ["ArchivedContentError", "DomainError", "ArchivedObjectError"],
      [c.__name__ for c in type(e).__mro__][:4])


def caught_by(exc, base):
    try:
        raise exc
    except base:
        return True
    except BaseException:  # noqa: BLE001
        return False


check("cmp6: 'except ArchivedObjectError' still catches it",
      caught_by(ArchivedContentError(de.ERR_AUTHORING_CONTENT_ARCHIVED), ArchivedObjectError))
check("cmp7: 'except DomainError' catches it too",
      caught_by(ArchivedContentError(de.ERR_AUTHORING_CONTENT_ARCHIVED), de.DomainError))
check("cmp8: it is NOT a ValueError -- the old class was not one either, and "
      "widening what a handler catches is a behaviour change",
      not isinstance(e, ValueError))

# Against the REAL handler tuples in app.py, not a hand-written list.
app_tree = ast.parse(APP_SRC)
handler_tuples = []
for node in ast.walk(app_tree):
    if not isinstance(node, ast.Try):
        continue
    for h in node.handlers:
        if h.type is None:
            continue
        names = ({ast.unparse(x) for x in h.type.elts} if isinstance(h.type, ast.Tuple)
                 else {ast.unparse(h.type)})
        if "ArchivedObjectError" in names:
            handler_tuples.append(sorted(names))
check("cmp9: app.py still has handlers catching ArchivedObjectError by name",
      len(handler_tuples) >= 7, len(handler_tuples))

BUILTIN = {"ValueError": ValueError, "KeyError": KeyError,
           "FileNotFoundError": FileNotFoundError, "Exception": Exception,
           "ArchivedObjectError": ArchivedObjectError,
           "SharedObjectError": asvc.SharedObjectError,
           "MissingAssetError": asvc.MissingAssetError,
           "VersionConflictError": asvc.VersionConflictError}
uncaught = []
for names in handler_tuples:
    bases = tuple(BUILTIN[n] for n in names if n in BUILTIN)
    if not bases or not caught_by(ArchivedContentError(de.ERR_AUTHORING_CONTENT_ARCHIVED), bases):
        uncaught.append(names)
check("cmp10: EVERY such handler tuple still catches the migrated error",
      not uncaught, uncaught)

# =====================================================================
# 2. Two codes, and every raise in the right one
# =====================================================================
check("code1: exactly two archived codes", len(de.ARCHIVED_CODES) == 2, de.ARCHIVED_CODES)
check("code2: part of the Authoring batch", set(de.ARCHIVED_CODES) <= set(de.AUTHORING_CODES))
check("code3: no overlap with the earlier batches",
      not (set(de.ARCHIVED_CODES) & (set(de.AUTHORING_OBJECT_CODES) | set(de.ACTOR_CODES))))
for code in de.ARCHIVED_CODES:
    for lg in i18n.SUPPORTED_LANGUAGES:
        check(f"code4[{code}/{lg}]: has a catalog entry", i18n.has_key(f"error.{code}", lg))

tree = ast.parse(SRC)
fns = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
       if isinstance(n, ast.FunctionDef)]
sites = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
        continue
    if getattr(node.exc.func, "id", "") != "ArchivedContentError":
        continue
    for a in node.exc.args:
        if isinstance(a, ast.Attribute) and a.attr in de.ARCHIVED_CODES:
            owner = min(((s, e, n_) for s, e, n_ in fns if s <= node.lineno <= e),
                        key=lambda f: f[1] - f[0], default=(0, 0, "?"))[2]
            sites.setdefault(a.attr, []).append(owner)

check("site1: all ten archived raises were migrated",
      sum(len(v) for v in sites.values()) == 10, {k: len(v) for k, v in sites.items()})
check("site2: nine content-archived, one container-archived",
      len(sites[de.ERR_AUTHORING_CONTENT_ARCHIVED]) == 9
      and len(sites[de.ERR_AUTHORING_CONTAINER_ARCHIVED]) == 1,
      {k: len(v) for k, v in sites.items()})
check("site3: the container code belongs to the shared-object guard only",
      sites[de.ERR_AUTHORING_CONTAINER_ARCHIVED] == ["_guard_table_or_figure_editable"],
      sites[de.ERR_AUTHORING_CONTAINER_ARCHIVED])
check("site4: every content-archived raise passes the object id",
      SRC.count("ERR_AUTHORING_CONTENT_ARCHIVED,") == 9,
      SRC.count("ERR_AUTHORING_CONTENT_ARCHIVED,"))
check("site5: no bare ArchivedObjectError raise survives",
      "raise ArchivedObjectError(" not in SRC)
check("site6: and no archived sentence is composed in the service any more",
      "è archiviato: ripristinalo" not in SRC and "collegata a un blocco archiviato" not in SRC)

# =====================================================================
# 3. Wording
# =====================================================================
content_it = i18n.translate(f"error.{de.ERR_AUTHORING_CONTENT_ARCHIVED}", "it")
container_it = i18n.translate(f"error.{de.ERR_AUTHORING_CONTAINER_ARCHIVED}", "it")
check("txt1: the Italian is built on 'contenuto archiviato'",
      "contenuto" in content_it and "archiviato" in content_it, content_it)
check("txt2: the internal labels are gone",
      not any(w in content_it + container_it for w in ("Il topic", "Il blocco ", "topic_id")),
      (content_it, container_it))
check("txt3: the object id is still shown, so the user knows WHICH content",
      "{object_id}" in content_it, content_it)
check("txt4: the container message tells the user to restore the BLOCK, which is "
      "not what they selected",
      "blocco" in container_it and "ripristina il blocco" in container_it, container_it)
check("txt5: neither repeats the button just pressed",
      not any(w in content_it + container_it
              for w in ("un revert", "aggiungere una tabella", "aggiungere una figura",
                        "copia locale", "aggiungere blocchi")),
      (content_it, container_it))
for code in de.ARCHIVED_CODES:
    check(f"txt6[{code}]: English is a real translation",
          i18n.translate(f"error.{code}", "en") != i18n.translate(f"error.{code}", "it"))

# =====================================================================
# 4. Through the real service and the real UI boundary
# =====================================================================
with tempfile.TemporaryDirectory() as tmp:
    proj = create_project(Path(tmp), project_name="ZZ_Arch_Probe", client="X")
    repo = IndustrialRepository(av.ccms_db_path(proj))
    svc = asvc.AuthoringService(repo)
    topic = repo.create_topic("T-A", "Uso", "", source_lang="it-IT", status="Draft")
    repo.create_publication("PUB-MAIN", "M", [topic], source_lang="it-IT")
    block = svc.create_block_in_topic(topic, "paragraph", actor="QA", text="Testo")
    svc.archive_topic(topic, actor="QA")

    exc = raised(svc.save_topic, topic, title="X", expected_version=2, actor="QA")
    check("svc1: saving an archived topic raises the structured code",
          isinstance(exc, de.DomainError)
          and exc.code == de.ERR_AUTHORING_CONTENT_ARCHIVED, repr(exc))
    check("svc2: and is still an ArchivedObjectError", isinstance(exc, ArchivedObjectError))
    check("svc3: the id of the archived content is carried",
          exc.params.get("object_id") == topic, exc.params)

    shown = app.ui_error_text(exc)
    check("svc4: the UI shows the normalized Italian, naming the content",
          shown == f"Il contenuto {topic} è archiviato: ripristinalo prima di modificarlo.",
          shown)
    check("svc5: and never the raw code", "ERR_" not in shown, shown)
    with i18n.use_language("en"):
        shown_en = app.ui_error_text(exc)
    check("svc6: English renders the same condition",
          shown_en == f"Content {topic} is archived: restore it before editing it.", shown_en)

    exc = raised(svc.create_block_in_topic, topic, "paragraph", actor="QA", text="X")
    check("svc7: adding to an archived topic raises the same code",
          isinstance(exc, de.DomainError)
          and exc.code == de.ERR_AUTHORING_CONTENT_ARCHIVED, repr(exc))

    # ---- not migrated: unchanged ---------------------------------------
    svc.restore_topic(topic, actor="QA")
    exc = raised(svc.restore_topic, topic, actor="QA")
    check("keep1: restoring a non-archived topic is still a plain ValueError",
          isinstance(exc, ValueError) and not isinstance(exc, de.DomainError), repr(exc))
    check("keep2: and still renders its own Italian text",
          "non è archiviato" in app.ui_error_text(exc), app.ui_error_text(exc))
    exc = raised(svc.save_topic, "T-NOPE", title="X", expected_version=1, actor="QA")
    check("keep3: an unknown topic still raises its own builtin",
          exc is not None and not isinstance(exc, de.DomainError), repr(exc))
    repo.close()

# =====================================================================
# 5. Boundary gates
# =====================================================================
check("bnd1: one resolver, one helper",
      APP_SRC.count("def ui_error_text(") == 1
      and APP_SRC.count("def domain_error_message(") == 1)
check("bnd2: no handler renders a bare exception",
      'save_msg=f"⛔ {e}"' not in APP_SRC and "raise gr.Error(str(e))" not in APP_SRC)
check("bnd3: neither archived code can reach the UI as a raw code",
      not any("ERR_" in app.ui_error_text(
          ArchivedContentError(c, object_id="T-1")) for c in de.ARCHIVED_CODES))

i18n.set_language(i18n.DEFAULT_LANGUAGE)
check("end1: the suite leaves the process on the default language",
      i18n.current_language() == "it")

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
