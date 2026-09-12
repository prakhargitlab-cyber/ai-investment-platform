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

    async def ensure_structured_market(self, _instrument_id, classes):
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

    def register_global_profile_metadata(self, instrument_id, _metadata):
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


def test_nse_quarterly_pdf_new_write_persists_metadata_and_facts_without_content(tmp_path) -> None:
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
    assert persisted.normalized_text is None
    assert persisted.raw_text is None
    assert persisted.canonical_url == SOURCE_URL
    assert any(fact.key.metric == "revenue" and fact.key.period_type == "QUARTERLY" for fact in facts)
    assert any(fact.value.source_url == SOURCE_URL for fact in facts)
    assert readiness.for_requirement("QUARTERLY_FINANCIALS").source_url == SOURCE_URL
    assert list(tmp_path.glob("*.pdf")) == []


def test_nse_financial_result_classification_never_persists_unparsed_pdf_text(tmp_path) -> None:
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
    assert persisted.normalized_text is None
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
