"""Supplementary close history; canonical NSE identity remains authoritative.

Uses the existing Yahoo acquisition/identity verifier. Close-only observations
are persisted as such: open/high/low/volume are never synthesized.
"""
import asyncio
from datetime import timedelta, time, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from urllib.parse import unquote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from app.historical_market_data import (
    HistoricalPriceIdentityConflict,
    HistoricalPriceProviderError,
    YahooHistoricalPriceProvider,
    MAX_REJECTED_CLOSE_DIAGNOSTICS,
    rejected_close_diagnostic,
    rejected_close_diagnostics_report,
    rejected_close_reason,
)
from app.cycle_checkpoint import CycleOwnershipLost
from app.models import DailyMarketBar, MarketPriceObservation


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


# Five years of daily history is the onboarding target (366 days/year absorbs leap years). It is only a ceiling:
# callers clamp to the listing date and the provider returns nothing before an ETF existed.
ETF_HISTORY_TARGET_DAYS = 5 * 366
# One provider request covers at most this many calendar days; longer gaps are fetched as sequential windows.
ETF_HISTORY_WINDOW_DAYS = 731


def _invalid_close(price):
    """True for a missing, NaN, infinite, zero or negative close (diagnostic count only)."""
    try:
        return price is None or not price.is_finite() or price <= 0
    except (AttributeError, TypeError, ArithmeticError):
        return True


def _recovery_session_bar(observation, *, key, ticker, start, end_date, now, validation_now,
                          schedules, exceptions):
    """Validate Yahoo evidence at the recovery persistence boundary.

    Yahoo's exchange-midnight labels and older UTC-midnight labels describe
    session dates, not the instant a close was executed. Require the persisted
    calendar and a retrieval after that session closed; never invent sessions.
    """
    from app.market_sessions import session_bounds
    from app.session_history import history_symbol

    if getattr(observation, 'source_mode', 'REAL') != 'REAL':
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:NON_REAL_HISTORY')
    try:
        observation = MarketPriceObservation.model_validate(observation.model_dump())
    except (ValueError, TypeError, AttributeError) as exc:
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:MALFORMED_OBSERVATION') from exc
    if observation.instrument_id != key:
        raise HistoricalPriceIdentityConflict(
            'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:OBSERVATION_INSTRUMENT_MISMATCH')
    if observation.currency != 'INR':
        raise HistoricalPriceIdentityConflict('VERIFIED_HISTORICAL_IDENTITY_MISMATCH:CURRENCY_MISMATCH')
    if observation.provider != 'YAHOO_FINANCE':
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:UNEXPECTED_PROVIDER')
    try:
        source = urlsplit(observation.source_url)
        source_valid = (source.scheme == 'https' and source.hostname == 'finance.yahoo.com'
                        and source.username is None and source.password is None
                        and source.port in (None, 443) and not source.fragment
                        and unquote(source.path) == f'/quote/{ticker}/history'
                        and history_symbol(observation) == ticker)
    except ValueError:
        source_valid = False
    if not source_valid:
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:UNVERIFIED_HISTORY_SOURCE')
    stamp, retrieved = observation.observed_at, observation.retrieved_at
    if any(value.tzinfo is None or value.utcoffset() is None for value in (stamp, retrieved)):
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:NAIVE_TIMESTAMP')
    if stamp > now or retrieved > validation_now:
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:FUTURE_TIMESTAMP')
    utc, local = stamp.astimezone(timezone.utc), stamp.astimezone(ZoneInfo('Asia/Kolkata'))
    day = utc.date() if utc.time() == time.min else local.date()
    if not start.date() <= day < end_date:
        return None
    bounds = session_bounds('NSE', schedules, exceptions, day)
    if bounds is None:
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:NOT_A_CONFIGURED_TRADING_SESSION')
    date_label = utc.time() == time.min or local.time() == time.min
    if (bounds[1] > now or retrieved < bounds[1]
            or (not date_label and abs((stamp - bounds[1]).total_seconds()) > 60)):
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:UNCOMPLETED_SESSION_OBSERVATION')
    close = observation.price
    if not close.is_finite() or close <= 0:
        raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:INVALID_CLOSE')
    return DailyMarketBar(global_instrument_id=key, trading_date=day,
        close=close.quantize(Decimal('0.000000000001'), rounding=ROUND_HALF_EVEN),
        currency='INR', provider='YAHOO_FINANCE', provider_symbol=ticker,
        source_mode='REAL', source_url=observation.source_url, retrieved_at=retrieved)


