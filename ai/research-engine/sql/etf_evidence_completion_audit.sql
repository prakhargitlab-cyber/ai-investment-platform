-- ETF evidence-completion milestone: bounded, read-only production-data diagnostic.
-- Use with: psql -U investment -d investment -X -v ON_ERROR_STOP=1 -f ...
-- No DML/DDL, no provider network requests, no scheduler, no identity approval.
-- The output is a DIAGNOSTIC, NOT an ETF READY_FRESH decision.
-- Exact readiness remains evaluate_etf_readiness() and its trusted source checks.

BEGIN TRANSACTION READ ONLY;

SELECT 'ETF_COMPLETION_MILESTONE_V1' AS audit,
       current_timestamp AS inspected_at,
       current_date AS database_date,
       'PRESENCE_AND_DATE_DIAGNOSTIC_NOT_APPLICATION_READINESS' AS safety_notice;

-- Global inventories: the only valid denominator is the live ACTIVE NSE ETF
-- canonical master. All research evidence joins on the canonical UUID.
SELECT evidence, observations, distinct_etfs
FROM (
    SELECT 'CANONICAL_ACTIVE_NSE_ETF'::text AS evidence,
           COUNT(*)::bigint AS observations,
           COUNT(DISTINCT instrument_id)::bigint AS distinct_etfs
    FROM portfolio.instrument_master
    WHERE asset_type = 'ETF' AND status = 'ACTIVE' AND primary_exchange = 'NSE'
    UNION ALL
    SELECT 'RESEARCH_NSE_LISTING', COUNT(*), COUNT(DISTINCT instrument_id)
    FROM research.etf_listing_observations WHERE source_mode = 'REAL'
    UNION ALL
    SELECT 'MARKET_PRICE', COUNT(*), COUNT(DISTINCT instrument_id)
    FROM research.etf_fact_observations
    WHERE source_mode = 'REAL' AND metric = 'MARKET_PRICE'
    UNION ALL
    SELECT 'TRADING_VOLUME', COUNT(*), COUNT(DISTINCT instrument_id)
    FROM research.etf_fact_observations
    WHERE source_mode = 'REAL' AND metric = 'TRADING_VOLUME'
    UNION ALL
    SELECT 'NAV', COUNT(*), COUNT(DISTINCT instrument_id)
    FROM research.etf_nav_observations WHERE source_mode = 'REAL'
    UNION ALL
    SELECT 'UNDERLYING_HOLDINGS', COUNT(*), COUNT(DISTINCT instrument_id)
    FROM research.etf_holdings_snapshots WHERE source_mode = 'REAL'
    UNION ALL
    SELECT metric, COUNT(*), COUNT(DISTINCT instrument_id)
    FROM research.etf_fact_observations
    WHERE source_mode = 'REAL' AND metric IN (
        'EXPENSE_RATIO', 'AUM', 'TRACKING_ERROR', 'TRACKING_DIFFERENCE',
        'BENCHMARK_INDEX', 'AMC_FUND_HOUSE'
    )
    GROUP BY metric
) evidence_counts
ORDER BY evidence;

