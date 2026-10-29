"""Supplementary close history; canonical NSE identity remains authoritative.

Uses the existing Yahoo acquisition/identity verifier. Close-only observations
are persisted as such: open/high/low/volume are never synthesized.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from uuid import UUID

from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    HistoricalPriceProviderError,
    YahooHistoricalPriceProvider,
)
from app.models import DailyMarketBar


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
        if instrument.get("assetType") != "ETF":
            raise HistoricalPriceIdentityConflict("ETF_HISTORY_LISTING_NOT_VERIFIED")
        # One policy owns returned identifiers, canonical NSE ETF trust and
        # provider type ambiguity. A second, literal listing comparison would
        # reject valid results already verified by that shared policy.
        super()._verify_identity_or_raise(instrument, ticker, access)


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
        bars = []
        for observation in observations:
            if observation.instrument_id != key:
                raise HistoricalPriceIdentityConflict(
                    "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:OBSERVATION_INSTRUMENT_MISMATCH")
            if observation.currency != "INR":
                raise HistoricalPriceIdentityConflict(
                    "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:CURRENCY_MISMATCH")
            stamp = observation.observed_at
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise HistoricalPriceProviderError("ETF_HISTORY_INVALID_OBSERVATION:NAIVE_TIMESTAMP")
            if stamp > now:
                raise HistoricalPriceProviderError("ETF_HISTORY_INVALID_OBSERVATION:FUTURE_TIMESTAMP")
            if not start.date() <= stamp.date() < end_date:
                continue
            close = observation.price
            if not close.is_finite() or close <= 0:
                raise HistoricalPriceProviderError("ETF_HISTORY_INVALID_OBSERVATION:INVALID_CLOSE")
            bars.append(DailyMarketBar(global_instrument_id=key, trading_date=stamp.date(),
                close=close.quantize(Decimal('0.000000000001'), rounding=ROUND_HALF_EVEN),
                currency=observation.currency, provider=observation.provider, provider_symbol=ticker,
                source_mode='REAL', source_url=observation.source_url, retrieved_at=observation.retrieved_at))
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
