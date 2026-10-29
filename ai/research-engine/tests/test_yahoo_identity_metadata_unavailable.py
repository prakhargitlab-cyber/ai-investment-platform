"""Focused regression tests for the Yahoo metadata-unavailable / Equity
Radar retries fix.

Root cause (production K8s evidence): for multiple valid NSE equity
tickers, Yahoo's ``.info`` came back with every identity field
(name/symbol/quoteType/exchange/currency) simultaneously ``None``,
concurrently with Yahoo HTTP 401 Invalid Crumb responses. The existing
identity gate (``verify_identity_by_isin_or_name`` in
``app.structured_market``) conflated "no identity evidence returned at
all" with "a genuine, populated, wrong company name" -- both produced
``COMPANY_NAME_MISMATCH``. This file pins:

  1. Completely-missing identity metadata now reports a DISTINCT reason
     (``PROVIDER_IDENTITY_UNAVAILABLE``) and still fails closed.
  2. A genuine, populated, wrong company name is UNCHANGED
     (``COMPANY_NAME_MISMATCH``), still fails closed.
  3. A valid, independently-verified identity is unaffected and still
     verifies (no regression to the existing INFY-class scenarios).
  4. An HTTP 401 / provider-authorization failure that actually raises
     (rather than yfinance's own ``.info`` silently returning an empty
     dict -- the production-observed shape above) is still classified as
     a technical, retryable provider failure, never silently downgraded
     and never treated as a proven identity mismatch.
  5. The new bounded session-refresh signal
     (``YahooTickerContexts.note_identity_metadata_unavailable``) is
     backoff-bounded: repeated reports within the cooldown window trigger
     at most one session rebuild, never an unbounded refresh-and-retry
     loop; a fresh cooldown window allows exactly one more.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason
from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    HistoricalPriceProviderError,
    YahooHistoricalPriceProvider,
)
from app.structured_market import verify_historical_price_identity, verify_identity_by_isin_or_name
from app.yahoo_ticker_context import YahooTickerContexts


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
    def __init__(self, info, price="1523.40", info_exc=None):
        self._info = info
        self._price = price
        self._info_exc = info_exc

    @property
    def info(self):
        if self._info_exc is not None:
            raise self._info_exc
        return self._info

    def history(self, **_kwargs):
        return _history(self._price)


def _equity_instrument(structured_ticker: str = "INFY.NS") -> dict:
    return {
        "globalInstrumentId": str(uuid4()),
        "ticker": "INFY",
        "structuredProviderTicker": structured_ticker,
        "currency": "INR",
        "isin": "INE009A01021",
        "companyName": "Infosys Limited",
        "canonicalName": "Infosys Limited",
        "exchange": "NSI",
    }


# 1 -- completely empty Yahoo identity metadata ----------------------------

def test_verify_identity_by_isin_or_name_reports_distinct_reason_for_no_name():
    """Unit-level pin on the exact producer: no returned name at all is
    PROVIDER_IDENTITY_UNAVAILABLE, not COMPANY_NAME_MISMATCH."""
    assert verify_identity_by_isin_or_name(
        expected_isin=None, expected_name="Infosys Limited",
        returned_isin=None, returned_name=None,
    ) == "PROVIDER_IDENTITY_UNAVAILABLE"
    assert verify_identity_by_isin_or_name(
        expected_isin=None, expected_name="Infosys Limited",
        returned_isin=None, returned_name="",
    ) == "PROVIDER_IDENTITY_UNAVAILABLE"
    assert verify_identity_by_isin_or_name(
        expected_isin=None, expected_name="Infosys Limited",
        returned_isin=None, returned_name="   ",
    ) == "PROVIDER_IDENTITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_empty_yahoo_identity_metadata_fails_closed_with_distinct_reason():
    """Production shape: every identity field Yahoo returns is None
    (returnedName/returnedSymbol/returnedQuoteType/returnedExchange/
    returnedCurrency), coinciding with a session-level crumb failure that
    yfinance's own .info call swallows rather than raising. This must
    still reject -- never accept price history without verified identity
    -- but with PROVIDER_IDENTITY_UNAVAILABLE, not COMPANY_NAME_MISMATCH."""
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(
        {"longName": None, "symbol": None, "quoteType": None, "exchange": None, "currency": None},
    ))
    with pytest.raises(HistoricalPriceIdentityConflict,
                        match="VERIFIED_HISTORICAL_IDENTITY_MISMATCH:PROVIDER_IDENTITY_UNAVAILABLE"):
        await provider.closes(
            _equity_instrument(),
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )


# 2 -- genuine, populated, wrong company name is unchanged -----------------

@pytest.mark.asyncio
async def test_genuine_wrong_company_name_is_still_company_name_mismatch():
    # No returned ISIN here on purpose: this isolates the name-similarity
    # path. A conflicting returned ISIN takes priority and would raise
    # ISIN_MISMATCH instead (see test_02_infy_uuid_with_hcl_insys_ticker_is_
    # rejected in test_yahoo_canonical_identity_integrity.py for that case).
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(
        {"longName": "HCL Insystems Limited", "symbol": "HCL-INSYS.NS",
         "exchange": "NSI", "currency": "INR"},
        price="10.70",
    ))
    with pytest.raises(HistoricalPriceIdentityConflict,
                        match="VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH"):
        await provider.closes(
            _equity_instrument("HCL-INSYS.NS"),
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )


# 3 -- valid, independently-verified identity is unaffected ----------------

@pytest.mark.asyncio
async def test_valid_independently_verified_identity_still_passes():
    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(
        {"longName": "Infosys Limited", "symbol": "INFY.NS",
         "exchange": "NSI", "currency": "INR", "isin": "INE009A01021"},
    ))
    rows = await provider.closes(
        _equity_instrument("INFY.NS"),
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].price == Decimal("1523.40")


# 4 -- HTTP 401 / authorization failure that actually raises ---------------

@pytest.mark.asyncio
async def test_http_401_authorization_failure_is_technical_not_identity_mismatch():
    """When the provider/session failure DOES raise (rather than yfinance
    silently returning an empty .info dict), it must be classified as a
    technical provider failure -- never silently downgraded to a stable
    code, and never confused with a proven identity mismatch. The reason
    stays TECHNICAL_RETRYABLE either way, but the two failure MECHANISMS
    (raised exception vs. empty-but-not-raising response) must not be
    merged into the same code path."""
    class Unauthorized(Exception):
        pass

    provider = YahooHistoricalPriceProvider(lambda _symbol: _FakeTicker(
        {}, info_exc=Unauthorized("401 Client Error: Invalid Crumb"),
    ))
    with pytest.raises(HistoricalPriceProviderError,
                        match="HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:Unauthorized"):
        await provider.closes(
            _equity_instrument(),
            start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
    assert classify_reason("HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:Unauthorized") == TECHNICAL_RETRYABLE


def test_provider_identity_unavailable_classifies_as_technical_retryable_distinct_string():
    """failure_taxonomy must classify the new reason separately (a
    distinct string) from a proven identity mismatch, while preserving
    the existing durable retry-limit behavior: both stay
    TECHNICAL_RETRYABLE (neither is a PERMANENT_REASONS member), since
    this module's default is that an unproven absence-of-evidence failure
    must never be treated as terminal."""
    unavailable = "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:PROVIDER_IDENTITY_UNAVAILABLE"
    mismatch = "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:COMPANY_NAME_MISMATCH"
    assert unavailable != mismatch
    assert classify_reason(unavailable) == TECHNICAL_RETRYABLE
    assert classify_reason(mismatch) == TECHNICAL_RETRYABLE


# 5 -- retry/backoff is bounded, not unbounded ------------------------------

def test_session_refresh_signal_is_bounded_by_cooldown_not_retried_unboundedly():
    """note_identity_metadata_unavailable must rebuild the session at most
    once per cooldown window, however many callers report the failure
    within that window (a whole baseline cycle's worth of instruments
    hitting the identical stale crumb), and must allow exactly one more
    refresh once the cooldown has elapsed -- bounded backoff for a
    plausible transient session fault, never an unbounded retry loop."""
    refresh_calls = []
    contexts = YahooTickerContexts(
        lambda symbol: None, session_refresh=lambda: refresh_calls.append(1),
        session_refresh_cooldown_seconds=1000.0,
    )
    assert contexts.note_identity_metadata_unavailable() is True
    assert len(refresh_calls) == 1
    # A burst of further reports within the cooldown window must not
    # trigger additional refreshes.
    for _ in range(10):
        assert contexts.note_identity_metadata_unavailable() is False
    assert len(refresh_calls) == 1
    # Once the cooldown has elapsed, exactly one more refresh is allowed.
    contexts._last_session_refresh_monotonic -= 2000.0
    assert contexts.note_identity_metadata_unavailable() is True
    assert len(refresh_calls) == 2


def test_session_refresh_signal_is_a_noop_without_a_refresh_hook():
    """A caller-supplied ticker_factory (tests, or any explicit wiring)
    owns its own session lifecycle -- no hook means no refresh attempt,
    and the call must still be a safe, side-effect-free no-op."""
    contexts = YahooTickerContexts(lambda symbol: None)
    assert contexts.note_identity_metadata_unavailable() is False


def test_yahoo_finance_provider_wires_session_refresh_only_for_default_factory():
    from app.settings import Settings
    from app.structured_market import YahooFinanceProvider
    import httpx

    default_provider = YahooFinanceProvider(Settings(), httpx.AsyncClient())
    assert default_provider.ticker_contexts._session_refresh is not None
    before = default_provider._bounded_session
    default_provider.ticker_contexts._session_refresh()
    assert default_provider._bounded_session is not before

    explicit_provider = YahooFinanceProvider(
        Settings(), httpx.AsyncClient(), ticker_factory=lambda symbol: None,
    )
    assert explicit_provider.ticker_contexts._session_refresh is None
