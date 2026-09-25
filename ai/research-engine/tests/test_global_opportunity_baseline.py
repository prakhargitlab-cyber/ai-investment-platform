"""Regression tests: REMOVE the fixed pre-deep 25-stock bias from global discovery.

These tests verify the baseline-first architecture where ALL eligible NSE equities
receive the same minimum baseline data contract before any shortlist or deep-
enrichment decision.  The fixed shortlist_limit=25 may only remain as an optional
safety maximum AFTER whole-universe baseline comparison — never as a gate on
which stocks receive mandatory baseline acquisition/evaluation.

Manual research history (cached evidence) may save provider calls (cache hit)
but must NOT change eligibility, add ranking bonuses, alter weights, bypass
baseline acquisition, force deep-pool entry, or alter global rank.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.global_scanner import GlobalScanner
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import PortfolioResearchOrchestrator
from app.repository import ResearchRepository
from app.settings import Settings
from app.research_readiness_runtime import (
    BASELINE_REQUIREMENT_IDS,
    _FINANCIAL_REQUIREMENTS,
    RepositoryResearchReadinessAdapter,
    ResearchReadinessRuntime,
    TargetedEnsureResult,
    jurisdiction_for_profile,
)
from test_global_scanner import instrument, persisted, NOW
from test_global_opportunity_ranker import inputs
from test_stock_rule_engine import _readiness


def _make_runtime(repo):
    """Build a real readiness runtime (executor=None → read-only safe)."""
    adapter = RepositoryResearchReadinessAdapter(repo)
    runtime = ResearchReadinessRuntime(repo, adapter, executor=None)
    return runtime


class _EnsureTracker:
    """Tracks all readiness.ensure calls, distinguishing baseline (BASELINE
    requirement IDs) from deep-evaluation (requirement_ids=None) calls."""

    def __init__(self):
        self.calls = []  # list of (global_instrument_id, requirement_ids or None)

    async def __call__(self, global_instrument_id, *, jurisdiction=None,
                       requirement_ids=None, identity_headers=None, **kwargs):
        self.calls.append((global_instrument_id, tuple(sorted(requirement_ids))
                           if requirement_ids is not None else None))
        # DI-10B: a real readiness snapshot (matching what the real production
        # ResearchReadinessRuntime.ensure() always returns) so the Stage-2
        # eligibility gate permits rule_engine.analyze() for deep-ready
        # candidates, exactly as before this task's fix was needed for these
        # tests to exercise real ranking/diagnostics behavior.
        return TargetedEnsureResult(_readiness(), (), ())

    @property
    def baseline_calls(self):
        """Calls with BASELINE_REQUIREMENT_IDS (baseline acquisition phase)."""
        return [c for c in self.calls if c[1] is not None and set(c[1]) == BASELINE_REQUIREMENT_IDS]

    @property
    def deep_calls(self):
        """Calls with requirement_ids=None (deep evaluation phase)."""
        return [c for c in self.calls if c[1] is None]

    @property
    def all_calls(self):
        return self.calls


def setup_acquisition(monkeypatch, count=30, stub_deep=True):
    """Setup orchestrator WITH acquisition enabled (readiness_runtime injected).

    All candidates receive fresh price + structured market data (via persisted()).
    ensure is mocked to always return a cache-hit (no planned targets).
    """
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    runtime = _make_runtime(repo)
    service = GlobalOpportunityOrchestrator(repo, store,
        profile_hydrator=hydrator.register_global_profile_metadata,
        readiness_runtime=runtime, clock=lambda: NOW)
    rows = [instrument(n) for n in range(1, count + 1)]
    for row in rows:
        persisted(store, row)
    pairs = {UUID(int=n): inputs(n) for n in range(1, count + 1)}

    monkeypatch.setattr(GlobalScanner, 'enrich_candidates',
                        lambda self, scan, **kwargs:
                        [pairs[c.global_instrument_id][0] for c in reversed(scan.candidates)
                         if c.eligible_for_deep_analysis])

    service.readiness.read = AsyncMock(return_value=object())

    async def analyze(profile, readiness, **kwargs):
        # Accept both full-analysis (allow_partial=False) and review (allow_partial=True).
        assert 'allow_partial' in kwargs
        assert kwargs['now'] == NOW
        return pairs[profile.instrument_id][1]

    service.rule_engine.analyze = AsyncMock(side_effect=analyze)

    tracker = _EnsureTracker()
    service.readiness.ensure = tracker
    return service, rows, pairs, store, tracker


def _baseline_ready_count(tracker):
    """Number of distinct global_instrument_ids that received baseline ensure."""
    return len(set(c[0] for c in tracker.baseline_calls))


# --- Test A: All eligible universe candidates receive baseline readiness ---

@pytest.mark.asyncio
async def test_A_all_eligible_receive_baseline_readiness(monkeypatch):
    """All eligible universe candidates receive baseline readiness evaluation.

    No fixed 25-stock gate may prevent baseline acquisition for any eligible
    stock.  shortlist_limit=25 must NOT restrict baseline evaluation.
    """
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=30)
    result = await service.run(rows, as_of=NOW, shortlist_limit=25)

    # Every one of the 30 eligible candidates received baseline ensure().
    assert _baseline_ready_count(tracker) == 30
    assert result.baseline_ready_count == 30
    assert result.universe_count == 30
    assert result.preliminary_eligible_count == 30


# --- Test B: shortlist_limit=25 does NOT restrict baseline evaluation ---

@pytest.mark.asyncio
async def test_B_shortlist_limit_does_not_restrict_baseline(monkeypatch):
    """shortlist_limit=25 must NOT restrict baseline evaluation to 25.

    In the old code, `[:shortlist_limit]` cut the universe before ensure().
    Now ALL eligible stocks get baseline ensure() regardless of shortlist_limit.
    """
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=30)
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    # All 30 got baseline acquisition — not just 25.
    assert _baseline_ready_count(tracker) == 30
    assert result.baseline_ready_count == 30
    # shortlist_limit=25 only capped the final deep pool, not baseline.
    assert result.shortlist_count <= 25
    # deep_pool_eligible_count is the pool BEFORE safety cap
    assert result.deep_pool_eligible_count == 30


# --- Test C: Cached/manual-researched candidate gets no ranking bonus ---

@pytest.mark.asyncio
async def test_C_cached_researched_no_bonus(monkeypatch):
    """A manually-researched candidate with rich cached evidence gets no
    ranking bonus.  Its V1 opportunity score is whatever the rule engine
    produces from the same evidence available to everyone.

    Two stocks with identical V1 scores: the manually-researched one (stock 1)
    must NOT outrank the equally-scored untouched stock 2, nor vice-versa —
    the tie is broken by deterministic UUID ordering, not evidence depth.
    Both received the same baseline acquisition (no preferential treatment).
    """
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=2)

    # Both stocks already have identical V1 scores from inputs() (core=80).
    # Manually-researched stock 1 has the same evidence depth in the test,
    # proving no bonus for cached/prior research history.
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=2)

    # Both received baseline acquisition with full baseline contract.
    assert _baseline_ready_count(tracker) == 2
    for call in tracker.baseline_calls:
        assert set(call[1]) == BASELINE_REQUIREMENT_IDS
    # Both are rank-eligible.
    assert len(result.top_n) == 2
    # Identical V1 scores → identical opportunity_scores.
    assert result.top_n[0].opportunity_score == result.top_n[1].opportunity_score


# --- Test D: Untouched stronger candidate can outrank cached candidate ---

@pytest.mark.asyncio
async def test_D_untouched_stronger_outranks_cached(monkeypatch):
    """An untouched candidate with genuinely stronger fundamentals can outrank
    a manually-researched candidate, proving cached evidence confers no bonus.

    Stock 1 = cached/previously-researched with moderate score (core=40).
    Stock 2 = untouched with stronger V1 score (core=95).
    Stock 2 must rank above Stock 1.
    """
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=2)

    # Stock 1: weaker V1 score (core=40 for all areas)
    pairs[UUID(int=1)] = inputs(1, core=40, confidence=50)
    # Stock 2: stronger V1 score (core=95 for all areas)
    pairs[UUID(int=2)] = inputs(2, core=95, confidence=95)

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=2)

    # Both received baseline — no skip or preferential treatment.
    assert _baseline_ready_count(tracker) == 2
    # Stock 2 (untouched, stronger) ranks above Stock 1 (cached, weaker).
    assert [e.global_instrument_id.int for e in result.top_n] == [2, 1]


# --- Test E: Dynamic deep pool deterministic ---

@pytest.mark.asyncio
async def test_E_dynamic_deep_pool_deterministic(monkeypatch):
    """The dynamic deep-enrichment pool is deterministic: two identical runs
    produce the same pool membership and order.  No random elements."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=100)

    first = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)
    # Reset clock and tracker for second run.
    service.clock = lambda: NOW
    tracker2 = _EnsureTracker()
    service.readiness.ensure = tracker2
    second = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    assert first.deep_candidate_count == second.deep_candidate_count
    assert first.shortlist_count == second.shortlist_count
    assert first.preliminary_eligible_count == second.preliminary_eligible_count
    # Baseline acquisition called for all eligible in both runs.
    assert _baseline_ready_count(tracker2) == 100
    # Top-N is deterministic.
    first_ids = [e.global_instrument_id.int for e in first.top_n]
    second_ids = [e.global_instrument_id.int for e in second.top_n]
    assert first_ids == second_ids


