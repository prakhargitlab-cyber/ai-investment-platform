from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from app.cycle_checkpoint import CycleCheckpoint, CycleOwnershipLost
from app.etf_full_universe import run_full_etf_cycle, reconcile, peer_results, _ensure_etf_history
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
    # No-op history acquirer: this test is about BASELINE/identity/NAV
    # coverage, not Yahoo history acquisition -- avoid a real network call.
    async def history_acquirer(repository,row,*,now,checkpoint):
        return await _ensure_etf_history(repository,row,now=now,checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result=await run_full_etf_cycle(repo,SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)),
            checkpoint=cp,cycle_id=cp.cycle_id,identity_headers={},client=client,clock=lambda:NOW,sleep=AsyncMock(),
            history_acquirer=history_acquirer)
    saved=repo.persistence.etf_radar_cycle(cp.cycle_id)
    assert result['coverage']['official']==result['coverage']['attempted']==result['coverage']['acquired']==size
    assert len(saved['outcomes'])==len(saved['candidates'])==size
    assert saved['coverage']['failed']==0 and saved['ranked']==[]
    assert saved['coverage']['evaluated']==size and saved['coverage']['eligible']==0
    assert saved['coverage']['analysis_incomplete']==size and saved['coverage']['unclassified']==size
    assert saved['excluded_by_reason']=={'UNVERIFIED_PEER_CLASSIFICATION':size}
    assert len(calls)==size+2
    # Each identity-resolved instrument now has both a BASELINE and an
    # independent ETF_ONBOARD (history) checkpoint row (Workstream 1).
    assert len(cp._progress)==size*2
    assert all(cp._progress[('ETF_ONBOARD',r['isin'])]['disposition']=='SUCCESS_EMPTY' for r in rows)
    assert repo.persistence.active_cycle_run('NSE') is None


@pytest.mark.asyncio
async def test_full_universe_list_request_sends_browser_identity_headers(repo):
    """Regression for the Full ETF Radar's silent NSE_ETF_LIST_URL ReadTimeout.

    NSE's archive host silently stalls (ReadTimeout) requests whose headers
    don't identify a browser (see app.etf_acquisition.NSE_ETF_LIST_HEADERS).
    run_full_etf_cycle's own transport.get(NSE_ETF_LIST_URL) call previously
    sent none at all, even though every other NSE call reachable from this
    cycle (MARKET_URL/META_URL, and the single-instrument
    etf_acquisition.discover_nse) already attaches browser-identity headers.
    """
    rows,csv=universe(1); seen={}
    def handler(request):
        if request.url.path.endswith('.csv'):
            seen['headers']=request.headers
            return httpx.Response(200,text=csv)
        if 'marketWatchApi' in request.url.path:
            return httpx.Response(200,json={'data':{'timestamp':NOW.strftime('%d-%b-%Y %H:%M:%S'),
                'navDate':NOW.strftime('%d-%b-%Y'),'data':[dict(symbol=r['primarySymbol'],series='EQ',
                identifier=r['primarySymbol']+'EQN',lastPrice=100,totalTradedVolume=500000,nav=100) for r in rows]}})
        symbol=request.url.params['symbol']; row=next(r for r in rows if r['primarySymbol']==symbol)
        return httpx.Response(200,json=dict(symbol=symbol,isin=row['isin'],isETFSec='true',isDelisted='false',
            isSuspended='false',activeSeries=['EQ'],marketType='N'))
    cp=await checkpoint(repo)
    async def history_acquirer(repository,row,*,now,checkpoint):
        return await _ensure_etf_history(repository,row,now=now,checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_full_etf_cycle(repo,SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)),
            checkpoint=cp,cycle_id=cp.cycle_id,identity_headers={},client=client,clock=lambda:NOW,sleep=AsyncMock(),
            history_acquirer=history_acquirer)
    assert 'headers' in seen, 'NSE_ETF_LIST_URL was never requested'
    from app.etf_acquisition import NSE_ETF_LIST_HEADERS
    for key,value in NSE_ETF_LIST_HEADERS.items():
        assert seen['headers'].get(key)==value


