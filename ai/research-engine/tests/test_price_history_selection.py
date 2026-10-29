"""Source selection over in-memory evidence; no database or provider calls."""
from datetime import date, datetime, time, timezone
from decimal import Decimal
from random import Random
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.market_sessions import MarketCalendarException, MarketTradingSchedule, completed_sessions
from app.models import CompanyResearchProfile, DailyMarketBar, MarketPriceObservation
from app.research_readiness import DurableResearchSnapshot, ResearchReadinessService, ResearchRequirementStatus
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from app.technical_features import TechnicalFeatureEngine, select_price_history


KEY = UUID(int=484)
NOW = datetime(2026, 10, 17, 16, tzinfo=timezone.utc)
BUDGET = date(2026, 2, 1)
URL = 'https://finance.yahoo.com/quote/SELECT.NS/history?interval=1d&auto_adjust=false'


@pytest.fixture
def context():
    # Synthetic weekday calendar plus the verified Budget special session.
    schedules = [MarketTradingSchedule('NSE', 'XNSE', 'IN', 'Asia/Kolkata',
        day, time(9, 15), time(15, 30), True) for day in range(5)]
    exceptions = [MarketCalendarException('NSE', BUDGET, 'SPECIAL_SESSION',
        time(9, 15), time(15, 30), 'Union Budget live trading')]
    sessions = [day for day, _ in completed_sessions('XNSE', schedules, exceptions,
        date(2025, 9, 1), NOW.date(), NOW)][-272:]
    assert BUDGET in sessions
    return SimpleNamespace(days=sessions, options=dict(as_of=NOW, currency='INR',
        market='XNSE', schedules=schedules, exceptions=exceptions, provider_symbol='SELECT.NS'))


def yahoo(context, *, gap=True, provider='YAHOO_FINANCE'):
    return [MarketPriceObservation(instrument_id=KEY, observed_at=datetime.combine(day, time.min, timezone.utc),
        retrieved_at=NOW, price=Decimal(100), currency='INR', provider=provider, source_url=URL)
        for day in context.days if not gap or day != BUDGET]


def nse(day):
    return DailyMarketBar(global_instrument_id=KEY, trading_date=day, open=Decimal(200),
        high=Decimal(201), low=Decimal(199), close=Decimal(200), volume=100,
        currency='INR', provider='NSE', provider_symbol='SELECT', source_mode='REAL',
        source_url='https://www.nseindia.com/history', retrieved_at=NOW)


@pytest.mark.parametrize('provider', ['YAHOO_FINANCE', 'YAHOO_FINANCE_MCP'])
@pytest.mark.parametrize('gap', [False, True])
@pytest.mark.parametrize('daily_count', [1, 19])
def test_sparse_nse_does_not_displace_longer_fresh_yahoo(context, provider, gap, daily_count):
    rows = yahoo(context, gap=gap, provider=provider)
    bars = [nse(BUDGET)] if daily_count == 1 else [nse(day) for day in context.days[-daily_count:]]
    before = [row.model_dump() for row in [*rows, *bars]]
    selected, daily, use_daily, reason = select_price_history(KEY, rows,
        daily_bar_history=bars, **context.options)
    assert not use_daily
    assert len(selected.observations) == (271 if gap else 272)
    assert len(daily.observations) == daily_count
    assert all(row.provider == provider and row.source_url == URL for row in selected.observations)
    assert selected.missing_dates == ((BUDGET,) if gap else ())
    assert selected.valid_until == datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    assert reason == ('NSE_DAILY_HISTORY_STALE' if daily_count == 1 else 'NSE_DAILY_HISTORY_BELOW_20')
    assert [row.model_dump() for row in [*rows, *bars]] == before


@pytest.mark.parametrize('daily_count', [20, 50, 150, 272])
def test_current_complete_nse_retains_preference(context, daily_count):
    bars = [nse(day) for day in context.days[-daily_count:]]
    selected, daily, use_daily, reason = select_price_history(KEY, yahoo(context, gap=False),
        daily_bar_history=bars, **context.options)
    assert use_daily and selected == daily and reason is None
    assert len(selected.observations) == daily_count
    assert all(row.provider == 'NSE' and row.close == 200 for row in selected.observations)


