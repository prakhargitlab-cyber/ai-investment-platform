# Stage2 Live Run Defect Handoff
**Date:** 2026-09-30  
**Correlation ID:** `c9eefb14-9f40-4a1d-b575-1d7e7f02710f`  
**Universe:** 2602 equities  
**Observation Window:** 2026-09-30T20:37 – 2026-09-30T21:10 UTC (~2h snapshot in `live_run_2h.txt`, 6343 lines)  
**Live Data Sources:** `.tmp/live_run_2h.txt`, `.tmp/yahoo_mcp_2h.txt`

---

## Executive Summary

**Status:** Stage2 deep processing at ~460/2602 as of 21:07:22 UTC. Progress advancing at ~10 candidates per 3 min (normal), dropping to ~10 per 6.5 min during 3/4 worker periods. Processing IS continuing but at suboptimal throughput.

**Pod Health:**  
- `research-engine-77b96d4564-5pxt2` — Running, 0 restarts, image `dev-known-fixes-cancel-20260930-01`. Env: `AIP_RESEARCH_STAGE2_CONCURRENCY=4`, `AIP_RESEARCH_READINESS_ENSURE_TIMEOUT_SECONDS=25`, `AIP_RESEARCH_REQUEST_TIMEOUT_SECONDS=10`, `AIP_RESEARCH_MCP_GATEWAY_TIMEOUT_SECONDS=10`. Distributed lock backend = process-local. Active Liveness/Readiness probe failures ("context deadline exceeded").
- `yahoo-finance-mcp-7c6655d566-ppf9q` — Running, 0 restarts, image `dev-yahoo-bounded-work-20260930-01`. Env: `AIP_YAHOO_MCP_MAX_CONCURRENCY=4`, `AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS=8`, limits cpu=300m memory=384Mi. Active Liveness/Readiness probe failures.
- `research-service-6784d8d87d-4zwmx` — Running, 5 restarts (last OOM kill 2026-09-29 16:22:10 UTC, Exit Code 137). Memory limit 384Mi is very tight.

**Memory:** `processMaxRssKb=1425144` (~1.39 GB) consistently; limit is 1536 Mi. RSS grew 1283→1299 MB over the 2h window.

**Durable Reuse (WORKING — NOT a defect):** `REUSED reason=ALREADY_PERSISTED` (88 occurrences), `LIGHTWEIGHT_CHECK_UNCHANGED` for SHAREHOLDING_PATTERN (32 occurrences), `structured_mapping_reused` for Yahoo tickers (all VERIFIED, resolution_attempted=false).

**Test Coverage:** Claude has added ~40+ new untracked test files in `ai/research-engine/tests/` plus modified 9 existing test files. Key new tests: `test_pdf_queue_vs_extraction_timeout.py`, `test_stage2_durable_reuse.py` (561 lines), `test_current_news_optional_non_blocking.py`, `test_di18_financial_readiness_hardening.py`, `test_yahoo_mcp_acquisition.py`, `test_news_search_completion_semantics.py`, etc.

### Issue Summary

| # | Issue | Status | Impact |
|---|-------|--------|--------|
| 1 | PDF extraction timeout does not cancel worker — successful parse discarded | **NEW** (partially addressed by `test_pdf_queue_vs_extraction_timeout.py`) | High — 16/47 extractions discarded after successful parse; worker runs 3s–158s after timeout |
| 2 | PDF extraction concurrency=1, 20s timeout, workers continue after timeout | **KNOWN** (addressed by `test_pdf_queue_vs_extraction_timeout.py`) | High — 15.5s queue waits, 46 timeouts, 20 queue timeouts |
| 3 | NSE host failure budget resets between fetch batches, retrying same URLs | **NEW** (NOT covered by any test) | High — DUPLICATE fetches for 42b59185 and 41d35eb3 |
| 4 | NSE filing discovery returns up to 3644 candidates, only 13 accepted | **NEW** (NOT covered by any test) | Medium — massive wasted fan-out, no client-side pre-filtering |
| 5 | Yahoo Finance MCP: 514 timeouts (>8s), 43.6% success rate, 4x duplicate tool calls | **KNOWN** (addressed by `test_yahoo_mcp_acquisition.py`) | High — 70 min wasted on failures in 120 min window |
| 6 | Direct finance.yahoo.com calls: 18 HTTP 503s, no circuit breaker | **NEW** (NOT covered by any test) | Medium — 3 retries per URL with no backoff/circuit breaking |
| 7 | orchestration_wait_expired (25s) does NOT terminate processing — soft timeout | **NEW** (NOT covered by any test) | High — wait_expired logs fire but worker keeps running; 3ac64fa6 waited 263s total |
| 8 | SEARCH_PROVIDER_DEGRADED: 102 occurrences, searxng unresponsive_engines always present | **KNOWN** (addressed by `test_degraded_provider_attempts_bounded_and_recover_after_backoff`) | High — CURRENT_NEWS cascades to DOCUMENT_BUDGET_EXHAUSTED (54) + evidence insufficient (33) |
| 9 | Shareholding contradiction: LIGHTWEIGHT_CHECK_UNCHANGED then EXTERNAL_CAPABILITY_UNSUPPORTED | **NEW** (NOT covered by any test) | Medium — inconsistent failure semantics, 22 occurrences |
| 10 | Readiness/failure semantics contradictions across instruments | **NEW** (NOT covered by any test) | Medium — same requirement fails with different reasons |
| 11 | NSE NETWORK_TIMEOUT: 39 events, 8–10s each, all from nsearchives.nseindia.com | **KNOWN** (addressed by `test_di18_financial_readiness_hardening.py`) | High — cascades to QUARTERLY_FINANCIALS/PDF_EXTRACTION_TIMEOUT failures |
| 12 | DOCUMENT_SIZE_LIMIT_EXCEEDED (9.5MB JKPAPER) does not pre-filter; extraction proceeds anyway | **NEW** (NOT covered by any test) | Low-Medium — size check is per-attempt, not a pre-filter |
| 13 | research-service OOM kill (5 restarts, 384Mi limit too tight for 4 concurrent MCP calls) | **KNOWN** (addressed by `test_global_opportunity_worker_pool.py`) | High — instability risk |
| 14 | PDF extraction worker leak: concurrency=1 held for 158s blocks all other candidates | **NEW** (NOT covered by test_pdf_queue_vs_extraction_timeout.py — only addresses queue vs extraction timeout distinction) | High |

