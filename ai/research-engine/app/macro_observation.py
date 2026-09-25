"""Global (non-instrument-scoped) macro observations -- e.g. RBI policy repo
rate, India CPI inflation.

Deliberately separate from `app/models.py::ResearchDocument` /
`global_financial_facts`: a macro observation describes the whole economy or
a whole market, not one instrument, and must never be written into (or read
back from) the per-instrument financial-facts table.  Nothing in this module
feeds the Rule Engine, rank eligibility, or any score -- this iteration is
acquisition + persistence + a read model only.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models import ResearchBaseModel

# Indicator identifiers. Scope is RBI repo rate, India CPI, Fed policy rate,
# and US CPI only -- no GDP, FII/DII, crude, FX, or index data.
RBI_REPO_RATE = "RBI_POLICY_REPO_RATE"
INDIA_CPI = "INDIA_CPI_INFLATION"
# Upper limit of the FOMC's target range (FRED series DFEDTARU) -- explicitly
# labelled as the upper limit, never silently presented as "the" Fed rate.
FED_POLICY_RATE = "FED_POLICY_TARGET_RATE_UPPER"
# FRED CPIAUCSL with units=pc1 (percent change from a year ago), computed by
# the official source itself -- never derived/estimated here.
US_CPI = "US_CPI_INFLATION_YOY"
SUPPORTED_INDICATORS = frozenset({RBI_REPO_RATE, INDIA_CPI, FED_POLICY_RATE, US_CPI})


class MacroProviderConfigurationError(RuntimeError):
    """The provider has no safely-automatable, configured source (e.g. no
    endpoint/API key set). Never raised to mean "fetched and found nothing"."""


class MacroProviderError(RuntimeError):
    """A configured provider's fetch attempt failed (network, HTTP status,
    or parse failure). Never silently converted into a value."""


class MacroObservation(ResearchBaseModel):
    indicator: Literal["RBI_POLICY_REPO_RATE", "INDIA_CPI_INFLATION", "FED_POLICY_TARGET_RATE_UPPER", "US_CPI_INFLATION_YOY"]
    region: str = "IN"
    period: str
    actual_value: float
    unit: str
    effective_at: AwareDatetime | None = None
    release_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    source: str
    source_url: str
    provider: str
    provenance: Literal["OFFICIAL_GOVERNMENT"] = "OFFICIAL_GOVERNMENT"
    # Forward-compatible only: NULL unless a trustworthy provider actually
    # supplies it. Nothing in this iteration computes or invents these.
    previous_value: float | None = None
    expected_value: float | None = None
    surprise: float | None = None

    @model_validator(mode="after")
    def _no_invented_expectation(self):
        if self.expected_value is not None and self.provenance != "OFFICIAL_GOVERNMENT":
            raise ValueError("EXPECTED_VALUE_REQUIRES_TRUSTWORTHY_PROVENANCE")
        return self


# Per-indicator freshness windows. RBI's Monetary Policy Committee reviews
# the repo rate roughly every ~2 months; India's CPI print is monthly with a
# reporting lag. These bound how long a persisted observation may be reused
# before it becomes eligible for refresh -- they do not define an economic
# calendar and do not predict the next release date.
_FRESHNESS_SECONDS = {
    RBI_REPO_RATE: 60 * 24 * 3600,
    INDIA_CPI: 35 * 24 * 3600,
    # FOMC meets roughly every 6-8 weeks; US CPI (BLS, via FRED) is monthly
    # with a reporting lag, same cadence reasoning as the India pair above.
    FED_POLICY_RATE: 56 * 24 * 3600,
    US_CPI: 35 * 24 * 3600,
}


def macro_freshness_state(observation: MacroObservation | None, now: datetime, ttl_seconds: float | None = None) -> str:
    """READY_FRESH / READY_STALE / MISSING -- read-model only, consumed by
    nothing yet. A future observed_at (clock skew / bad data) is never
    treated as fresh. `ttl_seconds` lets a caller supply the
    settings-configured window for the observation's indicator
    (india_macro_rbi_freshness_seconds / india_macro_cpi_freshness_seconds);
    the module-level default below is used only when the caller does not."""
    if observation is None:
        return "MISSING"
    if observation.observed_at > now:
        return "READY_STALE"
    ttl = ttl_seconds if ttl_seconds is not None else _FRESHNESS_SECONDS.get(observation.indicator, 30 * 24 * 3600)
    if now - observation.observed_at <= timedelta(seconds=ttl):
        return "READY_FRESH"
    return "READY_STALE"
