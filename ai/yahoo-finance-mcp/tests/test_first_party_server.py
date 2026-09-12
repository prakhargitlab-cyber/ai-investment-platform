from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from mcp import Client, StdioServerParameters
from pydantic import ValidationError
from starlette.testclient import TestClient

from fake_yahoo import factory
from yahoo_mcp_server.acquisition import YahooAcquisitionService
from yahoo_mcp_server.contracts import IdentityInput, NewsInput, RawFact, ToolPayload
from yahoo_mcp_server.server import REGISTERED_TOOLS, create_yahoo_mcp_server
from yahoo_mcp_server.settings import YahooMcpSettings


INSTRUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")


class RecordingAudit:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event, **values) -> None:
        self.events.append({"event": event, **values})


def settings(**overrides) -> YahooMcpSettings:
    values = {
        "AIP_ENVIRONMENT": "TEST",
        "AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS": "1",
        "AIP_YAHOO_MCP_MAX_CONCURRENCY": "2",
    }
    values.update(overrides)
    return YahooMcpSettings(**values)


def server(scenario="SUCCESS", *, audit=None, timeout="1"):
    config = settings(AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS=timeout)
    acquisition = YahooAcquisitionService(config, ticker_factory=factory(scenario))
    return create_yahoo_mcp_server(config, acquisition=acquisition, audit=audit)


def arguments(symbol="HAL.NS", **overrides):
    exchange = "XNSE" if symbol.endswith(".NS") else "XAMS" if symbol.endswith(".AS") else "XNAS"
    currency = "INR" if symbol.endswith(".NS") else "EUR" if symbol.endswith(".AS") else "USD"
    value = {
        "globalInstrumentId": str(INSTRUMENT_ID),
        "verifiedYahooSymbol": symbol,
        "region": "INDIA" if symbol.endswith(".NS") else "EUROPE" if symbol.endswith(".AS") else "USA",
        "exchange": exchange,
        "currency": currency,
    }
    value.update(overrides)
    return value


async def call_tool(instance, name, values=None):
    async with Client(instance) as client:
        return await client.call_tool(name, values or arguments())


@pytest.mark.asyncio
async def test_registers_only_the_eight_safe_read_tools_with_discoverable_strict_schemas():
    async with Client(server()) as client:
        listed = await client.list_tools()
    tools = {item.name: item for item in listed.tools}
    assert set(tools) == set(REGISTERED_TOOLS)
    assert all(item.annotations.read_only_hint is True for item in tools.values())
    assert all(item.annotations.destructive_hint is False for item in tools.values())
    quote_schema = tools["get_quote"].input_schema
    assert {"globalInstrumentId", "verifiedYahooSymbol", "region"}.issubset(quote_schema["required"])
    assert quote_schema["additionalProperties"] is False
    serialized = json.dumps(quote_schema)
    assert "companyName" not in serialized
    assert not {"trade", "order", "search_symbol", "fetch_url"}.intersection(tools)


@pytest.mark.asyncio
async def test_real_stdio_entrypoint_initializes_lists_tools_and_shuts_down_cleanly():
    environment = {
        key: value
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "HOME", "TMP", "TEMP")
        if (value := os.environ.get(key))
    }
    async with Client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "yahoo_mcp_server.main", "--transport", "stdio"],
            env=environment,
        ),
        read_timeout_seconds=5,
        cache=None,
    ) as client:
        listed = await client.list_tools()
    assert {item.name for item in listed.tools} == set(REGISTERED_TOOLS)


@pytest.mark.asyncio
async def test_quote_contract_preserves_canonical_identity_and_normalized_price():
    result = await call_tool(server(), "get_quote")
    assert result.is_error is False
    payload = ToolPayload.model_validate(result.structured_content)
    assert payload.global_instrument_id == INSTRUMENT_ID
    assert payload.symbol == "HAL.NS"
    assert str(payload.price) == "250.5"
    assert payload.source == "YAHOO_FINANCE"
    assert "quantity" not in result.structured_content


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["HAL.NS", "RBLBANK.NS", "AAPL", "MSFT", "BESI.AS"])
async def test_representative_verified_provider_symbols_are_forwarded_exactly(symbol):
    result = await call_tool(server(), "get_quote", arguments(symbol))
    assert result.is_error is False
    assert result.structured_content["symbol"] == symbol


