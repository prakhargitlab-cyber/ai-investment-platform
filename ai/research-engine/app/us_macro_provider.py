"""Federal Reserve policy rate and US CPI inflation acquisition.

Unlike the India macro providers (app/india_macro_provider.py), this
endpoint and response schema were directly verified against the official
FRED (Federal Reserve Bank of St. Louis) API documentation
(https://fred.stlouisfed.org/docs/api/fred/series_observations.html):

  GET https://api.stlouisfed.org/fred/series/observations
      ?series_id=<id>&api_key=<key>&file_type=json[&units=pc1]
  -> {"observations": [{"date": "YYYY-MM-DD", "value": "..."}, ...], ...}

Series ids used here were confirmed against their own official FRED series
pages, not guessed:
  - DFEDTARU: "Federal Funds Target Range - Upper Limit", set by the FOMC,
    maintained by the Board of Governors of the Federal Reserve System.
    Reported here as the *upper limit* of the target range, never silently
    presented as a single "the Fed rate" figure.
  - CPIAUCSL: "Consumer Price Index for All Urban Consumers: All Items in
    U.S. City Average" (source: U.S. Bureau of Labor Statistics, published
    via FRED), requested with units=pc1 ("Percent Change from Year Ago") --
    a transformation computed by FRED itself from the official index, never
    derived/estimated in this codebase.

No CAPTCHA: authentication is a plain API key in the query string. An API
key is required (free registration at fred.stlouisfed.org) and is never
guessed or defaulted to a placeholder; without one this provider raises
MacroProviderConfigurationError rather than fabricating a response.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.macro_observation import MacroObservation, MacroProviderConfigurationError, MacroProviderError, FED_POLICY_RATE, US_CPI

FRED_OBSERVATIONS_ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"


class _FredSeriesProvider:
    """Shared transport + parsing for one FRED series/observations request."""

    def __init__(self, api_key: str | None, *, endpoint: str = FRED_OBSERVATIONS_ENDPOINT,
                 client: httpx.AsyncClient | None = None, timeout_seconds: float = 10.0) -> None:
        if not api_key:
            raise MacroProviderConfigurationError("MACRO_PROVIDER_NOT_CONFIGURED:api_key")
        self.endpoint = endpoint
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=4.0))

    async def _latest_observation(self, series_id: str, *, units: str | None = None) -> dict:
        params = {"series_id": series_id, "api_key": self.api_key, "file_type": "json",
                  "sort_order": "desc", "limit": 1}
        if units:
            params["units"] = units
        try:
            response = await self._client.get(self.endpoint, params=params)
        except httpx.TimeoutException as exc:
            raise MacroProviderError("MACRO_PROVIDER_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE:transport") from exc
        if response.status_code in (401, 403):
            raise MacroProviderError("MACRO_PROVIDER_FORBIDDEN")
        if response.status_code == 429:
            raise MacroProviderError("MACRO_PROVIDER_RATE_LIMITED")
        if response.status_code >= 500:
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE")
        if response.status_code >= 400:
            raise MacroProviderError(f"MACRO_PROVIDER_UNAVAILABLE:http_status_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE:invalid_response") from exc
        observations = payload.get("observations") if isinstance(payload, dict) else None
        if not isinstance(observations, list) or not observations:
            raise MacroProviderError("MACRO_PROVIDER_NO_RECORDS")
        record = observations[0]
        date = record.get("date")
        value = record.get("value")
        if not date or value is None or str(value).strip() in {"", "."}:
            # FRED uses "." for a genuinely missing observation -- never
            # treated as a numeric value.
            raise MacroProviderError("MACRO_PROVIDER_FIELD_MISSING:value")
        try:
            parsed_value = float(value)
        except ValueError as exc:
            raise MacroProviderError("MACRO_PROVIDER_UNPARSEABLE_VALUE") from exc
        return {"period": str(date), "value": parsed_value}


class FedPolicyRateProvider(_FredSeriesProvider):
    """Upper limit of the FOMC's federal funds target range (FRED: DFEDTARU)."""

    provider_name = "FED_POLICY_RATE_FRED"
    series_id = "DFEDTARU"
    source_name = "Board of Governors of the Federal Reserve System (via FRED, Federal Reserve Bank of St. Louis)"

    async def fetch(self, now: datetime | None = None) -> MacroObservation:
        latest = await self._latest_observation(self.series_id)
        observed_at = now or datetime.now(timezone.utc)
        return MacroObservation(
            indicator=FED_POLICY_RATE, region="US", period=latest["period"], actual_value=latest["value"],
            unit="PERCENT", observed_at=observed_at, source=self.source_name,
            source_url=f"https://fred.stlouisfed.org/series/{self.series_id}",
            provider=self.provider_name, provenance="OFFICIAL_GOVERNMENT",
        )


class UsCpiProvider(_FredSeriesProvider):
    """US CPI, all urban consumers, percent change from a year ago
    (FRED: CPIAUCSL, units=pc1 -- the transformation FRED itself computes)."""

    provider_name = "US_CPI_FRED"
    series_id = "CPIAUCSL"
    source_name = "U.S. Bureau of Labor Statistics (via FRED, Federal Reserve Bank of St. Louis)"

    async def fetch(self, now: datetime | None = None) -> MacroObservation:
        latest = await self._latest_observation(self.series_id, units="pc1")
        observed_at = now or datetime.now(timezone.utc)
        return MacroObservation(
            indicator=US_CPI, region="US", period=latest["period"], actual_value=latest["value"],
            unit="PERCENT_YOY", observed_at=observed_at, source=self.source_name,
            source_url=f"https://fred.stlouisfed.org/series/{self.series_id}",
            provider=self.provider_name, provenance="OFFICIAL_GOVERNMENT",
        )
