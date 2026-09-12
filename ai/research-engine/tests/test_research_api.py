from fastapi.testclient import TestClient
import asyncio
from uuid import UUID, uuid4

import httpx
import pytest

import app.main as main
from app.main import app
from app.portfolio_orchestration import PortfolioResearchOrchestrator, PortfolioServiceUnavailableError
from app.repository import ResearchRepository
from app.settings import Settings
from app.models import (
    PortfolioResearchCompany,
    PortfolioResearchSummary,
    ReliabilityLevel,
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceMode,
    ProvenancedValue,
)
from app.persistence import SqliteResearchPersistence
from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from datetime import datetime, timezone
from decimal import Decimal


def test_research_company_summary_api_returns_demo_evidence_without_raw_bodies() -> None:
    client = TestClient(app)

    companies = client.get("/api/v1/research/companies").json()
    assert companies
    instrument_id = companies[0]["instrumentId"]

    summary = client.get(f"/api/v1/research/companies/{instrument_id}/summary")

    assert summary.status_code == 200
    body = summary.json()
    assert body["demo"] is True
    assert "overallScore" in body["catalystScore"]
    assert body["recentEvents"]
    assert "rawText" not in str(body)
    assert "normalizedText" not in str(body)


def test_research_company_summary_api_is_object_and_normalizes_casefolded_bucket_aliases() -> None:
    client = TestClient(app)
    instrument_id = client.get("/api/v1/research/companies").json()[0]["instrumentId"]
    expected = main.repository.summary(UUID(instrument_id), allow_demo=True)

    response = client.get(f"/api/v1/research/companies/{instrument_id}/summary")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.text.lstrip().startswith("{")
    body = response.json()
    assert isinstance(body, dict)
    assert isinstance(body["shareholdingSnapshots"], list)
    assert body["shareholdingFreshness"] in {"REAL", "UNAVAILABLE"}
    buckets = body["catalystScore"]["buckets"]
    assert len({key.casefold() for key in buckets}) == len(buckets)
    assert not ({"Guidance", "GUIDANCE"} <= set(buckets))
    category_evidence = body["catalystScore"]["categoryEvidence"]
    assert "CAPEX & Capacity" not in category_evidence
    for evidence in category_evidence.values():
        supporting_events = evidence["supportingEvents"]
        if evidence["status"] == "NO_EVIDENCE":
            assert supporting_events == []
            continue
        assert supporting_events
        identities = {
            event.get("independenceKey") or event["sourceDocumentId"]
            for event in supporting_events
        }
        assert evidence["sourceCount"] == len(identities)
    assert body["catalystScore"]["overallScore"] == expected.catalyst_score.overall_score
    assert body["shareholdingSnapshots"] == [snapshot.model_dump(mode="json", by_alias=True) for snapshot in expected.shareholding_snapshots]


def test_company_summary_serializes_unknown_basis_financial_history_from_persisted_facts(monkeypatch) -> None:
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    instrument_id = repository.list_profiles()[0].instrument_id
    retrieved = datetime(2026, 8, 1, tzinfo=timezone.utc)
    facts = []
    def add(period: str, period_type: str, metric: str, value: str) -> None:
        facts.append(FinancialFact(
            FinancialFactKey(instrument_id, metric, period, period_type, None),
            ProvenancedValue(value=Decimal(value), unit="INR lakh" if metric != "eps" else "INR per share",
                source_url="https://nsearchives.nseindia.com/result.pdf", source_name="NSE", source_type="EXCHANGE", retrieved_at=retrieved),
            FactSourceTier.OFFICIAL_NSE, "NSE", f"nse:{period}:{metric}", SourceMode.REAL,
        ))
    for period in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-06-30", "2025-03-31"):
        for metric, value in (("revenue", "228093"), ("pat", "31654"), ("eps", "1.63")):
            add(period, "QUARTERLY", metric, value)
    for period in ("2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31", "2022-03-31"):
        for metric, value in (("revenue", "803897"), ("pat", "69263"), ("eps", "3.57")):
            add(period, "ANNUAL", metric, value)
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(repository, "financial_facts_for", lambda _instrument_id: facts)

    body = TestClient(app).get(f"/api/v1/research/companies/{instrument_id}/summary").json()

    assert body["latestQuarterlyResult"]["period"] == "2026-06-30"
    assert [item["period"] for item in body["financialResultHistory"] if item["periodType"] == "QUARTERLY"] == ["2026-06-30", "2026-03-31", "2025-12-31", "2025-06-30"]
    assert [item["period"] for item in body["financialResultHistory"] if item["periodType"] == "ANNUAL"] == ["2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31"]
    assert {item["reportingBasis"] for item in body["financialResultHistory"]} == {None}


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("post", "/api/v1/research/prefetch"),
        ("get", "/api/v1/research/prefetch/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"),
        ("post", "/api/v1/research/companies/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/refresh"),
        ("post", "/api/v1/research/companies/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/backfill"),
        ("post", "/api/v1/research/portfolios/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/refresh"),
        ("get", "/api/v1/research/refresh-jobs/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"),
        ("get", "/api/v1/research/portfolios/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/refresh-job/active"),
    ),
)
def test_retired_legacy_research_routes_return_404(method: str, path: str) -> None:
    response = getattr(TestClient(app), method)(path)

    assert response.status_code == 404


