"""Milestone 4B.1: Authoring Foundation & Review Repair.

AuthoringService wraps an existing IndustrialRepository -- same sqlite3
connection, same DB file, same open/close lifecycle, same audit_events
table (reused via repo.audit_event(), never duplicated) -- exactly the
composition pattern ComplianceService already established for this
codebase. It does NOT duplicate or replace IndustrialRepository: every
CCMS read/write still goes through the repository's own tables and
version-history convention. What lives here instead is the set of
editing-workflow RULES a professional authoring editor needs on top of
that CRUD -- rules that do not belong distributed across app.py or
smuggled into the repository's generic CRUD methods:

- optimistic concurrency (atomic compare-and-swap save, never a separate
  SELECT-then-UPDATE race);
- draft recovery, kept OUT of the versioned content history entirely, so
  autosave never creates a content version;
- status invalidation (an edit to Approved/In Review content always drops
  it back to Draft, and invalidates -- never silently orphans -- any OPEN
  review task riding on the version being replaced);
- reorder (blocks within a topic, topics within a publication), guarded
  against a concurrently-changed structure;
- archive/restore (a reversible, audited, non-destructive removal from the
  authoring outline and publication assembly -- never a hard delete, never
  conflated with the Draft/In Review/Approved editorial lifecycle);
- revert-to-version (always creates a NEW version with old content; never
  rewrites history);
- a single, consistent audit trail for every one of the above, closing the
  gap the 4A audit found (ordinary CRUD on IndustrialRepository does not
  audit itself).

Scope for 4B.1: topic and content_block only. Tables/figures/components
are explicitly out of scope here (see the 4B.1 report) and are not
touched by this module.

Concurrency note: every multi-statement operation below opens its own
`BEGIN IMMEDIATE` transaction, deliberately -- not merely for the
compare-and-swap UPDATE itself (a single UPDATE statement is already
atomic at the SQLite engine level), but so the *whole* operation (the
CAS check, the *_versions history insert, review invalidation, the audit
event, the draft cleanup) commits or rolls back as one unit, exactly the
same rationale as release_manager.create_release_bundle's 3.5C TOCTOU
fix. AuthoringService itself is constructed once, before any transaction
opens (its own schema-ensure step, like ComplianceService's, calls
conn.executescript(), which implicitly commits a pending transaction --
constructing a second instance or re-running schema-ensure mid-transaction
would silently break atomicity); every method below only ever issues plain
`execute()` calls inside its own BEGIN IMMEDIATE block, never
executescript(), so nothing here can trip that landmine on itself.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from ccms_industrial import IndustrialRepository

AUTHORING_SERVICE_BUILD = "0.3.0-authoring-tables-figures-4c-20260903"

# 4C: draft recovery now also covers table/figure editing -- no schema
# change needed (authoring_drafts.kind has no CHECK constraint), just
# widening the set of kinds save_draft() accepts.
_EDITABLE_KINDS = {"topic", "block", "table", "figure"}
_INVALIDATING_STATUSES = {"Approved", "In Review"}

# 4B.2: the ONLY block types the editor can create. Deliberately a strict
# subset of IndustrialRepository.add_block()'s own allow-list -- table/
# figure/component_ref are structural/object-referencing types the text
# editor must never be able to touch (it would corrupt a shared object via
# a generic textarea), and no new enum value (heading/procedure_step) is
# introduced in this increment.
EDITABLE_TEXT_BLOCK_TYPES = {"paragraph", "note", "warning", "list_item"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS authoring_drafts (
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(kind, object_id)
);
"""


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


class VersionConflictError(Exception):
    """Raised when an optimistic-concurrency compare-and-swap save/reorder
    fails: the live object (or structure) no longer matches what the
    caller expected, because another session/process wrote first. The
    caller's original object, version, and history are left completely
    untouched -- no partial write, no fake version, no lost draft."""

    def __init__(self, object_id: Any, expected_version: Any, *, detail: str = ""):
        self.object_id = object_id
        self.expected_version = expected_version
        self.detail = detail
        msg = detail or (
            f"Conflitto di concorrenza su {object_id}: la versione attesa ({expected_version}) "
            "non corrisponde più a quella corrente -- qualcun altro ha salvato nel frattempo."
        )
        super().__init__(msg)


class ArchivedObjectError(Exception):
    """Raised when an editing operation targets an archived topic/block.
    Restore it first -- an archived object is not editable in place."""


class SharedObjectError(Exception):
    """4C: raised when a save/revert targets a table or figure referenced
    by more than one content_blocks row. Reuse-aware editing (fork a local
    copy vs. update every occurrence, with an impact-map UX) is explicitly
    4D scope -- until then, a shared object is fail-closed: never silently
    saved as a global edit no one asked for."""


class MissingAssetError(Exception):
    """4C: raised when a figure revert would restore a historical
    file_path whose bytes no longer exist on disk. Reverting to a missing
    asset would silently produce a new 'broken image' figure -- fail
    closed instead, with a message that says exactly why."""


_SHARED_OBJECT_MESSAGE = (
    "Questo elemento è riutilizzato in più punti. La modifica condivisa sarà gestita nella fase "
    "Reuse-aware authoring."
)


