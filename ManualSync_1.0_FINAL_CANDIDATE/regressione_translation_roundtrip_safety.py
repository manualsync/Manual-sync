"""CP5.4b-d regression: the translation round-trip commit (Approva traduzione), restore-safe and retry-safe.

The path under test: app.job_action_ui("Approva traduzione") -> WorkflowManager.approve_job
-> translation_roundtrip.commit_translation_roundtrip -> Translation Memory -> CCMS
translations -> translation_roundtrip_audit -> approved XLIFF, ROUNDTRIP_COMMIT.json, approval
ZIP -> translation_jobs UPDATE -> transition to APPROVED. Three SQLite files and the file
system: they cannot share one transaction.

CP5.4b brought the two P1 findings of CP5.4a into this suite and pinned them at HEAD 01bb10b,
as they behaved then (that version of the suite is the commit before the fix):

* P1-1  every job path was stored ABSOLUTE: after export -> restore in another folder -> the
        original removed, approving a job still in progress failed (FileNotFoundError on the
        old outbound package) and every archive -- the deliverable approval ZIP included --
        carried the project folder, i.e. C:\\Users\\<name>;
* P1-2  TM, each CCMS object, the audit row, the job row and its status were each committed on
        their own: a failure left some objects translated and others not, the job QA_READY, a
        raw technical message, and "Annulla" offered as for any job; a retry after the audit
        row committed it a second time and rewrote the CCMS rows.

CP5.4c/d CHANGED THAT ON PURPOSE, and this suite now pins the new behaviour:

* H   the happy path: QA_READY -> approve_job -> APPROVED; paths stored relative to the project,
      returned resolved; the audit row names its job; tm_path is the project-relative file name;
* R   P1-1: project A, job QA_READY -> export -> restore as B elsewhere -> A removed -> the job
      is approved in B: TM, package, XLIFF found in B, ZIP created, ROUNDTRIP_COMMIT.json and the
      archive of B free of any absolute path; an in-progress RECEIVED job passes its QA in B;
* X   no archive member carries the project folder or the Windows profile folder;
* L   legacy rows (absolute paths, as 01bb10b stored them): used in place; after a restore
      re-anchored in the new folder only when the file is there with the recorded hash --
      never read from the old folder, even if it still exists -- and NEVER rewritten; otherwise
      a fail-closed JobFileMissingError naming the file, nothing written;
* F   P1-2, the failure matrix: the 2nd TM entry, the 1st and 2nd CCMS write, the audit INSERT,
      the approval ZIP, the translation_jobs UPDATE, the APPROVED transition -- then a retry.
      CCMS translations + audit are ONE transaction (no partial CCMS any more); the job row and
      its status are ONE transaction; an APPROVAL_INTERRUPTED event records phase and whether
      the translation is committed; the retry completes the job with exactly one audit row and
      does not rewrite a CCMS translation committed by the interrupted attempt;
* S   a source edited after a committed interruption: completing stays fail-closed (QA), the
      message says the translation is already in the CCMS, the job can be cancelled knowingly;
* M   the interruption marker itself lost (the Workflow database fails twice): the committed
      translation is still known from its audit row, the cancellation still needs the
      acknowledgment, the retry still completes the job once;
* P   a deterministic fail-closed refusal (a missing target) is raised before any write, as
      itself -- not reported as an interruption;
* W   a status change is applied only from the status read: a concurrent one is not overwritten,
      and a job cancelled while its approval was committing is said to be so;
* Q   the terminology file of the QA: stored relative, restored elsewhere, legacy re-anchoring
      checked by hash; and receiving / re-running the QA of a job whose package is not in the
      project is refused BEFORE anything changes;
* N   the transaction primitives: single_transaction (BEGIN IMMEDIATE, refuses an open
      transaction), the lazy audit job_id column, the interruption exception, and what is
      deliberately left as it was (a direct commit without a job still appends an audit row --
      P2, deferred);
* U   what the user meets: a sentence saying what was written and that the approval can be
      repeated; "Annulla" on such a job asks a question that says what stays in TM/CCMS, the
      engine refuses the cancellation without that acknowledgment, and an answer to a question
      that no longer matches the job is not taken as one; the retry from the UI.

Failures are injected ONLY by monkeypatching or by a proxy on the sqlite3 connection, all
restored after use. Every project is a synthetic fixture under one temporary folder, removed at
the end (T1) -- also when the suite stops on an exception. Nothing is written outside it.

The work packages are generated OUTSIDE the project folder here: what package generation itself
writes into a project (TranslationPackages/.../TRANSLATION_IMPACT_SUMMARY.json records the TM
files it used, absolute -- translation_impact) is outside this change and outside X/R10.

Usage:  ..\\.venv\\Scripts\\python.exe regressione_translation_roundtrip_safety.py
"""
from __future__ import annotations

import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import atexit
import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import network_guard  # noqa: E402

GUARD = network_guard.install()

import gradio as gr  # noqa: E402

import app  # noqa: E402
import cat_tm  # noqa: E402
import i18n  # noqa: E402
import translation_roundtrip as trt  # noqa: E402
import translation_view as tv  # noqa: E402
import workflow_management as wfm  # noqa: E402
from ccms_industrial import IndustrialRepository  # noqa: E402
from translation_impact import publication_revision_token  # noqa: E402
from translation_work_package import generate_translation_work_packages  # noqa: E402
from workflow_management import JOB_PATH_COLUMNS, JobFileMissingError, WorkflowManager  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="erylos_cp54b_roundtrip_"))
atexit.register(shutil.rmtree, TMP, True)              # also when a regression makes the suite stop
# The real Windows profile (C:\Users\<name>): test_isolation moved HOME, but recorded the
# protected data folder, which lives in it. Its presence in an archive = the user name leaks.
PROFILE = Path(test_isolation.PROTECTED_DATA_HOME).parent
TM_NAME = "ManualSync_CONFIRMED_TM.sqlite"
_results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), str(detail)[:900]))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   [{str(detail)[:900]}]"))


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def attempt(fn):
    """('ok', value) or ('raise', exception) -- the observed behaviour IS the exception."""
    try:
        return "ok", fn()
    except Exception as exc:  # noqa: BLE001
        return "raise", exc


def code_of(out) -> str:
    """The type of the raised exception, or 'ok'."""
    return "ok" if out[0] == "ok" else type(out[1]).__name__


# ============================================================ fixture
TRANSLATIONS = {
    "Electrical ratings": "Valori nominali elettrici",
    "Install the drive vertically.": "Installare il convertitore verticalmente.",
    "Keep a clearance of 20 mm.": "Mantenere una distanza di 20 mm.",
}


def translate(langzip: Path, out: Path) -> Path:
    with zipfile.ZipFile(langzip) as z:
        name = next(n for n in z.namelist() if n.endswith("_WORK_ONLY.xlf"))
        out.write_bytes(z.read(name))
    ns = {"x": trt.XLIFF_NS}
    tree = ET.parse(out)
    for unit in tree.getroot().findall(".//x:unit", ns):
        seg = unit.find("./x:segment", ns)
        src = "".join(seg.find("./x:source", ns).itertext())
        tgt = seg.find("./x:target", ns)
        tgt.clear()
        tgt.text = TRANSLATIONS[src]
        seg.set("state", "translated")
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return out


def build(name: str, until: str = "QA_READY", terminology: bool = False) -> dict:
    """A job on a 1-topic / 2-block publication (three CCMS writes: title, B1, B2), driven to
    IN_TRANSLATION, RECEIVED or QA_READY (QA with a terminology file if asked)."""
    root = TMP / name
    project = root / "project"
    project.mkdir(parents=True)
    db = project / "ManualSync_CCMS.sqlite"
    repo = IndustrialRepository(db)
    try:
        repo.create_topic("T1", "Electrical ratings", "", source_lang="en-US", status="Approved", source_refs=["Spec#1"])
        repo.add_block("B1", "T1", "paragraph", position=1, text="Install the drive vertically.", status="Approved", source_refs=["Spec#1"])
        repo.add_block("B2", "T1", "paragraph", position=2, text="Keep a clearance of 20 mm.", status="Approved", source_refs=["Spec#2"])
        repo.create_publication("PUB1", "Demo Drive Manual", ["T1"], source_lang="en-US")
        wp = generate_translation_work_packages(repo, "PUB1", kind="publication", target_langs=["it-IT"], out_dir=root / "workpkg")
        token = publication_revision_token(repo, "PUB1")
    finally:
        repo.close()
    langzip = Path(wp["language_zips"][0])
    xlf = translate(langzip, root / "returned_it.xlf")
    wf = WorkflowManager(project)
    job = wf.create_job_from_package(langzip, actor="CP5.4b")
    wf.mark_sent(job, actor="CP5.4b")
    wf.mark_in_translation(job, actor="CP5.4b")
    if until != "IN_TRANSLATION":
        wf.register_returned_xliff(job, xlf, actor="CP5.4b")
    term = None
    if terminology:
        term = root / "terminologia.csv"
        term.write_text("source,target\nclearance,distanza\n", encoding="utf-8")
    if until == "QA_READY":
        q = wf.run_roundtrip_qa(job, db, terminology_path=term, strict_terminology=bool(term), actor="CP5.4b")
        assert q["job"]["status"] == "QA_READY", q["qa"]["summary"]
    return {"root": root, "project": project, "db": db, "langzip": langzip, "xlf": xlf, "wf": wf, "job": job, "token": token}


