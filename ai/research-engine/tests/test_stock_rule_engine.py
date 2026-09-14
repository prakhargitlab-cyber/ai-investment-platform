from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app import main

from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier
from app.models import (
    CompanyResearchProfile,
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
from app.research_readiness import (
    FreshnessPolicyRegistry,
    ResearchDataConfidence,
    ResearchReadinessResult,
    ResearchRequirementReadiness,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
    ResearchSourceTier,
    RuleEngineArea,
)
from app.stock_rule_engine import (
    CURRENT_NEWS_WINDOW_DAYS,
    STOCK_RULE_ENGINE_AREA_WEIGHTS,
    STOCK_RULE_ENGINE_VERSION,
    AreaScoreStatus,
    ConfidenceLevel,
    DecisionSignal,
    StockRuleEngineInput,
    StockRuleEngineService,
    StockRuleEngineV1,
    _structured_data,
)


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000000145")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000000245")


def _profile(*, country: str = "IN", exchange: str = "NSE") -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID,
        company_id=COMPANY_ID,
        company_name="Deterministic Industries",
        ticker="DET",
        exchange=exchange,
        mic="XNSE" if country == "IN" else "XNAS",
        country=country,
        currency="INR" if country == "IN" else "USD",
    )


def _readiness(
    overrides: dict[str, ResearchRequirementStatus] | None = None,
    *,
    unsupported: set[str] | None = None,
    critical_pct: int = 100,
    overall_pct: int = 100,
) -> ResearchReadinessResult:
    overrides = overrides or {}
    unsupported = unsupported or set()
    registry = ResearchRequirementRegistry.default()
    freshness = FreshnessPolicyRegistry.default()
    items = []
    for requirement in registry.requirements:
        status = (
            ResearchRequirementStatus.UNSUPPORTED
            if requirement.requirement_id in unsupported
            else overrides.get(requirement.requirement_id, ResearchRequirementStatus.READY_FRESH)
        )
        covered = tuple(item.input_id for item in requirement.inputs) if status in {
            ResearchRequirementStatus.READY_FRESH,
            ResearchRequirementStatus.READY_STALE,
        } else ()
        items.append(
            ResearchRequirementReadiness(
                requirement_id=requirement.requirement_id,
                rule_engine_area=requirement.rule_engine_area,
                mandatory=requirement.mandatory,
                status=status,
                source=None if status == ResearchRequirementStatus.UNSUPPORTED else "NSE",
                source_tier=None if status == ResearchRequirementStatus.UNSUPPORTED else ResearchSourceTier.OFFICIAL,
                as_of=None if status == ResearchRequirementStatus.UNSUPPORTED else NOW,
                retrieved_at=None if status == ResearchRequirementStatus.UNSUPPORTED else NOW,
                age=None if status == ResearchRequirementStatus.UNSUPPORTED else timedelta(0),
                freshness_policy=freshness.get(requirement.freshness_policy_id),
                evidence_ids=() if status == ResearchRequirementStatus.UNSUPPORTED else (f"evidence:{requirement.requirement_id}",),
                missing_reason="MISSING" if status == ResearchRequirementStatus.MISSING else None,
                conflict_reason="CONFLICT" if status == ResearchRequirementStatus.CONFLICTING else None,
                supported_actions=(),
                importance=requirement.importance,
                source_url=None if status == ResearchRequirementStatus.UNSUPPORTED else "https://www.nseindia.com/filing.pdf",
                covered_input_ids=covered,
                missing_input_ids=tuple(item.input_id for item in requirement.inputs if item.input_id not in covered),
                coverage_pct=100 if covered else 0,
                critical_coverage_pct=100 if covered else 0,
            )
        )
    return ResearchReadinessResult(
        global_instrument_id=INSTRUMENT_ID,
        requirements=tuple(items),
        generated_at=NOW,
        overall_status=ResearchRequirementStatus.READY_FRESH if not overrides else ResearchRequirementStatus.PARTIAL,
        overall_completeness_pct=overall_pct,
        critical_completeness_pct=critical_pct,
        confidence=ResearchDataConfidence.HIGH,
        confidence_pct=100,
    )


def _pv(value, *, source="NSE", url="https://www.nseindia.com/filing.pdf", as_of=NOW):
    return ProvenancedValue(
        value=value,
        unit="INR",
        as_of_date=as_of,
        source_url=url,
        source_name=source,
        source_type="EXCHANGE_FILING",
        published_at=as_of,
        retrieved_at=NOW,
        confidence=0.95,
    )


