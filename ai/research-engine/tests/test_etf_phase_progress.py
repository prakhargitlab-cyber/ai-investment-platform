"""Synthetic checkpoint doubles; no live cycle, database or provider."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cycle_checkpoint import CycleOwnershipLost
from app.etf_phase_progress import PHASES, etf_phase_progress, record_etf_phase


def phase(run, name):
    return next(p for p in etf_phase_progress(run)['phases'] if p['phase'] == name)


def test_legacy_completed_run_does_not_fabricate_any_eight_phase_counters():
    result = etf_phase_progress({'status': 'COMPLETED', 'selection': {'universe': [{}]*3}})
    assert len(result['phases']) == 8
    assert [p['phase'] for p in result['phases']] == list(PHASES)
    assert all(p['observed'] is False and p['total'] is None and p['succeeded'] is None for p in result['phases'])
    assert result['overall_percentage'] is None and result['current_phase'] is None


@pytest.mark.asyncio
async def test_fenced_roundtrip_counts_started_completed_skipped_reused_and_remaining():
    checkpoint = SimpleNamespace(save_selection=AsyncMock())
    selection = {'scope': 'FULL_OFFICIAL_NSE_ETF'}
    await record_etf_phase(checkpoint, selection, 'HISTORY_ONBOARDING', total=5,
        events=[('id-a','SUCCEEDED'), ('id-b','FAILED'), ('id-c','SKIPPED'), ('id-d','REUSED')])
    stored = json.loads(json.dumps(checkpoint.save_selection.call_args.args[0]))
    row = phase({'selection':stored,'status':'RUNNING'}, 'HISTORY_ONBOARDING')
    assert {k:row[k] for k in ('total','succeeded','failed','skipped','reused','remaining','pending','attempted')} == {
        'total':5,'succeeded':1,'failed':1,'skipped':1,'reused':1,'remaining':1,'pending':1,'attempted':2}


@pytest.mark.asyncio
async def test_retry_counts_attempts_without_double_counting_terminal_units():
    checkpoint = SimpleNamespace(save_selection=AsyncMock())
    selection = {}
    for state in ('IN_PROGRESS','FAILED','IN_PROGRESS','SUCCEEDED'):
        await record_etf_phase(checkpoint, selection, 'HISTORY_ONBOARDING', total=1, events=[('id',state)])
    row = phase({'selection':selection},'HISTORY_ONBOARDING')
    assert row['attempt_count'] == 2 and row['attempted'] == 1
    assert row['failed_attempts'] == 1 and row['failed'] == 0 and row['succeeded'] == 1
    assert row['remaining'] == row['in_progress'] == 0


@pytest.mark.asyncio
async def test_lost_owner_or_cancellation_does_not_advance_caller_state():
    checkpoint = SimpleNamespace(save_selection=AsyncMock(side_effect=CycleOwnershipLost('synthetic-cycle')))
    selection = {'scope':'FULL_OFFICIAL_NSE_ETF'}
    original = deepcopy(selection)
    with pytest.raises(CycleOwnershipLost):
        await record_etf_phase(checkpoint,selection,'PUBLICATION',total=1,events=[('result','IN_PROGRESS')])
    assert selection == original


@pytest.mark.asyncio
async def test_publication_only_succeeds_after_real_atomic_published_status():
    checkpoint = SimpleNamespace(save_selection=AsyncMock())
    selection = {}
    await record_etf_phase(checkpoint,selection,'PUBLICATION',total=1,events=[('result','IN_PROGRESS')])
    assert phase({'selection':selection,'status':'RUNNING'},'PUBLICATION')['succeeded'] == 0
    for status in ('PUBLISHED','COMPLETED'):
        row = phase({'selection':selection,'status':status},'PUBLICATION')
        assert row['succeeded'] == 1 and row['remaining'] == row['in_progress'] == 0
    for status in ('FAILED','CANCELLED'):
        assert phase({'selection':selection,'status':status},'PUBLICATION')['succeeded'] == 0
    assert selection['etf_phase_progress']['phases']['PUBLICATION']['items']['result']['state'] == 'IN_PROGRESS'


@pytest.mark.asyncio
async def test_progress_update_keeps_other_nested_selection_references():
    documents = {}
    checkpoint = SimpleNamespace(save_selection=AsyncMock())
    selection = {'document_acquisition':documents}
    await record_etf_phase(checkpoint,selection,'FUNDAMENTAL_ACQUISITION',total=0)
    documents['synthetic-isin'] = {'status':'SKIPPED'}
    assert selection['document_acquisition'] is documents
    assert selection['document_acquisition']['synthetic-isin']['status'] == 'SKIPPED'


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [
    {'phase':'INVENTED','total':1}, {'phase':'PUBLICATION','total':True},
    {'phase':'PUBLICATION','total':-1}, {'phase':'PUBLICATION','total':0,'events':[('result','SUCCEEDED')]},
    {'phase':'PUBLICATION','total':1,'events':[('result','INVENTED')]},
])
async def test_invalid_phase_events_do_not_persist(kwargs):
    checkpoint=SimpleNamespace(save_selection=AsyncMock())
    selection={}
    with pytest.raises(ValueError):
        await record_etf_phase(checkpoint,selection,**kwargs)
    checkpoint.save_selection.assert_not_called()
    assert selection == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_reason', ['cancelled', 'wrong-owner'])
async def test_real_sqlite_checkpoint_enforces_cancellation_and_owner_fence(lost_reason):
    from app.cycle_checkpoint import CycleCheckpoint
    from app.persistence import SqliteResearchPersistence
    store = SqliteResearchPersistence()
    try:
        store.create_cycle_run('synthetic-phase-cycle', {}, market='ETF')
        assert store.claim_cycle_run('synthetic-phase-cycle', 'synthetic-owner')
        cp = CycleCheckpoint(store, 'synthetic-phase-cycle', 'synthetic-owner')
        selection = {'scope':'FULL_OFFICIAL_NSE_ETF'}
        await record_etf_phase(cp,selection,'PUBLICATION',total=1,events=[('result','IN_PROGRESS')])
        previous = deepcopy(selection)
        if lost_reason == 'cancelled':
            store.request_cycle_cancel('synthetic-phase-cycle')
        else:
            cp = CycleCheckpoint(store,'synthetic-phase-cycle','different-synthetic-owner')
        with pytest.raises(CycleOwnershipLost):
            await record_etf_phase(cp,selection,'PUBLICATION',total=1,events=[('result','SUCCEEDED')])
        assert selection == previous
        assert store.cycle_run('synthetic-phase-cycle')['selection'] == previous
    finally:
        store._connection.close()
