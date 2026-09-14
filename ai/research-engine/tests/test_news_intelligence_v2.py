from dataclasses import replace
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from uuid import UUID
import sqlite3
import pytest
from app.business_exposure import *
from app.news_intelligence import *
from app.models import ResearchDocument
from app.persistence import SqliteResearchPersistence

NOW=datetime(2026,9,14,tzinfo=timezone.utc)
KEY=UUID(int=71)

def profile(text='Our key raw materials include copper. The company uses copper.',classification='OFFICIAL_COMPANY',confidence=.95):
    source=SourceReference(document_id=UUID(int=81),url='https://issuer.test/report',classification=classification,
        publication_time=NOW-timedelta(days=10),retrieved_at=NOW-timedelta(days=3),confidence=confidence)
    return extract_profile(KEY,'Generic Cable Limited','GCBL',[BusinessEvidence(instrument_id=KEY,text=text,source=source)],
        now=NOW-timedelta(days=3),industry='Wires and cables')

def document(text='Copper reaches record high, pressure builds on cable makers',classification='REPUTABLE_NEWS'):
    return ResearchDocument(document_id=UUID(int=82),canonical_url='https://publisher.test/article',original_url='https://publisher.test/article',
        title=text,normalized_text=text,content_hash=fingerprint(text),source_type='RSS',source_classification=classification,
        source_name='Publisher',content_type='text/plain',document_type='TEXT',reliability_level='LEVEL_C',source_mode='REAL',
        published_at=NOW-timedelta(days=2),retrieved_at=NOW,discovered_at=NOW,instrument_id=KEY,company_id=KEY)

def outcome(provider='web',state='SUCCESS_EMPTY',count=0):
    return ProviderOutcome(provider=provider,outcome=state,candidate_count=count,queries_planned=1,
        queries_completed=0 if state in {'FAILED','DEGRADED'} else 1)

def run(outcomes=None,count=0):
    return aggregate_search(KEY,outcomes or [outcome()],started_at=NOW,completed_at=NOW,qualifying_events=count)

def test_generic_extraction_provenance_confidence():
    p=profile()
    assert p.exposures[0].normalized_key=='COPPER'
    assert p.exposures[0].direction_when_price_rises==-1
    assert p.exposures[0].confidence==.92
    assert p.exposures[0].source_references[0].document_id==UUID(int=81)
    assert p==profile()

@pytest.mark.parametrize('text',['We manufacture cables.','Copper prices rose yesterday.','The company does not use copper.','We use an unknown material.'])
def test_unsupported_not_invented(text):
    assert not profile(text).exposures

@pytest.mark.parametrize('word,key',[('aluminum','ALUMINIUM'),('polyvinyl chloride','PVC'),('natural rubber','RUBBER'),('petroleum coke','PETCOKE')])
def test_ontology_normalization(word,key):
    assert profile('The company uses '+word).exposures[0].normalized_key==key

def test_lower_tier_not_high_confidence():
    assert profile(classification='OTHER').exposures[0].confidence==.4
    assert profile(confidence=0).exposures[0].confidence==0

def test_tiered_queries_deterministic_bounded():
    plan=query_plan(profile())
    assert plan==query_plan(profile()) and len(plan)<=14
    assert (1,'GCBL') in plan
    assert any(t==2 and 'order' in q for t,q in plan)
    assert any(t==3 and 'copper' in q for t,q in plan)
    assert any(t==4 and 'copper' in q and 'Wires' in q for t,q in plan)
    assert len({q.lower() for _,q in plan})==len(plan)
    other=profile().model_copy(update={'company_name':'Another Manufacturer','ticker':'OTHER'})
    assert all('Generic' not in q for _,q in query_plan(other))

@pytest.mark.parametrize('states,count,expected',[
    (['SUCCESS_WITH_RESULTS'],1,'SEARCH_COMPLETE_WITH_EVENTS'),
    (['SUCCESS_EMPTY'],0,'SEARCH_COMPLETE_NO_EVENTS'),
    (['FAILED'],0,'SEARCH_FAILED'),(['DEGRADED'],0,'SEARCH_FAILED'),
    (['SUCCESS_EMPTY','SUCCESS_WITH_RESULTS'],1,'SEARCH_COMPLETE_WITH_EVENTS'),
    (['SUCCESS_EMPTY','SUCCESS_EMPTY'],0,'SEARCH_COMPLETE_NO_EVENTS'),
    (['SUCCESS_WITH_RESULTS','DEGRADED'],1,'SEARCH_PARTIAL'),
    (['SUCCESS_EMPTY','FAILED'],0,'SEARCH_PARTIAL'),(['FAILED','FAILED'],0,'SEARCH_FAILED'),
    (['PARTIAL'],0,'SEARCH_PARTIAL')])
def test_provider_aggregation(states,count,expected):
    rows=[outcome('yahoo' if i==0 else 'web',s,1 if s=='SUCCESS_WITH_RESULTS' else 0) for i,s in enumerate(states)]
    assert run(rows,count).outcome==expected

