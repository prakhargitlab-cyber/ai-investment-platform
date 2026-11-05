#!/usr/bin/env python3
"""One-shot, read-only field-path report for Helm v1, candidate, and live.

Never prints values, credential bytes, manifest JSON/YAML, image strings,
environment contents, or exception text. Uses existing working server-connected
Helm dry run. No Go build, Helm upgrade, or Kubernetes write operation.
"""
import re
import sys

import helm_k3d_acceptance as base
import helm_k3d_impact_gate as impact


UNRELATED = frozenset({
    "api-gateway", "auth-service", "broker-service", "company-service",
    "frontend", "ibkr-connector", "kafka", "mcp-gateway",
    "notification-service", "portfolio-optimizer", "portfolio-service",
    "ranking-engine", "recommendation-service", "research-service",
    "risk-service", "valuation-engine", "yahoo-finance-mcp",
})
TARGETS = UNRELATED | {"research-engine"}
SAFE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,79}$")
MAX_PATHS = 9


def safe_name(value):
    if isinstance(value, str) and SAFE_NAME.fullmatch(value):
        return value
    return "REDACTED_NAME"


def differences(left, right, path="spec"):
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for name in sorted(set(left) | set(right)):
            if not isinstance(name, str):
                return [path + ".NONSTRING_KEY"]
            child_path = path + "." + safe_name(name)
            if name not in left or name not in right:
                result.append(child_path)
            else:
                result.extend(differences(left[name], right[name], child_path))
        return result
    if isinstance(left, list) and isinstance(right, list):
        if all(isinstance(item, dict) and
               isinstance(item.get("name"), str)
               for item in left + right):
            lmap = {item["name"]: item for item in left}
            rmap = {item["name"]: item for item in right}
            if len(lmap) != len(left) or len(rmap) != len(right):
                return [path + ".DUPLICATE_LIST_NAME"]
            result = []
            for name in sorted(set(lmap) | set(rmap)):
                child_path = path + "[" + safe_name(name) + "]"
                if name not in lmap or name not in rmap:
                    result.append(child_path)
                else:
                    result.extend(differences(lmap[name], rmap[name], child_path))
            return result
        return [] if left == right else [path + ".LIST_DIFFERENCE"]
    return [] if left == right else [path]


def main():
    old, candidate, live = impact.load_manifests()
    base.only_approved_revision_changes(old, candidate, live)
    if len(old) != 54 or set(old) != set(candidate) or set(old) - set(live):
        raise RuntimeError("RECONCILE_RESOURCE_IDENTITY_MISMATCH")
    print("RECONCILE_RESOURCE_COUNT: 54")
    print("ONLY_FIELD_PATHS_SHOWN_NO_VALUES")
    for name in sorted(TARGETS):
        key = ("apps/v1", "Deployment", name)
        if key not in old:
            raise RuntimeError("RECONCILE_DEPLOYMENT_MISSING")
        o, n, l = (doc[key].get("spec") for doc in (old, candidate, live))
        if not all(isinstance(x, dict) for x in (o, n, l)):
            raise RuntimeError("RECONCILE_SPEC_INVALID")
        authored = differences(o, n)
        drift = differences(n, l)
        original_drift = differences(o, l)
        print("DEPLOYMENT:", name)
        print("  V1_TO_CANDIDATE_PATH_COUNT:", len(authored))
        print("  CANDIDATE_TO_LIVE_PATH_COUNT:", len(drift))
        print("  V1_TO_LIVE_PATH_COUNT:", len(original_drift))
        # The listed paths show which authored fields require actual
        # reconciliation. Values are never printed, even for images.
        for label, paths in (("V1_TO_CANDIDATE", authored),
                             ("CANDIDATE_TO_LIVE", drift)):
            for item in paths[:MAX_PATHS]:
                print("  " + label + "_PATH:", item)
            if len(paths) > MAX_PATHS:
                print("  " + label + "_ADDITIONAL_PATHS:", len(paths) - MAX_PATHS)
    print("RECONCILIATION_REPORT_ONLY")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("HELM_UPGRADE_REMAINS_BLOCKED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # No raw error messages; they may contain sensitive subprocess output.
        print("RECONCILIATION_FIELD_REPORT_BLOCKED")
        sys.exit(2)
