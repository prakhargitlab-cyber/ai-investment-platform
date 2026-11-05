#!/usr/bin/env python3
"""Validate Helm's actual LOCAL manifest, without writing to Kubernetes.

This is deliberately separate from a live release upgrade impact check.
Never prints rendered manifests, Secret bytes, or environment values.
"""
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infrastructure/helm/ai-investment-platform"
PINNED = (
    "auth-service", "frontend", "ranking-engine", "ibkr-connector",
    "research-engine", "yahoo-finance-mcp", "mcp-gateway",
)
TAG = "ci-pin-immutable-20261105"


def render():
    args = [
        "helm", "template", "ai-investment-platform", str(CHART),
        "--namespace", "ai-investment",
        "--values", str(CHART / "values-dev.yaml"),
        "--set-string", "global.imageTag=ci-global",
    ]
    for name in PINNED:
        args += [
            "--set-string",
            "workloadOverrides." + name + ".imageTagOverride=" + TAG,
        ]
    completed = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, timeout=60,
    )
    if completed.returncode:
        raise RuntimeError("HELM_TEMPLATE_FAILED")
    return list(yaml.safe_load_all(completed.stdout))


def assert_contract(docs):
    objs = [doc for doc in docs if doc is not None]
    indexed = {
        (x["apiVersion"], x["kind"], x["metadata"]["name"]): x
        for x in objs
    }
    if len(indexed) != len(objs) or len(objs) != 54:
        raise RuntimeError("HELM_RENDER_RESOURCE_COUNT_OR_IDENTITY_MISMATCH")
    for name in PINNED:
        key = ("apps/v1", "Deployment", name)
        if key not in indexed:
            raise RuntimeError("PINNED_WORKLOAD_NOT_RENDERED")
        pod = indexed[key]["spec"]["template"]["spec"]
        containers = [x for x in pod["containers"] if x["name"] == name]
        if len(containers) != 1:
            raise RuntimeError("WORKLOAD_CONTAINER_IDENTITY_MISMATCH")
        if containers[0]["image"] != (
                "localhost:5001/ai-investment/" + name + ":" + TAG):
            raise RuntimeError("WORKLOAD_PIN_IGNORED")
        if name == "research-engine":
            init = [x for x in pod.get("initContainers", [])
                    if x["name"] == "wait-for-research-migrations"]
            if len(init) != 1 or init[0]["image"] != containers[0]["image"]:
                raise RuntimeError("RESEARCH_INIT_IMAGE_PIN_IGNORED")
    kafka = indexed[("apps/v1", "Deployment", "kafka")]
    container = next(x for x in kafka["spec"]["template"]["spec"]["containers"]
                     if x["name"] == "kafka")
    probe = container.get("startupProbe", {})
    for field, value in (
        ("initialDelaySeconds", 10),
        ("periodSeconds", 5),
        ("timeoutSeconds", 3),
        ("successThreshold", 1),
        ("failureThreshold", 120),
    ):
        if probe.get(field) != value:
            raise RuntimeError("KAFKA_PROBE_CHART_MISMATCH")
    print("RENDERED_RESOURCE_COUNT: 54")
    print("PINNED_DEPLOYMENTS_AND_RESEARCH_INIT_IMAGE: PASS")
    print("KAFKA_STARTUP_PROBE: PASS")
    print("NO_KUBERNETES_WRITES_PERFORMED")


if __name__ == "__main__":
    try:
        assert_contract(render())
    except Exception:
        print("HELM_RENDER_CONTRACT_BLOCKED")
        sys.exit(2)
