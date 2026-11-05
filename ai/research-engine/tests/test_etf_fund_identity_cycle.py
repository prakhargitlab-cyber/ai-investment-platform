from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from test_etf_full_universe import universe, checkpoint, NOW, _ensure_etf_history, run_full_etf_cycle  # noqa: F401
from threading import RLock
from app.persistence import SqliteResearchPersistence
from app.etf_amfi_navall import EXPECTED_HEADER
from app.etf_evidence import EtfMetric
from app.etf_phase_progress import etf_phase_progress


@pytest.fixture
def repo():
    store = SqliteResearchPersistence()
    async def dispatch(fn, *a, **k):
        return fn(*a, **k)
    yield SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), _run_blocking_persistence=dispatch)
    store._connection.close()


def navall(rows):
    lines = [EXPECTED_HEADER, " ", "Open Ended Schemes(Exchange Traded Funds (ETFs) - Equity ETF)", " ", "Test Mutual Fund", " "]
    lines += [f"{140000 + n};{r['isin']};-;{r['canonicalName']};Direct Plan;;100.0000;05-Nov-2026" for n, r in enumerate(rows)]
    return "\r\n".join(lines) + "\r\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "failure", "garbage", "unexpected"])
async def test_cycle_persists_exact_isin_fund_house_or_records_failure(repo, mode):
    rows, csv = universe(4)
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
    async def fetcher():
        if mode == "failure":
            raise httpx.ConnectError("down")
        if mode == "unexpected":
            raise RuntimeError("provider bug")
        if mode == "garbage":
            return "<html>maintenance</html>", None
        return navall(rows[:3]), None          # the 4th ETF is absent from AMFI
    cp = await checkpoint(repo)
    async def history(repository, row, *, now, checkpoint):
        return await _ensure_etf_history(repository, row, now=now, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)), checkpoint=cp,
            cycle_id=cp.cycle_id, identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
            history_acquirer=history, fund_identity_fetcher=fetcher)
    from uuid import UUID
    facts = [repo.persistence.etf_facts(UUID(r["globalInstrumentId"]), EtfMetric.AMC_FUND_HOUSE) for r in rows]
    if mode == "ok":
        assert [len(f) for f in facts] == [1, 1, 1, 0]
        assert facts[0][0].value == "Test Mutual Fund" and facts[0][0].provenance.source_metadata["amfi_scheme_code"] == "140000"
    else:
        assert all(f == [] for f in facts)
    attempts = [repo.persistence.etf_attempts(UUID(r["globalInstrumentId"])) for r in rows]
    outcomes = [[str(a.outcome) for a in at if a.provider == "AMFI"] for at in attempts]
    assert outcomes == ([["SUCCESS_WITH_DATA"]] * 3 + [["SUCCESS_EMPTY"]] if mode == "ok" else [["TECHNICAL_FAILURE"]] * 4)
    phases = {p["phase"]: p for p in etf_phase_progress(repo.persistence.cycle_run(cp.cycle_id))["phases"]}
    assert phases["FUNDAMENTAL_ACQUISITION"]["succeeded"] == (3 if mode == "ok" else 0)
