# GitHub OIDC: how a workflow signs in to AWS with no stored key.
#
# Each workflow run can ask GitHub for a short-lived signed token that says which
# repository, branch or pull request it is running for. AWS checks the signature against
# this provider and, if the role's trust policy accepts what the token says, hands back
# credentials that expire within the hour. Nothing long-lived is stored in GitHub.

locals {
  github_oidc_url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = local.github_oidc_url
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint: AWS verifies GitHub's certificate against its own trusted CAs.
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = local.github_oidc_url
}

locals {
  github_oidc_provider_arn = one(concat(
    aws_iam_openid_connect_provider.github[*].arn,
    data.aws_iam_openid_connect_provider.github[*].arn,
  ))
}

# Who may assume the pipeline role. Two kinds of run, from one repository, and nothing else:
#
#   repo:OWNER/NAME:ref:refs/heads/main   pushes to main, schedules, manual dispatch on main
#   repo:OWNER/NAME:pull_request          pull_request workflows
#
# Pull requests from forks never get here: GitHub does not issue an id-token to a
# pull_request run from a fork, and the workflows skip their AWS jobs for forks as well.
#
# If a workflow job sets `environment:`, GitHub changes the subject to
# repo:OWNER/NAME:environment:NAME and this trust policy refuses it. The workflows in this
# repository do not use environments for that reason.
data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repository}:ref:refs/heads/main",
        "repo:${var.github_repository}:pull_request",
      ]
    }
  }
}
