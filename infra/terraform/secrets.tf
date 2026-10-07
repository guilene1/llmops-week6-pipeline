# AWS Secrets Manager: database credentials.
#
#   admin secret   created by RDS itself (aurora.tf). Only the setup function reads it.
#   app secret     created here, for hr_app, the user the chat function connects as.
#                  Row-level security applies to hr_app, not to the admin.

resource "random_password" "db_app" {
  length  = 32
  special = false # letters and digits only: nothing to escape in a connection string
}

resource "aws_secretsmanager_secret" "db_app" {
  name                    = "${local.name}/db-app-user"
  description             = "Aurora credentials for hr_app, the application user"
  recovery_window_in_days = 0 # demo: delete at once on destroy, so the name can be reused
}

# Note: this value is also stored in the Terraform state file, so keep state private (see versions.tf).
resource "aws_secretsmanager_secret_version" "db_app" {
  secret_id = aws_secretsmanager_secret.db_app.id
  secret_string = jsonencode({
    username = "hr_app"
    password = random_password.db_app.result
  })
}

locals {
  db_admin_secret_arn = aws_rds_cluster.aurora.master_user_secret[0].secret_arn
}

# Langfuse keys, for tracing (lambda/app/tracing.py). Added for Week 6.
#
# Terraform creates the secret with placeholder values, and you put the real keys in
# yourself, so they never appear in code, in tfvars, or in the Terraform state:
#
#   aws secretsmanager put-secret-value --secret-id northwind-hr/langfuse \
#     --secret-string '{"public_key":"pk-lf-...","secret_key":"sk-lf-...","host":"https://us.cloud.langfuse.com"}'
#
# secret_string_wo is write-only: Terraform sends it once and does not keep it in state
# or compare it on the next plan, so your real keys are never overwritten or recorded.
# While the placeholders are there, tracing stays off and the assistant works as before.
resource "aws_secretsmanager_secret" "langfuse" {
  name                    = "${local.name}/langfuse"
  description             = "Langfuse public and secret key, and host, for tracing"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "langfuse" {
  secret_id = aws_secretsmanager_secret.langfuse.id
  secret_string_wo = jsonencode({
    public_key = "pk-lf-REPLACE_ME"
    secret_key = "sk-lf-REPLACE_ME"
    host       = "https://us.cloud.langfuse.com"
  })
  secret_string_wo_version = 1
}
