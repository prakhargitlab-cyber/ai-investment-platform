from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from app.contracts import McpAuthContext, McpAuthenticationType
from app.server import McpGatewayContainer, create_container
from app.settings import McpGatewaySettings


INSTRUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
WATCHLIST_ID = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)


class FakeApplicationReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], McpAuthContext, str]] = []
        self.provider_calls = 0
        self.closed = False
        self.raise_error: Exception | None = None

    async def invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        auth: McpAuthContext,
        request_id: str,
    ) -> dict[str, Any]:
        self.calls.append((tool, arguments, auth, request_id))
        if self.raise_error is not None:
            raise self.raise_error
        instrument_id = arguments.get("globalInstrumentId")
        responses: dict[str, dict[str, Any]] = {
            "get_research_readiness": {
                "globalInstrumentId": instrument_id,
                "overallStatus": "READY",
                "requirements": [{"requirementId": "QUARTERLY_FINANCIALS", "status": "READY_FRESH"}],
                "generatedAt": NOW.isoformat(),
            },
            "get_company_analysis": {
                "globalInstrumentId": instrument_id,
                "ruleEngineVersion": "STOCK_RULE_ENGINE_V1",
                "score": 78,
                "decision": "RESEARCH_READY",
                "generatedAt": NOW.isoformat(),
            },
            "get_financial_facts": {
                "globalInstrumentId": instrument_id,
                "financialResultHistory": [{"period": "2026-Q2", "revenue": 100}],
            },
            "get_quarterly_results": {
                "globalInstrumentId": instrument_id,
                "latestQuarterlyResult": {"period": "2026-Q2", "revenue": 100},
            },
            "get_shareholding": {
                "globalInstrumentId": instrument_id,
                "shareholdingSnapshots": [{"period": "2026-Q2", "promoterPercent": 51.0}],
            },
            "get_recent_news": {
                "globalInstrumentId": instrument_id,
                "days": arguments.get("days", 30),
                "news": [
                    {
                        "title": "Quarterly results published",
                        "publicationDate": NOW.isoformat(),
                        "source": {"type": "EXCHANGE_FILING", "url": "https://example.test/filing"},
                    }
                ],
            },
            "get_sector_performance": {
                "region": arguments.get("region"),
                "sector": arguments.get("sector"),
                "period": arguments.get("period"),
                "leaders": [{"globalInstrumentId": str(INSTRUMENT_ID), "returnPercent": 3.1}],
                "asOf": NOW.isoformat(),
            },
            "search_research_evidence": {
                "globalInstrumentId": instrument_id,
                "query": arguments.get("query"),
                "matches": [{"documentId": "doc-1", "sourceName": "NSE"}],
            },
            "get_watchlist": {
                "watchlistId": arguments.get("watchlistId"),
                "userId": str(auth.user_id) if auth.user_id else None,
                "instruments": [{"globalInstrumentId": str(INSTRUMENT_ID)}],
            },
        }
        return responses[tool]

    async def close(self) -> None:
        self.closed = True


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **kwargs: Any) -> None:
        self.events.append({"event": event, **kwargs})


@pytest.fixture
def settings() -> McpGatewaySettings:
    return McpGatewaySettings(
        AIP_ENVIRONMENT="TEST",
        AIP_MCP_SERVICE_IDENTITY="mcp-test-service",
        AIP_MCP_AUTHENTICATION_TYPE="TEST",
        AIP_MCP_SCOPES="mcp:read,watchlist:read",
        AIP_MCP_LOCAL_USER_ID=str(USER_ID),
        AIP_MCP_INVOCATION_TIMEOUT_SECONDS="0.1",
    )


@pytest.fixture
def auth() -> McpAuthContext:
    return McpAuthContext(
        userId=USER_ID,
        serviceIdentity="mcp-test-service",
        roles=("MCP_READER",),
        scopes=("mcp:read", "watchlist:read"),
        authenticationType=McpAuthenticationType.TEST,
    )


@pytest.fixture
def reader() -> FakeApplicationReader:
    return FakeApplicationReader()


@pytest.fixture
def audit() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
def container(settings, reader, audit) -> McpGatewayContainer:
    return create_container(settings, reader=reader, audit=audit)
