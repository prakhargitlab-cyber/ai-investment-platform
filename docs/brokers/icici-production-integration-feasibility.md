# ICICI Direct production integration feasibility

Research date: 2026-08-29  
Phase: 5D.6 — research and architecture only

## A. Executive conclusion

The public retail Breeze contract does **not** establish a production model in which one platform-owned AppKey authorizes thousands of unrelated ICICI Direct customers. Public documentation says a user registers an app, receives an AppKey/secret pair unique to that app, and logs in with that AppKey. It documents 100 calls/minute and 5,000 calls/day, but does not state whether those limits are scoped by AppKey, customer, session, account, IP, or another key. Multi-customer authorization and quota scope are therefore **UNVERIFIED**.

ICICI Securities officially states that it has API architecture for fintech partners and that Breeze offers tools/products from fintech partners. This is evidence that commercial partnerships exist, but no public partner API specification, eligibility criteria, multi-user authorization contract, quota model, pricing, or onboarding process was found. A production partner route is therefore **commercially plausible but technically UNVERIFIED** and requires written ICICI Securities confirmation.

The desired consumer experience—one platform application, ICICI-hosted authentication/MFA, callback, and a user-scoped authorization without consumer developer credentials—is **UNVERIFIED for ICICI Direct**. It must not be represented as supported until ICICI provides a partner contract.

For cross-broker read-only holdings, the strongest verified Indian route is the Account Aggregator ecosystem through depositories. CDSL officially operates as a Financial Information Provider and shares ISIN-wise demat holdings after customer account discovery, OTP linking, and consent. Both depositories are reported live in the AA ecosystem. However, only entities regulated by RBI, SEBI, IRDAI, or PFRDA may be FIUs. The platform must therefore either qualify under an appropriate regulatory registration or contract with a regulated FIU/AA technology partner; it must not self-identify as an FIU without legal confirmation.

Decision:

- Existing retail Breeze connector: **OPTIONAL_BYO_CREDENTIALS**.
- Preferred ICICI-specific production path: seek a written ICICI partner/enterprise arrangement with platform-owned credentials, per-user authorization, documented per-user or commercially adequate quotas, and read-only scope.
- Preferred broad Indian holdings aggregation path: investigate a regulated FIU partnership using CDSL/NSDL as AA FIPs.
- Phase direction: stop production-scale Breeze expansion pending ICICI commercial confirmation; retain the current connector for DEV, personal, and explicit BYO-credential use.

## B. Ordinary Breeze authentication ownership model

