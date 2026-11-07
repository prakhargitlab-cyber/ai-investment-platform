# Phase 2: ETF history recovery candidate discovery

Worktree: `.tmp/etf-history-recovery`, relative to the primary repository checkout.
Branch: `fix/etf-targeted-history-recovery`.
Source cycle: `17c48038-507b-4924-b902-20f3b7df4091`.

This phase implements and tests discovery only. No live discovery, provider call,
recovery, publication, deployment, container rebuild or live PostgreSQL mutation
was performed. Tests used synthetic local data, including a disposable PostgreSQL
16 container launched from an existing image and removed after verification.
The root `main` worktree was not changed. No commit, push or merge was performed.

## Supplied operational baseline

The accepted pilot inserted 43 bars for BANKBETF and NIFTYIETF, bringing the
database to 134,043 bars on 125 ETFs. Both provider windows succeeded, but the
approved range remained incomplete. Its reported recovery outcome is
`PARTIAL_FAILURE` / `HISTORICAL_RANGE_INCOMPLETE`, with 11/12 HTTP reservations,
2/2 provider-window reservations and zero preserved conflicts. The immutable
source cycle remains COMPLETED. A post-cycle backup with verified SHA-256 exists.
These are the operator-supplied verified baseline, not new database measurements
made during this coding task.

Existing recovery `4e4b71b9-ba49-414a-b625-685fc340cf57` is audit history. Discovery
never claims or resumes it. A terminal recovery cannot be reused as a new run;
existing execution guards reject COMPLETED, FAILED and CANCELLED run IDs.

## Architecture and transaction boundary

`app.etf_history_discovery.discover_etf_history_recovery(...)` is a separate
read-only Python API. `scripts.discover_etf_history_recovery` is its operator CLI;
no public HTTP endpoint is added.

The API dispatches one read callback through the existing repository persistence
worker. Inside it, `PostgresResearchPersistence.read_only_snapshot()` starts and
verifies a `REPEATABLE READ READ ONLY` PostgreSQL transaction. Nested existing
repository reads stay in that transaction through the adapter's outer depth.
The context refuses pending caller transactions and does not commit or change
their work. The CLI additionally sets the connection's default read-only state.
SQLite's query-only analogue exists solely for focused local/unit fixtures.

Discovery reads the completed source selection/progress, current stored bars,
persisted NSE calendar and previous recovery records. No connection to Yahoo,
portfolio-service, NSE or another provider is made. No credentials or source
response bodies are returned. PostgreSQL rejects persistent writes even if an
accidental write is injected into a discovery callback.

Shared `load_recovery_source()`, `source_candidate_eligibility()` and
`coverage_details()` serve discovery and explicit recovery. They preserve the
trusted NSE mapping, canonical UUID/ISIN/symbol, INR, ETF identity, calendar,
REAL provenance and unadjusted-price rules. Duplicate UUID/ISIN/symbol claims,
identity conflicts such as NV20's INDEX response, nontechnical outcomes and
already covered ranges are explicitly excluded. Empty/invalid source contracts
raise errors rather than producing an apparently valid empty shortlist.

No new persistence store, database table, schema migration, checkpoint phase,
publication path or scoring/readiness calculation was introduced. Snapshot
methods are additive; existing Equity paths do not invoke or change them.

## Coverage, ranking and output

The default inspected range is 366 calendar days ending at tomorrow's UTC date
(exclusive), considering only sessions completed at the reported `as_of` clock.
Paired explicit dates may select a recent range of at most 731 calendar days,
using the same execution range validation. All counts and first/last available
dates are labeled `coverage_scope=APPROVED_RANGE_ONLY`; they are not lifetime
history totals or proof of five-year completeness.

Missing listing dates are `MISSING`; an existing but independently unverified
date is `UNVERIFIED_NOT_USED`. Neither is used to erase missing sessions. Yahoo's
earliest stored candle never establishes a listing boundary or an absent prefix.
Older history remains `UNRESOLVED_OUTSIDE_APPROVED_RANGE`.

The ranking is deterministic:

1. Verified partial coverage with a matching previous recovery record.
2. More missing completed sessions within that group.
3. Primary symbol, ISIN and UUID as stable lexical tie-breakers.

The first group describes current validated partial coverage plus recorded prior
recovery activity; it does not prove which job inserted those bars or predict
provider success. A prior COMPLETED/FAILED status never overrides current gaps.
Previously partially recovered instruments remain eligible only while a genuine
gap exists. The summary's partial count uses this same current-coverage/prior-run
definition.

The limit is an integer from 1 to 5, default 5. The shortlist is bounded; discovery
still assesses the original failed-history set so summary counts and exclusions
refer to that entire set. `assessments` includes original unsuccessful checkpoints
(including nontechnical outcomes for explicit exclusion), not successful original
history candidates. Each assessment includes identity, failure classification,
range coverage/bounds, listing status, missing windows, previous recovery status,
eligibility/exclusion, priority and rationale. The summary gives original failed,
eligible, excluded, partial and returned counts.

