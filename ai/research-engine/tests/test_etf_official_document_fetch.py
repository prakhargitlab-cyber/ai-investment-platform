"""Offline-only tests for the bounded official-document fetch/ingestion layer.

No real network call is made anywhere in this file: every HTTP interaction
goes through httpx.MockTransport, injected via BoundedOfficialDocumentFetcher's
constructor (which forwards it to the existing HttpResearchFetcher). The one
exception -- loading real, already-verified NIFTYBEES/JUNIORBEES PDF bytes as
a MockTransport response body -- is local file I/O only, guarded by
pytest.skip() exactly like the existing genuine-document validation tests,
since `.tmp/` is gitignored and may not exist in every checkout.
"""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID
import io

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.etf_document_discovery import OfficialDocumentRegistry, register_official_document, ingest_etf_facts_from_document
from app.etf_evidence import EtfListing, EtfProvenance
from app.etf_official_document_fetch import BoundedFetchOutcome, BoundedOfficialDocumentFetcher
from app.etf_official_document_pipeline import (
    MAX_BOUNDED_INSTRUMENTS,
    EtfOfficialDocumentAcquisitionPipeline,
)
from app.etf_official_document_sources import (
    AccessTier,
    ApprovedDocumentSource,
    ApprovedDocumentType,
    ApprovedSourceRegistry,
)
from app.models import EtfResearchProfile
from app.persistence import SqliteResearchPersistence
from app.settings import Settings

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
INSTRUMENT_ID = UUID(int=42)
ISIN = "IN0000000042"
APPROVED_HOST = "amc.example.com"
APPROVED_URL = f"https://{APPROVED_HOST}/ProductNotes/fixture.pdf"

VALIDATION_DIR = Path(__file__).resolve().parents[3] / ".tmp" / "etf-validation"