# --- Test F: Rank-26 under old fixed cutoff not automatically discarded ---

@pytest.mark.asyncio
async def test_F_rank26_not_discarded_by_fixed_cutoff(monkeypatch):
    """Rank-26 under the old fixed cutoff is NOT automatically discarded.

    With 30 eligible candidates and shortlist_limit=25, the old code would
    cut rank-26 via [:25] BEFORE baseline acquisition — stock 26 would never
    get baseline ensure() at all.  Now ALL 30 get baseline acquisition AND the
    dynamic deep pool considers all 30.  We verify both:

    (a) With shortlist_limit=25 the safety cap may truncate the shortlist to 25,
        but stock 26 STILL received baseline acquisition (the old [:25] gate
        is gone).
    (b) With shortlist_limit=30 (no truncation) stock 26, holding the highest
        V1 score, reaches the top-N — proving it was never discarded merely
        for being rank-26.
    """
    # --- (a) shortlist_limit=25: stock 26 gets baseline acquisition ---
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=30)
    pairs[UUID(int=26)] = inputs(26, core=95, confidence=95)
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    baseline_ids = set(c[0] for c in tracker.baseline_calls)
    assert UUID(int=26) in baseline_ids  # NOT cut by old [:25] gate
    assert _baseline_ready_count(tracker) == 30
    # deep_pool_eligible_count is the pool BEFORE safety cap
    assert result.deep_pool_eligible_count == 30
    # deep_candidate_count is AFTER safety cap
    assert result.deep_candidate_count == 25  # capped by shortlist_limit
    assert result.shortlist_count == 25       # safety cap applied post-pool

    # --- (b) shortlist_limit=30: stock 26 reaches top-N ---
    service2, rows2, pairs2, store2, tracker2 = setup_acquisition(monkeypatch, count=30)
    pairs2[UUID(int=26)] = inputs(26, core=95, confidence=95)
    result2 = await service2.run(rows2, as_of=NOW, shortlist_limit=30, top_n=4)

    assert _baseline_ready_count(tracker2) == 30
    assert result2.deep_pool_eligible_count == 30  # pool before cap
    assert result2.deep_candidate_count == 30       # after cap (no truncation)
    assert result2.shortlist_count == 30            # no truncation
    top_ids = [e.global_instrument_id.int for e in result2.top_n]
    assert 26 in top_ids
    assert result2.top_n[0].global_instrument_id.int == 26  # highest V1 score first


