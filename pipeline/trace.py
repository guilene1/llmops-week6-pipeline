"""Print one trace's span tree from the Langfuse public API.

    python pipeline/trace.py <trace-id>
    python pipeline/trace.py --latest                 # the newest trace
    python pipeline/trace.py --latest --source app    # the newest from real traffic

The trace id is in the chat function's log ("trace <id> outcome answered") and in every
evaluation case's result. Keys come from LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and
LANGFUSE_HOST, or with --from-secret from the stack's own secret (needs AWS credentials).

    chat                         4210 ms  user NW-1001  outcome answered
    |- input-guardrail            118 ms  guardrail   blocked=False
    |- model-call                 652 ms  generation  amazon.nova-pro-v1:0  1,203 in / 41 out
    |- policy-search              431 ms  retriever   best 0.61, 4 kept, top: Sick Leave and Absence Policy
    |  `- query-embedding         104 ms  embedding
    ...
"""

import argparse
import json
import sys
import time
import urllib.error
from datetime import datetime

from langfuse_api import LangfuseAPI


def fetch_trace(api, trace_id, wait_seconds=30):
    """Langfuse ingests asynchronously, so a trace from a moment ago may not be there yet."""
    deadline = time.time() + wait_seconds
    while True:
        try:
            return api.get(f"traces/{trace_id}")
        except urllib.error.HTTPError as error:
            if error.code != 404 or time.time() > deadline:
                raise
            time.sleep(3)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def duration_ms(item):
    start, end = parse_time(item.get("startTime") or item.get("timestamp")), parse_time(item.get("endTime"))
    if start and end:
        return int((end - start).total_seconds() * 1000)
    if item.get("latency") is not None:  # traces report seconds
        return int(float(item["latency"]) * 1000)
    return None


def as_dict(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def describe(observation):
    """The one line of detail that matters for each kind of step."""
    kind = (observation.get("type") or "").lower()
    meta = observation.get("metadata") or {}
    output = as_dict(observation.get("output"))
    parts = [kind]
    if observation.get("model"):
        parts.append(observation["model"])
    usage = observation.get("usageDetails") or observation.get("usage") or {}
    if usage.get("input") is not None:
        parts.append(f"{usage.get('input', 0):,} in / {usage.get('output', 0):,} out")
    if "fallback_used" in meta:
        parts.append(f"fallback_used={meta['fallback_used']}")
    if "best_similarity" in meta or "chunks_kept" in meta:
        parts.append(f"best {meta.get('best_similarity')}, {meta.get('chunks_kept')} kept, "
                     f"top: {meta.get('top_source_title')}")
    if isinstance(output, dict):
        if "tool_calls" in output:
            parts.append("asked for " + ", ".join(output["tool_calls"]))
        else:
            parts.append(", ".join(f"{k}={v}" for k, v in output.items()))
    if observation.get("level") in ("ERROR", "WARNING"):
        parts.append(f"{observation['level']}: {observation.get('statusMessage', '')}")
    return "  ".join(str(p) for p in parts if p not in ("", None))


def print_tree(trace):
    observations = trace.get("observations", [])
    children = {}
    for observation in observations:
        children.setdefault(observation.get("parentObservationId"), []).append(observation)
    for group in children.values():
        group.sort(key=lambda o: o.get("startTime") or "")

    meta = trace.get("metadata") or {}
    print(f"trace    {trace['id']}")
    print(f"user     {trace.get('userId')}   session {trace.get('sessionId')}   tags {', '.join(trace.get('tags') or [])}")
    print(f"outcome  {meta.get('outcome')}   model {meta.get('model_id')}   "
          f"prompt {meta.get('prompt_version')}   code {meta.get('git_sha')}")
    print(f"question {trace.get('input')}")
    answer = str(trace.get("output") or "")
    print(f"answer   {answer[:200]}{' ...' if len(answer) > 200 else ''}\n")

    def walk(observation, prefix, last, depth):
        branch = "" if depth == 0 else ("`- " if last else "|- ")
        label = f"{prefix}{branch}{observation.get('name')}"
        ms = duration_ms(observation)
        print(f"{label:34} {'' if ms is None else f'{ms:>6} ms'}  {describe(observation)}")
        kids = children.get(observation["id"], [])
        for i, child in enumerate(kids):
            extension = "" if depth == 0 else ("   " if last else "|  ")
            walk(child, prefix + extension, i == len(kids) - 1, depth + 1)

    roots = children.get(None, [])
    for i, root in enumerate(roots):
        walk(root, "", i == len(roots) - 1, 0)
    if not roots:
        print("(no observations yet: Langfuse can take up to about 15 minutes on the Hobby plan)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("trace_id", nargs="?")
    parser.add_argument("--latest", action="store_true", help="the newest trace")
    parser.add_argument("--source", choices=["app", "eval"], help="with --latest: only this source")
    parser.add_argument("--from-secret", action="store_true", help="read the keys from Secrets Manager")
    parser.add_argument("--wait", type=int, default=30, help="seconds to wait for a new trace to appear")
    parser.add_argument("--json", action="store_true", help="print the raw trace")
    args = parser.parse_args()
    if not args.trace_id and not args.latest:
        parser.error("give a trace id, or --latest")

    api = LangfuseAPI.from_environment(args.from_secret)

    trace_id = args.trace_id
    if args.latest:
        params = {"limit": 1, "orderBy": "timestamp.desc"}
        if args.source:
            params["tags"] = f"source={args.source}"
        found = api.get("traces", params).get("data", [])
        if not found:
            sys.exit("No traces yet.")
        trace_id = found[0]["id"]

    try:
        trace = fetch_trace(api, trace_id, args.wait)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            sys.exit(f"Trace {trace_id} is not in Langfuse yet. On the free Hobby plan new traces "
                     "appear after up to about 15 minutes. Try again later, or wait longer: --wait 900")
        raise
    if args.json:
        print(json.dumps(trace, indent=2, default=str))
    else:
        print_tree(trace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
