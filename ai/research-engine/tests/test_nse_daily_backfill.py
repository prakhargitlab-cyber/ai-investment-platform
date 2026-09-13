from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

import app.nse_daily_backfill as backfill
from app.market_data_population import IndiaMarketDataPopulationJobs
from app.models import DailyMarketBar
from app.nse_historical_daily import NseHistoricalDailyProvider, NseHistoricalResult, BOOTSTRAP
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)
START, END = date(2026, 9, 1), date(2026, 9, 4)


def metadata(n=1, **changes):
    return dict(globalInstrumentId=str(UUID(int=n)), status='ACTIVE', assetType='EQUITY', country='IN',
        primaryExchange='NSE', currency='INR', providerMappings=[dict(provider='NSE', status='VERIFIED',
        providerSymbol=f'S{n}', currency='INR', resolutionSource='OFFICIAL_NSE')]) | changes


def bar(n=1, day=START, **changes):
    return DailyMarketBar(global_instrument_id=UUID(int=n), trading_date=day, open=Decimal('10'), high=Decimal('12'),
        low=Decimal('9'), close=Decimal('11'), volume=0, currency='INR', provider='NSE', provider_symbol=f'S{n}',
        source_mode='REAL', source_url='https://www.nseindia.com/history', retrieved_at=NOW).model_copy(update=changes)


class Provider:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.failures = {}
        self.price = Decimal('11')

    async def fetch(self, key, *, start, end, **kwargs):
        self.calls.append((key, start, end))
        reason = self.failures.get((key.int, start), self.failures.get(key.int))
        if isinstance(reason, Exception):
            raise reason
        values = [bar(key.int, day=start, close=self.price)]
        if start != end:
            values.append(bar(key.int, day=end, close=self.price))
        return NseHistoricalResult(key, start, end, provider_symbol=f'S{key.int}', http_status=200,
            status='UNAVAILABLE' if reason else 'SUCCESS', failure_reason=reason,
            rows_parsed=0 if reason else len(values), bars=[] if reason else values)

    async def aclose(self): self.closed = True


def setup(monkeypatch, values=None, **settings):
    values = [metadata()] if values is None else values
    universe = [v | {'exchange': v.get('primaryExchange')} for v in values]
    by_id = {UUID(v['globalInstrumentId']): v for v in values}
    orchestrator = SimpleNamespace(active_global_equities=AsyncMock(return_value=universe),
        global_instrument_metadata=AsyncMock(side_effect=lambda key, **kw: by_id[key]))
    store = SqliteResearchPersistence()
    config = Settings(**settings)
    repo = ResearchRepository(settings=config, persistence=store)
    jobs = IndiaMarketDataPopulationJobs(repo, None, orchestrator, config, clock=lambda: NOW, sleep=AsyncMock())
    providers = []
    def factory(*args):
        p = Provider(); providers.append(p); return p
    monkeypatch.setattr(backfill, 'NseHistoricalDailyProvider', factory)
    return jobs, store, providers


async def run(jobs, **kwargs):
    return await jobs.backfill_daily_bars(identity_headers={}, start=START, end=END, **kwargs)


@pytest.mark.parametrize('start,end,size,count', [
    (date(2026,1,1),date(2026,1,30),30,1), (date(2026,1,1),date(2026,1,31),30,2),
    (date(2025,12,15),date(2026,3,5),30,3), (date(2024,2,1),date(2024,3,1),30,1),
    (START,START,30,1), (date(2026,1,31),date(2026,2,1),1,2)])
def test_windows(start,end,size,count):
    windows=backfill.plan_windows(start,end,size)
    assert len(windows)==count and windows[0][0]==start and windows[-1][1]==end
    assert windows==backfill.plan_windows(start,end,size)
    assert all(0 <= (b-a).days < size for a,b in windows)
    assert all(windows[i][1]+timedelta(days=1)==windows[i+1][0] for i in range(len(windows)-1))


@pytest.mark.parametrize('start,end,size',[(END,START,30),(START,END,0),(START,END,-1),(NOW,END,30)])
def test_invalid_windows(start,end,size):
    with pytest.raises(ValueError): backfill.plan_windows(start,end,size)


def test_coverage_states_and_no_calendar_inference():
    assert backfill.coverage_plan([],START,END,NOW,72)[0]=='NO_HISTORY'
    assert backfill.coverage_plan([bar(day=END)],START,END,NOW,72)==('PARTIAL_HISTORY',[(START,END-timedelta(days=1))])
    assert backfill.coverage_plan([bar(),bar(day=END)],START,END,NOW,72)==('CURRENT_HISTORY',[])
    old=NOW-timedelta(days=5)
    assert backfill.coverage_plan([bar(retrieved_at=old)],START,END,NOW,72)==('STALE_HISTORY',[(START+timedelta(days=1),END)])
    # Sparse internal dates are not enough to claim a session gap.
    assert backfill.coverage_plan([bar(),bar(day=END)],START,END,NOW,72)[0]!='GAP_DETECTED'
    assert backfill.coverage_plan([bar(),bar(day=END)],START,END,NOW,72,force=True)[1]==[(START,END)]


