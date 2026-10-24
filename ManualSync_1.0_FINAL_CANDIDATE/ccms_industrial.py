from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from authoring_ccms import ContentRepository, COMPONENT_TOKEN_RE
from language_codes import canonical_lang
from project_layout import LEGACY_CCMS_DB_NAME

INDUSTRIAL_BUILD = "0.18.0-ccms-industrial-blocks-workflow-profiles-20260810"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_json(value: str | None, default):
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _norm_set(values: Iterable[str] | None) -> list[str]:
    return sorted({str(v).strip() for v in (values or []) if str(v).strip()})


def utc_instant(value: Any) -> datetime | None:
    """An aware datetime for an ISO-8601 timestamp that carries its offset (every writer uses
    _now()); None for anything else -- empty, naive, malformed. HARDENING-02B counts such a
    date as human work (fail-closed), see IndustrialRepository.migration_draft_human_work."""
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def _not_before(value: Any, start: Any) -> bool:
    """`value` is at or after `start` -- or one of them cannot be read (fail-closed)."""
    a, s = utc_instant(value), utc_instant(start)
    return a is None or s is None or a >= s


class IndustrialRepository(ContentRepository):
    """Industrial CCMS layer on top of the v0.17 repository.

    It deliberately keeps the v0.17 tables and API intact, then adds:
    - block-level structured content;
    - tables and figures as first-class objects;
    - reusable conditional profiles with multi-dimensional rules;
    - block/table/figure translations with source-version freshness;
    - review workflow and immutable publication releases;
    - block/object-level impact tracing.

    This is still not a DITA/DocBook implementation and does not claim full
    interoperability with any commercial CCMS. It establishes the invariants
    needed for a later standards mapping layer.
    """

    def __init__(self, path: str | Path):
        super().__init__(path)
        self._industrial_schema()

    def _industrial_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS content_blocks (
                block_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                block_type TEXT NOT NULL,
                position INTEGER NOT NULL,
                text_content TEXT NOT NULL DEFAULT '',
                object_ref_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Draft',
                version INTEGER NOT NULL DEFAULT 1,
                condition_json TEXT NOT NULL DEFAULT '{}',
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                evidence_status TEXT NOT NULL DEFAULT 'SOURCE_GROUNDED',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(topic_id) REFERENCES topics(topic_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_blocks_topic_pos ON content_blocks(topic_id,position);

            CREATE TABLE IF NOT EXISTS block_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                topic_id TEXT NOT NULL,
                block_type TEXT NOT NULL,
                position INTEGER NOT NULL,
                text_content TEXT NOT NULL,
                object_ref_id TEXT NOT NULL,
                status TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                evidence_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(block_id,version),
                FOREIGN KEY(block_id) REFERENCES content_blocks(block_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS component_translations (
                component_id TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                target_title TEXT NOT NULL DEFAULT '',
                target_body TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Draft',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(component_id,target_lang),
                FOREIGN KEY(component_id) REFERENCES components(component_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS block_translations (
                block_id TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                target_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Draft',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(block_id,target_lang),
                FOREIGN KEY(block_id) REFERENCES content_blocks(block_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS structured_tables (
                table_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                columns_json TEXT NOT NULL DEFAULT '[]',
                rows_json TEXT NOT NULL DEFAULT '[]',
                header_rows INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'Draft',
                version INTEGER NOT NULL DEFAULT 1,
                condition_json TEXT NOT NULL DEFAULT '{}',
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS structured_table_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                caption TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                rows_json TEXT NOT NULL,
                header_rows INTEGER NOT NULL,
                status TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(table_id,version),
                FOREIGN KEY(table_id) REFERENCES structured_tables(table_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS structured_table_translations (
                table_id TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                columns_json TEXT NOT NULL DEFAULT '[]',
                rows_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'Draft',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(table_id,target_lang),
                FOREIGN KEY(table_id) REFERENCES structured_tables(table_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS figures (
                figure_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '',
                alt_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Draft',
                version INTEGER NOT NULL DEFAULT 1,
                condition_json TEXT NOT NULL DEFAULT '{}',
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS figure_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                figure_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                caption TEXT NOT NULL,
                file_path TEXT NOT NULL,
                alt_text TEXT NOT NULL,
                status TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(figure_id,version),
                FOREIGN KEY(figure_id) REFERENCES figures(figure_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS figure_translations (
                figure_id TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                alt_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Draft',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(figure_id,target_lang),
                FOREIGN KEY(figure_id) REFERENCES figures(figure_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS profiles (
                profile_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                dimensions_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- 4E.1: condition dimension/value registry. profiles.dimensions_json
            -- and every condition_json already accepted a bare {field: value}
            -- dict with NO validation that `field`/`value` mean anything real --
            -- this registry is what makes an authored condition/profile
            -- checkable against real, stable ids rather than free-text that
            -- could typo itself into silently matching nothing. dimension_id/
            -- value_id are the stable identifiers stored in condition_json and
            -- profiles.dimensions_json; label/name are display-only and can be
            -- renamed freely without touching any already-authored condition.
            CREATE TABLE IF NOT EXISTS condition_dimensions (
                dimension_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS condition_values (
                value_id TEXT PRIMARY KEY,
                dimension_id TEXT NOT NULL,
                label TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(dimension_id) REFERENCES condition_dimensions(dimension_id)
            );
            CREATE INDEX IF NOT EXISTS ix_condition_values_dim ON condition_values(dimension_id);

            CREATE TABLE IF NOT EXISTS review_tasks (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                assigned_to TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'OPEN',
                decision TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_review_open ON review_tasks(status,assigned_to);

            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS publication_releases (
                release_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                profile_id TEXT NOT NULL DEFAULT '',
                target_lang TEXT NOT NULL DEFAULT '',
                snapshot_json TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                released_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(publication_id) REFERENCES publications(publication_id) ON DELETE CASCADE
            );
            """
        )
        # 4B.1 schema migration: archived_at, same rationale as topics.archived_at
        # (authoring_ccms.py) -- a separate axis from the editorial `status`
        # lifecycle, NULL = active for every pre-existing row.
        block_cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(content_blocks)")}
        if "archived_at" not in block_cols:
            self.conn.execute("ALTER TABLE content_blocks ADD COLUMN archived_at TEXT")
        # 4E.1 schema migration: profiles gain `active`, same reversible
        # archive/deactivate axis as every other 4E.1 registry row (never a
        # destructive delete) -- DEFAULT 1 keeps every pre-existing profile
        # active exactly as before this column existed.
        profile_cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(profiles)")}
        if "active" not in profile_cols:
            self.conn.execute("ALTER TABLE profiles ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        # 4E.3 schema migration: content_blocks/block_versions gain
        # text_format ('plain' | 'rich'). DEFAULT 'plain' is the load-
        # bearing safety property this whole milestone's backward-
        # compatibility guarantee rests on: every row that existed before
        # this column did is retroactively 'plain' with zero data touched,
        # meaning rich_text.render_html() never runs its markup-
        # interpreting parser over pre-existing text (which could contain
        # incidental '*'/'_'/'`'/'[]()' characters never meant as markup,
        # e.g. "M8*1.25", "MAX_PRESSURE", footnote-style "[1]") -- it falls
        # straight to the literal-escape path, byte-for-byte identical to
        # this app's rendering before 4E.3 existed. A block only ever
        # becomes 'rich' the moment a human explicitly re-saves it through
        # the (now rich-aware) Studio text editor -- see AuthoringService.
        # save_block's new `text_format` parameter -- which is an
        # intentional, visible action (the writer is looking at the same
        # box's live preview at that exact moment), never a silent
        # background reinterpretation.
        block_cols2 = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(content_blocks)")}
        if "text_format" not in block_cols2:
            self.conn.execute("ALTER TABLE content_blocks ADD COLUMN text_format TEXT NOT NULL DEFAULT 'plain'")
        block_version_cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(block_versions)")}
        if "text_format" not in block_version_cols:
            self.conn.execute("ALTER TABLE block_versions ADD COLUMN text_format TEXT NOT NULL DEFAULT 'plain'")
        self.conn.commit()

    # ---------- Conditions / profiles ----------
    def create_profile(self, profile_id: str, title: str, dimensions: dict[str, Any]) -> str:
        now = _now()
        self.conn.execute(
            "INSERT INTO profiles(profile_id,title,dimensions_json,created_at,updated_at) VALUES(?,?,?,?,?)",
            (profile_id.strip(), title.strip(), _json(dimensions or {}), now, now),
        )
        self.conn.commit()
        return profile_id

    def update_profile(self, profile_id: str, dimensions: dict[str, Any], *, title: str | None = None) -> None:
        row = self.conn.execute("SELECT title FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        self.conn.execute(
            "UPDATE profiles SET title=?,dimensions_json=?,updated_at=? WHERE profile_id=?",
            (row["title"] if title is None else title.strip(), _json(dimensions or {}), _now(), profile_id),
        )
        self.conn.commit()

    def get_profile(self, profile_id: str | None) -> dict[str, Any]:
        if not profile_id:
            return {}
        row = self.conn.execute("SELECT dimensions_json FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        return _load_json(row["dimensions_json"], {})

    @staticmethod
    def condition_matches(condition: dict[str, Any] | None, dimensions: dict[str, Any] | None) -> bool:
        cond = condition or {}
        dims = dimensions or {}
        if not cond:
            return True
        if "all" in cond:
            return all(IndustrialRepository.condition_matches(c, dims) for c in (cond.get("all") or []))
        if "any" in cond:
            return any(IndustrialRepository.condition_matches(c, dims) for c in (cond.get("any") or []))
        if "not" in cond:
            return not IndustrialRepository.condition_matches(cond.get("not") or {}, dims)

        field = str(cond.get("field", "")).strip()
        op = str(cond.get("op", "eq")).strip().lower()
        expected = cond.get("value")
        actual = dims.get(field)

        if op == "exists":
            return field in dims and actual not in (None, "", [], {})
        if op == "eq":
            return actual == expected
        if op == "neq":
            return actual != expected
        if op == "in":
            return actual in (expected or [])
        if op == "not_in":
            return actual not in (expected or [])
        if op == "contains":
            if isinstance(actual, (list, tuple, set)):
                return expected in actual
            return str(expected) in str(actual or "")
        if op == "contains_any":
            exp = set(expected or [])
            act = set(actual or []) if isinstance(actual, (list, tuple, set)) else {actual}
            return bool(exp & act)
        raise ValueError(f"Operatore condizione non supportato: {op}")

    # ---------- First-class objects ----------
    def create_table(self, table_id: str, columns: list[str], rows: list[list[Any]], *,
                     title: str = "", caption: str = "", header_rows: int = 1,
                     status: str = "Draft", condition: dict[str, Any] | None = None,
                     source_refs: Iterable[str] | None = None) -> str:
        now = _now()
        refs = _norm_set(source_refs)
        payload = (table_id.strip(), title.strip(), caption.strip(), _json(columns), _json(rows), int(header_rows),
                   status, 1, _json(condition or {}), _json(refs), now, now)
        self.conn.execute(
            """INSERT INTO structured_tables(table_id,title,caption,columns_json,rows_json,header_rows,status,version,condition_json,source_refs_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", payload)
        self.conn.execute(
            """INSERT INTO structured_table_versions(table_id,version,title,caption,columns_json,rows_json,header_rows,status,condition_json,source_refs_json,created_at)
               VALUES(?,1,?,?,?,?,?,?,?,?,?)""",
            (table_id.strip(), title.strip(), caption.strip(), _json(columns), _json(rows), int(header_rows), status,
             _json(condition or {}), _json(refs), now),
        )
        self.conn.commit()
        return table_id

    def update_table(self, table_id: str, *, columns: list[str] | None = None, rows: list[list[Any]] | None = None,
                     title: str | None = None, caption: str | None = None, status: str | None = None,
                     condition: dict[str, Any] | None = None, source_refs: Iterable[str] | None = None) -> int:
        r = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
        if not r:
            raise KeyError(table_id)
        v = int(r["version"]) + 1
        vals = {
            "title": r["title"] if title is None else title.strip(),
            "caption": r["caption"] if caption is None else caption.strip(),
            "columns": _load_json(r["columns_json"], []) if columns is None else columns,
            "rows": _load_json(r["rows_json"], []) if rows is None else rows,
            "header_rows": int(r["header_rows"]),
            "status": r["status"] if status is None else status,
            "condition": _load_json(r["condition_json"], {}) if condition is None else condition,
            "refs": _load_json(r["source_refs_json"], []) if source_refs is None else _norm_set(source_refs),
        }
        now = _now()
        self.conn.execute(
            """UPDATE structured_tables SET title=?,caption=?,columns_json=?,rows_json=?,status=?,version=?,condition_json=?,source_refs_json=?,updated_at=? WHERE table_id=?""",
            (vals["title"], vals["caption"], _json(vals["columns"]), _json(vals["rows"]), vals["status"], v,
             _json(vals["condition"]), _json(vals["refs"]), now, table_id),
        )
        self.conn.execute(
            """INSERT INTO structured_table_versions(table_id,version,title,caption,columns_json,rows_json,header_rows,status,condition_json,source_refs_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (table_id, v, vals["title"], vals["caption"], _json(vals["columns"]), _json(vals["rows"]), vals["header_rows"],
             vals["status"], _json(vals["condition"]), _json(vals["refs"]), now),
        )
        self.conn.commit()
        return v

    def set_table_translation(self, table_id: str, target_lang: str, *, caption: str = "",
                              columns: list[str] | None = None, rows: list[list[Any]] | None = None,
                              status: str = "Draft") -> None:
        target_lang = canonical_lang(target_lang)
        r = self.conn.execute("SELECT version,columns_json,rows_json FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
        if not r:
            raise KeyError(table_id)
        self.conn.execute("DELETE FROM structured_table_translations WHERE table_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (table_id, target_lang))
        self.conn.execute(
            """INSERT INTO structured_table_translations(table_id,target_lang,source_version,caption,columns_json,rows_json,status,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(table_id,target_lang) DO UPDATE SET source_version=excluded.source_version,caption=excluded.caption,
               columns_json=excluded.columns_json,rows_json=excluded.rows_json,status=excluded.status,updated_at=excluded.updated_at""",
            (table_id, target_lang, int(r["version"]), caption.strip(), _json(columns if columns is not None else _load_json(r["columns_json"], [])),
             _json(rows if rows is not None else _load_json(r["rows_json"], [])), status, _now()),
        )
        self._commit()

    def create_figure(self, figure_id: str, *, title: str = "", caption: str = "", file_path: str = "",
                      alt_text: str = "", status: str = "Draft", condition: dict[str, Any] | None = None,
                      source_refs: Iterable[str] | None = None) -> str:
        now = _now()
        refs = _norm_set(source_refs)
        self.conn.execute(
            """INSERT INTO figures(figure_id,title,caption,file_path,alt_text,status,version,condition_json,source_refs_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
            (figure_id.strip(), title.strip(), caption.strip(), str(file_path), alt_text.strip(), status, _json(condition or {}), _json(refs), now, now),
        )
        self.conn.execute(
            """INSERT INTO figure_versions(figure_id,version,title,caption,file_path,alt_text,status,condition_json,source_refs_json,created_at)
               VALUES(?,1,?,?,?,?,?,?,?,?)""",
            (figure_id.strip(), title.strip(), caption.strip(), str(file_path), alt_text.strip(), status, _json(condition or {}), _json(refs), now),
        )
        self.conn.commit()
        return figure_id

    def update_figure(self, figure_id: str, *, title: str | None = None, caption: str | None = None,
                      file_path: str | None = None, alt_text: str | None = None, status: str | None = None,
                      condition: dict[str, Any] | None = None, source_refs: Iterable[str] | None = None) -> int:
        r = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
        if not r:
            raise KeyError(figure_id)
        v = int(r["version"]) + 1
        vals = {
            "title": r["title"] if title is None else title.strip(),
            "caption": r["caption"] if caption is None else caption.strip(),
            "file_path": r["file_path"] if file_path is None else str(file_path),
            "alt_text": r["alt_text"] if alt_text is None else alt_text.strip(),
            "status": r["status"] if status is None else status,
            "condition": _load_json(r["condition_json"], {}) if condition is None else condition,
            "refs": _load_json(r["source_refs_json"], []) if source_refs is None else _norm_set(source_refs),
        }
        now = _now()
        self.conn.execute(
            """UPDATE figures SET title=?,caption=?,file_path=?,alt_text=?,status=?,version=?,condition_json=?,source_refs_json=?,updated_at=? WHERE figure_id=?""",
            (vals["title"], vals["caption"], vals["file_path"], vals["alt_text"], vals["status"], v, _json(vals["condition"]), _json(vals["refs"]), now, figure_id),
        )
        self.conn.execute(
            """INSERT INTO figure_versions(figure_id,version,title,caption,file_path,alt_text,status,condition_json,source_refs_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (figure_id, v, vals["title"], vals["caption"], vals["file_path"], vals["alt_text"], vals["status"], _json(vals["condition"]), _json(vals["refs"]), now),
        )
        self.conn.commit()
        return v

    def set_figure_translation(self, figure_id: str, target_lang: str, *, caption: str = "", alt_text: str = "", status: str = "Draft") -> None:
        target_lang = canonical_lang(target_lang)
        r = self.conn.execute("SELECT version FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
        if not r:
            raise KeyError(figure_id)
        self.conn.execute("DELETE FROM figure_translations WHERE figure_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (figure_id, target_lang))
        self.conn.execute(
            """INSERT INTO figure_translations(figure_id,target_lang,source_version,caption,alt_text,status,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(figure_id,target_lang) DO UPDATE SET source_version=excluded.source_version,caption=excluded.caption,
               alt_text=excluded.alt_text,status=excluded.status,updated_at=excluded.updated_at""",
            (figure_id, target_lang, int(r["version"]), caption.strip(), alt_text.strip(), status, _now()),
        )
        self._commit()

    # ---------- Blocks ----------
    def add_block(self, block_id: str, topic_id: str, block_type: str, *, position: int,
                  text: str = "", object_ref_id: str = "", status: str = "Draft",
                  condition: dict[str, Any] | None = None, source_refs: Iterable[str] | None = None,
                  evidence_status: str = "SOURCE_GROUNDED", text_format: str = "plain") -> str:
        if not self.conn.execute("SELECT 1 FROM topics WHERE topic_id=?", (topic_id,)).fetchone():
            raise KeyError(topic_id)
        bt = block_type.strip().lower()
        if bt not in {"paragraph", "warning", "note", "list_item", "table", "figure", "component_ref", "code"}:
            raise ValueError(f"block_type non supportato: {bt}")
        if bt == "table" and not self.conn.execute("SELECT 1 FROM structured_tables WHERE table_id=?", (object_ref_id,)).fetchone():
            raise KeyError(object_ref_id)
        if bt == "figure" and not self.conn.execute("SELECT 1 FROM figures WHERE figure_id=?", (object_ref_id,)).fetchone():
            raise KeyError(object_ref_id)
        if bt == "component_ref" and not self.conn.execute("SELECT 1 FROM components WHERE component_id=?", (object_ref_id,)).fetchone():
            raise KeyError(object_ref_id)
        # 4E.3: every NEW block starts 'plain' by default (text_format
        # param exists mainly for the manual-fixture builder / tests that
        # want to seed a block already in 'rich' form) -- the normal
        # Studio "create block" path never passes it, matching
        # create_block_in_topic's own "format flips to rich only on an
        # explicit rich-aware save" rule.
        tf = (text_format or "plain").strip().lower()
        if tf not in ("plain", "rich"):
            tf = "plain"
        refs = _norm_set(source_refs)
        now = _now()
        values = (block_id.strip(), topic_id, bt, int(position), text.strip(), object_ref_id.strip(), status, 1,
                  _json(condition or {}), _json(refs), evidence_status, now, now, tf)
        self.conn.execute(
            """INSERT INTO content_blocks(block_id,topic_id,block_type,position,text_content,object_ref_id,status,version,condition_json,source_refs_json,evidence_status,created_at,updated_at,text_format)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        self.conn.execute(
            """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at,text_format)
               VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?)""",
            (block_id.strip(), topic_id, bt, int(position), text.strip(), object_ref_id.strip(), status,
             _json(condition or {}), _json(refs), evidence_status, now, tf),
        )
        self.conn.commit()
        return block_id

    def update_block(self, block_id: str, *, text: str | None = None, object_ref_id: str | None = None,
                     status: str | None = None, condition: dict[str, Any] | None = None,
                     source_refs: Iterable[str] | None = None, evidence_status: str | None = None) -> int:
        r = self.conn.execute("SELECT * FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
        if not r:
            raise KeyError(block_id)
        v = int(r["version"]) + 1
        vals = {
            "text": r["text_content"] if text is None else text.strip(),
            "ref": r["object_ref_id"] if object_ref_id is None else object_ref_id.strip(),
            "status": r["status"] if status is None else status,
            "condition": _load_json(r["condition_json"], {}) if condition is None else condition,
            "refs": _load_json(r["source_refs_json"], []) if source_refs is None else _norm_set(source_refs),
            "evidence": r["evidence_status"] if evidence_status is None else evidence_status,
        }
        now = _now()
        self.conn.execute(
            """UPDATE content_blocks SET text_content=?,object_ref_id=?,status=?,version=?,condition_json=?,source_refs_json=?,evidence_status=?,updated_at=? WHERE block_id=?""",
            (vals["text"], vals["ref"], vals["status"], v, _json(vals["condition"]), _json(vals["refs"]), vals["evidence"], now, block_id),
        )
        self.conn.execute(
            """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (block_id, v, r["topic_id"], r["block_type"], int(r["position"]), vals["text"], vals["ref"], vals["status"],
             _json(vals["condition"]), _json(vals["refs"]), vals["evidence"], now),
        )
        self.conn.commit()
        return v

    def set_component_translation(self, component_id: str, target_lang: str, target_body: str, *, target_title: str = "", status: str = "Draft") -> None:
        target_lang = canonical_lang(target_lang)
        comp = self.conn.execute("SELECT version,title FROM components WHERE component_id=?", (component_id,)).fetchone()
        if not comp:
            raise KeyError(component_id)
        self.conn.execute("DELETE FROM component_translations WHERE component_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (component_id, target_lang))
        self.conn.execute(
            """INSERT INTO component_translations(component_id,target_lang,source_version,target_title,target_body,status,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(component_id,target_lang) DO UPDATE SET source_version=excluded.source_version,target_title=excluded.target_title,target_body=excluded.target_body,status=excluded.status,updated_at=excluded.updated_at""",
            (component_id, target_lang, int(comp["version"]), target_title or comp["title"], target_body.strip(), status, _now()),
        )
        self._commit()

    def set_block_translation(self, block_id: str, target_lang: str, target_text: str, *, status: str = "Draft") -> None:
        target_lang = canonical_lang(target_lang)
        r = self.conn.execute("SELECT version FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
        if not r:
            raise KeyError(block_id)
        self.conn.execute("DELETE FROM block_translations WHERE block_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (block_id, target_lang))
        self.conn.execute(
            """INSERT INTO block_translations(block_id,target_lang,source_version,target_text,status,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(block_id,target_lang) DO UPDATE SET source_version=excluded.source_version,target_text=excluded.target_text,
               status=excluded.status,updated_at=excluded.updated_at""",
            (block_id, target_lang, int(r["version"]), target_text.strip(), status, _now()),
        )
        self._commit()

    # ---------- Translation round-trip audit ----------
    # The table is created on first use, as it always was, not by _industrial_schema: a
    # database that never committed a round trip has none.
    def record_translation_roundtrip_audit(self, *, build: str, source_kind: str, source_id: str, source_version: int,
                                           source_lang: str, target_lang: str, package_sha256: str,
                                           returned_xliff_sha256: str, reviewer: str, tm_path: str,
                                           tm_entries_added: int, ccms_commits_json: str, committed_at: str,
                                           job_id: str = "") -> None:
        """One row of translation_roundtrip_audit, NOT committed: it belongs to the caller's
        transaction (the round-trip commit writes it together with its CCMS translations)."""
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS translation_roundtrip_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            build TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_version INTEGER NOT NULL,
            source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            package_sha256 TEXT NOT NULL,
            returned_xliff_sha256 TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            human_approved INTEGER NOT NULL,
            tm_path TEXT NOT NULL,
            tm_entries_added INTEGER NOT NULL,
            ccms_commits_json TEXT NOT NULL,
            committed_at TEXT NOT NULL
        )
    """)
        if "job_id" not in self._roundtrip_audit_columns():
            # CP5.4c: the translation job a row belongs to, so that an approval interrupted after
            # its commit is completed instead of committed a second time. Older rows carry ''.
            self.conn.execute("ALTER TABLE translation_roundtrip_audit ADD COLUMN job_id TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            """INSERT INTO translation_roundtrip_audit(build,source_kind,source_id,source_version,source_lang,target_lang,package_sha256,returned_xliff_sha256,reviewer,human_approved,tm_path,tm_entries_added,ccms_commits_json,committed_at,job_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (build, source_kind, source_id, int(source_version), source_lang, target_lang, package_sha256,
             returned_xliff_sha256, reviewer, 1, tm_path, int(tm_entries_added), ccms_commits_json, committed_at, str(job_id or "")),
        )

    def translation_roundtrip_audit_for_job(self, job_id: str, package_sha256: str, returned_xliff_sha256: str) -> dict[str, Any] | None:
        """The audit row of the round trip this job already committed -- same package, same
        returned XLIFF -- or None."""
        if not job_id or "job_id" not in self._roundtrip_audit_columns():
            return None
        row = self.conn.execute(
            "SELECT * FROM translation_roundtrip_audit WHERE job_id=? AND package_sha256=? AND returned_xliff_sha256=? ORDER BY id LIMIT 1",
            (str(job_id), package_sha256, returned_xliff_sha256),
        ).fetchone()
        return dict(row) if row else None

    def _roundtrip_audit_columns(self) -> set[str]:
        return {str(r[1]) for r in self.conn.execute("PRAGMA table_info(translation_roundtrip_audit)")}

    def blocks_table(self, topic_id: str | None = None, *, include_archived: bool = False) -> pd.DataFrame:
        sql = "SELECT * FROM content_blocks"
        clauses: list[str] = []
        params: list[Any] = []
        if topic_id:
            clauses.append("topic_id=?")
            params.append(topic_id)
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY topic_id,position,block_id"
        params = tuple(params)
        rows = []
        for r in self.conn.execute(sql, params):
            rows.append({
                "block_id": r["block_id"], "topic_id": r["topic_id"], "block_type": r["block_type"],
                "position": r["position"], "status": r["status"], "version": r["version"],
                "text": r["text_content"], "object_ref_id": r["object_ref_id"],
                "condition": r["condition_json"], "source_refs": " | ".join(_load_json(r["source_refs_json"], [])),
                "evidence_status": r["evidence_status"],
            })
        return pd.DataFrame(rows)

    # ---------- Assembly ----------
    def _legacy_variant_dimensions(self, variant: str | None, dimensions: dict[str, Any]) -> bool:
        # Keep v0.17 include/exclude behavior as a first guard for legacy topic conditions.
        return True

    def _topic_allowed_with_profile(self, topic: sqlite3.Row, dimensions: dict[str, Any]) -> bool:
        include = set(_load_json(topic["include_variants_json"], []))
        exclude = set(_load_json(topic["exclude_variants_json"], []))
        product = str(dimensions.get("product", "") or "")
        if include and product not in include:
            return False
        if product and product in exclude:
            return False
        return True


    @staticmethod
    def _translation_required_values(*values: Any) -> bool:
        """Return True only when source values contain linguistic material.

        Pure numbers, symbols, one-letter labels and alphanumeric technical codes are
        treated as source-invariant. This keeps release readiness aligned with the CAT
        extraction policy and avoids false missing-translation blockers for content
        that has nothing meaningful to localize.
        """
        flat: list[str] = []
        def collect(v: Any) -> None:
            if v is None:
                return
            if isinstance(v, dict):
                for x in v.values(): collect(x)
            elif isinstance(v, (list, tuple)):
                for x in v: collect(x)
            else:
                text = str(v).strip()
                if text: flat.append(text)
        for value in values:
            collect(value)
        if not flat:
            return False
        for token in re.findall(r"[^\W_]+", " ".join(flat), flags=re.UNICODE):
            if token.isalpha() and len(token) >= 2:
                return True
        return False

    def assemble_structured_publication(self, publication_id: str, *, profile_id: str | None = None,
                                        dimensions: dict[str, Any] | None = None,
                                        target_lang: str | None = None) -> tuple[pd.DataFrame, dict]:
        target_lang = canonical_lang(target_lang) if target_lang else None
        pub = self.conn.execute("SELECT * FROM publications WHERE publication_id=?", (publication_id,)).fetchone()
        if not pub:
            raise KeyError(publication_id)
        dims = dict(self.get_profile(profile_id)) if profile_id else {}
        dims.update(dimensions or {})
        out: list[dict[str, Any]] = []
        stale = missing = excluded = 0

        for link in self.conn.execute("SELECT * FROM publication_topics WHERE publication_id=? ORDER BY position", (publication_id,)):
            topic = self.conn.execute("SELECT * FROM topics WHERE topic_id=?", (link["topic_id"],)).fetchone()
            if topic["archived_at"]:
                # 4B.1: an archived topic is excluded from assembly (and therefore
                # from readiness/release/publishing) exactly like a profile-excluded
                # one -- never a hard delete, always reversible via restore_topic().
                excluded += 1
                continue
            if not self._topic_allowed_with_profile(topic, dims):
                excluded += 1
                continue
            # 4E.1: additive check alongside the legacy product-only allow/
            # deny list above -- a topic must satisfy BOTH the legacy check
            # (untouched, still governs old include/exclude_variants_json
            # content) and any new dimension-based condition_json assigned
            # through the Studio's "Varianti" panel. Every pre-existing
            # topic's condition_json defaults to '{}', which condition_matches
            # treats as "no condition -> always included", so this is a
            # strict no-op for any topic never touched by 4E.1 authoring.
            if not self.condition_matches(_load_json(topic["condition_json"], {}), dims):
                excluded += 1
                continue

            topic_title = str(topic["title"] or "")
            topic_title_translation_status = "SOURCE"
            topic_tr = None
            if target_lang and target_lang != canonical_lang(topic["source_lang"]):
                topic_tr = self.conn.execute("SELECT * FROM topic_translations WHERE topic_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (topic["topic_id"], target_lang)).fetchone()
                if topic_tr and str(topic_tr["target_title"] or "").strip():
                    topic_title = str(topic_tr["target_title"]).strip()
                    if int(topic_tr["source_version"]) != int(topic["version"]):
                        stale += 1
                        topic_title_translation_status = "STALE"
                    else:
                        topic_title_translation_status = str(topic_tr["status"] or "Draft")
                elif self._translation_required_values(topic["title"]):
                    missing += 1
                    topic_title_translation_status = "MISSING"
                else:
                    topic_title_translation_status = "NOT_REQUIRED"

            blocks = list(self.conn.execute(
                "SELECT * FROM content_blocks WHERE topic_id=? AND archived_at IS NULL ORDER BY position,block_id",
                (topic["topic_id"],)))
            if not blocks:
                # 4B.1: distinguish "this topic never had any blocks" (the true
                # legacy-fallback case below) from "every block was archived"
                # (must NOT resurrect the old topic.body text as a substitute --
                # an archived block must stay excluded from assembly, not
                # reappear through the legacy fallback path).
                has_any_block_ever = self.conn.execute(
                    "SELECT 1 FROM content_blocks WHERE topic_id=? LIMIT 1", (topic["topic_id"],)
                ).fetchone()
                if has_any_block_ever:
                    excluded += 1
                    continue
                # Backward-compatible fallback: a legacy topic becomes one paragraph block.
                legacy_content = self.resolve_components(topic["body"])
                legacy_status = "SOURCE"
                if target_lang and target_lang != canonical_lang(topic["source_lang"]):
                    if topic_tr and str(topic_tr["target_body"] or "").strip():
                        legacy_content = self.resolve_components(str(topic_tr["target_body"]))
                        if int(topic_tr["source_version"]) != int(topic["version"]):
                            stale += 1; legacy_status = "STALE"
                        else:
                            legacy_status = str(topic_tr["status"] or "Draft")
                    elif self._translation_required_values(topic["body"]):
                        missing += 1; legacy_status = "MISSING"
                    else:
                        legacy_status = "NOT_REQUIRED"
                out.append({
                    "publication_position": int(link["position"]), "topic_id": topic["topic_id"], "topic_title": topic_title,
                    "topic_title_translation_status": topic_title_translation_status,
                    "block_id": f"{topic['topic_id']}::legacy", "block_type": "paragraph", "block_position": 1,
                    "status": topic["status"], "version": int(topic["version"]), "content": legacy_content,
                    "object_ref_id": "", "translation_status": legacy_status, "source_refs": " | ".join(_load_json(topic["source_refs_json"], [])),
                    "evidence_status": topic["evidence_status"], "condition": "{}", "profile_id": profile_id or "", "dimensions": _json(dims),
                })
                continue

            for b in blocks:
                condition = _load_json(b["condition_json"], {})
                if not self.condition_matches(condition, dims):
                    excluded += 1
                    continue
                content = b["text_content"]
                translation_status = "SOURCE"
                if target_lang and target_lang != canonical_lang(topic["source_lang"]) and b["block_type"] not in {"table", "figure", "component_ref"}:
                    tr = self.conn.execute("SELECT * FROM block_translations WHERE block_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (b["block_id"], target_lang)).fetchone()
                    if tr:
                        content = tr["target_text"]
                        if int(tr["source_version"]) != int(b["version"]):
                            stale += 1
                            translation_status = "STALE"
                        else:
                            translation_status = tr["status"]
                    else:
                        if self._translation_required_values(b["text_content"]):
                            missing += 1
                            translation_status = "MISSING"
                        else:
                            translation_status = "NOT_REQUIRED"

                if b["block_type"] == "component_ref":
                    comp = self.conn.execute("SELECT * FROM components WHERE component_id=?", (b["object_ref_id"],)).fetchone()
                    content = comp["body"] if comp else f"[MISSING COMPONENT: {b['object_ref_id']}]"
                    if comp and target_lang and target_lang != canonical_lang(topic["source_lang"]):
                        tr = self.conn.execute("SELECT * FROM component_translations WHERE component_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (comp["component_id"], target_lang)).fetchone()
                        if tr:
                            content = tr["target_body"]
                            if int(tr["source_version"]) != int(comp["version"]):
                                stale += 1; translation_status = "STALE"
                            else:
                                translation_status = tr["status"]
                        else:
                            if self._translation_required_values(comp["title"], comp["body"]):
                                missing += 1; translation_status = "MISSING"
                            else:
                                translation_status = "NOT_REQUIRED"
                elif b["block_type"] == "table":
                    tbl = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (b["object_ref_id"],)).fetchone()
                    if not tbl:
                        content = {"error": f"MISSING TABLE {b['object_ref_id']}"}
                    else:
                        content = {
                            "table_id": tbl["table_id"], "title": tbl["title"], "caption": tbl["caption"],
                            "columns": _load_json(tbl["columns_json"], []), "rows": _load_json(tbl["rows_json"], []),
                            "header_rows": int(tbl["header_rows"]), "version": int(tbl["version"]), "status": tbl["status"],
                        }
                        if target_lang and target_lang != canonical_lang(topic["source_lang"]):
                            tr = self.conn.execute("SELECT * FROM structured_table_translations WHERE table_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (tbl["table_id"], target_lang)).fetchone()
                            if tr:
                                content.update({"caption": tr["caption"] or tbl["caption"], "columns": _load_json(tr["columns_json"], []), "rows": _load_json(tr["rows_json"], [])})
                                if int(tr["source_version"]) != int(tbl["version"]):
                                    stale += 1; translation_status = "STALE"
                                else:
                                    translation_status = tr["status"]
                            else:
                                if self._translation_required_values(tbl["title"], tbl["caption"], _load_json(tbl["columns_json"], []), _load_json(tbl["rows_json"], [])):
                                    missing += 1; translation_status = "MISSING"
                                else:
                                    translation_status = "NOT_REQUIRED"
                elif b["block_type"] == "figure":
                    fig = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (b["object_ref_id"],)).fetchone()
                    if not fig:
                        content = {"error": f"MISSING FIGURE {b['object_ref_id']}"}
                    else:
                        content = {"figure_id": fig["figure_id"], "title": fig["title"], "caption": fig["caption"], "file_path": fig["file_path"], "alt_text": fig["alt_text"], "version": int(fig["version"]), "status": fig["status"]}
                        if target_lang and target_lang != canonical_lang(topic["source_lang"]):
                            tr = self.conn.execute("SELECT * FROM figure_translations WHERE figure_id=? AND REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (fig["figure_id"], target_lang)).fetchone()
                            if tr:
                                content.update({"caption": tr["caption"] or fig["caption"], "alt_text": tr["alt_text"] or fig["alt_text"]})
                                if int(tr["source_version"]) != int(fig["version"]):
                                    stale += 1; translation_status = "STALE"
                                else:
                                    translation_status = tr["status"]
                            else:
                                if self._translation_required_values(fig["title"], fig["caption"], fig["alt_text"]):
                                    missing += 1; translation_status = "MISSING"
                                else:
                                    translation_status = "NOT_REQUIRED"

                out.append({
                    "publication_position": int(link["position"]), "topic_id": topic["topic_id"], "topic_title": topic_title,
                    "topic_title_translation_status": topic_title_translation_status,
                    "block_id": b["block_id"], "block_type": b["block_type"], "block_position": int(b["position"]),
                    "status": b["status"], "version": int(b["version"]), "content": content, "object_ref_id": b["object_ref_id"],
                    "translation_status": translation_status, "source_refs": " | ".join(_load_json(b["source_refs_json"], [])),
                    "evidence_status": b["evidence_status"], "condition": b["condition_json"], "profile_id": profile_id or "", "dimensions": _json(dims),
                })

        frame = pd.DataFrame(out)
        summary = {
            "publication_id": publication_id, "title": pub["title"], "profile_id": profile_id or "",
            "target_lang": target_lang or pub["source_lang"], "blocks": len(frame), "stale_translations": stale,
            "missing_translations": missing, "excluded_by_conditions": excluded, "dimensions": dims,
        }
        return frame, summary

    # ---------- Workflow ----------
    def _status_table(self, object_type: str) -> tuple[str, str]:
        mapping = {
            "topic": ("topics", "topic_id"),
            "block": ("content_blocks", "block_id"),
            "table": ("structured_tables", "table_id"),
            "figure": ("figures", "figure_id"),
        }
        if object_type not in mapping:
            raise ValueError("object_type deve essere topic, block, table o figure")
        return mapping[object_type]

    def request_review(self, object_type: str, object_id: str, *, requested_by: str, assigned_to: str = "", comment: str = "") -> int:
        table, key = self._status_table(object_type)
        if not self.conn.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (object_id,)).fetchone():
            raise KeyError(object_id)
        self.conn.execute(f"UPDATE {table} SET status=? WHERE {key}=?", ("In Review", object_id))
        cur = self.conn.execute(
            "INSERT INTO review_tasks(object_type,object_id,requested_by,assigned_to,status,comment,created_at) VALUES(?,?,?,?, 'OPEN', ?,?)",
            (object_type, object_id, requested_by, assigned_to, comment, _now()),
        )
        self._audit(object_type, object_id, "REQUEST_REVIEW", requested_by, {"assigned_to": assigned_to, "comment": comment})
        self.conn.commit()
        return int(cur.lastrowid)

    def decide_review(self, review_id: int, *, decision: str, actor: str, comment: str = "") -> str:
        r = self.conn.execute("SELECT * FROM review_tasks WHERE review_id=?", (int(review_id),)).fetchone()
        if not r:
            raise KeyError(review_id)
        if r["status"] != "OPEN":
            raise ValueError("Review già chiusa")
        d = decision.strip().upper()
        if d not in {"APPROVE", "REJECT"}:
            raise ValueError("decision deve essere APPROVE o REJECT")
        new_status = "Approved" if d == "APPROVE" else "Draft"
        table, key = self._status_table(r["object_type"])
        self.conn.execute(f"UPDATE {table} SET status=? WHERE {key}=?", (new_status, r["object_id"]))
        self.conn.execute(
            "UPDATE review_tasks SET status='CLOSED',decision=?,comment=?,decided_at=? WHERE review_id=?",
            (d, comment, _now(), int(review_id)),
        )
        self._audit(r["object_type"], r["object_id"], f"REVIEW_{d}", actor, {"review_id": review_id, "comment": comment})
        self.conn.commit()
        return new_status

    def review_queue(self, *, status: str = "OPEN") -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM review_tasks WHERE status=? ORDER BY created_at,review_id", self.conn, params=(status,))

    def _audit(self, object_type: str, object_id: str, action: str, actor: str, detail: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_events(object_type,object_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (object_type, object_id, action, actor, _json(detail or {}), _now()),
        )

    def audit_event(self, object_type: str, object_id: str, action: str, actor: str, detail: dict[str, Any] | None = None) -> None:
        """Public, supported entry point for appending an audit_events row.

        Same primitive as the internal _audit() (no parallel audit log) --
        exposed for callers outside this class, such as ComplianceService,
        that need to record actor/timestamp history on this repository's own
        audit trail without depending on a name-mangled-by-convention
        method. Does not commit, exactly like every other write here --
        callers commit as part of their own transaction.
        """
        self._audit(object_type, object_id, action, actor, detail)

    def validate_publication_for_release(self, publication_id: str, *, profile_id: str | None = None,
                                         dimensions: dict[str, Any] | None = None, target_lang: str | None = None,
                                         strict_approval: bool = True) -> dict[str, Any]:
        frame, summary = self.assemble_structured_publication(publication_id, profile_id=profile_id, dimensions=dimensions, target_lang=target_lang)
        issues = []
        if summary["stale_translations"]:
            issues.append(f"{summary['stale_translations']} traduzioni stale")
        if target_lang and summary["missing_translations"]:
            issues.append(f"{summary['missing_translations']} traduzioni mancanti")
        if strict_approval and not frame.empty:
            bad = frame[~frame["status"].astype(str).isin(["Approved", "Published"])]
            if len(bad):
                issues.append(f"{len(bad)} blocchi non approvati")
        missing_refs = 0
        for _, r in frame.iterrows():
            c = r["content"]
            if isinstance(c, dict) and "error" in c:
                missing_refs += 1
        if missing_refs:
            issues.append(f"{missing_refs} riferimenti oggetto mancanti")
        return {"PASS": not issues, "issues": issues, "summary": summary, "frame": frame}

    def release_publication(self, publication_id: str, *, release_id: str, released_by: str,
                            profile_id: str | None = None, dimensions: dict[str, Any] | None = None,
                            target_lang: str | None = None, commit: bool = True,
                            compliance_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """3.5C: this is the single lowest-level primitive that actually
        writes a publication_releases row, so it is also the last line of
        defense against a release that silently ignores an active
        Compliance scope -- the gate here cannot be skipped by calling this
        method directly instead of going through release_manager.create_release_bundle().

        commit=True means a standalone call (release_manager.py's own
        multi-language release always uses commit=False, see below), so
        there is no in-flight caller transaction this could disturb: it is
        safe to freshly construct a ComplianceService right here and
        re-derive+gate Compliance readiness itself, ignoring whatever
        compliance_snapshot argument was passed -- a direct caller cannot
        forge readiness by passing a fake "ready" snapshot.

        commit=False is reserved for release_manager.create_release_bundle's
        own atomic multi-language loop -- verified to be the ONLY real call
        site of commit=False in the codebase (grep audit, 3.5C integrity
        pass). ComplianceService._ensure_schema() calls
        conn.executescript(), which implicitly COMMITs any pending
        transaction -- constructing a fresh ComplianceService here would
        silently break that caller's atomicity (and, since create_release_bundle
        now opens its snapshot transaction with BEGIN IMMEDIATE specifically
        to close the Compliance TOCTOU window, would silently release that
        lock too). So a batched caller MUST pass its own already-gated
        compliance_snapshot -- computed once, under that same lock, by
        create_release_bundle immediately after BEGIN IMMEDIATE, via the
        same canonical compliance_readiness_for_publication(). This method
        refuses to proceed without a well-formed one rather than silently
        treating a missing/malformed snapshot as "not applicable"; that is
        the minimal API contract guard for this internal primitive, not a
        promise that commit=False is safe to call from anywhere else."""
        result = self.validate_publication_for_release(publication_id, profile_id=profile_id, dimensions=dimensions, target_lang=target_lang)
        if not result["PASS"]:
            raise ValueError("Release bloccata: " + "; ".join(result["issues"]))

        if commit:
            from compliance_intelligence import ComplianceService
            compliance_snapshot = ComplianceService(self).compliance_readiness_for_publication(publication_id)
        elif compliance_snapshot is None:
            raise RuntimeError(
                "release_publication(commit=False) richiede un compliance_snapshot già calcolato dal chiamante "
                "(release_manager.create_release_bundle) -- il gate Compliance non può essere saltato."
            )
        elif not ({"compliance_applicable", "compliance_ready"} <= compliance_snapshot.keys()):
            raise RuntimeError(
                "compliance_snapshot passato a release_publication(commit=False) non ha la forma attesa "
                "(mancano 'compliance_applicable'/'compliance_ready') -- deve provenire, non modificato, da "
                "ComplianceService.compliance_readiness_for_publication()."
            )
        if compliance_snapshot.get("compliance_applicable") and not compliance_snapshot.get("compliance_ready"):
            items = compliance_snapshot.get("blocking_items") or []
            preview = "; ".join(f"{b['scope_id']}/{b['requirement_id']} ({b['status']})" for b in items[:10])
            raise ValueError(
                f"Release bloccata: Compliance non pronta per {publication_id} "
                f"({len(items)} requisiti bloccanti su {compliance_snapshot['active_scope_count']} scope attivi). {preview}"
            )

        frame = result["frame"].copy()
        serializable_rows = []
        for _, r in frame.iterrows():
            item = r.to_dict()
            serializable_rows.append(item)
        snapshot = {"summary": result["summary"], "rows": serializable_rows, "compliance": compliance_snapshot}
        snapshot_json = _json(snapshot)
        sha = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO publication_releases(release_id,publication_id,profile_id,target_lang,snapshot_json,snapshot_sha256,released_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (release_id, publication_id, profile_id or "", target_lang or "", snapshot_json, sha, released_by, _now()),
        )
        self._audit("publication", publication_id, "RELEASE", released_by, {"release_id": release_id, "sha256": sha})
        if commit:
            self.conn.commit()
        return {"release_id": release_id, "sha256": sha, "blocks": len(frame)}

    def verify_release_snapshot(self, release_id: str) -> dict[str, Any]:
        """3.5C: the real, reusable integrity check for one historical
        release row -- recomputes sha256 over the stored snapshot_json and
        compares it to the stored snapshot_sha256, the exact mechanism
        release_publication() itself uses at write time. Makes "the release
        snapshot is verifiable" a callable product primitive, not something
        only demonstrated ad hoc by test code. Detects any alteration or
        corruption of the stored row; does not (and, without a PKI, cannot)
        prove who wrote it -- that is out of scope."""
        row = self.conn.execute(
            "SELECT release_id, snapshot_json, snapshot_sha256 FROM publication_releases WHERE release_id=?",
            (release_id,),
        ).fetchone()
        if not row:
            raise KeyError(release_id)
        actual = hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest()
        return {
            "release_id": release_id,
            "verified": actual == row["snapshot_sha256"],
            "expected_sha256": row["snapshot_sha256"],
            "actual_sha256": actual,
        }

    # ---------- Extended impact ----------
    def impact_map_industrial(self, changed_id: str, *, kind: str) -> pd.DataFrame:
        topic_ids: set[str] = set()
        if kind == "topic":
            topic_ids.add(changed_id)
        elif kind == "block":
            r = self.conn.execute("SELECT topic_id FROM content_blocks WHERE block_id=?", (changed_id,)).fetchone()
            if r: topic_ids.add(r["topic_id"])
        elif kind in {"table", "figure", "component"}:
            ref_type = {"table": "table", "figure": "figure", "component": "component_ref"}[kind]
            for r in self.conn.execute("SELECT topic_id FROM content_blocks WHERE block_type=? AND object_ref_id=?", (ref_type, changed_id)):
                topic_ids.add(r["topic_id"])
            if kind == "component":
                token = f"{{{{component:{changed_id}}}}}"
                for r in self.conn.execute("SELECT topic_id,body FROM topics"):
                    if token in (r["body"] or ""):
                        topic_ids.add(r["topic_id"])
        else:
            raise ValueError("kind non supportato")

        rows = []
        for tid in sorted(topic_ids):
            topic = self.conn.execute("SELECT title,version,status FROM topics WHERE topic_id=?", (tid,)).fetchone()
            for link in self.conn.execute("SELECT publication_id,position FROM publication_topics WHERE topic_id=?", (tid,)):
                pub = self.conn.execute("SELECT title FROM publications WHERE publication_id=?", (link["publication_id"],)).fetchone()
                translations = list(self.conn.execute("SELECT target_lang,source_version,status FROM topic_translations WHERE topic_id=?", (tid,)))
                rows.append({
                    "changed_kind": kind, "changed_id": changed_id, "topic_id": tid, "topic_title": topic["title"] if topic else "",
                    "topic_version": topic["version"] if topic else "", "topic_status": topic["status"] if topic else "",
                    "publication_id": link["publication_id"], "publication_title": pub["title"] if pub else "", "position": link["position"],
                    "languages": " | ".join(sorted({t["target_lang"] for t in translations})),
                    "stale_languages_if_topic_changes": " | ".join(sorted({t["target_lang"] for t in translations if topic and int(t["source_version"]) == int(topic["version"])})),
                })
        return pd.DataFrame(rows)

    # CP5.2b: the persistence steps of the migration import, moved VERBATIM out of
    # app.import_manual_ui -- same statements, parameters and commits. The caller keeps
    # the ORDER: for PDF/DOCX/DOC the draft is retired (and committed) before the new
    # file is parsed, for IDML only after it was fully imported. Pinned, behaviour
    # included, by regressione_import_orchestration.
    def migration_draft_objects(self, publication_id: str) -> tuple[list[str], list[str], list[str]]:
        """(topic ids, table ids, figure ids) of the publication's migration draft -- empty
        when the publication does not exist or links no topic. Re-import is allowed only
        while the publication is still a migration draft: ValueError otherwise."""
        pub = publication_id
        tids_old=[]; table_ids_old=[]; figure_ids_old=[]
        if self.conn.execute("SELECT 1 FROM publications WHERE publication_id=?",(pub,)).fetchone():
            linked=list(self.conn.execute(
                "SELECT t.topic_id,t.status FROM topics t JOIN publication_topics pt ON pt.topic_id=t.topic_id WHERE pt.publication_id=?",
                (pub,)
            ))
            if linked and any(str(r["status"]).lower() != "migrated" for r in linked):
                raise ValueError("La pubblicazione contiene contenuti già revisionati/approvati. Usa un nuovo Publication ID per una nuova migrazione.")
            tids_old=[str(r["topic_id"]) for r in linked]
            if tids_old:
                marks=",".join("?" for _ in tids_old)
                table_ids_old=[str(r[0]) for r in self.conn.execute(
                    f"SELECT object_ref_id FROM content_blocks WHERE topic_id IN ({marks}) AND block_type='table' AND object_ref_id<>''",
                    tids_old
                )]
                figure_ids_old=[str(r[0]) for r in self.conn.execute(
                    f"SELECT object_ref_id FROM content_blocks WHERE topic_id IN ({marks}) AND block_type='figure' AND object_ref_id<>''",
                    tids_old
                )]
        return tids_old, table_ids_old, figure_ids_old

    def retire_migration_draft(self, publication_id: str, topic_ids: list[str], table_ids: list[str],
                               figure_ids: list[str]) -> None:
        """Delete the draft found by migration_draft_objects() and COMMIT (nothing when it
        has no topic). Blocks, translations and versions go with it (ON DELETE CASCADE)."""
        pub = publication_id
        if topic_ids:
            self.conn.execute("DELETE FROM publication_topics WHERE publication_id=?",(pub,))
            self.conn.executemany("DELETE FROM topics WHERE topic_id=?",[(tid,) for tid in topic_ids])
            if table_ids:
                self.conn.executemany("DELETE FROM structured_tables WHERE table_id=?",[(tid,) for tid in table_ids])
            if figure_ids:
                self.conn.executemany("DELETE FROM figures WHERE figure_id=?",[(fid,) for fid in figure_ids])
            self.conn.commit()

    def publication_exists(self, publication_id: str) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM publications WHERE publication_id=?",(publication_id,)).fetchone())

    def update_publication_header(self, publication_id: str, title: str, source_lang: str) -> None:
        """Title and language stored AS RECEIVED and committed -- unlike create_publication(),
        which strips the title and canonicalizes the language (pinned asymmetry, S16)."""
        self.conn.execute("UPDATE publications SET title=?,source_lang=? WHERE publication_id=?",
                          (title,source_lang,publication_id))
        self.conn.commit()

    def structured_table_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM structured_tables").fetchone()[0])

    # HARDENING-02B (B5): the human work a re-import would destroy. The draft is exactly what
    # migration_draft_objects() returns -- the linked topics, their blocks, the tables and
    # figures those blocks point at -- found here by subquery (no parameter list). Audit rows,
    # review tasks and compliance evidence have no foreign key to the content, and a PDF/DOCX
    # re-import re-creates the same ids: they count only from the creation of the object they
    # name, older rows belong to a previous draft (regressione_import_orchestration S17j2).
    _MIGRATION_DRAFT = (
        "WITH t AS (SELECT tp.topic_id AS id, tp.created_at AS born FROM topics tp "
        "JOIN publication_topics pt ON pt.topic_id=tp.topic_id WHERE pt.publication_id=:pub), "
        "b AS (SELECT block_id AS id, created_at AS born FROM content_blocks WHERE topic_id IN (SELECT id FROM t)), "
        "tb AS (SELECT table_id AS id, created_at AS born FROM structured_tables WHERE table_id IN "
        "(SELECT object_ref_id FROM content_blocks WHERE topic_id IN (SELECT id FROM t) "
        "AND block_type='table' AND object_ref_id<>'')), "
        "fg AS (SELECT figure_id AS id, created_at AS born FROM figures WHERE figure_id IN "
        "(SELECT object_ref_id FROM content_blocks WHERE topic_id IN (SELECT id FROM t) "
        "AND block_type='figure' AND object_ref_id<>'')), "
        "o AS (SELECT 'topic' AS kind, id, born FROM t UNION ALL SELECT 'block', id, born FROM b "
        "UNION ALL SELECT 'table', id, born FROM tb UNION ALL SELECT 'figure', id, born FROM fg) "
    )

    def migration_draft_human_work(self, publication_id: str) -> dict[str, int]:
        """What would be lost if the publication's migration draft were retired, as
        {kind: count} with only the kinds found (empty = an untouched draft). SELECT only,
        no commit -- the caller refuses the re-import. Kinds:
          versions      topic/block/table/figure saved past version 1
          status        block/table/figure no longer 'Migrated' (topics: migration_draft_objects)
          archived      topic/block archived
          translations  any translation row (they cascade with their object, never stale)
          audit         audit row on a draft object, or on the publication, since that
                        object (the draft) was created
          review        review task on a draft object since it was created
          evidence      compliance evidence on a draft object added -- or linked -- since then
        A date that cannot be read counts. Autosaves (authoring_drafts) never count."""
        p = {"pub": publication_id}

        def rows(sql: str) -> list[sqlite3.Row]:
            return list(self.conn.execute(self._MIGRATION_DRAFT + sql, p))

        def total(sql: str) -> int:
            return int(rows(sql)[0][0] or 0)

        if not rows("SELECT 1 FROM t LIMIT 1"):
            return {}
        work = {
            "versions": total(
                "SELECT (SELECT COUNT(*) FROM topics WHERE topic_id IN (SELECT id FROM t) AND version>1)"
                " + (SELECT COUNT(*) FROM content_blocks WHERE block_id IN (SELECT id FROM b) AND version>1)"
                " + (SELECT COUNT(*) FROM structured_tables WHERE table_id IN (SELECT id FROM tb) AND version>1)"
                " + (SELECT COUNT(*) FROM figures WHERE figure_id IN (SELECT id FROM fg) AND version>1)"),
            "status": total(
                "SELECT (SELECT COUNT(*) FROM content_blocks WHERE block_id IN (SELECT id FROM b)"
                " AND COALESCE(LOWER(status),'')<>'migrated')"
                " + (SELECT COUNT(*) FROM structured_tables WHERE table_id IN (SELECT id FROM tb)"
                " AND COALESCE(LOWER(status),'')<>'migrated')"
                " + (SELECT COUNT(*) FROM figures WHERE figure_id IN (SELECT id FROM fg)"
                " AND COALESCE(LOWER(status),'')<>'migrated')"),
            "archived": total(
                "SELECT (SELECT COUNT(*) FROM topics WHERE topic_id IN (SELECT id FROM t) AND COALESCE(archived_at,'')<>'')"
                " + (SELECT COUNT(*) FROM content_blocks WHERE block_id IN (SELECT id FROM b)"
                " AND COALESCE(archived_at,'')<>'')"),
            "translations": total(
                "SELECT (SELECT COUNT(*) FROM topic_translations WHERE topic_id IN (SELECT id FROM t))"
                " + (SELECT COUNT(*) FROM block_translations WHERE block_id IN (SELECT id FROM b))"
                " + (SELECT COUNT(*) FROM structured_table_translations WHERE table_id IN (SELECT id FROM tb))"
                " + (SELECT COUNT(*) FROM figure_translations WHERE figure_id IN (SELECT id FROM fg))"),
        }
        since = self.migration_draft_created_at(publication_id)
        work["audit"] = sum(_not_before(r[0], r[1]) for r in rows(
            "SELECT e.created_at, o.born FROM audit_events e JOIN o ON o.kind=e.object_type AND o.id=e.object_id"))
        work["audit"] += sum(_not_before(r[0], since) for r in rows(
            "SELECT created_at FROM audit_events WHERE object_type='publication' AND object_id=:pub"))
        work["review"] = sum(_not_before(r[0], r[1]) for r in rows(
            "SELECT r.created_at, o.born FROM review_tasks r JOIN o ON o.kind=r.object_type AND o.id=r.object_id"))
        # compliance_evidence belongs to ComplianceService and exists only once it was opened:
        # looked up, never created (its constructor runs DDL and commits).
        present = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('compliance_evidence','compliance_evidence_link')")}
        work["evidence"] = 0
        if "compliance_evidence" in present:
            links: dict[str, list[str]] = {}
            if "compliance_evidence_link" in present:
                for r in rows("SELECT l.evidence_id, l.linked_at FROM compliance_evidence_link l WHERE l.evidence_id IN "
                              "(SELECT e.evidence_id FROM compliance_evidence e "
                              "JOIN o ON o.kind=e.evidence_kind AND o.id=e.object_id)"):
                    links.setdefault(r[0], []).append(r[1])
            work["evidence"] = sum(
                _not_before(r[1], r[2]) or any(_not_before(at, r[2]) for at in links.get(r[0], []))
                for r in rows("SELECT e.evidence_id, e.added_at, o.born FROM compliance_evidence e "
                              "JOIN o ON o.kind=e.evidence_kind AND o.id=e.object_id"))
        return {kind: n for kind, n in work.items() if n}

    def migration_draft_created_at(self, publication_id: str) -> str | None:
        """When the publication's migration draft was created: the earliest created_at of its
        linked topics (None when it links none) -- the start of HARDENING-02B's time filter."""
        row = self.conn.execute(
            "SELECT MIN(t.created_at) FROM topics t JOIN publication_topics pt ON pt.topic_id=t.topic_id "
            "WHERE pt.publication_id=?", (publication_id,)).fetchone()
        return None if row is None or row[0] is None else str(row[0])


def create_industrial_demo(out_dir: str | Path, *, figure_path: str | Path | None = None) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / LEGACY_CCMS_DB_NAME   # the industrial demo keeps the legacy file name
    db.unlink(missing_ok=True)
    repo = IndustrialRepository(db)
    try:
        repo.create_component("SAFETY-POWER", "Power isolation warning", "WARNING: Disconnect power and wait 15 minutes before service.", source_refs=["SafetySpec#4.2"])
        repo.create_topic("INSTALL-001", "Installation", "Legacy topic body kept for compatibility.", status="Approved", source_refs=["ManualEN#p30"])
        repo.create_topic("SERVICE-001", "Service", "Legacy service body.", status="Approved", source_refs=["ServiceSpec#2"])
        repo.create_publication("PUB-ACS", "ACS Installation Manual", ["INSTALL-001", "SERVICE-001"])

        repo.create_profile("EU-R5V2-SERVICE", "EU R5 v2 service edition", {"product":"R5v2", "market":"EU", "audience":"service", "features":["EMC"]})
        repo.create_profile("US-BASE-USER", "US base user edition", {"product":"BASE", "market":"US", "audience":"user", "features":[]})

        repo.create_table(
            "TBL-CLEARANCE", ["Frame", "Top clearance", "Side clearance"],
            [["R4", "100 mm", "50 mm"], ["R5 v2", "150 mm", "75 mm"]],
            title="Minimum clearances", caption="Minimum installation clearances", status="Approved",
            source_refs=["EngineeringTable#T-12"],
        )
        if figure_path:
            repo.create_figure("FIG-AIRFLOW", title="Airflow", caption="Cooling airflow around the drive", file_path=str(figure_path),
                               alt_text="Drive with arrows indicating vertical cooling airflow", status="Approved", source_refs=["Drawing#DWG-44"])
        else:
            repo.create_figure("FIG-AIRFLOW", title="Airflow", caption="Cooling airflow around the drive", file_path="",
                               alt_text="Drive with arrows indicating vertical cooling airflow", status="Approved", source_refs=["Drawing#DWG-44"])

        repo.add_block("BLK-I-01", "INSTALL-001", "paragraph", position=1,
                       text="Install the drive vertically on a rigid surface.", status="Approved", source_refs=["ManualEN#p30"])
        repo.add_block("BLK-I-02", "INSTALL-001", "component_ref", position=2,
                       object_ref_id="SAFETY-POWER", status="Approved", source_refs=["SafetySpec#4.2"])
        repo.add_block("BLK-I-03", "INSTALL-001", "table", position=3,
                       object_ref_id="TBL-CLEARANCE", status="Approved", source_refs=["EngineeringTable#T-12"])
        repo.add_block("BLK-I-04", "INSTALL-001", "figure", position=4,
                       object_ref_id="FIG-AIRFLOW", status="Approved", source_refs=["Drawing#DWG-44"])
        repo.add_block("BLK-I-05", "INSTALL-001", "warning", position=5,
                       text="For frame R5 v2 in the EU market, use the EMC screw supplied with the drive.", status="Approved",
                       condition={"all":[{"field":"product","op":"eq","value":"R5v2"},{"field":"market","op":"eq","value":"EU"}]},
                       source_refs=["EngineeringChange#EC-2026-41"])
        repo.add_block("BLK-S-01", "SERVICE-001", "paragraph", position=1,
                       text="Use approved service tools only.", status="Approved", source_refs=["ServiceSpec#2"])
        repo.add_block("BLK-S-02", "SERVICE-001", "note", position=2,
                       text="This service-only diagnostic step is hidden from end-user publications.", status="Approved",
                       condition={"field":"audience","op":"eq","value":"service"}, source_refs=["ServiceSpec#3"])

        repo.set_block_translation("BLK-I-01", "it-IT", "Installare il convertitore verticalmente su una superficie rigida.", status="Reviewed")
        repo.set_table_translation("TBL-CLEARANCE", "it-IT", caption="Distanze minime di installazione",
                                   columns=["Telaio", "Distanza superiore", "Distanza laterale"],
                                   rows=[["R4", "100 mm", "50 mm"], ["R5 v2", "150 mm", "75 mm"]], status="Reviewed")
        repo.set_figure_translation("FIG-AIRFLOW", "it-IT", caption="Flusso dell'aria di raffreddamento attorno al convertitore",
                                    alt_text="Convertitore con frecce che indicano il flusso verticale dell'aria", status="Reviewed")

        frame, summary = repo.assemble_structured_publication("PUB-ACS", profile_id="EU-R5V2-SERVICE")
        return {"repo": db, "frame": frame, "summary": summary}
    finally:
        repo.close()


def upgrade_legacy_topics_to_blocks(repo: IndustrialRepository) -> pd.DataFrame:
    """Convert v0.17 topic bodies into block-level content conservatively.

    Component tokens become component_ref blocks; surrounding text remains
    paragraph blocks. Existing block-level topics are left untouched.
    """
    rows = []
    for topic in repo.conn.execute("SELECT * FROM topics ORDER BY topic_id"):
        existing = repo.conn.execute("SELECT COUNT(*) AS n FROM content_blocks WHERE topic_id=?", (topic["topic_id"],)).fetchone()["n"]
        if existing:
            rows.append({"topic_id":topic["topic_id"], "created_blocks":0, "status":"SKIPPED_ALREADY_STRUCTURED"})
            continue
        body = str(topic["body"] or "")
        pos = 1
        cursor = 0
        created = 0
        for m in COMPONENT_TOKEN_RE.finditer(body):
            before = body[cursor:m.start()].strip()
            if before:
                bid = f"{topic['topic_id']}-B{pos:03d}"
                repo.add_block(bid, topic["topic_id"], "paragraph", position=pos, text=before,
                               status=topic["status"], source_refs=_load_json(topic["source_refs_json"], []), evidence_status=topic["evidence_status"])
                pos += 1; created += 1
            cid = m.group(1)
            bid = f"{topic['topic_id']}-B{pos:03d}"
            if repo.conn.execute("SELECT 1 FROM components WHERE component_id=?", (cid,)).fetchone():
                repo.add_block(bid, topic["topic_id"], "component_ref", position=pos, object_ref_id=cid,
                               status=topic["status"], source_refs=_load_json(topic["source_refs_json"], []), evidence_status=topic["evidence_status"])
            else:
                repo.add_block(bid, topic["topic_id"], "paragraph", position=pos, text=m.group(0),
                               status="Draft", source_refs=_load_json(topic["source_refs_json"], []), evidence_status="MISSING_REFERENCE")
            pos += 1; created += 1
            cursor = m.end()
        tail = body[cursor:].strip()
        if tail or created == 0:
            bid = f"{topic['topic_id']}-B{pos:03d}"
            repo.add_block(bid, topic["topic_id"], "paragraph", position=pos, text=tail,
                           status=topic["status"], source_refs=_load_json(topic["source_refs_json"], []), evidence_status=topic["evidence_status"])
            created += 1
        rows.append({"topic_id":topic["topic_id"], "created_blocks":created, "status":"MIGRATED"})
    return pd.DataFrame(rows)
