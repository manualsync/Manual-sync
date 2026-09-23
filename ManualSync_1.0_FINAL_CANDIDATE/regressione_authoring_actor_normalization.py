"""Authoring Actor Error Normalization V1 — "operatore", not "actor".

Thirty-two raises said some variant of "actor è obbligatorio ...", in 22
different sentences, each repeating the button the user had just pressed.
Two things were wrong with that. The clause carried nothing actionable --
the reason and the remedy are identical every time -- and "actor" is the
internal parameter name, leaking onto the screen in a product whose own
context bar asks for an "Operatore".

So this gate does what the previous one deliberately refused to do: it
changes the wording. That is the point, not a side effect, and the suite
is written to assert the NEW text and to prove the old term is gone.

Three codes, split only where the meaning genuinely differs:

    ERR_AUTHORING_ACTOR_REQUIRED            22  editing content
    ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS    9  variant configuration
    ERR_AUTHORING_ACTOR_REQUIRED_APPROVAL    1  granting an approval

The approval is separate on purpose: it is a governance act this product
never performs automatically, and telling someone "ogni modifica viene
registrata" while they approve a publication understates what they are
doing.

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
from ccms_industrial import IndustrialRepository  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

results = []
SRC = (HERE / "authoring_service.py").read_text(encoding="utf-8")


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
# 1. The old wording is gone, everywhere
# =====================================================================
check("gone1: no 'actor è obbligatorio' sentence survives in the service",
      "actor è obbligatorio" not in SRC)
check("gone2: no Authoring module raises that text any more",
      not any("actor è obbligatorio" in (HERE / m).read_text(encoding="utf-8")
              for m in ("authoring_service.py", "authoring_ccms.py", "authoring_view.py")))

CATALOG = {k: i18n.translate(k, lg) for lg in i18n.SUPPORTED_LANGUAGES
           for k in i18n.catalog_keys(lg)}
actor_it = [i18n.translate(f"error.{c}", "it") for c in de.ACTOR_CODES]
check("gone3: the Italian messages say 'operatore', never 'actor'",
      all("operatore" in t_ and "actor" not in t_.lower() for t_ in actor_it), actor_it)
check("gone4: and they no longer repeat the button the user just pressed",
      not any(w in " ".join(actor_it)
              for w in ("per salvare un topic", "per un revert", "per un reorder",
                        "per archiviare", "per ripristinare", "draft recovery")),
      actor_it)

# =====================================================================
# 2. The three codes, and what each is for
# =====================================================================
check("code1: exactly three actor codes", len(de.ACTOR_CODES) == 3, de.ACTOR_CODES)
check("code2: they are part of the Authoring batch",
      set(de.ACTOR_CODES) <= set(de.AUTHORING_CODES))
check("code3: and do not overlap the repository/object batch",
      not (set(de.ACTOR_CODES) & set(de.AUTHORING_OBJECT_CODES)))
for code in de.ACTOR_CODES:
    for lg in i18n.SUPPORTED_LANGUAGES:
        check(f"code4[{code}/{lg}]: has a catalog entry",
              i18n.has_key(f"error.{code}", lg))
check("code5: the three sentences are genuinely different",
      len(set(actor_it)) == 3, actor_it)
check("code6: English says 'operator' and is a real translation",
      all("operator" in i18n.translate(f"error.{c}", "en").lower()
          and i18n.translate(f"error.{c}", "en") != i18n.translate(f"error.{c}", "it")
          for c in de.ACTOR_CODES))
check("code7: the approval message names the approval, not a generic change",
      "approvazione" in i18n.translate(
          f"error.{de.ERR_AUTHORING_ACTOR_REQUIRED_APPROVAL}", "it"))
check("code8: the settings message names the configuration, not the content",
      "configurazione" in i18n.translate(
          f"error.{de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS}", "it"))

# Every raise site landed in the right group.
tree = ast.parse(SRC)
fns = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
       if isinstance(n, ast.FunctionDef)]
sites = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
        continue
    for a in node.exc.args:
        if isinstance(a, ast.Attribute) and a.attr in de.ACTOR_CODES:
            owner = min(((s, e, n_) for s, e, n_ in fns if s <= node.lineno <= e),
                        key=lambda f: f[1] - f[0], default=(0, 0, "?"))[2]
            sites.setdefault(a.attr, []).append(owner)

check("site1: all 32 actor raises were migrated",
      sum(len(v) for v in sites.values()) == 32,
      {k: len(v) for k, v in sites.items()})
check("site2: 22 content, 9 settings, 1 approval",
      len(sites[de.ERR_AUTHORING_ACTOR_REQUIRED]) == 22
      and len(sites[de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS]) == 9
      and len(sites[de.ERR_AUTHORING_ACTOR_REQUIRED_APPROVAL]) == 1,
      {k: len(v) for k, v in sites.items()})
check("site3: only the variant-configuration methods use the settings code",
      all(("condition" in f or "variant_profile" in f)
          for f in sites[de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS]),
      sites[de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS])
check("site4: only the bulk approval uses the approval code",
      sites[de.ERR_AUTHORING_ACTOR_REQUIRED_APPROVAL] == ["bulk_approve_publication"],
      sites[de.ERR_AUTHORING_ACTOR_REQUIRED_APPROVAL])
check("site5: no content-editing method was mislabelled as settings",
      not any(f in sites[de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS]
              for f in ("save_topic", "save_block", "save_table", "save_figure")))

# =====================================================================
# 3. Through the real services
# =====================================================================
with tempfile.TemporaryDirectory() as tmp:
    proj = create_project(Path(tmp), project_name="ZZ_Actor_Probe", client="X")
    repo = IndustrialRepository(av.ccms_db_path(proj))
    svc = asvc.AuthoringService(repo)
    topic = repo.create_topic("T-A", "Uso", "", source_lang="it-IT", status="Draft")
    repo.create_publication("PUB-MAIN", "M", [topic], source_lang="it-IT")

    CASES = [
        ("content / create topic", de.ERR_AUTHORING_ACTOR_REQUIRED,
         lambda: svc.create_topic_in_publication("PUB-MAIN", "Titolo", actor="")),
        ("content / save topic", de.ERR_AUTHORING_ACTOR_REQUIRED,
         lambda: svc.save_topic(topic, title="X", expected_version=1, actor="")),
        ("content / draft recovery", de.ERR_AUTHORING_ACTOR_REQUIRED,
         lambda: svc.save_draft("topic", topic, base_version=1, payload={}, actor="")),
        ("settings / create dimension", de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS,
         lambda: svc.create_condition_dimension("dim", "Nome", actor="")),
        ("settings / variant profile", de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS,
         lambda: svc.create_variant_profile("prof", "Nome", {}, actor="")),
        ("approval / bulk approve", de.ERR_AUTHORING_ACTOR_REQUIRED_APPROVAL,
         lambda: svc.bulk_approve_publication("PUB-MAIN", actor="")),
    ]
    for label, code, fn in CASES:
        exc = raised(fn)
        check(f"svc[{label}]: raises {code}",
              isinstance(exc, de.DomainError) and exc.code == code, repr(exc))
        check(f"svc[{label}]: is still a ValueError for existing catches",
              isinstance(exc, ValueError))
        shown = app.ui_error_text(exc)
        check(f"svc[{label}]: the UI shows the normalized Italian",
              "operatore" in shown and "ERR_" not in shown, shown)

    # English renders the same condition in English.
    exc = raised(lambda: svc.save_topic(topic, title="X", expected_version=1, actor=""))
    with i18n.use_language("en"):
        shown_en = app.ui_error_text(exc)
    check("lang1: the same code renders in English",
          "operator" in shown_en.lower() and "operatore" not in shown_en, shown_en)
    check("lang2: and the code is identical in both languages",
          exc.code == de.ERR_AUTHORING_ACTOR_REQUIRED)

    # Not migrated: still behaves exactly as before.
    exc = raised(lambda: svc.create_topic_in_publication("PUB-NOPE", "Titolo", actor="QA"))
    check("keep1: a missing publication is still a plain KeyError",
          isinstance(exc, KeyError) and not isinstance(exc, de.DomainError), repr(exc))
    exc = raised(lambda: svc.save_topic("T-NOPE", title="X", expected_version=1, actor="QA"))
    check("keep2: an unknown topic still raises its own builtin",
          exc is not None and not isinstance(exc, de.DomainError), repr(exc))
    # The archived-object raises were migrated by a LATER gate, which owns
    # their assertions. What is still un-migrated here -- and must stay
    # that way -- is the inverse guard: restoring something that is not
    # archived. (An earlier version of this check matched "è archiviato"
    # and so passed on the unrelated "non è archiviato." message; it was
    # asserting nothing.)
    exc = raised(lambda: svc.restore_topic(topic, actor="QA"))
    check("keep3: 'non è archiviato' is NOT migrated and stays a plain ValueError",
          isinstance(exc, ValueError) and not isinstance(exc, de.DomainError)
          and "non è archiviato" in str(exc), repr(exc))
    repo.close()

# =====================================================================
# 4. The boundary gates still hold
# =====================================================================
APP_SRC = (HERE / "app.py").read_text(encoding="utf-8")
check("bnd1: one resolver, one helper",
      APP_SRC.count("def ui_error_text(") == 1
      and APP_SRC.count("def domain_error_message(") == 1)
check("bnd2: no handler renders a bare exception",
      'save_msg=f"⛔ {e}"' not in APP_SRC and "raise gr.Error(str(e))" not in APP_SRC)
check("bnd3: an actor code cannot reach the UI as a raw code",
      not any("ERR_" in app.ui_error_text(de.DomainValidationError(c))
              for c in de.ACTOR_CODES))

i18n.set_language(i18n.DEFAULT_LANGUAGE)
check("end1: the suite leaves the process on the default language",
      i18n.current_language() == "it")

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
