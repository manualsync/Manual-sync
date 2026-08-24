from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cat_batch import _read_text_with_inline
from cat_tm import TranslationMemory, load_terminology, source_target_safe
from ccms_industrial import IndustrialRepository
from translation_impact import extract_source_units, units_to_segments, publication_revision_token
from language_codes import canonical_lang
from linguistic_safety import cross_language_issue

ROUNDTRIP_BUILD = "0.24.1-linguistic-safety-qa-20260820"
XLIFF_NS = "urn:oasis:names:tc:xliff:document:2.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned.strip("._") or "unknown"


def _nonempty(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _extract_language_package(package_zip: str | Path, root: Path) -> dict[str, Any]:
    package_zip = Path(package_zip)
    if not package_zip.exists():
        raise FileNotFoundError(package_zip)
    with zipfile.ZipFile(package_zip) as zf:
        names = set(zf.namelist())
        required = {"MANIFEST.json", "REVIEW_QUEUE.csv", "AUTO_REUSE_LOCKED.csv"}
        if not required.issubset(names):
            raise ValueError("Pacchetto v0.23 non valido: mancano MANIFEST/REVIEW_QUEUE/AUTO_REUSE_LOCKED.")
        xlf_names = [n for n in names if n.endswith("_WORK_ONLY.xlf")]
        if len(xlf_names) != 1:
            raise ValueError("Pacchetto lingua non valido: deve contenere esattamente un XLIFF WORK_ONLY.")
        zf.extractall(root)
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    for name, meta in manifest.get("files", {}).items():
        p = root / name
        if not p.exists():
            raise ValueError(f"File dichiarato nel manifest mancante: {name}")
        actual = _sha256(p)
        expected = str(meta.get("sha256", ""))
        if expected and actual != expected:
            raise ValueError(f"Checksum non valido nel pacchetto originale: {name}")
    return {
        "manifest": manifest,
        "original_xlf": root / xlf_names[0],
        "review_queue": root / "REVIEW_QUEUE.csv",
        "locked_csv": root / "AUTO_REUSE_LOCKED.csv",
        "package_sha256": _sha256(package_zip),
    }


def _parse_xliff(path: str | Path) -> tuple[dict[str, str], pd.DataFrame]:
    path = Path(path)
    root = ET.parse(path).getroot()
    if root.tag.split("}")[-1] != "xliff":
        raise ValueError("Il file restituito non è un XLIFF valido.")
    ns_uri = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
    if ns_uri != XLIFF_NS or str(root.attrib.get("version", "")) != "2.0":
        raise ValueError("ManualSync v0.24 accetta XLIFF 2.0 generati dal work package v0.23.")
    ns = {"x": XLIFF_NS}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in root.findall(".//x:unit", ns):
        sid = str(unit.attrib.get("id", "")).strip()
        if not sid:
            raise ValueError("Unit XLIFF senza id.")
        if sid in seen:
            raise ValueError(f"Unit XLIFF duplicata: {sid}")
        seen.add(sid)
        notes = {str(n.attrib.get("category", "")): (n.text or "") for n in unit.findall("./x:notes/x:note", ns)}
        seg = unit.find("./x:segment", ns)
        if seg is None:
            raise ValueError(f"Unit {sid} senza segment.")
        src = seg.find("./x:source", ns)
        tgt = seg.find("./x:target", ns)
        rows.append({
            "segment_id": sid,
            "source": _read_text_with_inline(src) if src is not None else "",
            "target": _read_text_with_inline(tgt) if tgt is not None else "",
            "state": str(seg.attrib.get("state", "")),
            "work_bucket_note": notes.get("manualsync-work-bucket", ""),
            "source_ref_note": notes.get("manualsync-source-ref", ""),
            "object_note": notes.get("manualsync-object", ""),
        })
    meta = {
        "srcLang": str(root.attrib.get("srcLang", "")),
        "trgLang": str(root.attrib.get("trgLang", "")),
        "version": str(root.attrib.get("version", "")),
    }
    columns = ["segment_id","source","target","state","work_bucket_note","source_ref_note","object_note"]
    return meta, pd.DataFrame(rows, columns=columns)


def _terminology_issue(source: str, target: str, terminology: dict[str, set[str]]) -> str:
    if not terminology:
        return ""
    s = (source or "").casefold()
    t = (target or "").casefold()
    issues: list[str] = []
    for src_term, target_terms in terminology.items():
        if src_term in s and target_terms and not any(term in t for term in target_terms):
            issues.append(f"{src_term} → atteso uno tra: {', '.join(sorted(target_terms))}")
    return "; ".join(issues)


def _current_object_version(repo: IndustrialRepository, kind: str, changed_id: str) -> int:
    if kind == "publication":
        return publication_revision_token(repo,changed_id)
    mapping = {
        "topic": ("topics", "topic_id"),
        "block": ("content_blocks", "block_id"),
        "table": ("structured_tables", "table_id"),
        "figure": ("figures", "figure_id"),
        "component": ("components", "component_id"),
    }
    if kind not in mapping:
        raise ValueError(f"Tipo oggetto non supportato: {kind}")
    table, key = mapping[kind]
    row = repo.conn.execute(f"SELECT version FROM {table} WHERE {key}=?", (changed_id,)).fetchone()
    if not row:
        raise KeyError(changed_id)
    return int(row["version"])


def inspect_translation_roundtrip(
    repo: IndustrialRepository,
    language_package_zip: str | Path,
    returned_xliff: str | Path,
    *,
    terminology_path: str | Path | None = None,
    strict_terminology: bool = False,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="manualsync_v24_roundtrip_"))
    out.mkdir(parents=True, exist_ok=True)
    original_dir = out / "original_package"
    original_dir.mkdir(exist_ok=True)

    pkg = _extract_language_package(language_package_zip, original_dir)
    manifest = pkg["manifest"]
    source_meta = manifest.get("source", {})
    kind = str(source_meta.get("kind", "")).strip().lower()
    changed_id = str(source_meta.get("id", "")).strip()
    package_version = int(source_meta.get("version", 0) or 0)
    source_lang = canonical_lang(str(source_meta.get("source_lang", "")))
    target_lang = canonical_lang(str(manifest.get("target_lang", "")))
    if not kind or not changed_id or not source_lang or not target_lang:
        raise ValueError("Manifest v0.23 incompleto.")

    current_version = _current_object_version(repo, kind, changed_id)
    source_version_ok = current_version == package_version

    original_meta, original_work = _parse_xliff(pkg["original_xlf"])
    returned_meta, returned = _parse_xliff(returned_xliff)
    if canonical_lang(original_meta["srcLang"]) != source_lang or canonical_lang(original_meta["trgLang"]) != target_lang:
        raise ValueError("Lingue del work package originale non coerenti con il manifest.")
    language_ok = canonical_lang(returned_meta["srcLang"]) == source_lang and canonical_lang(returned_meta["trgLang"]) == target_lang

    original_ids = set(original_work["segment_id"].astype(str))
    returned_ids = set(returned["segment_id"].astype(str))
    missing_ids = sorted(original_ids - returned_ids)
    extra_ids = sorted(returned_ids - original_ids)
    unit_set_ok = not missing_ids and not extra_ids

    returned_map = {str(r["segment_id"]): r for _, r in returned.iterrows()}
    terminology = load_terminology(terminology_path, source_lang, target_lang) if terminology_path else {}

    audit_rows: list[dict[str, Any]] = []
    for _, orig in original_work.iterrows():
        sid = str(orig["segment_id"])
        rr = returned_map.get(sid)
        if rr is None:
            audit_rows.append({
                "segment_id": sid, "work_bucket": orig.get("work_bucket_note", ""), "source": orig["source"], "target": "",
                "hard_qa": "FAIL", "hard_reason": "segmento mancante nel file restituito", "terminology": "", "status": "BLOCKED",
            })
            continue
        source_unchanged = str(rr["source"]) == str(orig["source"])
        target = _nonempty(rr["target"])
        hard_reasons: list[str] = []
        if not source_unchanged:
            hard_reasons.append("sorgente XLIFF modificata")
        if not target:
            hard_reasons.append("target vuoto/incompleto")
        if target:
            safe, reason = source_target_safe(str(orig["source"]), target)
            if not safe:
                hard_reasons.append(reason)
            linguistic_reason = cross_language_issue(str(orig["source"]), target, source_lang, target_lang)
            if linguistic_reason:
                hard_reasons.append(linguistic_reason)
        term_issue = _terminology_issue(str(orig["source"]), target, terminology) if target else ""
        blocked = bool(hard_reasons) or (strict_terminology and bool(term_issue))
        audit_rows.append({
            "segment_id": sid,
            "work_bucket": orig.get("work_bucket_note", ""),
            "source": orig["source"],
            "target": target,
            "hard_qa": "FAIL" if hard_reasons else "PASS",
            "hard_reason": "; ".join(hard_reasons),
            "terminology": term_issue,
            "status": "BLOCKED" if blocked else ("WARNING" if term_issue else "READY_FOR_HUMAN_APPROVAL"),
        })

    audit = pd.DataFrame(audit_rows)

    # Revalidate all package source segments (work + auto reuse) against current CCMS content.
    locked = pd.read_csv(pkg["locked_csv"]).fillna("")
    current_units, _ = extract_source_units(repo, changed_id, kind=kind)
    _, current_meta = units_to_segments(current_units)
    current_map = {str(r["segment_id"]): str(r["source"]) for _, r in current_meta.iterrows()}
    package_sources = {str(r["segment_id"]): str(r["source"]) for _, r in original_work.iterrows()}
    for _, r in locked.iterrows():
        package_sources[str(r.get("segment_id", ""))] = str(r.get("source", ""))
    source_snapshot_ok = set(package_sources) == set(current_map) and all(current_map.get(sid) == src for sid, src in package_sources.items())

    hard_fail_count = int((audit["hard_qa"] == "FAIL").sum()) if not audit.empty else 0
    terminology_issue_count = int(audit["terminology"].astype(str).str.strip().ne("").sum()) if not audit.empty else 0
    incomplete_count = int(audit["hard_reason"].astype(str).str.contains("target vuoto", regex=False).sum()) if not audit.empty else 0
    linguistic_fail_count = int(audit["hard_reason"].astype(str).str.contains("lingue diverse", regex=False).sum()) if not audit.empty else 0
    linguistic_fail_count += int(audit["hard_reason"].astype(str).str.contains("stopword gate", regex=False).sum()) if not audit.empty else 0
    global_blockers: list[str] = []
    if not source_version_ok:
        global_blockers.append(f"versione sorgente cambiata dopo export: package={package_version}, current={current_version}")
    if not source_snapshot_ok:
        global_blockers.append("snapshot sorgente CCMS non coincide più con il pacchetto")
    if not language_ok:
        global_blockers.append("lingue src/trg del file restituito non coerenti")
    if not unit_set_ok:
        global_blockers.append(f"insieme unit diverso: missing={missing_ids}; extra={extra_ids}")
    if hard_fail_count:
        global_blockers.append(f"{hard_fail_count} segmenti con hard QA FAIL")
    if strict_terminology and terminology_issue_count:
        global_blockers.append(f"{terminology_issue_count} issue terminologiche in modalità strict")

    commit_allowed = not global_blockers
    summary = {
        "build": ROUNDTRIP_BUILD,
        "source": {"kind": kind, "id": changed_id, "version_package": package_version, "version_current": current_version, "source_lang": source_lang},
        "target_lang": target_lang,
        "package_sha256": pkg["package_sha256"],
        "returned_xliff_sha256": _sha256(returned_xliff),
        "source_version_ok": source_version_ok,
        "source_snapshot_ok": source_snapshot_ok,
        "language_ok": language_ok,
        "unit_set_ok": unit_set_ok,
        "missing_unit_ids": missing_ids,
        "extra_unit_ids": extra_ids,
        "segments": int(len(audit)),
        "hard_fail_count": hard_fail_count,
        "incomplete_count": incomplete_count,
        "linguistic_fail_count": linguistic_fail_count,
        "terminology_issue_count": terminology_issue_count,
        "strict_terminology": bool(strict_terminology),
        "human_approval_required": True,
        "commit_allowed_after_human_approval": commit_allowed,
        "global_blockers": global_blockers,
    }
    audit_csv = out / "ROUNDTRIP_QA_SEGMENTS.csv"
    summary_json = out / "ROUNDTRIP_QA_SUMMARY.json"
    audit.to_csv(audit_csv, index=False, encoding="utf-8-sig")
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "summary": summary,
        "audit": audit,
        "audit_csv": audit_csv,
        "summary_json": summary_json,
        "manifest": manifest,
        "locked": locked,
        "original_work": original_work,
        "returned": returned,
        "current_meta": current_meta,
        "out_dir": out,
    }


