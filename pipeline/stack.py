"""Names and AWS calls shared by the pipeline scripts.

Everything here talks to the one deployed stack. In GitHub Actions the credentials come
from the OIDC role (pipeline/bootstrap). On a laptop they are your own.
"""

import json
import os

PROJECT = os.environ.get("PROJECT_NAME", "northwind-hr")
REGION = os.environ.get("AWS_REGION", "us-east-1")


class InvokeError(RuntimeError):
    """The function ran and raised. The message is its error payload."""


def function_name(short_name):
    return f"{PROJECT}-{short_name}"


def client(service, read_timeout=60):
    import boto3
    from botocore.config import Config

    # One attempt: a Lambda invoke that is retried runs the function twice, and an
    # evaluation run is four minutes of Bedrock calls.
    return boto3.client(
        service,
        region_name=REGION,
        config=Config(
            connect_timeout=10,
            read_timeout=read_timeout,
            retries={"total_max_attempts": 1},
        ),
    )


def invoke(short_name, payload, read_timeout=60):
    """Invoke a function synchronously and return its decoded result."""
    response = client("lambda", read_timeout).invoke(
        FunctionName=function_name(short_name),
        Payload=json.dumps(payload).encode(),
    )
    body = response["Payload"].read().decode()
    if response.get("FunctionError"):
        raise InvokeError(body)
    return json.loads(body)


def write_github_output(**values):
    """Set step outputs when running in GitHub Actions. Does nothing elsewhere."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