def test_generic_cable_regression():
    p=profile(); d=document(); f=extract_impacts(p,d,now=NOW)[0]
    assert f.event_type=='INPUT_COST_INCREASE' and f.relevance_type=='SECTOR_EXPOSURE'
    assert not f.company_mentioned and f.exposure_key=='COPPER' and f.relevance==.8
    assert f.direction==-1 and f.impact_score<0 and not f.severe_validated
    assert impact_at(f,NOW+timedelta(days=7))<0
    assert impact_at(f,NOW+timedelta(days=30)) is None
    assert f.short_term and f.medium_term and not f.long_term

@pytest.mark.parametrize('text,kind,sign',[
    ('Copper prices decrease','INPUT_COST_DECREASE',1),
    ('Generic Cable Limited demand increases','DEMAND_INCREASE',1),
    ('Generic Cable Limited capacity expansion','CAPACITY_EXPANSION',1),
    ('Generic Cable Limited guidance cut','GUIDANCE_CUT',-1),
    ('Generic Cable Limited selling price increase','COMPANY_PRICE_INCREASE',1),
    ('Generic Cable Limited guidance maintained','GUIDANCE_MAINTAINED',1)])
def test_event_types_and_company_specific_sign(text,kind,sign):
    features=extract_impacts(profile(),document(text),now=NOW)
    assert features[0].event_type==kind and features[0].direction==sign

def test_body_only_and_competitor_relevance():
    d=document('Expansion news'); d.normalized_text='Generic Cable Limited announces capacity expansion'
    assert extract_impacts(profile(),d,now=NOW)[0].relevance_type=='DIRECT_COMPANY'
    assert not extract_impacts(profile(),document('Rival Limited announces capacity expansion'),now=NOW)
    assert not extract_impacts(profile(),document('Gold prices rise'),now=NOW)

def test_severe_only_direct_official_and_no_commodity_override():
    for classification,expected in [('OFFICIAL_COMPANY',True),('OTHER',False),('REPUTABLE_NEWS',False)]:
        f=extract_impacts(profile(),document('Generic Cable Limited confirmed fraud',classification),now=NOW)[0]
        assert f.severe_validated==expected

def test_impact_formula_decay_and_contradictory_evidence():
    negative=extract_impacts(profile(),document(),now=NOW)[0]
    positive=extract_impacts(profile(),document('Generic Cable Limited margin guidance maintained'),now=NOW)[0]
    assert negative.impact_score==pytest.approx(-100*.5*.8*.75*.75)
    assert aggregate_impact([positive,negative],NOW)==aggregate_impact([negative,positive],NOW)
    assert aggregate_impact([positive,negative],NOW)>aggregate_impact([negative],NOW)
    assert aggregate_impact([negative,negative],NOW)==aggregate_impact([negative],NOW)

def test_lookahead_and_separate_timestamps():
    f=extract_impacts(profile(),document(),now=NOW)[0]
    assert f.publication_time==NOW-timedelta(days=2)
    assert f.discovered_at==f.computed_at==NOW
    assert impact_at(f,NOW-timedelta(hours=1)) is None
    future=profile().model_copy(update={'public_available_at':NOW+timedelta(days=1)})
    assert not extract_impacts(future,document(),now=NOW)

@pytest.mark.parametrize('state,expected',[('SUCCESS_EMPTY','READY_NO_EVENTS'),('FAILED','FAILED_SEARCH'),('PARTIAL','PARTIAL_SEARCH')])
def test_search_readiness(state,expected):
    r=run([outcome(state=state)])
    assert search_state(r,NOW)==expected
    assert search_state(r,NOW+timedelta(days=2))=='STALE_SEARCH'

def test_append_only_storage_and_provenance():
    store=SqliteResearchPersistence()
    p=profile(); d=document(); f=extract_impacts(p,d,now=NOW)[0]
    store.upsert_document(d)
    store.append_news_record(p); store.append_news_record(f); store.append_news_record(run())
    assert store.append_news_record(f)==f
    assert store.load_news_records(EventImpactFeature,KEY,as_of=NOW)==[f]
    assert not store.load_news_records(EventImpactFeature,KEY,as_of=NOW-timedelta(seconds=1))
    with pytest.raises(ValueError,match='IMMUTABLE'): store.append_news_record(f.model_copy(update={'impact_score':0}))
    with pytest.raises(sqlite3.IntegrityError): store._connection.execute('UPDATE research_event_impact_features SET impact_score=0')
    with pytest.raises(sqlite3.IntegrityError): store._connection.execute('DELETE FROM company_business_exposure_profiles')