def _segment_targets(result: dict[str, Any]) -> dict[str, str]:
    target_map: dict[str, str] = {}
    locked = result["locked"]
    # First seed fixed/pretranslated locked rows (TM reuse and non-translatable codes).
    for _, r in locked.iterrows():
        sid = str(r.get("segment_id", ""))
        target = _nonempty(r.get("pretranslation") or r.get("suggested_translation"))
        if sid and target:
            target_map[sid] = target
    # Then add actual returned translator targets for canonical work segments.
    for _, r in result["audit"].iterrows():
        sid = str(r.get("segment_id", ""))
        target = _nonempty(r.get("target"))
        if sid and target:
            target_map[sid] = target
    # Finally propagate exact intra-document repetitions from their canonical segment.
    if "repeat_of_segment_id" in locked.columns:
        pending = locked[locked.get("work_bucket", pd.Series(index=locked.index, dtype=str)).astype(str) == "INTRA_DOCUMENT_REUSE"]
        for _, r in pending.iterrows():
            sid = str(r.get("segment_id", ""))
            parent = str(r.get("repeat_of_segment_id", ""))
            target = _nonempty(target_map.get(parent))
            if sid and parent and target:
                target_map[sid] = target
    return target_map


def _join_targets(group: pd.DataFrame) -> str:
    return " ".join(_nonempty(x) for x in group["target"] if _nonempty(x)).strip()


