# Single-ISIN NSE ETF provisioning — local implementation only

No live provisioning, evidence ingestion, Radar cycle, image build/publication,
deployment or database migration is authorized by this document.

## Request and response

Existing ADMIN bearer authentication through API Gateway is required. The backend
uses the existing trusted identity resolver and ADMIN check; do not forge identity
headers from a client.

```http
POST /api/v1/market-universe/india/nse-etf/apply/INF204KB14I2
Authorization: Bearer <existing-admin-session>
Content-Length: 0
```

No body or query parameters. The URL selector must be one uppercase Indian ISIN
with valid length, characters and check digit. Arrays, comma-separated lists,
whitespace, lower case and invalid check digits are rejected before source I/O.
The client supplies no symbol, name, asset type or provider mapping.

Success (HTTP 200):

```json
{"status":"COMPLETED","isin":"INF204KB14I2","globalInstrumentId":"<canonical-uuid>","symbol":"NIFTYBEES","outcome":"VERIFIED"}
```

`VERIFIED` means either newly created or an identical already-verified identity.
It does not claim an insertion on retries. Existing incomplete/inactive/conflicting
identities are rejected, not repaired implicitly. HTTP 200 with `status=ABORTED`
and a fixed `outcome` means **not provisioned**, consistent with bulk business
failure semantics. No raw provider/SQL exception text is returned. Malformed
requests get 400; unauthenticated/non-ADMIN callers get 401/403; overlapping
admission in the same service process gets 409 with the existing
`NSE_ETF_UNIVERSE_APPLY_ALREADY_RUNNING` error.

The original bodyless `POST .../apply` retains bulk behavior and response shape.
Do not call it for single provisioning. Bodies or query selectors on either apply
route now fail explicitly with 400: a misplaced selector must never become bulk.

## Safety and scope

- The single operation never calls `bootstrapMissingEtfs()` or loops over writes.
- Every explicit request rereads both official sources. Shared
  `NseEtfListingValidation` checks the complete returned ETF list; the selected
  identity must occur exactly once, with no duplicate symbol or IV-series conflict.
- Exactly one call to `canonicalizeSelectedOfficialEtf` is possible. It reuses the
  existing canonicalizer and PostgreSQL transaction-scoped ISIN advisory lock.
  The database's existing unique ISIN/mapping constraints remain the final arbiter.
- The outer transaction includes creation and verification of the mapping. A
  postcondition failure rolls back both inserts. Matching existing ETF identity
  returns unchanged; existing Equity or conflicting identity is never reclassified.
- No legacy instrument attachment, holding, portfolio, broker, scheduler or Radar
  service is invoked. No app-user provisioning or schema change is added.
- Fixed audit log events include selected ISIN, outcome and canonical UUID on
  success. No fabricated creation count and no automatic retry or bulk fallback.
- Single and bulk share their existing per-process admission guard. Across pods,
  identical ISINs serialize at the existing database identity lock. Native
  PostgreSQL multi-pod concurrency is not empirically verified by the local tests.

## Verify NIFTYBEES immediately before separately approved provisioning

1. GET `/api/v1/market-universe/india/nse-etf/preview` using the existing ADMIN
   session. Require `sourceAvailable=true` and `ivSeriesSourceAvailable=true`.
2. Confirm exact ISIN `INF204KB14I2` and symbol `NIFTYBEES` in `missingEtfs`,
   absent from `equityToEtfCandidates`, `authoritativeConflicts`, `malformed` and
   `conflictVerificationIncomplete`. For a retry, an already-classified entry is
   not enough: independently verify the exact ACTIVE ETF and VERIFIED NSE mapping.
3. Confirm the official sources are still the configured NSE ETF list
   `https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv` and general
   security master `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`.
   The latter is only read for IV conflict detection; no equity synchronization runs.
4. Review the exact indexed reads below and capture their small result sets.
   Stop on unexpected records. Preview is advisory; POST verifies fresh sources
   again and rechecks identity inside its own transaction.

Read-only SQL for an independently approved verification session:

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '3s';
SET LOCAL lock_timeout = '500ms';
SELECT instrument_id, isin, normalized_isin, asset_type, status,
       primary_exchange, primary_symbol, currency, country
