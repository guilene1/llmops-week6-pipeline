"""Tracing, with the real Langfuse SDK and a local stand-in for Langfuse Cloud. No AWS.

What these prove:
  * masking removes emails, phone numbers and salaries before anything is sent
  * one question produces one trace with the expected spans, nested correctly
  * tracing off, misconfigured or unreachable never changes the answer
  * an unreachable Langfuse costs a request at most the flush timeout, once
"""

import gzip
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import assistant, config, embeddings, employees, retrieval, search_index, tracing

SALARY = "118,000"
EMAIL = "amara.diallo@northwind.example"
PHONE = "+1 (555) 123-4567"

# One test points tracing at an address that never answers, on purpose. The exporter then
# reports "Failed to export spans batch" as Python exits, after the results, which reads
# like a failure and is not one. In the Lambda that message stays: there it means something.
logging.getLogger("opentelemetry.exporter.otlp.proto.http").setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (f"Your salary is {SALARY}.", "Your salary is [number]."),
    ("Base 146000, bonus 12.5 percent", "Base [number], bonus 12.5 percent"),
    ("The band is $152,000 to $192,000", "The band is $[number] to $[number]"),
    (f"Write to {EMAIL}", "Write to [email]"),
    (f"Call {PHONE} today", "Call [phone] today"),
    (f"Or call {PHONE}.", "Or call [phone]."),
    ("Salary: 146000.", "Salary: [number]."),
    ("10 paid sick days, 0.67 a mile, 26 weeks", "10 paid sick days, 0.67 a mile, 26 weeks"),
])
def test_mask_text(text, expected):
    assert tracing.mask_text(text) == expected


def test_mask_walks_structures_and_keeps_system_fields():
    data = {
        "question": f"What is {EMAIL}'s salary?",
        "records": [{"base_salary": "118000"}],
        "top_source_title": "Compensation Bands 2026",
        "chunks_kept": 4096,
        "best_similarity": 0.361,
    }
    masked = tracing.mask(data=data)
    assert masked["question"] == "What is [email]'s salary?"
    assert masked["records"] == [{"base_salary": "[number]"}]
    assert masked["top_source_title"] == "Compensation Bands 2026"  # a title, not personal data
    assert masked["chunks_kept"] == 4096 and masked["best_similarity"] == 0.361


# ---------------------------------------------------------------------------
# A whole question, with everything outside the code faked
# ---------------------------------------------------------------------------

class FakeBedrock:
    """Round 1 asks for both tools. Round 2 answers, with things that must be masked.
    Then it starts again, so one fake can answer several questions."""

    def __init__(self):
        self.calls = 0

    def converse(self, **request):
        self.calls += 1
        if self.calls % 2 == 1:
            return {"stopReason": "tool_use", "usage": {"inputTokens": 1200, "outputTokens": 40},
                    "output": {"message": {"role": "assistant", "content": [
                        {"toolUse": {"toolUseId": "t1", "name": "search_hr_policies",
                                     "input": {"query": "paid sick days"}}},
                        {"toolUse": {"toolUseId": "t2", "name": "get_employee_records", "input": {}}},
                    ]}}}
        return {"stopReason": "end_turn", "usage": {"inputTokens": 1500, "outputTokens": 60},
                "output": {"message": {"role": "assistant", "content": [{"text":
                    f"You get 10 paid sick days. Your salary is {SALARY}. Questions: {EMAIL} "
                    f"or {PHONE}.\n\nSources: Sick Leave and Absence Policy"}]}}}


