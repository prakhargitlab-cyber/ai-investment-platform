# Phase 5D.2 — ICICI Direct / Breeze API Contract Verification

Research date: 2026-08-29  
Status: research/design only; no live request or production integration was performed.

## Executive decision

The current official retail product is **Breeze API by ICICI Securities / ICICI Direct**. It provides official HTTP documentation and Java, Python, and JavaScript SDKs. Verified reads include Customer Details, Demat Holdings, Portfolio Holdings, Portfolio Positions, Funds, Margin, Quotes, and instrument lookup/master data.

Use a narrowly allowlisted direct Java HTTP client in the future. Authentication is interactive and daily: the user logs into the official Breeze page with the AppKey, completes ICICI Direct credentials plus OTP, receives an `API_Session`, and exchanges it through Customer Details for the signed-request `session_token`. The session key expires 24 hours after generation or at midnight, whichever is earlier.

Live implementation remains blocked by two material gaps: no documented API logout/revocation operation; and Demat Holdings expose ISIN/quantity but no prices, while Portfolio Holdings expose prices but no ISIN/token. Joining by ticker alone is unsafe. Use ISIN as equity identity and never fabricate price/cost data.

## A. Official API/product name

**Breeze API**, ICICI Direct's retail API and the ICICI Securities trading API named by the official SDKs. The portal indicates that an ICICI Direct account is required and links account opening.

## B. Official sources consulted

