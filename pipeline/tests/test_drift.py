"""Drift detection, the review queue and promotion, against an in-memory Langfuse. No network.

The key test replays what simulate_drift.py sends, with its own question lists, and
checks the topic-mix alert fires on the burst and stays quiet on ordinary traffic.
"""

import json
import random
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import drift
import promote_case
import simulate_drift

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
REMOTE_TITLE = "Remote and Hybrid Work Policy"
TITLES = ["Sick Leave and Absence Policy", "Time Off Policy", "Expense and Reimbursement Policy",
          "Parental Leave Policy (United States)", "Bereavement Leave", "Payroll, Payslips and Pay Dates",
          "Tuition and Certification Reimbursement", "Wellness Allowance", "Per Diem Rates 2026",
          "Public Holiday Calendar 2026 (United States)"]


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeLangfuse:
    """Just enough of the public API: traces, observations, queues, score configs."""

    def __init__(self):
        self.traces, self.observations, self.queues, self.configs, self.items = [], [], [], [], {}

    def add(self, when, question, title, outcome="answered", tokens=60, similarity=0.55,
            latency=4.0, tags=("source=app",), fallback=False, user="NW-1005"):
        trace_id = f"t{len(self.traces):04d}"
        tools = ["search_hr_policies"] + (["model_fallback"] if fallback else [])
        # Metadata comes back as text for anything that is not a string or an int
        self.traces.append({"id": trace_id, "name": "chat", "timestamp": iso(when), "input": question,
                            "userId": user, "tags": list(tags), "latency": latency, "scores": [],
                            "metadata": {"outcome": outcome, "output_tokens": tokens,
                                         "tools_used": json.dumps(tools)}})
        self.observations.append({"traceId": trace_id, "name": "policy-search", "startTime": iso(when),
                                  "metadata": {"best_similarity": json.dumps(similarity),
                                               "top_source_title": title}})
        return trace_id

    @staticmethod
    def _within(stamp, start, end):
        return (not start or stamp >= start) and (not end or stamp < end)

    def pages(self, path, params=None, **_):
        params = params or {}
        if path == "traces":
            return [t for t in self.traces if t["name"] == params.get("name", "chat")
                    and self._within(t["timestamp"], params.get("fromTimestamp"), params.get("toTimestamp"))]
        if path == "observations":
            return [o for o in self.observations if o["name"] == params.get("name")
                    and self._within(o["startTime"], params.get("fromStartTime"), params.get("toStartTime"))]
        if path == "annotation-queues":
            return list(self.queues)
        if path == "score-configs":
            return list(self.configs)
        if path.startswith("annotation-queues/") and path.endswith("/items"):
            return list(self.items.get(path.split("/")[1], []))
        raise AssertionError(path)

    def post(self, path, body):
        if path == "score-configs":
            self.configs.append({"id": "cfg1", **body})
            return self.configs[-1]
        if path == "annotation-queues":
            self.queues.append({"id": "q1", **body})
            return self.queues[-1]
        if path.endswith("/items"):
            self.items.setdefault(path.split("/")[1], []).append(body)
            return body
        raise AssertionError(path)

    def get(self, path, params=None):
        return next(t for t in self.traces if path == f"traces/{t['id']}")


def title_for(question):
    if question in simulate_drift.REMOTE:
        return REMOTE_TITLE
    return TITLES[simulate_drift.NORMAL.index(question) % len(TITLES)]


def replay(api, start, count, pool, rng, minutes=1):
    for i in range(count):
        question = rng.choice(pool)
        api.add(start + timedelta(minutes=minutes * i), question, title_for(question),
                tokens=rng.randint(40, 90), similarity=rng.uniform(0.45, 0.65), latency=rng.uniform(3, 6))


def run_windows(api, current_since):
    start, reference_start = drift.windows(NOW, 24, current_since, 7)
    current, reference = drift.fetch(api, start, NOW), drift.fetch(api, reference_start, start)
    results = [drift.compare_signal(n, current, reference, start) for n in drift.SIGNALS]
    return current, reference, results, drift.compare_topics(current, reference)


# ---------------------------------------------------------------------------

def test_psi():
    same = {"a": 50, "b": 50}
    assert drift.psi(same, same) == pytest.approx(0)
    assert drift.psi({"a": 50, "b": 50}, {"a": 45, "b": 55}) < 0.1
    assert drift.psi({"a": 95, "b": 5}, {"a": 40, "b": 60}) > drift.PSI_LIMIT


