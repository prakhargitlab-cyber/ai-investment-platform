"""Deterministic offline Yahoo-like MCP server used only for provider contracts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.yahoo_finance_mcp import TOOL_SCHEMA_VERSION


def create_fake_yahoo_mcp_server(
    *, scenario: str = "SUCCESS", now: datetime | None = None
) -> MCPServer:
    observed = now or datetime.now(timezone.utc)
    server = MCPServer("fake-yahoo-finance", version="test", warn_on_duplicate_tools=True)

    def base(global_instrument_id: str, symbol: str) -> dict:
        actual_symbol = "CONFLICT.NS" if scenario == "IDENTITY_MISMATCH" else symbol
        value = {
            "schemaVersion": TOOL_SCHEMA_VERSION,
            "globalInstrumentId": global_instrument_id,
            "symbol": actual_symbol,
            "exchange": "NSE",
            "currency": "INR",
            "asOf": (
                observed - timedelta(days=5) if scenario == "STALE" else observed
            ).isoformat(),
            "sourceUrl": f"https://finance.yahoo.com/quote/{actual_symbol}",
        }
        if scenario == "MALFORMED_SCHEMA":
            value["schemaVersion"] = "UNSUPPORTED"
        return value

    async def wait_if_needed() -> None:
        if scenario == "TIMEOUT":
            await asyncio.sleep(5)
        if scenario == "HEALTH_DOWN":
            raise RuntimeError("offline fake health failure")

    @server.tool(structured_output=True)
    async def yahoo_latest_price(
        globalInstrumentId: str,
        verifiedYahooSymbol: str,
        region: str,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        await wait_if_needed()
        return {**base(globalInstrumentId, verifiedYahooSymbol), **({} if scenario == "INCOMPLETE" else {"price": "250.50"})}

    @server.tool(structured_output=True)
    async def yahoo_market_history(
        globalInstrumentId: str,
        verifiedYahooSymbol: str,
        region: str,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        await wait_if_needed()
        return {
            **base(globalInstrumentId, verifiedYahooSymbol),
            "prices": [
                {
                    "observedAt": (observed - timedelta(days=offset)).isoformat(),
                    "close": str(250 - offset),
                    "currency": "INR",
                }
                for offset in range(5)
            ],
        }

    @server.tool(structured_output=True)
    async def yahoo_financials(
        globalInstrumentId: str,
        verifiedYahooSymbol: str,
        region: str,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        await wait_if_needed()
        return {
            **base(globalInstrumentId, verifiedYahooSymbol),
            "facts": [
                {
                    "metric": metric,
                    "value": value,
                    "periodEnd": period,
                    "periodType": period_type,
                    "reportingBasis": "CONSOLIDATED",
                    "rawFieldOrigin": raw,
                }
                for period_type, period, revenue, pat in (
                    ("ANNUAL", "2025-03-31", "100", "10"),
                    ("ANNUAL", "2024-03-31", "90", "8"),
                    ("QUARTERLY", "2026-06-30", "30", "3"),
                    ("QUARTERLY", "2026-03-31", "25", "2"),
                )
                for metric, value, raw in (
                    ("revenue", revenue, "totalRevenue"),
                    ("pat", pat, "netIncome"),
                )
            ],
        }

    @server.tool(structured_output=True)
    async def yahoo_company_profile(
        globalInstrumentId: str,
        verifiedYahooSymbol: str,
        region: str,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        await wait_if_needed()
        return {
            **base(globalInstrumentId, verifiedYahooSymbol),
            "profile": {
                "companyName": "Ready Limited",
                "sector": "Industrials",
                "industry": "Infrastructure Operations",
            },
        }

    @server.tool(structured_output=True)
    async def yahoo_current_news(
        globalInstrumentId: str,
        verifiedYahooSymbol: str,
        region: str,
        exchange: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        await wait_if_needed()
        return {
            **base(globalInstrumentId, verifiedYahooSymbol),
            "news": [
                {
                    "headline": "Ready publishes an issuer update",
                    "url": "https://news.example/ready-update",
                    "publishedAt": (observed - timedelta(days=1)).isoformat(),
                    "issuerSymbol": verifiedYahooSymbol,
                    "publisher": "Example News",
                }
            ],
        }

    return server
