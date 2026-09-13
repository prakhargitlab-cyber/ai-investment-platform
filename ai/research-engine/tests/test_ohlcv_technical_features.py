from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from unittest.mock import Mock

import pytest

from app.models import DailyMarketBar
from app.technical_features import TechnicalFeatureEngine
from test_technical_features import history, NOW, STOCK


def candles(values, *, volumes=None, **changes):
    return [DailyMarketBar(global_instrument_id=STOCK, trading_date=p.observed_at.date(),
        open=p.price.quantize(Decimal('.00000001')), high=(p.price+1).quantize(Decimal('.00000001')),
        low=(p.price-1).quantize(Decimal('.00000001')), close=p.price.quantize(Decimal('.00000001')), previous_close=Decimal('999'),
        volume=100 if volumes is None else volumes[i], currency='INR', provider='NSE', provider_symbol='TEST',
        source_mode='REAL', source_url='https://www.nseindia.com/history', retrieved_at=p.retrieved_at
        ).model_copy(update=changes) for i,p in enumerate(history(values))]


def evaluate(rows, fallback=(), **kwargs):
    return TechnicalFeatureEngine().compute(STOCK, fallback, as_of=NOW, currency='INR', daily_bar_history=rows, **kwargs)


def test_atr_hand_calculated_gaps_and_wilder_update():
    values=[10]*13+[14,6]
    r=evaluate(candles(values))
    # Twelve TR=2, gap up TR=5, gap down TR=9. Provider prevClose=999 is ignored.
    assert r.atr14==pytest.approx(float(Decimal(38)/14),abs=1e-8)
    assert r.atr_pct==pytest.approx(float(Decimal(38)/14/6*100),abs=1e-8)
    r=evaluate(candles(values+[10]))
    assert r.atr14==pytest.approx(float((Decimal(38)/14*13+5)/14),abs=1e-8)


@pytest.mark.parametrize('count,atr,adx',[(14,None,None),(15,2,None),(27,2,None),(28,2,100),(60,2,100)])
def test_atr_adx_minimum_and_constant_range(count,atr,adx):
    r=evaluate(candles(range(100,100+count)))
    assert r.atr14==atr and r.adx14==adx
    assert r.feature_readiness['ATR14']==('AVAILABLE' if atr is not None else 'INSUFFICIENT_HISTORY')
    assert r.feature_readiness['ADX14']==('AVAILABLE' if adx is not None else 'INSUFFICIENT_HISTORY')


@pytest.mark.parametrize('values,expected',[(list(range(100,150)),100),(list(range(150,100,-1)),100),([100]*50,0)])
def test_adx_direction_is_not_strength(values,expected):
    assert evaluate(candles(values)).adx14==expected


def test_zero_range_and_directional_movement_ties():
    flat=candles([100]*30,open=Decimal(100),high=Decimal(100),low=Decimal(100))
    r=evaluate(flat)
    assert r.atr14==r.atr_pct==r.adx14==0
    # Outside bars with equally expanding high/low give +DM=-DM=0.
    tied=[b.model_copy(update={'high':Decimal(101+i),'low':Decimal(99-i)}) for i,b in enumerate(candles([100]*30))]
    assert evaluate(tied).adx14==0


def test_adx_independent_closed_form_decimal_reference():
    rows=candles([100+(i*7 % 17) for i in range(65)])
    trs=[]; plus=[]; minus=[]
    for previous,current in zip(rows,rows[1:]):
        trs.append(max(current.high-current.low,abs(current.high-previous.close),abs(current.low-previous.close)))
        up=current.high-previous.high; down=previous.low-current.low
        plus.append(max(up,Decimal(0)) if up>down else Decimal(0))
        minus.append(max(down,Decimal(0)) if down>up else Decimal(0))
    def weighted(values,index):
        q=Decimal(13)/14
        return sum(values[:14])/14*q**(index-13)+sum(values[k]/14*q**(index-k) for k in range(14,index+1))
    dx=[]
    for i in range(13,len(trs)):
        tr=weighted(trs,i); positive=100*weighted(plus,i)/tr; negative=100*weighted(minus,i)/tr
        dx.append(100*abs(positive-negative)/(positive+negative) if positive+negative else Decimal(0))
    r=evaluate(rows)
    assert r.adx14==pytest.approx(float(weighted(dx,len(dx)-1)),abs=1e-8)
    assert r.atr14==pytest.approx(float(weighted(trs,len(trs)-1)),abs=1e-8)
    assert 0 <= r.adx14 <= 100


