# ETF Radar: targeted post-phase-6 history recovery

Source cycle: `17c48038-507b-4924-b902-20f3b7df4091`, COMPLETED, published.
Investigation date: 2026-11-06 UTC. Implementation is local only. No recovery,
reset, full-universe run, scheduler change or deployment was executed.

This records the original implementation task. The subsequent operator-verified
pilot and Phase-2 read-only candidate discovery/reporting contract are documented
in [ETF history recovery candidate discovery](etf-history-recovery-discovery.md).
Discovery is separate from execution authorization and never reuses a terminal
recovery ID as a new run.

## Confirmed root causes

[Baseline evidence](etf-history-recovery-baseline.json) contains all 236 failed
canonical IDs, ISINs, symbols, exact persisted reasons and matched log diagnostics.
Database investigation used read-only transactions with rollback. It confirmed
134,000 existing bars on 123 instruments; the 236 failed instruments had no bars.
The source cycle, publication and successful observations were left unchanged.

| Group | Count | Verified evidence | Meaning / recovery policy |
| --- | ---: | --- | --- |
| Explicit chart error | 232 | `RETRYABLE_FAILURE` / `TECHNICAL_FAILURE`; persisted reason `HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:YFPricesMissingError`; log subtype `YAHOO_EXPLICIT_CHART_ERROR` | Yahoo rejected the first `[2021-11-02,2023-11-03)` window. This does not establish that newer data or the ETF itself is absent. |
| Empty/malformed chart response | 3 | Same persisted reason; subtype `YAHOO_EMPTY_OR_MALFORMED_RESPONSE`; NIFTYIETF, NIF100BEES, MID150BEES | Technical/provider failures remain unresolved. They cannot certify an empty trading history. |
| Conflicting instrument type | 1 | NV20, ISIN `INF174KA1ZE1`; `VERIFIED_HISTORICAL_IDENTITY_MISMATCH:QUOTE_TYPE_MISMATCH`; returned `INDEX`, `NSI`, `INR` | Blocked. No INDEX history is accepted as ETF evidence. |

`app/etf_full_universe.py::_ensure_etf_history` requests five years in oldest-first
windows of at most 731 calendar days. It returns immediately on a technical
failure, so none of these first-window failures reaches its newer windows. This
is the confirmed traversal obstacle; it is not proof of a Yahoo listing boundary.
The existing full-cycle behavior and its regression expectations are preserved.

All 359 frozen canonical records lack `listingDate`. The current portfolio
instrument schema and `GlobalInstrumentResponse` do not expose one; stored NSE
market metadata also lacks it. Its accuracy therefore cannot be assessed. An
AMFI NFO date, Yahoo's first returned candle or the absence of an older candle is
not substituted for an independently verified exchange listing date.

No rate-limit, timeout or session-backoff markers were observed in this cycle's
history diagnostics. This does not rule out a transient outage or justify
classifying a technical response as confirmed absence.

## Bounded, read-only newer-history probes

[Probe evidence](etf-history-recovery-probes.json) records two diagnostic calls
through the existing `EtfHistoryProvider`, including its unchanged returned
identity gate. No acquisition writer or persistence method was invoked.

| ETF | Canonical UUID | Returned newer observations | Returned date bounds |
| --- | --- | ---: | --- |
| BANKBETF | `8f7e3355-f6b9-45cc-b0df-aebff458da54` | 40 | 2026-09-07 to 2026-11-04 |
| NIFTYIETF | `587a47c3-963c-493c-acf0-f3b45a569c72` | 3 | 2026-11-02 to 2026-11-04 |

Both requested `[2026-09-07,2026-11-06)`, independently passed identity validation,
and used 9 session HTTP attempts total, all HTTP 200. The diagnostic ceiling was
12 total / 8 per instrument, concurrency 1, HTTP timeout 5 seconds, no application
retry. These are live diagnostic counts, not persisted bars or completed recovery.
They establish newer-history availability for these two ETFs only. The other
233 provider failures remain unprobed; NV20 remains blocked.

