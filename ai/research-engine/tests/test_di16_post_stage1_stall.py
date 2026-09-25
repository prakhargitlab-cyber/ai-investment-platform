"""DI-16: regression tests for the post-Stage-1 observability/correlation fix.

Scope (read-only diagnosis → narrow observability-only implementation):
- Stage 1 completion must proceed to the next orchestration phase (Stage 2) and
  be observable (stage2_start log emitted immediately after baseline_complete).
- No silent post-Stage-1 return: the success path emits stage2_complete; the
  worker emits opportunity_cycle_complete. (A cycle-level timeout is NOT added
  here -- the task forbids changing timeouts; a hang would now be diagnosable
  because stage2_start would appear without a matching stage2_complete.)
- Terminal cycle state persisted on post-Stage-1 failure (worker FAILED path).
- correlation_id threads worker -> run_global_opportunity_cycle -> orchestrator.run,
  and is persisted in the job-record payload (global_opportunity_top_selection
  payload JSON) -- no schema change.
- NO_CANDIDATES is accurate for a genuinely empty Yahoo result and is NOT a
  collapsed currency/quote-type/exchange rejection; rejected candidates are
  logged separately and never collapsed into NO_CANDIDATES.

No live network, no DB mutations, no threshold/scoring/timeout/matching changes.
"""
import asyncio
import json
import logging
from threading import RLock
from types import SimpleNamespace

import pytest

from app.global_opportunity_cycle import run_global_opportunity_cycle
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.opportunity_worker import OpportunityCycleWorker
from app.persistence import SqliteResearchPersistence
from app.structured_market import StructuredProviderError

from test_global_opportunity_acquisition import acquisition_service
from test_global_scanner import instrument as scanner_instrument, persisted, NOW
from test_stock_rule_engine import _readiness
from test_structured_market import provider, response, instrument as sm_instrument


@pytest.fixture
def store():
    return SqliteResearchPersistence()


# ---------------------------------------------------------------------------
# Task A: post-Stage-1 control flow must be observable (stage2_start log).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stage2_start_and_complete_logged_with_correlation(monkeypatch, caplog):
    """Stage 1 completion proceeds to Stage 2 AND is observable: the stage2_start
    log fires immediately after baseline_acquisition_complete (the exact gap that
    was silent in production), and stage2_complete marks the terminal success
    boundary. Both carry the correlation id end-to-end."""
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)

    async def ensure(key, **kwargs):
        persisted(store, scanner_instrument(key.int), age=-1 / 1440)
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(None, (), ())

    runtime.ensure.side_effect = ensure

    caplog.set_level(logging.INFO, logger="app.global_opportunity_orchestration")
    result = await service.run([scanner_instrument()], as_of=NOW, shortlist_limit=1,
                               identity_headers={'X-AIP-User-Id': 'server'},
                               correlation_id='corr-di16-001')

    messages = [r.message for r in caplog.records]
    # The FIRST new emission after baseline_acquisition_complete is stage2_start,
    # placed immediately before the Phase-2 re-scan -- closing the silent gap.
    assert any('baseline_acquisition_complete' in m for m in messages)
    assert any('stage2_start' in m and 'correlationId=corr-di16-001' in m for m in messages)
    complete_idx = next(i for i, m in enumerate(messages) if 'baseline_acquisition_complete' in m)
    start_idx = next(i for i, m in enumerate(messages) if 'stage2_start' in m)
    assert start_idx > complete_idx, 'stage2_start must follow baseline_acquisition_complete'
    # Explicit terminal-on-success boundary (no silent successful return).
    assert any('stage2_complete' in m and 'correlationId=corr-di16-001' in m for m in messages)
    # The single deep-ready candidate was actually analyzed (rules reached).
    assert result.diagnostics[0].disposition == 'ANALYZED'
    assert result.diagnostics[0].status == 'RANK_ELIGIBLE'


