"""Offline tests for runtime SBI TER downloads: no real HTTP requests."""
from datetime import date, datetime, timezone
from io import BytesIO
from uuid import uuid4

import httpx
import openpyxl
import pytest

from app.etf_evidence import EtfMetric
from app.persistence import SqliteResearchPersistence
from app.sbi_ter_workbook_adapter import BoundedSbiTerAdapter, EXPECTED_HEADERS, AdapterOutcome
from app.sbi_ter_runtime_download import (
    SBI_TER_SOURCE_URL, SbiTerRuntimeDownloader, _validate_xlsx_bytes,
)
from app.settings import Settings


NOW = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
CODE = "SBIM/O/O/EET/1"  # synthetic scheme/identity fixture, NOT a real approval


def workbook_bytes(ter, on_date=datetime(2026, 11, 3), *, headers=EXPECTED_HEADERS):
    workbook = openpyxl.Workbook()
    workbook.active.title = "Sheet1"
    workbook.active.append(list(headers))
    workbook.active.append(
        [CODE, "SBI Nifty 50 ETF", on_date, 0, 0, 0, 0, ter, 0, 0, 0, 0, 0]
    )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def response(payload, *, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             status=200):
    return httpx.Response(status, headers={"Content-Type": content_type}, content=payload)


@pytest.mark.asyncio
async def test_download_uses_latest_runtime_bytes_not_saved_fixture():
    payloads = [workbook_bytes("0.052"), workbook_bytes("0.071", datetime(2026, 11, 4))]
    requests = []

    def handler(request):
        requests.append(request.url)
        return response(payloads.pop(0))

    instrument_id = uuid4()
    adapter = BoundedSbiTerAdapter(
        identity_resolver=lambda code: instrument_id if code == CODE else None,
        clock=lambda: NOW,
    )
    store = SqliteResearchPersistence(":memory:")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = SbiTerRuntimeDownloader(Settings(), client=client, adapter=adapter, clock=lambda: NOW)
        first = await downloader.acquire(store)
        second = await downloader.acquire(store)
    assert requests == [httpx.URL(SBI_TER_SOURCE_URL), httpx.URL(SBI_TER_SOURCE_URL)]
    assert first.sha256 != second.sha256
    assert len(first.results) == 1 and first.results[0].outcome == AdapterOutcome.INGESTED
    assert len(second.results) == 1 and second.results[0].outcome == AdapterOutcome.INGESTED
    facts = store.etf_facts(instrument_id, EtfMetric.EXPENSE_RATIO)
    assert {(f.as_of_date, str(f.value)) for f in facts} == {
        (date(2026, 11, 3), "0.052"), (date(2026, 11, 4), "0.071")
    }


@pytest.mark.asyncio
async def test_default_unverified_resolver_never_persists():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: response(workbook_bytes("0.052"))
    )) as client:
        store = SqliteResearchPersistence(":memory:")
        outcome = await SbiTerRuntimeDownloader(Settings(), client=client, clock=lambda: NOW).acquire(store)
    assert outcome.results[0].outcome == AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY
    assert outcome.results[0].fact is None


@pytest.mark.asyncio
async def test_html_login_page_is_rejected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: response(b"<html>login required</html>", content_type="text/html")
    )) as client:
        with pytest.raises(ValueError, match="SBI_TER_UNEXPECTED_CONTENT_TYPE"):
            await SbiTerRuntimeDownloader(Settings(), client=client, clock=lambda: NOW).acquire(
                SqliteResearchPersistence(":memory:")
            )


@pytest.mark.asyncio
async def test_corrupt_xlsx_is_rejected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: response(b"not an xlsx")
    )) as client:
        with pytest.raises(ValueError, match="SBI_TER_WORKBOOK_SIGNATURE_INVALID"):
            await SbiTerRuntimeDownloader(Settings(), client=client, clock=lambda: NOW).acquire(
                SqliteResearchPersistence(":memory:")
            )


@pytest.mark.asyncio
async def test_schema_drift_is_rejected_before_persistence():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: response(workbook_bytes("0.052", headers=("Unknown", "Layout")))
    )) as client:
        store = SqliteResearchPersistence(":memory:")
        with pytest.raises(ValueError, match="WORKBOOK_SCHEMA_UNSUPPORTED_HEADER_ROW"):
            await SbiTerRuntimeDownloader(Settings(), client=client, clock=lambda: NOW).acquire(store)


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected():
    def handler(request):
        if request.url.host == "www.sbimf.com":
            return httpx.Response(302, headers={"Location": "https://other-public.example/current-year-ter.xlsx"})
        return response(workbook_bytes("0.052"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="SBI_TER_CROSS_HOST_REDIRECT_REJECTED"):
            await SbiTerRuntimeDownloader(Settings(), client=client, clock=lambda: NOW).acquire(
                SqliteResearchPersistence(":memory:")
            )


def test_zip_expansion_limit(monkeypatch):
    import app.sbi_ter_runtime_download as target
    monkeypatch.setattr(target, "MAX_SBI_TER_UNCOMPRESSED_BYTES", 128)
    with pytest.raises(ValueError, match="UNCOMPRESSED_LIMIT_EXCEEDED"):
        _validate_xlsx_bytes(workbook_bytes("0.052"))
