import csv
import io
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from app.nse_historical_daily import BOOTSTRAP, NseHistoricalDailyProvider, NseHistoricalResult, parse_csv, persist_daily_result, verified_identity
from app.persistence import SqliteResearchPersistence
from app.settings import Settings

KEY=UUID(int=1)
START, END=date(2026,9,1),date(2026,9,4)
FIXTURE=Path(__file__).parent/'fixtures/nse_historical_daily.csv'
META=dict(globalInstrumentId=str(KEY),status='ACTIVE',assetType='EQUITY',country='IN',primaryExchange='NSE',currency='INR',providerMappings=[dict(provider='NSE',providerSymbol='POLYCAB',status='VERIFIED',resolutionSource='OFFICIAL_NSE_NIFTY500',currency='INR')])

def result(): return NseHistoricalResult(KEY,START,END,provider_symbol='POLYCAB')
def modified(**changes):
    rows=list(csv.reader(io.StringIO(FIXTURE.read_text(encoding='utf-8-sig'))))
    headers=[h.strip() for h in rows[0]]
    for key,value in changes.items(): rows[1][headers.index(key)]=value
    out=io.StringIO(newline=''); csv.writer(out).writerows(rows)
    return out.getvalue().encode()

def test_real_contract():
    r=result(); parse_csv(FIXTURE.read_bytes(),r,'INR')
    assert r.rows_parsed==r.rows_accepted==4 and r.rows_rejected==0
    assert r.first_trading_date==START and r.last_trading_date==END
    assert r.bars[-1].turnover==Decimal('13025481530.00')
    assert r.bars[-1].volume==1559741 and type(r.bars[-1].volume) is int
    assert r.bars[0].close==Decimal('8895.50')
    assert r.bars[0].retrieved_at.utcoffset().total_seconds()==0

def test_optional_aliases_order_zero_precision():
    r=result(); parse_csv(b'Close,Date,Symbol,Series,Open,High,Low,Turnover (in Lacs)\n2,01-09-2026, polycab , eq ,1,2,1,100\n\n',r,'INR')
    assert r.bars[0].turnover is r.bars[0].volume is r.bars[0].previous_close is None
    r=result(); parse_csv(modified(**{'Total Traded Quantity':'0','Turnover ₹':'0','Prev Close':'-','Open Price':'8500.123456789012'}),r,'INR')
    assert r.bars[-1].turnover==r.bars[-1].volume==0 and r.bars[-1].previous_close is None
    assert r.bars[-1].open==Decimal('8500.123456789012')

@pytest.mark.parametrize('field,value',[('Symbol','OTHER'),('Series','BE'),('Date','31-Sep-2026'),('Date','31-Aug-2026'),('Open Price','NaN'),('High Price','1'),('Low Price','0'),('Close Price','-1'),('Prev Close','0'),('Total Traded Quantity','9223372036854775808'),('Total Traded Quantity','1.5'),('Total Traded Quantity','-1'),('Turnover ₹','-1'),('Open Price','1,2'),('Close Price','inf')])
def test_row_validation(field,value):
    r=result(); parse_csv(modified(**{field:value}),r,'INR')
    assert r.rows_parsed==4 and r.rows_accepted==3 and r.rows_rejected==1

def test_bigint_duplicates():
    r=result(); parse_csv(modified(**{'Total Traded Quantity':'9223372036854775807'}),r,'INR')
    assert r.bars[-1].volume==9223372036854775807
    r=result(); parse_csv(modified(Date='03-Sep-2026'),r,'INR')
    assert r.rows_accepted==2 and r.rejection_reasons=={'DUPLICATE_DATE':2}

@pytest.mark.parametrize('body',[b'',b'<html>x</html>',b'{}',b'Date,Close\n1,2',b'Date,Symbol,Series,Open,High,Low,Close\n"unclosed'])
def test_bad_contract(body):
    with pytest.raises((ValueError,csv.Error)): parse_csv(body,result(),'INR')

@pytest.mark.parametrize('change',[dict(providerMappings=[]),dict(status='INACTIVE'),dict(assetType='ETF'),dict(globalInstrumentId=str(UUID(int=2))),dict(providerMappings=META['providerMappings']*2),dict(currency=None,providerMappings=[dict(provider='NSE',providerSymbol='X',status='VERIFIED')]),*[dict(providerMappings=[META['providerMappings'][0]|u]) for u in [dict(status='UNVERIFIED'),dict(active=False),dict(providerSymbol=' '),dict(resolutionSource='BROKER_IMPORT_IDENTITY'),dict(currency='USD')]]])
def test_identity(change):
    with pytest.raises(ValueError): verified_identity(META|change,KEY)

async def fetch_with(handler,metadata=META,**settings):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sleep=AsyncMock()
        provider=NseHistoricalDailyProvider(SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=metadata)),Settings(**settings),client=client,sleep=sleep)
        return await provider.fetch(KEY,start=START,end=END),sleep

