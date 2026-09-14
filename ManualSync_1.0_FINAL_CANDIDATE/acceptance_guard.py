"""Guard for MUTATING manual/browser acceptance runs.

Why this exists
---------------
A UX Gate 1 acceptance run drove the real UI against the canonical demo
project and recorded a real applicability decision in it. Nothing in the
product misbehaved -- the script clicked, and the UI did exactly what it
is supposed to do. But the damage was invisible on screen: the header
counts read 2/38/0/1/41 both before and after, and only the coverage
ratio moved, 40% -> 33%. A demo used as a reference baseline had silently
stopped matching its own documentation.

The discipline this module encodes
----------------------------------
1. FAIL-CLOSED ALLOWLIST. A script that records decisions may only run
   against a project explicitly marked disposable (``ZZ_`` prefix).
   Everything else is refused: the canonical demo, every past acceptance
   project, and -- importantly -- every project that does not exist yet.
   A denylist would have protected only the names somebody remembered to
   list, which is precisely the case that failed.

2. OVERLAY SNAPSHOT + DELTA. Even on a disposable clone, a run states
   what it wrote. A "+0 everywhere" delta is how a supposedly read-only
   acceptance proves it was read-only; a non-zero delta on a run that
   claimed to change nothing is a failed acceptance.

Both read paths open SQLite ``mode=ro``: the guard itself cannot write.

Usage, at the very top of a mutating acceptance script -- before the
browser is launched, so a refusal costs nothing::

    from acceptance_guard import guarded_start, overlay_snapshot, report_delta

    project, before = guarded_start()      # refuses and exits(2) if not disposable
    ...                                    # drive the UI
    report_delta(before, overlay_snapshot(project))
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Mirrors app.DATA_HOME / app.ACTIVE_PROJECT_FILE. Duplicated deliberately:
# importing app here would pull Gradio and build the whole UI just to read
# two paths. If those move in app.py, they must move here too.
DATA_HOME = Path.home() / "ManualSyncData"
DATA_ROOT = DATA_HOME / "Projects"
ACTIVE_PROJECT_FILE = DATA_HOME / "active_project.json"

#: Only projects whose name starts with this may be mutated by an
#: acceptance script. This prefix IS the mechanism; everything below is
#: only there to make refusals say something useful.
DISPOSABLE_PREFIX = "ZZ_"

#: The project that carries the documented reference baseline
#: (41 requirements / 6 applicability / 2 COVERED / 3 evidence / 40%).
#: Read-only and manual acceptance may look at it; nothing may write to it.
CANONICAL_DEMO = "ERYLOS_Canonical_Demo"

#: Superseded demos, kept on disk with their audit history intact. Recorded
#: here so the retired one can never be mistaken for the canonical baseline.
RETIRED_DEMOS = {
    "4E3_Rich_Text_Manual_Acceptance":
        "Former canonical demo. Drifted on 2026-09-12 when a mutating "
        "acceptance run recorded one extra applicability decision "
        "(MR2023-A10-8-DOC-AVAILABILITY), moving coverage 40% -> 33%. "
        "Preserved unchanged, audit history included; superseded by "
        "ERYLOS_Canonical_Demo.",
}

# The project OVERLAY: what a project records on top of the imported pack.
# This is exactly what an acceptance run can corrupt without it showing.
_OVERLAY = {
    "applicability": "SELECT COUNT(*) FROM compliance_applicability",
    "assessments": "SELECT COUNT(*) FROM compliance_assessment",
    "covered": "SELECT COUNT(*) FROM compliance_assessment WHERE status='COVERED'",
    "evidence": "SELECT COUNT(*) FROM compliance_evidence",
    "evidence_links": "SELECT COUNT(*) FROM compliance_evidence_link",
    "audit_events": "SELECT COUNT(*) FROM audit_events",
}

_DB_NAMES = ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite")


class AcceptanceGuardError(RuntimeError):
    """Raised BEFORE any mutation when the target is not disposable."""


def is_disposable(project_name: str) -> bool:
    """The single rule, exposed so callers and tests agree on it."""
    name = str(project_name or "")
    return name.startswith(DISPOSABLE_PREFIX) and name != CANONICAL_DEMO


def active_project_dir() -> Path:
    """The project the app is pointed at, per its own pointer file."""
    if not ACTIVE_PROJECT_FILE.exists():
        raise AcceptanceGuardError(f"No active-project pointer at {ACTIVE_PROJECT_FILE}")
    try:
        data = json.loads(ACTIVE_PROJECT_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise AcceptanceGuardError(f"Unreadable active-project pointer: {exc}") from exc
    project_dir = str((data or {}).get("project_dir") or "").strip()
    if not project_dir:
        raise AcceptanceGuardError(f"Empty project_dir in {ACTIVE_PROJECT_FILE}")
    return Path(project_dir)


def database_path(project: Path) -> Path:
    for name in _DB_NAMES:
        candidate = project / name
        if candidate.exists():
            return candidate
    raise AcceptanceGuardError(f"No CCMS database in {project}")


def require_disposable(project: Path) -> Path:
    """Refuse unless this project is explicitly disposable."""
    name = project.name
    if name == CANONICAL_DEMO:
        raise AcceptanceGuardError(
            f"REFUSING TO RUN: this is the CANONICAL DEMO ({name}).\n"
            f"  It carries the documented reference baseline and must stay byte-identical.\n"
            f"  Clone it to a '{DISPOSABLE_PREFIX}...' project and point the app there first."
        )
    if name in RETIRED_DEMOS:
        raise AcceptanceGuardError(
            f"REFUSING TO RUN: '{name}' is a RETIRED demo kept for its audit history.\n"
            f"  {RETIRED_DEMOS[name]}\n"
            f"  Clone it to a '{DISPOSABLE_PREFIX}...' project if you need to drive it."
        )
    if not is_disposable(name):
        raise AcceptanceGuardError(
            f"REFUSING TO RUN: '{name}' is not marked disposable.\n"
            f"  A mutating acceptance script may only target a project named "
            f"'{DISPOSABLE_PREFIX}...'.\n"
            f"  This is fail-closed on purpose: every project is protected unless it "
            f"says otherwise."
        )
    database_path(project)  # raises if there is nothing to drive
    return project


def require_disposable_target() -> Path:
    """Guard the project the app is currently pointed at."""
    return require_disposable(active_project_dir())


def overlay_snapshot(project: Path) -> dict[str, int]:
    """Read-only counts of everything an acceptance run could change.

    A table this project has not created yet reports -1 rather than
    raising: a snapshot must never be the thing that fails a run.
    """
    conn = sqlite3.connect(f"file:{database_path(project)}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for key, sql in _OVERLAY.items():
            try:
                out[key] = int(conn.execute(sql).fetchone()[0])
            except sqlite3.OperationalError:
                out[key] = -1
        return out
    finally:
        conn.close()


def report_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Print, and return, what the run actually wrote."""
    delta = {k: after.get(k, 0) - v for k, v in before.items()}
    changed = {k: v for k, v in delta.items() if v}
    print("\n--- overlay delta (what this acceptance run wrote) ---")
    for key in before:
        mark = " " if not delta[key] else "*"
        print(f" {mark} {key:16s} {before[key]:>5} -> {after.get(key):<5} ({delta[key]:+d})")
    print("  no overlay change" if not changed else f"  CHANGED: {changed}")
    return delta


def guarded_start() -> tuple[Path, dict[str, int]]:
    """One call for the top of a mutating script: refuse, then snapshot.

    Exits the process with status 2 on refusal, so a guarded script cannot
    accidentally continue past it.
    """
    try:
        project = require_disposable_target()
    except AcceptanceGuardError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        raise SystemExit(2)
    before = overlay_snapshot(project)
    print(f"guard OK -- target is disposable: {project.name}")
    print(f"  overlay before: {before}")
    return project, before
