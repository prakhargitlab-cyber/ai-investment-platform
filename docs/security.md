# Security

## Credential Rules

Do not commit:

- broker usernames or passwords
- Azure credentials
- API keys
- LLM provider secrets
- database passwords
- private keys or certificates

Use `.env.example` files only for non-secret configuration shape. Store PRD secrets in Azure Key Vault and expose them to workloads through approved Kubernetes secret mechanisms.

## Broker Security

Broker integration must use provider-supported authentication and session mechanisms. Do not implement browser automation that stores or replays broker credentials.

Initial broker classes are placeholders only:

- `IBKRBrokerProvider`
- `ICICIDirectBrokerProvider`

They must not be treated as working integrations until official/supported API research is complete.

## Research Compliance

Automated research must:

- respect robots.txt and website terms
- obey rate limits
- use normal HTTP fetching first
- use Playwright only for permitted JavaScript-rendered public pages
- avoid CAPTCHA bypass
- avoid paywall bypass
- avoid authentication bypass
- respect licensing and redistribution restrictions

## Application Security Defaults

- Correlation IDs are propagated through `X-Correlation-Id`.
- Health endpoints are present for orchestration.
- Configuration is environment-driven.
- Source code contains no default production credentials.
- PRD Azure commands validate subscription context before planning or destroying.

## Phase 5A Authentication Boundary

The browser authenticates through `/api/v1/auth/dev/login` only in DEV. The DEV mechanism issues a signed bearer JWT for one of the fixed local identities (`user-a`, `user-b`) so multi-user isolation can be tested without real broker credentials or a production identity provider. PRD disables DEV login and must provide a real authentication configuration and Kubernetes Secret for token validation material before deployment.

The gateway is the external trust boundary for application APIs. Private `/api/**` routes require `Authorization: Bearer <token>` except intentional auth bootstrap and health routes. The gateway validates the token issuer and signature, derives the durable application user ID from `(issuer, subject)`, and stores the normalized principal as request attributes. It strips all inbound `X-AIP-User-*` headers before forwarding and then adds trusted internal identity headers itself.

Business services must not trust browser-supplied owner IDs. Portfolio and broker APIs resolve the authenticated principal from trusted gateway headers, provision a service-local `app_users` identity cache, and query private resources by authenticated user ID plus resource ID. Cross-user access is intentionally returned as not found to avoid confirming resource existence.

The durable external identity key is `(issuer, external_subject)`. Email and display name are profile attributes only and must not be used as the security identity.

## Ownership Model

Private data is user-owned:

- portfolios
- portfolio positions and holdings through their portfolio
- broker connections and future broker account references
- future preferences, watchlists, alerts, notes, and decisions

Shared public intelligence remains shared where appropriate:

- companies and instrument reference data
- public source documents
- public research events
- source provenance
- globally applicable catalyst calculations

Portfolio research is private because the portfolio membership is private. It can reuse shared company intelligence, but a user must not see another user's portfolio or broker connection because they share the same company.

## Broker Credential Boundary

Phase 5A does not enable real IBKR or ICICI Direct login. Broker connections are user-owned before real providers are enabled. Raw broker passwords, OTPs, MFA codes, plaintext broker secrets, JWTs, and access tokens must not be logged or stored. Future broker credentials must be represented through token or secret references tied to the authenticated user's broker connection.

## CORS

DEV may allow local browser origins such as `http://localhost:13000` and must allow the `Authorization` header. PRD must not add localhost origins and must not use wildcard origins with credentials.

## Future Required Controls

- Secret rotation process.
- Audit logging for broker sessions and recommendation generation.
- Formal threat model before broker connectivity is enabled.
