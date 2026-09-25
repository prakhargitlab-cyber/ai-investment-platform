"""Focused tests for the GlobalOpportunityScheduler and its interaction with
the NSE market calendar.

Categories covered:
  H — scheduler submits an hourly cycle during market hours
  I — scheduler submits a post-close run after session close (within 2h window)
  J — scheduler does NOT run during holidays or weekends
  K — scheduler prevents duplicate/overlapping runs (active worker check)
  L — scheduler recovers last run time on restart (no duplicate in-flight)
  M — last successful snapshot is served when a scheduled submit fails
  N — normal API/dashboard GET does not trigger a scan
"""
from datetime import datetime, timedelta, timezone, date, time as dtime
from uuid import uuid4

import pytest

from app.global_opportunity_scheduler import GlobalOpportunityScheduler
from app.market_sessions import (
    market_session_status, latest_completed_session,
    MarketTradingSchedule, MarketCalendarException,
)
from app.persistence import SqliteResearchPersistence

# Reuse the snapshot/publish harness.
from test_recommendation_lifecycle import snapshot, publish


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeWorker:
    """Minimal stand-in for OpportunityCycleWorker — records submissions and
    can simulate an active run or a submit failure."""

    def __init__(self, submit_raises: bool = False):
        self.submissions: list[dict] = []
        self.active: dict | None = None
        self.submit_raises = submit_raises

    def start(self):
        pass

    def submit(self, parameters: dict) -> dict:
        self.submissions.append(parameters)
        if self.submit_raises:
            raise RuntimeError("SIMULATED_SUBMIT_FAILURE")
        result = dict(
            cycle_id=f"fake-cycle-{len(self.submissions)}",
            status="ACCEPTED",
            updated_at=datetime.now(timezone.utc).isoformat(),
            parameters=parameters,
        )
        self.active = result
        return result


# NSE regular session: Mon–Fri 09:15–15:30 Asia/Kolkata  (UTC+5:30)
#   open  = 03:45 UTC, close = 10:00 UTC
_MARKET_OPEN_UTC = dtime(3, 45)
_MARKET_CLOSE_UTC = dtime(10, 0)

# 2026-09-14 is a Monday (weekday 0).  Sep 13 is Sunday.
_MONDAY = date(2026, 9, 14)


def _utc(hour: int, minute: int = 0, day: date = _MONDAY) -> datetime:
    """Build an aware UTC datetime for *day* at hour:minute."""
    return datetime.combine(day, dtime(hour, minute), tzinfo=timezone.utc)


def _market_hours_now() -> datetime:
    """Monday 06:00 UTC = 11:30 AM IST (well within the NSE session)."""
    return _utc(6, 0)


def _post_close_now() -> datetime:
    """Monday 10:30 UTC = 4:00 PM IST (30 min after 3:30 PM close)."""
    return _utc(10, 30)


def _weekend_now() -> datetime:
    """Sunday 06:00 UTC = 11:30 AM IST."""
    return _utc(6, 0, date(2026, 9, 13))


# ---------------------------------------------------------------------------
# H — scheduler submits an hourly cycle during market hours
# ---------------------------------------------------------------------------

def test_category_h_hourly_submission_during_market_hours():
    """When the clock is within NSE market hours, _maybe_submit submits a
    production cycle with candidate_ids=None."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
    )
    submitted = sched._maybe_submit()
    assert submitted is True
    assert len(worker.submissions) == 1
    params = worker.submissions[0]
    assert params["candidate_ids"] is None
    assert params["top_n"] == 4
    assert params["shortlist_limit"] == 25


def test_category_h_uses_nse_market_hours_and_skips_when_closed():
    """Outside market hours the scheduler must not submit a market-hours cycle."""
    store = SqliteResearchPersistence()
    # Monday 02:00 UTC = 7:30 AM IST — before open (09:15 IST).
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(2, 0),
    )
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 0


# ---------------------------------------------------------------------------
# I — scheduler submits a post-close run after session close
# ---------------------------------------------------------------------------

def test_category_i_post_close_run_within_window():
    """After the NSE close (within 2 h window) the scheduler submits a
    post-close cycle."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _post_close_now(),
    )
    submitted = sched._maybe_submit()
    assert submitted is True
    assert len(worker.submissions) == 1
    assert worker.submissions[0]["candidate_ids"] is None


