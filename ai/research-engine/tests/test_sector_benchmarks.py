"""Canonical identity, official captured contract, and persisted exact-date integration."""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.nse_index_history import ENDPOINT, NseIndexHistoryProvider, parse_index_history
from app.nse_historical_daily import BOOTSTRAP, NseHistoricalResult, persist_daily_result
from app.sector_benchmarks import *
from app.sector_relative_strength import SectorRelativeStrengthEngine
from app.persistence import SqliteResearchPersistence
from app.settings import Settings

START, END = date(2026, 8, 13), date(2026, 9, 11)
NOW = datetime(2026, 9, 13, 20, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / 'fixtures'
FILES = dict(zip(BENCHMARKS, ('500', 'it', 'financials', 'healthcare')))
STOCK = UUID(int=1)


def metadata(key=BROAD_KEY):
    return dict(globalInstrumentId=str(benchmark_id(key)), assetType='INDEX', status='ACTIVE',
        country='IN', primaryExchange='NSE', primarySymbol='NOT_IDENTITY', currency='INR',
        providerMappings=[dict(provider='NSE', providerSymbol=BENCHMARKS[key], status='VERIFIED',
            resolutionSource=CATALOG_VERSION, currency='INR', exchange='NSE')])


def classification(sector='Technology', key=STOCK):
    return dict(globalInstrumentId=str(key), canonicalSector=sector, source='NSE_INDICES_NIFTY500',
        retrievedAt=NOW.isoformat(), status='ACTIVE', assetType='EQUITY', country='IN', exchange='NSE')


def parsed(key=BROAD_KEY):
    result = NseHistoricalResult(benchmark_id(key), START, END, provider_symbol=BENCHMARKS[key],
        source_url=ENDPOINT, retrieved_at=NOW)
    parse_index_history((FIXTURES / f'nse_index_{FILES[key]}.json').read_bytes(), result, 'INR')
    result.status = 'SUCCESS'
    return result


@pytest.mark.parametrize('sector', list(SECTOR_KEYS))
def test_exact_mapping(sector):
    c = build_sector_contexts([classification(sector)], [metadata(k) for k in BENCHMARKS], {STOCK})[STOCK]
    assert c.mapping_version == 'SECTOR_BENCHMARK_MAPPING_V1'
    assert c.sector_benchmark.instrument_id == benchmark_id(SECTOR_KEYS[sector])
    assert c.market_benchmark.instrument_id == benchmark_id(BROAD_KEY)
    assert c.sector_mapping_status == 'AVAILABLE'
    with pytest.raises(ValueError): benchmark_id(BENCHMARKS[BROAD_KEY])


@pytest.mark.parametrize('sector', ['Industrials', 'Materials', 'Consumer Staples', 'Consumer Discretionary',
    'Energy', 'Utilities', 'Real Estate', 'Communication Services', 'FINANCIAL SERVICES', 'Bank'])
def test_unmapped_not_substituted(sector):
    c = build_sector_contexts([classification(sector)], [metadata(k) for k in BENCHMARKS], {STOCK})[STOCK]
    assert c.sector_benchmark is None and c.sector_mapping_status == 'UNMAPPED_SECTOR_BENCHMARK'
    assert c.market_benchmark is not None


@pytest.mark.parametrize('rows', [[], [classification(None)], [classification(), classification('Financials')],
    [classification() | dict(source='GUESSED')]])
def test_missing_ambiguous_classification(rows):
    c = build_sector_contexts(rows, [], {STOCK})[STOCK]
    assert c.sector_mapping_status == 'NO_SECTOR_CLASSIFICATION'
    assert c.market_mapping_status == 'BENCHMARK_IDENTITY_UNAVAILABLE'


@pytest.mark.parametrize('change', [dict(status='INACTIVE'), dict(assetType='EQUITY'), dict(currency=None),
    dict(globalInstrumentId=str(UUID(int=99))), dict(providerMappings=[]),
    dict(providerMappings=metadata()['providerMappings'] * 2),
    *[dict(providerMappings=[metadata()['providerMappings'][0] | x]) for x in
      [dict(active=False), dict(status='UNVERIFIED'), dict(providerSymbol='NIFTY 50'),
       dict(resolutionSource='GUESSED'), dict(currency='USD')]]])
def test_identity_rejected(change):
    with pytest.raises(ValueError, match='BENCHMARK_IDENTITY_UNAVAILABLE'):
        benchmark_identity(metadata() | change, BROAD_KEY)


@pytest.mark.parametrize('key', list(BENCHMARKS))
def test_observed_official_contract(key):
    r = parsed(key)
    assert r.rows_accepted == 22
    assert r.first_trading_date == START and r.last_trading_date == END
    assert all(b.volume is b.turnover is b.previous_close is None for b in r.bars)
    assert all(type(b.close) is Decimal and b.high >= b.low > 0 for b in r.bars)
    assert r.bars == sorted(r.bars, key=lambda b: b.trading_date)
    # EOD_TIMESTAMP is the trading date; HI_TIMESTAMP is the prior UTC day.
    assert r.bars[-1].trading_date == date(2026, 9, 11)


@pytest.mark.parametrize('field,value', [('EOD_INDEX_NAME', 'NIFTY 50'), ('EOD_TIMESTAMP', 'bad'),
    ('EOD_TIMESTAMP', '12-SEP-2026'), ('EOD_OPEN_INDEX_VAL', 'NaN'), ('EOD_LOW_INDEX_VAL', 0),
    ('EOD_HIGH_INDEX_VAL', 1), ('EOD_CLOSE_INDEX_VAL', None)])
def test_invalid_rows_fail_whole_window(field, value):
    payload = json.loads((FIXTURES / 'nse_index_500.json').read_bytes())
    payload['data'][0][field] = value
    r = NseHistoricalResult(benchmark_id(BROAD_KEY), START, END, provider_symbol='NIFTY 500')
    with pytest.raises(ValueError): parse_index_history(json.dumps(payload).encode(), r, 'INR')
    assert r.bars == []


@pytest.mark.parametrize('payload', [b'', b'<html>blocked</html>', b'{}', b'{"data":[]}', b'{"data":[{}]}'])
def test_invalid_contract(payload):
    with pytest.raises(ValueError): parse_index_history(payload, parsed(), 'INR')


def test_duplicate_and_precision():
    payload = json.loads((FIXTURES / 'nse_index_500.json').read_bytes())
    payload['data'][0]['EOD_OPEN_INDEX_VAL'] = '12345.123456789012'
    r = parsed()
    parse_index_history(json.dumps(payload).encode(), r, 'INR')
    assert r.bars[-1].open == Decimal('12345.123456789012')
    payload['data'].append(payload['data'][0])
    with pytest.raises(ValueError): parse_index_history(json.dumps(payload).encode(), parsed(), 'INR')


@pytest.mark.asyncio
async def test_session_reuse_query_spacing_no_cookie_logs(caplog):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers['user-agent'].startswith('Mozilla/')
        if str(request.url) == BOOTSTRAP:
            return httpx.Response(200, headers={'set-cookie': 'session=private-test-cookie; Path=/; Secure'})
        assert request.headers['cookie'] == 'session=private-test-cookie'
        assert dict(request.url.params) == {'indexType':'NIFTY 500', 'from':'13-08-2026', 'to':'11-09-2026'}
        return httpx.Response(200, content=(FIXTURES / 'nse_index_500.json').read_bytes())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sleep = AsyncMock()
        p = NseIndexHistoryProvider(SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=metadata())),
            Settings(), client=client, sleep=sleep)
        for _ in range(2):
            r = await p.fetch(benchmark_id(BROAD_KEY), start=START, end=END)
            assert r.status == 'SUCCESS' and r.rows_accepted == 22
        assert len(requests) == 3 and sleep.await_count >= 2
        await p.aclose()
    assert 'private-test-cookie' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('status,attempts', [(403,1), (404,1), (429,3), (500,3)])
