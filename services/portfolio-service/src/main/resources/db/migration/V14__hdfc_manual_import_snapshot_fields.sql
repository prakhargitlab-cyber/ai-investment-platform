ALTER TABLE manual_import_holding_snapshots ALTER COLUMN isin DROP NOT NULL;
ALTER TABLE manual_import_holding_snapshots ADD COLUMN long_term_quantity NUMERIC(38, 18);
ALTER TABLE manual_import_holding_snapshots ADD COLUMN total_pnl NUMERIC(38, 18);
ALTER TABLE manual_import_holding_snapshots ADD COLUMN today_pnl NUMERIC(38, 18);
