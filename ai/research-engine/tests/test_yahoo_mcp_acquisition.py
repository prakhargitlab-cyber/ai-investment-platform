from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey, merge_fact
from app.models import CompanyResearchProfile, ProvenancedValue, SourceMode
from app.research_readiness import (
    ProviderAuthorityRegistry,
    ResearchRefreshTarget,
    ResearchRefreshPlan,
    ResearchRequirementStatus,
    RuleEngineArea,
)
from app.research_readiness_runtime import CapabilityExecutionResult, ResearchReadinessRuntime
from app.repository import ResearchRepository
from app.settings import Settings
from app.persistence import SqliteResearchPersistence
from app.yahoo_mcp_acquisition import (
    ExternalMcpAcquisitionError,
    HttpExternalResearchToolGateway,
    McpFirstProviderPriority,
    McpFirstResearchCapabilityExecutor,
    YahooMcpNormalizedResult,
    YahooMcpResultPersister,
)


INSTRUMENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)


def profile(*, mapping=True):
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID,
        company_id=uuid4(),
        company_name="Ready Limited",
        ticker="READY",
        exchange="NSE",
        mic="XNSE",
        country="IN",
        currency="INR",
        provider_instrument_ids={"YAHOO_FINANCE": "READY.NS"} if mapping else {},
    )


def target(requirement="LATEST_PRICE", region="INDIA"):
    return ResearchRefreshTarget(
        requirement_id=requirement,
        rule_engine_area=RuleEngineArea.VALUATION,
        reason=ResearchRequirementStatus.MISSING,
        authority_policy=ProviderAuthorityRegistry.default().policy_for(requirement, region),
        existing_evidence_ids=(),
    )


def result(requirement="LATEST_PRICE", **overrides):
    value = {
        "adapterVersion": "YAHOO_FINANCE_MCP_ADAPTER_V1",
        "providerId": "YAHOO_FINANCE_MCP",
        "sourceTier": "APPROVED_EXTERNAL_TOOL",
        "sourceTool": "get_quote",
        "region": "INDIA",
        "requirementId": requirement,
        "globalInstrumentId": str(INSTRUMENT_ID),
        "symbol": "READY.NS",
        "exchange": "NSE",
        "currency": "INR",
        "retrievedAt": NOW.isoformat(),
        "observedAt": NOW.isoformat(),
        "sourceUrl": "https://finance.yahoo.com/quote/READY.NS",
        "confidence": 0.8,
        "freshness": "FRESH",
        "structuredFacts": [
            {
                "metric": "latestPrice",
                "value": "250",
                "unit": "INR",
                "asOf": NOW.isoformat(),
                "publishedAt": None,
                "sourceUrl": "https://finance.yahoo.com/quote/READY.NS",
                "confidence": 0.8,
                "rawFieldOrigin": "regularMarketPrice",
            }
        ],
        "financialFacts": [],
        "marketObservations": [
            {"observedAt": NOW.isoformat(), "price": "250", "currency": "INR"}
        ],
        "companyProfile": None,
        "news": [],
        "events": [],
        "shareholding": None,
    }
    value.update(overrides)
    return YahooMcpNormalizedResult.model_validate(value)


class FakeRepository:
    def __init__(self, item=None):
        self.item = item or profile()
        self.structured = []
        self.prices = []
        self.financial = []
        self.evidence = []
        self.shareholding = []

    def profile(self, _instrument_id):
        return self.item

    async def persist_structured_market_snapshot_async(self, record):
        self.structured.append(record)

    async def upsert_market_price_observation_async(self, observation):
        self.prices.append(observation)

    async def persist_international_financial_facts_async(self, facts):
        self.financial.extend(facts)
        return len(facts)

    async def persist_external_mcp_evidence_async(self, document, event):
        self.evidence.append((document, event))

    async def persist_external_mcp_shareholding_async(self, snapshot):
        self.shareholding.append(snapshot)
        return True


