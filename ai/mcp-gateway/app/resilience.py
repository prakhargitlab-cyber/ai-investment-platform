"""Small extension contracts for a later distributed resilience layer."""
from __future__ import annotations

from typing import Protocol

from app.contracts import McpAuthContext


class McpRateLimiter(Protocol):
    """Authorize a call using a future shared/distributed rate-limit backend."""

    async def authorize(
        self, *, tool: str, auth: McpAuthContext, request_id: str
    ) -> None: ...


class McpCircuitBreaker(Protocol):
    """Track downstream health without prescribing an in-process implementation."""

    async def before_call(self, *, dependency: str) -> None: ...

    async def record_success(self, *, dependency: str) -> None: ...

    async def record_failure(self, *, dependency: str) -> None: ...
