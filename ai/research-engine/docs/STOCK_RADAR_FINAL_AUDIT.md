# Stock Radar local correctness and throughput audit — 2026-09-28

## Scope and deployment boundary

Read-only audit of cycle `fb915256-dabd-4a42-afc6-1902e0bc7d0d` (2,585 NSE stocks).
No deployment, restart, live database mutation, new cycle, resource/concurrency change,
or modification of the running cycle. Dirty worktree and durable evidence preserved.

Pod: `research-engine-69cff4688c-592mp`, namespace `ai-investment`, 1/1 Running,
zero restarts at inspection. Image:
`ai-investment-registry:5000/ai-investment/research-engine:dev-20260927-212558`.
Digest: `sha256:6def0ce1d8192b5a1ae61b2d9a7342610d09aee3631fc686dd6efdf14351aba4`.

Before this audit's edits, comparison against `/app/app/*.py` in the running pod
found local differences in five production files: `repository.py`,
`deep_investigation.py`, `source_discovery.py`, `research_fetching.py`, and
`global_opportunity_orchestration.py`. Those existing fixes are awaiting deployment.
This audit additionally changes `failure_taxonomy.py` and makes narrow further
changes in `repository.py` and `global_opportunity_orchestration.py`.

## Progress and accounting

Read-only checkpoint snapshot at approximately 09:46 UTC:

| Phase/outcome | Count |
|---|---:|
| Baseline evaluated | 2,584 |
| Baseline retryable failure | 1 |
| Deep analyzed / eligible | 197 |
| Deep fully analyzed / rank filtered | 42 |
| Deep retryable readiness failure | 144 |
| Deep recorded evidence-unavailable | 5 |
| Deep in progress | 4 |

388 completed deep attempts, 392 deep checkpoint rows, 2,193 not yet started.
Cycle remains RUNNING with a renewed lease and resume_count=0. Completions increased
from 376 to 388 during inspection. These are interim checkpoint counts, not final
cycle counters or a claim of completed acceptance.

A later 10:28 UTC snapshot had 200 analyzed, 42 rank-filtered, 146 retryable failures,
five recorded evidence-unavailable, and four in-progress rows (393 completed).
At 10:30 there were six IN_PROGRESS rows. These are started-but-unconsumed checkpoints,
not a count of currently executing acquisition tasks: bounded look-ahead retains
completed acquisitions until ordered Rule Engine consumption. Logs showed ongoing
history/provider work and an acquisition completing while awaiting that consumption.

At the earlier 09:34 snapshot, all 232 persisted analyzed/rank-filtered Rule Engine
results were full, non-partial, and had no applicable unscorable non-news area.
Missing/degraded CURRENT_NEWS was present among successfully analyzed candidates.
Balance-sheet matrices distinguished CORPORATE, FINANCIAL_SECTOR, and
ASSET_MANAGEMENT; lender gaps were not satisfied using corporate debt/equity/cash.

## Confirmed defects already addressed by existing local changes

- Official ownership processing followed the announcement/PDF batch and could lose
  its shared budget. The local path processes NSE ownership first, reports exhausted
  budget as technical, and preserves requirement-specific ownership failures.
- Successful discovery was confused with successful parsing/persistence. Empty or
  failed XBRL without qualifying categories now records technical failure.
- Annual voting/scrutinizer results could match financial-result discovery. The
  existing local classifier excludes those attachments while retaining explicitly
  identified financial results.
- Fetched financial documents yielding no supported financial facts could record
  SUCCESS_EMPTY. The local correction reports PARSER_FAILED instead.
- Skipped documents consumed shared slots; a local attempt limit could appear as a
  completed empty governance check. Existing local budget guards correct both.
- `active=0/1` was hard-coded. The local log reports actual configured/in-flight
  concurrency; this is instrumentation only.

No reimplementation of these fixes was necessary. Existing durable authoritative
shareholding precedence and per-requirement sufficiency checks remain intact.
SBIN and SAICAPI had **no durable shareholding snapshots** in the inspected database;
discovery messages therefore do not prove an authoritative snapshot was downgraded.
FEDERALBNK did have qualifying Level-A NSE ownership and READY_FRESH shareholding.
Its stored financial-result text contains OCR/column corruption, including merged
NPA numeric cells. Applicable ratios cannot safely be invented from that text.
Its missing lender ratios remain technical/parser failures. SAICAPI's historical
timeout remains retryable; verified historical mapping safeguards are unchanged.

