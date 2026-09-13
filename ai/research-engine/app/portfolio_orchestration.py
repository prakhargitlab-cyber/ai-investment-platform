from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import re
import logging

import httpx

from app.models import (
    CompanyResearchProfile,
    CategoryEvidence,
    EvidenceState,
    EtfResearchProfile,
    EventImpact,
    PortfolioResearchCompany,
    PortfolioResearchSummary,
    PublicAnalyst,
    MarketFundamentals,
    ResearchSummary,
    StructuredMarketSnapshotRecord,
)
from app.repository import ResearchRepository
from app.scoring import canonical_read_model_score
from app.settings import Settings
from app.source_registry import registered_sources_for
from app.structured_research import enrich_company_research
from app.structured_market import StructuredProviderError, StructuredResearchProvider, YahooFinanceProvider, _is_financial_identity
from app.market_sessions import class_due, market_session_status, price_sync_eligible
from app.international_fundamentals import InternationalFundamentalsResult, international_provider_for
from app.sector_performance import belongs_to_region

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StructuredReconciliationOutcome:
    snapshot: object | None
    error: str | None
    due_classes: frozenset[str]
    market_status: str


class GlobalInstrumentNotFoundError(Exception):
    """The portfolio service confirmed that a global instrument does not exist."""


class PortfolioServiceUnavailableError(Exception):
    """A global instrument could not be resolved because portfolio-service is unavailable."""


class WatchlistNotFoundError(Exception):
    """The authenticated user does not own the requested watchlist."""


class WatchlistRegionMismatchError(Exception):
    """The canonical instrument region does not match the target watchlist."""


