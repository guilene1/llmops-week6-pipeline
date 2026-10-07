#!/usr/bin/env bash
#
# Bring the whole Northwind HR Assistant up, ready for Week 6, in one command. Or take it down.
#
#   bash scripts/deploy.sh                  everything, stopping to confirm each terraform apply
#   bash scripts/deploy.sh --yes            everything, without stopping
#   bash scripts/deploy.sh destroy          take the stack down; keep the state bucket and CI role
#   bash scripts/deploy.sh destroy --all    also remove those (end of the course)
#
# "Everything" is docs/deployment-guide.md, sections 1 to 8, in order:
#
#   1. check the tools, the AWS credentials and the Bedrock models
#   2. bootstrap: Terraform state bucket, GitHub OIDC provider, CI role (pipeline/bootstrap)
#   3. settings: infra/terraform/terraform.tfvars, with a Cognito domain prefix of its own
#   4. build the two zips
#   5. terraform apply: the infrastructure, then the database and demo data, the documents
#      loaded and indexed, the web app published, the demo users' password set
#      (infra/terraform/app_setup.tf)
#   6. smoke test: the API answers, and one real question is answered correctly
#
# Then open docs/week6-project.md and start at Stop 8.
#
# Run it from the root of your clone of this repository on GitHub, in Git Bash on Windows.
# About 20 minutes the first time, nearly all of it waiting on AWS. Running it again is
# safe: anything already done is left as it is, and a step that failed is retried.

set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: leave /aws/... paths alone

PROJECT=northwind-hr
REGION=${AWS_REGION:-us-east-1}
TF=infra/terraform
BOOT=pipeline/bootstrap
PYTHON=${PYTHON:-python}
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3
export PYTHON

ACTION=up
ALL=false
APPROVE=()
for arg in "$@"; do
  case "$arg" in
    up|destroy) ACTION=$arg ;;
    --yes|-y) APPROVE=(-auto-approve) ;;
    --all) ALL=true ;;
    *) echo "usage: bash scripts/deploy.sh [up|destroy] [--yes] [--all]" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Which GitHub repository the CI role should trust: your copy of this one
