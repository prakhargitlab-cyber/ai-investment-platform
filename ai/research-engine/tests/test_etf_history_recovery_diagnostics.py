"""Diagnostics-only explanations of HISTORICAL_RANGE_INCOMPLETE.

Every test here also pins that completeness semantics did not move: an absent session is never
recovered, bad closes are still rejected, listing dates are never inferred, and recovery stays
insert-only. All data is synthetic; no provider or live database is used.
"""
import copy
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.etf_history_recovery import (
    INCOMPLETE_DETAILS, HistoryRecoveryRequest, RecoveryEtfHistoryProvider,
    incomplete_details, missing_session_windows, plan_history_recovery, recover_etf_history,
)
from app.models import MarketPriceObservation
from app.historical_market_data import (
    MAX_REJECTED_CLOSE_DIAGNOSTICS, _ObservationList,
    normalize_rejected_close_diagnostics, rejected_close_diagnostic,
)
from scripts import recover_etf_history as cli
from test_etf_history_recovery import SOURCE_ID, bar, orchestrator, source
from test_etf_ingestion import repo  # noqa: F401  (fixture)
from test_etf_preflight import compatible_dispatch  # noqa: F401  (autouse fixture: arg-forwarding dispatch)

# The November 5 shape: recovery runs after the 2026-11-05 NSE close (10:00Z) and Yahoo has no
# 2026-11-05 candle yet. The persisted NSE calendar (V25) has no exception for that date.
NOV_NOW = datetime(2026, 11, 6, 3, 30, tzinfo=timezone.utc)
RETRIEVED = NOV_NOW + timedelta(minutes=2)
CLOCK = NOV_NOW + timedelta(minutes=5)
START, END = date(2026, 10, 26), date(2026, 11, 6)
D = [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30),
     date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]


def iso(*days):
    return [d.isoformat() for d in days]


class ScriptedProvider:
    """Returns exactly the scripted session dates (UTC-midnight date labels) inside the window."""

    def __init__(self, days, *, observations=None):
        self.days, self.observations, self.calls = list(days), observations, []

    async def closes(self, row, *, start, end, **_):
        self.calls.append((start.date(), end.date()))
        if self.observations is not None:
            return self.observations(row)
        return [MarketPriceObservation(instrument_id=UUID(row['globalInstrumentId']),
            observed_at=datetime.combine(day, time.min, timezone.utc), price=Decimal('100'),
            currency='INR', provider='YAHOO_FINANCE', source_url=bar(row, day).source_url,
            retrieved_at=RETRIEVED) for day in self.days if start.date() <= day < end.date()]


def make_request(rows, start=START, end=END):
    return HistoryRecoveryRequest(source_cycle_id=SOURCE_ID,
        instrument_ids=tuple(UUID(row['globalInstrumentId']) for row in rows), start=start, end=end)


async def run(repo, rows, provider_factory, **kwargs):
    req = make_request(rows, **kwargs)
    result = await recover_etf_history(repo, orchestrator(rows), req, now=NOV_NOW, identity_headers={},
        provider_factory=provider_factory, sleep=AsyncMock(), clock=lambda: CLOCK)
    return result, req


async def run_scripted(repo, rows, days):
    providers = []

    def factory(budget):
        providers.append(ScriptedProvider(days))
        return providers[-1]

    result, req = await run(repo, rows, factory)
    return result['results'][rows[0]['isin']], result, req, providers


def stored_dates(repo):
    return sorted(b.trading_date for b in repo.persistence.load_daily_market_bars({UUID(int=1)}))


# --------------------------------------------------------------------- pure helpers

SESSIONS = set(D)


@pytest.mark.parametrize('present, expected_detail', [
    (D[:-1], ['LATEST_SESSION_NOT_RETURNED']),
    (D[3:], ['PROVIDER_HISTORY_STARTS_AFTER_RANGE_START']),
    ([d for d in D if d not in (D[4], D[5])], ['INTERIOR_SESSIONS_MISSING']),
    (D[3:-1], ['LATEST_SESSION_NOT_RETURNED', 'PROVIDER_HISTORY_STARTS_AFTER_RANGE_START']),
    ([D[2], D[4], D[6]], ['LATEST_SESSION_NOT_RETURNED', 'PROVIDER_HISTORY_STARTS_AFTER_RANGE_START',
                          'INTERIOR_SESSIONS_MISSING']),
    ([D[0], D[1], D[2], D[3], D[4], D[6], D[7], D[8]], ['INTERIOR_SESSIONS_MISSING']),
    ([], ['LATEST_SESSION_NOT_RETURNED']),  # nothing stored: no prefix/interior claim is possible
    (D, []),
])
def test_incomplete_details_reports_every_applicable_condition(present, expected_detail):
    assert incomplete_details(SESSIONS, set(present)) == expected_detail
    assert set(expected_detail) <= set(INCOMPLETE_DETAILS)


def test_incomplete_details_ignores_stored_dates_outside_expected_sessions():
    # A weekend/holiday/out-of-range date can never satisfy an expected session.
    assert incomplete_details(SESSIONS, set(D[:-1]) | {date(2026, 11, 1), date(2026, 10, 20)}) == [
        'LATEST_SESSION_NOT_RETURNED']


