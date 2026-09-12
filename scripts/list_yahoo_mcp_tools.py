"""List first-party Yahoo MCP tools through the official SDK for local acceptance."""
from __future__ import annotations

import argparse
import asyncio
import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def list_tools(url: str) -> None:
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(5)) as http_client:
        async with Client(
            streamable_http_client(url, http_client=http_client),
            read_timeout_seconds=5,
            cache=None,
        ) as client:
            result = await client.list_tools()
    print(json.dumps({"tools": sorted(item.name for item in result.tools)}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    asyncio.run(list_tools(args.url))


if __name__ == "__main__":
    main()
