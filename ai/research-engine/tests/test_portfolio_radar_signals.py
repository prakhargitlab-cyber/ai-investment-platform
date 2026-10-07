from uuid import UUID, uuid4

import pytest

from app.opportunity_persistence import _portfolio_recommendation
from app.persistence import SqliteResearchPersistence


GID_1 = str(UUID(int=1))
GID_2 = str(UUID(int=2))


def publish_signal(
    store,
    *,
    global_instrument_id=GID_1,
    short_action="BUY",
    long_action="ACCUMULATE",
    new_investor_action="BUY_CANDIDATE",
    at="2026-10-01T12:00:00+00:00",
    controlled=False,
):
    cycle_id = str(uuid4())
    snapshot_id = str(uuid4())
    recommendation_id = str(uuid4())
    snapshot = {
        "snapshot_id": snapshot_id,
        "cycle_id": cycle_id,
        "global_instrument_id": global_instrument_id,
        "market": "NSE",
        "generated_at": at,
    }
    recommendation = {
        "recommendation_id": recommendation_id,
        "snapshot_id": snapshot_id,
        "global_instrument_id": global_instrument_id,
        "generated_at": at,
        "recommendation_engine_version": "RECOMMENDATION_ENGINE_V1",
        "fingerprint": str(uuid4()),
        "new_investor_action": new_investor_action,
        "short_term_action": short_action,
        "long_term_action": long_action,
    }
    state = {
        "global_instrument_id": global_instrument_id,
        "latest_recommendation_id": recommendation_id,
        "updated_at": at,
        "current_short_action": short_action,
        "current_long_action": long_action,
    }
    selection = {
        "cycle_id": cycle_id,
        "generated_at": at,
        "market": "NSE",
        "controlled_candidate_set": controlled,
    }
    store.publish_opportunity_cycle([snapshot], [recommendation], [state], selection)
    return cycle_id, recommendation_id


@pytest.mark.parametrize(
    ("action", "new_investor_action", "expected"),
    [
        ("BUY", "BUY_CANDIDATE", "BUY"),
        ("BUY", "STRONG_BUY_CANDIDATE", "STRONG_BUY"),
        ("WAIT", "WATCH_WAIT", "HOLD"),
        ("HOLD", "WATCH_WAIT", "HOLD"),
        ("PARTIAL_EXIT", "WATCH_WAIT", "PARTIAL_EXIT"),
        ("REDUCE", "WATCH_WAIT", "PARTIAL_EXIT"),
        ("EXIT", "AVOID", "FULL_EXIT"),
        ("EXIT_REVIEW", "AVOID", "FULL_EXIT"),
    ],
)
def test_existing_rule_engine_actions_map_to_portfolio_recommendations(
    action, new_investor_action, expected
):
    assert _portfolio_recommendation(action, new_investor_action) == expected


def test_bulk_projection_preserves_both_horizons_and_audit_identity():
    store = SqliteResearchPersistence()
    cycle_id, recommendation_id = publish_signal(
        store,
        short_action="HOLD",
        long_action="ACCUMULATE",
        new_investor_action="STRONG_BUY_CANDIDATE",
    )

    assert store.portfolio_recommendation_signals() == [{
        "globalInstrumentId": GID_1,
        "shortTermRecommendation": "HOLD",
        "longTermRecommendation": "STRONG_BUY",
        "shortTermAction": "HOLD",
        "longTermAction": "ACCUMULATE",
        "newInvestorAction": "STRONG_BUY_CANDIDATE",
        "recommendationAt": "2026-10-01T12:00:00+00:00",
        "evaluatedAt": "2026-10-01T12:00:00+00:00",
        "recommendationId": recommendation_id,
        "cycleId": cycle_id,
        "source": "RECOMMENDATION_CURRENT_STATE",
    }]


