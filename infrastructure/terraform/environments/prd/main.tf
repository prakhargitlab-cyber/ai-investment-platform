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
  source              = "../../modules/aks"
  name                = "aks-${var.project_name}-prd"
  resource_group_name = module.resource_group.name
  location            = module.resource_group.location
  dns_prefix          = "ai-investment-prd"
  subnet_id           = module.networking.aks_subnet_id
  tags                = local.tags
}
