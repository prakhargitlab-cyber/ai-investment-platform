from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.historical_market_data import YahooHistoricalPriceProvider
from app.international_fundamentals import EodhdFundamentalProvider, SecEdgarFundamentalProvider
from app.portfolio_context import PortfolioContext, PortfolioHoldingContext
from app.research_readiness import (
    DurableResearchSnapshot,
    FreshnessMode,
    FreshnessPolicyRegistry,
    ProviderAuthorityRegistry,
    ResearchCoverageService,
    ResearchEvidence,
    ResearchReadinessService,
    ResearchRefreshPlanner,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
    ResearchSourceTier,
    ResearchSupportedAction,
    RuleEngineArea,
)
from app.structured_market import YahooFinanceProvider


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
GLOBAL_INSTRUMENT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class RecordingDataSource:
    def __init__(self, snapshot: DurableResearchSnapshot) -> None:
        self.snapshot = snapshot
        self.loads: list[tuple[UUID, tuple[str, ...]]] = []
        self.provider_calls = 0
        self.holding_mutations = 0

    def load_by_global_instrument_id(self, global_instrument_id, requirements):
        self.loads.append(
            (global_instrument_id, tuple(requirement.requirement_id for requirement in requirements))
        )
        return self.snapshot


def evidence(
    requirement_id: str,
    *,
    age: timedelta = timedelta(hours=1),
    evidence_id: str | None = None,
    source: str = "NSE",
    source_tier: ResearchSourceTier = ResearchSourceTier.OFFICIAL,
    complete: bool = True,
    fact_key: str | None = None,
    value: str | None = None,
    unresolved: bool = False,
) -> ResearchEvidence:
    as_of = NOW - age
    return ResearchEvidence(
        evidence_id=evidence_id or f"{requirement_id}-{uuid4()}",
        requirement_id=requirement_id,
        source=source,
        source_tier=source_tier,
        retrieved_at=as_of,
        as_of=as_of,
        published_at=as_of,
        event_date=as_of,
        complete=complete,
        fact_key=fact_key,
        value_fingerprint=value,
        unresolved=unresolved,
    )


def complete_snapshot(
    *,
    overrides: dict[str, tuple[ResearchEvidence, ...]] | None = None,
    omit: set[str] | None = None,
    supported: frozenset[str] | None = None,
    refreshing: frozenset[str] = frozenset(),
    failures: dict[str, str] | None = None,
) -> DurableResearchSnapshot:
    registry = ResearchRequirementRegistry.default()
    values = {
        requirement.requirement_id: (
            evidence(
                requirement.requirement_id,
                age=timedelta(minutes=5)
                if requirement.requirement_id == "LATEST_PRICE"
                else timedelta(hours=1),
            ),
        )
        for requirement in registry.requirements
    }
    for requirement_id in omit or set():
        values.pop(requirement_id, None)
    values.update(overrides or {})
    return DurableResearchSnapshot(
        GLOBAL_INSTRUMENT_ID,
        values,
        supported_requirement_ids=supported,
        refreshing_requirement_ids=refreshing,
        failure_reasons=failures or {},
    )


def assess(snapshot: DurableResearchSnapshot):
    data_source = RecordingDataSource(snapshot)
    readiness = ResearchReadinessService(data_source).assess(
        GLOBAL_INSTRUMENT_ID, jurisdiction="INDIA", now=NOW
    )
    return data_source, readiness, ResearchRefreshPlanner().plan(readiness, jurisdiction="INDIA")


def target_ids(plan) -> set[str]:
    return {target.requirement_id for target in plan.targets}


def test_fresh_required_data_produces_no_provider_candidate() -> None:
    data_source, readiness, plan = assess(complete_snapshot())

    for target in plan.targets:
        data_source.provider_calls += 1

    assert readiness.mandatory_ready is True
    assert plan.targets == ()
    assert data_source.provider_calls == 0
    assert len(data_source.loads) == 1


def test_stale_required_data_is_the_only_targeted_refresh() -> None:
    stale = evidence("LATEST_PRICE", age=timedelta(minutes=16))
    _, readiness, plan = assess(complete_snapshot(overrides={"LATEST_PRICE": (stale,)}))

    assert readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_STALE
    assert target_ids(plan) == {"LATEST_PRICE"}