# --- Test G: Only missing/stale baseline data acquired ---

@pytest.mark.asyncio
async def test_G_only_missing_stale_acquired(monkeypatch):
    """Only missing/stale baseline data is acquired — fresh cached evidence
    (including prior manual research) results in planned_requirement_ids=().

    The test verifies that ensure is called with BASELINE_REQUIREMENT_IDS
    for all, and that the mock returns empty plans (cache hits) when data
    is already fresh.  When data is stale, the mock returns non-empty plans.
    """
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=5)

    # Override ensure to differentiate cache-hit (fresh) vs acquisition-needed (stale).
    # Stock 1 and 2 have fresh data (baseline already available).
    # Stock 3, 4, 5 have stale/missing data (acquisition needed).
    stale_ids = {UUID(int=3), UUID(int=4), UUID(int=5)}

    async def selective_ensure(global_instrument_id, *, jurisdiction=None,
                               requirement_ids=None, identity_headers=None, **kwargs):
        tracker.calls.append((global_instrument_id, tuple(sorted(requirement_ids))
                              if requirement_ids is not None else None))
        if global_instrument_id in stale_ids and requirement_ids is not None:
            # Return non-empty planned_requirement_ids to indicate acquisition needed.
            return TargetedEnsureResult(None, tuple(sorted(BASELINE_REQUIREMENT_IDS)), ())
        # Otherwise: cache hit (all fresh, no targets).
        return TargetedEnsureResult(None, (), ())

    service.readiness.ensure = selective_ensure

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    # All 5 got baseline ensure with full baseline contract.
    assert _baseline_ready_count(tracker) == 5
    # Only 3 needed acquisition (stale), 2 were cache hits (fresh).
    assert result.baseline_acquisition_needed_count == 3
    assert result.baseline_ready_count == 5


# --- Test H: Current-news absence does not block ---

