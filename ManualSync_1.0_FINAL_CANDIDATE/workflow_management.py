from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ccms_industrial import IndustrialRepository
from project_layout import ProjectLayout
from translation_roundtrip import (
    CONFIRMED_TM_NAME, RoundtripCommitInterrupted, inspect_translation_roundtrip, commit_translation_roundtrip,
)

WORKFLOW_BUILD = "0.25.0-workflow-job-management-20260811"

# translation_jobs columns that hold a file of the project, with the column that holds the
# file's SHA-256 when the job recorded one.
JOB_PATH_COLUMNS = {
    "package_path": "package_sha256",
    "returned_xliff_path": "returned_xliff_sha256",
    "qa_terminology_path": "qa_terminology_sha256",
    "approval_zip": "",
    "confirmed_tm_path": "",
}

STATUS_LABELS = {
    "READY_TO_SEND": "Da inviare",
    "SENT": "Inviato",
    "IN_TRANSLATION": "In traduzione",
    "RECEIVED": "Ricevuto",
    "QA_BLOCKED": "QA bloccato",
    "QA_READY": "QA pronto",
    "APPROVED": "Approvato",
    "CANCELLED": "Annullato",
}

ALLOWED_TRANSITIONS = {
    "READY_TO_SEND": {"SENT", "CANCELLED"},
    "SENT": {"IN_TRANSLATION", "RECEIVED", "CANCELLED"},
    "IN_TRANSLATION": {"RECEIVED", "CANCELLED"},
    "RECEIVED": {"QA_READY", "QA_BLOCKED", "CANCELLED"},
    "QA_BLOCKED": {"RECEIVED", "CANCELLED"},
    "QA_READY": {"APPROVED", "QA_BLOCKED", "CANCELLED"},
    "APPROVED": set(),
    "CANCELLED": set(),
}


class JobFileMissingError(FileNotFoundError):
    """A file the job needs is not in THIS project folder (CP5.4c): restored elsewhere without it,
    removed, or replaced by a file with another content."""


class ApprovalInterruptedError(RuntimeError):
    """approve_job stopped after the round-trip commit had begun writing (CP5.4c).

    ``translation_committed``: the CCMS translations and the audit row are in, only the approval
    is not closed. Either way the job is still QA_READY and repeating the approval completes it.
    """

    def __init__(self, message: str, *, job_id: str, phase: str, translation_committed: bool):
        super().__init__(message)
        self.job_id = job_id
        self.phase = phase
        self.translation_committed = translation_committed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return out or "unknown"


def _lexical(path: str | Path) -> Path:
    """Absolute and normalized ('..' collapsed) WITHOUT following links: a project folder or an
    assets folder that is a junction stays inside the project."""
    return Path(os.path.abspath(path))


def _validate_due_date(value: str | None) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("La scadenza deve essere nel formato YYYY-MM-DD.") from exc
    return value


def _read_package_manifest(package_zip: str | Path) -> dict[str, Any]:
    p = Path(package_zip)
    if not p.exists():
        raise FileNotFoundError(p)
    with zipfile.ZipFile(p) as zf:
        if "MANIFEST.json" not in zf.namelist():
            raise ValueError("Pacchetto non valido: MANIFEST.json mancante.")
        manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8"))
    source = manifest.get("source", {})
    required = [source.get("kind"), source.get("id"), source.get("version"), source.get("source_lang"), manifest.get("target_lang")]
    if any(x in (None, "") for x in required):
        raise ValueError("Manifest del pacchetto incompleto.")
    return manifest


