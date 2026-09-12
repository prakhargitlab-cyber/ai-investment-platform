"""Explicit, bounded INDIA historical-price population jobs.

This module is deliberately separate from Sector Performance reads. It may
invoke portfolio-service's mapping reconciliation and Yahoo only after an
authenticated operational POST starts a job.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

import yfinance as yf

from app.historical_market_data import (
    HistoricalPricePersistenceError,
    HistoricalPricePopulationService,
    HistoricalPriceProvider,
    HistoricalPriceProviderError,
    YahooHistoricalPriceProvider,
    has_year_historical_coverage,
)
from app.market_universe import IndiaMarketUniverseProvider, MarketUniverseInstrument, MarketUniverseUnavailable
from app.portfolio_orchestration import PortfolioServiceUnavailableError
from app.settings import Settings


_ACTIVE_STATUSES = {"QUEUED", "RUNNING"}
logger = logging.getLogger(__name__)


class IndiaMarketDataPopulationJobs:
    """One bounded in-process INDIA population worker with pollable status."""

    def __init__(
        self,
        repository,
        universe_provider: IndiaMarketUniverseProvider,
        orchestrator,
        settings: Settings,
        *,
        historical_provider: HistoricalPriceProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.repository = repository
        self.universe_provider = universe_provider
        self.orchestrator = orchestrator
        self.settings = settings
        self.historical_provider = historical_provider or YahooHistoricalPriceProvider(yf.Ticker)
        self.population = HistoricalPricePopulationService(repository, self.historical_provider)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_job_id: str | None = None

    async def submit(
        self,
        *,
        identity_headers: dict[str, str | None],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if self._active_job_id is not None:
            active = self._jobs.get(self._active_job_id)
            if active is not None and active["status"] in _ACTIVE_STATUSES:
                return _public_job(active)

        job_id = str(uuid4())
        job: dict[str, Any] = {
            "jobId": job_id,
            "status": "QUEUED",
            "region": "INDIA",
            "universeSize": 0,
            "attempted": 0,
            "populated": 0,
            "skipped": 0,
            "failed": 0,
            "reasons": {},
            "_failureStages": {},
            "_failureClasses": {},
            "startedAt": None,
            "completedAt": None,
        }
        self._jobs[job_id] = job
        self._active_job_id = job_id
        task = asyncio.create_task(
            self._run(job, dict(identity_headers), correlation_id),
            name=f"india-market-data-{job_id}",
        )
        self._tasks[job_id] = task
        task.add_done_callback(lambda _task, key=job_id: self._tasks.pop(key, None))
        return _public_job(job)

    def get(self, job_id: UUID | str) -> dict[str, Any] | None:
        job = self._jobs.get(str(job_id))
        return _public_job(job) if job is not None else None

    def active(self) -> dict[str, Any] | None:
        if self._active_job_id is None:
            return None
        job = self._jobs.get(self._active_job_id)
        return _public_job(job) if job is not None and job["status"] in _ACTIVE_STATUSES else None

    def latest(self) -> dict[str, Any] | None:
        if not self._jobs:
            return None
        job_id = next(reversed(self._jobs))
        return _public_job(self._jobs[job_id])

    async def wait(self, job_id: UUID | str) -> dict[str, Any] | None:
        task = self._tasks.get(str(job_id))
        if task is not None:
            await task
        return self.get(job_id)

    async def _run(
        self,
        job: dict[str, Any],
        identity_headers: dict[str, str | None],
        correlation_id: str | None,
    ) -> None:
        job.update(status="RUNNING", startedAt=self._clock())
        try:
            universe = await self.universe_provider.listings(
                correlation_id=correlation_id,
                identity_headers=identity_headers,
            )
        except MarketUniverseUnavailable:
            _record_reason(job, "UNIVERSE_UNAVAILABLE")
            job.update(status="FAILED", failed=1, completedAt=self._clock())
            _log_job_completed(job)
            return

        job["universeSize"] = len(universe)
        logger.info(
            "market_data_population event=POPULATION_JOB_STARTED jobId=%s universeSize=%s",
            job["jobId"],
            job["universeSize"],
        )
        batch_size = self.settings.market_data_population_batch_size
        for offset in range(0, len(universe), batch_size):
            batch = universe[offset:offset + batch_size]
            for instrument in batch:
                try:
                    await self._populate_one(job, instrument, identity_headers, correlation_id)
                except Exception:
                    # A malformed individual record must not strand or abort a
                    # market-wide job. Known failure modes are classified in
                    # _populate_one; this is a final per-item safety boundary.
                    job["failed"] += 1
                    _record_reason(job, "INSTRUMENT_PROCESSING_FAILED")
                finally:
                    delay = self.settings.market_data_population_request_interval_seconds
                    if instrument.canonical_sector and delay:
                        await self._sleep(delay)

        job.update(
            status="COMPLETED" if job["failed"] == 0 else "COMPLETED_WITH_ERRORS",
            completedAt=self._clock(),
        )
        _log_job_completed(job)

    async def _populate_one(
        self,
        job: dict[str, Any],
        instrument: MarketUniverseInstrument,
        identity_headers: dict[str, str | None],
        correlation_id: str | None,
    ) -> None:
        if not instrument.canonical_sector:
            job["skipped"] += 1
            _record_reason(job, "NO_CANONICAL_SECTOR")
            return

        job["attempted"] += 1
        try:
            metadata = await self.orchestrator.global_instrument_metadata(
                instrument.global_instrument_id,
                correlation_id=correlation_id,
                identity_headers=identity_headers,
            )
        except PortfolioServiceUnavailableError as exc:
            job["failed"] += 1
            _record_reason(job, "PROVIDER_UNAVAILABLE")
            _record_failure_stage(job, "PORTFOLIO_METADATA_UNAVAILABLE", exc)
            return

        mapping = _verified_yahoo_nse_mapping(metadata)
        if mapping is None:
            try:
                metadata = await self.orchestrator.reconcile_global_instrument_metadata(
                    instrument.global_instrument_id,
                    correlation_id=correlation_id,
                    identity_headers=identity_headers,
                )
            except PortfolioServiceUnavailableError as exc:
                job["failed"] += 1
                _record_reason(job, "PROVIDER_UNAVAILABLE")
                _record_failure_stage(job, "PORTFOLIO_RECONCILE_UNAVAILABLE", exc)
                return
            mapping = _verified_yahoo_nse_mapping(metadata)

        if mapping is None:
            job["skipped"] += 1
            _record_reason(job, "NO_VERIFIED_HISTORICAL_MAPPING")
            return

        now = self._clock()
        try:
            existing = (await self.repository.market_price_observations_for_instruments(
                {instrument.global_instrument_id}
            )).get(instrument.global_instrument_id, [])
        except Exception:
            job["failed"] += 1
            _record_reason(job, "PERSISTENCE_UNAVAILABLE")
            return
        initial_start = now - timedelta(days=self.settings.market_data_population_initial_lookback_days)
        observed_at = [value.observed_at for value in existing]
        year_coverage = bool(observed_at) and has_year_historical_coverage(
            min(observed_at),
            max(observed_at),
            len(observed_at),
        )
        if year_coverage:
            start = max(observed_at) + timedelta(days=1)
        else:
            start = initial_start
        end = now + timedelta(days=1)

        # Yahoo receives date-only bounds with an exclusive end. A YEAR-capable
        # series with no remaining date interval is a successful idempotent no-op.
        if year_coverage and start.date() >= end.date():
            job["populated"] += 1
            return

        payload = instrument.as_payload()
        payload["structuredProviderTicker"] = mapping
        try:
            written = await self.population.populate([payload], start=start, end=end)
        except HistoricalPriceProviderError as exc:
            job["failed"] += 1
            _record_reason(job, "PROVIDER_UNAVAILABLE")
            _record_failure_stage(job, "HISTORICAL_PROVIDER_UNAVAILABLE", exc)
            return
        except HistoricalPricePersistenceError:
            job["failed"] += 1
            _record_reason(job, "PERSISTENCE_UNAVAILABLE")
            return

        if written > 0:
            job["populated"] += 1
        else:
            job["skipped"] += 1
            _record_reason(job, "NO_VALID_HISTORY")


def _verified_yahoo_nse_mapping(metadata: dict[str, Any]) -> str | None:
    """Return only portfolio-service-owned, verified Yahoo NSE identity."""
    for mapping in metadata.get("providerMappings", []) if isinstance(metadata, dict) else []:
        if not isinstance(mapping, dict):
            continue
        symbol = str(mapping.get("providerSymbol") or "").strip()
        if (
            str(mapping.get("provider") or "").strip().upper() == "YAHOO_FINANCE"
            and str(mapping.get("status") or "").strip().upper() == "VERIFIED"
            and symbol.upper().endswith(".NS")
        ):
            return symbol
    return None


def _record_reason(job: dict[str, Any], reason: str) -> None:
    reasons = job["reasons"]
    reasons[reason] = reasons.get(reason, 0) + 1


def _record_failure_stage(job: dict[str, Any], stage: str, exc: Exception) -> None:
    stages = job["_failureStages"]
    stages[stage] = stages.get(stage, 0) + 1
    cause = exc.__cause__
    exception_class = type(cause).__name__ if cause is not None else type(exc).__name__
    classes = job["_failureClasses"]
    key = f"{stage}:{exception_class}"
    classes[key] = classes.get(key, 0) + 1


def _log_job_completed(job: dict[str, Any]) -> None:
    logger.info(
        "market_data_population event=POPULATION_JOB_COMPLETED jobId=%s status=%s "
        "attempted=%s populated=%s skipped=%s failed=%s reasons=%s "
        "failureStages=%s failureClasses=%s",
        job["jobId"],
        job["status"],
        job["attempted"],
        job["populated"],
        job["skipped"],
        job["failed"],
        job["reasons"],
        job["_failureStages"],
        job["_failureClasses"],
    )


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    value = dict(job)
    value["reasons"] = dict(job["reasons"])
    value.pop("_failureStages", None)
    value.pop("_failureClasses", None)
    return value