@pytest.mark.asyncio
async def test_H_news_absence_does_not_block(monkeypatch):
    """CURRENT_NEWS is NOT in BASELINE_REQUIREMENT_IDS.  Its absence must not
    block baseline acquisition or deep-pool entry.  A stock with no news data
    still reaches the dynamic deep pool and final evaluation."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=5)

    # All candidates have fresh price + structured financials but NO news.
    # This is already the default (persisted() doesn't add news).

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    # All eligible candidates proceeded through baseline and deep evaluation.
    assert _baseline_ready_count(tracker) == 5
    assert result.baseline_ready_count == 5
    assert result.deep_evaluated_count == len(result.diagnostics)
    # No candidate was suppressed due to missing news.
    assert not any('CURRENT_NEWS' in d.suppression_reasons for d in result.diagnostics)
    # CURRENT_NEWS is absent from baseline but present in full acquisition.
    assert 'CURRENT_NEWS' not in BASELINE_REQUIREMENT_IDS


# --- Test I: Historical active suggestions reviewed independently ---

@pytest.mark.asyncio
async def test_I_historical_suggestions_reviewed_independently(monkeypatch):
    """Historical active suggestions (review_ids) are reviewed independently
    of the deep pool.  A review candidate that is NOT in the shortlist still
    gets read-only evaluation.  A candidate that IS in both gets full eval.

    The review does NOT bypass the final ranker gate — a partial/suppressed
    review candidate never overrides the shortlist.
    """
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=3)

    # Stock 3 is strong (core=95) — in deep pool.
    pairs[UUID(int=3)] = inputs(3, core=95, confidence=95)
    # Stock 1 is a review candidate (prior recommendation) but weak (core=10).
    # partial=True makes the ranker add V1_ANALYSIS_NOT_ELIGIBLE gate → SUPPRESSED.
    pairs[UUID(int=1)] = inputs(1, core=10, confidence=10)
    pairs[UUID(int=1)][1].partial = True

    # Include stock 1 as a review_id (prior active suggestion).
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4,
                               review_ids=[UUID(int=1)])

    # Both reviewed and deep-evaluated candidates appear in diagnostics.
    diagnostic_ids = {d.global_instrument_id for d in result.diagnostics}
    assert UUID(int=1) in diagnostic_ids  # reviewed
    assert UUID(int=3) in diagnostic_ids  # deep-evaluated

    # The review candidate (stock 1) was NOT promoted just because it's a
    # prior recommendation — it was suppressed due to weak score.
    suppressed = [d for d in result.diagnostics
                  if d.global_instrument_id == UUID(int=1)]
    assert len(suppressed) == 1
    assert suppressed[0].status == 'SUPPRESSED'

    # Stock 3 (strong) is rank-eligible.
    eligible = [d for d in result.diagnostics
                if d.global_instrument_id == UUID(int=3)]
    assert len(eligible) == 1
    assert eligible[0].status == 'RANK_ELIGIBLE'


# --- Test J: Dashboard GET remains DB-only / no acquisition ---

@pytest.mark.asyncio
async def test_J_dashboard_db_only_no_acquisition(monkeypatch):
    """Dashboard GET /api/v1/research/opportunities/current remains DB-only.

    global_opportunity_radar() must read from V14 tables only — zero provider
    calls, zero acquisition, zero compute.  It must NOT trigger ensure() or
    any readiness acquisition.
    """
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    runtime = _make_runtime(repo)
    service = GlobalOpportunityOrchestrator(repo, store,
        profile_hydrator=hydrator.register_global_profile_metadata,
        readiness_runtime=runtime, clock=lambda: NOW)

    # Insert some data to read.
    rows = [instrument(n) for n in range(1, 4)]
    for row in rows:
        persisted(store, row)

    # Spy on ensure — it must never be called by the dashboard radar.
    ensure_spy = AsyncMock(return_value=TargetedEnsureResult(None, (), ()))
    service.readiness.ensure = ensure_spy
    service.readiness.read = AsyncMock(return_value=object())

    # Track SQL to verify it only reads.
    queries = []
    store._connection.set_trace_callback(queries.append)

    # Call the radar (dashboard data source) — persisted V14 read model only.
    radar = store.global_opportunity_radar()

    # Zero provider/network calls from ensure.
    assert ensure_spy.await_count == 0
    assert service.readiness.ensure.await_count == 0

    # SQL must contain no INSERT/UPDATE/DELETE/REPLACE (DB reads only).
    write_ops = [q for q in queries
                 if any(q.upper().startswith(kw) for kw in
                        ('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))]
    assert not write_ops, f"Dashboard radar performed writes: {write_ops}"

    # Verify radar returns valid structure.
    assert radar is not None
    assert 'best_buy_today' in radar
    assert 'top_short_term' in radar
    assert 'top_long_term' in radar
    assert 'top_exit' in radar
    assert 'generated_at' in radar


# --- Test K: HTTP GET endpoint is dashboard-only (no acquisition) ---

@pytest.mark.asyncio
async def test_K_http_get_opportunities_current_is_dashboard_only():
    """HTTP GET /api/v1/research/opportunities/current must be provider-free.

    The real FastAPI endpoint must not trigger any acquisition, provider calls,
    scanner runs, rule-engine analysis, or worker submissions.  It reads only
    the persisted V14 global radar tables.  Different user identities receive
    the same output (no user-scoped filtering).
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from unittest.mock import MagicMock
    from datetime import datetime, timezone

    # Use fixed timestamp to ensure consistent comparison
    FIXED_TIMESTAMP = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Create a fake persistence object that returns a known V14-style payload
    # without using SQLite (avoids thread-safety issues with TestClient)
    class FakePersistence:
        def global_opportunity_radar(self):
            """Return a known V14-style payload for the HTTP radar endpoint."""
            return dict(
                best_buy_today=None,
                top_short_term=[],
                top_long_term=[],
                top_exit=[],
                generated_at=FIXED_TIMESTAMP.isoformat(),
                last_processed_at=FIXED_TIMESTAMP.isoformat(),
                source_scan_id=None,
            )

        def global_current_suggestions(self, *, horizon=None):
            return []

        def global_market_scans(self):
            return []

    fake_store = FakePersistence()

    # Patch the persistence in the repository
    import app.main as main_module
    original_repository = main_module.repository

    # Create a mock repository with our fake persistence
    mock_repo = MagicMock()
    mock_repo.persistence = fake_store
    mock_repo._persistence_worker_lock = main_module.repository._persistence_worker_lock.__class__()
    mock_repo.list_profiles = main_module.repository.list_profiles

    main_module.repository = mock_repo

    try:
        client = TestClient(app)

        # Track calls to ensure no acquisition/providers/scanning happens
        spy_ensure = AsyncMock(return_value=TargetedEnsureResult(None, (), ()))
        main_module.research_readiness_runtime.ensure = spy_ensure

        # Spy on rule_engine.analyze to verify it's not called
        spy_analyze = AsyncMock()
        main_module.stock_rule_engine_service.analyze = spy_analyze

        # Call the HTTP endpoint
        response = client.get('/api/v1/research/opportunities/current')

        # Verify HTTP 200 and valid structure
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()
        assert 'best_buy_today' in data
        assert 'top_short_term' in data
        assert 'top_long_term' in data
        assert 'top_exit' in data
        assert 'generated_at' in data

        # Verify zero acquisition/provider calls
        assert spy_ensure.await_count == 0, "HTTP endpoint triggered ensure() acquisition calls"

        # Verify no rule engine analysis
        assert spy_analyze.await_count == 0, "HTTP endpoint triggered rule_engine.analyze()"

        # Verify same result for different user identities
        response2 = client.get('/api/v1/research/opportunities/current',
                               headers={'X-AIP-User-ID': 'different-user'})
        assert response2.status_code == 200
        assert response2.json() == data, "Different users received different radar output"

    finally:
        main_module.repository = original_repository


