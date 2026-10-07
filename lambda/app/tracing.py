"""Tracing to Langfuse: one trace per question, with a span for every step it took.

    chat                      the question, the answer, who asked, the outcome
      input-guardrail         blocked or not
      model-call              each Converse call: model, tokens, whether the fallback ran
      policy-search           best similarity, chunks kept, top source title
        query-embedding       Titan
      employee-lookup         how many records came back, never the records
      output-guardrail        changed or not

CloudWatch knows the function ran and how long it took. This is how you see why one
answer took nine seconds, which document it came from, and which model wrote it.

Four rules, and the code below is mostly about keeping them:

  * Tracing never fails a request. Every call into Langfuse is wrapped, and an error is
    printed once and swallowed. The worst case is a missing trace, never a missing answer.
  * Tracing never noticeably slows a request. Spans are exported by a background thread.
    flush(), which Lambda needs because it freezes the process after the handler returns,
    waits at most FLUSH_SECONDS and then lets the answer go anyway.
  * Nothing personal leaves the account unmasked. mask() runs on every input, output and
    metadata value: email addresses, phone numbers, and any number of four or more digits,
    which is where salaries are. user_id is the employee id, not the email.
  * Only questions are traced. Tracing is active inside request() and nowhere else, so
    ingestion, the pipeline's warm-up probe and the similarity report send nothing.

The keys live in Secrets Manager (infra/terraform/secrets.tf). While the secret holds its
placeholder values, or TRACING_ENABLED is false, every function here does nothing.
"""

import contextlib
import contextvars
import functools
import hashlib
import json
import re
import threading
import time
from pathlib import Path

from app import config

EXPORT_TIMEOUT_SECONDS = 2   # one attempt to send a batch of spans
FLUSH_SECONDS = 2.0          # the most a request waits for its spans to leave
SLOW_FLUSH_SECONDS = 1.0     # a healthy flush takes a fraction of this
SLOW_BACKOFF_SECONDS = 300   # after a slow flush, how long requests stop waiting
PLACEHOLDER = "REPLACE_ME"

# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# +1 (555) 123-4567, 555-123-4567, 0800 123 4567: a digit, eight or more digits or
# separators, a digit. A full stop after it ends the sentence, not the number.
PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d(?!\w)(?!\.\d)")
# 118,000 and 118000 and 4500.50, but not 0.67, 25 or 12.5. A comma after the number is
# punctuation ("146000, bonus"), a comma and a digit is more of the same number.
NUMBER = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?(?!\w)(?!,\d)")

# Metadata that is about the system, not about a person: left readable on purpose.
# Document titles contain years ("Compensation Bands 2026"), and the drift report
# groups traces by them.
UNMASKED_KEYS = {
    "top_source_title", "source_titles", "model", "model_id", "primary_model",
    "git_sha", "prompt_version", "outcome", "source", "embedding_model",
}


def mask_text(text):
    text = EMAIL.sub("[email]", text)
    text = PHONE.sub("[phone]", text)
    return NUMBER.sub("[number]", text)


def mask(data=None, **kwargs):
    """Langfuse calls this with data=<input, output or metadata> before anything is sent."""
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, dict):
        return {key: value if key in UNMASKED_KEYS else mask(data=value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=item) for item in data]
    return data  # numbers, booleans, None: counts and scores, not personal data


# ---------------------------------------------------------------------------
# The client, started once per Lambda container
# ---------------------------------------------------------------------------

_state = {"started": False, "client": None, "public_key": None}
_lock = threading.Lock()
_warned = set()

# Set by request(): which kind of traffic this is, and the chat it belongs to
_request = contextvars.ContextVar("tracing_request", default=None)


def _warn(what, error=None):
    """Print a tracing problem once per container, not once per request."""
    if what in _warned:
        return
    _warned.add(what)
    print(f"tracing: {what}" + (f": {type(error).__name__}: {error}" if error else ""))


def _read_keys():
    """The Langfuse keys, with short timeouts: a slow Secrets Manager must not hold up a question."""
    import boto3
    from botocore.config import Config

    secrets_manager = boto3.client(
        "secretsmanager", region_name=config.AWS_REGION,
        config=Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 2}),
    )
    value = secrets_manager.get_secret_value(SecretId=config.LANGFUSE_SECRET_ARN)["SecretString"]
    return json.loads(value)


def _start():
    if not config.TRACING_ENABLED:
        return None
    if not config.LANGFUSE_SECRET_ARN:
        return None
    try:
        keys = _read_keys()
        if PLACEHOLDER in keys.get("public_key", PLACEHOLDER) or PLACEHOLDER in keys.get("secret_key", PLACEHOLDER):
            _warn("off: the Langfuse secret still holds placeholder keys")
            return None

        from langfuse import Langfuse

        client = Langfuse(
            public_key=keys["public_key"],
            secret_key=keys["secret_key"],
            host=keys.get("host") or "https://us.cloud.langfuse.com",
            timeout=EXPORT_TIMEOUT_SECONDS,
            mask=mask,
            release=code_version(),
        )
        _state["public_key"] = keys["public_key"]
        print(f"tracing: on, sending to {keys.get('host')}")
        return client
    except Exception as error:  # tracing must never stop the function starting
        _warn("off: could not start", error)
        return None


def client():
    """The Langfuse client, or None when tracing is off."""
    if not _state["started"]:
        with _lock:
            if not _state["started"]:
                _state["client"] = _start()
                _state["started"] = True
    return _state["client"]


def active():
    return _request.get() is not None and client() is not None


