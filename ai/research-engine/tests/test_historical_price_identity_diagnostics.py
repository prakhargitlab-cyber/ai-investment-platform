"""Focused tests for the diagnostic-attribution-only additions made during
the ETF_HISTORY_LISTING_NOT_VERIFIED / COMPANY_NAME_MISMATCH root-cause
investigation. No comparison/raise logic was changed anywhere in this
change: every test here asserts that the NEW diagnostic fields correctly
classify an outcome that the EXISTING (unchanged) gates already produced,
never a new accept/reject decision.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.etf_history import EtfHistoryProvider, _field_check_status
from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    YahooHistoricalPriceProvider,
    _etf_fallback_diagnostic,
)


# ---------------------------------------------------------------------------
# _etf_fallback_diagnostic (shared COMPANY_NAME_MISMATCH gate, historical_market_data.py)
# ---------------------------------------------------------------------------

def test_fallback_diagnostic_none_when_reason_is_not_company_name_mismatch():
    """ISIN/EXCHANGE/CURRENCY mismatches never reach the ETF fallback at
    all (verify_historical_price_identity returns before consulting it) --
    the diagnostic must stay empty for them, not report an eligibility
    verdict for logic that never ran."""
    for reason in ("ISIN_MISMATCH", "EXCHANGE_MISMATCH", "CURRENCY_MISMATCH"):
        assert _etf_fallback_diagnostic(
            reason, expected_asset_type="ETF", expected_symbol="NIFTYBEES",
            returned_symbol="NIFTYBEES.NS", returned_quote_type="ETF",
            returned_exchange="NSI", returned_currency="INR",
        ) is None


def test_fallback_diagnostic_not_applicable_for_non_etf():
    assert _etf_fallback_diagnostic(
        "COMPANY_NAME_MISMATCH", expected_asset_type="EQUITY", expected_symbol="INFY",
        returned_symbol="INFY", returned_quote_type="EQUITY",
        returned_exchange="NSI", returned_currency="INR",
    ) == "NOT_APPLICABLE_NOT_AN_ETF"


def test_fallback_diagnostic_ineligible_missing_fields_lists_which_ones():
    result = _etf_fallback_diagnostic(
        "COMPANY_NAME_MISMATCH", expected_asset_type="ETF", expected_symbol="NIFTYBEES",
        returned_symbol=None, returned_quote_type=None,
        returned_exchange="NSI", returned_currency="INR",
    )
    assert result.startswith("INELIGIBLE_MISSING_FIELDS:")
    assert "returnedSymbol" in result and "returnedQuoteType" in result
    assert "returnedExchange" not in result and "returnedCurrency" not in result


def test_fallback_diagnostic_attempted_symbol_mismatch_when_bare_symbols_disagree():
    assert _etf_fallback_diagnostic(
        "COMPANY_NAME_MISMATCH", expected_asset_type="ETF", expected_symbol="NIFTYBEES",
        returned_symbol="GOLDBEES.NS", returned_quote_type="ETF",
        returned_exchange="NSI", returned_currency="INR",
    ) == "ATTEMPTED_SYMBOL_MISMATCH"


def test_fallback_diagnostic_tolerates_ns_suffix_difference_without_flagging_mismatch():
    """The fallback itself (verify_historical_price_identity) ignores a
    ".NS" suffix difference via _bare_symbol -- the diagnostic must agree
    with that, not report a symbol mismatch for a suffix-only difference."""
    result = _etf_fallback_diagnostic(
        "COMPANY_NAME_MISMATCH", expected_asset_type="ETF", expected_symbol="NIFTYBEES",
        returned_symbol="NIFTYBEES", returned_quote_type="EQUITY",  # quoteType disagrees instead
        returned_exchange="NSI", returned_currency="INR",
    )
    assert result == "ATTEMPTED_QUOTE_TYPE_MISMATCH"


def test_fallback_diagnostic_attempted_quote_type_mismatch():
    assert _etf_fallback_diagnostic(
        "COMPANY_NAME_MISMATCH", expected_asset_type="ETF", expected_symbol="NIFTYBEES",
        returned_symbol="NIFTYBEES.NS", returned_quote_type="EQUITY",
        returned_exchange="NSI", returned_currency="INR",
    ) == "ATTEMPTED_QUOTE_TYPE_MISMATCH"


# ---------------------------------------------------------------------------
# _field_check_status (ETF_HISTORY_LISTING_NOT_VERIFIED gate, etf_history.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("returned,ok,expected", [
    ("NIFTYBEES.NS", True, "MATCHED"),
    (None, False, "MISSING"),
    ("", False, "MISSING"),
    ("   ", False, "MISSING"),
    ("GOLDBEES.NS", False, "CONFLICTING"),
])
def test_field_check_status_classification(returned, ok, expected):
    assert _field_check_status(returned, ok) == expected


# ---------------------------------------------------------------------------
# End-to-end: EtfHistoryProvider's own extra gate logs the new breakdown
# without changing the raise condition.
# ---------------------------------------------------------------------------

def _make_history_ticker(info):
    class Timestamp:
        def to_pydatetime(self):
            return datetime(2026, 2, 2, tzinfo=timezone.utc)

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": "100.00"})]

    class Ticker:
        pass

    Ticker.info = info

    def history(self, **kwargs):
        return History()

    Ticker.history = history
    return Ticker()


@pytest.mark.asyncio
async def test_etf_history_gate_logs_missing_exchange(caplog):
    """Yahoo returning no exchange at all (None) must classify as MISSING,
    not CONFLICTING -- an omission is a different root-cause question
    (incomplete provider metadata) than a disagreement (genuine conflict
    or an overly strict comparison). ISIN matches at the shared base gate
    so the base gate passes cleanly and etf_history's own extra gate is
    actually reached."""
    instrument_id = uuid4()
    ticker_obj = _make_history_ticker({
        "symbol": "NIFTYBEES.NS", "quoteType": "ETF", "currency": "INR",
        "exchange": None,  # missing
        "longName": "Nippon India ETF Nifty 50 BeES", "isin": "INF204K01VQ2",
    })
    provider = EtfHistoryProvider(lambda symbol: ticker_obj)
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)

    with caplog.at_level("INFO"):
        with pytest.raises(HistoricalPriceIdentityConflict, match="ETF_HISTORY_LISTING_NOT_VERIFIED"):
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "NIFTYBEES.NS",
                 "companyName": "Nippon India ETF Nifty 50 BeES", "primarySymbol": "NIFTYBEES",
                 "assetType": "ETF", "currency": "INR", "isin": "INF204K01VQ2"},
                start=start, end=end,
            )
    [record] = [r for r in caplog.records if r.message.startswith("etf_history_listing_not_verified")]
    assert "symbolCheck=MATCHED" in record.message
    assert "quoteTypeCheck=MATCHED" in record.message
    assert "currencyCheck=MATCHED" in record.message
    assert "exchangeCheck=MISSING" in record.message


@pytest.mark.asyncio
async def test_etf_history_gate_logs_conflicting_exchange_even_when_shared_gate_normalizes_it(caplog):
    """"XNSE" is an exchange string the SHARED base gate's own normalization
    (_exchange_family) already recognizes as equivalent to NSE/NSI -- so the
    base gate passes cleanly -- but it is not literally "NSI" or "NSE", so
    etf_history's own un-normalized membership check still rejects it. This
    must classify as CONFLICTING (a present, disagreeing value), not
    MISSING, and is the concrete evidence for this investigation's leading
    root-cause hypothesis: this extra gate's comparison is stricter than,
    and inconsistent with, the shared normalization used everywhere else in
    this codebase for the exact same field."""
    instrument_id = uuid4()
    ticker_obj = _make_history_ticker({
        "symbol": "NIFTYBEES.NS", "quoteType": "ETF", "currency": "INR",
        "exchange": "XNSE",  # present, base-gate-equivalent, but not in etf_history's literal set
        "longName": "Nippon India ETF Nifty 50 BeES", "isin": "INF204K01VQ2",
    })
    provider = EtfHistoryProvider(lambda symbol: ticker_obj)
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)

    with caplog.at_level("INFO"):
        with pytest.raises(HistoricalPriceIdentityConflict, match="ETF_HISTORY_LISTING_NOT_VERIFIED"):
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "NIFTYBEES.NS",
                 "companyName": "Nippon India ETF Nifty 50 BeES", "primarySymbol": "NIFTYBEES",
                 "assetType": "ETF", "currency": "INR", "isin": "INF204K01VQ2"},
                start=start, end=end,
            )
    [record] = [r for r in caplog.records if r.message.startswith("etf_history_listing_not_verified")]
    assert "exchangeCheck=CONFLICTING" in record.message


@pytest.mark.asyncio
async def test_etf_history_gate_verified_matching_identity_does_not_raise():
    """A fully matching response (exact suffixed symbol, ETF, INR, NSI) must
    pass cleanly -- this is the control case proving the new diagnostics
    never fire or interfere when nothing is wrong."""
    instrument_id = uuid4()
    ticker_obj = _make_history_ticker({
        "symbol": "NIFTYBEES.NS", "quoteType": "ETF", "currency": "INR", "exchange": "NSI",
        "longName": "Nippon India ETF Nifty 50 BeES", "isin": "INF204K01VQ2",
    })
    provider = EtfHistoryProvider(lambda symbol: ticker_obj)
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)

    observations = await provider.closes(
        {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "NIFTYBEES.NS",
         "companyName": "Nippon India ETF Nifty 50 BeES", "primarySymbol": "NIFTYBEES",
         "assetType": "ETF", "currency": "INR", "isin": "INF204K01VQ2"},
        start=start, end=end,
    )
    assert len(observations) == 1


@pytest.mark.asyncio
async def test_etf_history_gate_wrong_asset_type_still_fails_closed():
    instrument_id = uuid4()
    ticker_obj = _make_history_ticker({
        "symbol": "NIFTYBEES.NS", "quoteType": "EQUITY", "currency": "INR", "exchange": "NSI",
        "longName": "Nippon India ETF Nifty 50 BeES",
    })
    provider = EtfHistoryProvider(lambda symbol: ticker_obj)
    start, end = datetime(2026, 2, 2, tzinfo=timezone.utc), datetime(2026, 2, 3, tzinfo=timezone.utc)
    with pytest.raises(HistoricalPriceIdentityConflict, match="ETF_HISTORY_LISTING_NOT_VERIFIED"):
        await provider.closes(
            {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "NIFTYBEES.NS",
             "companyName": "Nippon India ETF Nifty 50 BeES", "primarySymbol": "NIFTYBEES",
             "assetType": "ETF", "currency": "INR"},
            start=start, end=end,
        )


@pytest.mark.asyncio
async def test_etf_history_gate_isin_conflict_fails_closed_at_shared_gate_first():
    """A genuine ISIN conflict must be rejected by the shared base-class
    gate (VERIFIED_HISTORICAL_IDENTITY_MISMATCH:ISIN_MISMATCH) before the
    ETF-specific extra gate is ever reached -- fail-closed, unaffected by
    any diagnostic addition."""
    instrument_id = uuid4()
    ticker_obj = _make_history_ticker({
        "symbol": "NIFTYBEES.NS", "quoteType": "ETF", "currency": "INR", "exchange": "NSI",
        "longName": "Nippon India ETF Nifty 50 BeES", "isin": "INF000000000",
    })
    provider = EtfHistoryProvider(lambda symbol: ticker_obj)
    start, end = datetime(2026, 2, 2, tzinfo=timezone.utc), datetime(2026, 2, 3, tzinfo=timezone.utc)
    with pytest.raises(HistoricalPriceIdentityConflict, match="ISIN_MISMATCH"):
        await provider.closes(
            {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "NIFTYBEES.NS",
             "companyName": "Nippon India ETF Nifty 50 BeES", "primarySymbol": "NIFTYBEES",
             "assetType": "ETF", "currency": "INR", "isin": "INF204K01VQ2"},
            start=start, end=end,
        )


@pytest.mark.asyncio
async def test_provider_alias_without_independent_verification_is_ineligible_for_fallback(caplog):
    """A COMPANY_NAME_MISMATCH where Yahoo omitted quoteType/exchange/
    currency entirely (an unverifiable "alias" response) must be logged as
    ineligible for the ETF fallback, not silently treated the same as a
    genuine attempted-and-disagreed case."""
    instrument_id = uuid4()
    provider = YahooHistoricalPriceProvider(lambda symbol: _make_history_ticker({
        "longName": "NIFTY 50",  # Yahoo's benchmark-index alias name, not the fund's own name
    }))
    start, end = datetime(2026, 2, 2, tzinfo=timezone.utc), datetime(2026, 2, 3, tzinfo=timezone.utc)
    with caplog.at_level("INFO"):
        with pytest.raises(HistoricalPriceIdentityConflict, match="COMPANY_NAME_MISMATCH"):
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "NIFTYBEES.NS",
                 "companyName": "Nippon India ETF Nifty 50 BeES", "primarySymbol": "NIFTYBEES",
                 "assetType": "ETF", "currency": "INR"},
                start=start, end=end,
            )
    [record] = [r for r in caplog.records if r.message.startswith("historical_price_identity_rejected")]
    assert "etfFallbackDiagnostic=INELIGIBLE_MISSING_FIELDS" in record.message


@pytest.mark.asyncio
async def test_provider_alias_with_independent_verification_passes_via_fallback():
    """The same benchmark-style alias name, but THIS time Yahoo also
    returns a verifying symbol/quoteType/exchange/currency -- the fallback
    must accept it (three independent signals agreeing), exactly as the
    existing, unchanged fallback logic already does."""
    instrument_id = uuid4()
    provider = YahooHistoricalPriceProvider(lambda symbol: _make_history_ticker({
        "longName": "NIFTY 50", "symbol": "NIFTYBEES.NS", "quoteType": "ETF",
        "exchange": "NSI", "currency": "INR",
    }))
    start, end = datetime(2026, 2, 2, tzinfo=timezone.utc), datetime(2026, 2, 3, tzinfo=timezone.utc)
    observations = await provider.closes(
        {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "NIFTYBEES.NS",
         "companyName": "Nippon India ETF Nifty 50 BeES", "primarySymbol": "NIFTYBEES",
         "assetType": "ETF", "currency": "INR"},
        start=start, end=end,
    )
    assert len(observations) == 1
