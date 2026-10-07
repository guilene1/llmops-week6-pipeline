"""The gate's rules and comment, on made-up runs. No AWS."""

import json

import eval_gate
import make_baseline


def case(case_id, category, passed, answer="An answer.", problems=None):
    return {
        "id": case_id, "category": category, "tags": [], "who": "Amara Diallo",
        "question": f"question {case_id}?", "passed": passed,
        "problems": [] if passed else (problems or ["missing '5'"]),
        "answer": answer, "sources": [], "passages": [], "latency_ms": 4000,
        "input_tokens": 1000, "output_tokens": 50, "tools_used": [],
    }


def run_of(cases, p90=6000):
    passed = sum(c["passed"] for c in cases)
    return {"passed": passed, "total": len(cases), "failures": [c for c in cases if not c["passed"]],
            "cases": cases, "p90_latency_ms": p90, "total_tokens": 1050 * len(cases),
            "config": {"chat_model": "amazon.nova-pro-v1:0", "min_best_similarity": 0.25}}


def healthy_cases():
    cases = [case(f"fact-{i}", "factual", True) for i in range(40)]
    cases += [case(f"acl-{i}", "access_control", True) for i in range(5)]
    cases += [case(f"inj-{i}", "injection", True) for i in range(4)]
    cases.append(case("row-1", "table_row", False))  # one known failure in the baseline
    return cases


def baseline():
    return make_baseline.build_baseline(run_of(healthy_cases()), "abc1234def", "test")


def gate(run, base=baseline(), judged=None, calibrated=False):
    rules = eval_gate.evaluate_rules(run, base, judged, calibrated)
    comment, blocked = eval_gate.render(run, base, rules, judged, sha="feedbeef")
    return rules, comment, blocked


def test_the_baseline_itself_passes():
    _, comment, blocked = gate(run_of(healthy_cases()))
    assert not blocked
    assert comment.startswith("## Eval gate: passed")
    assert "Newly failing" not in comment


def test_one_access_control_failure_blocks_even_when_the_rate_is_fine():
    cases = healthy_cases()
    cases[40] = case("acl-0", "access_control", False, answer="Noah earns 146,000.",
                     problems=["leaked '146,000'"])
    cases[49] = case("row-1", "table_row", True)  # the rate stays the same
    rules, comment, blocked = gate(run_of(cases))
    assert blocked
    assert not next(r for r in rules if "access_control" in r.name).ok
    assert next(r for r in rules if "Pass rate" in r.name).ok
    assert "leaked '146,000'" in comment
    assert "> Noah earns 146,000." in comment


def test_an_injection_failure_blocks():
    cases = healthy_cases()
    cases[45] = case("inj-0", "injection", False)
    assert gate(run_of(cases))[2]


def test_a_drop_of_two_points_or_more_blocks():
    cases = healthy_cases()
    for i in range(2):  # 2 of 50 is 4 points
        cases[i] = case(f"fact-{i}", "factual", False)
    rules, comment, blocked = gate(run_of(cases))
    assert blocked
    assert "Newly failing (2)" in comment


def test_a_drop_under_two_points_passes():
    cases = healthy_cases()
    cases[0] = case("fact-0", "factual", False)   # one newly failing ...
    cases[49] = case("row-1", "table_row", True)  # ... and one newly passing: same rate
    rules, comment, blocked = gate(run_of(cases))
    assert not blocked
    assert "Newly failing (1)" in comment
    assert "Newly passing (1)" in comment


def test_slow_p90_blocks():
    rules, _, blocked = gate(run_of(healthy_cases(), p90=20000))
    assert blocked
    assert not next(r for r in rules if "p90" in r.name).ok


def test_no_baseline_blocks_with_instructions():
    rules, comment, blocked = gate(run_of(healthy_cases()), base=None)
    assert blocked
    assert "make baseline" in comment


def test_an_empty_must_be_perfect_category_blocks():
    cases = [c for c in healthy_cases() if c["category"] != "injection"]
    assert gate(run_of(cases))[2]


def test_config_change_is_named_against_the_baseline():
    run = run_of(healthy_cases())
    run["config"]["chat_model"] = "amazon.nova-lite-v1:0"
    _, comment, _ = gate(run)
    assert "`chat_model` amazon.nova-lite-v1:0 (baseline amazon.nova-pro-v1:0)" in comment


def test_uncalibrated_judge_warns_but_does_not_block():
    judged = {"model": "m", "prompt_version": "p", "mean": 1.0,
              "scores": {"fact-0": {"score": 1, "reason": "invented"}}}
    rules, comment, blocked = gate(run_of(healthy_cases()), judged=judged, calibrated=False)
    assert not blocked
    assert "warning" in comment


def test_calibrated_judge_blocks_a_clearly_ungrounded_answer():
    judged = {"model": "m", "prompt_version": "p", "mean": 1.0,
              "scores": {"fact-0": {"score": 2, "reason": "not in the passages"}}}
    assert gate(run_of(healthy_cases()), judged=judged, calibrated=True)[2]


def test_long_answers_are_truncated():
    cases = healthy_cases()
    cases[0] = case("fact-0", "factual", False, answer="x" * 5000)
    cases[49] = case("row-1", "table_row", True)
    _, comment, _ = gate(run_of(cases))
    assert "x" * 701 not in comment


def test_baseline_refuses_a_run_with_a_leak(tmp_path, monkeypatch):
    cases = healthy_cases()
    cases[40] = case("acl-0", "access_control", False)
    source = tmp_path / "eval.json"
    source.write_text(json.dumps(run_of(cases)))
    out = tmp_path / "baseline.json"
    monkeypatch.setattr("sys.argv", ["make_baseline", "--from", str(source), "--out", str(out)])
    assert make_baseline.main() == 1
    assert not out.exists()


def test_main_exit_code_and_comment_file(tmp_path, monkeypatch):
    run_file = tmp_path / "eval.json"
    run_file.write_text(json.dumps(run_of(healthy_cases())))
    base_file = tmp_path / "baseline.json"
    base_file.write_text(json.dumps(baseline()))
    out = tmp_path / "comment.md"
    monkeypatch.setattr("sys.argv", ["eval_gate", "--run", str(run_file), "--baseline",
                                      str(base_file), "--out", str(out)])
    assert eval_gate.main() == 0
    assert out.read_text().startswith("## Eval gate: passed")
