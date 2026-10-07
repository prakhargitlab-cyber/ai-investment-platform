"""Bounded, caller-owned Yahoo reuse; never share mutable tickers concurrently."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
import asyncio
import logging

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
    def __init__(self, factory, max_contexts=64):
        self.factory = factory
        self.max_contexts = max_contexts
        self._contexts = OrderedDict()
        self._lock = RLock()

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