def test_category_i_no_post_close_beyond_window():
    """More than 2 hours after close, no post-close run is submitted."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    # Monday 13:00 UTC = 6:30 PM IST — 3.5 h after close.
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(13, 0),
    )
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 0


# ---------------------------------------------------------------------------
# J — scheduler does not run during holidays or weekends
# ---------------------------------------------------------------------------

def test_category_j_no_runs_during_weekend():
    """On a Sunday (no NSE schedule), market_session_status returns CLOSED
    and latest_completed_session is too far back for a post-close window."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _weekend_now(),
    )
    # Verify the market status is indeed CLOSED (not OPEN).
    schedules, exceptions = sched._load_schedules()
    status = market_session_status("NSE", schedules, exceptions, _weekend_now())
    assert status == "CLOSED"
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 0


def test_category_j_no_runs_during_holiday():
    """When a CLOSED calendar exception marks a trading day as a holiday,
    market_session_status returns HOLIDAY and the scheduler must not run."""
    store = SqliteResearchPersistence()
    # Insert a holiday exception for Monday Sep 14.
    store._connection.execute(
        "INSERT INTO market_trading_calendar_exceptions "
        "(market_code, trading_date, exception_type, reason) "
        "VALUES (?, ?, ?, ?)",
        ("NSE", "2026-09-14", "CLOSED", "Test holiday"),
    )
    store._connection.commit()
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),  # Monday 06:00 UTC
    )
    schedules, exceptions = sched._load_schedules()
    status = market_session_status("NSE", schedules, exceptions, _market_hours_now())
    assert status == "HOLIDAY"
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 0


# ---------------------------------------------------------------------------
# K — scheduler prevents duplicate / overlapping runs
# ---------------------------------------------------------------------------

def test_category_k_overlap_prevention_when_worker_active():
    """If the worker already has an active run, _maybe_submit must not submit
    a second cycle."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    worker.active = dict(cycle_id="in-flight", status="RUNNING",
                         updated_at=_market_hours_now().isoformat())
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
    )
    assert sched.active is True
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 0


def test_category_k_no_duplicate_within_hourly_interval():
    """After a market-hours submission, a second call within the 1-hour interval
    must not submit again."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
        hourly_interval=timedelta(hours=1),
    )
    # First call: due, submits.
    assert sched._maybe_submit() is True
    # Second call: still within the 1-hour window — should not submit.
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 1


# ---------------------------------------------------------------------------
# L — scheduler recovers last run time on restart
# ---------------------------------------------------------------------------

def test_category_l_restart_recovery_prevents_immediate_duplicate():
    """On restart, the scheduler reads job history and sets
    _last_market_hours_run so that an immediate re-submit is suppressed."""
    store = SqliteResearchPersistence()
    # Simulate a previously completed market-hours job 30 minutes ago.
    recent = _market_hours_now() - timedelta(minutes=30)
    store.record_opportunity_job(dict(
        cycle_id="prev-cycle", status="COMPLETED",
        updated_at=recent.isoformat(), parameters={},
    ))
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
    )
    # Recovery sets last_market_hours_run so the interval gate blocks the submit.
    sched._recover_last_runs()
    assert sched._last_market_hours_run is not None
    assert sched._last_market_hours_run >= recent - timedelta(seconds=1)
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 0


def test_category_l_restart_recovery_long_ago_allows_new_run():
    """If the last completed job was more than the hourly interval ago, a new
    market-hours run is permitted after recovery."""
    store = SqliteResearchPersistence()
    old = _market_hours_now() - timedelta(hours=2)
    store.record_opportunity_job(dict(
        cycle_id="prev-cycle", status="COMPLETED",
        updated_at=old.isoformat(), parameters={},
    ))
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
    )
    sched._recover_last_runs()
    assert sched._maybe_submit() is True
    assert len(worker.submissions) == 1


# ---------------------------------------------------------------------------
# M — last successful snapshot served on failure
# ---------------------------------------------------------------------------

def test_category_m_failed_submit_does_not_clear_last_snapshot():
    """When worker.submit raises, _maybe_submit returns False and the
    previously published snapshot remains available via opportunity_current()."""
    store = SqliteResearchPersistence()
    # Publish a successful cycle so a snapshot exists.
    publish(store, [snapshot()])
    persisted = store.opportunity_current()
    assert persisted["best_buy_today"] is not None

    worker = FakeWorker(submit_raises=True)
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
    )
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 1  # submit was attempted

    # The last successful snapshot must still be served.
    current = store.opportunity_current()
    assert current is not None
    assert current["best_buy_today"] is not None
    assert current["generated_at"] == persisted["generated_at"]


# ---------------------------------------------------------------------------
# N — normal API / dashboard GET does not trigger a scan
# ---------------------------------------------------------------------------

def test_category_n_dashboard_read_does_not_trigger_scan():
    """Calling opportunity_current() (the GET /opportunities/current handler body)
    must not invoke the scheduler's worker.submit."""
    store = SqliteResearchPersistence()
    publish(store, [snapshot()])
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
    )
    # Simulate the GET handler reading the current snapshot.
    _ = store.opportunity_current()
    # No submission should have been triggered by the read.
    assert len(worker.submissions) == 0


