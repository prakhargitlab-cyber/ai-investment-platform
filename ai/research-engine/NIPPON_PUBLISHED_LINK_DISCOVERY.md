# Nippon ETF published-link discovery (review-only)

This milestone removes hardcoded publication months from **discovery**. It does
not change the old monthly URL-candidate helper, register any new official
sources, enable the existing production fetch flag, or authenticate a document.

## Required source approval before a live listing fetch

An operator must first find a **real, publicly accessible, stable Nippon India
issuer page** that publishes links to current ETF documents. The page must be
verified by normal browser review and recorded with its exact HTTPS URL,
response status and final URL. The PDF link supplied in the August 2026 example
is **not** itself a listing page. The homepage has **not** been verified as a
PDF-listing endpoint. No listing URL is configured by default.

With a subsequently reviewed issuer listing URL, call the module using:

```python
from app.etf_nippon_listing_discovery import discover_listing

result = await discover_listing(
    approved_listing_url=reviewed_exact_listing_url,
    enabled=True,
)
# result.candidates are UNVERIFIED_DISCOVERY_CANDIDATE only
```

The module refuses network access unless `enabled=True`, refuses non-issuer
or modified listing URLs, disallows redirects, bounds page size/link counts,
and accepts only PDF links actually present in the page's HTML under the two
specific issuer paths (factsheets and scheme differentiation).

If the issuer uses a JavaScript API, redirects to a different host, or does
not publish links in ordinary HTML anchors, the adapter will safely return no
candidates or reject the response. It must not silently invent date-based
filenames. A separately reviewed supported discovery surface is needed.

## Important unimplemented boundaries

- **Newest-document selection** needs PDF retrieval and validation of actual
  internal reporting dates (and publication provenance). The discovery module
  intentionally does **not** infer dates from PDF filenames or HTML order.
- **Authenticity / issuer source review** and exact scheme-to-ISIN binding
  remain unsatisfied. A public scheme heading or an NSE ticker/ISIN row alone
  does not grant issuer-document identity approval.
- The existing approved PDF registry remains exact-match and default-off.
  No discovered URL becomes registered, persisted as a trusted ETF fact,
  or eligible for ETF Radar scoring.
- A periodic scheduler and durable discovery checkpoint are not part of this
  task. Do not claim unattended live updates from this offline-tested code.

## Local test command

From `C:\workspace\ai-investment-platform-etf-identity\ai\research-engine`:

```powershell
& C:\workspace\ai-investment-platform\.venv\Scripts\python.exe -m pytest tests/test_etf_nippon_listing_discovery.py -q
```
