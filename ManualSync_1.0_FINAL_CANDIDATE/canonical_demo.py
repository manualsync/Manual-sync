"""Reproducible canonical demo — the reference baseline, built from scratch.

Why this is a repository module and not a one-off script
--------------------------------------------------------
The previous canonical demo drifted: an acceptance run recorded one extra
applicability decision in it, moving coverage 40% -> 33% while the header
counts stayed identical (2/38/0/1/41), so nothing on screen said so. The
demo could not simply be rebuilt, because the only recipe for it lived in
a scratch folder. A reference baseline whose recipe is not in the
repository is not a baseline; it is a file somebody hopes nobody breaks.

So the recipe lives here, and ``regressione_canonical_demo.py`` rebuilds
it on every run and asserts the numbers. If a future change to the pack,
the engine or the view model would alter the baseline, that suite fails
before anyone notices a demo reading the wrong percentage.

Rules this module holds itself to
---------------------------------
* **Supported service APIs only.** No SQL is executed against a project:
  every row is written by ``create_project``, the authoring service and
  ``ComplianceService``, exactly as the product UI writes them.
* **No special-casing.** The order below is forced by the engine, not
  chosen for convenience: ``set_assessment`` is fail-closed and refuses
  COVERED until current evidence is linked.
* **It never touches an existing project.** ``build_canonical_demo``
  refuses a directory that already exists.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import compliance_starter_packs as csp
import compliance_view as cv
from authoring_service import AuthoringService
from ccms_industrial import IndustrialRepository
from project_lifecycle import create_project

#: The documented reference baseline. Asserted by the regression suite.
BASELINE = {
    "requirements": 41,
    "applicability": 6,
    "covered_assessments": 2,
    "evidence": 3,
    "evidence_links": 3,
    "ratio_percent": 40,
}

#: Header counts the workspace shows for this baseline.
BASELINE_COUNTS = {"Covered": 2, "To verify": 38, "Missing": 0, "Not applicable": 1}

DEFAULT_NAME = "ERYLOS_Canonical_Demo"
PUBLICATION_ID = "PUB-MAIN"
ACTOR = "Responsabile Qualità"

# Five obligations a real machinery manual answers, plus one a professional
# machine genuinely does not: paper safety information for consumers.
_APPLIES = (
    "MR2023-A3-17441-INTENDED-USE",
    "MR2023-A3-17442-ASSEMBLY-INSTALL",
    "MR2023-A3-17442-MISUSE-WARNINGS",
    "MR2023-A3-17442-RESIDUAL-RISKS",
    "MR2023-A3-17442-PPE",
)
_NOT_APPLICABLE = ("MR2023-A10-7-PAPER-SAFETY-INFO",
                   "Macchina destinata esclusivamente a utilizzatori professionali.")
_COVERED = (
    ("MR2023-A3-17441-INTENDED-USE",
     "Il paragrafo iniziale descrive l'uso previsto della pompa."),
    ("MR2023-A3-17442-ASSEMBLY-INSTALL",
     "La sezione Installazione copre montaggio e collegamento."),
)


def build_canonical_demo(root: str | Path, *, name: str = DEFAULT_NAME) -> Path:
    """Create the demo under ``root`` and return its directory.

    Raises FileExistsError if the project already exists: rebuilding a
    canonical demo must never silently overwrite the one in use.
    """
    root = Path(root)
    if (root / name).exists():
        raise FileExistsError(f"{root / name} already exists")

    project_dir = create_project(
        root, project_name=name, client="ERYLOS Demo",
        product="Pompa centrifuga PC-200", manual_code="PC-200-IT", revision="A",
        source_lang="it-IT", owner=ACTOR)

    repo = IndustrialRepository(cv.ccms_db_path(project_dir))
    try:
        auth = AuthoringService(repo)

        # 1. A small but plausible manual. Evidence has to point at content
        #    that really exists at a real version -- add_evidence validates it.
        t_uso = repo.create_topic("T-USO", "Uso previsto", "", source_lang="it-IT", status="Draft")
        t_inst = repo.create_topic("T-INST", "Installazione", "", source_lang="it-IT",
                                   status="Draft")
        t_sic = repo.create_topic("T-SIC", "Sicurezza", "", source_lang="it-IT", status="Draft")
        repo.create_publication(PUBLICATION_ID,
                                "Manuale di istruzioni — Pompa centrifuga PC-200",
                                [t_uso, t_inst, t_sic], source_lang="it-IT")

        b_uso = auth.create_block_in_topic(
            t_uso, "paragraph", actor=ACTOR,
            text="La pompa centrifuga PC-200 è destinata al trasferimento di liquidi non "
                 "infiammabili in impianti industriali, entro i limiti di portata e prevalenza "
                 "indicati nella targa dati.")
        auth.create_block_in_topic(
            t_inst, "paragraph", actor=ACTOR,
            text="Fissare la pompa su basamento piano, allineare il giunto e collegare le "
                 "tubazioni di aspirazione e mandata senza sollecitare le bocche.")
        b_warn = auth.create_block_in_topic(
            t_sic, "warning", actor=ACTOR,
            text="Scollegare l'alimentazione elettrica e attendere l'arresto completo della "
                 "girante prima di qualsiasi intervento di manutenzione.")
        auth.create_block_in_topic(
            t_sic, "paragraph", actor=ACTOR,
            text="Indossare i dispositivi di protezione individuale previsti dalla valutazione "
                 "dei rischi dell'impianto.")

        # 2. The pack, through the same calls the "Aggiungi pacchetto" button makes.
        svc = cv.ComplianceService(repo)
        pack = csp.get_pack(csp.MACHINERY_DOC_PACK["pack_id"])
        scope_id = f"SCOPE-{csp.MACHINERY_DOC_PACK['pack_id']}"
        svc.create_scope(scope_id, PUBLICATION_ID, actor=ACTOR, title=pack["name"],
                         jurisdiction="EU")
        svc.import_requirement_pack(pack, scope_id=scope_id, actor=ACTOR,
                                    groups=[csp.GROUP_FULL_MACHINERY])

        # 3. Six applicability decisions.
        for rid in _APPLIES:
            svc.set_applicability(scope_id, rid, "APPLIES", actor=ACTOR)
        svc.set_applicability(scope_id, _NOT_APPLICABLE[0], "NOT_APPLICABLE", actor=ACTOR,
                              justification=_NOT_APPLICABLE[1])

        # 4. Evidence BEFORE assessment: set_assessment is fail-closed and
        #    refuses COVERED without valid current evidence. This ordering is
        #    the engine's rule, not a convenience.
        def _version(kind: str, object_id: str) -> int:
            return svc._object_current_version(kind, object_id)

        cv.link_ccms_object_as_evidence(
            svc, "block", b_uso["block_id"], _version("block", b_uso["block_id"]),
            "MR2023-A3-17441-INTENDED-USE", actor=ACTOR)
        cv.link_ccms_object_as_evidence(
            svc, "topic", t_inst, _version("topic", t_inst),
            "MR2023-A3-17442-ASSEMBLY-INSTALL", actor=ACTOR)
        cv.link_ccms_object_as_evidence(
            svc, "block", b_warn["block_id"], _version("block", b_warn["block_id"]),
            "MR2023-A3-17442-MISUSE-WARNINGS", actor=ACTOR)

        # 5. Two coverage assessments.
        for rid, comment in _COVERED:
            svc.set_assessment(scope_id, rid, "COVERED", actor=ACTOR, comment=comment)
    finally:
        repo.close()
    return project_dir


def measure(project_dir: str | Path) -> dict[str, Any]:
    """The baseline figures for a built demo, read the way the UI reads them."""
    repo = IndustrialRepository(cv.ccms_db_path(project_dir))
    try:
        svc = cv.ComplianceService(repo)
        scope_id = f"SCOPE-{csp.MACHINERY_DOC_PACK['pack_id']}"
        coverage = cv.documentation_coverage(svc, scope_id)
        count = lambda sql: int(svc.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "scope_id": scope_id,
            "requirements": len(svc.requirements_in_scope(scope_id)),
            "applicability": count("SELECT COUNT(*) FROM compliance_applicability"),
            "covered_assessments": count(
                "SELECT COUNT(*) FROM compliance_assessment WHERE status='COVERED'"),
            "evidence": count("SELECT COUNT(*) FROM compliance_evidence"),
            "evidence_links": count("SELECT COUNT(*) FROM compliance_evidence_link"),
            "ratio_percent": coverage["ratio_percent"],
            "counts": svc.summary_for_scope(scope_id)["counts"],
        }
    finally:
        repo.close()


def matches_baseline(measured: dict[str, Any]) -> bool:
    return all(measured.get(k) == v for k, v in BASELINE.items())
