"""Offline end-to-end staging contract. No external requests or actual SBI mappings."""
from datetime import date, datetime, timezone
from io import BytesIO
from uuid import UUID, uuid4

import httpx
import openpyxl
import pytest

from app.etf_evidence import EtfMetric
from app.sbi_ter_identity import ReviewedSbiSchemeIdentity
from app.sbi_ter_runtime_download import SbiTerRuntimeDownloader
from app.sbi_ter_staging import stage_and_audit_current_sbi_ter
from app.sbi_ter_workbook_adapter import BoundedSbiTerAdapter, EXPECTED_HEADERS
from app.settings import Settings


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
CODE = "SYNTHETIC/ETF/1"


def payload():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(list(EXPECTED_HEADERS))
    ws.append([CODE, "SBI Example ETF", datetime(2026, 11, 3), 0, 0, 0, 0, 0.12,
               0, 0, 0, 0, 0])
    stream = BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()


def downloader(*, adapter=None):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=payload(),
                headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
        )
    )
    return client, SbiTerRuntimeDownloader(
        Settings(), client=client, clock=lambda: NOW, adapter=adapter
    )


def canonical():
    return [dict(
        globalInstrumentId=str(UUID(int=1)), assetType="ETF", status="ACTIVE",
        primaryExchange="NSE", isin="INF200A01026", primarySymbol="EXAMPLEETF"
    )]


def reviewed():
    return ReviewedSbiSchemeIdentity(
        scheme_code=CODE, isin="INF200A01026", ticker="EXAMPLEETF",
        evidence_url="https://www.sbimf.com/example.pdf", evidence_sha256="a"*64,
        reviewed_by="Synthetic Reviewer", reviewed_on=date(2026, 11, 3),
        approval_reference="SYNTHETIC_TEST_ONLY"
    )


@pytest.mark.asyncio
async def test_unverified_live_workbook_remains_unresolved():
    client, source = downloader()
    async with client:
        report = await stage_and_audit_current_sbi_ter(source, canonical())
    assert report.staged_count == report.unresolved_count == 1
    assert report.review_matched_count == 0
    assert report.authorized_for_fact_persistence is False
    assert report.rows[0].canonical_instrument_id is None


@pytest.mark.asyncio
async def test_review_record_still_does_not_authorize_persistence():
    client, source = downloader()
    async with client:
        report = await stage_and_audit_current_sbi_ter(
            source, canonical(), reviewed_records=[reviewed()]
        )
    assert report.review_matched_count == 1
    assert report.unresolved_count == 0
    assert report.authorized_for_fact_persistence is False


@pytest.mark.asyncio
async def test_production_capable_resolver_cannot_be_used_in_staging():
    adapter = BoundedSbiTerAdapter(identity_resolver=lambda code: UUID(int=1))
    client, source = downloader(adapter=adapter)
    async with client:
        with pytest.raises(ValueError, match="SBI_TER_STAGING_MUST_NOT_PERSIST_FACTS"):
            await stage_and_audit_current_sbi_ter(source, canonical())


@pytest.mark.asyncio
async def test_canonical_identity_conflict_fails_closed():
    client, source = downloader()
    async with client:
        with pytest.raises(ValueError, match="REVIEWED_IDENTITY_NOT_IN_CANONICAL_UNIVERSE"):
            await stage_and_audit_current_sbi_ter(
                source, canonical(), reviewed_records=[
                    ReviewedSbiSchemeIdentity(
                        scheme_code=CODE, isin="INF999999999", ticker="EXAMPLEETF",
                        evidence_url="https://www.sbimf.com/example.pdf",
                        evidence_sha256="a"*64,
                        reviewed_by="Synthetic Reviewer", reviewed_on=date(2026, 11, 3),
                        approval_reference="SYNTHETIC_TEST_ONLY"
                    )
                ]
            )