def test_openapi_exposes_only_the_readiness_and_analysis_cutover_routes() -> None:
    paths = TestClient(app).get("/openapi.json").json()["paths"]

    assert "/api/v1/research/readiness/{global_instrument_id}" in paths
    assert "/api/v1/research/readiness/{global_instrument_id}/ensure" in paths
    assert "/api/v1/research/analysis/{global_instrument_id}" in paths
    assert "/api/v1/research/prefetch" not in paths
    assert "/api/v1/research/prefetch/{instrument_id}" not in paths
    assert "/api/v1/research/companies/{instrument_id}/refresh" not in paths
    assert "/api/v1/research/companies/{instrument_id}/backfill" not in paths
    assert "/api/v1/research/portfolios/{portfolio_id}/refresh" not in paths
    assert "/api/v1/research/refresh-jobs/{job_id}" not in paths
    assert "/api/v1/research/portfolios/{portfolio_id}/refresh-job/active" not in paths


def test_portfolio_research_summary_api_is_read_only(monkeypatch) -> None:
    class FakePortfolioOrchestrator:
        def __init__(self) -> None:
            self.read_called = False
            self.refresh_called = False

        async def read_portfolio_summary(self, portfolio_id, correlation_id=None, identity_headers=None):
            self.read_called = True
            return PortfolioResearchSummary(portfolio_id=portfolio_id)

        async def refresh_portfolio(self, portfolio_id, correlation_id=None, identity_headers=None):
            self.refresh_called = True
            return PortfolioResearchSummary(portfolio_id=portfolio_id)

    fake = FakePortfolioOrchestrator()
    monkeypatch.setattr(main, "portfolio_orchestrator", fake)
    client = TestClient(app)

    response = client.get("/api/v1/research/portfolios/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/summary")

    assert response.status_code == 200
    assert response.json()["portfolioId"] == "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
    assert fake.read_called is True
    assert fake.refresh_called is False


def test_portfolio_summary_api_is_object_and_normalizes_casefolded_company_evidence_without_losing_shareholding(monkeypatch) -> None:
    instrument_id = uuid4()
    periods = (
        datetime(2026, 6, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 31, tzinfo=timezone.utc),
        datetime(2025, 12, 31, tzinfo=timezone.utc),
        datetime(2025, 9, 30, tzinfo=timezone.utc),
    )
    snapshots = [
        ShareholdingSnapshot(
            instrument_id=instrument_id,
            period_end=period,
            source_provider="NSE",
            source_type="NSE_SHAREHOLDING_XBRL",
            source_identity_key=f"NSE_SHAREHOLDING:{index}",
            source_url=f"https://nsearchives.nseindia.com/corporate/xbrl/{index}.xml",
            confidence=Decimal("0.95"),
            reliability_level=ReliabilityLevel.LEVEL_A,
            source_mode=SourceMode.REAL,
            values=[ShareholdingSnapshotValue(
                category=ShareholdingCategory.PROMOTER,
                percentage=Decimal("49.40"),
                raw_source_label="Promoter and Promoter Group",
                source_locator="nse-xbrl:fixture",
                evidence_text="Official NSE XBRL fixture",
            )],
        )
        for index, period in enumerate(periods, start=1)
    ]

    class FakePortfolioOrchestrator:
        async def read_portfolio_summary(self, portfolio_id, correlation_id=None, identity_headers=None):
            return PortfolioResearchSummary(
                portfolio_id=portfolio_id,
                companies=[PortfolioResearchCompany(
                    instrument_id=instrument_id,
                    company_name="Generic Global Equity",
                    status="RESOLVED_RESEARCH_AVAILABLE",
                    evidence_coverage={
                        "Guidance": "POSITIVE_EVIDENCE",
                        "GUIDANCE": "",
                        "Growth": "NO_EVIDENCE",
                    },
                    shareholding_snapshots=snapshots,
                    shareholding_freshness="REAL",
                )],
            )

    monkeypatch.setattr(main, "portfolio_orchestrator", FakePortfolioOrchestrator())
    client = TestClient(app)
    response = client.get("/api/v1/research/portfolios/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/summary")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.text.lstrip().startswith("{")
    body = response.json()
    assert isinstance(body, dict)
    company = body["companies"][0]
    assert company["evidenceCoverage"]["GUIDANCE"] == "POSITIVE_EVIDENCE"
    assert len({key.casefold() for key in company["evidenceCoverage"]}) == len(company["evidenceCoverage"])
    assert len(company["shareholdingSnapshots"]) == 4
    assert company["shareholdingFreshness"] == "REAL"
    assert [snapshot["periodEnd"][:10] for snapshot in company["shareholdingSnapshots"]] == [period.date().isoformat() for period in periods]


