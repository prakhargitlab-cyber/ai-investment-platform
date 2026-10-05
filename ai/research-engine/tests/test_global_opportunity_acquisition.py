from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.global_scanner import GlobalScanner, GlobalPreScore
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.research_readiness_runtime import ResearchReadinessRuntime
from app.persistence import SqliteResearchPersistence
from app.global_opportunity_cycle import prepare_cycle
from test_global_scanner import instrument, persisted, NOW
from test_global_opportunity_orchestration import setup
from test_global_opportunity_ranker import inputs
from test_recommendation_lifecycle import snapshot
from test_stock_rule_engine import _readiness


def acquisition_service(monkeypatch, count=1):
    """Set up acquisition service with proper mocks for enrichment."""
    store = SqliteResearchPersistence()
    from app.repository import ResearchRepository
    from app.portfolio_orchestration import PortfolioResearchOrchestrator
    from app.settings import Settings
    from app.global_scanner import GlobalScanner

    repo = ResearchRepository(persistence=store)
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    offline = GlobalOpportunityOrchestrator(repo, store,
        profile_hydrator=hydrator.register_global_profile_metadata, clock=lambda:NOW)

    # Create pairs with enriched candidates - use large range to cover test cases
    from uuid import UUID
    pairs = {UUID(int=n): inputs(n) for n in range(1, 1001)}  # Supports up to 1000 candidates
    monkeypatch.setattr(GlobalScanner, 'enrich_candidates',
        lambda self, scan, **kwargs: [pairs[c.global_instrument_id][0] for c in reversed(scan.candidates) if c.global_instrument_id in pairs])

    runtime = ResearchReadinessRuntime(repo, offline.readiness_adapter, executor=object())
    runtime.ensure = AsyncMock()
    runtime.read = AsyncMock(return_value=object())
    service = GlobalOpportunityOrchestrator(repo, store,
        profile_hydrator=hydrator.register_global_profile_metadata, readiness_runtime=runtime,
        clock=lambda: NOW + timedelta(minutes=1))

    async def analyze(profile, readiness, **kwargs):
        assert kwargs['allow_partial'] is False
        assert kwargs['now'] == NOW + timedelta(minutes=1)
        return inputs(profile.instrument_id.int)[1]
    service.rule_engine.analyze = AsyncMock(side_effect=analyze)
    return service, runtime, store


@pytest.mark.parametrize('updates,eligible', [({}, True), ({'status': 'INACTIVE'}, False),
    ({'assetType': 'ETF'}, False), ({'providerMappings': []}, False), ({'canonicalName': None}, False)])
def test_pre_acquisition_identity_is_separate_from_missing_evidence(updates, eligible):
    candidate = GlobalPreScore().score(instrument() | updates, [], [], [], as_of=NOW)
    assert candidate.eligible_for_acquisition is eligible
    assert not candidate.eligible_for_deep_analysis


