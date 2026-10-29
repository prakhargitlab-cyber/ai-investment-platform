from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import respx
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


@pytest.fixture
def nse_session_repository():
    from pathlib import Path

    store = SqliteResearchPersistence()
    migration = (Path(__file__).resolve().parents[3] / "services/research-service/src/main/resources/db/migration"
                 / "V25__nse_cash_equity_calendar_2025_2026.sql")
    store._connection.executescript(migration.read_text(encoding="utf-8"))

    async def load(markets):
        return store.load_market_schedules(markets), store.load_market_calendar_exceptions(markets)

    yield SimpleNamespace(market_session_data=load, store=store)
    store._connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("now,close,until", [
    ("2026-10-18T12:00:00Z", "2026-10-16T10:00:00Z", "2026-10-19T03:45:00Z"),
    # V25 official NSE holiday and special-session exceptions, loaded from persistence.
    ("2026-10-02T12:00:00Z", "2026-10-01T10:00:00Z", "2026-10-05T03:45:00Z"),
    ("2026-01-31T12:00:00Z", "2026-01-30T10:00:00Z", "2026-02-01T03:45:00Z"),
    ("2026-02-01T12:00:00Z", "2026-02-01T10:00:00Z", "2026-02-02T03:45:00Z"),
    ("2026-02-01T04:00:00Z", None, None),
    ("2026-10-19T03:45:00Z", None, None),
    ("2026-10-19T08:00:00Z", None, None),
])
async def test_latest_price_context_uses_persisted_nse_calendar(nse_session_repository, now, close, until):
    gateway = HttpExternalResearchToolGateway("http://mcp-gateway", 1, "research-engine",
                                              repository=nse_session_repository)
    context = await gateway._latest_price_session(profile(), datetime.fromisoformat(now))
    if close is None:
        assert context is None
    else:
        assert datetime.fromisoformat(context["sessionClose"]) == datetime.fromisoformat(close)
        assert datetime.fromisoformat(context["validUntil"]) == datetime.fromisoformat(until)


@pytest.mark.asyncio
async def test_unknown_calendar_and_unverified_special_hours_do_not_extend_price_validity(nse_session_repository):
    gateway = HttpExternalResearchToolGateway("http://mcp-gateway", 1, "research-engine",
                                              repository=nse_session_repository)
    store = nse_session_repository.store
    store._connection.execute("UPDATE market_trading_calendar_exceptions SET open_time=NULL, close_time=NULL "
                              "WHERE market_code='NSE' AND trading_date='2026-02-01'")
    assert await gateway._latest_price_session(profile(), datetime(2026, 2, 1, 12, tzinfo=timezone.utc)) is None
    store._connection.execute("DELETE FROM market_trading_schedules")
    assert await gateway._latest_price_session(profile(), datetime(2026, 10, 18, 12, tzinfo=timezone.utc)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("requirement", ["LATEST_PRICE", "HISTORICAL_PRICE_SERIES"])
async def test_http_gateway_transmits_calendar_only_for_latest_price(monkeypatch, nse_session_repository, requirement):
    import app.yahoo_mcp_acquisition as module

    sunday = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return sunday.astimezone(tz)

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    gateway = HttpExternalResearchToolGateway("http://mcp-gateway", 1, "research-engine",
                                              repository=nse_session_repository)
    grant = target(requirement).authority_policy.fallback_policy.authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID, requirement_id=requirement,
        status=ResearchRequirementStatus.MISSING, confidence=None,
        permitted_provider_ids=("YAHOO_FINANCE_MCP",), now=sunday,
    )
    with respx.mock:
        route = respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(
            return_value=httpx.Response(200, json={"ok": True, "data": result(requirement).model_dump(mode="json", by_alias=True)}))
        try:
            await gateway.acquire_requirement(profile(), region="INDIA", requirement_id=requirement,
                                              authorization=grant, request_id="calendar-boundary")
        finally:
            await gateway._client.aclose()
        import json
        body = json.loads(route.calls.last.request.content)
    if requirement == "LATEST_PRICE":
        assert body["latestPriceSession"] == {
            "market": "XNSE", "sessionClose": "2026-10-16T10:00:00+00:00",
            "validUntil": "2026-10-19T03:45:00+00:00",
        }
    else:
        assert "latestPriceSession" not in body


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


