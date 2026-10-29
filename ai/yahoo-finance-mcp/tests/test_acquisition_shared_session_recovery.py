"""Regression pin: YahooAcquisitionService must share ONE YahooTickerContexts
between its structured and historical providers (Yahoo 401 / retry-storm
fix task, item 1 "trace Yahoo API calls... through... Yahoo Finance MCP").

Root cause (confirmed defect): YahooAcquisitionService.__init__ built
self.history = YahooHistoricalPriceProvider(self.ticker_factory) without
passing ticker_contexts=self.structured.ticker_contexts. Because
YahooHistoricalPriceProvider.__init__ falls back to its OWN, brand-new
YahooTickerContexts(ticker_factory) whenever ticker_contexts is omitted,
self.history ended up with a second, independent YahooTickerContexts that
had no session_refresh hook wired at all (only YahooFinanceProvider's own
constructor wires that hook). Through this service, the research-engine
session-rebuild-and-backoff fix for a stale/invalid Yahoo crumb
(note_identity_metadata_unavailable) was therefore silently inert --
exactly matching the production symptom of PROVIDER_IDENTITY_UNAVAILABLE
recurring at an undiminished rate.
"""
from __future__ import annotations

from yahoo_mcp_server.acquisition import YahooAcquisitionService
from yahoo_mcp_server.settings import YahooMcpSettings


class _FakeTicker:
    info: dict = {}

    def history(self, **_kwargs):
        raise RuntimeError("not used in this test")


def test_history_and_structured_providers_share_one_ticker_contexts():
    service = YahooAcquisitionService(YahooMcpSettings(), ticker_factory=lambda symbol: _FakeTicker())
    assert service.history.ticker_contexts is service.structured.ticker_contexts


def test_session_refresh_hook_is_wired_through_the_shared_contexts():
    """The shared contexts must actually have a working refresh hook (not
    just be the same object) -- this is what makes
    note_identity_metadata_unavailable's session rebuild take effect
    through this service's historical-price path.

    No custom ticker_factory here: YahooFinanceProvider deliberately wires
    session_refresh=None when the CALLER supplies its own ticker_factory
    (it would own that session's lifecycle, so there is nothing of the
    provider's own to rebuild -- see structured_market.YahooFinanceProvider
    .__init__'s docstring). Production's actual construction
    (yahoo_mcp_server/server.py: YahooAcquisitionService(config), no
    ticker_factory) takes the other branch, so this test omits it too, to
    pin the real wiring rather than a test-only configuration that the
    provider intentionally disables."""
    service = YahooAcquisitionService(YahooMcpSettings())
    before = service.structured._bounded_session
    refreshed = service.history.ticker_contexts.note_identity_metadata_unavailable()
    assert refreshed is True
    assert service.structured._bounded_session is not before
