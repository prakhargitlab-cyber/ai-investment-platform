from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import cycle_timing
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import (
    CompanyResearchProfile,
    DocumentStatus,
    EventImpact,
    MarketPriceObservation,
    ProvenancedValue,
    ReliabilityLevel,
    ResearchEvent,
    ResearchEventType,
    ResearchLifecycleStatus,
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceClassification,
    SourceMode,
    SourceType,
    StructuredInstrumentResolution,
    StructuredMarketSnapshot,
    StructuredMarketSnapshotRecord,
    TimeHorizon,
)
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository, _TRUSTED_NSE_PROFILE_IDENTITY
from app.research_readiness import (
    DurableResearchSnapshot,
    ProviderAuthorityRegistry,
    ResearchEvidence,
    ResearchReadinessService,
    ResearchRefreshTarget,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
    ResearchSourceTier,
)
from app.research_readiness_runtime import (
    CapabilityExecutionResult,
    ExistingResearchCapabilityExecutor,
    RepositoryResearchReadinessAdapter,
    ResearchReadinessRuntime,
    jurisdiction_for_profile,
    readiness_response,
)
from app.settings import Settings


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SOURCE_URL = "https://nsearchives.nseindia.com/corporate/quarterly-result.pdf"


def _profile(instrument_id: UUID = INSTRUMENT_ID) -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=instrument_id,
        company_id=uuid4(),
        company_name="Readiness India Limited",
        ticker="READY",
        exchange="NSE",
        mic="XNSE",
        country="IN",
        currency="INR",
        isin="INE000A01010",
        provider_instrument_ids={"NSE": "READY", "YAHOO_FINANCE": "READY.NS"},
    )


def test_global_metadata_registration_keeps_canonical_id_when_demo_isin_matches() -> None:
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=False, research_demo_enabled=True)
    )
    orchestrator = PortfolioResearchOrchestrator(repository, repository.settings)
    canonical_id = UUID("6c6e3c9f-9d08-421b-a3ce-72589f57e23a")

    registered = orchestrator.register_global_profile_metadata(
        canonical_id,
        {
            "globalInstrumentId": str(canonical_id),
            "canonicalName": "Reliance Industries Ltd.",
            "isin": "INE002A01018",
            "assetType": "EQUITY",
            "currency": "INR",
            "country": "IN",
            "primaryExchange": "NSE",
            "primarySymbol": "RELIANCE",
            "status": "ACTIVE",
            "providerMappings": [
                {
                    "provider": "NSE",
                    "providerSymbol": "RELIANCE",
                    "providerInstrumentId": "INE002A01018",
                    "exchange": "NSE",
                    "currency": "INR",
                    "status": "VERIFIED",
                }
            ],
        },
    )

    assert registered is True
    assert repository.profile(canonical_id).instrument_id == canonical_id
    assert repository.profile(canonical_id).provider_instrument_ids["NSE"] == "RELIANCE"


def _fact(
    profile: CompanyResearchProfile,
    metric: str,
    value: str,
    period_end: str,
    period_type: str,
) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(
            profile.instrument_id, metric, period_end, period_type, "CONSOLIDATED"
        ),
        ProvenancedValue(
            value=Decimal(value),
            unit="INR crore",
            as_of_date=datetime.fromisoformat(period_end).replace(tzinfo=timezone.utc),
            source_url=SOURCE_URL,
            source_name="NSE",
            source_type="EXCHANGE_ANNOUNCEMENT",
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW - timedelta(hours=1),
            confidence=0.98,
        ),
        FactSourceTier.OFFICIAL_NSE,
        "NSE",
        f"nse-{metric}-{period_end}-{period_type}",
        SourceMode.REAL,
    )


def _structured_record(profile: CompanyResearchProfile) -> StructuredMarketSnapshotRecord:
    fact_values = {
        "latestPrice": "250",
        "marketCap": "100000",
        "trailingEps": "12",
        "trailingPE": "20",
        "priceToBook": "3",
        "evToEbitda": "11",
        "freeCashFlow": "450",
        "roe": "18",
        "profitMargin": "14",
        "operatingCashFlow": "700",
        "revenueGrowth": "12",
        "earningsGrowth": "9",
        "totalDebt": "1000",
        "bookValue": "80",
        "totalCash": "500",
        "currentRatio": "1.5",
    }
    facts = {
        key: ProvenancedValue(
            value=Decimal(value),
            source_url="https://finance.yahoo.com/quote/READY.NS",
            source_name="Yahoo Finance",
            source_type="STRUCTURED_MARKET_PROVIDER",
            as_of_date=NOW - timedelta(minutes=5),
            retrieved_at=NOW - timedelta(minutes=4),
            confidence=0.9,
        )
        for key, value in fact_values.items()
    }
    facts["sector"] = ProvenancedValue(
        value="Industrials",
        source_url="https://finance.yahoo.com/quote/READY.NS",
        source_name="Yahoo Finance",
        retrieved_at=NOW - timedelta(hours=1),
    )
    resolution = StructuredInstrumentResolution(
        instrument_id=profile.instrument_id,
        provider="YAHOO_FINANCE",
        provider_ticker="READY.NS",
        company_name=profile.company_name,
        exchange="NSE",
        currency="INR",
        confidence=0.99,
        resolved_at=NOW - timedelta(hours=1),
    )
    snapshot = StructuredMarketSnapshot(
        resolution=resolution,
        status="SUCCESS",
        retrieved_at=NOW - timedelta(minutes=4),
        market_as_of=NOW - timedelta(minutes=5),
        source_url="https://finance.yahoo.com/quote/READY.NS",
        facts=facts,
    )
    return StructuredMarketSnapshotRecord(
        instrument_id=profile.instrument_id,
        provider="YAHOO_FINANCE",
        provider_instrument_id="READY.NS",
        exchange="NSE",
        currency="INR",
        source_url=snapshot.source_url,
        retrieved_at=snapshot.retrieved_at,
        persisted_at=snapshot.retrieved_at,
        last_price_at=snapshot.retrieved_at,
        last_valuation_at=snapshot.retrieved_at,
        last_fundamentals_at=snapshot.retrieved_at,
        last_success_at=snapshot.retrieved_at,
        snapshot=snapshot,
    )


def _event(
    profile: CompanyResearchProfile,
    event_type: ResearchEventType,
    *,
    age_days: int,
    title: str,
    impact: EventImpact = EventImpact.POSITIVE,
    source_url: str | None = None,
) -> ResearchEvent:
    event_at = NOW - timedelta(days=age_days)
    return ResearchEvent(
        instrument_id=profile.instrument_id,
        company_id=profile.company_id,
        event_type=event_type,
        event_date=event_at,
        detected_at=event_at,
        title=title,
        summary=title,
        source_document_id=uuid4(),
        source_url=source_url or f"https://news.example/{title.replace(' ', '-').lower()}",
        source_type=SourceType.NEWS,
        source_classification=SourceClassification.REPUTABLE_NEWS,
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
        confidence=0.85,
        impact=impact,
        time_horizon=TimeHorizon.SHORT_TERM,
        status=ResearchLifecycleStatus.VALIDATED,
        raw_evidence_reference=title,
        published_at=event_at,
        retrieved_at=event_at,
    )