@pytest.fixture
def offline_assistant(monkeypatch):
    monkeypatch.setattr(config, "GUARDRAIL_ID", "")
    fake = FakeBedrock()
    monkeypatch.setattr(assistant, "client", lambda: fake)
    monkeypatch.setattr(assistant, "log_request", lambda *args: None)
    monkeypatch.setattr(retrieval, "allowed_access_levels", lambda role: ["general"])
    monkeypatch.setattr(embeddings, "embed", lambda texts: [[0.1, 0.2, 0.3] for _ in texts])
    monkeypatch.setattr(search_index, "search", lambda vector, levels, **filters: [{
        "_score": 1.61,
        "_source": {"title": "Sick Leave and Absence Policy", "section": "Paid sick leave",
                    "content": "United States: 10 paid sick days per calendar year", "status": "current"},
    }])
    monkeypatch.setattr(employees, "visible_records", lambda viewer: [
        {"employee_id": "NW-1001", "full_name": "Amara Diallo", "base_salary": 118000}])
    return {"employee_id": "NW-1001", "full_name": "Amara Diallo", "job_title": "Software Engineer II",
            "department": "Engineering", "role": "employee"}


class Collector:
    """Stands in for Langfuse Cloud's OTLP endpoint and keeps what it receives."""

    def __init__(self):
        self.bodies = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                collector.bodies.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def spans(self):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        found = []
        for path, body in self.bodies:
            if not path.endswith("/v1/traces"):
                continue
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            for resource in request.resource_spans:
                for scope in resource.scope_spans:
                    for span in scope.spans:
                        attributes = {a.key: _value(a.value) for a in span.attributes}
                        found.append({"name": span.name, "id": span.span_id, "parent": span.parent_span_id,
                                      "trace": span.trace_id, "attributes": attributes})
        return found


def _value(value):
    """An OTLP attribute value as plain Python. Tags arrive as an array."""
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [_value(item) for item in value.array_value.values]
    return getattr(value, kind) if kind else None


def use_langfuse(monkeypatch, host):
    """Point tracing at `host`, with keys unique to this test (the SDK caches per key)."""
    monkeypatch.setattr(config, "LANGFUSE_SECRET_ARN", "arn:aws:secretsmanager:test")
    monkeypatch.setattr(config, "TRACING_ENABLED", True)
    monkeypatch.setattr(tracing, "_read_keys", lambda: {
        "public_key": f"pk-lf-test-{uuid.uuid4().hex}", "secret_key": "sk-lf-test", "host": host})
    monkeypatch.setattr(tracing, "_state", {"started": False, "client": None})


@pytest.fixture
def no_tracing(monkeypatch):
    monkeypatch.setattr(config, "LANGFUSE_SECRET_ARN", "")
    monkeypatch.setattr(tracing, "_state", {"started": False, "client": None})


def test_without_a_secret_tracing_does_nothing(offline_assistant, no_tracing):
    usage = {}
    with tracing.request("app"):
        text, sources = assistant.answer("How many sick days?", [], offline_assistant, usage)
    assert "10 paid sick days" in text
    assert usage["trace_id"] is None and usage["outcome"] == "answered"
    assert tracing.client() is None


def test_placeholder_keys_leave_tracing_off(offline_assistant, monkeypatch):
    use_langfuse(monkeypatch, "http://127.0.0.1:9")
    monkeypatch.setattr(tracing, "_read_keys", lambda: {
        "public_key": "pk-lf-REPLACE_ME", "secret_key": "sk-lf-REPLACE_ME", "host": "x"})
    with tracing.request("app"):
        text, _ = assistant.answer("How many sick days?", [], offline_assistant, {})
    assert "10 paid sick days" in text and tracing.client() is None


def test_a_failing_secrets_manager_does_not_fail_the_question(offline_assistant, monkeypatch):
    use_langfuse(monkeypatch, "http://127.0.0.1:9")

    def broken():
        raise TimeoutError("secrets manager did not answer")
    monkeypatch.setattr(tracing, "_read_keys", broken)
    with tracing.request("app"):
        text, _ = assistant.answer("How many sick days?", [], offline_assistant, {})
    assert "10 paid sick days" in text


