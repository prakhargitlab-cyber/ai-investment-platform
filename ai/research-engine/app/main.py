from typing import Any, Literal
from uuid import UUID, uuid4
import logging
import time
from contextlib import asynccontextmanager
import asyncio

import httpx
from fastapi import Body, FastAPI, File, Header, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field

from app.models import CategoryEvidence, PortfolioResearchCompany, PortfolioResearchSummary, ReliabilityLevel, ResearchEventType, ResearchSummary
from app.portfolio_orchestration import (
    GlobalInstrumentNotFoundError,
    PortfolioResearchOrchestrator,
    PortfolioServiceUnavailableError,
    WatchlistNotFoundError,
    WatchlistRegionMismatchError,
)
from app.structured_market import StructuredProviderError
from app.structured_financial import NseOfficialFinancialProvider
from app.repository import ResearchRepository
from app.scoring import canonical_read_model_score
from app.sector_leaderboard import build_sector_leaderboard
from app.sector_performance import belongs_to_region, performance_window, rank_performers
from app.market_universe import IndiaMarketUniverseProvider, MarketUniverseUnavailable
from app.market_data_population import IndiaMarketDataPopulationJobs
from app.market_data_ensure import IndiaMarketDataEnsureService, _internal_service_identity
from app.market_universe_sectors import canonical_sector_counts
from app.watchlists import AddWatchlistInstrumentRequest, EnsureDefaultWatchlistRequest, watchlist_research_projection
from app.scheduler import default_schedule_rules, nse_opportunity_schedule
from app.global_opportunity_scheduler import GlobalOpportunityScheduler
from app.settings import Settings, configure_application_logging, reset_request_id, set_request_id
from app.sources import default_source_providers
from app.research_readiness_runtime import (
    ExistingResearchCapabilityExecutor,
    RepositoryResearchReadinessAdapter,
    ResearchReadinessRuntime,
    jurisdiction_for_profile,
    readiness_response,
)
from app.stock_rule_engine import (
    StockRuleEngineService,
    analysis_eligibility_response,
)
from app.yahoo_mcp_acquisition import (
    HttpExternalResearchToolGateway,
    McpFirstResearchCapabilityExecutor,
)
from app.manual_evidence import (
    AcceptanceError,
    ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE,
    ALLOWED_METRICS_BY_EVIDENCE_TYPE,
    ManualEvidenceAcceptor,
    ManualEvidenceIngestor,
    SUPPORTED_EVIDENCE_TYPES,
    runtime_supported_file_type_labels,
)
from app.document_extraction import docx_extraction_available
from app.image_evidence_extraction import image_ocr_available, scanned_pdf_ocr_available
from app.models import EventImpact, TimeHorizon

settings = Settings()
configure_application_logging(settings)
repository = ResearchRepository(settings=settings)
portfolio_orchestrator = PortfolioResearchOrchestrator(repository, settings)
india_market_universe_provider = IndiaMarketUniverseProvider(portfolio_orchestrator)
market_data_population_jobs = IndiaMarketDataPopulationJobs(
    repository, india_market_universe_provider, portfolio_orchestrator, settings
)
market_data_ensure_service = IndiaMarketDataEnsureService(
    repository, india_market_universe_provider, portfolio_orchestrator,
    market_data_population_jobs, settings,
)
research_readiness_adapter = RepositoryResearchReadinessAdapter(repository)
existing_research_capability_executor = ExistingResearchCapabilityExecutor(
    repository, portfolio_orchestrator, market_data_population_jobs,
    official_financial_provider=NseOfficialFinancialProvider(
        repository._official_filing_discovery.client, repository._fetcher),
)
research_readiness_runtime = ResearchReadinessRuntime(
    repository,
    research_readiness_adapter,
    McpFirstResearchCapabilityExecutor(
        existing_research_capability_executor,
        repository,
        HttpExternalResearchToolGateway(
            settings.research_mcp_gateway_base_url,
            settings.research_mcp_gateway_timeout_seconds,
            settings.research_mcp_service_identity,
        ),
        enabled=settings.research_mcp_first_enabled,
    ),
    ensure_timeout_seconds=settings.research_readiness_ensure_timeout_seconds,
)
stock_rule_engine_service = StockRuleEngineService(repository, research_readiness_adapter)
manual_evidence_ingestor = ManualEvidenceIngestor(repository=repository)
manual_evidence_acceptor = ManualEvidenceAcceptor(
    repository=repository, runtime=research_readiness_runtime
)


@asynccontextmanager
async def research_lifespan(application):
    if hasattr(repository.persistence, 'record_opportunity_job'):
        # The worker (queue processor) always starts: manually-POSTed and
        # controlled/candidate cycles must still run even when the
        # automatic scheduler is disabled. Only the scheduler -- which
        # decides *when* to submit production cycles on its own -- is
        # gated by the narrow research_opportunity_scheduler_enabled flag.
        _opportunity_worker().start()
        if settings.research_opportunity_scheduler_enabled:
            _opportunity_scheduler().start()
    if hasattr(repository.persistence, 'create_cycle_run'):
        # ETF Radar's own worker, same generic lifecycle machinery, isolated
        # by market='ETF' (its own durable active-cycle slot/lease
        # namespace, never colliding with Equity's 'NSE' one). No scheduler:
        # ETF Radar cycles are only ever submitted on demand (API), never
        # auto-scheduled.
        _etf_radar_worker().start()
    try:
        yield
    finally:
        worker = getattr(application.state, 'opportunity_worker', None)
        if worker is not None:
            await worker.close()
            del application.state.opportunity_worker
        scheduler = getattr(application.state, 'opportunity_scheduler', None)
        if scheduler is not None:
            await scheduler.close()
            del application.state.opportunity_scheduler
        etf_worker = getattr(application.state, 'etf_radar_worker', None)
        if etf_worker is not None:
            await etf_worker.close()
            del application.state.etf_radar_worker
        # Readiness uses shielded single-flight tasks: explicitly drain them on shutdown.
        # Each instrument may now hold several concurrently running flights
        # (see ResearchReadinessRuntime.ensure's overlap-scoped join), so this
        # flattens every per-instrument list rather than assuming one task.
        flights = [flight.task for flight_list in research_readiness_runtime._flights.values() for flight in flight_list]
        for task in flights:
            task.cancel()
        await asyncio.gather(*flights, return_exceptions=True)
        # Bounded background work (currently: per-candidate optional
        # CURRENT_NEWS acquisitions kicked off by Stage-2 -- see
        # app/background_task_registry.py and
        # app/global_opportunity_orchestration.py) is deliberately never
        # cancelled here (unlike the readiness flights above): every entry
        # is real, worth-persisting evidence, not a resumable single-flight
        # computation. Give outstanding tasks a bounded window to finish and
        # persist on their own; anything still running past it is logged
        # (by drain() itself) rather than left to hang shutdown forever.
        from app.background_task_registry import current_news_background_tasks
        await current_news_background_tasks.drain(timeout=30)


app = FastAPI(title="Research Engine", version="0.3.0", lifespan=research_lifespan)
logger = logging.getLogger(__name__)


def _opportunity_worker():
    from app.opportunity_worker import OpportunityCycleWorker
    if not hasattr(app.state, 'opportunity_worker'):
        async def runner(**parameters):
            from app.global_opportunity_cycle import run_global_opportunity_cycle
            return await run_global_opportunity_cycle(repository, portfolio_orchestrator,
                **parameters, readiness_runtime=research_readiness_runtime,
                identity_headers=_internal_service_identity(portfolio_orchestrator.settings))
        app.state.opportunity_worker = OpportunityCycleWorker(repository, runner)
    return app.state.opportunity_worker


def _opportunity_scheduler():
    from app.opportunity_worker import OpportunityCycleWorker
    if not hasattr(app.state, 'opportunity_scheduler'):
        worker = _opportunity_worker()
        app.state.opportunity_scheduler = GlobalOpportunityScheduler(
            worker=worker,
            persistence=repository.persistence,
        )
    return app.state.opportunity_scheduler


def _etf_radar_worker():
    from app.opportunity_worker import OpportunityCycleWorker
    if not hasattr(app.state, 'etf_radar_worker'):
        async def etf_runner(**parameters):
            from app.etf_opportunity_cycle import run_etf_radar_cycle_async
            # FIX ETF RADAR 401 -- the background worker previously called
            # portfolio-service's instrument-enumeration endpoint
            # (GET /api/v1/instruments?...assetType=ETF...) with no identity
            # headers at all, unlike Equity's runner above (which has
            # always passed _internal_service_identity(...) into
            # run_global_opportunity_cycle), so portfolio-service rejected
            # it with 401. Same internal service identity, same settings.
            return await run_etf_radar_cycle_async(repository, portfolio_orchestrator, **parameters,
                identity_headers=_internal_service_identity(portfolio_orchestrator.settings))
        app.state.etf_radar_worker = OpportunityCycleWorker(repository, etf_runner, market='ETF')
    return app.state.etf_radar_worker


