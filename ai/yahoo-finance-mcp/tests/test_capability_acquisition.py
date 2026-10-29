"""Verify real tool dispatch does not download unrelated Yahoo capabilities."""
import pytest

from fake_yahoo import FakeYahooTicker
from test_first_party_server import arguments, call_tool, settings
from yahoo_mcp_server.acquisition import YahooAcquisitionService
from yahoo_mcp_server.server import create_yahoo_mcp_server


STATEMENTS = {'income_stmt', 'balance_sheet', 'cashflow', 'quarterly_income_stmt',
              'quarterly_balance_sheet', 'quarterly_cashflow'}


@pytest.mark.parametrize('tool,expected', [
    ('get_quote', set()), ('get_company_profile', set()), ('get_sector_industry', set()),
    ('get_analyst_data', set()), ('get_price_history', {'history'}),
    ('get_financials', {'income_stmt', 'balance_sheet', 'cashflow'}),
    ('get_quarterly_financials', {'quarterly_income_stmt', 'quarterly_balance_sheet', 'quarterly_cashflow'}),
    ('get_news', {'news'}),
])
async def test_each_tool_reads_only_its_required_capabilities(tool, expected):
    reads = []

    class Ticker(FakeYahooTicker):
        def __getattribute__(self, name):
            if name in STATEMENTS | {'news', 'history'}:
                reads.append(name)
            return super().__getattribute__(name)

    source = YahooAcquisitionService(settings(), ticker_factory=lambda symbol: Ticker(symbol))
    instance = create_yahoo_mcp_server(settings(), acquisition=source)
    try:
        result = await call_tool(instance, tool, arguments())
        assert not result.is_error
        assert set(reads) == expected
    finally:
        await source.close()
