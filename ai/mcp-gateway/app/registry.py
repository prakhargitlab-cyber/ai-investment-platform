"""Allowlisted MCP tool registry."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from app.contracts import McpToolDefinition


class DuplicateMcpToolError(ValueError):
    pass


class McpToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, McpToolDefinition] = {}

    def register(self, definition: McpToolDefinition) -> None:
        if definition.name in self._tools:
            raise DuplicateMcpToolError(f"MCP tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def get(self, name: str) -> McpToolDefinition | None:
        return self._tools.get(name)

    @property
    def tools(self) -> Mapping[str, McpToolDefinition]:
        return MappingProxyType(self._tools)

    def discover(self) -> list[dict[str, Any]]:
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "inputSchema": definition.input_model.model_json_schema(by_alias=True),
                "riskClass": definition.risk_class.value,
                "kind": definition.kind.value,
            }
            for definition in sorted(self._tools.values(), key=lambda item: item.name)
        ]