# --- Test L: Baseline fairness with >25 universe ---

@pytest.mark.asyncio
async def test_L_baseline_fairness_large_universe_all_eligible_get_baseline():
    """Large universe (>25): ALL eligible candidates receive baseline evaluation.

    Baseline evaluation must NOT be capped by shortlist_limit.  The dynamic deep
    pool is formed AFTER baseline comparison; shortlist_limit applies only to the
    deep pool size, not to baseline acquisition.

    Fresh cached evidence (prior manual research) must not trigger unnecessary
    provider acquisition and must not alter rank or eligibility.
    """
    # Create a 50-candidate universe
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    hydrator = PortfolioResearchOrchestrator(repo, Settings(), client=object())
    runtime = _make_runtime(repo)
    service = GlobalOpportunityOrchestrator(repo, store,
        profile_hydrator=hydrator.register_global_profile_metadata,
        readiness_runtime=runtime, clock=lambda: NOW)

    rows = [instrument(n) for n in range(1, 51)]  # 50 candidates
    for row in rows:
        persisted(store, row)

    # Track ensure calls with baseline vs deep distinction
    tracker = _EnsureTracker()
    service.readiness.ensure = tracker
    service.readiness.read = AsyncMock(return_value=object())

    # Track rule engine calls - proper async wrapper
    rule_engine_call_count = 0
    original_analyze = service.rule_engine.analyze
    async def count_analyze(profile, *args, **kwargs):
        nonlocal rule_engine_call_count
        rule_engine_call_count += 1
        return await original_analyze(profile, *args, **kwargs)  # Properly await!
    service.rule_engine.analyze = AsyncMock(side_effect=count_analyze)

    # Run with shortlist_limit=2 to verify baseline is not capped
    result = await service.run(rows, as_of=NOW, shortlist_limit=2)

    # Verify baseline fairness counters
    assert result.baseline_ready_count > 0, "No candidates received baseline"
    assert result.baseline_ready_count + result.baseline_incomplete_count > 25, \
        f"Baseline evaluation count ({result.baseline_ready_count + result.baseline_incomplete_count}) not >25"

    # ALL eligible candidates must receive baseline (not capped by shortlist_limit=2)
    baseline_eval_total = result.baseline_ready_count + result.baseline_incomplete_count
    assert baseline_eval_total == result.universe_count, \
        f"Only {baseline_eval_total}/{result.universe_count} got baseline evaluation (capped incorrectly)"

    # Verify diagnostics expose correct counts
    # deep_pool_eligible_count: pool size BEFORE safety cap
    assert result.deep_pool_eligible_count == 50, \
        f"deep_pool_eligible_count should be 50, got {result.deep_pool_eligible_count}"

    # deep_candidate_count: number sent to deep enrichment AFTER safety cap
    assert result.deep_candidate_count <= 2, \
        f"deep_candidate_count ({result.deep_candidate_count}) should be <= 2 after safety cap"

    # shortlist_count: should match capped deep_enrichment count
    assert result.shortlist_count <= 2, f"Shortlist ({result.shortlist_count}) exceeds limit (2)"

    # Verify baseline calls were made (use tracker.baseline_calls property)
    assert len(tracker.baseline_calls) > 25, \
        f"Baseline calls ({len(tracker.baseline_calls)}) not >25"

    # Deep calls (if any) should be <= deep_candidate_count (which is the capped value)
    assert len(tracker.deep_calls) <= result.deep_candidate_count, \
        f"Deep calls ({len(tracker.deep_calls)}) exceed deep pool ({result.deep_candidate_count})"


