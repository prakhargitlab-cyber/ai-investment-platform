"""Authenticated regional watchlist contracts and durable research projection."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


MarketRegion = Literal["INDIA", "EUROPE", "USA"]


class EnsureDefaultWatchlistRequest(BaseModel):
    region: MarketRegion


class AddWatchlistInstrumentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    global_instrument_id: UUID = Field(alias="globalInstrumentId")
    source_period: Literal["DAY", "WEEK", "MONTH", "YEAR"] | None = Field(
        default=None, alias="sourcePeriod"
    )
    source_performance_pct: Decimal | None = Field(default=None, alias="sourcePerformancePct")

    def portfolio_payload(self) -> dict[str, Any]:
        return {
            "globalInstrumentId": str(self.global_instrument_id),
            "sourcePeriod": self.source_period,
            "sourcePerformancePct": float(self.source_performance_pct) if self.source_performance_pct is not None else None,
        }


async def watchlist_research_projection(orchestrator, payload: dict, **kwargs) -> dict:
    """Compose membership with existing durable company research; never refresh."""
    memberships = payload.get("instruments", [])

    async def project(membership: dict) -> dict:
        instrument_id = UUID(str(membership["globalInstrumentId"]))
        metadata = membership.get("instrument")
        company = await orchestrator.read_global_company_state(
            instrument_id,
            metadata=metadata if isinstance(metadata, dict) else None,
            **kwargs,
        )
        return {
            "globalInstrumentId": str(instrument_id),
            "companyName": (metadata or {}).get("canonicalName") if isinstance(metadata, dict) else None,
            "ticker": (metadata or {}).get("primarySymbol") if isinstance(metadata, dict) else None,
            "exchange": (metadata or {}).get("primaryExchange") if isinstance(metadata, dict) else None,
            "country": (metadata or {}).get("country") if isinstance(metadata, dict) else None,
            "currency": (metadata or {}).get("currency") if isinstance(metadata, dict) else None,
            "assetType": (metadata or {}).get("assetType") if isinstance(metadata, dict) else None,
            "held": False,
            "sourcePeriod": membership.get("sourcePeriod"),
            "sourcePerformancePct": membership.get("sourcePerformancePct"),
            "addedAt": membership.get("addedAt"),
            "company": company.model_dump(mode="json", by_alias=True),
        }

    instruments = await asyncio.gather(*(project(value) for value in memberships))
    return {"watchlist": payload.get("watchlist", {}), "instruments": instruments}
