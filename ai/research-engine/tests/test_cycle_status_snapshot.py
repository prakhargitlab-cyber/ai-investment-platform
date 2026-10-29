from app.cycle_checkpoint import CandidateState, PHASE_BASELINE, PHASE_DEEP
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
    assert snapshot == {"phase": "DEEP", "baseline_evaluated": None,
                        # No PHASE_BASELINE rows were ever recorded for this cycle,
                        # so the live reconstruction (see status-reporting fix in
                        # cycle_status_snapshot) correctly finds zero terminal rows,
                        # not a fabricated/static admitted count.
                        "baseline_completed": 0, "baseline_failed": 0,
                        # No PHASE_BASELINE rows exist for this cycle either, so
                        # the three new BASELINE state-breakdown fields (Equity
                        # Radar progress-accounting follow-up, Part 2) are their
                        # own zero/None defaults: no row is IN_PROGRESS or an
                        # unexhausted RETRYABLE_FAILURE, and baseline_not_started
                        # is None because universe_count itself is None here.
                        "baseline_in_progress": 0, "baseline_awaiting_retry": 0,
                        "baseline_not_started": None,
                        "universe_count": None, "deep_attempted": 0, "deep_completed": 3, "deep_denominator": 4,
                        "deep_ready": 2, "deep_readiness_failed": 1, "deep_technical_failure": 0,
                        "ready": 2, "failed": 1, "technical": 0, "eligible": 1,
                        # BASELINE's scope (universe_count) was never recorded for this
                        # cycle, so its stage percentage -- and therefore the overall
                        # figure -- stays None rather than silently ignoring an
                        # unmeasured stage. DEEP's own stage percentage is a genuine
                        # completed/admitted ratio (3 of 4 = 75%).
                        "stages": [{"phase": "BASELINE", "completed": 0, "total": None, "percentage": None},
                                   {"phase": "DEEP", "completed": 3, "total": 4, "percentage": 75}],
                        "overall_percentage": None,
                        # Pre-existing fields this test's exact-equality assertion had drifted
                        # out of sync with (unrelated to the baseline_completed/baseline_failed
                        # fields added above) -- this cycle's analysis_scope='FULL' parameter
                        # surfaces here with no admitted candidates yet to compare against.
                        "analysis_scope": "FULL", "requested_shortlist_limit": None, "shortlist_limit_applied": None}


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


# -- Focused regressions for the stages / overall_percentage contract --------
#
# These exercise the "FINAL STABILIZATION" progress requirements directly:
# COMPLETED only reads as 100% when every applicable stage's own evidence
# supports it; FAILED/CANCELLED never read as a successful completion; the
# figures are deterministic and bounded, never derived from elapsed time,
# and remain consistent after a process restart (durable rows only).

@pytest.mark.parametrize('status', ['FAILED', 'CANCELLED'])
def test_failed_and_cancelled_never_report_successful_overall_percentage(status):
    store = _progress_fixture(25, 25)  # every admitted DEEP item already terminal
    store.update_cycle_run('progress', 'owner', selection={
        'deep_ids': list(map(str, range(25))), 'universe': list(map(str, range(25))),
        'universe_count': 25})
    # BASELINE's own percentage must be earned by real durable rows, not
    # by a static 'baseline_evaluated' figure alone (see the status-
    # reporting fix in cycle_status_snapshot) -- record all 25 as
    # genuinely terminal so this test's "every stage already 100%"
    # premise is actually true, not merely asserted via selection metadata.
    for number in range(25):
        store.record_cycle_progress('progress', 'owner', PHASE_BASELINE, str(number),
                                    CandidateState.COMPLETED, disposition='BASELINE_READY',
                                    payload={'has_outcome': True, 'ready': True})
    # Force the terminal run status directly, the same way the existing
    # test_lifecycle_does_not_redefine_work_or_ready_counters fixture does --
    # update_cycle_run's own fencing only allows COMPLETED from RUNNING/
    # PUBLISHED, not FAILED/CANCELLED from RUNNING via this path.
    with store._connection:
        store._connection.execute('UPDATE global_opportunity_cycle_run SET status=? WHERE cycle_id=?',
                                  (status, 'progress'))
    progress = store.cycle_status_snapshot('progress')
    assert progress['phase'] == status
    # Every stage's own completed/total evidence says 100% here -- proving
    # the None below comes specifically from the FAILED/CANCELLED guard,
    # not from missing stage evidence.
    assert progress['stages'][0]['percentage'] == 100
    assert progress['stages'][1]['percentage'] == 100
    assert progress['overall_percentage'] is None


