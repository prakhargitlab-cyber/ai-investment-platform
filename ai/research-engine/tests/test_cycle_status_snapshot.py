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
    assert snapshot == {"deep_completed": 3, "deep_denominator": 4, "ready": 2,
                        "failed": 1, "technical": 0, "eligible": 1}
