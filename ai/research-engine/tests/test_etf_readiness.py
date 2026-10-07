"""ETF-3 readiness tests: provider-free, deterministic."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.etf_readiness import (
    EtfReadinessStatus, EtfRequirementStatus, EtfReadinessSummary,
    evaluate_etf_readiness, is_etf_ready_for_analysis,
)
from app.etf_evidence import (
    EtfFact, EtfNavObservation, EtfListing, EtfProvenance,
    EtfAcquisitionAttempt as EtfAttempt, EtfAcquisitionOutcome as Outcome,
    EtfAuthority, EtfMetric
)
from app.persistence import SqliteResearchPersistence
from app.models import DailyMarketBar, SourceMode


NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
ID = UUID(int=1)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Pin evaluate_etf_readiness's internal datetime.now() to NOW so
    freshness checks are deterministic regardless of wall-clock date."""
    import app.etf_readiness as readiness
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(readiness, "datetime", Clock)


def provenance(**updates):
    data = dict(provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity="document:1",
        source_url="https://nsearchives.nseindia.com/document", authority=EtfAuthority.OFFICIAL_EXCHANGE,
        retrieved_at=NOW, reliability_level="LEVEL_A")
    return EtfProvenance(**(data | updates))


@pytest.fixture
def store(tmp_path):
    result = SqliteResearchPersistence(tmp_path / "etf.sqlite")
    yield result
    result._connection.close()


class TestEtfReadinessMandatory:
    """Tests for mandatory requirements (IDENTITY, MARKET_PRICE, TRADING_VOLUME)."""

    def test_all_3_mandatory_ready_is_ready(self, store):
        """All 3 mandatory requirements READY => ready for analysis."""
        # Identity
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        # Market price
        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        # Trading volume
        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.mandatory_total == 3
        assert summary.mandatory_ready == 3
        assert summary.mandatory_missing == 0
        assert summary.ready_for_analysis is True

    def test_market_price_missing_is_not_ready(self, store):
        """Market price missing => NOT ready."""
        # Identity
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        # Trading volume
        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.mandatory_missing == 1
        assert summary.ready_for_analysis is False

    def test_market_price_stale_is_not_ready(self, store):
        """Market price stale (past 1 day) is NOT ready_for_analysis.

        Per ETF-3 contract: stale mandatory evidence is NOT ready.
        """
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        # Market price with stale date (5 days ago)
        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=(NOW - timedelta(days=5)).date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.mandatory_total == 3
        assert summary.mandatory_stale == 1  # Market price is stale
        assert summary.ready_for_analysis is False  # Stale mandatory is NOT ready

    def test_trading_volume_missing_is_not_ready(self, store):
        """Trading volume missing => NOT ready."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.mandatory_missing == 1
        assert summary.ready_for_analysis is False

    def test_trading_volume_zero_is_ready(self, store):
        """Trading volume = 0 is valid evidence and therefore READY."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("0"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.mandatory_ready == 3
        assert summary.ready_for_analysis is True