FROM portfolio.instrument_master
WHERE normalized_isin = 'INF204KB14I2'
   OR (primary_exchange = 'NSE' AND primary_symbol = 'NIFTYBEES')
LIMIT 5;
SELECT instrument_id, provider, provider_symbol, provider_instrument_id,
       exchange, currency, status, resolution_source, verified_at
FROM portfolio.instrument_provider_mappings
WHERE provider = 'NSE' AND exchange = 'NSE' AND provider_symbol = 'NIFTYBEES'
LIMIT 5;
SELECT instrument_id, master_instrument_id
FROM portfolio.instruments WHERE isin = 'INF204KB14I2' LIMIT 5;
ROLLBACK;
```

For the currently missing identity, expected write set is one
`portfolio.instrument_master` (ETF, ACTIVE, NSE, NIFTYBEES, INR, IN, exact normalized
ISIN) plus one `portfolio.instrument_provider_mappings` referencing that UUID
(NSE, NIFTYBEES, provider ID INF204KB14I2, VERIFIED, nonnull verified timestamp,
source NSE_OFFICIAL_ETF_BOOTSTRAP). No legacy `instruments` row is created.
Repeat the same reads afterward; confirm one exact pair and the returned UUID.
Confirm authenticated `GET /api/v1/instruments?status=ACTIVE&assetType=ETF` includes
that UUID with verified mapping. Provisioning alone does not supply research facts.

## Separate approval checkpoints

1. **Artifact and deployment:** review code and tests, separately authorize image
   build/publication, record a verified registry manifest digest for the new
   portfolio-service image and the previous rollback image. Deploy only that
   container using a UID/resource-version-checked change, preserving all other
   configuration. Verify no pending migrations before rollout; no migration is
   part of this change. Do not enable the ETF manual gate or alter research-engine.
   No digest is provided here because no image was built or published.
2. **Live provisioning:** approve only the bodyless NIFTYBEES POST above after
   fresh preview and targeted identity verification. Inspect status/outcome and
   resulting records; stop on failure. Do not invoke bulk or retry automatically.
3. **Evidence acquisition:** separately approve one-UUID bounded NSE acquisition
   into research evidence tables; preserve provider limits and require fresh
   identity, market price and volume readiness. No broad universe ingestion.
4. **ETF execution:** separately approve the research-engine artifact/rollout and
   dedicated manual gate, keeping global recovery paused and scheduler disabled.
   Require controlled preflight READY, then one explicit controlled cycle and
   verify its exact UUID, evaluated candidate and durable result. A 202 or empty
   recommendations alone is not evidence of meaningful processing.

Rollback is workload-only: restore the reviewed previous portfolio-service
manifest digest after explicit approval. Preserve any committed canonical ETF
records and mapping; never delete data or undo holdings as rollback. A lost HTTP
response may follow a committed transaction: inspect exact identity before any
retry. Each provisioning transaction is atomic; bulk behavior is unchanged.

## Local validation

PowerShell 5.1, existing Java 17/Maven, offline dependency cache:

```powershell
mvn -o -f services/pom.xml -pl portfolio-service -am '-Dtest=NseEtfSingleProvisioningTest,NseEtfSinglePersistenceTest,NseEtfUniverseControllerTest,NseEtfUniverseApplyServiceTest,NseEtfUniverseBootstrapServiceTest,NseEtfUniversePreviewServiceTest,OfficialNseEtfSecurityListClientTest,InstrumentMasterServiceTest' '-Dsurefire.failIfNoSpecifiedTests=false' test -q
git diff --check
```

Tests use mocks and an automatically replaced embedded H2 datasource. The JPA
slice uses only test configuration, disables Flyway and creates an ephemeral
Hibernate test schema. No external PostgreSQL connection or provider GET occurs.
H2 tests verify a real master/mapping pair, idempotent UUID, rollback of both
inserts on failure and byte-for-field unchanged seeded portfolio/position/legacy
instrument/equity rows. PostgreSQL lock ordering is checked with mocks; existing
PostgreSQL constraints and cross-process locking are not reproduced by H2.
