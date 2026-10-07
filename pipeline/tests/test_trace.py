"""trace.py's tree, on a trace shaped like the Langfuse public API returns. No network."""

import trace as trace_cli

T0 = "2026-10-06T10:00:00.000Z"


def obs(id_, name, type_, start, end, parent=None, **extra):
    return {"id": id_, "name": name, "type": type_, "parentObservationId": parent,
            "startTime": f"2026-10-06T10:00:{start}Z", "endTime": f"2026-10-06T10:00:{end}Z", **extra}


SAMPLE = {
    "id": "abc123", "userId": "NW-1001", "sessionId": "12", "tags": ["source=app"],
    "input": "How many paid sick days do I get?", "output": "You get 10 paid sick days.",
    "metadata": {"outcome": "answered", "model_id": "amazon.nova-pro-v1:0",
                 "prompt_version": "1a2b3c4d5e", "git_sha": "9f8e7d6"},
    "observations": [
        obs("r", "chat", "SPAN", "00.000", "04.210"),
        obs("g1", "input-guardrail", "GUARDRAIL", "00.010", "00.128", "r", output='{"blocked": false}'),
        obs("m1", "model-call", "GENERATION", "00.130", "00.782", "r", model="amazon.nova-pro-v1:0",
            usageDetails={"input": 1203, "output": 41}, metadata={"fallback_used": False},
            output={"tool_calls": ["search_hr_policies"]}),
        obs("s", "policy-search", "RETRIEVER", "00.790", "01.221", "r",
            metadata={"best_similarity": 0.61, "chunks_kept": 4, "top_source_title": "Sick Leave and Absence Policy"}),
        obs("e", "query-embedding", "EMBEDDING", "00.800", "00.904", "s"),
        obs("g2", "output-guardrail", "GUARDRAIL", "04.000", "04.150", "r", output={"changed": False}),
    ],
}


def test_tree(capsys):
    trace_cli.print_tree(SAMPLE)
    out = capsys.readouterr().out
    assert "outcome  answered" in out and "user     NW-1001" in out
    lines = out.splitlines()
    chat = next(line for line in lines if line.startswith("chat"))
    assert "4210 ms" in chat
    model = next(line for line in lines if "model-call" in line)
    assert "652 ms" in model and "1,203 in / 41 out" in model and "asked for search_hr_policies" in model
    search = next(line for line in lines if "policy-search" in line)
    assert "best 0.61, 4 kept, top: Sick Leave and Absence Policy" in search
    embedding = next(line for line in lines if "query-embedding" in line)
    assert embedding.startswith("|  `- ")  # nested under policy-search
    assert "blocked=False" in out and "changed=False" in out
