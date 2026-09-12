"""CLI entry point for STDIO and stateless Streamable HTTP transports."""
from __future__ import annotations

import argparse
import logging
import sys

from mcp.server.transport_security import TransportSecuritySettings

from app.server import create_container, create_mcp_server
from app.settings import McpGatewaySettings


def main() -> None:
    settings = McpGatewaySettings()
    parser = argparse.ArgumentParser(description="AI Investment internal MCP gateway")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=settings.transport,
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    if not settings.feature_enabled:
        raise SystemExit("MCP gateway is disabled by AIP_FEATURE_MCP_ENABLED")

    server = create_mcp_server(create_container(settings))
    if args.transport == "stdio":
        server.run("stdio")
        return
    server.run(
        "streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.streamable_http_path,
        stateless_http=True,
        json_response=True,
        max_request_body_size=settings.max_request_body_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )


if __name__ == "__main__":
    main()
