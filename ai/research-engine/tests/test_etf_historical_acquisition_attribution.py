"""Regression tests for Task 2 of the Full NSE ETF Radar historical
identity validation / failure-attribution correction: the ETF_ONBOARD
phase's own persisted checkpoint outcome for each instrument must be
propagated into the published candidate as an additive
``historical_acquisition`` diagnostics field, WITHOUT changing the
recommendation or scoring formulas, and WITHOUT ever turning a historical
failure into a successful evaluation.

Covers Task 3 scenarios 7-9 (scenario 10 -- existing request budgets,
checkpoint recovery and rate limiting remain intact -- is covered by the
pre-existing tests in test_etf_full_universe.py, re-run unmodified as a
non-regression check rather than duplicated here).
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.etf_full_universe import run_full_etf_cycle, _ensure_etf_history
from app.models import MarketPriceObservation
from test_etf_ingestion import repo, META, NOW
from test_etf_full_universe import universe, checkpoint
from test_etf_preflight import compatible_dispatch


def _full_coverage_handler(rows, csv):
    def handler(request):
        if request.url.path.endswith('.csv'):
            return httpx.Response(200, text=csv)
        if 'marketWatchApi' in request.url.path:
            return httpx.Response(200, json={'data': {'timestamp': NOW.strftime('%d-%b-%Y %H:%M:%S'),
                'navDate': NOW.strftime('%d-%b-%Y'), 'data': [dict(symbol=r['primarySymbol'], series='EQ',
                identifier=r['primarySymbol']+'EQN', lastPrice=100, totalTradedVolume=500000, nav=100) for r in rows]}})
        symbol = request.url.params['symbol']
        row = next(r for r in rows if r['primarySymbol'] == symbol)
        return httpx.Response(200, json=dict(symbol=symbol, isin=row['isin'], isETFSec='true', isDelisted='false',
            isSuspended='false', activeSeries=['EQ'], marketType='N'))
    return handler


@pytest.mark.asyncio
async def test_scenario7_historical_persistence_uses_the_canonical_etf_uuid(repo):
    """Two distinct ETFs in the same cycle must each see their OWN
    ETF_ONBOARD outcome reflected in their own candidate's
    historical_acquisition field -- never swapped or cross-attributed.
    checkpoint.row('ETF_ONBOARD', item['isin']) is keyed by each
    instrument's own canonical ISIN, which the final-evaluation loop reads
    per-item from the SAME canonical row used for scoring, so a correctly-
    keyed lookup can never attribute one instrument's history outcome to
    another's canonical UUID."""
    rows, csv = universe(2)
    isin_a, isin_b = rows[0]['isin'], rows[1]['isin']

    async def history_acquirer(repository, row, *, now, checkpoint):
        if row['isin'] == isin_a:
            result = {'status': 'TECHNICAL_FAILURE', 'provider': 'YAHOO_FINANCE',
                      'reason': 'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH', 'observations': 0}
            await checkpoint.start('ETF_ONBOARD', row['isin'])
            await checkpoint.finish('ETF_ONBOARD', row['isin'],
                __import__('app.cycle_checkpoint', fromlist=['CandidateState']).CandidateState.RETRYABLE_FAILURE,
                disposition=result['status'], payload=result)
            return result
        return await _ensure_etf_history(repository, row, now=now, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))

    cp = await checkpoint(repo)
    source = SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_full_coverage_handler(rows, csv))) as client:
        result = await run_full_etf_cycle(repo, source, checkpoint=cp, cycle_id=cp.cycle_id,
            identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
            history_acquirer=history_acquirer)
    saved = repo.persistence.etf_radar_cycle(cp.cycle_id)
    by_instrument = {c['global_instrument_id']: c for c in saved['candidates']}
    candidate_a = by_instrument[rows[0]['globalInstrumentId']]
    candidate_b = by_instrument[rows[1]['globalInstrumentId']]
    assert candidate_a['historical_acquisition']['status'] == 'IDENTITY_MISMATCH'
    assert candidate_a['historical_acquisition']['reason'] == 'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH'
    # The second, genuinely-unrelated instrument must NOT inherit the
    # first's identity-mismatch outcome.
    assert candidate_b['historical_acquisition']['status'] != 'IDENTITY_MISMATCH'
    assert candidate_b['historical_acquisition']['status'] == 'INSUFFICIENT_TRADING_HISTORY'


