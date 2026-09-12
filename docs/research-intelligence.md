# Research Intelligence Engine

Phase 3 builds the research evidence foundation. It does not produce final BUY/SELL recommendations.

## Pipeline

Public source -> document discovery -> fetch -> extraction -> normalization -> deduplication -> entity resolution -> structured event extraction -> validation -> research fact store -> deterministic catalyst scoring -> future AI explanation.

LLMs are optional and may only produce schema-constrained extraction candidates. Invalid enum values, dates, numbers, or unsupported claims are rejected before persistence.

## Source Providers

The source abstraction records source type, supported countries and markets, fetch strategy, reliability level, rate-limit policy, JavaScript requirement, and automatic access status.

Default providers cover company websites, investor relations, exchange announcements, regulatory filings, government procurement, permitted RSS, permitted news, and a provider-neutral search discovery boundary. Google search result scraping is not implemented.

Search discovery is discovery only:

```text
search result/snippet -> candidate URL -> URL/security validation -> fetch original publisher page -> extract document -> validate evidence -> store event
```

Search snippets are never persisted as research evidence. Implementations must use a supported search API, such as a Google-compatible custom search endpoint, Brave-compatible API, or another approved provider. Provider credentials come only from runtime environment or secret configuration.

## Source Safety

The fetcher must not bypass CAPTCHA, paywalls, authentication, robots restrictions, anti-bot controls, or source terms. Sources that are not permitted for automation must be marked `UNAVAILABLE`, `RESTRICTED`, or `MANUAL_ONLY`.

Default live fetching is disabled with `AIP_RESEARCH_LIVE_ENABLED=false`. Demo fixtures are labelled `DEMO`.

## Phase 3 Controlled Live Source

The first controlled live ingestion path is limited to AIXTRON SE:

- Instrument ID: `11111111-1111-1111-1111-111111111111`
- Stable identity: `AIXA` / `XETR` / `DE000A0WMPJ6`
- Registered source: AIXTRON official press release page on `aixtron.com`
- Source reliability: `LEVEL_B`
- Source mode: `REAL`

The frontend does not submit arbitrary URLs. Refresh uses registered source definitions first, then optional bounded search discovery only for categories that remain `NO_EVIDENCE`.

## API Contract

Provider-free readiness and analysis, with an explicit targeted acquisition step:

```http
GET  /api/v1/research/readiness/{globalInstrumentId}
POST /api/v1/research/readiness/{globalInstrumentId}/ensure
POST /api/v1/research/analysis/{globalInstrumentId}
X-Correlation-Id: <caller-generated-id>
```

The readiness GET and analysis POST never call providers. Only `ensure` may
execute planner-selected stale, partial, or missing requirements under the
per-instrument single-flight boundary.

Summary:

```http
GET /api/v1/research/companies/{instrumentId}/summary
```

The response contains:

- `profile`: company and instrument identity
- `catalystScore`: deterministic score, bucket scores, confidence, generated timestamp
- `recentEvents`: classified evidence with `sourceMode`, reliability, confidence, impact, dates, values, evidence excerpt, source URL, source classification, and supporting source URLs
- `documents`: normalized source documents with `sourceMode`, `freshness`, content hash, publication/retrieval/discovery dates, reliability, source classification, independence key, and canonical URL
- `dataFreshness`: `REAL`, `DEMO`, `DEMO_FALLBACK`, `SOURCE_UNAVAILABLE`, or `UNAVAILABLE`
- `demo`: true only when the returned summary is demo/fallback data
- `sourceMix`: document counts by source type

## Fetching

Normal HTTP fetching is the first strategy. The implementation supports configurable user agent, connect/request timeouts, bounded retries, exponential backoff, `Retry-After`, redirect limits, redirect target validation, content-type validation, maximum content size, canonical URL normalization, and SSRF protection.

Playwright fallback is represented but disabled by default and not bundled in the default image. It may only be enabled for sources that permit JavaScript automation.

## Documents

`ResearchDocument` supports canonical URL, original URL, title, source type/name/classification, publisher, publication/retrieval/discovery dates, language, content type, document type, content hash, instrument/company IDs, status, reliability, discovery provider, and source independence key. Raw and normalized bodies are excluded from API responses.

PDFs are supported as metadata/reference documents in Phase 3. OCR is not implemented.

## Entity Resolution

Resolution does not use ticker alone. It combines ISIN, company name, aliases, ticker plus exchange, country, and known domains, and returns a confidence score.

## Events And Scoring

`ResearchEvent` captures event type, date, source document, source URL, reliability, confidence, impact, time horizon, monetary values, percentages, customer/counterparty, location, capacity, and a short evidence reference.

