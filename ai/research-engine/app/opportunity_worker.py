"""One lifespan-owned asyncio worker per research-engine pod.

Production (non-controlled) cycles are RESUMABLE (app/cycle_checkpoint.py):

* ``submit`` creates a durable run row; the DB active-cycle slot coalesces
  submissions from every replica onto the single active production cycle.
* ``start`` RESUMES an existing active cycle under the SAME cycle_id instead of
  marking it FAILED(WORKER_RESTARTED). Legacy job records without a durable run
  (nothing to resume from) are still marked FAILED(WORKER_RESTARTED).
* A DB lease (owner_id + lease_expires_at, renewed by a heartbeat) guarantees
  one owner across replicas; a worker whose lease expired is fenced out of all
  progress/publication writes. An idle worker takes over an active cycle whose
  lease has expired (e.g. the owning pod was OOM-killed).
* Graceful shutdown releases the lease and leaves the cycle resumable.
"""
import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from app.cycle_checkpoint import (DEFAULT_LEASE_SECONDS, CycleCheckpoint, CycleOwnershipLost, EXTERNAL_RESPONSE_PHASES, new_owner_id)
from app.global_opportunity_cycle import UniverseUnavailableError

logger = logging.getLogger(__name__)


class ManualEtfAdmissionError(RuntimeError):
    """Raised when a manual/paused ETF or Equity Radar submission is rejected.

    The error code string (e.g. 'ETF_MANUAL_EXECUTION_DISABLED',
    'ETF_FULL_UNIVERSE_EXECUTION_DISABLED', 'ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
    is carried in ``str(e)`` so the HTTP layer can map it to the appropriate
    status code without exposing internal gate details to the caller.
    """


