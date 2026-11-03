# ETF official source onboarding — first live PDF

## Scope and current finding (2026-11-03)

The checked-in source registry contains two reviewed Nippon India Mutual Fund Product Notes:

| ETF | ISIN | Registry access | Why not usable for unattended facts |
| --- | --- | --- | --- |
| NIFTYBEES | INF204KB14I2 | REQUIRES_AUTHENTICATION | ProductNotes endpoint requires OTP; reviewed document has no labelled ISIN/AUM/TER accepted by the existing strict parsers |
| JUNIORBEES | INF732E01045 | REQUIRES_AUTHENTICATION | Same restrictions |

These conclusions come from the checked-in source review documentation, not a fresh online verification. **There is currently no source in the default registry approved as PUBLIC_UNAUTHENTICATED.** Enabling `research_etf_official_document_fetch_enabled` alone must not be interpreted as successful acquisition.

## Operator review for one replacement factsheet

1. Select **one** exact PDF on the AMC's own HTTPS host for NIFTYBEES (ISIN `INF204KB14I2`) or JUNIORBEES (ISIN `INF732E01045`). Do not infer URLs from filenames, issuer homepages, search snippets or a second-hand URL.
2. Open the exact PDF in a clean browser session without AMC login, cookies, OTP or CAPTCHA. Record final URL, final host, status, content type, any redirects, date reviewed, and the AMC ownership evidence. If access is not clearly public, classify as `REQUIRES_AUTHENTICATION`.
3. Download only with the site's normal published access. Verify it is a parseable PDF and records an exact scheme identity (prefer a clearly labelled ISIN) plus labelled **AUM** and **expense ratio/TER** values with as-of dates and units. Do not substitute neighbouring-scheme figures from a combined factsheet.
4. Feed the downloaded document into the existing `registered_official_document` + `ingest_etf_facts_from_document` identity/metric path **offline** first; inspect rejection codes and produced provenance. Never edit the gates merely to make a document pass.
5. If the identity/metric proof passes, record exact `document_url`, `document_host`, `issuer_name`, `document_type`, verified `isin` and `ticker`, and a human review reference in `app/etf_official_document_sources.py`. Assign `PUBLIC_UNAUTHENTICATED` **only after** actual unauthenticated access is demonstrated and approved. Keep the rollout bounded to at most two approved instruments.
6. With explicit operator authorization, enable the existing feature flag in a controlled local environment. Verify only the allowlisted URL is contacted and final host is checked; verify persisted evidence is fresh, correctly scoped, and appears in final rule-engine COST and fund-quality metrics. Missing tracking, bid/ask spread, entry assessment, etc. must still remain unavailable, never fabricated.
7. Preserve the test contract and keep the PR in draft until a real URL is reviewed and a controlled live run produces source-backed facts. Do not deploy the flag to production as part of this source review.

## Offline test commands

From `C:\workspace\ai-investment-platform-etf-fix\ai\research-engine`:

```powershell
& C:\workspace\ai-investment-platform\.venv\Scripts\python.exe -m pytest tests/test_etf_default_source_live_gate.py tests/test_etf_document_canonical_binding.py tests/test_etf_official_document_fetch.py tests/test_etf_full_universe_document_acquisition.py -q
```

The default-source guard test is intentionally expected to require updating **after** an actually reviewed public source is added. Do not weaken it to a mere instrument-count check.

## Review record (fill only from firsthand evidence)

- Instrument / ISIN:
- AMC and proof of issuer-owned host:
- Exact PDF URL and verified redirect final URL:
- Reviewed UTC date/time, clean-browser accessibility:
- Response status, MIME type, bytes and PDF hash:
- Scheme identity label and location in PDF:
- AUM label, value, unit, as-of and page:
- TER/expense ratio label, value, unit, as-of and page:
- Existing strict parser outcome and persisted evidence:
- Human reviewer and unique approval reference:
- Remaining unavailable mandatory/supporting evidence:

No public replacement URL has been independently checked yet. Until that happens, this is a **review-ready procedure**, not completed live acquisition.


## Uploaded October 2026 combined factsheet review (2026-11-03)

User-supplied source URL (not independently HTTP-verified in this review):
`https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/Nippon-FS-October-2026.pdf`

Local review of the user-provided PDF: **371 physical PDF pages**, **13,264,965 bytes**, raw SHA-256 `9bdb8948221d19c62392dae1bebb1cda5ff2e2a4e09713f8cc9e7967973ccf0c`. PDF physical page 207 (printed page 207) has **Nippon India ETF Nifty 50 BeES**, reporting **2026-09-30**:

- AUM (not AAUM): INR **65,178.85 crore**; separately AAUM INR **66,163.18 crore**.
- Monthly-average expense ratio, Direct: **0.04%**. Annexure 1 (physical PDF page 368) separately lists Direct Plan Total TER **0.04%**, date 2026-09-30.
- Tracking error 1Y **0.02%**, tracking difference 1Y **-0.02%**, benchmark **Nifty 50 TRI**, NAV **258.0021**.
- Junior BeES also has a separately labelled section (physical PDF page 216); the PDF is **multi-scheme**, so whole-document parsing cannot safely select one fund by URL.