async def test_http_failures(status, attempts):
    requests = []
    def handler(request):
        if str(request.url) == BOOTSTRAP: return httpx.Response(200)
        requests.append(request)
        return httpx.Response(status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = NseIndexHistoryProvider(SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=metadata())),
            Settings(), client=client, sleep=AsyncMock())
        r = await p.fetch(benchmark_id(BROAD_KEY), start=START, end=END)
        assert r.failure_reason == f'HISTORICAL_HTTP_{status}' and not r.bars
        assert len(requests) == attempts


@pytest.mark.asyncio
async def test_identity_before_network_and_bounded_dates():
    def forbidden(request): pytest.fail('Network before identity/window gate')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        p = NseIndexHistoryProvider(SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=None)), Settings(), client=client)
        r = await p.fetch(benchmark_id(BROAD_KEY), start=START, end=END)
        assert r.failure_reason == 'BENCHMARK_IDENTITY_UNAVAILABLE'
        r = await p.fetch(benchmark_id(BROAD_KEY), start=START, end=END+timedelta(days=1))
        assert r.failure_reason == 'INVALID_REQUEST_WINDOW'


@pytest.mark.asyncio
async def test_persist_repeat_correction_other_provider_and_failure():
    store = SqliteResearchPersistence()
    repo = SimpleNamespace(upsert_daily_market_bars_async=AsyncMock(side_effect=store.upsert_daily_market_bars))
    r = parsed()
    await persist_daily_result(repo, r)
    await persist_daily_result(repo, r)
    corrected = r.bars[-1].model_copy(update={'close': r.bars[-1].close + Decimal('.01')})
    store.upsert_daily_market_bar(corrected.model_copy(update={'provider':'OTHER'}))
    r.bars[-1] = corrected
    await persist_daily_result(repo, r)
    rows = store.load_daily_market_bars({r.global_instrument_id}, provider='NSE')
    assert len(rows) == 22 and rows[-1].close == corrected.close
    assert len(store.load_daily_market_bars({r.global_instrument_id})) == 23
    repo.upsert_daily_market_bars_async.side_effect = RuntimeError('do not expose')
    await persist_daily_result(repo, r)
    assert r.failure_reason == 'DAILY_BAR_PERSISTENCE_UNAVAILABLE'
    assert len(store.load_daily_market_bars({r.global_instrument_id}, provider='NSE')) == 22


