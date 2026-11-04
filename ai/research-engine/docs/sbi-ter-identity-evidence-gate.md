# SBI TER scheme-code → ISIN evidence gate

## Verified input available (2026-11-04)

The live SBI TER workbook at
`https://www.sbimf.com/docs/default-source/ter_allschemes/current-year-ter.xlsx`
was fetched successfully on the user's Windows machine, original SHA-256
`c241a9d2ecfd9ad68cd432064e95b750b1a6cdc03c31da94d94b74a05ac339c9`,
1,849,064 bytes, 22 ETF schemes staged. The exact source workbook does
**not** contain an ISIN or an official NSDL-code→ISIN crosswalk.

The independent NSE canonical ETF registry gives symbol→ISIN, but does
**not** establish SBI's NSDL scheme code→ISIN. Name similarity does not fill
this gap. **Zero of the 22 SBI schemes is independently identity-verified.**

## What the new inspection supports

`app.sbi_ter_identity_document.inspect_sbi_identity_pdf(original_pdf_bytes, source_url=...)`
parses a bounded original PDF (8 MiB, 100 pages) supplied by an operator,
records its SHA-256, and reports **review-only** candidates if one page
explicitly labels exactly one `NSDL Scheme Code: ...` and exactly one
`ISIN: ...` (or `ISIN Number: ...`). No cross-page joins, fuzzy name
matching, ambiguous multi-code pages, or guessed URLs.

The input `source_url` is an operator claim, **not** proof that the PDF
was downloaded from that URL, and a same-page pair is **not** by itself
proof the two identifiers belong to one ETF scheme. Human inspection of
original official source, issuer ownership, document page/section, download
response/redirect chain, document hash, NSE listing, and approval is needed.

Inspect a previously acquired SBI original PDF offline:

```powershell
Set-Location "C:\workspace\ai-investment-platform\ai\research-engine"
& "..\..\.venv\Scripts\python.exe" -m app.sbi_ter_identity_document_cli `
    --pdf "C:\path\to\independently-reviewed-sbi-disclosure.pdf" `
    --source-url "https://www.sbimf.com/path/to/independently-reviewed-sbi-disclosure.pdf"
```

The example URL above is a **placeholder, not a discovered or approved link**.

This CLI does **not** call the network or persist ETF facts. It prints
`DOCUMENT_REVIEW_ONLY`, `identity_authenticated=false` and
`eligible_for_fact_persistence=false` for every result. An empty set is
not an error: it means the strict labelled pair was not found.

## Release gates for real TER observations

1. Obtain the **exact** actual public SBI-owned official PDF source that
   explicitly identifies a scheme code and ISIN; check uncredentialed
   final URL, HTTP status, MIME type, original hash, and relevant page.
   Never bypass OTP/login/CAPTCHA.
2. Review the exact document label(s), code+ISIN scheme scope and conflicting
   identities, and corroborate the ISIN/ticker with fresh official NSE
   listings and the active canonical ETF UUID.
3. Verify and store an evidence-bound, authorized approval record. A
   caller-supplied URL/hash/reviewer string is insufficient. The current
   `ReviewedSbiSchemeIdentity` metadata does **not** authenticate source bytes.
4. Integrate a trusted approval provider (not the inspection CLI) with the
   bounded persistence adapter, prove genuine positive + negative cases,
   provenance and scoring, and get an explicit release approval.
5. Until then, keep all 22 current codes `IDENTITY_UNRESOLVED_STAGED_ONLY`.
   Do not use this CLI's candidates as a resolver or publish expenses.

No official identity PDF URL has been independently authenticated in the
available material as of this implementation. This milestone implements
the strict, inspectable evidence-discovery primitive, **not** source-backed
ETF expense ratio publication.


## Produce a real, bounded NSDL-code inventory for issuer review

From the project root (this is **one intentional live SBI workbook download**,
not the complete 359-ETF cycle):

```powershell
Set-Location "C:\workspace\ai-investment-platform\ai\research-engine"
& "..\..\.venv\Scripts\python.exe" -m app.sbi_ter_live_smoke --include-staged-schemes
```

Output now lists the actual scheme code and published scheme name from the
fresh, hash-recorded official workbook. Each row deliberately reports
`isin: null`, `identity_verified: false` and
`canonical_instrument_id: null`. Names are only search aids for an
independent, authoritative crosswalk; they are **never join keys**.
Do not copy these names into a manufactured identity mapping.

The default `python -m app.sbi_ter_live_smoke` output remains compact.
Neither command alters a production DB, requires a reviewer identity record,
or starts a scheduled/full-universe run.
