"""Offline gate tests: one approved Nippon PDF, two review-only sections."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import app.etf_nippon_shared_review as shared
from app.etf_nippon_monthly_source import month_candidate
from app.etf_nippon_factsheet_review import ReviewOnlyEtfMetrics, HEADINGS
from app.etf_official_document_sources import (
    AccessTier, ApprovedDocumentSource, ApprovedDocumentType, ApprovedSourceRegistry,
)

CANDIDATE = month_candidate(2026, 10)
IDS = {"NIFTYBEES": UUID(int=10), "JUNIORBEES": UUID(int=11)}
ISINS = {"NIFTYBEES": "INF204KB14I2", "JUNIORBEES": "INF732E01045"}
RAW = b"%PDF-1.4\nA standalone fixture; parser is stubbed for orchestration tests."


def approved(*, access=AccessTier.PUBLIC_UNAUTHENTICATED, url=None, missing=()):
    sources = []
    for ticker, instrument in IDS.items():
        if ticker in missing:
            continue
        sources.append(ApprovedDocumentSource(
            instrument_id=instrument, isin=ISINS[ticker], ticker=ticker,
            issuer_name="Nippon India Mutual Fund",
            document_url=url or CANDIDATE.url, document_host="mf.nipponindiaim.com",
            document_type=ApprovedDocumentType.FACTSHEET,
            approval_reference="offline-review-only-source-test",
            access_tier=access,
        ))
    return ApprovedSourceRegistry(sources)


def fake_metric(pdf_bytes, ticker):
    assert pdf_bytes == RAW and ticker in HEADINGS
    return ReviewOnlyEtfMetrics(
        status="DOCUMENT_REVIEW_ONLY", ticker=ticker, scheme_heading=HEADINGS[ticker],
        pdf_page=207 if ticker == "NIFTYBEES" else 216,
        report_date=date(2026, 9, 30), aum_crore=Decimal("1000"),
        average_aum_crore=Decimal("1001"), monthly_average_expense_ratio_percent=Decimal("0.04"),
        tracking_error_1y_percent=Decimal("0.02"), tracking_difference_1y_percent=Decimal("-0.02"),
        raw_pdf_sha256=shared.sha256(pdf_bytes).hexdigest(),
    )


@pytest.mark.asyncio
async def test_two_approved_schemes_share_one_fetch_and_remain_review_only(monkeypatch):
    monkeypatch.setattr(shared, "extract_review_only", fake_metric)
    fetch = AsyncMock(return_value=RAW)
    result = await shared.review_approved_shared_month(CANDIDATE, approved(), IDS, fetch)
    fetch.assert_awaited_once_with(CANDIDATE.url)
    assert result.status == "DOCUMENT_REVIEW_ONLY"
    assert result.identity_verified is False
    assert len(result.metrics) == 2
    assert all(not m.identity_verified for m in result.metrics)
    assert [x.pdf_page for x in result.metrics] == [207, 216]


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", [
    ApprovedSourceRegistry(),
    approved(access=AccessTier.REQUIRES_AUTHENTICATION),
    approved(missing=("JUNIORBEES",)),
    approved(url=month_candidate(2026, 9).url),
])
async def test_no_network_without_exact_public_approval(registry):
    fetch = AsyncMock(return_value=RAW)
    with pytest.raises(ValueError, match="MONTHLY_FACTSHEET_EXACT_PUBLIC_APPROVAL_REQUIRED"):
        await shared.review_approved_shared_month(CANDIDATE, registry, IDS, fetch)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_reused_identity_without_network():
    fetch = AsyncMock(return_value=RAW)
    with pytest.raises(ValueError, match="SHARED_FACTSHEET_DUPLICATE_IDENTITY"):
        await shared.review_approved_shared_month(CANDIDATE, approved(),
            {"NIFTYBEES": IDS["NIFTYBEES"], "JUNIORBEES": IDS["NIFTYBEES"]}, fetch)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reporting_date_mismatch_is_rejected_after_one_fetch(monkeypatch):
    original = fake_metric
    def stale(pdf_bytes, ticker):
        from dataclasses import replace
        return replace(original(pdf_bytes, ticker), report_date=date(2026, 8, 31))
    monkeypatch.setattr(shared, "extract_review_only", stale)
    fetch = AsyncMock(return_value=RAW)
    with pytest.raises(ValueError, match="MONTHLY_FACTSHEET_REPORT_DATE_MISMATCH"):
        await shared.review_approved_shared_month(CANDIDATE, approved(), IDS, fetch)
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_hash_mismatch_is_rejected(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(shared, "extract_review_only", lambda pdf, ticker:
                        replace(fake_metric(pdf, ticker), raw_pdf_sha256="0" * 64))
    with pytest.raises(ValueError, match="SHARED_FACTSHEET_EVIDENCE_CONFLICT"):
        await shared.review_approved_shared_month(CANDIDATE, approved(), IDS,
                                                 AsyncMock(return_value=RAW))


@pytest.mark.asyncio
async def test_never_accept_more_than_two_schemes():
    fetch = AsyncMock(return_value=RAW)
    with pytest.raises(ValueError, match="SHARED_FACTSHEET_SCHEME_LIMIT"):
        await shared.review_approved_shared_month(CANDIDATE, approved(),
            {"NIFTYBEES": IDS["NIFTYBEES"], "JUNIORBEES": IDS["JUNIORBEES"], "OTHER": UUID(int=12)}, fetch)
    fetch.assert_not_awaited()