def test_v1_zero_vs_missing_negative_and_severe(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('network'))
    from test_stock_rule_engine import _inputs
    from app.stock_rule_engine import StockRuleEngineV1
    engine=StockRuleEngineV1(); base=replace(_inputs(events=[]),evaluated_at=NOW)
    neutral=engine._news(replace(base,news_search_run=run()))
    assert neutral.raw_score==50
    missing=engine._news(replace(base,news_search_run=run([outcome(state='FAILED')])))
    assert missing.raw_score is None
    f=extract_impacts(profile(),document(),now=NOW)[0]
    value=replace(base,news_features=(f,))
    assert engine._news(value).raw_score<50
    assert not engine._risk_overrides(value)
    severe=extract_impacts(profile(),document('Generic Cable Limited confirmed fraud','OFFICIAL_COMPANY'),now=NOW)[0]
    assert engine._risk_overrides(replace(base,news_features=(severe,)))
    assert engine.input_fingerprint(value,allow_partial=False)==engine.input_fingerprint(value,allow_partial=False)

def test_missing_news_not_full_analysis_blocker():
    from test_stock_rule_engine import _readiness
    from app.stock_rule_engine import StockRuleEngineEligibilityPolicy
    from app.research_readiness import ResearchRequirementStatus
    r=_readiness({'CURRENT_NEWS':ResearchRequirementStatus.FAILED})
    assert StockRuleEngineEligibilityPolicy().evaluate(r).full_analysis_allowed


def test_append_revision_does_not_leak_into_previous_evaluation():
    from uuid import uuid4
    original=extract_impacts(profile(),document('Generic Cable Limited confirmed fraud','OFFICIAL_COMPANY'),now=NOW)[0]
    later=NOW+timedelta(days=2)
    correction=original.model_copy(update={'feature_id':uuid4(),'computed_at':later,'discovered_at':later,
        'public_available_at':later,'impact_score':0,'direction':0,'severe_validated':False,'evidence_fingerprint':'revision2'})
    assert latest_known_features([correction,original],NOW)==[original]
    assert latest_known_features([original,correction],later)==[correction]
    assert aggregate_impact([original,correction],later)==0
    store=SqliteResearchPersistence(); store.upsert_document(document()); store.append_news_record(profile())
    store.append_news_record(original); store.append_news_record(correction)
    assert len(store.load_news_records(EventImpactFeature,KEY,as_of=later))==2
    assert store.load_news_records(EventImpactFeature,KEY,as_of=NOW)==[original]


def test_live_event_survives_stale_search_with_partial_coverage():
    from test_stock_rule_engine import _inputs, _readiness
    from app.stock_rule_engine import StockRuleEngineV1
    from app.research_readiness import ResearchRequirementStatus
    f=extract_impacts(profile(),document(),now=NOW)[0]
    stale=run().model_copy(update={'completed_at':NOW-timedelta(days=2)})
    value=replace(_inputs(events=[]),evaluated_at=NOW,news_search_run=stale,news_features=(f,),
        readiness=_readiness({'CURRENT_NEWS':ResearchRequirementStatus.READY_STALE}))
    news=StockRuleEngineV1()._news(value)
    assert news.status=='PARTIAL' and news.raw_score<50


@pytest.mark.parametrize('kind',['missing','zero','adverse','positive','severe'])
def test_ranker_news_renormalization_and_risk_gate(kind,monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('PROVIDER_CALL_DURING_RANKING'))
    from test_global_opportunity_ranker import inputs, area
    from app.global_opportunity_ranker import GlobalOpportunityRanker
    from test_stock_rule_engine import _inputs
    from app.stock_rule_engine import StockRuleEngineV1
    candidate,rule=inputs(n=71)
    value=replace(_inputs(events=[]),evaluated_at=NOW)
    if kind=='missing': value=replace(value,news_search_run=run([outcome(state='FAILED')]))
    elif kind=='zero': value=replace(value,news_search_run=run())
    else:
        text={'adverse':'Copper reaches record high','positive':'Copper prices decrease',
            'severe':'Generic Cable Limited confirmed fraud'}[kind]
        feature=extract_impacts(profile(),document(text,'OFFICIAL_COMPANY' if kind=='severe' else 'REPUTABLE_NEWS'),now=NOW)[0]
        value=replace(value,news_features=(feature,))
    engine=StockRuleEngineV1()
    rule.area_scores=[a for a in rule.area_scores if a.area!='NEWS_GEOPOLITICAL_EVENTS']+[engine._news(value)]
    rule.risk_overrides=engine._risk_overrides(value)
    result=GlobalOpportunityRanker().score(candidate,rule)
    assert result.rank_eligible==(kind!='severe')
    assert result.score_coverage==(93 if kind=='missing' else 100)
    assert GlobalOpportunityRanker().score(candidate,rule)==result
    if kind=='zero': assert area(rule,'NEWS_GEOPOLITICAL_EVENTS').raw_score==50
    if kind=='adverse': assert area(rule,'NEWS_GEOPOLITICAL_EVENTS').raw_score<50
    if kind=='positive': assert area(rule,'NEWS_GEOPOLITICAL_EVENTS').raw_score>50
