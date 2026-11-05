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

Preserved: canonical instrument identities, users, portfolios, transactions, authentication (portfolio-service
DB, untouched); all equity/stock tables and their immutable-history tables; `market_trading_schedules` and
`market_trading_calendar_exceptions` (NSE calendar, needed by history session logic); `flyway_schema_history`;
equity cycles and equity bars. Shared tables that may hold ETF-id rows (`research_documents`,
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
- ETF ids must not appear in equity-owned tables (ids must be ETF-exclusive).
- No orphan holdings positions.
- Post-conditions inside the transaction: preserved-table counts unchanged, ETF tables empty.
- **Dry-run by default**: the transaction rolls back unless `-v commit=yes`.

## Procedure (run from this directory)
1. Quiesce: no ETF cycle running; pause the manual/scheduled ETF trigger.
2. Backup (primary rollback): `scripts/database/backup.ps1` for the research database; record the file and checksum.
3. Export canonical ETF ids from portfolio-service to `./etf_ids.csv` (header `instrument_id`; assetType ETF, any status).
4. Inventory (read-only): `psql ... -f 00_inventory.sql` and `-f 50_shared_etf_rows_review.sql`; review every count, equity-overlap rows must be 0.
5. Scoped export (secondary rollback): `psql ... -v exportdir=/abs/path -f 10_export_rollback.sql`.
6. Dry-run: `psql ... -v expected_schema=research -v confirm=RESET_ETF_RADAR -f 20_reset.sql`
7. Apply (after approval): add `-v commit=yes`.
8. Verify: `psql ... -f 30_verify.sql` (all ETF counts 0, preserved counts unchanged, no orphans).
9. Start one normal full ETF cycle (separately approved). `/api/v1/etf-radar/current` returns 404
   `ETF_RADAR_CYCLE_NOT_FOUND` until it publishes; the UI must show its empty state.

`psql ...` = `psql -X -v ON_ERROR_STOP=1 -h HOST -U USER -d research_db` with `PGOPTIONS='-c search_path=research'`.

## Rollback
- Fast path (scoped): `psql -1 -v ON_ERROR_STOP=1 -v exportdir=/abs/path -f 40_restore.sql` restores exactly the
  exported rows in FK-safe order in one transaction. It fails on any key conflict, so if a new cycle has
  already written rows, re-run the reset first (or use the backup).
- Full path: restore the step-2 backup using the platform restore procedure.
- Nothing outside the listed tables is modified, so no other rollback is needed.

## Testing
`tests/run_reset_test.sh` (disposable DB only; name must contain `test`/`_t`/`disposable`) seeds ETF + equity +
calendar rows and checks: inventory, export, rejected bad-confirm and non-terminal-cycle runs, dry-run no-op,
apply, fingerprint-identical preserved data, restore is byte-identical. `ai/research-engine/tests/test_etf_reset_rebuild_postgres.py`
runs a full ETF cycle on PostgreSQL, applies this reset, and proves a second full cycle rebuilds identical state.
