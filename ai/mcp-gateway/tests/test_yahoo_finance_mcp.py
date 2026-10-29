from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp import Client
from pydantic import SecretStr
from starlette.testclient import TestClient

from app.contracts import McpErrorCode, McpGatewayError
from app.external import ExternalCapabilityState, McpServerRegistry
from app.server import create_container, create_mcp_server
from app.settings import McpGatewaySettings
from app.yahoo_finance_mcp import (
    PROVIDER_ID,
    TOOL_SCHEMA_VERSION,
    OfficialYahooMcpClientFactory,
    YahooFinanceCapabilityRegistry,
    YahooFinanceMcpConfig,
    YahooFinanceMcpProvider,
)
from conftest import FakeApplicationReader, INSTRUMENT_ID, NOW, RecordingAudit
from fake_yahoo_mcp_server import create_fake_yahoo_mcp_server


def mapping(requirement: str, capability: str, tool: str, **overrides):
    value = {
        "region": "INDIA",
        "requirementId": requirement,
        "capability": capability,
        "tool": tool,
        "state": "SUPPORTED",
    }
    value.update(overrides)
    return value


def payload(**overrides):
    value = {
        "schemaVersion": TOOL_SCHEMA_VERSION,
        "globalInstrumentId": str(INSTRUMENT_ID),
        "symbol": "READY.NS",
        "exchange": "NSE",
        "currency": "INR",
        "asOf": NOW.isoformat(),
        "sourceUrl": "https://finance.yahoo.com/quote/READY.NS",
        "price": "250.50",
    }
    value.update(overrides)
    return value


class FakeClient:
    def __init__(self, response, *, tools=("get_quote",), delay=0):
        self.response = response
        self.tools = tools
        self.delay = delay
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.tools])

    async def call_tool(self, name, arguments=None, **_kwargs):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(is_error=False, structured_content=self.response)


class FakeFactory:
    def __init__(self, client):
        self.client = client
        self.request_ids = []

    @asynccontextmanager
    async def connect(self, request_id):
        self.request_ids.append(request_id)
        yield self.client


class InMemoryServerFactory:
    def __init__(self, server):
        self.server = server

    @asynccontextmanager
    async def connect(self, _request_id):
        async with Client(self.server) as client:
            yield client


def provider(capability, response=None, *, delay=0, clock=lambda: NOW):
    capabilities = YahooFinanceCapabilityRegistry.from_json(json.dumps([capability]))
    client = FakeClient(response or payload(), tools=(capability["tool"],), delay=delay)
    adapter = YahooFinanceMcpProvider(
        YahooFinanceMcpConfig(
            transport="stdio",
            endpoint=None,
            stdio_command="fake-yahoo-mcp",
            stdio_args=(),
            auth_type="NONE",
            auth_header_name="Authorization",
            auth_token=None,
            stdio_token_env_name=None,
            timeout_seconds=0.05,
            max_retries=0,
            retry_backoff_seconds=0,
            max_concurrency=2,
        ),
        capabilities,
        client_factory=FakeFactory(client),
        clock=clock,
    )
    return adapter, client


def arguments(requirement="LATEST_PRICE", **overrides):
    value = {
        "globalInstrumentId": str(INSTRUMENT_ID),
        "region": "INDIA",
        "requirementId": requirement,
        "providerSymbol": "READY.NS",
        "expectedExchange": "XNSE",
        "expectedCurrency": "INR",
    }
    value.update(overrides)
    return value


