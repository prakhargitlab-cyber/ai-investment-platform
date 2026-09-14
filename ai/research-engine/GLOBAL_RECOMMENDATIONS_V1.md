# Global recommendation lifecycle MVP

## Architecture

Java research-service Flyway owns additive migration **V13**. Python shares that schema through
`SqliteResearchPersistence` / `PostgresResearchPersistence`; SQLite mirrors it for deterministic tests.
No acquisition, migration backfill, Rule Engine weight changes, or provider calls were added.

The explicit `run_global_opportunity_cycle()` job enumerates the canonical ACTIVE EQUITY catalog,
filters NSE, then calls the existing `GlobalOpportunityOrchestrator`. It reuses GlobalScanner,
Stage-B technical/sector enrichment, `STOCK_RULE_ENGINE_V1`, and `GLOBAL_OPPORTUNITY_RANKER_V1`.
Catalog and benchmark metadata reads occur before ranking; portfolio/watchlist/broker holdings
never supply candidates or affect any public score.

The default deep shortlist is 25 (configurable 1–100). Up to 25 previous public recommendations,
ordered by oldest projection update, have an additional review budget. This does not change
the scanner shortlist or scoring. Reviews that cannot be evaluated remain visible with
`NOT_EVALUATED_THIS_CYCLE`; the application does not claim their thesis was refreshed.

Within one database transaction, the job inserts immutable opportunity snapshots, **reads those
persisted rows back**, evaluates recommendations, appends changed recommendations, updates current
state, and publishes the Top-N projection. A failure rolls back the publication. The POST route
serializes cycles in the existing single-worker application. Multiple application processes would
need a distributed job coordinator before concurrent scheduling is enabled.

## Tables

* `global_opportunity_snapshot`: one immutable instrument row per cycle; versions, scores,
  eligibility/suppression, rank, price/date, reasons, and full technical/sector/rule evidence.
* `stock_recommendation_history`: append-only decisions, snapshot FK, fingerprint, reference price,
  short/long levels, reasons, and frozen evidence.
* `recommendation_current_state`: latest history FK, original recommendation anchor, separate
  short/long lifecycle/action, price, target distances, and reasons.
* `global_opportunity_top_selection`: immutable cycle publication with independent horizon picks.
* `recommendation_backtest_run`: immutable inputs, metrics, examples, and per-horizon outcome status.

Both PostgreSQL and SQLite reject UPDATE/DELETE of the immutable tables. Current-state updates
do not mutate history. Complete JSON documents are stored as text alongside queryable columns,
following a shared serialization contract; NULL numeric evidence remains SQL NULL / JSON null.

## Recommendation policy: RECOMMENDATION_ENGINE_V1

These are explicit starting rules, not calibrated investment forecasts or a second scoring engine.

* A positive candidate requires existing rank eligibility, opportunity score ≥65, confidence ≥60,
  and coverage ≥60. A qualified score ≥80 is a strong-buy candidate.
* Short BUY also requires UPTREND, BREAKOUT, PULLBACK_IN_UPTREND, or REVERSAL_CANDIDATE technical state.
* Long ACCUMULATE additionally requires available quality and valuation dimensions ≥60.
* Unavailable dimensions are not zero and do not constitute deterioration.
* Critical existing risk overrides, governance ≤20, or two available weak core dimensions ≤30
  prompt long EXIT_REVIEW. One weak core dimension prompts REDUCE.
* New investors and holders receive separate actions. Holder actions also reflect partial-profit,
  reduction, and exit-review lifecycle conditions. No LLM participates.

The fingerprint covers instrument/version, all actions, a rounded reference-price bucket,
rounded score/confidence/coverage state, precise stored levels, and deterministic evidence content.
Completion/cache clocks alone do not change it. Comparison with the latest fingerprint avoids
identical history rows while allowing a genuine A→B→A change to be recorded.

## Price ranges

All levels are in the persisted technical price's currency. V1 uses only explicit evidence:

* When `0 < support ≤ price < resistance`, short entry is support through
  `min(price, support × 1.03)` and target 1 is resistance.
* With positive ATR and support greater than ATR, target 2 is resistance + ATR;
  invalidation is support − ATR.
* An explicitly persisted positive fair value supports long accumulation at 80–90% of fair value.
  Bull target and invalidation require their own valid evidence. A valuation score or analyst
  consensus is never relabeled as fair value. The current upstream pipeline does not synthesize
  intrinsic value, so these long levels can legitimately remain unavailable.
* Unavailable levels are NULL and produce `PRICE_RANGE_EVIDENCE_INSUFFICIENT`.

## Lifecycle

Original recommendation levels remain anchored independently of later price/score changes.
Within 3% of a target is TARGET_APPROACHING; hitting target 1 triggers SHORT PARTIAL_PROFIT and
risk/reward-compression reasons. Target 2 proximity/reach has a separate reason. A valid long
thesis can remain HOLD simultaneously. Crossing invalidation prompts SHORT EXIT / LONG EXIT_REVIEW;
within 3% above invalidation is INVALIDATION_APPROACHING. Entry bands and proximity have separate
states. Thesis deterioration takes precedence over profit conditions.

