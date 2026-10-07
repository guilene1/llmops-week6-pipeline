"""The few calls the pipeline makes to the Langfuse public API, with the standard library.

Used by trace.py, drift.py and promote_case.py. Keys come from LANGFUSE_PUBLIC_KEY,
LANGFUSE_SECRET_KEY and LANGFUSE_HOST, or from the stack's own secret (needs AWS
credentials). The nightly drift workflow uses the first: it has no AWS access at all.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "https://us.cloud.langfuse.com"


class LangfuseAPI:
    def __init__(self, public_key, secret_key, host=DEFAULT_HOST):
        self.host = (host or DEFAULT_HOST).rstrip("/")
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    @classmethod
    def from_environment(cls, from_secret=False):
        if from_secret:
            import stack
            value = stack.client("secretsmanager").get_secret_value(
                SecretId=f"{stack.PROJECT}/langfuse")["SecretString"]
            keys = json.loads(value)
            return cls(keys["public_key"], keys["secret_key"], keys.get("host"))
        try:
            return cls(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"],
                       os.environ.get("LANGFUSE_HOST") or DEFAULT_HOST)
        except KeyError:
            sys.exit("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, or use --from-secret.")

    def request(self, method, path, params=None, body=None, attempts=5):
        url = f"{self.host}/api/public/{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as error:
                # The free plan limits API calls per minute. Wait as told, then try again.
                if error.code in (429, 502, 503, 504) and attempt < attempts:
                    time.sleep(float(error.headers.get("Retry-After") or 5 * attempt))
                    continue
                raise

    def get(self, path, params=None):
        return self.request("GET", path, params)

    def post(self, path, body):
        return self.request("POST", path, body=body)

    def pages(self, path, params=None, limit=100, max_pages=500):
        """Every item of a paged list endpoint."""
        params = dict(params or {})
        for page in range(1, max_pages + 1):
            result = self.get(path, {**params, "page": page, "limit": limit})
            items = result.get("data", [])
            yield from items
            total_pages = (result.get("meta") or {}).get("totalPages")
            if not items or (total_pages is not None and page >= total_pages):
                return


def iso(moment):
    """A datetime as the API wants it: UTC, with a Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
