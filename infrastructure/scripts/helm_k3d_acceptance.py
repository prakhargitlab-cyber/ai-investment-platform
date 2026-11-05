#!/usr/bin/env python3
"""Fail-closed, read-only k3d Helm upgrade acceptance. Never prints Secret values.

Run with repo venv Python and PyYAML. No cluster mutations. A passing result
does not authorize upgrade and does not check per-node image cache/rollback.
"""
import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

SERVER = "k3d-ai-investment-dev-server-0"
NAMESPACE = "ai-investment"
RELEASE = "ai-investment-platform"
IMAGE = "alpine/helm:4.2.3"
TAG = "etf-eq-rl-20261104-210859"
EXPECTED_IMAGE = "localhost:5001/ai-investment/research-engine:" + TAG
ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infrastructure" / "helm" / "ai-investment-platform"
K3S_CONFIG = "/etc/rancher/k3s/k3s.yaml"
RESOURCE_KINDS = (
    "deployments,statefulsets,services,serviceaccounts,"
    "persistentvolumeclaims,secrets,configmaps,roles,rolebindings,ingresses"
)
EXPECTED_COUNT = 54
SECRET_KEYS = {"dev-postgres": "POSTGRES_PASSWORD", "dev-searxng": "SEARXNG_SECRET"}


class Blocked(Exception):
    pass


def call(args):
    # Capture all potentially sensitive data; never echo stdout/stderr on errors.
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           check=False, timeout=150)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked("COMMAND_UNAVAILABLE_OR_TIMED_OUT") from exc
    if p.returncode:
        raise Blocked("COMMAND_FAILED: " + args[1])
    return p.stdout


def kube(*args):
    return call(["docker", "exec", SERVER, "/bin/kubectl",
                 "--kubeconfig=" + K3S_CONFIG, "-n", NAMESPACE, *args])


def rid(obj):
    return (obj["apiVersion"], obj["kind"], obj["metadata"]["name"])


def selected(obj):
    # Compare author-supplied desired fields to live resources. Server-added
    # defaults and Helm/Kubernetes-generated metadata are excluded.
    if obj["kind"] == "Secret":
        data = dict(obj.get("data") or {})
        for k, v in (obj.get("stringData") or {}).items():
            data[k] = base64.b64encode(v.encode("utf-8")).decode("ascii")
        return {"type": obj.get("type", "Opaque"), "data": data}
    fields = ("spec", "data", "binaryData", "rules", "subjects", "roleRef",
              "automountServiceAccountToken", "imagePullSecrets")
    return {k: obj[k] for k in fields if k in obj}


def compare(desired, actual, path, differences):
    # Desired keys must match, but Kubernetes may add default fields.
    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            differences.append(path)
            return
        for k, v in desired.items():
            if k not in actual:
                differences.append(path + "." + str(k))
            else:
                compare(v, actual[k], path + "." + str(k), differences)
    elif isinstance(desired, list):
        if not isinstance(actual, list) or len(desired) != len(actual):
            differences.append(path)
        else:
            for i, (a, b) in enumerate(zip(desired, actual)):
                compare(a, b, path + "[" + str(i) + "]", differences)
    elif desired != actual:
        differences.append(path)



APPROVED_FLAGS = {
    "AIP_RESEARCH_EQUITY_MANUAL_EXECUTION_ENABLED": "true",
    "AIP_RESEARCH_ETF_MANUAL_EXECUTION_ENABLED": "true",
    "AIP_RESEARCH_ETF_FULL_UNIVERSE_EXECUTION_ENABLED": "true",
}
PROTECTED_FLAGS = {
    "AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED": "true",
    "AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED": "false",
}


