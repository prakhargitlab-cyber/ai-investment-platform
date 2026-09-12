"""MCP protocol-edge normalization for unknown tools and malformed arguments."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from mcp.server.context import HandlerResult, ServerRequestContext
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from app.audit import McpAuditSink
from app.contracts import McpAuthContext, McpErrorCode, McpResultEnvelope, normalize_request_id
from app.registry import McpToolRegistry


class McpProtocolGuard:
    """Return the application error envelope before the SDK dispatches invalid calls."""

    def __init__(
        self,
        registry: McpToolRegistry,
        audit: McpAuditSink,
        auth: McpAuthContext,
    ) -> None:
        self.registry = registry
        self.audit = audit
        self.auth = auth

    async def __call__(
        self,
        context: ServerRequestContext[Any, Any],
        call_next: Callable[[ServerRequestContext[Any, Any]], Awaitable[HandlerResult]],
    ) -> HandlerResult:
        if context.method != "tools/call":
            return await call_next(context)
        params = context.params or {}
        tool = str(params.get("name") or "")
        definition = self.registry.get(tool)
        if definition is None:
            return self._failure(context, tool, McpErrorCode.MCP_TOOL_NOT_FOUND)
        try:
            definition.input_model.model_validate(params.get("arguments") or {})
        except ValidationError:
            return self._failure(context, tool, McpErrorCode.INVALID_ARGUMENT, definition)
        return await call_next(context)

    def _failure(self, context, tool, code, definition=None) -> CallToolResult:
        request_id = normalize_request_id(context.request_id)
        self.audit.emit(
            "MCP_TOOL_INVOKED",
            tool=tool,
            request_id=request_id,
            auth=self.auth,
            risk_class=definition.risk_class if definition else None,
            kind=definition.kind if definition else None,
        )
        self.audit.emit(
            "MCP_TOOL_FAILED",
            tool=tool,
            request_id=request_id,
            auth=self.auth,
            risk_class=definition.risk_class if definition else None,
            kind=definition.kind if definition else None,
            code=code.value,
        )
        envelope = McpResultEnvelope.failure(
            tool=tool,
            request_id=request_id,
            code=code,
        ).wire()
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(envelope, separators=(",", ":")))],
            structuredContent=envelope,
            isError=True,
        )
