from __future__ import annotations

import json
from pathlib import Path

from layout_engine import extract_layout, map_translation_blocks

BASE = Path(__file__).resolve().parent
BENCH = BASE / "benchmark"
RESULT_JSON = BASE / "RISULTATO_ALIGNMENT_REALE.json"
RESULT_TXT = BASE / "RISULTATO_ALIGNMENT_REALE.txt"

# These fixtures have comparable old-source/old-translation scopes and are used as
# a conservative release gate. Schneider is retained as an informational
# scope-mismatch fixture: 5 source blocks vs 3 translated blocks cannot represent a
# 95% alignable corpus by construction, so it must not be used to inflate/deflate
# an alignment-accuracy claim.
COMPARABLE = ("siemens", "abb_acs880", "abb_acs580")
INFORMATIONAL = ("schneider",)


def measure(name: str) -> dict:
    p = BENCH / name
    src = extract_layout(p / "old_IT.pdf").text_blocks
    tgt = extract_layout(p / "old_EN.pdf").text_blocks
    mapping = map_translation_blocks(src, tgt)
    confidences = [float(v[1]) for v in mapping.values()]
    source_count = len(src)
    mapped = len(mapping)
    high = sum(c >= 80.0 for c in confidences)
    low = sum(c < 60.0 for c in confidences)
    return {
        "fixture": name,
        "old_source_blocks": source_count,
        "old_translation_blocks": len(tgt),
        "mapped_blocks": mapped,
        "coverage_percent": round(100.0 * mapped / max(1, source_count), 1),
        "high_confidence_blocks_ge80": high,
        "high_confidence_percent_of_source": round(100.0 * high / max(1, source_count), 1),
        "mean_mapped_confidence": round(sum(confidences) / max(1, len(confidences)), 1),
        "low_confidence_mapped_lt60": low,
    }


def main() -> int:
    comparable = [measure(x) for x in COMPARABLE]
    informational = [measure(x) for x in INFORMATIONAL]

    source_total = sum(x["old_source_blocks"] for x in comparable)
    mapped_total = sum(x["mapped_blocks"] for x in comparable)
    high_total = sum(x["high_confidence_blocks_ge80"] for x in comparable)
    aggregate_coverage = round(100.0 * mapped_total / max(1, source_total), 1)
    aggregate_high = round(100.0 * high_total / max(1, source_total), 1)

    checks = []
    for row in comparable:
        checks.append({
            "test": f"{row['fixture']} legacy bilingual coverage >=95%",
            "status": "PASS" if row["coverage_percent"] >= 95.0 else "FAIL",
            "detail": f"{row['mapped_blocks']}/{row['old_source_blocks']} = {row['coverage_percent']:.1f}%",
        })
        checks.append({
            "test": f"{row['fixture']} no mapped pair below 60% confidence",
            "status": "PASS" if row["low_confidence_mapped_lt60"] == 0 else "FAIL",
            "detail": str(row["low_confidence_mapped_lt60"]),
        })

    checks += [
        {
            "test": "Aggregate comparable legacy bilingual coverage >=95%",
            "status": "PASS" if aggregate_coverage >= 95.0 else "FAIL",
            "detail": f"{mapped_total}/{source_total} = {aggregate_coverage:.1f}%",
        },
        {
            "test": "Aggregate high-confidence (>=80) coverage >=90%",
            "status": "PASS" if aggregate_high >= 90.0 else "FAIL",
            "detail": f"{high_total}/{source_total} = {aggregate_high:.1f}%",
        },
    ]

    failed = sum(x["status"] != "PASS" for x in checks)
    summary = {
        "build": "ManualSync 1.0 FINAL CANDIDATE — real industrial bilingual pre-alignment benchmark",
        "status": "PASS" if failed == 0 else "FAIL",
        "tests": len(checks),
        "pass": len(checks) - failed,
        "fail": failed,
        "comparable_fixtures": comparable,
        "informational_scope_mismatch_fixtures": informational,
        "aggregate_comparable": {
            "old_source_blocks": source_total,
            "mapped_blocks": mapped_total,
            "coverage_percent": aggregate_coverage,
            "high_confidence_blocks_ge80": high_total,
            "high_confidence_percent_of_source": aggregate_high,
        },
        "claim_scope": (
            "This benchmark measures old-source <-> old-translation structural pre-alignment on the included "
            "comparable industrial fixtures. It does not measure how much of a new revision is unchanged and "
            "is not a universal 95% customer-document guarantee. Customer-domain validation remains required "
            "before marketing a fixed percentage."
        ),
        "checks": checks,
    }
    RESULT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"{x['status']} | {x['test']} | {x['detail']}" for x in checks]
    lines += [
        "",
        f"Aggregate comparable pre-alignment coverage: {aggregate_coverage:.1f}% ({mapped_total}/{source_total})",
        f"Aggregate >=80 confidence coverage: {aggregate_high:.1f}% ({high_total}/{source_total})",
        "Schneider fixture is informational only because the bilingual scopes differ materially.",
        "No universal 95% customer-document guarantee is asserted by this benchmark.",
    ]
    RESULT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
