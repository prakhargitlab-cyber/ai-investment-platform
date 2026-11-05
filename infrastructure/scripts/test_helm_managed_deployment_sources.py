"""Static regressions for Helm-first full-stack deployment.

These protect intended source ownership. They do not replace Helm lint or a
real server-connected three-way patch check on an existing release.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = (ROOT / "platform.ps1").read_text(encoding="utf-8-sig")
CHART = ROOT / "infrastructure/helm/ai-investment-platform/templates"


def function_body(source, name):
    start = source.index("function " + name + " {")
    end = source.find("\nfunction ", start + 12)
    return source[start:end if end >= 0 else None]


def test_fresh_full_stack_uses_helm_image_tag_and_does_not_patch_after_helm():
    deploy = function_body(PLATFORM, "Deploy-Helm")
    start = function_body(PLATFORM, "Start-Platform")
    assert 'global.imageTag=$ImageTag' in deploy
    assert 'ibkr.enabled=$ibkrEnabled' in deploy
    assert 'ibkrConnector.replicaCount=$connectorReplicas' in deploy
    assert "--force-conflicts" not in deploy
    assert "HELM_RELEASE_DISCOVERY_FAILED" in deploy
    assert "EXISTING_HELM_RELEASE_REQUIRES_REVIEWED_RECONCILIATION" in deploy
    assert "Set-ImmutableDeploymentImages -Names $selected" not in start
    assert "Apply-LocalRuntimeNormalization" not in start


def test_kafka_startup_probe_chart_owned_not_imperatively_patched():
    source = (CHART / "dev-dependencies.yaml").read_text(encoding="utf-8")
    kafka = source.split("- name: kafka", 1)[1]
    kafka = kafka.split("kind: Service", 1)[0]
    assert "failureThreshold: 120" in kafka
    assert "initialDelaySeconds: 10" in kafka
    assert "timeoutSeconds: 3" in kafka
    normalizer = function_body(PLATFORM, "Apply-LocalRuntimeNormalization")
    assert "kubectl patch deployment kafka" not in normalizer


def test_each_app_template_supports_explicit_per_service_image_pin():
    ai = (CHART / "ai-services.yaml").read_text(encoding="utf-8")
    assert 'get $workloadOverride "imageTagOverride"' in ai
    assert 'aip.imageExplicitTag' in ai
    for template in ("frontend.yaml", "ibkr-connector.yaml"):
        data = (CHART / template).read_text(encoding="utf-8")
        assert '"imageTagOverride"' in data
        assert 'aip.imageExplicitTag' in data