def approve(fx, reviewer="Revisore CP5.4b"):
    return fx["wf"].approve_job(fx["job"], fx["db"], reviewer=reviewer, human_approved=True)


# ============================================================ read-only observation of a project
def ro(path: Path, sql: str, params=()):
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def has_table(path: Path, table: str) -> bool:
    return bool(ro(path, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def raw_job(project: Path, job: str) -> dict:
    """The job row as STORED (get_job resolves the paths)."""
    return ro(project / "ManualSync_Workflow.sqlite", "SELECT * FROM translation_jobs WHERE job_id=?", (job,))[0]


def events(project: Path, job: str, event_type: str) -> list[dict]:
    return [json.loads(r["detail_json"]) for r in ro(
        project / "ManualSync_Workflow.sqlite",
        "SELECT detail_json FROM workflow_events WHERE job_id=? AND event_type=? ORDER BY id", (job, event_type))]


ARTIFACTS = (("xlf", "_APPROVED_RETURN.xlf"), ("json", "ROUNDTRIP_COMMIT.json"), ("zip", "_ROUNDTRIP_APPROVAL.zip"))


def approval_dirs(project: Path, job: str) -> list[Path]:
    appr = project / "ManualSync_Workflow_Assets" / job / "approval"
    return sorted(appr.iterdir()) if appr.exists() else []


def state(project: Path, job: str, db: Path | None = None) -> dict:
    db = db or project / "ManualSync_CCMS.sqlite"
    tm = project / TM_NAME
    wfdb = project / "ManualSync_Workflow.sqlite"
    dirs = []
    for d in approval_dirs(project, job):
        names = [p.name for p in d.iterdir() if p.is_file()]
        dirs.append(sorted(k for k, suffix in ARTIFACTS if any(n.endswith(suffix) for n in names)))
    row = raw_job(project, job)
    qa_ready = ro(wfdb, "SELECT MAX(id) AS id FROM workflow_events WHERE job_id=? AND to_status='QA_READY'", (job,))[0]["id"]
    audit = ro(db, "SELECT * FROM translation_roundtrip_audit ORDER BY id") if has_table(db, "translation_roundtrip_audit") else None
    return {
        "TM": len(ro(tm, "SELECT id FROM tm_entries")) if tm.exists() else 0,
        "CCMS": [len(ro(db, "SELECT topic_id FROM topic_translations")), len(ro(db, "SELECT block_id FROM block_translations"))],
        "CCMS_STATUS": sorted({r["status"] for r in ro(db, "SELECT status FROM topic_translations UNION ALL SELECT status FROM block_translations")}),
        "CCMS_UPDATED": [r["u"] for r in ro(db, "SELECT updated_at AS u FROM topic_translations UNION ALL SELECT updated_at FROM block_translations")],
        "AUDIT": None if audit is None else len(audit),
        "AUDIT_ROWS": audit or [],
        "AUDIT_TM_ADDED": None if audit is None else [r["tm_entries_added"] for r in audit],
        "FILES": dirs,
        "ZIP": any("zip" in d for d in dirs),
        "WORKFLOW": [bool(row["reviewer"]), bool(row["approval_zip"])],
        "JOB": row["status"],
        "EVENTS": [[e["event_type"], e["to_status"]] for e in ro(
            wfdb, "SELECT event_type,to_status FROM workflow_events WHERE job_id=? AND id>? ORDER BY id", (job, qa_ready))],
    }


VISIBLE = ("TM", "CCMS", "AUDIT", "FILES", "ZIP", "WORKFLOW", "JOB")


def brief(st: dict) -> dict:
    return {k: st[k] for k in VISIBLE}


NOTHING_WRITTEN = {"TM": 0, "CCMS": [0, 0], "AUDIT": None, "FILES": [[]], "ZIP": False,
                   "WORKFLOW": [False, False], "JOB": "QA_READY"}
FULL = ["json", "xlf", "zip"]
APPROVED_ONCE = {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}


# ============================================================ injection (monkeypatch / connection proxy only)
class ConnProxy:
    """Delegates to a real sqlite3 connection; each predicate makes ONE matching execute raise, and
    ``fail_commit`` the next commit."""

    def __init__(self, real, *fail_execute, fail_commit=False):
        self._real, self._fails, self._fail_commit = real, list(fail_execute), fail_commit

    def commit(self):
        if self._fail_commit:
            self._fail_commit = False
            raise sqlite3.OperationalError("CP5.4b injected: database is locked")
        return self._real.commit()

    def execute(self, sql, *a, **kw):
        for fail in self._fails:
            if fail(sql, a):
                self._fails.remove(fail)
                raise sqlite3.OperationalError("CP5.4b injected: database is locked")
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Patch:
    def __init__(self):
        self.undo = []

    def set(self, obj, attr, value):
        self.undo.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def restore(self):
        for obj, attr, old in reversed(self.undo):
            setattr(obj, attr, old)
        self.undo.clear()


CCMS_SETTERS = ("set_topic_title_translation", "set_block_translation", "set_translation",
                "set_table_translation", "set_figure_translation", "set_component_translation")


def inject(case: str, fx: dict, P: Patch) -> None:
    if case == "A_tm":                                    # the 2nd TM entry fails; the 1st is already committed
        real_add = cat_tm.TranslationMemory.add
        n = {"c": 0}

        def add(self, *a, **kw):
            n["c"] += 1
            if n["c"] == 2:
                raise sqlite3.OperationalError("CP5.4b injected: TM write failed")
            return real_add(self, *a, **kw)
        P.set(cat_tm.TranslationMemory, "add", add)
    elif case in ("B_ccms1", "C_ccms2"):                  # the 1st / 2nd top-level CCMS write fails before writing
        k = 1 if case == "B_ccms1" else 2
        n = {"c": 0}
        for meth in CCMS_SETTERS:
            real = getattr(IndustrialRepository, meth)

            def wrapped(self, oid, *a, _real=real, **kw):
                top = getattr(self, "_cp54b_depth", 0) == 0          # set_topic_title -> set_translation is ONE write
                if top:
                    n["c"] += 1
                    if n["c"] == k:
                        raise sqlite3.OperationalError(f"CP5.4b injected: CCMS write #{k} failed")
                self._cp54b_depth = getattr(self, "_cp54b_depth", 0) + 1
                try:
                    return _real(self, oid, *a, **kw)
                finally:
                    self._cp54b_depth -= 1
            P.set(IndustrialRepository, meth, wrapped)
    elif case == "D_audit":                               # the audit INSERT fails (after every CCMS write)
        real_apply = trt._apply_ccms_translations

        def apply(repo, *a, **kw):
            out = real_apply(repo, *a, **kw)
            repo.conn = ConnProxy(repo.conn, lambda sql, a: "INSERT INTO translation_roundtrip_audit" in sql)
            return out
        P.set(trt, "_apply_ccms_translations", apply)
    elif case == "E_files":                               # the approval ZIP (the deliverable) cannot be written
        real_zip = zipfile.ZipFile

        class FailingZip(real_zip):
            def __init__(self, file, mode="r", *a, **kw):
                if mode == "w" and str(file).endswith("_ROUNDTRIP_APPROVAL.zip"):
                    raise OSError("CP5.4b injected: approval ZIP write failed")
                super().__init__(file, mode, *a, **kw)
        P.set(zipfile, "ZipFile", FailingZip)
    elif case == "F_workflow":                            # UPDATE translation_jobs SET reviewer/approval_zip fails
        fx["wf"].conn = ConnProxy(fx["wf"].conn, lambda sql, a: sql.startswith("UPDATE translation_jobs SET reviewer="))
    elif case == "G_transition":                          # the APPROVED transition fails
        fx["wf"].conn = ConnProxy(fx["wf"].conn, lambda sql, a: sql.startswith("UPDATE translation_jobs SET status=")
                                  and bool(a) and bool(a[0]) and a[0][0] == "APPROVED")
    else:
        raise ValueError(case)


def unwrap(fx):
    if isinstance(fx["wf"].conn, ConnProxy):
        fx["wf"].conn = fx["wf"].conn._real


# ============================================================ archive scan / legacy rows
def _forms(path: Path) -> list[bytes]:
    s = str(path)
    return [f.lower().encode("utf-8") for f in {s, s.replace("\\", "/"), s.replace("\\", "\\\\")}]


def leaks(archive: Path, paths: list[Path]) -> list[str]:
    """Archive members (nested ZIPs included) whose bytes carry one of `paths`."""
    forms = [f for p in paths for f in _forms(p)]
    found: list[str] = []

    def scan(z: zipfile.ZipFile, prefix: str):
        for n in z.namelist():
            data = z.read(n)
            if n.lower().endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                    scan(inner, f"{prefix}{n}!")
            if any(f in data.lower() for f in forms):
                found.append(prefix + n)
    with zipfile.ZipFile(archive) as z:
        scan(z, "")
    return sorted(found)


def make_legacy(project: Path, job: str) -> dict:
    """Rewrite the job row as 01bb10b stored it -- every path absolute, into this project folder --
    and return the values written."""
    cols = list(JOB_PATH_COLUMNS)
    con = sqlite3.connect(project / "ManualSync_Workflow.sqlite")
    try:
        row = con.execute(f"SELECT {','.join(cols)} FROM translation_jobs WHERE job_id=?", (job,)).fetchone()
        vals = {c: (str(project / v) if v else "") for c, v in zip(cols, row)}
        con.execute(f"UPDATE translation_jobs SET {','.join(c + '=?' for c in cols)} WHERE job_id=?", (*vals.values(), job))
        con.commit()
    finally:
        con.close()
    return vals


def export(project: Path, out_dir: Path) -> Path:
    wf = WorkflowManager(project)
    try:
        return wf.export_project_archive(out_dir=out_dir)
    finally:
        wf.close()


def commit_json_of(project: Path, job: str, index: int = -1) -> dict:
    """ROUNDTRIP_COMMIT.json of an approval folder, {} when there is none (a regression must
    produce FAIL lines, not stop the suite)."""
    dirs = approval_dirs(project, job)
    try:
        return json.loads((dirs[index] / "ROUNDTRIP_COMMIT.json").read_text(encoding="utf-8"))
    except (IndexError, OSError, ValueError):
        return {}


def relative_posix(value: str) -> bool:
    return bool(value) and not Path(value).is_absolute() and "\\" not in value and ".." not in Path(value).parts


# ============================================================ H -- happy path
print("== H  happy path")
H = build("happy")
st_before = state(H["project"], H["job"])
res = attempt(lambda: approve(H))
st = state(H["project"], H["job"])
job = H["wf"].get_job(H["job"])
audit = st["AUDIT_ROWS"]
appr_dir = (approval_dirs(H["project"], H["job"]) or [H["root"]])[0]
commit_json = commit_json_of(H["project"], H["job"], 0)
approved_xlf = next(appr_dir.glob("*_APPROVED_RETURN.xlf"), appr_dir / "missing.xlf")
check("H1: approve_job on a QA_READY job succeeds", res[0] == "ok", res)
check("H2: before the approval nothing is written (TM, CCMS, audit)",
      (st_before["TM"], st_before["CCMS"], st_before["AUDIT"]) == (0, [0, 0], None), brief(st_before))
check("H3: TM 3 entries, CCMS title + 2 blocks Reviewed, one audit row, artifacts, job APPROVED",
      brief(st) == APPROVED_ONCE and st["CCMS_STATUS"] == ["Reviewed"], st)
a = audit[0]
check("H4: audit row: publication PUB1, version = publication_revision_token = job.source_version",
      (a["source_kind"], a["source_id"]) == ("publication", "PUB1") and a["source_version"] == H["token"] == job["source_version"], a)
check("H5: audit package/XLIFF hashes = the job's = the files' (outbound copy, original package, approved copy)",
      a["package_sha256"] == job["package_sha256"] == sha(job["package_path"]) == sha(H["langzip"])
      and a["returned_xliff_sha256"] == job["returned_xliff_sha256"] == sha(approved_xlf), (a, job))
check("H6: audit human_approved=1, reviewer, tm_entries_added = 3",
      (a["human_approved"], a["reviewer"], a["tm_entries_added"]) == (1, "Revisore CP5.4b", 3), a)
with zipfile.ZipFile(job["approval_zip"]) as z:
    zip_names = sorted(z.namelist())
check("H7: approval ZIP = approved XLIFF, ROUNDTRIP_COMMIT.json, the two QA files",
      zip_names == sorted([approved_xlf.name, "ROUNDTRIP_COMMIT.json", "ROUNDTRIP_QA_SEGMENTS.csv", "ROUNDTRIP_QA_SUMMARY.json"]), zip_names)
check("H8: job events after QA_READY: one STATUS_CHANGED -> APPROVED", st["EVENTS"] == [["STATUS_CHANGED", "APPROVED"]], st["EVENTS"])
check("H9: ROUNDTRIP_COMMIT.json approved_by_human, reviewer, tm_entries_added",
      (commit_json["approved_by_human"], commit_json["reviewer"], commit_json["tm_entries_added"]) == (True, "Revisore CP5.4b", 3), commit_json)
raw = raw_job(H["project"], H["job"])
stored = {c: raw[c] for c in ("package_path", "returned_xliff_path", "approval_zip", "confirmed_tm_path")}
check("H10: CP5.4c: the job row stores its paths RELATIVE to the project folder, '/'-separated",
      all(relative_posix(v) for v in stored.values()) and stored["confirmed_tm_path"] == TM_NAME
      and stored["package_path"].startswith(f"ManualSync_Workflow_Assets/{H['job']}/outbound/"), stored)
check("H11: get_job returns them resolved: absolute, inside the project, existing",
      all(Path(job[c]).is_absolute() and Path(job[c]).is_relative_to(H["project"].resolve()) and Path(job[c]).is_file()
          for c in stored), {c: job[c] for c in stored})
approved_event = events(H["project"], H["job"], "STATUS_CHANGED")[-1]
check("H12: the APPROVED event records the approval ZIP relative to the project",
      approved_event == {"approval_zip": stored["approval_zip"]}, approved_event)
check("H13: ROUNDTRIP_COMMIT.json and the audit row record tm_path as the project-relative file name; "
      "one timestamp for both", commit_json["tm_path"] == a["tm_path"] == TM_NAME and commit_json["committed_at"] == a["committed_at"],
      (commit_json["tm_path"], a["tm_path"], commit_json["committed_at"], a["committed_at"]))
check("H14: the audit row names its job; the commit was not a resumption",
      a["job_id"] == H["job"] and res[1]["commit"]["resumed"] is False and "completed_after_interruption" not in commit_json, a)
check("H15: commit_translation_roundtrip still RETURNS absolute paths (TM, ZIP) for the UI download",
      Path(res[1]["commit"]["tm_path"]) == H["project"] / TM_NAME and Path(res[1]["commit"]["approval_zip"]).is_file(),
      res[1]["commit"]["tm_path"])

# ============================================================ R -- P1-1: restore an in-flight job elsewhere, approve it
print("== R  P1-1 restore of an in-flight job")
R = build("restore")
RR = build("restore_received", until="RECEIVED")                   # an in-progress job one step earlier
A = R["project"]
jobA = R["wf"].get_job(R["job"])
check("R1: in project A the job is QA_READY with QA PASS", (jobA["status"], jobA["qa_status"]) == ("QA_READY", "PASS"), jobA["status"])
rawA = raw_job(A, R["job"])
check("R2: the job stores project-relative paths; in A they resolve to existing files",
      relative_posix(rawA["package_path"]) and relative_posix(rawA["returned_xliff_path"])
      and Path(jobA["package_path"]).is_file() and Path(jobA["returned_xliff_path"]).is_file(),
      {k: rawA[k] for k in ("package_path", "returned_xliff_path")})
archive = R["wf"].export_project_archive(out_dir=R["root"] / "export")
R["wf"].close()
check("R3: the project archive is written", archive.is_file(), archive)
B = R["root"] / "elsewhere" / "project_B"
restored = WorkflowManager.restore_project_archive(archive, B)
check("R4: restored as project B in a different folder", restored == B and (B / "ManualSync_Workflow.sqlite").is_file(), restored)
shutil.rmtree(A)
check("R5: project A removed", not A.exists(), A)
wfB = WorkflowManager(B)
try:
    jobB = wfB.get_job(R["job"])
    out = attempt(lambda: wfB.approve_job(R["job"], B / "ManualSync_CCMS.sqlite", reviewer="Revisore B", human_approved=True))
    stB = state(B, R["job"])
    final_B = wfB.get_job(R["job"])
    archive_B = wfB.export_project_archive(out_dir=R["root"] / "export_B")
finally:
    wfB.close()
check("R6: in B get_job resolves the package and the XLIFF inside B",
      all(Path(jobB[k]).is_file() and Path(jobB[k]).is_relative_to(B.resolve()) for k in ("package_path", "returned_xliff_path")),
      {k: jobB[k] for k in ("package_path", "returned_xliff_path")})
check("R7: CP5.4c: the job is APPROVED in B", out[0] == "ok" and out[1]["job"]["status"] == "APPROVED", out)
check("R8: in B: TM found (3 entries in B's confirmed TM), package and XLIFF found (audit hashes = the job's), CCMS "
      "translated, approval ZIP created",
      brief(stB) == APPROVED_ONCE and stB["AUDIT_ROWS"][0]["package_sha256"] == final_B["package_sha256"]
      and stB["AUDIT_ROWS"][0]["returned_xliff_sha256"] == final_B["returned_xliff_sha256"]
      and Path(final_B["approval_zip"]).is_relative_to(B.resolve()) and Path(final_B["confirmed_tm_path"]) == (B / TM_NAME).resolve(),
      stB)
b_dir = approval_dirs(B, R["job"])[0]
b_json = (b_dir / "ROUNDTRIP_COMMIT.json").read_bytes()
with zipfile.ZipFile(next(b_dir.glob("*_ROUNDTRIP_APPROVAL.zip"))) as z:
    b_json_in_zip = z.read("ROUNDTRIP_COMMIT.json")
forms = [f for p in (A, B, PROFILE) for f in _forms(p)]
check("R9: ROUNDTRIP_COMMIT.json -- on disk and in the deliverable ZIP -- carries neither A's nor B's folder nor the "
      "Windows profile; tm_path is the file name",
      not any(f in b_json.lower() or f in b_json_in_zip.lower() for f in forms)
      and json.loads(b_json)["tm_path"] == TM_NAME, json.loads(b_json)["tm_path"])
leak_B = leaks(archive_B, [A, B, PROFILE])
check("R10: the archive of B (after the approval) exposes no absolute path: no folder of A or B, no Windows user name",
      leak_B == [], leak_B)
# the same restore for a job one step earlier: its QA runs in B
arch_rr = RR["wf"].export_project_archive(out_dir=RR["root"] / "export")
RR["wf"].close()
B2 = RR["root"] / "elsewhere" / "project_B"
WorkflowManager.restore_project_archive(arch_rr, B2)
shutil.rmtree(RR["project"])
wfB2 = WorkflowManager(B2)
try:
    qa = attempt(lambda: wfB2.run_roundtrip_qa(RR["job"], B2 / "ManualSync_CCMS.sqlite", actor="CP5.4b"))
    ok2 = attempt(lambda: wfB2.approve_job(RR["job"], B2 / "ManualSync_CCMS.sqlite", reviewer="Revisore B", human_approved=True))
finally:
    wfB2.close()
check("R11: a RECEIVED job restored elsewhere (original removed) passes its QA in B and is approved there",
      qa[0] == "ok" and qa[1]["job"]["status"] == "QA_READY" and ok2[0] == "ok" and ok2[1]["job"]["status"] == "APPROVED",
      (qa, ok2))

# ============================================================ X -- absolute paths in archives
print("== X  absolute paths in archives")
x_qa = leaks(archive, [A, PROFILE])
check("X1: CP5.4c: the archive of a QA_READY project (job files, Workflow DB, CCMS) carries neither its folder nor the "
      "Windows profile", x_qa == [], x_qa)
archive_h = H["wf"].export_project_archive(out_dir=H["root"] / "export")
x_appr = leaks(archive_h, [H["project"], PROFILE])
check("X2: CP5.4c: nor after an approval -- CCMS (audit), Workflow DB, ROUNDTRIP_COMMIT.json, approval ZIP", x_appr == [], x_appr)
H["wf"].close()

# ============================================================ L -- legacy rows: absolute paths as 01bb10b stored them
print("== L  legacy absolute paths")
L1 = build("legacy_in_place")
L1["wf"].close()
legacy_vals = make_legacy(L1["project"], L1["job"])
L1["wf"] = WorkflowManager(L1["project"])
l1 = attempt(lambda: approve(L1))
raw_l1 = raw_job(L1["project"], L1["job"])
check("L1: a legacy job (absolute paths into its own folder) is approved in place",
      l1[0] == "ok" and brief(state(L1["project"], L1["job"])) == APPROVED_ONCE, l1)
check("L2: NO silent conversion: the legacy package/XLIFF values stay as stored; only the new values are relative",
      raw_l1["package_path"] == legacy_vals["package_path"] and raw_l1["returned_xliff_path"] == legacy_vals["returned_xliff_path"]
      and relative_posix(raw_l1["approval_zip"]) and raw_l1["confirmed_tm_path"] == TM_NAME, raw_l1)
# an APPROVED legacy job: approval_zip / confirmed_tm_path absolute as well, then restored elsewhere
L1["wf"].close()
make_legacy(L1["project"], L1["job"])
arch_l1 = export(L1["project"], L1["root"] / "export")
L1B = L1["root"] / "elsewhere" / "project"
WorkflowManager.restore_project_archive(arch_l1, L1B)
shutil.rmtree(L1["project"])
wf = WorkflowManager(L1B)
try:
    j = wf.get_job(L1["job"])
finally:
    wf.close()
check("L3: a restored legacy APPROVED job: get_job re-anchors package, XLIFF, approval ZIP and TM in the new folder",
      all(Path(j[c]).is_file() and Path(j[c]).is_relative_to(L1B.resolve())
          for c in ("package_path", "returned_xliff_path", "approval_zip", "confirmed_tm_path")),
      {c: j[c] for c in JOB_PATH_COLUMNS})

LA = build("legacy_restore")
LA["wf"].close()
legacy_la = make_legacy(LA["project"], LA["job"])
arch_la = export(LA["project"], LA["root"] / "export")


def restore_la(name: str) -> Path:
    target = LA["root"] / name / "project"
    WorkflowManager.restore_project_archive(arch_la, target)
    return target


def approve_in(project: Path):
    wf = WorkflowManager(project)
    try:
        return attempt(lambda: wf.approve_job(LA["job"], project / "ManualSync_CCMS.sqlite", reviewer="Revisore L", human_approved=True))
    finally:
        wf.close()


outbound_rel = Path(legacy_la["package_path"]).relative_to(LA["project"])
# the original still exists, the copy in the restored project is gone: never read A's copy
L5 = restore_la("copy_missing_original_present")
(L5 / outbound_rel).unlink()
l5 = approve_in(L5)
check("L4: the restored copy of the package is missing while the ORIGINAL still exists: refused "
      "(JobFileMissingError, a FileNotFoundError) -- the old folder is never read; nothing written",
      code_of(l5) == "JobFileMissingError" and isinstance(l5[1], FileNotFoundError)
      and brief(state(L5, LA["job"])) == {**NOTHING_WRITTEN, "FILES": []}, (l5, brief(state(L5, LA["job"]))))
shutil.rmtree(LA["project"])
L2 = restore_la("restored")
l2 = approve_in(L2)
raw_l2 = raw_job(L2, LA["job"])
check("L5: legacy job, original removed: re-anchored in the restored folder (hash verified) and APPROVED there",
      l2[0] == "ok" and brief(state(L2, LA["job"])) == APPROVED_ONCE, l2)
check("L6: ... and its legacy rows are still the old absolute values (read, never rewritten)",
      raw_l2["package_path"] == legacy_la["package_path"] and raw_l2["returned_xliff_path"] == legacy_la["returned_xliff_path"],
      raw_l2)
L3 = restore_la("package_removed")
(L3 / outbound_rel).unlink()
l3 = approve_in(L3)
l3_it = app.ui_error_text(l3[1]) if l3[0] == "raise" else ""
check("L7: legacy job, package missing in the restored folder: JobFileMissingError, nothing written",
      code_of(l3) == "JobFileMissingError" and brief(state(L3, LA["job"])) == {**NOTHING_WRITTEN, "FILES": []}, l3)
check("L8: ... the UI shows a sentence that names the file and the job, and no absolute path",
      outbound_rel.name in l3_it and LA["job"] in l3_it and "non si trova in questo progetto" in l3_it
      and ":\\" not in l3_it and str(LA["root"]) not in l3_it, l3_it)
L4 = restore_la("package_altered")
(L4 / outbound_rel).write_bytes(b"not the package the job recorded")
l4 = approve_in(L4)
check("L9: legacy job, package in the restored folder with ANOTHER content (hash differs): refused the same way",
      code_of(l4) == "JobFileMissingError" and brief(state(L4, LA["job"])) == {**NOTHING_WRITTEN, "FILES": []}, l4)

# ============================================================ F -- P1-2: failure matrix, then a human retry
print("== F  P1-2 failure matrix + retry")
# FILES: one entry per approval/<second> folder, the artifacts it holds (the QA files are always
# there). WORKFLOW: [reviewer set, approval_zip set]. Was at 01bb10b (CP5.4b): C left the title
# translated and the blocks not; D left all three without an audit row; G left reviewer and
# approval_zip set on a QA_READY job; E/F/G ended with TWO audit rows and the CCMS rewritten.
EXPECTED = {
    "A_tm": ({"TM": 1, "CCMS": [0, 0], "AUDIT": None, "FILES": [[]], "ZIP": False, "WORKFLOW": [False, False], "JOB": "QA_READY"},
             {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [[], FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}),
    "B_ccms1": ({"TM": 3, "CCMS": [0, 0], "AUDIT": None, "FILES": [[]], "ZIP": False, "WORKFLOW": [False, False], "JOB": "QA_READY"},
                {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [[], FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}),
    "C_ccms2": ({"TM": 3, "CCMS": [0, 0], "AUDIT": None, "FILES": [[]], "ZIP": False, "WORKFLOW": [False, False], "JOB": "QA_READY"},
                {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [[], FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}),
    "D_audit": ({"TM": 3, "CCMS": [0, 0], "AUDIT": None, "FILES": [[]], "ZIP": False, "WORKFLOW": [False, False], "JOB": "QA_READY"},
                {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [[], FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}),
    "E_files": ({"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [["json", "xlf"]], "ZIP": False, "WORKFLOW": [False, False], "JOB": "QA_READY"},
                {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [["json", "xlf"], FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}),
    "F_workflow": ({"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [FULL], "ZIP": True, "WORKFLOW": [False, False], "JOB": "QA_READY"},
                   {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [FULL, FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}),
    "G_transition": ({"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [FULL], "ZIP": True, "WORKFLOW": [False, False], "JOB": "QA_READY"},
                     {"TM": 3, "CCMS": [1, 2], "AUDIT": 1, "FILES": [FULL, FULL], "ZIP": True, "WORKFLOW": [True, True], "JOB": "APPROVED"}),
}
# What approve_job raises, the interrupted phase and whether the translation was
# committed, the audit tm_entries_added after the retry (the TM entries the committing attempt
# added), and whether the retry rewrote a CCMS row that already existed.
EXPECTED_DETAIL = {
    "A_tm": ("ApprovalInterruptedError", "tm", False, [2], False),
    "B_ccms1": ("ApprovalInterruptedError", "ccms", False, [0], False),
    "C_ccms2": ("ApprovalInterruptedError", "ccms", False, [0], False),
    "D_audit": ("ApprovalInterruptedError", "ccms", False, [0], False),
    "E_files": ("ApprovalInterruptedError", "artifacts", True, [3], False),
    "F_workflow": ("ApprovalInterruptedError", "workflow", True, [3], False),
    "G_transition": ("ApprovalInterruptedError", "workflow", True, [3], False),
}
CAUSES = {"A_tm": "OperationalError", "B_ccms1": "OperationalError", "C_ccms2": "OperationalError", "D_audit": "OperationalError",
          "E_files": "OSError", "F_workflow": "OperationalError", "G_transition": "OperationalError"}
MATRIX = {}
for case in EXPECTED:
    fx = build(f"case_{case}")
    P = Patch()
    inject(case, fx, P)
    try:
        failed = attempt(lambda: approve(fx))
    finally:
        P.restore()
        unwrap(fx)
    st_fail = state(fx["project"], fx["job"])
    interrupted = fx["wf"].interrupted_approval(fx["job"])
    time.sleep(1.1)                                       # a human retry: a new approval/<second> folder
    retry = attempt(lambda: approve(fx))
    st_retry = state(fx["project"], fx["job"])
    still_interrupted = fx["wf"].interrupted_approval(fx["job"])
    fx["wf"].close()
    rewritten = st_fail["CCMS_UPDATED"] != st_retry["CCMS_UPDATED"][: len(st_fail["CCMS_UPDATED"])] if st_fail["CCMS_UPDATED"] else False
    recorded = events(fx["project"], fx["job"], "APPROVAL_INTERRUPTED")
    last_json = commit_json_of(fx["project"], fx["job"])
    approved_detail = events(fx["project"], fx["job"], "STATUS_CHANGED")[-1]
    MATRIX[case] = {"failure": code_of(failed), "residual": brief(st_fail), "interrupted": interrupted,
                    "retry": code_of(retry), "after_retry": brief(st_retry), "audit_tm_added": st_retry["AUDIT_TM_ADDED"],
                    "ccms_rewritten_on_retry": rewritten, "events": st_retry["EVENTS"]}
    exp_res, exp_after = EXPECTED[case]
    exp_code, exp_phase, exp_committed, exp_added, exp_rewritten = EXPECTED_DETAIL[case]
    resumed = exp_committed
    check(f"F[{case}] fails with {exp_code} (phase {exp_phase}, translation committed: {exp_committed}), cause {CAUSES[case]}",
          code_of(failed) == exp_code and (failed[1].phase, failed[1].translation_committed) == (exp_phase, exp_committed)
          and type(failed[1].__cause__.__cause__ if exp_phase != "workflow" else failed[1].__cause__).__name__ == CAUSES[case],
          failed)
    check(f"F[{case}] residual state  {exp_res}", MATRIX[case]["residual"] == exp_res, MATRIX[case]["residual"])
    check(f"F[{case}] the state is represented: APPROVAL_INTERRUPTED (phase {exp_phase}, translation committed: "
          f"{exp_committed}, error type only), interrupted_approval() says so",
          recorded == [{"phase": exp_phase, "translation_committed": exp_committed, "error": CAUSES[case]}]
          and interrupted is not None and (interrupted["phase"], interrupted["translation_committed"]) == (exp_phase, exp_committed),
          (recorded, interrupted))
    check(f"F[{case}] retry succeeds", retry[0] == "ok", retry)
    check(f"F[{case}] after retry     {exp_after}", MATRIX[case]["after_retry"] == exp_after, MATRIX[case]["after_retry"])
    check(f"F[{case}] exactly one audit row, for this job; tm_entries_added {exp_added}; CCMS rewritten on retry: {exp_rewritten}",
          st_retry["AUDIT_TM_ADDED"] == exp_added and rewritten == exp_rewritten
          and [r["job_id"] for r in st_retry["AUDIT_ROWS"]] == [fx["job"]], (st_retry["AUDIT_ROWS"], rewritten))
    check(f"F[{case}] the TM never duplicates and CCMS translations stay Reviewed",
          st_retry["TM"] == 3 and st_retry["CCMS_STATUS"] == ["Reviewed"], st_retry)
    check(f"F[{case}] after the retry: events INTERRUPTED -> APPROVED, interrupted_approval() None",
          st_retry["EVENTS"] == [["APPROVAL_INTERRUPTED", ""], ["STATUS_CHANGED", "APPROVED"]] and still_interrupted is None,
          (st_retry["EVENTS"], still_interrupted))
    if resumed:
        audit_row = st_retry["AUDIT_ROWS"][0]
        check(f"F[{case}] resumed, not recommitted: the new ROUNDTRIP_COMMIT.json carries the audit's committed_at and "
              f"reviewer, marked completed_after_interruption, and so does the APPROVED event",
              retry[0] == "ok" and retry[1]["commit"]["resumed"] is True and last_json.get("completed_after_interruption") is True
              and last_json["committed_at"] == audit_row["committed_at"] and last_json["reviewer"] == audit_row["reviewer"]
              and approved_detail.get("completed_after_interruption") is True, (last_json.get("committed_at"), approved_detail))
    else:
        check(f"F[{case}] committed by the retry itself (nothing was committed before)",
              retry[0] == "ok" and retry[1]["commit"]["resumed"] is False and "completed_after_interruption" not in last_json
              and "completed_after_interruption" not in approved_detail, approved_detail)

# ============================================================ S -- the source changes after a committed interruption
print("== S  source edited after a committed interruption")
SC = build("source_changed")
P = Patch()
inject("E_files", SC, P)
try:
    s_fail = attempt(lambda: approve(SC))
finally:
    P.restore()
st_s0 = state(SC["project"], SC["job"])
r = IndustrialRepository(SC["db"])
try:
    r.update_block("B1", text="Install the drive vertically, on a wall.")
finally:
    r.close()
time.sleep(1.1)
s_retry = attempt(lambda: approve(SC))
st_s1 = state(SC["project"], SC["job"])
check("S1: the interruption after the commit, then the source edited: completing is refused by the QA gate "
      "(fail-closed kept), with a sentence saying the translation is already in the manual",
      code_of(s_fail) == "ApprovalInterruptedError" and code_of(s_retry) == "ValueError"
      and str(s_retry[1]).startswith("Commit bloccato dal Round-Trip QA") and "già stata registrata nel manuale" in str(s_retry[1]),
      s_retry)
check("S2: ... nothing is written by the refused retry: one audit row, CCMS rows untouched, TM 3, job QA_READY, "
      "no second interruption event",
      (st_s1["AUDIT"], st_s1["CCMS_UPDATED"], st_s1["TM"], st_s1["JOB"]) == (1, st_s0["CCMS_UPDATED"], 3, "QA_READY")
      and st_s1["EVENTS"] == [["APPROVAL_INTERRUPTED", ""]], st_s1)
s_refused = attempt(lambda: SC["wf"].transition(SC["job"], "CANCELLED", actor="CP5.4b"))
s_cancel = attempt(lambda: SC["wf"].transition(SC["job"], "CANCELLED", actor="CP5.4b", acknowledge_interrupted_approval=True))
SC["wf"].close()
check("S3: ... not stuck: cancelling needs the acknowledgment, then is recorded with the committed interruption",
      code_of(s_refused) == "ValueError" and s_cancel[0] == "ok" and s_cancel[1]["status"] == "CANCELLED"
      and events(SC["project"], SC["job"], "STATUS_CHANGED")[-1]["interrupted_approval"]["translation_committed"] is True,
      (s_refused, s_cancel))

# ============================================================ M -- the interruption marker itself is lost
print("== M  interruption marker lost")
LM = build("marker_lost")
LM["wf"].conn = ConnProxy(LM["wf"].conn, lambda sql, a: sql.startswith("UPDATE translation_jobs SET reviewer="),
                          fail_commit=True)          # ... and then the commit of the APPROVAL_INTERRUPTED event
m_fail = attempt(lambda: approve(LM))
pending = LM["wf"].conn.in_transaction
unwrap(LM)
m_inter = LM["wf"].interrupted_approval(LM["job"])
check("M1: the job-row UPDATE fails and so does the commit of the APPROVAL_INTERRUPTED event: the error still says "
      "the translation is committed; no event is stored; nothing is left pending on the Workflow connection",
      code_of(m_fail) == "ApprovalInterruptedError" and m_fail[1].translation_committed is True
      and events(LM["project"], LM["job"], "APPROVAL_INTERRUPTED") == [] and pending is False, (m_fail, pending))
check("M2: interrupted_approval() still knows, from the CCMS audit row: translation committed",
      m_inter is not None and m_inter["translation_committed"] is True, m_inter)
m_refused = attempt(lambda: LM["wf"].transition(LM["job"], "CANCELLED", actor="CP5.4b"))
check("M3: so the cancellation still needs the acknowledgment", code_of(m_refused) == "ValueError", m_refused)
time.sleep(1.1)
m_retry = attempt(lambda: approve(LM))
st_m = state(LM["project"], LM["job"])
LM["wf"].close()
check("M4: the retry completes the job once (resumed: one audit row, CCMS not rewritten)",
      m_retry[0] == "ok" and m_retry[1]["commit"]["resumed"] is True and brief(st_m) == {**APPROVED_ONCE, "FILES": [FULL, FULL]},
      (m_retry, brief(st_m)))

# ============================================================ P -- a fail-closed refusal before any write
print("== P  fail-closed refusal before any write")
PF = build("prewrite_refusal")
P = Patch()
real_targets = trt._segment_targets


def _drop_one(result):
    targets = real_targets(result)
    targets.pop(sorted(targets)[-1])
    return targets


P.set(trt, "_segment_targets", _drop_one)
try:
    p_fail = attempt(lambda: approve(PF))
finally:
    P.restore()
st_p = state(PF["project"], PF["job"])
p_inter = PF["wf"].interrupted_approval(PF["job"])
PF["wf"].close()
check("P1: a missing target is refused as itself (RuntimeError 'Fail-closed TM commit ...'), not as an interruption",
      code_of(p_fail) == "RuntimeError" and str(p_fail[1]).startswith("Fail-closed TM commit"), p_fail)
check("P2: ... before any write: no TM, no CCMS, no audit, no event, nothing to acknowledge",
      brief(st_p) == NOTHING_WRITTEN and st_p["EVENTS"] == [] and p_inter is None, (brief(st_p), p_inter))

# ============================================================ W -- a status change applies only from the status read
print("== W  status change from the status read")
WR = build("status_race")
approve(WR)
real_get = WR["wf"].get_job
WR["wf"].get_job = lambda job_id: {**real_get(job_id), "status": "QA_READY"}   # a read made before the approval
w_out = attempt(lambda: WR["wf"].transition(WR["job"], "CANCELLED", actor="CP5.4b", acknowledge_interrupted_approval=True))
del WR["wf"].get_job
WR["wf"].close()
check("W1: a cancellation decided on a stale QA_READY does not overwrite the APPROVED job",
      code_of(w_out) == "ValueError" and "cambiato nel frattempo" in str(w_out[1])
      and raw_job(WR["project"], WR["job"])["status"] == "APPROVED"
      and [e["to_status"] for e in ro(WR["project"] / "ManualSync_Workflow.sqlite",
                                      "SELECT to_status FROM workflow_events WHERE job_id=?", (WR["job"],))].count("CANCELLED") == 0,
      w_out)

# a job cancelled (confirmed, in another session) while its approval was committing
WC = build("cancelled_meanwhile")
real_commit = wfm.commit_translation_roundtrip


def _commit_then_cancel(*a, **kw):
    out = real_commit(*a, **kw)
    other_wf = WorkflowManager(WC["project"])
    try:
        other_wf.transition(WC["job"], "CANCELLED", actor="altra sessione", acknowledge_interrupted_approval=True)
    finally:
        other_wf.close()
    return out


P = Patch()
P.set(wfm, "commit_translation_roundtrip", _commit_then_cancel)
try:
    wc = attempt(lambda: approve(WC))
finally:
    P.restore()
st_wc = state(WC["project"], WC["job"])
WC["wf"].close()
check("W2: cancelled meanwhile: the approval does not close the job, and says the translation is in while the job "
      "was cancelled (not 'repeat')",
      code_of(wc) == "ApprovalInterruptedError" and "nel frattempo il job è stato annullato" in str(wc[1])
      and (st_wc["JOB"], st_wc["AUDIT"], st_wc["WORKFLOW"]) == ("CANCELLED", 1, [False, False]), (wc, brief(st_wc)))

# ============================================================ J -- the assets folder is a junction (another drive, say)
print("== J  assets folder behind a junction")
import _winapi  # noqa: E402 -- CreateJunction needs no privilege, unlike a symlink
JR = TMP / "junction"
(JR / "elsewhere_assets").mkdir(parents=True)
(JR / "project").mkdir()
_winapi.CreateJunction(str(JR / "elsewhere_assets"), str(JR / "project" / "ManualSync_Workflow_Assets"))
JX = build("junction_src")
JX["wf"].close()
wf = WorkflowManager(JR / "project")
try:
    shutil.copy2(JX["db"], JR / "project" / "ManualSync_CCMS.sqlite")
    j_job = attempt(lambda: wf.create_job_from_package(JX["langzip"], actor="CP5.4b"))
    if j_job[0] == "ok":
        wf.mark_sent(j_job[1]); wf.mark_in_translation(j_job[1]); wf.register_returned_xliff(j_job[1], JX["xlf"])
        wf.run_roundtrip_qa(j_job[1], JR / "project" / "ManualSync_CCMS.sqlite")
        j_ok = attempt(lambda: wf.approve_job(j_job[1], JR / "project" / "ManualSync_CCMS.sqlite", reviewer="Revisore J", human_approved=True))
    else:
        j_ok = j_job
finally:
    wf.close()
check("J1: with ManualSync_Workflow_Assets a junction, jobs are created (paths stored relative, not through the link) "
      "and approved",
      j_job[0] == "ok" and relative_posix(raw_job(JR / "project", j_job[1])["package_path"])
      and j_ok[0] == "ok" and j_ok[1]["job"]["status"] == "APPROVED", (j_job, j_ok))
os.rmdir(JR / "project" / "ManualSync_Workflow_Assets")                   # the junction itself, not its target

# ============================================================ Q -- terminology file; refusals before any change
print("== Q  terminology file, early refusals")
QT = build("terminology", terminology=True)
raw_q = raw_job(QT["project"], QT["job"])
check("Q1: the QA terminology copy is stored relative to the project and resolved by get_job",
      relative_posix(raw_q["qa_terminology_path"]) and raw_q["qa_terminology_path"].endswith("/terminology/terminologia.csv")
      and Path(QT["wf"].get_job(QT["job"])["qa_terminology_path"]).is_file() and raw_q["qa_strict"] == 1, raw_q["qa_terminology_path"])
QT["wf"].close()
arch_q = export(QT["project"], QT["root"] / "export")
QB = QT["root"] / "elsewhere" / "project"
WorkflowManager.restore_project_archive(arch_q, QB)
legacy_q = make_legacy(QT["project"], QT["job"])
arch_ql = export(QT["project"], QT["root"] / "export_legacy")
shutil.rmtree(QT["project"])
wf = WorkflowManager(QB)
try:
    q_ok = attempt(lambda: wf.approve_job(QT["job"], QB / "ManualSync_CCMS.sqlite", reviewer="Revisore Q", human_approved=True))
finally:
    wf.close()
check("Q2: restored elsewhere, original removed: approved in B with its terminology (strict) found in B",
      q_ok[0] == "ok" and q_ok[1]["job"]["status"] == "APPROVED", q_ok)
term_rel = Path(legacy_q["qa_terminology_path"]).relative_to(QT["project"])
QL = QT["root"] / "legacy_altered" / "project"
WorkflowManager.restore_project_archive(arch_ql, QL)
(QL / term_rel).write_text("source,target\nclearance,altro\n", encoding="utf-8")
wf = WorkflowManager(QL)
try:
    q_alt = attempt(lambda: wf.approve_job(QT["job"], QL / "ManualSync_CCMS.sqlite", reviewer="Revisore Q", human_approved=True))
finally:
    wf.close()
check("Q3: legacy row, terminology replaced by another content in the restored folder: refused (hash), nothing written",
      code_of(q_alt) == "JobFileMissingError" and term_rel.name in str(q_alt[1])
      and brief(state(QL, QT["job"])) == {**NOTHING_WRITTEN, "FILES": []}, q_alt)
# a job still IN_TRANSLATION / QA_BLOCKED whose package is not in the project
QR = build("receive_without_package", until="IN_TRANSLATION")
pkg_q = Path(QR["wf"].get_job(QR["job"])["package_path"])
pkg_q.unlink()
q_recv = attempt(lambda: QR["wf"].register_returned_xliff(QR["job"], QR["xlf"], actor="CP5.4b"))
raw_qr = raw_job(QR["project"], QR["job"])
check("Q4: receiving the XLIFF of a job whose package is gone is refused BEFORE anything changes ('Nulla è stato "
      "modificato' is true): still IN_TRANSLATION, no XLIFF registered, no inbound copy",
      code_of(q_recv) == "JobFileMissingError" and "Nulla è stato modificato" in str(q_recv[1])
      and (raw_qr["status"], raw_qr["returned_xliff_path"]) == ("IN_TRANSLATION", "")
      and not (QR["project"] / "ManualSync_Workflow_Assets" / QR["job"] / "inbound").exists(), (q_recv, raw_qr["status"]))
QR["wf"].close()
QX = build("rerun_without_package", until="RECEIVED")
QX["wf"].transition(QX["job"], "QA_BLOCKED", actor="CP5.4b")       # RECEIVED -> QA_BLOCKED: an allowed transition
Path(QX["wf"].get_job(QX["job"])["package_path"]).unlink()
q_rerun = attempt(lambda: QX["wf"].rerun_roundtrip_qa(QX["job"], QX["db"], actor="CP5.4b"))
check("Q5: re-running the QA of a QA_BLOCKED job whose package is gone is refused before the job is reopened",
      code_of(q_rerun) == "JobFileMissingError" and raw_job(QX["project"], QX["job"])["status"] == "QA_BLOCKED", q_rerun)
QX["wf"].close()

# ============================================================ N -- the transaction primitives
print("== N  transaction primitives")
ndb = TMP / "primitives" / "ManualSync_CCMS.sqlite"
ndb.parent.mkdir(parents=True)
repo = IndustrialRepository(ndb)
other = sqlite3.connect(ndb)
try:
    repo.create_topic("T1", "Title", "", source_lang="en-US", status="Approved")
    repo.add_block("B1", "T1", "paragraph", position=1, text="One.", status="Approved")
    repo.add_block("B2", "T1", "paragraph", position=2, text="Two.", status="Approved")
    count = lambda: other.execute("SELECT COUNT(*) FROM block_translations").fetchone()[0]  # noqa: E731
    repo.set_block_translation("B1", "it-IT", "Uno.", status="Reviewed")
    check("N1: outside single_transaction a translation setter still commits on its own (seen by another connection)",
          count() == 1, count())
    seen_inside = None
    try:
        with repo.single_transaction():
            repo.set_block_translation("B2", "it-IT", "Due.", status="Reviewed")
            repo.set_translation("T1", "it-IT", "", target_title="Titolo", status="Reviewed")
            seen_inside = count()
            raise RuntimeError("CP5.4b: abort inside")
    except RuntimeError:
        pass
    check("N2: inside, nothing is visible to another connection; an error rolls back every write",
          seen_inside == 1 and count() == 1 and other.execute("SELECT COUNT(*) FROM topic_translations").fetchone()[0] == 0,
          (seen_inside, count()))
    with repo.single_transaction():
        repo.set_block_translation("B2", "it-IT", "Due.", status="Reviewed")
    check("N3: without an error everything is committed once at the end", count() == 2, count())
    with repo.single_transaction():
        nested = attempt(lambda: repo.single_transaction().__enter__())
    check("N4: single_transaction is not reentrant", nested[0] == "raise" and isinstance(nested[1], RuntimeError), nested)
    repo.conn.execute("UPDATE content_blocks SET updated_at=updated_at WHERE block_id='B1'")      # a caller's open transaction
    adopted = attempt(lambda: repo.single_transaction().__enter__())
    repo.conn.rollback()
    check("N4b: a transaction the caller left open is refused, not silently committed or rolled back with the window",
          adopted[0] == "raise" and isinstance(adopted[1], RuntimeError) and "already has a transaction open" in str(adopted[1]),
          adopted)
    writer = sqlite3.connect(ndb, timeout=0)
    try:
        with repo.single_transaction():
            blocked_write = attempt(lambda: writer.execute("UPDATE content_blocks SET updated_at=updated_at WHERE block_id='B2'"))
    finally:
        writer.close()
    check("N4c: the write lock is taken on entry (BEGIN IMMEDIATE): another writer cannot interleave",
          blocked_write[0] == "raise" and "locked" in str(blocked_write[1]), blocked_write)
    # an audit table as written before CP5.4c (15 columns, no job_id), with one row
    legacy_ddl = re.search(r'self\.conn\.execute\("""(\s*CREATE TABLE IF NOT EXISTS translation_roundtrip_audit.*?\))\s*"""\)',
                           Path(BASE / "ccms_industrial.py").read_text(encoding="utf-8"), re.S).group(1)
    other.execute(legacy_ddl)
    other.execute("INSERT INTO translation_roundtrip_audit(build,source_kind,source_id,source_version,source_lang,target_lang,"
                  "package_sha256,returned_xliff_sha256,reviewer,human_approved,tm_path,tm_entries_added,ccms_commits_json,committed_at) "
                  "VALUES('old','block','B1',1,'en-US','it-IT','p','x','R',1,'C:/old/TM.sqlite',1,'[]','2026-01-01')")
    other.commit()
    check("N5: before the first CP5.4c commit, a legacy audit table has no job_id and no job matches it",
          "job_id" not in repo._roundtrip_audit_columns() and repo.translation_roundtrip_audit_for_job("JOB-X", "p", "x") is None)
    with repo.single_transaction():
        repo.record_translation_roundtrip_audit(build="new", source_kind="block", source_id="B1", source_version=1, source_lang="en-US",
                                                target_lang="it-IT", package_sha256="p", returned_xliff_sha256="x", reviewer="R",
                                                tm_path=TM_NAME, tm_entries_added=0, ccms_commits_json="[]",
                                                committed_at="2026-01-02", job_id="JOB-X")
    rows = [tuple(r) for r in other.execute("SELECT build,job_id FROM translation_roundtrip_audit ORDER BY id")]
    check("N6: the job_id column is added lazily; the old row keeps '' (untouched), the new row names its job",
          rows == [("old", ""), ("new", "JOB-X")] and repo.translation_roundtrip_audit_for_job("JOB-X", "p", "x")["build"] == "new"
          and repo.translation_roundtrip_audit_for_job("JOB-X", "p", "other") is None, rows)
finally:
    other.close()
    repo.close()
# the interruption as commit_translation_roundtrip raises it, and the direct call without a job
D = build("direct")
P = Patch()
inject("D_audit", D, P)
r = IndustrialRepository(D["db"])
try:
    j = D["wf"].get_job(D["job"])
    direct_fail = attempt(lambda: trt.commit_translation_roundtrip(r, D["project"], j["package_path"], j["returned_xliff_path"],
                                                                   human_approved=True, out_dir=D["root"] / "direct_1", job_id=D["job"]))
finally:
    P.restore()
    r.close()
check("N7: commit_translation_roundtrip raises RoundtripCommitInterrupted(phase, translation_committed), the cause chained",
      direct_fail[0] == "raise" and isinstance(direct_fail[1], trt.RoundtripCommitInterrupted)
      and (direct_fail[1].phase, direct_fail[1].translation_committed) == ("ccms", False)
      and isinstance(direct_fail[1].__cause__, sqlite3.OperationalError)
      and "OperationalError: CP5.4b injected: database is locked" in str(direct_fail[1]), direct_fail)


def direct(n):
    r = IndustrialRepository(D["db"])
    try:
        j = D["wf"].get_job(D["job"])
        return trt.commit_translation_roundtrip(r, D["project"], j["package_path"], j["returned_xliff_path"],
                                                human_approved=True, out_dir=D["root"] / f"direct_{n}")
    finally:
        r.close()


direct(2)
direct(3)
st_d = state(D["project"], D["job"])
D["wf"].close()
check("N8: DEFERRED (P2), unchanged: a direct commit WITHOUT a job appends an audit row each time (job_id '')",
      st_d["AUDIT"] == 2 and [x["job_id"] for x in st_d["AUDIT_ROWS"]] == ["", ""] and st_d["TM"] == 3, st_d["AUDIT_ROWS"])

# ============================================================ U -- what the user meets
print("== U  the user's view of an interrupted approval")
U = build("ui")
P = Patch()
inject("D_audit", U, P)
try:
    ui = attempt(lambda: app.job_action_ui(str(U["project"]), U["job"], "Approva traduzione", None, None, False, "Revisore UI", True))
finally:
    P.restore()
U["wf"].close()
shown = str(getattr(ui[1], "message", ui[1])) if ui[0] == "raise" else ""
check("U1: job_action_ui raises gr.Error with a sentence: interrupted, the manual not changed, nothing lost, "
      "«Approva traduzione» can be repeated -- the technical cause in brackets",
      ui[0] == "raise" and isinstance(ui[1], gr.Error) and U["job"] in shown and "si è interrotta" in shown
      and "Il manuale non è stato modificato" in shown and "Nulla è andato perso" in shown
      and "«Approva traduzione»" in shown and "(CP5.4b injected: database is locked)" in shown, shown)
st_u = state(U["project"], U["job"])
check("U2: the CCMS is untouched (rolled back), the TM complete, no audit, the job QA_READY",
      (st_u["TM"], st_u["CCMS"], st_u["AUDIT"], st_u["JOB"]) == (3, [0, 0], None, "QA_READY"), brief(st_u))
wf = WorkflowManager(U["project"])
try:
    events_u = app._friendly_events(wf.events_table(U["job"]))
finally:
    wf.close()
check("U3: the job's event table names the interruption in words",
      "Approvazione interrotta" in set(events_u["Evento"]), list(events_u["Evento"]))
ask = app.job_action_view_ui(str(U["project"]), U["job"], "Annulla", None, None, False, "", False)
job_u = app._job_row(str(U["project"]), U["job"])
inter_u = app._job_interrupted_approval(str(U["project"]), U["job"])
check("U4: 'Annulla' asks a question that says what cancelling leaves in the TM (not the generic one)",
      ask[0] == tv.job_cancel_confirm_notice(job_u, inter_u) and ask[0] != tv.job_cancel_confirm_notice(job_u)
      and html.escape(i18n.t("ux.job.cancel.interrupted.body")) in ask[0] and ask[9].startswith(U["job"] + "|"),
      (ask[0][:300], ask[9]))
wf = WorkflowManager(U["project"])
try:
    refused = attempt(lambda: wf.transition(U["job"], "CANCELLED", actor="API"))
    status_after_refusal = wf.get_job(U["job"])["status"]
finally:
    wf.close()
check("U5: the engine refuses that cancellation without the acknowledgment (a ValueError saying why); the job is unchanged",
      code_of(refused) == "ValueError" and "conferma esplicitamente l'annullamento" in str(refused[1])
      and status_after_refusal == "QA_READY", refused)
time.sleep(1.1)                                           # a human retry: a new approval/<second> folder
retry_ui = app.job_action_view_ui(str(U["project"]), U["job"], "Approva traduzione", None, None, False, "Revisore UI", True)
st_ur = state(U["project"], U["job"])
check("U6: 'Approva traduzione' again from the UI completes the job: APPROVED, one audit row, TM 3, CCMS translated",
      brief(st_ur) == {**APPROVED_ONCE, "FILES": [[], FULL]} and retry_ui[0] == tv.job_result_notice("Approva traduzione", app._job_row(str(U["project"]), U["job"])),
      (brief(st_ur), retry_ui[0][:200]))
# the committed variant: the ZIP fails after the CCMS + audit commit, then the job is cancelled
UC = build("ui_committed")
P = Patch()
inject("E_files", UC, P)
try:
    seen = app.job_action_view_ui(str(UC["project"]), UC["job"], "Approva traduzione", None, None, False, "Revisore UI", True)
finally:
    P.restore()
UC["wf"].close()
check("U7: the failure after the commit is shown in place, with the 'translation recorded, approval not completed' sentence",
      "è registrata nel manuale e nella memoria di traduzione" in seen[0] and "non verrà scritta una seconda volta" in seen[0]
      and UC["job"] in seen[0], seen[0][:600])
ask_c = app.job_action_view_ui(str(UC["project"]), UC["job"], "Annulla", None, None, False, "", False)
check("U8: 'Annulla' then asks the committed variant: the translation is recorded and cancelling does not remove it",
      html.escape(i18n.t("ux.job.cancel.interrupted.committed")) in ask_c[0], ask_c[0][:300])
done = app.job_cancel_confirmed_ui(str(UC["project"]), ask_c[9], None, None, False, "", False)
st_c = state(UC["project"], UC["job"])
cancel_detail = events(UC["project"], UC["job"], "STATUS_CHANGED")[-1]
check("U9: answered 'Sì': CANCELLED, the event records the acknowledged interruption; translation and audit stay",
      (st_c["JOB"], st_c["TM"], st_c["CCMS"], st_c["AUDIT"]) == ("CANCELLED", 3, [1, 2], 1)
      and cancel_detail.get("interrupted_approval", {}).get("phase") == "artifacts"
      and cancel_detail["interrupted_approval"]["translation_committed"] is True, (brief(st_c), cancel_detail))
# a question asked BEFORE the interruption is not an acknowledgment of it
UQ = build("ui_stale_question")
ask_q = app.job_action_view_ui(str(UQ["project"]), UQ["job"], "Annulla", None, None, False, "", False)
P = Patch()
inject("D_audit", UQ, P)
try:
    attempt(lambda: approve(UQ))
finally:
    P.restore()
UQ["wf"].close()
answer = app.job_cancel_confirmed_ui(str(UQ["project"]), ask_q[9], None, None, False, "", False)
check("U10: 'Sì' to the generic question, answered after the approval got interrupted: not taken as the acknowledgment "
      "-- the specific question is asked instead, the job is not cancelled",
      ask_q[9] == UQ["job"] and html.escape(i18n.t("ux.job.cancel.interrupted.body")) in answer[0]
      and answer[9].startswith(UQ["job"] + "|") and raw_job(UQ["project"], UQ["job"])["status"] == "QA_READY",
      (answer[0][:300], answer[9]))
i18n.set_language(i18n.DEFAULT_LANGUAGE)

# ============================================================ T -- cleanup
shutil.rmtree(TMP, ignore_errors=True)
check("T1: the temporary fixtures were removed", not TMP.exists(), TMP)

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
(BASE / "RISULTATO_TRANSLATION_ROUNDTRIP_SAFETY.json").write_text(json.dumps({
    "suite": "translation round-trip commit safety (CP5.4b-d)", "tests": total, "pass": passed, "fail": total - passed,
    "matrix": MATRIX,
    "results": [{"test": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in _results],
}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
print(f"\n  {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