github_repository() {
  local repo=${GITHUB_REPOSITORY:-}
  if [ -z "$repo" ] && [ -f "$BOOT/terraform.tfvars" ]; then
    repo=$(sed -nE 's/^github_repository *= *"([^"]+)".*/\1/p' "$BOOT/terraform.tfvars")
  fi
  if [ -z "$repo" ]; then
    repo=$(git remote get-url origin 2>/dev/null | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##' || true)
  fi
  [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
    fail "Cannot tell which GitHub repository this is. Clone your copy from GitHub, or run with GITHUB_REPOSITORY=owner/name."
  echo "$repo"
}

# The repository's permanent ids, which GitHub now puts in the token the CI role trusts
# (pipeline/bootstrap/github_oidc.tf). From GitHub's public API; empty if it cannot be
# reached or the repository is private, and the role then accepts any ids for the name.
github_ids() {
  local api="https://api.github.com/repos/$1" auth=()
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    auth=(-H "Authorization: Bearer $(gh auth token)")
  fi
  curl -fsS "${auth[@]}" "$api" 2>/dev/null |
    "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['owner']['id'], d['id'])" 2>/dev/null || true
}

bootstrap_vars() {
  local repo=$1 ids owner_id="" repo_id=""
  ids=$(github_ids "$repo")
  [ -n "$ids" ] && read -r owner_id repo_id <<<"$ids"
  echo "-var github_repository=$repo -var github_owner_id=$owner_id -var github_repository_id=$repo_id -var aws_region=$REGION"
}

# "true" if the bootstrap creates (and so owns) the GitHub OIDC provider, "false" if the
# account had one before this project. An account holds only one, so a provider that
# exists and is not in the bootstrap's state is used, never created again or destroyed.
oidc_is_ours() {
  # The resource only: "data.aws_iam_openid_connect_provider.github" in the state means the
  # provider was there before, and the bootstrap merely looked it up.
  if terraform -chdir="$BOOT" state list 2>/dev/null | grep -q '^aws_iam_openid_connect_provider\.github'; then
    echo true
  elif aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text |
       grep -q token.actions.githubusercontent.com; then
    echo false
  else
    echo true
  fi
}

backend_config() {
  terraform -chdir="$BOOT" output -raw backend_config > "$TF/backend.hcl" 2>/dev/null || {
    rm -f "$TF/backend.hcl"
    fail "No state bucket found. Run: bash scripts/deploy.sh"
  }
}

# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------
if [ "$ACTION" = destroy ]; then
  step "Destroying the stack (about 20 minutes, mostly Lambda network interfaces detaching)"
  terraform -chdir="$BOOT" init -input=false >/dev/null
  backend_config
  # Terraform reads the configuration, zips included, even to destroy it
  [ -f build/layer.zip ] || { mkdir -p build && touch build/layer.zip; }
  [ -f build/function.zip ] || bash scripts/build-function.sh >/dev/null
  terraform -chdir="$TF" init -input=false -backend-config=backend.hcl >/dev/null
  terraform -chdir="$TF" destroy -input=false "${APPROVE[@]}"

  if [ "$ALL" = true ]; then
    step "Removing the bootstrap: state bucket, CI role, OIDC provider"
    REPO=$(github_repository)
    read -r -a BOOT_VARS <<<"$(bootstrap_vars "$REPO")"
    CREATE_OIDC=$(oidc_is_ours)
    # The bucket is protected while it holds state; lift that, then destroy. An OIDC
    # provider the account had before this project is left alone.
    terraform -chdir="$BOOT" apply -input=false "${BOOT_VARS[@]}" -var create_oidc_provider="$CREATE_OIDC" \
      -var allow_state_bucket_destroy=true -auto-approve >/dev/null
    terraform -chdir="$BOOT" destroy -input=false "${BOOT_VARS[@]}" -var create_oidc_provider="$CREATE_OIDC" \
      -var allow_state_bucket_destroy=true "${APPROVE[@]}"
  fi

  step "Done. Check nothing is left, per service:"
  echo "  aws lambda list-functions --query \"length(Functions[?starts_with(FunctionName,'$PROJECT')])\""
  echo "  aws opensearchserverless list-collections --query 'length(collectionSummaries)'"
  echo "  aws ec2 describe-nat-gateways --filter Name=state,Values=available --query 'length(NatGateways)'"
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Checks: the deployment guide's section 1
# ---------------------------------------------------------------------------
step "1/6 Checking tools, AWS credentials and Bedrock"

for tool in aws terraform git node npm curl "$PYTHON"; do
  have "$tool" || fail "$tool is not installed. See docs/deployment-guide.md, section 1."
done

"$PYTHON" - "$(terraform version -json)" "$(node --version)" "$(aws --version 2>&1)" <<'PY' || exit 1
import json, re, sys
terraform = tuple(int(x) for x in json.loads(sys.argv[1])["terraform_version"].split(".")[:2])
node = int(sys.argv[2].lstrip("v").split(".")[0])
aws = sys.argv[3]
problems = []
if terraform < (1, 11): problems.append(f"Terraform {'.'.join(map(str, terraform))}: 1.11 or newer is needed")
if node < 20: problems.append(f"Node {node}: 20 or newer is needed")
if not re.search(r"aws-cli/2\.", aws): problems.append("AWS CLI version 2 is needed")
if sys.version_info < (3, 12): problems.append(f"Python {sys.version_info.major}.{sys.version_info.minor}: 3.12 or newer is needed")
for p in problems: print("ERROR:", p, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
echo "tools: ok"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) ||
  fail "AWS credentials are not working. Run: aws configure (region us-east-1). Then: aws sts get-caller-identity"
echo "AWS account: $ACCOUNT, region $REGION"

MODELS=$(aws bedrock list-foundation-models --region "$REGION" --query 'modelSummaries[].modelId' --output text)
for model in amazon.nova-pro-v1:0 amazon.nova-lite-v1:0 amazon.titan-embed-text-v2:0; do
  grep -qw "$model" <<<"$MODELS" || fail "Bedrock does not offer $model in $REGION."
done
echo "Bedrock models: ok"

REPO=$(github_repository)
read -r -a BOOT_VARS <<<"$(bootstrap_vars "$REPO")"
echo "GitHub repository: $REPO ($(printf '%s ' "${BOOT_VARS[@]}" | grep -oE 'github_(owner|repository)_id=[0-9]*' | tr '\n' ' '))"

# ---------------------------------------------------------------------------
# 2. Bootstrap: state bucket, OIDC, CI role
# ---------------------------------------------------------------------------
step "2/6 Bootstrap: state bucket, GitHub OIDC provider, CI role (about 1 minute)"

terraform -chdir="$BOOT" init -input=false >/dev/null
CREATE_OIDC=$(oidc_is_ours)
[ "$CREATE_OIDC" = false ] && echo "This account already has a GitHub OIDC provider: using it."
terraform -chdir="$BOOT" apply -input=false "${BOOT_VARS[@]}" \
  -var create_oidc_provider="$CREATE_OIDC" "${APPROVE[@]}"

# ---------------------------------------------------------------------------
# 3. Settings
# ---------------------------------------------------------------------------
step "3/6 Settings: $TF/terraform.tfvars"

if [ ! -f "$TF/terraform.tfvars" ]; then
  # The Cognito sign-in domain is claimed across the whole region, so it gets a random
  # suffix. One Aurora instance: Multi-AZ failover is not worth paying for in a course.
  SUFFIX=$("$PYTHON" -c "import secrets,string;print(''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(6)))")
  sed -E "s/^cognito_domain_prefix = .*/cognito_domain_prefix = \"$PROJECT-$SUFFIX\"/;
          s/^aurora_instance_count = 2/aurora_instance_count = 1/;
          s/^aws_region *= .*/aws_region   = \"$REGION\"/" \
    "$TF/terraform.tfvars.example" > "$TF/terraform.tfvars"
  echo "Created it, with cognito_domain_prefix = \"$PROJECT-$SUFFIX\" and aurora_instance_count = 1."
else
  echo "Using the existing one."
fi
grep -q '"northwind-hr-yourname"' "$TF/terraform.tfvars" &&
  fail "Set cognito_domain_prefix in $TF/terraform.tfvars to something of your own."
backend_config

# ---------------------------------------------------------------------------
# 4. Build
# ---------------------------------------------------------------------------
step "4/6 Building the two zips"

if [ ! -f build/layer.zip ] || [ ! -s build/layer.zip ] || [ lambda/requirements.txt -nt build/layer.zip ]; then
  bash scripts/build-layer.sh
else
  echo "build/layer.zip is up to date with lambda/requirements.txt"
fi
bash scripts/build-function.sh

# ---------------------------------------------------------------------------
# 5. Apply: infrastructure, database, documents, web app, passwords
# ---------------------------------------------------------------------------
step "5/6 terraform apply: infrastructure, database, documents, web app, demo users (about 15 minutes)"

terraform -chdir="$TF" init -input=false -backend-config=backend.hcl >/dev/null
terraform -chdir="$TF" apply -input=false "${APPROVE[@]}"

# ---------------------------------------------------------------------------
# 6. Smoke test: the deployment guide's section 7, in two calls
# ---------------------------------------------------------------------------
step "6/6 Smoke test"

API=$(terraform -chdir="$TF" output -raw api_endpoint)
curl -fsS "$API/api/health" >/dev/null || fail "The API at $API/api/health did not answer."
echo "API health: ok"

# One golden-set question, asked as Amara, through the evaluate function inside the VPC:
# the database, the search index, the guardrail and the model all have to work for it to pass.
PAYLOAD='{"ask": {"id": "fact-sick-days", "email": "amara.diallo@northwind.example", "question": "How many paid sick days do I get per year?"}, "run_id": "deploy-smoke-test"}'
for attempt in 1 2 3; do
  if aws lambda invoke --function-name "$PROJECT-evaluate" --region "$REGION" --cli-read-timeout 300 \
       --cli-binary-format raw-in-base64-out --payload "$PAYLOAD" build/smoke.json >/dev/null 2>&1 &&
     "$PYTHON" -c "import json,sys; r=json.load(open('build/smoke.json')); print('Answer:', r['answer'][:160].replace(chr(10),' ')); sys.exit(0 if r.get('passed') else 1)"; then
    echo "One question end to end: ok"
    break
  fi
  [ "$attempt" = 3 ] && fail "The test question did not pass. Read: aws logs tail /aws/lambda/$PROJECT-evaluate --since 10m"
  echo "Not yet (Aurora may be waking up). Trying again in 20 seconds ..."
  sleep 20
done

# ---------------------------------------------------------------------------
cat <<EOF

$(printf '\033[1m')The stack is up.$(printf '\033[0m')

  App          $(terraform -chdir="$TF" output -raw app_url)
  Sign in as   amara.diallo, liam.fischer, priya.raman or dana.whitfield @northwind.example
  Password     terraform -chdir=$TF output -raw demo_password

Next: open docs/week6-project.md and start at Stop 8.

It costs money while it exists (about \$3 a day idle, an estimate). When you finish:
  bash scripts/deploy.sh destroy
EOF
