-- MANUAL ONLY: exactly ea48b845-e2a3-453b-8225-80a2a362d995.
-- See SINGLE_REFRESH_RUNBOOK.md. No startup/deployment invokes this file.
-- reviewed-preimages.hex is the exact original capture CSV encoded as hex.
-- preimage_sha256 MUST come from the independently reviewed approval record.
\set ON_ERROR_STOP on
\if :{?maintenance_approval}
\else
 DO $$ BEGIN RAISE EXCEPTION 'BLOCKED: maintenance_approval required'; END $$;
\endif
\if :{?evidence_id}
\else
 DO $$ BEGIN RAISE EXCEPTION 'BLOCKED: evidence_id required'; END $$;
\endif
\if :{?preimage_sha256}
\else
 DO $$ BEGIN RAISE EXCEPTION 'BLOCKED: preimage_sha256 required'; END $$;
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '60s';
SET LOCAL TIME ZONE 'UTC';
SET LOCAL search_path = pg_catalog, pg_temp;
SET LOCAL row_security = off;
CREATE TEMP TABLE maintenance_authorization ON COMMIT DROP AS
SELECT :'maintenance_approval'::text AS approval, :'evidence_id'::text AS evidence,
       :'preimage_sha256'::text AS preimage_sha256;
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM maintenance_authorization
   WHERE approval = 'APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION'
     AND evidence ~ '^[A-Za-z0-9_-]{8,100}$'
     AND preimage_sha256 ~ '^[a-f0-9]{64}$') THEN
  RAISE EXCEPTION 'Missing or invalid single-refresh approval, termination evidence ID or SHA-256';
 END IF;
 IF current_setting('session_replication_role') <> 'origin' THEN
  RAISE EXCEPTION 'Unexpected session replication role';
 END IF;
END $$;

CREATE TEMP TABLE preimage_file_hex (contents varchar(131072) NOT NULL) ON COMMIT DROP;
\copy preimage_file_hex FROM 'reviewed-preimages.hex' WITH (FORMAT text)
CREATE TEMP TABLE preimage_payload (payload json NOT NULL) ON COMMIT DROP;
DO $$ DECLARE file_bytes bytea; csv_record text; payload_text text; BEGIN
 IF (SELECT count(*) FROM preimage_file_hex) <> 1 THEN
  RAISE EXCEPTION 'One original preimage file required';
 END IF;
 IF EXISTS (SELECT FROM preimage_file_hex WHERE contents !~ '^([a-f0-9]{2})+$') THEN
  RAISE EXCEPTION 'Malformed preimage file hex';
 END IF;
 SELECT decode(contents, 'hex') INTO file_bytes FROM preimage_file_hex;
 IF encode(sha256(file_bytes), 'hex') IS DISTINCT FROM
    (SELECT preimage_sha256 FROM maintenance_authorization) THEN
  RAISE EXCEPTION 'Original preimage file SHA-256 mismatch';
 END IF;
 csv_record := convert_from(file_bytes, 'UTF8');
 -- capture_refresh_preimages.sql emits one quoted CSV field with a JSON
 -- array, followed by LF (or CRLF on a Windows client). Reject other forms.
 IF right(csv_record, 2) = E'\r\n' THEN
  csv_record := left(csv_record, length(csv_record) - 2);
 ELSIF right(csv_record, 1) = E'\n' THEN
  csv_record := left(csv_record, length(csv_record) - 1);
 ELSE
  RAISE EXCEPTION 'Preimage CSV must have its original record terminator';
 END IF;
 IF left(csv_record, 1) <> '"' OR right(csv_record, 1) <> '"'
    OR position(E'\n' in csv_record) > 0 OR position(E'\r' in csv_record) > 0 THEN
  RAISE EXCEPTION 'Expected one quoted preimage CSV field';
 END IF;
 payload_text := replace(substring(csv_record from 2 for length(csv_record) - 2), '""', '"');
 IF '"' || replace(payload_text, '"', '""') || '"' <> csv_record THEN
  RAISE EXCEPTION 'Malformed preimage CSV quoting';
 END IF;
 INSERT INTO preimage_payload VALUES (payload_text::json);
END $$;