class FakeGateway:
    def __init__(self, outcome=None):
        self.outcome = outcome or result()
        self.calls = []

    async def acquire_requirement(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeLegacy:
    def __init__(self, failure=None):
        self.calls = []
        self.fallback_calls = []
        self.failure = failure

    async def execute_primary(self, instrument_id, targets, **kwargs):
        self.calls.append((instrument_id, tuple(targets), kwargs))
        failures = ({targets[0].requirement_id: self.failure} if targets and self.failure else {})
        return CapabilityExecutionResult(("LEGACY_PROVIDER",) if targets else (), failures)

    async def execute_approved_fallbacks(self, instrument_id, targets):
        self.fallback_calls.append((instrument_id, tuple(targets)))
        return CapabilityExecutionResult(("LEGACY_SECONDARY",), {})


@pytest.mark.parametrize("region,fallback", [("INDIA", "NSE"), ("USA", "SEC_EDGAR"), ("EUROPE", "EODHD")])
def test_region_policy_is_yahoo_first_and_keeps_approved_fallback(region, fallback) -> None:
    route = McpFirstProviderPriority().route(region, "QUARTERLY_FINANCIALS")
    assert route.providers[0] == "YAHOO_FINANCE_MCP"
    assert any(fallback in provider for provider in route.providers[1:])


def test_unknown_region_and_governance_do_not_route_to_yahoo() -> None:
    priority = McpFirstProviderPriority()
    assert "YAHOO_FINANCE_MCP" not in priority.route("GLOBAL", "LATEST_PRICE").providers
    assert "YAHOO_FINANCE_MCP" not in priority.route("INDIA", "GOVERNANCE_HISTORY").providers


@pytest.mark.asyncio
async def test_yahoo_success_persists_and_causes_zero_fallback_calls() -> None:
    repository, gateway, legacy = FakeRepository(), FakeGateway(), FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    outcome = await executor.execute_primary(
        INSTRUMENT_ID,
        (target(),),
        jurisdiction="INDIA",
        correlation_id="correlation-5b",
        identity_headers={},
    )
    assert gateway.calls[0][1]["request_id"] == "correlation-5b"
    assert legacy.calls == []
    assert repository.structured and repository.prices
    assert outcome.satisfied_requirement_ids == ("LATEST_PRICE",)
    assert outcome.failures == {}


@pytest.mark.asyncio
async def test_completed_yahoo_requirement_is_not_sent_to_secondary_fallback() -> None:
    repository, gateway, legacy = FakeRepository(), FakeGateway(), FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    class DataSource:
        def mark_refreshing(self, *_args):
            pass

        def finish_refresh(self, *_args):
            pass

    runtime = object.__new__(ResearchReadinessRuntime)
    runtime.data_source = DataSource()
    runtime.executor = executor

    async def still_missing(*_args, **_kwargs):
        return SimpleNamespace(
            for_requirement=lambda _requirement: SimpleNamespace(
                status=ResearchRequirementStatus.MISSING
            )
        )

    runtime.read = still_missing
    plan = ResearchRefreshPlan(INSTRUMENT_ID, (target(),), NOW)
    await runtime._execute_plan(
        plan,
        jurisdiction="INDIA",
        correlation_id="no-double-call",
        identity_headers={},
    )
    assert legacy.calls == []
    assert legacy.fallback_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "EXTERNAL_CAPABILITY_UNSUPPORTED",
        "DOWNSTREAM_TIMEOUT",
        "EXTERNAL_SCHEMA_INVALID",
        "EXTERNAL_RESULT_INCOMPLETE",
        "EXTERNAL_IDENTITY_CONFLICT",
        "EXTERNAL_RESULT_STALE",
        "EXTERNAL_PROVIDER_UNAVAILABLE",
    ],
)
async def test_yahoo_failure_conditions_keep_reason_when_existing_fallback_is_empty(code) -> None:
    repository = FakeRepository()
    gateway = FakeGateway(ExternalMcpAcquisitionError(code))
    legacy = FakeLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)
    outcome = await executor.execute_primary(
        INSTRUMENT_ID,
        (target(),),
        jurisdiction="INDIA",
        correlation_id="fallback-5b",
        identity_headers={},
    )
    assert len(legacy.calls) == 1
    assert outcome.executed_capabilities == (
        "YAHOO_FINANCE_MCP:LATEST_PRICE",
        "LEGACY_PROVIDER",
    )
    assert outcome.failures == {"LATEST_PRICE": code}


@pytest.mark.asyncio
async def test_mcp_incomplete_and_partial_legacy_result_keep_both_diagnostics() -> None:
    repository = FakeRepository()
    gateway = FakeGateway(ExternalMcpAcquisitionError("EXTERNAL_RESULT_INCOMPLETE"))
    legacy = FakeLegacy("LEGACY_RETURNED_PARTIAL_EVIDENCE")
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    outcome = await executor.execute_primary(
        INSTRUMENT_ID,
        (target(),),
        jurisdiction="INDIA",
        correlation_id="partial-fallback-5b",
        identity_headers={},
    )

    assert outcome.failures == {
        "LATEST_PRICE": "EXTERNAL_RESULT_INCOMPLETE|LEGACY_RETURNED_PARTIAL_EVIDENCE"
    }


@pytest.mark.asyncio
async def test_verified_yahoo_mapping_is_required_before_network_access() -> None:
    gateway = HttpExternalResearchToolGateway("http://mcp-gateway", 1, "research-engine")
    grant = target().authority_policy.fallback_policy.authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="LATEST_PRICE",
        status=ResearchRequirementStatus.MISSING,
        confidence=None,
        permitted_provider_ids=("YAHOO_FINANCE_MCP",),
        now=NOW,
    )
    with pytest.raises(ExternalMcpAcquisitionError, match="VERIFIED_YAHOO_MAPPING_REQUIRED"):
        await gateway.acquire_requirement(
            profile(mapping=False),
            region="INDIA",
            requirement_id="LATEST_PRICE",
            authorization=grant,
            request_id="mapping-required",
        )


