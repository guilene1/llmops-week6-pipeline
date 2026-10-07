# Week 6 project: Production Patterns, Part 2

Three stops from the deck, built around the Northwind HR Assistant you deployed in earlier
weeks:

| Stop | Pattern | What you build |
|---|---|---|
| 8 | Tracing | Every question becomes a trace in Langfuse: each step, its time, its tokens, masked |
| 9 | Evals in CI/CD | A pull request cannot merge until the golden set passes against the deployed stack |
| 10 | Drift | A nightly check that production questions still look like the golden set, and a way to turn the ones that do not into new test cases |

Each stop opens the way the deck does: the **risk** it answers, the **controls** that answer
it, and the **proof** that the controls work. Then the steps, then how to prove it.

You are still not writing the application. What you build is everything around it. The
application changes in a few places only, and each one is listed where it happens.

---

## Before you start

Deploying the application is one command this week, because the subject is the pipeline,
not the deployment.

**You need** the tools from the deployment guide's section 1: the AWS CLI v2, Terraform 1.11
or newer, Python 3.12 or newer, Node 20 or newer, and Git (Git Bash on Windows, and run
everything from it). Plus AWS credentials (`aws configure`, region `us-east-1`), a Langfuse
Cloud account (the Hobby plan is free), and optionally the GitHub CLI (`gh`).

1. **Get the code.** Import this repository into your GitHub account
   (**github.com/new/import**, as the deployment guide describes for the original), then
   clone your copy and `cd` into it.
2. **Deploy everything:**

   ```bash
   bash scripts/deploy.sh
   ```

   It does the whole deployment guide, in order, and stops to show you each Terraform plan
   before applying it (add `--yes` to skip that):

   | | What | Time |
   |---|---|---|
   | 1 | Checks the tools, your AWS credentials and the Bedrock models | seconds |
   | 2 | Bootstrap: the Terraform state bucket, the GitHub OIDC provider, the CI role ([`pipeline/bootstrap`](../pipeline/bootstrap/README.md)), trusted by the GitHub repository you cloned from | 1 minute |
   | 3 | Writes `infra/terraform/terraform.tfvars`, with a Cognito domain of its own | seconds |
   | 4 | Builds the two zips | 1 to 2 minutes |
   | 5 | `terraform apply`: the infrastructure, then the database and demo data, the 50 documents loaded and indexed, the web app published, the demo users' password set ([`app_setup.tf`](../infra/terraform/app_setup.tf)) | about 15 minutes |
   | 6 | Smoke test: the API answers, and one golden-set question is answered correctly end to end | 1 minute |

   At the end it prints the app's address. The demo users' password:

   ```bash
   terraform -chdir=infra/terraform output -raw demo_password
   ```

   If it stops part way (Aurora still waking is the usual reason), run it again. Finished
   steps are not repeated. The deployment guide still explains each step, if you want to
   see what the script did.

That is all. Start at Stop 8.

| Tool in the deck | In this repository | Why |
|---|---|---|
| LiteLLM in front of the model | The Bedrock **Converse** API, as the application already uses | One request shape for every Bedrock model, with IAM instead of an API key. The model is a Terraform variable |
| promptfoo calling the model | promptfoo calling the **evaluate Lambda** as its provider | Answers need Aurora and OpenSearch, which have no route in from outside the VPC. The Lambda answers inside it, so promptfoo tests the whole assistant, access control included |
| A frontier model as judge | **Amazon Nova Lite** through Converse | Same account, same IAM, no new vendor. It gates only once calibrated against human labels |

---

## Stop 8: Tracing

**Risk.** An answer is wrong, slow or expensive, and nobody can say why. CloudWatch knows the
function ran for nine seconds. It does not know that eight of them were the fallback model,
that the policy search found nothing above the floor, or which prompt version was live.

**Controls.**

- One trace per question, with a span for each step: input guardrail, query embedding,
  policy search, employee lookup, every model call, output guardrail.
