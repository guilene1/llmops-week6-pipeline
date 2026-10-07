"""Nightly drift: is real traffic still the traffic the golden set was written for?

    python pipeline/drift.py                                   # last 24 h against the 7 days before
    python pipeline/drift.py --current-since 2026-10-06T14:05:00Z

It reads traces from Langfuse, never the stack, so it needs no AWS access. Evaluation
traces (tagged source=eval) are left out: they are the same 45 questions every time.

Signals, each compared with the reference window:

    output_tokens      response length
    no_evidence        share of questions nothing in the policies or records answered
    refused            share the assistant declined
    best_similarity    how close the best policy passage was, on average
    fallback           share that needed the fallback model
    latency_s          p90 answer time
    topic mix          share of questions per top source title, as a population
                       stability index (PSI)

A numeric signal alerts when it moves more than 2 standard deviations. With at least 3
days of reference traffic, the deviation is that of the daily values over those days, so
an ordinary bad Tuesday does not alert. With less, it is the deviation of the same
statistic over samples of reference traffic the size of the current window (a
bootstrap), which is what a fresh project and the drift replay have. The topic mix
alerts when PSI is above 0.2, the usual line for "the population has changed", and also
above what chance alone produces at this much traffic: on small samples, noise reaches 0.2.

Traces with no evidence, or a best similarity below 0.35, go to a Langfuse annotation
queue, where a person decides whether each one should become a golden-set case
(pipeline/promote_case.py). That is the loop: production shows a gap, a person confirms
it, the gate tests for it from then on.
"""

import argparse
import json
import math
import random
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

from langfuse_api import LangfuseAPI, iso

Z_LIMIT = 2.0
PSI_LIMIT = 0.2
MIN_TRACES = 20          # fewer than this in a window and a signal is not judged
MIN_DAY_TRACES = 5       # a reference day needs this many to count as a day
MIN_DAYS = 3             # days needed for the daily method
LOW_SIMILARITY = 0.35    # below this, a trace goes to the review queue
QUEUE_NAME = "drift-review"
SCORE_NAME = "promote_to_golden_set"
MAX_QUEUED = 25
NO_SOURCE = "(no source)"

# name: (statistic, label, smallest standard deviation taken seriously)
SIGNALS = {
    "output_tokens": ("mean", "Response length (output tokens)", 2.0),
    "no_evidence": ("rate", "No-evidence rate", 0.01),
    "refused": ("rate", "Refused rate", 0.01),
    "best_similarity": ("mean", "Mean best similarity", 0.005),
    "fallback": ("rate", "Fallback rate", 0.01),
    "latency_s": ("p90", "p90 latency (s)", 0.1),
}


# ---------------------------------------------------------------------------
# Reading traces
# ---------------------------------------------------------------------------

def _number(value):
    """Metadata comes back as numbers or as their JSON text, depending on the type."""
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _text(value):
    return None if value in (None, "", "null") else str(value).strip('"')


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_record(trace, searches):
    """One row per question, from a trace and the policy searches inside it."""
    meta = trace.get("metadata") or {}
    outcome = _text(meta.get("outcome")) or "unknown"
    searches = sorted(searches, key=lambda s: s.get("startTime") or "")
    similarities = [s for s in (_number((o.get("metadata") or {}).get("best_similarity")) for o in searches)
                    if s is not None]
    titles = [t for t in (_text((o.get("metadata") or {}).get("top_source_title")) for o in searches) if t]
    return {
        "id": trace["id"],
        "timestamp": parse_time(trace["timestamp"]),
        "question": trace.get("input"),
        "outcome": outcome,
        "output_tokens": _number(meta.get("output_tokens")),
        "no_evidence": outcome == "no_evidence",
        "refused": outcome == "refused",
        "fallback": "model_fallback" in _list(meta.get("tools_used")),
        "latency_s": _number(trace.get("latency")),
        "best_similarity": max(similarities) if similarities else None,
        "top_source": titles[0] if titles else NO_SOURCE,
    }


