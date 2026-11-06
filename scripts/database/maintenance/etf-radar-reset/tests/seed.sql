-- Entirely synthetic seed for tests/bootstrap.sql in a DISPOSABLE database.
\set ON_ERROR_STOP on
SET search_path = research;
INSERT INTO market_trading_schedules VALUES
  ('NSE','XNSE','IN','Asia/Kolkata',1,'09:15','15:30'),
  ('NYSE','XNYS','US','America/New_York',1,'09:30','16:00');
INSERT INTO market_trading_calendar_exceptions VALUES
  ('NSE','2026-11-10','CLOSED',NULL,NULL,'{"synthetic":true,"reason":"test holiday"}'),
  ('NYSE','2026-11-27','SPECIAL_HOURS','09:30','13:00',NULL);
INSERT INTO etf_listing_observations VALUES
  ('l1','NSE','INF000000001','AAAETF','11111111-1111-1111-1111-111111111111','ETF','2026-11-01T00:00:00Z','DEMO','{"synthetic":true}'),
  ('unbound-listing','NSE','INF000000004','UNBOUND',NULL,'ETF','2026-11-01T00:00:00Z','DEMO','{"unresolved":true}');
INSERT INTO etf_fact_observations VALUES
  ('f1','11111111-1111-1111-1111-111111111111','AUM','2026-10-31','SYNTHETIC','fixture','OFFICIAL',NULL,'2026-11-01T00:00:00Z','DEMO','{"synthetic":true,"value":"123.4500"}'),
  ('f2','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','EXPENSE_RATIO','2026-10-31','SYNTHETIC','fixture','OFFICIAL',NULL,'2026-11-01T00:00:00Z','DEMO','{"synthetic":true,"value":"0.15"}'),
  ('research-only','  AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA  ','AUM',NULL,'SYNTHETIC','fixture','OFFICIAL',NULL,'2026-11-01T00:00:00Z','DEMO','{"research_only":true}');
INSERT INTO etf_nav_observations VALUES
  ('n1','11111111-1111-1111-1111-111111111111','100.5000','INR','2026-10-31','SYNTHETIC','fixture','OFFICIAL',NULL,'2026-11-01T00:00:00Z','DEMO','{"synthetic":true,"nav":"100.5000"}');
INSERT INTO etf_holdings_snapshots VALUES
  ('h1','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','2026-10-31','SYNTHETIC','fixture','OFFICIAL',NULL,'2026-11-01T00:00:00Z','DEMO','{"synthetic":true,"count":2}');
INSERT INTO etf_holdings_positions VALUES ('h1',1,'{"synthetic":true,"weight":"0.2"}'),('h1',2,'{"synthetic":true,"weight":null}');
INSERT INTO etf_acquisition_attempts VALUES
  ('a1','11111111-1111-1111-1111-111111111111','AUM','SYNTHETIC','2026-11-01T00:00:00Z','UNAVAILABLE','{"synthetic":true}'),
  ('null-discovery',NULL,'DISCOVERY','SYNTHETIC','2026-11-01T00:00:00Z','SUCCESS_WITH_DATA','{"synthetic":true,"unbound":true}'),
  ('blank-discovery','   ','DISCOVERY','SYNTHETIC','2026-11-01T00:00:00Z','SUCCESS_EMPTY','{}'),
  ('whitespace-discovery',E'\t\r\n ','DISCOVERY','SYNTHETIC','2026-11-01T00:00:00Z','SUCCESS_EMPTY','{}');
INSERT INTO etf_radar_cycles VALUES ('c-etf-1','synthetic-v1','corr','2026-11-01','{"synthetic":true,"outcomes":[1,2]}','2026-11-01T00:00:00Z');
INSERT INTO global_opportunity_cycle_run(cycle_id,market,status,parameters,selection,created_at,updated_at) VALUES
  ('c-etf-1','ETF','COMPLETED','{"synthetic":true,"scope":"FULL_UNIVERSE"}','{"candidate_ids":["11111111-1111-1111-1111-111111111111"]}','2026-11-01','2026-11-01'),
  ('c-eq-1','IN','COMPLETED','{"synthetic":true,"scope":"EQUITY"}',NULL,'2026-11-01','2026-11-01');
