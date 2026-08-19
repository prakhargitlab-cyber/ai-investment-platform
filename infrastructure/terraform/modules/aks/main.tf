resource "azurerm_kubernetes_cluster" "main" {
  name                = var.name
  resource_group_name = var.resource_group_name
  location            = var.location
  dns_prefix          = var.dns_prefix
  tags                = var.tags

  default_node_pool {
    name           = "system"
    node_count     = 2
    vm_size        = "Standard_B2s"
    vnet_subnet_id = var.subnet_id
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin = "azure"
    network_policy = "azure"
  }
}
