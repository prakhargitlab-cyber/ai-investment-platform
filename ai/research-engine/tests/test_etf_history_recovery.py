"""Bounded history-only recovery with synthetic, explicitly scoped fixtures."""
import asyncio
import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, CycleOwnershipLost
from app.etf_history_recovery import (
    HistoryRecoveryRequest, RECOVERY_SLOT, RecoveryEtfHistoryProvider,
    plan_history_recovery, recover_etf_history,
)
from app.etf_readiness import evaluate_etf_readiness
from app.historical_market_data import HistoricalPriceProviderError
from app.market_sessions import completed_sessions
from app.models import DailyMarketBar, MarketPriceObservation
from test_etf_ingestion import META, NOW, repo
from test_etf_preflight import compatible_dispatch


SOURCE_ID = str(UUID(int=20000))
START, END = date(2026, 10, 5), date(2026, 10, 10)
REASON = 'HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:YFPricesMissingError'


def canonical(n):
    symbol = f'SYNTHETF{n}'
    return copy.deepcopy(META) | {'globalInstrumentId': str(UUID(int=n)), 'isin': f'IN{n:010d}',
        'primarySymbol': symbol, 'providerMappings': [META['providerMappings'][0] | {'providerSymbol': symbol}]}


async def source(repo, *, count=2, listing_date=None, reason=REASON, success=False):
    rows = [canonical(n) for n in range(1, count + 1)]
    if listing_date is not None:
        rows[0]['listingDate'] = listing_date
    store = repo.persistence
    _, created = store.create_cycle_run(SOURCE_ID, {'fixture': 'SYNTHETIC'}, market='ETF')
    assert created and store.claim_cycle_run(SOURCE_ID, 'source-owner', lease_seconds=600)
    cp = await CycleCheckpoint(store, SOURCE_ID, 'source-owner', run_blocking=repo._run_blocking_persistence).load()
    await cp.save_selection({'universe': [dict(canonical=row, isin=row['isin'], symbol=row['primarySymbol'],
        identity_status='VERIFIED') for row in rows], 'publication': 'SUCCEEDED'})
    for row in rows:
        await cp.start('ETF_ONBOARD', row['isin'])
        await cp.finish('ETF_ONBOARD', row['isin'],
            CandidateState.COMPLETED if success else CandidateState.RETRYABLE_FAILURE,
            disposition='SUCCESS_WITH_DATA' if success else 'TECHNICAL_FAILURE',
            failure_reason=None if success else reason,
            payload={'reason': None if success else reason, 'observations': 0,
                     'start': '2021-10-05', 'end': '2023-10-05'})
    store.save_etf_radar_cycle(SOURCE_ID, 'synthetic', None, NOW,
        {'cycle_id': SOURCE_ID, 'publication': 'SUCCEEDED', 'ranked': []})
    store.update_cycle_run(SOURCE_ID, 'source-owner', status='COMPLETED')
    return rows


def request(rows, **kwargs):
    return HistoryRecoveryRequest(source_cycle_id=SOURCE_ID,
        instrument_ids=tuple(UUID(row['globalInstrumentId']) for row in rows), start=START, end=END, **kwargs)


def orchestrator(rows, **changes):
    async def metadata(key, **kwargs):
        return copy.deepcopy(next(row for row in rows if row['globalInstrumentId'] == str(key))) | changes
    return SimpleNamespace(global_instrument_metadata=AsyncMock(side_effect=metadata))


def bar(row, day, **changes):
    return DailyMarketBar(global_instrument_id=UUID(row['globalInstrumentId']), trading_date=day,
        close=Decimal('100'), currency='INR', provider='YAHOO_FINANCE',
        provider_symbol=row['primarySymbol'] + '.NS', source_mode='REAL',
        source_url=f"https://finance.yahoo.com/quote/{row['primarySymbol']}.NS/history?auto_adjust=false&interval=1d",
        retrieved_at=NOW).model_copy(update=changes)


