"""Bounded, caller-owned Yahoo reuse; never share mutable tickers concurrently."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


@dataclass
class _TickerAccess:
    factory: Any
    symbol: str
    lock: Any = field(default_factory=RLock)
    _ticker: Any = None
    _info: Any = None
    structured_failure: str | None = None

    @property
    def ticker(self):
        if self._ticker is None:
            self._ticker = self.factory(self.symbol)
        return self._ticker

    @property
    def info(self):
        if self._info is None:
            logger.info("radar_acquisition_count operation=yahoo_identity_live_fetch count=1")
            self._info = self.ticker.info or {}
        return self._info


class YahooTickerContexts:
    """Reuse tickers only while their acquisition task is alive.

    A new context always fetches again. Missing context disables reuse. The
    per-context lock covers all access to the mutable yfinance instance, while
    the registry lock covers only lookup/construction, never upstream I/O.
    """
    def __init__(self, factory, max_contexts=64, *,
                 session_refresh: Callable[[], None] | None = None,
                 session_refresh_cooldown_seconds: float = 60.0):
        self.factory = factory
        self.max_contexts = max_contexts
        self._contexts = OrderedDict()
        self._lock = RLock()
        # Yahoo metadata unavailable / Equity Radar retries root cause:
        # `factory` (e.g. YahooFinanceProvider.ticker_factory) typically
        # closes over one SHARED, connection-pooled session object (see
        # _build_bounded_yahoo_session in structured_market.py). A fresh
        # yfinance Ticker per new acquisition context/task (see acquire()
        # above) is therefore NOT a fresh Yahoo session -- if that shared
        # session's cookie/crumb has gone stale or invalid (observed
        # concurrently with Yahoo HTTP 401 Invalid Crumb responses and
        # every identity field in `.info` coming back empty), every new
        # Ticker built from it keeps failing identically, and nothing
        # about getting a new Ticker object repairs that.
        #
        # `session_refresh`, when supplied by the caller that owns the
        # session (optional; None preserves today's behavior exactly),
        # lets exactly one signal -- "a resolved instrument's provider
        # identity metadata came back completely empty" -- request a new
        # session. Bounded by `session_refresh_cooldown_seconds` so a
        # whole cycle's worth of instruments hitting the same stale crumb
        # within the same window triggers at most one rebuild, never an
        # unbounded refresh-and-retry loop; a genuinely permanent
        # authorization failure (e.g. the egress IP itself is blocked)
        # just keeps failing after that one bounded attempt, exactly as
        # intended -- this is backoff for a plausible transient session
        # fault, not indefinite retry of a permanent one.
        self._session_refresh = session_refresh
        self._session_refresh_cooldown_seconds = session_refresh_cooldown_seconds
        self._last_session_refresh_monotonic: float | None = None
        self._session_refresh_lock = RLock()

    def note_identity_metadata_unavailable(self) -> bool:
        """Report that a provider response for a resolved instrument carried
        no identity metadata at all (see PROVIDER_IDENTITY_UNAVAILABLE in
        structured_market.verify_identity_by_isin_or_name). Rebuilds the
        underlying session via `session_refresh`, at most once per
        `session_refresh_cooldown_seconds` regardless of how many callers
        report this within that window. Returns True iff a refresh actually
        ran; False if no refresh hook is configured, or the cooldown is
        still active (the common case for every report after the first in
        a burst)."""
        if self._session_refresh is None:
            return False
        with self._session_refresh_lock:
            now = time.monotonic()
            last = self._last_session_refresh_monotonic
            if last is not None and (now - last) < self._session_refresh_cooldown_seconds:
                return False
            self._last_session_refresh_monotonic = now
        try:
            self._session_refresh()
        except Exception:
            logger.exception("yahoo_session_refresh_failed")
            return False
        logger.info("yahoo_session_refreshed_after_identity_metadata_unavailable")
        return True

    @contextmanager
    def acquire(self, symbol, context=None):
        with self._lock:
            # Blocking provider work can reach here after its async owner was
            # cancelled. It may finish normally, but must not repopulate reuse
            # state for an acquisition that has already ended.
            if isinstance(context, asyncio.Task) and context.done():
                context = None
            key = (context, symbol)
            access = self._contexts.get(key) if context is not None else None
            if access is None:
                access = _TickerAccess(self.factory, symbol)
                if isinstance(context, asyncio.Task):
                    # acquire() runs in provider threads. Task callbacks must
                    # be installed on their event loop, including the race in
                    # which the owner finishes before registration is delivered.
                    context.get_loop().call_soon_threadsafe(self._watch_owner, context)
            if context is not None:
                self._contexts[key] = access
                self._contexts.move_to_end(key)
                while len(self._contexts) > self.max_contexts:
                    self._contexts.popitem(last=False)
        with access.lock:
            yield access

    def _watch_owner(self, owner):
        if owner.done():
            self._release_owner(owner)
        else:
            owner.add_done_callback(self._release_owner)

    def _release_owner(self, owner):
        with self._lock:
            for key in [key for key in self._contexts if key[0] is owner]:
                self._contexts.pop(key, None)

    def retention_stats(self):
        """Counts only; diagnostics never retain provider objects or tasks."""
        with self._lock:
            return {
                'entries': len(self._contexts),
                'completed_task_entries': sum(
                    isinstance(owner, asyncio.Task) and owner.done()
                    for owner, _ in self._contexts),
            }