@pytest.mark.asyncio
async def test_history_reuses_close_semantics_and_filters_non_finite_rows():
    result = await call_tool(server(), "get_price_history", {**arguments(), "lookbackDays": 400})
    assert result.is_error is False
    assert [item["close"] for item in result.structured_content["prices"]] == ["248.0", "250.5"]


@pytest.mark.asyncio
async def test_profile_sector_and_industry_are_normalized_without_raw_response():
    profile = await call_tool(server(), "get_company_profile")
    sector = await call_tool(server(), "get_sector_industry")
    assert profile.structured_content["profile"]["companyName"] == "HAL.NS Company"
    assert sector.structured_content["profile"] == {
        "companyName": "HAL.NS Company",
        "sector": "Industrials",
        "industry": "Aerospace & Defense",
    }
    assert "longBusinessSummary" not in str(profile.structured_content)


@pytest.mark.asyncio
async def test_annual_and_quarterly_tools_keep_periods_separate_and_raw_origin():
    annual = await call_tool(server(), "get_financials")
    quarterly = await call_tool(server(), "get_quarterly_financials")
    annual_facts = annual.structured_content["facts"]
    quarterly_facts = quarterly.structured_content["facts"]
    assert {item.get("periodType") for item in annual_facts if item.get("periodType")} == {"ANNUAL"}
    assert {item.get("periodType") for item in quarterly_facts} == {"QUARTERLY"}
    assert any(item.get("rawFieldOrigin") == "Total Revenue" for item in annual_facts)
    assert len({item["periodEnd"] for item in quarterly_facts}) == 2


@pytest.mark.asyncio
async def test_news_is_current_deduplicated_and_issuer_scoped():
    result = await call_tool(server(), "get_news", {**arguments(), "days": 30})
    assert result.is_error is False
    assert len(result.structured_content["news"]) == 1
    article = result.structured_content["news"][0]
    assert article["issuerSymbol"] == "HAL.NS"
    assert article["publishedAt"]
    assert "materiality" not in article


@pytest.mark.asyncio
async def test_analyst_tool_exposes_only_present_supporting_fields():
    result = await call_tool(server(), "get_analyst_data")
    metrics = {item["metric"] for item in result.structured_content["facts"]}
    assert {
        "publicAnalystTargetMeanPrice",
        "publicAnalystCount",
        "publicAnalystConsensus",
    }.issubset(metrics)
    assert "investmentRecommendation" not in metrics


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("WRONG_SYMBOL", "YAHOO_MCP_IDENTITY_MISMATCH"),
        ("WRONG_EXCHANGE", "YAHOO_MCP_IDENTITY_MISMATCH"),
        ("WRONG_CURRENCY", "YAHOO_MCP_IDENTITY_MISMATCH"),
        ("EMPTY", "YAHOO_MCP_INCOMPLETE"),
        ("INVALID_INFO", "YAHOO_MCP_UPSTREAM_UNAVAILABLE"),
        ("UPSTREAM_FAILURE", "YAHOO_MCP_UPSTREAM_UNAVAILABLE"),
    ],
)
async def test_failures_are_deterministic_and_do_not_leak_upstream_details(scenario, expected):
    result = await call_tool(server(scenario), "get_quote")
    assert result.is_error is True
    message = str(result.content)
    assert expected in message
    assert "sensitive" not in message
    assert "Traceback" not in message


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_deterministic():
    result = await call_tool(server("TIMEOUT", timeout="0.05"), "get_quote")
    assert result.is_error is True
    assert "YAHOO_MCP_UPSTREAM_TIMEOUT" in str(result.content)