def test_daily_alignment_reversed_no_filling_and_provenance(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network during compute'))
    sector_key = SECTOR_KEYS['Technology']
    sector, market = parsed(sector_key), parsed()
    stock = [b.model_copy(update={'global_instrument_id':STOCK}) for b in sector.bars]
    contexts = build_sector_contexts([classification()], [metadata(k) for k in BENCHMARKS], {STOCK})
    daily = {STOCK:stock, sector.global_instrument_id:sector.bars, market.global_instrument_id:market.bars}
    def compute(rows):
        return SectorRelativeStrengthEngine().compute(STOCK, [], as_of=NOW, currency='INR', context=contexts[STOCK], daily_bar_histories=rows)
    first = compute(daily)
    assert first == compute({k:list(reversed(v)) for k,v in daily.items()})
    assert first.benchmark_mapping_version == MAPPING_VERSION
    assert first.relative_vs_sector1_m == 0 and first.sector_state != 'INSUFFICIENT_DATA'
    assert first.sector_return1_m == pytest.approx(float((sector.bars[-1].close / sector.bars[0].close - 1)*100))
    assert first.history_sources['sector'] == 'DAILY_MARKET_BAR_NSE'
    daily[sector.global_instrument_id] = sector.bars[1:]
    second = compute(daily)
    assert second.relative_vs_sector1_m is None and second.relative_vs_sector1_w == 0
    daily[sector.global_instrument_id] = [b.model_copy(update={'provider':'OTHER'}) for b in sector.bars]
    assert compute(daily).benchmark_states['sector'] == 'BENCHMARK_HISTORY_UNAVAILABLE'


@pytest.mark.asyncio
@pytest.mark.parametrize('count,multiple', [(0,False),(1,False),(18,False),(18,True)])
async def test_stage_b_bounded_queries(count, multiple):
    from app.global_scanner import GlobalScanner
    from test_global_scanner import instrument, persisted, scan
    store = SqliteResearchPersistence()
    items = [instrument(n) for n in range(10, 10+count)]
    for item in items: persisted(store, item)
    initial = await scan(items, store, top_n=max(count, 1))
    ids = {UUID(item['globalInstrumentId']) for item in items}
    sectors = list(SECTOR_KEYS)
    rows = [classification(sectors[i%3] if multiple else 'Technology', k) for i,k in enumerate(sorted(ids))]
    contexts = build_sector_contexts(rows, [metadata(k) for k in BENCHMARKS], ids)
    queries = []
    store._connection.set_trace_callback(queries.append)
    enriched = GlobalScanner(None, store).enrich_candidates(initial, sector_contexts=contexts)
    assert len(enriched) == count
    assert len(queries) == (2 if count else 0)
    assert all('SELECT' in q for q in queries)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['HISTORICAL_HTTP_403', 'INVALID_INDEX_ROW', 'DAILY_BAR_PERSISTENCE_UNAVAILABLE'])
async def test_worker_failure_isolation_session_closed(monkeypatch, reason):
    from app.market_data_population import IndiaMarketDataPopulationJobs
    import app.nse_index_history as module
    keys = sorted([benchmark_id(k) for k in BENCHMARKS], key=str)[:2]
    responses = [NseHistoricalResult(keys[0], START, END, failure_reason=reason), parsed()]
    provider = SimpleNamespace(fetch=AsyncMock(side_effect=responses), aclose=AsyncMock())
    factory = lambda *a, **k: provider
    monkeypatch.setattr(module, 'NseIndexHistoryProvider', factory)
    repo = SimpleNamespace(upsert_daily_market_bars_async=AsyncMock(return_value=22))
    jobs = IndiaMarketDataPopulationJobs(repo, None, None, Settings(), sleep=AsyncMock(), clock=lambda:NOW)
    results = await jobs.populate_benchmark_history(set(keys), start=START, end=END, identity_headers={})
    assert len(results) == 2 and results[0].failure_reason == reason and results[1].persisted_rows == 22
    assert provider.fetch.await_count == 2 and provider.aclose.await_count == 1
    assert repo.upsert_daily_market_bars_async.await_count == 1


