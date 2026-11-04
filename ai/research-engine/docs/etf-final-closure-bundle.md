# ETF final closure bundle — one controlled remediation run

## Actual accepted baseline (2026-11-04, deployed code 8ac1edb)

Application `evaluate_etf_readiness`, against real PostgreSQL:

- 359 active NSE ETFs.
- **351** READY_FRESH across all mandatory checks.
- **8** blocked (6 technical/identity, 2 missing price):
  - `BANKCASE`: official NSE metadata identity conflict.
  - `LTGILTCASE`, `NIFTYCASE`, `NXT100CASE`, `SILVERCASE`,
    `SML100CASE`: UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL.
  - `GSEC10ABSL`, `LIQUIDBETA`: MARKET_PRICE MISSING.
- NAV: 349 READY_STALE, 10 MISSING, zero READY_FRESH.
- Holdings/TER/AUM/tracking error/tracking difference: zero fresh.
- INDEX_CONSTITUENTS and INDEX_VALUATION: NOT_IMPLEMENTED.

`READY_FRESH` is meaningful *at the report clock*, not a perpetual status.

## Consolidated deliverables

1. `app/etf_completion_work_plan.py` transforms the full application
   acceptance report into **one strict operator action plan**. It rejects
   incomplete blocker counts, unknown mandatory statuses, duplicate
   instruments and invalid supporting counts. Every action explicitly
   disallows automatic resolution.
2. `app/etf_completion_acceptance_cli.py --work-plan` emits both the
   original full readiness results AND the categorized remediation plan
   in one read-only PostgreSQL invocation (no provider calls).
3. `sql/etf_final_closure_dossier.sql` shows the latest persisted attempt,
   source reason, canonical ISIN and evidence counts for *actual* failures
   across the universe, not five hardcoded ticker mappings.
4. Regression tests check the real production-shaped eight blockers and
   trust-gate failure cases.

## One operator validation command (Windows PowerShell)

Run on `main` after merging and deploying a new research-engine image
(older deployed containers cannot have the new `--work-plan` option):

```powershell
$server = "k3d-ai-investment-dev-server-0"
$namespace = "ai-investment"

# The deployed app provides exact application readiness + actionable gap plan.
docker exec $server kubectl -n $namespace exec deployment/research-engine `
    -c research-engine -- python -m app.etf_completion_acceptance_cli --work-plan
if ($LASTEXITCODE -ne 0) { throw "ETF completion report failed" }

# The new SQL is read-only and supplements the application report with
# observed acquisition evidence; it does not authorize changed identities.
$sql = Get-Content "ai/research-engine/sql/etf_final_closure_dossier.sql" -Raw
$sql | docker exec -i $server kubectl -n $namespace exec -i postgres-0 `
    -c postgres -- psql -U investment -d investment -v ON_ERROR_STOP=1
if ($LASTEXITCODE -ne 0) { throw "ETF evidence dossier failed" }
```

Local regression gate before merge:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  ai/research-engine/tests/test_etf_completion_work_plan.py `
  ai/research-engine/tests/test_etf_completion_acceptance.py `
  ai/research-engine/tests/test_etf_readiness.py `
  ai/research-engine/tests/test_etf_identity_readiness.py `
  ai/research-engine/tests/test_etf_official_document_fetch.py `
  ai/research-engine/tests/test_etf_document_trust.py -q
```

## Closure sequence: no speculative ingestion

1. **Two missing-price cases:** operator checks current canonical
   `globalInstrumentId` and directly uses existing authenticated
   `POST /api/v1/etf-radar/evidence` (max 5 unique canonical IDs) only
   if an actual new official market observation can be obtained. This
   endpoint can write evidence and attempts. It is never called by the
   read-only plan; use it only with explicit authorized operator action.
2. **Five symbol ambiguities:** independently compare exact canonical
   ISIN/symbol against fresh NSE ETF security-list rows and market-watch
   `symbol,series,identifier`. Reject duplicate/unknown rows; do not
   rewrite canonical symbols or add aliases based on names alone.
3. **BANKCASE:** examine exact NSE GetQuoteApi metadata vs canonical
   symbol/ISIN, exchange series and latest verification attempt; require
   later independently verified identity evidence before clearing conflict.
4. **NAV:** a source exists for 349 but every observation is STALE under
   the existing one-day threshold. Only verified newer NAV values may
   replace or add observations, never rebasing old values to today.
5. **Issuer metrics:** Holdings, TER, AUM, tracking metrics, benchmark,
   replication and AMC information need actual accessible official
   documents, canonical-ISIN-verified scheme sections, effective/as-of
   dates, source hashes and existing ingestion contracts.
   `Nippon` source documents requiring OTP are not automatically fetched;
   `SBI` scheme names/codes are still staged, **not** approved NSDL
   code→ISIN mappings. These restrictions remain non-negotiable.
6. **Final acceptance:** run the original application evaluator again.
   Ready can only increase on evidence that passed existing source and
   freshness gates. Continue to report unavailable metrics explicitly.

## Honest acceptance boundary

The bundle **does not claim completion of the missing issuer datasets**.
Without independently accessible official holdings/TER/AUM/tracking
documents and direct scheme→ISIN evidence, no code-only change can fill
those 359×metric gaps honestly. A trust-preserving terminal outcome is
`MISSING_OFFICIAL_SOURCE` or `NOT_IMPLEMENTED`, not an invented number.