## Recovery implementation and invariants

`app/etf_history_recovery.py` exposes:

- `HistoryRecoveryRequest`: explicit completed source cycle, 1–5 unique canonical
  UUIDs, inclusive start / exclusive end, recent range no longer than 731 days.
- `plan_history_recovery(...)`: read-only missing-session plan; no provider or
  operational checkpoint is created.
- `recover_etf_history(...)`: explicit execution API with fresh canonical metadata,
  checkpoint ownership, bounded acquisition and insert-only persistence.

Only source-cycle `ETF_ONBOARD` rows classified as retryable technical failures
are eligible. Successful source instruments are rejected. Explicit provider
identity conflicts are blocked, including NV20. Canonical UUID/ISIN/symbol,
exchange, ETF type, INR and trusted NSE provider mapping must still match fresh
portfolio-service metadata before any recovery checkpoint or acquisition.

The recovery targets an explicitly approved recent range. It does not silently
advance past a failed old window, mark that window empty or change the original
failure. Older history is always reported as `UNRESOLVED_OUTSIDE_APPROVED_RANGE`.
Even a fully recovered approved range does not imply five-year history or READY.
Missing/unverified canonical listing dates are reported and are not used to clamp
the request. A genuinely new listing can contribute its actual returned bars
while unresolved earlier sessions remain explicit.

Existing bars are revalidated using `etf_history_selection` and the persisted NSE
calendar. Recovery reuses only REAL, identity/source/currency/session-valid closes;
Yahoo quote URLs and adjusted/weekly history cannot satisfy missing daily closes.
Acquisition planning may reuse dates from separately verified providers, but it
never merges them into a return series or changes downstream provider selection.
Unknown/disabled calendars and special sessions with unknown hours fail closed.

Missing ranges are attempted newest first, serially. A technical window failure
stops that instrument; rate limits, access restrictions, timeouts, backoff and
budget exhaustion stop further provider work. Empty/incomplete responses remain
unresolved, not proven absence. There is no application retry loop or automatic
resubmission. Successful partial batches remain available for a later approved
incremental attempt.

The existing `acquire_etf_history(..., recovery_mode=True)` boundary validates each
observation, normalizes genuine NSE session labels, rejects wrong identities,
providers, adjusted sources, future/uncompleted sessions and conflicting duplicate
closes, and persists on the existing connection/table. The frozen session horizon
and fresh post-fetch retrieval clock are separate: real newly retrieved history
is accepted without expanding the approved session target.

`ON CONFLICT ... DO NOTHING` preserves every existing provider/day row, including
richer bars and concurrent inserts. Invalid pre-existing collisions remain
unchanged and unresolved; they are not counted as verified reuse. Ownership and
lease are checked before and after insertion in one transaction. No Equity,
fundamental, published research, source-cycle or readiness state is rewritten.

## Operational checkpoint and budgets

No schema migration is needed. Existing cycle-run/progress tables provide the
lease, cancellation, durable request reservations and per-instrument checkpoints.
The operational slot is `ETF_HISTORY_RECOVERY`, not the ETF publication slot.
It is an internal job namespace, not a market-data exchange. The ordinary ETF
worker cannot accidentally take it over and run a full universe after a crash.

One recovery owns that slot at a time. Resume requires the same recovery UUID,
source, ordered explicit IDs, range and budgets; only matching ACCEPTED/RUNNING
jobs can resume. Persisted successful outcomes and reservations survive restart;
reservations are never refunded. Existing per-candidate attempt limits apply.
Fresh requests remain bounded and revalidate all already-persisted bars.
Cancellation or lease loss stops acquisition/persistence; an owning worker
finalizes a durable cancellation using the existing fenced cancellation method.

