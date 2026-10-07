"""Wake Aurora before the evaluation, so the first case does not pay for it.

Aurora Serverless v2 scales to zero after 15 idle minutes, and the first query after that
can take longer than the chat function's 29 seconds. The evaluate function's search probe
reads the role_access table, so it wakes the database, and it embeds a query and searches
OpenSearch, so it wakes those too. It retries until one call succeeds.

    python pipeline/warm_up.py
"""

import argparse
import sys
import time

import stack


def warm_up(attempts, wait_seconds):
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            stack.invoke("evaluate", {"search": "How many paid sick days do I get?"}, read_timeout=120)
            print(f"attempt {attempt}: warm in {time.time() - started:.1f} s")
            return True
        except Exception as error:  # an invoke error, a timeout, a throttle: all mean "not yet"
            print(f"attempt {attempt}: not ready after {time.time() - started:.1f} s ({str(error)[:200]})")
            if attempt < attempts:
                time.sleep(wait_seconds)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--wait", type=int, default=20, help="seconds between attempts")
    args = parser.parse_args()
    if not warm_up(args.attempts, args.wait):
        print("The stack did not answer. Check the evaluate function's log.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
