-- Synthetic test DB only. Every base table is included with its name, row
-- count and ordered complete-row JSON digest, retaining every field and NULL.
-- The preserved mode excludes exactly the known synthetic reset scope.
\set ON_ERROR_STOP on
\if :{?preserved_only}
\else
  \set preserved_only false
\endif
SELECT format(
  'SELECT %L || ''|'' || count(*)::text || ''|'' || md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text COLLATE "C")::text,''[]'')) FROM %I.%I t %s;',
  c.relname, n.nspname, c.relname,
  CASE WHEN NOT :'preserved_only'::boolean THEN ''
       WHEN c.relname = 'global_daily_market_bars' THEN
         'WHERE t.global_instrument_id NOT IN (''11111111-1111-1111-1111-111111111111''::uuid,''bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb''::uuid,''aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa''::uuid)'
       WHEN c.relname = 'global_opportunity_cycle_run' THEN 'WHERE t.market <> ''ETF'''
       WHEN c.relname = 'global_opportunity_cycle_progress' THEN 'WHERE t.cycle_id <> ''c-etf-1'''
       WHEN c.relname = 'global_opportunity_cycle_active' THEN 'WHERE t.market <> ''ETF'' AND t.cycle_id <> ''c-etf-1'''
       ELSE '' END)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'research' AND c.relkind IN ('r','p')
  AND (NOT :'preserved_only'::boolean OR c.relname NOT IN
    ('etf_fact_observations','etf_nav_observations','etf_holdings_snapshots',
     'etf_holdings_positions','etf_listing_observations','etf_acquisition_attempts','etf_radar_cycles'))
ORDER BY c.relname COLLATE "C"
\gexec
