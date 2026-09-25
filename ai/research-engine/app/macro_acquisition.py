"""Acquisition + read-model orchestration for global macro observations
(RBI repo rate, India CPI).

Reuses the SingleFlightTTLCache / GlobalDataProvider hook
(app/global_data_provider.py) so however many of the ~2,580 NSE instruments
ask for the same indicator inside one refresh window collapse into a single
provider fetch, never one fetch per instrument. This module does not feed
the Rule Engine, rank eligibility, or any score -- acquisition + persistence
+ read model only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.global_data_provider import SingleFlightTTLCache
from app.macro_observation import (
    MacroObservation,
    MacroProviderConfigurationError,
    MacroProviderError,
    macro_freshness_state,
)


async def get_macro_observation(persistence, provider, cache: SingleFlightTTLCache, indicator: str,
                                 now: datetime | None = None, ttl_seconds: float | None = None,
                                 region: str = "IN") -> MacroObservation:
    """Return the current best-known observation for `indicator`.

    - Fresh persisted evidence is returned immediately: no provider call,
      whether this is the 1st or the 2,580th caller in the window.
    - Otherwise a fetch is attempted through `cache`, single-flighting
      concurrent callers into one provider call. Success persists (upsert)
      and returns the new observation.
    - A provider failure (misconfiguration or a failed fetch) never writes
      anything: the previously persisted observation, if any, is untouched
      and is returned as the best-effort answer instead of raising to every
      caller. With no persisted observation at all, the failure is raised --
      there is nothing to fall back to, and nothing is fabricated.

    `region` defaults to "IN" (the original RBI/India-CPI callers); Fed/US-CPI
    callers pass region="US". Indicator ids are already globally distinct
    across regions, but the cache key still includes region for robustness.
    """
    now = now or datetime.now(timezone.utc)
    persisted = persistence.load_macro_observation(indicator, region)
    if macro_freshness_state(persisted, now, ttl_seconds) == "READY_FRESH":
        return persisted

    async def _fetch_and_persist() -> MacroObservation:
        observation = await provider.fetch(now)
        persistence.upsert_macro_observation(observation)
        return observation

    try:
        return await cache.get_or_fetch(f"{indicator}:{region}", _fetch_and_persist)
    except (MacroProviderError, MacroProviderConfigurationError):
        if persisted is not None:
            return persisted
        raise
