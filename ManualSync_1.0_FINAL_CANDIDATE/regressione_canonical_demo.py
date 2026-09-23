"""Canonical demo — the reference baseline is reproducible, from the repository.

This suite rebuilds the demo from scratch on every run and asserts the
documented numbers:

    41 requirements / 6 applicability / 2 COVERED / 3 evidence links / 40%

It exists because the previous demo drifted and could not be rebuilt: the
only recipe lived in a scratch folder. Now a change to the pack, the
engine or the coverage view model that would move the baseline fails here,
loudly, instead of showing up as a demo quietly reading 33%.

It also pins the two properties that make the recipe trustworthy: it uses
supported service APIs only (no SQL against a project), and it refuses to
overwrite a project that already exists.
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import ast
import json
import re
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_demo as cd  # noqa: E402
import compliance_view as cv  # noqa: E402
import i18n  # noqa: E402

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


# =====================================================================
# 1. The recipe uses supported APIs only
# =====================================================================
SRC = (Path(__file__).parent / "canonical_demo.py").read_text(encoding="utf-8")
writes = re.findall(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE TABLE)\b", SRC, re.IGNORECASE)
check("api1: the builder executes no SQL write against a project -- every row is written "
      "through a service the product itself uses", not writes, writes)
check("api2: it does not reach for a raw sqlite connection either",
      "sqlite3" not in SRC and ".executescript(" not in SRC)

tree = ast.parse(SRC)
calls = {n.func.attr for n in ast.walk(tree)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
for required in ("create_scope", "import_requirement_pack", "set_applicability",
                 "set_assessment", "create_block_in_topic"):
    check(f"api3: the builder really calls {required}()", required in calls, sorted(calls))
check("api4: evidence is linked through the same helper the UI uses",
      "link_ccms_object_as_evidence" in SRC)

# =====================================================================
# 2. Build it and measure
# =====================================================================
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    project = cd.build_canonical_demo(root, name="ZZ_Canonical_Rebuild")
    check("build1: the project directory was created", project.exists(), project)

    m = cd.measure(project)
    for key, want in cd.BASELINE.items():
        check(f"base[{key}]: {want}", m[key] == want, {"expected": want, "got": m[key]})
    check("base-counts: the workspace header shows the documented counts",
          m["counts"] == cd.BASELINE_COUNTS, m["counts"])
    check("base-helper: matches_baseline agrees", cd.matches_baseline(m), m)

    # The ratio is the figure that drifted unnoticed last time, because the
    # header counts did NOT move with it. Pin the relationship explicitly.
    check("ratio1: 40% is 2 covered out of 5 applicable, not a stored number",
          m["ratio_percent"] == round(100 * 2 / 5), m["ratio_percent"])

    # 3. Language must not move the baseline.
    i18n.set_language("en")
    m_en = cd.measure(project)
    i18n.set_language("it")
    check("lang1: the baseline is identical in English -- language changes words, "
          "never counts",
          {k: m_en[k] for k in cd.BASELINE} == {k: m[k] for k in cd.BASELINE},
          {"it": {k: m[k] for k in cd.BASELINE}, "en": {k: m_en[k] for k in cd.BASELINE}})
    check("lang2: ...including the header counts", m_en["counts"] == m["counts"], m_en["counts"])

    # 4. It refuses to overwrite.
    try:
        cd.build_canonical_demo(root, name="ZZ_Canonical_Rebuild")
        refused = False
    except FileExistsError:
        refused = True
    check("safe1: rebuilding over an existing project is refused, never silently "
          "overwritten", refused)

    # 5. A rebuild in a clean root is deterministic.
    second = cd.build_canonical_demo(root, name="ZZ_Canonical_Rebuild_2")
    m2 = cd.measure(second)
    check("repeat1: a second independent build yields the same baseline",
          {k: m2[k] for k in cd.BASELINE} == {k: m[k] for k in cd.BASELINE},
          {k: (m[k], m2[k]) for k in cd.BASELINE})

    # 6. The demo is a real, usable project, not just correct counters.
    repo, svc = cv.open_service(project)
    try:
        rows = cv.requirement_list_rows(svc, m["scope_id"])
        check("usable1: the requirement list renders every requirement",
              len(rows) == cd.BASELINE["requirements"], len(rows))
        covered = [r for r in rows if r["status"] == "Covered"]
        check("usable2: the covered requirements really carry evidence",
              len(covered) == 2 and all(r["has_evidence"] for r in covered),
              [(r["requirement_id"], r["has_evidence"]) for r in covered])
        check("usable3: the not-applicable decision carries its justification",
              any((svc.applicability_row(m["scope_id"], rid) or {}).get("justification")
                  for rid in svc.requirements_in_scope(m["scope_id"])))
        check("usable4: the queue of work left is the rest of the pack",
              len(cv.to_complete_rows(svc, m["scope_id"])) == 38,
              len(cv.to_complete_rows(svc, m["scope_id"])))
    finally:
        repo.close()

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