class SyntheticProvider:
    def __init__(self, repo, budget, *, http_per_window=0, error_before=None, side_effect=None,
                 retrieved_at=NOW):
        self.repo, self.budget = repo, budget
        self.http_per_window, self.error_before, self.side_effect = http_per_window, error_before, side_effect
        self.retrieved_at = retrieved_at
        self.calls = []

    async def closes(self, row, *, start, end):
        self.calls.append((row['globalInstrumentId'], start.date(), end.date()))
        if self.side_effect:
            await self.side_effect(self.budget)
        for _ in range(self.http_per_window):
            await self.budget.reserve_http()
        if self.error_before and start.date() < self.error_before:
            raise HistoricalPriceProviderError(REASON)
        store = self.repo.persistence
        dates = completed_sessions('NSE', store.load_market_schedules({'NSE'}),
            store.load_market_calendar_exceptions({'NSE'}), start.date(), end.date() - timedelta(days=1), NOW)
        return [MarketPriceObservation(instrument_id=UUID(row['globalInstrumentId']),
            observed_at=datetime.combine(day, datetime.min.time(), timezone.utc), price=Decimal('100'),
            currency='INR', provider='YAHOO_FINANCE', source_url=bar(row, day).source_url,
            retrieved_at=self.retrieved_at)
            for day, _ in dates]


async def recover(repo, rows, req=None, **provider_options):
    providers = []

    def factory(budget):
        providers.append(SyntheticProvider(repo, budget, **provider_options))
        return providers[-1]

    result = await recover_etf_history(repo, orchestrator(rows), req or request(rows), now=NOW,
        identity_headers={}, provider_factory=factory, sleep=AsyncMock())
    return result, providers


@pytest.mark.asyncio
async def test_preflight_is_read_only_and_retains_source_failure(repo):
    rows = await source(repo)
    before = repo.persistence._connection.total_changes
    result = await plan_history_recovery(repo, request(rows), now=NOW)
    assert repo.persistence._connection.total_changes == before
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None
    assert len(result['instruments']) == 2
    assert all(item['original_failure'] == REASON for item in result['instruments'])
    assert all(item['canonical_listing_date'] is None for item in result['instruments'])
    assert all(item['listing_boundary'] == 'NOT_INDEPENDENTLY_VERIFIED_NOT_USED' for item in result['instruments'])
    assert result['publication'] == 'UNCHANGED' and result['recommendation'] == 'NOT_EVALUATED'


@pytest.mark.asyncio
@pytest.mark.parametrize('listing_date', [None, '2026-10-05', '2029-01-01'])
async def test_recent_scope_recovers_without_guessing_listing_boundary(repo, listing_date):
    rows = await source(repo, count=1, listing_date=listing_date)
    result, providers = await recover(repo, rows)
    outcome = result['results'][rows[0]['isin']]
    assert result['status'] == 'COMPLETED_APPROVED_RANGE'
    assert outcome['observations'] == outcome['valid_sessions'] == 5
    assert outcome['older_history'] == 'UNRESOLVED_OUTSIDE_APPROVED_RANGE'
    assert providers[0].calls == [(rows[0]['globalInstrumentId'], START, END)]
    _, readiness = evaluate_etf_readiness(repo.persistence, UUID(rows[0]['globalInstrumentId']), now=NOW)
    assert not readiness.ready_for_analysis
    assert outcome['recommendation'] == 'NOT_EVALUATED'


@pytest.mark.asyncio
async def test_newer_gap_succeeds_then_older_error_remains_unresolved(repo):
    rows = await source(repo, count=1)
    repo.persistence.upsert_daily_market_bars([bar(rows[0], date(2026, 10, 8))])
    result, providers = await recover(repo, rows, error_before=date(2026, 10, 8))
    calls = providers[0].calls
    assert calls == [(rows[0]['globalInstrumentId'], date(2026, 10, 9), END),
                     (rows[0]['globalInstrumentId'], START, date(2026, 10, 8))]
    outcome = result['results'][rows[0]['isin']]
    assert result['status'] == 'PARTIAL_FAILURE' and outcome['status'] == 'UNRESOLVED'
    assert outcome['observations'] == 1 and outcome['reason'] == REASON
    assert outcome['remaining_windows'] == [{'start': START.isoformat(), 'end_exclusive': '2026-10-08'}]
    assert 'ABSENT' not in str(outcome)