def test_overall_percentage_consistent_and_bounded_across_resumed_cycle(tmp_path):
    database = str(tmp_path / 'stages.sqlite')
    store = SqliteResearchPersistence(database)
    store.create_cycle_run('resume-stage', {})
    store.claim_cycle_run('resume-stage', 'owner')
    store.update_cycle_run('resume-stage', 'owner', selection={
        'deep_ids': list(map(str, range(10))), 'universe': list(map(str, range(10)))})
    for number in range(10):
        store.record_cycle_progress('resume-stage', 'owner', PHASE_BASELINE, str(number),
                                    CandidateState.TERMINAL_OUTCOME)
        store.record_cycle_progress('resume-stage', 'owner', PHASE_DEEP, str(number),
                                    CandidateState.EVIDENCE_UNAVAILABLE)
    before = store.cycle_status_snapshot('resume-stage')
    store._connection.close()

    # Resume: a fresh process reopening the same durable database must
    # reconstruct identical figures -- never inferred from in-memory state or
    # elapsed wall-clock time.
    restored = SqliteResearchPersistence(database)
    try:
        after = restored.cycle_status_snapshot('resume-stage')
        assert after == before
        for stage in after['stages']:
            assert stage['percentage'] is None or 0 <= stage['percentage'] <= 100
        assert after['overall_percentage'] is None or 0 <= after['overall_percentage'] <= 100
        assert after['stages'][0] == {'phase': 'BASELINE', 'completed': 10, 'total': 10, 'percentage': 100}
        assert after['stages'][1] == {'phase': 'DEEP', 'completed': 10, 'total': 10, 'percentage': 100}
        assert after['overall_percentage'] == 100
    finally:
        restored._connection.close()


def test_partially_worked_stage_yields_bounded_intermediate_percentage():
    store = _progress_fixture(6, 25)  # 6 of 25 admitted items terminal, none fully worked
    store.update_cycle_run('progress', 'owner', selection={
        'deep_ids': list(map(str, range(25))), 'universe': list(map(str, range(25))),
        'universe_count': 25})
    progress = store.cycle_status_snapshot('progress')
    # 6/25 = 24%, never fabricated above what the durable rows actually show.
    assert progress['stages'][1]['percentage'] == 24
    assert 0 <= progress['stages'][1]['percentage'] <= 100
    # BASELINE has a known universe but zero recorded BASELINE-phase rows yet
    # -- a real, bounded 0%, not an indeterminate None.
    assert progress['stages'][0] == {'phase': 'BASELINE', 'completed': 0, 'total': 25, 'percentage': 0}
    assert progress['overall_percentage'] == round(100 * 6 / 50)


def test_equity_shaped_cycle_does_not_report_100_percent_baseline_on_admission_alone():
    """Regression for the status-reporting correction (requirement 2D).

    Equity Radar's selection is shaped with deep_ids/shortlist_ids and a
    STATIC baseline_evaluated/universe_count written once at admission
    time (len(scan.candidates) -- see global_opportunity_orchestration.py's
    save_selection calls), never a flat 'universe' list. Before this fix,
    the BASELINE stage's reported 'completed' was that same static
    admitted count, so the stage showed 100% the instant the full
    universe was admitted -- regardless of how many PHASE_BASELINE rows
    had genuinely reached a terminal state. baseline_completed must
    instead track only durable, actually-finished BASELINE rows.
    """
    store = World(0).store
    store.create_cycle_run('equity-shaped', {'analysis_scope': 'FULL'})
    store.claim_cycle_run('equity-shaped', 'owner')
    # Admission: the full universe (2605, abbreviated to 5 here) is
    # recorded as both baseline_evaluated and universe_count -- exactly
    # the Equity Radar shape, with no flat 'universe' list.
    store.update_cycle_run('equity-shaped', 'owner', selection={
        'phase': 'CANDIDATE_SELECTION', 'baseline_evaluated': 5, 'universe_count': 5})
    admitted_only = store.cycle_status_snapshot('equity-shaped')
    assert admitted_only['baseline_evaluated'] == 5
    # The bug this guards against: reporting 100% from admission alone.
    assert admitted_only['stages'][0] == {'phase': 'BASELINE', 'completed': 0, 'total': 5, 'percentage': 0}
    assert admitted_only['baseline_completed'] == 0

    # Only 1 of the 5 admitted candidates has actually finished BASELINE
    # acquisition so far (one ready); a second is still retrying (below
    # DEFAULT_MAX_ATTEMPTS, so not yet terminal -- still genuinely in-flight).
    store.record_cycle_progress('equity-shaped', 'owner', PHASE_BASELINE, '1', CandidateState.COMPLETED,
                                disposition='BASELINE_READY', payload={'has_outcome': True, 'ready': True})
    store.record_cycle_progress('equity-shaped', 'owner', PHASE_BASELINE, '2', CandidateState.RETRYABLE_FAILURE,
                                disposition='BASELINE_INCOMPLETE', payload={'has_outcome': True, 'ready': False},
                                start_attempt=True)
    store.record_cycle_progress('equity-shaped', 'owner', PHASE_BASELINE, '2', CandidateState.RETRYABLE_FAILURE,
                                disposition='BASELINE_INCOMPLETE', payload={'has_outcome': True, 'ready': False},
                                start_attempt=True)
    partial = store.cycle_status_snapshot('equity-shaped')
    # Still 5 admitted (static, unchanged) but only 1 of 5 genuinely
    # terminal (candidate 2's RETRYABLE_FAILURE is at 2 attempts, below
    # DEFAULT_MAX_ATTEMPTS=3, so it is still in-flight, not terminal).
    assert partial['baseline_evaluated'] == 5
    assert partial['baseline_completed'] == 1
    assert partial['stages'][0] == {'phase': 'BASELINE', 'completed': 1, 'total': 5, 'percentage': 20}
    # Equity Radar progress-accounting follow-up (Part 2/5): the three new
    # BASELINE state-breakdown fields exactly partition universe_count (5).
    # Candidate 1 is COMPLETED/ready (counted in baseline_completed only).
    # Candidate 2 is RETRYABLE_FAILURE at 2 of DEFAULT_MAX_ATTEMPTS=3 attempts
    # -- not yet exhausted, so it is genuinely awaiting a bounded repair
    # retry, not "failed" and not "in progress" right now. Candidates 3-5
    # have no PHASE_BASELINE row at all yet (not yet scanned).
    assert partial['baseline_in_progress'] == 0
    assert partial['baseline_awaiting_retry'] == 1
    assert partial['baseline_not_started'] == 3
    # Exact, non-overlapping partition invariant: every admitted instrument is
    # in exactly one of completed/failed/awaiting_retry/in_progress/not_started.
    assert (partial['baseline_completed'] + partial['baseline_awaiting_retry']
            + partial['baseline_in_progress'] + partial['baseline_not_started']) == 5
    # No double counting: baseline_failed (exhausted-only) is a SUBSET of
    # baseline_completed (every terminal row), never added on top of it.
    assert partial['baseline_failed'] <= partial['baseline_completed']


