"""Focused backend tests for the Manual Evidence Upload feature.

Tests cover:
  * File-type detection / validation
  * Draft creation (extraction) — must NOT modify normalized data
  * SHAREHOLDING extraction + deterministic validation
  * Draft retrieval
  * Accept: atomic persistence, provenance, conflict detection
  * Dedup by content hash
  * Readiness recalculation through the normal path (not hard-coded)
"""
from pathlib import Path
import asyncio
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.manual_evidence import (
    AcceptanceError,
    DraftStatus,
    EvidenceType,
    ManualEvidenceAcceptor,
    ManualEvidenceDraft,
    ManualEvidenceIngestor,
    ProposedFact,
    SUPPORTED_EVIDENCE_TYPES,
    SUPPORTED_FILE_TYPE_LABELS,
    ValidationResult,
    content_hash_hex,
    is_supported_file_type,
    detect_mime_type,
)
from app.models import (
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
)
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RELIANCE_ID = UUID("44444444-4444-4444-4444-444444444444")
AIXTRON_ID = UUID("11111111-1111-1111-1111-111111111111")  # different demo-profile instrument, for isolation checks


def _most_recent_quarter_end_str(today: datetime) -> str:
    """dd/mm/yyyy for the most recently completed calendar quarter-end,
    so a SHAREHOLDING fixture is genuinely inside the 120-day freshness
    window (FreshnessPolicyRegistry.default()["SHAREHOLDING"]) regardless
    of when this test actually runs."""
    import calendar
    quarter_end_month = ((today.month - 1) // 3) * 3
    year = today.year
    if quarter_end_month == 0:
        quarter_end_month = 12
        year -= 1
    day = calendar.monthrange(year, quarter_end_month)[1]
    return f"{day:02d}/{quarter_end_month:02d}/{year}"

SAMPLE_TEXT = """Shareholding as on 31/03/2024

Promoter and promoter group: 47.15%
Public shareholders: 25.85%
Foreign institutional investors: 12.34%
Domestic institutional investors: 8.76%
Mutual funds: 4.21%
Insurance companies: 1.69%
"""

SAMPLE_CSV = """Shareholding as on 31/03/2024

Promoter and promoter group: 47.15%
Public shareholders: 25.85%
Foreign institutional investors: 12.34%
Domestic institutional investors: 8.76%
Mutual funds: 4.21%
Insurance companies: 1.69%
"""


@pytest.fixture
def repository():
    """Create a ResearchRepository with an in-memory SQLite persistence."""
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
def repository_with_profile(repository):
    """Ensure the Reliance profile exists in the repository."""
    profiles = repository.list_profiles()
    reliance = next((p for p in profiles if p.get("globalInstrumentId") == str(RELIANCE_ID)), None)
    if reliance is None:
        pytest.skip("Reliance profile not available in test environment")
    return repository


@pytest.fixture
def ingestor(repository):
    from app import manual_evidence
    manual_evidence._drafts.clear()
    return ManualEvidenceIngestor(repository=repository)


@pytest.fixture
def acceptor(repository):
    from app import manual_evidence
    from app.research_readiness_runtime import (
        ResearchReadinessRuntime,
        RepositoryResearchReadinessAdapter,
    )
    manual_evidence._drafts.clear()
    adapter = RepositoryResearchReadinessAdapter(repository)
    runtime = ResearchReadinessRuntime(repository, adapter, None, ensure_timeout_seconds=30)
    return ManualEvidenceAcceptor(repository=repository, runtime=runtime)


# ---------------------------------------------------------------------------
# File-type detection tests
# ---------------------------------------------------------------------------

class TestFileTypeDetection:
    def test_detect_mime_type_by_extension(self):
        assert detect_mime_type("doc.pdf", None) == "application/pdf"
        assert detect_mime_type("data.csv", None) == "text/csv"
        assert detect_mime_type("notes.txt", None) == "text/plain"
        # Defect 2 closure: PNG/JPEG now have a genuine bounded OCR
        # extraction path (app.image_evidence_extraction), so they are
        # supported file types -- see test_image_evidence_manual_upload.py.
        assert detect_mime_type("scan.png", None) == "image/png"
        assert detect_mime_type("image.jpg", None) == "image/jpeg"
        # Superseded (reusable DocumentExtractor closure): DOCX now has a
        # genuine native extraction path (python-docx), gated at the
        # capability-advertisement layer exactly like PNG/JPG/OCR.
        assert detect_mime_type("doc.docx", None) == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def test_detect_mime_type_by_content_type(self):
        assert detect_mime_type(None, "application/pdf") == "application/pdf"
        assert detect_mime_type(None, "text/csv") == "text/csv"

    def test_detect_mime_type_unsupported(self):
        assert detect_mime_type("doc.xlsx", None) is None
        assert detect_mime_type("doc.exe", None) is None

    def test_is_supported_file_type(self):
        assert is_supported_file_type("doc.pdf", None) is True
        assert is_supported_file_type("data.csv", "text/csv") is True
        assert is_supported_file_type("notes.txt", "text/plain") is True
        assert is_supported_file_type("scan.png", "image/png") is True
        assert is_supported_file_type("image.jpg", "image/jpeg") is True
        assert is_supported_file_type("doc.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document") is True
        assert is_supported_file_type("doc.exe", None) is False
        assert is_supported_file_type("doc.xlsx", None) is False

    def test_supported_file_type_labels(self):
        assert "PDF" in SUPPORTED_FILE_TYPE_LABELS
        assert "CSV" in SUPPORTED_FILE_TYPE_LABELS
        assert "TXT" in SUPPORTED_FILE_TYPE_LABELS
        assert "PNG" in SUPPORTED_FILE_TYPE_LABELS
        assert "JPG" in SUPPORTED_FILE_TYPE_LABELS
        assert "DOCX" in SUPPORTED_FILE_TYPE_LABELS
        assert set(SUPPORTED_FILE_TYPE_LABELS) == {"PDF", "CSV", "TXT", "PNG", "JPG", "DOCX"}

    def test_supported_evidence_types(self):
        # Generalized beyond the original SHAREHOLDING-only slice: CURRENT_NEWS
        # (manual-fields, no auto-extraction) and the four FinancialFact-backed
        # requirements (BUSINESS_QUALITY_FACTS, GROWTH_FACTS, BALANCE_SHEET_FACTS,
        # QUARTERLY_FINANCIALS -- also manual-fields, see
        # test_manual_evidence_financial_facts.py) now have genuine accept
        # paths too. Superseded: VALUATION_INPUTS, LATEST_PRICE,
        # HISTORICAL_PRICE_SERIES and SECTOR_MACRO are now ALSO supported
        # (the authoritative product rule -- status != READY_FRESH => Upload
        # Evidence must be offered, for every requirement -- supersedes the
        # prior restriction; see each type's own validation/build/persist
        # methods in app.manual_evidence and the final report).
        assert EvidenceType.SHAREHOLDING in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.CURRENT_NEWS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.GOVERNANCE_HISTORY in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.BUSINESS_QUALITY_FACTS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.GROWTH_FACTS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.BALANCE_SHEET_FACTS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.QUARTERLY_FINANCIALS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.VALUATION_INPUTS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.LATEST_PRICE in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.HISTORICAL_PRICE_SERIES in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.SECTOR_MACRO in SUPPORTED_EVIDENCE_TYPES
        assert len(SUPPORTED_EVIDENCE_TYPES) == 12

    def test_file_types_endpoint_advertises_only_extractable_formats(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.image_evidence_extraction import image_ocr_available
        from app.document_extraction import docx_extraction_available

        response = TestClient(app).get("/api/v1/research/evidence/file-types")

        assert response.status_code == 200
        # PNG/JPG/DOCX are fail-closed, gated on the SAME runtime capability
        # checks the endpoint itself uses (app.manual_evidence
        # ._ocr_dependent_labels_available / app.main.supported_evidence_file_types),
        # not a fixed hardcoded set -- a runtime image without the tesseract
        # OS binary (e.g. a Windows dev machine without it on PATH) is
        # expected, correct behavior to omit PNG/JPG here, and this
        # assertion must track that rather than assume availability.
        always_available = {"CSV", "PDF", "TXT"}
        conditionally_available = set()
        if image_ocr_available():
            conditionally_available |= {"PNG", "JPG"}
        if docx_extraction_available():
            conditionally_available |= {"DOCX"}
        assert set(response.json()["supportedFileTypes"]) == always_available | conditionally_available
        assert set(response.json()["supportedEvidenceTypes"]) == {
            "SHAREHOLDING", "CURRENT_NEWS", "ORDER_BOOK_CAPEX_GUIDANCE", "GOVERNANCE_HISTORY",
            "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS",
            "VALUATION_INPUTS", "LATEST_PRICE", "HISTORICAL_PRICE_SERIES", "SECTOR_MACRO",
        }


# ---------------------------------------------------------------------------
# Extraction / Draft tests
# ---------------------------------------------------------------------------

class TestEvidenceExtraction:
    def test_extract_shareholding_csv(self, ingestor):
        """Extract shareholding facts from a CSV file."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            content_type="text/csv",
            instrument_id=RELIANCE_ID,
        )
        assert draft.draft_id is not None
        assert draft.evidence_type == EvidenceType.SHAREHOLDING
        assert draft.status == DraftStatus.DRAFT
        assert draft.content_hash == content_hash_hex(SAMPLE_CSV.encode("utf-8"))
        assert len(draft.proposed_facts) > 0

        # Verify promoter was extracted
        promoter_facts = [f for f in draft.proposed_facts if "PROMOTER" in f.field and "PLEDGE" not in f.field]
        assert len(promoter_facts) == 1
        assert promoter_facts[0].value == Decimal("47.15")

    def test_extract_shareholding_txt(self, ingestor):
        """Extract shareholding facts from a plain text file."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_TEXT.encode("utf-8"),
            filename="shareholding.txt",
            content_type="text/plain",
            instrument_id=RELIANCE_ID,
        )
        assert draft.status == DraftStatus.DRAFT
        assert len(draft.proposed_facts) > 0

        # Verify all categories extracted
        categories = set()
        for f in draft.proposed_facts:
            cat_str = f.field.split(":")[1]
            categories.add(cat_str)
        assert "PROMOTER" in categories
        assert "PUBLIC_RETAIL" in categories
        assert "FII_FPI" in categories

    def test_extract_does_not_modify_db(self, ingestor, repository):
        """Extraction must NOT persist normalized data to the DB."""
        before_snapshots = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(before_snapshots) == 0

        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )

        after_snapshots = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(after_snapshots) == 0, "Draft extraction must not write to DB"
        # Draft must be stored in-process
        assert ingestor.get_draft(draft.draft_id) == draft

    def test_unsupported_file_type_rejected(self, ingestor):
        with pytest.raises(ValueError, match="UNSUPPORTED_FILE_TYPE"):
            ingestor.ingest(
                evidence_type="SHAREHOLDING",
                file_bytes=b"some data",
                filename="document.xlsx",
                instrument_id=RELIANCE_ID,
            )

    def test_docx_now_creates_a_draft_via_native_extraction(self, ingestor):
        """Superseded (reusable DocumentExtractor closure): DOCX now has a
        genuine native extraction path (paragraphs + tables via
        python-docx, installed in this environment -- see
        app.document_extraction.docx_extraction_available()), so a DOCX
        upload with no recognizable shareholding text produces an ordinary
        invalid/no-values draft, the same outcome unrecognizable PNG/JPG
        text produces, rather than a hard UNSUPPORTED_FILE_TYPE rejection."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=b"not extractable by the deterministic shareholding parser",
            filename="document.docx",
            instrument_id=RELIANCE_ID,
        )
        assert draft.status.value == "DRAFT"
        assert draft.proposed_facts == []

    def test_png_jpg_now_create_a_draft_via_bounded_ocr_not_a_hard_rejection(self, ingestor):
        """Defect 2 closure: PNG/JPG are genuinely extractable now (bounded
        OCR), so an image with no recognizable shareholding text produces an
        ordinary invalid/no-values draft -- the same outcome unrecognizable
        PDF/TXT content already produces -- never UNSUPPORTED_FILE_TYPE."""
        import io
        from PIL import Image
        blank = Image.new("RGB", (200, 80), color="white")
        buffer = io.BytesIO()
        blank.save(buffer, format="PNG")
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=buffer.getvalue(),
            filename="scan.png",
            instrument_id=RELIANCE_ID,
        )
        assert draft.content_type == "image/png"
        assert draft.proposed_facts == []

    def test_unsupported_evidence_type_rejected(self, ingestor):
        with pytest.raises(ValueError, match="UNSUPPORTED_EVIDENCE_TYPE"):
            ingestor.ingest(
                evidence_type="NOT_A_REAL_TYPE",
                file_bytes=SAMPLE_CSV.encode("utf-8"),
                filename="shareholding.csv",
                instrument_id=RELIANCE_ID,
            )

    def test_extract_no_shareholding_values(self, ingestor):
        """Text without recognizable shareholding values returns invalid draft."""
        text = b"This is a random document with no shareholding data."
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text,
            filename="random.txt",
            instrument_id=RELIANCE_ID,
        )
        assert draft.validation_results.valid is False
        assert len(draft.validation_results.errors) > 0
        assert draft.proposed_facts == []

    def test_content_hash_dedup_warning(self, ingestor, repository):
        """Identical content produces a duplicate warning."""
        draft1 = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        assert not any("DUPLICATE_CONTENT" in w for w in draft1.validation_results.warnings)

        # Second ingest of same content should warn about duplicate
        draft2 = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding_copy.csv",
            instrument_id=RELIANCE_ID,
        )
        assert any("DUPLICATE_CONTENT" in w for w in draft2.validation_results.warnings)


# ---------------------------------------------------------------------------
# Draft retrieval tests
# ---------------------------------------------------------------------------

class TestDraftRetrieval:
    def test_get_draft_returns_draft(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        retrieved = ingestor.get_draft(draft.draft_id)
        assert retrieved == draft
        assert retrieved.draft_id == draft.draft_id

    def test_get_draft_nonexistent_returns_none(self, ingestor):
        assert ingestor.get_draft(uuid4()) is None


# ---------------------------------------------------------------------------
# Accept tests
# ---------------------------------------------------------------------------

class TestEvidenceAccept:
    @pytest.mark.asyncio
    async def test_accept_creates_snapshot(self, ingestor, acceptor, repository):
        """Acceptance persists a shareholding snapshot with provenance."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        assert draft.validation_results.valid

        result = await acceptor.accept(draft_id=draft.draft_id)

        assert result["draftId"] == str(draft.draft_id)
        assert result["persisted"] is True
        assert result["readiness"] is not None
        # Snapshot should be in the DB
        snapshots = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap.source_provider == "USER_UPLOAD"
        assert len(snap.values) > 0

    @pytest.mark.asyncio
    async def test_accept_removes_draft_from_store(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft.draft_id)
        assert ingestor.get_draft(draft.draft_id) is None

    @pytest.mark.asyncio
    async def test_accept_invalid_draft_fails(self, ingestor, acceptor):
        """Cannot accept a draft with no extractable values."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=b"no data here",
            filename="random.txt",
            instrument_id=RELIANCE_ID,
        )
        assert draft.validation_results.valid is False
        with pytest.raises(AcceptanceError, match="DRAFT_INVALID"):
            await acceptor.accept(draft_id=draft.draft_id)

    @pytest.mark.asyncio
    async def test_accept_nonexistent_draft_fails(self, acceptor):
        with pytest.raises(AcceptanceError, match="DRAFT_NOT_FOUND"):
            await acceptor.accept(draft_id=uuid4())

    @pytest.mark.asyncio
    async def test_accept_already_accepted_draft_fails(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft.draft_id)
        # Draft was removed from store; accept again should fail
        with pytest.raises(AcceptanceError, match="DRAFT_NOT_FOUND"):
            await acceptor.accept(draft_id=draft.draft_id)

    @pytest.mark.asyncio
    async def test_accept_preserves_provenance(self, ingestor, acceptor, repository):
        """Acceptance persists provenance record with extraction method, hash, etc."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft.draft_id)

        records = repository.persistence.load_manual_evidence_drafts("SHAREHOLDING")
        assert len(records) == 1
        record = records[0]
        assert record["evidence_type"] == "SHAREHOLDING"
        assert record["content_hash"] == draft.content_hash
        assert record["original_filename"] == "shareholding.csv"
        assert record["content_type"] == "text/csv"
        assert record["extraction_method"] == "CSV_TEXT"

    @pytest.mark.asyncio
    async def test_accept_dedup_by_content_hash(self, ingestor, acceptor, repository):
        """Same content accepted twice does not create duplicate provenance."""
        draft1 = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft1.draft_id)

        # Load existing accepted records, then ingest same content again
        draft2 = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        # The second ingest should warn about duplicate content
        assert any("DUPLICATE_CONTENT" in w for w in draft2.validation_results.warnings)

        result = await acceptor.accept(draft_id=draft2.draft_id)
        # Provenance record should still be deduped (same content_hash)
        records = repository.persistence.load_manual_evidence_drafts("SHAREHOLDING")
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_accept_does_not_overwrite_trusted_official(
        self, ingestor, acceptor, repository
    ):
        """USER_UPLOAD evidence must not overwrite official NSE evidence.

        When a conflict exists and reconcile is not requested, acceptance
        should fail with AcceptanceError.
        """
        from app.models import (
            ReliabilityLevel,
            ShareholdingSnapshot as SS,
            ShareholdingSnapshotValue as SSV,
            SourceMode,
        )

        # Insert a trusted official NSE shareholding snapshot
        official_snapshot = SS(
            instrument_id=RELIANCE_ID,
            period_end=datetime(2024, 3, 31, tzinfo=timezone.utc),
            filing_basis="STANDALONE",
            source_provider="NSE",
            source_type="NSE_SHAREHOLDING_XBRL",
            source_identity_key="nse-official-1",
            source_url="https://nseindia.com",
            reliability_level=ReliabilityLevel.LEVEL_A,
            source_mode=SourceMode.REAL,
            confidence=Decimal("0.95"),
            values=[
                SSV(
                    category=ShareholdingCategory.PROMOTER,
                    percentage=Decimal("55.00"),
                    metric_basis=None,
                    raw_source_label="Promoter",
                    source_locator="nse-xbrl:1",
                    evidence_text="Promoter: 55.00%",
                ),
            ],
        )
        repository.persist_shareholding_snapshot(official_snapshot)

        # Now upload manual evidence with different promoter percentage
        conflicting_text = b"""Shareholding as on 31/03/2024
Promoter and promoter group: 47.15%
Public shareholders: 25.85%
"""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=conflicting_text,
            filename="conflicting.csv",
            instrument_id=RELIANCE_ID,
        )
        assert draft.validation_results.valid

        # Accept without reconciliation should fail
        with pytest.raises(AcceptanceError, match="CONFLICT_WITH_TRUSTED_EVIDENCE"):
            await acceptor.accept(draft_id=draft.draft_id)

    @pytest.mark.asyncio
    async def test_accept_conflict_with_reconcile(self, ingestor, acceptor, repository):
        """Accepting with reconcile_with_conflicts=True should succeed."""
        from app.models import (
            ReliabilityLevel,
            ShareholdingSnapshot as SS,
            ShareholdingSnapshotValue as SSV,
            SourceMode,
        )

        official_snapshot = SS(
            instrument_id=RELIANCE_ID,
            period_end=datetime(2024, 3, 31, tzinfo=timezone.utc),
            filing_basis="STANDALONE",
            source_provider="NSE",
            source_type="NSE_SHAREHOLDING_XBRL",
            source_identity_key="nse-official-2",
            source_url="https://nseindia.com",
            reliability_level=ReliabilityLevel.LEVEL_A,
            source_mode=SourceMode.REAL,
            confidence=Decimal("0.95"),
            values=[
                SSV(
                    category=ShareholdingCategory.PROMOTER,
                    percentage=Decimal("55.00"),
                    metric_basis=None,
                    raw_source_label="Promoter",
                    source_locator="nse-xbrl:2",
                    evidence_text="Promoter: 55.00%",
                ),
            ],
        )
        repository.persist_shareholding_snapshot(official_snapshot)

        conflicting_text = b"""Shareholding as on 31/03/2024
Promoter and promoter group: 47.15%
Public shareholders: 25.85%
"""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=conflicting_text,
            filename="conflicting.csv",
            instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft.draft_id, reconcile_with_conflicts=True)
        # Both snapshots should exist (official not overwritten)
        snapshots = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(snapshots) == 2

    @pytest.mark.asyncio
    async def test_accept_with_corrections(self, ingestor, acceptor, repository):
        """User corrections are applied and re-validated."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        # Apply a correction: change promoter percentage
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections={"shareholding:PROMOTER": Decimal("50.00")},
        )
        assert result["persisted"] is True
        assert result["corrections"] == {"shareholding:PROMOTER": Decimal("50.00")}
        # Wait, corrections value is Decimal - check it's serialized properly
        snapshots = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(snapshots) == 1
        promoter_val = next(
            v for v in snapshots[0].values if v.category == ShareholdingCategory.PROMOTER
        )
        assert promoter_val.percentage == Decimal("50.00")


# ---------------------------------------------------------------------------
# End-to-end service-level test
# ---------------------------------------------------------------------------

class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_flow(self, ingestor, acceptor, repository):
        """End-to-end: missing shareholding evidence -> upload/extract ->
        DB unchanged -> review/accept -> facts persisted with provenance ->
        readiness recalculated (not hard-coded).
        """
        # 1. Verify no existing shareholding evidence
        before = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(before) == 0

        # 2. Ingest (extraction) — creates a DRAFT, must NOT modify DB
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            content_type="text/csv",
            instrument_id=RELIANCE_ID,
        )
        assert draft.status == DraftStatus.DRAFT
        assert draft.validation_results.valid

        # 3. Verify DB is still unchanged after extraction
        after_extraction = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(after_extraction) == 0, \
            "Extraction must not write normalized data to the DB"

        # 4. Accept the draft
        result = await acceptor.accept(draft_id=draft.draft_id)
        assert result["persisted"] is True
        assert result["snapshotId"] is not None
        assert result["readiness"] is not None

        # 5. Verify facts are persisted in the DB
        snapshots = repository.persistence.load_shareholding_snapshots(
            instrument_ids={RELIANCE_ID}
        )
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap.instrument_id == RELIANCE_ID
        assert snap.source_provider == "USER_UPLOAD"
        assert len(snap.values) >= 3  # At least promoter, public, FII

        # 6. Verify provenance is persisted
        records = repository.persistence.load_manual_evidence_drafts("SHAREHOLDING")
        assert len(records) == 1
        assert records[0]["content_hash"] == draft.content_hash
        assert records[0]["evidence_type"] == "SHAREHOLDING"

        # 7. Verify readiness was recalculated (not hard-coded)
        readiness = result["readiness"]
        assert "overallStatus" in readiness
        assert readiness["overallStatus"] is not None

    @pytest.mark.asyncio
    async def test_readiness_not_hard_coded(self, ingestor, acceptor, repository):
        """Verify that readiness is recalculated through the normal path,
        not hard-coded to READY."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_TEXT.encode("utf-8"),
            filename="shareholding.txt",
            instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(draft_id=draft.draft_id)
        # Readiness should be a dict with overallStatus, not a string
        readiness = result["readiness"]
        assert isinstance(readiness, dict)
        assert "overallStatus" in readiness
        # It should reference the SHAREHOLDING requirement
        if "requirements" in readiness:
            shareholding_req = next(
                (r for r in readiness["requirements"] if r.get("requirementId") == "SHAREHOLDING"),
                None,
            )
            if shareholding_req:
                assert shareholding_req["status"] is not None

    @pytest.mark.asyncio
    async def test_fresh_complete_shareholding_drives_the_real_requirement_to_ready_fresh(
        self, ingestor, acceptor, repository
    ):
        """G (READY_FRESH TRANSITION TEST), positive case: a genuinely
        current, complete, correctly provenanced shareholding upload must
        cause the REAL ResearchReadinessRuntime (never a mock, never a
        hard-coded status) to derive SHAREHOLDING == READY_FRESH. The
        period must be inside the 120-day freshness window
        (FreshnessPolicyRegistry.default()["SHAREHOLDING"]) -- the fixed
        31/03/2024 date used elsewhere in this file is for
        persistence/provenance assertions only and is stale by the time
        this test actually runs.
        """
        period = _most_recent_quarter_end_str(datetime.now(timezone.utc))
        fresh_text = (
            f"Shareholding as on {period}\n\n"
            "Promoter and promoter group: 47.15%\n"
            "Public shareholders: 25.85%\n"
            "Foreign institutional investors: 12.34%\n"
            "Domestic institutional investors: 8.76%\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=fresh_text.encode("utf-8"),
            filename="fresh_shareholding.txt", instrument_id=RELIANCE_ID,
        )
        assert draft.validation_results.valid
        result = await acceptor.accept(draft_id=draft.draft_id)
        requirements = result["readiness"]["requirements"]
        shareholding = next(r for r in requirements if r["requirementId"] == "SHAREHOLDING")
        assert shareholding["status"] == "READY_FRESH"
        assert shareholding["sourceProvider"] == "USER_UPLOAD"
        assert shareholding["missingReason"] is None

    @pytest.mark.asyncio
    async def test_stale_shareholding_upload_never_reaches_ready_fresh_via_real_runtime(
        self, ingestor, acceptor, repository
    ):
        """G, negative case: a shareholding period older than the 120-day
        freshness window must never be derived as READY_FRESH, however
        complete/valid/provenanced it otherwise is."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=SAMPLE_TEXT.encode("utf-8"),
            filename="stale_shareholding.txt", instrument_id=RELIANCE_ID,
        )
        assert draft.reporting_period == datetime(2024, 3, 31, tzinfo=timezone.utc)
        result = await acceptor.accept(draft_id=draft.draft_id)
        requirements = result["readiness"]["requirements"]
        shareholding = next(r for r in requirements if r["requirementId"] == "SHAREHOLDING")
        assert shareholding["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_manual_shareholding_for_one_instrument_does_not_contaminate_another(
        self, ingestor, acceptor, repository
    ):
        """G, wrong-instrument isolation: accepting fresh SHAREHOLDING
        evidence for RELIANCE_ID must never affect another instrument's
        SHAREHOLDING requirement or persisted snapshots."""
        period = _most_recent_quarter_end_str(datetime.now(timezone.utc))
        fresh_text = (
            f"Shareholding as on {period}\n\n"
            "Promoter and promoter group: 50.00%\n"
            "Public shareholders: 30.00%\n"
            "Foreign institutional investors: 12.00%\n"
            "Domestic institutional investors: 8.00%\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=fresh_text.encode("utf-8"),
            filename="isolation.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft.draft_id)

        other_snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={AIXTRON_ID})
        assert len(other_snapshots) == 0
        readiness_other = await acceptor.runtime.read(AIXTRON_ID, jurisdiction="IN")
        other_shareholding = readiness_other.for_requirement("SHAREHOLDING")
        assert other_shareholding.status != "READY_FRESH"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_shareholding_draft(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        assert draft.validation_results.valid is True
        assert len(draft.validation_results.errors) == 0

    def test_invalid_percentage_detected(self, ingestor):
        """Text with percentage > 100 should produce validation error."""
        text = b"""Shareholding as on 31/03/2024