def test_yahoo_provider_registration_is_explicit_and_contains_no_cancelled_provider() -> None:
    adapter, _ = provider(mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"))
    registry = McpServerRegistry()
    registry.register(adapter)
    assert registry.provider_ids == (PROVIDER_ID,)
    assert "ZERODHA" not in registry.provider_ids
    assert "ALPHA_VANTAGE" not in registry.provider_ids
    assert adapter.metadata.priority == 1
    assert adapter.metadata.risk_class.value == "SAFE_READ"


@pytest.mark.asyncio
async def test_http_client_propagates_request_correlation_and_trace_context(monkeypatch) -> None:
    captured = {}

    class FakeHttpClient:
        def __init__(self, *, headers, timeout):
            captured["headers"] = headers
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeSdkClient:
        def __init__(self, transport, **_kwargs):
            captured["transport"] = transport

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("app.yahoo_finance_mcp.httpx2.AsyncClient", FakeHttpClient)
    monkeypatch.setattr(
        "app.yahoo_finance_mcp.streamable_http_client",
        lambda endpoint, **_kwargs: ("transport", endpoint),
    )
    monkeypatch.setattr("app.yahoo_finance_mcp.Client", FakeSdkClient)
    monkeypatch.setattr(
        "app.yahoo_finance_mcp.inject",
        lambda headers: headers.update(
            {"traceparent": "00-00000000000000000000000000001234-0000000000005678-01"}
        ),
    )
    factory = OfficialYahooMcpClientFactory(
        YahooFinanceMcpConfig(
            transport="streamable-http",
            endpoint="http://yahoo-finance-mcp/mcp",
            stdio_command=None,
            stdio_args=(),
            auth_type="NONE",
            auth_header_name="Authorization",
            auth_token=None,
            stdio_token_env_name=None,
            timeout_seconds=3,
            max_retries=0,
            retry_backoff_seconds=0,
            max_concurrency=1,
        )
    )

    async with factory.connect("correlation-5c"):
        pass

    assert captured["headers"]["X-Request-ID"] == "correlation-5c"
    assert captured["headers"]["X-Correlation-ID"] == "correlation-5c"
    assert captured["headers"]["traceparent"].endswith("-01")


def test_capability_defaults_are_unknown_and_shareholding_is_unsupported() -> None:
    registry = YahooFinanceCapabilityRegistry.from_json("[]")
    latest = registry.capability_for(region="INDIA", requirement_id="LATEST_PRICE")
    shareholding = registry.capability_for(region="INDIA", requirement_id="SHAREHOLDING")
    assert latest.state is ExternalCapabilityState.UNKNOWN
    assert shareholding.state is ExternalCapabilityState.UNSUPPORTED
    assert registry.supported == ()


def test_supported_capability_requires_exact_tool_and_rejects_secret_static_arguments() -> None:
    with pytest.raises(ValueError, match="exact tool"):
        YahooFinanceCapabilityRegistry.from_json(
            json.dumps([{"region": "INDIA", "requirementId": "LATEST_PRICE", "capability": "LATEST_PRICE", "state": "SUPPORTED"}])
        )
    with pytest.raises(ValueError, match="credentials"):
        YahooFinanceCapabilityRegistry.from_json(
            json.dumps([mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", toolArguments={"apiKey": "hidden"})])
        )


@pytest.mark.asyncio
async def test_valid_quote_is_normalized_with_provenance_and_correlation() -> None:
    adapter, client = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=600)
    )
    result = await adapter.invoke(
        tool="get_quote", arguments=arguments(), request_id="request-yahoo-1"
    )
    assert result["providerId"] == PROVIDER_ID
    assert result["globalInstrumentId"] == str(INSTRUMENT_ID)
    assert result["structuredFacts"][0]["metric"] == "latestPrice"
    assert client.calls == [(
        "get_quote",
        {
            "globalInstrumentId": str(INSTRUMENT_ID),
            "verifiedYahooSymbol": "READY.NS",
            "region": "INDIA",
            "exchange": "XNSE",
            "currency": "INR",
        },
    )]


@pytest.mark.asyncio
async def test_official_sdk_contract_against_offline_fake_mcp_server() -> None:
    configured = mapping(
        "LATEST_PRICE", "LATEST_PRICE", "yahoo_latest_price", maxAgeSeconds=600
    )
    capabilities = YahooFinanceCapabilityRegistry.from_json(json.dumps([configured]))
    adapter = YahooFinanceMcpProvider(
        YahooFinanceMcpConfig(
            transport="stdio",
            endpoint=None,
            stdio_command="unused-in-memory",
            stdio_args=(),
            auth_type="NONE",
            auth_header_name="Authorization",
            auth_token=None,
            stdio_token_env_name=None,
            timeout_seconds=1,
            max_retries=0,
            retry_backoff_seconds=0,
            max_concurrency=1,
        ),
        capabilities,
        client_factory=InMemoryServerFactory(
            create_fake_yahoo_mcp_server(now=NOW)
        ),
        clock=lambda: NOW,
    )
    normalized = await adapter.invoke(
        tool="yahoo_latest_price",
        arguments=arguments(),
        request_id="official-sdk-fake-server",
    )
    assert normalized["structuredFacts"][0]["value"] == "250.50"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"globalInstrumentId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        {"symbol": "WRONG.NS"},
        {"exchange": "NASDAQ"},
        {"currency": "USD"},
    ],
)
async def test_canonical_identity_conflicts_fail_closed(override) -> None:
    adapter, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"), payload(**override)
    )
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="identity")
    assert error.value.code is McpErrorCode.EXTERNAL_IDENTITY_CONFLICT


