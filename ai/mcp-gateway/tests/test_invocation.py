from __future__ import annotations

import asyncio

import pytest

from app.contracts import (
    McpAuthContext,
    McpAuthenticationType,
    McpRiskClass,
    McpToolDefinition,
    McpToolExecution,
    McpToolKind,
    StrictContract,
)
from app.invocation import McpInvocationService
from app.policy import McpToolPolicy
from app.registry import McpToolRegistry
from conftest import INSTRUMENT_ID, WATCHLIST_ID


async def invoke(container, tool, arguments, request_id="request-5a"):
    return await container.invocation.invoke(
        tool, arguments, container.auth, request_id=request_id
    )


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected_and_audited(container, audit) -> None:
    result = await invoke(container, "does_not_exist", {})
    assert result["error"]["code"] == "MCP_TOOL_NOT_FOUND"
    assert [event["event"] for event in audit.events] == ["MCP_TOOL_INVOKED", "MCP_TOOL_FAILED"]


@pytest.mark.asyncio
async def test_global_instrument_id_is_required_and_fuzzy_identity_is_rejected(container, reader) -> None:
    missing = await invoke(container, "get_research_readiness", {})
    fuzzy = await invoke(
        container, "get_research_readiness", {"globalInstrumentId": "Reliance Industries"}
    )
    assert missing["error"]["code"] == "INVALID_ARGUMENT"
    assert fuzzy["error"]["code"] == "INVALID_ARGUMENT"
    assert reader.calls == []


@pytest.mark.asyncio
async def test_non_held_company_is_supported_without_portfolio_fields(container, reader) -> None:
    result = await invoke(
        container,
        "get_financial_facts",
        {"globalInstrumentId": str(INSTRUMENT_ID)},
    )
    assert result["ok"] is True
    assert result["data"]["globalInstrumentId"] == str(INSTRUMENT_ID)
    assert not {"quantity", "averageCost", "investedAmount", "pnl", "allocation"}.intersection(
        result["data"]
    )
    assert reader.provider_calls == 0


@pytest.mark.asyncio
async def test_readiness_matches_existing_capability_and_does_not_acquire(container, reader) -> None:
    expected = await reader.invoke(
        "get_research_readiness",
        {"globalInstrumentId": str(INSTRUMENT_ID)},
        container.auth,
        "direct",
    )
    reader.calls.clear()
    result = await invoke(
        container, "get_research_readiness", {"globalInstrumentId": str(INSTRUMENT_ID)}
    )
    assert result["data"] == expected
    assert reader.provider_calls == 0
    assert reader.calls[0][0] == "get_research_readiness"


@pytest.mark.asyncio
async def test_analysis_has_rule_engine_parity_and_zero_provider_calls(container, reader) -> None:
    direct = await reader.invoke(
        "get_company_analysis",
        {"globalInstrumentId": str(INSTRUMENT_ID), "allowPartial": False},
        container.auth,
        "direct",
    )
    result = await invoke(
        container,
        "get_company_analysis",
        {"globalInstrumentId": str(INSTRUMENT_ID)},
    )
    assert result["data"] == direct
    assert result["provenance"]["ruleEngineVersion"] == "STOCK_RULE_ENGINE_V1"
    assert reader.provider_calls == 0


@pytest.mark.asyncio
async def test_recent_news_default_and_maximum_contract(container, reader) -> None:
    default_result = await invoke(
        container, "get_recent_news", {"globalInstrumentId": str(INSTRUMENT_ID)}
    )
    too_large = await invoke(
        container,
        "get_recent_news",
        {"globalInstrumentId": str(INSTRUMENT_ID), "days": 31},
    )
    item = default_result["data"]["news"][0]
    assert default_result["data"]["days"] == 30
    assert too_large["error"]["code"] == "INVALID_ARGUMENT"
    assert item["publicationDate"]
    assert item["source"]["type"] == "EXCHANGE_FILING"
    assert reader.provider_calls == 0


@pytest.mark.asyncio
async def test_sector_performance_uses_durable_read_only_capability(container, reader) -> None:
    result = await invoke(
        container,
        "get_sector_performance",
        {"region": "INDIA", "sector": "FINANCIAL_SERVICES", "period": "MONTH"},
    )
    assert result["ok"] is True
    assert result["data"]["sector"] == "FINANCIAL_SERVICES"
    assert reader.calls[-1][0] == "get_sector_performance"
    assert reader.provider_calls == 0


