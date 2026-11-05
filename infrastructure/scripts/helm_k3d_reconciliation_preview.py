#!/usr/bin/env python3
"""Read-only reconciliation feasibility preview; NEVER an upgrade manifest.

No cluster writes. Never print deployment specifications, env values,
credentials, Go JSON reports or patches. The comparison is against Kubernetes'
actual three-way merge algorithm via the existing networkless Linux calculator.

This preview intentionally does NOT copy or propose any Secret credential and
does NOT mark any field approved for deployment. It tests only whether matching
17 unrelated Deployments' live *spec* in a hypothetical target avoids patches.
"""
from copy import deepcopy
import json
import subprocess
import sys

import helm_k3d_acceptance as base
import helm_k3d_impact_gate as impact
import helm_k3d_strategic_patch as gate


PRESERVE_DEPLOYMENTS = frozenset({
    "api-gateway", "auth-service", "broker-service", "company-service",
    "frontend", "ibkr-connector", "kafka", "mcp-gateway",
    "notification-service", "portfolio-optimizer", "portfolio-service",
    "ranking-engine", "recommendation-service", "research-service",
    "risk-service", "valuation-engine", "yahoo-finance-mcp"
})


def preview_request(old, new, live):
    if set(old) != set(new) or set(old) - set(live) or len(old) != 54:
        raise RuntimeError("PREVIEW_IDENTITIES_BLOCKED")
    keys = sorted(old)
    draft = deepcopy(new)
    for key in keys:
        if key[1] != "Deployment" or key[2] not in PRESERVE_DEPLOYMENTS:
            continue
        current = live[key]
        if (current.get("apiVersion") != "apps/v1" or
                current.get("kind") != "Deployment" or
                current.get("metadata", {}).get("name") != key[2] or
                not isinstance(current.get("spec"), dict)):
            raise RuntimeError("PREVIEW_LIVE_SPEC_INVALID")
        # Hypothetical comparison only: never save, feed into Helm, or allow
        # release from this document. Live spec includes Kubernetes defaults.
        draft[key]["spec"] = deepcopy(current["spec"])
    found = {k[2] for k in keys if k[1] == "Deployment" and
             k[2] in PRESERVE_DEPLOYMENTS}
    if found != PRESERVE_DEPLOYMENTS:
        raise RuntimeError("PREVIEW_EXPECTED_DEPLOYMENTS_MISSING")
    # Secrets and research-engine stay unchanged and retain all protections.
    return json.dumps({"resources": [
        {"old": old[k], "new": draft[k], "live": live[k]} for k in keys
    ]}, separators=(",", ":")).encode("utf-8")


def run_calculator(payload):
    proc = subprocess.run(
        gate.calculator_command(gate.CALCULATOR), input=payload,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, timeout=120,
    )
    if proc.returncode not in (0, 3):
        raise RuntimeError("PREVIEW_CALCULATOR_FAILED")
    # Do not forward raw Go output, even on an error.
    result = json.loads(proc.stdout)
    if (result.get("version") != "strategic-three-way-v1" or
            result.get("resources") != 54 or
            result.get("blocked") is not (proc.returncode == 3)):
        raise RuntimeError("PREVIEW_REPORT_INVALID")
    risks = result.get("risk_names")
    if not isinstance(risks, list) or any(not isinstance(n, str) for n in risks):
        raise RuntimeError("PREVIEW_RISKS_INVALID")
    return risks, result


def main():
    if not gate.CALCULATOR.is_file():
        raise RuntimeError("LINUX_CALCULATOR_NOT_BUILT")
    old, new, live = impact.load_manifests()
    base.only_approved_revision_changes(old, new, live)
    risks, report = run_calculator(preview_request(old, new, live))
    remaining = sorted(set(risks) & {
        "Deployment/" + name for name in PRESERVE_DEPLOYMENTS
    })
    print("RECONCILIATION_PREVIEW_RESOURCES:", report["resources"])
    print("HYPOTHETICAL_PRESERVED_DEPLOYMENTS:", len(PRESERVE_DEPLOYMENTS))
    print("UNRELATED_DEPLOYMENT_PATCHES_STILL_PRESENT:", len(remaining))
    print("JWT_SECRET_UNCHANGED_AND_BLOCKED")
    print("RESEARCH_ENGINE_UNCHANGED_AND_BLOCKED")
    print("PREVIEW_NOT_AN_APPLICABLE_HELM_MANIFEST")
    print("NO_CREDENTIALS_OR_PATCH_VALUES_PRINTED")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("HELM_REVISION_2_NOT_AUTHORIZED")
    return 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # No exception text ever displayed; may contain private manifest data.
        print("RECONCILIATION_PREVIEW_BLOCKED")
        sys.exit(2)
