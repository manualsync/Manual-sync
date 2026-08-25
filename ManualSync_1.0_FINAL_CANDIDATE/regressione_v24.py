from pathlib import Path
import json
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

# Windows consoles default to a legacy codepage (e.g. cp1252) that cannot encode
# characters used in some check details; force UTF-8 stdio so the final report
# print never crashes after all checks have already run and passed.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from cat_tm import TranslationMemory
from ccms_industrial import IndustrialRepository, create_industrial_demo
from translation_work_package import generate_translation_work_packages
from translation_roundtrip import inspect_translation_roundtrip, commit_translation_roundtrip, XLIFF_NS

BASE = Path(__file__).resolve().parent
OUT = BASE / "regressione_v24_out"
if OUT.exists(): shutil.rmtree(OUT)
OUT.mkdir()
results = []


def check(name, condition, detail=""):
    ok = bool(condition)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def extract_work(pkg_zip: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pkg_zip) as zf:
        zf.extractall(dest)
    return next(dest.glob("*_WORK_ONLY.xlf"))


def set_target(src_xlf: Path, out_xlf: Path, target: str, *, source_override=None, trg_lang=None, remove_unit=False):
    tree = ET.parse(src_xlf); root = tree.getroot(); ns = {"x": XLIFF_NS}
    if trg_lang is not None: root.set("trgLang", trg_lang)
    units = root.findall(".//x:unit", ns)
    if remove_unit and units:
        file_el = root.find(".//x:file", ns); file_el.remove(units[-1])
    else:
        for unit in units:
            seg = unit.find("./x:segment", ns)
            src = seg.find("./x:source", ns); tgt = seg.find("./x:target", ns)
            if source_override is not None: src.text = source_override
            tgt.text = target
            seg.set("state", "translated")
    tree.write(out_xlf, encoding="utf-8", xml_declaration=True)


