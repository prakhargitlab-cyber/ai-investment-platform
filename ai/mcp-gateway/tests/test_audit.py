from __future__ import annotations

import json
import logging

from app.audit import StructuredMcpAuditLogger
from app.contracts import McpRiskClass, McpToolKind


def test_structured_audit_logs_metadata_trace_and_redacts_sensitive_details(
    auth, caplog, monkeypatch
) -> None:
    class SpanContext:
        is_valid = True
        trace_id = 0x1234

    class Span:
        @staticmethod
        def get_span_context():
            return SpanContext()

    monkeypatch.setattr("app.audit.trace.get_current_span", lambda: Span())
    logger = StructuredMcpAuditLogger(logging.getLogger("mcp-audit-test"), environment="TEST")
    with caplog.at_level(logging.INFO, logger="mcp-audit-test"):
        logger.emit(
            "MCP_TOOL_INVOKED",
            tool="get_company_analysis",
            request_id="audit-5a",
            auth=auth,
            risk_class=McpRiskClass.SAFE_READ,
            kind=McpToolKind.INTERNAL,
            details={
                "quantity": 100,
                "averageCost": 5,
                "token": "Bearer abc.def.ghi",
                "safeCode": "COMPANY_READ",
            },
        )
    record = json.loads(caplog.records[-1].message)
    assert record["event"] == "MCP_TOOL_INVOKED"
    assert record["requestId"] == "audit-5a"
    assert record["traceId"] == "00000000000000000000000000001234"
    assert record["details"] == {"safeCode": "COMPANY_READ"}
    assert "quantity" not in caplog.records[-1].message
    assert "averageCost" not in caplog.records[-1].message