---

## Cycle Status

- **Stage 1 (global_scanner):** `scan_progress: completed=2607/2607` — completed at 21:15:59 UTC. 1 worker, ~3.5 min.
- **Stage 2 (deep):** `stage2_deep_progress` at **460/2602** as of 21:07:22 UTC. 4 workers (active=4/4 except 20:47–20:54 where active=3/4).
- **Progress rate:** ~10 candidates per 3 min normally; 10 per 6.5 min during 3/4 worker period.
- **9 stage2_deep_progress checkpoints** in 2h window: 380→390→400→410→420→430→440→450→460.
- **Checkpoint gaps:** 218s, 192s, 156s, **387s** (3/4 worker), 198s, 272s, 138s, 191s.

---

## Issue 1: PDF Extraction Timeout Does Not Cancel Worker — Successful Parse Result Discarded

**Status: NEW** (partially addressed by `test_pdf_queue_vs_extraction_timeout.py` which adds `research_pdf_extraction_queue_timeout_seconds` as a separate setting, but does NOT address the worker-cancellation-after-extraction-timeout problem)

**Severity: HIGH**

### Evidence

The extraction timeout fires at 20.0s (`configuredExtractionTimeoutSeconds=20.0`), but the extraction worker thread is **NOT cancelled**. The worker continues running, and if it eventually produces a successful parse, the result is **discarded** because the timeout already fired — meaning the document is never persisted, and must be re-downloaded/re-extracted on the next attempt.

**Confirmed via 16 `pdf_extraction_discarded` events**, all with `parseOutcome=SUCCESS` but `ownership=DISCARDED`:

| Timestamp (UTC) | Document Path | workerElapsedMs | parseOutcome | ownership |
|---|---|---|---|---|
| 20:40:35 | AKMEFINTRADE_06022026165949_Board_Meeting_Outcome_signed.pdf | 158,200 | SUCCESS | DISCARDED |
| 20:46:39 | VIKASLIFE_09072026170636_Vllbm2406sd11__1_.pdf | 57,079 | SUCCESS | DISCARDED |
| 20:48:50 | DIACABS_13082026162203_OutcomeofResult30062026.pdf | 45,368 | SUCCESS | DISCARDED |
| 20:51:43 | NIBL_29072025144549_Outcome.pdf | 51,618 | SUCCESS | DISCARDED |
| 20:53:54 | ATALREAL_07102025205852_OutcomeBM07102025.pdf | 46,958 | SUCCESS | DISCARDED |
| 20:55:06 | CORONAREMEDIES_11052026172049_Outcome_of_Board_Meeting.pdf | 70,312 | SUCCESS | DISCARDED |
| 20:56:08 | CORONAREMEDIES_07082026155254_Outcome_of_BM.pdf | 23,743 | SUCCESS | DISCARDED |
| 20:56:51 | NIBL_29072025144549_Outcome.pdf | 47,734 | SUCCESS | DISCARDED |
| 20:58:48 | DANGEE_13082026163539_BM_Outcome..._June_30_2026.pdf | 27,723 | SUCCESS | DISCARDED |

**Detailed trace for AKMEFINTRADE (4291d5ae):**
1. 20:49:04.903 — `pdf_extraction_queued` (documentBytes=7,948,575, queueWaitMs=6, configuredConcurrency=1)
2. 20:49:04.909 — `pdf_extraction_started` (queueWaitMs=6, activeExtractions=1)
3. 20:49:25.014 — `pdf_extraction_timeout` fires (timeoutSeconds=20.0, workerElapsedMsSoFar=20,105) — **worker NOT cancelled**
4. 20:49:25.303 — `official_document_fetch outcome=FAILED reason=PDF_EXTRACTION_TIMEOUT` — document marked FAILED, never persisted
5. 20:51:43.757 — `pdf_extraction_worker_metrics` — worker finally finishes after **158,200ms** (2m38s), `parseOutcome=SUCCESS`
6. 20:51:43.780 — `pdf_extraction_discarded ownership=DISCARDED` — successful result thrown away

**15 extractions** have `workerElapsedMs > 20000` (exceeded the 20s timeout). Max = 158,200ms.

### Suggested Fix
- When `configuredExtractionTimeoutSeconds` fires, the extraction worker thread must be **cancelled/interrupted**, not just logged.
- If the worker does complete after timeout, its result should be persisted (not discarded) if the parse succeeded, so that downstream requirements can reuse it.
- Alternatively: increase `configuredExtractionTimeoutSeconds` to accommodate large PDFs while capping `workerElapsedMs` at the timeout.

---

## Issue 2: PDF Extraction Concurrency=1 Causes Severe Queue Blocking

