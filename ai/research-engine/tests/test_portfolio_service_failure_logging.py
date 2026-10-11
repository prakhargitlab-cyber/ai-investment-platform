"""Regression coverage for the WATCHLIST/instrument-enumeration failure path.

Both /api/v1/research/watchlists and /api/v1/research/market-universe/sectors
collapse ANY non-2xx response from portfolio-service (401, 403, 500, ...) and
any genuine connectivity failure into the same generic
PortfolioServiceUnavailableError. These tests pin two things: (1) that
behavior is unchanged -- callers still see PortfolioServiceUnavailableError
either way, and the client-facing status codes in app.main are untouched --
and (2) the diagnostic log line fires with the real downstream status code
(or "none" for a pure connectivity failure), the exception type, and the
correlation id, so a downstream 401/403 is no longer indistinguishable from
an outage. The log line never includes a response body.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.portfolio_orchestration import (
    PortfolioResearchOrchestrator,
    PortfolioServiceUnavailableError,
)
from app.settings import Settings


def _orchestrator(client: httpx.AsyncClient) -> PortfolioResearchOrchestrator:
    settings = Settings(_env_file=None)
    return PortfolioResearchOrchestrator(
        object(),
        settings,
        client=client,
        structured_provider=object(),
    )


@pytest.mark.asyncio
async def test_watchlist_401_from_portfolio_service_is_logged_with_status_code(caplog) -> None:
    secret_body = "do-not-leak-this-authenticated-user-context-detail"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": secret_body})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.WARNING, logger="app.portfolio_orchestration"):
            with pytest.raises(PortfolioServiceUnavailableError):
                await _orchestrator(client).list_watchlists(
                    correlation_id="corr-abc-123",
                    identity_headers={"X-AIP-User-Id": "11111111-1111-1111-1111-111111111111"},
                )
    finally:
        await client.aclose()

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "statusCode=401" in message
        and "/api/v1/watchlists" in message
        and "correlationId=corr-abc-123" in message
        and "exceptionType=HTTPStatusError" in message
        for message in messages
    )
    assert not any(secret_body in message for message in messages)


@pytest.mark.asyncio
async def test_watchlist_connectivity_failure_is_logged_without_a_status_code(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.WARNING, logger="app.portfolio_orchestration"):
            with pytest.raises(PortfolioServiceUnavailableError):
                await _orchestrator(client).list_watchlists(identity_headers={})
    finally:
        await client.aclose()

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "statusCode=none" in message and "exceptionType=ConnectError" in message and "correlationId=none" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_active_global_equities_403_from_portfolio_service_is_logged(caplog) -> None:
    secret_body = "do-not-leak-this-admin-role-required-detail"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": secret_body})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.WARNING, logger="app.portfolio_orchestration"):
            with pytest.raises(PortfolioServiceUnavailableError):
                await _orchestrator(client).active_global_equities(
                    correlation_id="corr-xyz-789", identity_headers={},
                )
    finally:
        await client.aclose()

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "statusCode=403" in message
        and "/api/v1/instruments" in message
        and "correlationId=corr-xyz-789" in message
        for message in messages
    )
    assert not any(secret_body in message for message in messages)


@pytest.mark.asyncio
async def test_watchlist_success_path_is_unaffected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await _orchestrator(client).list_watchlists(identity_headers={})
    finally:
        await client.aclose()

    assert result == []