@pytest.mark.asyncio
async def test_session_query_no_cookie_logs(caplog):
    calls=[]
    def handler(req):
        calls.append(req)
        assert req.headers['user-agent'].startswith('Mozilla/') and req.headers['referer']==BOOTSTRAP
        assert 'accept-language' in req.headers
        if len(calls)==1: return httpx.Response(200,headers={'set-cookie':'session=secret-cookie; Path=/; Secure'})
        assert req.headers['cookie']=='session=secret-cookie'
        assert dict(req.url.params)==dict(symbol='POLYCAB',series='EQ',csv='true',type='priceVolumeDeliverable',**{'from':'01-09-2026','to':'04-09-2026'})
        return httpx.Response(200,content=FIXTURE.read_bytes(),headers={'content-type':'text/csv'})
    with caplog.at_level(logging.INFO): r,sleep=await fetch_with(handler)
    assert r.status=='SUCCESS' and len(calls)==2 and sleep.await_count>=1
    assert 'secret-cookie' not in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize('status,count',[(403,1),(404,1),(429,3),(500,3)])
async def test_http_failures(status,count):
    calls=[]
    def handler(req):
        if str(req.url)==BOOTSTRAP: return httpx.Response(200)
        calls.append(req); return httpx.Response(status)
    r,_=await fetch_with(handler)
    assert r.failure_reason==f'HISTORICAL_HTTP_{status}' and len(calls)==count and not r.bars

@pytest.mark.asyncio
@pytest.mark.parametrize('body,ctype',[(b'','text/csv'),(b'<html>x</html>','text/html'),(b'wrong,headers','text/csv'),(b'Date,Symbol,Series,Open,High,Low,Close\n"oops','text/csv')])
async def test_provider_bad_response(body,ctype):
    r,_=await fetch_with(lambda req: httpx.Response(200) if str(req.url)==BOOTSTRAP else httpx.Response(200,content=body,headers={'content-type':ctype}))
    assert r.status=='UNAVAILABLE' and r.failure_reason and not r.bars

@pytest.mark.asyncio
async def test_bootstrap_and_missing_identity():
    r,_=await fetch_with(lambda req:httpx.Response(403))
    assert r.failure_reason=='BOOTSTRAP_HTTP_403'
    handler=AsyncMock()
    r,_=await fetch_with(handler,metadata=META|dict(providerMappings=[]))
    assert r.failure_reason=='NO_UNAMBIGUOUS_NSE_MAPPING'; handler.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize('error',[httpx.ReadTimeout,httpx.ConnectError])
async def test_transport(error):
    calls=[]
    def handler(req): calls.append(req); raise error('unavailable')
    r,_=await fetch_with(handler)
    assert len(calls)==3 and r.failure_reason in {'BOOTSTRAP_TIMEOUT','BOOTSTRAP_CONNECTION_FAILURE'}

@pytest.mark.asyncio
async def test_persistence():
    store=SqliteResearchPersistence()
    repo=SimpleNamespace(upsert_daily_market_bars_async=AsyncMock(side_effect=store.upsert_daily_market_bars))
    r=result(); parse_csv(FIXTURE.read_bytes(),r,'INR'); r.status='SUCCESS'
    other=r.bars[0].model_copy(update={'provider':'OTHER'}); store.upsert_daily_market_bar(other)
    await persist_daily_result(repo,r); await persist_daily_result(repo,r)
    r.bars[0]=r.bars[0].model_copy(update={'close':Decimal('8900')}); await persist_daily_result(repo,r)
    loaded=store.load_daily_market_bars({KEY})
    assert len(loaded)==5 and other in loaded and r.persisted_rows==4
    assert store.load_daily_market_bars({KEY},provider='NSE')[0].close==Decimal('8900')
    assert store.load_market_price_observations({KEY})==[]
    await persist_daily_result(repo,result())
    assert store.load_daily_market_bars({KEY})==loaded

@pytest.mark.asyncio
async def test_window_rejected_before_identity_or_network():
    resolver=SimpleNamespace(global_instrument_metadata=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail('network'))) as client:
        p=NseHistoricalDailyProvider(resolver,Settings(nse_historical_request_window_days=2),client=client)
        r=await p.fetch(KEY,start=START,end=END)
        assert r.failure_reason=='REQUEST_WINDOW_EXCEEDED'
        r=await p.fetch(KEY,start=END,end=START)
        assert r.failure_reason=='INVALID_DATE_RANGE'
        resolver.global_instrument_metadata.assert_not_called()

