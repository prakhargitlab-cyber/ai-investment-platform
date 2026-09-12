-- ITERATION 4 SAFE RESEARCH RESET
--
-- Preconditions:
--   1. The research-engine deployment no longer exposes legacy refresh,
--      backfill, portfolio-refresh, refresh-job, or prefetch routes.
--   2. No Kubernetes Job/CronJob or live request is performing research writes.
--   3. The verified Nifty/Yahoo historical coverage invariant is 481/481.
--
-- This deliberately uses ordered DELETE statements. It does not use TRUNCATE,
-- CASCADE, DROP SCHEMA, or any DELETE outside the research schema.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '10min';

-- A writer causes lock acquisition to fail and the transaction to roll back.
LOCK TABLE
    research.global_stock_rule_engine_results,
    research.research_event_sources,
    research.research_events,
    research.global_shareholding_snapshot_values,
    research.global_shareholding_snapshots,
    research.global_financial_facts,
    research.global_structured_market_snapshots,
    research.research_documents,
    research.research_refresh_jobs,
    research.research_refresh_runs
IN ACCESS EXCLUSIVE MODE;

LOCK TABLE
    research.flyway_schema_history_research,
    research.global_market_price_observations,
    research.irfc_financial_facts_backup_20260902,
    research.market_trading_calendar_exceptions,
    research.market_trading_schedules
IN SHARE MODE;

-- Prevent concurrent changes to all portfolio-owned identity, provider-mapping,
-- universe, holding, watchlist, broker, and user state while counts are checked.
DO $lock_portfolio$
DECLARE
    table_row record;
BEGIN
    FOR table_row IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'portfolio'
        ORDER BY tablename
    LOOP
        EXECUTE format('LOCK TABLE portfolio.%I IN SHARE MODE', table_row.tablename);
    END LOOP;
END
$lock_portfolio$;

CREATE TEMPORARY TABLE protected_table_counts (
    schema_name text NOT NULL,
    table_name text NOT NULL,
    row_count bigint NOT NULL,
    PRIMARY KEY (schema_name, table_name)
) ON COMMIT DROP;

INSERT INTO protected_table_counts (schema_name, table_name, row_count)
VALUES
    ('research', 'flyway_schema_history_research',
        (SELECT count(*) FROM research.flyway_schema_history_research)),
    ('research', 'global_market_price_observations',
        (SELECT count(*) FROM research.global_market_price_observations)),
    ('research', 'irfc_financial_facts_backup_20260902',
        (SELECT count(*) FROM research.irfc_financial_facts_backup_20260902)),
    ('research', 'market_trading_calendar_exceptions',
        (SELECT count(*) FROM research.market_trading_calendar_exceptions)),
    ('research', 'market_trading_schedules',
        (SELECT count(*) FROM research.market_trading_schedules));

DO $record_portfolio$
DECLARE
    table_row record;
    exact_count bigint;
BEGIN
    FOR table_row IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'portfolio'
        ORDER BY tablename
    LOOP
        EXECUTE format('SELECT count(*) FROM portfolio.%I', table_row.tablename)
            INTO exact_count;
        INSERT INTO protected_table_counts (schema_name, table_name, row_count)
        VALUES ('portfolio', table_row.tablename, exact_count);
    END LOOP;
END
$record_portfolio$;

DO $pre_reset_invariants$
DECLARE
    eligible_count bigint;
    covered_count bigint;
BEGIN
    SELECT count(*)
    INTO eligible_count
    FROM (
        SELECT DISTINCT universe.instrument_id
        FROM portfolio.nifty500_universe AS universe
        JOIN portfolio.instrument_provider_mappings AS mapping
          ON mapping.instrument_id = universe.instrument_id
        WHERE upper(mapping.provider) = 'YAHOO_FINANCE'
          AND upper(mapping.status) = 'VERIFIED'
    ) AS eligible;

    SELECT count(*)
    INTO covered_count
    FROM (
        SELECT DISTINCT universe.instrument_id
        FROM portfolio.nifty500_universe AS universe
        JOIN portfolio.instrument_provider_mappings AS mapping
          ON mapping.instrument_id = universe.instrument_id
        JOIN research.global_market_price_observations AS observation
          ON observation.instrument_id = universe.instrument_id
        WHERE upper(mapping.provider) = 'YAHOO_FINANCE'
          AND upper(mapping.status) = 'VERIFIED'
    ) AS covered;

    IF eligible_count <> 481 OR covered_count <> 481 THEN
        RAISE EXCEPTION
            'Reset aborted: verified India history coverage is %/% instead of 481/481',
            covered_count, eligible_count;
    END IF;
