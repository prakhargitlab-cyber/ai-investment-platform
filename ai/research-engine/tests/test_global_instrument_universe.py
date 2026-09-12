from uuid import uuid4
import httpx
import pytest
from app.portfolio_orchestration import PortfolioResearchOrchestrator, PortfolioServiceUnavailableError
from app.repository import ResearchRepository
from app.settings import Settings

@pytest.mark.asyncio
async def test_active_global_equities_uses_read_only_filtered_endpoint():
    instrument_id = str(uuid4()); requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"instruments": [{"globalInstrumentId": instrument_id, "ticker": "MSFT", "exchange": "XNAS", "country": "US", "currency": "USD"}], "page": 0, "size": 100, "totalElements": 1})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client).active_global_equities()
    assert result[0]["globalInstrumentId"] == instrument_id and result[0]["ticker"] == "MSFT"
    assert requests[0].method == "GET" and requests[0].url.path == "/api/v1/instruments"
    assert dict(requests[0].url.params) == {"status": "ACTIVE", "assetType": "EQUITY", "page": "0", "size": "500"}
    await client.aclose()

@pytest.mark.asyncio
async def test_active_global_equities_handles_empty_and_non_success_without_writes():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"instruments": []})))
    assert await PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client).active_global_equities() == []
    await client.aclose()
    failed = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(PortfolioServiceUnavailableError):
        await PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=failed).active_global_equities()
    await failed.aclose()

@pytest.mark.asyncio
async def test_active_global_equities_aggregates_later_page_with_get_only():
    requests=[]; later=str(uuid4())
    def handler(request):
        requests.append(request)
        page = request.url.params.get("page")
        if page == "0": return httpx.Response(200, json={"instruments": [{"globalInstrumentId": str(uuid4())}] * 500, "totalElements": 501})
        return httpx.Response(200, json={"instruments": [{"globalInstrumentId": later, "ticker":"LATE"}], "totalElements": 501})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    values=await PortfolioResearchOrchestrator(ResearchRepository(Settings()), Settings(), client=client).active_global_equities()
    assert len(values)==501 and values[-1]["globalInstrumentId"]==later
    assert [request.url.params.get("page") for request in requests] == ["0", "1"]
    assert {request.method for request in requests} == {"GET"}
    await client.aclose()
