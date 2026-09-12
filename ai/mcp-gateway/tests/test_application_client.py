from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.application_client import HttpApplicationResearchReader
from app.contracts import McpErrorCode, McpGatewayError
from conftest import INSTRUMENT_ID


@pytest.mark.asyncio
async def test_http_reader_uses_only_persisted_read_routes_and_propagates_correlation(
    settings, auth, monkeypatch
) -> None:
    captured: list[httpx.Request] = []
    monkeypatch.setattr(
        "app.application_client.inject",
        lambda headers: headers.update({"traceparent": "00-00000000000000000000000000001234-0000000000005678-01"}),
    )

    def transport(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"globalInstrumentId": str(INSTRUMENT_ID), "overallStatus": "READY"},
        )

    client = httpx.AsyncClient(
        base_url="http://research-engine",
        transport=httpx.MockTransport(transport),
    )
    reader = HttpApplicationResearchReader(settings, client=client)
    result = await reader.invoke(
        "get_research_readiness",
        {"globalInstrumentId": str(INSTRUMENT_ID)},
        auth,
        "request-reader",
    )
    assert result["overallStatus"] == "READY"
    assert captured[0].url.path == f"/api/v1/research/readiness/{INSTRUMENT_ID}"
    assert "ensure" not in captured[0].url.path and "refresh" not in captured[0].url.path
    assert captured[0].headers["x-request-id"] == "request-reader"
    assert captured[0].headers["x-correlation-id"] == "request-reader"
    assert captured[0].headers["traceparent"].startswith("00-00000000000000000000000000001234")
    await client.aclose()


@pytest.mark.asyncio
async def test_news_projection_uses_publication_window_and_provenance(settings, auth) -> None:
    now = datetime.now(timezone.utc)

    def transport(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "recentEvents": [
                    {
                        "eventId": "current",
                        "title": "Current filing",
                        "publishedAt": (now - timedelta(days=2)).isoformat(),
                        "sourceUrl": "https://example.test/current",
                        "sourceType": "EXCHANGE_FILING",
                    },
                    {
                        "eventId": "historical-governance",
                        "title": "Historical event",
                        "eventDate": (now - timedelta(days=45)).isoformat(),
                        "sourceType": "GOVERNANCE_HISTORY",
                    },
                ]
            },
        )

    client = httpx.AsyncClient(base_url="http://research-engine", transport=httpx.MockTransport(transport))
    reader = HttpApplicationResearchReader(settings, client=client)
    result = await reader.invoke(
        "get_recent_news",
        {"globalInstrumentId": str(INSTRUMENT_ID), "days": 30},
        auth,
        "news-request",
    )
    assert [item["eventId"] for item in result["news"]] == ["current"]
    assert result["news"][0]["publicationDate"]
    assert result["news"][0]["dateBasis"] == "PUBLISHED_AT"
    assert result["news"][0]["source"]["type"] == "EXCHANGE_FILING"
    await client.aclose()


@pytest.mark.asyncio
async def test_analysis_and_sector_routes_never_use_refresh_or_ensure(settings, auth) -> None:
    captured: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.startswith("/api/v1/research/analysis/"):
            return httpx.Response(
                200,
                json={
                    "globalInstrumentId": str(INSTRUMENT_ID),
                    "ruleEngineVersion": "STOCK_RULE_ENGINE_V1",
                },
            )
        return httpx.Response(200, json={"region": "INDIA", "sector": "FINANCIAL_SERVICES"})

    client = httpx.AsyncClient(base_url="http://research-engine", transport=httpx.MockTransport(transport))
    reader = HttpApplicationResearchReader(settings, client=client)
    analysis = await reader.invoke(
        "get_company_analysis",
        {"globalInstrumentId": str(INSTRUMENT_ID), "allowPartial": False},
        auth,
        "analysis-request",
    )
    sector = await reader.invoke(
        "get_sector_performance",
        {"region": "INDIA", "sector": "FINANCIAL_SERVICES", "period": "MONTH", "limit": 5},
        auth,
        "sector-request",
    )
    assert analysis["ruleEngineVersion"] == "STOCK_RULE_ENGINE_V1"
    assert sector["sector"] == "FINANCIAL_SERVICES"
    assert [request.url.path for request in captured] == [
        f"/api/v1/research/analysis/{INSTRUMENT_ID}",
        "/api/v1/research/sector-performance",
    ]
    assert all("ensure" not in request.url.path and "refresh" not in request.url.path for request in captured)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("get_research_readiness", McpErrorCode.READINESS_NOT_AVAILABLE),
        ("get_company_analysis", McpErrorCode.ANALYSIS_NOT_AVAILABLE),
    ],
)
async def test_missing_read_models_map_to_deterministic_errors(settings, auth, tool, expected) -> None:
    client = httpx.AsyncClient(
        base_url="http://research-engine",
        transport=httpx.MockTransport(lambda _request: httpx.Response(404, json={"detail": "not available"})),
    )
    reader = HttpApplicationResearchReader(settings, client=client)
    with pytest.raises(McpGatewayError) as error:
        await reader.invoke(
            tool,
            {"globalInstrumentId": str(INSTRUMENT_ID)},
            auth,
            "missing-read-model",
        )
    assert error.value.code == expected
    await client.aclose()


@pytest.mark.asyncio
async def test_unresolved_canonical_identity_is_distinguished(settings, auth) -> None:
    client = httpx.AsyncClient(
        base_url="http://research-engine",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(404, json={"detail": "COMPANY_NOT_RESOLVED"})
        ),
    )
    reader = HttpApplicationResearchReader(settings, client=client)
    with pytest.raises(McpGatewayError) as error:
        await reader.invoke(
            "get_research_readiness",
            {"globalInstrumentId": str(INSTRUMENT_ID)},
            auth,
            "unresolved-company",
        )
    assert error.value.code == McpErrorCode.COMPANY_NOT_RESOLVED
    await client.aclose()
