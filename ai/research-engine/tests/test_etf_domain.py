from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.etf_domain import EtfRequirement as R, EtfRequirementRole as Role, classify_etf, etf_applicability
from app.models import EtfHolding, EtfResearchProfile, EtfSubtype, ShareholdingSnapshot, ProvenancedValue


def profile(**updates):
    return EtfResearchProfile(instrument_id=UUID(int=1), fund_id=UUID(int=2), fund_name="Example fund",
        ticker="EXAMPLE", exchange="NSE", mic="XNSE", **updates)


@pytest.mark.parametrize("subtype", list(EtfSubtype))
def test_explicit_canonical_categories(subtype):
    assert classify_etf(profile(), category=subtype.value) == subtype
    assert classify_etf(profile(subtype=subtype)) == subtype


@pytest.mark.parametrize("asset,strategy,region,expected", [
    ("Equity", "Broad market index", None, EtfSubtype.EQUITY_INDEX),
    ("Equity", "Sector thematic", None, EtfSubtype.EQUITY_SECTOR_THEMATIC),
    ("Equity", "Smart beta", None, EtfSubtype.EQUITY_SMART_BETA),
    ("Gold", None, None, EtfSubtype.GOLD),
    ("Silver", None, None, EtfSubtype.SILVER),
    ("Fixed income", None, None, EtfSubtype.DEBT),
    ("Money market", None, None, EtfSubtype.MONEY_MARKET),
    ("Equity", None, "International", EtfSubtype.INTERNATIONAL_EQUITY),
    ("Debt", None, "International", EtfSubtype.INTERNATIONAL_DEBT),
    ("Multi asset", None, None, EtfSubtype.HYBRID),
    (None, None, None, EtfSubtype.OTHER),
    ("Equity", None, None, EtfSubtype.OTHER),
    ("Commodity", None, None, EtfSubtype.OTHER),
])
def test_metadata_classification(asset, strategy, region, expected):
    assert classify_etf(profile(asset_class=asset), investment_strategy=strategy, exposure_region=region) == expected


@pytest.mark.parametrize("ticker", ["GOLD", "SILVER", "BANK", "LIQUID", "ARBITRARY123"])
def test_identifiers_and_names_never_classify(ticker):
    candidate = profile().model_copy(update={"ticker": ticker, "fund_name": "Gold International Smart Beta Fund"})
    assert classify_etf(candidate) == EtfSubtype.OTHER
    assert classify_etf(candidate, category="DEBT") == EtfSubtype.DEBT


def test_unknown_conflicting_and_specific_metadata():
    assert classify_etf(profile(), category="Gold mining equities") == EtfSubtype.OTHER
    assert classify_etf(profile(underlying_index="An index")) == EtfSubtype.OTHER
    assert classify_etf(profile(asset_class="Equity"), category="GOLD") == EtfSubtype.OTHER
    assert classify_etf(profile(subtype="GOLD"), category="SILVER") == EtfSubtype.OTHER
    assert classify_etf(profile(asset_class="Debt"), category="INTERNATIONAL_DEBT") == EtfSubtype.INTERNATIONAL_DEBT


@pytest.mark.parametrize("subtype", list(EtfSubtype))
def test_complete_applicability_matrix(subtype):
    matrix = etf_applicability(subtype, is_benchmark_based=True)
    assert set(matrix) == set(R)
    mandatory = {R.IDENTITY, R.BENCHMARK_INDEX, R.NAV, R.MARKET_PRICE, R.AUM, R.EXPENSE_RATIO, R.LIQUIDITY}
    contextual = {R.INDEX_VALUATION, R.INDEX_MOMENTUM, R.CATEGORY_RELATIVE_PERFORMANCE, R.CURRENT_NEWS}
    for requirement, decision in matrix.items():
        expected = Role.MANDATORY if requirement in mandatory else Role.CONTEXTUAL if requirement in contextual else Role.SUPPORTING
        assert decision.role == expected
        assert decision.blocking == (requirement in mandatory)
    forbidden = {"PROMOTER", "PROMOTER_PLEDGE", "SHAREHOLDING_PATTERN", "ROE", "ROCE", "MARGINS",
                 "QUARTERLY_RESULTS", "ORDER_BOOK", "CAPEX_GUIDANCE", "GROSS_NPA", "NET_NPA", "CAPITAL_ADEQUACY"}
    assert not forbidden.intersection(matrix)


