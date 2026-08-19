# Research Intelligence Engine

Phase 3 builds the research evidence foundation. It does not produce final BUY/SELL recommendations.

## Pipeline

Public source -> document discovery -> fetch -> extraction -> normalization -> deduplication -> entity resolution -> structured event extraction -> validation -> research fact store -> deterministic catalyst scoring -> future AI explanation.

LLMs are optional and may only produce schema-constrained extraction candidates. Invalid enum values, dates, numbers, or unsupported claims are rejected before persistence.

## Source Providers

The source abstraction records source type, supported countries and markets, fetch strategy, reliability level, rate-limit policy, JavaScript requirement, and automatic access status.

Default providers cover company websites, investor relations, exchange announcements, regulatory filings, government procurement, permitted RSS, permitted news, and a disabled search-discovery placeholder. Google search result scraping is not implemented.

## Source Safety

The fetcher must not bypass CAPTCHA, paywalls, authentication, robots restrictions, anti-bot controls, or source terms. Sources that are not permitted for automation must be marked `UNAVAILABLE`, `RESTRICTED`, or `MANUAL_ONLY`.

Default live fetching is disabled with `AIP_RESEARCH_LIVE_ENABLED=false`. Demo fixtures are labelled `DEMO`.

## Fetching

Normal HTTP fetching is the first strategy. The implementation supports configurable user agent, connect/request timeouts, bounded retries, exponential backoff, `Retry-After`, redirect limits, redirect target validation, content-type validation, maximum content size, canonical URL normalization, and SSRF protection.

Playwright fallback is represented but disabled by default and not bundled in the default image. It may only be enabled for sources that permit JavaScript automation.

## Documents

`ResearchDocument` supports canonical URL, original URL, title, source type/name, publisher, publication/retrieval dates, language, content type, document type, content hash, instrument/company IDs, status, and reliability. Raw and normalized bodies are excluded from API responses.

PDFs are supported as metadata/reference documents in Phase 3. OCR is not implemented.

## Entity Resolution

Resolution does not use ticker alone. It combines ISIN, company name, aliases, ticker plus exchange, country, and known domains, and returns a confidence score.

## Events And Scoring

`ResearchEvent` captures event type, date, source document, source URL, reliability, confidence, impact, time horizon, monetary values, percentages, customer/counterparty, location, capacity, and a short evidence reference.

The deterministic `CatalystScore` is 0-100 and uses event weights, source reliability, confidence, impact, and temporal decay. It is not a recommendation engine.

## Persistence And Events

The Phase 3 schema artifact is in `ai/research-engine/db/migration/V1__research_intelligence.sql`.

Versioned event contracts exist for `research.document.discovered`, `research.document.fetched`, `research.document.processed`, `research.event.extracted`, `research.event.rejected`, and `research.company.updated`. Events contain IDs and metadata, not raw page bodies or secrets.

## Security

The fetcher rejects localhost, loopback, private ranges, link-local addresses, cloud metadata endpoints, non-http(s) schemes, and unsafe redirect targets. No paid API keys or credentials are required.
