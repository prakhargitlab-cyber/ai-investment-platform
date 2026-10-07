"""Phase 19 -- Portfolio ETF Integration.

ETF portfolio holdings consume CURRENT GLOBAL ETF Radar intelligence purely
by globalInstrumentId, reusing the single persisted ETF Radar cycle (no
duplication per user/portfolio), with a strict GLOBAL/PRIVATE boundary:

GLOBAL  (this module):  score, recommendation, confidence, ETF metrics
                        (data_completeness), as-of/version.
PRIVATE (never here):   quantity, average cost, position size, gain/loss,
                        account/broker -- these never flow through
                        etf_portfolio_recommendation_signals at all, because
                        the function never receives them in the first
                        place; portfolio-service/frontend merge client-side.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.persistence import SqliteResearchPersistence

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
ID_A = UUID(int=301)
ID_B = UUID(int=302)
ID_UNEVALUATED = UUID(int=303)


def _candidate(instrument_id, *, disposition="EVALUATED", score=72.5, recommendation="OPPORTUNITY",
               confidence="MEDIUM", data_completeness=0.85, symbol="NIFTYBEES"):
    return {
        "global_instrument_id": str(instrument_id),
        "symbol": symbol,
        "disposition": disposition,
        "reason": None,
        "subtype": None,
        "rule_engine_result": {
            "rule_engine_version": "ETF_RULE_ENGINE_V1",
            "instrument_id": str(instrument_id),
            "as_of": NOW.isoformat(),
            "factor_results": [],
            "overall_score": score,
            "data_completeness": data_completeness,
            "confidence": confidence,
        } if disposition == "EVALUATED" else None,
        "risk_gates": [],
        "recommendation": recommendation if disposition == "EVALUATED" else None,
        "rank": 1,
    }


def _seed_cycle(store, candidates, *, cycle_id="cycle-1", radar_version="ETF_RADAR_V1"):
    payload = {
        "radar_version": radar_version,
        "cycle_id": cycle_id,
        "correlation_id": None,
        "as_of": NOW.isoformat(),
        "candidates": candidates,
        "ranked": [c for c in candidates if c["disposition"] == "EVALUATED"],
        "excluded_by_reason": {},
    }
    store.save_etf_radar_cycle(cycle_id, radar_version, None, NOW, payload)
    return payload


@pytest.fixture
def store():
    return SqliteResearchPersistence()


# -- 1. ETF holding matches ETF intelligence by globalInstrumentId -----------

def test_signal_matches_by_global_instrument_id(store):
    _seed_cycle(store, [_candidate(ID_A, score=80.0, recommendation="STRONG_OPPORTUNITY")])
    signals = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A])
    assert len(signals) == 1
    row = signals[0]
    assert row["global_instrument_id"] == str(ID_A)
    assert row["recommendation"] == "STRONG_OPPORTUNITY"
    assert row["score"] == 80.0


def test_signal_filters_to_only_requested_instruments(store):
    _seed_cycle(store, [_candidate(ID_A), _candidate(ID_B, symbol="GOLDBEES")])
    signals = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A])
    assert {row["global_instrument_id"] for row in signals} == {str(ID_A)}


# -- 2. same ETF held by multiple users reuses global intelligence ----------

def test_same_etf_held_by_multiple_users_reuses_identical_global_row(store):
    _seed_cycle(store, [_candidate(ID_A, score=65.0, recommendation="WATCH")])
    # Two independent "users" (no user/portfolio identity passed at all --
    # the function's signature has no such parameter) both look up the same
    # instrument and must get byte-identical global intelligence back.
    user_one_view = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A])
    user_two_view = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A])
    assert user_one_view == user_two_view
    assert user_one_view[0]["cycle_id"] == "cycle-1"


# -- 3. private portfolio fields never enter global ETF intelligence --------

def test_private_fields_never_appear_in_global_signal(store):
    _seed_cycle(store, [_candidate(ID_A)])
    signals = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A])
    row = signals[0]
    private_fields = {"quantity", "average_cost", "averageCost", "position_size", "positionSize",
                       "gain_loss", "gainLoss", "account", "broker", "portfolio_id", "portfolioId",
                       "user_id", "userId"}
    assert not (private_fields & set(row.keys()))
    # The function signature itself cannot leak private data -- it is never
    # given a portfolio, position, or user object to read fields off of.
    import inspect
    params = inspect.signature(store.etf_portfolio_recommendation_signals).parameters
    assert set(params) <= {"instrument_ids"}


# -- 4. missing ETF intelligence is handled truthfully -----------------------

def test_missing_intelligence_is_absent_not_fabricated(store):
    _seed_cycle(store, [_candidate(ID_A)])
    # ID_UNEVALUATED was never part of any persisted cycle.
    signals = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A, ID_UNEVALUATED])
    returned_ids = {row["global_instrument_id"] for row in signals}
    assert returned_ids == {str(ID_A)}  # never a zeroed/fabricated row for ID_UNEVALUATED


def test_no_persisted_cycle_at_all_returns_empty_not_fabricated(store):
    signals = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A])
    assert signals == []


def test_non_evaluated_candidate_is_omitted_not_fabricated(store):
    """An admitted-but-not-yet-evaluated / ineligible candidate must not be
    presented as if it had a real score/recommendation."""
    _seed_cycle(store, [_candidate(ID_A, disposition="INSUFFICIENT_DATA", score=None,
                                    recommendation=None, confidence=None, data_completeness=None)])
    signals = store.etf_portfolio_recommendation_signals(instrument_ids=[ID_A])
    assert len(signals) == 1
    row = signals[0]
    assert row["recommendation"] is None
    assert row["score"] is None
    assert row["disposition"] == "INSUFFICIENT_DATA"


# -- 5. Equity portfolio Radar signal behavior is unchanged ------------------

def test_equity_portfolio_recommendation_signals_untouched(store):
    """Regression guard: the pre-existing Equity projection is a completely
    separate method/table and must behave exactly as before -- in
    particular, it must return [] when nothing has been published yet, same
    as it always has, and must never read from etf_radar_cycles."""
    assert store.portfolio_recommendation_signals(instrument_ids=[ID_A]) == []
    _seed_cycle(store, [_candidate(ID_A, score=99.0, recommendation="STRONG_OPPORTUNITY")])
    # Seeding an ETF cycle must have zero effect on the Equity projection.
    assert store.portfolio_recommendation_signals(instrument_ids=[ID_A]) == []
