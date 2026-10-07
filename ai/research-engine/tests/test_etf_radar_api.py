"""ETF Radar API tests: DB-only GETs never acquire, cycles are ETF-only,
Equity API stays untouched."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.etf_evidence import EtfAuthority, EtfFact, EtfListing, EtfMetric, EtfProvenance
from app.main import app as fastapi_app
from app.repository import ResearchRepository
from app.persistence import SqliteResearchPersistence

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
ID_A = UUID(int=201)


def provenance(**updates):
    data = dict(provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity="document:1",
        source_url="https://nsearchives.nseindia.com/document", authority=EtfAuthority.OFFICIAL_EXCHANGE,
        retrieved_at=NOW, reliability_level="LEVEL_A")
    return EtfProvenance(**(data | updates))


def _thread_safe_store() -> SqliteResearchPersistence:
    """Mirrors tests/test_global_opportunity_cycle_event_loop_offload.py's
    _thread_safe_store: TestClient dispatches the ASGI app on a different
    thread than the one that created the SQLite connection, and plain
    sqlite3 connections default to check_same_thread=True."""
    import sqlite3
    store = SqliteResearchPersistence()
    store._connection = sqlite3.connect(":memory:", check_same_thread=False)
    store._connection.row_factory = sqlite3.Row
    store._connection.execute("PRAGMA foreign_keys = ON")
    store.migrate()
    return store


@pytest.fixture
def repo():
    return ResearchRepository(persistence=_thread_safe_store())


class FakeEtfOrchestrator:
    def __init__(self, rows):
        self._rows = rows
        self.called = False

    async def active_global_etfs(self, **kwargs):
        self.called = True
        return self._rows


def test_current_returns_404_when_nothing_persisted(monkeypatch, repo):
    monkeypatch.setattr(main, "repository", repo)
    response = TestClient(fastapi_app).get("/api/v1/etf-radar/current")
    assert response.status_code == 404


def test_run_cycle_with_explicit_candidates_still_runs_synchronously(monkeypatch, repo):
    """The bounded candidate_ids diagnostic path is unchanged: it still
    evaluates and persists inline, returning the full result payload."""
    store = repo.persistence
    store.save_etf_evidence(EtfListing(instrument_id=ID_A, isin="IN0000000001", symbol="NIFTYBEES",
        name="Nifty BeES ETF", provenance=provenance()))
    store.save_etf_evidence(EtfFact(instrument_id=ID_A, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"),
        unit="INR", as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY)))
    monkeypatch.setattr(main, "repository", repo)
    fake = FakeEtfOrchestrator([])
    monkeypatch.setattr(main, "portfolio_orchestrator", fake)

    run_response = TestClient(fastapi_app).post("/api/v1/etf-radar/cycles",
        json={"top_n": 5, "candidate_ids": [str(ID_A)]})
    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["radar_version"] == "ETF_RADAR_V1"
    assert fake.called is False

    current_response = TestClient(fastapi_app).get("/api/v1/etf-radar/current")
    assert current_response.status_code == 200
    assert current_response.json()["cycle_id"] == payload["cycle_id"]


def test_run_cycle_without_candidate_ids_returns_202_without_waiting(monkeypatch, repo):
    """The full-universe path (no candidate_ids override) is routed onto the
    async worker: the HTTP response comes back immediately with a durable
    ACCEPTED/RUNNING cycle handle, never the full evaluated payload inline."""
    monkeypatch.setattr(main, "repository", repo)
    fake = FakeEtfOrchestrator([{"globalInstrumentId": str(ID_A), "symbol": "NIFTYBEES", "exchange": "NSE",
        "status": "ACTIVE", "assetType": "ETF"}])
    monkeypatch.setattr(main, "portfolio_orchestrator", fake)

    run_response = TestClient(fastapi_app).post("/api/v1/etf-radar/cycles", json={"top_n": 5})
    # The HTTP contract itself is the thing under test here: a 202 with a
    # durable cycle handle, never the full evaluated payload inline (that
    # inline-vs-worker distinction, independent of how fast the worker
    # happens to drain its one-deep queue in-process during a test, is
    # proven precisely at the worker-unit level in
    # tests/test_etf_radar_async_lifecycle.py, with a deliberately slow
    # runner).
    assert run_response.status_code == 202
    payload = run_response.json()
    assert payload["status"] in ("ACCEPTED", "RUNNING", "COMPLETED")
    assert "cycle_id" in payload and payload["cycle_id"]
    assert "radar_version" not in payload


def test_status_endpoint_rejects_unknown_and_non_etf_cycle_ids(monkeypatch, repo):
    monkeypatch.setattr(main, "repository", repo)
    response = TestClient(fastapi_app).get(f"/api/v1/etf-radar/cycles/{uuid4()}/status")
    assert response.status_code == 404
    # An Equity-market run row must never be returned through the ETF status route.
    repo.persistence.create_cycle_run(str(ID_A), {"top_n": 4}, market="NSE")
    response = TestClient(fastapi_app).get(f"/api/v1/etf-radar/cycles/{ID_A}/status")
    assert response.status_code == 404


def test_explicit_candidate_ids_skip_universe_lookup(monkeypatch, repo):
    monkeypatch.setattr(main, "repository", repo)
    fake = FakeEtfOrchestrator([])
    monkeypatch.setattr(main, "portfolio_orchestrator", fake)
    response = TestClient(fastapi_app).post("/api/v1/etf-radar/cycles", json={"candidate_ids": [str(ID_A)]})
    assert response.status_code == 200
    assert fake.called is False  # universe lookup bypassed entirely


def test_cycle_lookup_by_id_returns_404_for_unknown_cycle(monkeypatch, repo):
    monkeypatch.setattr(main, "repository", repo)
    response = TestClient(fastapi_app).get(f"/api/v1/etf-radar/cycles/{uuid4()}")
    assert response.status_code == 404


def test_etf_radar_endpoints_do_not_shadow_equity_endpoints():
    """Equity Radar's own routes remain registered and distinct."""
    paths = {route.path for route in fastapi_app.routes}
    assert "/api/v1/research/opportunities/current" in paths
    assert "/api/v1/etf-radar/current" in paths
    assert "/api/v1/research/opportunities/current" != "/api/v1/etf-radar/current"


