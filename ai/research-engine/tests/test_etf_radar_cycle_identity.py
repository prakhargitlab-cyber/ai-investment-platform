"""Focused regression tests for the FIX ETF RADAR 401 closure.

Root cause: the ETF Radar background worker's runner called
run_etf_radar_cycle_async() -> portfolio_orchestrator.active_global_etfs()
with no identity headers at all, unlike Equity's runner (which has always
passed _internal_service_identity(...) into run_global_opportunity_cycle --
see tests/test_opportunity_cycle_identity.py, which this file mirrors).
portfolio-service's GET /api/v1/instruments?...assetType=ETF... therefore
rejected the ETF background worker's enumeration call with 401, even though
the identical call for Equity (assetType=EQUITY) already authenticated
correctly with the same server-owned internal identity.
"""
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.market_data_ensure import _internal_service_identity


def _wire_repository(monkeypatch):
    import app.main as main
    from app.persistence import SqliteResearchPersistence
    monkeypatch.setattr(main.repository, "_persistence", SqliteResearchPersistence())
    return main


@pytest.mark.asyncio
async def test_etf_radar_worker_supplies_internal_identity_to_run_etf_radar_cycle_async(monkeypatch):
    """(1) ETF worker supplies internal identity -- main.py's _etf_radar_worker()
    etf_runner must pass the SAME _internal_service_identity(...) the Equity
    worker already passes, not omit it."""
    main = _wire_repository(monkeypatch)
    captured = {}

    async def fake_run_etf_radar_cycle_async(repository, portfolio_orchestrator, **parameters):
        captured.update(parameters)
        return {"cycle_id": "test-cycle", "universe_count": 0}

    monkeypatch.setattr("app.etf_opportunity_cycle.run_etf_radar_cycle_async", fake_run_etf_radar_cycle_async)
    # _etf_radar_worker() is memoized on app.state; a fresh run per test
    # process is fine here since this is the first access in this test.
    worker = main._etf_radar_worker()

    await worker.runner(top_n=10)

    expected = _internal_service_identity(main.portfolio_orchestrator.settings)
    assert captured.get("identity_headers") == expected
    assert captured["identity_headers"]["X-AIP-User-Roles"] == "ADMIN"
    assert captured["top_n"] == 10


@pytest.mark.asyncio
async def test_run_etf_radar_cycle_async_forwards_identity_headers_to_universe_enumeration(monkeypatch):
    """(2) ETF universe enumeration receives the identity -- the library
    function itself must forward identity_headers to
    portfolio_orchestrator.active_global_etfs (downstream of main.py),
    exactly as run_global_opportunity_cycle already does for Equity."""
    from app import etf_opportunity_cycle as cycle
    from app.persistence import SqliteResearchPersistence
    from app.repository import ResearchRepository

    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    orchestrator = AsyncMock()
    orchestrator.active_global_etfs.return_value = []

    server_identity = _internal_service_identity(main_settings())

    result = await cycle.run_etf_radar_cycle_async(
        repository, orchestrator, top_n=10, correlation_id="corr-1", identity_headers=server_identity,
    )

    assert orchestrator.active_global_etfs.await_count == 1
    forwarded = orchestrator.active_global_etfs.call_args.kwargs
    assert forwarded["identity_headers"] == server_identity
    assert forwarded["correlation_id"] == "corr-1"
    assert result["universe_count"] == 0


def main_settings():
    from app.settings import Settings
    return Settings()


@pytest.mark.asyncio
async def test_universe_client_fabricates_no_identity_when_none_is_given(monkeypatch):
    """(3) No identity is fabricated by the universe client -- CanonicalEtfUniverse
    must send exactly the headers it was given, never synthesize a fallback
    identity of its own when identity_headers is None/omitted."""
    import httpx
    from app.etf_universe import CanonicalEtfUniverse

    seen_headers = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"instruments": [], "totalElements": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await CanonicalEtfUniverse(client, "https://portfolio-service.internal").active_global_etfs()

    assert "x-aip-user-id" not in seen_headers
    assert "x-aip-user-roles" not in seen_headers
    assert "authorization" not in seen_headers


@pytest.mark.asyncio
async def test_candidate_ids_path_is_unaffected_by_the_identity_headers_addition(monkeypatch):
    """(4) Existing bounded candidate_ids path remains unchanged -- it builds
    rows from the given ids directly and must still never call
    active_global_etfs at all, identity_headers or not."""
    from app import etf_opportunity_cycle as cycle
    from app.persistence import SqliteResearchPersistence
    from app.repository import ResearchRepository

    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    orchestrator = AsyncMock()

    result = await cycle.run_etf_radar_cycle_async(
        repository, orchestrator, candidate_ids=[UUID(int=1)],
        identity_headers=_internal_service_identity(main_settings()),
    )

    assert orchestrator.active_global_etfs.await_count == 0
    assert result["cycle_id"]