@pytest.mark.asyncio
async def test_worker_throttling_cooldown_and_batch_bound(monkeypatch):
    from app.market_data_population import IndiaMarketDataPopulationJobs
    import app.nse_index_history as module
    keys = {benchmark_id(k) for k in BENCHMARKS}
    provider = SimpleNamespace(fetch=AsyncMock(return_value=NseHistoricalResult(next(iter(keys)), START, END,
        failure_reason='HISTORICAL_HTTP_429')), aclose=AsyncMock())
    monkeypatch.setattr(module, 'NseIndexHistoryProvider', lambda *a, **k:provider)
    jobs = IndiaMarketDataPopulationJobs(None, None, None, Settings(), sleep=AsyncMock(), clock=lambda:NOW)
    results = await jobs.populate_benchmark_history(keys, start=START, end=END, identity_headers={})
    assert provider.fetch.await_count == 1
    assert [r.failure_reason for r in results][1:] == ['RETRY_COOLDOWN']*3
    jobs.settings.market_data_population_batch_size = 1
    with pytest.raises(ValueError, match='BENCHMARK_BATCH_LIMIT'):
        await jobs.populate_benchmark_history(keys, start=START, end=END, identity_headers={})


def test_hand_calculated_daily_horizons_and_staleness():
    from app.models import DailyMarketBar
    keys = [STOCK, benchmark_id(SECTOR_KEYS['Technology']), benchmark_id(BROAD_KEY)]
    daily = {}
    # Independent linear prices: stock +2, sector +1, market flat per observation.
    for key, slope in zip(keys, (2,1,0)):
        daily[key] = [DailyMarketBar(global_instrument_id=key, trading_date=(NOW-timedelta(days=159-i)).date(),
            open=Decimal(1000+slope*i), high=Decimal(1000+slope*i), low=Decimal(1000+slope*i),
            close=Decimal(1000+slope*i), currency='INR', provider='NSE', provider_symbol='provenance',
            source_mode='REAL', source_url=ENDPOINT, retrieved_at=NOW) for i in range(160)]
    ctx = build_sector_contexts([classification()], [metadata(k) for k in BENCHMARKS], {STOCK})[STOCK]
    def compute(rows): return SectorRelativeStrengthEngine().compute(STOCK, [], as_of=NOW, context=ctx, daily_bar_histories=rows)
    result = compute(daily)
    for suffix, n in zip(('1_w','1_m','3_m','6_m'), (5,21,63,126)):
        assert getattr(result,'stock_return'+suffix) == pytest.approx(100*2*n/(1318-2*n))
        assert getattr(result,'sector_return'+suffix) == pytest.approx(100*n/(1159-n))
        assert getattr(result,'market_return'+suffix) == 0
    daily[keys[1]] = daily[keys[1]][:-10]
    assert compute(daily).benchmark_states['sector'] == 'STALE_BENCHMARK_HISTORY'


@pytest.mark.asyncio
async def test_metadata_adapter_is_batched_read_only():
    from app.portfolio_orchestration import PortfolioResearchOrchestrator
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == '/api/v1/instruments/benchmarks'
        return httpx.Response(200, json=[metadata(k) for k in BENCHMARKS])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        obj = PortfolioResearchOrchestrator(None, Settings(), client=client)
        obj.india_nifty500_universe = AsyncMock(return_value=[classification()])
        assert await obj.sector_benchmark_contexts(set()) == {}
        assert not requests and obj.india_nifty500_universe.await_count == 0
        result = await obj.sector_benchmark_contexts({STOCK})
        assert result[STOCK].sector_mapping_status == 'AVAILABLE'
        assert len(requests) == obj.india_nifty500_universe.await_count == 1


def test_india_market_benchmark_never_assigned_to_foreign_or_unknown_stock():
    registered = [metadata(k) for k in BENCHMARKS]
    for rows in ([], [classification() | dict(country='US', exchange='NASDAQ')]):
        result = build_sector_contexts(rows, registered, {STOCK})[STOCK]
        assert result.market_benchmark is None and result.sector_benchmark is None
