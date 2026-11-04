-- Final ETF evidence closure dossier — one read-only PostgreSQL operation.
-- Resolves actionable evidence gaps using stored observed facts/attempts.
-- Never asserts external source authenticity or approves an alias/ISIN.
BEGIN TRANSACTION READ ONLY;

WITH active AS (
    SELECT instrument_id, isin, primary_symbol AS ticker
    FROM portfolio.instrument_master
    WHERE asset_type='ETF' AND status='ACTIVE'
      AND primary_exchange='NSE'
),
latest_attempt AS (
    SELECT DISTINCT ON (instrument_id, metric)
      instrument_id, metric, outcome, provider, attempted_at,
      payload::jsonb ->> 'reason' AS reason
    FROM research.etf_acquisition_attempts
    WHERE metric IN ('IDENTITY', 'MARKET_PRICE', 'TRADING_VOLUME', 'NAV')
    ORDER BY instrument_id, metric, attempted_at DESC
),
evidence AS (
    SELECT instrument_id,
       COUNT(*) FILTER (WHERE metric='MARKET_PRICE') AS price_rows,
       COUNT(*) FILTER (WHERE metric='TRADING_VOLUME') AS volume_rows,
       COUNT(*) FILTER (WHERE metric='MARKET_PRICE'
         AND as_of_date ~ '^\d{4}-\d{2}-\d{2}$'
         AND as_of_date BETWEEN to_char(CURRENT_DATE - 1, 'YYYY-MM-DD')
                            AND to_char(CURRENT_DATE, 'YYYY-MM-DD')) AS recent_price,
       COUNT(*) FILTER (WHERE metric='TRADING_VOLUME'
         AND as_of_date ~ '^\d{4}-\d{2}-\d{2}$'
         AND as_of_date BETWEEN to_char(CURRENT_DATE - 1, 'YYYY-MM-DD')
                            AND to_char(CURRENT_DATE, 'YYYY-MM-DD')) AS recent_volume
    FROM research.etf_fact_observations
    WHERE source_mode='REAL' AND metric IN ('MARKET_PRICE', 'TRADING_VOLUME')
    GROUP BY instrument_id
),
identity AS (
    SELECT instrument_id,
      COUNT(*) AS listing_count,
      COUNT(DISTINCT (isin, symbol)) AS identity_pair_count
    FROM research.etf_listing_observations WHERE source_mode='REAL'
    GROUP BY instrument_id
),
reasons AS (
    SELECT c.instrument_id, c.ticker, c.isin,
      COALESCE(e.price_rows, 0) AS price_rows,
      COALESCE(e.volume_rows, 0) AS volume_rows,
      COALESCE(e.recent_price, 0) AS recent_price,
      COALESCE(e.recent_volume, 0) AS recent_volume,
      COALESCE(i.listing_count, 0) AS listing_rows,
      COALESCE(i.identity_pair_count, 0) AS listing_identity_pairs,
      id.outcome AS identity_last_outcome,
      id.reason AS identity_last_reason,
      id.attempted_at AS identity_last_attempt,
      px.outcome AS price_last_outcome,
      px.reason AS price_last_reason,
      px.attempted_at AS price_last_attempt,
      vol.outcome AS volume_last_outcome,
      vol.reason AS volume_last_reason,
      vol.attempted_at AS volume_last_attempt
    FROM active c
    LEFT JOIN evidence e ON e.instrument_id=c.instrument_id::text
    LEFT JOIN identity i ON i.instrument_id=c.instrument_id::text
    LEFT JOIN latest_attempt id ON id.instrument_id=c.instrument_id::text
                                AND id.metric='IDENTITY'
    LEFT JOIN latest_attempt px ON px.instrument_id=c.instrument_id::text
                                AND px.metric='MARKET_PRICE'
    LEFT JOIN latest_attempt vol ON vol.instrument_id=c.instrument_id::text
                                 AND vol.metric='TRADING_VOLUME'
)
SELECT
   ticker, isin, instrument_id,
   listing_rows, listing_identity_pairs,
   price_rows, volume_rows,
   recent_price, recent_volume,
   identity_last_outcome, identity_last_reason, identity_last_attempt,
   price_last_outcome, price_last_reason, price_last_attempt,
   volume_last_outcome, volume_last_reason, volume_last_attempt
FROM reasons
WHERE (recent_price = 0 OR recent_volume = 0
   OR (identity_last_reason LIKE 'NSE_METADATA_IDENTITY_CONFLICT%'
       AND identity_last_outcome='TECHNICAL_FAILURE'))
ORDER BY ticker, instrument_id
LIMIT 500;

-- No rows are created or updated. Proof of scheme-code-to-ISIN is
-- NOT derivable from this table and must come from official fund documents.
COMMIT;
