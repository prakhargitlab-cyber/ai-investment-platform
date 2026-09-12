# Interactive Brokers Provider

## CURRENT STATE

The repository contains an `IBKRBrokerProvider` adapter for a read-only Interactive Brokers foundation. The intended account structure is an Individual / retail IBKR account.

Real integration status:

- Real IBKR login attempted: NO
- Real IBKR connection used: NO
- Real trades executed: NO
- Real account, position, cash, or market-data response validated against IBKR: NO

## INDIVIDUAL ACCOUNT AUTH METHOD

Official IBKR documentation identifies Client Portal Gateway as the authentication path for retail and individual clients. The user authenticates directly with Interactive Brokers through the gateway login UI. The platform must never collect or store IBKR username, password, MFA, browser cookies, or raw session tokens.

Official source:

- https://www.interactivebrokers.com/docs/web-api/getting-started
- https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/installation-authentication
- https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/request-requirements
- https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/limitations-of-the-client-portal-gateway
- https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/client-portal-gateway-faq

Implementation target for Individual accounts:

- Auth method: `client-portal-gateway`
- Broker-service talks to a broker connector endpoint, not directly to the gateway host
- Browser login: required
- Server-side login automation: not supported by IBKR
- Daily reauthentication: required

## GLOBAL CONNECTOR ARCHITECTURE

The platform is a multi-user SaaS system. A single Client Portal Gateway session must never be shared between users.

The broker path is:

Authenticated user -> broker connection -> broker service -> broker connector abstraction -> user-specific IBKR connector runtime -> IBKR Client Portal Gateway -> Interactive Brokers.

`BrokerProvider` is business-facing and broker-neutral. It exposes read-only capabilities and provider status. IBKR HTTP/gateway details live behind the `BrokerConnector` contract.

Connector metadata is persisted as safe metadata only:

- `connector_id`
- `user_id`
- `broker_type`
- `runtime_mode`
- `runtime_status`
- `auth_status`
- heartbeat/auth timestamps
- idle/session timeout settings

The connector schema does not persist broker passwords, MFA values, raw cookies, raw session tokens, or browser secrets. If future runtime implementations need secret material, they must use opaque secret references owned by the authenticated user.

## RUNTIME MODES

The connector abstraction supports:

- `KUBERNETES`: platform-managed connector runtime.
- `LOCAL_AGENT`: user-device or local connector runtime exposing the same internal connector contract.

Broker-service must not care where the connector runs. It communicates with the configured connector service endpoint and uses authenticated user ownership checks for connector status, login, sync, and snapshot access.

IBKR states that Client Portal Gateway is intended to run locally. Therefore, this repository separates architecture capability from production deployment approval. The architecture can support Kubernetes-managed or local-agent connector runtimes, but AKS-hosted Client Portal Gateway is not marked production-approved until IBKR confirms that deployment/location model is acceptable for a global SaaS product.

## INTERACTIVE AUTHENTICATION

The flow is:

1. User clicks Connect IBKR.
2. Broker-service creates a user-owned connector instance and broker connection.
3. The connector returns an authentication URL/state.
4. The user authenticates directly with IBKR.
5. The connector checks `GET /iserver/auth/status`.
6. The broker connection becomes `CONNECTED` only when `authenticated=true` and `connected=true`.

The application never asks for or stores IBKR password/MFA data.

## OAUTH 2.0 RESTRICTION

OAuth 2.0 is not the production authentication model for this Individual IBKR integration.

Official IBKR documentation states OAuth 2.0 is for licensed Organizations, Financial Advisors, and IBrokers. It is not available to Individual account structures.

Official source:

- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-2/introduction

The platform must not request or configure OAuth2 client IDs, client key IDs, private keys, public-key registration metadata, or OAuth2 secrets for the Individual IBKR path.

## OAUTH 1.0A STATUS

OAuth 1.0a is documented by IBKR for licensed Financial Advisors, Organizations, IBrokers, and third-party services. It can access IBKR endpoints directly without the local Client Portal Gateway after registration and token/session setup.

Official sources:

- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/introduction
- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/request-requirements
- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/first-party-oauth/registration-process
- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/first-party-oauth/first-party-o-auth-workflow
- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/lst/obtain-live-session-token-signature
- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/lst/compute-live-session-token
- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/lst/validate-live-session-token
- https://www.interactivebrokers.com/docs/web-api/authentication/oauth-1a/authenticated-requests

OAuth 1.0a is not selected for the Individual account implementation until IBKR eligibility and registration approval requirements are confirmed for this specific account structure and use case.

## SESSION REQUIREMENTS

IBKR Web API sessions are two-tiered:

- An outer read-only session required to make Web API requests.
- A brokerage session required for `/iserver` endpoints, market data, and trading-capable functionality.

Official source:

