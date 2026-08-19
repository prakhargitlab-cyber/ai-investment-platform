# Interactive Brokers Provider

## SUPPORTED

- Provider registration and frontend status exposure.
- Fail-safe configuration model.
- Read-only capability contract placeholders.
- Provider-specific instrument normalization using contract ID, ISIN, ticker, exchange, MIC, currency, country, and asset type.

## NOT SUPPORTED

- Order placement.
- Browser automation.
- Stored broker usernames or passwords.
- Real account, position, cash, metadata, session, or market-data calls.

## AUTH METHOD

Official IBKR authentication documentation is not available in this repository. The adapter must not assume OAuth, Client Portal Gateway, TWS, IB Gateway, callbacks, headers, token formats, session refresh, or account identifiers until official docs are added and reviewed.

## CONFIG REQUIRED

Configuration is read from environment-backed Spring properties only:

- `IBKR_ENABLED`
- `IBKR_BASE_URL`
- `IBKR_CLIENT_ID`
- `IBKR_CALLBACK_URL`
- `IBKR_AUTH_METHOD`
- `IBKR_OFFICIAL_DOCUMENTATION_VERIFIED`

These names are local integration placeholders and must be reconciled with official IBKR documentation before real connectivity is enabled.

## READ-ONLY CAPABILITIES

No real IBKR read capability is currently enabled. Future verified read-only targets are accounts, positions, cash balances, account metadata, provider session status, and permitted market data.

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

No current official IBKR API documentation was found in repository-local docs or source files during Phase 2B.