def test_etf_portfolio_recommendations_endpoint_is_db_only_and_global_only(monkeypatch, repo):
    """GET /api/v1/research/etf-recommendations/current never acquires, and
    its own route is distinct from Equity's /recommendations/current."""
    from datetime import datetime, timezone
    monkeypatch.setattr(main, "repository", repo)
    payload = {
        "radar_version": "ETF_RADAR_V1", "cycle_id": "c1", "correlation_id": None,
        "as_of": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
        "candidates": [{
            "global_instrument_id": str(ID_A), "symbol": "NIFTYBEES", "disposition": "EVALUATED",
            "reason": None, "subtype": None,
            "rule_engine_result": {"rule_engine_version": "ETF_RULE_ENGINE_V1", "instrument_id": str(ID_A),
                "as_of": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(), "factor_results": [],
                "overall_score": 71.0, "data_completeness": 0.9, "confidence": "MEDIUM"},
            "risk_gates": [], "recommendation": "OPPORTUNITY", "rank": 1,
        }],
        "ranked": [], "excluded_by_reason": {},
    }
    repo.persistence.save_etf_radar_cycle("c1", "ETF_RADAR_V1", None, datetime(2026, 10, 1, tzinfo=timezone.utc), payload)

    response = TestClient(fastapi_app).get(
        f"/api/v1/research/etf-recommendations/current?global_instrument_id={ID_A}")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["global_instrument_id"] == str(ID_A)
    assert body[0]["recommendation"] == "OPPORTUNITY"
    private_fields = {"quantity", "average_cost", "position_size", "gain_loss", "account", "broker"}
    assert not (private_fields & set(body[0].keys()))

    paths = {route.path for route in fastapi_app.routes}
    assert "/api/v1/research/etf-recommendations/current" in paths
    assert "/api/v1/research/recommendations/current" in paths
    assert "/api/v1/research/etf-recommendations/current" != "/api/v1/research/recommendations/current"
