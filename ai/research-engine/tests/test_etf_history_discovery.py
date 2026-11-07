"""Synthetic database-only discovery: no live market data or recovery calls."""
import copy
from datetime import date, datetime, timedelta, timezone
import json
import socket
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.cycle_checkpoint import CycleCheckpoint
from app.etf_history_discovery import discover_etf_history_recovery
from app.etf_history_recovery import RECOVERY_SLOT
from test_etf_history_recovery import (
    END, REASON, SOURCE_ID, START, bar, request, source,
)
from test_etf_ingestion import NOW, repo
from test_etf_preflight import compatible_dispatch


async def discovery(repo, **kwargs):
    return await discover_etf_history_recovery(repo, SOURCE_ID, start=START, end=END, now=NOW, **kwargs)


def change_progress(store, row, **values):
    allowed = {'state', 'disposition', 'failure_reason', 'payload'}
    assert values and values.keys() <= allowed
    with store._connection:
        store._connection.execute('UPDATE global_opportunity_cycle_progress SET '
            + ', '.join(k + ' = ?' for k in values)
            + ' WHERE cycle_id = ? AND phase = ? AND global_instrument_id = ?',
            (*values.values(), SOURCE_ID, 'ETF_ONBOARD', row['isin']))


def change_universe(store, transform):
    selection = copy.deepcopy(store.cycle_run(SOURCE_ID)['selection'])
    transform(selection['universe'])
    with store._connection:
        store._connection.execute('UPDATE global_opportunity_cycle_run SET selection = ? WHERE cycle_id = ?',
                                  (json.dumps(selection), SOURCE_ID))


async def prior_recovery(repo, rows, *, status='FAILED', reason='HISTORICAL_RANGE_INCOMPLETE'):
    key = str(UUID(int=30000))
    store = repo.persistence
    _, created = store.create_cycle_run(key, request(rows).parameters(), market=RECOVERY_SLOT)
    assert created and store.claim_cycle_run(key, 'recovery-owner')
    cp = await CycleCheckpoint(store, key, 'recovery-owner', run_blocking=repo._run_blocking_persistence).load()
    await cp.save_selection({'status': 'PARTIAL_FAILURE', 'results': {
        row['isin']: {'instrument_id': row['globalInstrumentId'], 'status': 'UNRESOLVED',
                      'reason': reason, 'observations': 1}
        for row in rows}})
    store.update_cycle_run(key, 'recovery-owner', status=status)
    return key


@pytest.mark.asyncio
async def test_discovery_is_deterministic_independent_of_source_order(repo):
    await source(repo, count=7)
    first = await discovery(repo)
    change_universe(repo.persistence, lambda rows: rows.reverse())
    second = await discovery(repo)
    assert first == second
    assert len(first['candidates']) == 5
    assert first['summary'] == dict(source_universe=7, original_failed_history_candidates=7,
        eligible_candidates=7, excluded_candidates=0, partially_recovered_candidates=0,
        returned_shortlist_count=5)


@pytest.mark.asyncio
async def test_stable_symbol_isin_uuid_tie_breaking(repo):
    rows = await source(repo, count=12)
    change_universe(repo.persistence, lambda universe: universe.reverse())
    result = await discovery(repo)
    expected = sorted(rows, key=lambda r: (r['primarySymbol'], r['isin'], r['globalInstrumentId']))
    assert [r['instrument_id'] for r in result['candidates']] == [r['globalInstrumentId'] for r in expected[:5]]
    assert [r['recommended_priority'] for r in result['candidates']] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, 6, 50, True, 1.0, None])
async def test_shortlist_limit_is_strict_before_any_database_read(repo, limit, monkeypatch):
    dispatch = Mock(side_effect=AssertionError('invalid limits must not reach database'))
    monkeypatch.setattr(repo, '_run_blocking_persistence', dispatch)
    with pytest.raises(ValueError, match='ETF_DISCOVERY_LIMIT_OUT_OF_BOUNDS'):
        await discovery(repo, limit=limit)
    dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [1, 3, 5])
async def test_shortlist_bounded_without_discarding_assessments(repo, limit):
    await source(repo, count=8)
    result = await discovery(repo, limit=limit)
    assert len(result['candidates']) == limit
    assert len(result['assessments']) == result['summary']['eligible_candidates'] == 8