@pytest.mark.asyncio
async def test_persister_stamps_last_price_at_to_observation_time_not_retrieval():
    # Regression for last_price_at semantics: a stale Friday-close quote that is
    # re-downloaded "today" must keep its observation time, so refresh eligibility
    # (price_sync_eligible) can correctly flag it for re-fetch.
    repository = FakeRepository()
    observed = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)   # Friday 15:30 IST close
    retrieved = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)   # Monday re-download
    normalized = result(
        observedAt=observed.isoformat(),
        retrievedAt=retrieved.isoformat(),
        structuredFacts=[
            {
                "metric": "latestPrice", "value": "250", "unit": "INR",
                "asOf": observed.isoformat(), "publishedAt": None,
                "sourceUrl": "https://finance.yahoo.com/quote/READY.NS",
                "confidence": 0.8, "rawFieldOrigin": "regularMarketPrice",
            }
        ],
    )
    await YahooMcpResultPersister(repository).persist(normalized, repository.item)
    record = repository.structured[-1]
    assert record.market_as_of == observed
    assert record.last_price_at == observed       # observation time, NOT retrieval time
    assert record.last_price_at != retrieved


@pytest.mark.asyncio
async def test_persisted_news_article_source_independence_key_is_deterministic_sha256_hex():
    # Regression: source_independence_key is a fixed-width CHAR(64) column.
    # It must never hold the raw article URL (which is neither guaranteed to
    # be 64 characters nor safe to store there -- see StringDataRightTruncation
    # / format defects this replaces), only a deterministic SHA-256 hex digest
    # built from the repository's established content_hash(...) convention.
    from app.normalization import content_hash

    long_url = "https://news.example.test/a/" + ("x" * 2200) + "?id=1"
    repository = FakeRepository()
    normalized = result(
        news=[
            {
                "headline": "Board approves buyback",
                "url": long_url,
                "publishedAt": NOW.isoformat(),
                "publisher": "Example Wire",
                "issuerSymbol": "READY.NS",
                "summary": "The board approved a share buyback programme.",
            }
        ],
    )
    await YahooMcpResultPersister(repository).persist(normalized, repository.item)
    assert len(repository.evidence) == 1
    document, event = repository.evidence[0]

    # The complete URL round-trips unchanged (never truncated).
    assert document.canonical_url == long_url
    assert document.original_url == long_url
    assert len(long_url) > 2048

    key = document.source_independence_key
    assert key is not None
    assert len(key) == 64
    assert key == key.lower()
    assert all(c in "0123456789abcdef" for c in key)
    assert key == content_hash(long_url.casefold())

    # Deterministic: persisting the same canonical source again yields the
    # same identity key, and a differently-cased URL is treated as the same
    # source (case-insensitive), matching the prior casefold() intent.
    repository_again = FakeRepository()
    await YahooMcpResultPersister(repository_again).persist(normalized, repository_again.item)
    assert repository_again.evidence[0][0].source_independence_key == key

    upper_variant = result(
        news=[
            {
                "headline": "Board approves buyback",
                "url": long_url.upper(),
                "publishedAt": NOW.isoformat(),
                "publisher": "Example Wire",
                "issuerSymbol": "READY.NS",
                "summary": "The board approved a share buyback programme.",
            }
        ],
    )
    repository_ci = FakeRepository()
    await YahooMcpResultPersister(repository_ci).persist(upper_variant, repository_ci.item)
    assert repository_ci.evidence[0][0].source_independence_key == key


