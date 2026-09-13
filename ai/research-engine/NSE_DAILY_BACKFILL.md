# Bounded NSE daily OHLCV backfill

## Entry point and universe

`IndiaMarketDataPopulationJobs.backfill_daily_bars(identity_headers=...,
correlation_id=None, offset=0, instrument_ids=None, start=None, end=None,
force=False)` is an explicit async worker operation. It shares the existing
daily-bar lock with single-instrument acquisition. No GET, scanner, submit,
ensure, or scheduler automatically invokes it. Yahoo close-only behavior stays
unchanged. This extends the population boundary, not a second job framework.

The source is `active_global_equities()` (paginated canonical ACTIVE EQUITY
metadata). India/NSE candidates are deduplicated and sorted by UUID string;
at most `market_data_population_batch_size` (default 50) are selected.
Each selected candidate then resolves current detail metadata and passes the
committed `verified_identity` gate before NSE acquisition. Invalid mappings
consume a bounded batch slot and appear as failures; they are never fetched.
Listing metadata uses `exchange`; detail metadata uses `primaryExchange`, as
specified by the existing canonical API. No holdings/watchlists/sector filter.

Optional IDs restrict canonical membership; IDs absent from the universe are
not acquired. `next_offset` is null at completion, otherwise the next offset in
the sorted candidate universe or explicit-ID intersection. Selection is stable
for the same snapshot. Universe membership changes can shift offsets; no durable
snapshot cursor is claimed. Only identity metadata is enumerated globally;
history is read one instrument at a time and persisted per window.

## Date and coverage policy

Default interval: Asia/Kolkata current DATE minus existing initial lookback
(default 400 days), through current DATE inclusive. Optional bounds may narrow
but not extend that horizon or request future dates. Retrieval timestamps remain
UTC. `plan_windows` produces consecutive non-overlapping oldest-first windows,
using the configured provider limit (default 30 inclusive calendar days). A
401-calendar-date interval uses 14 windows. Thirty days is an operational bound,
not a claimed official NSE maximum.

Reads reuse `daily_market_bars_for_instruments({id}, end_date=end, provider='NSE')`.
Only the canonical ID, current symbol/currency, REAL NSE rows with non-null OHLC
satisfy coverage. Optional volume/turnover are not required.

- NO_HISTORY: fetch the target interval.
- PARTIAL_HISTORY: fetch the missing prefix before the earliest usable date;
  also fetch a stale tail when needed.
- STALE_HISTORY: retrieval age or latest-date lag exceeds configured historical
  freshness (72 hours). Fetch latest date + 1 through target end. If latest date
  already equals target end, refresh that final date for corrections/freshness.
- CURRENT_HISTORY: observed prefix is covered and tail sufficiently fresh; skip.
  This does not assert that every internal exchange session is present.
- GAP_DETECTED is deliberately not inferred without authoritative sessions.

INTERNAL_GAP_REPAIR = UNSUPPORTED_IN_THIS_PHASE. Weekly schedules and optional
calendar exceptions do not establish a complete historical NSE holiday calendar.
No weekday/holiday is synthesized or labelled missing market data.

Successful request ranges are remembered for the existing freshness interval
within the worker, keyed by canonical ID/symbol/currency. This suppresses repeated
boundary probes when a completed range has no bar on its exact first date.
Memory is used only alongside existing usable persisted rows. Missing rows still
cause NO_HISTORY acquisition. Empty/failed requests are never marked complete.
State is process-local, like existing jobs; after restart boundary probes can
recur. Listing-date-aware prefix suppression and durable empty-range evidence
are not implemented.

Force defaults false. Explicit force reacquires the requested bounded interval
for idempotent correction, but does not bypass cooldown or throttling.

## Execution and results

One lazily created NseHistoricalDailyProvider/session per job, closed in finally
including cancellation. Its existing cookie/header, spacing and bounded retry
behavior is reused. The population lock and inter-job spacing serialize this
operation with single-instrument acquisition. No global HTTP session, proxy
logic or concurrent NSE requests. Coordination is process-local, not distributed.

Each successful window uses `persist_daily_result` and existing
`ResearchRepository.upsert_daily_market_bars_async`. No SQL/schema change and no
close-only dual-write. Earlier good rows survive later failures.

Failures distinguish identity lookup, mapping, provider, throttling, HTTP, parser,
empty response, persistence and planning. Most failures stop the current
instrument's remaining windows, then continue with the next instrument. Empty
windows remain failures but allow later windows (for example, after listing).
Partial parser rejections persist accepted rows but count as failed windows and
never mark request coverage complete. A 429 stops further NSE calls for the job
and sets the existing 12-hour cooldown for subsequent backfill calls. Other
failures use per-instrument cooldown. Force cannot override either cooldown.

Requested-window counts are actual attempts, excluding unattempted windows
after failures. Processed instruments include failures and skips, and equal
succeeded + failed + skipped_current + skipped_cooldown. Per-instrument results
include requested DATE windows, coverage state, status/failure class/reason,
HTTP status, row counts and observed persisted bounds. Persisted row counts are
upserts, not necessarily newly inserted keys. No cookies, authentication headers
or raw exception text appear in summaries.

## Runtime smoke, 2026-09-13

A read-only canonical PostgreSQL snapshot contained 2,568 ACTIVE equities. The
existing trusted gate accepted all six examples: NILKAMAL, RAYMOND, GODREJAGRO,
ADANIPORTS, TDPOWERSYS, GEEKAYWIRE, plus POLYCAB. Names are validation examples,
not implementation selection logic.

A runtime adapter exposed this canonical snapshot through existing universe and
metadata method shapes. The generic worker was restricted to NILKAMAL
`4b085a61-0864-4ef1-ae23-0ba7e3ec6afb` and POLYCAB
`f8cb0fc7-082c-4d95-a77d-b1a9ca21d5a4`, September 1–4, 2026.

First run: two NO_HISTORY instruments, two successful windows, eight rows
received/accepted/persisted. Three NSE HTTP calls total: one bootstrap and two
history requests, all HTTP 200. Retries were disabled for this smoke. Current
ResearchRepository/SQLite persistence read back eight unique provider/day rows.
Immediate repeat: two CURRENT_HISTORY skips, zero windows, zero NSE calls,
identical rows. Runtime artifacts are under ignored `.tmp/`.

This validates live NSE and current local orchestration/persistence, not
authenticated canonical HTTP enumeration or deployed PostgreSQL application
end-to-end behavior. Mocked provider/HTTP and real SQLite repository tests cover
multi-window failure isolation, continuation, corrections, and cooldowns.

Out of scope: deployed PostgreSQL validation, ATR/ADX and volume technical
wiring, internal-gap repair without an authoritative calendar, sector benchmark
mapping/history, and broad-market benchmark history.
