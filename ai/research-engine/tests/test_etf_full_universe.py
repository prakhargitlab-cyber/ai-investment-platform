from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from app.cycle_checkpoint import CycleCheckpoint, CycleOwnershipLost
from app.etf_full_universe import run_full_etf_cycle, reconcile, peer_results
from app.etf_evidence import EtfAcquisitionAttempt, EtfAcquisitionOutcome, EtfProvenance, EtfAuthority
from app.etf_acquisition import parse_nse_etf_list
from test_etf_ingestion import repo, META, NOW
from test_etf_preflight import compatible_dispatch


def universe(size):
    rows = [META | {'globalInstrumentId':str(UUID(int=n)), 'isin':f'IN{n:010d}', 'primarySymbol':f'FIXTURE{n}',
        'providerMappings':[META['providerMappings'][0] | {'providerSymbol':f'FIXTURE{n}'}]} for n in range(1,size+1)]
    csv='Symbol,SecurityName,ISINNumber\n'+''.join(f"{r['primarySymbol']},Fixture ETF,{r['isin']}\n" for r in rows)
    return rows,csv


async def checkpoint(repo):
    key=str(UUID(int=10000))
    repo.persistence.create_cycle_run(key,{},market='ETF')
    repo.persistence.claim_cycle_run(key,'owner',lease_seconds=600)
    return await CycleCheckpoint(repo.persistence,key,'owner',run_blocking=repo._run_blocking_persistence).load()


@pytest.mark.asyncio
@pytest.mark.parametrize('size',[7,352,357])
async def test_complete_fixture_coverage(repo,size):
    rows,csv=universe(size); calls=[]
    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith('.csv'): return httpx.Response(200,text=csv)
        if 'marketWatchApi' in request.url.path:
            return httpx.Response(200,json={'data':{'timestamp':NOW.strftime('%d-%b-%Y %H:%M:%S'),
                'navDate':NOW.strftime('%d-%b-%Y'),'data':[dict(symbol=r['primarySymbol'],series='EQ',
                identifier=r['primarySymbol']+'EQN',lastPrice=100,totalTradedVolume=500000,nav=100) for r in rows]}})
        symbol=request.url.params['symbol']; row=next(r for r in rows if r['primarySymbol']==symbol)
        return httpx.Response(200,json=dict(symbol=symbol,isin=row['isin'],isETFSec='true',isDelisted='false',
            isSuspended='false',activeSeries=['EQ'],marketType='N'))
    cp=await checkpoint(repo)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result=await run_full_etf_cycle(repo,SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)),
            checkpoint=cp,cycle_id=cp.cycle_id,identity_headers={},client=client,clock=lambda:NOW,sleep=AsyncMock())
    saved=repo.persistence.etf_radar_cycle(cp.cycle_id)
    assert result['coverage']['official']==result['coverage']['attempted']==result['coverage']['acquired']==size
    assert len(saved['outcomes'])==len(saved['candidates'])==size
    assert saved['coverage']['failed']==0 and saved['ranked']==[]
    assert saved['coverage']['evaluated']==size and saved['coverage']['eligible']==0
    assert saved['coverage']['analysis_incomplete']==size and saved['coverage']['unclassified']==size
    assert saved['excluded_by_reason']=={'UNVERIFIED_PEER_CLASSIFICATION':size}
    assert len(calls)==size+2 and len(cp._progress)==size
    assert repo.persistence.active_cycle_run('NSE') is None