class OpportunityCycleWorker:
    def __init__(self, repository, runner, *, owner_id=None, lease_seconds=None, poll_seconds=None, market=None):
        self.repository, self.runner = repository, runner
        # Durable run/lease singleton-slot namespace. Equity keeps the
        # historical default ('NSE'); any other worker (e.g. ETF Radar) must
        # pass a distinct market so its active-cycle slot and lease never
        # collide with Equity's -- this class itself has no Equity-specific
        # behavior beyond this default.
        self.market = market or "NSE"
        self.queue = asyncio.Queue(maxsize=1)
        self.run_lock = asyncio.Lock()
        self.task = None
        self.active = None
        self.last_event_at = datetime.min.replace(tzinfo=timezone.utc)
        self.owner_id = owner_id or new_owner_id()
        settings = getattr(repository, 'settings', None)
        self.lease_seconds = float(lease_seconds or getattr(settings, 'research_opportunity_cycle_lease_seconds', DEFAULT_LEASE_SECONDS))
        self.poll_seconds = float(poll_seconds or min(60.0, max(0.05, self.lease_seconds / 4)))
        self._closing = False
        self._submission_lock = asyncio.Lock()
        self._in_flight_cycle_id = None
        self._manual_etf_active = False  # set True when an ETF manual run is admitted

    # -- persistence helpers ---------------------------------------------------
    @property
    def _store(self):
        return self.repository.persistence

    def _resumable_supported(self):
        return hasattr(self._store, 'create_cycle_run')

    def _is_cycle_cancelled(self, cycle_id):
        """Read-only durable check: has this cycle been cancelled (REQUESTED
        or terminal CANCELLED)?"""
        if not self._resumable_supported():
            return False
        return self._locked(self._store.cycle_cancellation_requested, cycle_id) if hasattr(self._store, 'cycle_cancellation_requested') else False

    def _lease_expired(self, run):
        if not run.get('owner_id') or not run.get('lease_expires_at'):
            return True
        clock = getattr(self._store, '_cycle_clock', lambda: datetime.now(timezone.utc))
        return run['lease_expires_at'] <= self._locked(clock).isoformat(timespec='microseconds')

    def _cancel_status(self, cycle_id):
        """Return 'CANCEL_REQUESTED', 'CANCELLED', or None if not cancelled."""
        if not self._resumable_supported():
            return None
        if not hasattr(self._store, 'cycle_cancel_status'):
            # Backwards-compatible fallback: if no cancel_status method, use
            # the cancellation-requested check as a boolean signal.
            return 'CANCELLED' if self._is_cycle_cancelled(cycle_id) else None
        return self._locked(self._store.cycle_cancel_status, cycle_id)

    @property
    def _recovery_paused(self):
        """Operator-controlled recovery pause (default OFF). When True:
        startup recovery, polling takeover, scheduler submission, and manual
        research submission are all blocked. The cancellation REQUEST/STATUS API
        and the fenced operator cancel-finalization endpoint remain available."""
        settings = getattr(self.repository, 'settings', None)
        return bool(getattr(settings, 'research_opportunity_recovery_paused', False))

    def _locked(self, operation, *args, **kwargs):
        with self.repository._persistence_worker_lock:
            return operation(*args, **kwargs)

    async def _blocking(self, operation, *args, **kwargs):
        run_blocking = getattr(self.repository, '_run_blocking_persistence', None)
        if run_blocking is not None:
            return await run_blocking(operation, *args, **kwargs)
        return self._locked(operation, *args, **kwargs)

    def record(self, value):
        # Windows clocks may return the same timestamp for consecutive transitions.
        # Preserve ordering without relying on random event UUIDs as a tie-break.
        self.last_event_at = max(datetime.now(timezone.utc), self.last_event_at + timedelta(microseconds=1),
                                 datetime.fromisoformat(value['updated_at']) + timedelta(microseconds=1))
        value['updated_at'] = self.last_event_at.isoformat()
        with self.repository._persistence_worker_lock:
            try:
                self.repository.persistence.record_opportunity_job(value, market=self.market)
            except TypeError:
                # Backwards compatibility for minimal test doubles that only
                # implement the original single-argument signature.
                self.repository.persistence.record_opportunity_job(value)

    def _job_for_run(self, run):
        jobs = self._locked(self._store.opportunity_jobs)
        job = next((j for j in jobs if j['cycle_id'] == run['cycle_id']), None)
        if job is None:
            job = dict(cycle_id=run['cycle_id'], status='RUNNING' if run['status'] != 'ACCEPTED' else 'ACCEPTED',
                       updated_at=datetime.now(timezone.utc).isoformat(), parameters=run.get('parameters') or {})
        return {**job, 'parameters': run.get('parameters') or job.get('parameters') or {}}

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        if self._recovery_paused:
            return
        if self.task is not None and not self.task.done():
            return
        self.queue = asyncio.Queue(maxsize=1)
        with self.repository._persistence_worker_lock:
            pending = self.repository.persistence.opportunity_jobs()
        active_run = self._locked(self._store.active_cycle_run, self.market) if self._resumable_supported() else None
        for value in pending:
            if value['status'] in {'ACCEPTED', 'RUNNING'}:
                if self._resumable_supported() and self._locked(self._store.cycle_run, value['cycle_id']) is not None:
                    continue  # durable and resumable: resumed below, never failed
                # A killed process cannot leave an apparently live, non-resumable job indefinitely.
                self.record({**value, 'status': 'FAILED', 'error_code': 'WORKER_RESTARTED',
                             'updated_at': datetime.now(timezone.utc).isoformat()})
        self.task = asyncio.create_task(self._work(), name='global-opportunity-worker')
        if active_run is not None:
            cancel_status = self._cancel_status(active_run['cycle_id'])
            if cancel_status in ('CANCEL_REQUESTED', 'CANCELLED'):
                # Startup/recovery barrier (old-to-new transition race): the
                # new worker must NOT resume a cycle whose cancellation has
                # been requested or is terminal -- UNLESS the original owner is
                # dead (lease expired), in which case this worker must take
                # over to drain in-flight work and coerce to terminal CANCELLED.
                if cancel_status == 'CANCEL_REQUESTED':
                    lease_expired = self._lease_expired(active_run)
                    if lease_expired:
                        logger.info("opportunity_cycle_cancel_drain_takeover cycleId=%s",
                                    active_run['cycle_id'])
                        self._enqueue_resume(active_run)
                    else:
                        logger.info("opportunity_cycle_cancel_owned_elsewhere cycleId=%s owner=%s",
                                    active_run['cycle_id'], active_run.get('owner_id'))
                else:  # CANCELLED (terminal) -- never resume
                    logger.info("opportunity_cycle_cancel_terminal_on_start cycleId=%s",
                                active_run['cycle_id'])
                self.record({**self._job_for_run(active_run), 'status': cancel_status,
                             'error_code': 'CYCLE_CANCELLED',
                             'updated_at': datetime.now(timezone.utc).isoformat()})
                if cancel_status != 'CANCEL_REQUESTED' or not self.active:
                    self.active = None
            elif (active_run.get('owner_id') == self.owner_id
                    or self._lease_expired(active_run)):
                self._enqueue_resume(active_run)
            else:
                logger.info("opportunity_cycle_owned_elsewhere cycleId=%s owner=%s",
                            active_run['cycle_id'], active_run.get('owner_id'))

    async def start_async(self):
        async with self._submission_lock:
            await self._start_async()

    async def _start_async(self):
        if self._recovery_paused:
            return
        if self.task is not None and not self.task.done():
            return
        def recover():
            for value in self._store.opportunity_jobs():
                if value['status'] not in {'ACCEPTED', 'RUNNING'}:
                    continue
                if self._resumable_supported() and self._store.cycle_run(value['cycle_id']) is not None:
                    continue  # includes the other Radar's durable slot
                self.record({**value, 'status': 'FAILED', 'error_code': 'WORKER_RESTARTED'})
        await self._blocking(recover)
        self.queue = asyncio.Queue(maxsize=1)
        self.task = asyncio.create_task(self._work(), name='global-opportunity-worker')
        await self._take_over_async()

    async def submit_async(self, parameters):
        # Keep queue/cache mutation on the event loop; serialize submissions
        # while the single shared connection performs durable admission.
        async with self._submission_lock:
            await self._start_async()
            if self._recovery_paused:
                run = await self._blocking(self._store.active_cycle_run, self.market) if self._resumable_supported() else None
                if run is not None:
                    job = await self._blocking(self._job_for_run, run)
                    return {**job, 'coalesced': True, 'paused': True}
                return {'cycle_id': None, 'status': 'RECOVERY_PAUSED', 'paused': True, 'error_code': 'RECOVERY_PAUSED'}
            if self.active is not None:
                status = await self._blocking(self._cancel_status, self.active['cycle_id'])
                if status != 'CANCELLED':
                    return {**self.active, **({'status': status, 'cancelled': True} if status else {}), 'coalesced': True}
                cancelled_id = self.active['cycle_id']
                self.active = None
                try:
                    queued = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                else:
                    self.queue.task_done()
                    if queued['cycle_id'] != cancelled_id:
                        self.queue.put_nowait(queued)
            parameters = {**parameters, 'correlation_id': str(uuid4())}
            cycle_id = str(uuid4())
            if self._resumable_supported():
                run, created = await self._blocking(self._store.create_cycle_run, cycle_id, parameters, market=self.market)
                if not created:
                    job = await self._blocking(self._job_for_run, run)
                    await self._take_over_async()
                    return {**job, 'coalesced': True, 'cancelled': run['status'] == 'CANCEL_REQUESTED'}
            value = dict(cycle_id=cycle_id, status='ACCEPTED', updated_at=datetime.now(timezone.utc).isoformat(), parameters=parameters)
            await self._blocking(self.record, value)
            # Takeover polling might already have enqueued this durable row.
            if self.active is None and not self.queue.full():
                self.active = value
                self.queue.put_nowait(value)
            return dict(value)

    def _check_manual_etf_admission(self, scope='BOUNDED'):
        """Evaluate the paused/manual ETF admission gate.

        Returns the scope-specific error code if admission is denied. The
        manual submission path is ONLY available when:
          - the worker's market is 'ETF' (equity workers cannot submit ETF),
          - recovery is paused (this is a diagnostics-only path),
          - the scheduler is explicitly disabled (exactly False),
          - the scope-specific execution flag is enabled.
        Any single precondition failure yields the scope-specific error code
        so the HTTP layer can surface a precise message.
        """
        settings = getattr(self.repository, 'settings', None)
        scope_error = 'ETF_FULL_UNIVERSE_EXECUTION_DISABLED' if scope == 'FULL_UNIVERSE' else 'ETF_MANUAL_EXECUTION_DISABLED'
        if self.market != 'ETF':
            return 'ETF_MANUAL_EXECUTION_DISABLED'
        if scope not in ('BOUNDED', 'FULL_UNIVERSE'):
            raise ValueError(f"invalid ETF execution scope: {scope}")
        recovery_paused = bool(getattr(settings, 'research_opportunity_recovery_paused', False))
        scheduler_enabled = getattr(settings, 'research_opportunity_scheduler_enabled', True)
        flag = getattr(settings, 'research_etf_full_universe_execution_enabled' if scope == 'FULL_UNIVERSE'
                       else 'research_etf_manual_execution_enabled', False)
        if not (recovery_paused and scheduler_enabled is False and flag):
            return scope_error
        return None

    async def submit_manual_etf_async(self, parameters, *, scope='BOUNDED', allow_active=False):
        """Admit a single manual ETF Radar cycle under the paused-diagnostics gate.

        Unlike ``submit_async``, this bypasses the recovery-pause early-return so
        that a controlled (LOCAL, ADMIN) cycle can run while the production
        scheduler is paused. It uses the market-specific ('ETF') active-cycle
        slot for coalescing and lease fencing, never touching the Equity ('NSE')
        slot.
        """
        deny = self._check_manual_etf_admission(scope)
        if deny is not None:
            raise ManualEtfAdmissionError(deny)
        async with self._submission_lock:
            # Parallel submissions to the same worker coalesce onto the
            # in-flight accepted job BEFORE consulting the durable active-cycle
            # slot: the worker task may already have transitioned the durable
            # run to RUNNING while the in-memory active is still ACCEPTED.
            if self.active is not None and not allow_active:
                if self.active.get('status') == 'ACCEPTED':
                    return {**self.active, 'coalesced': True}
                raise ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
            if self._resumable_supported():
                run = await self._blocking(self._store.active_cycle_run, self.market)
                if run is not None and not allow_active:
                    # A durable cycle exists in an active (non-terminal) state.
                    # If it is queued (ACCEPTED) and this worker has not yet
                    # taken ownership, coalesce onto it. If it is RUNNING, the
                    # cycle is in-flight and cannot be coalesced -- it requires
                    # explicit review/cancellation first.
                    if run.get('status') == 'ACCEPTED' and run.get('owner_id') == self.owner_id:
                        return {**self._job_for_run(run), 'coalesced': True, 'status': 'ACCEPTED'}
                    raise ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
            parameters = {**(parameters or {}), 'correlation_id': str(uuid4()), 'manual_etf_scope': scope}
            if scope == 'FULL_UNIVERSE' and 'full_universe_limits' in (parameters or {}):
                parameters = {**parameters, 'full_universe_limits': parameters['full_universe_limits']}
            cycle_id = str(uuid4())
            if self._resumable_supported():
                run, created = await self._blocking(self._store.create_cycle_run, cycle_id, parameters, market=self.market)
                if not created:
                    raise ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
            value = dict(cycle_id=cycle_id, status='ACCEPTED',
                         updated_at=datetime.now(timezone.utc).isoformat(), parameters=parameters)
            await self._blocking(self.record, value)
            self.active = value
            self._manual_etf_active = True
            self.queue.put_nowait(value)
            self.task = asyncio.create_task(self._work(), name='manual-etf-opportunity-worker')
            return dict(value)

    async def submit_manual_equity_async(self, parameters):
        """Admit a single manual Equity Radar cycle under the LOCAL gate.

        Only available when environment is LOCAL, recovery is paused, the
        scheduler is disabled, and the equity manual flag is enabled.
        Uses the default ('NSE') market slot for coalescing.
        """
        settings = getattr(self.repository, 'settings', None)
        environment = getattr(settings, 'environment', None)
        recovery_paused = bool(getattr(settings, 'research_opportunity_recovery_paused', False))
        scheduler_enabled = getattr(settings, 'research_opportunity_scheduler_enabled', True)
        equity_enabled = bool(getattr(settings, 'research_equity_manual_execution_enabled', False))
        if not (environment == 'LOCAL' and recovery_paused and scheduler_enabled is False and equity_enabled):
            raise ManualEtfAdmissionError('ETF_MANUAL_EXECUTION_DISABLED')
        async with self._submission_lock:
            # Parallel submissions coalesce onto the in-flight accepted job
            # BEFORE consulting the durable active-cycle slot: the worker task
            # may already have transitioned the durable run to RUNNING while
            # the in-memory active is still ACCEPTED.
            if self.active is not None:
                raise ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
            if self._resumable_supported():
                run = await self._blocking(self._store.active_cycle_run, self.market)
                if run is not None:
                    raise ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
            parameters = {**(parameters or {}), 'correlation_id': str(uuid4())}
            cycle_id = str(uuid4())
            if self._resumable_supported():
                run, created = await self._blocking(self._store.create_cycle_run, cycle_id, parameters, market=self.market)
                if not created:
                    raise ManualEtfAdmissionError('ETF_ACTIVE_CYCLE_REQUIRES_REVIEW')
            value = dict(cycle_id=cycle_id, status='ACCEPTED',
                         updated_at=datetime.now(timezone.utc).isoformat(), parameters=parameters)
            await self._blocking(self.record, value)
            self.active = value
            self.queue.put_nowait(value)
            self.task = asyncio.create_task(self._work(), name='manual-equity-opportunity-worker')
            return dict(value)

    def _enqueue_resume(self, run):
        if self.active is not None or self.queue.full():
            return
        value = {**self._job_for_run(run), 'resumed': True}
        logger.info("opportunity_cycle_resume_enqueued cycleId=%s status=%s resumeCount=%s",
                    run['cycle_id'], run['status'], run.get('resume_count'))
        # This is the pre-claim count. claim_cycle_run increments it atomically
        # when _run_one takes ownership; enqueueing alone is not a recovery.
        self.active = value
        self.queue.put_nowait(value)

    def _maybe_take_over(self):
        """Idle worker: resume an active cycle whose owner's lease expired."""
        if self.active is not None or not self._resumable_supported():
            return
        if self._recovery_paused:
            return
        run = self._locked(self._store.active_cycle_run, self.market)
        if run is None:
            return
        if self._cancel_status(run['cycle_id']) == 'CANCELLED':
            # A cancelled cycle must not be resumed; stop admitting new work.
            return
        if self._lease_expired(run):
            self._enqueue_resume(run)

    def submit(self, parameters):
        self.start()
        if self._recovery_paused:
            # Operator-controlled recovery pause: do not admit new candidates
            # or start a new cycle. Return a coalesced rejection so the
            # caller knows the cycle exists but is not being progressed.
            if self._resumable_supported():
                active_run = self._locked(self._store.active_cycle_run, self.market)
                if active_run is not None:
                    return {**self._job_for_run(active_run), 'coalesced': True,
                            'paused': True, 'cancelled': self._is_cycle_cancelled(active_run['cycle_id'])}
            return {'cycle_id': None, 'status': 'RECOVERY_PAUSED', 'paused': True,
                    'error_code': 'RECOVERY_PAUSED'}
        if self.active is not None:
            cancel_status = self._cancel_status(self.active['cycle_id'])
            if cancel_status == 'CANCELLED':
                # Operator finalization may run in another process. Never let
                # this worker's stale cache retain a released durable slot.
                cancelled_id = self.active['cycle_id']
                self.active = None
                try:
                    queued = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                else:
                    self.queue.task_done()
                    if queued['cycle_id'] != cancelled_id:
                        self.queue.put_nowait(queued)
            else:
                if cancel_status == 'CANCEL_REQUESTED':
                    run = self._locked(self._store.cycle_run, self.active['cycle_id'])
                    return {**self._job_for_run(run), 'coalesced': True, 'cancelled': True}
                return {**self.active, 'coalesced': True}
        correlation_id = str(uuid4())
        parameters = {**parameters, 'correlation_id': correlation_id}
        cycle_id = str(uuid4())
        if self._resumable_supported():
            run, created = self._locked(self._store.create_cycle_run, cycle_id, parameters, market=self.market)
            if not created:
                # The single active production cycle already exists (this pod
                # has not claimed it yet, or another replica owns it).
                if self._cancel_status(run['cycle_id']) == 'CANCELLED':
                    # Stop admitting new candidates: the active cycle is
                    # cancelled and must not be resumed or published.
                    return {**self._job_for_run(run), 'coalesced': True, 'cancelled': True}
                self._maybe_take_over()
                return {**self._job_for_run(run), 'coalesced': True}
        value = dict(cycle_id=cycle_id, status='ACCEPTED',
                     updated_at=datetime.now(timezone.utc).isoformat(), parameters=parameters)
        self.record(value)
        self.active = value
        self.queue.put_nowait(value)
        return dict(value)

    async def _take_over_async(self):
        if self.active is not None or not self._resumable_supported() or self._recovery_paused:
            return
        run = await self._blocking(self._store.active_cycle_run, self.market)
        if run is None:
            return
        if await self._blocking(self._cancel_status, run['cycle_id']) == 'CANCELLED':
            return
        if await self._blocking(self._lease_expired, run):
            job = await self._blocking(self._job_for_run, run)
            # Submission may have won while the durable reads were in flight.
            if self.active is None and not self.queue.full():
                self.active = {**job, 'resumed': True}
                self.queue.put_nowait(self.active)

    async def _work(self):
        # Cleanup may consume cancellation; close must still stop the loop.
        while not self._closing:
            try:
                value = await asyncio.wait_for(self.queue.get(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                try:
                    await self._take_over_async()
                except Exception:
                    logger.exception("opportunity_cycle_takeover_check_failed")
                continue
            try:
                async with self.run_lock:
                    await self._run_one(value)
            finally:
                self.queue.task_done()

    async def _heartbeat(self, cycle_id):
        interval = max(0.02, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._blocking(self._store.renew_cycle_lease, cycle_id, self.owner_id,
                                               lease_seconds=self.lease_seconds)
            except Exception:
                logger.exception("opportunity_cycle_lease_renew_failed cycleId=%s", cycle_id)
                continue
            if not renewed:
                logger.warning("opportunity_cycle_lease_lost cycleId=%s owner=%s", cycle_id, self.owner_id)
                return

    def _superseded_or_done(self, cycle_id):
        """Another worker owns this still-active cycle, or it already terminal."""
        run = self._locked(self._store.cycle_run, cycle_id)
        if run is None:
            return False
        if run.get('status') in ('COMPLETED', 'CANCELLED', 'CANCEL_REQUESTED'):
            return True
        if run.get('lease_expires_at') and self._lease_expired(run):
            return True
        return (run.get('status') in ('ACCEPTED', 'RUNNING', 'PUBLISHED')
                and run.get('owner_id') not in (None, self.owner_id))

    def _fail_run(self, cycle_id, error_code):
        with suppress(CycleOwnershipLost):
            run = self._locked(self._store.cycle_run, cycle_id)
            if run and run['status'] == 'PUBLISHED':
                # Publication won: leave its idempotent projection resumable.
                self._locked(self._store.release_cycle_run, cycle_id, self.owner_id)
                return
            self._locked(self._store.update_cycle_run, cycle_id, self.owner_id, status='FAILED', error_code=error_code)

    async def _run_one(self, value):
        cycle_id = value['cycle_id']
        self._in_flight_cycle_id = cycle_id
        resumable = self._resumable_supported() and await self._blocking(self._store.cycle_run, cycle_id) is not None
        checkpoint = heartbeat = None
        if resumable:
            if not await self._blocking(self._store.claim_cycle_run, cycle_id, self.owner_id, lease_seconds=self.lease_seconds):
                # A live worker (possibly another replica) owns this cycle.
                logger.info("opportunity_cycle_claim_skipped cycleId=%s owner=%s", cycle_id, self.owner_id)
                self.active = None
                return
            checkpoint = CycleCheckpoint(self._store, cycle_id, self.owner_id,
                                         run_blocking=getattr(self.repository, '_run_blocking_persistence', None))
            claimed_run = await self._blocking(self._store.cycle_run, cycle_id)
            logger.info("opportunity_cycle_claimed cycleId=%s owner=%s resumeCount=%s",
                        cycle_id, self.owner_id, claimed_run.get('resume_count'))
            # Recovery has no local acquisition to drain. Terminal cancellation
            # never resumes; recovered intent is finalized before runner entry.
            if await self._blocking(self._cancel_status, cycle_id) == 'CANCELLED':
                logger.info("opportunity_cycle_cancel_terminal cycleId=%s owner=%s", cycle_id, self.owner_id)
                # Already terminal (another worker or a prior pass coerced it).
                self.active = None
                return
            if await self._blocking(self._is_cycle_cancelled, cycle_id):
                # No local runner exists yet: the old process's work cannot be
                # drained here. Finalize under the newly acquired lease only.
                if await self._blocking(self._store.cancel_cycle_run, cycle_id, self.owner_id):
                    await self._blocking(self.record, {**value, 'status': 'CANCELLED', 'error_code': 'CYCLE_CANCELLED'})
                self.active = None
                return
            heartbeat = asyncio.create_task(self._heartbeat(cycle_id), name=f'opportunity-lease-{cycle_id}')
        try:
            value = {**value, 'status': 'RUNNING', 'updated_at': datetime.now(timezone.utc).isoformat()}
            value.pop('error_code', None)
            await self._blocking(self.record, value)
            self.active = value
            extra = {'cycle_id': cycle_id, 'checkpoint': checkpoint} if resumable else {}
            result = await self.runner(**value['parameters'], **extra)
            # Re-check cancellation after in-flight work completed: a cancel
            # request may have arrived mid-flight. Coerce to terminal CANCELLED
            # instead of publishing a successful snapshot.
            if resumable and await self._blocking(self._is_cycle_cancelled, cycle_id):
                logger.info("opportunity_cycle_cancel_after_runner cycleId=%s owner=%s", cycle_id, self.owner_id)
                if not await self._blocking(self._store.cancel_cycle_run, cycle_id, self.owner_id):
                    raise CycleOwnershipLost(cycle_id)
                value = {**value, 'status': 'CANCELLED', 'error_code': 'CYCLE_CANCELLED'}
                await self._blocking(self.record, value)
                if self.active is not None and self.active['cycle_id'] == cycle_id:
                    self.active = None
                return
            value = {**value, 'status': 'COMPLETED', 'result_cycle_id': result['cycle_id'],
                     'universe_count': result.get('universe_count')}
            if resumable:
                await self._blocking(self._store.update_cycle_run, cycle_id, self.owner_id, status='COMPLETED')
                value.update(resumed_candidates={k: v for k, v in checkpoint.restored.items() if k in EXTERNAL_RESPONSE_PHASES},
                             executed_candidates={k: v for k, v in checkpoint.executed.items() if k in EXTERNAL_RESPONSE_PHASES})
            logger.info(
                "opportunity_cycle_complete cycleId=%s correlationId=%s resultCycleId=%s "
                "universeCount=%s status=COMPLETED",
                value.get('cycle_id'), value.get('parameters', {}).get('correlation_id'),
                value.get('result_cycle_id'), value.get('universe_count'),
            )
        except asyncio.CancelledError:
            if resumable and await self._blocking(self._is_cycle_cancelled, cycle_id):
                if await self._blocking(self._store.cancel_cycle_run, cycle_id, self.owner_id):
                    value = {**value, 'status': 'CANCELLED', 'error_code': 'CYCLE_CANCELLED'}
                    await self._blocking(self.record, value)
            elif resumable and self._closing:
                if getattr(self, '_manual_etf_active', False):
                    # An ETF manual run was interrupted by shutdown. The cycle
                    # requires human review before it can be re-run; tag it
                    # with an explicit error code but leave it in its current
                    # (non-terminal) state so the next worker sees an active
                    # cycle and blocks with ETF_ACTIVE_CYCLE_REQUIRES_REVIEW
                    # rather than silently resuming.
                    with suppress(Exception):
                        await self._blocking(self._store.update_cycle_run, cycle_id, self.owner_id,
                                             error_code='ETF_MANUAL_RUN_INTERRUPTED_REVIEW_REQUIRED')
                    value = {**value, 'status': 'RUNNING',
                             'error_code': 'ETF_MANUAL_RUN_INTERRUPTED_REVIEW_REQUIRED'}
                else:
                    # Graceful shutdown without cancellation keeps the SAME cycle
                    # resumable by the next worker.
                    value = {**value, 'status': 'RUNNING', 'error_code': 'WORKER_STOPPED_RESUMABLE'}
                    with suppress(Exception):
                        await self._blocking(self._store.release_cycle_run, cycle_id, self.owner_id)
            else:
                value = {**value, 'status': 'FAILED', 'error_code': 'WORKER_STOPPED'}
                if resumable:
                    await self._blocking(self._fail_run, cycle_id, 'WORKER_STOPPED')
            raise
        except CycleOwnershipLost:
            # Our lease expired and another worker owns the cycle now; it
            # continues the same cycle_id. Record nothing terminal.
            logger.warning("opportunity_cycle_ownership_lost cycleId=%s owner=%s", cycle_id, self.owner_id)
            if await self._blocking(self._is_cycle_cancelled, cycle_id):
                await self._blocking(self._store.cancel_cycle_run, cycle_id, self.owner_id)
            value = {**value, 'status': 'RUNNING', 'error_code': 'OWNERSHIP_LOST'}
        except UniverseUnavailableError as exc:
            # Empty/missing universe -- fail fast without overwriting the
            # persisted radar. Distinct error_code so ops/monitoring can
            # separate a universe/data failure from generic cycle failures.
            logger.exception(
                "opportunity_cycle_failed cycleId=%s correlationId=%s errorType=%s",
                value.get('cycle_id'), value.get('parameters', {}).get('correlation_id'),
                "UniverseUnavailableError",
            )
            error_code = getattr(exc, 'error_code', None) or 'UNIVERSE_UNAVAILABLE'
            value = {**value, 'status': 'FAILED', 'error_code': error_code}
            if resumable:
                await self._blocking(self._fail_run, cycle_id, error_code)
        except Exception as exc:
            # The cycle entry point signals persisted cancellation this way.
            # Require both the exact internal signal and durable intent; an
            # unrelated RuntimeError must still be a genuine worker failure.
            if (type(exc) is RuntimeError and exc.args == ('CYCLE_CANCELLED',)
                    and resumable and await self._blocking(self._is_cycle_cancelled, cycle_id)):
                if await self._blocking(self._store.cancel_cycle_run, cycle_id, self.owner_id):
                    value = {**value, 'status': 'CANCELLED', 'error_code': 'CYCLE_CANCELLED'}
                    await self._blocking(self.record, value)
                return
            # Persist safe failure metadata, never provider/auth exception text.
            logger.exception(
                "opportunity_cycle_failed cycleId=%s correlationId=%s errorType=%s",
                value.get('cycle_id'), value.get('parameters', {}).get('correlation_id'),
                type(exc).__name__,
            )
            value = {**value, 'status': 'FAILED', 'error_code': 'OPPORTUNITY_CYCLE_FAILED'}
            if resumable:
                await self._blocking(self._fail_run, cycle_id, 'OPPORTUNITY_CYCLE_FAILED')
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await heartbeat
            # A zombie owner (lease taken over, or the cycle already completed
            # by another worker) must never publish any status for it.
            zombie = resumable and value.get('status') != 'COMPLETED' and await self._blocking(self._superseded_or_done, cycle_id)
            value['updated_at'] = datetime.now(timezone.utc).isoformat()
            try:
                if zombie:
                    logger.warning("opportunity_cycle_status_suppressed cycleId=%s owner=%s reason=SUPERSEDED",
                                   cycle_id, self.owner_id)
                else:
                    await self._blocking(self.record, value)
            finally:
                # Finalization can release this cycle while its old runner is
                # draining. Do not erase a replacement already queued by submit.
                if self.active is not None and self.active['cycle_id'] == cycle_id:
                    self.active = None

    async def close(self):
        self._closing = True
        try:
            if self.task is not None:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
                self.task = None
            # If a resumable cycle was in-flight and not yet coerced to
            # terminal (e.g. the task was cancelled outside _run_one), coerce
            # CANCEL_REQUESTED -> terminal CANCELLED here so no resumable
            # cancelled cycle is left for the next worker.
            in_flight_cycle_id = getattr(self, '_in_flight_cycle_id', None)
            if in_flight_cycle_id is not None and self._resumable_supported():
                cycle_id = in_flight_cycle_id
                if await self._blocking(self._is_cycle_cancelled, cycle_id) and await self._blocking(self._store.cancel_cycle_run, cycle_id, self.owner_id):
                    run = await self._blocking(self._store.cycle_run, cycle_id)
                    job = await self._blocking(self._job_for_run, run) if run else {}
                    await self._blocking(self.record, {**job, 'cycle_id': cycle_id,
                                 'status': 'CANCELLED', 'error_code': 'CYCLE_CANCELLED',
                                 'updated_at': datetime.now(timezone.utc).isoformat()})
            self.active = None
            self._in_flight_cycle_id = None
            self._manual_etf_active = False
        finally:
            self._closing = False
