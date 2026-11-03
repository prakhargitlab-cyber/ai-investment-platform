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
