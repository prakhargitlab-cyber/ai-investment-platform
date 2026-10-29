"""Offline Helm CLI checks; never contact Kubernetes or print rendered secrets."""
import json
from pathlib import Path
import subprocess

import pytest
import yaml


CHART = Path(__file__).resolve().parents[1]
BASE = yaml.safe_load((CHART / "values.yaml").read_text())


def render(overrides=None, *, dev=True):
    command = ["helm", "template", "review", str(CHART)]
    if dev:
        command += ["-f", str(CHART / "values-dev.yaml")]
    command += ["-f", "-"]
    return subprocess.run(command, input=json.dumps(overrides or {}),
                          text=True, capture_output=True, timeout=30)


def deployments(result):
    assert result.returncode == 0, "Helm rendering failed"
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all(result.stdout)
            if d and d.get("kind") == "Deployment"}


@pytest.mark.parametrize("layer", ["defaults", "service", "image", "workload"])
@pytest.mark.parametrize("unsafe", [{"replicaCount": 2}, {"autoscaling": {"enabled": True}}])
def test_effective_merge_rejects_unsafe_configuration(layer, unsafe):
    # No DEV workload override masking a lower-layer value in these cases.
    overrides = {"global": {"environment": "DEV"}}
    if layer == "defaults":
        overrides["aiServiceDefaults"] = unsafe
    elif layer == "service":
        overrides["aiServices"] = [dict(s, **unsafe) if s["name"] == "research-engine" else s
                                   for s in BASE["aiServices"]]
    else:
        key = "aiServiceImageOverrides" if layer == "image" else "workloadOverrides"
        overrides[key] = {"research-engine": unsafe}
    result = render(overrides, dev=False)
    assert result.returncode != 0
    assert "process-local lock requires" in result.stderr


@pytest.mark.parametrize("unsafe", [{"replicaCount": 2}, {"autoscaling": {"enabled": True}},
                                    {"replicaCount": -1}, {"replicaCount": "bad"}])
def test_dev_overlay_rejects_unsafe_workload(unsafe):
    result = render({"workloadOverrides": {"research-engine": unsafe}})
    assert result.returncode != 0
    assert "process-local lock requires" in result.stderr


@pytest.mark.parametrize("research", [
    {"opportunityRecoveryPaused": False}, {"opportunityRecoveryPaused": "true"},
    {"opportunityRecoveryPaused": None},
    {"opportunityScheduler": {"enabled": True}},
    {"opportunityScheduler": {"enabled": "false"}},
    {"etfManualExecutionEnabled": True}, {"etfManualExecutionEnabled": "false"},
])
def test_dev_rejects_unsafe_or_malformed_execution_flags(research):
    result = render({"research": research})
    assert result.returncode != 0
    assert "protected LOCAL/DEV" in result.stderr


def test_dev_render_is_explicitly_safe_and_recreate_omits_rolling_update():
    result = render({"workloadOverrides": {"research-engine": {
        "rolloutStrategy": {"rollingUpdate": {"maxSurge": 1, "maxUnavailable": 1}}}}})
    d = deployments(result)["research-engine"]
    assert d["spec"]["replicas"] == 1
    assert d["spec"]["strategy"] == {"type": "Recreate"}
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    for name, value in {
        "AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED": "true",
        "AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED": "false",
        "AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED": "false",
        "AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED": "false",
        "AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED": "true",
    }.items():
        assert [e.get("value") for e in env if e["name"] == name] == [value]
    assert not any(d and d.get("kind") == "HorizontalPodAutoscaler"
                   and d["spec"]["scaleTargetRef"]["name"] == "research-engine"
                   for d in yaml.safe_load_all(result.stdout))


def test_effective_workload_override_can_safely_override_unsafe_defaults():
    d = deployments(render({"aiServiceDefaults": {
        "replicaCount": 2, "autoscaling": {"enabled": True}}}))["research-engine"]
    assert d["spec"]["replicas"] == 1


def test_research_only_override_preserves_other_deployments():
    original = deployments(render())
    changed = deployments(render({"workloadOverrides": {"research-engine": {
        "replicaCount": 0, "rolloutStrategy": {"type": "RollingUpdate"}}}}))
    assert changed["research-engine"]["spec"]["replicas"] == 0
    assert original.pop("research-engine") != changed.pop("research-engine")
    assert original == changed


# -- FINAL STABILIZATION: ETF bounded/full-universe execution flags --------
#
# These restore render coverage for AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED
# and AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED, which the chart
# stopped rendering (see values.yaml / values-dev.yaml / ai-services.yaml).

def test_production_defaults_are_fail_closed_for_all_research_execution_flags():
    """With no LOCAL/DEV overlay, every research execution gate renders to its
    safe, fail-closed value straight from the base values.yaml -- independent
    of the LOCAL/DEV-only overrides layered on top by values-dev.yaml."""
    d = deployments(render({"global": {"environment": "DEV"}}, dev=False))["research-engine"]
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    for name in (
        "AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED",
        "AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED",
        "AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED",
    ):
        assert [e.get("value") for e in env if e["name"] == name] == ["false"]


def test_dev_overlay_keeps_both_etf_execution_gates_off_by_default():
    """values-dev.yaml enables Equity manual execution explicitly, but both
    ETF gates must stay false unless a controlled validation run has been
    expressly approved by editing values-dev.yaml itself."""
    d = deployments(render())["research-engine"]
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    for name in ("AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED",
                 "AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED"):
        assert [e.get("value") for e in env if e["name"] == name] == ["false"]
    assert [e.get("value") for e in env if e["name"] == "AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED"] == ["true"]