def test_reconcile_conflicts_missing_and_duplicates():
    rows,csv=universe(7)
    provenance=EtfProvenance(provider='NSE',source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',source_identity='fixture',source_url='https://nsearchives.nseindia.com/fixture',
        retrieved_at=NOW,authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A')
    listings=parse_nse_etf_list(csv,provenance)
    rows[0]['isin']='IN9999999999'
    mapped=reconcile(listings,rows[:-1])
    assert [i['identity_status'] for i in mapped].count('VERIFIED')==5
    assert [i['identity_status'] for i in mapped].count('CANONICAL_IDENTITY_CONFLICT')==1
    assert [i['identity_status'] for i in mapped].count('CANONICAL_ETF_UNAVAILABLE')==1
    with pytest.raises(ValueError,match='DUPLICATE_OFFICIAL_SYMBOL'): reconcile([listings[0],listings[0]],rows)
    with pytest.raises(ValueError,match='DUPLICATE_CANONICAL_ID'): reconcile(listings,rows+rows[:1])


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['cancel','expired','takeover'])
async def test_acquisition_fencing(repo,status):
    cp=await checkpoint(repo)
    if status=='cancel': repo.persistence.request_cycle_cancel(cp.cycle_id)
    else:
        repo.persistence._connection.execute('UPDATE global_opportunity_cycle_run SET lease_expires_at=? WHERE cycle_id=?',
            ((NOW-timedelta(days=5)).isoformat(),cp.cycle_id))
        repo.persistence._connection.commit()
        if status=='takeover': repo.persistence.claim_cycle_run(cp.cycle_id,'replacement',lease_seconds=600)
    attempt=EtfAcquisitionAttempt(instrument_id=UUID(int=1),metric='IDENTITY',provider='NSE',attempted_at=NOW,
        outcome=EtfAcquisitionOutcome.SUCCESS_EMPTY,reason='TEST',evidence_ids=[])
    with pytest.raises(CycleOwnershipLost): repo.persistence.save_etf_acquisition([],attempt,fence=(cp.cycle_id,cp.owner_id))
    assert repo.persistence.etf_attempts(UUID(int=1))==[]


def test_peer_comparison_stable_and_separate():
    from app.models import EtfResearchProfile,EtfSubtype
    profiles={str(UUID(int=n)):EtfResearchProfile(instrument_id=UUID(int=n),fund_id=UUID(int=n),fund_name='fixture',
        ticker=f'X{n}',exchange='NSE',mic='XNSE',subtype=peer) for n,peer in [(1,EtfSubtype.GOLD),(2,EtfSubtype.SILVER),(3,EtfSubtype.GOLD)]}
    def values(): return [dict(global_instrument_id=str(UUID(int=n)),recommendation='OPPORTUNITY',
        rule_engine_result={'overall_score':50},disposition='EVALUATED') for n in [3,2,1,4]]
    first=peer_results(values(),profiles)
    assert first==peer_results(list(reversed(values())),profiles)
    assert sorted(c['rank'] for c in first)==[1,1,2]

@pytest.mark.asyncio
async def test_provider_access_denial_and_budget_fail_closed(repo):
    from app.etf_full_universe import BudgetClient
    cp=await checkpoint(repo)
    selection={'limits':{'max_requests':2},'request_reservations':0}
    calls=[]
    def denied(request):
        calls.append(request)
        return httpx.Response(429)
    async with httpx.AsyncClient(transport=httpx.MockTransport(denied)) as client:
        budget=BudgetClient(client,cp,selection,sleep=AsyncMock())
        with pytest.raises(httpx.HTTPStatusError): await budget.get('https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv')
        with pytest.raises(RuntimeError,match='NSE_ACCESS_RESTRICTED'): await budget.get('https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv')
    assert len(calls)==1
    assert (await cp.selection())['failure_reason']=='NSE_ACCESS_RESTRICTED'
    assert repo.persistence.latest_etf_radar_cycle() is None


