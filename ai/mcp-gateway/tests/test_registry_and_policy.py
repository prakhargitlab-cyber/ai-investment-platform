from __future__ import annotations

import pytest

from app.contracts import (
    McpErrorCode,
    McpGatewayError,
    McpRiskClass,
    McpToolDefinition,
    McpToolExecution,
    McpToolKind,
    StrictContract,
)
from app.policy import McpToolPolicy
from app.registry import DuplicateMcpToolError, McpToolRegistry


class EmptyInput(StrictContract):
    pass


async def handler(_arguments, _auth, _request_id):
    return McpToolExecution(data={})


def definition(name: str, risk: McpRiskClass = McpRiskClass.SAFE_READ) -> McpToolDefinition:
    return McpToolDefinition(
        name=name,
        description="test",
        input_model=EmptyInput,
        risk_class=risk,
        kind=McpToolKind.INTERNAL,
        handler=handler,
    )


def test_known_tools_and_schemas_are_discoverable(container) -> None:
    assert container.registry.get("get_research_readiness") is not None
    discovered = {item["name"]: item for item in container.registry.discover()}
    assert len(discovered) == 9
    assert {item["riskClass"] for item in discovered.values()} == {"SAFE_READ"}
    assert {item["kind"] for item in discovered.values()} == {"INTERNAL"}
    assert discovered["get_company_analysis"]["riskClass"] == "SAFE_READ"
    assert discovered["get_company_analysis"]["kind"] == "INTERNAL"
    assert discovered["get_company_analysis"]["inputSchema"]["required"] == ["globalInstrumentId"]


def test_duplicate_registration_is_rejected() -> None:
    registry = McpToolRegistry()
    registry.register(definition("same"))
    with pytest.raises(DuplicateMcpToolError, match="already registered"):
        registry.register(definition("same"))


def test_safe_read_is_allowed(auth) -> None:
    McpToolPolicy().authorize(definition("read"), auth)


@pytest.mark.parametrize(
    "risk",
    [
        McpRiskClass.SENSITIVE_READ,
        McpRiskClass.WRITE_NON_FINANCIAL,
        McpRiskClass.FINANCIAL_ACTION,
        McpRiskClass.ADMIN_ACTION,
    ],
)
def test_every_non_safe_risk_class_is_denied(auth, risk) -> None:
    with pytest.raises(McpGatewayError) as error:
        McpToolPolicy().authorize(definition("unsafe", risk), auth)
    assert error.value.code == McpErrorCode.MCP_TOOL_DENIED
