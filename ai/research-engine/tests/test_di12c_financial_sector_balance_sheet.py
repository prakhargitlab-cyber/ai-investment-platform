"""DI-12C FIX 2 regression tests -- financial-sector balance-sheet fact pipeline.

Provider-free: no real providers, no real network.  The in-memory
``SqliteResearchPersistence`` is used only as a durable store double.

FIX 2 root cause: ``StockRuleEngine._balance_sheet`` financial branch queries
``capital_adequacy`` / ``gross_npa`` / ``net_npa`` from the persisted
``FinancialFact`` table via ``_latest_fact`` / ``_fact_series``.  The legacy
text parser recovered those ratios onto an in-memory ``QuarterlyResult`` but
never persisted them, so the scorer observed nothing -> ``UNSCORABLE`` ->
``BALANCE_SHEET_FACTS`` blocking even with DEBT/EQUITY/CASH present.

Correction: ``repository._official_financial_fact_candidates`` now calls
``official_financial_sector_ratio_facts(document)`` which persists the three
canonical ratio metrics as OFFICIAL_NSE ``FinancialFact`` rows on the EXISTING
official fact pipeline.  These tests pin that contract.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import (
    CompanyResearchProfile,
    SourceClassification,
    SourceMode,
    SourceType,
    ResearchDocument,
    DocumentStatus,
    DocumentType,
    ProvenancedValue,
    ReliabilityLevel,
)
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_readiness import (
    ResearchDataConfidence,
    ResearchReadinessResult,
    ResearchRequirementReadiness,
    ResearchRequirementStatus,
    ResearchSourceTier,
)
from app.settings import Settings
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.stock_rule_engine import (
    AreaScoreStatus,
    RuleEngineArea,
    StockRuleEngineInput,
    StockRuleEngineV1,
)
from app.settings import Settings
from app.structured_research import (
    _FINANCIAL_SECTOR_RATIO_METRICS,
    official_financial_sector_ratio_facts,
)


INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000000042")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000000043")
NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
QUARTER_END_ISO = "2026-06-30"

HFC_TEXT = (
    "Statement of Unaudited Financial Results for the quarter ended 30 June 2026 "
    "(Rs. in Crore) Housing Finance Company Particulars Quarter ended "
    "Revenue from operations 5000.00 Net Profit 900.00 "
    "Capital Adequacy Ratio 14.20% Gross NPA 1.05% Net NPA 0.45% "
    "Total Assets 125000.00 Total Borrowings 95000.00"
)

HFC_TEXT_NO_RATIOS = (
    "Statement of Unaudited Financial Results for the quarter ended 30 June 2026 "
    "(Rs. in Crore) Housing Finance Company Particulars "
    "Revenue from operations 5000.00 Net Profit 900.00 "
    "Total Assets 125000.00"
)

INDUSTRIAL_TEXT = (
    "Extract of Statement of Unaudited Financial Results for the quarter ended "
    "30 June 2026 Particulars Revenue 8,261.11 Net Profit 1,927.21 "
    "Borrowings 142851.50 Capital Adequacy Ratio 14.20%"
)


def _document(text: str, *, document_id=None) -> ResearchDocument:
    return ResearchDocument(
        canonical_url="https://nsearchives.nseindia.com/corporate/result.pdf",
        original_url="https://nsearchives.nseindia.com/corporate/result.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements",
        publisher="NSE",
        content_type="application/pdf",
        document_type=DocumentType.PDF_REFERENCE,
        normalized_text=text,
        content_hash="fixture-hfc",
        status=DocumentStatus.PROCESSED,
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        instrument_id=INSTRUMENT_ID,
        company_id=COMPANY_ID,
        published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        document_id=document_id or UUID("00000000-0000-0000-0000-000000000099"),
        discovery_provider="NSE_OFFICIAL_API",
        entity_resolution_confidence=0.99,
    )


def _repo() -> ResearchRepository:
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(":memory:"),
    )
    repo.profiles = [SimpleNamespace(
        instrument_id=INSTRUMENT_ID, company_name="HFC Example", company_id=COMPANY_ID,
        jurisdiction="INDIA", country="IN", exchange="NSE", mic="XNSE", currency="INR",
        industry="Housing Finance Company",
    )]
    return repo


def _profile() -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID, company_id=COMPANY_ID,
        company_name="HFC Example", isin="INE000A01010", ticker="HFC",
        exchange="NSE", mic="XNSE", country="IN", currency="INR",
        industry="Housing Finance Company",
    )


def _financial_inputs(*, facts=(), sector_text: str = "Housing Finance Company"):
    """A minimal StockRuleEngineInput scoped to the balance-sheet scorer path."""
    return StockRuleEngineInput(
        profile=_profile(),
        readiness=_readiness_all_ready(),
        financial_facts=tuple(facts),
        structured_snapshots=(),
        market_prices=(),
        events=(),
        shareholding=(),
        canonical_metadata={"canonicalSector": sector_text, "updatedAt": NOW.isoformat()},
        evaluated_at=NOW,
    )


def _readiness_all_ready() -> ResearchReadinessResult:
    from app.research_readiness import (
        FreshnessPolicyRegistry,
        ResearchDataConfidence,
        ResearchRequirementRegistry,
        ResearchSourceTier,
    )
    registry = ResearchRequirementRegistry.default()
    freshness = FreshnessPolicyRegistry.default()
    items = []
    for requirement in registry.requirements:
        items.append(ResearchRequirementReadiness(
            requirement_id=requirement.requirement_id,
            rule_engine_area=requirement.rule_engine_area,
            mandatory=requirement.mandatory,
            status=ResearchRequirementStatus.READY_FRESH,
            source="NSE",
            source_tier=ResearchSourceTier.OFFICIAL,
            as_of=NOW,
            retrieved_at=NOW,
            age=0.0,
            freshness_policy=freshness.get(requirement.freshness_policy_id),
            evidence_ids=(f"evidence:{requirement.requirement_id}",),
            missing_reason=None,
            conflict_reason=None,
            supported_actions=(),
            importance=requirement.importance,
            source_url="https://www.nseindia.com/filing.pdf",
            covered_input_ids=tuple(item.input_id for item in requirement.inputs),
            missing_input_ids=(),
            coverage_pct=100,
            critical_coverage_pct=100,
        ))
    return ResearchReadinessResult(
        global_instrument_id=INSTRUMENT_ID,
        requirements=tuple(items),
        generated_at=NOW,
        overall_status=ResearchRequirementStatus.READY_FRESH,
        overall_completeness_pct=100,
        critical_completeness_pct=100,
        confidence=ResearchDataConfidence.HIGH,
        confidence_pct=100,
    )


def _sector_ratio_facts() -> tuple[FinancialFact, ...]:
    """Three canonical OFFICIAL_NSE ratio facts at the June-2026 quarter."""
    published_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    def _fact(metric, value):
        return FinancialFact(
            FinancialFactKey(INSTRUMENT_ID, metric, QUARTER_END_ISO, "QUARTERLY", "CONSOLIDATED"),
            ProvenancedValue(
                value=Decimal(value), unit="PERCENT", as_of_date=published_at, period=None,
                source_url="https://nsearchives.nseindia.com/corporate/result.pdf",
                source_name="NSE corporate announcements",
                source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
                published_at=published_at, retrieved_at=published_at, confidence=0.82,
            ),
            FactSourceTier.OFFICIAL_NSE, "NSE",
            f"doc:official:{metric}", SourceMode.REAL,
        )
    return (_fact("capital_adequacy", "14.20"), _fact("gross_npa", "1.05"), _fact("net_npa", "0.45"))


# --------------------------------------------------------------------------- #
# TEST 4 -- official_financial_sector_ratio_facts yields canonical fact rows
# --------------------------------------------------------------------------- #
def test_fix2_official_extraction_yields_canonical_capital_gross_npa_net_npa_rows():
    document = _document(HFC_TEXT)
    facts = official_financial_sector_ratio_facts(document)
    metrics = {fact.key.metric for fact in facts}
    assert metrics == {"capital_adequacy", "gross_npa", "net_npa"}
    for fact in facts:
        assert fact.source_tier == FactSourceTier.OFFICIAL_NSE
        assert fact.source_mode == SourceMode.REAL
        assert fact.value.unit == "PERCENT"
        assert fact.key.period_type == "QUARTERLY"
        assert fact.key.period_end == QUARTER_END_ISO
    capital = next(f for f in facts if f.key.metric == "capital_adequacy")
    gross = next(f for f in facts if f.key.metric == "gross_npa")
    net = next(f for f in facts if f.key.metric == "net_npa")
    assert capital.value.value == Decimal("14.20")
    assert gross.value.value == Decimal("1.05")
    assert net.value.value == Decimal("0.45")
    assert capital.key.reporting_basis in (None, "", "CONSOLIDATED")


def test_fix2_no_fabricated_zero_when_no_ratio_text_present():
    document = _document(HFC_TEXT_NO_RATIOS)
    facts = official_financial_sector_ratio_facts(document)
    metrics = {fact.key.metric for fact in facts}
    assert metrics == set()
    # No zero-valued ratio rows are fabricated.
    assert all(not _missing_value(fact.value.value) for fact in facts)


def _missing_value(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# --------------------------------------------------------------------------- #
# TEST 5 -- persisted via the official fact pipeline -> repository read path
# --------------------------------------------------------------------------- #
def test_fix2_persisted_sector_ratios_flow_through_repository_read():
    repo = _repo()
    document = _document(HFC_TEXT)
    for fact in official_financial_sector_ratio_facts(document):
        repo._persistence.upsert_financial_fact(fact)
    loaded = repo.financial_facts_for(INSTRUMENT_ID)
    metrics = {fact.key.metric for fact in loaded}
    assert {"capital_adequacy", "gross_npa", "net_npa"}.issubset(metrics)
    for metric in ("capital_adequacy", "gross_npa", "net_npa"):
        match = [f for f in loaded if f.key.metric == metric]
        assert len(match) == 1
        assert match[0].source_tier == FactSourceTier.OFFICIAL_NSE
        assert match[0].value.unit == "PERCENT"
        assert match[0].key.period_end == QUARTER_END_ISO


# --------------------------------------------------------------------------- #
# TEST 6 -- scorer _balance_sheet financial branch is scorable (not UNSCORABLE)
# --------------------------------------------------------------------------- #
def test_fix2_balance_sheet_financial_branch_scores_with_persisted_ratios():
    result = StockRuleEngineV1()._balance_sheet(_financial_inputs(facts=_sector_ratio_facts()))
    assert result.area == RuleEngineArea.BALANCE_SHEET
    assert result.applicable is True
    assert result.status != AreaScoreStatus.UNSCORABLE
    assert result.status == AreaScoreStatus.READY_FRESH
    metric_names = {item.metric for item in result.metrics}
    assert {"CAPITAL_ADEQUACY", "GROSS_NPA", "NET_NPA"}.issubset(metric_names)
    assert result.raw_score is not None


# --------------------------------------------------------------------------- #
# TEST 7 -- missing ratios -> still UNSCORABLE financial branch (truthful)
# --------------------------------------------------------------------------- #
def test_fix2_missing_sector_ratios_keep_balance_sheet_truthful_unscorable():
    repo = _repo()
    document = _document(HFC_TEXT_NO_RATIOS)  # no ratio text -> no ratio facts
    facts = official_financial_sector_ratio_facts(document)
    assert facts == []
    # With NO financial-sector ratio facts the financial branch yields no
    # metrics and remains UNSCORABLE (truthful: evidence is genuinely absent).
    result = StockRuleEngineV1()._balance_sheet(_financial_inputs(facts=()))
    assert result.area == RuleEngineArea.BALANCE_SHEET
    assert result.applicable is True
    assert result.status == AreaScoreStatus.UNSCORABLE
    assert not any(item.metric in {"CAPITAL_ADEQUACY", "GROSS_NPA", "NET_NPA"} for item in result.metrics)
    assert "FINANCIAL_SECTOR_CAPITAL_OR_ASSET_QUALITY" in result.missing_inputs


# --------------------------------------------------------------------------- #
# TEST 8 -- industrial (non-financial) balance-sheet path is unchanged
# --------------------------------------------------------------------------- #
def test_fix2_industrial_balance_sheet_branch_ignores_sector_ratios():
    """A non-financial issuer must keep the DEBT/EQUITY/CASH/INTEREST-COVERAGE
    industrial branch; persisted capital_adequacy/gross_npa/net_npa facts must
    NOT cross over into the industrial path even if such a row existed."""
    industrial_facts = (
        FinancialFact(
            FinancialFactKey(INSTRUMENT_ID, "debt_or_borrowings", QUARTER_END_ISO, "ANNUAL", "CONSOLIDATED"),
            ProvenancedValue(
                value=Decimal("142851.50"), unit="INR", as_of_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
                source_url="https://nse/industrial", source_name="NSE",
                source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
                published_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
                retrieved_at=datetime(2026, 6, 30, tzinfo=timezone.utc), confidence=0.95,
            ),
            FactSourceTier.OFFICIAL_NSE, "NSE",
            "doc:industrial:debt", SourceMode.REAL,
        ),
        FinancialFact(
            FinancialFactKey(INSTRUMENT_ID, "equity", QUARTER_END_ISO, "ANNUAL", "CONSOLIDATED"),
            ProvenancedValue(
                value=Decimal("50000"), unit="INR", as_of_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
                source_url="https://nse/industrial", source_name="NSE",
                source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
                published_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
                retrieved_at=datetime(2026, 6, 30, tzinfo=timezone.utc), confidence=0.95,
            ),
            FactSourceTier.OFFICIAL_NSE, "NSE", "doc:industrial:eqity", SourceMode.REAL,
        ),
        FinancialFact(
            FinancialFactKey(INSTRUMENT_ID, "cash_and_cash_equivalents", QUARTER_END_ISO, "ANNUAL", "CONSOLIDATED"),
            ProvenancedValue(
                value=Decimal("4000"), unit="INR", as_of_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
                source_url="https://nse/industrial", source_name="NSE",
                source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
                published_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
                retrieved_at=datetime(2026, 6, 30, tzinfo=timezone.utc), confidence=0.95,
            ),
            FactSourceTier.OFFICIAL_NSE, "NSE", "doc:industrial:cash", SourceMode.REAL,
        ),
        *(_sector_ratio_facts()),  # a stray capital_adequacy fact that must NOT score industrially
    )
    result = StockRuleEngineV1()._balance_sheet(_financial_inputs(facts=industrial_facts, sector_text="Industrials"))
    assert result.area == RuleEngineArea.BALANCE_SHEET
    metric_names = {item.metric for item in result.metrics}
    # Industrial branch uses leverage/liquidity/coverage metrics, never the
    # financial-sector ratio metrics.
    assert metric_names.isdisjoint({"CAPITAL_ADEQUACY", "GROSS_NPA", "NET_NPA"})
    assert "DEBT_TO_EQUITY" in metric_names
    assert result.status != AreaScoreStatus.UNSCORABLE


# --------------------------------------------------------------------------- #
# TEST 9 -- BLOCKER 1: Lender BALANCE_SHEET_FACTS requires sector-ratio coverage
# -----------
# A regulated lender/HFC with DEBT/EQUITY/CASH but NO sector-ratio facts
# must NOT have BALANCE_SHEET_FACTS marked READY_FRESH. The scorer is
# UNSCORABLE because it needs capital_adequacy/gross_npa/net_npa.
# Readiness must align with the scorer's actual inputs.
# --------------------------------------------------------------------------- #
def test_blocker1_lender_balance_sheet_requires_sector_ratio_coverage():
    """For a regulated lender/HFC, BALANCE_SHEET_FACTS readiness must require
    sector-ratio metrics (capital_adequacy, gross_npa, net_npa) to match the
    scorer's financial branch inputs. Having only DEBT/EQUITY/CASH is not
    sufficient."""
    from app.research_readiness_runtime import (
        RepositoryResearchReadinessAdapter,
        _financial_fact_coverage,
    )
    from app.fact_precedence import FinancialFactKey

    # Create DEBT fact (simulating lender with debt but no sector ratios)
    debt_fact = FinancialFact(
        FinancialFactKey(INSTRUMENT_ID, "debt_or_borrowings", QUARTER_END_ISO, "ANNUAL", "CONSOLIDATED"),
        ProvenancedValue(
            value=Decimal("95000"), unit="INR", as_of_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
            source_url="https://nse/hfc", source_name="NSE",
            source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
            published_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 6, 30, tzinfo=timezone.utc), confidence=0.95,
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE", "doc:hfc:debt", SourceMode.REAL,
    )
    equity_fact = FinancialFact(
        FinancialFactKey(INSTRUMENT_ID, "equity", QUARTER_END_ISO, "ANNUAL", "CONSOLIDATED"),
        ProvenancedValue(
            value=Decimal("50000"), unit="INR", as_of_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
            source_url="https://nse/hfc", source_name="NSE",
            source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
            published_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 6, 30, tzinfo=timezone.utc), confidence=0.95,
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE", "doc:hfc:equity", SourceMode.REAL,
    )
    # NO sector-ratio facts - simulating the blocker scenario

    # Test that financial-fact coverage recognizes sector-ratio metrics
    coverage = _financial_fact_coverage(
        debt_fact, "debt_or_borrowings", {}, set(), set(), None
    )
    assert "BALANCE_SHEET_FACTS" in coverage
    assert "DEBT" in coverage["BALANCE_SHEET_FACTS"]
    # Sector-ratio input should NOT be present
    assert "FINANCIAL_SECTOR_CAPITAL_OR_ASSET_QUALITY" not in coverage.get("BALANCE_SHEET_FACTS", set())

    # Test that sector-ratio facts add the correct coverage
    capital_fact = FinancialFact(
        FinancialFactKey(INSTRUMENT_ID, "capital_adequacy", QUARTER_END_ISO, "QUARTERLY", "CONSOLIDATED"),
        ProvenancedValue(
            value=Decimal("14.20"), unit="PERCENT", as_of_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
            source_url="https://nse/hfc", source_name="NSE",
            source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 7, 1, tzinfo=timezone.utc), confidence=0.95,
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE", "doc:hfc:capital", SourceMode.REAL,
    )
    coverage = _financial_fact_coverage(
        capital_fact, "capital_adequacy", {}, set(), set(), None
    )
    assert "BALANCE_SHEET_FACTS" in coverage
    assert "FINANCIAL_SECTOR_CAPITAL_OR_ASSET_QUALITY" in coverage["BALANCE_SHEET_FACTS"]


# --------------------------------------------------------------------------- #
# TEST 10 -- Asset Management readiness/scorer agreement (no sector ratios)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("industry", ["Housing Finance Company", "Banking", "NBFC", "Asset Management"])
@pytest.mark.parametrize("with_ratios", [False, True])
def test_balance_sheet_readiness_and_scorer_agree(industry, with_ratios):
    import test_research_readiness as rr
    from app.research_applicability import classify_requirements, LENDER_BALANCE_SHEET_INPUT
    from app.research_readiness_runtime import _financial_fact_coverage, _evidence_from_financial_fact
    facts = []
    for metric, amount in [("debt_or_borrowings", "20"), ("equity", "100"),
                           ("cash_and_cash_equivalents", "10")]:
        facts.append(FinancialFact(
            FinancialFactKey(INSTRUMENT_ID, metric, QUARTER_END_ISO, "ANNUAL", "CONSOLIDATED"),
            ProvenancedValue(value=Decimal(amount), unit="INR", as_of_date=rr.NOW,
                source_url="https://nse/fixture", source_name="NSE",
                source_type=str(SourceType.EXCHANGE_ANNOUNCEMENT),
                published_at=rr.NOW, retrieved_at=rr.NOW, confidence=0.99),
            FactSourceTier.OFFICIAL_NSE, "NSE", "doc:fixture", SourceMode.REAL))
    if with_ratios:
        facts.extend(official_financial_sector_ratio_facts(_document(HFC_TEXT)))
    evidence = []
    for fact in facts:
        coverage = _financial_fact_coverage(fact, fact.key.metric, {}, set(), set(), None)
        if "BALANCE_SHEET_FACTS" in coverage:
            evidence.append(_evidence_from_financial_fact(fact, "BALANCE_SHEET_FACTS", tuple(coverage["BALANCE_SHEET_FACTS"])))
    snapshot = replace(rr.complete_snapshot(overrides={"BALANCE_SHEET_FACTS": tuple(evidence)}),
        applicability_by_requirement=classify_requirements("Financial Services", industry, "CANONICAL_REFERENCE"))
    _, readiness, _ = rr.assess(snapshot)
    area = readiness.for_requirement("BALANCE_SHEET_FACTS")
    scored = StockRuleEngineV1()._balance_sheet(replace(
        _financial_inputs(facts=facts, sector_text="Financial Services"), readiness=readiness,
        canonical_metadata={"canonicalSector": "Financial Services", "officialIndustry": industry}))
    if industry == "Asset Management":
        assert area.status == ResearchRequirementStatus.READY_FRESH
        assert LENDER_BALANCE_SHEET_INPUT in area.not_applicable_input_reasons
        assert "DEBT_TO_EQUITY" in {m.metric for m in scored.metrics}
        assert not {"CAPITAL_ADEQUACY", "GROSS_NPA", "NET_NPA"} & {m.metric for m in scored.metrics}
        assert scored.status != AreaScoreStatus.UNSCORABLE
    elif with_ratios:
        assert area.status == ResearchRequirementStatus.READY_FRESH
        assert scored.status != AreaScoreStatus.UNSCORABLE
    else:
        assert area.status != ResearchRequirementStatus.READY_FRESH
        assert LENDER_BALANCE_SHEET_INPUT in area.missing_input_ids
        assert scored.status == AreaScoreStatus.UNSCORABLE