@pytest.mark.asyncio
async def test_only_original_technical_history_failure_is_eligible(repo):
    rows = await source(repo, count=3)
    change_progress(repo.persistence, rows[1], state='EVIDENCE_UNAVAILABLE', disposition='NO_VERIFIED_HISTORY')
    change_progress(repo.persistence, rows[2], state='COMPLETED', disposition='SUCCESS_WITH_DATA',
                    failure_reason=None, payload='{}')
    result = await discovery(repo)
    assert [r['instrument_id'] for r in result['candidates']] == [rows[0]['globalInstrumentId']]
    assert result['summary']['original_failed_history_candidates'] == 2
    assert result['assessments'][1]['exclusion_reason'] == 'ETF_RECOVERY_ONLY_FAILED_HISTORY_ALLOWED'


@pytest.mark.asyncio
async def test_canonical_identity_conflict_excluded_by_shared_gate(repo):
    await source(repo, count=1)
    def conflict(universe):
        universe[0]['canonical']['isin'] = 'IN0000000099'
    change_universe(repo.persistence, conflict)
    result = await discovery(repo)
    assert result['candidates'] == []
    assert result['assessments'][0]['exclusion_reason'] == 'ETF_RECOVERY_CANONICAL_IDENTITY_NOT_VERIFIED'


@pytest.mark.asyncio
async def test_duplicate_canonical_identity_excluded(repo):
    await source(repo, count=1)
    change_universe(repo.persistence, lambda universe: universe.append(copy.deepcopy(universe[0])))
    result = await discovery(repo)
    assert result['candidates'] == []
    assert len(result['assessments']) == 1
    assert result['assessments'][0]['exclusion_reason'] == 'ETF_RECOVERY_AMBIGUOUS_OR_UNKNOWN_CANONICAL_ID'


@pytest.mark.asyncio
async def test_nv20_index_identity_conflict_remains_blocked(repo):
    await source(repo, count=1, reason='VERIFIED_HISTORICAL_IDENTITY_MISMATCH:QUOTE_TYPE_INDEX')
    def nv20(universe):
        universe[0]['symbol'] = universe[0]['canonical']['primarySymbol'] = 'NV20'
        universe[0]['canonical']['providerMappings'][0]['providerSymbol'] = 'NV20'
    change_universe(repo.persistence, nv20)
    result = await discovery(repo)
    row = result['assessments'][0]
    assert not result['candidates'] and row['primary_symbol'] == 'NV20'
    assert row['original_failure_classification'] == 'VERIFIED_HISTORICAL_IDENTITY_MISMATCH'
    assert row['exclusion_reason'] == 'ETF_RECOVERY_UNRESOLVED_PROVIDER_IDENTITY_CONFLICT'


@pytest.mark.asyncio
async def test_zero_bars_remain_real_missing_sessions_not_confirmed_absence(repo):
    await source(repo, count=1, reason='YAHOO_EXPLICIT_CHART_ERROR:synthetic private provider body')
    row = (await discovery(repo))['candidates'][0]
    assert row['existing_valid_bar_count'] == 0
    assert row['first_available_historical_date'] is row['last_available_historical_date'] is None
    assert row['missing_completed_sessions'] == row['expected_completed_sessions'] == 5
    assert row['missing_historical_windows'] == [{'start': START.isoformat(), 'end_exclusive': END.isoformat()}]
    assert row['original_failure_classification'] == 'YAHOO_EXPLICIT_CHART_ERROR'
    assert 'private provider body' not in json.dumps(row)


@pytest.mark.asyncio
async def test_verified_partial_recovery_with_gap_has_priority_without_reusing_run(repo):
    rows = await source(repo)
    repo.persistence.upsert_daily_market_bars([bar(rows[1], date(2026, 10, 8))])
    recovery_id = await prior_recovery(repo, [rows[1]])
    before = copy.deepcopy(repo.persistence.cycle_run(recovery_id))
    result = await discovery(repo)
    row = result['candidates'][0]
    assert row['instrument_id'] == rows[1]['globalInstrumentId']
    assert row['existing_valid_bar_count'] == 1 and row['missing_completed_sessions'] == 4
    assert row['missing_historical_windows'] == [
        {'start': '2026-10-09', 'end_exclusive': '2026-10-10'},
        {'start': '2026-10-05', 'end_exclusive': '2026-10-08'}]
    assert row['first_available_historical_date'] == row['last_available_historical_date'] == '2026-10-08'
    assert row['previous_recovery']['recovery_id'] == recovery_id
    assert row['previous_recovery']['checkpoint_status'] == 'FAILED'
    assert row['previous_recovery']['recovery_status'] == 'PARTIAL_FAILURE'
    assert row['previous_recovery']['terminal'] and not row['previous_recovery']['execution_authorized']
    assert row['priority_rationale'] == 'VERIFIED_PARTIAL_COVERAGE_WITH_PRIOR_RECOVERY'
    assert result['summary']['partially_recovered_candidates'] == 1
    assert repo.persistence.cycle_run(recovery_id) == before
    assert repo.persistence.active_cycle_run(RECOVERY_SLOT) is None