@pytest.mark.asyncio
async def test_full_endpoint_authorization_and_pause(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    from test_etf_preflight import ADMIN
    worker=SimpleNamespace(submit_async=AsyncMock(return_value={'cycle_id':str(UUID(int=33))}))
    monkeypatch.setattr(main,'_etf_radar_worker',lambda:worker)
    monkeypatch.setattr(main.settings,'research_opportunity_recovery_paused',False)
    client=TestClient(main.app)
    path='/api/v1/etf-radar/full-universe-cycles'
    assert client.post(path,json={}).status_code==401
    assert client.post(path,json={},headers=ADMIN | {'X-AIP-User-Roles':'USER'}).status_code==403
    assert client.post(path,json={'max_requests':15001},headers=ADMIN).status_code==422
    assert client.post(path,json={},headers=ADMIN).status_code==202
    assert worker.submit_async.await_args.args[0]['full_universe_limits']['max_universe']==2000
    monkeypatch.setattr(main.settings,'research_opportunity_recovery_paused',True)
    assert client.post(path,json={},headers=ADMIN).status_code==503
    assert worker.submit_async.await_count==1

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['resume','partial_failure','cancel_at_publication'])
async def test_recovery_and_no_partial_publication(repo,monkeypatch,mode):
    import asyncio
    import app.etf_full_universe as module
    rows,csv=universe(7); batches=[]
    async def acquire(repository,orchestrator,selected,client,**kwargs):
        batches.append([r['globalInstrumentId'] for r in selected])
        return {'instruments':[{'instrument_id':r['globalInstrumentId'],'status':'BLOCKED','attempts':
            [{'outcome':'TECHNICAL_FAILURE'}] if mode=='partial_failure' and r==rows[0] else []} for r in selected]}
    monkeypatch.setattr(module,'ingest_quotes',acquire)
    cp=await checkpoint(repo); source=SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    finish=cp.finish; finished=0
    async def interrupted(*args,**kwargs):
        nonlocal finished
        await finish(*args,**kwargs); finished+=1
        if finished==5: raise asyncio.CancelledError()
    if mode=='resume': monkeypatch.setattr(cp,'finish',interrupted)
    if mode=='cancel_at_publication':
        publish=repo.persistence.save_etf_radar_cycle
        def race(*args,**kwargs):
            repo.persistence.request_cycle_cancel(cp.cycle_id)
            publish(*args,**kwargs)
        monkeypatch.setattr(repo.persistence,'save_etf_radar_cycle',race)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,text=csv))) as client:
        async def run(handle):
            return await module.run_full_etf_cycle(repo,source,checkpoint=handle,cycle_id=cp.cycle_id,
                identity_headers={},client=client,clock=lambda:NOW,sleep=AsyncMock())
        if mode=='resume':
            with pytest.raises(asyncio.CancelledError): await run(cp)
            assert repo.persistence.latest_etf_radar_cycle() is None
            restored=await CycleCheckpoint(repo.persistence,cp.cycle_id,cp.owner_id,run_blocking=repo._run_blocking_persistence).load()
            await run(restored)
            assert [len(b) for b in batches]==[5,2]
            assert source.active_global_etfs.await_count==1
            assert len(repo.persistence.etf_radar_cycle(cp.cycle_id)['outcomes'])==7
        else:
            with pytest.raises((ValueError,CycleOwnershipLost)): await run(cp)
            assert repo.persistence.latest_etf_radar_cycle() is None
            assert len(cp._progress)==7


@pytest.mark.asyncio
async def test_read_only_full_coverage(repo,monkeypatch):
    from httpx import ASGITransport,AsyncClient
    import app.main as main
    from test_etf_preflight import ADMIN
    cp=await checkpoint(repo)
    await cp.save_selection({'scope':'FULL_OFFICIAL_NSE_ETF','universe':[{'isin':'test'}],'request_reservations':1})
    before='\n'.join(repo.persistence._connection.iterdump())
    monkeypatch.setattr(main,'repository',repo)
    monkeypatch.setattr(main,'_etf_radar_worker',lambda:pytest.fail('read started worker'))
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as client:
        response=await client.get(f'/api/v1/etf-radar/full-universe-cycles/{cp.cycle_id}/coverage',headers=ADMIN)
    assert response.status_code==200 and response.json()['official_count']==1
    assert response.json()['completed_count']==0
    assert '\n'.join(repo.persistence._connection.iterdump())==before


