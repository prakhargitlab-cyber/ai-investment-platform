# ICICI Direct controlled DEV read-only validation

This runbook prepares and performs the first operator-controlled Breeze DEV authentication. It must not be used for orders, portfolio import, automated OTP handling, or unattended authentication.

## Preconditions

- Use an authenticated DEV application user and the API gateway URL.
- Obtain the registered Breeze AppKey, secret key, and registered redirect URL outside Git and chat tooling.
- Never enter the ICICI password or OTP anywhere except the official ICICI/Breeze page.
- Do not run the portfolio import endpoint during this procedure.

## 1. Create the Kubernetes credential Secret without command-line literals

Run in an interactive PowerShell window. Values are read without echo, converted only in memory, and sent to Kubernetes over stdin. The command prints only Kubernetes resource status.

```powershell
$Namespace = '<dev-namespace>'
$CredentialSecretName = 'aip-icici-direct-dev'
$AppKeySecure = Read-Host 'Breeze AppKey' -AsSecureString
$SecretKeySecure = Read-Host 'Breeze secret key' -AsSecureString
$appKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AppKeySecure)
$secretKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecretKeySecure)
try {
    $appKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($appKeyPointer)
    $secretKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretKeyPointer)
    $secretManifest = @{
        apiVersion = 'v1'
        kind = 'Secret'
        metadata = @{ name = $CredentialSecretName; namespace = $Namespace }
        type = 'Opaque'
        data = @{
            'app-key' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($appKeyPlain))
            'secret-key' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($secretKeyPlain))
        }
    } | ConvertTo-Json -Depth 6 -Compress
    $secretManifest | kubectl apply -f -
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($appKeyPointer)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretKeyPointer)
    Remove-Variable appKeyPlain, secretKeyPlain, secretManifest, AppKeySecure, SecretKeySecure -ErrorAction SilentlyContinue
}
```

Do not use `kubectl get secret ... -o yaml/json`, `kubectl describe secret`, or shell command-line literals containing either credential.

## 2. Deploy broker-service configuration

Create an uncommitted local Helm override containing no credentials:

```yaml
iciciDirect:
  enabled: true
  officialDocumentationVerified: true
  redirectUrl: "<exact redirect URL registered for the Breeze app>"
  credentialSecretName: aip-icici-direct-dev
```

The chart supplies these verified constants:

- base URL: `https://api.icicidirect.com/breezeapi/api/v1/`
- login URL: `https://api.icicidirect.com/apiuser/login`
- authentication method: `breeze-interactive-session`
- secret reference: `ICICI_DIRECT_SECRET_KEY`

Deploy using the normal DEV Helm upgrade with the repository values, DEV values, and the uncommitted override. Delete the local override afterward. Do not change any `ibkr` values.

After rollout, confirm only pod readiness. Do not dump the pod environment. Before a session is attached, ICICI must report `AUTHENTICATION_REQUIRED`, never `CONNECTED`.

## 3. Obtain a DEV application access token

Keep the response and token in variables so PowerShell does not render them:

```powershell
$ApiBase = 'http://localhost:13000'
$devLogin = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/auth/dev/login" `
    -ContentType 'application/json' -Body '{"userKey":"user-a"}'
$AuthorizationHeaders = @{ Authorization = "Bearer $($devLogin.accessToken)" }
```

Do not print `$devLogin`, `$devLogin.accessToken`, or `$AuthorizationHeaders`.

## 4. Create the scoped ICICI connection

```powershell
$connection = Invoke-RestMethod -Method Post `
    -Uri "$ApiBase/api/v1/broker-connections/ICICI_DIRECT/connect" `
    -Headers $AuthorizationHeaders
$ConnectionId = $connection.connectionId
if ($connection.status -ne 'AUTHENTICATION_REQUIRED') { throw 'Unexpected pre-authentication state' }
```

Reuse an existing ICICI connection owned by the same DEV user instead if appropriate.

## 5. Perform interactive Breeze authentication

Initiate the connection-scoped login:

```powershell
$login = Invoke-RestMethod -Method Get `
    -Uri "$ApiBase/api/v1/broker-connections/$ConnectionId/icici-login" `
    -Headers $AuthorizationHeaders
