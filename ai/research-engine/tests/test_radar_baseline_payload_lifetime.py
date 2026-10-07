"""Offline FULL baseline: real pool/runtime/executor/provider, synthetic transport."""
import asyncio
import threading
import weakref
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator, _BaselineOutcome
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor, ResearchReadinessRuntime
from app.settings import Settings
from app.structured_market import YahooFinanceProvider
from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor
from app.yahoo_ticker_context import YahooTickerContexts
from test_research_readiness_runtime import RuntimeRepository, StateDataSource, NOW
from test_stock_rule_engine import _readiness


@pytest.mark.asyncio
@pytest.mark.parametrize('timeouts', [False, True])
async def test_full_baseline_releases_completed_provider_graphs(timeouts):
    count = 2434
    live = weakref.WeakValueDictionary()
    lock = threading.Lock()
    created = Counter()
    peaks = {'live': 0}
    persisted = {}
    samples = []

    class RawEvidence:
        def __init__(self, symbol):
            self.body = bytearray(256 * 1024)
            self.symbol = symbol

    class Ticker:
        def __init__(self, symbol):
            self.raw = RawEvidence(symbol)
            self.info = {
                'symbol': symbol, 'exchange': 'NSI', 'currency': 'INR',
                'quoteType': 'EQUITY', 'regularMarketPrice': 250,
                'regularMarketTime': int(NOW.timestamp()),
            }
            with lock:
                created[symbol] += 1
                live[symbol] = self.raw
                peaks['live'] = max(peaks['live'], len(live))

    provider = YahooFinanceProvider(Settings(), client=object(), ticker_factory=Ticker)
    repo = RuntimeRepository()
    repo.profile = lambda key: SimpleNamespace(instrument_id=key, country='IN', exchange='NSE')
    source = StateDataSource(set())
    source.remember_canonical_metadata = lambda *args: None

    async def acquire(key, classes, *, baseline_only, acquisition_context):
        assert baseline_only and classes == {'PRICE'}
        instrument = {'instrumentId': str(key), 'companyName': f'Company {key.int}',
                      'ticker': f'S{key.int}', 'exchange': 'NSE', 'currency': 'INR',
                      'assetType': 'EQUITY', 'structuredProviderTicker': f'S{key.int}.NS',
                      'structuredProviderStatus': 'VERIFIED'}
        snapshot = await provider.collect_baseline(instrument, acquisition_context=acquisition_context)
        if timeouts and key.int % 17 == 0:
            await asyncio.Event().wait()
        # The persistence seam retains the needed fact, never the provider graph.
        persisted[key] = snapshot.facts['latestPrice'].value
        return SimpleNamespace(error=None)

    executor = ExistingResearchCapabilityExecutor(
        repo, SimpleNamespace(ensure_structured_market=acquire, structured_provider=provider), SimpleNamespace())
    runtime = ResearchReadinessRuntime(repo, source, executor, ensure_timeout_seconds=0.5)
    ready = _readiness()
    missing = _readiness({'LATEST_PRICE': ResearchRequirementStatus.MISSING})

    async def read(key, **kwargs):
        return replace(ready if key in persisted else missing, global_instrument_id=key)
    runtime.read = read
    original_ensure = runtime.ensure

    async def observed_ensure(key, **kwargs):
        result = await original_ensure(key, **kwargs)
        # Done callbacks precede the completion delivered to the baseline pool.
        with lock, provider.ticker_contexts._lock:
            assert f'S{key.int}.NS' not in live
            samples.append((len(samples) + 1, len(live),
                            sum(owner.done() for owner, _ in provider.ticker_contexts._contexts)))
        return result
    runtime.ensure = observed_ensure
    service = GlobalOpportunityOrchestrator(
        repo, None, profile_hydrator=lambda *args: True, readiness_runtime=runtime)
    assert service._BASELINE_CONCURRENCY == 8
    candidates = [SimpleNamespace(global_instrument_id=UUID(int=i)) for i in range(1, count + 1)]
    results, failures = await asyncio.wait_for(service._acquire_baseline_requirements(
        candidates, {}, NOW, None), 120)
    await asyncio.sleep(0)
    timed_out = count // 17 if timeouts else 0
    assert len(results) == count and not failures
    assert len(persisted) == count - timed_out
    assert sum(item.ready for item in results.values()) == count - timed_out
    assert sum(item.timed_out for item in results.values()) == timed_out
    assert sum(item.failed for item in results.values()) == timed_out
    assert all(type(item) is _BaselineOutcome for item in results.values())
    assert all(set(vars(item)) == {'planned_requirement_ids', 'ready', 'attempted', 'timed_out', 'failed'}
               for item in results.values())
    assert len(created) == count and set(created.values()) == {1}
    assert len(samples) == count and samples[-1][0] > 50
    retained_at = {n: samples[n - 1][1:] for n in (50, 500, 1500, count)}
    print(f'payload_lifetime timeouts={timeouts} peak={peaks["live"]} samples={retained_at}')
    assert peaks['live'] <= 8, retained_at
    # Other candidates can have just finished in this loop turn, before their
    # done callbacks run. That transient backlog is bounded by the same pool.
    assert all(completed <= 8 for _, _, completed in samples), retained_at
    assert not live and not provider.ticker_contexts._contexts
    assert not runtime._flights and not runtime._evidence_readiness_cache
    assert provider.ticker_contexts.retention_stats() == {'entries': 0, 'completed_task_entries': 0}
    stats = runtime.retention_stats()
    assert stats['active_flights'] == stats['completed_flights'] == 0
    assert stats['yahoo_entries'] == stats['yahoo_completed_task_entries'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_mcp_context_released_when_owner_finishes(cancelled):
    executor = McpFirstResearchCapabilityExecutor(None, None, None, enabled=True)
    refs = []
    entered = asyncio.Event()

    class Payload:
        def __init__(self):
            self.body = bytearray(256 * 1024)

    async def owner():
        payload = Payload()
        refs.append(weakref.ref(payload))
        slot = executor._context_result_slot(asyncio.current_task())
        slot[('instrument', 'LATEST_PRICE')] = payload
        assert executor._context_result_slot(asyncio.current_task()) is slot
        entered.set()
        if cancelled:
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await entered.wait()
    if cancelled:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    await asyncio.sleep(0)
    assert not executor._context_results
    # Drop the caller's own cancelled-task traceback before checking the graph.
    del task
    await asyncio.sleep(0)
    assert refs[0]() is None


@pytest.mark.asyncio
async def test_late_thread_cannot_reregister_finished_ticker_context():
    pool = YahooTickerContexts(lambda symbol: SimpleNamespace(symbol=symbol))

    async def owner():
        with pool.acquire('S.NS', asyncio.current_task()) as access:
            assert access.ticker.symbol == 'S.NS'

    task = asyncio.create_task(owner())
    await task
    await asyncio.sleep(0)
    assert not pool._contexts
    # A to_thread operation may enter acquire after its async owner timed out.
    def late_worker():
        with pool.acquire('S.NS', task) as access:
            assert access.ticker.symbol == 'S.NS'
    await asyncio.to_thread(late_worker)
    assert not pool._contexts


@pytest.mark.asyncio
async def test_cancelled_owner_releases_registry_but_running_thread_keeps_its_work():
    class Payload:
        def __init__(self):
            self.body = bytearray(256 * 1024)

    pool = YahooTickerContexts(lambda _: Payload())
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    refs = []
    loop = asyncio.get_running_loop()

    def blocking_provider(owner):
        try:
            with pool.acquire('S.NS', owner) as access:
                refs.append(weakref.ref(access.ticker))
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
                # Registry cleanup never mutates a ticker still in use.
                assert len(access.ticker.body) == 256 * 1024
            del access
        finally:
            loop.call_soon_threadsafe(finished.set)

    async def owner():
        await asyncio.to_thread(blocking_provider, asyncio.current_task())

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert not pool._contexts
        assert refs[0]() is not None  # Truly active native work, not completed retention.
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 5)
    assert refs[0]() is None