# ---------------------------------------------------------------------------
# Tasks A/B/D: terminal cycle state persisted on post-Stage-1 failure, plus
# correlation id carried through the worker, completion log, and job payload.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_emits_completion_log_with_correlation(store, caplog):
    """A successful cycle is no longer silent: the worker emits a completion
    log (with correlationId) that the production stall left entirely absent."""
    caplog.set_level(logging.INFO, logger="app.opportunity_worker")

    async def ok(**kwargs):
        assert kwargs.get('correlation_id') is not None
        return {'cycle_id': 'xyz-di16', 'universe_count': 42}

    worker = OpportunityCycleWorker(
        SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), ok)
    try:
        job = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 2)
        assert job['parameters']['correlation_id']
        messages = [r.message for r in caplog.records]
        assert any('opportunity_cycle_complete' in m
                   and 'cycleId=' in m and 'correlationId=' in m
                   and 'resultCycleId=xyz-di16' in m for m in messages), messages
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_correlation_id_persisted_in_job_payload(store):
    """correlation_id is persisted in the job-record payload (no schema change),
    surviving the ACCEPTED -> RUNNING -> COMPLETED transitions."""
    async def ok(**kwargs):
        return {'cycle_id': 'xyz-di16', 'universe_count': 42}
    worker = OpportunityCycleWorker(
        SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), ok)
    try:
        job = worker.submit({'top_n': 4})
        correlation = job['parameters']['correlation_id']
        assert correlation
        await asyncio.wait_for(worker.queue.join(), 2)
        persisted_job = next(v for v in store.opportunity_jobs() if v['cycle_id'] == job['cycle_id'])
        assert persisted_job['parameters']['correlation_id'] == correlation
        assert persisted_job['status'] == 'COMPLETED'
        assert 'correlation_id' in json.dumps(persisted_job['parameters'])
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_failure_persists_failed_state_with_correlation(store):
    """Step 3: terminal cycle state is persisted on post-Stage-1 failure
    (the worker records FAILED + error_code). The correlation id is retained
    on the failure record so a stalled job is attributable."""
    async def boom(**kwargs):
        raise RuntimeError('db lock / pool exhaustion (sanitized)')
    worker = OpportunityCycleWorker(
        SimpleNamespace(persistence=store, _persistence_worker_lock=RLock()), boom)
    try:
        job = worker.submit({'top_n': 4})
        await asyncio.wait_for(worker.queue.join(), 2)
        failed = next(v for v in store.opportunity_jobs() if v['cycle_id'] == job['cycle_id'])
        assert failed['status'] == 'FAILED'
        assert failed['error_code'] == 'OPPORTUNITY_CYCLE_FAILED'
        # Raw exception text is never persisted into the public job payload.
        assert 'db lock' not in json.dumps(failed)
        assert failed['parameters']['correlation_id'] == job['parameters']['correlation_id']
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_run_global_opportunity_cycle_forwards_correlation_id(monkeypatch):
    """correlation_id threads worker -> run_global_opportunity_cycle ->
    orchestrator.run and lands on the persisted radar selection payload."""
    captured = {}

    class StubSource:
        async def active_global_equities(self, identity_headers=None):
            return []
        async def sector_benchmark_contexts(self, ids, identity_headers=None):
            return {}
        def register_global_profile_metadata(self, *a, **k):
            return False

    class StubPersistence:
        def recommendation_states(self):
            return []
        def recommendation_history(self):
            return []
        def opportunity_snapshots(self, cycle_id):
            return []
        def publish_opportunity_cycle(self, snapshots, recommendations, states, selection, *, build=None):
            return build(self.opportunity_snapshots(selection['cycle_id']))[2]
        def persist_global_suggestion_lifecycle(self, scan, cards):
            pass

    repo = SimpleNamespace(persistence=StubPersistence(), _persistence_worker_lock=RLock())

    async def fake_run(self, canonical_instruments, *, as_of, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(universe_count=2578, deep_evaluated_count=0,
                               shortlist_count=0, rank_eligible_count=0, top_n=[],
                               diagnostics=[], evaluated_entries=(), review_evidence={},
                               phase1_eligible_count=0, stage_b_count=0)

    monkeypatch.setattr(GlobalOpportunityOrchestrator, 'run', fake_run)
    selection = await run_global_opportunity_cycle(
        repo, StubSource(), top_n=4, shortlist_limit=25, candidate_ids=None,
        readiness_runtime=None, correlation_id='corr-forwarded')
    assert captured.get('correlation_id') == 'corr-forwarded'
    assert selection['correlation_id'] == 'corr-forwarded'
    assert selection['status'] == 'COMPLETED'


# ---------------------------------------------------------------------------
# Task C: NO_CANDIDATES is accurate for a genuinely empty Yahoo result and is
# NOT a collapsed currency/quote-type/exchange rejection.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_candidates_reason_logs_queries_and_is_not_a_collapsed_rejection(caplog):
    """A candidate accepted from a cached/persisted snapshot (score 1.0,
    IDENTITY_SCORE_ACCEPTABLE at pre-scan) is resolved through a *fresh* Yahoo
    search during deep acquisition. If that live search returns zero quotes,
    COMPANY_NOT_RESOLVED:NO_CANDIDATES is raised with the probed queries and
    provider errors -- semantically accurate, not a collapsed rejection."""
    caplog.set_level(logging.INFO, logger="app.structured_market")

    def handler(request):
        return response({"quotes": []})

    with pytest.raises(StructuredProviderError, match="COMPANY_NOT_RESOLVED:NO_CANDIDATES"):
        await provider(handler).resolve_instrument(sm_instrument())

    messages = [r.message for r in caplog.records]
    assert any("yahoo_discovery_identity" in m for m in messages)
    assert any("reason=NO_CANDIDATES queries=" in m and "provider_errors=" in m
               for m in messages)
    # No candidate was found, so none was logged/rejected here.
    assert not any("yahoo_candidate" in m for m in messages)


@pytest.mark.asyncio
async def test_rejected_candidate_is_logged_and_never_collapsed_into_no_candidates(caplog):
    """A Yahoo candidate returned but rejected for a currency mismatch is logged
    with its REJECT_* validation reason and produces a distinct error code
    (LOW_CONFIDENCE), never NO_CANDIDATES. This proves NO_CANDIDATES is not
    collapsing a later validation-stage rejection."""
    caplog.set_level(logging.INFO, logger="app.structured_market")

    def handler(request):
        return response({"quotes": [
            {"symbol": "ACME.AS", "quoteType": "EQUITY", "longname": "Acme Industries",
             "exchange": "AMS", "currency": "USD"},  # USD != held INR -> score 0
        ]})

    with pytest.raises(StructuredProviderError, match="COMPANY_NOT_RESOLVED:LOW_CONFIDENCE"):
        await provider(handler).resolve_instrument(sm_instrument(tradingCurrency="INR"))

    messages = [r.message for r in caplog.records]
    assert any("yahoo_candidate" in m and "REJECT_CURRENCY" in m for m in messages)
    # The genuinely-empty case is the ONLY trigger for NO_CANDIDATES: a returned
    # candidate that is rejected for currency must never collapse into it.
    assert not any("reason=NO_CANDIDATES" in m for m in messages)
    # Distinct, single LOW_CONFIDENCE classification (not collapsed to NO_CANDIDATES).
    assert len([m for m in messages if "yahoo_resolution_rejected reason=LOW_CONFIDENCE" in m]) == 1


# ---------------------------------------------------------------------------
# POST_STAGE1_BLOCKING_FINDING: the synchronous rule evaluation must NOT run on
# the event loop (it starves the synchronous /health route -> liveness restarts,
# see restartCount=3). It is offloaded via asyncio.to_thread -- the same offload
# primitive repository._run_blocking_persistence already uses.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rule_engine_analyze_evaluates_off_event_loop():
    """engine.evaluate (true rule_engine.py:506) is synchronous CPU-bound scoring.
    Before the fix it ran inline inside `analyze` (orchestration true 710/805, no
    offload), blocking the synchronous /health route and starving liveness probes.
    The fix routes it through asyncio.to_thread (the existing offload primitive),
    so the event loop stays responsive during evaluation. Proves by thread
    identity + a blocking simulate: evaluate must run on a worker thread, not the
    loop thread."""
    import threading
    import time
    from unittest.mock import MagicMock
    from uuid import uuid4

    from app.stock_rule_engine import StockRuleEngineService

    main_thread = threading.current_thread()
    seen_threads = []
    sentinel = MagicMock()

    class _SlowEngine:
        def input_fingerprint(self, inputs, *, allow_partial) -> str:
            return "fp"

        def evaluate(self, inputs, *, allow_partial, fingerprint=None):
            # Simulate the synchronous CPU-bound scoring that previously ran on
            # the event loop; record which thread performs it.
            time.sleep(0.05)
            seen_threads.append(threading.current_thread())
            return sentinel

    class _Repo:
        async def stock_rule_engine_result(self, *args, **kwargs):
            return None  # cache miss -> forces the real evaluate path

        async def persist_stock_rule_engine_result(self, *args, **kwargs):
            pass

        def _run_blocking_persistence(self, operation, *args, **kwargs):
            # Mirror the SQLite inline boundary exposed by the real repo.
            return operation(*args, **kwargs)

    class _Adapter:
        async def load(self, profile, readiness, *, now=None):
            return MagicMock()

    service = StockRuleEngineService(_Repo(), _Adapter())
    service.input_adapter = _Adapter()
    service.engine = _SlowEngine()

    profile = SimpleNamespace(instrument_id=uuid4())
    readiness = SimpleNamespace()

    result = await service.analyze(profile, readiness, allow_partial=False)

    assert result is sentinel
    assert seen_threads, "engine.evaluate was never invoked"
    assert seen_threads[0] is not main_thread, (
        "engine.evaluate ran on the event-loop thread (synchronous on-loop blocking "
        "that starves /health); it must be offloaded via asyncio.to_thread"
    )