def _shareholding(profile: CompanyResearchProfile) -> ShareholdingSnapshot:
    return ShareholdingSnapshot(
        instrument_id=profile.instrument_id,
        period_end=NOW - timedelta(days=45),
        source_provider="NSE",
        source_type="NSE_SHAREHOLDING_XBRL",
        source_identity_key="NSE_SHAREHOLDING:READY:2026Q2",
        source_url="https://nsearchives.nseindia.com/shareholding/ready.xml",
        published_at=NOW - timedelta(days=35),
        retrieved_at=NOW - timedelta(days=34),
        confidence=Decimal("0.99"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=[
            ShareholdingSnapshotValue(
                category=ShareholdingCategory.PROMOTER, percentage=Decimal("51.2")
            ),
            ShareholdingSnapshotValue(
                category=ShareholdingCategory.FII_FPI, percentage=Decimal("12.4")
            ),
        ],
    )


class DurableRepositoryFixture:
    def __init__(self, profile: CompanyResearchProfile, *, complete: bool = True) -> None:
        self.profiles = {profile.instrument_id: profile}
        self.provider_calls = 0
        self.portfolio_mutations = 0
        self.watchlist_mutations = 0
        self.facts = []
        self.structured = []
        self.observations = []
        self.documents = []
        self.events = []
        self.shareholding = []
        if complete:
            self.facts = [
                _fact(profile, "revenue", "100", "2026-08-31", "QUARTERLY"),
                _fact(profile, "pat", "12", "2026-08-31", "QUARTERLY"),
                _fact(profile, "eps", "5", "2026-08-31", "QUARTERLY"),
                _fact(profile, "revenue", "90", "2026-05-31", "QUARTERLY"),
                _fact(profile, "pat", "10", "2026-05-31", "QUARTERLY"),
                _fact(profile, "revenue", "360", "2026-03-31", "ANNUAL"),
                _fact(profile, "pat", "40", "2026-03-31", "ANNUAL"),
                _fact(profile, "revenue", "320", "2025-03-31", "ANNUAL"),
                _fact(profile, "pat", "35", "2025-03-31", "ANNUAL"),
                _fact(profile, "total_debt", "1000", "2026-03-31", "ANNUAL"),
                _fact(profile, "total_equity", "2500", "2026-03-31", "ANNUAL"),
                _fact(profile, "cash_and_cash_equivalents", "500", "2026-03-31", "ANNUAL"),
            ]
            self.structured = [_structured_record(profile)]
            self.observations = [
                MarketPriceObservation(
                    instrument_id=profile.instrument_id,
                    observed_at=NOW - timedelta(days=149 - index, hours=1),
                    price=Decimal(100 + index),
                    currency="INR",
                    provider="YAHOO_FINANCE",
                    source_url="https://finance.yahoo.com/quote/READY.NS/history",
                    retrieved_at=NOW - timedelta(minutes=3),
                )
                for index in range(150)
            ]
            self.events = [
                _event(profile, ResearchEventType.NEW_ORDER, age_days=2, title="Major order"),
                _event(
                    profile,
                    ResearchEventType.REGULATORY_EVENT,
                    age_days=3,
                    title="Regulatory review",
                    impact=EventImpact.NEGATIVE,
                ),
            ]
            self.shareholding = [_shareholding(profile)]

    def profile(self, instrument_id):
        return self.profiles[instrument_id]

    def financial_facts_for(self, instrument_id):
        return [value for value in self.facts if value.key.instrument_id == instrument_id]

    def structured_market_snapshots_for(self, instrument_ids):
        return {value: [item for item in self.structured if item.instrument_id == value] for value in instrument_ids}

    def market_price_observations_for(self, instrument_ids):
        return {value: [item for item in self.observations if item.instrument_id == value] for value in instrument_ids}

    def documents_for(self, instrument_id, source_mode=None):
        return [item for item in self.documents if item.instrument_id == instrument_id]

    def events_for(self, instrument_id, source_mode=None):
        return [item for item in self.events if item.instrument_id == instrument_id]

    def shareholding_for(self, instrument_id, limit=4):
        return [item for item in self.shareholding if item.instrument_id == instrument_id][:limit]

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


def test_durable_adapter_maps_all_rule_areas_without_portfolio_context() -> None:
    profile = _profile()
    repository = DurableRepositoryFixture(profile)
    adapter = RepositoryResearchReadinessAdapter(repository)
    adapter.remember_canonical_metadata(
        profile.instrument_id,
        {
            "globalInstrumentId": str(profile.instrument_id),
            "canonicalSector": "Industrials",
            "updatedAt": NOW.isoformat(),
            "quantity": 500,
            "portfolioId": str(uuid4()),
        },
    )

    result = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )

    assert {item.rule_engine_area for item in result.requirements} == {
        item.rule_engine_area for item in ResearchRequirementRegistry.default().requirements
    }
    assert result.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    assert result.for_requirement("QUARTERLY_FINANCIALS").source_url == SOURCE_URL
    assert result.for_requirement("HISTORICAL_PRICE_SERIES").coverage_pct == 100
    assert result.for_requirement("SHAREHOLDING").status == ResearchRequirementStatus.READY_FRESH
    assert result.for_requirement("SECTOR_MACRO").status == ResearchRequirementStatus.READY_FRESH
    assert repository.provider_calls == 0
    assert repository.portfolio_mutations == 0
    assert repository.watchlist_mutations == 0


def test_held_metadata_fields_do_not_change_public_readiness() -> None:
    first, second = _profile(uuid4()), _profile(uuid4())
    repo = DurableRepositoryFixture(first, complete=False)
    repo.profiles[second.instrument_id] = second
    adapter = RepositoryResearchReadinessAdapter(repo)
    base = {"canonicalSector": "Industrials", "updatedAt": NOW.isoformat()}
    adapter.remember_canonical_metadata(first.instrument_id, {**base, "quantity": 20, "portfolioId": str(uuid4())})
    adapter.remember_canonical_metadata(second.instrument_id, {**base, "quantity": 0, "held": False})
    service = ResearchReadinessService(adapter)

    held = service.assess(first.instrument_id, jurisdiction="INDIA", now=NOW)
    non_held = service.assess(second.instrument_id, jurisdiction="INDIA", now=NOW)

    assert [(item.requirement_id, item.status) for item in held.requirements] == [
        (item.requirement_id, item.status) for item in non_held.requirements
    ]
    assert repo.portfolio_mutations == repo.watchlist_mutations == 0


def test_news_boundary_deduplication_relevance_and_governance_history() -> None:
    profile = _profile()
    other = _profile(uuid4())
    repo = DurableRepositoryFixture(profile, complete=False)
    boundary = _event(
        profile,
        ResearchEventType.NEW_ORDER,
        age_days=30,
        title="Boundary event",
        source_url="https://news.example/boundary",
    )
    duplicate = boundary.model_copy(update={"event_id": uuid4(), "source_document_id": uuid4()})
    old_news = _event(profile, ResearchEventType.NEW_ORDER, age_days=31, title="Old event")
    old_governance = _event(
        profile,
        ResearchEventType.REGULATORY_EVENT,
        age_days=800,
        title="Unresolved fraud investigation",
        impact=EventImpact.NEGATIVE,
    )
    irrelevant = _event(other, ResearchEventType.REGULATORY_EVENT, age_days=1, title="Unrelated war exposure")
    repo.events = [boundary, duplicate, old_news, old_governance, irrelevant]
    adapter = RepositoryResearchReadinessAdapter(repo)
    snapshot = adapter.load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    result = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )

    current = result.for_requirement("CURRENT_NEWS")
    assert current.status == ResearchRequirementStatus.READY_STALE  # Still eligible at 30 days; daily acquisition is stale.
    assert len(current.evidence_ids) == 1
    assert "event:" + str(old_news.event_id) not in "|".join(current.evidence_ids)
    assert all(str(irrelevant.event_id) not in item.evidence_id for item in snapshot.evidence_for("CURRENT_NEWS"))
    governance = snapshot.evidence_for("GOVERNANCE_HISTORY")
    assert any(str(old_governance.event_id) in item.evidence_id and item.unresolved for item in governance)
    assert result.for_requirement("GOVERNANCE_HISTORY").status == ResearchRequirementStatus.READY_FRESH


def _target(requirement_id: str, jurisdiction: str = "INDIA") -> ResearchRefreshTarget:
    requirement = ResearchRequirementRegistry.default().get(requirement_id)
    return ResearchRefreshTarget(
        requirement_id,
        requirement.rule_engine_area,
        ResearchRequirementStatus.MISSING,
        ProviderAuthorityRegistry.default().policy_for(requirement_id, jurisdiction),
        (),
    )


class RecordingTargetRepository:
    def __init__(self, profile: CompanyResearchProfile) -> None:
        self._profile = profile
        self.category_calls: list[set[str]] = []
        self.settings = Settings(market_data_population_initial_lookback_days=400)

    def profile(self, _instrument_id):
        return self._profile

    async def refresh_targeted_categories(self, _instrument_id, categories, **_kwargs):
        self.category_calls.append(set(categories))

    async def market_price_observations_for_instruments(self, ids):
        return {value: [] for value in ids}


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.structured_calls: list[set[str]] = []
        self.international_calls = 0

    async def ensure_structured_market(self, _instrument_id, classes, *, baseline_only=False):
        self.structured_calls.append(set(classes))
        return SimpleNamespace(error=None)

    async def refresh_international_fundamentals(self, *_args, **_kwargs):
        self.international_calls += 1
        return SimpleNamespace(facts=[object()])


class RecordingPopulation:
    def __init__(self) -> None:
        self.calls = []

    async def populate(self, instruments, **kwargs):
        self.calls.append((instruments, kwargs))
        return 1


def _capability_executor():
    repo = RecordingTargetRepository(_profile())
    orchestrator = RecordingOrchestrator()
    jobs = SimpleNamespace(
        population=RecordingPopulation(),
        settings=repo.settings,
    )
    return ExistingResearchCapabilityExecutor(repo, orchestrator, jobs), repo, orchestrator, jobs