def test_missing_required_data_becomes_a_targeted_fetch_candidate() -> None:
    _, readiness, plan = assess(complete_snapshot(omit={"QUARTERLY_FINANCIALS"}))

    result = readiness.for_requirement("QUARTERLY_FINANCIALS")
    assert result.status == ResearchRequirementStatus.MISSING
    assert result.missing_reason == "NO_DURABLE_EVIDENCE"
    assert target_ids(plan) == {"QUARTERLY_FINANCIALS"}


def test_incomplete_required_data_is_partial_and_targeted() -> None:
    incomplete = evidence("QUARTERLY_FINANCIALS", complete=False)
    _, readiness, plan = assess(
        complete_snapshot(overrides={"QUARTERLY_FINANCIALS": (incomplete,)})
    )

    result = readiness.for_requirement("QUARTERLY_FINANCIALS")
    assert result.status == ResearchRequirementStatus.PARTIAL
    assert result.missing_reason == "INSUFFICIENT_COMPLETE_EVIDENCE"
    assert target_ids(plan) == {"QUARTERLY_FINANCIALS"}


def test_unrelated_fresh_category_is_not_refreshed() -> None:
    _, readiness, plan = assess(complete_snapshot(omit={"SECTOR_MACRO"}))

    assert readiness.for_requirement("HISTORICAL_PRICE_SERIES").status == ResearchRequirementStatus.READY_FRESH
    assert target_ids(plan) == {"SECTOR_MACRO"}


def test_equal_authority_disagreement_is_conflicting() -> None:
    conflicting = (
        evidence(
            "VALUATION_INPUTS",
            evidence_id="valuation-a",
            fact_key="trailing-pe",
            value="18.2",
        ),
        evidence(
            "VALUATION_INPUTS",
            evidence_id="valuation-b",
            fact_key="trailing-pe",
            value="21.7",
        ),
    )
    _, readiness, plan = assess(complete_snapshot(overrides={"VALUATION_INPUTS": conflicting}))

    result = readiness.for_requirement("VALUATION_INPUTS")
    assert result.status == ResearchRequirementStatus.CONFLICTING
    assert result.conflict_reason == "EQUAL_AUTHORITY_CONFLICT:trailing-pe:valuation-a,valuation-b"
    assert target_ids(plan) == {"VALUATION_INPUTS"}


def test_unsupported_category_is_not_scheduled() -> None:
    requirement_ids = {
        item.requirement_id for item in ResearchRequirementRegistry.default().requirements
    }
    supported = frozenset(requirement_ids - {"SECTOR_MACRO"})
    _, readiness, plan = assess(complete_snapshot(supported=supported))

    result = readiness.for_requirement("SECTOR_MACRO")
    assert result.status == ResearchRequirementStatus.UNSUPPORTED
    assert ResearchSupportedAction.UPLOAD_EVIDENCE in result.supported_actions
    assert ResearchSupportedAction.FIND_DATA not in result.supported_actions
    assert "SECTOR_MACRO" not in target_ids(plan)


def test_mandatory_and_optional_distinction_is_retained_by_planner() -> None:
    _, readiness, plan = assess(complete_snapshot(omit={"LATEST_PRICE", "SHAREHOLDING"}))

    assert readiness.for_requirement("LATEST_PRICE").mandatory is True
    assert readiness.for_requirement("SHAREHOLDING").mandatory is False
    assert readiness.for_requirement("SHAREHOLDING").status == ResearchRequirementStatus.MISSING
    assert target_ids(plan) == {"LATEST_PRICE"}


def test_current_news_older_than_thirty_days_is_excluded_but_not_deleted() -> None:
    old_news = evidence("CURRENT_NEWS", age=timedelta(days=31), evidence_id="old-news")
    boundary_news = evidence(
        "CURRENT_NEWS", age=timedelta(days=30), evidence_id="boundary-news"
    )
    snapshot = complete_snapshot(overrides={"CURRENT_NEWS": (old_news,)})
    _, readiness, plan = assess(snapshot)
    registry = ResearchRequirementRegistry.default()
    requirement = registry.get("CURRENT_NEWS")
    policy = FreshnessPolicyRegistry.default().get("CURRENT_NEWS")

    result = readiness.for_requirement("CURRENT_NEWS")
    assert result.status == ResearchRequirementStatus.MISSING
    assert result.missing_reason == "NO_EVIDENCE_IN_CURRENT_NEWS_WINDOW"
    assert result.evidence_ids == ()
    assert ResearchCoverageService().score_input_evidence(requirement, (old_news,), policy, NOW) == ()
    assert ResearchCoverageService().score_input_evidence(
        requirement, (boundary_news,), policy, NOW
    ) == (boundary_news,)
    assert snapshot.evidence_for("CURRENT_NEWS") == (old_news,)
    assert target_ids(plan) == {"CURRENT_NEWS"}