@pytest.mark.asyncio
async def test_current_no_session_and_other_provider_does_not_count(monkeypatch):
    jobs,store,providers=setup(monkeypatch)
    store.upsert_daily_market_bars([bar(provider='OTHER'),bar(day=END,provider='OTHER')])
    first=await run(jobs)
    assert first['instruments'][0]['coverage_state_before']=='NO_HISTORY' and first['rows_persisted']==2
    assert len(store.load_daily_market_bars({UUID(int=1)}))==4
    second=await run(jobs)
    assert second['skipped_current']==1 and second['requested_windows']==0 and len(providers)==1
    assert providers[0].closed


@pytest.mark.asyncio
async def test_prefix_tail_and_null_rows(monkeypatch):
    jobs,store,providers=setup(monkeypatch)
    store.upsert_daily_market_bar(bar(day=date(2026,9,3),retrieved_at=NOW-timedelta(days=5)))
    r=await run(jobs)
    assert [(a,b) for _,a,b in providers[0].calls]==[(START,date(2026,9,2)),(END,END)]
    assert r['instruments'][0]['coverage_state_before']=='PARTIAL_HISTORY'
    assert backfill.usable_bars([None,bar(open=None)],UUID(int=1),'S1','INR',END)==[]


@pytest.mark.asyncio
async def test_batch_order_offset_and_membership(monkeypatch):
    jobs,store,providers=setup(monkeypatch,[metadata(3),metadata(1),metadata(2)],market_data_population_batch_size=1)
    first=await run(jobs)
    assert first['requested_instruments']==1 and first['next_offset']==1
    second=await run(jobs,offset=1)
    assert second['instruments'][0]['globalInstrumentId']==str(UUID(int=2)) and second['next_offset']==2
    third=await run(jobs,instrument_ids={UUID(int=3),UUID(int=999)})
    assert third['requested_instruments']==1 and third['instruments'][0]['globalInstrumentId']==str(UUID(int=3))
    assert third['next_offset'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('values',[[],[metadata(assetType='ETF')],[metadata(status='INACTIVE')],[metadata(country='US')],[metadata(primaryExchange='BSE')]])
async def test_empty_eligible_universe(monkeypatch,values):
    jobs,_,providers=setup(monkeypatch,values)
    r=await run(jobs)
    assert r['requested_instruments']==0 and not providers and r['status']=='COMPLETED'


@pytest.mark.asyncio
@pytest.mark.parametrize('mapping',[[],[dict(provider='NSE',status='INVALID',providerSymbol='X')],
    [dict(provider='NSE',status='VERIFIED',providerSymbol=' ')], metadata()['providerMappings']*2,
    [metadata()['providerMappings'][0]|dict(active=False)], [metadata()['providerMappings'][0]|dict(currency='USD')]])
async def test_identity_failure_isolated(monkeypatch,mapping):
    jobs,_,providers=setup(monkeypatch,[metadata(providerMappings=mapping),metadata(2)])
    r=await run(jobs)
    assert r['failed_instruments']==1 and r['succeeded_instruments']==1
    assert r['instruments'][0]['failure_class']=='NO_TRUSTED_MAPPING'
    assert [key.int for key,_,_ in providers[0].calls]==[2]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,category',[('HISTORICAL_HTTP_403','HTTP_ERROR'),('NON_CSV_RESPONSE','PARSER_FAILURE'),
    ('EMPTY_RESPONSE','EMPTY_RESPONSE'),('HISTORICAL_TIMEOUT','PROVIDER_UNAVAILABLE'),(RuntimeError('secret-cookie'),'PROVIDER_UNAVAILABLE')])
async def test_provider_failure_isolation_and_cooldown(monkeypatch,reason,category):
    jobs,store,_=setup(monkeypatch,[metadata(),metadata(2)])
    provider=Provider(); provider.failures[1]=reason
    monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:provider)
    r=await run(jobs)
    assert r['failed_instruments']==1 and r['succeeded_instruments']==1 and r['failed_windows']==1
    assert r['instruments'][0]['failure_class']==category and provider.closed
    assert 'secret-cookie' not in str(r)
    repeat=await run(jobs)
    assert repeat['skipped_cooldown']==1 and repeat['skipped_current']==1