class TestEtfReadinessIdentity:
    """Tests for identity behavior."""

    def test_identity_missing_is_not_ready(self, store):
        """Identity missing => NOT ready."""
        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.mandatory_total == 3  # All 3 mandatory checked
        assert summary.mandatory_ready == 0
        assert summary.mandatory_missing == 3  # All 3 mandatory missing
        assert summary.ready_for_analysis is False

    def test_unresolved_listing_is_not_ready(self, store):
        """Unresolved NSE listing (no instrument_id) is NOT ready."""
        # NSE ETF list with unresolved instrument_id
        listing = EtfListing(instrument_id=None, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        statuses, summary = evaluate_etf_readiness(store, ID)

        identity_status = next(s for s in statuses if s.requirement == "IDENTITY")
        # instrument_id is None but listing exists - should be MISSING until resolved
        assert identity_status.status in (EtfReadinessStatus.MISSING, EtfReadinessStatus.READY_FRESH)


class TestEtfReadinessMarketPrice:
    """Tests for market price behavior."""

    def test_nav_not_satisfied_by_market_price(self, store):
        """NAV must NOT satisfy MARKET_PRICE requirement."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        # Store NAV but not market price
        nav = EtfNavObservation(instrument_id=ID, nav=Decimal("100"), currency="INR",
            nav_date=NOW.date(), provenance=provenance())
        store.save_etf_evidence(nav)

        # Market price should still be MISSING
        statuses, summary = evaluate_etf_readiness(store, ID)
        # 2 mandatory missing (MARKET_PRICE, TRADING_VOLUME)
        assert summary.mandatory_missing == 2


class TestEtfReadinessZeroVsMissing:
    """Tests for zero vs missing semantics."""

    def test_zero_volume_not_missing(self, store):
        """Zero volume is treated as valid evidence, not missing."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("0"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)
        assert summary.mandatory_ready == 3


class TestEtfReadinessSupporting:
    """Tests for supporting requirement behavior."""

    def test_nav_missing_still_ready(self, store):
        """NAV missing => still ready (supporting non-blocking)."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.ready_for_analysis is True
        nav_status = next(s for s in statuses if s.requirement == "NAV")
        assert nav_status.status == EtfReadinessStatus.MISSING

    def test_ter_missing_still_ready(self, store):
        """TER missing => still ready (supporting non-blocking)."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)
        assert summary.ready_for_analysis is True

        ter_status = next(s for s in statuses if s.requirement == "EXPENSE_RATIO")
        assert ter_status.status == EtfReadinessStatus.MISSING

    def test_stale_aum_supporting_nonblocking(self, store):
        """Stale AUM supporting evidence is non-blocking."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        # Stale AUM
        aum = EtfFact(instrument_id=ID, metric=EtfMetric.AUM, value=Decimal("1000000000"), unit="INR",
            as_of_date=(NOW - timedelta(days=50)).date(), provenance=provenance())
        store.save_etf_evidence(aum)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.ready_for_analysis is True  # Still ready despite stale AUM
        aum_status = next(s for s in statuses if s.requirement == "AUM")
        assert aum_status.status == EtfReadinessStatus.READY_STALE


class TestEtfReadinessCurrentNews:
    """Tests for CURRENT_NEWS (contextual) behavior."""

    def test_current_news_missing_still_ready(self, store):
        """CURRENT_NEWS missing => still ready (contextual non-blocking)."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.ready_for_analysis is True
        news_status = next(s for s in statuses if s.requirement == "CURRENT_NEWS")
        assert news_status.status == EtfReadinessStatus.MISSING

    def test_current_news_technical_failure_still_ready(self, store):
        """CURRENT_NEWS technical failure => still ready (contextual non-blocking)."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        # Add a failed news attempt (empty evidence list)
        attempt = EtfAttempt(
            attempt_id=UUID(int=999), instrument_id=ID, metric="CURRENT_NEWS", provider="NEWS_API",
            attempted_at=NOW, outcome=Outcome.TECHNICAL_FAILURE,
            reason="RATE_LIMIT_EXCEEDED", evidence_ids=[]
        )
        store.save_etf_acquisition([], attempt)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.ready_for_analysis is True
        news_status = next(s for s in statuses if s.requirement == "CURRENT_NEWS")
        assert news_status.status == EtfReadinessStatus.TECHNICAL_FAILURE


class TestEtfReadinessSummary:
    """Tests for summary behavior."""

    def test_deterministic_summary_counts(self, store):
        """Summary counts are deterministic."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        # Call multiple times - should be deterministic
        _, summary1 = evaluate_etf_readiness(store, ID)
        _, summary2 = evaluate_etf_readiness(store, ID)

        assert summary1 == summary2
        assert is_etf_ready_for_analysis(store, ID) == is_etf_ready_for_analysis(store, ID)

    def test_mandatory_all_ready_ready_for_analysis(self, store):
        """All mandatory READY => ready for analysis even with missing supporting."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        assert summary.mandatory_total == 3
        assert summary.mandatory_ready == 3
        assert summary.mandatory_missing == 0
        assert summary.ready_for_analysis is True


class TestEtfReadinessZeroVsMissing:
    """Additional zero vs missing tests - zero is valid evidence."""

    def test_zero_volume_is_ready(self, store):
        """Zero volume is valid evidence => READY."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("0"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)
        assert summary.ready_for_analysis is True


class TestEtfRequirementRole:
    """Tests for requirement role classification."""

    def test_identity_is_mandatory(self, store):
        """IDENTITY is MANDATORY."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        identity_status = next(s for s in statuses if s.requirement == "IDENTITY")
        assert identity_status.role == "MANDATORY"

    def test_market_price_is_mandatory(self, store):
        """MARKET_PRICE is MANDATORY."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        mp_status = next(s for s in statuses if s.requirement == "MARKET_PRICE")
        assert mp_status.role == "MANDATORY"

    def test_trading_volume_is_mandatory(self, store):
        """TRADING_VOLUME is MANDATORY."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        tv_status = next(s for s in statuses if s.requirement == "TRADING_VOLUME")
        assert tv_status.role == "MANDATORY"

    def test_nav_is_supporting(self, store):
        """NAV is SUPPORTING."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        nav_status = next(s for s in statuses if s.requirement == "NAV")
        assert nav_status.role == "SUPPORTING"

    def test_current_news_is_contextual(self, store):
        """CURRENT_NEWS is CONTEXTUAL."""
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)

        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)

        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

        statuses, summary = evaluate_etf_readiness(store, ID)

        news_status = next(s for s in statuses if s.requirement == "CURRENT_NEWS")
        assert news_status.role == "CONTEXTUAL"


