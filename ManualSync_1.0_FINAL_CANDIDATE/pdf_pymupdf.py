"""CP3.3: the PyMuPDF implementation of the PDF-reading contract (pdf_backend.PdfReader).

An adapter, deliberately thin: every method performs exactly the PyMuPDF calls its
callers used to make themselves, in the same order, with the same arguments, and
lets every exception through untouched. The layout analysis itself stays in
layout_engine (the PDF layout engine of the product); this class is the door to it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF

import layout_engine


class PyMuPdfReader:
    name = "pymupdf"

    def extract_layout(self, pdf_path: str | Path, page_spec: str = "") -> layout_engine.DocumentLayout:
        return layout_engine.extract_layout(pdf_path, page_spec)

    def iter_page_texts(self, pdf_path: str | Path) -> Iterator[str]:
        # was grounded_authoring.GroundingStore.ingest_pdf: fitz.open(path) / page.get_text("text") / close
        doc = fitz.open(pdf_path)
        try:
            for page in doc:
                yield page.get_text("text")
        finally:
            doc.close()

    def metadata_title(self, pdf_path: str | Path) -> str:
        # was legacy_family_inference._pdf_signals: fitz.open(str(path)) / metadata["title"] / close
        doc = fitz.open(str(pdf_path))
        try:
            return str((doc.metadata or {}).get("title") or "").strip()
        finally:
            doc.close()
