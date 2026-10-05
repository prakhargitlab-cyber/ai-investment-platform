"""AI INVESTMENT PLATFORM -- Stage-2 Objective C: HISTORICAL_PRICE_SERIES'
EXTERNAL_RESULT_INCOMPLETE classification and repair behavior.

Root cause (confirmed by direct trace, not re-audited): the gap-based
completeness contract in research_readiness_runtime.py's
_ensure_historical_prices (exact required coverage/range computed from
currently persisted trusted observations, only the missing tail/head window
requested) was already evidence-first and correct -- nothing there needed to
change. The actual defect was downstream, in
yahoo_mcp_acquisition.py's McpFirstResearchCapabilityExecutor.execute_primary:
under an ACTIVE deep-investigation repair budget, a generic post-success
readiness recheck (shared by every _MCP_FIRST_REQUIREMENTS member, not just
historical prices) reported a bare "EXTERNAL_RESULT_INCOMPLETE" whenever a
successful Yahoo call still left the requirement not-ready -- indistinguishable
from a transient provider/network failure. failure_taxonomy.classify_reason()
therefore always returned TECHNICAL_RETRYABLE for it, so
global_opportunity_orchestration's repair scheduler (_retryable(), gated on
failure_class == TECHNICAL_RETRYABLE) kept re-selecting the candidate for
another repair pass against an identical, already-proven-insufficient
acquisition.

Fix: exactly at the point this recheck already re-reads durable evidence
(no new logic, no loosened coverage requirement), HISTORICAL_PRICE_SERIES
now gets a distinct, qualified reason --
"EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT" -- added to
failure_taxonomy.PERMANENT_REASONS, so it classifies EVIDENCE_UNAVAILABLE
(deterministic for this acquisition) and is excluded from repair. Every
other requirement sharing this same recheck keeps the bare, still-retryable
EXTERNAL_RESULT_INCOMPLETE string -- no blanket reclassification. A genuine
transient network/provider failure (caught by the separate
ExternalMcpAcquisitionError / bare Exception branches above this recheck)
is untouched and remains retryable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.failure_taxonomy import (
    EVIDENCE_UNAVAILABLE,
    TECHNICAL_RETRYABLE,
    classify_reason,
    classify_requirement_failures,
)
from app.models import CompanyResearchProfile, MarketPriceObservation
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_readiness import ProviderAuthorityRegistry, ResearchRefreshTarget, ResearchRequirementRegistry, ResearchRequirementStatus
from app.settings import Settings
from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor, YahooMcpNormalizedResult

INSTRUMENT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _profile() -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID, company_id=uuid4(), company_name="History Gap Ltd",
        ticker="HISTGAP", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"YAHOO_FINANCE": "HISTGAP.NS", "NSE": "HISTGAP"},
    )


def _repo() -> tuple[ResearchRepository, CompanyResearchProfile]:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True), persistence=SqliteResearchPersistence())
    profile = _profile()
    repository.profiles = [profile]
    return repository, profile


def _target(requirement_id: str) -> ResearchRefreshTarget:
    requirement = ResearchRequirementRegistry.default().get(requirement_id)
    return ResearchRefreshTarget(
        requirement_id, requirement.rule_engine_area, ResearchRequirementStatus.MISSING,
        ProviderAuthorityRegistry.default().policy_for(requirement_id, "INDIA"), (),
    )


def _budget(requirement_id: str) -> RequirementAcquisitionBudget:
    async def _never_sufficient() -> bool:
        return False
    return RequirementAcquisitionBudget(
        instrument_id=INSTRUMENT_ID, requirement_id=requirement_id, as_of=NOW,
        sufficient=_never_sufficient,
    )


class _NoOpLegacy:
    """Legacy fallback that records calls but adds no further evidence --
    isolates the mcp_failures reason assigned by the McpFirst executor's own
    recheck from whatever the legacy/regional path might otherwise do."""

    def __init__(self) -> None:
        self.calls: list = []

    async def execute_primary(self, instrument_id, targets, **kwargs):
        from app.research_readiness_runtime import CapabilityExecutionResult
        self.calls.append(tuple(t.requirement_id for t in targets))
        return CapabilityExecutionResult()


class _SingleDayGateway:
    """Returns exactly ONE market observation for HISTORICAL_PRICE_SERIES --
    a technically successful Yahoo response (persist() will not raise, since
    written >= 1) that nonetheless cannot possibly satisfy
    has_year_historical_coverage()'s >= 2 observations spanning >= 365 days.
    This is the "successful provider response but insufficient required
    historical coverage" case Objective C describes."""

    def __init__(self, *, observed_at=NOW) -> None:
        self.calls = 0
        self._observed_at = observed_at

    async def acquire_requirement(self, profile, *, requirement_id, **kwargs):
        self.calls += 1
        return YahooMcpNormalizedResult.model_validate({
            "adapterVersion": "YAHOO_FINANCE_MCP_ADAPTER_V1", "providerId": "YAHOO_FINANCE_MCP",
            "sourceTier": "APPROVED_EXTERNAL_TOOL", "sourceTool": "get_history", "region": "INDIA",
            "requirementId": requirement_id, "globalInstrumentId": str(profile.instrument_id),
            "symbol": "HISTGAP.NS", "exchange": "NSE", "currency": "INR", "retrievedAt": NOW,
            "observedAt": NOW, "sourceUrl": "https://finance.yahoo.com/quote/HISTGAP.NS",
            "confidence": 0.8, "freshness": "FRESH",
            "structuredFacts": [], "financialFacts": [],
            "marketObservations": [
                {"observedAt": self._observed_at.isoformat(), "price": "101.5", "currency": "INR"},
            ],
        })


