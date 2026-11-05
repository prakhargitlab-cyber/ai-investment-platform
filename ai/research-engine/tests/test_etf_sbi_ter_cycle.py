"""Verified SBI TER evidence is persisted by the normal ETF cycle; everything else stays unavailable; failures are bounded."""
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import openpyxl
import pytest
from test_etf_full_universe import universe, checkpoint, NOW, _ensure_etf_history, run_full_etf_cycle  # noqa: F401
from test_etf_fund_identity_cycle import repo  # noqa: F401
from app import etf_sbi_ter_acquisition as acq
from app.etf_amfi_navall import EXPECTED_HEADER
from app.etf_evidence import EtfMetric
from app.sbi_ter_workbook_adapter import EXPECTED_HEADERS

AMC = "Test AMC Limited"
MASTER_HEADER = ("AMC,Code,Scheme Name,Scheme Type,Scheme Category,Scheme NAV Name,Scheme Minimum Amount,Launch Date,"
                 " Closure Date,ISIN Div Payout/ ISIN GrowthISIN Div Reinvestment")


def amfi_files(rows):
    nav = [EXPECTED_HEADER, " ", "Open Ended Schemes(Exchange Traded Funds (ETFs) - Equity ETF)", " ", "Test Mutual Fund", " "]
    master = [MASTER_HEADER]
    for n, r in enumerate(rows):
        nav.append(f"{140000 + n};{r['isin']};-;Fixture ETF {n};Direct Plan;;100.0000;05-Nov-2026")
        master.append(f"{AMC},{140000 + n},Fixture ETF {n},Open Ended,Exchange Traded Funds (ETFs) - Equity ETF,x,5000,"
                      f"01-Jan-2020,,{r['isin']}")
    return "\r\n".join(nav) + "\r\n", "\n".join(master) + "\n"


def workbook(entries):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sheet1"; ws.append(EXPECTED_HEADERS)
    for code, name, ter in entries:
        ws.append([code, name, datetime(2026, 11, 4), ter, 0, 0, 0, ter, 0, 0, 0, 0, 0])
    out = BytesIO(); wb.save(out); return out.getvalue()


async def run(repo, mode, monkeypatch, mutate=lambda nav, master: (nav, master)):
    monkeypatch.setattr(acq, "SBI_AMC", AMC)
    monkeypatch.setattr(acq, "SBI_FUND_HOUSE", "Test Mutual Fund")
    rows, csv = universe(4)
    nav, master = mutate(*amfi_files(rows))
    xlsx = workbook([("N0", "Fixture ETF 0", 0.07),             # exact -> persisted
                     ("N1", "Fixture ETF 1 ", 0.05),            # trailing space only -> persisted
                     ("N2", "Fixture ETF 22", 0.5),             # no exact AMFI name -> never matched
                     ("N3", "Fixture ETF 3 ETF FOF", 0.9),      # fund of fund -> excluded
                     ("N9", "Fixture ETF 2", 7.9)])             # implausible value -> withheld
    async def nav_fetcher():
        if mode == "navall_down":
            raise httpx.ConnectError("down")
        return nav, None
    async def wb_fetcher():
        if mode == "workbook_down":
            raise httpx.ConnectError("down")
        return (b"<html>maintenance</html>" if mode == "html" else xlsx), None
    async def master_fetcher():
        return master.encode(), None
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
    cp = await checkpoint(repo)
    async def history(repository, row, *, now, checkpoint):
        return await _ensure_etf_history(repository, row, now=now, checkpoint=checkpoint,
            provider=SimpleNamespace(closes=AsyncMock(return_value=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)), checkpoint=cp,
            cycle_id=cp.cycle_id, identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
            history_acquirer=history, fund_identity_fetcher=nav_fetcher, sbi_ter_fetchers=(wb_fetcher, master_fetcher))
    return rows, cp


def diag(repo, cp):
    return repo.persistence.cycle_run(cp.cycle_id)["selection"]["sourceDiagnostics"]["SBI_MUTUAL_FUND"]


def facts(repo, rows):
    return [repo.persistence.etf_facts(UUID(r["globalInstrumentId"]), EtfMetric.EXPENSE_RATIO) for r in rows]


def reasons(repo, rows):
    return [[(str(a.outcome), a.reason) for a in repo.persistence.etf_attempts(UUID(r["globalInstrumentId"]))
             if a.provider == "SBI_MUTUAL_FUND"] for r in rows]