| Limit | Default | Hard maximum |
| --- | ---: | ---: |
| Canonical instruments | Explicitly supplied | 5 |
| Range length / recency | Explicitly supplied | 731 calendar days |
| Provider history windows | 5 | 15 |
| Underlying session HTTP reservations | 40 | 60 |
| Acquisition concurrency | 1 | 1 |
| HTTP timeout | Existing configured timeout, capped at 15 seconds | 15 seconds |
| Spacing between session HTTP calls / windows | At least 1 second | Cannot be disabled by CLI |

HTTP reservations include cookie/crumb, metadata and library retry calls through
the existing session's `request` method. Redirect hops inside the HTTP backend are
not individually counted. Recovery disables the legacy one-session neighbouring
history probe, so an error cannot implicitly widen the approved missing range.
There are no alternate hosts, cookie bypasses, relaxed TLS checks or new feeds.
Reservations are serialized through their fenced checkpoint save by one async
lock per `RecoveryBudget` instance. A short threading lock binds that instance
to exactly one event loop; transport worker threads submit reservations to that
loop. Foreign-loop use is rejected. This is **per-instance, owner-loop
enforcement**, not a process-wide or distributed atomic counter across duplicate
budget instances. Resume loads the existing durable count; it does not restore
spent slots. Existing checkpoint ownership fences remain required.

The reservation lock covers budget checks, pacing and persistence, and is released
before network I/O. Stop state and ownership are rechecked after pacing; stop
state is checked again after saving and immediately before transport admission.
A failed or cancelled save closes the budget, retains any charged slot and
admits no HTTP. Cancellation before charging consumes no slot. Failed HTTP calls
also retain their reservations. Under contention, a new stop signal can prevent
an already charged request from reaching I/O; that reservation is not refunded.

The worker bridge rejects blocking calls on the owner loop and calls after that
loop stops. The owner loop must remain alive until transport workers drain; these
checks do not guarantee admission during arbitrary loop-shutdown races. Already
admitted/in-flight network work is bounded by its transport timeout and cannot be
synchronously revoked. No reservation lock is held during that I/O.

## CLI: plan first, execution only after separate acceptance

Run from `ai/research-engine` using the validated Python environment. Existing
database settings are read from `AIP_*`; no passwords, JWTs or full environment
manifests should appear in commands or logs.

```powershell
python -m scripts.recover_etf_history `
  --source-cycle-id 17c48038-507b-4924-b902-20f3b7df4091 `
  --instrument-id 8f7e3355-f6b9-45cc-b0df-aebff458da54 `
  --instrument-id 587a47c3-963c-493c-acf0-f3b45a569c72 `
  --start 2026-09-07 --end 2026-11-06 `
  --max-requests 2 --max-http-requests 12
