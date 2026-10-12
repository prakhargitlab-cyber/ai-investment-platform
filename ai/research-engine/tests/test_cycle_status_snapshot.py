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
    assert snapshot == {"phase": "DEEP", "baseline_evaluated": None, "universe_count": None, "deep_attempted": 0, "deep_completed": 3, "deep_denominator": 4,
                        "deep_ready": 2, "deep_readiness_failed": 1, "deep_technical_failure": 0,
                        "ready": 2, "failed": 1, "technical": 0, "eligible": 1}


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


# Processing completion is independent of readiness and lifecycle status.
import pytest
from app.persistence import SqliteResearchPersistence


def _progress_fixture(terminal, attempted):
    store = SqliteResearchPersistence()
    store.create_cycle_run('progress', {})
    store.claim_cycle_run('progress', 'owner')
    store.update_cycle_run('progress', 'owner', selection={'deep_ids': list(map(str, range(25)))})
    for number in range(attempted):
        store.record_cycle_progress('progress', 'owner', PHASE_DEEP, str(number),
                                    CandidateState.IN_PROGRESS, start_attempt=True)
        if number < terminal:
            store.record_cycle_progress('progress', 'owner', PHASE_DEEP, str(number),
                                        CandidateState.EVIDENCE_UNAVAILABLE)
    return store


@pytest.mark.parametrize('terminal,attempted', [(25, 25), (4, 4), (4, 25)])
def test_terminal_work_counts_do_not_depend_on_readiness(terminal, attempted):
    store = _progress_fixture(terminal, attempted)
    progress = store.cycle_status_snapshot('progress')
    assert store.cycle_run('progress')['status'] == 'RUNNING'
    assert progress['deep_completed'] == terminal
    assert progress['deep_attempted'] == attempted
    assert progress['deep_denominator'] == 25
    assert progress['deep_ready'] == 0
    assert progress['deep_readiness_failed'] == terminal
    assert progress['deep_technical_failure'] == 0
    assert progress['baseline_evaluated'] is None
    assert progress['universe_count'] is None


@pytest.mark.parametrize('status', ['RUNNING', 'PUBLISHED', 'COMPLETED', 'CANCELLED', 'FAILED'])
def test_lifecycle_does_not_redefine_work_or_ready_counters(status):
    store = _progress_fixture(4, 25)
    # Persist a fixture lifecycle directly; never run or cancel a worker.
    with store._connection:
        store._connection.execute('UPDATE global_opportunity_cycle_run SET status=? WHERE cycle_id=?',
                                  (status, 'progress'))
    progress = store.cycle_status_snapshot('progress')
    assert store.cycle_run('progress')['status'] == status
    assert (progress['deep_completed'], progress['deep_ready'], progress['deep_denominator']) == (4, 0, 25)


def test_exhausted_technical_failure_is_terminal_work_not_ready():
    from app.cycle_checkpoint import DEFAULT_MAX_ATTEMPTS
    store = _progress_fixture(0, 0)
    for _ in range(DEFAULT_MAX_ATTEMPTS):
        store.record_cycle_progress('progress', 'owner', PHASE_DEEP, '0',
                                    CandidateState.RETRYABLE_FAILURE, start_attempt=True)
    progress = store.cycle_status_snapshot('progress')
    assert (progress['deep_completed'], progress['deep_ready'], progress['deep_technical_failure']) == (1, 0, 1)


@pytest.mark.parametrize('counts', [{}, {'baseline_evaluated': 700, 'universe_count': 2621},
                                   {'baseline_evaluated': 700}])
def test_market_scan_counts_are_durable_and_never_inferred_from_shortlist(tmp_path, counts):
    database = str(tmp_path / 'progress.sqlite')
    store = SqliteResearchPersistence(database)
    store.create_cycle_run('scan', {})
    store.claim_cycle_run('scan', 'owner')
    store.update_cycle_run('scan', 'owner', selection={
        'phase': 'BASELINE', 'shortlist_ids': list(map(str, range(25))), **counts})
    store._connection.close()
    restored = SqliteResearchPersistence(database)
    try:
        progress = restored.cycle_status_snapshot('scan')
        assert progress['phase'] == 'BASELINE'
        assert progress['baseline_evaluated'] == counts.get('baseline_evaluated')
        assert progress['universe_count'] == counts.get('universe_count')
        assert progress['deep_denominator'] == 25
    finally:
        restored._connection.close()
