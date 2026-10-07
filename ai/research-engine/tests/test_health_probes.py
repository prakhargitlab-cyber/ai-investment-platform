from unittest.mock import Mock

from fastapi.testclient import TestClient


def test_liveness_is_a_dependency_free_process_sentinel(monkeypatch):
    from app import main

    blocked = Mock(side_effect=AssertionError("liveness must not touch dependencies"))
    monkeypatch.setattr(main, "repository", blocked)
    monkeypatch.setattr(main, "research_readiness_runtime", blocked)

    response = TestClient(main.app).get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_endpoint_remains_distinct_from_liveness():
    from app import main

    paths = {route.path for route in main.app.routes}
    assert "/health/live" in paths
    assert "/health" in paths
    assert "/health/live" != "/health"