def test_a_broken_sdk_does_not_fail_the_question(offline_assistant, monkeypatch):
    class Broken:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise RuntimeError(f"langfuse {name} broke")
            return fail
    monkeypatch.setattr(config, "LANGFUSE_SECRET_ARN", "arn")
    monkeypatch.setattr(tracing, "_state", {"started": True, "client": Broken()})
    with tracing.request("app"):
        text, _ = assistant.answer("How many sick days?", [], offline_assistant, {})
    assert "10 paid sick days" in text


def test_one_question_is_one_masked_trace_with_every_span(offline_assistant, monkeypatch):
    collector = Collector()
    use_langfuse(monkeypatch, collector.url)
    usage = {}
    with tracing.request("eval", session_id="eval-test"):
        text, _ = assistant.answer("How many paid sick days do I get?", [], offline_assistant, usage)

    assert SALARY in text  # the person still gets the real answer ...
    spans = collector.spans()
    assert spans, "nothing reached the collector"

    # ... and nothing personal left the function
    sent = repr([s["attributes"] for s in spans])
    for secret in (SALARY, "118000", EMAIL, "555) 123-4567"):
        assert secret not in sent
    assert "[number]" in sent and "[email]" in sent

    # One trace, rooted at "chat", with each step under it
    assert len({s["trace"] for s in spans}) == 1
    by_name = {}
    for s in spans:
        by_name.setdefault(s["name"], []).append(s)
    root = by_name["chat"][0]
    assert root["parent"] == b""
    for name in ("input-guardrail", "policy-search", "employee-lookup", "output-guardrail"):
        assert by_name[name][0]["parent"] == root["id"], name
    assert len(by_name["model-call"]) == 2
    assert by_name["query-embedding"][0]["parent"] == by_name["policy-search"][0]["id"]

    # The trace carries who and what, the evaluation tag, and the outcome
    root_attrs = repr(root["attributes"])
    assert "NW-1001" in root_attrs            # user id: the employee id
    assert EMAIL not in root_attrs            # never the email
    assert "source=eval" in root_attrs
    assert "answered" in root_attrs and assistant.PROMPT_VERSION in root_attrs
    search = repr(by_name["policy-search"][0]["attributes"])
    assert "Sick Leave and Absence Policy" in search and "0.61" in search
    model = repr(by_name["model-call"][-1]["attributes"])
    assert "amazon.nova-pro-v1:0" in model and "fallback_used" in model

    assert usage["trace_id"] and usage["outcome"] == "answered"


def test_an_unreachable_langfuse_costs_bounded_waits_then_nothing(offline_assistant, monkeypatch):
    # 10.255.255.1 is not routable: connections hang until they time out, the way they
    # do from a private subnet with no NAT.
    #
    # The first flush can return early while the exporter is still stuck connecting, so
    # the wait that runs out may be the first request's or the second's. Either way: no
    # request waits longer than FLUSH_SECONDS (plus starting the client, once), at most
    # two wait at all, and once a wait has run out nobody waits again.
    use_langfuse(monkeypatch, "http://10.255.255.1:81")
    timings = []
    for _ in range(5):
        started = time.monotonic()
        with tracing.request("app"):
            text, _ = assistant.answer("How many paid sick days do I get?", [], offline_assistant, {})
        timings.append(time.monotonic() - started)
        assert "10 paid sick days" in text
    assert max(timings) < tracing.FLUSH_SECONDS + 2.0, timings
    slow = [t for t in timings if t > 0.5]
    assert len(slow) <= 2, timings
    assert max(timings[2:]) < 0.5, timings  # backed off: no more waiting
    assert "slow_until" in tracing._state


def test_outside_a_request_nothing_is_traced(offline_assistant, monkeypatch):
    collector = Collector()
    use_langfuse(monkeypatch, collector.url)
    retrieval.search_policies("paid sick days", "employee")  # the pipeline's warm-up probe does this
    assert tracing._state["started"] is False  # the client was never even started
