"""System-owned scheduler for the shared global NSE opportunity scan.

Design notes
------------
* Replicas: ``self.worker.active`` is only a process-local fast path. Cross-pod
  correctness does NOT depend on it: every submission goes through
  ``OpportunityCycleWorker.submit`` -> ``create_cycle_run``, whose DB
  active-cycle slot (``global_opportunity_cycle_active``) coalesces ticks from
  any pod onto the single active production cycle, and a DB lease fences all
  progress/publication writes to exactly one owner (app/cycle_checkpoint.py).
  RollingUpdate maxSurge=1 overlaps are covered by
  tests/test_slice8_distributed_ownership.py.
* Last-successful snapshot is always served by ``opportunity_current()`` — a
  failed scheduled run never clears the shared projection.
* The scheduler never scans on behalf of a user; it submits a production
  (non-controlled) cycle that writes the system-wide snapshot.
* Restarts: ``OpportunityCycleWorker.start()`` RESUMES the durable active cycle
  under the same cycle_id (only legacy jobs without a durable run are marked
  FAILED(WORKER_RESTARTED)). On scheduler startup we re-derive the last run time
  from job history so no duplicate in-flight run is queued.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from app.market_sessions import (
    MarketTradingSchedule,
    MarketCalendarException,
    market_session_status,
    latest_completed_session,
)

logger = logging.getLogger(__name__)

# Phase 1 interval: hourly during NSE market hours.
_HOURLY_INTERVAL = timedelta(hours=1)
# Post-close window: one additional run within this window after session close.
_POST_CLOSE_WINDOW = timedelta(hours=2)
# How often the scheduler polls the clock.
_POLL_INTERVAL = timedelta(minutes=1)
_KOLKATA = ZoneInfo("Asia/Kolkata")


class GlobalOpportunityScheduler:
    """Drives the system-owned hourly + post-close NSE opportunity cycle.

    The scheduler is deliberately decoupled from the worker: it only decides
    *when* to submit, and delegates the actual cycle execution to
    ``OpportunityCycleWorker`` (which handles coalescing, locking, and
    status tracking). Normal user/API dashboard requests never trigger a scan.
    """

    def __init__(
        self,
        worker,
        persistence,
        *,
        clock: Callable[[], datetime] | None = None,
        poll_interval: timedelta = _POLL_INTERVAL,
        hourly_interval: timedelta = _HOURLY_INTERVAL,
        post_close_window: timedelta = _POST_CLOSE_WINDOW,
        market_code: str = "NSE",
    ):
        self.worker = worker
        self.persistence = persistence
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll_interval = poll_interval
        self.hourly_interval = hourly_interval
        self.post_close_window = post_close_window
        self.market_code = market_code
        self.task: asyncio.Task | None = None
        self._stopped = False
        # Timestamps of last submission per phase — prevents duplicate hourly
        # and post-close runs. Recovered from job history on restart.
        self._last_market_hours_run: datetime | None = None
        self._last_post_close_run: datetime | None = None

    @property
    def active(self) -> bool:
        """True when a cycle run is currently in-flight."""
        return getattr(self.worker, 'active', None) is not None

    @property
    def _recovery_paused(self) -> bool:
        """Mirrors the worker's recovery-pause check for the scheduler loop."""
        settings = getattr(getattr(self.worker, 'repository', None), 'settings', None)
        return bool(getattr(settings, 'research_opportunity_recovery_paused', False))

    def _load_schedules(self) -> tuple[list[MarketTradingSchedule], list[MarketCalendarException]]:
        """Load NSE schedules and exceptions from persistence (DB-backed calendar)."""
        schedules = self.persistence.load_market_schedules({self.market_code})
        exceptions = self.persistence.load_market_calendar_exceptions({self.market_code})
        # Fallback if persistence doesn't provide the calendar.
        if not schedules:
            schedules = [
                MarketTradingSchedule(self.market_code, "XNSE", "IN", "Asia/Kolkata", day,
                                      time(9, 15), time(15, 30), enabled=True)
                for day in range(5)
            ]
        return schedules, exceptions

    def _recover_last_runs(self) -> None:
        """On restart, recover last run times from job history to avoid duplicates."""
        try:
            jobs = self.persistence.opportunity_jobs()
        except AttributeError:
            return
        if not jobs:
            return
        for job in jobs:
            updated = job.get('updated_at')
            if not updated:
                continue
            try:
                when = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue
            status = job.get('status', '')
            # Track the most recent COMPLETED job as the last successful run
            # to prevent an immediate duplicate on restart.
            if status == 'COMPLETED':
                if self._last_market_hours_run is None or when > self._last_market_hours_run:
                    self._last_market_hours_run = when
            # Any ACCEPTED/RUNNING job that survived restart means a run is
            # in-flight (shouldn't normally happen since the worker marks
            # them FAILED on start, but be defensive).
            if status in ('ACCEPTED', 'RUNNING') and when > (self._last_market_hours_run or datetime.min.replace(tzinfo=timezone.utc)):
                self._last_market_hours_run = when

    def _market_hours_due(self, now: datetime) -> bool:
        """Check if an hourly scan is due during market hours."""
        if self._last_market_hours_run is not None:
            if now - self._last_market_hours_run < self.hourly_interval:
                return False
        return True

    def _post_close_due(self, now: datetime, schedules: list, exceptions: list) -> bool:
        """Check if a post-close run is due for the most recent completed session."""
        # Only one post-close run per completed session.
        if self._last_post_close_run is not None:
            completed = latest_completed_session(self.market_code, schedules, exceptions, now)
            if completed is not None and self._last_post_close_run >= completed:
                return False
        # The most recent completed session must be within the post-close window.
        completed = latest_completed_session(self.market_code, schedules, exceptions, now)
        if completed is None:
            return False
        if now - completed > self.post_close_window:
            return False
        return True

    def _has_valid_production_snapshot(self) -> bool:
        """True once a production (non-controlled) cycle has ever completed.

        Controlled/candidate diagnostic cycles (submitted with explicit
        candidate_ids) never count as the shared production snapshot, no
        matter how many have run. A history containing only FAILED
        production attempts also does not count -- retries stay allowed,
        startup is never permanently suppressed by a past failure.
        """
        try:
            jobs = self.persistence.opportunity_jobs()
        except AttributeError:
            return False
        for job in jobs:
            if job.get('status') != 'COMPLETED':
                continue
            parameters = job.get('parameters') or {}
            if parameters.get('candidate_ids'):
                continue
            return True
        return False

    def _maybe_submit_initial(self) -> bool:
        """[DEPRECATED: not invoked by _loop()] Bootstrap exactly one production
        cycle when there is no valid production snapshot yet.

        Fresh-environment initial execution is now owned exclusively by
        platform.ps1 (POST /opportunities/cycles after canonical NSE universe
        readiness); this method is retained only as an explicit/programmatic
        helper and is no longer auto-invoked at scheduler startup.
        """
        # NOTE: no scheduler-managed INITIAL_BOOTSTRAP path remains. _loop() submits
        # only via _maybe_submit() (hourly/post-close, single-replica + active-cycle
        # + single-flight protected). Keep this guard explicit for any future
        # explicit/programmatic caller.
        if self.active:
            return False
        if self._recovery_paused:
            return False
        if self._has_valid_production_snapshot():
            return False
        # Production submission: candidate_ids=None means the complete
        # applicable deep-research universe, never a bounded shortlist.
        # run_global_opportunity_cycle already forces this unbounded whenever
        # candidate_ids is None regardless of what's passed here; None is
        # passed explicitly too so this call site cannot silently reintroduce
        # a finite cap on its own.
        parameters = dict(top_n=4, shortlist_limit=None, candidate_ids=None)
        try:
            result = self.worker.submit(parameters)
            now = self.clock()
            # Start both cooldowns from this bootstrap submission so the
            # normal hourly/post-close cadence takes over cleanly instead of
            # firing again on the very next poll.
            self._last_market_hours_run = now
            self._last_post_close_run = now
            if result is not None:
                logger.info(
                    "scheduler operation=OPPORTUNITY_CYCLE_SUBMIT phase=INITIAL_BOOTSTRAP cycle_id=%s",
                    result.get('cycle_id'))
            return True
        except Exception:
            # Last successful snapshot (if any) continues to be served by
            # opportunity_current(); startup itself must never fail on this.
            logger.exception("scheduler operation=OPPORTUNITY_CYCLE_SUBMIT_FAILURE phase=INITIAL_BOOTSTRAP")
            return False

    def _maybe_submit(self) -> bool:
        """Decide whether to submit a cycle and do so if due. Returns True if submitted."""
        if self._recovery_paused:
            logger.info("scheduler operation=OPPORTUNITY_CYCLE_SKIP phase=RECOVERY_PAUSED reason=pause_active")
            return False
        if self.active:
            return False
        # Stop admitting new candidates: if the active cycle has been cancelled
        # (durable status='CANCELLED'), do not submit a new cycle until the
        # operator clears it.
        active_run = None
        try:
            active_run = self.persistence.active_cycle_run(self.market_code) \
                if hasattr(self.persistence, 'active_cycle_run') else None
        except AttributeError:
            active_run = None
        if active_run is not None:
            run_status = active_run.get('status')
            if run_status in ('CANCEL_REQUESTED', 'CANCELLED'):
                logger.info("scheduler operation=OPPORTUNITY_CYCLE_SKIP phase=%s reason=%s cycleId=%s",
                            "CANCEL_CHECK", run_status, active_run.get('cycle_id'))
                return False
        now = self.clock()
        schedules, exceptions = self._load_schedules()
        status = market_session_status(self.market_code, schedules, exceptions, now)
        # Fail closed: UNKNOWN means we can't determine session state.
        if status not in ("OPEN", "CLOSED"):
            return False
        phase = None
        if status == "OPEN":
            if self._market_hours_due(now):
                phase = "MARKET_HOURS"
        elif status == "CLOSED":
            if self._post_close_due(now, schedules, exceptions):
                phase = "POST_CLOSE"
        if phase is None:
            return False
        # Submit a production (non-controlled) cycle.
        # Production submission: candidate_ids=None means the complete
        # applicable deep-research universe, never a bounded shortlist.
        # run_global_opportunity_cycle already forces this unbounded whenever
        # candidate_ids is None regardless of what's passed here; None is
        # passed explicitly too so this call site cannot silently reintroduce
        # a finite cap on its own.
        parameters = dict(top_n=4, shortlist_limit=None, candidate_ids=None)
        try:
            result = self.worker.submit(parameters)
            if phase == "MARKET_HOURS":
                self._last_market_hours_run = now
            else:
                self._last_post_close_run = now
            if result is not None:
                logger.info(
                    "scheduler operation=OPPORTUNITY_CYCLE_SUBMIT phase=%s cycle_id=%s",
                    phase, result.get('cycle_id'))
            return True
        except Exception:
            # Last successful snapshot continues to be served by opportunity_current().
            logger.exception("scheduler operation=OPPORTUNITY_CYCLE_SUBMIT_FAILURE phase=%s", phase)
            return False

    async def _loop(self):
        """Main scheduler loop: poll at fixed interval and submit when due.

        This loop no longer performs an INITIAL_BOOTSTRAP submission at startup.
        The fresh-environment initial production cycle is owned exclusively by
        platform.ps1, which POSTs to /api/v1/research/opportunities/cycles after
        the canonical NSE universe becomes ready. The scheduler owns only the
        recurring hourly/market-hours + post-close cadence below (via
        _maybe_submit), gated by the same single-replica/active-cycle and
        single-flight protections. See _maybe_submit_initial (retained as an
        explicit/programmatic helper only).
        """
        self._recover_last_runs()
        while not self._stopped:
            try:
                self._maybe_submit()
            except Exception:
                logger.exception("scheduler operation=POLL_ERROR")
            await asyncio.sleep(self.poll_interval.total_seconds())

    def start(self):
        """Begin the background scheduling loop. Idempotent."""
        if self.task is not None and not self.task.done():
            return
        self._stopped = False
        self.task = asyncio.create_task(self._loop(), name='global-opportunity-scheduler')

    async def close(self):
        """Stop the scheduling loop and clean up."""
        self._stopped = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