-- Serialize both DML and DDL before checking the schema and full preimage.
LOCK TABLE research.research_refresh_runs IN EXCLUSIVE MODE;
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_class WHERE oid = 'research.research_refresh_runs'::regclass
   AND relkind = 'r' AND NOT relrowsecurity AND NOT relforcerowsecurity AND NOT relhassubclass)
   OR EXISTS (SELECT FROM pg_inherits WHERE inhrelid = 'research.research_refresh_runs'::regclass) THEN
  RAISE EXCEPTION 'Global comparison requires an ordinary table without row security or inheritance';
 END IF;
 IF (SELECT count(*) FROM pg_attribute WHERE attrelid = 'research.research_refresh_runs'::regclass
       AND attnum > 0 AND NOT attisdropped) <> 17
   OR EXISTS (
    SELECT FROM (VALUES
      ('refresh_run_id', 'uuid', true), ('instrument_id', 'uuid', true), ('company_id', 'uuid', true),
      ('started_at', 'timestamp with time zone', true), ('completed_at', 'timestamp with time zone', false),
      ('status', 'character varying(40)', true), ('mode', 'character varying(40)', true),
      ('documents_discovered', 'integer', true), ('documents_accepted', 'integer', true),
      ('events_extracted', 'integer', true), ('events_created', 'integer', true),
      ('events_updated', 'integer', true), ('deduplicated_count', 'integer', true),
      ('correlation_id', 'character varying(120)', false), ('safe_error_code', 'character varying(120)', false),
      ('safe_error_message', 'character varying(500)', false), ('updated_at', 'timestamp with time zone', true)
    ) AS expected(name, type, required)
    LEFT JOIN pg_attribute a ON a.attrelid = 'research.research_refresh_runs'::regclass
      AND a.attname = expected.name AND a.attnum > 0 AND NOT a.attisdropped
    WHERE a.attname IS NULL OR format_type(a.atttypid, a.atttypmod) <> expected.type
      OR a.attnotnull <> expected.required OR a.attgenerated <> '' OR a.attidentity <> '') THEN
  RAISE EXCEPTION 'Unexpected refresh schema';
 END IF;
 IF NOT EXISTS (SELECT FROM pg_constraint c JOIN pg_attribute a
   ON a.attrelid = c.conrelid AND a.attname = 'refresh_run_id'
   WHERE c.conrelid = 'research.research_refresh_runs'::regclass AND c.contype = 'p'
     AND c.conkey = ARRAY[a.attnum] AND NOT c.condeferrable) THEN
  RAISE EXCEPTION 'Unexpected refresh primary key';
 END IF;
 IF EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'research.research_refresh_runs'::regclass AND NOT tgisinternal)
   OR EXISTS (SELECT FROM pg_rewrite WHERE ev_class = 'research.research_refresh_runs'::regclass) THEN
  RAISE EXCEPTION 'Unreviewed triggers/rules could modify other data';
 END IF;
END $$;

CREATE TEMP TABLE expected (LIKE research.research_refresh_runs) ON COMMIT DROP;
DO $$ DECLARE p json; item json; keys text[]; columns text[]; BEGIN
 SELECT payload INTO p FROM preimage_payload;
 IF json_typeof(p) IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'One full preimage required'; END IF;
 IF json_array_length(p) <> 1 THEN RAISE EXCEPTION 'One full preimage required'; END IF;
 SELECT value INTO item FROM json_array_elements(p);
 IF json_typeof(item) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'One full preimage object required'; END IF;
 SELECT array_agg(attname::text ORDER BY attname::text) INTO columns FROM pg_attribute
 WHERE attrelid = 'research.research_refresh_runs'::regclass AND attnum > 0 AND NOT attisdropped;
 -- json, rather than jsonb, preserves duplicate keys so they fail this check.
 SELECT array_agg(key ORDER BY key) INTO keys FROM json_object_keys(item) key;
 IF keys IS DISTINCT FROM columns THEN RAISE EXCEPTION 'Missing, extra or duplicate preimage columns'; END IF;
 INSERT INTO expected SELECT * FROM json_populate_recordset(NULL::research.research_refresh_runs, p);
 IF (SELECT refresh_run_id FROM expected) IS DISTINCT FROM 'ea48b845-e2a3-453b-8225-80a2a362d995'::uuid THEN
  RAISE EXCEPTION 'Preimage ID set mismatch';
 END IF;
 IF (SELECT count(*) FROM research.research_refresh_runs WHERE completed_at IS NULL) <> 1
   OR EXISTS (SELECT FROM research.research_refresh_runs WHERE completed_at IS NULL
     AND refresh_run_id <> 'ea48b845-e2a3-453b-8225-80a2a362d995'::uuid) THEN
  RAISE EXCEPTION 'Global incomplete ID set mismatch';
 END IF;
 IF EXISTS (SELECT FROM expected e LEFT JOIN research.research_refresh_runs r USING (refresh_run_id)
   WHERE to_jsonb(r) IS DISTINCT FROM to_jsonb(e)) THEN
  RAISE EXCEPTION 'Full 17-column preimage mismatch';
 END IF;
 IF EXISTS (SELECT FROM expected WHERE status IS DISTINCT FROM 'RUNNING' OR mode IS DISTINCT FROM 'TARGETED_LIVE'
   OR correlation_id IS DISTINCT FROM '425aebf9-e576-4489-999a-87e52fe1a67d'
   OR completed_at IS NOT NULL OR documents_discovered IS DISTINCT FROM 0 OR documents_accepted IS DISTINCT FROM 0
   OR events_extracted IS DISTINCT FROM 0 OR events_created IS DISTINCT FROM 0 OR events_updated IS DISTINCT FROM 0
   OR deduplicated_count IS DISTINCT FROM 0) THEN
  RAISE EXCEPTION 'Unexpected refresh lifecycle, correlation or counters';
 END IF;
