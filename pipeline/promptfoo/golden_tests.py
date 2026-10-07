"""promptfoo tests, generated from the golden set in lambda/data/evaluation_questions.json.

One file stays the source of truth. Each case becomes one test:

    vars       what the provider needs to ask: question, email, history, case id
    metadata   what the assertion checks, and how the gate groups it

promptfoo expands any list in vars into one test per item, so the history travels as a
JSON string, and the expected facts travel in metadata.
"""

import json
from pathlib import Path

GOLDEN_SET = Path(__file__).resolve().parents[2] / "lambda" / "data" / "evaluation_questions.json"


def generate_tests(config=None):
    cases = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    tests = []
    for number, case in enumerate(cases, start=1):
        case_id = case.get("id", f"case-{number:02d}")
        tests.append({
            "description": f"{case_id}: {case['question'][:60]}",
            "vars": {
                "question": case["question"],
                "email": case["email"],
                "history": json.dumps(case.get("history", [])),
                "case_id": case_id,
            },
            "metadata": {
                "id": case_id,
                "category": case.get("category", ""),
                "tags": case.get("tags", []),
                "must_include": case.get("must_include", []),
                "must_not_include": case.get("must_not_include", []),
                "source": case.get("source", ""),
            },
        })
    return tests
