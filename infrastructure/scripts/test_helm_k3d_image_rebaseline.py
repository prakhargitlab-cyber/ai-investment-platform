"""Synthetic checks for private Helm image-tag reconciliation candidate."""
from copy import deepcopy
import pytest

import helm_k3d_image_rebaseline as plan


def fixture_documents():
    old, candidate, live = {}, {}, {}
    for name in sorted(plan.FIRST_PARTY):
        key = ("apps/v1", "Deployment", name)
        item = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [{
                "name": name, "image": "localhost:5001/ai-investment/" + name + ":dev-123"
            }]}}}
        }
        old[key] = deepcopy(item)
        candidate[key] = deepcopy(item)
        live[key] = deepcopy(item)
        live[key]["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "localhost:5001/ai-investment/" + name + ":stable-456"
        )
    for i in range(54 - len(plan.FIRST_PARTY)):
        key = ("v1", "ConfigMap", "placeholder-" + str(i))
        doc = {"apiVersion": "v1", "kind": "ConfigMap",
               "metadata": {"name": "placeholder-" + str(i)}}
        old[key], candidate[key], live[key] = (
            deepcopy(doc), deepcopy(doc), deepcopy(doc)
        )
    return old, candidate, live


def test_candidate_contains_only_image_tag_overrides():
    old, candidate, live = fixture_documents()
    result = plan.candidate_values(old, candidate, live)
    assert set(result) == {"workloadOverrides"}
    assert len(result["workloadOverrides"]) == 16
    assert all(value == {"imageTagOverride": "stable-456"}
               for value in result["workloadOverrides"].values())
    assert "secret" not in str(result).lower()
    assert "kafka" not in result["workloadOverrides"]


@pytest.mark.parametrize("image", [
    "localhost:5001/ai-investment/auth-service:latest",
    "localhost:5001/ai-investment/auth-service:dev",
    "untrusted.example/ai-investment/auth-service:stable-456",
    "localhost:5001/ai-investment/auth-service:stable-456@sha256:abc",
    "localhost:5001/ai-investment/other-service:stable-456",
    "localhost:5001/ai-investment/auth-service:stable/456",
])
def test_reject_unreviewed_image_references(image):
    with pytest.raises(ValueError):
        plan.image_tag(image, "auth-service")


def test_unknown_containers_cannot_be_inferred():
    old, candidate, live = fixture_documents()
    key = ("apps/v1", "Deployment", "frontend")
    live[key]["spec"]["template"]["spec"]["containers"][0]["name"] = "unknown"
    with pytest.raises(ValueError, match="CONTAINER_IDENTITY_MISMATCH"):
        plan.candidate_values(old, candidate, live)
