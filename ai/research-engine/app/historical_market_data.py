"""Provider-neutral historical-close population, deliberately outside request paths."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from requests.exceptions import Timeout as RequestsTimeout
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

try:
    from curl_cffi.requests.exceptions import Timeout as CurlTimeout
except ImportError:
    CurlTimeout = RequestsTimeout

from app.models import MarketPriceObservation
from app.structured_market import (
    _nse_symbol_matches,
    _canonical_nse_etf_identity_reason,
    verify_historical_price_identity,
)
from app.yahoo_ticker_context import YahooSessionBackoffActive, YahooTickerContexts

logger = logging.getLogger(__name__)

MAX_REJECTED_CLOSE_DIAGNOSTICS = 32
REJECTED_CLOSE_REASONS = frozenset({
    "CLOSE_MISSING", "CLOSE_MALFORMED", "CLOSE_NAN", "CLOSE_INFINITE",
    "CLOSE_ZERO", "CLOSE_NEGATIVE",
})


def rejected_close_reason(value: Any) -> str | None:
    """Classify an invalid close without retaining its raw value."""
    if value is None:
        return "CLOSE_MISSING"
    try:
        close = Decimal(str(value))
    except Exception:
        return "CLOSE_MALFORMED"
    if close.is_nan():
        return "CLOSE_NAN"
    if close.is_infinite():
        return "CLOSE_INFINITE"
    if close == 0:
        return "CLOSE_ZERO"
    if close < 0:
        return "CLOSE_NEGATIVE"
    return None


def rejected_close_diagnostic(timestamp: Any, reason: str) -> dict[str, Any] | None:
    """Return a bounded-field diagnostic, never raw prices or timestamps.

    An aware UTC-midnight or Asia/Kolkata-midnight label has an explicit date
    under the existing NSE daily-label convention. This is not proof that a
    session occurred or that a provider candle was available. Other labels,
    including strings, naive datetimes and NaT, retain an unknown date.
    """
    if not isinstance(reason, str) or reason not in REJECTED_CLOSE_REASONS:
        return None
    day = None
    try:
        if (isinstance(timestamp, datetime) and timestamp.tzinfo is not None
                and timestamp.utcoffset() is not None and not timestamp.fold
                and getattr(timestamp, "nanosecond", 0) == 0):
            # fold=0 is also the default for an unresolved repeated wall time.
            # Resolve named pytz zones through ZoneInfo so both folds can be
            # checked even when that tzinfo object fixes one selected offset.
            zone_name = getattr(timestamp.tzinfo, "key", None) or getattr(timestamp.tzinfo, "zone", None)
            zone = ZoneInfo(zone_name) if isinstance(zone_name, str) else timestamp.tzinfo
            wall = timestamp.replace(tzinfo=zone)
            if wall.replace(fold=0).utcoffset() != wall.replace(fold=1).utcoffset():
                return {"trading_date": None, "date_status": "UNKNOWN", "reason": reason}
            for label in (timestamp.astimezone(timezone.utc),
                          timestamp.astimezone(ZoneInfo("Asia/Kolkata"))):
                if (label.hour == label.minute == label.second == label.microsecond == 0
                        and getattr(label, "nanosecond", 0) == 0):
                    day = label.date().isoformat()
                    break
    except Exception:
        # Optional diagnostics cannot change acceptance or error propagation.
        day = None
    return {"trading_date": day, "date_status": "KNOWN" if day else "UNKNOWN", "reason": reason}


def normalize_rejected_close_diagnostics(entries: Any) -> list[dict[str, Any]]:
    """Whitelist at most 32 structured entries; never promote unknown dates."""
    if not isinstance(entries, (list, tuple)):
        return []
    result = []
    for entry in entries:
        if len(result) >= MAX_REJECTED_CLOSE_DIAGNOSTICS:
            break
        if not isinstance(entry, dict):
            continue
        reason = entry.get("reason")
        if not isinstance(reason, str) or reason not in REJECTED_CLOSE_REASONS:
            continue
        day = None
        if entry.get("date_status") == "KNOWN" and isinstance(entry.get("trading_date"), str):
            raw_day = entry["trading_date"]
            try:
                parsed = date.fromisoformat(raw_day)
                if parsed.isoformat() == raw_day:
                    day = raw_day
            except ValueError:
                pass
        result.append({"trading_date": day, "date_status": "KNOWN" if day else "UNKNOWN", "reason": reason})
    return result


def rejected_close_diagnostics_report(windows_run):
    """Whitelist at most 32 entries across provider/window/instrument reports.

    Retain order and duplicates. Omitted counts include valid overflow and
    known rejected rows lacking usable metadata, including legacy windows.
    Contradictory or unavailable counters are explicit unknowns, never zero.
    All-legacy reports keep their original interface without new fields.
    """
    diagnostics, omitted, omitted_known, available = [], 0, True, False
    for window in windows_run:
        if not isinstance(window, dict):
            continue
        count = window.get('rejected_invalid_close_count')
        count_known = type(count) is int and count >= 0
        if not any(key in window for key in
                   ('rejected_close_diagnostics', 'rejected_close_diagnostics_omitted')):
            if count_known:
                omitted += count
            else:
                omitted_known = False
            continue
        available = True
        entries = window.get('rejected_close_diagnostics')
        valid_entries, retained_before = 0, len(diagnostics)
        malformed_entries = not isinstance(entries, (list, tuple))
        for entry in entries if isinstance(entries, (list, tuple)) else ():
            try:
                normalized = normalize_rejected_close_diagnostics([entry])
            except Exception:
                normalized = []
            if not normalized:
                malformed_entries = True
                continue
            valid_entries += 1  # Count before the cap, so overflow cannot disappear.
            if len(diagnostics) < MAX_REJECTED_CLOSE_DIAGNOSTICS:
                diagnostics.append(normalized[0])
        retained = len(diagnostics) - retained_before
        upstream = window.get('rejected_close_diagnostics_omitted', 0)
        upstream_known = type(upstream) is int and upstream >= 0
        if count_known:
            if (not upstream_known or valid_entries + upstream > count):
                omitted_known = False
            else:
                omitted += count - retained
        elif ('rejected_invalid_close_count' in window or not upstream_known or malformed_entries
              or 'rejected_close_diagnostics_omitted' not in window):
            omitted_known = False
        else:
            omitted += upstream + valid_entries - retained
    if not available:
        return {}
    return {'rejected_close_diagnostics': diagnostics,
            'rejected_close_diagnostics_omitted': omitted if omitted_known else None}


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


class HistoricalPriceIdentityConflict(HistoricalPriceProviderError):
    """Raised when the provider's returned data does not verify as belonging
    to the canonical instrument the acquisition was requested for. A stable,
    truthful technical-failure reason -- never silently downgraded to
    HISTORICAL_PRICE_PROVIDER_UNAVAILABLE, and never persisted."""
    pass


class HistoricalPricePersistenceError(RuntimeError):
    pass


def _history_provider_error(exc: Exception) -> HistoricalPriceProviderError:
    if isinstance(exc, YFRateLimitError):
        reason = "EXTERNAL_PROVIDER_RATE_LIMITED"
    elif isinstance(exc, (TimeoutError, RequestsTimeout, CurlTimeout, httpx.TimeoutException)):
        reason = "DOWNSTREAM_TIMEOUT"
    else:
        reason = f"HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:{type(exc).__name__}"
    return HistoricalPriceProviderError(reason)


def _sanitized_diagnostic_field(value: Any) -> str | None:
    """Best-effort, non-raising coercion of an optional diagnostic attribute
    into a short, safe-to-log string. Never raises; returns None for
    anything that is not a usable, non-empty string value."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:200]


