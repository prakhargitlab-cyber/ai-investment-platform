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
