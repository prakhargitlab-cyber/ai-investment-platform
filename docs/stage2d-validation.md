# Stage 2D: disposable backup/verify/restore lifecycle

Base commit: `9ff11a25759bf965fc828c68ac8af58729f55548`.
Worktree: `C:\workspace\ai-investment-platform-stage2d`.
Branch: `test/stage2d-lifecycle`.

## What runs

`scripts/database/tests/Validate-LifecycleIntegration.ps1` loads the actual
`backup.ps1`, `verify-backup.ps1`, and `restore.ps1` functions, plus their shared
production modules. It does not replace those functions, manifest validation,
checksums, snapshot generation, row counting, archive verification, maintenance
file handling, restore confirmation, or recovery helpers with mocks.

The test-only `LifecycleTransport.psm1` replaces the `kubectl` command with an
explicit adapter. Exec operations run real PostgreSQL 16 utilities in one
disposable container. Copy operations use real binary `docker cp`, and every
production transfer is independently checked against both local SHA-256 and
container `sha256sum`. A read-only git shim supplies the actual source checkout
commit while the production backup writes its manifest under a temporary root.
Prompt answers are queued explicitly; an unexpected prompt fails the test.

Kubernetes Deployment replica counts and pod status are simulated. However, the
writer fixture includes three real PostgreSQL sessions holding uncommitted INSERT
transactions. Scaling a fixture writer to zero terminates its sessions and rolls
back its writes. Wait and remaining-pod checks query the actual sessions; the
adapter independently rejects destructive restore if any writer remains. Resume
restarts the original number of sessions. This tests real database writer locks
and termination without deploying a Kubernetes cluster or application services.
It does not validate Kubernetes controller behavior, RBAC, or the native kubectl
binary's argument marshalling.

Failure injection is explicit and confined to the adapter: replica discovery,
scale/wait/remaining-pod and resume failures; an upload that completes before
reporting an error; a real pg_dump failure writing to a nonexistent directory;
a partial database mutation followed by a real pg_restore error; and actual
post-restore table removal or Flyway version alteration. No production safety
check is disabled to accommodate these scenarios.

## Isolation

The harness takes no cluster, database host, Docker context, image, volume, or
target-container parameter. It resolves Docker once and uses the explicit local
Windows Docker named pipe. A GUID container name and ownership label are created
for each run. Container inspection asserts `--network none`, no published ports,
and no bind mounts or persistent Docker volumes. PostgreSQL data lives on tmpfs.
The existing initdb SQL is copied into the stopped disposable container before
startup, without mounting any host directory.

During production-script execution, PATH is empty, KUBECONFIG points to an empty
temporary file, native kubectl is unavailable, and HTTP helpers throw. The adapter
rejects foreign namespaces/containers, paths outside its temporary root, unsafe
remote paths, and unrecognized Kubernetes commands. It never invokes Kubernetes.
These guards are themselves exercised by negative tests. Docker commands target
only the newly created container. Cleanup verifies its ownership label before
removal and checks the exact temporary path before recursive deletion.

This is a bounded test transport, not a security sandbox for executing arbitrary
untrusted PowerShell. Production code is loaded from this reviewed worktree.

## Production corrections covered by the harness