class AuthoringService:
    """4B.1 editing-foundation service. Wraps an existing IndustrialRepository
    (composition, not inheritance/duplication) -- same connection, same
    lifecycle. Callers open/close the underlying IndustrialRepository as
    usual; this service never owns the connection."""

    def __init__(self, repo: IndustrialRepository):
        self.repo = repo
        self.conn = repo.conn
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    # ================================================================
    # Draft recovery -- explicitly OUTSIDE the versioned content history.
    # Never touches topics/topic_versions/content_blocks/block_versions.
    # Never read by assemble_structured_publication, translation, or
    # readiness -- a draft is not content until it is Saved.
    # ================================================================

    def save_draft(self, kind: str, object_id: str, *, base_version: int, payload: dict[str, Any],
                   actor: str, session_id: str = "") -> None:
        if kind not in _EDITABLE_KINDS:
            raise ValueError(f"kind deve essere uno tra {sorted(_EDITABLE_KINDS)}")
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per un draft recovery.")
        now = _now()
        self.conn.execute(
            """INSERT INTO authoring_drafts(kind,object_id,base_version,payload_json,actor,session_id,dirty,updated_at)
               VALUES(?,?,?,?,?,?,1,?)
               ON CONFLICT(kind,object_id) DO UPDATE SET base_version=excluded.base_version,
               payload_json=excluded.payload_json,actor=excluded.actor,session_id=excluded.session_id,
               dirty=1,updated_at=excluded.updated_at""",
            (kind, object_id, int(base_version), _json(payload), actor, session_id, now),
        )
        self.conn.commit()

    def load_draft(self, kind: str, object_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM authoring_drafts WHERE kind=? AND object_id=?", (kind, object_id)
        ).fetchone()
        if not row:
            return None
        return {
            "kind": kind, "object_id": object_id, "base_version": int(row["base_version"]),
            "payload": _load_json(row["payload_json"], {}), "actor": row["actor"],
            "session_id": row["session_id"], "dirty": bool(row["dirty"]), "updated_at": row["updated_at"],
        }

    def discard_draft(self, kind: str, object_id: str) -> None:
        """Explicit user action ('scarta le modifiche'): commits on its own,
        unlike _delete_draft() which only runs inside a save's own
        transaction."""
        self.conn.execute("DELETE FROM authoring_drafts WHERE kind=? AND object_id=?", (kind, object_id))
        self.conn.commit()

    def _delete_draft(self, kind: str, object_id: str) -> None:
        # Internal: no commit here. Only ever called from inside another
        # method's own BEGIN IMMEDIATE block, which owns the commit.
        self.conn.execute("DELETE FROM authoring_drafts WHERE kind=? AND object_id=?", (kind, object_id))

    # ================================================================
    # Load editable object -- current CCMS state + any recoverable draft.
    # ================================================================

    def load_topic_for_edit(self, topic_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if not row:
            raise KeyError(topic_id)
        return {
            "topic_id": topic_id, "title": row["title"], "body": row["body"], "status": row["status"],
            "version": int(row["version"]), "archived": bool(row["archived_at"]), "archived_at": row["archived_at"],
            "source_refs": _load_json(row["source_refs_json"], []),
            "draft": self.load_draft("topic", topic_id),
        }

    def load_block_for_edit(self, block_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
        if not row:
            raise KeyError(block_id)
        return {
            "block_id": block_id, "topic_id": row["topic_id"], "block_type": row["block_type"],
            "position": int(row["position"]), "text": row["text_content"], "status": row["status"],
            "version": int(row["version"]), "archived": bool(row["archived_at"]), "archived_at": row["archived_at"],
            "source_refs": _load_json(row["source_refs_json"], []),
            "object_ref_id": row["object_ref_id"],
            "draft": self.load_draft("block", block_id),
        }

    def load_table_for_edit(self, table_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
        if not row:
            raise KeyError(table_id)
        block = self._referencing_block("table", table_id)
        return {
            "table_id": table_id, "title": row["title"], "caption": row["caption"],
            "columns": _load_json(row["columns_json"], []), "rows": _load_json(row["rows_json"], []),
            "header_rows": int(row["header_rows"]), "status": row["status"], "version": int(row["version"]),
            "source_refs": _load_json(row["source_refs_json"], []),
            "archived": bool(block["archived_at"]) if block is not None else False,
            "reference_count": self._reference_count("table", table_id),
            "draft": self.load_draft("table", table_id),
        }

    def load_figure_for_edit(self, figure_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
        if not row:
            raise KeyError(figure_id)
        block = self._referencing_block("figure", figure_id)
        return {
            "figure_id": figure_id, "title": row["title"], "caption": row["caption"],
            "file_path": row["file_path"], "alt_text": row["alt_text"],
            "status": row["status"], "version": int(row["version"]),
            "source_refs": _load_json(row["source_refs_json"], []),
            "archived": bool(block["archived_at"]) if block is not None else False,
            "reference_count": self._reference_count("figure", figure_id),
            "asset_missing": bool(row["file_path"]) and not Path(row["file_path"]).exists(),
            "draft": self.load_draft("figure", figure_id),
        }

    # ================================================================
    # Review invalidation -- shared by save/revert. Never called for a
    # decide_review() outcome itself (that already manages its own status
    # transition correctly); only for an EDIT that supersedes a version a
    # review task was opened against.
    # ================================================================

    def _invalidate_open_review_if_edited(self, object_type: str, object_id: str, previous_status: str,
                                          new_status: str, actor: str) -> int:
        if previous_status not in _INVALIDATING_STATUSES:
            return 0
        rows = self.conn.execute(
            "SELECT review_id FROM review_tasks WHERE object_type=? AND object_id=? AND status='OPEN'",
            (object_type, object_id),
        ).fetchall()
        now = _now()
        for r in rows:
            rid = int(r["review_id"])
            self.conn.execute(
                "UPDATE review_tasks SET status='CLOSED',decision='INVALIDATED_BY_EDIT',comment=?,decided_at=? WHERE review_id=?",
                (f"Invalidated: {object_type} {object_id} was edited while {previous_status}", now, rid),
            )
            self.repo.audit_event(object_type, object_id, "REVIEW_INVALIDATED_BY_EDIT", actor,
                                  {"review_id": rid, "previous_status": previous_status, "new_status": new_status})
        return len(rows)

    @staticmethod
    def _status_after_edit(previous_status: str) -> str:
        return "Draft" if previous_status in _INVALIDATING_STATUSES else previous_status

    # ================================================================
    # Atomic save (compare-and-swap) -- topic
    # ================================================================

    def save_topic(self, topic_id: str, *, title: str | None = None, body: str | None = None,
                   source_refs: Iterable[str] | None = None, expected_version: int, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per salvare un topic.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(topic_id)
            if row["archived_at"]:
                self.conn.rollback()
                raise ArchivedObjectError(f"Il topic {topic_id} è archiviato: ripristinalo prima di modificarlo.")
            new_title = row["title"] if title is None else title.strip()
            new_body = row["body"] if body is None else body.strip()
            new_refs = _load_json(row["source_refs_json"], []) if source_refs is None else _norm_set(source_refs)
            previous_status = row["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(expected_version) + 1
            include = row["include_variants_json"]
            exclude = row["exclude_variants_json"]
            evidence = row["evidence_status"]

            cur = self.conn.execute(
                """UPDATE topics SET title=?,body=?,status=?,version=?,source_refs_json=?,updated_at=?
                   WHERE topic_id=? AND version=?""",
                (new_title, new_body, new_status, new_version, _json(new_refs), now, topic_id, int(expected_version)),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(topic_id, expected_version)
            self.conn.execute(
                """INSERT INTO topic_versions(topic_id,version,title,body,status,include_variants_json,
                   exclude_variants_json,source_refs_json,evidence_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (topic_id, new_version, new_title, new_body, new_status, include, exclude, _json(new_refs), evidence, now),
            )
            invalidated = self._invalidate_open_review_if_edited("topic", topic_id, previous_status, new_status, actor)
            self.repo.audit_event("topic", topic_id, "AUTHORING_SAVE", actor,
                                  {"from_version": int(expected_version), "to_version": new_version,
                                   "previous_status": previous_status, "new_status": new_status})
            self._delete_draft("topic", topic_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"topic_id": topic_id, "version": new_version, "status": new_status,
                "previous_status": previous_status, "review_tasks_invalidated": invalidated}

    # ================================================================
    # Atomic save (compare-and-swap) -- block
    # ================================================================

    def save_block(self, block_id: str, *, text: str | None = None, source_refs: Iterable[str] | None = None,
                   expected_version: int, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per salvare un block.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(block_id)
            if row["archived_at"]:
                self.conn.rollback()
                raise ArchivedObjectError(f"Il blocco {block_id} è archiviato: ripristinalo prima di modificarlo.")
            if text is not None and row["block_type"] not in EDITABLE_TEXT_BLOCK_TYPES:
                self.conn.rollback()
                raise ValueError(
                    f"Il blocco {block_id} è di tipo {row['block_type']!r}: non è editabile come testo generico "
                    "in questo incremento (tabelle/figure/componenti riusabili non vanno corrotti da una textarea)."
                )
            new_text = row["text_content"] if text is None else text.strip()
            new_refs = _load_json(row["source_refs_json"], []) if source_refs is None else _norm_set(source_refs)
            previous_status = row["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(expected_version) + 1
            condition = row["condition_json"]
            evidence = row["evidence_status"]
            object_ref_id = row["object_ref_id"]

            cur = self.conn.execute(
                """UPDATE content_blocks SET text_content=?,status=?,version=?,source_refs_json=?,updated_at=?
                   WHERE block_id=? AND version=?""",
                (new_text, new_status, new_version, _json(new_refs), now, block_id, int(expected_version)),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(block_id, expected_version)
            self.conn.execute(
                """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,
                   object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, new_version, row["topic_id"], row["block_type"], int(row["position"]), new_text,
                 object_ref_id, new_status, condition, _json(new_refs), evidence, now),
            )
            invalidated = self._invalidate_open_review_if_edited("block", block_id, previous_status, new_status, actor)
            self.repo.audit_event("block", block_id, "AUTHORING_SAVE", actor,
                                  {"from_version": int(expected_version), "to_version": new_version,
                                   "previous_status": previous_status, "new_status": new_status})
            self._delete_draft("block", block_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"block_id": block_id, "version": new_version, "status": new_status,
                "previous_status": previous_status, "review_tasks_invalidated": invalidated}

    # ================================================================
    # Reorder
    # ================================================================

    def reorder_blocks(self, topic_id: str, ordered_block_ids: list[str], *,
                       expected_block_ids: list[str], actor: str) -> list[str]:
        """Reassigns sequential positions to every active (non-archived)
        block of a topic. Rejected (ValueError) if ordered_block_ids isn't
        exactly the topic's current active block set -- no duplicates, no
        missing block, no block from another topic. Rejected
        (VersionConflictError) if expected_block_ids (the order the caller
        believes is current) no longer matches, under the same lock used
        to apply the change -- closing the TOCTOU window between read and
        write. Deliberately does NOT bump block_versions for the reordered
        blocks: their CONTENT did not change, only their position within
        the topic -- versioning every block on every reorder would flood
        history with noise for a structural, not content, operation. The
        reorder itself is still fully audited (before/after order,
        actor)."""
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per un reorder.")
        if len(ordered_block_ids) != len(set(ordered_block_ids)):
            raise ValueError("L'ordine proposto contiene ID di blocco duplicati.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live_rows = list(self.conn.execute(
                "SELECT block_id FROM content_blocks WHERE topic_id=? AND archived_at IS NULL ORDER BY position,block_id",
                (topic_id,),
            ))
            if not live_rows and not self.conn.execute("SELECT 1 FROM topics WHERE topic_id=?", (topic_id,)).fetchone():
                self.conn.rollback()
                raise KeyError(topic_id)
            live_ids = [r["block_id"] for r in live_rows]
            if set(ordered_block_ids) != set(live_ids):
                self.conn.rollback()
                missing = sorted(set(live_ids) - set(ordered_block_ids))
                extra = sorted(set(ordered_block_ids) - set(live_ids))
                raise ValueError(
                    f"L'ordine proposto non coincide con i blocchi attivi del topic {topic_id} "
                    f"(mancanti={missing}, estranei/di altro topic={extra})."
                )
            if list(expected_block_ids) != live_ids:
                self.conn.rollback()
                raise VersionConflictError(
                    topic_id, None,
                    detail=f"La struttura del topic {topic_id} è cambiata nel frattempo: ricarica prima di riordinare.",
                )
            for pos, bid in enumerate(ordered_block_ids, start=1):
                self.conn.execute("UPDATE content_blocks SET position=?,updated_at=? WHERE block_id=?", (pos, now, bid))
            self.repo.audit_event("topic", topic_id, "REORDER_BLOCKS", actor,
                                  {"before": live_ids, "after": list(ordered_block_ids)})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return list(ordered_block_ids)

    def reorder_publication_topics(self, publication_id: str, ordered_topic_ids: list[str], *,
                                   expected_topic_ids: list[str], actor: str) -> list[str]:
        """Same guarantees as reorder_blocks(), for a publication's topic
        order -- including the same active-only scope: an archived topic's
        publication_topics row is left untouched here (mirroring how
        reorder_blocks() never touches an archived block's position), so
        ordered_topic_ids/expected_topic_ids only ever need to name the
        publication's currently ACTIVE topics, never an archived one.
        restore_topic() is the only thing that repositions an archived
        topic's row, deterministically, when it becomes active again.

        Deliberately does NOT call IndustrialRepository.
        replace_publication_topics(): that method commits internally,
        which would split this operation across two transactions and
        defeat the atomicity this method must guarantee (a concurrent
        reader could observe the new order committed but the audit event
        not yet, or vice versa). The equivalent DELETE+INSERT is inlined
        here instead, under the same BEGIN IMMEDIATE as the audit event
        -- and only ever touches ACTIVE topics' rows, never an archived
        one's."""
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per un reorder.")
        if len(ordered_topic_ids) != len(set(ordered_topic_ids)):
            raise ValueError("L'ordine proposto contiene ID di topic duplicati.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self.conn.execute("SELECT 1 FROM publications WHERE publication_id=?", (publication_id,)).fetchone():
                self.conn.rollback()
                raise KeyError(publication_id)
            live_rows = list(self.conn.execute(
                """SELECT pt.topic_id FROM publication_topics pt
                   JOIN topics t ON t.topic_id=pt.topic_id
                   WHERE pt.publication_id=? AND t.archived_at IS NULL ORDER BY pt.position""",
                (publication_id,),
            ))
            live_ids = [r["topic_id"] for r in live_rows]
            if set(ordered_topic_ids) != set(live_ids):
                self.conn.rollback()
                missing = sorted(set(live_ids) - set(ordered_topic_ids))
                extra = sorted(set(ordered_topic_ids) - set(live_ids))
                raise ValueError(
                    f"L'ordine proposto non coincide con i topic ATTIVI della publication {publication_id} "
                    f"(mancanti={missing}, estranei/archiviati={extra})."
                )
            if list(expected_topic_ids) != live_ids:
                self.conn.rollback()
                raise VersionConflictError(
                    publication_id, None,
                    detail=f"L'ordine dei topic di {publication_id} è cambiato nel frattempo: ricarica prima di riordinare.",
                )
            for pos, tid in enumerate(ordered_topic_ids, start=1):
                self.conn.execute(
                    "UPDATE publication_topics SET position=? WHERE publication_id=? AND topic_id=?",
                    (pos, publication_id, tid),
                )
            self.conn.execute("UPDATE publications SET updated_at=? WHERE publication_id=?", (now, publication_id))
            self.repo.audit_event("publication", publication_id, "REORDER_TOPICS", actor,
                                  {"before": live_ids, "after": list(ordered_topic_ids)})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return list(ordered_topic_ids)

    # ================================================================
    # Archive / restore -- topic and block ONLY in 4B.1. A separate axis
    # from the editorial status lifecycle: archived_at is NULL (active) or
    # an ISO timestamp. Never conflated with Draft/In Review/Approved.
    # Reversible, audited, preserves full version history untouched.
    # ================================================================

    def archive_topic(self, topic_id: str, *, actor: str) -> None:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per archiviare.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT archived_at FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(topic_id)
            if row["archived_at"]:
                self.conn.rollback()
                raise ValueError(f"Il topic {topic_id} è già archiviato.")
            self.conn.execute("UPDATE topics SET archived_at=? WHERE topic_id=?", (now, topic_id))
            self.repo.audit_event("topic", topic_id, "ARCHIVE", actor, {})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def restore_topic(self, topic_id: str, *, actor: str) -> dict[str, Any]:
        """Same 'append after every active sibling' restore semantics as
        restore_block(), applied independently to EVERY publication this
        topic is linked into (a topic can belong to more than one
        publication) -- reorder_publication_topics() only ever renumbers
        active topics, so a stale position on a restored topic could
        otherwise collide with the renumbered active range in any of them."""
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per ripristinare.")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT archived_at FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(topic_id)
            if not row["archived_at"]:
                self.conn.rollback()
                raise ValueError(f"Il topic {topic_id} non è archiviato.")
            # Compute every new position BEFORE flipping archived_at, and
            # explicitly exclude this topic_id from each "max active
            # position" query -- otherwise, once archived_at is cleared,
            # this topic would count itself (still at its OLD stale
            # position) as part of the active set it is being appended
            # after, which could produce a non-deterministic or
            # off-by-one result depending on where that stale position
            # happened to fall.
            links = list(self.conn.execute(
                "SELECT publication_id FROM publication_topics WHERE topic_id=?", (topic_id,)
            ))
            repositioned: dict[str, int] = {}
            for link in links:
                pub_id = link["publication_id"]
                max_row = self.conn.execute(
                    """SELECT MAX(pt.position) AS m FROM publication_topics pt
                       JOIN topics t ON t.topic_id=pt.topic_id
                       WHERE pt.publication_id=? AND t.archived_at IS NULL AND pt.topic_id<>?""",
                    (pub_id, topic_id),
                ).fetchone()
                new_position = int(max_row["m"]) + 1 if max_row["m"] is not None else 1
                repositioned[pub_id] = new_position
            self.conn.execute("UPDATE topics SET archived_at=NULL WHERE topic_id=?", (topic_id,))
            for pub_id, new_position in repositioned.items():
                self.conn.execute(
                    "UPDATE publication_topics SET position=? WHERE publication_id=? AND topic_id=?",
                    (new_position, pub_id, topic_id),
                )
            self.repo.audit_event("topic", topic_id, "RESTORE", actor, {"repositioned_in_publications": repositioned})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"topic_id": topic_id, "repositioned_in_publications": repositioned}

    def archive_block(self, block_id: str, *, actor: str) -> None:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per archiviare.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT archived_at FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(block_id)
            if row["archived_at"]:
                self.conn.rollback()
                raise ValueError(f"Il blocco {block_id} è già archiviato.")
            self.conn.execute("UPDATE content_blocks SET archived_at=? WHERE block_id=?", (now, block_id))
            self.repo.audit_event("block", block_id, "ARCHIVE", actor, {})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def restore_block(self, block_id: str, *, actor: str) -> dict[str, Any]:
        """Restore semantics (explicit, deterministic, audited): a restored
        block is appended AFTER every currently-active block in its topic
        (new position = max(active positions) + 1, or 1 if none are
        active). It is deliberately NOT reinserted at its old pre-archive
        position: reorder_blocks() only ever touches active blocks, so an
        archived block's stale position can easily collide with (or fall
        inside) the renumbered active range by the time it is restored --
        exactly the risk this method closes. 'Append at the end' is simple,
        collision-free by construction (strictly greater than every active
        position, computed fresh under the same lock that applies it), and
        never ambiguous. The writer can always reorder afterwards if a
        different spot is wanted -- that is what reorder_blocks() is for."""
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per ripristinare.")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT archived_at,position,topic_id FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(block_id)
            if not row["archived_at"]:
                self.conn.rollback()
                raise ValueError(f"Il blocco {block_id} non è archiviato.")
            max_row = self.conn.execute(
                "SELECT MAX(position) AS m FROM content_blocks WHERE topic_id=? AND archived_at IS NULL",
                (row["topic_id"],),
            ).fetchone()
            new_position = int(max_row["m"]) + 1 if max_row["m"] is not None else 1
            old_position = int(row["position"])
            self.conn.execute(
                "UPDATE content_blocks SET archived_at=NULL,position=? WHERE block_id=?", (new_position, block_id)
            )
            self.repo.audit_event("block", block_id, "RESTORE", actor,
                                  {"old_position": old_position, "new_position": new_position})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"block_id": block_id, "topic_id": row["topic_id"], "position": new_position}

    # ================================================================
    # Revert to a historical version -- never rewrites history. Restoring
    # old content is itself an edit: produces a NEW version, and is
    # subject to the exact same status-invalidation rule as any other save.
    # ================================================================

    def revert_topic_to_version(self, topic_id: str, version: int, *, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per un revert.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(topic_id)
            if live["archived_at"]:
                self.conn.rollback()
                raise ArchivedObjectError(f"Il topic {topic_id} è archiviato: ripristinalo prima di un revert.")
            hist = self.conn.execute(
                "SELECT * FROM topic_versions WHERE topic_id=? AND version=?", (topic_id, int(version))
            ).fetchone()
            if not hist:
                self.conn.rollback()
                raise KeyError((topic_id, version))
            previous_status = live["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(live["version"]) + 1
            cur = self.conn.execute(
                """UPDATE topics SET title=?,body=?,status=?,version=?,include_variants_json=?,
                   exclude_variants_json=?,source_refs_json=?,evidence_status=?,updated_at=?
                   WHERE topic_id=? AND version=?""",
                (hist["title"], hist["body"], new_status, new_version, hist["include_variants_json"],
                 hist["exclude_variants_json"], hist["source_refs_json"], hist["evidence_status"], now,
                 topic_id, int(live["version"])),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(topic_id, live["version"])
            self.conn.execute(
                """INSERT INTO topic_versions(topic_id,version,title,body,status,include_variants_json,
                   exclude_variants_json,source_refs_json,evidence_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (topic_id, new_version, hist["title"], hist["body"], new_status, hist["include_variants_json"],
                 hist["exclude_variants_json"], hist["source_refs_json"], hist["evidence_status"], now),
            )
            self._invalidate_open_review_if_edited("topic", topic_id, previous_status, new_status, actor)
            self.repo.audit_event("topic", topic_id, "REVERT_TO_VERSION", actor,
                                  {"reverted_from_version": int(live["version"]),
                                   "reverted_to_content_of_version": int(version), "new_version": new_version})
            self._delete_draft("topic", topic_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"topic_id": topic_id, "new_version": new_version,
                "reverted_to_content_of_version": int(version), "status": new_status}

    def revert_block_to_version(self, block_id: str, version: int, *, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per un revert.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(block_id)
            if live["archived_at"]:
                self.conn.rollback()
                raise ArchivedObjectError(f"Il blocco {block_id} è archiviato: ripristinalo prima di un revert.")
            hist = self.conn.execute(
                "SELECT * FROM block_versions WHERE block_id=? AND version=?", (block_id, int(version))
            ).fetchone()
            if not hist:
                self.conn.rollback()
                raise KeyError((block_id, version))
            previous_status = live["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(live["version"]) + 1
            cur = self.conn.execute(
                """UPDATE content_blocks SET text_content=?,status=?,version=?,condition_json=?,
                   source_refs_json=?,evidence_status=?,updated_at=? WHERE block_id=? AND version=?""",
                (hist["text_content"], new_status, new_version, hist["condition_json"], hist["source_refs_json"],
                 hist["evidence_status"], now, block_id, int(live["version"])),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(block_id, live["version"])
            self.conn.execute(
                """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,
                   object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, new_version, live["topic_id"], live["block_type"], int(live["position"]),
                 hist["text_content"], live["object_ref_id"], new_status, hist["condition_json"],
                 hist["source_refs_json"], hist["evidence_status"], now),
            )
            self._invalidate_open_review_if_edited("block", block_id, previous_status, new_status, actor)
            self.repo.audit_event("block", block_id, "REVERT_TO_VERSION", actor,
                                  {"reverted_from_version": int(live["version"]),
                                   "reverted_to_content_of_version": int(version), "new_version": new_version})
            self._delete_draft("block", block_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"block_id": block_id, "new_version": new_version,
                "reverted_to_content_of_version": int(version), "status": new_status}

    # ================================================================
    # Bulk source approval (legacy/import-batch path) -- reconciles the
    # 4B.1 review-lifecycle bug: approve_source_ui trusts a whole freshly-
    # imported publication at once (a different use case from a reviewed
    # per-object decision, so it deliberately keeps bypassing
    # request_review/decide_review for the approval itself) but must not
    # leave an OPEN review_tasks row behind for anything it just approved.
    # ================================================================

    def bulk_approve_publication(self, publication_id: str, *, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per approvare.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            tids = [r["topic_id"] for r in self.conn.execute(
                "SELECT topic_id FROM publication_topics WHERE publication_id=?", (publication_id,)
            )]
            if not tids:
                self.conn.rollback()
                raise ValueError("Pubblicazione non trovata o vuota.")
            closed = 0
            for tid in tids:
                self.conn.execute("UPDATE topics SET status='Approved',updated_at=? WHERE topic_id=?", (now, tid))
                bids = [r["block_id"] for r in self.conn.execute(
                    "SELECT block_id FROM content_blocks WHERE topic_id=?", (tid,)
                )]
                self.conn.execute("UPDATE content_blocks SET status='Approved',updated_at=? WHERE topic_id=?", (now, tid))
                for object_type, object_id in [("topic", tid)] + [("block", b) for b in bids]:
                    open_rows = self.conn.execute(
                        "SELECT review_id FROM review_tasks WHERE object_type=? AND object_id=? AND status='OPEN'",
                        (object_type, object_id),
                    ).fetchall()
                    for r in open_rows:
                        rid = int(r["review_id"])
                        self.conn.execute(
                            "UPDATE review_tasks SET status='CLOSED',decision='APPROVED_VIA_BULK',comment=?,decided_at=? WHERE review_id=?",
                            ("Closed by bulk source approval", now, rid),
                        )
                        self.repo.audit_event(object_type, object_id, "REVIEW_APPROVED_VIA_BULK", actor, {"review_id": rid})
                        closed += 1
            self.repo.audit_event("publication", publication_id, "SOURCE_HUMAN_APPROVED", actor,
                                  {"topics": len(tids), "review_tasks_closed": closed})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"topics_approved": len(tids), "review_tasks_closed": closed}

    # ================================================================
    # 4B.2: topic/block CREATION with backend-generated IDs -- the editor
    # never asks a technical writer for an ID. Both follow the same
    # two-step shape: (1) call the existing, already-tested
    # IndustrialRepository create_topic()/add_block() primitive (which
    # commits on its own -- reused, never re-implemented here), then (2) a
    # SEPARATE atomic step places the new object at its deterministic
    # position (after a chosen sibling, or at the end), renumbering only
    # the active siblings, exactly like reorder_blocks()/
    # reorder_publication_topics() already do. This is NOT a single
    # all-or-nothing transaction across both steps (repo.create_topic()/
    # add_block() own their own commit and cannot be folded into this
    # service's BEGIN IMMEDIATE without breaking atomicity the same way
    # replace_publication_topics() would -- see reorder_publication_topics()'s
    # docstring for the identical reasoning). The residual risk is narrow
    # and low-severity: if step 2 fails, the new topic/block row exists but
    # is not yet linked/positioned -- an orphan, invisible to assembly and
    # outline, never a corrupted read for anyone else. Documented, not
    # hidden.
    # ================================================================

    def create_topic_in_publication(self, publication_id: str, title: str, *, actor: str,
                                    after_topic_id: str | None = None, source_lang: str = "en-US") -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per creare un topic.")
        title = str(title or "").strip()
        if not title:
            raise ValueError("Il titolo del topic è obbligatorio.")
        if not self.conn.execute("SELECT 1 FROM publications WHERE publication_id=?", (publication_id,)).fetchone():
            raise KeyError(publication_id)

        topic_id = f"TOPIC-{uuid4().hex[:10].upper()}"
        while self.conn.execute("SELECT 1 FROM topics WHERE topic_id=?", (topic_id,)).fetchone():
            topic_id = f"TOPIC-{uuid4().hex[:10].upper()}"
        self.repo.create_topic(topic_id, title, "", source_lang=source_lang, status="Draft")
        self.repo.audit_event("topic", topic_id, "AUTHORING_CREATE", actor, {"title": title, "publication_id": publication_id})
        self.conn.commit()  # audit_event() itself doesn't commit; this closes out step 1 before step 2's own BEGIN IMMEDIATE.

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            active_rows = list(self.conn.execute(
                """SELECT pt.topic_id FROM publication_topics pt
                   JOIN topics t ON t.topic_id=pt.topic_id
                   WHERE pt.publication_id=? AND t.archived_at IS NULL ORDER BY pt.position""",
                (publication_id,),
            ))
            active_ids = [r["topic_id"] for r in active_rows]
            if after_topic_id and after_topic_id in active_ids:
                insert_at = active_ids.index(after_topic_id) + 1
            else:
                insert_at = len(active_ids)
            new_order = active_ids[:insert_at] + [topic_id] + active_ids[insert_at:]
            for pos, tid in enumerate(new_order, start=1):
                if tid == topic_id:
                    self.conn.execute(
                        "INSERT INTO publication_topics(publication_id,topic_id,position) VALUES(?,?,?)",
                        (publication_id, tid, pos),
                    )
                else:
                    self.conn.execute(
                        "UPDATE publication_topics SET position=? WHERE publication_id=? AND topic_id=?",
                        (pos, publication_id, tid),
                    )
            position = new_order.index(topic_id) + 1
            self.repo.audit_event("publication", publication_id, "ADD_TOPIC", actor,
                                  {"topic_id": topic_id, "after_topic_id": after_topic_id, "position": position})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"topic_id": topic_id, "publication_id": publication_id, "position": position}

    def create_block_in_topic(self, topic_id: str, block_type: str, *, actor: str, text: str = "",
                              after_block_id: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per creare un blocco.")
        bt = str(block_type or "").strip().lower()
        if bt not in EDITABLE_TEXT_BLOCK_TYPES:
            raise ValueError(f"Tipo blocco non creabile dall'editor: {bt!r} (ammessi: {sorted(EDITABLE_TEXT_BLOCK_TYPES)}).")
        topic_row = self.conn.execute("SELECT archived_at FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if not topic_row:
            raise KeyError(topic_id)
        if topic_row["archived_at"]:
            raise ArchivedObjectError(f"Il topic {topic_id} è archiviato: ripristinalo prima di aggiungere blocchi.")

        block_id = f"BLK-{uuid4().hex[:10].upper()}"
        while self.conn.execute("SELECT 1 FROM content_blocks WHERE block_id=?", (block_id,)).fetchone():
            block_id = f"BLK-{uuid4().hex[:10].upper()}"
        # Placeholder position: real placement happens atomically below. Any
        # int works here since add_block()'s own commit is immediately
        # followed by the renumbering step before anyone else observes it
        # through the outline/assembly (both order by position,block_id).
        self.repo.add_block(block_id, topic_id, bt, position=1_000_000, text=text, status="Draft")
        self.repo.audit_event("block", block_id, "AUTHORING_CREATE", actor, {"block_type": bt, "topic_id": topic_id})
        self.conn.commit()

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            active_rows = list(self.conn.execute(
                "SELECT block_id FROM content_blocks WHERE topic_id=? AND archived_at IS NULL ORDER BY position,block_id",
                (topic_id,),
            ))
            active_ids = [r["block_id"] for r in active_rows if r["block_id"] != block_id]
            if after_block_id and after_block_id in active_ids:
                insert_at = active_ids.index(after_block_id) + 1
            else:
                insert_at = len(active_ids)
            new_order = active_ids[:insert_at] + [block_id] + active_ids[insert_at:]
            now = _now()
            for pos, bid in enumerate(new_order, start=1):
                self.conn.execute("UPDATE content_blocks SET position=?,updated_at=? WHERE block_id=?", (pos, now, bid))
            position = new_order.index(block_id) + 1
            self.repo.audit_event("topic", topic_id, "ADD_BLOCK", actor,
                                  {"block_id": block_id, "after_block_id": after_block_id, "position": position})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"block_id": block_id, "topic_id": topic_id, "block_type": bt, "position": position}

    # ================================================================
    # 4B.2: read-only helpers for the editor UI -- version history and
    # open-review lookup. Pure reads, no new business rule; every field
    # comes straight from *_versions/review_tasks, cross-referenced with
    # audit_events on a best-effort basis for the actor (a version row
    # itself never stores who saved it -- see the 4B.1 Integrity Gate
    # finding on content-version vs workflow-status semantics: *_versions
    # is frozen content+status-at-save-time, never a workflow ledger by
    # itself).
    # ================================================================

    def topic_history(self, topic_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version,title,status,created_at FROM topic_versions WHERE topic_id=? ORDER BY version DESC",
            (topic_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "version": int(r["version"]), "title": r["title"], "status": r["status"],
                "created_at": r["created_at"], "actor": self._actor_for_version("topic", topic_id, int(r["version"])),
            })
        return out

    def block_history(self, block_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version,text_content,status,created_at FROM block_versions WHERE block_id=? ORDER BY version DESC",
            (block_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "version": int(r["version"]), "text": r["text_content"], "status": r["status"],
                "created_at": r["created_at"], "actor": self._actor_for_version("block", block_id, int(r["version"])),
            })
        return out

    def _actor_for_version(self, object_type: str, object_id: str, version: int) -> str:
        for row in self.conn.execute(
            """SELECT actor, detail_json FROM audit_events
               WHERE object_type=? AND object_id=? AND action IN ('AUTHORING_SAVE','REVERT_TO_VERSION','AUTHORING_CREATE')
               ORDER BY event_id DESC""",
            (object_type, object_id),
        ):
            detail = _load_json(row["detail_json"], {})
            if detail.get("to_version") == version or detail.get("new_version") == version or (version == 1 and not detail):
                return str(row["actor"])
        return ""

    def open_review_for(self, object_type: str, object_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM review_tasks WHERE object_type=? AND object_id=? AND status='OPEN' ORDER BY created_at DESC LIMIT 1",
            (object_type, object_id),
        ).fetchone()
        return dict(row) if row else None

    # ================================================================
    # 4C: table/figure authoring. Reuses the EXISTING structured_tables/
    # figures schema and IndustrialRepository.create_table()/create_figure()
    # /request_review()/decide_review() primitives completely unchanged --
    # no second table/figure model, no parallel review workflow. Same CAS/
    # draft/audit/status-invalidation shape as save_topic/save_block above,
    # just targeting structured_tables/figures instead of topics/
    # content_blocks. Neither object type has its own archived_at column
    # (only the content_blocks row that references it does, via
    # object_ref_id) -- editing is blocked when that REFERENCING block is
    # archived, the same "restore it first" meaning ArchivedObjectError
    # already carries for topics/blocks.
    #
    # Shared-object protection (temporary, until 4D's reuse-aware fork/
    # update-shared UX): before any table/figure save/revert, this counts
    # how many content_blocks rows reference the object (not
    # impact_map_industrial()'s topic/publication-row count, which would
    # over-count an object used by exactly one block in a topic that
    # belongs to two publications -- the question here is "is this object
    # wired into more than one point of the content structure", which is a
    # block-level fact). More than one reference -> SharedObjectError,
    # fail-closed, no silent global edit.
    # ================================================================

    def _referencing_block(self, object_type: str, object_id: str):
        bt = {"table": "table", "figure": "figure"}[object_type]
        return self.conn.execute(
            "SELECT * FROM content_blocks WHERE block_type=? AND object_ref_id=? ORDER BY block_id LIMIT 1",
            (bt, object_id),
        ).fetchone()

    def _reference_count(self, object_type: str, object_id: str) -> int:
        bt = {"table": "table", "figure": "figure"}[object_type]
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM content_blocks WHERE block_type=? AND object_ref_id=?",
            (bt, object_id),
        ).fetchone()
        return int(row["c"])

    def _guard_table_or_figure_editable(self, object_type: str, object_id: str) -> None:
        """Shared by save/revert: archived-referencing-block check first
        (an archived object being edited at all is the more surprising/
        dangerous case), then the shared-object guard. Called from inside
        a caller's own BEGIN IMMEDIATE/try block; raises straight through
        to that block's own `except Exception: self.conn.rollback(); raise`
        -- the same pattern every early-exit check elsewhere in this file
        already relies on (a rollback() with no active transaction is a
        proven-safe no-op here, not a second, redundant one)."""
        block = self._referencing_block(object_type, object_id)
        if block is not None and block["archived_at"]:
            label = "tabella" if object_type == "table" else "figura"
            raise ArchivedObjectError(
                f"Questa {label} è collegata a un blocco archiviato: ripristina il blocco prima di modificarla."
            )
        if self._reference_count(object_type, object_id) > 1:
            raise SharedObjectError(_SHARED_OBJECT_MESSAGE)

    def _project_dir(self) -> Path:
        return Path(self.repo.path).parent

    def _materialize_uploaded_asset(self, source_path: str | Path, *, subdir: str = "figures") -> Path:
        """Collision-safe copy of a user-uploaded file into the project's
        own Assets/<subdir>/ store -- same spirit as idml_import.
        materialize_figure_assets (never fabricate a path, never silently
        overwrite a different file's bytes), simplified for a single
        incoming upload: a short random prefix makes the destination
        filename unique by construction, so no existence-probing/rename
        loop is needed. Returns the new file's ABSOLUTE path -- the same
        convention IDML-imported figures already store in
        figures.file_path (see idml_import.materialize_figure_assets),
        so preview/consumption code does not need two path conventions."""
        source_path = Path(source_path)
        if not source_path.exists():
            raise FileNotFoundError(f"File non trovato: {source_path}")
        assets_dir = self._project_dir() / "Assets" / subdir
        assets_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_path.name).strip("_") or "asset"
        dest = assets_dir / f"{uuid4().hex[:10]}_{safe_name}"
        shutil.copy2(source_path, dest)
        return dest

    # ---- table save/create ----

    def save_table(self, table_id: str, *, title: str | None = None, caption: str | None = None,
                   columns: list[str] | None = None, rows: list[list[Any]] | None = None,
                   header_rows: int | None = None, expected_version: int, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per salvare una tabella.")
        if columns is not None and not columns:
            raise ValueError("La tabella deve avere almeno una colonna.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(table_id)
            self._guard_table_or_figure_editable("table", table_id)
            new_title = row["title"] if title is None else title.strip()
            new_caption = row["caption"] if caption is None else caption.strip()
            new_columns = _load_json(row["columns_json"], []) if columns is None else [str(c) for c in columns]
            new_rows = _load_json(row["rows_json"], []) if rows is None else [[c for c in r] for r in rows]
            new_header_rows = int(row["header_rows"]) if header_rows is None else int(header_rows)
            previous_status = row["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(expected_version) + 1
            source_refs = row["source_refs_json"]
            condition = row["condition_json"]

            cur = self.conn.execute(
                """UPDATE structured_tables SET title=?,caption=?,columns_json=?,rows_json=?,header_rows=?,
                   status=?,version=?,updated_at=? WHERE table_id=? AND version=?""",
                (new_title, new_caption, _json(new_columns), _json(new_rows), new_header_rows,
                 new_status, new_version, now, table_id, int(expected_version)),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(table_id, expected_version)
            self.conn.execute(
                """INSERT INTO structured_table_versions(table_id,version,title,caption,columns_json,rows_json,
                   header_rows,status,condition_json,source_refs_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (table_id, new_version, new_title, new_caption, _json(new_columns), _json(new_rows),
                 new_header_rows, new_status, condition, source_refs, now),
            )
            invalidated = self._invalidate_open_review_if_edited("table", table_id, previous_status, new_status, actor)
            self.repo.audit_event("table", table_id, "AUTHORING_SAVE", actor,
                                  {"from_version": int(expected_version), "to_version": new_version,
                                   "previous_status": previous_status, "new_status": new_status})
            self._delete_draft("table", table_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"table_id": table_id, "version": new_version, "status": new_status,
                "previous_status": previous_status, "review_tasks_invalidated": invalidated}

    def create_table_in_topic(self, topic_id: str, *, actor: str, title: str = "", caption: str = "",
                              columns: list[str] | None = None, rows: list[list[Any]] | None = None,
                              header_rows: int = 1, after_block_id: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per creare una tabella.")
        columns = [str(c) for c in columns] if columns else ["Colonna 1", "Colonna 2"]
        rows = [[c for c in r] for r in rows] if rows else [["" for _ in columns]]
        topic_row = self.conn.execute("SELECT archived_at FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if not topic_row:
            raise KeyError(topic_id)
        if topic_row["archived_at"]:
            raise ArchivedObjectError(f"Il topic {topic_id} è archiviato: ripristinalo prima di aggiungere una tabella.")

        table_id = f"TBL-{uuid4().hex[:10].upper()}"
        while self.conn.execute("SELECT 1 FROM structured_tables WHERE table_id=?", (table_id,)).fetchone():
            table_id = f"TBL-{uuid4().hex[:10].upper()}"
        self.repo.create_table(table_id, columns, rows, title=title.strip(), caption=caption.strip(),
                               header_rows=header_rows, status="Draft")
        self.repo.audit_event("table", table_id, "AUTHORING_CREATE", actor, {"title": title, "topic_id": topic_id})
        self.conn.commit()

        block_id = f"BLK-{uuid4().hex[:10].upper()}"
        while self.conn.execute("SELECT 1 FROM content_blocks WHERE block_id=?", (block_id,)).fetchone():
            block_id = f"BLK-{uuid4().hex[:10].upper()}"
        self.repo.add_block(block_id, topic_id, "table", position=1_000_000, object_ref_id=table_id, status="Draft")
        self.repo.audit_event("block", block_id, "AUTHORING_CREATE", actor,
                              {"block_type": "table", "topic_id": topic_id, "object_ref_id": table_id})
        self.conn.commit()

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            active_rows = list(self.conn.execute(
                "SELECT block_id FROM content_blocks WHERE topic_id=? AND archived_at IS NULL ORDER BY position,block_id",
                (topic_id,),
            ))
            active_ids = [r["block_id"] for r in active_rows if r["block_id"] != block_id]
            if after_block_id and after_block_id in active_ids:
                insert_at = active_ids.index(after_block_id) + 1
            else:
                insert_at = len(active_ids)
            new_order = active_ids[:insert_at] + [block_id] + active_ids[insert_at:]
            now = _now()
            for pos, bid in enumerate(new_order, start=1):
                self.conn.execute("UPDATE content_blocks SET position=?,updated_at=? WHERE block_id=?", (pos, now, bid))
            position = new_order.index(block_id) + 1
            self.repo.audit_event("topic", topic_id, "ADD_BLOCK", actor,
                                  {"block_id": block_id, "after_block_id": after_block_id, "position": position})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"table_id": table_id, "block_id": block_id, "topic_id": topic_id, "position": position}

    def revert_table_to_version(self, table_id: str, version: int, *, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per un revert.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(table_id)
            self._guard_table_or_figure_editable("table", table_id)
            hist = self.conn.execute(
                "SELECT * FROM structured_table_versions WHERE table_id=? AND version=?", (table_id, int(version))
            ).fetchone()
            if not hist:
                self.conn.rollback()
                raise KeyError((table_id, version))
            previous_status = live["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(live["version"]) + 1
            cur = self.conn.execute(
                """UPDATE structured_tables SET title=?,caption=?,columns_json=?,rows_json=?,header_rows=?,
                   status=?,version=?,updated_at=? WHERE table_id=? AND version=?""",
                (hist["title"], hist["caption"], hist["columns_json"], hist["rows_json"], hist["header_rows"],
                 new_status, new_version, now, table_id, int(live["version"])),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(table_id, live["version"])
            self.conn.execute(
                """INSERT INTO structured_table_versions(table_id,version,title,caption,columns_json,rows_json,
                   header_rows,status,condition_json,source_refs_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (table_id, new_version, hist["title"], hist["caption"], hist["columns_json"], hist["rows_json"],
                 hist["header_rows"], new_status, hist["condition_json"], hist["source_refs_json"], now),
            )
            self._invalidate_open_review_if_edited("table", table_id, previous_status, new_status, actor)
            self.repo.audit_event("table", table_id, "REVERT_TO_VERSION", actor,
                                  {"reverted_from_version": int(live["version"]),
                                   "reverted_to_content_of_version": int(version), "new_version": new_version})
            self._delete_draft("table", table_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"table_id": table_id, "new_version": new_version,
                "reverted_to_content_of_version": int(version), "status": new_status}

    def table_history(self, table_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version,title,caption,status,created_at FROM structured_table_versions WHERE table_id=? ORDER BY version DESC",
            (table_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "version": int(r["version"]), "title": r["title"], "caption": r["caption"], "status": r["status"],
                "created_at": r["created_at"], "actor": self._actor_for_version("table", table_id, int(r["version"])),
            })
        return out

    # ---- figure save/create/replace ----

    def save_figure(self, figure_id: str, *, title: str | None = None, caption: str | None = None,
                    alt_text: str | None = None, file_path: str | None = None,
                    expected_version: int, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per salvare una figura.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(figure_id)
            self._guard_table_or_figure_editable("figure", figure_id)
            new_title = row["title"] if title is None else title.strip()
            new_caption = row["caption"] if caption is None else caption.strip()
            new_alt = row["alt_text"] if alt_text is None else alt_text.strip()
            new_path = row["file_path"] if file_path is None else str(file_path).strip()
            previous_status = row["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(expected_version) + 1
            source_refs = row["source_refs_json"]
            condition = row["condition_json"]

            cur = self.conn.execute(
                """UPDATE figures SET title=?,caption=?,file_path=?,alt_text=?,status=?,version=?,updated_at=?
                   WHERE figure_id=? AND version=?""",
                (new_title, new_caption, new_path, new_alt, new_status, new_version, now, figure_id, int(expected_version)),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(figure_id, expected_version)
            self.conn.execute(
                """INSERT INTO figure_versions(figure_id,version,title,caption,file_path,alt_text,status,
                   condition_json,source_refs_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (figure_id, new_version, new_title, new_caption, new_path, new_alt, new_status, condition, source_refs, now),
            )
            invalidated = self._invalidate_open_review_if_edited("figure", figure_id, previous_status, new_status, actor)
            self.repo.audit_event("figure", figure_id, "AUTHORING_SAVE", actor,
                                  {"from_version": int(expected_version), "to_version": new_version,
                                   "previous_status": previous_status, "new_status": new_status,
                                   "file_path_changed": file_path is not None})
            self._delete_draft("figure", figure_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"figure_id": figure_id, "version": new_version, "status": new_status,
                "previous_status": previous_status, "review_tasks_invalidated": invalidated}

    def replace_figure_image(self, figure_id: str, *, source_file_path: str | Path,
                             expected_version: int, actor: str) -> dict[str, Any]:
        """Materializes the new file FIRST (fail-closed: if the copy itself
        fails, nothing about the figure has changed at all), then does a
        normal CAS save_figure() with only file_path set -- caption/alt/
        title are left untouched (None) unless the caller also passes a
        change. If the save fails for ANY reason (conflict/archived/
        shared/missing), the live figure row is guaranteed untouched
        (save_figure() rolled back its own transaction before raising) --
        the only remaining cleanup is the orphaned copy on disk, deleted
        here so no unclassified asset is left behind."""
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per sostituire un'immagine.")
        if not self.conn.execute("SELECT 1 FROM figures WHERE figure_id=?", (figure_id,)).fetchone():
            raise KeyError(figure_id)
        materialized_path = self._materialize_uploaded_asset(source_file_path)
        try:
            result = self.save_figure(figure_id, file_path=str(materialized_path),
                                      expected_version=expected_version, actor=actor)
        except Exception:
            materialized_path.unlink(missing_ok=True)
            raise
        return result

    def create_figure_in_topic(self, topic_id: str, *, actor: str, source_file_path: str | Path,
                               title: str = "", caption: str = "", alt_text: str = "",
                               after_block_id: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per aggiungere una figura.")
        topic_row = self.conn.execute("SELECT archived_at FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if not topic_row:
            raise KeyError(topic_id)
        if topic_row["archived_at"]:
            raise ArchivedObjectError(f"Il topic {topic_id} è archiviato: ripristinalo prima di aggiungere una figura.")

        # Materialize BEFORE any DB write: if this fails, nothing partial
        # exists yet at all -- genuinely fail-closed, not a compensation.
        materialized_path = self._materialize_uploaded_asset(source_file_path)
        try:
            figure_id = f"FIG-{uuid4().hex[:10].upper()}"
            while self.conn.execute("SELECT 1 FROM figures WHERE figure_id=?", (figure_id,)).fetchone():
                figure_id = f"FIG-{uuid4().hex[:10].upper()}"
            self.repo.create_figure(figure_id, title=title.strip(), caption=caption.strip(),
                                    file_path=str(materialized_path), alt_text=alt_text.strip(), status="Draft")
        except Exception:
            materialized_path.unlink(missing_ok=True)
            raise
        self.repo.audit_event("figure", figure_id, "AUTHORING_CREATE", actor, {"title": title, "topic_id": topic_id})
        self.conn.commit()

        block_id = f"BLK-{uuid4().hex[:10].upper()}"
        while self.conn.execute("SELECT 1 FROM content_blocks WHERE block_id=?", (block_id,)).fetchone():
            block_id = f"BLK-{uuid4().hex[:10].upper()}"
        self.repo.add_block(block_id, topic_id, "figure", position=1_000_000, object_ref_id=figure_id, status="Draft")
        self.repo.audit_event("block", block_id, "AUTHORING_CREATE", actor,
                              {"block_type": "figure", "topic_id": topic_id, "object_ref_id": figure_id})
        self.conn.commit()

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            active_rows = list(self.conn.execute(
                "SELECT block_id FROM content_blocks WHERE topic_id=? AND archived_at IS NULL ORDER BY position,block_id",
                (topic_id,),
            ))
            active_ids = [r["block_id"] for r in active_rows if r["block_id"] != block_id]
            if after_block_id and after_block_id in active_ids:
                insert_at = active_ids.index(after_block_id) + 1
            else:
                insert_at = len(active_ids)
            new_order = active_ids[:insert_at] + [block_id] + active_ids[insert_at:]
            now = _now()
            for pos, bid in enumerate(new_order, start=1):
                self.conn.execute("UPDATE content_blocks SET position=?,updated_at=? WHERE block_id=?", (pos, now, bid))
            position = new_order.index(block_id) + 1
            self.repo.audit_event("topic", topic_id, "ADD_BLOCK", actor,
                                  {"block_id": block_id, "after_block_id": after_block_id, "position": position})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"figure_id": figure_id, "block_id": block_id, "topic_id": topic_id, "position": position,
                "file_path": str(materialized_path)}

    def revert_figure_to_version(self, figure_id: str, version: int, *, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per un revert.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(figure_id)
            self._guard_table_or_figure_editable("figure", figure_id)
            hist = self.conn.execute(
                "SELECT * FROM figure_versions WHERE figure_id=? AND version=?", (figure_id, int(version))
            ).fetchone()
            if not hist:
                self.conn.rollback()
                raise KeyError((figure_id, version))
            # The historical asset must still exist on disk before
            # reverting to it -- otherwise this would silently produce a
            # new "missing asset" figure, indistinguishable from data loss.
            if hist["file_path"] and not Path(hist["file_path"]).exists():
                self.conn.rollback()
                raise MissingAssetError(
                    f"L'immagine della versione {version} non è più disponibile su disco: impossibile ripristinare questa versione."
                )
            previous_status = live["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(live["version"]) + 1
            cur = self.conn.execute(
                """UPDATE figures SET title=?,caption=?,file_path=?,alt_text=?,status=?,version=?,updated_at=?
                   WHERE figure_id=? AND version=?""",
                (hist["title"], hist["caption"], hist["file_path"], hist["alt_text"], new_status, new_version,
                 now, figure_id, int(live["version"])),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(figure_id, live["version"])
            self.conn.execute(
                """INSERT INTO figure_versions(figure_id,version,title,caption,file_path,alt_text,status,
                   condition_json,source_refs_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (figure_id, new_version, hist["title"], hist["caption"], hist["file_path"], hist["alt_text"],
                 new_status, hist["condition_json"], hist["source_refs_json"], now),
            )
            self._invalidate_open_review_if_edited("figure", figure_id, previous_status, new_status, actor)
            self.repo.audit_event("figure", figure_id, "REVERT_TO_VERSION", actor,
                                  {"reverted_from_version": int(live["version"]),
                                   "reverted_to_content_of_version": int(version), "new_version": new_version})
            self._delete_draft("figure", figure_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"figure_id": figure_id, "new_version": new_version,
                "reverted_to_content_of_version": int(version), "status": new_status}

    def figure_history(self, figure_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version,title,caption,file_path,status,created_at FROM figure_versions WHERE figure_id=? ORDER BY version DESC",
            (figure_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "version": int(r["version"]), "title": r["title"], "caption": r["caption"],
                "file_path": r["file_path"], "status": r["status"], "created_at": r["created_at"],
                "actor": self._actor_for_version("figure", figure_id, int(r["version"])),
            })
        return out
