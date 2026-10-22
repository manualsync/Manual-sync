"""The application shell: which project is open, which manual, and what the
Home says right after the project changes.

Read-only. It lists what is on disk and renders words; opening a project,
creating one or importing a manual stay with the callbacks that already do
it. Every header control is built from here, so the header, the sidebar
and the Home can only ever describe the same project.

Two identifiers never reach the page: a project is offered by its folder
name inside the data root (never an absolute path), and a manual by its
title (its id is only the value behind the title).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import i18n
import project_view as pv
import ux_components as ux
from language_codes import canonical_langs
from project_layout import ProjectLayout

PROJECT_FILE = "ManualSync_Project.json"
DEFAULT_PUBLICATION = "PUB-MAIN"   # what an import creates when nobody chose


def _t(key: str, **params: Any) -> str:
    return i18n.translate(key, None, **params)


def _tn(key: str, n: int, **params: Any) -> str:
    if n == 1 and i18n.has_key(f"{key}.one"):
        return _t(f"{key}.one", n=n, **params)
    return _t(key, n=n, **params)


# ---------------------------------------------------------------- projects
@dataclass(frozen=True)
class ProjectEntry:
    key: str        # folder name inside the data root -- the only id the page sees
    name: str
    client: str

    @property
    def label(self) -> str:
        return f"{self.name} — {self.client}" if self.client else self.name


def is_project_dir(path: Path) -> bool:
    """Same test the app applies before opening anything: a manifest and a
    content database."""
    return (path.is_dir() and (path / PROJECT_FILE).exists()
            and ProjectLayout(path).has_ccms_db())


def _manifest(path: Path) -> dict:
    try:
        data = json.loads((path / PROJECT_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_projects(data_root: str | Path) -> list[ProjectEntry]:
    root = Path(data_root)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if is_project_dir(d):
            meta = _manifest(d)
            out.append(ProjectEntry(d.name, str(meta.get("project_name") or d.name).strip() or d.name,
                                    str(meta.get("client") or "").strip()))
    return sorted(out, key=lambda e: (e.name.casefold(), e.key.casefold()))


def project_key(data_root: str | Path, project_dir: str | Path | None) -> str:
    """The folder name of an open project, or "" when it is not one of ours."""
    if not project_dir:
        return ""
    try:
        p = Path(project_dir).resolve()
        return p.name if p.parent == Path(data_root).resolve() else ""
    except OSError:
        return ""


def resolve_project(data_root: str | Path, key: str | None) -> Path | None:
    """The project a header choice stands for -- only a direct child of the
    data root, never a path the page could have made up."""
    key = str(key or "").strip()
    if not key or key in (".", "..") or re.search(r"[\\/:]", key):
        return None
    root = Path(data_root).resolve()
    p = (root / key).resolve()
    if p.parent != root or not is_project_dir(p):
        return None
    return p


def project_choices(data_root: str | Path, project_dir: str | Path | None) -> tuple[list[tuple[str, str]], str | None]:
    """(choices, value) for the header's project selector. An open project
    outside the data root (restored elsewhere) is still listed, so the
    header never claims nothing is open while something is."""
    entries = list_projects(data_root)
    choices = [(e.label, e.key) for e in entries]
    key = project_key(data_root, project_dir)
    if project_dir and not key:
        meta = _manifest(Path(project_dir))
        key = Path(project_dir).name
        label = str(meta.get("project_name") or key)
        choices.append((label, key))
    return choices, (key or None)


# ---------------------------------------------------------------- manuals
def _read(project_dir: str | Path | None, table: str, sql: str) -> list[tuple]:
    """Rows of one SELECT, or [] when there is no project or no such table."""
    db = ProjectLayout(project_dir).find_ccms_db() if project_dir else None
    if db is None:
        return []
    conn = sqlite3.connect(str(db), timeout=10)   # SELECTs only
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return []
        return list(conn.execute(sql))
    finally:
        conn.close()


def publication_choices(project_dir: str | Path | None) -> list[tuple[str, str]]:
    """The project's manuals by title. With none yet, one entry that says so
    and stands for the id an import will create -- the header never shows a
    bare technical code."""
    rows = _read(project_dir, "publications",
                 "SELECT publication_id, title FROM publications ORDER BY title COLLATE NOCASE")
    if not rows:
        return [(_t("context.publication.none"), DEFAULT_PUBLICATION)]
    return [(str(title or pid), str(pid)) for pid, title in rows]


def pick_publication(choices: list[tuple[str, str]], current: str | None) -> str:
    """Keep the manual the user is on if this project has it; otherwise the
    project's first manual; otherwise the id an import would create."""
    ids = [pid for _label, pid in choices]
    current = str(current or "").strip()
    if current in ids:
        return current
    return ids[0] if ids else (current or DEFAULT_PUBLICATION)


def content_count(project_dir: str | Path | None) -> int:
    rows = _read(project_dir, "content_blocks", "SELECT COUNT(*) FROM content_blocks WHERE archived_at IS NULL")
    return int(rows[0][0]) if rows else 0


# ---------------------------------------------------------------- Home notices
def _name(project_dir: str | Path) -> str:
    meta = _manifest(Path(project_dir))
    return str(meta.get("project_name") or Path(project_dir).name)


def _language_lines(project_dir: str | Path) -> list[str]:
    meta = _manifest(Path(project_dir))
    source = str(meta.get("source_lang") or "").strip()
    targets = canonical_langs(meta.get("target_langs") or [])
    lines = []
    if source:
        lines.append(_t("ux.shell.fact.source", language=pv.language_label(source)))
    if targets:
        lines.append(_t("ux.shell.fact.targets", languages=", ".join(pv.language_label(c) for c in targets)))
    else:
        lines.append(_t("ux.shell.fact.no_targets"))
    return lines


def opened_notice(project_dir: str | Path) -> str:
    return ux.notice(ux.INFO, _t("ux.shell.opened.title", name=_name(project_dir)), _t("ux.shell.opened.body"))


def created_notice(project_dir: str | Path, already_existed: bool) -> str:
    if already_existed:
        return ux.notice(ux.INFO, _t("ux.shell.exists.title", name=_name(project_dir)), _t("ux.shell.exists.body"))
    return ux.notice(ux.SUCCESS, _t("ux.shell.created.title", name=_name(project_dir)), _t("ux.shell.created.body"),
                     items=_language_lines(project_dir), action=_t("ux.shell.created.todo"))


_TECH_MARKS = re.compile(r"[✅⛔ℹ⚠️]|\*\*")


def imported_notice(project_dir: str | Path, import_message: str = "") -> str:
    """After the guided import: what was imported, into which project, with
    which languages, and the one next step. The import's own report stays
    available, folded, as its technical details."""
    n = content_count(project_dir)
    details = _TECH_MARKS.sub("", str(import_message or "")).strip()
    return ux.notice(ux.SUCCESS, _t("ux.shell.imported.title", name=_name(project_dir)),
                     _tn("ux.shell.imported.body", n), items=_language_lines(project_dir),
                     action=_t("ux.shell.imported.todo"), details=details)


def open_error_notice() -> str:
    return ux.notice(ux.WARNING, _t("ux.shell.open_error.title"), _t("ux.shell.open_error.body"))

