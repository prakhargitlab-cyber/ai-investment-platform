from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.etf_comparison import holdings_overlap, tracking_metrics
from app.etf_screening import research_overview, screen_page, enrich_candidate
from app.etf_history import acquire_etf_history, EtfHistoryProvider
from app.etf_metrics import compute_etf_deterministic_metrics, EtfMetricId
from app.cycle_checkpoint import CandidateState, CycleOwnershipLost
from app.models import MarketPriceObservation, EtfHolding
from app.etf_evidence import EtfHoldingsSnapshot, EtfProvenance, EtfAuthority
from test_etf_ingestion import repo, META, NOW
from test_etf_preflight import compatible_dispatch
from test_etf_full_universe import checkpoint
from test_etf_rule_engine import TestScoreEtfFullEvidence


@pytest.mark.asyncio
async def test_history_persistence_factor_consumption_retry_and_fence(repo):
    cp = await checkpoint(repo)
    key = UUID(META['globalInstrumentId'])
    prices = [MarketPriceObservation(instrument_id=key, observed_at=NOW-timedelta(days=253-i),
        price=Decimal(100+i), currency='INR', provider='YAHOO_FINANCE',
        source_url='https://finance.yahoo.com/quote/TESTETF.NS/history', retrieved_at=NOW) for i in range(254)]
    provider = SimpleNamespace(closes=AsyncMock(return_value=prices))
    for _ in range(2):
        result = await acquire_etf_history(repo, META, now=NOW, checkpoint=cp, provider=provider)
        assert result['observations'] == 254
    bars = repo.persistence.load_daily_market_bars({key})
    assert len(bars) == 254 and all(b.open is None and b.volume is None for b in bars)
    assert all(b.source_url and b.retrieved_at and b.provider == 'YAHOO_FINANCE' for b in bars)
    metrics = compute_etf_deterministic_metrics(key, bars)
    assert metrics.get(EtfMetricId.RETURN_1Y).value is not None
    assert metrics.get(EtfMetricId.ANNUALIZED_VOLATILITY).value is not None
    assert repo.persistence.cycle_run(cp.cycle_id)['status'] == 'RUNNING'
    repo.persistence.request_cycle_cancel(cp.cycle_id)
    with pytest.raises(CycleOwnershipLost):
        await acquire_etf_history(repo, META, now=NOW, checkpoint=cp, provider=provider)


def test_yahoo_mapping_requires_returned_etf_listing():
    provider = EtfHistoryProvider(None)
    good = {'isin': META['isin'], 'symbol': 'TESTETF.NS', 'quoteType': 'ETF', 'currency': 'INR', 'exchange': 'NSI'}
    provider._verify_identity_or_raise(META, 'TESTETF.NS', SimpleNamespace(info=good))
    for change in ({'isin': 'IN9999999999'}, {'quoteType': 'EQUITY'}, {'symbol': 'OTHER.NS'}, {'currency': 'USD'}, {'exchange': None}):
        with pytest.raises(Exception):
            provider._verify_identity_or_raise(META, 'TESTETF.NS', SimpleNamespace(info=good | change))


def test_yahoo_mapping_rejection_logs_which_field_failed(caplog):
    """Diagnostic-only regression (ETF-radar historical-acquisition-
    completeness investigation): EtfHistoryProvider's own, stricter,
    second identity gate (distinct from the shared verify_historical_
    price_identity fallback) previously raised ETF_HISTORY_LISTING_NOT_
    VERIFIED with zero logging, making it impossible to tell from
    production logs which of symbol/quoteType/currency/exchange actually
    disagreed for any of the real ETF_ONBOARD failures attributed to this
    reason. The log line must now carry the four actually-returned values
    -- this must not change what raises or what is persisted."""
    provider = EtfHistoryProvider(None)
    good = {'isin': META['isin'], 'symbol': 'TESTETF.NS', 'quoteType': 'ETF', 'currency': 'INR', 'exchange': 'NSI'}
    with caplog.at_level("INFO"):
        with pytest.raises(Exception):
            provider._verify_identity_or_raise(META, 'TESTETF.NS', SimpleNamespace(info=good | {'quoteType': 'MUTUALFUND'}))
    [record] = [r for r in caplog.records if r.message.startswith("etf_history_listing_not_verified")]
    assert "returnedQuoteType=MUTUALFUND" in record.message
    assert "returnedSymbol=TESTETF.NS" in record.message
    assert "returnedCurrency=INR" in record.message
    assert "returnedExchange=NSI" in record.message


def test_tracking_known_series_missing_sessions_and_coverage():
    dates = [NOW.date()-timedelta(days=70-i) for i in range(71)]
    series = {d: Decimal(100+i) for i,d in enumerate(dates)}
    result = tracking_metrics(series, series, expected_dates=dates, benchmark_id='fixture-index', currency='INR')
    assert result['tracking_error_percent_pa'] == result['tracking_difference_percent'] == 0
    assert result['coverage'] == 1
    partial = {d:v for i,(d,v) in enumerate(series.items()) if i % 3}
    result = tracking_metrics(partial, series, expected_dates=dates, benchmark_id='fixture-index', currency='INR')
    assert result['tracking_error_percent_pa'] is None and result['coverage'] < Decimal('0.95')


