-- MANUAL ONLY. Run from the approved evidence directory, using psql -X.
-- reviewed-preimages.csv must be captured, reviewed and hashed independently.
\set ON_ERROR_STOP on
\if :{?maintenance_approval}
\else
  \echo 'BLOCKED: maintenance_approval required'
  DO $$ BEGIN RAISE EXCEPTION 'BLOCKED: maintenance_approval required'; END $$;
\endif
\if :{?evidence_id}
\else
  \echo 'BLOCKED: evidence_id required'
  DO $$ BEGIN RAISE EXCEPTION 'BLOCKED: evidence_id required'; END $$;
\endif
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
SET LOCAL TIME ZONE 'UTC';
CREATE TEMP TABLE maintenance_authorization ON COMMIT DROP AS
SELECT :'maintenance_approval'::text AS approval, :'evidence_id'::text AS evidence;
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM maintenance_authorization WHERE approval = 'APPROVED_REFRESH_TERMINATION_AND_RECONCILIATION' AND evidence ~ '^[A-Za-z0-9_-]{8,100}$') THEN
  RAISE EXCEPTION 'Missing approved termination/reconciliation evidence';
 END IF;
END $$;
CREATE TEMP TABLE preimage_payload (payload jsonb NOT NULL) ON COMMIT DROP;
\copy preimage_payload FROM 'reviewed-preimages.csv' WITH (FORMAT csv)
CREATE TEMP TABLE approved_ids (id uuid PRIMARY KEY) ON COMMIT DROP;
INSERT INTO approved_ids VALUES
 ('527caf33-c0ee-400c-92a6-66e41e7eeb54'),
 ('e3584834-2097-4f0a-a073-d8ea0bf4dee3'),
 ('5e71f48c-8ab3-4550-9cc0-e70e2b3530d3'),
 ('2c2f39cc-80cc-4ac2-89ef-31cdfa88d449'),
 ('ea8caf36-3cad-47ac-9268-f9228592c106'),
 ('02b33cab-abd0-41a3-a0cd-de21c8053d0c');
CREATE TEMP TABLE expected (LIKE research.research_refresh_runs) ON COMMIT DROP;
DO $$ DECLARE p jsonb; item jsonb; keys text[]; columns text[]; BEGIN
 IF (SELECT count(*) FROM preimage_payload) <> 1 THEN RAISE EXCEPTION 'One preimage payload required'; END IF;
 SELECT payload INTO p FROM preimage_payload;
 IF jsonb_typeof(p) <> 'array' OR jsonb_array_length(p) <> 6 THEN RAISE EXCEPTION 'Six full preimages required'; END IF;
 SELECT array_agg(attname::text ORDER BY attname::text) INTO columns FROM pg_attribute
 WHERE attrelid='research.research_refresh_runs'::regclass AND attnum>0 AND NOT attisdropped;
 IF cardinality(columns) <> 17 THEN RAISE EXCEPTION 'Unexpected refresh schema'; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(p) LOOP
  SELECT array_agg(key ORDER BY key) INTO keys FROM jsonb_object_keys(item) key;
  IF keys IS DISTINCT FROM columns THEN RAISE EXCEPTION 'Missing or extra preimage columns'; END IF;
 END LOOP;
 INSERT INTO expected SELECT * FROM jsonb_populate_recordset(NULL::research.research_refresh_runs,p);
 IF (SELECT count(DISTINCT refresh_run_id) FROM expected) <> 6 OR
    EXISTS (SELECT refresh_run_id FROM expected EXCEPT SELECT id FROM approved_ids) THEN
  RAISE EXCEPTION 'Preimage ID set mismatch';
 END IF;
