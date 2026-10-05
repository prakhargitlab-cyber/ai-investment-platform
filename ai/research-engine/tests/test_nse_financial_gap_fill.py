"""NSE-first financial input gaps through real SQLite persistence/readiness."""
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.fact_precedence import FactSourceTier
from app.financial_gap_fill import load_financial_gap_state
from app.research_readiness_runtime import CapabilityExecutionProgress
from app.yahoo_mcp_acquisition import (ExternalMcpAcquisitionError, McpFirstResearchCapabilityExecutor,
                                       YahooMcpNormalizedResult, YahooMcpResultPersister)
from test_nse_structured_financial import setup, NOW, fixed_readiness_clock
from test_research_readiness_runtime import _target


def wire(profile, requirement, facts):
    return YahooMcpNormalizedResult.model_validate({
        'adapterVersion': 'YAHOO_FINANCE_MCP_ADAPTER_V1', 'providerId': 'YAHOO_FINANCE_MCP',
        'sourceTier': 'APPROVED_EXTERNAL_TOOL', 'sourceTool': 'get_financials', 'region': 'INDIA',
        'requirementId': requirement, 'globalInstrumentId': str(profile.instrument_id),
        'symbol': 'GHCL.NS', 'exchange': 'NSE', 'currency': 'INR', 'retrievedAt': NOW,
        'observedAt': NOW, 'sourceUrl': 'https://finance.yahoo.com/quote/GHCL.NS',
        'confidence': .8, 'freshness': 'FRESH', 'financialFacts': facts,
        # Unrelated quote/summary data must not enter persistence in this mode.
        'structuredFacts': [{'metric': 'roe', 'value': 999, 'unit': '%', 'asOf': NOW,
            'sourceUrl': 'https://finance.yahoo.com/quote/GHCL.NS', 'confidence': .8}],
        'marketObservations': [{'observedAt': NOW, 'price': 999, 'currency': 'INR'}],
    })


def fact(metric='eps', value='0', period='2026-06-30', kind='QUARTERLY', basis='STANDALONE', unit=None):
    return {'metric': metric, 'value': value, 'unit': unit or ('INR/share' if metric == 'eps' else 'INR'),
        'periodEnd': period, 'periodType': kind, 'reportingBasis': basis,
        'asOf': period + 'T00:00:00Z', 'sourceUrl': 'https://finance.yahoo.com/quote/GHCL.NS', 'confidence': .8}


async def case(*, omit=(), gateway_failure=None):
    repo, profile, provider, legacy = setup()
    profile = profile.model_copy(update={'provider_instrument_ids': {'NSE': 'GHCL', 'YAHOO_FINANCE': 'GHCL.NS'}})
    repo.profiles = [profile]
    official = await provider.collect(profile)
    calls = []
    async def collect(_profile):
        calls.append('NSE')
        return [f for f in official if f.key.metric not in omit]
    provider.collect = AsyncMock(side_effect=collect)
    async def acquire(_profile, **kwargs):
        calls.append('YAHOO')
        assert kwargs['financial_gap_fill'] is True
        # Evidence is already durable when Yahoo is invoked.
        assert repo.financial_facts_for(profile.instrument_id) or gateway_failure
        if gateway_failure:
            raise gateway_failure
        return wire(profile, kwargs['requirement_id'], [fact(), fact('revenue', '999'),
                    fact('pat', '999'), fact('total_assets', '999')])
    gateway = AsyncMock()
    gateway.acquire_requirement.side_effect = acquire
    executor = McpFirstResearchCapabilityExecutor(legacy, repo, gateway, enabled=True)
    return repo, profile, official, provider, gateway, executor, calls


async def execute(executor, profile, *requirements, progress=None):
    return await executor.execute_primary(profile.instrument_id, [_target(r) for r in requirements],
        jurisdiction='INDIA', correlation_id='financial-gap-test', identity_headers=None, progress=progress)