def _isin(index: int) -> str:
    """Deterministic, regex-valid ISINs ([A-Z]{2}[A-Z0-9]{9}[0-9]) -- distinct

    per holding, so overlap can genuinely distinguish matching from
    non-matching constituents instead of every holding silently failing the
    identity check (the original fixture, borrowed from
    TestScoreEtfFullEvidence in test_etf_rule_engine.py, only set
    constituent_name, never constituent_isin -- holdings_coverage() then
    discarded every holding for lacking a matchable ISIN, which is the
    actual root cause of this test's prior failure: production code
    correctly reporting INSUFFICIENT_DATA for identity-less holdings, not a
    defect in holdings_overlap() itself).
    """
    return f"IN{index:09d}1"


def _holdings_snapshot(holding_isins: list[str]) -> EtfHoldingsSnapshot:
    return EtfHoldingsSnapshot(
        instrument_id=META_HOLDINGS_INSTRUMENT_ID, as_of_date=NOW.date(),
        provenance=EtfProvenance(provider="AMC", source_type="FUND_FACTSHEET",
            source_identity="document:holdings-fixture", source_url="https://amc.example/holdings",
            authority=EtfAuthority.OFFICIAL_FUND, retrieved_at=NOW, reliability_level="LEVEL_A"),
        holdings=[EtfHolding(etf_instrument_id=META_HOLDINGS_INSTRUMENT_ID, constituent_isin=isin,
            constituent_name=f"Stock {i}", weight_percentage=Decimal("2"),
            as_of_date=NOW.date(), source_provider="AMC") for i, isin in enumerate(holding_isins)])


META_HOLDINGS_INSTRUMENT_ID = UUID(int=42)


def test_holdings_overlap_unknown_partial_and_complete():
    # COMPLETE: 50 holdings, 2% weight each (100% coverage), all with
    # distinct, valid, matching ISINs on both sides -- full overlap.
    isins = [_isin(i) for i in range(50)]
    snapshot = _holdings_snapshot(isins)
    now = NOW

    # UNKNOWN: no holdings evidence on either side at all.
    assert holdings_overlap(None, None, now=now)['overlap_percent'] is None

    # COMPLETE: identical, fully-covered, identity-verified holdings compared
    # against themselves must show 100% overlap -- every constituent matches.
    assert holdings_overlap(snapshot, snapshot, now=now)['overlap_percent'] == 100

    # PARTIAL: only 10 of 50 holdings reported (20% coverage) falls below the
    # 95% minimum-coverage gate -- overlap is correctly withheld as
    # INSUFFICIENT_DATA rather than computed from an incomplete portfolio.
    partial = snapshot.model_copy(update={'holdings': snapshot.holdings[:10]})
    assert holdings_overlap(partial, snapshot, now=now)['overlap_percent'] is None

    # Duplicate/non-matching identity: re-adding one already-seen ISIN makes
    # the holding set internally inconsistent (same constituent reported
    # twice) -- genuinely unusable for a trustworthy overlap calculation.
    duplicate = snapshot.model_copy(update={'holdings': snapshot.holdings + snapshot.holdings[:1]})
    assert holdings_overlap(duplicate, snapshot, now=now)['overlap_percent'] is None

    # Non-matching ISINs: a same-size, same-coverage snapshot whose
    # constituents share NO ISIN with the first must report 0% overlap, not
    # None -- distinguishing "fully computed, zero actual overlap" from
    # "insufficient data to compute at all" is the whole point of the
    # overlap_percent/status contract.
    disjoint = _holdings_snapshot([_isin(100 + i) for i in range(50)])
    disjoint_result = holdings_overlap(snapshot, disjoint, now=now)
    assert disjoint_result['status'] == 'VALID'
    assert disjoint_result['overlap_percent'] == 0


def candidates(n):
    return [{'global_instrument_id': str(UUID(int=i+1)), 'symbol': f'ETF{i:02d}', 'rank': i+1,
             'recommendation': 'OPPORTUNITY', 'rule_engine_result': {'overall_score': 80-i}}
            for i in range(n)]


@pytest.mark.parametrize('count', [0, 3, 10, 15])
def test_overview_deterministic_no_forced_ten(count):
    rows = candidates(count)
    first = research_overview(rows)
    assert first == research_overview(list(reversed(rows)))
    assert len(first) == min(10, count)
    assert [c['overview_rank'] for c in first] == list(range(1, min(count, 10)+1))
    assert research_overview([{**c, 'recommendation': 'INSUFFICIENT_DATA'} for c in rows]) == []


