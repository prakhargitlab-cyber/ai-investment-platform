-- ISOLATED_DEPLOYMENT_READ_ONLY_V1
-- Executed only by explicit, authorized isolated deployment, inside each existing
-- PostgreSQL Pod. No application imports, schema creation, temporary objects,
-- locks that exclude writers, migrations, role changes, or persistent settings.
-- Every incomplete refresh/legacy job remains a blocker, including old orphans.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '1s';
WITH relations AS (
    SELECT c.oid, n.nspname, c.relname, c.relkind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
      AND c.relkind IN ('r', 'p', 'S')
), required_tables AS (
    SELECT name FROM unnest(ARRAY['global_opportunity_cycle_run', 'global_opportunity_cycle_active',
      'global_opportunity_cycle_progress', 'global_opportunity_top_selection', 'research_refresh_runs',
      'research_news_search_runs', 'etf_acquisition_attempts', 'etf_listing_observations',
      'etf_fact_observations', 'etf_nav_observations', 'etf_holdings_snapshots',
      'etf_holdings_positions', 'etf_radar_cycles', 'flyway_schema_history_research']) AS name
), table_fingerprints AS (
    -- The query text contains only catalog identifiers escaped with %I. All
    -- dynamic statements are SELECTs under a verified read-only transaction.
    SELECT nspname, relname, relkind,
      query_to_xml(format(
        'SELECT count(*) AS rows, md5(coalesce(string_agg(row_hash, '''' ORDER BY row_hash), '''')) AS hash FROM (SELECT md5(to_jsonb(t)::text) AS row_hash FROM %I.%I AS t OFFSET 0) AS row_hashes',
        nspname, relname), false, true, '')::text AS contents
    FROM relations
    WHERE relkind IN ('r', 'p')
), sequence_fingerprints AS (
    -- Sequences have no composite row type. Read their scalar state without
    -- nextval/setval, and include all pg_sequence definition fields.
    -- Sequence state is not MVCC: unchanged samples are required by the quiet
    -- window, but this does not make concurrent sequence use transactional.
    SELECT r.nspname, r.relname, r.relkind,
      CASE WHEN s.seqrelid IS NOT NULL THEN jsonb_build_array(
        s.seqtypid, s.seqstart, s.seqincrement, s.seqmax, s.seqmin,
        s.seqcache, s.seqcycle,
        query_to_xml(format(
          'SELECT last_value, log_cnt, is_called FROM %I.%I',
          r.nspname, r.relname), false, true, '')::text)::text
      ELSE NULL END AS contents
    FROM relations r LEFT JOIN pg_sequence s ON s.seqrelid = r.oid
    WHERE r.relkind = 'S'
), fingerprints AS MATERIALIZED (
    -- Keep one expensive observation per relation, even when summary consumes
    -- contents both for aggregation and completeness. This stores only the
    -- compact per-relation results, not table rows.
    SELECT * FROM table_fingerprints
    UNION ALL
    SELECT * FROM sequence_fingerprints
), schema_definition AS (
    SELECT coalesce(pg_catalog.string_agg(definition, E'\n'::text ORDER BY definition), '') AS definition FROM (
      SELECT n.nspname::text || '.'::text || c.relname::text || '.'::text || a.attname::text || ':'::text ||
        pg_catalog.format_type(a.atttypid, a.atttypmod) || ':'::text || a.attnotnull::text || ':'::text ||
        coalesce(pg_catalog.pg_get_expr(d.adbin,d.adrelid), '') AS definition
      FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
      JOIN pg_namespace n ON n.oid=c.relnamespace
      LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
      WHERE a.attnum>0 AND NOT a.attisdropped AND n.nspname IN (SELECT nspname FROM relations)
      UNION ALL
      SELECT n.nspname::text || ':'::text || c.conname::text || ':'::text || pg_catalog.pg_get_constraintdef(c.oid)
      FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
      WHERE n.nspname IN (SELECT nspname FROM relations)
      UNION ALL
      SELECT schemaname::text || ':'::text || indexname::text || ':'::text || indexdef::text FROM pg_indexes
      WHERE schemaname IN (SELECT nspname FROM relations)
    ) definitions
), jobs AS (
    SELECT payload::jsonb AS p, cycle_id, generated_at
    FROM research.global_opportunity_top_selection
), latest_jobs AS (
    SELECT DISTINCT ON (p->>'cycle_id') p
    FROM jobs WHERE p->>'record_kind' = 'CYCLE_JOB'
    ORDER BY p->>'cycle_id', generated_at DESC, cycle_id DESC
), work AS (
    SELECT
      (SELECT count(*) FROM research.global_opportunity_cycle_run
       WHERE status IS NULL OR status NOT IN ('COMPLETED','FAILED','CANCELLED') OR owner_id IS NOT NULL)
      + (SELECT count(*) FROM research.global_opportunity_cycle_active)
      + (SELECT count(*) FROM research.research_refresh_runs WHERE completed_at IS NULL)
      + (SELECT count(*) FROM latest_jobs
         WHERE p->>'cycle_id' IS NULL OR p->>'status' IS NULL OR
           p->>'status' NOT IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED','COMPLETED','FAILED','CANCELLED') OR
           (p->>'status' NOT IN ('COMPLETED','FAILED','CANCELLED') AND NOT EXISTS
             (SELECT 1 FROM research.global_opportunity_cycle_run r WHERE r.cycle_id::text = p->>'cycle_id')))
      + (SELECT count(*) FROM jobs WHERE jsonb_typeof(p) IS DISTINCT FROM 'object' OR
           (p ? 'record_kind' AND p->>'record_kind' IS DISTINCT FROM 'CYCLE_JOB') OR
           (p->>'record_kind' = 'CYCLE_JOB' AND
             (jsonb_typeof(p->'cycle_id') IS DISTINCT FROM 'string' OR p->>'cycle_id' = '' OR
              jsonb_typeof(p->'status') IS DISTINCT FROM 'string'))) AS incomplete
), sessions AS (
    -- Superuser visibility is required below; hidden session fields cannot pass.
    SELECT count(*) AS other_transactions FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid()
      AND (xact_start IS NOT NULL OR state IS NULL OR state <> 'idle')
), summary AS (
    -- Catalog name and internal "char" values must enter concatenation as text;
    -- otherwise text/anynonarray overloads can compete with text/text.
    SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(coalesce(pg_catalog.string_agg(
      nspname::text || '.'::text || relname::text || ':'::text || relkind::text || ':'::text || contents,
      E'\n'::text ORDER BY nspname, relname), ''::text), 'UTF8'::name)), 'hex'::text) AS fingerprint,
      count(*) FILTER (WHERE nspname = 'research' AND relkind IN ('r','p')) AS research_tables,
      coalesce(bool_and(contents IS NOT NULL), false) AS complete
    FROM fingerprints
)
SELECT json_build_object(
  'database', current_database(), 'username', current_user,
  'read_only', current_setting('transaction_read_only'),
  'is_superuser', current_setting('is_superuser'),
  'missing_required_tables', (SELECT count(*) FROM required_tables r WHERE NOT EXISTS
      (SELECT 1 FROM relations t WHERE t.nspname='research' AND t.relname=r.name AND t.relkind IN ('r','p'))),
  'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
  'status', CASE WHEN current_setting('transaction_read_only') = 'on'
      AND current_setting('is_superuser') = 'on'
      AND work.incomplete = 0 AND sessions.other_transactions = 0
      AND NOT EXISTS (SELECT 1 FROM required_tables r WHERE NOT EXISTS
          (SELECT 1 FROM relations t WHERE t.nspname='research' AND t.relname=r.name AND t.relkind IN ('r','p')))
      AND summary.complete AND summary.research_tables >= 40 THEN 'QUIET_SNAPSHOT' ELSE 'BLOCKED' END,
  'fingerprint', CASE WHEN summary.complete THEN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(summary.fingerprint || schema_definition.definition, 'UTF8'::name)), 'hex'::text) ELSE NULL END,
  'research_tables', summary.research_tables,
  'other_transactions', sessions.other_transactions, 'incomplete_work', work.incomplete)
FROM summary CROSS JOIN work CROSS JOIN sessions CROSS JOIN schema_definition;
ROLLBACK;
