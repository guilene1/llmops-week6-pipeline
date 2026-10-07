#!/usr/bin/env bash
#
# Load the policy documents and wait until they are indexed.
#
#   bash scripts/load-documents.sh <documents-bucket> [project] [region]
#
# The same two uploads as the deployment guide, step 4: the documents first, the manifest
# last, because uploading the manifest is what starts ingestion. Then it reads the ingest
# function's log until the run reports its chunk count, so whatever runs this knows the
# index is ready rather than guessing. Terraform runs it once, after the first apply
# (infra/terraform/app_setup.tf). After that, the pipeline keeps the index current.

set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: leave /aws/lambda/... alone

BUCKET=${1:?usage: load-documents.sh <documents-bucket> [project] [region]}
PROJECT=${2:-northwind-hr}
REGION=${3:-us-east-1}
LOG_GROUP="/aws/lambda/$PROJECT-ingest"

echo "Uploading the documents to s3://$BUCKET ..."
aws s3 sync lambda/data/documents/ "s3://$BUCKET/" --exclude manifest.json --region "$REGION" --only-show-errors

START_MS=$(( $(date +%s) * 1000 - 5000 ))
aws s3 cp lambda/data/documents/manifest.json "s3://$BUCKET/manifest.json" --region "$REGION" --only-show-errors
echo "Manifest uploaded: ingestion started. Waiting for it to finish (about 80 seconds) ..."

for _ in $(seq 1 90); do
  sleep 10
  LINES=$(aws logs filter-log-events --log-group-name "$LOG_GROUP" --start-time "$START_MS" \
    --region "$REGION" --query 'events[].message' --output text 2>/dev/null || true)
  if grep -q "chunks indexed" <<<"$LINES"; then
    grep -o "[0-9]* documents, [0-9]* chunks indexed[^\t]*" <<<"$LINES" | tail -1
    exit 0
  fi
  if grep -qE "\[ERROR\]|Task timed out|IngestionError" <<<"$LINES"; then
    echo "Ingestion failed. The ingest log says:" >&2
    tr '\t' '\n' <<<"$LINES" | tail -30 >&2
    exit 1
  fi
done

echo "No result in $LOG_GROUP after 15 minutes." >&2
exit 1