@pytest.mark.asyncio
async def test_nse_complete_means_no_yahoo_and_warm_evidence_reuses_both():
    repo, profile, _, provider, gateway, executor, calls = await case()
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    assert outcome.satisfied_requirement_ids == ('QUARTERLY_FINANCIALS',)
    assert not outcome.failures
    gateway.acquire_requirement.assert_not_awaited()
    repo.refresh_targeted_categories.assert_not_awaited()
    await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    await executor.execute_primary(profile.instrument_id,
        [replace(_target('QUARTERLY_FINANCIALS'), reason='READY_FRESH')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    provider.collect.assert_awaited_once()
    assert calls == ['NSE']


@pytest.mark.asyncio
async def test_partial_nse_then_only_missing_eps_persists_and_zero_is_useful():
    repo, profile, official, _, _, executor, calls = await case(omit={'eps'})
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    assert calls == ['NSE', 'YAHOO']
    assert not outcome.failures
    assert outcome.satisfied_requirement_ids == ('QUARTERLY_FINANCIALS',)
    stored = repo.financial_facts_for(profile.instrument_id)
    yahoo = [f for f in stored if f.source_tier == FactSourceTier.YAHOO]
    assert [(f.key.metric, f.value.value) for f in yahoo] == [('eps', Decimal(0))]
    for metric in ('revenue', 'pat'):
        nse = next(f for f in official if f.key.metric == metric and f.key.period_end == '2026-06-30')
        actual = next(f for f in stored if f.key == nse.key)
        assert actual.source_tier == FactSourceTier.OFFICIAL_NSE and actual.value.value == nse.value.value
    assert not repo.structured_market_snapshots_for({profile.instrument_id}).get(profile.instrument_id)
    assert not repo.market_price_observations_for({profile.instrument_id}).get(profile.instrument_id)
    repo.refresh_targeted_categories.assert_not_awaited()


@pytest.mark.asyncio
async def test_official_zero_is_covered_and_never_requests_fallback():
    repo, profile, official, provider, gateway, executor, _ = await case()
    provider.collect.side_effect = None
    provider.collect.return_value = [replace(f, value=f.value.model_copy(update={'value': Decimal(0)}))
                                     if f.key.metric == 'eps' else f for f in official]
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    assert not outcome.failures
    gateway.acquire_requirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_period_basis_unit_and_key_are_preserved_for_partial_history():
    repo, profile, official, _, _, _, _ = await case()
    one = [f for f in official if f.key.period_type == 'QUARTERLY' and f.key.period_end == '2026-06-30']
    await repo.persist_international_financial_facts_async(one)
    state = load_financial_gap_state(repo, profile.instrument_id)
    result = wire(profile, 'GROWTH_FACTS', [
        fact('revenue', '999'),  # existing exact key
        fact('revenue', '10', period='2026-03-31'),
        fact('revenue', '11', period='2026-03-31', basis='CONSOLIDATED'),
        fact('revenue', '12', period='2026-03-31', unit='USD'),
        fact('eps', '2', period='2026-03-31', unit='INR per share'),
        fact('eps', '3', period='2026-03-31', kind='AS_AT'),
    ])
    await YahooMcpResultPersister(repo).persist(result, profile, financial_gaps=state)
    added = [f for f in repo.financial_facts_for(profile.instrument_id) if f.source_tier == FactSourceTier.YAHOO]
    assert {(f.key.metric, f.key.period_end, f.key.period_type, f.key.reporting_basis, f.value.unit)
            for f in added} == {('revenue', '2026-03-31', 'QUARTERLY', 'STANDALONE', 'INR'),
                               ('eps', '2026-03-31', 'QUARTERLY', 'STANDALONE', 'INR per share')}


@pytest.mark.asyncio
async def test_period_labelled_roe_can_join_nse_revenue_pat_and_yahoo_eps():
    repo, profile, _, _, _, executor, _ = await case(omit={'eps', 'total_equity'})
    await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    state = load_financial_gap_state(repo, profile.instrument_id)
    assert 'ROE' in state.supported_inputs('BUSINESS_QUALITY_FACTS')
    await YahooMcpResultPersister(repo).persist(wire(profile, 'BUSINESS_QUALITY_FACTS', [
        fact('roe', '12', period='2026-03-31', kind='ANNUAL', unit='%'), fact('pat', '999')]),
        profile, financial_gaps=state)
    stored = repo.financial_facts_for(profile.instrument_id)
    assert {f.key.metric for f in stored if f.source_tier == FactSourceTier.YAHOO} == {'eps', 'roe'}
    assert {'revenue', 'pat'} <= {f.key.metric for f in stored if f.source_tier == FactSourceTier.OFFICIAL_NSE}


@pytest.mark.asyncio
async def test_nse_failure_cleared_only_after_actual_yahoo_readiness_and_remains_observable():
    repo, profile, _, provider, gateway, executor, _ = await case()
    provider.collect.side_effect = TimeoutError()
    gateway.acquire_requirement.side_effect = None
    gateway.acquire_requirement.return_value = wire(profile, 'QUARTERLY_FINANCIALS', [
        fact(metric, '1', period=period) for period in ('2026-06-30', '2026-03-31')
        for metric in ('revenue', 'pat', 'eps')])
    progress = CapabilityExecutionProgress()
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS', progress=progress)
    assert outcome.failures == {}
    assert outcome.satisfied_requirement_ids == ('QUARTERLY_FINANCIALS',)
    assert progress.failures['QUARTERLY_FINANCIALS'] == 'TimeoutError'
    assert any(r['provider'] == 'NSE' and r['failure_reason'] == 'TimeoutError'
               for r in repo.acquisition_observations_for(profile.instrument_id))


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['EXTERNAL_CAPABILITY_UNSUPPORTED', 'EXTERNAL_RESULT_INCOMPLETE', 'DOWNSTREAM_TIMEOUT'])
async def test_yahoo_failure_cleared_after_existing_fallback_satisfies_and_observation_survives(reason):
    repo, profile, official, _, _, executor, calls = await case(omit={'eps'},
        gateway_failure=ExternalMcpAcquisitionError(reason))
    async def documents(*_args, **_kwargs):
        calls.append('EXISTING_FALLBACK')
        await repo.persist_international_financial_facts_async(official)
    repo.refresh_targeted_categories.side_effect = documents
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    assert calls == ['NSE', 'YAHOO', 'EXISTING_FALLBACK']
    assert not outcome.failures
    assert any(r['failure_reason'] == reason for r in repo.acquisition_observations_for(profile.instrument_id))


@pytest.mark.asyncio
async def test_missing_mandatory_facts_remain_incomplete_and_transient_failure_retryable():
    from app.failure_taxonomy import classify_requirement_failures, TECHNICAL_RETRYABLE
    repo, profile, _, _, _, executor, _ = await case(omit={'eps'},
        gateway_failure=ExternalMcpAcquisitionError('DOWNSTREAM_TIMEOUT'))
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    assert not outcome.satisfied_requirement_ids
    assert outcome.failures['QUARTERLY_FINANCIALS'] == 'DOWNSTREAM_TIMEOUT'
    assert classify_requirement_failures(outcome.failures) == TECHNICAL_RETRYABLE
    repo.refresh_targeted_categories.assert_awaited_once()


@pytest.mark.asyncio
async def test_financial_gap_fill_empty_yahoo_result_is_qualified_permanent_not_retried():
    """Root Cause A, extended: _execute_financial_gaps (the PRIMARY acquisition
    path for QUARTERLY_FINANCIALS/GROWTH_FACTS/BUSINESS_QUALITY_FACTS/
    BALANCE_SHEET_FACTS) is a SECOND producer of bare EXTERNAL_RESULT_INCOMPLETE
    distinct from execute_primary's own two producer sites. A Yahoo call that
    completed and persisted zero facts here must be qualified to the
    permanent PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS reason -- the same
    deterministic-absence outcome execute_primary already recognizes -- so
    classify_requirement_failures correctly reports EVIDENCE_UNAVAILABLE
    (no repair) instead of TECHNICAL_RETRYABLE (repair keeps re-attempting a
    provider that already proved, this same cycle, that it has no facts)."""
    from app.failure_taxonomy import classify_requirement_failures, EVIDENCE_UNAVAILABLE
    repo, profile, _, _, _, executor, _ = await case(omit={'eps'},
        gateway_failure=ExternalMcpAcquisitionError('EXTERNAL_RESULT_INCOMPLETE'))
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    assert not outcome.satisfied_requirement_ids
    assert outcome.failures['QUARTERLY_FINANCIALS'] == 'PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS'
    assert classify_requirement_failures(outcome.failures) == EVIDENCE_UNAVAILABLE
    # The audit trail must still record what actually happened, unqualified.
    assert any(r['failure_reason'] == 'EXTERNAL_RESULT_INCOMPLETE'
               for r in repo.acquisition_observations_for(profile.instrument_id))


@pytest.mark.asyncio
async def test_runtime_fallback_cannot_reacquire_a_whole_financial_category():
    repo, profile, _, _, _, executor, _ = await case(omit={'eps'})
    executor.legacy_executor.execute_approved_fallbacks = AsyncMock()
    outcome = await executor.execute_approved_fallbacks(profile.instrument_id, [_target('QUARTERLY_FINANCIALS')])
    assert not outcome.executed_capabilities
    executor.legacy_executor.execute_approved_fallbacks.assert_not_awaited()


@pytest.mark.asyncio
async def test_useful_partial_persists_but_incomplete_requirement_is_not_certified():
    repo, profile, _, provider, gateway, executor, _ = await case(omit={'eps', 'pat'})
    # One current revenue quarter cannot prove comparable earnings history.
    original = provider.collect.side_effect
    async def one_quarter(item):
        return [f for f in await original(item) if f.key.period_end == '2026-06-30']
    provider.collect.side_effect = one_quarter
    gateway.acquire_requirement.side_effect = None
    gateway.acquire_requirement.return_value = wire(profile, 'QUARTERLY_FINANCIALS', [fact()])
    outcome = await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    assert not outcome.satisfied_requirement_ids
    assert any(f.key.metric == 'eps' and f.value.value == 0 for f in repo.financial_facts_for(profile.instrument_id))
    repo.refresh_targeted_categories.assert_awaited_once()


@pytest.mark.asyncio
async def test_authority_guard_still_rejects_yahoo_even_outside_gap_filter():
    repo, profile, official, _, _, _, _ = await case()
    await repo.persist_international_financial_facts_async(official)
    nse = next(f for f in official if f.key.metric == 'revenue' and f.key.period_end == '2026-06-30')
    await YahooMcpResultPersister(repo).persist(wire(profile, 'QUARTERLY_FINANCIALS', [fact('revenue', '999')]), profile)
    saved = next(f for f in repo.financial_facts_for(profile.instrument_id) if f.key == nse.key)
    assert saved.source_tier == FactSourceTier.OFFICIAL_NSE and saved.value.value == nse.value.value


@pytest.mark.asyncio
async def test_stale_eps_is_a_gap_but_prior_period_is_never_replaced():
    from datetime import datetime, timezone
    repo, profile, official, _, _, executor, _ = await case(omit={'eps'})
    old = next(f for f in official if f.key.metric == 'eps' and f.key.period_type == 'QUARTERLY')
    old = replace(old, key=replace(old.key, period_end='2024-06-30'),
        value=old.value.model_copy(update={'as_of_date': datetime(2024, 6, 30, tzinfo=timezone.utc)}))
    await repo.persist_international_financial_facts_async([old])
    # Use precisely the same unit within the EPS series.
    executor.gateway.acquire_requirement.side_effect = None
    executor.gateway.acquire_requirement.return_value = wire(profile, 'QUARTERLY_FINANCIALS', [
        fact(unit=old.value.unit)])
    await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    executor.gateway.acquire_requirement.assert_awaited_once()
    stored = repo.financial_facts_for(profile.instrument_id)
    assert next(f for f in stored if f.key == old.key).source_tier == FactSourceTier.OFFICIAL_NSE
    assert any(f.key.metric == 'eps' and f.key.period_end == '2026-06-30'
               and f.source_tier == FactSourceTier.YAHOO for f in stored)


@pytest.mark.asyncio
async def test_cancellation_during_nse_does_not_start_yahoo_or_documents():
    import asyncio
    repo, profile, _, provider, gateway, executor, _ = await case()
    provider.collect.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await execute(executor, profile, 'QUARTERLY_FINANCIALS')
    gateway.acquire_requirement.assert_not_awaited()
    repo.refresh_targeted_categories.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_same_key_units_cannot_be_merged_by_response_order():
    repo, profile, _, _, _, _, _ = await case(omit={'eps'})
    state = load_financial_gap_state(repo, profile.instrument_id)
    with pytest.raises(ExternalMcpAcquisitionError, match='EXTERNAL_RESULT_INCOMPLETE'):
        await YahooMcpResultPersister(repo).persist(wire(profile, 'QUARTERLY_FINANCIALS', [
            fact(unit='INR/share'), fact(unit='INR per share')]), profile, financial_gaps=state)
    assert not repo.financial_facts_for(profile.instrument_id)


@pytest.mark.asyncio
async def test_generic_failure_reverification_uses_actual_fallback_evidence():
    from app.research_readiness_runtime import CapabilityExecutionResult
    repo, profile, _, _, gateway, executor, _ = await case()
    gateway.acquire_requirement.side_effect = ExternalMcpAcquisitionError('EXTERNAL_CAPABILITY_UNSUPPORTED')
    async def fallback(*_args, **_kwargs):
        await YahooMcpResultPersister(repo).persist(wire(profile, 'LATEST_PRICE', []), profile)
        return CapabilityExecutionResult(('STRUCTURED_MARKET',))
    executor.legacy_executor.execute_primary = AsyncMock(side_effect=fallback)
    outcome = await execute(executor, profile, 'LATEST_PRICE')
    assert not outcome.failures
    assert any(r['failure_reason'] == 'EXTERNAL_CAPABILITY_UNSUPPORTED'
               for r in repo.acquisition_observations_for(profile.instrument_id))
