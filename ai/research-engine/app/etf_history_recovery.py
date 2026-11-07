"""Explicit, bounded history repair. Never scores, publishes or runs a universe.

The completed source cycle is immutable. A separate operational checkpoint uses
an isolated market slot so the normal ETF worker cannot resume this as a cycle.
Only missing completed sessions in an explicitly approved recent range are read
from Yahoo. Older failures remain unresolved, not certified as absent history.
"""
from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.cycle_checkpoint import CandidateState, CycleCheckpoint, CycleOwnershipLost
from app.etf_full_universe import BudgetClient
from app.etf_history import ETF_HISTORY_WINDOW_DAYS, EtfHistoryProvider, acquire_etf_history
from app.etf_history_selection import HISTORY_PROVIDERS, select_etf_history_bars
from app.historical_market_data import (
    rejected_close_diagnostics_report,
)
from app.market_sessions import completed_sessions, unresolved_session_dates
from app.session_history import history_symbol, missing_history_ranges
from app.structured_market import _canonical_nse_etf_identity_reason

RECOVERY_SLOT = 'ETF_HISTORY_RECOVERY'
MAX_INSTRUMENTS = 5
MAX_PROVIDER_WINDOWS = 15
MAX_HTTP_REQUESTS = 60
# Additive, order-stable explanation labels for HISTORICAL_RANGE_INCOMPLETE. Several can apply at once.
INCOMPLETE_DETAILS = ('LATEST_SESSION_NOT_RETURNED', 'PROVIDER_HISTORY_STARTS_AFTER_RANGE_START',
                      'INTERIOR_SESSIONS_MISSING')


class RecoveryEtfHistoryProvider(EtfHistoryProvider):
    def _confirmed_absent_session(self, yf_ticker, missing_date):
        # Recovery cannot widen a missing one-session request into an implicit
        # neighbour probe. Keep the original error unresolved instead.
        return False


@dataclass(frozen=True)
class HistoryRecoveryRequest:
    source_cycle_id: str
    instrument_ids: tuple[UUID, ...]
    start: date
    end: date  # exclusive
    max_requests: int = 5
    max_http_requests: int = 40

    def validate(self, now):
        UUID(str(self.source_cycle_id))
        ids = [str(UUID(str(key))) for key in self.instrument_ids]
        if not 1 <= len(ids) <= MAX_INSTRUMENTS or len(ids) != len(set(ids)):
            raise ValueError('ETF_RECOVERY_EXPLICIT_UNIQUE_SCOPE_REQUIRED_MAX_5')
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('ETF_RECOVERY_AWARE_CLOCK_REQUIRED')
        if (type(self.start) is not date or type(self.end) is not date or self.start >= self.end
                or self.end > now.date() + timedelta(days=1)
                or (self.end - self.start).days > ETF_HISTORY_WINDOW_DAYS
                or self.start < now.date() - timedelta(days=ETF_HISTORY_WINDOW_DAYS)):
            raise ValueError('ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS')
        if (type(self.max_requests) is not int or not 1 <= self.max_requests <= MAX_PROVIDER_WINDOWS
                or type(self.max_http_requests) is not int or not 1 <= self.max_http_requests <= MAX_HTTP_REQUESTS):
            raise ValueError('ETF_RECOVERY_BUDGET_OUT_OF_BOUNDS')

    def parameters(self):
        return {'kind': RECOVERY_SLOT, 'source_cycle_id': str(UUID(str(self.source_cycle_id))),
                'instrument_ids': [str(UUID(str(key))) for key in self.instrument_ids],
                'start': self.start.isoformat(), 'end_exclusive': self.end.isoformat(),
                'max_requests': self.max_requests, 'max_http_requests': self.max_http_requests}


def load_recovery_source(store, source_cycle_id):
    """One shared source contract for explicit recovery and discovery."""
    source = store.cycle_run(str(UUID(str(source_cycle_id))))
    if not source or source['market'] != 'ETF' or source['status'] != 'COMPLETED':
        raise ValueError('ETF_RECOVERY_COMPLETED_ETF_SOURCE_REQUIRED')
    universe = (source.get('selection') or {}).get('universe')
    if not isinstance(universe, list) or not universe:
        raise ValueError('ETF_RECOVERY_SOURCE_UNIVERSE_UNAVAILABLE')
    return source, universe, store.cycle_progress(str(UUID(str(source_cycle_id))))