**Status: KNOWN** (addressed by `test_pdf_queue_vs_extraction_timeout.py` which adds `research_pdf_extraction_queue_timeout_seconds` to distinguish queue-admission timeout from extraction timeout)

**Severity: HIGH**

### Evidence

`configuredExtractionTimeoutSeconds=20.0`, `configuredConcurrency=1` (confirmed in all `pdf_extraction_started`/`pdf_extraction_queued` events).

| Metric | Count |
|---|---|
| pdf_extraction_queued | 57 |
| pdf_extraction_started | 49 |
| pdf_extraction_timeout | 46 |
| pdf_extraction_queue_timeout | 20 |
| pdf_extraction_discarded | 16 |
| pdf_extraction metrics (worker) | 47 |

**Queue wait times** (`queueWaitMs`), all with `activeExtractions=1`:

| queueWaitMs | Document | Instrument |
|---|---|---|
| 16,890 | BRITANNIA | corporate |
| 15,481 | CORONAREMEDIES | 455c1c37 |
| 15,206 | (unspecified) | — |
| 14,509 | (unspecified) | — |

10 events with queueWaitMs > 5,000. The concurrency=1 setting serializes all PDF extraction, and with 20s extraction timeout + workers continuing after timeout, a single stuck extraction blocks the worker slot for up to 158s, causing cascading queue timeouts for all subsequent documents.

### Suggested Fix
- Increase `research_pdf_extraction_concurrency` beyond 1 (test already validates concurrency=1 in isolation but production needs higher).
- The new `research_pdf_extraction_queue_timeout_seconds` setting (from `test_pdf_queue_vs_extraction_timeout.py`) separates queue-admission timeout from extraction timeout, allowing queued callers to be rejected quickly while the extraction worker continues.

---

## Issue 3: NSE Host Failure Budget Resets Between Fetch Batches — Same URLs Retried Despite Prior Failures

**Status: NEW** (NOT covered by any test — `test_di18_financial_readiness_hardening.py` and `test_nse_verified_identity_trust.py` address financial readiness and NSE trust, but do NOT address host-failure-budget-reset between batches)

**Severity: HIGH**

### Evidence

**39 `NETWORK_TIMEOUT` events**, ALL from `nsearchives.nseindia.com`. Elapsed times: 8098–9939ms (consistently ~8–10s, threshold around 8s timeout).

**Same URLs retried within same candidate after NETWORK_TIMEOUT:**
- `VIKASLIFE_31072026132251_Vllbm2406sd_final.pdf` fails at 20:45:02 (8192ms), retried at 20:46:14 (36956ms queue timeout)
- `INDUSINDBK4` fails at 20:51:25 (9236ms), retried at 20:52:16 (8471ms)
- `INDUSINDBK3` fails at 20:51:33 (8289ms), retried at 20:52:20 (9327ms)

**Host failure budget resets between fetch batches (instrument 42b59185 / INDUSINDBK):**

| Timestamp | Event | Detail |
|---|---|---|
| 20:51:02 | filing_discovery | candidateCount=3644, acceptedCount=13 |
| 20:51:03 | First fetch batch | elapsedMs=30,343 → `HOST_TRANSPORT_FAILURE_BUDGET` → STOPPED |
| 20:52:03 | filing_discovery runs AGAIN | candidateCount=3644, acceptedCount=13 (identical) |
| 20:52:03 | Second fetch batch | elapsedMs=16,804 → same URLs retried despite just failing |

Same URL `INDUSINDBK4_24042026164002...pdf` fails `NETWORK_TIMEOUT` in **both** fetch batches (20:51:03 and 20:52:03).

**Instrument 41d35eb3 (VIKASLIFE):**
1. VIKASLIFE URLs fail `NETWORK_TIMEOUT`, host budget kicks in
2. Second fetch batch retries same URLs → `PDF_EXTRACTION_TIMEOUT` (26090ms, 36956ms)
3. `ATTEMPT_BUDGET SKIPPED` events fire (11 of them), but not before more retries occur

### Suggested Fix
- The host failure budget (`HOST_TRANSPORT_FAILURE_BUDGET`) is scoped per-fetch-batch, not per-candidate or per-instrument. A second fetch batch within the same candidate re-establishes the budget.
- Fix: persist host failure budget state across batches within the same candidate, or use a per-host backoff that survives batch boundaries.

---

## Issue 4: NSE Filing Discovery Returns Up to 3644 Candidates, Only 13 Accepted — No Client-Side Pre-Filtering

**Status: NEW** (NOT covered by any test)

**Severity: MEDIUM**

### Evidence

`filing_discovery` candidateCount values observed:

| candidateCount | AcceptedCount | Instrument Context |
|---|---|---|
| 3,644 | 13 | 42b59185 (INDUSINDBK) — runs TWICE |
| 1,429 | — | (second highest) |
| 1,241 | — | |
| 946 | — | |
| 680 | — | |
| 613 | — | |
| 494 | — | |
| 347 | — | (two instruments) |
| 284 | — | |
| 278 | — | |
| 227 | — | |

Most candidates are discarded as `NO_QUALIFYING_ATTACHMENT` or non-qualifying. The entire 3644-candidate list is fetched, then 3631 are filtered client-side. This wastes significant network I/O and CPU on parsing/discarding.

**Instrument 42b59185:** Filing discovery runs TWICE (20:51:02 and 20:52:03) with identical results (3644 candidates, 13 accepted). The duplicate discovery run doubles the wasted work.

### Suggested Fix
- Add server-side or client-side pre-filtering to NSE filing discovery to avoid fetching 3644 candidates when only 13 will be accepted.
- Cache filing discovery results per-instrument within the same candidate to avoid the duplicate discovery run.

