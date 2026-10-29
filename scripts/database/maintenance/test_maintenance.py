"""Offline tests; opt-in PostgreSQL tests create only a private synthetic container."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('observation_pod', ROOT / 'observation_pod.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.image = 'ai-investment-registry:5000/ai-investment/research-engine@sha256:' + 'a'*64
        env = [{'name': 'AIP_RESEARCH_DATABASE_' + k, 'value': v} for k,v in
               [('BACKEND','postgres'),('SCHEMA','research'),('NAME','investment'),('HOST','postgres'),('USER','investment')]]
        env.append({'name':'AIP_RESEARCH_DATABASE_PASSWORD','valueFrom':{'secretKeyRef':{'name':'database','key':'password'}}})
        self.deployment = {'metadata':{'name':'research-engine','namespace':'ai-investment'},
            'spec':{'selector':{'matchLabels':{'app.kubernetes.io/component':'research-engine'}},'template':{'spec':{'containers':[{'name':'research-engine','env':env}],
                                      'initContainers':[{'name':'unsafe'}], 'volumes':[{'name':'token'}]}}}}
        self.services = {'items':[{'spec':{'selector':{'app.kubernetes.io/component':'research-engine'}}}]}
    def generate(self):
        return module.generate(self.deployment,self.services,self.image)
    def test_idle_only_hardened_pod(self):
        original=copy.deepcopy(self.deployment)
        pod=self.generate(); spec=pod['spec']; c=spec['containers'][0]
        self.assertEqual(self.deployment,original)
        self.assertEqual(c['command'],['/usr/local/bin/python','-I','-S','-B','-c'])
        self.assertEqual(c['args'],['import time; time.sleep(14400)'])
        self.assertFalse(spec['automountServiceAccountToken'])
        for key in ['initContainers','volumes','serviceAccountName','ephemeralContainers']:
            self.assertNotIn(key,spec)
        for key in ['volumeMounts','ports','lifecycle']:
            self.assertNotIn(key,c)
        self.assertEqual(c['securityContext']['capabilities']['drop'],['ALL'])
        self.assertTrue(c['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(c['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual(spec['securityContext']['runAsUser'],10001)
    def test_tag_rejected(self):
        self.image='research-engine:latest'
        with self.assertRaises(ValueError): self.generate()
    def test_service_collision_rejected(self):
        self.services['items'].append({'spec':{'selector':{'maintenance.aip/purpose':'refresh-observation'}}})
        with self.assertRaises(ValueError): self.generate()
    def test_implicit_environment_rejected(self):
        self.deployment['spec']['template']['spec']['containers'][0]['envFrom']=[{'secretRef':{'name':'x'}}]
        with self.assertRaises(ValueError): self.generate()
    def test_database_identity_required(self):
        self.deployment['spec']['template']['spec']['containers'][0]['env'][2]['value']='other'
        with self.assertRaises(ValueError): self.generate()
    def test_pause_and_secret_reference(self):
        env={e['name']:e for e in self.generate()['spec']['containers'][0]['env']}
        self.assertEqual(env['AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED']['value'],'true')
        self.assertEqual(env['AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED']['value'],'false')
        self.assertIn('secretKeyRef',env['AIP_RESEARCH_DATABASE_PASSWORD']['valueFrom'])

    def test_literal_password_rejected(self):
        self.deployment['spec']['template']['spec']['containers'][0]['env'][-1] = {
            'name':'AIP_RESEARCH_DATABASE_PASSWORD', 'value':'must-not-be-emitted'}
        with self.assertRaises(ValueError): self.generate()

    def test_deployment_expression_collision_rejected(self):
        self.deployment['spec']['selector'] = {'matchExpressions':[
            {'key':'app.kubernetes.io/component', 'operator':'DoesNotExist'}]}
        with self.assertRaises(ValueError): self.generate()

class SqlContractTests(unittest.TestCase):
    def setUp(self):
        self.sql=(ROOT/'reconcile_refresh_runs.sql').read_text()
    def test_approval_and_transaction(self):
        for token in ['\\set ON_ERROR_STOP on',"RAISE EXCEPTION 'BLOCKED:",'APPROVED_REFRESH_TERMINATION_AND_RECONCILIATION',
                      "lock_timeout = '5s'", "statement_timeout = '30s'", 'BEGIN;', 'COMMIT;']:
            self.assertIn(token,self.sql)
        self.assertEqual(self.sql.count('COMMIT;'),1)
        self.assertLess(self.sql.index("RAISE EXCEPTION 'Incomplete rows remain'"),self.sql.index('COMMIT;'))
    def test_exact_ids_and_full_preimages(self):
        ids=['527caf33-c0ee-400c-92a6-66e41e7eeb54','e3584834-2097-4f0a-a073-d8ea0bf4dee3',
             '5e71f48c-8ab3-4550-9cc0-e70e2b3530d3','2c2f39cc-80cc-4ac2-89ef-31cdfa88d449',
             'ea8caf36-3cad-47ac-9268-f9228592c106','02b33cab-abd0-41a3-a0cd-de21c8053d0c']
        for value in ids: self.assertEqual(self.sql.count(value),1)
        for token in ['cardinality(columns) <> 17','keys IS DISTINCT FROM columns',
                      'to_jsonb(r) IS DISTINCT FROM to_jsonb(e)', 'count(DISTINCT refresh_run_id)',
                      'count(*) FROM changed) <> 6','EXCLUSIVE MODE','Global incomplete ID set mismatch']:
            self.assertIn(token,self.sql)
        self.assertFalse((ROOT/'reviewed-preimages.csv').exists())
    def test_only_target_table_mutated_and_audit_present(self):
        self.assertEqual(self.sql.count('UPDATE research.'),1)
        self.assertNotIn('DELETE FROM',self.sql.upper())
        for token in ['pg_trigger','pg_rewrite','relrowsecurity',"'BEFORE'", "'AFTER_PENDING_COMMIT'",'Non-lifecycle data changed',
                      'original failure time and cause unknown']:
            self.assertIn(token,self.sql)
    def test_capture_readonly_and_full_precision(self):
        sql=(ROOT/'capture_refresh_preimages.sql').read_text()
        for token in ['REPEATABLE READ READ ONLY','to_jsonb(r)',"TIME ZONE 'UTC'",'FORMAT csv','ROLLBACK;']:
            self.assertIn(token,sql)
        self.assertNotIn('UPDATE ',sql)

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import time
import uuid

@unittest.skipUnless(os.environ.get('AIP_MAINTENANCE_ISOLATED_PG') == '1', 'opt-in private PostgreSQL')
class IsolatedPostgresFixture(unittest.TestCase):
    @classmethod
    def docker(cls, *args, input=None, check=True):
        result = subprocess.run(['docker', *args], input=input, text=True,
                                capture_output=True, timeout=60)
        if check and result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result

    @classmethod
    def sql(cls, sql):
        return cls.docker('exec', '-i', cls.container, 'psql', '-X', '-qAt',
                          '-v', 'ON_ERROR_STOP=1', '-U', 'postgres',
                          '-d', 'maintenance_synthetic', input=sql).stdout.strip()

    @classmethod
    def setUpClass(cls):
        cls.container = 'aip-maintenance-synthetic-' + uuid.uuid4().hex
        image = cls.docker('image', 'inspect', 'postgres:16', '--format', '{{.Id}}').stdout.strip()
        cls.docker('run', '-d', '--pull=never', '--name', cls.container, '--network', 'none',
                   '--tmpfs', '/var/lib/postgresql/data', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', image)
        cls.addClassCleanup(cls.docker, 'rm', '-f', cls.container)
        for _ in range(60):
            # The entrypoint's temporary init server can answer pg_isready
            # before shutting down. Wait for the final PID-1 postgres server.
            if cls.docker('exec', cls.container, 'sh', '-c',
                          'test "$(cat /proc/1/comm)" = postgres && pg_isready -U postgres',
                          check=False).returncode == 0:
                break
            time.sleep(.5)
        else:
            raise AssertionError('Private PostgreSQL did not become ready')
        cls.docker('exec', cls.container, 'createdb', '-U', 'postgres', 'maintenance_synthetic')
        for file in ['reconcile_refresh_runs.sql', 'capture_refresh_preimages.sql', 'reconcile_single_refresh.sql']:
            cls.docker('cp', str(ROOT / file), cls.container + ':/tmp/' + file)
        print('ISOLATED PostgreSQL image:', image, flush=True)


class IsolatedPostgresTests(IsolatedPostgresFixture):
    def setUp(self):
        self.ids = re.findall(r"\('([0-9a-f-]{36})'\)", (ROOT / 'reconcile_refresh_runs.sql').read_text())
        self.sql("""