def _insert_recovery_bars(store, bars):
    """Use the existing connection/table; a recovery can never update a row.

    Conflict handling is enforced by the database, including concurrent writers,
    rather than by a read-before-write check that could race another acquisition.
    """
    inserted = []
    for bar in bars:
        cursor = store._connection.execute(
            'INSERT INTO global_daily_market_bars '
            '(global_instrument_id,trading_date,close_price,currency,provider,provider_symbol,'
            'source_mode,source_url,retrieved_at) VALUES (?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(global_instrument_id,trading_date,provider) DO NOTHING',
            (str(bar.global_instrument_id), bar.trading_date.isoformat(), str(bar.close), bar.currency,
             bar.provider, bar.provider_symbol, str(bar.source_mode), bar.source_url,
             bar.retrieved_at.astimezone(timezone.utc).isoformat()))
        if store._cycle_rowcount(cursor) == 1:
            inserted.append(bar)
    return inserted


async def acquire_etf_history(repository, row, *, now, checkpoint, provider=None, start=None, end=None,
                              recovery_mode=False, retrieval_clock=None):
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
    start = start if start is not None else now-timedelta(days=ETF_HISTORY_TARGET_DAYS)
    end = end if end is not None else now
    rejected_closes = 0
    rejected_entries, rejected_omitted = [], 0

    def rejection_report():
        return {'rejected_invalid_close_count': rejected_closes,
                'rejected_close_diagnostics': rejected_entries,
                'rejected_close_diagnostics_omitted': rejected_omitted}

    try:
        if recovery_mode:
            from app.structured_market import _canonical_nse_etf_identity_reason
            reason = _canonical_nse_etf_identity_reason(row, ticker)
            if reason:
                raise HistoricalPriceIdentityConflict('VERIFIED_HISTORICAL_IDENTITY_MISMATCH:' + reason)
            if any(value.tzinfo is None or value.utcoffset() is None for value in (now, start, end)):
                raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_RANGE:NAIVE_TIMESTAMP')
            if start >= end:
                raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_RANGE:EMPTY_OR_REVERSED')

            def calendar():
                store = repository.persistence
                return (store.load_market_schedules({'NSE', 'XNSE'}),
                        store.load_market_calendar_exceptions({'NSE', 'XNSE'}))

            schedules, exceptions = await repository._run_blocking_persistence(calendar)
            if not schedules:
                raise HistoricalPriceProviderError('HISTORICAL_SESSION_CALENDAR_UNAVAILABLE')
        observations = await provider.closes({**row, 'structuredProviderTicker': ticker},
            start=start, end=end)
        if recovery_mode:
            # Diagnostics only: provider-dropped rows plus any returned row with an unusable close.
            provider_rejections = int(getattr(observations, 'rejected_close_count', 0))
            boundary_rejections = [item for item in observations if _invalid_close(getattr(item, 'price', None))]
            rejected_closes = provider_rejections + len(boundary_rejections)
            try:
                details = rejected_close_diagnostics_report([{
                    'rejected_invalid_close_count': provider_rejections,
                    'rejected_close_diagnostics': getattr(observations, 'rejected_close_diagnostics', []),
                    'rejected_close_diagnostics_omitted': getattr(observations, 'rejected_close_diagnostics_omitted', 0),
                }])
                rejected_entries = details['rejected_close_diagnostics']
                rejected_omitted = details['rejected_close_diagnostics_omitted']
            except Exception:
                # Optional metadata must not turn acceptable bars into a
                # technical failure. Preserve the existing authoritative count.
                rejected_entries, rejected_omitted = [], provider_rejections
            for item in boundary_rejections:
                reason = rejected_close_reason(getattr(item, 'price', None))
                entry = rejected_close_diagnostic(getattr(item, 'observed_at', None), reason)
                if entry is not None and len(rejected_entries) < MAX_REJECTED_CLOSE_DIAGNOSTICS:
                    rejected_entries.append(entry)
                elif rejected_omitted is not None:
                    rejected_omitted += 1
        validation_now = retrieval_clock() if recovery_mode and retrieval_clock is not None else now
        if recovery_mode and (validation_now.tzinfo is None or validation_now.utcoffset() is None
                              or validation_now < now):
            raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_RETRIEVAL_CLOCK')
        bars = []
        recovery_dates = {}
        for observation in observations:
            if recovery_mode:
                bar = _recovery_session_bar(observation, key=key, ticker=ticker, start=start,
                    end_date=end_date, now=now, validation_now=validation_now,
                    schedules=schedules, exceptions=exceptions)
                if bar is None:
                    continue
                existing = recovery_dates.get(bar.trading_date)
                if existing is not None and existing.close != bar.close:
                    raise HistoricalPriceProviderError('ETF_HISTORY_INVALID_OBSERVATION:CONFLICTING_PROVIDER_SESSION')
                if existing is None or bar.retrieved_at > existing.retrieved_at:
                    recovery_dates[bar.trading_date] = bar
                continue
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
        if recovery_mode:
            bars = [recovery_dates[day] for day in sorted(recovery_dates)]
    except (CycleOwnershipLost, asyncio.CancelledError):
        raise
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
                'reason': str(exc), 'observations': 0,
                **(rejection_report() if recovery_mode else {})}
    except Exception as exc:
        return {'status': 'TECHNICAL_FAILURE', 'provider': 'YAHOO_FINANCE',
                'reason': type(exc).__name__, 'observations': 0,
                **(rejection_report() if recovery_mode else {})}
    # Fenced transaction uses the existing bar table, never a parallel ETF store.
    def persist():
        with repository._persistence_worker_lock:
            store = repository.persistence
            with store._connection:
                def assert_owner():
                    cursor = store._connection.execute(
                        "UPDATE global_opportunity_cycle_run SET updated_at=updated_at "
                        "WHERE cycle_id=? AND owner_id=? AND status='RUNNING' "
                        f"AND lease_expires_at > {store._lease_clock_sql()}",
                        (checkpoint.cycle_id, checkpoint.owner_id))
                    if store._cycle_rowcount(cursor) != 1:
                        raise CycleOwnershipLost(checkpoint.cycle_id)
                assert_owner()
                if recovery_mode:
                    inserted = _insert_recovery_bars(store, bars)
                    assert_owner()  # Expiry/cancellation cannot certify a completed write.
                    return inserted
                store.upsert_daily_market_bars(bars)
                return bars
    accepted_provider_count = len(bars)
    accepted_first = min((b.trading_date for b in bars), default=None)
    accepted_last = max((b.trading_date for b in bars), default=None)
    persisted = await repository._run_blocking_persistence(persist)
    if recovery_mode:
        reused = len(bars) - len(persisted)
        bars = persisted
    return {'status': 'SUCCESS_WITH_DATA' if bars else 'SUCCESS_EMPTY', 'provider': 'YAHOO_FINANCE',
            'observations': len(bars), 'return_convention': 'UNADJUSTED_PRICE_RETURN',
            'price_normalization': 'NUMERIC_38_12_HALF_EVEN',
            'first_date': str(min(b.trading_date for b in bars)) if bars else None,
            'last_date': str(max(b.trading_date for b in bars)) if bars else None,
            **({'preserved_conflicts': reused, 'write_policy': 'INSERT_ONLY',
                'provider_status': 'SUCCESS_WITH_DATA' if accepted_provider_count else 'SUCCESS_EMPTY',
                'accepted_provider_bar_count': accepted_provider_count,
                'accepted_provider_first_date': accepted_first.isoformat() if accepted_first else None,
                'accepted_provider_last_date': accepted_last.isoformat() if accepted_last else None,
                **rejection_report(),
                'persisted_bar_count': len(bars)} if recovery_mode else {})}