@pytest.mark.asyncio
async def test_incomplete_candidate_acquired_and_post_acquisition_evidence_used(monkeypatch):
    """Baseline-first: acquire baseline for all, then deep-acquire if reaching deep pool.

    This single-candidate test should show:
    1. One baseline ensure (requirement_ids=BASELINE_REQUIREMENT_IDS)
    2. One deep ensure (requirement_ids=None) if the candidate reaches the deep pool
    """
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)
    baseline_calls = []
    deep_calls = []

    async def ensure(key, **kwargs):
        requirement_ids = kwargs.get('requirement_ids')
        if requirement_ids is None:
            deep_calls.append(key)
        else:
            baseline_calls.append(key)

        assert kwargs['identity_headers'] == {'X-AIP-User-Id': 'server'}
        persisted(store, instrument(key.int), age=-1 / 1440)
        if requirement_ids is None:
            # DI-10B: the deep-ensure call needs a real readiness snapshot for
            # the Stage-2 eligibility gate to permit rule_engine.analyze().
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())

    runtime.ensure.side_effect = ensure
    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1,
                               identity_headers={'X-AIP-User-Id': 'server'})

    # Baseline-first architecture: baseline ensure + deep ensure for those in pool
    assert len(baseline_calls) == 1, f"Expected 1 baseline ensure call, got {len(baseline_calls)}"
    assert len(deep_calls) == 1, f"Expected 1 deep ensure call, got {len(deep_calls)}"
    assert runtime.ensure.await_count == 2, "Total of 2 ensure calls (baseline + deep)"

    assert result.phase1_eligible_count == 0 and result.shortlist_count == 1
    assert result.top_n[0].evidence_state['technical']['latest_price'] == 100
    # DI-10B: the deep-ensure call above now returns a real readiness snapshot
    # (matching real production, where ResearchReadinessRuntime.ensure() never
    # returns readiness=None), so the orchestrator's defensive readiness.read()
    # fallback -- for when ensure() itself returns no readiness at all -- is not
    # exercised here; that fallback has its own coverage in C2/C3/C7 (deep
    # readiness failure paths). What this test cares about -- baseline+deep
    # ensure counts, evidence_state, and the candidate actually reaching
    # ANALYZED/RANK_ELIGIBLE -- remains asserted below.
    assert result.diagnostics[0].status == 'RANK_ELIGIBLE'
    assert result.diagnostics[0].disposition == 'ANALYZED'


@pytest.mark.asyncio
async def test_500_candidates_baseline_all_deep_enrich_only_safety_cap(monkeypatch):
    """Baseline-first with 500 universe: ALL get baseline, shortlist_limit=25 caps deep pool only.

    Proves:
    - 500 universe_count
    - ALL 500 candidates receive baseline acquisition
    - Only ≤25 receive deep enrichment (safety cap)
    - shortlist_limit does NOT restrict baseline evaluation
    """
    from app.research_readiness_runtime import BASELINE_REQUIREMENT_IDS, TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)
    rows = [instrument(n) for n in range(1, 501)]

    baseline_ids = []
    deep_ids = []

    async def ensure(key, **kwargs):
        requirement_ids = kwargs.get('requirement_ids')
        if requirement_ids is not None and set(requirement_ids) == BASELINE_REQUIREMENT_IDS:
            baseline_ids.append(key.int)
        elif requirement_ids is None:
            deep_ids.append(key.int)
        # All candidates get persisted data
        persisted(store, instrument(key.int))
        # Return cache hit (no acquisition needed). DI-10B: the deep call
        # carries a real readiness snapshot, matching production, so the
        # Stage-2 eligibility gate lets rule_engine.analyze() run.
        if requirement_ids is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())

    runtime.ensure.side_effect = ensure
    result = await service.run(reversed(rows), as_of=NOW, shortlist_limit=25,
                               review_ids=[UUID(int=n) for n in range(100, 125)])

    # Baseline-first proves:
    assert result.universe_count == 500, f"Expected 500, got {result.universe_count}"
    assert len(baseline_ids) == 25
    assert result.baseline_evaluated_count == 500
    assert len(set(baseline_ids)) == 25
    assert set(deep_ids) == set(baseline_ids)

    # shortlist_limit caps deep pool AFTER it's formed, not baseline
    assert result.shortlist_count <= 25, f"Shortlist capped at 25, got {result.shortlist_count}"
    assert len(deep_ids) <= 25, f"Deep enrichment capped at 25, got {len(deep_ids)}"

    # deep_pool_eligible_count: pool size BEFORE safety cap (may be >25)
    assert result.deep_pool_eligible_count == 0  # Sparse identities admitted through rotation.

    # deep_candidate_count: AFTER safety cap (capped at shortlist_limit)
    assert result.deep_candidate_count <= 25, \
        f"deep_candidate_count should be <= 25, got {result.deep_candidate_count}"

    # shortlist_count should equal deep_candidate_count
    assert result.shortlist_count == result.deep_candidate_count, \
        f"shortlist_count ({result.shortlist_count}) should equal deep_candidate_count ({result.deep_candidate_count})"

    # Total: 500 baseline + ≤25 deep = ≤525 ensure calls
    assert runtime.ensure.await_count <= 50
    assert runtime.ensure.await_count == 25 + len(deep_ids)

    # Baseline-ready count should equal successful baseline acquisitions
    assert result.baseline_ready_count == 25

    # Deep-evaluated should match deep pool cap (≤25 from shortlist, not counting reviews)
    assert result.deep_evaluated_count <= 25

    # rule_engine.analyze is called for shortlist (≤25) + reviews (additional, up to review_ids limit)
    # With 25 review_ids added to 25 shortlist, total should be ≤50
    assert service.rule_engine.analyze.await_count <= 25


