#!/usr/bin/env python3
"""Read-only real Kubernetes three-way strategic patch for the pinned candidate.

Uses the existing isolated Go patch calculator. No patch bytes, environment
contents, Secret contents, or image tags are printed or written to disk.
A clean outcome is NOT permission to upgrade a live cluster.
"""
from copy import deepcopy
import json
import subprocess
import sys
import helm_k3d_acceptance as base
import helm_k3d_impact_gate as impact
import helm_k3d_image_pin_impact as review
import helm_k3d_image_rebaseline as pins
import helm_k3d_strategic_patch as strategic


def verify_candidate(old, original, live, proposed):
    """Prevent unreviewed changes before passing any data to Go."""
    kafka_normalized = review.allow_only_unchanged_live_kafka_probe(
        old, original, live)
    base.only_approved_revision_changes(old, kafka_normalized, live)
    if set(old) != set(proposed) or len(old) != base.EXPECTED_COUNT:
        raise RuntimeError("RESOURCE_SCOPE_CHANGED")

    # Every one of the 16 newly declared image pins must equal the live image.
    for name in pins.FIRST_PARTY:
        key = ("apps/v1", "Deployment", name)
        for docs in (old, proposed, live):
            if key not in docs:
                raise RuntimeError("MISSING_PINNED_DEPLOYMENT")
        desired = proposed[key]["spec"]["template"]["spec"]["containers"]
        actual = live[key]["spec"]["template"]["spec"]["containers"]
        dc = [c for c in desired if c.get("name") == name]
        lc = [c for c in actual if c.get("name") == name]
        if len(dc) != 1 or len(lc) != 1 or dc[0]["image"] != lc[0]["image"]:
            raise RuntimeError("PINNED_IMAGE_NOT_EQUAL_TO_LIVE")

    # Verify the complete scope: undo only the intended 16 image transitions
    # and reviewed Kafka probe in a throwaway in-memory copy. The remaining
    # changes must pass the original narrow research-only guard.
    neutral = deepcopy(proposed)
    for name in pins.FIRST_PARTY:
        key = ("apps/v1", "Deployment", name)
        previous = next(c for c in old[key]["spec"]["template"]["spec"]["containers"]
                        if c.get("name") == name)
        candidate = next(c for c in neutral[key]["spec"]["template"]["spec"]["containers"]
                         if c.get("name") == name)
        candidate["image"] = previous["image"]
    neutral = review.allow_only_unchanged_live_kafka_probe(old, neutral, live)
    base.only_approved_revision_changes(old, neutral, live)


def main():
    if not strategic.CALCULATOR.is_file():
        print("PATCH_CALCULATOR_NOT_FOUND_IN_WORKTREE")
        return 2
    old, original, live = impact.load_manifests()
    candidate = review.candidate_manifest()
    verify_candidate(old, original, live, candidate)
    keys = sorted(old)
    payload = json.dumps({
        "resources": [{"old": old[k], "new": candidate[k], "live": live[k]}
                      for k in keys]
    }, separators=(",", ":")).encode("utf-8")
    p = subprocess.run(
        strategic.calculator_command(strategic.CALCULATOR),
        input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120, check=False,
    )
    if p.returncode not in (0, 3):
        raise RuntimeError("PATCH_CALCULATION_FAILED")
    result = json.loads(p.stdout)
    if result.get("version") != "strategic-three-way-v1" or result.get("resources") != 54:
        raise RuntimeError("UNEXPECTED_PATCH_REPORT")
    names = result.get("risk_names")
    if not isinstance(names, list) or len(names) != len(set(names)):
        raise RuntimeError("INVALID_RISK_NAMES")
    allowed = {key[1] + "/" + key[2] for key in keys}
    if any(not isinstance(n, str) or n not in allowed for n in names):
        raise RuntimeError("UNEXPECTED_RISK_RESOURCE")
    safe_secrets = result.get("verified_secret_noop")
    if not isinstance(safe_secrets, list) or not set(safe_secrets).issubset({
        "Secret/aip-auth-jwt", "Secret/dev-postgres", "Secret/dev-searxng"}):
        raise RuntimeError("INVALID_SECRET_NOOP_REPORT")
    safe_deployments = result.get("verified_deployment_noop")
    if not isinstance(safe_deployments, list) or any(
            n not in allowed or not n.startswith("Deployment/") for n in safe_deployments):
        raise RuntimeError("INVALID_DEPLOYMENT_NOOP_REPORT")
    categories = result.get("patch_categories")
    if not isinstance(categories, dict) or any(n not in allowed for n in categories):
        raise RuntimeError("INVALID_PATCH_CATEGORIES")
    print("PINNED_STRATEGIC_PATCH_RESOURCE_COUNT:", result["resources"])
    print("PINNED_STRATEGIC_PATCH_NONEMPTY:", result.get("nonempty"))
    print("PINNED_STRATEGIC_PATCH_UNRESOLVED:", len(names))
    for name in names:
        print("UNRESOLVED_PATCH_RESOURCE:", name)
        safe_types = {"container-image", "container-env", "pod-template-other",
                      "pod-template-metadata", "spec", "spec-other",
                      "deployment-spec-other", "pod-spec-other", "container-other",
                      "metadata", "other", "secret-data-or-config-data",
                      "secret-stringData", "type", "binary-data"}
        c = categories.get(name, [])
        print("UNRESOLVED_PATCH_CATEGORIES:", name,
              ",".join(x for x in c if isinstance(x, str) and x in safe_types))
    print("PINNED_VERIFIED_DEPLOYMENT_NOOP:", len(safe_deployments))
    print("PINNED_VERIFIED_SECRET_NOOP:", len(safe_secrets))
    print("RESEARCH_PATCH_PRESENT:", bool(result.get("research_patch")))
    print("NO_PATCH_BYTES_OR_CREDENTIALS_DISPLAYED")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("HELM_UPGRADE_NOT_AUTHORIZED")
    return 3 if result.get("blocked") or names or result.get("research_patch") else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("PINNED_STRATEGIC_PATCH_CHECK_BLOCKED")
        sys.exit(2)
