import pytest

from app.historical_market_data import HistoricalPriceIdentityConflict, HistoricalPriceProviderError
from fake_yahoo import FakeYahooTicker
from test_first_party_server import arguments, call_tool, settings
from yahoo_mcp_server.acquisition import YahooAcquisitionService
from yahoo_mcp_server.server import create_yahoo_mcp_server


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [
    (HistoricalPriceProviderError("DOWNSTREAM_TIMEOUT"), "YAHOO_MCP_UPSTREAM_TIMEOUT"),
    (HistoricalPriceProviderError("EXTERNAL_PROVIDER_RATE_LIMITED"), "YAHOO_MCP_RATE_LIMITED"),
    (HistoricalPriceIdentityConflict("COMPANY_NAME_MISMATCH"), "YAHOO_MCP_IDENTITY_MISMATCH"),
])
async def test_history_preserves_safe_failure_classification(error, code):
    source = YahooAcquisitionService(settings(), ticker_factory=FakeYahooTicker)
    def failed(*args):
        raise error
    source.history._closes = failed
    try:
        result = await call_tool(create_yahoo_mcp_server(settings(), acquisition=source),
                                 "get_price_history", arguments())
        assert result.is_error
        assert code in result.content[0].text
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_default_history_ticker_uses_existing_bounded_transport(monkeypatch):
    sessions = []
    def ticker(symbol, *, session=None):
        sessions.append(session)
        return FakeYahooTicker(symbol)
    monkeypatch.setattr("yfinance.Ticker", ticker)
    source = YahooAcquisitionService(settings())
    try:
        result = await call_tool(create_yahoo_mcp_server(settings(), acquisition=source),
                                 "get_price_history", arguments())
        assert not result.is_error
        assert len(sessions) == 1
        assert sessions[0]._bound_seconds == source.settings.upstream_timeout_seconds
    finally:
        await source.close()