def _fact(metric: str, value, period: datetime, period_type: str = "ANNUAL") -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(INSTRUMENT_ID, metric, period.date().isoformat(), period_type, "CONSOLIDATED"),
        _pv(value, as_of=period),
        FactSourceTier.OFFICIAL_NSE,
        "NSE",
        f"filing:{period.date()}:{metric}",
        SourceMode.REAL,
    )


def _structured(**updates) -> StructuredMarketSnapshotRecord:
    facts = {
        "trailingPE": _pv(Decimal("16"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "forwardPE": _pv(Decimal("14"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "priceToBook": _pv(Decimal("2.2"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "evToEbitda": _pv(Decimal("8"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "pegRatio": _pv(Decimal("1.1"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "marketCap": _pv(Decimal("10000"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "freeCashFlow": _pv(Decimal("650"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "roe": _pv(Decimal("0.22"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "roce": _pv(Decimal("0.24"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "operatingMargin": _pv(Decimal("0.18"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "profitMargin": _pv(Decimal("0.12"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "debtToEquity": _pv(Decimal("35"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "totalDebt": _pv(Decimal("900"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "totalCash": _pv(Decimal("400"), source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
        "sector": _pv("Industrials", source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS"),
    }
    for key, raw in updates.items():
        if raw is None:
            facts.pop(key, None)
        else:
            facts[key] = _pv(raw, source="Yahoo Finance", url="https://finance.yahoo.com/quote/DET.NS")
    resolution = StructuredInstrumentResolution(
        instrument_id=INSTRUMENT_ID,
        provider="YAHOO",
        provider_ticker="DET.NS",
        company_name="Deterministic Industries",
        exchange="NSE",
        currency="INR",
        confidence=1,
        resolved_at=NOW,
    )
    snapshot = StructuredMarketSnapshot(
        resolution=resolution,
        status="SUCCESS",
        retrieved_at=NOW,
        market_as_of=NOW,
        source_name="Yahoo Finance",
        source_url="https://finance.yahoo.com/quote/DET.NS",
        facts=facts,
    )
    return StructuredMarketSnapshotRecord(
        instrument_id=INSTRUMENT_ID,
        provider="YAHOO_FINANCE",
        provider_instrument_id="DET.NS",
        exchange="NSE",
        mic="XNSE",
        currency="INR",
        source_url=snapshot.source_url,
        source_name=snapshot.source_name,
        source_type="STRUCTURED_MARKET_PROVIDER",
        source_identity="DET.NS",
        market_as_of=NOW,
        retrieved_at=NOW,
        persisted_at=NOW,
        last_success_at=NOW,
        snapshot=snapshot,
    )


def _prices(count: int = 180, *, falling: bool = False) -> tuple[MarketPriceObservation, ...]:
    values = []
    for index in range(count):
        price = Decimal("200") - Decimal(index) if falling else Decimal("100") + Decimal(index) / 2
        values.append(
            MarketPriceObservation(
                instrument_id=INSTRUMENT_ID,
                observed_at=NOW - timedelta(days=count - 1 - index),
                price=max(Decimal("1"), price),
                currency="INR",
                provider="YAHOO_FINANCE",
                source_url="https://finance.yahoo.com/quote/DET.NS/history",
                retrieved_at=NOW,
            )
        )
    return tuple(values)


def _base_facts(*, declining: bool = False, leverage: Decimal = Decimal("900")) -> tuple[FinancialFact, ...]:
    facts = []
    annual_dates = [datetime(year, 3, 31, tzinfo=timezone.utc) for year in (2023, 2024, 2025, 2026)]
    revenues = [1000, 1200, 1450, 1750] if not declining else [1750, 1450, 1200, 950]
    earnings = [90, 115, 145, 190] if not declining else [190, 140, 100, 60]
    for period, revenue, pat in zip(annual_dates, revenues, earnings):
        facts.extend((_fact("revenue", revenue, period), _fact("pat", pat, period)))
    latest = annual_dates[-1]
    facts.extend(
        (
            _fact("equity", 2500, latest),
            _fact("debt_or_borrowings", leverage, latest),
            _fact("cash_and_cash_equivalents", 400, latest),
            _fact("current_assets", 1800, latest),
            _fact("current_liabilities", 1000, latest),
            _fact("ebitda", 320, latest),
            _fact("finance_cost", 40, latest),
            _fact("operating_cash_flow", 230, latest),
            _fact("free_cash_flow", 150, latest),
        )
    )
    quarter_dates = [
        datetime(2024, 6, 30, tzinfo=timezone.utc), datetime(2024, 9, 30, tzinfo=timezone.utc),
        datetime(2024, 12, 31, tzinfo=timezone.utc), datetime(2025, 3, 31, tzinfo=timezone.utc),
        datetime(2025, 6, 30, tzinfo=timezone.utc), datetime(2025, 9, 30, tzinfo=timezone.utc),
        datetime(2025, 12, 31, tzinfo=timezone.utc), datetime(2026, 3, 31, tzinfo=timezone.utc),
    ]
    for index, period in enumerate(quarter_dates):
        revenue = Decimal(250 + index * 12)
        pat = Decimal(22 + index * 2)
        facts.extend(
            (
                _fact("revenue", revenue, period, "QUARTERLY"),
                _fact("pat", pat, period, "QUARTERLY"),
                _fact("eps", pat / 10, period, "QUARTERLY"),
                _fact("ebitda", revenue * Decimal("0.18"), period, "QUARTERLY"),
            )
        )
    return tuple(facts)


def _event(
    *,
    event_type=ResearchEventType.MAJOR_CONTRACT,
    impact=EventImpact.POSITIVE,
    at=NOW - timedelta(days=3),
    title="Material contract awarded",
    summary="Exchange filing confirms a major customer contract.",
    classification=SourceClassification.EXCHANGE,
    reliability=ReliabilityLevel.LEVEL_A,
    status=ResearchLifecycleStatus.VALIDATED,
    confidence=0.95,
    monetary=Decimal("500"),
) -> ResearchEvent:
    document_id = uuid4()
    return ResearchEvent(
        instrument_id=INSTRUMENT_ID,
        company_id=COMPANY_ID,
        event_type=event_type,
        event_date=at,
        detected_at=NOW,
        title=title,
        summary=summary,
        source_document_id=document_id,
        source_url=f"https://www.nseindia.com/event/{document_id}",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=classification,
        reliability=reliability,
        source_mode=SourceMode.REAL,
        confidence=confidence,
        impact=impact,
        time_horizon=TimeHorizon.MEDIUM_TERM,
        monetary_value=monetary,
        counterparty="Material Customer" if monetary is not None else None,
        status=status,
        raw_evidence_reference=title,
        published_at=at,
        retrieved_at=NOW,
        independence_key=str(document_id),
    )


def _shareholding() -> tuple[ShareholdingSnapshot, ...]:
    snapshots = []
    for period, promoter, fii, pledge in (
        (datetime(2025, 12, 31, tzinfo=timezone.utc), "51", "12", "4"),
        (datetime(2026, 3, 31, tzinfo=timezone.utc), "52", "13", "2"),
    ):
        snapshots.append(
            ShareholdingSnapshot(
                instrument_id=INSTRUMENT_ID,
                period_end=period,
                source_provider="NSE",
                source_type="EXCHANGE_FILING",
                source_identity_key=f"shareholding:{period.date()}",
                source_url=f"https://www.nseindia.com/shareholding/{period.date()}",
                published_at=period + timedelta(days=20),
                retrieved_at=NOW,
                confidence=Decimal("0.99"),
                reliability_level=ReliabilityLevel.LEVEL_A,
                source_mode=SourceMode.REAL,
                values=[
                    ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER, percentage=Decimal(promoter)),
                    ShareholdingSnapshotValue(category=ShareholdingCategory.FII_FPI, percentage=Decimal(fii)),
                    ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER_PLEDGE, percentage=Decimal(pledge), metric_basis="PERCENT_OF_PROMOTER_HOLDING"),
                ],
            )
        )
    return tuple(snapshots)


def _inputs(
    *,
    readiness=None,
    facts=None,
    structured=None,
    prices=None,
    events=None,
    shareholding=None,
    profile=None,
    sector="Industrials",
) -> StockRuleEngineInput:
    return StockRuleEngineInput(
        profile=profile or _profile(),
        readiness=readiness or _readiness(),
        financial_facts=tuple(_base_facts() if facts is None else facts),
        structured_snapshots=tuple((_structured(),) if structured is None else structured),
        market_prices=tuple(_prices() if prices is None else prices),
        events=tuple((_event(),) if events is None else events),
        shareholding=tuple(_shareholding() if shareholding is None else shareholding),
        canonical_metadata={"canonicalSector": sector, "updatedAt": NOW.isoformat()},
        evaluated_at=NOW,
    )


def _area(result, area: RuleEngineArea):
    return next(item for item in result.area_scores if item.area == area)


def test_nonpositive_structured_latest_price_is_excluded_from_rule_inputs() -> None:
    structured = _structured(latestPrice=Decimal("0"))
    data = _structured_data(_inputs(structured=(structured,), prices=()))

    assert not any(key.replace("_", "").lower() == "latestprice" for key in data)


def test_v1_top_level_weights_total_exactly_100_and_cover_all_areas():
    assert STOCK_RULE_ENGINE_VERSION == "STOCK_RULE_ENGINE_V1"
    assert sum(STOCK_RULE_ENGINE_AREA_WEIGHTS.values()) == 100
    assert set(STOCK_RULE_ENGINE_AREA_WEIGHTS) == set(RuleEngineArea)


def test_each_area_contribution_uses_configured_weight_and_run_is_deterministic():
    engine = StockRuleEngineV1()
    inputs = _inputs()
    first = engine.evaluate(inputs, allow_partial=False)
    second = engine.evaluate(inputs, allow_partial=False)
    assert first.model_dump() == second.model_dump()
    for area in first.area_scores:
        assert area.weight == STOCK_RULE_ENGINE_AREA_WEIGHTS[RuleEngineArea(area.area)]
        if area.raw_score is not None:
            assert area.weighted_contribution == pytest.approx(area.raw_score * area.weight / 100, abs=0.01)


def test_critical_missing_blocks_full_analysis_and_strong_buy():
    readiness = _readiness({"LATEST_PRICE": ResearchRequirementStatus.MISSING}, critical_pct=70, overall_pct=90)
    result = StockRuleEngineV1().evaluate(_inputs(readiness=readiness), allow_partial=False)
    assert result.decision_signal == DecisionSignal.INSUFFICIENT_DATA
    assert result.overall_score is None
    assert not result.eligibility.full_analysis_allowed


def test_partial_analysis_requires_explicit_flag_and_is_capped_at_hold():
    readiness = _readiness({"GROWTH_FACTS": ResearchRequirementStatus.MISSING}, critical_pct=90, overall_pct=82)
    denied = StockRuleEngineV1().evaluate(_inputs(readiness=readiness), allow_partial=False)
    allowed = StockRuleEngineV1().evaluate(_inputs(readiness=readiness), allow_partial=True)
    assert denied.decision_signal == DecisionSignal.INSUFFICIENT_DATA
    assert denied.overall_score is None
    assert allowed.overall_score is not None
    assert allowed.decision_signal not in {DecisionSignal.STRONG_BUY, DecisionSignal.BUY, DecisionSignal.ACCUMULATE}


def test_unsupported_optional_regional_requirement_is_not_a_bad_score():
    readiness = _readiness(unsupported={"SHAREHOLDING"})
    usa = _inputs(readiness=readiness, profile=_profile(country="US", exchange="NASDAQ"), shareholding=(), sector="Technology")
    result = StockRuleEngineV1().evaluate(usa, allow_partial=False)
    shareholding = _area(result, RuleEngineArea.SHAREHOLDING)
    assert not shareholding.applicable
    assert shareholding.status == AreaScoreStatus.UNSUPPORTED
    assert shareholding.raw_score is None


def test_cheaper_otherwise_equal_valuation_scores_better_and_optional_missing_is_omitted():
    engine = StockRuleEngineV1()
    cheap = engine._valuation(_inputs(structured=(_structured(trailingPE=8, forwardPE=7),)))
    expensive = engine._valuation(_inputs(structured=(_structured(trailingPE=45, forwardPE=40),)))
    optional_missing = engine._valuation(_inputs(structured=(_structured(pegRatio=None, evToEbitda=None),)))
    assert cheap.raw_score > expensive.raw_score
    assert optional_missing.raw_score is not None
    assert all(item.metric not in {"PEG", "EV_EBITDA"} for item in optional_missing.metrics)


def test_valuation_sector_applicability_changes_pb_and_excludes_financial_ev_ebitda():
    industrial = StockRuleEngineV1()._valuation(_inputs(sector="Industrials"))
    financial = StockRuleEngineV1()._valuation(_inputs(sector="Financial Services"))
    assert any(item.rule == "PRICE_TO_BOOK_GENERAL_V1" for item in industrial.metrics)
    assert any(item.rule == "PRICE_TO_BOOK_FINANCIAL_V1" for item in financial.metrics)
    assert not any(item.metric == "EV_EBITDA" for item in financial.metrics)


def test_stronger_returns_and_margins_score_better_and_cash_conversion_penalizes():
    engine = StockRuleEngineV1()
    strong = engine._quality(_inputs())
    weak = engine._quality(_inputs(structured=(_structured(roe=Decimal("0.03"), roce=Decimal("0.04"), operatingMargin=Decimal("0.03"), profitMargin=Decimal("0.01")),)))
    cash_poor_facts = tuple(fact for fact in _base_facts() if fact.key.metric != "operating_cash_flow") + (_fact("operating_cash_flow", 5, datetime(2026, 3, 31, tzinfo=timezone.utc)),)
    cash_poor = engine._quality(_inputs(facts=cash_poor_facts))
    assert strong.raw_score > weak.raw_score
    assert next(item for item in cash_poor.metrics if item.metric == "CASH_CONVERSION").score < 40


def test_sustained_growth_beats_decline_and_qoq_has_limited_weight():
    engine = StockRuleEngineV1()
    growing = engine._growth(_inputs(facts=_base_facts()))
    declining = engine._growth(_inputs(facts=_base_facts(declining=True)))
    assert growing.raw_score > declining.raw_score
    qoq = next(item for item in growing.metrics if item.metric == "RECENT_REVENUE_QOQ")
    structural = next(item for item in growing.metrics if item.metric == "REVENUE_CAGR")
    assert qoq.configured_subrule_weight == 6
    assert structural.configured_subrule_weight == 27


def test_unsustainable_leverage_scores_worse_but_bank_skips_industrial_debt_rule():
    engine = StockRuleEngineV1()
    sound = engine._balance_sheet(_inputs())
    stressed = engine._balance_sheet(_inputs(structured=(_structured(debtToEquity=250),)))
    bank = engine._balance_sheet(_inputs(sector="Banks"))
    assert sound.raw_score > stressed.raw_score
    assert not any(item.rule == "INDUSTRIAL_DEBT_TO_EQUITY_V1" for item in bank.metrics)


def test_quarterly_uses_comparable_yoy_and_labels_sequential_as_limited_weight():
    area = StockRuleEngineV1()._quarterly(_inputs())
    revenue_yoy = next(item for item in area.metrics if item.metric == "REVENUE_YOY")
    revenue_qoq = next(item for item in area.metrics if item.metric == "REVENUE_QOQ")
    assert revenue_yoy.rule == "COMPARABLE_QUARTER_REVENUE_YOY_V1"
    assert revenue_qoq.rule == "SEQUENTIAL_REVENUE_QOQ_LIMITED_WEIGHT_V1"
    assert "nseindia.com" in (revenue_yoy.source_url or "")


def test_material_structured_catalyst_scores_but_keyword_only_document_cannot_enter_engine():
    engine = StockRuleEngineV1()
    material = engine._catalysts(_inputs())
    keyword_only = engine._catalysts(_inputs(events=()))
    immaterial_event = _event(monetary=None, classification=SourceClassification.REPUTABLE_NEWS, title="capacity mentioned", summary="A generic article says capacity.")
    irrelevant = engine._catalysts(_inputs(events=(immaterial_event,)))
    assert material.raw_score is not None
    assert keyword_only.raw_score is None
    assert irrelevant.raw_score is None


def test_technical_uses_durable_prices_and_preserves_50_150_median_names():
    area = StockRuleEngineV1()._technical(_inputs())
    rules = {item.rule for item in area.metrics}
    assert "PRICE_VS_50_OBSERVATION_MEDIAN_V1" in rules
    assert "PRICE_VS_150_OBSERVATION_MEDIAN_V1" in rules
    assert all("AVERAGE" not in rule and "YAHOO_FETCH" not in rule for rule in rules)


@pytest.mark.parametrize(
    ("age_days", "eligible"),
    [(CURRENT_NEWS_WINDOW_DAYS, True), (CURRENT_NEWS_WINDOW_DAYS + 1, False)],
)
def test_current_news_inclusive_30_day_boundary(age_days, eligible):
    event = _event(at=NOW - timedelta(days=age_days))
    area = StockRuleEngineV1()._news(_inputs(events=(event,)))
    assert (area.raw_score is not None) is eligible


def test_irrelevant_geopolitical_event_excluded_but_relevant_exposure_changes_news_score():
    unrelated = _event(event_type=ResearchEventType.OTHER, impact=EventImpact.STRONG_NEGATIVE, title="War disrupts oil supply", summary="Oil prices rise.", classification=SourceClassification.REPUTABLE_NEWS)
    unrelated_area = StockRuleEngineV1()._news(_inputs(events=(unrelated,), sector="Software"))
    airline_area = StockRuleEngineV1()._news(_inputs(events=(unrelated,), sector="Airlines"))
    assert unrelated_area.raw_score is None
    assert airline_area.raw_score is not None
    assert airline_area.raw_score < 50


def test_positive_and_negative_relevant_current_events_move_score_and_duplicates_deduplicate():
    positive = _event(impact=EventImpact.STRONG_POSITIVE)
    negative = positive.model_copy(update={"impact": EventImpact.STRONG_NEGATIVE})
    engine = StockRuleEngineV1()
    assert engine._news(_inputs(events=(positive,))).raw_score > engine._news(_inputs(events=(negative,))).raw_score
    duplicate = positive.model_copy(update={"event_id": uuid4()})
    assert len(engine._news(_inputs(events=(positive, duplicate))).metrics) == 1


def test_india_shareholding_applies_while_non_india_is_unsupported_without_penalty():
    engine = StockRuleEngineV1()
    india = engine._shareholding(_inputs())
    usa = engine._shareholding(_inputs(profile=_profile(country="US", exchange="NASDAQ"), shareholding=()))
    assert india.applicable and india.raw_score is not None
    assert not usa.applicable and usa.status == AreaScoreStatus.UNSUPPORTED


def test_old_unresolved_governance_remains_scored_and_resolved_is_handled_differently():
    old = _event(event_type=ResearchEventType.REGULATORY_EVENT, impact=EventImpact.STRONG_NEGATIVE, at=NOW - timedelta(days=500), title="Regulatory enforcement", summary="Official enforcement remains unresolved.")
    resolved = old.model_copy(update={"event_id": uuid4(), "title": "Regulatory matter resolved", "summary": "Matter remediated and closed."})
    engine = StockRuleEngineV1()
    unresolved_area = engine._governance(_inputs(events=(old,)))
    resolved_area = engine._governance(_inputs(events=(resolved,)))
    assert unresolved_area.raw_score < resolved_area.raw_score
    assert old.event_id.hex in "".join(unresolved_area.evidence_references).replace("-", "")


def test_authoritative_severe_governance_can_override_but_weak_news_cannot():
    severe = _event(event_type=ResearchEventType.REGULATORY_EVENT, impact=EventImpact.STRONG_NEGATIVE, title="Confirmed accounting fraud", summary="Exchange confirms accounting fraud.")
    weak = severe.model_copy(update={"event_id": uuid4(), "source_classification": SourceClassification.REPUTABLE_NEWS, "reliability": ReliabilityLevel.LEVEL_C, "confidence": 0.55})
    engine = StockRuleEngineV1()
    severe_result = engine.evaluate(_inputs(events=(severe,)), allow_partial=False)
    weak_result = engine.evaluate(_inputs(events=(weak,)), allow_partial=False)
    assert any(item.code == "CONFIRMED_FRAUD_OR_ACCOUNTING_CRISIS" for item in severe_result.risk_overrides)
    assert severe_result.decision_signal == DecisionSignal.EXIT_REVIEW
    assert not weak_result.risk_overrides


def test_confidence_is_separate_from_raw_score_conflicts_lower_it_and_authority_raises_it():
    engine = StockRuleEngineV1()
    high = engine.evaluate(_inputs(), allow_partial=False)
    conflict_readiness = _readiness({"VALUATION_INPUTS": ResearchRequirementStatus.CONFLICTING}, critical_pct=80, overall_pct=90)
    low = engine.evaluate(_inputs(readiness=conflict_readiness), allow_partial=True)
    assert high.confidence == ConfidenceLevel.HIGH
    assert low.confidence_score < high.confidence_score
    assert high.overall_score != high.confidence_score


def test_same_inputs_have_same_fingerprint_and_relevant_change_invalidates_it():
    engine = StockRuleEngineV1()
    inputs = _inputs()
    first = engine.input_fingerprint(inputs, allow_partial=False)
    same = engine.input_fingerprint(inputs, allow_partial=False)
    changed = engine.input_fingerprint(_inputs(structured=(_structured(trailingPE=17),)), allow_partial=False)
    changed_readiness = replace(
        inputs.readiness,
        requirements=tuple(
            replace(item, source_url="https://www.nseindia.com/revised-filing.pdf")
            if item.requirement_id == "QUARTERLY_FINANCIALS"
            else item
            for item in inputs.readiness.requirements
        ),
    )
    provenance_changed = engine.input_fingerprint(
        _inputs(readiness=changed_readiness), allow_partial=False
    )
    assert first == same
    assert first != changed
    assert first != provenance_changed


def test_global_score_persistence_cache_and_private_portfolio_field_guard():
    persistence = SqliteResearchPersistence()
    result = StockRuleEngineV1().evaluate(_inputs(), allow_partial=False).model_dump(mode="json", by_alias=False)
    persistence.upsert_stock_rule_engine_result(result)
    loaded = persistence.load_stock_rule_engine_result(INSTRUMENT_ID, STOCK_RULE_ENGINE_VERSION, result["input_fingerprint"])
    assert loaded["input_fingerprint"] == result["input_fingerprint"]
    assert "portfolio_id" not in loaded
    with pytest.raises(ValueError, match="PRIVATE_PORTFOLIO_FIELD"):
        persistence.upsert_stock_rule_engine_result({**result, "portfolio_id": str(uuid4())})


class _CacheRepository:
    def __init__(self, inputs):
        self.inputs = inputs
        self.cache = {}
        self.provider_calls = 0
        self.portfolio_mutations = 0
        self.watchlist_mutations = 0

    async def financial_facts_for_instruments(self, ids): return {INSTRUMENT_ID: list(self.inputs.financial_facts)}
    async def structured_market_snapshots_for_instruments(self, ids): return {INSTRUMENT_ID: list(self.inputs.structured_snapshots)}
    async def market_price_observations_for_instruments(self, ids): return {INSTRUMENT_ID: list(self.inputs.market_prices)}
    def events_for(self, instrument_id, source_mode=None): return list(self.inputs.events)
    def shareholding_for(self, instrument_id, limit=8): return list(self.inputs.shareholding)
    async def stock_rule_engine_result(self, instrument_id, version, fingerprint): return self.cache.get((str(instrument_id), version, fingerprint))
    async def persist_stock_rule_engine_result(self, result): self.cache[(str(result["global_instrument_id"]), result["rule_engine_version"], result["input_fingerprint"])] = result
    async def fetch_yahoo(self): self.provider_calls += 1; raise AssertionError("provider called")
    async def mutate_portfolio(self): self.portfolio_mutations += 1; raise AssertionError("portfolio mutated")
    async def mutate_watchlist(self): self.watchlist_mutations += 1; raise AssertionError("watchlist mutated")


class _Metadata:
    def __init__(self): self.metadata = {"canonicalSector": "Industrials", "updatedAt": NOW.isoformat()}
    def canonical_metadata_for(self, instrument_id): return dict(self.metadata)
    def remember_canonical_metadata(self, instrument_id, metadata): self.metadata = dict(metadata)


@pytest.mark.asyncio
async def test_analysis_service_is_provider_free_non_held_safe_and_reuses_exact_cache():
    inputs = _inputs()
    repository = _CacheRepository(inputs)
    service = StockRuleEngineService(repository, _Metadata())
    first = await service.analyze(inputs.profile, inputs.readiness, allow_partial=False, now=NOW)
    second = await service.analyze(inputs.profile, inputs.readiness, allow_partial=False, now=NOW)
    assert not first.cache_hit
    assert second.cache_hit
    assert first.input_fingerprint == second.input_fingerprint
    assert first.overall_score == second.overall_score
    assert repository.provider_calls == repository.portfolio_mutations == repository.watchlist_mutations == 0


def test_score_record_has_explainable_metrics_sources_and_no_portfolio_fields():
    result = StockRuleEngineV1().evaluate(_inputs(), allow_partial=False)
    payload = result.model_dump(mode="json", by_alias=True)
    text = str(payload).casefold()
    assert payload["ruleEngineVersion"] == STOCK_RULE_ENGINE_VERSION
    assert payload["evidenceReferences"]
    assert any(metric["sourceUrl"] for area in payload["areaScores"] for metric in area["metrics"])
    quarterly = next(
        area for area in payload["areaScores"]
        if area["area"] == "QUARTERLY_EARNINGS_TREND"
    )
    assert any(
        source["sourceProvider"] == "NSE"
        and "nseindia.com" in source["sourceUrl"]
        for source in quarterly["sourceReferences"]
    )
    assert any(reference.startswith("evidence:") for reference in quarterly["evidenceReferences"])
    assert all(term not in text for term in ("portfolioid", "averagecost", "costbasis", "quantity", "allocation"))


class _ApiRepository:
    def __init__(self, profile):
        self.value = profile
        self.portfolio_mutations = 0
        self.watchlist_mutations = 0
        self.provider_calls = 0

    def profile(self, instrument_id):
        if instrument_id != self.value.instrument_id:
            raise StopIteration
        return self.value


class _ApiOrchestrator:
    def __init__(self, profile):
        self.profile = profile
        self.identity_reads = 0
        self.portfolio_mutations = 0
        self.watchlist_mutations = 0
        self.provider_calls = 0

    async def global_instrument_metadata(self, instrument_id, **kwargs):
        self.identity_reads += 1
        return {
            "globalInstrumentId": str(instrument_id),
            "canonicalSector": "Industrials",
            "updatedAt": NOW.isoformat(),
        }

    def register_global_profile_metadata(self, instrument_id, metadata):
        return instrument_id == self.profile.instrument_id


class _ApiReadinessRuntime:
    def __init__(self, readiness): self.value = readiness; self.provider_calls = 0
    async def read(self, instrument_id, jurisdiction="GLOBAL"): return self.value


class _ApiAnalysisService:
    def __init__(self, result): self.value = result; self.calls = []
    async def analyze(self, profile, readiness, *, allow_partial, now=None):
        self.calls.append((profile.instrument_id, allow_partial))
        return self.value


def test_analysis_api_accepts_global_identity_and_performs_no_provider_or_private_mutation(monkeypatch):
    profile = _profile()
    readiness = _readiness()
    result = StockRuleEngineV1().evaluate(_inputs(readiness=readiness), allow_partial=False)
    repository = _ApiRepository(profile)
    orchestrator = _ApiOrchestrator(profile)
    runtime = _ApiReadinessRuntime(readiness)
    service = _ApiAnalysisService(result)
    metadata = _Metadata()
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(main, "portfolio_orchestrator", orchestrator)
    monkeypatch.setattr(main, "research_readiness_runtime", runtime)
    monkeypatch.setattr(main, "research_readiness_adapter", metadata)
    monkeypatch.setattr(main, "stock_rule_engine_service", service)
    telemetry = []
    monkeypatch.setattr(main.logger, "info", lambda message, *args: telemetry.append(message % args))

    response = TestClient(main.app).post(
        f"/api/v1/research/analysis/{INSTRUMENT_ID}",
        json={"allowPartial": False},
        headers={
            "X-AIP-User-Id": "held-user",
            "X-AIP-User-Issuer": "gateway",
            "X-AIP-User-Subject": "subject",
        },
    )

    assert response.status_code == 200
    assert response.json()["globalInstrumentId"] == str(INSTRUMENT_ID)
    assert response.json()["ruleEngineVersion"] == STOCK_RULE_ENGINE_VERSION
    assert service.calls == [(INSTRUMENT_ID, False)]
    assert runtime.provider_calls == repository.provider_calls == orchestrator.provider_calls == 0
    assert repository.portfolio_mutations == repository.watchlist_mutations == 0
    assert orchestrator.portfolio_mutations == orchestrator.watchlist_mutations == 0
    assert any("operation=RULE_ENGINE_ANALYSIS" in entry for entry in telemetry)


def test_held_and_non_held_callers_receive_same_global_company_score(monkeypatch):
    profile = _profile()
    readiness = _readiness()
    result = StockRuleEngineV1().evaluate(_inputs(readiness=readiness), allow_partial=False)
    monkeypatch.setattr(main, "repository", _ApiRepository(profile))
    monkeypatch.setattr(main, "portfolio_orchestrator", _ApiOrchestrator(profile))
    monkeypatch.setattr(main, "research_readiness_runtime", _ApiReadinessRuntime(readiness))
    monkeypatch.setattr(main, "research_readiness_adapter", _Metadata())
    monkeypatch.setattr(main, "stock_rule_engine_service", _ApiAnalysisService(result))
    client = TestClient(main.app)

    held = client.post(
        f"/api/v1/research/analysis/{INSTRUMENT_ID}",
        headers={"X-AIP-User-Id": "held", "X-AIP-User-Issuer": "gateway", "X-AIP-User-Subject": "held"},
        json={"allowPartial": False},
    ).json()
    non_held = client.post(
        f"/api/v1/research/analysis/{INSTRUMENT_ID}",
        headers={"X-AIP-User-Id": "watchlist", "X-AIP-User-Issuer": "gateway", "X-AIP-User-Subject": "watchlist"},
        json={"allowPartial": False},
    ).json()

    for key in ("inputFingerprint", "overallScore", "qualityScore", "opportunityScore", "riskScore", "decisionSignal", "areaScores"):
        assert held[key] == non_held[key]

def test_domain_not_applicable_has_no_zero_or_neutral_score():
    readiness = _readiness({"ORDER_BOOK_CAPEX_GUIDANCE": ResearchRequirementStatus.NOT_APPLICABLE})
    result = StockRuleEngineV1()._catalysts(_inputs(readiness=readiness))
    assert result.status == AreaScoreStatus.NOT_APPLICABLE
    assert result.applicable is False
    assert result.raw_score is None
    assert result.metrics == []
