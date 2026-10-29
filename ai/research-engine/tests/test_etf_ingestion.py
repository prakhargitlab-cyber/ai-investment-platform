"""Isolated canonical -> ingestion -> readiness -> ranking regressions."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from app.etf_ingestion import canonical_etfs, ingest_quotes, ingest_history
from app.etf_evidence import EtfMetric
from app.etf_opportunity_cycle import run_etf_radar_cycle
from app.models import DailyMarketBar, StructuredMarketSnapshot, StructuredInstrumentResolution
from app.fact_precedence import ProvenancedValue
from app.persistence import SqliteResearchPersistence

ID = UUID(int=21)
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
META = dict(globalInstrumentId=str(ID), status="ACTIVE", assetType="ETF", country="IN",
    primaryExchange="NSE", primarySymbol="TESTETF", canonicalName="Test ETF", isin="IN0000000021", currency="INR",
    providerMappings=[dict(provider="NSE", providerSymbol="TESTETF", status="VERIFIED",
                          resolutionSource="NSE_OFFICIAL_ETF_BOOTSTRAP", currency="INR")])
CSV = "Symbol,SecurityName,ISINNumber\nTESTETF,Test ETF,IN0000000021\nOTHER,Other ETF,IN0000000022\n"


@pytest.fixture
def repo():
    store = SqliteResearchPersistence()
    async def dispatch(fn):
        return fn()
    value = SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), _run_blocking_persistence=dispatch)
    yield value
    store._connection.close()


def snapshot(*, volume=True, age=0, undated=False):
    stamp = NOW - timedelta(days=age)
    return {"data": {"timestamp": None if undated else stamp.strftime("%d-%b-%Y %H:%M:%S"),
        "navDate": stamp.strftime("%d-%b-%Y"), "totals": {"volume": 500000, "value": 50000000},
        "aduCount": {"advances": 1}, "marketStatus": {"marketStatus": "Closed"},
        "data": [{"symbol": "TESTETF", "series": "EQ", "identifier": "TESTETFEQN",
                  "lastPrice": 100, "totalTradedVolume": 500000 if volume else None, "nav": 99.5}]}}


async def ingest(repo, value=None, *, failure=None, csv=CSV, now=NOW):
    provider = SimpleNamespace(collect_baseline=AsyncMock())
    def handler(request):
        if request.url.path.endswith(".csv"):
            return httpx.Response(200, text=csv)
        if "marketWatchApi" in request.url.path:
            if failure:
                raise failure
            return httpx.Response(200, json=value or snapshot())
        return httpx.Response(200, json={"symbol": "TESTETF", "isin": META["isin"], "isETFSec": "true",
            "isDelisted": "false", "isSuspended": "false", "activeSeries": ["EQ"], "marketType": "N"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ingest_quotes(repo, SimpleNamespace(structured_provider=provider), [META], client, clock=lambda: now)
    return result, provider


@pytest.mark.asyncio
async def test_ingestion_is_scoped_dated_idempotent_and_never_publishes(repo):
    for _ in range(2):
        result, provider = await ingest(repo)
        assert result["status"] == "COMPLETED"
        assert result["instruments"][0]["summary"]["mandatory_ready"] == 3
        assert result["instruments"][0]["summary"]["supporting_missing"] > 0
        provider.collect_baseline.assert_not_called()
    assert len(repo.persistence.etf_listings()) == 1
    for metric in (EtfMetric.MARKET_PRICE, EtfMetric.TRADING_VOLUME):
        facts = repo.persistence.etf_facts(ID, metric)
        assert len(facts) == 1
        assert facts[0].as_of_date == NOW.date()
        assert facts[0].provenance.provider == "NSE"
    assert repo.persistence.latest_etf_radar_cycle() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["missing", "stale", "undated", "failure", "future"])
async def test_missing_mandatory_evidence_never_ranks(repo, variant):
    result, _ = await ingest(repo, snapshot(volume=variant != "missing", age=4 if variant == "stale" else -1 if variant == "future" else 0,
        undated=variant == "undated"), failure=TimeoutError("fixture timeout") if variant == "failure" else None)
    assert result["status"] == "PARTIAL_FAILURE"
    cycle = run_etf_radar_cycle([META], repo.persistence, now=NOW)
    assert not cycle.ranked
    assert cycle.candidates[0].disposition == "INSUFFICIENT_DATA"
    if variant in {"undated", "failure", "future"}:
        assert all(a["outcome"] == "TECHNICAL_FAILURE" for a in result["instruments"][0]["attempts"])


@pytest.mark.asyncio
async def test_official_identity_conflict_blocks_quotes_and_writes(repo):
    result, provider = await ingest(repo, csv=CSV.replace("TESTETF", "CONFLICT"))
    assert result["status"] == "BLOCKED"
    provider.collect_baseline.assert_not_called()
    assert repo.persistence.etf_listings() == []


@pytest.mark.asyncio
async def test_realistic_fresh_history_and_volume_can_rank_under_existing_rules(repo):
    await ingest(repo)
    for i in range(260):
        repo.persistence.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=ID,
            trading_date=NOW.date() - timedelta(days=259-i), close=Decimal("100") + Decimal(i)/2,
            currency="INR", provider="NSE", source_mode="REAL", source_url="https://www.nseindia.com/fixture",
            retrieved_at=NOW))
    result = run_etf_radar_cycle([META], repo.persistence, now=NOW)
    assert len(result.ranked) == 1
    assert result.ranked[0].recommendation in {"OPPORTUNITY", "STRONG_OPPORTUNITY"}
    assert result.ranked[0].rule_engine_result.overall_score > 60


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"assetType": "EQUITY"}, {"status": "INACTIVE"},
                                   {"providerMappings": []}, {"globalInstrumentId": str(UUID(int=22))}])
async def test_canonical_mismatch_rejected_before_ingestion(change):
    source = SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=META | change))
    with pytest.raises(ValueError):
        await canonical_etfs(source, [ID], identity_headers={})


@pytest.mark.asyncio
async def test_history_bounds_and_missing_instrument_do_not_fetch():
    source = SimpleNamespace(global_instrument_metadata=AsyncMock(side_effect=ValueError("missing")))
    with pytest.raises(ValueError):
        await canonical_etfs(source, [ID], identity_headers={})
    jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
                           populate_daily_bars=AsyncMock())
    with pytest.raises(ValueError, match="REQUEST_WINDOW"):
        await ingest_history(jobs, [META], start=NOW.date()-timedelta(days=31), end=NOW.date(), identity_headers={})
    jobs.populate_daily_bars.assert_not_called()


@pytest.mark.parametrize("path", ["/api/v1/etf-radar/evidence", "/api/v1/etf-radar/evidence/history"])
@pytest.mark.parametrize("role,status", [(None,401), ("ANALYST",403), ("ADMIN",200)])
def test_ingestion_endpoint_authorization(monkeypatch, path, role, status):
    import app.main as main
    import app.etf_ingestion as ingestion
    monkeypatch.setattr(main.settings, "structured_provider_enabled", False)
    source = AsyncMock(return_value=[META])
    operation = AsyncMock(return_value={"status": "COMPLETED", "instruments": []})
    monkeypatch.setattr(ingestion, "canonical_etfs", source)
    monkeypatch.setattr(ingestion, "ingest_quotes", operation)
    monkeypatch.setattr(ingestion, "ingest_history", operation)
    headers = {} if role is None else {"X-AIP-User-Id": str(ID), "X-AIP-User-Issuer":"test",
                                       "X-AIP-User-Subject":"test", "X-AIP-User-Roles":role}
    body = {"instrument_ids":[str(ID)], "start":NOW.date().isoformat(), "end":NOW.date().isoformat()}
    response = TestClient(main.app).post(path, headers=headers, json=body)
    assert response.status_code == status
    if status != 200:
        source.assert_not_called(); operation.assert_not_called()


def test_ingestion_rejects_unbounded_requests():
    import app.main as main
    response = TestClient(main.app).post("/api/v1/etf-radar/evidence", json={"instrument_ids":[str(ID)]*6})
    assert response.status_code == 422


def test_etf_quote_resolution_does_not_accept_equity_or_change_equity_contract():
    from app.structured_market import _trusted_nse_candidate_reason
    candidate = dict(symbol="TESTETF.NS", quoteType="ETF", exchange="NSI", currency="INR", isin=META["isin"])
    instrument = dict(assetType="ETF", isin=META["isin"], currency="INR")
    assert _trusted_nse_candidate_reason(instrument, "Test ETF", candidate, "TESTETF.NS") is None
    assert _trusted_nse_candidate_reason(instrument, "Test ETF", candidate | {"quoteType":"EQUITY"}, "TESTETF.NS") == "QUOTE_TYPE_MISMATCH"
    assert _trusted_nse_candidate_reason(instrument | {"assetType":"EQUITY"}, "Test ETF", candidate, "TESTETF.NS") == "QUOTE_TYPE_MISMATCH"


@pytest.mark.asyncio
async def test_partially_rejected_history_never_reports_complete():
    from app.nse_historical_daily import NseHistoricalResult
    value = NseHistoricalResult(ID, NOW.date(), NOW.date(), status="SUCCESS", persisted_rows=1,
                                rejection_reasons={"SYMBOL_MISMATCH": 1})
    jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
                           populate_daily_bars=AsyncMock(return_value=value))
    result = await ingest_history(jobs, [META], start=NOW.date(), end=NOW.date(), identity_headers={})
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["instruments"][0]["persisted_rows"] == 1


@pytest.mark.asyncio
async def test_nse_provenance_survives_reopen_and_retry(repo, tmp_path):
    repo.persistence._connection.close()
    path = tmp_path / "etf.sqlite"
    repo.persistence = SqliteResearchPersistence(path)
    await ingest(repo)
    before = repo.persistence.etf_facts(ID, EtfMetric.MARKET_PRICE)[0]
    repo.persistence._connection.close()
    repo.persistence = SqliteResearchPersistence(path)
    assert repo.persistence.etf_facts(ID, EtfMetric.MARKET_PRICE)[0] == before
    await ingest(repo)
    assert len(repo.persistence.etf_facts(ID, EtfMetric.MARKET_PRICE)) == 1
    assert before.provenance.source_metadata["session"]["totals"]["volume"] == 500000


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_optional_nav_is_not_mandatory_but_failure_is_reported(repo, missing):
    data = snapshot()
    data["data"]["data"][0]["nav"] = None if missing else "NaN"
    result, _ = await ingest(repo, data)
    assert result["instruments"][0]["summary"]["mandatory_ready"] == 3
    assert result["status"] == ("COMPLETED" if missing else "PARTIAL_FAILURE")
    # MARKET_PRICE/TRADING_VOLUME must never be affected by a NAV-only parse
    # failure -- each metric is acquired and recorded independently.
    attempts = {a["metric"]: a["outcome"] for a in result["instruments"][0]["attempts"]}
    assert attempts["MARKET_PRICE"] == "SUCCESS_WITH_DATA"
    assert attempts["TRADING_VOLUME"] == "SUCCESS_WITH_DATA"


@pytest.mark.asyncio
async def test_nav_parse_rejection_logs_only_symbol_and_capped_raw_value(repo, monkeypatch, caplog):
    # Diagnostic logging added for the "NSE_ACQUISITION_FAILED:INVALID_NSE_NUMBER"
    # NAV incident: confirms the log fires with just the public symbol and a
    # short, length-capped repr of the "nav" field alone -- never the full
    # quote/row (price, volume, session totals) and never credentials.
    import app.etf_ingestion as etf_ingestion
    captured = []
    monkeypatch.setattr(etf_ingestion.logger, "warning",
                        lambda msg, *args: captured.append(msg % args))
    data = snapshot()
    data["data"]["data"][0]["nav"] = "NaN"
    await ingest(repo, data)
    assert len(captured) == 1
    assert captured[0] == "etf_nav_parse_rejected symbol=TESTETF raw_nav='NaN'"
    assert "lastPrice" not in captured[0] and "totalTradedVolume" not in captured[0]


@pytest.mark.asyncio
async def test_metadata_conflict_blocks_quote_without_yahoo_fallback(repo):
    await ingest(repo)
    provider = SimpleNamespace(collect_baseline=AsyncMock())
    requests = []
    def handler(request):
        requests.append(str(request.url))
        if request.url.path.endswith(".csv"):
            return httpx.Response(200, text=CSV)
        if "marketWatchApi" in request.url.path:
            return httpx.Response(200, json=snapshot())
        return httpx.Response(200, json={"symbol": "TESTETF", "isin": "CONFLICT"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ingest_quotes(repo, SimpleNamespace(structured_provider=provider), [META], client, clock=lambda: NOW)
    assert result["status"] == "PARTIAL_FAILURE"
    assert len(requests) == 3
    assert len(repo.persistence.etf_facts(ID, EtfMetric.MARKET_PRICE)) == 1  # history preserved
    assert result["instruments"][0]["summary"]["mandatory_ready"] == 2
    cycle = run_etf_radar_cycle([META], repo.persistence, now=NOW)
    assert not cycle.ranked
    assert result["instruments"][0]["attempts"][0]["reason"].endswith("NSE_METADATA_IDENTITY_CONFLICT")
    provider.collect_baseline.assert_not_called()
    # An equal-time success cannot arbitrarily win by random attempt UUID.
    tied, _ = await ingest(repo)
    assert tied["status"] == "PARTIAL_FAILURE"
    recovered, _ = await ingest(repo, now=NOW + timedelta(seconds=1))
    assert recovered["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_provider_failure_does_not_persist_exception_secrets(repo):
    result, provider = await ingest(repo, failure=TimeoutError("fixture-sensitive-cookie"))
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["instruments"][0]["summary"]["mandatory_technical_failure"] == 2
    assert "fixture-sensitive-cookie" not in str(result)
    assert all(a["outcome"] == "TECHNICAL_FAILURE" for a in result["instruments"][0]["attempts"])
    provider.collect_baseline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2, 6])
async def test_invalid_batch_never_contacts_providers(repo, count):
    client = SimpleNamespace(get=AsyncMock())
    with pytest.raises(ValueError, match="CANDIDATE_LIMIT_OR_DUPLICATES"):
        await ingest_quotes(repo, None, [META] * count, client)
    client.get.assert_not_called()