DROP SCHEMA IF EXISTS research CASCADE;
CREATE SCHEMA research;
CREATE TABLE research.research_refresh_runs (
refresh_run_id uuid PRIMARY KEY, instrument_id uuid NOT NULL, company_id uuid NOT NULL,
started_at timestamptz NOT NULL, completed_at timestamptz, status varchar(40) NOT NULL,
mode varchar(40) NOT NULL, documents_discovered integer NOT NULL DEFAULT 0,
documents_accepted integer NOT NULL DEFAULT 0, events_extracted integer NOT NULL DEFAULT 0,
events_created integer NOT NULL DEFAULT 0, events_updated integer NOT NULL DEFAULT 0,
deduplicated_count integer NOT NULL DEFAULT 0, correlation_id varchar(120),
safe_error_code varchar(120), safe_error_message varchar(500), updated_at timestamptz NOT NULL);
CREATE TABLE research.checkpoint_sentinel (payload jsonb);
INSERT INTO research.checkpoint_sentinel VALUES ('{"preserve":"all history"}');
""")
        for i, key in enumerate(self.ids):
            self.sql(f"INSERT INTO research.research_refresh_runs VALUES ('{key}', '{uuid.UUID(int=i+1)}', '{uuid.UUID(int=i+20)}', '2026-09-25 22:56:45.123456+00', NULL, 'RUNNING', 'TARGETED_LIVE',0,0,0,0,0,0,'synthetic-correlation',NULL,NULL,'2026-09-25 22:56:45.654321+00');")
        self.sql("INSERT INTO research.research_refresh_runs SELECT 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', instrument_id,company_id,started_at,started_at,'COMPLETED',mode,1,2,3,4,5,6,correlation_id,NULL,NULL,updated_at FROM research.research_refresh_runs LIMIT 1;")
        self.docker('exec', self.container, 'psql', '-X', '-q', '-U', 'postgres',
                    '-d', 'maintenance_synthetic', '-f', '/tmp/capture_refresh_preimages.sql',
                    '-o', '/tmp/reviewed-preimages.csv')
        capture = self.docker('exec', self.container, 'cat', '/tmp/reviewed-preimages.csv').stdout
        self.preimages = json.loads(next(csv.reader(io.StringIO(capture)))[0])

    def write_preimages(self, rows):
        output = io.StringIO()
        csv.writer(output, lineterminator='\n').writerow([json.dumps(rows)])
        self.docker('exec', '-i', self.container, 'sh', '-c', 'cat > /tmp/reviewed-preimages.csv', input=output.getvalue())

    def snapshot(self):
        return self.sql('SELECT jsonb_agg(to_jsonb(r) ORDER BY refresh_run_id) FROM research.research_refresh_runs r;')

    def run_reconciliation(self, approval=True):
        args = ['exec', '-w', '/tmp', self.container, 'psql', '-X', '-U', 'postgres', '-d', 'maintenance_synthetic']
        if approval:
            args += ['-v', 'maintenance_approval=APPROVED_REFRESH_TERMINATION_AND_RECONCILIATION']
        args += ['-v', 'evidence_id=SYNTHETIC_TEST_ONLY', '-f', '/tmp/reconcile_refresh_runs.sql']
        return self.docker(*args, check=False)

    def assert_rollback(self, error):
        before = self.snapshot()
        result = self.run_reconciliation()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(error, result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('COMMITTED:', result.stdout)

    def test_success_exact_six_and_preserved_history(self):
        before = json.loads(self.snapshot())
        result = self.run_reconciliation()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(self.snapshot())
        changed = []
        metadata = {'status','completed_at','updated_at','safe_error_code','safe_error_message'}
        for old, new in zip(before, after):
            if old == new:
                continue
            changed.append(new['refresh_run_id'])
            self.assertEqual({k:v for k,v in old.items() if k not in metadata},
                             {k:v for k,v in new.items() if k not in metadata})
            self.assertEqual(new['status'], 'FAILED')
            self.assertEqual(new['completed_at'], new['updated_at'])
            self.assertEqual(new['safe_error_code'], 'ADMIN_WORKER_TERMINATION_VERIFIED')
        self.assertEqual(set(changed), set(self.ids))
        self.assertEqual(self.sql('SELECT payload FROM research.checkpoint_sentinel;'), '{"preserve": "all history"}')
        for marker in ['BEFORE','AFTER_PENDING_COMMIT','COMMIT','COMMITTED:']:
            self.assertIn(marker, result.stdout)

    def test_all_17_preimage_columns_fail_closed(self):
        mutations = dict(refresh_run_id=str(uuid.uuid4()), instrument_id=str(uuid.uuid4()), company_id=str(uuid.uuid4()),
            started_at='2026-09-25T22:56:45.123457+00:00', completed_at='2026-09-26T00:00:00+00:00',
            status='FAILED', mode='LIVE', documents_discovered=1, documents_accepted=1, events_extracted=1,
            events_created=1, events_updated=1, deduplicated_count=1, correlation_id=None, safe_error_code='changed',
            safe_error_message='changed', updated_at='2026-09-25T22:56:45.654322+00:00')
        self.assertEqual(len(mutations), 17)
        for key, value in mutations.items():
            with self.subTest(column=key):
                rows = copy.deepcopy(self.preimages)
                rows[0][key] = value
                self.write_preimages(rows)
                self.assert_rollback('Preimage ID set mismatch' if key == 'refresh_run_id' else 'Full 17-column preimage mismatch')

    def test_seventh_active_run_rolls_back(self):
        self.sql("UPDATE research.research_refresh_runs SET completed_at=NULL,status='RUNNING' WHERE refresh_run_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';")
        self.assert_rollback('Global incomplete ID set mismatch')

    def test_update_error_rolls_back(self):
        self.sql("ALTER TABLE research.research_refresh_runs ADD CONSTRAINT synthetic_failure CHECK(status <> 'FAILED');")
        self.assert_rollback('synthetic_failure')

    def test_missing_approval_nonzero_no_writes(self):
        before = self.snapshot()
        result = self.run_reconciliation(approval=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

class SingleRefreshPostgresTests(IsolatedPostgresFixture):
    refresh_id = 'ea48b845-e2a3-453b-8225-80a2a362d995'
    correlation_id = '425aebf9-e576-4489-999a-87e52fe1a67d'

    def setUp(self):
        self.sql('DROP SCHEMA IF EXISTS research CASCADE; CREATE SCHEMA research;')
        # Use the actual Flyway column contract, not an independently invented schema.
        source = (ROOT.parents[2] / 'services/research-service/src/main/resources/db/migration/V1__research_intelligence.sql').read_text()
        table = 'CREATE TABLE research.research_refresh_runs (' + source.split('CREATE TABLE research_refresh_runs (', 1)[1].split(');', 1)[0] + ');'
        self.sql(table)
        self.sql(f"""
