"""CP3.2: the ONE place that knows how ERYLOS lays out its data on disk.

Before CP3.2 the same knowledge was copied into a dozen modules: the data
home (``~/ManualSyncData``), its ``Projects`` folder, and the content
database of a project -- ``ManualSync_CCMS.sqlite``, or the older
``ManualSync_CCMS_v18.sqlite`` that early projects still carry, searched in
that order. Every copy had to agree; nothing made them.

This module only *names* paths. It never creates, opens, lists or deletes
anything, never reads the environment beyond ``Path.home()`` when asked for
the default data home, and imports nothing but the standard library -- so a
desktop app, a test or a future service can resolve a project's files
without pulling in SQLite, Gradio or the PDF stack. What happens when a file
is missing (which exception, which message) stays with each caller: the
callers raise different, user-visible errors and those are part of their
behaviour.

Deliberately NOT here (yet): the workflow database, the TM databases,
exports, logs, the project manifest. They are not duplicated today, or they
belong to a later checkpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DATA_DIR_NAME = "ManualSyncData"
PROJECTS_DIR_NAME = "Projects"

CCMS_DB_NAME = "ManualSync_CCMS.sqlite"
LEGACY_CCMS_DB_NAME = "ManualSync_CCMS_v18.sqlite"
# Lookup order for an EXISTING project: the current name first, then the legacy one.
CCMS_DB_CANDIDATES = (CCMS_DB_NAME, LEGACY_CCMS_DB_NAME)


def default_data_home(home: str | Path | None = None) -> Path:
    """``<home>/ManualSyncData`` -- the user's home when `home` is not given.

    Pure: the folder is not created (callers that need it create it, as before).
    """
    return (Path.home() if home is None else Path(home)) / DATA_DIR_NAME


def projects_dir(data_home: str | Path) -> Path:
    """The folder that holds one sub-folder per project."""
    return Path(data_home) / PROJECTS_DIR_NAME


@dataclass(frozen=True)
class ProjectLayout:
    """Where the files of ONE project live. Construction touches nothing on disk.

    `root` is kept exactly as given (relative stays relative, nothing is
    resolved), so every path below is what the old ``Path(project_dir) / name``
    code produced.
    """

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    @property
    def ccms_db(self) -> Path:
        """The content database a NEW project gets, and the one opened directly."""
        return self.root / CCMS_DB_NAME

    def find_ccms_db(self) -> Path | None:
        """The content database of an EXISTING project: the first candidate that
        exists, or None. Never creates anything."""
        for name in CCMS_DB_CANDIDATES:
            candidate = self.root / name
            if candidate.exists():
                return candidate
        return None

    def has_ccms_db(self) -> bool:
        return self.find_ccms_db() is not None
