"""Turn promptfoo's results into the run the gate reads.

    python pipeline/promptfoo/to_eval.py promptfoo-results.json --out eval.json

eval_gate.py, make_baseline.py and judge.py all read one shape: totals, p90 latency,
tokens, the stack's configuration, and one entry per case. promptfoo decides each case;
this file only rearranges what it decided.

It also compares promptfoo's verdict with the evaluate function's own check of the same
answer. They implement the same rules in two places, and a disagreement means one of
them has drifted. That is reported, loudly, rather than silently trusted.
"""

import argparse
import json
import math
import sys

ERROR = 2  # promptfoo's ResultFailureReason.ERROR


def p90(values):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def convert(promptfoo):
    rows = sorted(promptfoo["results"]["results"], key=lambda r: r.get("testIdx", 0))
    cases, disagreements, config = [], [], {}

    for row in rows:
        expected = (row.get("testCase") or {}).get("metadata") or {}
        variables = row.get("vars") or (row.get("testCase") or {}).get("vars") or {}
        response = row.get("response") or {}
        result = response.get("metadata") or {}
        config = config or result.get("config", {})
        grading = row.get("gradingResult") or {}

        # promptfoo's failureReason: 0 passed, 1 an assertion failed, 2 an error. It puts
        # the reason in "error" for both, so the number is what tells them apart.
        if row.get("failureReason") == ERROR or response.get("error") or not result:
            # The provider could not get an answer: a failed case, with the reason
            error = response.get("error") or row.get("error") or "no answer"
            problems = [f"error: {str(error)[:300]}"]
        elif row.get("success"):
            problems = []
        else:
            reason = grading.get("reason") or row.get("error") or "failed"
            problems = [part for part in reason.split("; ") if part] or [reason]

        passed = bool(row.get("success")) and not problems
        if result and "passed" in result and result["passed"] != passed:
            disagreements.append(expected.get("id") or variables.get("case_id"))

        cases.append({
            "id": expected.get("id") or variables.get("case_id"),
            "category": expected.get("category", ""),
            "tags": expected.get("tags", []),
            "who": result.get("who", variables.get("email", "")),
            "question": variables.get("question", ""),
            "passed": passed,
            "problems": problems,
            "answer": response.get("output") or result.get("answer") or "",
            "sources": result.get("sources", []),
            "passages": result.get("passages", []),
            "latency_ms": result.get("latency_ms", 0),
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
            "tools_used": result.get("tools_used", []),
            "outcome": result.get("outcome"),
            "trace_id": result.get("trace_id"),
        })

    passed = sum(case["passed"] for case in cases)
    input_tokens = sum(case["input_tokens"] for case in cases)
    output_tokens = sum(case["output_tokens"] for case in cases)
    run = {
        "passed": passed,
        "total": len(cases),
        "failures": [case for case in cases if not case["passed"]],
        "cases": cases,
        "p90_latency_ms": p90([case["latency_ms"] for case in cases if case["latency_ms"]]),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "config": config,
        "runner": "promptfoo",
        "promptfoo_eval_id": promptfoo.get("evalId"),
    }
    return run, disagreements


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results")
    parser.add_argument("--out", default="eval.json")
    args = parser.parse_args()

    with open(args.results, encoding="utf-8") as handle:
        run, disagreements = convert(json.load(handle))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(run, handle, indent=2)

    print(f"{run['passed']} of {run['total']} passed, p90 {run['p90_latency_ms'] / 1000:.1f} s, "
          f"{run['total_tokens']:,} tokens. Saved to {args.out}")
    if disagreements:
        print(f"promptfoo and the evaluate function disagree on {', '.join(disagreements)}. "
              "checks.py and check() in evaluate_handler.py must apply the same rules.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