@pytest.mark.asyncio
@pytest.mark.parametrize("label,url", [
    ("normal-url", "https://news.example.test/event-identity/board-buyback"),
    ("over-2000-chars", "https://news.example.test/event-identity/" + ("q" * 2100)),
])
async def test_persisted_news_event_independence_key_is_deterministic_sha256_hex_and_keeps_tool_tier(label, url):
    # Regression: research_events.independence_key is also a fixed-width
    # CHAR(64) column. It previously stored a raw, unbounded
    # "YAHOO_FINANCE_MCP:<url>" string (StringDataRightTruncation risk for
    # long URLs, same defect class as source_independence_key). It must now
    # be a deterministic 64-char lowercase SHA-256 hex digest built from the
    # shared content_hash() helper for both a normal-length and a >2000-char
    # URL, while still preserving the ability to recognize a Yahoo-MCP-sourced
    # event for source-tier classification
    # (app.research_readiness_runtime._event_source), since that no longer
    # reads a literal prefix out of independence_key.
    from app.normalization import content_hash
    from app.research_readiness import ResearchSourceTier
    from app.research_readiness_runtime import _event_source

    if label == "over-2000-chars":
        assert len(url) > 2000
    repository = FakeRepository()
    normalized = result(
        news=[
            {
                "headline": "Board approves capacity expansion",
                "url": url,
                "publishedAt": NOW.isoformat(),
                "publisher": "Example Wire",
                "issuerSymbol": "READY.NS",
                "summary": "The board approved a capacity expansion plan.",
            }
        ],
    )
    await YahooMcpResultPersister(repository).persist(normalized, repository.item)
    assert len(repository.evidence) == 1
    document, event = repository.evidence[0]

    key = event.independence_key
    assert key is not None
    assert len(key) == 64
    assert key == key.lower()
    assert all(c in "0123456789abcdef" for c in key)
    expected = content_hash(f"YAHOO_FINANCE_MCP:{url.casefold()}")
    assert key == expected

    # Deterministic repeat for the same semantic source.
    repository_again = FakeRepository()
    await YahooMcpResultPersister(repository_again).persist(normalized, repository_again.item)
    assert repository_again.evidence[0][1].independence_key == key

    # Existing semantics require provider/source distinction: the same URL
    # attributed to a different source identity must not collide.
    other_source_key = content_hash(f"OTHER_PROVIDER:{url.casefold()}")
    assert other_source_key != key

    # Source-tier classification still recognizes this event as Yahoo MCP
    # tool-sourced (APPROVED_EXTERNAL_TOOL), now via hash verification rather
    # than a literal "YAHOO_FINANCE_MCP:" prefix check.
    source, tier = _event_source(event, "CURRENT_NEWS")
    assert source == "APPROVED_EXTERNAL_TOOL"
    assert tier == ResearchSourceTier.APPROVED_EXTERNAL_TOOL

    # An event whose independence_key does NOT match the Yahoo MCP convention
    # for its own source_url (i.e. a different, non-Yahoo source identity)
    # must not be misclassified as Yahoo MCP tool-sourced.
    impostor = event.model_copy(update={"independence_key": other_source_key})
    impostor_source, impostor_tier = _event_source(impostor, "CURRENT_NEWS")
    assert impostor_source != "APPROVED_EXTERNAL_TOOL"
    assert impostor_tier != ResearchSourceTier.APPROVED_EXTERNAL_TOOL


