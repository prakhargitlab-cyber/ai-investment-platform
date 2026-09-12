from __future__ import annotations

import pytest
from mcp import Client

from app.server import create_mcp_server
from app.settings import McpGatewaySettings
from conftest import INSTRUMENT_ID


def test_local_configuration_loads_without_azure(monkeypatch) -> None:
    for name in (
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "KEY_VAULT_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = McpGatewaySettings(AIP_ENVIRONMENT="LOCAL")
    assert settings.environment == "LOCAL"
    assert settings.transport == "stdio"
    assert settings.external_providers_enabled is False
    assert settings.auth_context().authentication_type.value == "LOCAL_SERVICE"


def test_azure_configuration_uses_workload_identity_without_client_secret() -> None:
    settings = McpGatewaySettings(
        AIP_ENVIRONMENT="AZURE",
        AIP_MCP_SERVICE_IDENTITY="mcp-gateway",
        AIP_MCP_RESEARCH_BASE_URL="http://research-engine",
    )
    serialized = str(settings.model_dump()).upper()
    assert settings.auth_context().authentication_type.value == "WORKLOAD_IDENTITY"
    assert "CLIENT_SECRET" not in serialized
    assert settings.external_providers_enabled is False


@pytest.mark.asyncio
async def test_official_sdk_initializes_lists_schemas_and_calls_tool(container) -> None:
    server = create_mcp_server(container)
    async with Client(server) as client:
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert len(tools) == 9
        assert set(tools) == set(container.registry.tools)
        assert "globalInstrumentId" in tools["get_research_readiness"].input_schema["properties"]
        result = await client.call_tool(
            "get_research_readiness", {"globalInstrumentId": str(INSTRUMENT_ID)}
        )
        assert result.is_error is False
        assert result.structured_content["ok"] is True
        assert result.structured_content["data"]["overallStatus"] == "READY"
        unknown = await client.call_tool("not_a_tool", {})
        assert unknown.is_error is True
        assert unknown.structured_content["error"]["code"] == "MCP_TOOL_NOT_FOUND"
        invalid = await client.call_tool("get_research_readiness", {})
        assert invalid.is_error is True
        assert invalid.structured_content["error"]["code"] == "INVALID_ARGUMENT"
    assert container.reader.closed is True
