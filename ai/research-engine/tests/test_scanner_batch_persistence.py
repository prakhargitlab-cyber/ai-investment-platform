from dataclasses import replace
from uuid import UUID

import pytest

from app.persistence import SqliteResearchPersistence, DisabledResearchPersistence
from app.postgres_persistence import PostgresResearchPersistence, _PostgresConnectionAdapter
from app.repository import ResearchRepository
from test_fact_precedence import fact
from app.fact_precedence import FactSourceTier
from test_shareholding import _snapshot
from test_structured_market_persistence import _record


@pytest.mark.parametrize("method", ["load_financial_facts", "load_events", "load_shareholding_snapshots",
                                  "load_structured_market_snapshots", "load_market_price_observations"])
def test_empty_filter_never_reads_all_rows(method):
    store = SqliteResearchPersistence()
    queries = []
    store._connection.set_trace_callback(queries.append)
    assert getattr(store, method)(set()) == []
    assert queries == []
    assert getattr(DisabledResearchPersistence(), method)(set()) == []


def test_filtered_facts_snapshots_prices_and_shareholding_values():
    store = SqliteResearchPersistence()
    for n in [1, 2]:
        key = UUID(int=n)
        value = fact("pat", n, FactSourceTier.OFFICIAL_NSE)
        store.upsert_financial_fact(replace(value, key=replace(value.key, instrument_id=key)))
        store.upsert_shareholding_snapshot(_snapshot(key, source=f"source-{n}"))
        store.upsert_structured_market_snapshot(_record(key))
    queries = []
    store._connection.set_trace_callback(queries.append)
    ids = {UUID(int=1)}
    assert [f.key.instrument_id for f in store.load_financial_facts(ids)] == list(ids)
    snapshots = store.load_shareholding_snapshots(ids)
    assert [s.instrument_id for s in snapshots] == list(ids)
    assert len(snapshots[0].values) == 1
    assert [s.instrument_id for s in store.load_structured_market_snapshots(ids)] == list(ids)
    assert [p.instrument_id for p in store.load_market_price_observations(ids)] == list(ids)
    assert all(" WHERE " in q and " IN (" in q for q in queries)
    assert len(store.load_financial_facts()) == 2


@pytest.mark.asyncio
async def test_repository_financial_reads_pass_ids_to_persistence():
    class FilterRequired(DisabledResearchPersistence):
        def load_financial_facts(self, instrument_ids=None):
            assert instrument_ids == {UUID(int=1)}
            return []
    repository = ResearchRepository.__new__(ResearchRepository)
    repository._persistence = FilterRequired()
    import threading
    repository._persistence_worker_lock = threading.RLock()
    assert repository.financial_facts_for(UUID(int=1)) == []
    assert await repository.financial_facts_for_instruments({UUID(int=1)}) == {UUID(int=1): []}


def test_postgres_inherits_parameterized_bounded_batch_reads():
    queries = []
    class Connection:
        def execute(self, sql, params):
            queries.append((sql, params))
            return self
        def fetchall(self): return []
    store = PostgresResearchPersistence.__new__(PostgresResearchPersistence)
    store._connection = _PostgresConnectionAdapter(Connection())
    ids = {UUID(int=n) for n in range(1, 1002)}
    assert store.load_financial_facts(ids) == []
    assert [len(params) for _, params in queries] == [500, 500, 1]
    assert all("WHERE instrument_id IN (%s" in sql and "?" not in sql for sql, _ in queries)
    assert set(p for _, params in queries for p in params) == {str(i) for i in ids}


def test_event_and_supporting_source_reads_are_filtered():
    # Existing domain fixtures supply valid source documents and events.
    repository = ResearchRepository(persistence=DisabledResearchPersistence())
    store = SqliteResearchPersistence()
    events = list(repository.events.values())
    assert events
    for document in repository.documents.values(): store.upsert_document(document)
    for event in events: store.upsert_event(event)
    target = events[0].instrument_id
    queries = []
    store._connection.set_trace_callback(queries.append)
    loaded = store.load_events({target})
    assert loaded and {e.instrument_id for e in loaded} == {target}
    assert len(loaded) == sum(e.instrument_id == target for e in events)
    assert all(" WHERE " in q and " IN (" in q for q in queries)
    assert any("research_event_sources" in q and "event_id IN" in q for q in queries)
