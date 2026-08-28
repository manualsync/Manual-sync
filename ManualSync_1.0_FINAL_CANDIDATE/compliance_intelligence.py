"""Milestone 3.5A: Compliance Intelligence core data model & service.
Milestone 3.5A.1: integrity gate -- applicability/coverage contradiction
prevention, read-time staleness propagation, deterministic version-pinned
scope, atomic multi-statement writes, public audit API.

Not a certification engine. Models verifiable, traceable, human-reviewed
compliance READINESS against configured regulatory requirements -- final
responsibility always stays with the manufacturer/competent organization
(see Product Doctrine, "Compliance rule").

Deliberately a SERVICE over an existing IndustrialRepository connection, not
a repository subclass: ComplianceService(repo) wraps any already-open
IndustrialRepository -- same sqlite3 connection, same DB file, same
open/close lifecycle, same audit_events table (reused via
repo.audit_event(), never duplicated).

Domain model (8 new tables, all in the project's existing CCMS SQLite):
see the 3.5A docstring history for the full entity list; unchanged in
3.5A.1 -- this pass only strengthens the invariants enforced by the
service, it does not add or remove tables.

3.5A.1 invariants added on top of 3.5A:

1. Applicability/coverage can never contradict each other IN THE STORED
   DATA, not just in what compute_status() chooses to display:
   - set_assessment() refuses to write unless applicability is currently
     exactly APPLIES for that (scope, requirement) -- a coverage decision
     literally cannot be recorded against a requirement nobody has (yet)
     confirmed applies.
   - set_applicability() moving AWAY from APPLIES actively DELETES any
     existing compliance_assessment row for that (scope, requirement),
     after capturing its prior state into an audit event
     (INVALIDATED_BY_APPLICABILITY_CHANGE) -- so the contradiction is
     invalidated at decision time, not merely hidden at read time, while
     the audit trail still remembers what the assessment used to be.

2. Staleness is read-time, not just a change-impact query someone has to
   remember to run. compute_status() now derives "To verify" whenever:
   - the pinned requirement_version on the applicability/assessment row is
     behind the requirement's current version;
   - the requirement's own instrument_version_at_authoring is behind the
     regulatory instrument's current version (requirement_is_current());
   - a COVERED assessment's backing evidence is no longer current
     (unchanged from 3.5A's _has_valid_current_evidence(), now also
     consulted on READ, not only when a NEW Covered is being written).
   No new UI-facing state is introduced -- staleness always collapses into
   the existing "To verify" bucket, exactly as instructed.
   summary_for_scope() additionally surfaces instrument-staleness
   (stale_requirements) and standard-mapping staleness
   (stale_standard_mappings) as explicit, non-hidden lists, since those
   don't invalidate a Covered status by themselves but must not be
   invisible either.

3. Scope determinism: active_scope_for_publication() (singular) now RAISES
   if more than one ACTIVE scope exists for a publication, instead of
   silently picking the most recent -- concurrent scopes (e.g. per-market)
   are a real, supported case, so ambiguity must be explicit. Use the new
   plural active_scopes_for_publication() to enumerate them all. Scope
   references were already version-pinned in 3.5A (add_scope_reference
   pins ref_version at add time) -- verified, not changed.

4. Every multi-statement write goes through the small _atomic() context
   manager (thin wrapper around this connection's own commit/rollback --
   not a new transaction manager) so a failure partway through never
   leaves a half-written row committed by some later, unrelated call.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ccms_industrial import IndustrialRepository

COMPLIANCE_BUILD = "0.2.0-compliance-core-3-5a-1-integrity-20260828"

APPLICABILITY_STATES = {"APPLIES", "NOT_APPLICABLE", "TO_VERIFY"}
COVERAGE_STATES = {"COVERED", "TO_VERIFY", "MISSING"}
INTERNAL_EVIDENCE_KINDS = {"topic", "block", "table", "figure"}
EXTERNAL_EVIDENCE_KINDS = {"external_file", "customer_document", "human_attestation"}
_EVIDENCE_KINDS = INTERNAL_EVIDENCE_KINDS | EXTERNAL_EVIDENCE_KINDS
_STATUS_DISPLAY = {"COVERED": "Covered", "TO_VERIFY": "To verify", "MISSING": "Missing"}
_INTERNAL_TABLES = {
    "topic": ("topics", "topic_versions", "topic_id"),
    "block": ("content_blocks", "block_versions", "block_id"),
    "table": ("structured_tables", "structured_table_versions", "table_id"),
    "figure": ("figures", "figure_versions", "figure_id"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS compliance_regulatory_instrument (
    instrument_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'regulation',
    official_identifier TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    version INTEGER NOT NULL DEFAULT 1,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    official_source_ref TEXT NOT NULL DEFAULT '',
    supersedes_instrument_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compliance_regulatory_instrument_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    official_identifier TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    official_source_ref TEXT NOT NULL,
    supersedes_instrument_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(instrument_id, version),
    FOREIGN KEY(instrument_id) REFERENCES compliance_regulatory_instrument(instrument_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compliance_standard (
    standard_id TEXT PRIMARY KEY,
    designation TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    edition TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'CURRENT',
    version INTEGER NOT NULL DEFAULT 1,
    harmonized_under_instrument_id TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    external_reference_url TEXT NOT NULL DEFAULT '',
    licensed_content_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compliance_standard_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    designation TEXT NOT NULL,
    title TEXT NOT NULL,
    edition TEXT NOT NULL,
    status TEXT NOT NULL,
    harmonized_under_instrument_id TEXT NOT NULL,
    publisher TEXT NOT NULL,
    external_reference_url TEXT NOT NULL,
    licensed_content_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(standard_id, version),
    FOREIGN KEY(standard_id) REFERENCES compliance_standard(standard_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compliance_requirement (
    requirement_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL DEFAULT '',
    instrument_version_at_authoring INTEGER NOT NULL DEFAULT 0,
    article_ref TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compliance_requirement_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    instrument_version_at_authoring INTEGER NOT NULL,
    article_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(requirement_id, version),
    FOREIGN KEY(requirement_id) REFERENCES compliance_requirement(requirement_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compliance_requirement_standard_link (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id TEXT NOT NULL,
    requirement_version INTEGER NOT NULL,
    standard_id TEXT NOT NULL,
    standard_version INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    UNIQUE(requirement_id, standard_id),
    FOREIGN KEY(requirement_id) REFERENCES compliance_requirement(requirement_id) ON DELETE CASCADE,
    FOREIGN KEY(standard_id) REFERENCES compliance_standard(standard_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_creqstd_req ON compliance_requirement_standard_link(requirement_id);

CREATE TABLE IF NOT EXISTS compliance_risk (
    risk_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    hazard_type TEXT NOT NULL DEFAULT '',
    is_residual INTEGER NOT NULL DEFAULT 0,
    method_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compliance_risk_requirement_link (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    requirement_version INTEGER NOT NULL,
    linked_by TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    UNIQUE(risk_id, requirement_id),
    FOREIGN KEY(risk_id) REFERENCES compliance_risk(risk_id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_id) REFERENCES compliance_requirement(requirement_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_crisklink_req ON compliance_risk_requirement_link(requirement_id);
CREATE INDEX IF NOT EXISTS ix_crisklink_risk ON compliance_risk_requirement_link(risk_id);

CREATE TABLE IF NOT EXISTS compliance_scope (
    scope_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    jurisdiction TEXT NOT NULL DEFAULT '',
    profile_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(publication_id) REFERENCES publications(publication_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_cscope_pub ON compliance_scope(publication_id, status);

CREATE TABLE IF NOT EXISTS compliance_scope_reference (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    ref_version INTEGER NOT NULL,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    UNIQUE(scope_id, ref_kind, ref_id),
    FOREIGN KEY(scope_id) REFERENCES compliance_scope(scope_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_cscoperef_scope ON compliance_scope_reference(scope_id);

CREATE TABLE IF NOT EXISTS compliance_applicability (
    applicability_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    requirement_version INTEGER NOT NULL,
    applies TEXT NOT NULL,
    justification TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    UNIQUE(scope_id, requirement_id),
    FOREIGN KEY(scope_id) REFERENCES compliance_scope(scope_id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_id) REFERENCES compliance_requirement(requirement_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compliance_evidence (
    evidence_id TEXT PRIMARY KEY,
    evidence_kind TEXT NOT NULL,
    object_id TEXT NOT NULL DEFAULT '',
    object_version INTEGER NOT NULL DEFAULT 0,
    external_ref TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compliance_evidence_link (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    requirement_version INTEGER NOT NULL,
    linked_by TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    UNIQUE(evidence_id, requirement_id),
    FOREIGN KEY(evidence_id) REFERENCES compliance_evidence(evidence_id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_id) REFERENCES compliance_requirement(requirement_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_cevlink_req ON compliance_evidence_link(requirement_id);
CREATE INDEX IF NOT EXISTS ix_cevlink_ev ON compliance_evidence_link(evidence_id);

CREATE TABLE IF NOT EXISTS compliance_assessment (
    assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
    requirement_version_at_assessment INTEGER NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    UNIQUE(scope_id, requirement_id),
    FOREIGN KEY(scope_id) REFERENCES compliance_scope(scope_id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_id) REFERENCES compliance_requirement(requirement_id) ON DELETE CASCADE
);
"""


