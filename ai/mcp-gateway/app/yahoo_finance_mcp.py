"""Yahoo Finance MCP adapter with explicit capabilities and strict normalization.

The repository-owned first-party server returns the versioned contract validated
below. A capability remains callable only when configuration marks one exact
region/requirement/tool mapping SUPPORTED; malformed or incomplete results fail
closed so policy can use the existing regional fallback.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, AsyncContextManager, Callable, Protocol
from urllib.parse import quote

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from opentelemetry.propagate import inject
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from app.contracts import McpErrorCode, McpGatewayError, McpRiskClass, StrictContract
from app.external import (
    ExternalCapabilityState,
    ExternalMcpProviderCapability,
    ExternalMcpProviderMetadata,
    ExternalProviderHealthState,
)
from app.resilience import McpCircuitBreaker


PROVIDER_ID = "YAHOO_FINANCE_MCP"
ADAPTER_VERSION = "YAHOO_FINANCE_MCP_ADAPTER_V1"
TOOL_SCHEMA_VERSION = "YAHOO_FINANCE_MCP_TOOL_V1"
SUPPORTED_REGIONS = ("INDIA", "USA", "EUROPE")


class _RetryableProviderFailure(Exception):
    def __init__(self, code: McpErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


_FIRST_PARTY_ERRORS = {
    "YAHOO_MCP_UPSTREAM_TIMEOUT": McpErrorCode.DOWNSTREAM_TIMEOUT,
    "YAHOO_MCP_UPSTREAM_UNAVAILABLE": McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE,
    "YAHOO_MCP_RATE_LIMITED": McpErrorCode.EXTERNAL_PROVIDER_RATE_LIMITED,
    "YAHOO_MCP_UNSUPPORTED": McpErrorCode.EXTERNAL_CAPABILITY_UNSUPPORTED,
    "YAHOO_MCP_INVALID_RESPONSE": McpErrorCode.EXTERNAL_SCHEMA_INVALID,
    "YAHOO_MCP_INCOMPLETE": McpErrorCode.EXTERNAL_RESULT_INCOMPLETE,
    "YAHOO_MCP_IDENTITY_MISMATCH": McpErrorCode.EXTERNAL_IDENTITY_CONFLICT,
}

_READINESS_CATALYST_EVENT_TYPES = frozenset(
    {
        "NEW_ORDER",
        "ORDER_BACKLOG_CHANGE",
        "MAJOR_CONTRACT",
        "GOVERNMENT_CONTRACT",
        "ORDER_CANCELLED",
        "CAPEX",
        "FACTORY_EXPANSION",
        "CAPACITY_EXPANSION",
        "NEW_FACILITY",
        "PROJECT_DELAY",
        "MANAGEMENT_GUIDANCE",
        "GUIDANCE_RAISED",
        "GUIDANCE_LOWERED",
        "GUIDANCE_CUT",
        "GUIDANCE_MAINTAINED",
        "REVENUE_GUIDANCE",
        "MARGIN_GUIDANCE",
    }
)


class YahooMcpCapability(StrEnum):
    LATEST_PRICE = "LATEST_PRICE"
    MARKET_HISTORY = "MARKET_HISTORY"
    COMPANY_PROFILE = "COMPANY_PROFILE"
    VALUATION_INPUTS = "VALUATION_INPUTS"
    ANNUAL_FINANCIALS = "ANNUAL_FINANCIALS"
    QUARTERLY_FINANCIALS = "QUARTERLY_FINANCIALS"
    BALANCE_SHEET = "BALANCE_SHEET"
    INCOME_STATEMENT = "INCOME_STATEMENT"
    CASH_FLOW = "CASH_FLOW"
    GROWTH_INPUTS = "GROWTH_INPUTS"
    EARNINGS_TREND = "EARNINGS_TREND"
    NEWS = "NEWS"
    CATALYSTS_EVENTS = "CATALYSTS_EVENTS"
    SHAREHOLDING = "SHAREHOLDING"
    SECTOR_INDUSTRY = "SECTOR_INDUSTRY"
    ANALYST_DATA = "ANALYST_DATA"
    TECHNICAL_PRICE_INPUTS = "TECHNICAL_PRICE_INPUTS"


_REQUIREMENT_CAPABILITY = {
    "LATEST_PRICE": YahooMcpCapability.LATEST_PRICE,
    "HISTORICAL_PRICE_SERIES": YahooMcpCapability.MARKET_HISTORY,
    "VALUATION_INPUTS": YahooMcpCapability.VALUATION_INPUTS,
    "BUSINESS_QUALITY_FACTS": YahooMcpCapability.ANNUAL_FINANCIALS,
    "GROWTH_FACTS": YahooMcpCapability.GROWTH_INPUTS,
    "BALANCE_SHEET_FACTS": YahooMcpCapability.BALANCE_SHEET,
    "QUARTERLY_FINANCIALS": YahooMcpCapability.QUARTERLY_FINANCIALS,
    "CURRENT_NEWS": YahooMcpCapability.NEWS,
    "ORDER_BOOK_CAPEX_GUIDANCE": YahooMcpCapability.CATALYSTS_EVENTS,
    "SHAREHOLDING": YahooMcpCapability.SHAREHOLDING,
    "SECTOR_MACRO": YahooMcpCapability.SECTOR_INDUSTRY,
    "COMPANY_PROFILE": YahooMcpCapability.COMPANY_PROFILE,
    "ANALYST_DATA": YahooMcpCapability.ANALYST_DATA,
}


class YahooFinanceCapabilityRegistry:
    """One configured capability decision for every region/requirement pair."""

    def __init__(self, capabilities: list[ExternalMcpProviderCapability]) -> None:
        values = {(item.region, item.requirement_id): item for item in capabilities}
        if len(values) != len(capabilities):
            raise ValueError("Yahoo MCP capabilities must be unique by region and requirement")
        self._values = values

    @classmethod
    def from_json(cls, value: str | None) -> "YahooFinanceCapabilityRegistry":
        try:
            configured = json.loads(value or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("AIP_MCP_YAHOO_CAPABILITIES_JSON must be valid JSON") from exc
        if not isinstance(configured, list):
            raise ValueError("AIP_MCP_YAHOO_CAPABILITIES_JSON must be a JSON array")
        defaults = {
            (region, requirement): ExternalMcpProviderCapability(
                region=region,
                requirementId=requirement,
                capability=capability.value,
                state=(
                    ExternalCapabilityState.UNSUPPORTED
                    if capability is YahooMcpCapability.SHAREHOLDING
                    else ExternalCapabilityState.UNKNOWN
                ),
            )
            for region in SUPPORTED_REGIONS
            for requirement, capability in _REQUIREMENT_CAPABILITY.items()
        }
        for raw in configured:
            capability = ExternalMcpProviderCapability.model_validate(raw)
            expected = _REQUIREMENT_CAPABILITY.get(capability.requirement_id)
            if expected is None or capability.capability != expected.value:
                raise ValueError(
                    f"Unsupported Yahoo MCP requirement/capability mapping: "
                    f"{capability.requirement_id}/{capability.capability}"
                )
            if capability.region not in SUPPORTED_REGIONS:
                raise ValueError(f"Unsupported Yahoo MCP region: {capability.region}")
            if capability.state is ExternalCapabilityState.SUPPORTED and not capability.tool:
                raise ValueError("A SUPPORTED Yahoo MCP capability requires an exact tool name")
            _reject_sensitive_tool_arguments(capability.tool_arguments)
            defaults[(capability.region, capability.requirement_id)] = capability
        return cls(list(defaults.values()))

    def capability_for(
        self, *, region: str, requirement_id: str
    ) -> ExternalMcpProviderCapability | None:
        return self._values.get((region.strip().upper(), requirement_id.strip().upper()))

    @property
    def capabilities(self) -> tuple[ExternalMcpProviderCapability, ...]:
        return tuple(self._values[key] for key in sorted(self._values))

    @property
    def supported(self) -> tuple[ExternalMcpProviderCapability, ...]:
        return tuple(
            item
            for item in self.capabilities
            if item.state is ExternalCapabilityState.SUPPORTED and item.tool
        )


@dataclass(frozen=True)
class YahooFinanceMcpConfig:
    transport: str
    endpoint: str | None
    stdio_command: str | None
    stdio_args: tuple[str, ...]
    auth_type: str
    auth_header_name: str
    auth_token: SecretStr | None
    stdio_token_env_name: str | None
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    max_concurrency: int

    def __post_init__(self) -> None:
        transport = self.transport.strip().lower()
        auth_type = self.auth_type.strip().upper()
        if transport not in {"streamable-http", "stdio"}:
            raise ValueError("Yahoo MCP transport must be streamable-http or stdio")
        if transport == "streamable-http" and not self.endpoint:
            raise ValueError("Yahoo MCP Streamable HTTP requires an endpoint")
        if transport == "stdio" and not self.stdio_command:
            raise ValueError("Yahoo MCP STDIO requires an executable")
        if self.timeout_seconds <= 0 or self.max_concurrency < 1:
            raise ValueError("Yahoo MCP timeout and concurrency must be positive")
        if self.max_retries not in range(0, 4) or self.retry_backoff_seconds < 0:
            raise ValueError("Yahoo MCP retry configuration is outside the safe bound")
        if auth_type in {"BEARER", "HEADER"} and self.auth_token is None:
            raise ValueError("Yahoo MCP configured authentication requires a secret reference")
        if auth_type in {"NONE", "WORKLOAD_IDENTITY"} and self.auth_token is not None:
            raise ValueError("Yahoo MCP token is not valid for the configured authentication type")
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "auth_type", auth_type)


class YahooMcpClient(Protocol):
    async def list_tools(self): ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs): ...


class YahooMcpClientFactory(Protocol):
    def connect(self, request_id: str) -> AsyncContextManager[YahooMcpClient]: ...


class OfficialYahooMcpClientFactory:
    """Build official SDK clients without putting credentials in URLs or logs."""

    def __init__(self, config: YahooFinanceMcpConfig) -> None:
        self.config = config

    @asynccontextmanager
    async def connect(self, request_id: str):
        if self.config.transport == "stdio":
            environment = {
                key: value
                for key in ("PATH", "SYSTEMROOT", "WINDIR", "HOME", "TMP", "TEMP")
                if (value := os.environ.get(key))
            }
            environment["AIP_REQUEST_ID"] = request_id
            environment["AIP_CORRELATION_ID"] = request_id
            if self.config.auth_token and self.config.stdio_token_env_name:
                environment[self.config.stdio_token_env_name] = (
                    self.config.auth_token.get_secret_value()
                )
            parameters = StdioServerParameters(
                command=str(self.config.stdio_command),
                args=list(self.config.stdio_args),
                env=environment,
            )
            async with Client(
                parameters, read_timeout_seconds=self.config.timeout_seconds, cache=None
            ) as client:
                yield client
            return

        headers = {"X-Request-ID": request_id, "X-Correlation-ID": request_id}
        inject(headers)
        if self.config.auth_token:
            token = self.config.auth_token.get_secret_value()
            headers[self.config.auth_header_name] = (
                f"Bearer {token}" if self.config.auth_type == "BEARER" else token
            )
        async with httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(self.config.timeout_seconds),
        ) as http_client:
            transport = streamable_http_client(
                str(self.config.endpoint), http_client=http_client
            )
            async with Client(
                transport, read_timeout_seconds=self.config.timeout_seconds, cache=None
            ) as client:
                yield client


class YahooMcpRawFact(StrictContract):
    metric: str = Field(min_length=1, max_length=100)
    value: Any
    unit: str | None = Field(default=None, max_length=30)
    period_end: str | None = Field(default=None, alias="periodEnd")
    period_type: str | None = Field(default=None, alias="periodType")
    reporting_basis: str = Field(default="UNKNOWN", alias="reportingBasis")
    as_of: datetime | None = Field(default=None, alias="asOf")
    published_at: datetime | None = Field(default=None, alias="publishedAt")
    confidence: float = Field(default=0.78, ge=0, le=1)
    raw_field_origin: str | None = Field(default=None, alias="rawFieldOrigin", max_length=200)

    @field_validator("value")
    @classmethod
    def finite_scalar(cls, value: Any) -> Any:
        if isinstance(value, (dict, list, tuple)):
            raise ValueError("Yahoo MCP fact values must be scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Yahoo MCP fact values must be finite")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("Yahoo MCP fact values must be finite")
        return value

    @field_validator("period_end")
    @classmethod
    def valid_period_end(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Yahoo MCP periodEnd must be an ISO date") from exc
        return value

    @field_validator("period_type")
    @classmethod
    def valid_period_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in {"ANNUAL", "QUARTERLY", "AS_AT"}:
            raise ValueError("Yahoo MCP periodType is unsupported")
        return normalized


class YahooMcpRawPrice(StrictContract):
    observed_at: datetime = Field(alias="observedAt")
    close: Decimal
    currency: str | None = None

    @field_validator("close")
    @classmethod
    def valid_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("Yahoo MCP prices must be positive and finite")
        return value


class YahooMcpRawArticle(StrictContract):
    headline: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2048)
    published_at: datetime = Field(alias="publishedAt")
    publisher: str = Field(default="Yahoo Finance", max_length=200)
    issuer_symbol: str | None = Field(default=None, alias="issuerSymbol")
    related_symbols: tuple[str, ...] = Field(default=(), alias="relatedSymbols")
    summary: str | None = Field(default=None, max_length=2000)
    event_type: str | None = Field(default=None, alias="eventType", max_length=100)

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("Yahoo MCP evidence URLs must use HTTP(S)")
        return value


class YahooMcpRawProfile(StrictContract):
    company_name: str | None = Field(default=None, alias="companyName", max_length=300)
    sector: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=200)


class YahooMcpRawOwnership(StrictContract):
    period_end: datetime = Field(alias="periodEnd")
    promoter_holding_percent: Decimal | None = Field(default=None, alias="promoterHoldingPercent")
    promoter_pledge_percent: Decimal | None = Field(default=None, alias="promoterPledgePercent")
    promoter_pledge_basis: str | None = Field(default=None, alias="promoterPledgeBasis")
    fii_fpi_percent: Decimal | None = Field(default=None, alias="fiiFpiPercent")
    dii_percent: Decimal | None = Field(default=None, alias="diiPercent")
    public_retail_percent: Decimal | None = Field(default=None, alias="publicRetailPercent")
    institutional_ownership_percent: Decimal | None = Field(
        default=None, alias="institutionalOwnershipPercent"
    )

    @field_validator(
        "promoter_holding_percent",
        "promoter_pledge_percent",
        "fii_fpi_percent",
        "dii_percent",
        "public_retail_percent",
        "institutional_ownership_percent",
    )
    @classmethod
    def valid_percentage(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0 or value > 100):
            raise ValueError("Yahoo MCP ownership percentages must be between 0 and 100")
        return value


class YahooMcpRawPayload(StrictContract):
    schema_version: str = Field(alias="schemaVersion")
    adapter_version: str | None = Field(default=None, alias="adapterVersion", max_length=100)
    source: str | None = Field(default=None, max_length=100)
    global_instrument_id: str = Field(alias="globalInstrumentId", min_length=1, max_length=100)
    symbol: str = Field(min_length=1, max_length=100)
    exchange: str | None = Field(default=None, max_length=100)
    currency: str | None = Field(default=None, max_length=20)
    as_of: datetime | None = Field(default=None, alias="asOf")
    retrieved_at: datetime | None = Field(default=None, alias="retrievedAt")
    source_url: str | None = Field(default=None, alias="sourceUrl", max_length=2048)
    price: Decimal | None = None
    facts: tuple[YahooMcpRawFact, ...] = ()
    prices: tuple[YahooMcpRawPrice, ...] = ()
    profile: YahooMcpRawProfile | None = None
    news: tuple[YahooMcpRawArticle, ...] = ()
    news_query_succeeded: bool = Field(default=False, alias="newsQuerySucceeded")
    events: tuple[YahooMcpRawArticle, ...] = ()
    ownership: YahooMcpRawOwnership | None = None

    @model_validator(mode="after")
    def valid_contract(self):
        if self.schema_version != TOOL_SCHEMA_VERSION:
            raise ValueError("Unsupported Yahoo MCP tool schema version")
        if self.price is not None and (not self.price.is_finite() or self.price <= 0):
            raise ValueError("Yahoo MCP latest price must be positive and finite")
        if self.source_url and not self.source_url.startswith(("https://", "http://")):
            raise ValueError("Yahoo MCP source URL must use HTTP(S)")
        return self


_STRUCTURED_FACTS = {
    "trailingEps",
    "forwardEps",
    "trailingPE",
    "forwardPE",
    "priceToBook",
    "evToEbitda",
    "marketCap",
    "freeCashFlow",
    "operatingCashFlow",
    "roe",
    "roa",
    "roce",
    "profitMargin",
    "operatingMargin",
    "revenueGrowth",
    "earningsGrowth",
    "totalCash",
    "totalDebt",
    "debtToEquity",
    "currentRatio",
    "sector",
    "industry",
    "publicAnalystTargetMeanPrice",
    "publicAnalystTargetLowPrice",
    "publicAnalystTargetMedianPrice",
    "publicAnalystTargetHighPrice",
    "publicAnalystCount",
    "publicAnalystRecommendationMean",
    "publicAnalystConsensus",
}
_FINANCIAL_FACTS = {
    "revenue",
    "total_revenue",
    "pat",
    "net_income",
    "net_profit",
    "operating_income",
    "operating_profit",
    "ebit",
    "ebitda",
    "pbt",
    "tax",
    "finance_cost",
    "interest_expense",
    "eps",
    "cash_and_equivalents",
    "total_cash",
    "debt_or_borrowings",
    "total_debt",
    "total_assets",
    "total_liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
    "receivables",
    "inventory",
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "free_cash_flow",
    "capex",
    "operating_margin",
    "profit_margin",
    "ebitda_margin",
    "roe",
    "roce",
}


class YahooFinanceMcpProvider:
    metadata: ExternalMcpProviderMetadata

    def __init__(
        self,
        config: YahooFinanceMcpConfig,
        capabilities: YahooFinanceCapabilityRegistry,
        *,
        client_factory: YahooMcpClientFactory | None = None,
        circuit_breaker: McpCircuitBreaker | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.capabilities = capabilities
        self.client_factory = client_factory or OfficialYahooMcpClientFactory(config)
        self.circuit_breaker = circuit_breaker
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._concurrency = asyncio.Semaphore(config.max_concurrency)
        supported = capabilities.supported
        self.metadata = ExternalMcpProviderMetadata(
            providerId=PROVIDER_ID,
            regions=SUPPORTED_REGIONS,
            supportedRequirements=tuple(sorted({item.requirement_id for item in supported})),
            supportedTools=tuple(sorted({str(item.tool) for item in supported})),
            riskClass=McpRiskClass.SAFE_READ,
            authType=config.auth_type,
            priority=1,
            sourceTier="APPROVED_EXTERNAL_TOOL",
            healthState=ExternalProviderHealthState.UNKNOWN,
            timeoutSeconds=config.timeout_seconds,
            timeoutBehavior="BOUNDED_RETRY_THEN_FALLBACK",
            adapterVersion=ADAPTER_VERSION,
        )

    def capability_for(
        self, *, region: str, requirement_id: str
    ) -> ExternalMcpProviderCapability | None:
        return self.capabilities.capability_for(region=region, requirement_id=requirement_id)

    async def supports(self, *, region: str, requirement_id: str, tool: str) -> bool:
        capability = self.capability_for(region=region, requirement_id=requirement_id)
        return bool(
            capability
            and capability.state is ExternalCapabilityState.SUPPORTED
            and capability.tool == tool
        )

    async def invoke(
        self, *, tool: str, arguments: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        allowed = {
            "globalInstrumentId",
            "region",
            "requirementId",
            "providerSymbol",
            "expectedExchange",
            "expectedCurrency",
        }
        if set(arguments) - allowed:
            raise McpGatewayError(McpErrorCode.INVALID_ARGUMENT)
        region = str(arguments.get("region") or "").upper()
        requirement = str(arguments.get("requirementId") or "").upper()
        symbol = str(arguments.get("providerSymbol") or "").strip()
        global_instrument_id = str(arguments.get("globalInstrumentId") or "").strip()
        capability = self.capability_for(region=region, requirement_id=requirement)
        if (
            not symbol
            or not global_instrument_id
            or capability is None
            or capability.state is not ExternalCapabilityState.SUPPORTED
            or capability.tool != tool
        ):
            raise McpGatewayError(McpErrorCode.EXTERNAL_CAPABILITY_UNSUPPORTED)

        outbound = {
            **capability.tool_arguments,
            "globalInstrumentId": global_instrument_id,
            "verifiedYahooSymbol": symbol,
            "region": region,
            "exchange": arguments.get("expectedExchange"),
            "currency": arguments.get("expectedCurrency"),
        }
        async with self._concurrency:
            payload = await self._call(tool, outbound, request_id)
        try:
            raw = YahooMcpRawPayload.model_validate(payload)
        except ValidationError as exc:
            raise McpGatewayError(McpErrorCode.EXTERNAL_SCHEMA_INVALID) from exc
        self._validate_identity(
            raw,
            global_instrument_id=global_instrument_id,
            symbol=symbol,
            expected_exchange=arguments.get("expectedExchange"),
            expected_currency=arguments.get("expectedCurrency"),
        )
        normalized = self._normalize(
            raw,
            capability=capability,
            global_instrument_id=global_instrument_id,
            region=region,
            requirement=requirement,
            tool=tool,
        )
        self._validate_completeness(normalized, capability)
        return normalized

    async def _call(
        self, tool: str, outbound: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        last_code = McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE
        for attempt in range(self.config.max_retries + 1):
            try:
                if self.circuit_breaker:
                    await self.circuit_breaker.before_call(dependency=PROVIDER_ID)
                async with asyncio.timeout(self.config.timeout_seconds):
                    async with self.client_factory.connect(request_id) as client:
                        listing = await client.list_tools()
                        names = {item.name for item in listing.tools}
                        if tool not in names:
                            raise McpGatewayError(
                                McpErrorCode.EXTERNAL_CAPABILITY_UNSUPPORTED
                            )
                        result = await client.call_tool(tool, outbound)
                if result.is_error:
                    code = _first_party_error_code(result)
                    if code in {
                        McpErrorCode.DOWNSTREAM_TIMEOUT,
                        McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE,
                        McpErrorCode.EXTERNAL_PROVIDER_RATE_LIMITED,
                    }:
                        raise _RetryableProviderFailure(code)
                    raise McpGatewayError(code)
                if not isinstance(result.structured_content, dict):
                    raise McpGatewayError(McpErrorCode.EXTERNAL_SCHEMA_INVALID)
                if self.circuit_breaker:
                    await self.circuit_breaker.record_success(dependency=PROVIDER_ID)
                return result.structured_content
            except McpGatewayError:
                if self.circuit_breaker:
                    await self.circuit_breaker.record_failure(dependency=PROVIDER_ID)
                raise
            except TimeoutError:
                last_code = McpErrorCode.DOWNSTREAM_TIMEOUT
            except _RetryableProviderFailure as exc:
                last_code = exc.code
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                response = getattr(exc, "response", None)
                last_code = (
                    McpErrorCode.EXTERNAL_PROVIDER_RATE_LIMITED
                    if getattr(response, "status_code", None) == 429
                    else McpErrorCode.EXTERNAL_PROVIDER_UNAVAILABLE
                )
            if self.circuit_breaker:
                await self.circuit_breaker.record_failure(dependency=PROVIDER_ID)
            if attempt < self.config.max_retries:
                await asyncio.sleep(self.config.retry_backoff_seconds * (2**attempt))
        raise McpGatewayError(last_code)

    async def health(self) -> dict[str, Any]:
        required = {str(item.tool) for item in self.capabilities.supported}
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self.client_factory.connect("yahoo-mcp-health") as client:
                    listing = await client.list_tools()
            discovered = {item.name for item in listing.tools}
        except Exception:
            return {"status": ExternalProviderHealthState.DOWN.value, "providerId": PROVIDER_ID}
        missing = sorted(required - discovered)
        state = (
            ExternalProviderHealthState.DEGRADED
            if missing
            else ExternalProviderHealthState.UP
        )
        return {
            "status": state.value,
            "providerId": PROVIDER_ID,
            "configuredCapabilityCount": len(required),
            "missingConfiguredToolCount": len(missing),
        }

    def _validate_identity(
        self,
        raw: YahooMcpRawPayload,
        *,
        global_instrument_id: str,
        symbol: str,
        expected_exchange: Any,
        expected_currency: Any,
    ) -> None:
        if raw.global_instrument_id != global_instrument_id:
            raise McpGatewayError(McpErrorCode.EXTERNAL_IDENTITY_CONFLICT)
        if raw.symbol.strip().upper() != symbol.upper():
            raise McpGatewayError(McpErrorCode.EXTERNAL_IDENTITY_CONFLICT)
        expected_exchange_text = str(expected_exchange or "").strip()
        if expected_exchange_text:
            if not raw.exchange or _exchange_family(raw.exchange) != _exchange_family(
                expected_exchange_text
            ):
                raise McpGatewayError(McpErrorCode.EXTERNAL_IDENTITY_CONFLICT)
        expected_currency_text = str(expected_currency or "").strip().upper()
        if expected_currency_text and (
            not raw.currency or raw.currency.strip().upper() != expected_currency_text
        ):
            raise McpGatewayError(McpErrorCode.EXTERNAL_IDENTITY_CONFLICT)

    def _normalize(
        self,
        raw: YahooMcpRawPayload,
        *,
        capability: ExternalMcpProviderCapability,
        global_instrument_id: str,
        region: str,
        requirement: str,
        tool: str,
    ) -> dict[str, Any]:
        now = _aware(self.clock())
        observed = _aware(raw.as_of) if raw.as_of else None
        if capability.max_age_seconds:
            if observed is None:
                raise McpGatewayError(McpErrorCode.EXTERNAL_RESULT_INCOMPLETE)
            if observed > now + timedelta(minutes=5) or now - observed > timedelta(
                seconds=capability.max_age_seconds
            ):
                raise McpGatewayError(McpErrorCode.EXTERNAL_RESULT_STALE)

        source_url = raw.source_url or (
            f"https://finance.yahoo.com/quote/{quote(raw.symbol, safe='')}"
        )
        structured: list[dict[str, Any]] = []
        financial: list[dict[str, Any]] = []
        if raw.price is not None:
            structured.append(
                _wire_fact(
                    "latestPrice", raw.price, raw.currency, observed, None, source_url, None
                )
            )
        for fact in raw.facts:
            period_type = str(fact.period_type or "").upper()
            if period_type in {"ANNUAL", "QUARTERLY", "AS_AT"}:
                if fact.metric not in _FINANCIAL_FACTS or not fact.period_end:
                    raise McpGatewayError(McpErrorCode.EXTERNAL_SCHEMA_INVALID)
                value = _decimal(fact.value)
                if value is None:
                    raise McpGatewayError(McpErrorCode.EXTERNAL_SCHEMA_INVALID)
                financial.append(
                    {
                        **_wire_fact(
                            fact.metric,
                            value,
                            fact.unit,
                            _aware(fact.as_of) if fact.as_of else None,
                            _aware(fact.published_at) if fact.published_at else None,
                            source_url,
                            fact.raw_field_origin,
                            confidence=fact.confidence,
                        ),
                        "periodEnd": fact.period_end,
                        "periodType": period_type,
                        "reportingBasis": fact.reporting_basis.strip().upper() or "UNKNOWN",
                    }
                )
            else:
                if fact.metric not in _STRUCTURED_FACTS:
                    raise McpGatewayError(McpErrorCode.EXTERNAL_SCHEMA_INVALID)
                structured.append(
                    _wire_fact(
                        fact.metric,
                        fact.value,
                        fact.unit,
                        _aware(fact.as_of) if fact.as_of else observed,
                        _aware(fact.published_at) if fact.published_at else None,
                        source_url,
                        fact.raw_field_origin,
                        confidence=fact.confidence,
                    )
                )
        if raw.profile:
            for name, value in (("sector", raw.profile.sector), ("industry", raw.profile.industry)):
                if value:
                    structured.append(
                        _wire_fact(name, value, None, observed, None, source_url, name)
                    )

        prices = [
            {
                "observedAt": _aware(item.observed_at).isoformat(),
                "price": str(item.close),
                "currency": (item.currency or raw.currency),
            }
            for item in sorted(raw.prices, key=lambda item: item.observed_at)
        ]
        if raw.price is not None and observed is not None:
            prices.append(
                {
                    "observedAt": observed.isoformat(),
                    "price": str(raw.price),
                    "currency": raw.currency,
                }
            )
        prices = list(
            {
                (item["observedAt"], item["price"]): item for item in prices
            }.values()
        )
        news = _normalize_articles(raw.news, raw.symbol, now, current_news=True)
        events = _normalize_articles(raw.events, raw.symbol, now, current_news=False)
        ownership = _normalize_ownership(raw.ownership)
        return {
            "adapterVersion": ADAPTER_VERSION,
            "providerId": PROVIDER_ID,
            "sourceTier": "APPROVED_EXTERNAL_TOOL",
            "sourceTool": tool,
            "region": region,
            "requirementId": requirement,
            "globalInstrumentId": global_instrument_id,
            "symbol": raw.symbol,
            "exchange": raw.exchange,
            "currency": raw.currency,
            "retrievedAt": now.isoformat(),
            "observedAt": observed.isoformat() if observed else None,
            "sourceUrl": source_url,
            "confidence": 0.80,
            "freshness": "FRESH",
            "structuredFacts": structured,
            "financialFacts": financial,
            "acquisitionOutcome": "SUCCESS_EMPTY" if requirement == "CURRENT_NEWS" and raw.news_query_succeeded and not news else "SUCCESS",
            "marketObservations": prices,
            "companyProfile": (
                raw.profile.model_dump(mode="json", by_alias=True, exclude_none=True)
                if raw.profile
                else None
            ),
            "news": news,
            "events": events,
            "shareholding": ownership,
        }

    @staticmethod
    def _validate_completeness(
        result: dict[str, Any], capability: ExternalMcpProviderCapability
    ) -> None:
        requirement = capability.requirement_id
        structured = {item["metric"] for item in result["structuredFacts"]}
        financial = result["financialFacts"]
        metrics = {item["metric"] for item in financial}
        available = structured | metrics
        missing_configured = set(capability.required_fields) - available
        if missing_configured:
            raise McpGatewayError(McpErrorCode.EXTERNAL_RESULT_INCOMPLETE)
        complete = False
        if requirement == "LATEST_PRICE":
            complete = "latestPrice" in structured
        elif requirement == "HISTORICAL_PRICE_SERIES":
            complete = len(result["marketObservations"]) >= capability.minimum_items
        elif requirement == "VALUATION_INPUTS":
            complete = "latestPrice" in structured and bool(
                {"trailingEps", "forwardEps", "eps"} & available
            )
        elif requirement == "BUSINESS_QUALITY_FACTS":
            complete = (
                _period_count(financial, {"revenue", "total_revenue"}, "ANNUAL") >= 2
                and _period_count(
                    financial, {"pat", "net_income", "net_profit"}, "ANNUAL"
                )
                >= 2
                and bool(
                    {
                        "operating_cash_flow",
                        "free_cash_flow",
                        "roe",
                        "profit_margin",
                        "operating_margin",
                        "ebitda",
                    }
                    & metrics
                )
            )
        elif requirement == "GROWTH_FACTS":
            complete = (
                _period_count(financial, {"revenue", "total_revenue"}) >= 2
                and _period_count(financial, {"pat", "net_income", "net_profit", "eps"}) >= 2
            )
        elif requirement == "BALANCE_SHEET_FACTS":
            complete = bool({"debt_or_borrowings", "total_debt"} & metrics) and "equity" in metrics
        elif requirement == "QUARTERLY_FINANCIALS":
            complete = (
                _period_count(financial, {"revenue", "total_revenue"}, "QUARTERLY") >= 2
                and _period_count(financial, {"pat", "net_income", "net_profit"}, "QUARTERLY") >= 2
            )
        elif requirement == "CURRENT_NEWS":
            complete = len(result["news"]) >= capability.minimum_items or result.get("acquisitionOutcome") == "SUCCESS_EMPTY"
        elif requirement == "ORDER_BOOK_CAPEX_GUIDANCE":
            complete = len(result["events"]) >= capability.minimum_items and all(
                item.get("eventType") in _READINESS_CATALYST_EVENT_TYPES
                for item in result["events"]
            )
        elif requirement == "SHAREHOLDING":
            shareholding = result.get("shareholding") or {}
            complete = all(
                shareholding.get(key) is not None
                for key in (
                    "promoterHoldingPercent",
                    "promoterPledgePercent",
                    "promoterPledgeBasis",
                    "fiiFpiPercent",
                    "diiPercent",
                )
            ) and _is_quarter_end(shareholding.get("periodEnd"))
        elif requirement == "SECTOR_MACRO":
            complete = "sector" in structured
        elif requirement == "COMPANY_PROFILE":
            complete = bool((result.get("companyProfile") or {}).get("companyName"))
        elif requirement == "ANALYST_DATA":
            complete = bool(_STRUCTURED_FACTS.intersection(structured) & {
                "publicAnalystTargetMeanPrice",
                "publicAnalystTargetLowPrice",
                "publicAnalystTargetMedianPrice",
                "publicAnalystTargetHighPrice",
                "publicAnalystCount",
                "publicAnalystRecommendationMean",
                "publicAnalystConsensus",
            })
        if not complete:
            raise McpGatewayError(McpErrorCode.EXTERNAL_RESULT_INCOMPLETE)


def _normalize_articles(
    values: tuple[YahooMcpRawArticle, ...],
    expected_symbol: str,
    now: datetime,
    *,
    current_news: bool,
) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in values:
        published = _aware(item.published_at)
        related = {value.strip().upper() for value in item.related_symbols}
        issuer = str(item.issuer_symbol or "").strip().upper()
        if expected_symbol.upper() not in ({issuer} | related):
            continue
        if published > now + timedelta(minutes=5):
            continue
        if current_news and now - published > timedelta(days=30):
            continue
        key = (item.url.casefold(), item.headline.strip().casefold(), published.date().isoformat())
        result[key] = {
            "headline": item.headline.strip(),
            "url": item.url,
            "publishedAt": published.isoformat(),
            "publisher": item.publisher,
            "issuerSymbol": expected_symbol,
            "summary": item.summary,
            "eventType": item.event_type.strip().upper() if item.event_type else None,
        }
    return sorted(result.values(), key=lambda item: item["publishedAt"], reverse=True)


def _normalize_ownership(value: YahooMcpRawOwnership | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


def _wire_fact(
    metric: str,
    value: Any,
    unit: str | None,
    as_of: datetime | None,
    published_at: datetime | None,
    source_url: str,
    raw_field_origin: str | None,
    *,
    confidence: float = 0.80,
) -> dict[str, Any]:
    if isinstance(value, Decimal):
        value = str(value)
    return {
        "metric": metric,
        "value": value,
        "unit": unit,
        "asOf": as_of.isoformat() if as_of else None,
        "publishedAt": published_at.isoformat() if published_at else None,
        "sourceUrl": source_url,
        "confidence": confidence,
        "rawFieldOrigin": raw_field_origin,
    }


def _period_count(
    facts: list[dict[str, Any]], metrics: set[str], period_type: str | None = None
) -> int:
    return len(
        {
            item["periodEnd"]
            for item in facts
            if item["metric"] in metrics
            and (period_type is None or item["periodType"] == period_type)
        }
    )


def _is_quarter_end(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return False
    return (parsed.month, parsed.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}


def _exchange_family(value: str) -> str:
    normalized = "".join(character for character in value.upper() if character.isalnum())
    aliases = {
        "XNSE": "NSE",
        "NSI": "NSE",
        "NASDAQ": "NASDAQ",
        "XNAS": "NASDAQ",
        "NMS": "NASDAQ",
        "NGM": "NASDAQ",
        "NCM": "NASDAQ",
        "XNYS": "NYSE",
        "NYQ": "NYSE",
        "XLON": "LSE",
    }
    return aliases.get(normalized, normalized)


def _first_party_error_code(result: Any) -> McpErrorCode:
    """Map only the first-party server's allowlisted safe codes."""
    for item in getattr(result, "content", ()):
        message = str(getattr(item, "text", ""))
        for safe_code, gateway_code in _FIRST_PARTY_ERRORS.items():
            if safe_code in message:
                return gateway_code
    return McpErrorCode.EXTERNAL_SCHEMA_INVALID


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _reject_sensitive_tool_arguments(value: Any) -> None:
    sensitive = (
        "password",
        "token",
        "secret",
        "authorization",
        "cookie",
        "credential",
        "api_key",
        "apikey",
    )
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if any(part in normalized for part in sensitive):
                raise ValueError("Yahoo MCP static tool arguments cannot contain credentials")
            _reject_sensitive_tool_arguments(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_tool_arguments(item)
