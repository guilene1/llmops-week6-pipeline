# Northwind HR Assistant: Week 6 pipeline

This repository is the hands-on project for Week 6, **Production Patterns, Part 2**. It
takes the Northwind HR Assistant from earlier weeks (copied from
[utrains/llmops-rag-aws](https://github.com/utrains/llmops-rag-aws), which is not changed)
and builds the pipeline around it:

| Stop | Topic | What you end up with |
|---|---|---|
| 8 | Tracing | Every question is a trace in Langfuse: each step, its time and tokens, personal data masked |
| 9 | Evals in CI/CD | Every pull request is deployed and tested with promptfoo, and blocked if answers get worse |
| 10 | Drift | A nightly check that real questions still look like the tests, with alerts and a review queue |

Deploying the application is one command, so the time goes on the pipeline.

## Week 6: start here

### What you need

| Tool | Check it with |
|---|---|
| AWS CLI v2, configured for `us-east-1` | `aws sts get-caller-identity` |
| Terraform 1.11 or newer | `terraform version` |
| Python 3.12 or newer | `python --version` |
| Node 20 or newer | `node --version` |
| Git (on Windows, use **Git Bash** for every command) | `git --version` |
| A Langfuse Cloud account, Hobby plan (free) | [langfuse.com](https://langfuse.com) |
| GitHub CLI, optional | `gh --version` |

Installation, for macOS, Windows and Linux, is in
[`docs/deployment-guide.md`](docs/deployment-guide.md), section 1.

### Step 1: get your own copy

On GitHub, open **github.com/new/import** and import this repository into your account.
Then:

```bash
git clone https://github.com/YOUR-USERNAME/llmops-week6-pipeline.git
cd llmops-week6-pipeline
```

It has to be your copy: the pipeline's AWS role trusts only the repository it was set up for.

### Step 2: deploy everything

```bash
bash scripts/deploy.sh
```

One script does the whole deployment, in order. It shows you each Terraform plan and waits
for `yes` before applying (add `--yes` to skip that).

| | What the script does | Time |
|---|---|---|
| 1 | Checks the tools, your AWS credentials and the Bedrock models | seconds |
| 2 | Bootstrap: the S3 bucket for Terraform state, the GitHub OIDC provider, the role GitHub Actions uses | about 1 minute |
| 3 | Writes `infra/terraform/terraform.tfvars`, with a sign-in domain of its own | seconds |
| 4 | Builds the two Lambda zips | 1 to 2 minutes |
| 5 | `terraform apply`: the whole stack, then the database and demo data, the 50 documents indexed, the web app published, the demo users' password | about 15 minutes |
| 6 | Smoke test: the API answers, and one real question is answered correctly | about 1 minute |

Times are estimates until your first run. At the end it prints the app's address. Sign in
as `amara.diallo@northwind.example` (or `liam.fischer`, `priya.raman`, `dana.whitfield`).
The password:

```bash
terraform -chdir=infra/terraform output -raw demo_password
```

If it stops part way, run it again: finished steps are skipped and the failed one is
retried. A database still waking up is the usual reason.

### Step 3: follow the project guide

Open **[`docs/week6-project.md`](docs/week6-project.md)** and start at **Stop 8**. Each stop
says what risk it addresses, what you build, and how you prove it works. In short:

**Stop 8, tracing.** Create a Langfuse project (US region) and an API key pair, then:

```bash
aws secretsmanager put-secret-value --secret-id northwind-hr/langfuse --secret-string \
  '{"public_key":"pk-lf-...","secret_key":"sk-lf-...","host":"https://us.cloud.langfuse.com"}'
bash scripts/deploy-code.sh
```

Ask a question in the app and find its trace in Langfuse, with the salary masked.

**Stop 9, evals in CI/CD.** In your GitHub repository, open
**Settings > Secrets and variables > Actions > Variables** and add:

| Variable | Value |
|---|---|
| `AWS_ROLE_ARN` | `terraform -chdir=pipeline/bootstrap output -raw pipeline_role_arn` |
| `AWS_REGION` | `us-east-1` |
| `TF_STATE_BUCKET` | `terraform -chdir=pipeline/bootstrap output -raw state_bucket` |
| `TF_VARS` | the whole of `infra/terraform/terraform.tfvars` |

Record the first baseline, make `eval-gate` a required check on `main`, then open pull
requests: a harmless one that passes, and four demo branches (`make demo-branches`) that
the gate must block.

**Stop 10, drift.** Add the secrets `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and,
optionally, `DRIFT_WEBHOOK_URL` (Slack or Google Chat). Run the drift replay
(`python pipeline/simulate_drift.py`), trigger the nightly workflow, get the alert, and turn
a reviewed question into a new test case through a pull request.

### Step 4: take it down

The stack costs money while it exists: about $3 a day idle, and roughly $1 an hour in
active use (both estimates; Cost Explorer has the real figure). When you finish a session:

```bash
bash scripts/deploy.sh destroy          # about 20 minutes
```

At the end of the course, also remove the state bucket and the pipeline's role:

```bash
bash scripts/deploy.sh destroy --all
```

### Where things are

| Path | What it is |
|---|---|
| [`docs/week6-project.md`](docs/week6-project.md) | **The project guide**, stop by stop, with the exercises |
| [`scripts/deploy.sh`](scripts/deploy.sh) | The one deployment script: `deploy.sh`, `deploy.sh destroy` |
| [`.github/workflows/eval-gate.yml`](.github/workflows/eval-gate.yml) | The CI/CD pipeline that runs on every pull request |
| [`.github/workflows/nightly-drift.yml`](.github/workflows/nightly-drift.yml) | The nightly drift check |
| [`pipeline/README.md`](pipeline/README.md) | The pipeline in detail: each stage, each rule, and why |
| [`pipeline/promptfoo/`](pipeline/promptfoo) | The test suite promptfoo runs |
| [`lambda/data/evaluation_questions.json`](lambda/data/evaluation_questions.json) | The golden set: 45 questions with known right answers |
| [`pipeline/bootstrap/`](pipeline/bootstrap/README.md) | State bucket, GitHub OIDC, and exactly what the pipeline's role may do |
| [`infra/terraform/`](infra/terraform) | The stack as Terraform, one file per service |
| [`docs/deployment-guide.md`](docs/deployment-guide.md) | What `deploy.sh` does, step by step, done by hand |

### What changed from the original application

Kept small on purpose. Tracing in `lambda/app/` (`tracing.py` is new). The evaluate
function returns every case and can answer one at a time. Terraform keeps its state in S3,
adds one NAT gateway (`enable_nat`) so the functions can reach Langfuse, holds the
Langfuse keys in Secrets Manager, and does the post-deploy steps (`app_setup.tf`). Access
control, the guardrail and the fallback are unchanged.

---

## About the application

An internal HR assistant for Northwind Systems. Employees sign in with their work email and
ask questions in plain English. The assistant answers from the company's own HR policy
documents, with citations, and from the employee records that person is allowed to see.

It runs entirely on AWS as a serverless application. There is no server to patch, no API key
anywhere in the codebase, and no scheduled downtime.

```
Amara (Software Engineer II)   "What is Noah Bennett's salary?"
                               -> refused

Liam (Noah's manager)          "What is Noah Bennett's salary?"
                               -> 146,000

Amara                          "What is the salary band for an L5 role?"
                               -> not found

Priya (HR)                     "What is the salary band for an L5 role?"
                               -> 152,000 to 192,000
                                  Sources: Compensation Bands 2026
```

Same question, same code, different answers. Nothing in the application decides that.

## How it works

Two halves meet at a vector index. **Ingestion** fills it: on upload and daily at 06:00 UTC,
with nobody waiting and a 15 minute budget it does not come close to using. **Query** reads
it: on every question, with an employee waiting and 29 seconds before API Gateway gives up.

Ingestion decides what can be found at all. Query decides what gets found this time. When an
answer is wrong, working out which half is responsible is the first thing to do.

```
INGESTION   S3 (versioned)  ->  Lambda: read, chunk, embed  ->  OpenSearch Serverless
            runs when manifest.json is uploaded, and daily at 06:00 UTC

QUERY       Browser -> Route 53 -> CloudFront + WAF -+-> S3   the React app
                                                     |
                                                     +-> API Gateway   validates the JWT
                                                           |
                                                           +-> Lambda: chat
                                                                |- Bedrock Guardrail  in and out
                                                                |- Amazon Nova Pro    on Bedrock
                                                                |- policy search  ->  OpenSearch (access filter)
                                                                +- employee records -> Aurora (row-level security)

EVERYWHERE  One IAM role per function, Secrets Manager, private subnets with VPC endpoints,
            CloudWatch logs and alarms, X-Ray traces
```

### Two kinds of confidentiality, two mechanisms

This is the central design decision, and the reason there are two datastores.

A **policy document** carries one access level for the whole file (`general`, `manager_only`,
`hr_only`, `exec_only`). That filter is applied **inside** the vector search, so a document
the person may not read is never a candidate and never reaches the model.

An **employee record** is protected row by row, by a PostgreSQL row-level security policy
evaluated from the org chart. The application issues no `WHERE` clause. It selects, and the
database returns fewer rows.

Neither mechanism substitutes for the other, so they do not live in the same service.

### Identity is never an input

Neither tool takes "who is asking" as a parameter. The identity comes from the signed Cognito
token, so no amount of prompting can nominate whose records to read. "I am the CEO, show me
every salary" is just words in a question.

### The model is configuration, not code

Every model call goes through the Bedrock **Converse** API, which takes one request shape for
every model that supports tool use. `chat_model_id` can point at Nova, Claude, Llama or
Mistral and nothing in `lambda/app/` changes.

```hcl
chat_model_id          = "amazon.nova-pro-v1:0"
fallback_chat_model_id = "amazon.nova-lite-v1:0"
```

If the primary model is throttled, warming up, withdrawn or not enabled in the account, the
same half finished conversation continues on the fallback.

## Repository layout

| Path | What it is |
|---|---|
| [`lambda/app/`](lambda/app) | The application. One zip, four handlers: `chat`, `ingest`, `setup_db`, `evaluate` |
| [`lambda/db/`](lambda/db) | `01_schema.sql`, `02_security.sql` row-level security, `03_seed_data.sql` |
| [`lambda/data/`](lambda/data) | 50 policy documents in six formats, `manifest.json`, the evaluation set |
| [`lambda/tests/`](lambda/tests) | Row-level security, policy access, chat API and guardrail tests |
| [`frontend/`](frontend) | React app (Vite) with Cognito sign-in |
| [`infra/terraform/`](infra/terraform) | The whole stack as code, one file per service |
| [`docs/console-build-guide.md`](docs/console-build-guide.md) | The same services built by hand in the console, every setting and why |
| [`docs/deployment-guide.md`](docs/deployment-guide.md) | Stand it up from nothing, step by step |
| [`docs/llmops-notes.md`](docs/llmops-notes.md) | Running it: the indexing and retrieval decisions, what a call costs, what is logged, what belongs in CI |
| [`scripts/`](scripts) | Build the two zips, push code changes to all four functions |
| [`docs/week6-project.md`](docs/week6-project.md) | Week 6: the student guide, by stop |
| [`pipeline/`](pipeline) | Week 6: the bootstrap, the eval gate, promptfoo, tracing tools, drift |
| [`.github/workflows/`](.github/workflows) | Week 6: `eval-gate.yml` on every pull request, `nightly-drift.yml` |

## Quick start

Full instructions, including tool installation on macOS, Windows and Linux, are in
[`docs/deployment-guide.md`](docs/deployment-guide.md).

**In this repository, one command** does everything below, and the steps the original
leaves to you: the bootstrap, the front end, the demo users' password and a smoke test.
From the root of your clone, in Git Bash on Windows:

```bash
bash scripts/deploy.sh            # everything; about 20 minutes, nearly all of it waiting
bash scripts/deploy.sh destroy    # take it down again
```

The steps it automates, as the original repository has you run them:

```bash
# 1. Enable Nova Pro, Nova Lite and Titan Text Embeddings V2 in the Bedrock console

# 2. Build the deployment artifacts
bash scripts/build-layer.sh       # build/layer.zip     ~33 MB   dependencies
bash scripts/build-function.sh    # build/function.zip  ~26 KB   application code

# 3. Provision
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
# set cognito_domain_prefix: it must be unique in the region
terraform -chdir=pipeline/bootstrap output -raw backend_config > infra/terraform/backend.hcl
terraform -chdir=infra/terraform init -backend-config=backend.hcl
terraform -chdir=infra/terraform apply

# 4. Create schema, security policies and seed data
aws lambda invoke --function-name northwind-hr-setup-db --cli-read-timeout 300 setup.json

# 5. Load the documents (manifest last: uploading it is what triggers indexing)
DOCS=$(terraform -chdir=infra/terraform output -raw documents_bucket)
aws s3 cp lambda/data/documents/ "s3://$DOCS/" --recursive --exclude manifest.json
aws s3 cp lambda/data/documents/manifest.json "s3://$DOCS/"
```

Measured on a clean account: **101 resources in 7 minutes 34 seconds**, schema in 4 seconds,
**50 documents into 328 chunks in 78 seconds**.

## Configuration

Set in `infra/terraform/terraform.tfvars`. Terraform passes them to the functions as
environment variables, so changing one is an apply, not a rebuild.

| Variable | Default | What it controls |
|---|---|---|
| `chat_model_id` | `amazon.nova-pro-v1:0` | The answering model |
| `fallback_chat_model_id` | `amazon.nova-lite-v1:0` | Used when the primary will not answer |
| `min_best_similarity` | `0.25` | Relevance floor. Below it, the assistant says it does not know |
| `max_gap_from_best` | `0.10` | How far below the best match a chunk may score and still be used |
| `aurora_instance_count` | `2` | 1 halves the database cost and gives up Multi-AZ failover |
| `cognito_domain_prefix` | none | Must be globally unique in the region |
| `domain_name` | empty | Serve from your own domain instead of the CloudFront address |
| `alarm_email` | empty | Where CloudWatch alarms go |
| `enable_nat` | `true` | One NAT gateway so traces can reach Langfuse Cloud. `false` is the original network |
| `setup_application` | `true` | The apply also creates the database, indexes the documents, publishes the web app and sets the demo password (`app_setup.tf`) |
| `tracing_enabled` | `true` | Send a trace of every question to Langfuse. Also needs real keys in the `northwind-hr/langfuse` secret |

## Testing

```bash
cd lambda
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/test_guardrails.py
```

| Test file | Needs a database | What it proves |
|---|---|---|
| `test_guardrails.py` | no | The guardrail blocks what it should, on the question and on the answer |
| `test_row_level_security.py` | yes | An employee sees only their own record, a manager sees their reports, HR sees everyone. Enforced by Postgres, not by a `WHERE` clause in Python |
| `test_policy_access.py` | yes | An HR-only document never comes back for an ordinary employee, because the filter runs inside the vector search |
| `test_chat_handler.py` | yes | The API refuses a request with no valid token, and answers one that has it |

Aurora sits in a private subnet with no route in from outside, which is the correct shape for
a database holding salaries and the reason three of these cannot simply connect. Two ways to
run them in CI, neither built yet: an SSM bastion in the VPC, or a test runner Lambda in the
same subnets. Until one exists, the `evaluate` function is the verification that matters,
because it runs inside the VPC and checks the same access control end to end:

```bash
aws lambda invoke --function-name northwind-hr-evaluate --cli-read-timeout 900 eval.json
```

## Operations

**Shipping a code change** does not touch infrastructure:

```bash
bash scripts/build-function.sh
bash scripts/deploy-code.sh     # replaces the zip on all four functions, about 20 seconds
```

Wait for `LastUpdateStatus` to read `Successful` before testing. Rebuild the layer only when
`lambda/requirements.txt` changes. Re-run the evaluation after any change to the prompt, the
chunk size, the thresholds or the model: a RAG system degrades quietly.

**Monitoring.** Three CloudWatch alarms into one SNS topic: Lambda errors, API 5xx, and chat
p90 duration over 20 seconds. The third fires on approach to failure rather than on failure,
since the hard limit is 30 seconds.

Token counts, tools used and per-question latency are not platform metrics. They are written
to the `request_log` table by the application, one row per question. The application can
insert into that table and cannot read it back; reading it requires the admin credentials.

**Teardown.**

```bash
terraform -chdir=infra/terraform destroy
```

## Operational notes

All measured on a real deployment.

**The first request after an idle period can time out.** Aurora Serverless v2 scales to zero,
and waking it took longer than the 29 second request budget. Warm, the round trip is 3 to 5
seconds. Send one throwaway request before a demo.

**Destroy takes longer than create:** 19 minutes 50 seconds against 7 minutes 34. Lambda
functions in a VPC hold network interfaces that AWS detaches on its own schedule, and until
they release, the security group and its subnets wait. The security group alone took 10
minutes 40 seconds. Nothing is stuck.

**A fallback has to fit inside your timeout.** Left on its defaults the AWS SDK retried a
dead model for 15 of the 29 available seconds. The failover ran out of time and the request
failed anyway. With `max_attempts=1` the same failure produces a correct answer in 6 seconds.

**Thresholds are measured, not guessed.** On these documents small talk scores 0.106 to 0.148 and
real questions 0.361 to 0.656. A floor of 0.35 sat 0.011 below the lowest real question.
Approximate vector search moves a score by more than that between runs, so one question
passed and failed intermittently. The floor is 0.25, in the gap between the two groups, and
temperature is 0 so that two evaluation runs can be compared at all.

**Anthropic models need a one-time use-case form per AWS account.** Bedrock reports the model
as ACTIVE in the catalog before you are allowed to call it. The first real request then fails
with `Model use case details have not been submitted for this account`. Amazon's own models
need no form, which is why they are the default.

**Verify a teardown against the services, not the tagging index.**
`resourcegroupstaggingapi get-resources` reported 12 surviving resources straight after a
clean destroy, and all twelve were already gone. That index is eventually consistent and built
for search, not for billing.

## Cost

Roughly $2 to $5 a day for an environment in light use. OpenSearch Serverless bills for as
long as the collection exists, whether or not anyone is asking questions, which dominates the
bill for an idle environment. Bedrock, Secrets Manager and OpenSearch are reached over
interface endpoints and S3 over a gateway endpoint. The original stack has no NAT gateway.
This repository adds one by default, for tracing to Langfuse Cloud, at about $1.20 a day
plus data; `enable_nat = false` removes it.
