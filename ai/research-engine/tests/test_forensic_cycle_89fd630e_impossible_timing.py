"""Forensic analysis of controlled cycle 89fd630e-5212-4018-ba6d-09889f83822e.

Issue 3 -- IMPOSSIBLE OPERATION TIMING:
one reported RequirementTimingRecord showed
    queued_at=16:58:56, started_at=17:01:51, completed_at=17:00:35
i.e. started_at AFTER completed_at -- impossible for a single
queued->started->completed span.

Root cause: `CycleTimingRecorder.start_requirement()` reuses one
RequirementTimingRecord per (candidate_id, requirement_id) across every
attempt within a cycle (deliberate -- see the class docstring: elapsed
sub-fields are accumulators meant to span retries). On a SECOND attempt
(a repair-pass retry hitting the same key) it unconditionally overwrote
`started_at` with the new attempt's start time while leaving `completed_at`
set from the FIRST attempt's completion -- so a report produced between
that overwrite and the retry's own completion showed the retry's
(later) started_at next to the first attempt's (earlier) completed_at.

Fix: `start_requirement()` now clears `completed_at` (and the now-stale
`elapsed_ms`) back to None whenever it reuses an existing record, before
setting the new `started_at` -- a fresh attempt starting means the
operation is back in flight, never "complete" from a moment that preceded
this attempt's own start.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.cycle_timing import CycleTimingRecorder


def _assert_ordering(record_dict: dict) -> None:
    queued = record_dict["queued_at"]
    started = record_dict["started_at"]
    completed = record_dict["completed_at"]
    if queued and started:
        assert datetime.fromisoformat(queued) <= datetime.fromisoformat(started), record_dict
    if started and completed:
        assert datetime.fromisoformat(started) <= datetime.fromisoformat(completed), record_dict


def test_single_attempt_ordering_is_sane() -> None:
    recorder = CycleTimingRecorder()
    record = recorder.start_requirement("cand-1", "SHAREHOLDING")
    recorder.complete_requirement(record)
    report = recorder.report()
    (op,) = [o for o in report["operations"] if o["requirement_id"] == "SHAREHOLDING"]
    _assert_ordering(op)
    assert op["completed_at"] is not None


def test_a_retry_on_the_same_requirement_never_produces_a_completed_before_started() -> None:
    """The exact forensic defect: start -> complete (attempt 1) -> start
    again (repair retry) must never leave a stale completed_at from attempt
    1 visible next to attempt 2's later started_at."""
    recorder = CycleTimingRecorder()

    first = recorder.start_requirement("cand-1", "QUARTERLY_FINANCIALS")
    recorder.complete_requirement(first)
    first_completed_at = first.completed_at
    assert first_completed_at is not None

    # Repair-pass retry reuses the exact same (candidate, requirement) key.
    second = recorder.start_requirement("cand-1", "QUARTERLY_FINANCIALS")
    assert second is first  # confirms the record-reuse-by-design behavior

    # A report taken WHILE the retry is still in flight (before its own
    # complete_requirement call) must not show an impossible window.
    mid_retry_report = recorder.report()
    (op,) = [o for o in mid_retry_report["operations"] if o["requirement_id"] == "QUARTERLY_FINANCIALS"]
    assert op["completed_at"] is None, "a fresh attempt in flight must not report a stale prior completion"
    _assert_ordering(op)

    recorder.complete_requirement(second)
    final_report = recorder.report()
    (op2,) = [o for o in final_report["operations"] if o["requirement_id"] == "QUARTERLY_FINANCIALS"]
    _assert_ordering(op2)
    assert datetime.fromisoformat(op2["completed_at"]) >= datetime.fromisoformat(op2["started_at"])


def test_every_emitted_operation_record_satisfies_queued_started_completed_ordering() -> None:
    """Regression required by the forensic task: queued_at <= started_at <=
    completed_at for EVERY emitted operation timing record, across a mix of
    single-attempt and retried requirements."""
    recorder = CycleTimingRecorder()

    plain = recorder.start_requirement("cand-2", "LATEST_PRICE")
    recorder.complete_requirement(plain)

    retried_first = recorder.start_requirement("cand-2", "SHAREHOLDING")
    recorder.complete_requirement(retried_first)
    retried_second = recorder.start_requirement("cand-2", "SHAREHOLDING")
    recorder.complete_requirement(retried_second)

    still_running = recorder.start_requirement("cand-2", "CURRENT_NEWS")  # never completed

    report = recorder.report()
    for op in report["operations"]:
        _assert_ordering(op)
