#!/usr/bin/env python3
"""Read-only actual Helm candidate / Kubernetes strategic three-way patch audit.

Requires PyYAML, Docker, a locally compiled trusted Linux Go patch calculator,
and the pinned k3d server. No Kubernetes writes. Never prints manifest bytes.
The output is diagnostic: it does not authorize helm upgrade.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

RELEASE = "ai-investment-platform"
NAMESPACE = "ai-investment"
SERVER = "k3d-ai-investment-dev-server-0"
HELM_IMAGE = "alpine/helm:4.2.3"
K3S_CONFIG = "/etc/rancher/k3s/k3s.yaml"
KINDS = ("deployments,statefulsets,services,serviceaccounts,"
         "persistentvolumeclaims,secrets,configmaps,roles,rolebindings,ingresses")
ALLOWED = {"Deployment/api-gateway", "Deployment/auth-service"}
PHASE = "START"


def phase(label):
    global PHASE
    PHASE = label
    print("AUDIT_STAGE:", label, flush=True)


class Blocked(Exception):
    pass


def call(args, *, data=None, timeout=150):
    try:
        p = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("COMMAND_FAILED_OR_TIMED_OUT") from exc
    if p.returncode:
        raise Blocked("READ_ONLY_COMMAND_RETURNED_FAILURE")
    return p.stdout


def resource_id(item):
    return (item["apiVersion"], item["kind"], item["metadata"]["name"])


def parse_docs(raw):
    docs = [d for d in yaml.safe_load_all(raw) if isinstance(d, dict)]
    result = {resource_id(d): d for d in docs}
    if len(result) != len(docs):
        raise Blocked("DUPLICATE_RESOURCE_ID")
    return result


def kube(*arguments):
    return call(["docker", "exec", SERVER, "/bin/kubectl",
                 "--kubeconfig=" + K3S_CONFIG, "-n", NAMESPACE, *arguments])


def helm(base, *arguments):
    return call(base + list(arguments))


def calculator(executable, resources):
    # The Go implementation is trusted local code, isolated from the network,
    # filesystems and host credentials. Sensitive docs only enter via stdin.
    payload = json.dumps({"resources": resources}, separators=(",", ":")).encode()
    command = ["docker", "run", "--rm", "-i", "--network", "none",
               "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--user", "65534:65534",
               "--mount", "type=bind,source=" + str(executable.resolve())
               + ",target=/usr/local/bin/helm-patch-impact,readonly",
               "--entrypoint", "/usr/local/bin/helm-patch-impact", "golang:1.23-alpine"]
    try:
        p = subprocess.run(command, input=payload, capture_output=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("STRATEGIC_PATCH_CALCULATOR_UNAVAILABLE") from exc
    if p.returncode not in (0, 3):
        raise Blocked("STRATEGIC_PATCH_CALCULATION_FAILED")
    try:
        result = json.loads(p.stdout)
    except ValueError as exc:
        raise Blocked("STRATEGIC_PATCH_REPORT_NOT_JSON") from exc
    if result.get("version") != "strategic-three-way-v1":
        raise Blocked("STRATEGIC_PATCH_REPORT_VERSION_UNEXPECTED")
    if result.get("blocked") is not (p.returncode == 3):
        raise Blocked("STRATEGIC_PATCH_EXIT_MISMATCH")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", type=Path, required=True)
    parser.add_argument("--calculator", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    arguments = parser.parse_args()
    phase("READ_RELEASE_SPEC")
    spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
    if (spec.get("revision") != 1 or spec.get("release") != RELEASE
            or spec.get("namespace") != NAMESPACE
            or set(spec.get("images", {})) != {"api-gateway", "auth-service"}):
        raise Blocked("RELEASE_SPEC_SCOPE_INVALID")
    if not arguments.chart.is_dir() or not arguments.calculator.is_file():
        raise Blocked("HELM_CHART_OR_CALCULATOR_MISSING")
    phase("READ_K3D_KUBECONFIG")
    # Source chart is mounted read-only. No base64 contents printed or written.
    config = call(["docker", "exec", SERVER, "/bin/cat", K3S_CONFIG])
    if b"server: https://127.0.0.1:6443" not in config:
        raise Blocked("K3D_KUBECONFIG_ENDPOINT_UNEXPECTED")
    with tempfile.TemporaryDirectory(prefix="aip-impact-") as directory:
        kubeconfig = Path(directory) / "config"
        kubeconfig.write_bytes(config)
        base = ["docker", "run", "--rm", "--network", "container:" + SERVER,
                "--mount", "type=bind,source=" + str(kubeconfig)
                + ",target=/tmp/kubeconfig,readonly",
                "--mount", "type=bind,source=" + str(arguments.chart.resolve())
                + ",target=/chart,readonly",
                "--entrypoint", "helm", HELM_IMAGE, "--kubeconfig", "/tmp/kubeconfig"]
        phase("READ_HELM_REVISION_ONE_MANIFEST")
        old = parse_docs(helm(base, "get", "manifest", RELEASE, "--namespace",
                              NAMESPACE, "--revision", "1"))
        tag = spec["images"]["auth-service"]["image"].rsplit(":", 1)[-1]
        if tag != spec["images"]["api-gateway"]["image"].rsplit(":", 1)[-1]:
            raise Blocked("SERVICE_TAG_MISMATCH")
        origins = json.dumps(spec["origins"])
        phase("HELM_SERVER_DRY_RUN")
        preview = helm(base, "upgrade", RELEASE, "/chart", "--namespace",
                       NAMESPACE, "--reuse-values", "--dry-run=server",
                       "--set-string", "workloadOverrides.auth-service.imageTagOverride=" + tag,
                       "--set-string", "workloadOverrides.api-gateway.imageTagOverride=" + tag,
                       "--set-json", "apiGateway.cors.allowedOrigins=" + origins)
    phase("PARSE_HELM_DRY_RUN")
    marker = b"\nMANIFEST:\n"
    if marker not in preview:
        raise Blocked("HELM_DRY_RUN_MANIFEST_NOT_FOUND")
    proposed = parse_docs(preview.split(marker, 1)[1].split(b"\nNOTES:\n", 1)[0])
    phase("READ_LIVE_RESOURCES")
    live_items = json.loads(kube("get", KINDS, "-o", "json"))["items"]
    live = {resource_id(d): d for d in live_items}
    if len(old) != 54 or set(old) != set(proposed) or set(old) - set(live):
        raise Blocked("HELM_RESOURCE_IDENTITY_MISMATCH")
    resources = [{"old": old[k], "new": proposed[k], "live": live[k]}
                 for k in sorted(old)]
    phase("STRATEGIC_PATCH_CALCULATION")
    result = calculator(arguments.calculator, resources)
    phase("VALIDATE_PATCH_REPORT")
    if result.get("resources") != 54:
        raise Blocked("PATCH_RESOURCE_COUNT_INVALID")
    risk = result.get("risk_names")
    categories = result.get("patch_categories")
    if not isinstance(risk, list) or not isinstance(categories, dict):
        raise Blocked("PATCH_REPORT_SCHEMA_INVALID")
    known = {k[1] + "/" + k[2] for k in old}
    if any(name not in known for name in risk):
        raise Blocked("UNEXPECTED_PATCH_RESOURCE_NAME")
    # Do not whitelist any patch based on a broad category label alone.
    # Report nonempty patch resources including verified noop candidates.
    changed = sorted(categories)
    if not set(changed).issubset(known):
        raise Blocked("PATCH_CATEGORY_RESOURCE_NAME_INVALID")
    print("MANAGED_HELM_RESOURCES:", len(old))
    print("ACTUAL_STRATEGIC_THREE_WAY_NONEMPTY_PATCHES:", result.get("nonempty"))
    print("UNRESOLVED_PATCH_RESOURCES:", len(risk))
    for name in changed:
        print("PATCH_RESOURCE:", name)
    unexpected = sorted(set(changed) - ALLOWED)
    print("OUTSIDE_TWO_SERVICE_SCOPE:", len(unexpected))
    for name in unexpected:
        print("UNRELATED_RESOURCE_PATCH:", name)
    print("NO_PATCH_BYTES_OR_SECRET_VALUES_PRINTED")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("NO_DEPLOYMENT_AUTHORIZATION")
    # Even a two-service-only patch requires exact container field-level
    # verification and operator approval; this diagnostic never greenlights.
    return 3 if changed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never print exception strings: Helm or YAML errors may contain Secrets.
        print("HELM_IMPACT_AUDIT_BLOCKED_AT:", PHASE, file=sys.stderr)
        print("NO_KUBERNETES_WRITES_PERFORMED", file=sys.stderr)
        sys.exit(2)
