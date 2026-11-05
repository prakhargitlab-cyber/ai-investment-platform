"""No-cluster tests for Kafka startup probe scope exception."""
from copy import deepcopy
import pytest

from helm_k3d_image_pin_impact import allow_only_unchanged_live_kafka_probe

KEY = ("apps/v1", "Deployment", "kafka")
PROBE = {
    "tcpSocket": {"port": "broker"},
    "initialDelaySeconds": 10,
    "periodSeconds": 5,
    "timeoutSeconds": 3,
    "successThreshold": 1,
    "failureThreshold": 120,
}


def docs():
    def deployment(probe):
        return {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "kafka"},
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{
                    "name": "kafka", "image": "bitnami/kafka:3",
                    "startupProbe": deepcopy(probe)
                }]}}
            },
        }
    old = {KEY: deployment({**PROBE, "failureThreshold": 30})}
    candidate = {KEY: deployment(PROBE)}
    live = {KEY: deployment(PROBE)}
    return old, candidate, live


def test_allows_only_reviewed_probe_that_is_already_live():
    old, proposed, live = docs()
    result = allow_only_unchanged_live_kafka_probe(old, proposed, live)
    assert result == old
    assert proposed[KEY]["spec"]["template"]["spec"]["containers"][0]["startupProbe"] == PROBE


@pytest.mark.parametrize("mutation", ["live_probe", "desired_probe", "replicas", "image"])
def test_rejects_unreviewed_kafka_changes(mutation):
    old, proposed, live = docs()
    if mutation == "live_probe":
        live[KEY]["spec"]["template"]["spec"]["containers"][0]["startupProbe"]["failureThreshold"] = 40
    elif mutation == "desired_probe":
        proposed[KEY]["spec"]["template"]["spec"]["containers"][0]["startupProbe"]["periodSeconds"] = 8
    elif mutation == "replicas":
        proposed[KEY]["spec"]["replicas"] = 2
    else:
        proposed[KEY]["spec"]["template"]["spec"]["containers"][0]["image"] = "unexpected/kafka:new"
    with pytest.raises(RuntimeError):
        allow_only_unchanged_live_kafka_probe(old, proposed, live)