END $$;
LOCK TABLE research.research_refresh_runs IN EXCLUSIVE MODE;
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_class WHERE oid='research.research_refresh_runs'::regclass AND (relrowsecurity OR relkind <> 'r')) THEN
  RAISE EXCEPTION 'Global comparison requires an ordinary table without row security'; END IF;
 IF EXISTS (SELECT FROM pg_trigger WHERE tgrelid='research.research_refresh_runs'::regclass AND NOT tgisinternal AND tgenabled <> 'D') OR
    EXISTS (SELECT FROM pg_rewrite WHERE ev_class='research.research_refresh_runs'::regclass AND rulename <> '_RETURN') THEN
  RAISE EXCEPTION 'Unreviewed triggers/rules could modify other data'; END IF;
 IF (SELECT count(*) FROM research.research_refresh_runs WHERE completed_at IS NULL) <> 6 OR
    EXISTS (SELECT refresh_run_id FROM research.research_refresh_runs WHERE completed_at IS NULL EXCEPT SELECT id FROM approved_ids) THEN
  RAISE EXCEPTION 'Global incomplete ID set mismatch';
 END IF;
 IF EXISTS (SELECT FROM expected e LEFT JOIN research.research_refresh_runs r USING(refresh_run_id)
            WHERE to_jsonb(r) IS DISTINCT FROM to_jsonb(e)) THEN
  RAISE EXCEPTION 'Full 17-column preimage mismatch';
 END IF;
 IF EXISTS (SELECT FROM expected WHERE status IS DISTINCT FROM 'RUNNING' OR mode IS DISTINCT FROM 'TARGETED_LIVE'
   OR completed_at IS NOT NULL OR documents_discovered IS DISTINCT FROM 0 OR documents_accepted IS DISTINCT FROM 0
   OR events_extracted IS DISTINCT FROM 0 OR events_created IS DISTINCT FROM 0 OR events_updated IS DISTINCT FROM 0
   OR deduplicated_count IS DISTINCT FROM 0) THEN RAISE EXCEPTION 'Unexpected refresh lifecycle or counters'; END IF;
END $$;
SELECT 'BEFORE' AS stage, a.evidence, to_jsonb(e) AS row FROM expected e CROSS JOIN maintenance_authorization a ORDER BY refresh_run_id;
CREATE TEMP TABLE reconciliation_clock ON COMMIT DROP AS SELECT clock_timestamp() AS reconciled_at;
CREATE TEMP TABLE changed ON COMMIT DROP AS
WITH updated AS (
 UPDATE research.research_refresh_runs r
 SET status='FAILED', completed_at=c.reconciled_at, updated_at=c.reconciled_at,
     safe_error_code='ADMIN_WORKER_TERMINATION_VERIFIED',
     safe_error_message='Administrative reconciliation after verified worker termination; original failure time and cause unknown. Evidence: ' || a.evidence
 FROM expected e, reconciliation_clock c, maintenance_authorization a
 WHERE r.refresh_run_id=e.refresh_run_id
 RETURNING r.*
) SELECT * FROM updated;
DO $$ BEGIN
 IF (SELECT count(*) FROM changed) <> 6 THEN RAISE EXCEPTION 'Exactly six updates required'; END IF;
 IF EXISTS (SELECT FROM expected e JOIN research.research_refresh_runs r USING(refresh_run_id)
   WHERE (to_jsonb(r) - ARRAY['status','completed_at','updated_at','safe_error_code','safe_error_message']) IS DISTINCT FROM
         (to_jsonb(e) - ARRAY['status','completed_at','updated_at','safe_error_code','safe_error_message'])) THEN
  RAISE EXCEPTION 'Non-lifecycle data changed'; END IF;
 IF EXISTS (SELECT FROM research.research_refresh_runs r JOIN expected e USING(refresh_run_id)
 CROSS JOIN reconciliation_clock c CROSS JOIN maintenance_authorization a
 WHERE r.status IS DISTINCT FROM 'FAILED' OR r.completed_at IS DISTINCT FROM c.reconciled_at
 OR r.updated_at IS DISTINCT FROM c.reconciled_at
 OR r.safe_error_code IS DISTINCT FROM 'ADMIN_WORKER_TERMINATION_VERIFIED'
 OR r.safe_error_message IS DISTINCT FROM ('Administrative reconciliation after verified worker termination; original failure time and cause unknown. Evidence: ' || a.evidence)) THEN
  RAISE EXCEPTION 'Postimage mismatch'; END IF;
 IF EXISTS (SELECT FROM research.research_refresh_runs WHERE completed_at IS NULL) THEN RAISE EXCEPTION 'Incomplete rows remain'; END IF;
END $$;
SELECT 'AFTER_PENDING_COMMIT' AS stage, a.evidence, to_jsonb(r) AS row
FROM research.research_refresh_runs r JOIN approved_ids i ON i.id=r.refresh_run_id
CROSS JOIN maintenance_authorization a ORDER BY refresh_run_id;
COMMIT;
\echo 'COMMITTED: independently read back all six rows; retain this output and reviewed input hashes'