@pytest.mark.asyncio
async def test_complete_approved_range_excluded_but_older_history_unresolved(repo):
    rows = await source(repo, count=1)
    repo.persistence.upsert_daily_market_bars([bar(rows[0], START + timedelta(days=n)) for n in range(5)])
    result = await discovery(repo)
    row = result['assessments'][0]
    assert not result['candidates']
    assert row['exclusion_reason'] == 'HISTORICAL_RANGE_ALREADY_COVERED'
    assert row['missing_completed_sessions'] == 0 and row['missing_historical_windows'] == []
    assert row['older_history'] == 'UNRESOLVED_OUTSIDE_APPROVED_RANGE'


@pytest.mark.asyncio
@pytest.mark.parametrize('listing_date', [None, '2026-10-08', '2029-01-01'])
async def test_unverified_listing_date_never_removes_expected_sessions(repo, listing_date):
    await source(repo, count=1, listing_date=listing_date)
    row = (await discovery(repo))['candidates'][0]
    assert row['expected_completed_sessions'] == row['missing_completed_sessions'] == 5
    assert row['listing_date_verification_status'] == ('MISSING' if listing_date is None else 'UNVERIFIED_NOT_USED')


@pytest.mark.asyncio
async def test_provider_success_does_not_certify_complete_coverage(repo):
    rows = await source(repo, count=1)
    repo.persistence.upsert_daily_market_bars([bar(rows[0], START)])
    await prior_recovery(repo, rows, reason='HISTORICAL_RANGE_INCOMPLETE')
    row = (await discovery(repo))['candidates'][0]
    assert row['eligibility_status'] == 'ELIGIBLE'
    assert row['previous_recovery']['reason_classification'] == 'HISTORICAL_RANGE_INCOMPLETE'
    assert row['previous_recovery']['recovery_status'] == 'PARTIAL_FAILURE'
    assert row['previous_recovery']['checkpoint_status'] == 'FAILED'
    assert row['missing_completed_sessions'] == 4
    assert row['missing_historical_windows']