-- One row per actual ACTIVE NSE ETF; no hardcoded ticker, scheme code or ISIN.
-- No fabricated dates: a formatted ISO text date is only a *diagnostic* range
-- hint, and records with unknown/malformed dates never count as recent.
-- Real readiness is strictly more conservative (publication age, provenance,
-- acquisition conflicts, evidence precedence, and derived requirements).
WITH universe AS (
    SELECT instrument_id, isin, primary_symbol AS symbol
    FROM portfolio.instrument_master
    WHERE asset_type = 'ETF' AND status = 'ACTIVE' AND primary_exchange = 'NSE'
),
listing AS (
    SELECT c.instrument_id,
        COUNT(l.evidence_id) AS listing_rows,
        COUNT(l.evidence_id) FILTER (
            WHERE l.instrument_id = c.instrument_id::text
              AND l.isin = c.isin
              AND l.symbol = c.symbol
              AND l.source_mode = 'REAL'
              AND l.payload::jsonb #>> '{provenance,provider}' = 'NSE'
              AND l.payload::jsonb #>> '{provenance,authority}' = 'OFFICIAL_EXCHANGE'
              AND l.payload::jsonb #>> '{provenance,source_mode}' = 'REAL'
        ) AS official_pairs
    FROM universe c
    LEFT JOIN research.etf_listing_observations l
      ON l.instrument_id = c.instrument_id::text
         OR l.isin = c.isin
         OR l.symbol = c.symbol
    GROUP BY c.instrument_id
),
facts AS (
    SELECT instrument_id,
        COUNT(*) FILTER (WHERE metric = 'MARKET_PRICE') AS price_rows,
        COUNT(*) FILTER (WHERE metric = 'TRADING_VOLUME') AS volume_rows,
        COUNT(*) FILTER (
          WHERE metric = 'MARKET_PRICE'
            AND as_of_date ~ '^\d{4}-\d{2}-\d{2}$'
            AND as_of_date BETWEEN to_char(CURRENT_DATE - 1, 'YYYY-MM-DD')
                               AND to_char(CURRENT_DATE, 'YYYY-MM-DD')
        ) AS recent_price_rows,
        COUNT(*) FILTER (
          WHERE metric = 'TRADING_VOLUME'
            AND as_of_date ~ '^\d{4}-\d{2}-\d{2}$'
            AND as_of_date BETWEEN to_char(CURRENT_DATE - 1, 'YYYY-MM-DD')
                               AND to_char(CURRENT_DATE, 'YYYY-MM-DD')
        ) AS recent_volume_rows,
        COUNT(*) FILTER (WHERE metric = 'EXPENSE_RATIO') AS ter_rows,
        COUNT(*) FILTER (WHERE metric = 'AUM') AS aum_rows,
        COUNT(*) FILTER (WHERE metric = 'TRACKING_ERROR') AS te_rows,
        COUNT(*) FILTER (WHERE metric = 'TRACKING_DIFFERENCE') AS td_rows
    FROM research.etf_fact_observations
    WHERE source_mode = 'REAL'
    GROUP BY instrument_id
),
nav AS (
    SELECT instrument_id, COUNT(*) AS nav_rows
    FROM research.etf_nav_observations
    WHERE source_mode = 'REAL'
    GROUP BY instrument_id
),
holdings AS (
    SELECT instrument_id, COUNT(*) AS holding_rows
    FROM research.etf_holdings_snapshots
    WHERE source_mode = 'REAL'
    GROUP BY instrument_id
),
attempts AS (
    SELECT instrument_id,
       MAX(attempted_at) FILTER (
          WHERE reason LIKE 'NSE_METADATA_IDENTITY_CONFLICT%'
       ) AS latest_conflict,
       MAX(attempted_at) FILTER (
          WHERE reason = 'NSE_METADATA_IDENTITY_VERIFIED'
            AND outcome = 'SUCCESS_WITH_DATA'
       ) AS latest_verified
    FROM (
      SELECT instrument_id, attempted_at, outcome,
             payload::jsonb ->> 'reason' AS reason
      FROM research.etf_acquisition_attempts
      WHERE metric = 'IDENTITY' AND provider = 'NSE'
    ) a
    GROUP BY instrument_id
),
classified AS (
    SELECT u.instrument_id, u.isin, u.symbol,
        CASE
          WHEN COALESCE(l.listing_rows, 0) <> 1 OR COALESCE(l.official_pairs, 0) <> 1
            THEN 'IDENTITY_MISSING_CONFLICT_OR_UNTRUSTED'
          WHEN a.latest_conflict IS NOT NULL
            AND (a.latest_verified IS NULL OR a.latest_verified <= a.latest_conflict)
            THEN 'IDENTITY_METADATA_CONFLICT'
          WHEN COALESCE(f.recent_price_rows, 0) = 0
            THEN 'PRICE_MISSING_OR_NOT_RECENT'
          WHEN COALESCE(f.recent_volume_rows, 0) = 0
            THEN 'VOLUME_MISSING_OR_NOT_RECENT'
          ELSE 'MANDATORY_EVIDENCE_PRESENT_RECENT_NOT_READINESS_VERIFIED'
        END AS diagnostic_gate,
        COALESCE(f.price_rows, 0) AS price_rows,
        COALESCE(f.volume_rows, 0) AS volume_rows,
        COALESCE(n.nav_rows, 0) AS nav_rows,
        COALESCE(h.holding_rows, 0) AS holdings_rows,
        COALESCE(f.ter_rows, 0) AS ter_rows,
        COALESCE(f.aum_rows, 0) AS aum_rows,
        COALESCE(f.te_rows, 0) AS tracking_error_rows,
        COALESCE(f.td_rows, 0) AS tracking_difference_rows
    FROM universe u
    LEFT JOIN listing l ON l.instrument_id = u.instrument_id
    LEFT JOIN facts f ON f.instrument_id = u.instrument_id::text
    LEFT JOIN nav n ON n.instrument_id = u.instrument_id::text
    LEFT JOIN holdings h ON h.instrument_id = u.instrument_id::text
    LEFT JOIN attempts a ON a.instrument_id = u.instrument_id::text
)
SELECT
    diagnostic_gate,
    COUNT(*) AS etfs,
    COUNT(*) FILTER (WHERE nav_rows = 0) AS nav_missing,
    COUNT(*) FILTER (WHERE holdings_rows = 0) AS holdings_missing,
    COUNT(*) FILTER (WHERE ter_rows = 0) AS ter_missing,
    COUNT(*) FILTER (WHERE aum_rows = 0) AS aum_missing,
    COUNT(*) FILTER (WHERE tracking_error_rows = 0) AS tracking_error_missing,
    COUNT(*) FILTER (WHERE tracking_difference_rows = 0) AS tracking_difference_missing
