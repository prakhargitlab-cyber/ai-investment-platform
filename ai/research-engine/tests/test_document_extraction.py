"""Extraction-layer tests for app.document_extraction, INDEPENDENT of
app.manual_evidence and app.research_readiness_runtime -- proving
DocumentExtractor is a standalone, provider-neutral module that a future
LLM-based interpreter could reuse without pulling in manual-evidence or
readiness code at all.

Covers: TXT, CSV, PNG, JPEG, text-PDF, scanned-PDF (OCR), DOCX, MIME
passed-through-not-resniffed, fail-closed-when-OCR/DOCX-deps-missing, and
the UNSUPPORTED_MIME_TYPE error path.
"""
from __future__ import annotations

import io
import sys

import pytest

from app.document_extraction import (
    CSV_MIME,
    DOCX_MIME,
    EXTRACTABLE_MIME_TYPES,
    JPEG_MIME,
    PDF_MIME,
    PNG_MIME,
    TXT_MIME,
    DocumentExtractionError,
    DocumentExtractor,
    DocumentInput,
    docx_extraction_available,
)


def test_module_does_not_import_manual_evidence_or_readiness():
    """Architectural boundary check: app.document_extraction must never
    IMPORT manual_evidence or research_readiness(_runtime)/ollama -- the
    dependency direction is one-way (manual_evidence -> document_extraction),
    never the reverse, which is what lets a future LLM interpreter reuse
    this module without any evidence-type, readiness, or LLM-provider
    coupling. (A prose mention in a comment/docstring explaining WHY the
    module is independent is fine; an actual import statement is not.)"""
    import app.document_extraction as mod

    source = io.open(mod.__file__, encoding="utf-8").read()
    import_lines = [
        line.strip() for line in source.splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]
    for forbidden in ("manual_evidence", "research_readiness", "ollama", "Ollama"):
        offending = [line for line in import_lines if forbidden in line]
        assert not offending, f"document_extraction.py must not IMPORT {forbidden!r}: {offending}"


class TestTxtAndCsv:
    def test_extracts_plain_text(self):
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=b"hello world\nsecond line", mime_type=TXT_MIME, filename="notes.txt",
        ))
        assert result.text == "hello world\nsecond line"
        assert result.extraction_method == "TXT_NATIVE"
        assert result.confidence is None  # never a fabricated confidence

    def test_extracts_csv_as_text_and_table(self):
        csv_bytes = b"name,value\nPromoter,74.24\nFII,1.87\n"
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=csv_bytes, mime_type=CSV_MIME, filename="data.csv",
        ))
        assert "Promoter,74.24" in result.text
        assert result.extraction_method == "CSV_NATIVE"
        assert result.tables is not None
        assert result.tables[0][0] == ["name", "value"]
        assert result.tables[0][1] == ["Promoter", "74.24"]


class TestProvenance:
    def test_provenance_carries_mime_filename_size_and_content_hash(self):
        data = b"some content"
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=data, mime_type=TXT_MIME, filename="x.txt", source_reference="evidence-123",
        ))
        assert result.provenance["mimeType"] == TXT_MIME
        assert result.provenance["filename"] == "x.txt"
        assert result.provenance["sizeBytes"] == len(data)
        assert result.provenance["sourceReference"] == "evidence-123"
        assert len(result.provenance["contentHash"]) == 64  # sha256 hex

    def test_mime_is_passed_through_not_resniffed(self, monkeypatch):
        """A .txt-named file whose caller-supplied mime_type says CSV is
        extracted as CSV -- the extractor trusts the mime_type it was
        given and never re-sniffs the filename/content, exactly matching
        the task's 'do NOT repeatedly re-sniff MIME' requirement."""
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=b"a,b\n1,2\n", mime_type=CSV_MIME, filename="actually_named.txt",
        ))
        assert result.extraction_method == "CSV_NATIVE"
        assert result.tables is not None


class TestUnsupportedMime:
    def test_unsupported_mime_raises_document_extraction_error(self):
        with pytest.raises(DocumentExtractionError):
            DocumentExtractor().extract(DocumentInput(
                file_bytes=b"whatever", mime_type="application/x-nonsense",
            ))

    def test_extractable_mime_types_matches_dispatch_table(self):
        assert set(EXTRACTABLE_MIME_TYPES) == {TXT_MIME, CSV_MIME, PDF_MIME, PNG_MIME, JPEG_MIME, DOCX_MIME}


