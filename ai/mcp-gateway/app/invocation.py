"""MCP invocation gateway: lookup, validation, policy, timeout and normalization."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import ValidationError

from app.audit import McpAuditSink
from app.contracts import (
    McpAuthContext,
    McpErrorCode,
    McpGatewayError,
    McpResultEnvelope,
    normalize_request_id,
)
from app.policy import McpToolPolicy
from app.registry import McpToolRegistry


class McpInvocationService:
    def __init__(
        self,
        registry: McpToolRegistry,
        policy: McpToolPolicy,
        audit: McpAuditSink,
        *,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.registry = registry
        self.policy = policy
        self.audit = audit
        self.timeout_seconds = timeout_seconds

    async def invoke(
        self,
        tool: str,
        arguments: dict[str, Any] | None,
        auth: McpAuthContext,
        *,
        request_id: str | int | None = None,
    ) -> dict[str, Any]:
        request_id_value = normalize_request_id(request_id)
        started = time.perf_counter()
        definition = self.registry.get(tool)
        self.audit.emit(
            "MCP_TOOL_INVOKED",
            tool=tool,
            request_id=request_id_value,
            auth=auth,
            risk_class=definition.risk_class if definition else None,
            kind=definition.kind if definition else None,
        )

        if definition is None:
            return self._failed(
                tool, request_id_value, auth, McpErrorCode.MCP_TOOL_NOT_FOUND, started
            )

        try:
            self.policy.authorize(definition, auth)
        except McpGatewayError as exc:
            return self._denied(tool, request_id_value, auth, definition, exc.code, started)

        try:
            validated = definition.input_model.model_validate(arguments or {})
        except ValidationError:
            return self._failed(
                tool, request_id_value, auth, McpErrorCode.INVALID_ARGUMENT, started, definition
            )

        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await definition.handler(validated, auth, request_id_value)
        except TimeoutError:
            return self._failed(
                tool, request_id_value, auth, McpErrorCode.DOWNSTREAM_TIMEOUT, started, definition
            )
        except McpGatewayError as exc:
            return self._failed(tool, request_id_value, auth, exc.code, started, definition)
        except Exception:
            return self._failed(
                tool,
                request_id_value,
                auth,
                McpErrorCode.DOWNSTREAM_UNAVAILABLE,
                started,
                definition,
            )

        envelope = McpResultEnvelope.success(
            tool=tool,
            request_id=request_id_value,
            data=result.data,
            generated_at=result.generated_at,
            rule_engine_version=result.rule_engine_version,
            warnings=list(result.warnings),
        )
        self.audit.emit(
            "MCP_TOOL_SUCCEEDED",
            tool=tool,
            request_id=request_id_value,
            auth=auth,
            risk_class=definition.risk_class,
            kind=definition.kind,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return envelope.wire()

    def _denied(self, tool, request_id, auth, definition, code, started):
        self.audit.emit(
            "MCP_TOOL_DENIED",
            tool=tool,
            request_id=request_id,
            auth=auth,
            risk_class=definition.risk_class,
            kind=definition.kind,
            code=code.value,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return McpResultEnvelope.failure(
            tool=tool, request_id=request_id, code=code
        ).wire()

    def _failed(self, tool, request_id, auth, code, started, definition=None):
        self.audit.emit(
            "MCP_TOOL_FAILED",
            tool=tool,
            request_id=request_id,
            auth=auth,
            risk_class=definition.risk_class if definition else None,
            kind=definition.kind if definition else None,
            code=code.value,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return McpResultEnvelope.failure(
            tool=tool, request_id=request_id, code=code
        ).wire()
