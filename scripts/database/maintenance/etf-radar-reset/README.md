# ETF Radar data reset (operator procedure)

Removes **only ETF Radar-owned research data** from the `research` schema so a fresh, normal full ETF Radar
cycle can rebuild everything. This is an operator script, deliberately **not** a Flyway migration (a migration
would auto-run in every environment). It changes no schema, constraint, `flyway_schema_history` or any other
service's database. **Do not run without explicit approval.**

## What is deleted / preserved

| Deleted (ETF-owned) | Rule |
|---|---|
| `etf_holdings_positions`, `etf_holdings_snapshots`, `etf_fact_observations`, `etf_nav_observations`, `etf_listing_observations`, `etf_acquisition_attempts`, `etf_radar_cycles` | all rows (ETF-only tables) |
| `global_opportunity_cycle_progress` / `_active` / `_run` | only rows of cycles with `market='ETF'` (and the ETF active pointer) |
| `global_daily_market_bars` | only rows whose `global_instrument_id` is in the ETF id set |

ETF id set = canonical ETF ids exported from portfolio-service (`etf_ids.csv`) ∪ ids referenced by the ETF tables.
Every branch is trimmed and lowercased before deduplication; NULL and whitespace-only identifiers are excluded.
The same normalization is used for inventory, overlap guards, scoped export and deletion.

Preserved: canonical instrument identities, users, portfolios, transactions, authentication (portfolio-service
DB, untouched); all equity/stock tables and their immutable-history tables; `market_trading_schedules` and
`market_trading_calendar_exceptions` (NSE calendar, needed by history session logic); `flyway_schema_history`;
equity cycles and equity bars. Both known Flyway names (`research.flyway_schema_history` and
`research.flyway_schema_history_research`) are reported separately as present or absent. Existing metadata is
checked by relation identity, row count and full-row hash inside the reset transaction. Missing metadata
remains missing: these scripts never bootstrap or repair it, and its absence does not certify schema version.
Shared tables that may hold ETF-id rows (`research_documents`,
`global_manual_evidence`, `global_market_price_observations`, `global_structured_market_snapshots`,
`research_acquisition_observations`) are **reported by the inventory but not deleted**; review the counts and
decide separately (no ETF code path currently writes manual evidence/price observations).
`50_shared_etf_rows_review.sql` (read-only) shows, per table, the ETF-id row counts and, for `research_documents`, any dependents
(`research_events`, `research_event_sources`, `global_shareholding_snapshots`, immutable `research_event_impact_features`).
Findings: the normal ETF cycle never writes these five tables (ETF cycle code writes only `etf_*` tables and
`global_daily_market_bars`); ETF rows can only come from operator-driven manual-evidence/official-document ingestion, none of which
runs by default. Rows are keyed by instrument id with no ETF-specific ownership marker, `research_documents` rows may be shared by
content hash and referenced by equity event tables, so they are preserved. Delete such rows only after the review script shows zero
dependents, with a separate approval.
No views, materialized views or DB-side caches exist in the schema. Run-time caches are per-run objects only.

## Guards (any failure aborts and rolls back)
- `confirm=RESET_ETF_RADAR` and schema `research`.
- No ETF cycle in `ACCEPTED/RUNNING/PUBLISHED/CANCEL_REQUESTED` (cancel or let finish first; stop the ETF worker).
- No non-ETF active pointer referencing an ETF cycle.
- ETF ids must not appear in any of the **14** equity-owned tables reported by inventory (ids must be ETF-exclusive).
- No orphan holdings positions.
- Post-conditions inside the transaction: all 14 Equity-owned tables, all five shared evidence tables,
  calendar reference rows and non-ETF lifecycle/bar counts remain unchanged; ETF tables/lifecycle rows are empty.
  Historical bars are partitioned explicitly before deletion (`total = ETF-owned + preserved`), then the
  remaining total must equal the preserved count and there must be zero ETF-scoped bars. Scope complements
  use `NOT EXISTS`, so nullable identifiers cannot poison preservation counts.
- **Dry-run by default**: the transaction rolls back unless `-v commit=yes`.

## Procedure (run from this directory)
1. Quiesce all ETF writers: pause manual/scheduled cycles, bounded acquisition, history onboarding and
   operator evidence ingestion. Keep them stopped through export, reset and verification. The scoped
   export uses separate statement snapshots and requires this quiescence for a consistent rollback set.
