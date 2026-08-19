# Azure Deployment

## Azure Context

Expected Azure environment:

- Subscription name: Azure subscription 1
- Subscription ID: `2dccba84-7038-4126-b0b2-32f8f29bcbd4`
- Tenant ID: `ee868f5c-6f21-48fa-b329-3e114d8d229d`
- Region: `westeurope`

The canonical checked-in PRD Azure account configuration is `config/prd/azure.json`. `platform.ps1` reads this file and passes those values into Terraform. `prd.auto.tfvars.example` is a human-readable Terraform template for direct Terraform use; if values differ, treat `config/prd/azure.json` as authoritative for platform commands.

Development authentication uses the currently authenticated Azure CLI context. Credentials must not be stored in this repository.

## Subscription Guardrail

Before PRD deployment, validate:

```powershell
az account show
```

If the active subscription is wrong, set it manually:

```powershell
az account set --subscription "2dccba84-7038-4126-b0b2-32f8f29bcbd4"
```

`platform.ps1` fails safely when the active Azure subscription does not match the expected subscription ID.

## PRD Terraform

The first Terraform environment is located at:

```text
infrastructure/terraform/environments/prd
```

It defines Terraform-owned PRD resources:

- Resource group
- Azure Container Registry
- VNet
- AKS subnet
- Azure Key Vault
- AKS cluster

For this foundation iteration:

```powershell
.\platform.ps1 up PRD
```

validates Azure and creates a Terraform plan, but intentionally stops before `terraform apply`.

## Disposable PRD

When real PRD resources have been created from this Terraform state, destruction must be limited to Terraform-owned resources:

```powershell
.\platform.ps1 down PRD
```

The script validates the active Azure subscription and requires typing `DESTROY ai-investment-platform PRD` before it invokes `terraform destroy`. After destruction, audit remaining tagged resources:

```powershell
az resource list --tag project=ai-investment-platform --output table
```

Do not manually delete unrelated Azure resources.

Foundation validation must not run `terraform plan`, `terraform apply`, or `terraform destroy`, and must not create Azure resources.
