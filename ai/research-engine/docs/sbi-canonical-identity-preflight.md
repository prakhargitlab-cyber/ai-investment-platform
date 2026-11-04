# SBI canonical ETF identity preflight (PR #18)

This read-only preflight consumes the **unapproved, source-derived** review
candidates produced by PR #17. It compares each candidate's ISIN and **new**
NSE ticker with the existing canonical ETF master-data schema and the persisted
official NSE `EtfListing` observations.

## Inputs and source boundaries

- PR #17 `SbiNseReconciliationReport`, whose rows preserve exact source PDF
  and TER workbook SHA-256 fingerprints, plus effective circular date
- Canonical ETF rows already fetched from the portfolio-service's existing
  canonical API, using fields `globalInstrumentId`, `assetType`, `status`,
  `primaryExchange`, `isin`, `primarySymbol`; there is **no new network client**
- Existing read-only research persistence with `etf_listings()` and
  `etf_attempts(instrument_id)` used by `_check_identity_ready`
- Explicit timezone-aware `now` for deterministic evaluation

The verifier rejects candidate reports marked approved or with inconsistent
source fingerprints/effective dates. For each candidate it requires a unique,
active NSE ETF canonical pair, non-zero UUID, exactly one matching official NSE
listing, approved exchange provenance, and `READY_FRESH` from the existing
identity readiness policy (which also checks unresolved metadata conflicts).

## Outcomes

- `VERIFIED_CANONICAL_CANDIDATE`: exact canonical and fresh official NSE
  listing agree. **Not** direct NSDL code→ISIN documentary proof
- `MISSING_CANONICAL_IDENTITY`: no matching ISIN or symbol found in canonical rows
- `MISSING_NSE_LISTING`: canonical pair exists, no official listing observation
- `NSE_LISTING_NOT_FRESH`: no fresh trusted NSE listing / unresolved identity
  metadata conflict
- `CONFLICT`: inactive or ambiguous canonical identity, incorrect ISIN/symbol,
  duplicate UUID or listing, or untrusted source provenance

The only successful flag is the **preflight candidate status**; every result
is `approved_for_persistence=False`. This is not the identity approval step,
not a `ReviewedSbiSchemeIdentity`, and cannot authorize TER ingestion, scoring
or other production financial facts.

## Test

From repo root (Windows PowerShell):

```powershell
.\.venv\Scripts\python.exe -m pytest `
  ai/research-engine/tests/test_sbi_ter_canonical_preflight.py `
  ai/research-engine/tests/test_sbi_ter_nse_circular_review.py `
  ai/research-engine/tests/test_sbi_ter_identity.py `
  ai/research-engine/tests/test_etf_nippon_identity_preflight.py -q
```

There is deliberately no auto-run CLI for the canonical source: a caller must
pass actual, current canonical rows and existing **durably verified** NSE
listings. Reconstructing these from the NSE circular or hardcoding sample
master data would create circular evidence. Initial expected output is
`MISSING_CANONICAL_IDENTITY` or `MISSING_NSE_LISTING` until genuinely
independent records exist.