def test_filters_units_null_pagination_and_sort():
    rows = candidates(30)
    rows[0]['screening_metrics'] = {'aum': {'value': '1000000000', 'unit': 'INR', 'quality': 'VALID'}}
    payload = {'cycle_id': 'fixture', 'candidates': rows}
    assert len(screen_page(payload, page=2)['items']) == 5
    assert screen_page(payload, filters={'aum': {'min': 1, 'unit': 'INR'}})['total'] == 1
    with pytest.raises(ValueError, match='UNIT'):
        screen_page(payload, filters={'aum': {'min': 1, 'unit': 'USD'}})
    assert screen_page(payload, search='ETF29')['total'] == 1
    assert screen_page(payload, sort='score', descending=True)['items'][0]['symbol'] == 'ETF00'


def test_entry_conditions_consistent_no_price_targets():
    row = candidates(1)[0]
    enrich_candidate(row, now=NOW)
    assert row['entry_assessment']['classification'] == 'INSUFFICIENT_EVIDENCE'
    row['entry_inputs'] = dict(current=True, premium_percent='0.2', momentum_percent='1', return_1m_percent='3', spread_percent='0.2', volume=10000)
    enrich_candidate(row, now=NOW)
    assert row['entry_assessment']['classification'] == 'FAVORABLE_ENTRY_ZONE'
    row['entry_inputs']['return_1m_percent'] = '11'
    enrich_candidate(row, now=NOW)
    assert row['entry_assessment']['classification'] == 'EXTENDED_AVOID_CHASING'
    assert row['entry_assessment']['price_band'] is None


@pytest.mark.asyncio
async def test_old_terminal_etf_progress_reconstructed(repo):
    cp = await checkpoint(repo)
    await cp.save_selection({'universe': [META]})
    await cp.start('BASELINE', META['isin'])
    await cp.finish('BASELINE', META['isin'], CandidateState.TERMINAL_OUTCOME, payload={'outcome': 'VERIFIED'})
    repo.persistence.update_cycle_run(cp.cycle_id, cp.owner_id, status='COMPLETED')
    result = repo.persistence.cycle_status_snapshot(cp.cycle_id)
    assert result['phase'] == 'COMPLETED'
    assert result['baseline_evaluated'] == result['universe_count'] == 1
    assert result['stages'][0]['percentage'] == 100
    assert result['overall_percentage'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [3, 12])
async def test_full_cycle_publishes_only_qualifying_top_ten_from_persisted_evidence(repo, size):
    import httpx
    from app.etf_full_universe import run_full_etf_cycle
    from test_etf_full_universe import universe
    from app.etf_evidence import EtfFact, EtfProvenance
    rows, csv = universe(size)
    for row in rows:
        for metric, value in [('EXPENSE_RATIO', '0.1'), ('TRACKING_ERROR', '0.1')]:
            repo.persistence.save_etf_evidence(EtfFact(instrument_id=row['globalInstrumentId'], metric=metric,
                value=value, unit='PERCENT', as_of_date=NOW.date(), provenance=EtfProvenance(
                    provider='FIXTURE_AMC', authority='OFFICIAL_FUND', source_type='FACTSHEET',
                    source_identity='fixture', source_url='https://fixture.invalid/factsheet', retrieved_at=NOW)))
    def handler(request):
        if request.url.path.endswith('.csv'):
            return httpx.Response(200, text=csv)
        if 'marketWatchApi' in request.url.path:
            return httpx.Response(200, json={'data': {'timestamp': NOW.strftime('%d-%b-%Y %H:%M:%S'),
                'navDate': NOW.strftime('%d-%b-%Y'), 'data': [dict(symbol=r['primarySymbol'], series='EQ',
                    identifier=r['primarySymbol']+'EQN', lastPrice=100, totalTradedVolume=500000, nav=100,
                    assets='Descriptive fund name', mainCatagory='EQUITY', subCatagory='Nifty 50') for r in rows]}})
        row = next(r for r in rows if r['primarySymbol'] == request.url.params['symbol'])
        return httpx.Response(200, json=dict(symbol=row['primarySymbol'], isin=row['isin'], isETFSec='true',
            isDelisted='false', isSuspended='false', activeSeries=['EQ'], marketType='N'))
    async def history(repository, row, *, now, checkpoint):
        observations = [MarketPriceObservation(instrument_id=row['globalInstrumentId'], observed_at=now-timedelta(days=253-i),
            price=Decimal(100), currency='INR', provider='YAHOO_FINANCE', source_url='https://fixture.invalid/history',
            retrieved_at=now) for i in range(254)]
        return await acquire_etf_history(repository, row, now=now, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=observations)))
    cp = await checkpoint(repo)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows),
            etf_catalog_onboarding_unsupported=True), checkpoint=cp, cycle_id=cp.cycle_id,
            identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(), history_acquirer=history)
    payload = repo.persistence.etf_radar_cycle(cp.cycle_id)
    assert len(payload['top_opportunities']) == min(size, 10)
    assert payload['coverage']['eligible'] == size
    assert all(c['peer_group'] == 'EQUITY:nifty 50' for c in payload['ranked'])
    assert all(c['history_observations'] == 254 for c in payload['candidates'])
    assert [c['rank'] for c in payload['top_opportunities']] == list(range(1, min(size, 10)+1))
    assert all(c['entry_assessment']['price_band'] is None for c in payload['top_opportunities'])
