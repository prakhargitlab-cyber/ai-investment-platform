# First-ADMIN bootstrap and on-demand PostgreSQL backup/restore

This covers two `platform.ps1` features added on top of the existing
`role-admin-cli` role-management mechanism. Both are reviewed-but-not-yet-run
in this checkout: see the "Verification status" note at the end before
relying on either in a shared environment.

## Feature 1: automatic first-ADMIN bootstrap

`.\platform.ps1 up` now calls `Invoke-FirstAdminBootstrapIfNeeded` once the
full stack is deployed and healthy. It submits a disposable Kubernetes Job
that runs the auth-service jar under a new `first-admin-bootstrap` Spring
profile (`FirstAdminBootstrapRunner` / `FirstAdminBootstrapService`), which:

1. Takes the same `auth.role_admin_lock` row lock already used by
   `RoleAdminService`, so this can never race with a concurrent bootstrap
   attempt or a manual `role-admin-cli` grant/revoke.
2. If any account already holds ADMIN, does nothing (exit code 0).
3. If accounts exist but none holds ADMIN, refuses to act (exit code 3) and
   `platform.ps1` prints a warning pointing at `role-admin-cli` for manual,
   reviewed recovery -- it never silently creates a second administrator.
4. Only on a genuinely empty database does it create the account (password
   hashed with the same `BCryptPasswordEncoder` normal registration uses),
   mark it `ACTIVE` the same way email verification would, and grant it
   ADMIN exclusively through `RoleAdminService.grantRole` (so the grant gets
   a normal `app_user_role_audit` row, operator `system:first-admin-bootstrap`).

The account email defaults to `prakhar.gitlab@gmail.com` and can be
overridden with `-FirstAdminEmail` or `$env:AIP_FIRST_ADMIN_EMAIL`. The
password is generated locally by `platform.ps1`, passed into the Job only
through a short-lived Kubernetes Secret (`first-admin-bootstrap-credentials`,
deleted immediately after the Job finishes), and printed to the console
exactly once -- it is never written to disk and never logged by the Java
side. Save it when shown; it cannot be recovered afterward (only its hash is
stored).

Because the decision logic lives entirely in the locked transaction, running
`.\platform.ps1 up` repeatedly, after a `down`/`up` cycle, or after a
`restore` that already contains an ADMIN, is always safe and makes no
duplicate account, grant, or audit row.

## Feature 2: on-demand PostgreSQL backup/restore

```powershell
.\platform.ps1 backup
.\platform.ps1 backups
.\platform.ps1 backup-verify -Backup latest
.\platform.ps1 restore -Backup latest
.\platform.ps1 restore -Backup 2026-10-10_153000_ab12cd -AuthorizeDestructiveRestore
```

Nothing here runs automatically. Implementation lives in `scripts/database/`
(`PostgresBackupCommon.psm1`, `backup.ps1`, `list-backups.ps1`,
`verify-backup.ps1`, `restore.ps1`), dot-sourced by `platform.ps1`.

- **backup**: runs `pg_dump -Fc` for the `investment` database inside the
  `postgres` StatefulSet pod, pulls the archive out with `kubectl cp`
  (binary-safe; never through a PowerShell text pipeline), checksums it,
  confirms `pg_restore --list` can read it, and only then publishes
  `backups\postgres\<timestamp>_<suffix>\` (written to a `.tmp-*` sibling
  first, renamed atomically on success -- a failed or interrupted backup
  never appears as a valid one). `manifest.json` records the backup id,
  UTC timestamp, source kube context/namespace, PostgreSQL server version,
  database name/format/file/size/checksum, the current git commit, every
  per-schema Flyway version found, and a `verificationStatus` of
  `ARCHIVE_INTEGRITY_VERIFIED` (explicitly not "restore-tested" -- see
  below). No password or Kubernetes Secret is ever written into it.
- **backups**: lists `backups\postgres\*\manifest.json`, newest first by
  the manifest's own `createdAtUtc` (never raw filesystem mtime).
- **backup-verify**: re-checks manifest validity, file presence, sizes,
  SHA-256 checksums, and `pg_restore --list`. It explicitly prints that this
  is an archive-integrity check, not a full test restore.
- **restore**: resolves and re-verifies the backup, prints the full
  source/target plan, checks PostgreSQL major-version compatibility and
  whether the backup's recorded Flyway versions are newer than anything in
  this checkout, detects existing data in the target database, and requires
  typing `RESTORE` to proceed. Overwriting a database that already has
  schema objects, or restoring across a different source/target kube
  context or namespace, additionally requires `-AuthorizeDestructiveRestore`.
  When existing data is detected, `platform.ps1` scales the DB-connected
  Java services (and research-engine) to zero replicas first (stopping
  writers), takes one more safety backup unless `-SkipSafetyBackup` is
  passed, restores with `pg_restore --clean --if-exists --no-owner
  --no-privileges` (so source-cluster role/ownership is never blindly
  copied onto the target), then scales the services back to their prior
  replica counts. It never runs Flyway, first-ADMIN bootstrap, or an
  Opportunity Radar cycle itself -- those only happen through normal
  application startup, independent of this script. Pass `-TargetDatabase
  <name>` to restore into a separate, disposable database on the same
  Postgres instance first, which is the recommended way to test a backup
  before ever restoring over the live `investment` database.

`backups\` is covered by a new `.gitignore` entry; dumps are never committed.

## Verification status (read before relying on this)

- Feature 1's Java code (`FirstAdminBootstrapProperties`/`Outcome`/
  `Service`/`Runner`, plus the `AppUserRoleRepository.existsByRole` and
  `AuthServiceApplication` changes) and its new tests
  (`FirstAdminBootstrapServiceIntegrationTest`,
  `FirstAdminBootstrapRunnerTest`) were written against the real
  surrounding source and reviewed line-by-line against
  `RoleAdminService`/`RoleAdminCliRunner`/`AuthLifecycleService`, but
  **`mvn test` could not be executed** in this environment: the sandbox
  used for this change has no Maven/JDK toolchain reachable from the
  repository checkout, and the separate environment that does have Maven
  has no network path to Maven Central. Run the existing
  `services/auth-service` test suite (including the two new test classes)
  before merging.
- The concurrency test for Feature 1 is explicitly marked best-effort: it
  runs against the same H2-in-PostgreSQL-mode database the rest of the
  auth-service test suite uses, and H2's `SELECT ... FOR UPDATE` locking is
  not guaranteed to match real PostgreSQL semantics. Re-verify the
  "concurrent bootstrap attempts produce exactly one grant" guarantee with a
  disposable real PostgreSQL database, following the same opt-in pattern as
  `RoleAdminPostgresVerificationTest`.
- All of Feature 2 (`scripts/database/*.ps1` and the `platform.ps1`
  changes) was written and only balance/structure-checked (matching
  braces/parens/brackets); **no PowerShell interpreter was available in
  either environment used for this change**, so none of it has been
  syntax-checked by `powershell`/`pwsh`, let alone executed against a real
  cluster or PostgreSQL instance. Run `.\platform.ps1 backup` against a
  disposable or already-expendable local environment first, and read
  through `scripts/database/restore.ps1` closely before the first real
  restore, particularly the maintenance-mode scale-down/scale-up and the
  `-AuthorizeDestructiveRestore`/`-SkipSafetyBackup` gates.
- Neither feature was run against the existing `k3d-ai-investment-dev`
  cluster, Helm release, or PostgreSQL data at any point while producing
  this change, consistent with the task's safety constraints.
