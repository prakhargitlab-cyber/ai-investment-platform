ALTER TABLE portfolio_positions ADD COLUMN market_value_amount NUMERIC(19, 4);
ALTER TABLE portfolio_positions ADD COLUMN market_value_currency VARCHAR(3);
ALTER TABLE portfolio_positions ADD COLUMN unrealized_profit_loss_amount NUMERIC(19, 4);
ALTER TABLE portfolio_positions ADD COLUMN unrealized_profit_loss_currency VARCHAR(3);
