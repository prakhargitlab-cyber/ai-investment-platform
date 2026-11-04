# SBI real-data canonical preflight — offline, read-only

## Purpose

PR #18's `preflight_sbi_candidates` consumes independently existing
canonical instrument master rows **and** existing NSE listing evidence.
Do not fabricate these inputs from the circular. PR #19 supplies narrowly
scoped, no-network adapters to run that preflight against actual local data.

**No hardcoded SBI scheme-code→ISIN maps. No database or financial facts
written. No production permissions or authentication bypass.**

## 1. Export source-derived reconciliation

Use the already-validated original circular at
`.tmp/etf-validation/CML77115.pdf`. From `ai/research-engine`:

```powershell
& "..\..\.venv\Scripts\python.exe" -m app.sbi_ter_nse_circular_review_cli `
  --nse-pdf ".tmp\etf-validation\CML77115.pdf" `
  > ".tmp\etf-validation\sbi-review.json"
if ($LASTEXITCODE -ne 0) { throw "Source-derived SBI report failed" }\n# Out-File -Encoding utf8 retains portable JSON encoding on Windows PowerShell.
```

This is **one** intentional official SBI TER workbook download and in-memory
staging. Do not run a full opportunity cycle. Output is candidate-only.

## 2. Capture actual, complete canonical ETF response

Portfolio-service controller:
`GET /api/v1/instruments?assetType=ETF&status=ACTIVE&page=0&size=500`
(requires the existing authenticated-user context). It returns
`{ "instruments": [...], "page": 0, "size": 500, "totalElements": N }`,
with `globalInstrumentId`, `ticker`, `exchange`, `isin`,
`assetType`, `status`. Export the JSON **from your own authenticated,
running service** to `.tmp/etf-validation/canonical-etfs.json`. Never
commit a token or captured response; avoid writing credentials into shell
history. This CLI deliberately does **not** implement an HTTP authentication
client.

The adapter rejects incomplete/truncated pages: it requires
`len(instruments) == totalElements` and `page == 0`. If the service's
actual ETF count exceeds 500, the service page limit requires a separately
reviewed, completeness-preserving pagination path; do **not** silently call
the first page the full universe. Empty complete exports are permitted, but
will result in missing canonical identities.

## 3. Identify a genuine existing research SQLite file

The research ETF evidence store must **already contain**
`etf_listing_observations` and `etf_acquisition_attempts` tables.
The CLI accepts an **existing** local SQLite path and uses SQLite's
`mode=ro`. It never initializes a new DB, inserts fake listings or updates
records. If your deployed research persistence is PostgreSQL rather than
SQLite, this milestone does **not** connect to production or bypass that
boundary. A separately approved, read-only snapshot/export workflow is
needed; do not point the CLI at a production database.

## 4. Run the integrated preflight

From `ai/research-engine`:

```powershell
& "..\..\.venv\Scripts\python.exe" -m app.sbi_ter_real_data_audit_cli `
  --reconciliation-json ".tmp\etf-validation\sbi-review.json" `
  --canonical-json ".tmp\etf-validation\canonical-etfs.json" `
  --research-sqlite ".tmp\research.db"
```

Replace `.tmp\research.db` **only** with the actual existing local
research SQLite file, never an invented path. This command has **no network
access**, writes no facts and does not perform an official NSE fetch.

The report includes one status per candidate:
`VERIFIED_CANONICAL_CANDIDATE`, `MISSING_CANONICAL_IDENTITY`,
`MISSING_NSE_LISTING`, `NSE_LISTING_NOT_FRESH`, `CONFLICT`.
A verified canonical candidate remains **unapproved** NSDL-code→ISIN evidence.
All output states `approved_for_persistence=false` and
`identity_verified_for_scoring=false`.

**Limitations:** A saved canonical API response cannot prove its own
authenticity or retrieval time. The saved NSE evidence rows must themselves
be independently source-audited; freshness and provenance fields are
validated by the existing gate but do not prove that an external source
was genuinely downloaded. SQLite listing revisions are examined without
coalescing: ambiguous observations fail closed.

## Tests

From repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  ai/research-engine/tests/test_sbi_ter_real_data_audit.py `
  ai/research-engine/tests/test_sbi_ter_canonical_preflight.py `
  ai/research-engine/tests/test_sbi_ter_nse_circular_review.py -q
```

The tests use **synthetic** records; real source verification requires
operator-provided real inputs from steps 1–3. Keep local `.tmp` inputs and
`docs/etf-official-evidence-investigation.md` untracked and untouched.
