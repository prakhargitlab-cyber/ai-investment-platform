from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from mcp.server.context import HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from opentelemetry import trace
from opentelemetry.propagate import extract
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from yahoo_mcp_server.acquisition import YahooAcquisitionService, YahooMcpServiceError
from yahoo_mcp_server.audit import YahooMcpAudit
from yahoo_mcp_server.contracts import (
    HistoryInput,
    IdentityInput,
    NewsInput,
    RawArticle,
    RawFact,
    RawPrice,
    RawProfile,
    Region,
    ToolPayload,
)
from yahoo_mcp_server.settings import YahooMcpSettings


REGISTERED_TOOLS = (
    "get_quote",
    "get_price_history",
    "get_company_profile",
    "get_financials",
    "get_quarterly_financials",
    "get_news",
    "get_sector_industry",
    "get_analyst_data",
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TRACER = trace.get_tracer("ai-investment.yahoo-finance-mcp")
_STRUCTURED_FINANCIAL_METRICS = frozenset(
    {
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
    }
)
_ANALYST_METRICS = frozenset(
    {
        "publicAnalystTargetLowPrice",
        "publicAnalystTargetMedianPrice",
        "publicAnalystTargetMeanPrice",
        "publicAnalystTargetHighPrice",
        "publicAnalystCount",
        "publicAnalystRecommendationMean",
        "publicAnalystConsensus",
    }
)
_BASE_ARGUMENTS = {
    "globalInstrumentId",
    "verifiedYahooSymbol",
    "region",
    "exchange",
    "currency",
}
_TOOL_ARGUMENTS = {
    **{name: _BASE_ARGUMENTS for name in REGISTERED_TOOLS},
    "get_price_history": _BASE_ARGUMENTS | {"lookbackDays"},
    "get_news": _BASE_ARGUMENTS | {"days"},
}


class StrictToolArguments:
    """Reject unknown tools and undeclared fields before SDK argument binding."""

    async def __call__(self, context: ServerRequestContext, call_next) -> HandlerResult:
        if context.method != "tools/call":
            return await call_next(context)
        params = context.params or {}
        tool = str(params.get("name") or "")
        allowed = _TOOL_ARGUMENTS.get(tool)
        arguments = params.get("arguments") or {}
        if allowed is None or not isinstance(arguments, dict) or set(arguments) - allowed:
            return CallToolResult(
                content=[TextContent(type="text", text="YAHOO_MCP_INVALID_ARGUMENT")],
                isError=True,
            )
        return await call_next(context)


def create_yahoo_mcp_server(
    settings: YahooMcpSettings | None = None,
    *,
    acquisition: YahooAcquisitionService | None = None,
    audit: YahooMcpAudit | None = None,
) -> MCPServer:
    config = settings or YahooMcpSettings()
    source = acquisition or YahooAcquisitionService(config)
    audit_sink = audit or YahooMcpAudit(
        service=config.service_name, environment=config.environment
    )
    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield source
        finally:
            await source.close()

    server = MCPServer(
        "ai-investment-yahoo-finance-mcp",
        title="AI Investment First-party Yahoo Finance MCP",
        description="Controlled read-only Yahoo Finance acquisition using verified mappings.",
        instructions=(
            "Accept only canonical globalInstrumentId values and application-verified Yahoo symbols. "
            "This server never discovers or persists identity and exposes no write capability."
        ),
        version="0.1.0",
        lifespan=lifespan,
        warn_on_duplicate_tools=True,
        middleware=[StrictToolArguments()],
    )
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )

    @server.tool(annotations=annotations, structured_output=True)
    async def get_quote(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Return a current Yahoo quote for an application-verified provider mapping."""
        identity = _identity(globalInstrumentId, verifiedYahooSymbol, region, exchange, currency)

        async def work() -> dict[str, Any]:
            snapshot = await source.snapshot(identity)
            fact = snapshot.facts.get("latestPrice")
            if fact is None:
                raise YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
            return _payload(identity, snapshot, price=fact.value).wire()

        return await _execute("get_quote", identity, ctx, audit_sink, work)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_price_history(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
        lookbackDays: int = 400,
    ) -> dict[str, Any]:
        """Return bounded historical close observations for a verified Yahoo symbol."""
        command = HistoryInput(
            globalInstrumentId=globalInstrumentId,
            verifiedYahooSymbol=verifiedYahooSymbol,
            region=region,
            exchange=exchange,
            currency=currency,
            lookbackDays=lookbackDays,
        )

        async def work() -> dict[str, Any]:
            observations = await source.closes(command, lookback_days=command.lookback_days)
            return ToolPayload(
                globalInstrumentId=command.global_instrument_id,
                symbol=command.verified_yahoo_symbol,
                exchange=command.exchange,
                currency=command.currency,
                asOf=observations[-1].observed_at,
                retrievedAt=datetime.now(timezone.utc),
                sourceUrl=observations[-1].source_url,
                prices=tuple(
                    RawPrice(
                        observedAt=item.observed_at,
                        close=item.price,
                        currency=item.currency or command.currency,
                    )
                    for item in observations
                ),
            ).wire()

        return await _execute("get_price_history", command, ctx, audit_sink, work)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_company_profile(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Return the normalized Yahoo company profile without identity discovery."""
        identity = _identity(globalInstrumentId, verifiedYahooSymbol, region, exchange, currency)

        async def work() -> dict[str, Any]:
            snapshot = await source.snapshot(identity)
            profile = _profile(snapshot)
            if not any((profile.company_name, profile.sector, profile.industry)):
                raise YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
            return _payload(identity, snapshot, profile=profile).wire()

        return await _execute("get_company_profile", identity, ctx, audit_sink, work)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_financials(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Return normalized annual statements and supported valuation inputs."""
        identity = _identity(globalInstrumentId, verifiedYahooSymbol, region, exchange, currency)

        async def work() -> dict[str, Any]:
            snapshot = await source.snapshot(identity)
            facts = _statement_facts(snapshot, "ANNUAL") + _selected_facts(
                snapshot, _STRUCTURED_FINANCIAL_METRICS
            )
            facts = facts[: config.max_response_items]
            if not facts:
                raise YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
            latest = snapshot.facts.get("latestPrice")
            return _payload(
                identity,
                snapshot,
                price=latest.value if latest else None,
                facts=tuple(facts),
            ).wire()

        return await _execute("get_financials", identity, ctx, audit_sink, work)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_quarterly_financials(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Return normalized quarterly statement facts with explicit period identity."""
        identity = _identity(globalInstrumentId, verifiedYahooSymbol, region, exchange, currency)

        async def work() -> dict[str, Any]:
            snapshot = await source.snapshot(identity)
            facts = _statement_facts(snapshot, "QUARTERLY")
            facts = facts[: config.max_response_items]
            if not facts:
                raise YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
            return _payload(identity, snapshot, facts=tuple(facts)).wire()

        return await _execute("get_quarterly_financials", identity, ctx, audit_sink, work)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_news(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        """Return issuer-scoped Yahoo news published during the last 30 days at most."""
        command = NewsInput(
            globalInstrumentId=globalInstrumentId,
            verifiedYahooSymbol=verifiedYahooSymbol,
            region=region,
            exchange=exchange,
            currency=currency,
            days=days,
        )

        async def work() -> dict[str, Any]:
            snapshot = await source.snapshot(command)
            cutoff = datetime.now(timezone.utc) - timedelta(days=command.days)
            articles = []
            seen = set()
            for item in snapshot.news:
                published = item.get("publishedAt")
                url = str(item.get("url") or "")
                if not isinstance(published, datetime) or published < cutoff or url in seen:
                    continue
                seen.add(url)
                articles.append(
                    RawArticle(
                        headline=item.get("headline"),
                        url=url,
                        publishedAt=published,
                        publisher=item.get("publisher") or "Yahoo Finance",
                        issuerSymbol=command.verified_yahoo_symbol,
                        summary=item.get("summary"),
                    )
                )
                if len(articles) >= config.max_response_items:
                    break
            payload = _payload(command, snapshot, news=tuple(articles)).wire()
            payload["newsQuerySucceeded"] = True
            return payload

        return await _execute("get_news", command, ctx, audit_sink, work)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_sector_industry(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Return Yahoo sector and industry labels as supporting evidence."""
        identity = _identity(globalInstrumentId, verifiedYahooSymbol, region, exchange, currency)

        async def work() -> dict[str, Any]:
            snapshot = await source.snapshot(identity)
            profile = _profile(snapshot)
            if not profile.sector and not profile.industry:
                raise YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
            return _payload(identity, snapshot, profile=profile).wire()

        return await _execute("get_sector_industry", identity, ctx, audit_sink, work)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_analyst_data(
        globalInstrumentId: UUID,
        verifiedYahooSymbol: str,
        region: Region,
        ctx: Context,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Return only Yahoo analyst fields that are present; never derive a recommendation."""
        identity = _identity(globalInstrumentId, verifiedYahooSymbol, region, exchange, currency)

        async def work() -> dict[str, Any]:
            snapshot = await source.snapshot(identity)
            facts = _selected_facts(snapshot, _ANALYST_METRICS)
            if not facts:
                raise YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
            return _payload(identity, snapshot, facts=tuple(facts)).wire()

        return await _execute("get_analyst_data", identity, ctx, audit_sink, work)

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "service": config.service_name, "registeredTools": len(REGISTERED_TOOLS)}
        )

    @server.custom_route("/health/ready", methods=["GET"], include_in_schema=False)
    async def ready(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ready", "service": config.service_name, "upstreamRequired": False}
        )

    # MCP 2.2.0 derives function schemas but leaves object extras unspecified.
    # The pinned SDK exposes registered Tool metadata through ToolManager; mark
    # the advertised schema as strict to match the protocol-edge guard above.
    for registered in server._tool_manager.list_tools():
        registered.parameters["additionalProperties"] = False

    return server


def _identity(
    global_instrument_id: UUID,
    symbol: str,
    region: Region,
    exchange: str | None,
    currency: str | None,
) -> IdentityInput:
    return IdentityInput(
        globalInstrumentId=global_instrument_id,
        verifiedYahooSymbol=symbol,
        region=region,
        exchange=exchange,
        currency=currency,
    )


async def _execute(
    tool: str,
    identity: IdentityInput,
    context: Context,
    audit: YahooMcpAudit,
    work: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    started = monotonic()
    request_id = _request_id(context)
    common = {
        "request_id": request_id,
        "global_instrument_id": str(identity.global_instrument_id),
        "symbol": identity.verified_yahoo_symbol,
        "tool": tool,
        "started": started,
    }
    parent_context = extract(dict(context.headers or {}))
    with _TRACER.start_as_current_span(f"yahoo_mcp.{tool}", context=parent_context):
        audit.emit("YAHOO_MCP_REQUEST", **common)
        try:
            result = await work()
        except YahooMcpServiceError as exc:
            event = (
                "YAHOO_MCP_IDENTITY_MISMATCH"
                if exc.code == "YAHOO_MCP_IDENTITY_MISMATCH"
                else "YAHOO_MCP_UNSUPPORTED"
                if exc.code == "YAHOO_MCP_UNSUPPORTED"
                else "YAHOO_MCP_FAILED"
            )
            audit.emit(event, **common, error_code=exc.code)
            raise ToolError(exc.code) from None
        except (ValidationError, ValueError):
            audit.emit("YAHOO_MCP_FAILED", **common, error_code="YAHOO_MCP_INVALID_RESPONSE")
            raise ToolError("YAHOO_MCP_INVALID_RESPONSE") from None
        except Exception:
            audit.emit(
                "YAHOO_MCP_FAILED",
                **common,
                error_code="YAHOO_MCP_UPSTREAM_UNAVAILABLE",
            )
            raise ToolError("YAHOO_MCP_UPSTREAM_UNAVAILABLE") from None
        audit.emit("YAHOO_MCP_SUCCESS", **common)
        return result


def _request_id(context: Context) -> str:
    headers = context.headers or {}
    candidate = str(
        headers.get("x-request-id")
        or headers.get("X-Request-ID")
        or headers.get("x-correlation-id")
        or os.environ.get("AIP_REQUEST_ID")
        or context.request_id
        or ""
    )
    return candidate if _REQUEST_ID.fullmatch(candidate) else str(uuid4())


def _payload(identity: IdentityInput, snapshot, **updates: Any) -> ToolPayload:
    return ToolPayload(
        globalInstrumentId=identity.global_instrument_id,
        symbol=identity.verified_yahoo_symbol,
        exchange=snapshot.resolution.exchange or identity.exchange,
        currency=snapshot.resolution.currency or identity.currency,
        asOf=snapshot.market_as_of or snapshot.retrieved_at,
        retrievedAt=snapshot.retrieved_at,
        sourceUrl=snapshot.source_url,
        **updates,
    )


def _profile(snapshot) -> RawProfile:
    def text(metric: str) -> str | None:
        fact = snapshot.facts.get(metric)
        return str(fact.value) if fact and fact.value not in (None, "") else None

    return RawProfile(
        companyName=text("providerCompanyName"),
        sector=text("sector"),
        industry=text("industry"),
    )


def _selected_facts(snapshot, metrics: frozenset[str]) -> list[RawFact]:
    result = []
    for metric in sorted(metrics):
        value = snapshot.facts.get(metric)
        if value is None:
            continue
        result.append(
            RawFact(
                metric=metric,
                value=value.value,
                unit=value.unit,
                asOf=value.as_of_date,
                publishedAt=value.published_at,
                confidence=value.confidence or 0.78,
                rawFieldOrigin=value.calculation_basis or metric,
            )
        )
    return result


def _statement_facts(snapshot, period_type: str) -> list[RawFact]:
    values = []
    for item in snapshot.statement_facts:
        if str(item.get("periodType") or "").upper() != period_type:
            continue
        values.append(
            RawFact(
                metric=item.get("metric"),
                value=item.get("value"),
                periodEnd=item.get("periodEnd"),
                periodType=period_type,
                reportingBasis=item.get("reportingBasis") or "UNKNOWN",
                publishedAt=item.get("publishedAt"),
                confidence=item.get("confidence") or 0.78,
                rawFieldOrigin=item.get("rawFieldOrigin"),
            )
        )
    return values
