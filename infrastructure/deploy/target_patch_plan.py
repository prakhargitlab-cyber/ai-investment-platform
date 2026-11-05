#!/usr/bin/env python3
"""Read-only local k3d target Deployment patch plan for auth-reset PR #28.

Do not use Helm revision-1 manifest drift as a deployment precondition.
This program NEVER performs Kubernetes writes. It checks only two live
Deployments and a pinned research-engine image. All API reads are scoped
to the explicit k3d node, not the user's default kubeconfig.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

NAMESPACE = "ai-investment"
RELEASE = "ai-investment-platform"
NODE = "k3d-ai-investment-dev-server-0"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"
TARGETS = {"api-gateway": "GATEWAY_CORS_ALLOWED_ORIGINS",
           "auth-service": "AUTH_ALLOWED_PUBLIC_ORIGINS"}
PINNED_RESEARCH_IMAGE = (
    "localhost:5001/ai-investment/research-engine:etf-eq-rl-20261104-210859"
)


class Blocked(ValueError):
    pass


def kubectl(*args):
    command = ["docker", "exec", NODE, "/bin/kubectl",
               "--kubeconfig=" + KUBECONFIG, "-n", NAMESPACE, *args]
    try:
        process = subprocess.run(command, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("CLUSTER_READ_UNAVAILABLE") from exc
    if process.returncode:
        raise Blocked("KUBERNETES_READ_FAILED")
    try:
        return json.loads(process.stdout)
    except ValueError as exc:
        raise Blocked("KUBERNETES_RESPONSE_INVALID") from exc


def one_container(deployment, expected_name):
    if (deployment.get("apiVersion") != "apps/v1"
            or deployment.get("kind") != "Deployment"
            or deployment.get("metadata", {}).get("name") != expected_name
            or deployment.get("metadata", {}).get("namespace") != NAMESPACE):
        raise Blocked("DEPLOYMENT_IDENTITY_MISMATCH")
    labels = deployment["metadata"].get("labels", {})
    annotations = deployment["metadata"].get("annotations", {})
    if (labels.get("app.kubernetes.io/instance") != RELEASE
            or labels.get("app.kubernetes.io/managed-by") != "Helm"
            or annotations.get("meta.helm.sh/release-name") != RELEASE
            or annotations.get("meta.helm.sh/release-namespace") != NAMESPACE):
        raise Blocked("DEPLOYMENT_HELM_OWNERSHIP_UNVERIFIED")
    containers = deployment.get("spec", {}).get("template", {}).get(
        "spec", {}).get("containers", [])
    if len(containers) != 1 or containers[0].get("name") != expected_name:
        raise Blocked("CONTAINER_SCOPE_UNEXPECTED")
    return containers[0]


def target_plan(deployment, name, expected_image, origins):
    container = one_container(deployment, name)
    current_image = container.get("image")
    if not isinstance(current_image, str) or not current_image:
        raise Blocked("CURRENT_IMAGE_INVALID")
    if not isinstance(expected_image, str) or not expected_image.startswith(
            "localhost:5001/ai-investment/" + name + ":"):
        raise Blocked("RELEASE_IMAGE_SCOPE_INVALID")
    env = container.get("env", [])
    if not isinstance(env, list):
        raise Blocked("TARGET_ENV_INVALID")
    env_name = TARGETS[name]
    entries = [e for e in env if isinstance(e, dict) and e.get("name") == env_name]
    if len(entries) > 1:
        raise Blocked("DUPLICATE_ORIGIN_ENV")
    current_origins = entries[0].get("value") if entries else None
    if entries and ("valueFrom" in entries[0]
                    or not isinstance(current_origins, str)):
        raise Blocked("ORIGIN_ENV_USES_UNEXPECTED_SOURCE")
    rv = deployment["metadata"].get("resourceVersion")
    if not isinstance(rv, str) or not rv:
        raise Blocked("RESOURCE_VERSION_UNAVAILABLE")
    # No literal values or Secret-derived environment entries are displayed.
    return {
        "name": name,
        "image_change": current_image != expected_image,
        "origin_change": current_origins != origins,
        "resource_version_present": True,
    }


def validate_research(deployment):
    container = one_container(deployment, "research-engine")
    if container.get("image") != PINNED_RESEARCH_IMAGE:
        raise Blocked("PINNED_RESEARCH_ENGINE_IMAGE_MISMATCH")
    if (deployment.get("status", {}).get("readyReplicas", 0) or 0) < 1:
        raise Blocked("RESEARCH_ENGINE_NOT_READY")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    if (spec.get("namespace") != NAMESPACE
            or spec.get("release") != RELEASE
            or spec.get("revision") != 1
            or set(spec.get("images", {})) != set(TARGETS)):
        raise Blocked("RELEASE_SPEC_INVALID")
    origins = spec.get("origins")
    if (not isinstance(origins, list) or len(origins) != 5
            or len(set(origins)) != len(origins)
            or any(not isinstance(o, str) or not o.startswith("http://localhost:")
                   and not o.startswith("http://127.0.0.1:") for o in origins)):
        raise Blocked("LOCAL_ORIGINS_INVALID")
    print("READ_ONLY_TWO_DEPLOYMENT_PLAN", flush=True)
    validate_research(kubectl("get", "deployment", "research-engine", "-o", "json"))
    print("PINNED_RESEARCH_ENGINE_VERIFIED", flush=True)
    for name in TARGETS:
        obj = kubectl("get", "deployment", name, "-o", "json")
        item = target_plan(obj, name, spec["images"][name]["image"],
                           ",".join(origins))
        print("TARGET:", name, "IMAGE_CHANGE:", item["image_change"],
              "ORIGIN_CHANGE:", item["origin_change"], flush=True)
    print("PATCH_FIELDS_ONLY: image + approved origins on two Deployments")
    print("NO_CLUSTER_WRITES_PERFORMED")
    print("NO_DEPLOYMENT_AUTHORIZATION")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Only print controlled codes; never expose raw responses or env values.
        print("TARGET_PLAN_BLOCKED:",
              str(exc) if isinstance(exc, Blocked) else "UNEXPECTED_ERROR",
              file=sys.stderr)
        print("NO_CLUSTER_WRITES_PERFORMED", file=sys.stderr)
        sys.exit(2)
