"""Offline semantic boundaries for generic NSE data quality."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.extraction import RuleBasedEventExtractor, governance_disclosure
from app.models import ResearchEventType
from app.research_applicability import classify_requirements, RequirementApplicability, CONCEPT_INPUTS
from app.research_readiness import ResearchRefreshPlanner, ResearchRequirementRegistry
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor, readiness_response
from app.stock_rule_engine import _fact_series, _aligned_ratios
from app.valuation_evidence import materialize_valuation
from test_research_readiness import complete_snapshot, assess, evidence
from test_research_engine import _document
from test_research_readiness_runtime import _profile
from test_stock_rule_engine import _fact, _inputs, NOW
from test_news_readiness_freshness_v2 import valuation_fixture

KEY = 'ORDER_BOOK_CAPEX_GUIDANCE'


def test_financial_business_order_book_na_does_not_exclude_capex_or_guidance():
    decision = classify_requirements('Financial Services', 'Banks - Regional', 'CANONICAL_REFERENCE')[KEY]
    assert decision.concepts['ORDER_BOOK'].state == 'NOT_APPLICABLE'
    assert decision.concepts['CAPEX'].state == 'APPLICABLE'
    assert decision.concepts['GUIDANCE'].state == 'APPLICABLE'
    assert set(decision.excluded_inputs) == {CONCEPT_INPUTS['ORDER_BOOK']}


@pytest.mark.parametrize('industry,source', [(None, None), ('Banks', None), ('Unclassified', 'canonical')])
def test_unknown_is_not_na(industry, source):
    decision = classify_requirements(None, industry, source)[KEY]
    assert decision.concepts['ORDER_BOOK'].state == 'UNKNOWN'
    assert not decision.excluded_inputs


@pytest.mark.parametrize('asset', ['ETF', 'BOND', 'INDEX', 'MUTUAL_FUND'])
def test_asset_na_no_plan_no_completeness_penalty(asset):
    snapshot = complete_snapshot(omit={KEY})
    decisions = classify_requirements(None, None, None, asset_type=asset)
    _, result, _ = assess(replace(snapshot, applicability_by_requirement=decisions))
    plan = ResearchRefreshPlanner().plan(result, requirement_ids=[KEY])
    assert not plan.targets
    assert result.overall_completeness_pct == 100
    assert result.confidence_pct == assess(complete_snapshot())[1].confidence_pct
    assert result.for_requirement(KEY).evidence_ids == ()
    from app.stock_rule_engine import StockRuleEngineEligibilityPolicy
    assert result.mandatory_ready
    assert StockRuleEngineEligibilityPolicy().evaluate(result).full_analysis_allowed


@pytest.mark.parametrize('state', ['APPLICABLE', 'UNKNOWN'])
def test_missing_applicable_and_unknown_enter_plan(state):
    snapshot = complete_snapshot(omit={KEY})
    _, result, _ = assess(replace(snapshot, applicability_by_requirement={KEY: RequirementApplicability(state)}))
    assert result.for_requirement(KEY).status == 'MISSING'
    assert ResearchRefreshPlanner().plan(result, requirement_ids=[KEY]).targets


def test_concept_state_and_exclusion_survive_actual_executor():
    decisions = classify_requirements(None, 'Banks - Regional', 'canonical')
    _, result, _ = assess(replace(complete_snapshot(omit={KEY}), applicability_by_requirement=decisions))
    target = ResearchRefreshPlanner().plan(result, requirement_ids=[KEY]).targets
    profile = _profile()
    repo = SimpleNamespace(refresh_targeted_categories=AsyncMock(), profile=lambda _: profile)
    from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor
    gateway = SimpleNamespace(acquire_requirement=AsyncMock(side_effect=AssertionError('Umbrella acquisition must not search N/A concepts')))
    executor = McpFirstResearchCapabilityExecutor(ExistingResearchCapabilityExecutor(repo, None, None), repo, gateway, enabled=True)
    asyncio.run(executor.execute_primary(result.global_instrument_id, target, jurisdiction='INDIA', correlation_id=None, identity_headers=None))
    assert repo.refresh_targeted_categories.call_args.args[1] == {'CAPEX', 'NEW_FACILITIES', 'GUIDANCE'}
    gateway.acquire_requirement.assert_not_awaited()
    row = next(r for r in readiness_response(result, ResearchRequirementRegistry.default())['requirements'] if r['requirementId'] == KEY)
    assert row['subrequirements']['ORDER_BOOK']['evidenceState'] == 'NOT_APPLICABLE'
    assert row['subrequirements']['GUIDANCE']['evidenceState'] == 'MISSING'


@pytest.mark.parametrize('text,event', [
    ('The glossary defines capital expenditure and order book.', None),
    ('CAPEX was not disclosed in this presentation.', None),
    ('The order of proceedings follows.', None),
    ('Management is optimistic about a bright future.', None),
    ('Capital expenditure incurred amounted to INR 20 crore.', ResearchEventType.CAPEX),
    ('The board approved capex of INR 40 crore.', ResearchEventType.CAPEX),
    ('The order book stood at INR 500 crore.', ResearchEventType.ORDER_BACKLOG_CHANGE),
    ('Revenue guidance maintained for the full year.', ResearchEventType.GUIDANCE_MAINTAINED),
])
def test_meaningful_business_extraction(text, event):
    profile = _profile()
    document = _document('https://nsearchives.nseindia.com/disclosure.html', text)
    document.instrument_id = profile.instrument_id
    document.company_id = profile.company_id
    events = RuleBasedEventExtractor().extract(document)
    assert ([e for e in events if e.event_type == event] if event else not events)


@pytest.mark.parametrize('text,expected', [
    ('Investor presentation with management names and promoter information', False),
    ('Auditor resignation effective today', True),
    ('Corporate governance report', True),
    ('Regulatory action against the company', True),
    ('Meet the management and auditor at our investor day', False),
])
def test_governance_requires_disclosure(text, expected):
    assert governance_disclosure(text) is expected


def test_stale_governance_is_stale_not_missing():
    snapshot = complete_snapshot(overrides={'GOVERNANCE_HISTORY': (evidence('GOVERNANCE_HISTORY', age=timedelta(days=400)),)})
    assert assess(snapshot)[1].for_requirement('GOVERNANCE_HISTORY').status == 'READY_STALE'


def test_unavailable_governance_is_not_negative_company_evidence():
    from app.stock_rule_engine import StockRuleEngineV1
    result = StockRuleEngineV1()._governance(_inputs(events=[], shareholding=[]))
    assert result.raw_score is None
    assert result.metrics == []


@pytest.mark.parametrize('problem', ['unit', 'period', 'conflict'])
def test_valuation_incompatible_denominator_missing_not_zero(problem):
    record, price = valuation_fixture()
    record.snapshot.facts = {'trailingEps': record.snapshot.facts['trailingEps']}
    records = [record]
    if problem == 'unit': record.snapshot.facts['trailingEps'].unit = 'INR_MILLION'
    if problem == 'period': record.snapshot.facts['trailingEps'].period = 'QUARTERLY'
    if problem == 'conflict':
        other = record.model_copy(deep=True)
        other.snapshot.facts['trailingEps'].value = 20
        records.append(other)
    assert materialize_valuation(records, [price], now=price.observed_at) == {}


def test_fact_series_never_interleaves_bases_periods_units():
    first = _fact('revenue', 100, NOW-timedelta(days=90), 'QUARTERLY')
    last = _fact('revenue', 120, NOW, 'QUARTERLY')
    standalone = replace(last, key=replace(last.key, reporting_basis='STANDALONE'), value=last.value.model_copy(update={'value': 999}))
    annual = replace(last, key=replace(last.key, period_type='ANNUAL'))
    result = _fact_series(_inputs(facts=[first, last, standalone, annual]), ['revenue'], 'QUARTERLY')
    assert [r.value for r in result] == [100, 120]
    assert len({(r.reporting_basis, r.period_type, r.unit) for r in result}) == 1


def test_ratios_do_not_mix_reporting_basis():
    n = _fact_series(_inputs(facts=[_fact('pat', 10, NOW)]), ['pat'], 'ANNUAL')[0]
    d = replace(n, value=Decimal(100), reporting_basis='STANDALONE')
    assert _aligned_ratios([n], [d], multiplier=Decimal(100)) == []

@pytest.mark.parametrize('category,title', [
    ('CAPEX', 'Board approved capital expenditure of INR 200 crore'),
    ('ORDERS_BACKLOG', 'Order book stood at INR 400 crore'),
    ('GUIDANCE', 'Revenue guidance maintained for the full year'),
    ('MANAGEMENT', 'Auditor resignation'),
])
def test_official_business_discovery_uses_trusted_mapping_only(category, title):
    import httpx
    from app.source_discovery import OfficialFilingDiscovery
    profile = _profile()
    profile.provider_instrument_ids['NSE'] = 'SYNTHETIC'
    calls = []
    def response(request):
        calls.append(request)
        assert request.url.params['symbol'] == 'SYNTHETIC'
        return httpx.Response(200, json=[{'desc': title, 'attchmntFile': 'https://nsearchives.nseindia.com/test.pdf'}])
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            discovery = OfficialFilingDiscovery(client)
            rows = await discovery.discover(profile, {category}, set())
            assert len(rows) == 1
            assert rows[0].source.categories == (category,)
            profile.provider_instrument_ids.pop('NSE')
            assert await discovery.discover(profile, {category}, set()) == []
    asyncio.run(execute())
    assert len(calls) == 1


def test_missing_trailing_denominator_does_not_fabricate_valuation():
    _, price = valuation_fixture()
    # Raw quarterly EPS does not prove a split-adjusted trailing denominator.
    assert materialize_valuation([], [price], now=price.observed_at) == {}


def test_na_event_does_not_make_other_concepts_ready():
    decision = classify_requirements(None, 'Banks - Regional', 'canonical')
    order = replace(evidence(KEY), covered_input_ids=('MATERIAL_CATALYST_EVIDENCE', CONCEPT_INPUTS['ORDER_BOOK']))
    _, result, _ = assess(replace(complete_snapshot(overrides={KEY:(order,)}), applicability_by_requirement=decision))
    row = result.for_requirement(KEY)
    assert row.status == 'MISSING'
    assert row.concept_evidence_states == {'ORDER_BOOK':'NOT_APPLICABLE','CAPEX':'MISSING','GUIDANCE':'MISSING'}

@pytest.mark.parametrize('unit,value', [('INR crore', '2'), ('INR million', '20'), ('INR lakh', '200'), ('INR', '20000000')])
def test_explicit_currency_scales_feed_equivalent_rule_values(unit, value):
    fact = _fact('revenue', Decimal(value), NOW)
    fact = replace(fact, value=fact.value.model_copy(update={'unit':unit}))
    row = _fact_series(_inputs(facts=[fact]), ['revenue'], 'ANNUAL')[0]
    assert row.value == 20000000
    assert row.unit == 'INR'


def test_fresh_capex_does_not_consume_missing_guidance_or_acquire_na_orders():
    decision = classify_requirements(None, 'Banks - Regional', 'canonical')
    capex = replace(evidence(KEY), covered_input_ids=('MATERIAL_CATALYST_EVIDENCE', CONCEPT_INPUTS['CAPEX']))
    _, result, _ = assess(replace(complete_snapshot(overrides={KEY:(capex,)}), applicability_by_requirement=decision))
    row = result.for_requirement(KEY)
    assert row.status == 'READY_FRESH'
    target = ResearchRefreshPlanner().plan(result, requirement_ids=[KEY]).targets
    assert len(target) == 1
    repo = SimpleNamespace(refresh_targeted_categories=AsyncMock())
    asyncio.run(ExistingResearchCapabilityExecutor(repo,None,None).execute_primary(result.global_instrument_id,
        target, jurisdiction='INDIA', correlation_id=None, identity_headers=None))
    assert repo.refresh_targeted_categories.call_args.args[1] == {'GUIDANCE'}


def test_provider_failure_not_hidden_by_executor_completion():
    from test_research_readiness_runtime import DurableRepositoryFixture
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    from app.research_readiness import ResearchReadinessService
    profile = _profile()
    repo = DurableRepositoryFixture(profile, complete=False)
    repo.acquisition_observations_for = lambda _: [
        {'requirement_id':KEY,'provider':'NSE','outcome':'FAILED','failure_reason':'SOURCE_UNAVAILABLE'},
        {'requirement_id':KEY,'provider':'READINESS_EXECUTOR','outcome':'COMPLETED','evidence_count':0},
    ]
    row = ResearchReadinessService(RepositoryResearchReadinessAdapter(repo)).assess(profile.instrument_id).for_requirement(KEY)
    assert row.missing_reason == 'SOURCE_UNAVAILABLE'
    assert row.applicability != 'NOT_APPLICABLE'
    assert row.acquisition_observation['provider'] == 'NSE'


def test_document_empty_or_no_relevant_evidence_diagnostics():
    from app.research_readiness_runtime import _document_diagnostic
    from app.models import DocumentStatus
    doc = _document('https://example.test/filing', 'A routine notice with no relevant evidence.')
    doc.status = DocumentStatus.PROCESSED
    assert _document_diagnostic(doc, [], [])['state'] == 'PROCESSED_NO_RELEVANT_EVIDENCE'
    doc.normalized_text = ''
    assert _document_diagnostic(doc, [], [])['state'] == 'NO_EXTRACTABLE_TEXT'


def test_direct_repository_refresh_uses_same_positive_applicability_rules():
    from app.repository import ResearchRepository
    from test_stock_rule_engine import _structured
    record = _structured(industry='Banks - Regional')
    profile = _profile().model_copy(update={'instrument_id': record.instrument_id})
    repo = SimpleNamespace(structured_market_snapshots_for=lambda _: {record.instrument_id:[record]})
    assert ResearchRepository._inapplicable_business_categories(repo, profile) == {'ORDERS_BACKLOG', 'CONTRACTS'}
    record.snapshot.facts.pop('industry')
    assert ResearchRepository._inapplicable_business_categories(repo, profile) == set()


def test_resolved_governance_document_does_not_remain_fresh_forever():
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    doc = _document('https://example.test/governance', 'Regulatory action was resolved and the case closed.')
    values = {'GOVERNANCE_HISTORY':[], 'SECTOR_MACRO':[]}
    RepositoryResearchReadinessAdapter._append_documents(values, [doc])
    assert len(values['GOVERNANCE_HISTORY']) == 1
    assert not values['GOVERNANCE_HISTORY'][0].unresolved


def test_annual_completeness_does_not_combine_standalone_and_consolidated():
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    first = _fact('revenue', 100, NOW-timedelta(days=365))
    second = _fact('revenue', 120, NOW)
    second = replace(second, key=replace(second.key, reporting_basis='STANDALONE'))
    values = {r.requirement_id:[] for r in ResearchRequirementRegistry.default().requirements}
    RepositoryResearchReadinessAdapter._append_financial_evidence(values, [first,second])
    assert not any('ANNUAL_CAGR_INPUTS' in row.covered_input_ids for row in values['GROWTH_FACTS'])

@pytest.mark.parametrize('revision,newer', [(True,True),(False,True),(True,False)])
def test_explicit_newer_official_revision_reconciles_without_other_source_deletion(revision,newer):
    from test_di15_financial_authority_upgrade import _repo, _seed
    from test_official_nse_financial_parsing import _document as filing, JUNE
    repo, profile = _repo()
    original = filing(JUNE)
    original.published_at = NOW-timedelta(days=2)
    old = repo._official_financial_fact_candidates(original)
    _seed(repo, old)
    document = filing(JUNE.replace('8,261.11','8,262.11'))
    document.title = 'Revised financial results' if revision else 'Financial results'
    document.published_at = NOW-timedelta(days=1 if newer else 3)
    repo.documents[document.document_id] = document
    expected = repo._official_financial_fact_candidates(document)
    key = next(f.key for f in expected if f.key.metric == 'revenue' and f.key.period_end == '2026-06-30')
    extra = replace(old[0], key=replace(old[0].key, period_end='2020-06-30'), source_identity='unrelated-source')
    _seed(repo, [extra])
    asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    stored = repo.financial_facts_for(profile.instrument_id)
    chosen = next(f for f in stored if f.key == key)
    assert chosen.value.value == Decimal('8262.11' if revision and newer else '8261.11')
    assert any(f.key == extra.key and f.source_identity == 'unrelated-source' for f in stored)
    assert len(stored) == len({f.key for f in stored}) == len(old)+1
    assert not asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))


@pytest.mark.parametrize('text,url,state', [
    ('[PDF_PAGE 1]', 'https://example.test/scanned.pdf', 'NO_EXTRACTABLE_TEXT'),
    ('', 'https://example.test/archive.zip', 'UNSUPPORTED_DOCUMENT'),
    ('Corporate governance report', 'https://example.test/governance.html', 'PROCESSED_WITH_EVIDENCE'),
])
def test_document_diagnostics_distinguish_supported_qualitative_evidence(text,url,state):
    from app.models import DocumentStatus
    from app.research_readiness_runtime import _document_diagnostic
    document = _document(url,text)
    document.status = DocumentStatus.PROCESSED
    assert _document_diagnostic(document,[],[])['state'] == state

@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('published', [NOW, None])
def test_trusted_official_revision_metadata_reaches_ingestion(asynchronous, published):
    from test_di15_financial_authority_upgrade import _repo, _seed
    from test_official_nse_financial_parsing import _document as filing, JUNE
    from app.source_registry import RegisteredResearchSource
    from app.models import SourceType, SourceClassification, ReliabilityLevel
    from app.research_fetching import FetchResult
    repo, profile = _repo()
    original = filing(JUNE)
    _seed(repo, repo._official_financial_fact_candidates(original))
    url = 'https://nsearchives.nseindia.com/corporate/revised.pdf'
    source = RegisteredResearchSource('revision', profile.instrument_id, url,
        SourceType.EXCHANGE_ANNOUNCEMENT, 'NSE', 'NSE', ReliabilityLevel.LEVEL_A,
        source_classification=SourceClassification.EXCHANGE, company_id=profile.company_id,
        discovery_method='NSE_OFFICIAL_API', categories=('FINANCIAL_RESULTS',),
        official_nse_profile_symbol=profile.provider_instrument_ids['NSE'],
        official_published_at=published, official_title='Revised financial results')
    result = FetchResult(url, 200, 'application/pdf', JUNE.replace('8,261.11','8,262.11'), 100)
    if asynchronous:
        document = asyncio.run(repo._ingest_registered_fetch_result_async(profile,source,result,expected_profile=profile))
    else:
        document = repo._ingest_registered_fetch_result(profile,source,result,expected_profile=profile)
    assert document.title == source.official_title
    assert document.published_at == published
    chosen = next(f for f in repo.financial_facts_for(profile.instrument_id)
        if f.key.metric == 'revenue' and f.key.period_end == '2026-06-30')
    assert chosen.value.value == Decimal('8262.11' if published else '8261.11')
    assert chosen.value.published_at == (published or original.published_at)

@pytest.mark.parametrize('prior_unit,comparable', [('USD',False),('INR crore',True)])
def test_quarterly_readiness_respects_normalized_currency(prior_unit,comparable):
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    facts = []
    for period, unit in [(NOW-timedelta(days=90),prior_unit),(NOW,'INR')]:
        for metric in ('revenue','pat'):
            fact = _fact(metric,100,period,'QUARTERLY')
            facts.append(replace(fact,value=fact.value.model_copy(update={'unit':unit})))
    values = {r.requirement_id:[] for r in ResearchRequirementRegistry.default().requirements}
    RepositoryResearchReadinessAdapter._append_financial_evidence(values,facts)
    assert any('COMPARABLE_QUARTERS' in row.covered_input_ids for row in values['QUARTERLY_FINANCIALS']) is comparable


def test_normalized_official_input_survives_restart_and_repairs_old_parser_value(tmp_path):
    from app.persistence import SqliteResearchPersistence
    from app.repository import ResearchRepository, _TRUSTED_NSE_PROFILE_IDENTITY
    from app.settings import Settings
    from app.models import SourceType, SourceClassification, SourceMode, ReliabilityLevel
    from test_official_nse_financial_parsing import _profile as official_profile, JUNE
    store = SqliteResearchPersistence(tmp_path/'research.sqlite')
    settings = Settings(research_demo_enabled=False)
    repo = ResearchRepository(settings=settings,persistence=store)
    profile = official_profile()
    repo.profiles = [profile]
    document = repo.ingest_fixture(original_url='https://nsearchives.nseindia.com/restart.pdf',
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name='NSE',publisher='NSE',content_type='application/pdf',body=JUNE,
        reliability=ReliabilityLevel.LEVEL_A,source_mode=SourceMode.REAL,
        discovery_provider='NSE_OFFICIAL_API', expected_profile=profile,
        _trusted_profile_identity=_TRUSTED_NSE_PROFILE_IDENTITY, _metadata_only_nse_financial_result=True)
    original = next(f for f in repo.financial_facts_for(profile.instrument_id) if f.key.metric == 'revenue')
    poisoned = replace(original,value=original.value.model_copy(update={'value':Decimal('999')}))
    store.upsert_financial_fact(poisoned,allow_same_tier_correction=True)
    restarted = ResearchRepository(settings=settings,persistence=store)
    restarted.profiles = [profile]
    assert next(d for d in restarted.documents_for(profile.instrument_id) if d.document_id == document.document_id).normalized_text
    assert asyncio.run(restarted._reconcile_incomplete_persisted_official_financial_facts(profile))
    facts = restarted.financial_facts_for(profile.instrument_id)
    assert next(f for f in facts if f.key == original.key).value.value == original.value.value
    assert len(facts) == len({f.key for f in facts})
    assert not asyncio.run(restarted._reconcile_incomplete_persisted_official_financial_facts(profile))
    assert list(tmp_path.glob('*.pdf')) == []

@pytest.mark.parametrize('row_symbol,row_isin,accepted', [
    ('UNRELATED',None,False),
    ('READY','WRONG_ISIN',False),
    ('UNRELATED','MATCH',True),
    ('READY','MATCH',True),
])
def test_official_announcement_rejects_identity_contradictions_with_exact_isin_priority(row_symbol,row_isin,accepted):
    import httpx
    from app.source_discovery import OfficialFilingDiscovery
    profile = _profile()
    profile.provider_instrument_ids['NSE'] = 'READY'
    row = {'desc':'Financial Results','attchmntFile':'https://nsearchives.nseindia.com/result.pdf','symbol':row_symbol}
    if row_isin:
        row['isin'] = profile.isin if row_isin == 'MATCH' else row_isin
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=[row]))) as client:
            return await OfficialFilingDiscovery(client).discover(profile,{'FINANCIAL_RESULTS'},set())
    assert bool(asyncio.run(execute())) is accepted


def test_trusted_nse_business_target_uses_category_aware_production_dispatch():
    from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor
    decisions = classify_requirements(None,'Engineering & Construction','canonical')
    _, readiness, _ = assess(replace(complete_snapshot(omit={KEY}),applicability_by_requirement=decisions))
    targets = ResearchRefreshPlanner().plan(readiness,requirement_ids=[KEY]).targets
    assert targets and not targets[0].excluded_input_ids
    profile = _profile()
    profile.provider_instrument_ids['NSE'] = 'READY'
    repo = SimpleNamespace(profile=lambda _:profile,refresh_targeted_categories=AsyncMock())
    gateway = SimpleNamespace(acquire_requirement=AsyncMock(side_effect=AssertionError('NSE must get the scoped target')))
    executor = McpFirstResearchCapabilityExecutor(ExistingResearchCapabilityExecutor(repo,None,None),repo,gateway,enabled=True)
    asyncio.run(executor.execute_primary(profile.instrument_id,targets,jurisdiction='INDIA',correlation_id=None,identity_headers=None))
    gateway.acquire_requirement.assert_not_awaited()
    repo.refresh_targeted_categories.assert_awaited_once()
    assert repo.refresh_targeted_categories.call_args.args[1] == {'ORDERS_BACKLOG','CONTRACTS','CAPEX','NEW_FACILITIES','GUIDANCE'}
