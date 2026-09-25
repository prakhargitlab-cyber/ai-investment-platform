"""Regression coverage for the GLOBAL OPPORTUNITY CYCLE event-loop starvation
fix: GlobalScanner.scan() must periodically yield control back to the event
loop while it walks a (potentially large) active-equity universe, so a
full-market scan cannot hold Uvicorn -- including the /health endpoint --
unresponsive for the scan's entire duration.

Before the fix, scan()'s batch loop performed synchronous SQLite reads
(load_structured_market_snapshots / load_financial_facts /
load_market_price_observations) plus a synchronous GlobalPreScore.score()
call per instrument, with zero await/yield points between the initial
universe fetch and the function's return. Declaring the function `async def`
did not make its body cooperative: once started, nothing else on the event
loop -- including a liveness/readiness probe handler -- could run until the
whole batched loop finished.

These tests do not touch scoring, eligibility, ordering, concurrency
bounds, NSE identity rules, or persistence semantics. They only prove (a)
the new yield point actually lets another ready coroutine run interleaved
with a multi-batch scan, and (b) inserting that yield point changes nothing
about scan() results, regardless of how the universe is batched.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from app.global_scanner import GlobalScanner
from app.persistence import SqliteResearchPersistence
from test_global_scanner import instrument, persisted

AS_OF = datetime(2026, 9, 13, tzinfo=timezone.utc)


class _FixedUniverse:
    """Minimal EquityUniverse stub: returns a fixed instrument list with no
    HTTP round trip, so these tests isolate scan()'s own batch loop."""

    def __init__(self, items):
        self._items = items

    async def active_global_equities(self, **kwargs):
        return self._items


def _seeded_store(count):
    store = SqliteResearchPersistence()
    items = []
    for n in range(1, count + 1):
        item = instrument(n)
        items.append(item)
        persisted(store, item)
    return store, items


@pytest.mark.asyncio
async def test_scan_yields_event_loop_between_batches():
    """With batch_size=1 over 5 instruments, scan() performs 5 batch
    iterations and must yield after each one. A concurrently scheduled
    'ticker' coroutine -- which only makes progress when the event loop is
    free to run something else -- must be scheduled multiple times *while
    scan() is still in flight*, proving scan() does not monopolize the loop
    for its full duration."""
    store, items = _seeded_store(5)
    scanner = GlobalScanner(_FixedUniverse(items), store, batch_size=1)

    ticks = 0
    scan_done = False

    async def ticker():
        nonlocal ticks
        while not scan_done:
            ticks += 1
            await asyncio.sleep(0)

    async def run_scan():
        nonlocal scan_done
        result = await scanner.scan(as_of=AS_OF, top_n=0)
        scan_done = True
        return result

    ticker_task = asyncio.create_task(ticker())
    result = await run_scan()
    await ticker_task

    assert len(result.candidates) == 5
    # 5 batches -> 5 yield points -> the ticker must have been interleaved
    # at least a handful of times before scan_done flips.
    assert ticks >= 4, f"expected the event loop to interleave the ticker during the scan, got {ticks} ticks"


@pytest.mark.asyncio
async def test_scan_without_multiple_batches_still_completes():
    """Sanity control: a single-batch scan (batch_size >= universe size)
    still yields exactly once and completes normally -- the fix does not
    require multiple batches to function."""
    store, items = _seeded_store(3)
    scanner = GlobalScanner(_FixedUniverse(items), store, batch_size=250)
    result = await scanner.scan(as_of=AS_OF, top_n=0)
    assert len(result.candidates) == 3


@pytest.mark.asyncio
async def test_scan_results_identical_across_batch_sizes():
    """The added `await asyncio.sleep(0)` is purely a scheduling point: a
    universe walked in one batch vs. many small batches must produce byte-
    for-byte identical candidate ordering, pre_score values, eligibility,
    and counts."""
    store, items = _seeded_store(7)

    single_batch = await GlobalScanner(_FixedUniverse(items), store, batch_size=250).scan(as_of=AS_OF, top_n=10)
    multi_batch = await GlobalScanner(_FixedUniverse(items), store, batch_size=2).scan(as_of=AS_OF, top_n=10)

    assert [c.global_instrument_id for c in single_batch.candidates] == \
           [c.global_instrument_id for c in multi_batch.candidates]
    assert [c.pre_score for c in single_batch.candidates] == \
           [c.pre_score for c in multi_batch.candidates]
    assert [c.eligible_for_deep_analysis for c in single_batch.candidates] == \
           [c.eligible_for_deep_analysis for c in multi_batch.candidates]
    assert single_batch.eligible_candidates == multi_batch.eligible_candidates
    assert single_batch.acquisition_eligible_count == multi_batch.acquisition_eligible_count
    assert single_batch.deep_analysis_candidate_ids == multi_batch.deep_analysis_candidate_ids
    assert single_batch.total_canonical_active_equities == multi_batch.total_canonical_active_equities