@pytest.mark.asyncio
async def test_valid_bars_reused_repeated_recovery_has_zero_io_and_source_unchanged(repo):
    rows = await source(repo)
    source_before = copy.deepcopy(repo.persistence.cycle_run(SOURCE_ID))
    progress_before = copy.deepcopy(repo.persistence.cycle_progress(SOURCE_ID))
    publication_before = copy.deepcopy(repo.persistence.etf_radar_cycle(SOURCE_ID))
    repo.persistence.upsert_daily_market_bars([bar(rows[0], day) for day in (START, date(2026, 10, 6))])
    first, providers = await recover(repo, rows)
    assert first['results'][rows[0]['isin']]['observations'] == 3
    assert providers[0].calls[0][1] == date(2026, 10, 7)
    before = [b.model_dump() for b in repo.persistence.load_daily_market_bars({UUID(r['globalInstrumentId']) for r in rows})]
    second, providers = await recover(repo, rows)
    assert providers == [] and second['request_reservations'] == 0
    assert all(item['observations'] == 0 for item in second['results'].values())
    assert before == [b.model_dump() for b in repo.persistence.load_daily_market_bars({UUID(r['globalInstrumentId']) for r in rows})]
    assert repo.persistence.cycle_run(SOURCE_ID) == source_before
    assert repo.persistence.cycle_progress(SOURCE_ID) == progress_before
    assert repo.persistence.etf_radar_cycle(SOURCE_ID) == publication_before