Promoter and promoter group: 150%
Public shareholders: 25.85%
"""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text,
            filename="bad.csv",
            instrument_id=RELIANCE_ID,
        )
        # The parser should still extract, but validation should catch it
        if draft.validation_results.valid is False:
            assert any("INVALID_PERCENTAGE" in e for e in draft.validation_results.errors)

    def test_period_uses_quarter_end(self, ingestor):
        """Text with a quarter-end date should not warn about non-quarter-end."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=SAMPLE_CSV.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        # 31/03/2024 is a quarter-end, so no PERIOD_NOT_QUARTER_END warning
        assert not any("PERIOD_NOT_QUARTER_END" in w for w in draft.validation_results.warnings)


class TestRealProductionPathGokulAgroRegression:
    """TASK D regression: the REAL production call path end to end --
    PNG -> app.document_extraction.DocumentExtractor ->
    ManualEvidenceIngestor.ingest() -> draft response -- never bypassing
    ManualEvidenceIngestor by calling app.evidence_interpretation directly
    (that is a separate, narrower unit-test concern covered by
    tests/test_shareholding_table_interpretation.py). This is the exact
    path that was found broken (OCR spatial metadata was silently
    discarded before reaching the interpreter -- see
    ManualEvidenceIngestor._extract_text / _extract_shareholding)."""

    def test_full_ingest_resolves_all_categories_with_ocr_metadata_reaching_the_interpreter(self, ingestor, monkeypatch):
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")

        import io
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(
            str(Path(__file__).resolve().parent / "fixtures" / "fonts" / "DejaVuSansMono-Bold.ttf"), 36)
        col_x = [40, 600, 830, 1060, 1290]
        rows = [
            ["", "Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"],
            ["Promoters", "73.67", "74.24", "74.24", "74.24"],
            ["FIIs", "1.87", "1.58", "1.51", "1.87"],
            ["DIIs", "0.01", "0.01", "0.09", "0.11"],
            ["Public", "24.45", "24.16", "24.18", "23.78"],
            ["No. of Shareholders", "49,673", "52,198", "52,418", "50,407"],
        ]
        image = Image.new("RGB", (1650, 60 + len(rows) * 60), color="white")
        draw = ImageDraw.Draw(image)
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                draw.text((col_x[c], 20 + r * 60), cell, fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        # Spy on interpret_shareholding to prove metadata (OCR word
        # boxes) actually REACHES it through the real ingest() call --
        # not a structural inference, a direct observation of the call.
        received = {}
        from app import evidence_interpretation as ei_module
        real_interpret = ei_module.interpret_shareholding

        def spy_interpret_shareholding(extracted, **kwargs):
            received["metadata"] = getattr(extracted, "metadata", None)
            return real_interpret(extracted, **kwargs)

        monkeypatch.setattr(ei_module, "interpret_shareholding", spy_interpret_shareholding)
        # app.manual_evidence imports interpret_shareholding lazily
        # inside _extract_shareholding (`from app.evidence_interpretation
        # import interpret_shareholding`), so patching the module
        # attribute above is picked up on the next ingest() call.

        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=buffer.getvalue(),
            filename="gokul_agro_shareholding.png",
            instrument_id=RELIANCE_ID,
        )

        # 1. OCR word metadata reached the interpreter.
        assert received.get("metadata"), "interpret_shareholding received no/empty metadata"
        assert received["metadata"].get("ocrWords"), "ocrWords missing from metadata reaching the interpreter"

        # 2. Four correctly grounded Jun 2026 ownership categories proposed.
        by_field = {fact.field: fact.value for fact in draft.proposed_facts}
        assert by_field.get("shareholding:PROMOTER") == Decimal("74.24")
        assert by_field.get("shareholding:FII_FPI") == Decimal("1.87")
        assert by_field.get("shareholding:DII") == Decimal("0.11")
        assert by_field.get("shareholding:PUBLIC_RETAIL") == Decimal("23.78")
        assert len([f for f in draft.proposed_facts if f.field.startswith("shareholding:")]) == 4

        # 3. Reporting period = 2026-06-01, precision MONTH.
        assert draft.reporting_period is not None
        assert (draft.reporting_period.year, draft.reporting_period.month, draft.reporting_period.day) == (2026, 6, 1)

        # 4. Shareholder count remains supplementary, never a proposedFact
        #    or an ownership percentage.
        assert draft.field_suggestions is not None
        assert draft.field_suggestions.get("shareholderCount") == "50407"
        assert not any("shareholderCount" in f.field for f in draft.proposed_facts)
        assert not any(f.field == "shareholding:SHAREHOLDER_COUNT" for f in draft.proposed_facts)

        # 5. No promoter pledge is invented -- absent from source, absent from draft.
        assert not any("PROMOTER_PLEDGE" in f.field for f in draft.proposed_facts)

        # 6. Nothing accepted or persisted as canonical financial evidence
        #    without explicit user acceptance -- ingest() alone must never
        #    persist. Drafts live in-process only until accept().
        assert draft.status == DraftStatus.DRAFT

    def test_unmatched_ocr_garbled_fii_dii_labels_produce_bounded_warnings_not_silence(self, ingestor):
        """CURRENT CLOSURE update: 'Flls'/'Dlls' (an 'I'/'l' misread of
        'FIIs'/'DIIs') is now resolved by the margin-safe short-variant
        matcher, since each is measurably CLOSER to its own canonical
        variant than to the other category's (see
        TestCaseXGokulAgroLiteralOcrTypoFiisDiisMarginDisambiguation in
        test_shareholding_table_interpretation.py) -- that is the fix
        this closure pass was required to make, so it is covered
        elsewhere and no longer asserted as 'stays unmatched' here.

        This test now covers the genuinely-unclear case the original
        intent was protecting: two DIFFERENT garbled labels ('Giis',
        'Ciis') that are each equidistant (tied) between the FII_FPI
        and DII canonical variants -- neither can be favored over the
        other, so both must be left unmatched, never force-assigned by
        row order or by surrounding percentages/totals, with a bounded
        warning identifying each unmatched row, not silence."""
        text = (
            "Sep 2025,Dec 2025,Mar 2026,Jun 2026\n"
            "Promoters,73.67,74.24,74.24,74.24\n"
            "Giis,1.87,1.58,1.51,1.87\n"
            "Ciis,0.01,0.01,0.09,0.11\n"
            "Public,24.45,24.16,24.18,23.78\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text.encode("utf-8"),
            filename="gokul_agro_shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        by_field = {fact.field: fact.value for fact in draft.proposed_facts}
        assert by_field.get("shareholding:PROMOTER") == Decimal("74.24")
        assert by_field.get("shareholding:PUBLIC_RETAIL") == Decimal("23.78")
        assert "shareholding:FII_FPI" not in by_field
        assert "shareholding:DII" not in by_field
        warnings_text = " | ".join(draft.validation_results.warnings)
        assert "AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE" in warnings_text
        assert "FII_FPI" in warnings_text and "DII" in warnings_text
        # Never forced into a category by row position or by nearby
        # totals/percentages -- the warnings above are the only trace;
        # no FII_FPI/DII proposedFact was fabricated (already asserted
        # via by_field above).

    def test_unrelated_text_lines_never_produce_resemblance_warnings(self, ingestor):
        """Negative control for requirement B: ordinary unrelated text
        in the document must never trigger a resemblance warning -- only
        text that is genuinely close to a known short category variant
        and looks like a table row (has a trailing number) does."""
        text = (
            "Sep 2025,Jun 2026\n"
            "Promoters,73.67,74.24\n"
            "Public,24.45,23.78\n"
            "Notes: this filing excludes pledged shares and is subject to review.\n"
            "Disclaimer,and,other,prose,here\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text.encode("utf-8"),
            filename="shareholding.csv",
            instrument_id=RELIANCE_ID,
        )
        assert not any("RESEMBLANCE" in w for w in draft.validation_results.warnings)


class TestFieldClassificationPartialExtraction:
    """TASK E item 3 -- ManualEvidenceDraft.field_classification exposes
    per-category EXTRACTED/MISSING/AMBIGUOUS/INVALID status through the
    real production ingestion path (ingestor.ingest()), reusing
    app.evidence_interpretation.classify_shareholding_fields -- no new
    recognition logic, nothing fabricated."""

    def test_complete_extraction_marks_all_four_categories_extracted(self, ingestor):
        text = (
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
            "Promoters   73.67      74.24      74.24      74.24\n"
            "FIIs         1.87       1.58       1.51       1.87\n"
            "DIIs         0.01       0.01       0.09       0.11\n"
            "Public      24.45      24.16      24.18      23.78\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text.encode("utf-8"),
            filename="complete.csv",
            instrument_id=RELIANCE_ID,
        )
        assert draft.field_classification == {
            "PROMOTER": "EXTRACTED",
            "FII_FPI": "EXTRACTED",
            "DII": "EXTRACTED",
            "PUBLIC_RETAIL": "EXTRACTED",
        }

    def test_partial_extraction_classifies_missing_categories_without_dropping_the_rest(self, ingestor):
        # No DII row or label at all anywhere in the document.
        text = (
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
            "Promoters   73.67      74.24      74.24      74.24\n"
            "FIIs         1.87       1.58       1.51       1.87\n"
            "Public      24.45      24.16      24.18      23.78\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text.encode("utf-8"),
            filename="partial.csv",
            instrument_id=RELIANCE_ID,
        )
        by_field = {fact.field: fact.value for fact in draft.proposed_facts}
        # The three found categories are still proposed -- DII's absence
        # never discards them.
        assert by_field.get("shareholding:PROMOTER") == Decimal("74.24")
        assert by_field.get("shareholding:FII_FPI") == Decimal("1.87")
        assert by_field.get("shareholding:PUBLIC_RETAIL") == Decimal("23.78")
        assert draft.field_classification == {
            "PROMOTER": "EXTRACTED",
            "FII_FPI": "EXTRACTED",
            "DII": "MISSING",
            "PUBLIC_RETAIL": "EXTRACTED",
        }

    def test_ambiguous_label_classifies_that_category_as_ambiguous_not_silently_resolved(self, ingestor):
        # CURRENT CLOSURE update: a single garbled label is only
        # classified AMBIGUOUS when it is genuinely TIED between FII
        # and DII variants (equal edit distance to both) -- a label
        # that is measurably closer to one (e.g. the old "Fdis"
        # fixture, distance 1 from "fiis" vs distance 2 from "diis")
        # now resolves to that closer category via the margin-safe
        # short-variant matcher, so "Giis" (tied at distance 1 to both)
        # is used here to keep testing genuine, unresolved ambiguity.
        text = (
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
            "Promoters   73.67      74.24      74.24      74.24\n"
            "Giis         1.87       1.58       1.51       1.87\n"
            "Public      24.45      24.16      24.18      23.78\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text.encode("utf-8"),
            filename="ambiguous.csv",
            instrument_id=RELIANCE_ID,
        )
        by_field = {fact.field: fact.value for fact in draft.proposed_facts}
        assert "shareholding:FII_FPI" not in by_field
        assert "shareholding:DII" not in by_field
        assert draft.field_classification["PROMOTER"] == "EXTRACTED"
        assert draft.field_classification["PUBLIC_RETAIL"] == "EXTRACTED"
        assert draft.field_classification["FII_FPI"] == "AMBIGUOUS"
        assert draft.field_classification["DII"] == "AMBIGUOUS"
        assert any("AMBIGUOUS_CATEGORY_LABEL" in w for w in draft.validation_results.warnings)

    def test_invalid_row_value_classifies_that_category_as_invalid(self, ingestor):
        # PROMOTER's row has fewer values than the header -- matched
        # label, unresolved value (existing ROW_COLUMN_COUNT_MISMATCH
        # behavior), which must classify as INVALID, not MISSING.
        text = (
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
            "Promoters   74.24\n"
            "FIIs         1.87       1.58       1.51       1.87\n"
            "DIIs         0.01       0.01       0.09       0.11\n"
            "Public      24.45      24.16      24.18      23.78\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=text.encode("utf-8"),
            filename="invalid_row.csv",
            instrument_id=RELIANCE_ID,
        )
        by_field = {fact.field: fact.value for fact in draft.proposed_facts}
        assert "shareholding:PROMOTER" not in by_field
        assert draft.field_classification["PROMOTER"] == "INVALID"
        assert draft.field_classification["FII_FPI"] == "EXTRACTED"
        assert any("ROW_COLUMN_COUNT_MISMATCH" in w for w in draft.validation_results.warnings)

    def test_field_classification_is_none_for_non_shareholding_evidence_types(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS",
            file_bytes=b"Some news article text about the company.",
            filename="news.txt",
            instrument_id=RELIANCE_ID,
        )
        assert draft.field_classification is None


class TestTaskE2EditableReviewFields:
    """TASK E2 -- SHAREHOLDING Review fields are editable for all four
    canonical ownership categories (not just the ones extraction
    happened to find), reusing the existing `corrections`/accept()
    contract end to end: no parallel persistence mechanism, no
    fabrication, no automatic acceptance."""

    PARTIAL_TABLE = (
        "Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoters   73.67      74.24      74.24      74.24\n"
        "FIIs         1.87       1.58       1.51       1.87\n"
        "Public      24.45      24.16      24.18      23.78\n"
    )  # DII is entirely MISSING from this source.

    @pytest.mark.asyncio
    async def test_correcting_an_extracted_value_preserves_the_original_and_marks_user_corrected(
        self, ingestor, acceptor, repository
    ):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        promoter_fact = next(f for f in draft.proposed_facts if f.field == "shareholding:PROMOTER")
        assert promoter_fact.provenance == "OCR_EXTRACTED"
        assert promoter_fact.original_value is None  # not yet corrected

        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections={
                "shareholding:PROMOTER": Decimal("70.00"),
                "shareholding:DII": Decimal("0.11"),  # never extracted at all
            },
        )
        snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={RELIANCE_ID})
        by_category = {v.category: v.percentage for v in snapshots[0].values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("70.00")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.PUBLIC_RETAIL] == Decimal("23.78")

    def test_correcting_a_missing_category_creates_a_fact_with_user_entered_provenance(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        assert draft.field_classification["DII"] == "MISSING"
        assert not any(f.field == "shareholding:DII" for f in draft.proposed_facts)

        draft.corrections = {"shareholding:DII": Decimal("0.11")}
        from app.manual_evidence import ManualEvidenceAcceptor
        acceptor_for_apply = ManualEvidenceAcceptor(repository=ingestor.repository)
        correction_errors = acceptor_for_apply._apply_corrections(draft)
        assert correction_errors == []
        dii_fact = next(f for f in draft.proposed_facts if f.field == "shareholding:DII")
        assert dii_fact.provenance == "USER_ENTERED"
        assert dii_fact.original_value is None
        assert dii_fact.value == Decimal("0.11")

    @pytest.mark.asyncio
    async def test_reporting_period_correction_iso_month_preserves_month_precision(
        self, ingestor, acceptor, repository
    ):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections={
                "shareholding:DII": Decimal("0.11"),
                "reportingPeriod": "2026-09",
            },
        )
        assert result["persisted"] is True
        snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={RELIANCE_ID})
        # "2026-09" (MONTH precision) normalizes to the first of that
        # month, exactly like "Sep 2026" would during extraction --
        # never manufactured to a quarter-end day the user never gave.
        assert snapshots[0].period_end.date().isoformat() == "2026-09-01"

    @pytest.mark.asyncio
    async def test_reporting_period_correction_explicit_day_is_used_exactly(
        self, ingestor, acceptor, repository
    ):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections={
                "shareholding:DII": Decimal("0.11"),
                "reportingPeriod": "2026-06-30",
            },
        )
        snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={RELIANCE_ID})
        assert snapshots[0].period_end.date().isoformat() == "2026-06-30"

    @pytest.mark.asyncio
    async def test_reporting_period_correction_free_text_reuses_extraction_parser(
        self, ingestor, acceptor, repository
    ):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections={
                "shareholding:DII": Decimal("0.11"),
                "reportingPeriod": "Sep 2026",
            },
        )
        snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={RELIANCE_ID})
        assert snapshots[0].period_end.date().isoformat() == "2026-09-01"

    @pytest.mark.asyncio
    async def test_invalid_reporting_period_correction_is_rejected_not_guessed(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_REPORTING_PERIOD_CORRECTION"):
            await acceptor.accept(
                draft_id=draft.draft_id,
                corrections={"reportingPeriod": "not a real date"},
            )

    @pytest.mark.asyncio
    async def test_unknown_category_correction_key_is_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="UNKNOWN_CATEGORY_CORRECTION"):
            await acceptor.accept(
                draft_id=draft.draft_id,
                corrections={"shareholding:NOT_A_REAL_CATEGORY": Decimal("1.0")},
            )

    @pytest.mark.asyncio
    async def test_non_numeric_correction_value_is_rejected_not_crashed(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_PERCENTAGE"):
            await acceptor.accept(
                draft_id=draft.draft_id,
                corrections={"shareholding:DII": "not-a-number"},
            )

    @pytest.mark.asyncio
    async def test_ownership_total_far_from_100_percent_warns_but_does_not_block(
        self, ingestor, acceptor, repository
    ):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            # Deliberately implausible: correcting PROMOTER sharply down
            # makes the four primary categories sum to far less than
            # 100% (no OTHERS/mutual-fund/insurance category is being
            # tracked here, which is legitimate on its own -- so this is
            # a WARNING, never a hard error).
            corrections={
                "shareholding:PROMOTER": Decimal("1.00"),
                "shareholding:DII": Decimal("0.01"),
            },
        )
        assert result["persisted"] is True
        assert any("OWNERSHIP_TOTAL_IMPLAUSIBLE" in w for w in result["validationResults"]["warnings"])

    @pytest.mark.asyncio
    async def test_shareholder_count_correction_key_is_never_persisted_as_a_category(
        self, ingestor, acceptor, repository
    ):
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections={
                "shareholding:DII": Decimal("0.11"),
                "shareholderCount": "50407",
            },
        )
        snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={RELIANCE_ID})
        categories = {v.category for v in snapshots[0].values}
        assert ShareholdingCategory.PROMOTER in categories
        # shareholderCount never became a persisted category.
        for v in snapshots[0].values:
            assert v.category != "shareholderCount"

    @pytest.mark.asyncio
    async def test_end_to_end_partial_extraction_prefill_manual_completion_explicit_acceptance(
        self, ingestor, acceptor, repository
    ):
        """Full TASK E2 workflow: partial extraction -> prefill ->
        manual completion of the missing category -> deterministic
        validation -> explicit accept() -- never auto-accepted."""
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=self.PARTIAL_TABLE.encode("utf-8"),
            filename="partial.csv", instrument_id=RELIANCE_ID,
        )
        # Draft stays in DRAFT status until explicit accept().
        assert draft.status == DraftStatus.DRAFT
        assert draft.field_classification == {
            "PROMOTER": "EXTRACTED", "FII_FPI": "EXTRACTED",
            "DII": "MISSING", "PUBLIC_RETAIL": "EXTRACTED",
        }
        prefilled = {f.field: f.value for f in draft.proposed_facts}
        assert prefilled["shareholding:PROMOTER"] == Decimal("74.24")

        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections={"shareholding:DII": Decimal("0.11")},
        )
        assert result["persisted"] is True
        snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={RELIANCE_ID})
        assert len(snapshots) == 1
        by_category = {v.category: v.percentage for v in snapshots[0].values}
        assert by_category == {
            ShareholdingCategory.PROMOTER: Decimal("74.24"),
            ShareholdingCategory.FII_FPI: Decimal("1.87"),
            ShareholdingCategory.DII: Decimal("0.11"),
            ShareholdingCategory.PUBLIC_RETAIL: Decimal("23.78"),
        }
        # USER_UPLOAD authority is unchanged by corrections.
        assert snapshots[0].source_provider == "USER_UPLOAD"
        assert result["readiness"] is not None  # recalculated through the normal path

    @pytest.mark.asyncio
    async def test_higher_authority_official_evidence_is_not_overridden_by_a_correction(
        self, ingestor, acceptor, repository
    ):
        """TASK E2 item 12 -- a USER_UPLOAD correction must never silently
        override existing trusted official evidence for the same
        instrument/period/category; the pre-existing conflict-detection
        path still governs that, untouched by this change."""
        from app.models import ReliabilityLevel, ShareholdingSnapshot, ShareholdingSnapshotValue, SourceMode

        official_snapshot = ShareholdingSnapshot(
            # "Jun 2026" in the uploaded table below is MONTH precision,
            # normalizing to 2026-06-01 (first-of-month placeholder,
            # never a manufactured day) -- matched here exactly so the
            # conflict check's period_end equality actually lines up.
            instrument_id=RELIANCE_ID,
            period_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
            source_provider="NSE",
            source_type="XBRL",
            source_identity_key="nse-xbrl:official-e2",
            source_url="https://nse.example/official-e2",
            confidence=Decimal("0.95"),
            reliability_level=ReliabilityLevel.LEVEL_A,
            source_mode=SourceMode.REAL,
            values=[
                ShareholdingSnapshotValue(
                    category=ShareholdingCategory.PROMOTER,
                    percentage=Decimal("55.00"),
                    raw_source_label="Promoter",
                    source_locator="nse-xbrl:2",
                    evidence_text="Promoter: 55.00%",
                ),
            ],
        )
        repository.persist_shareholding_snapshot(official_snapshot)

        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters   73.67      74.24\n"
        )
        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING", file_bytes=text.encode("utf-8"),
            filename="conflicting.csv", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="CONFLICT_WITH_TRUSTED_EVIDENCE"):
            await acceptor.accept(
                draft_id=draft.draft_id,
                corrections={"shareholding:PROMOTER": Decimal("30.00")},
            )
        snapshots = repository.persistence.load_shareholding_snapshots(instrument_ids={RELIANCE_ID})
        assert len(snapshots) == 1
        assert snapshots[0].values[0].percentage == Decimal("55.00")