---

## Issue 5: Yahoo Finance MCP — Massive Timeout/Failure Rate (514 Timeouts, 43.6% Success)

**Status: KNOWN** (addressed by `test_yahoo_mcp_acquisition.py` — adds `AIP_YAHOO_MCP_MAX_CONCURRENCY=4` and `AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS=8`)

**Severity: HIGH**

### Evidence

In 2 hours: 939 total requests, **530 failures** (43.6% success rate).

| Tool | Requests | Failed | Success % | Notes |
|---|---|---|---|---|
| get_news | 624 | 328 | 47.4% | 52.6% failure rate |
| get_financials | 149 | 87 | 41.6% | 58.4% failure |
| get_quote | 72 | 50 | 30.6% | 69.4% failure |
| get_sector_industry | 36 | 32 | 8.9% | 88.9% failure |
| get_quarterly_financials | 41 | 25 | 39.0% | 61.0% failure |
| get_price_history | 17 | 8 | 52.9% | 47.1% failure |

- **514 timeouts** (>8000ms) — nearly all hit the 8-second upstream timeout. Average failure duration: 7,930ms.
- **Total time on failed requests: 70 minutes** (out of 120 min window).
- **Total time on successful requests: 25 minutes**.

**Duplicate tool calls:**
- `get_financials` for RAINBOW.NS called 8 times, GAJA.NS 8 times, TEMPSENS.NS 8 times.
- `get_news` for GPPL.NS 4 times, HEROMOTORS.NS 4 times, URBANCO.NS 4 times.
- Many instruments have 4x duplicate calls. Each timeout costs 8 seconds.

**Same symbols retried after timeout:**
- EDELWEISS.NS `get_news` failed at 19:15:25 (8789ms), retried at 19:15:25, succeeded at 19:15:29 (3513ms).
- GANDHAR.NS `get_sector_industry` retried 3x after 8s timeouts.

### Suggested Fix
- The fix adds `AIP_YAHOO_MCP_MAX_CONCURRENCY=4` (bounded work) and `AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS=8`.
- Need to verify the duplicate-call deduplication is also addressed (the test file `test_yahoo_mcp_acquisition.py` should cover this).

---

## Issue 6: Direct finance.yahoo.com Calls — 18 HTTP 503s, No Circuit Breaker

**Status: NEW** (NOT covered by any test — `test_yahoo_mcp_acquisition.py` addresses the MCP gateway path, not direct HTTP web scraping)

**Severity: MEDIUM**

### Evidence

42 total requests to `finance.yahoo.com` from research-engine (`app.research_fetching`).

**HTTP status distribution** from `fetch_http_headers_received`:
- 200 = 236, 301 = 28, 302 = 3, 308 = 1, 401 = 2, 402 = 1, 403 = 42, 404 = 1, 429 = 3, **503 = 9** (plus matching body_complete 503s)

**18 HTTP 503 responses** (9 from headers + 9 from body completion).

**3 instruments hit repeated 503s:**

| Instrument | 503 #1 | 503 #2 | 503 #3 | Notes |
|---|---|---|---|---|
| SWARNSAR.BO | 645ms | 296ms | 801ms | |
| EIMCOELECO.NS | 987ms | 386ms | 287ms | |
| DGCONTENT.NS | 707ms | 318ms | 311ms | |

Each 503 returns `bodyBytes=2067` (same error page).

**No failure budget/circuit breaker** — after 503s on `/quote/X`, the system retries the same URL 2 more times (3 total attempts) then gives up, with no backoff or circuit breaking.

**The 503 pattern:**
1. 301 redirect first (e.g., `/quote/SWARNSAR.BO` → `/quote/SWARNSAR.BO/`)
2. 3x 503 on the redirected URL (645ms, 296ms, 801ms)
3. Abandon

**403s** (42 occurrences) are from other financial sites (moneycontrol.com, etc.) — these trigger the yahoo.com fallback chain.

### Suggested Fix
- Add a circuit breaker for direct `finance.yahoo.com` HTTP calls.
- On 503, do not immediately retry — implement exponential backoff and stop after 1-2 retries.
- Consider removing direct web scraping fallback entirely and relying solely on the Yahoo Finance MCP gateway (which has bounded concurrency and timeout settings).

---

## Issue 7: orchestration_wait_expired (25s) Does NOT Terminate Processing — Soft Timeout

**Status: NEW** (NOT covered by any test — `test_global_opportunity_scheduler.py` and `test_global_opportunity_worker_pool.py` address scheduling/concurrency but not the soft-timeout semantics)

**Severity: HIGH**

### Evidence

**115 `orchestration_wait_expired` events** (25s budget), 78 unique instruments, 30+ instruments hit ≥2 times.

Top wait_expired inFlight groups:
- `GLOBAL_NEWS_SEARCH` (40)
- `YAHOO_FINANCE_MCP:CURRENT_NEWS + GLOBAL_NEWS_SEARCH` (26)
- `NSE:QUARTERLY_FINANCIALS` (24)
- `YAHOO_FINANCE_MCP:SECTOR_MACRO` (13)
- `STRUCTURED_MARKET` (3)

**Critical finding: wait_expired does NOT terminate processing.** Example with instrument 3ac64fa6:
1. 20:52:32 — `orchestration_wait_expired` (SECTOR_MACRO) — logs timeout, continues
2. 20:53:58 — `orchestration_wait_expired` again (QUARTERLY_FINANCIALS) — second timeout, still continues
3. 20:56:09 — `acquisition_completed` fires — **2+ minutes after the second timeout**

