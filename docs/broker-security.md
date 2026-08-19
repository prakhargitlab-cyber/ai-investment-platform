# Broker Security

Broker integrations are read-only in Phase 2B. The platform must not store broker usernames, passwords, OTP values, CAPTCHA answers, MFA answers, or raw access tokens in source, logs, Kafka messages, frontend bundles, or Docker images.

## Secret Boundary

- `SecretProvider` reads secrets from the environment in DEV.
- `AzureKeyVaultSecretProvider` is a production boundary only and is not required for local development.
- `BrokerTokenStore` stores token references and session state, not broker passwords or OTP values.
- Token storage is intentionally abstracted so production storage can become encrypted or Key Vault backed without changing broker callers.

## Authentication Boundary

Provider authentication can only be implemented from official provider documentation. If a provider requires OAuth, gateway software, a callback, local service, user approval, or a session refresh endpoint, the adapter must model those operational prerequisites exactly.

## Audit Boundary

Broker operation audit logging records provider, operation, timestamp-derived duration, success or failure, correlation ID, and status code strings. It must not log request or response payloads that may contain tokens, account details, OTP values, or credentials.

## Prohibited

- Browser automation against broker login pages.
- Scraping OTP, CAPTCHA, or MFA screens.
- Persisting raw broker credentials.
- Exposing auth values to frontend code.
- Retrying authentication aggressively.
- Executing trades.