`previous_recovery.checkpoint_status` is the database lifecycle status;
`recovery_status` is the reported approved-range result, such as PARTIAL_FAILURE.
These remain distinct from instrument/provider results. The previous ID is never
authorization, and `execution_authorized=false` is explicit in the output.
Malformed matching recovery scopes raise a stable error without rewriting them.

## Commands

Run in the approved database connection environment from the existing worktree;
database settings use the existing `AIP_*` variables. Discovery needs no bearer
token, execution confirmation, live-data flag or persistence-write enablement.
It accepts none of the execution flags.

From the recovery worktree root, with the validated Python 3.12 environment active:

```powershell
Set-Location 'ai/research-engine'
python -m scripts.discover_etf_history_recovery --source-cycle-id 17c48038-507b-4924-b902-20f3b7df4091 --limit 5
```

Explicit range example, also read-only:

```powershell
python -m scripts.discover_etf_history_recovery `
  --source-cycle-id 17c48038-507b-4924-b902-20f3b7df4091 `
  --limit 5 --start 2026-09-07 --end 2026-11-06
```

These commands were not run against the live database during this task. A result
is a reviewable shortlist, not permission to execute. The existing recovery CLI
still requires explicit UUIDs, `--apply`, exact `--confirm RECOVER_ETF_HISTORY`,
an authorized environment-only bearer token, live/non-demo settings, enabled
persistence and unchanged window/HTTP budgets. It does not chain batches, retry
automatically, publish or recalculate recommendations.

## Sample JSON and reporting distinction

[Full sanitized sample](etf-history-recovery-discovery-sample.json) is generated
from four **synthetic disposable fixtures**, not from the current 359-ETF source.
It has two eligible candidates, one verified partial candidate, one explicit
provider identity conflict and one already covered range. The sample UUIDs and
symbols are synthetic. Its source cycle is also synthetic.

```json
{
  "mode": "READ_ONLY",
  "execution_authorized": false,
  "provider_requests": 0,
  "summary": {
    "source_universe": 4,
    "original_failed_history_candidates": 4,
    "eligible_candidates": 2,
    "excluded_candidates": 2,
    "partially_recovered_candidates": 1,
    "returned_shortlist_count": 2
  }
}
```

The recovery report preserves separate provider and range meanings:

| Field/value | Meaning |
| --- | --- |
| `windows[].provider_status=SUCCESS_WITH_DATA` | Provider returned acceptable bars; some may collide with preserved rows. |
| `accepted_provider_bar_count` | Acceptable provider bars before insert-only collision handling. |
| `persisted_bar_count` | Newly inserted bars recorded by the outcome/window; existing observations remain unchanged. |
| `recovery_outcome=RECOVERED_APPROVED_RANGE` | Required completed sessions in the approved range are covered. |
| `recovery_outcome=HISTORICAL_RANGE_INCOMPLETE` | Required sessions still have unresolved gaps, including after successful provider responses. |
| `recovery_outcome=TECHNICAL_FAILURE` | An actual technical failure occurred. |
| `recovery_outcome=UNRESOLVED` | A budget/operational stop; it does not invent a failed provider response. |

Sanitized reports include per-window/provider outcomes, inserted counts, remaining
windows, total recorded insertions, HTTP reservations/budget and provider-window
reservations/budget. Existing aliases remain supported. Conservative technical,
gap and stop evidence outranks a contradictory completion flag. Additive JSON
fields and legacy inference require no checkpoint-schema change or rewrite.

## Changed files in Phase 2

Paths are relative to `ai/research-engine/` unless prefixed with `docs/`.
Existing Phase-1 changes in the worktree were preserved.

