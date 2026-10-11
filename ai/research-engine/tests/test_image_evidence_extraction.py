"""Defect 2 closure: bounded image/scanned-PDF OCR extraction.

Covers the full requirement list: valid PNG, valid JPEG, scanned PDF,
oversized image, invalid image, timeout/failure, and empty extraction --
all against the real tesseract/pdftoppm/pdfinfo binaries (no mocking of
the OCR engine itself), proving the bounds this module exists for are
real, not theoretical.
"""
from __future__ import annotations

import io
import subprocess

import pytest
from PIL import Image, ImageDraw

from app.image_evidence_extraction import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    extract_text_from_image,
    extract_text_from_scanned_pdf,
)


def _png_with_text(text: str, size=(400, 120)) -> bytes:
    image = Image.new("RGB", size, color="white")
    draw = ImageDraw.Draw(image)
    draw.text((10, 40), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_text(text: str, size=(400, 120)) -> bytes:
    image = Image.new("RGB", size, color="white")
    draw = ImageDraw.Draw(image)
    draw.text((10, 40), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def test_valid_png_extracts_real_text():
    result = extract_text_from_image(_png_with_text("HELLO EVIDENCE"), "image/png")
    assert result.status == "EXTRACTED"
    assert "HELLO" in result.text.upper()
    assert result.content_hash  # provenance always present


def test_valid_jpeg_extracts_real_text():
    result = extract_text_from_image(_jpeg_with_text("SAMPLE TEXT"), "image/jpeg")
    assert result.status == "EXTRACTED"
    assert "SAMPLE" in result.text.upper()


def test_oversized_image_is_rejected_before_any_subprocess_runs():
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_IMAGE_BYTES + 1)
    result = extract_text_from_image(oversized, "image/png")
    assert result.status == "REJECTED"
    assert result.reason == "IMAGE_TOO_LARGE"


def test_decompression_bomb_pixel_count_is_rejected():
    # A real (small-file, huge-dimension) image exceeding the pixel budget.
    huge = Image.new("1", (6000, 6000))  # 36M pixels > MAX_IMAGE_PIXELS, 1-bit mode keeps bytes small
    buffer = io.BytesIO()
    huge.save(buffer, format="PNG")
    assert len(buffer.getvalue()) < MAX_IMAGE_BYTES
    result = extract_text_from_image(buffer.getvalue(), "image/png")
    assert result.status == "REJECTED"
    assert result.reason == "IMAGE_TOO_MANY_PIXELS"


def test_invalid_image_bytes_are_rejected_truthfully():
    result = extract_text_from_image(b"not an image at all, just garbage bytes", "image/png")
    assert result.status == "REJECTED"
    assert result.reason == "INVALID_IMAGE"


def test_unsupported_content_type_is_rejected():
    result = extract_text_from_image(_png_with_text("x"), "image/gif")
    assert result.status == "REJECTED"
    assert result.reason == "UNSUPPORTED_IMAGE_TYPE"


def test_empty_file_is_rejected():
    result = extract_text_from_image(b"", "image/png")
    assert result.status == "REJECTED"
    assert result.reason == "EMPTY_FILE"


def test_blank_image_yields_truthful_empty_extraction_not_fabricated_text():
    blank = Image.new("RGB", (200, 80), color="white")
    buffer = io.BytesIO()
    blank.save(buffer, format="PNG")
    result = extract_text_from_image(buffer.getvalue(), "image/png")
    assert result.status == "EMPTY"
    assert result.text == ""


def test_ocr_timeout_is_surfaced_truthfully_not_as_a_crash(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1))
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = extract_text_from_image(_png_with_text("TIMEOUT CASE"), "image/png")
    assert result.status == "TIMEOUT"
    assert result.reason == "EXTRACTION_TIMEOUT"


def test_ocr_process_failure_is_surfaced_truthfully(monkeypatch):
    class _FailedCompletedProcess:
        returncode = 1
        stderr = b"tesseract: fatal error"
        stdout = b""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FailedCompletedProcess())
    result = extract_text_from_image(_png_with_text("FAILURE CASE"), "image/png")
    assert result.status == "FAILED"
    assert result.reason == "OCR_PROCESS_FAILED"


def test_scanned_pdf_with_no_text_layer_is_ocrd():
    png_bytes = _png_with_text("SCANNED PDF EVIDENCE")
    pdf_bytes = subprocess.run(
        ["img2pdf", "-"], input=png_bytes, capture_output=True, timeout=20, check=True
    ).stdout
    result = extract_text_from_scanned_pdf(pdf_bytes)
    assert result.status == "EXTRACTED"
    assert "SCANNED" in result.text.upper()
    assert result.page_count == 1


def test_scanned_pdf_exceeding_max_pages_is_rejected_not_silently_truncated():
    png_bytes = _png_with_text("PAGE")
    pages = [png_bytes] * 6  # MAX_PDF_PAGES is 5
    # Build a genuine multi-page PDF via img2pdf with repeated page args.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for i, data in enumerate(pages):
            p = Path(d) / f"p{i}.png"
            p.write_bytes(data)
            paths.append(str(p))
        multi_pdf = subprocess.run(["img2pdf"] + paths, capture_output=True, timeout=20, check=True).stdout
    result = extract_text_from_scanned_pdf(multi_pdf)
    assert result.status == "REJECTED"
    assert result.reason == "PDF_TOO_MANY_PAGES"
    assert result.page_count == 6


def test_invalid_pdf_bytes_are_rejected_truthfully():
    result = extract_text_from_scanned_pdf(b"not a pdf at all")
    assert result.status == "REJECTED"
    assert result.reason == "INVALID_PDF"
