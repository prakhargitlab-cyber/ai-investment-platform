-- Synthetic, disposable-only fixtures; NOT application migrations.
\set ON_ERROR_STOP on
DO $bootstrap$
BEGIN
  IF current_database() <> 'etf_reset_disposable_test' THEN
    RAISE EXCEPTION 'TEST_DATABASE_NAME_REQUIRED';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'research') THEN
    RAISE EXCEPTION 'TEST_REQUIRES_FRESH_DATABASE_WITHOUT_RESEARCH_SCHEMA';
  END IF;
END
$bootstrap$;
CREATE SCHEMA research;
SET search_path = research;
CREATE TABLE etf_fact_observations (
  evidence_id text PRIMARY KEY, instrument_id text NOT NULL, metric text NOT NULL,
  as_of_date text, provider text NOT NULL, source_identity text NOT NULL,
  authority text NOT NULL, published_at text, retrieved_at text NOT NULL,
  source_mode text NOT NULL, payload text NOT NULL
);
CREATE TABLE etf_nav_observations (
  evidence_id text PRIMARY KEY, instrument_id text NOT NULL, nav text NOT NULL,
  currency text NOT NULL, nav_date text NOT NULL, provider text NOT NULL,
  source_identity text NOT NULL, authority text NOT NULL, published_at text,
  retrieved_at text NOT NULL, source_mode text NOT NULL, payload text NOT NULL
);
CREATE TABLE etf_holdings_snapshots (
  evidence_id text PRIMARY KEY, instrument_id text NOT NULL, as_of_date text NOT NULL,
  provider text NOT NULL, source_identity text NOT NULL, authority text NOT NULL,
  published_at text, retrieved_at text NOT NULL, source_mode text NOT NULL, payload text NOT NULL
);
CREATE TABLE etf_holdings_positions (
  snapshot_id text NOT NULL REFERENCES etf_holdings_snapshots(evidence_id),
  position_number integer NOT NULL, payload text NOT NULL,
  PRIMARY KEY (snapshot_id, position_number)
);
-- Nullable IDs mirror the production discovery/listing contract.
CREATE TABLE etf_listing_observations (
  evidence_id text PRIMARY KEY, exchange text NOT NULL CHECK (exchange = 'NSE'),
  isin text NOT NULL, symbol text NOT NULL, instrument_id text,
  asset_type text NOT NULL CHECK (asset_type = 'ETF'), retrieved_at text NOT NULL,
  source_mode text NOT NULL, payload text NOT NULL
);
CREATE TABLE etf_acquisition_attempts (
  attempt_id text PRIMARY KEY, instrument_id text, metric text NOT NULL,
  provider text NOT NULL, attempted_at text NOT NULL, outcome text NOT NULL, payload text NOT NULL
);
CREATE TABLE etf_radar_cycles (
  cycle_id text PRIMARY KEY, radar_version text NOT NULL, correlation_id text,
  as_of text NOT NULL, payload text NOT NULL, created_at text NOT NULL
);
CREATE TABLE global_opportunity_cycle_run (
  cycle_id text PRIMARY KEY, market text NOT NULL,
  status text NOT NULL CHECK (status IN ('ACCEPTED','RUNNING','PUBLISHED','CANCEL_REQUESTED','COMPLETED','FAILED','CANCELLED')),
  parameters text NOT NULL, as_of text, selection text, owner_id text, lease_expires_at text,
  resume_count integer NOT NULL DEFAULT 0, error_code text, created_at text NOT NULL, updated_at text NOT NULL
);
CREATE TABLE global_opportunity_cycle_active (
  market text PRIMARY KEY, cycle_id text NOT NULL REFERENCES global_opportunity_cycle_run(cycle_id)
);
CREATE TABLE global_opportunity_cycle_progress (
  cycle_id text NOT NULL REFERENCES global_opportunity_cycle_run(cycle_id),
  phase text NOT NULL CHECK (phase IN ('BASELINE','DEEP','ETF_ONBOARD')),
  global_instrument_id text NOT NULL,
  state text NOT NULL CHECK (state IN ('PENDING','IN_PROGRESS','COMPLETED','RETRYABLE_FAILURE','EVIDENCE_UNAVAILABLE','TERMINAL_OUTCOME')),
  disposition text, failure_reason text, attempts integer NOT NULL DEFAULT 0, payload text, updated_at text NOT NULL,
  PRIMARY KEY (cycle_id, phase, global_instrument_id)
);
CREATE TABLE global_daily_market_bars (
  global_instrument_id uuid NOT NULL, trading_date date NOT NULL,
  open_price numeric, high_price numeric, low_price numeric, close_price numeric NOT NULL,
  previous_close numeric, volume numeric, turnover numeric, currency text NOT NULL,
  provider text NOT NULL, provider_symbol text NOT NULL, source_mode text NOT NULL,
  source_url text NOT NULL, retrieved_at text NOT NULL,
  PRIMARY KEY (global_instrument_id, trading_date, provider)
);
CREATE TABLE market_trading_schedules (
  market_code text NOT NULL, mic text, country_code text, timezone text NOT NULL,
  trading_day integer NOT NULL, regular_open_time time NOT NULL, regular_close_time time NOT NULL,
  PRIMARY KEY (market_code, trading_day)
);
CREATE TABLE market_trading_calendar_exceptions (
  market_code text NOT NULL, trading_date date NOT NULL, exception_type text NOT NULL,
  open_time time, close_time time, payload jsonb,
  PRIMARY KEY (market_code, trading_date)
);
-- Equity/shared fixtures model only columns needed by the reset and document
-- review. No providers, application lifecycle or application migrations run.
CREATE TABLE research_documents (
  document_id uuid PRIMARY KEY, instrument_id uuid, content_hash text,
  duplicate_of_document_id uuid REFERENCES research_documents(document_id), payload jsonb
);
DO $tables$
DECLARE table_name text; id_column text;
BEGIN
  FOR table_name, id_column IN SELECT * FROM (VALUES
    ('global_financial_facts','instrument_id'),
    ('global_shareholding_snapshots','instrument_id'),
    ('global_stock_rule_engine_results','global_instrument_id'),
    ('research_events','instrument_id'),
    ('research_refresh_runs','instrument_id'),
    ('research_news_search_runs','instrument_id'),
    ('research_event_impact_features','instrument_id'),
    ('company_business_exposure_profiles','instrument_id'),
    ('global_opportunity_snapshot','global_instrument_id'),
    ('global_stock_suggestion','global_instrument_id'),
    ('global_stock_suggestion_current','global_instrument_id'),
    ('global_stock_suggestion_history','global_instrument_id'),
    ('stock_recommendation_history','global_instrument_id'),
    ('recommendation_current_state','global_instrument_id')
  ) AS guard_tables(table_name,id_column)
  LOOP
    EXECUTE format('CREATE TABLE research.%I (row_key text PRIMARY KEY, %I uuid NOT NULL, payload jsonb)',table_name,id_column);
  END LOOP;
END
$tables$;
ALTER TABLE global_shareholding_snapshots ADD COLUMN research_document_id uuid REFERENCES research_documents(document_id);
ALTER TABLE research_events ADD COLUMN source_document_id uuid REFERENCES research_documents(document_id);
ALTER TABLE research_event_impact_features ADD COLUMN source_document_id uuid REFERENCES research_documents(document_id);
CREATE TABLE research_event_sources (row_key text PRIMARY KEY, document_id uuid REFERENCES research_documents(document_id), payload jsonb);
CREATE TABLE global_manual_evidence (row_key text PRIMARY KEY, instrument_id uuid NOT NULL, payload jsonb);
CREATE TABLE global_market_price_observations (row_key text PRIMARY KEY, instrument_id uuid NOT NULL, payload jsonb);
CREATE TABLE global_structured_market_snapshots (row_key text PRIMARY KEY, instrument_id uuid NOT NULL, payload jsonb);
CREATE TABLE research_acquisition_observations (row_key text PRIMARY KEY, instrument_id uuid NOT NULL, payload jsonb);
-- No Flyway stand-in: absence is a required initial state.