def test_baseline_breakdown_partitions_universe_with_in_progress_and_exhausted_retry():
    """Deterministic fixture covering all five BASELINE buckets at once
    (Equity Radar progress-accounting follow-up, Part 5): ready, exhausted-
    retry failed, awaiting-retry (not exhausted), actively in progress right
    now, and never-touched -- verifying the exact partition invariant and
    that no bucket goes negative or double-counts another.
    """
    store = World(0).store
    store.create_cycle_run('baseline-breakdown', {'analysis_scope': 'FULL'})
    store.claim_cycle_run('baseline-breakdown', 'owner')
    store.update_cycle_run('baseline-breakdown', 'owner', selection={
        'phase': 'CANDIDATE_SELECTION', 'baseline_evaluated': 6, 'universe_count': 6})

    # Candidate 1: ready (ONE terminal row, state=COMPLETED).
    store.record_cycle_progress('baseline-breakdown', 'owner', PHASE_BASELINE, '1', CandidateState.COMPLETED,
                                disposition='BASELINE_READY', payload={'has_outcome': True, 'ready': True})
    # Candidate 2: exhausted retry -- 3 attempts (== DEFAULT_MAX_ATTEMPTS), still
    # RETRYABLE_FAILURE -- this is baseline_failed, distinct from awaiting_retry.
    for _ in range(3):
        store.record_cycle_progress('baseline-breakdown', 'owner', PHASE_BASELINE, '2',
                                    CandidateState.RETRYABLE_FAILURE, disposition='BASELINE_INCOMPLETE',
                                    payload={'has_outcome': True, 'ready': False}, start_attempt=True)
    # Candidate 3: awaiting retry -- only 1 of 3 attempts used.
    store.record_cycle_progress('baseline-breakdown', 'owner', PHASE_BASELINE, '3',
                                CandidateState.RETRYABLE_FAILURE, disposition='BASELINE_INCOMPLETE',
                                payload={'has_outcome': True, 'ready': False}, start_attempt=True)
    # Candidate 4: actively being worked on right now (left at IN_PROGRESS,
    # never finished) -- genuinely in-flight, never a RETRYABLE_FAILURE row.
    store.record_cycle_progress('baseline-breakdown', 'owner', PHASE_BASELINE, '4',
                                CandidateState.IN_PROGRESS, start_attempt=True)
    # Candidates 5 and 6: never touched -- no PHASE_BASELINE row at all.

    snapshot = store.cycle_status_snapshot('baseline-breakdown')
    assert snapshot['baseline_completed'] == 2   # candidate 1 (ready) + candidate 2 (exhausted)
    assert snapshot['baseline_failed'] == 1       # candidate 2 only
    assert snapshot['baseline_awaiting_retry'] == 1   # candidate 3 only
    assert snapshot['baseline_in_progress'] == 1      # candidate 4 only
    assert snapshot['baseline_not_started'] == 2      # candidates 5, 6
    # No negative counts anywhere.
    for field_name in ('baseline_completed', 'baseline_failed', 'baseline_awaiting_retry',
                       'baseline_in_progress', 'baseline_not_started'):
        assert snapshot[field_name] >= 0
    # Exact partition: completed + awaiting_retry + in_progress + not_started == universe.
    # (baseline_failed is a subset of baseline_completed, not a sixth bucket.)
    assert (snapshot['baseline_completed'] + snapshot['baseline_awaiting_retry']
            + snapshot['baseline_in_progress'] + snapshot['baseline_not_started']) == 6


