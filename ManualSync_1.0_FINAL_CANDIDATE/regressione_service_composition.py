"""CP5.1a regression: CHARACTERIZATION of how a project's repository and services are opened.

Today the two composition functions live in the presenters:

    authoring_view.open_service / ccms_db_path    -> ERR_AUTHORING_REPOSITORY_MISSING
    compliance_view.open_service / ccms_db_path   -> ERR_PROJECT_REPOSITORY_MISSING

and three services reach them through the presenter (impact_center,
impact_intelligence, translation_intelligence; publishing_output imports
authoring_view without using it). CP5.1b will move the composition to a neutral
module and leave the presenter names as aliases. This suite pins, BEFORE the move
(HEAD e29b255), what every caller observes -- so the move has to leave it identical:

* E1/E2  open_service on a project without database: exception type, code and params,
         for str, Path and relative inputs -- authoring and compliance must not swap;
* E3     open_service on the current, legacy and both-names layouts: the repository and
         service types, the file opened, the service bound to the returned repository;
* E4     a failure creates nothing (no folder, no database, nothing in the cwd);
* E5-E7  the errors the services raise when the database is missing, and the
         validation errors they raise BEFORE opening it (the order is part of the contract:
         assess_object_impact opens the authoring side first, so the compliance error is
         reachable only through _collect_compliance);
* E8     the reports of the three services on a synthetic project with a block, a
         publication and compliance evidence (generated ids and timestamps normalized);
* E9     importing publishing_output still loads authoring_view (through publishing_preview);
* E10    no module replaces open_service or ccms_db_path at run time.

Golden digests were pinned after three identical runs; M1 proves they keep the
authoring/compliance distinction. Nothing is written outside a temporary folder.

CP5.1b moved the composition to project_services; the E digests above did not change.
Section S pins the new structure:

* S1     the presenter names are aliases of the SAME project_services objects;
* S2/S3  the four services import no presenter, and importing them loads none (nor UI);
* S4     project_services: repository, services, ProjectLayout, domain_errors -- no SQL,
         no effect at import, exactly its five functions;
* S5     the aliases sit at the position of the former definitions;
* S6     S1 and S2 can fail (a swapped alias, a re-introduced presenter import).

Re-pin only after a reviewed, intentional behaviour change:
    ..\\.venv\\Scripts\\python.exe regressione_service_composition.py --print-golden

Usage:  ..\\.venv\\Scripts\\python.exe regressione_service_composition.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()

import authoring_view as av  # noqa: E402
import compliance_view as cv  # noqa: E402
import domain_errors as de  # noqa: E402
import impact_center as ic  # noqa: E402
import impact_intelligence as ii  # noqa: E402
import translation_intelligence as ti  # noqa: E402
from authoring_service import AuthoringService  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from compliance_intelligence import ComplianceService  # noqa: E402
from project_layout import CCMS_DB_NAME, LEGACY_CCMS_DB_NAME  # noqa: E402
from project_lifecycle import create_project, update_project  # noqa: E402

PRINT_GOLDEN = "--print-golden" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="erylos_cp51a_services_"))
ACTOR = "Service Composition Suite"
_results: list[tuple[str, bool, str]] = []
_observed: dict[str, str] = {}


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:700]))
    if not PRINT_GOLDEN:
        print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:700]}]"))


def plain(o):
    if hasattr(o, "__dataclass_fields__"):
        return [[f, plain(getattr(o, f))] for f in o.__dataclass_fields__]
    if isinstance(o, dict):
        return [[plain(k), plain(v)] for k, v in o.items()]
    if isinstance(o, (list, tuple)):
        return [plain(x) for x in o]
    if isinstance(o, (set, frozenset)):
        return sorted(plain(x) for x in o)
    if isinstance(o, Path):
        return str(o)
    return o


# Generated identifiers (BLK-26E703BB75) and UTC timestamps are the only run-dependent values.
_GEN_ID = re.compile(r"\b[A-Z]{2,4}-[0-9A-F]{10}\b")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?")


def normalized(o) -> str:
    text = json.dumps(plain(o), ensure_ascii=False, default=repr)
    for root in (str(TMP), str(test_isolation.TEST_HOME)):
        for form in (root.replace("\\", "\\\\"), root, root.replace("\\", "/")):
            text = text.replace(json.dumps(form)[1:-1], "<TMP>").replace(form, "<TMP>")
    ids: dict[str, str] = {}
    text = _GEN_ID.sub(lambda m: ids.setdefault(m.group(0), f"<ID{len(ids) + 1}>"), text)
    return _TS.sub("<TS>", text)


def digest(o) -> str:
    return hashlib.sha256(normalized(o).encode("utf-8")).hexdigest()


def snap(label, o):
    _observed[label] = digest(o)


def outcome(fn, *a, **kw):
    """('ok', value) or ('raise', type, code, params) -- what a caller observes."""
    try:
        return ("ok", fn(*a, **kw))
    except Exception as exc:  # noqa: BLE001 -- the observed behaviour IS the exception
        return ("raise", type(exc).__name__, getattr(exc, "code", str(exc)),
                sorted((k, str(v)) for k, v in getattr(exc, "params", {}).items()))


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def opened(fn, project_dir):
    """What open_service hands back, closed again: types, file, binding."""
    res = outcome(fn, project_dir)
    if res[0] != "ok":
        return res
    repo, svc = res[1]
    try:
        return ("ok", type(repo).__name__, type(svc).__name__, repo.path.name,
                repo.path.parent == Path(project_dir), svc.repo is repo)
    finally:
        repo.close()


OPENERS = {"authoring_view.open_service": av.open_service, "compliance_view.open_service": cv.open_service}

# ------------------------------------------------------------ fixtures: the on-disk states
P_NONE = TMP / "projects" / "none"
P_NONE.mkdir(parents=True)
(P_NONE / "ManualSync_Project.json").write_text("{}", encoding="utf-8")


def real_project(name: str) -> Path:
    return create_project(TMP / "real", project_name=name, source_lang="it-IT")


P_CURRENT = real_project("Current")
P_LEGACY = real_project("Legacy")
(P_LEGACY / CCMS_DB_NAME).rename(P_LEGACY / LEGACY_CCMS_DB_NAME)
P_BOTH = real_project("Both")
shutil.copyfile(P_BOTH / CCMS_DB_NAME, P_BOTH / LEGACY_CCMS_DB_NAME)
FOUND = {"current": P_CURRENT, "legacy": P_LEGACY, "both": P_BOTH}
REL_NONE = "erylos_cp51a_rel/none"          # relative to the cwd; must never be created

# ============================================================ E1/E2: database missing
before_none = tree(P_NONE)
missing = {}
for label, fn in OPENERS.items():
    for form, arg in (("str", str(P_NONE)), ("Path", P_NONE), ("relative", REL_NONE)):
        missing[f"{label}[{form}]"] = outcome(fn, arg)
check("E1: authoring_view.open_service without database -> DomainFileMissingError ERR_AUTHORING_REPOSITORY_MISSING",
      all(missing[f"authoring_view.open_service[{f}]"][1:3] == ("DomainFileMissingError", de.ERR_AUTHORING_REPOSITORY_MISSING)
          for f in ("str", "Path", "relative")), missing)
check("E2: compliance_view.open_service without database -> DomainFileMissingError ERR_PROJECT_REPOSITORY_MISSING",
      all(missing[f"compliance_view.open_service[{f}]"][1:3] == ("DomainFileMissingError", de.ERR_PROJECT_REPOSITORY_MISSING)
          for f in ("str", "Path", "relative")), missing)
check("E1b: the only param is project_dir = str(the root as given): absolute stays absolute, relative stays relative",
      all(missing[f"{o}[str]"][3] == [("project_dir", str(P_NONE))] == missing[f"{o}[Path]"][3]
          and missing[f"{o}[relative]"][3] == [("project_dir", str(Path(REL_NONE)))] for o in OPENERS), missing)
caught = []
for label, fn in OPENERS.items():
    try:
        fn(P_NONE)
    except FileNotFoundError as exc:      # the builtin a caller may still be catching
        caught.append((label, type(exc).__name__))
check("E1c: the error is a FileNotFoundError too (callers catching the builtin still catch it)",
      caught == [(k, "DomainFileMissingError") for k in OPENERS], caught)
snap("open_service_missing", missing)

# ============================================================ E4: a failure creates nothing
check("E4: a failed open creates nothing in the project folder", tree(P_NONE) == before_none, tree(P_NONE))
check("E4b: a failed open with a relative path creates nothing in the cwd",
      not Path(REL_NONE).exists() and not Path(REL_NONE).parent.exists(), Path.cwd())

# ============================================================ E3: database present
found = {f"{label}[{state}|{form}]": opened(fn, arg)
         for label, fn in OPENERS.items() for state, p in FOUND.items()
         for form, arg in (("str", str(p)), ("Path", p))}
check("E3: authoring opener -> (IndustrialRepository, AuthoringService), the service bound to that repository",
      all(v[0] == "ok" and v[1:3] == (IndustrialRepository.__name__, AuthoringService.__name__) and v[5]
          for k, v in found.items() if k.startswith("authoring")), found)
check("E3b: compliance opener -> (IndustrialRepository, ComplianceService), the service bound to that repository",
      all(v[0] == "ok" and v[1:3] == (IndustrialRepository.__name__, ComplianceService.__name__) and v[5]
          for k, v in found.items() if k.startswith("compliance")), found)
check("E3c: the file opened is the current name, the legacy name when alone, the current name when both exist",
      all(v[3] == {"current": CCMS_DB_NAME, "legacy": LEGACY_CCMS_DB_NAME, "both": CCMS_DB_NAME}[k.split("[")[1].split("|")[0]]
          and v[4] for k, v in found.items()), found)
snap("open_service_found", found)

# ============================================================ E5-E7: services, database missing
svc_err = {
    "ii.assess_object_impact(block)": outcome(ii.assess_object_impact, P_NONE, "block", "BLK-X"),
    "ii.assess_object_impact(bad kind)": outcome(ii.assess_object_impact, P_NONE, "bogus", "BLK-X"),
    "ii.assess_object_impact(empty id)": outcome(ii.assess_object_impact, P_NONE, "block", ""),
    "ii._collect_compliance": outcome(ii._collect_compliance, str(P_NONE), "block", "BLK-X", 1),
    "ti.assess_translation_intelligence": outcome(ti.assess_translation_intelligence, P_NONE, "BLK-X"),
    "ti.assess_translation_intelligence(block)": outcome(ti.assess_translation_intelligence, P_NONE, "BLK-X",
                                                         object_type="block"),
    "ti.assess_translation_intelligence(bad kind)": outcome(ti.assess_translation_intelligence, P_NONE, "BLK-X",
                                                            object_type="bogus"),
    "ti.assess_translation_intelligence(empty id)": outcome(ti.assess_translation_intelligence, P_NONE, ""),
    "ic.assess_impact_center": outcome(ic.assess_impact_center, P_NONE, "BLK-X"),
    "ic.assess_impact_center(block)": outcome(ic.assess_impact_center, P_NONE, "BLK-X", object_type="block"),
    "ii.assess_object_impact(unknown object)": outcome(ii.assess_object_impact, P_CURRENT, "block", "BLK-NOPE"),
    "ti.assess_translation_intelligence(unknown object)": outcome(ti.assess_translation_intelligence, P_CURRENT, "BLK-NOPE"),
    "ic.assess_impact_center(unknown object)": outcome(ic.assess_impact_center, P_CURRENT, "BLK-NOPE"),
}
AUTH_MISSING = ("DomainFileMissingError", de.ERR_AUTHORING_REPOSITORY_MISSING)
check("E5: assess_object_impact without database raises the AUTHORING error (authoring side opened first)",
      svc_err["ii.assess_object_impact(block)"][1:3] == AUTH_MISSING, svc_err["ii.assess_object_impact(block)"])
check("E5b: assess_object_impact validates kind and id BEFORE opening the database",
      svc_err["ii.assess_object_impact(bad kind)"][1:3] == ("DomainValidationError", de.ERR_IMPACT_KIND_UNSUPPORTED)
      and svc_err["ii.assess_object_impact(empty id)"][1:3] == ("DomainNotFoundError", de.ERR_CONTENT_OBJECT_NOT_FOUND),
      [svc_err["ii.assess_object_impact(bad kind)"], svc_err["ii.assess_object_impact(empty id)"]])
check("E6: _collect_compliance (the compliance_view path) raises the COMPLIANCE error",
      svc_err["ii._collect_compliance"][1:3] == ("DomainFileMissingError", de.ERR_PROJECT_REPOSITORY_MISSING),
      svc_err["ii._collect_compliance"])
check("E7: translation intelligence and the Impact Center without database raise the AUTHORING error",
      all(svc_err[k][1:3] == AUTH_MISSING for k in ("ti.assess_translation_intelligence",
                                                     "ti.assess_translation_intelligence(block)",
                                                     "ic.assess_impact_center", "ic.assess_impact_center(block)")),
      {k: v for k, v in svc_err.items() if k.startswith(("ti.", "ic."))})
check("E7b: translation intelligence validates id and kind BEFORE opening the database",
      svc_err["ti.assess_translation_intelligence(empty id)"][1:3] == ("DomainNotFoundError", de.ERR_CONTENT_OBJECT_NOT_FOUND)
      and svc_err["ti.assess_translation_intelligence(bad kind)"][1:3]
      == ("DomainValidationError", de.ERR_TRANSLATION_KIND_UNSUPPORTED),
      [svc_err["ti.assess_translation_intelligence(empty id)"], svc_err["ti.assess_translation_intelligence(bad kind)"]])
check("E7c: with a database, an unknown object is DomainNotFoundError ERR_CONTENT_OBJECT_NOT_FOUND in all three",
      all(svc_err[k][1:3] == ("DomainNotFoundError", de.ERR_CONTENT_OBJECT_NOT_FOUND)
          for k in svc_err if k.endswith("(unknown object)")), svc_err)
check("E4c: the services' failures created nothing in the project without database", tree(P_NONE) == before_none,
      tree(P_NONE))
snap("service_errors", svc_err)

# ============================================================ E8: service reports on a synthetic project
project = create_project(TMP / "e8", project_name="Composition", source_lang="it-IT")
update_project(project, target_langs=["de-DE", "fr-FR"])
repo = IndustrialRepository(project / CCMS_DB_NAME)
try:
    repo.create_topic("T-A", "Uso previsto", "", source_lang="it-IT", status="Draft")
    repo.create_publication("PUB-MAIN", "Manuale Pompa PC-200", ["T-A"], source_lang="it-IT")
finally:
    repo.close()
repo, auth = av.open_service(str(project))
try:
    block_id = auth.create_block_in_topic("T-A", "paragraph", actor=ACTOR,
                                          text="La pressione massima ammessa è 12 bar.")["block_id"]
finally:
    repo.close()
crepo, csvc = cv.open_service(str(project))
try:
    csvc.create_instrument("INST-T", "TEST/1", actor=ACTOR, kind="directive", title="Direttiva di prova")
    csvc.create_requirement("REQ-1", "Avvertenze sull'uso scorretto", actor=ACTOR, instrument_id="INST-T",
                            article_ref="Art. 1.7.4", description="Descrizione")
    csvc.create_scope("SCOPE-1", publication_id="PUB-MAIN", title="Ambito", jurisdiction="EU", actor=ACTOR)
    csvc.add_scope_reference("SCOPE-1", ref_kind="instrument", ref_id="INST-T", actor=ACTOR)
    csvc.set_applicability("SCOPE-1", "REQ-1", "APPLIES", actor=ACTOR, justification="t")
    csvc.add_evidence(f"EV-{block_id}-v1", "block", actor=ACTOR, object_id=block_id, object_version=1,
                      description="Evidenza")
    csvc.link_evidence_requirement(f"EV-{block_id}-v1", "REQ-1", actor=ACTOR)
    csvc.set_assessment("SCOPE-1", "REQ-1", "COVERED", actor=ACTOR, comment="ok")
finally:
    crepo.close()

reports = {
    "ii.assess_object_impact": ii.assess_object_impact(project, "block", block_id),
    "ti.assess_translation_intelligence": ti.assess_translation_intelligence(project, block_id),
    "ic.assess_impact_center": ic.assess_impact_center(project, block_id),
}
impact = reports["ii.assess_object_impact"]
check("E8: the impact report reaches both openers -- structure (authoring) and compliance records are present",
      {r.domain for r in impact.records} >= {"structure", "compliance"}, sorted({r.domain for r in impact.records}))
check("E8b: the three reports describe the same object at version 1",
      all(r.object_id == block_id and r.object_version == 1 for r in reports.values()), "")
check("E8c: normalization removes every run-dependent value (no generated id or timestamp left)",
      block_id not in normalized(reports) and not _TS.search(normalized(reports).replace("<TS>", "")), "")
snap("service_reports", reports)

# ============================================================ E9: the import graph of publishing_output
_CHILD = ("import json, sys; sys.path.insert(0, sys.argv[1]); import test_isolation; import network_guard; "
          "network_guard.install(); import importlib; importlib.import_module(sys.argv[2]); "
          "print(json.dumps(sorted(m for m in ('authoring_view', 'compliance_view', 'gradio', 'app', 'webview') "
          "if m in sys.modules)))")


def loaded_by(module: str) -> list[str]:
    out = subprocess.run([sys.executable, "-B", "-c", _CHILD, str(BASE), module], capture_output=True, text=True,
                         timeout=180, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), cwd=str(BASE))
    if out.returncode != 0:
        return ["<error>", out.stderr[-400:]]
    return json.loads(out.stdout.strip().splitlines()[-1])


po_loaded = loaded_by("publishing_output")
check("E9: importing publishing_output loads authoring_view (through publishing_preview) and no UI",
      po_loaded == ["authoring_view"], po_loaded)
# What CP5.1a observed at e29b255, before the move: the three services loaded the presenters.
CP51A_PRESENTERS_AT_IMPORT = {"impact_center": ["authoring_view", "compliance_view"],
                              "impact_intelligence": ["authoring_view", "compliance_view"],
                              "translation_intelligence": ["authoring_view"]}
NOW_AT_IMPORT = {m: loaded_by(m) for m in CP51A_PRESENTERS_AT_IMPORT}
print(f"  INFO  presenters/UI loaded at import: CP5.1a {CP51A_PRESENTERS_AT_IMPORT} -> now {NOW_AT_IMPORT}")

# ============================================================ E10: nobody replaces the openers at run time
TARGETS = {"open_service", "ccms_db_path"}
replacements = []
for p in sorted(BASE.glob("*.py")):
    if p.name == Path(__file__).name:
        continue
    t = ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
    for n in ast.walk(t):
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else []
        replacements += [f"{p.name}:{n.lineno}" for x in targets if isinstance(x, ast.Attribute) and x.attr in TARGETS]
        if (isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
                and (n.func.id if isinstance(n.func, ast.Name) else n.func.attr) == "setattr"
                and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value in TARGETS):
            replacements.append(f"{p.name}:{n.lineno}")
check("E10: no product module or suite assigns or setattr()s open_service / ccms_db_path", not replacements, replacements)

# ============================================================ S: the CP5.1b structure (composition in project_services)
import project_services as ps  # noqa: E402


def aliases_ok() -> bool:
    return (av.open_service is ps.open_authoring_service and cv.open_service is ps.open_compliance_service
            and av.ccms_db_path is ps.authoring_db_path and cv.ccms_db_path is ps.compliance_db_path)


check("S1: the presenter names are aliases of the SAME project_services objects (authoring -> authoring, "
      "compliance -> compliance)", aliases_ok(),
      [getattr(f, "__qualname__", f) for f in (av.open_service, cv.open_service, av.ccms_db_path, cv.ccms_db_path)])


def presenter_imports(src: str) -> list[str]:
    """Every *_view module imported anywhere in the source (top level or inside functions)."""
    found = []
    for n in ast.walk(ast.parse(src)):
        names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else []
        found += [x for x in names if x.split(".")[0].endswith("_view")]
    return sorted(found)


SERVICES = ("impact_center", "impact_intelligence", "translation_intelligence", "publishing_output")
SRC = {m: (BASE / f"{m}.py").read_text(encoding="utf-8") for m in SERVICES}
static_edges = {m: presenter_imports(SRC[m]) for m in SERVICES}
check("S2: impact_center, impact_intelligence, translation_intelligence and publishing_output import no presenter",
      not any(static_edges.values()), static_edges)
check("S3: importing impact_center, impact_intelligence or translation_intelligence loads no presenter and no UI",
      all(v == [] for v in NOW_AT_IMPORT.values()), NOW_AT_IMPORT)

PS_SRC = (BASE / "project_services.py").read_text(encoding="utf-8")
PS_TREE = ast.parse(PS_SRC)
ps_imports = sorted({a.name.split(".")[0] for n in ast.walk(PS_TREE) if isinstance(n, ast.Import) for a in n.names}
                    | {(n.module or "").split(".")[0] for n in ast.walk(PS_TREE) if isinstance(n, ast.ImportFrom)})
PS_ALLOWED = {"__future__", "pathlib", "domain_errors", "ccms_industrial", "project_layout", "authoring_service",
              "compliance_intelligence"}
ps_calls = sorted({n.func.attr for n in ast.walk(PS_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
                  & {"execute", "executemany", "executescript", "read_sql_query", "read_sql", "connect"})
ps_top_ok = all(isinstance(st, (ast.Import, ast.ImportFrom, ast.FunctionDef))
                or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)) for st in PS_TREE.body)
ps_defs = sorted(st.name for st in PS_TREE.body if isinstance(st, ast.FunctionDef))
check("S4: project_services imports only the repository, the two services, ProjectLayout and domain_errors; "
      "no SQL, no effect at import; exactly the four openers/paths plus _ccms_db",
      set(ps_imports) <= PS_ALLOWED and not ps_calls and ps_top_ok
      and ps_defs == ["_ccms_db", "authoring_db_path", "compliance_db_path", "open_authoring_service",
                      "open_compliance_service"], [ps_imports, ps_calls, ps_top_ok, ps_defs])


def alias_position(module: str, before: str, after: str) -> bool:
    body = ast.parse((BASE / f"{module}.py").read_text(encoding="utf-8")).body
    names = [("def", st.name) if isinstance(st, ast.FunctionDef) else
             ("alias", st.module) if isinstance(st, ast.ImportFrom) and st.module == "project_services" else None
             for st in body]
    names = [x for x in names if x]
    if ("alias", "project_services") not in names or ("def", before) not in names or ("def", after) not in names:
        return False
    i = names.index(("alias", "project_services"))
    return (names[i - 1] == ("def", before) and names[i + 1] == ("def", after)
            and ("def", "ccms_db_path") not in names and ("def", "open_service") not in names)


check("S5: the aliases sit where the definitions were (authoring_view: after block_type_label; compliance_view: "
      "after evidence_kind_labels) and no old definition is left",
      alias_position("authoring_view", "block_type_label", "publication_options")
      and alias_position("compliance_view", "evidence_kind_labels", "scope_options"), "")

# S6: S1 and S2 can fail -- a swapped alias and a re-introduced presenter import are both caught.
_saved = av.open_service
av.open_service = ps.open_compliance_service
try:
    swapped_caught = not aliases_ok()
finally:
    av.open_service = _saved
check("S6: a swapped alias (authoring name -> compliance opener) is caught by S1, then restored",
      swapped_caught and aliases_ok(), "")
check("S6b: an `import authoring_view` put back into impact_center is caught by S2",
      presenter_imports(SRC["impact_center"] + "\nimport authoring_view as av\n") == ["authoring_view"], "")

# ============================================================ M: the digests keep the authoring/compliance distinction
swap = {de.ERR_AUTHORING_REPOSITORY_MISSING: de.ERR_PROJECT_REPOSITORY_MISSING,
        de.ERR_PROJECT_REPOSITORY_MISSING: de.ERR_AUTHORING_REPOSITORY_MISSING}
swapped = {k: (v[0], v[1], swap.get(v[2], v[2]), v[3]) for k, v in missing.items()}
check("M1: exchanging the two repository-missing codes changes the pinned digest", digest(swapped) != _observed["open_service_missing"],
      digest(swapped)[:12])

check("N1: no network access attempted", not GUARD.snapshot(), GUARD.snapshot())

# Pinned at HEAD e29b255 (CP5.1a) after three identical runs.
GOLDEN = {
    'open_service_found': 'f4ab805fc3ccda8738504f362a822be0ca30fa50a53e7b95e2ec5c43c17e0a4b',
    'open_service_missing': 'c8cb6a810d4156728636d737583c8ff119ea2bf1a753d60261f7a63d564a59fc',
    'service_errors': '8d4320567248241ef92a1db42303e24c8297a49e636cc60b1084c8d4c173cad5',
    'service_reports': 'cf7e29fb6b62f7e360c04f29240f89ce571b8373b18dee6402ef3c56a724259f',
}

if PRINT_GOLDEN:
    print("GOLDEN = {")
    for k in sorted(_observed):
        print(f"    {k!r}: {_observed[k]!r},")
    print("}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

for label in sorted(_observed):
    check(f"SNAP {label}: identical to the CP5.1a baseline", GOLDEN.get(label) == _observed[label],
          f"expected {GOLDEN.get(label)} got {_observed[label]}")
check("SNAP: every pinned output was observed", set(GOLDEN) == set(_observed), sorted(set(GOLDEN) ^ set(_observed)))

shutil.rmtree(TMP, ignore_errors=True)
check("T1: the temporary fixtures were removed", not TMP.exists(), TMP)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_SERVICE_COMPOSITION.json").write_text(json.dumps({
    "suite": "service composition (CP5.1a + CP5.1b)", "tests": total, "pass": passed, "fail": total - passed,
    "digests": _observed, "presenters_at_import": {"cp51a": CP51A_PRESENTERS_AT_IMPORT, "now": NOW_AT_IMPORT},
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