@pytest.mark.asyncio
async def test_provider_response_cannot_create_identity_or_mapping() -> None:
    adapter, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"),
        payload(providerMappings=[{"symbol": "NEW"}]),
    )
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="mapping")
    assert error.value.code is McpErrorCode.EXTERNAL_SCHEMA_INVALID


@pytest.mark.asyncio
async def test_timeout_is_deterministic() -> None:
    adapter, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"), delay=0.2
    )
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="timeout")
    assert error.value.code is McpErrorCode.DOWNSTREAM_TIMEOUT
    assert "fake-yahoo" not in str(error.value)


@pytest.mark.asyncio
async def test_absent_discovered_tool_fails_closed() -> None:
    adapter, client = provider(mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"))
    client.tools = ("different_tool",)
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="tool-list")
    assert error.value.code is McpErrorCode.EXTERNAL_CAPABILITY_UNSUPPORTED
    assert client.calls == []


@pytest.mark.asyncio
async def test_stale_and_incomplete_results_are_distinct() -> None:
    stale, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=60),
        payload(asOf=(NOW - timedelta(minutes=2)).isoformat()),
    )
    with pytest.raises(McpGatewayError) as stale_error:
        await stale.invoke(tool="get_quote", arguments=arguments(), request_id="stale")
    assert stale_error.value.code is McpErrorCode.EXTERNAL_RESULT_STALE

    incomplete, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"), payload(price=None)
    )
    with pytest.raises(McpGatewayError) as incomplete_error:
        await incomplete.invoke(tool="get_quote", arguments=arguments(), request_id="incomplete")
    assert incomplete_error.value.code is McpErrorCode.EXTERNAL_RESULT_INCOMPLETE


@pytest.mark.asyncio
async def test_nse_latest_close_is_valid_during_weekend_with_internal_calendar_context():
    close = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    adapter, client = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=900),
        payload(asOf=close.isoformat()), clock=lambda: now,
    )
    result = await adapter.invoke(tool="get_quote", arguments=arguments(latestPriceSession={
        "market": "XNSE", "sessionClose": close.isoformat(),
        "validUntil": "2026-10-19T03:45:00+00:00",
    }), request_id="weekend-close")
    assert result["observedAt"] == close.isoformat()
    assert result["structuredFacts"][0]["asOf"] == close.isoformat()
    assert "latestPriceSession" not in client.calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("observed,now,close,until,accepted", [
    # Official Oct 2 closure: Thursday close survives the holiday and weekend.
    ("2026-10-01T10:00:00Z", "2026-10-02T12:00:00Z", "2026-10-01T10:00:00Z", "2026-10-05T03:45:00Z", True),
    # The Budget Sunday is a real session, not an ordinary weekend.
    ("2026-02-01T10:00:00Z", "2026-02-01T12:00:00Z", "2026-02-01T10:00:00Z", "2026-02-02T03:45:00Z", True),
    ("2026-01-30T10:00:00Z", "2026-02-01T12:00:00Z", "2026-02-01T10:00:00Z", "2026-02-02T03:45:00Z", False),
    # An old session or intraday observation cannot masquerade as the latest close.
    ("2026-10-15T10:00:00Z", "2026-10-18T12:00:00Z", "2026-10-16T10:00:00Z", "2026-10-19T03:45:00Z", False),
    ("2026-10-16T07:00:00Z", "2026-10-18T12:00:00Z", "2026-10-16T10:00:00Z", "2026-10-19T03:45:00Z", False),
    # Closed-market validity ends exactly at the next opening.
    ("2026-10-16T10:00:00Z", "2026-10-19T03:44:59Z", "2026-10-16T10:00:00Z", "2026-10-19T03:45:00Z", True),
    ("2026-10-16T10:00:00Z", "2026-10-19T03:45:00Z", "2026-10-16T10:00:00Z", "2026-10-19T03:45:00Z", False),
    # Replaying a closed-market context cannot extend a live intraday quote.
    ("2026-10-19T07:44:59Z", "2026-10-19T08:00:00Z", "2026-10-16T10:00:00Z", "2026-10-19T03:45:00Z", False),
])
async def test_latest_price_session_bounds(observed, now, close, until, accepted):
    adapter, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=900),
        payload(asOf=observed), clock=lambda: datetime.fromisoformat(now),
    )
    call = adapter.invoke(tool="get_quote", arguments=arguments(latestPriceSession={
        "market": "NSE", "sessionClose": close, "validUntil": until,
    }), request_id="session-bounds")
    if accepted:
        result = await call
        assert datetime.fromisoformat(result["observedAt"]) == datetime.fromisoformat(observed)
    else:
        with pytest.raises(McpGatewayError) as error:
            await call
        assert error.value.code is McpErrorCode.EXTERNAL_RESULT_STALE


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected", [(900, None), (901, McpErrorCode.EXTERNAL_RESULT_STALE),
                                           (-1, McpErrorCode.EXTERNAL_RESULT_STALE)])