## Additional local corrections

### False genuine-unavailable classification

Four checkpointed candidates had READY_FRESH ownership evidence with FII/DII/public
categories, but only one period and no promoter/pledge metric. The existing scorer
therefore had no holding-level or trend metric. A fifth candidate had only negative
valuation ratios usable by readiness but excluded by the scorer's positive-ratio
rules. None of these facts proves authoritative absence.

`GlobalOpportunityOrchestrator._unscorable_failures` previously inferred permanent
absence from readiness/scorer disagreement. `failure_taxonomy` also treated
ACQUISITION_NOT_DUE as permanent absence. Both conditions now remain technical and
retryable. The orchestrator preserves prior acquisition reasons and adds
RULE_AREA_UNSCORABLE, preventing even an earlier empty discovery result from masking
the disagreement. Bounded repair and exact candidate accounting are preserved.
No scores, missing ownership values, or alternative valuation thresholds are added;
the scoring gate remains closed for unsupported inputs.

### Discarded universe-wide identity scan

`ResearchRepository._prepare_ingested_document` scanned every company profile even
when `_has_trusted_nse_profile_identity` had already verified exact instrument,
company, official discovery method, and canonical NSE symbol. It then discarded the
scan's identity and confidence. That redundant scan is now skipped only for the
existing private trusted-identity token. Unverified sources still resolve and pass
the same relevance gate. Historical identity and ISIN checks are untouched.

## Throughput evidence and limits

Retained log window: 07:29:07–09:35:28 UTC. Durations are wall-clock measurements;
they are not an exclusive CPU profile and must not be summed as independent costs.

| Operation | Samples | Median | p95 | Maximum |
|---|---:|---:|---:|---:|
| Document PREPARE (includes thread scheduling/text identity work) | 243 | 26.410 s | 109.087 s | 171.597 s |
| Document persistence | 256 | 0.677 s | 3.439 s | 16.312 s |
| Financial-fact processing/persistence | 238 | 0.479 s | 2.287 s | 8.775 s |
| Official document fetch | 448 | 20.596 s | 107.006 s | 190.634 s |
| Official filing batch | 251 | 23.898 s | 149.616 s | 234.106 s |
| PDF queue wait at worker start | 267 | 0.002 s | 9.994 s | 19.610 s |
| PDF worker elapsed | 266 | 3.977 s | 24.532 s | 87.115 s |

Stage-2 is configured at four; the deployed `_running`/`_prefetch_ahead` path limits
running acquisition tasks to that setting, with up to 16 started-but-unconsumed results.
Checkpoint IN_PROGRESS counts cannot measure instantaneous execution concurrency.
The deployed progress log does not expose that count correctly; the existing local
instrumentation fix does. Hard-coded `active=0/1` is not deadlock evidence.
Progress advanced from 270 completions at 07:34:15 to 370 at 09:30:26: approximately
51.6 completions/hour over that observed interval, not a forecast for the whole cycle.
Between the 09:46 and 10:28 snapshots, only five further candidates completed
(approximately seven/hour). **This wall-clock interval includes host standby** and
must not be used as a provider/engine-only throughput measurement. Later logs show progress,
PREPARE median 32.506 seconds (15 samples), and historical-provider waits lasting
roughly two minutes. Ordered consumption can delay checkpoint completion behind a
slow candidate; no lock/deadlock conclusion is supported by these observations.
25-second orchestration wait expiry is not the duration of the underlying provider work.

Read-only Windows System event inspection confirmed Kernel-Power Modern Standby
events (506/507): entry at 09:48:41 UTC, a transition at 10:12:09, exit at 10:17:15,
then entry at 10:19:38 and exit at 10:23:59. These correspond to the long local-test
and live-log gaps, including a 260.881-second log gap. Host suspension is an additional
confirmed wall-clock throughput constraint. No power settings were changed.

CPU limit remains 500m. An initial top sample showed 500m; a separate 70.39-second
cgroup delta averaged 383m, with 431/670 scheduling periods throttled and 5.906 seconds
of accumulated throttled time. CPU throttling and long document preparation are
confirmed constraints. The local scan removal has not been benchmarked in deployment.

The window recorded 152 NETWORK_TIMEOUT, 23 PDF_EXTRACTION_QUEUE_TIMEOUT, 20
PDF_EXTRACTION_TIMEOUT, seven DOCUMENT_SIZE_LIMIT_EXCEEDED, two
PDF_TEXT_EXTRACTION_FAILED, and one PDF_SIGNATURE_INVALID official-fetch events.
These are event counts, not unique failed stocks. PDF serialization remains one;
timed-out workers continue to hold their safety permit until physical extraction ends.