def test_missing_ohlc_restarts_warmup_chronology_and_no_blending():
    rows=candles(range(100,140))
    rows[-2]=rows[-2].model_copy(update={'high':None})
    r=evaluate(rows,history(range(1000,1040)))
    assert r.atr14 is r.adx14 is None and r.feature_readiness['ATR14']=='MISSING_OHLC'
    assert r.latest_price==139 and r.dma20 is not None
    assert r==evaluate(list(reversed(rows)),history(range(1000,1040)))
    other=[b.model_copy(update={'provider':'YAHOO_FINANCE','high':Decimal(9999)}) for b in rows]
    assert evaluate(rows+other).atr14 is None


@pytest.mark.parametrize('count,volumes,average,ratio,state',[
    (20,[100]*20,None,None,'UNAVAILABLE'),(21,[100]*21,100,1,'NORMAL'),
    (21,[100]*20+[150],100,1.5,'EXPANSION'),(21,[100]*20+[75],100,.75,'CONTRACTION'),
    (21,[100]*20+[0],100,0,'CONTRACTION'),(21,[0]*21,0,None,'UNAVAILABLE'),
    (21,[100]*20+[None],100,None,'UNAVAILABLE'),(21,[None]+[100]*20,None,None,'UNAVAILABLE')])
def test_volume_boundaries(count,volumes,average,ratio,state):
    r=evaluate(candles([100]*count,volumes=volumes))
    assert r.current_volume==volumes[-1] and r.volume_average20==average and r.volume_ratio20==ratio and r.volume_state==state


def test_bigint_current_exact_ratio_decimal():
    maximum=9223372036854775807
    r=evaluate(candles([100]*21,volumes=[maximum]*21))
    assert r.current_volume==maximum and type(r.current_volume) is int and r.volume_ratio20==1
    assert r.model_dump()['current_volume']==maximum


@pytest.mark.parametrize('volume,confirmed,breakout',[(200,True,'VOLUME_CONFIRMED'),(100,False,'PRICE_BREAKOUT'),(None,None,'PRICE_BREAKOUT')])
def test_breakout_confirmation_and_existing_bonus(volume,confirmed,breakout):
    values=[100]*60+[104]
    r=evaluate(candles(values,volumes=[100]*60+[volume]))
    base=TechnicalFeatureEngine().compute(STOCK,history(values),as_of=NOW,currency='INR')
    assert r.technical_state=='BREAKOUT' and r.breakout_state==breakout
    assert r.breakout_volume_confirmed is confirmed
    assert r.technical_score==min(100,base.technical_score+(5 if confirmed else 0))


def test_reversal_confirmation_is_diagnostic_only():
    values=[200-.6*i for i in range(240)]+[56.6+.3*i for i in range(20)]
    r=evaluate(candles(values,volumes=[100]*259+[200]))
    base=TechnicalFeatureEngine().compute(STOCK,history(values),as_of=NOW,currency='INR')
    assert r.technical_state=='REVERSAL_CANDIDATE' and r.reversal_volume_confirmed is True
    assert r.technical_score==base.technical_score


def test_fallback_and_daily_source_priority():
    fallback=history(range(100,160))
    r=evaluate([],fallback)
    assert r==TechnicalFeatureEngine().compute(STOCK,fallback,as_of=NOW,currency='INR')
    assert r.technical_input_source=='CLOSE_ONLY_FALLBACK' and r.atr14 is r.adx14 is None
    richer=evaluate(candles(range(200,260)),fallback)
    assert richer.latest_price==259 and richer.technical_input_source=='DAILY_MARKET_BAR_NSE'
    assert richer.history_trading_end==candles(range(200,260))[-1].trading_date
    other=evaluate(candles(range(200,260),provider='OTHER'),fallback)
    assert other.latest_price==159 and other.technical_input_source=='CLOSE_ONLY_FALLBACK'


