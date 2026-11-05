#!/usr/bin/env bash
# Disposable-DB harness. NEVER point at a real database. Requires a throwaway PG with the research schema migrated.
# usage: PGHOST=.. PGPORT=.. PGUSER=.. PGDATABASE=<disposable> tests/run_reset_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."
case "${PGDATABASE:?}" in *test*|*_t|*disposable*) ;; *) echo "refusing: database name must look disposable"; exit 2;; esac
Q="psql -X -q -v ON_ERROR_STOP=1 -At"
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT
FP="select 'cal',count(*),md5(string_agg(t::text,'|' order by t::text)) from research.market_trading_schedules t union all
 select 'flyway',count(*),md5(string_agg(t::text,'|' order by t::text)) from research.flyway_schema_history t union all
 select 'eq_bars',count(*),md5(string_agg(t::text,'|' order by t::text)) from research.global_daily_market_bars t where global_instrument_id::text like '9999%' union all
 select 'eq_runs',count(*),md5(string_agg(t::text,'|' order by t::text)) from research.global_opportunity_cycle_run t where market<>'ETF' union all
 select 'eq_prog',count(*),md5(string_agg(t::text,'|' order by t::text)) from research.global_opportunity_cycle_progress t where cycle_id='c-eq-1' union all
 select 'eq_active',count(*),md5(string_agg(t::text,'|' order by t::text)) from research.global_opportunity_cycle_active t where market<>'ETF'"
ALL="select count(*)||'/'||md5(string_agg(t::text,'|' order by t::text)) from (
 select evidence_id::text||payload::text t from research.etf_fact_observations union all select evidence_id::text from research.etf_nav_observations
 union all select evidence_id::text from research.etf_holdings_snapshots union all select snapshot_id||position_number::text from research.etf_holdings_positions
 union all select evidence_id::text from research.etf_listing_observations union all select attempt_id::text from research.etf_acquisition_attempts
 union all select cycle_id::text from research.etf_radar_cycles union all select cycle_id::text from research.global_opportunity_cycle_run
 union all select cycle_id||phase||global_instrument_id from research.global_opportunity_cycle_progress union all select market||cycle_id from research.global_opportunity_cycle_active
 union all select global_instrument_id::text||trading_date::text from research.global_daily_market_bars) x(t)"
# Disposable DBs migrated by plain psql lack Flyway's history table: create a stand-in so the preserved-table checks run.
$Q -c "CREATE TABLE IF NOT EXISTS research.flyway_schema_history(installed_rank int, version text); INSERT INTO research.flyway_schema_history SELECT 1,'1' WHERE NOT EXISTS (SELECT 1 FROM research.flyway_schema_history)"
# clean slate
$Q -c "TRUNCATE research.etf_holdings_positions,research.etf_holdings_snapshots,research.etf_fact_observations,research.etf_nav_observations,research.etf_listing_observations,research.etf_acquisition_attempts,research.etf_radar_cycles,research.global_opportunity_cycle_active,research.global_opportunity_cycle_progress,research.global_opportunity_cycle_run,research.global_daily_market_bars"
$Q -f tests/seed.sql
cp tests/etf_ids.sample.csv ./etf_ids.csv; trap 'rm -rf "$OUT" ./etf_ids.csv' EXIT
BEFORE_ALL=$($Q -c "$ALL"); BEFORE_FP=$($Q -c "$FP")
echo "1) inventory"; $Q -f 00_inventory.sql | tail -8
echo "1b) shared-row review (read-only)"; $Q -f 50_shared_etf_rows_review.sql | tail -6
echo "2) export"; $Q -v exportdir=$OUT -f 10_export_rollback.sql; ls $OUT | wc -l
echo "3) guard: wrong confirm must fail"
if $Q -v expected_schema=research -v confirm=nope -v commit=yes -f 20_reset.sql >/dev/null 2>&1; then echo FAIL; exit 1; else echo "  rejected (ok)"; fi
[ "$($Q -c "$ALL")" = "$BEFORE_ALL" ] || { echo "FAIL: data changed after rejected run"; exit 1; }
echo "4) guard: non-terminal ETF cycle must fail"
$Q -c "UPDATE research.global_opportunity_cycle_run SET status='RUNNING' WHERE cycle_id='c-etf-1'"
if $Q -v expected_schema=research -v confirm=RESET_ETF_RADAR -v commit=yes -f 20_reset.sql >/dev/null 2>&1; then echo FAIL; exit 1; else echo "  rejected (ok)"; fi
$Q -c "UPDATE research.global_opportunity_cycle_run SET status='COMPLETED' WHERE cycle_id='c-etf-1'"
echo "5) dry run must change nothing"
$Q -v expected_schema=research -v confirm=RESET_ETF_RADAR -f 20_reset.sql | tail -2
[ "$($Q -c "$ALL")" = "$BEFORE_ALL" ] || { echo "FAIL: dry run changed data"; exit 1; }
echo "6) apply"
$Q -v expected_schema=research -v confirm=RESET_ETF_RADAR -v commit=yes -f 20_reset.sql | tail -2
$Q -f 30_verify.sql
[ "$($Q -c "$FP")" = "$BEFORE_FP" ] || { echo "FAIL: preserved data changed"; exit 1; }
echo "  preserved fingerprints identical"
echo "7) restore"; $Q -1 -v exportdir=$OUT -f 40_restore.sql
[ "$($Q -c "$ALL")" = "$BEFORE_ALL" ] || { echo "FAIL: restore not identical"; exit 1; }
[ "$($Q -c "$FP")" = "$BEFORE_FP" ] || { echo "FAIL: preserved differ post restore"; exit 1; }
echo "ALL RESET/RESTORE CHECKS PASSED"