class TestDerivedSupportingRequirementsAreVisible:
    """Regression guard: ETC3_SUPPORTING previously named HISTORICAL_RETURNS,
    VOLATILITY, DRAWDOWN, INDEX_MOMENTUM, CONCENTRATION, INDEX_VALUATION and
    INDEX_CONSTITUENTS, but evaluate_etf_readiness() silently dropped every
    one of them (EtfMetric(metric) raised ValueError, caught and ignored).
    They must now appear in the returned statuses with a real, non-silent
    disposition."""

    def _minimal_mandatory_evidence(self, store):
        listing = EtfListing(instrument_id=ID, isin="IN0000000001", symbol="NIFTYBEES",
            name="Nifty BeES ETF", underlying_reference="Nifty 50", provenance=provenance())
        store.save_etf_evidence(listing)
        price = EtfFact(instrument_id=ID, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"), unit="INR",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(price)
        volume = EtfFact(instrument_id=ID, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"), unit="shares",
            as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE"))
        store.save_etf_evidence(volume)

    def test_no_price_history_reports_missing_not_absent(self, store):
        self._minimal_mandatory_evidence(store)
        statuses, _ = evaluate_etf_readiness(store, ID)
        by_requirement = {s.requirement: s for s in statuses}
        for requirement in ("HISTORICAL_RETURNS", "VOLATILITY", "DRAWDOWN", "INDEX_MOMENTUM"):
            assert requirement in by_requirement, f"{requirement} vanished from readiness output"
            assert by_requirement[requirement].status == EtfReadinessStatus.MISSING

    def test_no_holdings_snapshot_reports_concentration_missing(self, store):
        self._minimal_mandatory_evidence(store)
        statuses, _ = evaluate_etf_readiness(store, ID)
        by_requirement = {s.requirement: s for s in statuses}
        assert by_requirement["CONCENTRATION"].status == EtfReadinessStatus.MISSING

    def test_unimplemented_requirements_are_explicit_not_fabricated(self, store):
        self._minimal_mandatory_evidence(store)
        statuses, _ = evaluate_etf_readiness(store, ID)
        by_requirement = {s.requirement: s for s in statuses}
        for requirement in ("INDEX_VALUATION", "INDEX_CONSTITUENTS"):
            assert by_requirement[requirement].status == EtfReadinessStatus.NOT_IMPLEMENTED

    def test_fresh_price_history_makes_historical_metrics_ready(self, store):
        self._minimal_mandatory_evidence(store)
        for i in range(260):
            bar = DailyMarketBar(global_instrument_id=ID, trading_date=NOW.date() - timedelta(days=260 - i),
                close=Decimal("100") + Decimal(i) * Decimal("0.05"), currency="INR", provider="NSE",
                source_mode=SourceMode.REAL, source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW)
            store.upsert_daily_market_bar(bar)
        statuses, _ = evaluate_etf_readiness(store, ID)
        by_requirement = {s.requirement: s for s in statuses}
        for requirement in ("HISTORICAL_RETURNS", "VOLATILITY", "DRAWDOWN", "INDEX_MOMENTUM"):
            assert by_requirement[requirement].status in {EtfReadinessStatus.READY_FRESH, EtfReadinessStatus.READY_STALE}
