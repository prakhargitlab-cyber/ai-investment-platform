#!/usr/bin/env python3
"""Build a private Helm image-tag candidate from live k3d state.

The output contains only 16 validated first-party workload image tags.\nKafka is an externally sourced image and requires a separate chart-managed\nstartup/replica review, not a first-party image override. No Kubernetes
writes, helm upgrade, Secret values, env values, Helm manifest bodies, or
image repositories are persisted. Do not use this file as automatic approval.
"""
from pathlib import Path
import os
import re
import sys
import tempfile

import yaml

import helm_k3d_acceptance as base
import helm_k3d_impact_gate as impact


FIRST_PARTY = frozenset({
    "api-gateway", "auth-service", "broker-service", "company-service",
    "frontend", "ibkr-connector", "mcp-gateway",
    "notification-service", "portfolio-optimizer", "portfolio-service",
    "ranking-engine", "recommendation-service", "research-service",
    "risk-service", "valuation-engine", "yahoo-finance-mcp",
})
TAG = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$")
LOCAL_REPOS = ("localhost:5001/", "ai-investment-registry:5000/")
OUTPUT = base.ROOT / ".tmp" / "helm-reconciliation-image-pins.yaml"


def image_tag(image, name, *, allow_mutable=False):
    """Accept only the expected locally built image repo and an immutable tag."""
    if not isinstance(image, str):
        raise ValueError("INVALID_IMAGE_REFERENCE")
    candidates = tuple(prefix + "ai-investment/" + name + ":"
                       for prefix in LOCAL_REPOS)
    for prefix in candidates:
        if image.startswith(prefix):
            tag = image[len(prefix):]
            if TAG.fullmatch(tag) and (allow_mutable or tag not in ("latest", "dev")):
                return tag
    raise ValueError("UNREVIEWED_LIVE_IMAGE_REFERENCE")


def candidate_values(old, proposed, live):
    if (len(old) != base.EXPECTED_COUNT or set(old) != set(proposed)
            or set(old) - set(live)):
        raise ValueError("RESOURCE_IDENTITIES_CHANGED")
    result = {}
    for name in sorted(FIRST_PARTY):
        key = ("apps/v1", "Deployment", name)
        if key not in old:
            raise ValueError("EXPECTED_DEPLOYMENT_MISSING")
        target = proposed[key]["spec"]["template"]["spec"]
        actual = live[key]["spec"]["template"]["spec"]
        authored_containers = target.get("containers", ())
        running_containers = actual.get("containers", ())
        if not isinstance(authored_containers, list) or not isinstance(running_containers, list):
            raise ValueError("INVALID_CONTAINERS")
        candidate = [x for x in authored_containers if x.get("name") == name]
        observed = [x for x in running_containers if x.get("name") == name]
        if len(candidate) != 1 or len(observed) != 1:
            raise ValueError("CONTAINER_IDENTITY_MISMATCH")
        # Expected template currently renders the same LOCAL repo. If a
        # different registry is required, that is a separate reviewed change.
        _ = image_tag(candidate[0].get("image"), name, allow_mutable=True)
        result[name] = {"imageTagOverride": image_tag(observed[0].get("image"), name)}
    if set(result) != FIRST_PARTY:
        raise ValueError("MISSING_IMAGE_PINS")
    return {"workloadOverrides": result}


def main():
    if OUTPUT.exists():
        raise ValueError("RECONCILIATION_CANDIDATE_ALREADY_EXISTS")
    old, proposed, live = impact.load_manifests()
    base.only_approved_revision_changes(old, proposed, live)
    candidate = candidate_values(old, proposed, live)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Local private file, never Git, and no temporary dump of Secrets.
    fd, temporary = tempfile.mkstemp(prefix="helm-image-pins-", suffix=".yaml",
                                    dir=OUTPUT.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(candidate, handle, sort_keys=True)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("LOCAL_HELM_IMAGE_PINS_PREPARED:", len(FIRST_PARTY))
    print("CANDIDATE_PATH: .tmp/helm-reconciliation-image-pins.yaml")
    print("SECRETS_AND_ENV_VALUES_NOT_EXPORTED")
    print("IMAGE_REPOSITORIES_AND_TAGS_NOT_PRINTED")
    print("NOT_A_COMPLETE_HELM_RECONCILIATION")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("HELM_UPGRADE_NOT_AUTHORIZED")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("HELM_IMAGE_RECONCILIATION_PREPARATION_BLOCKED")
        sys.exit(2)
