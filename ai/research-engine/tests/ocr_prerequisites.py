"""Shared skip marker for tests that require the real OCR/PDF toolchain
(tesseract-ocr, poppler-utils' pdftoppm/pdfinfo, and the img2pdf package) to
genuinely be installed and on PATH.

These are deliberate, documented integration tests (see the module
docstrings of test_image_evidence_extraction.py, test_image_evidence_manual_
upload.py, and test_current_news_draft_endpoint_regression.py): they prove
the real OCR happy path against the actual binaries, which
test_ocr_runtime_capability_gating.py already covers in a binary-independent
way by monkeypatching shutil.which. Without this marker, a developer or CI
runner missing these OS-level tools (confirmed: Windows dev machines
commonly lack them) gets a confusing FAILED/IntegrityError-style result
instead of an honest SKIPPED with a clear reason -- and production itself is
unaffected either way: app/image_evidence_extraction.py already fails
closed (never fabricates extracted text) when a binary is missing; this
marker only changes how the TEST SUITE reports that known, pre-existing
environment gap, never production behavior.

Install (Debian/Ubuntu): apt-get install -y tesseract-ocr poppler-utils
                         pip install img2pdf
Install (Windows): install Tesseract OCR and Poppler for Windows, add both
                    to PATH; pip install img2pdf
"""
from __future__ import annotations

import shutil

import pytest

TESSERACT_PRESENT = shutil.which("tesseract") is not None
POPPLER_PRESENT = shutil.which("pdftoppm") is not None and shutil.which("pdfinfo") is not None
IMG2PDF_PRESENT = shutil.which("img2pdf") is not None

OCR_TOOLCHAIN_PRESENT = TESSERACT_PRESENT and POPPLER_PRESENT and IMG2PDF_PRESENT

_MISSING = [
    name for name, present in (
        ("tesseract", TESSERACT_PRESENT),
        ("pdftoppm/pdfinfo (poppler-utils)", POPPLER_PRESENT),
        ("img2pdf", IMG2PDF_PRESENT),
    ) if not present
]
_REASON = (
    "requires the real OCR/PDF toolchain on PATH; missing: " + ", ".join(_MISSING)
    if _MISSING else ""
)

requires_ocr_toolchain = pytest.mark.skipif(not OCR_TOOLCHAIN_PRESENT, reason=_REASON)
requires_image_ocr = pytest.mark.skipif(not TESSERACT_PRESENT, reason="requires tesseract on PATH")
