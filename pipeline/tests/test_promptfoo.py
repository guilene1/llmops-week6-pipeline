"""The promptfoo suite: its tests, its assertion, and the conversion the gate reads. No AWS.

The fixture below has the shape promptfoo 0.124 writes with --output results.json,
taken from a real local run: one pass, one failed assertion, one provider error.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pipeline" / "promptfoo"))
sys.path.insert(0, str(ROOT / "lambda"))

import checks  # noqa: E402
import golden_tests as promptfoo_tests  # noqa: E402
import to_eval  # noqa: E402


def row(index, case_id, category, success, failure_reason, output, metadata=None, error=None, reason=None):
    response = {"output": output, "metadata": metadata} if metadata else {"error": error}
    return {
        "testIdx": index, "success": success, "failureReason": failure_reason, "error": error or reason,
        "vars": {"question": f"question {case_id}?", "email": "amara.diallo@northwind.example",
                 "history": "[]", "case_id": case_id},
        "testCase": {"metadata": {"id": case_id, "category": category, "tags": []}},
        "response": response,
        "gradingResult": {"pass": success, "reason": reason or "all checks passed"} if metadata else None,
    }


def lambda_result(passed, answer, sources=(), latency=4000):
    return {"who": "Amara Diallo", "answer": answer, "passed": passed, "problems": [],
            "sources": list(sources), "passages": [], "latency_ms": latency, "input_tokens": 1000,
            "output_tokens": 40, "tools_used": ["search_hr_policies"], "outcome": "answered",
            "trace_id": "abc", "config": {"chat_model": "amazon.nova-pro-v1:0"}}


RESULTS = {"evalId": "eval-1", "results": {"results": [
    row(2, "oos-sre", "out_of_scope", False, 2, None,
        error='evaluate function failed: {"errorMessage": "Bedrock throttled"}'),
    row(0, "fact-sick-days", "factual", True, 0, "10 days.",
        lambda_result(True, "10 days.", ["Sick Leave and Absence Policy"], latency=3000)),
    row(1, "row-juneteenth", "table_row", False, 1, "Not covered.",
        lambda_result(False, "Not covered.", latency=9000), reason="missing '19'"),
]}}


def test_conversion_keeps_order_and_tells_failures_from_errors():
    run, disagreements = to_eval.convert(RESULTS)
    assert disagreements == []
    assert [c["id"] for c in run["cases"]] == ["fact-sick-days", "row-juneteenth", "oos-sre"]
    assert run["passed"] == 1 and run["total"] == 3
    juneteenth, sre = run["cases"][1], run["cases"][2]
    assert juneteenth["problems"] == ["missing '19'"] and juneteenth["answer"] == "Not covered."
    assert sre["problems"][0].startswith("error: evaluate function failed")
    assert run["p90_latency_ms"] == 9000           # errors have no latency and are left out
    assert run["total_tokens"] == 2080
    assert run["config"]["chat_model"] == "amazon.nova-pro-v1:0"


def test_a_disagreement_with_the_lambda_is_reported():
    results = json.loads(json.dumps(RESULTS))
    results["results"]["results"][1]["response"]["metadata"]["passed"] = False
    _, disagreements = to_eval.convert(results)
    assert disagreements == ["fact-sick-days"]


def test_every_golden_case_becomes_one_test_with_no_list_vars():
    golden = json.loads((ROOT / "lambda" / "data" / "evaluation_questions.json").read_text(encoding="utf-8"))
    generated = promptfoo_tests.generate_tests()
    assert len(generated) == len(golden)
    for test in generated:
        # A list in vars makes promptfoo multiply the test
        assert all(isinstance(value, str) for value in test["vars"].values()), test["vars"]
        assert test["metadata"]["id"] == test["vars"]["case_id"]


@pytest.fixture(scope="module")
def lambda_check():
    pytest.importorskip("psycopg", reason="needs lambda/requirements.txt installed")
    from app import evaluate_handler
    return evaluate_handler.check


def test_checks_py_agrees_with_the_lambda_on_every_case(lambda_check):
    """The same rules in two places: prove they give the same verdict, case by case."""
    golden = json.loads((ROOT / "lambda" / "data" / "evaluation_questions.json").read_text(encoding="utf-8"))
    for case in golden:
        answers = [
            # A good answer, a bare one, and one that leaks everything forbidden
            " and ".join(case.get("must_include", [])) + "\n\nSources: " + case.get("source", ""),
            "That isn't covered.",
            " ".join(case.get("must_include", []) + case.get("must_not_include", [])).upper(),
        ]
        for answer in answers:
            for sources in ([], [case.get("source", "")]):
                ours = checks.problems_for(case, answer, sources)
                theirs = lambda_check(case, answer, [{"title": title} for title in sources])
                assert ours == theirs, (case.get("id"), answer)


def test_get_assert_reads_promptfoo_context():
    context = {"test": {"metadata": {"must_include": ["5"], "source": "Time Off Policy"}},
               "providerResponse": {"metadata": {"sources": ["Time Off Policy"]}}}
    assert checks.get_assert("You can carry over 5 days.", context)["pass"] is True
    failed = checks.get_assert("You can carry over ten days.", context)
    assert failed["pass"] is False and failed["reason"] == "missing '5'"