def test_full_universe_reuses_single_instrument_discovery_nse_list_headers():
    """Guards against the two NSE ETF-list acquisition paths drifting apart
    again into separately-maintained (and silently incompatible) header
    dicts -- etf_full_universe must keep importing the one definition.
    """
    from app.etf_acquisition import NSE_ETF_LIST_HEADERS as acquisition_headers
    from app.etf_full_universe import NSE_ETF_LIST_HEADERS as full_universe_headers
    assert full_universe_headers is acquisition_headers


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
    # The full-universe flag now gates the production (recovery_paused=False)
    # branch too -- see test_full_universe_gate_enforced_on_every_submission_branch
    # in test_etf_manual_execution.py for the authorization-matrix regression.
    # This test is about admin auth / validation / worker wiring, not the gate
    # itself, so it explicitly authorizes to keep exercising the 202 path.
    monkeypatch.setattr(main.settings,'research_etf_full_universe_execution_enabled',True)
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
    # No-op history acquirer: this test is about BASELINE recovery/cancellation
    # semantics, not Yahoo history acquisition -- avoid a real network call.
    async def history_acquirer(repository,row,*,now,checkpoint):
        return await _ensure_etf_history(repository,row,now=now,checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,text=csv))) as client:
        async def run(handle):
            return await module.run_full_etf_cycle(repo,source,checkpoint=handle,cycle_id=cp.cycle_id,
                identity_headers={},client=client,clock=lambda:NOW,sleep=AsyncMock(),
                history_acquirer=history_acquirer)
        if mode=='resume':
            with pytest.raises(asyncio.CancelledError): await run(cp)
            assert repo.persistence.latest_etf_radar_cycle() is None
            restored=await CycleCheckpoint(repo.persistence,cp.cycle_id,cp.owner_id,run_blocking=repo._run_blocking_persistence).load()
            await run(restored)
            assert [len(b) for b in batches]==[5,2]
            assert source.active_global_etfs.await_count==1
            assert len(repo.persistence.etf_radar_cycle(cp.cycle_id)['outcomes'])==7
        elif mode=='partial_failure':
            # Regression: ONE instrument's genuine BASELINE technical failure
            # (rows[0]) must no longer discard the other 6 reconciled
            # instruments' results -- only a failure across EVERY reconciled
            # instrument fails the whole cycle (see etf_full_universe.py's
            # publication gate: counts['failed'] >= counts['reconciled']).
            await run(cp)
            saved=repo.persistence.etf_radar_cycle(cp.cycle_id)
            assert len(saved['outcomes'])==7
            assert saved['coverage']['failed']==1
            failed_candidate=next(c for c in saved['candidates'] if c.get('disposition')=='TECHNICAL_FAILURE')
            assert failed_candidate['global_instrument_id']==rows[0]['globalInstrumentId']
            assert failed_candidate['recommendation'] is None
            assert all(c.get('global_instrument_id')!=rows[0]['globalInstrumentId'] for c in saved['ranked'])
            assert len(saved['candidates'])==7
        else:
            with pytest.raises((ValueError,CycleOwnershipLost)): await run(cp)
            assert repo.persistence.latest_etf_radar_cycle() is None
            # BASELINE (7) plus the decoupled ETF_ONBOARD history pass (7),
            # which now runs to completion before the ValueError/CycleOwnershipLost
            # raised downstream of it (acquisition-unavailable / publication race).
            assert len(cp._progress)==14


@pytest.mark.asyncio
async def test_partial_technical_failure_does_not_discard_verified_results(repo,monkeypatch):
    """Regression for the all-or-nothing publication gate: a technical
    failure on ONE instrument must not discard the other, genuinely
    verified instruments' results, and must not itself enter ranking.
    """
    import app.etf_full_universe as module
    rows,csv=universe(5)
    async def acquire(repository,orchestrator,selected,client,**kwargs):
        return {'instruments':[{'instrument_id':r['globalInstrumentId'],
            'status':'BLOCKED' if r==rows[0] else 'READY',
            'attempts':[{'outcome':'TECHNICAL_FAILURE'}] if r==rows[0] else []} for r in selected]}
    monkeypatch.setattr(module,'ingest_quotes',acquire)
    cp=await checkpoint(repo); source=SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    async def history_acquirer(repository,row,*,now,checkpoint):
        return await _ensure_etf_history(repository,row,now=now,checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,text=csv))) as client:
        await module.run_full_etf_cycle(repo,source,checkpoint=cp,cycle_id=cp.cycle_id,
            identity_headers={},client=client,clock=lambda:NOW,sleep=AsyncMock(),
            history_acquirer=history_acquirer)
    saved=repo.persistence.etf_radar_cycle(cp.cycle_id)
    assert saved['coverage']['official']==5
    assert saved['coverage']['failed']==1
    assert saved['coverage']['acquired']==4
    failed=[c for c in saved['candidates'] if c.get('disposition')=='TECHNICAL_FAILURE']
    assert len(failed)==1 and failed[0]['global_instrument_id']==rows[0]['globalInstrumentId']
    assert failed[0]['recommendation'] is None
    # The 4 non-failed instruments have no quote/history facts in this
    # fixture (ingest_quotes and history_acquirer are both faked with no
    # persisted evidence), so the rule engine correctly reports
    # INSUFFICIENT_DATA rather than fabricating a recommendation -- the
    # substantive check is that they are NOT the technically-failed
    # instrument and did go through real evaluation (not silently dropped).
    not_failed=[c for c in saved['candidates'] if c.get('disposition')!='TECHNICAL_FAILURE']
    assert len(not_failed)==4
    assert all(c.get('global_instrument_id')!=rows[0]['globalInstrumentId'] for c in not_failed)
    assert all(c.get('disposition')!='TECHNICAL_FAILURE' for c in not_failed)
    assert all(c.get('global_instrument_id')!=rows[0]['globalInstrumentId'] for c in saved['ranked'])
    assert saved['excluded_by_reason'].get('BASELINE_ACQUISITION_TECHNICAL_FAILURE')==1


