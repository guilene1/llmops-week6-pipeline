"""An LLM judge for grounding: is each answer supported by the passages it cited?

    python pipeline/judge.py eval.json --out judge.json

The string checks in the evaluation ask "does the answer contain 26 weeks". They cannot
ask "is everything else in the answer true". This asks Amazon Nova Lite, through the same
Converse API the application uses, to score each cited answer from 1 to 5 against the
passages behind it.

A judge is a model, so it is wrong some of the time. It does not gate anything until
pipeline/judge_calibrate.py has measured it against human labels and found it agrees at
least 85 percent of the time. Change the prompt or the model and that measurement no
longer applies, which is why the calibration report records a hash of both.
"""

import argparse
import hashlib
import json
import os
import re
import sys

import stack

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "amazon.nova-lite-v1:0")

# Calibration maps a score to a label: this or above counts as grounded
GROUNDED_FROM = 4

SYSTEM = """You check whether an HR assistant's answer is supported by the policy passages
it was given. You judge grounding only: not style, tone or helpfulness."""

PROMPT = """Question:
{question}

Passages the answer cited:
{passages}

Answer:
{answer}

Score how well the answer is supported by the passages, from 1 to 5.
5  Every factual claim (numbers, dates, durations, conditions, yes or no) is in the passages.
4  Every key claim is supported. Wording differs, or a harmless detail is added.
3  The main claim is supported, but a secondary claim is not in the passages.
2  A key claim is not in the passages, or goes further than they do.
1  A key claim contradicts the passages, or the answer is invented.

Ignore the "Sources:" line. Do not use your own knowledge of HR policy.
Reply with JSON only: {{"score": <1-5>, "reason": "<one sentence>"}}"""

PROMPT_VERSION = hashlib.sha256((JUDGE_MODEL + SYSTEM + PROMPT).encode()).hexdigest()[:12]


def format_passages(passages):
    return "\n\n".join(
        f"[{i}] {p.get('title', '')} - {p.get('section', '')}\n{p.get('content', '')}"
        for i, p in enumerate(passages, start=1)
    )


def parse_score(text):
    """Pull {"score", "reason"} out of the reply, tolerating text around the JSON."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            score = int(data["score"])
            if 1 <= score <= 5:
                return score, str(data.get("reason", "")).strip()
        except (ValueError, KeyError, TypeError):
            pass
    match = re.search(r"\b([1-5])\b", text)
    if match:
        return int(match.group(1)), text.strip()[:200]
    raise ValueError(f"no score in judge reply: {text[:200]!r}")


def judge_one(bedrock, question, passages, answer):
    response = bedrock.converse(
        modelId=JUDGE_MODEL,
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": PROMPT.format(
            question=question, passages=format_passages(passages), answer=answer)}]}],
        inferenceConfig={"temperature": 0.0, "maxTokens": 200},
    )
    text = "".join(block.get("text", "") for block in response["output"]["message"]["content"])
    return parse_score(text)


def judge_run(result, bedrock=None):
    """Score every case that cited passages. Refusals and small talk cite none and are skipped."""
    bedrock = bedrock or stack.client("bedrock-runtime", read_timeout=60)
    scores = {}
    for case in result["cases"]:
        if not case.get("passages"):
            continue
        try:
            score, reason = judge_one(bedrock, case["question"], case["passages"], case["answer"])
        except Exception as error:
            print(f"  {case['id']:34} judge error: {error}", file=sys.stderr)
            continue
        scores[case["id"]] = {"score": score, "reason": reason}
        print(f"  {case['id']:34} {score}  {reason[:90]}")
    values = [s["score"] for s in scores.values()]
    return {
        "model": JUDGE_MODEL,
        "prompt_version": PROMPT_VERSION,
        "scores": scores,
        "mean": round(sum(values) / len(values), 2) if values else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("eval_json")
    parser.add_argument("--out", default="judge.json")
    args = parser.parse_args()

    with open(args.eval_json, encoding="utf-8") as handle:
        result = json.load(handle)
    judged = judge_run(result)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(judged, handle, indent=2)
    print(f"{len(judged['scores'])} answers judged, mean {judged['mean']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
