#!/usr/bin/env python3
"""Conservative no-write Helm upgrade impact gate.

Do not confuse this with a faithful Helm strategic-patch simulator. It refuses
to authorize upgrade whenever a live pod template would be exposed to a Helm
three-way reconciliation risk, or protected state differs.
"""
import json
import sys
import tempfile
from pathlib import Path

import yaml
import helm_k3d_acceptance as base


class ImpactBlocked(Exception):
    pass


def critical_live_drift(old, desired, live):
    """Return only safe resource identifiers; never credential values."""
    findings = []
    protected = {"Deployment", "StatefulSet", "Secret", "PersistentVolumeClaim"}
    for key in sorted(old):
        if key not in desired or key not in live:
            findings.append(("IDENTITY", key[1], key[2]))
            continue
        old_doc, target, current = old[key], desired[key], live[key]
        kind, name = key[1], key[2]
        if kind not in protected:
            continue
        if kind == "Secret":
            if base.selected(target) != base.selected(current):
                findings.append(("SECRET_DATA_OR_TYPE", kind, name))
        elif kind == "PersistentVolumeClaim":
            if base.selected(old_doc) != base.selected(target):
                findings.append(("PVC_DESIRED_CHANGE", kind, name))
        elif kind == "StatefulSet":
            if base.selected(target) != base.selected(current):
                findings.append(("STATEFULSET_LIVE_DIFFERS_FROM_TARGET", kind, name))
        elif name != "research-engine":
            baseline_pod = (old_doc.get("spec") or {}).get("template")
            proposed_pod = (target.get("spec") or {}).get("template")
            live_pod = (current.get("spec") or {}).get("template")
            if baseline_pod != proposed_pod:
                findings.append(("UNRELATED_DEPLOYMENT_DESIRED_CHANGE", kind, name))
            if proposed_pod != live_pod:
                findings.append(("UNRELATED_DEPLOYMENT_LIVE_DRIFT", kind, name))
    return findings


def load_manifests():
    config = base.call(["docker", "exec", base.SERVER, "/bin/cat", base.K3S_CONFIG])
    if b"server: https://127.0.0.1:6443" not in config:
        raise ImpactBlocked("INTERNAL_API_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="aip-impact-") as work:
        cfg = Path(work) / "kubeconfig"
        cfg.write_bytes(config)
        helm = ["docker", "run", "--rm",
                "--network", "container:" + base.SERVER,
                "--mount", "type=bind,source=" + str(cfg) +
                           ",target=/tmp/kubeconfig,readonly",
                "--entrypoint", "helm", base.IMAGE,
                "--kubeconfig", "/tmp/kubeconfig"]
        original = base.call(helm + ["get", "manifest", base.RELEASE,
                                    "--namespace", base.NAMESPACE, "--revision", "1"])
        desired_output = base.call(
            helm[:8] +
            ["--mount", "type=bind,source=" + str(base.CHART) + ",target=/chart,readonly"] +
            helm[8:] +
            ["upgrade", base.RELEASE, "/chart", "--namespace", base.NAMESPACE,
             "--reuse-values", "--dry-run=server",
             "--set", "research.opportunityRecoveryPaused=true",
             "--set", "research.opportunityScheduler.enabled=false",
             "--set", "research.equityManualExecutionEnabled=true",
             "--set", "research.etfManualExecutionEnabled=true",
             "--set", "research.etfFullUniverseExecutionEnabled=true",
             "--set-string", "aiServiceImageOverrides.research-engine.tag=" + base.TAG]
        ).decode("utf-8")
        marker = "\nMANIFEST:\n"
        if marker not in desired_output:
            raise ImpactBlocked("RENDERED_MANIFEST_MISSING")
        target_yaml = desired_output.split(marker, 1)[1].split("\nNOTES:\n", 1)[0]
        old = {base.rid(d): d for d in yaml.safe_load_all(original) if isinstance(d, dict)}
        target = {base.rid(d): d for d in yaml.safe_load_all(target_yaml) if isinstance(d, dict)}
    live_result = json.loads(base.kube("get", base.RESOURCE_KINDS, "-o", "json"))
    live = {base.rid(d): d for d in live_result["items"]}
    if len(old) != base.EXPECTED_COUNT or len(target) != base.EXPECTED_COUNT:
        raise ImpactBlocked("RESOURCE_COUNT_MISMATCH")
    if set(old) != set(target) or set(target) - set(live):
        raise ImpactBlocked("RESOURCE_IDENTITIES_MISMATCH")
    return old, target, live


def main():
    old, target, live = load_manifests()
    # Scope checks still apply, before impact discussion.
    base.only_approved_revision_changes(old, target, live)
    risks = critical_live_drift(old, target, live)
    print("READ_ONLY_IMPACT_GATE_RESOURCE_COUNT:", len(target))
    print("UNRESOLVED_PROTECTED_RESOURCE_RISK_COUNT:", len(risks))
    for typ, kind, name in risks:
        print("PATCH_RISK:", typ, kind + "/" + name)
    if risks:
        raise ImpactBlocked("POTENTIAL_UNINTENDED_HELM_RECONCILIATION")
    print("CONSERVATIVE_IMPACT_GATE_PASSED")
    print("ACTUAL_HELM_STRATEGIC_PATCH_NOT_COMPUTED")
    print("NO_UPGRADE_PERFORMED")


if __name__ == "__main__":
    try:
        main()
    except (ImpactBlocked, base.Blocked) as exc:
        print("IMPACT_GATE_BLOCKED:", exc)
        sys.exit(2)
    except Exception:
        print("IMPACT_GATE_BLOCKED: UNEXPECTED_ERROR")
        sys.exit(2)