def test_bulk_projection_filters_by_canonical_global_instrument_id():
    store = SqliteResearchPersistence()
    publish_signal(store, global_instrument_id=GID_1)
    publish_signal(store, global_instrument_id=GID_2, at="2026-10-01T13:00:00+00:00")

    result = store.portfolio_recommendation_signals(instrument_ids={GID_2})

    assert [signal["globalInstrumentId"] for signal in result] == [GID_2]


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "RUNNING"])
def test_non_authoritative_cycle_job_cannot_replace_published_signal(status):
    store = SqliteResearchPersistence()
    cycle_id, recommendation_id = publish_signal(store)
    before = store.portfolio_recommendation_signals()
    store.record_opportunity_job({
        "cycle_id": str(uuid4()),
        "status": status,
        "updated_at": "2026-10-02T12:00:00+00:00",
        "error_code": "TEST_ONLY",
        "parameters": {},
    })

    after = store.portfolio_recommendation_signals()

    assert after == before
    assert after[0]["cycleId"] == cycle_id
    assert after[0]["recommendationId"] == recommendation_id


def test_controlled_or_intermediate_publication_cannot_replace_current_state():
    store = SqliteResearchPersistence()
    publish_signal(store, short_action="BUY")
    before = store.portfolio_recommendation_signals()
    publish_signal(
        store,
        short_action="EXIT",
        long_action="EXIT_REVIEW",
        new_investor_action="AVOID",
        at="2026-10-02T12:00:00+00:00",
        controlled=True,
    )
    assert store.portfolio_recommendation_signals() == before


def test_newer_authoritative_publication_replaces_older_signal():
    store = SqliteResearchPersistence()
    old_cycle_id, _ = publish_signal(store, short_action="BUY")
    new_cycle_id, new_recommendation_id = publish_signal(
        store,
        short_action="EXIT",
        long_action="EXIT_REVIEW",
        new_investor_action="AVOID",
        at="2026-10-03T12:00:00+00:00",
    )

    signal = store.portfolio_recommendation_signals()[0]

    assert signal["shortTermRecommendation"] == "FULL_EXIT"
    assert signal["longTermRecommendation"] == "FULL_EXIT"
    assert signal["cycleId"] == new_cycle_id
    assert signal["cycleId"] != old_cycle_id
    assert signal["recommendationId"] == new_recommendation_id


def test_new_completed_review_refreshes_evaluation_time_without_rewriting_recommendation_audit():
    store = SqliteResearchPersistence()
    old_cycle_id, recommendation_id = publish_signal(store, short_action="BUY")
    new_cycle_id = str(uuid4())
    snapshot_id = str(uuid4())
    at = "2026-10-04T12:00:00+00:00"
    snapshot = {
        "snapshot_id": snapshot_id,
        "cycle_id": new_cycle_id,
        "global_instrument_id": GID_1,
        "market": "NSE",
        "generated_at": at,
    }
    state = {
        **store.recommendation_states()[0],
        "updated_at": at,
    }
    store.publish_opportunity_cycle(
        [snapshot],
        [],
        [state],
        {
            "cycle_id": new_cycle_id,
            "generated_at": at,
            "market": "NSE",
            "controlled_candidate_set": False,
        },
    )

    signal = store.portfolio_recommendation_signals()[0]

    assert signal["shortTermRecommendation"] == "BUY"
    assert signal["recommendationId"] == recommendation_id
    assert signal["cycleId"] == old_cycle_id
    assert signal["cycleId"] != new_cycle_id
    assert signal["evaluatedAt"] == at


def test_http_contract_forwards_holdings_as_one_bulk_canonical_id_read(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    calls = []

    def load(*, instrument_ids=None):
        calls.append(instrument_ids)
        return []

    monkeypatch.setattr(
        main.repository.persistence,
        "portfolio_recommendation_signals",
        load,
    )

    response = TestClient(main.app).get(
        "/api/v1/research/recommendations/current",
        params=[
            ("global_instrument_id", GID_1),
            ("global_instrument_id", GID_2),
        ],
    )

    assert response.status_code == 200
    assert response.json() == []
    assert calls == [[UUID(GID_1), UUID(GID_2)]]
