variable "name" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "tenant_id" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "purge_protection_enabled" {
  type        = bool
  description = "Protect production secrets from immediate permanent deletion."
  default     = true
}

variable "soft_delete_retention_days" {
  type        = number
  description = "Key Vault soft-delete retention window."
  default     = 90
}

variable "enable_rbac_authorization" {
  type        = bool
  description = "Use Azure RBAC so workload identities can receive least-privilege secret access."
  default     = true
}
