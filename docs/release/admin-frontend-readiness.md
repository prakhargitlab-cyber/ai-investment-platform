# ADMIN frontend release readiness — 2026-10-10

**GO for source review; NO-GO for deployment or bootstrap.** Image builds/pushes, live V5 migration, Kubernetes changes, and role changes need separate approval. None were performed. All edits and validation artifacts are in `C:\workspace\ai-investment-platform-admin-release`; the original worktree was not modified.

This patch builds on `b64f297` (CLI profile disables Flyway), `77281b2` (isolated ADMIN snapshot), and baseline `1f42a14`. Use this document's containing commit as the corrected frontend release source. Dependencies and Dockerfile are unchanged.

## Findings

- The navigation label contained `U+0393 U+00E5 U+00C6` instead of a right arrow, consistent with decoding UTF-8 bytes using a different code page. The existing uncommitted correction is now an ASCII JSX entity, `&rarr;`. The production browser test locates the exact rendered `Administration → Users & Roles` label.
- Edge measured the actual deployment at `http://localhost:18080` as a secure context with `crypto.randomUUID`, and at `http://192.168.178.178:18080` as an insecure context without it. Both returned HTTP 200 and exposed `crypto.getRandomValues`. Only the document was allowed through during this capability probe; application assets/API calls were blocked.
- The shared API client's `createCorrelationId` now uses native UUID generation when available, otherwise generates UUID v4 from 16 cryptographically random bytes with version/variant bits set. It fails explicitly if secure randomness is unavailable. No weak random fallback, dependency, authentication change, or Playwright polyfill was introduced. This does not encrypt HTTP; use HTTPS outside the controlled local environment. [MDN randomUUID](https://developer.mozilla.org/en-US/docs/Web/API/Crypto/randomUUID), [MDN getRandomValues](https://developer.mozilla.org/en-US/docs/Web/API/Crypto/getRandomValues).
- Browser tests take `ADMIN_TEST_BASE_URL`; `ADMIN_TEST_EXPECT_INSECURE=true` requires native UUID support to be absent. Every mocked API call must carry a UUID v4 correlation header; ADMIN requests also require the fixture bearer header. Tests cover production assets, Unicode navigation, USER invisibility, search/details, required reason, cancelled/confirmed changes, audit display, and 403 cleanup. Every API call, including background workspace calls, is mocked.
- Seeded authenticated localStorage still causes the existing server/client hydration mismatch: one React #418 per scenario. The harness tolerates that specific mismatch and rejects other page errors. This is a documented limitation, not a clean-console claim.

## Results

| Check | Result |
|---|---|
| TypeScript `tsc --noEmit` | Exit 0 |
| Production `npm run build`, Next 16.3.0 | Exit 0; standalone server emitted |
| New UUID unit tests | 4 passed |
| Targeted backend | 28 passed; 0 failures/errors/skips |
| Current CRLF frontend suite | 282 total: 273 passed, 9 baseline failures |
| Baseline `1f42a14`, matching CRLF/dependencies | 278 total: 269 passed, identical 9 failures |
| Baseline LF scratch copy | 278 passed, 0 failed |
| Corrected LF scratch copy | 282 passed, 0 failed |
| Production USER + ADMIN, loopback HTTP | 2 scenarios passed; native UUID present |
| Production USER + ADMIN, LAN HTTP | 2 scenarios passed; native UUID absent, application fallback used |
| Standalone health | HTTP 200, `status=ok`, both origins |
| Production static assets | 9 asset requests per scenario, all HTTP 200; JS and CSS required |
| Bootstrap template | Locally parsed; suspension, placeholders, no retry, Flyway disabled verified |

Backend breakdown: AdminUserIntegrationTest 9, RoleAdminServiceIntegrationTest 6, RoleAdminCliRunnerTest 2, AuthLifecycleIntegrationTest 6, GatewayAuthenticationFilterTest 4, AdminAuthProxyTest 1. These ran with test configuration and in-memory H2, not the live database. The prior 18 disposable PostgreSQL verification tests were **not rerun** in this frontend task.

