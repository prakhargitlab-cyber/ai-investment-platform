"""Verified NSE fixtures through the real financial persistence/readiness path."""
import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _SINGLETON_MAX_DOCUMENTS, acquisition_budget, investigate
from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_requirement_failures
from app.research_readiness_runtime import (ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter,
                                            ResearchReadinessRuntime, TargetedEnsureResult)
from app.research_readiness import ResearchReadinessService, ResearchRequirementStatus
from app.structured_financial import NseOfficialFinancialProvider, parse_nse_financial_xbrl
from test_di15_financial_authority_upgrade import _repo
from test_research_readiness_runtime import _target
from test_stock_rule_engine import _readiness

FIXTURES = Path(__file__).parent / 'fixtures' / 'nse_financial_xbrl'
ROWS = json.loads((FIXTURES / 'filings.json').read_text())
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_readiness_clock(monkeypatch):
    # Captured filing periods stay real; replay readiness at the capture date.
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)
    monkeypatch.setattr('app.research_readiness.datetime', FixedDatetime)
    monkeypatch.setattr('app.research_readiness_runtime.datetime', FixedDatetime)


def setup():
    repo, profile = _repo()
    profile = profile.model_copy(update={'isin': 'INE539A01019', 'provider_instrument_ids': {'NSE': 'GHCL'}})
    repo.profiles = [profile]
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={'data': ROWS},
                             request=httpx.Request('GET', NseOfficialFinancialProvider.RESULTS_URL))))
    xml = {ROWS[0]['xbrl']: (FIXTURES / 'ghcl_20260630.xml').read_bytes(),
           ROWS[1]['xbrl']: (FIXTURES / 'ghcl_20260331.xml').read_bytes()}
    async def fetch(url, **kwargs):
        return SimpleNamespace(final_url=url, status_code=200, content_type='application/xml', content=xml[url])
    fetcher = SimpleNamespace(settings=repo.settings, fetch_network=AsyncMock(side_effect=fetch))
    provider = NseOfficialFinancialProvider(client, fetcher, clock=lambda: NOW)
    repo.refresh_targeted_categories = AsyncMock()
    executor = ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), None, official_financial_provider=provider)
    return repo, profile, provider, executor


@pytest.mark.asyncio
async def test_verified_structured_quarters_persist_and_satisfy_readiness_without_pdf():
    repo, profile, provider, executor = setup()
    outcome = await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    assert not outcome.failures
    repo.refresh_targeted_categories.assert_not_awaited()
    actual = ResearchReadinessService(RepositoryResearchReadinessAdapter(repo)).assess(
        profile.instrument_id, jurisdiction='INDIA', now=NOW)
    assert actual.for_requirement('QUARTERLY_FINANCIALS').status == 'READY_FRESH'
    facts = repo.financial_facts_for(profile.instrument_id)
    revenue = next(f for f in facts if f.key.metric == 'revenue' and f.key.period_end == '2026-06-30')
    assert revenue.value.value == Decimal('7742600000') and revenue.value.unit == 'INR'
    assert revenue.source_provider == 'NSE' and revenue.source_identity == ROWS[0]['xbrl']
    assert revenue.key.reporting_basis == 'STANDALONE'
    assert any(f.key.period_type == 'ANNUAL' for f in facts)
    assert any(f.key.metric == 'total_equity' and f.key.period_type == 'AS_AT' for f in facts)
    # This small verified feed does not manufacture the remaining balance/quality inputs.
    assert actual.for_requirement('BALANCE_SHEET_FACTS').status != 'READY_FRESH'
    provider.client.get.reset_mock()
    provider.fetcher.fetch_network.reset_mock()
    runtime = ResearchReadinessRuntime(repo, RepositoryResearchReadinessAdapter(repo), executor)
    warm = await runtime.ensure(profile.instrument_id, jurisdiction='INDIA',
                                requirement_ids=['QUARTERLY_FINANCIALS'], wait_for_completion=True)
    assert not warm.planned_requirement_ids
    provider.client.get.assert_not_awaited()
    provider.fetcher.fetch_network.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_financial_coverage_still_uses_pdf_fallback():
    repo, profile, provider, executor = setup()
    provider.client.get.return_value = httpx.Response(200, json={'data': [ROWS[0]]},
        request=httpx.Request('GET', provider.RESULTS_URL))
    await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS'), _target('GROWTH_FACTS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    repo.refresh_targeted_categories.assert_awaited_once()
    assert repo.refresh_targeted_categories.call_args.args[1] == {'FINANCIAL_RESULTS'}
    assert repo.financial_facts_for(profile.instrument_id)