2. Backup (primary rollback): `scripts/database/backup.ps1` for the research database; record the file and checksum.
3. Export canonical ETF ids from portfolio-service to `./etf_ids.csv` (header `instrument_id`; assetType ETF, any status).
4. Inventory (read-only): `psql ... -f 00_inventory.sql` and `-f 50_shared_etf_rows_review.sql`; review every count, equity-overlap rows must be 0.
5. Scoped export (secondary rollback): `psql ... -v exportdir=/abs/path -f 10_export_rollback.sql`.
6. Dry-run: `psql ... -v expected_schema=research -v confirm=RESET_ETF_RADAR -f 20_reset.sql`
7. Apply (after approval): add `-v commit=yes`.
8. Verify from this same directory with the original canonical CSV: `psql ... -f 30_verify.sql`.
   It fails if ETF tables, ETF lifecycle rows, ETF-scoped bars or orphan lifecycle rows remain.
   Compare the reported preserved counts with inventory; standalone verification cannot reconstruct a
   pre-reset baseline after the fact. After ETF tables are emptied, its ID scope can only reconstruct the
   canonical CSV, so it cannot independently detect leftover bars for research-only IDs outside that CSV.
   The reset verifies the complete frozen union and preservation counts inside its own transaction.
9. Start one normal full ETF cycle (separately approved). `/api/v1/etf-radar/current` returns 404
   `ETF_RADAR_CYCLE_NOT_FOUND` until it publishes; the UI must show its empty state.

`psql ...` = `psql -X -v ON_ERROR_STOP=1 -h HOST -U USER -d investment` with `PGOPTIONS='-c search_path=research'`.

## Rollback
- Fast path (scoped): `psql -1 -v ON_ERROR_STOP=1 -v exportdir=/abs/path -f 40_restore.sql` restores exactly the
  exported rows in FK-safe order in one transaction. It fails on any key conflict, so if a new cycle has
  already written rows, re-run the reset first (or use the backup).
- Full path: restore the step-2 backup using the platform restore procedure.
- Nothing outside the listed tables is modified, so no other rollback is needed.

## Testing
`tests/run_reset_test.sh` accepts only the database name `etf_reset_disposable_test`, and refuses to
bootstrap an existing `research` schema. It uses synthetic tables and rows entirely within that disposable
database; it never runs an ETF/Equity cycle or application test suite. Fixtures cover NULL/blank identifiers,
normalized research-only IDs, mixed ETF/Equity bars, every Equity overlap guard, all five preserved shared
tables, missing/present Flyway names, confirmation/schema/active-cycle guards, default rollback, explicit
commit, scoped restoration and a late-conflict restore rollback. Comparisons fingerprint complete rows in
every research table, including payloads, NAV, prices, volumes and calendar exceptions.

Verified on 2026-11-06 with disposable PostgreSQL 16: **128 passed, 0 failed, 0 skipped** in 6.73 seconds.
For each of the four Flyway-presence cases, default dry-run preserved the complete database fingerprint,
explicit commit removed the three synthetic ETF bars while retaining the Equity bar and unrelated rows,
and scoped restore reproduced every original row. A conflicting final restore COPY rolled back all prior
COPYs. This is synthetic fixture evidence, not a production reset or a restore of the production backup.

Example focused test launch from this directory (Docker only; no host database connection or exposed port):

```powershell
$testContainer = 'etf-reset-test-' + [guid]::NewGuid().ToString('N')
$testSource = (Get-Location).Path
try {
  docker run --detach --rm --pull=never --name $testContainer --network none `
    --tmpfs /var/lib/postgresql/data:rw --env POSTGRES_HOST_AUTH_METHOD=trust `
    --env POSTGRES_DB=etf_reset_disposable_test `
    --mount "type=bind,source=$testSource,target=/source,readonly" postgres:16-alpine
  if ($LASTEXITCODE -ne 0) { throw 'DISPOSABLE_POSTGRES_START_FAILED' }
  $ready = $false
  for ($attempt=0; $attempt -lt 30; $attempt++) {
    docker exec $testContainer pg_isready -U postgres -d etf_reset_disposable_test *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Milliseconds 500
  }
  if (-not $ready) { throw 'DISPOSABLE_POSTGRES_NOT_READY' }
  docker exec --env PGUSER=postgres --env PGDATABASE=etf_reset_disposable_test `
    --env ETF_RESET_SOURCE_DIR=/source $testContainer bash -o pipefail -c `
    "sed 's/\r$//' /source/tests/run_reset_test.sh | bash"
  if ($LASTEXITCODE -ne 0) { throw 'FOCUSED_RESET_TEST_FAILED' }
} finally {
  docker rm --force $testContainer | Out-Null
}
```

The container has no network and no production credentials. The fixture metadata tables exist only in this
throwaway database; the operator scripts do not create them. Source files are mounted read-only and the
harness copies them into its own temporary work directory, normalizing CRLF there. A successful synthetic
scoped restoration does **not** establish that an existing production backup has been test-restored.
