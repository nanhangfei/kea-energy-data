variable "account_id" {
  description = "The personal account's 12-digit id (KEA_AWS_ACCOUNT_ID in .env)."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id is the 12-digit AWS account id."
  }
}

variable "profile" {
  description = "AWS CLI profile for the personal account."
  type        = string
  default     = "kea-personal"
}

variable "region" {
  description = "Sydney: Snowflake's region, and classic MWAA's (deployment-plan.md §1)."
  type        = string
  default     = "ap-southeast-2"
}

variable "alert_email" {
  description = "Where the budget alerts and the forgotten-session reminder go."
  type        = string
}

variable "budget_usd" {
  description = "Monthly cost budget in USD, counted before credits (deployment-plan.md §1, row 3)."
  type        = number
  default     = 40
}

variable "session_max_hours" {
  description = "A project=kea NAT gateway, RDS instance or MWAA environment older than this triggers the hourly reminder."
  type        = number
  default     = 6
}