Category scoring distinguishes validated evidence from missing evidence. Each category is reported as `POSITIVE_EVIDENCE`, `NEUTRAL_EVIDENCE`, `NEGATIVE_EVIDENCE`, `MIXED_EVIDENCE`, or `NO_EVIDENCE`; `NO_EVIDENCE` categories have a `null` score and are omitted from the overall catalyst calculation rather than being treated as score 50. Score 50 is reserved for validated neutral evidence.

Targeted research is an allowlisted discovery pass that runs only for categories with `NO_EVIDENCE` after primary source extraction. Discovery returns registered public sources with approved domains and source metadata; fetching still uses the SSRF, timeout, retry, content-type, redirect, size-limit, and user-agent controls. The system does not expose arbitrary URL fetching.

Optional search discovery runs after registered-source discovery when enabled with `AIP_RESEARCH_SEARCH_ENABLED=true` and a configured provider. The Google-compatible adapter requires `AIP_RESEARCH_SEARCH_ENDPOINT`, `AIP_RESEARCH_SEARCH_ENGINE_ID`, and a secret-backed `AIP_RESEARCH_SEARCH_API_KEY`; the adapter maps these to provider-specific `q`, `key`, `cx`, and `num` parameters. The Brave-compatible adapter requires `AIP_RESEARCH_SEARCH_ENDPOINT` and a secret-backed `AIP_RESEARCH_SEARCH_API_KEY`; it maps these to provider-specific `q`, `count`, and `X-Subscription-Token` values. It is bounded by max queries per category, max results per query, max documents per refresh, refresh cooldown, and cache TTL settings. Discovered URLs are classified as `OFFICIAL_COMPANY`, `REGULATORY`, `EXCHANGE`, `CUSTOMER`, `PARTNER`, `SUPPLIER`, `REPUTABLE_NEWS`, or `OTHER`; `OTHER` is rejected from scoring.

Source independence is tracked with canonical URLs and normalized content hashes. Syndicated or copied pages are deduplicated and do not increase independent-source confidence. Conflicting positive and negative evidence is preserved as `MIXED_EVIDENCE`.

The deterministic `CatalystScore` is 0-100 and uses event weights, source reliability, confidence, impact, and temporal decay. It is not a recommendation engine.

## Persistence And Events

Versioned event contracts exist for `research.document.discovered`, `research.document.fetched`, `research.document.processed`, `research.event.extracted`, `research.event.rejected`, and `research.company.updated`. Events contain IDs and metadata, not raw page bodies or secrets.

Phase 4 persists normalized research data in PostgreSQL when `AIP_RESEARCH_PERSISTENCE_ENABLED=true`. The research engine uses the `research_documents`, `research_events`, `research_event_sources`, and `research_refresh_runs` tables. Runtime summaries are still built from the same provider-neutral `ResearchDocument` and `ResearchEvent` models, but REAL documents and events are reloaded from durable storage at startup.

Research schema migrations are owned by the Spring `research-service`, following the existing repository Flyway pattern used by portfolio and broker services. The migration is `services/research-service/src/main/resources/db/migration/V1__research_intelligence.sql`, applied by Flyway as version `1 - research intelligence` into the `research` schema with history table `flyway_schema_history_research`. In Kubernetes DEV, `research-engine` waits for `research-service` readiness when persistence is enabled, so Flyway has completed before the Python engine starts using PostgreSQL. The Python PostgreSQL repository does not create production tables at runtime; it fails fast with `RESEARCH_SCHEMA_NOT_MIGRATED` if the Flyway-owned schema is missing.

Idempotency strategy:

- Documents are unique by canonical URL and content hash.
- Events are unique by a stable event fingerprint composed from instrument ID, event type, normalized evidence reference, monetary value, customer, and counterparty.
- Event-source links are unique by event, document, and evidence excerpt, which allows multiple supporting documents without duplicate scoring contribution.
- Refresh runs are append-only audit records with safe status, counters, correlation ID, and sanitized error fields.

Portfolio-wide research refresh and its durable job polling model were retired
in Iteration 4. Portfolio and watchlist summaries remain read-only projections;
public company acquisition is keyed by `globalInstrumentId` and initiated only
through the requirement-targeted readiness `ensure` endpoint.

Kafka is not introduced in Phase 4 because no downstream consumer requires decoupled research updates yet. The existing in-process platform event list remains safe metadata only. If a future `ResearchUpdated` Kafka event is added, it must contain only event ID, version, correlation ID, instrument ID, company ID, refresh run ID, research status, and generated time.

## Security

The fetcher rejects localhost, loopback, private ranges, link-local addresses, cloud metadata endpoints, non-http(s) schemes, and unsafe redirect targets. Search API keys are optional runtime secrets and must not be committed.