END
$pre_reset_invariants$;

-- Foreign-key children precede parents. Public facts, provider-derived
-- snapshots, run provenance, and the V1 result cache are intentionally rebuilt.
DELETE FROM research.global_stock_rule_engine_results;
DELETE FROM research.research_event_sources;
DELETE FROM research.global_shareholding_snapshot_values;
DELETE FROM research.global_shareholding_snapshots;
DELETE FROM research.research_events;
DELETE FROM research.global_financial_facts;
DELETE FROM research.global_structured_market_snapshots;
DELETE FROM research.research_documents;
DELETE FROM research.research_refresh_jobs;
DELETE FROM research.research_refresh_runs;

DO $post_reset_checks$
DECLARE
    protected_row record;
    exact_count bigint;
    eligible_count bigint;
    covered_count bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM research.global_stock_rule_engine_results)
       OR EXISTS (SELECT 1 FROM research.research_event_sources)
       OR EXISTS (SELECT 1 FROM research.global_shareholding_snapshot_values)
       OR EXISTS (SELECT 1 FROM research.global_shareholding_snapshots)
       OR EXISTS (SELECT 1 FROM research.research_events)
       OR EXISTS (SELECT 1 FROM research.global_financial_facts)
       OR EXISTS (SELECT 1 FROM research.global_structured_market_snapshots)
       OR EXISTS (SELECT 1 FROM research.research_documents)
       OR EXISTS (SELECT 1 FROM research.research_refresh_jobs)
       OR EXISTS (SELECT 1 FROM research.research_refresh_runs) THEN
        RAISE EXCEPTION 'Reset aborted: a rebuildable table is not empty';
    END IF;

    FOR protected_row IN
        SELECT schema_name, table_name, row_count
        FROM protected_table_counts
        ORDER BY schema_name, table_name
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %I.%I',
            protected_row.schema_name,
            protected_row.table_name
        ) INTO exact_count;
        IF exact_count <> protected_row.row_count THEN
            RAISE EXCEPTION
                'Reset aborted: protected table %.% changed from % to % rows',
                protected_row.schema_name,
                protected_row.table_name,
                protected_row.row_count,
                exact_count;
        END IF;
    END LOOP;

    SELECT count(*)
    INTO eligible_count
    FROM (
        SELECT DISTINCT universe.instrument_id
        FROM portfolio.nifty500_universe AS universe
        JOIN portfolio.instrument_provider_mappings AS mapping
          ON mapping.instrument_id = universe.instrument_id
        WHERE upper(mapping.provider) = 'YAHOO_FINANCE'
          AND upper(mapping.status) = 'VERIFIED'
    ) AS eligible;

    SELECT count(*)
    INTO covered_count
    FROM (
        SELECT DISTINCT universe.instrument_id
        FROM portfolio.nifty500_universe AS universe
        JOIN portfolio.instrument_provider_mappings AS mapping
          ON mapping.instrument_id = universe.instrument_id
        JOIN research.global_market_price_observations AS observation
          ON observation.instrument_id = universe.instrument_id
        WHERE upper(mapping.provider) = 'YAHOO_FINANCE'
          AND upper(mapping.status) = 'VERIFIED'
    ) AS covered;

    IF eligible_count <> 481 OR covered_count <> 481 THEN
        RAISE EXCEPTION
            'Reset aborted: post-reset India history coverage is %/% instead of 481/481',
            covered_count, eligible_count;
    END IF;
END
$post_reset_checks$;

SELECT schema_name, table_name, row_count
FROM protected_table_counts
ORDER BY schema_name, table_name;

COMMIT;
