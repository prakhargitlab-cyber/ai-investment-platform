from fastapi.testclient import TestClient

from app.main import app


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


def test_research_refresh_is_safe_fixture_backed() -> None:
    client = TestClient(app)
    instrument_id = client.get("/api/v1/research/companies").json()[0]["instrumentId"]

    response = client.post(
        f"/api/v1/research/companies/{instrument_id}/refresh",
        headers={"X-Correlation-Id": "phase3-api-test"},
    )

    assert response.status_code == 200
    assert response.json()["dataFreshness"] == "DEMO"