@app.get('/api/v1/research/opportunities/cycles/{cycle_id}/status')
async def opportunity_cycle_status(cycle_id: UUID):
    # Production discovery executes synchronous scanner reads in a worker
    # thread under the repository's serialization lock. Waiting for that lock
    # directly in this async route blocks Uvicorn's event loop, so one status
    # poll can also starve /health and /health/live. Route the targeted durable
    # read through the repository's existing bounded persistence offload.
    loader = getattr(repository.persistence, 'opportunity_job', None)
    run_blocking = getattr(repository, '_run_blocking_persistence', None)
    if run_blocking is not None and loader is not None:
        value = await run_blocking(loader, str(cycle_id))
    elif run_blocking is not None:
        jobs_loader = getattr(repository.persistence, 'opportunity_jobs', None)
        values = await run_blocking(jobs_loader) if jobs_loader is not None else []
        value = next((v for v in values if v['cycle_id'] == str(cycle_id)), None)
    else:
        # Compatibility for minimal SQLite-only test doubles. The production
        # ResearchRepository always supplies the offload boundary above.
        with repository._persistence_worker_lock:
            if loader is not None:
                value = loader(str(cycle_id))
            else:
                jobs_loader = getattr(repository.persistence, 'opportunity_jobs', None)
                values = jobs_loader() if jobs_loader is not None else []
                value = next((v for v in values if v['cycle_id'] == str(cycle_id)), None)
    if value is None:
        raise HTTPException(404, 'OPPORTUNITY_CYCLE_NOT_FOUND')
    return value


@app.get('/api/v1/research/opportunities/current')
async def opportunity_radar():
    with repository._persistence_worker_lock:
        return repository.persistence.global_opportunity_radar()


@app.get('/api/v1/research/recommendations/current')
async def current_portfolio_recommendations(
    global_instrument_id: list[UUID] = Query(default=[]),
):
    """Bulk current Radar projection for portfolio holdings."""
    with repository._persistence_worker_lock:
        return repository.persistence.portfolio_recommendation_signals(
            instrument_ids=global_instrument_id
        )


@app.get('/api/v1/research/opportunities/history/{instrument_id}')
async def opportunity_history(instrument_id: UUID):
    with repository._persistence_worker_lock:
        return repository.persistence.recommendation_history(instrument_id)


@app.get('/api/v1/research/etf-recommendations/current')
async def current_etf_portfolio_recommendations(
    global_instrument_id: list[UUID] = Query(default=[]),
):
    """Bulk current ETF Radar projection for portfolio holdings -- the ETF
    counterpart of /api/v1/research/recommendations/current above. DB-only
    read of the latest persisted ETF Radar cycle; never acquires or
    triggers a new cycle. Returns only global fields (score/recommendation/
    confidence/data completeness/as-of/version) -- callers supply their own
    private position data (quantity, cost, account) separately and merge
    client-side; this endpoint never receives or returns it."""
    with repository._persistence_worker_lock:
        return repository.persistence.etf_portfolio_recommendation_signals(
            instrument_ids=global_instrument_id
        )


# ---------------------------------------------------------------------------
# ETF Radar -- a separate, ETF-only Radar. Deliberately NOT routed through
# the Equity opportunity-cycle worker/job-queue machinery above: this is a
# bounded, synchronous, DB-evidence-only cycle (see
# app.etf_opportunity_cycle.run_etf_radar_cycle) with no company-specific
# acquisition, which also means the GET endpoints below never trigger any
# provider acquisition -- they only ever read already-persisted evidence.
# ---------------------------------------------------------------------------

@app.get('/api/v1/etf-radar/current')
async def etf_radar_current():
    """DB-only read of the most recently persisted ETF Radar cycle. Never
    triggers acquisition or a new cycle -- opening this endpoint is safe to
    call as often as the UI likes."""
    with repository._persistence_worker_lock:
        result = repository.persistence.latest_etf_radar_cycle()
    if result is None:
        raise HTTPException(404, 'ETF_RADAR_CYCLE_NOT_FOUND')
    return result


@app.get('/api/v1/etf-radar/cycles/{cycle_id}')
async def etf_radar_cycle_status(cycle_id: UUID):
    with repository._persistence_worker_lock:
        result = repository.persistence.etf_radar_cycle(cycle_id)
    if result is None:
        raise HTTPException(404, 'ETF_RADAR_CYCLE_NOT_FOUND')
    return result


class EtfRadarCycleRequest(BaseModel):
    top_n: int | None = Field(default=10, ge=1, le=100)
    candidate_ids: list[UUID] | None = Field(default=None, max_length=200)


@app.post('/api/v1/etf-radar/cycles')
async def etf_radar_cycle(body: EtfRadarCycleRequest, request: Request):
    """Start one ETF Radar cycle.

    An explicit candidate_ids override is a small, bounded diagnostic set
    (the only case where fixture-sized runs are expected -- see the project
    guardrails against ever running a full-universe ETF Radar outside this
    kind of bounded, explicit call); it is still evaluated synchronously and
    returned in the same response, exactly as before.

    Without an override, candidates come from the canonical ETF-only
    universe, which can be arbitrarily large, so the analysis runs on the
    same generic, durable, lease-owned worker/job lifecycle Equity Radar
    uses (app.opportunity_worker.OpportunityCycleWorker), under its own
    'ETF' market namespace. This endpoint returns as soon as the durable
    run row is created/coalesced -- it does NOT wait for admission,
    readiness, metrics, ETF_RULE_ENGINE_V1, risk gates, recommendation,
    ranking, or persistence to finish. Poll
    GET /api/v1/etf-radar/cycles/{cycle_id}/status for progress, and
    GET /api/v1/etf-radar/cycles/{cycle_id} (or /current) for the persisted
    result once status is COMPLETED. Either way, this endpoint itself never
    acquires anything from a provider, and never touches company-specific
    (Equity) research acquisition.
    """
    from app.etf_opportunity_cycle import etf_cycle_result_to_dict, run_etf_radar_cycle

    correlation_id = request.headers.get('x-correlation-id')
    if body.candidate_ids is not None:
        rows = [{'globalInstrumentId': str(instrument_id), 'assetType': 'ETF', 'exchange': 'NSE', 'status': 'ACTIVE'}
                for instrument_id in body.candidate_ids]
        with repository._persistence_worker_lock:
            result = run_etf_radar_cycle(rows, repository.persistence, correlation_id=correlation_id, top_n=body.top_n)
            payload = etf_cycle_result_to_dict(result)
            repository.persistence.save_etf_radar_cycle(result.cycle_id, result.radar_version, result.correlation_id,
                result.as_of, payload)
        return payload
    worker = _etf_radar_worker()
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=202, content=worker.submit({'top_n': body.top_n}))


def _etf_owned_run(store, cycle_id: str):
    """Fetch the durable run row, but only if it belongs to the ETF market
    namespace -- never leaks an Equity cycle's internal run/job state back
    through an ETF-Radar-labelled response, even though cycle_id (a UUID)
    is technically unique enough that no row would ever collide."""
    getter = getattr(store, 'cycle_run', None)
    run = getter(cycle_id) if getter is not None else None
    if run is None or run.get('market') != 'ETF':
        return None
    return run


@app.get('/api/v1/etf-radar/cycles/{cycle_id}/status')
async def etf_radar_cycle_lifecycle_status(cycle_id: UUID):
    """Durable lifecycle status (ACCEPTED/RUNNING/COMPLETED/FAILED/
    CANCEL_REQUESTED/CANCELLED) of a cycle submitted through the async
    worker above. DB-only; never acquires or re-runs anything. Distinct
    from GET /cycles/{cycle_id}, which returns the persisted ETF Radar
    RESULT payload (only available once status is COMPLETED)."""
    loader = getattr(repository.persistence, 'opportunity_job', None)
    run_blocking = getattr(repository, '_run_blocking_persistence', None)

    def _load():
        with repository._persistence_worker_lock:
            if _etf_owned_run(repository.persistence, str(cycle_id)) is None:
                return None
            return loader(str(cycle_id)) if loader is not None else None

    if run_blocking is not None:
        value = await run_blocking(_load)
    else:
        value = _load()
    if value is None:
        raise HTTPException(404, 'ETF_RADAR_CYCLE_NOT_FOUND')
    return value