def test_missing_session_windows_lists_actual_sessions_not_calendar_days():
    present = {D[2], D[3], D[6]}
    windows = missing_session_windows(SESSIONS, present)
    assert windows == [
        {'start': '2026-11-04', 'end_exclusive': '2026-11-06', 'first_missing_session': '2026-11-04',
         'last_missing_session': '2026-11-05', 'missing_session_count': 2},
        {'start': '2026-10-30', 'end_exclusive': '2026-11-03', 'first_missing_session': '2026-10-30',
         'last_missing_session': '2026-11-02', 'missing_session_count': 2},  # spans the weekend
        {'start': '2026-10-26', 'end_exclusive': '2026-10-28', 'first_missing_session': '2026-10-26',
         'last_missing_session': '2026-10-27', 'missing_session_count': 2},
    ]
    assert missing_session_windows(SESSIONS, SESSIONS) == []


# --------------------------------------------------------------------- the November 5 scenario

@pytest.mark.asyncio
async def test_november_5_missing_latest_session_is_explained_but_never_recovered(repo):
    rows = await source(repo, count=1)
    outcome, result, req, providers = await run_scripted(repo, rows, D[:-1])
    assert outcome['status'] == 'UNRESOLVED' and outcome['reason'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert outcome['recovery_outcome'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert result['status'] != 'COMPLETED_APPROVED_RANGE'
    assert outcome['incomplete_detail'] == ['LATEST_SESSION_NOT_RETURNED']
    assert outcome['expected_latest_completed_session'] == '2026-11-05'
    assert outcome['expected_completed_sessions'] == 9
    assert outcome['provider_first_accepted_date'] == '2026-10-26'
    assert outcome['provider_last_accepted_date'] == '2026-11-04'
    assert outcome['rejected_invalid_close_count'] == 0
    assert outcome['observations'] == outcome['valid_sessions'] == 8
    assert outcome['remaining_windows'] == [{'start': '2026-11-05', 'end_exclusive': '2026-11-06'}]
    assert outcome['missing_session_windows'] == [{'start': '2026-11-05', 'end_exclusive': '2026-11-06',
        'first_missing_session': '2026-11-05', 'last_missing_session': '2026-11-05', 'missing_session_count': 1}]
    assert outcome['recommendation'] == 'NOT_EVALUATED'
    assert outcome['older_history'] == 'UNRESOLVED_OUTSIDE_APPROVED_RANGE'
    window = outcome['windows'][0]
    assert window['accepted_provider_first_date'] == '2026-10-26'
    assert window['accepted_provider_last_date'] == '2026-11-04'
    assert window['rejected_invalid_close_count'] == 0
    assert window['first_date'] == '2026-10-26' and window['last_date'] == '2026-11-04'  # existing fields kept
    assert stored_dates(repo) == D[:-1]  # the absent session is not synthesised
    # Plan with a clock after the bars were retrieved (a plan clock before RETRIEVED would treat the
    # stored bars as FUTURE_HISTORY; that existing read behaviour is intentionally untouched).
    plan = await plan_history_recovery(repo, req, now=CLOCK + timedelta(hours=1))
    assert plan['instruments'][0]['missing_windows'] == [{'start': '2026-11-05', 'end_exclusive': '2026-11-06'}]
    assert 'listing_date' not in outcome and 'canonical_listing_date' not in outcome


@pytest.mark.asyncio
async def test_later_separately_authorised_recovery_fetches_only_the_missing_session(repo):
    rows = await source(repo, count=1)
    await run_scripted(repo, rows, D[:-1])
    outcome, result, req, providers = await run_scripted(repo, rows, D)  # new recovery id, 11-05 now published
    assert providers[0].calls == [(date(2026, 11, 5), date(2026, 11, 6))]
    assert outcome['status'] == 'RECOVERED_APPROVED_RANGE' and result['status'] == 'COMPLETED_APPROVED_RANGE'
    assert outcome['incomplete_detail'] == [] and outcome['missing_session_windows'] == []
    assert outcome['provider_first_accepted_date'] == outcome['provider_last_accepted_date'] == '2026-11-05'
    assert stored_dates(repo) == D


@pytest.mark.asyncio
async def test_fully_returned_range_carries_empty_diagnostics(repo):
    rows = await source(repo, count=1)
    outcome, result, _, _ = await run_scripted(repo, rows, D)
    assert outcome['status'] == 'RECOVERED_APPROVED_RANGE' and outcome['reason'] is None
    assert outcome['incomplete_detail'] == [] and outcome['missing_session_windows'] == []
    assert outcome['remaining_windows'] == []
    assert outcome['expected_latest_completed_session'] == '2026-11-05'
    assert outcome['provider_last_accepted_date'] == '2026-11-05'


# --------------------------------------------------------------------- early history / interior gaps

@pytest.mark.asyncio
async def test_incomplete_early_history_is_reported_without_inferring_a_listing_date(repo):
    rows = await source(repo, count=1)
    outcome, _, _, _ = await run_scripted(repo, rows, D[3:])
    assert outcome['reason'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert outcome['incomplete_detail'] == ['PROVIDER_HISTORY_STARTS_AFTER_RANGE_START']
    assert outcome['provider_first_accepted_date'] == '2026-10-29'
    assert outcome['remaining_windows'] == [{'start': '2026-10-26', 'end_exclusive': '2026-10-29'}]
    assert outcome['missing_session_windows'][0]['missing_session_count'] == 3
    assert 'listing_date' not in outcome and 'canonical_listing_date' not in outcome
    assert stored_dates(repo) == D[3:]  # no backfilled or synthetic early bars


@pytest.mark.asyncio
async def test_early_history_and_missing_latest_session_are_both_reported(repo):
    rows = await source(repo, count=1)
    outcome, _, _, _ = await run_scripted(repo, rows, D[3:-1])
    assert outcome['incomplete_detail'] == ['LATEST_SESSION_NOT_RETURNED',
                                            'PROVIDER_HISTORY_STARTS_AFTER_RANGE_START']
    assert [w['first_missing_session'] for w in outcome['missing_session_windows']] == ['2026-11-05', '2026-10-26']


@pytest.mark.asyncio
async def test_interior_gap_spanning_a_weekend_is_one_actual_session_window(repo):
    rows = await source(repo, count=1)
    outcome, _, _, _ = await run_scripted(repo, rows, [d for d in D if d not in (D[4], D[5])])
    assert outcome['incomplete_detail'] == ['INTERIOR_SESSIONS_MISSING']
    assert outcome['remaining_windows'] == [{'start': '2026-10-30', 'end_exclusive': '2026-11-03'}]
    assert outcome['missing_session_windows'] == [{'start': '2026-10-30', 'end_exclusive': '2026-11-03',
        'first_missing_session': '2026-10-30', 'last_missing_session': '2026-11-02', 'missing_session_count': 2}]
    assert outcome['expected_completed_sessions'] == 9 and outcome['valid_sessions'] == 7


@pytest.mark.asyncio
async def test_all_three_conditions_are_reported_together(repo):
    rows = await source(repo, count=1)
    outcome, _, _, _ = await run_scripted(repo, rows, [D[2], D[4], D[6]])
    assert outcome['incomplete_detail'] == list(INCOMPLETE_DETAILS)
    assert [w['missing_session_count'] for w in outcome['missing_session_windows']] == [2, 1, 1, 2]
    assert outcome['remaining_windows'] == [
        {'start': w['start'], 'end_exclusive': w['end_exclusive']} for w in outcome['missing_session_windows']]


# --------------------------------------------------------------------- invalid closes

def yahoo_frame(closes):
    """DataFrame shaped like yfinance daily history: exchange-midnight index labels."""
    index = pd.DatetimeIndex([pd.Timestamp(day.isoformat(), tz='Asia/Kolkata') for day in closes])
    return pd.DataFrame({'Close': pd.Series(list(closes.values()), index=index, dtype=object)})


class FixedClockProvider(RecoveryEtfHistoryProvider):
    """The real recovery provider (real close-rejection code) over a scripted Yahoo history."""

    def __init__(self, closes):
        frame = yahoo_frame(closes)
        super().__init__(lambda symbol: SimpleNamespace(history=lambda **kwargs: frame))

    def _verify_identity_or_raise(self, instrument, ticker, access):
        return None

    async def closes(self, instrument, *, start, end, acquisition_context=None):
        result = await super().closes(instrument, start=start, end=end, acquisition_context=acquisition_context)
        fixed = copy.copy(result)  # keeps the diagnostic count; only pins the retrieval clock
        fixed[:] = [item.model_copy(update={'retrieved_at': RETRIEVED}) for item in result]
        return fixed


def closes_with(bad):
    return {day: bad.get(day, 100.0 + index) for index, day in enumerate(D)}


BAD_VALUES = [float('nan'), float('inf'), float('-inf'), 0.0, -5.0, None]


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_value', BAD_VALUES, ids=repr)
async def test_invalid_latest_session_close_is_rejected_counted_and_leaves_the_session_missing(repo, bad_value):
    rows = await source(repo, count=1)
    result, _ = await run(repo, rows, lambda budget: FixedClockProvider(closes_with({D[-1]: bad_value})))
    outcome = result['results'][rows[0]['isin']]
    assert outcome['rejected_invalid_close_count'] == 1
    assert outcome['windows'][0]['rejected_invalid_close_count'] == 1
    assert outcome['windows'][0]['status'] == 'SUCCESS_WITH_DATA'  # existing silent-drop behaviour kept
    assert outcome['status'] == 'UNRESOLVED' and outcome['reason'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert outcome['incomplete_detail'] == ['LATEST_SESSION_NOT_RETURNED']
    assert outcome['provider_last_accepted_date'] == '2026-11-04'
    assert outcome['remaining_windows'] == [{'start': '2026-11-05', 'end_exclusive': '2026-11-06'}]
    assert stored_dates(repo) == D[:-1]


@pytest.mark.asyncio
async def test_invalid_interior_and_latest_closes_are_both_counted_and_both_explained(repo):
    rows = await source(repo, count=1)
    result, _ = await run(repo, rows, lambda budget: FixedClockProvider(
        closes_with({D[2]: float('nan'), D[-1]: 0.0, D[4]: -1.0})))
    outcome = result['results'][rows[0]['isin']]
    assert outcome['rejected_invalid_close_count'] == 3
    assert outcome['incomplete_detail'] == ['LATEST_SESSION_NOT_RETURNED', 'INTERIOR_SESSIONS_MISSING']
    assert stored_dates(repo) == [d for d in D if d not in (D[2], D[4], D[-1])]


@pytest.mark.asyncio
async def test_provider_counts_invalid_closes_and_still_returns_a_plain_list():
    provider = FixedClockProvider(closes_with({D[1]: float('nan'), D[3]: None, D[5]: 0.0, D[8]: -2.0}))
    observations = await provider.closes({'globalInstrumentId': str(UUID(int=1)), 'currency': 'INR',
        'structuredProviderTicker': 'SYNTHETF1.NS'}, start=datetime.combine(START, time.min, timezone.utc),
        end=datetime.combine(END, time.min, timezone.utc))
    assert isinstance(observations, list) and len(observations) == 5
    assert observations.rejected_close_count == 4
    assert [o.observed_at.astimezone(timezone(timedelta(hours=5, minutes=30))).date() for o in observations] == [
        D[0], D[2], D[4], D[6], D[7]]


@pytest.mark.asyncio
async def test_invalid_observation_reaching_the_recovery_boundary_still_fails_the_window(repo):
    """The existing hard rejection is unchanged; the count only explains it."""
    rows = await source(repo, count=1)

    def observations(row):
        good = [MarketPriceObservation(instrument_id=UUID(row['globalInstrumentId']),
            observed_at=datetime.combine(day, time.min, timezone.utc), price=Decimal('100'), currency='INR',
            provider='YAHOO_FINANCE', source_url=bar(row, day).source_url, retrieved_at=RETRIEVED)
            for day in D[:-1]]
        bad = MarketPriceObservation.model_construct(instrument_id=UUID(row['globalInstrumentId']),
            observed_at=datetime.combine(D[-1], time.min, timezone.utc), price=Decimal('NaN'), currency='INR',
            provider='YAHOO_FINANCE', source_url=bar(row, D[-1]).source_url, retrieved_at=RETRIEVED)
        return good + [bad]

    result, _ = await run(repo, rows, lambda budget: ScriptedProvider([], observations=observations))
    outcome = result['results'][rows[0]['isin']]
    assert outcome['windows'][0]['status'] == 'TECHNICAL_FAILURE'
    assert outcome['windows'][0]['rejected_invalid_close_count'] == 1
    assert outcome['rejected_invalid_close_count'] == 1
    assert outcome['recovery_outcome'] == 'TECHNICAL_FAILURE'
    assert outcome['rejected_close_diagnostics'] == [rejected_entry()]
    assert outcome['rejected_close_diagnostics_omitted'] == 0
    assert outcome['incomplete_detail'] == []  # a technical failure is not relabelled as incomplete history
    assert outcome['missing_session_windows'][0]['missing_session_count'] == 9
    assert stored_dates(repo) == []


# --------------------------------------------------------------------- sanitized CLI report

@pytest.mark.asyncio
async def test_sanitized_report_exposes_only_whitelisted_diagnostics(repo):
    rows = await source(repo, count=1)
    outcome, result, req, _ = await run_scripted(repo, rows, D[3:-1])
    report = cli.sanitized_report(result, req, apply=True)
    row = report['instruments'][0]
    assert row['recovery_outcome'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert row['incomplete_detail'] == ['LATEST_SESSION_NOT_RETURNED', 'PROVIDER_HISTORY_STARTS_AFTER_RANGE_START']
    assert row['expected_latest_completed_session'] == '2026-11-05'
    assert row['expected_completed_sessions'] == 9 and row['rejected_invalid_close_count'] == 0
    assert row['provider_first_accepted_date'] == '2026-10-29' and row['provider_last_accepted_date'] == '2026-11-04'
    assert row['remaining_windows'] == [{'start': '2026-11-05', 'end_exclusive': '2026-11-06'},
                                        {'start': '2026-10-26', 'end_exclusive': '2026-10-29'}]
    assert row['missing_session_windows'][0] == {'start': '2026-11-05', 'end_exclusive': '2026-11-06',
        'first_missing_session': '2026-11-05', 'last_missing_session': '2026-11-05', 'missing_session_count': 1}
    window = row['windows'][0]
    assert window['accepted_provider_first_date'] == '2026-10-29' and window['rejected_invalid_close_count'] == 0


def test_sanitized_report_filters_unknown_diagnostic_values_and_omits_them_for_old_payloads():
    instrument = str(UUID(int=1))
    req = HistoryRecoveryRequest(source_cycle_id=SOURCE_ID, instrument_ids=(UUID(instrument),), start=START, end=END)
    hostile = {'instrument_id': instrument, 'status': 'UNRESOLVED', 'reason': 'HISTORICAL_RANGE_INCOMPLETE',
        'incomplete_detail': ['INTERIOR_SESSIONS_MISSING', 'https://evil.example/token', 'RAW ERROR BODY'],
        'missing_session_windows': [{'start': '2026-11-05', 'end_exclusive': 'not a date',
            'first_missing_session': '2026-11-05', 'last_missing_session': '<script>', 'missing_session_count': -4,
            'raw': 'secret'}, 'junk'],
        'expected_latest_completed_session': 'password=1', 'rejected_invalid_close_count': 'many',
        'windows': [{'start': '2026-11-05', 'end_exclusive': '2026-11-06', 'status': 'SUCCESS_EMPTY',
                     'accepted_provider_first_date': 'bad', 'rejected_invalid_close_count': 2.5}]}
    legacy = {'instrument_id': instrument, 'status': 'UNRESOLVED', 'reason': 'HISTORICAL_RANGE_INCOMPLETE',
              'remaining_windows': [{'start': '2026-11-05', 'end_exclusive': '2026-11-06'}], 'windows': []}
    row = cli.sanitized_report({'recovery_id': str(UUID(int=2)), 'results': {'a': hostile}}, req, apply=True)['instruments'][0]
    assert row['incomplete_detail'] == ['INTERIOR_SESSIONS_MISSING']
    assert row['missing_session_windows'] == [{'start': '2026-11-05', 'end_exclusive': None,
        'first_missing_session': '2026-11-05', 'last_missing_session': None, 'missing_session_count': None}]
    assert row['expected_latest_completed_session'] is None and row['rejected_invalid_close_count'] is None
    assert row['windows'][0]['accepted_provider_first_date'] is None
    assert row['windows'][0]['rejected_invalid_close_count'] is None
    old = cli.sanitized_report({'recovery_id': str(UUID(int=2)), 'results': {'a': legacy}}, req, apply=True)['instruments'][0]
    assert old['recovery_outcome'] == 'HISTORICAL_RANGE_INCOMPLETE'
    for key in ('incomplete_detail', 'missing_session_windows', 'expected_latest_completed_session',
                'provider_first_accepted_date', 'provider_last_accepted_date', 'rejected_invalid_close_count',
                'expected_completed_sessions'):
        assert key not in old


# ------------------------------------------------------- bounded rejected-row attribution

def rejected_entry(day='2026-11-05', reason='CLOSE_NAN'):
    return {'trading_date': day, 'date_status': 'KNOWN' if day else 'UNKNOWN', 'reason': reason}


class RawRowsProvider(RecoveryEtfHistoryProvider):
    """The real parsing loop over arbitrary, entirely synthetic provider rows."""

    def __init__(self, rows):
        self.rows, self.history_calls = rows, 0
        super().__init__(lambda symbol: SimpleNamespace(history=self.history))

    def history(self, **kwargs):
        self.history_calls += 1
        return SimpleNamespace(iterrows=lambda: iter(self.rows))

    def _verify_identity_or_raise(self, instrument, ticker, access):
        return None


async def raw_observations(rows):
    provider = RawRowsProvider(rows)
    result = await provider.closes({'globalInstrumentId': str(UUID(int=1)), 'currency': 'INR',
        'structuredProviderTicker': 'SYNTHETF1.NS'},
        start=datetime.combine(START, time.min, timezone.utc),
        end=datetime.combine(END, time.min, timezone.utc))
    assert provider.history_calls == 1  # diagnostics never cause another request
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('bad,reason', [
    (float('nan'), 'CLOSE_NAN'), (Decimal('sNaN'), 'CLOSE_NAN'),
    (0, 'CLOSE_ZERO'), (Decimal('-0.0'), 'CLOSE_ZERO'), (-5, 'CLOSE_NEGATIVE'),
    (float('inf'), 'CLOSE_INFINITE'), (float('-inf'), 'CLOSE_INFINITE'),
    ('malformed-close-password=DO_NOT_EMIT', 'CLOSE_MALFORMED'), (None, 'CLOSE_MISSING'),
])
async def test_rejected_provider_closes_keep_reason_date_and_existing_count(bad, reason, caplog):
    label = pd.Timestamp('2026-11-05', tz='Asia/Kolkata')
    good = pd.Timestamp('2026-11-04', tz='Asia/Kolkata')
    observations = await raw_observations([(good, {'Close': 123}), (label, {'Close': bad})])
    assert isinstance(observations, list) and len(observations) == 1
    assert observations[0].price == Decimal('123') and observations[0].observed_at == good
    assert observations.rejected_close_count == 1
    assert observations.rejected_close_diagnostics == [rejected_entry(reason=reason)]
    assert observations.rejected_close_diagnostics_omitted == 0
    assert 'DO_NOT_EMIT' not in json.dumps(observations.rejected_close_diagnostics)
    assert 'DO_NOT_EMIT' not in caplog.text


@pytest.mark.asyncio
async def test_missing_close_column_is_rejected_without_timestamp_loss():
    observations = await raw_observations([(pd.Timestamp('2026-11-05', tz='UTC'), {})])
    assert observations == [] and observations.rejected_close_count == 1
    assert observations.rejected_close_diagnostics == [rejected_entry(reason='CLOSE_MISSING')]


@pytest.mark.asyncio
@pytest.mark.parametrize('label,day', [
    (pd.Timestamp('2026-11-05', tz='UTC'), '2026-11-05'),
    (pd.Timestamp('2026-11-05', tz='Asia/Kolkata'), '2026-11-05'),
    (pd.Timestamp('2026-11-04T18:30:00Z'), '2026-11-05'),
    (datetime(2026, 11, 5, tzinfo=timezone.utc), '2026-11-05'),
    (datetime(2026, 11, 5), None),
    (date(2026, 11, 5), None),
    (pd.Timestamp('2026-11-05T10:00:00Z'), None),  # no calendar at this diagnostic boundary
    (pd.Timestamp('2026-11-04T23:59:00Z'), None),
    (pd.Timestamp('2026-11-05T00:00:00.000000001Z'), None),
    (datetime(2026, 11, 5, tzinfo=timezone.utc, fold=1), None),
    ('2026-11-05T00:00:00Z', None),  # raw strings are never interpreted
    ('malformed-timestamp-token=DO_NOT_EMIT', None),
    (1762300000, None), (None, None), (pd.NaT, None), (float('nan'), None),
])
async def test_rejected_date_labels_are_known_only_when_safely_interpretable(label, day):
    observations = await raw_observations([(label, {'Close': float('nan')})])
    assert observations == [] and observations.rejected_close_count == 1
    assert observations.rejected_close_diagnostics == [rejected_entry(day)]
    assert 'DO_NOT_EMIT' not in json.dumps(observations.rejected_close_diagnostics)


def test_rejected_date_failure_is_optional_and_does_not_leak_timestamp():
    # A non-date object is never parsed, inspected or converted to a string.
    class PrivateTimestamp:
        def __str__(self):
            raise AssertionError('raw timestamps must not be stringified')
    assert rejected_close_diagnostic(PrivateTimestamp(), 'CLOSE_NAN') == rejected_entry(None)


@pytest.mark.asyncio
async def test_november_5_rejection_propagates_without_changing_completeness(repo):
    rows = await source(repo, count=1)
    result, req = await run(repo, rows, lambda budget: FixedClockProvider(closes_with({D[-1]: float('nan')})))
    outcome = result['results'][rows[0]['isin']]
    report = cli.sanitized_report(result, req, apply=True)['instruments'][0]
    for value in (outcome, outcome['windows'][0], report, report['windows'][0]):
        assert value['rejected_close_diagnostics'] == [rejected_entry()]
        assert value['rejected_close_diagnostics_omitted'] == 0
        assert value['rejected_invalid_close_count'] == 1
    assert report['windows'][0]['provider_status'] == 'SUCCESS_WITH_DATA'
    assert outcome['reason'] == outcome['recovery_outcome'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert outcome['remaining_windows'] == [{'start': '2026-11-05', 'end_exclusive': '2026-11-06'}]
    assert stored_dates(repo) == D[:-1]


@pytest.mark.asyncio
async def test_absent_november_5_has_no_invented_rejected_row(repo):
    rows = await source(repo, count=1)
    outcome, result, req, _ = await run_scripted(repo, rows, D[:-1])
    report = cli.sanitized_report(result, req, apply=True)['instruments'][0]
    for value in (outcome, outcome['windows'][0], report, report['windows'][0]):
        assert value['rejected_close_diagnostics'] == []
        assert value['rejected_close_diagnostics_omitted'] == 0
        assert value['rejected_invalid_close_count'] == 0
    assert outcome['reason'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert stored_dates(repo) == D[:-1]


@pytest.mark.asyncio
async def test_rejected_november_4_does_not_attribute_an_absent_november_5_row(repo):
    rows = await source(repo, count=1)
    closes = closes_with({D[-2]: 0})
    closes.pop(D[-1])
    result, _ = await run(repo, rows, lambda budget: FixedClockProvider(closes))
    outcome = result['results'][rows[0]['isin']]
    assert outcome['rejected_close_diagnostics'] == [rejected_entry('2026-11-04', 'CLOSE_ZERO')]
    assert outcome['rejected_close_diagnostics_omitted'] == 0
    assert outcome['reason'] == 'HISTORICAL_RANGE_INCOMPLETE' and stored_dates(repo) == D[:-2]


@pytest.mark.asyncio
async def test_provider_diagnostics_are_bounded_counts_exact_and_instances_isolated():
    limit = MAX_REJECTED_CLOSE_DIAGNOSTICS
    count = limit + 9
    rows = [(pd.Timestamp('2026-11-04', tz='UTC'), {'Close': float('nan')})] * count
    observations = await raw_observations(rows)
    assert observations.rejected_close_count == count
    assert observations.rejected_close_diagnostics == [rejected_entry('2026-11-04')] * limit
    assert observations.rejected_close_diagnostics_omitted == 9
    other = _ObservationList()
    assert other.rejected_close_count == 0 and other.rejected_close_diagnostics == []
    assert other.rejected_close_diagnostics_omitted == 0
    copied = copy.copy(observations)
    assert copied.rejected_close_diagnostics == observations.rejected_close_diagnostics
    assert copied.rejected_close_count == count


def test_rejection_aggregation_and_cli_bound_output_without_losing_omissions():
    from app.etf_history_recovery import recovery_diagnostics
    entries = [rejected_entry()] * MAX_REJECTED_CLOSE_DIAGNOSTICS
    windows = [{'rejected_close_diagnostics': entries, 'rejected_close_diagnostics_omitted': 5,
                'rejected_invalid_close_count': MAX_REJECTED_CLOSE_DIAGNOSTICS + 5}] * 2
    combined = recovery_diagnostics({D[-1]}, set(), windows, 'HISTORICAL_RANGE_INCOMPLETE')
    assert combined['rejected_invalid_close_count'] == 74
    assert len(combined['rejected_close_diagnostics']) == 32
    assert combined['rejected_close_diagnostics_omitted'] == 42
    instrument = str(UUID(int=1))
    req = HistoryRecoveryRequest(SOURCE_ID, (UUID(instrument),), START, END)
    result = {'recovery_id': str(UUID(int=2)), 'results': {'row': {
        'instrument_id': instrument, 'reason': 'HISTORICAL_RANGE_INCOMPLETE',
        'windows': windows, **combined}}}
    sanitized = cli.sanitized_report(result, req, apply=True)['instruments'][0]
    assert sanitized['rejected_close_diagnostics'] == entries
    assert sanitized['rejected_close_diagnostics_omitted'] == 42
    assert all(len(w['rejected_close_diagnostics']) == 32 for w in sanitized['windows'])
    assert all(w['rejected_close_diagnostics_omitted'] == 5 for w in sanitized['windows'])


def test_rejection_sanitization_drops_raw_values_and_never_promotes_unknown_dates():
    secret = 'token=DO_NOT_EMIT_PRIVATE_PRICE_OR_PAYLOAD'
    dirty = [
        {**rejected_entry(), 'price': secret, 'payload': secret, 'timestamp': secret},
        {'reason': 'CLOSE_ZERO', 'date_status': 'KNOWN', 'trading_date': secret},
        {'reason': 'CLOSE_NEGATIVE', 'date_status': 'UNKNOWN', 'trading_date': '2026-11-05'},
        {'reason': 'CLOSE_NAN', 'date_status': 'KNOWN', 'trading_date': '20261105'},
        {'reason': secret, 'date_status': 'KNOWN', 'trading_date': '2026-11-05'}, secret,
    ]
    clean = normalize_rejected_close_diagnostics(dirty)
    assert clean == [rejected_entry(), rejected_entry(None, 'CLOSE_ZERO'),
                     rejected_entry(None, 'CLOSE_NEGATIVE'), rejected_entry(None)]
    assert secret not in json.dumps(clean)
    assert all(set(item) == {'reason', 'date_status', 'trading_date'} for item in clean)
    instrument = str(UUID(int=1))
    req = HistoryRecoveryRequest(SOURCE_ID, (UUID(instrument),), START, END)
    result = {'recovery_id': str(UUID(int=2)), 'results': {'row': {
        'instrument_id': instrument, 'rejected_close_diagnostics': dirty * 20,
        'rejected_close_diagnostics_omitted': secret,
        'windows': [{'rejected_close_diagnostics': dirty * 20,
                     'rejected_close_diagnostics_omitted': True, 'raw': secret}]}}}
    report = cli.sanitized_report(result, req, apply=True)['instruments'][0]
    assert len(report['rejected_close_diagnostics']) == 32
    assert report['rejected_close_diagnostics_omitted'] is None
    assert report['windows'][0]['rejected_close_diagnostics_omitted'] is None
    assert secret not in json.dumps(report)


def test_legacy_rejection_count_does_not_invent_per_date_diagnostics():
    instrument = str(UUID(int=1))
    req = HistoryRecoveryRequest(SOURCE_ID, (UUID(instrument),), START, END)
    result = {'recovery_id': str(UUID(int=2)), 'results': {'row': {
        'instrument_id': instrument, 'rejected_invalid_close_count': 1,
        'windows': [{'rejected_invalid_close_count': 1}]}}}
    row = cli.sanitized_report(result, req, apply=True)['instruments'][0]
    for value in (row, row['windows'][0]):
        assert value['rejected_invalid_close_count'] == 1
        assert 'rejected_close_diagnostics' not in value
        assert 'rejected_close_diagnostics_omitted' not in value


@pytest.mark.parametrize('label', [
    datetime(2026, 10, 25, 1, tzinfo=ZoneInfo('Europe/London'), fold=0),
    datetime(2026, 10, 25, 1, tzinfo=ZoneInfo('Europe/London'), fold=1),
    datetime(2026, 10, 25, 2, tzinfo=ZoneInfo('Europe/Berlin'), fold=0),
    datetime(2026, 10, 25, 2, tzinfo=ZoneInfo('Europe/Berlin'), fold=1),
    pd.Timestamp(datetime(2026, 10, 25, 1, tzinfo=ZoneInfo('Europe/London'), fold=0)),
    pd.Timestamp(datetime(2026, 10, 25, 1, tzinfo=ZoneInfo('Europe/London'), fold=1)),
    pd.Timestamp('2026-10-25T01:00:00').tz_localize('Europe/London', ambiguous=True),
    pd.Timestamp('2026-10-25T01:00:00').tz_localize('Europe/London', ambiguous=False),
])
def test_review_both_folds_of_dst_ambiguous_wall_times_remain_unknown(label):
    assert rejected_close_diagnostic(label, 'CLOSE_NAN') == rejected_entry(None)


@pytest.mark.parametrize('label,day', [
    (datetime(2026, 7, 1, 1, tzinfo=ZoneInfo('Europe/London')), '2026-07-01'),
    (datetime(2026, 11, 5, tzinfo=ZoneInfo('Europe/London')), '2026-11-05'),
    (datetime(2026, 11, 5, 9, tzinfo=ZoneInfo('Asia/Tokyo')), '2026-11-05'),
    (pd.Timestamp('2026-11-04T18:30:00Z'), '2026-11-05'),
])
def test_review_unambiguous_converted_midnight_labels_keep_the_existing_date(label, day):
    assert rejected_close_diagnostic(label, 'CLOSE_NAN') == rejected_entry(day)


@pytest.mark.parametrize('legacy_first', [False, True])
def test_review_mixed_legacy_and_new_windows_account_for_all_known_omissions(legacy_first):
    from app.etf_history_recovery import recovery_diagnostics
    legacy = {'rejected_invalid_close_count': 3}
    current = {'rejected_invalid_close_count': 1, 'rejected_close_diagnostics': [rejected_entry()],
               'rejected_close_diagnostics_omitted': 0}
    windows = [legacy, current] if legacy_first else [current, legacy]
    combined = recovery_diagnostics({D[-1]}, set(), windows, 'HISTORICAL_RANGE_INCOMPLETE')
    assert combined['rejected_invalid_close_count'] == 4
    assert combined['rejected_close_diagnostics'] == [rejected_entry()]
    assert combined['rejected_close_diagnostics_omitted'] == 3
    req = HistoryRecoveryRequest(SOURCE_ID, (UUID(int=1),), START, END)
    result = {'recovery_id': str(UUID(int=2)), 'results': {'row': {
        'instrument_id': str(UUID(int=1)), 'windows': windows, **combined}}}
    report = cli.sanitized_report(result, req, apply=True)['instruments'][0]
    assert report['rejected_invalid_close_count'] == 4
    assert report['rejected_close_diagnostics_omitted'] == 3
    legacy_report = report['windows'][0 if legacy_first else 1]
    assert 'rejected_close_diagnostics' not in legacy_report


@pytest.mark.parametrize('legacy', [{}, {'rejected_invalid_close_count': None}])
def test_review_mixed_windows_do_not_claim_zero_unknown_legacy_omissions(legacy):
    from app.etf_history_recovery import rejected_close_diagnostics_report
    current = {'rejected_invalid_close_count': 1, 'rejected_close_diagnostics': [rejected_entry()],
               'rejected_close_diagnostics_omitted': 0}
    report = rejected_close_diagnostics_report([legacy, current])
    assert report['rejected_close_diagnostics'] == [rejected_entry()]
    assert report['rejected_close_diagnostics_omitted'] is None


@pytest.mark.parametrize('counter,omitted', [({}, None), ({'rejected_invalid_close_count': 3}, 2)])
def test_review_absent_omission_counter_needs_an_authoritative_total(counter, omitted):
    from app.etf_history_recovery import rejected_close_diagnostics_report
    report = rejected_close_diagnostics_report([{'rejected_close_diagnostics': [rejected_entry()], **counter}])
    assert report['rejected_close_diagnostics'] == [rejected_entry()]
    assert report['rejected_close_diagnostics_omitted'] == omitted


@pytest.mark.asyncio
@pytest.mark.parametrize('count,omitted', [(40, 8), (2, None)])
async def test_review_uncapped_optional_metadata_never_hides_overflow(repo, count, omitted):
    rows = await source(repo, count=1)

    class MetadataProvider(FixedClockProvider):
        async def closes(self, *args, **kwargs):
            observations = await super().closes(*args, **kwargs)
            observations.rejected_close_count = count
            observations.rejected_close_diagnostics = [rejected_entry()] * 40
            observations.rejected_close_diagnostics_omitted = 0
            return observations

    result, req = await run(repo, rows, lambda budget: MetadataProvider(closes_with({})))
    outcome = result['results'][rows[0]['isin']]
    report = cli.sanitized_report(result, req, apply=True)['instruments'][0]
    for value in (outcome, outcome['windows'][0], report, report['windows'][0]):
        assert value['rejected_invalid_close_count'] == count
        assert len(value['rejected_close_diagnostics']) == 32
        assert value['rejected_close_diagnostics_omitted'] == omitted
    assert outcome['status'] == 'RECOVERED_APPROVED_RANGE'
    assert stored_dates(repo) == D


@pytest.mark.asyncio
@pytest.mark.parametrize('broken_field', ['rejected_close_diagnostics', 'rejected_close_diagnostics_omitted'])
@pytest.mark.parametrize('count', [0, 3])
async def test_review_unreadable_optional_metadata_cannot_fail_valid_acquisition(repo, broken_field, count, caplog):
    rows = await source(repo, count=1)
    secret = 'PRIVATE_OPTIONAL_METADATA_DO_NOT_EMIT'

    class BrokenMetadata(list):
        rejected_close_count = count

        @property
        def rejected_close_diagnostics(self):
            if broken_field == 'rejected_close_diagnostics':
                raise ValueError(secret)
            return [rejected_entry()] * count

        @property
        def rejected_close_diagnostics_omitted(self):
            if broken_field == 'rejected_close_diagnostics_omitted':
                raise ValueError(secret)
            return 0

    def observations(row):
        return BrokenMetadata([MarketPriceObservation(instrument_id=UUID(row['globalInstrumentId']),
            observed_at=datetime.combine(day, time.min, timezone.utc), price=Decimal('100'), currency='INR',
            provider='YAHOO_FINANCE', source_url=bar(row, day).source_url, retrieved_at=RETRIEVED) for day in D])

    result, req = await run(repo, rows, lambda budget: ScriptedProvider([], observations=observations))
    outcome = result['results'][rows[0]['isin']]
    assert outcome['status'] == 'RECOVERED_APPROVED_RANGE'
    assert outcome['persisted_bar_count'] == len(D) and stored_dates(repo) == D
    assert outcome['rejected_invalid_close_count'] == count
    assert outcome['rejected_close_diagnostics'] == []
    assert outcome['rejected_close_diagnostics_omitted'] == count
    assert secret not in json.dumps(cli.sanitized_report(result, req, apply=True))
    assert secret not in caplog.text
