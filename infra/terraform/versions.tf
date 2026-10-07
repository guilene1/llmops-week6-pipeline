# Which Terraform and which providers this project needs, and where AWS resources go.

terraform {
  # 1.10 is the first release with use_lockfile on the S3 backend, and 1.11 the first
  # with write-only arguments, which keep the Langfuse keys out of state (secrets.tf)
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.49 is the first release with generation = "NEXTGEN" on an OpenSearch
      # Serverless collection group, which is what lets the collection scale to zero.
      version = ">= 6.49, < 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State lives in S3, so your laptop and the GitHub Actions pipeline work on the same
  # stack. The bucket is created by pipeline/bootstrap. Its name contains your account id,
  # so it is not written here: it comes from backend.hcl at init time (a partial
  # configuration). backend.hcl is not committed.
  #
  #   terraform -chdir=pipeline/bootstrap output -raw backend_config > infra/terraform/backend.hcl
  #   terraform -chdir=infra/terraform init -backend-config=backend.hcl
  #
  # use_lockfile writes a .tflock object next to the state, so two applies cannot run at once.
  backend "s3" {
    key          = "northwind-hr/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  # Every resource gets these tags, so the whole demo is easy to find in the console and in the bill
  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}

# CloudFront is global, and its WAF must be created in us-east-1, whatever region the rest uses
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}