INSERT INTO research.research_refresh_runs VALUES
('{self.refresh_id}','00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002',
 '2026-09-25 22:56:45.123456+00',NULL,'RUNNING','TARGETED_LIVE',0,0,0,0,0,0,
 '{self.correlation_id}',NULL,NULL,'2026-09-25 22:56:45.654321+00');
INSERT INTO research.research_refresh_runs SELECT 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
 instrument_id,company_id,started_at,started_at,'COMPLETED',mode,1,2,3,4,5,6,
 'unrelated-history',NULL,NULL,updated_at FROM research.research_refresh_runs;
CREATE TABLE research.checkpoint_sentinel (payload jsonb);
INSERT INTO research.checkpoint_sentinel VALUES ('{{"preserve":"all history"}}');
""")
        self.capture()

    def capture(self):
        self.docker('exec', self.container, 'psql', '-X', '-q', '-U', 'postgres',
                    '-d', 'maintenance_synthetic', '-f', '/tmp/capture_refresh_preimages.sql',
                    '-o', '/tmp/reviewed-preimages.csv')
        raw = self.docker('exec', self.container, 'cat', '/tmp/reviewed-preimages.csv').stdout.encode('utf-8')
        self.preimages = json.loads(next(csv.reader(io.StringIO(raw.decode('utf-8'))))[0])
        self.write_raw(raw)

    def write_raw(self, raw, approve_hash=True):
        if approve_hash:
            self.approved_hash = hashlib.sha256(raw).hexdigest()
        self.docker('exec', '-i', self.container, 'sh', '-c',
                    'cat > /tmp/reviewed-preimages.hex', input=raw.hex() + '\n')

    def write_payload(self, payload):
        output = io.StringIO()
        csv.writer(output, lineterminator='\n', quoting=csv.QUOTE_ALL).writerow([payload])
        self.write_raw(output.getvalue().encode('utf-8'))

    def write_preimages(self, rows):
        self.write_payload(json.dumps(rows, ensure_ascii=False))

    def snapshot(self):
        return (self.sql('SELECT jsonb_agg(to_jsonb(r) ORDER BY refresh_run_id) FROM research.research_refresh_runs r;'),
                self.sql('SELECT jsonb_agg(payload ORDER BY payload::text) FROM research.checkpoint_sentinel;'))

    def run_reconciliation(self, **overrides):
        variables = dict(maintenance_approval='APPROVED_SINGLE_REFRESH_TERMINATION_AND_RECONCILIATION',
                         evidence_id='SYNTHETIC_TERMINATION_ONLY', preimage_sha256=self.approved_hash)
        variables.update(overrides)
        args = ['exec', '-w', '/tmp', self.container, 'psql', '-X', '-U', 'postgres', '-d', 'maintenance_synthetic']
        for key, value in variables.items():
            if value is not None:
                args += ['-v', key + '=' + value]
        return self.docker(*args, '-f', '/tmp/reconcile_single_refresh.sql', check=False)

    def assert_rejected(self, error, **overrides):
        before = self.snapshot()
        result = self.run_reconciliation(**overrides)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(error, result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('COMMITTED:', result.stdout)

    def test_success_exact_one_and_unrelated_data_preserved(self):
        before = json.loads(self.snapshot()[0])
        sentinel = self.snapshot()[1]
        result = self.run_reconciliation()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(self.snapshot()[0])
        changed = [(old, new) for old, new in zip(before, after) if old != new]
        self.assertEqual(len(changed), 1)
        old, new = changed[0]
        self.assertEqual(new['refresh_run_id'], self.refresh_id)
        fields = {'status', 'completed_at', 'updated_at', 'safe_error_code', 'safe_error_message'}
        self.assertEqual({k: v for k, v in old.items() if k not in fields},
                         {k: v for k, v in new.items() if k not in fields})
        self.assertEqual(new['status'], 'FAILED')
        self.assertEqual(new['completed_at'], new['updated_at'])
        self.assertEqual(new['safe_error_code'], 'ADMIN_WORKER_TERMINATION_VERIFIED')
        self.assertIn('original failure time and cause unknown', new['safe_error_message'])
        self.assertIn('SYNTHETIC_TERMINATION_ONLY', new['safe_error_message'])
        self.assertEqual(self.snapshot()[1], sentinel)
        self.assertIn(self.approved_hash, result.stdout)
        for marker in ['BEFORE', 'AFTER_PENDING_COMMIT', 'COMMIT', 'COMMITTED:']:
            self.assertIn(marker, result.stdout)

    def test_missing_authorization_and_old_six_record_authorization(self):
        self.assert_rejected('maintenance_approval required', maintenance_approval=None)
        self.assert_rejected('Missing or invalid single-refresh approval',
                             maintenance_approval='APPROVED_REFRESH_TERMINATION_AND_RECONCILIATION')

    def test_missing_or_invalid_evidence_id(self):
        self.assert_rejected('evidence_id required', evidence_id=None)
        for value in ['short', 'spaces are not valid', 'x' * 101, "invalid'; SELECT 1; --"]:
            with self.subTest(value=value):
                self.assert_rejected('Missing or invalid single-refresh approval', evidence_id=value)

    def test_missing_invalid_and_mismatched_sha256(self):
        self.assert_rejected('preimage_sha256 required', preimage_sha256=None)
        self.assert_rejected('Missing or invalid single-refresh approval', preimage_sha256='not-a-hash')
        self.assert_rejected('Original preimage file SHA-256 mismatch', preimage_sha256='0' * 64)

    def test_modified_file_cannot_reuse_approved_hash(self):
        self.write_raw(b'changed capture\n', approve_hash=False)
        self.assert_rejected('Original preimage file SHA-256 mismatch')

    def test_all_17_preimage_columns_fail_closed(self):
        mutations = dict(refresh_run_id=str(uuid.uuid4()), instrument_id=str(uuid.uuid4()), company_id=str(uuid.uuid4()),
            started_at='2026-09-25T22:56:45.123457+00:00', completed_at='2026-09-26T00:00:00+00:00',
            status='FAILED', mode='LIVE', documents_discovered=1, documents_accepted=1, events_extracted=1,
            events_created=1, events_updated=1, deduplicated_count=1, correlation_id=None, safe_error_code='changed',
            safe_error_message='changed', updated_at='2026-09-25T22:56:45.654322+00:00')
        self.assertEqual(len(mutations), 17)
        for key, value in mutations.items():
            with self.subTest(column=key):
                rows = copy.deepcopy(self.preimages)
                rows[0][key] = value
                self.write_preimages(rows)
                self.assert_rejected('Preimage ID set mismatch' if key == 'refresh_run_id' else 'Full 17-column preimage mismatch')

    def test_missing_extra_duplicate_preimages_and_keys(self):
        for value in [[], self.preimages * 2]:
            self.write_preimages(value)
            self.assert_rejected('One full preimage required')
        for key, extra in [('company_id', False), ('extra', True)]:
            rows = copy.deepcopy(self.preimages)
            if extra:
                rows[0][key] = 'unexpected'
            else:
                del rows[0][key]
            self.write_preimages(rows)
            self.assert_rejected('Missing, extra or duplicate preimage columns')
        payload = json.dumps(self.preimages)
        self.write_payload(payload.replace('"status": "RUNNING"', '"status": "RUNNING", "status": "RUNNING"'))
        self.assert_rejected('Missing, extra or duplicate preimage columns')

    def test_invalid_csv_and_hex(self):
        for raw in [b'[]\n', b'"[]",extra\n', b'"[]"\n\n', b'"[]"', b'"[{""bad"":true}]"\n']:
            with self.subTest(raw=raw):
                self.write_raw(raw)
                self.assert_rejected('preimage' if b'bad' in raw else 'CSV')
        self.docker('exec', self.container, 'sh', '-c', "printf 'not-hex\\n' > /tmp/reviewed-preimages.hex")
        self.assert_rejected('Malformed preimage file hex')

    def test_crlf_capture_uses_exact_original_file_hash(self):
        output = io.StringIO()
        csv.writer(output, lineterminator='\r\n').writerow([json.dumps(self.preimages)])
        self.write_raw(output.getvalue().encode('utf-8'))
        result = self.run_reconciliation()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_more_than_one_incomplete_record(self):
        self.sql("UPDATE research.research_refresh_runs SET completed_at=NULL,status='RUNNING' WHERE refresh_run_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';")
        self.assert_rejected('Global incomplete ID set mismatch')

    def test_different_global_incomplete_id(self):
        self.sql(f"UPDATE research.research_refresh_runs SET refresh_run_id='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb' WHERE refresh_run_id='{self.refresh_id}';")
        self.assert_rejected('Global incomplete ID set mismatch')

    def test_reviewed_but_unexpected_status_mode_or_correlation(self):
        for column, value in [('status', 'FAILED'), ('mode', 'LIVE'), ('correlation_id', 'another-correlation')]:
            with self.subTest(column=column):
                self.sql(f"UPDATE research.research_refresh_runs SET {column}='{value}' WHERE refresh_run_id='{self.refresh_id}';")
                self.capture()
                self.assert_rejected('Unexpected refresh lifecycle, correlation or counters')
                original = {'status': 'RUNNING', 'mode': 'TARGETED_LIVE', 'correlation_id': self.correlation_id}[column]
                self.sql(f"UPDATE research.research_refresh_runs SET {column}='{original}' WHERE refresh_run_id='{self.refresh_id}';")

    def test_reviewed_but_nonzero_counters(self):
        for column in ['documents_discovered', 'documents_accepted', 'events_extracted', 'events_created', 'events_updated', 'deduplicated_count']:
            with self.subTest(column=column):
                self.sql(f"UPDATE research.research_refresh_runs SET {column}=1 WHERE refresh_run_id='{self.refresh_id}';")
                self.capture()
                self.assert_rejected('Unexpected refresh lifecycle, correlation or counters')
                self.sql(f"UPDATE research.research_refresh_runs SET {column}=0 WHERE refresh_run_id='{self.refresh_id}';")

    def test_completion_timestamp_is_not_cleared_or_overwritten(self):
        self.sql(f"UPDATE research.research_refresh_runs SET completed_at=started_at,status='COMPLETED' WHERE refresh_run_id='{self.refresh_id}';")
        self.assert_rejected('Global incomplete ID set mismatch')

    def test_unexpected_trigger_even_when_disabled(self):
        self.sql("""CREATE FUNCTION research.unexpected_write() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN INSERT INTO research.checkpoint_sentinel VALUES ('{"unexpected":true}'); RETURN NEW; END $$;