def test_category_n_schedule_endpoint_reports_opportunity_schedule():
    """The /schedule endpoint exposes the NSE opportunity schedule alongside
    source rules."""
    from app.scheduler import nse_opportunity_schedule
    rules = nse_opportunity_schedule()
    assert len(rules) == 2
    phases = {r.phase for r in rules}
    assert set(phases) == {"MARKET_HOURS", "POST_CLOSE"}


# ---------------------------------------------------------------------------
# O — initial-cycle bootstrap helper (_maybe_submit_initial)
#
# The scheduler NO LONGER auto-invokes _maybe_submit_initial() at startup.
# The fresh-environment initial production cycle is owned exclusively by
# platform.ps1 (POST /api/v1/research/opportunities/cycles after canonical NSE
# universe readiness). The tests below drive _maybe_submit_initial() directly to
# validate its cooldown/overlap semantics; A1-A3 verify _loop()/start() never
# bootstraps at startup and that hourly/market-hours + single-flight protections
# are intact. See _maybe_submit_initial (retained as a programmatic helper).
# ---------------------------------------------------------------------------

def test_category_o_bootstrap_submits_when_no_snapshot_exists():
    """A completely fresh persistence (no job history at all) must have the
    scheduler submit exactly one production cycle on its own, without waiting
    for the next hourly/post-close window."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    # Clock is deliberately OUTSIDE market hours/post-close -- the bootstrap
    # must not depend on _maybe_submit's own market-hours gating.
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(2, 0),  # Monday 02:00 UTC = 7:30 AM IST, pre-open
    )
    submitted = sched._maybe_submit_initial()
    assert submitted is True
    assert len(worker.submissions) == 1
    params = worker.submissions[0]
    assert params["candidate_ids"] is None
    assert params["top_n"] == 4
    assert params["shortlist_limit"] == 25


def test_category_o_bootstrap_skips_when_valid_production_snapshot_exists():
    """If a production (non-controlled) cycle has already completed, the
    bootstrap must not submit a duplicate -- the persisted snapshot already
    covers it, regardless of the scheduler process restarting."""
    store = SqliteResearchPersistence()
    store.record_opportunity_job(dict(
        cycle_id="prev-production-cycle", status="COMPLETED",
        updated_at=_market_hours_now().isoformat(),
        parameters=dict(top_n=4, shortlist_limit=25, candidate_ids=None),
    ))
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(2, 0),
    )
    assert sched._maybe_submit_initial() is False
    assert len(worker.submissions) == 0


def test_category_o_bootstrap_skips_when_worker_already_active():
    """An in-flight (ACCEPTED/RUNNING) cycle -- from a manual POST racing
    startup, or an already-running scheduler task -- must never be
    duplicated by the bootstrap."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    worker.active = dict(cycle_id="in-flight", status="RUNNING",
                         updated_at=_utc(2, 0).isoformat())
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(2, 0),
    )
    assert sched._maybe_submit_initial() is False
    assert len(worker.submissions) == 0


def test_category_o_bootstrap_allows_retry_after_historical_failure_only():
    """A job history containing only FAILED production attempts (e.g. the
    starvation bug from a previous deploy) must not permanently suppress the
    bootstrap -- a retry is still submitted."""
    store = SqliteResearchPersistence()
    store.record_opportunity_job(dict(
        cycle_id="prev-failed-cycle", status="FAILED", error_code="WORKER_STOPPED",
        updated_at=_market_hours_now().isoformat(),
        parameters=dict(top_n=4, shortlist_limit=25, candidate_ids=None),
    ))
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(2, 0),
    )
    assert sched._maybe_submit_initial() is True
    assert len(worker.submissions) == 1


def test_category_o_bootstrap_ignores_controlled_candidate_cycles_as_snapshot():
    """A completed controlled/candidate diagnostic cycle (explicit
    candidate_ids) is not the shared production snapshot -- it must not
    suppress the automatic bootstrap of a real production cycle."""
    store = SqliteResearchPersistence()
    store.record_opportunity_job(dict(
        cycle_id="controlled-diagnostic-cycle", status="COMPLETED",
        updated_at=_market_hours_now().isoformat(),
        parameters=dict(top_n=4, shortlist_limit=25, candidate_ids=[str(uuid4())]),
    ))
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(2, 0),
    )
    assert sched._maybe_submit_initial() is True
    assert len(worker.submissions) == 1
    assert worker.submissions[0]["candidate_ids"] is None