@pytest.mark.asyncio
async def test_private_financial_fields_and_tokens_are_removed(container, reader) -> None:
    async def private_response(*_args, **_kwargs):
        return {
            "globalInstrumentId": str(INSTRUMENT_ID),
            "quantity": 10,
            "averageCost": 99,
            "pnl": 12,
            "P&L": 12,
            "cost basis": 99,
            "nested": {"authorization": "Bearer abc.def.ghi", "safe": "kept"},
            "apiKey": "secret-value",
            "message": "upstream failed with api_key=must-not-leak",
        }

    reader.invoke = private_response
    result = await invoke(
        container, "get_financial_facts", {"globalInstrumentId": str(INSTRUMENT_ID)}
    )
    serialized = str(result)
    assert result["data"] == {
        "globalInstrumentId": str(INSTRUMENT_ID),
        "nested": {"safe": "kept"},
        "message": "upstream failed with api_key=<redacted>",
    }
    assert "secret-value" not in serialized and "must-not-leak" not in serialized
    assert "Bearer" not in serialized


@pytest.mark.asyncio
async def test_invalid_arguments_are_deterministic(container) -> None:
    result = await invoke(
        container,
        "get_sector_performance",
        {"region": "MARS", "sector": "", "period": "WEEK", "unexpected": True},
    )
    assert result["error"] == {
        "code": "INVALID_ARGUMENT",
        "message": "The tool arguments are invalid.",
    }


class EmptyInput(StrictContract):
    pass


def service_for(handler, auth, audit, timeout=0.01, risk=McpRiskClass.SAFE_READ):
    registry = McpToolRegistry()
    registry.register(
        McpToolDefinition(
            name="test_tool",
            description="test",
            input_model=EmptyInput,
            risk_class=risk,
            kind=McpToolKind.INTERNAL,
            handler=handler,
        )
    )
    return McpInvocationService(registry, McpToolPolicy(), audit, timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_timeout_is_deterministic(auth, audit) -> None:
    async def slow(*_args):
        await asyncio.sleep(0.1)
        return McpToolExecution(data={})

    result = await service_for(slow, auth, audit).invoke("test_tool", {}, auth)
    assert result["error"]["code"] == "DOWNSTREAM_TIMEOUT"


@pytest.mark.asyncio
async def test_downstream_exception_does_not_leak(auth, audit) -> None:
    async def explode(*_args):
        raise RuntimeError("database password leaked-stack-marker")

    result = await service_for(explode, auth, audit).invoke("test_tool", {}, auth)
    assert result["error"]["code"] == "DOWNSTREAM_UNAVAILABLE"
    assert "leaked-stack-marker" not in str(result)


@pytest.mark.asyncio
async def test_policy_denial_emits_denied_audit_event(auth, audit) -> None:
    async def unused(*_args):
        raise AssertionError("denied handler must never run")

    result = await service_for(
        unused, auth, audit, risk=McpRiskClass.FINANCIAL_ACTION
    ).invoke("test_tool", {}, auth, request_id="denied-5a")
    assert result["error"]["code"] == "MCP_TOOL_DENIED"
    assert [event["event"] for event in audit.events] == [
        "MCP_TOOL_INVOKED",
        "MCP_TOOL_DENIED",
    ]


@pytest.mark.asyncio
async def test_invoked_success_and_request_correlation_are_retained(container, audit) -> None:
    result = await invoke(
        container,
        "get_research_readiness",
        {"globalInstrumentId": str(INSTRUMENT_ID)},
        request_id="correlation-5a",
    )
    assert result["requestId"] == "correlation-5a"
    assert [item["event"] for item in audit.events] == ["MCP_TOOL_INVOKED", "MCP_TOOL_SUCCEEDED"]
    assert all(item["request_id"] == "correlation-5a" for item in audit.events)


@pytest.mark.asyncio
async def test_watchlist_requires_user_scope_and_preserves_user_isolation(container, reader) -> None:
    result = await invoke(container, "get_watchlist", {"watchlistId": str(WATCHLIST_ID)})
    assert result["data"]["userId"] == str(container.auth.user_id)
    assert reader.calls[-1][2].user_id == container.auth.user_id


@pytest.mark.asyncio
async def test_watchlist_fails_closed_without_user_or_scope(container, reader) -> None:
    service_only = McpAuthContext(
        serviceIdentity="mcp-test-service",
        scopes=("mcp:read", "watchlist:read"),
        authenticationType=McpAuthenticationType.TEST,
    )
    no_user = await container.invocation.invoke(
        "get_watchlist", {"watchlistId": str(WATCHLIST_ID)}, service_only
    )
    missing_scope = container.auth.model_copy(update={"scopes": ("mcp:read",)})
    no_scope = await container.invocation.invoke(
        "get_watchlist", {"watchlistId": str(WATCHLIST_ID)}, missing_scope
    )
    assert no_user["error"]["code"] == "UNAUTHORIZED"
    assert no_scope["error"]["code"] == "FORBIDDEN"
    assert reader.calls == []
