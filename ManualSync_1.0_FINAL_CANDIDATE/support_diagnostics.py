from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from product_version import PRODUCT_BUILD, PRODUCT_VERSION
from project_lifecycle import read_project, project_health

SUPPORT_BUILD = "1.0-commercial-stabilization-support-20260819"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_integrity(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "status": "NOT_PRESENT"}
    try:
        conn = sqlite3.connect(str(path))
        try:
            value = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
        return {"exists": True, "status": "PASS" if value.lower() == "ok" else "FAIL", "detail": value}
    except Exception as exc:
        return {"exists": True, "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}


def system_health(data_home: str | Path, project_dir: str | Path | None = None) -> dict[str, Any]:
    data_home = Path(data_home)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    try:
        data_home.mkdir(parents=True, exist_ok=True)
        probe = data_home / f".write_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        add("Data directory writable", True, str(data_home))
    except Exception as exc:
        add("Data directory writable", False, f"{type(exc).__name__}: {exc}")

    p = Path(project_dir).resolve() if project_dir else None
    project_meta = None
    if p:
        try:
            project_meta = read_project(p)
            add("Project manifest readable", True, project_meta.get("project_name", p.name))
        except Exception as exc:
            add("Project manifest readable", False, f"{type(exc).__name__}: {exc}")
        ccms = next((p / n for n in ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite") if (p / n).exists()), p / "ManualSync_CCMS.sqlite")
        for label, db in (("CCMS SQLite integrity", ccms), ("Workflow SQLite integrity", p / "ManualSync_Workflow.sqlite")):
            res = _sqlite_integrity(db)
            add(label, res.get("status") in {"PASS", "NOT_PRESENT"}, res)
        try:
            health = project_health(p)
            add("Project dashboard readable", True, {k: health.get(k) for k in ("publications", "blocks", "total_jobs", "qa_blocked")})
        except Exception as exc:
            add("Project dashboard readable", False, f"{type(exc).__name__}: {exc}")

    return {
        "support_build": SUPPORT_BUILD,
        "product_version": PRODUCT_VERSION,
        "product_build": PRODUCT_BUILD,
        "generated_at": _now(),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "data_home": str(data_home),
        "project_dir": str(p) if p else "",
        "project": project_meta or {},
        "checks": checks,
        "pass": sum(1 for c in checks if c["status"] == "PASS"),
        "fail": sum(1 for c in checks if c["status"] == "FAIL"),
    }


def create_support_bundle(data_home: str | Path, project_dir: str | Path | None = None, out_dir: str | Path | None = None) -> Path:
    """Create a support ZIP without copying project databases or customer documents."""
    data_home = Path(data_home)
    out = Path(out_dir) if out_dir else data_home / "Support"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = out / f"ManualSync_SUPPORT_{stamp}.zip"
    health = system_health(data_home, project_dir)

    candidate_logs = [
        data_home / "MANUALSYNC_APP.log",
        data_home / "Logs" / "ManualSync_Desktop.log",
        data_home / "Logs" / "ManualSync_Desktop_Error.log",
        data_home / "Logs" / "ManualSync_Packaged_Smoke_Error.log",
    ]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("DIAGNOSTICS.json", json.dumps(health, ensure_ascii=False, indent=2))
        zf.writestr(
            "README.txt",
            "Pacchetto diagnostico ManualSync. Non include database di progetto, PDF, XLIFF, DOCX o altri documenti cliente.\n",
        )
        for log in candidate_logs:
            if log.exists() and log.is_file():
                # Cap each log to the last 2 MB to keep support bundles manageable.
                data = log.read_bytes()
                if len(data) > 2 * 1024 * 1024:
                    data = data[-2 * 1024 * 1024 :]
                zf.writestr(f"LOGS/{log.name}", data)
    return target
