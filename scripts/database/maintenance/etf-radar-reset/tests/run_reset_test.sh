#!/usr/bin/env bash
# Synthetic disposable-PostgreSQL regression only. No application/ETF cycle.
# Root creates the isolated DB/container; this harness never launches services.
# Example inside a network-disabled container with /source read-only:
# PGDATABASE=etf_reset_disposable_test ETF_RESET_SOURCE_DIR=/source bash /source/tests/run_reset_test.sh
set -euo pipefail
if [[ "${PGDATABASE:-}" != etf_reset_disposable_test ]]; then
  echo 'REFUSED: PGDATABASE must be exactly etf_reset_disposable_test' >&2
  exit 2
fi
SOURCE_DIR=${ETF_RESET_SOURCE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}
WORK_DIR=$(mktemp -d -t etf-reset-regression.XXXXXXXX)
PASSED=0
FAILED=0
cleanup() {
  local rc=$?
  if (( rc != 0 && FAILED == 0 )); then FAILED=1; fi
  printf 'TEST_SUMMARY passed=%d failed=%d skipped=0\n' "$PASSED" "$FAILED"
  rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT
# Copy only local operator scripts/fixtures; preserve the read-only source mount.
# Removing CR at line endings also supports Windows worktrees without a rebuild.
for source_file in "$SOURCE_DIR"/*.sql; do
  sed 's/\r$//' "$source_file" > "$WORK_DIR/$(basename -- "$source_file")"
done
for fixture in bootstrap.sql seed.sql fingerprint.sql; do
  sed 's/\r$//' "$SOURCE_DIR/tests/$fixture" > "$WORK_DIR/$fixture"
done
sed 's/\r$//' "$SOURCE_DIR/tests/etf_ids.sample.csv" > "$WORK_DIR/etf_ids.csv"
cd -- "$WORK_DIR"
Q=(psql -X -q -v ON_ERROR_STOP=1 -At)
RESET=(-v expected_schema=research -v confirm=RESET_ETF_RADAR)
GUARD_TABLES=(
  global_financial_facts:instrument_id
  global_shareholding_snapshots:instrument_id
  global_stock_rule_engine_results:global_instrument_id
  research_events:instrument_id
  research_refresh_runs:instrument_id
  research_news_search_runs:instrument_id
  research_event_impact_features:instrument_id
  company_business_exposure_profiles:instrument_id
  global_opportunity_snapshot:global_instrument_id
  global_stock_suggestion:global_instrument_id
  global_stock_suggestion_current:global_instrument_id
  global_stock_suggestion_history:global_instrument_id
  stock_recommendation_history:global_instrument_id
  recommendation_current_state:global_instrument_id
)
pass() { PASSED=$((PASSED + 1)); printf 'PASS %s\n' "$1"; }
fail() {
  FAILED=$((FAILED + 1))
  printf 'FAIL %s\n' "$1" >&2
  if [[ -s command.err ]]; then cat command.err >&2; fi
  exit 1
}
equal() { [[ "$2" == "$3" ]] || fail "$1"; pass "$1"; }
sql() { "${Q[@]}" -c "$1"; }
fingerprint() { "${Q[@]}" -f fingerprint.sql; }
preserved_fingerprint() { "${Q[@]}" -v preserved_only=true -f fingerprint.sql; }
run_ok() {
  local label=$1; shift
  "$@" >command.out 2>command.err || fail "$label"
  pass "$label"
}
reject_unchanged() {
  local label=$1 reason=$2 before; shift 2
  before=$(fingerprint)
  if "${Q[@]}" "$@" >command.out 2>command.err; then fail "$label unexpectedly succeeded"; fi
  grep -F -- "$reason" command.err >/dev/null || fail "$label failed for an unexpected reason"
  equal "$label: no permanent row/table changes" "$before" "$(fingerprint)"
}
flyway_state() {
  local default_present=$1 configured_present=$2
  "${Q[@]}" -f _flyway.sql -c "SELECT count(*)=2
    AND count(DISTINCT table_name)=2
    AND bool_and(CASE table_name
      WHEN 'flyway_schema_history' THEN present=$default_present AND
        (CASE WHEN present THEN row_count=2 ELSE row_count IS NULL OR row_count=0 END)
      WHEN 'flyway_schema_history_research' THEN present=$configured_present AND
        (CASE WHEN present THEN row_count=2 ELSE row_count IS NULL OR row_count=0 END)
      ELSE false END) FROM etf_flyway_state;"
}
create_flyway_fixture() {
  # Fixed names only, in an already verified fresh synthetic test schema.
  sql "CREATE TABLE research.$1(installed_rank integer PRIMARY KEY,version text,checksum integer,success boolean,description text);
       INSERT INTO research.$1 VALUES (0,NULL,NULL,true,'synthetic schema'),(1,'1',123,true,'synthetic migration');" >/dev/null
}
apply_roundtrip() {
  local mode=$1 default_present=$2 configured_present=$3
  local before preserved exports after conflict_before
  before=$(fingerprint)
  preserved=$(preserved_fingerprint)
  equal "$mode Flyway state reports exact known relations" t "$(flyway_state "$default_present" "$configured_present")"
  run_ok "$mode inventory succeeds" "${Q[@]}" -f 00_inventory.sql
  equal "$mode inventory is read-only" "$before" "$(fingerprint)"
  run_ok "$mode shared-row review succeeds" "${Q[@]}" -f 50_shared_etf_rows_review.sql
  equal "$mode shared-row review is read-only" "$before" "$(fingerprint)"
  exports="$WORK_DIR/export-$mode"
  mkdir -- "$exports"
  run_ok "$mode scoped rollback export succeeds" "${Q[@]}" -v "exportdir=$exports" -f 10_export_rollback.sql
  equal "$mode export contains every deleted table" 11 "$(find "$exports" -maxdepth 1 -type f -name '*.csv' | wc -l | tr -d ' ')"
  equal "$mode export is read-only" "$before" "$(fingerprint)"
  run_ok "$mode default dry run succeeds" "${Q[@]}" "${RESET[@]}" -f 20_reset.sql
  equal "$mode default dry run changes nothing" "$before" "$(fingerprint)"
  run_ok "$mode commit other than exact yes remains dry run" "${Q[@]}" "${RESET[@]}" -v commit=YES -f 20_reset.sql
  equal "$mode non-yes commit changes nothing" "$before" "$(fingerprint)"
  run_ok "$mode committed reset succeeds" "${Q[@]}" "${RESET[@]}" -v commit=yes -f 20_reset.sql
  equal "$mode all ETF-only tables empty" 0 "$(sql "SELECT
    (SELECT count(*) FROM research.etf_holdings_positions)+(SELECT count(*) FROM research.etf_holdings_snapshots)+
    (SELECT count(*) FROM research.etf_fact_observations)+(SELECT count(*) FROM research.etf_nav_observations)+
    (SELECT count(*) FROM research.etf_listing_observations)+(SELECT count(*) FROM research.etf_acquisition_attempts)+
    (SELECT count(*) FROM research.etf_radar_cycles);")"
  equal "$mode only equity bars remain including research-only-ID removal" 1 "$(sql "SELECT count(*) FROM research.global_daily_market_bars WHERE global_instrument_id='99999999-9999-9999-9999-999999999999';")"
  equal "$mode no ETF bars remain" 0 "$(sql "SELECT count(*) FROM research.global_daily_market_bars WHERE global_instrument_id<>'99999999-9999-9999-9999-999999999999';")"
  equal "$mode only equity lifecycle rows remain" '1|1|1' "$(sql "SELECT
    (SELECT count(*) FROM research.global_opportunity_cycle_run),
    (SELECT count(*) FROM research.global_opportunity_cycle_progress),
    (SELECT count(*) FROM research.global_opportunity_cycle_active);")"
  equal "$mode all preserved rows are identical" "$preserved" "$(preserved_fingerprint)"
  after=$(fingerprint)
  run_ok "$mode verification succeeds" "${Q[@]}" -f 30_verify.sql
  equal "$mode verification is read-only" "$after" "$(fingerprint)"
  equal "$mode Flyway presence and count survive reset" t "$(flyway_state "$default_present" "$configured_present")"
  if [[ "$mode" == absent ]]; then
    # This key conflicts in the LAST COPY, after all lifecycle/evidence COPYs.
    sql "INSERT INTO research.global_daily_market_bars VALUES
      ('11111111-1111-1111-1111-111111111111','2026-10-30',NULL,NULL,NULL,999,NULL,NULL,NULL,
       'INR','NSE','CONFLICT','DEMO','https://synthetic.invalid/conflict','2026-11-02');" >/dev/null
    conflict_before=$(fingerprint)
    reject_unchanged 'late restore conflict rolls back every earlier COPY' 'duplicate key value violates unique constraint' -1 -v "exportdir=$exports" -f 40_restore.sql
    equal 'late restore conflict retains original conflicting row values' "$conflict_before" "$(fingerprint)"
    sql "DELETE FROM research.global_daily_market_bars WHERE global_instrument_id='11111111-1111-1111-1111-111111111111';" >/dev/null
  fi
  run_ok "$mode rollback restore succeeds" "${Q[@]}" -1 -v "exportdir=$exports" -f 40_restore.sql
  equal "$mode restore reproduces every complete row" "$before" "$(fingerprint)"
  equal "$mode restore preserves all unrelated rows" "$preserved" "$(preserved_fingerprint)"
}

equal 'connection is the exact disposable database' etf_reset_disposable_test "$(sql 'SELECT current_database();')"
run_ok 'bootstrap refuses existing schemas and creates only synthetic fixtures' "${Q[@]}" -f bootstrap.sql
run_ok 'synthetic fixture seed succeeds' "${Q[@]}" -f seed.sql
scope=$("${Q[@]}" -f _scope.sql -c "SELECT count(*)=3 AND count(DISTINCT instrument_id)=3
  AND count(*) FILTER (WHERE instrument_id IS NULL OR instrument_id ~ '^[[:space:]]*$')=0
  AND bool_and(instrument_id=lower(btrim(instrument_id)))
  AND count(*) FILTER (WHERE instrument_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')=1
  FROM etf_scope;")
equal 'scope filters NULL/blank, deduplicates and normalizes all branches' t "$scope"
equal 'scope has two canonical IDs plus one research-only identity' 2 "$(sql "SELECT count(DISTINCT lower(btrim(instrument_id))) FROM research.etf_fact_observations WHERE lower(btrim(instrument_id))<>'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';")"
equal 'both metadata tables genuinely absent before any operator command' t "$(sql "SELECT to_regclass('research.flyway_schema_history') IS NULL AND to_regclass('research.flyway_schema_history_research') IS NULL;")"

reject_unchanged 'wrong confirmation guard' 'confirm' -v expected_schema=research -v confirm=nope -v commit=yes -f 20_reset.sql
reject_unchanged 'missing confirmation guard' 'confirm' -v expected_schema=research -v commit=yes -f 20_reset.sql
reject_unchanged 'wrong schema guard' 'expected_schema' -v expected_schema=public -v confirm=RESET_ETF_RADAR -v commit=yes -f 20_reset.sql
reject_unchanged 'missing schema guard' 'expected_schema' -v confirm=RESET_ETF_RADAR -v commit=yes -f 20_reset.sql
for status in ACCEPTED RUNNING PUBLISHED CANCEL_REQUESTED; do
  sql "UPDATE research.global_opportunity_cycle_run SET status='$status' WHERE cycle_id='c-etf-1';" >/dev/null
  reject_unchanged "non-terminal $status guard" 'non-terminal' "${RESET[@]}" -v commit=yes -f 20_reset.sql
  sql "UPDATE research.global_opportunity_cycle_run SET status='COMPLETED' WHERE cycle_id='c-etf-1';" >/dev/null
done
sql "UPDATE research.global_opportunity_cycle_active SET cycle_id='c-etf-1' WHERE market='IN';" >/dev/null
reject_unchanged 'non-ETF active pointer guard' 'non-ETF active pointer' "${RESET[@]}" -v commit=yes -f 20_reset.sql
sql "UPDATE research.global_opportunity_cycle_active SET cycle_id='c-eq-1' WHERE market='IN';" >/dev/null
for entry in "${GUARD_TABLES[@]}"; do
  table=${entry%:*}; column=${entry#*:}
  sql "INSERT INTO research.$table(row_key,$column,payload)
       VALUES ('overlap','11111111-1111-1111-1111-111111111111','{\"synthetic\":true,\"overlap\":true}');" >/dev/null
  reject_unchanged "equity overlap guard $table" 'equity-owned' "${RESET[@]}" -v commit=yes -f 20_reset.sql
  sql "DELETE FROM research.$table WHERE row_key='overlap';" >/dev/null
done
equal 'all fourteen overlap guard tables exercised' 14 "${#GUARD_TABLES[@]}"

apply_roundtrip absent false false
create_flyway_fixture flyway_schema_history
apply_roundtrip default_only true false
sql 'DROP TABLE research.flyway_schema_history;' >/dev/null
create_flyway_fixture flyway_schema_history_research
apply_roundtrip configured_only false true
create_flyway_fixture flyway_schema_history
apply_roundtrip both true true
printf 'ALL FOCUSED RESET/RESTORE CHECKS PASSED\n'
