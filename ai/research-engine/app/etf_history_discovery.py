"""Deterministic, database-only discovery for explicitly bounded ETF repair.

Discovery ranks coverage gaps, never provider success probabilities. It opens
one enforced read-only snapshot, performs no provider calls, and cannot claim a
checkpoint, publish research, or authorize an execution.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import re
from uuid import UUID

from app.etf_history_recovery import (
    HistoryRecoveryRequest, MAX_INSTRUMENTS, RECOVERY_SLOT, coverage_details,
    load_recovery_source, source_candidate_eligibility,
)


_FAILURES = frozenset({
    'YAHOO_EXPLICIT_CHART_ERROR', 'YAHOO_MALFORMED_RESPONSE', 'YAHOO_RATE_LIMITED',
    'YAHOO_SESSION_BACKOFF_ACTIVE', 'YAHOO_DOWNSTREAM_TIMEOUT', 'YAHOO_FETCH_FAILED',
    'YAHOO_ACCESS_RESTRICTED', 'EXTERNAL_PROVIDER_RATE_LIMITED',
    'EXTERNAL_PROVIDER_UNAVAILABLE', 'DOWNSTREAM_TIMEOUT',
    'VERIFIED_HISTORICAL_IDENTITY_MISMATCH', 'PROVIDER_IDENTITY_UNAVAILABLE',
    'HISTORICAL_PRICE_PROVIDER_UNAVAILABLE', 'HISTORICAL_RANGE_INCOMPLETE',
    'ETF_REQUEST_BUDGET_EXHAUSTED', 'ETF_HTTP_BUDGET_EXHAUSTED',
    'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE', 'HISTORICAL_SESSION_HOURS_UNVERIFIED',
})
_FAILED_STATES = frozenset({'RETRYABLE_FAILURE', 'EVIDENCE_UNAVAILABLE', 'TERMINAL_OUTCOME'})
_RUN_STATUSES = frozenset({'ACCEPTED', 'RUNNING', 'PUBLISHED', 'CANCEL_REQUESTED',
                           'COMPLETED', 'FAILED', 'CANCELLED'})


def _failure_class(value):
    """Expose codes, never arbitrary persisted exception bodies or URLs."""
    if not value:
        return None
    tokens = str(value).split(':')
    if tokens[:2] == ['HISTORICAL_PRICE_PROVIDER_UNAVAILABLE', 'YFPricesMissingError']:
        return 'YAHOO_FETCH_FAILED'
    if tokens[:2] == ['VERIFIED_HISTORICAL_IDENTITY_MISMATCH', 'PROVIDER_IDENTITY_UNAVAILABLE']:
        return 'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:PROVIDER_IDENTITY_UNAVAILABLE'
    if tokens[0] == 'HISTORICAL_PRICE_PROVIDER_UNAVAILABLE' and len(tokens) > 1 and tokens[1] in _FAILURES:
        return tokens[1]
    return tokens[0] if tokens[0] in _FAILURES else 'UNCLASSIFIED_TECHNICAL_FAILURE'


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def _identity(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def _previous_recoveries(store, source_cycle_id):
    """Read existing operational checkpoints; never create or resume one."""
    runs = store._connection.execute(
        'SELECT cycle_id, parameters, selection, status, created_at '
        'FROM global_opportunity_cycle_run WHERE market = ?', (RECOVERY_SLOT,)).fetchall()
    by_instrument = {}
    for run in sorted(runs, key=lambda r: (str(r['created_at']), str(r['cycle_id']))):
        parameters = _object(run['parameters'])
        if (parameters.get('kind') != RECOVERY_SLOT
                or _uuid(parameters.get('source_cycle_id')) != source_cycle_id):
            continue
        instrument_ids = parameters.get('instrument_ids')
        if not isinstance(instrument_ids, (list, tuple)):
            raise ValueError('ETF_DISCOVERY_INVALID_RECOVERY_METADATA')
        selection = _object(run['selection'])
        results = _object(selection.get('results'))
        for key in instrument_ids:
            key = _uuid(key)
            if key is None:
                continue
            outcomes = [r for r in results.values() if isinstance(r, dict)
                        and _uuid(r.get('instrument_id')) == key]
            outcome = outcomes[0] if len(outcomes) == 1 else {}
            instrument_status = outcome.get('status')
            by_instrument[key] = {
                'recovery_id': _uuid(run['cycle_id']),
                'checkpoint_status': run['status'] if run['status'] in _RUN_STATUSES else 'UNKNOWN',
                'recovery_status': selection.get('status') if selection.get('status') in {
                    'PARTIAL_FAILURE', 'COMPLETED_APPROVED_RANGE'} else None,
                'instrument_status': instrument_status if instrument_status in {
                    'RECOVERED_APPROVED_RANGE', 'UNRESOLVED', 'TECHNICAL_FAILURE',
                    'HISTORICAL_RANGE_INCOMPLETE'} else None,
                'reason_classification': _failure_class(outcome.get('reason')),
                'terminal': run['status'] in {'COMPLETED', 'FAILED', 'CANCELLED'},
                'execution_authorized': False,
            }
    return by_instrument


def _windows(values):
    return [{'start': a.isoformat(), 'end_exclusive': b.isoformat()} for a, b in values]


async def discover_etf_history_recovery(repository, source_cycle_id, *, limit=5,
                                      start=None, end=None, now=None):
    """Return a stable shortlist inside one read-only consistent snapshot.

    Counts/dates refer to the explicitly reported recent range, not lifetime
    history. Missing listing boundaries and older history remain unresolved.
    The maximum five shortlist does not bound the immutable universe inspected.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_INSTRUMENTS:
        raise ValueError('ETF_DISCOVERY_LIMIT_OUT_OF_BOUNDS')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None and now.utcoffset() is not None:
        now = now.astimezone(timezone.utc)
    source_cycle_id = str(UUID(str(source_cycle_id)))
    end = end if end is not None else now.date() + timedelta(days=1)
    start = start if start is not None else end - timedelta(days=366)
    # Reuse the execution contract's range/clock checks without granting scope
    # or writing a checkpoint. A synthetic UUID is only a validation sentinel.
    request = HistoryRecoveryRequest(source_cycle_id, (UUID(int=1),), start, end)
    request.validate(now)

    def read():
        store = repository.persistence
        with store.read_only_snapshot():
            _, universe, progress = load_recovery_source(store, source_cycle_id)
            previous = _previous_recoveries(store, source_cycle_id)
            assessments = []
            seen = set()
            for item in universe:
                if not isinstance(item, dict):
                    continue
                canonical = item.get('canonical') if isinstance(item.get('canonical'), dict) else {}
                key = _uuid(canonical.get('globalInstrumentId'))
                prior = progress.get(('ETF_ONBOARD', item.get('isin')))
                if not prior or (prior.get('state') not in _FAILED_STATES
                                 and prior.get('disposition') != 'TECHNICAL_FAILURE'):
                    continue
                # Duplicate identities are assessed once and rejected by the
                # same shared eligibility check used by explicit execution.
                dedup = key or (item.get('isin'), item.get('symbol'))
                if dedup in seen:
                    continue
                seen.add(dedup)
                row, failure, exclusion = source_candidate_eligibility(universe, progress, key)
                row = row or canonical
                listing = row.get('listingDate')
                record = {
                    'source_cycle_id': source_cycle_id,
                    'instrument_id': key,
                    'isin': _identity(row.get('isin') or item.get('isin'), r'IN[A-Z0-9]{9}[0-9]'),
                    'primary_symbol': _identity(row.get('primarySymbol') or item.get('symbol'), r'[A-Z0-9&._-]{1,50}'),
                    'original_failure_classification': _failure_class(failure),
                    'existing_valid_bar_count': None,
                    'first_available_historical_date': None,
                    'last_available_historical_date': None,
                    'coverage_scope': 'APPROVED_RANGE_ONLY',
                    'canonical_listing_date': _date(listing),
                    'listing_date_verification_status': 'MISSING' if not listing else 'UNVERIFIED_NOT_USED',
                    'missing_historical_windows': [],
                    'missing_completed_sessions': None,
                    'expected_completed_sessions': None,
                    'previous_recovery': previous.get(key),
                    'eligibility_status': 'EXCLUDED' if exclusion else 'ELIGIBLE',
                    'exclusion_reason': exclusion,
                    'recommended_priority': None,
                    'priority_rationale': None,
                    'older_history': 'UNRESOLVED_OUTSIDE_APPROVED_RANGE',
                }
                if not exclusion:
                    try:
                        present, windows, expected = coverage_details(store, row, request, now)
                    except ValueError as exc:
                        # Calendar uncertainty is explicit, not empty coverage.
                        if str(exc) not in {'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE',
                                            'HISTORICAL_SESSION_HOURS_UNVERIFIED'}:
                            raise
                        record['eligibility_status'] = 'EXCLUDED'
                        record['exclusion_reason'] = str(exc)
                    else:
                        record.update(existing_valid_bar_count=len(present),
                            first_available_historical_date=min(present).isoformat() if present else None,
                            last_available_historical_date=max(present).isoformat() if present else None,
                            missing_historical_windows=_windows(windows),
                            missing_completed_sessions=len(expected - present),
                            expected_completed_sessions=len(expected))
                        if not windows:
                            record['eligibility_status'] = 'EXCLUDED'
                            record['exclusion_reason'] = 'HISTORICAL_RANGE_ALREADY_COVERED'
                assessments.append(record)

            eligible = [r for r in assessments if r['eligibility_status'] == 'ELIGIBLE']
            # Valid partial coverage with a matching prior run is independently
            # observable; the run's existence does not prove it wrote the bars.
            # Then prioritize larger real session gaps. Neither predicts feed
            # availability. Symbol/ISIN/UUID provide a stable final ordering.
            def priority(r):
                partial = bool(r['previous_recovery'] and r['existing_valid_bar_count'])
                return (not partial, -r['missing_completed_sessions'],
                        r['primary_symbol'] or '', r['isin'] or '', r['instrument_id'] or '')
            eligible.sort(key=priority)
            for index, row in enumerate(eligible, 1):
                row['recommended_priority'] = index
                row['priority_rationale'] = (
                    'VERIFIED_PARTIAL_COVERAGE_WITH_PRIOR_RECOVERY'
                    if row['previous_recovery'] and row['existing_valid_bar_count']
                    else 'MISSING_COMPLETED_SESSION_COVERAGE')
            assessments.sort(key=lambda r: (r['recommended_priority'] is None,
                r['recommended_priority'] or 0, r['primary_symbol'] or '',
                r['isin'] or '', r['instrument_id'] or ''))
            return {
                'kind': 'ETF_HISTORY_RECOVERY_DISCOVERY', 'mode': 'READ_ONLY',
                'source_cycle_id': source_cycle_id, 'as_of': now.isoformat(),
                'range': {'start': start.isoformat(), 'end_exclusive': end.isoformat()},
                'limit': limit,
                'summary': {
                    'source_universe': len(universe),
                    'original_failed_history_candidates': len(assessments),
                    'eligible_candidates': len(eligible),
                    'excluded_candidates': len(assessments) - len(eligible),
                    'partially_recovered_candidates': sum(bool(r['previous_recovery']
                        and r['existing_valid_bar_count'] and r['missing_historical_windows']) for r in assessments),
                    'returned_shortlist_count': min(limit, len(eligible)),
                },
                'candidates': eligible[:limit], 'assessments': assessments,
                'priority_policy': 'VERIFIED_PARTIAL_THEN_MISSING_SESSIONS_DESC_SYMBOL_ISIN_UUID',
                'priority_is_provider_success_prediction': False,
                'execution_authorized': False, 'provider_requests': 0,
                'publication': 'UNCHANGED', 'recommendation': 'NOT_EVALUATED',
            }
    return await repository._run_blocking_persistence(read)