$login.loginUrl
```

Verify that the URL host is exactly `api.icicidirect.com`, then open it manually. Complete username/password and OTP only on that official page. Obtain `API_Session` according to the verified Breeze flow; do not paste it into a command line or save it in a file.

Attach it without terminal echo or command-history disclosure:

```powershell
$ApiSessionSecure = Read-Host 'Breeze API_Session' -AsSecureString
$apiSessionPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ApiSessionSecure)
try {
    $apiSessionPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiSessionPointer)
    $sessionBody = @{ apiSession = $apiSessionPlain } | ConvertTo-Json -Compress
    $attached = Invoke-RestMethod -Method Post `
        -Uri "$ApiBase/api/v1/broker-connections/$ConnectionId/icici-session" `
        -Headers $AuthorizationHeaders -ContentType 'application/json' -Body $sessionBody
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiSessionPointer)
    Remove-Variable apiSessionPlain, sessionBody, ApiSessionSecure -ErrorAction SilentlyContinue
}
if ($attached.status -ne 'CONNECTED') { throw 'Breeze session did not become CONNECTED' }
```

Do not print `$attached` wholesale. It must not contain session material.

## 6. Validate reads in order

### Account / Customer Details

```powershell
$accounts = @(Invoke-RestMethod -Method Get `
    -Uri "$ApiBase/api/v1/broker-connections/$ConnectionId/accounts" `
    -Headers $AuthorizationHeaders)
$accounts | Select-Object brokerType, status, baseCurrency
```

Expected: HTTP 200, one normalized `ICICI_DIRECT` account, `ACTIVE`, and `baseCurrency` null. Do not print display name or the masked external reference unnecessarily.

### Demat Holdings

```powershell
$holdings = @(Invoke-RestMethod -Method Get `
    -Uri "$ApiBase/api/v1/broker-connections/$ConnectionId/demat-holdings" `
    -Headers $AuthorizationHeaders)
$invalidIdentity = @($holdings | Where-Object { $_.instrument.providerInstrumentId -notmatch '^ISIN:[A-Z]{2}[A-Z0-9]{9}[0-9]$' })
$fabricated = @($holdings | Where-Object {
    $null -ne $_.instrument.exchange -or $null -ne $_.instrument.tradingCurrency -or
    $null -ne $_.averageCost -or $null -ne $_.currentPrice -or
    $null -ne $_.marketValue -or $null -ne $_.unrealizedProfitLoss
})
[pscustomobject]@{
    holdingCount = $holdings.Count
    invalidIdentityCount = $invalidIdentity.Count
    fabricatedValuationCount = $fabricated.Count
}
```

Expected: HTTP 200; both error counts zero. Compare quantities privately against broker-visible delivery holdings. Do not publish the complete holdings list.

### Funds

```powershell
$funds = @(Invoke-RestMethod -Method Get `
    -Uri "$ApiBase/api/v1/broker-connections/$ConnectionId/funds" `
    -Headers $AuthorizationHeaders)
$funds | Select-Object source, @{Name='currency';Expression={$_.cash.currency}}, `
    @{Name='amountPresent';Expression={$null -ne $_.cash.amount}}, `
    @{Name='settledCashAbsent';Expression={$null -eq $_.settledCash}}
```

Expected: HTTP 200, source `BREEZE_FUNDS_TOTAL_BANK_BALANCE`, currency `INR`, amount present, and settled cash absent. Inspect the amount locally only; do not label it settled, withdrawable, or buying power.

### Provider capabilities

```powershell
$providers = @(Invoke-RestMethod -Method Get -Uri "$ApiBase/api/v1/brokers" -Headers $AuthorizationHeaders)
$icici = $providers | Where-Object brokerType -eq 'ICICI_DIRECT'
$icici | Select-Object status, providerStatus, capabilities, readOnly
```

Expected connected capabilities: `ACCOUNTS_READ`, `ACCOUNT_METADATA_READ`, `POSITIONS_READ`, and `CASH_READ`. `PORTFOLIO_READ` and `ORDER_EXECUTION` must be absent; `readOnly` must be true.

## 7. Stop gate

Stop after the four validations. Do not call portfolio import, broker sync as a substitute for import, order APIs, Portfolio Holdings, Portfolio Positions, or market-data APIs.

Current ICICI Demat snapshots are not safe for Phase 5C import: account base currency, instrument exchange/trading currency, average cost, and current price are absent, while the import path and persistence schema require them.

At the end of the operator session, remove sensitive PowerShell variables:

```powershell
Remove-Variable devLogin, AuthorizationHeaders, login, attached, accounts, holdings, funds, providers, icici -ErrorAction SilentlyContinue
```
