"""Offline retry/recovery contracts: original PDF bytes, no DB or providers."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app import repository as repository_module
from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier, merge_fact
from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE, classify_reason
from app.financial_authority import annual_income_history_gaps, authoritative_financial_upgrade_required, quarterly_income_history_gaps
from app.financial_gap_fill import load_financial_gap_state, required_quarterly_periods
from app.financial_metric_extraction import extract_semantic_facts
from app.financial_projection import project_semantic_financial_facts
from app.models import CompanyResearchProfile, ProvenancedValue, ResearchDocument, SourceMode
from app.normalization import content_hash, normalize_financial_fact, normalize_text
from app.pdf_structure import preserve_pdf_structure
from app.repository import ResearchRepository
from app.research_fetching import HttpResearchFetcher, NetworkFetchResult
from app.research_readiness import ResearchReadinessService, ResearchRefreshPlanner, ResearchRequirementStatus
from app.research_readiness_runtime import ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter
from app.settings import Settings
from app.source_registry import RegisteredResearchSource
from app.stock_rule_engine import _fact_series
from test_nse_pdf_layout_financials import PDF, PDF_HASH, SOURCE, EXPECTED, _pdf_pages

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("financial_fixture_clock")
INSTRUMENT, COMPANY = UUID(int=701), UUID(int=702)


def _profile():
    return CompanyResearchProfile(instrument_id=INSTRUMENT, company_id=COMPANY, company_name='20 Microns Limited',
        ticker='20MICRONS', exchange='NSE', mic='XNSE', country='IN', currency='INR',
        provider_instrument_ids={'NSE': '20MICRONS'})


def _source(profile):
    return RegisteredResearchSource('original-fixture', INSTRUMENT, SOURCE, 'EXCHANGE_ANNOUNCEMENT', 'NSE', 'NSE', 'LEVEL_A',
        source_classification='EXCHANGE', company_id=COMPANY, discovery_method='NSE_OFFICIAL_API',
        categories=('FINANCIAL_RESULTS',), official_nse_profile_symbol=profile.provider_instrument_ids['NSE'],
        official_title='Audited financial results', official_published_at=datetime(2026, 5, 22, tzinfo=timezone.utc))


def _document(text='Audited consolidated financial results with unresolved table cells', **updates):
    values = dict(canonical_url=SOURCE, original_url=SOURCE, source_type='EXCHANGE_ANNOUNCEMENT',
        source_classification='EXCHANGE', source_name='NSE', publisher='NSE', content_type='application/pdf',
        document_type='PDF_REFERENCE', normalized_text=text, content_hash=content_hash(text),
        title='Audited financial results', status='PROCESSED', reliability_level='LEVEL_A', source_mode='REAL',
        instrument_id=INSTRUMENT, company_id=COMPANY, retrieved_at=NOW,
        published_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
        discovery_provider='NSE_OFFICIAL_API', entity_resolution_confidence=.99)
    values.update(updates)
    return ResearchDocument(**values)


def _fact(metric='revenue', period='2026-03-31', amount='100', unit='INR', basis='CONSOLIDATED', owner='other'):
    return FinancialFact(FinancialFactKey(INSTRUMENT, metric, period, 'ANNUAL', basis),
        ProvenancedValue(value=Decimal(amount), unit=unit, source_url=SOURCE, source_name='NSE',
            retrieved_at=NOW, as_of_date=datetime.fromisoformat(period).replace(tzinfo=timezone.utc)),
        FactSourceTier.OFFICIAL_NSE, 'NSE', owner, SourceMode.REAL)


class MemoryFinancialStore:
    """Only Python data; never opens a database driver."""
    def __init__(self, facts=()):
        self.facts = {f.key: f for f in facts}
        self.observations, self.reconciliations = [], []

    def load_financial_facts(self, instruments):
        return [f for f in self.facts.values() if f.key.instrument_id in instruments]

    def load_acquisition_observations(self, instrument):
        return [r for r in self.observations if r['instrument_id'] == instrument]

    def upsert_acquisition_observation(self, instrument, requirement, provider, outcome, observed_at,
                                     source_url=None, failure_reason=None, evidence_count=0):
        self.observations.append(dict(instrument_id=instrument, requirement_id=requirement, provider=provider,
            outcome=outcome, observed_at=observed_at, source_url=source_url,
            failure_reason=failure_reason, evidence_count=evidence_count))

    def upsert_financial_fact(self, fact, *, allow_same_tier_correction=False):
        prior = self.facts.get(fact.key)
        accepted = merge_fact(prior, fact, allow_same_tier_correction=allow_same_tier_correction)
        self.facts[fact.key] = accepted
        return prior != accepted

    def reconcile_financial_facts_for_source(self, instrument, owner, facts, *, complete_scopes=(), **options):
        self.reconciliations.append((owner, tuple(facts), complete_scopes))
        assert not complete_scopes  # Partial layout must never authorize deletion.
        return sum(self.upsert_financial_fact(f, allow_same_tier_correction=(
            self.facts.get(f.key) is not None and self.facts[f.key].source_identity == owner)) for f in facts)


def _repository(documents=(), facts=(), fetcher=None):
    repo = ResearchRepository.__new__(ResearchRepository)
    repo.settings, repo._persistence, repo._fetcher = Settings(), MemoryFinancialStore(facts), fetcher
    profile = _profile()
    repo.profile = lambda *_: profile
    repo.documents_for = lambda *_, **__: list(documents)
    repo._financial_authority_attempts, repo._official_filing_flights = {}, {}
    repo._category_successful_no_change_checks = {}
    repo._category_refresh = {}

    async def blocking(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    repo._run_blocking_persistence = blocking
    repo.structured_market_snapshots_for = lambda *_: {}
    repo.market_price_observations_for = lambda *_: {}
    repo.daily_market_bars_for = lambda *_: {}
    repo.events_for = lambda *_, **__: []
    repo.shareholding_for = lambda *_, **__: []
    repo.shareholding_period_groups = lambda *_, **__: []
    repo.news_records_for = lambda *_, **__: []
    repo._sync_persisted_document_identities = lambda *_: None
    repo._apply_prepared_ingested_document_async = AsyncMock(side_effect=AssertionError(
        'Same-content recovery must retain the durable identity'))
    return repo, repo._persistence, profile


def _receipt(repo, doc, outcome='UNSUPPORTED', *, age=timedelta(), failure=None):
    repo._record_financial_document_parse(doc, outcome)
    receipt = repo._persistence.observations[-1]
    receipt['observed_at'] = NOW - age
    if failure:
        receipt['failure_reason'] = failure
    return receipt


@pytest.fixture(scope='module')
def original():
    content = PDF.read_bytes()
    assert hashlib.sha256(content).hexdigest() == PDF_HASH
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail('No provider requests')))
    fetcher = HttpResearchFetcher(Settings(), client=client)
    response = NetworkFetchResult(SOURCE, 200, 'application/pdf', content, {}, False, None, 0, 0, 0)
    result = fetcher.process_network_response(response)
    doc = _document(normalize_text(result.text), pdf_structure=result.pdf_structure)
    yield doc, fetcher, response
    fetcher._pdf_extraction_executor.shutdown(wait=True)
    asyncio.run(client.aclose())


def test_parsed_receipt_and_ready_facts_survive_capability_upgrade(original, monkeypatch):
    document = original[0]
    facts = project_semantic_financial_facts(document).facts
    repo, store, _ = _repository([document], facts)
    repo._record_financial_document_parse(document, 'PARSED', facts)
    monkeypatch.setattr(repository_module, 'FINANCIAL_PARSER_VERSION', 'next-version')
    monkeypatch.setattr(repository_module, 'FINANCIAL_PARSER_CAPABILITY', 'NEW_VERIFIED_LAYOUT_CAPABILITY')
    assert repo._financial_document_parse_unchanged(document) == 'PARSED'
    before = dict(store.facts)
    assert repo._reconcile_persisted_official_financial_document(document) == (True, 0)
    assert store.facts == before and not store.reconciliations
    assert repo._reusable_official_document(INSTRUMENT, SOURCE, source=_source(_profile())) is document


@pytest.mark.parametrize('change', ['version', 'capability', 'content', 'missing_requirements'])
def test_unsupported_reopens_once_for_meaningful_change(change, monkeypatch):
    document = _document()
    repo, store, _ = _repository([document])
    _receipt(repo, document)
    assert repo._financial_document_parse_unchanged(document, now=NOW) == 'UNSUPPORTED'
    if change == 'version':
        monkeypatch.setattr(repository_module, 'FINANCIAL_PARSER_VERSION', 'new-version')
    elif change == 'capability':
        monkeypatch.setattr(repository_module, 'FINANCIAL_PARSER_CAPABILITY', 'NEW_WORD_AND_CELL_SUPPORT')
    elif change == 'content':
        document = document.model_copy(update={'normalized_text': document.normalized_text + ' Amended results'})
    else:
        new = _fact()
        store.facts[new.key] = new
    assert repo._financial_document_parse_unchanged(document, now=NOW) is None
    _receipt(repo, document)
    assert repo._financial_document_parse_unchanged(document, now=NOW) == 'UNSUPPORTED'


def test_unchanged_unsupported_respects_failure_specific_backoff(monkeypatch):
    document = _document()
    repo, _, _ = _repository([document])
    receipt = _receipt(repo, document)
    assert repo._financial_document_parse_unchanged(document, now=NOW + timedelta(days=2)) == 'UNSUPPORTED'
    assert repo._financial_document_parse_unchanged(document, now=NOW + timedelta(days=4)) is None
    receipt['failure_reason'] = 'PDF_EXTRACTION_TIMEOUT'
    retry = timedelta(seconds=repo.settings.research_authority_retry_backoff_seconds)
    assert repo._financial_document_parse_unchanged(document, now=NOW + retry / 2) == 'UNSUPPORTED'
    assert repo._financial_document_parse_unchanged(document, now=NOW + retry + timedelta(seconds=1)) is None
    receipt['failure_reason'] = 'ROBOTS_OR_ACCESS_BLOCKED'
    monkeypatch.setattr(repository_module, 'FINANCIAL_PARSER_CAPABILITY', 'NEW_CAPABILITY')
    assert repo._financial_document_parse_unchanged(document, now=NOW) == 'UNSUPPORTED'
    assert classify_reason('PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS') == EVIDENCE_UNAVAILABLE
    assert classify_reason('PARSER_FAILED') == TECHNICAL_RETRYABLE
    assert classify_reason('PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS|NETWORK_TIMEOUT') == TECHNICAL_RETRYABLE


def test_legacy_unsupported_receipt_requires_one_current_byte_attempt():
    document = _document()
    repo, _, _ = _repository([document])
    receipt = _receipt(repo, document)
    identity = repo._financial_document_parse_identity(document).replace('financial-parse:3:', 'financial-parse:2:')
    receipt['source_url'] = identity + '?keys=%5B%5D'
    assert repo._financial_document_parse_unchanged(document) is None
    _receipt(repo, document)
    assert repo._financial_document_parse_unchanged(document) == 'UNSUPPORTED'


def test_checked_receipt_cannot_suppress_due_parser_or_source_retry(monkeypatch):
    document = _document()
    repo, store, profile = _repository([document])
    receipt = _receipt(repo, document)
    repo._record_financial_reconciliation(profile, repo._financial_reconciliation_identity(profile))
    assert repo._financial_reconciliation_unchanged(profile)
    receipt['observed_at'] = NOW - timedelta(days=4)
    assert not repo._financial_reconciliation_unchanged(profile)
    receipt['observed_at'] = NOW
    monkeypatch.setattr(repository_module, 'FINANCIAL_PARSER_CAPABILITY', 'NEW_CAPABILITY')
    assert not repo._financial_reconciliation_unchanged(profile)
    store.upsert_acquisition_observation(INSTRUMENT, 'QUARTERLY_FINANCIALS', 'NSE', 'FAILED', NOW,
        failure_reason='PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS')
    assert repo.financial_authority_upgrade_due(profile, NOW)
    store.observations[-1]['source_url'] = repo._financial_parser_retry_context(profile)
    assert not repo.financial_authority_upgrade_due(profile, NOW)
    repo._financial_authority_attempts[INSTRUMENT] = NOW + timedelta(seconds=2)
    store.observations[-1]['source_url'] = None
    assert not repo.financial_authority_upgrade_due(profile, NOW + timedelta(seconds=3))


def test_text_only_reconstruction_is_not_layout_recovery(original):
    document = original[0].model_copy(update={'pdf_structure': None})
    repo, store, profile = _repository([document])
    old = _receipt(repo, document)
    old['source_url'] = old['source_url'].replace('financial-parse:3:', 'financial-parse:2:')
    text_only = preserve_pdf_structure(document.normalized_text)
    assert text_only.representation == 'EXTRACTED_LINES'
    assert not any(p.tables or any(line.words for line in p.lines) for p in text_only.pages)
    assert not extract_semantic_facts(document.normalized_text, structure=text_only).accepted_facts
    assert repo._reconcile_persisted_official_financial_document(document) == (False, 0)
    assert store.observations[-1] is old
    assert repo._reusable_official_document(INSTRUMENT, SOURCE, source=_source(profile)) is None
    _receipt(repo, document)
    assert repo._reusable_official_document(INSTRUMENT, SOURCE, source=_source(profile)) is document


@pytest.mark.asyncio
@pytest.mark.parametrize('cached', [True, False])
async def test_normal_acquisition_recovers_real_layout_and_preserves_history(original, cached, monkeypatch):
    original_doc, fetcher, response = original
    document = original_doc.model_copy(update={'pdf_structure': None})
    history = _fact(period='2024-03-31', owner=str(document.document_id))
    standalone = _fact(period='2025-03-31', basis='STANDALONE', owner='another-document')
    repo, store, profile = _repository([document], [history, standalone], fetcher)
    receipt = _receipt(repo, document)
    receipt['source_url'] = receipt['source_url'].replace('financial-parse:3:', 'financial-parse:2:')
    source = _source(profile)
    assert repo._reusable_official_document(INSTRUMENT, SOURCE, source=source) is None
    if cached:
        fetcher._cache_pdf_response(response)
    else:
        with fetcher._pdf_byte_cache_lock:
            fetcher._pdf_byte_cache.clear()
            fetcher._pdf_byte_cache_bytes = 0
    # The uncached branch simulates one normal fetch with local bytes. The
    # HTTP client rejects every real request, including provider/DNS IO.
    network = AsyncMock(return_value=response)
    monkeypatch.setattr(fetcher, 'fetch_network', network)
    recovered, joined = await repo._single_flight_official_filing(profile, source)
    assert not joined and network.await_count == (0 if cached else 1)
    assert not repo._official_filing_flights
    assert recovered.document_id == document.document_id and recovered.normalized_text == document.normalized_text
    structure = recovered.pdf_structure
    assert structure.representation == 'PDF_LAYOUT' and structure.content_hash == PDF_HASH
    assert structure.pages[16].tables and all(w.region.geometry for line in structure.pages[16].lines for w in line.words)
    semantic = extract_semantic_facts(recovered.normalized_text, structure=structure, evidence_at=recovered.published_at)
    assert {(f.metric, f.period_end): f.value for f in semantic.accepted_facts if (f.metric, f.period_end) in EXPECTED} == EXPECTED
    projection = project_semantic_financial_facts(recovered, semantic)
    assert projection.state == 'PARTIAL_FOR_SOURCE_SCOPE' and not projection.scopes
    income = [f for f in projection.facts if f.key.metric in {'revenue', 'pat'}]
    assert len(income) == 4
    for f in income:
        assert f.key.reporting_basis == 'CONSOLIDATED' and f.key.period_type == 'ANNUAL'
        column = 4 if f.key.period_end == '2026-03-31' else 5
        assert 'page=17;' in f.value.calculation_basis and f'column={column};' in f.value.calculation_basis
        assert 'row=' in f.value.calculation_basis and 'audit=AUDITED' in f.value.calculation_basis
        assert f.source_identity == str(document.document_id) and f.value.source_url == SOURCE
        assert f.value.unit == 'INR lakh' and PDF_HASH in f.value.calculation_basis
    assert store.facts[history.key] is history and store.facts[standalone.key] is standalone
    assert len(store.facts) == len(projection.facts) + 2
    assert len(store.reconciliations) == 1
    assert repo._reconcile_persisted_official_financial_document(recovered) == (True, 0)
    assert len(store.reconciliations) == 1
    assert repo._financial_document_parse_unchanged(document) == 'PARSED'
    assert repo._reusable_official_document(INSTRUMENT, SOURCE, source=source) is document
    normalized = repo.financial_facts_for(INSTRUMENT)
    current = next(f for f in normalized if f.key.metric == 'revenue' and f.key.period_end == '2026-03-31')
    assert current.value.value == Decimal('9538326000') and current.value.unit == 'INR'
    assert 'source_unit=INR lakh' in current.value.calculation_basis
    assert not any(f.key.metric in {'ebit', 'roce'} for f in normalized)


@pytest.mark.asyncio
async def test_registered_source_recovery_also_uses_cached_original_bytes(original, monkeypatch):
    document, fetcher, response = original
    document = document.model_copy(update={'pdf_structure': None})
    repo, store, profile = _repository([document], fetcher=fetcher)
    fetcher._cache_pdf_response(response)
    network = AsyncMock(side_effect=AssertionError('Cached original bytes must be reused'))
    monkeypatch.setattr(fetcher, 'fetch', network)
    recovered = await repo._fetch_registered_source(profile, _source(profile), expected_profile=profile)
    assert recovered.pdf_structure.representation == 'PDF_LAYOUT'
    assert len(store.facts) == 6 and not network.await_count


def test_cache_is_bounded_expires_and_cannot_replace_newer_publication(original, monkeypatch):
    document, fetcher, response = original
    document = document.model_copy(update={'pdf_structure': None})
    repo, _, profile = _repository([document], fetcher=fetcher)
    fetcher._cache_pdf_response(response)
    assert repo._cached_official_pdf_response(profile, _source(profile)) is response
    newer = replace(_source(profile), official_published_at=document.published_at + timedelta(days=1))
    assert repo._cached_official_pdf_response(profile, newer) is None
    monkeypatch.setattr(fetcher, '_pdf_byte_cache_ttl_seconds', 0)
    assert fetcher.cached_pdf_response(SOURCE) is None and fetcher._pdf_byte_cache_bytes == 0
    monkeypatch.setattr(fetcher, '_pdf_byte_cache_max_bytes', 12)
    for n in range(3):
        fetcher._cache_pdf_response(replace(response, final_url=f'https://nsearchives.nseindia.com/{n}.pdf', content=b'%PDF1234'))
    assert len(fetcher._pdf_byte_cache) == 1 and fetcher._pdf_byte_cache_bytes == 8


def test_partial_annual_facts_keep_fy2024_requirements_acquirable(original):
    document = original[0]
    projected = project_semantic_financial_facts(document).facts
    repo, store, profile = _repository([document], projected)
    repo._record_financial_document_parse(document, 'PARSED', projected)
    state = load_financial_gap_state(repo, INSTRUMENT)
    growth = state.readiness.for_requirement('GROWTH_FACTS')
    assert growth.status == ResearchRequirementStatus.PARTIAL
    assert 'ANNUAL_CAGR_INPUTS' in growth.missing_input_ids
    expected = frozenset(FinancialFactKey(INSTRUMENT, metric, '2024-03-31', 'ANNUAL', 'CONSOLIDATED')
                         for metric in ('revenue', 'pat'))
    assert state.missing_annual_fact_keys == expected == annual_income_history_gaps(projected)
    assert authoritative_financial_upgrade_required(profile, projected)
    plan = ResearchRefreshPlanner().plan(state.readiness, jurisdiction='INDIA', requirement_ids=['GROWTH_FACTS'])
    assert len(plan.targets) == 1 and plan.targets[0].requirement_id == 'GROWTH_FACTS'
    for key in expected:
        assert state.accepts('GROWTH_FACTS', _fact(key.metric, key.period_end, owner='earlier-filing'), 'INR')
    before = dict(store.facts)
    assert repo._reconcile_persisted_official_financial_document(document) == (True, 0)
    assert store.facts == before and not store.reconciliations


@pytest.mark.asyncio
async def test_missing_requirement_plan_reaches_normal_financial_acquisition(original):
    document = original[0]
    repo, _, _ = _repository([document], project_semantic_financial_facts(document).facts)
    state = load_financial_gap_state(repo, INSTRUMENT)
    plan = ResearchRefreshPlanner().plan(state.readiness, jurisdiction='INDIA', requirement_ids=['GROWTH_FACTS'])
    repo.refresh_targeted_categories = AsyncMock()
    executor = ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), SimpleNamespace())
    result = await executor.execute_primary(INSTRUMENT, plan.targets, jurisdiction='INDIA',
        correlation_id='offline-only', identity_headers=None)
    assert not result.failures
    args, kwargs = repo.refresh_targeted_categories.await_args
    assert args == (INSTRUMENT, {'FINANCIAL_RESULTS'})
    assert 'authority_upgrade_categories' not in kwargs
    repo._category_evidence_timing = lambda *_: (NOW, datetime(2026, 3, 31, tzinfo=timezone.utc))
    repo._category_refresh[INSTRUMENT, 'FINANCIAL_RESULTS'] = NOW
    assert not repo._category_is_eligible_to_check(INSTRUMENT, 'FINANCIAL_RESULTS', NOW)[0]
    assert repo._category_is_eligible_to_check(INSTRUMENT, 'FINANCIAL_RESULTS', NOW, bypass_evidence_cooldown=True)[0]


@pytest.mark.asyncio
async def test_real_byte_retry_that_remains_unsupported_records_backoff(original, monkeypatch):
    fetcher = original[1]
    response = replace(original[2], content=_pdf_pages([{
        'ruled': False, 'annual_group': 'Year ended Quarter ended', 'annual_audit': 'Aud1ted'}]))
    result = fetcher.process_network_response(response)
    assert result.pdf_structure.representation == 'EXTRACTED_LINES'
    document = _document(normalize_text(result.text))
    assert not project_semantic_financial_facts(document).facts
    repo, store, profile = _repository([document], fetcher=fetcher)
    old = _receipt(repo, document)
    old['source_url'] = old['source_url'].replace('financial-parse:3:', 'financial-parse:2:')
    monkeypatch.setattr(fetcher, 'fetch_network', AsyncMock(side_effect=AssertionError('Reuse cached bytes')))
    await repo._single_flight_official_filing(profile, _source(profile))
    assert len(store.observations) == 2 and store.observations[-1]['outcome'] == 'UNSUPPORTED'
    assert repo._financial_document_parse_unchanged(document) == 'UNSUPPORTED'
    assert repo._reusable_official_document(INSTRUMENT, SOURCE, source=_source(profile)) is document
    assert not store.facts and not store.reconciliations


def test_complete_ready_growth_is_reused_and_incoherent_series_stay_partial():
    facts = [_fact(metric, f'{year}-03-31') for year in (2024, 2025, 2026) for metric in ('revenue', 'pat')]
    facts += [replace(_fact(metric, period), key=FinancialFactKey(INSTRUMENT, metric, period, 'QUARTERLY', 'CONSOLIDATED'))
              for period in ('2026-03-31', '2026-06-30', '2025-12-31', '2025-09-30') for metric in ('revenue', 'pat')]
    repo, _, _ = _repository(facts=facts)
    readiness = ResearchReadinessService(RepositoryResearchReadinessAdapter(repo)).assess(INSTRUMENT, evidence_only=True)
    assert readiness.for_requirement('GROWTH_FACTS').status == ResearchRequirementStatus.READY_FRESH
    assert not ResearchRefreshPlanner().plan(readiness, requirement_ids=['GROWTH_FACTS']).targets
    assert not annual_income_history_gaps(facts)
    for replacement in (_fact('total_debt', '2024-03-31'), _fact('pat', '2024-03-31', basis='STANDALONE'),
                        _fact('pat', '2024-03-31', unit='USD')):
        partial = [f for f in facts if not (f.key.metric == 'pat' and f.key.period_end == '2024-03-31')]
        partial.append(replacement)
        repo, _, _ = _repository(facts=partial)
        assert 'ANNUAL_CAGR_INPUTS' in load_financial_gap_state(repo, INSTRUMENT).missing['GROWTH_FACTS']


@pytest.mark.asyncio
@pytest.mark.parametrize('unit,amount', [('INR lakh', '100'), ('INR crore', '1'), ('INR thousand', '10000'), ('INR', '10000000')])
async def test_shared_financial_reads_compare_scales_once_and_preserve_source(unit, amount):
    source = _fact(amount=amount, unit=unit)
    pat, equity = _fact('pat', amount='10', unit='INR lakh'), _fact('total_equity', amount='10000000', unit='INR')
    repo, store, _ = _repository(facts=[source, pat, equity])
    synchronous = repo.financial_facts_for(INSTRUMENT)
    batched = (await repo.financial_facts_for_instruments({INSTRUMENT}))[INSTRUMENT]
    for values in (synchronous, batched):
        revenue = next(f for f in values if f.key.metric == 'revenue')
        assert revenue.value.value == Decimal('10000000') and revenue.value.unit == 'INR'
        assert normalize_financial_fact(revenue) is revenue
        series = _fact_series(SimpleNamespace(financial_facts=values), ('revenue',), 'ANNUAL')
        assert len(series) == 1 and series[0].value == Decimal('10000000')
        assert next(f for f in values if f.key.metric == 'roe').value.value == Decimal('10')
        if unit != 'INR':
            assert f'source_unit={unit}' in revenue.value.calculation_basis
    assert store.facts[source.key] is source and source.value.value == Decimal(amount) and source.value.unit == unit
    eps = _fact('eps', amount='18.94', unit='INR per share')
    assert normalize_financial_fact(eps) is eps


def _qfact(metric='revenue', period='2026-06-30', amount='100', unit='INR', basis='CONSOLIDATED', owner='other'):
    return FinancialFact(
        FinancialFactKey(INSTRUMENT, metric, period, 'QUARTERLY', basis),
        ProvenancedValue(value=Decimal(amount), unit=unit, source_url=SOURCE, source_name='NSE',
            retrieved_at=NOW, as_of_date=datetime.fromisoformat(period).replace(tzinfo=timezone.utc)),
        FactSourceTier.OFFICIAL_NSE, 'NSE', owner, SourceMode.REAL)


def _four_quarters(prefix='2025-', suffix='-30'):
    """The four most-recent quarter-ends on a March-31 fiscal calendar."""
    return [f'{prefix}06-30', f'{prefix}03-31', f'2025-12-31', '2025-09-30']


def test_required_quarterly_periods_derives_four_completed_quarters_from_latest():
    """Anchors on the latest *reported* quarter-end and walks back by 3-month
    intervals without inventing future or unpublished periods."""
    facts = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30')
             for m in ('revenue', 'pat')]
    periods = required_quarterly_periods(facts, NOW)
    assert periods == ('2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30')


def test_required_quarterly_periods_empty_when_no_quarterly_facts():
    assert required_quarterly_periods([], NOW) == ()


def test_required_quarterly_periods_respects_non_march_fiscal_year_end():
    """A September-30 fiscal year-end yields Sep/Jun/Mar/Dec quarter-ends."""
    facts = [_qfact(m, p) for m in ('revenue', 'pat')
             for p in ('2026-09-30', '2026-06-30', '2026-03-31', '2025-12-31')]
    periods = required_quarterly_periods(facts, NOW)
    assert periods == ('2026-09-30', '2026-06-30', '2026-03-31', '2025-12-31')


def test_required_quarterly_periods_from_two_quarters_walks_back_correctly():
    """Two reported quarters still derive four expected period-ends from the
    fiscal calendar anchored on the latest."""
    facts = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31') for m in ('revenue', 'pat')]
    periods = required_quarterly_periods(facts, NOW)
    assert periods == ('2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30')


def test_quarterly_income_history_gaps_detects_missing_historical_quarters():
    """With only the two most-recent quarters, the two prior calendar quarters
    are flagged as acquisition targets for (revenue, pat)."""
    facts = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31') for m in ('revenue', 'pat')]
    gaps = quarterly_income_history_gaps(facts)
    expected = frozenset(
        FinancialFactKey(INSTRUMENT, metric, p, 'QUARTERLY', 'CONSOLIDATED')
        for p in ('2025-12-31', '2025-09-30') for metric in ('revenue', 'pat'))
    assert gaps == expected


def test_quarterly_income_history_gaps_complete_when_all_four_present():
    facts = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30')
             for m in ('revenue', 'pat')]
    assert quarterly_income_history_gaps(facts) == frozenset()


def test_quarterly_income_history_gaps_missing_single_quarter_only():
    """A single missing quarter reports only that quarter's (revenue, pat) gaps."""
    present = ('2026-06-30', '2026-03-31', '2025-12-31')
    facts = [_qfact(m, p) for p in present for m in ('revenue', 'pat')]
    gaps = quarterly_income_history_gaps(facts)
    expected = frozenset(
        FinancialFactKey(INSTRUMENT, metric, '2025-09-30', 'QUARTERLY', 'CONSOLIDATED')
        for metric in ('revenue', 'pat'))
    assert gaps == expected


