-- Snapshot only the known research Flyway table names. Never bootstrap metadata.
-- The research service configures flyway_schema_history_research; older installs
-- may use the default name. Both absence and presence must survive the reset.
CREATE TEMP TABLE etf_flyway_state (
  table_name text PRIMARY KEY,
  present boolean NOT NULL,
  relation_oid oid,
  row_count bigint,
  row_hash text
);
DO $flyway$
DECLARE name text; relation regclass; n bigint; h text;
BEGIN
  FOREACH name IN ARRAY ARRAY['flyway_schema_history', 'flyway_schema_history_research'] LOOP
    relation := to_regclass(format('research.%I', name));
    n := NULL;
    h := NULL;
    IF relation IS NOT NULL THEN
      EXECUTE format(
        'SELECT count(*), md5(coalesce(string_agg(t::text, ''|'' ORDER BY t::text), '''')) FROM %s t',
        relation) INTO n, h;
    END IF;
    INSERT INTO etf_flyway_state VALUES (name, relation IS NOT NULL, relation::oid, n, h);
  END LOOP;
END
$flyway$;