@pytest.mark.asyncio
async def test_scenario8_final_candidate_exposes_actual_etf_onboard_failure_reason(repo):
    """A genuine ETF_ONBOARD historical-identity-mismatch failure must be
    visible on the published candidate as its own specific status/reason --
    not collapsed into a generic technical failure, and the additive field
    must never change the candidate's recommendation or disposition (the
    candidate is still fully EVALUATED; only its historical-acquisition
    diagnostics changes)."""
    rows, csv = universe(1)

    async def history_acquirer(repository, row, *, now, checkpoint):
        from app.cycle_checkpoint import CandidateState
        result = {'status': 'TECHNICAL_FAILURE', 'provider': 'YAHOO_FINANCE',
                  'reason': 'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH', 'observations': 0}
        await checkpoint.start('ETF_ONBOARD', row['isin'])
        await checkpoint.finish('ETF_ONBOARD', row['isin'], CandidateState.RETRYABLE_FAILURE,
            disposition=result['status'], payload=result)
        return result

    cp = await checkpoint(repo)
    source = SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_full_coverage_handler(rows, csv))) as client:
        result = await run_full_etf_cycle(repo, source, checkpoint=cp, cycle_id=cp.cycle_id,
            identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
            history_acquirer=history_acquirer)
    saved = repo.persistence.etf_radar_cycle(cp.cycle_id)
    candidate = saved['candidates'][0]
    # This is a BASELINE-successful, ETF_ONBOARD-failed instrument: it is
    # still fully evaluated by the rule engine (a historical acquisition
    # failure is never turned into a successful evaluation of something
    # else, and never silently dropped from the published candidates).
    assert candidate['disposition'] == 'EVALUATED'
    assert candidate['historical_acquisition'] == {
        'status': 'IDENTITY_MISMATCH',
        'reason': 'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH',
        'observations': 0,
    }
    # The recommendation/scoring formulas are untouched by this additive
    # field: with no quote/history facts persisted in this fixture, the
    # rule engine's own, pre-existing, independent judgment is
    # INSUFFICIENT_DATA -- exactly what it would already have been without
    # this field existing at all.
    assert candidate.get('recommendation') != 'OPPORTUNITY'


@pytest.mark.asyncio
async def test_scenario9_successful_persisted_history_is_reread_before_final_scoring(repo):
    """A real, successful ETF_ONBOARD history acquisition (bars actually
    persisted) must be reflected as historical_acquisition.status ==
    'ACQUIRED', and the final evaluation pass -- which runs via
    run_etf_radar_cycle AFTER the ETF_ONBOARD phase completes within the
    same run_full_etf_cycle() call -- must have actually re-read those
    freshly-persisted bars rather than scoring from stale/absent data."""
    rows, csv = universe(1)
    key = UUID(rows[0]['globalInstrumentId'])
    prices = [MarketPriceObservation(instrument_id=key, observed_at=NOW - timedelta(days=253 - i),
        price=Decimal(100 + i), currency='INR', provider='YAHOO_FINANCE',
        source_url='https://finance.yahoo.com/quote/FIXTURE1.NS/history', retrieved_at=NOW) for i in range(254)]

    async def history_acquirer(repository, row, *, now, checkpoint):
        return await _ensure_etf_history(repository, row, now=now, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=prices)))

    cp = await checkpoint(repo)
    source = SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_full_coverage_handler(rows, csv))) as client:
        result = await run_full_etf_cycle(repo, source, checkpoint=cp, cycle_id=cp.cycle_id,
            identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
            history_acquirer=history_acquirer)
    saved = repo.persistence.etf_radar_cycle(cp.cycle_id)
    candidate = saved['candidates'][0]
    assert candidate['historical_acquisition']['status'] == 'ACQUIRED'
    assert candidate['historical_acquisition']['observations'] == 254
    # Prove the final evaluation pass actually re-read the freshly-persisted
    # bars (not stale/absent evidence): a return/volatility metric derived
    # from exactly those 254 bars must be present among the published
    # source_references for this candidate.
    bars = repo.persistence.load_daily_market_bars({key})
    assert len(bars) == 254
    # Prove the final evaluation pass actually re-read the freshly-persisted
    # bars (not stale/absent evidence): the rule engine's own
    # performance/risk factors, which require >=252 persisted daily bars to
    # compute at all (PERIOD_SESSIONS[RETURN_1Y]=252,
    # VOLATILITY_WINDOW_SESSIONS=63), must be AVAILABLE rather than
    # UNAVAILABLE -- that is only possible if evaluate() read these exact
    # 254 bars this cycle.
    factor_results = {f['factor']: f['status'] for f in
        (candidate.get('rule_engine_result') or {}).get('factor_results', [])}
    assert factor_results.get('PERFORMANCE_MOMENTUM') == 'SCORED'
    assert factor_results.get('RISK_DRAWDOWN_VOLATILITY') == 'SCORED'