def _apply_ccms_translations(repo: IndustrialRepository, current_meta: pd.DataFrame, target_map: dict[str, str], target_lang: str) -> list[dict[str, Any]]:
    frame = current_meta.copy()
    frame["target"] = frame["segment_id"].map(target_map).fillna("")
    if frame["target"].astype(str).str.strip().eq("").any():
        missing = frame.loc[frame["target"].astype(str).str.strip().eq(""), "segment_id"].astype(str).tolist()
        raise RuntimeError(f"Fail-closed: target mancanti durante commit CCMS: {missing}")

    commits: list[dict[str, Any]] = []
    # Plain block / fallback topic.
    for (kind, oid), g in frame.groupby(["object_kind", "object_id"], sort=False):
        kind = str(kind); oid = str(oid)
        if kind == "block":
            repo.set_block_translation(oid, target_lang, _join_targets(g), status="Reviewed")
            commits.append({"object_kind": kind, "object_id": oid, "status": "CCMS_UPDATED"})
        elif kind == "topic":
            repo.set_translation(oid, target_lang, _join_targets(g), status="Reviewed")
            commits.append({"object_kind": kind, "object_id": oid, "status": "CCMS_UPDATED"})
        elif kind == "topic_title":
            repo.set_topic_title_translation(oid, target_lang, _join_targets(g), status="Reviewed")
            commits.append({"object_kind": kind, "object_id": oid, "status": "CCMS_UPDATED", "note": "topic title localized"})
        elif kind == "table":
            src = repo.conn.execute("SELECT * FROM structured_tables WHERE table_id=?", (oid,)).fetchone()
            columns = json.loads(src["columns_json"] or "[]")
            rows = json.loads(src["rows_json"] or "[]")
            caption = ""
            unsupported_title = False
            for ref, rg in g.groupby("source_ref", sort=False):
                value = _join_targets(rg)
                ref = str(ref)
                if ref.endswith("#caption"):
                    caption = value
                elif ref.endswith("#title"):
                    unsupported_title = True
                else:
                    m = re.search(r"#header\[(\d+)\]$", ref)
                    if m:
                        i = int(m.group(1)) - 1
                        if 0 <= i < len(columns): columns[i] = value
                        continue
                    m = re.search(r"#row\[(\d+)\]col\[(\d+)\]$", ref)
                    if m:
                        ri, ci = int(m.group(1)) - 1, int(m.group(2)) - 1
                        if 0 <= ri < len(rows) and 0 <= ci < len(rows[ri]): rows[ri][ci] = value
            repo.set_table_translation(oid, target_lang, caption=caption, columns=columns, rows=rows, status="Reviewed")
            commits.append({"object_kind": kind, "object_id": oid, "status": "CCMS_UPDATED", "note": "title non localizzato nello schema v0.18" if unsupported_title else ""})
        elif kind == "figure":
            caption = ""; alt_text = ""; unsupported_title = False
            for ref, rg in g.groupby("source_ref", sort=False):
                value = _join_targets(rg); ref = str(ref)
                if ref.endswith("#caption"): caption = value
                elif ref.endswith("#alt_text"): alt_text = value
                elif ref.endswith("#title"): unsupported_title = True
            repo.set_figure_translation(oid, target_lang, caption=caption, alt_text=alt_text, status="Reviewed")
            commits.append({"object_kind": kind, "object_id": oid, "status": "CCMS_UPDATED", "note": "title non localizzato nello schema v0.18" if unsupported_title else ""})
        elif kind == "component":
            rows = g.sort_values("segment_id")
            title = ""
            body_parts = []
            for _, rr in rows.iterrows():
                sid = str(rr.get("segment_id", ""))
                target = str(rr.get("target", "") or "").strip()
                if sid.endswith(":title"):
                    title = target
                elif target:
                    body_parts.append(target)
            repo.set_component_translation(oid, target_lang, " ".join(body_parts), target_title=title, status="Reviewed")
            commits.append({"object_kind": kind, "object_id": oid, "status": "CCMS_UPDATED", "note": "component translation persisted"})
        else:
            commits.append({"object_kind": kind, "object_id": oid, "status": "TM_ONLY", "note": "tipo non persistibile nel CCMS corrente"})
    return commits


