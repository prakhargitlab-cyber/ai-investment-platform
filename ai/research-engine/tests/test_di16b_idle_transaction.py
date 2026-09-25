"""DI-16B regression: read-only persistence operations must not leave a
long-lived production (psycopg) connection `idle in transaction`.

Root cause (proven from source, see plan DI-16B):
    `SqliteResearchPersistence.load_market_calendar_exceptions` (and 9 sibling
    reads) executed `self._connection.execute("SELECT ...").fetchall()` with no
    `with self._connection:` guard. Under `PostgresResearchPersistence.__init__`
    the connection is constructed with `autocommit=False` and reused for the
    whole process (postgres_persistence.py:24/27). With psycopg3 autocommit
    disabled, the first command of a session implicitly BEGINs a transaction;
    a SELECT therefore opens a transaction that is only ended by commit/rollback.
    Because these read methods never entered `with self._connection:` (whose
    `__exit__` commits on success / rolls back on exception), the implicit
    transaction was never closed -> the connection sat `idle in transaction`
    with `last query = SELECT * FROM market_trading_calendar_exceptions`.

Fix: each standalone read-only persistence method now wraps its SELECT(s) in
`with self._connection:`, reusing the exact commit/rollback boundary the write
methods already use. Writes are untouched.

These tests use a psycopg3-like fake connection (autocommit=False, auto-BEGIN on
execute, commit/rollback in the context-manager exit) so the read-no-open-txn
invariant is verifiable without a live Postgres cluster. Pre-fix, every read
method below leaves `in_txn=True` (test FAILS); post-fix `__exit__` commits so
`in_txn=False` (test PASSES).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.persistence import SqliteResearchPersistence
from app.stock_rule_engine import STOCK_RULE_ENGINE_VERSION, StockRuleEngineService


INSTRUMENT_ID = UUID("00000000-0000-0000-0000-000000000145")
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


class _FakePsycopgCursor:
    """psycopg3 cursor stand-in: fetchall/fetchone return canned rows."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)


class FakePsycopgConnection:
    """Minimal psycopg3 connection model that reproduces DI-16B's trigger:

    * ``autocommit = False`` -- a transaction is NOT auto-committed per
      statement (matches PostgresResearchPersistence.__init__).
    * the first ``execute`` implicitly BEGINs a transaction (``in_txn`` becomes
      True), exactly like psycopg3 under autocommit=False.
    * ``__exit__`` commits (success) or rolls back (exception), mirroring
      ``_PostgresConnectionAdapter.__exit__``.

    Read methods that forget ``with self._connection:`` therefore leave
    ``in_txn`` True forever -> the production `idle in transaction` leak.
    """

    autocommit = False

    def __init__(self, rows=()):
        self.in_txn = False
        self.committed = 0
        self.rolled_back = 0
        self.entered = 0
        self.exited = 0
        self.executed_sql = []
        self._rows = _FakePsycopgCursor(rows)

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited += 1
        if exc_type is None:
            self.committed += 1
        else:
            self.rolled_back += 1
        self.in_txn = False
        return False

    def execute(self, sql, params=()):
        # psycopg3 implicitly begins a transaction on the first statement when
        # autocommit is disabled -- that is precisely the leak source.
        self.in_txn = True
        self.executed_sql.append(str(sql))
        return self

    def fetchall(self):
        return self._rows.fetchall()

    def fetchone(self):
        return self._rows.fetchone()

    def commit(self):
        self.committed += 1
        self.in_txn = False

    def rollback(self):
        self.rolled_back += 1
        self.in_txn = False


def _store_with_fake(rows=()):
    """A SqliteResearchPersistence that uses the psycopg-like fake connection.

    We bypass ``__init__`` (no sqlite schema needed -- the fake returns empty
    rows) and inject the fake connection directly, exactly as
    ``PostgresResearchPersistence`` would hold a long-lived ``autocommit=False``
    connection.
    """
    store = object.__new__(SqliteResearchPersistence)
    store._connection = FakePsycopgConnection(rows=rows)
    return store


# (human label, callable that performs one read-only persistence operation)
READ_CALLERS = [
    ("load_market_calendar_exceptions", lambda s: s.load_market_calendar_exceptions({"NSE"})),
    ("load_market_schedules", lambda s: s.load_market_schedules({"NSE"})),
    ("load_documents", lambda s: s.load_documents()),
    ("load_acquisition_observations", lambda s: s.load_acquisition_observations(INSTRUMENT_ID)),
    ("load_stock_rule_engine_result", lambda s: s.load_stock_rule_engine_result(INSTRUMENT_ID, STOCK_RULE_ENGINE_VERSION, "fp-di16b")),
    ("load_structured_market_snapshots", lambda s: s.load_structured_market_snapshots({INSTRUMENT_ID})),
    ("load_market_price_observations", lambda s: s.load_market_price_observations({INSTRUMENT_ID})),
    ("load_market_price_coverage", lambda s: s.load_market_price_coverage({INSTRUMENT_ID})),
    ("load_events", lambda s: s.load_events({INSTRUMENT_ID})),
    ("load_financial_facts", lambda s: s.load_financial_facts({INSTRUMENT_ID})),
    ("load_shareholding_snapshots", lambda s: s.load_shareholding_snapshots({INSTRUMENT_ID})),
    ("load_daily_market_bars", lambda s: s.load_daily_market_bars({INSTRUMENT_ID})),
]