def test_deep_technical_vs_technical_distinguish_exhausted_from_awaiting_retry():
    """Equity Radar progress-accounting follow-up (Part 1): reproduces the
    exact observed scenario (113 DEEP RETRYABLE_FAILURE rows in the database,
    deep_technical_failure == 0 in the API) with a small deterministic
    fixture, and confirms it is NOT a mapping defect or a stale snapshot:
    `technical` counts every CURRENT RETRYABLE_FAILURE row in the admitted
    set regardless of attempts; `deep_technical_failure` counts only the
    subset that has exhausted DEFAULT_MAX_ATTEMPTS (terminal, no further
    retry will happen). A cycle freshly admitted to DEEP can genuinely have
    technical > 0 while deep_technical_failure is still 0.
    """
    store = World(0).store
    store.create_cycle_run('deep-technical', {})
    store.claim_cycle_run('deep-technical', 'owner')
    ids = [str(n) for n in range(5)]
    store.update_cycle_run('deep-technical', 'owner', selection={'deep_ids': ids})
    # Candidates 0, 1, 2: RETRYABLE_FAILURE at 1 of 3 attempts -- awaiting
    # retry, NOT exhausted.
    for key in ids[:3]:
        store.record_cycle_progress('deep-technical', 'owner', PHASE_DEEP, key,
                                    CandidateState.RETRYABLE_FAILURE, start_attempt=True)
    # Candidate 3: analyzed successfully.
    store.record_cycle_progress('deep-technical', 'owner', PHASE_DEEP, '3', CandidateState.COMPLETED,
                                disposition='ANALYZED', start_attempt=True,
                                payload={'diagnostic': {'disposition': 'ANALYZED', 'rank_eligible': True}})
    # Candidate 4: never touched at all.
    snapshot = store.cycle_status_snapshot('deep-technical')
    assert snapshot['technical'] == 3
    assert snapshot['deep_technical_failure'] == 0
    assert snapshot['deep_completed'] == 1
    assert snapshot['deep_attempted'] == 4
    assert snapshot['deep_denominator'] == 5
    # Now exhaust candidate 0's retries (2 more attempts, reaching attempts=3
    # == DEFAULT_MAX_ATTEMPTS): it becomes terminal, moving from
    # 'awaiting retry' into 'exhausted'. technical still counts it (still
    # RETRYABLE_FAILURE), but deep_technical_failure now does too.
    for _ in range(2):
        store.record_cycle_progress('deep-technical', 'owner', PHASE_DEEP, '0',
                                    CandidateState.RETRYABLE_FAILURE, start_attempt=True)
    exhausted = store.cycle_status_snapshot('deep-technical')
    assert exhausted['technical'] == 3          # unchanged -- still 3 RETRYABLE_FAILURE rows total
    assert exhausted['deep_technical_failure'] == 1   # candidate 0 only, now exhausted
    assert exhausted['deep_completed'] == 2            # candidate 3 (analyzed) + candidate 0 (exhausted)
    # DEEP partition invariant: ready + readiness_failed + technical_failure
    # + awaiting_retry + not_yet_attempted == deep_denominator, where
    # awaiting_retry = technical - deep_technical_failure (frontend-side
    # derivation, verified here against the authoritative fields directly).
    awaiting_retry = exhausted['technical'] - exhausted['deep_technical_failure']
    not_yet_attempted = exhausted['deep_denominator'] - exhausted['deep_attempted']
    assert awaiting_retry == 2       # candidates 1, 2
    assert not_yet_attempted == 1     # candidate 4
    assert (exhausted['deep_ready'] + exhausted['deep_readiness_failed']
            + exhausted['deep_technical_failure'] + awaiting_retry + not_yet_attempted
            ) == exhausted['deep_denominator']
    for value in (awaiting_retry, not_yet_attempted, exhausted['deep_technical_failure']):
        assert value >= 0