- https://www.interactivebrokers.com/docs/web-api/authentication/sessions

Client Portal Gateway requirements:

- User must log in through a browser on the same machine as the Client Portal Gateway.
- API calls must be made from the same machine where the gateway was authenticated.
- IBKR does not support automated Client Portal Gateway authentication.
- Clients must reauthenticate daily.
- Sessions can time out without regular requests or `/tickle`.
- `/iserver/auth/status` reports session authentication state.
- `/iserver/auth/ssodh/init` initializes brokerage session when needed.

Official sources:

- https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/limitations-of-the-client-portal-gateway
- https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/client-portal-gateway-faq
- https://www.interactivebrokers.com/docs/web-api/authentication/faq
- https://www.interactivebrokers.com/docs/web-api/v1/endpoints/session/initialize-brokerage-session

## VERIFIED READ-ONLY ENDPOINTS

### Brokerage Session Status

- Method: `GET`
- Path: `/iserver/auth/status`
- Purpose: determine current authentication status
- Required auth: authenticated Client Portal Gateway session
- Response shape: includes `authenticated`, `connected`, `competing`, `established`, and message/status fields
- Verification source: https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-session/get-brokerage-status

### Accounts

- Method: `GET`
- Path: `/portfolio/accounts`
- Purpose: list accounts for which the user can view position and account information
- Required auth: authenticated Web API session
- Response shape: account objects including `id`, `accountId`, `displayName`, `accountAlias`, `currency`, and metadata
- Verification source: https://www.interactivebrokers.com/docs/web-api/trading/portfolio-and-positions/querying-your-accounts

### Positions

- Method: `GET`
- Path: `/portfolio/{accountId}/positions/{pageId}`
- Purpose: list positions for the given account
- Required auth: authenticated Web API session; `/portfolio/accounts` or `/portfolio/subaccounts` must be called first
- Pagination: page ID starts at 0; each page returns up to 100 positions
- Response shape: position objects including `acctId`, `conid`, `contractDesc`, `position`, `mktPrice`, `avgCost`, `currency`, `listingExchange`, `assetClass`, and `ticker`
- Verification source: https://www.interactivebrokers.com/docs/web-api/v1/endpoints/portfolio/positions

### Cash / Currency Balances

- Method: `GET`
- Path: `/portfolio/{accountId}/ledger`
- Purpose: retrieve cash balances by currency
- Required auth: authenticated Web API session; `/portfolio/accounts` or `/portfolio/subaccounts` must be called first
- Response shape: object keyed by currency, including `cashbalance`, `currency`, `settledcash`, `netliquidationvalue`, and other ledger values
- Verification source: https://www.interactivebrokers.com/docs/web-api/trading/portfolio-and-positions/querying-currency-balances

### Market Data

- Method: `GET`
- Path: `/iserver/marketdata/snapshot`
- Purpose: retrieve top-of-book market data snapshots for conids and requested field tags
- Required auth: authenticated brokerage session and relevant market data permissions
- Response shape: array of market data objects keyed by numeric field tags plus fields such as `conid`, `conidEx`, `_updated`, and `server_id`
- Entitlement requirements: carried by the IBKR username
- Verification source: https://www.interactivebrokers.com/docs/web-api/trading/market-data/top-of-book-snapshots

Market data remains disabled in this platform unless explicitly enabled by verified entitlement and configuration.

## READ-ONLY CAPABILITIES

When disabled, documentation-unverified, OAuth2-configured, or missing connector configuration, IBKR advertises no capabilities.

When explicitly enabled with the verified Individual-account Client Portal Gateway method, the current code may advertise:

- `ACCOUNTS_READ`
- `ACCOUNT_METADATA_READ`
- `PORTFOLIO_READ`
- `POSITIONS_READ`
- `CASH_READ`

`MARKET_DATA_READ` is not enabled by default.

## NOT SUPPORTED

- Order placement
- Order modification
- Order cancellation
- Buy/sell actions
- Trade execution
- Browser login automation
- Stored broker usernames or passwords
- Stored OTP, MFA, CAPTCHA, raw access-token, OAuth2 private-key, or OAuth2 client secret values
- Shared IBKR Gateway sessions across users
- Broker-service direct dependency on localhost, host machine aliases, Pod IPs, or Node IPs

## MULTI-USER IMPLICATIONS

Each authenticated application user owns their own broker connection and connector instance. Client Portal Gateway has machine/session constraints:

- A gateway session is authenticated by one IBKR username.
- A single IBKR username can have only one brokerage session active at a time.
- API calls must originate on the same machine as the authenticated gateway.
- Multiple application users are isolated at the application data and connector layers, and each user's real IBKR connectivity requires a separate authenticated gateway/session boundary.

The platform must not share one user's IBKR gateway session with another application user.

## CURRENT PROVIDER STATUS

