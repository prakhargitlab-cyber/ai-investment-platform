-- GENERATED RESET PLAN ONLY. This file was not executed in Iteration 1.
-- Run only after an operator has taken a backup, stopped research writers, and
-- reviewed the live schema inventory in the companion architecture document.
-- This uses DELETE so foreign-key mistakes or future schema dependencies fail
-- the transaction. It deliberately does not use TRUNCATE or CASCADE.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- Fail rather than race a live writer. Protected tables are share-locked so the
-- before/after count check remains meaningful until this transaction commits.
LOCK TABLE
    research.research_event_sources,
    research.research_events,
    research.global_shareholding_snapshot_values,
    research.global_shareholding_snapshots,
    research.global_financial_facts,
    research.research_documents,
    research.research_refresh_jobs,
    research.research_refresh_runs
IN ACCESS EXCLUSIVE MODE;

LOCK TABLE
    research.flyway_schema_history_research,
    research.global_market_price_observations,
    research.global_structured_market_snapshots,
    research.irfc_financial_facts_backup_20260902,
    research.market_trading_calendar_exceptions,
    research.market_trading_schedules
IN SHARE MODE;

-- Record the protected table counts so this transaction can prove they did not
-- change while rebuildable research data was cleared.
CREATE TEMPORARY TABLE protected_research_table_counts (
    table_name TEXT PRIMARY KEY,
    row_count BIGINT NOT NULL
) ON COMMIT DROP;

INSERT INTO protected_research_table_counts (table_name, row_count)
VALUES
    ('flyway_schema_history_research', (SELECT count(*) FROM research.flyway_schema_history_research)),
    ('global_market_price_observations', (SELECT count(*) FROM research.global_market_price_observations)),
    ('global_structured_market_snapshots', (SELECT count(*) FROM research.global_structured_market_snapshots)),
    ('irfc_financial_facts_backup_20260902', (SELECT count(*) FROM research.irfc_financial_facts_backup_20260902)),
    ('market_trading_calendar_exceptions', (SELECT count(*) FROM research.market_trading_calendar_exceptions)),
    ('market_trading_schedules', (SELECT count(*) FROM research.market_trading_schedules));

-- Dependency order: evidence links and child values precede their parent rows.
DELETE FROM research.research_event_sources;
DELETE FROM research.research_events;
DELETE FROM research.global_shareholding_snapshot_values;
DELETE FROM research.global_shareholding_snapshots;
DELETE FROM research.global_financial_facts;
DELETE FROM research.research_documents;
DELETE FROM research.research_refresh_jobs;
DELETE FROM research.research_refresh_runs;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM research.research_event_sources)
       OR EXISTS (SELECT 1 FROM research.research_events)
       OR EXISTS (SELECT 1 FROM research.global_shareholding_snapshot_values)
       OR EXISTS (SELECT 1 FROM research.global_shareholding_snapshots)
       OR EXISTS (SELECT 1 FROM research.global_financial_facts)
       OR EXISTS (SELECT 1 FROM research.research_documents)
       OR EXISTS (SELECT 1 FROM research.research_refresh_jobs)
       OR EXISTS (SELECT 1 FROM research.research_refresh_runs) THEN
        RAISE EXCEPTION 'Rebuildable research reset did not reach an empty state';
    END IF;

    IF (SELECT row_count FROM protected_research_table_counts WHERE table_name = 'flyway_schema_history_research')
           <> (SELECT count(*) FROM research.flyway_schema_history_research)
       OR (SELECT row_count FROM protected_research_table_counts WHERE table_name = 'global_market_price_observations')
           <> (SELECT count(*) FROM research.global_market_price_observations)
       OR (SELECT row_count FROM protected_research_table_counts WHERE table_name = 'global_structured_market_snapshots')
           <> (SELECT count(*) FROM research.global_structured_market_snapshots)
       OR (SELECT row_count FROM protected_research_table_counts WHERE table_name = 'irfc_financial_facts_backup_20260902')
           <> (SELECT count(*) FROM research.irfc_financial_facts_backup_20260902)
       OR (SELECT row_count FROM protected_research_table_counts WHERE table_name = 'market_trading_calendar_exceptions')
           <> (SELECT count(*) FROM research.market_trading_calendar_exceptions)
       OR (SELECT row_count FROM protected_research_table_counts WHERE table_name = 'market_trading_schedules')
           <> (SELECT count(*) FROM research.market_trading_schedules) THEN
        RAISE EXCEPTION 'A protected research-schema table changed during reset';
    END IF;
END $$;

COMMIT;
