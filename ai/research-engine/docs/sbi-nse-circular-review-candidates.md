# SBI TER / NSE circular — source-derived review candidates (not a crosswalk)

## What the supplied NSE circular proves

The two-page NSE listing circular `NSE/CML/77115`, dated **2026-10-26**,
is effective **2026-10-30**. Page 1, row 8 explicitly associates the
**SBI Nifty 50 ETF** name, ISIN `INF200KA1FS1`, previous symbol
`SETFNIF50`, and new symbol `SBINIFTY50`.
User-uploaded PDF bytes independently inspected in this work:
SHA-256 `0cf0218de4a2d096a9a772126e8721fe8e74a43332159ecff2f46e9380e447d0`.
The URL is:
`https://nsearchives.nseindia.com/content/circulars/CML77115.pdf`.

The live official SBI TER workbook, downloaded 2026-11-04, independently
lists code `SBIM/O/O/EET/15/06/0046` with published name
`SBI Nifty 50 ETF`. Its SHA-256 was
`c241a9d2ecfd9ad68cd432064e95b750b1a6cdc03c31da94d94b74a05ac339c9`.

**These documents do NOT jointly print the same NSDL scheme code and ISIN.**
Their name equality is a strong *review candidate*, not direct code-to-ISIN
proof and not an approved master-data binding.

## Implementation

`app.sbi_ter_nse_circular_review` reads original PDF bytes, verifies
circular markers, checks the effective date, extracts complete single-line
table rows, and correlates with caller-supplied staged SBI TER observations
using exact case/whitespace-normalized new scheme names. No guessed rows,
fuzzy joins, hardcoded scheme-to-ISIN mapping, or code-prefix mapping.

Outputs include both source SHA-256 values, NSE row/page, exact scheme code,
ISIN and new/old ticker **as review candidates only**. All candidates carry
`approved_for_persistence=False`; no resolver, DB write or scoring path.

The circular URL is **claimed provenance** provided by the operator. The
offline tool does not authenticate its HTTP retrieval. Genuine bytes, an
issuer-domain source, valid source timestamps and current canonical NSE
identity still require an independent evidence review. A display-name match
can remain ambiguous across share classes and scheme changes.

## Controlled operator command

Place the **unaltered** user-provided `CML77115.pdf` at a local path,
for example `C:\workspace\ai-investment-platform\ai\research-engine\.tmp\etf-validation\CML77115.pdf`.
Do not commit the original PDF or change the financial facts.

From `ai/research-engine`:

```powershell
& "..\..\.venv\Scripts\python.exe" -m app.sbi_ter_nse_circular_review_cli `
  --nse-pdf ".tmp\etf-validation\CML77115.pdf"
```

This command makes a **single fresh, bounded** official SBI workbook
download, uses an in-memory database, and prints source-derived review
candidates without financial TER figures. No full 359-ETF cycle occurs.

To run offline unit tests from project root:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  ai/research-engine/tests/test_sbi_ter_nse_circular_review.py `
  ai/research-engine/tests/test_sbi_ter_runtime_download.py `
  ai/research-engine/tests/test_sbi_ter_staging.py -q
```

If the PDF is placed at the optional fixture path, the genuine-document
test executes; otherwise that **one optional test** is skipped.

## Publication gate remains closed

A reviewer must establish direct scheme-code→ISIN linkage (or a separately
approved cross-source identity policy), validate documentary authenticity,
check current NSE canonical ISIN + effective ticker, and bind authorization
to source bytes and scope before any real TER data can enter scoring.
Do not transform the reported candidates into
`ReviewedSbiSchemeIdentity` objects automatically.
