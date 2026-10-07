"""Read a Terraform plan, decide whether it needs applying, and write it up for the PR.

    terraform show -json tfplan > plan.json
    terraform show -no-color tfplan > plan.txt
    python pipeline/plan_summary.py plan.json plan.txt --out plan.md

The pipeline builds function.zip fresh on every run, so its hash never matches the one in
state and every plan shows the four functions changing. That part is not a configuration
change: the code is deployed by scripts/deploy-code.sh a moment later either way. Anything
else in the plan is, and means apply.

Why apply whenever the plan has a real change, rather than only when the pull request
touches infra/: there is one stack. A pull request that raised a threshold and was then
closed leaves the stack on that threshold. The next pull request's plan shows it going
back, and applying it is what puts the stack back in step with the code being evaluated.
"""

import argparse
import json
import sys

import stack

# What changes on aws_lambda_function when only the zip differs
CODE_ONLY = {
    "source_code_hash", "code_sha256", "source_code_size", "last_modified",
    "version", "qualified_arn", "qualified_invoke_arn",
}

MAX_PLAN_TEXT = 55000  # GitHub's comment limit is 65,536 characters


def changed_keys(change):
    """The attributes a resource change actually alters."""
    before = change.get("before") or {}
    after = change.get("after") or {}
    unknown = change.get("after_unknown") or {}
    keys = set(before) | set(after) | set(unknown)
    return {k for k in keys if unknown.get(k) is True or before.get(k) != after.get(k)}


def classify(plan):
    """Return (config_changes, code_only_changes), each a list of (address, actions)."""
    config_changes, code_only = [], []
    for change in plan.get("resource_changes", []):
        actions = change["change"]["actions"]
        if actions in (["no-op"], ["read"]):
            continue
        entry = (change["address"], "/".join(actions))
        if (change["type"] == "aws_lambda_function" and actions == ["update"]
                and changed_keys(change["change"]) <= CODE_ONLY):
            code_only.append(entry)
        else:
            config_changes.append(entry)
    return config_changes, code_only


def render(config_changes, code_only, plan_text, infra_touched):
    lines = ["## Terraform plan", ""]
    if config_changes:
        lines.append(f"**{len(config_changes)} configuration change(s). The pipeline will apply them.**")
        if not infra_touched:
            lines += ["", "This pull request does not touch `infra/`, so these changes put the stack "
                      "back in step with this branch. A previous run, or a change applied from a "
                      "laptop, left it different."]
        lines += ["", "| Resource | Action |", "|---|---|"]
        lines += [f"| `{address}` | {actions} |" for address, actions in config_changes]
    else:
        lines.append("**No configuration changes.** Nothing to apply.")
    if code_only:
        lines += ["", f"{len(code_only)} function(s) show a new code hash only. That is the fresh "
                  "build of `function.zip`, deployed by `scripts/deploy-code.sh` in a later step."]

    text = plan_text.strip()
    if len(text) > MAX_PLAN_TEXT:
        text = text[:MAX_PLAN_TEXT] + "\n\n... truncated. The full plan is in the workflow log."
    lines += ["", "<details><summary>Full plan</summary>", "", "```", text, "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan_json")
    parser.add_argument("plan_text")
    parser.add_argument("--out", default="plan.md")
    parser.add_argument("--infra-touched", action="store_true",
                        help="the pull request changes files under infra/")
    args = parser.parse_args()

    with open(args.plan_json, encoding="utf-8") as handle:
        plan = json.load(handle)
    with open(args.plan_text, encoding="utf-8") as handle:
        plan_text = handle.read()

    config_changes, code_only = classify(plan)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render(config_changes, code_only, plan_text, args.infra_touched))

    needs_apply = bool(config_changes)
    print(f"configuration changes: {len(config_changes)}, code only: {len(code_only)}, "
          f"apply: {str(needs_apply).lower()}")
    for address, actions in config_changes:
        print(f"  {actions:16} {address}")
    stack.write_github_output(needs_apply=str(needs_apply).lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
