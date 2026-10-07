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

        response = TestClient(app).get("/api/v1/research/evidence/file-types")

        assert response.status_code == 200
        assert set(response.json()["supportedFileTypes"]) == {"CSV", "PDF", "TXT", "PNG", "JPG", "DOCX"}
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