@pytest.mark.asyncio
async def test_invalid_existing_provenance_is_not_reused_or_overwritten(repo):
    rows = await source(repo, count=1)
    invalid = bar(rows[0], START, source_url='https://finance.yahoo.com/quote/SYNTHETF1.NS/history?auto_adjust=true')
    repo.persistence.upsert_daily_market_bars([invalid])
    plan = await plan_history_recovery(repo, request(rows), now=NOW)
    assert plan['instruments'][0]['existing_valid_sessions'] == 0
    result, _ = await recover(repo, rows)
    outcome = result['results'][rows[0]['isin']]
    assert outcome['status'] == 'UNRESOLVED' and outcome['reason'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert outcome['observations'] == outcome['valid_sessions'] == 4
    stored = repo.persistence.load_daily_market_bars({UUID(rows[0]['globalInstrumentId'])})
    assert stored[0].model_dump() == invalid.model_dump()


@pytest.mark.asyncio
async def test_window_budget_exhaustion_preserves_success_and_remaining_gaps(repo):
    rows = await source(repo, count=1)
    repo.persistence.upsert_daily_market_bars([bar(rows[0], day) for day in (date(2026, 10, 6), date(2026, 10, 8))])
    result, providers = await recover(repo, rows, request(rows, max_requests=1))
    assert result['request_reservations'] == len(providers[0].calls) == 1
    assert result['stop_reason'] == 'ETF_REQUEST_BUDGET_EXHAUSTED'
    outcome = result['results'][rows[0]['isin']]
    assert outcome['observations'] == 1 and len(outcome['remaining_windows']) == 2
    assert outcome['status'] == 'UNRESOLVED'


@pytest.mark.asyncio
async def test_actual_http_budget_covers_hidden_provider_requests(repo):
    rows = await source(repo, count=1)
    result, providers = await recover(repo, rows, request(rows, max_http_requests=1), http_per_window=2)
    assert result['http_reservations'] == result['request_reservations'] == len(providers[0].calls) == 1
    assert result['stop_reason'] == 'ETF_HTTP_BUDGET_EXHAUSTED'
    assert result['results'][rows[0]['isin']]['observations'] == 0
    assert repo.persistence.load_daily_market_bars({UUID(rows[0]['globalInstrumentId'])}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['EXTERNAL_PROVIDER_RATE_LIMITED', 'DOWNSTREAM_TIMEOUT',
                                  'EXTERNAL_PROVIDER_UNAVAILABLE:YAHOO_SESSION_BACKOFF_ACTIVE'])
async def test_systemic_provider_failure_stops_before_next_instrument(repo, reason):
    rows = await source(repo)

    async def fail(_):
        raise HistoricalPriceProviderError(reason)

    result, providers = await recover(repo, rows, side_effect=fail)
    assert result['stop_reason'] == reason
    assert len(providers[0].calls) == len(result['results']) == 1
    assert result['results'][rows[0]['isin']]['status'] == 'UNRESOLVED'


@pytest.mark.asyncio
async def test_cancellation_finalizes_only_recovery_checkpoint(repo):
    rows = await source(repo)
    source_before = repo.persistence.cycle_run(SOURCE_ID)
    captured = []

    async def cancel(budget):
        captured.append(budget.checkpoint.cycle_id)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await recover(repo, rows, side_effect=cancel)
    run = repo.persistence.cycle_run(captured[0])
    assert run['status'] == 'CANCELLED' and run['market'] == RECOVERY_SLOT
    assert repo.persistence.cycle_run(SOURCE_ID) == source_before
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None


@pytest.mark.asyncio
async def test_lost_checkpoint_owner_propagates_without_historical_writes(repo):
    rows = await source(repo)
    captured = []

    async def lose_owner(budget):
        captured.append(budget.checkpoint.cycle_id)
        with repo.persistence._connection:
            repo.persistence._connection.execute('UPDATE global_opportunity_cycle_run SET owner_id=? WHERE cycle_id=?',
                ('replacement-owner', budget.checkpoint.cycle_id))

    with pytest.raises(CycleOwnershipLost):
        await recover(repo, rows, side_effect=lose_owner)
    assert repo.persistence.cycle_run(captured[0])['owner_id'] == 'replacement-owner'
    assert repo.persistence.load_daily_market_bars({UUID(r['globalInstrumentId']) for r in rows}) == []


@pytest.mark.asyncio
async def test_current_canonical_change_fails_before_creating_recovery(repo):
    rows = await source(repo)
    factory = AsyncMock()
    with pytest.raises(ValueError, match='CURRENT_CANONICAL_IDENTITY_CONFLICT'):
        await recover_etf_history(repo, orchestrator(rows, isin='IN9999999999'), request(rows), now=NOW,
            identity_headers={}, provider_factory=factory)
    factory.assert_not_called()
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['VERIFIED_HISTORICAL_IDENTITY_MISMATCH:QUOTE_TYPE_MISMATCH',
                                  'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:ISIN_MISMATCH'])
async def test_known_provider_identity_conflict_remains_blocked(repo, reason):
    rows = await source(repo, reason=reason)
    with pytest.raises(ValueError, match='UNRESOLVED_PROVIDER_IDENTITY_CONFLICT'):
        await plan_history_recovery(repo, request(rows), now=NOW)


@pytest.mark.asyncio
async def test_successful_source_history_is_outside_recovery_scope(repo):
    rows = await source(repo, success=True)
    with pytest.raises(ValueError, match='ONLY_FAILED_HISTORY_ALLOWED'):
        await plan_history_recovery(repo, request(rows), now=NOW)


@pytest.mark.parametrize('ids', [(), (UUID(int=1),) * 2, tuple(UUID(int=i) for i in range(6))])
def test_scope_requires_one_to_five_unique_explicit_ids(ids):
    with pytest.raises(ValueError, match='UNIQUE_SCOPE_REQUIRED_MAX_5'):
        HistoryRecoveryRequest(SOURCE_ID, ids, START, END).validate(NOW)


