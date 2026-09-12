"""Central LOCAL/TEST/AZURE MCP gateway settings."""
from __future__ import annotations

import json
import re
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.contracts import McpAuthContext, McpAuthenticationType


class McpGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    environment: Literal["LOCAL", "TEST", "AZURE"] = Field(
        default="LOCAL",
        validation_alias=AliasChoices("AIP_MCP_ENVIRONMENT", "AIP_ENVIRONMENT"),
    )
    service_name: str = Field(default="mcp-gateway", validation_alias="AIP_MCP_SERVICE_NAME")
    feature_enabled: bool = Field(default=True, validation_alias="AIP_FEATURE_MCP_ENABLED")
    transport: Literal["stdio", "streamable-http"] = Field(
        default="stdio", validation_alias="AIP_MCP_TRANSPORT"
    )
    host: str = Field(default="127.0.0.1", validation_alias="AIP_MCP_HOST")
    port: int = Field(default=8001, ge=1, le=65535, validation_alias="AIP_MCP_PORT")
    streamable_http_path: str = Field(default="/mcp", validation_alias="AIP_MCP_HTTP_PATH")
    max_request_body_bytes: int = Field(
        default=262_144, ge=1024, le=4_194_304, validation_alias="AIP_MCP_MAX_REQUEST_BODY_BYTES"
    )
    allowed_hosts_csv: str = Field(
        default="localhost,localhost:*,127.0.0.1,127.0.0.1:*",
        validation_alias="AIP_MCP_ALLOWED_HOSTS",
    )
    allowed_origins_csv: str = Field(default="", validation_alias="AIP_MCP_ALLOWED_ORIGINS")

    research_base_url: str = Field(
        default="http://127.0.0.1:8000", validation_alias="AIP_MCP_RESEARCH_BASE_URL"
    )
    invocation_timeout_seconds: float = Field(
        default=10.0, gt=0, le=120, validation_alias="AIP_MCP_INVOCATION_TIMEOUT_SECONDS"
    )
    http_connect_timeout_seconds: float = Field(
        default=3.0, gt=0, le=60, validation_alias="AIP_MCP_HTTP_CONNECT_TIMEOUT_SECONDS"
    )
    http_read_timeout_seconds: float = Field(
        default=8.0, gt=0, le=120, validation_alias="AIP_MCP_HTTP_READ_TIMEOUT_SECONDS"
    )
    http_max_connections: int = Field(
        default=50, ge=1, le=500, validation_alias="AIP_MCP_HTTP_MAX_CONNECTIONS"
    )
    http_max_keepalive_connections: int = Field(
        default=20, ge=0, le=200, validation_alias="AIP_MCP_HTTP_MAX_KEEPALIVE_CONNECTIONS"
    )
    service_identity: str = Field(
        default="local-mcp-gateway", validation_alias="AIP_MCP_SERVICE_IDENTITY"
    )
    authentication_type: McpAuthenticationType | None = Field(
        default=None, validation_alias="AIP_MCP_AUTHENTICATION_TYPE"
    )
    local_user_id: UUID | None = Field(default=None, validation_alias="AIP_MCP_LOCAL_USER_ID")
    roles_csv: str = Field(default="MCP_READER", validation_alias="AIP_MCP_ROLES")
    scopes_csv: str = Field(
        default="mcp:read", validation_alias="AIP_MCP_SCOPES"
    )
    external_providers_enabled: bool = Field(
        default=False, validation_alias="AIP_MCP_EXTERNAL_PROVIDERS_ENABLED"
    )
    external_caller_identities_csv: str = Field(
        default="research-engine", validation_alias="AIP_MCP_EXTERNAL_CALLER_IDENTITIES"
    )
    yahoo_finance_mcp_enabled: bool = Field(
        default=False, validation_alias="AIP_MCP_YAHOO_ENABLED"
    )
    yahoo_finance_mcp_transport: Literal["streamable-http", "stdio"] = Field(
        default="streamable-http", validation_alias="AIP_MCP_YAHOO_TRANSPORT"
    )
    yahoo_finance_mcp_endpoint: str | None = Field(
        default=None, validation_alias="AIP_MCP_YAHOO_ENDPOINT"
    )
    yahoo_finance_mcp_stdio_command: str | None = Field(
        default=None, validation_alias="AIP_MCP_YAHOO_STDIO_COMMAND"
    )
    yahoo_finance_mcp_stdio_args_json: str = Field(
        default="[]", validation_alias="AIP_MCP_YAHOO_STDIO_ARGS_JSON"
    )
    yahoo_finance_mcp_auth_type: str = Field(
        default="NONE", validation_alias="AIP_MCP_YAHOO_AUTH_TYPE"
    )
    yahoo_finance_mcp_auth_header_name: str = Field(
        default="Authorization", validation_alias="AIP_MCP_YAHOO_AUTH_HEADER_NAME"
    )
    yahoo_finance_mcp_auth_token: SecretStr | None = Field(
        default=None, validation_alias="AIP_MCP_YAHOO_AUTH_TOKEN"
    )
    yahoo_finance_mcp_stdio_token_env_name: str | None = Field(
        default=None, validation_alias="AIP_MCP_YAHOO_STDIO_TOKEN_ENV_NAME"
    )
    yahoo_finance_mcp_capabilities_json: str = Field(
        default="[]", validation_alias="AIP_MCP_YAHOO_CAPABILITIES_JSON"
    )
    yahoo_finance_mcp_timeout_seconds: float = Field(
        default=8.0, gt=0, le=120, validation_alias="AIP_MCP_YAHOO_TIMEOUT_SECONDS"
    )
    yahoo_finance_mcp_max_retries: int = Field(
        default=1, ge=0, le=3, validation_alias="AIP_MCP_YAHOO_MAX_RETRIES"
    )
    yahoo_finance_mcp_retry_backoff_seconds: float = Field(
        default=0.2, ge=0, le=10, validation_alias="AIP_MCP_YAHOO_RETRY_BACKOFF_SECONDS"
    )
    yahoo_finance_mcp_max_concurrency: int = Field(
        default=4, ge=1, le=32, validation_alias="AIP_MCP_YAHOO_MAX_CONCURRENCY"
    )

    @field_validator("streamable_http_path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            raise ValueError("AIP_MCP_HTTP_PATH must begin with /")
        return path

    @field_validator("research_base_url")
    @classmethod
    def valid_research_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("AIP_MCP_RESEARCH_BASE_URL must be HTTP(S)")
        return url

    @field_validator("yahoo_finance_mcp_endpoint")
    @classmethod
    def valid_yahoo_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("AIP_MCP_YAHOO_ENDPOINT must be HTTP(S)")
        return url

    @field_validator("yahoo_finance_mcp_auth_type")
    @classmethod
    def valid_yahoo_auth_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"NONE", "BEARER", "HEADER", "WORKLOAD_IDENTITY"}:
            raise ValueError("Unsupported Yahoo MCP authentication type")
        return normalized

    @field_validator("yahoo_finance_mcp_auth_header_name")
    @classmethod
    def valid_yahoo_auth_header(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", normalized):
            raise ValueError("Yahoo MCP auth header name is invalid")
        return normalized

    @field_validator("yahoo_finance_mcp_stdio_token_env_name")
    @classmethod
    def valid_yahoo_token_env(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,99}", normalized):
            raise ValueError("Yahoo MCP token environment name is invalid")
        if normalized in {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "TMP", "TEMP"}:
            raise ValueError("Yahoo MCP token cannot replace a process environment setting")
        return normalized

    @field_validator("yahoo_finance_mcp_stdio_args_json", "yahoo_finance_mcp_capabilities_json")
    @classmethod
    def valid_json_array(cls, value: str) -> str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Yahoo MCP JSON configuration must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise ValueError("Yahoo MCP JSON configuration must be an array")
        return value

    @property
    def allowed_hosts(self) -> list[str]:
        return _csv(self.allowed_hosts_csv)

    @property
    def allowed_origins(self) -> list[str]:
        return _csv(self.allowed_origins_csv)

    @property
    def external_caller_identities(self) -> frozenset[str]:
        return frozenset(_csv(self.external_caller_identities_csv))

    @property
    def yahoo_finance_mcp_stdio_args(self) -> tuple[str, ...]:
        return tuple(str(value) for value in json.loads(self.yahoo_finance_mcp_stdio_args_json))

    def auth_context(self) -> McpAuthContext:
        auth_type = self.authentication_type or (
            McpAuthenticationType.WORKLOAD_IDENTITY
            if self.environment == "AZURE"
            else McpAuthenticationType.LOCAL_SERVICE
        )
        return McpAuthContext(
            userId=self.local_user_id,
            serviceIdentity=self.service_identity,
            roles=tuple(_csv(self.roles_csv)),
            scopes=tuple(_csv(self.scopes_csv)),
            authenticationType=auth_type,
        )


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
