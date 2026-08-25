from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from app import _auto_project_legacy_tm
from project_lifecycle import create_project

BASE = Path(__file__).resolve().parent
results = []


def check(name: str, cond: bool, detail="") -> None:
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="manualsync_alignment_policy_") as td:
        project = create_project(
            Path(td) / "Projects",
            project_name="Alignment Policy",
            source_lang="it-IT",
            target_langs=["en-US"],
        )
        analysis = project / "Analyses" / "RUN-001"
        analysis.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame([
            {
                "vecchio_originale": "Avviare il motore prima della calibrazione.",
                "vecchia_traduzione": "Start the motor before calibration.",
                "affidabilita_traduzione": 95.0,
            },
            {
                "vecchio_originale": "Chiudere il carter prima dell'avvio.",
                "vecchia_traduzione": "Close the guard before startup.",
                "affidabilita_traduzione": 85.0,
            },
            {
                "vecchio_originale": "Mantenere una distanza di 20 mm.",
                "vecchia_traduzione": "Maintain a clearance of 25 mm.",
                "affidabilita_traduzione": 98.0,
            },
        ])
        frame.to_csv(analysis / "RUN-001_dettaglio_testo.csv", index=False, encoding="utf-8-sig")
        (analysis / "RUN-001_sintesi.json").write_text(
            json.dumps({"lingua_vecchia_traduzione": "en-US"}), encoding="utf-8"
        )
        tm_path = _auto_project_legacy_tm(project)
        check("Project legacy TM created", tm_path is not None and Path(tm_path).exists(), tm_path)
        # NOTE: `with sqlite3.connect(...) as conn` only manages the transaction
        # (commit/rollback) — it does NOT close the connection. On Windows that left
        # this file (WAL mode) locked when the enclosing TemporaryDirectory tried to
        # delete it. Close explicitly so the handle is released before cleanup.
        conn = sqlite3.connect(tm_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT source,target,alignment_confidence,approved,penalty FROM tm_entries ORDER BY source"
            ).fetchall()
        finally:
            conn.close()
        by_source = {r["source"]: dict(r) for r in rows}
        check("Technically unsafe bilingual pair excluded", "Mantenere una distanza di 20 mm." not in by_source, list(by_source))
        hi = by_source.get("Avviare il motore prima della calibrazione.")
        mid = by_source.get("Chiudere il carter prima dell'avvio.")
        check("Confidence >=90 eligible for zero-penalty reuse", hi is not None and float(hi["penalty"]) == 0.0 and int(hi["approved"]) == 1, hi)
        check("Confidence 80-89 preserved as approved review suggestion", mid is not None and float(mid["penalty"]) > 0.0 and int(mid["approved"]) == 1, mid)
        check("Confidence 85 receives deterministic 5-point penalty", mid is not None and abs(float(mid["penalty"]) - 5.0) < 0.01, mid)

    summary = {
        "build": "ManualSync 1.0 FINAL CANDIDATE — alignment confidence policy",
        "tests": len(results),
        "pass": sum(x["status"] == "PASS" for x in results),
        "fail": sum(x["status"] != "PASS" for x in results),
        "results": results,
    }
    (BASE / "RISULTATO_ALIGNMENT_POLICY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (BASE / "RISULTATO_ALIGNMENT_POLICY.txt").write_text(
        "\n".join(f"{x['status']} | {x['test']} | {x['detail']}" for x in results)
        + f"\n\nPASS: {summary['pass']}/{summary['tests']}\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