@pytest.mark.asyncio
async def test_only_news_missing_runs_only_existing_global_search_path() -> None:
    executor, repo, orchestrator, jobs = _capability_executor()
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("CURRENT_NEWS")],
        jurisdiction="INDIA",
        correlation_id=None,
        identity_headers=None,
    )

    assert result.executed_capabilities == ("GLOBAL_NEWS_SEARCH",)
    assert repo.category_calls == [{"CATALYSTS", "RISKS", "REGULATORY", "MANAGEMENT", "GUIDANCE"}]
    assert orchestrator.structured_calls == []
    assert jobs.population.calls == []


@pytest.mark.asyncio
async def test_only_shareholding_stale_runs_only_nse_shareholding_path() -> None:
    executor, repo, orchestrator, jobs = _capability_executor()
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING")],
        jurisdiction="INDIA",
        correlation_id=None,
        identity_headers=None,
    )

    assert result.executed_capabilities == ("SHAREHOLDING",)
    assert repo.category_calls == [{"SHAREHOLDING_PATTERN"}]
    assert orchestrator.structured_calls == []
    assert jobs.population.calls == []


@pytest.mark.asyncio
async def test_financials_and_news_group_only_their_existing_capabilities() -> None:
    executor, repo, orchestrator, _jobs = _capability_executor()
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("QUARTERLY_FINANCIALS"), _target("CURRENT_NEWS")],
        jurisdiction="INDIA",
        correlation_id="targeted",
        identity_headers=None,
    )

    assert result.executed_capabilities == ("FINANCIALS", "GLOBAL_NEWS_SEARCH")
    assert repo.category_calls == [{
        "FINANCIAL_RESULTS", "CATALYSTS", "RISKS", "REGULATORY", "MANAGEMENT", "GUIDANCE"
    }]
    assert orchestrator.structured_calls == []


class StateDataSource:
    def __init__(self, missing: set[str]) -> None:
        self.missing = set(missing)
        self.loads = 0
        self.failures: dict[str, str] = {}

    def load_by_global_instrument_id(self, instrument_id, requirements):
        self.loads += 1
        now = datetime.now(timezone.utc)
        evidence = {
            item.requirement_id: (
                ResearchEvidence(
                    evidence_id=f"ready:{item.requirement_id}",
                    requirement_id=item.requirement_id,
                    source="NSE",
                    source_tier=ResearchSourceTier.OFFICIAL,
                    retrieved_at=now - timedelta(minutes=1),
                    as_of=now - timedelta(minutes=1),
                    event_date=now - timedelta(minutes=1),
                    published_at=now - timedelta(minutes=1),
                ),
            )
            for item in requirements
            if item.requirement_id not in self.missing
        }
        return DurableResearchSnapshot(
            instrument_id, evidence, failure_reasons=self.failures
        )

    def mark_refreshing(self, _instrument_id, _requirement_ids):
        return None

    def finish_refresh(self, _instrument_id, failures=None):
        self.failures = dict(failures or {})


class RuntimeRepository:
    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


class UpdatingExecutor:
    def __init__(self, source: StateDataSource, *, update_primary: bool = True) -> None:
        self.source = source
        self.update_primary = update_primary
        self.primary_calls: list[set[str]] = []
        self.fallback_calls: list[set[str]] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def execute_primary(self, _instrument_id, targets, **_kwargs):
        ids = {target.requirement_id for target in targets}
        self.primary_calls.append(ids)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.update_primary:
            self.source.missing.difference_update(ids)
        return CapabilityExecutionResult(("RECORDED_PRIMARY",), {})

    async def execute_approved_fallbacks(self, _instrument_id, targets):
        ids = {target.requirement_id for target in targets}
        self.fallback_calls.append(ids)
        self.source.missing.difference_update(ids)
        return CapabilityExecutionResult(("RECORDED_FALLBACK",), {})


def _runtime(source: StateDataSource, executor: UpdatingExecutor) -> ResearchReadinessRuntime:
    return ResearchReadinessRuntime(
        RuntimeRepository(),
        source,  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_all_fresh_ensure_executes_zero_provider_capabilities() -> None:
    source = StateDataSource(set())
    executor = UpdatingExecutor(source)
    result = await _runtime(source, executor).ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=None,
    )

    assert result.planned_requirement_ids == ()
    assert result.executed_capabilities == ()
    assert executor.primary_calls == executor.fallback_calls == []


@pytest.mark.asyncio
async def test_runtime_targets_only_selected_missing_requirement_and_skips_fresh() -> None:
    source = StateDataSource({"CURRENT_NEWS", "SHAREHOLDING"})
    executor = UpdatingExecutor(source)
    result = await _runtime(source, executor).ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=["NEWS_GEOPOLITICAL_EVENTS"],
    )

    assert result.planned_requirement_ids == ("CURRENT_NEWS",)
    assert executor.primary_calls == [{"CURRENT_NEWS"}]
    assert "SHAREHOLDING" in source.missing


@pytest.mark.asyncio
async def test_primary_missing_uses_only_approved_existing_financial_fallback() -> None:
    source = StateDataSource({"QUARTERLY_FINANCIALS"})
    executor = UpdatingExecutor(source, update_primary=False)
    result = await _runtime(source, executor).ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=["QUARTERLY_FINANCIALS"],
    )

    assert executor.primary_calls == [{"QUARTERLY_FINANCIALS"}]
    assert executor.fallback_calls == [{"QUARTERLY_FINANCIALS"}]
    assert result.executed_capabilities == ("RECORDED_PRIMARY", "RECORDED_FALLBACK")
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH


@pytest.mark.asyncio
async def test_concurrent_same_instrument_ensure_reuses_single_flight() -> None:
    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source)
    executor.release = asyncio.Event()
    runtime = _runtime(source, executor)

    first = asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
    ))
    await executor.started.wait()
    second = asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
    ))
    await asyncio.sleep(0)
    executor.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert executor.primary_calls == [{"CURRENT_NEWS"}]
    assert first_result.reused_single_flight is False
    assert second_result.reused_single_flight is True


@pytest.mark.asyncio
async def test_disjoint_requirement_groups_for_same_instrument_run_concurrently() -> None:
    """Area 2 / runtime_ensure bottleneck root cause: ResearchReadinessRuntime
    used to key its single-flight join purely on the instrument, so an
    ensure() call for one requirement group (e.g. a mandatory
    BUSINESS_QUALITY_FACTS-style group) would block ANY concurrently issued
    ensure() for a totally disjoint requirement on the SAME instrument (e.g.
    CURRENT_NEWS, which deep_investigation.investigate deliberately runs
    concurrently with the mandatory loop -- see _acquire_news_in_background)
    until the unrelated flight finished entirely. That is exactly why
    CURRENT_NEWS's own runtime_ensure_elapsed_ms tracked almost the whole
    investigation's wall time in production. The join must now be scoped to
    actual requirement-id overlap: a disjoint call must start its own
    acquisition immediately, without waiting on an unrelated in-flight one."""
    source = StateDataSource({"CURRENT_NEWS", "SHAREHOLDING"})
    executor = UpdatingExecutor(source)
    executor.release = asyncio.Event()
    runtime = _runtime(source, executor)

    first = asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
    ))
    await executor.started.wait()
    assert executor.primary_calls == [{"CURRENT_NEWS"}]

    second = asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["SHAREHOLDING"]
    ))
    # The disjoint second call must reach its own execute_primary and block
    # there (on the SAME shared release event) rather than being stuck
    # waiting on the first call's single-flight task before ever starting.
    for _ in range(50):
        if len(executor.primary_calls) == 2:
            break
        await asyncio.sleep(0)
    assert executor.primary_calls == [{"CURRENT_NEWS"}, {"SHAREHOLDING"}]

    executor.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.planned_requirement_ids == ("CURRENT_NEWS",)
    assert second_result.planned_requirement_ids == ("SHAREHOLDING",)
    # Neither call joined the other's flight -- both ran their own.
    assert first_result.reused_single_flight is False
    assert second_result.reused_single_flight is False
    assert runtime._flights == {}


@pytest.mark.asyncio
async def test_partially_overlapping_requirement_groups_only_wait_on_the_overlap() -> None:
    """A follower requesting a MIX of one requirement already owned by an
    in-flight group and one entirely disjoint requirement must still join
    (and subtract the already-attempted id from) the overlapping flight --
    the overlap-scoped fix must not regress the pre-existing exact-overlap
    join behavior into "never join."""
    source = StateDataSource({"CURRENT_NEWS", "SHAREHOLDING"})
    executor = UpdatingExecutor(source)
    executor.release = asyncio.Event()
    runtime = _runtime(source, executor)

    first = asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
    ))
    await executor.started.wait()
    assert executor.primary_calls == [{"CURRENT_NEWS"}]

    second = asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS", "SHAREHOLDING"],
    ))
    await asyncio.sleep(0)
    # The overlapping portion (CURRENT_NEWS) is genuinely shared: the
    # follower shield-waits on the owner's flight for that id before its own
    # (disjoint) SHAREHOLDING remainder can be dispatched -- releasing here
    # lets the owner's flight (and, in turn, the follower's join) complete.
    executor.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    # The follower's own fresh acquisition only covers SHAREHOLDING -- the
    # CURRENT_NEWS portion was served entirely by joining the first call's
    # flight, exactly as the single-overlap join already did before this fix.
    assert executor.primary_calls == [{"CURRENT_NEWS"}, {"SHAREHOLDING"}]

    assert first_result.reused_single_flight is False
    assert second_result.reused_single_flight is False
    assert set(second_result.planned_requirement_ids) == {"CURRENT_NEWS", "SHAREHOLDING"}
    assert runtime._flights == {}


