from types import SimpleNamespace
from uuid import UUID
import pytest
from app.news_acquisition import acquire_news
from app.source_discovery import CandidateSearchResult, DisabledSearchDiscoveryProvider
from app.persistence import SqliteResearchPersistence
from app.news_intelligence import SearchRun, EventImpactFeature
from app.business_exposure import extract_profile, CompanyBusinessExposureProfile
from test_news_intelligence_v2 import NOW, KEY, document, profile


class Provider:
    def __init__(self,name='web',results=(),fail=False,degraded=False):
        self.provider_name=name; self.results=results; self.fail=fail; self.calls=[]; self.last_query_degraded=degraded
    async def discover(self,company,category,window):
        self.calls.append(window.explicit_queries)
        if self.fail: raise RuntimeError('SECRET_MUST_NOT_ESCAPE')
        return list(self.results)


class Repository:
    def __init__(self):
        self.settings=SimpleNamespace(market_data_population_request_interval_seconds=1.25)
        self._persistence=SqliteResearchPersistence(); self.documents={}; self.fetches=[]
        d=document('Our principal raw material is copper.','OFFICIAL_COMPANY')
        d.document_id=UUID(int=83); d.canonical_url='https://issuer.test/report'
        self.documents[d.document_id]=d; self._persistence.upsert_document(d)
        self._fetcher=SimpleNamespace(fetch=self.fetch)
    def documents_for(self,*a,**k): return list(self.documents.values())
    def structured_market_snapshots_for(self,keys): return {k:[] for k in keys}
    async def append_news_record(self,value): return self._persistence.append_news_record(value)
    async def _run_blocking_persistence(self,fn,*args): return fn(*args)
    async def fetch(self,url):
        self.fetches.append(url)
        return SimpleNamespace(text='<html><title>Copper reaches record high</title><body>Pressure builds on cable makers.</body></html>',
            content_type='text/html',final_url=url)


def company():
    from test_research_readiness_runtime import _profile
    return _profile(KEY).model_copy(update={'company_id':KEY,'company_name':'Generic Cable Limited','ticker':'GCBL'})
def candidate(url='https://publisher.test/news'):
    return CandidateSearchResult('Copper reaches record high',url,'snippet is not evidence',NOW,'web','id','copper wires','CURRENT_NEWS')
async def no_sleep(seconds): pass


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected',[('empty','SEARCH_COMPLETE_NO_EVENTS'),('fail','SEARCH_FAILED'),('disabled','SEARCH_FAILED'),('degraded','SEARCH_PARTIAL')])
async def test_worker_search_outcomes(kind,expected):
    provider=DisabledSearchDiscoveryProvider() if kind=='disabled' else Provider(fail=kind=='fail',degraded=kind=='degraded')
    repo=Repository()
    run,features=await acquire_news(repo,company(),providers=[provider],now=NOW,sleep=no_sleep)
    assert run.outcome==expected and features==[]
    assert 'SECRET' not in run.model_dump_json()
    assert repo._persistence.load_news_records(SearchRun,KEY,as_of=NOW)==[run]


@pytest.mark.asyncio
async def test_worker_exposure_queries_persist_features_and_bounded_spacing():
    repo=Repository(); provider=Provider(results=[candidate()]); spacing=[]
    async def sleep(seconds): spacing.append(seconds)
    run,features=await acquire_news(repo,company(),providers=[provider],industry='Wires and cables',now=NOW,sleep=sleep,max_queries=6)
    assert run.outcome=='SEARCH_COMPLETE_WITH_EVENTS'
    assert features and features[0].relevance_type=='SECTOR_EXPOSURE'
    assert any('copper' in q[0].lower() for q in provider.calls)
    assert len(provider.calls)==6 and spacing==[1.25]*6
    assert len(repo.fetches)==1
    assert len(repo._persistence.load_news_records(EventImpactFeature,KEY,as_of=NOW))==1
    again,second=await acquire_news(repo,company(),providers=[provider],industry='Wires and cables',now=NOW,sleep=no_sleep,max_queries=6)
    assert len(repo._persistence.load_news_records(EventImpactFeature,KEY,as_of=NOW))==1
    assert features==second


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['fetch','budget'])
async def test_document_incompleteness_never_certifies_no_events(failure):
    repo=Repository(); provider=Provider(results=[candidate(),candidate('https://publisher.test/second')])
    if failure=='fetch':
        async def broken(url): raise RuntimeError('private error')
        repo._fetcher.fetch=broken
    run,_=await acquire_news(repo,company(),providers=[provider],now=NOW,sleep=no_sleep,max_documents=1)
    assert run.outcome=='SEARCH_PARTIAL'


@pytest.mark.asyncio
async def test_empty_yahoo_does_not_short_circuit_web():
    repo=Repository(); yahoo=Provider('yahoo'); web=Provider('web',[candidate()])
    result,features=await acquire_news(repo,company(),providers=[yahoo,web],now=NOW,sleep=no_sleep)
    assert result.outcome=='SEARCH_COMPLETE_WITH_EVENTS' and features
    assert yahoo.calls and web.calls


def test_labelled_profile_fields_and_negation():
    p=profile('Products: cables, wires. Business segments: industrial. Competitors: Other Ltd. We use copper.')
    assert p.products_services==['cables','wires']
    assert p.business_segments==['industrial']
    assert p.competitor_names==['Other Ltd']
    from app.news_intelligence import extract_impacts
    assert not extract_impacts(p,document('Generic Cable Limited denies confirmed fraud','OFFICIAL_COMPANY'),now=NOW)


@pytest.mark.asyncio
async def test_persisted_yahoo_empty_plus_web_events_requires_no_yahoo_call():
    repo=Repository()
    repo.acquisition_observations_for=lambda key:[dict(requirement_id='CURRENT_NEWS',provider='YAHOO_FINANCE_MCP',
        observed_at=NOW.isoformat(),outcome='SUCCESS_EMPTY')]
    result,features=await acquire_news(repo,company(),providers=[Provider(results=[candidate()])],now=NOW,sleep=no_sleep)
    assert result.outcome=='SEARCH_COMPLETE_WITH_EVENTS' and features
    assert {p.provider:p.outcome for p in result.providers}['YAHOO_FINANCE_MCP']=='SUCCESS_EMPTY'


def test_competitor_action_in_same_document_not_attributed_to_company():
    from app.news_intelligence import extract_impacts
    assert not extract_impacts(profile(),document('Generic Cable Limited reported sales. Other Manufacturer confirmed fraud.'),now=NOW)
