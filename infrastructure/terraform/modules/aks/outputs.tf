output "name" {
  value = azurerm_kubernetes_cluster.main.name
}

output "id" {
  value = azurerm_kubernetes_cluster.main.id
}

output "oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.main.oidc_issuer_url
}

output "kubelet_identity_object_id" {
  value = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
}

output "node_resource_group" {
  value = azurerm_kubernetes_cluster.main.node_resource_group
}
