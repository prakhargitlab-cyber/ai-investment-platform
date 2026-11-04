# ETF evidence completion milestone — one consolidated acceptance gate

## Objective and current measured baseline

Finish ETF engineering without inventing investment evidence or reopening already
validated identity work. Actual local k3d PostgreSQL as of **2026-11-04**:

| Evidence | Unique ETFs | Scope |
| --- | ---: | --- |
| Canonical-linked NSE official listing observations | 359 | Research |
| Market price | 351 | Research |
| Trading volume | 353 | Research |
| Both mandatory price and volume plus listing | 351 | Raw presence only |
| NAV observations | 349 | Dedicated table |
| Underlying holdings snapshots | 0 | Dedicated table |
| TER/AUM/tracking error/tracking difference | 0 | Generic ETF facts |

Five SBI review candidates were matched in BOTH portfolio and research
canonical identities with no unresolved NSE metadata conflict, **but** the NSE
circular does not prove SBI workbook NSDL scheme code -> ISIN. They remain
unapproved. Do not promote them to `ReviewedSbiSchemeIdentity`.

## Delivered as one milestone

1. `sql/etf_evidence_completion_audit.sql`: one read-only PostgreSQL
   inventory, per-universe identity and market-data diagnostic, supporting
   evidence gap classification and prioritized first 25 missing-evidence items.
   Queries are derived from actual canonical rows, never ticker maps.
2. `app/etf_completion_acceptance.py`: one-pass bounded (max 500) evaluation
   of the **existing** `evaluate_etf_readiness` contract, with no replacement
   readiness logic and no production writes. It records EVERY mandatory and
   supporting status including `NOT_IMPLEMENTED`, plus the first 25 blocked
   instruments, and caches repeated read-only listing/attempt/bar calls.
3. `app/etf_completion_acceptance_cli.py`: explicit PostgreSQL read-only
   session and existing authenticated application DB settings. Reads actual
   `portfolio.instrument_master` and `research` evidence; neither calls
   providers nor publishes a cycle. This is intended to run inside the
   configured research-engine environment **after normal deployment**, not
   against an invented local database.
4. Offline acceptance tests including fail-closed invalid universes,
   inconsistent evaluator summaries and caching.

## Run now against the existing k3d PostgreSQL

From the **repository root** on Windows PowerShell, after checking out this PR:

```powershell
$server = "k3d-ai-investment-dev-server-0"
$namespace = "ai-investment"
$sql = Get-Content "ai/research-engine/sql/etf_evidence_completion_audit.sql" -Raw
$sql | docker exec -i $server kubectl -n $namespace exec -i postgres-0 -c postgres -- `
    psql -U investment -d investment -v ON_ERROR_STOP=1
if ($LASTEXITCODE -ne 0) { throw "ETF completion audit failed" }
```

This is explicitly `BEGIN TRANSACTION READ ONLY`; does not apply migrations,
create rows or start any provider acquisition. The status
`MANDATORY_EVIDENCE_PRESENT_RECENT_NOT_READINESS_VERIFIED` is **not**
`READY_FRESH`; the exact evaluator handles publication/provenance/conflict
semantics that a SQL inventory cannot fully reproduce.

### Actual application-level readiness (after reviewed deployment)

```powershell
# Inside a running research-engine container with the matching new code,
# normal AIP_RESEARCH_DATABASE_* settings and DB access:
python -m app.etf_completion_acceptance_cli
```

DO NOT call the old deployed image and expect it to contain this CLI. Do not
run a full ETF radar cycle. Operator must ensure app database identity and
the service's normal PostgreSQL connection configuration. The CLI uses
`default_transaction_read_only=on` for every audit SELECT, rejects more than
500 ETFs, does not print credentials, does not ingest or score, and does not
approve TER or holdings. It may produce an explicit error if the live user
cannot access `portfolio.instrument_master`; arrange authorized read-only
access rather than bypassing RBAC.

## Tests before merge

```powershell
.\.venv\Scripts\python.exe -m pytest `
  ai/research-engine/tests/test_etf_completion_acceptance.py `
  ai/research-engine/tests/test_etf_readiness.py `
  ai/research-engine/tests/test_etf_identity_readiness.py `
  ai/research-engine/tests/test_sbi_ter_canonical_preflight.py `
  ai/research-engine/tests/test_sbi_ter_real_data_audit.py -q
```

## Completion workstreams and evidence gates

**P0 — Mandatory integrity:** Use the acceptance report, not raw row counts,
to close missing/stale/conflicting MARKET_PRICE, TRADING_VOLUME, IDENTITY
cases. Repair only objectively documented technical faults using existing
bounded providers. No automatic successful evaluation for missing evidence.

**P1 — Verified holdings and NAV:** Use issuer/exchange documents and existing
`EtfHoldingsSnapshot` and `EtfNavObservation` contracts. Require exact
instrument identity, bounded source-approved retrieval, document date and
position-level weight validation. If no approved accessible issuer document
exists, mark `MISSING_OFFICIAL_SOURCE` in the work queue; never insert empty
holdings just to clear a gate.

**P1 — TER, AUM and tracking metrics:** Existing `EtfFact` and
`EtfOfficialDocumentAcquisitionPipeline` accept safely parsed official
evidence only after scheme-identity validation. SBI's 22 NSDL scheme rows
remain staged; five name-joined NSE candidates do not substitute for
direct code→ISIN documentary attestation. Do NOT create approval records
from a canonical ISIN/symbol match. Add actual official source URLs and
metric parsers only after reviewing authentic issuer documents. Expose
unsupported requirements as missing/not-implemented, not zero.

**P2 — Final radar:** Run bounded, canonical-verified dry-read first; check
all mandatory checks `READY_FRESH`, factor/risk gates, deterministic bars,
recommendation abstention and cycle publication. Do not run the 359-ETF
acquisition cycle simply to produce an acceptance report.

## Definition of done

- [ ] Read-only database diagnostic successfully runs against live 359 ETF universe
- [ ] Application-level readiness evaluated (no inferred 351/359 guarantee)
- [ ] Every missing/stale/technical mandatory case has a documented closure or terminal reason
- [ ] All official fund evidence sources explicitly approved and instrument-scoped
- [ ] Holdings/TER/AUM/tracking facts ingested only where real sources and identity proof exist
- [ ] All unknown/unavailable source requirements remain explicit in readiness output
- [ ] Reviewed SBI code→ISIN evidence established independently BEFORE any TER persistence
- [ ] Bounded end-to-end radar and portfolio tests pass
- [ ] No unreviewed numeric facts, unbounded provider loops or scheduler enablement

**This PR delivers consolidated audit/acceptance machinery; it cannot
supply missing fund documents, fabricate source review, or declare all 359
ETFs research-complete.** The resulting report is the agreed work queue
for the remaining source-onboarding and bounded acquisition implementation.