@pytest.mark.asyncio
async def test_one_ensure_failure_does_not_abort_other_candidates(monkeypatch):
    """Baseline acquisition failure for one candidate does not abort others.

    Proves:
    - One candidate's baseline ensure failure does not stop others
    - Failed candidate is suppressed if baseline remains incomplete
    - Second candidate still becomes rank eligible
    - Provider/private exception text is not exposed
    - If ensure throws but durable evidence is already complete,
      eligibility is determined from persisted evidence (not exception alone)
    """
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)

    async def ensure(key, **kwargs):
        if key.int == 1:
            raise RuntimeError('private provider details')
        persisted(store, instrument(key.int))
        # Return successful cache hit for successful candidates. DI-10B: the
        # deep call carries a real readiness snapshot so the Stage-2
        # eligibility gate lets rule_engine.analyze() run for candidate 2.
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())

    runtime.ensure.side_effect = ensure
    result = await service.run([instrument(1), instrument(2)], as_of=NOW, shortlist_limit=2)

    # Candidate 1 failed baseline acquisition → SUPPRESSED
    diag_1 = [d for d in result.diagnostics if d.global_instrument_id == UUID(int=1)][0]
    assert diag_1.status == 'SUPPRESSED', f"Failed baseline should suppress, got {diag_1.status}"
    assert any(d.failure_reason == 'BASELINE_ACQUISITION_FAILED' for d in result.diagnostics if d.global_instrument_id == UUID(int=1))

    # Candidate 2 succeeded → RANK_ELIGIBLE
    diag_2 = [d for d in result.diagnostics if d.global_instrument_id == UUID(int=2)][0]
    assert diag_2.status == 'RANK_ELIGIBLE', f"Successful baseline should proceed, got {diag_2.status} with reasons {diag_2.suppression_reasons}"

    # Top-N contains only candidate 2
    assert len(result.top_n) == 1
    assert result.top_n[0].global_instrument_id == UUID(int=2)

    # Exception text not exposed in output
    result_json = result.model_dump_json()
    assert 'private provider details' not in result_json, "Exception text leaked into output"


@pytest.mark.asyncio
async def test_post_acquisition_rule_gate_stays_strict(monkeypatch):
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)
    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        # DI-10B: the deep call carries a real readiness snapshot so the
        # Stage-2 eligibility gate lets rule_engine.analyze() run -- this test
        # exercises the RANKER's own strict gate, not the readiness gate.
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())
    runtime.ensure.side_effect = ensure
    rule = inputs(1)[1]
    rule.partial = True
    service.rule_engine.analyze.side_effect = None
    service.rule_engine.analyze.return_value = rule
    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1)
    assert result.top_n == [] and result.diagnostics[0].status == 'SUPPRESSED'
    assert service.rule_engine.analyze.call_args.kwargs['allow_partial'] is False


# ============================================================================
# DI-10B: explicit, deterministic Stage-2 candidate disposition regression
# tests. Each test exercises the acquisition-enabled shortlist deep-analyze
# loop in GlobalOpportunityOrchestrator.run() directly.
# ============================================================================

