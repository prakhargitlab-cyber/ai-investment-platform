"""RBI policy repo rate and India CPI inflation acquisition.

Provider-boundary design note (see the accompanying report's source audit):
this sandbox has no outbound network reachability to rbi.org.in,
data.rbi.org.in (DBIE) or data.gov.in, and data.gov.in's own pages refuse
automated fetch (robots.txt), so no live endpoint or dataset schema could be
verified from here. RBI's DBIE reporting portal is a BI/UI product, not a
documented stable JSON API, and is not safely automatable without either
brittle browser automation or an unverified scrape -- both are out of scope
by instruction. Government of India's Open Government Data (OGD) platform
(api.data.gov.in) is, by contrast, a documented, key-authenticated, JSON
REST API with no CAPTCHA -- the one structured/authoritative option this
iteration can build a real parser for. Per instruction ("implement the
provider boundary and report the limitation rather than scraping
unreliably"), the concrete resource id/endpoint and its exact field names
are NOT hardcoded here (that would be an unverified guess, i.e. a fabricated
technical detail); they are supplied through settings by whoever has
verified the concrete resource from a network-enabled environment. Nothing
here invents a value: if unconfigured, or if the response is missing an
expected field, acquisition fails cleanly and no observation is persisted.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.macro_observation import MacroObservation, MacroProviderConfigurationError, MacroProviderError, RBI_REPO_RATE, INDIA_CPI


class _DataGovInOgdProvider:
    """Shared transport + parsing for data.gov.in's OGD platform API: a
    documented JSON envelope, ``{"records": [{...}, ...], ...}``, returned
    for every resource on the platform. Authentication is a plain API key in
    the query string -- no CAPTCHA, no browser session."""

    def __init__(self, endpoint: str | None, api_key: str | None, *, client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = 10.0) -> None:
        if not endpoint or not api_key:
            raise MacroProviderConfigurationError("MACRO_PROVIDER_NOT_CONFIGURED:endpoint_or_api_key")
        self.endpoint = endpoint
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=4.0))

    async def _fetch_records(self) -> list[dict]:
        params = {"api-key": self.api_key, "format": "json", "limit": 1, "sort[-period]": ""}
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
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not records:
            raise MacroProviderError("MACRO_PROVIDER_NO_RECORDS")
        return records

    @staticmethod
    def _field(record: dict, field_name: str) -> str:
        value = record.get(field_name)
        if value is None or str(value).strip() == "":
            raise MacroProviderError(f"MACRO_PROVIDER_FIELD_MISSING:{field_name}")
        return str(value).strip()

    @staticmethod
    def _parse_value(raw: str) -> float:
        try:
            return float(raw)
        except ValueError as exc:
            raise MacroProviderError("MACRO_PROVIDER_UNPARSEABLE_VALUE") from exc


class RbiRepoRateProvider(_DataGovInOgdProvider):
    """RBI policy/repo rate, sourced from a data.gov.in OGD resource
    verified and configured by settings (research_india_macro_rbi_*)."""

    provider_name = "RBI_REPO_RATE_DATA_GOV_IN"

    def __init__(self, endpoint, api_key, *, period_field="period", value_field="repo_rate",
                 source_name="Reserve Bank of India (via data.gov.in Open Government Data)", **kwargs) -> None:
        super().__init__(endpoint, api_key, **kwargs)
        self.period_field = period_field
        self.value_field = value_field
        self.source_name = source_name

    async def fetch(self, now: datetime | None = None) -> MacroObservation:
        records = await self._fetch_records()
        record = records[0]
        period = self._field(record, self.period_field)
        value = self._parse_value(self._field(record, self.value_field))
        observed_at = now or datetime.now(timezone.utc)
        return MacroObservation(
            indicator=RBI_REPO_RATE, region="IN", period=period, actual_value=value, unit="PERCENT",
            observed_at=observed_at, source=self.source_name, source_url=self.endpoint,
            provider=self.provider_name, provenance="OFFICIAL_GOVERNMENT",
        )


class IndiaCpiProvider(_DataGovInOgdProvider):
    """India CPI inflation (headline), sourced from a data.gov.in OGD
    resource verified and configured by settings (research_india_macro_cpi_*)."""

    provider_name = "INDIA_CPI_DATA_GOV_IN"

    def __init__(self, endpoint, api_key, *, period_field="period", value_field="inflation_rate",
                 source_name="Ministry of Statistics & Programme Implementation (via data.gov.in Open Government Data)",
                 **kwargs) -> None:
        super().__init__(endpoint, api_key, **kwargs)
        self.period_field = period_field
        self.value_field = value_field
        self.source_name = source_name

    async def fetch(self, now: datetime | None = None) -> MacroObservation:
        records = await self._fetch_records()
        record = records[0]
        period = self._field(record, self.period_field)
        value = self._parse_value(self._field(record, self.value_field))
        observed_at = now or datetime.now(timezone.utc)
        return MacroObservation(
            indicator=INDIA_CPI, region="IN", period=period, actual_value=value, unit="PERCENT",
            observed_at=observed_at, source=self.source_name, source_url=self.endpoint,
            provider=self.provider_name, provenance="OFFICIAL_GOVERNMENT",
        )
