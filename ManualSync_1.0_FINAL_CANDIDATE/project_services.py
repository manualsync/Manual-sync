"""CP5: the composition root -- opens a project's content repository and its services.

Until CP5 these functions lived in the presenters (authoring_view, compliance_view),
so impact_center, impact_intelligence and translation_intelligence -- services --
had to import a presenter just to open the repository. They were moved here
unchanged in behaviour; the presenters keep `ccms_db_path` and `open_service` as
aliases of these same objects, so app and every existing caller are untouched.

The two openers differ ONLY in the error raised when the project has no content
database, and that difference is part of the contract (the UI shows a different
sentence): the authoring side raises ERR_AUTHORING_REPOSITORY_MISSING, the
compliance side ERR_PROJECT_REPOSITORY_MISSING. Where the database is, is
ProjectLayout's business; nothing here knows a file name.

No presentation, no SQL, no effect at import.
"""
from __future__ import annotations

from pathlib import Path

import domain_errors as de
from domain_errors import DomainFileMissingError
from ccms_industrial import IndustrialRepository
from project_layout import ProjectLayout
from authoring_service import AuthoringService
from compliance_intelligence import ComplianceService


def _ccms_db(project_dir: str | Path, missing_error: str) -> Path:
    layout = ProjectLayout(project_dir)
    p = layout.find_ccms_db()
    if p is None:
        raise DomainFileMissingError(missing_error, project_dir=str(layout.root))
    return p


def authoring_db_path(project_dir: str | Path) -> Path:
    return _ccms_db(project_dir, de.ERR_AUTHORING_REPOSITORY_MISSING)


def compliance_db_path(project_dir: str | Path) -> Path:
    return _ccms_db(project_dir, de.ERR_PROJECT_REPOSITORY_MISSING)


def open_authoring_service(project_dir: str | Path) -> tuple[IndustrialRepository, AuthoringService]:
    repo = IndustrialRepository(authoring_db_path(project_dir))
    return repo, AuthoringService(repo)


def open_compliance_service(project_dir: str | Path) -> tuple[IndustrialRepository, ComplianceService]:
    repo = IndustrialRepository(compliance_db_path(project_dir))
    return repo, ComplianceService(repo)
