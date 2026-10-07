# The bucket the main stack keeps its Terraform state in.
#
#   versioned    every apply keeps the previous state, so a bad apply can be undone
#   encrypted    the state holds the hr_app database password (secrets.tf) in plain text
#   private      no public access, and HTTPS only
#
# Locking needs no DynamoDB table: the S3 backend's use_lockfile writes a .tflock object
# beside the state.

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "${var.project_name}-tfstate-${local.account_id}"
  state_key    = "${var.project_name}/terraform.tfstate"
}

resource "aws_s3_bucket" "state" {
  bucket        = local.state_bucket
  force_destroy = var.allow_state_bucket_destroy
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "HttpsOnly"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}