DB stage times are materially below preparation times in this window. A read-only
pg_stat_activity sample showed no lock-wait event, but does not prove the absence of
all DB contention. End-of-cycle readiness/DB aggregate timings were not available;
no claim that DB/readiness overhead is zero is made.

Reuse is already implemented: financial requirements share one FINANCIAL_RESULTS
capability, with instrument single-flight and persisted-document reuse. Logs contain
40 ALREADY_PERSISTED reuse events and one ROLLING_WINDOW_COMPLETE short-circuit.
The latter instrument has durable June-quarter financial facts retrieved September
25, predating this attempt. Fresh quarterly evidence is reused; an outstanding lender
ratio can still legitimately require further acquisition. No universe cap or skipped
mandatory research is introduced.

## Validation

All tests use the existing outbound-blocking runner, including sockets used by
providers. Only the standard-library local socketpair exception for asyncio is allowed.
Initial additional-regression run: **25 passed, zero failed** (76.78 seconds).
Broader focused stock validation: **746 passed, zero failed** (2,764.43 seconds;
includes host-standby pauses). Existing NSE verified identity-trust tests:
**27 passed, zero failed** (57.55 seconds).
Total distinct focused tests: **773 passed, zero failed**; the initial 25 overlap
the 746 and are not added again. All 98 tests from the prior stock baseline groups
(applicability, lender contract, Rule Engine, CURRENT_NEWS, freshness/session) passed.

Commands, from `ai/research-engine` (PowerShell):

```powershell
$runner = 'C:/Users/Prakhar/AppData/Local/Temp/radar_correctness_uaiu6bxd/run_blocked.py'
python $runner tests/test_radar_final_audit.py tests/test_full_research_state_contract.py -q --tb=short --junitxml=C:/Users/Prakhar/AppData/Local/Temp/radar_final_audit_a_bst_rn/new.xml
python $runner '@C:/Users/Prakhar/AppData/Local/Temp/radar_final_audit_a_bst_rn/focused_args.txt'
python $runner tests/test_nse_verified_identity_trust.py -q --tb=short --junitxml=C:/Users/Prakhar/AppData/Local/Temp/radar_final_audit_a_bst_rn/identity.xml
```

The focused manifest covers 36 test files: readiness/runtime, applicability/lenders,
Rule Engine, CURRENT_NEWS, freshness, ownership/precedence, official financial and
semantic extraction, historical/verified identity, orchestration/ranking/worker pool,
shared acquisition, failure taxonomy, filing concurrency, PDF safety/reuse, prior
Radar corrections, and full-research/terminal accounting. The complete manifest,
JUnit XML, read-only evidence snapshots, host power events, and task-only patch are in
`C:/Users/Prakhar/AppData/Local/Temp/radar_final_audit_a_bst_rn`.

Tests added in `tests/test_radar_final_audit.py` cover scheduler/scorer gaps, preserved
failure reasons, bounded repair/accounting, real ownership/negative-valuation scorer
gaps, discarded resolver work, and rejection of unverified instrument/company/symbol
or discovery identity. `tests/test_full_research_state_contract.py` is corrected to
require retryable outcomes rather than unsupported claims of genuine absence.

## Remaining work and interpretation

- The running old image still contains the confirmed defects. Its five false
  evidence-unavailable checkpoints need a controlled repair/re-evaluation after a
  separately authorized deployment; this audit does not mutate them.
- Ownership without a promoter metric/history and negative-only valuation remain
  explicitly blocked, retryable input/scoring gaps. This task does not invent scores
  or declare those stocks bad or genuinely unavailable.
- Corrupted financial layouts, upstream timeouts/search degradation, and PDF limits
  remain reliability constraints. Technical outcomes must stay technical.
- CPU sizing, PDF throughput, and wider parser coverage are performance/capability
  debt, not reasons to weaken requirements or change settings in this audit.
- Host Modern Standby interrupts controlled wall-clock throughput measurement;
  validation must account for these intervals. No system setting was modified.
- Controlled validation can test the local corrections; full-universe success is
  not established while the old-image cycle is still running.

Decision: **READY_FOR_CONTROLLED_VALIDATION** of the local corrections. This is not
full-universe acceptance or authorization to deploy/restart/repair the live cycle.