def test_old_unresolved_governance_evidence_remains_queryable_and_ready() -> None:
    old_issue = evidence(
        "GOVERNANCE_HISTORY",
        age=timedelta(days=4 * 365),
        evidence_id="unresolved-litigation",
        unresolved=True,
    )
    snapshot = complete_snapshot(overrides={"GOVERNANCE_HISTORY": (old_issue,)})
    data_source, readiness, plan = assess(snapshot)

    assert readiness.for_requirement("GOVERNANCE_HISTORY").status == ResearchRequirementStatus.READY_FRESH
    assert ResearchCoverageService().governance_history(data_source.snapshot) == (old_issue,)
    assert "GOVERNANCE_HISTORY" not in target_ids(plan)


def test_canonical_global_instrument_id_is_required_before_any_db_read() -> None:
    data_source = RecordingDataSource(complete_snapshot())
    service = ResearchReadinessService(data_source)

    with pytest.raises(ValueError, match="canonical globalInstrumentId is required"):
        service.assess(None, now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="canonical globalInstrumentId is required"):
        service.assess(UUID(int=0), now=NOW)
    assert data_source.loads == []


def test_readiness_and_portfolio_projection_do_not_mutate_holdings() -> None:
    holding = PortfolioHoldingContext(
        global_instrument_id=GLOBAL_INSTRUMENT_ID,
        quantity=Decimal("15"),
        average_cost=Decimal("101.50"),
        market_value=Decimal("1800"),
        unrealized_pnl=Decimal("277.50"),
    )
    context = PortfolioContext(
        context_id="context-1",
        owner_session_id="session-1",
        portfolio_id=uuid4(),
        holdings=(holding,),
        expires_at=NOW + timedelta(hours=1),
    )
    data_source, _, _ = assess(complete_snapshot())

    assert context.global_instrument_ids == (GLOBAL_INSTRUMENT_ID,)
    assert context.holdings == (holding,)
    assert data_source.holding_mutations == 0
    with pytest.raises(FrozenInstanceError):
        context.holdings[0].quantity = Decimal("0")  # type: ignore[misc]


def test_market_data_requirements_keep_distinct_freshness_semantics() -> None:
    policies = FreshnessPolicyRegistry.default()
    latest = policies.get("LATEST_PRICE")
    historical = policies.get("HISTORICAL_PRICE_SERIES")
    _, readiness, plan = assess(complete_snapshot())

    assert latest.mode == FreshnessMode.MARKET_SESSION_AWARE
    assert historical.mode == FreshnessMode.DAILY_INCREMENTAL
    assert latest.maximum_age != historical.maximum_age
    assert readiness.for_requirement("LATEST_PRICE").status == ResearchRequirementStatus.READY_FRESH
    assert readiness.for_requirement("HISTORICAL_PRICE_SERIES").status == ResearchRequirementStatus.READY_FRESH
    assert not {"LATEST_PRICE", "HISTORICAL_PRICE_SERIES"} & target_ids(plan)


def test_existing_provider_adapter_contracts_remain_available() -> None:
    assert YahooFinanceProvider.provider_name == "YAHOO_FINANCE"
    assert YahooHistoricalPriceProvider.provider_name == "YAHOO_FINANCE"
    assert SecEdgarFundamentalProvider.provider_name == "SEC_EDGAR"
    assert EodhdFundamentalProvider.provider_name == "EODHD"


def test_requirement_contract_exposes_all_readiness_fields_and_area_weights() -> None:
    _, readiness, _ = assess(complete_snapshot())
    result = readiness.for_requirement("LATEST_PRICE")
    registry = ResearchRequirementRegistry.default()

    assert result.requirement_id == "LATEST_PRICE"
    assert result.rule_engine_area == RuleEngineArea.PRICE_TECHNICAL
    assert result.source == "NSE"
    assert result.source_tier == ResearchSourceTier.OFFICIAL
    assert result.as_of == NOW - timedelta(minutes=5)
    assert result.retrieved_at == NOW - timedelta(minutes=5)
    assert result.age == timedelta(minutes=5)
    assert result.freshness_policy.policy_id == "LATEST_PRICE"
    assert len(result.evidence_ids) == 1
    assert result.missing_reason is None
    assert result.conflict_reason is None
    assert result.supported_actions == ()
    assert set(registry.area_weights) == set(RuleEngineArea)
    assert sum(registry.area_weights.values(), Decimal("0")) == Decimal("1.00")