@pytest.mark.asyncio
async def test_orchestration_wait_expiry_does_not_duplicate_scheduling() -> None:
    """With the timeout-ownership fix, a background-cycle ensure() call whose
    observation budget (ensure_timeout_seconds) elapses no longer blocks
    indefinitely on the underlying task. Instead it cancels the owned task,
    drains it under a hard deadline, and returns a bounded ACQUISITION_TIMEOUT
    failure result.

    A second, overlapping ensure() call for the same instrument arriving
    after the first timed out must start a FRESH acquisition (the first
    flight was cancelled/done and reaped from _flights by the done-callback)
    -- it must NOT join a cancelled/stale flight, and must NOT skip acquiring
    work that the first call never completed. This proves the timeout fix
    returns bounded control to the scheduler instead of pinning workers.
    """
    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source)
    executor.release = asyncio.Event()
    runtime = ResearchReadinessRuntime(
        RuntimeRepository(),
        source,  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        ensure_timeout_seconds=0.01,
    )

    first = asyncio.create_task(runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"],
        wait_for_completion=True,
    ))
    await executor.started.wait()
    # Let the tiny ensure_timeout_seconds elapse so the timeout path fires for
    # the still-in-flight first call (task cancelled + drained) before issuing
    # the second, overlapping ensure() call.
    await asyncio.sleep(0.05)
    # The first call must have hit the timeout path and returned a bounded
    # ACQUISITION_TIMEOUT failure (NOT blocked forever on the task).
    assert first.done(), "first ensure() must return (not block) after timeout"
    first_result = await first
    assert "CURRENT_NEWS" in first_result.failures
    assert "ACQUISITION_TIMEOUT" in first_result.failures["CURRENT_NEWS"]
    assert first_result.reused_single_flight is False
    # The first call's task was cancelled and reaped from _flights by the
    # done-callback, so no live flight remains.
    assert INSTRUMENT_ID not in runtime._flights

    # Now release and issue a second call with a longer timeout -- it must
    # start a fresh acquisition (not join the cancelled flight) and complete
    # successfully.
    executor.release.set()
    runtime.ensure_timeout_seconds = 5.0
    second_result = await runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"],
        wait_for_completion=True,
    )
    assert executor.primary_calls == [{"CURRENT_NEWS"}, {"CURRENT_NEWS"}]
    assert second_result.reused_single_flight is False
    assert second_result.failures == {}
    assert "CURRENT_NEWS" not in source.missing


class BudgetExecutor:
    def __init__(self, source: StateDataSource) -> None:
        self.source = source
        self.primary_calls = 0
        self.fallback_calls = 0
        self.cancelled = False

    async def execute_primary(self, _instrument_id, targets, **_kwargs):
        self.primary_calls += 1
        first = targets[0].requirement_id
        progress = _kwargs.get("progress")
        if progress is not None:
            progress.executed(f"YAHOO_FINANCE_MCP:{first}")
            for target in targets:
                progress.failed(target.requirement_id, "EXTERNAL_RESULT_INCOMPLETE")
        self.source.missing.discard(first)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return CapabilityExecutionResult(("UNREACHABLE",), {})

    async def execute_approved_fallbacks(self, _instrument_id, _targets):
        self.fallback_calls += 1
        return CapabilityExecutionResult()


@pytest.mark.asyncio
async def test_ensure_budget_retains_committed_success_and_reports_unexecuted_targets() -> None:
    source = StateDataSource({"CURRENT_NEWS", "SHAREHOLDING"})
    executor = BudgetExecutor(source)
    runtime = ResearchReadinessRuntime(
        RuntimeRepository(), source, executor, ensure_timeout_seconds=0.02  # type: ignore[arg-type]
    )
    started = asyncio.get_running_loop().time()

    result = await runtime.ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS", "SHAREHOLDING"],
    )

    assert asyncio.get_running_loop().time() - started < 0.5
    assert executor.primary_calls == 1
    assert executor.fallback_calls == 0
    assert executor.cancelled is True
    completed = result.planned_requirement_ids[0]
    deferred = result.planned_requirement_ids[1]
    assert result.readiness.for_requirement(completed).status == ResearchRequirementStatus.READY_FRESH
    assert result.readiness.for_requirement(deferred).status == ResearchRequirementStatus.FAILED
    assert result.executed_capabilities == (f"YAHOO_FINANCE_MCP:{completed}",)
    assert result.failures == {
        deferred: "EXTERNAL_RESULT_INCOMPLETE|ACQUISITION_TIMEOUT"
    }


class EmptyFailureExecutor:
    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.primary_calls = 0
        self.fallback_calls = 0

    async def execute_primary(self, _instrument_id, targets, **_kwargs):
        self.primary_calls += 1
        return CapabilityExecutionResult(
            ("YAHOO_FINANCE_MCP:CURRENT_NEWS",),
            {target.requirement_id: self.reason for target in targets},
        )

    async def execute_approved_fallbacks(self, _instrument_id, _targets):
        self.fallback_calls += 1
        return CapabilityExecutionResult()


@pytest.mark.asyncio
async def test_individual_capability_failure_returns_partial_result_with_reason() -> None:
    source = StateDataSource({"CURRENT_NEWS"})
    executor = EmptyFailureExecutor("EXTERNAL_CAPABILITY_UNSUPPORTED")
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor)  # type: ignore[arg-type]

    result = await runtime.ensure(
        INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
    )

    assert result.failures == {"CURRENT_NEWS": "EXTERNAL_CAPABILITY_UNSUPPORTED"}
    assert result.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.FAILED
    assert executor.primary_calls == executor.fallback_calls == 1


class MetadataOnlyOrchestrator:
    def __init__(self, profile: CompanyResearchProfile) -> None:
        self.profile = profile
        self.calls: list[str] = []
        self.portfolio_mutations = 0
        self.watchlist_mutations = 0

    async def global_instrument_metadata(self, instrument_id, **_kwargs):
        self.calls.append("GET_CANONICAL_IDENTITY")
        return {
            "globalInstrumentId": str(instrument_id),
            "canonicalSector": "Industrials",
            "updatedAt": NOW.isoformat(),
        }

    def register_global_profile_metadata(self, instrument_id, _metadata, *, reason_out=None):
        return instrument_id == self.profile.instrument_id


