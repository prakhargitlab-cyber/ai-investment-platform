"""Official MCP protocol adapter over the application invocation gateway."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.application_client import ApplicationResearchReader, HttpApplicationResearchReader
from app.audit import McpAuditSink, StructuredMcpAuditLogger
from app.contracts import (
    McpAuthContext,
    McpErrorCode,
    McpGatewayError,
    SAFE_ERROR_MESSAGES,
    StrictContract,
    normalize_request_id,
)
from app.external import (
    ExternalMcpGateway,
    McpServerRegistry,
    ProviderFallbackAuthorization,
)
from app.invocation import McpInvocationService
from app.policy import McpToolPolicy
from app.protocol import McpProtocolGuard
from app.registry import McpToolRegistry
from app.settings import McpGatewaySettings
from app.tools import build_internal_tool_registry
from app.yahoo_finance_mcp import (
    YahooFinanceCapabilityRegistry,
    YahooFinanceMcpConfig,
    YahooFinanceMcpProvider,
)


class ExternalResearchAcquisitionRequest(StrictContract):
    provider_id: str = Field(alias="providerId")
    region: str
    requirement_id: str = Field(alias="requirementId")
    global_instrument_id: UUID = Field(alias="globalInstrumentId")
    provider_symbol: str = Field(alias="providerSymbol", min_length=1, max_length=100)
    expected_exchange: str | None = Field(default=None, alias="expectedExchange", max_length=100)
    expected_currency: str | None = Field(default=None, alias="expectedCurrency", max_length=20)
    authorization: ProviderFallbackAuthorization


@dataclass
class McpGatewayContainer:
    settings: McpGatewaySettings
    auth: McpAuthContext
    reader: ApplicationResearchReader
    registry: McpToolRegistry
    audit: McpAuditSink
    invocation: McpInvocationService
    external_registry: McpServerRegistry
    external_gateway: ExternalMcpGateway


def create_container(
    settings: McpGatewaySettings,
    *,
    reader: ApplicationResearchReader | None = None,
    audit: McpAuditSink | None = None,
) -> McpGatewayContainer:
    application_reader = reader or HttpApplicationResearchReader(settings)
    registry = build_internal_tool_registry(application_reader)
    audit_sink = audit or StructuredMcpAuditLogger(environment=settings.environment)
    external_registry = McpServerRegistry()
    if settings.yahoo_finance_mcp_enabled:
        capabilities = YahooFinanceCapabilityRegistry.from_json(
            settings.yahoo_finance_mcp_capabilities_json
        )
        external_registry.register(
            YahooFinanceMcpProvider(
                YahooFinanceMcpConfig(
                    transport=settings.yahoo_finance_mcp_transport,
                    endpoint=settings.yahoo_finance_mcp_endpoint,
                    stdio_command=settings.yahoo_finance_mcp_stdio_command,
                    stdio_args=settings.yahoo_finance_mcp_stdio_args,
                    auth_type=settings.yahoo_finance_mcp_auth_type,
                    auth_header_name=settings.yahoo_finance_mcp_auth_header_name,
                    auth_token=settings.yahoo_finance_mcp_auth_token,
                    stdio_token_env_name=settings.yahoo_finance_mcp_stdio_token_env_name,
                    timeout_seconds=settings.yahoo_finance_mcp_timeout_seconds,
                    max_retries=settings.yahoo_finance_mcp_max_retries,
                    retry_backoff_seconds=settings.yahoo_finance_mcp_retry_backoff_seconds,
                    max_concurrency=settings.yahoo_finance_mcp_max_concurrency,
                ),
                capabilities,
            )
        )
    return McpGatewayContainer(
        settings=settings,
        auth=settings.auth_context(),
        reader=application_reader,
        registry=registry,
        audit=audit_sink,
        invocation=McpInvocationService(
            registry,
            McpToolPolicy(),
            audit_sink,
            timeout_seconds=settings.invocation_timeout_seconds,
        ),
        external_registry=external_registry,
        external_gateway=ExternalMcpGateway(
            external_registry,
            enabled=settings.external_providers_enabled,
            timeout_seconds=settings.invocation_timeout_seconds,
            audit=audit_sink,
            auth=settings.auth_context(),
        ),
    )


def create_mcp_server(container: McpGatewayContainer) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield container
        finally:
            await container.reader.close()

    server = MCPServer(
        "ai-investment-internal-mcp",
        title="AI Investment Internal MCP",
        description="Read-only internal application intelligence tools.",
        instructions=(
            "Use canonical globalInstrumentId values only. Tools read persisted application data "
            "and never place trades, mutate portfolios, or trigger provider acquisition."
        ),
        version="0.2.0",
        lifespan=lifespan,
        warn_on_duplicate_tools=True,
        middleware=[McpProtocolGuard(container.registry, container.audit, container.auth)],
    )
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_research_readiness(globalInstrumentId: str, ctx: Context) -> dict[str, Any]:
        """Read persisted Research Readiness for a canonical instrument; performs no acquisition."""
        return await _invoke(
            container,
            ctx,
            "get_research_readiness",
            {"globalInstrumentId": globalInstrumentId},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_company_analysis(
        globalInstrumentId: str, ctx: Context, allowPartial: bool = False
    ) -> dict[str, Any]:
        """Return the existing STOCK_RULE_ENGINE_V1 result over persisted application data."""
        return await _invoke(
            container,
            ctx,
            "get_company_analysis",
            {"globalInstrumentId": globalInstrumentId, "allowPartial": allowPartial},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_financial_facts(globalInstrumentId: str, ctx: Context) -> dict[str, Any]:
        """Read persisted financial statement facts for a canonical instrument."""
        return await _invoke(
            container,
            ctx,
            "get_financial_facts",
            {"globalInstrumentId": globalInstrumentId},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_quarterly_results(globalInstrumentId: str, ctx: Context) -> dict[str, Any]:
        """Read persisted quarterly result facts for a canonical instrument."""
        return await _invoke(
            container,
            ctx,
            "get_quarterly_results",
            {"globalInstrumentId": globalInstrumentId},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_shareholding(globalInstrumentId: str, ctx: Context) -> dict[str, Any]:
        """Read persisted shareholding snapshots for a canonical instrument."""
        return await _invoke(
            container,
            ctx,
            "get_shareholding",
            {"globalInstrumentId": globalInstrumentId},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_recent_news(
        globalInstrumentId: str, ctx: Context, days: int = 30
    ) -> dict[str, Any]:
        """Read persisted current news; days must be between 1 and 30 inclusive."""
        return await _invoke(
            container,
            ctx,
            "get_recent_news",
            {"globalInstrumentId": globalInstrumentId, "days": days},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_sector_performance(
        region: str, sector: str, period: str, ctx: Context, limit: int = 5
    ) -> dict[str, Any]:
        """Read durable Sector Performance for USA, EUROPE, or INDIA."""
        return await _invoke(
            container,
            ctx,
            "get_sector_performance",
            {"region": region, "sector": sector, "period": period, "limit": limit},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def search_research_evidence(
        globalInstrumentId: str, query: str, ctx: Context, limit: int = 10
    ) -> dict[str, Any]:
        """Search persisted research evidence metadata; never performs external search."""
        return await _invoke(
            container,
            ctx,
            "search_research_evidence",
            {"globalInstrumentId": globalInstrumentId, "query": query, "limit": limit},
        )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_watchlist(watchlistId: str, ctx: Context) -> dict[str, Any]:
        """Read the authenticated user's watchlist research projection."""
        return await _invoke(container, ctx, "get_watchlist", {"watchlistId": watchlistId})

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": container.settings.service_name,
                "registeredTools": len(container.registry.tools),
            }
        )

    @server.custom_route("/health/ready", methods=["GET"], include_in_schema=False)
    async def ready(_request: Request) -> JSONResponse:
        # External providers are intentionally excluded from readiness in 5A.
        return JSONResponse(
            {
                "status": "ready",
                "service": container.settings.service_name,
                "externalProviderDependency": False,
            }
        )

    @server.custom_route(
        "/internal/v1/external-research/acquire", methods=["POST"], include_in_schema=False
    )
    async def acquire_external_research(request: Request) -> JSONResponse:
        request_id = normalize_request_id(
            request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID")
        )
        caller = str(request.headers.get("X-AIP-Service-Identity") or "").strip()
        if caller not in container.settings.external_caller_identities:
            return _external_error(McpErrorCode.UNAUTHORIZED, request_id, 401)
        try:
            command = ExternalResearchAcquisitionRequest.model_validate(await request.json())
        except (ValueError, ValidationError):
            return _external_error(McpErrorCode.INVALID_ARGUMENT, request_id, 400)
        try:
            data = await container.external_gateway.invoke_requirement(
                provider_id=command.provider_id,
                region=command.region,
                requirement_id=command.requirement_id,
                global_instrument_id=command.global_instrument_id,
                arguments={
                    "providerSymbol": command.provider_symbol,
                    "expectedExchange": command.expected_exchange,
                    "expectedCurrency": command.expected_currency,
                },
                request_id=request_id,
                authorization=command.authorization,
            )
        except McpGatewayError as exc:
            status_code = 403 if exc.code in {
                McpErrorCode.FORBIDDEN,
                McpErrorCode.MCP_TOOL_DENIED,
            } else 424
            return _external_error(exc.code, request_id, status_code)
        return JSONResponse({"ok": True, "requestId": request_id, "data": data})

    return server


def _external_error(code: McpErrorCode, request_id: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "requestId": request_id,
            "error": {"code": code.value, "message": SAFE_ERROR_MESSAGES[code]},
        },
        status_code=status_code,
    )


async def _invoke(
    container: McpGatewayContainer,
    context: Context,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return await container.invocation.invoke(
        tool,
        arguments,
        container.auth,
        request_id=context.request_id,
    )