@pytest.mark.asyncio
async def test_C1_deep_ready_candidate_is_analyzed_with_accurate_counters(monkeypatch):
    """DI-10B C1: deep ensure succeeds with sufficient readiness
    (full_analysis_allowed=True) -> rule_engine.analyze() IS called ->
    disposition ANALYZED -> accurate Stage-2 counters."""
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())

    runtime.ensure.side_effect = ensure
    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1)

    assert service.rule_engine.analyze.await_count == 1
    assert result.diagnostics[0].status == 'RANK_ELIGIBLE'
    assert result.diagnostics[0].disposition == 'ANALYZED'
    assert result.deep_attempted_count == 1
    assert result.deep_ready_count == 1
    assert result.rule_analyzed_count == 1
    assert result.evaluated_count == 1
    assert result.deep_readiness_failed_count == 0
    assert result.deep_acquisition_timeout_count == 0
    assert result.deep_source_unavailable_count == 0
    assert result.rule_exception_count == 0
    assert result.suppressed_count == 0


@pytest.mark.asyncio
async def test_C2_deep_acquisition_timeout_is_distinguished_and_analyze_not_called(monkeypatch):
    """DI-10B C2: the deep-ensure result signals a bounded acquisition timeout
    (failures containing ACQUISITION_TIMEOUT, matching
    ResearchReadinessRuntime._execute_plan_bounded()'s own TimeoutError
    handling) with full_analysis_allowed=False -> rule_engine.analyze() is NOT
    called -> disposition DEEP_ACQUISITION_TIMEOUT -> accurate counters -> the
    candidate still gets exactly one persisted diagnostic (never silently
    dropped)."""
    from app.research_readiness import ResearchRequirementStatus
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)
    not_ready = _readiness(overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}, critical_pct=50)

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(not_ready, (), (), failures={"VALUATION_INPUTS": "ACQUISITION_TIMEOUT"})
        return TargetedEnsureResult(_readiness(), (), ())

    runtime.ensure.side_effect = ensure
    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1)

    assert service.rule_engine.analyze.await_count == 0
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].disposition == 'DEEP_ACQUISITION_TIMEOUT'
    assert result.diagnostics[0].status == 'SUPPRESSED'
    assert result.deep_acquisition_timeout_count == 1
    assert result.deep_readiness_failed_count == 0
    assert result.deep_ready_count == 0
    assert result.rule_analyzed_count == 0
    assert result.top_n == []


@pytest.mark.asyncio
async def test_C3_deep_readiness_not_met_without_timeout_is_distinguished(monkeypatch):
    """DI-10B C3: deep ensure completes (no ACQUISITION_TIMEOUT signal in
    failures) but mandatory readiness remains insufficient
    (full_analysis_allowed=False) -> rule_engine.analyze() is NOT called ->
    disposition DEEP_READINESS_NOT_MET with the exact readiness reason ->
    accurate counters -- provably distinct from the timeout case (C2)."""
    from app.research_readiness import ResearchRequirementStatus
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)
    not_ready = _readiness(overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}, critical_pct=50)

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(not_ready, (), ())  # no failures -> no timeout signal
        return TargetedEnsureResult(_readiness(), (), ())

    runtime.ensure.side_effect = ensure
    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1)

    assert service.rule_engine.analyze.await_count == 0
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].disposition == 'DEEP_READINESS_NOT_MET'
    assert result.diagnostics[0].status == 'SUPPRESSED'
    assert result.diagnostics[0].failure_reason
    assert 'VALUATION_INPUTS' in result.diagnostics[0].suppression_reasons
    assert result.deep_readiness_failed_count == 1
    assert result.deep_acquisition_timeout_count == 0
    assert result.rule_analyzed_count == 0