def test_deterministic_duplicates_corrections_conflicts_and_asof():
    rows=candles(range(100,140)); last=rows[-1]
    r=evaluate(rows+[last]); assert r.observation_count==40 and r.duplicate_observation_count==1
    newer=last.model_copy(update={'close':Decimal(140),'retrieved_at':NOW})
    assert evaluate(rows+[newer]).latest_price==140
    conflicting=newer.model_copy(update={'close':Decimal(141)})
    r=evaluate(rows+[newer,conflicting],history(range(200,240)))
    assert r.latest_price is r.atr14 is r.adx14 is None and r.technical_score is None
    assert 'MIXED_NOT_ALLOWED' in r.source_diagnostics
    assert r==evaluate(list(reversed(rows+[newer,conflicting])),history(range(200,240)))
    future=last.model_copy(update={'close':Decimal(999),'retrieved_at':NOW+timedelta(seconds=1)})
    assert evaluate(rows+[future]).latest_price==139


@pytest.mark.asyncio
@pytest.mark.parametrize('count,queries',[(0,0),(1,2),(18,2)])
async def test_stage_b_batch_queries_and_no_network(count,queries,monkeypatch):
    from app.global_scanner import GlobalScanner
    from app.persistence import SqliteResearchPersistence
    from test_global_scanner import instrument,persisted,scan
    from app.nse_historical_daily import NseHistoricalDailyProvider
    monkeypatch.setattr(NseHistoricalDailyProvider,'fetch',Mock(side_effect=AssertionError('provider called')))
    store=SqliteResearchPersistence(); items=[instrument(n) for n in range(1,count+1)]
    for item in items:
        persisted(store,item)
        key=UUID(item['globalInstrumentId'])
        for b in candles(range(100,140)):
            store.upsert_daily_market_bar(b.model_copy(update={'global_instrument_id':key}))
    initial=await scan(items,store)
    before=initial.model_dump()
    statements=[]; store._connection.set_trace_callback(statements.append)
    result=GlobalScanner(None,store).enrich_candidates(initial)
    assert len(statements)==queries and len(result)==count
    assert initial.model_dump()==before
    if count:
        assert sum('global_daily_market_bars' in s for s in statements)==1
        assert all(c.technical_feature_snapshot.technical_input_source=='DAILY_MARKET_BAR_NSE' for c in result)
        assert all(c.technical_score==c.stage_b_score for c in result)
    assert result==GlobalScanner(None,store).enrich_candidates(initial)

@pytest.mark.parametrize('change',[{'open':None},{'high':None},{'low':None}])
def test_missing_latest_ohlc(change):
    rows=candles(range(100,140)); rows[-1]=rows[-1].model_copy(update=change)
    r=evaluate(rows)
    assert r.atr14 is r.adx14 is None and r.feature_readiness['ATR14']=='MISSING_OHLC'
    assert r.technical_score is not None


def test_conflict_cannot_be_compressed_out_of_ohlc_or_volume():
    rows=candles(range(100,140))
    duplicate=rows[-2].model_copy(update={'close':Decimal(999)})
    r=evaluate(rows+[duplicate])
    assert r.atr14 is r.adx14 is r.volume_ratio20 is None
    assert r.feature_readiness['VOLUME20']=='CONFLICTING'


def test_recovery_after_missing_candle_and_no_source_identity_guessing():
    rows=candles(range(100,160))
    rows[10]=rows[10].model_copy(update={'low':None})
    r=evaluate(rows)
    assert r.atr14==2 and r.adx14==100
    unknown=[b.model_copy(update={'global_instrument_id':UUID(int=2)}) for b in rows]
    assert evaluate(unknown).latest_price is None
    assert evaluate(rows,trusted_providers=frozenset()).latest_price is None
    assert evaluate([b.model_copy(update={'source_mode':'DEMO'}) for b in rows]).latest_price is None