def _pdf_bytes(text_lines):
    """Build a tiny, genuinely parseable single-page PDF (no network, no fixtures)."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    page = writer.pages[0]
    content = "BT /F1 12 Tf 50 700 Td\n"
    for index, line in enumerate(text_lines):
        if index > 0:
            content += "0 -14 Td\n"
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += f"({escaped}) Tj\n"
    content += "ET"
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                              NameObject("/BaseFont"): NameObject("/Helvetica")})
    fontdict = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fontdict})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


VALID_PDF_TEXT_LINES = ["Scheme: Fixture ETF", "ISIN: IN0000000042", "AUM: INR 10 crore"]
VALID_PDF_BYTES = _pdf_bytes(VALID_PDF_TEXT_LINES)


def approved_source(*, url=APPROVED_URL, host=APPROVED_HOST, access_tier=AccessTier.PUBLIC_UNAUTHENTICATED):
    return ApprovedDocumentSource(
        instrument_id=INSTRUMENT_ID, isin=ISIN, ticker="FIXTURE", issuer_name="Fixture AMC",
        document_url=url, document_host=host, document_type=ApprovedDocumentType.PRODUCT_NOTE,
        approval_reference="offline-fixture-review", access_tier=access_tier,
    )


def registry_with(*sources):
    return ApprovedSourceRegistry(sources or [approved_source()])


def settings():
    return Settings(research_etf_official_document_fetch_enabled=True)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def pdf_handler(body=VALID_PDF_BYTES, status_code=200, content_type="application/pdf"):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body, headers={"content-type": content_type})
    return handle


@pytest.fixture
def store():
    result = SqliteResearchPersistence(":memory:")
    result.save_etf_evidence(EtfListing(instrument_id=INSTRUMENT_ID, isin=ISIN, symbol="FIXTURE",
        name="Fixture ETF", provenance=EtfProvenance(provider="NSE",
            source_type="NSE_OFFICIAL_ETF_SECURITY_LIST", source_identity="fixture-list",
            source_url="https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv",
            authority="OFFICIAL_EXCHANGE", reliability_level="LEVEL_A", retrieved_at=NOW)))
    yield result
    result._connection.close()


def profile():
    return EtfResearchProfile(instrument_id=INSTRUMENT_ID, fund_id=INSTRUMENT_ID, fund_name="Fixture ETF",
        ticker="FIXTURE", exchange="NSE", mic="XNSE", isin=ISIN, currency="INR",
        known_domains=[APPROVED_HOST])


# ---------------------------------------------------------------------------
# Fetch adapter (Phase 2/3) scenarios
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approved_source_successful_fetch_extracts_text():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=VALID_PDF_BYTES, headers={"content-type": "application/pdf"})

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.FETCHED
    assert calls == [APPROVED_URL]
    assert "ISIN: IN0000000042" in result.normalized_text
    assert result.raw_bytes_sha256 == sha256(VALID_PDF_BYTES).hexdigest()
    assert result.normalized_text_sha256 == sha256(result.normalized_text.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_unapproved_url_is_rejected_without_any_network_call():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=VALID_PDF_BYTES, headers={"content-type": "application/pdf"})

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, "https://amc.example.com/ProductNotes/not-approved.pdf")
    assert result.outcome == BoundedFetchOutcome.URL_NOT_APPROVED
    assert calls == []  # the handler must never be invoked for an unapproved URL


@pytest.mark.asyncio
async def test_unapproved_url_for_a_different_instrument_is_rejected():
    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(pdf_handler()))
    result = await fetcher.fetch(UUID(int=999), APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.URL_NOT_APPROVED


@pytest.mark.asyncio
async def test_cross_host_redirect_to_a_different_public_host_is_rejected():
    def handle(request):
        if request.url.host == APPROVED_HOST:
            return httpx.Response(302, headers={"location": "https://attacker.example.com/fixture.pdf"})
        return httpx.Response(200, content=VALID_PDF_BYTES, headers={"content-type": "application/pdf"})

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.CROSS_HOST_REDIRECT_REJECTED
    assert "attacker.example.com" in result.reason


@pytest.mark.asyncio
async def test_redirect_to_a_private_ip_is_blocked_by_ssrf_validation():
    def handle(request):
        if request.url.host == APPROVED_HOST:
            return httpx.Response(302, headers={"location": "https://127.0.0.1/fixture.pdf"})
        return httpx.Response(200, content=VALID_PDF_BYTES, headers={"content-type": "application/pdf"})

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.SSRF_BLOCKED_BY_URL_VALIDATION


@pytest.mark.asyncio
async def test_oversized_response_is_rejected_before_full_download():
    oversized = b"%PDF-1.4\n" + b"0" * 2048

    def handle(request):
        return httpx.Response(200, content=oversized, headers={"content-type": "application/pdf"})

    small_settings = Settings(research_etf_official_document_fetch_enabled=True,
                               research_official_document_max_bytes=1024)
    fetcher = BoundedOfficialDocumentFetcher(registry_with(), small_settings, client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.SIZE_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_html_response_is_rejected_as_unexpected_content_type():
    def handle(request):
        return httpx.Response(200, content=b"<html>please sign in</html>", headers={"content-type": "text/html"})

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.UNEXPECTED_CONTENT_TYPE
    assert result.content_type == "text/html"


@pytest.mark.asyncio
async def test_html_masquerading_as_pdf_content_type_still_fails_signature_check():
    """A captcha page served with a (wrong) application/pdf content-type must
    still be rejected, via the existing PDF magic-byte check, not accepted."""
    def handle(request):
        return httpx.Response(200, content=b"<html>captcha challenge</html>",
                               headers={"content-type": "application/pdf"})

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.PDF_EXTRACTION_FAILED
    assert "PDF_SIGNATURE_INVALID" in result.reason


@pytest.mark.asyncio
async def test_corrupted_pdf_structure_is_rejected():
    truncated = VALID_PDF_BYTES[: len(VALID_PDF_BYTES) // 2]

    def handle(request):
        return httpx.Response(200, content=truncated, headers={"content-type": "application/pdf"})

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.PDF_EXTRACTION_FAILED


@pytest.mark.asyncio
async def test_persistent_server_error_after_retries_is_reported_not_raised():
    """HttpResearchFetcher returns (not raises) the last response after
    exhausting retries on a persistent 5xx -- the adapter must check
    status_code post-return rather than rely purely on exceptions."""
    calls = []

    def handle(request):
        calls.append(1)
        return httpx.Response(503, content=b"", headers={"content-type": "text/plain"})

    fast_settings = Settings(research_etf_official_document_fetch_enabled=True, research_max_retries=1)
    fetcher = BoundedOfficialDocumentFetcher(registry_with(), fast_settings, client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.HTTP_STATUS_ERROR
    assert "503" in result.reason
    assert len(calls) == fast_settings.research_max_retries + 1  # one initial attempt + retries


@pytest.mark.asyncio
async def test_restricted_status_is_reported_as_http_status_error():
    def handle(request):
        return httpx.Response(403, content=b"forbidden")

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.HTTP_STATUS_ERROR


@pytest.mark.asyncio
async def test_transport_failure_is_reported_not_raised():
    def handle(request):
        raise httpx.ConnectError("connection refused")

    fetcher = BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.TRANSPORT_ERROR


# ---------------------------------------------------------------------------
# Access-tier enforcement: authenticated/unreviewed sources are refused
# before any network call is made.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_requires_authentication_source_is_rejected_without_any_network_call():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=VALID_PDF_BYTES, headers={"content-type": "application/pdf"})

    gated = approved_source(access_tier=AccessTier.REQUIRES_AUTHENTICATION)
    fetcher = BoundedOfficialDocumentFetcher(registry_with(gated), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.AUTHENTICATION_REQUIRED_NOT_SUITABLE
    assert calls == []  # the handler must never be invoked for a gated source


@pytest.mark.asyncio
async def test_unreviewed_source_defaults_to_not_fetchable_without_any_network_call():
    """A source constructed without an explicit access_tier must default to
    REQUIRES_AUTHENTICATION, not to anything fetchable -- an unreviewed
    source is never fetchable by omission."""
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=VALID_PDF_BYTES, headers={"content-type": "application/pdf"})

    unreviewed = ApprovedDocumentSource(
        instrument_id=INSTRUMENT_ID, isin=ISIN, ticker="FIXTURE", issuer_name="Fixture AMC",
        document_url=APPROVED_URL, document_host=APPROVED_HOST, document_type=ApprovedDocumentType.PRODUCT_NOTE,
        approval_reference="offline-fixture-review",
        # access_tier deliberately omitted
    )
    assert unreviewed.access_tier is AccessTier.REQUIRES_AUTHENTICATION

    fetcher = BoundedOfficialDocumentFetcher(registry_with(unreviewed), settings(), client=client_for(handle))
    result = await fetcher.fetch(INSTRUMENT_ID, APPROVED_URL)
    assert result.outcome == BoundedFetchOutcome.AUTHENTICATION_REQUIRED_NOT_SUITABLE
    assert calls == []


def test_default_bounded_registry_marks_both_real_sources_as_requires_authentication():
    """The real NIFTYBEES/JUNIORBEES Product Note URLs sit on NIpponIndia's
    OTP-gated path (confirmed by direct review) -- they must never be
    treated as auto-fetchable."""
    from app.etf_official_document_sources import default_bounded_registry
    registry = default_bounded_registry()
    sources = registry.all_sources()
    assert len(sources) == 2
    assert all(source.access_tier is AccessTier.REQUIRES_AUTHENTICATION for source in sources)


@pytest.mark.asyncio
async def test_pipeline_also_blocks_a_requires_authentication_source(store):
    gated = approved_source(access_tier=AccessTier.REQUIRES_AUTHENTICATION)
    approved = registry_with(gated)
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=VALID_PDF_BYTES, headers={"content-type": "application/pdf"})

    pipeline = EtfOfficialDocumentAcquisitionPipeline(
        approved, settings(), fetcher=BoundedOfficialDocumentFetcher(approved, settings(), client=client_for(handle)))
    result = await pipeline.acquire(store, profile(), APPROVED_URL)
    assert result.status == "FETCH_FAILED"
    assert "AUTHENTICATION_REQUIRED_NOT_SUITABLE" in result.reason
    assert calls == []
    assert store.etf_facts(INSTRUMENT_ID) == []


# ---------------------------------------------------------------------------
# Allowlist contract (Phase 1) invariants
# ---------------------------------------------------------------------------

def test_approved_source_rejects_a_url_on_a_different_host():
    with pytest.raises(ValueError):
        ApprovedDocumentSource(instrument_id=INSTRUMENT_ID, isin=ISIN, ticker="FIXTURE",
            issuer_name="Fixture AMC", document_url="https://other.example.com/fixture.pdf",
            document_host=APPROVED_HOST, document_type=ApprovedDocumentType.PRODUCT_NOTE,
            approval_reference="offline-fixture-review")


def test_approved_source_rejects_non_https():
    with pytest.raises(ValueError):
        ApprovedDocumentSource(instrument_id=INSTRUMENT_ID, isin=ISIN, ticker="FIXTURE",
            issuer_name="Fixture AMC", document_url=f"http://{APPROVED_HOST}/fixture.pdf",
            document_host=APPROVED_HOST, document_type=ApprovedDocumentType.PRODUCT_NOTE,
            approval_reference="offline-fixture-review")


def test_default_bounded_registry_never_exceeds_the_rollout_limit():
    from app.etf_official_document_sources import default_bounded_registry
    assert default_bounded_registry().instrument_count() <= MAX_BOUNDED_INSTRUMENTS


def test_pipeline_construction_rejects_an_oversized_registry():
    oversized = ApprovedSourceRegistry([
        approved_source(url=f"https://{APPROVED_HOST}/a.pdf"),
        ApprovedDocumentSource(instrument_id=UUID(int=2), isin="IN0000000002", ticker="B",
            issuer_name="Fixture AMC", document_url=f"https://{APPROVED_HOST}/b.pdf",
            document_host=APPROVED_HOST, document_type=ApprovedDocumentType.PRODUCT_NOTE,
            approval_reference="ref-b", access_tier=AccessTier.PUBLIC_UNAUTHENTICATED),
        ApprovedDocumentSource(instrument_id=UUID(int=3), isin="IN0000000003", ticker="C",
            issuer_name="Fixture AMC", document_url=f"https://{APPROVED_HOST}/c.pdf",
            document_host=APPROVED_HOST, document_type=ApprovedDocumentType.PRODUCT_NOTE,
            approval_reference="ref-c", access_tier=AccessTier.PUBLIC_UNAUTHENTICATED),
    ])
    with pytest.raises(ValueError):
        EtfOfficialDocumentAcquisitionPipeline(oversized, settings())


# ---------------------------------------------------------------------------
# Integration (Phase 4): fetch -> existing registered-document ingestion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_flag_blocks_acquisition_even_with_a_valid_approved_document(store):
    pipeline = EtfOfficialDocumentAcquisitionPipeline(
        registry_with(), Settings(research_etf_official_document_fetch_enabled=False),
        fetcher=BoundedOfficialDocumentFetcher(registry_with(), settings(), client=client_for(pdf_handler())))
    result = await pipeline.acquire(store, profile(), APPROVED_URL)
    assert result.status == "DISABLED"
    assert store.etf_facts(INSTRUMENT_ID) == []


@pytest.mark.asyncio
async def test_successful_fetch_reaches_the_unmodified_identity_gate_and_persists_a_fact(store):
    approved = registry_with()
    pipeline = EtfOfficialDocumentAcquisitionPipeline(
        approved, settings(), fetcher=BoundedOfficialDocumentFetcher(approved, settings(), client=client_for(pdf_handler())))
    result = await pipeline.acquire(store, profile(), APPROVED_URL)
    assert result.status == "INGESTED", result.reason
    assert result.facts["aum"]["outcome"] == "SUCCESS_WITH_DATA"
    fact, = store.etf_facts(INSTRUMENT_ID)
    assert fact.value == 100000000  # INR 10 crore


@pytest.mark.asyncio
async def test_wrong_scheme_document_is_still_rejected_by_the_unmodified_identity_gate(store):
    """The fetch succeeds (it is a real, well-formed PDF); the existing,
    untouched verified_scheme_section gate is what must reject it."""
    wrong_scheme_pdf = _pdf_bytes(["Scheme: A Totally Different ETF", "ISIN: IN0000009999", "AUM: INR 50 crore"])
    approved = registry_with()
    pipeline = EtfOfficialDocumentAcquisitionPipeline(
        approved, settings(),
        fetcher=BoundedOfficialDocumentFetcher(approved, settings(), client=client_for(pdf_handler(wrong_scheme_pdf))))
    result = await pipeline.acquire(store, profile(), APPROVED_URL)
    assert result.status == "INGESTED"  # the pipeline itself ran; the fact extraction failed closed
    assert result.facts["aum"]["outcome"] == "TECHNICAL_FAILURE"
    assert store.etf_facts(INSTRUMENT_ID) == []


@pytest.mark.asyncio
async def test_missing_aum_and_ter_is_reported_as_field_absent_not_fabricated(store):
    no_metrics_pdf = _pdf_bytes(["Scheme: Fixture ETF", "ISIN: IN0000000042",
                                  "This document contains no reportable financial figures."])
    approved = registry_with()
    pipeline = EtfOfficialDocumentAcquisitionPipeline(
        approved, settings(),
        fetcher=BoundedOfficialDocumentFetcher(approved, settings(), client=client_for(pdf_handler(no_metrics_pdf))))
    result = await pipeline.acquire(store, profile(), APPROVED_URL)
    assert result.status == "INGESTED"
    assert result.facts["aum"]["reason"] == "VALID_DOCUMENT_FIELD_ABSENT"
    assert result.facts["expense_ratio"]["reason"] == "VALID_DOCUMENT_FIELD_ABSENT"
    assert store.etf_facts(INSTRUMENT_ID) == []


@pytest.mark.asyncio
async def test_a_second_fetch_of_the_same_url_is_rejected_as_a_duplicate_registration(store):
    approved = registry_with()
    pipeline = EtfOfficialDocumentAcquisitionPipeline(
        approved, settings(),
        fetcher=BoundedOfficialDocumentFetcher(approved, settings(), client=client_for(pdf_handler())))
    first = await pipeline.acquire(store, profile(), APPROVED_URL)
    assert first.status == "INGESTED"
    second = await pipeline.acquire(store, profile(), APPROVED_URL)
    assert second.status == "FETCH_FAILED"
    assert "already registered" in second.reason


@pytest.mark.asyncio
async def test_candidate_facts_remain_visible_after_ingestion(store):
    approved = registry_with()
    pipeline = EtfOfficialDocumentAcquisitionPipeline(
        approved, settings(), fetcher=BoundedOfficialDocumentFetcher(approved, settings(), client=client_for(pdf_handler())))
    await pipeline.acquire(store, profile(), APPROVED_URL)
    facts = store.etf_facts(INSTRUMENT_ID)
    assert len(facts) == 1
    assert facts[0].provenance.source_url == APPROVED_URL


def test_hash_mismatch_between_registered_approval_and_ingested_text_fails_closed(store):
    """Simulates a stale/tampered registration: the approved sha256 does not
    match the text actually being ingested. The existing, unmodified
    verified_scheme_section hash check (not this task's new code) must
    reject it."""
    text = "Scheme: Fixture ETF\nISIN: IN0000000042\nAUM: INR 10 crore"
    registry = OfficialDocumentRegistry()
    register_official_document(registry=registry, instrument_id=INSTRUMENT_ID, document_url=APPROVED_URL,
        canonical_url=APPROVED_URL, source_name="Fixture AMC",
        normalized_text_sha256=sha256(b"this is not the real document text").hexdigest(),
        scheme_isin=ISIN, approval_reference="offline-fixture-review", retrieved_at=NOW, published_at=NOW)
    result = ingest_etf_facts_from_document(store, profile(), text, document_url=APPROVED_URL,
        registry=registry, clock=lambda: NOW)
    assert result["aum"]["outcome"] == "TECHNICAL_FAILURE"
    assert "HASH_MISMATCH" in result["aum"]["reason"]
    assert store.etf_facts(INSTRUMENT_ID) == []


# ---------------------------------------------------------------------------
# Authenticity cross-check against the real, previously hash-verified PDFs
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not VALIDATION_DIR.exists(), reason="real validation fixtures not present in this checkout")
@pytest.mark.asyncio
async def test_fetching_the_real_niftybees_pdf_extracts_the_same_text_already_verified():
    real_pdf = (VALIDATION_DIR / "NIFTYBEES-official.pdf").read_bytes()
    niftybees_source = ApprovedDocumentSource(
        instrument_id=UUID("805e0ebc-ea95-4859-b739-bdfbf5db327e"), isin="INF204KB14I2", ticker="NIFTYBEES",
        issuer_name="Nippon India Mutual Fund",
        document_url="https://mf.nipponindiaim.com/FundsAndPerformance/ProductNotes/fixture-niftybees.pdf",
        document_host="mf.nipponindiaim.com", document_type=ApprovedDocumentType.PRODUCT_NOTE,
        approval_reference="offline-real-pdf-cross-check",
        # This test exercises the fetch/extraction path itself, with a
        # fixture URL distinct from the real (auth-protected) ProductNotes
        # path -- it is not asserting that the real path is fetchable.
        access_tier=AccessTier.PUBLIC_UNAUTHENTICATED)
    approved = ApprovedSourceRegistry([niftybees_source])

    def handle(request):
        return httpx.Response(200, content=real_pdf, headers={"content-type": "application/pdf"})

    fetcher = BoundedOfficialDocumentFetcher(approved, settings(), client=client_for(handle))
    result = await fetcher.fetch(niftybees_source.instrument_id, niftybees_source.document_url)
    assert result.outcome == BoundedFetchOutcome.FETCHED
    assert result.raw_bytes_sha256 == sha256(real_pdf).hexdigest()
    # Independent proof this is genuinely the same document already validated,
    # not a re-fabricated stand-in for it: the already-proven structural
    # limitation (no labelled ISIN line) still holds on the fetched copy.
    import re
    assert not re.findall(r"(?im)^\s*ISIN\s*:\s*(\S+)\s*$", result.normalized_text)
