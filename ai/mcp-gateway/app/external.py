"""Provider-neutral future external MCP registry and explicit fallback gate."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import StrEnum
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator

from app.contracts import (
    McpErrorCode,
    McpAuthContext,
    McpGatewayError,
    McpRiskClass,
    McpToolKind,
    StrictContract,
    sanitize_response_data,
)
from app.audit import McpAuditSink


class ExternalCapabilityState(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"


class ExternalProviderHealthState(StrEnum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class ExternalMcpProviderCapability(StrictContract):
    region: str
    requirement_id: str = Field(alias="requirementId", min_length=1)
    capability: str = Field(min_length=1)
    tool: str | None = None
    state: ExternalCapabilityState = ExternalCapabilityState.UNKNOWN
    minimum_items: int = Field(default=1, ge=1, le=1000, alias="minimumItems")
    max_age_seconds: int | None = Field(default=None, ge=1, le=31_536_000, alias="maxAgeSeconds")
    required_fields: tuple[str, ...] = Field(default=(), alias="requiredFields")
    tool_arguments: dict[str, Any] = Field(default_factory=dict, alias="toolArguments")

    @field_validator("region", "requirement_id", "capability")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("tool")
    @classmethod
    def normalize_tool(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("required_fields", mode="before")
    @classmethod
    def normalize_fields(cls, value) -> tuple[str, ...]:
        return tuple(str(item).strip() for item in (value or ()) if str(item).strip())


class ExternalMcpProviderMetadata(StrictContract):
    provider_id: str = Field(alias="providerId", min_length=1)
    kind: Literal[McpToolKind.EXTERNAL] = McpToolKind.EXTERNAL
    regions: tuple[str, ...]
    supported_requirements: tuple[str, ...] = Field(alias="supportedRequirements")
    supported_tools: tuple[str, ...] = Field(alias="supportedTools")
    risk_class: McpRiskClass = Field(alias="riskClass")
    auth_type: str = Field(alias="authType", min_length=1)
    priority: int = Field(default=100, ge=1, le=1000)
    source_tier: str = Field(default="APPROVED_EXTERNAL_TOOL", alias="sourceTier")
    health_state: ExternalProviderHealthState = Field(
        default=ExternalProviderHealthState.UNKNOWN, alias="healthState"
    )
    timeout_seconds: float = Field(default=10.0, gt=0, le=120, alias="timeoutSeconds")
    timeout_behavior: str = Field(default="BOUNDED_FAIL_CLOSED", alias="timeoutBehavior")
    adapter_version: str = Field(default="UNSPECIFIED", alias="adapterVersion")

    @field_validator("provider_id", "auth_type", "source_tier", "timeout_behavior")
    @classmethod
    def upper_identifier(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("regions", "supported_requirements", mode="before")
    @classmethod
    def upper_values(cls, value):
        return tuple(str(item).strip().upper() for item in value)

    @field_validator("supported_tools", mode="before")
    @classmethod
    def exact_tool_names(cls, value):
        return tuple(str(item).strip() for item in value)


class ProviderFallbackAuthorization(StrictContract):
    authorized: bool
    issued_by: str = Field(alias="issuedBy")
    global_instrument_id: UUID = Field(alias="globalInstrumentId")
    requirement_id: str = Field(alias="requirementId")
    permitted_provider_ids: tuple[str, ...] = Field(alias="permittedProviderIds")
    issued_at: datetime = Field(alias="issuedAt")

    @field_validator("issued_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issuedAt must be timezone-aware")
        return value.astimezone(timezone.utc)


class ExternalMcpProvider(Protocol):
    metadata: ExternalMcpProviderMetadata

    async def supports(self, *, region: str, requirement_id: str, tool: str) -> bool: ...

    def capability_for(
        self, *, region: str, requirement_id: str
    ) -> ExternalMcpProviderCapability | None: ...

    async def invoke(
        self, *, tool: str, arguments: dict[str, Any], request_id: str
    ) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...


class McpServerRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ExternalMcpProvider] = {}

    def register(self, provider: ExternalMcpProvider) -> None:
        provider_id = provider.metadata.provider_id
        if provider_id in self._providers:
            raise ValueError(f"External MCP provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> ExternalMcpProvider | None:
        return self._providers.get(provider_id.strip().upper())

    @property
    def count(self) -> int:
        return len(self._providers)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


class ExternalMcpGateway:
    """Future routing seam; no provider is registered by the production container in 5A."""

    def __init__(
        self,
        registry: McpServerRegistry,
        *,
        enabled: bool = False,
        timeout_seconds: float = 10,
        audit: McpAuditSink | None = None,
        auth: McpAuthContext | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.registry = registry
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.audit = audit
        self.auth = auth

    async def invoke_requirement(
        self,
        *,
        provider_id: str,
        region: str,
        requirement_id: str,
        global_instrument_id: UUID,
        arguments: dict[str, Any],
        request_id: str,
        authorization: ProviderFallbackAuthorization | None,
    ) -> dict[str, Any]:
        """Resolve a configured capability so callers cannot choose arbitrary provider tools."""
        try:
            self._authorize(
                provider_id=provider_id,
                requirement_id=requirement_id,
                global_instrument_id=global_instrument_id,
                authorization=authorization,
            )
        except McpGatewayError as exc:
            self._audit(
                "MCP_TOOL_DENIED",
                f"{provider_id}:{requirement_id}",
                request_id,
                code=exc.code,
            )
            raise
        provider = self.registry.get(provider_id)
        if provider is None:
            self._audit(
                "MCP_TOOL_FAILED",
                f"{provider_id}:{requirement_id}",
                request_id,
                code=McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE,
            )
            raise McpGatewayError(McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE)
        resolver = getattr(provider, "capability_for", None)
        capability = resolver(region=region, requirement_id=requirement_id) if resolver else None
        if (
            capability is None
            or capability.state is not ExternalCapabilityState.SUPPORTED
            or not capability.tool
        ):
            self._audit(
                "MCP_TOOL_FAILED",
                f"{provider_id}:{requirement_id}",
                request_id,
                code=McpErrorCode.EXTERNAL_CAPABILITY_UNSUPPORTED,
            )
            raise McpGatewayError(McpErrorCode.EXTERNAL_CAPABILITY_UNSUPPORTED)
        return await self.invoke(
            provider_id=provider_id,
            region=region,
            requirement_id=requirement_id,
            tool=capability.tool,
            global_instrument_id=global_instrument_id,
            arguments=arguments,
            request_id=request_id,
            authorization=authorization,
        )

    async def invoke(
        self,
        *,
        provider_id: str,
        region: str,
        requirement_id: str,
        tool: str,
        global_instrument_id: UUID,
        arguments: dict[str, Any],
        request_id: str,
        authorization: ProviderFallbackAuthorization | None,
    ) -> dict[str, Any]:
        started = monotonic()
        try:
            self._authorize(
                provider_id=provider_id,
                requirement_id=requirement_id,
                global_instrument_id=global_instrument_id,
                authorization=authorization,
            )
        except McpGatewayError as exc:
            self._audit("MCP_TOOL_DENIED", tool, request_id, code=exc.code)
            raise
        provider = self.registry.get(provider_id)
        if not self.enabled or provider is None:
            self._audit(
                "MCP_TOOL_FAILED",
                tool,
                request_id,
                code=McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE,
            )
            raise McpGatewayError(McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE)
        if provider.metadata.risk_class is not McpRiskClass.SAFE_READ:
            self._audit("MCP_TOOL_DENIED", tool, request_id, code=McpErrorCode.MCP_TOOL_DENIED)
            raise McpGatewayError(McpErrorCode.MCP_TOOL_DENIED)
        self._audit("MCP_TOOL_INVOKED", tool, request_id)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if not await provider.supports(
                    region=region, requirement_id=requirement_id, tool=tool
                ):
                    raise McpGatewayError(McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE)
                result = await provider.invoke(
                    tool=tool,
                    arguments={
                        **arguments,
                        "globalInstrumentId": str(global_instrument_id),
                        "region": region.strip().upper(),
                        "requirementId": requirement_id.strip().upper(),
                    },
                    request_id=request_id,
                )
        except TimeoutError as exc:
            self._audit(
                "MCP_TOOL_FAILED",
                tool,
                request_id,
                code=McpErrorCode.DOWNSTREAM_TIMEOUT,
                duration_ms=round((monotonic() - started) * 1000),
            )
            raise McpGatewayError(McpErrorCode.DOWNSTREAM_TIMEOUT) from exc
        except McpGatewayError as exc:
            self._audit(
                "MCP_TOOL_FAILED",
                tool,
                request_id,
                code=exc.code,
                duration_ms=round((monotonic() - started) * 1000),
            )
            raise
        except Exception as exc:
            self._audit(
                "MCP_TOOL_FAILED",
                tool,
                request_id,
                code=McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE,
                duration_ms=round((monotonic() - started) * 1000),
            )
            raise McpGatewayError(McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE) from exc
        try:
            self._enforce_identity_boundary(result, global_instrument_id)
        except McpGatewayError as exc:
            self._audit(
                "MCP_TOOL_FAILED",
                tool,
                request_id,
                code=exc.code,
                duration_ms=round((monotonic() - started) * 1000),
            )
            raise
        normalized = sanitize_response_data(result)
        self._audit(
            "MCP_TOOL_SUCCEEDED",
            tool,
            request_id,
            duration_ms=round((monotonic() - started) * 1000),
        )
        return normalized

    def _audit(
        self,
        event: str,
        tool: str,
        request_id: str,
        *,
        code: McpErrorCode | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if self.audit is None or self.auth is None:
            return
        self.audit.emit(
            event,
            tool=tool,
            request_id=request_id,
            auth=self.auth,
            risk_class=McpRiskClass.SAFE_READ,
            kind=McpToolKind.EXTERNAL,
            code=code.value if code else None,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _authorize(
        *, provider_id, requirement_id, global_instrument_id, authorization
    ) -> None:
        if (
            authorization is None
            or not authorization.authorized
            or authorization.issued_by != "ProviderFallbackPolicy"
            or authorization.global_instrument_id != global_instrument_id
            or authorization.requirement_id.upper() != requirement_id.upper()
            or provider_id.upper()
            not in {value.upper() for value in authorization.permitted_provider_ids}
        ):
            raise McpGatewayError(McpErrorCode.FORBIDDEN)

    @staticmethod
    def _enforce_identity_boundary(result: dict[str, Any], global_instrument_id: UUID) -> None:
        forbidden = {"canonicalidentity", "providermapping", "providermappings", "createidentity"}

        def inspect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = "".join(char for char in str(key).lower() if char.isalnum())
                    if normalized in forbidden:
                        raise McpGatewayError(McpErrorCode.FORBIDDEN)
                    if normalized == "globalinstrumentid" and str(item) != str(
                        global_instrument_id
                    ):
                        raise McpGatewayError(McpErrorCode.FORBIDDEN)
                    inspect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    inspect(item)

        inspect(result)