- Every trace records who asked (the employee id, never the email), the git commit, the
  prompt version, the model, and the outcome: `answered`, `no_evidence`, `refused`,
  `guardrail_blocked`.
- Masking before anything leaves the account: email addresses, phone numbers, and every
  number of four digits or more, which is where the salaries are.
- Tracing can never fail a question, and waits at most 2 seconds for Langfuse.

**Proof.** One question in the app shows a complete, masked span tree in Langfuse, and the
cost of tracing to a request is measured, not assumed.

### What changed in the application

This is the one real application change of the week. Read the code before you deploy it:

| File | Change |
|---|---|
| `lambda/app/tracing.py` | New. The only code that talks to Langfuse, and every call is guarded |
| `lambda/app/assistant.py` | Spans around the guardrails, the model calls and the employee lookup; the outcome |
| `lambda/app/retrieval.py` | The policy-search and embedding spans |
| `lambda/app/chat_handler.py` | One traced request per question, flushed before the function returns |
| `lambda/requirements.txt` | `langfuse>=3,<4` |
| `infra/terraform/secrets.tf` | The secret `northwind-hr/langfuse`, with placeholder keys |
| `infra/terraform/nat.tf` | One NAT gateway, so the functions can reach Langfuse. About $1.20 a day |

Questions worth answering from the code: why does `tracing.request()` exist at all? (So that
ingestion and the pipeline's warm-up never send traces.) Why is the secret written with
`secret_string_wo`? (So your real keys are never in the Terraform state.) Why does the
flush run in a thread? (Because Langfuse being down must not hold up an answer.)

### Steps

1. In Langfuse, create a project in the **US** region and an API key pair.
2. Give the application the keys. `deploy.sh` already deployed everything tracing needs,
   with placeholder keys, so tracing has been off until now:

   ```bash
   bash scripts/set-langfuse-keys.sh
   ```

   It asks for the public key, the secret key (not shown as you type) and the region, checks
   them with Langfuse, stores them in the secret `northwind-hr/langfuse`, and restarts the
   functions so they read them. To skip the questions, copy `.env.example` to `.env` and
   fill in the three values first: the script, and `pipeline/trace.py`, read them from there.
   `.env` is in `.gitignore`; never commit it.

3. Sign in to the app as Amara and ask: `What is my salary?`

### Prove it

The chat function's log names each question's trace straight away. Langfuse shows it later:
**on the free Hobby plan, new traces appear after a delay of up to about 15 minutes** (the
Tracing page says "New data in ~15 min"). Measured here: questions asked at 11:59 appeared
at about 12:10. Nothing is wrong while you wait.

```bash
export MSYS_NO_PATHCONV=1      # Git Bash on Windows
aws logs tail /aws/lambda/northwind-hr-chat --since 15m --format short | grep "trace "
python pipeline/trace.py <trace-id>         # once Langfuse shows it; reads the keys from .env
```

You should see `chat` with a child for each step it took: the two guardrails, the employee
lookup (a salary question reads the records), and a model call before and after it, each with
its token counts. Ask a policy question too, and the policy search appears with its best
similarity and top source. In the Langfuse UI, open the same
trace: the answer reads `Your salary is [number]`. Amara saw 118,000. Langfuse never did.

Then measure what it costs:

```bash
aws lambda invoke --function-name northwind-hr-evaluate --cli-read-timeout 600 \
  --cli-binary-format raw-in-base64-out --payload '{"tracing_overhead": {"runs": 10}}' overhead.json
cat overhead.json
```

Write down `difference_ms`. Measured when this guide was written: **+0.17 s at p50** (2.30 s
off, 2.47 s on) and +0.36 s at p90, over 10 runs. Most of it is the flush to Langfuse through
the NAT gateway, which the function waits for before returning. Compare yours: if it is far
higher, something on the path to Langfuse is slow.

**Exercise.** Set `enable_nat = false`, apply, ask a question, and read the chat log. The
answer still arrives. What did tracing print, and how long did the question take? Then set
it back.

---

## Stop 9: Evals in CI/CD

**Risk.** A change that reads as harmless in review makes the assistant worse, quietly. A
prompt tidy-up, a threshold, a cheaper model, a parser simplification. Each looks fine in
the diff. None fails a unit test. The first person to notice is an employee who gets a wrong
answer about their leave.

**Controls.**

- A golden set of 45 cases with stable ids and categories: `factual`, `table_row`,
  `access_control`, `refusal`, `injection`, `multi_turn`, `out_of_scope`.
- A pipeline on every pull request that deploys the change to the stack and runs the golden
  set through promptfoo.
- A gate with four rules: every `access_control` case passes, every `injection` case
  passes, the pass rate stays within 2 points of the baseline, p90 latency stays under 20
  seconds.
- A baseline that changes only through a pull request of its own.
- A pull request comment that names every case that broke and the answer it got.

**Proof.** A harmless pull request passes. Each of four realistic changes is blocked, and the
comment says which cases broke and why.

### The pipeline

[`.github/workflows/eval-gate.yml`](../.github/workflows/eval-gate.yml):

```
0. What changed → 1. Lint and unit tests → 2. Terraform plan → 3. Deploy code
  → 4. Re-index → 5. Warm up Aurora → 6. promptfoo evaluation → eval-gate
```

One job per stage, in one line: the Actions tab draws it as a chain, and a failure stops
the chain at that box. `eval-gate` is the last box and the required check.

Read [`pipeline/README.md`](../pipeline/README.md) for what each stage does and the three
choices behind it. The short version: there is one stack, so every run first puts the stack
in step with the branch under test, and the pipeline's role can change configuration but
cannot create or delete anything.

Application changes for this stop: `evaluate_handler.py` returns every case with its id,
latency and tokens, and answers one case at a time for promptfoo (`{"ask": case}`).
`assistant.answer` can report its token counts.

### Steps

1. **GitHub variables.** Settings > Secrets and variables > Actions > Variables:

   | Variable | Value |
   |---|---|
   | `AWS_ROLE_ARN` | `terraform -chdir=pipeline/bootstrap output -raw pipeline_role_arn` |
   | `AWS_REGION` | `us-east-1` |
   | `TF_STATE_BUCKET` | `terraform -chdir=pipeline/bootstrap output -raw state_bucket` |
   | `TF_VARS` | the whole of your `infra/terraform/terraform.tfvars` |

   No secrets: AWS access is GitHub OIDC, and the role trusts only your repository.

2. **The first baseline.** With the stack running `main`:

   ```bash
   make baseline NOTE="first baseline"
   git switch -c baseline/first && git add pipeline/baseline.json
   git commit -m "Record the first eval baseline" && git push -u origin baseline/first
   ```

   Open the pull request and merge it once the gate passes.

3. **The required status check.** Settings > Branches > Add branch protection rule (or Rules
   > Rulesets) for `main`. Tick **Require status checks to pass before merging** and add
   `eval-gate`. It appears in the search once the workflow has run at least once. Without
   this step the gate is advice; with it, the merge button stays grey.

### Prove it: a harmless change passes

Change a comment in `lambda/app/config.py`, push a branch, open a pull request. Expect two
comments, the plan and the gate, and a green `eval-gate`. Note how long the run took.

### Exercises: four changes the gate must catch

```bash
make demo-branches        # or: bash pipeline/demo/make-demo-branches.sh
```

That creates four branches, one commit each. For each one, **before you push it**, read the
diff and write down which categories of case you expect to fail, and why. Then push it, open
a pull request, and compare your prediction with the comment.

| Branch | The change | Questions to answer from the comment |
|---|---|---|
| `demo/drop-citations` | The prompt stops asking for a "Sources:" line. "The web app shows sources anyway" | Why do answers with the right facts in them now fail? Read `_cited()` in `assistant.py` |
| `demo/raise-similarity-floor` | `min_best_similarity` 0.25 to 0.45, "to cut weak matches" | Which questions lost their evidence? Compare with the similarity report in the deployment guide |
| `demo/swap-to-lite` | The chat model becomes Nova Lite, 13 times cheaper | Which categories broke? Is it the facts, the citations, or the tool calls? |
| `demo/break-csv-rows` | The CSV reader returns one section per file instead of one per row | What happened to the chunk count? Why do the `table_row` cases fail and the others not? |

The branches share one stack, so the gate runs them one at a time. Close each pull request
without merging. The next run puts the stack back.

**Exercise.** The `drop-citations` comment shows answers that contain the right fact and still
fail. Is the gate wrong, or the change? Argue it either way, then read the "Sources:" rule in
`SYSTEM_PROMPT` again.

**Optional: the grounding judge.** Set the variable `ENABLE_JUDGE` to `true` and the gate also
asks Nova Lite to score each cited answer against its passages. It only blocks once
calibrated:

```bash
make calibrate-judge      # 40 human-labelled answers, 20 good, 20 bad
```

If agreement is at least 85 percent, commit `pipeline/judge_calibration_report.json`. Read the
rows marked MISS first: which kinds of bad answer does the judge let through?

---

## Stop 10: Drift and the loop

**Risk.** The golden set passes every night and the assistant is still failing people,
because people have started asking about something the golden set never thought of. A
policy changes, a reorganisation is announced, a new benefit launches. Nothing is broken.
The traffic moved.

**Controls.**

- A nightly comparison of the last 24 hours of real traffic with the 7 days before:
  response length, no-evidence and refused rates, best similarity, fallback rate, p90
  latency, and the topic mix as a population stability index.
- An alert to Slack or Google Chat when a signal moves more than 2 standard deviations, or
  the topic mix PSI goes above 0.2 (and above what chance alone produces).
- An Evidently report of the same two windows, kept for 30 days.
- A review queue in Langfuse for the questions the assistant could not ground.
- A script that turns a reviewed question into a golden-set case, through a pull request
  and the gate.

**Proof.** A replayed burst of remote-work questions fires the topic-mix alert. A trace from
the review queue becomes a golden-set case through a pull request, and that pull request
passes the gate.

### Steps

1. **GitHub secrets.** Settings > Secrets and variables > Actions > Secrets:
   `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (the same keys as in the stack's secret), and
   `DRIFT_WEBHOOK_URL`: a Slack incoming webhook, or a Google Chat space webhook (Space >
   Apps and integrations > Webhooks). Both accept the same message.
2. **Run it once by hand**, to create the review queue and see a quiet night:

   ```bash
   gh workflow run nightly-drift.yml
   ```

   With little traffic so far, most signals say "not judged". That is correct: fewer than 20
   questions is not evidence of anything.

### Closing exercise: the drift replay

```bash
pip install -r pipeline/requirements-drift.txt   # it reads the demo password from Terraform
python pipeline/simulate_drift.py          # about 8 minutes
```

The script signs three demo users in through Cognito, asks 48 ordinary questions, prints a
time, then asks 30 remote-work questions mixed with 10 ordinary ones. Run the nightly check
with that time:

```bash
gh workflow run nightly-drift.yml -f current_since=<the printed time>
```

1. **The alert.** Your channel gets a message: topic mix PSI well above 0.2, "Remote and
   Hybrid Work Policy" from a few percent of questions to most. The run is red. Open its
   summary, then download `drift-report` and open the Evidently report. Which other signals
   moved, and which did not? Why would response length move with the topic?
2. **The queue.** In Langfuse, Annotation Queues > `drift-review`. Read the queued traces.
   Pick a remote-work question the golden set should cover. Check the answer against
   `remote_work_policy.md` yourself. Set `promote_to_golden_set` to true.
3. **The case.** Promote it, with the fact a right answer must contain:

   ```bash
   python pipeline/promote_case.py <trace-id> --from-secret --try \
     --must-include "60 days" --source "Remote and Hybrid Work Policy" --tag remote_work
   git push -u origin golden/<the printed case id>
   ```

   `--try` asks the stack first. If the assistant gets it wrong today, the case would block
   the gate: decide whether the assistant or your expectation is wrong before you push.
4. **The gate.** Open the pull request. The comment lists the new case under "Not in the
   baseline", passing. Merge it. Record a new baseline in its own pull request.

That is the loop: production showed a gap, a person confirmed it, and from now on every
pull request is tested against it.

**Exercise.** The replay also includes "what did the previous remote working policy say"
questions. The older document is `superseded`. What does the assistant answer, which
document does it cite, and should either be a golden-set case?

---

## Cost for a week

Estimates for one stack in us-east-1, from the figures measured in this repository and list
prices. The infrastructure that exists whether or not anyone asks a question is still the
bill.

| Item | A week | Notes |
|---|---|---|
| OpenSearch Serverless | about $40 | The largest item, as in earlier weeks |
| NAT gateway and its public address | about $8.40 | $1.20 a day. `enable_nat = false` removes it, and tracing |
| AWS WAF | about $2 | |
| Aurora Serverless v2 | $1 to $5 | Wakes for every gate run and every replay |
| Bedrock | $2 to $4 | A gate run is 45 questions, about $0.10. Twenty runs, two replays, a judge calibration |
| Secrets Manager | about $0.30 | Three secrets |
| Lambda, API Gateway, CloudFront, S3, Cognito | under $1 | Mostly free tier |
| S3 state bucket | cents | |
| GitHub Actions | $0 | Free for public repositories. Private: a gate run is 10 to 15 minutes of the 2,000 free a month |
| Langfuse Hobby | $0 | 50,000 units a month. A gate run is about 400 (45 traces and their spans) |
| **Total** | **about $55 to $65** | |

---

## Teardown, in this order

The order matters because the stack's state lives in the bucket the bootstrap created.

1. **Stop the schedule**, so it does not fail every night against a stack that is gone:

   ```bash
   gh workflow disable nightly-drift.yml
   ```

   Close any open demo pull requests.
2. **Destroy the stack**, exactly as in the deployment guide. About 20 minutes, most of it
   Lambda network interfaces detaching. Then check per service, not with the tagging index.

   ```bash
   bash scripts/deploy.sh destroy
   ```

3. **Destroy the bootstrap**: the state bucket, the pipeline role, the OIDC provider. Last,
   because step 2 needed the bucket.

   ```bash
   terraform -chdir=pipeline/bootstrap apply   -var allow_state_bucket_destroy=true
   terraform -chdir=pipeline/bootstrap destroy -var allow_state_bucket_destroy=true
   ```

4. **Outside AWS.** In Langfuse, revoke the API keys (and delete the project if you are done
   with it). In GitHub, remove the repository variables and secrets. In Slack or Google Chat,
   delete the webhook.

Check that nothing is left:

```bash
aws s3api list-buckets --query "length(Buckets[?starts_with(Name,'northwind-hr')])"   # 0
aws iam get-role --role-name northwind-hr-github-pipeline 2>&1 | grep -c NoSuchEntity  # 1
aws ec2 describe-nat-gateways --filter Name=state,Values=available \
  --query 'length(NatGateways)'                                                        # 0
```

---

## Where to look

| Path | What it is |
|---|---|
| [`pipeline/README.md`](../pipeline/README.md) | The pipeline in detail: tracing, the gate, the judge, drift, setup |
| [`pipeline/bootstrap/`](../pipeline/bootstrap/README.md) | State bucket, OIDC, the pipeline role and exactly what it may do |
| [`.github/workflows/`](../.github/workflows) | `eval-gate.yml`, `nightly-drift.yml` |
| [`pipeline/promptfoo/`](../pipeline/promptfoo) | The golden set as a promptfoo suite |
| [`lambda/data/evaluation_questions.json`](../lambda/data/evaluation_questions.json) | The golden set itself |
| [`lambda/app/tracing.py`](../lambda/app/tracing.py) | Tracing and masking |