def test_readiness_get_is_read_only_and_returns_source_freshness(monkeypatch) -> None:
    profile = _profile()
    repository = DurableRepositoryFixture(profile)
    adapter = RepositoryResearchReadinessAdapter(repository)
    executor = UpdatingExecutor(StateDataSource(set()))
    runtime = ResearchReadinessRuntime(repository, adapter, executor)  # type: ignore[arg-type]
    orchestrator = MetadataOnlyOrchestrator(profile)
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(main, "portfolio_orchestrator", orchestrator)
    monkeypatch.setattr(main, "research_readiness_adapter", adapter)
    monkeypatch.setattr(main, "research_readiness_runtime", runtime)

    response = TestClient(main.app).get(
        f"/api/v1/research/readiness/{profile.instrument_id}",
        headers={
            "X-AIP-User-Id": "user",
            "X-AIP-User-Issuer": "gateway",
            "X-AIP-User-Subject": "subject",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["globalInstrumentId"] == str(profile.instrument_id)
    assert body["overallCompletenessPct"] > 0
    quarterly = next(item for item in body["requirements"] if item["requirementId"] == "QUARTERLY_FINANCIALS")
    assert quarterly["sourceProvider"] == "NSE"
    assert quarterly["sourceUrl"] == SOURCE_URL
    assert quarterly["freshnessPolicy"]["mode"] == "RELEASE_AWARE_QUARTERLY"
    assert executor.primary_calls == []
    assert orchestrator.calls == ["GET_CANONICAL_IDENTITY"]
    assert orchestrator.portfolio_mutations == orchestrator.watchlist_mutations == 0


def test_readiness_ensure_api_expands_one_area_and_executes_only_its_missing_requirement(
    monkeypatch,
) -> None:
    profile = _profile()
    repository = DurableRepositoryFixture(profile, complete=False)
    identity_adapter = RepositoryResearchReadinessAdapter(repository)
    source = StateDataSource({"CURRENT_NEWS", "SHAREHOLDING"})
    executor = UpdatingExecutor(source)
    runtime = _runtime(source, executor)
    orchestrator = MetadataOnlyOrchestrator(profile)
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(main, "portfolio_orchestrator", orchestrator)
    monkeypatch.setattr(main, "research_readiness_adapter", identity_adapter)
    monkeypatch.setattr(main, "research_readiness_runtime", runtime)

    response = TestClient(main.app).post(
        f"/api/v1/research/readiness/{profile.instrument_id}/ensure",
        headers={
            "X-AIP-User-Id": "user",
            "X-AIP-User-Issuer": "gateway",
            "X-AIP-User-Subject": "subject",
        },
        json={"requirements": ["NEWS_GEOPOLITICAL_EVENTS"]},
    )

    assert response.status_code == 200
    assert response.json()["refreshState"] == {
        "plannedRequirements": ["CURRENT_NEWS"],
        "executedCapabilities": ["RECORDED_PRIMARY"],
        "reusedSingleFlight": False,
        "failureReasons": {},
    }
    assert executor.primary_calls == [{"CURRENT_NEWS"}]
    assert "SHAREHOLDING" in source.missing
    assert repository.portfolio_mutations == repository.watchlist_mutations == 0


def test_readiness_ensure_api_rejects_unknown_requirement_without_provider_work(
    monkeypatch,
) -> None:
    profile = _profile()
    repository = DurableRepositoryFixture(profile, complete=False)
    identity_adapter = RepositoryResearchReadinessAdapter(repository)
    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source)
    runtime = _runtime(source, executor)
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(main, "portfolio_orchestrator", MetadataOnlyOrchestrator(profile))
    monkeypatch.setattr(main, "research_readiness_adapter", identity_adapter)
    monkeypatch.setattr(main, "research_readiness_runtime", runtime)

    response = TestClient(main.app).post(
        f"/api/v1/research/readiness/{profile.instrument_id}/ensure",
        headers={
            "X-AIP-User-Id": "user",
            "X-AIP-User-Issuer": "gateway",
            "X-AIP-User-Subject": "subject",
        },
        json={"requirements": ["UNREGISTERED_PROVIDER_FACT"]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "UNKNOWN_RESEARCH_REQUIREMENT:UNREGISTERED_PROVIDER_FACT"
    assert executor.primary_calls == executor.fallback_calls == []


def test_readiness_response_is_deterministic_and_has_no_stock_score() -> None:
    source = StateDataSource({"LATEST_PRICE"})
    result = ResearchReadinessService(source).assess(
        INSTRUMENT_ID, jurisdiction="INDIA", now=NOW
    )
    first = readiness_response(result, ResearchRequirementRegistry.default())
    second = readiness_response(result, ResearchRequirementRegistry.default())

    assert first == second
    assert first["criticalCompletenessPct"] < 100
    assert first["overallCompletenessPct"] < 100
    assert "score" not in first
    assert first["confidence"] in {"LOW", "MEDIUM", "HIGH"}


def test_nse_quarterly_pdf_persists_normalized_parser_input_without_pdf_bytes(tmp_path) -> None:
    persistence = SqliteResearchPersistence(tmp_path / "research.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_demo_enabled=False), persistence=persistence
    )
    profile = _profile(uuid4())
    repository.profiles.append(profile)
    text = (
        "Readiness India Limited READY INE000A01010 Statement of Unaudited Financial "
        "Results for the quarter ended 30 June 2026 Amounts in Rs. Crore Particulars "
        "Quarter ended 30.06.2026 31.03.2026 30.06.2025 Revenue from operations "
        "100.00 90.00 80.00 Profit for the period 12.00 10.00 8.00 Basic EPS 5.00 4.00 3.00"
    )

    document = repository.ingest_fixture(
        original_url=SOURCE_URL,
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name="NSE corporate announcements",
        publisher="NSE",
        content_type="application/pdf",
        body=text,
        reliability=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        source_classification=SourceClassification.EXCHANGE,
        discovery_provider="NSE_OFFICIAL_API",
        expected_profile=profile,
        document_status=DocumentStatus.PARSED,
        _trusted_profile_identity=_TRUSTED_NSE_PROFILE_IDENTITY,
    )

    persisted = next(
        item for item in persistence.load_documents() if item.document_id == document.document_id
    )
    facts = repository.financial_facts_for(profile.instrument_id)
    adapter = RepositoryResearchReadinessAdapter(repository)
    readiness = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )

    assert document.normalized_text
    assert persisted.normalized_text == document.normalized_text
    assert persisted.raw_text is None
    assert persisted.canonical_url == SOURCE_URL
    assert any(fact.key.metric == "revenue" and fact.key.period_type == "QUARTERLY" for fact in facts)
    assert any(fact.value.source_url == SOURCE_URL for fact in facts)
    assert readiness.for_requirement("QUARTERLY_FINANCIALS").source_url == SOURCE_URL
    assert list(tmp_path.glob("*.pdf")) == []


def test_nse_financial_result_retains_normalized_input_for_future_parser_reconciliation(tmp_path) -> None:
    persistence = SqliteResearchPersistence(tmp_path / "research.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_demo_enabled=False), persistence=persistence
    )
    profile = _profile(uuid4())
    repository.profiles.append(profile)

    document = repository.ingest_fixture(
        original_url=SOURCE_URL,
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name="NSE corporate announcements",
        publisher="NSE",
        content_type="application/pdf",
        body=(
            "Readiness India Limited READY INE000A01010 official quarterly financial "
            "result attachment whose tabular facts could not be normalized safely."
        ),
        reliability=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        source_classification=SourceClassification.EXCHANGE,
        discovery_provider="NSE_OFFICIAL_API",
        expected_profile=profile,
        document_status=DocumentStatus.PARSED,
        _trusted_profile_identity=_TRUSTED_NSE_PROFILE_IDENTITY,
        _metadata_only_nse_financial_result=True,
    )

    persisted = next(
        item for item in persistence.load_documents() if item.document_id == document.document_id
    )

    assert document.normalized_text
    assert persisted.normalized_text == document.normalized_text
    assert persisted.raw_text is None
    assert persisted.canonical_url == SOURCE_URL
    assert repository.financial_facts_for(profile.instrument_id) == []
    assert list(tmp_path.glob("*.pdf")) == []


@pytest.mark.asyncio
async def test_repository_targeted_boundary_forwards_only_selected_legacy_categories(
    tmp_path,
) -> None:
    persistence = SqliteResearchPersistence(tmp_path / "targeted.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=persistence,
    )
    profile = _profile(uuid4())
    repository.profiles.append(profile)
    calls: list[tuple[set[str], bool]] = []

    async def record_targeted(_instrument_id, _pre_resolved, *, force, requested_categories):
        calls.append((set(requested_categories), force))

    repository._refresh_live = record_targeted  # type: ignore[method-assign]
    await repository.refresh_targeted_categories(
        profile.instrument_id,
        {"CATALYSTS", "RISKS"},
        correlation_id="readiness-test",
        allow_demo=False,
    )

    assert calls == [({"CATALYSTS", "RISKS"}, True)]


@pytest.mark.asyncio
async def test_repository_targeted_refresh_cancels_owned_provider_work_and_records_reason(
    tmp_path,
) -> None:
    persistence = SqliteResearchPersistence(tmp_path / "targeted-cancel.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=persistence,
    )
    profile = _profile(uuid4())
    repository.profiles.append(profile)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block_targeted(_instrument_id, _pre_resolved, *, force, requested_categories):
        assert force is True
        assert requested_categories == {"CATALYSTS"}
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    repository._refresh_live = block_targeted  # type: ignore[method-assign]
    task = asyncio.create_task(
        repository.refresh_targeted_categories(
            profile.instrument_id,
            {"CATALYSTS"},
            correlation_id="readiness-cancel-test",
            allow_demo=False,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert cancelled.is_set()
    assert profile.instrument_id not in repository._instrument_refresh_flights
    row = persistence._connection.execute(
        """
        SELECT status, safe_error_code
        FROM research_refresh_runs
        WHERE correlation_id = ?
        """,
        ("readiness-cancel-test",),
    ).fetchone()
    assert tuple(row) == ("FAILED", "ACQUISITION_CANCELLED")


def test_jurisdiction_mapping_is_provider_neutral() -> None:
    assert jurisdiction_for_profile(_profile()) == "INDIA"
    european = _profile(uuid4()).model_copy(update={"country": "DE", "exchange": "XETR"})
    american = _profile(uuid4()).model_copy(update={"country": "US", "exchange": "XNAS"})
    assert jurisdiction_for_profile(european) == "EUROPE"
    assert jurisdiction_for_profile(american) == "USA"


def test_phase_timing_recorders_are_noops_without_active_recorder() -> None:
    # Without a cycle_scope / span, the phase recorders must be safe no-ops.
    cycle_timing.record_readiness_load_elapsed(1.0)
    cycle_timing.record_planning_elapsed(1.0)
    cycle_timing.record_single_flight_wait_elapsed(1.0)
    cycle_timing.record_observation_recording_elapsed(1.0)
    cycle_timing.record_final_readiness_reload_elapsed(1.0)


@pytest.mark.asyncio
async def test_ensure_emits_phase_timing_under_active_recorder() -> None:
    from app.cycle_timing import (
        CycleTimingRecorder,
        cycle_scope,
        track_requirement,
    )

    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source)
    runtime = _runtime(source, executor)
    recorder = CycleTimingRecorder("phase-timing-cycle")

    with cycle_scope(recorder):
        with track_requirement("rt-1", "CURRENT_NEWS") as span:
            await runtime.ensure(
                INSTRUMENT_ID,
                jurisdiction="INDIA",
                requirement_ids=["CURRENT_NEWS"],
            )

    # Every phase that should have run must have recorded a non-negative ms.
    assert span.readiness_load_elapsed_ms >= 0.0
    assert span.planning_elapsed_ms >= 0.0
    assert span.final_readiness_reload_elapsed_ms >= 0.0
    # No single-flight target and no record_acquisition_observer on RuntimeRepository:
    # both may legitimately be 0.0 (the recorder only fires when the phase runs).
    assert span.single_flight_wait_elapsed_ms == 0.0
    assert span.observation_recording_elapsed_ms == 0.0
    # Cycle-level aggregates must mirror the span values.
    assert recorder._cycle_readiness_load_ms == span.readiness_load_elapsed_ms
    assert recorder._cycle_planning_ms == span.planning_elapsed_ms
    assert recorder._cycle_final_readiness_reload_ms == span.final_readiness_reload_elapsed_ms


@pytest.mark.asyncio
async def test_single_flight_wait_recorded_for_follower() -> None:
    from app.cycle_timing import (
        CycleTimingRecorder,
        cycle_scope,
        track_requirement,
    )

    source = StateDataSource({"CURRENT_NEWS"})

    class SlowExecutor(UpdatingExecutor):
        async def execute_primary(self, instrument_id, targets, **_kwargs):
            self.started.set()
            await asyncio.sleep(0.05)  # blocks the owner's plan briefly so the
            # follower's shielded wait is measurable in wall-clock time.
            return await super().execute_primary(instrument_id, targets, **_kwargs)

    executor = SlowExecutor(source)
    runtime = _runtime(source, executor)
    recorder = CycleTimingRecorder("sf-wait-cycle")

    with cycle_scope(recorder):
        with track_requirement("sf-owner", "CURRENT_NEWS") as owner_span:
            first = asyncio.create_task(runtime.ensure(
                INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
            ))
            await executor.started.wait()
            with track_requirement("sf-follower", "CURRENT_NEWS") as follower_span:
                second = asyncio.create_task(runtime.ensure(
                    INSTRUMENT_ID, jurisdiction="INDIA", requirement_ids=["CURRENT_NEWS"]
                ))
                await asyncio.sleep(0)
            first_result, second_result = await asyncio.gather(first, second)

    assert first_result.reused_single_flight is False
    assert second_result.reused_single_flight is True
    # The owner executed the plan; the follower waited on the shielded flight.
    assert owner_span.single_flight_wait_elapsed_ms == 0.0
    assert follower_span.single_flight_wait_elapsed_ms > 0.0
    # The cycle aggregate captures the follower's wait.
    assert recorder._cycle_single_flight_wait_ms == follower_span.single_flight_wait_elapsed_ms


@pytest.mark.asyncio
async def test_execute_plan_collapses_final_read_when_no_fallback() -> None:
    # No-fallback path: after_primary fully satisfies the target, so the
    # previously-redundant final_readiness read() is elided and reused.
    # 4 reads would have occurred pre-optimization (initial + after_primary +
    # final_readiness + ensure-final). With the collapse: 3 (initial +
    # after_primary/reused + ensure-final).
    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source, update_primary=True)
    runtime = _runtime(source, executor)

    result = await runtime.ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"],
    )

    assert executor.primary_calls == [{"CURRENT_NEWS"}]
    assert executor.fallback_calls == []
    assert result.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_FRESH
    # initial read + after_primary read + ensure-final read == 3 (no collapsed 4th).
    assert source.loads == 3


@pytest.mark.asyncio
async def test_execute_plan_re_reads_final_readiness_when_fallback_runs() -> None:
    # Fallback path: the fallback persists new evidence, so a separate final
    # read() is REQUIRED to capture the persisted change. The optimization
    # must NOT elide this read.
    source = StateDataSource({"QUARTERLY_FINANCIALS"})
    executor = UpdatingExecutor(source, update_primary=False)
    runtime = _runtime(source, executor)

    result = await runtime.ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=["QUARTERLY_FINANCIALS"],
    )

    assert executor.primary_calls == [{"QUARTERLY_FINANCIALS"}]
    assert executor.fallback_calls == [{"QUARTERLY_FINANCIALS"}]
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    # initial + after_primary + final_readiness-after-fallback + ensure-final == 4.
    assert source.loads == 4


