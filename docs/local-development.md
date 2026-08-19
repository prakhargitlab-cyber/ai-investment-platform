# Local Development

## Prerequisites

- Java 17
- Maven
- Python 3.12+
- Node.js 20+
- Docker Desktop
- k3d
- kubectl
- Helm

## Validate the Foundation

```powershell
mvn clean verify

python -m py_compile ai/research-engine/app/main.py
python -m py_compile ai/valuation-engine/app/main.py
python -m py_compile ai/ranking-engine/app/main.py
python -m py_compile ai/portfolio-optimizer/app/main.py

helm lint infrastructure/helm/ai-investment-platform
```

## Run DEV Platform

```powershell
.\platform.ps1 up DEV
.\platform.ps1 status DEV
.\platform.ps1 down DEV
```

The DEV command creates a local k3d cluster named `ai-investment-dev` and installs the Helm chart into the `ai-investment` namespace.

## Spring Boot Local Ports

When services are run directly from an IDE or Maven without overriding `SERVER_PORT`, their local defaults are:

| Service | Port |
| --- | ---: |
| api-gateway | 8080 |
| auth-service | 8081 |
| portfolio-service | 8082 |
| broker-service | 8083 |
| company-service | 8084 |
| research-service | 8085 |
| recommendation-service | 8086 |
| risk-service | 8087 |
| notification-service | 8088 |

Container and Helm deployments can override these through `SERVER_PORT`.

## Local LLM Option

DEV configuration supports Ollama via:

```text
AIP_LLM_PROVIDER=ollama
AIP_OLLAMA_BASE_URL=http://ollama:11434
```

No model is pulled or called in the foundation iteration.
