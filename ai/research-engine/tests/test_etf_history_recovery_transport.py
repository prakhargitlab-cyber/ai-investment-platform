"""Synthetic transport only; exercise the actual threaded Yahoo budget wrapper."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pandas as pd
import pytest

from app.cycle_checkpoint import CycleCheckpoint
from app.etf_history_recovery import RECOVERY_SLOT, RecoveryBudget, live_recovery_provider
from app.historical_market_data import HistoricalPriceProviderError
from test_etf_history_recovery import canonical
from test_etf_ingestion import repo
from test_etf_preflight import compatible_dispatch


START = datetime(2026, 10, 5, tzinfo=timezone.utc)
END = datetime(2026, 10, 7, tzinfo=timezone.utc)


class SyntheticSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []

    def request(self, method, url, *args, **kwargs):
        self.requests.append((method, url))
        return SimpleNamespace(status_code=self.statuses.pop(0) if self.statuses else 200)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)


def synthetic_transport(monkeypatch, row, statuses=(200,), *, timeout=10):
    """Fake HTTP and Yahoo records; retain production session wrapping/identity."""
    session = SyntheticSession(statuses)
    factory = []

    def bounded_session(seconds):
        factory.append(seconds)
        return session

    class SyntheticTicker:
        def __init__(self, symbol, session):
            self.symbol, self.session = symbol, session

        def history(self, **kwargs):
            response = self.session.get('https://synthetic.invalid/chart')
            if response.status_code != 200:
                # Model a hidden library retry; every attempt must reserve.
                response = self.session.get('https://synthetic.invalid/chart-retry')
            if response.status_code != 200:
                raise RuntimeError('SYNTHETIC_HTTP_FAILURE')
            return pd.DataFrame({'Close': [100]}, index=pd.DatetimeIndex([START]))

        @property
        def info(self):
            # Identity metadata is additional underlying I/O, not another window.
            self.session.get('https://synthetic.invalid/identity')
            self.session.post('https://synthetic.invalid/identity-module')
            return dict(symbol=self.symbol, quoteType='ETF', exchange='NSI',
                        currency='INR', isin=row['isin'], longName=row['canonicalName'])

    monkeypatch.setattr('app.structured_market._build_bounded_yahoo_session', bounded_session)
    monkeypatch.setattr('yfinance.Ticker', SyntheticTicker)
    return session, factory, SimpleNamespace(structured_provider_timeout_seconds=timeout)


async def real_budget(repo, *, ceiling):
    store = repo.persistence
    cycle_id = str(UUID(int=30001))
    _, created = store.create_cycle_run(cycle_id, {'kind': RECOVERY_SLOT}, market=RECOVERY_SLOT)
    assert created and store.claim_cycle_run(cycle_id, 'transport-owner', lease_seconds=600)
    checkpoint = await CycleCheckpoint(store, cycle_id, 'transport-owner',
        run_blocking=repo._run_blocking_persistence).load()
    selection = {'limits': {'max_requests': 5, 'max_http_requests': ceiling},
                 'request_reservations': 0, 'http_reservations': 0}
    await checkpoint.save_selection(selection)
    return RecoveryBudget(checkpoint, selection, sleep=AsyncMock())


def durable_reservations(repo, budget):
    return repo.persistence.cycle_run(budget.checkpoint.cycle_id)['selection']['http_reservations']


@pytest.mark.asyncio
async def test_hidden_library_retry_and_identity_requests_are_durably_reserved(repo, monkeypatch):
    row = canonical(1)
    session, factory, settings = synthetic_transport(monkeypatch, row, (500, 200, 200, 200))
    budget = await real_budget(repo, ceiling=4)
    provider = live_recovery_provider(budget, settings)
    observations = await asyncio.wait_for(provider.closes(
        row | {'structuredProviderTicker': row['primarySymbol'] + '.NS'}, start=START, end=END), timeout=3)
    assert len(observations) == 1
    assert observations[0].instrument_id == UUID(row['globalInstrumentId'])
    assert [method for method, _ in session.requests] == ['GET', 'GET', 'GET', 'POST']
    assert budget.selection['http_reservations'] == durable_reservations(repo, budget) == 4
    assert budget.selection['request_reservations'] == 0
    assert factory == [10]
    assert budget.sleep.await_count == 3

    # A resumed budget reads persisted reservations; it cannot regain requests.
    resumed = RecoveryBudget(budget.checkpoint,
        repo.persistence.cycle_run(budget.checkpoint.cycle_id)['selection'], sleep=AsyncMock())
    with pytest.raises(RuntimeError, match='ETF_HTTP_BUDGET_EXHAUSTED'):
        await resumed.reserve_http()
    assert durable_reservations(repo, budget) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('ceiling', [1, 2, 3])
async def test_http_ceiling_blocks_hidden_retry_or_identity_io_before_network(repo, monkeypatch, ceiling):
    row = canonical(1)
    session, _, settings = synthetic_transport(monkeypatch, row, (500, 200, 200, 200))
    budget = await real_budget(repo, ceiling=ceiling)
    provider = live_recovery_provider(budget, settings)
    with pytest.raises(HistoricalPriceProviderError):
        await asyncio.wait_for(provider.closes(
            row | {'structuredProviderTicker': row['primarySymbol'] + '.NS'}, start=START, end=END), timeout=3)
    assert budget.abort == 'ETF_HTTP_BUDGET_EXHAUSTED'
    assert len(session.requests) == durable_reservations(repo, budget) == ceiling
    assert repo.persistence.load_daily_market_bars({UUID(row['globalInstrumentId'])}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason', [(403, 'YAHOO_ACCESS_RESTRICTED'),
                                         (429, 'EXTERNAL_PROVIDER_RATE_LIMITED')])
async def test_access_rejection_blocks_hidden_library_retry_and_later_requests(repo, monkeypatch, status, reason):
    row = canonical(1)
    session, _, settings = synthetic_transport(monkeypatch, row, (status, 200, 200, 200))
    budget = await real_budget(repo, ceiling=10)
    provider = live_recovery_provider(budget, settings)
    with pytest.raises(HistoricalPriceProviderError):
        await asyncio.wait_for(provider.closes(
            row | {'structuredProviderTicker': row['primarySymbol'] + '.NS'}, start=START, end=END), timeout=3)
    assert budget.abort == reason
    assert len(session.requests) == durable_reservations(repo, budget) == 1
    with pytest.raises(RuntimeError, match=reason):
        await asyncio.to_thread(session.get, 'https://synthetic.invalid/after-block')
    assert len(session.requests) == durable_reservations(repo, budget) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('timeout,expected', [(2, 2), (10, 10), (30, 15)])
async def test_existing_bounded_session_factory_keeps_tighter_timeout(repo, monkeypatch, timeout, expected):
    row = canonical(1)
    session, factory, settings = synthetic_transport(monkeypatch, row, timeout=timeout)
    budget = await real_budget(repo, ceiling=4)
    live_recovery_provider(budget, settings)
    assert factory == [expected]
    assert expected <= min(15, timeout)
    assert session.requests == [] and durable_reservations(repo, budget) == 0


@pytest.mark.asyncio
async def test_closed_budget_never_reserves_or_performs_network(repo, monkeypatch):
    row = canonical(1)
    session, _, settings = synthetic_transport(monkeypatch, row)
    budget = await real_budget(repo, ceiling=4)
    live_recovery_provider(budget, settings)
    budget.closed = True
    with pytest.raises(RuntimeError, match='ETF_RECOVERY_STOPPED'):
        await asyncio.wait_for(asyncio.to_thread(session.get, 'https://synthetic.invalid/closed'), timeout=3)
    assert session.requests == []
    assert durable_reservations(repo, budget) == 0
