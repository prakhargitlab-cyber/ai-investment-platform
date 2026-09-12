from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.historical_market_data import HistoricalPricePopulationService, YahooHistoricalPriceProvider
from app.models import MarketPriceObservation
from app.persistence import SqliteResearchPersistence


class FixtureProvider:
    provider_name = "FIXTURE"
    def __init__(self, instrument_id): self.instrument_id = instrument_id
    async def closes(self, _instrument, *, start, end):
        return [MarketPriceObservation(instrument_id=self.instrument_id, observed_at=start, price=Decimal("100"), currency="USD", provider=self.provider_name, source_url="fixture://history", retrieved_at=end)]


@pytest.mark.asyncio
async def test_yahoo_adapter_uses_verified_ticker_date_bounds_and_converts_close_rows():
    instrument_id = uuid4()
    observed = {}
    observed_at = datetime(2025, 9, 8, tzinfo=timezone.utc)

    class Timestamp:
        def to_pydatetime(self):
            return observed_at

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": "123.45"})]

    class Ticker:
        def history(self, **kwargs):
            observed["history"] = kwargs
            return History()

    def ticker_factory(symbol):
        observed["symbol"] = symbol
        return Ticker()

    start = datetime(2025, 8, 4, 10, tzinfo=timezone.utc)
    end = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
    provider = YahooHistoricalPriceProvider(ticker_factory)

    rows = await provider.closes(
        {
            "globalInstrumentId": str(instrument_id),
            "ticker": "UNVERIFIED",
            "structuredProviderTicker": "INFY.NS",
            "currency": "INR",
        },
        start=start,
        end=end,
    )

    assert observed["symbol"] == "INFY.NS"
    assert observed["history"] == {
        "start": start.date(),
        "end": end.date(),
        "auto_adjust": False,
    }
    assert len(rows) == 1
    assert rows[0].instrument_id == instrument_id
    assert rows[0].observed_at == observed_at
    assert rows[0].price == Decimal("123.45")
    assert rows[0].provider == "YAHOO_FINANCE"


@pytest.mark.asyncio
async def test_yahoo_adapter_skips_non_finite_close_rows_without_discarding_valid_history():
    instrument_id = uuid4()
    first = datetime(2025, 9, 8, tzinfo=timezone.utc)

    class Timestamp:
        def __init__(self, value):
            self.value = value

        def to_pydatetime(self):
            return self.value

    class History:
        def iterrows(self):
            return [
                (Timestamp(first), {"Close": "123.45"}),
                (Timestamp(first + timedelta(days=1)), {"Close": float("nan")}),
                (Timestamp(first + timedelta(days=2)), {"Close": "Infinity"}),
                (Timestamp(first + timedelta(days=3)), {"Close": "124.50"}),
            ]

    class Ticker:
        def history(self, **_kwargs):
            return History()

    rows = await YahooHistoricalPriceProvider(lambda _symbol: Ticker()).closes(
        {
            "globalInstrumentId": str(instrument_id),
            "structuredProviderTicker": "KEC.NS",
            "currency": "INR",
        },
        start=first,
        end=first + timedelta(days=4),
    )

    assert [(row.observed_at, row.price) for row in rows] == [
        (first, Decimal("123.45")),
        (first + timedelta(days=3), Decimal("124.50")),
    ]


@pytest.mark.asyncio
async def test_population_is_idempotent_and_persists_real_provider_observations(tmp_path):
    persistence = SqliteResearchPersistence(tmp_path / "research.db")
    instrument_id = uuid4(); start = datetime(2025, 9, 1, tzinfo=timezone.utc); end = start + timedelta(days=1)
    population = HistoricalPricePopulationService(persistence, FixtureProvider(instrument_id))
    await population.populate([{"globalInstrumentId": str(instrument_id), "ticker": "MSFT"}], start=start, end=end)
    await population.populate([{"globalInstrumentId": str(instrument_id), "ticker": "MSFT"}], start=start, end=end)
    rows = persistence.load_market_price_observations({instrument_id})
    assert len(rows) == 1
    assert rows[0].price == Decimal("100") and rows[0].provider == "FIXTURE"


def test_daily_history_and_intraday_snapshot_coexist_while_exact_overlap_upserts(tmp_path):
    persistence = SqliteResearchPersistence(tmp_path / "coexistence.db")
    instrument_id = uuid4()
    daily_at = datetime(2026, 9, 8, tzinfo=timezone.utc)
    intraday_at = daily_at + timedelta(hours=10)

    for observed_at, price in (
        (daily_at, Decimal("100")),
        (intraday_at, Decimal("101")),
        (daily_at, Decimal("102")),
    ):
        persistence.upsert_market_price_observation(MarketPriceObservation(
            instrument_id=instrument_id,
            observed_at=observed_at,
            price=price,
            currency="INR",
            provider="YAHOO_FINANCE",
            source_url="fixture://coexistence",
            retrieved_at=intraday_at,
        ))

    rows = persistence.load_market_price_observations({instrument_id})
    assert [(row.observed_at, row.price) for row in rows] == [
        (daily_at, Decimal("102")),
        (intraday_at, Decimal("101")),
    ]


def test_durable_coverage_query_is_aggregate_and_ignores_non_positive_prices(tmp_path):
    persistence = SqliteResearchPersistence(tmp_path / "coverage.db")
    instrument_id = uuid4()
    first = datetime(2025, 8, 1, tzinfo=timezone.utc)
    latest = datetime(2026, 9, 5, tzinfo=timezone.utc)
    for observed_at, price in ((first, Decimal("100")), (latest, Decimal("120")), (latest + timedelta(days=1), Decimal("0"))):
        persistence.upsert_market_price_observation(MarketPriceObservation(
            instrument_id=instrument_id, observed_at=observed_at, price=price,
            currency="INR", provider="FIXTURE", source_url="fixture://coverage", retrieved_at=latest,
        ))
    assert persistence.load_market_price_coverage({instrument_id}) == {
        instrument_id: (first, latest, 2)
    }