Local evidence is in ignored `artifacts/`: `frontend-build.log`, `frontend-build-result.json`, `frontend-current.tap.log`, `frontend-baseline.tap.log`, `frontend-current-lf.tap.log`, `frontend-baseline-lf.tap.log`, `backend-targeted.log`, `browser-loopback.log`, `browser-lan.log`, and `npm-audit.json`. The pre-existing untracked `frontend/tests_output.txt` was left untouched and excluded from the commit.

## Nine baseline failures

These source-text tests use multiline regex boundaries with literal LF (`\n`) without accommodating CRLF. Empty captures then fail content assertions. Baseline and release failure-name sets match exactly. Normalizing only scratch copies of `investment-workspace.tsx` and `verify-email/page.tsx` to LF removes all nine without changing logic or assertions. This demonstrates test portability defects, not nine established functional defects.

| Test file | Failing test names |
|---|---|
| `auth-close4-verify-ui.test.mjs` | verification success is an exclusive public confirmation with a scheduled sign-in redirect; invalid verification links expose only recovery actions; logout clears authenticated state and returns to the canonical login route |
| `ibkr-portfolio-bootstrap.test.mjs` | IBKR polling bootstraps once on authenticated CONNECTED and never on page load; bootstrap refreshes portfolios and selects only a returned or connection-linked portfolio; bootstrap failure is sanitized, leaves authentication state untouched, and does not sync non-IBKR auth outcomes |
| `phase5e-ui.test.mjs` | refresh eligibility is based on canonical global identity, not a portfolio-summary row or existing research; Talbros/Ujjivan-shaped global selections are refreshable while unresolved and unsupported selections are not; portfolio detail and research-table drawers join global research to a different local holding ID |

Reproduction: archive `1f42a14` into a scratch directory under `artifacts`, use the same installed dependencies through a junction in that scratch frontend, match CRLF in the two source files, then run `node --test --test-reporter=tap tests/*.test.mjs`. Repeat after normalizing those two scratch files to LF. The corrected scratch copy uses isolated HEAD plus the changed frontend files. No original worktree files are involved.

## Packaging and verification commands

`next.config.mjs` uses `output: "standalone"`. The Dockerfile correctly copies `.next/standalone` to `/app`, `.next/static` to `/app/.next/static`, and `public` to `/app/public`; starts `node server.js` as UID/GID 1000; and binds port 3000 on 0.0.0.0. Installed Next documentation confirms static/public need separate copies. No Dockerfile fix was necessary.

Validation copied these three outputs into `artifacts/frontend-runtime` and ran its traced server, not `next start`. This verifies packaging layout on Windows. Alpine native dependencies and container UID behavior remain unverified until an approved image build/smoke test.

Run from the isolated worktree with Node 24, Python Playwright/Edge, Maven and Java 17:

```powershell
Set-Location C:\workspace\ai-investment-platform-admin-release
Push-Location frontend
npm ci --ignore-scripts
node node_modules/typescript/bin/tsc --noEmit
node --test --test-reporter=tap tests/correlation-id.test.mjs
node --test --test-reporter=tap tests/*.test.mjs
# Full CRLF checkout run has the documented 9 baseline failures.
$env:NEXT_PUBLIC_API_BASE_URL = ''
$env:NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED = 'false'
npm run build
if ($LASTEXITCODE -ne 0) { throw 'Production build failed' }
Pop-Location
mvn -f pom.xml -pl services/auth-service,services/api-gateway -am test '-Dtest=AdminUserIntegrationTest,RoleAdminServiceIntegrationTest,RoleAdminCliRunnerTest,AuthLifecycleIntegrationTest,GatewayAuthenticationFilterTest,AdminAuthProxyTest' '-Dsurefire.failIfNoSpecifiedTests=false'
if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed' }
```

Prepare a fresh standalone runner. Stop it with Ctrl+C when finished:

