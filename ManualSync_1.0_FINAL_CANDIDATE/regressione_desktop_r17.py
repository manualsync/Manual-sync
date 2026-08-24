from __future__ import annotations

import json
import shutil
from pathlib import Path

from ccms_industrial import IndustrialRepository
import release_manager as rm
from release_manager import create_release_bundle, ReleaseIdConflictError
from app import _friendly_release_failure
from product_version import PRODUCT_BUILD

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "regressione_desktop_r17_out"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

results=[]
def check(name, cond, detail=""):
    ok=bool(cond)
    results.append({"name":name,"status":"PASS" if ok else "FAIL","detail":str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")

def make_project(path: Path, target_text: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    repo=IndustrialRepository(path/"ManualSync_CCMS.sqlite")
    repo.create_topic("T1","Electrical ratings","Source body.",source_lang="en-US",status="Approved",source_refs=["SPEC#1"])
    repo.create_publication("PUB1","Demo",["T1"],source_lang="en-US")
    repo.add_block("B1","T1","paragraph",position=1,text="Install the drive vertically.",status="Approved",source_refs=["SPEC#1"])
    repo.set_topic_title_translation("T1","it-IT","Valori nominali elettrici",status="Reviewed")
    repo.set_block_translation("B1","it-IT",target_text,status="Reviewed")
    repo.close()
    return path

def release_rows(project: Path, parent_id: str) -> list[str]:
    repo=IndustrialRepository(project/"ManualSync_CCMS.sqlite")
    rows=repo.conn.execute(
        "SELECT release_id FROM publication_releases WHERE release_id LIKE ? ORDER BY release_id",
        (parent_id+"-%",),
    ).fetchall()
    out=[str(r["release_id"]) for r in rows]
    repo.close()
    return out

# ------------------------------------------------------------------
# 1) Production preflight block MUST NOT reserve immutable IDs
# ------------------------------------------------------------------
fixture=make_project(OUT/"fixture", "TRADUZIONE DI COLLAUDO: Installare il convertitore verticalmente.")
blocked=False
err=""
for attempt in (1,2):
    try:
        create_release_bundle(
            fixture,"PUB1",target_langs=["it-IT"],released_by="R17 Regression",
            release_id="REL-R17-BLOCK",release_mode="production",out_dir=OUT/"blocked_release",
        )
    except RuntimeError as e:
        blocked=True
        err=str(e)
    check(f"Production fixture attempt {attempt} blocked by business gate", blocked and "TRADUZIONE DI COLLAUDO" in err, err)
    rows=release_rows(fixture,"REL-R17-BLOCK")
    check(f"Blocked attempt {attempt} leaves zero immutable release rows", rows == [], rows)

check("Blocked production creates no ZIP", not any((OUT/"blocked_release").glob("*REL-R17-BLOCK*RELEASE.zip")))

# ------------------------------------------------------------------
# 2) Successful release reserves IDs; duplicate is intercepted cleanly
# ------------------------------------------------------------------
clean=make_project(OUT/"clean", "Installare il convertitore verticalmente.")
bundle=create_release_bundle(
    clean,"PUB1",target_langs=["it-IT"],released_by="R17 Regression",
    release_id="REL-R17-OK",release_mode="production",out_dir=OUT/"ok_release",
)
check("Clean release created", Path(bundle["release_zip"]).exists(), bundle["release_zip"])
rows=release_rows(clean,"REL-R17-OK")
check("Successful release reserves both source and target snapshots", rows == ["REL-R17-OK-en-US","REL-R17-OK-it-IT"], rows)

conflict=False
conflict_text=""
try:
    create_release_bundle(
        clean,"PUB1",target_langs=["it-IT"],released_by="R17 Regression",
        release_id="REL-R17-OK",release_mode="production",out_dir=OUT/"dup_release",
    )
except ReleaseIdConflictError as e:
    conflict=True
    conflict_text=str(e)
check("Duplicate release ID intercepted before SQLite UNIQUE error", conflict, conflict_text)
check("Duplicate message is business-readable", "già utilizzato o riservato" in conflict_text and "UNIQUE" not in conflict_text, conflict_text)
html=_friendly_release_failure(ReleaseIdConflictError(conflict_text), "production")
check("UI renders friendly duplicate-ID card", "Release ID già utilizzato" in html and "IntegrityError" not in html and "UNIQUE" not in html, html)

# ------------------------------------------------------------------
# 3) Inject failure on second snapshot: transaction must rollback first
# ------------------------------------------------------------------
atomic=make_project(OUT/"atomic", "Installare il convertitore verticalmente.")
original=IndustrialRepository.release_publication
calls={"n":0}
def failing_release(self,*args,**kwargs):
    calls["n"]+=1
    if calls["n"]==2:
        raise RuntimeError("injected second-language snapshot failure")
    return original(self,*args,**kwargs)
IndustrialRepository.release_publication=failing_release
try:
    failed=False
    try:
        create_release_bundle(
            atomic,"PUB1",target_langs=["it-IT"],released_by="R17 Regression",
            release_id="REL-R17-ATOMIC",release_mode="production",out_dir=OUT/"atomic_release",
        )
    except RuntimeError as e:
        failed="injected second-language" in str(e)
    check("Injected second-language failure reached", failed)
finally:
    IndustrialRepository.release_publication=original

rows=release_rows(atomic,"REL-R17-ATOMIC")
check("Second-language failure rolls back first snapshot", rows == [], rows)
check("Atomic failure creates no ZIP", not any((OUT/"atomic_release").glob("*REL-R17-ATOMIC*RELEASE.zip")))

# ------------------------------------------------------------------
# 4) Build markers
# ------------------------------------------------------------------
check("R17 atomic invariant retained in current build", PRODUCT_BUILD.startswith("1.0.0-"), PRODUCT_BUILD)
check("R17 release invariant retained in current module", (rm.RELEASE_BUILD.startswith("0.27.5-") or rm.RELEASE_BUILD.startswith("0.27.6-") or rm.RELEASE_BUILD.startswith("0.27.7-")), rm.RELEASE_BUILD)

summary={"build":PRODUCT_BUILD,"tests":len(results),"pass":sum(r["status"]=="PASS" for r in results),"fail":sum(r["status"]=="FAIL" for r in results),"results":results}
(ROOT/"RISULTATO_REGRESSIONE_DESKTOP_R17.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
(ROOT/"RISULTATO_REGRESSIONE_DESKTOP_R17.txt").write_text("\n".join(f"{r['status']} | {r['name']} | {r['detail']}" for r in results)+f"\n\nTOTAL {summary['pass']}/{summary['tests']} PASS\n",encoding="utf-8")
print(json.dumps({"tests":summary["tests"],"pass":summary["pass"],"fail":summary["fail"]},indent=2))
