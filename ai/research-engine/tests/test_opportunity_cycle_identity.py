"""Focused regression tests for the Global Opportunity cycle authentication fix.

The opportunity cycle is a server/global job. Its downstream canonical-universe call
to portfolio-service must authenticate with the server-owned internal identity
(X-AIP-User-*), never the caller's browser Authorization / x-user-id headers. A
portfolio-service failure (PortfolioServiceUnavailableError) must surface as a
controlled HTTP 502 (PORTFOLIO_SERVICE_UNAVAILABLE) instead of an unhandled 500.
"""
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException, Request

from app.market_data_ensure import _internal_service_identity
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioServiceUnavailableError


def _browser_request():
    """A caller request carrying browser identity headers the global cycle must NOT reuse."""
    return Request({
        "type": "http",
        "query_string": b"",
        "path": "/api/v1/research/opportunities/cycles",
        "headers": [
            (b"authorization", b"Bearer browser-token-test"),
            (b"x-user-id", b"browser-user-123"),
            (b"x-correlation-id", b"corr-abc"),
        ],
    })


def _wire_repository(monkeypatch):
    import app.main as main
    # In-memory persistence so the handler's publish_opportunity_cycle gate is satisfied,
    # isolated from any prior test wiring.
    monkeypatch.setattr(main.repository, "_persistence", SqliteResearchPersistence())
    return main


@pytest.mark.asyncio
async def test_opportunity_cycle_forwards_server_identity_and_preserves_params(monkeypatch):
    main = _wire_repository(monkeypatch)
    captured = {}

    async def fake_run(repository, canonical_source, *, top_n=None, shortlist_limit=None,
                       candidate_ids=None, identity_headers=None, readiness_runtime=None,
                       analysis_scope=None):
        captured['readiness_runtime'] = readiness_runtime
        captured["identity_headers"] = identity_headers
        captured["top_n"] = top_n
        captured["shortlist_limit"] = shortlist_limit
        captured["candidate_ids"] = candidate_ids
        captured["analysis_scope"] = analysis_scope
        return {"cycle_id": "srv", "universe_count": 1,
                "controlled_candidate_set": bool(candidate_ids)}

    monkeypatch.setattr("app.global_opportunity_cycle.run_global_opportunity_cycle", fake_run)

    body = main.OpportunityCycleRequest(top_n=3, shortlist_limit=10,
                                        candidate_ids=[UUID(int=1), UUID(int=2)])
    result = await main.opportunity_cycle(body, _browser_request())

    expected = _internal_service_identity(main.portfolio_orchestrator.settings)
    # (1) server-owned X-AIP-User-* identity is passed downstream to portfolio-service.
    assert captured["identity_headers"] == expected
    assert captured['readiness_runtime'] is main.research_readiness_runtime
    assert captured["identity_headers"]["X-AIP-User-Id"] == main.portfolio_orchestrator.settings.market_data_internal_user_id
    assert captured["identity_headers"]["X-AIP-User-Issuer"] == main.portfolio_orchestrator.settings.market_data_internal_issuer
    assert captured["identity_headers"]["X-AIP-User-Subject"] == main.portfolio_orchestrator.settings.market_data_internal_subject
    assert captured["identity_headers"]["X-AIP-User-Roles"] == "ADMIN"
    # (2) incoming browser Authorization/user headers are NOT reused as the scanner identity.
    forwarded = {key.lower() for key in captured["identity_headers"]}
    assert "authorization" not in forwarded
    assert "x-user-id" not in forwarded
    # (3) top_n / shortlist_limit / candidate_ids / analysis_scope pass through unchanged.
    assert captured["top_n"] == 3
    assert captured["shortlist_limit"] == 10
    assert captured["candidate_ids"] == [UUID(int=1), UUID(int=2)]
    assert captured["analysis_scope"] == "BOUNDED"
    # (4) existing successful cycle behavior is unchanged.
    assert result == {"cycle_id": "srv", "universe_count": 1, "controlled_candidate_set": True}


@pytest.mark.asyncio
async def test_opportunity_cycle_portfolio_unavailable_returns_controlled_502(monkeypatch):
    main = _wire_repository(monkeypatch)

    async def raise_unavailable(*args, **kwargs):
        raise PortfolioServiceUnavailableError("Portfolio service unavailable for instrument enumeration")

    monkeypatch.setattr("app.global_opportunity_cycle.run_global_opportunity_cycle", raise_unavailable)

    with pytest.raises(HTTPException) as exc:
        await main.opportunity_cycle(main.OpportunityCycleRequest(candidate_ids=[UUID(int=1)]), _browser_request())

    assert exc.value.status_code == 502
    assert exc.value.detail == "PORTFOLIO_SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_run_global_opportunity_cycle_forwards_server_identity_to_universe(monkeypatch):
    """Library-level: run_global_opportunity_cycle propagates identity_headers to the
    canonical-universe call (downstream of main.py), so the server identity reaches
    portfolio-service /api/v1/instruments."""
    from app import global_opportunity_cycle as cycle
    from app.settings import Settings
    from test_global_opportunity_orchestration import setup
    from test_global_scanner import NOW

    service, rows, pairs, store = setup(monkeypatch, 3)

    class Clock:
        @staticmethod
        def now(*args):
            return NOW

    monkeypatch.setattr(cycle, "datetime", Clock)
    monkeypatch.setattr(cycle, "GlobalOpportunityOrchestrator", lambda *a, **k: service)

    source = AsyncMock()
    source.active_global_equities.return_value = rows
    source.sector_benchmark_contexts.return_value = {}

    server_identity = _internal_service_identity(Settings())
    await cycle.run_global_opportunity_cycle(service.repository, source,
                                             candidate_ids=[UUID(int=1)], top_n=2,
                                             identity_headers=server_identity)

    assert source.active_global_equities.await_count == 1
    forwarded = source.active_global_equities.call_args.kwargs["identity_headers"]
    assert forwarded == server_identity
    assert forwarded["X-AIP-User-Roles"] == "ADMIN"
    assert source.sector_benchmark_contexts.call_args.kwargs["identity_headers"] == server_identity
