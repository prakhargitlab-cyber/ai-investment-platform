"""Bounded per-instrument retention of ResearchEvent objects.

Memory hardening (pre-flight). ResearchRepository previously loaded EVERY
historical research event at startup and kept every extracted event, plus its
dedup key, for the life of the process (~3.9 KB per event, linear, never
evicted), and events_for() scanned all of them on each call.

Durable research_events (PostgreSQL) is the source of truth. This store keeps
events per instrument, loads an instrument's durable events on first use
(load_events({instrument})), and keeps at most ``max_instruments`` instruments
resident (LRU). Instruments holding events that are NOT durable (DEMO, disabled
persistence, tests writing ``repo.events[id] = event`` directly) are pinned and
never evicted, because eviction would lose the only copy. Dedup keys live with
their instrument's bucket, so they are reloaded together with its events.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, MutableMapping
from typing import Any
from uuid import UUID

DEFAULT_MAX_INSTRUMENTS = 256


class _Bucket:
    __slots__ = ("events", "keys", "loaded", "pinned")

    def __init__(self) -> None:
        self.events: dict[UUID, Any] = {}
        self.keys: set = set()
        self.loaded = False
        self.pinned = False


class BoundedEventStore(MutableMapping):
    def __init__(self, loader: Callable[[UUID], list], key_of: Callable[[Any], tuple], *,
                 durable: Callable[[], bool], max_instruments: int = DEFAULT_MAX_INSTRUMENTS) -> None:
        self._loader, self._key_of, self._durable = loader, key_of, durable
        self.max_instruments = max(1, int(max_instruments))
        self._buckets: OrderedDict[UUID, _Bucket] = OrderedDict()
        self._instrument_of: dict[UUID, UUID] = {}
        self.applied = 0
        self.loads = 0
        self.evictions = 0

    # -- instrument access ----------------------------------------------------
    def bucket(self, instrument_id: UUID) -> _Bucket:
        bucket = self._buckets.get(instrument_id)
        if bucket is None:
            bucket = self._buckets[instrument_id] = _Bucket()
        self._buckets.move_to_end(instrument_id)
        if not bucket.loaded:
            bucket.loaded = True
            if self._durable():
                self.loads += 1
                for event in self._loader(instrument_id):
                    if event.event_id not in bucket.events:
                        bucket.events[event.event_id] = event
                        bucket.keys.add(self._key_of(event))
                        self._instrument_of[event.event_id] = instrument_id
        self._evict()
        return bucket

    def for_instrument(self, instrument_id: UUID) -> list:
        return list(self.bucket(instrument_id).events.values())

    def add(self, event, *, durable: bool) -> bool:
        """Register an extracted event; False when its dedup key is known."""
        bucket = self.bucket(event.instrument_id)
        key = self._key_of(event)
        if key in bucket.keys:
            return False
        bucket.keys.add(key)
        bucket.events[event.event_id] = event
        self._instrument_of[event.event_id] = event.instrument_id
        if not durable:
            bucket.pinned = True
        self.applied += 1
        return True

    def resident_keys(self) -> set:
        return set().union(*(b.keys for b in self._buckets.values())) if self._buckets else set()

    def stats(self) -> dict[str, int]:
        return {"resident_instruments": len(self._buckets), "resident_events": len(self._instrument_of),
                "pinned_instruments": sum(b.pinned for b in self._buckets.values()),
                "loads": self.loads, "evictions": self.evictions, "applied": self.applied}

    def _evict(self) -> None:
        if len(self._buckets) <= self.max_instruments:
            return
        for instrument_id in list(self._buckets):
            if len(self._buckets) <= self.max_instruments:
                break
            bucket = self._buckets[instrument_id]
            if bucket.pinned or instrument_id == next(reversed(self._buckets)):
                continue
            for event_id in bucket.events:
                self._instrument_of.pop(event_id, None)
            del self._buckets[instrument_id]
            self.evictions += 1

    # -- dict[UUID, ResearchEvent] compatibility (resident events) -------------
    def __getitem__(self, event_id: UUID):
        return self._buckets[self._instrument_of[event_id]].events[event_id]

    def __setitem__(self, event_id: UUID, event) -> None:
        bucket = self._buckets.get(event.instrument_id)
        if bucket is None:
            bucket = self._buckets[event.instrument_id] = _Bucket()
        bucket.events[event_id] = event
        bucket.keys.add(self._key_of(event))
        bucket.pinned = True  # a direct write has not proven durability
        self._instrument_of[event_id] = event.instrument_id

    def __delitem__(self, event_id: UUID) -> None:
        instrument_id = self._instrument_of.pop(event_id)
        bucket = self._buckets[instrument_id]
        event = bucket.events.pop(event_id)
        bucket.keys.discard(self._key_of(event))

    def __iter__(self):
        return iter(list(self._instrument_of))

    def __len__(self) -> int:
        return len(self._instrument_of)

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._instrument_of
