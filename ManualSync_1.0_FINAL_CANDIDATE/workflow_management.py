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
from translation_roundtrip import inspect_translation_roundtrip, commit_translation_roundtrip

WORKFLOW_BUILD = "0.25.0-workflow-job-management-20260811"

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
             str(package_copy), _sha256(package_copy), str(notes or "").strip(), now, now),
        )
        self._event(job_id, "JOB_CREATED", "", "READY_TO_SEND", actor, {"package_sha256": _sha256(package_copy)})
        self.conn.commit()
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM translation_jobs WHERE job_id=?", (str(job_id).strip(),)).fetchone()
        if not row:
            raise KeyError(job_id)
        return dict(row)

    def transition(self, job_id: str, to_status: str, *, actor: str = "", detail: Any = None) -> dict[str, Any]:
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
        vals = [target] + list(stamps.values()) + [job_id]
        self.conn.execute(f"UPDATE translation_jobs SET {','.join(set_sql)} WHERE job_id=?", vals)
        self._event(job_id, "STATUS_CHANGED", current, target, actor, detail)
        self.conn.commit()
        return self.get_job(job_id)

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
        inbound = self.assets_dir / job_id / "inbound"; inbound.mkdir(parents=True, exist_ok=True)
        dst = inbound / src.name
        shutil.copy2(src, dst)
        self.conn.execute(
            "UPDATE translation_jobs SET returned_xliff_path=?,returned_xliff_sha256=?,qa_status='',qa_summary_json='',updated_at=? WHERE job_id=?",
            (str(dst), _sha256(dst), _now(), job_id),
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
                repo, job["package_path"], job["returned_xliff_path"], terminology_path=term_copy or None,
                strict_terminology=bool(strict_terminology), out_dir=out,
            )
        finally:
            repo.close()
        summary = result["summary"]
        qa_status = "PASS" if summary["commit_allowed_after_human_approval"] else "BLOCKED"
        next_status = "QA_READY" if qa_status == "PASS" else "QA_BLOCKED"
        self.conn.execute(
            "UPDATE translation_jobs SET qa_status=?,qa_summary_json=?,qa_terminology_path=?,qa_terminology_sha256=?,qa_strict=?,updated_at=? WHERE job_id=?",
            (qa_status, json.dumps(summary, ensure_ascii=False, sort_keys=True), term_copy, term_hash, 1 if strict_terminology else 0, _now(), job_id),
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
        if not human_approved:
            raise ValueError("Approvazione umana esplicita obbligatoria.")
        job = self.get_job(job_id)
        if job["status"] != "QA_READY" or job["qa_status"] != "PASS":
            raise ValueError("Il job non è in QA_READY con QA PASS.")
        # Re-run v0.24 fail-closed during commit.
        repo = IndustrialRepository(ccms_db)
        out = self.assets_dir / job_id / "approval" / datetime.now().strftime("%Y%m%d_%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        try:
            committed = commit_translation_roundtrip(
                repo, self.project_dir, job["package_path"], job["returned_xliff_path"], human_approved=True,
                terminology_path=job.get('qa_terminology_path') or None, strict_terminology=bool(job.get('qa_strict')), reviewer=reviewer or "Human reviewer", out_dir=out,
            )
        finally:
            repo.close()
        self.conn.execute(
            "UPDATE translation_jobs SET reviewer=?,approval_zip=?,confirmed_tm_path=?,updated_at=? WHERE job_id=?",
            (str(reviewer or "Human reviewer"), str(committed["approval_zip"]), str(committed["tm_path"]), _now(), job_id),
        )
        self.conn.commit()
        self.transition(job_id, "APPROVED", actor=reviewer or "Human reviewer", detail={"approval_zip": str(committed["approval_zip"])})
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

            ccms = next((stage / n for n in ["ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"] if (stage / n).exists()), None)
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