def _historical_price_failure_diagnostic(
    exc: Exception,
    *,
    global_instrument_id: Any,
    ticker: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Builds a structured, sanitized diagnostic for a failed Yahoo
    historical-price request, without altering the failure code or
    exception propagation that `_history_provider_error` already produces.

    Only reads attributes yfinance's own exceptions may already carry
    (`yahoo_reason`, `debug_info`); never performs an additional Yahoo
    request and never includes raw response bodies, cookies, authorization
    headers, or request URLs.
    """
    yahoo_reason = _sanitized_diagnostic_field(getattr(exc, "yahoo_reason", None))
    debug_info = _sanitized_diagnostic_field(getattr(exc, "debug_info", None))
    if yahoo_reason is not None:
        failure_subtype = "YAHOO_EXPLICIT_CHART_ERROR"
    elif debug_info is not None:
        failure_subtype = "YAHOO_EMPTY_OR_MALFORMED_RESPONSE"
    else:
        failure_subtype = "UNCLASSIFIED_EXCEPTION"
    return {
        "globalInstrumentId": str(global_instrument_id),
        "ticker": ticker,
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "exceptionType": type(exc).__name__,
        "failureSubtype": failure_subtype,
        "yahooReason": yahoo_reason,
    }


def _etf_fallback_diagnostic(
    reason: str,
    *,
    expected_asset_type: Any,
    expected_symbol: Any,
    returned_symbol: Any,
    returned_quote_type: Any,
    returned_exchange: Any,
    returned_currency: Any,
    canonical_nse_etf_verified: bool = False,
) -> str | None:
    """Explain a rejected name fallback without deciding identity itself."""
    if reason != "COMPANY_NAME_MISMATCH":
        return None
    if str(expected_asset_type or "").strip().upper() != "ETF":
        return "NOT_APPLICABLE_NOT_AN_ETF"
    required = [("expectedSymbol", expected_symbol), ("returnedSymbol", returned_symbol),
                ("returnedExchange", returned_exchange), ("returnedCurrency", returned_currency)]
    if not canonical_nse_etf_verified:
        required.append(("returnedQuoteType", returned_quote_type))
    missing = [name for name, value in required if not value]
    if missing:
        return f"INELIGIBLE_MISSING_FIELDS:{','.join(missing)}"
    if not _nse_symbol_matches(expected_symbol, returned_symbol):
        return "ATTEMPTED_SYMBOL_MISMATCH"
    quote_type = str(returned_quote_type or "").strip().upper()
    if quote_type != "ETF" and not (canonical_nse_etf_verified and quote_type in {"", "EQUITY"}):
        return "ATTEMPTED_QUOTE_TYPE_MISMATCH"
    if canonical_nse_etf_verified:
        return "CANONICAL_NSE_ETF_NAME_CONFLICT"
    return "UNEXPECTED_STILL_REJECTED"


def _log_historical_price_failure_diagnostic(
    exc: Exception,
    *,
    global_instrument_id: Any,
    ticker: str,
    start: datetime,
    end: datetime,
) -> None:
    """Logs the sanitized failure diagnostic for `exc`. Never raises, and
    never alters or masks the caller's own exception propagation: any
    problem building or logging the diagnostic (including a malformed
    exception attribute that raises on access) is caught and discarded here,
    so callers can always proceed straight to raising the classified
    technical-failure error, unaffected by this function's outcome."""
    try:
        diagnostic = _historical_price_failure_diagnostic(
            exc,
            global_instrument_id=global_instrument_id,
            ticker=ticker,
            start=start,
            end=end,
        )
        logger.warning("historical_price_failure_diagnostic %s", diagnostic)
    except Exception:
        logger.warning(
            "historical_price_failure_diagnostic_unavailable exceptionType=%s",
            type(exc).__name__,
        )


class _ObservationList(list):
    """A plain list of observations that also reports how many provider rows were dropped.

    Diagnostics only: ``rejected_close_count`` counts returned rows whose Close was missing,
    NaN, infinite, zero or negative. Those rows are still rejected exactly as before.
    """

    def __init__(self, values=()):
        super().__init__(values)
        self.rejected_close_count = 0
        self.rejected_close_diagnostics = []
        self.rejected_close_diagnostics_omitted = 0

    def reject_close(self, timestamp: Any, reason: str) -> None:
        self.rejected_close_count += 1
        diagnostic = rejected_close_diagnostic(timestamp, reason)
        if diagnostic is not None and len(self.rejected_close_diagnostics) < MAX_REJECTED_CLOSE_DIAGNOSTICS:
            self.rejected_close_diagnostics.append(diagnostic)
        else:
            self.rejected_close_diagnostics_omitted += 1


class YahooHistoricalPriceProvider:
    """Existing structured-market Yahoo integration's historical-close adapter.

    It is invoked only by an explicit/scheduled population worker, never by a
    sector-performance request.
    """
    provider_name = "YAHOO_FINANCE"

    def __init__(self, ticker_factory: Any, *, ticker_contexts: YahooTickerContexts | None = None) -> None:
        self.ticker_factory = ticker_factory
        self.ticker_contexts = ticker_contexts or YahooTickerContexts(ticker_factory)
        self._shared_context = ticker_contexts is not None

    async def closes(self, instrument: dict[str, Any], *, start: datetime, end: datetime,
                     acquisition_context: object | None = None) -> list[MarketPriceObservation]:
        context = acquisition_context or (asyncio.current_task() if self._shared_context else None)
        return await asyncio.to_thread(self._closes, instrument, start, end, context)

    def _closes(self, instrument: dict[str, Any], start: datetime, end: datetime, context=None) -> list[MarketPriceObservation]:
        ticker = str(instrument.get("structuredProviderTicker") or instrument.get("ticker") or "").strip()
        if not ticker:
            return []
        try:
            return self._closes_locked(instrument, ticker, start, end, context)
        except YahooSessionBackoffActive as exc:
            # Sustained Yahoo session failure (see yahoo_ticker_context.py's
            # exponential backoff): fail fast, technical/retryable, WITHOUT
            # making any Yahoo request at all -- the actual retry-storm fix,
            # shared with the structured-snapshot path (same ticker_contexts).
            raise HistoricalPriceProviderError("EXTERNAL_PROVIDER_UNAVAILABLE:YAHOO_SESSION_BACKOFF_ACTIVE") from exc

    def _closes_locked(self, instrument: dict[str, Any], ticker: str, start: datetime, end: datetime, context=None) -> list[MarketPriceObservation]:
        with self.ticker_contexts.acquire(ticker, context) as access:
            logger.info("radar_acquisition_count operation=historical_series_live_acquisition provider=YAHOO_FINANCE count=1")
            yf_ticker = access.ticker
            try:
                # Per-call exception propagation: a failed Yahoo request must
                # never certify a successful empty range or prefix check.
                history = yf_ticker.history(
                    start=start.date(), end=end.date(), auto_adjust=False, raise_errors=True,
                )
            except YFPricesMissingError as exc:
                # A single verified-missing completed session (never a wider
                # range) may be a genuinely absent candle rather than a
                # technical failure. Confirm this only via one additional,
                # bounded Yahoo request that independently shows surrounding
                # sessions present and this exact date absent -- never by
                # trusting the original error's wording alone, never via a
                # retry of the same request, and never for a multi-day range.
                confirmed_absent = False
                if end.date() - start.date() == timedelta(days=1):
                    try:
                        confirmed_absent = self._confirmed_absent_session(yf_ticker, start.date())
                    except Exception:
                        # Any failure while attempting to confirm absence means
                        # absence is NOT confirmed -- fail closed below, same as
                        # if this disambiguation did not exist.
                        confirmed_absent = False
                if confirmed_absent:
                    # A confirmed-empty result must still pass the same
                    # verified-symbol identity gate as any other successful
                    # response -- an absence confirmed under the wrong
                    # instrument's identity is not evidence of anything.
                    self._verify_identity_or_raise(instrument, ticker, access)
                    logger.info(
                        "historical_price_confirmed_absent_session globalInstrumentId=%s ticker=%s date=%s",
                        instrument.get("globalInstrumentId"), ticker, start.date().isoformat(),
                    )
                    history = None
                else:
                    _log_historical_price_failure_diagnostic(
                        exc, global_instrument_id=instrument.get("globalInstrumentId"),
                        ticker=ticker, start=start, end=end,
                    )
                    raise _history_provider_error(exc) from exc
            except Exception as exc:
                _log_historical_price_failure_diagnostic(
                    exc, global_instrument_id=instrument.get("globalInstrumentId"),
                    ticker=ticker, start=start, end=end,
                )
                error = _history_provider_error(exc)
                if isinstance(exc, YFRateLimitError):
                    # A sustained Yahoo 429 needs the same "back off the
                    # shared session" response as PROVIDER_IDENTITY_UNAVAILABLE
                    # -- continuing to hammer a rate-limited session is the
                    # same retry-storm failure mode either way.
                    self.ticker_contexts.note_identity_metadata_unavailable()
                raise error from exc
            rows = list(history.iterrows()) if history is not None else []
            if rows:
                # Recheck canonical identity against the same returned info;
                # sharing a ticker never substitutes for verification.
                self._verify_identity_or_raise(instrument, ticker, access)
        out: list[MarketPriceObservation] = _ObservationList()
        for observed_at, row in rows:
            try:
                close_value = row["Close"]
            except KeyError:
                out.reject_close(observed_at, "CLOSE_MISSING")
                continue
            except Exception:
                out.reject_close(observed_at, "CLOSE_MALFORMED")
                continue
            try:
                close = Decimal(str(close_value))
            except Exception:
                out.reject_close(observed_at, "CLOSE_MISSING" if close_value is None else "CLOSE_MALFORMED")
                continue
            # Yahoo can include rows whose Close value is NaN. Decimal accepts
            # "nan", but ordering it raises decimal.InvalidOperation and would
            # discard every otherwise valid row returned for the instrument.
            if not close.is_finite() or close <= 0:
                out.reject_close(observed_at, rejected_close_reason(close))
                continue
            stamp = observed_at.to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            out.append(MarketPriceObservation(
                instrument_id=UUID(str(instrument["globalInstrumentId"])), observed_at=stamp,
                price=close, currency=instrument.get("currency"), provider=self.provider_name,
                source_url=f"https://finance.yahoo.com/quote/{ticker}/history?interval=1d&auto_adjust=false", retrieved_at=datetime.now(timezone.utc),
            ))
        return out

    def _confirmed_absent_session(self, yf_ticker: Any, missing_date) -> bool:
        """One bounded, non-retried additional Yahoo request issued only after
        an exact single-day request already raised YFPricesMissingError.
        Returns True only when this second request itself SUCCEEDS and its
        own returned rows independently show at least one session before and
        at least one session after `missing_date`, with `missing_date` itself
        absent from those rows. Any failure, any ambiguous result, or
        `missing_date` unexpectedly present all return False -- callers must
        then fall back to the existing fail-closed technical-failure path.
        Never retries this same request, never widens beyond a fixed small
        padding window, and never adjusts budgets, concurrency, or timeouts.
        """
        padding = timedelta(days=5)
        wide_start = missing_date - padding
        wide_end = missing_date + timedelta(days=1) + padding
        history = yf_ticker.history(
            start=wide_start, end=wide_end, auto_adjust=False, raise_errors=True,
        )
        if history is None or history.empty:
            return False
        observed_dates = {ts.to_pydatetime().date() for ts in history.index}
        if missing_date in observed_dates:
            return False
        has_before = any(d < missing_date for d in observed_dates)
        has_after = any(d > missing_date for d in observed_dates)
        return has_before and has_after

    def _verify_identity_or_raise(self, instrument: dict[str, Any], ticker: str, yf_ticker: Any) -> None:
        """Enforce: a Yahoo market observation for canonical instrument X must
        not be persisted unless the Yahoo symbol used for acquisition is
        currently and explicitly verified as belonging to X. Verified against
        what the provider actually RETURNS for this request, not merely a
        `mapping.status == VERIFIED` flag inherited from an external system.
        """
        try:
            info = yf_ticker.info or {}
        except Exception as exc:
            raise _history_provider_error(exc) from exc
        if not isinstance(info, dict):
            raise HistoricalPriceProviderError("HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:INVALID_INFO")
        expected_name = instrument.get("companyName") or instrument.get("canonicalName") or instrument.get("ticker") or ticker
        returned_name = info.get("longName") or info.get("shortName")
        etf = str(instrument.get("assetType") or "").strip().upper() == "ETF"
        canonical_reason = _canonical_nse_etf_identity_reason(instrument, ticker) if etf else None
        canonical_nse_etf_verified = etf and canonical_reason is None
        reason = verify_historical_price_identity(
            expected_isin=instrument.get("isin"),
            expected_name=expected_name,
            expected_exchange_family="XNSE",
            expected_currency=instrument.get("currency"),
            returned_isin=info.get("isin"),
            returned_name=returned_name,
            returned_exchange=info.get("exchange") or info.get("fullExchangeName"),
            returned_currency=info.get("currency"),
            # Canonical NSE verification is independent of Yahoo metadata;
            # it may resolve ETF type/name ambiguity, never explicit conflicts.
            expected_symbol=instrument.get("primarySymbol") or instrument.get("ticker"),
            expected_asset_type=instrument.get("assetType"),
            returned_symbol=info.get("symbol"),
            returned_quote_type=info.get("quoteType"),
            canonical_nse_etf_verified=canonical_nse_etf_verified,
        )
        if etf and canonical_reason and ("providerMappings" in instrument
                or canonical_reason.startswith("CANONICAL_IDENTITY_CONFLICT:")):
            reason = canonical_reason
        if (str(instrument.get("assetType") or "").strip().upper() == "ETF"
                and (expected_symbol := instrument.get("primarySymbol") or instrument.get("ticker"))
                and not _nse_symbol_matches(expected_symbol, ticker)):
            reason = "SYMBOL_MISMATCH"
        if reason == "PROVIDER_IDENTITY_UNAVAILABLE":
            # Yahoo returned literally no identity evidence for a resolved
            # ticker (every one of name/isin/exchange/currency absent) --
            # the production signature of a stale/invalid crumb on the
            # shared Yahoo session (concurrent HTTP 401 Invalid Crumb
            # observed in production logs), not a genuine wrong-company
            # mapping. A brand-new yfinance Ticker per acquisition context
            # (see YahooTickerContexts.acquire) does not help on its own,
            # since every Ticker still shares the same underlying session.
            # Report the signal so the session can be rebuilt, bounded by
            # its own cooldown -- never on every single occurrence, and
            # never for a persistent/permanent provider rejection that a
            # rebuilt session also fails. This never weakens the
            # fail-closed rejection below: the current acquisition still
            # raises and nothing is persisted either way.
            self.ticker_contexts.note_identity_metadata_unavailable()
        else:
            # Any other outcome -- verified, or a genuine but populated
            # mismatch -- proves Yahoo actually responded with real
            # identity evidence on this shared session, so a brief,
            # genuinely transient blip must not leave acquisition
            # throttled long after Yahoo has already recovered.
            self.ticker_contexts.note_identity_metadata_available()
        if reason is not None:
            # Log public identity fields and the applicable trust boundary,
            # without raw provider payloads or credentials.
            etf_fallback_diagnostic = _etf_fallback_diagnostic(
                reason, expected_asset_type=instrument.get("assetType"),
                expected_symbol=instrument.get("primarySymbol") or instrument.get("ticker"),
                returned_symbol=info.get("symbol"), returned_quote_type=info.get("quoteType"),
                returned_exchange=info.get("exchange") or info.get("fullExchangeName"),
                returned_currency=info.get("currency"),
                canonical_nse_etf_verified=canonical_nse_etf_verified,
            )
            logger.info(
                "historical_price_identity_rejected globalInstrumentId=%s ticker=%s reason=%s "
                "expectedName=%s returnedName=%s expectedSymbol=%s expectedAssetType=%s "
                "returnedSymbol=%s returnedQuoteType=%s returnedExchange=%s returnedCurrency=%s "
                "etfFallbackDiagnostic=%s",
                instrument.get("globalInstrumentId"), ticker, reason,
                _sanitized_diagnostic_field(expected_name), _sanitized_diagnostic_field(returned_name),
                _sanitized_diagnostic_field(instrument.get("primarySymbol") or instrument.get("ticker")),
                _sanitized_diagnostic_field(instrument.get("assetType")),
                _sanitized_diagnostic_field(info.get("symbol")),
                _sanitized_diagnostic_field(info.get("quoteType")),
                _sanitized_diagnostic_field(info.get("exchange") or info.get("fullExchangeName")),
                _sanitized_diagnostic_field(info.get("currency")),
                etf_fallback_diagnostic,
            )
            raise HistoricalPriceIdentityConflict(f"VERIFIED_HISTORICAL_IDENTITY_MISMATCH:{reason}")


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
            except HistoricalPriceIdentityConflict:
                # Truthful, stable technical-failure reason -- never
                # collapsed into the generic provider-unavailable label.
                raise
            except HistoricalPriceProviderError:
                raise
            except Exception as exc:
                raise HistoricalPriceProviderError("HISTORICAL_PRICE_PROVIDER_UNAVAILABLE") from exc
            for observation in observations:
                if observation.instrument_id != UUID(str(instrument['globalInstrumentId'])):
                    raise HistoricalPriceIdentityConflict('HISTORICAL_PRICE_INSTRUMENT_MISMATCH')
                if instrument.get('currency') and observation.currency != instrument['currency']:
                    raise HistoricalPriceIdentityConflict('HISTORICAL_PRICE_CURRENCY_MISMATCH')
                stamp = observation.observed_at
                day = stamp.date()
                if instrument.get('exchange') in {'NSE', 'XNSE'} and stamp.tzinfo is not None:
                    # Daily DATE labels may be UTC midnight or exchange-local
                    # midnight. Ignore provider over-delivery outside this range.
                    utc_stamp = stamp.astimezone(timezone.utc)
                    day = utc_stamp.date() if utc_stamp.hour == utc_stamp.minute == utc_stamp.second == 0 else stamp.astimezone(ZoneInfo('Asia/Kolkata')).date()
                if not start.date() <= day < end.date():
                    continue
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
