#!/usr/bin/env bash
#
# Build the React app and publish it behind CloudFront.
#
#   bash scripts/publish-frontend.sh <frontend-bucket> <distribution-id>
#
# The deployment guide's step 5 in one command. The VITE_* settings come from the
# environment (Terraform passes them); they are written to frontend/.env.production, which
# Vite reads at build time. None of them is a secret: they end up in the browser.

set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: leave "/*" alone

BUCKET=${1:?usage: publish-frontend.sh <frontend-bucket> <distribution-id>}
DISTRIBUTION=${2:?usage: publish-frontend.sh <frontend-bucket> <distribution-id>}

: "${VITE_COGNITO_AUTHORITY:?set by Terraform, or copy from: terraform output -raw frontend_env}"
cat > frontend/.env.production <<EOF
VITE_AUTH_MODE=${VITE_AUTH_MODE:-cognito}
VITE_COGNITO_AUTHORITY=$VITE_COGNITO_AUTHORITY
VITE_COGNITO_CLIENT_ID=$VITE_COGNITO_CLIENT_ID
VITE_COGNITO_DOMAIN=$VITE_COGNITO_DOMAIN
EOF

echo "Building the front end ..."
(cd frontend && npm ci --no-audit --no-fund --loglevel=error && npm run build)

echo "Publishing to s3://$BUCKET ..."
aws s3 sync frontend/dist/ "s3://$BUCKET/" --delete --only-show-errors
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION" --paths "/*" \
  --query 'Invalidation.Status' --output text