def source_candidate_eligibility(universe, progress, key):
    """Return the existing execution gate's decision without raising for a row.

    Discovery may explain exclusions; explicit execution still raises exactly
    the same stable guard codes. Neither path treats provider errors as absence.
    """
    try:
        key = str(UUID(str(key)))
    except (ValueError, TypeError, AttributeError):
        return None, '', 'ETF_RECOVERY_AMBIGUOUS_OR_UNKNOWN_CANONICAL_ID'
    matches = [item for item in universe if isinstance(item, dict)
               and isinstance(item.get('canonical'), dict)
               and str(item['canonical'].get('globalInstrumentId')) == key]
    if len(matches) != 1:
        return None, '', 'ETF_RECOVERY_AMBIGUOUS_OR_UNKNOWN_CANONICAL_ID'
    item = matches[0]
    row = copy.deepcopy(item['canonical'])
    prior = progress.get(('ETF_ONBOARD', item.get('isin'))) or {}
    payload = prior.get('payload') if isinstance(prior.get('payload'), dict) else {}
    failure = payload.get('reason') or prior.get('failure_reason') or ''
    failure = failure if isinstance(failure, str) else ''
    reason = _canonical_nse_etf_identity_reason(row, str(row.get('primarySymbol')) + '.NS')
    if (reason or item.get('identity_status') != 'VERIFIED'
            or item.get('isin') != row.get('isin') or item.get('symbol') != row.get('primarySymbol')):
        return row, failure, 'ETF_RECOVERY_CANONICAL_IDENTITY_NOT_VERIFIED'
    claims = [other for other in universe if isinstance(other, dict)
              and isinstance(other.get('canonical'), dict)
              and (other['canonical'].get('isin') == row['isin']
                   or other['canonical'].get('primarySymbol') == row['primarySymbol'])]
    if len(claims) != 1:
        return row, failure, 'ETF_RECOVERY_AMBIGUOUS_OR_UNKNOWN_CANONICAL_ID'
    if prior.get('state') != 'RETRYABLE_FAILURE' or prior.get('disposition') != 'TECHNICAL_FAILURE':
        return row, failure, 'ETF_RECOVERY_ONLY_FAILED_HISTORY_ALLOWED'
    if failure.startswith('VERIFIED_HISTORICAL_IDENTITY_MISMATCH:') and not failure.startswith(
            'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:PROVIDER_IDENTITY_UNAVAILABLE'):
        return row, failure, 'ETF_RECOVERY_UNRESOLVED_PROVIDER_IDENTITY_CONFLICT'
    return row, failure, None


def _source_candidates(store, request):
    _, universe, progress = load_recovery_source(store, request.source_cycle_id)
    rows = []
    for key in request.instrument_ids:
        row, failure, exclusion = source_candidate_eligibility(universe, progress, key)
        if exclusion:
            raise ValueError(exclusion)
        rows.append((row, failure))
    return rows