def test_official_peer_classification_never_guesses_index_or_fund_names():
    from app.etf_full_universe import official_peer_profile
    rows,_=universe(1)
    assert str(official_peer_profile({'canonical':rows[0],'underlying_reference':'Gold'}).subtype)=='GOLD'
    assert str(official_peer_profile({'canonical':rows[0],'underlying_reference':'Silver'}).subtype)=='SILVER'
    assert official_peer_profile({'canonical':rows[0],'underlying_reference':'Unknown broad market index'}) is None
    assert official_peer_profile({'canonical':rows[0]}) is None


def test_real_canonical_enumeration_projection_is_supported():
    rows,csv=universe(7)
    for row in rows:
        row['ticker']=row.pop('primarySymbol')
        row['exchange']=row.pop('primaryExchange')
    provenance=EtfProvenance(provider='NSE',source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',source_identity='fixture',
        source_url='https://nsearchives.nseindia.com/fixture',retrieved_at=NOW,
        authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A')
    mapped=reconcile(parse_nse_etf_list(csv,provenance),rows)
    assert len(mapped)==7 and all(row['identity_status']=='VERIFIED' for row in mapped)


@pytest.mark.parametrize('asset,category,strategy,expected',[
    ('Equity','EQUITY_INDEX','broad market index','EQUITY_INDEX'),
    ('Equity','EQUITY_SECTOR_THEMATIC','sector','EQUITY_SECTOR_THEMATIC'),
    ('Debt','DEBT',None,'DEBT'),('Gold','GOLD',None,'GOLD'),('Silver','SILVER',None,'SILVER'),
    ('Equity','INTERNATIONAL_EQUITY',None,'INTERNATIONAL_EQUITY'),
    ('Debt','INTERNATIONAL_DEBT',None,'INTERNATIONAL_DEBT'),
    ('Hybrid','HYBRID',None,'HYBRID'),('Equity','GOLD',None,None),
    ('Equity','EQUITY_INDEX','sector',None),('Unknown',None,None,None)])
def test_verified_market_metadata_peer_categories(asset,category,strategy,expected):
    from decimal import Decimal
    from app.etf_full_universe import official_peer_profile
    from app.etf_evidence import EtfFact,EtfMetric
    from app.nse_etf_market import MARKET_URL
    rows,_=universe(1); row=rows[0]
    p=EtfProvenance(provider='NSE',source_type='NSE_ETF_MARKET_WATCH',source_identity='fixture',
        source_url=MARKET_URL,retrieved_at=NOW,authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A',
        source_metadata={'record':{'symbol':row['primarySymbol'],'assets':asset,'mainCatagory':category,'subCatagory':strategy},
            'metadata':{'symbol':row['primarySymbol'],'isin':row['isin'],'isETFSec':'true','isDelisted':'false',
                'isSuspended':'false','activeSeries':['EQ'],'marketType':'N'}})
    fact=EtfFact(instrument_id=UUID(row['globalInstrumentId']),metric=EtfMetric.MARKET_PRICE,value=Decimal(100),unit='INR',as_of_date=NOW.date(),provenance=p)
    profile=official_peer_profile({'canonical':row},fact)
    assert (str(profile.subtype) if profile else None)==expected
    fact.provenance.source_metadata['metadata']['isin']='IN9999999999'
    assert official_peer_profile({'canonical':row},fact) is None

@pytest.mark.asyncio
async def test_access_restriction_survives_recovery_without_any_provider_call(repo):
    cp=await checkpoint(repo)
    await cp.save_selection({'scope':'FULL_OFFICIAL_NSE_ETF','failure_reason':'NSE_ACCESS_RESTRICTED',
        'limits':{'max_requests':100,'max_universe':100,'max_seconds':30},'request_reservations':1})
    client=SimpleNamespace(get=AsyncMock(side_effect=AssertionError('provider called after access restriction')))
    source=SimpleNamespace(active_global_etfs=AsyncMock())
    with pytest.raises(RuntimeError,match='NSE_ACCESS_RESTRICTED'):
        await run_full_etf_cycle(repo,source,checkpoint=cp,cycle_id=cp.cycle_id,identity_headers={},client=client)
    client.get.assert_not_called()
    source.active_global_etfs.assert_not_called()
