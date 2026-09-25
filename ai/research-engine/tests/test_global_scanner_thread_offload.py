"""Regression coverage for the GLOBAL SCANNER EVENT-LOOP SAFETY follow-up fix.

The prior fix (test_global_scanner_event_loop_yield.py) only proved that
scan() yields *between* batches. That is insufficient on its own: with the
production batch_size default of 250, a single batch's synchronous
persistence reads + scoring can itself exceed the Kubernetes liveness
timeout, and inter-batch yielding does nothing to protect /health while
that one batch is still executing.

This fix adds an injectable `run_blocking` boundary to GlobalScanner: each
batch's synchronous work (GlobalScanner._score_batch) is now invoked via
`await self.run_blocking(...)` instead of unconditionally inline. In
production, GlobalOpportunityOrchestrator wires this to
ResearchRepository._run_blocking_persistence -- the existing, already
thread-safety-audited executor boundary that offloads to a worker thread
via asyncio.to_thread for the Postgres backend (serialized through a
threading.RLock) and runs inline for the SQLite backend (documented as
"deliberately single-thread-affine", unsafe to move to a worker thread).

These tests exercise GlobalScanner directly with a real asyncio.to_thread
based run_blocking (the same primitive the repository boundary uses). They
deliberately do NOT drive a real SqliteResearchPersistence through
asyncio.to_thread: doing so raises `sqlite3.ProgrammingError: SQLite
objects created in a thread can only be used in that same thread` --
empirical, first-hand confirmation of exactly the constraint
_run_blocking_persistence's own docstring documents, and exactly why
production keeps the SQLite backend inline and only offloads the
thread-safe Postgres/psycopg backend. Since these tests are about
GlobalScanner.run_blocking's event-loop behavior (not about
SqliteResearchPersistence's own thread affinity), they exercise a
thread-safe fake store instead, built once synchronously from real
SqliteResearchPersistence-backed data so its content is identical to what
production scoring would see.

They prove:

  1. The event loop can make progress *during* a single slow batch's
     execution, not merely between batches (the gap the previous fix left
     open).
  2. Without an offloading run_blocking (the default / SQLite path), a
     slow batch still blocks the loop for its own duration -- confirming
     the new run_blocking parameter is what closes the gap, not something
     incidental.
  3. Results/order are unchanged regardless of which run_blocking is used.
  4. Batching stays bounded: _score_batch is invoked once per batch (never
     once per instrument), and every batch's id set stays within
     batch_size.
  5. Offloaded work uses a bounded thread pool, not one thread per stock:
     the number of distinct worker threads observed never exceeds the
     number of batches.

No scoring, eligibility, ordering, concurrency bounds, NSE identity rules,
or persistence semantics are touched by these tests or by the fix itself.
"""
import asyncio
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.global_scanner import GlobalScanner
from app.persistence import SqliteResearchPersistence
from test_global_scanner import instrument, persisted

AS_OF = datetime(2026, 9, 13, tzinfo=timezone.utc)

# Kept well under the real 3s Kubernetes liveness timeout so the suite stays
# fast, but long enough to be unambiguous evidence of blocking vs. not.
_SLOW_BATCH_SECONDS = 0.25


class _FixedUniverse:
    """Minimal EquityUniverse stub: returns a fixed instrument list with no
    HTTP round trip, so these tests isolate scan()'s own batch loop."""

    def __init__(self, items):
        self._items = items

    async def active_global_equities(self, **kwargs):
        return self._items


class _ThreadSafeFakeStore:
    """A persistence double exposing only the read methods GlobalScanner's
    _score_batch needs, backed by plain Python dict/list structures rather
    than a live DB connection -- safe to call from any thread. See the
    module docstring for why a real SqliteResearchPersistence cannot be
    used here instead."""

    def __init__(self, snapshots_by_id, facts_by_id, prices_by_id, delay=0.0):
        self._snapshots_by_id = snapshots_by_id
        self._facts_by_id = facts_by_id
        self._prices_by_id = prices_by_id
        self.delay = delay
        self.call_thread_names = []
        self.call_batch_sizes = []

    def load_structured_market_snapshots(self, ids):
        self.call_thread_names.append(threading.current_thread().name)
        self.call_batch_sizes.append(len(ids))
        if self.delay:
            time.sleep(self.delay)
        return [row for key in ids for row in self._snapshots_by_id.get(key, [])]

    def load_financial_facts(self, ids):
        return [row for key in ids for row in self._facts_by_id.get(key, [])]

    def load_market_price_observations(self, ids):
        return [row for key in ids for row in self._prices_by_id.get(key, [])]


