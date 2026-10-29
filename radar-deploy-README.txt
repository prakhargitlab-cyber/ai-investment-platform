RADAR DEPLOYMENT UTILITY (PowerShell 5.1)
========================================
Place radar-deploy.ps1 in C:\workspace\ai-investment-platform\ (same directory as platform.ps1).
It does not run or change platform.ps1.

Commands from repository root:
  .\radar-deploy.ps1 -Action Plan
  .\radar-deploy.ps1 -Action Build -Tag radar-20261015-01 -EnableDevLoginBuild
  .\radar-deploy.ps1 -Action Push -Tag radar-20261015-01 -ApprovePush
  .\radar-deploy.ps1 -Action Deploy -Tag radar-20261015-01 -ApproveDeploy
  .\radar-deploy.ps1 -Action Fresh -ApproveFresh -Namespace ai-investment-new -FreshValuesFile C:\path\reviewed-values.yaml

Deployment is prompted again, modifies only selected workloads' container image references,
and checks rollout health. It DOES NOT Helm-upgrade the entire release.
To deploy only frontend: -Components frontend
To deploy only research-engine: -Components research-engine

Temporary kubeconfig:
  If the Kubernetes API still uses the known unbound host port, recreate the previously
  verified temporary Docker socat forwarder and supply -Kubeconfig <path-to-temporary-kubeconfig>.
  Or export KUBECONFIG before invoking the script. Never disable TLS verification.

IMPORTANT:
  - Fresh mode installs into a NEW namespace only, and requires -FreshValuesFile with reviewed image tags, secrets, persistence and environment settings. It never creates a k3d cluster.
  - Docker pushes are explicit and separately approved; build is local only.
  - Image build may download Dockerfile package dependencies; --pull=false only avoids refreshing base image.
  - Helm owns deployments, so later Helm upgrades may revert image tags set with kubectl.
  - Application startup may execute migrations. Verify the current migration readiness first.
  - Never run this tool in production; mutating actions check expected k3d dev context.
  - Do not commit private kubeconfig or auth-admin-grant-once.json.
