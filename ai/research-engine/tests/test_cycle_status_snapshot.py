from app.cycle_checkpoint import CandidateState, PHASE_DEEP
from test_cycle_checkpoint_recovery import World


def test_cycle_status_snapshot_reconstructs_same_cycle_counters():
    world = World(4)
    store = world.store
    cycle_id = "status-cycle"
    store.create_cycle_run(cycle_id, {"analysis_scope": "FULL"})
    assert store.claim_cycle_run(cycle_id, "owner")
    for number, disposition, state in (
        (1, "ANALYZED", CandidateState.COMPLETED),
        (2, "RANK_FILTERED", CandidateState.COMPLETED),
        (3, "DEEP_READINESS_NOT_MET", CandidateState.EVIDENCE_UNAVAILABLE),
    ):
        store.record_cycle_progress(cycle_id, "owner", PHASE_DEEP, str(number), state,
                                    disposition=disposition,
                                    payload={"diagnostic": {"disposition": disposition,
                                                            "rank_eligible": number == 1}})
    store.update_cycle_run(cycle_id, "owner", selection={"deep_ids": ["1", "2", "3", "4"]})
    snapshot = store.cycle_status_snapshot(cycle_id)
    assert snapshot == {"phase": "DEEP", "baseline_evaluated": None, "universe_count": None, "deep_attempted": 0, "deep_completed": 3, "deep_denominator": 4, "ready": 2,
                        "failed": 1, "technical": 0, "eligible": 1}


def test_phase_markers_do_not_invent_baseline_percentages():
    store = World(2).store
    store.create_cycle_run('phases', {})
    store.claim_cycle_run('phases', 'owner')
    initial = store.cycle_status_snapshot('phases')
    assert initial['phase'] == 'BASELINE'
    assert initial['baseline_evaluated'] is None
    store.update_cycle_run('phases', 'owner', selection={
        'phase': 'CANDIDATE_SELECTION', 'baseline_evaluated': 2621, 'universe_count': 2621})
    assert store.cycle_status_snapshot('phases')['phase'] == 'CANDIDATE_SELECTION'
    store.update_cycle_run('phases', 'owner', selection={'deep_ids': ['one', 'two'],
        'baseline_evaluated': 2621, 'universe_count': 2621})
    store.record_cycle_progress('phases', 'owner', PHASE_DEEP, 'one', CandidateState.IN_PROGRESS, start_attempt=True)
    deep = store.cycle_status_snapshot('phases')
    assert (deep['phase'], deep['baseline_evaluated'], deep['deep_attempted'], deep['deep_completed']) == ('DEEP', 2621, 1, 0)


def test_status_ignores_unrelated_malformed_legacy_payload_but_not_own_errors():
    import pytest
    store = World(0).store
    store.record_opportunity_job({'cycle_id': 'wanted', 'status': 'RUNNING', 'updated_at': '2026-10-01'})
    store._connection.execute("INSERT INTO global_opportunity_top_selection VALUES (?,?,?,?)",
        ('bad', '2026-10-02', 'NSE', '{broken json'))
    assert store.opportunity_job('wanted')['status'] == 'RUNNING'
    store._connection.execute("INSERT INTO global_opportunity_top_selection VALUES (?,?,?,?)",
        ('bad-own', '2026-10-03', 'NSE', '{wanted broken json'))
    with pytest.raises(ValueError):
        store.opportunity_job('wanted')
