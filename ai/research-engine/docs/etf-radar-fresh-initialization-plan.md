# ETF Radar: fresh-initialization implementation plan (no backfill)

Status 2026-11-05. Strategy: reset ETF Radar-owned research data, then run one normal full ETF cycle.
No historical backfill runner, incremental backfill job or manual backfill endpoint exists or is planned.

## Scope
1. Removed: `etf_history_backfill` runner + tests, runner-only fence hooks on daily-bar writes
   (`persistence.py`, `nse_historical_daily.py`, `market_data_population.py`, `repository.py` restored to HEAD).
   Kept (shared): `etf_history.py`, `etf_history_selection.py`, `etf_history_read.py`, `nse_daily_backfill.py`
   (existing), daily-bar read methods used by the history API/chart.
2. Product: UI redesign, historical chart, Research Universe fix, recommendation gating, eight-phase progress (done, tested).
3. The normal cycle persists history (HISTORY_ONBOARDING -> `global_daily_market_bars`, default 400 days) and NSE
   identity/price/volume/NAV evidence (EVIDENCE_VALIDATION). Chart ranges beyond stored depth show only stored data.

## Evidence acquisition status (no fabrication; missing = explicit UNAVAILABLE)
| Field | Acquired by normal cycle today | Gap / next step |
|---|---|---|
| Identity, price, volume, NAV | Yes (NSE official) | NAV 0 is treated as unavailable |
| History | Yes (Yahoo provider, validated bars) | depth is a product decision (400d vs 3Y/5Y) |
| AUM, expense ratio, tracking error/difference | No: only 2 reviewed Nippon PDFs (OTP-gated, never fetched) | Needs an approved source. Candidate: AMFI monthly TER, tracking error/difference (ISIN + scheme code), quarterly AAUM. Endpoints/formats not verified: sandbox cannot reach amfiindia.com. Step: approved read-only sample capture on the Windows host, then parsers with exact-ISIN matching only |
| Fund metadata | canonical name only | same AMFI/AMC sources, exact ISIN |
| Bid/ask spread, benchmark series, holdings | no writer / parser | not obtainable from verified sources yet; stay unavailable |
No parser is written from library docs; none guesses identity from names.

## Reset
`scripts/database/maintenance/etf-radar-reset/` (README = inventory, guards, procedure, rollback). Operator script, not a
Flyway migration; dry-run default; scoped export/restore; tested on disposable PostgreSQL 16.

## Verification (sandbox)
- Backend: 1149 passed (etf_*, sector benchmarks, checkpoint recovery), 281 in the targeted subset; PG suites 37 passed.
  Known failures unchanged from baseline: `test_nse_etf_failures_preserve_history_and_identity_on_postgres` and the
  equity-side / missing-fixture set.
- Schema init: 25 migrations apply on PG16; `_assert_schema_available` passes after reset.
- Reset/restore harness and PG reset-then-rebuild test pass. Frontend previously 450/450, tsc clean, build OK.

## Not done / needs approval
Reset execution, full cycle, AMFI sample capture, P2 (SBI identity), commit/merge/deploy (normalize CRLF, 3 commits, explicit paths).

## Update 2026-11-05 (evening): sources, identity, five-year history

