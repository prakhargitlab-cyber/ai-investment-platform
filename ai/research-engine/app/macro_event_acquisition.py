"""Acquisition + read-model orchestration for the global macro event
calendar (FOMC, RBI MPC).

Reuses the SingleFlightTTLCache hook (app/global_data_provider.py) so
however many of the ~2,580 NSE instruments ask for the same calendar inside
one refresh window collapse into a single provider fetch. Nothing here is
consumed by the Rule Engine, rank eligibility, or any score.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.global_data_provider import SingleFlightTTLCache
from app.macro_event import MacroEvent
from app.macro_observation import MacroProviderConfigurationError, MacroProviderError


async def get_macro_events(persistence, provider, cache: SingleFlightTTLCache, event_type: str, indicator: str,
                            region: str, now: datetime | None = None,
                            ttl_seconds: float | None = None) -> list[MacroEvent]:
    """Return the current best-known upcoming calendar for (event_type,
    indicator, region).

    - If the persisted calendar was refreshed within `ttl_seconds`, no
      provider call is made at all -- whether this is the 1st or the
      2,580th caller in the window.
    - Otherwise a fetch is attempted through `cache`, single-flighting
      concurrent callers into one provider call. Every event returned is
      upserted idempotently (deterministic id).
    - A provider failure never writes anything: previously persisted future
      events are returned unchanged, never dropped, never marked completed
      or cancelled as a side effect of a failed refresh. Only when there is
      no persisted evidence at all does the failure propagate -- there is
      nothing to fall back to, and nothing is fabricated.
    """
    now = now or datetime.now(timezone.utc)
    last_refreshed = persistence.macro_events_last_refreshed(event_type, indicator, region)
    fresh = (
        last_refreshed is not None
        and ttl_seconds is not None
        and (now - last_refreshed).total_seconds() <= ttl_seconds
    )
    if not fresh:
        async def _fetch_and_persist() -> list[MacroEvent]:
            events = await provider.fetch(now)
            for event in events:
                persistence.upsert_macro_event(event)
            return events

        try:
            await cache.get_or_fetch(f"{event_type}:{indicator}:{region}", _fetch_and_persist)
        except (MacroProviderError, MacroProviderConfigurationError):
            if last_refreshed is None:
                raise

    return persistence.upcoming_macro_events(region=region, indicator=indicator, now=now)
