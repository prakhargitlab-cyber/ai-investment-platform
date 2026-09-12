locals {
  short_name = "aiinvestprd"
  tags = {
    project     = var.project_name
    environment = "prd"
    managed_by  = "terraform"
  }
}

module "resource_group" {
  source   = "../../modules/resource-group"
  name     = "rg-${var.project_name}-prd"
  location = var.location
  tags     = local.tags
}

module "networking" {
  source              = "../../modules/networking"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  project_name        = var.project_name
  address_space       = ["10.42.0.0/16"]
  aks_subnet_prefixes = ["10.42.1.0/24"]
  tags                = local.tags
}

module "acr" {
  source              = "../../modules/acr"
  name                = "${local.short_name}acr"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = local.tags
}

module "key_vault" {
  source              = "../../modules/key-vault"
  name                = "${local.short_name}kv"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tenant_id           = var.tenant_id
  tags                = local.tags
}

module "aks" {
  source                  = "../../modules/aks"
  name                    = "aks-${var.project_name}-prd"
  resource_group_name     = module.resource_group.name
  location                = module.resource_group.location
  dns_prefix              = "ai-investment-prd"
  subnet_id               = module.networking.aks_subnet_id
  private_cluster_enabled = var.aks_private_cluster_enabled
  local_account_disabled  = var.aks_local_account_disabled
  tags                    = local.tags
}

resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = module.acr.id
  role_definition_name = "AcrPull"
  principal_id         = module.aks.kubelet_identity_object_id
}

resource "azurerm_user_assigned_identity" "platform_workload" {
  name                = "id-${var.project_name}-prd-workload"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "platform_workload" {
  name      = "fic-${var.project_name}-prd"
  parent_id = azurerm_user_assigned_identity.platform_workload.id
  issuer    = module.aks.oidc_issuer_url
  audience  = ["api://AzureADTokenExchange"]
  subject   = "system:serviceaccount:${var.workload_identity_namespace}:${var.workload_identity_service_account}"
}

resource "azurerm_role_assignment" "platform_key_vault_secrets" {
  scope                = module.key_vault.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.platform_workload.principal_id
}
