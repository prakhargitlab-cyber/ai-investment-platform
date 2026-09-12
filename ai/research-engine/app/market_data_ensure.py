"""Lightweight dashboard orchestration for global INDIA market data."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.historical_market_data import has_year_historical_coverage
from app.market_universe import IndiaMarketUniverseProvider, MarketUniverseInstrument, MarketUniverseUnavailable
from app.settings import Settings


logger = logging.getLogger(__name__)


class IndiaMarketDataEnsureService:
    """Checks durable freshness and schedules heavy work without awaiting it."""

    def __init__(
        self,
        repository,
        universe_provider: IndiaMarketUniverseProvider,
        portfolio_orchestrator,
        population_jobs,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.universe_provider = universe_provider
        self.portfolio_orchestrator = portfolio_orchestrator
        self.population_jobs = population_jobs
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._last_refresh_failure_at: datetime | None = None

    async def ensure(
        self,
        *,
        identity_headers: dict[str, str | None],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        try:
            instruments = await self.universe_provider.listings(
                correlation_id=correlation_id,
                identity_headers=identity_headers,
            )
        except MarketUniverseUnavailable:
            # An unavailable INDIA universe at Dashboard bootstrap means the
            # durable Nifty reference cache cannot currently be read. Treat
            # that as reference data requiring refresh rather than terminating
            # ensure before the existing protected refresh path can run.
            refresh_status = await self._ensure_reference_refresh(correlation_id, now)
            return _response(refresh_status, None, "UNAVAILABLE", None)

        last_updated_at = _latest_retrieved_at(instruments)
        universe_stale = (
            not instruments
            or last_updated_at is None
            or now - last_updated_at > timedelta(hours=self.settings.market_data_nifty_freshness_hours)
        )
        historical = await self._historical_freshness(instruments, now)
        active_job = self.population_jobs.active()

        if universe_stale:
            refresh_status = await self._ensure_reference_refresh(correlation_id, now)
            historical_status = "RUNNING" if active_job else historical["status"]
            return _response(
                refresh_status,
                last_updated_at,
                historical_status,
                active_job.get("jobId") if active_job else None,
                historical,
            )

        if active_job is not None:
            logger.info(
                "market_data_ensure event=POPULATION_ACTIVE_REUSED jobId=%s jobStatus=%s",
                active_job.get("jobId"),
                active_job.get("status"),
            )
            return _response("FRESH", last_updated_at, "RUNNING", active_job.get("jobId"), historical)

        if historical["status"] == "STALE":
            if self._population_retry_deferred(now):
                latest_job = self.population_jobs.latest()
                logger.info(
                    "market_data_ensure event=POPULATION_RETRY_DEFERRED jobId=%s jobStatus=%s",
                    latest_job.get("jobId") if latest_job else None,
                    latest_job.get("status") if latest_job else None,
                )
                return _response("FRESH", last_updated_at, historical["status"], None, historical)

            logger.info("market_data_ensure event=POPULATION_SUBMIT_STARTED")
            job = await self.population_jobs.submit(
                identity_headers=_internal_service_identity(self.settings),
                correlation_id=correlation_id,
            )
            logger.info(
                "market_data_ensure event=POPULATION_SUBMIT_COMPLETED jobId=%s jobStatus=%s",
                job.get("jobId"),
                job.get("status"),
            )
            return _response("FRESH", last_updated_at, "POPULATION_STARTED", job.get("jobId"), historical)

        return _response("FRESH", last_updated_at, historical["status"], None, historical)

    async def wait_for_reference_refresh(self) -> None:
        task = self._refresh_task
        if task is not None:
            await task

    async def _historical_freshness(
        self,
        instruments: list[MarketUniverseInstrument],
        now: datetime,
    ) -> dict[str, Any]:
        eligible = [value for value in instruments if value.canonical_sector]
        if not eligible:
            historical = {
                "status": "STALE",
                "eligibleInstruments": 0,
                "coveredInstruments": 0,
                "lastObservedAt": None,
            }
            _log_historical_freshness(historical)
            return historical
        ids = {value.global_instrument_id for value in eligible}
        try:
            coverage = await self.repository.market_price_coverage_for_instruments(ids)
        except Exception as exc:
            logger.warning(
                "market_data_ensure event=HISTORICAL_COVERAGE_FAILED "
                "eligibleInstrumentCount=%s coveredInstrumentCount=0 exceptionClass=%s "
                "errorIdentifier=HISTORICAL_COVERAGE_UNAVAILABLE",
                len(eligible),
                type(exc).__name__,
            )
            historical = {
                "status": "UNAVAILABLE",
                "eligibleInstruments": len(eligible),
                "coveredInstruments": 0,
                "lastObservedAt": None,
            }
            _log_historical_freshness(historical)
            return historical

        covered = 0
        latest_covered: datetime | None = None
        oldest_latest_covered: datetime | None = None
        for instrument_id in ids:
            value = coverage.get(instrument_id)
            if value is None:
                continue
            first_observed_at, latest_observed_at, observation_count = value
            if not has_year_historical_coverage(
                first_observed_at,
                latest_observed_at,
                observation_count,
            ):
                continue
            covered += 1
            if latest_covered is None or latest_observed_at > latest_covered:
                latest_covered = latest_observed_at
            if oldest_latest_covered is None or latest_observed_at < oldest_latest_covered:
                oldest_latest_covered = latest_observed_at

        is_fresh = (
            covered == len(eligible)
            and oldest_latest_covered is not None
            and now - oldest_latest_covered <= timedelta(hours=self.settings.market_data_historical_freshness_hours)
        )
        historical = {
            "status": "FRESH" if is_fresh else "STALE",
            "eligibleInstruments": len(eligible),
            "coveredInstruments": covered,
            "lastObservedAt": latest_covered,
        }
        _log_historical_freshness(historical)
        return historical

    async def _ensure_reference_refresh(self, correlation_id: str | None, now: datetime) -> str:
        async with self._refresh_lock:
            if self._refresh_task is not None and not self._refresh_task.done():
                return "REFRESH_STARTED"
            if (
                self._last_refresh_failure_at is not None
                and now - self._last_refresh_failure_at
                < timedelta(minutes=self.settings.market_data_ensure_retry_cooldown_minutes)
            ):
                return "UNAVAILABLE"
            self._refresh_task = asyncio.create_task(
                self._refresh_reference_then_population(correlation_id),
                name="india-nifty-reference-ensure",
            )
            return "REFRESH_STARTED"

    async def _refresh_reference_then_population(self, correlation_id: str | None) -> None:
        identity = _internal_service_identity(self.settings)
        try:
            await self.portfolio_orchestrator.refresh_india_nifty500_reference(
                correlation_id=correlation_id,
                identity_headers=identity,
            )
            self._last_refresh_failure_at = None
            logger.info("market_data_ensure event=REFERENCE_REFRESH_COMPLETED")
        except Exception as exc:
            self._last_refresh_failure_at = self._clock()
            logger.warning(
                "market_data_ensure event=REFERENCE_REFRESH_FAILED exceptionClass=%s "
                "errorIdentifier=REFERENCE_REFRESH_FAILED",
                _diagnostic_exception_class(exc),
            )
            return

        try:
            instruments = await self.universe_provider.listings(
                correlation_id=correlation_id,
                identity_headers=identity,
            )
            instrument_count = len(instruments)
        except Exception as exc:
            logger.warning(
                "market_data_ensure event=POST_REFRESH_UNIVERSE_READ_FAILED exceptionClass=%s "
                "errorIdentifier=POST_REFRESH_UNIVERSE_UNAVAILABLE",
                type(exc).__name__,
            )
            return

        logger.info(
            "market_data_ensure event=POST_REFRESH_UNIVERSE_READ_COMPLETED instrumentCount=%s",
            instrument_count,
        )
        try:
            historical = await self._historical_freshness(instruments, self._clock())
            if historical["status"] != "STALE":
                return

            active_job = self.population_jobs.active()
            if active_job is not None:
                logger.info(
                    "market_data_ensure event=POPULATION_ACTIVE_REUSED jobId=%s jobStatus=%s",
                    active_job.get("jobId"),
                    active_job.get("status"),
                )
                return

            now = self._clock()
            if self._population_retry_deferred(now):
                latest_job = self.population_jobs.latest()
                logger.info(
                    "market_data_ensure event=POPULATION_RETRY_DEFERRED jobId=%s jobStatus=%s",
                    latest_job.get("jobId") if latest_job else None,
                    latest_job.get("status") if latest_job else None,
                )
                return

            logger.info("market_data_ensure event=POPULATION_SUBMIT_STARTED")
            # Submit is non-blocking and owns its duplicate-job guard.
            job = await self.population_jobs.submit(
                identity_headers=identity,
                correlation_id=correlation_id,
            )
            logger.info(
                "market_data_ensure event=POPULATION_SUBMIT_COMPLETED jobId=%s jobStatus=%s",
                job.get("jobId"),
                job.get("status"),
            )
        except Exception as exc:
            # A later ensure can retry population without repeating the now
            # successful universe refresh.
            logger.warning(
                "market_data_ensure event=POPULATION_HANDOFF_FAILED exceptionClass=%s "
                "errorIdentifier=POPULATION_HANDOFF_FAILED",
                type(exc).__name__,
            )
            return

    def _population_retry_deferred(self, now: datetime) -> bool:
        latest = self.population_jobs.latest()
        if latest is None or latest.get("status") in {"QUEUED", "RUNNING"}:
            return False
        completed_at = _as_utc(latest.get("completedAt"))
        return (
            completed_at is not None
            and now - completed_at < timedelta(hours=self.settings.market_data_population_retry_cooldown_hours)
        )


def _diagnostic_exception_class(exc: Exception) -> str:
    """Expose only the safe transport/error type hidden by a wrapper."""
    cause = exc.__cause__
    return type(cause).__name__ if cause is not None else type(exc).__name__


def _internal_service_identity(settings: Settings) -> dict[str, str]:
    """Server-owned identity; never derives ADMIN authority from browser input."""
    return {
        "X-AIP-User-Id": settings.market_data_internal_user_id,
        "X-AIP-User-Issuer": settings.market_data_internal_issuer,
        "X-AIP-User-Subject": settings.market_data_internal_subject,
        "X-AIP-User-Roles": "ADMIN",
    }


def _log_historical_freshness(historical: dict[str, Any]) -> None:
    logger.info(
        "market_data_ensure event=HISTORICAL_FRESHNESS_RESULT status=%s "
        "eligibleInstrumentCount=%s coveredInstrumentCount=%s",
        historical["status"],
        historical["eligibleInstruments"],
        historical["coveredInstruments"],
    )


def _latest_retrieved_at(instruments: list[MarketUniverseInstrument]) -> datetime | None:
    values = [_as_utc(value.retrieved_at) for value in instruments]
    usable = [value for value in values if value is not None]
    return max(usable, default=None)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _response(
    universe_status: str,
    last_updated_at: datetime | None,
    historical_status: str,
    job_id: str | None,
    historical: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = historical or {}
    return {
        "region": "INDIA",
        "universe": {
            "status": universe_status,
            "lastUpdatedAt": last_updated_at,
        },
        "historicalPrices": {
            "status": historical_status,
            "jobId": job_id,
            "lastObservedAt": history.get("lastObservedAt"),
            "eligibleInstruments": history.get("eligibleInstruments", 0),
            "coveredInstruments": history.get("coveredInstruments", 0),
        },
    }
