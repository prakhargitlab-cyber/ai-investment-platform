# Local k3d Helm access when Docker Desktop drops published ports

## Observed incident (2026-11-05)

The Windows kubeconfig was initially pointing at an unrelated AKS context. The local k3d kubeconfig then pointed to host.docker.internal:54206, but Docker showed HostConfig.PortBindings for the k3d load balancer while NetworkSettings.Ports had empty lists. Windows could not connect to that port. The Kubernetes API itself returned ok from /readyz inside k3d, with three nodes Ready, research-engine 1/1, and postgres-0 1/1. These facts do not justify restarting Docker Desktop, recreating the cluster, or touching the database.

Copying /etc/rancher/k3s/k3s.yaml with docker cp failed on Windows because that path is a symlink and the Windows user lacked symlink privileges. The read-only preflight uses docker exec ... /bin/cat instead, which dereferences the link inside the Linux container.

## One-command read-only connectivity check

Run from the repository root, in PowerShell:

    & .\infrastructure\scripts\helm-k3d-readonly-preflight.ps1

Prerequisites: Docker Desktop running, the existing k3d server container running, and the alpine/helm:4.2.3 image available (pull it separately only if missing). The script does not use the Windows default Kubernetes context. It verifies /readyz, takes an ephemeral copy of the internal k3s kubeconfig, starts a disposable Helm client with the existing server's network namespace, and performs only helm list and helm history. It deletes the temporary kubeconfig in finally.

Security: the temporary kubeconfig contains cluster-admin material. Never print it, commit it, archive it, or leave it on disk. This is an operator-local preflight; the script does not fix Docker Desktop host port publishing and must not be run against untrusted container images. A bind-mounted kubeconfig is accessible to the temporary Helm container during execution. Use a pinned and reviewed Helm image in security-sensitive environments.

If the preflight fails, record only its error code and relevant non-secret container/network status. Do not retry with privileged containers, disabled TLS checks, cluster recreation, Docker prune, or Docker Desktop restart.

## Separate release gate — NOT executed by this script

The Helm chart credential-preservation fix is already merged on main (commit bcbd005). The installed release may still be revision 1. This connectivity script deliberately does not reconcile it.

Before any revision-2 upgrade, a release operator must:

1. Validate the correct local cluster, release, namespace, and explicit chart/values precedence.
2. Perform a Kubernetes-connected Helm server-side dry run with secrets hidden; ensure the new lookup logic reuses both the existing PostgreSQL and SearXNG credentials, and does not rotate them.
3. Compare full resources with stored revision 1 AND live Kubernetes, not just resource counts. Preserve 54 identities and ensure no unexpected pod, database, storage, Secret, ConfigMap or service changes.
4. Verify that BOTH research-engine containers use the deployed NSE EQ/RL image and all five protected DEV settings match the running Deployment; scheduler remains off.
5. Verify the image is available to the target k3d nodes after prior Docker image pruning. Validate backups independently.
6. Obtain explicit approval for a narrowly scoped upgrade and post-upgrade health checks. Keep rollback and database recovery available.

A dry run is a validation operation, not proof of risk-free deployment. The script does not implement or authorize the upgrade.

## Separate BSE market-data blocker

Verified BSE listing identity is not the same as executed trading evidence. Live BSE acquisition remains blocked pending an authorized feed with eligible session timestamps, executed price, positive volume and verified currency. Do not weaken fail-closed readiness rules, invent prices, or claim all 359 ETFs are ready. The absence of that external feed cannot be resolved by a Kubernetes or Helm PR.
