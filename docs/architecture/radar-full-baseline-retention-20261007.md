# FULL baseline retention investigation — 2026-10-07

Production evidence supplied for cycle `d2f87b61-b951-4205-9ab6-5772b2fbcb38`
establishes OOMKilled/137 after baseline progress 50/2434. It does not include
a heap profile or allocation trace. The defect below is reproduced through
the production baseline call chain offline; it is **not proof that this was
the only allocation responsible for the production kill**.

## Retaining path

`GlobalOpportunityOrchestrator._acquire_baseline_requirements`
→ `ResearchReadinessRuntime.ensure` (default `wait_for_completion=False`)
→ `_execute_plan_until_ready` → `_execute_plan`
→ `ExistingResearchCapabilityExecutor.execute_primary`
→ structured-market reconciliation → `YahooFinanceProvider.collect_baseline`
→ `asyncio.to_thread(_collect_resolved_yfinance)`
→ `YahooTickerContexts._contexts[(acquisition_task, symbol)]`
→ `_TickerAccess._ticker` / `_info`.

The process-lived provider registry kept the completed task and its mutable
provider graph until LRU eviction, even though reuse is explicitly scoped to
that one task. Historical acquisition shares that ticker registry. Up to 64
completed contexts survived, independently of the eight-candidate pool. This
is growth with completed candidates up to the existing cache ceiling, not an
unlimited O(N) leak. There was no byte bound on those 64 ticker graphs.

`McpFirstResearchCapabilityExecutor._context_results` had the same lifetime
defect: completed task keys and complete successful provider result objects
survived in its separate 64-context registry. Failure entries contain safe
codes, not exception objects, but retained task keys can still retain task
state. Both registries now remove entries when their owner task settles.
Non-task explicit tokens retain their existing LRU contract. No cache limit,
concurrency, timeout, retry, acquisition, or evidence policy was changed.

Thread-side registration is scheduled onto the owner's event loop. If the
owner already finished, the entry is removed immediately; a delayed thread
cannot register a completed context again. Removing a registry entry does
not mutate an access object still held by active native work.

## Other batch retention reviewed

| Structure/path | Lifetime / finding |
| --- | --- |
| Baseline `results` | Already compact `_BaselineOutcome`: planned IDs and four booleans; no readiness/provider graph. Required O(N) accounting remains. |
| Baseline `pending` / `done` | Window bounded by eight; task results are compact outcomes. No universe-sized `gather` of acquisitions. |
| Candidate/metadata/selection/checkpoint collections | Identity, canonical metadata and durable outcome information; selection/recovery semantics unchanged. |
| Runtime `_flights` / child tasks | Existing completion/drain cleanup retained. Previous tests cover these structures but omitted the provider registries. |
| Readiness snapshots / `_assess_memo` | Per-execution locals; existing evidence-only cache ceiling retained. Adapter metadata/failure/session indexes do not store raw provider bodies. |
| Structured snapshot cache | `collect_baseline` bypasses and invalidates the combined full-snapshot cache; it does not populate it. Full research cache behavior was not changed. |
| Repository document bodies | Existing count/byte-bounded durable cache; pinned active writes and compact durable indexes have distinct lifetimes. New baseline telemetry reports its counts/bytes. |
| PDF/document acquisition | Baseline selects price, history, valuation and sector requirements; it does not select financial/document/news acquisition. Existing documents may still be loaded for readiness. Parser active working-set risk is distinct from the reproduced ticker retention. |
| Discovery/retry state | Existing NSE rows cache and failed URL/cooldown metadata; not a universe-wide collection of baseline acquisition results. |
| Persistence | Operations awaited through the repository's serialized boundary. No new result buffer or persistence queue introduced. Cancelled `to_thread` work can outlive the async caller. |
| Timing/progress | Existing bounded timing records and scalar counters; new telemetry retains only numeric counts, never tasks/payloads. |

## Before/after experiment

`test_radar_baseline_payload_lifetime.py` executes the real baseline pool,
readiness planning/task ownership, capability executor, structured provider,
thread dispatch and ticker registry. Transport uses synthetic tickers carrying
256 KiB raw bodies. Persistence/readiness seams model compact persisted facts;
the existing durable readiness tests separately validate evidence semantics.
All four baseline requirements are requested; this fixture starts with a
missing price and the other requirements ready.

Each variant processes **2434** candidates at the unchanged concurrency **8**.
The mixed variant forces the real bounded-timeout branch for every seventeenth
candidate (143 timeouts) using a test-only shortened budget.

| Observation | Before cleanup | After cleanup |
| --- | --- | --- |
| Successful batch peak live raw payloads | 64 / 16 MiB | 8 / 2 MiB |
| Successful completion 50 | 51 live, 51 completed-task entries | 0 live, 0 completed-task entries |
| Successful completions 500, 1500, 2434 | 64 live, 64 completed-task entries | 0 live, 0 completed-task entries |
| Mixed timeout batch peak | 64 / 16 MiB | 8 / 2 MiB |
| End of either batch | 64 retained payloads | 0 payloads, 0 provider entries, 0 flights |

These are exact synthetic payload byte counts and weak-reference lifetimes,
not a measurement of production RSS. Intermediate active counts can vary
with scheduling. Tests assert every completed candidate's own payload is
already gone, peak counts never exceed eight, all 2434 outcomes are compact,
and exact ready/timeout/failure accounting. Separate regressions cover MCP
success/cancellation cleanup and a native thread finishing after owner
cancellation. No forced GC is used to make the batch release assertions pass.

The previous 2 MiB test allocated only inside a fake executor coroutine and
exercised `wait_for_completion=True`. It never constructed a Yahoo provider or
populated either context registry. It could pass while this retention remained.

The baseline progress line's `failed` counter counts candidate-level exception
or hydration failures (`reason is not None`); it does not count every timeout
inside a returned ensure result. Those flags are aggregated separately after
the batch. Therefore `Failed=0` does not prove every provider acquisition
succeeded. The existing progress logging cadence is unchanged.

## Recovery counter

`opportunity_cycle_resume_enqueued` logs the row **before** `_run_one` claims
it. `claim_cycle_run` atomically increments `resume_count` when a different
owner takes over a non-ACCEPTED run. Enqueueing is not itself a successful
takeover. A regression proves enqueue=0, new-owner claim=1, repeated same-owner
claim=1, completion=1 under the same cycle ID. No persistence accounting fix
was needed. `opportunity_cycle_claimed` now logs the post-claim value.

## Production observability and limits

At existing baseline progress checkpoints, `baseline_acquisition_retention`
reports active candidates, compact outcomes, active/completed readiness
flights, readiness cache entries, Yahoo/MCP active-or-completed registry
entries, and document-cache accounting. Callback cleanup can lag a task's
completion by one loop turn; a sustained completed-entry count is the useful
signal, distinct from a momentary callback backlog.

This closes the reproduced completed-provider retention path. It does not
establish a whole-process production RSS ceiling: yfinance/native-library
allocation, non-cancellable active threads, existing durable document volume,
allocator behavior, and concurrently running work were not measured against
the killed container. The reported 1536 MiB kill cannot be quantitatively
attributed solely from the supplied status/progress logs. No deployment,
production Radar execution, memory-limit change, commit or push was performed.

## Focused validation

The 12-file Radar/OOM/recovery/readiness/provider/PDF selection finished with
166 passed and one failure: the existing live-owner/coalescing test lost its
0.3-second test lease during completion and correctly fenced the old owner.
An unchanged isolated rerun passed (1 passed). No lease duration or production
timing was adjusted. The run also emitted the existing Starlette/httpx test
client deprecation warning. `git diff --check` passed.
