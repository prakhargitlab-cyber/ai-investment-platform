"""Task 2 regression: real OS thread work cannot exceed the asyncio pool size.

Context (production): baseline acquisition dispatches a synchronous yfinance
call via asyncio.to_thread. _execute_plan_bounded cancels and "abandons" the
owning coroutine after ensure_timeout_seconds -- but a cancelled coroutine
cannot stop the underlying OS thread, which keeps running the synchronous
call until it genuinely returns. If the asyncio-level admission pool (bounded
to 8) keeps launching new candidates on top of abandoned-but-still-running
threads, the number of concurrently alive synchronous operations -- and
whatever memory each one holds (yfinance session objects, response bodies) --
is NOT bounded by the 8 asyncio slots. This proves the growth is real and
that app.structured_market.YahooFinanceProvider._thread_dispatch_semaphore
(acquired before a thread is dispatched, released only by that thread's own
completion) closes it without changing any timeout/retry/evidence/readiness
semantics -- the asyncio-level timeout-and-abandon behavior is reproduced
exactly, unmodified.
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from app.settings import Settings
from app.structured_market import YahooFinanceProvider


class _HangingTicker:
    """A ticker whose `.info` blocks on a real OS-level threading.Event,
    exactly like a hung synchronous network call -- it cannot be interrupted
    by asyncio cancellation of its caller's coroutine."""

    _active = 0
    _peak = 0
    _lock = threading.Lock()

    def __init__(self, symbol, release_event, started_event=None):
        self.symbol = symbol
        self._release = release_event
        self._started = started_event

    @property
    def info(self):
        cls = _HangingTicker
        with cls._lock:
            cls._active += 1
            cls._peak = max(cls._peak, cls._active)
        try:
            if self._started is not None:
                self._started.set()
            # Real synchronous blocking work. asyncio cancellation of the
            # coroutine that dispatched us has no effect on this thread.
            self._release.wait(10)
            return {
                "symbol": self.symbol, "exchange": "NSI", "currency": "INR",
                "quoteType": "EQUITY", "regularMarketPrice": 250,
            }
        finally:
            with cls._lock:
                cls._active -= 1


def _resolution(n):
    from datetime import datetime, timezone
    from app.models import StructuredInstrumentResolution
    return StructuredInstrumentResolution(
        instrument_id=UUID(int=n), provider="YAHOO_FINANCE", provider_ticker=f"S{n}.NS",
        company_name=f"Company {n}", exchange="NSE", currency="INR", quote_type="EQUITY",
        confidence=0.95, resolved_at=datetime.now(timezone.utc), status="VERIFIED_NSE_CANDIDATE",
    )


