# pipeline/bootstrap

Run once, from your laptop, before you deploy the stack. It creates three things the rest
of the week depends on:

| Resource | Why |
|---|---|
| S3 bucket `northwind-hr-tfstate-<account id>` | Terraform state for `infra/terraform`, so the pipeline and your laptop see the same stack. Versioned, encrypted, private, HTTPS only. Locking uses `use_lockfile`, so there is no DynamoDB table |
| GitHub OIDC provider | Lets a workflow run sign in to AWS with a token that expires within the hour. No AWS key is stored in GitHub |
| IAM role `northwind-hr-github-pipeline` | What the workflows sign in as. Trusted only by your repository's `main` branch and its pull requests |

It is a separate Terraform root because the main stack cannot create the bucket its own
state lives in. Its own state stays in this folder, as `terraform.tfstate`. Keep that file:
you need it to destroy these resources at the end.

## What the role may do

Three policies, written out in [`pipeline_role.tf`](pipeline_role.tf):

| Policy | Allows |
|---|---|
| `ship` | Update and invoke the four functions. Read and write the documents bucket. Read their logs. Read and write the Terraform state, its lock, and the pipeline's record of what the index was built from. Call Nova Lite for the optional grounding judge |
| `terraform-read` | Read every resource in `infra/terraform`, which is what `terraform plan` does to refresh |
| `terraform-config` | Change function settings, rewrite the chat and evaluate model permissions, publish a new dependency layer |

**Who it trusts.** GitHub names the repository in its token in one of two forms: the
long-standing `repo:OWNER/NAME:pull_request`, or, for newer repositories,
`repo:OWNER@OWNER_ID/NAME@REPO_ID:pull_request`, with the permanent ids of the account and
the repository. The role accepts both. `scripts/deploy.sh` reads the two ids from GitHub's
public API and pins them, so a repository deleted and re-created under the same name by
someone else is not trusted. If the role is ever refused ("Not authorized to perform
sts:AssumeRoleWithWebIdentity"), CloudTrail's `AssumeRoleWithWebIdentity` events show what
the token said, in `userIdentity.userName`.

That last policy is the whole of what the pipeline can change. Thresholds, model ids and
anything else held in an environment variable can be applied from a pull request. A pull
request that adds a resource, or changes the network, the database, the guardrail or
access control, plans successfully and then fails at apply with `AccessDenied`. That is
intended: structural changes are applied from a laptop, by a person.

It cannot read the database admin password, which RDS keeps in its own secret outside the
`northwind-hr/` prefix.

## Steps

You need the same AWS credentials the deployment guide uses (an administrator, because this
creates IAM).

```bash
cp pipeline/bootstrap/terraform.tfvars.example pipeline/bootstrap/terraform.tfvars
# set github_repository to your-username/llmops-week6-pipeline

terraform -chdir=pipeline/bootstrap init
terraform -chdir=pipeline/bootstrap apply
```

Thirteen resources, in under a minute.

If the apply fails with `EntityAlreadyExists` on the OIDC provider, your account already has
one for GitHub. Set `create_oidc_provider = false` and apply again.

### Point the main stack at the bucket

```bash
terraform -chdir=pipeline/bootstrap output -raw backend_config > infra/terraform/backend.hcl
terraform -chdir=infra/terraform init -backend-config=backend.hcl
```

`backend.hcl` holds the bucket name and region. It is not secret, but it is specific to your
account, so it is not committed. From here, follow
[`docs/deployment-guide.md`](../../docs/deployment-guide.md) as written.

**Already deployed with local state?** Run the init with `-migrate-state` and answer `yes`.
Terraform copies the local state into the bucket. Delete the local `terraform.tfstate`
afterwards, so there is only one copy that could be applied.

### Tell GitHub

```bash
terraform -chdir=pipeline/bootstrap output -raw github_variables
```

In your repository, **Settings > Secrets and variables > Actions > Variables**, add each of
those three as a repository **variable**. They are identifiers, not credentials: without a
token from a workflow in your repository, the role ARN is useless.

## Check it

```bash
BUCKET=$(terraform -chdir=pipeline/bootstrap output -raw state_bucket)

aws s3api get-bucket-versioning --bucket "$BUCKET"          # "Status": "Enabled"
aws s3api get-bucket-encryption --bucket "$BUCKET" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault'
aws iam get-role --role-name northwind-hr-github-pipeline \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition'
```

After the main stack's first apply, the state is in the bucket:

```bash
aws s3 ls "s3://$BUCKET/northwind-hr/"                      # terraform.tfstate
```

## Teardown

Last, after `infra/terraform` has been destroyed, because the state bucket holds its state:

```bash
terraform -chdir=pipeline/bootstrap apply -var allow_state_bucket_destroy=true
terraform -chdir=pipeline/bootstrap destroy -var allow_state_bucket_destroy=true
```

The first command only flips `force_destroy` on the bucket, which is what lets the second
delete a versioned bucket that still holds old state versions.