- Default: `NOT_CONFIGURED`
- Enabled without official documentation verification: `DOCUMENTATION_REQUIRED`
- Enabled with OAuth2/private_key_jwt: `DOCUMENTATION_REQUIRED` for Individual account integration
- Enabled with Client Portal Gateway but missing connector endpoint: `NOT_CONFIGURED`
- Enabled with Client Portal Gateway but unauthenticated session: `AUTHENTICATION_REQUIRED`
- Real provider enabled for real use: NO

## REQUIRED BEFORE REAL CREDENTIAL USE

Before real IBKR credentials or sessions can be configured:

1. Complete the connector runtime implementation for either `KUBERNETES`, `LOCAL_AGENT`, or both.
2. Ensure no server-side browser automation is used.
3. Define how users will perform interactive browser login and daily reauthentication.
4. Define how the platform detects session timeout and prompts reauth safely.
5. Verify read-only scopes/capabilities in a controlled non-trading test plan.
6. Keep PRD Individual IBKR runtime disabled until IBKR approves the production connector runtime location for this SaaS use case.

## PACKAGE AND LICENSING

Do not commit the IBKR Gateway ZIP/package or derivative redistribution artifact to source control unless redistribution rights are verified. DEV should use an external/local artifact path or an approved image build process. PRD must treat IBKR Gateway packaging as a deployment prerequisite, not an assumed right.

## PHASE 5B DEV REAL PACKAGE VALIDATION

This validation uses the user's locally downloaded and extracted official Client Portal Gateway package. The package path is supplied at execution time only and must not be committed or baked into any image.

Package checks:

```powershell
$IbkrPackage = "<LOCAL_EXTRACTED_CLIENT_PORTAL_GATEWAY_PATH>"
Test-Path "$IbkrPackage\bin\run.sh"
Test-Path "$IbkrPackage\root\conf.yaml"
Get-ChildItem "$IbkrPackage" | Select-Object -First 20
```

DEV package injection uses a Kubernetes PVC mounted into the `ibkr-connector` pod:

```powershell
kubectl create namespace ai-investment --dry-run=client -o yaml | kubectl apply -f -
@'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ibkr-gateway-package-dev
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
'@ | kubectl apply -n ai-investment -f -
kubectl run ibkr-package-loader -n ai-investment --image=busybox:1.36 --restart=Never --command -- sh -c "mkdir -p /package && sleep 3600" --overrides '{"spec":{"containers":[{"name":"ibkr-package-loader","image":"busybox:1.36","command":["sh","-c","mkdir -p /package && sleep 3600"],"volumeMounts":[{"name":"pkg","mountPath":"/package"}]}],"volumes":[{"name":"pkg","persistentVolumeClaim":{"claimName":"ibkr-gateway-package-dev"}}]}}'
kubectl wait -n ai-investment --for=condition=Ready pod/ibkr-package-loader --timeout=120s
kubectl cp "$IbkrPackage\." ai-investment/ibkr-package-loader:/package
kubectl delete pod ibkr-package-loader -n ai-investment
```

Deploy DEV with the external package mounted. The Gateway remains inside the connector pod and broker-service talks only to `http://ibkr-connector`:

```powershell
helm upgrade --install ai-investment-platform infrastructure/helm/ai-investment-platform `
  --namespace ai-investment --create-namespace `
  --values infrastructure/helm/ai-investment-platform/values-dev.yaml `
  --set ibkr.connectorBaseUrl=http://ibkr-connector `
  --set ibkr.connectorInternalToken=dev-internal-connector-token-change-me `
  --set ibkrConnector.gatewayPackagePath=/opt/ibkr/clientportal.gw `
  --set ibkrConnector.gatewayBaseUrl=https://127.0.0.1:5000/v1/api `
  --set ibkrConnector.gatewayTlsVerify=false `
  --set ibkrConnector.loginPublicBaseUrl=http://localhost:18080 `
  --set ibkrConnector.packageVolumeClaim=ibkr-gateway-package-dev
```

Initial connector validation:

```powershell
kubectl rollout status deployment/ibkr-connector -n ai-investment --timeout=300s
kubectl exec -n ai-investment deploy/ibkr-connector -- sh -c "ps -ef | grep '[b]in/run.sh'"
kubectl exec -n ai-investment deploy/ibkr-connector -- sh -c "python - <<'PY'
import socket
s=socket.create_connection(('127.0.0.1',5000),5)
s.close()
print('port 5000 listening')
PY"
```

The user-facing login URL must be obtained through broker-service or the API gateway, not by browsing to a Pod IP or Kubernetes service DNS. For local DEV ingress the URL should start with:

```text
http://localhost:18080/connector-sessions/
```

Current limitation: this DEV runtime supports one active IBKR session per connector pod. Dynamic per-user Kubernetes pod orchestration is not implemented yet.