def _seeded_items(count):
    """Populate a real (single-thread) SqliteResearchPersistence with `count`
    instruments, then extract the resulting rows into plain dict/list
    structures grouped by instrument id -- safe to hand to a thread-safe
    fake store afterwards. All real-SQLite interaction here happens
    synchronously on the calling (test) thread only.

    Returns (items, snapshots_by_id, facts_by_id, prices_by_id).
    """
    real_store = SqliteResearchPersistence()
    items = []
    for n in range(1, count + 1):
        item = instrument(n)
        items.append(item)
        persisted(real_store, item)
    ids = {UUID(item["globalInstrumentId"]) for item in items}

    snapshots_by_id = defaultdict(list)
    for row in real_store.load_structured_market_snapshots(ids):
        snapshots_by_id[row.instrument_id].append(row)
    facts_by_id = defaultdict(list)
    for row in real_store.load_financial_facts(ids):
        facts_by_id[row.key.instrument_id].append(row)
    prices_by_id = defaultdict(list)
    for row in real_store.load_market_price_observations(ids):
        prices_by_id[row.instrument_id].append(row)
    return items, snapshots_by_id, facts_by_id, prices_by_id


def _fake_store(count, delay=0.0):
    items, snapshots_by_id, facts_by_id, prices_by_id = _seeded_items(count)
    store = _ThreadSafeFakeStore(snapshots_by_id, facts_by_id, prices_by_id, delay=delay)
    return store, items


async def _to_thread_run_blocking(operation, *args, **kwargs):
    """Stand-in for ResearchRepository._run_blocking_persistence's Postgres
    branch: offload to a worker thread via asyncio.to_thread. Uses the same
    primitive the real production boundary uses."""
    return await asyncio.to_thread(operation, *args, **kwargs)


async def _run_ticker_against(coro):
    """Run `coro` concurrently with a 'ticker' coroutine that only makes
    progress when the event loop is free to schedule something else.
    Returns (result, tick_count)."""
    ticks = 0
    done = False

    async def ticker():
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0.01)

    async def runner():
        nonlocal done
        result = await coro
        done = True
        return result

    ticker_task = asyncio.create_task(ticker())
    result = await runner()
    await ticker_task
    return result, ticks


@pytest.mark.asyncio
async def test_single_slow_batch_blocks_event_loop_without_offload():
    """Control: with the default (inline) run_blocking -- i.e. the previous
    fix's behavior, or scanning against SQLite -- a single batch whose
    persistence read takes _SLOW_BATCH_SECONDS still blocks the event loop
    for that entire duration, because nothing yields *during* the batch.
    This is exactly the gap the follow-up task identified."""
    store, items = _fake_store(3, delay=_SLOW_BATCH_SECONDS)
    scanner = GlobalScanner(_FixedUniverse(items), store, batch_size=250)  # single batch, no offload

    result, ticks = await _run_ticker_against(scanner.scan(as_of=AS_OF, top_n=0))

    assert len(result.candidates) == 3
    # The ticker cannot be scheduled at all while the synchronous
    # time.sleep() is running inline on the event-loop thread.
    assert ticks <= 1, f"expected the event loop to be starved during the slow batch, got {ticks} ticks"


@pytest.mark.asyncio
async def test_single_slow_batch_offloaded_keeps_event_loop_responsive():
    """The fix: with a to_thread-based run_blocking (matching production's
    ResearchRepository._run_blocking_persistence boundary for the Postgres
    backend), the same slow batch's synchronous work executes on a worker
    thread, and the event loop stays free to run other coroutines -- such
    as a liveness probe handler -- for the batch's entire duration, not
    just between batches."""
    store, items = _fake_store(3, delay=_SLOW_BATCH_SECONDS)
    scanner = GlobalScanner(_FixedUniverse(items), store, batch_size=250,
                             run_blocking=_to_thread_run_blocking)  # single batch, offloaded

    result, ticks = await _run_ticker_against(scanner.scan(as_of=AS_OF, top_n=0))

    assert len(result.candidates) == 3
    # Over ~_SLOW_BATCH_SECONDS of wall-clock time, a free event loop polling
    # every 0.01s should tick well more than once.
    assert ticks >= 5, f"expected the event loop to stay responsive during the offloaded batch, got {ticks} ticks"