# ---------------------------------------------------------------------------
# Context-span propagation for grouped (multi-member) acquisition.
#
# Root cause being guarded: deep_investigation._acquire_group calls ONE shared
# runtime.ensure() on behalf of N requirement members but (pre-fix) did NOT
# bind any RequirementTimingRecord as _current_span while that ensure() ran.
# Every descendant recorder (_record -> span._add) therefore hit span=None and
# silently dropped per-requirement attribution, even though the cycle-level
# recorder (_current_recorder, set once by the outer cycle_scope) kept the
# cycle aggregates non-zero. The fix: bind the FIRST member's record as the
# active span around the shared ensure(), so granular descendant sub-timing
# (provider/network/discovery/PDF/persistence) attributes to a representative
# member exactly as deep_investigation's own comment intends, while group
# totals still flow to EVERY member via record_elapsed_on().
# ---------------------------------------------------------------------------


def test_group_span_bind_routes_descendant_timing_to_first_member() -> None:
    # Reproduces the _acquire_group shape at the cycle_timing layer:
    # start N member records, bind_span the first, record descendant timing
    # (provider_elapsed == the kind of leaf metric emitted inside ensure()),
    # then unbind and write totals to all members.
    from app.cycle_timing import (
        CycleTimingRecorder,
        cycle_scope,
        bind_span,
        unbind_span,
        record_elapsed_on,
    )

    recorder = CycleTimingRecorder("group-span-cycle")
    with cycle_scope(recorder):
        first = recorder.start_requirement(INSTRUMENT_ID, "GROWTH_FACTS")
        second = recorder.start_requirement(INSTRUMENT_ID, "BALANCE_SHEET_FACTS")
        # Pre-fix behaviour: no span bound -> provider timing is dropped from
        # BOTH member records (span is None) but still reaches the cycle total.
        # Post-fix: bind the first member so descendant attribution lands.
        token = bind_span(first)
        try:
            cycle_timing.record_provider_elapsed(1234.5)  # leaf inside ensure()
        finally:
            unbind_span(token)
        # The group's own elapsed totals are written to EVERY member afterwards.
        record_elapsed_on(first, "runtime_ensure_elapsed_ms", 999.0)
        record_elapsed_on(second, "runtime_ensure_elapsed_ms", 999.0)

    # The first member (the active span) captures the descendant leaf timing:
    assert first.provider_elapsed_ms == 1234.5
    # Second member never held the span -> no leaf sub-timing of its own:
    assert second.provider_elapsed_ms == 0.0
    # Both members received the group total via record_elapsed_on:
    assert first.runtime_ensure_elapsed_ms == 999.0
    assert second.runtime_ensure_elapsed_ms == 999.0
    # Cycle aggregate still captured the provider work once:
    assert recorder.report()["aggregate_provider_wait_ms"] == 1234.5


def test_group_span_bind_does_not_cross_contaminate_concurrent_requirements() -> None:
    # Two DIFFERENT requirement spans active in sequence must not bleed timing
    # into each other -- the bind/unbind boundary is strict.
    from app.cycle_timing import (
        CycleTimingRecorder,
        cycle_scope,
        bind_span,
        unbind_span,
        record_provider_elapsed,
    )

    recorder = CycleTimingRecorder("no-cross-cycle")
    with cycle_scope(recorder):
        growth = recorder.start_requirement(INSTRUMENT_ID, "GROWTH_FACTS")
        token = bind_span(growth)
        try:
            record_provider_elapsed(500.0)
        finally:
            unbind_span(token)
        # After unbind, no span is active; a late recorder call must not land
        # on growth (proving the span is truly unbound, not still live).
        record_provider_elapsed(1.0)

        balance = recorder.start_requirement(INSTRUMENT_ID, "BALANCE_SHEET_FACTS")
        token = bind_span(balance)
        try:
            record_provider_elapsed(7.0)
        finally:
            unbind_span(token)

    assert growth.provider_elapsed_ms == 500.0
    assert balance.provider_elapsed_ms == 7.0
    # The cycle aggregate captures ALL provider work -- including the 1.0
    # emitted after the span was unbound (cycle totals are independent of any
    # single-requirement span, by design), plus both members' attribution.
    assert recorder.report()["aggregate_provider_wait_ms"] == 500.0 + 7.0 + 1.0


