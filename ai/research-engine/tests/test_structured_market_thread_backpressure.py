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


# --- Regression: yfinance's own 30s-per-request default exceeds
# ResearchReadinessRuntime's ensure_timeout_seconds=25.0 background-cycle
# acquisition budget, and the thread-dispatch semaphore above only bounds
# COUNT, not DURATION -- a slow-but-not-literally-hung Yahoo response still
# holds its slot for however long it actually takes. app.structured_market
# ._build_bounded_yahoo_session forces every underlying HTTP request's
# timeout down to structured_provider_timeout_seconds so a stalled/slow call
# fails fast and frees its slot well inside the 25s observation window --
# WITHOUT discarding yfinance's own preferred transport (curl_cffi with
# Chrome TLS impersonation when installed, confirmed installed in this
# codebase's environment) for a plain requests.Session that would lose that
# anti-bot fingerprinting. ---

def test_bounded_yahoo_session_caps_every_request_timeout_on_whichever_backend_is_active():
    """Backend-agnostic: must work whether yfinance.data.new_session()
    returns a curl_cffi Session (Chrome impersonation -- the preferred
    backend, confirmed installed in this environment) or a plain
    requests.Session fallback. This factory deliberately reuses yfinance's
    OWN session-construction function rather than reimplementing it, so
    whichever backend yfinance would have picked for itself is preserved;
    only the per-request timeout is capped on top of it.
    """
    import app.structured_market as sm

    class _FakeRealSession:
        """Stands in for whatever yf.data.new_session() would really
        return (curl_cffi or requests) -- only .request matters here."""
        def __init__(self):
            self.calls = []

        def request(self, method, url, *args, **kwargs):
            self.calls.append(kwargs.get("timeout"))
            return "stubbed-response"

    fake_real_session = _FakeRealSession()
    original_new_session = sm.yf.data.new_session
    sm.yf.data.new_session = lambda: fake_real_session
    try:
        session = sm._build_bounded_yahoo_session(bound_seconds=8.0)
    finally:
        sm.yf.data.new_session = original_new_session

    # The factory must hand back yfinance's OWN session object (so its
    # cookies/impersonation/connection pooling are untouched), not a
    # replacement -- only .request is wrapped on top of it.
    assert session is fake_real_session

    # yfinance's YfData.get()/.post() always pass an explicit timeout
    # (hardcoded default 30s in yfinance's own code) -- a longer requested
    # timeout must be clamped down to the configured bound before reaching
    # the real transport.
    session.request("GET", "https://example.invalid", timeout=30)
    assert fake_real_session.calls[-1] == 8.0

    # A caller that omits timeout entirely still gets the bound applied,
    # never an unbounded request.
    session.request("GET", "https://example.invalid")
    assert fake_real_session.calls[-1] == 8.0

    # A request that already asks for something SHORTER than the bound is
    # left alone -- this only caps, never extends.
    session.request("GET", "https://example.invalid", timeout=2.0)
    assert fake_real_session.calls[-1] == 2.0



def test_yahoo_finance_provider_default_ticker_factory_uses_bounded_session():
    """The confirmed fix site: when no ticker_factory override is injected
    (the real production path, as opposed to every other test in this
    file, which injects its own factory), YahooFinanceProvider must hand
    yfinance a session whose per-request timeout is capped at
    structured_provider_timeout_seconds -- built from yfinance's own
    preferred session (new_session()), not a hand-rolled requests.Session
    that would silently drop curl_cffi's Chrome impersonation.
    """
    from app.structured_market import YahooFinanceProvider

    settings = Settings()
    provider = YahooFinanceProvider(settings, client=object())

    captured = {}

    def fake_ticker_ctor(symbol, session=None):
        captured["symbol"] = symbol
        captured["session"] = session
        return object()

    import app.structured_market as sm
    original = sm.yf.Ticker
    sm.yf.Ticker = fake_ticker_ctor
    try:
        provider.ticker_factory("RELIANCE.NS")
    finally:
        sm.yf.Ticker = original

    assert captured["symbol"] == "RELIANCE.NS"
    assert captured["session"] is provider._bounded_session
    # _build_bounded_yahoo_session stamps this for introspection/testing --
    # it is yfinance's own real session object (curl_cffi or requests,
    # whichever new_session() picked), not a dedicated subclass.
    assert captured["session"]._bound_seconds == settings.structured_provider_timeout_seconds