@pytest.mark.asyncio
async def test_scan_results_identical_with_and_without_offload():
    """The added run_blocking boundary is purely an execution-location
    change: results (ordering, pre_score, eligibility, counts) must be
    byte-for-byte identical whether a batch runs inline or is offloaded via
    asyncio.to_thread, and regardless of batching granularity."""
    items, snapshots_by_id, facts_by_id, prices_by_id = _seeded_items(7)
    store_inline = _ThreadSafeFakeStore(snapshots_by_id, facts_by_id, prices_by_id)
    store_offloaded = _ThreadSafeFakeStore(snapshots_by_id, facts_by_id, prices_by_id)

    inline = await GlobalScanner(_FixedUniverse(items), store_inline, batch_size=250).scan(as_of=AS_OF, top_n=10)
    offloaded = await GlobalScanner(_FixedUniverse(items), store_offloaded, batch_size=2,
                                     run_blocking=_to_thread_run_blocking).scan(as_of=AS_OF, top_n=10)

    assert [c.global_instrument_id for c in inline.candidates] == \
           [c.global_instrument_id for c in offloaded.candidates]
    assert [c.pre_score for c in inline.candidates] == [c.pre_score for c in offloaded.candidates]
    assert [c.eligible_for_deep_analysis for c in inline.candidates] == \
           [c.eligible_for_deep_analysis for c in offloaded.candidates]
    assert inline.eligible_candidates == offloaded.eligible_candidates
    assert inline.acquisition_eligible_count == offloaded.acquisition_eligible_count
    assert inline.deep_analysis_candidate_ids == offloaded.deep_analysis_candidate_ids
    assert inline.total_canonical_active_equities == offloaded.total_canonical_active_equities


@pytest.mark.asyncio
async def test_batching_remains_bounded_not_per_instrument():
    """_score_batch (and therefore the persistence read it wraps) must be
    invoked once per batch -- never once per instrument -- and every
    batch's id set must respect batch_size, with and without offload."""
    store, items = _fake_store(10, delay=0)
    scanner = GlobalScanner(_FixedUniverse(items), store, batch_size=3, run_blocking=_to_thread_run_blocking)

    result = await scanner.scan(as_of=AS_OF, top_n=0)

    assert len(result.candidates) == 10
    # 10 instruments / batch_size 3 -> 4 batches, not 10 individual calls.
    assert len(store.call_batch_sizes) == 4, store.call_batch_sizes
    assert all(size <= 3 for size in store.call_batch_sizes)
    assert sum(store.call_batch_sizes) == 10


@pytest.mark.asyncio
async def test_offload_does_not_spawn_one_thread_per_instrument():
    """The offload boundary must reuse a bounded worker thread pool
    (asyncio.to_thread's default executor), not create one thread per
    stock: the number of distinct OS threads observed executing batch work
    must never exceed the number of batches."""
    store, items = _fake_store(20, delay=0)
    scanner = GlobalScanner(_FixedUniverse(items), store, batch_size=4, run_blocking=_to_thread_run_blocking)

    result = await scanner.scan(as_of=AS_OF, top_n=0)

    assert len(result.candidates) == 20
    num_batches = len(store.call_batch_sizes)
    assert num_batches == 5  # 20 / batch_size 4
    distinct_threads = set(store.call_thread_names)
    assert len(distinct_threads) <= num_batches
    # None of the offloaded work ran on the event-loop's own thread.
    assert threading.current_thread().name not in distinct_threads


@pytest.mark.asyncio
async def test_default_run_blocking_is_inline_passthrough():
    """Sanity/back-compat: constructing GlobalScanner without run_blocking
    (every existing call site and test) must behave exactly as before --
    batch work runs inline, synchronously, on the calling thread."""
    store, items = _fake_store(2, delay=0)
    scanner = GlobalScanner(_FixedUniverse(items), store, batch_size=250)

    result = await scanner.scan(as_of=AS_OF, top_n=0)

    assert len(result.candidates) == 2
    assert store.call_thread_names == [threading.current_thread().name]