def test_exchange_date_is_not_utc_observation_date():
    # At 20:00 UTC it is already the next exchange DATE. Retrieval is known.
    asof=datetime(2026,9,13,20,tzinfo=timezone.utc)
    row=candles([100])[0].model_copy(update={'trading_date':datetime(2026,9,14).date(),'retrieved_at':asof})
    r=TechnicalFeatureEngine().compute(STOCK,[],as_of=asof,currency='INR',daily_bar_history=[row])
    assert r.history_trading_start==r.history_trading_end==row.trading_date
    assert r.observation_count==1


def test_no_network_in_ohlcv_computation(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',Mock(side_effect=AssertionError('network')))
    rows=candles(range(100,160))
    engine=TechnicalFeatureEngine()
    before=[b.model_dump() for b in rows]
    first=engine.compute(STOCK,[],as_of=NOW,daily_bar_history=rows)
    assert first==engine.compute(STOCK,[],as_of=NOW,daily_bar_history=reversed(rows))
    assert before==[b.model_dump() for b in rows]


def test_stale_candles_expose_diagnostics_but_do_not_score():
    rows=candles(range(100,140))
    rows=[b.model_copy(update={'trading_date':b.trading_date-timedelta(days=20)}) for b in rows]
    r=evaluate(rows)
    assert r.atr14==2 and r.adx14==100 and r.feature_readiness['ATR14']=='STALE'
    assert r.technical_score is None and r.technical_state=='INSUFFICIENT_DATA'


@pytest.mark.asyncio
async def test_stage_b_score_change_is_volume_evidence_only_and_fallback_works():
    from app.global_scanner import GlobalScanner
    from app.persistence import SqliteResearchPersistence
    from test_global_scanner import instrument,persisted,scan
    store=SqliteResearchPersistence(); items=[instrument(1),instrument(2)]
    values=[100]*60+[104]
    for item in items:
        persisted(store,item,price=False)
        for p in history(values,UUID(item['globalInstrumentId'])):
            store.upsert_market_price_observation(p.model_copy(update={'provider':'YAHOO_FINANCE'}))
    initial=await scan(items,store)
    scanner=GlobalScanner(None,store)
    before={r.global_instrument_id:r for r in scanner.enrich_candidates(initial)}
    store.upsert_daily_market_bars(candles(values,volumes=[100]*60+[200]))
    after={r.global_instrument_id:r for r in scanner.enrich_candidates(initial)}
    assert after[UUID(int=2)]==before[UUID(int=2)]
    assert before[STOCK].pre_score==after[STOCK].pre_score
    assert after[STOCK].technical_score==min(100,before[STOCK].technical_score+5)
    assert after[STOCK].stage_b_score==after[STOCK].technical_score

@pytest.mark.parametrize('daily_count,age,reason',[(4,0,'NSE_DAILY_HISTORY_BELOW_20'),(40,20,'NSE_DAILY_HISTORY_STALE')])
def test_immature_or_stale_daily_source_does_not_disable_current_fallback(daily_count,age,reason):
    rows=candles(range(200,200+daily_count))
    rows=[b.model_copy(update={'trading_date':b.trading_date-timedelta(days=age)}) for b in rows]
    fallback=history(range(100,160))
    before=evaluate([],fallback)
    r=evaluate(rows,fallback)
    assert r.technical_input_source=='CLOSE_ONLY_FALLBACK' and reason in r.source_diagnostics
    assert r.technical_score==before.technical_score and r.latest_price==before.latest_price
    assert r.atr14 is r.adx14 is r.current_volume is None
    assert r.daily_bar_observation_count==daily_count


def test_short_daily_still_used_when_all_history_insufficient():
    r=evaluate(candles([100]*4),history([200]*3))
    assert r.technical_input_source=='DAILY_MARKET_BAR_NSE' and r.observation_count==4
    assert r.technical_state=='INSUFFICIENT_DATA'