@pytest.mark.asyncio
async def test_total_acquisition_failure_still_fails_the_whole_cycle(repo,monkeypatch):
    """The partial-success relaxation must not suppress a genuine
    universe-wide acquisition outage: if EVERY reconciled instrument
    technically fails, the cycle must still raise rather than publish an
    all-failed, zero-opportunity result as if it were a normal cycle.
    """
    import app.etf_full_universe as module
    rows,csv=universe(3)
    async def acquire(repository,orchestrator,selected,client,**kwargs):
        return {'instruments':[{'instrument_id':r['globalInstrumentId'],'status':'BLOCKED',
            'attempts':[{'outcome':'TECHNICAL_FAILURE'}]} for r in selected]}
    monkeypatch.setattr(module,'ingest_quotes',acquire)
    cp=await checkpoint(repo); source=SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    async def history_acquirer(repository,row,*,now,checkpoint):
        return await _ensure_etf_history(repository,row,now=now,checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,text=csv))) as client:
        with pytest.raises(ValueError,match='ETF_UNIVERSE_OR_ACQUISITION_UNAVAILABLE'):
            await module.run_full_etf_cycle(repo,source,checkpoint=cp,cycle_id=cp.cycle_id,
                identity_headers={},client=client,clock=lambda:NOW,sleep=AsyncMock(),
                history_acquirer=history_acquirer)
    assert repo.persistence.latest_etf_radar_cycle() is None


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
    ('Equity','EQUITY_SMART_BETA','Nifty 200 Momentum 30','EQUITY_SMART_BETA'),
    ('Equity','EQUITY_SMART_BETA','Nifty 100 Quality 30','EQUITY_SMART_BETA'),
    ('Equity','EQUITY_SECTOR_THEMATIC','Nifty India Consumption','EQUITY_SECTOR_THEMATIC'),
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


