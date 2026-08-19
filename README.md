# AI Investment Intelligence Platform

Production-grade monorepo foundation for an AI-powered investment intelligence platform.

This repository is independent and does not use or modify any existing user-management project.

## Stack

- Backend: Java 17, Spring Boot, Maven
- AI services: Python 3.12+, FastAPI
- Frontend: React / Next.js
- Data platform: PostgreSQL, Redis, Kafka
- Infrastructure: Docker, k3d, Kubernetes, Helm, Terraform
- PRD target: Microsoft Azure AKS in `westeurope`
- Observability target: OpenTelemetry, Prometheus, Grafana

## Repository Layout

```text
frontend/
services/
ai/
shared/
infrastructure/
config/
scripts/
docs/
platform.ps1
```

## Platform Commands

```powershell
.\platform.ps1 up DEV
.\platform.ps1 status DEV
.\platform.ps1 down DEV

.\platform.ps1 up PRD
.\platform.ps1 status PRD
.\platform.ps1 down PRD
```

The first foundation iteration does not apply Azure infrastructure. `up PRD` validates the Azure subscription and creates a Terraform plan only. `down PRD` requires an explicit typed confirmation before running `terraform destroy` against this project's PRD Terraform state.

## Safe Local Validation

```powershell
mvn clean verify
python -m compileall ai/research-engine/app
helm lint infrastructure/helm/ai-investment-platform
terraform -chdir=infrastructure/terraform/environments/prd fmt -check
```

Foundation validation creates no Azure resources. Use Terraform plan/apply only after an explicit deployment review.

## Phase 3 Research Intelligence

The research foundation lives in `ai/research-engine` and exposes `/api/v1/research/*` APIs for company profiles, documents, extracted events, source providers, schedule rules, refresh, and deterministic catalyst summaries. It uses demo fixtures by default and does not perform Google result scraping, CAPTCHA bypass, paywall bypass, paid API calls, or BUY/SELL recommendations.

## Security Baseline

No broker credentials, Azure credentials, API keys, or LLM provider secrets belong in source control. Use Azure CLI context for development authentication and Key Vault / Kubernetes secrets for production runtime secrets.
