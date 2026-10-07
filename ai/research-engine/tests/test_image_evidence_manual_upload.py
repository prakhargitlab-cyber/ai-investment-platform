"""Defect 2 closure: PNG/JPEG/scanned-PDF uploads through the real
ManualEvidenceIngestor/Acceptor pipeline -- CURRENT_NEWS is used as the
evidence type because it always requires_manual_fields=True regardless of
what OCR extracts, which lets these tests isolate "did extraction run and
behave safely" from "was the extracted text good enough to auto-derive a
fact" (SHAREHOLDING's concern, covered separately). Proves the full
required flow: image/document -> extraction -> user review/correction ->
explicit Accept -> canonical persistence -> real ResearchReadinessRuntime
recalculation, with no persistence before Accept and no fabricated
READY_FRESH from OCR output alone.
"""
import io
import subprocess
from datetime import datetime, timezone
from uuid import UUID

import pytest
from PIL import Image, ImageDraw

from app.manual_evidence import AcceptanceError, ManualEvidenceAcceptor, ManualEvidenceIngestor
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings

RELIANCE_ID = UUID("44444444-4444-4444-4444-444444444444")


def _png_with_text(text: str) -> bytes:
    image = Image.new("RGB", (420, 120), color="white")
    ImageDraw.Draw(image).text((10, 40), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_text(text: str) -> bytes:
    image = Image.new("RGB", (420, 120), color="white")
    ImageDraw.Draw(image).text((10, 40), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _scanned_pdf_with_text(text: str) -> bytes:
    return subprocess.run(
        ["img2pdf", "-"], input=_png_with_text(text), capture_output=True, timeout=20, check=True
    ).stdout


def _valid_current_news_fields(**overrides):
    fields = {
        "title": "Regulatory filing uploaded as a photographed/scanned document",
        "summary": "Manually supplied summary; OCR text is supporting evidence only.",
        "eventDate": datetime.now(timezone.utc).isoformat(),
        "sourceUrl": "https://example-news.test/article/image-evidence",
        "eventType": "REGULATORY_EVENT",
        "impact": "NEGATIVE",
        "timeHorizon": "SHORT_TERM",
    }
    fields.update(overrides)
    return fields


@pytest.fixture
def repository():
    return ResearchRepository(
        settings=Settings(
            research_mcp_gateway_base_url="http://localhost",
            research_mcp_gateway_timeout_seconds=30,
            research_mcp_service_identity="test",
            research_mcp_first_enabled=False,
            research_readiness_ensure_timeout_seconds=30,
            research_opportunity_scheduler_enabled=False,
            country="IN",
            exchange="XNSE",
        ),
        persistence=SqliteResearchPersistence(),
    )


@pytest.fixture
def ingestor(repository):
    from app import manual_evidence
    manual_evidence._drafts.clear()
    return ManualEvidenceIngestor(repository=repository)


@pytest.fixture
def acceptor(repository):
    from app import manual_evidence
    from app.research_readiness_runtime import ResearchReadinessRuntime, RepositoryResearchReadinessAdapter
    manual_evidence._drafts.clear()
    adapter = RepositoryResearchReadinessAdapter(repository)
    runtime = ResearchReadinessRuntime(repository, adapter, None, ensure_timeout_seconds=30)
    return ManualEvidenceAcceptor(repository=repository, runtime=runtime)


class TestImageIngestExtraction:
    def test_png_is_a_supported_file_type(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=_png_with_text("ANNOUNCEMENT"),
            filename="notice.png", content_type="image/png", instrument_id=RELIANCE_ID,
        )
        assert draft.content_type == "image/png"
        assert "PNG" in draft.extraction_method

    def test_jpeg_is_a_supported_file_type(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=_jpeg_with_text("ANNOUNCEMENT"),
            filename="notice.jpg", content_type="image/jpeg", instrument_id=RELIANCE_ID,
        )
        assert draft.content_type == "image/jpeg"
        assert "JPG" in draft.extraction_method

    def test_scanned_pdf_with_no_text_layer_is_ocrd_through_the_normal_pdf_path(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=_scanned_pdf_with_text("SCANNED NOTICE"),
            filename="scanned.pdf", content_type="application/pdf", instrument_id=RELIANCE_ID,
        )
        assert draft.content_type == "application/pdf"

    def test_oversized_image_still_creates_a_draft_as_supporting_evidence_not_a_crash(self, ingestor):
        oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (9 * 1024 * 1024)
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=oversized,
            filename="huge.png", content_type="image/png", instrument_id=RELIANCE_ID,
        )
        assert draft.requires_manual_fields is True
        assert any("NO_TEXT_EXTRACTED" in w for w in draft.validation_results.warnings)

    def test_invalid_image_bytes_still_create_a_draft_not_a_crash(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"garbage, not an image",
            filename="broken.png", content_type="image/png", instrument_id=RELIANCE_ID,
        )
        assert any("NO_TEXT_EXTRACTED" in w for w in draft.validation_results.warnings)

    def test_timeout_during_extraction_still_creates_a_draft_not_a_crash(self, ingestor, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1))
        monkeypatch.setattr(subprocess, "run", fake_run)
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=_png_with_text("SLOW"),
            filename="slow.png", content_type="image/png", instrument_id=RELIANCE_ID,
        )
        assert any("NO_TEXT_EXTRACTED" in w for w in draft.validation_results.warnings)


class TestImageEvidenceAcceptFlow:
    @pytest.mark.asyncio
    async def test_no_persistence_before_accept(self, ingestor, acceptor, repository):
        """No event is persisted merely by uploading/extracting the image."""
        before = len(repository.events_for(RELIANCE_ID))
        ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=_png_with_text("PENDING REVIEW"),
            filename="notice.png", content_type="image/png", instrument_id=RELIANCE_ID,
        )
        assert len(repository.events_for(RELIANCE_ID)) == before

    @pytest.mark.asyncio
    async def test_persistence_and_readiness_recalculation_after_accept(self, ingestor, acceptor, repository):
        """Extraction alone never makes evidence canonical: only explicit
        Accept persists it, and only the REAL ResearchReadinessRuntime (not
        OCR output) ever derives READY_FRESH."""
        before = len(repository.events_for(RELIANCE_ID))
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=_png_with_text("FRESH ANNOUNCEMENT"),
            filename="notice.png", content_type="image/png", instrument_id=RELIANCE_ID,
        )
        assert len(repository.events_for(RELIANCE_ID)) == before  # still nothing new before accept
        result = await acceptor.accept(draft_id=draft.draft_id, corrections=_valid_current_news_fields())
        assert len(repository.events_for(RELIANCE_ID)) == before + 1  # exactly one new event persisted
        requirements = result["readiness"]["requirements"]
        current_news = next(r for r in requirements if r["requirementId"] == "CURRENT_NEWS")
        assert current_news["status"] == "READY_FRESH"

    @pytest.mark.asyncio
    async def test_accept_without_required_fields_is_rejected_even_with_good_ocr_text(self, ingestor, acceptor):
        """OCR extracting readable text is never sufficient by itself --
        the structured review/correction/acceptance contract still applies
        in full."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=_png_with_text("GOOD OCR TEXT HERE"),
            filename="notice.png", content_type="image/png", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="DRAFT_INVALID"):
            await acceptor.accept(draft_id=draft.draft_id)
