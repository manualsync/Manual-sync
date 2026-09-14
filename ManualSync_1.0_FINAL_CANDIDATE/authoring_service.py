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

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import domain_errors as de
from domain_errors import DomainFileMissingError, DomainValidationError
from ccms_industrial import IndustrialRepository
import rich_text

AUTHORING_SERVICE_BUILD = "0.4.0-authoring-reuse-revision-4d-20260903"

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
    by more than one content_blocks row, and the caller has not passed
    confirm_shared=True. 4D: this is no longer a dead end -- the caller has
    two ways past it, both explicit and audited: retry with
    confirm_shared=True ('Aggiorna ovunque', see _guard_table_or_figure_
    editable) or fork_table_local()/fork_figure_local() ('Crea una copia
    locale'). Until one of those is chosen, fail-closed: never silently
    save a global edit no one asked for."""


class MissingAssetError(Exception):
    """4C: raised when a figure revert would restore a historical
    file_path whose bytes no longer exist on disk. Reverting to a missing
    asset would silently produce a new 'broken image' figure -- fail
    closed instead, with a message that says exactly why."""


_SHARED_OBJECT_MESSAGE = (
    "Questo elemento è utilizzato in più punti. Scegli come procedere: «Aggiorna ovunque» oppure "
    "«Crea una copia locale»."
)

# 4D integrity gate: a save/revert confirmed via 'Aggiorna ovunque' is only
# ever safe if the reference SET it is about to apply to is exactly the one
# the user actually previewed -- expected_version alone does not catch a
# block being added to or removed from that set between the preview and
# the Save click (the object's own version can stay identical while its
# reference set changes). See get_reuse_impact()'s "impact_token" and
# _guard_table_or_figure_editable()'s confirm_shared branch.
_IMPACT_DRIFT_MESSAGE = (
    "Gli utilizzi di questo elemento sono cambiati dopo l'anteprima. Riapri il confronto prima di salvare."
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
            raise DomainValidationError(de.ERR_AUTHORING_DRAFT_KIND_UNSUPPORTED,
                                        allowed=sorted(_EDITABLE_KINDS))
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            "condition": _load_json(row["condition_json"], {}),
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
            "condition": _load_json(row["condition_json"], {}),
            "object_ref_id": row["object_ref_id"],
            "text_format": row["text_format"],
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
                   source_refs: Iterable[str] | None = None,
                   condition: dict[str, Iterable[str]] | None = None,
                   expected_version: int, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            previous_condition_json = row["condition_json"]
            if condition is None:
                new_condition_json = previous_condition_json
                condition_changed = False
                compiled_condition: dict[str, Any] = {}
            else:
                compiled_condition, _normalized = self._compile_and_validate_condition(condition)
                new_condition_json = _json(compiled_condition)
                condition_changed = new_condition_json != previous_condition_json

            cur = self.conn.execute(
                """UPDATE topics SET title=?,body=?,status=?,version=?,source_refs_json=?,condition_json=?,updated_at=?
                   WHERE topic_id=? AND version=?""",
                (new_title, new_body, new_status, new_version, _json(new_refs), new_condition_json, now,
                 topic_id, int(expected_version)),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(topic_id, expected_version)
            self.conn.execute(
                """INSERT INTO topic_versions(topic_id,version,title,body,status,include_variants_json,
                   exclude_variants_json,source_refs_json,condition_json,evidence_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (topic_id, new_version, new_title, new_body, new_status, include, exclude, _json(new_refs),
                 new_condition_json, evidence, now),
            )
            invalidated = self._invalidate_open_review_if_edited("topic", topic_id, previous_status, new_status, actor)
            self.repo.audit_event("topic", topic_id, "AUTHORING_SAVE", actor,
                                  {"from_version": int(expected_version), "to_version": new_version,
                                   "previous_status": previous_status, "new_status": new_status})
            if condition_changed:
                self.repo.audit_event("topic", topic_id, "AUTHORING_CONDITIONS_UPDATED", actor,
                                      {"version": new_version, "condition": compiled_condition})
            self._delete_draft("topic", topic_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"topic_id": topic_id, "version": new_version, "status": new_status,
                "previous_status": previous_status, "review_tasks_invalidated": invalidated,
                "condition_changed": condition_changed}

    # ================================================================
    # Atomic save (compare-and-swap) -- block
    # ================================================================

    def save_block(self, block_id: str, *, text: str | None = None, source_refs: Iterable[str] | None = None,
                   condition: dict[str, Iterable[str]] | None = None, text_format: str | None = None,
                   expected_version: int, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            # 4E.3: text_format=None means "leave this block's stored
            # format exactly as it already is" -- every pre-4E.3 caller
            # (this whole codebase's other suites included) omits it and
            # is completely unaffected.
            #
            # text_format="auto" is the STABILIZATION fix for Blocker 2
            # (a plain paragraph must not be silently promoted just
            # because the save came through the rich-aware editor): an
            # already-'rich' block stays 'rich' unconditionally (no
            # silent demotion either); a 'plain' block is promoted ONLY
            # if the saved text genuinely introduces rich structure
            # (rich_text.has_rich_structure() -- a real mark, a link, or
            # a list; plain multi-line text alone never counts). When it
            # is NOT promoted, the text actually stored is the UNESCAPED
            # form: the client-side DOM->canonical serializer always
            # backslash-escapes literal markup-like characters (see
            # rich_text.py's module docstring), but 'plain' format never
            # runs the parser/unescaper on read -- so if this save stays
            # 'plain', the escape must be undone HERE, once, at write
            # time, or a legacy-format reader would show a literal
            # stray backslash the writer never typed.
            if text_format is None:
                new_text_format = row["text_format"]
            elif str(text_format).strip().lower() == "auto":
                if row["text_format"] == "rich":
                    new_text_format = "rich"
                elif text is not None and rich_text.has_rich_structure(new_text):
                    new_text_format = "rich"
                else:
                    new_text_format = "plain"
                    if text is not None:
                        new_text = rich_text.unescape_literal(new_text)
            else:
                tf = str(text_format).strip().lower()
                new_text_format = tf if tf in ("plain", "rich") else row["text_format"]
            new_refs = _load_json(row["source_refs_json"], []) if source_refs is None else _norm_set(source_refs)
            previous_status = row["status"]
            new_status = self._status_after_edit(previous_status)
            new_version = int(expected_version) + 1
            evidence = row["evidence_status"]
            object_ref_id = row["object_ref_id"]
            previous_condition_json = row["condition_json"]
            if condition is None:
                new_condition_json = previous_condition_json
                condition_changed = False
                compiled_condition: dict[str, Any] = {}
            else:
                # NOTE: this ALWAYS targets this block's own condition_json,
                # never a referenced table/figure's -- 4E.1's reuse rule:
                # conditions belong to the block PLACEMENT, not the shared
                # object (structured_tables/figures keep their own
                # condition_json column untouched by this method, so a
                # table shared by two blocks can carry two different
                # placement conditions without the shared table itself
                # ever being written to here).
                compiled_condition, _normalized = self._compile_and_validate_condition(condition)
                new_condition_json = _json(compiled_condition)
                condition_changed = new_condition_json != previous_condition_json

            cur = self.conn.execute(
                """UPDATE content_blocks SET text_content=?,status=?,version=?,source_refs_json=?,condition_json=?,updated_at=?,text_format=?
                   WHERE block_id=? AND version=?""",
                (new_text, new_status, new_version, _json(new_refs), new_condition_json, now, new_text_format, block_id, int(expected_version)),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(block_id, expected_version)
            self.conn.execute(
                """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,
                   object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at,text_format)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, new_version, row["topic_id"], row["block_type"], int(row["position"]), new_text,
                 object_ref_id, new_status, new_condition_json, _json(new_refs), evidence, now, new_text_format),
            )
            invalidated = self._invalidate_open_review_if_edited("block", block_id, previous_status, new_status, actor)
            self.repo.audit_event("block", block_id, "AUTHORING_SAVE", actor,
                                  {"from_version": int(expected_version), "to_version": new_version,
                                   "previous_status": previous_status, "new_status": new_status})
            if condition_changed:
                self.repo.audit_event("block", block_id, "AUTHORING_CONDITIONS_UPDATED", actor,
                                      {"version": new_version, "condition": compiled_condition})
            self._delete_draft("block", block_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"block_id": block_id, "version": new_version, "status": new_status,
                "previous_status": previous_status, "review_tasks_invalidated": invalidated,
                "condition_changed": condition_changed}

    # ================================================================
    # Conditions & Variants -- 4E.1 foundation.
    #
    # Deliberately NOT a new evaluation engine: IndustrialRepository.
    # condition_matches() (ccms_industrial.py) already implements a fully
    # general boolean-tree evaluator (all/any/not + eq/neq/in/not_in/
    # contains/contains_any/exists) and is already wired into
    # assemble_structured_publication() for both topics and blocks. The
    # 4E.1 ticket explicitly forbids exposing that generality to authors
    # (no NOT, no nesting, no DSL) -- so this layer only ever produces and
    # understands a restricted compiled SHAPE of that same JSON:
    #   {}                                            -- no restriction
    #   {"field": dim, "op": "in", "value": [v, ...]}  -- one dimension
    #   {"all": [<leaf>, <leaf>, ...]}                 -- 2+ dimensions (AND)
    # Multiple values within one leaf's "value" list are OR'd by "in";
    # multiple leaves inside "all" are AND'd -- exactly "same dimension =
    # OR, different dimensions = AND" from the ticket. This requires zero
    # change to condition_matches() or to publication assembly: both
    # already evaluate this shape correctly today.
    #
    # Registry (condition_dimensions / condition_values, ccms_industrial.
    # py's _schema()) makes every dimension_id/value_id a real, checkable
    # identifier instead of free-text that could silently typo into
    # matching nothing. Human labels are display-only and can be renamed
    # without touching any already-authored condition_json, since only
    # the stable id is ever stored there.
    # ================================================================

    @staticmethod
    def _compile_condition(dimension_values: dict[str, Iterable[str]]) -> dict[str, Any]:
        """dimension_id -> allowed value_ids (OR within the dimension) =>
        the boolean-tree shape condition_matches() understands (AND across
        dimensions). Empty/blank dimensions are dropped; no restriction at
        all compiles to {}."""
        leaves = []
        for dim_id in sorted(str(d) for d in (dimension_values or {})):
            values = sorted({str(v).strip() for v in (dimension_values.get(dim_id) or []) if str(v).strip()})
            if not values:
                continue
            leaves.append({"field": dim_id, "op": "in", "value": values})
        if not leaves:
            return {}
        if len(leaves) == 1:
            return leaves[0]
        return {"all": leaves}

    @staticmethod
    def _decompile_condition(condition: dict[str, Any] | None) -> dict[str, list[str]] | None:
        """Inverse of _compile_condition, for the restricted 4E.1 shape
        only. Returns None if `condition` uses richer boolean-tree features
        (any/not/nested/other operators) this simplified editor cannot
        represent -- callers MUST treat None as "too complex to edit here",
        never silently approximate it."""
        cond = condition or {}
        if not cond:
            return {}
        if "any" in cond or "not" in cond:
            return None
        leaves = cond["all"] if ("all" in cond and isinstance(cond.get("all"), list)) else [cond]
        result: dict[str, list[str]] = {}
        for leaf in leaves:
            if not isinstance(leaf, dict) or (set(leaf.keys()) - {"field", "op", "value"}):
                return None
            if leaf.get("op") != "in":
                return None
            field = str(leaf.get("field", "")).strip()
            values = leaf.get("value")
            if not field or not isinstance(values, list):
                return None
            result[field] = [str(v) for v in values]
        return result

    @staticmethod
    def _condition_field_refs(condition: dict[str, Any] | None) -> list[tuple[str, list[str]]]:
        """Best-effort (field, values) extraction from ANY condition_json
        shape, including richer trees this layer never authors itself but
        might still need to report on (e.g. is_dimension_referenced against
        data written before 4E.1 or via direct import). Informational only
        -- never used for evaluation."""
        cond = condition or {}
        if not isinstance(cond, dict) or not cond:
            return []
        if isinstance(cond.get("all"), list):
            out = []
            for c in cond["all"]:
                out.extend(AuthoringService._condition_field_refs(c))
            return out
        if isinstance(cond.get("any"), list):
            out = []
            for c in cond["any"]:
                out.extend(AuthoringService._condition_field_refs(c))
            return out
        if "not" in cond:
            return AuthoringService._condition_field_refs(cond.get("not"))
        field = cond.get("field")
        if not field:
            return []
        value = cond.get("value")
        values = value if isinstance(value, list) else ([value] if value is not None else [])
        return [(str(field), [str(v) for v in values])]

    def _compile_and_validate_condition(self, dimension_values: dict[str, Iterable[str]]) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """Validates every dimension_id/value_id in `dimension_values`
        against the registry -- unknown id raises ValueError, never
        silently accepted -- then compiles to the boolean-tree storage
        shape. Returns (compiled_json, normalized) so callers can also
        report/audit the readable normalized form."""
        normalized: dict[str, list[str]] = {}
        for dim_id, values in (dimension_values or {}).items():
            dim_id = str(dim_id).strip()
            if not dim_id:
                continue
            if not self.conn.execute(
                "SELECT 1 FROM condition_dimensions WHERE dimension_id=?", (dim_id,)
            ).fetchone():
                raise ValueError(f"Dimensione sconosciuta: {dim_id!r}.")
            vals = sorted({str(v).strip() for v in (values or []) if str(v).strip()})
            for v in vals:
                if not self.conn.execute(
                    "SELECT 1 FROM condition_values WHERE value_id=? AND dimension_id=?", (v, dim_id)
                ).fetchone():
                    raise ValueError(f"Valore sconosciuto per la dimensione {dim_id!r}: {v!r}.")
            if vals:
                normalized[dim_id] = vals
        return self._compile_condition(normalized), normalized

    # ---- dimension / value registry ----

    def create_condition_dimension(self, dimension_id: str, name: str, *, description: str = "",
                                   actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        dimension_id = str(dimension_id or "").strip()
        name = str(name or "").strip()
        if not dimension_id or not re.fullmatch(r"[a-z0-9_]+", dimension_id):
            raise ValueError("L'id della dimensione deve essere in minuscolo, con lettere/numeri/underscore.")
        if not name:
            raise ValueError("La dimensione deve avere un nome leggibile.")
        if self.conn.execute("SELECT 1 FROM condition_dimensions WHERE dimension_id=?", (dimension_id,)).fetchone():
            raise ValueError(f"Esiste già una dimensione con id {dimension_id!r}.")
        now = _now()
        description = str(description or "").strip()
        self.conn.execute(
            "INSERT INTO condition_dimensions(dimension_id,name,description,active,created_at,updated_at) "
            "VALUES(?,?,?,1,?,?)",
            (dimension_id, name, description, now, now),
        )
        self.repo.audit_event("condition_dimension", dimension_id, "CONDITION_DIMENSION_CREATED", actor,
                              {"name": name, "description": description})
        self.conn.commit()
        return {"dimension_id": dimension_id, "name": name, "description": description, "active": True}

    def rename_condition_dimension(self, dimension_id: str, *, name: str | None = None,
                                   description: str | None = None, actor: str) -> dict[str, Any]:
        """Label-only edit: dimension_id (the stable identifier stored in
        every authored condition_json) never changes."""
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        row = self.conn.execute("SELECT * FROM condition_dimensions WHERE dimension_id=?", (dimension_id,)).fetchone()
        if not row:
            raise KeyError(dimension_id)
        new_name = row["name"] if name is None else str(name).strip()
        new_desc = row["description"] if description is None else str(description).strip()
        if not new_name:
            raise ValueError("La dimensione deve avere un nome leggibile.")
        now = _now()
        self.conn.execute(
            "UPDATE condition_dimensions SET name=?,description=?,updated_at=? WHERE dimension_id=?",
            (new_name, new_desc, now, dimension_id),
        )
        self.repo.audit_event("condition_dimension", dimension_id, "CONDITION_DIMENSION_RENAMED", actor,
                              {"from_name": row["name"], "to_name": new_name})
        self.conn.commit()
        return {"dimension_id": dimension_id, "name": new_name, "description": new_desc}

    def set_condition_dimension_active(self, dimension_id: str, active: bool, *, actor: str) -> dict[str, Any]:
        """Reversible archive/reactivate -- never a destructive delete, per
        the ticket's explicit 'no destructive deletion if referenced' rule.
        Deactivating does NOT touch any already-authored condition_json
        that references this dimension (existing conditions keep working
        exactly as before); it only removes the dimension from the picker
        for NEW conditions in the Studio UI."""
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        row = self.conn.execute("SELECT * FROM condition_dimensions WHERE dimension_id=?", (dimension_id,)).fetchone()
        if not row:
            raise KeyError(dimension_id)
        now = _now()
        self.conn.execute(
            "UPDATE condition_dimensions SET active=?,updated_at=? WHERE dimension_id=?",
            (1 if active else 0, now, dimension_id),
        )
        self.repo.audit_event("condition_dimension", dimension_id,
                              "CONDITION_DIMENSION_ACTIVATED" if active else "CONDITION_DIMENSION_DEACTIVATED",
                              actor, {})
        self.conn.commit()
        return {"dimension_id": dimension_id, "active": bool(active)}

    def list_condition_dimensions(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM condition_dimensions ORDER BY name COLLATE NOCASE").fetchall()
        out = []
        for r in rows:
            if not include_inactive and not r["active"]:
                continue
            out.append({
                "dimension_id": r["dimension_id"], "name": r["name"], "description": r["description"],
                "active": bool(r["active"]), "referenced": self.is_dimension_referenced(r["dimension_id"]),
            })
        return out

    def create_condition_value(self, value_id: str, dimension_id: str, label: str, *, description: str = "",
                               actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        value_id = str(value_id or "").strip()
        label = str(label or "").strip()
        if not value_id or not re.fullmatch(r"[a-z0-9_]+", value_id):
            raise ValueError("L'id del valore deve essere in minuscolo, con lettere/numeri/underscore.")
        if not label:
            raise ValueError("Il valore deve avere un'etichetta leggibile.")
        if not self.conn.execute("SELECT 1 FROM condition_dimensions WHERE dimension_id=?", (dimension_id,)).fetchone():
            raise KeyError(dimension_id)
        if self.conn.execute("SELECT 1 FROM condition_values WHERE value_id=?", (value_id,)).fetchone():
            raise ValueError(f"Esiste già un valore con id {value_id!r}.")
        now = _now()
        description = str(description or "").strip()
        self.conn.execute(
            "INSERT INTO condition_values(value_id,dimension_id,label,description,active,created_at,updated_at) "
            "VALUES(?,?,?,?,1,?,?)",
            (value_id, dimension_id, label, description, now, now),
        )
        self.repo.audit_event("condition_value", value_id, "CONDITION_VALUE_CREATED", actor,
                              {"dimension_id": dimension_id, "label": label})
        self.conn.commit()
        return {"value_id": value_id, "dimension_id": dimension_id, "label": label,
                "description": description, "active": True}

    def rename_condition_value(self, value_id: str, *, label: str | None = None, description: str | None = None,
                               actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        row = self.conn.execute("SELECT * FROM condition_values WHERE value_id=?", (value_id,)).fetchone()
        if not row:
            raise KeyError(value_id)
        new_label = row["label"] if label is None else str(label).strip()
        new_desc = row["description"] if description is None else str(description).strip()
        if not new_label:
            raise ValueError("Il valore deve avere un'etichetta leggibile.")
        now = _now()
        self.conn.execute(
            "UPDATE condition_values SET label=?,description=?,updated_at=? WHERE value_id=?",
            (new_label, new_desc, now, value_id),
        )
        self.repo.audit_event("condition_value", value_id, "CONDITION_VALUE_RENAMED", actor,
                              {"from_label": row["label"], "to_label": new_label})
        self.conn.commit()
        return {"value_id": value_id, "label": new_label, "description": new_desc}

    def set_condition_value_active(self, value_id: str, active: bool, *, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        row = self.conn.execute("SELECT * FROM condition_values WHERE value_id=?", (value_id,)).fetchone()
        if not row:
            raise KeyError(value_id)
        now = _now()
        self.conn.execute(
            "UPDATE condition_values SET active=?,updated_at=? WHERE value_id=?",
            (1 if active else 0, now, value_id),
        )
        self.repo.audit_event("condition_value", value_id,
                              "CONDITION_VALUE_ACTIVATED" if active else "CONDITION_VALUE_DEACTIVATED", actor, {})
        self.conn.commit()
        return {"value_id": value_id, "active": bool(active)}

    def list_condition_values(self, dimension_id: str | None = None, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        if dimension_id:
            rows = self.conn.execute(
                "SELECT * FROM condition_values WHERE dimension_id=? ORDER BY label COLLATE NOCASE", (dimension_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM condition_values ORDER BY dimension_id,label COLLATE NOCASE"
            ).fetchall()
        out = []
        for r in rows:
            if not include_inactive and not r["active"]:
                continue
            out.append({
                "value_id": r["value_id"], "dimension_id": r["dimension_id"], "label": r["label"],
                "description": r["description"], "active": bool(r["active"]),
                "referenced": self.is_value_referenced(r["value_id"]),
            })
        return out

    def _all_condition_jsons(self) -> list[dict[str, Any]]:
        """Every condition_json currently stored anywhere -- topics,
        blocks, and the vestigial table/figure object-level column (kept
        for schema symmetry, never written to by this service -- see the
        save_block reuse note above). Informational only, used by the
        is_*_referenced 'already used' checks below."""
        out = []
        for table in ("topics", "content_blocks", "structured_tables", "figures"):
            for row in self.conn.execute(f"SELECT condition_json FROM {table}"):
                out.append(_load_json(row["condition_json"], {}))
        return out

    def is_dimension_referenced(self, dimension_id: str) -> bool:
        for cond in self._all_condition_jsons():
            if any(field == dimension_id for field, _values in self._condition_field_refs(cond)):
                return True
        for row in self.conn.execute("SELECT dimensions_json FROM profiles"):
            if dimension_id in _load_json(row["dimensions_json"], {}):
                return True
        return False

    def is_value_referenced(self, value_id: str) -> bool:
        for cond in self._all_condition_jsons():
            if any(value_id in values for _field, values in self._condition_field_refs(cond)):
                return True
        for row in self.conn.execute("SELECT dimensions_json FROM profiles"):
            if value_id in _load_json(row["dimensions_json"], {}).values():
                return True
        return False

    # ---- variant profiles ----

    def _validate_profile_dimensions(self, dimension_values: dict[str, str]) -> dict[str, str]:
        """Fail-closed: every dimension_id/value_id in a profile must
        exist in the registry, and the value must belong to the stated
        dimension -- a profile is the 'active configuration' every
        evaluate/assembly trusts, so it must never silently reference
        something that does not exist."""
        out: dict[str, str] = {}
        for dim_id, value_id in (dimension_values or {}).items():
            dim_id = str(dim_id).strip()
            value_id = str(value_id).strip()
            if not dim_id or not value_id:
                continue
            if not self.conn.execute("SELECT 1 FROM condition_dimensions WHERE dimension_id=?", (dim_id,)).fetchone():
                raise ValueError(f"Dimensione sconosciuta nel profilo: {dim_id!r}.")
            if not self.conn.execute(
                "SELECT 1 FROM condition_values WHERE value_id=? AND dimension_id=?", (value_id, dim_id)
            ).fetchone():
                raise ValueError(f"Valore sconosciuto nel profilo per la dimensione {dim_id!r}: {value_id!r}.")
            out[dim_id] = value_id
        return out

    def create_variant_profile(self, profile_id: str, name: str, dimension_values: dict[str, str], *,
                               actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        profile_id = str(profile_id or "").strip()
        name = str(name or "").strip()
        if not profile_id:
            raise ValueError("Il profilo deve avere un id.")
        if not name:
            raise ValueError("Il profilo deve avere un nome.")
        if self.conn.execute("SELECT 1 FROM profiles WHERE profile_id=?", (profile_id,)).fetchone():
            raise ValueError(f"Esiste già un profilo con id {profile_id!r}.")
        validated = self._validate_profile_dimensions(dimension_values)
        self.repo.create_profile(profile_id, name, validated)
        self.repo.audit_event("profile", profile_id, "VARIANT_PROFILE_CREATED", actor,
                              {"name": name, "dimensions": validated})
        self.conn.commit()
        return {"profile_id": profile_id, "name": name, "dimensions": validated, "active": True}

    def update_variant_profile(self, profile_id: str, *, name: str | None = None,
                               dimension_values: dict[str, str] | None = None, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        row = self.conn.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        new_name = row["title"] if name is None else str(name).strip()
        if not new_name:
            raise ValueError("Il profilo deve avere un nome.")
        validated = _load_json(row["dimensions_json"], {}) if dimension_values is None \
            else self._validate_profile_dimensions(dimension_values)
        self.repo.update_profile(profile_id, validated, title=new_name)
        self.repo.audit_event("profile", profile_id, "VARIANT_PROFILE_UPDATED", actor,
                              {"name": new_name, "dimensions": validated})
        self.conn.commit()
        return {"profile_id": profile_id, "name": new_name, "dimensions": validated, "active": bool(row["active"])}

    def set_variant_profile_active(self, profile_id: str, active: bool, *, actor: str) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_SETTINGS)
        row = self.conn.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        now = _now()
        self.conn.execute("UPDATE profiles SET active=?,updated_at=? WHERE profile_id=?",
                          (1 if active else 0, now, profile_id))
        self.repo.audit_event("profile", profile_id,
                              "VARIANT_PROFILE_ACTIVATED" if active else "VARIANT_PROFILE_DEACTIVATED", actor, {})
        self.conn.commit()
        return {"profile_id": profile_id, "active": bool(active)}

    def get_variant_profile(self, profile_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        return {"profile_id": profile_id, "name": row["title"], "dimensions": _load_json(row["dimensions_json"], {}),
                "active": bool(row["active"])}

    def list_variant_profiles(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM profiles ORDER BY title COLLATE NOCASE").fetchall()
        out = []
        for r in rows:
            if not include_inactive and not r["active"]:
                continue
            out.append({"profile_id": r["profile_id"], "name": r["title"],
                        "dimensions": _load_json(r["dimensions_json"], {}), "active": bool(r["active"])})
        return out

    # ---- evaluator ----

    def evaluate_conditions(self, condition: dict[str, Any] | None, profile_dimensions: dict[str, str]) -> dict[str, Any]:
        """Rich, explainable evaluation of one condition against one
        resolved profile (dimension_id -> single selected value_id) --
        what the Studio's 'Valuta contenuto con questo profilo' surfaces to
        a writer. Distinct from IndustrialRepository.condition_matches() (a
        plain bool used by publication assembly): this explains WHY.

        Fail-closed: a dimension_id/value_id referenced by `condition` (or
        by the profile's own selection) that does not exist AT ALL in the
        registry makes the result unresolved -- NEVER included by default.
        An existing-but-INACTIVE dimension/value is not unresolved: it is a
        normal, resolvable (if unmatched) state. 4E.1's fail-closed rule is
        specifically about references to something that does not exist,
        not about something merely archived."""
        parsed = self._decompile_condition(condition)
        if parsed is None:
            return {"included": False,
                    "reason": "Condizione troppo complessa per questa valutazione semplificata.",
                    "matched_dimensions": [], "failed_dimensions": [], "unresolved_dimensions": []}
        if not parsed:
            return {"included": True, "reason": "Nessuna condizione: contenuto sempre incluso.",
                    "matched_dimensions": [], "failed_dimensions": [], "unresolved_dimensions": []}

        matched: list[str] = []
        failed: list[str] = []
        unresolved: list[str] = []
        for dim_id, allowed_values in parsed.items():
            if not self.conn.execute("SELECT 1 FROM condition_dimensions WHERE dimension_id=?", (dim_id,)).fetchone():
                unresolved.append(dim_id)
                continue
            unknown_values = [v for v in allowed_values if not self.conn.execute(
                "SELECT 1 FROM condition_values WHERE value_id=? AND dimension_id=?", (v, dim_id)).fetchone()]
            if unknown_values:
                unresolved.append(dim_id)
                continue
            profile_value = profile_dimensions.get(dim_id)
            if profile_value is None:
                failed.append(dim_id)
                continue
            if not self.conn.execute(
                "SELECT 1 FROM condition_values WHERE value_id=? AND dimension_id=?", (profile_value, dim_id)
            ).fetchone():
                unresolved.append(dim_id)
                continue
            if profile_value in allowed_values:
                matched.append(dim_id)
            else:
                failed.append(dim_id)

        if unresolved:
            reason = "Condizione non risolvibile per: " + ", ".join(sorted(unresolved)) + "."
            included = False
        elif failed:
            reason = "Escluso: non soddisfa " + ", ".join(sorted(failed)) + "."
            included = False
        else:
            reason = "Incluso: soddisfa " + ", ".join(sorted(matched)) + "." if matched else \
                "Incluso: nessuna dimensione richiesta."
            included = True
        return {"included": included, "reason": reason, "matched_dimensions": matched,
                "failed_dimensions": failed, "unresolved_dimensions": unresolved}

    def evaluate_element_with_profile(self, kind: str, object_id: str, profile_id: str) -> dict[str, Any]:
        """Convenience wrapper for the Studio's 'Valuta contenuto con
        questo profilo' mini-evaluator: loads the element's OWN
        condition_json (topic or block -- a table/figure block is
        evaluated via ITS block, per the reuse rule that conditions live
        on the placement, not the shared object) plus the profile's
        dimensions, then delegates to evaluate_conditions()."""
        if kind == "topic":
            row = self.conn.execute("SELECT condition_json FROM topics WHERE topic_id=?", (object_id,)).fetchone()
        elif kind == "block":
            row = self.conn.execute("SELECT condition_json FROM content_blocks WHERE block_id=?", (object_id,)).fetchone()
        else:
            raise ValueError("kind deve essere 'topic' o 'block'.")
        if not row:
            raise KeyError(object_id)
        profile = self.get_variant_profile(profile_id)
        condition = _load_json(row["condition_json"], {})
        result = self.evaluate_conditions(condition, profile["dimensions"])
        result["profile_id"] = profile_id
        result["profile_name"] = profile["name"]
        result["profile_active"] = profile["active"]
        # 4E.1 stabilization: carries resolved dimension/value NAMES for
        # this element's OWN condition, so the view layer's human-facing
        # message (authoring_view.evaluate_summary_text) never needs a
        # second DB round-trip or its own registry lookups just to word
        # "this content is for Mercato = US" -- the labels are already
        # here, matching the exact terms describe_condition() uses
        # everywhere else in the standard-mode UI (never raw ids).
        result["condition_description"] = self.describe_condition(condition)
        return result

    def describe_condition(self, condition: dict[str, Any] | None) -> dict[str, Any]:
        """Resolves a compiled condition_json into human-readable labels
        (never raw ids), for authoring_view.py's standard-mode summary
        formatter -- the view layer has no DB access, so label resolution
        happens here. always_included=True with an empty dimensions list
        covers both {} (genuinely no condition) and a condition too
        complex for this simplified editor to decompile (too_complex=True
        distinguishes the two; standard mode must never guess a summary
        for something it can't faithfully represent)."""
        parsed = self._decompile_condition(condition)
        if not parsed:
            return {"always_included": True, "too_complex": parsed is None, "dimensions": []}
        dims = []
        for dim_id, value_ids in parsed.items():
            dim_row = self.conn.execute("SELECT name FROM condition_dimensions WHERE dimension_id=?", (dim_id,)).fetchone()
            dim_name = dim_row["name"] if dim_row else dim_id
            values = []
            for v in value_ids:
                v_row = self.conn.execute("SELECT label FROM condition_values WHERE value_id=?", (v,)).fetchone()
                values.append({"value_id": v, "label": v_row["label"] if v_row else v})
            dims.append({"dimension_id": dim_id, "name": dim_name, "values": values})
        dims.sort(key=lambda d: d["name"].lower())
        return {"always_included": False, "too_complex": False, "dimensions": dims}

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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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
            # 4E.3: text_format is part of what a revert restores, exactly
            # like text_content itself -- reverting to a version that
            # predates this milestone (or was saved before ever being
            # touched by the rich-aware editor) correctly restores 'plain'
            # rather than leaving whatever format the CURRENT row happens
            # to carry. `hist["text_format"]` is safe even for a
            # block_versions row written before this column existed,
            # since the additive migration's DEFAULT 'plain' applies
            # retroactively to it too.
            hist_text_format = hist["text_format"] if "text_format" in hist.keys() else "plain"
            cur = self.conn.execute(
                """UPDATE content_blocks SET text_content=?,status=?,version=?,condition_json=?,
                   source_refs_json=?,evidence_status=?,updated_at=?,text_format=? WHERE block_id=? AND version=?""",
                (hist["text_content"], new_status, new_version, hist["condition_json"], hist["source_refs_json"],
                 hist["evidence_status"], now, hist_text_format, block_id, int(live["version"])),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(block_id, live["version"])
            self.conn.execute(
                """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,
                   object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at,text_format)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, new_version, live["topic_id"], live["block_type"], int(live["position"]),
                 hist["text_content"], live["object_ref_id"], new_status, hist["condition_json"],
                 hist["source_refs_json"], hist["evidence_status"], now, hist_text_format),
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED_APPROVAL)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        title = str(title or "").strip()
        if not title:
            raise DomainValidationError(de.ERR_AUTHORING_TOPIC_TITLE_REQUIRED)
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
                              after_block_id: str | None = None, text_format: str = "plain") -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        bt = str(block_type or "").strip().lower()
        if bt not in EDITABLE_TEXT_BLOCK_TYPES:
            raise DomainValidationError(de.ERR_AUTHORING_BLOCK_TYPE_UNSUPPORTED,
                                        block_type=bt,
                                        allowed=sorted(EDITABLE_TEXT_BLOCK_TYPES))
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
        self.repo.add_block(block_id, topic_id, bt, position=1_000_000, text=text, status="Draft",
                           text_format=text_format)
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
            "SELECT version,text_content,text_format,status,created_at FROM block_versions WHERE block_id=? ORDER BY version DESC",
            (block_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "version": int(r["version"]), "text": r["text_content"], "text_format": r["text_format"], "status": r["status"],
                "created_at": r["created_at"], "actor": self._actor_for_version("block", block_id, int(r["version"])),
            })
        return out

    def get_topic_version(self, topic_id: str, version: int) -> dict[str, Any]:
        """4D: full historical content of one topic version (title/body),
        needed to actually compute a diff between two versions --
        topic_history() above only returns summary fields for the history
        list, by design."""
        row = self.conn.execute(
            "SELECT * FROM topic_versions WHERE topic_id=? AND version=?", (topic_id, int(version))
        ).fetchone()
        if not row:
            raise KeyError((topic_id, version))
        return {
            "topic_id": topic_id, "version": int(row["version"]), "title": row["title"], "body": row["body"],
            "status": row["status"], "created_at": row["created_at"],
        }

    def get_block_version(self, block_id: str, version: int) -> dict[str, Any]:
        """4D: full historical content of one text-block version (see
        get_topic_version for the identical rationale)."""
        row = self.conn.execute(
            "SELECT * FROM block_versions WHERE block_id=? AND version=?", (block_id, int(version))
        ).fetchone()
        if not row:
            raise KeyError((block_id, version))
        return {
            "block_id": block_id, "version": int(row["version"]), "text": row["text_content"],
            "block_type": row["block_type"], "status": row["status"], "created_at": row["created_at"],
            "text_format": row["text_format"],
        }

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

    def get_reuse_impact(self, object_type: str, object_id: str) -> dict[str, Any]:
        """4D: block-level-precise reuse impact for the shared-object
        decision UX -- distinct from repo.impact_map_industrial(), which is
        topic/publication-ROW granularity and advisory-only (the advanced-
        mode 'where is this reused' listing). This is the primitive that
        actually decides is_shared / drives the 'Aggiorna ovunque' vs
        'Crea una copia locale' choice, so it must count the real
        content_blocks references (the same fact _reference_count already
        computes for the fail-closed guard), never the over-counted
        topic/publication rows a topic assembled into two publications
        would otherwise produce.

        4D Integrity Gate (component scope decision, Option 1): also
        accepts object_type="component" -- component/component_ref is
        genuinely modeled and versioned (ContentRepository.create_
        component, components/component_versions), so its reuse CAN be
        reported precisely, block-level, exactly like table/figure. This
        is read-only reporting only: there is no save_component()/
        _guard_table_or_figure_editable() path for components (no Studio
        editor exists to apply 'Aggiorna ovunque'/'Crea una copia locale'
        to), so is_shared here only ever drives a read-only notice, never
        a write guard. Scope boundary, documented rather than silently
        gapped: this counts block-level component_ref references only --
        a component embedded via an inline {{component:ID}} token directly
        in a topic body (a distinct, text-substitution reuse mechanism,
        see resolve_components()/repo.impact_map_industrial(kind=
        "component")) is NOT counted here, since folding two different
        reuse mechanisms into one block-reference count would misrepresent
        both. Inline-token usage stays visible only through the existing
        advanced-mode impact_summary()."""
        bt = {"table": "table", "figure": "figure", "component": "component_ref"}[object_type]
        blocks = self.conn.execute(
            "SELECT block_id,topic_id,archived_at FROM content_blocks WHERE block_type=? AND object_ref_id=? ORDER BY block_id",
            (bt, object_id),
        ).fetchall()
        block_refs: list[dict[str, Any]] = []
        topic_ids_seen: list[str] = []
        archived_reference_count = 0
        for b in blocks:
            topic = self.conn.execute("SELECT title FROM topics WHERE topic_id=?", (b["topic_id"],)).fetchone()
            topic_title = topic["title"] if topic else b["topic_id"]
            archived = bool(b["archived_at"])
            if archived:
                archived_reference_count += 1
            block_refs.append({
                "block_id": b["block_id"], "topic_id": b["topic_id"], "topic_title": topic_title,
                "archived": archived,
            })
            if b["topic_id"] not in topic_ids_seen:
                topic_ids_seen.append(b["topic_id"])
        topic_title_by_id = {br["topic_id"]: br["topic_title"] for br in block_refs}
        topics = [{"topic_id": tid, "title": topic_title_by_id.get(tid, tid)} for tid in topic_ids_seen]
        publications: list[dict[str, str]] = []
        seen_pubs: set[str] = set()
        for tid in topic_ids_seen:
            for p in self.conn.execute(
                """SELECT p.publication_id,p.title FROM publication_topics pt
                   JOIN publications p ON p.publication_id=pt.publication_id WHERE pt.topic_id=?""",
                (tid,),
            ):
                if p["publication_id"] not in seen_pubs:
                    seen_pubs.add(p["publication_id"])
                    publications.append({"publication_id": p["publication_id"], "title": p["title"]})
        table_or_figure = {"table": "structured_tables", "figure": "figures", "component": "components"}[object_type]
        id_col = {"table": "table_id", "figure": "figure_id", "component": "component_id"}[object_type]
        obj_row = self.conn.execute(
            f"SELECT source_refs_json,version FROM {table_or_figure} WHERE {id_col}=?", (object_id,)
        ).fetchone()
        source_refs = _load_json(obj_row["source_refs_json"], []) if obj_row else []
        version = int(obj_row["version"]) if obj_row else None
        reference_count = len(block_refs)
        # Deterministic snapshot of exactly what the user is about to see:
        # object version + the EXACT sorted set of referencing block_ids
        # (archived ones included -- _reference_count already counts them
        # too, so the token must track the same population it gates).
        # Session-only, never persisted: recomputed fresh on every guard
        # check against live DB state, so a stale token can never match
        # once anything in this snapshot has actually changed.
        token_payload = _json({
            "object_type": object_type, "object_id": object_id, "version": version,
            "block_ids": sorted(br["block_id"] for br in block_refs),
        })
        impact_token = hashlib.sha256(token_payload.encode("utf-8")).hexdigest()
        return {
            "object_type": object_type, "object_id": object_id, "version": version,
            "reference_count": reference_count,
            "is_shared": reference_count > 1, "block_refs": block_refs, "topics": topics,
            "publications": publications, "archived_reference_count": archived_reference_count,
            "source_refs": source_refs, "impact_token": impact_token,
        }

    def _guard_table_or_figure_editable(self, object_type: str, object_id: str, *,
                                        confirm_shared: bool = False,
                                        expected_impact_token: str | None = None) -> None:
        """Shared by save/revert: archived-referencing-block check first
        (an archived object being edited at all is the more surprising/
        dangerous case), then the shared-object guard. Called from inside
        a caller's own BEGIN IMMEDIATE/try block; raises straight through
        to that block's own `except Exception: self.conn.rollback(); raise`
        -- the same pattern every early-exit check elsewhere in this file
        already relies on (a rollback() with no active transaction is a
        proven-safe no-op here, not a second, redundant one).

        4D: confirm_shared=True is the ONLY way past the shared-object
        guard -- it is how 'Aggiorna ovunque' becomes an explicit,
        deliberate, audited override rather than a removal of the fail-
        closed gate. The caller (save_table/save_figure/revert_*) is
        responsible for recording that decision in the audit trail; this
        guard only decides whether to raise.

        4D integrity gate: confirm_shared=True is NOT itself sufficient --
        expected_impact_token must also match get_reuse_impact()'s CURRENT
        token (object version + exact sorted reference-block-id set),
        re-read live inside this same guard call. This closes a real gap
        expected_version alone missed: the object's own version can stay
        identical while a block is added to or removed from its reference
        set between the user's preview and the Save click, which would
        otherwise silently apply a global edit to more (or fewer) points
        than the user actually saw and decided on. Any drift -> fail
        closed, same SharedObjectError channel, no partial write."""
        block = self._referencing_block(object_type, object_id)
        if block is not None and block["archived_at"]:
            label = "tabella" if object_type == "table" else "figura"
            raise ArchivedObjectError(
                f"Questa {label} è collegata a un blocco archiviato: ripristina il blocco prima di modificarla."
            )
        if self._reference_count(object_type, object_id) > 1 and not confirm_shared:
            raise SharedObjectError(_SHARED_OBJECT_MESSAGE)
        if confirm_shared:
            if not expected_impact_token:
                raise ValueError(
                    "confirm_shared=True richiede expected_impact_token: rileggi get_reuse_impact() "
                    "prima di salvare (non è mai sicuro presumere che l'impatto non sia cambiato)."
                )
            current_token = self.get_reuse_impact(object_type, object_id)["impact_token"]
            if expected_impact_token != current_token:
                raise SharedObjectError(_IMPACT_DRIFT_MESSAGE)

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
            raise DomainFileMissingError(de.ERR_AUTHORING_ASSET_FILE_MISSING,
                                         path=str(source_path))
        assets_dir = self._project_dir() / "Assets" / subdir
        assets_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_path.name).strip("_") or "asset"
        dest = assets_dir / f"{uuid4().hex[:10]}_{safe_name}"
        shutil.copy2(source_path, dest)
        return dest

    # ---- table save/create ----

    def save_table(self, table_id: str, *, title: str | None = None, caption: str | None = None,
                   columns: list[str] | None = None, rows: list[list[Any]] | None = None,
                   header_rows: int | None = None, expected_version: int, actor: str,
                   confirm_shared: bool = False, expected_impact_token: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        if columns is not None and not columns:
            raise ValueError("La tabella deve avere almeno una colonna.")
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(table_id)
            self._guard_table_or_figure_editable("table", table_id, confirm_shared=confirm_shared,
                                                 expected_impact_token=expected_impact_token)
            reference_count = self._reference_count("table", table_id)
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
            save_detail = {"from_version": int(expected_version), "to_version": new_version,
                           "previous_status": previous_status, "new_status": new_status}
            if reference_count > 1:
                save_detail["decision"] = "shared_update"
                save_detail["impact_count"] = reference_count
            self.repo.audit_event("table", table_id, "AUTHORING_SAVE", actor, save_detail)
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
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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

    def revert_table_to_version(self, table_id: str, version: int, *, actor: str,
                                confirm_shared: bool = False,
                                expected_impact_token: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(table_id)
            self._guard_table_or_figure_editable("table", table_id, confirm_shared=confirm_shared,
                                                 expected_impact_token=expected_impact_token)
            reference_count = self._reference_count("table", table_id)
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
            revert_detail = {"reverted_from_version": int(live["version"]),
                             "reverted_to_content_of_version": int(version), "new_version": new_version}
            if reference_count > 1:
                revert_detail["decision"] = "shared_update"
                revert_detail["impact_count"] = reference_count
            self.repo.audit_event("table", table_id, "REVERT_TO_VERSION", actor, revert_detail)
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

    def get_table_version(self, table_id: str, version: int) -> dict[str, Any]:
        """4D: full historical content of one table version (columns/rows/
        header_rows), needed to actually compute a diff between two
        versions -- table_history() above only returns summary fields
        (title/caption/status/created_at) for the history list, by
        design."""
        row = self.conn.execute(
            "SELECT * FROM structured_table_versions WHERE table_id=? AND version=?", (table_id, int(version))
        ).fetchone()
        if not row:
            raise KeyError((table_id, version))
        return {
            "table_id": table_id, "version": int(row["version"]), "title": row["title"], "caption": row["caption"],
            "columns": _load_json(row["columns_json"], []), "rows": _load_json(row["rows_json"], []),
            "header_rows": int(row["header_rows"]), "status": row["status"], "created_at": row["created_at"],
        }

    def fork_table_local(self, table_id: str, *, block_id: str, expected_version: int, actor: str) -> dict[str, Any]:
        """4D 'Crea una copia locale': atomically replaces ONE block's
        reference to a shared table with a brand-new, independent table
        that starts as a copy of the shared table's current content. Only
        the given block is rewired -- every other block still referencing
        the original table is completely untouched, which is the entire
        point of this decision (as opposed to 'Aggiorna ovunque').

        True atomicity (single BEGIN IMMEDIATE, raw INSERTs) is required
        here, unlike create_table_in_topic()'s accepted two-phase pattern:
        repo.create_table() commits internally, which would break this
        operation's "completes as one logical operation" requirement, so
        it is deliberately NOT used here.

        Fails closed (VersionConflictError, with an explicit Italian
        message) if the table's live version no longer matches
        expected_version at fork time -- never fork a version different
        from the one the user actually previewed the impact of."""
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(table_id)
            if int(live["version"]) != int(expected_version):
                self.conn.rollback()
                raise VersionConflictError(
                    table_id, expected_version,
                    detail="L'elemento è cambiato dopo l'anteprima dell'impatto. Riapri il confronto prima di creare la copia locale.",
                )
            block = self.conn.execute("SELECT * FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
            if not block:
                self.conn.rollback()
                raise KeyError(block_id)
            if block["block_type"] != "table" or block["object_ref_id"] != table_id:
                self.conn.rollback()
                raise ValueError(f"Il blocco {block_id} non referenzia la tabella {table_id}.")
            if block["archived_at"]:
                self.conn.rollback()
                raise ArchivedObjectError(
                    f"Il blocco {block_id} è archiviato: ripristinalo prima di creare una copia locale."
                )

            new_table_id = f"TBL-{uuid4().hex[:10].upper()}"
            while self.conn.execute("SELECT 1 FROM structured_tables WHERE table_id=?", (new_table_id,)).fetchone():
                new_table_id = f"TBL-{uuid4().hex[:10].upper()}"
            self.conn.execute(
                """INSERT INTO structured_tables(table_id,title,caption,columns_json,rows_json,header_rows,
                   status,version,condition_json,source_refs_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'Draft',1,?,?,?,?)""",
                (new_table_id, live["title"], live["caption"], live["columns_json"], live["rows_json"],
                 live["header_rows"], live["condition_json"], live["source_refs_json"], now, now),
            )
            self.conn.execute(
                """INSERT INTO structured_table_versions(table_id,version,title,caption,columns_json,rows_json,
                   header_rows,status,condition_json,source_refs_json,created_at)
                   VALUES(?,1,?,?,?,?,?,'Draft',?,?,?)""",
                (new_table_id, live["title"], live["caption"], live["columns_json"], live["rows_json"],
                 live["header_rows"], live["condition_json"], live["source_refs_json"], now),
            )
            new_block_version = int(block["version"]) + 1
            cur = self.conn.execute(
                "UPDATE content_blocks SET object_ref_id=?,version=?,updated_at=? WHERE block_id=? AND version=?",
                (new_table_id, new_block_version, now, block_id, int(block["version"])),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(block_id, block["version"])
            self.conn.execute(
                """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,
                   object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, new_block_version, block["topic_id"], block["block_type"], int(block["position"]),
                 block["text_content"], new_table_id, block["status"], block["condition_json"],
                 block["source_refs_json"], block["evidence_status"], now),
            )
            self.repo.audit_event("table", new_table_id, "AUTHORING_FORK_CREATED", actor,
                                  {"forked_from_object_id": table_id, "forked_from_version": int(live["version"]),
                                   "source_block_id": block_id})
            self.repo.audit_event("table", table_id, "AUTHORING_FORKED", actor,
                                  {"new_object_id": new_table_id, "source_block_id": block_id})
            self.repo.audit_event("block", block_id, "REWIRED_TO_LOCAL_FORK", actor,
                                  {"from_object_id": table_id, "to_object_id": new_table_id})
            self._delete_draft("table", table_id)
            self._delete_draft("block", block_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"table_id": new_table_id, "block_id": block_id, "forked_from_object_id": table_id,
                "forked_from_version": int(expected_version)}

    # ---- figure save/create/replace ----

    def save_figure(self, figure_id: str, *, title: str | None = None, caption: str | None = None,
                    alt_text: str | None = None, file_path: str | None = None,
                    expected_version: int, actor: str, confirm_shared: bool = False,
                    expected_impact_token: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
            if not row:
                self.conn.rollback()
                raise KeyError(figure_id)
            self._guard_table_or_figure_editable("figure", figure_id, confirm_shared=confirm_shared,
                                                 expected_impact_token=expected_impact_token)
            reference_count = self._reference_count("figure", figure_id)
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
            save_detail = {"from_version": int(expected_version), "to_version": new_version,
                           "previous_status": previous_status, "new_status": new_status,
                           "file_path_changed": file_path is not None}
            if reference_count > 1:
                save_detail["decision"] = "shared_update"
                save_detail["impact_count"] = reference_count
            self.repo.audit_event("figure", figure_id, "AUTHORING_SAVE", actor, save_detail)
            self._delete_draft("figure", figure_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"figure_id": figure_id, "version": new_version, "status": new_status,
                "previous_status": previous_status, "review_tasks_invalidated": invalidated}

    def replace_figure_image(self, figure_id: str, *, source_file_path: str | Path,
                             expected_version: int, actor: str, confirm_shared: bool = False,
                             expected_impact_token: str | None = None) -> dict[str, Any]:
        """Materializes the new file FIRST (fail-closed: if the copy itself
        fails, nothing about the figure has changed at all), then does a
        normal CAS save_figure() with only file_path set -- caption/alt/
        title are left untouched (None) unless the caller also passes a
        change. If the save fails for ANY reason (conflict/archived/
        shared/missing), the live figure row is guaranteed untouched
        (save_figure() rolled back its own transaction before raising) --
        the only remaining cleanup is the orphaned copy on disk, deleted
        here so no unclassified asset is left behind.

        confirm_shared/expected_impact_token forward straight through to
        save_figure() -- image replacement is exactly the kind of shared-
        object mutation 'Aggiorna ovunque' must be able to authorize, and
        it is subject to the identical impact-drift check as a caption/
        alt-text edit (see save_figure/_guard_table_or_figure_editable)."""
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        if not self.conn.execute("SELECT 1 FROM figures WHERE figure_id=?", (figure_id,)).fetchone():
            raise KeyError(figure_id)
        materialized_path = self._materialize_uploaded_asset(source_file_path)
        try:
            result = self.save_figure(figure_id, file_path=str(materialized_path),
                                      expected_version=expected_version, actor=actor,
                                      confirm_shared=confirm_shared, expected_impact_token=expected_impact_token)
        except Exception:
            materialized_path.unlink(missing_ok=True)
            raise
        return result

    def create_figure_in_topic(self, topic_id: str, *, actor: str, source_file_path: str | Path,
                               title: str = "", caption: str = "", alt_text: str = "",
                               after_block_id: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
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

    def revert_figure_to_version(self, figure_id: str, version: int, *, actor: str,
                                 confirm_shared: bool = False,
                                 expected_impact_token: str | None = None) -> dict[str, Any]:
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(figure_id)
            self._guard_table_or_figure_editable("figure", figure_id, confirm_shared=confirm_shared,
                                                 expected_impact_token=expected_impact_token)
            reference_count = self._reference_count("figure", figure_id)
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
            revert_detail = {"reverted_from_version": int(live["version"]),
                             "reverted_to_content_of_version": int(version), "new_version": new_version}
            if reference_count > 1:
                revert_detail["decision"] = "shared_update"
                revert_detail["impact_count"] = reference_count
            self.repo.audit_event("figure", figure_id, "REVERT_TO_VERSION", actor, revert_detail)
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

    def get_figure_version(self, figure_id: str, version: int) -> dict[str, Any]:
        """4D: full historical content of one figure version, needed to
        actually compute a diff between two versions (see get_table_version
        for the identical rationale)."""
        row = self.conn.execute(
            "SELECT * FROM figure_versions WHERE figure_id=? AND version=?", (figure_id, int(version))
        ).fetchone()
        if not row:
            raise KeyError((figure_id, version))
        return {
            "figure_id": figure_id, "version": int(row["version"]), "title": row["title"],
            "caption": row["caption"], "file_path": row["file_path"], "alt_text": row["alt_text"],
            "status": row["status"], "created_at": row["created_at"],
        }

    def fork_figure_local(self, figure_id: str, *, block_id: str, expected_version: int,
                          actor: str) -> dict[str, Any]:
        """4D 'Crea una copia locale' for a shared figure -- same atomicity/
        TOCTOU/audit-trail shape as fork_table_local() above. Reuses the
        SAME file_path string as the original (no byte copy): safe because
        neither the original nor the fork ever mutates existing asset bytes
        in place -- only replace_figure_image() (via
        _materialize_uploaded_asset) ever creates a brand-new file on
        disk."""
        actor = str(actor or "").strip()
        if not actor:
            raise DomainValidationError(de.ERR_AUTHORING_ACTOR_REQUIRED)
        now = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            live = self.conn.execute("SELECT * FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
            if not live:
                self.conn.rollback()
                raise KeyError(figure_id)
            if int(live["version"]) != int(expected_version):
                self.conn.rollback()
                raise VersionConflictError(
                    figure_id, expected_version,
                    detail="L'elemento è cambiato dopo l'anteprima dell'impatto. Riapri il confronto prima di creare la copia locale.",
                )
            block = self.conn.execute("SELECT * FROM content_blocks WHERE block_id=?", (block_id,)).fetchone()
            if not block:
                self.conn.rollback()
                raise KeyError(block_id)
            if block["block_type"] != "figure" or block["object_ref_id"] != figure_id:
                self.conn.rollback()
                raise ValueError(f"Il blocco {block_id} non referenzia la figura {figure_id}.")
            if block["archived_at"]:
                self.conn.rollback()
                raise ArchivedObjectError(
                    f"Il blocco {block_id} è archiviato: ripristinalo prima di creare una copia locale."
                )

            new_figure_id = f"FIG-{uuid4().hex[:10].upper()}"
            while self.conn.execute("SELECT 1 FROM figures WHERE figure_id=?", (new_figure_id,)).fetchone():
                new_figure_id = f"FIG-{uuid4().hex[:10].upper()}"
            self.conn.execute(
                """INSERT INTO figures(figure_id,title,caption,file_path,alt_text,status,version,
                   condition_json,source_refs_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,'Draft',1,?,?,?,?)""",
                (new_figure_id, live["title"], live["caption"], live["file_path"], live["alt_text"],
                 live["condition_json"], live["source_refs_json"], now, now),
            )
            self.conn.execute(
                """INSERT INTO figure_versions(figure_id,version,title,caption,file_path,alt_text,status,
                   condition_json,source_refs_json,created_at) VALUES(?,1,?,?,?,?,'Draft',?,?,?)""",
                (new_figure_id, live["title"], live["caption"], live["file_path"], live["alt_text"],
                 live["condition_json"], live["source_refs_json"], now),
            )
            new_block_version = int(block["version"]) + 1
            cur = self.conn.execute(
                "UPDATE content_blocks SET object_ref_id=?,version=?,updated_at=? WHERE block_id=? AND version=?",
                (new_figure_id, new_block_version, now, block_id, int(block["version"])),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise VersionConflictError(block_id, block["version"])
            self.conn.execute(
                """INSERT INTO block_versions(block_id,version,topic_id,block_type,position,text_content,
                   object_ref_id,status,condition_json,source_refs_json,evidence_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, new_block_version, block["topic_id"], block["block_type"], int(block["position"]),
                 block["text_content"], new_figure_id, block["status"], block["condition_json"],
                 block["source_refs_json"], block["evidence_status"], now),
            )
            self.repo.audit_event("figure", new_figure_id, "AUTHORING_FORK_CREATED", actor,
                                  {"forked_from_object_id": figure_id, "forked_from_version": int(live["version"]),
                                   "source_block_id": block_id})
            self.repo.audit_event("figure", figure_id, "AUTHORING_FORKED", actor,
                                  {"new_object_id": new_figure_id, "source_block_id": block_id})
            self.repo.audit_event("block", block_id, "REWIRED_TO_LOCAL_FORK", actor,
                                  {"from_object_id": figure_id, "to_object_id": new_figure_id})
            self._delete_draft("figure", figure_id)
            self._delete_draft("block", block_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"figure_id": new_figure_id, "block_id": block_id, "forked_from_object_id": figure_id,
                "forked_from_version": int(expected_version)}