END $$;

SELECT 'BEFORE' AS stage, a.evidence, a.preimage_sha256, to_jsonb(e) AS row
FROM expected e CROSS JOIN maintenance_authorization a;
CREATE TEMP TABLE reconciliation_clock ON COMMIT DROP AS SELECT clock_timestamp() AS reconciled_at;
CREATE TEMP TABLE changed ON COMMIT DROP AS
WITH updated AS (
 UPDATE research.research_refresh_runs r
 SET status = 'FAILED', completed_at = c.reconciled_at, updated_at = c.reconciled_at,
     safe_error_code = 'ADMIN_WORKER_TERMINATION_VERIFIED',
     safe_error_message = 'Administrative reconciliation after verified worker termination; original failure time and cause unknown. Evidence: ' || a.evidence
 FROM expected e, reconciliation_clock c, maintenance_authorization a
 WHERE r.refresh_run_id = e.refresh_run_id
 RETURNING r.*
) SELECT * FROM updated;
DO $$ BEGIN
 IF (SELECT count(*) FROM changed) <> 1 THEN RAISE EXCEPTION 'Exactly one update required'; END IF;
 IF EXISTS (SELECT FROM expected e JOIN research.research_refresh_runs r USING (refresh_run_id)
   WHERE (to_jsonb(r) - ARRAY['status','completed_at','updated_at','safe_error_code','safe_error_message']) IS DISTINCT FROM
         (to_jsonb(e) - ARRAY['status','completed_at','updated_at','safe_error_code','safe_error_message'])) THEN
  RAISE EXCEPTION 'Non-lifecycle data changed';
 END IF;
 IF EXISTS (SELECT FROM research.research_refresh_runs r JOIN expected e USING (refresh_run_id)
   CROSS JOIN reconciliation_clock c CROSS JOIN maintenance_authorization a
   WHERE r.status IS DISTINCT FROM 'FAILED' OR r.completed_at IS DISTINCT FROM c.reconciled_at
     OR r.updated_at IS DISTINCT FROM c.reconciled_at
     OR r.safe_error_code IS DISTINCT FROM 'ADMIN_WORKER_TERMINATION_VERIFIED'
     OR r.safe_error_message IS DISTINCT FROM ('Administrative reconciliation after verified worker termination; original failure time and cause unknown. Evidence: ' || a.evidence)) THEN
  RAISE EXCEPTION 'Postimage mismatch';
 END IF;
 IF EXISTS (SELECT FROM research.research_refresh_runs WHERE completed_at IS NULL) THEN
  RAISE EXCEPTION 'Incomplete rows remain';
 END IF;
END $$;
SELECT 'AFTER_PENDING_COMMIT' AS stage, a.evidence, a.preimage_sha256, to_jsonb(r) AS row
FROM research.research_refresh_runs r JOIN expected e USING (refresh_run_id)
CROSS JOIN maintenance_authorization a;
COMMIT;
\echo 'COMMITTED: independently read back the single approved row; retain original CSV, approved SHA-256 and termination evidence'
