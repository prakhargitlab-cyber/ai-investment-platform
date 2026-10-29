"""Supplementary close history; canonical NSE identity remains authoritative.

Uses the existing Yahoo acquisition/identity verifier. Close-only observations
are persisted as such: open/high/low/volume are never synthesized.
"""
import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from uuid import UUID

from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    HistoricalPriceProviderError,
    YahooHistoricalPriceProvider,
    _sanitized_diagnostic_field,
)
from app.models import DailyMarketBar

logger = logging.getLogger(__name__)


def _field_check_status(returned, ok: bool) -> str:
    """Read-only MISSING/CONFLICTING/MATCHED classification of one compared
    field, for logging only. `ok` is the caller's own already-computed
    pass/fail boolean for that field -- this function never re-derives or
    second-guesses it, only explains it: MISSING when the provider simply
    did not return a usable value at all, CONFLICTING when it returned one
    that disagreed, MATCHED otherwise. Distinguishing the two matters
    because they point at different causes: an omitted field is an
    incomplete-provider-metadata question, a disagreeing one is a genuine-
    identity-conflict (or overly-strict-comparison) question.
    """
    if ok:
        return "MATCHED"
    if returned is None or (isinstance(returned, str) and not returned.strip()):
        return "MISSING"
    return "CONFLICTING"


class EtfHistoryProvider(YahooHistoricalPriceProvider):
    def _verify_identity_or_raise(self, instrument, ticker, access):
        super()._verify_identity_or_raise(instrument, ticker, access)
        info = access.info or {}
        returned_symbol = info.get('symbol')
        returned_quote_type = info.get('quoteType')
        returned_currency = info.get('currency')
        returned_exchange = info.get('exchange')
        symbol_ok = returned_symbol == ticker
        quote_type_ok = returned_quote_type == 'ETF'
        currency_ok = returned_currency == 'INR'
        exchange_ok = returned_exchange in {'NSI', 'NSE'}
        if not (symbol_ok and quote_type_ok and currency_ok and exchange_ok):
            # Diagnostic only (no behavior change): the prior raise site gave
            # no indication of WHICH of the four compared fields actually
            # failed, leaving this ETF-specific second gate (distinct from,
            # and stricter than, the shared verify_historical_price_identity
            # fallback already logged via historical_price_identity_rejected)
            # completely unobservable -- exactly the gap blocking root-cause
            # attribution for the ETF_HISTORY_LISTING_NOT_VERIFIED share of
            # ETF_ONBOARD technical failures. Logging the actual returned
            # values (not the request-side ticker), plus an explicit per-
            # field MISSING/CONFLICTING/MATCHED breakdown (symbolCheck/
            # quoteTypeCheck/currencyCheck/exchangeCheck), lets a future
            # cycle's logs show directly, e.g., whether Yahoo omitted
            # exchange entirely (MISSING) versus returned a genuinely
            # different one (CONFLICTING), or whether the symbol comparison
            # failed because Yahoo echoed it in a different case/suffix
            # form (CONFLICTING, not MISSING) -- without changing what is
            # verified or persisted. NOTE: this raise condition compares
            # returned_symbol against `ticker` (which includes the ".NS"
            # suffix) verbatim, unlike the shared fallback in
            # verify_historical_price_identity, which deliberately compares
            # via _bare_symbol() (ignoring the ".NS" suffix) specifically
            # because Yahoo does not always echo that suffix back. Whether
            # that difference is this gate's actual root cause for the
            # ETF_HISTORY_LISTING_NOT_VERIFIED failures is NOT confirmed by
            # any observed Yahoo response available in this investigation
            # (no live provider call was made); this diagnostic is added so
            # the next real cycle's logs can confirm or rule it out before
            # any comparison logic here is changed.
            logger.info(
                "etf_history_listing_not_verified globalInstrumentId=%s expectedTicker=%s "
                "returnedSymbol=%s returnedQuoteType=%s returnedCurrency=%s returnedExchange=%s "
                "symbolCheck=%s quoteTypeCheck=%s currencyCheck=%s exchangeCheck=%s",
                instrument.get("globalInstrumentId"), ticker,
                _sanitized_diagnostic_field(returned_symbol), _sanitized_diagnostic_field(returned_quote_type),
                _sanitized_diagnostic_field(returned_currency), _sanitized_diagnostic_field(returned_exchange),
                _field_check_status(returned_symbol, symbol_ok), _field_check_status(returned_quote_type, quote_type_ok),
                _field_check_status(returned_currency, currency_ok), _field_check_status(returned_exchange, exchange_ok),
            )
            raise HistoricalPriceIdentityConflict('ETF_HISTORY_LISTING_NOT_VERIFIED')


