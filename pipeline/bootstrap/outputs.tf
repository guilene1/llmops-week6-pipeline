# What the main stack and GitHub need from the bootstrap. None of these is a secret.

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "pipeline_role_arn" {
  description = "Set as the GitHub Actions variable AWS_ROLE_ARN"
  value       = aws_iam_role.pipeline.arn
}

# Write it beside the main stack, then init with it:
#   terraform -chdir=pipeline/bootstrap output -raw backend_config > infra/terraform/backend.hcl
#   terraform -chdir=infra/terraform init -backend-config=backend.hcl
output "backend_config" {
  value = <<-EOT
    bucket = "${aws_s3_bucket.state.bucket}"
    region = "${var.aws_region}"
  EOT
}

# Repository variables (Settings > Secrets and variables > Actions > Variables), not secrets
output "github_variables" {
  value = <<-EOT
    AWS_ROLE_ARN     = ${aws_iam_role.pipeline.arn}
    AWS_REGION       = ${var.aws_region}
    TF_STATE_BUCKET  = ${aws_s3_bucket.state.bucket}
  EOT
}