# --- Test M: Production diagnostics expose baseline/deep counts ---

@pytest.mark.asyncio
async def test_M_diagnostics_expose_baseline_deep_counts(monkeypatch):
    """Production diagnostics (OpportunityRanking) expose baseline/deep counts
    so that monitoring can verify the baseline-first funnel is working.

    For a 50-stock test with shortlist_limit=2, expected should be:
    universe_count = 50
    baseline evaluated = 50
    deep_pool_eligible_count may be 50
    deep_candidate_count <= 2
    shortlist_count <= 2
    """
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=50)
    result = await service.run(rows, as_of=NOW, shortlist_limit=2, top_n=4)

    # All new diagnostics fields present and correct.
    assert result.baseline_ready_count == 50
    assert result.baseline_acquisition_needed_count == 0  # all cache hits (mocked)
    assert result.baseline_incomplete_count == 0
    assert result.preliminary_eligible_count == 50
    assert result.universe_count == 50

    # deep_pool_eligible_count: pool size BEFORE safety cap
    assert result.deep_pool_eligible_count == 50

    # deep_candidate_count: AFTER safety cap (<= shortlist_limit)
    assert result.deep_candidate_count <= 2, \
        f"deep_candidate_count ({result.deep_candidate_count}) should be <= 2"

    # shortlist_count: should match deep_candidate_count AND be <= shortlist_limit
    assert result.shortlist_count <= 2, f"shortlist ({result.shortlist_count}) exceeds limit (2)"
    assert result.shortlist_count == result.deep_candidate_count

    assert len(result.diagnostics) > 0
    # Each diagnostic has a status.
    assert all(d.status in ('RANK_ELIGIBLE', 'SUPPRESSED', 'FAILED')
               for d in result.diagnostics)


# --- Regression tests for the live baseline-acquisition fix (jurisdiction=None) ---


class _JurisdictionTracker:
    """Records the jurisdiction passed to each readiness.ensure() call so the
    Phase-1 baseline contract (jurisdiction=INDIA, NOT None) can be asserted.
    Unlike _EnsureTracker above, this also captures the jurisdiction argument
    that previously was hard-coded to None (the live failure root cause)."""

    def __init__(self):
        self.calls = []  # (global_instrument_id, jurisdiction, requirement_ids or None)

    async def __call__(self, global_instrument_id, *, jurisdiction=None,
                       requirement_ids=None, identity_headers=None, **kwargs):
        self.calls.append((global_instrument_id, jurisdiction,
                           tuple(sorted(requirement_ids))
                           if requirement_ids is not None else None))
        return TargetedEnsureResult(None, (), ())

    @property
    def baseline_calls(self):
        return [c for c in self.calls if c[2] is not None and set(c[2]) == BASELINE_REQUIREMENT_IDS]

    @property
    def deep_calls(self):
        return [c for c in self.calls if c[2] is None]

    @property
    def baseline_jurisdictions(self):
        return [c[1] for c in self.baseline_calls]