@pytest.mark.parametrize('start,end', [(date(2021, 1, 1), END), (START, START),
                                     (START, date(2026, 10, 11))])
def test_only_explicit_bounded_recent_range_allowed(start, end):
    with pytest.raises(ValueError, match='RECENT_RANGE_REQUIRED'):
        HistoryRecoveryRequest(SOURCE_ID, (UUID(int=1),), start, end).validate(NOW)


@pytest.mark.asyncio
async def test_non_etf_source_is_rejected_without_mutation(repo):
    repo.persistence.create_cycle_run(SOURCE_ID, {}, market='NSE')
    repo.persistence.claim_cycle_run(SOURCE_ID, 'equity-owner')
    repo.persistence.update_cycle_run(SOURCE_ID, 'equity-owner', status='COMPLETED')
    before = copy.deepcopy(repo.persistence.cycle_run(SOURCE_ID))
    with pytest.raises(ValueError, match='COMPLETED_ETF_SOURCE_REQUIRED'):
        await plan_history_recovery(repo, request([canonical(1)]), now=NOW)
    assert repo.persistence.cycle_run(SOURCE_ID) == before


@pytest.mark.asyncio
async def test_equity_rows_and_published_research_are_unchanged(repo):
    rows = await source(repo, count=1)
    equity = bar(canonical(999), START)
    repo.persistence.upsert_daily_market_bars([equity])
    store = repo.persistence
    guarded = ['global_financial_facts', 'global_shareholding_snapshots', 'global_stock_rule_engine_results',
        'research_events', 'research_refresh_runs', 'research_news_search_runs', 'research_event_impact_features',
        'company_business_exposure_profiles', 'global_opportunity_snapshot', 'global_stock_suggestion',
        'global_stock_suggestion_current', 'global_stock_suggestion_history', 'stock_recommendation_history',
        'recommendation_current_state', 'etf_radar_cycles', 'etf_fact_observations', 'etf_nav_observations']
    before = {name: [tuple(row) for row in store._connection.execute('SELECT * FROM ' + name)] for name in guarded}
    await recover(repo, rows)
    assert store.load_daily_market_bars({UUID(int=999)}) == [equity]
    assert before == {name: [tuple(row) for row in store._connection.execute('SELECT * FROM ' + name)] for name in guarded}


@pytest.mark.asyncio
async def test_delayed_retrieval_clock_is_accepted_through_recovery_pipeline(repo):
    rows = await source(repo, count=1)
    retrieved = NOW + timedelta(seconds=5)
    result = await recover_etf_history(repo, orchestrator(rows), request(rows), now=NOW,
        identity_headers={}, sleep=AsyncMock(), clock=lambda: NOW + timedelta(seconds=10),
        provider_factory=lambda budget: SyntheticProvider(repo, budget, retrieved_at=retrieved))
    assert result['status'] == 'COMPLETED_APPROVED_RANGE'
    assert result['results'][rows[0]['isin']]['valid_sessions'] == 5
    assert all(b.retrieved_at == retrieved for b in repo.persistence.load_daily_market_bars({UUID(int=1)}))