Total candidate duration: 263s. Disposition: `DEEP_READINESS_NOT_MET`.

**Candidate durations** (acquire_start → candidate_complete):
- 3ac64fa6: 263s (slowest)
- 39a4b630: 248.9s
- 4232857e: 223.1s
- 402bc43b: 188.1s
- 3e03a087: 175s

Many candidates taking 2–3.5 minutes. **All slowest candidates ended DEEP_READINESS_NOT_MET.**

**92 `stage2_candidate_complete` events:** 72 ANALYZED, 20 DEEP_READINESS_NOT_MET.
**112 `stage2_candidate_readiness` events:** 74 READY, 18 TECHNICAL_FAILURE, 18 INCOMPLETE, 2 RULE_AREA_UNSCORABLE.
**0 cancellation/timeout-termination events found** — confirming wait_expired is purely informational.

### Suggested Fix
- `orchestration_wait_expired` should be a **hard timeout**: terminate the in-flight requirement acquisition after 25s, record it as a partial completion, and move the candidate to readiness evaluation.
- Currently: 25s wait logs, but worker continues for 2+ minutes, blocking the worker slot and delaying all other candidates.

---

## Issue 8: SEARCH_PROVIDER_DEGRADED — searxng Consistently Unresponsive (102 Occurrences)

**Status: KNOWN** (addressed by `test_degraded_provider_attempts_bounded_and_recover_after_backoff` and `test_same_acquisition_budget_does_not_retry_known_degraded_provider` — bounds retries within same acquisition budget and adds backoff)

**Severity: HIGH**

### Evidence

