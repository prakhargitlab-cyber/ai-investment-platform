from __future__ import annotations

import re
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class YahooMcpSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    environment: Literal["LOCAL", "TEST", "AZURE"] = Field(
        default="LOCAL",
        validation_alias=AliasChoices("AIP_YAHOO_MCP_ENVIRONMENT", "AIP_ENVIRONMENT"),
    )
    service_name: str = Field(
        default="yahoo-finance-mcp", validation_alias="AIP_YAHOO_MCP_SERVICE_NAME"
    )
    transport: Literal["stdio", "streamable-http"] = Field(
        default="stdio", validation_alias="AIP_YAHOO_MCP_TRANSPORT"
    )
    host: str = Field(default="127.0.0.1", validation_alias="AIP_YAHOO_MCP_HOST")
    port: int = Field(default=8002, ge=1, le=65535, validation_alias="AIP_YAHOO_MCP_PORT")
    http_path: str = Field(default="/mcp", validation_alias="AIP_YAHOO_MCP_HTTP_PATH")
    max_request_body_bytes: int = Field(
        default=131_072,
        ge=1024,
        le=1_048_576,
        validation_alias="AIP_YAHOO_MCP_MAX_REQUEST_BODY_BYTES",
    )
    max_response_items: int = Field(
        default=1000, ge=1, le=5000, validation_alias="AIP_YAHOO_MCP_MAX_RESPONSE_ITEMS"
    )
    upstream_timeout_seconds: float = Field(
        default=8.0, gt=0, le=60, validation_alias="AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS"
    )
    max_concurrency: int = Field(
        default=4, ge=1, le=32, validation_alias="AIP_YAHOO_MCP_MAX_CONCURRENCY"
    )
    user_agent: str = Field(
        default="AIInvestmentResearchBot/0.1 contact=research-compliance@example.invalid",
        validation_alias="AIP_YAHOO_MCP_USER_AGENT",
    )
    allowed_hosts_csv: str = Field(
        default="localhost,localhost:*,127.0.0.1,127.0.0.1:*",
        validation_alias="AIP_YAHOO_MCP_ALLOWED_HOSTS",
    )
    allowed_origins_csv: str = Field(
        default="", validation_alias="AIP_YAHOO_MCP_ALLOWED_ORIGINS"
    )

    @field_validator("http_path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        result = value.strip()
        if not result.startswith("/"):
            raise ValueError("AIP_YAHOO_MCP_HTTP_PATH must begin with /")
        return result

    @field_validator("user_agent")
    @classmethod
    def bounded_user_agent(cls, value: str) -> str:
        result = re.sub(r"[\r\n]", "", value).strip()
        if not 1 <= len(result) <= 300:
            raise ValueError("AIP_YAHOO_MCP_USER_AGENT is invalid")
        return result

    @property
    def allowed_hosts(self) -> list[str]:
        return _csv(self.allowed_hosts_csv)

    @property
    def allowed_origins(self) -> list[str]:
        return _csv(self.allowed_origins_csv)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
