"""Bounded NSE daily-bar planning and execution within population jobs.

No exchange sessions are synthesized. Coverage means observed boundary coverage,
not proof that every internal trading session is present.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from app.nse_historical_daily import NseHistoricalDailyProvider, persist_daily_result, verified_identity

DAY = timedelta(days=1)


def plan_windows(start: date, end: date, size: int) -> list[tuple[date, date]]:
    if type(start) is not date or type(end) is not date or start > end or type(size) is not int or size < 1:
        raise ValueError('WINDOW_PLANNING_FAILURE')
    windows = []
    cursor = start
    while cursor <= end:
        last = cursor + timedelta(days=min(size - 1, (end - cursor).days))
        windows.append((cursor, last))
        if last == end:
            break
        cursor = last + DAY
    return windows


def usable_bars(rows, key, symbol, currency, end):
    return sorted((b for b in (rows or []) if b is not None
        and b.global_instrument_id == key and b.provider == 'NSE'
        and b.source_mode == 'REAL' and b.provider_symbol == symbol and b.currency == currency
        and b.trading_date <= end and all(getattr(b, name) is not None for name in ('open', 'high', 'low', 'close'))),
        key=lambda b: b.trading_date)


def coverage_plan(rows, start, end, now, freshness_hours, *, force=False):
    """Conservative prefix/tail policy; short tail lag is freshness, not a holiday claim."""
    if not rows:
        return 'NO_HISTORY', [(start, end)]
    first, last = rows[0], rows[-1]
    freshness = timedelta(hours=freshness_hours)
    prefix = first.trading_date > start
    stale = now - last.retrieved_at >= freshness or timedelta(days=(end - last.trading_date).days) >= freshness
    state = 'PARTIAL_HISTORY' if prefix else 'STALE_HISTORY' if stale else 'CURRENT_HISTORY'
    if force:
        return state, [(start, end)]
    intervals = []
    if prefix:
        intervals.append((start, min(end, first.trading_date - DAY)))
    if stale:
        # Revisit the last bar only when no later date exists, to allow correction
        # and refresh retrieval provenance; otherwise acquire an incremental tail.
        tail = min(end, last.trading_date + DAY) if last.trading_date < end else end
        intervals.append((max(start, tail), end))
    return state, intervals


def uncovered(intervals, completed):
    """Subtract successful request ranges, not imagined exchange sessions."""
    remaining = list(intervals)
    for low, high in sorted(completed):
        next_ranges = []
        for start, end in remaining:
            if high < start or low > end:
                next_ranges.append((start, end))
            else:
                if start < low:
                    next_ranges.append((start, low - DAY))
                if high < end:
                    next_ranges.append((high + DAY, end))
        remaining = next_ranges
    return remaining


def failure_class(reason):
    if reason == 'DAILY_BAR_PERSISTENCE_UNAVAILABLE':
        return 'PERSISTENCE_FAILURE'
    if '429' in reason:
        return 'THROTTLED'
    if 'HTTP_' in reason:
        return 'HTTP_ERROR'
    if reason in {'EMPTY_RESPONSE', 'NO_VALID_HISTORY'}:
        return 'EMPTY_RESPONSE'
    if any(s in reason for s in ('CSV', 'HEADERS', 'REJECTED_ROWS')):
        return 'PARSER_FAILURE'
    if any(s in reason for s in ('MAPPING', 'CURRENCY', 'UNSUPPORTED_INSTRUMENT', 'IDENTITY_MISMATCH')):
        return 'NO_TRUSTED_MAPPING'
    if reason.startswith('IDENTITY'):
        return 'IDENTITY_UNAVAILABLE'
    if 'WINDOW' in reason or 'DATE_RANGE' in reason:
        return 'WINDOW_PLANNING_FAILURE'
    return 'PROVIDER_UNAVAILABLE'


def _item(key):
    return dict(globalInstrumentId=str(key), nse_symbol=None, coverage_state_before='UNKNOWN',
        requested_windows=0, successful_windows=0, failed_windows=0, rows_received=0,
        rows_accepted=0, rows_persisted=0, status='FAILED', failure_reason=None,
        failure_class=None, earliest_persisted_date=None, latest_persisted_date=None, windows=[])


async def run_backfill(jobs, *, identity_headers, correlation_id, offset, instrument_ids, start, end, force):
    now = jobs._clock()
    today = now.astimezone(ZoneInfo('Asia/Kolkata')).date()
    settings = jobs.settings
    end = today if end is None else end
    start = today - timedelta(days=settings.market_data_population_initial_lookback_days) if start is None else start
    summary = dict(job_started_at=now, job_finished_at=None, status='RUNNING', failure_reason=None,
        requested_instruments=0, processed_instruments=0, succeeded_instruments=0, failed_instruments=0,
        skipped_current=0, skipped_cooldown=0, requested_windows=0, successful_windows=0,
        failed_windows=0, rows_received=0, rows_accepted=0, rows_persisted=0,
        first_requested_date=None, last_requested_date=None, target_start=start, target_end=end,
        offset=offset, next_offset=None, candidate_instruments=0, instruments=[],
        internal_gap_repair='UNSUPPORTED_IN_THIS_PHASE')
    provider = None
    job_failure_stage = 'WINDOW_PLANNING_FAILURE'
    try:
        if (type(offset) is not int or offset < 0 or type(force) is not bool
                or type(start) is not date or type(end) is not date or start > end or end > today
                or start < today - timedelta(days=settings.market_data_population_initial_lookback_days)):
            raise ValueError('WINDOW_PLANNING_FAILURE')
        # Canonical metadata only; no portfolio/watchlist/scanner membership.
        job_failure_stage = 'UNIVERSE_UNAVAILABLE'
        universe = await jobs.orchestrator.active_global_equities(
            identity_headers=identity_headers, correlation_id=correlation_id)
        candidates = set()
        for value in universe:
            if not isinstance(value, dict):
                continue
            if (value.get('status') != 'ACTIVE' or value.get('assetType') != 'EQUITY'
                    or value.get('country') not in {'IN', 'IND', 'INDIA'}
                    or value.get('exchange') not in {'NSE', 'XNSE'}):
                continue
            try:
                key = UUID(str(value.get('globalInstrumentId')))
            except ValueError:
                continue
            if instrument_ids is None or key in instrument_ids:
                candidates.add(key)
        ordered = sorted(candidates, key=str)
        selected = ordered[offset:offset + settings.market_data_population_batch_size]
        summary['candidate_instruments'] = len(ordered)
        summary['requested_instruments'] = len(selected)
        summary['next_offset'] = offset + len(selected) if offset + len(selected) < len(ordered) else None
        throttled = (jobs._daily_bar_throttled_at is not None and now - jobs._daily_bar_throttled_at
            < timedelta(hours=settings.market_data_population_retry_cooldown_hours))
        for key in selected:
            item = _item(key)
            summary['instruments'].append(item)
            summary['processed_instruments'] += 1
            stage = 'IDENTITY_UNAVAILABLE'
            try:
                metadata = await jobs.orchestrator.global_instrument_metadata(key,
                    identity_headers=identity_headers, correlation_id=correlation_id)
                symbol, currency = verified_identity(metadata, key)
                item['nse_symbol'] = symbol
                stage = 'PERSISTENCE_FAILURE'
                existing = await jobs.repository.daily_market_bars_for_instruments({key}, end_date=end, provider='NSE')
                rows = usable_bars(existing.get(key), key, symbol, currency, end)
                if rows:
                    item['earliest_persisted_date'], item['latest_persisted_date'] = rows[0].trading_date, rows[-1].trading_date
                stage = 'WINDOW_PLANNING_FAILURE'
                state, intervals = coverage_plan(rows, start, end, now,
                    settings.market_data_historical_freshness_hours, force=force)
                item['coverage_state_before'] = state
                cache_key = (key, symbol, currency)
                fresh_ranges = [(a, b, stamp) for a, b, stamp in jobs._daily_bar_completed_ranges.get(cache_key, [])
                    if now - stamp < timedelta(hours=settings.market_data_historical_freshness_hours)]
                jobs._daily_bar_completed_ranges[cache_key] = fresh_ranges
                if not force and rows:
                    intervals = uncovered(intervals, [(a, b) for a, b, _ in fresh_ranges])
                failed_at = jobs._daily_bar_failures.get(key)
                if throttled or (failed_at is not None and now - failed_at < timedelta(hours=settings.market_data_population_retry_cooldown_hours)):
                    item.update(status='SKIPPED_COOLDOWN', failure_reason='JOB_THROTTLED' if throttled else 'RETRY_COOLDOWN')
                    continue
                if not intervals:
                    item.update(status='SKIPPED_CURRENT')
                    continue
                windows = [window for a, b in intervals for window in plan_windows(a, b, settings.nse_historical_request_window_days)]
                stage = 'PROVIDER_UNAVAILABLE'
                if provider is None:
                    provider = NseHistoricalDailyProvider(jobs.orchestrator, settings)
                for a, b in windows:
                    item['requested_windows'] += 1
                    detail = dict(start=a, end=b, status='FAILED', failure_reason=None, http_status=None)
                    item['windows'].append(detail)
                    summary['first_requested_date'] = min(summary['first_requested_date'] or a, a)
                    summary['last_requested_date'] = max(summary['last_requested_date'] or b, b)
                    try:
                        result = await provider.fetch(key, start=a, end=b,
                            identity_headers=identity_headers, correlation_id=correlation_id)
                        item['rows_received'] += result.rows_parsed
                        item['rows_accepted'] += result.rows_accepted
                        detail['http_status'] = result.http_status
                        result = await persist_daily_result(jobs.repository, result)
                        item['rows_persisted'] += result.persisted_rows
                        if result.persisted_rows:
                            item['earliest_persisted_date'] = min(item['earliest_persisted_date'] or result.first_trading_date, result.first_trading_date)
                            item['latest_persisted_date'] = max(item['latest_persisted_date'] or result.last_trading_date, result.last_trading_date)
                        reason = result.failure_reason
                        if result.rows_rejected and reason in {None, 'NO_VALID_HISTORY'}:
                            reason = 'REJECTED_ROWS'
                        if reason:
                            detail['failure_reason'] = reason
                            item['failed_windows'] += 1
                            item.update(failure_reason=reason, failure_class=failure_class(reason))
                            jobs._daily_bar_failures[key] = jobs._clock()
                            if '429' in reason:
                                throttled = True
                                jobs._daily_bar_throttled_at = jobs._clock()
                            # Empty windows may precede listing or contain closures;
                            # they are failures, not fabricated zero-bar successes.
                            if failure_class(reason) != 'EMPTY_RESPONSE':
                                break
                        else:
                            detail['status'] = 'SUCCESS'
                            item['successful_windows'] += 1
                            jobs._daily_bar_completed_ranges[cache_key].append((a, b, jobs._clock()))
                    except Exception:
                        item['failed_windows'] += 1
                        detail['failure_reason'] = 'PROVIDER_UNAVAILABLE'
                        item.update(failure_reason='PROVIDER_UNAVAILABLE', failure_class='PROVIDER_UNAVAILABLE')
                        jobs._daily_bar_failures[key] = jobs._clock()
                        break
                if not item['failed_windows']:
                    item['status'] = 'SUCCESS'
                    jobs._daily_bar_failures.pop(key, None)
            except Exception as exc:
                safe_reasons = {'IDENTITY_MISMATCH', 'UNSUPPORTED_INSTRUMENT', 'NO_UNAMBIGUOUS_NSE_MAPPING',
                    'NO_TRUSTED_NSE_MAPPING', 'INVALID_NSE_MAPPING', 'MISSING_OR_AMBIGUOUS_CURRENCY', 'WINDOW_PLANNING_FAILURE'}
                reason = str(exc) if type(exc) is ValueError and str(exc) in safe_reasons else stage
                item.update(failure_reason=reason, failure_class=failure_class(reason) if stage == 'IDENTITY_UNAVAILABLE' else stage)
                jobs._daily_bar_failures[key] = jobs._clock()
            finally:
                for metric in ('requested_windows', 'successful_windows', 'failed_windows', 'rows_received', 'rows_accepted', 'rows_persisted'):
                    summary[metric] += item[metric]
                counter = {'SUCCESS':'succeeded_instruments', 'FAILED':'failed_instruments',
                    'SKIPPED_CURRENT':'skipped_current', 'SKIPPED_COOLDOWN':'skipped_cooldown'}[item['status']]
                summary[counter] += 1
        summary['status'] = 'COMPLETED_WITH_ERRORS' if summary['failed_instruments'] else 'COMPLETED'
    except Exception:
        summary['status'] = 'FAILED'
        summary['failure_reason'] = job_failure_stage
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception:
                summary.update(status='FAILED', failure_reason='SESSION_CLOSE_FAILURE')
            finally:
                await jobs._sleep(settings.market_data_population_request_interval_seconds)
        summary['job_finished_at'] = jobs._clock()
    return summary