@pytest.mark.asyncio
async def test_successful_but_insufficient_historical_response_gets_qualified_reason() -> None:
    """TEST 4: provider returns a successful but insufficient range -> an
    honest, specific unresolved-coverage reason, not an opaque generic one."""
    repository, profile = _repo()
    legacy = _NoOpLegacy()
    gateway = _SingleDayGateway()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    token = _scope.set(_budget("HISTORICAL_PRICE_SERIES"))
    try:
        outcome = await executor.execute_primary(
            INSTRUMENT_ID, (_target("HISTORICAL_PRICE_SERIES"),), jurisdiction="INDIA",
            correlation_id="objective-c-history", identity_headers={},
        )
    finally:
        _scope.reset(token)

    assert gateway.calls == 1
    assert outcome.failures.get("HISTORICAL_PRICE_SERIES") == (
        "EXTERNAL_RESULT_INCOMPLETE:HISTORICAL_COVERAGE_INSUFFICIENT"
    )


@pytest.mark.asyncio
async def test_insufficient_successful_response_classifies_evidence_unavailable_not_retryable() -> None:
    """TEST 5: this qualified reason must stop repair from re-running an
    identical acquisition, via the existing TECHNICAL_RETRYABLE gate."""
    repository, profile = _repo()
    legacy = _NoOpLegacy()
    gateway = _SingleDayGateway()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    token = _scope.set(_budget("HISTORICAL_PRICE_SERIES"))
    try:
        outcome = await executor.execute_primary(
            INSTRUMENT_ID, (_target("HISTORICAL_PRICE_SERIES"),), jurisdiction="INDIA",
            correlation_id="objective-c-history-classify", identity_headers={},
        )
    finally:
        _scope.reset(token)

    reason = outcome.failures["HISTORICAL_PRICE_SERIES"]
    assert classify_reason(reason) == EVIDENCE_UNAVAILABLE
    assert classify_requirement_failures(
        {"HISTORICAL_PRICE_SERIES": reason}, ("HISTORICAL_PRICE_SERIES",)
    ) == EVIDENCE_UNAVAILABLE


