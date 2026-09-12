"""Broad-market discovery boundary; it is not a portfolio or dashboard read path."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class MarketUniverseListing:
    ticker: str
    exchange: str
    country: str
    currency: str | None
    company_name: str | None
    sector: str | None
    industry: str | None
    isin: str | None = None


class MarketUniverseUnavailable(RuntimeError): pass


@dataclass(frozen=True)
class MarketUniverseInstrument:
    """Provider-neutral, canonical market-universe identity.

    Classification remains owned upstream. In particular, a missing
    ``canonical_sector`` is retained so operational population can report it,
    while Sector Performance can exclude it without recreating India's
    classification rules in Python.
    """

    global_instrument_id: UUID
    ticker: str
    company_name: str | None
    isin: str | None
    exchange: str
    mic: str | None
    country: str
    currency: str | None
    asset_type: str
    status: str
    region: str
    canonical_sector: str | None
    official_industry: str | None
    source: str | None
    retrieved_at: datetime | str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "globalInstrumentId": str(self.global_instrument_id),
            "ticker": self.ticker,
            "symbol": self.ticker,
            "companyName": self.company_name,
            "canonicalName": self.company_name,
            "isin": self.isin,
            "exchange": self.exchange,
            "mic": self.mic,
            "country": self.country,
            "currency": self.currency,
            "assetType": self.asset_type,
            "status": self.status,
            "region": self.region,
            "canonicalSector": self.canonical_sector,
            "officialIndustry": self.official_industry,
            "source": self.source,
            "retrievedAt": self.retrieved_at,
        }


class MarketUniverseProvider(Protocol):
    region: str
    async def listings(self) -> list[MarketUniverseListing]: ...


class UsaMarketUniverseProvider:
    """SEC supplies listed issuer identity but not complete sector classification.

    A licensed/verified listing metadata feed is deliberately required before
    this provider can publish a broad, sector-filterable universe.
    """
    region = "USA"
    async def listings(self) -> list[MarketUniverseListing]:
        raise MarketUniverseUnavailable("USA_UNIVERSE_REQUIRES_SECTOR_METADATA_SOURCE")


class EuropeMarketUniverseProvider:
    """EODHD exchange-symbol universe boundary; requires AIP_EODHD_API_KEY."""
    region = "EUROPE"
    async def listings(self) -> list[MarketUniverseListing]:
        raise MarketUniverseUnavailable("EUROPE_UNIVERSE_REQUIRES_EODHD_API_KEY")


class IndiaMarketUniverseProvider:
    """Adapter over portfolio-service's durable official NSE/Nifty 500 cache."""
    region = "INDIA"

    def __init__(self, portfolio_orchestrator) -> None:
        self._portfolio_orchestrator = portfolio_orchestrator

    async def listings(
        self,
        *,
        correlation_id: str | None = None,
        identity_headers: dict[str, str | None] | None = None,
    ) -> list[MarketUniverseInstrument]:
        try:
            values = await self._portfolio_orchestrator.india_nifty500_universe(
                correlation_id=correlation_id,
                identity_headers=identity_headers,
            )
        except Exception as exc:
            # Avoid importing the orchestration module here and creating a
            # provider-layer cycle.  The route/job translates one stable
            # provider-neutral outcome.
            raise MarketUniverseUnavailable("INDIA_NIFTY500_UNIVERSE_UNAVAILABLE") from exc

        instruments: list[MarketUniverseInstrument] = []
        for value in values:
            try:
                instrument_id = UUID(str(value.get("globalInstrumentId")))
            except (TypeError, ValueError, AttributeError):
                continue
            ticker = str(value.get("symbol") or "").strip()
            status = str(value.get("status") or "").strip().upper()
            asset_type = str(value.get("assetType") or "").strip().upper()
            country = str(value.get("country") or "").strip().upper()
            exchange = str(value.get("exchange") or "").strip().upper()
            if not ticker or status != "ACTIVE" or asset_type != "EQUITY" or country != "IN" or not exchange:
                continue
            instruments.append(MarketUniverseInstrument(
                global_instrument_id=instrument_id,
                ticker=ticker,
                company_name=_optional_text(value.get("companyName")),
                isin=_optional_text(value.get("isin")),
                exchange=exchange,
                mic=_optional_text(value.get("mic")),
                country=country,
                currency=_optional_text(value.get("currency")),
                asset_type=asset_type,
                status=status,
                region=self.region,
                canonical_sector=_optional_text(value.get("canonicalSector")),
                official_industry=_optional_text(value.get("officialIndustry")),
                source=_optional_text(value.get("source")),
                retrieved_at=value.get("retrievedAt"),
            ))
        return instruments


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
