"""Slice 6: fresh-DB acquisition efficiency -- measured, deterministic, offline.

Counts application-owned work (HTTP requests, discovery calls) rather than
wall time, and asserts required evidence is unchanged.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from app.models import CompanyResearchProfile
from app.source_discovery import OfficialFilingDiscovery

ROWS = [
    {"an_dt": "14-Aug-2026 18:53:06", "desc": "Financial Results", "category": "Financial Results",
     "attchmntText": "Unaudited financial results for the quarter ended June 30, 2026",
     "attchmntFile": "https://nsearchives.nseindia.com/corporate/X_results_jun.pdf"},
    {"an_dt": "10-Aug-2026 10:00:00", "desc": "Appointment of Director", "attchmntText": "Appointment of director",
     "attchmntFile": "https://nsearchives.nseindia.com/corporate/X_director.pdf"},
    {"an_dt": "05-Aug-2026 10:00:00", "desc": "Order worth Rs 500 crore", "attchmntText": "New order worth Rs 500 crore contract",
     "attchmntFile": "https://nsearchives.nseindia.com/corporate/X_order.pdf"},
]


def _profile():
    return CompanyResearchProfile(
        instrument_id=UUID(int=77), company_id=UUID(int=78), company_name="Example Industries Limited",
        isin="INE000X01010", ticker="EXAMPLEX", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "EXAMPLEX"})


def _discovery(counter, rows=ROWS, status=200):
    def handle(request):
        counter.append(str(request.url))
        return httpx.Response(status, json=rows, request=request)
    return OfficialFilingDiscovery(httpx.AsyncClient(transport=httpx.MockTransport(handle)))


@pytest.mark.asyncio
async def test_one_instrument_investigation_fetches_nse_announcements_once():
    """Deep investigation asks discovery once per requirement group (financial
    results, governance, business). They share one announcements list."""
    calls: list[str] = []
    discovery = _discovery(calls)
    profile = _profile()
    financial = await discovery.discover(profile, {"FINANCIAL_RESULTS"}, set())
    governance = await discovery.discover(profile, {"RISKS", "REGULATORY", "MANAGEMENT"}, set())
    business = await discovery.discover(profile, {"ORDERS_BACKLOG", "CONTRACTS"}, set())
    assert len(calls) == 1, calls
    # Evidence selection per requirement is unchanged by the shared list.
    assert [r.source.url.rsplit("/", 1)[1] for r in financial] == ["X_results_jun.pdf"]
    assert [r.source.url.rsplit("/", 1)[1] for r in governance] == ["X_director.pdf"]
    assert {r.source.url.rsplit("/", 1)[1] for r in business} == {"X_order.pdf"}


@pytest.mark.asyncio
async def test_concurrent_callers_share_a_single_request():
    calls: list[str] = []
    discovery = _discovery(calls)
    profile = _profile()
    await asyncio.gather(*(discovery.discover(profile, {"FINANCIAL_RESULTS"}, set()) for _ in range(5)))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failures_are_not_cached_and_ttl_expires():
    calls: list[str] = []
    failing = _discovery(calls, status=503)
    with pytest.raises(Exception):
        await failing.discover(_profile(), {"FINANCIAL_RESULTS"}, set())
    with pytest.raises(Exception):
        await failing.discover(_profile(), {"FINANCIAL_RESULTS"}, set())
    assert len(calls) == 2  # a failure is retried, never served from cache
    ok_calls: list[str] = []
    fresh = _discovery(ok_calls)
    fresh.rows_ttl_seconds = 0
    await fresh.discover(_profile(), {"FINANCIAL_RESULTS"}, set())
    await fresh.discover(_profile(), {"FINANCIAL_RESULTS"}, set())
    assert len(ok_calls) == 2


def test_rows_cache_is_bounded():
    discovery = _discovery([])
    assert discovery.rows_cache_max_symbols <= 64
