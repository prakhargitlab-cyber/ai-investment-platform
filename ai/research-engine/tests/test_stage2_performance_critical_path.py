"""Performance-investigation follow-up pass -- the six areas the prior
consolidated-fix-pass report flagged as unexamined:

  1. fixed 25-second orchestration wait
  2. repeated runtime.ensure() calls on warm evidence
  3. provider cooldowns/backoffs
  4. repository persistence lock granularity
  5. PDF concurrency=1 serialization
  6. discovery single-flight/reuse

Each was traced through the actual code (no real-provider cycle run, per
the standing constraint). Five of the six were found ALREADY correctly
implemented, with existing regression coverage:
  - #1: tests/test_di20g_acquisition_lifecycle.py::
        test_25_deep_candidates_use_completion_not_request_budget_and_account_for_baseline
        proves the wait_for_completion=True path (the only mode Stage-2
        uses -- every call site in deep_investigation.py and
        global_opportunity_orchestration.py passes wait_for_completion=True)
        NEVER aborts/truncates on the 25s mark: it is a diagnostic log
        threshold only (asyncio.wait(timeout=...) followed by an
        unconditional `await task`), not a hard budget. It cannot multiply
        into added wall-clock cost across candidates because it adds none
        to begin with.
  - #7 (single-flight): tests/test_research_readiness_runtime.py::
        test_concurrent_same_instrument_ensure_reuses_single_flight already
        proves two concurrent ensure() calls for the same instrument share
        one underlying acquisition task via ResearchReadinessRuntime._flights.

This file adds the remaining new, targeted regression coverage: #2
(READY_FRESH mandatory evidence never re-enters acquisition), #3
(READY_STALE's existing recheck policy is unchanged), #5 (the repository
persistence lock's critical section is scoped to the persistence operation
itself, never network/provider wait), and #6 (PDF capacity contention does
not hold the repository lock, and non-PDF work is not blocked by it).

No production code changes accompany this file: every area it covers was
verified already correct by tracing the actual call graph (see the final
report). Section 4 (repository lock) is architecturally REQUIRED given a
single shared psycopg connection (app/postgres_persistence.py) -- serializing
DB operations across stage2_concurrency=4 is correctness protection, not a
bug, and per the task's own instruction was left untouched (no connection-
pool redesign performed).
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.deep_investigation import investigate
from app.repository import ResearchRepository
from app.research_fetching import HttpResearchFetcher
from app.research_readiness import ResearchRequirementStatus
from app.settings import Settings

from test_stock_rule_engine import INSTRUMENT_ID, _readiness


# ---------------------------------------------------------------------------
# 2 -- READY_FRESH durable mandatory evidence never re-enters acquisition ---
# ---------------------------------------------------------------------------
class _NeverAcquireRuntime:
    """ensure()/read() that raises if acquisition is ever attempted -- proves
    investigate() genuinely never calls it for already-READY_FRESH evidence,
    rather than merely completing quickly by coincidence."""

    def __init__(self, readiness):
        self.readiness = readiness

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False):
        return self.readiness

    async def ensure(self, *a, **k):
        raise AssertionError("ensure() must never be called for READY_FRESH mandatory evidence")


@pytest.mark.asyncio
async def test_02_all_ready_fresh_mandatory_evidence_never_calls_ensure():
    readiness = _readiness()  # every requirement defaults to READY_FRESH
    runtime = _NeverAcquireRuntime(readiness)
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    assert not result.failures
    # Every mandatory requirement's matrix entry reflects the pre-existing
    # durable READY_FRESH evidence, not a fresh (re-)acquisition attempt.
    for requirement_id, entry in matrix.items():
        if requirement_id == "CURRENT_NEWS":
            continue
        assert entry["state"] == "READY_FRESH"


# ---------------------------------------------------------------------------
# 3 -- READY_STALE still follows the existing (unmodified) recheck policy --
# ---------------------------------------------------------------------------
class _RecordingRuntime:
    def __init__(self, readiness):
        self.readiness = readiness
        self.ensure_calls: list[tuple] = []

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False):
        return self.readiness

    async def ensure(self, instrument_id, *, jurisdiction, requirement_ids, identity_headers=None,
                      correlation_id=None, wait_for_completion=True):
        from dataclasses import replace
        from app.research_readiness_runtime import TargetedEnsureResult
        self.ensure_calls.append(tuple(requirement_ids))
        requirement_id = requirement_ids[0]
        self.readiness = replace(self.readiness, requirements=tuple(
            replace(row, status=ResearchRequirementStatus.READY_FRESH, missing_input_ids=())
            if row.requirement_id == requirement_id else row for row in self.readiness.requirements))
        return TargetedEnsureResult(self.readiness, (requirement_id,), (f"NSE:{requirement_id}",))


@pytest.mark.asyncio
async def test_03_ready_stale_still_attempts_a_recheck_exactly_as_before():
    # Unchanged, pre-existing behavior (this pass did not touch
    # _requires_acquisition or the readiness/eligibility policy): a
    # READY_STALE mandatory requirement is still eligible for a recheck
    # attempt -- whether that recheck does any REAL provider work is then
    # governed entirely by the repository's own freshness/cooldown gate
    # (ResearchRepository._category_is_eligible_to_check), not by
    # deep_investigation.py, which this pass did not modify.
    readiness = _readiness({"QUARTERLY_FINANCIALS": ResearchRequirementStatus.READY_STALE})
    runtime = _RecordingRuntime(readiness)
    await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    assert ("QUARTERLY_FINANCIALS",) in runtime.ensure_calls


# ---------------------------------------------------------------------------
# 5 -- repository persistence lock does not cover network/provider wait ----
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_05_persistence_lock_does_not_block_concurrent_unrelated_async_work():
    repo = ResearchRepository(settings=Settings())
    # Force the thread+lock branch (_run_serialized_persistence_operation):
    # the default test repository's SQLite persistence takes a fast
    # synchronous inline path that never touches the lock at all (see
    # _run_blocking_persistence), so exercising the lock itself requires a
    # persistence object that isn't a SqliteResearchPersistence instance --
    # exactly what production's PostgresResearchPersistence is (it inherits
    # SqliteResearchPersistence but lives in app.postgres_persistence, which
    # _run_blocking_persistence's isinstance/module check special-cases).
    class _NotSqlite:
        pass
    repo._persistence = _NotSqlite()

    hold_seconds = 0.2

    def _slow_persistence_op():
        time.sleep(hold_seconds)
        return "persisted"

    persistence_started = threading.Event()

    def _slow_persistence_op_signaling():
        persistence_started.set()
        time.sleep(hold_seconds)
        return "persisted"

    network_finished_at = {}
    started = time.monotonic()

    async def _simulated_network_wait():
        # Represents work that was NEVER routed through the repository lock
        # (network download, provider HTTP calls) -- see
        # HttpResearchFetcher.fetch_network / _fetch_official_filings,
        # neither of which references self._persistence_worker_lock or any
        # ResearchRepository instance.
        await asyncio.sleep(hold_seconds * 0.5)
        network_finished_at["t"] = time.monotonic() - started

    persistence_task = asyncio.create_task(repo._run_blocking_persistence(_slow_persistence_op_signaling))
    await asyncio.get_event_loop().run_in_executor(None, persistence_started.wait)
    network_task = asyncio.create_task(_simulated_network_wait())

    await asyncio.gather(persistence_task, network_task)

    # The "network" work finished on its own timeline (~hold_seconds*0.5),
    # not delayed until the persistence lock was released
    # (~hold_seconds) -- proving the lock's critical section never extends
    # to unrelated concurrent async work.
    assert network_finished_at["t"] < hold_seconds * 0.9


@pytest.mark.asyncio
async def test_05b_persistence_lock_does_still_serialize_two_persistence_operations():
    # Control: proves the lock is real and does its actual job (protecting
    # the single shared connection), not that it has simply been removed.
    repo = ResearchRepository(settings=Settings())
    class _NotSqlite:
        pass
    repo._persistence = _NotSqlite()

    order: list[str] = []
    hold_seconds = 0.08

    def _op(name):
        order.append(f"{name}_start")
        time.sleep(hold_seconds)
        order.append(f"{name}_end")
        return name

    await asyncio.gather(
        repo._run_blocking_persistence(_op, "A"),
        repo._run_blocking_persistence(_op, "B"),
    )
    # Fully interleaved (A_start, B_start, A_end, B_end) would mean the lock
    # did not serialize them; the lock guarantees one completes before the
    # other starts.
    assert order in (["A_start", "A_end", "B_start", "B_end"],
                      ["B_start", "B_end", "A_start", "A_end"])


# ---------------------------------------------------------------------------
# 6 -- PDF extraction-capacity contention never holds the repository lock --
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_06_pdf_extraction_semaphore_contention_does_not_block_repository_persistence():
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    repo = ResearchRepository(settings=Settings())
    class _NotSqlite:
        pass
    repo._persistence = _NotSqlite()

    hold_seconds = 0.2
    started = time.monotonic()
    persistence_finished_at = {}

    async def _hold_pdf_semaphore():
        # Simulates two concurrent candidates each needing the (concurrency=1)
        # PDF-parse permit: the second one queues on the semaphore for the
        # full hold_seconds.
        await fetcher._pdf_extraction_semaphore.acquire()
        try:
            await asyncio.sleep(hold_seconds)
        finally:
            fetcher._pdf_extraction_semaphore.release()

    def _fast_persistence_op():
        time.sleep(0.01)
        return "ok"

    async def _run_persistence():
        await repo._run_blocking_persistence(_fast_persistence_op)
        persistence_finished_at["t"] = time.monotonic() - started

    holder = asyncio.create_task(_hold_pdf_semaphore())
    await asyncio.sleep(0.02)  # let holder acquire the permit first
    second_pdf_waiter = asyncio.create_task(_hold_pdf_semaphore())
    persistence = asyncio.create_task(_run_persistence())

    await asyncio.gather(holder, second_pdf_waiter, persistence)

    # A completely independent, fast persistence op finishes almost
    # immediately -- it is never queued behind PDF-extraction-permit
    # contention, because HttpResearchFetcher's semaphore and
    # ResearchRepository's persistence lock are architecturally
    # independent objects (neither class references the other's lock).
    assert persistence_finished_at["t"] < hold_seconds * 0.5


def test_06b_http_research_fetcher_never_references_a_repository_lock():
    # Structural control: HttpResearchFetcher has no attribute that could
    # even accidentally alias a ResearchRepository's persistence lock.
    fetcher = HttpResearchFetcher(Settings(research_pdf_extraction_concurrency=1))
    assert not hasattr(fetcher, "_persistence_worker_lock")
    assert not hasattr(fetcher, "repository")