def test_category_o_bootstrap_prevents_immediate_duplicate_then_hourly_schedule_resumes():
    """After the bootstrap submits, the very next poll (still within the
    hourly interval) must not submit again -- and once the hourly interval
    has genuinely elapsed, the normal schedule takes over exactly as it
    would have without the bootstrap."""
    store = SqliteResearchPersistence()
    worker = FakeWorker()
    now = {"t": _market_hours_now()}
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: now["t"],
        hourly_interval=timedelta(hours=1),
    )
    assert sched._maybe_submit_initial() is True
    assert len(worker.submissions) == 1

    # Immediately afterward, a normal scheduled poll must not duplicate it.
    assert sched._maybe_submit() is False
    assert len(worker.submissions) == 1

    # Simulate the bootstrap cycle completing (the real OpportunityCycleWorker
    # clears .active on COMPLETED/FAILED; FakeWorker leaves it set until the
    # caller does, matching how the other overlap tests in this file drive it).
    worker.active = None
    # An hour later, the ordinary hourly schedule is due again, unaffected.
    now["t"] = now["t"] + timedelta(hours=1, minutes=1)
    assert sched._maybe_submit() is True
    assert len(worker.submissions) == 2


@pytest.mark.asyncio
async def test_scheduler_startup_does_not_submit_initial_bootstrap(monkeypatch):
    """A1: scheduler startup (_loop via start()) must NOT submit INITIAL_BOOTSTRAP.

    The fresh-environment initial production cycle is owned exclusively by
    platform.ps1 (POST /api/v1/research/opportunities/cycles after canonical NSE
    universe readiness). The clock is deliberately outside market hours/post-close
    so _maybe_submit also does not fire -- any submission here would be a stray
    scheduler-owned bootstrap.
    """
    import asyncio

    store = SqliteResearchPersistence()
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _utc(2, 0),  # Monday 02:00 UTC = 7:30 AM IST, pre-open
        poll_interval=timedelta(milliseconds=10),
    )
    bootstrap_calls = []
    real_bootstrap = sched._maybe_submit_initial

    def spy():
        bootstrap_calls.append(True)
        return real_bootstrap()

    monkeypatch.setattr(sched, "_maybe_submit_initial", spy)
    sched.start()
    try:
        await asyncio.sleep(0.05)  # give the loop a few poll cycles
        assert len(worker.submissions) == 0, "scheduler submitted INITIAL_BOOTSTRAP at startup"
        assert len(bootstrap_calls) == 0, "_maybe_submit_initial was invoked at startup"
    finally:
        await sched.close()


@pytest.mark.asyncio
async def test_scheduler_startup_submits_only_via_scheduled_window(monkeypatch):
    """A2: when an actual configured scheduled execution is due at startup,
    _loop submits via _maybe_submit (MARKET_HOURS), never via
    _maybe_submit_initial/INITIAL_BOOTSTRAP."""
    import asyncio

    store = SqliteResearchPersistence()
    worker = FakeWorker()
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),  # inside NSE session, due
        poll_interval=timedelta(milliseconds=10),
        hourly_interval=timedelta(hours=1),
    )
    bootstrap_calls = []
    monkeypatch.setattr(sched, "_maybe_submit_initial",
                        lambda: (bootstrap_calls.append(True), False)[1])
    sched.start()
    try:
        await asyncio.sleep(0.05)
        # The scheduled hourly window fired exactly once; bootstrap never did.
        assert len(bootstrap_calls) == 0, "startup invoked _maybe_submit_initial"
        assert len(worker.submissions) == 1, "scheduled due window should submit exactly once"
        assert worker.submissions[0]["candidate_ids"] is None  # production cycle
        assert worker.submissions[0]["top_n"] == 4
    finally:
        await sched.close()


@pytest.mark.asyncio
async def test_scheduler_startup_single_flight_blocks_when_worker_active(monkeypatch):
    """A3: the active/single-flight + single-replica protections hold at startup
    -- an in-flight worker run suppresses any submission on the scheduler."""
    import asyncio

    store = SqliteResearchPersistence()
    worker = FakeWorker()
    worker.active = dict(cycle_id="racing-post", status="RUNNING",
                         updated_at=_market_hours_now().isoformat())
    sched = GlobalOpportunityScheduler(
        worker=worker, persistence=store,
        clock=lambda: _market_hours_now(),
        poll_interval=timedelta(milliseconds=10),
        hourly_interval=timedelta(hours=1),
    )
    # MUST never be called at startup -- if it is, fail loudly.
    monkeypatch.setattr(sched, "_maybe_submit_initial",
                        lambda: pytest.fail("_maybe_submit_initial must not be called"))
    sched.start()
    try:
        await asyncio.sleep(0.05)
        assert len(worker.submissions) == 0, "active/single-flight check failed at startup"
    finally:
        await sched.close()
