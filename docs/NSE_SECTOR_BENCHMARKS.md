# NSE canonical sector benchmarks and price history

## Identity and classification

Baseline: `076215b5ee757061357aa6a71e5a6e85988d8baa` (OHLCV technical features).
The audited master contained 2,568 equities and no indices. Stock classification
comes from `portfolio.nifty500_universe.canonical_sector`, populated by
`Nifty500ReferenceService` and `NiftyIndustrySectorClassifier` from the official
Nifty 500 constituent file. The persisted source is `NSE_INDICES_NIFTY500`.
Portfolio/watchlist membership is irrelevant.

`SECTOR_BENCHMARK_MAPPING_V1` resolves exact canonical classifications to platform
benchmark keys. `NSE_BENCHMARK_CATALOG_V1` records the authoritative registration
evidence. These are **price indices**, not total-return series.

| Canonical classification | Platform key | Canonical UUID | Verified NSE request symbol |
|---|---|---|---|
| India broad market | INDIA_BROAD_PRICE | 56623ea0-224c-3481-b6d9-66c448b282a0 | NIFTY 500 |
| Technology | INDIA_TECHNOLOGY_PRICE | 4af7634a-1894-333d-ba68-a1c41a32c71c | NIFTY IT |
| Financials | INDIA_FINANCIALS_PRICE | 13ba7849-43e2-3e1e-a3f3-d93ee32d3d2f | NIFTY FINANCIAL SERVICES |
| Healthcare | INDIA_HEALTHCARE_PRICE | 055c4e9f-5592-3be6-8f87-3d05bb25b95a | NIFTY HEALTHCARE INDEX |

UUIDs use Java `UUID.nameUUIDFromBytes(UTF8("aip:benchmark:" + platformKey))`;
the Python adapter implements the identical UUIDv3 operation. A calculated UUID
alone is **not** registration or identity evidence. Both acquisition and context
resolution require the actual master row and exactly one verified NSE mapping.
Provider symbols never identify stocks or benchmark instruments.

The existing master service now registers `AssetType.INDEX` through explicit
`POST /api/v1/instruments/benchmarks/{benchmarkKey}/register`. It uses the existing
transaction/advisory-lock, master, and mapping repositories. Unknown keys and
conflicting identities fail. Repeating a registration is idempotent. The existing
VARCHAR asset-type column accepts INDEX; no migration is required or supplied.
`GET /api/v1/instruments/benchmarks` only reads registered identities and mappings.
It does not register instruments or acquire data. Authentication follows the
existing global-instrument controller convention.

Acquisition requires ACTIVE / INDEX / IN / NSE / INR master metadata and exactly
one NSE mapping with VERIFIED status, matching exchange/currency/request symbol,
and catalog resolution source. Inactive, missing, conflicting, or untrusted
identity fails closed. Context construction also requires trusted India universe
membership before assigning the India broad benchmark.

## Mapping evidence and exclusions

- [Nifty 500](https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-500)
  is the broad benchmark: its large/mid/small-cap coverage matches the platform's
  existing Nifty 500 classification universe. There is no Nifty 50 fallback.