@app.delete('/api/v1/etf-radar/cycles/{cycle_id}/cancel')
async def cancel_etf_radar_cycle(
    cycle_id: UUID,
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    """ADMIN-gated durable cancellation REQUEST for an ETF Radar cycle.

    Same two-phase semantics as Equity's
    DELETE /api/v1/research/opportunities/cycles/{cycle_id}/cancel, reusing
    the identical generic primitives (request_cycle_cancel / cycle_run /
    cycle_cancel_status) -- cycles are isolated by cycle_id (a UUID, unique
    across both Equity and ETF Radar), so this can never request
    cancellation of an Equity cycle or vice versa.
    """
    _require_market_data_admin(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_roles)
    store = repository.persistence
    if not hasattr(store, 'request_cycle_cancel'):
        raise HTTPException(503, 'RECOMMENDATION_PERSISTENCE_REQUIRED')

    def _request_cancel():
        with repository._persistence_worker_lock:
            run = _etf_owned_run(store, str(cycle_id))
            if run is None:
                raise HTTPException(404, 'ETF_RADAR_CYCLE_NOT_FOUND')
            previous_status = run.get('status')
            was_cancelled = store.cycle_cancel_status(str(cycle_id)) is not None
            transitioned = store.request_cycle_cancel(str(cycle_id))
            run = store.cycle_run(str(cycle_id)) or run
        return {
            'cycle_id': str(cycle_id),
            'previous_status': previous_status,
            'cancelled': was_cancelled or transitioned,
            'status': (run or {}).get('status'),
        }

    run_blocking = getattr(repository, '_run_blocking_persistence', None)
    return await run_blocking(_request_cancel) if run_blocking is not None else _request_cancel()


class OpportunityCycleRequest(BaseModel):
    # top_n is legacy display metadata (the UI initial page size). It is validated
    # only against its previous compatible range and is NEVER used to truncate the
    # qualifying persisted recommendation set: production passes top_n=None to the
    # orchestrator (unbounded ranking) and publication iterates ranking.evaluated_entries.
    top_n: int = Field(default=4, ge=2, le=4)
    shortlist_limit: int = Field(default=25, ge=1, le=100)
    candidate_ids: list[UUID] | None = Field(default=None, max_length=100)
    # BOUNDED (default): existing behavior -- shortlist_limit caps the deep-
    # investigation pool. FULL: deep-investigate every candidate that passes
    # legitimate baseline eligibility/admission, with shortlist_limit not
    # applied as a pre-deep cap (see app/global_opportunity_cycle.py). This
    # is persisted verbatim in the durable cycle/job parameters, so status,
    # resume, recovery, and historical inspection all see the real scope.
    analysis_scope: Literal['BOUNDED', 'FULL'] = 'BOUNDED'


@app.post('/api/v1/research/opportunities/cycles')
async def opportunity_cycle(body: OpportunityCycleRequest, request: Request):
    from app.global_opportunity_cycle import run_global_opportunity_cycle
    if not hasattr(repository.persistence, 'publish_opportunity_cycle'):
        raise HTTPException(503, 'RECOMMENDATION_PERSISTENCE_REQUIRED')
    # Operator-controlled recovery pause: refuse new cycle submissions while paused.
    # The cancellation REQUEST/STATUS API and the fenced /finalize-cancel endpoint
    # remain available. Both production and controlled submissions are blocked.
    if settings.research_opportunity_recovery_paused:
        raise HTTPException(503, 'RECOVERY_PAUSED')
    worker = _opportunity_worker()
    if body.candidate_ids is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=202, content=worker.submit(
            {'top_n': body.top_n, 'shortlist_limit': body.shortlist_limit, 'analysis_scope': body.analysis_scope}))
    # Diagnostics use the same process lock and never update production state.
    async with worker.run_lock:
        try:
            return await run_global_opportunity_cycle(repository, portfolio_orchestrator,
                top_n=body.top_n, shortlist_limit=body.shortlist_limit, candidate_ids=body.candidate_ids,
                analysis_scope=body.analysis_scope,
                readiness_runtime=research_readiness_runtime,
                identity_headers=_internal_service_identity(portfolio_orchestrator.settings))
        except PortfolioServiceUnavailableError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail='PORTFOLIO_SERVICE_UNAVAILABLE') from exc