def test_yahoo_gap_remains_blocking_in_readiness_and_technicals(context):
    rows, bars = yahoo(context), [nse(BUDGET)]
    profile = CompanyResearchProfile(instrument_id=KEY, company_id=KEY, company_name='Selection Fixture',
        ticker='SELECT', exchange='NSE', mic='XNSE', currency='INR', country='IN',
        provider_instrument_ids={'YAHOO_FINANCE': 'SELECT.NS'})
    evidence = {name: [] for name in ('HISTORICAL_PRICE_SERIES', 'LATEST_PRICE', 'VALUATION_INPUTS')}
    RepositoryResearchReadinessAdapter._append_market_observations(evidence, rows, profile=profile,
        now=NOW, schedules=context.options['schedules'], exceptions=context.options['exceptions'], bars=bars)
    series = evidence['HISTORICAL_PRICE_SERIES'][0]
    assert not series.complete and series.source_url == URL
    assert {'FIFTY_OBSERVATION_TECHNICAL_BASIS', 'ONE_HUNDRED_FIFTY_OBSERVATION_TECHNICAL_BASIS'} <= set(series.covered_input_ids)
    source = SimpleNamespace(load_by_global_instrument_id=lambda *args: DurableResearchSnapshot(KEY, evidence))
    readiness = ResearchReadinessService(source).assess(KEY, jurisdiction='INDIA', now=NOW)
    result = readiness.for_requirement('HISTORICAL_PRICE_SERIES')
    assert result.status == ResearchRequirementStatus.PARTIAL
    assert result.missing_reason == 'INSUFFICIENT_COMPLETE_EVIDENCE'
    technical = TechnicalFeatureEngine().compute(KEY, rows, daily_bar_history=bars, **context.options)
    assert technical.observation_count == 271 and technical.daily_bar_observation_count == 1
    assert technical.technical_input_source == 'CLOSE_ONLY_FALLBACK'
    assert 'HISTORY_SESSION_GAPS' in technical.missing_inputs
    assert technical.latest_price is technical.dma50 is technical.technical_score is None


def test_selection_is_deterministic_and_does_not_fill_across_providers(context):
    rows = yahoo(context)
    bars = [nse(BUDGET), *[nse(day) for day in context.days[-3:]]]
    baseline = select_price_history(KEY, rows, daily_bar_history=bars, **context.options)
    for seed in (1, 484, 2605):
        reordered_rows, reordered_bars = list(rows), list(bars)
        Random(seed).shuffle(reordered_rows)
        Random(seed).shuffle(reordered_bars)
        assert select_price_history(KEY, reordered_rows, daily_bar_history=reordered_bars, **context.options) == baseline
    assert not baseline[2] and baseline[0].missing_dates == (BUDGET,)
    assert BUDGET not in baseline[0].session_dates and BUDGET in baseline[1].session_dates


def test_stale_gapped_yahoo_is_not_promoted_by_a_fresh_quote(context):
    rows = yahoo(context)[:-10]
    rows.append(rows[-1].model_copy(update={'observed_at': NOW, 'source_url': URL.split('/history')[0]}))
    _, _, use_daily, _ = select_price_history(KEY, rows, daily_bar_history=[nse(BUDGET)], **context.options)
    assert use_daily


def test_latest_nse_conflict_cannot_be_hidden_by_yahoo(context):
    first = nse(BUDGET)
    conflicting = first.model_copy(update={'close': Decimal(201)})
    selected, _, use_daily, reason = select_price_history(KEY, yahoo(context, gap=False),
        daily_bar_history=[first, conflicting], **context.options)
    assert use_daily and selected.current_conflict and reason is None


def test_conflicting_yahoo_cannot_replace_sparse_nse(context):
    rows = yahoo(context)
    rows.append(rows[-1].model_copy(update={'provider': 'YAHOO_FINANCE_MCP', 'price': Decimal(101)}))
    selected, _, use_daily, reason = select_price_history(KEY, rows,
        daily_bar_history=[nse(BUDGET)], **context.options)
    assert use_daily and len(selected.observations) == 1 and reason is None


def test_mature_nse_still_requires_complete_yahoo_to_switch(context):
    bars = [nse(day) for day in context.days[:40]]
    selected, _, use_daily, reason = select_price_history(KEY, yahoo(context),
        daily_bar_history=bars, **context.options)
    assert use_daily and len(selected.observations) == 40 and reason is None