async def test_latest_price_retains_strict_live_ttl_and_rejects_future(age, expected):
    adapter, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=900),
        payload(asOf=(NOW - timedelta(seconds=age)).isoformat()),
    )
    if expected is None:
        assert (await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="live"))["freshness"] == "FRESH"
    else:
        with pytest.raises(McpGatewayError) as error:
            await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="live")
        assert error.value.code is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", [None, "2026-10-16T10:00:00"])
async def test_latest_price_requires_a_genuine_aware_observation_timestamp(stamp):
    adapter, _ = provider(mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=900), payload(asOf=stamp))
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="missing-time")
    assert error.value.code is McpErrorCode.EXTERNAL_RESULT_INCOMPLETE


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    None,
    {"market": "UNKNOWN", "sessionClose": "2026-10-16T10:00:00Z", "validUntil": "2026-10-19T03:45:00Z"},
    {"market": "NSE", "sessionClose": "2026-10-16T10:00:00", "validUntil": "2026-10-19T03:45:00Z"},
    {"market": "NSE", "sessionClose": "2026-10-19T10:00:00Z", "validUntil": "2026-10-16T03:45:00Z"},
])
async def test_missing_or_invalid_calendar_cannot_extend_freshness(context):
    adapter, client = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=900),
        payload(asOf="2026-10-16T10:00:00Z"),
        clock=lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc),
    )
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(latestPriceSession=context), request_id="unknown-calendar")
    assert error.value.code is (McpErrorCode.EXTERNAL_RESULT_STALE if context is None else McpErrorCode.INVALID_ARGUMENT)
    if context is not None:
        assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"region": "USA"}, {"region": "EUROPE"},
                                      {"expectedExchange": "UNKNOWN"}, {"requirementId": "VALUATION_INPUTS"}])
async def test_nse_session_context_is_scoped_to_latest_nse_price(overrides):
    adapter, client = provider(mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=900))
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(latestPriceSession={
            "market": "XNSE", "sessionClose": NOW.isoformat(), "validUntil": (NOW + timedelta(days=3)).isoformat(),
        }, **overrides), request_id="scope")
    assert error.value.code is McpErrorCode.INVALID_ARGUMENT
    assert client.calls == []


@pytest.mark.asyncio
async def test_provider_cannot_supply_its_own_calendar_freshness_exemption():
    adapter, _ = provider(
        mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=900),
        payload(latestPriceSession={"market": "NSE", "sessionClose": NOW.isoformat(),
                                    "validUntil": (NOW + timedelta(days=3)).isoformat()}),
    )
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(tool="get_quote", arguments=arguments(), request_id="provider-calendar")
    assert error.value.code is McpErrorCode.EXTERNAL_SCHEMA_INVALID


