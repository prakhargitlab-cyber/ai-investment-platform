"""Mocked discovery through bounded fetch: no live providers."""
from itertools import permutations

import httpx
import pytest

from app.repository import ResearchRepository, _fair_official_filing_order
from app.settings import Settings
from app.source_discovery import OfficialFilingDiscovery
from test_di11c_official_document_budget import _RecordingFetcher


@pytest.mark.asyncio
async def test_financial_candidates_prioritize_supported_recent_results_before_subtype():
    repo = ResearchRepository(settings=Settings(research_official_document_max_attempts_per_refresh=1))
    profile = repo.list_profiles()[0].model_copy(update={
        "country": "IN", "exchange": "NSE", "isin": "INE000A01010",
        "provider_instrument_ids": {"NSE": "SYNTH"},
    })
    root = "https://nsearchives.nseindia.com/corporate/"
    rows = [
        {"desc": "Investor presentation financial results", "attchmntFile": root + "old.pdf", "an_dt": "01-Jan-2020 10:00:00"},
        {"desc": "Financial results", "attchmntFile": root + "archive.zip", "an_dt": "20-Sep-2026 10:00:00"},
        {"desc": "Financial results", "attchmntFile": root + "recent.pdf", "an_dt": "19-Sep-2026 10:00:00"},
        {"desc": "Investor presentation", "attchmntFile": root + "general.pdf", "an_dt": "20-Sep-2026 10:00:00"},
        {"desc": "Financial results", "attchmntFile": root + "wrong.pdf", "an_dt": "20-Sep-2026 10:00:00", "isin": "WRONG", "symbol": "SYNTH"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows))) as client:
        filings = await OfficialFilingDiscovery(client=client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    assert next(f for f in filings if f.source.url.endswith("recent.pdf")).category == "FINANCIAL_RESULTS"
    assert not any(f.source.url.endswith("wrong.pdf") for f in filings)
    for ordering in permutations(filings):
        assert _fair_official_filing_order(list(ordering))[0].source.url == root + "recent.pdf"
    fetcher = _RecordingFetcher()
    repo._fetcher = fetcher
    await repo._fetch_official_filings(profile, filings, set())
    assert fetcher.urls == [root + "recent.pdf"]  # failed real read consumes the one slot
    assert repo.settings.research_official_document_max_attempts_per_refresh == 1


@pytest.mark.asyncio
async def test_financial_date_ties_are_deterministic():
    from dataclasses import replace
    from datetime import datetime, timezone
    from app.source_discovery import DiscoveryResult
    from test_di11c_official_document_budget import _official_source
    repo = ResearchRepository(settings=Settings())
    profile = repo.list_profiles()[0]
    filings = [DiscoveryResult("FINANCIAL_RESULTS", replace(
        _official_source(profile, suffix, "FINANCIAL_RESULTS"),
        official_published_at=datetime(2026, 9, 19, tzinfo=timezone.utc))) for suffix in ("b", "a")]
    assert [f.source.url for f in _fair_official_filing_order(filings)] == sorted(f.source.url for f in filings)
    assert _fair_official_filing_order(filings) == _fair_official_filing_order(list(reversed(filings)))
