"""Fail-closed MCP risk and authorization policy."""
from __future__ import annotations

from app.contracts import (
    McpAuthContext,
    McpErrorCode,
    McpGatewayError,
    McpRiskClass,
    McpToolDefinition,
)


class McpToolPolicy:
    """Iteration 5A permits authenticated SAFE_READ definitions only."""

    def authorize(self, definition: McpToolDefinition, auth: McpAuthContext) -> None:
        if definition.risk_class is not McpRiskClass.SAFE_READ:
            raise McpGatewayError(McpErrorCode.MCP_TOOL_DENIED)
        if not auth.service_identity.strip():
            raise McpGatewayError(McpErrorCode.UNAUTHORIZED)
        if definition.requires_user and auth.user_id is None:
            raise McpGatewayError(McpErrorCode.UNAUTHORIZED)
        missing_scopes = set(definition.required_scopes).difference(auth.scopes)
        if missing_scopes:
            raise McpGatewayError(McpErrorCode.FORBIDDEN)