def commit_translation_roundtrip(
    repo: IndustrialRepository,
    project_dir: str | Path,
    language_package_zip: str | Path,
    returned_xliff: str | Path,
    *,
    human_approved: bool,
    terminology_path: str | Path | None = None,
    strict_terminology: bool = False,
    reviewer: str = "Human reviewer",
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not human_approved:
        raise ValueError("Conferma umana obbligatoria: ManualSync non approva automaticamente le traduzioni.")
    result = inspect_translation_roundtrip(
        repo, language_package_zip, returned_xliff,
        terminology_path=terminology_path, strict_terminology=strict_terminology, out_dir=out_dir,
    )
    summary = result["summary"]
    if not summary["commit_allowed_after_human_approval"]:
        raise ValueError("Commit bloccato dal Round-Trip QA: " + " | ".join(summary["global_blockers"]))

    target_map = _segment_targets(result)
    current_meta = result["current_meta"]
    source_lang = summary["source"]["source_lang"]
    target_lang = summary["target_lang"]

    tm_path = Path(project_dir) / "ManualSync_CONFIRMED_TM.sqlite"
    tm = TranslationMemory(tm_path)
    added = 0
    try:
        for _, r in current_meta.iterrows():
            sid = str(r["segment_id"])
            source = str(r["source"])
            target = _nonempty(target_map.get(sid))
            safe, reason = source_target_safe(source, target)
            if not target or not safe:
                raise RuntimeError(f"Fail-closed TM commit {sid}: {reason or 'target vuoto'}")
            before = tm.count()
            tm.add(source_lang, target_lang, source, target, origin=f"ManualSync v0.24 RoundTrip {summary['source']['kind']}:{summary['source']['id']}", approved=True, tm_name="Project Confirmed", priority=200, penalty=0)
            added += tm.count() - before
    finally:
        tm.close()

    ccms_commits = _apply_ccms_translations(repo, current_meta, target_map, target_lang)

    repo.conn.execute("""
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
    repo.conn.execute(
        """INSERT INTO translation_roundtrip_audit(build,source_kind,source_id,source_version,source_lang,target_lang,package_sha256,returned_xliff_sha256,reviewer,human_approved,tm_path,tm_entries_added,ccms_commits_json,committed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ROUNDTRIP_BUILD, summary["source"]["kind"], summary["source"]["id"], int(summary["source"]["version_current"]), source_lang, target_lang,
         summary["package_sha256"], summary["returned_xliff_sha256"], str(reviewer or "Human reviewer"), 1, str(tm_path), int(added), json.dumps(ccms_commits, ensure_ascii=False), _now()),
    )
    repo.conn.commit()

    out = Path(result["out_dir"])
    approved_xlf = out / f"ManualSync_{_safe_name(target_lang)}_APPROVED_RETURN.xlf"
    shutil.copy2(returned_xliff, approved_xlf)
    commit_payload = {
        **summary,
        "approved_by_human": True,
        "reviewer": str(reviewer or "Human reviewer"),
        "tm_path": str(tm_path),
        "tm_entries_added": int(added),
        "ccms_commits": ccms_commits,
        "committed_at": _now(),
    }
    commit_json = out / "ROUNDTRIP_COMMIT.json"
    commit_json.write_text(json.dumps(commit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    approval_zip = out / f"ManualSync_{_safe_name(target_lang)}_ROUNDTRIP_APPROVAL.zip"
    with zipfile.ZipFile(approval_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(approved_xlf, arcname=approved_xlf.name)
        zf.write(result["audit_csv"], arcname="ROUNDTRIP_QA_SEGMENTS.csv")
        zf.write(result["summary_json"], arcname="ROUNDTRIP_QA_SUMMARY.json")
        zf.write(commit_json, arcname=commit_json.name)

    return {
        "summary": commit_payload,
        "audit": result["audit"],
        "tm_path": tm_path,
        "commit_json": commit_json,
        "approval_zip": approval_zip,
        "ccms_commits": pd.DataFrame(ccms_commits),
    }