@pytest.mark.asyncio
async def test_structured_growth_uses_durable_prior_year_comparison_without_pdf():
    repo, profile, provider, executor = setup()
    # Materialize this year's (2026-03-31) annual facts through the real
    # structured provider first, so the synthetic prior-year fact below is
    # built in the exact shape (reporting_basis/unit/source identity) the
    # official feed actually produces -- not a hand-invented one that could
    # silently stop matching production's own fact shape.
    setup_outcome = await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    assert not setup_outcome.failures
    current_annual = [fact for fact in repo.financial_facts_for(profile.instrument_id)
                      if fact.key.period_type == 'ANNUAL' and fact.key.period_end == '2026-03-31'
                      and fact.key.metric in {'revenue', 'pat', 'eps'}]
    assert current_annual
    # Durable prior-year (2025-03-31) official history, derived from this
    # year's real facts: same metric/basis/unit, a distinct period and a
    # genuinely different value (so this is a real YoY comparison, not a
    # duplicate), and a distinct source_identity marking it as already
    # persisted from an earlier filing/cycle -- not something this cycle's
    # fetch produced.
    prior = [replace(fact,
                      key=replace(fact.key, period_end='2025-03-31'),
                      value=fact.value.model_copy(update={'period': '2025-03-31',
                          'value': fact.value.value * Decimal('0.85')}),
                      source_identity=fact.source_identity + ':DURABLE_PRIOR_FY')
             for fact in current_annual]
    await repo.persist_international_financial_facts_async(prior)

    # Retrieving current results again (e.g. a later cycle) must reuse the
    # already-durable current+prior evidence, not fabricate a second annual
    # comparison or invoke the PDF fallback.
    repo.refresh_targeted_categories.reset_mock()
    provider.client.get.reset_mock()
    provider.fetcher.fetch_network.reset_mock()
    outcome = await executor.execute_primary(profile.instrument_id,
        [_target('GROWTH_FACTS'), _target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    assert not outcome.failures
    repo.refresh_targeted_categories.assert_not_awaited()
    readiness = ResearchReadinessService(RepositoryResearchReadinessAdapter(repo)).assess(
        profile.instrument_id, jurisdiction='INDIA', now=NOW)
    assert readiness.for_requirement('GROWTH_FACTS').status == 'READY_FRESH'


@pytest.mark.asyncio
async def test_narrative_requirements_keep_document_route_after_structured_financial_success():
    repo, profile, _, executor = setup()
    await executor.execute_primary(profile.instrument_id,
        [_target('QUARTERLY_FINANCIALS'), _target('GOVERNANCE_HISTORY'), _target('ORDER_BOOK_CAPEX_GUIDANCE')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    categories = repo.refresh_targeted_categories.call_args.args[1]
    assert 'FINANCIAL_RESULTS' not in categories
    assert {'RISKS', 'REGULATORY', 'MANAGEMENT', 'CAPEX', 'ORDERS_BACKLOG'} <= categories


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [TimeoutError(), httpx.ReadTimeout('timeout'), httpx.ConnectError('offline')])
async def test_structured_transient_failure_survives_empty_pdf_fallback_as_retryable(failure):
    repo, profile, provider, executor = setup()
    provider.client.get.side_effect = failure
    outcome = await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    repo.refresh_targeted_categories.assert_awaited_once()
    assert classify_requirement_failures(outcome.failures) == TECHNICAL_RETRYABLE
    assert not repo.financial_facts_for(profile.instrument_id)


@pytest.mark.asyncio
async def test_deep_finalization_preserves_structured_timeout_over_pdf_no_supported_facts():
    repo, profile, provider, executor = setup()
    provider.client.get.side_effect = TimeoutError()
    readiness = _readiness({'QUARTERLY_FINANCIALS': ResearchRequirementStatus.MISSING})
    async def pdf_fallback(key, *args, **kwargs):
        acquisition_budget(key).failures.append('PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS')
    repo.refresh_targeted_categories.side_effect = pdf_fallback
    async def ensure(key, *, requirement_ids, **kwargs):
        result = await executor.execute_primary(profile.instrument_id, [_target(r) for r in requirement_ids],
            jurisdiction='INDIA', correlation_id=None, identity_headers=None)
        return TargetedEnsureResult(readiness, tuple(requirement_ids), result.executed_capabilities,
                                    failures=result.failures)
    runtime = SimpleNamespace(read=AsyncMock(return_value=readiness), ensure=ensure, repository=repo)
    _, _, matrix = await investigate(runtime, profile.instrument_id, jurisdiction='INDIA')
    assert matrix['QUARTERLY_FINANCIALS']['failure'] == 'TimeoutError'
    assert classify_requirement_failures({'QUARTERLY_FINANCIALS': matrix['QUARTERLY_FINANCIALS']['failure']}) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_structured_cancellation_propagates_without_pdf_fallback():
    repo, profile, provider, executor = setup()
    provider.client.get.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')],
            jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    repo.refresh_targeted_categories.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_official_pdf_fallback_clears_structured_timeout():
    repo, profile, provider, executor = setup()
    facts = await provider.collect(profile)
    provider.client.get.side_effect = TimeoutError()
    async def fallback(*args, **kwargs):
        await repo.persist_international_financial_facts_async(facts)
    repo.refresh_targeted_categories.side_effect = fallback
    outcome = await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    assert not outcome.failures


@pytest.mark.parametrize('old,new', [
    (b'>GHCL<', b'>OTHER<'), (b'>INE539A01019<', b'>INE000000000<'),
    (b'>Standalone<', b'>Consolidated<'),
])
def test_xml_identity_and_basis_conflicts_fail_closed(old, new):
    _, profile, _, _ = setup()
    xml = (FIXTURES / 'ghcl_20260630.xml').read_bytes().replace(old, new)
    with pytest.raises(ValueError):
        parse_nse_financial_xbrl(xml, profile, ROWS[0], retrieved_at=NOW)


def test_unsupported_taxonomy_nil_wrong_units_and_ytd_are_never_normalized_as_valid_quarters():
    _, profile, _, _ = setup()
    xml = (FIXTURES / 'ghcl_20260630.xml').read_bytes()
    assert not parse_nse_financial_xbrl(xml.replace(b'2026-01-31/in-capmkt', b'2099-01-01/in-capmkt'),
                                      profile, ROWS[0], retrieved_at=NOW)
    assert not parse_nse_financial_xbrl(xml.replace(b'unitRef="INR"', b'unitRef="bad"').replace(
        b'unitRef="INRPerShare"', b'unitRef="bad"'), profile, ROWS[0], retrieved_at=NOW)
    assert not parse_nse_financial_xbrl(xml.replace(b'2026-04-01', b'2026-01-01'),
                                      profile, ROWS[0], retrieved_at=NOW)
    nil_xml = xml.replace(b'<xbrli:xbrl', b'<xbrli:xbrl xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
    assert not parse_nse_financial_xbrl(nil_xml.replace(b'unitRef=', b'xsi:nil="true" unitRef='),
                                      profile, ROWS[0], retrieved_at=NOW)


def test_conflicting_duplicate_fact_is_omitted_and_eps_retains_explicit_total_diluted_tag():
    _, profile, _, _ = setup()
    xml = (FIXTURES / 'ghcl_20260630.xml').read_bytes()
    duplicate = re.search(rb'<in-capmkt:RevenueFromOperations\b.*?</in-capmkt:RevenueFromOperations>', xml).group()
    conflicting = duplicate.replace(b'>7742600000<', b'>123<')
    facts = parse_nse_financial_xbrl(xml.replace(b'</xbrli:xbrl>', conflicting + b'</xbrli:xbrl>'),
                                    profile, ROWS[0], retrieved_at=NOW)
    assert not any(f.key.metric == 'revenue' for f in facts)
    eps = next(f for f in facts if f.key.metric == 'eps')
    assert eps.value.value == Decimal('21.04') and eps.value.unit == 'INR per share'
    assert 'DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations' in eps.value.calculation_basis
    assert eps.value.published_at.isoformat() == '2026-08-01T16:04:06+05:30'


@pytest.mark.asyncio
async def test_revision_is_unsupported_and_keeps_financial_pdf_fallback():
    repo, profile, provider, executor = setup()
    provider.client.get.return_value = httpx.Response(200,
        json={'data': [{**ROWS[0], 'revised_Date': '02-Aug-2026 12:00:00'}]},
        request=httpx.Request('GET', provider.RESULTS_URL))
    await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    provider.fetcher.fetch_network.assert_not_awaited()
    repo.refresh_targeted_categories.assert_awaited_once()
    assert not repo.financial_facts_for(profile.instrument_id)


def test_parse_rejects_missing_quarter_end_or_broadcast_date_without_typeerror():
    _, profile, _, _ = setup()
    xml = (FIXTURES / 'ghcl_20260630.xml').read_bytes()
    for mutated in ({**ROWS[0], 'qe_Date': None}, {**ROWS[0], 'broadcast_Date': None}):
        with pytest.raises(ValueError, match='INVALID_DATE'):
            parse_nse_financial_xbrl(xml, profile, mutated, retrieved_at=NOW)


@pytest.mark.asyncio
async def test_collect_skips_row_with_null_quarter_end_or_broadcast_date(monkeypatch):
    # NSE's integrated-filing-results endpoint can return a provisional row
    # for the requested symbol/type/xbrl-shape with qe_Date or broadcast_Date
    # set to null (e.g. a pending/withdrawn entry). Before this fix, collect()
    # indexed row["qe_Date"]/row["broadcast_Date"] unconditionally and raised
    # TypeError (strptime() argument 1 must be str, not NoneType) -- an
    # optional-provider failure that was swallowed down to a bare
    # "reason=TypeError" with no further detail. The row must instead be
    # skipped like any other unsupported row, leaving the remaining valid
    # rows (or the existing PDF fallback, if none remain) to satisfy evidence.
    _, profile, provider, _ = setup()
    null_qe_date_row = {**ROWS[0], 'qe_Date': None}
    provider.client.get.return_value = httpx.Response(200,
        json={'data': [null_qe_date_row, ROWS[1]]},
        request=httpx.Request('GET', provider.RESULTS_URL))
    facts = await provider.collect(profile)
    assert provider.fetcher.fetch_network.await_count == 1
    assert provider.fetcher.fetch_network.await_args.args[0] == ROWS[1]['xbrl']
    assert facts and all(f.key.period_end == '2026-03-31' for f in facts)


@pytest.mark.asyncio
async def test_collect_returns_empty_when_every_row_has_null_dates(monkeypatch):
    _, profile, provider, _ = setup()
    rows = [{**ROWS[0], 'qe_Date': None}, {**ROWS[1], 'broadcast_Date': None}]
    provider.client.get.return_value = httpx.Response(200, json={'data': rows},
        request=httpx.Request('GET', provider.RESULTS_URL))
    assert await provider.collect(profile) == []
    provider.fetcher.fetch_network.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_filing_date_falls_back_to_pdf_without_cycle_crashing():
    repo, profile, provider, executor = setup()
    provider.client.get.return_value = httpx.Response(200,
        json={'data': [{**ROWS[0], 'qe_Date': None}, {**ROWS[1], 'qe_Date': None}]},
        request=httpx.Request('GET', provider.RESULTS_URL))
    await executor.execute_primary(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    provider.fetcher.fetch_network.assert_not_awaited()
    repo.refresh_targeted_categories.assert_awaited_once()
    assert not repo.financial_facts_for(profile.instrument_id)


@pytest.mark.asyncio
async def test_xml_collection_is_bounded_and_uses_verified_query_contract(monkeypatch):
    _, profile, provider, _ = setup()
    rows = [{**ROWS[0], 'qe_Date': f'30-Jun-{year}'} for year in range(2026, 2016, -1)]
    provider.client.get.return_value = httpx.Response(200, json={'data': rows},
        request=httpx.Request('GET', provider.RESULTS_URL))
    monkeypatch.setattr('app.structured_financial.parse_nse_financial_xbrl', lambda *a, **k: [])
    assert not await provider.collect(profile)
    assert provider.fetcher.fetch_network.await_count == 4
    assert provider.client.get.call_args.kwargs['params'] == {
        'symbol': 'GHCL', 'type': 'Integrated Filing- Financials', 'page': 1, 'size': 20}


def test_dtd_and_duplicate_contexts_fail_closed():
    _, profile, _, _ = setup()
    xml = (FIXTURES / 'ghcl_20260630.xml').read_bytes()
    with pytest.raises(ValueError, match='UNSAFE_XML'):
        parse_nse_financial_xbrl(b'<!DOCTYPE xbrl>' + xml, profile, ROWS[0], retrieved_at=NOW)
    context = re.search(rb'<xbrli:context\b.*?</xbrli:context>', xml).group()
    with pytest.raises(ValueError, match='DUPLICATE_CONTEXT'):
        parse_nse_financial_xbrl(xml.replace(b'</xbrli:xbrl>', context + b'</xbrli:xbrl>'),
                                profile, ROWS[0], retrieved_at=NOW)


@pytest.mark.asyncio
async def test_pdf_response_to_xml_request_never_enters_pdf_parser():
    repo, profile, provider, executor = setup()
    provider.fetcher.fetch_network.side_effect = None
    provider.fetcher.fetch_network.return_value = SimpleNamespace(final_url=ROWS[0]['xbrl'],
        status_code=200, content_type='application/pdf', content=b'%PDF')
    with pytest.raises(ValueError, match='INVALID_RESPONSE'):
        await provider.collect(profile)
    assert _SINGLETON_MAX_DOCUMENTS == RequirementAcquisitionBudget.__dataclass_fields__['max_documents'].default == 4


@pytest.mark.asyncio
async def test_disabled_live_research_never_contacts_official_feed():
    _, profile, provider, _ = setup()
    provider.fetcher.settings.research_live_enabled = False
    assert not await provider.collect(profile)
    provider.client.get.assert_not_awaited()
    provider.fetcher.fetch_network.assert_not_awaited()


def test_production_reuses_existing_clients_and_activates_provider():
    import app.main as main
    provider = main.existing_research_capability_executor.official_financial_provider
    assert isinstance(provider, NseOfficialFinancialProvider)
    assert provider.client is main.repository._official_filing_discovery.client
    assert provider.fetcher is main.repository._fetcher