FROM classified
GROUP BY diagnostic_gate
ORDER BY diagnostic_gate;

-- Build an actionable and bounded shortlist directly from canonical rows.
-- Missing supporting metrics are reported as missing, NEVER assumed unavailable.
-- An ETF may be otherwise price-ready but not safe for a research recommendation.
WITH u AS (
    SELECT instrument_id, isin, primary_symbol
    FROM portfolio.instrument_master
    WHERE asset_type = 'ETF' AND status = 'ACTIVE' AND primary_exchange = 'NSE'
),
p AS (
    SELECT instrument_id,
      COUNT(*) FILTER (WHERE metric='MARKET_PRICE') AS price_rows,
      COUNT(*) FILTER (WHERE metric='TRADING_VOLUME') AS volume_rows,
      COUNT(*) FILTER (WHERE metric='EXPENSE_RATIO') AS ter_rows,
      COUNT(*) FILTER (WHERE metric='AUM') AS aum_rows,
      COUNT(*) FILTER (WHERE metric='TRACKING_ERROR') AS tracking_rows
    FROM research.etf_fact_observations
    WHERE source_mode='REAL'
    GROUP BY instrument_id
),
h AS (
    SELECT instrument_id, count(*) AS holding_rows
    FROM research.etf_holdings_snapshots WHERE source_mode='REAL'
    GROUP BY instrument_id
)
SELECT u.primary_symbol AS ticker, u.isin, u.instrument_id,
    CASE WHEN COALESCE(p.price_rows,0)=0 OR COALESCE(p.volume_rows,0)=0
      THEN 'P0_MANDATORY_MARKET_DATA'
      ELSE 'P1_OFFICIAL_FUND_EVIDENCE' END AS next_workstream,
    concat_ws(',',
       CASE WHEN COALESCE(p.price_rows,0)=0 THEN 'MARKET_PRICE' END,
       CASE WHEN COALESCE(p.volume_rows,0)=0 THEN 'TRADING_VOLUME' END,
       CASE WHEN COALESCE(h.holding_rows,0)=0 THEN 'UNDERLYING_HOLDINGS' END,
       CASE WHEN COALESCE(p.ter_rows,0)=0 THEN 'EXPENSE_RATIO' END,
       CASE WHEN COALESCE(p.aum_rows,0)=0 THEN 'AUM' END,
       CASE WHEN COALESCE(p.tracking_rows,0)=0 THEN 'TRACKING_ERROR' END
    ) AS missing_evidence
FROM u
LEFT JOIN p ON p.instrument_id=u.instrument_id::text
LEFT JOIN h ON h.instrument_id=u.instrument_id::text
ORDER BY CASE WHEN COALESCE(p.price_rows,0)=0 OR COALESCE(p.volume_rows,0)=0
                  THEN 0 ELSE 1 END,
         u.primary_symbol, u.instrument_id
LIMIT 25;

COMMIT;
