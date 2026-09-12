from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.sector_performance import belongs_to_region, performance_window, rank_performers, rank_top_gainers


def observation(days_ago, price):
    return SimpleNamespace(observed_at=datetime(2026, 9, 7, tzinfo=timezone.utc) - timedelta(days=days_ago), price=Decimal(str(price)))


@pytest.mark.parametrize("period,reference_days", [("DAY", 1), ("WEEK", 7), ("MONTH", 30), ("YEAR", 365)])
def test_performance_uses_nearest_valid_prior_trading_observation(period, reference_days):
    latest, reference, pct = performance_window([observation(reference_days + 1, 100), observation(reference_days, 110), observation(0, 121)], period)
    assert latest.price == Decimal("121")
    assert reference.price == Decimal("110")
    assert pct == Decimal("10")


def test_missing_or_non_positive_reference_is_ineligible():
    assert performance_window([observation(0, 120)], "WEEK") is None
    assert performance_window([observation(8, 0), observation(0, 120)], "WEEK") is None


def test_day_uses_previous_observed_trading_close_not_calendar_subtraction():
    # Monday close must compare with Friday's observation; no Sunday quote is invented.
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    friday = monday - timedelta(days=3)
    latest, reference, pct = performance_window([
        SimpleNamespace(observed_at=friday, price=Decimal("100")),
        SimpleNamespace(observed_at=monday, price=Decimal("105")),
    ], "DAY")
    assert reference.observed_at == friday
    assert pct == Decimal("5")


def test_top_gainers_are_deduplicated_sorted_and_backend_limited():
    rows = [
        {"globalInstrumentId": f"id-{index}", "ticker": f"T{index}", "performancePct": Decimal(10 - index)}
        for index in range(7)
    ] + [{"globalInstrumentId": "id-0", "ticker": "ZZZ", "performancePct": Decimal("1")}]
    result = rank_top_gainers(rows, 99)
    assert [row["globalInstrumentId"] for row in result] == ["id-0", "id-1", "id-2", "id-3", "id-4"]


def test_equal_performance_has_stable_ticker_then_identity_order():
    result = rank_top_gainers([
        {"globalInstrumentId": "b", "ticker": "BBB", "performancePct": Decimal("4")},
        {"globalInstrumentId": "a", "ticker": "AAA", "performancePct": Decimal("4")},
    ])
    assert [row["globalInstrumentId"] for row in result] == ["a", "b"]


def test_best_and_worst_are_independently_limited_and_deterministic():
    rows = [{"globalInstrumentId": f"id-{index}", "ticker": f"T{index}", "performancePct": Decimal(index - 3)} for index in range(7)]
    best, worst = rank_performers(rows, 5)
    assert [row["performancePct"] for row in best] == [Decimal("3"), Decimal("2"), Decimal("1"), Decimal("0"), Decimal("-1")]
    assert [row["performancePct"] for row in worst] == [Decimal("-3"), Decimal("-2"), Decimal("-1"), Decimal("0"), Decimal("1")]


def test_region_membership_is_provider_neutral():
    assert belongs_to_region({"country": "IN", "exchange": "XNSE"}, "INDIA")
    assert belongs_to_region({"country": "US", "exchange": "XNAS"}, "USA")
    assert belongs_to_region({"country": "DE", "exchange": "XETR"}, "EUROPE")
    assert not belongs_to_region({"country": "US", "exchange": "XNAS"}, "EUROPE")
