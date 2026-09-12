from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from app.settings import Settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHART = REPOSITORY_ROOT / "infrastructure" / "helm" / "ai-investment-platform"


def test_dev_helm_renders_research_engine_with_kubernetes_portfolio_service_address() -> None:
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "configuration-test",
            str(CHART),
            "--values",
            str(CHART / "values-dev.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    research_deployment = next(
        document
        for document in yaml.safe_load_all(rendered)
        if document
        and document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "research-engine"
    )
    environment = {
        item["name"]: item.get("value")
        for item in research_deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["AIP_PORTFOLIO_SERVICE_BASE_URL"] == "http://portfolio-service"


def test_local_development_can_override_portfolio_service_to_localhost(monkeypatch) -> None:
    monkeypatch.setenv("AIP_PORTFOLIO_SERVICE_BASE_URL", "http://localhost:8080")

    assert Settings(_env_file=None).portfolio_service_base_url == "http://localhost:8080"
