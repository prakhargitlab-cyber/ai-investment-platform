# Helm DEV credential preservation

The chart must retain the PostgreSQL and SearXNG credentials already present in the release namespace. A routine upgrade must not replace either credential with a newly generated value. This change affects chart rendering only; it does not rotate credentials or change a running database.

The resource names and consumer references remain unchanged:

| Credential | Secret name value | Data key | Fresh-install generated length |
|---|---|---|---:|
| PostgreSQL | `devDependencies.postgres.passwordSecretName` (default `dev-postgres`) | `POSTGRES_PASSWORD` | 32 |
| SearXNG | `devDependencies.searxng.secretName` (default `dev-searxng`) | `devDependencies.searxng.secretKeyName` (default `SEARXNG_SECRET`) | 48 |

The PostgreSQL Secret is rendered when `devDependencies.enabled` is true. The SearXNG Secret additionally requires `devDependencies.searxng.enabled`. No new resource, hook, namespace, or credential store is needed.

## Credential resolution

The shared `aip.devCredential` helper in [templates/_helpers.tpl](../infrastructure/helm/ai-investment-platform/templates/_helpers.tpl) performs an exact namespaced lookup of the configured Secret. Its two callers are in [templates/dev-dependencies.yaml](../infrastructure/helm/ai-investment-platform/templates/dev-dependencies.yaml). It does not search other namespaces or release names.

| Render state | Required behavior |
|---|---|
| Existing valid Secret, install or upgrade | Reuse its existing encoded credential verbatim. |
| Existing Secret with an invalid type, missing key, malformed encoding, or empty credential | Stop rendering with a value-safe error. Explicit values do not repair it. |
| Missing Secret during an upgrade | Stop rendering, including when an explicit credential value is supplied. |
| Missing Secret during a fresh install, explicit nonempty string supplied | Encode and use that bootstrap value. |
| Missing Secret during a fresh install, no bootstrap value | Generate the credential once for this install render. |
| API permission, connectivity, or other lookup error | Propagate the error and stop rendering. |

An existing credential takes precedence over `devDependencies.postgres.password` and `devDependencies.searxng.secretKey`. These values are bootstrap inputs, not a rotation interface. Supplying a different value while a valid Secret exists deliberately leaves the existing credential unchanged. Credential rotation requires a separately reviewed process that also accounts for the running consumers and PostgreSQL's stored authentication state.

Existing Secrets must have type `Opaque` and a string value under the required `data` key. Base64 validation rejects malformed encodings rather than treating a decoder's error text as a password. The decoded credential must contain at least one byte. Existing whitespace is preserved exactly; the helper does not trim or normalize credentials. Valid existing encoded bytes are emitted through `data`, preserving the credential without a `stringData` round trip. Diagnostics must not include encoded or decoded credentials.

The helper does not fall back to randomness when an expected Secret is absent or invalid. A Secret renamed in values is also missing at its new name, so an upgrade fails. Enabling one of these dependencies for the first time during an upgrade likewise requires explicit provisioning review; the missing-Secret guard is not bypassed by bootstrap values.

## Offline rendering and test limits

Helm's normal client-side rendering does not read cluster objects. An ordinary offline install render can therefore generate fresh-install credentials; it cannot demonstrate preservation of a deployed credential. An offline upgrade render with `--is-upgrade` must fail when it cannot obtain the existing Secret. Do not add an offline bypass or supply purported existing credentials through values to make that render pass.

[Helm's lookup documentation](https://helm.sh/docs/chart_template_guide/functions_and_pipelines/#using-the-lookup-function) distinguishes an absent object from an API error and documents the client/server dry-run distinction. [The function reference](https://helm.sh/docs/chart_template_guide/function_list/) documents the template functions used by the helper.

The installed Helm version is **v4.2.3**. Its [versioned lookup implementation](https://github.com/helm/helm/blob/v4.2.3/pkg/engine/lookup_func.go) returns an empty object for a missing resource and propagates other API errors. Its [template engine](https://github.com/helm/helm/blob/v4.2.3/pkg/engine/engine.go) suppresses `fail` errors in lint mode: a successful `helm lint` result alone does not validate the credential guards.

Offline tests can exercise the real lookup behavior against a loopback-only fake API with synthetic Secret fixtures and a disposable kubeconfig, using `helm template --dry-run=server`. Helm v4.2.3 does not support `--hide-secret` for `template`, so the test subprocess captures output in memory and never prints rendered Secret bodies. These tests must never use an operator's kubeconfig, contact a real cluster, or retain generated credentials as artifacts. Include missing, invalid, access-denied and valid existing-Secret cases for both consumers, and verify that explicit bootstrap values cannot replace an existing credential or bypass the upgrade guard.

The DEV render retains all **54 resource identities**, including their kinds, names and effective namespace. A research-engine image override must continue to affect both the application container and `wait-for-research-migrations`, while other workloads remain unchanged. The five existing DEV execution settings remain:

| Setting | Value |
|---|---|
| `research.opportunityRecoveryPaused` | `true` |
| `research.opportunityScheduler.enabled` | `false` |
| `research.equityManualExecutionEnabled` | `true` |
| `research.etfManualExecutionEnabled` | `true` |
| `research.etfFullUniverseExecutionEnabled` | `true` |

Rendering these settings does not execute a research run. Stale test expectations must be corrected to match the existing policy without modifying the policy itself.

## Release acceptance remains separate

No Helm upgrade, real-cluster server-side dry run, live Secret read, Kubernetes change, database operation, image build or deployment was performed while implementing this chart change. AMFI code and backups are outside this work.

A separately authorized release review must verify the intended context and namespace, use an upgrade server-side dry run with secret hiding enabled (`helm upgrade` with `--dry-run=server --hide-secret`), and inspect a sanitized diff before deployment. The secret-hiding flag belongs to `install`/`upgrade`, not `template`. No upgrade command, including an upgrade dry run, was executed during this work, and that release review has not been performed. No Secret content should be printed or copied into a values file, report or command history.

The render-time lookup does not prove that a retained PostgreSQL credential matches an already initialized data volume, prevent a later out-of-band Secret change, or alter manifests stored in older Helm revisions. Fresh-install generation assumes separately verified fresh provisioning. Rollback and credential recovery remain separate operations; they are not performed or authorized by this helper.
