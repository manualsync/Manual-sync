"""Acceptance guard — a mutating acceptance run may only target a disposable project.

This suite exists because the rule it pins was learned the expensive way: a
UX Gate 1 acceptance run drove the real UI against the canonical demo and
recorded an applicability decision in it. The header counts were identical
before and after (2/38/0/1/41); only the coverage ratio moved, 40% -> 33%.
A reference baseline stopped matching its own documentation and nothing on
screen said so.

So the two properties under test are:

* the allowlist is FAIL-CLOSED -- an unknown project name is refused, not
  allowed. A denylist would only have protected names somebody thought of;
* a run can PROVE what it wrote, via an overlay snapshot and a delta.

Every fixture is a real project built through the supported APIs, and every
read the guard performs is opened ``mode=ro``, so the guard can never be the
thing that mutates a project.

Pure server-side: no browser, no server, no Gradio event loop.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance_guard as ag  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from compliance_intelligence import ComplianceService  # noqa: E402
from project_lifecycle import create_project  # noqa: E402

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def refusal_for(project: Path) -> str | None:
    """The refusal message, or None if the guard allowed this project."""
    try:
        ag.require_disposable(project)
        return None
    except ag.AcceptanceGuardError as exc:
        return str(exc)


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    # ---------------------------------------------------------------
    # 1. The rule itself, on names alone.
    # ---------------------------------------------------------------
    check("rule1: a ZZ_ project is disposable", ag.is_disposable("ZZ_Gate1_Acceptance"))
    check("rule2: the canonical demo is never disposable, prefix or not",
          not ag.is_disposable(ag.CANONICAL_DEMO))
    check("rule3: a retired demo is not disposable",
          all(not ag.is_disposable(n) for n in ag.RETIRED_DEMOS), sorted(ag.RETIRED_DEMOS))
    check("rule4: FAIL-CLOSED -- a project nobody has ever heard of is refused, "
          "not allowed",
          not ag.is_disposable("Some_Customer_Project_Created_Next_Year"))
    check("rule5: the prefix is a prefix, not a substring -- 'My_ZZ_Thing' is not "
          "disposable", not ag.is_disposable("My_ZZ_Thing"))
    check("rule6: an empty or missing name is refused",
          not ag.is_disposable("") and not ag.is_disposable(None))

    # ---------------------------------------------------------------
    # 2. Real projects on disk.
    # ---------------------------------------------------------------
    disposable = create_project(root, project_name="ZZ_Gate_Probe", client="X")
    canonical = create_project(root, project_name=ag.CANONICAL_DEMO, client="X")
    retired = create_project(root, project_name=sorted(ag.RETIRED_DEMOS)[0], client="X")
    customer = create_project(root, project_name="Cliente_Reale_2027", client="X")

    check("proj1: a disposable project is allowed", refusal_for(disposable) is None)

    msg_canonical = refusal_for(canonical)
    check("proj2: the canonical demo is refused", msg_canonical is not None)
    check("proj2b: and the refusal says WHY it is protected, by name",
          msg_canonical and "CANONICAL DEMO" in msg_canonical
          and ag.CANONICAL_DEMO in msg_canonical, msg_canonical)

    msg_retired = refusal_for(retired)
    check("proj3: a retired demo is refused", msg_retired is not None)
    check("proj3b: and the refusal explains it is kept for its audit history",
          msg_retired and "RETIRED" in msg_retired and "audit history" in msg_retired,
          msg_retired)

    msg_customer = refusal_for(customer)
    check("proj4: an ordinary project is refused too", msg_customer is not None)
    check("proj4b: and the refusal states the fail-closed rule",
          msg_customer and "not marked disposable" in msg_customer
          and ag.DISPOSABLE_PREFIX in msg_customer, msg_customer)

    check("proj5: every refusal names a concrete next step (clone to ZZ_...)",
          all(ag.DISPOSABLE_PREFIX in m for m in (msg_canonical, msg_retired, msg_customer)))

    # A project with no CCMS database cannot be driven at all.
    empty = root / "ZZ_No_Database"
    empty.mkdir()
    check("proj6: a disposable name with no CCMS database is still refused",
          refusal_for(empty) is not None, refusal_for(empty))

    # ---------------------------------------------------------------
    # 3. The overlay snapshot and the delta.
    # ---------------------------------------------------------------
    # A brand-new project has no compliance_* tables yet: the schema is created
    # the first time a ComplianceService opens it. The snapshot reports -1 for
    # a table that does not exist, which is deliberately distinguishable from
    # 0 ("exists, no rows") -- and it must not raise either way.
    bare = ag.overlay_snapshot(disposable)
    check("snap1: the snapshot covers every overlay table an acceptance run can write",
          set(bare) == {"applicability", "assessments", "covered", "evidence",
                        "evidence_links", "audit_events"}, sorted(bare))
    check("snap2: a table that does not exist yet reports -1, not a crash and not 0",
          bare["applicability"] == -1 and bare["audit_events"] == 0, bare)

    # From here on, the realistic case: a project a ComplianceService has opened.
    repo = IndustrialRepository(ag.database_path(disposable))
    ComplianceService(repo)
    repo.close()

    before = ag.overlay_snapshot(disposable)
    check("snap2b: once the schema exists, an untouched overlay is 0 everywhere",
          all(v == 0 for v in before.values()), before)

    # Write one row the way a decision would, then prove the delta reports it.
    db = ag.database_path(disposable)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO compliance_applicability"
                 "(scope_id,requirement_id,applies,requirement_version,justification,"
                 " decided_by,decided_at) VALUES('S','R','APPLIES',1,'','t','now')")
    conn.commit()
    conn.close()

    after = ag.overlay_snapshot(disposable)
    delta = ag.report_delta(before, after)
    check("snap3: the delta reports exactly the one row that was written",
          delta["applicability"] == 1, delta)
    check("snap4: and reports zero for everything untouched",
          all(v == 0 for k, v in delta.items() if k != "applicability"), delta)
    check("snap5: an unchanged project reports a delta of zero everywhere -- this is "
          "how a read-only acceptance proves it was read-only",
          not any(ag.report_delta(after, ag.overlay_snapshot(disposable)).values()))

    # ---------------------------------------------------------------
    # 4. The guard cannot itself mutate what it inspects.
    # ---------------------------------------------------------------
    digest_before = db.read_bytes()
    for _ in range(3):
        ag.overlay_snapshot(disposable)
        refusal_for(canonical)
    check("ro1: snapshotting and refusing leave the database byte-identical",
          db.read_bytes() == digest_before)

    ro_conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ro_conn.execute("CREATE TABLE nope(x)")
        wrote = True
    except sqlite3.OperationalError:
        wrote = False
    finally:
        ro_conn.close()
    check("ro2: the guard's own connection mode genuinely refuses writes", not wrote)

    # ---------------------------------------------------------------
    # 5. The pointer path.
    # ---------------------------------------------------------------
    check("ptr1: the guard reads the same pointer file the app writes",
          ag.ACTIVE_PROJECT_FILE.name == "active_project.json"
          and ag.ACTIVE_PROJECT_FILE.parent == ag.DATA_HOME, ag.ACTIVE_PROJECT_FILE)

    missing = root / "nowhere" / "active_project.json"
    real_pointer = ag.ACTIVE_PROJECT_FILE
    try:
        ag.ACTIVE_PROJECT_FILE = missing
        try:
            ag.active_project_dir()
            raised = False
        except ag.AcceptanceGuardError:
            raised = True
        check("ptr2: a missing pointer is an error, never a silent default",
              raised)

        bad = root / "bad_pointer.json"
        bad.write_text("{}", encoding="utf-8")
        ag.ACTIVE_PROJECT_FILE = bad
        try:
            ag.active_project_dir()
            raised = False
        except ag.AcceptanceGuardError:
            raised = True
        check("ptr3: a pointer with no project_dir is an error too", raised)
    finally:
        ag.ACTIVE_PROJECT_FILE = real_pointer

    check("ptr4: the pointer module global was restored after the test",
          ag.ACTIVE_PROJECT_FILE == real_pointer)

    # ---------------------------------------------------------------
    # 6. The registry keeps the two demos distinguishable.
    # ---------------------------------------------------------------
    check("reg1: the canonical demo is not also listed as retired",
          ag.CANONICAL_DEMO not in ag.RETIRED_DEMOS)
    check("reg2: the retired former demo is recorded, with the reason",
          "4E3_Rich_Text_Manual_Acceptance" in ag.RETIRED_DEMOS
          and "40%" in ag.RETIRED_DEMOS["4E3_Rich_Text_Manual_Acceptance"],
          sorted(ag.RETIRED_DEMOS))

print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"),
                  "tests": len(results)}))