@pytest.mark.asyncio
@respx.mock
async def test_gateway_reuses_one_http_client_across_sequential_calls():
    """Root-cause fix (YAHOO_FINANCE_MCP sequential-call latency): a BASELINE
    candidate makes several sequential acquire_requirement() calls against
    this same gateway singleton inside one shared ensure_timeout_seconds
    window. Previously each call opened (`async with httpx.AsyncClient(...)`)
    and immediately tore down a brand-new client/connection, paying a fresh
    connection-establishment cost on every one of those sequential round
    trips. The gateway must now lazily create exactly one httpx.AsyncClient
    and reuse it (pooled/keep-alive) across every subsequent call, never
    opening a second one."""
    route = respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(
        return_value=httpx.Response(404, json={"ok": False, "error": {"code": "EXTERNAL_PROVIDER_UNAVAILABLE"}})
    )
    gateway = HttpExternalResearchToolGateway("http://mcp-gateway", 1.0, "research-engine")
    grant = target().authority_policy.fallback_policy.authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="LATEST_PRICE",
        status=ResearchRequirementStatus.MISSING,
        confidence=None,
        permitted_provider_ids=("YAHOO_FINANCE_MCP",),
        now=NOW,
    )
    assert gateway._client is None
    for _ in range(3):
        with pytest.raises(ExternalMcpAcquisitionError):
            await gateway.acquire_requirement(
                profile(), region="INDIA", requirement_id="LATEST_PRICE",
                authorization=grant, request_id="reuse-check",
            )
    assert route.call_count == 3
    assert gateway._client is not None
    first_client = gateway._client
    with pytest.raises(ExternalMcpAcquisitionError):
        await gateway.acquire_requirement(
            profile(), region="INDIA", requirement_id="LATEST_PRICE",
            authorization=grant, request_id="reuse-check-2",
        )
    # Same client instance reused on every call -- never recreated.
    assert gateway._client is first_client


@pytest.mark.asyncio
@respx.mock
async def test_queue_wait_for_the_semaphore_is_bounded_by_the_effective_timeout():
    """Regression for the semaphore queue-wait budget defect: live BASELINE
    telemetry showed mcp_requirement_timing durations clustering at 20-25s
    -- far above the 10s per-call gateway timeout -- while the Yahoo MCP
    pod itself answered in 0.1-6.3s. The semaphore acquisition previously
    had no timeout of its own, so a caller queued behind a busy slot could
    wait indefinitely (bounded only by whatever outer deadline eventually
    cancelled it), silently burning the caller's own effective_timeout
    budget on pure scheduling delay before a request was even dispatched.
    This proves a caller with a short effective_timeout fails fast (close
    to that timeout, not after some much longer unbounded wait) when every
    slot is occupied, and that no HTTP request is even attempted."""
    route = respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "data": result("LATEST_PRICE").model_dump(mode="json", by_alias=True),
        })
    )
    gateway = HttpExternalResearchToolGateway(
        "http://mcp-gateway", 5.0, "research-engine", max_concurrent_requests=1,
    )
    grant = target().authority_policy.fallback_policy.authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="LATEST_PRICE",
        status=ResearchRequirementStatus.MISSING,
        confidence=None,
        permitted_provider_ids=("YAHOO_FINANCE_MCP",),
        now=NOW,
    )
    # Occupy the one available slot for far longer than the second call's
    # own effective_timeout, so the second call is purely queue-blocked.
    await gateway._request_semaphore.acquire()
    asyncio.get_event_loop().call_later(2.0, gateway._request_semaphore.release)

    timings = []
    started = asyncio.get_event_loop().time()
    with pytest.raises(ExternalMcpAcquisitionError, match="DOWNSTREAM_TIMEOUT"):
        await gateway.acquire_requirement(
            profile(), region="INDIA", requirement_id="LATEST_PRICE",
            authorization=grant, request_id="queue-timeout-check",
            timeout_seconds=0.2,
            timing_hook=lambda queue_wait, http_duration: timings.append((queue_wait, http_duration)),
        )
    elapsed = asyncio.get_event_loop().time() - started

    # Failed close to the 0.2s budget, not after the full 2.0s the slot
    # was actually held -- the queue-wait is bounded by effective_timeout.
    assert elapsed < 1.0, elapsed
    # No HTTP request was ever dispatched -- the call never got a slot.
    assert route.call_count == 0
    # The timing hook reports the queue-wait timeout itself, with zero
    # HTTP duration (the request was never attempted).
    assert len(timings) == 1
    queue_wait, http_duration = timings[0]
    assert queue_wait >= 0.15, queue_wait
    assert http_duration == 0.0


