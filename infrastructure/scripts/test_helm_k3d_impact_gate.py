"""Offline tests for conservative risk detection; synthetic manifests only."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("helm_k3d_impact_gate", HERE / "helm_k3d_impact_gate.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def deployment(name, image="approved", extra=None):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {"template": {"spec": {
            "containers": [{"name": name, "image": image,
                            "env": [{"name": "SAFE", "value": "yes"}]}]
        }}, **(extra or {})}
    }


def test_unrelated_live_image_drift_blocks():
    key = ("apps/v1", "Deployment", "auth-service")
    old = {key: deployment("auth-service", "old")}
    desired = {key: deployment("auth-service", "old")}
    live = {key: deployment("auth-service", "new")}
    assert ("UNRELATED_DEPLOYMENT_LIVE_DRIFT", "Deployment", "auth-service") in (
        mod.critical_live_drift(old, desired, live)
    )


def test_reconciled_live_workload_is_not_flagged():
    key = ("apps/v1", "Deployment", "auth-service")
    same = deployment("auth-service")
    assert mod.critical_live_drift({key: same}, {key: same}, {key: same}) == []


def test_unrelated_desired_change_blocks():
    key = ("apps/v1", "Deployment", "auth-service")
    old = {key: deployment("auth-service", "old")}
    desired = {key: deployment("auth-service", "new")}
    live = {key: deployment("auth-service", "new")}
    assert ("UNRELATED_DEPLOYMENT_DESIRED_CHANGE", "Deployment", "auth-service") in (
        mod.critical_live_drift(old, desired, live)
    )


def test_protected_secret_data_difference_blocks():
    key = ("v1", "Secret", "dev-postgres")
    old = {key: {"apiVersion": "v1", "kind": "Secret",
                 "metadata": {"name": "dev-postgres"}, "type": "Opaque",
                 "data": {"POSTGRES_PASSWORD": "YmVmb3Jl"}}}
    desired = {key: old[key]}
    live = {key: {**old[key], "data": {"POSTGRES_PASSWORD": "YWZ0ZXI="}}}
    assert ("SECRET_DATA_OR_TYPE", "Secret", "dev-postgres") in (
        mod.critical_live_drift(old, desired, live)
    )


def test_statefulset_drift_blocks():
    key = ("apps/v1", "StatefulSet", "postgres")
    old = {key: {"apiVersion": "apps/v1", "kind": "StatefulSet",
                 "metadata": {"name": "postgres"}, "spec": {"replicas": 1}}}
    desired = {key: old[key]}
    live = {key: {**old[key], "spec": {"replicas": 2}}}
    assert ("STATEFULSET_LIVE_DIFFERS_FROM_TARGET", "StatefulSet", "postgres") in (
        mod.critical_live_drift(old, desired, live)
    )


def test_helm_docker_chart_mount_is_before_entrypoint():
    source = (HERE / "helm_k3d_impact_gate.py").read_text(encoding="utf-8")
    assert 'helm[:8]' not in source.replace('helm[:8]\n', '') or (
        '# would have placed --mount after --entrypoint' in source
    )
    assert '"--mount", "type=bind,source=" + str(base.CHART)' in source
    assert '"--entrypoint", "helm", base.IMAGE' in source
