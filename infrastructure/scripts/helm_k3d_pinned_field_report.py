#!/usr/bin/env python3
"""Read-only field-path diagnostics for 17 live Deployment drifts.

Only public Kubernetes field names, whitelisted container names and counts
are emitted. No image tags, env values, Secrets, or manifest bodies.
"""
import sys

import helm_k3d_impact_gate as impact
import helm_k3d_image_pin_impact as pinned
import helm_k3d_reconcile_fields as fields

NAMES = tuple(sorted(fields.UNRELATED))
SAFE_PATH_PREFIXES = ("spec.",)
MAX_PATHS = 14


def main():
    old, _, live = impact.load_manifests()
    proposed = pinned.candidate_manifest()
    if len(old) != 54 or set(old) != set(proposed) or set(old) - set(live):
        raise RuntimeError("RESOURCE_SCOPE_MISMATCH")
    # Only print differences on Deployment specs, never Secret or ConfigMap data.
    for name in NAMES:
        key = ("apps/v1", "Deployment", name)
        before = old[key].get("spec")
        after = proposed[key].get("spec")
        running = live[key].get("spec")
        if not all(isinstance(x, dict) for x in (before, after, running)):
            raise RuntimeError("MISSING_DEPLOYMENT_SPEC")
        old_new = fields.differences(before, after)
        old_live = fields.differences(before, running)
        new_live = fields.differences(after, running)
        print("DEPLOYMENT:", name)
        print("  OLD_NEW_FIELD_COUNT:", len(old_new))
        print("  OLD_LIVE_FIELD_COUNT:", len(old_live))
        print("  NEW_LIVE_FIELD_COUNT:", len(new_live))
        # All paths are emitted through the safe_name() sanitizer in fields.
        # Env var names are reported but their values never are.
        for label, paths in (
            ("OLD_TO_NEW", old_new),
            ("NEW_TO_LIVE", new_live),
        ):
            for path in paths[:MAX_PATHS]:
                print(" ", label + "_PATH:", path)
            if len(paths) > MAX_PATHS:
                print(" ", label + "_MORE:", len(paths) - MAX_PATHS)
    print("READ_ONLY_DEPLOYMENT_FIELD_REPORT_COMPLETE")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("HELM_UPGRADE_NOT_AUTHORIZED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("READ_ONLY_DEPLOYMENT_FIELD_REPORT_BLOCKED")
        sys.exit(2)
