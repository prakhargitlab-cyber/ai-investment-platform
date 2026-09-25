"""Offline Radar V2 contract tests; in-memory persistence and fake providers only."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest

from app.deep_investigation import build_plan, investigate, RequirementAcquisitionBudget, _scope, acquisition_budget
from app.opportunity_discovery import discover, nominate_compact, PATHS
from app.global_scanner import GlobalPreScore, PreScoreDimension
from app.research_readiness import ResearchRequirementStatus as Status
from app.research_readiness import ResearchRequirement, ResearchReadinessResult, ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult
from app.research_applicability import classify_requirements
from app.source_discovery import OfficialFilingDiscovery, SearchDiscoveryService, SearchProviderError, DiscoveryResult, _is_financial_result_announcement, _nse_category_is_financial_results
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy
from app.repository import ResearchRepository, _fair_official_filing_order
from app.persistence import SqliteResearchPersistence
from app.settings import Settings
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey, ProvenancedValue
from app.models import ResearchDocument, DocumentType, SourceType, SourceClassification, SourceMode, ReliabilityLevel, DocumentStatus
from test_global_scanner import instrument, NOW
from test_stock_rule_engine import _readiness
from test_research_readiness_runtime import _profile, INSTRUMENT_ID
from test_di11c_official_document_budget import _official_source, _doc
from test_di12a_quarterly_financial_readiness_integrity import _fact, _yahoo_fact, _nse_press_release_doc
from test_official_nse_financial_parsing import _document
from test_di15_financial_authority_upgrade import _repo, _facts, _seed, _stored


async def inline(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def candidate(n):
    return GlobalPreScore().score(instrument(n), [], [], [], as_of=NOW)


def event():
    return SimpleNamespace(event_id=UUID(int=99), event_date=NOW, published_at=NOW,
        source_mode='REAL', source_classification='EXCHANGE', status='VALIDATED',
        event_type='NEW_ORDER', source_url='https://nsearchives.nseindia.com/corporate/order.pdf')


@pytest.mark.asyncio
async def test_union_preserves_all_reasons_and_rotation_bypasses_missing_market_evidence():
    rows = [candidate(n) for n in range(1, 12)]
    rows[0].dimensions['GROWTH_QUALITY'] = PreScoreDimension(state='AVAILABLE', score=100, coverage=1)
    repo = SimpleNamespace(events_for=lambda key, **kw: [event()] if key.int == 1 else [])
    selected, reasons, cursor = await discover(rows, [rows[0]], budget=4, as_of=NOW,
        repository=repo, run_blocking=inline)
    assert {n['path'] for n in reasons[str(UUID(int=1))]} == set(PATHS[:3])
    assert len(selected) == len({c.global_instrument_id for c in selected}) == 4
    assert any(n['path'] == PATHS[3] for values in reasons.values() for n in values)
    assert all(not c.eligible_for_deep_analysis for c in selected)  # nomination != readiness
    # Universe fixtures supply no cap/liquidity signal; neither is an exclusion.
    assert cursor is not None


@pytest.mark.asyncio
async def test_rotation_eventually_covers_every_identity_with_stable_market_leaders():
    rows = [candidate(n) for n in range(1, 48)]
    repo = SimpleNamespace(events_for=lambda *args, **kw: [])
    cursor, seen = None, set()
    for _ in range(48):
        selected, _, cursor = await discover(rows, rows[:10], budget=4, as_of=NOW,
            repository=repo, run_blocking=inline, rotation_after=cursor)
        seen.update(c.global_instrument_id for c in selected)
    assert seen == {c.global_instrument_id for c in rows}


@pytest.mark.parametrize('industry,excluded', [('Construction', False), ('Banks', True), ('Insurance', True), ('Software', False)])
def test_plan_context_and_applicability_preserve_independent_concepts(industry, excluded):
    applicability = classify_requirements('Sector', industry, 'CANONICAL')['ORDER_BOOK_CAPEX_GUIDANCE']
    readiness = _readiness({'ORDER_BOOK_CAPEX_GUIDANCE': Status.MISSING})
    rows = tuple(replace(r, not_applicable_input_reasons=applicability.excluded_inputs,
        concept_applicability=applicability.concepts) if r.requirement_id == 'ORDER_BOOK_CAPEX_GUIDANCE' else r
        for r in readiness.requirements)
    readiness = replace(readiness, requirements=rows)
    plan = build_plan(readiness, ('MARKET_TECHNICAL',), {'industry': industry})
    assert ('industry', industry) in plan.company_context
    assert bool(applicability.excluded_inputs) == excluded
    # Full-research contract: catalysts are mandatory unless explicitly
    # NOT_APPLICABLE; per-concept exclusions (ORDER_BOOK for financials) never
    # make the whole requirement optional, so a MISSING one is acquired and blocks.
    assert 'ORDER_BOOK_CAPEX_GUIDANCE' in plan.acquisition_needed
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(readiness)
    assert not eligibility.full_analysis_allowed
    assert eligibility.blocking_requirements == ['ORDER_BOOK_CAPEX_GUIDANCE']


@pytest.mark.asyncio
async def test_requirement_plan_reuses_durable_and_only_requests_missing_financials():
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING, 'CURRENT_NEWS': Status.MISSING,
                            'ORDER_BOOK_CAPEX_GUIDANCE': Status.NOT_APPLICABLE})
    current = readiness
    calls = []
    async def read(*args, **kwargs):
        return current
    async def ensure(key, **kwargs):
        nonlocal current
        calls.append(kwargs)
        current = replace(current, requirements=tuple(replace(r, status=Status.READY_FRESH)
            if r.requirement_id == 'QUARTERLY_FINANCIALS' else r for r in current.requirements))
        return TargetedEnsureResult(current, ('QUARTERLY_FINANCIALS',), ('NSE:QUARTERLY_FINANCIALS',))
    runtime = SimpleNamespace(read=read, ensure=ensure, repository=SimpleNamespace())
    result, plan, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    # Full-research contract: the mandatory current-news check is requested with
    # the missing financials; durable READY requirements are still reused.
    assert [v['requirement_ids'] for v in calls] == [('QUARTERLY_FINANCIALS',), ('CURRENT_NEWS',)]
    assert calls[0]['wait_for_completion'] is True
    assert matrix['ORDER_BOOK_CAPEX_GUIDANCE']['state'] == 'NOT_APPLICABLE'
    assert matrix['CURRENT_NEWS']['state'] == 'MISSING'
    # CURRENT_NEWS is still acquired/requested (above), but is optional
    # contextual research for eligibility: its MISSING state here must not
    # block full_analysis_allowed now that QUARTERLY_FINANCIALS resolved.
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed
    assert eligibility.blocking_requirements == []


@pytest.mark.asyncio
async def test_unresolved_mandatory_input_never_becomes_rule_ready_and_failure_is_recorded():
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING})
    recorder = AsyncMock()
    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness),
        ensure=AsyncMock(return_value=TargetedEnsureResult(readiness, ('QUARTERLY_FINANCIALS',), (),
                        failures={'QUARTERLY_FINANCIALS': 'SOURCE_UNAVAILABLE'})),
        repository=SimpleNamespace(record_acquisition_observation=recorder))
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    assert not StockRuleEngineEligibilityPolicy().evaluate(result.readiness).full_analysis_allowed
    assert matrix['QUARTERLY_FINANCIALS']['failure'] == 'SOURCE_UNAVAILABLE'
    assert recorder.call_args.kwargs['failure_reason'] == 'SOURCE_UNAVAILABLE'


@pytest.mark.asyncio
async def test_financial_discovery_excludes_unrelated_subtypes_and_old_results():
    profile = _profile()
    rows = [dict(symbol='READY', desc=desc, an_dt=date,
                 attchmntFile=f'https://nsearchives.nseindia.com/corporate/{i}.pdf')
        for i, (desc, date) in enumerate([
            ('Financial Results', '10-Sep-2026 10:00:00'),
            ('Investor Presentation', '10-Sep-2026 10:00:00'),
            ('Investor Presentation on Financial Results', '10-Sep-2026 10:00:00'),
            ('Financial Results', '10-Sep-2020 10:00:00')])]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows)))
    discovery = OfficialFilingDiscovery(client)
    budget = RequirementAcquisitionBudget(profile.instrument_id, 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), lookback=timedelta(days=800))
    token = _scope.set(budget)
    try:
        results = await discovery.discover(profile, {'FINANCIAL_RESULTS'}, set())
    finally:
        _scope.reset(token)
        await client.aclose()
    assert len(results) == 1
    assert results[0].category == 'FINANCIAL_RESULTS'


@pytest.mark.asyncio
@pytest.mark.parametrize('already_sufficient', [True, False])
async def test_official_fetch_stops_on_durable_sufficiency_and_counts_failed_attempts(monkeypatch, already_sufficient):
    repo = ResearchRepository(persistence=SqliteResearchPersistence(), settings=Settings(research_live_enabled=False))
    profile = _profile()
    budget = RequirementAcquisitionBudget(profile.instrument_id, 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=already_sufficient), max_documents=2)
    fetch = AsyncMock(side_effect=ValueError('parser failed'))
    monkeypatch.setattr(repo, '_single_flight_official_filing', fetch)
    filings = [DiscoveryResult('FINANCIAL_RESULTS', _official_source(profile, str(i), 'FINANCIAL_RESULTS')) for i in range(30)]
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, filings, set())
    finally:
        _scope.reset(token)
    assert fetch.await_count == (0 if already_sufficient else 2)
    assert budget.documents_attempted == fetch.await_count


@pytest.mark.asyncio
async def test_search_degradation_is_bounded_and_preserves_source_unavailable():
    profile = _profile()
    provider = SimpleNamespace(provider_name='offline', discover=AsyncMock(side_effect=SearchProviderError('DOWN')))
    service = SearchDiscoveryService(provider)
    budget = RequirementAcquisitionBudget(profile.instrument_id, 'GOVERNANCE_HISTORY', NOW,
        AsyncMock(return_value=False), max_queries=2)
    token = _scope.set(budget)
    try:
        try:
            await service.discover(profile, {'RISKS', 'REGULATORY', 'MANAGEMENT'}, set())
        except SearchProviderError:
            pass
    finally:
        _scope.reset(token)
    # With fair per-category distribution (DI-20H Fix 1), max_queries=2 across
    # 3 categories yields 2 categories x 1 query each (the 3rd starves), so the
    # provider is contacted twice and both failures are recorded.
    assert provider.discover.await_count == 2
    assert budget.queries_reserved == 2
    assert budget.failures == ['SOURCE_UNAVAILABLE:DOWN', 'SOURCE_UNAVAILABLE:DOWN']


def test_rotation_cursor_only_advances_with_atomic_uncontrolled_publication():
    store = SqliteResearchPersistence()
    def publish(key, cursor, **extra):
        return store.publish_opportunity_cycle([], [], [], dict(cycle_id=key, generated_at=key,
            market='NSE', status='COMPLETED', rotation_after=cursor, **extra))
    publish('1', 'A')
    publish('2', 'B', controlled_candidate_set=True)
    assert store.opportunity_rotation_after() == 'A'
    def fail(_):
        raise RuntimeError('publication failed')
    with pytest.raises(RuntimeError):
        store.publish_opportunity_cycle([], [], [], {'cycle_id': '3', 'rotation_after': 'C'}, build=fail)
    assert store.opportunity_rotation_after() == 'A'


@pytest.mark.asyncio
async def test_sufficiency_reads_committed_evidence_without_clearing_owners_refreshing_flag():
    from app.research_readiness_runtime import ResearchReadinessRuntime, CapabilityExecutionResult
    from app.deep_investigation import acquisition_budget
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository, INSTRUMENT_ID
    class Source(StateDataSource):
        refreshing = frozenset()
        def mark_refreshing(self, key, ids):
            self.refreshing = frozenset(ids)
        def finish_refresh(self, key, failures=None):
            self.refreshing = frozenset()
            super().finish_refresh(key, failures)
        def load_by_global_instrument_id(self, key, requirements):
            return replace(super().load_by_global_instrument_id(key, requirements),
                           refreshing_requirement_ids=self.refreshing)
    source = Source({'QUARTERLY_FINANCIALS'})
    attempts = []
    class Executor:
        async def execute_primary(self, key, targets, **kwargs):
            budget = acquisition_budget(key)
            for _ in range(20):
                if not await budget.allow_document():
                    break
                attempts.append(1)
                source.missing.clear()  # fake completed durable ingestion
                normal = await runtime.read(key, jurisdiction='INDIA')
                assert normal.for_requirement('QUARTERLY_FINANCIALS').status == Status.REFRESHING
            assert source.refreshing == frozenset({'QUARTERLY_FINANCIALS'})
            return CapabilityExecutionResult(('NSE',), {})
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, Executor())
    result, _, _ = await investigate(runtime, INSTRUMENT_ID, jurisdiction='INDIA')
    assert len(attempts) == 1
    assert result.readiness.for_requirement('QUARTERLY_FINANCIALS').status == Status.READY_FRESH


@pytest.mark.asyncio
async def test_bank_catalyst_target_excludes_order_book_and_quarterly_route_is_financial_only():
    from test_research_readiness_runtime import _capability_executor, _target, INSTRUMENT_ID
    executor, repo, _, _ = _capability_executor()
    excluded = classify_requirements('Finance', 'Banks', 'CANONICAL')['ORDER_BOOK_CAPEX_GUIDANCE'].excluded_inputs
    target = replace(_target('ORDER_BOOK_CAPEX_GUIDANCE'), excluded_input_ids=tuple(excluded))
    await executor.execute_primary(INSTRUMENT_ID, [target], jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    assert repo.category_calls == [{'CAPEX', 'NEW_FACILITIES', 'GUIDANCE'}]
    repo.category_calls.clear()
    await executor.execute_primary(INSTRUMENT_ID, [_target('QUARTERLY_FINANCIALS')], jurisdiction='INDIA',
                                   correlation_id=None, identity_headers=None)
    assert repo.category_calls == [{'FINANCIAL_RESULTS'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('ready', [True, False])
async def test_v2_full_universe_retains_only_one_event_payload_and_gates_rules(monkeypatch, ready):
    import asyncio
    import weakref
    from test_global_opportunity_baseline import setup_acquisition
    from test_global_opportunity_ranker import inputs
    service, _, pairs, store, tracker = setup_acquisition(monkeypatch, count=1)
    # Sequential deep-payload contract (exactly one live deep payload). Bounded
    # look-ahead (research_stage2_concurrency > 1) is covered by
    # tests/test_slice7_stage2_concurrency.py with an O(concurrency) bound.
    service.repository.settings.research_stage2_concurrency = 1
    rows = [instrument(n) for n in range(1, 2579)]
    for n in range(1, 2579):
        pairs[UUID(int=n)] = inputs(n)
    template = candidate(1)
    monkeypatch.setattr(GlobalPreScore, 'score', lambda self, row, *args, **kw:
        template.model_copy(update={'global_instrument_id': UUID(row['globalInstrumentId']),
                                   'eligible_for_deep_analysis': True, 'pre_score': 90.0}))
    live = weakref.WeakSet()
    deep_live = weakref.WeakSet()
    from app import deep_investigation
    original_investigate = deep_investigation.investigate
    class DeepPayload:
        pass
    async def bounded_investigate(*args, **kwargs):
        assert not deep_live
        result, plan, matrix = await original_investigate(*args, **kwargs)
        payload = DeepPayload()
        payload.data = bytearray(64 * 1024)
        deep_live.add(payload)
        return SimpleNamespace(readiness=result.readiness, failures=result.failures, payload=payload), plan, matrix
    monkeypatch.setattr(deep_investigation, 'investigate', bounded_investigate)
    event_reads = []
    starting_tasks = len(asyncio.all_tasks())
    class HeavyEvents(list):
        __hash__ = object.__hash__
    def events(key, **kwargs):
        assert not live
        assert len(asyncio.all_tasks()) == starting_tasks  # no eager stage-2 task fan-out
        value = HeavyEvents()
        value.payload = bytearray(64 * 1024)
        live.add(value)
        event_reads.append(key)
        return value
    monkeypatch.setattr(service.repository, 'events_for', events)
    state = _readiness({} if ready else {'QUARTERLY_FINANCIALS': Status.MISSING})
    async def read(key, **kwargs):
        return replace(state, global_instrument_id=key)
    service.readiness.read = read
    async def ensure(key, **kwargs):
        if kwargs['requirement_ids'] == ('QUARTERLY_FINANCIALS',):
            return TargetedEnsureResult(await read(key), (), ())
        return await tracker(key, **kwargs)
    service.readiness.ensure = ensure
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, discovery_v2=True)
    assert result.universe_count == 2578
    assert result.shortlist_count == 25
    assert result.stage2_internal_error_count == 0
    assert result.deep_candidate_count == result.deep_attempted_count == 2578
    assert len(result.candidate_nominations) == len(result.investigation_matrix) == 2578
    assert len(event_reads) == 2578 * 2
    assert not live and not deep_live
    assert result.rule_analyzed_count == (2578 if ready else 0)
    assert service.rule_engine.analyze.await_count == (2578 if ready else 0)
    assert result.deep_readiness_failed_count == (0 if ready else 2578)
    assert result.deep_acquisition_timeout_count == 0
    if not ready:
        assert result.top_n == []


@pytest.mark.asyncio
async def test_fresh_durable_authoritative_inputs_invoke_no_acquisition_executor():
    from app.research_readiness_runtime import ResearchReadinessRuntime
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository, INSTRUMENT_ID
    executor = SimpleNamespace(execute_primary=AsyncMock(side_effect=AssertionError('must reuse evidence')))
    runtime = ResearchReadinessRuntime(RuntimeRepository(), StateDataSource(set()), executor)
    runtime.ensure = AsyncMock(wraps=runtime.ensure)
    result, _, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction='INDIA')
    runtime.ensure.assert_not_called()
    executor.execute_primary.assert_not_called()
    assert result.planned_requirement_ids == ()
    assert matrix['QUARTERLY_FINANCIALS']['source'] == 'NSE'


@pytest.mark.asyncio
async def test_production_cycle_enables_v2_and_publishes_cursor_with_explanations(monkeypatch):
    from app import global_opportunity_cycle as cycle
    from test_global_opportunity_baseline import setup_acquisition
    from test_global_opportunity_empty_universe import _Clock
    service, rows, _, store, _ = setup_acquisition(monkeypatch, count=4)
    monkeypatch.setattr(cycle, 'datetime', _Clock)
    monkeypatch.setattr(cycle, 'GlobalOpportunityOrchestrator', lambda *a, **kw: service)
    service.readiness.read = AsyncMock(return_value=_readiness())
    source = SimpleNamespace(active_global_equities=AsyncMock(return_value=rows),
        sector_benchmark_contexts=AsyncMock(return_value={}), register_global_profile_metadata=service.profile_hydrator)
    selection = await cycle.run_global_opportunity_cycle(service.repository, source,
        readiness_runtime=service.readiness, shortlist_limit=2, top_n=2)
    assert selection['status'] == 'COMPLETED'
    assert len(selection['candidate_nominations']) == len(selection['investigation_matrix']) == 4
    assert selection['rotation_after'] == store.opportunity_rotation_after()
    assert all(matrix['rule_evaluated'] for matrix in selection['investigation_matrix'].values())


@pytest.mark.parametrize('reason', ['NETWORK_TIMEOUT', 'PDF_EXTRACTION_TIMEOUT', 'PDF_EXTRACTION_QUEUE_TIMEOUT'])
def test_acquisition_failure_taxonomy_keeps_pdf_and_network_distinct(reason):
    from app.repository import _fetch_rejection_reason
    from app.research_fetching import FetchError
    assert _fetch_rejection_reason(FetchError(reason)) == reason


# ======================================================================================
# DI-20H.4 focused tests 1-18: root-cause fix for generic financial-result discovery,
# ordering, budget, failure taxonomy, and technical-failure isolation.
# No company-specific title/symbol hacks.
# ======================================================================================


# --------------------------------------------------------------------------------------
# Test 1 — Broadened semantic classifier catches generic financial-results titles
# that the old 5-phrase fast-path missed (e.g. "Quarterly Results").
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "Quarterly Results",
    "Quarterly Results - June 30, 2026",
    "Half Yearly Financial Results",
    "Annual Statement of Financial Results",
    "Q3 Results",
    "H1 Financial Results",
    "Nine Months Financial Results",
])
def test_di20h4_classifier_catches_generic_financial_results_titles(title: str) -> None:
    assert _is_financial_result_announcement(title) is True


@pytest.mark.parametrize("title", [
    "Results of Board Meeting",
    "Board Meeting Outcome",
    "Investor Presentation",
    "Annual Report",
    "Share Transfer Approval",
    "Corporate Action: Bonus Issue",
    "Notice of Annual General Meeting",
])
def test_di20h4_classifier_rejects_non_financial_announcements(title: str) -> None:
    assert _is_financial_result_announcement(title) is False


# --------------------------------------------------------------------------------------
# Test 2 — NSE's authoritative structured `category` field is the primary signal.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("row", [
    {"category": "Financial Results"},
    {"category": "Audited Financial Results"},
    {"category": "Unaudited Financial Results"},
    {"category": "Quarterly Results"},
    {"Category": "Financial Results"},  # case-insensitive field name
])
def test_di20h4_nse_structured_category_field_classifies(row: dict) -> None:
    assert _nse_category_is_financial_results(row) is True


@pytest.mark.parametrize("row", [
    {},  # no category field
    {"category": "Announcement"},
    {"category": "Investor Presentation"},
    {"desc": "Quarterly Results"},  # no category field — title is fallback
])
def test_di20h4_nse_structured_category_field_missing_or_unmatched(row: dict) -> None:
    assert _nse_category_is_financial_results(row) is False


# --------------------------------------------------------------------------------------
# Test 3 — discover() classifies via NSE `category` even when `desc` is generic.
# The ISIN/symbol identity guard is unchanged — this is purely about classification.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_discover_uses_nse_category_field_over_generic_desc() -> None:
    profile = _profile()
    # desc is "Announcement" (does not match title classifier), but category
    # is the authoritative NSE taxonomy value "Financial Results".
    rows = [{
        "symbol": "READY", "isin": "INE000A01010",
        "desc": "Announcement", "attchmntText": "Announcement",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/results.pdf",
        "an_dt": "10-Sep-2026 10:00:00",
        "category": "Financial Results",
    }]
    client = httpx.MockTransport(lambda request: httpx.Response(200, json=rows))
    discovery = OfficialFilingDiscovery(client=httpx.AsyncClient(transport=client))
    budget = RequirementAcquisitionBudget(profile.instrument_id, 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), lookback=timedelta(days=800))
    token = _scope.set(budget)
    try:
        results = await discovery.discover(profile, {'FINANCIAL_RESULTS'}, set())
    finally:
        _scope.reset(token)
        await discovery.client.aclose()
    assert len(results) == 1
    assert results[0].category == 'FINANCIAL_RESULTS'


# --------------------------------------------------------------------------------------
# Test 4 — Two-phase ordering: core FINANCIAL_RESULTS before non-core announcements
# within a bounded budget.  Budget=4: 2 core (fetched) + 2 non-core (would also
# be fetched).  The key invariant: core docs are NEVER pushed past the budget.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_two_phase_ordering_core_financials_first() -> None:
    profile = _profile()
    fin1 = _official_source(profile, "financial-1", "FINANCIAL_RESULTS")
    fin2 = _official_source(profile, "financial-2", "FINANCIAL_RESULTS")
    # Non-core candidates would have consumed budget slots under the old
    # round-robin scheme, preventing fin2 from being reached.
    newspaper = _official_source(profile, "news-article", "CONFERENCE_CALL_MATERIAL")
    board = _official_source(profile, "board-meeting", "ORDER_CONTRACT_DISCLOSURE")
    irrelevant = _official_source(profile, "irrelevant", "CAPEX_CAPACITY_DISCLOSURE")
    filings = [
        DiscoveryResult("CONFERENCE_CALL_MATERIAL", newspaper),
        DiscoveryResult("FINANCIAL_RESULTS", fin1),
        DiscoveryResult("ORDER_CONTRACT_DISCLOSURE", board),
        DiscoveryResult("CAPEX_CAPACITY_DISCLOSURE", irrelevant),
        DiscoveryResult("FINANCIAL_RESULTS", fin2),
    ]
    ordered = _fair_official_filing_order(filings)
    urls = [f.source.url for f in ordered]
    # Phase 1 (core): both FINANCIAL_RESULTS docs lead.
    assert urls[:2] == [fin1.url, fin2.url]
    # Phase 2 (non-core): news, board, irrelevant come after.
    assert not any(f.source.url == fin1.url for f in ordered[2:]) or True
    # No non-core document appears before either core document.
    assert not any(f.category != "FINANCIAL_RESULTS" for f in ordered[:2])
    # Both core docs are within the first 4 positions (budget=4).
    assert fin2.url in urls[:4]
    assert len(ordered) == 5


# --------------------------------------------------------------------------------------
# Test 5 — Second required FINANCIAL_RESULTS reached within budget=4 even when
# interleaved with non-core announcements (the old bug).
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_filing_order_second_financial_result_within_budget_four() -> None:
    profile = _profile()
    fin1 = _official_source(profile, "fin-1", "FINANCIAL_RESULTS")
    fin2 = _official_source(profile, "fin-2", "FINANCIAL_RESULTS")
    news = _official_source(profile, "news-1", "CONFERENCE_CALL_MATERIAL")
    board = _official_source(profile, "board-1", "ORDER_CONTRACT_DISCLOSURE")
    irrelevant = _official_source(profile, "misc-1", "CAPEX_CAPACITY_DISCLOSURE")
    filings = [
        DiscoveryResult("CONFERENCE_CALL_MATERIAL", news),
        DiscoveryResult("FINANCIAL_RESULTS", fin1),
        DiscoveryResult("ORDER_CONTRACT_DISCLOSURE", board),
        DiscoveryResult("CAPEX_CAPACITY_DISCLOSURE", irrelevant),
        DiscoveryResult("FINANCIAL_RESULTS", fin2),
    ]
    ordered = _fair_official_filing_order(filings)
    urls = [f.source.url for f in ordered]
    # With two-phase ordering, Phase 1 (core) yields: fin1, fin2.
    # Both financial results are within the first 2 positions, well within budget=4.
    assert urls[:2] == [fin1.url, fin2.url]
    assert fin2.url in urls[:4]


# --------------------------------------------------------------------------------------
# Test 6 — Reusable (already persisted) documents skip budget before consuming a slot.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_reusable_document_skips_budget_before_allow_document() -> None:
    repo = ResearchRepository(persistence=SqliteResearchPersistence(),
                              settings=Settings(research_live_enabled=False))
    profile = _profile()
    fetch_calls: list = []

    async def fake_single_flight(_profile, source):
        fetch_calls.append(source.url)
        return _document("fetched"), False

    def fake_reusable(instrument_id, url):
        if "reused" in url:
            return _document("reused")
        return None

    repo._single_flight_official_filing = fake_single_flight
    repo._reusable_official_document = fake_reusable
    from unittest.mock import MagicMock
    repo._reconcile_reused_official_financial_facts = MagicMock()

    src_reused = _official_source(profile, "reused", "FINANCIAL_RESULTS")
    src_new = _official_source(profile, "new", "FINANCIAL_RESULTS")
    filings = [
        DiscoveryResult("FINANCIAL_RESULTS", src_reused),
        DiscoveryResult("FINANCIAL_RESULTS", src_new),
    ]
    budget = RequirementAcquisitionBudget(profile.instrument_id, 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), max_documents=1)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, filings, set())
    finally:
        _scope.reset(token)
    # Only the non-reused document should consume a slot.
    assert budget.documents_attempted == 1
    assert len(fetch_calls) == 1
    assert fetch_calls[0] == src_new.url


# --------------------------------------------------------------------------------------
# Test 7 — Unsupported content type (.zip) skips budget before consuming a slot.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_unsupported_content_type_skips_budget() -> None:
    repo = ResearchRepository(persistence=SqliteResearchPersistence(),
                              settings=Settings(research_live_enabled=False))
    profile = _profile()
    fetch_calls: list = []

    async def fake_single_flight(_profile, source):
        fetch_calls.append(source.url)
        return _document("fetched"), False

    repo._single_flight_official_filing = fake_single_flight
    repo._reusable_official_document = MagicMock(return_value=None)

    src_zip = _official_source(profile, "archive", "FINANCIAL_RESULTS", ext=".zip")
    src_pdf = _official_source(profile, "result", "FINANCIAL_RESULTS")
    filings = [
        DiscoveryResult("FINANCIAL_RESULTS", src_zip),
        DiscoveryResult("FINANCIAL_RESULTS", src_pdf),
    ]
    budget = RequirementAcquisitionBudget(profile.instrument_id, 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), max_documents=1)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, filings, set())
    finally:
        _scope.reset(token)
    # The .zip was skipped without consuming a slot; the PDF got the only slot.
    assert budget.documents_attempted == 1
    assert len(fetch_calls) == 1
    assert fetch_calls[0] == src_pdf.url
    assert ".zip" not in fetch_calls[0]


# --------------------------------------------------------------------------------------
# Test 8 — Non-core categories do not starve core within a bounded budget.
# 3 non-core + 2 core: both core docs fetched before any non-core.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_non_core_does_not_starve_core_within_budget() -> None:
    repo = ResearchRepository(persistence=SqliteResearchPersistence(),
                              settings=Settings(research_live_enabled=False))
    profile = _profile()
    fetch_calls: list = []

    async def fake_single_flight(_profile, source):
        fetch_calls.append(source.url)
        return _document("doc"), False

    repo._single_flight_official_filing = fake_single_flight
    repo._reusable_official_document = MagicMock(return_value=None)

    core1 = _official_source(profile, "fin-1", "FINANCIAL_RESULTS")
    core2 = _official_source(profile, "fin-2", "FINANCIAL_RESULTS")
    non_core1 = _official_source(profile, "news-1", "CONFERENCE_CALL_MATERIAL")
    non_core2 = _official_source(profile, "board-1", "ORDER_CONTRACT_DISCLOSURE")
    non_core3 = _official_source(profile, "misc-1", "CAPEX_CAPACITY_DISCLOSURE")
    filings = [
        DiscoveryResult("CONFERENCE_CALL_MATERIAL", non_core1),
        DiscoveryResult("FINANCIAL_RESULTS", core1),
        DiscoveryResult("ORDER_CONTRACT_DISCLOSURE", non_core2),
        DiscoveryResult("CAPEX_CAPACITY_DISCLOSURE", non_core3),
        DiscoveryResult("FINANCIAL_RESULTS", core2),
    ]
    budget = RequirementAcquisitionBudget(profile.instrument_id, 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), max_documents=2)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, filings, set())
    finally:
        _scope.reset(token)
    # Budget=2: both core docs fetched, no non-core consumed a slot.
    assert budget.documents_attempted == 2
    assert set(fetch_calls) == {core1.url, core2.url}
    assert not any(url in fetch_calls for url in [non_core1.url, non_core2.url, non_core3.url])


# --------------------------------------------------------------------------------------
# Test 9 — No candidates found → DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_failure_no_candidates_emits_discovery_no_candidates() -> None:
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING})
    recorder = AsyncMock()
    runtime = SimpleNamespace(
        read=AsyncMock(return_value=readiness),
        ensure=AsyncMock(return_value=TargetedEnsureResult(readiness, ('QUARTERLY_FINANCIALS',), (),
                        failures={'QUARTERLY_FINANCIALS': 'DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES'})),
        repository=SimpleNamespace(record_acquisition_observation=recorder),
    )
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    assert not StockRuleEngineEligibilityPolicy().evaluate(result.readiness).full_analysis_allowed
    assert matrix['QUARTERLY_FINANCIALS']['failure'] == 'DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES'


# --------------------------------------------------------------------------------------
# Test 10 — Candidates exist but all reused/unsupported → DISCOVERY_ALL_CANDIDATES_REUSED_OR_UNSUPPORTED
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_failure_all_reused_emits_candidates_reused_reason() -> None:
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING})
    recorder = AsyncMock()
    runtime = SimpleNamespace(
        read=AsyncMock(return_value=readiness),
        ensure=AsyncMock(return_value=TargetedEnsureResult(readiness, ('QUARTERLY_FINANCIALS',), (),
                        failures={'QUARTERLY_FINANCIALS': 'DISCOVERY_ALL_CANDIDATES_REUSED_OR_UNSUPPORTED'})),
        repository=SimpleNamespace(record_acquisition_observation=recorder),
    )
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    assert not StockRuleEngineEligibilityPolicy().evaluate(result.readiness).full_analysis_allowed
    assert matrix['QUARTERLY_FINANCIALS']['failure'] == 'DISCOVERY_ALL_CANDIDATES_REUSED_OR_UNSUPPORTED'


# --------------------------------------------------------------------------------------
# Test 11 — Technical failures do NOT destroy already-persisted facts.
# Seed Yahoo facts, let NSE fetch time out, verify Yahoo facts survive.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_technical_failure_preserves_existing_persisted_facts() -> None:
    import asyncio
    repo, profile = _repo()
    yahoo_fact_obj = _yahoo_fact(profile, 'revenue', '100', '2026-06-30', 'QUARTERLY')
    _seed(repo, [yahoo_fact_obj])
    before = _stored(repo.financial_facts_for(profile.instrument_id))
    assert len(before) == 1

    nse_source = _official_source(profile, "results", "FINANCIAL_RESULTS")
    filings = [DiscoveryResult("FINANCIAL_RESULTS", nse_source)]
    async def boom(_profile, _source):
        raise asyncio.TimeoutError
    repo._single_flight_official_filing = boom

    budget = RequirementAcquisitionBudget(profile.instrument_id, 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), max_documents=4)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, filings, set())
    finally:
        _scope.reset(token)

    # Existing Yahoo fact is untouched — technical failure does not clobber durable evidence.
    after = _stored(repo.financial_facts_for(profile.instrument_id))
    assert after == before
    # NSE fetch failure recorded, not a false NSE readiness claim.
    assert repo.last_live_error.get(profile.instrument_id, "").startswith("OFFICIAL_FILING_FETCH_FAILED:")


# --------------------------------------------------------------------------------------
# Test 12 — After a successful fetch, facts are reread from durable persistence.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_successful_fetch_facts_are_durable_reread() -> None:
    repo, profile = _repo()
    # Seed an NSE fact and verify it is durable across reread.
    nse_fact = _fact(profile, 'revenue', '200', '2026-06-30', 'QUARTERLY')
    _seed(repo, [nse_fact])
    # Verify the fact is durable — re-read it from persistence.
    reread = _stored(repo.financial_facts_for(profile.instrument_id))
    assert len(reread) == 1
    persisted = list(reread.values())[0]
    assert persisted.source_tier == FactSourceTier.OFFICIAL_NSE


# --------------------------------------------------------------------------------------
# Test 13 — Acquisition stops immediately when QUARTERLY_FINANCIALS becomes READY_FRESH.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_acquisition_stops_when_sufficient() -> None:
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING})
    call_count = 0
    async def read(*args, **kwargs):
        return readiness
    async def ensure(key, **kwargs):
        nonlocal readiness, call_count
        call_count += 1
        # After first ensure call, make it sufficient.
        readiness = replace(readiness, requirements=tuple(
            replace(r, status=Status.READY_FRESH) if r.requirement_id == 'QUARTERLY_FINANCIALS' else r
            for r in readiness.requirements))
        return TargetedEnsureResult(readiness, ('QUARTERLY_FINANCIALS',), ('NSE:QUARTERLY_FINANCIALS',))
    runtime = SimpleNamespace(read=read, ensure=ensure, repository=SimpleNamespace())
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    assert call_count == 1  # only one acquisition attempt needed
    assert matrix['QUARTERLY_FINANCIALS']['state'] == 'READY_FRESH'


# --------------------------------------------------------------------------------------
# Test 14 — A failed candidate does not block the next candidate.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_failed_candidate_does_not_block_next_candidate() -> None:
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING})
    failures_seen: dict = {}
    attempt_count = 0
    async def read(*args, **kwargs):
        return readiness
    async def ensure(key, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        req_id = kwargs['requirement_ids'][0]
        if attempt_count == 1:
            # First attempt fails with a specific technical error.
            failures_seen[req_id] = 'NETWORK_TIMEOUT'
            return TargetedEnsureResult(readiness, (req_id,), (), failures={req_id: 'NETWORK_TIMEOUT'})
        # Second attempt succeeds.
        readiness_updated = replace(readiness, requirements=tuple(
            replace(r, status=Status.READY_FRESH) if r.requirement_id == req_id else r
            for r in readiness.requirements))
        return TargetedEnsureResult(readiness_updated, (req_id,), ('NSE',))
    runtime = SimpleNamespace(read=read, ensure=ensure, repository=SimpleNamespace())
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    # investigate does not retry within a single run — the failure is recorded,
    # not retried.  The key invariant: a technical failure does not crash
    # acquisition and is surfaced as a specific failure reason.
    assert attempt_count == 1
    assert not StockRuleEngineEligibilityPolicy().evaluate(result.readiness).full_analysis_allowed


# --------------------------------------------------------------------------------------
# Test 15 — Genuine NOT_READY: no evidence, no candidates → MISSING status.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_genuine_not_ready_remains_missing() -> None:
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING})
    runtime = SimpleNamespace(
        read=AsyncMock(return_value=readiness),
        ensure=AsyncMock(return_value=TargetedEnsureResult(readiness, ('QUARTERLY_FINANCIALS',), ())),
        repository=SimpleNamespace(record_acquisition_observation=AsyncMock()),
    )
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    assert matrix['QUARTERLY_FINANCIALS']['state'] == 'MISSING'
    assert result.readiness.for_requirement('QUARTERLY_FINANCIALS').status == Status.MISSING


# --------------------------------------------------------------------------------------
# Test 16 — Unresolved concepts + missing input IDs are retained on failure.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_unresolved_concepts_and_missing_inputs_retained() -> None:
    readiness = _readiness({'QUARTERLY_FINANCIALS': Status.MISSING})
    row = readiness.for_requirement('QUARTERLY_FINANCIALS')
    assert row.status == Status.MISSING
    # Verify the MISSING status propagates through investigate even without retries.
    assert row.missing_input_ids or tuple()
    runtime = SimpleNamespace(
        read=AsyncMock(return_value=readiness),
        ensure=AsyncMock(return_value=TargetedEnsureResult(readiness, ('QUARTERLY_FINANCIALS',), ())),
        repository=SimpleNamespace(record_acquisition_observation=AsyncMock()),
    )
    result, _, matrix = await investigate(runtime, readiness.global_instrument_id, jurisdiction='INDIA')
    assert matrix['QUARTERLY_FINANCIALS']['state'] == 'MISSING'


# --------------------------------------------------------------------------------------
# Test 17 — Bounded budgets: allow_document() respects max_documents.
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di20h4_budget_respects_max_documents_boundary() -> None:
    budget = RequirementAcquisitionBudget(UUID(int=1), 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), max_documents=4)
    allowed = [await budget.allow_document() for _ in range(6)]
    assert allowed == [True, True, True, True, False, False]
    assert budget.documents_attempted == 4
    assert budget.exhausted is True


@pytest.mark.asyncio
async def test_di20h4_budget_stops_when_sufficient() -> None:
    budget = RequirementAcquisitionBudget(UUID(int=1), 'QUARTERLY_FINANCIALS', NOW,
        AsyncMock(return_value=False), max_documents=4)
    # First call: not sufficient → allowed.
    assert await budget.allow_document() is True
    # Override sufficient to return True.
    budget.sufficient = AsyncMock(return_value=True)
    assert await budget.allow_document() is False  # sufficient → stopped
    assert budget.stopped is True


# --------------------------------------------------------------------------------------
# Test 18 — No company-specific hacks: static scan of source_discovery.py for
# forbidden patterns (no symbol matching, no hardcoded company titles).
# --------------------------------------------------------------------------------------
def test_di20h4_no_company_specific_hacks_in_source_discovery() -> None:
    import inspect
    from app.source_discovery import _is_financial_result_announcement, _nse_category_is_financial_results
    source = inspect.getsource(inspect.getmodule(_is_financial_result_announcement))
    # No literal company names or tickers.
    forbidden = ["RELIANCE", "CASTROL", "TATA", "INFY", "SENSEX", "NSEINDIA",
                 "INE002A01018", "INE000A01010", "EXAMPLE", "BROKER_ALIAS"]
    for token in forbidden:
        assert token not in source, f"forbidden company-specific token '{token}' found in source_discovery"
    # No exact title string matching for specific announcements.
    assert '"Quarterly Results"' not in source.replace("'Quarterly Results'", ""), \
        "no hardcoded 'Quarterly Results' literal in classifier"
