output "resource_group_name" {
  value = module.resource_group.name
}

output "acr_login_server" {
  value = module.acr.login_server
}

output "aks_cluster_name" {
  value = module.aks.name
}

output "key_vault_name" {
  value = module.key_vault.name
}

output "workload_identity_client_id" {
  value = azurerm_user_assigned_identity.platform_workload.client_id
}

output "aks_oidc_issuer_url" {
  value = module.aks.oidc_issuer_url
}
