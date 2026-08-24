from pathlib import Path
import json
import shutil
import zipfile
import pandas as pd

from cat_tm import TranslationMemory
from ccms_industrial import IndustrialRepository, create_industrial_demo
from translation_work_package import generate_translation_work_packages, inspect_work_xliff, export_work_xliff

BASE = Path(__file__).resolve().parent
OUT = BASE / "regressione_v23_out"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir()
results = []

def check(name, condition, detail=""):
    ok = bool(condition)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")

# Real v0.22-style fixture: one safe exact segment + one changed technical value.
demo = create_industrial_demo(OUT / "ccms")
repo = IndustrialRepository(demo["repo"])
try:
    tm_it_path = OUT / "TM_IT.sqlite"
    tm_de_path = OUT / "TM_DE.sqlite"
    tm_it = TranslationMemory(tm_it_path); tm_de = TranslationMemory(tm_de_path)
    try:
        exact = "Install the drive vertically on a rigid surface."
        tm_it.add("en-US", "it-IT", exact, "Installare il convertitore verticalmente su una superficie rigida.", approved=True, tm_name="IT Legacy", priority=100)
        tm_de.add("en-US", "de-DE", exact, "Installieren Sie den Antrieb senkrecht auf einer starren Oberfläche.", approved=True, tm_name="DE Legacy", priority=100)
        tm_it.add("en-US", "it-IT", "Maintain a clearance of 15 mm.", "Mantenere una distanza di 15 mm.", approved=True, tm_name="IT Legacy", priority=100)
    finally:
        tm_it.close(); tm_de.close()

    repo.update_block(
        "BLK-I-01",
        text="Install the drive vertically on a rigid surface. Maintain a clearance of 20 mm.",
        status="Approved",
        source_refs=["EngineeringChange#EC-2026-88"],
    )
    result = generate_translation_work_packages(
        repo, "BLK-I-01", kind="block", tm_paths=[tm_it_path, tm_de_path], fuzzy_threshold=70,
        require_approved=True, out_dir=OUT / "packages",
    )
    packages = result["packages"].set_index("target_lang")
    check("Two language packages generated", set(packages.index) == {"it-IT", "de-DE"}, packages.to_string())
    check("Safe auto reuse is locked outside work", int(packages.loc["it-IT", "auto_reuse_locked_segments"]) == 1 and int(packages.loc["de-DE", "auto_reuse_locked_segments"]) == 1, packages.to_string())
    check("Italian technical change is work only", int(packages.loc["it-IT", "technical_review"]) == 1 and int(packages.loc["it-IT", "work_segments"]) == 1, packages.to_string())
    check("German missing segment is new translation", int(packages.loc["de-DE", "new_translation"]) == 1 and int(packages.loc["de-DE", "work_segments"]) == 1, packages.to_string())

    it_zip = Path(result["master_zip"]).parent / packages.loc["it-IT", "package_zip"]
    de_zip = Path(result["master_zip"]).parent / packages.loc["de-DE", "package_zip"]
    check("Per-language ZIPs exist", it_zip.exists() and de_zip.exists(), f"{it_zip} | {de_zip}")
    with zipfile.ZipFile(it_zip) as zf:
        names = set(zf.namelist())
        check("Language ZIP contains XLIFF queue locked and manifest", {"ManualSync_it-IT_WORK_ONLY.xlf", "REVIEW_QUEUE.csv", "AUTO_REUSE_LOCKED.csv", "MANIFEST.json"}.issubset(names), names)
        extract = OUT / "inspect_it"; zf.extractall(extract)
    it_xlf = inspect_work_xliff(OUT / "inspect_it" / "ManualSync_it-IT_WORK_ONLY.xlf")
    check("AUTO_REUSE never enters translator XLIFF", not (it_xlf["work_bucket"] == "AUTO_REUSE").any() and len(it_xlf) == 1, it_xlf.to_string())
    check("Technical Review target is forcibly empty", it_xlf.iloc[0]["work_bucket"] == "TECHNICAL_REVIEW" and not str(it_xlf.iloc[0]["target"]).strip(), it_xlf.to_string())
    check("Legacy technical translation kept only as audit reference", "15 mm" in str(it_xlf.iloc[0]["legacy_reference"]), it_xlf.to_string())

    with zipfile.ZipFile(de_zip) as zf:
        extract = OUT / "inspect_de"; zf.extractall(extract)
    de_xlf = inspect_work_xliff(OUT / "inspect_de" / "ManualSync_de-DE_WORK_ONLY.xlf")
    check("New Translation target is empty", len(de_xlf) == 1 and de_xlf.iloc[0]["work_bucket"] == "NEW_TRANSLATION" and not str(de_xlf.iloc[0]["target"]).strip(), de_xlf.to_string())

    with zipfile.ZipFile(result["master_zip"]) as zf:
        names = set(zf.namelist())
        check("Master ZIP contains both language packages and impact audit", any("it-IT" in n for n in names) and any("de-DE" in n for n in names) and "IMPACT/TRANSLATION_IMPACT_SEGMENTS.csv" in names, sorted(names))

    # Hard invariant: calling the XLIFF exporter with AUTO_REUSE is rejected.
    blocked = False
    fake = pd.DataFrame([{"segment_id":"x","source":"Safe text","work_bucket":"AUTO_REUSE"}])
    try:
        export_work_xliff(fake, OUT / "forbidden.xlf", source_lang="en-US", target_lang="it-IT", source_version=1, changed_kind="block", changed_id="X")
    except ValueError as exc:
        blocked = "AUTO_REUSE" in str(exc)
    check("Exporter rejects AUTO_REUSE by invariant", blocked, "AUTO_REUSE must not reach translator workfile")

    # Source lifecycle gate remains fail-closed.
    repo.update_block("BLK-I-01", status="Draft")
    blocked = False
    try:
        generate_translation_work_packages(repo, "BLK-I-01", kind="block", tm_paths=[tm_it_path], require_approved=True, out_dir=OUT / "draft")
    except ValueError as exc:
        blocked = "non approvato" in str(exc)
    check("Draft source cannot generate packages", blocked, "Draft object must be rejected")
finally:
    repo.close()

summary = {
    "build": "v0.23",
    "tests": len(results),
    "pass": sum(r["status"] == "PASS" for r in results),
    "fail": sum(r["status"] == "FAIL" for r in results),
    "results": results,
}
(BASE / "RISULTATO_REGRESSIONE_TRANSLATION_WORK_PACKAGE_V23.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(BASE / "RISULTATO_REGRESSIONE_TRANSLATION_WORK_PACKAGE_V23.txt").write_text(
    "MANUALSYNC v0.23 — TRANSLATION WORK PACKAGE\n\n" +
    "\n".join(f"{r['status']} | {r['test']} | {r['detail']}" for r in results) +
    f"\n\nPASS: {summary['pass']}/{summary['tests']}\n", encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, indent=2))
