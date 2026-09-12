-- The portfolio-wide refresh job API and all application callers were removed
-- before this migration. Targeted readiness ensure records provider work in
-- research_refresh_runs, which remains part of the active architecture.
DROP TABLE IF EXISTS research.research_refresh_jobs;