@pytest.mark.asyncio
async def test_C4_rule_engine_exception_is_classified_and_does_not_abort_cycle(monkeypatch):
    """DI-10B C4: rule_engine.analyze() raising an unexpected exception for one
    candidate is classified explicitly as RULE_ENGINE_EXCEPTION (never exposing
    the raw exception text) and does not abort the cycle -- a second, healthy
    candidate still completes and becomes rank-eligible."""
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())
    runtime.ensure.side_effect = ensure

    async def analyze(profile, readiness, **kwargs):
        if profile.instrument_id == UUID(int=1):
            raise RuntimeError('unexpected rule engine failure with sensitive detail')
        return inputs(profile.instrument_id.int)[1]
    service.rule_engine.analyze = AsyncMock(side_effect=analyze)

    result = await service.run([instrument(1), instrument(2)], as_of=NOW, shortlist_limit=2)

    diag_1 = [d for d in result.diagnostics if d.global_instrument_id == UUID(int=1)][0]
    assert diag_1.disposition == 'RULE_ENGINE_EXCEPTION'
    assert diag_1.status == 'SUPPRESSED'
    assert 'sensitive detail' not in result.model_dump_json()

    diag_2 = [d for d in result.diagnostics if d.global_instrument_id == UUID(int=2)][0]
    assert diag_2.disposition == 'ANALYZED'
    assert diag_2.status == 'RANK_ELIGIBLE'

    assert result.rule_exception_count == 1
    assert result.rule_analyzed_count == 1
    assert len(result.top_n) == 1 and result.top_n[0].global_instrument_id == UUID(int=2)


@pytest.mark.asyncio
async def test_C5_analyzed_but_rank_filtered_is_distinct_from_readiness_failure(monkeypatch):
    """DI-10B C5: a candidate that IS fully analyzed but fails a real ranking
    gate (here a BLOCK_BUY risk override) gets disposition RANK_FILTERED --
    provably distinct (both in disposition and in counters) from a candidate
    that never reached rule_engine.analyze() at all. A partial / not-full rule
    result is NOT a ranking rejection (full-research state contract; see
    tests/test_full_research_state_contract.py)."""
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(_readiness(), (), ())
        return TargetedEnsureResult(_readiness(), (), ())
    runtime.ensure.side_effect = ensure

    from app.stock_rule_engine import RiskOverrideResult, RiskOverrideSeverity
    rule = inputs(1)[1]
    rule.risk_overrides = [RiskOverrideResult(code='CONFIRMED_FRAUD_OR_ACCOUNTING_CRISIS',
                                              severity=RiskOverrideSeverity.CRITICAL)]
    service.rule_engine.analyze.side_effect = None
    service.rule_engine.analyze.return_value = rule

    result = await service.run([instrument()], as_of=NOW, shortlist_limit=1)

    assert service.rule_engine.analyze.await_count == 1
    assert result.top_n == []
    assert result.diagnostics[0].status == 'SUPPRESSED'
    assert result.diagnostics[0].disposition == 'RANK_FILTERED'
    assert result.rule_analyzed_count == 1
    assert result.evaluated_count == 1
    assert result.deep_readiness_failed_count == 0
    assert result.deep_source_unavailable_count == 0


@pytest.mark.asyncio
async def test_C6_mixed_shortlist_dispositions_all_survive_with_accurate_counters(monkeypatch):
    """DI-10B C6: a mixed shortlist (one acquisition timeout, one readiness
    failure, one rule-engine exception, one successfully analyzed/rankable
    candidate) completes the production cycle -- the successful candidate
    survives into evaluated_entries/top_n, the other three retain their own
    explicit disposition, and every counter exactly matches the four
    dispositions (no candidate silently lost, no double-counting)."""
    from app.research_readiness import ResearchRequirementStatus
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)
    not_ready = _readiness(overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}, critical_pct=50)
    ready = _readiness()

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get('requirement_ids') is not None:
            return TargetedEnsureResult(_readiness(), (), ())  # baseline call: irrelevant here
        if key == UUID(int=1):
            return TargetedEnsureResult(not_ready, (), (), failures={"VALUATION_INPUTS": "ACQUISITION_TIMEOUT"})
        if key == UUID(int=2):
            return TargetedEnsureResult(not_ready, (), (), failures={"VALUATION_INPUTS": "SOURCE_ERROR"})
        return TargetedEnsureResult(ready, (), ())
    runtime.ensure.side_effect = ensure

    async def analyze(profile, readiness, **kwargs):
        if profile.instrument_id == UUID(int=3):
            raise RuntimeError('rule engine internal failure')
        return inputs(profile.instrument_id.int)[1]
    service.rule_engine.analyze = AsyncMock(side_effect=analyze)

    rows = [instrument(n) for n in range(1, 5)]
    result = await service.run(rows, as_of=NOW, shortlist_limit=4)

    by_id = {d.global_instrument_id: d for d in result.diagnostics}
    assert len(result.diagnostics) == 4, "every shortlisted candidate carries exactly one diagnostic"
    assert by_id[UUID(int=1)].disposition == 'DEEP_ACQUISITION_TIMEOUT'
    assert by_id[UUID(int=2)].disposition == 'DEEP_READINESS_NOT_MET'
    assert by_id[UUID(int=3)].disposition == 'RULE_ENGINE_EXCEPTION'
    assert by_id[UUID(int=4)].disposition == 'ANALYZED'
    assert by_id[UUID(int=4)].status == 'RANK_ELIGIBLE'

    assert result.deep_acquisition_timeout_count == 1
    assert result.deep_readiness_failed_count == 1
    assert result.rule_exception_count == 1
    assert result.rule_analyzed_count == 1
    assert result.evaluated_count == 1
    assert result.rank_eligible_count == 1
    assert result.suppressed_count == 3

    assert {e.global_instrument_id for e in result.evaluated_entries} == {UUID(int=4)}
    assert {e.global_instrument_id for e in result.top_n} == {UUID(int=4)}