@pytest.mark.asyncio
async def test_throttle_stops_job_and_next_job(monkeypatch):
    jobs,_,_=setup(monkeypatch,[metadata(),metadata(2)])
    provider=Provider(); provider.failures[1]='HISTORICAL_HTTP_429'
    monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:provider)
    r=await run(jobs)
    assert r['failed_instruments']==1 and r['skipped_cooldown']==1 and len(provider.calls)==1
    r=await run(jobs,force=True)
    assert r['skipped_cooldown']==2 and len(provider.calls)==1


@pytest.mark.asyncio
async def test_per_window_persistence_later_failure_preserves_rows(monkeypatch):
    jobs,store,_=setup(monkeypatch,[metadata(),metadata(2)],nse_historical_request_window_days=2)
    provider=Provider(); provider.failures[(1,date(2026,9,3))]='HISTORICAL_HTTP_500'
    monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:provider)
    r=await run(jobs)
    assert r['requested_windows']==4 and r['successful_windows']==3 and r['failed_windows']==1
    assert r['rows_received']==r['rows_accepted']==r['rows_persisted']==6
    assert len(store.load_daily_market_bars({UUID(int=1)},provider='NSE'))==2
    assert r['instruments'][0]['earliest_persisted_date']==START
    assert r['first_requested_date']==START and r['last_requested_date']==END


@pytest.mark.asyncio
async def test_persistence_failure_next_instrument(monkeypatch):
    jobs,store,_=setup(monkeypatch,[metadata(),metadata(2)])
    original=jobs.repository.upsert_daily_market_bars_async
    async def writer(bars):
        if bars[0].global_instrument_id.int==1: raise RuntimeError('secret')
        return await original(bars)
    jobs.repository.upsert_daily_market_bars_async=writer
    r=await run(jobs)
    assert r['instruments'][0]['failure_class']=='PERSISTENCE_FAILURE' and r['succeeded_instruments']==1
    assert 'secret' not in str(r)


@pytest.mark.asyncio
async def test_force_correction_idempotence(monkeypatch):
    jobs,store,_=setup(monkeypatch)
    provider=Provider(); monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:provider)
    await run(jobs)
    provider.price=Decimal('10.5')
    r=await run(jobs,force=True)
    assert r['rows_persisted']==2
    rows=store.load_daily_market_bars({UUID(int=1)},provider='NSE')
    assert len(rows)==2 and all(b.close==Decimal('10.5') for b in rows)


@pytest.mark.asyncio
async def test_default_lookback_exchange_date_and_invalid_range(monkeypatch):
    jobs,_,providers=setup(monkeypatch)
    jobs._clock=lambda:datetime(2026,9,12,20,tzinfo=timezone.utc)
    r=await jobs.backfill_daily_bars(identity_headers={})
    assert r['target_end']==date(2026,9,13) and r['target_start']==date(2026,9,13)-timedelta(days=400)
    assert r['requested_windows']==14 and providers[0].closed
    bad=await jobs.backfill_daily_bars(identity_headers={},start=END,end=START)
    assert bad['failure_reason']=='WINDOW_PLANNING_FAILURE'


@pytest.mark.asyncio
async def test_real_provider_one_session_verified_query_and_serialization(monkeypatch):
    jobs,_,_=setup(monkeypatch,[metadata(),metadata(2)],nse_historical_request_window_days=2)
    calls=[]
    def handler(req):
        calls.append(req)
        if str(req.url)==BOOTSTRAP: return httpx.Response(200,headers={'set-cookie':'session=private; Path=/'})
        assert req.headers['cookie']=='session=private'
        symbol=req.url.params['symbol']; day=req.url.params['from']
        return httpx.Response(200,text=f'Date,Symbol,Series,Open,High,Low,Close\n{day},{symbol},EQ,10,12,9,11\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sleep=AsyncMock()
        provider=NseHistoricalDailyProvider(jobs.orchestrator,jobs.settings,client=client,sleep=sleep)
        provider.aclose=AsyncMock()
        monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:provider)
        r=await run(jobs)
        assert len(calls)==5 and sum(str(c.url)==BOOTSTRAP for c in calls)==1
        assert [c.url.params['symbol'] for c in calls[1:]]==['S1','S1','S2','S2']
        assert sleep.await_count>=4 and r['successful_windows']==4
        provider.aclose.assert_awaited_once()
        assert 'private' not in str(r)

@pytest.mark.asyncio
async def test_successful_range_evidence_avoids_boundary_refetch(monkeypatch):
    jobs,store,_=setup(monkeypatch)
    p=Provider()
    async def fetch(key, *, start, end, **kw):
        p.calls.append((key,start,end))
        return NseHistoricalResult(key,start,end,status='SUCCESS',bars=[bar(day=date(2026,9,2))],rows_parsed=1)
    p.fetch=fetch
    monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:p)
    await run(jobs)
    r=await run(jobs)
    assert r['skipped_current']==1 and len(p.calls)==1
    jobs._clock=lambda: NOW+timedelta(hours=73)
    r=await run(jobs)
    assert r['requested_windows']>0 and len(p.calls)>1


