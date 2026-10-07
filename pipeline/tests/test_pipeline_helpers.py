"""The plan classifier, the judge's reply parser, the reindex fingerprint, the golden set."""

import json
from pathlib import Path

import pytest

import judge
import plan_summary
import reindex

ROOT = Path(__file__).resolve().parents[2]


def lambda_change(before, after, unknown=None, actions=("update",)):
    return {"address": 'aws_lambda_function.app["chat"]', "type": "aws_lambda_function",
            "change": {"actions": list(actions), "before": before, "after": after,
                       "after_unknown": unknown or {}}}


def test_code_hash_only_is_not_a_configuration_change():
    plan = {"resource_changes": [lambda_change(
        {"source_code_hash": "a", "timeout": 29}, {"source_code_hash": "b", "timeout": 29},
        {"last_modified": True, "qualified_arn": True})]}
    config_changes, code_only = plan_summary.classify(plan)
    assert config_changes == [] and len(code_only) == 1


def test_an_environment_change_is_a_configuration_change():
    env_before = {"variables": {"MIN_BEST_SIMILARITY": "0.25"}}
    env_after = {"variables": {"MIN_BEST_SIMILARITY": "0.45"}}
    plan = {"resource_changes": [lambda_change(
        {"source_code_hash": "a", "environment": [env_before]},
        {"source_code_hash": "b", "environment": [env_after]})]}
    config_changes, _ = plan_summary.classify(plan)
    assert len(config_changes) == 1


def test_other_resources_and_no_ops():
    plan = {"resource_changes": [
        {"address": "aws_iam_role_policy.chat", "type": "aws_iam_role_policy",
         "change": {"actions": ["update"], "before": {}, "after": {}}},
        {"address": "aws_vpc.main", "type": "aws_vpc",
         "change": {"actions": ["no-op"], "before": {}, "after": {}}},
    ]}
    config_changes, _ = plan_summary.classify(plan)
    assert config_changes == [("aws_iam_role_policy.chat", "update")]


@pytest.mark.parametrize("reply, score", [
    ('{"score": 4, "reason": "all supported"}', 4),
    ('Here you go: {"score": 2, "reason": "x"} thanks', 2),
    ("Score: 5", 5),
])
def test_judge_reply_parsing(reply, score):
    assert judge.parse_score(reply)[0] == score


def test_judge_reply_without_a_score_raises():
    with pytest.raises(ValueError):
        judge.parse_score("I cannot tell.")


def test_fingerprint_is_stable_and_changes_with_the_code(monkeypatch, tmp_path):
    first = reindex.fingerprint()
    assert first == reindex.fingerprint()
    code = tmp_path / "lambda" / "app"
    code.mkdir(parents=True)
    (code / "documents.py").write_text("changed")
    monkeypatch.setattr(reindex, "ROOT", tmp_path)
    monkeypatch.setattr(reindex, "INDEX_CODE", ["lambda/app/documents.py"])
    monkeypatch.setattr(reindex, "DOCUMENTS_DIR", tmp_path / "none")
    assert reindex.fingerprint() != first


def test_golden_set_ids_and_categories():
    cases = json.loads((ROOT / "lambda" / "data" / "evaluation_questions.json").read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    allowed = {"factual", "table_row", "access_control", "refusal", "injection",
               "multi_turn", "out_of_scope"}
    assert {c["category"] for c in cases} <= allowed
    assert all(isinstance(c["tags"], list) for c in cases)
    # The drift replay asks about remote work, and the golden set must not already cover it
    # beyond the one original case. Cases promoted from production traces are how that gap
    # is meant to close, so they are allowed.
    remote = [c["id"] for c in cases if "remote_work" in c["tags"] and "promoted" not in c["tags"]]
    assert remote == ["fact-work-abroad"]


def test_calibration_set_is_balanced():
    data = json.loads((ROOT / "pipeline" / "judge_calibration.json").read_text(encoding="utf-8"))
    labels = [e["label"] for e in data["examples"]]
    assert labels.count("good") == 20 and labels.count("bad") == 20
    assert len({e["id"] for e in data["examples"]}) == 40