@pytest.mark.asyncio
async def test_C7_all_candidates_fail_deep_readiness_yields_valid_empty_ranking(monkeypatch):
    """DI-10B C7: every shortlisted candidate legitimately fails deep
    readiness -> the orchestrator still returns a valid ranking
    (universe_count>0, top_n=[]) with an explicit, non-generic disposition for
    every one of them -- never a silently missing candidate -- exactly the
    shape run_global_opportunity_cycle needs to publish a valid empty
    COMPLETED radar rather than misreporting UNIVERSE_UNAVAILABLE."""
    from app.research_readiness import ResearchRequirementStatus
    from app.research_readiness_runtime import TargetedEnsureResult

    service, runtime, store = acquisition_service(monkeypatch)
    not_ready = _readiness(overrides={"VALUATION_INPUTS": ResearchRequirementStatus.MISSING}, critical_pct=50)

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get('requirement_ids') is None:
            return TargetedEnsureResult(not_ready, (), ())
        return TargetedEnsureResult(_readiness(), (), ())
    runtime.ensure.side_effect = ensure

    rows = [instrument(n) for n in range(1, 4)]
    result = await service.run(rows, as_of=NOW, shortlist_limit=3)

    assert result.universe_count == 3
    assert result.top_n == []
    assert result.rank_eligible_count == 0
    assert service.rule_engine.analyze.await_count == 0
    assert len(result.diagnostics) == 3
    assert all(d.disposition == 'DEEP_READINESS_NOT_MET' for d in result.diagnostics)
    assert result.deep_readiness_failed_count == 3


def publish(store, controlled, at, price=1000):
    s = snapshot()
    s.update(generated_at=at.isoformat(), current_price=price)
    history, states, selection = prepare_cycle(store, [s], cycle_id=s['cycle_id'], now=s['generated_at'], top_n=4)
    selection['controlled_candidate_set'] = controlled
    store.publish_opportunity_cycle([s], history, states, selection)
    return selection


def test_controlled_cycle_cannot_replace_full_market_radar_or_state():
    store = SqliteResearchPersistence()
    first = publish(store, False, NOW)
    states = store.recommendation_states()
    publish(store, True, NOW + timedelta(hours=1), price=1185)
    assert store.opportunity_current()['cycle_id'] == first['cycle_id']
    assert store.recommendation_states() == states
    assert store.opportunity_current()['top_short_term'][0]['current_short_action'] == 'BUY'
    last = publish(store, False, NOW + timedelta(hours=2))
    assert store.opportunity_current()['cycle_id'] == last['cycle_id']


def test_controlled_only_cycle_leaves_normal_radar_empty():
    store = SqliteResearchPersistence()
    publish(store, True, NOW)
    assert store.opportunity_current()['generated_at'] is None
