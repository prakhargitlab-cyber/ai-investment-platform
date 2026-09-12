"""Provider-neutral historical-close population, deliberately outside request paths."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.models import MarketPriceObservation


def has_year_historical_coverage(
    first_observed_at: datetime,
    latest_observed_at: datetime,
    observation_count: int,
) -> bool:
    """Return whether durable prices span the YEAR comparison horizon."""
    return (
        observation_count >= 2
        and latest_observed_at - first_observed_at >= timedelta(days=365)
    )


class HistoricalPriceProvider(Protocol):
    provider_name: str

    async def closes(self, instrument: dict[str, Any], *, start: datetime, end: datetime) -> list[MarketPriceObservation]: ...


class HistoricalPriceProviderError(RuntimeError):
    pass


class HistoricalPricePersistenceError(RuntimeError):
    pass


class YahooHistoricalPriceProvider:
    """Existing structured-market Yahoo integration's historical-close adapter.

    It is invoked only by an explicit/scheduled population worker, never by a
    sector-performance request.
    """
    provider_name = "YAHOO_FINANCE"

    def __init__(self, ticker_factory: Any) -> None:
        self.ticker_factory = ticker_factory

    async def closes(self, instrument: dict[str, Any], *, start: datetime, end: datetime) -> list[MarketPriceObservation]:
        return await asyncio.to_thread(self._closes, instrument, start, end)

    def _closes(self, instrument: dict[str, Any], start: datetime, end: datetime) -> list[MarketPriceObservation]:
        ticker = str(instrument.get("structuredProviderTicker") or instrument.get("ticker") or "").strip()
        if not ticker:
            return []
        history = self.ticker_factory(ticker).history(start=start.date(), end=end.date(), auto_adjust=False)
        out: list[MarketPriceObservation] = []
        for observed_at, row in history.iterrows():
            try:
                close = Decimal(str(row["Close"]))
            except Exception:
                continue
            # Yahoo can include rows whose Close value is NaN. Decimal accepts
            # "nan", but ordering it raises decimal.InvalidOperation and would
            # discard every otherwise valid row returned for the instrument.
            if not close.is_finite() or close <= 0:
                continue
            stamp = observed_at.to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            out.append(MarketPriceObservation(
                instrument_id=UUID(str(instrument["globalInstrumentId"])), observed_at=stamp,
                price=close, currency=instrument.get("currency"), provider=self.provider_name,
                source_url=f"https://finance.yahoo.com/quote/{ticker}/history", retrieved_at=datetime.now(timezone.utc),
            ))
        return out


class HistoricalPricePopulationService:
    """Idempotent population boundary for a scheduled/explicit market-data job."""
    def __init__(self, persistence, provider: HistoricalPriceProvider) -> None:
        self.persistence = persistence
        self.provider = provider

    async def populate(self, instruments: list[dict[str, Any]], *, start: datetime, end: datetime) -> int:
        written = 0
        for instrument in instruments:
            try:
                observations = await self.provider.closes(instrument, start=start, end=end)
            except Exception as exc:
                raise HistoricalPriceProviderError("HISTORICAL_PRICE_PROVIDER_UNAVAILABLE") from exc
            for observation in observations:
                async_writer = getattr(self.persistence, "upsert_market_price_observation_async", None)
                try:
                    if async_writer is not None:
                        await async_writer(observation)
                    else:
                        self.persistence.upsert_market_price_observation(observation)
                except Exception as exc:
                    raise HistoricalPricePersistenceError("HISTORICAL_PRICE_PERSISTENCE_UNAVAILABLE") from exc
                written += 1
        return written