demo = create_industrial_demo(OUT / "ccms")
repo = IndustrialRepository(demo["repo"])
try:
    tm_path = OUT / "TM_IT.sqlite"
    tm = TranslationMemory(tm_path)
    try:
        tm.add("en-US", "it-IT", "Maintain a clearance of 15 mm.", "Mantenere una distanza di 15 mm.", approved=True, tm_name="IT Legacy", priority=100)
    finally: tm.close()

    repo.update_block("BLK-I-01", text="Maintain a clearance of 20 mm.", status="Approved", source_refs=["EC#24"])
    pkg = generate_translation_work_packages(repo, "BLK-I-01", kind="block", tm_paths=[tm_path], fuzzy_threshold=70, require_approved=True, out_dir=OUT / "pkg")
    package_zip = Path(pkg["language_zips"][0])
    original_xlf = extract_work(package_zip, OUT / "original")

    good_xlf = OUT / "returned_good.xlf"
    set_target(original_xlf, good_xlf, "Mantenere una distanza di 20 mm.")
    qa = inspect_translation_roundtrip(repo, package_zip, good_xlf, out_dir=OUT / "qa_good")
    check("Clean returned XLIFF passes hard QA", qa["summary"]["hard_fail_count"] == 0 and qa["summary"]["commit_allowed_after_human_approval"], qa["summary"])
    check("Technical changed value preserved in target", qa["audit"].iloc[0]["hard_qa"] == "PASS", qa["audit"].to_string())

    badnum = OUT / "returned_badnum.xlf"
    set_target(original_xlf, badnum, "Mantenere una distanza di 25 mm.")
    qa_bad = inspect_translation_roundtrip(repo, package_zip, badnum, out_dir=OUT / "qa_badnum")
    check("Wrong numeric value is blocked", qa_bad["summary"]["hard_fail_count"] == 1 and not qa_bad["summary"]["commit_allowed_after_human_approval"], qa_bad["audit"].to_string())

    changed_src = OUT / "returned_changed_source.xlf"
    set_target(original_xlf, changed_src, "Mantenere una distanza di 20 mm.", source_override="Maintain a clearance of 25 mm.")
    qa_src = inspect_translation_roundtrip(repo, package_zip, changed_src, out_dir=OUT / "qa_src")
    check("Translator cannot alter source text", qa_src["summary"]["hard_fail_count"] == 1, qa_src["audit"].to_string())

    wrong_lang = OUT / "returned_wrong_lang.xlf"
    set_target(original_xlf, wrong_lang, "Mantenere una distanza di 20 mm.", trg_lang="fr-FR")
    qa_lang = inspect_translation_roundtrip(repo, package_zip, wrong_lang, out_dir=OUT / "qa_lang")
    check("Wrong target language is blocked", not qa_lang["summary"]["language_ok"] and not qa_lang["summary"]["commit_allowed_after_human_approval"], qa_lang["summary"])

    missing = OUT / "returned_missing.xlf"
    set_target(original_xlf, missing, "", remove_unit=True)
    qa_missing = inspect_translation_roundtrip(repo, package_zip, missing, out_dir=OUT / "qa_missing")
    check("Missing XLIFF unit is blocked", not qa_missing["summary"]["unit_set_ok"] and not qa_missing["summary"]["commit_allowed_after_human_approval"], qa_missing["summary"])

    # Package checksum tamper must be detected before QA.
    tamper_dir = OUT / "tamper_pkg"; tamper_dir.mkdir()
    with zipfile.ZipFile(package_zip) as zf: zf.extractall(tamper_dir)
    qcsv = tamper_dir / "REVIEW_QUEUE.csv"
    qcsv.write_text(qcsv.read_text(encoding="utf-8-sig") + "\nTAMPER", encoding="utf-8-sig")
    tampered_zip = OUT / "tampered_package.zip"
    with zipfile.ZipFile(tampered_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in tamper_dir.iterdir(): zf.write(f, arcname=f.name)
    tamper_block = False
    try:
        inspect_translation_roundtrip(repo, tampered_zip, good_xlf, out_dir=OUT / "qa_tamper")
    except ValueError as exc:
        tamper_block = "Checksum" in str(exc) or "checksum" in str(exc)
    check("Tampered original package checksum is blocked", tamper_block, "manifest checksum must fail")

    # Optional terminology can be strict and then blocks a technically safe target.
    term_csv = OUT / "terminology.csv"
    term_csv.write_text("source,target\nclearance,distanza\n", encoding="utf-8")
    badterm = OUT / "returned_badterm.xlf"
    set_target(original_xlf, badterm, "Mantenere uno spazio di 20 mm.")
    qa_term = inspect_translation_roundtrip(repo, package_zip, badterm, terminology_path=term_csv, strict_terminology=True, out_dir=OUT / "qa_term")
    check("Strict approved terminology can block commit", qa_term["summary"]["hard_fail_count"] == 0 and qa_term["summary"]["terminology_issue_count"] == 1 and not qa_term["summary"]["commit_allowed_after_human_approval"], qa_term["audit"].to_string())

    human_block = False
    try:
        commit_translation_roundtrip(repo, OUT / "project_nohuman", package_zip, good_xlf, human_approved=False, out_dir=OUT / "commit_nohuman")
    except ValueError as exc:
        human_block = "umana" in str(exc).lower()
    check("Commit requires explicit human approval", human_block, "must fail closed")

    project_dir = OUT / "project"; project_dir.mkdir()
    committed = commit_translation_roundtrip(repo, project_dir, package_zip, good_xlf, human_approved=True, reviewer="Regression Reviewer", out_dir=OUT / "commit_good")
    check("Approved round-trip creates confirmed TM", Path(committed["tm_path"]).exists(), committed["tm_path"])
    tm2 = TranslationMemory(committed["tm_path"])
    try:
        rows = tm2.entries("en-US", "it-IT", approved_only=True)
        check("Confirmed TM contains current 20 mm translation", any("20 mm" in r["source"] and "20 mm" in r["target"] for r in rows), [(r["source"], r["target"]) for r in rows])
    finally: tm2.close()
    tr = repo.conn.execute("SELECT * FROM block_translations WHERE block_id='BLK-I-01' AND target_lang='it-IT'").fetchone()
    check("CCMS block translation updated as Reviewed", tr is not None and tr["status"] == "Reviewed" and "20 mm" in tr["target_text"], dict(tr) if tr else None)
    audit_row = repo.conn.execute("SELECT * FROM translation_roundtrip_audit ORDER BY id DESC LIMIT 1").fetchone()
    check("Immutable round-trip audit row recorded", audit_row is not None and audit_row["human_approved"] == 1, dict(audit_row) if audit_row else None)
    check("Approval ZIP and commit JSON produced", Path(committed["approval_zip"]).exists() and Path(committed["commit_json"]).exists(), committed["summary"])

    # Source version changed after package generation: must fail closed.
    repo.update_block("BLK-I-01", text="Maintain a clearance of 30 mm.", status="Approved", source_refs=["EC#25"])
    stale = inspect_translation_roundtrip(repo, package_zip, good_xlf, out_dir=OUT / "qa_stale")
    check("Stale work package is blocked after source version change", not stale["summary"]["source_version_ok"] and not stale["summary"]["source_snapshot_ok"] and not stale["summary"]["commit_allowed_after_human_approval"], stale["summary"])

    # Placeholder safety using a fresh package.
    repo.update_block("BLK-I-01", text="Install {model} with torque 20 Nm.", status="Approved", source_refs=["EC#26"])
    tm3_path = OUT / "TM_PLACEHOLDER.sqlite"
    tm3 = TranslationMemory(tm3_path)
    try:
        tm3.add("en-US", "it-IT", "Install {model} with torque 15 Nm.", "Installare {model} con coppia 15 Nm.", approved=True, tm_name="Legacy", priority=100)
    finally: tm3.close()
    pkg2 = generate_translation_work_packages(repo, "BLK-I-01", kind="block", tm_paths=[tm3_path], fuzzy_threshold=60, require_approved=True, out_dir=OUT / "pkg_placeholder")
    pzip2 = Path(pkg2["language_zips"][0]); ox2 = extract_work(pzip2, OUT / "original_placeholder")
    badph = OUT / "returned_bad_placeholder.xlf"
    set_target(ox2, badph, "Installare la macchina con coppia 20 Nm.")
    qph = inspect_translation_roundtrip(repo, pzip2, badph, out_dir=OUT / "qa_placeholder")
    check("Lost placeholder is blocked", qph["summary"]["hard_fail_count"] == 1 and "placeholder" in qph["audit"].iloc[0]["hard_reason"].lower(), qph["audit"].to_string())
finally:
    repo.close()

summary = {"build":"v0.24","tests":len(results),"pass":sum(r["status"]=="PASS" for r in results),"fail":sum(r["status"]=="FAIL" for r in results),"results":results}
(BASE / "RISULTATO_REGRESSIONE_TRANSLATION_ROUNDTRIP_V24.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(BASE / "RISULTATO_REGRESSIONE_TRANSLATION_ROUNDTRIP_V24.txt").write_text("MANUALSYNC v0.24 — TRANSLATION ROUND-TRIP QA\n\n" + "\n".join(f"{r['status']} | {r['test']} | {r['detail']}" for r in results) + f"\n\nPASS: {summary['pass']}/{summary['tests']}\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
