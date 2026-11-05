#!/usr/bin/env python3
"""Guarded, explicit two-Deployment PR #28 local-k3d patch.

Unlike a Helm upgrade this intentionally leaves Helm revision 1 unchanged.
It is NOT a general Linux/AKS installer. Default mode is read-only. Requires
--apply --approve-exact-two-services to make Kubernetes writes. The writes
use RFC6902 JSON Patch with test operations on the live resourceVersion,
the existing image and (when present) the origin environment entry.
No other resources or pod spec fields are targeted. If the second write
fails the first may already be applied: FAIL CLOSED, do not blind rollback.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import release_guard
from target_patch_plan import (
    Blocked, NAMESPACE, NODE, KUBECONFIG, PINNED_RESEARCH_IMAGE,
    RELEASE, TARGETS, kubectl, one_container, target_plan,
    validate_research,
)


def command(*arguments, timeout=120):
    cmd = ["docker", "exec", NODE, "/bin/kubectl",
           "--kubeconfig=" + KUBECONFIG, "-n", NAMESPACE, *arguments]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("KUBECTL_UNAVAILABLE_OR_TIMEOUT") from exc
    if proc.returncode:
        raise Blocked("KUBERNETES_COMMAND_FAILED")
    return proc.stdout


def patch_operations(obj, name, image, origins):
    """Only exact image and origin-env paths. Never serialize other env data."""
    info = target_plan(obj, name, image, origins)
    container = one_container(obj, name)
    base = "/spec/template/spec/containers/0"
    ops = [{"op": "test", "path": "/metadata/resourceVersion",
            "value": obj["metadata"]["resourceVersion"]}]
    if info["image_change"]:
        ops += [
            {"op": "test", "path": base + "/image",
             "value": container["image"]},
            {"op": "replace", "path": base + "/image", "value": image},
        ]
    if info["origin_change"]:
        env_name = TARGETS[name]
        env = container.get("env", [])
        indexes = [i for i, entry in enumerate(env)
                   if entry.get("name") == env_name]
        if indexes:
            index = indexes[0]
            ops += [
                {"op": "test", "path": base + "/env/" + str(index),
                 "value": env[index]},
                {"op": "replace", "path": base + "/env/" + str(index),
                 "value": {"name": env_name, "value": origins}},
            ]
        else:
            if "env" in container:
                ops.append({
                    "op": "add", "path": base + "/env/-",
                    "value": {"name": env_name, "value": origins},
                })
            else:
                ops.append({
                    "op": "add", "path": base + "/env",
                    "value": [{"name": env_name, "value": origins}],
                })
    return ops


def verified_images(spec):
    # Reuse the exact validated release specification and registry HEAD digest
    # checks. Only localhost private registry is acceptable in this tool.
    release_guard.validate_spec(spec)
    for name, image in sorted(spec["images"].items()):
        try:
            release_guard.check_registry(name, image)
        except release_guard.Blocked as exc:
            raise Blocked("IMAGE_REGISTRY_DIGEST_CHECK_FAILED") from exc
        print("REGISTRY_DIGEST_VERIFIED:", name, flush=True)


def revision_one():
    args = argparse.Namespace(mode="k3d", node=NODE, namespace=NAMESPACE)
    try:
        release_guard.helm_revision(args, 1, RELEASE)
    except release_guard.Blocked as exc:
        raise Blocked("HELM_REVISION_ONE_NO_LONGER_CURRENT") from exc


def inspect(name):
    return kubectl("get", "deployment", name, "-o", "json")


def check_rollout(name, expected_image, expected_origins):
    command("rollout", "status", "deployment/" + name,
            "--timeout=180s", timeout=210)
    obj = inspect(name)
    info = target_plan(obj, name, expected_image, expected_origins)
    if info["image_change"] or info["origin_change"]:
        raise Blocked("TARGET_READBACK_MISMATCH")
    desired = obj.get("spec", {}).get("replicas", 1)
    status = obj.get("status", {})
    if (status.get("observedGeneration", 0)
            < obj.get("metadata", {}).get("generation", 0)
            or status.get("readyReplicas", 0) != desired
            or status.get("updatedReplicas", 0) != desired):
        raise Blocked("TARGET_ROLLOUT_NOT_READY")
    print("TARGET_ROLLOUT_VERIFIED:", name, flush=True)


def run(spec, apply=False, approval=False):
    if (spec.get("namespace") != NAMESPACE or spec.get("release") != RELEASE
            or spec.get("revision") != 1):
        raise Blocked("RELEASE_SCOPE_UNEXPECTED")
    verified_images(spec)
    revision_one()
    research = inspect("research-engine")
    validate_research(research)
    print("PINNED_RESEARCH_ENGINE_VERIFIED", flush=True)
    origins = ",".join(spec["origins"])
    live = {}
    patches = {}
    for name in sorted(TARGETS):
        obj = inspect(name)
        live[name] = obj
        patches[name] = patch_operations(obj, name,
                                         spec["images"][name]["image"], origins)
        print("TARGET_PATCH_PLAN:", name,
              "OPERATIONS:", len(patches[name]), flush=True)
    if not apply:
        print("NO_KUBERNETES_WRITES_PERFORMED")
        print("REQUIRE_EXPLICIT_APPLY_AND_APPROVAL")
        return 0
    if not approval:
        raise Blocked("EXPLICIT_TWO_SERVICE_APPROVAL_REQUIRED")

    # Recheck release & research immediately before the first write.
    revision_one()
    validate_research(inspect("research-engine"))
    started = []
    for name in sorted(TARGETS):
        # Each patch uses the original resourceVersion; concurrent edits fail
        # atomically. A partially completed two-Deployment rollout must be
        # handled through readback, not blind rollback.
        ops = patches[name]
        if len(ops) == 1:
            print("TARGET_ALREADY_MATCHES:", name, flush=True)
            continue
        try:
            command("patch", "deployment", name, "--type=json",
                    "-p", json.dumps(ops, separators=(",", ":")))
        except Blocked:
            print("PARTIAL_APPLY_POSSIBLE:",
                  ",".join(started) if started else "none", file=sys.stderr)
            raise
        started.append(name)
        print("TARGET_PATCH_SUBMITTED:", name, flush=True)

    for name in sorted(TARGETS):
        check_rollout(name, spec["images"][name]["image"], origins)
    validate_research(inspect("research-engine"))
    revision_one()
    print("TWO_DEPLOYMENT_PATCH_AND_ROLLOUT_VERIFIED")
    print("HELM_REVISION_UNCHANGED_ONE")
    print("UI_AND_CORS_ACCEPTANCE_STILL_REQUIRED")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-exact-two-services", action="store_true")
    args = parser.parse_args()
    if args.approve_exact_two_services and not args.apply:
        raise Blocked("APPROVAL_WITHOUT_APPLY_IS_INVALID")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    return run(spec, apply=args.apply, approval=args.approve_exact_two_services)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("TWO_SERVICE_RELEASE_BLOCKED:",
              str(exc) if isinstance(exc, Blocked) else "UNEXPECTED_ERROR",
              file=sys.stderr)
        sys.exit(2)
