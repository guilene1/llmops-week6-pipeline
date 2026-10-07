"""Evaluation, inside AWS: ask questions with known answers and score the assistant.

    aws lambda invoke --function-name northwind-evaluate response.json
    aws lambda invoke --function-name northwind-evaluate \
        --cli-binary-format raw-in-base64-out --payload '{"similarity_report": true}' response.json

Run it after every change to the prompt, the thresholds, the chunking or the model.
The similarity report prints the scores you need to set MIN_BEST_SIMILARITY for Titan.
"""

import functools
import json
import math
import time

from app import assistant, config, embeddings, employees, retrieval, search_index, tracing

QUESTIONS_FILE = config.DATA_DIR / "evaluation_questions.json"

SMALL_TALK = ["hello there", "thanks!", "ok", "tell me a joke"]
REAL_QUESTIONS = [
    "How many vacation days can I carry over?",
    "What is the mileage reimbursement rate?",
    "Can I work from another country?",
    "What is the salary band for an L5 role?",
]


def normalize(text):
    return text.lower().replace(",", "").replace("$", "").replace("*", "")


def check(case, answer, sources):
    problems = []
    text = normalize(answer)
    for fact in case.get("must_include", []):
        if normalize(fact) not in text:
            problems.append(f"missing '{fact}'")
    for fact in case.get("must_not_include", []):
        if normalize(fact) in text:
            problems.append(f"leaked '{fact}'")
    if case.get("source") and case["source"] not in {source["title"] for source in sources}:
        problems.append(f"didn't use '{case['source']}'")
    return problems


def p90(values):
    """The value 90 percent of answers came in under (nearest rank)."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


@functools.cache
def golden_set():
    return {case["id"]: case for case in json.loads(QUESTIONS_FILE.read_text(encoding="utf-8")) if "id" in case}


def golden_case(case_id):
    """The bundled case with this id, or nothing, so the caller's fields stand alone."""
    return golden_set().get(case_id, {})


def current_config():
    return {
        "chat_model": config.CHAT_MODEL,
        "fallback_chat_model": config.FALLBACK_CHAT_MODEL,
        "min_best_similarity": config.MIN_BEST_SIMILARITY,
        "max_gap_from_best": config.MAX_GAP_FROM_BEST,
    }


def ask(case, run_id, flush=True):
    """Ask one evaluation case's question as that employee, and check the answer.

    The full run below calls this for every case. The pipeline calls it one case at a
    time through promptfoo ({"ask": case}), which asserts on the answer itself; the
    "problems" this returns are the Lambda's own check, kept so the two can be compared.
    promptfoo sends the question, not the expected facts, so those come from the copy of
    the golden set inside this zip, built from the same commit.
    """
    case = {**golden_case(case["id"]), **case}
    employee = employees.find_by_email(case["email"])
    usage = {}
    # Every case is a trace tagged source=eval, and one run is one Langfuse session, so
    # evaluation traffic can be found together and left out of the drift report.
    with tracing.request("eval", session_id=run_id, tags=[f"case={case['id']}"], flush=flush):
        started = time.time()
        # "history" replays earlier turns, so follow-ups are tested the way people ask them
        answer, sources = assistant.answer(case["question"], case.get("history", []), employee, usage)
        latency_ms = int((time.time() - started) * 1000)
    problems = check(case, answer, sources)
    return {
        # A stable id, so a pipeline can tell which case changed between two runs
        "id": case["id"],
        "category": case.get("category", ""),
        "tags": case.get("tags", []),
        "who": employee["full_name"],
        "question": case["question"],
        "passed": not problems,
        "problems": problems,
        # So a failure can be read without re-asking the question
        "answer": answer,
        "sources": sorted({source["title"] for source in sources}),
        # The cited passages, so a judge can check the answer against them
        "passages": [
            {key: source.get(key) for key in ("title", "section", "content")}
            for source in sources
        ],
        "latency_ms": latency_ms,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "tools_used": usage.get("tools_used", []),
        "outcome": usage.get("outcome"),
        "trace_id": usage.get("trace_id"),
    }


