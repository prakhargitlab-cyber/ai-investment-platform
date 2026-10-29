-- Separately approved READ-ONLY capture. psql -X -q -f this-file -o reviewed-preimages.csv
-- Do not pipe through PowerShell text conversion. Review/hash the resulting CSV.
\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='5s';
SET LOCAL TIME ZONE 'UTC';
COPY (SELECT jsonb_agg(to_jsonb(r) ORDER BY refresh_run_id) FROM research.research_refresh_runs r WHERE completed_at IS NULL) TO STDOUT WITH (FORMAT csv);
ROLLBACK;