INSERT INTO global_opportunity_cycle_progress(cycle_id,phase,global_instrument_id,state,attempts,payload,updated_at) VALUES
  ('c-etf-1','ETF_ONBOARD','11111111-1111-1111-1111-111111111111','COMPLETED',1,'{"kept":false}','2026-11-01'),
  ('c-eq-1','BASELINE','99999999-9999-9999-9999-999999999999','COMPLETED',2,'{"kept":true,"nullable":null}','2026-11-01');
INSERT INTO global_opportunity_cycle_active VALUES ('ETF','c-etf-1'),('IN','c-eq-1');
INSERT INTO global_daily_market_bars VALUES
  ('11111111-1111-1111-1111-111111111111','2026-10-30',1,2,1,2,1,10,20,'INR','NSE','AAAETF','DEMO','https://synthetic.invalid/fixture','2026-11-01T00:00:00Z'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','2026-10-30',1,3,1,3,NULL,20,60,'INR','NSE','BBBETF','DEMO','https://synthetic.invalid/fixture','2026-11-01T00:00:00Z'),
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','2026-10-30',NULL,NULL,NULL,9,NULL,NULL,NULL,'INR','NSE','RESEARCHONLY','DEMO','https://synthetic.invalid/fixture','2026-11-01T00:00:00Z'),
  ('99999999-9999-9999-9999-999999999999','2026-10-30',2,5,1,4,NULL,33,132,'INR','YAHOO','EQ.NS','DEMO','https://synthetic.invalid/fixture','2026-11-01T00:00:00Z');
DO $seed_equity$
DECLARE table_name text; id_column text;
BEGIN
  FOR table_name, id_column IN SELECT * FROM (VALUES
    ('global_financial_facts','instrument_id'),('global_shareholding_snapshots','instrument_id'),
    ('global_stock_rule_engine_results','global_instrument_id'),('research_events','instrument_id'),
    ('research_refresh_runs','instrument_id'),('research_news_search_runs','instrument_id'),
    ('research_event_impact_features','instrument_id'),('company_business_exposure_profiles','instrument_id'),
    ('global_opportunity_snapshot','global_instrument_id'),('global_stock_suggestion','global_instrument_id'),
    ('global_stock_suggestion_current','global_instrument_id'),('global_stock_suggestion_history','global_instrument_id'),
    ('stock_recommendation_history','global_instrument_id'),('recommendation_current_state','global_instrument_id')
  ) AS guard_tables(table_name,id_column)
  LOOP
    EXECUTE format('INSERT INTO research.%I(row_key,%I,payload) VALUES ($1,$2,$3)',table_name,id_column)
      USING 'equity-seed','99999999-9999-9999-9999-999999999999'::uuid,'{"synthetic":true,"keep":true,"nullable":null}'::jsonb;
  END LOOP;
END
$seed_equity$;
INSERT INTO research_documents VALUES
  ('dddddddd-dddd-dddd-dddd-dddddddddd01','11111111-1111-1111-1111-111111111111','shared-content',NULL,'{"synthetic":true,"etf":true}'),
  ('dddddddd-dddd-dddd-dddd-dddddddddd02','99999999-9999-9999-9999-999999999999','shared-content',NULL,'{"synthetic":true,"equity":true}'),
  ('dddddddd-dddd-dddd-dddd-dddddddddd03',NULL,'unbound',NULL,NULL);
UPDATE global_shareholding_snapshots SET research_document_id='dddddddd-dddd-dddd-dddd-dddddddddd01';
UPDATE research_events SET source_document_id='dddddddd-dddd-dddd-dddd-dddddddddd01';
UPDATE research_event_impact_features SET source_document_id='dddddddd-dddd-dddd-dddd-dddddddddd01';
INSERT INTO research_event_sources VALUES ('shared-dependent','dddddddd-dddd-dddd-dddd-dddddddddd01','{"keep":true}');
DO $seed_shared$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['global_manual_evidence','global_market_price_observations','global_structured_market_snapshots','research_acquisition_observations'] LOOP
    EXECUTE format('INSERT INTO research.%I VALUES ($1,$2,$3),($4,$5,$6)',table_name)
      USING 'etf-shared','11111111-1111-1111-1111-111111111111'::uuid,'{"synthetic":true,"preserved":"ETF"}'::jsonb,
            'equity-shared','99999999-9999-9999-9999-999999999999'::uuid,'{"synthetic":true,"preserved":"EQUITY","nullable":null}'::jsonb;
  END LOOP;
END
$seed_shared$;
