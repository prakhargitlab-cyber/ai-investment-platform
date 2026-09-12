from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.portfolio_orchestration import (
    PortfolioResearchOrchestrator,
    PortfolioServiceUnavailableError,
)
from app.settings import Settings


def _orchestrator(settings: Settings, client: httpx.AsyncClient) -> PortfolioResearchOrchestrator:
    return PortfolioResearchOrchestrator(
        object(),
        settings,
        client=client,
        structured_provider=object(),
    )


@pytest.mark.asyncio
async def test_only_nifty_reference_refresh_uses_dedicated_timeout() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"instruments": [], "totalElements": 0})
        return httpx.Response(200, json={"activeUniverse": 498})

    settings = Settings(
        _env_file=None,
        research_request_timeout_seconds=10.0,
        research_connect_timeout_seconds=3.0,
        market_data_nifty_refresh_timeout_seconds=30.0,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(10.0, connect=3.0),
    )
    try:
        assert await _orchestrator(settings, client).india_nifty500_universe() == []
        result = await _orchestrator(settings, client).refresh_india_nifty500_reference()
    finally:
        await client.aclose()

    assert result == {"activeUniverse": 498}
    assert requests[0].extensions["timeout"] == {
        "connect": 3.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    assert requests[1].extensions["timeout"] == {
        "connect": 3.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 30.0,
    }


@pytest.mark.asyncio
async def test_nifty_reference_refresh_timeout_remains_service_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture refresh timeout", request=request)

    settings = Settings(_env_file=None, market_data_nifty_refresh_timeout_seconds=30.0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PortfolioServiceUnavailableError) as captured:
            await _orchestrator(settings, client).refresh_india_nifty500_reference()
    finally:
        await client.aclose()

    assert isinstance(captured.value.__cause__, httpx.ReadTimeout)


def test_nifty_reference_refresh_timeout_setting_uses_aip_environment(monkeypatch) -> None:
    monkeypatch.setenv("AIP_MARKET_DATA_NIFTY_REFRESH_TIMEOUT_SECONDS", "45")

    assert Settings(_env_file=None).market_data_nifty_refresh_timeout_seconds == 45.0


@pytest.mark.parametrize("value", [9.99, 120.01])
def test_nifty_reference_refresh_timeout_setting_is_bounded(value: float) -> None:
    with pytest.raises(ValidationError, match="must be between 10 and 120"):
        Settings(_env_file=None, market_data_nifty_refresh_timeout_seconds=value)