@pytest.mark.asyncio
async def test_ensure_under_bound_group_span_attributes_descendant_timing() -> None:
    # End-to-end through the REAL ResearchReadinessRuntime.ensure() path:
    # bind_span(first_member) around ensure(), with a capability executor that
    # emits record_provider_elapsed inside execute_primary (as production does
    # via research_fetching). Assert the first member's record captures the
    # provider timing and the cycle aggregate survives -- reproducing the
    # INFY/KPIGREEN zero-attribution symptom and its fix.
    from app.cycle_timing import CycleTimingRecorder, cycle_scope, bind_span, unbind_span

    source = StateDataSource({"GROWTH_FACTS", "BALANCE_SHEET_FACTS"})
    recorder = CycleTimingRecorder("ensure-bound-cycle")

    captured: dict[str, object] = {}

    class RecordingProviderExecutor:
        def __init__(self, src):
            self._src = src
            self.primary_calls: list[set[str]] = []

        async def execute_primary(self, _instrument_id, targets, **_kwargs):
            ids = {t.requirement_id for t in targets}
            self.primary_calls.append(ids)
            self._src.missing.difference_update(ids)
            # Emit the kind of descendant leaf timing production emits inside
            # the provider/discovery/PDF/persistence layer:
            cycle_timing.record_provider_elapsed(250.0)
            return CapabilityExecutionResult(("RECORDED_PRIMARY",), {})

        async def execute_approved_fallbacks(self, _instrument_id, targets):
            return CapabilityExecutionResult((), {})

    executor = RecordingProviderExecutor(source)
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, executor)

    with cycle_scope(recorder):
        first_member = recorder.start_requirement(INSTRUMENT_ID, "GROWTH_FACTS")
        second_member = recorder.start_requirement(INSTRUMENT_ID, "BALANCE_SHEET_FACTS")
        try:
            token = bind_span(first_member)
            try:
                result = await runtime.ensure(
                    INSTRUMENT_ID,
                    jurisdiction="INDIA",
                    requirement_ids=["GROWTH_FACTS", "BALANCE_SHEET_FACTS"],
                    wait_for_completion=True,
                )
            finally:
                unbind_span(token)
        finally:
            recorder.complete_requirement(first_member)
            recorder.complete_requirement(second_member)
        captured["result"] = result
        captured["first"] = first_member
        captured["second"] = second_member
        captured["report"] = recorder.report()

    # Both members served by the single shared primary call:
    assert executor.primary_calls == [{"GROWTH_FACTS", "BALANCE_SHEET_FACTS"}]
    # The first group member (the active span during ensure()) now captures the
    # descendant provider timing -- this is the field that was 0.0 pre-fix:
    first_member = captured["first"]
    second_member = captured["second"]
    assert first_member.planning_elapsed_ms >= 0.0
    assert first_member.readiness_load_elapsed_ms >= 0.0
    assert first_member.final_readiness_reload_elapsed_ms >= 0.0
    assert first_member.provider_elapsed_ms == 250.0
    # Second member never held the span, so it carries no leaf sub-timing
    # (only the group totals written via record_elapsed_on in production):
    assert second_member.provider_elapsed_ms == 0.0
    # Cycle aggregate survived even though it's set independently of the span:
    assert captured["report"]["aggregate_provider_wait_ms"] == 250.0
    # And the readiness result itself is unchanged (semantics preserved):
    result = captured["result"]
    assert result.failures == {}
    assert result.readiness.for_requirement("GROWTH_FACTS").status != ResearchRequirementStatus.MISSING


# ---------------------------------------------------------------------------
# Redundancy-elimination tests for ResearchReadinessRuntime.read().
#
# Root-cause attribution (controlled INFY + KPIGREEN cycle, stage2WallClockMs
# = 62508): ResearchReadinessService.assess was invoked 65 times for 2
# investigations because every runtime.read() re-ran both
#   (a) repository.market_session_data() -> 2 locked persistence loads
#       (load_market_schedules + load_market_calendar_exceptions), and
#   (b) RepositoryResearchReadinessAdapter.load_by_global_instrument_id() ->
#       8 synchronous repository reads,
#   under a single _persistence_worker_lock hold. So assess_count == read_count
#   == 65 and market_session_data ran 130 locked sub-calls.
#
# Two provably-safe optimizations were applied:
#   (1) Bounded market-session cache on ResearchRepository (invalidated on any
#       persistence write) collapses the repeated locked schedule loads.
#   (2) evidence_only=True reads reuse the in-flight ResearchReadinessResult
#       when no persistence mutation occurred since the snapshot was taken,
#       and durable reads (evidence_only=False) clear the cache so the final
#       verification and post-mutation re-reads remain authoritative.
#
# These tests are provider-free: they count assess invocations (via
# StateDataSource.loads) and market_session_data invocations without touching
# any network/provider code.
# ---------------------------------------------------------------------------


class MutationTrackingRepository:
    """RuntimeRepository stub whose _run_blocking_persistence mirrors
    ResearchRepository's write-prefix classification and bumps a readiness-
    mutation generation counter on persistence writes, so evidence-only reuse
    invalidation can be exercised without a real database."""

    def __init__(self) -> None:
        self._readiness_mutation_generation = 0
        self.write_calls: list[str] = []

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        qn = getattr(operation, "__qualname__", "") or ""
        name = getattr(operation, "__name__", "") or ""
        label = name or qn
        if any(label.startswith(p) for p in ResearchRepository._PERSISTENCE_WRITE_PREFIXES):
            self.write_calls.append(label)
            self._readiness_mutation_generation += 1
        return operation(*args, **kwargs)


class _NoopExecutor:
    async def execute_primary(self, *_a, **_k):
        return CapabilityExecutionResult((), {})

    async def execute_approved_fallbacks(self, *_a, **_k):
        return CapabilityExecutionResult((), {})


def _run_sync(coro):
    """Run a coroutine to completion on a fresh loop (test helper)."""
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run_market_session_sync(repository, markets):
    """Invoke repository.market_session_data synchronously (test helper)."""
    return _run_sync(repository.market_session_data(markets))


@pytest.mark.asyncio
async def test_evidence_only_read_reuses_unmutated_snapshot() -> None:
    """Multiple evidence_only=True reads with no intervening persistence
    mutation must reuse the cached ResearchReadinessResult instead of invoking
    assess a second time. A durable (evidence_only=False) read precedes them,
    so the cache is primed; the first evidence_only read misses (loads go 1->2)
    and caches, subsequent evidence_only reads hit (loads unchanged)."""
    source = StateDataSource(set())
    runtime = _runtime(source, UpdatingExecutor(source))  # uses RuntimeRepository stub

    await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")  # durable: loads=1, cache cleared
    assert source.loads == 1
    assert INSTRUMENT_ID not in runtime._evidence_readiness_cache

    first = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)
    assert source.loads == 2  # cache miss -> assess ran
    assert INSTRUMENT_ID in runtime._evidence_readiness_cache

    second = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)
    third = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)
    assert source.loads == 2  # both cache hits -> no new assess
    assert second is first
    assert third is first


@pytest.mark.asyncio
async def test_persistence_mutation_invalidates_reused_readiness() -> None:
    """A persistence write between an evidence_only read and the next must bump
    the repository mutation generation so the reused snapshot is discarded and
    the subsequent evidence_only read re-assesses (durable reload)."""
    source = StateDataSource(set())
    repo = MutationTrackingRepository()
    runtime = ResearchReadinessRuntime(repo, source, _NoopExecutor())  # type: ignore[arg-type]

    await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")  # durable: loads=1
    assert source.loads == 1

    evidence = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)
    assert source.loads == 2  # cache miss -> assess ran
    assert INSTRUMENT_ID in runtime._evidence_readiness_cache
    cached_gen = runtime._evidence_readiness_cache[INSTRUMENT_ID][1]
    assert cached_gen == 0

    # Simulate a persistence write (e.g. record_acquisition_observation in
    # ensure post-execution recording) -- the generation must bump.
    repo._readiness_mutation_generation += 1

    again = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)
    assert source.loads == 3  # mutation invalidated cache -> re-assessed
    assert again is not evidence  # not the stale cached object
    assert runtime._evidence_readiness_cache[INSTRUMENT_ID][1] == 1


