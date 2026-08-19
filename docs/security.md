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

## Future Required Controls

- Authentication and authorization design review.
- Tenant and user data isolation model.
- Secret rotation process.
- Audit logging for broker sessions and recommendation generation.
- Formal threat model before broker connectivity is enabled.
