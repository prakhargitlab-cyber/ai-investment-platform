# Architecture

## System Shape

The platform is a monorepo with independently containerisable services:

- Java Spring Boot services own application APIs and business workflows.
- Python FastAPI services own AI, research extraction, scoring assistance, ranking, valuation, and optimization engines.
- Next.js owns the browser-facing application shell.
- Shared Java modules hold common domain and web concerns that would otherwise be duplicated.

## Service Boundaries

- `api-gateway`: future external API entrypoint and routing policy.
- `auth-service`: authentication and authorization boundary.
- `portfolio-service`: portfolio, positions, holdings, and exposure model.
- `broker-service`: broker provider orchestration through `BrokerProvider`.
- `company-service`: company and instrument reference data.
- `research-service`: orchestration of public-source research workflows.
- `recommendation-service`: deterministic recommendation calculation and explanation assembly.
- `risk-service`: portfolio risk, concentration, currency exposure, and diversification checks.
- `notification-service`: alerts and asynchronous user notifications.

## Authentication And User Isolation

Phase 5A makes the application multi-user. The browser sends a bearer access token to the API Gateway. The gateway validates the token, derives a stable application user ID from the validated `(issuer, subject)`, strips client-supplied internal identity headers, and forwards trusted `X-AIP-User-*` headers to internal services.

The normalized principal contains:

- application user ID
- issuer
- subject
- email, when available
- display name, when available
- roles or authorities, when available

Portfolio and broker services keep service-local `app_users` identity caches with a unique `(issuer, external_subject)` boundary. These records support foreign keys from private resources while keeping business services independent of a specific identity provider implementation.

All private portfolio and broker repository lookups include the authenticated user ID. A guessed UUID for another user's portfolio or broker connection is treated as not found. The frontend never supplies trusted owner IDs; owner assignment comes only from gateway-authenticated identity context.

DEV uses two deterministic local identities (`user-a`, `user-b`) through the auth service so isolation can be validated locally. PRD disables DEV login and expects real OIDC-compatible issuer configuration plus Kubernetes Secret references for token validation material.

## Shared Domain

Securities are not identified by ticker alone. The initial `Instrument` model includes:

- internal instrument ID
- ISIN
- ticker
- exchange
- MIC
- currency
- country
- asset type
- company name

Broker-specific instrument identity additionally carries broker security ID and broker contract ID where providers expose them. Stable normalization uses the broker identifier plus ISIN, exchange, ticker, and currency; ticker alone is never globally unique.

The model is deliberately shared from `shared/java/domain` to avoid duplicated incompatible security identifiers across services.

## Broker Provider Architecture

Broker integrations must implement `BrokerProvider`.

Initial placeholders:

- `IBKRBrokerProvider`
- `ICICIDirectBrokerProvider`

The real-provider adapters fail safely because official provider documentation is not present locally. Disabled providers report `NOT_CONFIGURED`; enabled but unverified providers report `DOCUMENTATION_REQUIRED`. The placeholders throw `UnsupportedOperationException` for read calls until supported authentication, session handling, and market data APIs are verified from official documentation. Browser automation that stores broker usernames or passwords is out of scope and prohibited.

### Broker Connector Architecture

Broker-service owns broker connections and talks to provider-specific connector abstractions. For IBKR Individual accounts, `BrokerProvider` delegates gateway HTTP details to a `BrokerConnector` implementation. Each authenticated user's IBKR connection is associated with a user-owned connector instance (`connector_id`, `user_id`, provider, runtime/auth status, heartbeat/auth timestamps, and timeout settings). No password, MFA value, browser cookie, or raw session token is stored.

The same contract is used in DEV and PRD:

- DEV: k3d -> broker-service -> broker connector endpoint -> user-specific connector runtime.
- PRD: AKS -> broker-service -> broker connector endpoint -> user-specific connector runtime.

Only environment, routing, secret backend, scaling, and resource values should differ. The connector runtime mode is abstracted as `KUBERNETES` or `LOCAL_AGENT`, because IBKR documents Client Portal Gateway as a local runtime and AKS-hosted Gateway is not production-approved until IBKR confirms that model for this SaaS use case.

