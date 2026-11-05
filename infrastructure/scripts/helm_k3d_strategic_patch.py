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
    safe_secrets = result.get("verified_secret_noop")
    if not isinstance(safe_secrets, list) or len(safe_secrets) != len(set(safe_secrets)):
        raise RuntimeError("INVALID_VERIFIED_SECRET_REPORT")
    if any(not isinstance(n, str) or n not in {
        "Secret/aip-auth-jwt", "Secret/dev-postgres", "Secret/dev-searxng"
    } for n in safe_secrets):
        raise RuntimeError("UNKNOWN_VERIFIED_SECRET")
    if set(safe_secrets) & set(names):
        raise RuntimeError("VERIFIED_SECRET_STILL_IN_RISK_LIST")
    if result.get("blocked") is not (proc.returncode == 3):
        raise RuntimeError("PATCH_RESULT_EXIT_MISMATCH")

    permitted_categories = {
        "metadata", "type", "secret-data-or-config-data", "secret-stringData",
        "binary-data", "other", "spec", "spec-other",
        "deployment-spec-other", "pod-template-other",
        "pod-template-metadata", "pod-spec-other", "container-other",
        "container-image", "container-env"
    }
    categories = result.get("patch_categories")
    if not isinstance(categories, dict):
        raise RuntimeError("PATCH_CATEGORY_REPORT_MISSING")
    reported = {key for key, vals in categories.items()
                if isinstance(key, str) and isinstance(vals, list)}
    if len(reported) != result.get("nonempty") or (
        reported != set(names) | set(safe_secrets) | ({"Deployment/research-engine"}
                                  if result.get("research_patch") else set())
    ):
        raise RuntimeError("PATCH_CATEGORY_RESOURCE_SET_MISMATCH")
    if any(not vals or any(v not in permitted_categories for v in vals)
           for vals in categories.values()):
        raise RuntimeError("UNRECOGNIZED_PATCH_CATEGORY")
    jwt_code = result.get("jwt_failure_code", "")
    permitted_jwt_codes = {
        "", "VERIFIED", "PATCH_SHAPE", "OLD_DATA_INVALID",
        "NEW_DATA_INVALID", "LIVE_DATA_INVALID", "TYPE_MISMATCH",
        "OLD_TO_NEW_BYTES_MISMATCH", "NEW_TO_LIVE_BYTES_MISMATCH"
    }
    if jwt_code not in permitted_jwt_codes:
        raise RuntimeError("UNEXPECTED_JWT_DIAGNOSTIC")
    if ("Secret/aip-auth-jwt" in categories) != bool(jwt_code):
        raise RuntimeError("JWT_DIAGNOSTIC_COVERAGE_MISMATCH")
    if (jwt_code == "VERIFIED") != ("Secret/aip-auth-jwt" in safe_secrets):
        raise RuntimeError("JWT_DIAGNOSTIC_VERIFICATION_MISMATCH")
    research_labels = result.get("research_patch_labels")
    allowed_research_labels = {
        "STRATEGIC_LIST_DIRECTIVE", "OTHER_TOP_LEVEL",
        "OTHER_DEPLOYMENT_SPEC", "OTHER_POD_TEMPLATE",
        "OTHER_POD_SPEC", "CONTAINER_IMAGE",
        "OTHER_CONTAINER_FIELD", "OTHER_ENV_NAME", "APPROVED_ENV_NAME",
        "ENV_VALUE_PATCH", "OTHER_ENV_FIELD", "OTHER_NESTED_FIELD",
        "OTHER_LIST_PATCH", "NONOBJECT_PATCH"
    }
    if not isinstance(research_labels, list) or any(
        label not in allowed_research_labels for label in research_labels
    ):
        raise RuntimeError("INVALID_RESEARCH_AUDIT_LABELS")
    if bool(research_labels) != bool(result.get("research_patch")):
        raise RuntimeError("RESEARCH_AUDIT_COVERAGE_MISMATCH")
    print("KUBERNETES_STRATEGIC_PATCH_RESOURCES:", result["resources"])
    print("NONEMPTY_THREE_WAY_PATCHES:", result.get("nonempty"))
    print("RESEARCH_ENGINE_PATCH_NONEMPTY:", result.get("research_patch"))
    print("UNRESOLVED_UNRELATED_RESOURCE_PATCHES:", len(names))
    print("VERIFIED_STRINGDATA_ONLY_SECRET_NOOPS:", len(safe_secrets))
    for name in sorted(safe_secrets):
        print("VERIFIED_SECRET_NOOP:", name)
    for name in names:
        print("PATCH_RESOURCE:", name)
    for name in sorted(categories):
        print("PATCH_CATEGORY:", name, ",".join(sorted(set(categories[name]))))
    if jwt_code:
        print("JWT_SECRET_PATCH_AUDIT:", jwt_code)
    for label in research_labels:
        print("RESEARCH_PATCH_AUDIT:", label)
    print("RESEARCH_PATCH_NOT_YET_APPROVED")
    print("PATCH_BYTES_AND_SECRET_VALUES_NOT_DISPLAYED")
    print("SECRET_NOOP_MEANS_WRITE_ONLY_STRINGDATA_REMOVAL_AND_BYTE_EQUALITY")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    if proc.returncode == 3:
        print("UPGRADE_REMAINS_BLOCKED: NONEMPTY_PATCH_REQUIRES_REVIEW")
        return 3
    print("PATCH_CHECK_NO_CHANGES")
    print("UPGRADE_STILL_REQUIRES_OPERATOR_REVIEW")
    return 0


# Only exact reviewed literals are eligible for diagnostics. Never print
# exception strings from subprocess/JSON/system errors: they might include
# credential material or parser excerpts from input manifests.
SAFE_FAILURE_CODES = frozenset({
    "COMPILED_GO_PATCH_CALCULATOR_MISSING",
    "KUBERNETES_PATCH_CALCULATION_FAILED",
    "UNEXPECTED_PATCH_REPORT_VERSION",
    "PATCH_RESOURCE_COUNT_MISMATCH",
    "INVALID_PATCH_RISK_NAMES",
    "UNKNOWN_PATCH_RISK_RESOURCE",
    "INVALID_VERIFIED_SECRET_REPORT",
    "UNKNOWN_VERIFIED_SECRET",
    "VERIFIED_SECRET_STILL_IN_RISK_LIST",
    "PATCH_RESULT_EXIT_MISMATCH",
    "PATCH_CATEGORY_REPORT_MISSING",
    "PATCH_CATEGORY_RESOURCE_SET_MISMATCH",
    "UNRECOGNIZED_PATCH_CATEGORY",
    "UNEXPECTED_JWT_DIAGNOSTIC",
    "JWT_DIAGNOSTIC_COVERAGE_MISMATCH",
    "JWT_DIAGNOSTIC_VERIFICATION_MISMATCH",
    "INVALID_RESEARCH_AUDIT_LABELS",
    "RESEARCH_AUDIT_COVERAGE_MISMATCH",
})


def safe_failure_code(exc):
    if type(exc) is RuntimeError and len(exc.args) == 1:
        if type(exc.args[0]) is str and exc.args[0] in SAFE_FAILURE_CODES:
            return exc.args[0]
    return "UNCLASSIFIED_READ_ONLY_CHECK_FAILURE"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("PATCH_IMPACT_CHECK_BLOCKED:", safe_failure_code(exc))
        sys.exit(2)