def only_approved_revision_changes(baseline, proposed, actual):
    """Require identical rendered revision except exact approved research fields.

    Uses names, not array indexes, to avoid approving reordered fields.
    This is NOT an emulation of Helm's strategic three-way patch.
    """
    if set(baseline) != set(proposed):
        raise Blocked("RESOURCE_IDENTITY_CHANGE")
    research_key = ("apps/v1", "Deployment", "research-engine")
    if research_key not in baseline or research_key not in actual:
        raise Blocked("RESEARCH_DEPLOYMENT_MISSING")
    old = deepcopy(baseline)
    new = deepcopy(proposed)
    live = actual[research_key]
    o = old[research_key]["spec"]["template"]["spec"]
    n = new[research_key]["spec"]["template"]["spec"]
    l = live["spec"]["template"]["spec"]

    def by_name(array, what):
        result = {x["name"]: x for x in array}
        if len(result) != len(array):
            raise Blocked("DUPLICATE_" + what)
        return result

    for kind in ("containers", "initContainers"):
        old_containers = by_name(o.get(kind, []), "CONTAINER")
        new_containers = by_name(n.get(kind, []), "CONTAINER")
        live_containers = by_name(l.get(kind, []), "CONTAINER")
        if set(old_containers) != set(new_containers):
            raise Blocked("CONTAINER_IDENTITY_CHANGE")
        for name, old_container in old_containers.items():
            if kind == "containers" and name != "research-engine":
                # Chart may name the app container unexpectedly; other containers
                # must not be silently authorized to change image.
                continue
            expected_new = new_containers[name]["image"]
            if expected_new != EXPECTED_IMAGE:
                raise Blocked("RESEARCH_IMAGE_NOT_APPROVED")
            if live_containers.get(name, {}).get("image") != expected_new:
                raise Blocked("LIVE_RESEARCH_IMAGE_MISMATCH")
            new_containers[name]["image"] = old_container["image"]

    old_apps = by_name(o.get("containers", []), "APP_CONTAINER")
    new_apps = by_name(n.get("containers", []), "APP_CONTAINER")
    live_apps = by_name(l.get("containers", []), "APP_CONTAINER")
    if len(old_apps) != 1 or set(old_apps) != set(new_apps) or set(old_apps) != set(live_apps):
        raise Blocked("APP_CONTAINER_IDENTITY_CHANGE")
    name = next(iter(old_apps))
    old_env = by_name(old_apps[name].get("env", []), "ENV")
    new_env = by_name(new_apps[name].get("env", []), "ENV")
    live_env = by_name(live_apps[name].get("env", []), "ENV")
    if set(old_env) != set(new_env):
        raise Blocked("ENV_IDENTITY_CHANGE")
    for flag, value in APPROVED_FLAGS.items():
        if flag not in old_env or flag not in new_env or flag not in live_env:
            raise Blocked("MISSING_PROTECTED_FLAG")
        if new_env[flag].get("value") != value or live_env[flag].get("value") != value:
            raise Blocked("PROTECTED_FLAG_VALUE_MISMATCH")
        new_env[flag]["value"] = old_env[flag].get("value")
    for flag, value in PROTECTED_FLAGS.items():
        if new_env.get(flag, {}).get("value") != value or live_env.get(flag, {}).get("value") != value:
            raise Blocked("SAFETY_FLAG_VALUE_MISMATCH")
    # The chart generates the same full manifests for all other resources.
    # Mask ONLY the approved fields, then require exact structural identity.
    for key in old:
        if old[key] != new[key]:
            raise Blocked("UNAPPROVED_STORED_RELEASE_CHANGE: " + key[1] + "/" + key[2])
    return True

