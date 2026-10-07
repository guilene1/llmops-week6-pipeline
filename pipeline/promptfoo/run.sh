#!/usr/bin/env bash
#
# Run the golden set through promptfoo against the deployed stack, and write the run the
# gate reads. The pipeline's stage 6 and `make baseline` both use this, so a baseline is
# always measured the same way as the pull requests compared with it.
#
#   bash pipeline/promptfoo/run.sh                 # writes eval.json
#   bash pipeline/promptfoo/run.sh out.json
#
# Needs Node 20 or newer, Python with boto3, and AWS credentials that may invoke the
# evaluate function. Also writes promptfoo-results.json and promptfoo-report.html, the
# latter being promptfoo's own report: open it in a browser.

set -euo pipefail
cd "$(dirname "$0")/../.."

OUT=${1:-eval.json}
PROMPTFOO_VERSION=${PROMPTFOO_VERSION:-0.124.0}
CONCURRENCY=${EVAL_CONCURRENCY:-4}

export PROMPTFOO_DISABLE_TELEMETRY=1
export PROMPTFOO_DISABLE_UPDATE=1
export PROMPTFOO_PYTHON=${PROMPTFOO_PYTHON:-${PYTHON:-python}}
export EVAL_RUN_ID=${EVAL_RUN_ID:-eval-$(date -u +%Y%m%dT%H%M%SZ)}

# promptfoo exits 100 when some tests failed. That is a finished run, for the gate to
# judge. Anything else is a broken run.
set +e
npx --yes "promptfoo@$PROMPTFOO_VERSION" eval \
  --config pipeline/promptfoo/promptfooconfig.yaml \
  --max-concurrency "$CONCURRENCY" --no-cache --no-progress-bar \
  --output promptfoo-results.json --output promptfoo-report.html
code=$?
set -e
if [ "$code" -ne 0 ] && [ "$code" -ne 100 ]; then
  echo "promptfoo did not finish (exit $code)" >&2
  exit "$code"
fi

"$PROMPTFOO_PYTHON" pipeline/promptfoo/to_eval.py promptfoo-results.json --out "$OUT"
