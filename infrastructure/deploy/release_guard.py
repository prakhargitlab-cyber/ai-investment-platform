#!/usr/bin/env python3
"""Fail-closed, cross-platform release inspection (Python 3.10+, stdlib).

The plan and verify commands are read-only. Neither authorizes or performs
a Helm upgrade. A protected-resource Helm three-way patch review is a separate
mandatory release gate.
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

SERVICES = {"auth-service", "api-gateway"}
ORIGINS = {"http://localhost:13000", "http://localhost:18080", "http://localhost:18081"}
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


class Blocked(Exception):
    pass


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != {"namespace", "release", "revision", "images", "origins"}:
        raise Blocked("SPEC_SCHEMA_INVALID")
    if spec["namespace"] != "ai-investment" or spec["release"] != "ai-investment-platform":
        raise Blocked("CLUSTER_RELEASE_UNEXPECTED")
    if type(spec["revision"]) is not int or spec["revision"] != 1:
        raise Blocked("EXPECTED_REVISION_MUST_BE_ONE")
    if not isinstance(spec["origins"], list) or not ORIGINS.issubset(set(spec["origins"])):
        raise Blocked("REQUIRED_ALLOWED_ORIGINS_MISSING")
    images = spec["images"]
    if not isinstance(images, dict) or set(images) != SERVICES:
        raise Blocked("EXPECTED_EXACTLY_TWO_APPLICATION_IMAGES")
    tags = set()
    for service, image_spec in images.items():
        if not isinstance(image_spec, dict) or set(image_spec) != {"image", "digest"}:
            raise Blocked("IMAGE_RECORD_INVALID")
        image = image_spec["image"]
        if not isinstance(image, str) or not image.startswith("localhost:5001/ai-investment/" + service + ":"):
            raise Blocked("IMAGE_REPOSITORY_INVALID")
        tag = image.split(":")[-1]
        if not TAG_RE.fullmatch(tag) or tag == "dev":
            raise Blocked("IMMUTABLE_IMAGE_TAG_REQUIRED")
        tags.add(tag)
        digest = image_spec["digest"]
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise Blocked("IMAGE_DIGEST_INVALID")
    if len(tags) != 1:
        raise Blocked("REQUIRE_SAME_RELEASE_TAG")
    return images


def checked_output(cmd):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("KUBECTL_UNAVAILABLE_OR_TIMED_OUT") from exc
    if proc.returncode:
        raise Blocked("KUBECTL_QUERY_FAILED")
    return proc.stdout


def kube_json(args, obj):
    prefix = (["docker", "exec", args.node, "/bin/kubectl",
               "--kubeconfig=/etc/rancher/k3s/k3s.yaml"]
              if args.mode == "k3d"
              else ["kubectl", "--context", args.context])
    try:
        return json.loads(checked_output(prefix + ["-n", args.namespace,
                          "get"] + obj + ["-o", "json"]))
    except (UnicodeError, ValueError) as exc:
        raise Blocked("KUBERNETES_JSON_INVALID") from exc


def check_registry(service, image_spec):
    tag = image_spec["image"].rsplit(":", 1)[1]
    url = "http://localhost:5001/v2/ai-investment/" + service + "/manifests/" + tag
    request = urllib.request.Request(url, method="HEAD", headers={"Accept": INDEX_TYPE})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise Blocked("REGISTRY_STATUS_NOT_OK")
            actual = response.headers.get("Docker-Content-Digest")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Blocked("IMAGE_NOT_AVAILABLE_IN_REGISTRY") from exc
    if actual != image_spec["digest"]:
        raise Blocked("IMAGE_DIGEST_MISMATCH")


def helm_revision(args, expected, release):
    secret_list = kube_json(args, ["secrets", "-l", "owner=helm,name=" + release])
    deployed = [s for s in secret_list.get("items", [])
                if s.get("metadata", {}).get("labels", {}).get("status") == "deployed"]
    if len(deployed) != 1:
        raise Blocked("HELM_DEPLOYED_RELEASE_NOT_UNIQUE")
    if deployed[0]["metadata"]["labels"].get("version") != str(expected):
        raise Blocked("HELM_REVISION_CHANGED")
    print("HELM_REVISION_VERIFIED:", expected)


def live_deployments(args, images, release, post):
    result = kube_json(args, ["deployments", "auth-service", "api-gateway"])
    items = result.get("items", [])
    if {d.get("metadata", {}).get("name") for d in items} != SERVICES:
        raise Blocked("LIVE_DEPLOYMENT_SET_CHANGED")
    for deployment in items:
        service = deployment["metadata"]["name"]
        annotations = deployment["metadata"].get("annotations", {})
        if annotations.get("meta.helm.sh/release-name") != release:
            raise Blocked("LIVE_DEPLOYMENT_HELM_OWNER_MISMATCH")
        pod = deployment["spec"]["template"]["spec"]
        matching = [c for c in pod["containers"] if c.get("name") == service]
        if len(matching) != 1:
            raise Blocked("LIVE_CONTAINER_IDENTITY_CHANGED")
        live = matching[0]
        desired_image = images[service]["image"]
        if (post and live.get("image") != desired_image) or (not post and live.get("image") == desired_image):
            raise Blocked("LIVE_IMAGE_OUTSIDE_EXPECTED_STAGE")
        replicas = deployment["spec"].get("replicas", 1)
        if deployment.get("status", {}).get("readyReplicas", 0) != replicas:
            raise Blocked("LIVE_REPLICA_READINESS_FAILED")
        if deployment.get("status", {}).get("updatedReplicas", 0) != replicas:
            raise Blocked("LIVE_ROLLOUT_INCOMPLETE")
        if post:
            env = {e["name"]: e.get("value") for e in live.get("env", [])}
            allowed_key = ("AUTH_ALLOWED_PUBLIC_ORIGINS" if service == "auth-service"
                           else "GATEWAY_CORS_ALLOWED_ORIGINS")
            if not ORIGINS.issubset(set((env.get(allowed_key) or "").split(","))):
                raise Blocked("LIVE_CORS_OR_AUTH_ORIGINS_MISSING")
            if service == "auth-service" and env.get("APP_PUBLIC_URL") != "http://localhost:18080":
                raise Blocked("APP_PUBLIC_URL_CHANGED")
        print("LIVE_DEPLOYMENT_VERIFIED:", service)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "verify"))
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--mode", choices=("k3d", "native"), required=True)
    parser.add_argument("--node", help="explicit k3d server Docker container")
    parser.add_argument("--context", help="explicit native Kubernetes context")
    parser.add_argument("--allow-local-http-registry", action="store_true")
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        images = validate_spec(spec)
        if args.mode == "k3d" and not args.node:
            raise Blocked("K3D_NODE_REQUIRED")
        if args.mode == "native" and not args.context:
            raise Blocked("EXPLICIT_KUBECTL_CONTEXT_REQUIRED")
        if args.mode != "k3d":
            raise Blocked("AZURE_REGISTRY_PROFILE_NOT_YET_IMPLEMENTED")
        if not args.allow_local_http_registry:
            raise Blocked("INSECURE_HTTP_REGISTRY_REQUIRES_LOCAL_OPT_IN")
        args.namespace = spec["namespace"]
        for service, image in sorted(images.items()):
            check_registry(service, image)
            print("REGISTRY_IMAGE_VERIFIED:", service)
        post = args.command == "verify"
        helm_revision(args, spec["revision"] + (1 if post else 0), spec["release"])
        live_deployments(args, images, spec["release"], post)
        if post:
            print("POST_RELEASE_IMAGE_AND_CONFIG_VERIFIED")
            print("UI_ACCEPTANCE_STILL_REQUIRED")
        else:
            print("READ_ONLY_PREFLIGHT_PASSED")
            print("NO_DEPLOYMENT_AUTHORIZATION: Helm three-way diff and protected-resource gate pending")
        return 0
    except (Blocked, KeyError, TypeError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, Blocked) else "MALFORMED_INPUT"
        print("BLOCKED:", message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