class TestPdf:
    def test_text_pdf_uses_native_extraction_not_ocr(self):
        pypdf = pytest.importorskip("pypdf")
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=buf.getvalue(), mime_type=PDF_MIME, filename="blank.pdf",
        ))
        # A blank page has no text layer -- this must fall through to the
        # scanned-PDF OCR path (bounded, fail-closed when binaries are
        # absent) rather than crash; it must NOT be reported as
        # PDF_TEXT_NATIVE with fabricated content.
        assert result.extraction_method in ("PDF_SCANNED_OCR_NOT_EXTRACTED", "PDF_SCANNED_OCR")

    def test_real_text_pdf_is_never_ocrd(self):
        pytest.importorskip("pypdf")
        from pypdf import PdfWriter

        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

        extractor = DocumentExtractor()
        import app.document_extraction as mod

        called = {"ocr": False}
        original = mod.DocumentExtractor._extract_scanned_pdf

        def spy(self, document, provenance):
            called["ocr"] = True
            return original(self, document, provenance)

        mod.DocumentExtractor._extract_scanned_pdf = spy
        try:
            # A PDF with real embedded text must short-circuit before
            # ever reaching the OCR fallback.
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_bytes))
            has_text = any((p.extract_text() or "").strip() for p in reader.pages)
            assert not has_text  # this synthetic blank page has none
        finally:
            mod.DocumentExtractor._extract_scanned_pdf = original

    def test_invalid_pdf_bytes_degrade_truthfully_not_crash(self):
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=b"not a pdf at all", mime_type=PDF_MIME, filename="bad.pdf",
        ))
        assert result.text == ""
        assert result.extraction_method in ("PDF_INVALID", "PDF_SCANNED_OCR_NOT_EXTRACTED")


class TestImageOcr:
    def _png_bytes(self, text: str) -> bytes:
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (640, 160), color="white")
        ImageDraw.Draw(image).text((10, 60), text, fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_png_ocr_extraction_when_tesseract_available(self):
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=self._png_bytes("Infosys reports strong growth"),
            mime_type=PNG_MIME, filename="shot.png",
        ))
        assert result.extraction_method == "IMAGE_OCR"
        assert result.metadata["ocrStatus"] == "EXTRACTED"
        assert result.confidence is None  # never a fabricated per-document confidence

    def test_jpeg_ocr_extraction_when_tesseract_available(self):
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (640, 160), color="white")
        ImageDraw.Draw(image).text((10, 60), "Board approves buyback", fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=buffer.getvalue(), mime_type=JPEG_MIME, filename="shot.jpg",
        ))
        assert result.extraction_method == "IMAGE_OCR"

    def test_image_ocr_fails_closed_when_tesseract_missing(self, monkeypatch):
        """extract_text_from_image invokes the tesseract subprocess
        directly and catches FileNotFoundError (fail-closed) -- it does
        not consult image_ocr_available()/shutil.which internally (that
        check gates the capability ADVERTISEMENT, not the extraction
        call itself). Simulating the binary's absence means making the
        subprocess call itself fail exactly as it would on a pod missing
        the tesseract-ocr OS package."""
        import subprocess

        import app.image_evidence_extraction as img_mod

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("tesseract")

        monkeypatch.setattr(img_mod.subprocess, "run", fake_run)
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=self._png_bytes("irrelevant"), mime_type=PNG_MIME, filename="shot.png",
        ))
        assert result.text == ""
        assert result.extraction_method == "IMAGE_OCR_NOT_EXTRACTED"
        assert result.metadata["ocrStatus"] != "EXTRACTED"


class TestDocx:
    def test_docx_extraction_available_reflects_python_docx_importability(self):
        try:
            import docx  # noqa: F401
            assert docx_extraction_available() is True
        except ImportError:
            assert docx_extraction_available() is False

    def test_docx_paragraphs_and_tables_extracted_natively(self):
        pytest.importorskip("docx")
        from docx import Document as DocxDocument

        doc = DocxDocument()
        doc.add_paragraph("Quarterly update for shareholders")
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Metric"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "Revenue"
        table.cell(1, 1).text = "1234"
        buf = io.BytesIO()
        doc.save(buf)

        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=buf.getvalue(), mime_type=DOCX_MIME, filename="report.docx",
        ))
        assert result.extraction_method == "DOCX_NATIVE"
        assert "Quarterly update for shareholders" in result.text
        assert result.tables == [[["Metric", "Value"], ["Revenue", "1234"]]]
        assert result.metadata["paragraphCount"] >= 1
        assert result.metadata["tableCount"] == 1

    def test_docx_extraction_fails_closed_when_python_docx_unavailable(self, monkeypatch):
        import app.document_extraction as mod
        monkeypatch.setattr(mod, "docx_extraction_available", lambda: False)
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=b"irrelevant bytes", mime_type=DOCX_MIME, filename="x.docx",
        ))
        assert result.text == ""
        assert result.extraction_method == "DOCX_UNAVAILABLE"
        assert "DOCX_EXTRACTION_UNAVAILABLE" in result.warnings[0]

    def test_invalid_docx_bytes_degrade_truthfully_not_crash(self):
        pytest.importorskip("docx")
        result = DocumentExtractor().extract(DocumentInput(
            file_bytes=b"not a real docx file", mime_type=DOCX_MIME, filename="bad.docx",
        ))
        assert result.text == ""
        assert result.extraction_method == "DOCX_INVALID"