def run_evaluation(only=""):
    cases = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    # {"only": "meal"} re-runs just the questions containing that word
    if only:
        cases = [case for case in cases if only.lower() in case["question"].lower()]
    results = []
    run_id = time.strftime("eval-%Y%m%dT%H%M%SZ", time.gmtime())
    for number, case in enumerate(cases, start=1):
        case = {"id": f"case-{number:02d}", **case}
        result = ask(case, run_id, flush=False)
        results.append(result)
        problems = result["problems"]

        status = "PASS" if not problems else "FAIL"
        reason = "; ".join(problems)
        name = result["who"]
        short_question = case["question"][:52]
        print(f"{status}  {name:15} {short_question:52}  {reason}")

    passed = 0
    failures = []
    for result in results:
        if result["problems"]:
            failures.append(result)
        else:
            passed += 1

    print(f"\n{passed} of {len(results)} passed")
    tracing.flush_now(timeout=10)  # once, for the whole run

    latencies = [result["latency_ms"] for result in results]
    input_tokens = sum(result["input_tokens"] for result in results)
    output_tokens = sum(result["output_tokens"] for result in results)
    print(f"p90 latency {p90(latencies)} ms, {input_tokens + output_tokens} tokens")

    return {
        "passed": passed,
        "total": len(results),
        "failures": failures,
        # Everything below was added for the pipeline (pipeline/eval_gate.py).
        # The three keys above are unchanged.
        "cases": results,
        "p90_latency_ms": p90(latencies),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "config": current_config(),
    }


def similarity_report():
    """Score small talk and real questions, so the relevance floor can be measured.

    Every access level is allowed here, because this is about how close the
    question is to the text, not about who may read it.
    """
    levels = ["general", "manager_only", "hr_only", "exec_only"]
    groups = {"small_talk": SMALL_TALK, "real_questions": REAL_QUESTIONS}
    report = {}

    for label, questions in groups.items():
        scores = {}
        for question in questions:
            question_vector = embeddings.embed([question])[0]
            hits = search_index.search(question_vector, levels)
            if hits:
                best = round(retrieval.similarity_from_score(hits[0]["_score"]), 3)
            else:
                best = None
            scores[question] = best
            print(f"{label:15} {best}  {question}")
        report[label] = scores

    print("Set MIN_BEST_SIMILARITY between the highest small-talk score")
    print("and the lowest real-question score.")
    return report


def search_probe(query, old_version=False):
    """What retrieval returns for one query, to see why an answer went wrong.

        --payload '{"search": "2024 PTO carryover", "old_version": true}'
    """
    chunks = retrieval.search_policies(query, "employee", old_version=old_version)
    return [
        {key: chunk.get(key) for key in ("title", "section", "version", "status", "similarity")}
        for chunk in chunks
    ]


def tracing_overhead(runs=10, question="How many paid sick days do I get per year?",
                     email="amara.diallo@northwind.example"):
    """What tracing costs a request, measured on the real stack.

        --payload '{"tracing_overhead": {"runs": 10}}'

    Asks the same question with tracing off and on, alternately, so warm caches and a
    waking database favour neither. "on" includes the flush a chat request waits for.
    The traces are tagged source=eval and overhead_probe, so the drift report skips them.
    """
    employee = employees.find_by_email(email)
    assistant.answer(question, [], employee)  # warm everything first; not counted
    timings = {"off": [], "on": []}
    for _ in range(runs):
        for mode in ("off", "on"):
            started = time.time()
            if mode == "on":
                with tracing.request("eval", tags=["overhead_probe"]):
                    assistant.answer(question, [], employee)
            else:
                assistant.answer(question, [], employee)
            timings[mode].append(int((time.time() - started) * 1000))

    def summary(values):
        return {"mean_ms": round(sum(values) / len(values)), "p50_ms": sorted(values)[len(values) // 2],
                "p90_ms": p90(values)}

    report = {"runs": runs, "tracing_active": tracing.client() is not None,
              "off": summary(timings["off"]), "on": summary(timings["on"])}
    report["difference_ms"] = {key: report["on"][key] - report["off"][key] for key in report["off"]}
    print(json.dumps(report))
    return report


def handler(event, context):
    if (event or {}).get("ask"):
        # One case, for promptfoo (pipeline/promptfoo/provider.py)
        result = ask(event["ask"], event.get("run_id") or None)
        return {**result, "config": current_config()}
    if (event or {}).get("tracing_overhead") is not None:
        return tracing_overhead(**(event["tracing_overhead"] or {}))
    if (event or {}).get("search"):
        return search_probe(event["search"], event.get("old_version", False))
    if (event or {}).get("similarity_report"):
        return similarity_report()
    return run_evaluation((event or {}).get("only", ""))


if __name__ == "__main__":
    result = run_evaluation()
    raise SystemExit(0 if result["passed"] == result["total"] else 1)
