"""Measure the judge against human labels, before it is allowed to gate anything.

    python pipeline/judge_calibrate.py

Runs the judge over pipeline/judge_calibration.json (20 answers a person labelled good,
20 bad) and reports how often it agrees. Writes pipeline/judge_calibration_report.json,
which is committed: the gate reads it, and lets the judge block a pull request only when

    agreement is at least 85 percent, and
    the model, the prompt and the labelled examples are the ones that were measured.

Change any of the three and the report no longer matches, so the judge drops back to
advisory until this is run again. Needs AWS credentials that may call Nova Lite.
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import judge

HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE / "judge_calibration.json"
REPORT_FILE = HERE / "judge_calibration_report.json"
REQUIRED_AGREEMENT = 0.85


def calibration_hash():
    return hashlib.sha256(CALIBRATION_FILE.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def is_calibrated(report):
    """True when a report says the judge, as it is now, may gate."""
    return bool(
        report
        and report.get("agreement", 0) >= REQUIRED_AGREEMENT
        and report.get("model") == judge.JUDGE_MODEL
        and report.get("prompt_version") == judge.PROMPT_VERSION
        and report.get("calibration_version") == calibration_hash()
    )


def load_report():
    if REPORT_FILE.exists():
        return json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    return None


def main():
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    examples = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))["examples"]
    bedrock = judge.stack.client("bedrock-runtime", read_timeout=60)

    rows = []
    confusion = {"good": {"good": 0, "bad": 0}, "bad": {"good": 0, "bad": 0}}
    for example in examples:
        score, reason = judge.judge_one(bedrock, example["question"], example["passages"], example["answer"])
        predicted = "good" if score >= judge.GROUNDED_FROM else "bad"
        confusion[example["label"]][predicted] += 1
        agree = predicted == example["label"]
        rows.append({"id": example["id"], "label": example["label"], "score": score,
                     "predicted": predicted, "agree": agree, "reason": reason})
        print(f"  {'ok  ' if agree else 'MISS'} {example['id']:34} human {example['label']:4} judge {score}")

    agreement = sum(row["agree"] for row in rows) / len(rows)
    report = {
        "model": judge.JUDGE_MODEL,
        "prompt_version": judge.PROMPT_VERSION,
        "calibration_version": calibration_hash(),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "examples": len(rows),
        "agreement": round(agreement, 3),
        "required": REQUIRED_AGREEMENT,
        "confusion": {"human_good": confusion["good"], "human_bad": confusion["bad"]},
        "results": rows,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"\nagreement {agreement:.1%} on {len(rows)} examples (required {REQUIRED_AGREEMENT:.0%})")
    print(f"  human good: judge good {confusion['good']['good']}, judge bad {confusion['good']['bad']}")
    print(f"  human bad:  judge good {confusion['bad']['good']}, judge bad {confusion['bad']['bad']}")
    if agreement >= REQUIRED_AGREEMENT:
        print(f"The judge may gate. Commit {REPORT_FILE.relative_to(HERE.parent)}.")
    else:
        print("Below the bar: the judge stays advisory. Read the MISS rows before changing the prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
