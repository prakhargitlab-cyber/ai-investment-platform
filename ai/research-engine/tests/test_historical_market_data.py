from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from yfinance.exceptions import YFPricesMissingError

from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    HistoricalPriceProviderError,
    HistoricalPricePopulationService,
    YahooHistoricalPriceProvider,
    _historical_price_failure_diagnostic,
    _sanitized_diagnostic_field,
)
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
        info = {"longName": "Infosys Limited", "exchange": "NSI", "currency": "INR"}

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
            "companyName": "Infosys Limited",
        },
        start=start,
        end=end,
    )

    assert observed["symbol"] == "INFY.NS"
    assert observed["history"] == {
        "start": start.date(),
        "end": end.date(),
        "auto_adjust": False,
        "raise_errors": True,
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
        info = {"longName": "KEC International Limited", "exchange": "NSI", "currency": "INR"}

        def history(self, **_kwargs):
            return History()

    rows = await YahooHistoricalPriceProvider(lambda _symbol: Ticker()).closes(
        {
            "globalInstrumentId": str(instrument_id),
            "structuredProviderTicker": "KEC.NS",
            "currency": "INR",
            "companyName": "KEC International Limited",
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


class _FakeYahooMissingPricesError(Exception):
    """Local stand-in for yfinance's YFPricesMissingError shape (carries the
    same optional yahoo_reason/debug_info attributes) so these tests do not
    depend on the vendored library's internals."""

    def __init__(self, yahoo_reason=None, debug_info=None):
        super().__init__("no price data found")
        self.yahoo_reason = yahoo_reason
        self.debug_info = debug_info


def _failing_provider(ticker_factory):
    return YahooHistoricalPriceProvider(ticker_factory)


@pytest.mark.asyncio
async def test_yahoo_adapter_logs_sanitized_diagnostic_when_yahoo_reason_present(caplog):
    instrument_id = uuid4()
    exc = _FakeYahooMissingPricesError(yahoo_reason="No data found, symbol may be delisted")

    class Ticker:
        def history(self, **kwargs):
            raise exc

    provider = _failing_provider(lambda symbol: Ticker())
    start = datetime(2025, 8, 4, tzinfo=timezone.utc)
    end = datetime(2025, 9, 9, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        with pytest.raises(HistoricalPriceProviderError) as excinfo:
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS"},
                start=start, end=end,
            )

    # Failure code and exception chaining are unchanged by the added diagnostic.
    assert str(excinfo.value) == "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:_FakeYahooMissingPricesError"
    assert excinfo.value.__cause__ is exc

    [record] = [r for r in caplog.records if r.message.startswith("historical_price_failure_diagnostic")]
    diagnostic = record.args
    assert diagnostic["globalInstrumentId"] == str(instrument_id)
    assert diagnostic["ticker"] == "PGIL.NS"
    assert diagnostic["start"] == "2025-08-04"
    assert diagnostic["end"] == "2025-09-09"
    assert diagnostic["exceptionType"] == "_FakeYahooMissingPricesError"
    assert diagnostic["failureSubtype"] == "YAHOO_EXPLICIT_CHART_ERROR"
    assert diagnostic["yahooReason"] == "No data found, symbol may be delisted"
    # Never log raw provider response bodies/cookies/headers/URLs.
    assert "source_url" not in diagnostic and "cookie" not in str(diagnostic).lower()


@pytest.mark.asyncio
async def test_yahoo_adapter_classifies_malformed_response_when_only_debug_info_present(caplog):
    instrument_id = uuid4()
    exc = _FakeYahooMissingPricesError(yahoo_reason=None, debug_info="chart.result is empty")

    class Ticker:
        def history(self, **kwargs):
            raise exc

    provider = _failing_provider(lambda symbol: Ticker())
    start = datetime(2025, 8, 4, tzinfo=timezone.utc)
    end = datetime(2025, 9, 9, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        with pytest.raises(HistoricalPriceProviderError):
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS"},
                start=start, end=end,
            )

    [record] = [r for r in caplog.records if r.message.startswith("historical_price_failure_diagnostic")]
    diagnostic = record.args
    assert diagnostic["failureSubtype"] == "YAHOO_EMPTY_OR_MALFORMED_RESPONSE"
    assert diagnostic["yahooReason"] is None


@pytest.mark.asyncio
async def test_yahoo_adapter_classifies_unclassified_when_neither_attribute_present(caplog):
    instrument_id = uuid4()
    exc = TimeoutError("connect timed out")

    class Ticker:
        def history(self, **kwargs):
            raise exc

    provider = _failing_provider(lambda symbol: Ticker())
    start = datetime(2025, 8, 4, tzinfo=timezone.utc)
    end = datetime(2025, 9, 9, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        with pytest.raises(HistoricalPriceProviderError) as excinfo:
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS"},
                start=start, end=end,
            )

    # DOWNSTREAM_TIMEOUT classification in _history_provider_error is unchanged.
    assert str(excinfo.value) == "DOWNSTREAM_TIMEOUT"

    [record] = [r for r in caplog.records if r.message.startswith("historical_price_failure_diagnostic")]
    diagnostic = record.args
    assert diagnostic["failureSubtype"] == "UNCLASSIFIED_EXCEPTION"
    assert diagnostic["yahooReason"] is None


@pytest.mark.asyncio
async def test_yahoo_adapter_diagnostic_drops_malformed_non_string_fields(caplog):
    instrument_id = uuid4()
    # Malformed/unexpected attribute shapes (e.g. a dict instead of a str)
    # must be dropped safely rather than crashing the diagnostic path.
    exc = _FakeYahooMissingPricesError(yahoo_reason={"unexpected": "shape"}, debug_info=12345)

    class Ticker:
        def history(self, **kwargs):
            raise exc

    provider = _failing_provider(lambda symbol: Ticker())
    start = datetime(2025, 8, 4, tzinfo=timezone.utc)
    end = datetime(2025, 9, 9, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        with pytest.raises(HistoricalPriceProviderError):
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS"},
                start=start, end=end,
            )

    [record] = [r for r in caplog.records if r.message.startswith("historical_price_failure_diagnostic")]
    diagnostic = record.args
    assert diagnostic["yahooReason"] is None
    assert diagnostic["failureSubtype"] == "UNCLASSIFIED_EXCEPTION"


def test_sanitized_diagnostic_field_truncates_and_rejects_non_strings():
    assert _sanitized_diagnostic_field("  No data found  ") == "No data found"
    assert _sanitized_diagnostic_field("") is None
    assert _sanitized_diagnostic_field("   ") is None
    assert _sanitized_diagnostic_field(None) is None
    assert _sanitized_diagnostic_field(42) is None
    assert _sanitized_diagnostic_field({"a": 1}) is None
    long_value = "x" * 500
    assert _sanitized_diagnostic_field(long_value) == "x" * 200


def test_historical_price_failure_diagnostic_prefers_yahoo_reason_over_debug_info():
    instrument_id = uuid4()
    exc = _FakeYahooMissingPricesError(yahoo_reason="delisted", debug_info="also present")
    diagnostic = _historical_price_failure_diagnostic(
        exc,
        global_instrument_id=instrument_id,
        ticker="PGIL.NS",
        start=datetime(2025, 8, 4, tzinfo=timezone.utc),
        end=datetime(2025, 9, 9, tzinfo=timezone.utc),
    )
    assert diagnostic["failureSubtype"] == "YAHOO_EXPLICIT_CHART_ERROR"
    assert diagnostic["yahooReason"] == "delisted"


@pytest.mark.asyncio
async def test_yahoo_adapter_diagnostic_cannot_mask_original_failure_when_attribute_raises(caplog):
    """A malformed exception whose yahoo_reason is a property that itself
    raises (not AttributeError) must never alter, suppress, or replace the
    original failure's classification or exception chain."""
    instrument_id = uuid4()

    class _RaisingAttributeError(Exception):
        @property
        def yahoo_reason(self):
            raise RuntimeError("boom: attribute access exploded")

    exc = _RaisingAttributeError("no price data found")

    class Ticker:
        def history(self, **kwargs):
            raise exc

    provider = _failing_provider(lambda symbol: Ticker())
    start = datetime(2025, 8, 4, tzinfo=timezone.utc)
    end = datetime(2025, 9, 9, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        with pytest.raises(HistoricalPriceProviderError) as excinfo:
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS"},
                start=start, end=end,
            )

    # The original failure's classification and exception chain are exactly
    # what _history_provider_error would have produced with no diagnostic
    # logic present at all -- unaltered by the raising attribute.
    assert str(excinfo.value) == "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:_RaisingAttributeError"
    assert excinfo.value.__cause__ is exc

    # A safe fallback diagnostic is still logged, naming only the exception
    # type (never a guessed, more-specific subtype it could not justify).
    fallback = [r for r in caplog.records if r.message.startswith("historical_price_failure_diagnostic_unavailable")]
    assert len(fallback) == 1
    assert fallback[0].args == ("_RaisingAttributeError",)


class _WideHistoryFixture:
    """Minimal stand-in for a yfinance history() DataFrame result: `.empty`
    plus an `.index` of Timestamp-like objects with `.to_pydatetime()`, which
    is all `_confirmed_absent_session` reads."""

    def __init__(self, observed_dates):
        self._timestamps = [_FixtureTimestamp(datetime(d.year, d.month, d.day, tzinfo=timezone.utc)) for d in observed_dates]

    @property
    def empty(self):
        return not self._timestamps

    @property
    def index(self):
        return self._timestamps

    def iterrows(self):
        return []


class _FixtureTimestamp:
    def __init__(self, value):
        self._value = value

    def to_pydatetime(self):
        return self._value


@pytest.mark.asyncio
async def test_yahoo_adapter_confirms_absent_candle_for_verified_nse_special_session(caplog):
    """PGIL-style NSE SPECIAL_SESSION missing candle: the exact Feb 1 request
    raises YFPricesMissingError; a second, bounded surrounding-date request
    independently shows Jan 29-30 and Feb 2-4 present with Feb 1 absent. The
    acquisition must report a valid empty result -- never raise, never
    fabricate a price."""
    instrument_id = uuid4()
    calls = []
    exact_exc = YFPricesMissingError("PGIL.NS", "no debug info")
    surrounding_dates = [date(2026, 1, 29), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4)]

    class Ticker:
        # A confirmed-absent result still passes through the identity gate,
        # so this fixture's info must match the requested instrument.
        info = {"longName": "Procter & Gamble Industries Limited", "exchange": "NSI", "currency": "INR"}

        def history(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise exact_exc
            return _WideHistoryFixture(surrounding_dates)

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end = datetime(2026, 2, 2, tzinfo=timezone.utc)

    with caplog.at_level("INFO"):
        rows = await provider.closes(
            {
                "globalInstrumentId": str(instrument_id),
                "structuredProviderTicker": "PGIL.NS",
                "companyName": "Procter & Gamble Industries Limited",
                "currency": "INR",
            },
            start=start, end=end,
        )

    assert rows == []
    assert len(calls) == 2
    assert calls[0] == {"start": start.date(), "end": end.date(), "auto_adjust": False, "raise_errors": True}
    # Exactly one additional, bounded request -- never unbounded, never retried.
    assert calls[1]["start"] == date(2026, 1, 27)
    assert calls[1]["end"] == date(2026, 2, 7)
    assert calls[1]["auto_adjust"] is False
    assert calls[1]["raise_errors"] is True
    confirmations = [r for r in caplog.records if r.message.startswith("historical_price_confirmed_absent_session")]
    assert len(confirmations) == 1
    assert "PGIL.NS" in confirmations[0].getMessage()
    assert "2026-02-01" in confirmations[0].getMessage()
    # No technical-failure diagnostic was logged for a confirmed absence.
    assert not [r for r in caplog.records if r.message.startswith("historical_price_failure_diagnostic")]


def test_confirmed_absent_session_true_when_surrounding_sessions_present_and_target_missing():
    """Unit coverage of the disambiguation helper itself: surrounding candles
    returned successfully, exact target date absent from them."""

    class Ticker:
        def history(self, **kwargs):
            return _WideHistoryFixture([date(2026, 1, 30), date(2026, 2, 3)])

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    assert provider._confirmed_absent_session(Ticker(), date(2026, 2, 1)) is True


def test_confirmed_absent_session_false_when_target_date_unexpectedly_present():
    """If the wider, bounded request actually contains the supposedly-missing
    date, absence is not confirmed -- callers must fail closed."""

    class Ticker:
        def history(self, **kwargs):
            return _WideHistoryFixture([date(2026, 1, 30), date(2026, 2, 1), date(2026, 2, 3)])

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    assert provider._confirmed_absent_session(Ticker(), date(2026, 2, 1)) is False


@pytest.mark.asyncio
async def test_yahoo_adapter_keeps_genuine_timeout_as_technical_failure(caplog):
    """A genuine Yahoo timeout (not YFPricesMissingError) must never trigger
    the absence-confirmation path -- no second request, unchanged
    classification and exception chain."""
    instrument_id = uuid4()
    calls = []
    exc = TimeoutError("connect timed out")

    class Ticker:
        def history(self, **kwargs):
            calls.append(kwargs)
            raise exc

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end = datetime(2026, 2, 2, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        with pytest.raises(HistoricalPriceProviderError) as excinfo:
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS"},
                start=start, end=end,
            )

    assert str(excinfo.value) == "DOWNSTREAM_TIMEOUT"
    assert excinfo.value.__cause__ is exc
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_yahoo_adapter_fails_closed_when_absence_cannot_be_independently_confirmed(caplog):
    """A malformed/failing surrounding-date response must never be treated as
    confirmation: the original YFPricesMissingError is preserved exactly as a
    technical failure, same code and exception chain as if this
    disambiguation did not exist."""
    instrument_id = uuid4()
    calls = []
    exact_exc = YFPricesMissingError("PGIL.NS", "no debug info")

    class Ticker:
        def history(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise exact_exc
            # The wider, bounded confirmation request itself fails -- absence
            # is not confirmed, and this must not be retried or widened further.
            raise YFPricesMissingError("PGIL.NS", "no debug info")

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end = datetime(2026, 2, 2, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        with pytest.raises(HistoricalPriceProviderError) as excinfo:
            await provider.closes(
                {"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS"},
                start=start, end=end,
            )

    assert str(excinfo.value) == "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:YFPricesMissingError"
    assert excinfo.value.__cause__ is exact_exc
    assert len(calls) == 2
    [record] = [r for r in caplog.records if r.message.startswith("historical_price_failure_diagnostic")]
    diagnostic = record.args
    assert diagnostic["exceptionType"] == "YFPricesMissingError"


@pytest.mark.asyncio
async def test_yahoo_adapter_identity_mismatch_remains_a_hard_failure_alongside_absence_confirmation():
    """The verified-symbol identity gate is untouched by the absence
    disambiguation: a successfully-returned row with a mismatched identity
    must still raise HistoricalPriceIdentityConflict, never a provider-
    unavailable or confirmed-absence outcome."""
    instrument_id = uuid4()
    observed_at = datetime(2026, 2, 2, tzinfo=timezone.utc)

    class Timestamp:
        def to_pydatetime(self):
            return observed_at

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": "100.00"})]

    class Ticker:
        info = {"longName": "A Totally Different Company", "exchange": "NSI", "currency": "INR"}

        def history(self, **kwargs):
            return History()

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)

    with pytest.raises(HistoricalPriceIdentityConflict):
        await provider.closes(
            {
                "globalInstrumentId": str(instrument_id),
                "structuredProviderTicker": "PGIL.NS",
                "companyName": "Procter & Gamble Industries Limited",
                "currency": "INR",
            },
            start=start, end=end,
        )


@pytest.mark.asyncio
async def test_yahoo_adapter_identity_rejection_logs_the_compared_fund_names(caplog):
    """Regression for the full-universe ETF investigation (COMPANY_NAME_
    MISMATCH diagnosability): historical_price_identity_rejected previously
    logged only globalInstrumentId/ticker/reason, giving no way to tell a
    genuine wrong-company mapping apart from the provider simply returning a
    differently-formatted (or absent) name for the SAME correct fund. The
    log line must now also carry the two compared name strings, unchanged
    in meaning -- this is diagnostic-only and must not affect what raises,
    what is matched, or what is persisted."""
    instrument_id = uuid4()
    observed_at = datetime(2026, 2, 2, tzinfo=timezone.utc)

    class Timestamp:
        def to_pydatetime(self):
            return observed_at

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": "100.00"})]

    class Ticker:
        info = {"longName": "NIFTY 50", "exchange": "NSI", "currency": "INR"}

        def history(self, **kwargs):
            return History()

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)

    with caplog.at_level("INFO"):
        with pytest.raises(HistoricalPriceIdentityConflict):
            await provider.closes(
                {
                    "globalInstrumentId": str(instrument_id),
                    "structuredProviderTicker": "NIFTYBEES.NS",
                    "companyName": "Nippon India ETF Nifty 50 BeES",
                    "currency": "INR",
                },
                start=start, end=end,
            )
    [record] = [r for r in caplog.records if r.message.startswith("historical_price_identity_rejected")]
    assert "reason=COMPANY_NAME_MISMATCH" in record.message
    assert "expectedName=Nippon India ETF Nifty 50 BeES" in record.message
    assert "returnedName=NIFTY 50" in record.message


@pytest.mark.asyncio
async def test_yahoo_adapter_logs_etf_fallback_fields_on_company_name_mismatch(caplog):
    """Diagnostic-only regression (ETF-radar historical-acquisition-
    completeness investigation): a COMPANY_NAME_MISMATCH rejection must log
    the exact fields the ETF-specific fallback (verify_historical_price_
    identity's expected_symbol/expected_asset_type/returned_symbol/
    returned_quote_type/returned_exchange/returned_currency) compared, so a
    rejection where the fallback never had a chance to apply (e.g. Yahoo
    omitted quoteType here) is distinguishable in logs from one where it
    ran but the fields genuinely disagreed. Must not change what raises or
    what is persisted."""
    instrument_id = uuid4()
    observed_at = datetime(2026, 2, 2, tzinfo=timezone.utc)

    class Timestamp:
        def to_pydatetime(self):
            return observed_at

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": "100.00"})]

    class Ticker:
        # No quoteType/symbol returned at all -- the ETF fallback cannot
        # apply, and the log must show that plainly (empty/None fields)
        # rather than silently omitting them.
        info = {"longName": "NIFTY 50", "exchange": "NSI", "currency": "INR"}

        def history(self, **kwargs):
            return History()

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)

    with caplog.at_level("INFO"):
        with pytest.raises(HistoricalPriceIdentityConflict):
            await provider.closes(
                {
                    "globalInstrumentId": str(instrument_id),
                    "structuredProviderTicker": "NIFTYBEES.NS",
                    "companyName": "Nippon India ETF Nifty 50 BeES",
                    "primarySymbol": "NIFTYBEES",
                    "assetType": "ETF",
                    "currency": "INR",
                },
                start=start, end=end,
            )
    [record] = [r for r in caplog.records if r.message.startswith("historical_price_identity_rejected")]
    assert "expectedSymbol=NIFTYBEES" in record.message
    assert "expectedAssetType=ETF" in record.message
    # Yahoo returned no symbol/quoteType here -- the log must show that
    # absence plainly (empty), proving the fallback never had a chance to
    # apply, rather than silently omitting the fields.
    assert "returnedSymbol=" in record.message
    assert "returnedQuoteType=" in record.message


@pytest.mark.asyncio
async def test_confirmed_absent_candle_is_not_persisted_or_counted_as_coverage(tmp_path):
    """A confirmed-absent result (empty list, no exception) must flow through
    HistoricalPricePopulationService.populate() exactly like any other
    legitimately empty range: zero written, no persistence call, no error --
    never counted as price evidence or historical coverage."""
    instrument_id = uuid4()

    class ConfirmedAbsentProvider:
        provider_name = "YAHOO_FINANCE"

        async def closes(self, _instrument, *, start, end, acquisition_context=None):
            return []

    persistence = SqliteResearchPersistence(str(tmp_path / "confirmed_absent.db"))
    service = HistoricalPricePopulationService(persistence, ConfirmedAbsentProvider())

    written = await service.populate(
        [{"globalInstrumentId": str(instrument_id), "structuredProviderTicker": "PGIL.NS", "currency": "INR"}],
        start=datetime(2026, 2, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 2, tzinfo=timezone.utc),
    )

    assert written == 0


@pytest.mark.asyncio
async def test_yahoo_adapter_valid_candles_process_unchanged_by_absence_confirmation():
    """A normal successful request (no exception at all) is completely
    unaffected by the new YFPricesMissingError branch -- exactly one Yahoo
    call, rows converted exactly as before."""
    instrument_id = uuid4()
    observed_at = datetime(2026, 2, 2, tzinfo=timezone.utc)
    calls = []

    class Timestamp:
        def to_pydatetime(self):
            return observed_at

    class History:
        def iterrows(self):
            return [(Timestamp(), {"Close": "456.70"})]

    class Ticker:
        info = {"longName": "Procter & Gamble Industries Limited", "exchange": "NSI", "currency": "INR"}

        def history(self, **kwargs):
            calls.append(kwargs)
            return History()

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 1, 29, tzinfo=timezone.utc)
    end = datetime(2026, 2, 5, tzinfo=timezone.utc)

    rows = await provider.closes(
        {
            "globalInstrumentId": str(instrument_id),
            "structuredProviderTicker": "PGIL.NS",
            "companyName": "Procter & Gamble Industries Limited",
            "currency": "INR",
        },
        start=start, end=end,
    )

    assert len(calls) == 1
    assert len(rows) == 1
    assert rows[0].price == Decimal("456.70")


@pytest.mark.asyncio
async def test_confirmed_absent_session_identity_mismatch_is_not_accepted_as_empty_coverage():
    """A confirmed-absent result must still pass the verified-symbol identity
    gate: when the wider, bounded confirmation request independently shows
    the target date absent but the ticker's own identity does not match the
    requested instrument, this must raise HistoricalPriceIdentityConflict --
    never be accepted as a successful, confirmed empty result."""
    instrument_id = uuid4()
    calls = []
    exact_exc = YFPricesMissingError("PGIL.NS", "no debug info")
    surrounding_dates = [date(2026, 1, 30), date(2026, 2, 3)]

    class Ticker:
        info = {"longName": "A Totally Different Company", "exchange": "NSI", "currency": "INR"}

        def history(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise exact_exc
            return _WideHistoryFixture(surrounding_dates)

    provider = YahooHistoricalPriceProvider(lambda symbol: Ticker())
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end = datetime(2026, 2, 2, tzinfo=timezone.utc)

    with pytest.raises(HistoricalPriceIdentityConflict):
        await provider.closes(
            {
                "globalInstrumentId": str(instrument_id),
                "structuredProviderTicker": "PGIL.NS",
                "companyName": "Procter & Gamble Industries Limited",
                "currency": "INR",
            },
            start=start, end=end,
        )

    # Both the exact-date request and the one bounded confirmation request
    # ran -- absence was independently confirmed -- but identity still gates
    # the outcome, exactly as it would for any other successful response.
    assert len(calls) == 2