def fetch(api, start, end):
    """Every chat trace in [start, end), without evaluation traffic."""
    traces = [t for t in api.pages("traces", {
        "fromTimestamp": iso(start), "toTimestamp": iso(end), "name": "chat",
        "orderBy": "timestamp.asc", "fields": "core,io,metrics",
    }) if "source=eval" not in (t.get("tags") or [])]

    searches = {}
    for observation in api.pages("observations", {
        "name": "policy-search", "fromStartTime": iso(start), "toStartTime": iso(end),
    }):
        searches.setdefault(observation.get("traceId"), []).append(observation)
    return [to_record(trace, searches.get(trace["id"], [])) for trace in traces]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def statistic(kind, values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    if kind == "p90":
        ordered = sorted(values)
        return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]
    return sum(values) / len(values)  # mean, and a rate is the mean of 0s and 1s


def _mean_std(values):
    mean = sum(values) / len(values)
    return mean, math.sqrt(sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1))


def compare_signal(name, current, reference, current_start, seed=7):
    kind, label, min_std = SIGNALS[name]
    result = {"signal": name, "label": label, "alert": False}
    cur_values = [r[name] for r in current if r[name] is not None]
    ref_values = [r[name] for r in reference if r[name] is not None]
    if len(cur_values) < MIN_TRACES or len(ref_values) < MIN_TRACES:
        result.update(note=f"not judged: {len(cur_values)} current and {len(ref_values)} reference values, "
                           f"needs {MIN_TRACES} of each")
        result["current"] = statistic(kind, cur_values)
        return result

    days = {}
    for record in reference:
        if record[name] is not None:
            day = int((current_start - record["timestamp"]).total_seconds() // 86400)
            days.setdefault(day, []).append(record[name])
    daily = [statistic(kind, v) for v in days.values() if len(v) >= MIN_DAY_TRACES]

    if len(daily) >= MIN_DAYS:
        method = f"daily, {len(daily)} reference days"
        mean, std = _mean_std(daily)
    else:
        # A bootstrap: the same statistic over many samples of reference traffic, each the
        # size of the current window. Seeded, so a re-run gives the same answer.
        method = "bootstrap of reference traffic"
        rng = random.Random(seed)
        samples = [statistic(kind, rng.choices(ref_values, k=len(cur_values))) for _ in range(400)]
        mean, std = _mean_std(samples)

    std = max(std, min_std)
    current_value = statistic(kind, cur_values)
    z = (current_value - mean) / std
    result.update(current=current_value, reference=mean, std=std, z=z, method=method,
                  alert=abs(z) > Z_LIMIT)
    return result


def psi(reference_counts, current_counts, floor=1e-4):
    """Population stability index between two category counts. 0.1 is a shift worth a look,
    0.2 a population that has changed."""
    ref_total, cur_total = sum(reference_counts.values()), sum(current_counts.values())
    total = 0.0
    for category in set(reference_counts) | set(current_counts):
        p = max(reference_counts.get(category, 0) / ref_total, floor)
        q = max(current_counts.get(category, 0) / cur_total, floor)
        total += (q - p) * math.log(q / p)
    return total


def psi_noise(reference, size, seed=7, samples=200):
    """The PSI that chance alone produces: reference traffic against samples of itself the
    size of the current window. PSI is biased upward on small samples. With ten topics and
    forty questions, noise alone reaches about 0.2, so 0.2 on its own would fire on a quiet
    day. 95th percentile."""
    rng = random.Random(seed)
    titles = [r["top_source"] for r in reference]
    ref_counts = Counter(titles)
    values = sorted(psi(ref_counts, Counter(rng.choices(titles, k=size))) for _ in range(samples))
    return values[int(0.95 * len(values)) - 1]


def compare_topics(current, reference):
    ref_counts = Counter(r["top_source"] for r in reference)
    cur_counts = Counter(r["top_source"] for r in current)
    result = {"signal": "topic_mix", "label": "Topic mix (top source title)", "alert": False}
    if len(current) < MIN_TRACES or len(reference) < MIN_TRACES:
        result["note"] = f"not judged: {len(current)} current and {len(reference)} reference questions"
        return result
    value = psi(ref_counts, cur_counts)
    noise = psi_noise(reference, len(current))
    movers = sorted(
        ((title, ref_counts.get(title, 0) / len(reference), cur_counts.get(title, 0) / len(current))
         for title in set(ref_counts) | set(cur_counts)),
        key=lambda m: abs(m[2] - m[1]), reverse=True)[:5]
    result.update(psi=value, noise=noise, alert=value > PSI_LIMIT and value > noise,
                  movers=[{"title": t, "reference_share": r, "current_share": c} for t, r, c in movers])
    return result


def review_candidates(current):
    return [r for r in current
            if r["no_evidence"] or (r["best_similarity"] is not None and r["best_similarity"] < LOW_SIMILARITY)]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt(name, value):
    if value is None:
        return "n/a"
    kind = SIGNALS.get(name, ("",))[0]
    if kind == "rate":
        return f"{100 * value:.1f}%"
    return f"{value:.3f}" if name == "best_similarity" else f"{value:.1f}"


def summary_markdown(window, results, topics, queued, current, reference):
    alerts = [r for r in results + [topics] if r.get("alert")]
    lines = [f"## Drift: {'ALERT' if alerts else 'no drift'}", "",
             f"Current window {window['current']}, {len(current)} questions. "
             f"Reference {window['reference']}, {len(reference)} questions. Evaluation traffic excluded.", "",
             "| Signal | Current | Reference | z | Method | |", "|---|---|---|---|---|---|"]
    for r in results:
        if "z" in r:
            lines.append(f"| {r['label']} | {_fmt(r['signal'], r['current'])} | "
                         f"{_fmt(r['signal'], r['reference'])} | {r['z']:+.1f} | {r['method']} | "
                         f"{'**ALERT**' if r['alert'] else 'ok'} |")
        else:
            lines.append(f"| {r['label']} | {_fmt(r['signal'], r.get('current'))} | | | {r['note']} | |")
    if "psi" in topics:
        lines += ["", f"**Topic mix PSI {topics['psi']:.2f}** (alert above {PSI_LIMIT}, and above "
                  f"{topics['noise']:.2f}, what chance alone reaches at this traffic): "
                  f"{'**ALERT**' if topics['alert'] else 'ok'}", "",
                  "| Top source title | Reference share | Current share |", "|---|---|---|"]
        lines += [f"| {m['title']} | {100 * m['reference_share']:.0f}% | {100 * m['current_share']:.0f}% |"
                  for m in topics["movers"]]
    else:
        lines += ["", f"Topic mix: {topics['note']}"]
    lines += ["", f"{queued} trace(s) added to the Langfuse annotation queue `{QUEUE_NAME}` for review."]
    return "\n".join(lines) + "\n"


def alert_text(results, topics, window, current, reference, queued, run_url=None):
    """One message, plain enough for Slack and Google Chat alike: both take {"text": ...}
    and render *bold* and <url|label>."""
    lines = ["*Northwind HR Assistant: drift alert*"]
    if topics.get("alert"):
        top = topics["movers"][0]
        lines.append(f"Topic mix PSI {topics['psi']:.2f} (limit {PSI_LIMIT}). Biggest move: {top['title']} "
                     f"{100 * top['reference_share']:.0f}% -> {100 * top['current_share']:.0f}%")
    for r in results:
        if r.get("alert"):
            lines.append(f"{r['label']}: {_fmt(r['signal'], r['reference'])} -> "
                         f"{_fmt(r['signal'], r['current'])} (z {r['z']:+.1f})")
    lines.append(f"{len(current)} questions in {window['current']}, against {len(reference)} in the reference.")
    lines.append(f"{queued} trace(s) queued for review in Langfuse ({QUEUE_NAME}).")
    if run_url:
        lines.append(f"<{run_url}|Workflow run and Evidently report>")
    return "\n".join(lines)


def post_webhook(url, text):
    request = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json; charset=UTF-8"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status


def evidently_report(current, reference, path):
    """The same two windows as an Evidently drift report: per column distributions and tests."""
    import pandas as pd
    from evidently import DataDefinition, Dataset, Report
    from evidently.presets import DataDriftPreset, DataSummaryPreset

    columns = ["output_tokens", "best_similarity", "latency_s", "top_source", "outcome"]
    definition = DataDefinition(numerical_columns=columns[:3], categorical_columns=columns[3:])

    def frame(records):
        return Dataset.from_pandas(pd.DataFrame([{c: r[c] for c in columns} for r in records]),
                                   data_definition=definition)

    snapshot = Report([DataDriftPreset(), DataSummaryPreset()]).run(
        current_data=frame(current), reference_data=frame(reference))
    snapshot.save_html(path)


# ---------------------------------------------------------------------------
# The review queue
# ---------------------------------------------------------------------------

def ensure_queue(api):
    """The queue's id, creating it (and the score a reviewer sets) the first time."""
    for queue in api.pages("annotation-queues"):
        if queue.get("name") == QUEUE_NAME:
            return queue["id"]
    config_id = next((c["id"] for c in api.pages("score-configs") if c.get("name") == SCORE_NAME), None)
    if config_id is None:
        config_id = api.post("score-configs", {
            "name": SCORE_NAME, "dataType": "BOOLEAN",
            "description": "True: turn this trace into a golden-set case with pipeline/promote_case.py",
        })["id"]
    return api.post("annotation-queues", {
        "name": QUEUE_NAME, "scoreConfigIds": [config_id],
        "description": "Traces the nightly drift job found with no evidence or low similarity",
    })["id"]


def queue_for_review(api, candidates):
    queue_id = ensure_queue(api)
    already = {item.get("objectId") for item in api.pages(f"annotation-queues/{queue_id}/items")}
    added = 0
    for record in candidates:
        if added >= MAX_QUEUED:
            break
        if record["id"] in already:
            continue
        api.post(f"annotation-queues/{queue_id}/items", {"objectId": record["id"], "objectType": "TRACE"})
        added += 1
    return added


# ---------------------------------------------------------------------------

def windows(now, current_hours, current_since, reference_days):
    current_start = parse_time(current_since) if current_since else now - timedelta(hours=current_hours)
    reference_start = current_start - timedelta(days=reference_days)
    return current_start, reference_start


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--current-hours", type=float, default=24)
    parser.add_argument("--current-since", default="", help="ISO time: the current window starts here")
    parser.add_argument("--reference-days", type=float, default=7)
    parser.add_argument("--json", default="drift.json")
    parser.add_argument("--summary", default="drift.md")
    parser.add_argument("--report", default="drift-report.html", help="Evidently HTML report")
    parser.add_argument("--webhook", default="", help="Slack or Google Chat incoming webhook URL")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--no-queue", action="store_true", help="do not add traces to the review queue")
    parser.add_argument("--fail-on-alert", action="store_true")
    parser.add_argument("--from-secret", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    current_start, reference_start = windows(now, args.current_hours, args.current_since, args.reference_days)
    api = LangfuseAPI.from_environment(args.from_secret)

    current = fetch(api, current_start, now)
    reference = fetch(api, reference_start, current_start)
    window = {"current": f"{iso(current_start)} to {iso(now)}",
              "reference": f"{iso(reference_start)} to {iso(current_start)}"}
    print(f"current: {len(current)} questions, reference: {len(reference)}")

    results = [compare_signal(name, current, reference, current_start) for name in SIGNALS]
    topics = compare_topics(current, reference)
    alerts = [r for r in results + [topics] if r.get("alert")]

    queued = 0
    if not args.no_queue:
        try:
            queued = queue_for_review(api, review_candidates(current))
        except Exception as error:  # the queue is a convenience: never lose the alert over it
            print(f"warning: could not add to the review queue: {error}", file=sys.stderr)

    summary = summary_markdown(window, results, topics, queued, current, reference)
    with open(args.summary, "w", encoding="utf-8") as handle:
        handle.write(summary)
    with open(args.json, "w", encoding="utf-8") as handle:
        json.dump({"window": window, "current_count": len(current), "reference_count": len(reference),
                   "signals": results, "topic_mix": topics, "queued": queued,
                   "alert": bool(alerts)}, handle, indent=2, default=str)
    print(summary)

    if args.report and current and reference:
        try:
            evidently_report(current, reference, args.report)
            print(f"Evidently report: {args.report}")
        except Exception as error:
            print(f"warning: no Evidently report: {error}", file=sys.stderr)

    if alerts and args.webhook:
        try:
            post_webhook(args.webhook, alert_text(results, topics, window, current, reference, queued, args.run_url))
            print("alert posted to the webhook")
        except Exception as error:
            print(f"warning: the webhook refused the alert: {error}", file=sys.stderr)
    elif alerts:
        print("alert raised; no webhook configured")

    return 1 if alerts and args.fail_on_alert else 0


if __name__ == "__main__":
    sys.exit(main())
