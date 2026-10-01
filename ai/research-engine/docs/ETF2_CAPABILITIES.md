# ETF-2 capability audit and durable evidence contract

This audit is based on existing repository code and fixtures, not live provider
calls. Parser support does not prove availability for every ETF. No universe
crawl, deployment, DEV migration, readiness, scoring or production dispatch is
part of ETF-2.

## Existing infrastructure inspected

- Portfolio service `OfficialNseEtfSecurityListClient.listAll/parseAll` and
  `OfficialNseEtfSecurityListClientTest`: official `eq_etfseclist.csv`, with
  Symbol, SecurityName, ISINNumber and optional Underlying. DateofListing is a
  listing date, not fund inception. Underlying can be an asset or opaque name,
  not a canonical index identifier.
- `InstrumentMasterEntity.normalizeIsin`, `GlobalInstrumentController` and
  `portfolio_orchestration.PortfolioServiceClient` global metadata paths:
  canonical UUID and normalized ISIN exist. Python's universe search is
  equity-filtered. No existing generic ETF import endpoint is exposed there.
- `structured_market.YahooFinanceProvider`, `FIELD_MAP`, `_normalize_facts`,
  `tests/test_structured_market.py`: price, volume, bid, ask, explicit navPrice.
  **navPrice gets market_as_of, which is the quote time, not proven NAV time.**
- `repository._update_etf_facts/_extract_etf_facts`: legacy in-memory TER,
  AUM and NAV text extraction. Fund/index inference and named-company holdings
  heuristics are not suitable for authoritative durable facts and are not reused.
- Existing registered-source fetching, document normalization and search in
  `research_fetching`, `source_discovery`, and `repository`: document transport
  exists; search snippets and domain guesses do not establish fund evidence.
- `nse_historical_daily.NseHistoricalDailyProvider`: dated OHLCV transport;
  no NAV/fund attributes, and ETF-universe coverage is not demonstrated by its
  equity fixtures. Historical prices are never NAV.
- `yahoo_mcp_acquisition`: existing stock-oriented external tool contracts,
  including company ownership; no proven ETF portfolio holdings contract.

## Field findings

| Field | Finding | ETF-2 adapter scope |
|---|---|---|
| IDENTITY | AUTHORITATIVE_AVAILABLE | Official NSE list; exact-ISIN ETF master binding |
| BENCHMARK_INDEX | NOT_PROVEN | Preserve raw Underlying separately; do not invent a benchmark |
| NAV | SECONDARY_AVAILABLE numeric value; authoritative/date contract NOT_PROVEN | Dedicated durable NAV model/store; no acquisition from Yahoo quote timestamps |
| AUM | NOT_PROVEN for NSE/AMC coverage | Validated official document parser for existing EUR/USD/GBP million/billion forms; unknown effective date remains unknown |
| EXPENSE_RATIO / TER | NOT_PROVEN for universe coverage | Explicit percent labels in validated official documents |
| MARKET_PRICE | SECONDARY_AVAILABLE | Existing Yahoo quote adapter, exact ETF identity and quote date required |
| TRADING_VOLUME | SECONDARY_AVAILABLE | Existing Yahoo volume field; explicit zero retained |
| BID_ASK_SPREAD | DERIVABLE from bid/ask; authoritative ETF quotes NOT_PROVEN | Store explicit bid/ask; no derived spread implemented |
| TRACKING_ERROR | NOT_IMPLEMENTED | Schema only; no acquisition or computation |
| TRACKING_DIFFERENCE | NOT_IMPLEMENTED | Schema only; no acquisition or computation |
| AMC / FUND_HOUSE | NOT_PROVEN authoritative metadata coverage | Schema only; legacy name inference excluded |
| FUND_INCEPTION_DATE | NOT_PROVEN | Schema only; never use listing date |
| REPLICATION_METHOD | NOT_PROVEN | Schema only |
| UNDERLYING_HOLDINGS | NOT_IMPLEMENTED | Dedicated snapshots/positions; no ownership reuse or guessed constituent mapping |
| Subtype / asset class | ETF-1 metadata classifier available | Durable fact types; no inference from ticker/list name |

## Persistence and reuse

Flyway **V20** adds six ETF-only tables: fact observations, NAV observations,
holdings snapshots, holdings positions, listing observations and acquisition
attempts. The Python persistence mixin is inherited by the existing PostgreSQL
adapter. SQLite tests use the equivalent schema. No startup assertion for the
new tables is added to the stock runtime before deployment of V20.

Payloads preserve exact decimal strings and all optional provenance. Content
hashes include instrument, metric/period, provider, source identity, value and
provenance, excluding repeated retrieval time. Equivalent decimal scales and
holding order have the same identity. Corrections append revisions; duplicates
do not modify original evidence. NAV and market-price rows cannot share a model
or table. Evidence and acquisition outcomes commit atomically.

Selection is official exchange/fund > other authoritative > secondary, then
effective date, publication time, retrieval time and deterministic revision ID.
Stale primary evidence is not displaced by fresh secondary evidence. REAL and
DEMO reads are separated. Secondary reuse requires an explicit opt-in.

Calendar-day maximum ages are 1 day for quotes/NAV, 35 for AUM/holdings/tracking,
90 for TER, and 365 for slow-changing metadata. Missing effective dates produce
UNKNOWN_AS_OF; retrieval or failed-check time never substitutes for them.
The pure read/reuse method does not create a provider attempt. These are ETF
evidence reuse policies, not readiness or scoring thresholds.

Outcomes distinguish data, valid empty, explicit authoritative non-disclosure,
technical failure, and unsupported acquisition. Missing fields or unsupported
interfaces never establish genuine unavailability. Explicit non-disclosure is
scoped to its retained official source and does not claim exhaustive absence
across every provider. Transport and parse errors are technical failures.

## ETF-3 prerequisites and limits

Readiness can consume the evidence contracts, but must account for unsupported
coverage and UNKNOWN_AS_OF. Before claiming complete authoritative ETF research,
prove dated NAV, dated AUM/TER, benchmark semantics and liquidity coverage.
An instrument-master import/resolution integration is still needed for new
NSE listings: discovery persists unresolved official identity without inventing
a master UUID or mutating an equity record. V20 must be applied through the
normal deployment process before PostgreSQL ETF methods are used. No migrations
are applied to DEV in this phase. No derived metric is implemented.
