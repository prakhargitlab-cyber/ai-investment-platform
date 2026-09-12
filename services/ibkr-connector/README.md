# IBKR Connector Runtime

`ibkr-connector` manages one user-specific Interactive Brokers Client Portal Gateway runtime session. It contains process management and read-only Gateway proxy operations only. Portfolio synchronization and broker business logic remain in `broker-service`.

## Artifact policy

Do not commit the IBKR Client Portal Gateway ZIP, extracted binaries, certificates, or bundled proprietary files to this repository. The runtime expects an official package supplied outside the repository:

- DEV: mount or provide the locally downloaded and extracted official package.
- PRD: keep the artifact source configuration-driven and disabled until legal and deployment approval exists.

Required configuration:

- `AIP_IBKR_GATEWAY_PACKAGE_PATH`: path to the extracted official package containing `bin/run.sh` and `root/conf.yaml`.
- `AIP_IBKR_GATEWAY_BASE_URL`: Gateway API base URL reachable from the connector process, for example the Gateway's `/v1/api` endpoint.
- `AIP_INTERNAL_TOKEN`: shared internal token used by broker-service.
- `AIP_IBKR_LOGIN_PUBLIC_BASE_URL`: external base route that fronts this connector, if the returned login URL must be absolute.

DEV-only TLS:

- `AIP_IBKR_GATEWAY_TLS_VERIFY=false` may be used only for the connector's local Gateway client when the official Gateway uses a self-signed certificate.
- PRD must keep TLS verification enabled unless a deployment-approved certificate trust chain is configured.

## DEV image build

From the repository root:

```powershell
docker build -f services/ibkr-connector/Dockerfile -t ai-investment/ibkr-connector:dev .
```

## DEV container run

Replace the package path with the local extracted official IBKR Gateway package. This command mounts it read-only and does not bake IBKR binaries into the image:

```powershell
docker run --rm -p 18080:8080 `
  -e AIP_IBKR_GATEWAY_PACKAGE_PATH=/opt/ibkr/clientportal.gw `
  -e AIP_IBKR_GATEWAY_BASE_URL=<GATEWAY_API_BASE_URL> `
  -e AIP_IBKR_GATEWAY_TLS_VERIFY=false `
  -e AIP_INTERNAL_TOKEN=dev-internal-connector-token-change-me `
  -e AIP_IBKR_LOGIN_PUBLIC_BASE_URL=http://127.0.0.1:18080 `
  -v "<OFFICIAL_IBKR_GATEWAY_PACKAGE_PATH>:/opt/ibkr/clientportal.gw:ro" `
  ai-investment/ibkr-connector:dev
```

The runtime launches only:

```text
bin/run.sh root/conf.yaml
```

It does not shell-execute arbitrary commands.

## Internal API

All `/internal/**` routes require `X-Internal-Token` and user ownership via `X-AIP-User-Id` after creation.

- `POST /internal/connectors`
- `GET /internal/connectors/{id}/status`
- `GET /internal/connectors/{id}/login`
- `POST /internal/connectors/{id}/stop`
- `POST /internal/connectors/{id}/restart`
- `GET /internal/connectors/{id}/accounts`
- `GET /internal/connectors/{id}/positions?account_id={accountId}&page=0`
- `GET /internal/connectors/{id}/ledger?account_id={accountId}`

Trading routes are intentionally absent. Order submission, modification, cancellation, and reply endpoints are not proxied.

## Isolation model

The service contract is per-user and per-connector. This DEV runtime enforces one active connector per process so a deployed pod/container owns exactly one IBKR Gateway session:

```text
User A -> Connector A runtime -> Gateway A
User B -> Connector B runtime -> Gateway B
```

Kubernetes production orchestration should create one runtime pod per connector session. The current Helm chart deploys a single disabled-by-default runtime suitable for DEV validation and is not a claim of dynamic multi-user pod orchestration.
