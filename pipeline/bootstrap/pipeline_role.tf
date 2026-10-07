# The one role GitHub Actions uses. Three policies, each for one job:
#
#   ship              push code to the four functions, invoke them, read and write the
#                     documents bucket, read and write the Terraform state, and call
#                     Nova Lite for the optional grounding judge
#   terraform-read    everything `terraform plan` reads to refresh this stack
#   terraform-config  the few writes a configuration change needs: function settings
#                     (thresholds, model ids), the chat and evaluate model permissions,
#                     and a new dependency layer
#
# What it deliberately cannot do: create or delete anything else. A pull request that adds
# a resource, changes the network or touches the database fails at apply with AccessDenied.
# That is intended. Structural changes are applied from a laptop, by a person, with the
# credentials the deployment guide uses. The pipeline applies configuration.
#
# Read actions use Describe*, Get* and List* where Terraform's refresh calls more of them
# than is worth listing, and those calls change with provider versions. They are scoped to
# this stack's resources wherever the service supports resource-level permissions. Write
# actions are listed one by one.

locals {
  region = var.aws_region
  p      = var.project_name

  function_names = ["chat", "ingest", "setup-db", "evaluate"]

  function_arns = [for f in local.function_names :
  "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.p}-${f}"]

  # The roles the main stack creates for the functions (infra/terraform/iam.tf)
  function_role_arns = [for f in local.function_names :
  "arn:aws:iam::${local.account_id}:role/${local.p}-${f}"]

  layer_arn          = "arn:aws:lambda:${local.region}:${local.account_id}:layer:${local.p}-dependencies"
  layer_version_arns = "${local.layer_arn}:*"

  # bucket_prefix in infra/terraform/s3.tf adds a random suffix, hence the wildcard
  documents_bucket_arn = "arn:aws:s3:::${local.p}-documents-*"
  frontend_bucket_arn  = "arn:aws:s3:::${local.p}-frontend-*"

  log_group_arns = [
    "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.p}-*",
    "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/apigateway/${local.p}-*",
  ]
}

resource "aws_iam_role" "pipeline" {
  name                 = "${local.p}-github-pipeline"
  description          = "Assumed by GitHub Actions in ${var.github_repository} through OIDC"
  assume_role_policy   = data.aws_iam_policy_document.github_trust.json
  max_session_duration = 3600
}