- Writer selectors now match the chart's `app.kubernetes.io/component` labels.
  The remaining-pod query uses supported chained
  [Pod field selectors](https://kubernetes.io/docs/concepts/overview/working-with-objects/field-selectors/)
  instead of a compound JSONPath predicate. Unreadable replica counts abort restore rather than being interpreted as absent
  writers. A retry cannot overwrite an existing maintenance recovery record.
- Restore uploads are inside the cleanup `try/finally`, including upload failure.
- A synchronized manifest with missing/invalid counts is rejected, rather than
  silently downgraded. Required PostgreSQL/Flyway metadata and the supported
  one-database format are checked. The checksum sidecar must match the manifest.
- Restored Flyway versions must match every recorded schema, in addition to the
  existing complete table/count comparison.
- The server-version probe explicitly uses the `postgres` database, supporting
  application database names that differ from the username.
- Before pg_restore, the existing revocation helper writes a negative restore
  record to `postgres.platform_restore.databases` and revokes any pending fresh
  token. It never creates a fresh-provisioning token. The negative record survives
  application restores and partial failures, including on installations without
  a provisioning table. Application restores into reserved PostgreSQL databases
  are rejected so the control state remains outside restore targets.
- Automatic first-ADMIN startup checks that restore record before constructing
  credentials or Jobs. A record skips automatic creation; an unreadable record
  fails closed. Manual administration remains a separate explicit operation.
- The Radar existence query's concatenated SQL is passed as one argument. The
  harness executes the real initial-cycle function and requires
  `SKIP_NO_FRESH_PROVISIONING_PROOF` after successful schema/count probes.

The last case extracts only the two bootstrap function definitions from the real
`platform.ps1` AST; it never executes platform command dispatch. Restored empty
user/cycle tables stay empty, no Job/workload or HTTP request occurs, the absent
fresh marker stays absent, and no local Radar success marker is fabricated.
The separate research scheduler is unchanged and is not run by this harness.

## Windows reproduction

Prerequisites: Windows PowerShell 5.1, Pester 5.7.1 (or compatible Pester 5), Docker
Desktop in Linux-container mode, and a locally available `postgres:16` image.
No Kubernetes credentials or running platform are required.

```powershell
Set-Location C:\workspace\ai-investment-platform-stage2d
Set-ExecutionPolicy -Scope Process Bypass -Force
Import-Module Pester -MinimumVersion 5.0
$pester = Invoke-Pester .\scripts\database\tests -Output Detailed -PassThru
if ($pester.FailedCount -gt 0) { throw 'Pester failed' }

# Always use a dedicated process; do not dot-source the lifecycle harness.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\database\tests\Validate-LifecycleIntegration.ps1
if ($LASTEXITCODE -ne 0) { throw 'Lifecycle integration failed' }

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\database\tests\Validate-SnapshotIntegration.ps1
if ($LASTEXITCODE -ne 0) { throw 'Snapshot regression failed' }

Get-Content .\.tmp\stage2d-lifecycle-results.json -Raw | ConvertFrom-Json |
    Select-Object -ExpandProperty Results | Format-Table Name, Status, Seconds -AutoSize
git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check
```

Every run writes its full case results, source SHA, transfer/restore counts,
transport event trace, failure detail, and container-cleanup result to
`.tmp/stage2d-lifecycle-results.json`. Dump archives and fixture data are deleted;
they are never committed. Expected rejection cases print production `FAIL` or
maintenance messages; the authoritative result is the case status and process
exit code. An unexpected failure produces a nonzero exit code and a failed case.

## Validation results

See the accompanying execution results in `.tmp/stage2d-lifecycle-results.json`.
The lifecycle suite covers these cases individually:

| Case | Expected result |
| --- | --- |
| Infrastructure and transport isolation | Foreign targets/commands denied; disposable infrastructure verified |
| Production backup | Binary payload/Unicode, manifest, hashes, Flyway and synchronized counts verified |
| Production archive verification | Valid backup accepted |
| Corrupted binary | SHA mismatch rejected before mutation |
| Corrupt archive with recomputed checksum | Real pg_restore rejects the archive |
| Incomplete manifest / checksum sidecar | Missing fields and inconsistent checksum rejected |
| Authorization and confirmation | Missing authorization, declined confirmation, conflicting flags and reserved target rejected |
| Configured-target restore | Real safety backup before overwrite; writers stopped, resumed and marker removed |
| Safety backup restore | Overwritten pre-restore row recovered in missing disposable target |
| Replica-query failure | No destructive action |
| Scale-down failure | No destructive action; recovery state retained |
| Wait failure | No destructive action; recovery state retained |
| Remaining-writer failure | No destructive action; recovery state retained |
| Safety-backup failure | Real pg_dump failure; no destructive restore; temporary artifacts cleaned |
| Interrupted upload | Restore denied; uploaded artifact cleaned; writers remain stopped |
| Partial restore failure | Real pg_restore failure; maintenance retained; deliberate repair succeeds |
| Missing restored table | Complete table-count validation rejects it |
| Flyway mismatch | Per-schema version validation rejects it |
| Resume failure | Restored data retained; recovery record preserved for explicit resume |
| Bootstrap after restore | No fresh-token creation/consumption, automatic ADMIN creation or initial Radar |

Failed-restore cases also verify that a retry cannot overwrite saved replica
counts and that explicit repair/recovery is possible. The original Stage 2C
snapshot integration remains a separate regression check for snapshot argument
substitution, concurrent commits, transaction lifetime, failure exit codes and
cleanup. Pester includes the original 26 checks plus four restore-ledger checks.