```powershell
$runner = Join-Path (Get-Location) ('artifacts/runner-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runner | Out-Null
Get-ChildItem frontend/.next/standalone -Force | Copy-Item -Destination $runner -Recurse -Force
Copy-Item frontend/.next/static (Join-Path $runner '.next/static') -Recurse -Force
Copy-Item frontend/public (Join-Path $runner 'public') -Recurse -Force
$env:PORT = '3107'
$env:HOSTNAME = '0.0.0.0'
$env:NODE_ENV = 'production'
node (Join-Path $runner 'server.js')
```

In a second terminal:

```powershell
Set-Location C:\workspace\ai-investment-platform-admin-release
Invoke-RestMethod http://127.0.0.1:3107/api/health
$env:ADMIN_TEST_BASE_URL = 'http://127.0.0.1:3107'
$env:ADMIN_TEST_EXPECT_INSECURE = 'false'
python frontend/tests/admin-users-browser.py
if ($LASTEXITCODE -ne 0) { throw 'Loopback browser tests failed' }
# Substitute the host's actual LAN IPv4 if it has changed.
$env:ADMIN_TEST_BASE_URL = 'http://192.168.178.178:3107'
$env:ADMIN_TEST_EXPECT_INSECURE = 'true'
python frontend/tests/admin-users-browser.py
if ($LASTEXITCODE -ne 0) { throw 'Insecure HTTP browser tests failed' }
```

## Image build plan — approval required, not executed

Build the frontend from a clean archive of this commit. Inputs: frontend Dockerfile/.dockerignore, package manifests/lockfile, Next/TypeScript configuration and declarations, `app/**`, `public/**`. This excludes auth/gateway/research-engine service source, the original worktree's unrelated uncommitted changes, and the untracked test log. Existing committed frontend Radar components remain part of the frontend. Do not run the repository-wide platform build/deploy script or use the dirty original worktree.

```powershell
Set-Location C:\workspace\ai-investment-platform-admin-release
$releaseSha = (git rev-parse HEAD).Trim()
$shortSha = $releaseSha.Substring(0, 12)
$buildContext = Join-Path (Get-Location) "artifacts/frontend-image-$shortSha"
if (Test-Path -LiteralPath $buildContext) { throw 'Use a new clean build context' }
$archivePath = Join-Path (Get-Location) "artifacts/frontend-image-$shortSha.zip"
git -c core.autocrlf=false archive --format=zip "--output=$archivePath" "${releaseSha}:frontend"
if ($LASTEXITCODE -ne 0) { throw 'Archive failed' }
Expand-Archive -LiteralPath $archivePath -DestinationPath $buildContext
$frontendImage = "localhost:5001/ai-investment/frontend:admin-release-$shortSha"
# STOP: approval required for image build and container smoke test.
docker build --pull=false --label "org.opencontainers.image.revision=$releaseSha" --build-arg 'NEXT_PUBLIC_API_BASE_URL=' --build-arg 'NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED=false' --tag $frontendImage $buildContext
docker run --rm --name admin-frontend-smoke -p 3107:3000 $frontendImage
```

During the approved container smoke test, repeat the second-terminal health/browser commands. APIs remain mocked. Record image/base-image digests and results. Pushing requires separate approval: `docker push $frontendImage`. Deploy an immutable digest. Never put a secret or JWT in build arguments.

Read-only deployment inspection still showed auth `dev-20261008-182305`, gateway `dev-20261008-163552`, frontend `dev-20261008-143335`, under `localhost:5001/ai-investment/`. Cached ADMIN image tags do not attest this corrected source. Replace the old frontend ADMIN candidate.

After separate approval: verify V1–V4/checksums, backup/recovery and auth schema permissions; apply V5 through the approved migration/auth release; release auth, gateway, then frontend. Starting normal auth can itself apply V5 because Flyway is enabled. The bootstrap CLI explicitly disables it. Auth/gateway must share issuer and secret reference; do not print or rotate secrets as part of this release. Verify health and read-only authorization before bootstrap.

Rollback: preserve prior image digests/revisions first. Revert frontend, then gateway/auth if needed under the approved plan. Retain additive V5 and audit history; do not drop the lock table or rewrite Flyway history. Image rollback does not undo a grant. Role rollback requires a separate audited authorization and must respect the last-admin guard.