Trading capabilities remain absent. `ORDER_EXECUTION` is rejected by the shared broker capability model.

Phase 2B adds broker-neutral session, token-reference, rate-limit, retry, circuit-breaker, audit, and instrument-normalization boundaries. These are architecture boundaries only; they do not make IBKR or ICICI Direct connected providers.

Market data is routed through a fallback provider: verified source, stale cache, demo mock only when explicitly enabled, otherwise `UNAVAILABLE`. Mock quotes are marked `MOCK` and displayed as `DEMO`.

## AI Provider Architecture

The Python research engine defines an `LlmProvider` protocol. DEV can use Ollama/local models; PRD can configure a cloud LLM provider later without changing callers.

LLMs should extract, summarize, and explain. Deterministic recommendation scoring remains owned by application logic.

## Research Access

Research fetching must start with normal HTTP fetching and HTML parsing. Playwright is reserved for permitted JavaScript-rendered public pages. The platform must respect robots.txt, website terms, rate limits, licensing, authentication walls, paywalls, CAPTCHAs, and other access controls.

Phase 3 research intelligence is implemented in `ai/research-engine`. It creates structured evidence from source documents, performs deduplication and entity resolution, extracts typed research events, and computes deterministic catalyst scores. The LLM boundary is optional and schema-constrained; final BUY/SELL recommendations are not implemented.

Shared public research intelligence is not duplicated per user. Companies, instruments, source documents, source provenance, and globally applicable research events remain shared. Portfolio research endpoints are authenticated and private because portfolio membership is user-owned; they can reuse shared company intelligence without exposing another user's portfolio.

## Environments

The same source and images should run in DEV and PRD.

- DEV: Docker Desktop plus k3d, local PostgreSQL, local Redis, local Kafka, optional Ollama. DEV PostgreSQL must use a Kubernetes PVC and must not rely on the Postgres pod or container filesystem for data durability.
- PRD: Azure AKS, ACR, VNet, AKS subnet, Key Vault, Helm deployments, Terraform-owned infrastructure. PRD must use managed PostgreSQL or equivalent durable production database storage; k3d `local-path` storage is DEV-only and must not be reused as a production persistence model.

## Global instrument identity

`portfolio-service` owns the durable global Instrument Master because it already owns position-facing instrument adoption and the portfolio PostgreSQL schema. Flyway V16 adds `instrument_master`, `instrument_provider_mappings`, and the nullable `instruments.master_instrument_id` adoption FK. Existing `instruments` and position foreign keys are preserved; legacy rows are linked lazily only through strong ISIN, stable provider ID, or exact provider listing identity.

The master contains only public, slowly changing security identity. Provider mappings retain listing context, so a company, an ADR, another share class, and another currency/exchange listing are not merged by name. ISIN is the strongest cross-source identity when present; IBKR conid and import-provider security keys remain durable provider identities. NSE/BSE mappings are created only from supplied exchange identity, and Yahoo symbols are discovered and validated—suffixes are never manufactured.

PostgreSQL is authoritative. Quote caches and the research engine's bounded in-process cache are optional accelerators and are not loaded with the full master at startup. Portfolio responses expose both the unchanged legacy `instrumentId` and `globalInstrumentId`, plus reusable verified provider mappings. The research engine consumes this provider-neutral response and reuses a verified `YAHOO_FINANCE` mapping before external search. A conflicting ticker, exchange, currency, or quote type is rejected and requires explicit revalidation; an existing verified mapping is never silently overwritten.

DEV and PRD use the same source code, Dockerfile strategy, and Helm templates. Environment differences are supplied through Helm values, Terraform variables, and runtime environment variables.

## Frontend Experience

Frontend work must follow the mandatory requirements in `docs/frontend-ui-ux-requirements.md`.

The browser application should look and behave like a production-grade investment intelligence platform: premium financial SaaS, not a generic admin dashboard. Phase 2B uses actual portfolio and broker-readiness APIs where available and clearly labels mock broker output as demo data.