class _GlobalInstrumentClient:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def get(self, url: str, headers: dict | None = None) -> httpx.Response:
        self.calls.append({"method": "GET", "url": url, "headers": headers or {}})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def post(self, url: str, headers: dict | None = None) -> httpx.Response:
        self.calls.append({"method": "POST", "url": url, "headers": headers or {}})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _global_master_payload(global_id: UUID) -> dict:
    return {
        "globalInstrumentId": str(global_id), "canonicalName": "Generic Components Limited",
        "isin": "INE000A01010", "assetType": "EQUITY", "country": "IN", "currency": "INR",
        "primaryExchange": "NSE", "primarySymbol": "GENERIC",
        "providerMappings": [
            {"provider": "NSE", "providerSymbol": "GENERIC", "status": "VERIFIED", "exchange": "NSE"},
            {"provider": "YAHOO_FINANCE", "providerSymbol": "GENERIC.NS", "status": "VERIFIED", "exchange": "NSE"},
            {"provider": "NSE", "providerSymbol": "BAD_ALIAS", "status": "INVALID", "exchange": "NSE"},
        ],
    }


def test_global_master_api_restores_unknown_profile_with_verified_mappings() -> None:
    global_id = uuid4()
    url = f"http://portfolio-service/api/v1/instruments/{global_id}"
    client = _GlobalInstrumentClient(httpx.Response(200, json=_global_master_payload(global_id), request=httpx.Request("GET", url)))
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client)

    assert asyncio.run(orchestrator.restore_global_profile(global_id, identity_headers={"X-AIP-User-Id": "user"})) is True
    profile = repo.profile(global_id)
    assert profile.instrument_id == global_id
    assert profile.provider_instrument_ids["NSE"] == "GENERIC"
    assert profile.provider_instrument_ids["YAHOO_FINANCE"] == "GENERIC.NS"
    assert "BAD_ALIAS" not in profile.provider_instrument_ids.values()
    assert client.calls == [{"method": "GET", "url": url, "headers": {"X-AIP-User-Id": "user"}}]


def test_global_master_restore_preserves_verified_international_mapping_for_existing_drawer_path() -> None:
    global_id = uuid4(); payload = _global_master_payload(global_id)
    payload.update({"canonicalName": "Aalberts N.V.", "isin": "NL0000852564", "country": "NL", "currency": "EUR", "primaryExchange": "XAMS", "primarySymbol": "AALB"})
    payload["providerMappings"] = [{"provider": "EODHD", "providerSymbol": "AALB.AS", "providerInstrumentId": "AALB.AS", "status": "VERIFIED", "exchange": "XAMS", "currency": "EUR"}]
    url = f"http://portfolio-service/api/v1/instruments/{global_id}"
    client = _GlobalInstrumentClient(httpx.Response(200, json=payload, request=httpx.Request("GET", url)))
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    assert asyncio.run(PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=client).restore_global_profile(global_id))
    assert repo.profile(global_id).provider_instrument_ids == {"EODHD": "AALB.AS"}


def test_direct_summary_restores_from_global_master_without_portfolio_hydration(monkeypatch) -> None:
    global_id = uuid4()
    url = f"http://portfolio-service/api/v1/instruments/{global_id}"
    lookup_client = _GlobalInstrumentClient(httpx.Response(200, json=_global_master_payload(global_id), request=httpx.Request("GET", url)))
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    orchestrator = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=lookup_client)
    monkeypatch.setattr(main, "repository", repo)
    monkeypatch.setattr(main, "portfolio_orchestrator", orchestrator)

    response = TestClient(app).get(f"/api/v1/research/companies/{global_id}/summary", headers={"X-AIP-User-Id": "user"})

    assert response.status_code == 200
    assert response.json()["profile"]["instrumentId"] == str(global_id)
    assert len(lookup_client.calls) == 1
    assert lookup_client.calls[0]["method"] == "GET"


def test_global_master_404_is_unresolved_and_transient_failure_is_not_cached() -> None:
    global_id = uuid4()
    url = f"http://portfolio-service/api/v1/instruments/{global_id}"
    repo = ResearchRepository(settings=Settings(research_demo_enabled=False))
    missing = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=_GlobalInstrumentClient(httpx.Response(404, request=httpx.Request("GET", url))))
    # The orchestrator's 404 is explicit and does not register an alias profile.
    from app.portfolio_orchestration import GlobalInstrumentNotFoundError
    try:
        asyncio.run(missing.restore_global_profile(global_id))
        assert False, "expected global master 404"
    except GlobalInstrumentNotFoundError:
        pass
    failing = PortfolioResearchOrchestrator(repo, Settings(portfolio_service_base_url="http://portfolio-service"), client=_GlobalInstrumentClient(httpx.ConnectError("down")))
    try:
        asyncio.run(failing.restore_global_profile(global_id))
        assert False, "expected service-unavailable outcome"
    except PortfolioServiceUnavailableError:
        pass
    assert all(profile.instrument_id != global_id for profile in repo.list_profiles())