CREATE TRIGGER unexpected BEFORE UPDATE ON research.research_refresh_runs
FOR EACH ROW EXECUTE FUNCTION research.unexpected_write();""")
        self.assert_rejected('Unreviewed triggers/rules')
        self.sql('ALTER TABLE research.research_refresh_runs DISABLE TRIGGER unexpected;')
        self.assert_rejected('Unreviewed triggers/rules')

    def test_unexpected_rule(self):
        self.sql("""CREATE RULE unexpected AS ON UPDATE TO research.research_refresh_runs
DO ALSO INSERT INTO research.checkpoint_sentinel VALUES ('{"unexpected":true}');""")
        self.assert_rejected('Unreviewed triggers/rules')

    def test_schema_changes_fail_closed(self):
        self.sql('ALTER TABLE research.research_refresh_runs ADD COLUMN unexpected text;')
        self.assert_rejected('Unexpected refresh schema')
        self.sql('ALTER TABLE research.research_refresh_runs DROP COLUMN unexpected; ALTER TABLE research.research_refresh_runs ALTER COLUMN status TYPE varchar(41);')
        self.assert_rejected('Unexpected refresh schema')
        self.sql('ALTER TABLE research.research_refresh_runs ALTER COLUMN status TYPE varchar(40); ALTER TABLE research.research_refresh_runs ALTER COLUMN mode DROP NOT NULL;')
        self.assert_rejected('Unexpected refresh schema')

    def test_row_security_or_missing_primary_key_rejected(self):
        self.sql('ALTER TABLE research.research_refresh_runs ENABLE ROW LEVEL SECURITY;')
        self.assert_rejected('ordinary table without row security')
        self.sql('ALTER TABLE research.research_refresh_runs DISABLE ROW LEVEL SECURITY; ALTER TABLE research.research_refresh_runs DROP CONSTRAINT research_refresh_runs_pkey;')
        self.assert_rejected('Unexpected refresh primary key')

    def test_failure_during_update_rolls_back_everything(self):
        self.sql("ALTER TABLE research.research_refresh_runs ADD CONSTRAINT synthetic_failure CHECK(status <> 'FAILED');")
        self.assert_rejected('synthetic_failure')

    def test_repeat_execution_cannot_rewrite_terminal_history(self):
        result = self.run_reconciliation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_rejected('Global incomplete ID set mismatch')

    def test_lock_timeout_rolls_back(self):
        before = self.snapshot()
        process = subprocess.Popen(['docker', 'exec', '-e', 'PGAPPNAME=single_refresh_test_lock', self.container,
            'psql', '-X', '-q', '-U', 'postgres', '-d', 'maintenance_synthetic', '-c',
            'BEGIN; LOCK TABLE research.research_refresh_runs IN ROW EXCLUSIVE MODE; SELECT pg_sleep(9); ROLLBACK;'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(20):
                held = self.sql("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING(pid) WHERE a.application_name='single_refresh_test_lock' AND l.relation='research.research_refresh_runs'::regclass AND l.granted;")
                if held != '0':
                    break
                time.sleep(.1)
            else:
                self.fail('Synthetic lock was not acquired')
            result = self.run_reconciliation()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('lock timeout', result.stderr)
            self.assertNotIn('COMMITTED:', result.stdout)
        finally:
            process.communicate(timeout=20)
        self.assertEqual(self.snapshot(), before)


if __name__=='__main__':
    unittest.main()