@pytest.mark.asyncio
async def test_nonfinite_financial_cell_is_omitted_without_rejecting_valid_facts():
    result = await call_tool(server("NAN_FACT"), "get_financials")
    assert result.is_error is False
    payload = ToolPayload.model_validate(result.structured_content)
    assert not any(fact.metric == "marketCap" for fact in payload.facts)
    assert any(fact.metric == "pat" for fact in payload.facts)


@pytest.mark.asyncio
async def test_unknown_or_unsafe_tool_and_extra_arguments_fail_closed():
    unknown = await call_tool(server(), "place_order")
    extra = await call_tool(server(), "get_quote", {**arguments(), "companyName": "fuzzy"})
    assert unknown.is_error is True
    assert extra.is_error is True


def test_input_contract_rejects_fuzzy_identity_bad_symbol_and_more_than_30_news_days():
    with pytest.raises(ValidationError):
        IdentityInput(**{**arguments(), "companyName": "Hindustan Aeronautics"})
    with pytest.raises(ValidationError):
        IdentityInput(**arguments("../../etc/passwd"))
    with pytest.raises(ValidationError):
        NewsInput(**{**arguments(), "days": 31})


def test_output_fact_contract_rejects_malformed_period_identity():
    with pytest.raises(ValidationError):
        RawFact(metric="revenue", value="1", periodEnd="FY2026", periodType="ANNUAL")
    with pytest.raises(ValidationError):
        RawFact(metric="revenue", value="1", periodEnd="2026-03-31", periodType="TRAILING")


def test_health_and_readiness_do_not_call_yahoo_upstream():
    app = server("UPSTREAM_FAILURE").streamable_http_app(stateless_http=True, json_response=True)
    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/health/ready")
    assert health.status_code == 200 and health.json()["registeredTools"] == 8
    assert ready.status_code == 200 and ready.json()["upstreamRequired"] is False


@pytest.mark.asyncio
async def test_audit_records_safe_metadata_without_arguments_results_or_private_fields():
    audit = RecordingAudit()
    result = await call_tool(server(audit=audit), "get_quote")
    assert result.is_error is False
    assert [item["event"] for item in audit.events] == ["YAHOO_MCP_REQUEST", "YAHOO_MCP_SUCCESS"]
    serialized = json.dumps(audit.events, default=str)
    for forbidden in ("quantity", "averageCost", "authorization", "currentPrice", "facts", "news"):
        assert forbidden not in serialized


def test_local_and_azure_profiles_parse_without_secrets_or_cloud_dependency():
    local = YahooMcpSettings(AIP_ENVIRONMENT="LOCAL")
    azure = YahooMcpSettings(AIP_ENVIRONMENT="AZURE")
    assert local.transport == "stdio"
    assert azure.environment == "AZURE"
    assert "secret" not in json.dumps(azure.model_dump()).lower()

@pytest.mark.asyncio
async def test_financial_issuer_quarters_with_missing_cells_keep_valid_reported_values():
    from fake_yahoo import FakeYahooTicker
    class FinancialTicker(FakeYahooTicker):
        @property
        def info(self):
            return {**super().info, "sector": "Financial Services", "industry": "Banks - Regional"}
        def __init__(self, symbol):
            super().__init__(symbol)
            self.quarterly_income_stmt.iloc[0, 0] = float("nan")
    config = settings()
    acquisition = YahooAcquisitionService(config, ticker_factory=FinancialTicker)
    instance = create_yahoo_mcp_server(config, acquisition=acquisition)
    result = await call_tool(instance, "get_quarterly_financials")
    assert not result.is_error
    payload = ToolPayload.model_validate(result.structured_content)
    assert any(fact.metric == "pat" for fact in payload.facts)
    assert sum(fact.metric == "revenue" for fact in payload.facts) == 1
    assert not any(fact.metric == "roce" for fact in payload.facts)