@app.delete('/api/v1/research/opportunities/cycles/{cycle_id}/cancel')
async def cancel_opportunity_cycle(
    cycle_id: UUID,
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    """ADMIN-gated durable cycle cancellation REQUEST (two-phase).

    Phase 1 (request): sets status='CANCEL_REQUESTED' for the active cycle run
    (if still ACCEPTED/RUNNING/PUBLISHED), PRESERVING ownership, lease, and the
    active slot so the owning worker can drain in-flight work. Phase 2
    (terminal): the owning worker (or a drain-takeover worker) coerces to
    terminal 'CANCELLED' and releases ownership + the active slot.

    Idempotent: a second request on an already-CANCEL_REQUESTED (or terminal)
    cycle is a no-op and returns the current status. Does NOT restart, re-run,
    or mutate any evidence rows.

    NOTE: This endpoint can only affect a cycle handled by a *future* worker
    image that reads the cancellation flag. It cannot cancel a cycle currently
    executing under an already-running old worker image (see runbook step 2).
    """
    _require_market_data_admin(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_roles)
    store = repository.persistence
    if not hasattr(store, 'request_cycle_cancel'):
        raise HTTPException(503, 'RECOMMENDATION_PERSISTENCE_REQUIRED')

    def _request_cancel():
        # Keep the existing atomic read/transition/read sequence and lock. In
        # production this closure runs in the repository worker thread, where
        # the outer serialization lock is safely re-entrant.
        with repository._persistence_worker_lock:
            run = store.cycle_run(str(cycle_id))
            if run is None:
                raise HTTPException(404, 'OPPORTUNITY_CYCLE_NOT_FOUND')
            previous_status = run.get('status')
            was_cancelled = store.cycle_cancel_status(str(cycle_id)) is not None
            transitioned = store.request_cycle_cancel(str(cycle_id))
            run = store.cycle_run(str(cycle_id)) or run
        return {
            'cycle_id': str(cycle_id),
            'previous_status': previous_status,
            'cancelled': was_cancelled or transitioned,
            'status': (run or {}).get('status'),
            'status_phase': 'CANCEL_REQUESTED' if (was_cancelled or transitioned) and run.get('status') == 'CANCEL_REQUESTED' else ('CANCELLED' if run.get('status') == 'CANCELLED' else None),
        }

    run_blocking = getattr(repository, '_run_blocking_persistence', None)
    return await run_blocking(_request_cancel) if run_blocking is not None else _request_cancel()


@app.post('/api/v1/research/opportunities/cycles/{cycle_id}/finalize-cancel')
async def finalize_cycle_cancel(
    cycle_id: UUID,
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    """ADMIN-gated fenced terminal transition for an unowned CANCEL_REQUESTED cycle.

    Phase 2 finalization that does NOT invoke the research runner. Used when the
    owning worker has terminated (old-worker transition) or while the recovery
    pause is active. Transitions CANCEL_REQUESTED -> terminal CANCELLED, releasing
    ownership/lease and removing the active slot. Only affects cycles currently in
    CANCEL_REQUESTED — a terminal/PUBLISHED/COMPLETED/FAILED cycle is never touched.
    Idempotent: already-CANCELLED returns 409 (already terminal).
    """
    _require_market_data_admin(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_roles)
    store = repository.persistence
    if not hasattr(store, 'cancel_cycle_run_unowned'):
        raise HTTPException(503, 'RECOMMENDATION_PERSISTENCE_REQUIRED')
    with repository._persistence_worker_lock:
        run = store.cycle_run(str(cycle_id))
        if run is None:
            raise HTTPException(404, 'OPPORTUNITY_CYCLE_NOT_FOUND')
        status = run.get('status')
        if status == 'CANCELLED':
            raise HTTPException(409, 'ALREADY_CANCELLED')
        if status != 'CANCEL_REQUESTED':
            raise HTTPException(409, f'NOT_IN_CANCEL_REQUESTED: {status}')
        # Fenced terminal transition: status='CANCEL_REQUESTED' -> 'CANCELLED'.
        # Does NOT require owner_id (the cycle may be unowned).
        transitioned = store.cancel_cycle_run_unowned(str(cycle_id))
        run = store.cycle_run(str(cycle_id)) or run
    return {
        'cycle_id': str(cycle_id),
        'transitioned': transitioned,
        'status': run.get('status'),
    }


class BacktestRequest(BaseModel):
    start: str
    end: str
    market: str = 'NSE'
    horizon: str = 'SHORT_TERM'
    benchmark_id: UUID | None = None
    # Pins the evaluation clock ("as of" date) instead of always using the
    # real current time -- makes NOT_MATURED vs EVALUATED/MISSING_MARKET_DATA
    # reproducible for a chosen evaluation date. Optional and backward
    # compatible: omitted, behavior is identical to before this field existed.
    as_of: str | None = None


@app.get('/api/v1/research/backtesting/runs')
async def backtest_runs():
    with repository._persistence_worker_lock:
        return repository.persistence.backtests()


@app.post('/api/v1/research/backtesting/runs')
async def create_backtest(body: BacktestRequest):
    from app.recommendation_backtesting import run_backtest
    if not hasattr(repository.persistence, 'save_backtest'):
        raise HTTPException(503, 'RECOMMENDATION_PERSISTENCE_REQUIRED')
    if body.market != 'NSE':
        raise HTTPException(422, 'UNSUPPORTED_MARKET')
    try:
        with repository._persistence_worker_lock:
            return run_backtest(repository.persistence, start=body.start, end=body.end,
                                horizon=body.horizon, benchmark_id=body.benchmark_id, as_of=body.as_of)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    candidate = request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-Id")
    request_id = candidate.strip() if candidate else ""
    if not request_id or len(request_id) > 120 or not all(
        character.isalnum() or character in "-_.:/" for character in request_id
    ):
        request_id = str(uuid4())
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Correlation-Id"] = request_id
    return response


class ResearchReadinessEnsureRequest(BaseModel):
    requirements: list[str] | None = Field(default=None)


class StockRuleEngineAnalysisRequest(BaseModel):
    allow_partial: bool = Field(default=False, alias="allowPartial")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/health/live")
def liveness() -> dict[str, str]:
    """Cheap process liveness check for the Kubernetes liveness probe.

    Keep this independent of persistence, providers, readiness, and Radar
    state.  A slow dependency or a busy event loop must make the pod
    temporarily unready, never look dead to Kubernetes.
    """
    return {"status": "ok", "service": settings.service_name}


@app.get("/providers/llm")
def llm_provider() -> dict[str, str]:
    """Report the configured LLM provider and prove config-driven resolution works.

    `mode` stays "optional-not-called": no business logic currently invokes the
    resolved provider's structured_extract. This endpoint exists to demonstrate
    that provider selection is entirely configuration-driven (AIP_LLM_PROVIDER)
    via app.llm.get_llm_provider's registry, so adding a future provider (e.g.
    Llama) only requires implementing the LlmProvider contract and registering it
    there, never touching this endpoint or any domain/business logic.
    """
    from app.llm import get_llm_provider

    resolved = get_llm_provider(settings.llm_provider)
    return {
        "provider": settings.llm_provider,
        "resolved_provider_class": type(resolved).__name__,
        "mode": "optional-not-called",
    }


@app.get("/api/v1/research/sources")
def sources():
    return default_source_providers()


@app.get("/api/v1/research/schedule")
def schedule():
    return {"source_rules": default_schedule_rules(), "opportunity_schedule": nse_opportunity_schedule()}


@app.get("/api/v1/research/companies")
def companies(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    """Paginated list of all profiles with deterministic ordering.

    Backward-compatible: callers that omit page/page_size receive the first
    50 profiles. The old no-argument form /api/v1/research/companies still
    works — it just gets page 1 with the default page size.
    """
    profiles = repository.list_profiles()
    total = len(profiles)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = profiles[start:end]
    return {
        "items": page_items,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "hasMore": end < total,
    }


@app.get("/api/v1/research/readiness/{global_instrument_id}")
async def research_readiness(
    global_instrument_id: UUID,
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    """Read public-company readiness without invoking any research provider."""
    started = time.perf_counter()
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    identity_headers = _identity_headers(
        x_aip_user_id,
        x_aip_user_issuer,
        x_aip_user_subject,
        x_aip_user_email,
        x_aip_user_display_name,
        x_aip_user_roles,
    )
    profile = await _readiness_profile(
        global_instrument_id, x_correlation_id, identity_headers
    )
    result = await research_readiness_runtime.read(
        global_instrument_id, jurisdiction=jurisdiction_for_profile(profile)
    )
    response = readiness_response(result, research_readiness_runtime.requirement_registry)
    response["analysisEligibility"] = analysis_eligibility_response(result)
    logger.info(
        "research_flow operation=READINESS_GET globalInstrumentId=%s outcome=SUCCESS durationMs=%s",
        global_instrument_id,
        round((time.perf_counter() - started) * 1000),
    )
    return response


@app.post("/api/v1/research/readiness/{global_instrument_id}/ensure")
async def ensure_research_readiness(
    global_instrument_id: UUID,
    payload: ResearchReadinessEnsureRequest | None = Body(default=None),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    """Execute only stale/missing planner targets, then re-read readiness."""
    started = time.perf_counter()
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    identity_headers = _identity_headers(
        x_aip_user_id,
        x_aip_user_issuer,
        x_aip_user_subject,
        x_aip_user_email,
        x_aip_user_display_name,
        x_aip_user_roles,
    )
    profile = await _readiness_profile(
        global_instrument_id, x_correlation_id, identity_headers
    )
    try:
        result = await research_readiness_runtime.ensure(
            global_instrument_id,
            jurisdiction=jurisdiction_for_profile(profile),
            requirement_ids=payload.requirements if payload else None,
            correlation_id=x_correlation_id,
            identity_headers=identity_headers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = readiness_response(
        result.readiness,
        research_readiness_runtime.requirement_registry,
        ensure=result,
    )
    response["analysisEligibility"] = analysis_eligibility_response(result.readiness)
    logger.info(
        "research_flow operation=TARGETED_ENSURE globalInstrumentId=%s outcome=SUCCESS durationMs=%s planned=%s executed=%s failures=%s singleFlightReused=%s",
        global_instrument_id,
        round((time.perf_counter() - started) * 1000),
        len(result.planned_requirement_ids),
        len(result.executed_capabilities),
        len(result.failures),
        result.reused_single_flight,
    )
    return response


@app.post("/api/v1/research/analysis/{global_instrument_id}")
async def analyze_company_research(
    global_instrument_id: UUID,
    payload: StockRuleEngineAnalysisRequest | None = Body(default=None),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    """Calculate a versioned public-company score using durable data only."""
    started = time.perf_counter()
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    identity_headers = _identity_headers(
        x_aip_user_id,
        x_aip_user_issuer,
        x_aip_user_subject,
        x_aip_user_email,
        x_aip_user_display_name,
        x_aip_user_roles,
    )
    profile = await _readiness_profile(
        global_instrument_id, x_correlation_id, identity_headers
    )
    readiness = await research_readiness_runtime.read(
        global_instrument_id, jurisdiction=jurisdiction_for_profile(profile)
    )
    result = await stock_rule_engine_service.analyze(
        profile,
        readiness,
        allow_partial=payload.allow_partial if payload else False,
    )
    logger.info(
        "research_flow operation=RULE_ENGINE_ANALYSIS globalInstrumentId=%s outcome=SUCCESS durationMs=%s cacheHit=%s decisionSignal=%s",
        global_instrument_id,
        round((time.perf_counter() - started) * 1000),
        result.cache_hit,
        result.decision_signal,
    )
    return result.model_dump(mode="json", by_alias=True)


@app.get("/api/v1/research/instruments/search")
async def search_research_instruments(
    q: str = Query(..., min_length=3, max_length=128),
    region: str = Query(default="INDIA"),
    limit: int = Query(default=20, ge=1, le=20),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
) -> list[dict]:
    """Search the global canonical instrument universe from official masters.

    India uses the persisted canonical NSE-backed master; the USA and
    EUROPE universes use the verified portfolio-service active-equity master.  This
    never proxies a browser-side NSE/Yahoo text search and never restricts results
    to portfolio holdings or a watchlist.
    """
    started = time.perf_counter()
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    q = q.strip()
    if len(q) < 3:
        raise HTTPException(status_code=422, detail="SEARCH_QUERY_TOO_SHORT")
    normalized_region = str(region).strip().upper()
    if normalized_region not in {"USA", "EUROPE", "INDIA"}:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_REGION")
    identity_headers = _identity_headers(
        x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
        x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
    )
    try:
        results = await portfolio_orchestrator.search_instruments(
            q,
            normalized_region,
            limit=limit,
            correlation_id=x_correlation_id,
            identity_headers=identity_headers,
        )
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(
            status_code=502,
            detail="Portfolio service unavailable for instrument search",
        ) from exc
    logger.info(
        "research_flow operation=INSTRUMENT_SEARCH region=%s query=%s matches=%s durationMs=%s",
        normalized_region,
        q[:64],
        len(results),
        round((time.perf_counter() - started) * 1000),
    )
    return results


@app.get("/api/v1/research/sector-leaderboard")
async def sector_leaderboard(
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
    ):
    candidates = []
    identity_headers = _identity_headers(
        x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
        x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
    )
    for item in await portfolio_orchestrator.active_global_equities(
        correlation_id=x_correlation_id, identity_headers=identity_headers
    ):
        try:
            instrument_id = UUID(str(item["globalInstrumentId"]))
        except (KeyError, ValueError):
            continue
        score = repository.persisted_canonical_read_model_score(instrument_id)
        if score is None or score.overall_score is None:
            continue
        records = repository.structured_market_snapshots_for({instrument_id}).get(instrument_id, [])
        sector = next((str(record.snapshot.facts["sector"].value) for record in records if record.snapshot.facts.get("sector") and record.snapshot.facts["sector"].value), None)
        if not sector:
            continue
        coverage = sum(1 for value in score.category_evidence.values() if value.score is not None)
        candidates.append({"globalInstrumentId": str(instrument_id), "companyName": item.get("canonicalName"), "ticker": item.get("ticker"), "exchange": item.get("exchange"), "country": item.get("country"), "currency": item.get("currency"), "sector": sector, "industry": next((str(record.snapshot.facts["industry"].value) for record in records if record.snapshot.facts.get("industry")), None), "score": score.overall_score, "evidenceCoverage": coverage})
    return build_sector_leaderboard(candidates)


@app.get("/api/v1/research/sector-performance")
async def sector_performance(
    region: str = Query(...), sector: str = Query(...), period: str = Query(...), limit: int = Query(default=5),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None), x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None), x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None), x_aip_user_roles: str | None = Header(default=None),
):
    from app.sector_leaderboard import normalize_sector
    normalized_sector, sector_label = normalize_sector(sector)
    normalized_period = period.upper()
    if normalized_period not in {"DAY", "WEEK", "MONTH", "YEAR"}:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_PERFORMANCE_PERIOD")
    normalized_region = region.upper()
    if normalized_region not in {"USA", "EUROPE", "INDIA"}:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_REGION")
    identity_headers = _identity_headers(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_email, x_aip_user_display_name, x_aip_user_roles)
    if normalized_region == "INDIA":
        try:
            universe = [value.as_payload() for value in await india_market_universe_provider.listings(
                correlation_id=x_correlation_id, identity_headers=identity_headers
            )]
        except MarketUniverseUnavailable as exc:
            raise HTTPException(status_code=503, detail="INDIA_MARKET_UNIVERSE_UNAVAILABLE") from exc
    else:
        universe = await portfolio_orchestrator.active_global_equities(
            correlation_id=x_correlation_id, identity_headers=identity_headers
        )
    eligible = [item for item in universe if belongs_to_region(item, normalized_region)]
    ids = {UUID(str(item["globalInstrumentId"])) for item in eligible if item.get("globalInstrumentId")}
    snapshots = {} if normalized_region == "INDIA" else repository.structured_market_snapshots_for(ids)
    observations = repository.market_price_observations_for(ids)
    candidates = []
    for item in eligible:
        try:
            instrument_id = UUID(str(item["globalInstrumentId"]))
        except (KeyError, ValueError):
            continue
        records = snapshots.get(instrument_id, [])
        raw_sector = (item.get("canonicalSector") if normalized_region == "INDIA" else
                      next((str(record.snapshot.facts["sector"].value) for record in records if record.snapshot.facts.get("sector") and record.snapshot.facts["sector"].value), None))
        if not raw_sector or normalize_sector(raw_sector)[0] != normalized_sector:
            continue
        window = performance_window(observations.get(instrument_id, []), normalized_period)
        if window is None:
            continue
        latest, reference, performance_pct = window
        candidates.append({"globalInstrumentId": str(instrument_id), "companyName": item.get("canonicalName"), "ticker": item.get("ticker"), "exchange": item.get("exchange"), "currency": latest.currency or item.get("currency"), "latestPrice": latest.price, "referencePrice": reference.price, "performancePct": performance_pct, "_asOf": latest.observed_at})
    best, worst = rank_performers(candidates, limit)
    selected = [*best, *worst]
    return {"region": normalized_region, "sector": sector_label, "period": normalized_period,
            "asOf": max((value["_asOf"] for value in selected), default=None),
            "bestPerformers": [{key: value for key, value in row.items() if key != "_asOf"} for row in best],
            "worstPerformers": [{key: value for key, value in row.items() if key != "_asOf"} for row in worst]}


@app.get("/api/v1/research/market-universe/sectors")
async def market_universe_sectors(
    region: str = Query(...),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    """List sectors represented by the selected region's durable universe."""
    normalized_region = str(region).strip().upper()
    if normalized_region not in {"USA", "EUROPE", "INDIA"}:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_REGION")
    identity_headers = _identity_headers(
        x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
        x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
    )
    try:
        if normalized_region == "INDIA":
            universe = [
                value.as_payload()
                for value in await india_market_universe_provider.listings(
                    correlation_id=x_correlation_id,
                    identity_headers=identity_headers,
                )
            ]
        else:
            universe = await portfolio_orchestrator.active_global_equities(
                correlation_id=x_correlation_id,
                identity_headers=identity_headers,
            )
    except (MarketUniverseUnavailable, PortfolioServiceUnavailableError) as exc:
        raise HTTPException(status_code=503, detail="MARKET_UNIVERSE_UNAVAILABLE") from exc

    eligible = [item for item in universe if belongs_to_region(item, normalized_region)]
    if normalized_region == "INDIA":
        sector_by_instrument = {
            str(item.get("globalInstrumentId") or "").strip(): item.get("canonicalSector")
            for item in eligible
        }
    else:
        ids_by_text: dict[str, UUID] = {}
        for item in eligible:
            try:
                instrument_id = UUID(str(item["globalInstrumentId"]))
            except (KeyError, ValueError, TypeError):
                continue
            ids_by_text[str(instrument_id)] = instrument_id
        snapshots = repository.structured_market_snapshots_for(set(ids_by_text.values()))
        sector_by_instrument = {
            identity: next(
                (
                    str(record.snapshot.facts["sector"].value)
                    for record in snapshots.get(instrument_id, [])
                    if record.snapshot.facts.get("sector")
                    and record.snapshot.facts["sector"].value
                ),
                None,
            )
            for identity, instrument_id in ids_by_text.items()
        }

    return {
        "region": normalized_region,
        "sectors": canonical_sector_counts(eligible, sector_by_instrument),
    }


@app.get("/api/v1/research/watchlists")
async def list_research_watchlists(
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    try:
        return await portfolio_orchestrator.list_watchlists(
            correlation_id=x_correlation_id,
            identity_headers=_identity_headers(
                x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
                x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
            ),
        )
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(status_code=502, detail="WATCHLIST_SERVICE_UNAVAILABLE") from exc


@app.post("/api/v1/research/watchlists/default/ensure")
async def ensure_default_research_watchlist(
    payload: EnsureDefaultWatchlistRequest,
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    try:
        return await portfolio_orchestrator.ensure_default_watchlist(
            payload.region,
            correlation_id=x_correlation_id,
            identity_headers=_identity_headers(
                x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
                x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
            ),
        )
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(status_code=502, detail="WATCHLIST_SERVICE_UNAVAILABLE") from exc


@app.post("/api/v1/research/watchlists/{watchlist_id}/instruments")
async def add_research_watchlist_instrument(
    watchlist_id: UUID,
    payload: AddWatchlistInstrumentRequest,
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    try:
        return await portfolio_orchestrator.add_watchlist_instrument(
            watchlist_id,
            payload.portfolio_payload(),
            correlation_id=x_correlation_id,
            identity_headers=_identity_headers(
                x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
                x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
            ),
        )
    except WatchlistRegionMismatchError as exc:
        raise HTTPException(status_code=409, detail="WATCHLIST_REGION_MISMATCH") from exc
    except WatchlistNotFoundError as exc:
        raise HTTPException(status_code=404, detail="WATCHLIST_NOT_FOUND") from exc
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(status_code=502, detail="WATCHLIST_SERVICE_UNAVAILABLE") from exc


@app.delete("/api/v1/research/watchlists/{watchlist_id}/instruments/{instrument_id}", status_code=204)
async def remove_research_watchlist_instrument(
    watchlist_id: UUID,
    instrument_id: UUID,
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    try:
        await portfolio_orchestrator.remove_watchlist_instrument(
            watchlist_id, instrument_id, correlation_id=x_correlation_id,
            identity_headers=_identity_headers(
                x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
                x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
            ),
        )
    except WatchlistNotFoundError as exc:
        raise HTTPException(status_code=404, detail="WATCHLIST_NOT_FOUND") from exc
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(status_code=502, detail="WATCHLIST_SERVICE_UNAVAILABLE") from exc


@app.get("/api/v1/research/watchlists/{watchlist_id}/research")
async def research_watchlist_presentation(
    watchlist_id: UUID,
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    identity = _identity_headers(
        x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
        x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
    )
    try:
        watchlist = await portfolio_orchestrator.watchlist(
            watchlist_id, correlation_id=x_correlation_id, identity_headers=identity,
        )
        return await watchlist_research_projection(
            portfolio_orchestrator, watchlist,
            correlation_id=x_correlation_id, identity_headers=identity,
        )
    except WatchlistNotFoundError as exc:
        raise HTTPException(status_code=404, detail="WATCHLIST_NOT_FOUND") from exc
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(status_code=502, detail="WATCHLIST_SERVICE_UNAVAILABLE") from exc


@app.post("/api/v1/research/market-data/population", status_code=status.HTTP_202_ACCEPTED)
async def start_market_data_population(
    region: str = Query(...),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_market_data_admin(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_roles)
    if str(region).strip().upper() != "INDIA":
        raise HTTPException(status_code=400, detail="MARKET_DATA_POPULATION_REGION_NOT_SUPPORTED")
    return await market_data_population_jobs.submit(
        identity_headers=_identity_headers(
            x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
            x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
        ),
        correlation_id=x_correlation_id,
    )


@app.post("/api/v1/research/market-data/ensure")
async def ensure_market_data(
    region: str = Query(...),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_market_data_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    if str(region).strip().upper() != "INDIA":
        raise HTTPException(status_code=400, detail="MARKET_DATA_ENSURE_REGION_NOT_SUPPORTED")
    return await market_data_ensure_service.ensure(
        identity_headers=_identity_headers(
            x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
            x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
        ),
        correlation_id=x_correlation_id,
    )


@app.get("/api/v1/research/market-data/population/{job_id}")
async def market_data_population_status(
    job_id: UUID,
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    _require_market_data_admin(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_roles)
    job = market_data_population_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="MARKET_DATA_POPULATION_JOB_NOT_FOUND")
    return job


@app.get("/api/v1/research/companies/{instrument_id}")
async def company(instrument_id: UUID, x_correlation_id: str | None = Header(default=None), x_aip_user_id: str | None = Header(default=None), x_aip_user_issuer: str | None = Header(default=None), x_aip_user_subject: str | None = Header(default=None), x_aip_user_email: str | None = Header(default=None), x_aip_user_display_name: str | None = Header(default=None), x_aip_user_roles: str | None = Header(default=None)):
    try:
        return repository.profile(instrument_id)
    except StopIteration as exc:
        profile_reason: dict = {}
        if await _resolve_restored_instrument_profile(instrument_id, x_correlation_id, _identity_headers(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_email, x_aip_user_display_name, x_aip_user_roles), reason_out=profile_reason):
            return repository.profile(instrument_id)
        not_resolved_reason = profile_reason.get("reason")
        not_resolved_detail = f"COMPANY_NOT_RESOLVED:{not_resolved_reason}" if not_resolved_reason else "COMPANY_NOT_RESOLVED"
        raise HTTPException(status_code=404, detail=not_resolved_detail) from exc


@app.get("/api/v1/research/companies/{instrument_id}/events")
async def events(
    instrument_id: UUID,
    event_type: ResearchEventType | None = Query(default=None, alias="eventType"),
    impact: str | None = None,
    reliability: ReliabilityLevel | None = None,
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
):
    await _require_profile_or_restored_instrument(instrument_id, x_correlation_id, _identity_headers(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, None, None, None))
    return repository.events_for(instrument_id, event_type=event_type, impact=impact, reliability=reliability)


@app.get("/api/v1/research/companies/{instrument_id}/documents")
async def documents(instrument_id: UUID, x_correlation_id: str | None = Header(default=None), x_aip_user_id: str | None = Header(default=None), x_aip_user_issuer: str | None = Header(default=None), x_aip_user_subject: str | None = Header(default=None)):
    await _require_profile_or_restored_instrument(instrument_id, x_correlation_id, _identity_headers(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, None, None, None))
    return repository.documents_for(instrument_id)


@app.get("/api/v1/research/companies/{instrument_id}/summary", response_model=ResearchSummary)
async def summary(instrument_id: UUID, x_correlation_id: str | None = Header(default=None), x_aip_user_id: str | None = Header(default=None), x_aip_user_issuer: str | None = Header(default=None), x_aip_user_subject: str | None = Header(default=None), x_aip_user_email: str | None = Header(default=None), x_aip_user_display_name: str | None = Header(default=None), x_aip_user_roles: str | None = Header(default=None)) -> ResearchSummary:
    identity_headers = _identity_headers(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_email, x_aip_user_display_name, x_aip_user_roles)
    if await _is_restored_etf_instrument(instrument_id, x_correlation_id, identity_headers):
        raise HTTPException(status_code=404, detail="ETF research summary is available through portfolio research")
    await _require_profile_or_restored_instrument(instrument_id, x_correlation_id, identity_headers)
    return _normalized_summary_response(repository.summary(instrument_id, allow_demo=True))


@app.get(
    "/api/v1/research/companies/{instrument_id}/presentation",
    response_model=PortfolioResearchCompany,
)
async def global_company_research_presentation(
    instrument_id: UUID,
    region: str = Query(...),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
) -> PortfolioResearchCompany:
    """Read a transient, non-held company row from durable global research."""
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    normalized_region = str(region).strip().upper()
    if normalized_region not in {"USA", "EUROPE", "INDIA"}:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_REGION")
    identity_headers = _identity_headers(
        x_aip_user_id, x_aip_user_issuer, x_aip_user_subject,
        x_aip_user_email, x_aip_user_display_name, x_aip_user_roles,
    )
    try:
        metadata = await portfolio_orchestrator.global_instrument_metadata(
            instrument_id,
            correlation_id=x_correlation_id,
            identity_headers=identity_headers,
        )
        canonical_identity = {
            "country": metadata.get("country"),
            "exchange": metadata.get("primaryExchange") or metadata.get("exchange"),
            "mic": metadata.get("primaryMic") or metadata.get("mic"),
        }
        if not belongs_to_region(canonical_identity, normalized_region):
            raise HTTPException(status_code=409, detail="INSTRUMENT_REGION_MISMATCH")
        company = await portfolio_orchestrator.read_global_company_state(
            instrument_id,
            metadata=metadata,
            correlation_id=x_correlation_id,
            identity_headers=identity_headers,
        )
        company = await portfolio_orchestrator.enrich_global_company_durables(company)
        return company.model_copy(update={
            "evidence_coverage": _canonical_casefolded_text(company.evidence_coverage),
        })
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(
            status_code=502,
            detail="Portfolio service unavailable for global research presentation",
        ) from exc


def _normalized_summary_response(summary: ResearchSummary) -> ResearchSummary:
    """Normalize legacy/display category aliases at the HTTP contract edge."""
    score = canonical_read_model_score(summary.catalyst_score)
    buckets = _canonical_casefolded_values(score.buckets)
    evidence = _canonical_casefolded_evidence(score.category_evidence)
    if buckets == summary.catalyst_score.buckets and evidence == summary.catalyst_score.category_evidence:
        return summary
    return summary.model_copy(update={
        "catalyst_score": score.model_copy(update={"buckets": buckets, "category_evidence": evidence}),
    })


def _canonical_casefolded_values(values: dict[str, int | None]) -> dict[str, int | None]:
    groups: dict[str, list[str]] = {}
    for key in values:
        groups.setdefault(key.casefold(), []).append(key)
    normalized: dict[str, int | None] = {}
    for keys in groups.values():
        canonical = _canonical_category_key(keys)
        normalized[canonical] = values[canonical]
        if normalized[canonical] is None:
            normalized[canonical] = next((values[key] for key in keys if values[key] is not None), None)
    return normalized


def _canonical_casefolded_evidence(values: dict[str, CategoryEvidence]) -> dict[str, CategoryEvidence]:
    groups: dict[str, list[str]] = {}
    for key in values:
        groups.setdefault(key.casefold(), []).append(key)
    normalized: dict[str, CategoryEvidence] = {}
    for keys in groups.values():
        canonical = _canonical_category_key(keys)
        selected = values[canonical]
        if selected.score is None:
            selected = next((values[key] for key in keys if values[key].score is not None), selected)
        normalized[canonical] = selected.model_copy(update={"category": canonical})
    return normalized


def _canonical_casefolded_text(values: dict[str, str]) -> dict[str, str]:
    """Apply the same legacy/canonical key rule to portfolio evidence status."""
    groups: dict[str, list[str]] = {}
    for key in values:
        groups.setdefault(key.casefold(), []).append(key)
    normalized: dict[str, str] = {}
    for keys in groups.values():
        canonical = _canonical_category_key(keys)
        selected = values[canonical]
        if not selected:
            selected = next((values[key] for key in keys if values[key]), selected)
        normalized[canonical] = selected
    return normalized


def _normalized_portfolio_summary_response(summary: PortfolioResearchSummary) -> PortfolioResearchSummary:
    """Normalize only API-facing legacy research-category aliases per company."""
    companies = [
        company.model_copy(update={
            "evidence_coverage": _canonical_casefolded_text(company.evidence_coverage),
        })
        for company in summary.companies
    ]
    if all(company.evidence_coverage == normalized.evidence_coverage
           for company, normalized in zip(summary.companies, companies, strict=True)):
        return summary
    return summary.model_copy(update={"companies": companies})


def _canonical_category_key(keys: list[str]) -> str:
    # Canonical category identifiers are upper-case in the research contract;
    # retain the original key only when a duplicate set has no such identifier.
    return next((key for key in keys if key == key.upper()), keys[0])


@app.get("/api/v1/research/portfolios/{portfolio_id}/summary", response_model=PortfolioResearchSummary)
async def portfolio_research_summary(
    portfolio_id: UUID,
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
    x_aip_user_email: str | None = Header(default=None),
    x_aip_user_display_name: str | None = Header(default=None),
    x_aip_user_roles: str | None = Header(default=None),
):
    identity_headers = {
        "X-AIP-User-Id": x_aip_user_id,
        "X-AIP-User-Issuer": x_aip_user_issuer,
        "X-AIP-User-Subject": x_aip_user_subject,
        "X-AIP-User-Email": x_aip_user_email,
        "X-AIP-User-Display-Name": x_aip_user_display_name,
        "X-AIP-User-Roles": x_aip_user_roles,
    }
    try:
        result = await portfolio_orchestrator.read_portfolio_summary(
            portfolio_id,
            correlation_id=x_correlation_id,
            identity_headers=identity_headers,
        )
        normalization_started = time.perf_counter()
        normalized = _normalized_portfolio_summary_response(result)
        logger.info(
            "portfolio_summary_stage stage=NORMALIZE portfolioId=%s durationMs=%s",
            portfolio_id,
            round((time.perf_counter() - normalization_started) * 1000),
        )
        return normalized
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in {401, 403, 404}:
            raise HTTPException(status_code=status_code, detail="Portfolio research access denied") from exc
        raise HTTPException(status_code=502, detail="Portfolio service unavailable for research summary") from exc


@app.post("/api/v1/research/structured-market/snapshot")
async def structured_market_snapshot(instrument: dict = Body(...)):
    """Internal provider-neutral structured snapshot used by research and manual price refresh."""
    try:
        return await portfolio_orchestrator.structured_quote(instrument)
    except StructuredProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/internal/v1/research/instruments/resolve-provider")
async def resolve_provider_identity(instrument: dict = Body(...), x_aip_service_identity: str | None = Header(default=None)):
    """Identity-only reconciliation. Never collects or persists research data."""
    if x_aip_service_identity != "portfolio-service":
        raise HTTPException(status_code=403, detail="INTERNAL_SERVICE_REQUIRED")
    if (instrument.get("structuredNseCandidateSource") != "VERIFIED_NSE"
            or not instrument.get("isin") or instrument.get("assetType") != "EQUITY"):
        raise HTTPException(status_code=422, detail="VERIFIED_NSE_IDENTITY_REQUIRED")
    try:
        resolution = await portfolio_orchestrator.structured_provider.resolve_instrument(instrument)
        return resolution.model_dump(mode="json")
    except StructuredProviderError as exc:
        raise HTTPException(status_code=503 if "UNAVAILABLE" in str(exc) else 422, detail=str(exc)) from exc


def _require_profile(instrument_id: UUID) -> None:
    try:
        repository.profile(instrument_id)
    except StopIteration as exc:
        raise HTTPException(status_code=404, detail="Research profile not found") from exc


async def _require_profile_or_restored_instrument(instrument_id: UUID, correlation_id: str | None = None, identity_headers: dict[str, str | None] | None = None) -> None:
    try:
        repository.profile(instrument_id)
    except StopIteration as exc:
        profile_reason: dict = {}
        if await _resolve_restored_instrument_profile(instrument_id, correlation_id, identity_headers, reason_out=profile_reason):
            return
        not_resolved_reason = profile_reason.get("reason")
        not_resolved_detail = f"COMPANY_NOT_RESOLVED:{not_resolved_reason}" if not_resolved_reason else "COMPANY_NOT_RESOLVED"
        raise HTTPException(status_code=404, detail=not_resolved_detail) from exc


async def _readiness_profile(
    global_instrument_id: UUID,
    correlation_id: str | None,
    identity_headers: dict[str, str | None],
):
    """Resolve canonical public identity without reconciliation or holding writes."""
    try:
        metadata = await portfolio_orchestrator.global_instrument_metadata(
            global_instrument_id,
            correlation_id=correlation_id,
            identity_headers=identity_headers,
        )
        profile_reason: dict = {}
        if not portfolio_orchestrator.register_global_profile_metadata(
            global_instrument_id, metadata, reason_out=profile_reason
        ):
            not_resolved_reason = profile_reason.get("reason")
            not_resolved_detail = f"COMPANY_NOT_RESOLVED:{not_resolved_reason}" if not_resolved_reason else "COMPANY_NOT_RESOLVED"
            raise HTTPException(status_code=404, detail=not_resolved_detail)
        profile = repository.profile(global_instrument_id)
    except (GlobalInstrumentNotFoundError, StopIteration) as exc:
        raise HTTPException(status_code=404, detail="COMPANY_NOT_RESOLVED") from exc
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(
            status_code=502,
            detail="Portfolio service unavailable for global instrument lookup",
        ) from exc
    research_readiness_adapter.remember_canonical_metadata(
        global_instrument_id, metadata
    )
    return profile


async def _resolve_restored_instrument_profile(instrument_id: UUID, correlation_id: str | None = None, identity_headers: dict[str, str | None] | None = None, *, reason_out: dict | None = None) -> bool:
    try:
        return await portfolio_orchestrator.restore_global_profile(
            instrument_id, correlation_id=correlation_id, identity_headers=identity_headers, reason_out=reason_out
        )
    except GlobalInstrumentNotFoundError:
        return False
    except PortfolioServiceUnavailableError as exc:
        raise HTTPException(status_code=502, detail="Portfolio service unavailable for global instrument lookup") from exc


async def _is_restored_etf_instrument(instrument_id: UUID, correlation_id: str | None = None, identity_headers: dict[str, str | None] | None = None) -> bool:
    try:
        repository.profile(instrument_id)
        return False
    except StopIteration:
        if not await _resolve_restored_instrument_profile(instrument_id, correlation_id, identity_headers):
            return False
        try:
            repository.etf_profile(instrument_id)
            return True
        except StopIteration:
            return False


def _identity_headers(user_id, issuer, subject, email, display_name, roles) -> dict[str, str | None]:
    return {"X-AIP-User-Id": user_id, "X-AIP-User-Issuer": issuer, "X-AIP-User-Subject": subject,
            "X-AIP-User-Email": email, "X-AIP-User-Display-Name": display_name, "X-AIP-User-Roles": roles}


def _require_market_data_admin(
    user_id: str | None,
    issuer: str | None,
    subject: str | None,
    roles: str | None,
) -> None:
    if not (user_id and issuer and subject):
        raise HTTPException(status_code=401, detail="Market data population access denied")
    role_set = {value.strip().upper() for value in str(roles or "").split(",") if value.strip()}
    if "ADMIN" not in role_set:
        raise HTTPException(status_code=403, detail="Market data population admin role required")


def _require_market_data_user(user_id: str | None, issuer: str | None, subject: str | None) -> None:
    if not (user_id and issuer and subject):
        raise HTTPException(status_code=401, detail="Market data ensure access denied")


def _require_research_user(user_id: str | None, issuer: str | None, subject: str | None) -> None:
    if not (user_id and issuer and subject):
        raise HTTPException(status_code=401, detail="Research ensure access denied")


# ---------------------------------------------------------------------------
# Manual Evidence Upload endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v1/research/evidence/file-types")
def supported_evidence_file_types():
    """Return supported file types for manual evidence upload.

    allowedMetricsByEvidenceType is additive, read-only capability discovery
    for evidence types backed by a repeating structured-row schema (the
    FinancialFact family -- see ALLOWED_METRICS_BY_EVIDENCE_TYPE in
    app.manual_evidence) so the frontend's metric picker is driven by the
    same canonical list accept() itself validates against, rather than a
    hand-maintained, driftable duplicate. Omitted entirely for evidence
    types with no such restricted metric set (e.g. SHAREHOLDING,
    CURRENT_NEWS).
    """
    return {
        # Fail-closed: PNG/JPG are only listed when the tesseract OCR
        # binary they depend on is actually present in this runtime image
        # (see app.manual_evidence.runtime_supported_file_type_labels).
        # SUPPORTED_FILE_TYPE_LABELS (the full source-level contract) is
        # intentionally no longer returned here.
        "supportedFileTypes": runtime_supported_file_type_labels(),
        "ocrCapability": {
            "imageOcrAvailable": image_ocr_available(),
            "scannedPdfOcrAvailable": scanned_pdf_ocr_available(),
            "docxExtractionAvailable": docx_extraction_available(),
        },
        "supportedEvidenceTypes": [e.value for e in SUPPORTED_EVIDENCE_TYPES],
        "allowedMetricsByEvidenceType": {
            evidence_type.value: sorted(metrics)
            for evidence_type, metrics in ALLOWED_METRICS_BY_EVIDENCE_TYPE.items()
            if evidence_type in SUPPORTED_EVIDENCE_TYPES
        },
        # Additive capability discovery for the ResearchEvent-backed,
        # restricted-event-type evidence types (ORDER_BOOK_CAPEX_GUIDANCE,
        # GOVERNANCE_HISTORY): the canonical event types accept() will
        # actually validate against (ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE in
        # app.manual_evidence), so the frontend's event-type dropdown is
        # driven by the server's own list rather than a hand-maintained,
        # driftable duplicate. Omitted for evidence types with no such
        # restriction (SHAREHOLDING, CURRENT_NEWS, the FinancialFact family).
        "allowedEventTypesByEvidenceType": {
            evidence_type.value: sorted(t.value for t in event_types)
            for evidence_type, event_types in ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE.items()
            if evidence_type in SUPPORTED_EVIDENCE_TYPES
        },
        # The full canonical EventImpact / TimeHorizon value sets -- sourced
        # from the server's own enums (app.models) rather than a frontend
        # duplicate, for the same drift-free reason as above. These two
        # fields apply to every ResearchEvent-backed evidence type.
        "eventImpactValues": [v.value for v in EventImpact],
        "timeHorizonValues": [v.value for v in TimeHorizon],
    }


@app.post("/api/v1/research/evidence/draft")
async def create_evidence_draft(
    global_instrument_id: UUID,
    evidence_type: str = Query(..., description="Evidence type, e.g. SHAREHOLDING"),
    file: UploadFile = File(...),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
):
    """Upload an evidence file and produce a DRAFT extraction proposal.

    Extraction creates a DRAFT/proposal only.  Extracted structured facts
    MUST NOT directly modify normalized investment data.  Only an explicit
    Accept (separate endpoint) may persist validated normalized facts.
    """
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    started = time.perf_counter()
    file_bytes = await file.read()
    try:
        draft = manual_evidence_ingestor.ingest(
            evidence_type=evidence_type,
            file_bytes=file_bytes,
            filename=file.filename or "uploaded",
            content_type=file.content_type,
            instrument_id=global_instrument_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Safety net (runtime defect closure): an unexpected exception
        # anywhere in extraction/interpretation (e.g. a deterministic
        # parser choking on a genuinely noisy real-world OCR result that
        # none of our synthetic test fixtures reproduced) must never reach
        # the browser as an opaque, undiagnosable 500 -- it is logged here
        # with the full traceback and correlation id so the NEXT occurrence
        # is immediately diagnosable, and the client gets a safe, non-leaky
        # message rather than an internal stack trace.
        logger.exception(
            "research_flow operation=EVIDENCE_DRAFT globalInstrumentId=%s "
            "evidenceType=%s outcome=FAILURE correlationId=%s durationMs=%s",
            global_instrument_id, evidence_type, x_correlation_id or "NONE",
            round((time.perf_counter() - started) * 1000),
        )
        raise HTTPException(
            status_code=500,
            detail="EVIDENCE_DRAFT_FAILED: an unexpected error occurred while processing "
                   "this file. The issue has been logged for investigation.",
        ) from exc
    logger.info(
        "research_flow operation=EVIDENCE_DRAFT globalInstrumentId=%s outcome=SUCCESS "
        "durationMs=%s draftId=%s",
        global_instrument_id, round((time.perf_counter() - started) * 1000),
        draft.draft_id,
    )
    return _draft_response(draft)


@app.get("/api/v1/research/evidence/draft/{draft_id}")
async def get_evidence_draft(
    draft_id: UUID,
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
):
    """Retrieve a DRAFT by ID for review."""
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    draft = manual_evidence_ingestor.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"DRAFT_NOT_FOUND: {draft_id}")
    return _draft_response(draft)


class EvidenceAcceptRequest(BaseModel):
    corrections: dict[str, Any] | None = Field(default=None)
    reconcileWithConflicts: bool = Field(default=False)


@app.post("/api/v1/research/evidence/draft/{draft_id}/accept")
async def accept_evidence_draft(
    draft_id: UUID,
    payload: EvidenceAcceptRequest | None = Body(default=None),
    x_correlation_id: str | None = Header(default=None),
    x_aip_user_id: str | None = Header(default=None),
    x_aip_user_issuer: str | None = Header(default=None),
    x_aip_user_subject: str | None = Header(default=None),
):
    """Explicitly accept a reviewed DRAFT, persisting validated normalized facts.

    This is the ONLY endpoint that may persist normalized facts from manual
    evidence.  It performs:
      1. Re-validation of the draft (including user corrections).
      2. Conflict detection against existing trusted official evidence.
      3. Atomic acceptance: persists the snapshot through the existing
         repository/persistence contract and signals evidence commitment.
      4. Readiness recalculation through the normal Research Readiness path
         (NEVER hard-coded READY).
    """
    _require_research_user(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject)
    started = time.perf_counter()
    try:
        result = await manual_evidence_acceptor.accept(
            draft_id=draft_id,
            corrections=payload.corrections if payload else None,
            # Pre-existing bug: this read the snake_case name of a
            # camelCase-only Pydantic field (reconcileWithConflicts), which
            # raises AttributeError on ANY accept call that actually sends a
            # request body -- i.e. every real HTTP call, SHAREHOLDING
            # included. Fixed to the field's real name; no request/response
            # shape changed (the wire field was always reconcileWithConflicts).
            reconcile_with_conflicts=payload.reconcileWithConflicts if payload else False,
        )
    except AcceptanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        # Same safety net as the draft endpoint above: never let an
        # unexpected exception during accept/persist/readiness-recompute
        # reach the browser as an undiagnosable 500.
        logger.exception(
            "research_flow operation=EVIDENCE_ACCEPT draftId=%s outcome=FAILURE "
            "correlationId=%s durationMs=%s",
            draft_id, x_correlation_id or "NONE", round((time.perf_counter() - started) * 1000),
        )
        raise HTTPException(
            status_code=500,
            detail="EVIDENCE_ACCEPT_FAILED: an unexpected error occurred while accepting "
                   "this evidence. The issue has been logged for investigation.",
        ) from exc
    logger.info(
        "research_flow operation=EVIDENCE_ACCEPT draftId=%s outcome=SUCCESS "
        "durationMs=%s snapshotId=%s",
        draft_id, round((time.perf_counter() - started) * 1000),
        result.get("snapshotId"),
    )
    return result


def _draft_response(draft):
    """Serialize a ManualEvidenceDraft to a JSON-friendly dict."""
    return {
        "draftId": str(draft.draft_id),
        "evidenceType": draft.evidence_type.value,
        "contentHash": draft.content_hash,
        "originalFilename": draft.original_filename,
        "contentType": draft.content_type,
        "instrumentId": str(draft.instrument_id) if draft.instrument_id else None,
        "reportingPeriod": draft.reporting_period.isoformat() if draft.reporting_period else None,
        # Informational only -- see ManualEvidenceDraft.extracted_text.
        # Never a proposed fact, never auto-applied to any field.
        "extractedText": draft.extracted_text,
        "proposedFacts": [
            {
                "field": f.field,
                "value": str(f.value),
                "sourceLocator": f.source_locator,
                "evidenceText": f.evidence_text,
                "rawSourceLabel": f.raw_source_label,
                "metricBasis": f.metric_basis,
                "confidence": f.confidence,
                "validationError": f.validation_error,
                # TASK E2 item 3/8 -- where this field's CURRENT value
                # came from ("OCR_EXTRACTED"/"USER_CORRECTED"/
                # "USER_ENTERED") and, when it differs, the interpreter's
                # own originally extracted value -- so Review can show
                # the original OCR/source value beside an editable,
                # corrected one without losing it. Both are purely
                # informational: neither is itself ever submitted as a
                # correction or treated as accepted evidence.
                "provenance": f.provenance,
                "originalValue": str(f.original_value) if f.original_value is not None else None,
                # TASK F -- VALUATION_INPUTS auto-extracted facts carry
                # enough shape to prefill a FinancialFact editor row
                # (metric/value/periodEnd/periodType/unit); None for
                # every other evidence type's proposed facts.
                "periodEnd": f.period_end,
                "periodType": f.period_type,
                "unit": f.unit,
            }
            for f in draft.proposed_facts
        ],
        "validationResults": {
            "valid": draft.validation_results.valid,
            "errors": draft.validation_results.errors,
            "warnings": draft.validation_results.warnings,
            "conflicts": draft.validation_results.conflicts,
        },
        "extractionMethod": draft.extraction_method,
        "createdAt": draft.created_at.isoformat(),
        "status": draft.status.value,
        # CURRENT_NEWS (and any future type with no safe auto-extraction):
        # the frontend must collect these fields from the user before
        # accept() -- see app.manual_evidence.MANUAL_FIELD_SCHEMA.
        "requiresManualFields": draft.requires_manual_fields,
        "manualFieldSchema": list(draft.manual_field_schema),
        # Conservative, NON-BINDING pre-fill suggestions (see
        # app.evidence_interpretation.interpret_current_news) -- never a
        # proposed fact, never auto-applied; the user still explicitly
        # supplies and submits every manualFieldSchema field as
        # `corrections` at accept() time. None when no evidence
        # interpreter produced suggestions for this draft.
        "fieldSuggestions": draft.field_suggestions,
        # TASK E item 3/4 -- per-category partial-extraction
        # classification ("EXTRACTED"/"MISSING"/"AMBIGUOUS"/"INVALID")
        # for SHAREHOLDING's four primary categories, so the Review form
        # can prefill what was extracted and prompt only for what is
        # missing/ambiguous/invalid, instead of asking the user to
        # re-enter correctly extracted information. None for every other
        # evidence type (see ManualEvidenceDraft.field_classification).
        "fieldClassification": draft.field_classification,
    }
