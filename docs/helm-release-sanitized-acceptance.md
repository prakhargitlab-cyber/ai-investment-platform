# Helm revision-2 sanitized acceptance

This is a read-only acceptance tool for the existing local k3d cluster. It is not a deployment tool. It reuses the internal k3d Helm access established by PR #23 without depending on the broken Windows host-port mapping or copying a Linux symlink to Windows.

From the repository root, after fetching this PR branch, with the Helm Docker image already present and PyYAML installed in the project's Python venv:

    .\.venv\Scripts\python.exe infrastructure\scripts\helm_k3d_acceptance.py

The tool:
- Verifies that the Kubernetes API is healthy and Helm revision 1 is the deployed revision.
- Runs a server-connected Helm revision-2 dry-run with protected values and the approved NSE image tag; no install/upgrade is submitted.
- Keeps unredacted rendered manifests only in process memory because equality checks cannot use Helm's --hide-secret output; stdout and stderr from Helm are never displayed.
- Compares 54 proposed identities with live resources and intended spec/data fields, ignoring server-injected defaults and bookkeeping metadata.
- Checks rendered data equality for both DEV PostgreSQL and SearXNG credential keys.
- Requires research-engine app/init images to match the approved tag.
- Prints only counts and changed field paths, never Secret values. Blocks the release on any unexpected difference.

Security: The tool reads live Kubernetes Secrets and temporarily mounts the existing admin kubeconfig in a disposable Helm container. Use on a trusted Windows workstation and verified Docker image only. It removes its temporary kubeconfig on exit. No log, dump, or manifest artifacts are created.

The tool deliberately fails closed on drift. It does not verify the research-image cache on every k3d node, rollback, or backup validity; these are separate gates before an actual upgrade. Any BSE market-data recovery requiring executed price, positive volume, currency, and timestamps remains out of scope.

Do not merge this PR or run a Helm upgrade on the basis of an untested result. First run the script and its pure unit tests (pytest infrastructure/scripts/test_helm_k3d_acceptance.py), review sanitized output, and separately verify image availability and rollback capability.

## First operator result (blocked safely)

The first run verified 54/54 resource identities, exact DEV Postgres/SearXNG credential matches and approved research images, but reported 105 desired-vs-live field differences, including unrelated Java workload image/env fields. No upgrade occurred. This is **not** an application incident: it means the proposed upgrade was not equivalent to live desired state. The first script used only --reuse-values; the chart's established LOCAL/DEV configuration is in values-dev.yaml. The next revision explicitly supplies -f /chart/values-dev.yaml in addition to --reuse-values, retaining the guard. This is a hypothesis to test, not proof that the 105 differences are harmless. If any differences remain, the gate must continue blocking. No automatic drift repair or deployment is authorized.
