"""Turn a reviewed trace into a golden-set case, on a new branch, ready for a pull request.

    python pipeline/promote_case.py <trace-id> --must-include "60 days" \
        --source "Remote and Hybrid Work Policy" --try

This closes the loop. The nightly drift job puts weak traces in the Langfuse annotation
queue. A person reads each one there, and for those worth testing from now on sets the
score promote_to_golden_set to true. This reads that trace, takes the question and who
asked it, adds the facts the reviewer says a right answer must contain, and appends the
case to lambda/data/evaluation_questions.json on a branch called golden/<case id>.

The pull request runs the eval gate like any other. The new case shows under "Not in the
baseline", so the reviewer sees whether the assistant already gets it right. A case that
fails costs 2.2 points of pass rate and blocks the merge: fix the assistant, or the
case, first. --try asks the stack now, so you know before you push.

Nothing is pushed: that is yours to do.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from langfuse_api import LangfuseAPI
from trace import fetch_trace

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_SET = ROOT / "lambda" / "data" / "evaluation_questions.json"
SEED_DATA = ROOT / "lambda" / "db" / "03_seed_data.sql"
CATEGORIES = ["factual", "table_row", "access_control", "refusal", "injection", "multi_turn", "out_of_scope"]
MASKS = ("[number]", "[email]", "[phone]")
SCORE_NAME = "promote_to_golden_set"


def email_for(employee_id):
    """The trace has the employee id, never the email. The seed data maps one to the other."""
    match = re.search(rf"\('{re.escape(employee_id)}',\s*'([^']+@[^']+)'", SEED_DATA.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def marked_for_promotion(trace):
    for score in trace.get("scores") or []:
        if score.get("name") == SCORE_NAME:
            value = score.get("stringValue") if score.get("stringValue") is not None else score.get("value")
            return str(value).lower() in ("true", "1", "1.0")
    return False


def slug(text, length=40):
    """lower-case-words-like-this, cut at a word boundary."""
    full = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(full) <= length:
        return full
    cut = full[:length]
    return cut if full[length] == "-" else cut.rsplit("-", 1)[0]


def build_case(trace, args, cases):
    question = args.question or (trace.get("input") or "").strip()
    if isinstance(question, dict):
        question = question.get("question", "")
    if not question:
        sys.exit("The trace has no question. Pass --question.")
    if any(mask in question for mask in MASKS):
        sys.exit(f"The question was masked before it left the account: {question!r}\n"
                 "Pass the original wording with --question.")

    email = args.email or email_for(trace.get("userId") or "")
    if not email:
        sys.exit(f"No email for user {trace.get('userId')!r} in the seed data. Pass --email.")

    if not (args.must_include or args.must_not_include):
        sys.exit("Say what a right answer must contain (--must-include) or must not (--must-not-include).")

    case_id = args.id or f"promoted-{slug(question)}"
    if case_id in {c.get("id") for c in cases}:
        sys.exit(f"A case with id {case_id!r} already exists. Pass --id.")
    if any(c["question"].lower() == question.lower() and c["email"] == email for c in cases):
        sys.exit("The golden set already asks this question as this person.")

    case = {
        "id": case_id,
        "category": args.category,
        "tags": ["promoted", f"trace:{trace['id']}", *args.tag],
        "email": email,
        "question": question,
    }
    if args.must_include:
        case["must_include"] = args.must_include
    if args.must_not_include:
        case["must_not_include"] = args.must_not_include
    if args.source:
        case["source"] = args.source
    case["why"] = args.why or f"Promoted from production trace {trace['id']} on {date.today().isoformat()}."
    return case


def try_on_stack(case):
    """Ask the deployed assistant now, with the evaluate function's own check."""
    import stack
    result = stack.invoke("evaluate", {"ask": case, "run_id": "promote-case"}, read_timeout=120)
    print(f"\nThe stack answers ({result['latency_ms']} ms):\n  {result['answer'][:600]}")
    print(f"Sources: {', '.join(result['sources']) or 'none'}")
    if result["problems"]:
        print(f"It would FAIL: {'; '.join(result['problems'])}")
    else:
        print("It would pass.")
    return not result["problems"]


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("trace_id")
    parser.add_argument("--must-include", action="append", default=[], help="a fact a right answer contains")
    parser.add_argument("--must-not-include", action="append", default=[], help="a text a right answer never contains")
    parser.add_argument("--source", default="", help="the document title a right answer cites")
    parser.add_argument("--category", choices=CATEGORIES, default="factual")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--id", default="", help="the case id (default: promoted-<question>)")
    parser.add_argument("--why", default="", help="what this case protects against")
    parser.add_argument("--question", default="", help="the original wording, if the trace's was masked")
    parser.add_argument("--email", default="", help="who asks, if not in the seed data")
    parser.add_argument("--try", dest="try_it", action="store_true", help="ask the stack first (AWS credentials)")
    parser.add_argument("--force", action="store_true", help="promote even if not marked, or if it would fail")
    parser.add_argument("--no-branch", action="store_true", help="edit the file in place, no branch or commit")
    parser.add_argument("--from-secret", action="store_true")
    args = parser.parse_args()

    trace = fetch_trace(LangfuseAPI.from_environment(args.from_secret), args.trace_id)
    if not marked_for_promotion(trace) and not args.force:
        sys.exit(f"Trace {args.trace_id} is not marked {SCORE_NAME} = true. Review it in the "
                 "Langfuse annotation queue first, or pass --force.")

    cases = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    case = build_case(trace, args, cases)
    print(json.dumps(case, indent=2, ensure_ascii=False))

    if args.try_it and not try_on_stack(case) and not args.force:
        sys.exit("Not promoted: the case fails today. Fix the assistant or the case, or pass --force "
                 "to record it failing (the gate will then block until it passes).")

    if not args.no_branch:
        if git("status", "--porcelain"):
            sys.exit("The working tree has changes. Commit or stash them first.")
        git("switch", "-c", f"golden/{case['id']}")

    cases.append(case)
    with open(GOLDEN_SET, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(cases, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    if args.no_branch:
        print(f"\nAdded {case['id']} to {GOLDEN_SET}.")
        return 0
    git("add", str(GOLDEN_SET.relative_to(ROOT)))
    git("commit", "-m", f"Add golden case {case['id']} from trace {args.trace_id}")
    print(f"\nOn branch golden/{case['id']}, one commit. Next:")
    print(f"  git push -u origin golden/{case['id']}")
    print("  then open a pull request: the eval gate runs the new case with the others")
    return 0


if __name__ == "__main__":
    sys.exit(main())
