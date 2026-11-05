"""SBI TER evidence, attempts and cycle diagnostics persist on real PostgreSQL (all constraints) and are cleared by the reset."""
import os, shutil, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from test_postgres_migrations_and_checkpoint import database, _apply, _connect, _migrations, _store
from test_etf_reset_rebuild_postgres import _repo, _psql, RESET_DIR, ETF_TABLES
from test_etf_full_universe import universe, NOW
from test_etf_sbi_ter_cycle import amfi_files, workbook, AMC
from app import etf_sbi_ter_acquisition as acq
from app.cycle_checkpoint import CycleCheckpoint
from app.etf_evidence import EtfMetric
from app.etf_full_universe import run_full_etf_cycle, _ensure_etf_history

pytestmark = pytest.mark.skipif(not os.environ.get("AIP_TEST_POSTGRES_ADMIN_DSN"), reason="isolated PostgreSQL required")


@pytest.mark.asyncio
async def test_sbi_ter_persists_on_postgres_and_is_removed_by_the_reset(database, monkeypatch):
    if shutil.which("psql") is None:
        pytest.skip("psql required")
    monkeypatch.setattr(acq, "SBI_AMC", AMC); monkeypatch.setattr(acq, "SBI_FUND_HOUSE", "Test Mutual Fund")
    conn = _connect(database); _apply(conn, _migrations())
    conn.execute("CREATE TABLE IF NOT EXISTS flyway_schema_history(installed_rank int, version text)")
    conn.execute("INSERT INTO flyway_schema_history VALUES (1,'1')"); conn.commit(); conn.close()
    store = _store(database); repo = _repo(store)
    rows, csv = universe(3)
    nav, master = amfi_files(rows)
    xlsx = workbook([("N0", "Fixture ETF 0", 0.07), ("N1", "Fixture ETF 1", 0.05), ("N2", "Fixture ETF 2", 7.9)])
    async def nav_f(): return nav, None
    async def wb_f(): return xlsx, None
    async def ms_f(): return master.encode(), None
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
    key = str(UUID(int=30001))
    store.create_cycle_run(key, {}, market="ETF"); store.claim_cycle_run(key, "owner", lease_seconds=600)
    cp = await CycleCheckpoint(store, key, "owner", run_blocking=repo._run_blocking_persistence).load()
    async def history(repository, row, *, now, checkpoint):
        return await _ensure_etf_history(repository, row, now=now, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)), checkpoint=cp,
                cycle_id=key, identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
                history_acquirer=history, fund_identity_fetcher=nav_f, sbi_ter_fetchers=(wb_f, ms_f))
        got = [store.etf_facts(UUID(r["globalInstrumentId"]), EtfMetric.EXPENSE_RATIO) for r in rows]
        assert [len(g) for g in got] == [1, 1, 0]
        f = got[0][0]
        assert (str(f.value), f.unit, str(f.as_of_date), f.provenance.provider) == ("0.07", "percent", "2026-11-04", "SBI_MUTUAL_FUND")
        assert f.provenance.source_metadata["identity_confidence"] == "VERIFIED_EXACT_NAME"
        diag = store.cycle_run(key)["selection"]["sourceDiagnostics"]["SBI_MUTUAL_FUND"]
        assert diag["status"] == "OK" and diag["ingested"] == 2 and diag["withheld"] == 1
        attempts = [a for a in store.etf_attempts(UUID(rows[2]["globalInstrumentId"])) if a.provider == "SBI_MUTUAL_FUND"]
        assert [(str(a.outcome), a.reason) for a in attempts] == [("SUCCESS_EMPTY", "SOURCE_VALUE_WITHHELD:TOTAL_TER_ABOVE_PLAUSIBLE_LIMIT")]
        store.update_cycle_run(key, "owner", status="COMPLETED")
        work = tempfile.mkdtemp()
        try:
            for fl in RESET_DIR.glob("*.sql"):
                shutil.copy(fl, work)
            Path(work, "etf_ids.csv").write_text("instrument_id\n" + "".join(r["globalInstrumentId"] + "\n" for r in rows))
            res = _psql(database, work, "-v", "expected_schema=research", "-v", "confirm=RESET_ETF_RADAR", "-v", "commit=yes", "-f", "20_reset.sql")
            assert res.returncode == 0, res.stderr
        finally:
            shutil.rmtree(work, ignore_errors=True)
        c = _connect(database)
        try:
            assert all(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0 for t in ETF_TABLES)
        finally:
            c.close()
    finally:
        close = getattr(store._connection, "close", None)
        if close: close()