class ComplianceService:
    """Compliance Intelligence core (3.5A/3.5A.1). Wraps an existing
    IndustrialRepository. Does not own the sqlite3 connection or its
    lifecycle -- the caller opens and closes the underlying
    IndustrialRepository as usual. Schema creation is idempotent (CREATE
    TABLE IF NOT EXISTS) and safe to call on every open."""

    def __init__(self, repo: IndustrialRepository):
        self.repo = repo
        self.conn = repo.conn
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    # ---------- internal helpers ----------
    @staticmethod
    def _require_actor(actor: Any) -> str:
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("actor è obbligatorio per registrare questa decisione.")
        return actor

    def _audit(self, object_type: str, object_id: str, action: str, actor: str, detail: dict | None = None) -> None:
        # Public, supported primitive on IndustrialRepository -- no parallel
        # audit log for compliance.
        self.repo.audit_event(object_type, object_id, action, actor, detail or {})

    @contextmanager
    def _atomic(self):
        """Thin wrapper around this connection's own commit/rollback -- not
        a new transaction manager. Every write method that touches more
        than one row/table (or writes then audits) runs inside this, so a
        failure partway through never leaves a half-written change sitting
        in an open transaction that a later, unrelated call could commit by
        accident."""
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def _requirement_row(self, requirement_id: str):
        row = self.conn.execute("SELECT * FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not row:
            raise KeyError(requirement_id)
        return row

    def _instrument_current_version(self, instrument_id: str) -> int | None:
        row = self.conn.execute("SELECT version FROM compliance_regulatory_instrument WHERE instrument_id=?", (instrument_id,)).fetchone()
        return int(row["version"]) if row else None

    def _object_version_exists(self, kind: str, object_id: str, version: int) -> bool:
        _, versions_table, id_col = _INTERNAL_TABLES[kind]
        row = self.conn.execute(
            f"SELECT 1 FROM {versions_table} WHERE {id_col}=? AND version=?", (object_id, int(version))
        ).fetchone()
        return row is not None

    def _object_current_version(self, kind: str, object_id: str) -> int | None:
        table, _, id_col = _INTERNAL_TABLES[kind]
        row = self.conn.execute(f"SELECT version FROM {table} WHERE {id_col}=?", (object_id,)).fetchone()
        return int(row["version"]) if row else None

    def _has_valid_current_evidence(self, requirement_id: str) -> bool:
        for row in self.conn.execute(
            """SELECT e.evidence_kind, e.object_id, e.object_version FROM compliance_evidence e
               JOIN compliance_evidence_link l ON l.evidence_id = e.evidence_id
               WHERE l.requirement_id = ?""",
            (requirement_id,),
        ):
            kind = row["evidence_kind"]
            if kind in EXTERNAL_EVIDENCE_KINDS:
                return True
            if kind in INTERNAL_EVIDENCE_KINDS:
                current = self._object_current_version(kind, row["object_id"])
                if current is not None and current == int(row["object_version"]):
                    return True
        return False

    def requirement_is_current(self, requirement_id: str) -> bool:
        """False when the requirement's own regulatory instrument has moved
        to a newer version since this requirement was authored/last updated
        against it -- the requirement's own text validity is then
        uncertain, so any assessment resting on it must not read as
        current."""
        req = self._requirement_row(requirement_id)
        if not req["instrument_id"]:
            return True
        live = self._instrument_current_version(req["instrument_id"])
        return live is None or live == int(req["instrument_version_at_authoring"])

    # ---------- regulatory instrument ----------
    def create_instrument(self, instrument_id: str, official_identifier: str, *, actor: str, kind: str = "regulation",
                          title: str = "", status: str = "ACTIVE", valid_from: str = "", valid_until: str = "",
                          official_source_ref: str = "", supersedes_instrument_id: str = "") -> str:
        actor = self._require_actor(actor)
        if not str(official_identifier or "").strip():
            raise ValueError("official_identifier è obbligatorio.")
        if self.conn.execute("SELECT 1 FROM compliance_regulatory_instrument WHERE instrument_id=?", (instrument_id,)).fetchone():
            raise ValueError(f"instrument_id già esistente: {instrument_id}")
        now = _now()
        vals = (instrument_id, kind.strip(), official_identifier.strip(), title.strip(), status.strip(), 1,
                valid_from.strip(), valid_until.strip(), official_source_ref.strip(), supersedes_instrument_id.strip(), now, now)
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_regulatory_instrument(instrument_id,kind,official_identifier,title,status,version,
                   valid_from,valid_until,official_source_ref,supersedes_instrument_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
            self.conn.execute(
                """INSERT INTO compliance_regulatory_instrument_versions(instrument_id,version,kind,official_identifier,title,
                   status,valid_from,valid_until,official_source_ref,supersedes_instrument_id,created_at)
                   VALUES(?,1,?,?,?,?,?,?,?,?,?)""",
                (instrument_id, kind.strip(), official_identifier.strip(), title.strip(), status.strip(),
                 valid_from.strip(), valid_until.strip(), official_source_ref.strip(), supersedes_instrument_id.strip(), now))
            self._audit("compliance_regulatory_instrument", instrument_id, "CREATE", actor, {"official_identifier": official_identifier})
        return instrument_id

    def update_instrument(self, instrument_id: str, *, actor: str, **fields: Any) -> int:
        actor = self._require_actor(actor)
        r = self.conn.execute("SELECT * FROM compliance_regulatory_instrument WHERE instrument_id=?", (instrument_id,)).fetchone()
        if not r:
            raise KeyError(instrument_id)
        allowed = {"kind", "official_identifier", "title", "status", "valid_from", "valid_until",
                   "official_source_ref", "supersedes_instrument_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"campi non aggiornabili: {sorted(unknown)}")
        vals = {k: (str(fields[k]).strip() if k in fields else r[k]) for k in allowed}
        v = int(r["version"]) + 1
        now = _now()
        with self._atomic():
            self.conn.execute(
                """UPDATE compliance_regulatory_instrument SET kind=?,official_identifier=?,title=?,status=?,version=?,
                   valid_from=?,valid_until=?,official_source_ref=?,supersedes_instrument_id=?,updated_at=? WHERE instrument_id=?""",
                (vals["kind"], vals["official_identifier"], vals["title"], vals["status"], v, vals["valid_from"],
                 vals["valid_until"], vals["official_source_ref"], vals["supersedes_instrument_id"], now, instrument_id))
            self.conn.execute(
                """INSERT INTO compliance_regulatory_instrument_versions(instrument_id,version,kind,official_identifier,title,
                   status,valid_from,valid_until,official_source_ref,supersedes_instrument_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (instrument_id, v, vals["kind"], vals["official_identifier"], vals["title"], vals["status"],
                 vals["valid_from"], vals["valid_until"], vals["official_source_ref"], vals["supersedes_instrument_id"], now))
            self._audit("compliance_regulatory_instrument", instrument_id, "UPDATE", actor, {"version": v})
        return v

    # ---------- standard ----------
    def create_standard(self, standard_id: str, designation: str, *, actor: str, title: str = "", edition: str = "",
                        status: str = "CURRENT", harmonized_under_instrument_id: str = "", publisher: str = "",
                        external_reference_url: str = "", licensed_content_ref: str = "") -> str:
        actor = self._require_actor(actor)
        if not str(designation or "").strip():
            raise ValueError("designation è obbligatoria.")
        if self.conn.execute("SELECT 1 FROM compliance_standard WHERE standard_id=?", (standard_id,)).fetchone():
            raise ValueError(f"standard_id già esistente: {standard_id}")
        if harmonized_under_instrument_id and not self.conn.execute(
            "SELECT 1 FROM compliance_regulatory_instrument WHERE instrument_id=?", (harmonized_under_instrument_id,)
        ).fetchone():
            raise KeyError(harmonized_under_instrument_id)
        now = _now()
        vals = (standard_id, designation.strip(), title.strip(), edition.strip(), status.strip(), 1,
                harmonized_under_instrument_id.strip(), publisher.strip(), external_reference_url.strip(),
                licensed_content_ref.strip(), now, now)
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_standard(standard_id,designation,title,edition,status,version,
                   harmonized_under_instrument_id,publisher,external_reference_url,licensed_content_ref,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
            self.conn.execute(
                """INSERT INTO compliance_standard_versions(standard_id,version,designation,title,edition,status,
                   harmonized_under_instrument_id,publisher,external_reference_url,licensed_content_ref,created_at)
                   VALUES(?,1,?,?,?,?,?,?,?,?,?)""",
                (standard_id, designation.strip(), title.strip(), edition.strip(), status.strip(),
                 harmonized_under_instrument_id.strip(), publisher.strip(), external_reference_url.strip(),
                 licensed_content_ref.strip(), now))
            self._audit("compliance_standard", standard_id, "CREATE", actor, {"designation": designation})
        return standard_id

    def update_standard(self, standard_id: str, *, actor: str, **fields: Any) -> int:
        actor = self._require_actor(actor)
        r = self.conn.execute("SELECT * FROM compliance_standard WHERE standard_id=?", (standard_id,)).fetchone()
        if not r:
            raise KeyError(standard_id)
        allowed = {"designation", "title", "edition", "status", "harmonized_under_instrument_id", "publisher",
                   "external_reference_url", "licensed_content_ref"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"campi non aggiornabili: {sorted(unknown)}")
        vals = {k: (str(fields[k]).strip() if k in fields else r[k]) for k in allowed}
        v = int(r["version"]) + 1
        now = _now()
        with self._atomic():
            self.conn.execute(
                """UPDATE compliance_standard SET designation=?,title=?,edition=?,status=?,version=?,
                   harmonized_under_instrument_id=?,publisher=?,external_reference_url=?,licensed_content_ref=?,updated_at=?
                   WHERE standard_id=?""",
                (vals["designation"], vals["title"], vals["edition"], vals["status"], v,
                 vals["harmonized_under_instrument_id"], vals["publisher"], vals["external_reference_url"],
                 vals["licensed_content_ref"], now, standard_id))
            self.conn.execute(
                """INSERT INTO compliance_standard_versions(standard_id,version,designation,title,edition,status,
                   harmonized_under_instrument_id,publisher,external_reference_url,licensed_content_ref,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (standard_id, v, vals["designation"], vals["title"], vals["edition"], vals["status"],
                 vals["harmonized_under_instrument_id"], vals["publisher"], vals["external_reference_url"],
                 vals["licensed_content_ref"], now))
            self._audit("compliance_standard", standard_id, "UPDATE", actor, {"version": v})
        return v

    # ---------- requirement ----------
    def create_requirement(self, requirement_id: str, title: str, *, actor: str, instrument_id: str = "",
                           article_ref: str = "", description: str = "", status: str = "ACTIVE") -> str:
        actor = self._require_actor(actor)
        if not str(title or "").strip():
            raise ValueError("title è obbligatorio.")
        if self.conn.execute("SELECT 1 FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone():
            raise ValueError(f"requirement_id già esistente: {requirement_id}")
        instrument_version = 0
        if instrument_id:
            inst = self.conn.execute("SELECT version FROM compliance_regulatory_instrument WHERE instrument_id=?", (instrument_id,)).fetchone()
            if not inst:
                raise KeyError(instrument_id)
            instrument_version = int(inst["version"])
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_requirement(requirement_id,instrument_id,instrument_version_at_authoring,
                   article_ref,title,description,status,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)""",
                (requirement_id, instrument_id, instrument_version, article_ref.strip(), title.strip(),
                 description.strip(), status.strip(), now, now))
            self.conn.execute(
                """INSERT INTO compliance_requirement_versions(requirement_id,version,instrument_id,
                   instrument_version_at_authoring,article_ref,title,description,status,created_at)
                   VALUES(?,1,?,?,?,?,?,?,?)""",
                (requirement_id, instrument_id, instrument_version, article_ref.strip(), title.strip(),
                 description.strip(), status.strip(), now))
            self._audit("compliance_requirement", requirement_id, "CREATE", actor, {"instrument_id": instrument_id})
        return requirement_id

    def update_requirement(self, requirement_id: str, *, actor: str, **fields: Any) -> int:
        actor = self._require_actor(actor)
        r = self.conn.execute("SELECT * FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not r:
            raise KeyError(requirement_id)
        allowed = {"instrument_id", "article_ref", "title", "description", "status"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"campi non aggiornabili: {sorted(unknown)}")
        vals = {k: (str(fields[k]).strip() if k in fields else r[k]) for k in allowed}
        instrument_version = int(r["instrument_version_at_authoring"])
        if "instrument_id" in fields and vals["instrument_id"]:
            inst = self.conn.execute("SELECT version FROM compliance_regulatory_instrument WHERE instrument_id=?", (vals["instrument_id"],)).fetchone()
            if not inst:
                raise KeyError(vals["instrument_id"])
            instrument_version = int(inst["version"])
        v = int(r["version"]) + 1
        now = _now()
        with self._atomic():
            self.conn.execute(
                """UPDATE compliance_requirement SET instrument_id=?,instrument_version_at_authoring=?,article_ref=?,
                   title=?,description=?,status=?,version=?,updated_at=? WHERE requirement_id=?""",
                (vals["instrument_id"], instrument_version, vals["article_ref"], vals["title"], vals["description"],
                 vals["status"], v, now, requirement_id))
            self.conn.execute(
                """INSERT INTO compliance_requirement_versions(requirement_id,version,instrument_id,
                   instrument_version_at_authoring,article_ref,title,description,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (requirement_id, v, vals["instrument_id"], instrument_version, vals["article_ref"], vals["title"],
                 vals["description"], vals["status"], now))
            self._audit("compliance_requirement", requirement_id, "UPDATE", actor, {"version": v})
        return v

    def link_requirement_standard(self, requirement_id: str, standard_id: str, *, actor: str, note: str = "") -> int:
        actor = self._require_actor(actor)
        req = self.conn.execute("SELECT version FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not req:
            raise KeyError(requirement_id)
        std = self.conn.execute("SELECT version FROM compliance_standard WHERE standard_id=?", (standard_id,)).fetchone()
        if not std:
            raise KeyError(standard_id)
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_requirement_standard_link(requirement_id,requirement_version,standard_id,
                   standard_version,note,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(requirement_id,standard_id) DO UPDATE SET requirement_version=excluded.requirement_version,
                   standard_version=excluded.standard_version,note=excluded.note,confirmed_by=excluded.confirmed_by,
                   confirmed_at=excluded.confirmed_at""",
                (requirement_id, int(req["version"]), standard_id, int(std["version"]), note.strip(), actor, now))
            self._audit("compliance_requirement", requirement_id, "LINK_STANDARD", actor, {"standard_id": standard_id})
        return int(req["version"])

    # ---------- risk ----------
    def create_risk(self, risk_id: str, title: str, *, actor: str, description: str = "", hazard_type: str = "",
                    is_residual: bool = False, method_metadata: dict | None = None) -> str:
        actor = self._require_actor(actor)
        if not str(title or "").strip():
            raise ValueError("title è obbligatorio.")
        if self.conn.execute("SELECT 1 FROM compliance_risk WHERE risk_id=?", (risk_id,)).fetchone():
            raise ValueError(f"risk_id già esistente: {risk_id}")
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_risk(risk_id,title,description,hazard_type,is_residual,method_metadata_json,
                   created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (risk_id, title.strip(), description.strip(), hazard_type.strip(), 1 if is_residual else 0,
                 _json(method_metadata or {}), actor, now, now))
            self._audit("compliance_risk", risk_id, "CREATE", actor, {})
        return risk_id

    def link_risk_requirement(self, risk_id: str, requirement_id: str, *, actor: str) -> int:
        actor = self._require_actor(actor)
        if not self.conn.execute("SELECT 1 FROM compliance_risk WHERE risk_id=?", (risk_id,)).fetchone():
            raise KeyError(risk_id)
        req = self.conn.execute("SELECT version FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not req:
            raise KeyError(requirement_id)
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_risk_requirement_link(risk_id,requirement_id,requirement_version,linked_by,linked_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(risk_id,requirement_id) DO UPDATE SET requirement_version=excluded.requirement_version,
                   linked_by=excluded.linked_by,linked_at=excluded.linked_at""",
                (risk_id, requirement_id, int(req["version"]), actor, now))
            self._audit("compliance_risk", risk_id, "LINK_REQUIREMENT", actor, {"requirement_id": requirement_id})
        return int(req["version"])

    # ---------- scope ----------
    def create_scope(self, scope_id: str, publication_id: str, *, actor: str, title: str = "", jurisdiction: str = "",
                     profile_id: str = "", notes: str = "", status: str = "ACTIVE") -> str:
        actor = self._require_actor(actor)
        if not self.conn.execute("SELECT 1 FROM publications WHERE publication_id=?", (publication_id,)).fetchone():
            raise KeyError(publication_id)
        if self.conn.execute("SELECT 1 FROM compliance_scope WHERE scope_id=?", (scope_id,)).fetchone():
            raise ValueError(f"scope_id già esistente: {scope_id}")
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_scope(scope_id,publication_id,title,jurisdiction,profile_id,status,notes,
                   created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (scope_id, publication_id, title.strip(), jurisdiction.strip(), profile_id.strip(), status.strip(),
                 notes.strip(), actor, now, now))
            self._audit("compliance_scope", scope_id, "CREATE", actor, {"publication_id": publication_id})
        return scope_id

    def add_scope_reference(self, scope_id: str, ref_kind: str, ref_id: str, *, actor: str) -> int:
        actor = self._require_actor(actor)
        ref_kind = str(ref_kind or "").strip().lower()
        if ref_kind not in {"instrument", "standard"}:
            raise ValueError("ref_kind deve essere 'instrument' o 'standard'")
        if not self.conn.execute("SELECT 1 FROM compliance_scope WHERE scope_id=?", (scope_id,)).fetchone():
            raise KeyError(scope_id)
        table, id_col = ("compliance_regulatory_instrument", "instrument_id") if ref_kind == "instrument" else ("compliance_standard", "standard_id")
        row = self.conn.execute(f"SELECT version FROM {table} WHERE {id_col}=?", (ref_id,)).fetchone()
        if not row:
            raise KeyError(ref_id)
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_scope_reference(scope_id,ref_kind,ref_id,ref_version,added_by,added_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(scope_id,ref_kind,ref_id) DO UPDATE SET ref_version=excluded.ref_version,
                   added_by=excluded.added_by,added_at=excluded.added_at""",
                (scope_id, ref_kind, ref_id, int(row["version"]), actor, now))
            self._audit("compliance_scope", scope_id, "ADD_REFERENCE", actor, {"ref_kind": ref_kind, "ref_id": ref_id})
        return int(row["version"])

    def active_scopes_for_publication(self, publication_id: str) -> list[str]:
        """All ACTIVE scopes for this publication, oldest first. Concurrent
        scopes (e.g. one per market/jurisdiction) are a real, supported
        case -- this is the unambiguous way to enumerate them."""
        return [r["scope_id"] for r in self.conn.execute(
            "SELECT scope_id FROM compliance_scope WHERE publication_id=? AND status='ACTIVE' ORDER BY created_at",
            (publication_id,))]

    def active_scope_for_publication(self, publication_id: str) -> str | None:
        """Convenience singular accessor for the common single-scope case.
        Raises if the publication genuinely has more than one ACTIVE scope
        -- it never silently picks one; callers that must support several
        concurrent scopes should use active_scopes_for_publication()."""
        scopes = self.active_scopes_for_publication(publication_id)
        if len(scopes) > 1:
            raise ValueError(
                f"Più di uno scope ACTIVE per la publication {publication_id} ({scopes}): "
                "usa active_scopes_for_publication() ed esplicita quale scope intendi -- "
                "questa funzione singolare non sceglie arbitrariamente."
            )
        return scopes[0] if scopes else None

    # ---------- applicability ----------
    def set_applicability(self, scope_id: str, requirement_id: str, applies: str, *, actor: str,
                          justification: str = "", comment: str = "") -> int:
        actor = self._require_actor(actor)
        applies = str(applies or "").strip().upper()
        if applies not in APPLICABILITY_STATES:
            raise ValueError(f"applies deve essere uno tra {sorted(APPLICABILITY_STATES)}")
        if applies == "NOT_APPLICABLE" and not str(justification or "").strip():
            raise ValueError("Una decisione 'Not applicable' richiede una giustificazione esplicita.")
        if not self.conn.execute("SELECT 1 FROM compliance_scope WHERE scope_id=?", (scope_id,)).fetchone():
            raise KeyError(scope_id)
        req = self.conn.execute("SELECT version FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not req:
            raise KeyError(requirement_id)
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_applicability(scope_id,requirement_id,requirement_version,applies,
                   justification,decided_by,decided_at,comment) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope_id,requirement_id) DO UPDATE SET requirement_version=excluded.requirement_version,
                   applies=excluded.applies,justification=excluded.justification,decided_by=excluded.decided_by,
                   decided_at=excluded.decided_at,comment=excluded.comment""",
                (scope_id, requirement_id, int(req["version"]), applies, justification.strip(), actor, now, comment.strip()))
            self._audit("compliance_applicability", f"{scope_id}::{requirement_id}", f"APPLICABILITY_{applies}", actor,
                        {"justification": justification, "comment": comment})
            if applies != "APPLIES":
                # Invariant: a compliance_assessment row may only exist while
                # applicability is APPLIES. Moving away from APPLIES actively
                # invalidates a stale/contradictory coverage row right now,
                # instead of leaving compute_status() to merely mask it.
                stale = self.conn.execute(
                    "SELECT status,decided_by,decided_at,comment FROM compliance_assessment WHERE scope_id=? AND requirement_id=?",
                    (scope_id, requirement_id)).fetchone()
                if stale:
                    self.conn.execute(
                        "DELETE FROM compliance_assessment WHERE scope_id=? AND requirement_id=?", (scope_id, requirement_id))
                    self._audit(
                        "compliance_assessment", f"{scope_id}::{requirement_id}", "INVALIDATED_BY_APPLICABILITY_CHANGE",
                        actor, {"previous_status": stale["status"], "previous_decided_by": stale["decided_by"],
                                "previous_decided_at": stale["decided_at"], "new_applicability": applies})
        return int(req["version"])

    # ---------- evidence ----------
    def add_evidence(self, evidence_id: str, evidence_kind: str, *, actor: str, object_id: str = "",
                     object_version: int | None = None, external_ref: str = "", description: str = "") -> str:
        actor = self._require_actor(actor)
        kind = str(evidence_kind or "").strip().lower()
        if kind not in _EVIDENCE_KINDS:
            raise ValueError(f"evidence_kind non supportato: {kind}")
        if kind in INTERNAL_EVIDENCE_KINDS:
            if not object_id or object_version is None:
                raise ValueError("Un'evidenza interna richiede object_id e object_version.")
            if not self._object_version_exists(kind, object_id, int(object_version)):
                raise KeyError(f"{kind}:{object_id}@v{object_version} non esiste nel CCMS di questo progetto.")
        else:
            if not str(external_ref or "").strip() and not str(description or "").strip():
                raise ValueError("Un'evidenza esterna richiede almeno external_ref o description per essere verificabile.")
        if self.conn.execute("SELECT 1 FROM compliance_evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
            raise ValueError(f"evidence_id già esistente: {evidence_id}")
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_evidence(evidence_id,evidence_kind,object_id,object_version,external_ref,
                   description,added_by,added_at) VALUES(?,?,?,?,?,?,?,?)""",
                (evidence_id, kind, object_id, int(object_version) if object_version is not None else 0,
                 external_ref.strip(), description.strip(), actor, now))
            self._audit("compliance_evidence", evidence_id, "CREATE", actor, {"kind": kind, "object_id": object_id})
        return evidence_id

    def link_evidence_requirement(self, evidence_id: str, requirement_id: str, *, actor: str) -> int:
        actor = self._require_actor(actor)
        if not self.conn.execute("SELECT 1 FROM compliance_evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
            raise KeyError(evidence_id)
        req = self.conn.execute("SELECT version FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not req:
            raise KeyError(requirement_id)
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_evidence_link(evidence_id,requirement_id,requirement_version,linked_by,linked_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(evidence_id,requirement_id) DO UPDATE SET requirement_version=excluded.requirement_version,
                   linked_by=excluded.linked_by,linked_at=excluded.linked_at""",
                (evidence_id, requirement_id, int(req["version"]), actor, now))
            self._audit("compliance_evidence", evidence_id, "LINK_REQUIREMENT", actor, {"requirement_id": requirement_id})
        return int(req["version"])

    # ---------- assessment (coverage) ----------
    def set_assessment(self, scope_id: str, requirement_id: str, status: str, *, actor: str, comment: str = "") -> int:
        actor = self._require_actor(actor)
        status = str(status or "").strip().upper()
        if status not in COVERAGE_STATES:
            raise ValueError(f"status deve essere uno tra {sorted(COVERAGE_STATES)}")
        if not self.conn.execute("SELECT 1 FROM compliance_scope WHERE scope_id=?", (scope_id,)).fetchone():
            raise KeyError(scope_id)
        req = self.conn.execute("SELECT version FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not req:
            raise KeyError(requirement_id)
        ap = self.conn.execute(
            "SELECT applies FROM compliance_applicability WHERE scope_id=? AND requirement_id=?",
            (scope_id, requirement_id)).fetchone()
        if not ap or ap["applies"] != "APPLIES":
            raise ValueError(
                "Impossibile registrare una coverage assessment: il requisito non è attualmente 'APPLIES' in "
                "questo scope. Imposta prima set_applicability(..., 'APPLIES', ...) -- applicability e coverage "
                "non possono contraddirsi."
            )
        if status == "COVERED" and not self._has_valid_current_evidence(requirement_id):
            raise ValueError(
                "Impossibile impostare 'Covered': nessuna evidenza valida e corrente collegata a questo requisito. "
                "Collega un'evidenza interna aggiornata o un'evidenza esterna con provenance prima di dichiarare copertura."
            )
        now = _now()
        with self._atomic():
            self.conn.execute(
                """INSERT INTO compliance_assessment(scope_id,requirement_id,requirement_version_at_assessment,status,
                   decided_by,decided_at,comment) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(scope_id,requirement_id) DO UPDATE SET requirement_version_at_assessment=excluded.requirement_version_at_assessment,
                   status=excluded.status,decided_by=excluded.decided_by,decided_at=excluded.decided_at,comment=excluded.comment""",
                (scope_id, requirement_id, int(req["version"]), status, actor, now, comment.strip()))
            self._audit("compliance_assessment", f"{scope_id}::{requirement_id}", f"ASSESS_{status}", actor, {"comment": comment})
        return int(req["version"])

    # ---------- derived status (single source of truth, staleness-aware) ----------
    def compute_status(self, scope_id: str, requirement_id: str) -> str:
        """Applicability and coverage are never two independent sources of
        truth, and neither is ever trusted blindly against a moving
        requirement/instrument/evidence underneath them:
        - NOT_APPLICABLE always wins over any stored assessment (and, by
          construction in set_applicability(), no contradictory assessment
          row can even exist any more -- see class docstring point 1).
        - a decision (applicability OR assessment) pinned to a requirement
          version that is no longer current reads as "To verify", not as
          whatever stale status it used to hold.
        - a requirement whose own instrument reference is now stale reads
          the same way.
        - a COVERED assessment whose backing evidence is no longer current
          reads as "To verify", re-checked on every read, not only when it
          was first written.
        No new UI-facing state: staleness always collapses into the
        existing "To verify" bucket."""
        req = self._requirement_row(requirement_id)
        current_req_version = int(req["version"])
        requirement_current = self.requirement_is_current(requirement_id)

        ap = self.conn.execute(
            "SELECT applies, requirement_version FROM compliance_applicability WHERE scope_id=? AND requirement_id=?",
            (scope_id, requirement_id),
        ).fetchone()
        if not ap or ap["applies"] == "TO_VERIFY":
            return "To verify"
        applicability_stale = int(ap["requirement_version"]) != current_req_version
        if ap["applies"] == "NOT_APPLICABLE":
            return "To verify" if applicability_stale else "Not applicable"

        # APPLIES
        if applicability_stale or not requirement_current:
            return "To verify"
        asm = self.conn.execute(
            "SELECT status, requirement_version_at_assessment FROM compliance_assessment WHERE scope_id=? AND requirement_id=?",
            (scope_id, requirement_id),
        ).fetchone()
        if not asm:
            return "To verify"
        if int(asm["requirement_version_at_assessment"]) != current_req_version:
            return "To verify"
        if asm["status"] == "COVERED" and not self._has_valid_current_evidence(requirement_id):
            return "To verify"
        return _STATUS_DISPLAY[asm["status"]]

    def requirements_in_scope(self, scope_id: str) -> list[str]:
        ids: set[str] = set()
        for r in self.conn.execute(
            """SELECT cr.requirement_id FROM compliance_requirement cr
               JOIN compliance_scope_reference sr ON sr.ref_kind='instrument' AND sr.ref_id=cr.instrument_id
               WHERE sr.scope_id=?""",
            (scope_id,),
        ):
            ids.add(r["requirement_id"])
        for r in self.conn.execute("SELECT DISTINCT requirement_id FROM compliance_applicability WHERE scope_id=?", (scope_id,)):
            ids.add(r["requirement_id"])
        for r in self.conn.execute("SELECT DISTINCT requirement_id FROM compliance_assessment WHERE scope_id=?", (scope_id,)):
            ids.add(r["requirement_id"])
        return sorted(ids)

    def summary_for_scope(self, scope_id: str) -> dict[str, Any]:
        if not self.conn.execute("SELECT 1 FROM compliance_scope WHERE scope_id=?", (scope_id,)).fetchone():
            raise KeyError(scope_id)
        reqs = self.requirements_in_scope(scope_id)
        counts = {"Covered": 0, "To verify": 0, "Missing": 0, "Not applicable": 0}
        rows = []
        stale_requirements = []
        stale_standard_mappings = []
        for rid in reqs:
            status = self.compute_status(scope_id, rid)
            counts[status] += 1
            rows.append({"requirement_id": rid, "status": status})
            if not self.requirement_is_current(rid):
                stale_requirements.append(rid)
            for link in self.conn.execute(
                """SELECT l.standard_id, l.standard_version, s.version AS current_version
                   FROM compliance_requirement_standard_link l JOIN compliance_standard s ON s.standard_id=l.standard_id
                   WHERE l.requirement_id=? AND l.standard_version<>s.version""", (rid,)):
                stale_standard_mappings.append({
                    "requirement_id": rid, "standard_id": link["standard_id"],
                    "mapped_version": link["standard_version"], "current_version": link["current_version"],
                })
        return {
            "scope_id": scope_id, "total": len(reqs), "counts": counts, "requirements": rows,
            "stale_requirements": stale_requirements, "stale_standard_mappings": stale_standard_mappings,
        }

    # ---------- change impact (data-level, no monitoring engine) ----------
    def change_impact_for_requirement(self, requirement_id: str) -> dict[str, Any]:
        req = self.conn.execute("SELECT version FROM compliance_requirement WHERE requirement_id=?", (requirement_id,)).fetchone()
        if not req:
            raise KeyError(requirement_id)
        current = int(req["version"])
        stale_applicability = [dict(r) for r in self.conn.execute(
            "SELECT scope_id,requirement_version FROM compliance_applicability WHERE requirement_id=? AND requirement_version<>?",
            (requirement_id, current))]
        stale_assessment = [dict(r) for r in self.conn.execute(
            "SELECT scope_id,requirement_version_at_assessment FROM compliance_assessment WHERE requirement_id=? AND requirement_version_at_assessment<>?",
            (requirement_id, current))]
        stale_evidence_links = [dict(r) for r in self.conn.execute(
            "SELECT evidence_id,requirement_version FROM compliance_evidence_link WHERE requirement_id=? AND requirement_version<>?",
            (requirement_id, current))]
        stale_risk_links = [dict(r) for r in self.conn.execute(
            "SELECT risk_id,requirement_version FROM compliance_risk_requirement_link WHERE requirement_id=? AND requirement_version<>?",
            (requirement_id, current))]
        stale_standard_links = [dict(r) for r in self.conn.execute(
            "SELECT standard_id,requirement_version FROM compliance_requirement_standard_link WHERE requirement_id=? AND requirement_version<>?",
            (requirement_id, current))]
        affected_scopes = sorted({r["scope_id"] for r in stale_applicability} | {r["scope_id"] for r in stale_assessment})
        affected_publications = sorted({
            row["publication_id"] for sid in affected_scopes
            for row in self.conn.execute("SELECT publication_id FROM compliance_scope WHERE scope_id=?", (sid,))
        })
        return {
            "requirement_id": requirement_id, "current_version": current,
            "stale_applicability": stale_applicability, "stale_assessment": stale_assessment,
            "stale_evidence_links": stale_evidence_links, "stale_risk_links": stale_risk_links,
            "stale_standard_links": stale_standard_links,
            "affected_scopes": affected_scopes, "affected_publications": affected_publications,
        }

    def change_impact_for_instrument(self, instrument_id: str) -> dict[str, Any]:
        inst = self.conn.execute("SELECT version FROM compliance_regulatory_instrument WHERE instrument_id=?", (instrument_id,)).fetchone()
        if not inst:
            raise KeyError(instrument_id)
        current = int(inst["version"])
        stale_requirements = [dict(r) for r in self.conn.execute(
            "SELECT requirement_id,instrument_version_at_authoring FROM compliance_requirement WHERE instrument_id=? AND instrument_version_at_authoring<>?",
            (instrument_id, current))]
        stale_scope_refs = [dict(r) for r in self.conn.execute(
            "SELECT scope_id,ref_version FROM compliance_scope_reference WHERE ref_kind='instrument' AND ref_id=? AND ref_version<>?",
            (instrument_id, current))]
        return {"instrument_id": instrument_id, "current_version": current,
                "stale_requirements": stale_requirements, "stale_scope_references": stale_scope_refs}
