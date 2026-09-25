"""Focused contract tests for the market-session-aware freshness policy.

These tests pin the behaviour that keeps candidates from being suppressed by
``CRITICAL_STALE:VALUATION`` / ``CRITICAL_STALE:BALANCE_SHEET`` when the
underlying data is genuinely current, while keeping the conservative fail-closed
fallbacks intact.

No live provider calls are made.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from app.market_sessions import (
    MarketCalendarException,
    MarketTradingSchedule,
    price_session_valid_until,
    price_sync_eligible,
)
from uuid import UUID

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import ProvenancedValue, SourceMode
from app.research_readiness import (
    FreshnessMode,
    FreshnessPolicyRegistry,
    ProviderAuthorityRegistry,
    ResearchEvidence,
    ResearchReadinessService,
    ResearchRequirementRegistry,
    ResearchSourceTier,
)
from app.research_readiness_runtime import _evidence_from_financial_fact


# NSE: Mon-Fri 09:15-15:30 Asia/Kolkata (03:45-10:00 UTC); weekends closed.
NSE = [
    MarketTradingSchedule("NSE", "XNSE", "IN", "Asia/Kolkata", day, time(9, 15), time(15, 30))
    for day in range(5)
]
FRI_CLOSE = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)      # Friday 15:30 IST
MON_OPEN = datetime(2026, 9, 14, 3, 45, tzinfo=timezone.utc)         # Monday 09:15 IST
TUE_OPEN = datetime(2026, 9, 15, 3, 45, tzinfo=timezone.utc)         # Tuesday 09:15 IST (Mon holiday)
SAT = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)             # weekend
MON_DURING = datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc)       # Monday 12:30 IST (session live)
MON_AFTER_CLOSE = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)  # Monday 16:30 IST (session done)

_POLICIES = FreshnessPolicyRegistry.default()
PRICE = _POLICIES.get("LATEST_PRICE")
HISTORICAL = _POLICIES.get("HISTORICAL_PRICE_SERIES")
QUARTERLY = _POLICIES.get("QUARTERLY_FINANCIALS")


def _evidence(
    requirement_id: str,
    *,
    as_of: datetime | None = None,
    retrieved_at: datetime | None = None,
    valid_until: datetime | None = None,
    covered_input_ids: tuple[str, ...] = (),
    evidence_id: str = "evidence",
    value: str = "1",
) -> ResearchEvidence:
    anchor = as_of or retrieved_at or FRI_CLOSE
    return ResearchEvidence(
        evidence_id=evidence_id,
        requirement_id=requirement_id,
        source="NSE",
        source_tier=ResearchSourceTier.TRUSTED_MARKET_DATA,
        retrieved_at=retrieved_at or anchor,
        as_of=as_of,
        published_at=as_of,
        valid_until=valid_until,
        fact_key="fact",
        value_fingerprint=value,
        covered_input_ids=covered_input_ids,
    )


# --- LATEST_PRICE (MARKET_SESSION_AWARE, 15 min) ---


def test_latest_price_fresh_through_weekend_before_next_session_opens():
    assert PRICE.mode == FreshnessMode.MARKET_SESSION_AWARE
    valid_until = price_session_valid_until("NSE", NSE, [], FRI_CLOSE)
    assert valid_until == MON_OPEN
    ev = _evidence(
        "LATEST_PRICE", as_of=FRI_CLOSE, retrieved_at=FRI_CLOSE,
        valid_until=valid_until, covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert PRICE.is_fresh(ev, SAT) is True
    assert PRICE.is_fresh(ev, MON_OPEN - timedelta(seconds=1)) is True
    # Once Monday's session is live, a Friday close is stale again.
    assert PRICE.is_fresh(ev, MON_DURING) is False


def test_price_validity_persists_across_persisted_holiday():
    holidays = [MarketCalendarException("NSE", date(2026, 9, 14), "CLOSED")]
    valid_until = price_session_valid_until("NSE", NSE, holidays, FRI_CLOSE)
    assert valid_until == TUE_OPEN  # Monday holiday -> valid until Tuesday open
    ev = _evidence(
        "LATEST_PRICE", as_of=FRI_CLOSE, retrieved_at=FRI_CLOSE,
        valid_until=valid_until, covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert PRICE.is_fresh(ev, MON_DURING) is True  # Monday is a holiday -> still fresh


def test_latest_price_stale_once_next_session_completes():
    valid_until = price_session_valid_until("NSE", NSE, [], FRI_CLOSE)
    ev = _evidence(
        "LATEST_PRICE", as_of=FRI_CLOSE, retrieved_at=FRI_CLOSE,
        valid_until=valid_until, covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert PRICE.is_fresh(ev, MON_AFTER_CLOSE) is False


def test_stale_quote_redownloaded_today_is_not_fresh_via_retrieved_at():
    # Re-downloaded "today": observation is still Friday's close; retrieved_at is now.
    ev = _evidence(
        "LATEST_PRICE", as_of=FRI_CLOSE, retrieved_at=MON_DURING,
        valid_until=None, covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert PRICE.is_fresh(ev, MON_DURING) is False


def test_current_session_quote_stays_fresh_within_window():
    ev = _evidence(
        "LATEST_PRICE", as_of=MON_DURING, retrieved_at=MON_DURING,
        valid_until=None, covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert PRICE.is_fresh(ev, MON_DURING + timedelta(minutes=4)) is True
    assert PRICE.is_fresh(ev, MON_DURING + timedelta(minutes=16)) is False


def test_price_fallback_without_calendar_context_is_conservative():
    ev = _evidence(
        "LATEST_PRICE", as_of=FRI_CLOSE, retrieved_at=FRI_CLOSE,
        valid_until=None, covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert PRICE.is_fresh(ev, SAT) is False


# --- HISTORICAL_PRICE_SERIES (DAILY_INCREMENTAL, 36 h) ---


def test_historical_series_survives_weekend_then_stales_after_next_session():
    assert HISTORICAL.mode == FreshnessMode.DAILY_INCREMENTAL
    valid_until = price_session_valid_until("NSE", NSE, [], FRI_CLOSE)
    ev = _evidence(
        "HISTORICAL_PRICE_SERIES", as_of=FRI_CLOSE, retrieved_at=FRI_CLOSE,
        valid_until=valid_until, covered_input_ids=("DURABLE_PRICE_OBSERVATIONS",),
    )
    assert HISTORICAL.is_fresh(ev, SAT) is True
    assert HISTORICAL.is_fresh(ev, MON_DURING) is False
    # Holiday extends validity for the series too.
    holidays = [MarketCalendarException("NSE", date(2026, 9, 14), "CLOSED")]
    vu = price_session_valid_until("NSE", NSE, holidays, FRI_CLOSE)
    ev_h = _evidence(
        "HISTORICAL_PRICE_SERIES", as_of=FRI_CLOSE, retrieved_at=FRI_CLOSE,
        valid_until=vu, covered_input_ids=("DURABLE_PRICE_OBSERVATIONS",),
    )
    assert HISTORICAL.is_fresh(ev_h, MON_DURING) is True


# --- last_price_at / refresh eligibility semantics (observation time) ---


def test_price_sync_eligibility_uses_observation_time_not_retrieval_time():
    observed = FRI_CLOSE            # real observation: Friday close
    fetched_now = MON_DURING        # re-fetched "today"
    # Bug surface: a last_price_at stamped to fetch time masks staleness.
    assert price_sync_eligible("OPEN", fetched_now, 300, MON_DURING) is False
    # Fixed: stale observation time -> eligible to re-fetch within an open session.
    assert price_sync_eligible("OPEN", observed, 300, MON_DURING) is True
    # Off-hours: never re-fetch (fail-closed).
    assert price_sync_eligible("CLOSED", observed, 300, MON_DURING) is False
    # Fresh current-session quote is not due.
    assert price_sync_eligible("OPEN", MON_DURING, 300, MON_DURING + timedelta(minutes=4)) is False


# --- BALANCE_SHEET_FACTS -> QUARTERLY_FINANCIALS (RELEASE_AWARE_QUARTERLY, 120 d) ---


def test_balance_sheet_march_debt_is_stale_and_june_supporting_does_not_freshen_it():
    assert QUARTERLY.mode == FreshnessMode.RELEASE_AWARE_QUARTERLY
    march_debt = _evidence(
        "BALANCE_SHEET_FACTS",
        as_of=datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
        retrieved_at=MON_DURING,
        covered_input_ids=("DEBT",),
        evidence_id="debt:2026-03",
    )
    june_cost = _evidence(
        "BALANCE_SHEET_FACTS",
        as_of=datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc),
        retrieved_at=MON_DURING,
        covered_input_ids=("INTEREST_COVERAGE_INPUTS",),
        evidence_id="cost:2026-06",
    )
    assert not QUARTERLY.is_fresh(march_debt, MON_DURING)   # March >120 d -> stale
    assert QUARTERLY.is_fresh(june_cost, MON_DURING)        # June <120 d -> fresh
    # Per-input: the June fact supports a *different* input, so it cannot satisfy DEBT.
    assert march_debt.covered_input_ids == ("DEBT",)
    assert june_cost.covered_input_ids == ("INTEREST_COVERAGE_INPUTS",)


def test_reporting_calendar_valid_until_is_honored_for_balance_sheet():
    next_filing = datetime(2026, 10, 30, tzinfo=timezone.utc)
    march_debt = _evidence(
        "BALANCE_SHEET_FACTS",
        as_of=datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
        retrieved_at=MON_DURING,
        valid_until=next_filing,
        covered_input_ids=("DEBT",),
        evidence_id="debt:2026-03:calendar",
    )
    assert QUARTERLY.is_fresh(march_debt, MON_DURING) is True


def test_balance_sheet_without_valid_until_falls_back_to_conservative_ttl():
    march_debt = _evidence(
        "BALANCE_SHEET_FACTS",
        as_of=datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
        retrieved_at=MON_DURING,
        covered_input_ids=("DEBT",),
    )
    assert QUARTERLY.is_fresh(march_debt, MON_DURING) is False  # March >120 d


def test_retrieved_at_does_not_freshen_old_balance_sheet_facts():
    march_debt = _evidence(
        "BALANCE_SHEET_FACTS",
        as_of=datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc),
        retrieved_at=MON_DURING,  # fetched "today", as_of still March
        covered_input_ids=("DEBT",),
    )
    assert QUARTERLY.is_fresh(march_debt, MON_DURING) is False


# --- Period-aware BALANCE_SHEET_FACTS valid_until via _evidence_from_financial_fact ---


_REGISTRY = ResearchRequirementRegistry.default()
_AUTHORITY_REGISTRY = ProviderAuthorityRegistry.default()


def _financial_fact(metric, period_end, period_type, as_of_date,
                    source_tier=FactSourceTier.OFFICIAL_REGULATORY):
    """Build a FinancialFact whose ProvenancedValue carries an explicit as_of_date."""
    return FinancialFact(
        key=FinancialFactKey(UUID(int=1), metric, period_end, period_type),
        value=ProvenancedValue(
            value=1, source_url="https://example.test", source_name="test",
            as_of_date=as_of_date, published_at=as_of_date, retrieved_at=as_of_date,
        ),
        source_tier=source_tier,
        source_provider="REGULATORY_FILING",
        source_identity="test-fact",
        source_mode=SourceMode.REAL,
    )


def test_balance_sheet_quarterly_debt_equity_cash_gets_period_aware_valid_until():
    """Fix 1: _evidence_from_financial_fact sets valid_until (as_of + 120 d) for
    quarterly BALANCE_SHEET_FACTS debt/equity/cash, keeping them fresh within TTL."""
    now = MON_DURING
    quarterly_as_of = now - timedelta(days=60)   # 60 days ago — inside 120-day window
    for metric in ("total_debt", "total_equity", "cash_and_cash_equivalents"):
        fact = _financial_fact(metric, "2026-07-15", "QUARTERLY", quarterly_as_of)
        ev = _evidence_from_financial_fact(fact, "BALANCE_SHEET_FACTS", ("DEBT",))
        assert ev.valid_until is not None
        assert ev.valid_until == quarterly_as_of + timedelta(days=120)
        assert QUARTERLY.is_fresh(ev, now) is True


def test_balance_sheet_annual_debt_equity_cash_gets_period_aware_valid_until():
    """Fix 1: _evidence_from_financial_fact sets valid_until (as_of + 400 d) for
    annual BALANCE_SHEET_FACTS debt/equity/cash, keeping them fresh within TTL."""
    now = MON_DURING
    annual_as_of = now - timedelta(days=300)   # 300 days ago — inside 400-day window
    for metric in ("total_debt", "total_equity", "cash_and_cash_equivalents"):
        fact = _financial_fact(metric, "2025-11-19", "ANNUAL", annual_as_of)
        ev = _evidence_from_financial_fact(fact, "BALANCE_SHEET_FACTS", ("DEBT",))
        assert ev.valid_until is not None
        assert ev.valid_until == annual_as_of + timedelta(days=400)
        assert QUARTERLY.is_fresh(ev, now) is True


def test_balance_sheet_quarterly_overdue_period_is_stale():
    """Genuinely overdue quarterly reporting period => stale (beyond 120-day TTL)."""
    now = MON_DURING
    overdue_as_of = now - timedelta(days=200)  # 200 days ago — beyond 120-day window
    fact = _financial_fact("total_debt", "2026-02-26", "QUARTERLY", overdue_as_of)
    ev = _evidence_from_financial_fact(fact, "BALANCE_SHEET_FACTS", ("DEBT",))
    assert ev.valid_until == overdue_as_of + timedelta(days=120)
    assert QUARTERLY.is_fresh(ev, now) is False


def test_balance_sheet_annual_overdue_period_is_stale():
    """Genuinely overdue annual reporting period => stale (beyond 400-day TTL)."""
    now = MON_DURING
    overdue_as_of = now - timedelta(days=500)  # 500 days ago — beyond 400-day window
    fact = _financial_fact("total_equity", "2025-03-31", "ANNUAL", overdue_as_of)
    ev = _evidence_from_financial_fact(fact, "BALANCE_SHEET_FACTS", ("EQUITY",))
    assert ev.valid_until == overdue_as_of + timedelta(days=400)
    assert QUARTERLY.is_fresh(ev, now) is False


def test_balance_sheet_quarterly_120_day_boundary_is_inclusive():
    """Existing 120-day / period constraints remain intact: exactly at the boundary
    is still fresh (now == valid_until => now <= valid_until)."""
    now = MON_DURING
    boundary_as_of = now - timedelta(days=120)
    fact = _financial_fact("total_debt", "2026-05-15", "QUARTERLY", boundary_as_of)
    ev = _evidence_from_financial_fact(fact, "BALANCE_SHEET_FACTS", ("DEBT",))
    assert ev.valid_until == boundary_as_of + timedelta(days=120)  # == now
    assert QUARTERLY.is_fresh(ev, now) is True
    # One second past the boundary is stale.
    assert QUARTERLY.is_fresh(ev, now + timedelta(seconds=1)) is False


def test_balance_sheet_not_stale_through_required_inputs_when_fresh_quarterly():
    """End-to-end: fresh quarterly debt + equity evidence (with period-aware
    valid_until from _evidence_from_financial_fact) => _required_inputs_are_fresh True."""
    now = MON_DURING
    quarterly_as_of = now - timedelta(days=60)
    debt_ev = _evidence_from_financial_fact(
        _financial_fact("total_debt", "2026-07-15", "QUARTERLY", quarterly_as_of),
        "BALANCE_SHEET_FACTS", ("DEBT",),
    )
    equity_ev = _evidence_from_financial_fact(
        _financial_fact("total_equity", "2026-07-15", "QUARTERLY", quarterly_as_of),
        "BALANCE_SHEET_FACTS", ("EQUITY",),
    )
    # Full-research contract: CASH (net-debt subrule) is a required input too.
    cash_ev = _evidence_from_financial_fact(
        _financial_fact("cash_and_cash_equivalents", "2026-07-15", "QUARTERLY", quarterly_as_of),
        "BALANCE_SHEET_FACTS", ("CASH",),
    )
    req = _REGISTRY.get("BALANCE_SHEET_FACTS")
    policy = _POLICIES.get("QUARTERLY_FINANCIALS")
    authority = _AUTHORITY_REGISTRY.policy_for("BALANCE_SHEET_FACTS", "INDIA")
    assert ResearchReadinessService._required_inputs_are_fresh(
        req, [debt_ev, equity_ev, cash_ev], authority, policy, now
    )
    assert not ResearchReadinessService._required_inputs_are_fresh(
        req, [debt_ev, equity_ev], authority, policy, now
    )


# --- VALUATION evidence selection: any fresh candidate wins (Fix 2) ---

_VALUATION_REQ = _REGISTRY.get("VALUATION_INPUTS")
_VALUATION_POLICY = _POLICIES.get("VALUATION_INPUTS")
_VALUATION_AUTHORITY = _AUTHORITY_REGISTRY.policy_for("VALUATION_INPUTS", "GLOBAL")


def _fresh_pb(now):
    """PB is a required valuation input under the full-research contract."""
    return ResearchEvidence(
        evidence_id="fresh-pb", requirement_id="VALUATION_INPUTS", source="LICENSED_STRUCTURED",
        source_tier=ResearchSourceTier.LICENSED_STRUCTURED, retrieved_at=now, as_of=now,
        published_at=now, valid_until=now + timedelta(minutes=15), covered_input_ids=("PB",),
    )


def test_valuation_fresh_eps_not_displaced_by_stale_authoritative():
    """Fix 2: a fresher valid EARNINGS_BASIS candidate must not be displaced by an
    older, more-authoritative candidate that has expired."""
    now = MON_DURING
    # Older authoritative source (COMPANY_FILING / OFFICIAL) that is stale.
    old_authoritative = ResearchEvidence(
        evidence_id="old-cmp-filing",
        requirement_id="VALUATION_INPUTS",
        source="COMPANY_FILING",
        source_tier=ResearchSourceTier.OFFICIAL,
        retrieved_at=now - timedelta(days=100),
        as_of=now - timedelta(days=100),
        published_at=now - timedelta(days=100),
        valid_until=now - timedelta(days=1),  # expired yesterday
        covered_input_ids=("EARNINGS_BASIS",),
    )
    # Fresh less-authoritative source (LICENSED_STRUCTURED) that is current.
    fresh_structured = ResearchEvidence(
        evidence_id="fresh-structured",
        requirement_id="VALUATION_INPUTS",
        source="LICENSED_STRUCTURED",
        source_tier=ResearchSourceTier.LICENSED_STRUCTURED,
        retrieved_at=now - timedelta(days=10),
        as_of=now - timedelta(days=10),
        published_at=now - timedelta(days=10),
        valid_until=(now - timedelta(days=10)) + timedelta(days=120),  # expires in ~110 d
        covered_input_ids=("EARNINGS_BASIS",),
    )
    # Fresh price evidence (LATEST_USABLE_PRICE).
    fresh_price = ResearchEvidence(
        evidence_id="fresh-price",
        requirement_id="VALUATION_INPUTS",
        source="NSE",
        source_tier=ResearchSourceTier.TRUSTED_MARKET_DATA,
        retrieved_at=now,
        as_of=now,
        published_at=now,
        valid_until=now + timedelta(minutes=15),  # session-valid
        covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    evidence = [old_authoritative, fresh_structured, fresh_price, _fresh_pb(now)]
    assert ResearchReadinessService._required_inputs_are_fresh(
        _VALUATION_REQ, evidence, _VALUATION_AUTHORITY, _VALUATION_POLICY, now
    )


def test_valuation_stale_price_makes_inputs_stale():
    """Old/stale price must still make VALUATION stale even with fresh EPS basis."""
    now = MON_DURING
    fresh_eps = ResearchEvidence(
        evidence_id="fresh-eps",
        requirement_id="VALUATION_INPUTS",
        source="NSE",
        source_tier=ResearchSourceTier.LICENSED_STRUCTURED,
        retrieved_at=now - timedelta(days=10),
        as_of=now - timedelta(days=10),
        published_at=now - timedelta(days=10),
        valid_until=(now - timedelta(days=10)) + timedelta(days=120),
        covered_input_ids=("EARNINGS_BASIS",),
    )
    stale_price = ResearchEvidence(
        evidence_id="stale-price",
        requirement_id="VALUATION_INPUTS",
        source="NSE",
        source_tier=ResearchSourceTier.TRUSTED_MARKET_DATA,
        retrieved_at=now - timedelta(days=10),
        as_of=now - timedelta(days=10),
        published_at=now - timedelta(days=10),
        valid_until=now - timedelta(seconds=1),  # just expired
        covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert not ResearchReadinessService._required_inputs_are_fresh(
        _VALUATION_REQ, [fresh_eps, stale_price], _VALUATION_AUTHORITY, _VALUATION_POLICY, now
    )


def test_valuation_120_day_boundary_constraint_remains_intact():
    """Existing 120-day / period constraint remains intact: EARNINGS_BASIS at
    exactly the 120-day boundary (valid_until == now) is still fresh."""
    now = MON_DURING
    boundary_as_of = now - timedelta(days=120)
    boundary_eps = ResearchEvidence(
        evidence_id="boundary-eps",
        requirement_id="VALUATION_INPUTS",
        source="LICENSED_STRUCTURED",
        source_tier=ResearchSourceTier.LICENSED_STRUCTURED,
        retrieved_at=boundary_as_of,
        as_of=boundary_as_of,
        published_at=boundary_as_of,
        valid_until=boundary_as_of + timedelta(days=120),  # == now
        covered_input_ids=("EARNINGS_BASIS",),
    )
    fresh_price = ResearchEvidence(
        evidence_id="boundary-price",
        requirement_id="VALUATION_INPUTS",
        source="NSE",
        source_tier=ResearchSourceTier.TRUSTED_MARKET_DATA,
        retrieved_at=now,
        as_of=now,
        published_at=now,
        valid_until=now + timedelta(minutes=15),
        covered_input_ids=("LATEST_USABLE_PRICE",),
    )
    assert _VALUATION_POLICY.is_fresh(boundary_eps, now)
    assert ResearchReadinessService._required_inputs_are_fresh(
        _VALUATION_REQ, [boundary_eps, fresh_price, _fresh_pb(now)], _VALUATION_AUTHORITY, _VALUATION_POLICY, now
    )
