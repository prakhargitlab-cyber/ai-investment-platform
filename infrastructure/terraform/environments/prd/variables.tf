variable "subscription_id" {
  type        = string
  description = "Expected Azure subscription ID."
}

variable "tenant_id" {
  type        = string
  description = "Expected Azure tenant ID."
}

variable "location" {
  type        = string
  description = "Azure region."
}

variable "project_name" {
  type        = string
  description = "Project resource prefix."
  default     = "ai-investment-platform"
}

variable "workload_identity_namespace" {
  type        = string
  description = "Kubernetes namespace used by the Helm release."
  default     = "ai-investment"
}

variable "workload_identity_service_account" {
  type        = string
  description = "ServiceAccount name configured in the Azure Helm values."
  default     = "ai-investment-platform"
}

variable "aks_private_cluster_enabled" {
  type        = bool
  description = "Enable only after private DNS and operator connectivity have been designed."
  default     = false
}

variable "aks_local_account_disabled" {
  type        = bool
  description = "Enable after Entra Kubernetes RBAC administrators are configured."
  default     = false
}
