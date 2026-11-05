"""Pure unit tests only; no Docker, kubeconfig, network, or cluster access."""
import base64
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("helm_acceptance", HERE / "helm_k3d_acceptance.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_secret_stringdata_matches_encoded_live_without_exposing_plaintext():
    value = "synthetic-secret-never-a-real-password"
    requested = {"kind": "Secret", "type": "Opaque", "stringData": {"KEY": value}}
    actual = {"kind": "Secret", "type": "Opaque",
              "data": {"KEY": base64.b64encode(value.encode()).decode()}}
    differences = []
    mod.compare(mod.selected(requested), mod.selected(actual), "Secret/test", differences)
    assert differences == []


def test_secret_mutation_is_blocked_with_path_only():
    differences = []
    mod.compare({"data": {"KEY": "encoded-before"}},
                {"data": {"KEY": "encoded-after"}}, "Secret/test", differences)
    assert differences == ["Secret/test.data.KEY"]
    assert not any("encoded-before" in x or "encoded-after" in x for x in differences)


def test_kubernetes_generated_fields_are_ignored_but_intended_change_is_not():
    requested = {"kind": "Deployment", "spec": {"replicas": 1}}
    actual = {"kind": "Deployment", "spec": {"replicas": 1, "progressDeadlineSeconds": 600}}
    differences = []
    mod.compare(mod.selected(requested), mod.selected(actual), "Deployment/example", differences)
    assert differences == []
    actual["spec"]["replicas"] = 2
    differences.clear()
    mod.compare(mod.selected(requested), mod.selected(actual), "Deployment/example", differences)
    assert differences == ["Deployment/example.spec.replicas"]