def test_records_parse_text_metadata_and_drop_evaluation_traffic():
    api = FakeLangfuse()
    api.add(NOW - timedelta(hours=1), "q", "Time Off Policy", tokens=71, similarity=0.512, fallback=True)
    api.add(NOW - timedelta(hours=1), "q", "Time Off Policy", tags=("source=eval",))
    records = drift.fetch(api, NOW - timedelta(days=1), NOW)
    assert len(records) == 1
    r = records[0]
    assert r["output_tokens"] == 71 and r["best_similarity"] == pytest.approx(0.512)
    assert r["fallback"] is True and r["top_source"] == "Time Off Policy"


def test_the_replay_fires_the_topic_mix_alert():
    """What simulate_drift.py does, end to end through the drift logic."""
    api, rng = FakeLangfuse(), random.Random(6)
    reference_start = NOW - timedelta(hours=3)
    replay(api, reference_start, 48, simulate_drift.NORMAL, rng)              # --reference 48
    split = reference_start + timedelta(minutes=60)
    burst = [rng.choice(simulate_drift.REMOTE) for _ in range(30)] + \
            [rng.choice(simulate_drift.NORMAL) for _ in range(10)]          # --remote 30 --normal 10
    rng.shuffle(burst)
    for i, question in enumerate(burst):
        api.add(split + timedelta(minutes=i), question, title_for(question))

    current, reference, results, topics = run_windows(api, iso(split))
    assert len(current) == 40 and len(reference) == 48
    assert topics["alert"] and topics["psi"] > drift.PSI_LIMIT
    assert topics["movers"][0]["title"] == REMOTE_TITLE
    assert topics["movers"][0]["current_share"] == pytest.approx(0.75)
    assert all("bootstrap" in r["method"] for r in results if "method" in r)


def test_ordinary_traffic_does_not_alert():
    api, rng = FakeLangfuse(), random.Random(11)
    for day in range(7, 0, -1):  # a week of ordinary days, then an ordinary day
        replay(api, NOW - timedelta(days=day, hours=-1), 40, simulate_drift.NORMAL, rng, minutes=20)
    current, reference, results, topics = run_windows(api, "")
    assert len(current) == 40 and len(reference) >= 240
    assert not topics["alert"], topics
    assert not [r for r in results if r["alert"]], results
    assert all("daily" in r["method"] for r in results if "method" in r)


def test_a_rising_no_evidence_rate_alerts():
    api, rng = FakeLangfuse(), random.Random(3)
    for day in range(7, 0, -1):
        replay(api, NOW - timedelta(days=day, hours=-1), 40, simulate_drift.NORMAL, rng, minutes=20)
    for i in range(40):
        api.add(NOW - timedelta(hours=10) + timedelta(minutes=10 * i), "q", TITLES[i % len(TITLES)],
                outcome="no_evidence" if i % 3 == 0 else "answered", similarity=0.3 if i % 3 == 0 else 0.55)
    current, _, results, _ = run_windows(api, "")
    by_name = {r["signal"]: r for r in results}
    assert by_name["no_evidence"]["alert"] and by_name["no_evidence"]["z"] > drift.Z_LIMIT
    assert by_name["best_similarity"]["alert"] and by_name["best_similarity"]["z"] < -drift.Z_LIMIT
    assert len(drift.review_candidates(current)) == 14


def test_too_little_traffic_is_not_judged():
    api, rng = FakeLangfuse(), random.Random(1)
    replay(api, NOW - timedelta(days=2), 30, simulate_drift.NORMAL, rng)
    replay(api, NOW - timedelta(hours=2), 5, simulate_drift.REMOTE, rng)
    _, _, results, topics = run_windows(api, "")
    assert not topics["alert"] and "not judged" in topics["note"]
    assert all(not r["alert"] for r in results)


def test_review_queue_is_created_once_and_never_duplicates():
    api = FakeLangfuse()
    for _ in range(5):
        api.add(NOW - timedelta(hours=1), "q", drift.NO_SOURCE, outcome="no_evidence", similarity=0.2)
    candidates = drift.review_candidates(drift.fetch(api, NOW - timedelta(days=1), NOW))
    assert drift.queue_for_review(api, candidates) == 5
    assert drift.queue_for_review(api, candidates) == 0  # second night: already queued
    assert len(api.queues) == 1 and api.queues[0]["scoreConfigIds"] == ["cfg1"]
    assert api.configs[0]["name"] == drift.SCORE_NAME and api.configs[0]["dataType"] == "BOOLEAN"