@pytest.mark.asyncio
async def test_annual_and_quarterly_periods_remain_distinct() -> None:
    facts = [
        {"metric": metric, "value": value, "periodEnd": period, "periodType": kind, "reportingBasis": "CONSOLIDATED"}
        for kind, period, value in (
            ("ANNUAL", "2025-03-31", "100"),
            ("ANNUAL", "2024-03-31", "90"),
            ("QUARTERLY", "2026-06-30", "30"),
            ("QUARTERLY", "2026-03-31", "25"),
        )
        for metric in ("revenue", "pat")
    ]
    adapter, _ = provider(
        mapping("QUARTERLY_FINANCIALS", "QUARTERLY_FINANCIALS", "get_financials"),
        payload(facts=facts),
    )
    result = await adapter.invoke(
        tool="get_financials",
        arguments=arguments("QUARTERLY_FINANCIALS"),
        request_id="periods",
    )
    identities = {(item["periodEnd"], item["periodType"]) for item in result["financialFacts"]}
    assert ("2025-03-31", "ANNUAL") in identities
    assert ("2026-06-30", "QUARTERLY") in identities


@pytest.mark.asyncio
async def test_news_filters_old_and_unrelated_articles_without_keyword_scoring() -> None:
    news = [
        {"headline": "Issuer update", "url": "https://news.test/current", "publishedAt": (NOW - timedelta(days=2)).isoformat(), "issuerSymbol": "READY.NS"},
        {"headline": "Old story", "url": "https://news.test/old", "publishedAt": (NOW - timedelta(days=31)).isoformat(), "issuerSymbol": "READY.NS"},
        {"headline": "Other issuer", "url": "https://news.test/other", "publishedAt": NOW.isoformat(), "issuerSymbol": "OTHER.NS"},
    ]
    adapter, _ = provider(
        mapping("CURRENT_NEWS", "NEWS", "get_news"), payload(news=news)
    )
    result = await adapter.invoke(
        tool="get_news", arguments=arguments("CURRENT_NEWS"), request_id="news"
    )
    assert [item["headline"] for item in result["news"]] == ["Issuer update"]
    assert result["news"][0]["publishedAt"]
    assert "materiality" not in result["news"][0]


@pytest.mark.asyncio
async def test_successful_empty_news_requires_explicit_provider_scan_success():
    configured = mapping("CURRENT_NEWS", "NEWS", "get_news")
    adapter, _ = provider(configured, payload(news=[], newsQuerySucceeded=True))
    result = await adapter.invoke(tool="get_news", arguments=arguments("CURRENT_NEWS"), request_id="empty-news")
    assert result["acquisitionOutcome"] == "SUCCESS_EMPTY"
    assert result["news"] == []
    assert result["events"] == []
    adapter, _ = provider(configured, payload(news=[]))
    with pytest.raises(McpGatewayError):
        await adapter.invoke(tool="get_news", arguments=arguments("CURRENT_NEWS"), request_id="absent-news")


@pytest.mark.asyncio
async def test_generic_institutional_ownership_is_not_shareholding_equivalence() -> None:
    configured = mapping("SHAREHOLDING", "SHAREHOLDING", "get_ownership")
    response = payload(
        ownership={
            "periodEnd": NOW.isoformat(),
            "institutionalOwnershipPercent": "42",
        }
    )
    adapter, _ = provider(configured, response)
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(
            tool="get_ownership", arguments=arguments("SHAREHOLDING"), request_id="ownership"
        )
    assert error.value.code is McpErrorCode.EXTERNAL_RESULT_INCOMPLETE


@pytest.mark.asyncio
async def test_shareholding_requires_an_exact_quarter_end_period() -> None:
    configured = mapping("SHAREHOLDING", "SHAREHOLDING", "get_ownership")
    response = payload(
        ownership={
            "periodEnd": "2026-06-29T00:00:00Z",
            "promoterHoldingPercent": "51",
            "promoterPledgePercent": "0",
            "promoterPledgeBasis": "PROMOTER_HOLDING",
            "fiiFpiPercent": "14",
            "diiPercent": "9",
        }
    )
    adapter, _ = provider(configured, response)
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(
            tool="get_ownership",
            arguments=arguments("SHAREHOLDING"),
            request_id="ownership-period",
        )
    assert error.value.code is McpErrorCode.EXTERNAL_RESULT_INCOMPLETE


