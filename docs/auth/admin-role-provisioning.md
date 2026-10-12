# One-time administrator bootstrap

This is a reviewed procedure, not an executed grant. Target: the existing account
`prakhar.gitlab@gmail.com`. Explicit approval is required before live migrations,
grants, builds/pushes of Docker images, deployments or Kubernetes jobs.
No administrator password is created; public registration only assigns USER.

See [secure-user-role-management.md](secure-user-role-management.md) for architecture,
API contracts, security properties, test commands and deployment sequencing.

## Preconditions and read-only review

Use the reviewed release containing V4 roles/audit and V5 role-change locking.
An authorized migration process must apply both first. The bootstrap command below
disables Flyway to prevent accidental migrations. Confirm the intended database.
Inject DB credentials and JWT configuration through existing secret management;
never paste secrets into shell commands, manifests or logs. Do not reuse an
unreviewed saved job file.

Run through the approved read-only database access channel:

```sql
SELECT id, email, account_status, email_verified_at
FROM auth.app_users WHERE normalized_email = 'prakhar.gitlab@gmail.com';

SELECT r.role, r.granted_at
FROM auth.app_user_roles r JOIN auth.app_users u ON u.id = r.user_id
WHERE u.normalized_email = 'prakhar.gitlab@gmail.com';

SELECT count(*) AS active_verified_administrators
FROM auth.app_users u JOIN auth.app_user_roles r ON r.user_id = u.id
WHERE r.role = 'ADMIN' AND u.account_status = 'ACTIVE'
  AND u.email_verified_at IS NOT NULL;

SELECT version, description, success FROM auth.flyway_schema_history_auth
WHERE version IN ('4', '5');
SELECT id FROM auth.role_admin_lock WHERE id = 1;
```

Require exactly one candidate, ACTIVE status, a verification timestamp, and USER.
If ADMIN already exists, inspect its audit history and stop: bootstrap is complete.
If another active administrator exists, prefer having them grant access in the UI.
The candidate's live status has not been independently checked during this task.

Identify the actual authorized infrastructure operator, distinct from the target,
and record an approval/change-ticket reason. CLI execution is a privileged
infrastructure capability; its operator string is asserted by that operator, not
an application JWT. Do not invent an operator name to bypass self-management rules.
HTTP APIs always derive operator identity from verified JWT identity and the database.

## Grant, only after explicit approval

Use a reviewed auth-service JAR in an isolated process with approved secret-injected
DB_* and AUTH_* configuration. Replace the operator and reason placeholders.

```powershell
$env:ROLE_ADMIN_ACTION = 'GRANT'
$env:ROLE_ADMIN_EMAIL = 'prakhar.gitlab@gmail.com'
$env:ROLE_ADMIN_ROLE = 'ADMIN'
$env:ROLE_ADMIN_OPERATOR = '<actual authorized infrastructure operator>'
$env:ROLE_ADMIN_REASON = '<approved ticket and bootstrap justification>'
try {
    java -jar services/auth-service/target/auth-service-0.1.0-SNAPSHOT.jar `
      --spring.profiles.active=role-admin-cli `
      --spring.main.web-application-type=none `
      --spring.flyway.enabled=false
    if ($LASTEXITCODE -ne 0) { throw 'Bootstrap failed; inspect safe error and audit state.' }
} finally {
    Remove-Item Env:ROLE_ADMIN_ACTION, Env:ROLE_ADMIN_EMAIL, Env:ROLE_ADMIN_ROLE, Env:ROLE_ADMIN_OPERATOR, Env:ROLE_ADMIN_REASON -ErrorAction SilentlyContinue
}
```

Never enable this profile on a serving deployment. If Kubernetes is the approved
execution mechanism, use a separate short-lived Job with an approved image containing
this code, secret references, these non-secret role parameters, Flyway disabled,
restartPolicy Never and backoffLimit 0. Creating that Job requires deployment approval.
No Job is created here.

The grant and audit commit atomically. Repeated grants are no-ops without extra audit
rows. Validation failures return process exit 1; success returns 0. Always verify state.

## Verify and finish

```sql
SELECT u.id, u.email, r.role, r.granted_at
FROM auth.app_users u JOIN auth.app_user_roles r ON r.user_id = u.id
WHERE u.normalized_email = 'prakhar.gitlab@gmail.com';

SELECT a.id, a.target_email, a.role, a.action, a.operator, a.reason, a.created_at
FROM auth.app_user_role_audit a JOIN auth.app_users u ON u.id = a.user_id
WHERE u.normalized_email = 'prakhar.gitlab@gmail.com'
ORDER BY a.created_at DESC, a.id DESC;
```

Require USER and ADMIN plus a GRANT audit with the approved operator and reason.
The candidate signs out and signs in using existing credentials. Fresh JWT/login
roles include ADMIN and the sidebar exposes Administration -> Users & Roles.
Verify listing/history through the gateway and confirm ordinary USER requests get 403.
Remove the one-shot runtime/parameters and retain approval and audit evidence.

## Revocation and rollback

Live revocation also requires approval. Use another administrator through the UI,
or the audited CLI with REVOKE and a truthful operator and reason. The last active,
verified administrator cannot be removed: establish an approved replacement first.
Do not manually delete role/audit rows or use an old CLI to bypass the invariant.

Existing JWT claims persist until expiry or reauthentication. The new admin APIs
also check current database roles, so subsequent requests are denied immediately
after revocation. Other pre-existing APIs retain their existing token semantics.
