# Stage 2C: backup consistency and fresh-provisioning verification

The isolated change is based on Stage 2B (`e6c1d7f`). Validation uses Windows
PowerShell 5.1, Pester 5.7.1, Helm, and a disposable PostgreSQL 16 container.
It does not access Kubernetes, the live database, or Radar.

`platform.ps1`, `backup.ps1`, and `restore.ps1` import the shared `.psm1` module.
Both modules explicitly export their public functions. Both Pester files import
the production modules; I/O mocks use `-ModuleName PostgresBackupCommon`.
`restore.ps1` calls `Resolve-RowCountsTrustworthy` directly. Tests call that helper
with real manifest objects and check the restore AST for the production call.
Malformed, absent, or unsynchronized counts cannot produce full verification.
Restore rejects missing/extra tables and count failures instead of silently
comparing a partial result. Ordered dictionaries use `Contains`, not `ContainsKey`.

## Fresh-install evidence

There is no “sum of all non-cycle tables” heuristic. The DEV chart mounts
`files/fresh-provisioning.sql` in the official image's initdb directory. It creates
one marker in `postgres.platform_bootstrap.provisioning` only when PGDATA is
initialized. An existing PVC does not receive a marker on upgrade or restart.
The marker records the application database name, creation time, and consumption
time. It is outside the application database's logical backup.

The initial-cycle check atomically consumes an unconsumed marker created within
the previous hour. Only the exact successful result `claimed` is accepted. Missing
tables, errors, empty results, repeated claims, and expired markers deny startup.
The marker is consumed before the remaining eligibility checks and before any
Radar request; failed or interrupted checks do not grant an automatic retry.
This is deliberately conservative: a slow or interrupted first provisioning may
skip the automatic initial cycle. Existing installations are never retroactively
declared fresh. Do not manually create or reset this marker.

Flyway history, seed/reference rows, and initialization records may exist: they
are not summed or enumerated to infer freshness. The Radar table and user table
must still exist and have readable, nonnegative counts. Any cycle row denies
eligibility. User count must be zero, or exactly one when this same startup
positively confirmed `first_admin_bootstrap_outcome=CREATED`. An already-present
admin or additional user is not allowlisted. Ordinary startup and the independent
research scheduler retain their existing behavior.

The supported restore path revokes the target database's marker before invoking
`pg_restore`, including failed restores. Revocation errors abort restore; old
installations without a marker remain supported. Raw/manual database replacement
outside this path must not be combined with a pending provisioning marker.

## Snapshot protocol

The production script builder is also used by the integration test. One psql
session opens REPEATABLE READ and exports a snapshot. `\setenv` passes the actual
snapshot to the `pg_dump --snapshot` child while the exporter remains open.
Enumeration and counts execute inside that same transaction. The caller also
validates the complete table set against its preliminary enumeration; concurrent
DDL that changes that set fails closed. `-X -qAt` avoids startup-file effects and
transaction status lines in count output.

psql does not expand variables inside `\!`; see the official
[psql documentation](https://www.postgresql.org/docs/16/app-psql.html).
Shell-command errors are not SQL errors, so a unique temporary status file records
the actual pg_dump exit code. The shell propagates SQL exit 3 and dump exit codes
separately, and removes the temporary directory on exit. PowerShell 5.1 transports
the script as UTF-8 base64 to avoid native multiline/quote corruption. Dump bytes
still use the existing binary-safe file copy path.

## Windows validation commands

Run from this isolated worktree with Docker Desktop running and Pester 5 installed:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
Import-Module Pester -MinimumVersion 5.0
$result = Invoke-Pester .\scripts\database\tests -Output Detailed -PassThru
if ($result.FailedCount -gt 0) { throw 'Stage 2C Pester validation failed' }

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\database\tests\Validate-SnapshotIntegration.ps1
if ($LASTEXITCODE -ne 0) { throw 'Stage 2C PostgreSQL integration validation failed' }

powershell -NoProfile -ExecutionPolicy Bypass -File .\platform.ps1 components
helm lint .\infrastructure\helm\ai-investment-platform -f .\infrastructure\helm\ai-investment-platform\values-dev.yaml
helm lint .\infrastructure\helm\ai-investment-platform -f .\infrastructure\helm\ai-investment-platform\values-dev.yaml --set devDependencies.postgres.persistence.enabled=false
git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check
```

The integration script creates a uniquely named container with `--network none`,
no published ports, and temporary PGDATA. Its only host mount is the read-only
initdb SQL fixture from this worktree. It removes its container and temporary
files in `finally`. It exercises real psql/pg_dump/pg_restore, including a committed
write after snapshot export: the live count becomes 3 while the manifest and
restored dump both remain 2. It checks the actual snapshot argument, rejection
after the exporter ends, SQL failure after a successful dump, dump failure exit
47, absence of lingering transactions/status files, and production helper parsing.
For production-helper tests only, the transport is adapted from kubectl exec to
docker exec against the disposable container; PostgreSQL calls are real.

Validation completed on 2026-10-10: 26 Pester tests passed, independent PostgreSQL integration,
module loading via `platform.ps1 components`, Helm lint with persistence on/off,
and whitespace checks. No live database access, Radar run, deployment, or merge
was performed. Actual Kubernetes transport and a full platform deployment are
outside this validation scope.
