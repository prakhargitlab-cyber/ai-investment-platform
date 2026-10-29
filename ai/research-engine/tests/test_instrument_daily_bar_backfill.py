"""Targeted tests for the single-instrument NSE historical daily-bar
backfill route (POST /api/v1/research/market-data/population/instrument).

This route is a thin, ADMIN-gated wrapper around the existing, already
well-tested IndiaMarketDataPopulationJobs.backfill_daily_bars worker
(see tests/test_nse_daily_backfill.py), scoped to exactly one
global_instrument_id. These tests cover only what is specific to the new
route: authorization, instrument-identity/type/exchange/status validation
before the worker is invoked, single-instrument scoping of the call into
the worker, cooldown/failure surfacing, and that the route genuinely
dispatches into the real persistence path. They deliberately do not
re-test window planning, coverage classification or failure-class
taxonomy, which belong to test_nse_daily_backfill.py.

No live NSE acquisition occurs in this file: the only network-shaped
dependency, NseHistoricalDailyProvider, is always monkeypatched.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.nse_daily_backfill as backfill
from app.main import app as fastapi_app
from app.market_data_population import IndiaMarketDataPopulationJobs
from app.models import DailyMarketBar
from app.nse_historical_daily import NseHistoricalResult
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import GlobalInstrumentNotFoundError, PortfolioServiceUnavailableError
from app.repository import ResearchRepository
from app.settings import Settings

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)
ID = UUID(int=42)
START, END = date(2026, 9, 1), date(2026, 9, 4)

ADMIN_HEADERS = {"X-AIP-User-Id": "u1", "X-AIP-User-Issuer": "test",
                 "X-AIP-User-Subject": "sub1", "X-AIP-User-Roles": "ADMIN"}
ROUTE = "/api/v1/research/market-data/population/instrument"


def equity_metadata(**changes):
    return dict(globalInstrumentId=str(ID), canonicalName="20 Microns Limited", isin="INE144J01027",
        assetType="EQUITY", currency="INR", country="IN", primaryExchange="NSE", primarySymbol="20MICRONS",
        status="ACTIVE", providerMappings=[dict(provider="NSE", providerSymbol="20MICRONS",
        providerInstrumentId=None, exchange="NSE", currency="INR", status="VERIFIED",
        resolutionSource="OFFICIAL_NSE", confidence=1.0)]) | changes


class _FakeOrchestrator:
    """Stands in for PortfolioResearchOrchestrator.global_instrument_metadata."""

    def __init__(self, metadata=None, error=None):
        self._metadata = metadata
        self._error = error
        self.calls = []

    async def global_instrument_metadata(self, instrument_id, *, correlation_id=None, identity_headers=None):
        self.calls.append(instrument_id)
        if self._error is not None:
            raise self._error
        return self._metadata


class _FakeJobs:
    """Stands in for IndiaMarketDataPopulationJobs.backfill_daily_bars."""

    def __init__(self, summary):
        self._summary = summary
        self.calls = []

    async def backfill_daily_bars(self, **kwargs):
        self.calls.append(kwargs)
        return self._summary


def _summary(**item_changes):
    item = dict(globalInstrumentId=str(ID), nse_symbol="20MICRONS", coverage_state_before="NO_HISTORY",
        requested_windows=1, successful_windows=1, failed_windows=0, rows_received=2, rows_accepted=2,
        rows_persisted=2, status="SUCCESS", failure_reason=None, failure_class=None,
        earliest_persisted_date=START, latest_persisted_date=END, windows=[]) | item_changes
    return dict(status="COMPLETED", failure_reason=None, requested_instruments=1, processed_instruments=1,
        succeeded_instruments=1 if item["status"] == "SUCCESS" else 0,
        failed_instruments=0 if item["status"] == "SUCCESS" else 1,
        skipped_current=1 if item["status"] == "SKIPPED_CURRENT" else 0,
        skipped_cooldown=1 if item["status"] == "SKIPPED_COOLDOWN" else 0,
        instruments=[item])


def _wire(monkeypatch, *, orchestrator=None, jobs=None):
    if orchestrator is not None:
        monkeypatch.setattr(main, "portfolio_orchestrator", orchestrator)
    if jobs is not None:
        monkeypatch.setattr(main, "market_data_population_jobs", jobs)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,expected_status", [(None, 401), ("ANALYST", 403), ("ADMIN", 200)])
def test_authorization(monkeypatch, role, expected_status):
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    headers = {} if role is None else {"X-AIP-User-Id": "u1", "X-AIP-User-Issuer": "test",
                                        "X-AIP-User-Subject": "sub1", "X-AIP-User-Roles": role}
    response = TestClient(fastapi_app).post(ROUTE, headers=headers, json={"global_instrument_id": str(ID)})
    assert response.status_code == expected_status
    if expected_status != 200:
        assert not orchestrator.calls and not jobs.calls


# ---------------------------------------------------------------------------
# Instrument identity / type / exchange / status validation, before the
# worker is ever invoked.
# ---------------------------------------------------------------------------

def test_unknown_instrument_rejected_before_worker_invocation(monkeypatch):
    orchestrator = _FakeOrchestrator(error=GlobalInstrumentNotFoundError(str(ID)))
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 404 and response.json()["detail"] == "COMPANY_NOT_RESOLVED"
    assert not jobs.calls


def test_portfolio_service_unavailable_surfaces_as_502(monkeypatch):
    orchestrator = _FakeOrchestrator(error=PortfolioServiceUnavailableError("down"))
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 502
    assert not jobs.calls


def test_etf_instrument_rejected_before_worker_invocation(monkeypatch):
    orchestrator = _FakeOrchestrator(metadata=equity_metadata(assetType="ETF"))
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 400 and response.json()["detail"] == "INSTRUMENT_NOT_EQUITY"
    assert not jobs.calls


@pytest.mark.parametrize("changes", [dict(primaryExchange="BSE"), dict(country="US")])
def test_non_nse_instrument_rejected_before_worker_invocation(monkeypatch, changes):
    orchestrator = _FakeOrchestrator(metadata=equity_metadata(**changes))
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 400 and response.json()["detail"] == "INSTRUMENT_NOT_NSE_LISTED"
    assert not jobs.calls


def test_inactive_instrument_rejected_before_worker_invocation(monkeypatch):
    orchestrator = _FakeOrchestrator(metadata=equity_metadata(status="DELISTED"))
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 400 and response.json()["detail"] == "INSTRUMENT_NOT_ACTIVE"
    assert not jobs.calls


# ---------------------------------------------------------------------------
# Single-instrument scoping of the call into the existing worker.
# ---------------------------------------------------------------------------

def test_calls_worker_scoped_to_exactly_this_one_instrument(monkeypatch):
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS,
        json={"global_instrument_id": str(ID), "start": START.isoformat(), "end": END.isoformat(), "force": True})
    assert response.status_code == 200
    assert len(jobs.calls) == 1
    call = jobs.calls[0]
    assert call["instrument_ids"] == {ID}
    assert call["start"] == START and call["end"] == END and call["force"] is True
    body = response.json()
    assert body["globalInstrumentId"] == str(ID)
    assert body["instrument"]["globalInstrumentId"] == str(ID)


def test_defaults_to_no_explicit_date_range_or_force(monkeypatch):
    """Omitted start/end/force are passed through as None/False, letting the
    existing worker apply its own bounded defaults -- no date-range logic is
    duplicated here."""
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(_summary())
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 200
    call = jobs.calls[0]
    assert call["start"] is None and call["end"] is None and call["force"] is False


# ---------------------------------------------------------------------------
# Cooldown enforcement and invalid date ranges surfaced from the worker.
# ---------------------------------------------------------------------------

def test_cooldown_surfaced_as_a_successful_skipped_response_not_an_error(monkeypatch):
    """A 200 here must never be read as 'data was populated': the HTTP status
    alone is not the signal. instrument.status is explicitly SKIPPED_COOLDOWN
    (never SUCCESS) and rows_persisted is 0, by construction of the
    underlying worker (the cooldown check short-circuits before any provider
    call, so the per-instrument item can never carry both SKIPPED_COOLDOWN
    and a nonzero rows_persisted)."""
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(_summary(status="SKIPPED_COOLDOWN", failure_reason="RETRY_COOLDOWN", rows_persisted=0))
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 200
    instrument = response.json()["instrument"]
    assert instrument["status"] == "SKIPPED_COOLDOWN" and instrument["status"] != "SUCCESS"
    assert instrument["rows_persisted"] == 0
    assert instrument["failure_reason"] == "RETRY_COOLDOWN"


def test_invalid_date_range_from_worker_surfaces_as_400(monkeypatch):
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(dict(status="FAILED", failure_reason="WINDOW_PLANNING_FAILURE", instruments=[]))
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS,
        json={"global_instrument_id": str(ID), "start": END.isoformat(), "end": START.isoformat()})
    assert response.status_code == 400 and response.json()["detail"] == "WINDOW_PLANNING_FAILURE"


def test_universe_unavailable_from_worker_surfaces_as_502(monkeypatch):
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(dict(status="FAILED", failure_reason="UNIVERSE_UNAVAILABLE", instruments=[]))
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 502


def test_instrument_dropped_by_worker_membership_snapshot_reported_honestly(monkeypatch):
    """The independent pre-check passed, but the worker's own (separate)
    universe read excluded the instrument -- e.g. a race against a
    concurrent portfolio-service change. The route must not claim a
    backfill ran."""
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(dict(status="COMPLETED", failure_reason=None, requested_instruments=0, instruments=[]))
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def test_malformed_instrument_id_rejected():
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": "not-a-uuid"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Persistence-path invocation: a real IndiaMarketDataPopulationJobs wired to
# an in-memory SQLite repository, driven end-to-end through the HTTP route.
# Only the network-shaped NseHistoricalDailyProvider is replaced, exactly as
# tests/test_nse_daily_backfill.py does for the whole-universe path -- this
# is the same persist_daily_result boundary, invoked through the new route.
# ---------------------------------------------------------------------------

def _thread_safe_store() -> SqliteResearchPersistence:
    import sqlite3
    store = SqliteResearchPersistence()
    store._connection = sqlite3.connect(":memory:", check_same_thread=False)
    store._connection.row_factory = sqlite3.Row
    store._connection.execute("PRAGMA foreign_keys = ON")
    store.migrate()
    return store


class _FakeProvider:
    def __init__(self, *args):
        self.calls = []

    async def fetch(self, key, *, start, end, **kwargs):
        self.calls.append((key, start, end))
        values = [DailyMarketBar(global_instrument_id=key, trading_date=start, open=Decimal("10"),
            high=Decimal("12"), low=Decimal("9"), close=Decimal("11"), volume=0, currency="INR",
            provider="NSE", provider_symbol="20MICRONS", source_mode="REAL",
            source_url="https://www.nseindia.com/history", retrieved_at=NOW)]
        return NseHistoricalResult(key, start, end, provider_symbol="20MICRONS", http_status=200,
            status="SUCCESS", rows_parsed=1, bars=values)

    async def aclose(self):
        pass


def test_route_genuinely_invokes_the_real_persistence_path(monkeypatch):
    repo = ResearchRepository(persistence=_thread_safe_store())
    metadata_row = equity_metadata()
    orchestrator = SimpleNamespace(
        global_instrument_metadata=AsyncMock(return_value=metadata_row),
        active_global_equities=AsyncMock(return_value=[metadata_row | {"exchange": metadata_row["primaryExchange"]}]),
    )
    jobs = IndiaMarketDataPopulationJobs(repo, None, orchestrator, Settings(), clock=lambda: NOW, sleep=AsyncMock())
    monkeypatch.setattr(backfill, "NseHistoricalDailyProvider", _FakeProvider)
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)

    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS,
        json={"global_instrument_id": str(ID), "start": START.isoformat(), "end": START.isoformat()})

    assert response.status_code == 200
    body = response.json()
    assert body["instrument"]["status"] == "SUCCESS"
    assert body["instrument"]["rows_persisted"] == 1
    persisted = repo.persistence.load_daily_market_bars({ID})
    assert len(persisted) == 1 and persisted[0].trading_date == START and persisted[0].provider == "NSE"


def test_route_never_touches_etf_or_whole_universe_paths(monkeypatch):
    """The route calls only backfill_daily_bars; it must never reach
    populate_daily_bars/ingest_history (ETF) or submit() (whole-universe)."""
    orchestrator = _FakeOrchestrator(metadata=equity_metadata())
    jobs = _FakeJobs(_summary())
    jobs.populate_daily_bars = AsyncMock()
    jobs.submit = AsyncMock()
    _wire(monkeypatch, orchestrator=orchestrator, jobs=jobs)
    response = TestClient(fastapi_app).post(ROUTE, headers=ADMIN_HEADERS, json={"global_instrument_id": str(ID)})
    assert response.status_code == 200
    jobs.populate_daily_bars.assert_not_called()
    jobs.submit.assert_not_called()