- [Nifty IT](https://www.niftyindices.com/indices/equity/sectoral-indices/nifty-it)
  matches the classifier's Information Technology → Technology mapping.
- [Nifty Financial Services](https://www.niftyindices.com/indices/equity/sectoral-indices/nifty-financial-services)
  covers banks and other financial services, matching Financial Services →
  Financials. A bank-only index would exclude part of this classification.
- [Nifty Healthcare](https://www.niftyindices.com/indices/equity/sectoral-indices/nifty-healthcare-index)
  matches Healthcare; a pharma-only substitution is not made.

The official NSE `/api/allIndices` catalog and the captured history responses
confirmed these four index contracts on 2026-09-13. Fixtures retain those responses.

Observed unmapped classifications: Communication Services (14), Consumer
Discretionary (88), Consumer Staples (28), Energy (17), Industrials (75), Materials
(55), Real Estate (11), Utilities (17); 17 records lacked classification. Mapped
counts were Financials 101, Healthcare 48, Technology 27. These counts describe
the audit snapshot, not an ongoing invariant. Broad compound classifications
are not assigned to narrower indices. Raw `FINANCIAL SERVICES`, `Bank`, and other
aliases are not accepted by the mapping adapter; classification must first come
from the canonical classifier. Duplicate classification evidence fails closed.

## Official history contract

History version: `NSE_INDEX_HISTORY_V1`.

`GET https://www.nseindia.com/api/historicalOR/indicesHistory`
with `indexType=<verified mapping>`, `from=DD-MM-YYYY`, `to=DD-MM-YYYY`.
Bootstrap: `https://www.nseindia.com/report-detail/eq_security`.
The older `/api/historical/indicesHistory` returned HTTP 200 HTML during discovery
and is deliberately not used. The equity security CSV endpoint is not reused.

The index provider composes the existing NSE provider's transport/session,
browser headers, cookies, lock, request spacing and bounded retry behavior; it
does not call its equity identity gate or CSV parser. One transport/session is
reused sequentially per explicit benchmark population invocation and closed in
`finally`. There is no global cookie pool, parallel fetch, proxy, or bypass.
This small composition uses the existing private transport methods; a future
transport refactor must preserve the tested shared-session contract.

JSON is `{ "data": [ ... ] }`. Required fields:
`EOD_INDEX_NAME`, `EOD_TIMESTAMP`, `EOD_OPEN_INDEX_VAL`, `EOD_HIGH_INDEX_VAL`,
`EOD_LOW_INDEX_VAL`, `EOD_CLOSE_INDEX_VAL`.

Verified response-name aliases, confined to this parser:

| Requested symbol | EOD_INDEX_NAME |
|---|---|
| NIFTY 500 | NIFTY 500 |
| NIFTY IT | NIFTY IT |
| NIFTY FINANCIAL SERVICES | NIFTY FIN SERVICE |
| NIFTY HEALTHCARE INDEX | NIFTY HEALTHCARE |

Case/whitespace normalization is explicit; no other symbol equivalence is inferred.
`EOD_TIMESTAMP` (for example `11-SEP-2026`) is the exchange-local DATE.
`HI_TIMESTAMP` can be the previous UTC day and is never used for trading dates.
JSON decimals are decoded directly to Decimal; domain OHLC validation applies.
Missing fields, malformed values, out-of-window dates, duplicate dates, or wrong
index names reject the complete response before persistence. Empty responses and
HTTP 200 HTML are explicit failures, not zero-valued observations.

`HIT_TRADED_QTY` and `HIT_TURN_OVER` were observed, but their index aggregation and
units are not normalized in this phase. Volume, turnover, and previous close
remain null. Actual OHLC values are index points; INR is the trusted index market
denomination, not an assertion that each point is a tradable currency amount.

## Population and persistence

An explicit worker calls `IndiaMarketDataPopulationJobs.populate_benchmark_history`
with registered canonical IDs and one bounded inclusive start/end window. It
uses `ResearchRepository.upsert_daily_market_bars_async` via `persist_daily_result`.
Primary identity remains `(global_instrument_id, trading_date, provider='NSE')`.
Corrections update that key; another provider remains separate. No deletes,
fabricated OHLC, or close-history dual-writes occur.

The existing `nse_historical_request_window_days` defaults to 30 inclusive
calendar days. This is an operational bound, **not an NSE guaranteed maximum**.
Invocations are capped by `market_data_population_batch_size`, ordered by UUID,
and persist per successful window. This explicit bounded operation does not
schedule a broad backfill or automatically extend lookbacks.

Spacing uses `market_data_population_request_interval_seconds` (default 0.20).
The existing default two retries apply to transient network/5xx/429 failures;
ordinary 4xx, including 403, are not retried. Exponential backoff and Retry-After
handling are inherited. Failure cooldown uses the existing population cooldown;
429 also inhibits subsequent instruments in the worker. Cooldowns are process
local, like the existing population jobs. Results reuse `NseHistoricalResult`,
including HTTP status, failure reason, accepted/rejected counts and persisted rows.

## Computation and batching

Prepare contexts with
`await orchestrator.sector_benchmark_contexts(candidate_ids, identity_headers=...)`,
then pass them to `GlobalScanner.enrich_candidates(..., sector_contexts=contexts)`.
This keeps the existing explicit Stage-B dependency boundary. No dashboard,
watchlist, portfolio, scanner GET, or feature computation invokes acquisition.

The context adapter reads the existing paged canonical universe and one benchmark
catalog response. Portfolio-service batches master and mapping reads. Stage B
collects distinct canonical stock/benchmark IDs and performs the existing bounded
close-history read plus one NSE daily-bar read, grouping in memory. With the
default batch bound, history SELECT counts are 0 for empty, 2 for one stock,
2 for 18 same-sector stocks, and 2 for 18 stocks across the three mapped sectors.
Canonical metadata preparation is separate from these history query counts.

The existing SectorRelativeStrengthEngine consumes persisted NSE daily closes
with DATE semantics, selecting a coherent provider series. Existing close-only
fallback remains supported. Stock short/stale OHLC fallback follows the existing
technical input policy; individual dates/providers are never spliced together.

Lookbacks preserve the existing 5 / 21 / 63 / 126 **stock observations** for
1W / 1M / 3M / 6M. Both exact stock endpoints must exist in the benchmark history.
Returns are `(end / start - 1) * 100`. Weekends, holidays, and missing intermediate
dates are not synthesized. No forward/back fill, nearest-date matching, or
calendar-session guessing is performed. A missing endpoint makes that horizon
unavailable; at least two horizons are needed for the existing score/state rules.
Thresholds, score weights, Technical Features V2 and Rule Engine V1 are unchanged.

Snapshots expose mapping version, canonical benchmark IDs, history source and
explicit benchmark states: NO_SECTOR_CLASSIFICATION, UNMAPPED_SECTOR_BENCHMARK,
BENCHMARK_IDENTITY_UNAVAILABLE, BENCHMARK_HISTORY_UNAVAILABLE,
INSUFFICIENT_OVERLAP, STALE_BENCHMARK_HISTORY, AVAILABLE. Per-horizon diagnostics
remain available even when another horizon has overlap. Missing evidence is never
zero or a weak-sector conclusion. Existing seven-day freshness policy remains.

## Controlled runtime validation and limits

One four-day Nifty 500 probe established the JSON contract. Subsequently just
four 30-day requests (2026-08-13 through 2026-09-11, one per registered index)
returned HTTP 200 and 22 rows each. No all-sector or all-equity backfill ran.
The captured responses were replayed locally without additional provider calls.

The real master service registered/read back all four identities in the existing
H2 test schema; its public metadata fed the Python runtime. The existing repository
persisted 88 captured rows to local SQLite. Repeat upserts left 88 rows. Real stock
classification and close history were copied read-only from local PostgreSQL:

| Stock | Verified canonical sector | Actual overlapping dates | Result |
|---|---|---:|---|
| HDFCBANK | Financials | 22 | Sector and market evidence available |
| SUNPHARMA | Healthcare | 22 | Sector and market evidence available |
| TCS | Technology | 22 | Sector and market evidence available |
| LT | Industrials | 22 with market | Sector explicitly unmapped |

Repeated/reversed-input results matched, with networking blocked during compute.
The small benchmark capture supports short horizons only; 3M/6M formulas are
validated independently in tests, not claimed as available from this smoke.
These outputs are engineering diagnostics, not production investment conclusions.

The deployed PostgreSQL instance lacks the pre-existing daily-bar table. No
migration was created/applied, and no benchmark was directly inserted into that
master by SQL. Deployment of the prior daily-bar schema plus this canonical
registration path remains necessary before deployed acquisition can work.

Unresolved: unmapped sectors, deployed PostgreSQL validation, Global Opportunity
Ranker, short/long action model, recommendation history/lifecycle, news/macro,
prediction/backtesting. None is implemented by this slice.

## Validation results

Final research-engine suite: **992 passed**, one existing dependency deprecation
warning. Included: 64 new benchmark tests, 21 sector-relative tests, 25 technical
tests, 42 OHLCV tests, 35 daily-bar persistence tests, 45 backfill tests, 55 NSE
equity provider tests, 22 population tests, 31 Phase-1/batch scanner tests, and
29 Rule Engine V1 tests.

Java benchmark registration/instrument-master tests: 19 passed, including the
H2 registration/read-back integration test. The broader portfolio reactor run
reported 194 tests with two failures: `CanonicalIdentityBootstrapTest` assumes
an empty shared H2 database (passes when rerun alone), and
`AppUserProvisionerTest` expects an obsolete conflict target/SQL shape. Their
implementation/test files are unchanged by this phase; no unrelated fixes were
included. The benchmark H2 test rolls its writes back after exporting public
metadata for the runtime smoke.

`git diff --check` passed. No changes to Technical Features V2, Rule Engine V1,
NSE equity provider/backfill, frontend, or migrations. `platform.ps1` is unchanged
by this phase (phase-start SHA256
`4577DA738957A6C35A8035812586344E76A557DFE41A7355F5A4D5F3E9D2E3AF`).
Nothing staged, committed, or pushed. `smtp.password` was not opened.