@pytest.mark.asyncio
async def test_throttle_recovery_and_long_retry_after():
    calls=[]
    def handler(req):
        if str(req.url)==BOOTSTRAP: return httpx.Response(200)
        calls.append(req)
        return httpx.Response(429,headers={'retry-after':'5'}) if len(calls)==1 else httpx.Response(200,content=FIXTURE.read_bytes())
    r,sleep=await fetch_with(handler)
    assert r.status=='SUCCESS' and len(calls)==2
    assert any(c.args==(5,) for c in sleep.await_args_list)
    calls.clear()
    def throttled(req):
        if str(req.url)==BOOTSTRAP: return httpx.Response(200)
        calls.append(req); return httpx.Response(429,headers={'retry-after':'120'})
    r,_=await fetch_with(throttled)
    assert r.failure_reason=='HISTORICAL_HTTP_429' and len(calls)==1

@pytest.mark.asyncio
async def test_session_reused_serially():
    active=0
    calls=[]
    async def handler(req):
        nonlocal active
        import asyncio
        active+=1; assert active==1
        await asyncio.sleep(0)
        active-=1; calls.append(req)
        return httpx.Response(200,content=FIXTURE.read_bytes())
    import asyncio
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p=NseHistoricalDailyProvider(SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=META)),Settings(),client=client,sleep=AsyncMock())
        results=await asyncio.gather(p.fetch(KEY,start=START,end=END),p.fetch(KEY,start=START,end=END))
        assert all(r.status=='SUCCESS' for r in results)
        assert sum(str(r.url)==BOOTSTRAP for r in calls)==1

@pytest.mark.asyncio
async def test_explicit_population_boundary(monkeypatch):
    from app.market_data_population import IndiaMarketDataPopulationJobs
    from app.repository import ResearchRepository
    import app.nse_historical_daily as module
    store=SqliteResearchPersistence()
    repo=ResearchRepository(settings=Settings(),persistence=store)
    resolver=SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=META))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=FIXTURE.read_bytes()))) as client:
        provider=NseHistoricalDailyProvider(resolver,Settings(),client=client,sleep=AsyncMock())
        monkeypatch.setattr(module,'NseHistoricalDailyProvider',lambda *args:provider)
        jobs=IndiaMarketDataPopulationJobs(repo,None,resolver,Settings(),sleep=AsyncMock())
        r=await jobs.populate_daily_bars(KEY,start=START,end=END,identity_headers={})
        assert r.persisted_rows==4
        assert len((await repo.daily_market_bars_for_instruments({KEY},provider='NSE'))[KEY])==4
        assert store.load_market_price_observations({KEY})==[]
        assert jobs.historical_provider.provider_name=='YAHOO_FINANCE'

@pytest.mark.asyncio
async def test_persistence_failure_explicit():
    r=result(); parse_csv(FIXTURE.read_bytes(),r,'INR'); r.status='SUCCESS'
    await persist_daily_result(SimpleNamespace(upsert_daily_market_bars_async=AsyncMock(side_effect=RuntimeError())),r)
    assert r.failure_reason=='DAILY_BAR_PERSISTENCE_UNAVAILABLE' and r.persisted_rows==0

def test_malformed_row_shape_and_duplicate_header():
    r=result(); parse_csv(b'Date,Symbol,Series,Open,High,Low,Close\n01-09-2026,POLYCAB,EQ,1,2,1,2,extra\n',r,'INR')
    assert r.rows_rejected==1 and r.rejection_reasons=={'MALFORMED_ROW':1}
    with pytest.raises(ValueError): parse_csv(b'Date,Symbol,Series,Open,Open Price,High,Low,Close\n',result(),'INR')

def test_observed_rupee_turnover_matches_quantity_times_average():
    # NSE average is rounded to two decimals; turnover is explicitly rupees.
    for row in csv.DictReader(io.StringIO(FIXTURE.read_text(encoding='utf-8-sig'))):
        row={k.strip():v for k,v in row.items()}
        qty=Decimal(row['Total Traded Quantity'].replace(',',''))
        average=Decimal(row['Average Price'].replace(',',''))
        turnover=Decimal(row['Turnover ₹'].replace(',',''))
        assert abs(turnover-qty*average)<=qty*Decimal('0.005')

@pytest.mark.asyncio
async def test_generic_verified_symbol_not_primary_symbol():
    metadata=META|dict(primarySymbol='DO_NOT_USE',providerMappings=[META['providerMappings'][0]|dict(providerSymbol='ANOTHER')])
    def handler(req):
        if str(req.url)==BOOTSTRAP: return httpx.Response(200)
        assert req.url.params['symbol']=='ANOTHER'
        return httpx.Response(200,content=FIXTURE.read_bytes().replace(b'POLYCAB',b'ANOTHER'))
    r,_=await fetch_with(handler,metadata=metadata)
    assert r.rows_accepted==4 and all(b.provider_symbol=='ANOTHER' and b.global_instrument_id==KEY for b in r.bars)
