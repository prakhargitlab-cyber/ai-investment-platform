import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from app.historical_market_data import YahooHistoricalPriceProvider, HistoricalPriceIdentityConflict
from app.structured_market import YahooFinanceProvider
from app.settings import Settings
from app.market_data_population import IndiaMarketDataPopulationJobs
from test_research_readiness_runtime import INSTRUMENT_ID, NOW


class Ticker:
    def __init__(self):
        self.info_reads = 0
        self.history_calls = 0
        self.news = []

    @property
    def info(self):
        self.info_reads += 1
        return {"symbol": "READY.NS", "exchange": "NSI", "currency": "INR",
                "longName": "Readiness India Limited", "quoteType": "EQUITY",
                "regularMarketPrice": 250, "regularMarketTime": int(NOW.timestamp())}

    def history(self, **kwargs):
        self.history_calls += 1
        return pd.DataFrame({"Close": [250]}, index=pd.DatetimeIndex([NOW]))


def instrument(**extra):
    return {"globalInstrumentId": str(INSTRUMENT_ID), "instrumentId": str(INSTRUMENT_ID),
            "companyName": "Readiness India Limited", "structuredProviderTicker": "READY.NS",
            "structuredProviderStatus": "VERIFIED", "exchange": "NSE", "currency": "INR",
            "assetType": "EQUITY", **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("history_first", [False, True])
async def test_structured_and_history_share_one_ticker_and_info_with_full_identity_validation(history_first):
    made = []
    def factory(symbol):
        ticker = Ticker()
        made.append(ticker)
        return ticker
    provider = YahooFinanceProvider(Settings(), client=object(), ticker_factory=factory)
    jobs = IndiaMarketDataPopulationJobs(SimpleNamespace(), None,
        SimpleNamespace(structured_provider=provider), Settings())
    history = jobs.historical_provider
    context = asyncio.current_task()
    async def fetch():
        return await history.closes(instrument(), start=NOW - timedelta(days=2), end=NOW)
    if history_first:
        assert await fetch()
    await provider.collect_baseline(instrument(), acquisition_context=context)
    assert await fetch()
    # Preserve baseline-to-full fallback reuse (including its cache invalidation).
    await provider.collect(instrument(), acquisition_context=context)
    assert len(made) == 1 and made[0].info_reads == 1
    with pytest.raises(HistoricalPriceIdentityConflict):
        await history.closes(instrument(companyName="Unrelated Mining Corporation"), start=NOW, end=NOW)
    assert made[0].info_reads == 1  # identity checked again against returned data
    await provider.collect_baseline(instrument(), acquisition_context=object())
    assert len(made) == 2 and made[1].info_reads == 1


@pytest.mark.asyncio
async def test_concurrent_same_context_access_does_not_duplicate_ticker_or_info():
    factory = Mock(side_effect=lambda _: Ticker())
    provider = YahooFinanceProvider(Settings(), client=object(), ticker_factory=factory)
    history = YahooHistoricalPriceProvider(factory, ticker_contexts=provider.ticker_contexts)
    context = object()
    await asyncio.gather(provider.collect_baseline(instrument(), acquisition_context=context),
                         history.closes(instrument(), start=NOW, end=NOW, acquisition_context=context))
    assert factory.call_count == 1
    with provider.ticker_contexts.acquire("READY.NS", context) as access:
        assert access.ticker.info_reads == 1


@pytest.mark.asyncio
async def test_transient_identity_failure_is_retryable_in_a_new_context():
    class BrokenTicker(Ticker):
        @property
        def info(self):
            raise TimeoutError()
    factory = Mock(side_effect=[BrokenTicker(), Ticker()])
    provider = YahooFinanceProvider(Settings(), client=object(), ticker_factory=factory)
    history = YahooHistoricalPriceProvider(factory, ticker_contexts=provider.ticker_contexts)
    with pytest.raises(Exception, match="HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:TimeoutError"):
        await history.closes(instrument(), start=NOW, end=NOW, acquisition_context=object())
    assert await history.closes(instrument(), start=NOW, end=NOW, acquisition_context=object())
    assert factory.call_count == 2