async def acquire_etf_history(repository, row, *, now, checkpoint, provider=None, start=None, end=None):
    """One sequential bounded request per instrument; retry occurs in a new cycle.

    A ticker constructed from the verified NSE symbol is only a lookup candidate.
    It cannot pass persistence without returned identity/type/exchange validation.
    """
    if provider is None:
        import yfinance as yf
        # Shares the same fix as app.structured_market
        # ._build_bounded_yahoo_session (see its docstring): yfinance's own
        # per-request default timeout (30s, hardcoded in
        # yfinance.data.YfData.get/.post) is otherwise unbounded from this
        # module's perspective -- a single slow/rate-limited instrument's
        # .closes()/.info call pair could silently run well past any
        # caller-side expectation with no independent bound of its own.
        # Capping it at the same structured_provider_timeout_seconds budget
        # used elsewhere in this codebase for Yahoo acquisitions makes a
        # stalled call fail fast with a genuine, truthful TECHNICAL_FAILURE
        # instead of hanging the sequential ETF_ONBOARD pass indefinitely.
        # This reuses the SAME factory (not a requests.Session subclass) so
        # it keeps whichever backend yfinance actually prefers (curl_cffi
        # with Chrome impersonation, when installed) instead of silently
        # downgrading to a plain requests session and losing yfinance's own
        # anti-bot-detection fingerprinting.
        from app.settings import Settings
        from app.structured_market import _build_bounded_yahoo_session
        bounded_session = _build_bounded_yahoo_session(Settings().structured_provider_timeout_seconds)
        provider = EtfHistoryProvider(lambda symbol: yf.Ticker(symbol, session=bounded_session))
    key = UUID(str(row['globalInstrumentId']))
    ticker = row['primarySymbol'] + '.NS'
    end_date = end.date() if end is not None else now.date() + timedelta(days=1)
    start = start if start is not None else now-timedelta(days=400)
    end = end if end is not None else now
    try:
        observations = await provider.closes({**row, 'structuredProviderTicker': ticker},
            start=start, end=end)
        bars = [DailyMarketBar(global_instrument_id=key, trading_date=o.observed_at.date(),
                close=o.price.quantize(Decimal('0.000000000001'), rounding=ROUND_HALF_EVEN),
                currency=o.currency, provider=o.provider, provider_symbol=ticker,
                source_mode='REAL', source_url=o.source_url, retrieved_at=o.retrieved_at)
                for o in observations if o.instrument_id == key and o.currency == 'INR'
                and start.date() <= o.observed_at.date() < end_date]
    except HistoricalPriceProviderError as exc:
        # Root cause (ETF_ONBOARD 352-failure investigation, source-
        # specific failure classification): collapsing every exception
        # down to type(exc).__name__ discarded the actual identity-gate
        # reason (EXCHANGE_MISMATCH / CURRENCY_MISMATCH / ISIN_MISMATCH /
        # COMPANY_NAME_MISMATCH -- see verify_historical_price_identity)
        # behind the uniformly uninformative label
        # 'HistoricalPriceIdentityConflict', making a genuine, specific
        # identity conflict indistinguishable from any other technical
        # failure. HistoricalPriceProviderError/HistoricalPriceIdentity
        # Conflict already carry a stable, specific message (see their
        # raise sites); preserve it instead of discarding it. This does
        # NOT change retryability/state classification (still
        # TECHNICAL_FAILURE/RETRYABLE_FAILURE, exactly as before) -- only
        # the diagnostic reason text is more truthful.
        return {'status': 'TECHNICAL_FAILURE', 'provider': 'YAHOO_FINANCE',
                'reason': str(exc), 'observations': 0}
    except Exception as exc:
        return {'status': 'TECHNICAL_FAILURE', 'provider': 'YAHOO_FINANCE',
                'reason': type(exc).__name__, 'observations': 0}
    # Fenced transaction uses the existing bar table, never a parallel ETF store.
    def persist():
        with repository._persistence_worker_lock:
            store = repository.persistence
            with store._connection:
                cursor = store._connection.execute(
                    "UPDATE global_opportunity_cycle_run SET updated_at=updated_at "
                    "WHERE cycle_id=? AND owner_id=? AND status='RUNNING' "
                    f"AND lease_expires_at > {store._lease_clock_sql()}",
                    (checkpoint.cycle_id, checkpoint.owner_id))
                if store._cycle_rowcount(cursor) != 1:
                    from app.cycle_checkpoint import CycleOwnershipLost
                    raise CycleOwnershipLost(checkpoint.cycle_id)
                store.upsert_daily_market_bars(bars)
    await repository._run_blocking_persistence(persist)
    return {'status': 'SUCCESS_WITH_DATA' if bars else 'SUCCESS_EMPTY', 'provider': 'YAHOO_FINANCE',
            'observations': len(bars), 'return_convention': 'UNADJUSTED_PRICE_RETURN',
            'price_normalization': 'NUMERIC_38_12_HALF_EVEN',
            'first_date': str(min(b.trading_date for b in bars)) if bars else None,
            'last_date': str(max(b.trading_date for b in bars)) if bars else None}