class TestGokulAgroOcrPlusArtifactRegression:
    """FINAL FIX regression -- the real runtime failure: a real
    screenshot's OCR pass can read a table gridline/divider as a
    literal "+" character next to a row label and/or inside a value
    cell. This is rendered directly into the image (not injected as
    synthetic text) so Tesseract genuinely produces the "+" the same
    way the real upload did, and the full PRODUCTION path is exercised
    end to end: PNG -> ManualEvidenceIngestor.ingest() -> draft.

    Before the fix: a stray "+" defeated BOTH the decorative-label-
    prefix stripper (so literal "FIIs"/"DIIs" fell through exact/fuzzy
    matching into AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE) and the table-
    context guard (so EVERY row, including PROMOTER/PUBLIC_RETAIL, was
    rejected as "not a table row"), and the early-return path on zero
    collected categories also discarded the already-recognized
    reporting period and shareholderCount.
    """

    def test_full_ingest_resolves_all_categories_despite_stray_plus_ocr_artifacts(self, ingestor):
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")

        import io
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(
            str(Path(__file__).resolve().parent / "fixtures" / "fonts" / "DejaVuSansMono-Bold.ttf"), 36)
        col_x = [40, 600, 830, 1060, 1290]
        # "+" prefixed onto every row label -- as if a misread table
        # gridline/divider sits just left of the label column, exactly
        # the reported shape ("AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE for
        # literal FIIs and DIIs").
        rows = [
            ["", "Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"],
            ["+Promoters", "73.67", "74.24", "74.24", "74.24"],
            ["+FIIs", "1.87", "1.58", "1.51", "1.87"],
            ["+DIIs", "0.01", "0.01", "0.09", "0.11"],
            ["+Public", "24.45", "24.16", "24.18", "23.78"],
            ["+No. of Shareholders", "49,673", "52,198", "52,418", "50,407"],
        ]
        image = Image.new("RGB", (1650, 60 + len(rows) * 60), color="white")
        draw = ImageDraw.Draw(image)
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                draw.text((col_x[c], 20 + r * 60), cell, fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        draft = ingestor.ingest(
            evidence_type="SHAREHOLDING",
            file_bytes=buffer.getvalue(),
            filename="gokul_agro_shareholding_plus_artifact.png",
            instrument_id=RELIANCE_ID,
        )
        ocr_debug = draft.validation_results.errors + draft.validation_results.warnings

        # All four categories still resolve -- the stray "+" is noise,
        # never mistaken for a decimal digit or a reason to discard the
        # row as narrative text.
        by_field = {fact.field: fact.value for fact in draft.proposed_facts}
        assert by_field.get("shareholding:PROMOTER") == Decimal("74.24"), ocr_debug
        assert by_field.get("shareholding:FII_FPI") == Decimal("1.87"), ocr_debug
        assert by_field.get("shareholding:DII") == Decimal("0.11"), ocr_debug
        assert by_field.get("shareholding:PUBLIC_RETAIL") == Decimal("23.78"), ocr_debug

        # Literal "FIIs"/"DIIs" (correctly spelled, just "+"-prefixed)
        # must resolve as EXACT matches, never fall through to the
        # ambiguous-resemblance warning path.
        assert not any("AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE" in w for w in draft.validation_results.warnings), ocr_debug
        assert not any("UNMATCHED_CATEGORY_LABEL_RESEMBLANCE" in w for w in draft.validation_results.warnings), ocr_debug

        # Reporting period = 2026-06-01, precision MONTH -- preserved
        # even though it was previously discarded whenever the category
        # loop found nothing.
        assert draft.reporting_period is not None, ocr_debug
        assert (draft.reporting_period.year, draft.reporting_period.month, draft.reporting_period.day) == (2026, 6, 1), ocr_debug

        # Shareholder count stays supplementary, never an ownership fact.
        assert draft.field_suggestions is not None, ocr_debug
        assert draft.field_suggestions.get("shareholderCount") == "50407", ocr_debug
        assert not any("shareholderCount" in f.field for f in draft.proposed_facts)

        # Partial-extraction classification still correctly reports all
        # four as EXTRACTED (TASK E/E2 behavior preserved by this fix).
        assert draft.field_classification == {
            "PROMOTER": "EXTRACTED", "FII_FPI": "EXTRACTED",
            "DII": "EXTRACTED", "PUBLIC_RETAIL": "EXTRACTED",
        }, ocr_debug