class Webhook:
    def __init__(self):
        self.received = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                hook.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"


def test_main_alerts_the_webhook_and_writes_its_files(tmp_path, monkeypatch):
    api, rng = FakeLangfuse(), random.Random(6)
    now = datetime.now(timezone.utc)
    replay(api, now - timedelta(hours=3), 48, simulate_drift.NORMAL, rng)
    split = now - timedelta(minutes=50)
    for i in range(40):
        question = rng.choice(simulate_drift.REMOTE if i % 4 else simulate_drift.NORMAL)
        api.add(split + timedelta(minutes=1 + i), question, title_for(question))
    monkeypatch.setattr(drift.LangfuseAPI, "from_environment", classmethod(lambda cls, from_secret=False: api))
    hook = Webhook()
    monkeypatch.setattr("sys.argv", [
        "drift", "--current-since", iso(split), "--webhook", hook.url, "--fail-on-alert",
        "--json", str(tmp_path / "d.json"), "--summary", str(tmp_path / "d.md"),
        "--report", str(tmp_path / "r.html"), "--run-url", "https://example.invalid/run/1"])
    assert drift.main() == 1  # an alert, and --fail-on-alert
    text = hook.received[0]["text"]
    assert text.startswith("*Northwind HR Assistant: drift alert*")
    assert "Topic mix PSI" in text and REMOTE_TITLE in text and "<https://example.invalid/run/1|" in text
    assert json.loads((tmp_path / "d.json").read_text())["alert"] is True
    assert "## Drift: ALERT" in (tmp_path / "d.md").read_text()


# ---------------------------------------------------------------------------
# promote_case.py
# ---------------------------------------------------------------------------

def promotable(api, question="Can I work from another country for a few weeks?", marked=True):
    trace_id = api.add(NOW, question, REMOTE_TITLE, user="NW-1005")
    if marked:
        api.traces[-1]["scores"] = [{"name": promote_case.SCORE_NAME, "value": 1, "stringValue": "True"}]
    return trace_id


def run_promote(monkeypatch, tmp_path, api, *argv):
    golden = tmp_path / "golden.json"
    golden.write_text(promote_case.GOLDEN_SET.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(promote_case, "GOLDEN_SET", golden)
    monkeypatch.setattr(promote_case, "fetch_trace", lambda api_, trace_id: api.get(f"traces/{trace_id}"))
    monkeypatch.setattr(promote_case.LangfuseAPI, "from_environment", classmethod(lambda cls, from_secret=False: api))
    monkeypatch.setattr("sys.argv", ["promote_case", *argv, "--no-branch"])
    return golden


def test_promote_appends_a_reviewed_case(monkeypatch, tmp_path):
    api = FakeLangfuse()
    trace_id = promotable(api)
    golden = run_promote(monkeypatch, tmp_path, api, trace_id, "--must-include", "60 days",
                         "--source", REMOTE_TITLE, "--tag", "remote_work")
    assert promote_case.main() == 0
    cases = json.loads(golden.read_text(encoding="utf-8"))
    case = cases[-1]
    assert case["id"] == "promoted-can-i-work-from-another-country-for-a"
    assert case["email"] == "amara.diallo@northwind.example"  # from NW-1005 in the seed data
    assert case["must_include"] == ["60 days"] and case["source"] == REMOTE_TITLE
    assert {"promoted", f"trace:{trace_id}", "remote_work"} <= set(case["tags"])
    assert len(cases) == 46


def test_promote_refuses_an_unreviewed_trace(monkeypatch, tmp_path):
    api = FakeLangfuse()
    trace_id = promotable(api, marked=False)
    run_promote(monkeypatch, tmp_path, api, trace_id, "--must-include", "60 days")
    with pytest.raises(SystemExit, match="not marked"):
        promote_case.main()


def test_promote_refuses_a_masked_question(monkeypatch, tmp_path):
    api = FakeLangfuse()
    trace_id = promotable(api, question="Is the [number] allowance taxable?")
    run_promote(monkeypatch, tmp_path, api, trace_id, "--must-include", "x")
    with pytest.raises(SystemExit, match="masked"):
        promote_case.main()


def test_seed_data_lookup():
    assert promote_case.email_for("NW-1003") == "liam.fischer@northwind.example"
    assert promote_case.email_for("NW-9999") is None
