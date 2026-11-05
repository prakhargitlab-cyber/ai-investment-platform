"""Synthetic tests: preview is in-memory and never changes Secrets."""
from copy import deepcopy

import pytest
import helm_k3d_reconciliation_preview as preview


def documents():
    names = sorted(preview.PRESERVE_DEPLOYMENTS)
    old, new, live = {}, {}, {}
    for name in names:
        key = ("apps/v1", "Deployment", name)
        before = {"apiVersion": "apps/v1", "kind": "Deployment",
                  "metadata": {"name": name},
                  "spec": {"template": {"spec": {"containers": [
                      {"name": name, "image": "old-image"}
                  ]}}}}
        candidate = deepcopy(before)
        current = deepcopy(before)
        current["spec"]["template"]["spec"]["containers"][0]["image"] = "live-image"
        old[key], new[key], live[key] = before, candidate, current
    for index in range(54 - len(names)):
        name = "resource-" + str(index)
        key = ("v1", "ConfigMap", name)
        item = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}}
        old[key], new[key], live[key] = item, deepcopy(item), deepcopy(item)
    return old, new, live


def test_preserves_original_target_and_only_17_candidate_specs():
    import json
    old, new, live = documents()
    original = deepcopy(new)
    payload = json.loads(preview.preview_request(old, new, live))
    assert len(payload["resources"]) == 54
    assert new == original
    for item in payload["resources"]:
        if item["old"]["kind"] == "Deployment":
            assert item["new"]["spec"] == item["live"]["spec"]
        else:
            assert item["new"] == item["old"]


def test_rejects_missing_expected_deployment():
    old, new, live = documents()
    key = ("apps/v1", "Deployment", "api-gateway")
    del old[key], new[key], live[key]
    extra = ("v1", "ConfigMap", "extra")
    old[extra] = new[extra] = live[extra] = {
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "extra"}
    }
    with pytest.raises(RuntimeError, match="PREVIEW_EXPECTED_DEPLOYMENTS_MISSING"):
        preview.preview_request(old, new, live)


def test_invalid_live_deployment_spec_blocks():
    old, new, live = documents()
    key = ("apps/v1", "Deployment", "auth-service")
    live[key].pop("spec")
    with pytest.raises(RuntimeError, match="PREVIEW_LIVE_SPEC_INVALID"):
        preview.preview_request(old, new, live)
