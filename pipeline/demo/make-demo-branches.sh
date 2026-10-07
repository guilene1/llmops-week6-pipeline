#!/usr/bin/env bash
#
# Create the four demo branches: small, realistic changes that each look harmless in a
# review and that the eval gate must block.
#
#   bash pipeline/demo/make-demo-branches.sh          # branches from main
#   git push -u origin demo/drop-citations            # then open a pull request for each
#
#   demo/drop-citations          the prompt stops asking for a "Sources:" line. Reads like
#                                a tidy-up: the web app lists sources anyway.
#   demo/raise-similarity-floor  min_best_similarity 0.25 to 0.45, "to cut weak matches"
#   demo/swap-to-lite            the chat model becomes Nova Lite, 13 times cheaper
#   demo/break-csv-rows          the CSV reader returns one section per file, not per row
#
# Each branch is one commit on top of main. Nothing is pushed: that is your decision.
# The edits are made by exact text replacement, and the script stops if the text it
# expects is not there, rather than committing something different.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

BASE=${1:-main}
PYTHON=${PYTHON:-python}

if [ -n "$(git status --porcelain)" ]; then
  echo "The working tree has changes. Commit or stash them first." >&2
  exit 1
fi
START=$(git rev-parse --abbrev-ref HEAD)

# replace FILE OLD NEW: exactly one occurrence, or fail
replace() {
  "$PYTHON" - "$@" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
count = text.count(old)
if count != 1:
    sys.exit(f"{path}: expected the text once, found it {count} times:\n{old}")
open(path, "w", encoding="utf-8", newline="").write(text.replace(old, new))
PY
}

branch() {
  local name=$1
  if git show-ref --quiet "refs/heads/demo/$name"; then
    echo "demo/$name already exists, skipped"
    return 1
  fi
  git switch --quiet -c "demo/$name" "$BASE"
}

finish() {
  git commit --quiet -am "$1"
  echo "created $(git rev-parse --abbrev-ref HEAD): $1"
  git switch --quiet "$START"
}

if branch drop-citations; then
  replace lambda/app/assistant.py \
'- End with a "Sources:" line naming the documents ONLY when you used
  search_hr_policies. Name each document by its title only, once.
' ''
  finish "Drop the Sources line from the prompt: the web app shows sources already"
fi

if branch raise-similarity-floor; then
  replace infra/terraform/variables.tf 'default     = "0.25"' 'default     = "0.45"'
  finish "Raise the relevance floor to 0.45 to cut weak matches"
fi

if branch swap-to-lite; then
  replace infra/terraform/variables.tf \
    'default     = "amazon.nova-pro-v1:0"' 'default     = "amazon.nova-lite-v1:0"'
  finish "Switch the chat model to Nova Lite: 13 times cheaper per token"
fi

if branch break-csv-rows; then
  replace lambda/app/documents.py \
'        sections.append((label, line))

    return sections' \
'        sections.append((label, line))

    # One section for the whole table: fewer, larger chunks
    return [("Table", "\n".join(text for _, text in sections))] if sections else []'
  finish "Read each CSV file as one section: fewer, larger chunks"
fi

echo
echo "Push each branch and open a pull request against $BASE:"
echo "  git push -u origin demo/drop-citations demo/raise-similarity-floor demo/swap-to-lite demo/break-csv-rows"
echo "They share one stack, so the gate runs them one at a time."