def test_bare_external_result_incomplete_remains_technical_retryable_everywhere_else() -> None:
    """Regression guard: the new qualified string is scoped to
    HISTORICAL_PRICE_SERIES only. The bare EXTERNAL_RESULT_INCOMPLETE string
    (shared by every other requirement using the same generic recheck, and
    by HISTORICAL_PRICE_SERIES itself when nothing persistable came back at
    all) must stay TECHNICAL_RETRYABLE -- no blanket reclassification."""
    assert classify_reason("EXTERNAL_RESULT_INCOMPLETE") == TECHNICAL_RETRYABLE
    assert classify_requirement_failures(
        {"QUARTERLY_FINANCIALS": "EXTERNAL_RESULT_INCOMPLETE"}, ("QUARTERLY_FINANCIALS",)
    ) == TECHNICAL_RETRYABLE
    assert classify_requirement_failures(
        {"SHAREHOLDING": "EXTERNAL_RESULT_INCOMPLETE"}, ("SHAREHOLDING",)
    ) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_transient_provider_failure_for_historical_prices_remains_retryable() -> None:
    """TEST 6: a genuine transient/network failure must remain retryable --
    the qualified reason is only ever assigned after a provably successful,
    durably-insufficient response, never for a provider error."""
    from app.yahoo_mcp_acquisition import ExternalMcpAcquisitionError

    class _FlakyGateway:
        calls = 0

        async def acquire_requirement(self, profile, **kwargs):
            _FlakyGateway.calls += 1
            raise ExternalMcpAcquisitionError("EXTERNAL_PROVIDER_UNAVAILABLE")

    repository, profile = _repo()
    legacy = _NoOpLegacy()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, _FlakyGateway(), enabled=True)

    token = _scope.set(_budget("HISTORICAL_PRICE_SERIES"))
    try:
        outcome = await executor.execute_primary(
            INSTRUMENT_ID, (_target("HISTORICAL_PRICE_SERIES"),), jurisdiction="INDIA",
            correlation_id="objective-c-transient", identity_headers={},
        )
    finally:
        _scope.reset(token)

    reason = outcome.failures["HISTORICAL_PRICE_SERIES"]
    assert reason == "EXTERNAL_PROVIDER_UNAVAILABLE"
    assert classify_reason(reason) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_provider_fills_full_range_is_ready_no_qualified_failure() -> None:
    """TEST 3 (provider fills range -> READY): readiness's own
    HISTORICAL_PRICE_SERIES contract requires FIFTY_OBSERVATION_TECHNICAL_
    BASIS (>= 50 observations) as well as a >= 365 day span (see
    research_readiness.py's HISTORICAL_PRICE_SERIES ResearchRequirement and
    research_readiness_runtime.py's evidence builder) -- a full, genuinely
    sufficient Yahoo response satisfying that real contract must not be
    reported as any kind of failure."""
    repository, profile = _repo()
    legacy = _NoOpLegacy()
    # Freshness uses the real wall clock (ResearchReadinessService.assess()
    # defaults `now` to datetime.now()), so the latest observation must be
    # recent enough (DAILY_INCREMENTAL, 36h) relative to whenever this test
    # actually runs -- not relative to the fixed NOW used elsewhere in this
    # file for the insufficient-coverage scenarios.
    real_now = datetime.now(timezone.utc)
    observations = [
        {"observedAt": (real_now - timedelta(hours=1) - timedelta(days=i * 7)).isoformat(),
         "price": str(90 + i), "currency": "INR"}
        for i in range(60)
    ]

    class _FullRangeGateway:
        calls = 0

        async def acquire_requirement(self, prof, *, requirement_id, **kwargs):
            _FullRangeGateway.calls += 1
            return YahooMcpNormalizedResult.model_validate({
                "adapterVersion": "YAHOO_FINANCE_MCP_ADAPTER_V1", "providerId": "YAHOO_FINANCE_MCP",
                "sourceTier": "APPROVED_EXTERNAL_TOOL", "sourceTool": "get_history", "region": "INDIA",
                "requirementId": requirement_id, "globalInstrumentId": str(prof.instrument_id),
                "symbol": "HISTGAP.NS", "exchange": "NSE", "currency": "INR", "retrievedAt": NOW,
                "observedAt": NOW, "sourceUrl": "https://finance.yahoo.com/quote/HISTGAP.NS",
                "confidence": 0.8, "freshness": "FRESH",
                "structuredFacts": [], "financialFacts": [],
                "marketObservations": observations,
            })

    executor = McpFirstResearchCapabilityExecutor(legacy, repository, _FullRangeGateway(), enabled=True)
    token = _scope.set(_budget("HISTORICAL_PRICE_SERIES"))
    try:
        outcome = await executor.execute_primary(
            INSTRUMENT_ID, (_target("HISTORICAL_PRICE_SERIES"),), jurisdiction="INDIA",
            correlation_id="objective-c-full-range", identity_headers={},
        )
    finally:
        _scope.reset(token)

    assert "HISTORICAL_PRICE_SERIES" not in outcome.failures