def coverage_details(store, row, request, now, *, validation_now=None):
    key = UUID(row['globalInstrumentId'])
    schedules = store.load_market_schedules({'NSE', 'XNSE'})
    exceptions = store.load_market_calendar_exceptions({'NSE', 'XNSE'})
    usable = [s for s in schedules if s.enabled and 'NSE' in {s.market_code.upper(), (s.mic or '').upper()}]
    if (not usable or any(s.regular_open_time >= s.regular_close_time for s in usable)
            or len({s.timezone for s in usable}) != 1):
        raise ValueError('HISTORICAL_SESSION_CALENDAR_UNAVAILABLE')
    try:
        ZoneInfo(usable[0].timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError('HISTORICAL_SESSION_CALENDAR_UNAVAILABLE') from exc
    as_of = min(now, datetime.combine(request.end, time.min, timezone.utc) - timedelta(microseconds=1))
    if unresolved_session_dates('NSE', schedules, exceptions, request.start, as_of):
        raise ValueError('HISTORICAL_SESSION_HOURS_UNVERIFIED')
    expected = {d for d, _ in completed_sessions('NSE', schedules, exceptions, request.start, as_of.date(), as_of)}
    bars = store.load_daily_market_bars({key}, start_date=request.start, end_date=request.end - timedelta(days=1))
    present = set()
    for provider in HISTORY_PROVIDERS:
        accepted, _ = select_etf_history_bars(bars, key, now=validation_now or now, provider=provider,
            canonical_symbol=row['primarySymbol'], schedules=schedules, exceptions=exceptions, require_calendar=True)
        for bar in accepted:
            # A Yahoo quote/adjusted URL cannot stand in for an unadjusted daily candle.
            if provider != 'NSE' and history_symbol(bar) != row['primarySymbol'] + '.NS':
                continue
            if bar.trading_date in expected:
                present.add(bar.trading_date)
    # Enumerate only actual completed, configured sessions. There is no guessed
    # listing date, weekday filling, synthetic bar or inferred absence prefix.
    ranges = missing_history_ranges(present, market='NSE', schedules=schedules, exceptions=exceptions,
                                    start=request.start, as_of=as_of)
    windows = [(a, min(b, request.end)) for a, b in ranges if a < request.end]
    return present, sorted(windows, reverse=True), expected


def _coverage(store, row, request, now, *, validation_now=None):
    present, windows, _ = coverage_details(store, row, request, now, validation_now=validation_now)
    return present, windows


def missing_session_windows(expected, present):
    """Actual missing completed sessions, grouped like ``remaining_windows`` (same order).

    Diagnostic only. Each group is a run of consecutive *expected* sessions with no valid bar, so a
    group may span a weekend or holiday. Nothing is inferred about listing dates or absent history.
    """
    groups, run = [], []
    for day in sorted(expected):
        if day in present:
            if run:
                groups.append(run)
                run = []
        else:
            run.append(day)
    if run:
        groups.append(run)
    return [{'start': g[0].isoformat(), 'end_exclusive': (g[-1] + timedelta(days=1)).isoformat(),
             'first_missing_session': g[0].isoformat(), 'last_missing_session': g[-1].isoformat(),
             'missing_session_count': len(g)} for g in sorted(groups, reverse=True)]


def incomplete_details(expected, present):
    """Every applicable explanation for missing completed sessions; never a single precedence pick.

    Based on valid stored sessions, so it explains completeness without claiming a listing date.
    """
    ordered = sorted(expected)
    kept = sorted(set(present) & set(ordered))
    missing = set(ordered) - set(kept)
    found = []
    if not missing:
        return found
    if ordered[-1] in missing:
        found.append('LATEST_SESSION_NOT_RETURNED')
    if kept:
        if ordered[0] in missing:
            found.append('PROVIDER_HISTORY_STARTS_AFTER_RANGE_START')
        if any(kept[0] < day < kept[-1] for day in missing):
            found.append('INTERIOR_SESSIONS_MISSING')
    return found


def recovery_diagnostics(expected, present, windows_run, failure):
    """Additive report fields. Never changes status, failure or any completeness decision."""
    ordered = sorted(expected)
    firsts = [w['accepted_provider_first_date'] for w in windows_run if w.get('accepted_provider_first_date')]
    lasts = [w['accepted_provider_last_date'] for w in windows_run if w.get('accepted_provider_last_date')]
    return {
        'expected_completed_sessions': len(ordered),
        'expected_latest_completed_session': ordered[-1].isoformat() if ordered else None,
        'provider_first_accepted_date': min(firsts) if firsts else None,
        'provider_last_accepted_date': max(lasts) if lasts else None,
        'rejected_invalid_close_count': sum(int(w.get('rejected_invalid_close_count') or 0) for w in windows_run),
        'missing_session_windows': missing_session_windows(expected, present),
        'incomplete_detail': incomplete_details(expected, present) if failure == 'HISTORICAL_RANGE_INCOMPLETE' else [],
        **rejected_close_diagnostics_report(windows_run),
    }


async def plan_history_recovery(repository, request, *, now):
    """Read-only: no checkpoint creation, provider construction or acquisition."""
    request.validate(now)
    def read():
        candidates = _source_candidates(repository.persistence, request)
        result = []
        for row, failure in candidates:
            present, windows = _coverage(repository.persistence, row, request, now)
            result.append({'instrument_id': row['globalInstrumentId'], 'isin': row['isin'],
                'symbol': row['primarySymbol'], 'original_failure': failure,
                'canonical_listing_date': row.get('listingDate'),
                'listing_boundary': 'NOT_INDEPENDENTLY_VERIFIED_NOT_USED',
                'existing_valid_sessions': len(present),
                'missing_windows': [{'start': a.isoformat(), 'end_exclusive': b.isoformat()} for a, b in windows],
                'older_history': 'UNRESOLVED_OUTSIDE_APPROVED_RANGE'})
        return {'kind': RECOVERY_SLOT, 'parameters': request.parameters(), 'instruments': result,
                'write_policy': 'INSERT_ONLY', 'publication': 'UNCHANGED', 'recommendation': 'NOT_EVALUATED'}
    return await repository._run_blocking_persistence(read)


class RecoveryBudget(BudgetClient):
    """Durable window + HTTP ceilings for one budget on one owning event loop.

    Transport threads dispatch reservations to that loop. This does not provide
    process-wide or distributed counter atomicity across separate instances.
    """
    def __init__(self, checkpoint, selection, **kwargs):
        super().__init__(None, checkpoint, selection, **kwargs)
        self.http_last = None
        self.closed = False
        self._http_loop = None
        self._http_lock = None
        self._http_loop_guard = threading.Lock()

    def _http_lock_for_current_loop(self):
        loop = asyncio.get_running_loop()
        # No awaits or I/O under the threading lock; racing first uses on
        # different loops must not silently create independent async locks.
        with self._http_loop_guard:
            if self._http_loop is None:
                self._http_loop, self._http_lock = loop, asyncio.Lock()
            elif self._http_loop is not loop:
                raise RuntimeError('ETF_RECOVERY_BUDGET_EVENT_LOOP_MISMATCH')
        return self._http_lock

    def _assert_http_open(self):
        if self.closed:
            raise RuntimeError('ETF_RECOVERY_STOPPED')
        if self.abort:
            raise RuntimeError(self.abort)

    async def assert_owned(self):
        run = await self.checkpoint.run()
        if not run or run['status'] != 'RUNNING' or run.get('owner_id') != self.checkpoint.owner_id:
            raise CycleOwnershipLost(self.checkpoint.cycle_id)
        renewed = await self.checkpoint._call(self.checkpoint.persistence.renew_cycle_lease,
                                             self.checkpoint.cycle_id, self.checkpoint.owner_id)
        if not renewed:
            raise CycleOwnershipLost(self.checkpoint.cycle_id)

    async def reserve_request(self):
        await self.assert_owned()
        await super().reserve_request()

    async def reserve_http(self):
        async with self._http_lock_for_current_loop():
            self._assert_http_open()
            await self.assert_owned()
            self._assert_http_open()
            if self.selection['http_reservations'] >= self.selection['limits']['max_http_requests']:
                self.abort = 'ETF_HTTP_BUDGET_EXHAUSTED'
                raise RuntimeError(self.abort)
            if self.http_last is not None:
                await self.sleep(max(0, 1 - (self.monotonic() - self.http_last)))
                self._assert_http_open()
                await self.assert_owned()
                self._assert_http_open()
            self.selection['http_reservations'] += 1
            try:
                # The persistence bridge shields/drains its worker on cancellation.
                # Retain this lock until that save finishes or propagates failure.
                await self.checkpoint.save_selection(self.selection)
            except BaseException:
                # A failed/cancelled save may have committed. Never refund the
                # charge or admit more HTTP from an uncertain checkpoint state.
                self.closed = True
                raise
            self.http_last = self.monotonic()
            self._assert_http_open()


def live_recovery_provider(budget, settings):
    """Instrument the existing approved session, never substitute a new feed."""
    loop = asyncio.get_running_loop()
    budget._http_lock_for_current_loop()
    import yfinance as yf
    from app.structured_market import _build_bounded_yahoo_session
    session = _build_bounded_yahoo_session(min(15, settings.structured_provider_timeout_seconds))
    original = session.request
    def request(method, url, *args, **kwargs):
        budget._assert_http_open()
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            raise RuntimeError('ETF_RECOVERY_HTTP_REQUIRES_WORKER_THREAD')
        if loop.is_closed() or not loop.is_running():
            raise RuntimeError('ETF_RECOVERY_STOPPED')
        reservation = budget.reserve_http()
        try:
            pending = asyncio.run_coroutine_threadsafe(reservation, loop)
        except Exception:
            reservation.close()
            raise
        pending.result()
        budget._assert_http_open()
        # Reservation lock has been released; no network I/O runs under it.
        response = original(method, url, *args, **kwargs)
        if response.status_code in (403, 429):
            budget.abort = 'EXTERNAL_PROVIDER_RATE_LIMITED' if response.status_code == 429 else 'YAHOO_ACCESS_RESTRICTED'
        return response
    session.request = request
    return RecoveryEtfHistoryProvider(lambda symbol: yf.Ticker(symbol, session=session))


async def recover_etf_history(repository, orchestrator, request, *, now, identity_headers,
                              recovery_id=None, provider_factory=None, sleep=asyncio.sleep, clock=None):
    """Explicit execution API; callers must authorize separately from preflight.

    No source-cycle update, publication, fundamental acquisition or readiness
    promotion. Resume only an identical, still-RUNNING recovery checkpoint.
    """
    plan = await plan_history_recovery(repository, request, now=now)
    source_rows = await repository._run_blocking_persistence(lambda: _source_candidates(repository.persistence, request))
    fresh = []
    for row, _ in source_rows:
        current = await orchestrator.global_instrument_metadata(UUID(row['globalInstrumentId']), identity_headers=identity_headers)
        if (any(current.get(k) != row.get(k) for k in
                ('globalInstrumentId', 'isin', 'primarySymbol', 'primaryExchange', 'assetType', 'currency'))
                or _canonical_nse_etf_identity_reason(current, row['primarySymbol'] + '.NS')):
            raise ValueError('ETF_RECOVERY_CURRENT_CANONICAL_IDENTITY_CONFLICT')
        fresh.append(current)
    recovery_id = str(UUID(str(recovery_id))) if recovery_id else str(uuid4())
    owner = str(uuid4())
    store = repository.persistence
    parameters = request.parameters()
    def claim():
        # Never let this CLI join or alter an unrelated run or its active slot.
        existing = store.cycle_run(recovery_id)
        if existing and (existing['market'] != RECOVERY_SLOT or existing['parameters'] != parameters
                         or existing['status'] not in ('ACCEPTED', 'RUNNING')):
            raise ValueError('ETF_RECOVERY_RESUME_CONTRACT_MISMATCH')
        if not existing:
            run, created = store.create_cycle_run(recovery_id, parameters, market=RECOVERY_SLOT)
            if not created:
                raise ValueError('ETF_RECOVERY_OTHER_OWNER_ACTIVE')
        if not store.claim_cycle_run(recovery_id, owner):
            raise CycleOwnershipLost(recovery_id)
    await repository._run_blocking_persistence(claim)
    checkpoint = CycleCheckpoint(store, recovery_id, owner, run_blocking=repository._run_blocking_persistence)
    try:
        return await _execute_recovery(repository, request, checkpoint, plan, fresh, now,
            provider_factory=provider_factory, sleep=sleep, clock=clock or (lambda: datetime.now(timezone.utc)))
    except asyncio.CancelledError:
        await asyncio.shield(checkpoint._call(store.cancel_cycle_run, recovery_id, owner))
        raise
    except CycleOwnershipLost:
        run = await checkpoint.run()
        if run and run.get('owner_id') == owner and run['status'] == 'CANCEL_REQUESTED':
            await checkpoint._call(store.cancel_cycle_run, recovery_id, owner)
        raise
    finally:
        await asyncio.shield(checkpoint._call(store.release_cycle_run, recovery_id, owner))


async def _execute_recovery(repository, request, checkpoint, plan, fresh, now, *, provider_factory, sleep, clock):
    store, recovery_id, owner = repository.persistence, checkpoint.cycle_id, checkpoint.owner_id
    await checkpoint.load()
    selection = await checkpoint.selection() or {'limits': {'max_requests': request.max_requests,
        'max_http_requests': request.max_http_requests}, 'request_reservations': 0, 'http_reservations': 0,
        'as_of': now.isoformat(), 'plan': plan, 'results': {}}
    if selection.get('limits') != {'max_requests': request.max_requests, 'max_http_requests': request.max_http_requests}:
        raise ValueError('ETF_RECOVERY_PERSISTED_BUDGET_MISMATCH')
    # Resume the original clock: later sessions must require a separately approved range.
    now = datetime.fromisoformat(selection['as_of'])
    await checkpoint.save_selection(selection)
    budget = RecoveryBudget(checkpoint, selection, sleep=sleep)
    provider = None
    stop_reason = None
    try:
        for row in fresh:
            await budget.assert_owned()
            isin = row['isin']
            prior = checkpoint.restorable('ETF_ONBOARD', isin)
            if prior is not None:
                payload = prior.get('payload')
                if not isinstance(payload, dict) or payload.get('instrument_id') != row['globalInstrumentId']:
                    raise ValueError('ETF_RECOVERY_CHECKPOINT_PAYLOAD_CONFLICT')
                _, remaining = await repository._run_blocking_persistence(
                    lambda: _coverage(store, row, request, now, validation_now=clock()))
                if prior['state'] == 'COMPLETED' and remaining:
                    raise ValueError('ETF_RECOVERY_CHECKPOINT_COVERAGE_CHANGED')
                selection['results'][isin] = payload
                await checkpoint.save_selection(selection)
                continue
            await checkpoint.start('ETF_ONBOARD', isin)
            written, failure, windows_run = 0, None, []
            _, windows = await repository._run_blocking_persistence(lambda: _coverage(store, row, request, now, validation_now=clock()))
            for start, end in windows:
                try:
                    await budget.reserve_request()
                except RuntimeError as exc:
                    if isinstance(exc, CycleOwnershipLost):
                        raise
                    if str(exc) != 'ETF_REQUEST_BUDGET_EXHAUSTED':
                        raise
                    failure = stop_reason = str(exc)
                    break
                if provider is None:
                    if provider_factory is None:
                        from app.settings import Settings
                        provider = live_recovery_provider(budget, Settings())
                    else:
                        provider = provider_factory(budget)
                result = await acquire_etf_history(repository, row, now=now, checkpoint=checkpoint, provider=provider,
                    start=datetime.combine(start, time.min, timezone.utc),
                    end=datetime.combine(end, time.min, timezone.utc), recovery_mode=True, retrieval_clock=clock)
                await budget.assert_owned()
                written += result['observations']
                windows_run.append({'start': start.isoformat(), 'end_exclusive': end.isoformat(), **result})
                if budget.abort or result['status'] == 'TECHNICAL_FAILURE':
                    failure = budget.abort or result['reason']
                    if budget.abort or any(token in failure for token in
                            ('RATE_LIMITED', 'SESSION_BACKOFF_ACTIVE', 'DOWNSTREAM_TIMEOUT')):
                        stop_reason = failure
                    break  # Never skip a failed window and label its gap resolved.
            present, remaining, expected = await repository._run_blocking_persistence(
                lambda: coverage_details(store, row, request, now, validation_now=clock()))
            failure = failure or ('HISTORICAL_RANGE_INCOMPLETE' if remaining else None)
            outcome = {'instrument_id': row['globalInstrumentId'], 'isin': isin, 'observations': written,
                'status': 'RECOVERED_APPROVED_RANGE' if not failure else 'UNRESOLVED', 'reason': failure,
                'valid_sessions': len(present), 'windows': windows_run,
                'remaining_windows': [{'start': a.isoformat(), 'end_exclusive': b.isoformat()} for a, b in remaining],
                'older_history': 'UNRESOLVED_OUTSIDE_APPROVED_RANGE', 'recommendation': 'NOT_EVALUATED'}
            outcome['persisted_bar_count'] = written
            outcome.update(recovery_diagnostics(expected, present, windows_run, failure))
            outcome['recovery_outcome'] = (
                'RECOVERED_APPROVED_RANGE' if not failure else
                'HISTORICAL_RANGE_INCOMPLETE' if failure == 'HISTORICAL_RANGE_INCOMPLETE' else
                'UNRESOLVED' if failure in ('ETF_REQUEST_BUDGET_EXHAUSTED', 'ETF_HTTP_BUDGET_EXHAUSTED',
                                          'ETF_RECOVERY_STOPPED') else 'TECHNICAL_FAILURE')
            await checkpoint.finish('ETF_ONBOARD', isin,
                CandidateState.RETRYABLE_FAILURE if failure else CandidateState.COMPLETED,
                disposition=outcome['status'], failure_reason=failure, payload=outcome)
            selection['results'][isin] = outcome
            await checkpoint.save_selection(selection)
            if stop_reason:
                break
        complete = (len(selection['results']) == len(fresh)
                    and all(r['status'] == 'RECOVERED_APPROVED_RANGE' for r in selection['results'].values()))
        selection['status'] = 'COMPLETED_APPROVED_RANGE' if complete else 'PARTIAL_FAILURE'
        selection['stop_reason'] = stop_reason
        await checkpoint.save_selection(selection)
        await checkpoint._call(store.update_cycle_run, recovery_id, owner,
                               status='COMPLETED' if complete else 'FAILED', error_code=stop_reason)
        return {'recovery_id': recovery_id, **selection}
    except asyncio.CancelledError:
        budget.closed = True
        await asyncio.shield(checkpoint._call(store.cancel_cycle_run, recovery_id, owner))
        raise
    finally:
        budget.closed = True