@pytest.mark.asyncio
async def test_persister_does_not_mutate_verified_mapping() -> None:
    repository = FakeRepository()
    before = dict(repository.item.provider_instrument_ids)
    await YahooMcpResultPersister(repository).persist(result(), repository.item)
    assert repository.item.provider_instrument_ids == before


def _financial(metric, value, tier, provider):
    key = FinancialFactKey(INSTRUMENT_ID, metric, "2025-03-31", "ANNUAL", "CONSOLIDATED")
    return FinancialFact(
        key,
        ProvenancedValue(
            value=Decimal(value),
            source_url="https://source.test/fact",
            source_name=provider,
            retrieved_at=NOW,
        ),
        tier,
        provider,
        f"{provider}:{metric}",
        SourceMode.REAL,
    )


def test_mcp_first_acquisition_does_not_downgrade_official_fact_precedence() -> None:
    official = _financial("revenue", "100", FactSourceTier.OFFICIAL_NSE, "NSE")
    yahoo = _financial("revenue", "90", FactSourceTier.YAHOO, "YAHOO_FINANCE_MCP")
    assert merge_fact(official, yahoo) is official
    assert merge_fact(yahoo, official) is official


@pytest.mark.asyncio
async def test_mcp_persister_keeps_existing_official_fact_for_same_period() -> None:
    canonical = profile()
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=False, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(),
    )
    repository.profiles.append(canonical)
    official = _financial("revenue", "100", FactSourceTier.OFFICIAL_NSE, "NSE")
    assert repository.persistence.upsert_financial_fact(official) is True
    normalized = result(
        "BUSINESS_QUALITY_FACTS",
        structuredFacts=[],
        marketObservations=[],
        financialFacts=[
            {
                "metric": "revenue",
                "value": "90",
                "unit": "INR",
                "asOf": NOW.isoformat(),
                "publishedAt": NOW.isoformat(),
                "sourceUrl": "https://finance.yahoo.com/quote/READY.NS",
                "confidence": 0.8,
                "rawFieldOrigin": "totalRevenue",
                "periodEnd": "2025-03-31",
                "periodType": "ANNUAL",
                "reportingBasis": "CONSOLIDATED",
            }
        ],
    )
    await YahooMcpResultPersister(repository).persist(normalized, canonical)
    saved = repository.financial_facts_for(INSTRUMENT_ID)
    assert len(saved) == 1
    assert saved[0].source_provider == "NSE"
    assert saved[0].value.value == Decimal("100")


def test_wire_contract_rejects_private_or_unknown_provider_fields() -> None:
    raw = result().model_dump(mode="json", by_alias=True)
    raw["quantity"] = 10
    with pytest.raises(ValidationError):
        YahooMcpNormalizedResult.model_validate(raw)


@pytest.mark.asyncio
async def test_successful_empty_news_is_durable_metadata_without_invented_events():
    persistence = SqliteResearchPersistence()
    repository = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False), persistence=persistence)
    canonical = profile()
    repository.profiles.append(canonical)
    empty = result("CURRENT_NEWS", structuredFacts=[], marketObservations=[], acquisitionOutcome="SUCCESS_EMPTY")
    assert await YahooMcpResultPersister(repository).persist(empty, canonical) == 0
    rows = persistence.load_acquisition_observations(INSTRUMENT_ID)
    assert rows[0]["outcome"] == "SUCCESS_EMPTY"
    assert rows[0]["evidence_count"] == 0
    assert persistence.load_events() == []
    assert persistence.load_documents() == []

@pytest.mark.asyncio
async def test_targeted_refresh_keeps_other_durable_failure_states():
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    from app.research_readiness import ResearchRequirementRegistry
    persistence = SqliteResearchPersistence()
    repository = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False), persistence=persistence)
    repository.profiles.append(profile())
    await repository.record_acquisition_observation(INSTRUMENT_ID, "CURRENT_NEWS", "READINESS_EXECUTOR", "FAILED", NOW, failure_reason="ACQUISITION_TIMEOUT")
    adapter = RepositoryResearchReadinessAdapter(repository)
    adapter.finish_refresh(INSTRUMENT_ID, {"VALUATION_INPUTS": "PROVIDER_UNAVAILABLE"})
    snapshot = adapter.load_by_global_instrument_id(INSTRUMENT_ID, ResearchRequirementRegistry.default().requirements)
    assert snapshot.failure_reasons["CURRENT_NEWS"] == "ACQUISITION_TIMEOUT"
    assert snapshot.failure_reasons["VALUATION_INPUTS"] == "PROVIDER_UNAVAILABLE"