@pytest.mark.asyncio
async def test_resume_uses_original_session_horizon_before_later_session_closed(repo):
    rows = await source(repo, count=1)
    req = request(rows)
    recovery_id = str(UUID(int=30000))
    before_friday_close = NOW.replace(hour=9)

    def interrupted_factory(budget):
        raise RuntimeError('SYNTHETIC_INTERRUPTION_BEFORE_PROVIDER_CONSTRUCTION')

    with pytest.raises(RuntimeError, match='SYNTHETIC_INTERRUPTION'):
        await recover_etf_history(repo, orchestrator(rows), req, now=before_friday_close,
            recovery_id=recovery_id, identity_headers={}, provider_factory=interrupted_factory,
            sleep=AsyncMock(), clock=lambda: before_friday_close)
    pending = repo.persistence.cycle_run(recovery_id)
    assert pending['status'] == 'RUNNING' and pending['owner_id'] is None
    assert pending['selection']['as_of'] == before_friday_close.isoformat()
    providers = []

    def factory(budget):
        providers.append(SyntheticProvider(repo, budget))
        return providers[-1]

    resumed = await recover_etf_history(repo, orchestrator(rows), req, now=NOW, recovery_id=recovery_id,
        identity_headers={}, provider_factory=factory, sleep=AsyncMock(), clock=lambda: NOW)
    assert resumed['as_of'] == before_friday_close.isoformat()
    assert resumed['status'] == 'COMPLETED_APPROVED_RANGE'
    assert resumed['results'][rows[0]['isin']]['valid_sessions'] == 4
    assert providers[0].calls == [(rows[0]['globalInstrumentId'], START, date(2026, 10, 9))]
    assert max(b.trading_date for b in repo.persistence.load_daily_market_bars({UUID(int=1)})) == date(2026, 10, 8)


@pytest.mark.asyncio
async def test_completed_progress_payload_rehydrates_after_selection_save_interruption(repo, monkeypatch):
    rows = await source(repo)
    req, recovery_id = request(rows), str(UUID(int=30001))
    original = repo.persistence.update_cycle_run
    interrupted = False

    def interrupt_after_progress(cycle_id, owner_id, **fields):
        nonlocal interrupted
        if cycle_id == recovery_id and not interrupted and (fields.get('selection') or {}).get('results'):
            interrupted = True
            raise RuntimeError('SYNTHETIC_CRASH_AFTER_COMPLETED_PROGRESS')
        return original(cycle_id, owner_id, **fields)

    monkeypatch.setattr(repo.persistence, 'update_cycle_run', interrupt_after_progress)
    with pytest.raises(RuntimeError, match='SYNTHETIC_CRASH_AFTER_COMPLETED_PROGRESS'):
        await recover_etf_history(repo, orchestrator(rows), req, now=NOW, recovery_id=recovery_id,
            identity_headers={}, provider_factory=lambda budget: SyntheticProvider(repo, budget),
            sleep=AsyncMock(), clock=lambda: NOW)
    run = repo.persistence.cycle_run(recovery_id)
    assert run['selection']['results'] == {} and run['owner_id'] is None
    progress = repo.persistence.cycle_progress(recovery_id)
    assert progress[('ETF_ONBOARD', rows[0]['isin'])]['state'] == 'COMPLETED'
    monkeypatch.setattr(repo.persistence, 'update_cycle_run', original)
    providers = []

    def factory(budget):
        providers.append(SyntheticProvider(repo, budget))
        return providers[-1]

    resumed = await recover_etf_history(repo, orchestrator(rows), req, now=NOW, recovery_id=recovery_id,
        identity_headers={}, provider_factory=factory, sleep=AsyncMock(), clock=lambda: NOW)
    assert resumed['status'] == 'COMPLETED_APPROVED_RANGE' and len(resumed['results']) == 2
    assert providers[0].calls == [(rows[1]['globalInstrumentId'], START, END)]
    assert resumed['request_reservations'] == 2


