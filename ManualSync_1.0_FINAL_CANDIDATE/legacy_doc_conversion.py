"""Isolated legacy Word .doc -> .docx conversion (Milestone 3B.1).

.doc (Word 97-2003 binary/OLE2 format) is not readable by python-docx, which
only understands the .docx (OOXML) container used by the Milestone 3B
pipeline. Rather than writing a second, from-scratch .doc parser/importer,
this module's only job is to produce a throwaway, isolated .docx file from a
.doc source using a local, offline LibreOffice headless conversion, so the
EXISTING, already-validated 3B pipeline (detect_docx_language,
import_docx_as_topics, _write_topic_groups_to_repo, ...) can consume it
unchanged. No domain logic (headings, tables, order, CCMS writing) lives here.

Runtime resolution order:
1. The sidecar LibreOffice runtime ManualSync ships next to the app
   (SIDECAR_SOFFICE below). This is the only path the shipped product is
   meant to depend on -- it must never require the customer to have
   LibreOffice or Microsoft Word installed. The installer/packaging work to
   actually place a runtime there is tracked separately
   (docs/reports/LEGACY_DOC_RUNTIME_PACKAGING_V1.md) and is NOT part of this
   module.
2. DEV-ONLY fallback: an explicit env var, or a system-installed LibreOffice,
   detected so engineers can exercise the real converter before the sidecar
   runtime is bundled. This fallback is never a supported customer dependency.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from docx import Document as DocxDocument

SIDECAR_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime" / "libreoffice"
SIDECAR_SOFFICE = SIDECAR_RUNTIME_DIR / "program" / "soffice.exe"

_DEV_FALLBACK_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


class LegacyDocConversionError(Exception):
    """Raised for any fail-closed outcome of a .doc -> .docx conversion."""


class LegacyDocConverterUnavailable(LegacyDocConversionError):
    """Raised when no local conversion runtime (sidecar or dev fallback) is found."""


def find_soffice_binary() -> Path | None:
    """Resolve a local soffice binary. See module docstring for the order."""
    if SIDECAR_SOFFICE.exists():
        return SIDECAR_SOFFICE
    env_override = os.environ.get("MANUALSYNC_DEV_SOFFICE_PATH")
    if env_override and Path(env_override).exists():
        return Path(env_override)
    for candidate in _DEV_FALLBACK_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p
    found = shutil.which("soffice")
    return Path(found) if found else None


@contextmanager
def convert_legacy_doc_to_docx(doc_path: str | Path, *, timeout: float = 120.0,
                                soffice_command: list[str] | None = None):
    """Convert a .doc file to a temporary, isolated .docx and yield its path.

    The temporary output directory and LibreOffice profile are removed when
    the `with` block exits, whether conversion succeeded or not -- the caller
    must finish reading/importing the yielded path before leaving the block.

    `soffice_command` overrides binary resolution (used by tests to inject a
    fake converter); production callers should leave it as None.

    Success requires more than exit code 0: the expected .docx must exist,
    be non-empty, and be openable by python-docx. Any other outcome (missing
    binary, timeout, crash, empty/corrupt output) raises
    LegacyDocConversionError (or the more specific
    LegacyDocConverterUnavailable) instead of silently producing bad data.
    """
    doc_path = Path(doc_path)
    if doc_path.suffix.lower() != ".doc":
        raise ValueError(f"convert_legacy_doc_to_docx richiede un file .doc, ricevuto: {doc_path.suffix!r}")

    if soffice_command is None:
        binary = find_soffice_binary()
        if binary is None:
            raise LegacyDocConverterUnavailable(
                "Nessun motore di conversione locale per documenti Word legacy (.doc) è disponibile su questa installazione."
            )
        soffice_command = [str(binary)]

    work_dir = Path(tempfile.mkdtemp(prefix="manualsync_doc_convert_"))
    profile_dir = work_dir / "profile"
    out_dir = work_dir / "out"
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        argv = [
            *soffice_command,
            "--headless", "--invisible", "--nocrashreport", "--nodefault",
            "--nologo", "--nofirststartwizard", "--norestore",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to", "docx",
            "--outdir", str(out_dir),
            str(doc_path.resolve()),
        ]
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            subprocess.run(argv, capture_output=True, timeout=timeout, **popen_kwargs)
        except subprocess.TimeoutExpired as exc:
            raise LegacyDocConversionError(
                f"Timeout ({timeout}s) durante la conversione locale di {doc_path.name}."
            ) from exc
        except OSError as exc:
            raise LegacyDocConversionError(
                f"Impossibile avviare il motore di conversione locale per {doc_path.name}."
            ) from exc

        # Exit code alone is not trusted: verify a real, openable .docx exists.
        produced = out_dir / f"{doc_path.stem}.docx"
        if not produced.exists() or produced.stat().st_size == 0:
            raise LegacyDocConversionError(
                f"La conversione locale di {doc_path.name} non ha prodotto un documento valido."
            )
        try:
            DocxDocument(str(produced))
        except Exception as exc:
            raise LegacyDocConversionError(
                f"Il documento convertito da {doc_path.name} risulta non apribile o corrotto."
            ) from exc

        yield produced
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
