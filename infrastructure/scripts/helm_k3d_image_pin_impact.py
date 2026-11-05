#!/usr/bin/env python3
"""Read-only exact scoped Helm server-render of private image-pin candidate.

No manifest bodies, images, credentials or env values are displayed.
Never performs kubectl or Helm writes.
"""
from pathlib import Path
import json
import re
import sys
import tempfile
import yaml
import helm_k3d_acceptance as base
import helm_k3d_impact_gate as impact
import helm_k3d_image_rebaseline as pins
from helm_k3d_reconcile_fields import differences

SAFE_TAG = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$")


def candidate_manifest():
    path = pins.OUTPUT
    # Explicit file validation before allowing Helm to read private values.
    if not path.is_file() or path.stat().st_size > 8192:
        raise RuntimeError("IMAGE_PINS_FILE_MISSING_OR_TOO_LARGE")
    value = yaml.safe_load(path.read_bytes())
    if not isinstance(value, dict) or set(value) != {"workloadOverrides"}:
        raise RuntimeError("UNEXPECTED_CANDIDATE_KEYS")
    overrides = value["workloadOverrides"]
    if not isinstance(overrides, dict) or set(overrides) != pins.FIRST_PARTY:
        raise RuntimeError("UNEXPECTED_IMAGE_PIN_SET")
    for item in overrides.values():
        if (not isinstance(item, dict) or set(item) != {"imageTagOverride"}
                or not isinstance(item["imageTagOverride"], str)
                or not SAFE_TAG.fullmatch(item["imageTagOverride"])
                or item["imageTagOverride"] in ("dev", "latest")):
            raise RuntimeError("INVALID_IMAGE_PIN")

    config = base.call(["docker", "exec", base.SERVER, "/bin/cat", base.K3S_CONFIG])
    if b"server: https://127.0.0.1:6443" not in config:
        raise RuntimeError("INTERNAL_KUBE_API_CONFIG_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="helm-pins-review-") as d:
        cfg = Path(d) / "config"
        cfg.write_bytes(config)
        cmd = [
            "docker", "run", "--rm", "--network", "container:" + base.SERVER,
            "--mount", "type=bind,source=" + str(cfg) + ",target=/tmp/config,readonly",
            "--mount", "type=bind,source=" + str(base.CHART) + ",target=/chart,readonly",
            "--mount", "type=bind,source=" + str(path.resolve()) + ",target=/tmp/pins.yaml,readonly",
            "--entrypoint", "helm", base.IMAGE,
            "--kubeconfig", "/tmp/config",
            "upgrade", base.RELEASE, "/chart",
            "--namespace", base.NAMESPACE, "--reuse-values", "--dry-run=server",
            "--values", "/tmp/pins.yaml",
            "--set", "research.opportunityRecoveryPaused=true",
            "--set", "research.opportunityScheduler.enabled=false",
            "--set", "research.equityManualExecutionEnabled=true",
            "--set", "research.etfManualExecutionEnabled=true",
            "--set", "research.etfFullUniverseExecutionEnabled=true",
            "--set-string", "aiServiceImageOverrides.research-engine.tag=" + base.TAG
        ]
        output = base.call(cmd).decode("utf-8")
    marker = "\nMANIFEST:\n"
    if marker not in output:
        raise RuntimeError("RENDERED_MANIFEST_MISSING")
    result = output.split(marker, 1)[1].split("\nNOTES:\n", 1)[0]
    docs = [x for x in yaml.safe_load_all(result) if isinstance(x, dict)]
    by_id = {base.rid(x): x for x in docs}
    if len(by_id) != len(docs):
        raise RuntimeError("DUPLICATE_RENDERED_RESOURCE")
    return by_id


def main():
    # Existing loader captures live/old and the known, previously scoped
    # research-only candidate. This deliberately doesn't upgrade anything.
    old, original, live = impact.load_manifests()
    base.only_approved_revision_changes(old, original, live)
    updated = candidate_manifest()
    if (len(old) != base.EXPECTED_COUNT or set(old) != set(updated)
            or set(old) - set(live)):
        raise RuntimeError("RENDERED_RESOURCE_IDENTITY_MISMATCH")
    # The 16 workloads must now render exactly the current live main images.
    aligned = []
    registry_only = []
    tag_mismatch = []
    pin_ignored = []
    live_changed_since_pin = []
    for name in sorted(pins.FIRST_PARTY):
        key = ("apps/v1", "Deployment", name)
        current = next(x for x in live[key]["spec"]["template"]["spec"]["containers"]
                       if x.get("name") == name)
        desired = next(x for x in updated[key]["spec"]["template"]["spec"]["containers"]
                       if x.get("name") == name)
        if desired["image"] == current["image"]:
            aligned.append(name)
        else:
            live_tag = pins.image_tag(current["image"], name)
            desired_tag = pins.image_tag(desired["image"], name, allow_mutable=True)
            if live_tag == desired_tag:
                registry_only.append(name)
            else:
                tag_mismatch.append(name)
                expected_pin = yaml.safe_load(pins.OUTPUT.read_bytes())["workloadOverrides"][name]["imageTagOverride"]
                if desired_tag != expected_pin:
                    pin_ignored.append(name)
                if live_tag != expected_pin:
                    live_changed_since_pin.append(name)
    # Inspect authored spec mutations without accidentally approving them.
    other_changed = []
    for key in sorted(old):
        if key == ("apps/v1", "Deployment", "research-engine"):
            continue
        if key[1] == "Deployment" and key[2] in pins.FIRST_PARTY:
            previous = old[key]["spec"]
            current = updated[key]["spec"]
            # Images are the only allowed change for these 16 workloads.
            from copy import deepcopy
            previous, current = deepcopy(previous), deepcopy(current)
            for spec in (previous, current):
                containers = spec["template"]["spec"]["containers"]
                for item in containers:
                    if item.get("name") == key[2]:
                        item.pop("image", None)
            if differences(previous, current):
                other_changed.append(key[2])
        elif key[1] == "Secret":
            # Metadata/resourceVersion changes alone are not credential changes.
            # Use the same effective data normalization as the secret guard;
            # never print the bytes or key names.
            if base.selected(old[key]) != base.selected(updated[key]):
                other_changed.append("Secret/" + key[2])
        elif old[key] != updated[key]:
            other_changed.append(key[1] + "/" + key[2])
    print("IMAGE_PIN_CANDIDATE_RESOURCE_COUNT:", len(updated))
    print("FIRST_PARTY_IMAGES_MATCH_LIVE:", len(aligned))
    print("IMAGE_REGISTRY_ALIAS_ONLY:", len(registry_only))
    for name in registry_only:
        print("REGISTRY_ALIAS_ONLY:", name)
    print("IMAGE_TAG_MISMATCH:", len(tag_mismatch))
    print("HELM_RENDER_IGNORED_PIN:", len(pin_ignored))
    for name in pin_ignored:
        print("PIN_NOT_RENDERED:", name)
    print("LIVE_TAG_CHANGED_SINCE_CANDIDATE:", len(live_changed_since_pin))
    for name in live_changed_since_pin:
        print("PIN_STALE_AGAINST_LIVE:", name)
    for name in tag_mismatch:
        print("TAG_NOT_ALIGNED:", name)
    print("NON_IMAGE_OR_OUT_OF_SCOPE_RENDERED_CHANGES:", len(other_changed))
    for name in other_changed[:20]:
        print("OUT_OF_SCOPE_RESOURCE:", name)
    print("NO_CREDENTIALS_OR_IMAGE_TAGS_PRINTED")
    print("NO_KUBERNETES_WRITES_PERFORMED")
    print("HELM_UPGRADE_NOT_AUTHORIZED")
    return 0 if not tag_mismatch and not other_changed else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("IMAGE_PIN_IMPACT_REVIEW_BLOCKED")
        sys.exit(2)
