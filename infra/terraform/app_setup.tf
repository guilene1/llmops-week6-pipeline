# The steps after the infrastructure, which the deployment guide has you type by hand:
#
#   step 3   create the schema, the security policies and the demo data   (setup-db)
#   step 4   load the policy documents and wait until they are indexed
#   step 5   build the React app and publish it behind CloudFront
#   step 6   give the demo users a password                               (cognito.tf)
#
# With setup_application = true (the default), `terraform apply` does all four, so a
# stack is usable the moment the apply ends. Added for Week 6, where the subject is the
# pipeline, not the deployment. The guide still describes each step, and the scripts
# these call can be run on their own.
#
# Steps 4 and 5 need the AWS CLI, and step 5 needs Node: the same tools the guide uses.
# They run where Terraform runs, so run apply from Git Bash on Windows.
#
# If a step fails (a database still waking, an index not yet accepting writes), the
# apply stops with the step's own error. Run apply again: a failed step is retried, a
# finished one is not.

locals {
  setup_count = var.setup_application ? 1 : 0
  repo_root   = abspath("${path.module}/../..")

  # Hash file contents with line endings normalised, so a Windows checkout (CRLF) and the
  # Linux CI runner (LF) agree, and a plan in the pipeline does not see a change that is
  # only line endings.
  sql_files = sort(fileset("${local.repo_root}/lambda/db", "*.sql"))
  sql_hash  = sha256(join("", [for f in local.sql_files : sha256(replace(file("${local.repo_root}/lambda/db/${f}"), "\r\n", "\n"))]))
  frontend_files = sort(concat(
    tolist(fileset("${local.repo_root}/frontend", "src/**")),
    ["index.html", "package.json", "package-lock.json", "vite.config.js"],
  ))
  frontend_hash = sha256(join("", [for f in local.frontend_files : sha256(replace(file("${local.repo_root}/frontend/${f}"), "\r\n", "\n"))]))

  # What the browser needs to find Cognito. Public: it ends up in the JavaScript bundle.
  frontend_settings = {
    VITE_AUTH_MODE         = "cognito"
    VITE_COGNITO_AUTHORITY = "https://${aws_cognito_user_pool.main.endpoint}"
    VITE_COGNITO_CLIENT_ID = aws_cognito_user_pool_client.web.id
    VITE_COGNITO_DOMAIN    = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
  }
}

# Step 3: tables, row-level security, demo data. setup-db is safe to run twice, so this
# runs again whenever the SQL in lambda/db changes, and not otherwise.
resource "aws_lambda_invocation" "setup_db" {
  count         = local.setup_count
  function_name = aws_lambda_function.app["setup-db"].function_name
  input         = jsonencode({})

  triggers = {
    sql = local.sql_hash
  }

  depends_on = [
    aws_rds_cluster_instance.aurora,
    aws_secretsmanager_secret_version.db_app,
    aws_vpc_endpoint.interface, # the function reads its passwords through this
    aws_iam_role_policy.setup_db,
  ]
}

# Step 4: documents, once per bucket. After the first load the pipeline keeps the index in
# step with the repository (pipeline/reindex.py), so later document changes are not
# Terraform's to make. Re-run by hand with: bash scripts/load-documents.sh <bucket>
resource "terraform_data" "documents" {
  count            = local.setup_count
  triggers_replace = [aws_s3_bucket.documents.id]

  provisioner "local-exec" {
    working_dir = local.repo_root
    interpreter = ["bash", "-c"]
    command     = "bash scripts/load-documents.sh '${aws_s3_bucket.documents.bucket}' '${var.project_name}' '${var.aws_region}'"
  }

  depends_on = [
    aws_s3_bucket_notification.documents, # the manifest upload is what starts ingestion
    aws_lambda_permission.ingest_from_s3,
    aws_opensearchserverless_collection.policies,
    aws_opensearchserverless_access_policy.data,
    aws_opensearchserverless_security_policy.network,
    aws_vpc_endpoint.aoss_data,
    aws_iam_role_policy.ingest,
    aws_cloudwatch_log_group.lambda,
  ]
}

# Step 5: the React app. Rebuilt and republished when its source or its Cognito settings
# change.
resource "terraform_data" "frontend" {
  count            = local.setup_count
  triggers_replace = [local.frontend_hash, jsonencode(local.frontend_settings), aws_s3_bucket.frontend.id]

  provisioner "local-exec" {
    working_dir = local.repo_root
    interpreter = ["bash", "-c"]
    command     = "bash scripts/publish-frontend.sh '${aws_s3_bucket.frontend.bucket}' '${aws_cloudfront_distribution.app.id}'"
    environment = local.frontend_settings
  }

  depends_on = [
    aws_s3_bucket_policy.frontend,
    aws_cognito_user_pool_client.web,
    aws_cognito_user_pool_domain.main,
  ]
}