@pytest.mark.parametrize('field,value', [
    ('isin', ' in0000000001 '),
    ('isin', 'in 0000000001'),
    ('primarySymbol', ' fixture1 '),
])
def test_reconcile_normalizes_canonical_identifiers(field,value):
    rows,csv=universe(1)
    rows[0][field]=value
    provenance=EtfProvenance(provider='NSE',source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',source_identity='fixture',
        source_url='https://nsearchives.nseindia.com/fixture',retrieved_at=NOW,
        authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A')
    result=reconcile(parse_nse_etf_list(csv,provenance),rows)[0]
    assert result['identity_status']=='VERIFIED'
    assert result['canonical']['isin']=='IN0000000001'
    assert result['canonical']['primarySymbol']=='FIXTURE1'
    # Reconciliation copies metadata; caller-owned rows retain their source text.
    assert rows[0][field]==value


@pytest.mark.parametrize('canonical_changes,mapping_changes', [
    ({'primarySymbol':'DIFFERENT'}, {}),
    ({'isin':'IN9999999999'}, {}),
    ({}, {'providerSymbol':'DIFFERENT'}),
    ({}, {'status':'RESOLVED'}),
    ({}, {'status':'REJECTED'}),
    ({}, {'status':None}),
    ({}, {'resolutionSource':'BROKER_IMPORT_IDENTITY'}),
    ({}, {'active':False}),
    ({'currency':'USD'}, {'currency':'USD'}),
    ({'currency':None}, {}),
    ({}, {'currency':'USD'}),
    ({'primaryExchange':'BSE'}, {}),
    ({'assetType':'EQUITY'}, {}),
])
def test_reconcile_rejects_explicit_conflicts_and_untrusted_nse_mapping(canonical_changes,mapping_changes):
    rows,csv=universe(1)
    rows[0]=rows[0] | canonical_changes
    rows[0]['providerMappings']=[rows[0]['providerMappings'][0] | mapping_changes]
    provenance=EtfProvenance(provider='NSE',source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',source_identity='fixture',
        source_url='https://nsearchives.nseindia.com/fixture',retrieved_at=NOW,
        authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A')
    result=reconcile(parse_nse_etf_list(csv,provenance),rows)[0]
    assert result['identity_status']=='CANONICAL_IDENTITY_CONFLICT'
    assert result['canonical'] is None


@pytest.mark.parametrize('field,value', [
    ('isin',' in0000000001 '),
    ('primarySymbol',' fixture1 '),
])
def test_reconcile_rejects_multiple_matches_after_normalization(field,value):
    rows,csv=universe(1)
    duplicate=rows[0] | {'globalInstrumentId':str(UUID(int=2)), 'isin':'IN0000000002', 'primarySymbol':'OTHER',field:value}
    provenance=EtfProvenance(provider='NSE',source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',source_identity='fixture',
        source_url='https://nsearchives.nseindia.com/fixture',retrieved_at=NOW,
        authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A')
    result=reconcile(parse_nse_etf_list(csv,provenance),rows+[duplicate])[0]
    assert result['identity_status']=='CANONICAL_IDENTITY_CONFLICT'
    assert result['canonical'] is None


def test_reconcile_rejects_duplicate_nse_mappings():
    rows,csv=universe(1)
    rows[0]['providerMappings']=rows[0]['providerMappings'] * 2
    provenance=EtfProvenance(provider='NSE',source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',source_identity='fixture',
        source_url='https://nsearchives.nseindia.com/fixture',retrieved_at=NOW,
        authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A')
    result=reconcile(parse_nse_etf_list(csv,provenance),rows)[0]
    assert result['identity_status']=='CANONICAL_IDENTITY_CONFLICT'
    assert result['canonical'] is None


def test_peer_group_normalizes_official_asset_class_case_and_spacing():
    from app.models import EtfResearchProfile,EtfSubtype
    profiles={str(UUID(int=n)):EtfResearchProfile(instrument_id=UUID(int=n),fund_id=UUID(int=n),fund_name='fixture',
        ticker=f'X{n}',exchange='NSE',mic='XNSE',subtype=EtfSubtype.EQUITY_INDEX,
        asset_class=asset,underlying_index='Nifty 50') for n,asset in [(1,'Equity'),(2,' EQUITY ')]}
    candidates=[dict(global_instrument_id=key,recommendation='OPPORTUNITY',
        rule_engine_result={'overall_score':50},disposition='EVALUATED') for key in profiles]
    ranked=peer_results(candidates,profiles)
    assert {candidate['peer_group'] for candidate in ranked}=={'EQUITY:nifty 50'}
    assert [candidate['rank'] for candidate in ranked]==[1,2]


@pytest.mark.parametrize('record_symbol,expected', [
    (' fixture1 ', 'EQUITY_INDEX'),
    ('fixture1', 'EQUITY_INDEX'),
    ('FIXTURE10', None),
    (None, None),
    (123, None),
])
def test_verified_peer_classification_normalizes_watch_record_symbol(record_symbol,expected):
    from decimal import Decimal
    from app.etf_full_universe import official_peer_profile
    from app.etf_evidence import EtfFact,EtfMetric
    from app.nse_etf_market import MARKET_URL
    rows,_=universe(1); row=rows[0]
    provenance=EtfProvenance(provider='NSE',source_type='NSE_ETF_MARKET_WATCH',source_identity='fixture',
        source_url=MARKET_URL,retrieved_at=NOW,authority=EtfAuthority.OFFICIAL_EXCHANGE,reliability_level='LEVEL_A',
        source_metadata={'record':{'symbol':record_symbol,'assets':'Equity','mainCatagory':'EQUITY','subCatagory':'Nifty 50'},
            'metadata':{'symbol':row['primarySymbol'],'isin':row['isin'],'isETFSec':'true','isDelisted':'false',
                'isSuspended':'false','activeSeries':['EQ'],'marketType':'N'}})
    fact=EtfFact(instrument_id=UUID(row['globalInstrumentId']),metric=EtfMetric.MARKET_PRICE,
        value=Decimal(100),unit='INR',as_of_date=NOW.date(),provenance=provenance)
    profile=official_peer_profile({'canonical':row},fact)
    assert (str(profile.subtype) if profile else None)==expected
