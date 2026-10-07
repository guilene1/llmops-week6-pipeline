#!/usr/bin/env bash
#
# Give the application your Langfuse keys, in one command. Stop 8, part B.
#
#   bash scripts/set-langfuse-keys.sh
#
# It takes the keys from a .env file at the repository root if there is one (copy
# .env.example to .env and fill it in), and otherwise asks for them. Then:
#
#   1. checks the keys with Langfuse, so a typo or the wrong region is caught now
#   2. stores them in AWS Secrets Manager, in the secret Terraform created (northwind-hr/langfuse)
#   3. restarts the functions, so they read the new keys
#
# The application never reads .env: the functions run in AWS, so the keys have to be in
# Secrets Manager. .env only saves you typing them. It is in .gitignore; never commit it.
# Typed in instead, the secret key is never shown on screen or kept in your shell history.

set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

PROJECT=northwind-hr
REGION=${AWS_REGION:-us-east-1}
PYTHON=${PYTHON:-python}
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3

# Read the three LANGFUSE_* lines from .env, without running the file as a script
LANGFUSE_PUBLIC_KEY="" LANGFUSE_SECRET_KEY="" LANGFUSE_HOST=""
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    key=$(echo "$key" | tr -d '[:space:]')
    value=${value%$'\r'}
    value=$(echo "$value" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')   # spaces around the value
    value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
    case "$key" in
      LANGFUSE_PUBLIC_KEY) LANGFUSE_PUBLIC_KEY=$value ;;
      LANGFUSE_SECRET_KEY) LANGFUSE_SECRET_KEY=$value ;;
      LANGFUSE_HOST|LANGFUSE_BASE_URL) LANGFUSE_HOST=$value ;;   # Langfuse's own snippet says BASE_URL
    esac
  done < .env
fi

if [[ "$LANGFUSE_PUBLIC_KEY" == pk-lf-?* && "$LANGFUSE_SECRET_KEY" == sk-lf-?* &&
      "$LANGFUSE_PUBLIC_KEY" != "pk-lf-..." && "$LANGFUSE_SECRET_KEY" != "sk-lf-..." ]]; then
  LANGFUSE_HOST=${LANGFUSE_HOST:-https://us.cloud.langfuse.com}
  echo "Using the keys in .env (public key ${LANGFUSE_PUBLIC_KEY:0:12}..., host $LANGFUSE_HOST)."
else
  [ -f .env ] && echo "No usable keys in .env, so asking instead."
  echo "Paste the keys from Langfuse: your project > Settings > API Keys."
  echo
  read -r -p "Public key (pk-lf-...): " LANGFUSE_PUBLIC_KEY
  read -r -s -p "Secret key (sk-lf-..., not shown as you type): " LANGFUSE_SECRET_KEY
  echo
  read -r -p "Region: US or EU? [US]: " LANGFUSE_REGION_ANSWER
  case "${LANGFUSE_REGION_ANSWER:-US}" in
    [Ee][Uu]) LANGFUSE_HOST="https://cloud.langfuse.com" ;;
    *)        LANGFUSE_HOST="https://us.cloud.langfuse.com" ;;
  esac
fi

[[ "$LANGFUSE_PUBLIC_KEY" == pk-lf-* ]] || { echo "ERROR: the public key should start with pk-lf-" >&2; exit 1; }
[[ "$LANGFUSE_SECRET_KEY" == sk-lf-* ]] || { echo "ERROR: the secret key should start with sk-lf-" >&2; exit 1; }
export LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_HOST

echo
echo "1/3 Checking the keys with Langfuse at $LANGFUSE_HOST ..."
"$PYTHON" - <<'PY'
import base64, os, sys, urllib.error, urllib.request
token = base64.b64encode(f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()).decode()
request = urllib.request.Request(f"{os.environ['LANGFUSE_HOST']}/api/public/projects",
                                 headers={"Authorization": f"Basic {token}"})
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        import json
        names = [p.get("name") for p in json.loads(response.read()).get("data", [])]
        print(f"    ok: the keys belong to project {', '.join(names) or '(unnamed)'}")
except urllib.error.HTTPError as error:
    if error.code == 401:
        sys.exit("ERROR: Langfuse refused these keys. Check you copied both whole, and that the "
                 "region is right: a project made at us.cloud.langfuse.com is US, at cloud.langfuse.com is EU.")
    sys.exit(f"ERROR: Langfuse answered {error.code}. Try again in a minute.")
except OSError as error:
    sys.exit(f"ERROR: could not reach {os.environ['LANGFUSE_HOST']}: {error}")
PY

echo "2/3 Storing them in AWS Secrets Manager ($PROJECT/langfuse) ..."
# boto3 builds the JSON and sends it, so the secret never appears in a command line
"$PYTHON" - <<PY
import json, os, boto3
boto3.client("secretsmanager", region_name="$REGION").put_secret_value(
    SecretId="$PROJECT/langfuse",
    SecretString=json.dumps({"public_key": os.environ["LANGFUSE_PUBLIC_KEY"],
                             "secret_key": os.environ["LANGFUSE_SECRET_KEY"],
                             "host": os.environ["LANGFUSE_HOST"]}))
print("    ok")
PY

echo "3/3 Restarting the functions so they read the new keys ..."
[ -f build/function.zip ] || bash scripts/build-function.sh >/dev/null
bash scripts/deploy-code.sh "$PROJECT" "$REGION" >/dev/null
for name in chat evaluate; do
  aws lambda wait function-updated-v2 --function-name "$PROJECT-$name" --region "$REGION"
done
echo "    ok"

cat <<EOF

Tracing is on. Now ask a question in the app, then open Langfuse > Tracing:
you will see a trace named "chat". On the free Hobby plan, Langfuse shows new traces
after a delay of up to about 15 minutes (the page says "New data in ~15 min"). The chat
function's log names each trace straight away:
  aws logs tail /aws/lambda/northwind-hr-chat --since 10m --format short | grep "trace "
EOF