| File | Phase-2 change |
| --- | --- |
| `app/etf_history_discovery.py` | New read-only deterministic discovery API and assessment output. |
| `scripts/discover_etf_history_recovery.py` | New discovery-only CLI, bounded arguments and sanitized output. |
| `app/etf_history_recovery.py` | Shared source/eligibility/coverage helpers and additive range-outcome reporting; execution gates preserved. |
| `app/etf_history.py` | Add accepted-provider versus inserted counts/status without changing price/session validation or collision behavior. |
| `app/persistence.py` | Add snapshot contract and SQLite query-only fixture implementation. |
| `app/postgres_persistence.py` | Add enforced, verified PostgreSQL repeatable-read/read-only snapshot context. |
| `scripts/recover_etf_history.py` | Preserve provider/range outcome distinctions and expose safe counts/budgets; all apply gates retained. |
| `tests/test_etf_history_discovery.py` | New deterministic eligibility, coverage, exclusion, prior-state and no-write/no-network tests. |
| `tests/test_etf_history_discovery_cli.py` | New command contract, sanitization and read-only-only surface tests. |
| `tests/test_etf_history_discovery_postgres.py` | New real PostgreSQL enforcement/snapshot tests on disposable synthetic data. |
| `tests/test_etf_history_recovery.py` | Add immutable terminal-ID refusal tests. |
| `tests/test_etf_history_recovery_persistence.py` | Verify accepted provider evidence can coexist with zero inserts under preserved collisions. |
| `tests/test_etf_history_recovery_cli.py` | Pilot-shaped and contradictory-state reporting regressions; execution gate regressions retained. |
| `docs/etf-history-recovery-discovery.md` | New Phase-2 architecture, commands, verification and limitations. |
| `docs/etf-history-recovery-discovery-sample.json` | New explicitly synthetic sanitized output sample. |
| `docs/etf-history-targeted-recovery.md` | Link the Phase-2 read-only discovery/reporting contract. |

## Focused validation

Final command from `ai/research-engine`:

```powershell
python -m pytest -q tests/test_etf_history_recovery.py tests/test_etf_history_recovery_persistence.py tests/test_etf_history_recovery_cli.py tests/test_etf_history_recovery_transport.py tests/test_etf_history_discovery.py tests/test_etf_history_discovery_cli.py tests/test_etf_history_discovery_postgres.py
```

Result: **240 passed, 0 failed, 0 skipped in 8.15 seconds**. One existing
Starlette/httpx deprecation warning. No full application suite was run.
The PostgreSQL tests used `AIP_TEST_ETF_DISCOVERY_DSN` only for the loopback-bound
disposable database `etf_history_discovery_test`; they refuse other database names,
non-loopback hosts and the standard production port. No production DSN was used.
Ten tests exercise actual PostgreSQL, including rejected INSERT/UPDATE/DELETE/DDL,
snapshot consistency across concurrent fixture writes, pending-work refusal,
read-only API enforcement, source/bars preservation and malformed source failure.

`git diff --check` passed; new untracked files were also checked explicitly.
Only the existing isolated recovery worktree was changed.

## Remaining limits and release decision

- The live shortlist and current eligibility counts have not been measured in
  this phase. The supplied 134,043/125 baseline is preserved, not reasserted from
  newly queried production data.
- Listing boundaries remain unverified. Calendar gaps are expected-session gaps,
  not proof that the ETF traded on every missing day or that Yahoo can supply it.
- Priority expresses validated coverage and gap size, never provider success
  probability or an investment recommendation.
- Prior metadata comes from existing internal checkpoints. A later recovery still
  revalidates current portfolio-service identity and every acquired observation.
- Reported insertion counts are recorded outcome/window counts, not reconstructed
  lifetime totals. A crash between a bar commit and its progress receipt can leave
  valid reused bars without an attributed insertion receipt; discovery relies on
  current validated coverage instead of guessing that attribution.
- Discovery does not repair unavailable NAV, TER, spread, holdings, AUM or tracking
  evidence, and does not change READY/actionable gates or published research.

The patch is ready for review. No deployment or live recovery acceptance is
implied. Code rollback removes the Phase-2 additions; no data rollback or migration
is required because discovery writes no research data.

## Staged security-screening fixture exceptions

The following test literals are synthetic fixtures reviewed from the
Git index. They exercise credential redaction, mocked bearer validation and
diagnostic sanitization; they are not operational credentials. Preserve their
format-sensitive test behavior and report these findings as reviewed exceptions.

| Repository-relative location | Fixture | Purpose |
| --- | --- | --- |
| `ai/research-engine/tests/test_etf_history_discovery_cli.py:22` | `SECRET` | Assert that discovery output and errors never echo the synthetic sentinel. |
| `ai/research-engine/tests/test_etf_history_recovery_cli.py:22` | `SECRET` | Mock environment-only bearer input and assert CLI output/error redaction. |
| `ai/research-engine/tests/test_etf_history_recovery_diagnostics.py:550` | `secret` | Inject synthetic malformed fields and assert sanitized diagnostics exclude them. |
| `ai/research-engine/tests/test_etf_history_recovery_diagnostics.py:682` | `secret` | Raise a synthetic optional-metadata error and assert neither output nor logs reveal it. |
| `ai/research-engine/tests/test_etf_history_recovery_persistence.py:90` | `source_url` userinfo | Verify that a synthetic observation with embedded URL credentials fails provenance validation before persistence; the provider is mocked and the URL is never requested. |

These are exceptions for the reviewed literals, not a blanket exclusion for
test files, variable names or future values. No fixture values are reproduced
here, and the screening rules remain enabled. The documentation's worktree and
CLI instructions use repository-relative paths rather than a local drive path.
