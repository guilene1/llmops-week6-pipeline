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
#   ...:ref:refs/heads/main   pushes to main, schedules, manual dispatch on main
#   ...:pull_request          pull_request workflows
#
# GitHub writes the repository into the token's subject in one of two forms:
#
#   repo:OWNER/NAME:pull_request                         the long-standing form
#   repo:OWNER@OWNER_ID/NAME@REPO_ID:pull_request        with the permanent ids
#
# The second carries the ids of the account and the repository, which never change, so a
# repository deleted and re-created under the same name by someone else is not trusted.
# Repositories created recently get it by default. Both are accepted here. With the ids
# known (scripts/deploy.sh reads them from GitHub's public API), the second is pinned to
# exactly those ids; without them, any ids are accepted for this owner and name.
#
# Pull requests from forks never get here: GitHub does not issue an id-token to a
# pull_request run from a fork, and the workflows skip their AWS jobs for forks as well.
#
# If a workflow job sets `environment:`, GitHub changes the subject to
# ...:environment:NAME and this trust policy refuses it. The workflows in this repository
# do not use environments for that reason.
locals {
  github_owner = split("/", var.github_repository)[0]
  github_name  = split("/", var.github_repository)[1]
  github_with_ids = (var.github_owner_id != "" && var.github_repository_id != ""
    ? "${local.github_owner}@${var.github_owner_id}/${local.github_name}@${var.github_repository_id}"
  : "${local.github_owner}@*/${local.github_name}@*")

  github_subjects = flatten([
    for repo in [var.github_repository, local.github_with_ids] : [
      "repo:${repo}:ref:refs/heads/main",
      "repo:${repo}:pull_request",
    ]
  ])
}

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

    # StringLike, for the "@*" form when the ids are not known. Values without a "*" still
    # have to match exactly.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.github_subjects
    }
  }
}
