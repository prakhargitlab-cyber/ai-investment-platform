"""Structured, argument-free MCP audit events."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from opentelemetry import trace

from app.contracts import McpAuthContext, McpRiskClass, McpToolKind, sanitize_response_data


class McpAuditSink(Protocol):
    def emit(
        self,
        event: str,
        *,
        tool: str,
        request_id: str,
        auth: McpAuthContext,
        risk_class: McpRiskClass | None = None,
        kind: McpToolKind | None = None,
        code: str | None = None,
        duration_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...


class StructuredMcpAuditLogger:
    def __init__(self, logger: logging.Logger | None = None, *, environment: str = "LOCAL") -> None:
        self.logger = logger or logging.getLogger("aip.mcp.audit")
        self.environment = environment

    def emit(
        self,
        event: str,
        *,
        tool: str,
        request_id: str,
        auth: McpAuthContext,
        risk_class: McpRiskClass | None = None,
        kind: McpToolKind | None = None,
        code: str | None = None,
        duration_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "mcp-gateway",
            "environment": self.environment,
            "level": "INFO",
            "event": event,
            "tool": tool,
            "requestId": request_id,
            "serviceIdentity": auth.service_identity,
            "authenticationType": auth.authentication_type.value,
        }
        if risk_class is not None:
            record["riskClass"] = risk_class.value
        if kind is not None:
            record["toolKind"] = kind.value
        if code is not None:
            record["code"] = code
        if duration_ms is not None:
            record["durationMs"] = duration_ms
        if details:
            record["details"] = sanitize_response_data(details)
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record["traceId"] = format(span_context.trace_id, "032x")
        self.logger.info(json.dumps(record, separators=(",", ":"), sort_keys=True))
