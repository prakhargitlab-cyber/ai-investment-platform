# Market Data

Phase 2B defines the real market-data connectivity boundary without claiming a live quote source.

## Provider Order

1. Broker-supported market data after official provider docs and read-only validation exist.
2. Secondary documented and permitted source.
3. Stale cache.
4. Mock quotes only when demo mode is enabled.

## Freshness Vocabulary

- `REAL_TIME`
- `DELAYED`
- `END_OF_DAY`
- `STALE`
- `MOCK`, displayed as `DEMO`
- `UNAVAILABLE`

## Current Implementation

`FallbackMarketDataProvider` is the primary portfolio-service provider. It checks usable cached quotes first, exposes expired cache entries as `STALE`, returns mock quotes only when `MARKET_DATA_DEMO_MODE=true`, and otherwise returns `UNAVAILABLE` with source `NoVerifiedMarketDataProvider`.

The base runtime default is `MARKET_DATA_DEMO_MODE=false`. DEV and test profiles may explicitly enable demo mode. Cached `MOCK` quotes are ignored when demo mode is disabled, so a real/non-demo runtime cannot silently reuse fake prices.

Quotes carry `source`, `sourceTimestamp`, `receivedAt`, and `freshness`. The older `timestamp` field remains the quote/source timestamp for API compatibility.

Redis stores deterministic fresh and stale keys:

- `market:quote:{instrumentId}`
- `market:quote:stale:{instrumentId}`

No arbitrary quote website scraping is implemented. No paid API is configured.

## Validation Status

- Real market-data source validated: NO
- Broker market data validated: NO
- Mock fallback available in DEV/demo mode: YES
