"""CP3.1 regression: the architecture boundaries as they are TODAY, protected by a ratchet.

Static analysis only (ast over the source files) -- no product module is
imported, nothing is executed, nothing is written outside the result file.

Why this exists
---------------
CP3.0 mapped the real architecture before any `erylos_core` extraction:
Gradio is confined to four modules, the domain modules never import a UI,
a web framework, Windows APIs or an AI SDK -- but SQL lives in ~25 modules,
PyMuPDF is pulled in by the central repository, the project-path convention
is duplicated, five services import a presenter (`*_view`) and several
modules act at import time. CP3.1 does NOT fix any of that. It freezes it:

* rules that already hold are enforced absolutely (A, B, D, E, F);
* problems that exist are recorded in an explicit BASELINE, and the suite
  fails if they GROW -- a new module with SQL, a new SQL call in a tolerated
  module, a new PDF import, a new presenter import, a new import-time
  effect, a new copy of the project path. Shrinking is reported, never
  blocked, so CP3.2+ can remove items and then tighten the baseline here.

CP3.2 introduced project_layout.py as the single owner of the on-disk layout:
its path knowledge is exempt from PATH, rule LAYOUT keeps it pure (stdlib only,
no SQL, no import effect), PATH2 proves the spread shrank against the CP3.1
picture (CP31_PROJECT_PATHS), and the census now also counts the legacy
ManualSync_CCMS_v18.sqlite name.

CP3.3 put PyMuPDF behind the reading contract pdf_backend (implemented by
pdf_pymupdf) and moved the PDF/DOCX readers out of authoring_ccms: rule PDFB
confines fitz to layout_engine, pdf_pymupdf and publishing_output (the HTML->PDF
writer, which keeps its own boundary for a later checkpoint) and keeps the
contract pure; the PDF baseline was tightened (no core candidate loads the PDF
stack at import any more) and PDF3 proves the drop against the CP3.2 picture.

CP3.4 moved two families of pure algorithms out of layout_engine into
translation_alignment and language_signals: rule ALGO keeps them standard-library
only -- no PyMuPDF, no layout_engine, no SQL, no UI or presenter, no app, no
network -- with no import-time effect.

Every rule is proven able to fail: section M copies the sources into a
temporary folder, injects one violation per rule, runs the very same
checks and expects them to fire. The real sources are hashed before and
after to prove they were never touched.

Update the baseline only deliberately (a reviewed CP decision):
    ..\\.venv\\Scripts\\python.exe regressione_architecture_boundaries.py --print-baseline

Usage:  ..\\.venv\\Scripts\\python.exe regressione_architecture_boundaries.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import hashlib
import json
import os
import pprint
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent

# ============================================================ classification (CP3.0)
UI_DESKTOP = frozenset({"app", "app_engineering", "offline_frontend", "manualsync_desktop"})

# CP3.0 section 11: modules (or modules whose main part) can become erylos_core.
CORE_CANDIDATES = frozenset({
    "domain_errors", "language_codes", "linguistic_safety", "rich_text", "publishing_preflight",
    "product_version", "compliance_starter_packs",
    "authoring_ccms", "ccms_industrial", "authoring_service", "compliance_intelligence",
    "workflow_management", "project_lifecycle", "release_readiness",
    "impact_intelligence", "impact_center", "impact_economics",
    "translation_intelligence", "translation_impact", "publishing_preview",
    "grounded_authoring", "generative_orchestrator", "cat_tm",
    "project_layout",          # CP3.2
    "pdf_backend",             # CP3.3 (the reading contract)
    "translation_alignment",   # CP3.4 (pure algorithms)
    "language_signals",        # CP3.4 (pure algorithms)
    "project_services",        # CP5 (composition root: opens repository + services)
})

# CP3.4: pure analysis algorithms taken out of layout_engine; standard library only.
ALGORITHM_MODULES = frozenset({"translation_alignment", "language_signals"})
ALGORITHM_ALLOWED_IMPORTS = {"__future__", "re", "pathlib"}

# CP3.2: the single sanctioned owner of the on-disk layout (data home, Projects folder,
# content-database names). Its path knowledge is the point; it must stay pure.
LAYOUT_MODULE = "project_layout"
LAYOUT_ALLOWED_IMPORTS = {"__future__", "dataclasses", "pathlib"}
# CP3.3: who may talk to PyMuPDF directly, and what the reading contract may import.
PDF_ENGINE_MODULES = frozenset({"layout_engine", "pdf_pymupdf", "publishing_output"})
PDF_CONTRACT = "pdf_backend"
PDF_CONTRACT_STDLIB = {"__future__", "pathlib", "typing"}
PDF_CONTRACT_LOCAL = {"pdf_pymupdf"}     # only lazily, inside default_reader()
LAYOUT_REQUIRED_NAMES = {"DATA_DIR_NAME", "PROJECTS_DIR_NAME", "CCMS_DB_NAME", "LEGACY_CCMS_DB_NAME",
                         "CCMS_DB_CANDIDATES", "default_data_home", "projects_dir", "ProjectLayout"}

# Build/test infrastructure (CP3.0 category E): may import presenters and fixtures.
INFRASTRUCTURE = frozenset({
    "packaging_payload", "environment_guard", "canonical_demo", "acceptance_fixture", "acceptance_guard",
    "real_data_guard", "test_isolation", "chrome_locator", "network_guard", "verifica_webview2_offline",
})

# CP3.0 section 6: the classes that own a connection or a schema (the persistence layer of today).
SQL_AUTHORIZED = frozenset({
    "authoring_ccms",          # ContentRepository
    "ccms_industrial",         # IndustrialRepository
    "authoring_service",       # AuthoringService (repo.conn, authoring_drafts)
    "compliance_intelligence", # ComplianceService (repo.conn, compliance_* tables)
    "workflow_management",     # WorkflowManager
    "cat_tm",                  # TranslationMemory
    "grounded_authoring",      # GroundingStore
    "generative_orchestrator", # GenerationLedger
})

UI_LIBS = {"gradio"}
DESKTOP_LIBS = {"webview", "pywebview"}
WEB_LIBS = {"fastapi", "starlette", "uvicorn", "flask", "django"}
WINDOWS_LIBS = {"ctypes", "winreg", "msvcrt", "_winapi", "win32api", "win32con", "win32gui", "win32com",
                "pywintypes", "pythoncom", "comtypes", "wmi"}
WINDOWS_ATTRS = {"startfile", "windll", "WinDLL", "oledll", "CREATE_NO_WINDOW", "STARTUPINFO"}
PAYMENT_LIBS = {"stripe"}
AI_SDKS = {"anthropic", "openai", "google.generativeai", "google.genai", "google.ai", "vertexai", "cohere",
           "mistralai", "langchain", "langchain_core", "langchain_openai", "langchain_anthropic",
           "llama_index", "litellm", "groq", "ollama", "transformers", "together", "replicate", "ai21",
           "voyageai", "huggingface_hub"}
AI_LOCK_PACKAGES = {"anthropic", "openai", "google-generativeai", "google-genai", "cohere", "mistralai",
                    "langchain", "langchain-core", "langchain-openai", "langchain-anthropic", "llama-index",
                    "litellm", "groq", "ollama", "transformers", "together", "replicate", "ai21", "voyageai"}
NETWORK_LIBS = {"socket", "ssl", "urllib.request", "urllib3", "http.client", "http.server", "requests",
                "httpx", "aiohttp", "websocket", "websockets", "smtplib", "ftplib", "telnetlib",
                "xmlrpc.client", "paramiko", "grpc"}
PDF_LIBS = {"fitz", "pymupdf", "numpy", "PIL", "docx"}
FORBIDDEN_IN_CORE = UI_LIBS | DESKTOP_LIBS | WEB_LIBS | WINDOWS_LIBS | PAYMENT_LIBS | AI_SDKS | NETWORK_LIBS
PDF_BOUNDARY_FORBIDDEN = UI_LIBS | DESKTOP_LIBS | WEB_LIBS | PAYMENT_LIBS | AI_SDKS | NETWORK_LIBS   # CP3.3

SQL_METHODS = {"execute", "executemany", "executescript", "read_sql_query", "read_sql", "read_sql_table"}
PATH_HELPERS = {"_ccms_db", "ccms_db_path", "ccms_db", "_ccms"}

# Secrets: generic shapes of AI provider keys. The patterns cannot match their own
# source text (a '[' always follows the fixed prefix).
KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{40,}"),
    re.compile(r"\bxai-[A-Za-z0-9]{40,}"),
    re.compile(r"(?:ANTHROPIC|OPENAI|GEMINI|GOOGLE|COHERE|MISTRAL|GROQ|HF|HUGGINGFACE)_API_KEY\s*[=:]\s*['\"][^'\"\s]{8,}['\"]"),
]
TEXT_SUFFIXES = {".py", ".ps1", ".cmd", ".json", ".txt", ".md", ".spec", ".csv", ".toml", ".ini", ".cfg", ".yml", ".yaml"}
SKIP_DIRS = {".buildvenv", ".venv", "dist", "build", "release", "installer_stage", "build_launcher",
             "dist_launcher", "build_reports", "__pycache__", "benchmark", "fonts", "brand"}

# ============================================================ analysis
def is_suite(name: str) -> bool:
    return name.startswith("regressione_")


def load_sources(root: Path) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.glob("*.py")) if not is_suite(p.stem)}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _module_level_statements(body):
    """Statements that run when the module is imported (defs/classes excluded,
    `if __name__ == "__main__":` excluded)."""
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(st, ast.If):
            t = st.test
            if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__":
                continue
            yield st  # the test itself runs
            yield from _module_level_statements(st.body)
            yield from _module_level_statements(st.orelse)
            continue
        if isinstance(st, ast.Try):
            yield from _module_level_statements(st.body)
            for h in st.handlers:
                yield from _module_level_statements(h.body)
            yield from _module_level_statements(st.orelse)
            yield from _module_level_statements(st.finalbody)
            continue
        if isinstance(st, (ast.With, ast.For, ast.While)):
            yield st
            yield from _module_level_statements(st.body)
            continue
        yield st


def _shallow_walk(node):
    """ast.walk that does not descend into nested function/class/lambda bodies."""
    todo = [node]
    while todo:
        n = todo.pop()
        yield n
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)) and child in getattr(n, "body", []):
                continue  # bodies are yielded separately by _module_level_statements
            todo.append(child)


def _root_name(expr) -> str:
    while isinstance(expr, (ast.Attribute, ast.Call, ast.Subscript)):
        expr = expr.func if isinstance(expr, ast.Call) else expr.value
    return expr.id if isinstance(expr, ast.Name) else ""


_CACHE: dict[tuple, dict] = {}


def analyse(sources: dict[str, str]) -> dict:
    local = frozenset(sources)
    facts: dict = {"modules": {}}
    for name, src in sources.items():
        key = (name, hashlib.sha256(src.encode("utf-8")).hexdigest(), local)
        if key not in _CACHE:          # mutants re-analyse only the one file they change
            _CACHE[key] = _analyse_module(name, src, local)
        facts["modules"][name] = _CACHE[key]
    return facts


def _analyse_module(name: str, src: str, local: frozenset) -> dict:
    tree = ast.parse(src, filename=name + ".py")
    doc_ids = _docstring_nodes(tree)
    imports_all, imports_top = set(), set()
    top_stmt_ids = {id(s) for s in _module_level_statements(tree.body)}
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for m in mods:
            imports_all.add(m)
            if id(node) in top_stmt_ids:
                imports_top.add(m)

    def roots(ms):
        out = set()
        for m in ms:
            parts = m.split(".")
            for i in range(1, len(parts) + 1):
                out.add(".".join(parts[:i]))
        return out

    all_names, top_names = roots(imports_all), roots(imports_top)
    win_attrs = sorted({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in WINDOWS_ATTRS})
    sql = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in SQL_METHODS:
            sql[n.func.attr] = sql.get(n.func.attr, 0) + 1
    path = {}
    for n in ast.walk(tree):
        key = None
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in PATH_HELPERS:
            key = f"def {n.name}"
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            if "ManualSync_CCMS.sqlite" in n.value:
                key = "'ManualSync_CCMS.sqlite'"
            elif "ManualSync_CCMS_v18.sqlite" in n.value:      # CP3.2: the legacy name is layout knowledge too
                key = "'ManualSync_CCMS_v18.sqlite'"
            elif "ManualSyncData" in n.value:
                key = "'ManualSyncData'"
        elif (isinstance(n, ast.Name) and n.id == "DATA_HOME") or (isinstance(n, ast.Attribute) and n.attr == "DATA_HOME"):
            key = "DATA_HOME"
        if key:
            path[key] = path.get(key, 0) + 1
    defs = {s.name for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
    imported_modules = {a.asname or a.name.split(".")[0] for s in tree.body if isinstance(s, ast.Import) for a in s.names}
    effects = set()
    for st in _module_level_statements(tree.body):
        if isinstance(st, ast.With):
            for item in st.items:
                if _root_name(item.context_expr) in ("gr", "gradio"):
                    effects.add("ui_build")
        if isinstance(st, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute) and t.value.attr == "environ":
                    effects.add("env_write")
                elif isinstance(t, ast.Attribute) and _root_name(t) in imported_modules | local:
                    effects.add("global_patch")
        for n in _shallow_walk(st):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            attr = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            root = _root_name(f)
            if attr in ("mkdir", "makedirs"):
                effects.add("mkdir")
            elif root == "atexit" and attr == "register":
                effects.add("atexit")
            elif attr in ("putenv",) or (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute)
                                         and f.value.attr == "environ" and attr in ("setdefault", "update", "pop")):
                effects.add("env_write")
            elif attr in ("install", "register_namespace", "install_guard", "reconfigure") or \
                    (root == "sys" and attr in ("insert", "append")):
                effects.add("global_patch")
            elif attr in ("read_text", "read_bytes", "open", "iterdir", "glob", "exists", "is_file", "is_dir", "load"):
                effects.add("io_read")
            elif isinstance(f, ast.Name) and f.id in defs:
                effects.add("local_call")
    return {
        "imports_all": sorted(all_names), "imports_top": sorted(top_names),
        "local_all": sorted(m for m in all_names if m in local and m != name),
        "local_top": sorted(m for m in top_names if m in local and m != name),
        "win_attrs": win_attrs, "sql": sql, "path": path, "effects": sorted(effects),
        "top_names": sorted({t.id for st in tree.body if isinstance(st, ast.Assign) for t in st.targets
                             if isinstance(t, ast.Name)}
                            | {st.name for st in tree.body if isinstance(st, (ast.FunctionDef, ast.ClassDef))}),
    }


def pdf_at_import(facts: dict) -> dict[str, list[str]]:
    """For each module: which PDF libs it loads at import time, directly or via local modules."""
    mods = facts["modules"]
    out = {}
    for name in mods:
        seen, todo, libs = set(), [name], set()
        while todo:
            m = todo.pop()
            if m in seen or m not in mods:
                continue
            seen.add(m)
            libs |= set(mods[m]["imports_top"]) & PDF_LIBS
            todo += mods[m]["local_top"]
        if libs:
            out[name] = sorted(libs)
    return out


def scan_keys(root: Path) -> list[str]:
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune build outputs, venvs and binary asset folders instead of walking them
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("RISULTATO_"))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() not in TEXT_SUFFIXES or fn.startswith("RISULTATO_"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for pat in KEY_PATTERNS:
                for m in pat.finditer(text):
                    hits.append(f"{p.relative_to(root)}: {m.group(0)[:12]}...")
    return hits


def scan_lock_ai(root: Path) -> list[str]:
    hits = []
    for p in sorted(root.glob("requirements*.txt")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            name = re.split(r"[=<>!~;\s\[]", ln.strip(), maxsplit=1)[0].lower().replace("_", "-")
            if name and not name.startswith("#") and name in AI_LOCK_PACKAGES:
                hits.append(f"{p.name}: {name}")
    return hits


def current_baseline(facts: dict) -> dict:
    mods = facts["modules"]
    view_edges = sorted(f"{m}->{v}" for m, d in mods.items() for v in d["local_all"]
                        if v.endswith("_view") and not m.endswith("_view")
                        and m not in UI_DESKTOP and m not in INFRASTRUCTURE)
    return {
        "gradio_importers": sorted(m for m, d in mods.items() if "gradio" in d["imports_all"]),
        "view_import_edges": view_edges,
        "sql_tolerated": {m: sum(d["sql"].values()) for m, d in sorted(mods.items())
                          if d["sql"] and m not in SQL_AUTHORIZED},
        "pdf_direct_imports": {m: sorted(set(d["imports_all"]) & PDF_LIBS) for m, d in sorted(mods.items())
                               if set(d["imports_all"]) & PDF_LIBS},
        "core_pdf_at_import": sorted(m for m in pdf_at_import(facts) if m in CORE_CANDIDATES),
        "project_paths": {m: d["path"] for m, d in sorted(mods.items()) if d["path"] and m != LAYOUT_MODULE},
        "import_effects": {m: d["effects"] for m, d in sorted(mods.items()) if d["effects"]},
    }


def evaluate(facts: dict, root: Path, baseline: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """(violations, shrink notes). A violation is (rule, detail)."""
    mods = facts["modules"]
    cur = current_baseline(facts)
    v, notes = [], []
    # A -- gradio only in UI/Desktop modules
    for m in cur["gradio_importers"]:
        if m not in UI_DESKTOP:
            v.append(("A", f"{m} imports gradio (allowed only in {sorted(UI_DESKTOP)})"))
    # B / F -- core candidates import no UI, desktop, web, Windows, payment, AI SDK, network
    for m in sorted(CORE_CANDIDATES & set(mods)):
        bad = sorted(set(mods[m]["imports_all"]) & FORBIDDEN_IN_CORE)
        for lib in bad:
            rule = "F" if lib in NETWORK_LIBS else "B"
            v.append((rule, f"core candidate {m} imports {lib}"))
        for attr in mods[m]["win_attrs"]:
            v.append(("B", f"core candidate {m} uses Windows-specific API .{attr}"))
    # C -- services never import a presenter (*_view) beyond the CP3.1 baseline
    for e in cur["view_import_edges"]:
        if e not in baseline["view_import_edges"]:
            v.append(("C", f"new service -> presenter import {e}"))
    notes += [f"C: {e} removed (baseline can be tightened)" for e in baseline["view_import_edges"]
              if e not in cur["view_import_edges"]]
    # D -- no concrete AI SDK anywhere in the application code or the locks
    for m, d in sorted(mods.items()):
        for lib in sorted(set(d["imports_all"]) & AI_SDKS):
            v.append(("D", f"{m} imports AI SDK {lib}"))
    v += [("D", f"AI SDK in lock {h}") for h in scan_lock_ai(root)]
    # E -- no hardcoded AI API key
    v += [("E", f"possible API key in {h}") for h in scan_keys(root)]
    # SQL -- no new module with SQL, no growth in tolerated modules
    for m, n in cur["sql_tolerated"].items():
        allowed = baseline["sql_tolerated"].get(m)
        if allowed is None:
            v.append(("SQL", f"{m} now issues SQL ({n} calls) and is not a persistence module"))
        elif n > allowed:
            v.append(("SQL", f"{m}: {n} SQL calls > baseline {allowed}"))
        elif n < allowed:
            notes.append(f"SQL: {m} {allowed} -> {n} (baseline can be tightened)")
    notes += [f"SQL: {m} no longer issues SQL" for m in baseline["sql_tolerated"] if m not in cur["sql_tolerated"]]
    # PDF -- no new direct PDF-stack import, no new core candidate loading PDF at import
    for m, libs in cur["pdf_direct_imports"].items():
        new = sorted(set(libs) - set(baseline["pdf_direct_imports"].get(m, [])))
        if new:
            v.append(("PDF", f"{m} newly imports {new}"))
    for m in cur["core_pdf_at_import"]:
        if m not in baseline["core_pdf_at_import"]:
            v.append(("PDF", f"core candidate {m} now loads the PDF stack at import"))
    notes += [f"PDF: core {m} no longer loads the PDF stack at import" for m in baseline["core_pdf_at_import"]
              if m not in cur["core_pdf_at_import"]]
    # PATH -- project-path / data-home knowledge may not spread
    for m, d in cur["project_paths"].items():
        for k, n in d.items():
            allowed = baseline["project_paths"].get(m, {}).get(k)
            if allowed is None or n > allowed:
                v.append(("PATH", f"{m}: {k} x{n} (baseline {allowed or 0})"))
    for m, d in baseline["project_paths"].items():
        for k, allowed in d.items():
            n = cur["project_paths"].get(m, {}).get(k, 0)
            if n < allowed:
                notes.append(f"PATH: {m} {k} {allowed} -> {n} (baseline can be tightened)")
    # LAYOUT -- project_layout exists, is the owner of the names, and stays pure (CP3.2)
    lay = mods.get(LAYOUT_MODULE)
    if lay is None:
        v.append(("LAYOUT", f"{LAYOUT_MODULE}.py is missing"))
    else:
        roots = {m.split(".")[0] for m in lay["imports_all"]}
        if roots - LAYOUT_ALLOWED_IMPORTS:
            v.append(("LAYOUT", f"{LAYOUT_MODULE} imports {sorted(roots - LAYOUT_ALLOWED_IMPORTS)}"))
        if LAYOUT_REQUIRED_NAMES - set(lay["top_names"]):
            v.append(("LAYOUT", f"{LAYOUT_MODULE} lost {sorted(LAYOUT_REQUIRED_NAMES - set(lay['top_names']))}"))
        if lay["sql"] or lay["effects"]:
            v.append(("LAYOUT", f"{LAYOUT_MODULE} issues SQL or acts at import: {lay['sql']} {lay['effects']}"))
    # PDFB -- PyMuPDF only in the PDF engine modules; the reading contract stays pure (CP3.3)
    for m, d in sorted(mods.items()):
        engine = sorted(set(d["imports_all"]) & {"fitz", "pymupdf"})
        if engine and m not in PDF_ENGINE_MODULES:
            v.append(("PDFB", f"{m} imports PyMuPDF directly ({engine}); only {sorted(PDF_ENGINE_MODULES)} may"))
        if m in CORE_CANDIDATES and "layout_engine" in d["local_top"]:
            v.append(("PDFB", f"core candidate {m} imports layout_engine at import time"))
    for m in (PDF_CONTRACT, "pdf_pymupdf"):
        if m in mods:
            bad = sorted(set(mods[m]["imports_all"]) & PDF_BOUNDARY_FORBIDDEN)
            if bad:
                v.append(("PDFB", f"{m} imports {bad}"))
            if mods[m]["sql"] or mods[m]["effects"]:
                v.append(("PDFB", f"{m} issues SQL or acts at import: {mods[m]['sql']} {mods[m]['effects']}"))
    # ALGO -- the pure algorithm modules import nothing but the standard library they need (CP3.4)
    for m in sorted(ALGORITHM_MODULES):
        d = mods.get(m)
        if d is None:
            v.append(("ALGO", f"{m}.py is missing"))
            continue
        roots = {x.split(".")[0] for x in d["imports_all"]}
        if roots - ALGORITHM_ALLOWED_IMPORTS:
            v.append(("ALGO", f"{m} imports {sorted(roots - ALGORITHM_ALLOWED_IMPORTS)}"))
        if d["local_all"]:
            v.append(("ALGO", f"{m} depends on product modules {d['local_all']}"))
        if d["sql"] or d["effects"] or d["win_attrs"]:
            v.append(("ALGO", f"{m} issues SQL / acts at import / uses Windows APIs: {d['sql']} {d['effects']} {d['win_attrs']}"))
    con = mods.get(PDF_CONTRACT)
    if con is None:
        v.append(("PDFB", f"{PDF_CONTRACT}.py is missing"))
    else:
        top_roots = {x.split(".")[0] for x in con["imports_top"]}
        if top_roots - PDF_CONTRACT_STDLIB:
            v.append(("PDFB", f"{PDF_CONTRACT} imports at module level {sorted(top_roots - PDF_CONTRACT_STDLIB)}"))
        if set(con["local_all"]) - PDF_CONTRACT_LOCAL:
            v.append(("PDFB", f"{PDF_CONTRACT} depends on {sorted(set(con['local_all']) - PDF_CONTRACT_LOCAL)}"))
    # EFFECTS -- no new import-time side effect
    for m, kinds in cur["import_effects"].items():
        new = sorted(set(kinds) - set(baseline["import_effects"].get(m, [])))
        if new:
            v.append(("EFFECTS", f"{m}: new import-time effect(s) {new}"))
    return v, notes


# ============================================================ BASELINE (CP3.1 at ae2d7b2, project_paths tightened in CP3.2)
# Generated with --print-baseline and reviewed against the CP3.0 report. Items may only
# disappear (CP3.2+); anything new fails until a reviewed CP decision updates it here.
BASELINE = {
    # Rule A holds today; recorded for the report.
    "gradio_importers": ["app", "app_engineering", "manualsync_desktop", "offline_frontend"],
    # Services/adapters that import a presenter. CP5 moved open_service / ccms_db_path to
    # project_services and removed 5 of the 6 CP3.1 edges (see CP31_VIEW_IMPORT_EDGES).
    # What is left uses presentation helpers (labels, summaries) and is a separate
    # decision -- tolerated here, never extended.
    "view_import_edges": [
        "publishing_preview->authoring_view",
    ],
    # SQL calls (execute/executemany/executescript/pandas read_sql*) OUTSIDE the
    # persistence modules of SQL_AUTHORIZED. Moving them is CP3.5 work. CP5.2b moved the 11 of
    # app.import_manual_ui into IndustrialRepository (app 18 -> 7, outside 117 -> 106).
    "sql_tolerated": {
        "acceptance_fixture": 1, "acceptance_guard": 1, "app": 7, "asset_intelligence": 1,
        "authoring_view": 13, "canonical_demo": 1, "compliance_view": 5, "impact_center": 4,
        "impact_economics": 5, "impact_intelligence": 5, "project_lifecycle": 3, "project_view": 7,
        "publishing_bridges": 1, "publishing_preview": 7, "release_manager": 4, "release_readiness": 2,
        "shell_view": 2, "support_diagnostics": 1, "translation_impact": 27,
        "translation_intelligence": 5, "translation_roundtrip": 4,
    },
    # Direct imports (any level) of the PDF/imaging/DOCX stack. Tightened in CP3.3:
    # authoring_ccms (docx), grounded_authoring (fitz) and legacy_family_inference (fitz)
    # left it; content_import (the DOCX reader) and pdf_pymupdf (the reading adapter) joined.
    "pdf_direct_imports": {
        "content_import": ["docx"],
        "layout_engine": ["PIL", "docx", "fitz", "numpy"],
        "legacy_doc_conversion": ["docx"],
        "legacy_family_inference": ["docx"],
        "pdf_pymupdf": ["fitz"],
        "publishing_bridges": ["docx"],
        "publishing_output": ["fitz"],
        "release_manager": ["docx"],
    },
    # Core candidates that load the PDF stack at import. Emptied by CP3.3 (it was 16
    # modules, mostly via ccms_industrial -> authoring_ccms -> layout_engine): it must stay empty.
    "core_pdf_at_import": [],
    # Knowledge of the project layout / data home OUTSIDE project_layout.py.
    # CP3.2 tightened this (see CP31_PROJECT_PATHS below for the CP3.1 picture) and
    # added the legacy ManualSync_CCMS_v18.sqlite name to the census. What is left:
    #   * thin compatibility wrappers kept because callers and suites use them
    #     (app._ccms_db, project_view.ccms_db, release_*._ccms_db) -- they only choose
    #     the exception/message, the lookup is ProjectLayout's; CP5 merged the two
    #     *_view.ccms_db_path copies into project_services._ccms_db (2 -> 1), the
    #     presenters keep the name as an alias;
    #   * app.DATA_HOME / commercial_acceptance: the public module global, now
    #     DEFINED by project_layout.default_data_home() but referenced by name;
    #   * test infrastructure guards (acceptance_guard, real_data_guard,
    #     verifica_webview2_offline, environment_guard) -- independent ON PURPOSE: a
    #     guard must still protect the real folder if product code ever got it wrong;
    #   * app_engineering.py -- legacy, not shipped, left untouched.
    "project_paths": {
        "acceptance_guard": {"'ManualSyncData'": 1, "'ManualSync_CCMS.sqlite'": 1, "'ManualSync_CCMS_v18.sqlite'": 1,
                             "DATA_HOME": 3},
        "app": {"DATA_HOME": 11, "def _ccms_db": 1},
        "app_engineering": {"'ManualSync_CCMS.sqlite'": 5, "'ManualSync_CCMS_v18.sqlite'": 1, "def _ccms_db": 1},
        "commercial_acceptance": {"DATA_HOME": 4},
        "environment_guard": {"'ManualSync_CCMS.sqlite'": 1},
        "project_services": {"def _ccms_db": 1},
        "project_view": {"def ccms_db": 1},
        "real_data_guard": {"'ManualSyncData'": 2},
        "release_manager": {"def _ccms_db": 1},
        "release_readiness": {"def _ccms_db": 1},
        "verifica_webview2_offline": {"'ManualSyncData'": 1},
    },
    # Statements that act when the module is imported.
    #   app: GRADIO_ANALYTICS_ENABLED, offline_frontend.install(), _load_active_project(),
    #        DATA_ROOT.mkdir(), the whole gr.Blocks UI;  manualsync_desktop: LOG_DIR from
    #        data_home() + mkdir;  temp_artifacts: atexit cleanup;  translation_work_package:
    #        ET.register_namespace (process-global);  erylos_theme: font files read;
    #        compliance_starter_packs: pack data built by _req();  test infrastructure as designed.
    "import_effects": {
        "app": ["env_write", "global_patch", "local_call", "mkdir", "ui_build"],
        "app_engineering": ["ui_build"],
        "compliance_starter_packs": ["local_call"],
        "environment_guard": ["local_call"],
        "erylos_theme": ["io_read", "local_call"],
        "manualsync_desktop": ["local_call", "mkdir"],
        "temp_artifacts": ["atexit"],
        "test_isolation": ["atexit", "env_write", "global_patch", "mkdir"],
        "translation_work_package": ["global_patch"],
        "verifica_webview2_offline": ["global_patch"],
    },
}

# The CP3.1 service -> presenter edges, kept to prove CP5 removed 5 of them -- never compared as a limit.
CP31_VIEW_IMPORT_EDGES = [
    "impact_center->authoring_view",
    "impact_intelligence->authoring_view",
    "impact_intelligence->compliance_view",
    "publishing_output->authoring_view",
    "publishing_preview->authoring_view",
    "translation_intelligence->authoring_view",
]

# CP3.2's core_pdf_at_import, kept to prove CP3.3 emptied it -- never compared as a limit.
CP32_CORE_PDF_AT_IMPORT = [
    "authoring_ccms", "authoring_service", "cat_tm", "ccms_industrial", "compliance_intelligence",
    "generative_orchestrator", "grounded_authoring", "impact_center", "impact_economics",
    "impact_intelligence", "project_lifecycle", "publishing_preview", "release_readiness",
    "translation_impact", "translation_intelligence", "workflow_management",
]

# The CP3.1 baseline (commit ae2d7b2), kept to prove CP3.2 reduced it -- never compared as a limit.
CP31_PROJECT_PATHS = {
    "acceptance_guard": {"'ManualSyncData'": 1, "'ManualSync_CCMS.sqlite'": 1, "DATA_HOME": 3},
    "app": {"'ManualSyncData'": 1, "'ManualSync_CCMS.sqlite'": 2, "DATA_HOME": 11, "def _ccms_db": 1},
    "app_engineering": {"'ManualSync_CCMS.sqlite'": 5, "def _ccms_db": 1},
    "authoring_ccms": {"'ManualSync_CCMS.sqlite'": 1},
    "authoring_view": {"'ManualSync_CCMS.sqlite'": 1, "def ccms_db_path": 1},
    "commercial_acceptance": {"'ManualSyncData'": 1, "'ManualSync_CCMS.sqlite'": 2, "DATA_HOME": 4},
    "compliance_view": {"'ManualSync_CCMS.sqlite'": 1, "def ccms_db_path": 1},
    "environment_guard": {"'ManualSync_CCMS.sqlite'": 1},
    "impact_center": {"'ManualSync_CCMS.sqlite'": 2},
    "manualsync_desktop": {"'ManualSyncData'": 1},
    "project_lifecycle": {"'ManualSync_CCMS.sqlite'": 2},
    "project_view": {"'ManualSync_CCMS.sqlite'": 1, "def ccms_db": 1},
    "real_data_guard": {"'ManualSyncData'": 2},
    "release_manager": {"'ManualSync_CCMS.sqlite'": 1, "def _ccms_db": 1},
    "release_readiness": {"'ManualSync_CCMS.sqlite'": 1, "def _ccms_db": 1},
    "shell_view": {"'ManualSync_CCMS.sqlite'": 1, "def _ccms": 1},
    "support_diagnostics": {"'ManualSync_CCMS.sqlite'": 2},
    "verifica_webview2_offline": {"'ManualSyncData'": 1},
    "workflow_management": {"'ManualSync_CCMS.sqlite'": 1},
}

if __name__ == "__main__" and "--print-baseline" in sys.argv:
    pprint.pprint(current_baseline(analyse(load_sources(BASE))), width=120, sort_dicts=True)
    sys.exit(0)

# ============================================================ the suite
_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:600]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:600]}]"))


def tree_hash(root: Path) -> str:
    """The analysed product sources and the locks (the suites themselves are not product)."""
    h = hashlib.sha256()
    for p in sorted(q for q in root.glob("*.py") if not is_suite(q.stem)):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    for p in sorted(root.glob("requirements*.txt")):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


HASH_BEFORE = tree_hash(BASE)
sources = load_sources(BASE)
facts = analyse(sources)
cur = current_baseline(facts)
violations, notes = evaluate(facts, BASE, BASELINE)
by_rule: dict[str, list[str]] = {}
for rule, detail in violations:
    by_rule.setdefault(rule, []).append(detail)

print(f"  {len(sources)} modules analysed (regressione_* excluded)")
check("A: gradio is imported only by app, app_engineering, offline_frontend, manualsync_desktop",
      not by_rule.get("A"), by_rule.get("A"))
check("A2: the UI/Desktop set is exactly the CP3.0 one (all four still import gradio)",
      cur["gradio_importers"] == BASELINE["gradio_importers"], cur["gradio_importers"])
check("B: core candidates import no gradio / webview / FastAPI-family / Windows API / Stripe / AI SDK",
      not by_rule.get("B"), by_rule.get("B"))
check("C: no service/adapter imports a *_view presenter beyond the CP5 baseline (publishing_preview only)",
      not by_rule.get("C"), by_rule.get("C"))
check("C2: CP5 removed 5 of the 6 CP3.1 service -> presenter edges; only publishing_preview -> authoring_view is left",
      len(CP31_VIEW_IMPORT_EDGES) == 6 and cur["view_import_edges"] == ["publishing_preview->authoring_view"]
      and set(cur["view_import_edges"]) <= set(CP31_VIEW_IMPORT_EDGES), cur["view_import_edges"])
check("D: no concrete AI SDK in the application code or in the requirement locks",
      not by_rule.get("D"), by_rule.get("D"))
check("E: no hardcoded AI API key in the product folder", not by_rule.get("E"), by_rule.get("E"))
check("F: core candidates import no network library", not by_rule.get("F"), by_rule.get("F"))
sql_total = sum(sum(d["sql"].values()) for d in facts["modules"].values())
sql_auth = {m: sum(facts["modules"][m]["sql"].values()) for m in sorted(SQL_AUTHORIZED) if m in facts["modules"]}
print(f"  SQL: {sql_total} calls in {sum(1 for d in facts['modules'].values() if d['sql'])} modules -- "
      f"persistence {sum(sql_auth.values())} in {len(sql_auth)}, outside {sum(cur['sql_tolerated'].values())} "
      f"in {len(cur['sql_tolerated'])}")
check("SQL: no new module issues SQL and no tolerated module issues more SQL than the baseline",
      not by_rule.get("SQL"), by_rule.get("SQL"))
check("PDF: no new PDF/imaging/DOCX import and no new core candidate loading PyMuPDF at import",
      not by_rule.get("PDF"), by_rule.get("PDF"))
check("PDF2: layout_engine is (still) the module that imports fitz, numpy, PIL and docx at import",
      {"fitz", "numpy", "PIL", "docx"} <= set(facts["modules"]["layout_engine"]["imports_top"]),
      facts["modules"]["layout_engine"]["imports_top"])
check("PATH: project-path / data-home knowledge does not spread beyond the baseline",
      not by_rule.get("PATH"), by_rule.get("PATH"))
check("EFFECTS: no new import-time side effect", not by_rule.get("EFFECTS"), by_rule.get("EFFECTS"))
check("LAYOUT: project_layout exists, owns the layout names and stays pure (stdlib only, no SQL, no import effect)",
      not by_rule.get("LAYOUT"), by_rule.get("LAYOUT"))
# CP3.2 before/after: same census keys as CP3.1 (the v18 name was not counted then).
_cp31_keys = {"'ManualSyncData'", "'ManualSync_CCMS.sqlite'", "DATA_HOME", "def _ccms_db", "def ccms_db_path",
              "def ccms_db", "def _ccms"}
_cp31_total = sum(n for d in CP31_PROJECT_PATHS.values() for k, n in d.items() if k in _cp31_keys)
_now_total = sum(n for d in cur["project_paths"].values() for k, n in d.items() if k in _cp31_keys)
_ccms_lit_31 = sum(d.get("'ManualSync_CCMS.sqlite'", 0) for d in CP31_PROJECT_PATHS.values())
_ccms_lit_now = sum(d.get("'ManualSync_CCMS.sqlite'", 0) for d in cur["project_paths"].values())
print(f"  PATH: modules with layout knowledge {len(CP31_PROJECT_PATHS)} -> {len(cur['project_paths'])} (+ project_layout); "
      f"occurrences {_cp31_total} -> {_now_total}; 'ManualSync_CCMS.sqlite' literals {_ccms_lit_31} -> {_ccms_lit_now}")
check("PATH2: CP3.2 reduced the layout knowledge spread across modules (fewer modules, fewer copies)",
      len(cur["project_paths"]) < len(CP31_PROJECT_PATHS) and _now_total < _cp31_total and _ccms_lit_now < _ccms_lit_31,
      (len(cur["project_paths"]), _now_total, _ccms_lit_now))
check("PDFB: PyMuPDF only in layout_engine, pdf_pymupdf and publishing_output; the reading contract is pure "
      "(stdlib typing, no SQL, no UI/web/network/AI, no import effect) and core imports no layout_engine",
      not by_rule.get("PDFB"), by_rule.get("PDFB"))
_fitz_users = sorted(m for m, d in facts["modules"].items() if set(d["imports_all"]) & {"fitz", "pymupdf"})
print(f"  PDF: PyMuPDF imported by {', '.join(_fitz_users)}; core candidates loading the PDF stack at import "
      f"{len(CP32_CORE_PDF_AT_IMPORT)} -> {len(cur['core_pdf_at_import'])}")
check("PDF3: CP3.3 removed the PDF stack from the import of every core candidate (CP3.2 had 16)",
      len(CP32_CORE_PDF_AT_IMPORT) == 16 and cur["core_pdf_at_import"] == [], cur["core_pdf_at_import"])
_readers = sorted(m for m, d in facts["modules"].items() if PDF_CONTRACT in d["local_all"])
print(f"  PDF: modules reading PDFs through {PDF_CONTRACT}: {', '.join(_readers)}")
check("PDF4: the PDF readers of the import, grounding, TM and legacy-family code go through pdf_backend",
      {"content_import", "grounded_authoring", "cat_tm", "legacy_family_inference"} <= set(_readers), _readers)
check("ALGO: translation_alignment and language_signals import only re/pathlib -- no PyMuPDF, layout_engine, SQL, "
      "UI, presenter, app or network -- and do nothing at import", not by_rule.get("ALGO"), by_rule.get("ALGO"))
_algo_users = {m: sorted(x for x in d["local_all"] if x in ALGORITHM_MODULES)
               for m, d in facts["modules"].items() if set(d["local_all"]) & ALGORITHM_MODULES}
print(f"  ALGO: consumers of the pure algorithm modules: {_algo_users}")
check("ALGO2: layout_engine, the translation memory and the IDML importer take the algorithms from the new modules",
      {"layout_engine", "cat_tm", "idml_import"} <= set(_algo_users)
      and "layout_engine" not in facts["modules"]["idml_import"]["local_all"]
      and "layout_engine" not in facts["modules"]["cat_tm"]["local_all"], _algo_users)
layout_users = sorted(m for m, d in facts["modules"].items() if LAYOUT_MODULE in d["local_all"])
print(f"  LAYOUT: {len(layout_users)} modules resolve paths through project_layout: {', '.join(layout_users)}")
check("LAYOUT2: ProjectLayout is the central point -- the shipped modules that resolve the content database use it",
      {"app", "project_services", "project_view", "shell_view", "release_manager", "release_readiness",
       "project_lifecycle", "impact_center", "support_diagnostics", "workflow_management", "authoring_ccms",
       "ccms_industrial", "commercial_acceptance", "manualsync_desktop"} <= set(layout_users), layout_users)
for n in notes:
    print("  NOTE  " + n)

# ============================================================ M: every rule can fail
# M0 analyses a full temporary copy of the sources. Each mutant then writes ONE
# mutated module (plus the locks) into its own temporary folder; the check runs on
# the clean copy with that file read back from disk in place of the original.
FAKE_KEY = "sk-" + "ant-" + "api03-" + "Q" * 40          # assembled at runtime: never a literal here
MUTANTS = [
    ("core -> Gradio", "rich_text", "\nimport gradio as gr\n", "B"),
    ("core -> FastAPI (lazy import)", "domain_errors", "\ndef _m():\n    from fastapi import FastAPI\n    return FastAPI\n", "B"),
    ("core -> Windows (winreg)", "language_codes", "\nimport winreg\n", "B"),
    ("core -> Windows (os.startfile)", "linguistic_safety", "\nimport os\ndef _m(p):\n    os.startfile(p)\n", "B"),
    ("core -> Stripe", "publishing_preflight", "\nimport stripe\n", "B"),
    ("core -> network", "linguistic_safety", "\nimport requests\n", "F"),
    ("non-UI module -> Gradio", "shell_view", "\nimport gradio\n", "A"),
    ("service -> presenter", "compliance_intelligence", "\nimport authoring_view\n", "C"),
    ("AI SDK in app", "app", "\nimport anthropic\n", "D"),
    ("hardcoded API key", "product_version", f"\n_K = \"{FAKE_KEY}\"\n", "E"),
    ("new SQL module", "rich_text", "\ndef _q(c):\n    return c.execute('SELECT 1')\n", "SQL"),
    ("more SQL in tolerated module", "authoring_view", "\ndef _q(c):\n    return c.execute('SELECT 1')\n", "SQL"),
    ("new PDF import", "publishing_preflight", "\nimport fitz\n", "PDF"),
    ("core now loads PDF at import", "language_codes", "\nimport layout_engine\n", "PDF"),
    ("new project-path copy", "publishing_preflight", "\n_DB = 'ManualSync_CCMS.sqlite'\n", "PATH"),
    ("new import-time effect", "rich_text", "\nimport atexit\natexit.register(print)\n", "EFFECTS"),
    ("migrated copy re-introduced", "impact_center", "\n_DB = 'ManualSync_CCMS.sqlite'\n", "PATH"),
    ("legacy name copied again", "shell_view", "\n_OLD = ('ManualSync_CCMS_v18.sqlite',)\n", "PATH"),
    ("data home re-derived", "manualsync_desktop", "\n_H = 'ManualSyncData'\n", "PATH"),
    ("ProjectLayout opens SQLite", "project_layout", "\nimport sqlite3\n", "LAYOUT"),
    ("ProjectLayout creates folders at import", "project_layout", "\nPath('x').mkdir(exist_ok=True)\n", "LAYOUT"),
    ("core service imports PyMuPDF again", "grounded_authoring", "\nimport fitz\n", "PDFB"),
    ("UI presenter imports PyMuPDF", "shell_view", "\nimport fitz\n", "PDFB"),
    ("reading contract imports PyMuPDF", "pdf_backend", "\nimport fitz\n", "PDFB"),
    ("reading contract depends on the repository", "pdf_backend", "\nimport ccms_industrial\n", "PDFB"),
    ("PDF adapter reaches the network", "pdf_pymupdf", "\nimport requests\n", "PDFB"),
    ("core imports layout_engine at import", "cat_tm", "\nfrom layout_engine import map_translation_blocks\n", "PDF"),
    ("SQL in the PDF/DOCX reader", "content_import", "\ndef _q(c):\n    return c.execute('SELECT 1')\n", "SQL"),
    ("algorithm imports PyMuPDF", "translation_alignment", "\nimport fitz\n", "ALGO"),
    ("algorithm imports the PDF engine", "translation_alignment", "\nimport layout_engine\n", "ALGO"),
    ("algorithm imports SQLite", "language_signals", "\nimport sqlite3\n", "ALGO"),
    ("algorithm imports Gradio", "language_signals", "\nimport gradio\n", "ALGO"),
    ("algorithm imports the app", "translation_alignment", "\ndef _m():\n    import app\n", "ALGO"),
    ("algorithm imports a presenter", "language_signals", "\nimport authoring_view\n", "ALGO"),
    ("algorithm reaches the network", "translation_alignment", "\nimport requests\n", "ALGO"),
    ("algorithm imports FastAPI", "language_signals", "\nfrom fastapi import FastAPI\n", "ALGO"),
    ("composition root imports a presenter", "project_services", "\nimport authoring_view\n", "C"),
    ("composition root issues SQL", "project_services", "\ndef _q(c):\n    return c.execute('SELECT 1')\n", "SQL"),
    ("composition root imports Gradio", "project_services", "\nimport gradio\n", "B"),
    ("service re-imports a presenter", "impact_center", "\nimport authoring_view\n", "C"),
]
mut_root = Path(tempfile.mkdtemp(prefix="erylos_cp31_mutants_"))
try:
    clean = mut_root / "clean"
    clean.mkdir()
    for p in BASE.glob("*.py"):
        if not is_suite(p.stem):
            shutil.copy2(p, clean / p.name)
    for p in BASE.glob("requirements*.txt"):
        shutil.copy2(p, clean / p.name)
    clean_sources = load_sources(clean)
    cv, _ = evaluate(analyse(clean_sources), clean, BASELINE)
    check("M0: an unmodified copy produces no violation (no false positive)", not cv, cv)

    def mutant_dir(tag: str) -> Path:
        d = mut_root / tag
        d.mkdir()
        for lock in clean.glob("requirements*.txt"):
            shutil.copy2(lock, d / lock.name)
        return d

    for i, (label, module, snippet, rule) in enumerate(MUTANTS, 1):
        d = mutant_dir(f"m{i:02d}")
        target = d / f"{module}.py"
        target.write_text(clean_sources[module] + snippet, encoding="utf-8")
        mutated = dict(clean_sources)
        mutated[module] = target.read_text(encoding="utf-8")
        mv, _ = evaluate(analyse(mutated), d, BASELINE)
        fired = [x for r, x in mv if r == rule]
        check(f"M{i}: mutant '{label}' ({module}) is caught by rule {rule}", fired, mv)
        shutil.rmtree(d, ignore_errors=True)
    # the lock rule too: an AI SDK pinned in a requirements file
    d = mutant_dir("mlock")
    (d / "requirements.runtime.lock.txt").write_text(
        (d / "requirements.runtime.lock.txt").read_text(encoding="utf-8") + "\nopenai==1.0.0\n", encoding="utf-8")
    mv, _ = evaluate(analyse(clean_sources), d, BASELINE)
    check(f"M{len(MUTANTS) + 1}: mutant 'AI SDK pinned in a lock' is caught by rule D", [x for r, x in mv if r == "D"], mv)
finally:
    shutil.rmtree(mut_root, ignore_errors=True)
check(f"M{len(MUTANTS) + 2}: the mutant folders were removed", not mut_root.exists(), mut_root)
check("I1: the real sources and locks were not modified by this suite", tree_hash(BASE) == HASH_BEFORE, "")

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_ARCHITECTURE_BOUNDARIES.json").write_text(json.dumps({
    "suite": "architecture boundaries (CP3.1)", "tests": total, "pass": passed, "fail": total - passed,
    "modules_analysed": len(sources), "sql_total_calls": sql_total, "sql_persistence": sql_auth,
    "current": cur, "shrink_notes": notes,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
