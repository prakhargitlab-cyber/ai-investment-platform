"""Bounded image/scanned-PDF text extraction for manual evidence uploads.

Architecture (Defect 2 closure): every OCR step runs as a separate OS
process (the pre-installed ``tesseract`` and ``pdftoppm`` binaries),
never as an in-process model or library call. This is deliberate: the
research-engine process has previously hit OOM failures under unbounded
work (see docs/architecture/radar-full-baseline-retention-20261007.md),
and this module must not be able to reproduce that failure mode.
A subprocess that runs too long is killed by ``timeout=`` without taking
the research-engine process down with it; a subprocess that allocates too
much memory affects only itself, not this process's heap.

The only new in-process Python dependency is Pillow, used solely to
validate/sniff untrusted image bytes (format, dimensions) BEFORE anything
is ever handed to a subprocess -- Pillow decodes image headers, it does
not run any model and has no unbounded-memory failure mode comparable to
an OCR/ML stack.

Bounds enforced, all before any subprocess runs:
  - file size (MAX_IMAGE_BYTES / MAX_PDF_BYTES)
  - decoded pixel count (MAX_IMAGE_PIXELS) -- guards against decompression
    bombs (a tiny file that decodes to an enormous bitmap)
  - page count for scanned PDFs (MAX_PDF_PAGES)
  - wall-clock time per extraction (EXTRACTION_TIMEOUT_SECONDS)

This module never executes uploaded content: tesseract/pdftoppm only ever
read image/PDF *pixels*, and all subprocess arguments are a fixed command
list (no shell=True, no string interpolation of user-controlled paths
beyond a path this module itself created in its own temporary directory).
Extracted text is sanitized (control characters stripped, length capped)
before being handed back. Provenance (sha256 of the original bytes) is
returned so the caller can tie extracted text back to the uploaded
artifact -- the same content_hash discipline the rest of
app.manual_evidence already uses.

Extraction alone never makes evidence canonical: the caller
(ManualEvidenceIngestor) threads this module's output through the exact
same draft -> user review/correction -> explicit accept() ->
canonical persistence -> ResearchReadinessRuntime recalculation pipeline
that PDF/CSV/TXT evidence already uses. Nothing here ever sets a
requirement to READY_FRESH directly.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024          # 8 MB
MAX_PDF_BYTES = 20 * 1024 * 1024           # 20 MB
MAX_IMAGE_PIXELS = 25_000_000              # ~25 megapixels (e.g. 5000x5000)
MAX_PDF_PAGES = 5                          # bounded scanned-PDF OCR scope
EXTRACTION_TIMEOUT_SECONDS = 20            # per OCR subprocess call
MAX_EXTRACTED_TEXT_CHARS = 20_000          # bounds downstream memory/storage

SUPPORTED_IMAGE_MIME_TYPES = ("image/png", "image/jpeg")


def image_ocr_available() -> bool:
    """Lightweight, no-subprocess-execution runtime capability check: is the
    ``tesseract`` binary this module's PNG/JPEG OCR path depends on actually
    present? This is a pure PATH lookup (``shutil.which``) -- it never runs
    tesseract, never touches any file, and costs nothing beyond a filesystem
    stat. Used to gate the manual-evidence capability advertisement so a
    deployment missing the tesseract-ocr OS package truthfully stops
    offering PNG/JPEG upload instead of accepting it and failing later."""
    return shutil.which("tesseract") is not None


def scanned_pdf_ocr_available() -> bool:
    """Same lightweight PATH-only check as image_ocr_available(), but for
    the scanned-PDF OCR fallback, which additionally needs the poppler-utils
    binaries pdftoppm (rasterization) and pdfinfo (page-count bounds check)."""
    return (
        shutil.which("tesseract") is not None
        and shutil.which("pdftoppm") is not None
        and shutil.which("pdfinfo") is not None
    )


@dataclass
class ImageExtractionResult:
    status: str  # "EXTRACTED" | "REJECTED" | "TIMEOUT" | "FAILED" | "EMPTY"
    text: str | None
    reason: str | None
    content_hash: str
    page_count: int | None = field(default=None)


def _sanitize_text(raw: str) -> str:
    """Strip control characters (keep newlines/tabs) and cap length.

    Never executed, never interpreted -- this is plain text handed into
    the same review/correction UI that PDF/CSV/TXT extraction already
    populates.
    """
    cleaned = "".join(ch for ch in raw if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127))
    return cleaned[:MAX_EXTRACTED_TEXT_CHARS]


def _run_tesseract(image_path: Path, workdir: Path) -> tuple[str | None, str | None]:
    """Run the tesseract CLI against one bounded, already-validated image
    file. Returns (text, failure_reason); failure_reason is None on
    success (including a legitimate empty result)."""
    output_base = workdir / "ocr_out"
    try:
        result = subprocess.run(
            ["tesseract", str(image_path), str(output_base), "--psm", "6"],
            timeout=EXTRACTION_TIMEOUT_SECONDS,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except FileNotFoundError:
        # tesseract is not installed in this runtime image. This is a
        # truthful, non-crashing rejection -- the capability endpoint
        # (image_ocr_available/scanned_pdf_ocr_available) is what should
        # normally keep the UI from ever reaching this path when the
        # binary is missing, but a direct API call must still fail closed
        # rather than raise an unhandled exception.
        logger.warning("tesseract_extraction_unavailable reason=binary_not_found")
        return None, "OCR_UNAVAILABLE"
    if result.returncode != 0:
        logger.warning("tesseract_extraction_failed returncode=%s stderr=%s",
                        result.returncode, result.stderr.decode("utf-8", errors="replace")[:500])
        return None, "OCR_PROCESS_FAILED"
    output_path = output_base.with_suffix(".txt")
    if not output_path.exists():
        return None, "OCR_PROCESS_FAILED"
    text = output_path.read_text(encoding="utf-8", errors="replace")
    return text, None


def extract_text_from_image(file_bytes: bytes, content_type: str, filename: str | None = None) -> ImageExtractionResult:
    """Bounded OCR extraction for a single PNG/JPEG image."""
    digest = hashlib.sha256(file_bytes).hexdigest()
    if content_type not in SUPPORTED_IMAGE_MIME_TYPES:
        return ImageExtractionResult("REJECTED", None, "UNSUPPORTED_IMAGE_TYPE", digest)
    if len(file_bytes) > MAX_IMAGE_BYTES:
        return ImageExtractionResult("REJECTED", None, "IMAGE_TOO_LARGE", digest)
    if len(file_bytes) == 0:
        return ImageExtractionResult("REJECTED", None, "EMPTY_FILE", digest)

    try:
        from PIL import Image
    except ImportError:
        logger.warning("image_extraction_unavailable reason=Pillow_not_installed")
        return ImageExtractionResult("REJECTED", None, "IMAGE_EXTRACTION_UNAVAILABLE", digest)

    import io
    try:
        with Image.open(io.BytesIO(file_bytes)) as probe:
            probe.verify()  # raises on a corrupt/non-image file; never decodes pixel data
        with Image.open(io.BytesIO(file_bytes)) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                return ImageExtractionResult("REJECTED", None, "IMAGE_TOO_MANY_PIXELS", digest)
            if image.format not in ("PNG", "JPEG"):
                return ImageExtractionResult("REJECTED", None, "INVALID_IMAGE", digest)
            with tempfile.TemporaryDirectory(prefix="aip-evidence-ocr-") as workdir_name:
                workdir = Path(workdir_name)
                normalized_path = workdir / "input.png"
                # Re-encode via Pillow rather than writing the raw uploaded
                # bytes to disk: this guarantees tesseract only ever reads a
                # file this process itself produced from already-validated
                # pixel data, never the untrusted bytes directly.
                image.convert("L").save(normalized_path, format="PNG")
                text, failure = _run_tesseract(normalized_path, workdir)
    except Exception as exc:  # noqa: BLE001 -- any decode failure is a truthful rejection, never a crash
        # Bounded diagnostics only (runtime defect closure: clipboard-paste
        # transport tracing) -- filename, declared MIME, byte length, and
        # the first 8 bytes' hex signature (a real PNG is always
        # 89 50 4E 47 0D 0A 1A 0A; a real JPEG always starts FF D8) are
        # exactly enough to tell "these are genuinely not image bytes"
        # apart from "these bytes were corrupted/truncated/re-encoded
        # somewhere upstream of this process" on the next occurrence,
        # without ever logging raw file bytes, base64, or any private
        # screenshot content.
        logger.warning(
            "image_validation_failed reason=%s filename=%s declaredMime=%s byteLength=%s "
            "firstEightBytesHex=%s contentHash=%s",
            exc, filename or "unknown", content_type, len(file_bytes),
            file_bytes[:8].hex(), digest,
        )
        return ImageExtractionResult("REJECTED", None, "INVALID_IMAGE", digest)

    if failure == "TIMEOUT":
        return ImageExtractionResult("TIMEOUT", None, "EXTRACTION_TIMEOUT", digest)
    if failure is not None:
        return ImageExtractionResult("FAILED", None, failure, digest)
    sanitized = _sanitize_text(text or "")
    if not sanitized.strip():
        return ImageExtractionResult("EMPTY", "", None, digest)
    return ImageExtractionResult("EXTRACTED", sanitized, None, digest)


def extract_text_from_scanned_pdf(file_bytes: bytes) -> ImageExtractionResult:
    """Bounded OCR fallback for a scanned PDF (no extractable text layer).

    Rasterizes at most MAX_PDF_PAGES pages via the pre-installed pdftoppm
    binary, OCRs each page image with the same bounded tesseract path as
    extract_text_from_image, and concatenates the results. Never attempts
    to OCR a PDF with more than MAX_PDF_PAGES pages -- it rejects instead
    of silently truncating without telling the caller.
    """
    digest = hashlib.sha256(file_bytes).hexdigest()
    if len(file_bytes) > MAX_PDF_BYTES:
        return ImageExtractionResult("REJECTED", None, "PDF_TOO_LARGE", digest)
    if len(file_bytes) == 0:
        return ImageExtractionResult("REJECTED", None, "EMPTY_FILE", digest)

    with tempfile.TemporaryDirectory(prefix="aip-evidence-pdf-ocr-") as workdir_name:
        workdir = Path(workdir_name)
        pdf_path = workdir / "input.pdf"
        pdf_path.write_bytes(file_bytes)

        try:
            info = subprocess.run(
                ["pdfinfo", str(pdf_path)],
                timeout=EXTRACTION_TIMEOUT_SECONDS, capture_output=True, check=False,
            )
        except subprocess.TimeoutExpired:
            return ImageExtractionResult("TIMEOUT", None, "EXTRACTION_TIMEOUT", digest)
        except FileNotFoundError:
            logger.warning("scanned_pdf_extraction_unavailable reason=pdfinfo_not_found")
            return ImageExtractionResult("REJECTED", None, "OCR_UNAVAILABLE", digest)
        if info.returncode != 0:
            return ImageExtractionResult("REJECTED", None, "INVALID_PDF", digest)
        page_count = None
        for line in info.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("Pages:"):
                try:
                    page_count = int(line.split(":", 1)[1].strip())
                except ValueError:
                    page_count = None
        if page_count is None:
            return ImageExtractionResult("REJECTED", None, "INVALID_PDF", digest)
        if page_count > MAX_PDF_PAGES:
            return ImageExtractionResult("REJECTED", None, "PDF_TOO_MANY_PAGES", digest, page_count=page_count)

        image_prefix = workdir / "page"
        try:
            rasterize = subprocess.run(
                ["pdftoppm", "-png", "-r", "200", str(pdf_path), str(image_prefix)],
                timeout=EXTRACTION_TIMEOUT_SECONDS, capture_output=True, check=False,
            )
        except subprocess.TimeoutExpired:
            return ImageExtractionResult("TIMEOUT", None, "EXTRACTION_TIMEOUT", digest, page_count=page_count)
        except FileNotFoundError:
            logger.warning("scanned_pdf_extraction_unavailable reason=pdftoppm_not_found")
            return ImageExtractionResult("REJECTED", None, "OCR_UNAVAILABLE", digest, page_count=page_count)
        if rasterize.returncode != 0:
            return ImageExtractionResult("FAILED", None, "PDF_RASTERIZE_FAILED", digest, page_count=page_count)

        page_texts: list[str] = []
        for page_image in sorted(workdir.glob("page-*.png")):
            text, failure = _run_tesseract(page_image, workdir)
            if failure == "TIMEOUT":
                return ImageExtractionResult("TIMEOUT", None, "EXTRACTION_TIMEOUT", digest, page_count=page_count)
            if failure is not None:
                return ImageExtractionResult("FAILED", None, failure, digest, page_count=page_count)
            page_texts.append(text or "")

    sanitized = _sanitize_text("\n\n".join(page_texts))
    if not sanitized.strip():
        return ImageExtractionResult("EMPTY", "", None, digest, page_count=page_count)
    return ImageExtractionResult("EXTRACTED", sanitized, None, digest, page_count=page_count)
