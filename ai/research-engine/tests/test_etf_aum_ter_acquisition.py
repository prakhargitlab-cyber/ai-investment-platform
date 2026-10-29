"""Focused synthetic fixtures for amount/date parsing through authorized acquisition."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.etf_evidence import EtfMetric as M, etf_freshness, ETF_MAX_AGE
from app.etf_rule_engine import _fresh_fact
from test_etf_document_trust import NOW, ID, HEADER, acquire, store


@pytest.mark.parametrize("amount,expected,currency", [
    ("INR 10 crore", "100000000", "INR"),
    ("INR 10.5 crores", "105000000", "INR"),
    ("Rs. 25 lakh", "2500000", "INR"),
    ("Rs 10 lakhs", "1000000", "INR"),
    ("INR 1,00,000 crore", "1000000000000", "INR"),
    ("INR 1,23,456.78 lakh", "12345678000", "INR"),
    ("INR 10,000,000 million", "10000000000000", "INR"),
    ("INR 1.5 billion", "1500000000", "INR"),
    ("INR 500 mn", "500000000", "INR"),
    ("USD 1.25 bn", "1250000000", "USD"),
    ("EUR 500 mn", "500000000", "EUR"),
    ("GBP 1.5 billion", "1500000000", "GBP"),
    ("INR 0 crore", "0", "INR"),
])
def test_amount_scale_and_currency(store, amount, expected, currency):
    result = acquire(store, HEADER + f"AUM: {amount}")
    assert result.outcome == "SUCCESS_WITH_DATA", result.reason
    fact, = store.etf_facts(ID, M.AUM)
    assert (fact.value, fact.unit) == (Decimal(expected), currency)
    assert fact.as_of_date is None


@pytest.mark.parametrize("symbol", [chr(0x20b9), "INR", "Rs", "Rs."])
def test_rupee_symbols_with_decimal_scales(store, symbol):
    assert acquire(store, HEADER + f"AUM: {symbol}10.5 crore").outcome == "SUCCESS_WITH_DATA"
    fact, = store.etf_facts(ID, M.AUM)
    assert (fact.value, fact.unit) == (Decimal("105000000"), "INR")


@pytest.mark.parametrize("number", ["0", "0.0035", "0.35", "5.00", "100"])
def test_ter_is_percentage_points_without_rescaling(store, number):
    assert acquire(store, HEADER + f"TER: {number}%", M.EXPENSE_RATIO).outcome == "SUCCESS_WITH_DATA"
    fact, = store.etf_facts(ID, M.EXPENSE_RATIO)
    assert (fact.value, fact.unit) == (Decimal(number), "percent")


@pytest.mark.parametrize("value,metric", [
    ("AUM: INR -10 crore", M.AUM), ("AUM: (INR 10 crore)", M.AUM),
    ("AUM: INR 10 crore approximately", M.AUM), ("AUM: INR 10-20 crore", M.AUM),
    ("AUM: INR 1,234,56 crore", M.AUM), ("AUM: INR 10..5 crore", M.AUM),
    ("AUM: INR NaN crore", M.AUM), ("AUM: INR 1e9 crore", M.AUM),
    ("AUM: INR 123456789012345678901234567890 crore", M.AUM),
    ("AUM: INR 0,035 crore", M.AUM),
    ("AUM: USD 10 crore", M.AUM), ("AUM: 10 crore", M.AUM),
    ("TER: -0.35%", M.EXPENSE_RATIO), ("TER: (0.35%)", M.EXPENSE_RATIO),
    ("TER: 0.35", M.EXPENSE_RATIO), ("TER: 35 bps", M.EXPENSE_RATIO),
    ("TER: 0,35%", M.EXPENSE_RATIO), ("TER: 0.35% / 0.5%", M.EXPENSE_RATIO),
    ("TER: 0,035%", M.EXPENSE_RATIO),
    ("TER: 0.35%\nTER: not published", M.EXPENSE_RATIO),
    ("AUM: INR 10 crore\nAUM: INR 11 crore", M.AUM),
])
def test_ambiguous_or_negative_metric_rejected(store, value, metric):
    assert acquire(store, HEADER + value, metric).outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []


@pytest.mark.parametrize("text,expected", [
    ("No metric present", "SUCCESS_EMPTY"),
    ("TER: not disclosed", "GENUINELY_UNAVAILABLE"),
    ("TER: not published", "GENUINELY_UNAVAILABLE"),
])
def test_absence_is_not_fabricated(store, text, expected):
    result = acquire(store, HEADER + text, M.EXPENSE_RATIO)
    assert result.outcome == expected
    assert store.etf_facts(ID) == []
    if expected == "GENUINELY_UNAVAILABLE":
        assert result.unavailability_evidence.source_metadata["scheme_isin"]


@pytest.mark.parametrize("metric,label", [(M.AUM, "AUM: INR 10 crore"), (M.EXPENSE_RATIO, "TER: 0.35%")])
@pytest.mark.parametrize("beyond", [0, 1])
def test_existing_freshness_boundaries_unchanged(store, metric, label, beyond):
    reporting = NOW.date() - ETF_MAX_AGE[metric] - timedelta(days=beyond)
    result = acquire(store, HEADER + f"{label} (as of {reporting})", metric)
    assert result.outcome == "SUCCESS_WITH_DATA", result.reason
    fact, = store.etf_facts(ID, metric)
    assert etf_freshness(fact, metric, now=NOW) == ("STALE" if beyond else "FRESH")
    assert (_fresh_fact(fact, metric, now=NOW) is None) == bool(beyond)


@pytest.mark.parametrize("suffix", [
    "(as of 09/10/2026)", "(as of 2026-02-30)",
    "(as of 2026-09-30)\nAUM reporting date: 2026-10-01",
])
def test_ambiguous_invalid_or_conflicting_dates_rejected(store, suffix):
    assert acquire(store, HEADER + "AUM: INR 10 crore " + suffix).outcome == "TECHNICAL_FAILURE"
    assert store.etf_facts(ID) == []
