from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.contracts import McpErrorCode, McpGatewayError, McpRiskClass
from app.external import (
    ExternalMcpGateway,
    ExternalMcpProviderMetadata,
    McpServerRegistry,
    ProviderFallbackAuthorization,
)
from conftest import INSTRUMENT_ID


class MockExternalProvider:
    metadata = ExternalMcpProviderMetadata(
        providerId="TEST_MCP",
        regions=("INDIA",),
        supportedRequirements=("CURRENT_NEWS",),
        supportedTools=("SEARCH_NEWS",),
        riskClass=McpRiskClass.SAFE_READ,
        authType="MOCK",
    )

    def __init__(self, *, supported=True, result=None) -> None:
        self.supported = supported
        self.result = result or {"globalInstrumentId": str(INSTRUMENT_ID), "evidence": []}
        self.calls = 0

    async def supports(self, **_kwargs) -> bool:
        return self.supported

    async def invoke(self, **_kwargs):
        self.calls += 1
        return self.result

    async def health(self):
        return {"status": "UP"}


def authorization(**overrides) -> ProviderFallbackAuthorization:
    values = {
        "authorized": True,
        "issuedBy": "ProviderFallbackPolicy",
        "globalInstrumentId": INSTRUMENT_ID,
        "requirementId": "CURRENT_NEWS",
        "permittedProviderIds": ("TEST_MCP",),
        "issuedAt": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return ProviderFallbackAuthorization(**values)


def test_mock_external_provider_can_register_and_duplicates_fail() -> None:
    registry = McpServerRegistry()
    provider = MockExternalProvider()
    registry.register(provider)
    assert registry.get("test_mcp") is provider
    assert provider.metadata.kind.value == "EXTERNAL"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(provider)


@pytest.mark.asyncio
async def test_unsupported_requirement_is_rejected() -> None:
    registry = McpServerRegistry()
    provider = MockExternalProvider(supported=False)
    registry.register(provider)
    gateway = ExternalMcpGateway(registry, enabled=True)
    with pytest.raises(McpGatewayError) as error:
        await gateway.invoke(
            provider_id="TEST_MCP",
            region="INDIA",
            requirement_id="CURRENT_NEWS",
            tool="SEARCH_NEWS",
            global_instrument_id=INSTRUMENT_ID,
            arguments={},
            request_id="external-5a",
            authorization=authorization(),
        )
    assert error.value.code == McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_fallback_requires_explicit_policy_authorization() -> None:
    registry = McpServerRegistry()
    provider = MockExternalProvider()
    registry.register(provider)
    gateway = ExternalMcpGateway(registry, enabled=True)
    with pytest.raises(McpGatewayError) as error:
        await gateway.invoke(
            provider_id="TEST_MCP",
            region="INDIA",
            requirement_id="CURRENT_NEWS",
            tool="SEARCH_NEWS",
            global_instrument_id=INSTRUMENT_ID,
            arguments={},
            request_id="external-5a",
            authorization=None,
        )
    assert error.value.code == McpErrorCode.FORBIDDEN
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_external_provider_cannot_create_or_change_canonical_identity() -> None:
    for result in (
        {"createIdentity": {"symbol": "NEW"}},
        {"globalInstrumentId": "99999999-9999-4999-8999-999999999999"},
        {"nested": {"providerMappings": [{"symbol": "NEW"}]}},
    ):
        registry = McpServerRegistry()
        provider = MockExternalProvider(result=result)
        registry.register(provider)
        gateway = ExternalMcpGateway(registry, enabled=True)
        with pytest.raises(McpGatewayError) as error:
            await gateway.invoke(
                provider_id="TEST_MCP",
                region="INDIA",
                requirement_id="CURRENT_NEWS",
                tool="SEARCH_NEWS",
                global_instrument_id=INSTRUMENT_ID,
                arguments={},
                request_id="external-5a",
                authorization=authorization(),
            )
        assert error.value.code == McpErrorCode.FORBIDDEN


@pytest.mark.asyncio
async def test_policy_authorized_external_result_keeps_canonical_identity_and_is_sanitized() -> None:
    registry = McpServerRegistry()
    provider = MockExternalProvider(
        result={
            "globalInstrumentId": str(INSTRUMENT_ID),
            "evidence": [{"title": "safe", "apiKey": "must-not-leak"}],
        }
    )
    registry.register(provider)
    gateway = ExternalMcpGateway(registry, enabled=True)
    result = await gateway.invoke(
        provider_id="TEST_MCP",
        region="INDIA",
        requirement_id="CURRENT_NEWS",
        tool="SEARCH_NEWS",
        global_instrument_id=INSTRUMENT_ID,
        arguments={},
        request_id="external-allowed-5a",
        authorization=authorization(),
    )
    assert result == {
        "globalInstrumentId": str(INSTRUMENT_ID),
        "evidence": [{"title": "safe"}],
    }
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_external_provider_exception_is_normalized() -> None:
    class FailedProvider(MockExternalProvider):
        async def invoke(self, **_kwargs):
            raise RuntimeError("provider credential and stack must not escape")

    registry = McpServerRegistry()
    registry.register(FailedProvider())
    gateway = ExternalMcpGateway(registry, enabled=True)
    with pytest.raises(McpGatewayError) as error:
        await gateway.invoke(
            provider_id="TEST_MCP",
            region="INDIA",
            requirement_id="CURRENT_NEWS",
            tool="SEARCH_NEWS",
            global_instrument_id=INSTRUMENT_ID,
            arguments={},
            request_id="external-failure-5a",
            authorization=authorization(),
        )
    assert error.value.code == McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE
    assert "credential" not in str(error.value)


@pytest.mark.asyncio
async def test_non_safe_external_provider_is_denied_before_invocation() -> None:
    provider = MockExternalProvider()
    provider.metadata = provider.metadata.model_copy(
        update={"risk_class": McpRiskClass.FINANCIAL_ACTION}
    )
    registry = McpServerRegistry()
    registry.register(provider)
    gateway = ExternalMcpGateway(registry, enabled=True)
    with pytest.raises(McpGatewayError) as error:
        await gateway.invoke(
            provider_id="TEST_MCP",
            region="INDIA",
            requirement_id="CURRENT_NEWS",
            tool="SEARCH_NEWS",
            global_instrument_id=INSTRUMENT_ID,
            arguments={},
            request_id="external-denied-5a",
            authorization=authorization(),
        )
    assert error.value.code == McpErrorCode.MCP_TOOL_DENIED
    assert provider.calls == 0
