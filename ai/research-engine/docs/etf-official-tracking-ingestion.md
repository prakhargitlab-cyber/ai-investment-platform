# Official ETF tracking evidence — verified document ingestion

## Capability added

The existing approved-source document ingestion now supports `TRACKING_ERROR`
and `TRACKING_DIFFERENCE` in addition to AUM and TER.

- The bound PDF/approved-document pipeline opts into all four fields using
  `include_tracking=True`.
- Direct legacy calls retain the previous two-field behavior unless the
  caller explicitly opts into tracking.
- Tracking Error is a nonnegative percentage (0 to 100).
- Tracking Difference is a *signed* percentage (-100 to 100).
- Both tracking metrics **must have a separate explicit ISO reporting date**.
  Neither publication time, retrieval time nor the "AUM and TER as of" shared
  date can silently date a tracking metric.
- All existing gates remain: official approved exact URL, source access
  rules, canonical ISIN plus scheme heading, hash-bound text/section,
  document authenticity/provenance, future-date rejection, conflicting
  official same-period evidence rejection, and atomic persistence contract.
- If a document does not contain either tracking metric, the attempt
  remains `SUCCESS_EMPTY`. It never invents a value.

### Accepted sample format (illustrative *synthetic* text, not issuer data)

```text
Scheme: Example ETF
ISIN: IN0000000042
AUM: INR 10 crore (as of 2026-09-30)
TER: 0.35% (as of 2026-09-30)
Tracking Error: 0.21% (as of 2026-09-30)
Tracking Difference: -0.06% (as of 2026-09-30)
```

Dates/units are explicit. Different actual issuer formats require separate
source-authenticated parsing evidence, rather than permissive guesses.

## Bounded regression gate

```powershell
Set-Location C:\workspace\ai-investment-platform
$tests = @(
  'ai/research-engine/tests/test_etf_document_trust.py',
  'ai/research-engine/tests/test_etf_official_document_fetch.py',
  'ai/research-engine/tests/test_etf_document_canonical_binding.py',
  'ai/research-engine/tests/test_etf_readiness.py',
  'ai/research-engine/tests/test_etf_completion_acceptance.py',
  'ai/research-engine/tests/test_etf_completion_work_plan.py'
)
.\.venv\Scripts\python.exe -m pytest $tests -q
if ($LASTEXITCODE -ne 0) { throw 'ETF regression failed' }
```

## Real-data boundary

The live 2026-11-04 database has no `UNDERLYING_HOLDINGS`,
`EXPENSE_RATIO`, `AUM`, `TRACKING_ERROR`, or
`TRACKING_DIFFERENCE` facts in the active 359-ETF universe.
Merely adding parsing capability does **not** supply any of these fields
until a matching, independently approved and genuinely accessible official
source containing the labelled metrics is provided and processed.

Two pre-reviewed Nippon Product Notes are
`REQUIRES_AUTHENTICATION` and lack proven scheme-labelled fields.
They remain blocked; do not bypass OTP/authentication or downgrade source
identity requirements. SBI scheme-code-to-ISIN proposals remain unapproved.

Mandatory readiness remains 351/359 at the last measured clock; no
market-price retry, identity override, full-universe job or production
evidence write is performed by this change.
