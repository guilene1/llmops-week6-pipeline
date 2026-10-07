"""Write a new baseline from an evaluation run. Writing it is all this does.

    make baseline NOTE="why"                        # run promptfoo now, on the stack
    python pipeline/make_baseline.py --from eval.json --note "why"

The run comes from pipeline/promptfoo/run.sh, the same runner the gate uses, so the
baseline and the pull requests compared with it are measured the same way. A saved run
works too: the eval-result artifact of a gate run holds one.

Changing the baseline changes what every later pull request is measured against, so it is
never a side effect of a pipeline run. You run this on purpose, against a stack that is
running main, then commit pipeline/baseline.json in a pull request of its own. That pull
request runs the gate against the new baseline, and the reviewer sees exactly which cases
moved and why you say that is acceptable.

A run with a failing access_control or injection case is refused: that is never a state to
measure everything else against.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import eval_gate

HERE = Path(__file__).resolve().parent


def git_sha():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True, cwd=HERE).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def build_baseline(run, sha, note):
    return {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_sha": sha,
        "note": note,
        "config": run.get("config", {}),
        "passed": run["passed"],
        "total": run["total"],
        "pass_rate": round(eval_gate.rate(run["passed"], run["total"]), 2),
        "p90_latency_ms": run.get("p90_latency_ms", 0),
        "total_tokens": run.get("total_tokens", 0),
        "categories": eval_gate.category_counts(run["cases"]),
        "cases": {
            case["id"]: {"passed": case["passed"], "category": case.get("category", ""),
                         "question": case["question"]}
            for case in run["cases"]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", required=True,
                        help="an eval.json from pipeline/promptfoo/run.sh")
    parser.add_argument("--note", default="", help="why the baseline is changing")
    parser.add_argument("--out", default=str(eval_gate.BASELINE_FILE))
    args = parser.parse_args()

    run = json.loads(Path(args.source).read_text(encoding="utf-8"))

    if "cases" not in run:
        print("That run has no per-case results. Deploy the current code first.", file=sys.stderr)
        return 1

    counts = eval_gate.category_counts(run["cases"])
    for category in eval_gate.MUST_BE_PERFECT:
        c = counts.get(category, {"passed": 0, "total": 0})
        if c["passed"] != c["total"]:
            print(f"Refused: {category} is {c['passed']} of {c['total']}. Fix that first.",
                  file=sys.stderr)
            return 1

    baseline = build_baseline(run, git_sha(), args.note)
    Path(args.out).write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}: {baseline['passed']} of {baseline['total']} "
          f"({baseline['pass_rate']}%), p90 {baseline['p90_latency_ms'] / 1000:.1f} s, "
          f"{baseline['total_tokens']:,} tokens")
    print("\nNext, in a pull request of its own:")
    print("  git switch -c baseline/$(date +%Y%m%d)")
    print("  git add pipeline/baseline.json")
    print('  git commit -m "Update eval baseline: <why>"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