@pytest.mark.asyncio
async def test_accepted_exact_contract_resumes_after_preclaim_interruption(repo):
    rows = await source(repo, count=1)
    req, recovery_id = request(rows), str(UUID(int=30002))
    _, created = repo.persistence.create_cycle_run(recovery_id, req.parameters(), market=RECOVERY_SLOT)
    assert created and repo.persistence.cycle_run(recovery_id)['status'] == 'ACCEPTED'
    resumed = await recover_etf_history(repo, orchestrator(rows), req, now=NOW, recovery_id=recovery_id,
        identity_headers={}, provider_factory=lambda budget: SyntheticProvider(repo, budget),
        sleep=AsyncMock(), clock=lambda: NOW)
    assert resumed['status'] == 'COMPLETED_APPROVED_RANGE'
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['COMPLETED', 'FAILED', 'CANCELLED'])
async def test_terminal_recovery_id_cannot_be_reused(repo, terminal):
    rows = await source(repo, count=1)
    req, recovery_id = request(rows), str(UUID(int=30003))
    repo.persistence.create_cycle_run(recovery_id, req.parameters(), market=RECOVERY_SLOT)
    assert repo.persistence.claim_cycle_run(recovery_id, 'prior-owner')
    repo.persistence.update_cycle_run(recovery_id, 'prior-owner', status=terminal)
    before = copy.deepcopy(repo.persistence.cycle_run(recovery_id))
    provider_factory = AsyncMock()
    with pytest.raises(ValueError, match='ETF_RECOVERY_RESUME_CONTRACT_MISMATCH'):
        await recover_etf_history(repo, orchestrator(rows), req, now=NOW, recovery_id=recovery_id,
            identity_headers={}, provider_factory=provider_factory, sleep=AsyncMock())
    provider_factory.assert_not_called()
    assert repo.persistence.cycle_run(recovery_id) == before
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None
    assert repo.persistence.load_daily_market_bars({UUID(rows[0]['globalInstrumentId'])}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation,reason', [
    ("UPDATE market_trading_schedules SET enabled=0", 'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE'),
    ("UPDATE market_trading_schedules SET timezone='Synthetic/Invalid'", 'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE'),
    ("UPDATE market_trading_schedules SET regular_close_time='08:00'", 'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE'),
    ("INSERT INTO market_trading_calendar_exceptions(market_code,trading_date,exception_type) "
     "VALUES('NSE','2026-10-07','SPECIAL_SESSION')", 'HISTORICAL_SESSION_HOURS_UNVERIFIED'),
])
async def test_unsupported_calendar_fails_before_identity_or_provider_io(repo, mutation, reason):
    rows = await source(repo, count=1)
    with repo.persistence._connection:
        repo.persistence._connection.execute(mutation)
    metadata = orchestrator(rows)
    factory = AsyncMock()
    with pytest.raises(ValueError, match=reason):
        await recover_etf_history(repo, metadata, request(rows), now=NOW, identity_headers={},
            provider_factory=factory, sleep=AsyncMock())
    metadata.global_instrument_metadata.assert_not_awaited()
    factory.assert_not_called()
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None


@pytest.mark.asyncio
async def test_durable_cancellation_during_provider_work_removes_only_recovery_slot(repo):
    rows = await source(repo)
    source_before = copy.deepcopy(repo.persistence.cycle_run(SOURCE_ID))
    captured = []

    async def cancel_request(budget):
        captured.append(budget.checkpoint.cycle_id)
        assert repo.persistence.request_cycle_cancel(budget.checkpoint.cycle_id)

    with pytest.raises(CycleOwnershipLost):
        await recover(repo, rows, side_effect=cancel_request)
    assert repo.persistence.cycle_run(captured[0])['status'] == 'CANCELLED'
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None
    assert repo.persistence.cycle_run(SOURCE_ID) == source_before
    assert repo.persistence.load_daily_market_bars({UUID(r['globalInstrumentId']) for r in rows}) == []


@pytest.mark.asyncio
async def test_recovery_provider_never_widens_single_session_error_into_neighbour_probe():
    from yfinance.exceptions import YFPricesMissingError
    calls = []

    def history(**kwargs):
        calls.append(kwargs)
        raise YFPricesMissingError('SYNTHETF1.NS', 'Synthetic explicit chart failure')

    provider = RecoveryEtfHistoryProvider(lambda symbol: SimpleNamespace(history=history))
    with pytest.raises(HistoricalPriceProviderError):
        await provider.closes(canonical(1) | {'structuredProviderTicker': 'SYNTHETF1.NS'},
            start=datetime(2026, 10, 5, tzinfo=timezone.utc), end=datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert len(calls) == 1
    assert calls[0]['start'] == START and calls[0]['end'] == date(2026, 10, 6)
