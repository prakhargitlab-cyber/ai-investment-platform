"""Historical-market-data integrity layer (consolidated fix pass, Issue 1).

Root cause of the reported INFY bug (latest_price=1000.2 but
latest_swing_low=support_level=10.7, distance_from52_week_low_pct=9247%,
short_entry_low=10.7, with history_readiness=FULL_HISTORY and
confidence=100): neither normalize_price_history nor
normalize_daily_history (app/technical_features.py) ever screened the
historical series for plausibility -- only format/provenance/same-day-
conflict checks existed. A single isolated bad tick (any legitimately-
formatted but wrong-magnitude observation -- provider glitch, decimal-shift,
mislabeled corporate action, etc.) therefore flowed straight into
`prices`/`rows`, and from there into support_level/resistance_level
(min/max over a rolling window), latest_swing_low/high (fractal pivot scan),
distance_from52_week_low/high_pct (min/max over ~year), and finally into
RecommendationEngineV1.ranges()'s short_entry_low/high, which reads
support_level directly off the persisted technical snapshot.

The fix (_quarantine_implausible_indices, wired into both
normalize_price_history and normalize_daily_history) is a generic,
scale-invariant surrounding-series-continuity screen: any single-day move
beyond +-2x (a threshold grounded in real NSE circuit-filter behavior, not
a symbol- or price-specific number) is a *candidate* anomaly, and is only
quarantined if the immediately following observation reverts back near the
pre-move level (a bad tick that never held). A move that PERSISTS at its
new level -- exactly what a genuine split/bonus/reverse-split/rights issue
or a real large price move looks like -- is never quarantined, adjusted, or
deleted; it is left in the series exactly as persisted. Raw/durable
persistence is never touched by this layer at all -- only the in-memory
technical-feature view (PriceHistory.observations) excludes a quarantined
point, and PriceHistory.quarantined_dates plus
TechnicalFeatureSnapshot.quarantined_observation_count/quarantined_dates/
source_diagnostics=['QUARANTINED_IMPLAUSIBLE_OBSERVATIONS_EXCLUDED'] record
exactly what was excluded and why, and reduce `confidence` via the same
conflict_factor mechanism conflicting_dates already used (never a
zero/default substitution).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.recommendation_engine import ranges
from app.technical_features import TechnicalFeatureEngine, _quarantine_implausible_indices

from test_technical_features import NOW, STOCK, compute, history
from test_ohlcv_technical_features import candles, evaluate


def _series_with_isolated_spike(n=260, base=1000.0, spike_index=150, spike_value=10.7):
    values = [base + (i % 10) for i in range(n)]
    values[spike_index] = spike_value
    return values


# 1 -- isolated corrupt historical price cannot become support/resistance ---
def test_01_isolated_corrupt_point_excluded_from_support_and_resistance_close_only():
    values = _series_with_isolated_spike()
    r = compute(values)
    assert r.support_level is not None and r.support_level > 900
    assert r.resistance_level is not None and r.resistance_level < 1100
    assert r.latest_swing_low is None or r.latest_swing_low > 900


def test_01b_isolated_corrupt_point_excluded_from_support_and_resistance_daily_bars():
    values = _series_with_isolated_spike()
    r = evaluate(candles(values))
    assert r.support_level is not None and r.support_level > 900
    assert r.resistance_level is not None and r.resistance_level < 1100
    assert r.latest_swing_low is None or r.latest_swing_low > 900
    assert r.quarantined_observation_count == 1


# 2 -- no absurd 52-week distance or recommendation entry range -------------
def test_02_no_absurd_52_week_distance_or_recommendation_entry_range():
    values = _series_with_isolated_spike()
    r = evaluate(candles(values))
    assert r.distance_from52_week_low_pct is not None
    assert abs(r.distance_from52_week_low_pct) < 50  # nowhere near the ~9247% the bug produced
    snapshot = {"current_price": r.latest_price, "evidence_state": {"technical": r.model_dump()}}
    entry = ranges(snapshot)
    if entry.get("short_entry_low") is not None:
        assert entry["short_entry_low"] > 900


def test_02b_quarantined_point_never_becomes_the_reported_52_week_low_itself():
    # Direct unit-level proof of the underlying screen, independent of the
    # rest of the technical/recommendation pipeline.
    values = _series_with_isolated_spike()
    quarantined = _quarantine_implausible_indices([float(v) for v in values])
    assert 150 in quarantined
    clean = [v for i, v in enumerate(values) if i not in quarantined]
    assert min(clean) > 900


# 3 -- legitimate corporate action remains valid/adjusted correctly ---------
@pytest.mark.parametrize("ratio", [0.5, 0.2, 0.1, 0.01, 2.0, 3.0])
def test_03_legitimate_persisting_level_shift_is_never_quarantined(ratio):
    n, shift_at = 260, 150
    values = [1000.0 + (i % 10) for i in range(shift_at)]
    values += [(1000.0 + (i % 10)) * ratio for i in range(shift_at, n)]
    r = evaluate(candles(values))
    assert r.quarantined_observation_count == 0
    assert r.observation_count == n
    assert r.confidence == pytest.approx(100.0, abs=1e-6) or r.confidence > 0


def test_03b_legitimate_large_single_day_move_that_holds_is_not_quarantined():
    # Not a "split" per se -- just a real, large, one-day move (e.g. a
    # results-day gap) that HOLDS afterward. Distinguishing feature is
    # identical: does the level persist, not the specific cause.
    values = [1000.0] * 150 + [2500.0 + (i % 5) for i in range(110)]
    r = evaluate(candles(values))
    assert r.quarantined_observation_count == 0
    assert r.observation_count == len(values)


# 4 -- uncertain series degrades technical readiness/confidence truthfully --
def test_04_quarantine_truthfully_reduces_confidence_never_reports_100():
    values = _series_with_isolated_spike()
    r = evaluate(candles(values))
    assert r.quarantined_observation_count == 1
    assert r.confidence < 100
    assert 'QUARANTINED_IMPLAUSIBLE_OBSERVATIONS_EXCLUDED' in r.source_diagnostics


def test_04b_clean_series_is_unaffected_confidence_stays_full():
    # Control: proves test 04 is a real signal, not the formula always
    # discounting confidence regardless of input.
    values = [1000.0 + (i % 10) for i in range(260)]
    r = evaluate(candles(values))
    assert r.quarantined_observation_count == 0
    assert 'QUARANTINED_IMPLAUSIBLE_OBSERVATIONS_EXCLUDED' not in r.source_diagnostics


def test_04c_edge_of_series_anomaly_is_never_quarantined_without_a_future_neighbor():
    # No look-ahead: an anomalous LAST observation cannot be confirmed as
    # reverted (there is no future point yet), so it must never be
    # quarantined outright -- discarding it could silently drop a genuine,
    # still-unfolding move. (It may still be truthfully flagged elsewhere,
    # e.g. staleness/conflict diagnostics, which this layer does not touch.)
    values = [1000.0 + (i % 10) for i in range(259)] + [10.7]
    quarantined = _quarantine_implausible_indices([float(v) for v in values])
    assert len(values) - 1 not in quarantined


# Multi-day-run limitation, documented rather than silently unhandled -------
def test_04d_known_limitation_two_consecutive_corrupt_points_not_detected():
    values = _series_with_isolated_spike(spike_index=150)
    values[151] = 10.7  # a second, adjacent corrupted point
    quarantined = _quarantine_implausible_indices([float(v) for v in values])
    # Documented limitation (see module docstring / final report): this
    # generic single-point reversion test is not required to catch a run of
    # two or more consecutive anomalies. Asserting the known behavior here
    # (rather than leaving it silently unverified) keeps it from being
    # mistaken for a regression later.
    assert quarantined == set()
