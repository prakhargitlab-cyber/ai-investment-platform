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


def test_upgrade_does_not_add_undeclared_dev_profile():
    source = (HERE / "helm_k3d_acceptance.py").read_text(encoding="utf-8")
    assert '"-f", "/chart/values-dev.yaml"' not in source
    assert '"--reuse-values", "--dry-run=server"' in source


def synthetic_three_way():
    from copy import deepcopy
    key = ("apps/v1", "Deployment", "research-engine")
    env = ([{"name": x, "value": "false"} for x in mod.APPROVED_FLAGS]
           + [{"name": x, "value": v} for x, v in mod.PROTECTED_FLAGS.items()])
    old = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "research-engine"},
        "spec": {"template": {"spec": {
            "containers": [{"name": "research-engine", "image": "old-image", "env": env}],
            "initContainers": [{"name": "wait-for-research-migrations", "image": "old-image"}]
        }}}
    }
    new = deepcopy(old)
    new_pod = new["spec"]["template"]["spec"]
    new_pod["containers"][0]["image"] = mod.EXPECTED_IMAGE
    new_pod["initContainers"][0]["image"] = mod.EXPECTED_IMAGE
    for item in new_pod["containers"][0]["env"]:
        if item["name"] in mod.APPROVED_FLAGS:
            item["value"] = "true"
    live = deepcopy(new)
    live["spec"]["template"]["spec"]["containers"][0]["env"].append({
        "name": "EXTRA_LIVE_ONLY", "value": "historical-drift"
    })
    return {key: old}, {key: new}, {key: live}


def test_approved_research_fields_allow_unrelated_live_drift():
    old, new, live = synthetic_three_way()
    assert mod.only_approved_revision_changes(old, new, live)


def test_rejects_unrelated_desired_workload_change():
    old, new, live = synthetic_three_way()
    new[next(iter(new))]["spec"]["replicas"] = 3
    import pytest
    with pytest.raises(mod.Blocked, match="UNAPPROVED_STORED_RELEASE_CHANGE"):
        mod.only_approved_revision_changes(old, new, live)


def test_rejects_unapproved_live_research_image():
    old, new, live = synthetic_three_way()
    live[next(iter(live))]["spec"]["template"]["spec"]["containers"][0]["image"] = "wrong-tag"
    import pytest
    with pytest.raises(mod.Blocked, match="LIVE_RESEARCH_IMAGE_MISMATCH"):
        mod.only_approved_revision_changes(old, new, live)


def test_rejects_wrong_live_execution_gate():
    old, new, live = synthetic_three_way()
    live[next(iter(live))]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "false"
    import pytest
    with pytest.raises(mod.Blocked, match="PROTECTED_FLAG_VALUE_MISMATCH"):
        mod.only_approved_revision_changes(old, new, live)


def test_rejects_extra_desired_resource():
    old, new, live = synthetic_three_way()
    new[("v1", "ConfigMap", "unreviewed")] = {
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "unreviewed"}
    }
    import pytest
    with pytest.raises(mod.Blocked, match="RESOURCE_IDENTITY_CHANGE"):
        mod.only_approved_revision_changes(old, new, live)


def test_legacy_secret_stringdata_to_data_keeps_identical_credential():
    old, new, live = synthetic_three_way()
    key = ("v1", "Secret", "dev-postgres")
    value = "invented-password-for-structural-test"
    old[key] = {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "dev-postgres"}, "type": "Opaque",
                "stringData": {"POSTGRES_PASSWORD": value}}
    new[key] = {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "dev-postgres"}, "type": "Opaque",
                "data": {"POSTGRES_PASSWORD": base64.b64encode(value.encode()).decode()}}
    assert mod.only_approved_revision_changes(old, new, live)


def test_legacy_secret_changed_credential_remains_blocked():
    old, new, live = synthetic_three_way()
    key = ("v1", "Secret", "dev-postgres")
    old[key] = {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "dev-postgres"}, "type": "Opaque",
                "stringData": {"POSTGRES_PASSWORD": "invented-before"}}
    new[key] = {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "dev-postgres"}, "type": "Opaque",
                "data": {"POSTGRES_PASSWORD":
                         base64.b64encode(b"invented-after").decode()}}
    import pytest
    with pytest.raises(mod.Blocked, match="UNAPPROVED_SECRET_CONTENT_CHANGE"):
        mod.only_approved_revision_changes(old, new, live)


def test_legacy_secret_metadata_mutation_remains_blocked():
    old, new, live = synthetic_three_way()
    key = ("v1", "Secret", "dev-searxng")
    value = "invented-secret"
    old[key] = {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "dev-searxng"}, "type": "Opaque",
                "stringData": {"SEARXNG_SECRET": value}}
    new[key] = {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "dev-searxng", "annotations": {"change": "bad"}},
                "type": "Opaque",
                "data": {"SEARXNG_SECRET": base64.b64encode(value.encode()).decode()}}
    import pytest
    with pytest.raises(mod.Blocked, match="UNAPPROVED_SECRET_METADATA_CHANGE"):
        mod.only_approved_revision_changes(old, new, live)
