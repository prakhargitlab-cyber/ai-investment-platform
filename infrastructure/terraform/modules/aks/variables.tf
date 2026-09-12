variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "dns_prefix" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "node_count" {
  type        = number
  description = "Initial system node count. Keep this modest for low-cost environments."
  default     = 2
}

variable "node_vm_size" {
  type        = string
  description = "System node VM size."
  default     = "Standard_B2s"
}

variable "oidc_issuer_enabled" {
  type        = bool
  description = "Enable the OIDC issuer required by AKS Workload Identity."
  default     = true
}

variable "workload_identity_enabled" {
  type        = bool
  description = "Enable AKS Workload Identity."
  default     = true
}

variable "key_vault_secrets_provider_enabled" {
  type        = bool
  description = "Enable the AKS Secrets Store CSI provider for Azure Key Vault."
  default     = true
}

variable "private_cluster_enabled" {
  type        = bool
  description = "Whether the Kubernetes API server is private. Decide this with the operations network model."
  default     = false
}

variable "local_account_disabled" {
  type        = bool
  description = "Disable local Kubernetes administrator accounts after Entra RBAC is configured."
  default     = false
}

variable "outbound_type" {
  type        = string
  description = "AKS outbound mode. Stable egress designs may override this with userAssignedNATGateway or userDefinedRouting."
  default     = "loadBalancer"
}
