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

Phase 2B adds broker-neutral session, token-reference, rate-limit, retry, circuit-breaker, audit, and instrument-normalization boundaries. These are architecture boundaries only; they do not make IBKR or ICICI Direct connected providers.

Market data is routed through a fallback provider: verified source, stale cache, demo mock only when explicitly enabled, otherwise `UNAVAILABLE`. Mock quotes are marked `MOCK` and displayed as `DEMO`.

## AI Provider Architecture

The Python research engine defines an `LlmProvider` protocol. DEV can use Ollama/local models; PRD can configure a cloud LLM provider later without changing callers.

LLMs should extract, summarize, and explain. Deterministic recommendation scoring remains owned by application logic.

## Research Access

Research fetching must start with normal HTTP fetching and HTML parsing. Playwright is reserved for permitted JavaScript-rendered public pages. The platform must respect robots.txt, website terms, rate limits, licensing, authentication walls, paywalls, CAPTCHAs, and other access controls.

Phase 3 research intelligence is implemented in `ai/research-engine`. It creates structured evidence from source documents, performs deduplication and entity resolution, extracts typed research events, and computes deterministic catalyst scores. The LLM boundary is optional and schema-constrained; final BUY/SELL recommendations are not implemented.

## Environments

The same source and images should run in DEV and PRD.

- DEV: Docker Desktop plus k3d, local PostgreSQL, local Redis, local Kafka, optional Ollama.
- PRD: Azure AKS, ACR, VNet, AKS subnet, Key Vault, Helm deployments, Terraform-owned infrastructure.

DEV and PRD use the same source code, Dockerfile strategy, and Helm templates. Environment differences are supplied through Helm values, Terraform variables, and runtime environment variables.

## Frontend Experience

Frontend work must follow the mandatory requirements in `docs/frontend-ui-ux-requirements.md`.

The browser application should look and behave like a production-grade investment intelligence platform: premium financial SaaS, not a generic admin dashboard. Phase 2B uses actual portfolio and broker-readiness APIs where available and clearly labels mock broker output as demo data.