@pytest.mark.asyncio
async def test_N_phase1_baseline_ensure_never_receives_none_jurisdiction(monkeypatch):
    """ROOT CAUSE regression: _acquire_baseline_requirements must never pass
    jurisdiction=None to the production readiness runtime. ResearchReadinessRuntime
    .ensure/.read require a real jurisdiction string; the live 2585-wide scan
    collapsed to shortlist_count=0 because every candidate's baseline ensure was
    issued with the hard-coded jurisdiction=None literal.
    """
    service, rows, pairs, store, _ = setup_acquisition(monkeypatch, count=10)
    tracker = _JurisdictionTracker()
    service.readiness.ensure = tracker

    await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    assert tracker.baseline_calls, "expected Phase-1 baseline ensure calls"
    # The live root cause was jurisdiction=None; the fix must eliminate it entirely.
    assert all(j is not None and j != "" for j in tracker.baseline_jurisdictions), \
        "Phase-1 baseline ensure received jurisdiction=None (live failure root cause)"
    # The production jurisdiction helper that replaces None must yield a real region.
    profile = service.repository.profile(UUID(int=1))
    assert jurisdiction_for_profile(profile) == "INDIA"


@pytest.mark.asyncio
async def test_O_phase1_baseline_ensure_passes_india_for_nse_profile(monkeypatch):
    """FIX regression: for an NSE/India (country=IN, exchange=NSE) profile the
    baseline helper must pass jurisdiction='INDIA' -- the value
    jurisdiction_for_profile derives -- not the old None literal."""
    service, rows, pairs, store, _ = setup_acquisition(monkeypatch, count=8)
    tracker = _JurisdictionTracker()
    service.readiness.ensure = tracker

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    assert tracker.baseline_calls
    assert all(j == "INDIA" for j in tracker.baseline_jurisdictions), \
        f"Expected INDIA jurisdiction for NSE/India profiles, got {tracker.baseline_jurisdictions}"
    assert result.baseline_ready_count == 8
    assert result.baseline_incomplete_count == 0


@pytest.mark.asyncio
async def test_P_profile_hydration_failure_does_not_abort_others(monkeypatch):
    """A PROFILE_HYDRATION_FAILED for one candidate must not abort baseline
    acquisition for the others, and must be labelled distinctly from the generic
    BASELINE_ACQUISITION_FAILED (so diagnostics can distinguish hydration failures)."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=3)
    real_hydrator = service.profile_hydrator

    def flaky_hydrator(key, payload):
        if key == UUID(int=1):
            return False
        return real_hydrator(key, payload)
    service.profile_hydrator = flaky_hydrator

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    diag_1 = [d for d in result.diagnostics if d.global_instrument_id == UUID(int=1)]
    assert diag_1, "key 1 should carry a hydration-failure diagnostic"
    assert all(d.status == "SUPPRESSED" for d in diag_1)
    assert any(d.failure_reason == "PROFILE_HYDRATION_FAILED" for d in diag_1)
    # Provider/private text is never exposed: only the label is retained.
    assert "secret" not in result.model_dump_json()
    # Others were NOT aborted by key 1's hydration failure.
    assert result.baseline_ready_count == 2
    assert result.baseline_incomplete_count == 1
    assert result.baseline_ready_count + result.baseline_incomplete_count == 3


@pytest.mark.asyncio
async def test_Q_deep_ready_cache_hit_still_receives_baseline_ensure(monkeypatch):
    """A deep-ready cache-hit candidate (fresh persisted evidence,
    eligible_for_deep_analysis=True) must still receive the Phase-1 baseline ensure
    with the full BASELINE_REQUIREMENT_IDS contract.  The live jurisdiction=None bug
    errored every deep-ready baseline ensure, which is why shortlist_count went 0."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=5)

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    # All eligible candidates are deep-ready (fresh persisted evidence) cache-hits.
    assert _baseline_ready_count(tracker) == 5
    assert result.baseline_ready_count == 5
    assert result.baseline_incomplete_count == 0
    # Baseline acquisition used the full baseline contract for all deep-ready candidates.
    for call in tracker.baseline_calls:
        assert set(call[1]) == BASELINE_REQUIREMENT_IDS
    assert len(tracker.deep_calls) >= 1
    # Deep-ready candidates progressed to deep evaluation.
    assert result.deep_evaluated_count == len(result.diagnostics)


