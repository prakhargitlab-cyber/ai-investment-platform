"""Guardian Review Slice 1 -- Yahoo/canonical identity integrity.

Proves the fix for the historical corruption class found in persisted data:
canonical instrument INFY (UUID 64fcda58-0574-4480-8b0f-94f296907c9b) had a
YAHOO_FINANCE observation whose source URL/ticker was HCL-INSYS.NS instead
of the correct INFY.NS. The historical-price acquisition path
(app.historical_market_data.YahooHistoricalPriceProvider._closes) trusted a
`structuredProviderTicker` string with no independent check that Yahoo's
RETURNED data actually belongs to the requested canonical instrument.

The fix (verify_historical_price_identity /
verify_identity_by_isin_or_name in app.structured_market, wired into
YahooHistoricalPriceProvider._closes via _verify_identity_or_raise) enforces:
a market observation for canonical instrument X must not be persisted unless
the provider's RETURNED identity (ISIN if available, else company-name
similarity, plus exchange/currency family) verifies against X -- not merely
a `mapping.status == VERIFIED` flag inherited from an external system, and
not merely an echo of the requested symbol back (which the INFY/HCL-INSYS
row would have trivially passed, since HCL-INSYS.NS was itself the
requested/returned symbol -- the corruption was upstream of the request, in
which ticker got associated with which UUID).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    HistoricalPricePopulationService,
    YahooHistoricalPriceProvider,
)
from app.persistence import SqliteResearchPersistence

INFY_UUID = UUID("64fcda58-0574-4480-8b0f-94f296907c9b")


def _history(price="1523.40", observed_at=None):
    observed_at = observed_at or datetime(2026, 9, 20, tzinfo=timezone.utc)

    class Timestamp:
        def to_pydatetime(self):
            return observed_at

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": price})]

    return History()


class _FakeTicker:
    def __init__(self, info, price="1523.40"):
        self.info = info
        self._price = price

    def history(self, **_kwargs):
        return _history(self._price)


def _infy_instrument(structured_ticker: str) -> dict:
    return {
        "globalInstrumentId": str(INFY_UUID),
        "ticker": "INFY",
        "structuredProviderTicker": structured_ticker,
        "currency": "INR",
        "isin": "INE009A01021",
        "companyName": "Infosys Limited",
        "canonicalName": "Infosys Limited",
        "exchange": "NSI",
    }


INFY_YAHOO_INFO = {
    "symbol": "INFY.NS", "longName": "Infosys Limited",
    "exchange": "NSI", "currency": "INR", "isin": "INE009A01021",
}

HCL_INSYS_YAHOO_INFO = {
    # What Yahoo actually returns for the WRONG ticker -- a different
    # company entirely, under whatever symbol was mistakenly requested.
    "symbol": "HCL-INSYS.NS", "longName": "HCL Insystems Limited",
    "exchange": "NSI", "currency": "INR", "isin": "INE151A01021",
}


# 1 -- INFY canonical UUID + INFY.NS -> allowed -----------------------------
@pytest.mark.asyncio
async def test_01_infy_uuid_with_correct_infy_ticker_is_allowed():
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(INFY_YAHOO_INFO))
    rows = await provider.closes(
        _infy_instrument("INFY.NS"),
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].instrument_id == INFY_UUID
    assert rows[0].price == Decimal("1523.40")


# 2 -- INFY canonical UUID + HCL-INSYS.NS -> rejected -----------------------
@pytest.mark.asyncio
async def test_02_infy_uuid_with_hcl_insys_ticker_is_rejected():
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(HCL_INSYS_YAHOO_INFO, price="10.70"))
    with pytest.raises(HistoricalPriceIdentityConflict, match="VERIFIED_HISTORICAL_IDENTITY_MISMATCH"):
        await provider.closes(
            _infy_instrument("HCL-INSYS.NS"),
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )


# 3 -- no MarketPriceObservation persisted on mismatch ----------------------
@pytest.mark.asyncio
async def test_03_no_observation_persisted_on_identity_mismatch(tmp_path):
    persistence = SqliteResearchPersistence(tmp_path / "identity.db")
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(HCL_INSYS_YAHOO_INFO, price="10.70"))
    service = HistoricalPricePopulationService(persistence, provider)
    with pytest.raises(HistoricalPriceIdentityConflict):
        await service.populate(
            [_infy_instrument("HCL-INSYS.NS")],
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
    rows = persistence.load_market_price_observations({INFY_UUID})
    assert rows == []


# 4 -- concurrent different instruments cannot cross-contaminate -----------
@pytest.mark.asyncio
async def test_04_concurrent_different_instruments_no_cross_contamination():
    other_uuid = uuid4()
    infos = {
        "INFY.NS": (INFY_YAHOO_INFO, "1523.40"),
        "TCS.NS": (
            {"symbol": "TCS.NS", "longName": "Tata Consultancy Services Limited",
             "exchange": "NSI", "currency": "INR", "isin": "INE467B01029"},
            "3900.00",
        ),
    }

    def ticker_factory(symbol):
        info, price = infos[symbol]
        return _FakeTicker(info, price)

    provider = YahooHistoricalPriceProvider(ticker_factory)
    infy_instrument = _infy_instrument("INFY.NS")
    tcs_instrument = {
        "globalInstrumentId": str(other_uuid),
        "ticker": "TCS",
        "structuredProviderTicker": "TCS.NS",
        "currency": "INR",
        "isin": "INE467B01029",
        "companyName": "Tata Consultancy Services Limited",
        "exchange": "NSI",
    }

    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 21, tzinfo=timezone.utc)
    infy_rows, tcs_rows = await asyncio.gather(
        provider.closes(infy_instrument, start=start, end=end),
        provider.closes(tcs_instrument, start=start, end=end),
    )
    assert infy_rows[0].instrument_id == INFY_UUID
    assert infy_rows[0].price == Decimal("1523.40")
    assert tcs_rows[0].instrument_id == other_uuid
    assert tcs_rows[0].price == Decimal("3900.00")


# 5 -- verified valid mapping continues to work (no ISIN on returned side,
# falls back to name similarity, exactly as the live path already does) ----
@pytest.mark.asyncio
async def test_05_verified_valid_mapping_without_returned_isin_still_works():
    info_no_isin = {"symbol": "INFY.NS", "longName": "Infosys Ltd", "exchange": "NSI", "currency": "INR"}
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(info_no_isin))
    rows = await provider.closes(
        _infy_instrument("INFY.NS"),
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].instrument_id == INFY_UUID


# 6 -- ETF behavior remains correct (no quote-type restriction in this gate,
# unlike live discovery's EQUITY/STOCK-only candidate filter) --------------
@pytest.mark.asyncio
async def test_06_etf_identity_verifies_via_isin_match():
    etf_uuid = uuid4()
    etf_instrument = {
        "globalInstrumentId": str(etf_uuid),
        "ticker": "NIFTYBEES",
        "structuredProviderTicker": "NIFTYBEES.NS",
        "currency": "INR",
        "isin": "INF204KB14I2",
        "companyName": "Nippon India ETF Nifty 50 BeES",
        "exchange": "NSI",
    }
    etf_info = {
        "symbol": "NIFTYBEES.NS", "longName": "Nippon India ETF Nifty 50 BeES",
        "exchange": "NSI", "currency": "INR", "isin": "INF204KB14I2", "quoteType": "ETF",
    }
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(etf_info, price="255.10"))
    rows = await provider.closes(
        etf_instrument,
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].instrument_id == etf_uuid


# 7 -- renamed/successor company: ISIN unchanged after a name change is
# still authoritative and is never overridden by a low name-similarity
# score, exactly as the live path's existing ISIN-priority rule already
# guarantees (this reuses that identical rule, not a new/stricter one) -----
@pytest.mark.asyncio
async def test_07_renamed_company_isin_still_authoritative_despite_name_drift():
    renamed_uuid = uuid4()
    renamed_instrument = {
        "globalInstrumentId": str(renamed_uuid),
        "ticker": "OLDNAME",
        "structuredProviderTicker": "NEWNAME.NS",
        "currency": "INR",
        "isin": "INE999Z01018",
        "companyName": "Old Legacy Industries Limited",
        "exchange": "NSI",
    }
    # Yahoo now returns the POST-RENAME company name under the same ISIN.
    renamed_info = {
        "symbol": "NEWNAME.NS", "longName": "Completely Rebranded Holdings Ltd",
        "exchange": "NSI", "currency": "INR", "isin": "INE999Z01018",
    }
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(renamed_info, price="88.20"))
    rows = await provider.closes(
        renamed_instrument,
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].instrument_id == renamed_uuid


# 8 -- an ISIN mismatch is rejected outright even if names happen to be
# similar (guards against a coincidentally-similar-named different company)
@pytest.mark.asyncio
async def test_08_isin_mismatch_rejected_even_with_similar_name():
    similar_name_info = {
        "symbol": "INFY.NS", "longName": "Infosys BPO Limited",  # similar name, different company/ISIN
        "exchange": "NSI", "currency": "INR", "isin": "INE999Z99999",
    }
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(similar_name_info, price="10.70"))
    with pytest.raises(HistoricalPriceIdentityConflict, match="ISIN_MISMATCH"):
        await provider.closes(
            _infy_instrument("INFY.NS"),
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