def test_load_market_calendar_exceptions_leaves_no_open_transaction():
    """DI-16B headline: the exact owner of the observed leak must not leave the
    production connection idle-in-transaction.

    Pre-fix this FAILS: ``load_market_calendar_exceptions`` called
    ``self._connection.execute("SELECT ... FROM market_trading_calendar_exceptions")``
    with no ``with self._connection:``, so under ``autocommit=False`` the implicit
    psycopg transaction was never committed.
    """
    store = _store_with_fake()
    store.load_market_calendar_exceptions({"NSE"})
    conn = store._connection
    assert conn.committed >= 1, "read must commit its implicit transaction via with self._connection:"
    assert not conn.in_txn, "connection must not be idle-in-transaction after the read completes"
    assert any("market_trading_calendar_exceptions" in sql for sql in conn.executed_sql)


@pytest.mark.parametrize("label,call", READ_CALLERS)
def test_read_only_persistence_operation_leaves_no_open_transaction(label, call):
    """Generic scope check (Task B): every standalone read-only persistence
    operation -- not just the calendar exceptions query -- must terminate its
    implicit transaction on return. Pre-fix this fails for every entry.
    """
    store = _store_with_fake()
    call(store)
    conn = store._connection
    assert conn.committed >= 1, f"{label}: read did not commit via with self._connection:"
    assert not conn.in_txn, f"{label}: connection left idle-in-transaction"


def test_unguarded_select_pattern_leaks_transaction(fake_psycopg_conn=FakePsycopgConnection):
    """Documents the DI-16B defect mechanism: a SELECT executed against an
    ``autocommit=False`` connection WITHOUT a ``with self._connection:`` (or an
    explicit commit) leaves the transaction open -- this is exactly the code
    shape that shipped before the fix.
    """
    conn = FakePsycopgConnection()
    # Direct, unguarded SELECT (the pre-fix pattern) opens a txn that is never
    # committed because __exit__ is never invoked.
    conn.execute("SELECT * FROM market_trading_calendar_exceptions").fetchall()
    assert conn.in_txn is True, "unguarded SELECT under autocommit=False must leave a transaction open"
    assert conn.committed == 0


# --- Write atomicity: writes must still commit and roll back correctly ---


def _result_dict():
    return {
        "global_instrument_id": str(INSTRUMENT_ID),
        "rule_engine_version": STOCK_RULE_ENGINE_VERSION,
        "input_fingerprint": "fp-di16b",
        "calculated_at": NOW.isoformat(),
        "input_as_of": NOW.isoformat(),
        "overall_score": 0.8,
        "quality_score": 0.7,
        "opportunity_score": 0.6,
        "risk_score": 0.3,
        "confidence_score": 0.9,
        "decision_signal": "BUY",
        "partial": False,
    }


def test_write_commits_atomically():
    """Writes that use ``with self._connection:`` must commit on success."""
    store = SqliteResearchPersistence(":memory:")
    store.upsert_stock_rule_engine_result(_result_dict())
    loaded = store.load_stock_rule_engine_result(INSTRUMENT_ID, STOCK_RULE_ENGINE_VERSION, "fp-di16b")
    assert loaded is not None, "committed write must be visible to a subsequent read"
    assert loaded["overall_score"] == 0.8
    assert loaded["decision_signal"] == "BUY"


def test_write_failure_rolls_back():
    """A write that raises mid-transaction must roll back so no partial state
    is persisted. We wrap the real sqlite3 connection in a small proxy that
    raises on the INSERT inside the write ``with`` block; the persistence
    method's ``__exit__`` must therefore take the rollback branch. The read
    path (``load_stock_rule_engine_result``) reuses the same ``with
    self._connection:`` boundary, so the rolled-back row is correctly invisible.
    """

    class _FailingInsertConn:
        def __init__(self, real):
            self._real = real
            self.in_txn = False
            self.committed = 0
            self.rolled_back = 0

        def __enter__(self):
            self.in_txn = True
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.committed += 1
                self._real.commit()
            else:
                self.rolled_back += 1
                self._real.rollback()
            self.in_txn = False
            return False

        def execute(self, sql, params=()):
            if isinstance(sql, str) and sql.startswith("INSERT INTO global_stock_rule_engine_results"):
                raise RuntimeError("simulated write failure")
            return self._real.execute(sql, params)

        def executescript(self, sql):
            return self._real.executescript(sql)

        @property
        def row_factory(self):
            return self._real.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._real.row_factory = value

        def commit(self):
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

    store = SqliteResearchPersistence(":memory:")
    store._connection = _FailingInsertConn(store._connection)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        store.upsert_stock_rule_engine_result(_result_dict())

    conn = store._connection
    assert conn.rolled_back == 1, "write failure must trigger rollback via the with boundary"
    # Rolled back -> nothing committed, subsequent read sees nothing.
    assert store.load_stock_rule_engine_result(INSTRUMENT_ID, STOCK_RULE_ENGINE_VERSION, "fp-di16b") is None


def test_di16_offload_boundary_preserved():
    """Guards the DI-16 event-loop starvation fix: ``StockRuleEngineService.analyze``
    must still offload the synchronous ``engine.evaluate`` via
    ``asyncio.to_thread`` so the on-loop reads do not block ``/health``.
    """
    src = inspect.getsource(StockRuleEngineService.analyze)
    assert "asyncio.to_thread" in src, "DI-16 offload (asyncio.to_thread) must remain in analyze"
