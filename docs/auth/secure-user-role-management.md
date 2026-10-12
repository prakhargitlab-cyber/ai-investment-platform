# Secure user and role management

## Source review (2026-10-09)

- `AuthController` exposes registration, verification, login, reset and configuration-gated dev login under `/api/v1/auth`. `AuthLifecycleService` stores BCrypt credentials, hashed verification/reset tokens, and an explicit USER role on registration. Login requires ACTIVE status and loads database roles; empty legacy assignments fall back to USER. Dev identities remain USER-only.
- `HmacJwtService` issues HS256 tokens and verifies signatures, issuer, subject, issue time presence and expiry. Local subjects are durable account UUIDs; external identities use issuer/subject. No Spring Security filter chain or method-authorization annotations existed in auth-service; the security dependency was password crypto only.
- Existing `RoleAdminService` and the non-web `role-admin-cli` runner supported ADMIN grants/revocations, email-based self-management rejection, and audits. They lacked required reasons, current HTTP actor authorization, last-admin protection, and serialization. `RoleAdminProperties` binds action/email/role/operator/reason; it remains unchanged.
- V1–V3 define `auth.app_users`, credentials/status/verification fields and hashed token tables. V4 defines `(user_id, role)` role keys and audit rows containing target UUID/email, role, action, operator, optional legacy reason and creation time. These structures are reused. V5 adds one permanent mutex row; it does not grant any roles.
- `GatewayAuthenticationFilter` verifies bearer JWTs on private API routes and the proxy strips client identity headers. The existing auth prefix was exempt. The new admin subtree is now explicitly protected and the existing auth proxy preserves Authorization for independent downstream verification.
- The Next.js React workspace uses state-based navigation rather than a URL per screen. Current user and bearer token are stored in localStorage. The existing `request` client attaches bearer credentials and correlation IDs and clears the session on 401. The admin screen follows these conventions and shares UI components.
- Existing H2 tests cover auth lifecycle and role changes/new JWTs; gateway tests cover verified identity and header spoofing. Added tests cover admin APIs, concurrent changes, CLI results, and mocked-browser behavior.

## API contract

Base: `/api/v1/auth/admin/users`. Every request requires `Authorization: Bearer <token>` with a verified ADMIN claim **and** an ACTIVE, email-verified account that still has ADMIN in the database. The issuer/subject pair determines the actor. Client operator fields, email claims and identity headers never select the operator.

| Method | Path relative to base | Result |
| --- | --- | --- |
| GET | `?search=&page=0&size=20` | Case-insensitive email/name substring search, ordered by email then UUID |
| GET | `/{userId}` | User details and effective assigned roles |
| PUT | `/{userId}/roles/ADMIN` | Grant ADMIN; body `{"reason":"Approved ticket OPS-42"}`; 204 |
| DELETE | `/{userId}/roles/ADMIN` | Revoke ADMIN; same reason body; 204 |
| GET | `/{userId}/audit?page=0&size=20` | Target's role-change history, newest first, stable UUID tie-breaker |

Pages contain `content`, `page`, `size`, `totalElements`, `totalPages`. Page is zero-based; size is 1–100. Search is at most 320 characters. Reasons must contain non-whitespace text and be at most 500 characters. Only ADMIN is mutable; USER remains the baseline, including the existing legacy fallback.

User fields: `userId`, `email`, `displayName`, `status`, `emailVerifiedAt`, `createdAt`, `lastLoginAt`, `roles`. Credentials and tokens are never serialized. Audit fields: `id`, `userId`, `targetEmail`, `role`, `action`, `operator`, `reason`, `createdAt`. HTTP operators use `user:<immutable UUID>`; CLI operators identify the trusted infrastructure operator. Legacy audit operator strings are retained.

Missing/invalid/expired credentials return 401; authenticated USER, self-management, revoked ADMIN and inactive operators return 403. Missing users return 404. Invalid pagination/reason/identifier syntax returns 400. Unsupported roles, ineligible grant targets, last-active-admin removal and supported lock-conflict handling return 409. Successful duplicate grants and absent-role revocations return 204 without creating additional audit rows. Admin responses carry `Cache-Control: no-store`.

## Security and concurrency

- Both gateway and auth-service check JWT roles. Auth-service independently verifies signatures and issuer and resolves the actor from the database, so direct service access cannot bypass authorization with spoofed headers.
- Every mutation, including CLI operations, locks `auth.role_admin_lock(id=1)` with `SELECT ... FOR UPDATE` before permission/role reads. The lock remains held through the atomic role change and audit commit. This serializes changes across service instances and protects against concurrent last-admin removal and duplicate grants.
- HTTP actor authorization is repeated **inside** that lock. An operator whose ADMIN role was just revoked cannot complete a queued mutation using old JWT claims.
- Self-management is forbidden. CLI checks email and UUID forms; HTTP checks immutable IDs. An ADMIN grant requires ACTIVE status and a verification timestamp. Revocation cannot remove the final ACTIVE, verified administrator.
- No hardcoded administrator password, registration auto-promotion, or web bootstrap endpoint exists. CLI execution is a privileged infrastructure capability, not an authenticated end-user operation; control access to its runtime and database credentials and supply a truthful operator identity.
- Role changes require a reason. Audits record actual committed changes, not unsuccessful attempts or duplicate requests. There are no audit-edit/delete APIs. Database-level administrators can still alter rows; this is not a tamper-proof external audit archive.
- Previously issued JWT claims do not change. Login again to receive new roles. Admin APIs also check persisted roles, so revocation immediately prevents subsequent admin authorization checks. A read already authorized before a revocation may finish. Other pre-existing APIs keep their existing JWT behavior.

## UI

