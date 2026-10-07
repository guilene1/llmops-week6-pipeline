"""promptfoo provider: ask the deployed assistant one golden-set question.

promptfoo runs on the GitHub runner, outside the VPC. The assistant needs Aurora, which
has no route in from outside, so the question is answered by the evaluate function
inside the VPC ({"ask": case}) and the answer comes back here. The function returns the
answer and everything the gate needs about it: sources, passages, tokens, latency,
outcome and the Langfuse trace id.

Latency is the assistant's own time, measured inside the function, so it does not
include the invoke round trip and stays comparable with the baseline.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # pipeline/, for stack.py

import stack  # noqa: E402

# One Langfuse session for the whole run. run.sh sets it, so every worker process agrees.
RUN_ID = os.environ.get("EVAL_RUN_ID") or time.strftime("eval-%Y%m%dT%H%MZ", time.gmtime())


def call_api(prompt, options, context):
    variables = context.get("vars", {})
    case = {
        "id": variables["case_id"],
        "email": variables["email"],
        "question": variables["question"],
        "history": json.loads(variables.get("history") or "[]"),
    }
    try:
        result = stack.invoke("evaluate", {"ask": case, "run_id": RUN_ID}, read_timeout=120)
    except Exception as error:  # an invoke error, a timeout: a failed case, not a crashed run
        return {"error": f"evaluate function failed: {str(error)[:500]}"}

    return {
        "output": result["answer"],
        "metadata": result,
        "tokenUsage": {
            "prompt": result.get("input_tokens", 0),
            "completion": result.get("output_tokens", 0),
            "total": result.get("input_tokens", 0) + result.get("output_tokens", 0),
        },
    }