def main():
    if not CHART.is_dir():
        raise Blocked("CHART_DIRECTORY_MISSING")
    health = call(["docker", "exec", SERVER, "/bin/kubectl",
                   "--kubeconfig=" + K3S_CONFIG, "get", "--raw=/readyz"])
    if health.strip() != b"ok":
        raise Blocked("KUBERNETES_API_NOT_READY")

    history = json.loads(kube("get", "secret",
                             "-l", "owner=helm,name=" + RELEASE,
                             "-o", "json"))
    revisions = [x["metadata"].get("labels", {}) for x in history["items"]]
    deployed = [x for x in revisions if x.get("status") == "deployed"]
    if len(deployed) != 1 or deployed[0].get("version") != "1":
        raise Blocked("UNEXPECTED_HELM_RELEASE_REVISION")

    # Resolve the kubeconfig symlink inside Linux, not via Windows docker cp.
    config = call(["docker", "exec", SERVER, "/bin/cat", K3S_CONFIG])
    if b"server: https://127.0.0.1:6443" not in config:
        raise Blocked("UNEXPECTED_INTERNAL_API_ENDPOINT")
    with tempfile.TemporaryDirectory(prefix="aip-helm-acceptance-") as directory:
        cfg = Path(directory) / "kubeconfig"
        cfg.write_bytes(config)
        # Secrets in the dry-run manifest remain in subprocess memory only.
        # DO NOT use --hide-secret: equality cannot be checked if redacted.
        cmd = [
            "docker", "run", "--rm", "--network", "container:" + SERVER,
            "--mount", "type=bind,source=" + str(cfg) + ",target=/tmp/kubeconfig,readonly",
            "--mount", "type=bind,source=" + str(CHART) + ",target=/chart,readonly",
            "--entrypoint", "helm", IMAGE, "--kubeconfig", "/tmp/kubeconfig",
            "upgrade", RELEASE, "/chart", "--namespace", NAMESPACE,
            # Avoid adding values-dev.yaml here: it changes portfolio-service
            # in addition to the intended research-engine corrections.
            # Base upgrades on Helm revision-1 stored values, not local profile
            # defaults, until a separately reviewed values migration exists.
            "--reuse-values", "--dry-run=server",
            "--set", "research.opportunityRecoveryPaused=true",
            "--set", "research.opportunityScheduler.enabled=false",
            "--set", "research.equityManualExecutionEnabled=true",
            "--set", "research.etfManualExecutionEnabled=true",
            "--set", "research.etfFullUniverseExecutionEnabled=true",
            "--set-string", "aiServiceImageOverrides.research-engine.tag=" + TAG
        ]
        raw = call(cmd).decode("utf-8")
        marker = "\nMANIFEST:\n"
        if marker not in raw:
            raise Blocked("HELM_MANIFEST_SECTION_MISSING")
        manifest = raw.split(marker, 1)[1].split("\nNOTES:\n", 1)[0]
        desired_docs = [d for d in yaml.safe_load_all(manifest) if isinstance(d, dict)]
        baseline_cmd = [
            "docker", "run", "--rm", "--network", "container:" + SERVER,
            "--mount", "type=bind,source=" + str(cfg) + ",target=/tmp/kubeconfig,readonly",
            "--entrypoint", "helm", IMAGE, "--kubeconfig", "/tmp/kubeconfig",
            "get", "manifest", RELEASE, "--namespace", NAMESPACE, "--revision", "1"
        ]
        baseline_docs = [d for d in yaml.safe_load_all(call(baseline_cmd))
                         if isinstance(d, dict)]
    if len(desired_docs) != EXPECTED_COUNT:
        raise Blocked("UNEXPECTED_RENDERED_RESOURCE_COUNT: " + str(len(desired_docs)))
    expected = {}
    for obj in desired_docs:
        key = rid(obj)
        if key in expected:
            raise Blocked("DUPLICATE_RENDERED_RESOURCE")
        expected[key] = obj

    baseline = {rid(o): o for o in baseline_docs}
    if len(baseline) != EXPECTED_COUNT or set(baseline) != set(expected):
        raise Blocked("STORED_RELEASE_IDENTITIES_MISMATCH")
    live_list = json.loads(kube("get", RESOURCE_KINDS, "-o", "json"))
    live = {rid(o): o for o in live_list["items"]}
    scope_ok = only_approved_revision_changes(baseline, expected, live)
    missing = sorted(set(expected) - set(live))
    if missing:
        print("MISSING_LIVE_IDENTITIES_COUNT:", len(missing))
        raise Blocked("RENDERED_RESOURCE_MISSING_IN_LIVE")

    # Compare three independent states to identify the source of drift.
    # Stored v1 is the historical Helm baseline, not a substitute for live.
    baseline_to_live = []
    proposed_to_baseline = []
    differences = []
    for key, desired in sorted(expected.items()):
        current = live[key]
        # Keep credentials in memory and report only labels/paths, never values.
        label = key[1] + "/" + key[2]
        compare(selected(desired), selected(current), label, differences)
        compare(selected(baseline[key]), selected(current), label, baseline_to_live)
        compare(selected(desired), selected(baseline[key]), label, proposed_to_baseline)
    for secret_name, data_key in SECRET_KEYS.items():
        obj = next((o for o in desired_docs if o["kind"] == "Secret"
                    and o["metadata"]["name"] == secret_name), None)
        if obj is None or data_key not in selected(obj)["data"]:
            raise Blocked("EXPECTED_DEV_CREDENTIAL_NOT_RENDERED")
        live_obj = live.get(rid(obj))
        if live_obj is None or selected(obj)["data"][data_key] != (
                live_obj.get("data") or {}).get(data_key):
            raise Blocked("DEV_CREDENTIAL_CHANGED: " + secret_name)

    research = expected.get(("apps/v1", "Deployment", "research-engine"))
    if research is None:
        raise Blocked("RESEARCH_DEPLOYMENT_MISSING")
    pod = research["spec"]["template"]["spec"]
    images = [x["image"] for x in pod.get("containers", []) + pod.get("initContainers", [])]
    if len(images) != 2 or any(v != EXPECTED_IMAGE for v in images):
        raise Blocked("RESEARCH_IMAGES_NOT_APPROVED")

    print("RENDERED_RESOURCE_COUNT:", len(expected))
    print("RESOURCE_IDENTITIES_PRESENT:", len(expected))
    print("POSTGRES_CREDENTIAL_PRESERVED: yes")
    print("SEARXNG_CREDENTIAL_PRESERVED: yes")
    print("RESEARCH_IMAGES_APPROVED: yes")
    print("STORED_V1_TO_LIVE_FIELD_DIFFERENCES:", len(baseline_to_live))
    print("PROPOSED_V2_TO_STORED_V1_FIELD_DIFFERENCES:", len(proposed_to_baseline))
    print("DIFFERENT_DESIRED_FIELDS:", len(differences))
    print("PROPOSED_V2_VS_STORED_V1_FIELD_PATHS:")
    for path in proposed_to_baseline[:25]:
        print("CHANGE_FROM_V1_PATH:", path)
    if len(proposed_to_baseline) > 25:
        print("ADDITIONAL_CHANGE_FROM_V1_PATHS:", len(proposed_to_baseline) - 25)
    print("PROPOSED_V2_VS_STORED_V1_RESOURCES:",
          ", ".join(sorted(set(p.split(".")[0] for p in proposed_to_baseline))) or "(none)")
    print("STORED_V1_VS_LIVE_RESOURCES:",
          ", ".join(sorted(set(p.split(".")[0] for p in baseline_to_live))) or "(none)")
    for path in differences[:25]:
        print("DIFF_PATH:", path)  # paths only; no values
    if len(differences) > 25:
        print("ADDITIONAL_DIFF_PATHS:", len(differences) - 25)
    print("HISTORICAL_DRIFT_FIELDS:", len(differences))
    if not scope_ok:
        raise Blocked("RELEASE_SCOPE_GUARD_FAILED")
    print("RELEASE_SCOPE_GUARD_PASSED")
    print("HELM_THREE_WAY_PATCH_NOT_VERIFIED")
    print("NO_UPGRADE_PERFORMED; IMAGE_CACHE_AND_ROLLBACK_STILL_UNVERIFIED")


if __name__ == "__main__":
    try:
        main()
    except Blocked as exc:
        print("SANITIZED_HELM_ACCEPTANCE_BLOCKED:", exc)
        sys.exit(2)
    except Exception:
        # Untrusted exception strings could embed Secret material; do not echo.
        print("SANITIZED_HELM_ACCEPTANCE_BLOCKED: UNEXPECTED_ERROR")
        sys.exit(2)