- [Breeze HTTP API Reference](https://api.icicidirect.com/breezeapi/documents/index.html)
- [Breeze API portal](https://api.icicidirect.com/apiuser/home)
- [Official session-key article](https://www.icicidirect.com/futures-and-options/api/breeze/article/what-is-a-session-key-and-how-to-generate-it-for-using-breezeapi)
- [Official Breeze Java SDK](https://github.com/Idirect-Tech/Breeze-Java-SDK)
- [Official Breeze Python SDK](https://github.com/Idirect-Tech/Breeze-Python-SDK)
- [Official Python SDK examples](https://github.com/Idirect-Tech/Breeze-Python-SDK/blob/main/README.md)
- [Official Python SDK endpoint configuration](https://github.com/Idirect-Tech/Breeze-Python-SDK/blob/main/breeze_connect/config.py)

The older [ICICIDirect API document](https://api.icicidirect.com/apiuser/ICICIDirectAPIDOC.htm) was checked only for legacy differences. Current Breeze documentation/SDKs take precedence.

## C. Authentication flow

The official reference calls the mechanism OAuth 2.0, but documents a Breeze-specific flow—not a standard authorization-code/token endpoint exchange. Do not assume generic Spring OAuth behavior.

1. The ICICI Direct customer logs into the Breeze portal and registers an app with app name and redirect URL.
2. Registration issues an **AppKey** and **secret_key** unique to the app.
3. Send the user's browser to `https://api.icicidirect.com/apiuser/login?api_key=<URL-ENCODED-APPKEY>`.
4. On the official page, the user enters ICICI Direct credentials, generates an OTP, and logs in. Our platform must never collect or automate those values.
5. Successful login exposes an **API_Session** in the resulting URL/page. Exact callback method and parameter encoding are **UNVERIFIED**.
6. Call Customer Details with JSON fields `SessionToken=<API_Session>` and `AppKey=<AppKey>`. The official reference specifies `GET /breezeapi/api/v1/customerdetails`, JSON content type, and no signed common headers.
7. Customer Details returns `Success.session_token`, used as `X-SessionToken` thereafter. The SDK decodes it internally into user ID/session key; direct REST uses the returned value.
8. For each subsequent v1 request, serialize the exact JSON body, create UTC ISO-8601 time with zero milliseconds (for example `2024-06-01T10:23:56.000Z`), and compute `SHA-256(timestamp + exactJsonBody + secret_key)`.
9. Send `Content-Type: application/json`, `X-Checksum: token <hex-digest>`, `X-Timestamp`, `X-AppKey`, and `X-SessionToken`. Client/server time must be within 60 seconds.

| Authentication item | Verification |
|---|---|
| AppKey, secret_key, registered redirect URL | VERIFIED |
| Browser login URL, ICICI credentials and OTP on official page | VERIFIED |
| API_Session and Customer Details exchange | VERIFIED |
| Signed headers and SHA-256 checksum | VERIFIED |
| Generic OAuth refresh token | UNVERIFIED; none documented |
| Programmatic credential/OTP authentication | UNVERIFIED and prohibited here |
| Exact callback method/parameter encoding | UNVERIFIED |

## D. Session lifecycle

- Session key validity: **24 hours after generation or midnight, whichever is earlier**.
- A new key is required for the next trading day; the app need not be recreated but must be active.
- Known expiry/auth failures map to `SESSION_EXPIRED` and require interactive login.
- Refresh token/silent renewal: **UNVERIFIED / unsupported**.
- REST logout/revocation: **UNVERIFIED**. Disconnect must erase local secrets and must not claim server revocation.
- Whether a new session invalidates old sessions and permitted concurrency: **UNVERIFIED**.

## E. Required credential/configuration fields

| Current field | Decision | Reason |
|---|---|---|
| `enabled` | KEEP | Local feature gate. |
| `baseUrl` | KEEP | Official v1 base is `https://api.icicidirect.com/breezeapi/api/v1/`; retain a test/config seam. |
| `clientId` | RENAME to `appKey` | Official term is AppKey. |
| `callbackUrl` | RENAME to `redirectUrl` | Official registration term. |
| `authMethod` | KEEP as fixed strategy selector | Use a local value such as `breeze-interactive-session`, not generic authorization-code OAuth. |
| `officialDocumentationVerified` | KEEP | Existing safety gate. |

## F. Recommended future `ICICIDirectProviderProperties`

Future conceptual properties:

```text
enabled
baseUrl
loginUrl
appKey
redirectUrl
authMethod = breeze-interactive-session
secretKeyReference
officialDocumentationVerified
```

`secretKeyReference` is an **ADD** and resolves through a secret provider. Add `loginUrl` only as an allowlisted test seam. Do not add persisted API/session tokens, ICICI password, OTP, or speculative security ID. Runtime session material does not belong in provider properties.

## G. Account contract

Verified operation: `GET /breezeapi/api/v1/customerdetails` with `{"SessionToken":"<API_Session>","AppKey":"<AppKey>"}`.

Verified response fields include `idirect_userid`, `idirect_user_name`, `idirect_lastlogin_time`, `segments_allowed`, exchange dates/status, and `session_token`.

Safe normalized mapping:

| `BrokerAccount` field | Mapping |
|---|---|
| `brokerAccountId` | Deterministic internal ID from provider + `idirect_userid`. |
| `userId` | Authenticated application user only. |
| `brokerType` | `ICICI_DIRECT`. |
| `externalAccountReference` | Masked `idirect_userid`; whether this is a trading-account number is **UNVERIFIED**. |
| `displayName` | `idirect_user_name`, with privacy controls. |
| `baseCurrency` | INR is implied by Funds' rupee semantics, but Customer Details has no currency field: **UNVERIFIED as an account attribute**. |
| `status` | Active only after current-session Customer Details succeeds. |

Account type, Demat/trading account number, multiple-account enumeration, and explicit base currency are **UNVERIFIED**. The endpoint describes the authenticated customer, not an account list.

## H. Holdings contracts

### Demat Holdings — preferred long-term ownership source

`GET /breezeapi/api/v1/dematholdings`, signed headers, exact `{}` body.

Verified fields: `stock_code`, `stock_ISIN`, `quantity`, `demat_total_bulk_quantity`, `demat_avail_quantity`, `blocked_quantity`, `demat_allocated_quantity`.

This explicitly represents Demat holdings and supplies ISIN. It does not supply average cost, price, value, or exchange. Use documented `quantity`; no official semantics justify a derived net formula from the other quantities. Preserve them as metadata when supported.

### Portfolio Holdings — richer portfolio/valuation view

`GET /breezeapi/api/v1/portfolioholdings` with `exchange_code` (`NSE`/`NFO`), ISO dates, `stock_code`, and optional `portfolio_type`. The reference marks dates/stock code required while official SDK examples use blank stock code for all; complete-query semantics are **UNVERIFIED**.

Responses include stock/exchange code, quantity, average/current price, product and derivative fields, realized/unrealized P&L, and open-position value. Official SDK guidance says `NSE` for equity holdings. It returns no ISIN/token, so it must not independently create canonical equity positions. A ticker-only join to Demat Holdings is unsafe and **UNVERIFIED**.

## I. Positions contract

`GET /breezeapi/api/v1/portfoliopositions`, signed headers, `{}` body.

Responses include segment/product/exchange/stock, derivative attributes, action, quantity, average price, LTP/price, settlement/margin, cover/stop-loss, MTF, pledge, P&L, and order-related fields.

This is distinct from Demat Holdings and represents trading positions, including derivative and potentially intraday/MTF state. It must not substitute for delivery holdings. Initial live scope should exclude derivatives and leveraged/intraday products until their identity/lifecycle are separately designed.

## J. Funds/cash contract

Verified read: `GET /breezeapi/api/v1/funds`, signed headers, empty JSON body.

Verified fields: `bank_account`, `total_bank_balance`, segment allocations, per-segment trade blocks, `block_by_trade_balance`, and `unallocated_balance`. The write form documents amounts as rupees, supporting INR normalization. Do not invent arithmetic relationships.

Safe initial mapping: INR; reported cash from `total_bank_balance` with provider-label provenance; settled cash `null`; preserve allocations/blocks/unallocated separately rather than calling them available or settled cash. Never invoke `POST /funds` (`setFunds`). Official `GET /margin` fields are margin, not cash, and must not populate settled cash.

## K. Stable instrument identity

For delivery equities:

```text
provider = ICICI_DIRECT
externalInstrumentId = ISIN:<normalized stock_ISIN>
```

`stock_ISIN` is an official Demat Holdings field and is a security identifier, unlike display ticker. Remove formatting whitespace and validate ISIN shape. Ticker alone is forbidden.

Official `get_names` maps exchange/stock code to `isec_stock_code` and `isec_token`; the master is updated daily at 08:00. Token stability across corporate actions/exchanges is **UNVERIFIED**. Portfolio responses expose neither ISIN nor token, and their safe mapping to Demat ISIN is **UNVERIFIED**. Reject/quarantine records without verified ISIN rather than invent identity. Canonical derivative identity is also **UNVERIFIED**.

The existing normalizer may structurally accept broker security ID or ISIN, but the future delivery-equity adapter should supply only verified ISIN unless ICICI documents token stability.

## L. Exchange representation

The HTTP reference verifies `NSE` cash equity and `NFO` derivatives, and says BSE/MCX securities are currently unavailable. Current official SDK notes also list BSE/BFO, and Customer Details samples expose BSE/FNO/NDX status. Therefore:

- `NSE`: VERIFIED for cash equity.
- `NFO`: VERIFIED for NSE derivatives.
- BSE/BFO: **UNVERIFIED due to conflicting official sources**.
- MCX/NDX applicability: **UNVERIFIED for Phase 5D.3**.

Preserve upstream exchange codes and normalize only explicitly supported values; do not hard-code the broader SDK list yet.

## M. Rate limits and operational rules

- REST limit: **100 calls/minute and 5,000 calls/day**.
- Timestamp skew: at most **60 seconds**.
- Session: **24 hours or midnight, whichever is earlier**.
- App must be active to generate a session.
- Session concurrency, login conflicts, read trading-hour restrictions, maintenance SLA, and throttling response/`Retry-After`: **UNVERIFIED**.
- Static IP is officially required for order requests. Its applicability to read-only calls is **UNVERIFIED**.

Enforce per-user/per-connection budgets beneath both limits. Do not retry authentication failures; only bounded/backoff retries for safe reads.

## N. SDK vs direct HTTP recommendation

Choose **A. direct Java HTTP client**.

Direct REST is officially documented. The official Java SDK exists but is documented as a manually included JAR and exposes trading mutations alongside reads. A small allowlisted Spring client can expose only approved reads, avoids a Python sidecar, and simplifies Kubernetes deployment, observability, timeouts, connection pooling, and scoped sessions. The official Java SDK remains a contract oracle for signing/serialization tests.

## O. Connector mapping

| Connector method | Verified upstream/design |
|---|---|
| `status(userId, connectionId)` | No status endpoint. Check scoped local expiry; optionally probe Customer Details within limits. Missing session → `AUTHENTICATION_REQUIRED`; known expiry/auth failure → `SESSION_EXPIRED`; transport/5xx → unavailable; success → `CONNECTED`. |
| `fetchAccounts(...)` | Customer Details; produce one current-customer account without fabricating type/number. |
| `fetchPositions(...)` | Initial delivery source is Demat Holdings. Portfolio Holdings may enrich only after safe identity joining; Portfolio Positions remains a distinct trading-position feed. |
| `fetchCashBalances(...)` | Funds GET; preserve semantic distinctions and never treat Margin as cash. |
| `disconnect(...)` | Erase scoped local API_Session/session token. Server logout is **UNVERIFIED**. |

All state remains keyed by `(authenticated application userId, broker connectionId)`. No singleton client may hold mutable session credentials.

## P. Read-only capabilities

Advertise only after implementation and fixture tests:

| Capability | Safe decision |
|---|---|
| `ACCOUNTS_READ` | Supported via Customer Details, subject to one-current-customer limitation. |
| `ACCOUNT_METADATA_READ` | Supported for returned identity/name/segment/exchange metadata. |
| `POSITIONS_READ` | Supported only with explicit distinction between delivery Demat holdings and trading positions. |
| `CASH_READ` | Supported via Funds, without a settled-cash claim. |
| `PORTFOLIO_READ` | Enable only when ISIN holdings can be represented without fabricated price/cost; may require normalized-model adjustment or verified enrichment. |
| `ORDER_EXECUTION` | Always absent. |

Until documentation, configuration, authentication, connector implementation, and tests are satisfied, capabilities remain empty.

## Q. Security/session storage

- No secrets/session values in Git, logs, errors, API responses, portfolio-service, or portfolio tables.
- Handle AppKey conservatively; resolve `secret_key` through `secretKeyReference`.
- Production: Kubernetes Secret; future Azure Key Vault via existing secret-provider abstraction; local: ignored environment/process configuration.
- Encrypt API_Session/session token at rest, keyed by `(userId, connectionId)`, with created/expiry timestamps. Erase on disconnect/expiry.
- Build login URLs only on the allowlisted official host and URL-encode AppKey.
- Correlate callback completion to authenticated user/connection with one-time state/nonce. Upstream callback details remain **UNVERIFIED**.
- Never receive/store/proxy ICICI password or OTP.
- Sign exact sent bytes and redact AppKey, API_Session, session token, secret, checksum, bank account, and customer identifiers.
- Require TLS validation, strict timeouts, response-size limits, rate limits, and host allowlists.

## R. Consolidated UNVERIFIED items

1. Exact redirect callback method, parameter encoding, and error payload.
2. Refresh/silent renewal; REST logout/revocation.
3. Concurrent-session limits and web/mobile login interaction.
4. Whether `idirect_userid` is a formal trading-account number; account type, multiple accounts, explicit currency.
5. Demat quantity reconciliation beyond the reported `quantity`.
6. Safe Portfolio stock-code → Demat ISIN join; ISEC token/code stability.
7. Canonical derivative identity.
8. BSE/BFO read support due to conflicting official material; MCX/NDX Phase 5D.3 applicability.
9. Read-call static-IP requirement, trading-hour restrictions, maintenance/SLA, throttle response.
10. Whether `total_bank_balance` is withdrawable/available trading cash.
11. Whether blank filters/date ranges always yield the complete current equity Portfolio Holdings set.

Resolve these through updated official documentation or written clarification from `breezeapi@icicisecurities.com`, not experiments against a funded account.

## S. Proposed Phase 5D.3

1. Obtain written clarification for callback, logout, account ID/currency, complete holdings, instrument/token stability, and BSE.
2. Capture sanitized official fixtures for success, empty, malformed, expired, throttled, and unavailable responses—no live calls in automated tests.
3. Rename properties and add secret references; never store runtime tokens as properties.
4. Implement allowlisted read-only Java HTTP signing with injected clock.
5. Implement interactive login correlation and encrypted scoped sessions.
6. Normalize Customer Details and Funds.
7. Implement Demat Holdings with ISIN-only identity; decide how to represent missing cost/price before portfolio import.
8. Keep Portfolio Holdings enrichment and Portfolio Positions under separate semantics/tests.
9. Add dual-window limits, midnight/24-hour expiry, redaction, isolation, and no-order tests.
10. Advertise capabilities one by one only after contract tests.

Out of scope: fund mutations, margin additions, order/trade mutations, order streams/webhooks, and all order execution.

## T. Safety confirmation

- No live brokerage action or ICICI API request was performed.
- No order was placed and no production trading capability was enabled.
- No endpoint/schema/identity guarantee/auth behavior was invented; gaps are marked **UNVERIFIED**.
- IBKR code was not changed.
- Phase 5C portfolio persistence was not changed.
- No production code changed in Phase 5D.2; only this research document changed.