## Bootstrap template and gates

No original bootstrap Job was found here, and its location was not supplied. `admin-bootstrap-job.approval-required.yaml` is a replacement review template, not a claim that the unidentified original was modified. It is outside Helm, suspended, uses approval-required operator/reason/image/database placeholders, has no retry, and rejects unfilled value placeholders before Java starts. It runs only `role-admin-cli` with Flyway disabled and Hibernate validation enabled. Do not apply it.

Approve a real distinct executor and a 1–500 character reason. Do not disguise the candidate's identity with an alias: the service rejects matching email, UUID or `user:UUID`. CLI identity is trusted operations input; HTTP identity comes from verified JWT claims. If the candidate is the executor, resolve the conflict without bypassing the guard. Use an immutable updated auth image containing the CLI profile fix.

Before any grant, an authorized operator should use an existing secure DB connection for these read-only checks, without credentials on the command line:

```sql
BEGIN READ ONLY;
SELECT version, success FROM auth.flyway_schema_history_auth ORDER BY installed_rank;
-- Query the lock only after confirming V5 is present and successful.
SELECT id FROM auth.role_admin_lock;
SELECT u.id, u.email, u.account_status, u.email_verified_at,
       array_agg(r.role ORDER BY r.role) FILTER (WHERE r.role IS NOT NULL) AS roles
FROM auth.app_users u LEFT JOIN auth.app_user_roles r ON r.user_id = u.id
WHERE u.normalized_email IN ('prakhar.gitlab@gmail.com', 'prakhar.unique@gmail.com')
GROUP BY u.id, u.email, u.account_status, u.email_verified_at;
SELECT count(*) AS active_verified_admins
FROM auth.app_users u JOIN auth.app_user_roles r ON r.user_id = u.id
WHERE r.role = 'ADMIN' AND u.account_status = 'ACTIVE' AND u.email_verified_at IS NOT NULL;
COMMIT;
```

Require one ACTIVE/verified candidate, lock row exactly `id=1`, and `prakhar.unique@gmail.com` USER-only. Check existing administrator state before calling this a first bootstrap. Review the fully populated Job and execution window, obtain approval, and execute once. Investigate failures before any retry. Both CLI and HTTP use RoleAdminService's transaction, PostgreSQL lock and same-transaction audit insertion. Duplicate grants are no-ops with no new audit; an existing grant requires reviewing the original audit.

After approved bootstrap, intentionally reauthenticate for fresh JWTs, without printing/storing tokens in reports. Candidate list/details/audit GETs through gateway should return 200; `prakhar.unique@gmail.com` should remain USER-only, have no menu, and get 403 for direct ADMIN GETs. Anonymous requests should return 401. Reauthentication is a separate deliberate session action, not a read-only DB check. Actual grant/revoke acceptance needs another approved test account/change; use mocks until then, never revoke the sole administrator as a test.

## Remaining blockers

1. Corrected image not built/pushed; Alpine/container smoke verification and approval remain.
2. Unchanged lockfile audit reports 10 affected package entries: 9 high, 1 critical, including direct `next`. Runtime advisories include image/OG processing and caching; other entries affect development tooling. Exact URLs/ranges are in `artifacts/npm-audit.json`. Deployment needs security triage/remediation or documented risk acceptance. No dependency upgrade was silently included.
3. Nine baseline CRLF portability failures and the seeded-session hydration mismatch remain documented.
4. Original Job location, actual distinct operator, approved reason, secret references and immutable auth image are unresolved. The suspended template is intentionally incomplete.
5. Live V5 and both users' roles were not queried in this task. SQL/fresh-session checks above are gates, not completed verifications.
6. Migration, deployment, role changes and real post-deployment checks require approval. No Radar workflow was triggered.

Recommendation: accept the narrow frontend fix for review. Do not deploy/bootstrap. After approval, the next action is an isolated frontend image build and mocked container smoke test; resolve security and bootstrap gates before release.