@pytest.mark.asyncio
async def test_catalyst_requires_a_readiness_supported_event_type() -> None:
    configured = mapping(
        "ORDER_BOOK_CAPEX_GUIDANCE", "CATALYSTS_EVENTS", "get_events"
    )
    response = payload(
        events=[
            {
                "headline": "Generic corporate update",
                "url": "https://news.test/generic-update",
                "publishedAt": NOW.isoformat(),
                "issuerSymbol": "READY.NS",
                "eventType": "OTHER",
            }
        ]
    )
    adapter, _ = provider(configured, response)
    with pytest.raises(McpGatewayError) as error:
        await adapter.invoke(
            tool="get_events",
            arguments=arguments("ORDER_BOOK_CAPEX_GUIDANCE"),
            request_id="unsupported-event-type",
        )
    assert error.value.code is McpErrorCode.EXTERNAL_RESULT_INCOMPLETE


@pytest.mark.asyncio
async def test_health_does_not_return_endpoint_or_credentials() -> None:
    adapter, client = provider(mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote"))
    adapter.config = YahooFinanceMcpConfig(
        **{
            **adapter.config.__dict__,
            "auth_type": "HEADER",
            "auth_token": SecretStr("super-secret-token"),
        }
    )
    health = await adapter.health()
    assert health["status"] == "UP"
    serialized = str(health)
    assert "super-secret-token" not in serialized
    client.tools = ()
    assert (await adapter.health())["status"] == "DEGRADED"


@pytest.mark.parametrize("with_session", [False, True])
def test_internal_acquisition_route_requires_caller_and_emits_argument_free_audit(with_session) -> None:
    capabilities = json.dumps(
        [mapping("LATEST_PRICE", "LATEST_PRICE", "get_quote", maxAgeSeconds=600)]
    )
    settings = McpGatewaySettings(
        AIP_ENVIRONMENT="TEST",
        AIP_MCP_AUTHENTICATION_TYPE="TEST",
        AIP_MCP_EXTERNAL_PROVIDERS_ENABLED="true",
        AIP_MCP_YAHOO_ENABLED="true",
        AIP_MCP_YAHOO_TRANSPORT="stdio",
        AIP_MCP_YAHOO_STDIO_COMMAND="fake-yahoo-mcp",
        AIP_MCP_YAHOO_CAPABILITIES_JSON=capabilities,
        AIP_MCP_YAHOO_MAX_RETRIES="0",
        AIP_MCP_EXTERNAL_CALLER_IDENTITIES="research-engine",
    )
    audit = RecordingAudit()
    container = create_container(settings, reader=FakeApplicationReader(), audit=audit)
    adapter = container.external_registry.get(PROVIDER_ID)
    fake_client = FakeClient(payload(), tools=("get_quote",))
    adapter.client_factory = FakeFactory(fake_client)
    adapter.clock = lambda: NOW
    app = create_mcp_server(container).streamable_http_app(
        stateless_http=True, json_response=True
    )
    authorization = {
        "authorized": True,
        "issuedBy": "ProviderFallbackPolicy",
        "globalInstrumentId": str(INSTRUMENT_ID),
        "requirementId": "LATEST_PRICE",
        "permittedProviderIds": [PROVIDER_ID],
        "issuedAt": NOW.isoformat(),
    }
    body = {
        "providerId": PROVIDER_ID,
        "region": "INDIA",
        "requirementId": "LATEST_PRICE",
        "globalInstrumentId": str(INSTRUMENT_ID),
        "providerSymbol": "READY.NS",
        "expectedExchange": "NSE",
        "expectedCurrency": "INR",
        "authorization": authorization,
    }
    if with_session:
        adapter.clock = lambda: NOW + timedelta(days=2)
        body["latestPriceSession"] = {
            "market": "XNSE", "sessionClose": NOW.isoformat(),
            "validUntil": "2026-09-14T03:45:00+00:00",
        }
    with TestClient(app) as client:
        assert client.post("/internal/v1/external-research/acquire", json=body).status_code == 401
        response = client.post(
            "/internal/v1/external-research/acquire",
            json=body,
            headers={"X-AIP-Service-Identity": "research-engine", "X-Request-ID": "route-5b"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["providerId"] == PROVIDER_ID
    assert response.json()["data"]["observedAt"] == NOW.isoformat()
    assert "latestPriceSession" not in fake_client.calls[0][1]
    assert [event["event"] for event in audit.events] == [
        "MCP_TOOL_INVOKED",
        "MCP_TOOL_SUCCEEDED",
    ]
    serialized_audit = str(audit.events)
    assert "READY.NS" not in serialized_audit
    assert "structuredFacts" not in serialized_audit