def test_quarterly_income_history_gaps_empty_when_no_quarterly_facts():
    assert quarterly_income_history_gaps([]) == frozenset()


def test_quarterly_income_history_gaps_separate_from_annual_requirements():
    """Annual gaps are independent of quarterly gaps — a missing FY2024 annual
    does not appear in quarterly gaps and vice versa."""
    annual_facts = [_fact(m, f'{y}-03-31') for y in (2025, 2026) for m in ('revenue', 'pat')]
    quarterly_facts = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30')
                       for m in ('revenue', 'pat')]
    facts = annual_facts + quarterly_facts
    annual_gaps = annual_income_history_gaps(facts)
    quarterly_gaps = quarterly_income_history_gaps(facts)
    assert {g.period_end for g in annual_gaps} == {'2024-03-31'}
    assert quarterly_gaps == frozenset()


def test_missing_quarterly_fact_keys_through_gap_state():
    """FinancialGapState reports missing quarterly keys when QUARTERLY_FINANCIALS
    inputs are genuinely missing, and stays empty when coverage is complete."""
    complete = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30')
                for m in ('revenue', 'pat')]
    repo, _, _ = _repository(facts=complete)
    state = load_financial_gap_state(repo, INSTRUMENT)
    assert state.missing_quarterly_fact_keys == frozenset()
    partial = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31') for m in ('revenue', 'pat')]
    repo2, _, _ = _repository(facts=partial)
    state2 = load_financial_gap_state(repo2, INSTRUMENT)
    assert state2.missing_quarterly_fact_keys == quarterly_income_history_gaps(partial)
    assert {k.metric for k in state2.missing_quarterly_fact_keys} == {'revenue', 'pat'}
    assert {k.period_end for k in state2.missing_quarterly_fact_keys} == {'2025-12-31', '2025-09-30'}


def test_gap_state_accepts_quarterly_fact_for_missing_period():
    """accepts() admits a historical quarterly fact for an input that readiness
    reports as missing (e.g. QUARTERLY_EPS), while rejecting already-present
    periods or future periods."""
    facts = [_qfact(m, p) for p in ('2026-06-30', '2026-03-31') for m in ('revenue', 'pat')]
    repo, _, _ = _repository(facts=facts)
    state = load_financial_gap_state(repo, INSTRUMENT)
    # QUARTERLY_EPS is missing (no EPS facts) — a historical EPS fact for a
    # required period is accepted:
    assert state.accepts('QUARTERLY_FINANCIALS',
                         _qfact('eps', '2025-12-31', owner='earlier-filing', unit='INR per share'), 'INR')
    # An already-present (revenue, period) key is not re-accepted (no change):
    assert not state.accepts('QUARTERLY_FINANCIALS',
                             _qfact('revenue', '2026-06-30', owner='same-filing'), 'INR')
    # A future quarter (beyond the as-of date) is never accepted:
    assert not state.accepts('QUARTERLY_FINANCIALS',
                             _qfact('eps', '2027-12-31', owner='future-filing', unit='INR per share'), 'INR')
