from __future__ import annotations

import argparse
import logging
import sys

from mcp.server.transport_security import TransportSecuritySettings

from yahoo_mcp_server.server import create_yahoo_mcp_server
from yahoo_mcp_server.settings import YahooMcpSettings


def main() -> None:
    settings = YahooMcpSettings()
    parser = argparse.ArgumentParser(description="First-party Yahoo Finance MCP server")
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default=settings.transport
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    server = create_yahoo_mcp_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
        return
    server.run(
        "streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.http_path,
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
