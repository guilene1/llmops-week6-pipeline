"""Rebuild the policy index, but only when what built it has changed.

There is one stack, shared by every pull request. So "did this pull request change the
documents or the chunking?" is the wrong question. The right one is "does the index on the
stack match this checkout?". A demo branch that broke the CSV reader leaves a broken index
behind it, and the next pull request must not be evaluated against that.

So the index carries a fingerprint: a hash of the documents and of the code that reads,
chunks and embeds them. It is stored beside the Terraform state, written only after a
rebuild succeeds, and compared at the start of every run.

    python pipeline/reindex.py check                      # is a rebuild needed?
    python pipeline/reindex.py run --bucket DOCUMENTS     # upload, rebuild, wait

The rebuild is the same one the deployment guide uses: upload the documents, then the
manifest, whose upload triggers the ingest function. This waits for its log line.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import stack

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = ROOT / "lambda" / "data" / "documents"

# What decides the contents of the index. config.py holds CHUNK_SIZE and CHUNK_OVERLAP.
INDEX_CODE = [
    "lambda/app/documents.py",
    "lambda/app/chunking.py",
    "lambda/app/embeddings.py",
    "lambda/app/search_index.py",
    "lambda/app/ingest_handler.py",
    "lambda/app/config.py",
]

INDEXED = re.compile(r"(\d+) documents, (\d+) chunks indexed")
FAILED = re.compile(r"\[ERROR\]|Task timed out|IngestionError|Traceback")


def fingerprint():
    """A hash of every input to the index. Line endings are normalised, so a Windows
    checkout and a Linux runner agree."""
    digest = hashlib.sha256()
    paths = sorted(DOCUMENTS_DIR.rglob("*")) + [ROOT / name for name in INDEX_CODE]
    for path in paths:
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def state_location(state_bucket):
    return state_bucket, f"{stack.PROJECT}/pipeline/index-fingerprint.json"


def read_record(state_bucket):
    """What the last successful rebuild recorded, or None."""
    if not state_bucket:
        return None
    bucket, key = state_location(state_bucket)
    try:
        body = stack.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)
    except Exception as error:
        if "NoSuchKey" in str(error) or "Not Found" in str(error):
            return None
        raise


def write_record(state_bucket, record):
    if not state_bucket:
        return
    bucket, key = state_location(state_bucket)
    stack.client("s3").put_object(
        Bucket=bucket, Key=key, Body=json.dumps(record, indent=2).encode(),
        ContentType="application/json",
    )


def upload_documents(bucket):
    """Make the bucket hold exactly the documents in this checkout, manifest excluded.

    Files no longer in the checkout are deleted from the bucket: ingestion refuses to run
    while the bucket holds a file the manifest does not list. The bucket is versioned, so
    a deleted file can still be recovered.
    """
    s3 = stack.client("s3", read_timeout=120)
    local = {p.name: p for p in DOCUMENTS_DIR.iterdir() if p.is_file() and p.name != "manifest.json"}

    remote = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        remote.update(item["Key"] for item in page.get("Contents", []))

    for name, path in sorted(local.items()):
        s3.upload_file(str(path), bucket, name)
    stale = sorted(remote - set(local) - {"manifest.json"})
    for name in stale:
        s3.delete_object(Bucket=bucket, Key=name)
    print(f"uploaded {len(local)} documents, removed {len(stale)} no longer in the repository")


def trigger_and_wait(bucket, timeout_seconds):
    """Upload the manifest, which starts ingestion, then read the ingest log until it ends."""
    logs = stack.client("logs")
    group = f"/aws/lambda/{stack.function_name('ingest')}"
    started_ms = int(time.time() * 1000) - 5000

    stack.client("s3").upload_file(str(DOCUMENTS_DIR / "manifest.json"), bucket, "manifest.json")
    print("manifest uploaded, ingestion started. Waiting for the ingest log ...")

    deadline = time.time() + timeout_seconds
    seen = []
    while time.time() < deadline:
        time.sleep(10)
        events = []
        for page in logs.get_paginator("filter_log_events").paginate(
            logGroupName=group, startTime=started_ms
        ):
            events.extend(page.get("events", []))
        seen = [event["message"].rstrip() for event in sorted(events, key=lambda e: e["timestamp"])]

        for line in seen:
            match = INDEXED.search(line)
            if match:
                return {"documents": int(match.group(1)), "chunks": int(match.group(2))}
        failed = [line for line in seen if FAILED.search(line)]
        if failed:
            raise RuntimeError("Ingestion failed:\n" + "\n".join(seen[-30:]))

    raise RuntimeError(
        f"No result in the ingest log after {timeout_seconds} s. Last lines:\n" + "\n".join(seen[-30:])
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["check", "run"])
    parser.add_argument("--state-bucket", default="", help="TF_STATE_BUCKET; without it, always rebuild")
    parser.add_argument("--bucket", help="the documents bucket (terraform output documents_bucket)")
    parser.add_argument("--sha", default="", help="the commit being indexed, for the record")
    parser.add_argument("--out", default="reindex.json", help="where to write what happened")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    current = fingerprint()
    record = read_record(args.state_bucket)
    needed = not record or record.get("fingerprint") != current

    if args.action == "check":
        reason = ("no record of a previous rebuild" if not record
                  else "documents or indexing code differ from the index" if needed
                  else f"index already built from these inputs (commit {record.get('sha', '?')[:7]})")
        print(f"rebuild needed: {str(needed).lower()} ({reason})")
        stack.write_github_output(needed=str(needed).lower())
        return 0

    if not args.bucket:
        parser.error("run needs --bucket")

    upload_documents(args.bucket)
    result = trigger_and_wait(args.bucket, args.timeout)
    previous_chunks = (record or {}).get("chunks")
    print(f"{result['documents']} documents, {result['chunks']} chunks"
          + (f" (previous index: {previous_chunks})" if previous_chunks else ""))

    write_record(args.state_bucket, {
        "fingerprint": current, "sha": args.sha,
        "documents": result["documents"], "chunks": result["chunks"],
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"reindexed": True, "previous_chunks": previous_chunks, **result}, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
