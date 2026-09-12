ALTER TABLE portfolio_positions ADD COLUMN imported_price_amount NUMERIC(38, 18);
ALTER TABLE portfolio_positions ADD COLUMN imported_price_currency VARCHAR(3);
ALTER TABLE portfolio_positions ADD COLUMN imported_market_value_amount NUMERIC(38, 18);
ALTER TABLE portfolio_positions ADD COLUMN imported_market_value_currency VARCHAR(3);
ALTER TABLE portfolio_positions ADD COLUMN imported_unrealized_pnl_amount NUMERIC(38, 18);
ALTER TABLE portfolio_positions ADD COLUMN imported_unrealized_pnl_currency VARCHAR(3);
ALTER TABLE portfolio_positions ADD COLUMN imported_unrealized_pnl_percent NUMERIC(38, 18);

ALTER TABLE portfolio_positions ALTER COLUMN current_price_amount DROP NOT NULL;
ALTER TABLE portfolio_positions ALTER COLUMN current_price_currency DROP NOT NULL;

UPDATE portfolio_positions
SET imported_price_amount = CASE WHEN current_price_amount > 0 THEN current_price_amount ELSE NULL END,
    imported_price_currency = CASE WHEN current_price_amount > 0 THEN current_price_currency ELSE NULL END,
    imported_market_value_amount = market_value_amount,
    imported_market_value_currency = market_value_currency,
    imported_unrealized_pnl_amount = unrealized_profit_loss_amount,
    imported_unrealized_pnl_currency = unrealized_profit_loss_currency,
    current_price_amount = NULL,
    current_price_currency = NULL,
    market_value_amount = NULL,
    market_value_currency = NULL,
    unrealized_profit_loss_amount = NULL,
    unrealized_profit_loss_currency = NULL
WHERE source_type = 'MANUAL_CSV_IMPORT';