@pytest.mark.asyncio
async def test_abandoned_timeouts_cannot_exceed_bounded_concurrent_threads():
    """Admit far more candidates than the thread bound, cancelling (abandoning)
    each owner shortly after dispatch -- exactly how _execute_plan_bounded
    behaves on ensure_timeout_seconds expiry. The real number of concurrently
    alive synchronous Yahoo calls (including abandoned stragglers still
    finishing) must never exceed structured_provider_max_concurrent_threads,
    even though 30 admissions are issued and every owner is abandoned almost
    immediately.
    """
    _HangingTicker._active = 0
    _HangingTicker._peak = 0
    release = threading.Event()  # never set until the end: every dispatch hangs

    # This host may have few CPUs, giving asyncio's *default* executor
    # (sized min(32, cpu_count()+4)) fewer than 8 workers on its own --
    # that would conflate "executor queueing" with the semaphore bound this
    # test targets. Give the loop a dedicated, deliberately larger executor
    # so the only thing capping concurrently-*running* synchronous work is
    # the semaphore under test.
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32))

    settings = Settings()
    assert settings.structured_provider_max_concurrent_threads == 8  # existing baseline bound, unchanged
    provider = YahooFinanceProvider(settings, client=object(),
                                     ticker_factory=lambda symbol: _HangingTicker(symbol, release))

    admissions = 30
    abandoned_owners = []
    for n in range(1, admissions + 1):
        resolution = _resolution(n)
        owner = asyncio.create_task(provider._collect_resolved(resolution, baseline_only=True))
        try:
            # Mirrors _execute_plan_bounded: a short observation budget, then
            # cancel-and-abandon (asyncio.wait_for cancels the awaited task on
            # timeout, same as _execute_plan_bounded's task.cancel()). The
            # underlying thread (if it was dispatched) keeps running regardless.
            await asyncio.wait_for(owner, timeout=0.02)
        except asyncio.TimeoutError:
            abandoned_owners.append(owner)
        except asyncio.CancelledError:
            abandoned_owners.append(owner)
        # Let the event loop advance between admissions.
        await asyncio.sleep(0)

    # Give every dispatched-but-abandoned thread a moment to actually reach
    # the blocking `.info` read and register itself as active.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _HangingTicker._peak < settings.structured_provider_max_concurrent_threads:
        await asyncio.sleep(0.02)

    # The critical assertion: 30 admissions, every owner abandoned almost
    # immediately, yet real concurrently-alive synchronous work never
    # exceeded the configured bound.
    assert _HangingTicker._peak <= settings.structured_provider_max_concurrent_threads, (
        f"peak concurrent synchronous threads {_HangingTicker._peak} exceeded "
        f"the bound {settings.structured_provider_max_concurrent_threads} -- "
        "abandoned owners allowed unbounded underlying thread accumulation")
    # And the bound was actually exercised (not trivially satisfied because
    # too few candidates ever got far enough to dispatch).
    assert _HangingTicker._peak == settings.structured_provider_max_concurrent_threads

    # A fresh dispatch attempted right now must genuinely block: all 8 slots
    # are held by hung-but-abandoned threads, none of which have finished.
    resolution = _resolution(999)
    blocked_owner = asyncio.create_task(provider._collect_resolved(resolution, baseline_only=True))
    await asyncio.sleep(0.05)
    assert not blocked_owner.done(), "a 9th dispatch must wait for a real free slot, not just an asyncio-level one"
    blocked_owner.cancel()

    # Drain: release every hung thread and confirm the semaphore -- and the
    # live-thread counter -- fully recover (no permanent starvation/leak).
    release.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _HangingTicker._active > 0:
        await asyncio.sleep(0.02)
    assert _HangingTicker._active == 0
    assert provider._thread_dispatch_semaphore._value == settings.structured_provider_max_concurrent_threads

    # Drain cancelled owner tasks so pytest-asyncio does not warn about
    # pending tasks at teardown.
    for owner in abandoned_owners:
        if not owner.done():
            owner.cancel()
        try:
            await owner
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_successful_dispatches_still_plateau_at_the_bound_not_at_eight_asyncio_slots():
    """Successful (non-abandoned) calls must also respect the thread bound --
    this is not a timeout-only code path. Also proves the fix does not merely
    coincide with the existing 8-candidate asyncio pool: the thread bound is
    independently configurable and is what is actually enforced.
    """
    _HangingTicker._active = 0
    _HangingTicker._peak = 0
    release = threading.Event()

    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32))
    settings = Settings(structured_provider_max_concurrent_threads=3)
    provider = YahooFinanceProvider(settings, client=object(),
                                     ticker_factory=lambda symbol: _HangingTicker(symbol, release))

    owners = [asyncio.create_task(provider._collect_resolved(_resolution(n), baseline_only=True))
              for n in range(1, 11)]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _HangingTicker._peak < 3:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    assert _HangingTicker._peak == 3, f"expected the configured bound 3, saw peak {_HangingTicker._peak}"

    release.set()
    results = await asyncio.gather(*owners, return_exceptions=True)
    assert all(not isinstance(r, Exception) for r in results)
    assert _HangingTicker._active == 0
    assert provider._thread_dispatch_semaphore._value == 3