@pytest.mark.asyncio
async def test_discovery_enforced_read_only_no_network_no_mutation_or_publication(repo, monkeypatch):
    rows = await source(repo, count=1)
    repo.persistence.upsert_daily_market_bars([bar(rows[0], START)])
    source_before = copy.deepcopy(repo.persistence.cycle_run(SOURCE_ID))
    progress_before = copy.deepcopy(repo.persistence.cycle_progress(SOURCE_ID))
    publication_before = copy.deepcopy(repo.persistence.etf_radar_cycle(SOURCE_ID))
    bars_before = [b.model_dump() for b in repo.persistence.load_daily_market_bars({UUID(rows[0]['globalInstrumentId'])})]
    changes_before = repo.persistence._connection.total_changes
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No provider network allowed')))
    queries = []
    repo.persistence._connection.set_trace_callback(queries.append)
    try:
        result = await discovery(repo)
    finally:
        repo.persistence._connection.set_trace_callback(None)
    assert repo.persistence._connection.total_changes == changes_before
    assert not any(q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER')) for q in queries)
    assert any('query_only' in q.lower() for q in queries)
    assert repo.persistence.cycle_run(SOURCE_ID) == source_before
    assert repo.persistence.cycle_progress(SOURCE_ID) == progress_before
    assert repo.persistence.etf_radar_cycle(SOURCE_ID) == publication_before
    assert [b.model_dump() for b in repo.persistence.load_daily_market_bars({UUID(rows[0]['globalInstrumentId'])})] == bars_before
    assert result['provider_requests'] == 0 and not result['execution_authorized']
    assert result['publication'] == 'UNCHANGED' and result['recommendation'] == 'NOT_EVALUATED'


@pytest.mark.asyncio
async def test_one_snapshot_dispatch_for_whole_discovery(repo, monkeypatch):
    await source(repo, count=3)
    original = repo._run_blocking_persistence
    calls = []
    async def dispatch(fn):
        calls.append(fn)
        return await original(fn)
    monkeypatch.setattr(repo, '_run_blocking_persistence', dispatch)
    await discovery(repo)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_bars_from_other_instrument_or_invalid_source_do_not_cover_gap(repo):
    rows = await source(repo)
    repo.persistence.upsert_daily_market_bars([
        bar(rows[1], START), bar(rows[0], START, source_url='https://unverified.invalid/history')])
    result = await discovery(repo)
    records = {r['instrument_id']: r for r in result['assessments']}
    assert records[rows[0]['globalInstrumentId']]['existing_valid_bar_count'] == 0
    assert records[rows[1]['globalInstrumentId']]['existing_valid_bar_count'] == 1


@pytest.mark.asyncio
async def test_default_range_clock_and_range_limits(repo):
    await source(repo, count=1)
    result = await discover_etf_history_recovery(repo, SOURCE_ID, now=NOW)
    assert result['range'] == {'start': (NOW.date() - timedelta(days=365)).isoformat(),
                              'end_exclusive': (NOW.date() + timedelta(days=1)).isoformat()}
    with pytest.raises(ValueError, match='AWARE_CLOCK'):
        await discover_etf_history_recovery(repo, SOURCE_ID, now=datetime(2026, 10, 9))
    with pytest.raises(ValueError, match='MAX_731'):
        await discover_etf_history_recovery(repo, SOURCE_ID, start=date(2020, 1, 1), end=END, now=NOW)


@pytest.mark.asyncio
async def test_default_range_uses_utc_date_for_aware_non_utc_clock(repo):
    await source(repo, count=1)
    shifted = datetime(2026, 10, 10, 1, tzinfo=timezone(timedelta(hours=14)))
    result = await discover_etf_history_recovery(repo, SOURCE_ID, now=shifted)
    assert result['as_of'] == '2026-10-09T11:00:00+00:00'
    assert result['range']['end_exclusive'] == '2026-10-10'


@pytest.mark.asyncio
async def test_terminal_checkpoint_never_overrides_current_coverage(repo):
    rows = await source(repo, count=1)
    await prior_recovery(repo, rows, status='COMPLETED', reason=None)
    result = await discovery(repo)
    row = result['candidates'][0]
    assert row['previous_recovery']['checkpoint_status'] == 'COMPLETED'
    assert row['missing_completed_sessions'] == 5
    assert row['eligibility_status'] == 'ELIGIBLE' and not result['execution_authorized']


@pytest.mark.asyncio
async def test_malformed_canonical_identity_has_explicit_safe_exclusion(repo):
    await source(repo, count=1)
    def malformed(universe):
        universe[0]['canonical']['globalInstrumentId'] = 'not-a-uuid'
        universe[0]['canonical']['isin'] = 'synthetic.private-token'
        universe[0]['canonical']['primarySymbol'] = 'https://private.invalid?token=secret'
    change_universe(repo.persistence, malformed)
    result = await discovery(repo)
    row = result['assessments'][0]
    assert row['instrument_id'] is row['isin'] is row['primary_symbol'] is None
    assert row['eligibility_status'] == 'EXCLUDED'
    assert row['exclusion_reason'] == 'ETF_RECOVERY_AMBIGUOUS_OR_UNKNOWN_CANONICAL_ID'
    assert 'private' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('instrument_ids', [None, 'not-an-id-list', 7, {}])
async def test_matching_prior_checkpoint_malformed_scope_fails_closed_without_mutation(repo, instrument_ids):
    rows = await source(repo, count=1)
    recovery_id = await prior_recovery(repo, rows)
    parameters = request(rows).parameters() | {'instrument_ids': instrument_ids}
    with repo.persistence._connection:
        repo.persistence._connection.execute(
            'UPDATE global_opportunity_cycle_run SET parameters = ? WHERE cycle_id = ?',
            (json.dumps(parameters), recovery_id))
    before = copy.deepcopy(repo.persistence.cycle_run(recovery_id))
    changes = repo.persistence._connection.total_changes
    for _ in range(2):
        with pytest.raises(ValueError, match='^ETF_DISCOVERY_INVALID_RECOVERY_METADATA$'):
            await discovery(repo)
    assert repo.persistence._connection.total_changes == changes
    assert repo.persistence.cycle_run(recovery_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['isin', 'primarySymbol'])
async def test_duplicate_isin_or_symbol_excluded_by_shared_canonical_gate(repo, field):
    await source(repo, count=2)
    def conflicting_claim(universe):
        universe[1]['canonical'][field] = universe[0]['canonical'][field]
        if field == 'isin':
            universe[1]['isin'] = universe[0]['isin']
        else:
            symbol = universe[0]['symbol']
            universe[1]['symbol'] = symbol
            universe[1]['canonical']['providerMappings'][0]['providerSymbol'] = symbol
    change_universe(repo.persistence, conflicting_claim)
    result = await discovery(repo)
    assert result['candidates'] == []
    assert len(result['assessments']) == 2
    assert all(r['exclusion_reason'] == 'ETF_RECOVERY_AMBIGUOUS_OR_UNKNOWN_CANONICAL_ID'
               for r in result['assessments'])