@pytest.mark.asyncio
async def test_cycle_persists_only_exact_verified_sbi_ter_and_marks_the_rest_unavailable(repo, monkeypatch):
    rows, _ = await run(repo, "ok", monkeypatch)
    f = facts(repo, rows)
    assert [len(x) for x in f] == [1, 1, 0, 0]
    assert str(f[0][0].value) == "0.07" and f[0][0].unit == "percent" and str(f[0][0].as_of_date) == "2026-11-04"
    assert f[0][0].provenance.source_metadata["identity_confidence"] == "VERIFIED_EXACT_NAME"
    assert f[0][0].provenance.source_metadata["amfi_scheme_code"] == "140000"
    assert f[0][0].provenance.source_url.endswith("current-year-ter.xlsx")
    r = reasons(repo, rows)
    assert r[2] == [("SUCCESS_EMPTY", "SOURCE_VALUE_WITHHELD:TOTAL_TER_ABOVE_PLAUSIBLE_LIMIT")]
    assert r[3] == [("SUCCESS_EMPTY", "NOT_IN_SBI_TER_WORKBOOK_UNDER_AN_EXACT_VERIFIED_NAME")]
    # AUM is never produced here
    assert all(repo.persistence.etf_facts(UUID(x["globalInstrumentId"]), EtfMetric.AUM) == [] for x in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["navall_down", "workbook_down", "html"])
async def test_source_failures_are_bounded_and_persist_nothing(repo, monkeypatch, mode):
    rows, cp = await run(repo, mode, monkeypatch)
    assert all(x == [] for x in facts(repo, rows))
    assert diag(repo, cp)["status"] == "FAILED"


@pytest.mark.asyncio
async def test_ok_run_diagnostic_and_unavailable_reasons(repo, monkeypatch):
    rows, cp = await run(repo, "ok", monkeypatch)
    assert diag(repo, cp)["status"] == "OK" and diag(repo, cp)["ingested"] == 2 and diag(repo, cp)["withheld"] == 1



NOW_AFTER = datetime(2026, 11, 6, 12, tzinfo=__import__("datetime").timezone.utc)


@pytest.mark.asyncio
async def test_missing_or_withheld_ter_never_counts_as_available_or_ready(repo, monkeypatch):
    from app.etf_readiness import evaluate_etf_readiness
    from app.etf_screening import screening_evidence
    rows, _ = await run(repo, "ok", monkeypatch)
    for n, row in enumerate(rows):
        key = UUID(row["globalInstrumentId"])
        reqs, _summary = evaluate_etf_readiness(repo.persistence, key, now=NOW_AFTER)
        status = {r.requirement: str(r.status) for r in reqs}
        selected = {m.value: (repo.persistence.etf_facts(key, m) or [None])[0]
                    for m in (EtfMetric.EXPENSE_RATIO, EtfMetric.AUM, EtfMetric.TRACKING_ERROR, EtfMetric.TRACKING_DIFFERENCE)}
        selected = {k: v for k, v in selected.items() if v is not None}
        metrics = screening_evidence(repo.persistence, key, selected, {}, now=NOW_AFTER)["screening_metrics"]
        if n < 2:                      # accepted: exact name + both AMFI files + canonical
            assert status["EXPENSE_RATIO"] == "READY_FRESH" and metrics["expense_ratio"]["quality"] == "VALID"
        else:                          # withheld (7.9%) or unmatched: missing, with no value at all
            assert status["EXPENSE_RATIO"] == "MISSING"
            assert metrics["expense_ratio"] == {"value": None, "unit": "PERCENT_PA", "quality": "UNAVAILABLE",
                                                "reason": "NO_PERSISTED_METRIC_EVIDENCE"}
        for name in ("aum", "tracking_error", "tracking_difference"):          # never acquired, never invented
            assert metrics[name]["quality"] == "UNAVAILABLE" and metrics[name]["value"] is None
        assert status["AUM"] == "MISSING"


def _drop_row_from_navall(nav, master):
    return nav.replace(nav.split("\r\n")[6] + "\r\n", ""), master          # row 0 only in the scheme master


def _other_amc(nav, master):
    return nav, master.replace(AMC, "Some Other AMC Limited")


def _different_isins(nav, master):
    # AMFI publishes both files consistently with ISINs that no canonical NSE ETF has
    for n in range(1, 5):
        nav = nav.replace(f"IN{n:010d}", f"INZ{n:09d}"[:11] + "0")
        master = master.replace(f"IN{n:010d}", f"INZ{n:09d}"[:11] + "0")
    return nav, master


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate,expected", [
    (_drop_row_from_navall, [0, 1, 0, 0]),   # ISIN confirmed in one AMFI file only -> rejected even with exact name
    (_other_amc, [0, 0, 0, 0]),             # AMC identity not independently verified -> nothing
    (_different_isins, [0, 0, 0, 0]),       # exact names, but no canonical NSE ETF owns those ISINs -> nothing
])
async def test_canonical_exact_isin_gate_is_enforced_in_the_running_cycle(repo, monkeypatch, mutate, expected):
    rows, _ = await run(repo, "ok", monkeypatch, mutate)
    assert [len(x) for x in facts(repo, rows)] == expected


@pytest.mark.asyncio
async def test_workbook_failure_is_durable_per_instrument_and_in_the_cycle_record(repo, monkeypatch):
    rows, cp = await run(repo, "workbook_down", monkeypatch)
    assert all(r == [("TECHNICAL_FAILURE", "SBI_TER_ConnectError:down")] for r in reasons(repo, rows))
    d = diag(repo, cp)
    assert d["status"] == "FAILED" and d["failure"] == "SBI_TER_ConnectError:down"
    assert all(x == [] for x in facts(repo, rows))
