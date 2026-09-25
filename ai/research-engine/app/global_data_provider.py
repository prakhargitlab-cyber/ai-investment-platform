"""Minimal reusable hook for a future global (non-instrument-scoped) data
provider -- e.g. a macro/market data source (Fed, RBI, CPI, an economic
calendar, FII/DII flows, ...) that should be fetched ONCE per refresh window
and reused across the whole ~2,580-stock universe, rather than once per
instrument.

This module deliberately implements NO concrete macro data, scoring, or
ranking logic. It only generalizes the single-flight + bounded-TTL caching
pattern already proven in `OfficialFilingDiscovery._announcement_rows`
(app/source_discovery.py, per-symbol) into a form any future provider can
reuse -- instrument-scoped as today, or, by sharing one key across every
caller, genuinely global. No provider is wired to this module yet.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Generic, Protocol, TypeVar

T = TypeVar("T")

# A future global provider shares this single key across every instrument's
# evaluation, so SingleFlightTTLCache.get_or_fetch(GLOBAL_CACHE_KEY, ...)
# fetches once per TTL window no matter how many instruments ask for it.
GLOBAL_CACHE_KEY = "GLOBAL"


class SingleFlightTTLCache(Generic[T]):
    """Fetch-once-per-key, reuse-until-TTL-expiry cache with single-flight
    de-duplication of concurrent callers for the same key.

    Concurrent callers for a key already in flight await the same underlying
    fetch instead of issuing their own; a value already cached and still
    within its TTL is returned without a fetch at all. A provider that scopes
    every caller to one shared key (GLOBAL_CACHE_KEY) is therefore fetched
    once and its result served to every caller -- including, for a future
    global market/macro provider, all ~2,580 instruments evaluated in one
    cycle -- instead of once per instrument.
    """

    def __init__(self, ttl_seconds: float, max_keys: int = 32) -> None:
        if ttl_seconds <= 0:
            raise ValueError("INVALID_CACHE_TTL")
        if max_keys <= 0:
            raise ValueError("INVALID_CACHE_SIZE")
        self.ttl_seconds = ttl_seconds
        self.max_keys = max_keys
        self._entries: "OrderedDict[str, tuple[float, T]]" = OrderedDict()
        self._flights: dict[str, asyncio.Future] = {}

    async def get_or_fetch(self, key: str, fetch: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < self.ttl_seconds:
            self._entries.move_to_end(key)
            return cached[1]
        flight = self._flights.get(key)
        if flight is not None:
            return await asyncio.shield(flight)
        flight = asyncio.get_running_loop().create_future()
        self._flights[key] = flight
        try:
            value = await fetch()
        except BaseException as exc:
            flight.set_exception(exc)
            flight.exception()  # retrieved: joined callers re-raise it themselves
            raise
        else:
            flight.set_result(value)
            self._entries[key] = (now, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_keys:
                self._entries.popitem(last=False)
            return value
        finally:
            self._flights.pop(key, None)


class GlobalDataProvider(Protocol):
    """Contract for a future provider whose data is not scoped to a single
    instrument (e.g. Fed/RBI policy rates, CPI, an economic calendar,
    FII/DII flows) so it can be fetched once per cycle and reused by every
    instrument's evaluation. No concrete implementation is provided here;
    a future implementation is expected to route its fetch through a
    SingleFlightTTLCache keyed by GLOBAL_CACHE_KEY."""

    async def fetch_global_snapshot(self, now) -> object:
        """Return one snapshot object valid for the whole universe."""
