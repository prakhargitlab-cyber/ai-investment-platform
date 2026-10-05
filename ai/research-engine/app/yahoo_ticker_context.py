"""Bounded, caller-owned Yahoo reuse; never share mutable tickers concurrently."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
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
    """One provider instance's contextual tickers, bounded to 64 entries.

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
            key = (context, symbol)
            access = self._contexts.get(key) if context is not None else None
            if access is None:
                access = _TickerAccess(self.factory, symbol)
            if context is not None:
                self._contexts[key] = access
                self._contexts.move_to_end(key)
                while len(self._contexts) > self.max_contexts:
                    self._contexts.popitem(last=False)
        with access.lock:
            yield access