def test_uploaded_evidence_does_not_outrank_official_evidence() -> None:
    official = evidence(
        "QUARTERLY_FINANCIALS",
        evidence_id="official",
        source="NSE",
        source_tier=ResearchSourceTier.OFFICIAL,
        fact_key="revenue-2026-q2",
        value="100",
    )
    upload = evidence(
        "QUARTERLY_FINANCIALS",
        evidence_id="upload",
        source="USER_UPLOAD",
        source_tier=ResearchSourceTier.USER_UPLOAD,
        fact_key="revenue-2026-q2",
        value="999",
    )
    _, readiness, _ = assess(
        complete_snapshot(overrides={"QUARTERLY_FINANCIALS": (upload, official)})
    )

    result = readiness.for_requirement("QUARTERLY_FINANCIALS")
    assert result.status == ResearchRequirementStatus.READY_FRESH
    assert result.source == "NSE"
    assert result.conflict_reason is None


def test_external_tool_fallback_is_policy_controlled() -> None:
    policy = ProviderAuthorityRegistry.default().policy_for("QUARTERLY_FINANCIALS", "USA")

    assert policy.fallback_policy.permits(ResearchRequirementStatus.MISSING)
    assert policy.fallback_policy.permits(ResearchRequirementStatus.CONFLICTING)
    assert policy.fallback_policy.permits(ResearchRequirementStatus.READY_STALE, confidence=0.4)
    assert not policy.fallback_policy.permits(ResearchRequirementStatus.READY_STALE, confidence=0.95)


def test_provider_authority_is_fact_specific_and_unregistered_facts_fail_closed() -> None:
    registry = ProviderAuthorityRegistry.default()
    order_book = registry.policy_for("ORDER_BOOK_CAPEX_GUIDANCE", "GLOBAL")
    sector_macro = registry.policy_for("SECTOR_MACRO", "GLOBAL")

    assert {
        registry.policy_for(requirement.requirement_id, "GLOBAL").requirement_id
        for requirement in ResearchRequirementRegistry.default().requirements
    } == {
        requirement.requirement_id
        for requirement in ResearchRequirementRegistry.default().requirements
    }
    assert tuple(
        item.source
        for item in registry.policy_for("QUARTERLY_FINANCIALS", "INDIA").authorities[:2]
    ) == ("NSE", "COMPANY_FILING")
    assert registry.policy_for("QUARTERLY_FINANCIALS", "USA").authorities[0].source == "SEC_EDGAR"
    assert tuple(
        item.source
        for item in registry.policy_for("QUARTERLY_FINANCIALS", "EUROPE").authorities[:3]
    ) == ("REGULATORY_FILING", "COMPANY_FILING", "EODHD")
    assert registry.policy_for("LATEST_PRICE", "INDIA").authorities[0].source == (
        "CONFIGURED_MARKET_DATA"
    )
    assert registry.policy_for("CANONICAL_IDENTITY", "GLOBAL").authorities[0].source == (
        "ISIN_OR_PERMANENT_ID"
    )
    assert order_book.authorities != sector_macro.authorities
    with pytest.raises(KeyError, match="No fact-specific provider authority policy"):
        registry.policy_for("UNREGISTERED_FACT", "GLOBAL")


def test_refreshing_and_failed_states_are_representable() -> None:
    refreshing_snapshot = complete_snapshot(
        omit={"SECTOR_MACRO"}, refreshing=frozenset({"SECTOR_MACRO"})
    )
    _, refreshing, refreshing_plan = assess(refreshing_snapshot)
    assert refreshing.for_requirement("SECTOR_MACRO").status == ResearchRequirementStatus.REFRESHING
    assert "SECTOR_MACRO" not in target_ids(refreshing_plan)

    failed_snapshot = complete_snapshot(
        omit={"SECTOR_MACRO"}, failures={"SECTOR_MACRO": "PROVIDER_TIMEOUT"}
    )
    _, failed, failed_plan = assess(failed_snapshot)
    assert failed.for_requirement("SECTOR_MACRO").status == ResearchRequirementStatus.FAILED
    assert failed.for_requirement("SECTOR_MACRO").missing_reason == "PROVIDER_TIMEOUT"
    assert target_ids(failed_plan) == {"SECTOR_MACRO"}
