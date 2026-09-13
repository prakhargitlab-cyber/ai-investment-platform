# NSE historical daily acquisition

Integration: `IndiaMarketDataPopulationJobs.populate_daily_bars(global_instrument_id,
start=date, end=date, identity_headers=...)` is an explicit, single-instrument
worker entry point. It resolves current portfolio-service metadata, applies the
existing trusted provider-mapping gate plus active equity/ambiguity/currency
checks, and calls `ResearchRepository.upsert_daily_market_bars_async`.
It is deliberately not called by submit, ensure, scanners, or GET handlers.
The existing `HistoricalPriceProvider.closes` protocol and Yahoo priority remain
close-only. No dual-write occurs.

## Observed contract

`tests/fixtures/nse_historical_daily.csv` is the unmodified successful official
NSE response captured on 2026-09-13, after a normal cookie-bearing bootstrap at
https://www.nseindia.com/report-detail/eq_security (HTTP 200).

The canonical store was checked at runtime: globalInstrumentId
`f8cb0fc7-082c-4d95-a77d-b1a9ca21d5a4`, POLYCAB, ACTIVE EQUITY, NSE, IN, INR;
NSE mapping VERIFIED, resolution source OFFICIAL_NSE_NIFTY500.

One historical request, one window, no retries:
https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData?from=01-09-2026&to=04-09-2026&symbol=POLYCAB&type=priceVolumeDeliverable&series=EQ&csv=true

HTTP 200, text/csv, 910 bytes, four rows; both requested boundaries were present.
Observed headers (surrounding whitespace omitted): Symbol, Series, Date,
Prev Close, Open Price, High Price, Low Price, Last Price, Close Price,
Average Price, Total Traded Quantity, Turnover ₹, No. of Trades,
Deliverable Qty, % Dly Qt to Traded Qty.

Required: Date, Symbol, Series, Open/High/Low/Close (the explicit ` Price`
aliases are supported). Optional: Prev Close, Total Traded Quantity, Turnover ₹.
Headers are trimmed, case-folded, and whitespace-collapsed; duplicate normalized
headers fail the response. Symbols and series are trimmed and uppercased only.
Dates accept DD-MM-YYYY and observed DD-Mon-YYYY with an explicit English month
map, without locale or UTC conversion. Numeric grouping accepts Western and
Indian comma grouping; prices never pass through float. Missing optional cells
(blank or `-`) remain null. Duplicate valid dates reject all contenders.

TURNOVER_UNIT: VERIFIED for the observed `Turnover ₹` header (INR).
TURNOVER_CONVERSION: remove validated grouping commas, parse Decimal directly;
multiplier 1, and only with trusted INR currency metadata. For example,
`13,02,54,81,530.00` becomes Decimal(`13025481530.00`). Unobserved turnover
headers, including `Turnover (in Lacs)`, remain unavailable/null; no lakh
conversion is implemented or claimed verified.

## Operational policy

`nse_historical_request_window_days` defaults to 30 inclusive calendar days.
This is an operational bound, not an NSE guaranteed maximum. Larger requests
fail before metadata/network calls; this slice does not split or backfill them.
`nse_historical_max_retries` defaults to 2 (validated range 0–3).
The existing `market_data_population_request_interval_seconds` governs all
bootstrap/history attempts; ordinary httpx cookie storage and supported content
decoders are used. One provider serializes its requests; the population entry
point serializes calls and applies spacing between sessions. No distributed
rate limiter is claimed; use the existing single worker deployment convention.

403/404 and other non-429 4xx fail without retry. 429 and 5xx, timeouts and network
errors have bounded exponential backoff (1s, 2s, capped at 8s). Retry-After is
honored; when it exceeds 30 seconds this call fails without retrying early.
Cookies and request headers are never logged by this module. Results expose
HTTP status, identity, bounds, provenance, retrieval time, counts, date coverage,
row rejection reasons, and explicit acquisition/persistence failure reasons.
Failures never delete previously persisted evidence.

## Runtime persistence validation

The same captured live response (no second history request) was parsed and four
bars were persisted/read back through the current ResearchRepository and
SqliteResearchPersistence at `.tmp/nse-runtime.sqlite`. DATE, OHLC range, integer
volume, provider, symbol, source URL and exact model roundtrip were checked.
The deployed research-engine does not yet contain DailyMarketBar, so deployed
PostgreSQL end-to-end validation was not possible without a separate deployment.
No deployment or migration was performed. The local runtime database is ignored
and is not part of the change.
