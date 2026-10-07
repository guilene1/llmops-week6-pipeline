"""The drift replay: real sign-ins, real questions, a sudden change of subject.

    python pipeline/simulate_drift.py                  # reference traffic, then the burst
    python pipeline/simulate_drift.py --phase burst    # just the burst

It signs demo users in through Cognito exactly as the web app does (SRP, against the
app's own client, so the API accepts the ID token), then asks questions through the API.
These are real requests: they are traced as source=app, like anyone's.

    reference   ordinary questions across the policies: what normal looks like
    burst       mostly remote-work questions, mixed with ordinary ones. Say a new remote
                working rule was announced this morning and everyone is asking about it.

Between the two it prints a time. Run the nightly workflow with that time as
current_since, and it compares the burst with the reference: the topic mix moves and
the alert fires. The golden set has one remote-work case, so this is also the gap the
review queue and promote_case.py close.

Needs: the stack's Terraform outputs (it reads them, the demo users' password included
when Terraform set it; otherwise set DEMO_PASSWORD), and
pip install -r pipeline/requirements-drift.txt.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USERS = ["amara.diallo@northwind.example", "liam.fischer@northwind.example", "priya.raman@northwind.example"]

# Questions whose answers are spread across the policy documents
NORMAL = [
    "How many paid sick days do I get per year?",
    "How many unused vacation days can I carry over?",
    "Where do I submit a time off request?",
    "What is the mileage reimbursement rate?",
    "How much can I claim for meals per day when travelling?",
    "How long is paid parental leave for a primary caregiver in the US?",
    "How many days of bereavement leave do I get if a parent dies?",
    "How much tuition reimbursement can I get per year?",
    "Do I get paid during jury duty?",
    "Is Juneteenth a company holiday?",
    "What is the hotel cap per night in Munich?",
    "How much is the wellness allowance?",
    "When is payday in the US?",
    "How do I change my bank details?",
    "How many counselling sessions does the EAP give me?",
    "What does the relocation policy cover for an international move?",
    "What is the individual annual deductible on the Northwind Plus PPO plan?",
    "How do I report a concern about my manager?",
    "What is the dress code?",
    "How does the referral bonus work?",
    "When do I need a doctor's note for sick leave?",
    "Can I take unpaid leave for a career break?",
    "What is the per diem in London?",
    "How long do I have to submit an expense claim?",
]

# From remote_work_policy.md (current) and hybrid_and_remote_working.md (superseded)
REMOTE = [
    "How many days a week do I need to be in the office?",
    "Can I work from another country for a few weeks?",
    "How far in advance do I need approval to work abroad?",
    "What is the maximum number of days I can work from another country?",
    "Can I work from a different city in my own country without approval?",
    "Can my hybrid role become fully remote?",
    "Who approves converting a hybrid role to remote?",
    "Is there a home office allowance?",
    "What equipment does Northwind provide for working from home?",
    "Do remote employees get an internet allowance?",
    "Do hybrid employees get money for internet?",
    "What are the core collaboration hours?",
    "Can I work remotely from a country where Northwind has no entity?",
    "Who chooses the anchor days for my team?",
    "Is furniture covered by the home office allowance?",
    "What did the previous remote working policy say about working abroad?",
    "Under the old hybrid policy, how many office days a month were expected?",
    "Does Northwind pay for a coworking space?",
]


def terraform_outputs():
    result = subprocess.run(["terraform", "-chdir=infra/terraform", "output", "-json"],
                            capture_output=True, text=True, check=True)
    outputs = {key: value["value"] for key, value in json.loads(result.stdout).items()}
    client_id = next(line.split("=", 1)[1] for line in outputs["frontend_env"].splitlines()
                     if line.startswith("VITE_COGNITO_CLIENT_ID="))
    return outputs["api_endpoint"], outputs["cognito_user_pool_id"], client_id


def demo_password_from_terraform():
    """The password Terraform set (setup_application = true), or None."""
    result = subprocess.run(["terraform", "-chdir=infra/terraform", "output", "-raw", "demo_password"],
                            capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value and " " not in value else None


def sign_in(email, password, pool_id, client_id, region):
    """SRP sign-in, as the browser does. No AWS credentials involved: unsigned requests."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from pycognito.aws_srp import AWSSRP

    cognito = boto3.client("cognito-idp", region_name=region, config=Config(signature_version=UNSIGNED))
    tokens = AWSSRP(username=email, password=password, pool_id=pool_id, client_id=client_id,
                    client=cognito).authenticate_user()
    return tokens["AuthenticationResult"]["IdToken"]


def ask(api, token, question):
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/chat", data=json.dumps({"question": question}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=35) as response:
        return json.loads(response.read())


def send(api, tokens, questions, delay, label):
    ok = failed = 0
    for number, (user, question) in enumerate(questions, start=1):
        for attempt in (1, 2):
            try:
                answer = ask(api, tokens[user], question)["answer"]
                ok += 1
                print(f"  {label} {number:3}/{len(questions)} {user.split('.')[0]:6} {question[:58]:58} "
                      f"{answer[:40].replace(chr(10), ' ')}")
                break
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == 2:
                    failed += 1
                    print(f"  {label} {number:3}/{len(questions)} failed: {error}")
                else:
                    time.sleep(5)  # the first question after idle can time out while Aurora wakes
        time.sleep(delay)
    return ok, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", choices=["both", "reference", "burst"], default="both")
    parser.add_argument("--reference", type=int, default=48, help="ordinary questions before the split")
    parser.add_argument("--remote", type=int, default=30, help="remote-work questions in the burst")
    parser.add_argument("--normal", type=int, default=10, help="ordinary questions mixed into the burst")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between questions")
    parser.add_argument("--seed", type=int, default=6)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()

    password = os.environ.get("DEMO_PASSWORD") or demo_password_from_terraform()
    if not password:
        sys.exit("Set DEMO_PASSWORD to the demo users' password (deployment guide, step 6).")

    api, pool_id, client_id = terraform_outputs()
    tokens = {user: sign_in(user, password, pool_id, client_id, args.region) for user in USERS}
    print(f"signed in {len(tokens)} demo users through Cognito; asking {api}")
    rng = random.Random(args.seed)

    def mix(pool, count):
        return [(rng.choice(USERS), rng.choice(pool)) for _ in range(count)]

    if args.phase in ("both", "reference"):
        print(f"\nReference: {args.reference} ordinary questions")
        send(api, tokens, mix(NORMAL, args.reference), args.delay, "ref")

    split = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.phase in ("both", "burst"):
        time.sleep(2)
        burst = mix(REMOTE, args.remote) + mix(NORMAL, args.normal)
        rng.shuffle(burst)
        print(f"\nBurst: {args.remote} remote-work and {args.normal} ordinary questions, from {split}")
        send(api, tokens, burst, args.delay, "burst")

    print(f"\nSplit time: {split}")
    print("Compare the burst with what came before:")
    print(f"  gh workflow run nightly-drift.yml -f current_since={split}")
    print(f"  python pipeline/drift.py --current-since {split}      # the same, locally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
