"""Offline regressions for completed ranged attempts and ETF history budgets."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from yfinance.exceptions import YFRateLimitError

from app.etf_full_universe import BudgetClient, _ensure_etf_history
from app.historical_market_data import (
    HistoricalPricePopulationService, HistoricalPriceProviderError, YahooHistoricalPriceProvider,
)
from app.models import DailyMarketBar
from app.repository import ResearchRepository
from app.research_readiness import ProviderAuthorityRegistry, ResearchRefreshTarget, ResearchRequirementStatus
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor
from app.settings import Settings
from test_equity_session_history import KEY, NOW, candle, dates, profile, store
from test_etf_ingestion import META, repo as etf_repo
from test_etf_full_universe import checkpoint


@pytest.fixture(autouse=True)
def compatible_etf_dispatch(etf_repo):
    async def dispatch(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    etf_repo._run_blocking_persistence = dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [
    (TimeoutError("private upstream detail"), "DOWNSTREAM_TIMEOUT"),
    (YFRateLimitError(), "EXTERNAL_PROVIDER_RATE_LIMITED"),
    (ConnectionError("private upstream detail"), "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:ConnectionError"),
])
async def test_yahoo_history_failures_cannot_become_empty_success(error, expected):
    class Ticker:
        def history(self, **kwargs):
            # Emulate yfinance's documented default of swallowing errors.
            if kwargs.get("raise_errors"):
                raise error
            return None
    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    with pytest.raises(HistoricalPriceProviderError, match=expected):
        await provider.closes({"structuredProviderTicker": "20MICRONS.NS"}, start=NOW-timedelta(days=1), end=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_completed_ranged_attempt_supersedes_old_mcp_timeout_without_erasing_prices(store, monkeypatch, failed):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr("app.research_readiness_runtime.datetime", Clock)
    repository = ResearchRepository(persistence=store, settings=Settings(research_demo_enabled=False))
    p = profile()
    p.listing_date = date(2026, 1, 1)
    repository.profiles.append(p)
    days = dates(store)
    missing = days[30]
    for day in days:
        if day != missing:
            await repository.upsert_market_price_observation_async(candle(day))
    await repository.record_acquisition_observation(KEY, "HISTORICAL_PRICE_SERIES", "YAHOO_FINANCE_MCP",
        "FAILED", NOW-timedelta(days=4), failure_reason="DOWNSTREAM_TIMEOUT")
    closes = AsyncMock(side_effect=HistoricalPriceProviderError("EXTERNAL_PROVIDER_RATE_LIMITED")) if failed else AsyncMock(return_value=[])
    jobs = SimpleNamespace(settings=Settings(), population=HistoricalPricePopulationService(repository, SimpleNamespace(closes=closes)))
    executor = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), jobs)
    target = ResearchRefreshTarget("HISTORICAL_PRICE_SERIES", "TECHNICALS", ResearchRequirementStatus.PARTIAL,
        ProviderAuthorityRegistry.default().policy_for("HISTORICAL_PRICE_SERIES", "INDIA"), ())
    result = await executor.execute_primary(KEY, (target,), jurisdiction="INDIA", correlation_id=None, identity_headers={})
    expected = ("EXTERNAL_PROVIDER_RATE_LIMITED" if failed else
                "EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT")
    assert result.failures["HISTORICAL_PRICE_SERIES"] == expected
    receipt = next(r for r in repository.acquisition_observations_for(KEY) if r["provider"] == "YAHOO_FINANCE")
    assert receipt["outcome"] == ("FAILED" if failed else "SUCCESS_EMPTY")
    assert f"start={missing}&end={missing+timedelta(days=1)}" in receipt["source_url"]
    assert len(store.load_market_price_observations({KEY})) == len(days)-1
    assert closes.await_count == 1


def etf_bar(day, now):
    return DailyMarketBar(global_instrument_id=UUID(META["globalInstrumentId"]), trading_date=day,
        close=Decimal("100"), currency="INR", provider="YAHOO_FINANCE", provider_symbol="TESTETF.NS",
        source_mode="REAL", source_url="https://finance.yahoo.com/quote/TESTETF.NS/history", retrieved_at=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,missing_tail", [(200, False), (253, True), (253, False)])
async def test_etf_reuses_sessions_but_200_closes_do_not_satisfy_year_history(etf_repo, count, missing_tail):
    from app.market_sessions import completed_sessions
    cp = await checkpoint(etf_repo)
    schedules = etf_repo.persistence.load_market_schedules({"NSE"})
    days = [day for day, _ in completed_sessions("NSE", schedules, [], NOW.date()-timedelta(days=400), NOW.date(), NOW)]
    present = days[-count-1:-1] if missing_tail else days[-count:]
    etf_repo.persistence.upsert_daily_market_bars([etf_bar(day, NOW) for day in present])
    provider = SimpleNamespace(closes=AsyncMock(return_value=[]))
    reserve = AsyncMock()
    result = await _ensure_etf_history(etf_repo, META, now=NOW, checkpoint=cp,
                                      provider=provider, before_request=reserve)
    if count == 253 and not missing_tail:
        assert result["status"] == "SUFFICIENT_EXISTING_HISTORY"
        provider.closes.assert_not_called()
        reserve.assert_not_called()
    else:
        assert result["status"] == "SUCCESS_EMPTY"
        assert provider.closes.await_count == reserve.await_count == 1
        request = provider.closes.call_args.kwargs
        assert request["start"].date() == (days[-1] if missing_tail else days[0])
        last_missing = days[-1] if missing_tail else days[days.index(present[0])-1]
        assert request["end"].date() == last_missing+timedelta(days=1)
        assert not any(request["start"].date() <= day < request["end"].date() for day in present)


@pytest.mark.asyncio
async def test_etf_history_reserves_shared_budget_before_network_and_preserves_cancel_fence(etf_repo):
    cp = await checkpoint(etf_repo)
    selection = {"limits": {"max_requests": 1}, "request_reservations": 1}
    budget = BudgetClient(None, cp, selection, sleep=AsyncMock())
    provider = SimpleNamespace(closes=AsyncMock(return_value=[]))
    with pytest.raises(RuntimeError, match="ETF_REQUEST_BUDGET_EXHAUSTED"):
        await _ensure_etf_history(etf_repo, META, now=NOW, checkpoint=cp, provider=provider,
                                  before_request=budget.reserve_request)
    provider.closes.assert_not_called()
    assert selection["request_reservations"] == 1
    etf_repo.persistence.request_cycle_cancel(cp.cycle_id)
    fresh_budget = BudgetClient(None, cp, {"limits": {"max_requests": 2}, "request_reservations": 0})
    from app.cycle_checkpoint import CycleOwnershipLost
    with pytest.raises(CycleOwnershipLost):
        await _ensure_etf_history(etf_repo, META, now=NOW, checkpoint=cp, provider=provider,
                                  before_request=fresh_budget.reserve_request)
    provider.closes.assert_not_called()


@pytest.mark.asyncio
async def test_completed_range_survives_later_failure_and_retry_skips_it(store, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr("app.research_readiness_runtime.datetime", Clock)
    repository = ResearchRepository(persistence=store, settings=Settings(research_demo_enabled=False))
    p = profile()
    p.listing_date = date(2026, 10, 9)
    repository.profiles.append(p)
    for day in (date(2026, 10, 12), date(2026, 10, 14)):
        await repository.upsert_market_price_observation_async(candle(day))
    closes = AsyncMock(side_effect=[
        [candle(date(2026, 10, 9))], HistoricalPriceProviderError("DOWNSTREAM_TIMEOUT"),
    ])
    jobs = SimpleNamespace(settings=Settings(), population=HistoricalPricePopulationService(repository, SimpleNamespace(closes=closes)))
    executor = ExistingResearchCapabilityExecutor(repository, SimpleNamespace(), jobs)
    with pytest.raises(HistoricalPriceProviderError, match="DOWNSTREAM_TIMEOUT"):
        await executor._ensure_historical_prices(KEY)
    assert len(store.load_market_price_observations({KEY})) == 3
    receipt = next(r for r in repository.acquisition_observations_for(KEY)
                   if r["requirement_id"] == "HISTORICAL_PRICE_SERIES")
    assert receipt["evidence_count"] == 1 and receipt["outcome"] == "FAILED"
    async def remaining(instrument, *, start, end):
        assert start.date() > date(2026, 10, 9)
        return [candle(day) for day in dates(store, start=start.date()) if day < end.date()]
    closes.side_effect = remaining
    assert await executor._ensure_historical_prices(KEY) == 3
    assert len(store.load_market_price_observations({KEY})) == 6


@pytest.mark.asyncio
async def test_etf_resume_keeps_original_universe_limit(etf_repo):
    import httpx
    from app.etf_full_universe import run_full_etf_cycle
    from test_etf_full_universe import universe
    rows, csv = universe(2)
    cp = await checkpoint(etf_repo)
    await cp.save_selection({"scope": "FULL_OFFICIAL_NSE_ETF", "request_reservations": 0,
                            "limits": {"max_requests": 10, "max_universe": 1, "max_seconds": 30}})
    source = SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, text=csv,
        request=httpx.Request("GET", "https://nsearchives.nseindia.com/content/equities/etf.csv"))))
    with pytest.raises(ValueError, match="OFFICIAL_UNIVERSE_EMPTY_OR_RESOURCE_LIMIT_EXCEEDED"):
        await run_full_etf_cycle(etf_repo, source, checkpoint=cp, cycle_id=cp.cycle_id, identity_headers={},
            client=client, limits={"max_requests": 100, "max_universe": 100, "max_seconds": 100})
    assert client.get.await_count == 1
