"""Fail-closed runtime OCR capability gating (runtime container blocker).

A real deployment was found to be missing the tesseract/poppler-utils OS
packages in its research-engine image, even though the source-level OCR
code path existed and the Dockerfile has since been fixed to install them.
These tests guard the *other* half of that incident: the manual-evidence
capability advertisement (`/api/v1/research/evidence/file-types`) must
never claim PNG/JPEG/scanned-PDF support is available when the binaries
it depends on are not actually present on PATH, in any future
misconfigured deployment.

All gating here is a lightweight ``shutil.which`` existence check --
never an actual OCR invocation -- so these tests monkeypatch
``shutil.which`` rather than uninstalling real binaries.
"""
from __future__ import annotations

from app import image_evidence_extraction as ocr_mod
from app import manual_evidence


def _which_only(*allowed: str):
    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in allowed else None
    return fake_which


class TestOcrAvailabilityChecks:
    def test_image_ocr_available_when_tesseract_present(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("tesseract", "pdftoppm", "pdfinfo"))
        assert ocr_mod.image_ocr_available() is True

    def test_image_ocr_unavailable_when_tesseract_missing(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("pdftoppm", "pdfinfo"))
        assert ocr_mod.image_ocr_available() is False

    def test_scanned_pdf_ocr_available_when_all_three_present(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("tesseract", "pdftoppm", "pdfinfo"))
        assert ocr_mod.scanned_pdf_ocr_available() is True

    def test_scanned_pdf_ocr_unavailable_when_poppler_missing(self, monkeypatch):
        # tesseract present, but pdftoppm/pdfinfo (poppler-utils) missing.
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("tesseract"))
        assert ocr_mod.scanned_pdf_ocr_available() is False

    def test_scanned_pdf_ocr_unavailable_when_tesseract_missing(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("pdftoppm", "pdfinfo"))
        assert ocr_mod.scanned_pdf_ocr_available() is False


class TestRuntimeSupportedFileTypeLabels:
    def test_all_binaries_present_advertises_png_jpg(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("tesseract", "pdftoppm", "pdfinfo"))
        labels = manual_evidence.runtime_supported_file_type_labels()
        assert set(labels) == {"PDF", "CSV", "TXT", "PNG", "JPG"}

    def test_tesseract_missing_drops_png_and_jpg_only(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only())
        labels = manual_evidence.runtime_supported_file_type_labels()
        assert "PNG" not in labels
        assert "JPG" not in labels
        # Ordinary text PDF/CSV/TXT extraction has no OCR dependency and
        # must remain advertised regardless of tesseract availability.
        assert set(labels) == {"PDF", "CSV", "TXT"}

    def test_poppler_missing_but_tesseract_present_still_advertises_png_jpg(self, monkeypatch):
        # PNG/JPEG OCR only needs tesseract; poppler-utils is only needed
        # for the scanned-PDF fallback, which has no distinct capability
        # label of its own in supportedFileTypes (PDF already covers the
        # native-text-layer case). Confirm the two dependencies are not
        # conflated.
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("tesseract"))
        labels = manual_evidence.runtime_supported_file_type_labels()
        assert "PNG" in labels
        assert "JPG" in labels


class TestFileTypesEndpointRuntimeGating:
    def _get(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app).get("/api/v1/research/evidence/file-types")

    def test_all_binaries_present(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("tesseract", "pdftoppm", "pdfinfo"))
        body = self._get().json()
        assert set(body["supportedFileTypes"]) == {"CSV", "PDF", "TXT", "PNG", "JPG"}
        assert body["ocrCapability"] == {"imageOcrAvailable": True, "scannedPdfOcrAvailable": True}

    def test_tesseract_missing(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only())
        body = self._get().json()
        assert "PNG" not in body["supportedFileTypes"]
        assert "JPG" not in body["supportedFileTypes"]
        assert set(body["supportedFileTypes"]) == {"CSV", "PDF", "TXT"}
        assert body["ocrCapability"] == {"imageOcrAvailable": False, "scannedPdfOcrAvailable": False}

    def test_poppler_missing_only(self, monkeypatch):
        monkeypatch.setattr(ocr_mod.shutil, "which", _which_only("tesseract"))
        body = self._get().json()
        # PNG/JPEG OCR is unaffected by a missing poppler-utils.
        assert "PNG" in body["supportedFileTypes"]
        assert "JPG" in body["supportedFileTypes"]
        assert body["ocrCapability"] == {"imageOcrAvailable": True, "scannedPdfOcrAvailable": False}

    def test_ordinary_text_formats_always_advertised(self, monkeypatch):
        # Regardless of OCR binary availability, CSV/TXT/PDF (native text
        # extraction, no OS-binary dependency) must never be gated.
        for allowed in [(), ("tesseract",), ("tesseract", "pdftoppm", "pdfinfo")]:
            monkeypatch.setattr(ocr_mod.shutil, "which", _which_only(*allowed))
            body = self._get().json()
            assert {"CSV", "PDF", "TXT"}.issubset(set(body["supportedFileTypes"]))


class TestMissingBinaryFailsClosedNotCrashed:
    """Defense-in-depth: even if a direct API call bypasses the UI's
    capability gating, a missing OCR binary must produce a truthful
    rejection result, never an unhandled FileNotFoundError."""

    def test_tesseract_missing_image_extraction_fails_closed(self, monkeypatch):
        import subprocess as subprocess_mod

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "tesseract":
                raise FileNotFoundError("tesseract not found")
            return subprocess_mod.run(cmd, *args, **kwargs)

        monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)

        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buf, format="PNG")

        result = ocr_mod.extract_text_from_image(buf.getvalue(), "image/png")
        assert result.status == "FAILED"
        assert result.reason == "OCR_UNAVAILABLE"

    def test_pdfinfo_missing_scanned_pdf_extraction_fails_closed(self, monkeypatch):
        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "pdfinfo":
                raise FileNotFoundError("pdfinfo not found")
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)

        result = ocr_mod.extract_text_from_scanned_pdf(b"%PDF-1.4\n...not a real pdf...")
        assert result.status == "REJECTED"
        assert result.reason == "OCR_UNAVAILABLE"