### Source-coverage matrix
| Field | Source | Status | Identity key | Notes |
|---|---|---|---|---|
| Identity, price, volume, NAV | NSE (official) | acquired by cycle | NSE symbol + ISIN | unchanged |
| Fund house | AMFI NAVAll (`portal.amfiindia.com/spages/NAVAll.txt`) | **implemented** (`etf_amfi_navall.py`, flag `research_etf_amfi_navall_enabled`, default off) | exact ISIN, ETF category only | host-captured 2026-11-05; 369 ETF rows, no duplicate ISIN; FoF schemes named "ETF" are excluded by category |
| AMFI scheme code | NAVAll | stored in fact `source_metadata` | exact ISIN | join key for TER |
| Expense ratio (TER) | AMFI `www.amfiindia.com/api/populate-te-rdata-revised` (JSON, paginated; scheme code, no ISIN) | **not implemented** | ISIN -> NAVAll scheme code -> TER scheme code | endpoint found via WebFetch only; `www.amfiindia.com` times out from the host, so no host sample/headers; need a capture before a parser |
| Tracking error / difference | AMFI `/api/tracking-error-data`, `/api/tracking-difference` | **blocked by identity** | none (name + benchmark + fund-house id only) | cannot be attached without name matching, which is prohibited; requires an approved exact-identifier source (AMC disclosure) or an approved name rule |
| AUM / AAUM | AMFI quarterly AAUM | not verified | unknown | endpoint/format not confirmed |
| Bid/ask spread | NSE quote API (intraday order book) | not acquired | symbol | point-in-time only, meaningless outside market hours; not host-verified |
| Benchmark history | NSE Indices (niftyindices.com reachable from host) | not acquired | ETF "Underlying" text -> index name needs reviewed mapping | programmatic endpoint unverified (probe returned an HTML page); terms of use unreviewed; gold/silver have no NSE index |
| Holdings | per-AMC monthly portfolio files | not acquired | ISIN per AMC | formats differ per AMC; no common official aggregate |
| Inception date | AMC SID/factsheet | not acquired | ISIN | NSE listing date is not inception and is not used as such |

### History
Onboarding target is five years (`ETF_HISTORY_TARGET_DAYS=1830`), clamped to the canonical `listingDate` when known, requested in
sequential windows of at most 731 days, oldest first; bars are persisted per window, so interrupted cycles resume from stored sessions.
A leading gap is not re-requested once at least one year is stored (unless a known listing date shows a >14 day gap). Full-cycle
request/time budgets were raised (3000 requests / 7200 s). Limitation: an existing store that already holds >= 1 year without a known
listing date will not be extended backwards; a reset + fresh cycle (planned) or a known listing date does that.
Gap: `listingDate` is currently populated only when the canonical row carries it.

### TER / AUM source investigation (2026-11-05, host-verified from `portal.amfiindia.com` unless stated)
| Source | Format and identity | Verdict |
|---|---|---|
| `portal.amfiindia.com/spages/NAVAll.txt` | text; scheme code + ISIN + fund house + category | in use (exact ISIN) |
| `portal.amfiindia.com/DownloadSchemeData_Po.aspx?mf=0` | CSV (HTTP 200, 4.1 MB, 16,485 rows, 10 fields; header's last two ISIN columns are merged into one); AMC, scheme code, type, category, ISIN, Launch Date, Closure Date. 375 ETF-category rows, no duplicate codes | **not implemented**. Useful for AMC legal name and a launch date, but "Launch Date" is the NFO opening date and "Closure Date" the NFO close (16 ETF rows have unparseable dates), so it is not fund inception and must not be labelled as such |
| `portal.amfiindia.com/rssNAV.aspx?fwise=y`, `AUMReport_Rpt_Po.aspx` | AMC-level quarterly AAUM only | not usable for per-ETF AUM |
| `www.amfiindia.com/api/average-aum-schemewise` (per amfipy source, not host-verified) | scheme name + category + AAUM, **no scheme code/ISIN** | not attachable without name matching |
| `www.amfiindia.com/api/populate-te-rdata-revised` (TER) | JSON, scheme code, no ISIN | the only identity-bearing TER found, but `www.amfiindia.com` does not respond from the host network (three 20 s timeouts), so no sample/format verification |
| AMC TER workbooks (e.g. SBI) | scheme names / NSDL codes, no ISIN | identity gap as documented in `sbi-ter-identity-evidence-gate.md` |
No TER or AUM parser was written. Decision needed: save a real AMFI TER JSON from a browser that can reach `www.amfiindia.com`, or approve an
"exact AMFI-published scheme-name equality within the same AMC" rule for AMC-published TER/AAUM files.

AMFI failures are bounded: `_acquire_fund_identity` records a per-instrument TECHNICAL_FAILURE attempt for any provider error (timeout,
HTTP error, malformed file, unexpected exception) and the cycle continues; only lease loss/cancellation propagate.
