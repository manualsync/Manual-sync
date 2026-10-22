"""CP3.3: the PDF-READING contract -- what the rest of ERYLOS may ask of a PDF.

PyMuPDF has two unrelated jobs in this product. READING: layout analysis for
migration and alignment, page text for grounding, the metadata title for
legacy-family matching. WRITING: HTML -> PDF with fitz.Story in
publishing_output, plus reading its own output back to verify it. They have
different callers, inputs and failure modes, so they are different contracts.
This module is the reading one; publishing keeps its writer for its own
checkpoint.

The contract lists ONLY the three operations the code performs today. It
imports nothing but the standard library, so importing it -- or anything that
depends on it -- never loads PyMuPDF. The implementation (pdf_pymupdf) is
loaded on first use by default_reader(), the one place that names it.

Errors are not translated: whatever the backend raises (fitz.FileDataError on
a corrupt file, FileNotFoundError on a missing one, ...) reaches the caller
unchanged, as it did when callers used PyMuPDF directly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class PdfReader(Protocol):
    name: str

    def extract_layout(self, pdf_path: str | Path, page_spec: str = "") -> Any:
        """Text blocks (with page, bbox, font size, heading flag), tables and images
        of the selected pages -- layout_engine's DocumentLayout today."""
        ...

    def iter_page_texts(self, pdf_path: str | Path) -> Iterator[str]:
        """The plain text of every page, in page order. The document is opened when
        iteration starts and closed when it ends or the iterator is closed."""
        ...

    def metadata_title(self, pdf_path: str | Path) -> str:
        """The document's metadata title, stripped; "" when it has none."""
        ...


def default_reader() -> PdfReader:
    """The reader the product uses: PyMuPDF. Callers look this up at call time
    (``pdf_backend.default_reader()``), so a different reader can be substituted
    here, in one place."""
    from pdf_pymupdf import PyMuPdfReader
    return PyMuPdfReader()
