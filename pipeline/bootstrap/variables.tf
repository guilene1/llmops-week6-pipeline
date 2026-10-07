# Put your values in terraform.tfvars (see terraform.tfvars.example).

variable "github_repository" {
  description = <<-EOT
    Your copy of this repository, as owner/name, e.g. your-username/llmops-week6-pipeline.
    The pipeline role trusts this repository only: its main branch and its pull requests.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use owner/name, for example your-username/llmops-week6-pipeline."
  }
}

variable "aws_region" {
  description = "Must match aws_region in infra/terraform."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Must match project_name in infra/terraform. Every permission is scoped to resources with this prefix."
  type        = string
  default     = "northwind-hr"
}

variable "create_oidc_provider" {
  description = <<-EOT
    An AWS account can hold only one OIDC provider for token.actions.githubusercontent.com.
    If yours already has one (another course, another project), set this to false and
    the existing provider is used.
  EOT
  type        = bool
  default     = true
}

variable "allow_state_bucket_destroy" {
  description = <<-EOT
    false protects the state bucket: destroy fails while it holds any state, which is
    what you want while the stack exists. Set true only at final teardown, after the
    main stack has been destroyed.
  EOT
  type        = bool
  default     = false
}
