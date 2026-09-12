from uuid import UUID, uuid4
import logging
import time

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, status
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
from app.repository import ResearchRepository
from app.scoring import canonical_read_model_score
from app.sector_leaderboard import build_sector_leaderboard
from app.sector_performance import belongs_to_region, performance_window, rank_performers
from app.market_universe import IndiaMarketUniverseProvider, MarketUniverseUnavailable
from app.market_data_population import IndiaMarketDataPopulationJobs
from app.market_data_ensure import IndiaMarketDataEnsureService
from app.market_universe_sectors import canonical_sector_counts
from app.watchlists import AddWatchlistInstrumentRequest, EnsureDefaultWatchlistRequest, watchlist_research_projection
from app.scheduler import default_schedule_rules
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
    repository, portfolio_orchestrator, market_data_population_jobs
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
app = FastAPI(title="Research Engine", version="0.3.0")
logger = logging.getLogger(__name__)


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


@app.get("/providers/llm")
def llm_provider() -> dict[str, str]:
    return {"provider": settings.llm_provider, "mode": "optional-not-called"}


@app.get("/api/v1/research/sources")
def sources():
    return default_source_providers()


@app.get("/api/v1/research/schedule")
def schedule():
    return default_schedule_rules()


@app.get("/api/v1/research/companies")
def companies():
    return repository.list_profiles()


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
        if await _resolve_restored_instrument_profile(instrument_id, x_correlation_id, _identity_headers(x_aip_user_id, x_aip_user_issuer, x_aip_user_subject, x_aip_user_email, x_aip_user_display_name, x_aip_user_roles)):
            return repository.profile(instrument_id)
        raise HTTPException(status_code=404, detail="COMPANY_NOT_RESOLVED") from exc


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


def _require_profile(instrument_id: UUID) -> None:
    try:
        repository.profile(instrument_id)
    except StopIteration as exc:
        raise HTTPException(status_code=404, detail="Research profile not found") from exc


async def _require_profile_or_restored_instrument(instrument_id: UUID, correlation_id: str | None = None, identity_headers: dict[str, str | None] | None = None) -> None:
    try:
        repository.profile(instrument_id)
    except StopIteration as exc:
        if await _resolve_restored_instrument_profile(instrument_id, correlation_id, identity_headers):
            return
        raise HTTPException(status_code=404, detail="COMPANY_NOT_RESOLVED") from exc


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
        if not portfolio_orchestrator.register_global_profile_metadata(
            global_instrument_id, metadata
        ):
            raise HTTPException(status_code=404, detail="COMPANY_NOT_RESOLVED")
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


async def _resolve_restored_instrument_profile(instrument_id: UUID, correlation_id: str | None = None, identity_headers: dict[str, str | None] | None = None) -> bool:
    try:
        return await portfolio_orchestrator.restore_global_profile(
            instrument_id, correlation_id=correlation_id, identity_headers=identity_headers
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