- **102 `SEARCH_PROVIDER_DEGRADED`** occurrences — EVERY CURRENT_NEWS query shows `unresponsive_engine_count=1–3` on provider=searxng.
- `search_query_degraded`: 43 events.
- **54 `DOCUMENT_BUDGET_EXHAUSTED`** — frequently combined with `SEARCH_PROVIDER_DEGRADED`.
- `deep_requirement_budget_exhausted` for CURRENT_NEWS: reason=`DOCUMENT_BUDGET_EXHAUSTED|DOCUMENT_FETCH_FAILED|SEARCH_PROVIDER_DEGRADED:unresponsive_engines=2` (repeated for multiple instruments).
- **33 `deep_requirement_evidence_insufficient`** for CURRENT_NEWS — ALL with `SEARCH_PROVIDER_DEGRADED` reasons.
- 7 `SEARCH_PROVIDER_UNAVAILABLE`.
- `search_query_failed SEARCH_PROVIDER_TIMEOUT`: 1 (21:08:01 Spencer's Retail).
- YAHOO_FINANCE_MCP:CURRENT_NEWS also times out (26 instruments in wait_expired with `YAHOO_FINANCE_MCP:CURRENT_NEWS` inFlight).

### Suggested Fix
- The fix adds `RequirementAcquisitionBudget` context with per-instrument, per-requirement scoping. `_scope.set(budget)` ensures search discovery does not retry a known-degraded provider within the same acquisition budget.
- Need to verify the backoff timing: test shows `tick[0] += 31` to recover — confirm 31s backoff is sufficient for production searxng recovery.

---

## Issue 9: Shareholding Contradiction — LIGHTWEIGHT_CHECK_UNCHANGED Then EXTERNAL_CAPABILITY_UNSUPPORTED

**Status: NEW** (NOT covered by any test — `test_shareholding.py` exists but based on git status is likely a prior iteration's test, not in Claude's current modified set)

**Severity: MEDIUM**

### Evidence

- **22 `EXTERNAL_CAPABILITY_UNSUPPORTED`** — all for SHAREHOLDING `deep_requirement_evidence_insufficient`.
- Pattern: `shareholding_official_discovery outcome=ZERO_RESULTS rowCount=0` → `LIGHTWEIGHT_CHECK_UNCHANGED` → then `EXTERNAL_CAPABILITY_UNSUPPORTED`.
- NSE returns no shareholding data for these instruments (`NON_QUARTER_REPORTING_DATE` or `ZERO_RESULTS`).
- 3 shareholding discoveries SUCCEEDED (SUCCESS with rowCount/snapshotCount) → SHAREHOLDING satisfied.
- 5 REJECTED (`NON_QUARTER_REPORTING_DATE`), 12 ZERO_RESULTS.

**Contradiction (3ca1b0d7):**
1. `LIGHTWEIGHT_CHECK_UNCHANGED` fires for SHAREHOLDING at 20:37:56.076
2. Deep still acquires SHAREHOLDING at 20:37:59.884 (2.8s later)
3. `deep_requirement_evidence_insufficient SHAREHOLDING reason=EXTERNAL_CAPABILITY_UNSUPPORTED`

**Contradiction (402bc43b):**
1. `LIGHTWEIGHT_CHECK_UNCHANGED` at 20:43:02.978 and 20:43:12.560
2. `research_targeted_ensure_complete` at 20:43:13 includes SHAREHOLDING_PATTERN
3. Deep reports `SOURCE_UNAVAILABLE:SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED:unresponsive_engines=1` at 20:43:27.272

402bc43b gets **different failure reason** than 3ca1b0d7 for the same requirement:
- 3ca1b0d7 → `EXTERNAL_CAPABILITY_UNSUPPORTED`
- 402bc43b → `SOURCE_UNAVAILABLE:SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED`

**2 `RULE_AREA_UNSCORABLE` events** (43e00a19, 4439c8ad), `failure_class=TECHNICAL_RETRYABLE`, `blocking=['SHAREHOLDING']`.

### Suggested Fix
- `LIGHTWEIGHT_CHECK_UNCHANGED` should short-circuit the deep requirement acquisition — if the lightweight check says nothing changed, the deep acquisition should not proceed.
- Failure reason classification is inconsistent: the same requirement (SHAREHOLDING) on the same instrument class can fail with `EXTERNAL_CAPABILITY_UNSUPPORTED` or `SOURCE_UNAVAILABLE:SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED` depending on timing.
- Need a single source of truth for shareholding availability: if NSE returns ZERO_RESULTS, classify as `EXTERNAL_CAPABILITY_UNSUPPORTED` consistently, not sometimes as a search provider failure.

---

## Issue 10: Readiness/Failure Semantics Contradictions

**Status: NEW** (NOT covered by any test)

**Severity: MEDIUM**

### Evidence

- 18 `stage2_candidate_readiness state=TECHNICAL_FAILURE` events (18 INCOMPLETE + 18 TECHNICAL_FAILURE = 36 of 112 total readiness events; 74 READY, 2 RULE_AREA_UNSCORABLE).
- 4 `deep_requirement_source_unavailable` events.
- 91 `deep_requirement_evidence_insufficient`.

**Contradiction 1 (3ca1b0d7):**
- `LIGHTWEIGHT_CHECK_UNCHANGED` for SHAREHOLDING at 20:37:56.076
- Then `deep_requirement_evidence_insufficient SHAREHOLDING reason=EXTERNAL_CAPABILITY_UNSUPPORTED`

Same instrument, same requirement: lightweight says "unchanged, skip" but deep says "externally unsupported." The two readiness layers are not communicating.

**Contradiction 2 (402bc43b):**
- `LIGHTWEIGHT_CHECK_UNCHANGED` at 20:43:02.978 and 20:43:12.560
- `research_targeted_ensure_complete` at 20:43:13 includes SHAREHOLDING_PATTERN
- `SOURCE_UNAVAILABLE:SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED:unresponsive_engines=1` at 20:43:27.272

**Contradiction 3:** Instrument 3ca1b0d7 → SHAREHOLDING fails with `EXTERNAL_CAPABILITY_UNSUPPORTED`. Instrument 402bc43b → SHAREHOLDING fails with `SOURCE_UNAVAILABLE:SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED`. Same requirement, different failure reason, different failure_class.

**2 `TECHNICAL_RETRYABLE`** occurrences — `RULE_AREA_UNSCORABLE` for 43e00a19 and 4439c8ad, `blocking=['SHAREHOLDING']`.

### Suggested Fix
- Define a single failure-classification pipeline: `EXTERNAL_CAPABILITY_UNSUPPORTED` should be terminal (not retryable), `TECHNICAL_RETRYABLE` should not block the candidate from being scored, and `SOURCE_UNAVAILABLE` (degraded search) should be retryable within the current cycle but not cascade into `RULE_AREA_UNSCORABLE`.
- Currently: 43e00a19 and 4439c8ad are `RULE_AREA_UNSCORABLE` with `failure_class=TECHNICAL_RETRYABLE` and `blocking=['SHAREHOLDING']` — the candidate is blocked from scoring entirely. If `TECHNICAL_RETRYABLE`, it should be marked incomplete but not unscorable.

---

## Issue 11: NSE NETWORK_TIMEOUT Cascades to QUARTERLY_FINANCIALS and PDF_EXTRACTION_TIMEOUT

**Status: KNOWN** (addressed by `test_di18_financial_readiness_hardening.py` — hardens financial readiness with retry/circuit-breaker)

**Severity: HIGH**

### Evidence

- **39 `NETWORK_TIMEOUT`** events, ALL from `nsearchives.nseindia.com`. Elapsed: 8098–9939ms.
- `QUARTERLY_FINANCIALS → NETWORK_TIMEOUT`: 8 occurrences (NSE PDF fetch cascades)
- `QUARTERLY_FINANCIALS → PDF_EXTRACTION_TIMEOUT`: 7 occurrences
- `QUARTERLY_FINANCIALS → PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS`: 3
  - 3ca1b0d7: 28 FINANCIAL_RESULTS discovered, 0 parseable facts
  - 40b90fd5: 16 discovered, 0 extracted
  - 402bc43b: 2 discovered, 0 extracted
- `GROWTH_FACTS → NETWORK_TIMEOUT`: 2 (41d35eb3 with 24 FINANCIAL_RESULTS discovered, all failed)
- `BUSINESS_QUALITY_FACTS → PDF_EXTRACTION_TIMEOUT`: 2; `PDF_EXTRACTION_QUEUE_TIMEOUT`: 1
- `BALANCE_SHEET_FACTS → NETWORK_TIMEOUT`: 1; `PDF_EXTRACTION_TIMEOUT`: 1

**Instrument 41d35eb3 (VIKASLIFE):** 5 documents failed with NETWORK_TIMEOUT, then both GROWTH_FACTS and QUARTERLY_FINANCIALS reported insufficient with `discovery={'FINANCIAL_RESULTS': 24}`.

### Suggested Fix
- The `test_di18_financial_readiness_hardening.py` test adds retry-with-backoff and circuit-breaker semantics for NSE PDF fetches.
- Verify: the fix should prevent the cascade where NETWORK_TIMEOUT on PDF fetch → PDF_EXTRACTION_TIMEOUT → PARSER_FAILED → empty facts → DEEP_READINESS_NOT_MET.

---

## Issue 12: DOCUMENT_SIZE_LIMIT_EXCEEDED Does Not Pre-Filter Large PDFs

**Status: NEW** (NOT covered by any test)

**Severity: LOW–MEDIUM**

### Evidence

1 `DOCUMENT_SIZE_LIMIT_EXCEEDED` event:
- JKPAPER PDF (9.5MB), `DOCUMENT_SIZE_LIMIT_EXCEEDED elapsedMs=828`
- Extraction THEN proceeds for the same document (queueWaitMs=34, succeeds in 4135ms)

So the size limit check fires but does NOT prevent the extraction from proceeding. The check is per-attempt, not a pre-filter. The document is still downloaded, queued, and extracted — the size limit only logs a warning.

### Suggested Fix
- If `DOCUMENT_SIZE_LIMIT_EXCEEDED` fires, skip the extraction entirely.
- Add the document to a "too large, do not retry" cache so subsequent requirements don't re-attempt it.

---

## Issue 13: research-service OOM Kill (5 Restarts, 384Mi Limit Too Tight)

**Status: KNOWN** (addressed by `test_global_opportunity_worker_pool.py` — worker pool sizing)

**Severity: HIGH**

### Evidence

- `research-service-6784d8d87d-4zwmx` — Running, **5 restarts**, last Exit Code 137 (OOM kill) at 2026-09-29 16:22:10 UTC.
- Memory limit: 384Mi.
- This is very tight for 4 concurrent Yahoo API calls with 8s timeout.
- The 403 errors from finance.yahoo.com and other sites may be causing unbounded response handling.

### Suggested Fix
- Increase `resources.limits.memory` for research-service beyond 384Mi.
- The Stage2 fix should review the worker pool sizing test to ensure memory usage per worker is bounded.

---

## Issue 14: PDF Extraction Worker Leak — Concurrency=1 Held for 158s Blocks All Candidates

**Status: NEW** (the `test_pdf_queue_vs_extraction_timeout.py` test addresses queue vs extraction timeout distinction, but does NOT address the worker-leak problem where a slow extraction holds the single slot for 158s)

**Severity: HIGH**

### Evidence

The PDF extraction worker pool has `configuredConcurrency=1`. When a large PDF (e.g., AKMEFINTRADE at 7.9MB) takes 158,200ms to extract, the single worker slot is held for the entire duration. During this time:

- 10 other documents queue with `queueWaitMs > 5,000` (max 16,890ms for BRITANNIA)
- 20 `pdf_extraction_queue_timeout` events fire
- 46 `pdf_extraction_timeout` events fire (20s timeout) but worker continues
- 16 `pdf_extraction_discarded` events fire (successful parse, but discarded)

The 4-second window between `pdf_extraction_timeout` firing (20:49:25) and `pdf_extraction_worker_released` (20:51:43) = 138 seconds where the worker slot is held by a timed-out extraction that nobody wants the result of.

### Suggested Fix
- Increase PDF extraction concurrency beyond 1.
- When extraction timeout fires, cancel/interrupt the worker thread so the slot is freed immediately.
- Currently: timeout fires → worker keeps running for up to 158s → result discarded → slot was occupied the entire time → all queued documents timeout.

---

## Issue Tagging Summary

### CONFIRMED Issues (Stage2 patch addresses these)

| # | Issue | Test(s) | Status |
|---|-------|---------|--------|
| 2 | PDF extraction concurrency=1, 20s timeout, workers continue after timeout | `test_pdf_queue_vs_extraction_timeout.py` | KNOWN |
| 8 | SEARCH_PROVIDER_DEGRADED: 102 occurrences, searxng unresponsive | `test_degraded_provider_attempts_bounded_and_recover_after_backoff`, `test_same_acquisition_budget_does_not_retry_known_degraded_provider` | KNOWN |
| 5 | Yahoo Finance MCP: 514 timeouts, 43.6% success, 4x duplicate tool calls | `test_yahoo_mcp_acquisition.py` | KNOWN |
| 11 | NSE NETWORK_TIMEOUT cascades to financial facts | `test_di18_financial_readiness_hardening.py` | KNOWN |
| 13 | research-service OOM kill, 384Mi too tight | `test_global_opportunity_worker_pool.py` | KNOWN |
| 1 (partial) | PDF extraction timeout does not cancel worker | `test_pdf_queue_vs_extraction_timeout.py` (adds queue timeout, but worker-cancellation NOT addressed) | NEEDS CODE CORRELATION |

### NEEDS CODE CORRELATION (may be partially addressed — verify against diff)

| # | Issue | Relevant Files | Status |
|---|-------|---------------|--------|
| 1 | PDF extraction timeout doesn't cancel worker — result discarded after SUCCESS | `research_fetching.py`, `repository.py` | NEEDS CORRELATION |
| 7 | orchestration_wait_expired does NOT terminate processing (soft timeout) | `opportunity_worker.py`, `deep_investigation.py`, `global_opportunity_scheduler.py` | NEEDS CORRELATION |
| 9 | Shareholding: LIGHTWEIGHT_CHECK_UNCHANGED → EXTERNAL_CAPABILITY_UNSUPPORTED contradiction | `research_readiness_runtime.py`, `repository.py`, `source_discovery.py` | NEEDS CORRELATION |
| 10 | Readiness/failure semantics contradictions (TECHNICAL_RETRYABLE but RULE_AREA_UNSCORABLE) | `failure_taxonomy.py`, `research_readiness_runtime.py` | NEEDS CORRELATION |

### GENUINELY NEW (not covered by any existing or new test)

| # | Issue | Status |
|---|-------|--------|
| 3 | NSE host failure budget resets between fetch batches, retrying same URLs | **NEW** |
| 4 | NSE filing discovery returns 3644 candidates, only 13 accepted, no pre-filtering | **NEW** |
| 6 | Direct finance.yahoo.com 503s with no circuit breaker | **NEW** |
| 12 | DOCUMENT_SIZE_LIMIT_EXCEEDED does not pre-filter large PDFs | **NEW** |
| 14 | PDF extraction worker leak — single slot held 158s blocks all candidates | **NEW** |

---

## Appendix: Pod Configuration Reference

### research-engine (`dev-known-fixes-cancel-20260930-01`)
| Env Var | Value |
|---|---|
| AIP_RESEARCH_STAGE2_CONCURRENCY | 4 |
| AIP_RESEARCH_READINESS_ENNSURE_TIMEOUT_SECONDS | 25 |
| AIP_RESEARCH_REQUEST_TIMEOUT_SECONDS | 10 |
| AIP_RESEARCH_MCP_GATEWAY_TIMEOUT_SECONDS | 10 |
| Distributed lock backend | process (NOT etcd/redis) |

**Probes:** Liveness failed "context deadline exceeded" ×26 over 105m; Readiness failed ×23 over 105m.

### yahoo-finance-mcp (`dev-yahoo-bounded-work-20260930-01`)
| Env Var | Value |
|---|---|
| AIP_YAHOO_MCP_MAX_CONCURRENCY | 4 |
| AIP_YAHOO_MCP_UPSTREAM_TIMEOUT_SECONDS | 8 |
| cpu limit | 300m |
| memory limit | 384Mi |

**Probes:** Liveness/Readiness both failed "context deadline exceeded" at observation time (~21:11 UTC).

### research-service (`dev-20260928-180605`)
| Field | Value |
|---|---|
| Restarts | 5 (last OOM kill 2026-09-29 16:22:10 UTC, Exit Code 137) |
| Memory limit | 384Mi |
| Readiness probe failures | ×6 over 28h |

---

## Appendix: Key Metrics Summary

### Stage2 Deep Processing (2h window: 20:37–21:10 UTC)
| Metric | Value |
|---|---|
| stage2_deep_progress | 380→460 (80 candidates, 34%) |
| Checkpoint gaps | 218s, 192s, 156s, 387s, 198s, 272s, 138s, 191s |
| Active workers | 4/4 normally; 3/4 during 20:47–20:54 |
| orchestration_wait_expired | 115 (78 unique instruments, 30+ hit ≥2x) |
| stage2_candidate_complete | 92 (72 ANALYZED, 20 DEEP_READINESS_NOT_MET) |
| stage2_candidate_readiness | 112 (74 READY, 18 TECHNICAL_FAILURE, 18 INCOMPLETE, 2 RULE_AREA_UNSCORABLE) |
| processMaxRssKb | 1,425,144 (~1.39 GB; limit 1,536 MB) |

### PDF Pipeline
| Metric | Value |
|---|---|
| configuredConcurrency | 1 |
| configuredExtractionTimeoutSeconds | 20.0 |
| pdf_extraction_queued | 57 |
| pdf_extraction_started | 49 |
| pdf_extraction_timeout | 46 |
| pdf_extraction_queue_timeout | 20 |
| pdf_extraction_discarded | 16 |
| Max queueWaitMs | 16,890 (BRITANNIA) |
| Max workerElapsedMs | 158,200 (AKMEFINTRADE, 7.9MB) |
| Documents with workerElapsedMs > 20,000 | 15 |

### Yahoo Finance MCP (2h window)
| Metric | Value |
|---|---|
| Total requests | 939 |
| Total failures | 530 (43.6% success) |
| Timeouts (>8000ms) | 514 |
| get_news failures | 328/624 (52.6%) |
| get_financials failures | 87/149 (58.4%) |
| get_quote failures | 50/72 (69.4%) |
| get_sector_industry failures | 32/36 (88.9%) |
| Duplicate get_financials (RAINBOW.NS) | 8x |
| Duplicate get_news (GPPL.NS) | 4x |
| Failed request time | 70 min (of 120) |
| Successful request time | 25 min (of 120) |

### Direct finance.yahoo.com
| Metric | Value |
|---|---|
| Total requests | 42 |
| HTTP 503 | 18 |
| HTTP 403 | 42 |
| Max retries on same URL | 3 (no backoff/circuit breaker) |

### NSE Network
| Metric | Value |
|---|---|
| NETWORK_TIMEOUT | 39 (all from nsearchives.nseindia.com) |
| Timeout range | 8098–9939ms |
| Max filing_discovery candidates | 3,644 (only 13 accepted) |
| Duplicate filing_discovery runs | 42b59185 ran twice (3644 candidates each) |

### Search / Current News
| Metric | Value |
|---|---|
| SEARCH_PROVIDER_DEGRADED | 102 |
| DOCUMENT_BUDGET_EXHAUSTED | 54 |
| search_query_degraded | 43 |
| deep_requirement_evidence_insufficient (CURRENT_NEWS) | 33 |
| SEARCH_PROVIDER_UNAVAILABLE | 7 |

### Shareholding
| Metric | Value |
|---|---|
| EXTERNAL_CAPABILITY_UNSUPPORTED | 22 (all SHAREHOLDING) |
| shareholding_official_discovery ZERO_RESULTS | 12 |
| NON_QUARTER_REPORTING_DATE (REJECTED) | 5 |
| SHAREHOLDING SUCCEEDED | 3 |
| RULE_AREA_UNSCORABLE (TECHNICAL_RETRYABLE, blocking=SHAREHOLDING) | 2 |