**FAIL CLOSED / NOT YET APPROVABLE FOR AUTOMATED METRIC INGESTION:**
1. Across the searchable PDF text, neither canonical `INF204KB14I2` nor Junior BeES `INF732E01045` is present. This is not proof that the printed artifact contains no image-only ISIN, but no matching text was discovered.
2. `app.etf_document_parser.verified_scheme_section` demands exactly one `ISIN: <canonical>` within a hash-bound scheme section. `app.etf_official_document_pipeline` does not currently pass an explicitly reviewed `section_span` to registration for this combined document.
3. `parse_metric` demands a labelled one-line metric with an accepted unit and ISO as-of format, whereas the actual factsheet uses multi-line table layout and `September 30, 2026`. Adding artificial ISIN lines, relabelling the text, or assuming the AMC URL proves scheme identity would bypass the evidence contract.
4. Uploaded bytes prove the PDF can be inspected offline. They do **not** prove unauthenticated live HTTP retrieval, redirect chain, or final host. Do not mark `PUBLIC_UNAUTHENTICATED` solely because the file was supplied.

Next safe step: obtain an AMC/exchange document with a **labelled exact ISIN** for the scheme and establish an independently verified binding between that identity source and each clearly bounded scheme section before engineering a dedicated multi-scheme table extractor. Keep data `DOCUMENT_REVIEW_ONLY`, do not persist it as a trusted ETF fact or change the default registry yet.


## Implemented in PR #5: safe monthly resolver and review-only layout parser

- `app.etf_nippon_monthly_source.month_candidate(year, month)` produces the **exact expected filename** from the fixed Nippon issuer path; `bounded_candidates` returns at most two recent URL candidates. It does **not** make an HTTP request or grant source approval. A URL is only a candidate until verified against the issuer and registered through the existing allowlist controls.
- `app.etf_nippon_factsheet_review.extract_review_only(pdf_bytes, ticker)` scans the bounded 25 MB / 500-page PDF for an exact fund title and `Fund Details` section, rejects duplicates, and separately extracts fund AUM, **AAUM**, monthly average expense ratio, and 1-year tracking error/difference for NIFTYBEES and JUNIORBEES.
- The return type always reports `status=DOCUMENT_REVIEW_ONLY` and `identity_verified=False`. It cannot create `EtfFact`, authenticate an issuer, set `PUBLIC_UNAUTHENTICATED`, or influence scoring. It is a staging step, **not finished ingestion**.
- `tests/test_etf_nippon_monthly_source.py` checks month boundaries, URL shapes, ambiguity, and review-only semantics. Genuine-document tests are optional and use the gitignored `.tmp/etf-validation/Nippon-FS-October-2026.pdf` path.
- Still pending: independent official ISIN-to-section corroboration; real clean-session HTTP verification; a separately reviewed policy allowing controlled monthly family URLs; a one-download-per-publication cache and durable provenance; a fund-metric ingestion adapter built without weakening `verified_scheme_section`; and a guarded live test. Do **not** enable the existing automated-fetch flag based only on these resolver/parser files.


## Phase 2 staging coordinator (2026-11-03)

`app.etf_nippon_shared_review.review_approved_shared_month` is a strictly bounded **staging-only** coordinator. Its injected async PDF provider is called **once** after exact URL / issuer-host / public-access approvals have been established for each ETF, and then the reviewed PDF sections are parsed. It independently rejects unexpected report dates and mismatched document hashes. The coordinator does not perform HTTP, find sources, or write ETF evidence.

`tests/test_etf_nippon_shared_review.py` covers one-call/two-section semantics, missing/auth-gated/wrong-month approval, duplicate identities, date mismatch, and digest mismatch without a network call.

**Critical remaining blocks:** This staging coordinator has no production network adapter: the approved source registry still only includes authenticated Product Notes, and `PUBLIC_UNAUTHENTICATED` approval for the combined factsheet remains unverified. A real exchange-issued scheme-to-ISIN identity binding is still needed; matching a section heading is not sufficient. No `EtfFact` may be persisted by this code, and no buy recommendation becomes investment-ready merely because review-only metrics parse. Keep document fetch disabled and PR draft.


## Phase 3: identity preflight added (2026-11-03)

`app.etf_nippon_identity_preflight` adds a strict, **non-persisting** preflight for each extracted factsheet section and for both sections as one unit. A caller must supply an explicit, reviewed scheme-heading -> exact ISIN/ticker/instrument UUID attestation and a separate, recent, persisted NSE ETF listing; the code reuses the existing `_check_identity_ready` freshness/conflict gate, verifies listing provenance/source host, and rejects missing or cross-instrument ISIN collisions. It returns `READY_FOR_MANUAL_REVIEW` with `identity_verified_for_scoring=False`, **not investment evidence**.

Important: the attestation object is an operator assertion. A nonempty `review_reference` and an NSE-domain URL are structural checks, **not proof that a reviewer truly verified an exchange source**. This work has not established new external exchange evidence and has not tied the October PDF to an ISIN through an independently checked published document. The independent review process and its evidence manifest still need to be completed before any future ingestion adapter is considered.

The code has **not been run against the user's Windows worktree yet**. Run `python -m pytest tests/test_etf_nippon_identity_preflight.py tests/test_etf_nippon_shared_review.py tests/test_etf_nippon_monthly_source.py -q` after pulling, and keep PR draft and fetch flag disabled.

Pending before automating anything beyond manual review: actual public PDF HTTP verification and approved month-family policy; exact source document provenance/caching; independently reviewed NSE identity documents and corresponding attestations; ingest integration preserving immutable source and page provenance; backend and frontend regression tests, and no admission to investment-ready scoring without all other mandatory sources.