The sidebar exposes **Administration → Users & Roles** only for ADMIN. The screen also checks roles before mounting data-fetching controls. It provides paginated search, status and verification, user details, grant/revoke actions, mandatory reasons, confirmation prompts, feedback and paginated audit history. Self-management controls are hidden. A 401/403 clears displayed admin data and disables the screen. API enforcement remains authoritative if localStorage is manipulated.

## Validation

Follow-up: [disposable PostgreSQL verification](postgresql-role-admin-verification.md) passed 18 tests on PostgreSQL 16.15, including shared transaction IDs, lock retention through commit, and rollback after injected commit failures for both CLI and HTTP. The disposable database was removed afterward; the live environment remains untouched.

Final targeted Java run: **29 tests passed, zero failures/errors/skips** (23 auth-service, 6 gateway). This includes 9 new admin integration tests, 2 CLI tests, a new gateway proxy test, the gateway authorization tests, and existing auth lifecycle/role-token regressions. Concurrent duplicate grants, simultaneous last-admin revocations and mutual administrator revocations all passed. Flyway ran only against the test H2 database. This is a targeted run, not the whole multi-service Maven suite.

Run isolated Java checks from the repository root:

```powershell
mvn -f services/pom.xml -pl auth-service,api-gateway -am test "-Dtest=AdminUserIntegrationTest,RoleAdminServiceIntegrationTest,RoleAdminCliRunnerTest,AuthLifecycleIntegrationTest,GatewayAuthenticationFilterTest,AdminAuthProxyTest" "-Dsurefire.failIfNoSpecifiedTests=false"
```

Auth integration tests use `application-test.yml` and in-memory H2 only. Its lock timeout is 15 seconds to allow concurrent transactions during cold JVM query compilation. No production datasource is used. Coverage includes USER denial of read/mutation/history APIs, header/operator spoofing, self-promotion, mandatory reasons, missing users, unsupported roles, current database authorization, last-admin protection, duplicate requests, simultaneous revocations, immutable operator audits, registration/login and fresh JWT role changes.

Frontend checks, from `frontend`:

```powershell
npx tsc --noEmit
npm test
npm run dev -- --hostname 127.0.0.1 --port 3107
```

With that local server running, execute `python frontend/tests/admin-users-browser.py` from the repository root. It requires Python Playwright and Edge. It intercepts **all** API requests and checks hidden USER navigation, ADMIN search/details, required reason, cancelled/confirmed grants, revocation, audit and denied-access cleanup without contacting a backend.

Observed frontend results: TypeScript passed; all 302 existing Node tests passed; the new fixture-browser test passed. The checkout had an existing empty directory named `frontend/next.config.js` that caused repeated Next dev restarts. It was temporarily renamed for browser verification and restored afterward, along with Next's generated `next-env.d.ts` changes. The temporary server was stopped. Resolve that local placeholder before repeating browser verification if it still causes restart loops.

## Deployment and remaining operational checks

1. Review the patch together with the pre-existing uncommitted role-management files. The V4 migration, role entities and role properties already existed in the working tree and must be included in the eventual release if not yet committed.
2. Obtain explicit approval before live migrations, builds/pushes of Docker images, deployment or role grants. None are part of the source/test implementation task.
3. On an approved disposable PostgreSQL environment, validate V4/V5 and rerun concurrent mutation scenarios before rollout; H2 does not establish PostgreSQL-specific production behavior. Preserve existing Flyway checksums; V5 is additive.
4. After approval, apply V5 using the normal controlled migration process and deploy auth-service, gateway and frontend together. Do not run an old CLI binary alongside the new code: it does not participate in the mutex or last-admin rule. Keep role writes paused during mixed-version rollout.
5. Keep `role-admin-cli` disabled in serving deployments. Set matching strong `AUTH_JWT_SECRET`/issuer values in gateway and auth-service through secret management, disable dev login in production, and use TLS. The repository still has an existing development-secret fallback; this feature does not replace platform configuration.
6. Follow [the bootstrap procedure](admin-role-provisioning.md). After approval and execution, verify USER gets 403 through the gateway and directly against auth-service, then verify ADMIN listing/audits with a fresh login. Do not put bearer tokens in command history or logs.
7. Application rollback should retain V4/V5 and audit data. Do not revoke the sole administrator as a rollback strategy; provision an approved replacement first. An old CLI must not be used for rollback grants/revocations.

Remaining limits: localStorage remains exposed to pre-existing XSS risks; frontend role visibility can be stale until reauthentication; role-change rate limiting/MFA and external audit export are not introduced. Future account-disable/delete operations must participate in the same last-admin invariant and locking protocol. The fixture-browser test accounts for the existing workspace hydration mismatch caused by localStorage initialization. No live candidate status, production migration or production concurrency check has been performed.

## Files changed for this task

- Backend: `AdminAuthenticationFilter.java`, `AdminUserController.java`, `RoleAdminService.java`, `RoleAdminCliRunner.java`, `AuthServiceApplication.java`, `AppUserRepository.java`, `AppUserRoleAuditRepository.java`, `V5__role_admin_lock.sql`.
- Gateway: `GatewayAuthenticationFilter.java`.
- Frontend: `admin-api.ts`, `admin-users.tsx`, `admin-users.css`, targeted edits in `investment-workspace.tsx`.
- Tests: `AdminUserIntegrationTest.java`, `RoleAdminCliRunnerTest.java`, `RoleAdminServiceIntegrationTest.java`, `GatewayAuthenticationFilterTest.java`, `AdminAuthProxyTest.java`, auth `application-test.yml`, `admin-users-browser.py`.
- Documentation: this report and `admin-role-provisioning.md`.

All other pre-existing changes belong to the shared working tree and are outside this task.
