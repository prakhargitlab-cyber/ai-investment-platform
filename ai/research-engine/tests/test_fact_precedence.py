from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey, merge_fact
from app.models import ProvenancedValue
from app.persistence import SqliteResearchPersistence


INSTRUMENT = UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa")


def fact(metric: str, value, tier: FactSourceTier, *, period="2026-06-30", period_type="QUARTERLY", basis="CONSOLIDATED", provider="NSE"):
    return FinancialFact(
        FinancialFactKey(INSTRUMENT, metric, period, period_type, basis),
        ProvenancedValue(value=value, unit="INR", source_url=f"https://example.invalid/{provider}", source_name=provider,
                         source_type="EXCHANGE" if tier == FactSourceTier.OFFICIAL_NSE else "STRUCTURED_MARKET_PROVIDER",
                         retrieved_at=datetime.now(timezone.utc)),
        tier, provider, f"{provider}:{metric}:{period}",
    )


def test_nse_wins_conflicts_and_yahoo_only_fills_missing() -> None:
    nse_revenue = fact("revenue", Decimal("100"), FactSourceTier.OFFICIAL_NSE)
    yahoo_revenue = fact("revenue", Decimal("98"), FactSourceTier.YAHOO, provider="YAHOO_FINANCE")
    yahoo_debt = fact("debt", Decimal("40"), FactSourceTier.YAHOO, provider="YAHOO_FINANCE")
    assert merge_fact(nse_revenue, yahoo_revenue) is nse_revenue
    assert merge_fact(None, yahoo_debt) is yahoo_debt


def test_search_fills_only_absent_and_cannot_overwrite_yahoo_or_nse() -> None:
    yahoo = fact("debt", Decimal("40"), FactSourceTier.YAHOO, provider="YAHOO_FINANCE")
    search = fact("debt", Decimal("42"), FactSourceTier.SEARCH, provider="SEARCH")
    absent = fact("capex", Decimal("8"), FactSourceTier.SEARCH, provider="SEARCH")
    assert merge_fact(yahoo, search) is yahoo
    assert merge_fact(None, absent) is absent
    assert merge_fact(fact("pat", 12, FactSourceTier.OFFICIAL_NSE), fact("pat", 11, FactSourceTier.SEARCH, provider="SEARCH")).source_provider == "NSE"


def test_missing_fallback_never_erases_valid_value() -> None:
    nse = fact("eps", Decimal("4.5"), FactSourceTier.OFFICIAL_NSE)
    assert merge_fact(nse, fact("eps", None, FactSourceTier.YAHOO, provider="YAHOO_FINANCE")) is nse


def test_period_type_and_basis_are_collision_boundaries() -> None:
    quarterly = fact("revenue", 100, FactSourceTier.OFFICIAL_NSE)
    annual = fact("revenue", 400, FactSourceTier.YAHOO, period_type="ANNUAL", provider="YAHOO_FINANCE")
    standalone = fact("revenue", 90, FactSourceTier.YAHOO, basis="STANDALONE", provider="YAHOO_FINANCE")
    with pytest.raises(ValueError): merge_fact(quarterly, annual)
    with pytest.raises(ValueError): merge_fact(quarterly, standalone)


def test_explicit_official_same_fact_correction_preserves_winning_provenance() -> None:
    original = fact("revenue", 100, FactSourceTier.OFFICIAL_NSE, provider="NSE_RECORD_1")
    correction = fact("revenue", 101, FactSourceTier.OFFICIAL_NSE, provider="NSE_RECORD_2")
    assert merge_fact(original, correction) is original
    accepted = merge_fact(original, correction, allow_same_tier_correction=True)
    assert accepted is correction
    assert accepted.value.source_name == "NSE_RECORD_2"
    structured = fact("revenue", 102, FactSourceTier.STRUCTURED_FUNDAMENTALS, provider="EODHD")
    assert merge_fact(structured, fact("revenue", 103, FactSourceTier.STRUCTURED_FUNDAMENTALS, provider="EODHD_CORRECTION"), allow_same_tier_correction=True) is structured


def test_durable_fact_upsert_round_trip_and_canonical_boundaries() -> None:
    store = SqliteResearchPersistence()
    q1 = fact("revenue", Decimal("100"), FactSourceTier.OFFICIAL_NSE)
    q2 = fact("revenue", Decimal("110"), FactSourceTier.OFFICIAL_NSE, period="2026-09-30")
    annual = fact("revenue", Decimal("400"), FactSourceTier.OFFICIAL_NSE, period_type="ANNUAL")
    standalone = fact("revenue", Decimal("90"), FactSourceTier.OFFICIAL_NSE, basis="STANDALONE")
    assert store.upsert_financial_fact(q1)
    assert not store.upsert_financial_fact(fact("revenue", None, FactSourceTier.YAHOO, provider="YAHOO_FINANCE"))
    assert store.upsert_financial_fact(q2)
    assert store.upsert_financial_fact(annual)
    assert store.upsert_financial_fact(standalone)
    loaded = store.load_financial_facts()
    assert len(loaded) == 4
    assert next(value for value in loaded if value.key == q1.key).value.value == Decimal("100")
    assert next(value for value in loaded if value.key == q1.key).source_tier == FactSourceTier.OFFICIAL_NSE


def test_persisted_tier_three_is_official_nse_and_outranks_structured_fundamentals() -> None:
    store = SqliteResearchPersistence()
    nse = fact("revenue", Decimal("100"), FactSourceTier.OFFICIAL_NSE, basis="UNKNOWN")
    assert int(FactSourceTier.OFFICIAL_NSE) == 3
    assert store.upsert_financial_fact(nse)
    loaded = store.load_financial_facts()[0]
    assert loaded.source_tier == FactSourceTier.OFFICIAL_NSE
    assert not store.upsert_financial_fact(fact("revenue", Decimal("90"), FactSourceTier.STRUCTURED_FUNDAMENTALS, basis="UNKNOWN", provider="EODHD"))


def test_yahoo_round_trip_fills_only_same_unknown_identity_and_never_replaces_official() -> None:
    store = SqliteResearchPersistence()
    yahoo = fact("revenue", Decimal("98"), FactSourceTier.YAHOO, basis="UNKNOWN", provider="YAHOO_FINANCE")
    official = fact("revenue", Decimal("100"), FactSourceTier.OFFICIAL_NSE, basis="UNKNOWN", provider="NSE")
    annual = fact("revenue", Decimal("390"), FactSourceTier.YAHOO, period_type="ANNUAL", basis="UNKNOWN", provider="YAHOO_FINANCE")
    assert store.upsert_financial_fact(yahoo)
    assert store.upsert_financial_fact(official)
    assert not store.upsert_financial_fact(fact("revenue", Decimal("99"), FactSourceTier.YAHOO, basis="UNKNOWN", provider="YAHOO_FINANCE"))
    assert store.upsert_financial_fact(annual)
    loaded = store.load_financial_facts()
    assert len(loaded) == 2
    assert next(value for value in loaded if value.key.period_type == "QUARTERLY").source_provider == "NSE"
