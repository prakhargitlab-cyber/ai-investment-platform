# ICICI Direct Provider

## SUPPORTED

- Provider registration and frontend status exposure.
- Fail-safe configuration model.
- Read-only capability contract placeholders.
- Provider-specific Indian instrument normalization using broker security ID, ISIN, ticker, exchange, MIC, INR currency, country, and asset type.

## NOT SUPPORTED

- Order placement.
- Login page scraping.
- OTP, CAPTCHA, or MFA screen automation.
- Stored passwords, OTP values, or MFA answers.
- Real account, holdings, positions, cash, margin, session, or market-data calls.

## AUTH METHOD

Official ICICI Direct integration documentation is not available in this repository. The adapter must not assume API keys, OAuth, session-token exchange, headers, token refresh behavior, or account identifiers until official docs are added and reviewed.

## CONFIG REQUIRED

Configuration is read from environment-backed Spring properties only:

- `ICICI_DIRECT_ENABLED`
- `ICICI_DIRECT_BASE_URL`
- `ICICI_DIRECT_CLIENT_ID`
- `ICICI_DIRECT_CALLBACK_URL`
- `ICICI_DIRECT_AUTH_METHOD`
- `ICICI_DIRECT_OFFICIAL_DOCUMENTATION_VERIFIED`

These names are local integration placeholders and must be reconciled with official ICICI Direct documentation before real connectivity is enabled.

## READ-ONLY CAPABILITIES

No real ICICI Direct read capability is currently enabled. Future verified read-only targets are accounts, holdings, positions, cash or margin information, provider session status, and permitted market data.

## CAPABILITIES

Current advertised capabilities: none.

## KNOWN LIMITATIONS

- Provider reports `NOT_CONFIGURED` by default.
- Provider reports `DOCUMENTATION_REQUIRED` when enabled without locally verified official documentation.
- No network requests are made.

## OFFICIAL DOCUMENTATION VERIFIED

NO

## REAL CONNECTION VALIDATED

NO

## CURRENT PROVIDER STATUS

- Default: `NOT_CONFIGURED`
- Enabled without locally verified official docs: `DOCUMENTATION_REQUIRED`
- Real trades executed: NO

## DOCUMENTATION SOURCE

No current official ICICI Direct API documentation was found in repository-local docs or source files during Phase 2B.
