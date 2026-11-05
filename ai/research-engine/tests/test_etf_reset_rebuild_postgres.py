"""Proof that a fresh full ETF cycle rebuilds from the empty ETF state produced by the reset procedure.

Isolated PostgreSQL only (own random database). Runs the operator reset SQL for real, then a second full cycle.
"""
import os, shutil, subprocess, tempfile
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from test_postgres_migrations_and_checkpoint import database, _apply, _connect, _migrations, _store
from test_etf_full_universe import universe, NOW
from app.cycle_checkpoint import CycleCheckpoint
from app.etf_full_universe import run_full_etf_cycle, _ensure_etf_history

pytestmark = pytest.mark.skipif(not os.environ.get("AIP_TEST_POSTGRES_ADMIN_DSN"), reason="isolated PostgreSQL required")
RESET_DIR = Path(__file__).resolve().parents[3] / "scripts" / "database" / "maintenance" / "etf-radar-reset"
SIZE = 7
ETF_TABLES = ["etf_fact_observations", "etf_nav_observations", "etf_holdings_snapshots", "etf_holdings_positions",
              "etf_listing_observations", "etf_acquisition_attempts", "etf_radar_cycles"]


def _repo(store):
    async def dispatch(fn, *a, **k):
        return fn(*a, **k)
    return SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), _run_blocking_persistence=dispatch)


async def _run_cycle(repo, key):
    rows, csv = universe(SIZE)

    def handler(request):
        if request.url.path.endswith(".csv"):
            return httpx.Response(200, text=csv)
        if "marketWatchApi" in request.url.path:
            return httpx.Response(200, json={"data": {"timestamp": NOW.strftime("%d-%b-%Y %H:%M:%S"),
                "navDate": NOW.strftime("%d-%b-%Y"), "data": [dict(symbol=r["primarySymbol"], series="EQ",
                identifier=r["primarySymbol"] + "EQN", lastPrice=100, totalTradedVolume=500000, nav=100) for r in rows]}})
        symbol = request.url.params["symbol"]; row = next(r for r in rows if r["primarySymbol"] == symbol)
        return httpx.Response(200, json=dict(symbol=symbol, isin=row["isin"], isETFSec="true", isDelisted="false",
            isSuspended="false", activeSeries=["EQ"], marketType="N"))
    repo.persistence.create_cycle_run(key, {}, market="ETF")
    repo.persistence.claim_cycle_run(key, "owner", lease_seconds=600)
    cp = await CycleCheckpoint(repo.persistence, key, "owner", run_blocking=repo._run_blocking_persistence).load()

    async def history_acquirer(repository, row, *, now, checkpoint):
        return await _ensure_etf_history(repository, row, now=now, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)), checkpoint=cp,
            cycle_id=key, identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
            history_acquirer=history_acquirer)
    return rows


def _counts(info):
    conn = _connect(info)
    try:
        out = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ETF_TABLES}
        out["etf_runs"] = conn.execute("SELECT count(*) FROM global_opportunity_cycle_run WHERE market='ETF'").fetchone()[0]
        return out
    finally:
        conn.close()


def _psql(info, work, *args):
    env = dict(os.environ, PGPASSWORD=info["password"], PGOPTIONS="-c search_path=research")
    return subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", info["host"], "-p", str(info["port"]),
        "-U", info["user"], "-d", info["dbname"], *args], cwd=work, env=env, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_fresh_cycle_rebuilds_after_reset(database):
    if shutil.which("psql") is None:
        pytest.skip("psql required")
    conn = _connect(database); _apply(conn, _migrations())
    conn.execute("CREATE TABLE IF NOT EXISTS flyway_schema_history(installed_rank int, version text)")
    conn.execute("INSERT INTO flyway_schema_history VALUES (1,'1')"); conn.commit(); conn.close()
    store = _store(database); repo = _repo(store)
    try:
        rows = await _run_cycle(repo, str(UUID(int=20001)))
        first = store.etf_radar_cycle(str(UUID(int=20001)))
        before = _counts(database)
        assert before["etf_radar_cycles"] == 1 and before["etf_listing_observations"] >= SIZE and before["etf_runs"] == 1
        # The worker finalizes its run; the reset guard refuses non-terminal ETF cycles.
        store.update_cycle_run(str(UUID(int=20001)), 'owner', status='COMPLETED')
        work = tempfile.mkdtemp()
        try:
            for f in RESET_DIR.glob("*.sql"):
                shutil.copy(f, work)
            Path(work, "etf_ids.csv").write_text("instrument_id\n" + "".join(r["globalInstrumentId"] + "\n" for r in rows))
            res = _psql(database, work, "-v", "expected_schema=research", "-v", "confirm=RESET_ETF_RADAR", "-v", "commit=yes", "-f", "20_reset.sql")
            assert res.returncode == 0, res.stderr
            empty = _counts(database)
            assert set(empty.values()) == {0}, empty
            assert store.etf_radar_cycle(str(UUID(int=20001))) is None or not store.etf_radar_cycle(str(UUID(int=20001)))
            # schema intact: the engine's own initialization check still passes on the same database
            _store(database)._assert_schema_available()
            # second, fresh full cycle from the empty ETF research state
            await _run_cycle(repo, str(UUID(int=20002)))
            second = store.etf_radar_cycle(str(UUID(int=20002)))
            after = _counts(database)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        assert after == before
        assert second["coverage"] == first["coverage"]
        assert len(second["outcomes"]) == len(first["outcomes"]) == SIZE
    finally:
        close = getattr(store._connection, 'close', None)
        if close: close()