@pytest.mark.asyncio
async def test_durable_read_still_reloads_after_mutation() -> None:
    """A durable (evidence_only=False) read after a mutation must always hit the
    backend -- it is the authoritative verification path and must never serve
    the evidence-only cache. This preserves the post-mutation final durable
    verification at deep_investigation.investigate L589."""
    source = StateDataSource(set())
    runtime = _runtime(source, UpdatingExecutor(source))

    await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")  # loads=1
    before = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)
    assert source.loads == 2

    # Force a mutation generation bump (simulates an upsert/record during exec).
    # RuntimeRepository (the _runtime stub) does not carry the attribute, so
    # attach it as a plain attribute mirroring ResearchRepository.
    setattr(runtime.repository, "_readiness_mutation_generation",
            getattr(runtime.repository, "_readiness_mutation_generation", 0) + 1)
    after = await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")  # durable reload
    assert source.loads == 3
    assert after is not before


@pytest.mark.asyncio
async def test_final_durable_verification_is_never_cached() -> None:
    """The final authoritative readiness verification (investigate L589, a
    durable read) must not leave an evidence-only cache entry behind. A final
    durable read must always perform the backend load."""
    source = StateDataSource({"CURRENT_NEWS"})
    executor = UpdatingExecutor(source, update_primary=True)
    runtime = _runtime(source, executor)

    result = await runtime.ensure(
        INSTRUMENT_ID,
        jurisdiction="INDIA",
        requirement_ids=["CURRENT_NEWS"],
    )
    # The final read inside ensure() is durable (evidence_only=False) and is
    # never cached -- assert no evidence-only entry survived the ensure() flow.
    assert INSTRUMENT_ID not in runtime._evidence_readiness_cache
    assert result.readiness.for_requirement("CURRENT_NEWS").status == ResearchRequirementStatus.READY_FRESH


@pytest.mark.asyncio
async def test_assess_call_reduction_under_evidence_only_spike() -> None:
    """Simulate the deep_investigation evidence_only sufficiency pre-checks
    (multiple evidence_only reads in a row with no mutation) and assert the
    assess invocation count is materially reduced: 1 durable read + 1
    evidence_only miss + 4 cache hits = 2 assess calls instead of 6."""
    source = StateDataSource(set())
    runtime = _runtime(source, UpdatingExecutor(source))

    await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")  # durable -> assess #1
    for _ in range(5):
        await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)  # 1 miss + 4 hits
    assert source.loads == 2  # 6 reads, but only 2 assess invocations


def test_market_session_data_reuse_within_cycle(tmp_path) -> None:
    """Within one investigate flow, market_session_data for the same market set
    must be served from the repository bounded cache instead of re-running the
    two locked loads on every read. No persistence write occurs, so the cache
    is not invalidated."""
    persistence = SqliteResearchPersistence(tmp_path / "sched.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_demo_enabled=False), persistence=persistence
    )
    repository._market_session_cache.clear()

    first_markets, first_exceptions = _run_market_session_sync(repository, {"XNSE"})
    second_markets, second_exceptions = _run_market_session_sync(repository, {"XNSE"})

    assert first_markets is second_markets
    assert first_exceptions is second_exceptions
    assert len(repository._market_session_cache) == 1
    assert frozenset({"XNSE"}) in repository._market_session_cache


def test_market_session_data_cache_survives_unrelated_writes(tmp_path) -> None:
    """Area 3 fix: a persistence write that cannot possibly touch the
    market_schedules / market_trading_calendar_exceptions tables (e.g.
    record_acquisition_observation -- grep of app/persistence.py confirms no
    upsert/insert/delete path exists for those two tables at all) must NOT
    invalidate the market-session cache. Previously ANY write cleared it,
    which is exactly why load_market_schedules/load_market_calendar_exceptions
    were observed ~598 times in a single Radar cycle despite this cache
    already existing -- upsert_market_price_observation alone (~3944 calls/
    cycle) was wiping it almost continuously. The readiness mutation
    generation (which gates the SEPARATE evidence-only readiness cache) must
    still bump on every write, unaffected by this narrowing."""
    persistence = SqliteResearchPersistence(tmp_path / "sched-write.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_demo_enabled=False), persistence=persistence
    )
    repository._market_session_cache.clear()
    profile = _profile(uuid4())
    repository.profiles.append(profile)

    _run_market_session_sync(repository, {"XNSE"})  # populates cache
    assert len(repository._market_session_cache) == 1

    _run_sync(
        repository.record_acquisition_observation(
            profile.instrument_id,
            "CURRENT_NEWS",
            "READINESS_EXECUTOR",
            "COMPLETED",
            NOW,
        )
    )
    assert len(repository._market_session_cache) == 1  # survives an unrelated write
    assert repository._readiness_mutation_generation == 1  # still bumped


def test_market_session_data_cache_invalidated_by_schedule_relevant_write(tmp_path) -> None:
    """Safety net for the Area 3 narrowing above: a write whose operation name
    actually matches a market-schedule/calendar-exception mutation (none
    exists in this codebase today, but _is_market_session_cache_invalidating_write
    exists precisely so one could be added safely later) still invalidates the
    cache -- this proves the narrowing never lets a genuinely schedule-relevant
    write serve a stale cached snapshot across the mutation boundary."""
    persistence = SqliteResearchPersistence(tmp_path / "sched-relevant.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_demo_enabled=False), persistence=persistence
    )
    repository._market_session_cache.clear()
    profile = _profile(uuid4())
    repository.profiles.append(profile)

    _run_market_session_sync(repository, {"XNSE"})  # populates cache
    assert len(repository._market_session_cache) == 1

    def upsert_market_schedule(*_args, **_kwargs):
        return None

    _run_sync(repository._run_blocking_persistence(upsert_market_schedule))
    assert len(repository._market_session_cache) == 0  # invalidated
    assert repository._readiness_mutation_generation == 1


def test_market_session_data_cache_expires_after_ttl(tmp_path, monkeypatch) -> None:
    """The narrowed-invalidation market-session cache has no write-driven
    reset left for ordinary cycle traffic, and ResearchRepository outlives a
    single Radar cycle -- so it must not serve schedule/calendar data
    indefinitely stale. A bounded TTL re-reads reference data periodically
    without reintroducing broad per-write invalidation."""
    persistence = SqliteResearchPersistence(tmp_path / "sched-ttl.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_demo_enabled=False), persistence=persistence
    )
    repository._market_session_cache.clear()
    profile = _profile(uuid4())
    repository.profiles.append(profile)

    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "app.repository.time.monotonic", lambda: clock["now"]
    )

    _run_market_session_sync(repository, {"XNSE"})  # populates cache at t=1000
    assert len(repository._market_session_cache) == 1

    # Still within the TTL window: the cached tuple is reused as-is.
    clock["now"] = 1000.0 + repository._MARKET_SESSION_CACHE_TTL_SECONDS - 1
    cached_entry = repository._market_session_cache[frozenset({"XNSE"})]
    _run_market_session_sync(repository, {"XNSE"})
    assert repository._market_session_cache[frozenset({"XNSE"})] is cached_entry

    # Past the TTL: the next call must reload (new cache-entry timestamp),
    # proving the cache does not serve stale reference data indefinitely.
    clock["now"] = 1000.0 + repository._MARKET_SESSION_CACHE_TTL_SECONDS + 1
    _run_market_session_sync(repository, {"XNSE"})
    assert repository._market_session_cache[frozenset({"XNSE"})] is not cached_entry


def test_market_session_data_does_not_leak_across_instruments(tmp_path) -> None:
    """A schedule-relevant persistence write invalidates the global
    market-session cache so no instrument can observe schedule data cached
    before the mutation boundary. This proves the cache cannot leak a stale
    schedule snapshot across an unsafe (schedule-write) boundary."""
    persistence = SqliteResearchPersistence(tmp_path / "sched-leak.sqlite")
    repository = ResearchRepository(
        settings=Settings(research_demo_enabled=False), persistence=persistence
    )
    repository._market_session_cache.clear()
    profile_a = _profile(uuid4())
    profile_b = _profile(uuid4())
    repository.profiles.append(profile_a)
    repository.profiles.append(profile_b)

    cached_before = _run_market_session_sync(repository, {"XNSE"})
    assert len(repository._market_session_cache) == 1

    def upsert_market_schedule(*_args, **_kwargs):
        return None

    _run_sync(repository._run_blocking_persistence(upsert_market_schedule))
    assert len(repository._market_session_cache) == 0

    # The next load for B is fresh (re-queried from storage), not the pre-write
    # cached tuple object.
    fresh_markets, fresh_exceptions = _run_market_session_sync(repository, {"XNSE"})
    assert fresh_markets is not None
    assert fresh_exceptions is not None
