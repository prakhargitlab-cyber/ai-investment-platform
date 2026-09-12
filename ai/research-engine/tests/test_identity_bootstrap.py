from uuid import uuid4
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi.testclient import TestClient
from app import main
from app.models import StructuredInstrumentResolution
from app.structured_market import StructuredProviderError
from test_structured_market import provider, response, instrument

@pytest.mark.asyncio
@pytest.mark.parametrize('symbol,isin,name', [('ALPHA','INE111A01010','Alpha Components Limited'),('BETA','INE222A01010','Beta Engineering Limited'),('GAMMA','INE333A01010','Gamma Services Limited')])
async def test_trusted_official_identity_resolves_generically_without_research_acquisition(symbol,isin,name):
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        assert request.url.params['newsCount']=='0'
        return response({'quotes':[{'symbol':symbol+'.NS','exchange':'NSI','quoteType':'EQUITY','longname':name,'isin':isin}]})
    resolved=await provider(handler).resolve_instrument(instrument(canonicalName=name,isin=isin,structuredNseCandidateTicker=symbol+'.NS',structuredNseCandidateSource='VERIFIED_NSE'))
    assert resolved.provider_ticker==symbol+'.NS'
    assert resolved.status=='VERIFIED_NSE_CANDIDATE'
    assert calls==['/v1/finance/search']

@pytest.mark.asyncio
@pytest.mark.parametrize('case',['mismatched_isin','ambiguous','unavailable'])
async def test_mapping_validation_fails_closed(case):
    quote={'symbol':'ALPHA.NS','exchange':'NSI','quoteType':'EQUITY','longname':'Alpha Components Limited','isin':'INE111A01010'}
    if case=='mismatched_isin':quote['isin']='INE222A01010'
    def handler(request):return response({'quotes':[quote,quote] if case=='ambiguous' else [quote]},503 if case=='unavailable' else 200)
    with pytest.raises(StructuredProviderError,match='UNAVAILABLE' if case=='unavailable' else 'COMPANY_NOT_RESOLVED'):
        await provider(handler).resolve_instrument(instrument(canonicalName='Alpha Components Limited',isin='INE111A01010',structuredNseCandidateTicker='ALPHA.NS',structuredNseCandidateSource='VERIFIED_NSE'))

def test_internal_identity_route_never_collects_snapshot(monkeypatch):
    from datetime import datetime, timezone
    resolver=AsyncMock(return_value=StructuredInstrumentResolution(instrument_id=uuid4(),provider='YAHOO_FINANCE',provider_ticker='ALPHA.NS',company_name='Alpha Components Limited',confidence=.95,resolved_at=datetime.now(timezone.utc),status='VERIFIED_NSE_CANDIDATE'))
    collect=AsyncMock(side_effect=AssertionError('Research acquisition forbidden'))
    monkeypatch.setattr(main.portfolio_orchestrator.structured_provider,'resolve_instrument',resolver)
    monkeypatch.setattr(main.portfolio_orchestrator.structured_provider,'collect',collect)
    payload={'assetType':'EQUITY','isin':'INE111A01010','structuredNseCandidateSource':'VERIFIED_NSE','structuredNseCandidateTicker':'ALPHA.NS'}
    client=TestClient(main.app)
    assert client.post('/internal/v1/research/instruments/resolve-provider',json=payload).status_code==403
    assert client.post('/internal/v1/research/instruments/resolve-provider',json=payload,headers={'X-AIP-Service-Identity':'portfolio-service'}).status_code==200
    collect.assert_not_called()

@pytest.mark.asyncio
async def test_targeted_ensure_uses_mapping_added_after_initial_canonical_read():
    from app.repository import ResearchRepository
    from app.persistence import SqliteResearchPersistence
    from app.settings import Settings
    from app.portfolio_orchestration import PortfolioResearchOrchestrator
    from app.research_readiness_runtime import ResearchReadinessRuntime, RepositoryResearchReadinessAdapter
    from app.yahoo_mcp_acquisition import McpFirstResearchCapabilityExecutor
    from test_yahoo_mcp_acquisition import INSTRUMENT_ID, FakeGateway, FakeLegacy
    repository=ResearchRepository(settings=Settings(research_live_enabled=False,research_demo_enabled=False),persistence=SqliteResearchPersistence())
    orchestrator=PortfolioResearchOrchestrator(repository,repository.settings)
    metadata={'globalInstrumentId':str(INSTRUMENT_ID),'canonicalName':'Ready Limited','isin':'INE111A01010','assetType':'EQUITY','currency':'INR','country':'IN','primaryExchange':'NSE','primarySymbol':'READY','status':'ACTIVE','providerMappings':[{'provider':'NSE','providerSymbol':'READY','status':'VERIFIED','exchange':'NSE','currency':'INR'}]}
    assert orchestrator.register_global_profile_metadata(INSTRUMENT_ID,metadata)
    assert 'YAHOO_FINANCE' not in repository.profile(INSTRUMENT_ID).provider_instrument_ids
    metadata['providerMappings'].append({'provider':'YAHOO_FINANCE','providerSymbol':'READY.NS','status':'VERIFIED','exchange':'NSE','currency':'INR','resolutionSource':'YAHOO_FROM_VERIFIED_NSE'})
    assert orchestrator.register_global_profile_metadata(INSTRUMENT_ID,metadata)
    class VerifiedGateway(FakeGateway):
        async def acquire_requirement(self,profile,**kwargs):
            assert profile.instrument_id==INSTRUMENT_ID
            assert profile.provider_instrument_ids['YAHOO_FINANCE']=='READY.NS'
            return await super().acquire_requirement(profile,**kwargs)
    gateway=VerifiedGateway()
    legacy=FakeLegacy()
    executor=McpFirstResearchCapabilityExecutor(legacy,repository,gateway,enabled=True)
    runtime=ResearchReadinessRuntime(repository,RepositoryResearchReadinessAdapter(repository),executor)
    result=await runtime.ensure(INSTRUMENT_ID,jurisdiction='INDIA',requirement_ids=['LATEST_PRICE'])
    assert 'VERIFIED_YAHOO_MAPPING_REQUIRED' not in str(result.failures)
    assert gateway.calls
    assert repository.market_price_observations_for({INSTRUMENT_ID})[INSTRUMENT_ID]
    assert repository.financial_facts_for(INSTRUMENT_ID)==[]
