"""Native PostgreSQL fence test; own random database on the disposable server only."""
import os
from datetime import datetime,timezone,timedelta
from uuid import uuid4
import pytest
from test_postgres_migrations_and_checkpoint import database,_apply,_connect,_migrations,_store
from app.etf_evidence import EtfAcquisitionAttempt,EtfAcquisitionOutcome
from app.cycle_checkpoint import CycleOwnershipLost
pytestmark=pytest.mark.skipif(not os.environ.get('AIP_TEST_POSTGRES_ADMIN_DSN'),reason='isolated PostgreSQL required')

@pytest.mark.parametrize('mode',['owned','cancel','expired','takeover','during_save'])
def test_native_acquisition_fence(database,mode,monkeypatch):
    conn=_connect(database); _apply(conn,_migrations()); conn.close()
    store=_store(database); cycle=str(uuid4()); instrument=uuid4()
    try:
        store.create_cycle_run(cycle,{},market='ETF')
        store.claim_cycle_run(cycle,'owner',lease_seconds=60)
        attempt=EtfAcquisitionAttempt(instrument_id=instrument,metric='IDENTITY',provider='NSE',
            attempted_at=datetime.now(timezone.utc),outcome=EtfAcquisitionOutcome.SUCCESS_EMPTY,reason='TEST',evidence_ids=[])
        if mode=='cancel': store.request_cycle_cancel(cycle)
        elif mode in ('expired','takeover'):
            with store._connection:
                store._connection.execute('UPDATE global_opportunity_cycle_run SET lease_expires_at=? WHERE cycle_id=?',
                    ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),cycle))
            if mode=='takeover': store.claim_cycle_run(cycle,'new-owner',lease_seconds=60)
        if mode=='during_save':
            execute=store._connection.execute
            def expire_during_insert(sql,args=()):
                result=execute(sql,args)
                if 'INSERT INTO etf_acquisition_attempts' in sql:
                    execute('UPDATE global_opportunity_cycle_run SET lease_expires_at=? WHERE cycle_id=?',
                        ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),cycle))
                return result
            monkeypatch.setattr(store._connection,'execute',expire_during_insert)
        if mode=='owned':
            store.save_etf_acquisition([],attempt,fence=(cycle,'owner'))
            store.save_etf_acquisition([],attempt,fence=(cycle,'owner'))
            assert len(store.etf_attempts(instrument))==1
        else:
            with pytest.raises(CycleOwnershipLost): store.save_etf_acquisition([],attempt,fence=(cycle,'owner'))
            assert store.etf_attempts(instrument)==[]
    finally: store._connection.connection.close()
