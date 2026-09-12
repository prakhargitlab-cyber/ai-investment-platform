# Local PostgreSQL Backup And Restore

DEV PostgreSQL runs in Kubernetes and stores data on the `postgres-data-dev` PVC. Do not back up or restore by copying container filesystem data while Postgres is running; use logical backups.

Create a timestamped custom-format backup:

```powershell
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force -Path ".tmp\backups" | Out-Null
kubectl -n ai-investment exec deploy/postgres -- pg_dump -U investment -d investment -Fc > ".tmp\backups\investment-$timestamp.dump"
```

Capture globals when role-level metadata matters:

```powershell
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
kubectl -n ai-investment exec deploy/postgres -- pg_dumpall -U investment --globals-only > ".tmp\backups\investment-globals-$timestamp.sql"
```

Restore into an initialized DEV database:

```powershell
Get-Content ".tmp\backups\investment-YYYYMMDD-HHMMSS.dump" -AsByteStream | kubectl -n ai-investment exec -i deploy/postgres -- pg_restore -U investment -d investment --clean --if-exists --no-owner --no-privileges
```

Retrieve the database password from the Kubernetes Secret only for commands that require an explicit password. Do not commit passwords or generated dumps.
