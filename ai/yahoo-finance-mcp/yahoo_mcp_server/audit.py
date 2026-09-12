from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from opentelemetry import trace


logger = logging.getLogger("yahoo_finance_mcp.audit")


class YahooMcpAudit:
    def __init__(self, *, service: str, environment: str) -> None:
        self.service = service
        self.environment = environment

    def emit(
        self,
        event: str,
        *,
        request_id: str,
        global_instrument_id: str,
        symbol: str,
        tool: str,
        started: float,
        error_code: str | None = None,
    ) -> None:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": self.service,
            "environment": self.environment,
            "level": "INFO" if event in {"YAHOO_MCP_REQUEST", "YAHOO_MCP_SUCCESS"} else "WARNING",
            "event": event,
            "requestId": request_id,
            "globalInstrumentId": global_instrument_id,
            "yahooSymbol": symbol,
            "tool": tool,
            "durationMs": max(0, round((monotonic() - started) * 1000, 2)),
        }
        if span_context.is_valid:
            payload["traceId"] = format(span_context.trace_id, "032x")
        if error_code:
            payload["errorCode"] = error_code
        message = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if payload["level"] == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)