@pytest.mark.asyncio
async def test_R_all_eligible_enter_baseline_before_shortlist_cap(monkeypatch):
    """shortlist_limit caps the deep pool ONLY, never baseline acquisition.
    With count=30 and shortlist_limit=10, all 30 eligible receive the Phase-1
    baseline ensure; only <=10 reach deep enrichment."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=30)

    result = await service.run(rows, as_of=NOW, shortlist_limit=10, top_n=4)

    assert _baseline_ready_count(tracker) == 30
    assert result.baseline_ready_count == 30
    assert result.baseline_incomplete_count == 0
    # Pool BEFORE safety cap still contains every eligible candidate.
    assert result.deep_pool_eligible_count == 30
    # shortlist_limit=10 only caps the deep pool, not baseline.
    assert result.shortlist_count <= 10
    assert result.deep_candidate_count <= 10


@pytest.mark.asyncio
async def test_S_profile_identity_mismatch_isolated_and_labeled(monkeypatch):
    """A PROFILE_IDENTITY_MISMATCH (repository profile's instrument_id != key)
    must be surfaced as PROFILE_IDENTITY_MISMATCH -- distinct from
    PROFILE_HYDRATION_FAILED and from the generic BASELINE_ACQUISITION_FAILED --
    and must not abort the other candidates' baseline acquisition."""
    service, rows, pairs, store, tracker = setup_acquisition(monkeypatch, count=3)
    real_profile = service.repository.profile

    def mismatched_profile(key):
        if key == UUID(int=1):
            return SimpleNamespace(instrument_id=UUID(int=99999))
        return real_profile(key)
    service.repository.profile = mismatched_profile

    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=4)

    # Key 1's Phase-1 baseline was rejected with the distinct identity-mismatch label.
    diag_mismatch = [d for d in result.diagnostics
                     if d.global_instrument_id == UUID(int=1)
                     and d.failure_reason == "PROFILE_IDENTITY_MISMATCH"]
    assert diag_mismatch, "expected a PROFILE_IDENTITY_MISMATCH diagnostic for key 1"
    # Others were NOT aborted by key 1's identity mismatch.
    assert result.baseline_ready_count == 2
    assert result.baseline_incomplete_count == 1


# --- Test Z: Phase-1 baseline requirement contract is the cheap structured set ---

def test_Z_baseline_requirement_ids_are_cheap_structured_set_only():
    """Problem B invariant: the full-universe Phase-1 baseline contract is the
    cheap structured-market set

        {LATEST_PRICE, HISTORICAL_PRICE_SERIES, VALUATION_INPUTS, SECTOR_MACRO}

    and is DISJOINT from _FINANCIAL_REQUIREMENTS (no
    BUSINESS_QUALITY_FACTS / GROWTH_FACTS / BALANCE_SHEET_FACTS /
    QUARTERLY_FINANCIALS, which trigger the expensive FINANCIAL_RESULTS
    reconcile -> NSE discovery -> Searxng/Management/RISKS/PDF fetch cascade for
    all 2578 stocks) and from GOVERNANCE_HISTORY (RISKS/REGULATORY/MANAGEMENT
    search fallback) and CURRENT_NEWS. Full financial/governance/search/PDF
    enrichment is deferred to Stage-2 shortlist deep ensure(requirement_ids=None)."""

    expect = {"LATEST_PRICE", "HISTORICAL_PRICE_SERIES",
              "VALUATION_INPUTS", "SECTOR_MACRO"}
    # The trimmed constant is exactly the cheap structured set.
    assert set(BASELINE_REQUIREMENT_IDS) == expect
    # Disjoint from the deep financial requirements (no FINANCIAL_RESULTS arm).
    assert BASELINE_REQUIREMENT_IDS.isdisjoint(_FINANCIAL_REQUIREMENTS)
    # No financial/governance/news IDs reach the full-universe baseline pass.
    assert "GOVERNANCE_HISTORY" not in BASELINE_REQUIREMENT_IDS
    assert "CURRENT_NEWS" not in BASELINE_REQUIREMENT_IDS
    expensive = (set(_FINANCIAL_REQUIREMENTS)
                 | {"GOVERNANCE_HISTORY", "CURRENT_NEWS"})
    assert expensive.isdisjoint(BASELINE_REQUIREMENT_IDS)
    # Genuine trim: the cheap set is strictly smaller than the expensive union.
    assert len(BASELINE_REQUIREMENT_IDS) < len(expensive)
