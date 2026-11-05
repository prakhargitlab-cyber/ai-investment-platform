#!/usr/bin/env python3
"""Read-only wrapper for locally compiled Kubernetes strategic patch checker.

Credential-containing objects are passed only via subprocess stdin (in memory).
The Go tool prints resource names/counts only, never patch values.
"""
import json
from pathlib import Path
import subprocess
import sys

import helm_k3d_impact_gate as impact
import helm_k3d_acceptance as base

ROOT = Path(__file__).resolve().parents[2]
CALCULATOR = ROOT / ".tmp" / "helm-patch-impact" / "helm-patch-impact.exe"


def main():
    if not CALCULATOR.is_file():
        raise RuntimeError("COMPILED_GO_PATCH_CALCULATOR_MISSING")
    old, new, live = impact.load_manifests()
    base.only_approved_revision_changes(old, new, live)

    # Gather exactly the existing 54 resources; never write manifests to disk.
    keys = sorted(old)
    payload = json.dumps({"resources": [
        {"old": old[key], "new": new[key], "live": live[key]}
        for key in keys
    ]}, separators=(",", ":")).encode("utf-8")

    proc = subprocess.run(
        [str(CALCULATOR)], input=payload,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        check=False
    )
    # The executable is expected to emit strictly a fixed-schema summary.
    # Do not echo raw stderr/stdout: values could be included by a bad binary.
    if proc.returncode not in (0, 3):
        raise RuntimeError("KUBERNETES_PATCH_CALCULATION_FAILED")
    result = json.loads(proc.stdout)
    if result.get("version") != "strategic-three-way-v1":
        raise RuntimeError("UNEXPECTED_PATCH_REPORT_VERSION")
    if result.get("resources") != 54:
        raise RuntimeError("PATCH_RESOURCE_COUNT_MISMATCH")
    names = result.get("risk_names")
    if not isinstance(names, list) or any(
        not isinstance(name, str) or "/" not in name or len(name) > 160
        for name in names
    ):
        raise RuntimeError("INVALID_PATCH_RISK_NAMES")
    if any(name not in {k[1] + "/" + k[2] for k in keys} for name in names):
        raise RuntimeError("UNKNOWN_PATCH_RISK_RESOURCE")
    if result.get("blocked") is not (proc.returncode == 3):
        raise RuntimeError("PATCH_RESULT_EXIT_MISMATCH")

    print("KUBERNETES_STRATEGIC_PATCH_RESOURCES:", result["resources"])
    print("NONEMPTY_THREE_WAY_PATCHES:", result.get("nonempty"))
    print("RESEARCH_ENGINE_PATCH_NONEMPTY:", result.get("research_patch"))
    print("UNRELATED_RESOURCE_PATCHES:", len(names))
    for name in names:
        print("PATCH_RESOURCE:", name)
    print("PATCH_BYTES_AND_SECRET_VALUES_NOT_DISPLAYED")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    if proc.returncode == 3:
        print("UPGRADE_REMAINS_BLOCKED: NONEMPTY_PATCH_REQUIRES_REVIEW")
        return 3
    print("PATCH_CHECK_NO_CHANGES")
    print("UPGRADE_STILL_REQUIRES_OPERATOR_REVIEW")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("PATCH_IMPACT_CHECK_BLOCKED")
        sys.exit(2)