No position membership is inferred from a recommendation's lifecycle: holder actions are a
public recommendation for someone who already holds it, not an instruction executed on an account.

## Top-N and dashboard

`top_n` defaults to 4 and accepts 2–4. Short BUY and long ACCUMULATE/TOP_UP lists are selected
separately after existing suppression gates. Each uses the existing opportunity ordering:
score, confidence, coverage, Rule Engine score, then canonical UUID. Lists may contain fewer
than N when fewer candidates qualify; they are never padded with fabricated picks.

Dashboard reads persisted selection, snapshot, history, and current-state rows. It does not run
the scanner, Rule Engine, recommendation engine, readiness ensure, or providers. UI membership
annotations use the already-loaded portfolio/watchlist state **after** ranking. The generation
timestamp and missing/stale areas expose the cycle's data status.

## APIs and controlled DEV execution

* `GET /api/v1/research/opportunities/current`
* `GET /api/v1/research/opportunities/history/{instrumentId}`
* `POST /api/v1/research/opportunities/cycles`
* `GET /api/v1/research/backtesting/runs`
* `POST /api/v1/research/backtesting/runs`

The existing gateway already routes `/api/v1/research/**` to the research engine. GET routes are
read-only. With persistence disabled, reads return empty results and write endpoints return 503.

After research-service applies V13, submit a small canonical candidate set first:

```json
{"top_n": 2, "shortlist_limit": 2, "candidate_ids": ["<existing-canonical-NSE-UUID>"]}
```

Omit `candidate_ids` for a global canonical-universe run. The job does not fetch missing stock data;
it can report no eligible results when the persisted evidence is insufficient. It is intentionally
not invoked from a dashboard effect or GET. No broad live cycle was required for validation.

Example backtest POST (UTC instants; end must not be in the future):

```json
{"start":"2026-01-01T00:00:00Z","end":"2026-09-01T00:00:00Z","market":"NSE","horizon":"SHORT_TERM"}
```

## Backtesting V1 and temporal guards

This MVP evaluates **recorded historical recommendations**, not hypothetical recommendations for
dates before the system had history. The selected date range defines the historical recommendation
cohort; each row's generation timestamp is its evaluation date T. No current event/exposure tables
are queried during a backtest.

Frozen evidence must have publication/public-availability/discovery/retrieval/computation timestamps
no later than T. News features use V12's `latest_known_features` selection and typed model validation,
in addition to the recursive timestamp checks. Persisted price normalization checks both observation
and retrieval timestamps for the entry. Daily closes retain their original retrieval clock, so later
corrections cannot be used as an earlier entry. Future prices are outcome data only.

Horizons are 7, 30, 91, 182, and 365 calendar days (1W/1M/3M/6M/1Y). The first available persisted
close on/after the horizon, within seven days, supplies the outcome. Missing entry/future prices
have explicit statuses and are excluded from return/hit-rate denominators. Metrics include cohort
count, evaluated/missing counts, hit rate (>0), mean/median/best/worst returns, close-based maximum
adverse excursion, and benchmark excess when explicit benchmark evidence exists. The optional
`benchmark_id` selects a canonical persisted benchmark; no benchmark is guessed.

Returns are unadjusted price returns, not simulated executions, dividend-adjusted total returns,
or a transaction-cost model. The UI provides dates, market, horizon, run/selection, all five horizon
metrics, and winner/loser examples without complex charts.

## Validation notes

Run focused Python tests in `tests/test_recommendation_lifecycle.py` with existing scanner,
ranker, and orchestration tests. `test_recommendation_postgres.py` is opt-in using
`RECOMMENDATION_TEST_PG_PORT` against an isolated `recommendation_validation` database already
migrated by Flyway. The complete research-engine suite is also required.

V12 uses PostgreSQL types and PL/pgSQL triggers, which the existing H2 test profile cannot execute.
Run Maven/Flyway validation with disposable PostgreSQL datasource overrides and a second database
for `dailyBarsUpgradeJdbcUrl`. The upgrade assertion now expects V10→V13 (three migrations).
V1–V12 are unchanged. Frontend production builds and rendering tests run in Docker.

The broader frontend suite has three existing source-assertion failures also reproduced with
unchanged HEAD sources: visible-stock readiness navigation, shareholding drawer company lookup,
and drawer provider/sector/industry projection. The new recommendation rendering tests pass.

## Known follow-ups

* Freshness tuning.
* SHAREHOLDING behavior in global “Find required data”; acquisition policy is unchanged.
* Recommendation threshold calibration.
* Entry/target calibration and explicit fair-value evidence availability.
* Prediction/ML.
* Benchmark calibration, adjusted-return/corporate-action handling, and execution costs.
* Distributed job coordination before scheduling concurrent application instances.
