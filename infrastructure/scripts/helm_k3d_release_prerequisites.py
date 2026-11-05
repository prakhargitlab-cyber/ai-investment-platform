#!/usr/bin/env python3
"""Read-only, fail-closed release prerequisites: node cache and local backups.

This script is an evidence inventory, NOT an upgrade authorization. It never
reads database backup contents or Kubernetes Secret data.
"""
import json
from pathlib import Path
import subprocess
import sys
import time

SERVER = "k3d-ai-investment-dev-server-0"
EXPECTED_IMAGE = "localhost:5001/ai-investment/research-engine:etf-eq-rl-20261104-210859"
ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / ".tmp" / "main-db-backups"


def kubectl(*args):
    try:
        p = subprocess.run(
            ["docker", "exec", SERVER, "/bin/kubectl",
             "--kubeconfig=/etc/rancher/k3s/k3s.yaml", *args],
            capture_output=True, timeout=40, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("KUBECTL_UNAVAILABLE")
    if p.returncode:
        raise RuntimeError("KUBECTL_QUERY_FAILED")
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise RuntimeError("KUBECTL_RESPONSE_NOT_JSON")


def evidence():
    nodes = kubectl("get", "nodes", "-o", "json")["items"]
    if len(nodes) != 3:
        raise RuntimeError("NODE_COUNT_CHANGED")

    cached = []
    ready = []
    for node in nodes:
        name = node["metadata"]["name"]
        is_ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in node.get("status", {}).get("conditions", [])
        )
        if is_ready:
            ready.append(name)
        image_names = {
            tag for item in node.get("status", {}).get("images", [])
            for tag in item.get("names", [])
        }
        if EXPECTED_IMAGE in image_names:
            cached.append(name)
    print("NODES_READY:", len(ready), "/", len(nodes))
    print("RESEARCH_IMAGE_TAG_ADVERTISED_BY_NODES:", len(cached), "/", len(nodes))
    print("IMAGE_TAG_CACHE_EVIDENCE_ONLY: registry pull availability not established")
    if len(ready) != 3:
        raise RuntimeError("NODES_NOT_ALL_READY")

    # The node.status.images cache is incomplete on some K3s versions, and
    # absence does not prove the registry image is unavailable. Do not
    # manufacture a passing conclusion from absent cache evidence.
    if len(cached) != 3:
        print("IMAGE_AVAILABILITY_REQUIRES_MANUAL_REVIEW")

    if not BACKUP_DIR.is_dir():
        raise RuntimeError("MAIN_BACKUP_DIRECTORY_MISSING")
    backup_files = [p for p in BACKUP_DIR.iterdir() if p.is_file()
                    and p.stat().st_size > 0]
    if not backup_files:
        raise RuntimeError("NO_NONEMPTY_MAIN_BACKUP_FILES")
    newest = max(p.stat().st_mtime for p in backup_files)
    age_hours = round((time.time() - newest) / 3600, 1)
    print("NONEMPTY_MAIN_BACKUP_FILES:", len(backup_files))
    print("NEWEST_BACKUP_AGE_HOURS:", age_hours)
    print("BACKUP_RESTORE_NOT_VERIFIED_BY_THIS_SCRIPT")
    print("HELM_ROLLBACK_NOT_VERIFIED_BY_THIS_SCRIPT")
    print("RELEASE_PREREQUISITE_INVENTORY_COMPLETE")
    print("NO_DEPLOYMENT_OR_DATA_CHANGES_MADE")


if __name__ == "__main__":
    try:
        evidence()
    except Exception:
        # Do not print stderr or paths that could contain sensitive details.
        print("RELEASE_PREREQUISITE_INVENTORY_BLOCKED")
        sys.exit(2)