class WorkflowManager:
    """Persistent operational layer for translation jobs.

    It deliberately lives beside the CCMS database instead of changing the
    validated CCMS/CAT schemas. Every state change is audited. Translation QA
    and final commit reuse the v0.24 fail-closed engine rather than duplicating
    its rules.
    """

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.project_dir / "ManualSync_Workflow.sqlite"
        self.assets_dir = self.project_dir / "ManualSync_Workflow_Assets"
        self.assets_dir.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    # ---------- Paths of the job files ----------
    # CP5.4c. Up to CP5.4b every path was stored absolute: it tied the project to the folder --
    # and the machine -- that created it. After a restore elsewhere a job still in progress could
    # no longer be approved, and every archive carried C:\Users\<name>. Paths are now stored
    # relative to the project folder; the absolute ones already stored are read, never rewritten.
    def _stored(self, path: str | Path) -> str:
        """How a file of this project is persisted: relative to the project folder, '/'-separated."""
        return _lexical(path).relative_to(_lexical(self.project_dir)).as_posix()

    def _resolve(self, stored: str, sha256: str = "") -> Path | None:
        """Where a persisted path is in THIS project folder, or None.

        A relative value is resolved against the project folder (and refused if it leads out of
        it). An absolute value was stored before CP5.4c: used as is while it points into this
        project; otherwise -- the project was restored or moved -- re-anchored here on the layout
        (the ManualSync_Workflow_Assets/... part, or the confirmed TM), and accepted only if that
        file exists and, when the job recorded its hash, has that same content.
        """
        if not stored:
            return None
        root = _lexical(self.project_dir)
        p = Path(stored)
        if not p.is_absolute():
            candidate = _lexical(root / p)
            if not candidate.is_relative_to(root):
                return None
            # Before CP5.4c a project opened through a relative folder stored paths relative to
            # the working directory: still accepted while they point into this project.
            cwd_relative = _lexical(p)
            if not candidate.is_file() and cwd_relative.is_relative_to(root) and cwd_relative.is_file():
                return cwd_relative
            return candidate
        if _lexical(p).is_relative_to(root):
            return p
        if self.assets_dir.name in p.parts:
            i = len(p.parts) - 1 - p.parts[::-1].index(self.assets_dir.name)
            candidate = root.joinpath(*p.parts[i:])
        elif p.name == CONFIRMED_TM_NAME:
            candidate = root / p.name
        else:
            return None
        try:
            if not candidate.is_file() or (sha256 and _sha256(candidate) != sha256):
                return None
        except OSError:
            return None
        return candidate

    def _job_file(self, job: dict[str, Any], column: str) -> Path:
        """A file the next step of the job reads: it must be in THIS project -- never another
        project's copy, even one still on disk. Fail-closed, with a sentence the user can act on."""
        p = Path(str(job[column] or ""))
        if p.is_absolute() and p.is_file() and _lexical(p).is_relative_to(_lexical(self.project_dir)):
            return p
        raise JobFileMissingError(
            f"Il file «{p.name}» del job {job['job_id']} non si trova in questo progetto: era registrato in un'altra "
            "cartella oppure è stato rimosso o sostituito. Nulla è stato modificato. Ripristina il progetto completo, "
            "oppure genera un nuovo pacchetto e crea un nuovo job.")

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_project (
                project_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                client TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vendors (
                vendor_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS translation_jobs (
                job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                vendor_id TEXT NOT NULL DEFAULT '',
                due_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                work_segments INTEGER NOT NULL DEFAULT 0,
                work_words INTEGER NOT NULL DEFAULT 0,
                linguistic_review_segments INTEGER NOT NULL DEFAULT 0,
                technical_review_segments INTEGER NOT NULL DEFAULT 0,
                new_translation_segments INTEGER NOT NULL DEFAULT 0,
                package_path TEXT NOT NULL,
                package_sha256 TEXT NOT NULL,
                returned_xliff_path TEXT NOT NULL DEFAULT '',
                returned_xliff_sha256 TEXT NOT NULL DEFAULT '',
                qa_status TEXT NOT NULL DEFAULT '',
                qa_summary_json TEXT NOT NULL DEFAULT '',
                qa_terminology_path TEXT NOT NULL DEFAULT '',
                qa_terminology_sha256 TEXT NOT NULL DEFAULT '',
                qa_strict INTEGER NOT NULL DEFAULT 0,
                reviewer TEXT NOT NULL DEFAULT '',
                approval_zip TEXT NOT NULL DEFAULT '',
                confirmed_tm_path TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL DEFAULT '',
                approved_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(project_id) REFERENCES workflow_project(project_id)
            );
            CREATE INDEX IF NOT EXISTS ix_jobs_status ON translation_jobs(status);
            CREATE INDEX IF NOT EXISTS ix_jobs_target_lang ON translation_jobs(target_lang);
            CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_status TEXT NOT NULL DEFAULT '',
                to_status TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES translation_jobs(job_id) ON DELETE CASCADE
            );
            """
        )
        self.conn.commit()
        if not self.conn.execute("SELECT 1 FROM workflow_project WHERE project_id='MAIN'").fetchone():
            now = _now()
            self.conn.execute(
                "INSERT INTO workflow_project(project_id,title,client,owner,created_at,updated_at) VALUES('MAIN',?,?,?,?,?)",
                (self.project_dir.name or "ManualSync Project", "", "", now, now),
            )
            self.conn.commit()

    def set_project_metadata(self, title: str, client: str = "", owner: str = "") -> None:
        now = _now()
        self.conn.execute(
            "UPDATE workflow_project SET title=?,client=?,owner=?,updated_at=? WHERE project_id='MAIN'",
            (str(title or "ManualSync Project").strip(), str(client or "").strip(), str(owner or "").strip(), now),
        )
        self.conn.commit()

    def project_metadata(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM workflow_project WHERE project_id='MAIN'").fetchone()
        return dict(row) if row else {}

    def upsert_vendor(self, vendor_id: str, name: str, email: str = "", notes: str = "") -> pd.DataFrame:
        vid = str(vendor_id or "").strip()
        if not vid or not str(name or "").strip():
            raise ValueError("Vendor ID e nome fornitore sono obbligatori.")
        now = _now()
        self.conn.execute(
            """INSERT INTO vendors(vendor_id,name,email,notes,active,created_at,updated_at)
               VALUES(?,?,?,?,1,?,?)
               ON CONFLICT(vendor_id) DO UPDATE SET name=excluded.name,email=excluded.email,notes=excluded.notes,active=1,updated_at=excluded.updated_at""",
            (vid, str(name).strip(), str(email or "").strip(), str(notes or "").strip(), now, now),
        )
        self.conn.commit()
        return self.vendors_table()

    def vendors_table(self) -> pd.DataFrame:
        rows = self.conn.execute("SELECT vendor_id,name,email,notes,active,updated_at FROM vendors ORDER BY name").fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def _new_job_id(self, target_lang: str) -> str:
        stem = f"JOB-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_safe(target_lang)}"
        candidate = stem
        n = 2
        while self.conn.execute("SELECT 1 FROM translation_jobs WHERE job_id=?", (candidate,)).fetchone():
            candidate = f"{stem}-{n}"; n += 1
        return candidate

    def _event(self, job_id: str, event_type: str, from_status: str = "", to_status: str = "", actor: str = "", detail: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO workflow_events(job_id,event_type,from_status,to_status,actor,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, event_type, from_status, to_status, str(actor or ""), json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), _now()),
        )

    def create_job_from_package(self, package_zip: str | Path, *, vendor_id: str = "", due_date: str = "", notes: str = "", actor: str = "") -> str:
        manifest = _read_package_manifest(package_zip)
        due = _validate_due_date(due_date)
        vendor_id = str(vendor_id or "").strip()
        if vendor_id and not self.conn.execute("SELECT 1 FROM vendors WHERE vendor_id=? AND active=1", (vendor_id,)).fetchone():
            raise ValueError(f"Fornitore non trovato/attivo: {vendor_id}")
        source = manifest["source"]
        target_lang = str(manifest["target_lang"])
        job_id = self._new_job_id(target_lang)
        job_dir = self.assets_dir / job_id
        outbound = job_dir / "outbound"; outbound.mkdir(parents=True, exist_ok=True)
        package_copy = outbound / Path(package_zip).name
        shutil.copy2(package_zip, package_copy)
        counts = manifest.get("counts", {})
        now = _now()
        self.conn.execute(
            """INSERT INTO translation_jobs(
               job_id,project_id,source_kind,source_id,source_version,source_lang,target_lang,vendor_id,due_date,status,
               work_segments,work_words,linguistic_review_segments,technical_review_segments,new_translation_segments,
               package_path,package_sha256,notes,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, "MAIN", str(source["kind"]), str(source["id"]), int(source["version"]), str(source["source_lang"]), target_lang,
             vendor_id, due, "READY_TO_SEND", int(counts.get("work_segments", 0)), int(counts.get("work_words", 0)),
             int(counts.get("linguistic_review_segments", 0)), int(counts.get("technical_review_segments", 0)), int(counts.get("new_translation_segments", 0)),
             self._stored(package_copy), _sha256(package_copy), str(notes or "").strip(), now, now),
        )
        self._event(job_id, "JOB_CREATED", "", "READY_TO_SEND", actor, {"package_sha256": _sha256(package_copy)})
        self.conn.commit()
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any]:
        """The job row; its file paths resolved in this project folder (see _resolve). A path that
        cannot be resolved is returned as stored."""
        row = self.conn.execute("SELECT * FROM translation_jobs WHERE job_id=?", (str(job_id).strip(),)).fetchone()
        if not row:
            raise KeyError(job_id)
        job = dict(row)
        for column, sha_column in JOB_PATH_COLUMNS.items():
            found = self._resolve(job[column], job[sha_column] if sha_column else "")
            if found is not None:
                job[column] = str(found)
        return job

    def transition(self, job_id: str, to_status: str, *, actor: str = "", detail: Any = None,
                   acknowledge_interrupted_approval: bool = False) -> dict[str, Any]:
        """Change the job status. Cancelling a job whose approval was interrupted after it began
        writing (interrupted_approval) leaves that work in the TM / CCMS: it needs
        ``acknowledge_interrupted_approval``, and the event records it."""
        if str(to_status or "").strip().upper() == "CANCELLED":
            interrupted = self.interrupted_approval(job_id)
            if interrupted and not acknowledge_interrupted_approval:
                raise ValueError(
                    f"L'approvazione del job {job_id} si è interrotta dopo aver già registrato parte del lavoro: "
                    "annullarlo non lo rimuove dalla memoria di traduzione né dal manuale. Completa l'approvazione "
                    "con «Approva traduzione», oppure conferma esplicitamente l'annullamento.")
            if interrupted:
                detail = {**(detail or {}), "interrupted_approval": interrupted}
        self._change_status(job_id, to_status, actor=actor, detail=detail)
        self.conn.commit()
        return self.get_job(job_id)

    def _change_status(self, job_id: str, to_status: str, *, actor: str = "", detail: Any = None) -> None:
        """transition() without the commit: for a change that must land with other writes."""
        job = self.get_job(job_id)
        current = job["status"]
        target = str(to_status or "").strip().upper()
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"Transizione non consentita: {current} → {target}")
        stamps: dict[str, str] = {"updated_at": _now()}
        if target == "SENT": stamps["sent_at"] = _now()
        if target == "RECEIVED": stamps["received_at"] = _now()
        if target == "APPROVED": stamps["approved_at"] = _now()
        set_sql = ["status=?"] + [f"{k}=?" for k in stamps]
        vals = [target] + list(stamps.values()) + [job_id, current]
        # Only from the status just read: a change made meanwhile by another session is not overwritten.
        if self.conn.execute(f"UPDATE translation_jobs SET {','.join(set_sql)} WHERE job_id=? AND status=?", vals).rowcount != 1:
            raise ValueError(f"Transizione non consentita: lo stato del job {job_id} è cambiato nel frattempo.")
        self._event(job_id, "STATUS_CHANGED", current, target, actor, detail)

    def interrupted_approval(self, job_id: str) -> dict[str, Any] | None:
        """How this job's approval was interrupted after it began writing -- while the job is
        neither APPROVED nor CANCELLED -- or None. "Approva traduzione" completes such a job.

        From the job's last APPROVAL_INTERRUPTED event and, authoritatively, from the CCMS: an audit
        row of this job means its translation is committed, also when the event could not be
        written (the Workflow database was the failing one)."""
        job = self.get_job(job_id)
        if job["status"] in ("APPROVED", "CANCELLED"):
            return None
        row = self.conn.execute(
            "SELECT detail_json,created_at FROM workflow_events WHERE job_id=? AND event_type='APPROVAL_INTERRUPTED' ORDER BY id DESC LIMIT 1",
            (str(job_id).strip(),),
        ).fetchone()
        info = None
        if row:
            detail = json.loads(row["detail_json"] or "{}")
            info = {"phase": str(detail.get("phase", "")), "translation_committed": bool(detail.get("translation_committed")),
                    "at": row["created_at"]}
        # Only a QA_READY job can hold a committed but unclosed approval; only it opens the CCMS.
        ccms_db = ProjectLayout(self.project_dir).find_ccms_db()
        if job["status"] == "QA_READY" and not (info and info["translation_committed"]) and ccms_db is not None:
            try:
                repo = IndustrialRepository(ccms_db)
                try:
                    committed = repo.translation_roundtrip_audit_for_job(job["job_id"], job["package_sha256"], job["returned_xliff_sha256"])
                finally:
                    repo.close()
            except sqlite3.Error:
                committed = None  # an unreadable CCMS: what the events say is all there is to go on
            if committed is not None:
                info = {**(info or {"phase": "", "at": committed["committed_at"]}), "translation_committed": True}
        return info

    def _approval_interrupted(self, job_id: str, phase: str, translation_committed: bool, cause: BaseException | None,
                              actor: str) -> ApprovalInterruptedError:
        """Record that an approval stopped after it began writing; return the error to raise.

        The job stays QA_READY. The event carries the error TYPE only: its text may hold a path."""
        try:
            self.conn.rollback()
            self._event(job_id, "APPROVAL_INTERRUPTED", "", "", actor,
                        {"phase": phase, "translation_committed": bool(translation_committed),
                         "error": type(cause).__name__ if cause is not None else ""})
            self.conn.commit()
        except sqlite3.Error:
            # Best effort: the failure itself is what the caller must see. A committed translation
            # is still known from its audit row (interrupted_approval).
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
        reason = (str(cause) or type(cause).__name__) if cause is not None else "errore sconosciuto"
        try:
            cancelled = self.get_job(job_id)["status"] == "CANCELLED"
        except (sqlite3.Error, KeyError):
            cancelled = False
        if translation_committed and cancelled:
            message = (f"La traduzione del job {job_id} è registrata nel manuale e nella memoria di traduzione, ma nel "
                       "frattempo il job è stato annullato: l'approvazione non è stata chiusa.")
        elif translation_committed:
            message = (f"La traduzione del job {job_id} è registrata nel manuale e nella memoria di traduzione, ma "
                       f"l'approvazione non è stata completata per un errore tecnico ({reason}). Ripeti «Approva "
                       "traduzione» per completarla: la traduzione non verrà scritta una seconda volta.")
        else:
            message = (f"L'approvazione del job {job_id} si è interrotta per un errore tecnico ({reason}). Il manuale "
                       "non è stato modificato; la memoria di traduzione confermata può contenere già alcune voci. "
                       "Nulla è andato perso: ripeti «Approva traduzione», l'operazione si può ripetere senza rischi.")
        return ApprovalInterruptedError(message, job_id=str(job_id), phase=phase, translation_committed=translation_committed)

    def mark_sent(self, job_id: str, actor: str = "") -> dict[str, Any]:
        return self.transition(job_id, "SENT", actor=actor)

    def mark_in_translation(self, job_id: str, actor: str = "") -> dict[str, Any]:
        return self.transition(job_id, "IN_TRANSLATION", actor=actor)

    def register_returned_xliff(self, job_id: str, returned_xliff: str | Path, *, actor: str = "") -> dict[str, Any]:
        job = self.get_job(job_id)
        if job["status"] not in {"SENT", "IN_TRANSLATION", "QA_BLOCKED"}:
            raise ValueError(f"Non puoi registrare un ritorno XLIFF dallo stato {job['status']}.")
        src = Path(returned_xliff)
        if not src.exists(): raise FileNotFoundError(src)
        self._job_file(job, "package_path")  # the QA that follows needs it: refuse before changing anything
        inbound = self.assets_dir / job_id / "inbound"; inbound.mkdir(parents=True, exist_ok=True)
        dst = inbound / src.name
        shutil.copy2(src, dst)
        self.conn.execute(
            "UPDATE translation_jobs SET returned_xliff_path=?,returned_xliff_sha256=?,qa_status='',qa_summary_json='',updated_at=? WHERE job_id=?",
            (self._stored(dst), _sha256(dst), _now(), job_id),
        )
        self.conn.commit()
        current = self.get_job(job_id)["status"]
        if current != "RECEIVED":
            self.transition(job_id, "RECEIVED", actor=actor, detail={"returned_xliff_sha256": _sha256(dst)})
        return self.get_job(job_id)

    def run_roundtrip_qa(self, job_id: str, ccms_db: str | Path, *, terminology_path: str | Path | None = None, strict_terminology: bool = False, actor: str = "") -> dict[str, Any]:
        job = self.get_job(job_id)
        if job["status"] != "RECEIVED":
            raise ValueError("Il QA può essere eseguito solo su un job nello stato RECEIVED.")
        if not job["returned_xliff_path"]:
            raise ValueError("XLIFF restituito non registrato.")
        package = self._job_file(job, "package_path")
        returned = self._job_file(job, "returned_xliff_path")
        repo = IndustrialRepository(ccms_db)
        out = self.assets_dir / job_id / "qa" / datetime.now().strftime("%Y%m%d_%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        term_copy = ''
        term_hash = ''
        if terminology_path:
            src_term = Path(terminology_path)
            term_dir = self.assets_dir / job_id / 'terminology'; term_dir.mkdir(parents=True, exist_ok=True)
            dst_term = term_dir / src_term.name
            shutil.copy2(src_term, dst_term)
            term_copy = str(dst_term); term_hash = _sha256(dst_term)
        try:
            result = inspect_translation_roundtrip(
                repo, package, returned, terminology_path=term_copy or None,
                strict_terminology=bool(strict_terminology), out_dir=out,
            )
        finally:
            repo.close()
        summary = result["summary"]
        qa_status = "PASS" if summary["commit_allowed_after_human_approval"] else "BLOCKED"
        next_status = "QA_READY" if qa_status == "PASS" else "QA_BLOCKED"
        self.conn.execute(
            "UPDATE translation_jobs SET qa_status=?,qa_summary_json=?,qa_terminology_path=?,qa_terminology_sha256=?,qa_strict=?,updated_at=? WHERE job_id=?",
            (qa_status, json.dumps(summary, ensure_ascii=False, sort_keys=True), self._stored(term_copy) if term_copy else '', term_hash,
             1 if strict_terminology else 0, _now(), job_id),
        )
        self.conn.commit()
        self.transition(job_id, next_status, actor=actor, detail={"qa_status": qa_status, "hard_fail_count": summary.get("hard_fail_count", 0)})
        return {"job": self.get_job(job_id), "qa": result}

    def rerun_roundtrip_qa(self, job_id: str, ccms_db: str | Path, *, returned_xliff: str | Path | None = None, terminology_path: str | Path | None = None, strict_terminology: bool = False, actor: str = "") -> dict[str, Any]:
        """Re-run QA after a blocked return, optionally replacing the returned XLIFF.

        A failed QA moves the job to QA_BLOCKED. Before the v1.0 Desktop R9 fix,
        the UI attempted to call run_roundtrip_qa() directly from QA_BLOCKED and
        ignored any corrected XLIFF uploaded by the user. That could never succeed
        because run_roundtrip_qa() is intentionally fail-closed and accepts only
        RECEIVED jobs. This method performs the required QA_BLOCKED -> RECEIVED
        reopening step and, when supplied, registers the corrected XLIFF first.
        """
        job = self.get_job(job_id)
        if returned_xliff:
            self.register_returned_xliff(job_id, returned_xliff, actor=actor)
        else:
            self._job_file(job, "package_path")          # refuse before reopening the job
            if job["returned_xliff_path"]:
                self._job_file(job, "returned_xliff_path")
            status = job["status"]
            if status == "QA_BLOCKED":
                self.transition(job_id, "RECEIVED", actor=actor, detail={"reason": "QA rerun without new XLIFF"})
            elif status != "RECEIVED":
                raise ValueError(f"Il QA può essere rieseguito solo da QA_BLOCKED/RECEIVED, stato attuale: {status}.")
        return self.run_roundtrip_qa(
            job_id, ccms_db, terminology_path=terminology_path,
            strict_terminology=bool(strict_terminology), actor=actor,
        )

    def approve_job(self, job_id: str, ccms_db: str | Path, *, reviewer: str, human_approved: bool) -> dict[str, Any]:
        """Commit the job's round trip and close the job APPROVED.

        A failure after the commit began writing leaves the job QA_READY with an
        APPROVAL_INTERRUPTED event, and raises ApprovalInterruptedError -- saying whether the CCMS
        translation is in or not. Repeating the approval completes the job either way, without
        committing the translation twice."""
        if not human_approved:
            raise ValueError("Approvazione umana esplicita obbligatoria.")
        job = self.get_job(job_id)
        if job["status"] != "QA_READY" or job["qa_status"] != "PASS":
            raise ValueError("Il job non è in QA_READY con QA PASS.")
        package = self._job_file(job, "package_path")
        returned = self._job_file(job, "returned_xliff_path")
        terminology = self._job_file(job, "qa_terminology_path") if job.get("qa_terminology_path") else None
        actor = reviewer or "Human reviewer"
        # Re-run v0.24 fail-closed during commit.
        repo = IndustrialRepository(ccms_db)
        out = self.assets_dir / job_id / "approval" / datetime.now().strftime("%Y%m%d_%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        try:
            committed = commit_translation_roundtrip(
                repo, self.project_dir, package, returned, human_approved=True,
                terminology_path=terminology, strict_terminology=bool(job.get('qa_strict')), reviewer=actor, out_dir=out,
                job_id=job_id,
            )
        except RoundtripCommitInterrupted as exc:
            raise self._approval_interrupted(job_id, exc.phase, exc.translation_committed, exc.__cause__, actor) from exc
        finally:
            repo.close()
        # The job row and its APPROVED status land together, or neither does.
        try:
            approval_zip = self._stored(committed["approval_zip"])
            detail = {"approval_zip": approval_zip}
            if committed["resumed"]:
                detail["completed_after_interruption"] = True
            self.conn.execute(
                "UPDATE translation_jobs SET reviewer=?,approval_zip=?,confirmed_tm_path=?,updated_at=? WHERE job_id=?",
                (str(committed["summary"]["reviewer"]), approval_zip, self._stored(committed["tm_path"]), _now(), job_id),
            )
            self._change_status(job_id, "APPROVED", actor=actor, detail=detail)
            self.conn.commit()
        except Exception as exc:
            raise self._approval_interrupted(job_id, "workflow", True, exc, actor) from exc
        return {"job": self.get_job(job_id), "commit": committed}

    def jobs_table(self) -> pd.DataFrame:
        rows = self.conn.execute(
            """SELECT j.job_id,j.status,j.source_kind,j.source_id,j.source_version,j.source_lang,j.target_lang,
                      COALESCE(v.name,j.vendor_id,'') AS vendor,j.due_date,j.work_words,j.technical_review_segments,
                      j.new_translation_segments,j.qa_status,j.created_at,j.sent_at,j.received_at,j.approved_at
               FROM translation_jobs j LEFT JOIN vendors v ON v.vendor_id=j.vendor_id
               ORDER BY CASE j.status WHEN 'QA_BLOCKED' THEN 0 WHEN 'RECEIVED' THEN 1 WHEN 'QA_READY' THEN 2 WHEN 'IN_TRANSLATION' THEN 3 WHEN 'SENT' THEN 4 WHEN 'READY_TO_SEND' THEN 5 ELSE 6 END,
                        j.created_at DESC"""
        ).fetchall()
        data = []
        today = date.today()
        for r in rows:
            d = dict(r)
            due = str(d.get("due_date") or "")
            overdue = False
            if due and d["status"] not in {"APPROVED", "CANCELLED"}:
                try: overdue = date.fromisoformat(due) < today
                except ValueError: overdue = False
            d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
            d["overdue"] = "SI" if overdue else ""
            data.append(d)
        cols = ["job_id","status_label","target_lang","vendor","due_date","overdue","source_kind","source_id","source_version","work_words","technical_review_segments","new_translation_segments","qa_status","created_at","sent_at","received_at","approved_at"]
        return pd.DataFrame(data, columns=cols)

    def events_table(self, job_id: str | None = None) -> pd.DataFrame:
        if job_id:
            rows = self.conn.execute("SELECT id,job_id,event_type,from_status,to_status,actor,detail_json,created_at FROM workflow_events WHERE job_id=? ORDER BY id DESC", (job_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT id,job_id,event_type,from_status,to_status,actor,detail_json,created_at FROM workflow_events ORDER BY id DESC LIMIT 200").fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def dashboard(self) -> dict[str, Any]:
        total = int(self.conn.execute("SELECT COUNT(*) FROM translation_jobs").fetchone()[0])
        counts = {r["status"]: int(r["n"]) for r in self.conn.execute("SELECT status,COUNT(*) n FROM translation_jobs GROUP BY status")}
        words = int(self.conn.execute("SELECT COALESCE(SUM(work_words),0) FROM translation_jobs WHERE status NOT IN ('APPROVED','CANCELLED')").fetchone()[0])
        overdue = 0
        for r in self.conn.execute("SELECT due_date,status FROM translation_jobs WHERE due_date<>'' AND status NOT IN ('APPROVED','CANCELLED')"):
            try:
                if date.fromisoformat(r["due_date"]) < date.today(): overdue += 1
            except ValueError:
                pass
        return {
            "total_jobs": total,
            "ready_to_send": counts.get("READY_TO_SEND", 0),
            "in_translation": counts.get("SENT", 0) + counts.get("IN_TRANSLATION", 0),
            "received_or_qa": counts.get("RECEIVED", 0) + counts.get("QA_READY", 0) + counts.get("QA_BLOCKED", 0),
            "approved": counts.get("APPROVED", 0),
            "qa_blocked": counts.get("QA_BLOCKED", 0),
            "open_work_words": words,
            "overdue": overdue,
        }

    def export_project_archive(self, out_dir: str | Path | None = None) -> Path:
        """Write the project archive and return its path.

        With no ``out_dir`` the archive goes into a fresh temporary
        directory that belongs to the CALLER from then on (see
        temp_artifacts for how the UI releases it).

        A failed export leaves nothing behind: the archive is written under
        a ``.partial`` name and renamed only once complete, so no
        successful-looking archive can exist after an error, a previous
        archive with the same name is not truncated by a failed attempt,
        and a temporary directory created by this call is removed.
        """
        created_out = not out_dir
        out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="manualsync_v25_project_archive_"))
        partial: Path | None = None
        try:
            out.mkdir(parents=True, exist_ok=True)
            archive = out / f"ManualSync_{_safe(self.project_metadata().get('title','Project'))}_PROJECT.zip"
            partial = archive.with_name(archive.name + ".partial")
            files: list[dict[str, Any]] = []
            for p in sorted(self.project_dir.rglob("*")):
                if not p.is_file():
                    continue
                # Project archives are outputs, never recursively archive another project archive.
                if p.resolve() == archive.resolve() or p.name == "MANUALSYNC_PROJECT_MANIFEST.json":
                    continue
                rel = p.relative_to(self.project_dir).as_posix()
                files.append({"path": rel, "sha256": _sha256(p), "bytes": p.stat().st_size})
            manifest = {
                "build": WORKFLOW_BUILD,
                "project": self.project_metadata(),
                "dashboard": self.dashboard(),
                "files": files,
                "exported_at": _now(),
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("MANUALSYNC_PROJECT_MANIFEST.json", manifest_bytes)
                for meta in files:
                    zf.write(self.project_dir / meta["path"], arcname=meta["path"])
            os.replace(partial, archive)
            return archive
        except BaseException:
            if partial is not None:
                partial.unlink(missing_ok=True)
            if created_out:
                shutil.rmtree(out, ignore_errors=True)
            raise

    @staticmethod
    def restore_project_archive(archive_zip: str | Path, out_dir: str | Path | None = None) -> Path:
        """Restore a project transactionally, without ever merging into old state.

        The archive is fully validated in a staging directory first. Only after
        checksums and SQLite integrity checks pass is the staging directory moved
        into its final location. This avoids partially restored/corrupted projects.
        """
        import os
        import sqlite3
        import stat

        archive_zip = Path(archive_zip)
        if not archive_zip.exists():
            raise FileNotFoundError(archive_zip)

        final_root = Path(out_dir) if out_dir else None
        if final_root is not None:
            final_root.parent.mkdir(parents=True, exist_ok=True)
            if final_root.exists() and any(final_root.iterdir()):
                raise FileExistsError(f"La destinazione di ripristino esiste già e non è vuota: {final_root}")
            stage = Path(tempfile.mkdtemp(prefix=f".{final_root.name}.restore-", dir=str(final_root.parent)))
        else:
            stage = Path(tempfile.mkdtemp(prefix="manualsync_v25_restored_project_"))

        completed = False
        try:
            with zipfile.ZipFile(archive_zip) as zf:
                infos = zf.infolist()
                names = [i.filename for i in infos]
                if "MANUALSYNC_PROJECT_MANIFEST.json" not in names:
                    raise ValueError("Archivio progetto ManualSync non valido: manifest mancante.")
                if len(infos) > 100000:
                    raise ValueError("Archivio progetto non valido: numero di file eccessivo.")
                total_uncompressed = sum(max(0, int(i.file_size)) for i in infos)
                if total_uncompressed > 20 * 1024 * 1024 * 1024:
                    raise ValueError("Archivio progetto non valido: dimensione espansa eccessiva.")
                for info in infos:
                    pp = Path(info.filename)
                    if pp.is_absolute() or ".." in pp.parts:
                        raise ValueError("Archivio progetto non sicuro: path traversal rilevato.")
                    # Reject symbolic links in ZIP archives.
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if mode and stat.S_ISLNK(mode):
                        raise ValueError("Archivio progetto non sicuro: collegamento simbolico rilevato.")

                manifest = json.loads(zf.read("MANUALSYNC_PROJECT_MANIFEST.json").decode("utf-8"))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("files", []), list):
                    raise ValueError("Archivio progetto ManualSync non valido: manifest corrotto.")
                declared = {str(m.get("path", "")) for m in manifest.get("files", [])}
                payload_names = {n for n in names if n and not n.endswith("/") and n != "MANUALSYNC_PROJECT_MANIFEST.json"}
                if declared != payload_names:
                    missing = sorted(declared - payload_names)[:5]
                    extra = sorted(payload_names - declared)[:5]
                    raise ValueError(f"Archivio progetto incoerente: file mancanti={missing}, file non dichiarati={extra}")
                zf.extractall(stage)

            for meta in manifest.get("files", []):
                rel = str(meta.get("path", ""))
                p = stage / rel
                if not p.exists() or not p.is_file():
                    raise ValueError(f"Archivio progetto incompleto: {rel}")
                if int(meta.get("bytes", -1)) != p.stat().st_size:
                    raise ValueError(f"Dimensione archivio progetto non valida: {rel}")
                if _sha256(p) != str(meta.get("sha256", "")):
                    raise ValueError(f"Checksum archivio progetto non valido: {rel}")

            ccms = ProjectLayout(stage).find_ccms_db()
            if ccms is None:
                raise ValueError("Archivio progetto senza repository CCMS.")

            for db in [ccms, stage / "ManualSync_Workflow.sqlite"]:
                if not db.exists():
                    continue
                conn = sqlite3.connect(str(db))
                try:
                    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                finally:
                    conn.close()
                if integrity.lower() != "ok":
                    raise ValueError(f"Database non integro nel backup: {db.name}: {integrity}")

            if final_root is None:
                completed = True
                return stage

            if final_root.exists():
                # Only an empty directory is allowed here; remove it before atomic move.
                final_root.rmdir()
            os.replace(stage, final_root)
            completed = True
            return final_root
        finally:
            if not completed and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def export_snapshot(self, out_dir: str | Path | None = None) -> dict[str, Path]:
        out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="manualsync_v25_workflow_export_"))
        out.mkdir(parents=True, exist_ok=True)
        jobs = self.jobs_table(); events = self.events_table(); vendors = self.vendors_table()
        jobs_path = out / "WORKFLOW_JOBS.csv"; events_path = out / "WORKFLOW_AUDIT.csv"; vendors_path = out / "WORKFLOW_VENDORS.csv"
        jobs.to_csv(jobs_path, index=False, encoding="utf-8-sig")
        events.to_csv(events_path, index=False, encoding="utf-8-sig")
        vendors.to_csv(vendors_path, index=False, encoding="utf-8-sig")
        payload = {"build": WORKFLOW_BUILD, "project": self.project_metadata(), "dashboard": self.dashboard(), "exported_at": _now()}
        summary = out / "WORKFLOW_SUMMARY.json"; summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"jobs": jobs_path, "events": events_path, "vendors": vendors_path, "summary": summary}