@pytest.mark.asyncio
async def test_cooldown_expiry_and_identity_unavailable(monkeypatch):
    jobs,_,_=setup(monkeypatch)
    original=jobs.orchestrator.global_instrument_metadata.side_effect
    jobs.orchestrator.global_instrument_metadata.side_effect=ValueError('Cookie: SECRET')
    r=await run(jobs)
    assert r['instruments'][0]['failure_class']=='IDENTITY_UNAVAILABLE'
    assert 'SECRET' not in str(r)
    jobs.orchestrator.global_instrument_metadata.side_effect=original
    assert (await run(jobs))['skipped_cooldown']==1
    jobs._clock=lambda: NOW+timedelta(hours=13)
    assert (await run(jobs))['succeeded_instruments']==1


@pytest.mark.asyncio
async def test_session_closes_on_cancel(monkeypatch):
    import asyncio
    jobs,_,_=setup(monkeypatch)
    p=Provider()
    p.fetch=AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:p)
    with pytest.raises(asyncio.CancelledError): await run(jobs)
    assert p.closed


@pytest.mark.asyncio
async def test_population_lock_serializes_backfill_jobs(monkeypatch):
    import asyncio
    jobs,_,providers=setup(monkeypatch)
    a,b=await asyncio.gather(run(jobs),run(jobs))
    assert a['succeeded_instruments']==1 and b['skipped_current']==1 and len(providers)==1


@pytest.mark.asyncio
async def test_window_planning_failure_and_universe_failure(monkeypatch):
    jobs,_,providers=setup(monkeypatch)
    r=await jobs.backfill_daily_bars(identity_headers={},offset=-1)
    assert r['failure_reason']=='WINDOW_PLANNING_FAILURE' and not providers
    jobs.orchestrator.active_global_equities.side_effect=RuntimeError('secret')
    r=await run(jobs)
    assert r['failure_reason']=='UNIVERSE_UNAVAILABLE' and 'secret' not in str(r)


@pytest.mark.asyncio
async def test_empty_window_does_not_block_later_history(monkeypatch):
    jobs,store,_=setup(monkeypatch,nse_historical_request_window_days=2)
    p=Provider(); p.failures[(1,START)]='NO_VALID_HISTORY'
    monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:p)
    r=await run(jobs)
    assert r['requested_windows']==2 and r['failed_windows']==1 and r['successful_windows']==1
    assert r['failed_instruments']==1 and r['rows_persisted']==2
    assert len(store.load_daily_market_bars({UUID(int=1)},provider='NSE'))==2


@pytest.mark.asyncio
async def test_partial_parser_rows_persist_but_not_marked_complete(monkeypatch):
    jobs,store,_=setup(monkeypatch)
    p=Provider()
    async def fetch(key,*,start,end,**kw):
        return NseHistoricalResult(key,start,end,status='SUCCESS',rows_parsed=2,rows_rejected=1,bars=[bar()])
    p.fetch=fetch; monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:p)
    r=await run(jobs)
    assert r['failed_windows']==1 and r['rows_persisted']==1 and r['rows_received']==2 and r['rows_accepted']==1
    assert r['instruments'][0]['failure_class']=='PARSER_FAILURE'
    assert jobs._daily_bar_completed_ranges[(UUID(int=1),'S1','INR')]==[]

@pytest.mark.asyncio
async def test_missing_persisted_rows_override_request_memory(monkeypatch):
    jobs,store,providers=setup(monkeypatch)
    await run(jobs)
    jobs.repository.daily_market_bars_for_instruments=AsyncMock(return_value={UUID(int=1):None})
    again=await run(jobs)
    assert again['instruments'][0]['coverage_state_before']=='NO_HISTORY'
    assert again['requested_windows']==1 and len(providers)==2


@pytest.mark.asyncio
async def test_rejected_rows_are_parser_failure_not_empty(monkeypatch):
    jobs,_,_=setup(monkeypatch,nse_historical_request_window_days=2)
    p=Provider()
    p.fetch=AsyncMock(return_value=NseHistoricalResult(UUID(int=1),START,END,
        failure_reason='NO_VALID_HISTORY', rows_parsed=1, rows_rejected=1))
    monkeypatch.setattr(backfill,'NseHistoricalDailyProvider',lambda *args:p)
    result=await run(jobs)
    assert result['requested_windows']==1 and result['instruments'][0]['failure_class']=='PARSER_FAILURE'
