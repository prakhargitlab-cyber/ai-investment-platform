-- Synthetic seed for the DISPOSABLE test database only.
SET search_path = research;
INSERT INTO market_trading_schedules(market_code,mic,country_code,timezone,trading_day,regular_open_time,regular_close_time) VALUES ('NSE','XNSE','IN','Asia/Kolkata',1,'09:15','15:30') ON CONFLICT DO NOTHING;
-- ETF ids: 11111111-..., 22222222-...; equity id: 99999999-...
INSERT INTO etf_listing_observations(evidence_id,exchange,isin,symbol,instrument_id,asset_type,retrieved_at,source_mode,payload) VALUES
 ('l1','NSE','INF000000001','AAAETF','11111111-1111-1111-1111-111111111111','ETF','2026-11-01T00:00:00Z','REAL','{}');
INSERT INTO etf_fact_observations VALUES ('f1','11111111-1111-1111-1111-111111111111','AUM','2026-10-31','p','s','OFFICIAL',NULL,'2026-11-01T00:00:00Z','REAL','{}'),
 ('f2','22222222-2222-2222-2222-222222222222','EXPENSE_RATIO','2026-10-31','p','s','OFFICIAL',NULL,'2026-11-01T00:00:00Z','REAL','{}');
INSERT INTO etf_nav_observations VALUES ('n1','11111111-1111-1111-1111-111111111111',100.5,'INR','2026-10-31','p','s','OFFICIAL',NULL,'2026-11-01T00:00:00Z','REAL','{}');
INSERT INTO etf_holdings_snapshots VALUES ('h1','22222222-2222-2222-2222-222222222222','2026-10-31','p','s','OFFICIAL',NULL,'2026-11-01T00:00:00Z','REAL','{}');
INSERT INTO etf_holdings_positions VALUES ('h1',1,'{"a":1}'),('h1',2,'{"a":2}');
INSERT INTO etf_acquisition_attempts VALUES ('a1','11111111-1111-1111-1111-111111111111','AUM','p','2026-11-01T00:00:00Z','UNAVAILABLE','{}');
INSERT INTO etf_radar_cycles VALUES ('c-etf-1','v1','corr','2026-11-01','{}','2026-11-01T00:00:00Z');
INSERT INTO global_opportunity_cycle_run(cycle_id,market,status,parameters,created_at,updated_at) VALUES
 ('c-etf-1','ETF','COMPLETED','{}','2026-11-01','2026-11-01'),('c-eq-1','IN','COMPLETED','{}','2026-11-01','2026-11-01');
INSERT INTO global_opportunity_cycle_progress(cycle_id,phase,global_instrument_id,state,updated_at) VALUES
 ('c-etf-1','ETF_ONBOARD','11111111-1111-1111-1111-111111111111','COMPLETED','2026-11-01'),
 ('c-eq-1','BASELINE','99999999-9999-9999-9999-999999999999','COMPLETED','2026-11-01');
INSERT INTO global_opportunity_cycle_active(market,cycle_id) VALUES ('IN','c-eq-1');
INSERT INTO global_daily_market_bars(global_instrument_id,trading_date,open_price,high_price,low_price,close_price,previous_close,volume,turnover,currency,provider,provider_symbol,source_mode,source_url,retrieved_at) VALUES
 ('11111111-1111-1111-1111-111111111111','2026-10-30',1,2,1,2,1,10,20,'INR','YAHOO','AAAETF.NS','REAL','u','2026-11-01T00:00:00Z'),
 ('22222222-2222-2222-2222-222222222222','2026-10-30',1,2,1,2,1,10,20,'INR','YAHOO','BBBETF.NS','REAL','u','2026-11-01T00:00:00Z'),
 ('99999999-9999-9999-9999-999999999999','2026-10-30',1,2,1,2,1,10,20,'INR','YAHOO','EQ.NS','REAL','u','2026-11-01T00:00:00Z');
