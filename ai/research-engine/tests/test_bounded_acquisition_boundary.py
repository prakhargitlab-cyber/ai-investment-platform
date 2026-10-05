"""Admission precedes every live acquisition; persisted evaluation stays exhaustive."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, PHASE_BASELINE
from app.deep_investigation import build_plan
from app.global_opportunity_orchestration import ACQUISITION_SELECTION_VERSION
from app.global_scanner import GlobalScanner
from app.research_readiness import ResearchRequirementStatus as Status
from app.research_readiness_runtime import BASELINE_REQUIREMENT_IDS, TargetedEnsureResult
from test_global_opportunity_baseline import setup_acquisition
from test_global_scanner import instrument, persisted, NOW
from test_stock_rule_engine import _readiness


@pytest.mark.asyncio
async def test_2609_provider_free_evaluations_admit_only_25_sparse_candidates(monkeypatch):
    service, _, _, store, tracker = setup_acquisition(monkeypatch, count=0)
    rows = [instrument(n) for n in range(1, 2610)]
    scored, acquired, deep = [], [], []
    stage_a_writes = []
    original = GlobalScanner._score_batch

    def score(scanner, ids, metadata, at):
        scored.extend(sorted(ids, key=str))
        return original(scanner, ids, metadata, at)

    monkeypatch.setattr(GlobalScanner, "_score_batch", score)
    def trace(sql):
        if not acquired and sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            stage_a_writes.append(sql)
    store._connection.set_trace_callback(trace)
    forbidden = AsyncMock(side_effect=AssertionError("Unexpected network/PDF acquisition"))
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    service.repository.refresh_targeted_categories = forbidden

    async def ensure(key, **kwargs):
        assert len(scored) == 2609
        assert set(kwargs["requirement_ids"]) == BASELINE_REQUIREMENT_IDS
        acquired.append(key)
        return await tracker(key, **kwargs)

    async def investigate(runtime, key, **kwargs):
        assert key in acquired
        assert len(scored) == 2609 + 25  # Only admitted candidates are rescanned.
        deep.append(key)
        readiness = replace(_readiness({"GOVERNANCE_HISTORY": Status.MISSING}), global_instrument_id=key)
        return TargetedEnsureResult(readiness, (), ()), build_plan(readiness), {}

    service.readiness.ensure = ensure
    monkeypatch.setattr("app.deep_investigation.investigate", investigate)
    result = await service.run(reversed(rows), as_of=NOW, shortlist_limit=25, discovery_v2=True)
    assert set(scored[:2609]) == {UUID(int=n) for n in range(1, 2610)}
    assert len(acquired) == len(set(acquired)) == len(deep) == 25
    assert set(acquired) == set(deep)
    assert result.baseline_evaluated_count == 2609
    assert result.acquisition_admitted_count == result.deep_candidate_count == 25
    assert result.acquisition_deferred_count == 2584
    assert not stage_a_writes
    forbidden.assert_not_called()
    service.rule_engine.analyze.assert_not_called()  # Missing mandatory governance.
    assert not result.evaluated_entries and not result.top_n
    assert all(d.disposition == "DEFERRED" for d in result.diagnostics if d.global_instrument_id not in acquired)
    for key, matrix in result.investigation_matrix.items():
        if UUID(key) not in acquired:
            assert all(row["acquisition_state"] == "DEFERRED" and not row["attempted"]
                       for row in matrix["requirements"].values())


@pytest.mark.asyncio
async def test_admission_and_results_ignore_input_and_acquisition_completion_order(monkeypatch):
    async def run(reverse):
        service, rows, _, _, tracker = setup_acquisition(monkeypatch, count=40)
        completed = []
        async def ensure(key, **kwargs):
            if kwargs.get("requirement_ids") is not None:
                await asyncio.sleep((key.int if reverse else 41 - key.int) / 1000)
                completed.append(key)
            return await tracker(key, **kwargs)
        service.readiness.ensure = ensure
        result = await service.run(list(reversed(rows)) if reverse else rows, as_of=NOW, shortlist_limit=25,
                                   review_ids=[UUID(int=40)], top_n=None)
        admitted = {key for key, _ in tracker.baseline_calls}
        deferred = {d.global_instrument_id for d in result.diagnostics if d.disposition == "DEFERRED"}
        analyzed = {call.args[0].instrument_id for call in service.rule_engine.analyze.await_args_list}
        assert len(admitted) == 25 and len(deferred) == 15
        assert analyzed <= admitted
        assert not deferred & analyzed
        assert not deferred & {e.global_instrument_id for e in result.evaluated_entries + result.top_n}
        assert UUID(int=40) in deferred
        assert str(UUID(int=40)) in result.review_evidence  # Price lifecycle only.
        return result, completed
    first, first_order = await run(False)
    second, second_order = await run(True)
    assert first_order != second_order
    assert first.model_dump(exclude={"generated_at"}) == second.model_dump(exclude={"generated_at"})


@pytest.mark.asyncio
@pytest.mark.parametrize('timeout_reason', ['ACQUISITION_TIMEOUT', 'TimeoutError'])
async def test_timeout_return_is_not_baseline_ready_and_mandatory_gate_stays_closed(monkeypatch, caplog, timeout_reason):
    caplog.set_level('INFO', logger='app.global_opportunity_orchestration')
    service, rows, _, _, tracker = setup_acquisition(monkeypatch, count=2)
    async def ensure(key, **kwargs):
        if key.int == 1:
            return TargetedEnsureResult(_readiness({"LATEST_PRICE": Status.MISSING}), ("LATEST_PRICE",),
                                        ("STRUCTURED_MARKET",), failures={"LATEST_PRICE": timeout_reason})
        return await tracker(key, **kwargs)
    service.readiness.ensure = ensure
    result = await service.run(rows, as_of=NOW, shortlist_limit=2)
    assert result.baseline_evaluated_count == result.acquisition_admitted_count == 2
    assert result.baseline_ready_count == result.baseline_incomplete_count == 1
    assert result.baseline_acquisition_needed_count == result.baseline_acquisition_attempted_count == 1
    assert result.baseline_acquisition_timeout_count == result.baseline_acquisition_failed_count == 1
    assert {call.args[0].instrument_id for call in service.rule_engine.analyze.await_args_list} == {UUID(int=2)}
    assert UUID(int=1) not in {e.global_instrument_id for e in result.evaluated_entries + result.top_n}
    assert 'baseline_incomplete=1' in caplog.text
    assert 'baseline_failed=' not in caplog.text


async def checkpoint(store, *, selection=None):
    cycle_id = str(uuid4())
    store.create_cycle_run(cycle_id, {"top_n": 4, "shortlist_limit": 25})
    store.claim_cycle_run(cycle_id, "test-owner", lease_seconds=60)
    handle = await CycleCheckpoint(store, cycle_id, "test-owner").load()
    if selection is not None:
        await handle.save_selection(selection)
    return handle


@pytest.mark.asyncio
async def test_legacy_unbounded_checkpoint_is_capped_before_baseline_and_remains_bounded_on_resume(monkeypatch):
    service, rows, _, store, tracker = setup_acquisition(monkeypatch, count=40)
    ids = [str(UUID(int=n)) for n in range(40, 0, -1)]
    handle = await checkpoint(store, selection={"deep_ids": ids, "shortlist_ids": ids})
    for key in ids:
        await handle.start(PHASE_BASELINE, key)
        await handle.finish(PHASE_BASELINE, key, CandidateState.COMPLETED,
                            payload={"has_outcome": True, "planned_requirement_ids": []})
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, checkpoint=handle)
    assert {key for key, _ in tracker.baseline_calls} == {UUID(key) for key in ids[:25]}
    assert result.acquisition_admitted_count == 25 and result.acquisition_deferred_count == 15
    selection = await handle.selection()
    assert selection["acquisition_selection_version"] == ACQUISITION_SELECTION_VERSION
    assert selection["deep_ids"] == ids[:25]
    tracker.calls.clear()
    service.rule_engine.analyze.reset_mock()
    await handle.load()
    resumed = await service.run(reversed(rows), as_of=NOW, shortlist_limit=25, checkpoint=handle)
    assert not tracker.calls
    service.rule_engine.analyze.assert_not_called()
    assert resumed.acquisition_admitted_count == 25
    assert {e.global_instrument_id for e in resumed.evaluated_entries} <= {UUID(key) for key in ids[:25]}


@pytest.mark.asyncio
async def test_selection_is_saved_before_acquisition_and_cancel_prevents_further_baseline_dispatch(monkeypatch):
    service, rows, _, store, tracker = setup_acquisition(monkeypatch, count=40)
    handle = await checkpoint(store)
    started = []
    async def ensure(key, **kwargs):
        selection = await handle.selection()
        assert len(selection["deep_ids"]) == 25
        started.append(key)
        store.request_cycle_cancel(handle.cycle_id)
        return await tracker(key, **kwargs)
    service.readiness.ensure = ensure
    with pytest.raises(asyncio.CancelledError):
        await service.run(rows, as_of=NOW, shortlist_limit=25, checkpoint=handle)
    assert len(started) == 1
    service.rule_engine.analyze.assert_not_called()
    assert store.cycle_run(handle.cycle_id)["status"] == "CANCEL_REQUESTED"


@pytest.mark.asyncio
async def test_resume_preserves_missing_selected_identity_without_admitting_replacements(monkeypatch):
    service, rows, _, store, tracker = setup_acquisition(monkeypatch, count=3)
    ids = [str(UUID(int=n)) for n in (1, 2)]
    handle = await checkpoint(store, selection={"deep_ids": ids, "shortlist_ids": ids})
    first = await service.run(rows[1:], as_of=NOW, shortlist_limit=2, checkpoint=handle)
    assert {key for key, _ in tracker.baseline_calls} == {UUID(int=2)}
    assert (await handle.selection())["deep_ids"] == ids
    assert any(d.global_instrument_id == UUID(int=1) and
               d.failure_reason == "SELECTED_CANDIDATE_NOT_IN_RESCAN" for d in first.diagnostics)
    tracker.calls.clear()
    await handle.load()
    resumed = await service.run(rows, as_of=NOW, shortlist_limit=2, checkpoint=handle)
    assert {key for key, _ in tracker.baseline_calls} == {UUID(int=1)}
    assert resumed.acquisition_admitted_count == 2
    assert any(d.global_instrument_id == UUID(int=3) and d.disposition == "DEFERRED"
               for d in resumed.diagnostics)


@pytest.mark.asyncio
async def test_rotation_eventually_admits_every_sparse_identity(monkeypatch):
    service, _, _, _, tracker = setup_acquisition(monkeypatch, count=0)
    rows = [instrument(n) for n in range(1, 14)]
    cursor, seen = None, set()
    service.readiness.read = AsyncMock(return_value=_readiness({"GOVERNANCE_HISTORY": Status.MISSING}))
    # No deep evidence is available: rotation still progresses independently.
    async def investigate(runtime, key, **kwargs):
        readiness = await runtime.read(key, jurisdiction="INDIA")
        return TargetedEnsureResult(readiness, (), ()), build_plan(readiness), {}
    monkeypatch.setattr("app.deep_investigation.investigate", investigate)
    for _ in range(5):
        tracker.calls.clear()
        result = await service.run(rows, as_of=NOW, shortlist_limit=3, discovery_v2=True, rotation_after=cursor)
        cursor = result.rotation_after
        seen.update(key for key, _ in tracker.baseline_calls)
        assert len(tracker.baseline_calls) == 3
    assert seen == {UUID(int=n) for n in range(1, 14)}


@pytest.mark.asyncio
async def test_highest_id_last_in_input_order_still_admitted_on_evidence_alone(monkeypatch):
    """The pre-admission nomination stage ranks the entire 2,609-stock universe
    by persisted evidence, never by instrument id or input list position.

    Candidate #2609 -- the highest id, and here also the very last row in
    input order -- carries the only persisted growth evidence in an
    otherwise fully sparse 2,609-stock universe (every other candidate has
    no persisted financial facts at all, so it earns no evidence-based
    nomination). If admission secretly worked by taking the first N
    candidates (by id or by input position) and only then "analyzing" them,
    #2609 could never be admitted: it is both the numerically largest id and
    the structurally last candidate under any such truncate-then-analyze
    bug. It is admitted here purely because nominate_compact() scores it on
    GROWTH_QUALITY and the nomination pools are sorted by that score, not by
    id or position (see _form_dynamic_deep_pool and discover() in
    app/global_opportunity_orchestration.py / app/opportunity_discovery.py).

    The same admitted set results whether the universe is supplied in
    natural or shuffled order, proving the full universe is evaluated
    before, and independently of, the bounded 25-candidate admission step.
    """
    import random

    service, _, _, store, tracker = setup_acquisition(monkeypatch, count=0)
    rows = [instrument(n) for n in range(1, 2610)]
    # Only the last-by-id, last-in-input-order candidate carries any growth
    # evidence; the remaining 2,608 candidates are fully sparse (no
    # persisted structured market data of any kind).
    persisted(store, rows[-1], metrics={"revenueGrowth": 40, "earningsGrowth": 35}, price=False)

    async def investigate(runtime, key, **kwargs):
        # Mirrors test_2609_provider_free_evaluations_admit_only_25_sparse_candidates:
        # deliberately incomplete so no admitted candidate reaches
        # rule_engine.analyze() (this test is about *admission*, not ranking
        # of final recommendations).
        readiness = replace(_readiness({"GOVERNANCE_HISTORY": Status.MISSING}), global_instrument_id=key)
        return TargetedEnsureResult(readiness, (), ()), build_plan(readiness), {}

    monkeypatch.setattr("app.deep_investigation.investigate", investigate)

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, discovery_v2=True)
    admitted = {key for key, _ in tracker.baseline_calls}
    assert result.baseline_evaluated_count == 2609
    assert len(admitted) == 25
    assert UUID(int=2609) in admitted
    service.rule_engine.analyze.assert_not_called()

    tracker.calls.clear()
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    result2 = await service.run(shuffled, as_of=NOW, shortlist_limit=25, discovery_v2=True)
    admitted2 = {key for key, _ in tracker.baseline_calls}
    assert admitted2 == admitted
    assert result2.baseline_evaluated_count == 2609