```

This default opens a PostgreSQL-enforced read-only session and prints a sanitized
plan. It does not contact Yahoo or portfolio-service, create a job or write bars.
The example is an initial two-instrument scope, not a command executed in this task.
Its budget may be exhausted before the range is complete; that is a partial failure,
not permission to raise budgets automatically.

After release/recovery acceptance, the same command may add
`--apply --confirm RECOVER_ETF_HISTORY`. Apply requires existing live/non-demo
settings, enabled research persistence and an authorized bearer token supplied securely through
`AIP_ETF_RECOVERY_BEARER_TOKEN`; the token is forwarded only to the existing
portfolio-service metadata read and is never serialized in output/checkpoints.
No new endpoint or authentication policy is added. Use `--recovery-id UUID` only
to resume the identical ACCEPTED/RUNNING job. A terminal job is immutable; a
separately approved new request can reuse its verified bars.

Exit codes: 0 successful plan/approved-range completion; 2 validation, ownership or
technical exception; 3 partial recovery; 130 operator interruption. No result is a
new published recommendation. Publication remains that of the source cycle.
An apply report explicitly lists requested instruments that were not attempted
after a global stop; no attempted/successful checkpoint is invented for them.

## Changed files and local regression results

All paths below are repository-relative. No other tracked application file was
changed; no portfolio, Equity, scheduler, authentication, schema or migration file
was modified.

| File | Purpose |
| --- | --- |
| `ai/research-engine/app/etf_history.py` | Optional strict recovery validation and insert-only fenced writes; legacy behavior retained apart from propagating cancellation/ownership loss. |
| `ai/research-engine/app/etf_history_recovery.py` | Read-only planning, bounded recovery, actual HTTP/window budgets, immutable source cycle and owned resume/cancellation. |
| `ai/research-engine/scripts/recover_etf_history.py` | Plan-by-default PostgreSQL operator CLI, explicit apply gates, current canonical metadata reads, sanitized output and cleanup. |
| `ai/research-engine/tests/test_etf_history_recovery.py` | Synthetic orchestration, scoped retry/reuse, calendar/clock handling, resume/cancellation, identity and Equity/publication preservation. |
| `ai/research-engine/tests/test_etf_history_recovery_persistence.py` | Synthetic provenance/session rejection, insert-only idempotency, conflict and transaction fencing tests. |
| `ai/research-engine/tests/test_etf_history_recovery_cli.py` | No-write preflight, exact approval/configuration gates, bounded inputs, safe output, partial-scope reporting and cleanup. |
| `ai/research-engine/tests/test_etf_history_recovery_transport.py` | Fake transport through the real threaded wrapper; hidden identity/retry HTTP accounting, access stops, timeout and resume limits. |
| `ai/research-engine/tests/test_etf_history_recovery_budget.py` | Synthetic task/thread contention at limits 1, 3 and 18; exhaustion, cancellation, failed saves, owner-loop enforcement and unlocked transport I/O. |
| `docs/etf-history-recovery-baseline.json` | Sanitized read-only 236-instrument failure inventory and cycle/log evidence. |
| `docs/etf-history-recovery-probes.json` | Bounded live diagnostic evidence for two ETFs; no prices or persisted observations. |
| `docs/etf-history-official-evidence-audit.md` | Official AMFI/SBI contracts, source retrieval evidence, exact identity gates and unresolved NAV/TER boundaries. |
| `docs/etf-history-targeted-recovery.md` | Root cause, execution contract, limits, validation, remaining gaps and rollback. |

Run from `ai/research-engine` with Python 3.12 and the declared test dependencies:

```powershell
python -m pytest -q tests/test_etf_history_recovery.py tests/test_etf_history_recovery_persistence.py tests/test_etf_history_recovery_cli.py tests/test_etf_history_identity_classification.py tests/test_etf_history_bounded_session.py tests/test_etf_five_year_history.py tests/test_historical_market_data.py tests/test_radar_history_remediation.py tests/test_etf_readiness.py tests/test_etf_market_pair_readiness.py tests/test_etf_amfi_navall.py tests/test_etf_identity_resolution.py tests/test_sbi_ter_exact_name_identity.py tests/test_sbi_ter_real_values.py tests/test_etf_sbi_ter_cycle.py
python -m pytest -q tests/test_etf_history_recovery_transport.py
```

Final local results on 2026-11-06: **333 passed, 0 failed, 0 skipped in 70.23 seconds**
for the first command; **10 passed, 0 failed, 0 skipped in 3.90 seconds** for the
transport command. Total: **343 distinct focused regressions passed**. Each run
reported one existing Starlette/httpx deprecation warning. No full application
suite was run. All test market observations and transport responses are synthetic;
no test contacted a live acquisition source or production database.

The initial aggregate command in `.venv` stopped at collection with three missing
`openpyxl` import errors. The installed Python 3.12 environment already had the
dependency and passed the final commands; no package installation or validation
skip was used. Implementation failures found during review (retrieval clock,
resume boundaries, unusable calendars and durable cancellation finalization) were
corrected and covered by regression tests. No implementation failures remain in
the final focused runs. `git diff --check` passes.

## Official NAV and TER evidence

See the [official evidence audit](etf-history-official-evidence-audit.md) for URLs,
retrieval dates, source contracts, hashes, exact-ISIN/AMC identity checks and tests.
The existing NAVAll adapter emits fund house only; zero NAV is an acquisition
boundary, not proof of absent source NAV. Exact ISIN alone does not supply a
reviewed per-unit, currency, distribution-class and eligible NAV-date contract.
No unfinished NAV adapter is enabled. SBI TER retains exact scheme/AMC/ISIN joins;
non-SBI TER remains unavailable. A current SBI workbook fetch failed TLS chain
validation on this host; certificate checks were preserved. Existing 358 fund-house
and 18 SBI TER observations are untouched. No AUM, holdings, spread or tracking
metrics are populated by history recovery.

## Rollback and remaining acceptance

The implementation is not deployed. Code rollback is removal/revert of this
isolated patch; there are no migrations or configuration/scheduler changes.
If execution is separately approved later, stop the CLI/request cancellation
before rollback and retain its successfully verified new bars. Since existing
bars are never updated or deleted, data rollback is normally unnecessary.
Do not reset or delete by a broad instrument/date range. Any exceptional removal
of invalid new evidence requires separately reviewed exact row/provenance checks
and a backup; concurrent valid rows must remain intact.

NV20 is blocked by its explicit INDEX response. The 233 unprobed provider failures
have no confirmed recovery result. Missing historical listing boundaries, older
history and unresolved recent sessions remain explicit. No full-history/readiness
or actionable-recommendation guarantee is made, and deployment/live execution
require separate acceptance after review of the bounded plan.

## Incomplete-range diagnostics (additive, diagnostics only)

`HISTORICAL_RANGE_INCOMPLETE` semantics are unchanged: a session counts as recovered only when a
valid stored bar exists for a completed NSE session. The following fields only explain a gap; none
affects status, eligibility, budgets, insert-only persistence, the calendar or date conversion.
Older checkpoints lack them, and the sanitized report then omits them.

Per instrument outcome (also in the sanitized report):

| Field | Meaning |
| --- | --- |
| `incomplete_detail` | List, fixed order, all that apply; only set when the outcome is `HISTORICAL_RANGE_INCOMPLETE`, else `[]`. |
| `missing_session_windows` | Actual missing completed sessions, same order as `remaining_windows`: `start`, `end_exclusive`, `first_missing_session`, `last_missing_session`, `missing_session_count`. A window may span a weekend or holiday. |
| `expected_latest_completed_session` / `expected_completed_sessions` | Latest and number of completed NSE sessions required in the approved range at the recovery clock. |
| `provider_first_accepted_date` / `provider_last_accepted_date` | First and last trading dates the provider returned that passed validation in this run. |
| `rejected_invalid_close_count` | Provider rows dropped for a missing, NaN, infinite, zero or negative close, plus any returned observation with such a close. Rejection behaviour is unchanged. |

Each provider window additionally carries `accepted_provider_first_date`, `accepted_provider_last_date`
and `rejected_invalid_close_count`.

`incomplete_detail` values (they are based on valid stored sessions, never on a listing date):

- `LATEST_SESSION_NOT_RETURNED`: the latest expected session has no valid bar.
- `PROVIDER_HISTORY_STARTS_AFTER_RANGE_START`: the first expected session is missing but later ones are present.
  This does not establish a listing date or that earlier history does not exist.
- `INTERIOR_SESSIONS_MISSING`: expected sessions are missing between the first and last valid stored sessions.

A technical failure keeps its own reason and `incomplete_detail` stays `[]`; its gaps still appear in
`missing_session_windows`. A planning read whose clock precedes the bars' retrieval time treats those
bars as future (`FUTURE_HISTORY`); that existing behaviour is unchanged.
