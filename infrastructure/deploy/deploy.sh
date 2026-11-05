#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-plan}"
case "$ACTION" in plan|verify) ;; *) echo "Expected plan or verify" >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$ROOT/release_guard.py" "$ACTION" --spec "$ROOT/releases/auth-reset-pr28.k3d.json" --mode k3d --node k3d-ai-investment-dev-server-0 --allow-local-http-registry
