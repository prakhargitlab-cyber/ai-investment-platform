# PostgreSQL transaction verification — 2026-10-09

**Result: PASS.** `RoleAdminPostgresVerificationTest` ran 18 tests with zero failures,
errors or skips on PostgreSQL **16.15**, using READ COMMITTED isolation. No H2 tests
were run for this verification. No production application code was changed.

## Isolation of the verification environment

- Used the already-cached `postgres:16` image; no image build, push or deployment.
- Created container `role-admin-verify-3eb0e955d108` with `--rm`, label
  `aip.role-admin-verification=true`, and a tmpfs at `/var/lib/postgresql/data`.
- Published only `127.0.0.1:58953`; database was `role_admin_verify_3eb0e955d108`.
- Overrode both application and Flyway JDBC URLs explicitly. The opt-in test rejects
  URLs outside the loopback/disposable-name pattern and has no live URL fallback.
- V1–V5 were applied only to this newly created disposable database. PostgreSQL
  reported every migration successful. All identities and role changes were synthetic
  test fixtures (`example.test`); the bootstrap candidate was never accessed.
- Used the actual CLI runner class with the Spring-proxied service, without activating
  the CLI startup profile or executing a bootstrap command. HTTP checks used MockMvc
  with the real auth filter, controller, repositories and transaction manager.
- After verification, stopped the labelled disposable container and confirmed it
  no longer appeared in `docker ps -a`; its tmpfs database was discarded.

## Source evidence

`RoleAdminService.java`:

- Lines 31–34: `grantRole` is `@Transactional` and calls `lockChanges()` first.
- Lines 49–52: `revokeRole` is `@Transactional` and calls `lockChanges()` first.
- Lines 90–99: HTTP `changeRole` is `@Transactional`, acquires the same lock before
  operator authorization, then invokes private grant/revoke helpers in that transaction.
- Lines 75–76 execute exactly:

  ```sql
  select id from auth.role_admin_lock where id = 1 for update
  ```

- Lines 45–46 and 66–67 perform role changes and audit saves. There is no
  `REQUIRES_NEW`, separate connection, async write or caught exception in this service.
  The repositories participate in the surrounding Spring transaction.

`RoleAdminCliRunner.java` lines 37/40 call the injected service's public grant/revoke
methods. Its catch blocks run only after the transactional proxy has returned or
failed, so catching the exception does not commit a failed transaction.
`AdminUserController.java` lines 49/55 call the injected service's `changeRole` method.
The runtime test explicitly confirmed that this service is a Spring AOP proxy.

## PostgreSQL evidence

Test-only triggers recorded `txid_current()` for both role-table and audit-table writes.
A deferred audit trigger paused the transaction **during COMMIT**, after its writes,
using an advisory lock held by a separate test connection. `pg_stat_activity` and
`pg_blocking_pids()` confirmed that the service had reached this pause.

While paused, neither write was visible to an independent connection, and a separate
`SELECT ... FOR UPDATE NOWAIT` against `auth.role_admin_lock(id=1)` failed with
PostgreSQL SQLSTATE **55P03**. After releasing the gate, both writes became visible,
their recorded transaction IDs matched, and the mutex could be acquired immediately.

| Path | Action | Role and audit transaction ID | Lock retained until commit |
| --- | --- | --- | --- |
| CLI | GRANT | 941 | Passed |
| CLI | REVOKE | 962 | Passed |
| HTTP | GRANT | 982 | Passed |
| HTTP | REVOKE | 1003 | Passed |

A second deferred trigger deliberately raised an exception at commit. All four
CLI/HTTP × GRANT/REVOKE cases passed: the role remained in its original state, no
audit or witness rows persisted, and the mutex was released. CLI reported exit code
1; HTTP propagated the transaction failure instead of returning success.

The nine inherited admin integration cases also passed on PostgreSQL, including
duplicate concurrent grants, simultaneous last-admin revocations, concurrent
administrators revoking one another, JWT rejection, authorization and operator audits.

Command used:

```powershell
mvn -f services/pom.xml -pl auth-service -am test `
  '-Dtest=RoleAdminPostgresVerificationTest' `
  '-Dsurefire.failIfNoSpecifiedTests=false' `
  '-DroleAdmin.disposablePostgresUrl=jdbc:postgresql://127.0.0.1:58953/role_admin_verify_3eb0e955d108'
```

Surefire evidence is in
`services/auth-service/target/surefire-reports/com.aiinvestment.auth.RoleAdminPostgresVerificationTest.txt`
and the corresponding XML, including the `COMMIT_VERIFIED` and `ROLLBACK_VERIFIED`
markers. The disposable address above is historical, not a reusable database.

## Scope and caveats

- No blocker was found for the requested locking/atomicity/rollback behavior.
- This conclusion covers **RoleAdminService-managed ADMIN mutations**. Public
  registration separately inserts the baseline USER role in its own lifecycle
  transaction; it does not take the administrator mutex. No other public API
  manages ADMIN outside this service.
- The annotations use default propagation/isolation and default rollback rules.
  The verified database isolation was READ COMMITTED. Current business exceptions
  extend RuntimeException, and database/commit failures are unchecked; these roll
  back. Arbitrary future checked exceptions would need explicit rollback rules.
- Live schema state, nondefault isolation, external database writers and other
  production operational settings were not inspected or modified.

Only a new opt-in verification test and documentation were added for this request.