class PortfolioResearchOrchestrator:
    def __init__(
        self,
        repository: ResearchRepository,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        structured_provider: StructuredResearchProvider | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
        self.structured_provider = structured_provider or YahooFinanceProvider(settings)
        self._structured_by_instrument: dict[UUID, tuple[datetime, object]] = {}

    async def active_global_equities(
        self,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> list[dict]:
        """Read the portfolio-service owned active-equity universe; never mutate it."""
        from app.global_scanner import CanonicalEquityUniverse

        try:
            return await CanonicalEquityUniverse(self._client, self.settings.portfolio_service_base_url).active_global_equities(
                correlation_id=correlation_id, identity_headers=identity_headers,
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for instrument enumeration") from exc

    async def india_nifty500_universe(self, *, correlation_id: str | None = None, identity_headers: dict[str, str | None] | None = None) -> list[dict]:
        """Read portfolio-service owned NSE/Nifty universe; never uses portfolios."""
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        values: list[dict] = []
        page = 0
        page_size = 500
        try:
            while True:
                response = await self._client.get(
                    f"{self.settings.portfolio_service_base_url}/api/v1/market-universe/india/nifty500",
                    params={"page": page, "size": page_size},
                    headers=headers or None,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("instruments", []), list):
                    raise ValueError("Invalid India market-universe response")
                batch = payload.get("instruments", [])
                values.extend(batch)
                try:
                    total = int(payload.get("totalElements", len(values)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid India market-universe pagination") from exc
                if not batch:
                    if len(values) < total:
                        raise ValueError("India market-universe pagination ended before totalElements")
                    break
                if len(values) >= total:
                    break
                page += 1
            return values
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for India market universe") from exc

    async def refresh_india_nifty500_reference(
        self,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> dict:
        """Start the portfolio-owned official reference refresh.

        Callers must supply a server-owned operational identity. Browser role
        headers are never promoted inside this client.
        """
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        try:
            response = await self._client.post(
                f"{self.settings.portfolio_service_base_url}/api/v1/market-universe/india/nifty500/refresh",
                headers=headers or None,
                timeout=httpx.Timeout(
                    self.settings.market_data_nifty_refresh_timeout_seconds,
                    connect=self.settings.research_connect_timeout_seconds,
                ),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid India reference refresh response")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for India reference refresh") from exc

    async def global_instrument_metadata(
        self,
        instrument_id: UUID,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> dict:
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        try:
            response = await self._client.get(
                f"{self.settings.portfolio_service_base_url}/api/v1/instruments/{instrument_id}",
                headers=headers or None,
            )
            if response.status_code == 404:
                raise GlobalInstrumentNotFoundError(str(instrument_id))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid global instrument response")
            return payload
        except GlobalInstrumentNotFoundError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for global instrument lookup") from exc

    async def list_watchlists(
        self,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> list[dict]:
        payload = await self._watchlist_request(
            "GET", "/api/v1/watchlists", correlation_id=correlation_id,
            identity_headers=identity_headers,
        )
        if not isinstance(payload, list) or not all(isinstance(value, dict) for value in payload):
            raise PortfolioServiceUnavailableError("Portfolio service returned an invalid watchlist response")
        return payload

    async def ensure_default_watchlist(
        self,
        region: str,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> dict:
        payload = await self._watchlist_request(
            "POST", "/api/v1/watchlists/default/ensure", json={"region": region},
            correlation_id=correlation_id, identity_headers=identity_headers,
        )
        if not isinstance(payload, dict):
            raise PortfolioServiceUnavailableError("Portfolio service returned an invalid watchlist response")
        return payload

    async def watchlist(
        self,
        watchlist_id: UUID,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> dict:
        payload = await self._watchlist_request(
            "GET", f"/api/v1/watchlists/{watchlist_id}", correlation_id=correlation_id,
            identity_headers=identity_headers,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("instruments"), list):
            raise PortfolioServiceUnavailableError("Portfolio service returned an invalid watchlist detail response")
        return payload

    async def add_watchlist_instrument(
        self,
        watchlist_id: UUID,
        payload: dict,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> dict:
        value = await self._watchlist_request(
            "POST", f"/api/v1/watchlists/{watchlist_id}/instruments", json=payload,
            correlation_id=correlation_id, identity_headers=identity_headers,
        )
        if not isinstance(value, dict):
            raise PortfolioServiceUnavailableError("Portfolio service returned an invalid watchlist membership response")
        return value

    async def remove_watchlist_instrument(
        self,
        watchlist_id: UUID,
        instrument_id: UUID,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> None:
        await self._watchlist_request(
            "DELETE", f"/api/v1/watchlists/{watchlist_id}/instruments/{instrument_id}",
            correlation_id=correlation_id, identity_headers=identity_headers,
            expect_json=False,
        )

    async def _watchlist_request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
        expect_json: bool = True,
    ):
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        try:
            response = await self._client.request(
                method,
                f"{self.settings.portfolio_service_base_url}{path}",
                headers=headers or None,
                json=json,
            )
        except httpx.HTTPError as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for watchlists") from exc
        if response.status_code == 404:
            raise WatchlistNotFoundError(path)
        if response.status_code == 409:
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if code == "WATCHLIST_REGION_MISMATCH":
                raise WatchlistRegionMismatchError(code)
        try:
            response.raise_for_status()
            return response.json() if expect_json else None
        except (httpx.HTTPError, ValueError) as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for watchlists") from exc

    async def read_global_company_state(
        self,
        instrument_id: UUID,
        *,
        metadata: dict | None = None,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> PortfolioResearchCompany:
        """Project one global instrument through the existing durable research read model.

        This is the non-portfolio counterpart of a portfolio company row. It
        reads canonical instrument metadata and already-persisted research; it
        neither creates a holding nor performs provider work.
        """
        payload = metadata or await self.global_instrument_metadata(
            instrument_id,
            correlation_id=correlation_id,
            identity_headers=identity_headers,
        )
        instrument = _global_master_instrument(payload, instrument_id)
        return await self._read_company_state(instrument)

    async def reconcile_global_instrument_metadata(
        self,
        instrument_id: UUID,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> dict:
        """Ask portfolio-service's identity owner to validate provider mappings.

        This intentionally returns metadata only.  Unlike the interactive
        company refresh path, it does not register a process-local research
        profile and does not fetch fundamentals.
        """
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        try:
            response = await self._client.post(
                f"{self.settings.portfolio_service_base_url}/api/v1/instruments/{instrument_id}/reconcile",
                headers=headers or None,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid global instrument reconciliation response")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for global instrument reconciliation") from exc

    async def restore_global_profile(
        self,
        global_instrument_id: UUID,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> bool:
        """Restore a process-local profile from the global instrument master API.

        Direct company reads are keyed by the global master ID, never by a local
        portfolio instrument ID.  The returned shape is normalized to the same
        instrument representation used by portfolio-position orchestration so
        provider mapping selection remains centralized here.
        """
        try:
            self.repository.profile(global_instrument_id)
            return True
        except StopIteration:
            pass
        try:
            self.repository.etf_profile(global_instrument_id)
            return True
        except StopIteration:
            pass

        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        try:
            response = await self._client.get(
                f"{self.settings.portfolio_service_base_url}/api/v1/instruments/{global_instrument_id}",
                headers=headers or None,
            )
        except httpx.HTTPError as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for global instrument lookup") from exc
        if response.status_code == 404:
            raise GlobalInstrumentNotFoundError(str(global_instrument_id))
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for global instrument lookup") from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise PortfolioServiceUnavailableError("Portfolio service returned an invalid global instrument response")
        instrument = _global_master_instrument(payload, global_instrument_id)
        asset_type = _instrument_asset_type(instrument)
        if asset_type == "ETF":
            return self._resolve_etf_profile(instrument, register_missing=True) is not None
        return self._resolve_profile(instrument, register_missing=True) is not None

    def register_global_profile_metadata(
        self,
        global_instrument_id: UUID,
        payload: dict,
    ) -> bool:
        """Hydrate the process-local public profile from an already-read identity row."""
        instrument = _global_master_instrument(payload, global_instrument_id)
        if _instrument_asset_type(instrument) == "ETF":
            return self._resolve_etf_profile(instrument, register_missing=True) is not None
        # A canonical global identity must keep ownership of its UUID.  The
        # general portfolio resolver may legitimately reuse an older profile
        # by ISIN/ticker (including a demo seed), which would leave a public
        # watchlist/non-held instrument registered under the wrong UUID.
        # Prefer an exact profile and otherwise create the canonical profile
        # directly from the already-validated instrument-master response.
        profile = next(
            (
                value
                for value in self.repository.list_profiles()
                if value.instrument_id == global_instrument_id
            ),
            None,
        )
        if profile is None:
            profile = self._register_equity_profile_from_instrument(
                instrument,
                str(instrument.get("provider") or "").upper(),
                str(instrument.get("providerInstrumentId") or "").upper(),
                str(instrument.get("isin") or "").upper(),
                str(_instrument_ticker(instrument) or "").upper(),
                _instrument_exchange(instrument),
                _normalize_exchange(
                    instrument.get("canonicalMic") or instrument.get("mic")
                ),
            )
        if profile is None:
            return False
        _refresh_profile_from_global_instrument(profile, instrument)
        return True

    async def reconcile_global_profile(
        self,
        global_instrument_id: UUID,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> bool:
        """Refresh one process-local profile from portfolio-service's mapping owner."""
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        url = f"{self.settings.portfolio_service_base_url}/api/v1/instruments/{global_instrument_id}/reconcile"
        try:
            response = await self._client.post(url, headers=headers or None)
        except httpx.HTTPError as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for global instrument reconciliation") from exc
        if response.status_code == 404:
            raise GlobalInstrumentNotFoundError(str(global_instrument_id))
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PortfolioServiceUnavailableError("Portfolio service unavailable for global instrument reconciliation") from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise PortfolioServiceUnavailableError("Portfolio service returned an invalid global instrument response")
        instrument = _global_master_instrument(payload, global_instrument_id)
        asset_type = _instrument_asset_type(instrument)
        if asset_type == "ETF":
            return self._resolve_etf_profile(instrument, register_missing=True) is not None
        profile = self._resolve_profile(instrument, register_missing=True)
        if profile is None:
            return False
        _refresh_profile_from_global_instrument(profile, instrument)
        return True

    async def refresh_instrument(
        self,
        instrument_id: UUID,
        *,
        correlation_id: str | None = None,
        allow_demo: bool = True,
        pre_resolved_categories: set[str] | None = None,
        instrument: dict | None = None,
        structured_outcome: StructuredReconciliationOutcome | None = None,
        structured_records: list[StructuredMarketSnapshotRecord] | None = None,
        market_data=None,
    ) -> ResearchSummary | EtfResearchProfile:
        """Run the shared explicit refresh and durable structured reconciliation."""
        source_instrument = instrument or self._instrument_for_registered_profile(instrument_id)
        asset_type = _instrument_asset_type(source_instrument)
        outcome = structured_outcome or await self._reconcile_structured_market(
            instrument_id,
            source_instrument,
            records=structured_records,
            market_data=market_data,
        )
        if asset_type == "ETF":
            return await self.repository.refresh_etf(instrument_id, correlation_id=correlation_id)
        categories = set(pre_resolved_categories or set())
        categories.update(_structured_categories(outcome.snapshot))
        return await self.repository.refresh(
            instrument_id,
            correlation_id=correlation_id,
            allow_demo=allow_demo,
            pre_resolved_categories=categories,
        )

    def _instrument_for_registered_profile(self, instrument_id: UUID) -> dict:
        """Rebuild only persisted identity needed by the provider; never invent a symbol."""
        try:
            profile = self.repository.profile(instrument_id)
            return {
                "instrumentId": str(profile.instrument_id), "assetType": "EQUITY",
                "companyName": profile.company_name, "canonicalName": profile.company_name,
                "ticker": profile.ticker, "exchange": profile.exchange, "mic": profile.mic,
                "isin": profile.isin, "country": profile.country, "currency": profile.currency,
                "structuredProviderTicker": profile.provider_instrument_ids.get("YAHOO_FINANCE"),
                "structuredProviderStatus": "VERIFIED" if profile.provider_instrument_ids.get("YAHOO_FINANCE") else None,
                "nseSymbol": profile.provider_instrument_ids.get("NSE"),
            }
        except StopIteration:
            profile = self.repository.etf_profile(instrument_id)
            return {
                "instrumentId": str(profile.instrument_id), "assetType": "ETF",
                "companyName": profile.fund_name, "canonicalName": profile.fund_name,
                "ticker": profile.ticker, "exchange": profile.exchange, "mic": profile.mic,
                "isin": profile.isin, "currency": profile.currency,
                "structuredProviderTicker": profile.provider_instrument_id if str(profile.provider or "").upper() == "YAHOO_FINANCE" else None,
                "structuredProviderStatus": "VERIFIED" if str(profile.provider or "").upper() == "YAHOO_FINANCE" else None,
            }

    async def _reconcile_structured_market(
        self,
        instrument_id: UUID,
        instrument: dict,
        *,
        records: list[StructuredMarketSnapshotRecord] | None = None,
        market_data=None,
        requested_classes: set[str] | None = None,
        force_requested: bool = False,
    ) -> StructuredReconciliationOutcome:
        """Reconcile durable structured state; summaries never call this path."""
        if not self.settings.structured_provider_enabled or _instrument_asset_type(instrument) not in {"EQUITY", "ETF"}:
            return StructuredReconciliationOutcome(None, None, frozenset(), "UNKNOWN")
        if records is None:
            records = (await self.repository.structured_market_snapshots_for_instruments({instrument_id})).get(instrument_id, [])
        record = _preferred_structured_record(records)
        market = str(instrument.get("mic") or instrument.get("exchange") or "").upper()
        if market_data is None:
            market_data = await self.repository.market_session_data({market} if market else set())
        schedules, exceptions = market_data
        now = datetime.now(timezone.utc)
        status = market_session_status(market, schedules, exceptions, now)
        due = _structured_due_classes(record, status, self.settings, now)
        if requested_classes is not None:
            requested = {str(value).strip().upper() for value in requested_classes}
            unknown = requested - {"PRICE", "VALUATION", "FUNDAMENTALS", "ANALYST"}
            if unknown:
                raise ValueError(f"UNKNOWN_STRUCTURED_DATA_CLASS:{','.join(sorted(unknown))}")
            due = requested if force_requested else due & requested
        if not due:
            return StructuredReconciliationOutcome(record.snapshot if record else None, None, frozenset(), status)
        try:
            snapshot = await self.structured_provider.collect(instrument)
            await self._persist_structured_snapshot(instrument_id, snapshot)
            await self.repository.persist_yahoo_statement_facts_async(instrument_id, snapshot)
            logger.info("structured_provider_complete canonical_instrument=%s dueClasses=%s marketStatus=%s", instrument_id, sorted(due), status)
            return StructuredReconciliationOutcome(snapshot, None, frozenset(due), status)
        except Exception as exc:
            error = str(exc) if isinstance(exc, StructuredProviderError) else type(exc).__name__
            await self.repository.record_structured_market_failure_async(
                instrument_id, getattr(self.structured_provider, "provider_name", "YAHOO_FINANCE"), error, error
            )
            logger.warning("structured_provider_failed canonical_instrument=%s dueClasses=%s reason=%s", instrument_id, sorted(due), error)
            return StructuredReconciliationOutcome(record.snapshot if record else None, error, frozenset(due), status)

    async def ensure_structured_market(
        self,
        instrument_id: UUID,
        requested_classes: set[str],
    ) -> StructuredReconciliationOutcome:
        """Run one explicitly selected structured-market capability.

        The provider keeps ownership of collection and persistence.  The
        readiness planner owns only the requested fact classes.
        """
        return await self._reconcile_structured_market(
            instrument_id,
            self._instrument_for_registered_profile(instrument_id),
            requested_classes=requested_classes,
            force_requested=True,
        )

    async def refresh_international_fundamentals(
        self,
        profile: CompanyResearchProfile,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> InternationalFundamentalsResult | None:
        """Ingest a non-India profile through the shared financial-fact store.

        It is deliberately profile-based, rather than portfolio-based, so the
        same global instrument can later be refreshed by a market screener.
        """
        provider = international_provider_for(profile, self.settings, client=self._client)
        if provider is None:
            return None
        result = await provider.collect(profile)
        if result.facts:
            await self.repository.persist_international_financial_facts_async(result.facts)
        for provider, provider_id in result.verified_provider_ids.items():
            await self._persist_verified_provider_mapping(
                profile,
                provider,
                provider_id,
                correlation_id=correlation_id,
                identity_headers=identity_headers,
            )
        return result

    async def _persist_verified_provider_mapping(
        self,
        profile: CompanyResearchProfile,
        provider: str,
        provider_id: str,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> None:
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        response = await self._client.put(
            f"{self.settings.portfolio_service_base_url}/api/v1/instruments/{profile.instrument_id}/provider-mappings/verified",
            headers=headers or None,
            json={"provider": provider, "providerSymbol": profile.ticker, "providerInstrumentId": provider_id,
                  "exchange": profile.exchange, "currency": profile.currency,
                  "resolutionSource": "RESEARCH_ENGINE_VERIFIED_FUNDAMENTALS", "confidence": 0.90},
        )
        if response.status_code == 404:
            raise GlobalInstrumentNotFoundError(str(profile.instrument_id))
        response.raise_for_status()

    async def refresh_portfolio(
        self,
        portfolio_id: UUID,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
        instruments: list[dict] | None = None,
        progress_callback=None,
    ) -> PortfolioResearchSummary:
        instruments = instruments if instruments is not None else await self.prepare_portfolio_refresh(portfolio_id, correlation_id, identity_headers)
        logger.info("portfolio_research_refresh_start portfolioId=%s instrumentCount=%s", portfolio_id, len(instruments))
        max_concurrency = self.settings.portfolio_refresh_instrument_concurrency
        logger.info(
            "portfolio_refresh_concurrency portfolioId=%s instrumentCount=%s maxConcurrency=%s",
            portfolio_id,
            len(instruments),
            max_concurrency,
        )
        semaphore = asyncio.Semaphore(max_concurrency)
        refreshed_global_instruments: set[UUID] = set()
        instrument_ids = {_safe_uuid(instrument.get("instrumentId")) for instrument in instruments}
        instrument_ids.discard(None)
        structured_records = await self.repository.structured_market_snapshots_for_instruments(instrument_ids)
        markets = {
            str(instrument.get("mic") or instrument.get("canonicalMic") or instrument.get("exchange") or "").upper()
            for instrument in instruments
        }
        market_data = await self.repository.market_session_data({market for market in markets if market})

        async def worker(instrument: dict) -> PortfolioResearchSummary:
            async with semaphore:
                global_instrument_id = instrument.get("instrumentId")
                started = time.monotonic()
                logger.info("portfolio_refresh_instrument_start globalInstrumentId=%s", global_instrument_id)
                try:
                    local = await self._refresh_portfolio_instrument(
                        portfolio_id,
                        instrument,
                        correlation_id,
                        refreshed_global_instruments,
                        structured_records,
                        market_data,
                    )
                    outcome = local.companies[0].status if local.companies else "NO_COMPANY"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "portfolio_refresh_instrument_complete globalInstrumentId=%s correlationId=%s outcome=FAILED reason=%s",
                        global_instrument_id,
                        correlation_id or "NONE",
                        type(exc).__name__,
                    )
                    local = PortfolioResearchSummary(portfolio_id=portfolio_id, companies_requested=1, companies_degraded=1)
                    local.companies.append(PortfolioResearchCompany(
                        instrument_id=_safe_uuid(instrument.get("instrumentId")),
                        company_name=_instrument_name(instrument),
                        ticker=_instrument_ticker(instrument),
                        exchange=_instrument_exchange(instrument),
                        isin=instrument.get("isin"),
                        provider=instrument.get("provider"),
                        provider_instrument_id=instrument.get("providerInstrumentId"),
                        asset_type=_instrument_asset_type(instrument) or None,
                        status="SEARCH_PROVIDER_UNAVAILABLE",
                        safe_error_code=type(exc).__name__,
                        safe_error_message=str(exc)[:500],
                    ))
                    outcome = "FAILED"
                logger.info(
                    "portfolio_refresh_instrument_complete globalInstrumentId=%s durationMs=%s outcome=%s",
                    global_instrument_id,
                    round((time.monotonic() - started) * 1000),
                    outcome,
                )
                if progress_callback is not None:
                    await progress_callback(local)
                return local

        locals_in_order = await asyncio.gather(*(worker(instrument) for instrument in instruments))
        result = PortfolioResearchSummary(portfolio_id=portfolio_id, companies_requested=len(instruments))
        for local in locals_in_order:
            result.companies_resolved += local.companies_resolved
            result.companies_failed += local.companies_failed
            result.companies_degraded += local.companies_degraded
            result.companies_succeeded += local.companies_succeeded
            result.documents_created += local.documents_created
            result.events_created += local.events_created
            result.companies.extend(local.companies)
        return await self._finalize_async(result)

    async def prepare_portfolio_refresh(self, portfolio_id: UUID, correlation_id: str | None = None,
                                        identity_headers: dict[str, str | None] | None = None) -> list[dict]:
        positions = await self._load_positions(portfolio_id, correlation_id, identity_headers)
        return self._dedupe_instruments(positions)

    async def _refresh_portfolio_instrument(
        self,
        portfolio_id: UUID,
        instrument: dict,
        correlation_id: str | None,
        refreshed_global_instruments: set[UUID],
        structured_records: dict[UUID, list[StructuredMarketSnapshotRecord]],
        market_data,
    ) -> PortfolioResearchSummary:
        """Run the existing sequential pipeline for exactly one instrument."""
        result = PortfolioResearchSummary(portfolio_id=portfolio_id, companies_requested=1)
        asset_type = _instrument_asset_type(instrument)
        logger.info("instrument_research_refresh_start globalInstrumentId=%s company=%s exchange=%s country=%s assetType=%s", instrument.get("instrumentId"), _instrument_name(instrument), _instrument_exchange(instrument), instrument.get("country"), asset_type or "UNKNOWN")
        if asset_type == "ETF":
            profile = self._resolve_etf_profile(instrument, register_missing=True)
            result.companies_resolved += 1
            documents_before = len(self.repository.documents_for(profile.instrument_id)) if profile else 0
            if profile:
                await self.refresh_instrument(
                    profile.instrument_id,
                    correlation_id=correlation_id,
                    instrument=instrument,
                    structured_records=structured_records.get(profile.instrument_id, []),
                    market_data=market_data,
                )
            documents_after = len(self.repository.documents_for(profile.instrument_id)) if profile else documents_before
            result.documents_created += max(documents_after - documents_before, 0)
            result.companies_degraded += 1
            result.companies.append(_etf_company_from_profile(profile, instrument, self.repository.documents_for(profile.instrument_id) if profile else [], self.repository.last_refresh.get(profile.instrument_id) if profile else None, self.repository.last_live_error.get(profile.instrument_id) if profile else "ETF_RESEARCH_NOT_REFRESHED"))
            return result
        if asset_type in {"FUND", "BOND", "CASH", "CRYPTO"}:
            result.companies_resolved += 1
            result.companies.append(_unsupported_asset_company(instrument, asset_type))
            return result
        profile = self._resolve_profile(instrument, register_missing=True)
        if profile is None:
            result.companies_failed += 1
            result.companies.append(PortfolioResearchCompany(instrument_id=_safe_uuid(instrument.get("instrumentId")), company_name=_instrument_name(instrument), ticker=_instrument_ticker(instrument), exchange=_instrument_exchange(instrument), isin=instrument.get("isin"), provider=instrument.get("provider"), provider_instrument_id=instrument.get("providerInstrumentId"), asset_type=asset_type or None, status="COMPANY_NOT_RESOLVED", safe_error_code="COMPANY_NOT_RESOLVED", safe_error_message="Holding identity did not match a registered research company."))
            return result
        result.companies_resolved += 1
        allow_demo = not _is_real_broker_instrument(instrument)
        if profile.instrument_id in refreshed_global_instruments:
            logger.info("instrument_research_refresh_reused portfolioId=%s globalInstrumentId=%s reason=DUPLICATE_RESOLVED_PROFILE", portfolio_id, profile.instrument_id)
            summary = self.repository.summary(profile.instrument_id, allow_demo=allow_demo)
            company = _company_from_summary(summary, status=_status_from_summary(summary, self.repository.last_live_error.get(profile.instrument_id)), source_instrument=instrument, safe_error_code=self.repository.last_live_error.get(profile.instrument_id))
            _attach_structured_market(company, self._cached_structured_snapshot(profile.instrument_id), None)
            result.companies_succeeded += int(company.status == "RESOLVED_RESEARCH_AVAILABLE")
            result.companies_degraded += int(company.status != "RESOLVED_RESEARCH_AVAILABLE")
            result.companies.append(company)
            return result
        refreshed_global_instruments.add(profile.instrument_id)
        structured_snapshot = None
        structured_error = None
        has_registered_sources = bool(registered_sources_for(profile.instrument_id))
        can_live_search = self.settings.research_live_enabled and self.settings.research_search_enabled
        structured_outcome = await self._reconcile_structured_market(
            profile.instrument_id,
            instrument,
            records=structured_records.get(profile.instrument_id, []),
            market_data=market_data,
        )
        structured_snapshot = structured_outcome.snapshot
        structured_error = structured_outcome.error
        if not has_registered_sources and not can_live_search and not _eligible_for_official_nse_research(profile):
            summary = self.repository.summary(profile.instrument_id, allow_demo=allow_demo)
            company = _company_from_summary(summary, status=_status_from_summary(summary, self.repository.last_live_error.get(profile.instrument_id)), source_instrument=instrument, safe_error_code=self.repository.last_live_error.get(profile.instrument_id))
            _attach_structured_market(company, structured_snapshot, structured_error)
            result.companies_succeeded += int(company.status == "RESOLVED_RESEARCH_AVAILABLE")
            result.companies_degraded += int(company.status != "RESOLVED_RESEARCH_AVAILABLE")
            result.companies.append(company)
            return result
        documents_before = len(self.repository.documents_for(profile.instrument_id))
        events_before = len(self.repository.events_for(profile.instrument_id))
        try:
            await self.refresh_instrument(
                profile.instrument_id,
                correlation_id=correlation_id,
                allow_demo=True,
                pre_resolved_categories=_structured_categories(structured_snapshot),
                instrument=instrument,
                structured_outcome=structured_outcome,
            )
            result.documents_created += max(len(self.repository.documents_for(profile.instrument_id)) - documents_before, 0)
            result.events_created += max(len(self.repository.events_for(profile.instrument_id)) - events_before, 0)
            summary = self.repository.summary(profile.instrument_id, allow_demo=allow_demo)
            company = _company_from_summary(summary, status=_status_from_summary(summary, self.repository.last_live_error.get(profile.instrument_id)), source_instrument=instrument, safe_error_code=self.repository.last_live_error.get(profile.instrument_id))
        except Exception as exc:
            result.companies_degraded += 1
            summary = self.repository.summary(profile.instrument_id, allow_demo=allow_demo)
            company = _company_from_summary(summary, status="RESOLVED_PARTIAL_DATA" if structured_snapshot else "SEARCH_PROVIDER_UNAVAILABLE", source_instrument=instrument, safe_error_code=type(exc).__name__, safe_error_message=str(exc)[:500])
        else:
            result.companies_succeeded += int(company.status == "RESOLVED_RESEARCH_AVAILABLE")
            result.companies_degraded += int(company.status != "RESOLVED_RESEARCH_AVAILABLE")
        _attach_structured_market(company, structured_snapshot, structured_error)
        result.companies.append(company)
        return result

    async def read_portfolio_summary(
        self,
        portfolio_id: UUID,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> PortfolioResearchSummary:
        started = time.perf_counter()
        positions = await self._load_positions(portfolio_id, correlation_id, identity_headers)
        instruments = self._dedupe_instruments(positions)
        result = PortfolioResearchSummary(portfolio_id=portfolio_id, companies_requested=len(instruments))
        for instrument in instruments:
            company = await self._read_company_state(instrument)
            result.companies.append(company)
            if company.status == "COMPANY_NOT_RESOLVED":
                result.companies_failed += 1
            else:
                result.companies_resolved += 1
                if company.status == "RESOLVED_RESEARCH_AVAILABLE":
                    result.companies_succeeded += 1
                elif company.status != "RESEARCH_NOT_APPLICABLE":
                    result.companies_degraded += 1
        await self._attach_durable_structured_snapshots(result)
        result = await self._finalize_async(result)
        logger.info(
            "portfolio_summary_stage stage=TOTAL portfolioId=%s positionCount=%s uniqueInstrumentCount=%s durationMs=%s",
            portfolio_id,
            len(positions),
            len(instruments),
            round((time.perf_counter() - started) * 1000),
        )
        return result

    def _finalize(self, result: PortfolioResearchSummary) -> PortfolioResearchSummary:
        facts_by_instrument = {
            company.instrument_id: self.repository.financial_facts_for(company.instrument_id)
            for company in result.companies if company.asset_type == "EQUITY" and company.instrument_id
        }
        return self._finalize_with_facts(result, facts_by_instrument)

    async def _finalize_async(self, result: PortfolioResearchSummary) -> PortfolioResearchSummary:
        started = time.perf_counter()
        instrument_ids = {company.instrument_id for company in result.companies if company.asset_type == "EQUITY" and company.instrument_id}
        facts_by_instrument = await self.repository.financial_facts_for_instruments(instrument_ids)
        threshold = Decimal(str(self.settings.research_ownership_change_threshold_percentage_points))
        for index, company in enumerate(result.companies):
            if company.asset_type != "EQUITY" or not company.instrument_id:
                continue
            documents = list(self.repository.documents_for(company.instrument_id))
            events = list(self.repository.events_for(company.instrument_id))
            enrichment_started = time.perf_counter()
            enriched = await asyncio.to_thread(
                self._enrich_company_snapshot, company.model_copy(deep=True), documents, events, threshold,
                list(facts_by_instrument.get(company.instrument_id, [])),
            )
            logger.info(
                "portfolio_summary_stage stage=ENRICH_COMPANY instrumentId=%s durationMs=%s documentCount=%s eventCount=%s financialFactCount=%s",
                company.instrument_id,
                round((time.perf_counter() - enrichment_started) * 1000),
                len(documents),
                len(events),
                len(facts_by_instrument.get(company.instrument_id, [])),
            )
            if enriched.structured_market:
                _attach_structured_market(enriched, enriched.structured_market, None)
            result.companies[index] = enriched
        result = self._finalize_company_counts(result)
        logger.info(
            "portfolio_summary_stage stage=FINALIZE companyCount=%s durationMs=%s",
            len(result.companies),
            round((time.perf_counter() - started) * 1000),
        )
        return result

    def _finalize_with_facts(self, result: PortfolioResearchSummary, facts_by_instrument) -> PortfolioResearchSummary:
        threshold = Decimal(str(self.settings.research_ownership_change_threshold_percentage_points))
        for company in result.companies:
            if company.asset_type == "EQUITY" and company.instrument_id:
                enrich_company_research(
                    company,
                    self.repository.documents_for(company.instrument_id),
                    self.repository.events_for(company.instrument_id),
                    threshold,
                    facts_by_instrument.get(company.instrument_id, []),
                )
                if company.structured_market:
                    _attach_structured_market(company, company.structured_market, None)
        return self._finalize_company_counts(result)


    def _finalize_company_counts(self, result: PortfolioResearchSummary) -> PortfolioResearchSummary:
        result.total_companies = result.companies_requested
        result.completed = sum(company.status == "RESOLVED_RESEARCH_AVAILABLE" for company in result.companies)
        result.partial = sum(company.status == "RESOLVED_PARTIAL_DATA" for company in result.companies)
        result.failed = sum(company.status in {
            "COMPANY_NOT_RESOLVED", "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_RETURNED_ZERO_RESULTS",
            "RESULTS_REJECTED", "DOCUMENT_FETCH_FAILED", "EXTRACTION_EMPTY", "RESOLVED_NO_SOURCES",
        } for company in result.companies)
        result.unsupported = sum(company.status in {"ETF_UNSUPPORTED", "RESEARCH_NOT_APPLICABLE"} for company in result.companies)
        result.in_progress = sum(company.status == "IN_PROGRESS" for company in result.companies)
        return result


    @staticmethod
    def _enrich_company_snapshot(company, documents, events, threshold, facts):
        enrich_company_research(company, documents, events, threshold, facts)
        return company

    async def _read_company_state(self, instrument: dict) -> PortfolioResearchCompany:
        started = time.perf_counter()
        summary_counts = {"documents": 0, "events": 0, "shareholding": 0}
        instrument_id = instrument.get("instrumentId")
        try:
            return await self._read_company_state_impl(instrument, summary_counts)
        finally:
            logger.info(
                "portfolio_summary_stage stage=READ_COMPANY instrumentId=%s durationMs=%s documentCount=%s eventCount=%s shareholdingCount=%s",
                instrument_id,
                round((time.perf_counter() - started) * 1000),
                summary_counts["documents"],
                summary_counts["events"],
                summary_counts["shareholding"],
            )

    async def _read_company_state_impl(
        self, instrument: dict, summary_counts: dict[str, int]
    ) -> PortfolioResearchCompany:
        asset_type = _instrument_asset_type(instrument)
        if asset_type == "ETF":
            profile = self._resolve_etf_profile(instrument, register_missing=False)
            if profile is None:
                profile = self._resolve_etf_profile(instrument, register_missing=True)
            company = _etf_company_from_profile(
                profile,
                instrument,
                self.repository.documents_for(profile.instrument_id) if profile else [],
                self.repository.last_refresh.get(profile.instrument_id) if profile else None,
                self.repository.last_live_error.get(profile.instrument_id) if profile else None,
            )
            await self._attach_current_structured_snapshot(company, instrument)
            return company
        if asset_type in {"FUND", "BOND", "CASH", "CRYPTO"}:
            return _unsupported_asset_company(instrument, asset_type)
        # A portfolio position already carries a globally resolved instrument
        # and verified provider mappings.  Absence of prior research must not
        # make that global instrument unavailable: register its process-local
        # research profile so the UI can offer the first refresh.
        profile = self._resolve_profile(
            instrument,
            register_missing=_safe_uuid(instrument.get("globalInstrumentId")) is not None,
        )
        if profile is None:
            enriched = _enriched_equity_company(instrument)
            if enriched is not None:
                return enriched
            return PortfolioResearchCompany(
                instrument_id=_safe_uuid(instrument.get("instrumentId")),
                company_name=_instrument_name(instrument),
                ticker=_instrument_ticker(instrument),
                exchange=_instrument_exchange(instrument),
                isin=instrument.get("isin"),
                provider=instrument.get("provider"),
                provider_instrument_id=instrument.get("providerInstrumentId"),
                asset_type=asset_type or None,
                status="COMPANY_NOT_RESOLVED",
                safe_error_code="COMPANY_NOT_RESOLVED",
                safe_error_message="Holding identity did not match a registered research company.",
            )
        allow_demo = not _is_real_broker_instrument(instrument)
        summary = self.repository.summary(profile.instrument_id, allow_demo=allow_demo)
        summary_counts.update(
            documents=len(summary.documents),
            events=len(summary.recent_events),
            shareholding=len(summary.shareholding_snapshots),
        )
        status = _status_from_summary(summary, self.repository.last_live_error.get(profile.instrument_id))
        company = _company_from_summary(
            summary,
            status=status,
            source_instrument=instrument,
            safe_error_code=self.repository.last_live_error.get(profile.instrument_id),
        )
        await self._attach_current_structured_snapshot(company, instrument)
        return company

    async def _attach_current_structured_snapshot(
        self, company: PortfolioResearchCompany, instrument: dict
    ) -> None:
        """Attach a cached or fresh provider snapshot when serving the drawer.

        The research summary must not depend on a previous refresh request having
        hit the same process. Yahoo collection has its own short quote cache and
        a durable verified mapping is supplied by portfolio-service, so this
        fallback reuses the mapping directly and never repeats discovery.
        """
        if not self.settings.structured_provider_enabled:
            return
        snapshot = self._cached_structured_snapshot(company.instrument_id) if company.instrument_id else None
        _attach_structured_market(company, snapshot, None)

    def _cached_structured_snapshot(self, instrument_id: UUID) -> object | None:
        cached = self._structured_by_instrument.get(instrument_id)
        if cached is None:
            return None
        expires_at, snapshot = cached
        if expires_at > datetime.now(timezone.utc):
            return snapshot
        self._structured_by_instrument.pop(instrument_id, None)
        return None

    def _store_structured_snapshot(self, instrument_id: UUID, snapshot: object) -> None:
        self._structured_by_instrument[instrument_id] = (
            datetime.now(timezone.utc) + timedelta(seconds=self.settings.structured_market_price_freshness_seconds),
            snapshot,
        )

    async def _persist_structured_snapshot(self, instrument_id: UUID, snapshot) -> None:
        now = datetime.now(timezone.utc)
        facts = snapshot.facts
        record = StructuredMarketSnapshotRecord(
            instrument_id=instrument_id, provider=snapshot.resolution.provider, provider_instrument_id=snapshot.resolution.provider_ticker,
            exchange=snapshot.resolution.exchange, currency=snapshot.resolution.currency, quote_type=snapshot.resolution.quote_type,
            source_url=snapshot.source_url, source_name=snapshot.source_name, source_type=snapshot.source_type,
            source_identity=snapshot.resolution.provider_ticker, market_as_of=snapshot.market_as_of, retrieved_at=snapshot.retrieved_at, persisted_at=now,
            last_price_at=now if _positive_decimal_fact(facts.get("latestPrice")) is not None else None,
            last_valuation_at=now if any(key in facts for key in ("trailingPE", "forwardPE", "priceToBook")) else None,
            last_fundamentals_at=now if any(key in facts for key in ("trailingEPS", "roe", "roa", "roce")) else None,
            last_analyst_at=now if any(key.startswith("publicAnalyst") for key in facts) else None,
            last_success_at=now, last_provider_attempt_at=now, acquisition_status="SUCCESS", snapshot=snapshot,
        )
        await self.repository.persist_structured_market_snapshot_async(record)
        self._store_structured_snapshot(instrument_id, snapshot)

    async def _attach_durable_structured_snapshots(self, result: PortfolioResearchSummary) -> None:
        ids = {company.instrument_id for company in result.companies if company.instrument_id}
        records = await self.repository.structured_market_snapshots_for_instruments(ids)
        markets = {str(company.primary_exchange or company.exchange or "").upper() for company in result.companies}
        schedules, exceptions = await self.repository.market_session_data(markets)
        now = datetime.now(timezone.utc)
        for company in result.companies:
            if not company.instrument_id:
                continue
            choices = records.get(company.instrument_id, [])
            record = next((item for item in choices if item.provider == "NSE_STRUCTURED"), None) or next((item for item in choices if item.provider == "YAHOO_FINANCE"), None) or (choices[0] if choices else None)
            _attach_durable_structured_market(company, record, schedules, exceptions, now, self.settings)

    async def _attach_global_durable_structured_snapshot(
        self, company: PortfolioResearchCompany
    ) -> None:
        """Attach the persisted structured-market snapshot for a single non-held company.

        The public/global research path never runs an interactive refresh, so the
        in-process cache attached by ``_attach_current_structured_snapshot`` is
        frequently empty.  Reuse the durable snapshot (the same source the
        portfolio path reads in bulk) so the drawer projects provider identity,
        sector, industry and market data instead of falling back to N/A.
        """
        if not company.instrument_id:
            return
        records = await self.repository.structured_market_snapshots_for_instruments(
            {company.instrument_id}
        )
        markets = {str(company.primary_exchange or company.exchange or "").upper()}
        if "" in markets:
            markets.discard("")
        schedules, exceptions = await self.repository.market_session_data(markets) if markets else ({}, {})
        now = datetime.now(timezone.utc)
        choices = records.get(company.instrument_id, [])
        record = _preferred_structured_record(choices)
        _attach_durable_structured_market(company, record, schedules, exceptions, now, self.settings)

    async def _finalize_global_company(
        self, company: PortfolioResearchCompany
    ) -> PortfolioResearchCompany:
        """Project durable financial/valuation facts for a single non-held company.

        Mirrors the per-company body of ``_finalize_async`` so the public research
        projection reads valuation state, quarterly and statement history and
        shareholding changes from the same durable facts as portfolio positions.
        """
        if company.asset_type != "EQUITY" or not company.instrument_id:
            return company
        facts_by_instrument = await self.repository.financial_facts_for_instruments(
            {company.instrument_id}
        )
        threshold = Decimal(str(self.settings.research_ownership_change_threshold_percentage_points))
        documents = list(self.repository.documents_for(company.instrument_id))
        events = list(self.repository.events_for(company.instrument_id))
        enriched = await asyncio.to_thread(
            self._enrich_company_snapshot, company.model_copy(deep=True), documents, events, threshold,
            list(facts_by_instrument.get(company.instrument_id, [])),
        )
        if enriched.structured_market:
            _attach_structured_market(enriched, enriched.structured_market, None)
        return enriched

    async def enrich_global_company_durables(
        self, company: PortfolioResearchCompany
    ) -> PortfolioResearchCompany:
        """Attach all durable facts to a public, non-held company row.

        This is the non-portfolio counterpart of ``_finalize_async``: it applies
        durable structured-market facts and durable financial/valuation evidence so
        the Stock Research drawer and research-intelligence sections project real
        data instead of N/A placeholders.  No provider work is performed and no
        portfolio/holding identity is mutated.
        """
        await self._attach_global_durable_structured_snapshot(company)
        return await self._finalize_global_company(company)

    async def search_instruments(
        self,
        query: str,
        region: str,
        *,
        limit: int = 20,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> list[dict]:
        """Search the global canonical instrument universe by name/symbol/ISIN.

        India uses the persisted canonical NSE-backed master; the
        USA/EUROPE universes use the verified portfolio-service active-equity
        master.  The browser never calls NSE or a price provider directly.
        Results are ranked by canonical identity match and backed by the durable
        read model; financial identity (sector/industry) is projected from the
        durable structured-market snapshot when available.
        """
        normalized_region = str(region).strip().upper()
        if normalized_region not in {"INDIA", "USA", "EUROPE"}:
            return []
        if len(query.strip()) < 3:
            return []
        limit = min(20, max(1, limit))
        raw_universe = await self.active_global_equities(
            correlation_id=correlation_id, identity_headers=identity_headers
        )
        candidates = [_normalize_search_item(item) for item in raw_universe]
        query_norm = query.strip().lower()
        matched = [
            candidate
            for candidate in candidates
            if _search_item_matches(candidate, query_norm)
            and belongs_to_region(candidate, normalized_region)
            and candidate["assetType"] == "EQUITY"
            and _safe_uuid(candidate["globalInstrumentId"]) is not None
        ]
        ids = {UUID(item["globalInstrumentId"]) for item in matched if item["globalInstrumentId"]}
        records = await self.repository.structured_market_snapshots_for_instruments(ids) if ids else {}
        for candidate in matched:
            candidate["region"] = normalized_region
            global_id = candidate["globalInstrumentId"]
            try:
                global_uuid = UUID(global_id)
            except (ValueError, TypeError):
                global_uuid = None
            if global_uuid is not None:
                candidate["sector"] = _sector_from_records(global_uuid, records) or candidate.get("sector")
                candidate["industry"] = _industry_from_records(global_uuid, records) or candidate.get("industry")
            score = self.repository.persisted_canonical_read_model_score(global_uuid) if global_uuid else None
            candidate["score"] = score.overall_score if score is not None else None
            candidate["_rank"] = _search_rank(candidate, query_norm)
        matched.sort(key=lambda item: (item["_rank"], -(item["score"] or 0)))
        for candidate in matched:
            candidate.pop("_rank", None)
        return matched[:limit]

    async def structured_quote(self, instrument: dict) -> object:
        if _instrument_asset_type(instrument) not in {"EQUITY", "ETF"}:
            raise StructuredProviderError("RESOLUTION_UNSUPPORTED_ASSET_TYPE")
        snapshot = await self.structured_provider.collect(instrument)
        instrument_id = _safe_uuid(instrument.get("instrumentId"))
        if instrument_id:
            await self._persist_structured_snapshot(instrument_id, snapshot)
        return snapshot

    async def _load_positions(
        self,
        portfolio_id: UUID,
        correlation_id: str | None,
        identity_headers: dict[str, str | None] | None,
    ) -> list[dict]:
        started = time.perf_counter()
        headers = {key: value for key, value in (identity_headers or {}).items() if value}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        response = await self._client.get(
            f"{self.settings.portfolio_service_base_url}/api/v1/portfolios/{portfolio_id}/positions",
            headers=headers or None,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("PORTFOLIO_POSITIONS_INVALID_RESPONSE")
        positions = [item for item in payload if isinstance(item, dict)]
        logger.info(
            "portfolio_summary_stage stage=LOAD_POSITIONS portfolioId=%s durationMs=%s positionCount=%s",
            portfolio_id,
            round((time.perf_counter() - started) * 1000),
            len(positions),
        )
        return positions

    def _dedupe_instruments(self, positions: list[dict]) -> list[dict]:
        instruments: list[dict] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        for position in positions:
            instrument = position.get("instrument")
            if not isinstance(instrument, dict):
                continue
            global_instrument_id = str(instrument.get("globalInstrumentId") or "").strip()
            key = (
                f"GLOBAL:{global_instrument_id}", "", "", "", "", ""
            ) if global_instrument_id else (
                str(instrument.get("instrumentId") or ""),
                str(instrument.get("provider") or "").upper(),
                str(instrument.get("providerInstrumentId") or "").upper(),
                str(instrument.get("isin") or "").upper(),
                str(instrument.get("ticker") or "").upper(),
                _normalize_exchange(instrument.get("exchange")),
            )
            if key in seen:
                continue
            seen.add(key)
            research_instrument = dict(instrument)
            research_instrument["instrumentId"] = instrument.get("globalInstrumentId") or instrument.get("instrumentId")
            for mapping in instrument.get("providerMappings") or []:
                if not _trusted_provider_mapping(mapping):
                    continue
                provider = str(mapping.get("provider") or "").upper()
                if provider == "YAHOO_FINANCE":
                    research_instrument.update({
                        "structuredProviderTicker": mapping.get("providerSymbol"),
                        "structuredProviderExchange": mapping.get("exchange"),
                        "structuredProviderCurrency": mapping.get("currency"),
                        "structuredProviderStatus": mapping.get("status"),
                    })
                elif provider == "NSE":
                    research_instrument["nseSymbol"] = mapping.get("providerSymbol")
                elif provider == "BSE":
                    research_instrument["bseSymbol"] = mapping.get("providerSymbol") or mapping.get("providerInstrumentId")
            research_instrument["_positionDataFreshness"] = position.get("dataFreshness")
            research_instrument["_researchCustomDisplayName"] = (
                position.get("customDisplayName") or position.get("userDisplayName")
            )
            research_instrument["_researchDisplayName"] = position.get("displayName")
            instruments.append(research_instrument)
        return instruments

    def _resolve_profile(self, instrument: dict, *, register_missing: bool) -> CompanyResearchProfile | None:
        instrument_id = _safe_uuid(instrument.get("instrumentId"))
        provider = str(instrument.get("provider") or "").upper()
        provider_instrument_id = str(instrument.get("providerInstrumentId") or "").upper()
        isin = str(instrument.get("isin") or "").upper()
        ticker = str(_instrument_ticker(instrument) or "").upper()
        exchange = _instrument_exchange(instrument)
        mic = _normalize_exchange(instrument.get("canonicalMic") or instrument.get("mic"))
        for profile in self.repository.list_profiles():
            if provider and provider_instrument_id:
                known = {key.upper(): value.upper() for key, value in profile.provider_instrument_ids.items()}
                if known.get(provider) == provider_instrument_id:
                    return _hydrate_verified_exchange_mappings(profile, instrument)
            if instrument_id and profile.instrument_id == instrument_id:
                return _hydrate_verified_exchange_mappings(profile, instrument)
            if isin and profile.isin and profile.isin.upper() == isin:
                return _hydrate_verified_exchange_mappings(profile, instrument)
            known_markets = {profile.exchange.upper(), profile.mic.upper()}
            if mic and mic != "UNKNOWN":
                known_markets.add(mic)
            if ticker == profile.ticker.upper() and exchange and exchange != "UNKNOWN" and exchange in known_markets:
                return _hydrate_verified_exchange_mappings(profile, instrument)
        if not register_missing:
            return None
        return self._register_equity_profile_from_instrument(instrument, provider, provider_instrument_id, isin, ticker, exchange, mic)

    def _resolve_etf_profile(self, instrument: dict, *, register_missing: bool) -> EtfResearchProfile | None:
        instrument_id = _safe_uuid(instrument.get("instrumentId"))
        provider = str(instrument.get("provider") or "").upper()
        provider_instrument_id = str(instrument.get("providerInstrumentId") or "").upper()
        isin = str(instrument.get("isin") or "").upper()
        ticker = str(_instrument_ticker(instrument) or "").upper()
        exchange = _instrument_exchange(instrument)
        for profile in self.repository.list_etf_profiles():
            if instrument_id and profile.instrument_id == instrument_id:
                return profile
            if provider and provider_instrument_id and profile.provider and profile.provider_instrument_id:
                if profile.provider.upper() == provider and profile.provider_instrument_id.upper() == provider_instrument_id:
                    return profile
            if isin and profile.isin and profile.isin.upper() == isin:
                return profile
        if not register_missing:
            return None
        if not ticker or ticker == "UNKNOWN" or not exchange or exchange == "UNKNOWN":
            return None
        fund_name = _instrument_name(instrument)
        if not fund_name or fund_name.upper() == "UNKNOWN":
            return None
        profile = EtfResearchProfile(
            instrument_id=instrument_id or uuid5(NAMESPACE_URL, "|".join([provider, provider_instrument_id, isin, ticker, exchange])),
            fund_id=uuid5(NAMESPACE_URL, f"etf|{isin}|{ticker}|{exchange}|{fund_name}"),
            fund_name=fund_name,
            ticker=ticker,
            exchange=exchange,
            mic=_normalize_exchange(instrument.get("canonicalMic") or instrument.get("mic")) or exchange,
            provider=provider or instrument.get("provider"),
            provider_instrument_id=provider_instrument_id or instrument.get("providerInstrumentId"),
            isin=isin or None,
            currency=str(instrument.get("tradingCurrency") or instrument.get("currency") or "").upper() or None,
            fund_provider=_infer_fund_provider_from_name(fund_name),
            underlying_index=_infer_underlying_index_from_name(fund_name),
        )
        return self.repository.register_etf_profile(profile)

    def _register_equity_profile_from_instrument(
            self,
            instrument: dict,
            provider: str,
            provider_instrument_id: str,
            isin: str,
            ticker: str,
            exchange: str,
            mic: str,
        ) -> CompanyResearchProfile | None:
        asset_type = _instrument_asset_type(instrument)
        company_name = _instrument_name(instrument)
        currency = str(instrument.get("tradingCurrency") or instrument.get("currency") or "").upper()
        country = str(instrument.get("country") or "").upper()
        if asset_type not in {"", "EQUITY"}:
            return None
        if not ticker or ticker == "UNKNOWN" or not exchange or exchange == "UNKNOWN":
            return None
        if not company_name or company_name.upper() in {"UNKNOWN", ticker}:
            return None
        instrument_id = _safe_uuid(instrument.get("instrumentId")) or uuid5(
            NAMESPACE_URL,
            "|".join([provider, provider_instrument_id, isin, ticker, exchange]),
        )
        profile = CompanyResearchProfile(
            instrument_id=instrument_id,
            company_id=uuid5(NAMESPACE_URL, f"company|{isin}|{ticker}|{exchange}|{company_name}"),
            company_name=company_name,
            aliases=_company_aliases(company_name, ticker),
            provider_instrument_ids={
                **({provider: provider_instrument_id} if provider and provider_instrument_id else {}),
                **({"NSE": str(instrument.get("nseSymbol"))} if instrument.get("nseSymbol") else {}),
                **({"BSE": str(instrument.get("bseSymbol"))} if instrument.get("bseSymbol") else {}),
                **({"YAHOO_FINANCE": str(instrument.get("structuredProviderTicker"))} if instrument.get("structuredProviderTicker") else {}),
                **{str(mapping.get("provider")).upper(): str(mapping.get("providerSymbol") or mapping.get("providerInstrumentId"))
                    for mapping in instrument.get("providerMappings") or []
                    if _trusted_provider_mapping(mapping) and mapping.get("provider")
                    and (mapping.get("providerSymbol") or mapping.get("providerInstrumentId"))},
            },
            isin=isin or None,
            ticker=ticker,
            exchange=exchange,
            mic=mic or exchange,
            country=country or "UNKNOWN",
            currency=currency or "UNKNOWN",
            known_domains=[],
        )
        self.repository.profiles.append(profile)
        return profile


def _company_from_summary(
    summary: ResearchSummary,
    *,
    status: str,
    source_instrument: dict | None = None,
    safe_error_code: str | None = None,
    safe_error_message: str | None = None,
) -> PortfolioResearchCompany:
    events = summary.recent_events
    positive = sum(1 for event in events if "POSITIVE" in event.impact)
    negative = sum(1 for event in events if "NEGATIVE" in event.impact)
    neutral = sum(1 for event in events if event.impact in {EventImpact.NEUTRAL, EventImpact.UNCERTAIN})
    read_model_score = canonical_read_model_score(summary.catalyst_score)
    evidence_coverage = {
        category: evidence.status for category, evidence in read_model_score.category_evidence.items()
    }
    durable_categories = {"ORDERS_BACKLOG", "CAPEX", "CLIENTS"}
    durable_evidence = {
        category: evidence.model_copy(update={"supporting_events": sorted(
            evidence.supporting_events,
            key=lambda event: event.event_date or event.published_at or event.detected_at,
            reverse=True,
        )[:5]})
        for category, evidence in read_model_score.category_evidence.items()
        if category in durable_categories
    }
    catalyst_events = [
        event for evidence in durable_evidence.values() for event in evidence.supporting_events
        if str(getattr(event.event_type, "value", event.event_type)) in {"CAPEX", "CAPACITY_EXPANSION", "NEW_FACILITY", "FACTORY_EXPANSION", "NEW_ORDER", "ORDER_WIN", "MAJOR_CONTRACT", "NEW_CONTRACT", "NEW_CUSTOMER", "CLIENT_WIN", "GEOGRAPHIC_EXPANSION", "ACQUISITION", "PRODUCT_LAUNCH", "REGULATORY_APPROVAL"}
    ]
    durable_evidence["CATALYSTS"] = CategoryEvidence(
        category="CATALYSTS",
        status=EvidenceState.NO_EVIDENCE if not catalyst_events else EvidenceState.NEUTRAL_EVIDENCE,
        score=None,
        event_count=len(catalyst_events),
        source_count=len({event.source_document_id for event in catalyst_events}),
        independent_source_count=len({event.independence_key or event.source_document_id for event in catalyst_events}),
        supporting_events=sorted(catalyst_events, key=lambda event: event.event_date or event.published_at or event.detected_at, reverse=True)[:5],
    )
    missing = [category for category, state in evidence_coverage.items() if state == "NO_EVIDENCE"]
    source_count = len({document.source_independence_key or document.canonical_url for document in summary.documents})
    listing_provider, listing_symbol, primary_exchange, verified_mappings = _listing_identity(summary.profile, source_instrument)
    return PortfolioResearchCompany(
        instrument_id=summary.profile.instrument_id,
        company_id=summary.profile.company_id,
        company_name=summary.profile.company_name,
        ticker=summary.profile.ticker,
        exchange=summary.profile.exchange,
        isin=summary.profile.isin,
        provider=source_instrument.get("provider") if source_instrument else None,
        provider_instrument_id=source_instrument.get("providerInstrumentId") if source_instrument else None,
        listing_provider=listing_provider,
        listing_symbol=listing_symbol,
        primary_exchange=primary_exchange,
        verified_provider_mappings=verified_mappings,
        asset_type=_instrument_asset_type(source_instrument) if source_instrument else "EQUITY",
        status=status if summary.documents or events or status != "AVAILABLE" else "RESEARCH_NOT_REFRESHED",
        catalyst_score=summary.catalyst_score.overall_score if summary.documents or events else None,
        confidence=summary.catalyst_score.research_confidence if summary.documents or events else None,
        evidence_coverage=evidence_coverage,
        durable_category_evidence=durable_evidence,
        latest_event=events[0] if events else None,
        positive_events_count=positive,
        negative_events_count=negative,
        neutral_events_count=neutral,
        document_count=len(summary.documents),
        event_count=len(events),
        source_count=source_count,
        last_refresh=summary.last_refresh_at,
        freshness=summary.data_freshness,
        mode="DEMO" if summary.demo else summary.data_freshness,
        missing_categories=missing,
        shareholding_snapshots=summary.shareholding_snapshots,
        shareholding_freshness=summary.shareholding_freshness,
        safe_error_code=safe_error_code,
        safe_error_message=safe_error_message,
    )


def _normalize_search_item(item: dict) -> dict:
    """Coerce a universe listing (NSE master or portfolio-service master) into the
    canonical search-candidate shape used by ``search_instruments``."""
    ticker = str(item.get("canonicalSymbol") or item.get("primarySymbol") or item.get("ticker") or item.get("symbol") or "").strip()
    company_name = (
        str(item.get("companyName") or item.get("canonicalName") or item.get("name") or ticker).strip()
    )
    provider_symbols = [
        str(mapping.get("providerSymbol") or mapping.get("providerInstrumentId") or "").strip()
        for mapping in (item.get("providerMappings") or [])
        if _trusted_provider_mapping(mapping) and str(mapping.get("status")).upper() == "VERIFIED"
    ]
    return {
        "globalInstrumentId": str(item.get("globalInstrumentId") or ""),
        "companyName": company_name,
        "symbol": ticker,
        "canonicalSymbol": ticker,
        "exchange": str(item.get("exchange") or item.get("primaryExchange") or item.get("mic") or "").strip(),
        "mic": str(item.get("canonicalMic") or item.get("mic") or "").strip(),
        "country": str(item.get("country") or "").strip().upper(),
        "currency": str(item.get("currency") or "").strip().upper(),
        "isin": str(item.get("isin") or "").strip().upper(),
        "sector": str(item.get("canonicalSector") or item.get("sector") or "").strip(),
        "industry": str(item.get("officialIndustry") or item.get("industry") or "").strip(),
        "assetType": str(item.get("assetType") or "EQUITY").strip().upper(),
        "providerSymbols": [symbol for symbol in provider_symbols if symbol],
    }


def _search_item_matches(candidate: dict, query: str) -> bool:
    """True when the query token appears against a canonical identity field."""
    if not query:
        return True
    targets: list[str] = []
    for key in ("symbol", "companyName", "isin"):
        value = candidate.get(key)
        if value:
            targets.append(str(value).strip().lower())
    targets.extend(
        str(symbol).strip().lower() for symbol in candidate.get("providerSymbols", [])
    )
    return any(query in value for value in targets if value)


def _search_rank(candidate: dict, query: str) -> int:
    """Rank match quality: 0 = exact identity, ascending = weaker match.

    Order: exact symbol/ISIN -> provider symbol -> symbol/name prefix ->
    ISIN prefix -> symbol/name substring.
    """
    symbol = (candidate.get("symbol") or "").strip().lower()
    company_name = (candidate.get("companyName") or "").strip().lower()
    isin = (candidate.get("isin") or "").strip().lower()
    provider_symbols = [
        str(symbol).strip().lower() for symbol in candidate.get("providerSymbols", [])
    ]
    if query == symbol or query == isin:
        return 0
    if query and any(query == symbol for symbol in provider_symbols):
        return 1
    if symbol.startswith(query):
        return 2
    if company_name.startswith(query):
        return 3
    if isin.startswith(query):
        return 4
    if query in symbol or query in company_name:
        return 5
    return 5


def _sector_from_records(global_instrument_id: UUID, records: dict[UUID, list]) -> str | None:
    record = _preferred_structured_record(records.get(global_instrument_id, []))
    if not record:
        return None
    facts = getattr(getattr(record, "snapshot", None), "facts", None)
    if not isinstance(facts, dict):
        return None
    fact = facts.get("sector")
    value = getattr(fact, "value", fact)
    return str(value) if value else None


def _industry_from_records(global_instrument_id: UUID, records: dict[UUID, list]) -> str | None:
    record = _preferred_structured_record(records.get(global_instrument_id, []))
    if not record:
        return None
    facts = getattr(getattr(record, "snapshot", None), "facts", None)
    if not isinstance(facts, dict):
        return None
    fact = facts.get("industry")
    value = getattr(fact, "value", fact)
    return str(value) if value else None


def _listing_identity(profile: CompanyResearchProfile | EtfResearchProfile, instrument: dict | None) -> tuple[str | None, str | None, str | None, dict[str, str]]:
    """Expose verified listing identities without changing broker provenance fields."""
    mappings: dict[str, str] = {}
    for mapping in (instrument or {}).get("providerMappings") or []:
        if not _trusted_provider_mapping(mapping):
            continue
        provider = str(mapping.get("provider") or "").upper()
        symbol = str(mapping.get("providerSymbol") or mapping.get("providerInstrumentId") or "").strip()
        if provider and symbol:
            mappings[provider] = symbol
    for provider, symbol in getattr(profile, "provider_instrument_ids", {}).items():
        if provider and symbol:
            mappings.setdefault(str(provider).upper(), str(symbol))
    nse_symbol = mappings.get("NSE")
    if nse_symbol:
        return "NSE", nse_symbol, profile.exchange, mappings
    return None, profile.ticker or _instrument_ticker(instrument or {}), profile.exchange, mappings


def _etf_company_from_profile(
    profile: EtfResearchProfile | None,
    instrument: dict,
    documents: list,
    last_refresh,
    safe_error_code: str | None,
) -> PortfolioResearchCompany:
    status = "ETF_UNSUPPORTED"
    if profile:
        listing_provider, listing_symbol, primary_exchange, verified_mappings = _listing_identity(profile, instrument)
    else:
        listing_provider, listing_symbol, primary_exchange, verified_mappings = None, None, _instrument_exchange(instrument), {}
    return PortfolioResearchCompany(
        instrument_id=profile.instrument_id if profile else _safe_uuid(instrument.get("instrumentId")),
        company_id=profile.fund_id if profile else None,
        company_name=profile.fund_name if profile else _instrument_name(instrument),
        ticker=profile.ticker if profile else _instrument_ticker(instrument),
        exchange=profile.exchange if profile else _instrument_exchange(instrument),
        isin=profile.isin if profile else instrument.get("isin"),
        provider=profile.provider if profile else instrument.get("provider"),
        provider_instrument_id=profile.provider_instrument_id if profile else instrument.get("providerInstrumentId"),
        listing_provider=listing_provider,
        listing_symbol=listing_symbol,
        primary_exchange=primary_exchange,
        verified_provider_mappings=verified_mappings,
        asset_type="ETF",
        status=status,
        document_count=len(documents),
        source_count=len({document.source_independence_key or document.canonical_url for document in documents}),
        last_refresh=last_refresh,
        freshness="REAL" if documents else "UNAVAILABLE",
        mode="REAL" if documents else "UNAVAILABLE",
        missing_categories=[] if documents else ["ETF_PROFILE", "ETF_PERFORMANCE", "INDEX_OUTLOOK", "ETF_RISK"],
        etf_profile=profile,
        safe_error_code=None if documents else safe_error_code,
        safe_error_message="Company-only research is not applicable to ETFs.",
    )


def _unsupported_asset_company(instrument: dict, asset_type: str) -> PortfolioResearchCompany:
    return PortfolioResearchCompany(
        instrument_id=_safe_uuid(instrument.get("instrumentId")),
        company_name=_instrument_name(instrument),
        ticker=_instrument_ticker(instrument),
        exchange=_instrument_exchange(instrument),
        isin=instrument.get("isin"),
        provider=instrument.get("provider"),
        provider_instrument_id=instrument.get("providerInstrumentId"),
        asset_type=asset_type,
        status="RESEARCH_NOT_APPLICABLE",
        freshness="INSTRUMENT_RESOLVED",
        mode="UNSUPPORTED_ASSET_TYPE",
        safe_error_code="RESEARCH_NOT_APPLICABLE",
        safe_error_message="Research is not applicable for this asset type.",
    )


def _safe_uuid(value) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _normalize_exchange(value) -> str:
    upper = str(value or "").upper()
    return {
        "IBIS": "XETR",
        "IBIS2": "XETR",
        "AEB": "XAMS",
    }.get(upper, upper)


def _instrument_ticker(instrument: dict) -> str | None:
    return instrument.get("canonicalSymbol") or instrument.get("ticker") or instrument.get("brokerSymbol")


def _instrument_exchange(instrument: dict) -> str:
    return _normalize_exchange(instrument.get("canonicalExchange") or instrument.get("exchange"))


def _instrument_name(instrument: dict) -> str:
    custom_name = _clean_name(instrument.get("_researchCustomDisplayName"))
    if custom_name:
        return custom_name
    ticker = _clean_name(instrument.get("ticker"))
    structured_name = _clean_name(instrument.get("companyName"))
    if structured_name and structured_name.upper() != ticker.upper() and not _legacy_composite_company_name(structured_name):
        return structured_name
    resolved_name = _clean_name(instrument.get("canonicalName"))
    if resolved_name and resolved_name.upper() != ticker.upper():
        return resolved_name
    for candidate in (instrument.get("_researchDisplayName"), structured_name, instrument.get("brokerDescription")):
        parsed = _legacy_composite_company_name(_clean_name(candidate))
        if parsed:
            return parsed
    return ticker or _clean_name(instrument.get("brokerSymbol")) or "Resolved instrument"


def _clean_name(value) -> str:
    return str(value or "").strip()


def _company_aliases(company_name: str, ticker: str) -> list[str]:
    legal_suffix = re.compile(
        r"\s+(?:n\.?v\.?|s\.?e\.?|a\.?g\.?|plc|ltd\.?|limited|inc\.?|corp\.?|corporation)$",
        re.IGNORECASE,
    )
    aliases: list[str] = []
    current = company_name.strip()
    while current:
        stripped = legal_suffix.sub("", current).strip(" ,.-")
        if stripped == current:
            break
        if stripped and stripped.lower() != company_name.lower() and stripped not in aliases:
            aliases.append(stripped)
        current = stripped
    if ticker and ticker not in aliases:
        aliases.append(ticker)
    return aliases


def _global_master_instrument(payload: dict, global_instrument_id: UUID) -> dict:
    """Normalize the public global-instrument API to the position instrument shape."""
    mappings = [mapping for mapping in payload.get("providerMappings") or [] if isinstance(mapping, dict)]
    verified = [mapping for mapping in mappings if _trusted_provider_mapping(mapping)]
    instrument = {
        "instrumentId": str(global_instrument_id),
        "globalInstrumentId": str(global_instrument_id),
        "isin": payload.get("isin"),
        "ticker": payload.get("primarySymbol"),
        "exchange": payload.get("primaryExchange"),
        "companyName": payload.get("canonicalName"),
        "canonicalName": payload.get("canonicalName"),
        "assetType": payload.get("assetType"),
        "country": payload.get("country"),
        "tradingCurrency": payload.get("currency"),
        "currency": payload.get("currency"),
        "providerMappings": mappings,
    }
    for mapping in verified:
        provider = str(mapping.get("provider") or "").upper()
        symbol = mapping.get("providerSymbol")
        if provider == "NSE" and symbol:
            instrument["nseSymbol"] = symbol
        elif provider == "BSE" and (symbol or mapping.get("providerInstrumentId")):
            instrument["bseSymbol"] = symbol or mapping.get("providerInstrumentId")
        elif provider == "YAHOO_FINANCE" and symbol:
            instrument.update({
                "structuredProviderTicker": symbol,
                "structuredProviderExchange": mapping.get("exchange"),
                "structuredProviderCurrency": mapping.get("currency"),
                "structuredProviderStatus": mapping.get("status"),
            })
    if not instrument["ticker"]:
        instrument["ticker"] = instrument.get("nseSymbol") or instrument.get("bseSymbol")
    if not instrument["exchange"]:
        instrument["exchange"] = "NSE" if instrument.get("nseSymbol") else ("BSE" if instrument.get("bseSymbol") else None)
    return instrument


def _hydrate_verified_exchange_mappings(profile: CompanyResearchProfile, instrument: dict) -> CompanyResearchProfile:
    """Keep a reused global profile current with verified exchange identities."""
    mappings = dict(profile.provider_instrument_ids)
    if instrument.get("nseSymbol"):
        mappings["NSE"] = str(instrument["nseSymbol"])
    if instrument.get("bseSymbol"):
        mappings["BSE"] = str(instrument["bseSymbol"])
    if instrument.get("structuredProviderTicker"):
        mappings["YAHOO_FINANCE"] = str(instrument["structuredProviderTicker"])
    for mapping in instrument.get("providerMappings") or []:
        if _trusted_provider_mapping(mapping):
            provider = str(mapping.get("provider") or "").upper()
            value = mapping.get("providerSymbol") or mapping.get("providerInstrumentId")
            if provider and value:
                mappings[provider] = str(value)
    if mappings != profile.provider_instrument_ids:
        profile.provider_instrument_ids = mappings
    return profile


def _refresh_profile_from_global_instrument(profile: CompanyResearchProfile, instrument: dict) -> None:
    """Replace provider identity metadata while preserving global research identity and evidence."""
    profile.company_name = _instrument_name(instrument)
    profile.isin = str(instrument.get("isin") or "").upper() or None
    profile.ticker = str(_instrument_ticker(instrument) or profile.ticker).upper()
    profile.exchange = _instrument_exchange(instrument) or profile.exchange
    profile.mic = _normalize_exchange(instrument.get("canonicalMic") or instrument.get("mic")) or profile.exchange
    profile.country = str(instrument.get("country") or profile.country).upper()
    profile.currency = str(instrument.get("tradingCurrency") or instrument.get("currency") or profile.currency).upper()
    profile.aliases = _company_aliases(profile.company_name, profile.ticker)
    authoritative: dict[str, str] = {}
    for mapping in instrument.get("providerMappings") or []:
        if not _trusted_provider_mapping(mapping):
            continue
        provider = str(mapping.get("provider") or "").upper()
        value = mapping.get("providerSymbol") or mapping.get("providerInstrumentId")
        if provider and value:
            authoritative[provider] = str(value)
    profile.provider_instrument_ids = authoritative


def _trusted_provider_mapping(mapping: object) -> bool:
    if not isinstance(mapping, dict) or str(mapping.get("status") or "").upper() not in {"VERIFIED", "RESOLVED"}:
        return False
    return not (
        str(mapping.get("provider") or "").upper() == "NSE"
        and str(mapping.get("resolutionSource") or mapping.get("resolution_source") or "").upper() == "BROKER_IMPORT_IDENTITY"
    )


def _eligible_for_official_nse_research(profile: CompanyResearchProfile) -> bool:
    return (
        profile.country.upper() in {"IN", "IND", "INDIA"}
        and profile.exchange.upper() in {"NSE", "XNSE"}
        and bool(profile.provider_instrument_ids.get("NSE"))
    )


def _legacy_composite_company_name(value: str) -> str | None:
    segments = [segment.strip() for segment in value.split("/")]
    return segments[2] if len(segments) >= 3 and segments[2] else None


def _attach_structured_market(company: PortfolioResearchCompany, snapshot, error: str | None) -> None:
    company.structured_market = snapshot
    company.structured_provider_status = snapshot.status if snapshot else (
        "STRUCTURED_PROVIDER_UNAVAILABLE" if error else None
    )
    if snapshot is None:
        return
    facts = snapshot.facts
    company.current_price = _positive_decimal_fact(facts.get("latestPrice"))
    if company.valuation.current_pe is None:
        company.valuation.current_pe = facts.get("trailingPE")
    if company.valuation.roe is None:
        company.valuation.roe = facts.get("roe")
    if company.valuation.roce is None:
        company.valuation.roce = facts.get("roce")
    # Structured evidence is usable even if every document/search provider failed.
    if company.status in {
        "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_RETURNED_ZERO_RESULTS", "RESULTS_REJECTED",
        "DOCUMENT_FETCH_FAILED", "EXTRACTION_EMPTY", "RESOLVED_NO_SOURCES", "SOURCE_DISCOVERY_UNAVAILABLE",
    }:
        company.status = "RESOLVED_PARTIAL_DATA"
        company.safe_error_message = "Structured market evidence is available; public document discovery remains incomplete."


def _attach_durable_structured_market(company: PortfolioResearchCompany, record, schedules, exceptions, now, settings) -> None:
    if record is None:
        company.structured_provider_status = "NEVER_FETCHED"
        company.price_freshness = "NEVER_FETCHED"
        company.market_status = market_session_status(company.primary_exchange or company.exchange, schedules, exceptions, now)
        return
    _attach_structured_market(company, record.snapshot, None)
    status = market_session_status(record.mic or record.exchange or company.primary_exchange or company.exchange, schedules, exceptions, now)
    company.market_status = status
    company.market_as_of = record.market_as_of
    company.structured_provider_status = record.acquisition_status
    company.price_freshness = "FRESH" if not class_due(record.last_price_at, settings.structured_market_price_freshness_seconds, now) else "STALE"
    company.public_analyst = _public_analyst_from_record(record, now, settings)
    company.market_fundamentals = _market_fundamentals_from_record(record, now, settings)
    latest = _positive_decimal_fact(record.snapshot.facts.get("latestPrice"))
    previous = _decimal_fact(record.snapshot.facts.get("previousClose"))
    company.current_price = latest
    if latest is None or previous is None:
        company.price_direction = "UNKNOWN"
        return
    change = latest - previous
    company.price_change = change
    company.price_direction = "UP" if change > 0 else "DOWN" if change < 0 else "UNCHANGED"
    company.price_change_percent = None if previous == 0 else (change / previous) * Decimal("100")


def _public_analyst_from_record(record, now, settings) -> PublicAnalyst | None:
    facts = record.snapshot.facts
    keys = {
        "target_low_price": "publicAnalystTargetLowPrice", "target_median_price": "publicAnalystTargetMedianPrice",
        "target_mean_price": "publicAnalystTargetMeanPrice", "target_high_price": "publicAnalystTargetHighPrice",
        "analyst_count": "publicAnalystCount", "recommendation_mean": "publicAnalystRecommendationMean",
        "consensus": "publicAnalystConsensus",
    }
    selected = {field: facts[key] for field, key in keys.items() if facts.get(key) is not None}
    if not selected:
        return None
    provenance = {(value.source_name, value.source_url) for value in selected.values()}
    source_name, source_url = next(iter(provenance)) if len(provenance) == 1 else (None, None)
    as_of_values = {value.as_of_date for value in selected.values()}
    retrieved_values = {value.retrieved_at for value in selected.values()}
    return PublicAnalyst(
        target_low_price=_decimal_fact(selected.get("target_low_price")), target_median_price=_decimal_fact(selected.get("target_median_price")),
        target_mean_price=_decimal_fact(selected.get("target_mean_price")), target_high_price=_decimal_fact(selected.get("target_high_price")),
        analyst_count=int(_decimal_fact(selected["analyst_count"])) if _decimal_fact(selected.get("analyst_count")) is not None else None,
        recommendation_mean=_decimal_fact(selected.get("recommendation_mean")), consensus=str(selected["consensus"].value) if selected.get("consensus") else None,
        currency=next((value.unit for field, value in selected.items() if field.startswith("target_") and value.unit), record.currency),
        provider=record.provider, provider_instrument_id=record.provider_instrument_id, source_name=source_name, source_url=source_url,
        as_of=next(iter(as_of_values)) if len(as_of_values) == 1 else None,
        retrieved_at=next(iter(retrieved_values)) if len(retrieved_values) == 1 else None,
        freshness="FRESH" if not class_due(record.last_analyst_at, settings.structured_analyst_freshness_seconds, now) else "STALE",
    )


def _market_fundamentals_from_record(record, now, settings) -> MarketFundamentals | None:
    keys = {"market_cap":"marketCap","enterprise_value":"enterpriseValue","trailing_pe":"trailingPE","forward_pe":"forwardPE","price_to_book":"priceToBook","price_to_sales":"priceToSales","ev_to_revenue":"evToRevenue","ev_to_ebitda":"evToEbitda","peg_ratio":"pegRatio","trailing_eps":"trailingEps","forward_eps":"forwardEps","book_value_per_share":"bookValue","roe":"roe","roa":"roa","debt_to_equity":"debtToEquity","profit_margin":"profitMargin","operating_margin":"operatingMargin","revenue_growth":"revenueGrowth","earnings_growth":"earningsGrowth","total_cash":"totalCash","total_debt":"totalDebt","free_cash_flow":"freeCashFlow","operating_cash_flow":"operatingCashFlow"}
    selected = {field: record.snapshot.facts[key] for field, key in keys.items() if record.snapshot.facts.get(key) is not None}
    if not selected: return None
    provenance = {(v.source_name, v.source_url) for v in selected.values()}
    source_name, source_url = next(iter(provenance)) if len(provenance) == 1 else (None, None)
    financial = _is_financial_identity({"sector": getattr(record.snapshot.facts.get("sector"), "value", None), "industry": getattr(record.snapshot.facts.get("industry"), "value", None), "longName": record.snapshot.resolution.company_name})
    semantics = ({"debt_to_equity": "BANK_SPECIFIC_INTERPRETATION_REQUIRED", "total_debt": "BANK_SPECIFIC_INTERPRETATION_REQUIRED", "operating_margin": "BANK_SPECIFIC_INTERPRETATION_REQUIRED", "ev_to_ebitda": "NOT_MEANINGFUL_FOR_FINANCIAL_ENTITY", "roce": "NOT_MEANINGFUL_FOR_FINANCIAL_ENTITY"} if financial else {})
    return MarketFundamentals(**{field: _decimal_fact(value) for field, value in selected.items()}, provider=record.provider, provider_instrument_id=record.provider_instrument_id, source_name=source_name, source_url=source_url, as_of=record.market_as_of, retrieved_at=record.retrieved_at, freshness="FRESH" if not class_due(record.last_fundamentals_at or record.last_valuation_at, settings.structured_fundamentals_freshness_seconds, now) else "STALE", metric_semantics=semantics)


def _decimal_fact(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value.value))
    except Exception:
        return None


def _positive_decimal_fact(value) -> Decimal | None:
    number = _decimal_fact(value)
    return number if number is not None and number.is_finite() and number > 0 else None


def _structured_categories(snapshot) -> set[str]:
    if snapshot is None:
        return set()
    facts = snapshot.facts
    categories: set[str] = set()
    if any(key in facts for key in ("trailingPE", "forwardPE", "priceToBook", "evToEbitda")):
        categories.add("VALUATION")
    if any(key in facts for key in ("publicAnalystConsensus", "publicAnalystTargetMeanPrice", "publicAnalystCount")):
        categories.update({"ANALYST_OPINION", "ANALYST_TARGETS"})
    # Yahoo insider/institution percentages intentionally do not satisfy Indian ownership categories.
    return categories


def _preferred_structured_record(records: list[StructuredMarketSnapshotRecord]) -> StructuredMarketSnapshotRecord | None:
    return (
        next((record for record in records if record.provider == "NSE_STRUCTURED"), None)
        or next((record for record in records if record.provider == "YAHOO_FINANCE"), None)
        or (records[0] if records else None)
    )


def _structured_due_classes(
    record: StructuredMarketSnapshotRecord | None,
    market_status: str,
    settings: Settings,
    now: datetime,
) -> set[str]:
    """Return durable structured classes due for an explicit refresh.

    A missing first-success snapshot deliberately includes slower classes, so
    closed or unknown exchange scheduling cannot starve initial acquisition.
    """
    if record is None or record.last_success_at is None:
        return {"PRICE", "VALUATION", "FUNDAMENTALS", "ANALYST"}
    due: set[str] = set()
    if price_sync_eligible(market_status, record.last_price_at, settings.structured_market_price_freshness_seconds, now):
        due.add("PRICE")
    if class_due(record.last_valuation_at or record.last_success_at, settings.structured_valuation_freshness_seconds, now):
        due.add("VALUATION")
    if class_due(record.last_fundamentals_at or record.last_success_at, settings.structured_fundamentals_freshness_seconds, now):
        due.add("FUNDAMENTALS")
    if class_due(record.last_analyst_at or record.last_success_at, settings.structured_analyst_freshness_seconds, now):
        due.add("ANALYST")
    return due


def _instrument_asset_type(instrument: dict) -> str:
    asset_type = str(instrument.get("assetType") or "").upper()
    security_type = str(instrument.get("securityType") or "").upper()
    identity = " ".join(str(instrument.get(key) or "") for key in (
        "companyName", "canonicalName", "brokerDescription", "displayName", "ticker", "canonicalSymbol"
    )).upper()
    if (
        security_type == "ETF"
        or re.search(r"\b(?:ETF|EXCHANGE[ -]TRADED FUND)\b", identity)
        or re.search(r"\b[A-Z]+BEES\b", identity)
    ):
        return "ETF"
    if security_type in {"ETF", "FUND", "MUTUALFUND", "MF"}:
        return "ETF" if security_type == "ETF" else "FUND"
    return asset_type


def _enriched_equity_company(instrument: dict) -> PortfolioResearchCompany | None:
    asset_type = _instrument_asset_type(instrument)
    ticker = _instrument_ticker(instrument)
    exchange = _instrument_exchange(instrument)
    company_name = _instrument_name(instrument).strip()
    if not instrument.get("canonicalName") or not instrument.get("canonicalSymbol") or not instrument.get("canonicalExchange"):
        return None
    if asset_type != "EQUITY" or not ticker or not exchange or exchange == "UNKNOWN":
        return None
    if not company_name or company_name.upper() in {"UNKNOWN", str(ticker).upper()}:
        return None
    return PortfolioResearchCompany(
        instrument_id=_safe_uuid(instrument.get("instrumentId")),
        company_id=uuid5(NAMESPACE_URL, f"company|{instrument.get('isin') or ''}|{ticker}|{exchange}|{company_name}"),
        company_name=company_name,
        ticker=ticker,
        exchange=exchange,
        isin=instrument.get("isin"),
        provider=instrument.get("provider"),
        provider_instrument_id=instrument.get("providerInstrumentId"),
        asset_type=asset_type,
        status="RESOLVED_NO_SOURCES",
        freshness="INSTRUMENT_RESOLVED",
        mode="UNAVAILABLE",
        safe_error_code="RESEARCH_NOT_REFRESHED",
        safe_error_message="No shared public research has been collected for this company yet.",
    )


def _is_real_broker_instrument(instrument: dict) -> bool:
    return str(instrument.get("_positionDataFreshness") or "").upper() == "REAL_BROKER"


def _infer_fund_provider_from_name(name: str) -> str | None:
    lower = name.lower()
    if "ishares" in lower or "blackrock" in lower:
        return "iShares"
    if "vanguard" in lower:
        return "Vanguard"
    if "xtrackers" in lower:
        return "Xtrackers"
    return None


def _infer_underlying_index_from_name(name: str) -> str | None:
    upper = name.upper()
    if "S&P 500" in upper or "SP 500" in upper:
        return "S&P 500"
    if "NASDAQ 100" in upper or "NASDAQ-100" in upper:
        return "NASDAQ 100"
    return None


def _status_from_summary(summary: ResearchSummary, live_error: str | None = None) -> str:
    if summary.documents or summary.recent_events:
        categories_with_evidence = sum(
            evidence.status != "NO_EVIDENCE"
            for evidence in summary.catalyst_score.category_evidence.values()
        )
        return "RESOLVED_RESEARCH_AVAILABLE" if categories_with_evidence >= 3 else "RESOLVED_PARTIAL_DATA"
    if live_error in {
        "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_RETURNED_ZERO_RESULTS", "RESULTS_REJECTED",
        "DOCUMENT_FETCH_FAILED", "EXTRACTION_EMPTY",
    }:
        return live_error
    if live_error and live_error.startswith("SEARCH_PROVIDER_UNAVAILABLE"):
        return "SEARCH_PROVIDER_UNAVAILABLE"
    if live_error == "GOOGLE_PROVIDER_UNAVAILABLE" or (live_error and live_error.startswith("SEARCH_PROVIDER")):
        return "SEARCH_PROVIDER_UNAVAILABLE"
    if live_error and live_error.startswith("SEARCH_SOURCE_UNAVAILABLE"):
        return "DOCUMENT_FETCH_FAILED"
    if live_error:
        return "SEARCH_PROVIDER_UNAVAILABLE"
    return "RESOLVED_NO_SOURCES"
