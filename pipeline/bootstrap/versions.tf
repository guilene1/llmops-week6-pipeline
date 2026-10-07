# The bootstrap is its own Terraform root, applied once, from a laptop, before anything else.
#
# It creates what the main stack and the pipeline need to exist first: the bucket the main
# stack keeps its state in, and the role GitHub Actions signs in as. It cannot keep its own
# state in a bucket it has not created yet, so its state stays local, in this folder.
# That state holds no secrets: a bucket, an OIDC provider, a role and three policies.
# Keep the file (or move it into the bucket afterwards, see README.md) so you can destroy
# these resources at the end.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.49, < 7.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      Component = "pipeline-bootstrap"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