def test_benchmark_status_is_explicit_not_missing_evidence():
    assert etf_applicability(EtfSubtype.OTHER)[R.BENCHMARK_INDEX].blocking
    matrix = etf_applicability(EtfSubtype.OTHER, is_benchmark_based=False)
    for requirement in (R.BENCHMARK_INDEX, R.TRACKING_ERROR, R.TRACKING_DIFFERENCE, R.INDEX_CONSTITUENTS,
                        R.INDEX_VALUATION, R.INDEX_MOMENTUM):
        assert matrix[requirement].role == Role.NOT_APPLICABLE
    assert matrix[R.NAV].blocking
    assert matrix[R.CURRENT_NEWS].role == Role.CONTEXTUAL


def test_legacy_profile_roundtrip_and_missing_nav_not_replaced_by_price():
    price = ProvenancedValue(value=Decimal("123"), source_name="fixture", source_url="https://example.org",
        source_type="EXCHANGE_ANNOUNCEMENT", retrieved_at=datetime(2026, 6, 30, tzinfo=timezone.utc))
    old = profile(fund_provider="Example AMC", underlying_index="Example benchmark", facts={"market_price": price})
    restored = EtfResearchProfile.model_validate(old.model_dump(by_alias=True))
    assert restored == old
    assert restored.subtype == EtfSubtype.OTHER
    assert restored.asset_class is None and restored.inception_date is None
    assert restored.is_benchmark_based is None
    assert restored.fund_provider == "Example AMC"
    assert restored.underlying_index == "Example benchmark"
    assert set(restored.facts) == {"market_price"}
    assert "nav" not in restored.facts
    assert etf_applicability(restored.subtype)[R.NAV].blocking


@pytest.mark.parametrize("weight", [None, Decimal("0"), Decimal("12.5")])
def test_holdings_are_separate_and_preserve_missing_values(weight):
    holding = EtfHolding(etf_instrument_id=UUID(int=1), as_of_date=date(2026, 6, 30),
        source_provider="fixture", weight_percentage=weight)
    assert not isinstance(holding, ShareholdingSnapshot)
    assert "category" not in EtfHolding.model_fields
    assert holding.weight_percentage == weight
    for field in ("quantity", "value", "constituent_identifier", "constituent_symbol", "constituent_isin",
                  "constituent_name", "value_currency", "confidence", "reliability_level", "source_locator", "evidence_reference"):
        assert getattr(holding, field) is None
    assert EtfHolding.model_validate(holding.model_dump(by_alias=True)) == holding


def test_holdings_preserve_explicit_amounts_and_provenance():
    holding = EtfHolding(etf_instrument_id=UUID(int=1), constituent_identifier="provider:42", constituent_symbol="EXAMPLE",
        constituent_isin="IN0000000000", constituent_name="Example constituent", weight_percentage=Decimal("2.5"),
        quantity=Decimal("0"), value=Decimal("100"), value_currency="INR", as_of_date=date(2026, 6, 30),
        source_provider="fixture", source_locator="table:1/row:2", evidence_reference="document:42",
        confidence=0.9, reliability_level="LEVEL_A")
    assert holding.quantity == 0 and holding.value == 100
    assert holding.source_locator == "table:1/row:2"
    assert EtfHolding.model_validate_json(holding.model_dump_json()) == holding


@pytest.mark.parametrize("invalid", [{"weight_percentage": -1}, {"weight_percentage": 101}, {"confidence": 2}])
def test_holdings_reject_invalid_ranges(invalid):
    with pytest.raises(ValidationError):
        EtfHolding(etf_instrument_id=UUID(int=1), as_of_date=date(2026, 6, 30), source_provider="fixture", **invalid)