# ---------------------------------------------------------------------------
# What the rest of the application calls
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def request(source, session_id=None, tags=(), flush=True):
    """Trace what happens inside: one question.

    source   "app" for real traffic, "eval" for the evaluation, so the two can be told
             apart and the drift report can leave evaluation runs out
    flush    send the spans before leaving. A chat request must; the evaluation flushes
             once at the end of its run instead.
    """
    token = _request.set({"source": source, "session_id": session_id, "tags": list(tags)})
    try:
        yield
    finally:
        _request.reset(token)
        if flush:
            flush_now()


def observe(name, as_type=None):
    """Langfuse's @observe, applied only while a request is being traced.

    Inputs and outputs are not captured automatically: function arguments here include
    employee records. What a span shows is set explicitly, and masked.
    """
    def decorate(function):
        traced = []

        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if not active():
                return function(*args, **kwargs)
            if not traced:
                try:
                    from langfuse import observe as langfuse_observe

                    traced.append(langfuse_observe(
                        name=name, as_type=as_type, capture_input=False, capture_output=False,
                    )(function))
                except Exception as error:
                    _warn("could not wrap a function", error)
                    return function(*args, **kwargs)
            # Name the client explicitly. Left to find it, Langfuse skips tracing whenever
            # more than one client exists in the process, to avoid sending to the wrong project.
            return traced[0](*args, langfuse_public_key=_state.get("public_key"), **kwargs)
        return wrapper
    return decorate


class _Observation:
    """A span that swallows its own errors. Without tracing, it does nothing."""

    def __init__(self, inner=None):
        self._inner = inner

    def update(self, **fields):
        if self._inner is None:
            return
        try:
            self._inner.update(**fields)
        except Exception as error:
            _warn("could not update a span", error)


@contextlib.contextmanager
def span(name, as_type="span", **fields):
    """A child span around a block of code:

        with tracing.span("input-guardrail", as_type="guardrail") as span:
            ...
            span.update(output={"blocked": blocked})
    """
    manager = None
    observation = _Observation()
    if active():
        try:
            manager = client().start_as_current_observation(name=name, as_type=as_type, **fields)
            observation = _Observation(manager.__enter__())
        except Exception as error:
            _warn(f"could not start span {name}", error)
            manager = None
    try:
        yield observation
    except BaseException as error:
        _close(manager, type(error), error, error.__traceback__)
        raise
    else:
        _close(manager, None, None, None)


def _close(manager, *exc_info):
    if manager is None:
        return
    try:
        manager.__exit__(*exc_info)
    except Exception as error:
        _warn("could not end a span", error)


def update_span(**fields):
    """Add output or metadata to the innermost span, for example from inside a helper."""
    if not active():
        return
    try:
        client().update_current_span(**fields)
    except Exception as error:
        _warn("could not update the current span", error)


def update_trace(**fields):
    """Set the trace's user, input, output and metadata. The request's source is added here."""
    if not active():
        return
    context = _request.get()
    metadata = {"source": context["source"], **(fields.pop("metadata", None) or {})}
    tags = [f"source={context['source']}", *context["tags"]]
    try:
        client().update_current_trace(
            session_id=context["session_id"], tags=tags, metadata=metadata, **fields,
        )
    except Exception as error:
        _warn("could not update the trace", error)


def trace_id():
    if not active():
        return None
    try:
        return client().get_current_trace_id()
    except Exception as error:
        _warn("could not read the trace id", error)
        return None


def flush_now(timeout=FLUSH_SECONDS):
    """Send what is buffered, waiting at most `timeout` seconds.

    The flush runs in its own thread. If Langfuse is slow or unreachable, the request
    stops waiting and carries on; the thread finishes, or not, on its own time. Lambda
    freezes it with the process and resumes it on the next invocation.

    After one slow flush, requests stop waiting at all for SLOW_BACKOFF_SECONDS. Without
    that, an unreachable Langfuse would add the full timeout to every question.
    """
    langfuse = _state["client"]
    if langfuse is None:
        return
    if time.monotonic() < _state.get("slow_until", 0):
        timeout = 0  # still start the flush, but do not wait for it
    finished = threading.Event()

    def run():
        try:
            langfuse.flush()
        except Exception as error:
            _warn("flush failed", error)
        finally:
            finished.set()

    started = time.monotonic()
    threading.Thread(target=run, name="langfuse-flush", daemon=True).start()
    if not timeout:
        return
    finished.wait(timeout)
    # Slow, not only unfinished: a failed export gives up after EXPORT_TIMEOUT_SECONDS,
    # which can land just inside the wait and look like success.
    if time.monotonic() - started > SLOW_FLUSH_SECONDS:
        _state["slow_until"] = time.monotonic() + SLOW_BACKOFF_SECONDS
        print(f"tracing: flush took {time.monotonic() - started:.1f} s. Not waiting for Langfuse "
              f"for the next {SLOW_BACKOFF_SECONDS} s; answers are not held back for it.")


# ---------------------------------------------------------------------------
# Versions, recorded on every trace
# ---------------------------------------------------------------------------

@functools.cache
def code_version():
    """The git commit this code was built from.

    The pipeline writes app/build_info.json before it builds the zip. A zip built on a
    laptop has none, so it gets a hash of the code instead: still different for every
    different build, and marked "local" so nobody mistakes it for a commit.
    """
    app_dir = Path(__file__).parent
    build_info = app_dir / "build_info.json"
    try:
        if build_info.exists():
            return json.loads(build_info.read_text(encoding="utf-8"))["git_sha"]
    except Exception as error:
        _warn("could not read build_info.json", error)
    digest = hashlib.sha256()
    for path in sorted(app_dir.glob("*.py")):
        digest.update(path.read_bytes())
    return "local-" + digest.hexdigest()[:10]


def text_version(text):
    """A short stable id for a prompt: change one word and it changes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