The [official Breeze reference](https://api.icicidirect.com/breezeapi/documents/index.html) says:

- registration occurs on the Breeze API portal;
- the registrant supplies an app name and redirect URL;
- an AppKey and `secret_key` pair unique to the registered app is issued;
- the AppKey identifies the app;
- login begins at the ICICI-hosted URL containing that AppKey;
- the successful login produces `API_Session`, which Customer Details exchanges for a session token.

The older [official ICICI Direct API document](https://api.icicidirect.com/apiuser/ICICIDirectAPIDOC.htm) is more explicit: “User will need to come and register on ICICI direct developer portal,” after which the user creates the AppKey/client secret. The [official Breeze portal](https://api.icicidirect.com/) calls Breeze a retail API, requires an ICICI Direct account, and says API facilities are offered to “clients” under client terms.

Verified ownership conclusions:

- The key pair belongs to a registered **application**.
- Registration is performed in a logged-in ICICI Direct/developer context and is presented as a client/user workflow.
- The public material does not define a corporate tenant, service account, delegated administrator, or partner client type.
- Whether an ordinary retail AppKey can belong to a company independently of one ICICI customer: **UNVERIFIED**.
- Whether one customer can create multiple keys: VERIFIED; the current reference says each client can have multiple keys, subject to order-routing restrictions for unregistered algos.

For public retail Breeze, the safest supported product interpretation is individual/client-owned developer credentials, not platform-wide delegated authorization.

## C. Exact Breeze rate-limit scope

The [official API reference](https://api.icicidirect.com/breezeapi/documents/index.html), [official FAQ](https://www.icicidirect.com/faqs/fno/what-is-the-rate-limit-for-breeze-api), and [Breeze product page](https://www.icicidirect.com/futures-and-options/api/breeze) state only:

> Breeze accepts up to 100 API calls per minute and 5,000 API calls per day.

None of those sources states the enforcement key.

| Possible scope | Officially established? |
|---|---|
| AppKey | **UNVERIFIED** |
| Registered application | **UNVERIFIED** |
| ICICI customer/client | **UNVERIFIED** |
| `API_Session` or session token | **UNVERIFIED** |
| Trading/demat account | **UNVERIFIED** |
| Source IP | **UNVERIFIED** for read calls |
| Combination of the above | **UNVERIFIED** |

The static-IP rules in the reference concern order routing and do not establish read-call quota scope. The platform must not model 5,000/day as either a global AppKey quota or a per-user quota until ICICI answers in writing.

## D. Can one platform AppKey serve unrelated users?

**UNVERIFIED.**

The login flow technically includes an AppKey and then an ICICI customer login, and Customer Details returns a customer-specific session. That demonstrates a user-specific session result, but it does not establish permission to use one AppKey for unrelated customers.

No official public source found answers:

- whether a retail AppKey may authorize a customer other than its registrant;
- whether one AppKey may authorize 10,000 unrelated customers;
- whether the redirect represents consent to a third-party platform;
- whether sessions under one AppKey are contractually segregated for a multi-tenant application;
- whether quotas are shared or per authorized customer;
- whether commercial approval is required.

Accordingly, the answer to “can our company register one Breeze application for 10,000 customers?” is **UNVERIFIED**, not yes.

## E. Official partner/enterprise options

Official evidence of partner activity exists:

- ICICI Securities’ [2019–20 annual report](https://www.icicisecurities.com/Upload/ArticleAttachments/ICICI_Securities_Limited_Annual_Report_FY1920.pdf) says it launched API architecture to integrate quickly with fintech players and partners.
- The [ICICI Securities corporate history](https://www.icicisecurities.com/wfrmAboutUs.aspx) says Breeze offers tools and products from pure-tech and fintech partners.
- A [2021 business performance presentation](https://www.icicisecurities.com/Upload/ArticleAttachments/Performance_Review_Q1_FY2022.pdf) describes an API/quant ecosystem and third-party platform connectivity.
- The [2021–22 annual report](https://www.icicisecurities.com/Upload/ArticleAttachments/ICICI_Securities_Limited_Annual_Report_FY2021_22.pdf) describes APIfication and partnering with fintech/technology partners.

What this proves:

- ICICI Securities has undertaken fintech and third-party API integrations.
- Breeze has a third-party platform ecosystem.

What it does not prove:

- a generally available partner or enterprise API;
- partner eligibility or pricing;
- a public OAuth authorization-code flow;
- read-only delegated scopes;
- a multi-customer AppKey contract;
- partner quota or SLA;
- production onboarding documentation.

No separate public partner developer portal or technical specification was found. Therefore:

| Option | Finding |
|---|---|
| Retail Breeze only | Publicly documented and verified |
| Separate partner/enterprise API | **UNVERIFIED** |
| Negotiated commercial Breeze capacity | **UNVERIFIED** |
| Partner OAuth/consent | **UNVERIFIED** |
| Bespoke fintech integration | Officially evidenced as a business practice; technical availability is **UNVERIFIED** |

The official public contact is [breezeapi@icicisecurities.com](https://api.icicidirect.com/breezeapi/documents/index.html). No dedicated enterprise onboarding contact was found.

## F. Consumer redirect/consent support

Desired experience:

```text
Platform-owned application
  → ICICI-hosted customer login and MFA
  → explicit delegated consent
  → callback authorization code/session
  → per-user platform token
```

Classification: **UNVERIFIED**.

Retail Breeze verifies an ICICI-hosted login, OTP/MFA handling, a registered redirect URL, and a customer-specific session. It does not document:

- consent scopes;
- an authorization grant to a third-party company;
- standard authorization code/token exchange semantics;
- `state`, PKCE, refresh tokens, or revocation;
- one platform client authorizing unrelated customers;
- consumers using the flow without their own developer registration.

The documentation calls its mechanism OAuth 2.0, but the published flow is Breeze-specific. The term alone is not evidence of a platform OAuth product.

## G. Account Aggregator feasibility

### Technical feasibility

**VERIFIED for demat holdings through participating depositories.**

SEBI’s [August 2022 circular](https://www.sebi.gov.in/legal/circulars/aug-2022/participation-as-financial-information-providers-in-account-aggregator-framework_62157.html) permitted depositories to participate as FIPs in the AA ecosystem.

CDSL’s [official FIP page](https://www.cdslindia.com/Investors/FIP.html) states that:

- customers discover accounts using registered mobile number and PAN;
- they link a demat account/FI type using OTP;
- they approve a consent specifying validity, frequency, date range, purpose, and FI types;
- CDSL shares encrypted data through the AA to the FIU;
- summary data includes portfolio value and ISIN-wise holdings;
- transaction data is available for the requested period;
- supported FI types include equity, mutual funds, ETFs, IDRs, CIS, AIFs, InvITs, and REITs.

Sahamati’s [October 2024 adoption report](https://sahamati.org.in/wp-content/uploads/2024/11/AA-Adoption-Nos-as-of-Oct-24.pdf) reports both depositories live and explicitly lists equity shares/demat, ETFs, mutual funds, SIPs, AIFs, IDRs, CIS, and InvITs.

The AA route can therefore provide cross-broker demat ownership because the depository, rather than each broker, is the FIP. It does not automatically provide broker cash/funds, order state, intraday positions, margin, or cost basis. CDSL’s published summary is ISIN-wise holdings and portfolio value; purchase price/cost data is not established.

### Regulatory/commercial feasibility

**Not directly available to an unregulated software platform.**

Sahamati’s [joining guidance](https://sahamati.org.in/how-to-join-the-account-aggregator-network-to-share-and-access-financial-data/) and [FAQ](https://sahamati.org.in/faq/) state that only entities registered and regulated by RBI, SEBI, IRDAI, or PFRDA may be FIPs or FIUs.

Therefore the platform has two plausible routes:

1. become an appropriately regulated FIU after specialist legal analysis; or
2. partner contractually with an existing regulated FIU and an AA/TSP that supports the intended wealth-management/personal-finance purpose.

Whether a regulated partner can expose the data to this platform under its FIU purpose, retention policy, and customer contract is **UNVERIFIED** and must be established commercially and legally. An AA license alone is not a shortcut: an AA manages consent and transport and cannot act as the consuming FIU for an unregulated product.

Bank-account AA data is a separate FI type. It must not be treated as brokerage funds or settled broker cash.

## H. NSDL/CDSL/depository aggregation feasibility

### Consolidated Account Statement

CDSL’s [official CAS FAQ](https://www.cdslindia.com/cas/FAQ.html) states that CAS consolidates transactions and holdings across CDSL and NSDL demat accounts plus mutual-fund units held in statement-of-account form. NSDL’s [demat account holder guide](https://nsdl.co.in/downloadables/pdf/e-Guide%20for%20demat%20account%20holders%20-%20English.pdf) likewise describes CAS for all demat accounts and mutual-fund investments, aggregated using the first holder’s PAN.

SEBI’s [2025 CAS circular](https://www.sebi.gov.in/legal/circulars/feb-2025/revised-timelines-for-issuance-of-consolidated-account-statement-cas-by-depositories_91927.html) confirms CAS for all securities assets and its monthly/half-yearly dispatch schedule.

CAS is valuable evidence that cross-depository aggregation exists, but it is a periodic investor statement, not a published real-time third-party API. Automated email/PDF ingestion would create consent, identity, document-security, latency, and parsing concerns and should not be mistaken for an official API integration. Screen or credential scraping is rejected.

### Preferred depository mechanism

The Account Aggregator FIP route is more appropriate than CAS ingestion for a consent-driven production integration because it provides standardized API data, explicit consent, account discovery/linking, and ISIN-wise holdings. It still requires a regulated FIU relationship.

No public direct CDSL/NSDL API for an arbitrary unregulated portfolio application was found: **UNVERIFIED**.

## I. Zerodha comparison

The [Kite Connect introduction](https://kite.trade/docs/connect/v3/) and [login documentation](https://kite.trade/docs/connect/v3/user/) verify:

- an application receives an `api_key` and `api_secret` and registers a redirect URL;
- users are sent to Zerodha’s login;
- the callback returns a `request_token`;
- the platform exchanges it using its API secret for a per-user `access_token`;
- tokens normally expire at 06:00 the next day;
- approved platforms may receive long-standing read permissions;
- secrets belong on a backend for public applications.

The [postback documentation](https://kite.trade/docs/connect/v3/postbacks/) explicitly says public/platform apps can use a single API key for multiple users. Thus multi-user platform authorization is **SUPPORTED** at a high level; users do not each need a developer AppKey.

Published endpoint limits are in the [official exception reference](https://kite.trade/docs/connect/v3/exceptions/). It lists 1 request/second for quotes, 3/second for historical candles, 10/second for orders, and 10/second for other endpoints. Public documentation does not clearly define every limit’s multi-user scope. Zerodha’s official developer forum states limits are per API key/client for multi-user platforms, but commercial capacity/approval for 10,000 users remains **UNVERIFIED** and requires Zerodha engagement.

Suitability: authentication UX is structurally suitable; large-scale capacity and long-standing read access require platform approval/commercial confirmation.

## J. Upstox comparison

Upstox’s [official authentication guide](https://upstox.com/developer/api-documentation/authentication/) documents standard OAuth 2.0 authorization code flow:

- the app owns `client_id` and `client_secret`;
- Upstox hosts customer login;
- the callback returns a single-use code plus optional `state`;
- the backend exchanges it for a user access token;
- the application never handles the customer’s Upstox credentials.

The [profile API](https://upstox.com/developer/api-documentation/get-profile/) explicitly addresses businesses building multi-client applications. The [token response](https://upstox.com/developer/api-documentation/get-token/) also documents an extended token intended for prolonged read-only access.

The [official rate-limit page](https://upstox.com/developer/api-documentation/rate-limiting/) explicitly says limits are per API, per user. Standard holdings, positions, funds, and similar reads allow 50 requests/second, 500/minute, and 2,000/30 minutes per user.

Conclusion: the desired platform-owned application plus per-user authorization model is officially supported at a high level, and users do not need individual developer credentials. Exchange/app review, commercial terms, and 100,000-user operational approval remain **UNVERIFIED**.

## K. Angel One comparison

The [official SmartAPI documentation](https://smartapi.angelone.in/docs) documents two relevant flows:

- direct login API using client code, PIN, and TOTP; this is inappropriate for a third-party portfolio platform because it would handle brokerage authentication factors;
- a public Publisher login URL with platform API key, registered redirect URL, and `state`, returning user-specific auth/feed tokens after Angel-hosted login.

The public login page and terms acknowledge external third-party applications. The [app creation page](https://smartapi.angelone.in/create) offers Trading, Publisher, Historical Data, and Market Feeds app types and a registered redirect URL.

The [official rate-limit documentation](https://smartapi.angelone.in/docs) says limits are calculated by client code. It lists, among others, profile at 3/second and 1,000/hour and RMS/funds at 2/second. Thus published throttling is user/client-code scoped.

What remains **UNVERIFIED**:

- whether Publisher login grants holdings and funds reads or is limited to publisher/order use;
- whether one registered application may authorize 10,000 unrelated users for portfolio reads;
- partner review/commercial requirements;
- token lifetime/renewal for a read-only aggregator at scale.

Conclusion: Angel provides promising hosted multi-user login primitives, but suitability for 10,000-user read-only aggregation is **UNVERIFIED** pending written confirmation.

## L. 1k / 10k / 100k user scalability assessment

Assume one read cycle requires at least profile, holdings, and funds: three calls per user. Multiple daily cycles multiply that number. These calculations compare workload to published limits; they do not assert undocumented quota scope.

| Connected users | Calls for one cycle | Ordinary Breeze | Partner/AA path |
|---:|---:|---|---|
| 1,000 | 3,000 | **UNVERIFIED**. Fits 5,000/day only if the quota is shared and only one cycle is performed; two cycles would exceed it. Scope is unknown. | **COMMERCIAL AGREEMENT REQUIRED** |
| 10,000 | 30,000 | **NOT VIABLE if 5,000/day is platform/AppKey scoped; otherwise UNVERIFIED** | **COMMERCIAL AGREEMENT REQUIRED** |
| 100,000 | 300,000 | **NOT VIABLE if 5,000/day is platform/AppKey scoped; otherwise UNVERIFIED** | **COMMERCIAL AGREEMENT REQUIRED** |

At 100 calls/minute, a fully shared quota would take at least 30 minutes, 5 hours, and 50 hours respectively for one three-call cycle, ignoring retries and other traffic. The 5,000/day ceiling would be the harder constraint above roughly 1,666 users per one-cycle day. These are conditional capacity calculations, not proof that the quota is AppKey-scoped.

No public ICICI partner capacity was found, so none of the three scales can be called viable under a partner contract without negotiation and load/SLA commitments.

AA/depository architecture has production ecosystem evidence, but product-specific throughput, consent frequency, data-use purpose, retention, and partner pricing require regulated partner negotiation.

## M. Credential-storage comparison

| Model | UX | Security/storage | Scalability | Dependency | Suitability |
|---|---|---|---|---|---|
| A. Platform AppKey/secret + user authorization | Excellent | One high-value platform secret; per-user tokens | Excellent only with per-user/commercial quotas | Broker approval and documented delegation | Preferred broker-specific model, **UNVERIFIED for ICICI** |
| B. Each user supplies AppKey/secret | Poor | Platform stores thousands of developer secrets plus sessions | Quotas may isolate users, but operations/support are burdensome | Every user must register and manage an app | Optional expert/personal feature only |
| C. Partner/enterprise OAuth/consent | Best | Platform partner secret plus scoped/revocable per-user tokens | Designed for multi-tenancy if contract confirms | Commercial onboarding, SLA, compliance | Preferred ICICI-specific production path |
| D. AA through regulated partner | Good consent UX | Standardized encrypted consent/data flow; no broker credentials | Cross-institution architecture; partner capacity required | Regulated FIU/AA/TSP relationship | Preferred broad holdings aggregation candidate |
| E. Depository aggregation | Excellent coverage in principle | Avoids broker passwords/keys; ISIN-centric | One route across brokers | AA/FIU regulation or periodic CAS constraints | Strong for ownership, not broker funds/cost/trading state |

## N. Regulatory/commercial considerations

- The platform must not call itself an FIU or join the AA production network unless it is appropriately registered and regulated, or operating under a legally valid regulated-partner model.
- Consent purpose, frequency, retention, revocation, audit, encryption, and onward sharing must match the FIU’s regulated use case.
- Depository AA holdings do not establish purchase cost, broker cash, margin, intraday positions, or tax lots.
- A broker partner agreement must explicitly cover unrelated customers, read-only scopes, session/token handling, quota isolation, SLA, support, security review, data retention, incident handling, and termination/revocation.
- Existing trading-capable APIs must remain constrained to read-only platform capabilities regardless of what a commercial API permits.
- Static-IP/order rules are not a reason to enable trading and do not answer read-only scaling.
- Legal analysis is required before presenting portfolio analytics/advice based on AA data, especially if activity could constitute regulated investment advice, research, portfolio management, or another SEBI-regulated service.

## O. Recommended production architecture

Use two parallel discovery tracks, neither of which requires deleting Breeze:

1. **ICICI partner track**
   - Contact ICICI Securities with a written multi-tenant read-only use case.
   - Require a platform-owned partner client, ICICI-hosted customer authorization, user-scoped revocable tokens, documented quota scope, commercial capacity, and production SLA.
   - Adapt the existing `ICICIDirectConnector` behind a new official implementation if ICICI supplies a distinct contract.

2. **Indian holdings aggregation track**
   - Engage regulated FIUs/AA technology providers that already support CDSL and NSDL FI types.
   - Validate whether the platform’s personal-finance/wealth use case can lawfully operate through that FIU.
   - Normalize depository ISIN holdings through a separate consent-aggregator provider, not as fake ICICI broker data.

Do not make retail Breeze the default consumer connection flow. Do not ask ordinary users for developer credentials. Keep broker-specific connectors for metadata unavailable from depositories, but make depository/AA holdings a first-class potential source with explicit provenance.

## P. Existing retail Breeze connector classification

**OPTIONAL_BYO_CREDENTIALS**

Rationale:

- ordinary Breeze is officially available and functional for a client’s registered app;
- it is useful for DEV, personal use, and expert users who knowingly provide their own developer credentials;
- the public contract does not verify platform-wide multi-user authorization or quota isolation;
- classifying it `PRODUCTION_PRIMARY` would claim unverified permissions and capacity;
- `DEVELOPMENT_ONLY` is too restrictive because official retail use is real and supported;
- `NOT_SUITABLE` is too absolute because BYO/personal use remains legitimate.

The connector should remain intact and trading-disabled.

## Q. Recommended broker authentication-model abstraction

An explicit authentication model would improve provider selection, UX, secret policy, and scaling decisions. Recommended conceptual values:

| Model | Meaning |
|---|---|
| `LOCAL_GATEWAY` | User-operated/local broker gateway, such as IBKR Client Portal Gateway |
| `INDIVIDUAL_API_CREDENTIALS` | User owns developer AppKey/secret; platform stores/resolves them per connection |
| `PLATFORM_OAUTH` | Public platform application with broker-hosted login and per-user token |
| `PARTNER_OAUTH` | Contracted enterprise/partner application with delegated scopes, SLA, and commercial quotas |
| `CONSENT_AGGREGATOR` | Regulated consent network/AA flow rather than broker authentication |

These values should describe authentication/authorization provenance, not broker type. A provider may support more than one model. Future connection metadata should also distinguish credential owner (`USER`, `PLATFORM`, `PARTNER`), token scope, consent expiry, and data provenance. Do not implement these enums until product and commercial contracts are chosen.

## R. Questions requiring direct ICICI Securities contact

Send to `breezeapi@icicisecurities.com` and request routing to enterprise/fintech partnerships:

1. Is one Breeze AppKey permitted to authorize unrelated ICICI Direct customers?
2. Are 100/minute and 5,000/day enforced per AppKey, application, customer, account, session, IP, or combination?
3. Is there a partner/enterprise/B2B2C API contract for portfolio aggregation?
4. Can a partner use one platform client while customers authenticate only on ICICI-hosted pages?
5. Is explicit consent captured, and what scopes are available for Customer Details, Demat Holdings, and Funds?
6. Are authorization code, `state`, PKCE, refresh, revocation, logout, and webhook contracts available?
7. Are tokens independently scoped and revocable per customer and per platform connection?
8. What read quotas apply per platform and per customer at 1k, 10k, and 100k users?
9. Are higher quotas, batch reads, delta feeds, webhooks, or scheduled portfolio exports available?
10. What onboarding, security assessment, static egress IP, certification, audit, SLA, support, and commercial terms apply?
11. May the integration be strictly read-only with all order scopes disabled contractually?
12. Does ICICI participate as a broker/FIP for funds or positions beyond depository holdings in AA?

Until written answers arrive, each item is **UNVERIFIED**.

## S. Official source URLs

### ICICI Direct / ICICI Securities

- [Breeze API reference](https://api.icicidirect.com/breezeapi/documents/index.html)
- [Breeze portal](https://api.icicidirect.com/)
- [Breeze product page](https://www.icicidirect.com/futures-and-options/api/breeze)
- [Breeze rate-limit FAQ](https://www.icicidirect.com/faqs/fno/what-is-the-rate-limit-for-breeze-api)
- [Breeze support contact](https://www.icicidirect.com/faqs/fno/who-can-i-contact-for-queries-or-support)
- [Legacy official API registration document](https://api.icicidirect.com/apiuser/ICICIDirectAPIDOC.htm)
- [ICICI Securities corporate history](https://www.icicisecurities.com/wfrmAboutUs.aspx)
- [ICICI Securities FY2019–20 annual report](https://www.icicisecurities.com/Upload/ArticleAttachments/ICICI_Securities_Limited_Annual_Report_FY1920.pdf)
- [ICICI Securities FY2021–22 annual report](https://www.icicisecurities.com/Upload/ArticleAttachments/ICICI_Securities_Limited_Annual_Report_FY2021_22.pdf)
- [ICICI Securities Q1 FY2022 performance review](https://www.icicisecurities.com/Upload/ArticleAttachments/Performance_Review_Q1_FY2022.pdf)
- [Official Breeze Java SDK](https://github.com/Idirect-Tech/Breeze-Java-SDK)

### Account Aggregator / depositories / SEBI

- [SEBI: depositories as AA FIPs](https://www.sebi.gov.in/legal/circulars/aug-2022/participation-as-financial-information-providers-in-account-aggregator-framework_62157.html)
- [CDSL as FIP](https://www.cdslindia.com/Investors/FIP.html)
- [CDSL CAS FAQ](https://www.cdslindia.com/cas/FAQ.html)
- [NSDL demat account holder guide](https://nsdl.co.in/downloadables/pdf/e-Guide%20for%20demat%20account%20holders%20-%20English.pdf)
- [SEBI 2025 CAS timelines](https://www.sebi.gov.in/legal/circulars/feb-2025/revised-timelines-for-issuance-of-consolidated-account-statement-cas-by-depositories_91927.html)
- [Sahamati: joining the AA network](https://sahamati.org.in/how-to-join-the-account-aggregator-network-to-share-and-access-financial-data/)
- [Sahamati FAQ](https://sahamati.org.in/faq/)
- [Sahamati October 2024 adoption report](https://sahamati.org.in/wp-content/uploads/2024/11/AA-Adoption-Nos-as-of-Oct-24.pdf)
- [RBI Account Aggregator overview](https://rbi.org.in/scripts/PublicationsView.aspx?Id=18086)
- [Sahamati/ReBIT API specifications v2](https://sahamati.org.in/wp-content/uploads/2025/02/2-API_Specifications_v2.0.0.pdf)

### Comparison brokers

- [Zerodha Kite Connect introduction](https://kite.trade/docs/connect/v3/)
- [Zerodha login/session](https://kite.trade/docs/connect/v3/user/)
- [Zerodha rate limits](https://kite.trade/docs/connect/v3/exceptions/)
- [Zerodha public/platform postbacks](https://kite.trade/docs/connect/v3/postbacks/)
- [Upstox authentication](https://upstox.com/developer/api-documentation/authentication/)
- [Upstox token response](https://upstox.com/developer/api-documentation/get-token/)
- [Upstox multi-client profile API](https://upstox.com/developer/api-documentation/get-profile/)
- [Upstox rate limits](https://upstox.com/developer/api-documentation/rate-limiting/)
- [Angel One SmartAPI documentation](https://smartapi.angelone.in/docs)
- [Angel One SmartAPI app creation](https://smartapi.angelone.in/create)
- [Angel One Publisher login](https://smartapi.angelone.in/publisher-login/v2/login/)

## T. Consolidated unresolved items

The following remain **UNVERIFIED**:

1. Breeze rate-limit enforcement key.
2. Permission for one retail Breeze AppKey to authorize unrelated customers.
3. Corporate ownership of a retail AppKey independent of an ICICI customer.
4. Public availability of an ICICI partner/enterprise API.
5. ICICI partner authentication, consent, token, refresh, revocation, and logout contracts.
6. ICICI partner read scopes, quotas, batch/delta capability, SLA, pricing, and onboarding.
7. Whether the desired consumer redirect/consent UX is supported without user developer credentials.
8. Whether an existing regulated FIU can lawfully provide depository AA data to this product and under which purpose code.
9. NSDL’s current detailed FI-type payload and participating AA list equivalent to CDSL’s public page.
10. Direct depository API availability outside the AA/FIU framework.
11. AA/depository cost basis, broker cash, margin, and intraday-position availability; none is established by the cited holdings contract.
12. Zerodha commercial capacity and approved long-standing read access for 10k+ users.
13. Angel Publisher login authorization for holdings/funds and 10k-user platform approval.
14. Commercial terms and production capacity for all comparator brokers.

No source was stretched to resolve these questions by inference.

## Safety confirmation

- No production code was changed.
- No real credentials were used.
- No developer application was created.
- No authentication was performed.
- No live brokerage API call was made.
- No order, trade, or other brokerage action was performed.
