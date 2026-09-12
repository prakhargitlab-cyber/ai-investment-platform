"""Read-only MCP tool contracts over existing application capabilities."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.application_client import ApplicationResearchReader
from app.contracts import (
    McpAuthContext,
    McpToolDefinition,
    McpRiskClass,
    McpToolExecution,
    McpToolKind,
    StrictContract,
)
from app.registry import McpToolRegistry


class CanonicalInstrumentInput(StrictContract):
    global_instrument_id: UUID = Field(alias="globalInstrumentId")

    @field_validator("global_instrument_id")
    @classmethod
    def non_zero_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("canonical globalInstrumentId is required")
        return value


class CompanyAnalysisInput(CanonicalInstrumentInput):
    allow_partial: bool = Field(default=False, alias="allowPartial")


class RecentNewsInput(CanonicalInstrumentInput):
    days: int = Field(default=30, ge=1, le=30)


class SectorPerformanceInput(StrictContract):
    region: Literal["USA", "EUROPE", "INDIA"]
    sector: str = Field(min_length=1, max_length=120)
    period: Literal["DAY", "WEEK", "MONTH", "YEAR"]
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("sector")
    @classmethod
    def non_blank_sector(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("sector is required")
        return normalized


class EvidenceSearchInput(CanonicalInstrumentInput):
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def non_blank_query(cls, value: str) -> str:
        return value.strip()


class WatchlistInput(StrictContract):
    watchlist_id: UUID = Field(alias="watchlistId")


TOOL_MODELS: tuple[tuple[str, str, type[StrictContract], bool], ...] = (
    (
        "get_research_readiness",
        "Read persisted Research Readiness for one canonical globalInstrumentId; never acquires data.",
        CanonicalInstrumentInput,
        False,
    ),
    (
        "get_company_analysis",
        "Return the existing STOCK_RULE_ENGINE_V1 analysis over durable data.",
        CompanyAnalysisInput,
        False,
    ),
    (
        "get_financial_facts",
        "Read persisted financial statement facts for one canonical globalInstrumentId.",
        CanonicalInstrumentInput,
        False,
    ),
    (
        "get_quarterly_results",
        "Read persisted quarterly results for one canonical globalInstrumentId.",
        CanonicalInstrumentInput,
        False,
    ),
    (
        "get_shareholding",
        "Read persisted shareholding snapshots for one canonical globalInstrumentId.",
        CanonicalInstrumentInput,
        False,
    ),
    (
        "get_recent_news",
        "Read persisted current news within a maximum rolling window of 30 days.",
        RecentNewsInput,
        False,
    ),
    (
        "get_sector_performance",
        "Read the existing durable Sector Performance result without population or provider calls.",
        SectorPerformanceInput,
        False,
    ),
    (
        "search_research_evidence",
        "Search persisted evidence metadata for one canonical globalInstrumentId.",
        EvidenceSearchInput,
        False,
    ),
    (
        "get_watchlist",
        "Read one authenticated user's persisted watchlist research projection.",
        WatchlistInput,
        True,
    ),
)


def build_internal_tool_registry(reader: ApplicationResearchReader) -> McpToolRegistry:
    registry = McpToolRegistry()
    for name, description, input_model, requires_user in TOOL_MODELS:
        registry.register(
            McpToolDefinition(
                name=name,
                description=description,
                input_model=input_model,
                risk_class=McpRiskClass.SAFE_READ,
                kind=McpToolKind.INTERNAL,
                handler=_handler(reader, name),
                required_scopes=("mcp:read", "watchlist:read")
                if requires_user
                else ("mcp:read",),
                requires_user=requires_user,
            )
        )
    return registry


def _handler(reader: ApplicationResearchReader, tool: str):
    async def execute(arguments, auth: McpAuthContext, request_id: str) -> McpToolExecution:
        payload: dict[str, Any] = arguments.model_dump(mode="json", by_alias=True)
        result = await reader.invoke(tool, payload, auth, request_id)
        generated_at = _generated_at(result)
        rule_engine_version = (
            str(result.get("ruleEngineVersion"))
            if tool == "get_company_analysis" and result.get("ruleEngineVersion")
            else None
        )
        return McpToolExecution(
            data=result,
            generated_at=generated_at,
            rule_engine_version=rule_engine_version,
        )

    return execute


def _generated_at(result: dict[str, Any]):
    from datetime import datetime

    value = result.get("generatedAt") or result.get("asOf")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