# ---------------------------------------------------------------------------
# ship: deploy code, run the functions, load documents, keep state
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ship" {
  statement {
    sid = "DeployAndInvokeFunctions"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:InvokeFunction",
    ]
    resources = local.function_arns
  }

  statement {
    sid       = "ListDocumentsBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [local.documents_bucket_arn]
  }

  statement {
    sid       = "ReadWriteDocuments"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.documents_bucket_arn}/*"]
  }

  # Read the function logs, so a failed evaluation or a re-index can be reported in the run
  statement {
    sid       = "ReadFunctionLogs"
    actions   = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"]
    resources = [for arn in local.log_group_arns : "${arn}:*"]
  }

  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }

  statement {
    sid       = "ReadWriteState"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.state_key}"]
  }

  statement {
    sid       = "StateLock"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.state_key}.tflock"]
  }

  # The pipeline's own records, beside the Terraform state: which documents and indexing
  # code the policy index was last built from (pipeline/reindex.py)
  statement {
    sid       = "PipelineRecords"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.p}/pipeline/*"]
  }

  # The optional grounding judge (pipeline/judge.py) calls Nova Lite through Converse
  statement {
    sid       = "JudgeModel"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${local.region}::foundation-model/amazon.nova-lite-v1:0"]
  }
}

# ---------------------------------------------------------------------------
# terraform-read: what a plan reads to refresh every resource in infra/terraform
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "terraform_read" {
  # EC2 and RDS describe calls do not support resource-level permissions
  statement {
    sid       = "NetworkAndDatabase"
    actions   = ["ec2:Describe*", "rds:Describe*", "rds:ListTagsForResource"]
    resources = ["*"]
  }

  statement {
    sid = "Functions"
    actions = [
      "lambda:Get*",
      "lambda:List*",
    ]
    resources = concat(local.function_arns, [local.layer_arn, local.layer_version_arns])
  }

  statement {
    sid = "FunctionRoles"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
    ]
    resources = local.function_role_arns
  }

  # Refreshing aws_secretsmanager_secret_version reads the value. Only the secrets this
  # stack creates, under northwind-hr/. The RDS-managed admin secret (rds!cluster-...)
  # is not covered, so the pipeline can never read the database admin password.
  statement {
    sid = "StackSecrets"
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:GetSecretValue",
    ]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:${local.p}/*"]
  }

  statement {
    sid = "StackBuckets"
    actions = [
      "s3:ListBucket",
      "s3:GetBucket*",
      "s3:GetAccelerateConfiguration",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration",
    ]
    resources = [local.documents_bucket_arn, local.frontend_bucket_arn]
  }

  statement {
    sid       = "VectorStore"
    actions   = ["aoss:BatchGet*", "aoss:Get*", "aoss:List*"]
    resources = ["*"]
  }

  statement {
    sid       = "Guardrail"
    actions   = ["bedrock:GetGuardrail", "bedrock:ListGuardrails", "bedrock:ListTagsForResource"]
    resources = ["arn:aws:bedrock:${local.region}:${local.account_id}:guardrail/*"]
  }

  statement {
    sid       = "LogGroups"
    actions   = ["logs:DescribeLogGroups", "logs:ListTagsForResource", "logs:ListTagsLogGroup"]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:*"]
  }

  statement {
    sid       = "IngestSchedule"
    actions   = ["events:DescribeRule", "events:ListTargetsByRule", "events:ListTagsForResource"]
    resources = ["arn:aws:events:${local.region}:${local.account_id}:rule/${local.p}-*"]
  }

  statement {
    sid = "Alarms"
    actions = [
      "sns:GetTopicAttributes",
      "sns:GetSubscriptionAttributes",
      "sns:ListTagsForResource",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:ListTagsForResource",
    ]
    resources = [
      "arn:aws:sns:${local.region}:${local.account_id}:${local.p}-*",
      "arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:${local.p}-*",
    ]
  }

  # DescribeUserPoolDomain takes no resource ARN, so this one is account wide.
  # AdminGetUser is how Terraform refreshes the demo users. It reads attributes, not passwords.
  statement {
    sid = "SignIn"
    actions = [
      "cognito-idp:DescribeUserPool",
      "cognito-idp:DescribeUserPoolClient",
      "cognito-idp:DescribeUserPoolDomain",
      "cognito-idp:GetUserPoolMfaConfig",
      "cognito-idp:AdminGetUser",
      "cognito-idp:ListTagsForResource",
    ]
    resources = ["*"]
  }

  statement {
    sid     = "Api"
    actions = ["apigateway:GET"]
    resources = [
      "arn:aws:apigateway:${local.region}::/apis",
      "arn:aws:apigateway:${local.region}::/apis/*",
      "arn:aws:apigateway:${local.region}::/tags/*",
    ]
  }

  # CloudFront, its WAF, and the optional custom domain are global or us-east-1 services
  # whose read calls take no resource ARN.
  statement {
    sid = "EdgeAndDomain"
    actions = [
      "cloudfront:Get*",
      "cloudfront:List*",
      "wafv2:Get*",
      "wafv2:List*",
      "acm:DescribeCertificate",
      "acm:ListCertificates",
      "acm:ListTagsForCertificate",
      "route53:GetHostedZone",
      "route53:GetChange",
      "route53:ListHostedZones",
      "route53:ListHostedZonesByName",
      "route53:ListResourceRecordSets",
      "route53:ListTagsForResource",
    ]
    resources = ["*"]
  }
}

# ---------------------------------------------------------------------------
# terraform-config: the writes a configuration change needs, and nothing more
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "terraform_config" {
  # Thresholds, model ids and other settings are environment variables (lambda.tf)
  statement {
    sid       = "UpdateFunctionSettings"
    actions   = ["lambda:UpdateFunctionConfiguration"]
    resources = local.function_arns
  }

  # Changing chat_model_id or fallback_chat_model_id rewrites the chat and evaluate
  # roles' inline policy (iam.tf), so the functions may call the new model.
  statement {
    sid       = "UpdateFunctionPermissions"
    actions   = ["iam:PutRolePolicy"]
    resources = local.function_role_arns
  }

  # Updating a function's configuration can pass its existing role back to Lambda.
  # Only these four roles, and only to Lambda.
  statement {
    sid       = "PassFunctionRoles"
    actions   = ["iam:PassRole"]
    resources = local.function_role_arns
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  # A change to lambda/requirements.txt produces a new layer version
  statement {
    sid       = "PublishDependencyLayer"
    actions   = ["lambda:PublishLayerVersion", "lambda:DeleteLayerVersion"]
    resources = [local.layer_arn, local.layer_version_arns]
  }
}

# ---------------------------------------------------------------------------
# Attach. Managed policies rather than inline, because together they are larger than
# the 10 KB a role may hold inline.
# ---------------------------------------------------------------------------

locals {
  pipeline_policies = {
    ship             = data.aws_iam_policy_document.ship.json
    terraform-read   = data.aws_iam_policy_document.terraform_read.json
    terraform-config = data.aws_iam_policy_document.terraform_config.json
  }
}

resource "aws_iam_policy" "pipeline" {
  for_each    = local.pipeline_policies
  name        = "${local.p}-github-pipeline-${each.key}"
  description = "GitHub Actions pipeline: ${each.key}"
  policy      = each.value
}

resource "aws_iam_role_policy_attachment" "pipeline" {
  for_each   = aws_iam_policy.pipeline
  role       = aws_iam_role.pipeline.name
  policy_arn = each.value.arn
}
