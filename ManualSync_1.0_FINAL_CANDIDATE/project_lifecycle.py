from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from language_codes import canonical_lang, canonical_langs

from ccms_industrial import IndustrialRepository
from workflow_management import WorkflowManager

PROJECT_BUILD = "0.28.0-project-lifecycle-commercial-hardening-20260819"
PROJECT_FILE = "ManualSync_Project.json"


def _safe(v: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(v or "").strip()).strip("._") or "Project"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON in the same directory and atomically replace the destination.

    This prevents a power loss/crash from leaving a truncated project manifest.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def create_project(
    root: str | Path,
    *,
    project_name: str,
    client: str = "",
    product: str = "",
    manual_code: str = "",
    revision: str = "",
    source_lang: str = "en-US",
    target_langs: list[str] | None = None,
    owner: str = "",
) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    project_dir = root / _safe(project_name)

    # Commercial safety: never silently merge a "new" project into an existing
    # directory. A duplicate name must be an explicit user decision.
    if project_dir.exists() and any(project_dir.iterdir()):
        raise FileExistsError(f"Esiste già un progetto con questo nome: {project_name}")
    project_dir.mkdir(parents=True, exist_ok=True)

    db = project_dir / "ManualSync_CCMS.sqlite"
    repo = IndustrialRepository(db)
    repo.close()
    wf = WorkflowManager(project_dir)
    wf.set_project_metadata(project_name, client, owner)
    wf.close()

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "build": PROJECT_BUILD,
        "project_name": project_name,
        "client": client,
        "product": product,
        "manual_code": manual_code,
        "revision": revision,
        "source_lang": canonical_lang(source_lang),
        "target_langs": canonical_langs(target_langs or []),
        "owner": owner,
        "created_at": now,
        "updated_at": now,
    }
    _atomic_write_json(project_dir / PROJECT_FILE, payload)
    return project_dir


def read_project(project_dir: str | Path) -> dict[str, Any]:
    p = Path(project_dir) / PROJECT_FILE
    if not p.exists():
        return {"project_name": Path(project_dir).name, "target_langs": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Manifest progetto ManualSync non valido.")
    return data


def update_project(project_dir: str | Path, **updates) -> dict[str, Any]:
    data = read_project(project_dir)
    clean = {k: v for k, v in updates.items() if v is not None}
    if "source_lang" in clean:
        clean["source_lang"] = canonical_lang(clean["source_lang"])
    if "target_langs" in clean:
        clean["target_langs"] = canonical_langs(clean["target_langs"] or [])
    data.update(clean)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(Path(project_dir) / PROJECT_FILE, data)
    return data


def project_health(project_dir: str | Path) -> dict[str, Any]:
    d = read_project(project_dir)
    base = Path(project_dir)
    wf = WorkflowManager(base)
    try:
        dash = wf.dashboard()
    finally:
        wf.close()
    db = None
    for n in ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"):
        if (base / n).exists():
            db = base / n
            break
    pubs = topics = blocks = 0
    if db:
        repo = IndustrialRepository(db)
        try:
            pubs = int(repo.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0])
            topics = int(repo.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
            blocks = int(repo.conn.execute("SELECT COUNT(*) FROM content_blocks").fetchone()[0])
        finally:
            repo.close()
    return {**d, "publications": pubs, "topics": topics, "blocks": blocks, **dash}