@pytest.mark.asyncio
@respx.mock
async def test_http_portion_gets_only_the_remaining_budget_after_queue_wait():
    """Once a slot frees up, the HTTP request must be capped by whatever
    of the caller's effective_timeout the queue-wait has NOT already
    consumed -- not a fresh full-length timeout window. Otherwise total
    wall time (queue-wait + HTTP) could exceed the budget the caller
    computed from its own remaining ensure() window, which is exactly how
    live BASELINE calls were silently overrunning their allotted share of
    the shared 25s orchestration budget. respx's mocked transport does not
    itself enforce httpx-level request timeouts against a slow side_effect,
    so this asserts directly on the `timeout` value handed to the real
    httpx.AsyncClient.post call -- the actual mechanism being fixed --
    rather than on respx reproducing socket-level timeout behavior."""
    respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(
        return_value=httpx.Response(200, json={
            "ok": True, "data": result("LATEST_PRICE").model_dump(mode="json", by_alias=True),
        })
    )
    gateway = HttpExternalResearchToolGateway(
        "http://mcp-gateway", 5.0, "research-engine", max_concurrent_requests=1,
    )
    grant = target().authority_policy.fallback_policy.authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="LATEST_PRICE",
        status=ResearchRequirementStatus.MISSING,
        confidence=None,
        permitted_provider_ids=("YAHOO_FINANCE_MCP",),
        now=NOW,
    )
    captured_timeouts = []
    real_post = httpx.AsyncClient.post

    async def _spying_post(self, *args, **kwargs):
        captured_timeouts.append(kwargs.get("timeout"))
        return await real_post(self, *args, **kwargs)

    # Occupy the slot for 0.3s -- the caller below has a 0.4s total budget,
    # so only ~0.1s of it should remain earmarked for the HTTP call once
    # the slot frees -- never the full 0.4s again.
    await gateway._request_semaphore.acquire()
    asyncio.get_event_loop().call_later(0.3, gateway._request_semaphore.release)

    timings = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.AsyncClient, "post", _spying_post)
        await gateway.acquire_requirement(
            profile(), region="INDIA", requirement_id="LATEST_PRICE",
            authorization=grant, request_id="http-budget-check",
            timeout_seconds=0.4,
            timing_hook=lambda queue_wait, http_duration: timings.append((queue_wait, http_duration)),
        )
    assert len(captured_timeouts) == 1
    # NOT the full 0.4s budget again -- only what the ~0.3s queue-wait left.
    assert 0.0 < captured_timeouts[0] < 0.2, captured_timeouts[0]
    assert len(timings) == 1
    queue_wait, _http_duration = timings[0]
    assert queue_wait >= 0.25, queue_wait


@pytest.mark.asyncio
@respx.mock
async def test_timing_hook_reports_queue_wait_and_http_duration_separately_on_success():
    """A successful call with no contention reports near-zero queue-wait
    and the actual HTTP duration as two distinct numbers -- the fix this
    review asked for (item 2: measure/enforce HTTP timeout separately from
    queue-wait time) applies to the success path too, not just failures."""
    async def _responder(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={
            "ok": True, "data": result("LATEST_PRICE").model_dump(mode="json", by_alias=True),
        })

    respx.post("http://mcp-gateway/internal/v1/external-research/acquire").mock(side_effect=_responder)
    gateway = HttpExternalResearchToolGateway("http://mcp-gateway", 5.0, "research-engine")
    grant = target().authority_policy.fallback_policy.authorize_external_tool(
        global_instrument_id=INSTRUMENT_ID,
        requirement_id="LATEST_PRICE",
        status=ResearchRequirementStatus.MISSING,
        confidence=None,
        permitted_provider_ids=("YAHOO_FINANCE_MCP",),
        now=NOW,
    )
    timings = []
    await gateway.acquire_requirement(
        profile(), region="INDIA", requirement_id="LATEST_PRICE",
        authorization=grant, request_id="timing-split-check",
        timing_hook=lambda queue_wait, http_duration: timings.append((queue_wait, http_duration)),
    )
    assert len(timings) == 1
    queue_wait, http_duration = timings[0]
    assert queue_wait < 0.02, queue_wait
    assert http_duration >= 0.04, http_duration
