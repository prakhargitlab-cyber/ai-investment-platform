"""Provider-neutral MCP contracts and safe result envelopes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class McpRiskClass(StrEnum):
    SAFE_READ = "SAFE_READ"
    SENSITIVE_READ = "SENSITIVE_READ"
    WRITE_NON_FINANCIAL = "WRITE_NON_FINANCIAL"
    FINANCIAL_ACTION = "FINANCIAL_ACTION"
    ADMIN_ACTION = "ADMIN_ACTION"


class McpToolKind(StrEnum):
    INTERNAL = "INTERNAL"
    EXTERNAL = "EXTERNAL"


class McpAuthenticationType(StrEnum):
    LOCAL_SERVICE = "LOCAL_SERVICE"
    WORKLOAD_IDENTITY = "WORKLOAD_IDENTITY"
    FEDERATED_USER = "FEDERATED_USER"
    TEST = "TEST"


class McpErrorCode(StrEnum):
    MCP_TOOL_NOT_FOUND = "MCP_TOOL_NOT_FOUND"
    MCP_TOOL_DENIED = "MCP_TOOL_DENIED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    COMPANY_NOT_RESOLVED = "COMPANY_NOT_RESOLVED"
    READINESS_NOT_AVAILABLE = "READINESS_NOT_AVAILABLE"
    ANALYSIS_NOT_AVAILABLE = "ANALYSIS_NOT_AVAILABLE"
    DOWNSTREAM_TIMEOUT = "DOWNSTREAM_TIMEOUT"
    DOWNSTREAM_UNAVAILABLE = "DOWNSTREAM_UNAVAILABLE"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    EXTERNAL_PROVIDER_UNAVAILABLE = "EXTERNAL_PROVIDER_UNAVAILABLE"
    EXTERNAL_CAPABILITY_UNSUPPORTED = "EXTERNAL_CAPABILITY_UNSUPPORTED"
    EXTERNAL_SCHEMA_INVALID = "EXTERNAL_SCHEMA_INVALID"
    EXTERNAL_RESULT_INCOMPLETE = "EXTERNAL_RESULT_INCOMPLETE"
    EXTERNAL_IDENTITY_CONFLICT = "EXTERNAL_IDENTITY_CONFLICT"
    EXTERNAL_RESULT_STALE = "EXTERNAL_RESULT_STALE"
    EXTERNAL_PROVIDER_RATE_LIMITED = "EXTERNAL_PROVIDER_RATE_LIMITED"


SAFE_ERROR_MESSAGES: dict[McpErrorCode, str] = {
    McpErrorCode.MCP_TOOL_NOT_FOUND: "The requested MCP tool is not registered.",
    McpErrorCode.MCP_TOOL_DENIED: "The requested MCP tool is not permitted by policy.",
    McpErrorCode.INVALID_ARGUMENT: "The tool arguments are invalid.",
    McpErrorCode.COMPANY_NOT_RESOLVED: "The canonical global instrument was not resolved.",
    McpErrorCode.READINESS_NOT_AVAILABLE: "Research readiness is not available.",
    McpErrorCode.ANALYSIS_NOT_AVAILABLE: "Company analysis is not available.",
    McpErrorCode.DOWNSTREAM_TIMEOUT: "The internal application request timed out.",
    McpErrorCode.DOWNSTREAM_UNAVAILABLE: "The internal application capability is unavailable.",
    McpErrorCode.UNAUTHORIZED: "An authenticated MCP identity is required.",
    McpErrorCode.FORBIDDEN: "The MCP identity is not authorized for this operation.",
    McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE: "No approved external MCP provider is available.",
    McpErrorCode.EXTERNAL_CAPABILITY_UNSUPPORTED: "The approved external provider does not support this capability.",
    McpErrorCode.EXTERNAL_SCHEMA_INVALID: "The external provider returned an invalid schema.",
    McpErrorCode.EXTERNAL_RESULT_INCOMPLETE: "The external provider result did not satisfy the requirement.",
    McpErrorCode.EXTERNAL_IDENTITY_CONFLICT: "The external provider result conflicts with canonical identity.",
    McpErrorCode.EXTERNAL_RESULT_STALE: "The external provider result is outside the allowed freshness window.",
    McpErrorCode.EXTERNAL_PROVIDER_RATE_LIMITED: "The external provider is temporarily rate limited.",
}


class McpAuthContext(StrictContract):
    user_id: UUID | None = Field(default=None, alias="userId")
    service_identity: str = Field(min_length=1, max_length=200, alias="serviceIdentity")
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    authentication_type: McpAuthenticationType = Field(alias="authenticationType")

    @field_validator("roles", "scopes", mode="before")
    @classmethod
    def normalize_authorities(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        values = value.split(",") if isinstance(value, str) else value
        return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))

    def identity_headers(self) -> dict[str, str]:
        headers = {
            "X-AIP-Service-Identity": self.service_identity,
            "X-AIP-Authentication-Type": self.authentication_type.value,
        }
        if self.user_id is not None:
            user_id = str(self.user_id)
            headers.update(
                {
                    "X-AIP-User-Id": user_id,
                    "X-AIP-User-Issuer": self.service_identity,
                    "X-AIP-User-Subject": user_id,
                }
            )
        if self.roles:
            headers["X-AIP-User-Roles"] = ",".join(self.roles)
        return headers


class McpProvenance(StrictContract):
    source: str = "INTERNAL_APPLICATION"
    generated_at: datetime = Field(alias="generatedAt")
    rule_engine_version: str | None = Field(default=None, alias="ruleEngineVersion")


class McpError(StrictContract):
    code: McpErrorCode
    message: str


class McpResultEnvelope(StrictContract):
    ok: bool
    tool: str
    request_id: str = Field(alias="requestId")
    data: Any | None = None
    provenance: McpProvenance | None = None
    warnings: list[str] = Field(default_factory=list)
    error: McpError | None = None

    @classmethod
    def success(
        cls,
        *,
        tool: str,
        request_id: str,
        data: Any,
        generated_at: datetime | None = None,
        rule_engine_version: str | None = None,
        warnings: list[str] | None = None,
    ) -> "McpResultEnvelope":
        return cls(
            ok=True,
            tool=tool,
            requestId=request_id,
            data=sanitize_response_data(data),
            provenance=McpProvenance(
                generatedAt=generated_at or datetime.now(timezone.utc),
                ruleEngineVersion=rule_engine_version,
            ),
            warnings=warnings or [],
        )

    @classmethod
    def failure(
        cls, *, tool: str, request_id: str, code: McpErrorCode
    ) -> "McpResultEnvelope":
        return cls(
            ok=False,
            tool=tool,
            requestId=request_id,
            error=McpError(code=code, message=SAFE_ERROR_MESSAGES[code]),
        )

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class McpGatewayError(Exception):
    def __init__(self, code: McpErrorCode) -> None:
        super().__init__(SAFE_ERROR_MESSAGES[code])
        self.code = code


@dataclass(frozen=True)
class McpToolExecution:
    data: Any
    generated_at: datetime | None = None
    rule_engine_version: str | None = None
    warnings: tuple[str, ...] = ()


ToolHandler = Callable[[BaseModel, McpAuthContext, str], Awaitable[McpToolExecution]]


@dataclass(frozen=True)
class McpToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    risk_class: McpRiskClass
    kind: McpToolKind
    handler: ToolHandler
    required_scopes: tuple[str, ...] = ("mcp:read",)
    requires_user: bool = False


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SENSITIVE_KEYS = {
    "quantity",
    "averagecost",
    "investedamount",
    "pnl",
    "pl",
    "profitloss",
    "profitandloss",
    "costbasis",
    "allocation",
    "brokeraccountid",
    "authorization",
    "cookie",
    "token",
    "oauthtoken",
    "apikey",
    "api_key",
    "oauthcode",
    "oauthcredential",
}
_SENSITIVE_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "credential",
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)"
    r"\s*[:=]\s*[^\s&,;]+"
)


def normalize_request_id(value: str | int | None) -> str:
    candidate = "" if value is None else str(value).strip()
    return candidate if _REQUEST_ID_PATTERN.fullmatch(candidate) else str(uuid4())


def sanitize_response_data(value: Any) -> Any:
    """Recursively remove private financial and credential-shaped response fields."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            compact = normalized.replace("_", "")
            if normalized in _SENSITIVE_KEYS or compact in _SENSITIVE_KEYS:
                continue
            if any(part in compact for part in _SENSITIVE_KEY_PARTS):
                continue
            result[str(key)] = sanitize_response_data(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_response_data(item) for item in value]
    if isinstance(value, str):
        if _BEARER_PATTERN.search(value) or _JWT_PATTERN.search(value):
            return "<redacted>"
        return _CREDENTIAL_ASSIGNMENT_PATTERN.sub(r"\1=<redacted>", value)
    return value
