#!/usr/bin/env python3
"""End-to-end LOCAL MCP smoke test over the official STDIO transport."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from mcp import Client, StdioServerParameters


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc).isoformat()


class PersistedReadFixture(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        path = urlparse(self.path).path
        if path == f"/api/v1/research/readiness/{INSTRUMENT_ID}":
            self._json(
                {
                    "globalInstrumentId": INSTRUMENT_ID,
                    "overallStatus": "READY",
                    "requirements": [],
                    "generatedAt": NOW,
                }
            )
            return
        if path == "/api/v1/research/sector-performance":
            self._json(
                {
                    "region": "INDIA",
                    "sector": "FINANCIAL_SERVICES",
                    "period": "MONTH",
                    "leaders": [],
                    "asOf": NOW,
                }
            )
            return
        self._json({"message": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if path == f"/api/v1/research/analysis/{INSTRUMENT_ID}":
            self._json(
                {
                    "globalInstrumentId": INSTRUMENT_ID,
                    "ruleEngineVersion": "STOCK_RULE_ENGINE_V1",
                    "score": 78,
                    "generatedAt": NOW,
                }
            )
            return
        self._json({"message": "not found"}, status=404)

    def _json(self, value: dict, *, status: int = 200) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def persisted_read_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), PersistedReadFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def rejected(client: Client, tool: str) -> bool:
    try:
        result = await client.call_tool(tool, {})
    except Exception:
        return True
    return bool(result.is_error)


async def run_smoke(research_base_url: str) -> None:
    environment = {
        "AIP_ENVIRONMENT": "LOCAL",
        "AIP_FEATURE_MCP_ENABLED": "true",
        "AIP_MCP_TRANSPORT": "stdio",
        "AIP_MCP_RESEARCH_BASE_URL": research_base_url,
        "AIP_MCP_SERVICE_IDENTITY": "local-mcp-smoke",
        "AIP_MCP_SCOPES": "mcp:read",
        "AIP_MCP_EXTERNAL_PROVIDERS_ENABLED": "false",
        "PYTHONUNBUFFERED": "1",
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.main", "--transport", "stdio"],
        env=environment,
        cwd=ROOT,
    )
    async with Client(parameters) as client:
        print("initialize_mcp=PASS")
        listing = await client.list_tools()
        names = {tool.name for tool in listing.tools}
        assert len(names) == 9 and "get_research_readiness" in names
        print(f"list_tools=PASS count={len(names)}")

        readiness = await client.call_tool(
            "get_research_readiness", {"globalInstrumentId": INSTRUMENT_ID}
        )
        assert readiness.structured_content["data"]["overallStatus"] == "READY"
        print("get_research_readiness=PASS provider_calls=0")

        analysis = await client.call_tool(
            "get_company_analysis", {"globalInstrumentId": INSTRUMENT_ID}
        )
        assert analysis.structured_content["provenance"]["ruleEngineVersion"] == "STOCK_RULE_ENGINE_V1"
        print("get_company_analysis=PASS rule_engine=STOCK_RULE_ENGINE_V1 provider_calls=0")

        sector = await client.call_tool(
            "get_sector_performance",
            {"region": "INDIA", "sector": "FINANCIAL_SERVICES", "period": "MONTH"},
        )
        assert sector.structured_content["data"]["sector"] == "FINANCIAL_SERVICES"
        print("get_sector_performance=PASS provider_calls=0")

        assert await rejected(client, "not_a_tool")
        print("invalid_tool_rejected=PASS")
        assert await rejected(client, "place_order")
        print("unsafe_tool_unavailable=PASS")
    print("clean_shutdown=PASS")


def main() -> None:
    with persisted_read_server() as research_base_url:
        asyncio.run(run_smoke(research_base_url))
    print("local_mcp_smoke=PASS")


if __name__ == "__main__":
    main()
