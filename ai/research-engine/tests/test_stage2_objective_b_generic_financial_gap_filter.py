"""AI INVESTMENT PLATFORM -- Stage-2 Objective B: the generic (non-NSE-gap-
fill) financial acquisition path must not bypass metric/period/basis/unit-
level gap filtering.

Root cause (confirmed by direct trace): app.financial_gap_fill.
FinancialGapState.accepts() is the exact metric+period+period_type+
reporting_basis+unit-aware filter already proven by
tests/test_nse_financial_gap_fill.py for the narrow NSE-gap-fill path
(McpFirstResearchCapabilityExecutor._execute_financial_gaps, only reachable
when _uses_nse_financial_gaps(profile) is True -- INDIA + NSE + a configured
official_financial_provider + acquire_official_financials). Outside that
narrow path -- any instrument where that precondition is not met -- the
generic per-target Yahoo loop in execute_primary called
self.persister.persist(result, profile) with NO financial_gaps filter at
all, so a whole-requirement Yahoo response's unrelated facts could enter
persistence even though only one specific metric was the genuine gap.

Fix: the generic loop now passes financial_gaps=<freshly-reloaded
FinancialGapState> to persist() whenever target.requirement_id is one of
FINANCIAL_GAP_REQUIREMENTS, reusing the exact same accepts()/merge_fact()
contract the narrow path already relies on -- not a new filter, just wiring
the existing one into the path that was bypassing it. This does not touch
FinancialGapState, accepts(), merge_fact(), or the narrow
_execute_financial_gaps path itself (those targets are pulled out of
`targets` before the generic loop runs whenever the narrow path's own gate
is satisfied, so there is no double-filtering or behavior change there).
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import CompanyResearchProfile, ProvenancedValue, SourceMode
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_readiness import ProviderAuthorityRegistry, ResearchRefreshTarget, ResearchRequirementRegistry, ResearchRequirementStatus, RuleEngineArea
from app.settings import Settings
from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor, YahooMcpNormalizedResult

INSTRUMENT_ID = uuid4()
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
PERIOD = "2026-06-30"


def _profile() -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID, company_id=uuid4(), company_name="Generic Gap Ltd",
        ticker="GAPFILL", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"YAHOO_FINANCE": "GAPFILL.NS", "NSE": "GAPFILL"},
    )


def _repository_with_nse_revenue(value: str) -> tuple[ResearchRepository, CompanyResearchProfile]:
    database = tempfile.mktemp(suffix=".db")
    repository = ResearchRepository(settings=Settings(
        research_live_enabled=True, research_persistence_enabled=True,
        research_database_backend="sqlite", research_database_name=database,
    ), persistence=SqliteResearchPersistence(database))
    profile = _profile()
    repository.profiles = [profile]
    repository._persistence.upsert_financial_fact(FinancialFact(
        FinancialFactKey(INSTRUMENT_ID, "revenue", PERIOD, "QUARTERLY", "STANDALONE"),
        ProvenancedValue(value=Decimal(value), unit="INR", as_of_date=NOW,
                         source_url="https://www.nseindia.com/quarterly-result.xml",
                         source_name="NSE", source_type="EXCHANGE_ANNOUNCEMENT",
                         retrieved_at=NOW, confidence=0.95),
        FactSourceTier.OFFICIAL_NSE, "NSE", "nse-xbrl:revenue:" + PERIOD, SourceMode.REAL,
    ))
    return repository, profile


class _NoOfficialFinancialLegacy:
    """Legacy executor WITHOUT official_financial_provider -- the exact
    precondition the generic (non-NSE-gap-fill) acquisition path requires.
    _uses_nse_financial_gaps() checks getattr(legacy, "official_financial_
    provider", None) is not None, which is False with no such attribute at
    all, forcing GROWTH_FACTS/BALANCE_SHEET_FACTS/BUSINESS_QUALITY_FACTS/
    QUARTERLY_FINANCIALS targets through the generic per-target Yahoo loop
    instead of _execute_financial_gaps.
    """

    def __init__(self) -> None:
        self.calls: list = []

    async def execute_primary(self, instrument_id, targets, **kwargs):
        from app.research_readiness_runtime import CapabilityExecutionResult
        self.calls.append(tuple(targets))
        return CapabilityExecutionResult()


class _OneShotYahooGateway:
    """Returns one whole-requirement Yahoo response containing BOTH the
    genuinely missing metric (EPS) and an unrelated/conflicting one
    (Revenue, with a different value than the already-persisted NSE fact)
    -- exactly the "Yahoo returns Revenue as well as requested EPS" shape
    the task describes. Records how many times it was actually called, to
    confirm the provider is still invoked only once at the requirement
    level (no per-metric fan-out)."""

    def __init__(self, facts) -> None:
        self.calls = 0
        self._facts = facts

    async def acquire_requirement(self, profile, *, requirement_id, **kwargs):
        self.calls += 1
        return YahooMcpNormalizedResult.model_validate({
            "adapterVersion": "YAHOO_FINANCE_MCP_ADAPTER_V1", "providerId": "YAHOO_FINANCE_MCP",
            "sourceTier": "APPROVED_EXTERNAL_TOOL", "sourceTool": "get_financials", "region": "INDIA",
            "requirementId": requirement_id, "globalInstrumentId": str(profile.instrument_id),
            "symbol": "GAPFILL.NS", "exchange": "NSE", "currency": "INR", "retrievedAt": NOW,
            "observedAt": NOW, "sourceUrl": "https://finance.yahoo.com/quote/GAPFILL.NS",
            "confidence": 0.8, "freshness": "FRESH", "financialFacts": self._facts,
            "structuredFacts": [], "marketObservations": [],
        })


def _fact(metric, value, *, period=PERIOD, kind="QUARTERLY", basis="STANDALONE", unit=None):
    return {"metric": metric, "value": value, "unit": unit or ("INR/share" if metric == "eps" else "INR"),
            "periodEnd": period, "periodType": kind, "reportingBasis": basis,
            "asOf": period + "T00:00:00Z",
            "sourceUrl": "https://finance.yahoo.com/quote/GAPFILL.NS", "confidence": 0.8}


def _target(requirement_id: str) -> ResearchRefreshTarget:
    requirement = ResearchRequirementRegistry.default().get(requirement_id)
    return ResearchRefreshTarget(
        requirement_id, requirement.rule_engine_area, ResearchRequirementStatus.MISSING,
        ProviderAuthorityRegistry.default().policy_for(requirement_id, "INDIA"), (),
    )


@pytest.mark.asyncio
async def test_generic_path_only_accepts_the_genuine_gap_and_discards_unrelated_fact() -> None:
    repository, profile = _repository_with_nse_revenue("1000")
    legacy = _NoOfficialFinancialLegacy()
    gateway = _OneShotYahooGateway([_fact("eps", "12.5"), _fact("revenue", "999")])
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    await executor.execute_primary(
        INSTRUMENT_ID, (_target("GROWTH_FACTS"),), jurisdiction="INDIA",
        correlation_id="objective-b-generic", identity_headers={},
    )

    assert gateway.calls == 1, "a whole-requirement provider call is still fine -- filtering happens on the payload"

    facts = {(f.key.metric, f.key.period_end): f for f in repository.financial_facts_for(INSTRUMENT_ID)}

    # The genuine gap (EPS) was accepted and persisted from Yahoo.
    eps = facts.get(("eps", PERIOD))
    assert eps is not None
    assert eps.source_provider == "YAHOO_FINANCE_MCP"
    assert eps.value.value == Decimal("12.5")

    # The already-authoritative NSE Revenue fact was NOT overwritten by the
    # unrelated Revenue fact Yahoo's response also happened to carry --
    # this is the exact "existing authoritative NSE Revenue must remain
    # unchanged" requirement.
    revenue = facts[("revenue", PERIOD)]
    assert revenue.source_provider == "NSE"
    assert revenue.value.value == Decimal("1000")


@pytest.mark.asyncio
async def test_generic_path_discards_unrelated_fact_when_only_one_metric_is_missing() -> None:
    # NSE already has Revenue AND Pat; only ROE is missing. Yahoo returns
    # ROE plus an unrelated/stale Revenue value -- only ROE may enter.
    repository, profile = _repository_with_nse_revenue("1000")
    repository._persistence.upsert_financial_fact(FinancialFact(
        FinancialFactKey(INSTRUMENT_ID, "pat", PERIOD, "QUARTERLY", "STANDALONE"),
        ProvenancedValue(value=Decimal("100"), unit="INR", as_of_date=NOW,
                         source_url="https://www.nseindia.com/quarterly-result.xml",
                         source_name="NSE", source_type="EXCHANGE_ANNOUNCEMENT",
                         retrieved_at=NOW, confidence=0.95),
        FactSourceTier.OFFICIAL_NSE, "NSE", "nse-xbrl:pat:" + PERIOD, SourceMode.REAL,
    ))
    legacy = _NoOfficialFinancialLegacy()
    gateway = _OneShotYahooGateway([
        _fact("roe", "18.2", unit="%"), _fact("revenue", "1"),
    ])
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    await executor.execute_primary(
        INSTRUMENT_ID, (_target("BUSINESS_QUALITY_FACTS"),), jurisdiction="INDIA",
        correlation_id="objective-b-generic-roe", identity_headers={},
    )

    facts = {(f.key.metric, f.key.period_end): f for f in repository.financial_facts_for(INSTRUMENT_ID)}
    roe = facts.get(("roe", PERIOD))
    assert roe is not None and roe.source_provider == "YAHOO_FINANCE_MCP"
    revenue = facts[("revenue", PERIOD)]
    assert revenue.source_provider == "NSE" and revenue.value.value == Decimal("1000"), (
        "the unrelated Revenue fact Yahoo's response also carried must never enter "
        "through this gap-fill path"
    )
