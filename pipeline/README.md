# pipeline

The Week 6 pipeline around the Northwind HR Assistant. The application lives in
`lambda/` and `infra/`. Everything that tests, gates and watches it lives here and in
`.github/workflows/`.

## Tracing (Stop 8)

Every question becomes one trace in Langfuse Cloud, with a span for each step:

```
chat                               user NW-1001, outcome, model, prompt version, git sha
|- input-guardrail      guardrail  blocked or not
|- model-call           generation model id, tokens in and out, whether the fallback ran
|- policy-search        retriever  best similarity, chunks kept, top source title
|  `- query-embedding   embedding  Titan
|- employee-lookup      tool       how many records came back, never the records
|- model-call           generation ...
`- output-guardrail     guardrail  changed or not
```

The code is [`lambda/app/tracing.py`](../lambda/app/tracing.py). What it promises:

- **It never fails a request.** Every call into Langfuse is wrapped. A broken SDK, a
  missing secret or an unreachable Langfuse costs a trace, not an answer. The tests in
  `lambda/tests/test_tracing.py` break each of those on purpose.
- **It never noticeably slows a request.** Spans leave on a background thread. The chat
  function waits at most 2 seconds for them before returning, and once a wait runs out it
  stops waiting at all for 5 minutes. With Langfuse unreachable, that means one or two
  questions take up to 2 seconds longer, then none do. Measured locally: about 2.7 seconds
  for the first (starting the client included), then 3 to 10 milliseconds.
- **Nothing personal leaves unmasked.** Email addresses, phone numbers and every number of
  four or more digits (salaries) become `[email]`, `[phone]` and `[number]` before anything
  is sent. `user_id` is the employee id, never the email. Document titles, model ids and
  versions stay readable.
- **Only questions are traced.** Chat requests are tagged `source=app`, evaluation cases
  `source=eval`, one Langfuse session per evaluation run. Ingestion and the pipeline's
  warm-up probe send nothing.

Every trace records `outcome`: `answered`, `no_evidence` (nothing in the policies or the
records answered it), `refused` (usually a record this person may not see) or
`guardrail_blocked`. The drift report in Stop 10 counts them.

### Turning it on

1. **Langfuse.** Sign up at [langfuse.com](https://langfuse.com) (the Hobby plan is free),
   create a project in the **US** region, and under **Settings > API Keys** create a key
   pair. US, because the stack is in us-east-1: each flush crosses that distance.
2. **The stack.** A stack deployed with `bash scripts/deploy.sh` already has everything
   below. One deployed earlier needs the new layer and an apply from your laptop (the
   pipeline's role cannot create the new secret); running `bash scripts/deploy.sh` again
   does both.

   Expect a new secret, `northwind-hr/langfuse`, holding placeholders; a new layer
   version; `LANGFUSE_SECRET_ARN` and `TRACING_ENABLED` on chat and evaluate; and one more
   statement in their IAM policy. `enable_nat` must be `true`: Langfuse is outside AWS.
3. **The keys.** Put them in the secret yourself, so they are never in code, tfvars or the
   Terraform state (the secret uses a write-only argument, so Terraform never reads them
   back or overwrites them):

   ```bash
   bash scripts/set-langfuse-keys.sh
   ```

   It checks the keys with Langfuse first, stores them with `PutSecretValue`, and restarts
   the functions so they read them. The by-hand equivalent is `aws secretsmanager
   put-secret-value --secret-id northwind-hr/langfuse` followed by `bash scripts/deploy-code.sh`.

   A running function reads the secret once, on its first traced question. Until then,
   or while the secret holds placeholders, tracing is off and the log says so once:
   `tracing: off: the Langfuse secret still holds placeholder keys`.
4. **Ask a question** in the app. The chat log names its trace:

   ```bash
   aws logs tail /aws/lambda/northwind-hr-chat --since 5m --format short | grep trace
   # trace 4f1c0e... outcome answered
   python pipeline/trace.py --from-secret 4f1c0e...      # or: make trace ID=latest
   ```

   The same tree is in the Langfuse UI under **Tracing**. Check that the salary in the
   answer shows as `[number]` there.

The off switch is `tracing_enabled = false` and an apply. No rebuild.

### What it costs a request

Measured on the real stack by the evaluate function, which asks the same question with
tracing off and on, alternately:

```bash
aws lambda invoke --function-name northwind-hr-evaluate --cli-read-timeout 600 \
  --cli-binary-format raw-in-base64-out --payload '{"tracing_overhead": {"runs": 10}}' overhead.json
cat overhead.json      # off and on: mean, p50, p90, and the difference
```

Measured on the real stack, 2026-10-07, 10 runs each of one policy question:

| | Tracing off | Tracing on | Difference |
|---|---|---|---|
| p50 | 2.30 s | 2.47 s | **+0.17 s** |
| p90 | 2.52 s | 2.88 s | +0.36 s |
| mean | 2.32 s | 2.72 s | +0.39 s |

Most of the difference is the flush: the function sends its spans from us-east-1 to
Langfuse's US region through the NAT gateway and waits for the reply before returning. The
mean sits above the median because the first traced question in a container also starts
the Langfuse client, about a second, once. Against the 29 second budget, p90 is about 1
percent. Ten runs is a small sample: read it as "about 0.2 seconds", not as three digits.

For comparison, locally, with AWS stubbed out and a stand-in Langfuse on the same machine,
tracing added about 4 ms at p50: the SDK's own work, with no network.

## The eval gate

[`.github/workflows/eval-gate.yml`](../.github/workflows/eval-gate.yml) runs on every pull
request. Anything that could change an answer is deployed to the stack and evaluated
before it can merge.

One chain, one job per stage, each starting when the one before it succeeds:

```
0. What changed → 1. Lint and unit tests → 2. Terraform plan → 3. Deploy code
  → 4. Re-index → 5. Warm up Aurora → 6. promptfoo evaluation → eval-gate
```

| Stage | What it does | AWS |
|---|---|---|
| 0. What changed | Does this pull request need the stack at all? Docs and the bootstrap alone do not | no |
| 1. Lint and unit tests | ruff, the guardrail and tracing tests, the pipeline's own tests | no |
| 2. Terraform plan | The plan, posted as a comment. Apply only when it has a configuration change | yes |
| 3. Deploy code | `scripts/build-function.sh`, `scripts/deploy-code.sh`, about 20 seconds | yes |
| 4. Re-index | Only when the index was built from other documents or indexing code | yes |
| 5. Warm up Aurora | Retries until the database answers | yes |
| 6. promptfoo evaluation | The golden set through promptfoo; its provider asks the evaluate function | yes |
| eval-gate | Compare with `baseline.json`, comment, pass or fail. The required check | no |

The whole workflow runs one pull request at a time (its concurrency group is
`northwind-hr-stack`), so another pull request's deploy can never land between this one's
deploy and its evaluation. Measured on the first full run: about 6 minutes, with the
re-index included. Each stage starts a fresh machine, which adds about half a minute per
stage.

### Stage 6: promptfoo

[`promptfoo/`](promptfoo) runs the golden set. promptfoo is on the GitHub runner, outside
the VPC, and Aurora has no route in from outside, so its provider does not call a model.
It asks the evaluate function, which answers inside the VPC exactly as the chat function
would and sends back the answer with its sources, tokens, latency, outcome and trace id.

| File | Role |
|---|---|
| `promptfooconfig.yaml` | The suite: prompt, provider, tests, assertion |
| `golden_tests.py` | One promptfoo test per case in `lambda/data/evaluation_questions.json`. The golden set stays in that one file |
| `provider.py` | Invokes the evaluate function with `{"ask": case}` |
| `checks.py` | The golden set's checks: `must_include`, `must_not_include`, the cited `source` |
| `to_eval.py` | promptfoo's results, rearranged into the run `eval_gate.py` reads |
| `run.sh` | All of the above. The gate and `make baseline` both call it |

promptfoo runs four cases at a time and decides each one. `eval_gate.py` then applies the
rules against the baseline and writes the comment, as before. promptfoo's own report,
`promptfoo-report.html`, is in the `eval-result` artifact: download it and open it in a
browser to page through every answer.

**The same check in two places.** The evaluate function still checks each answer itself,
and `to_eval.py` compares the two verdicts. If `checks.py` and `check()` in
`evaluate_handler.py` ever disagree, the run fails and names the case, rather than
trusting one silently. `pipeline/tests/test_promptfoo.py` holds them to the same answer on
every case.

**Where this differs from the deck.** The deck points promptfoo at a model through
LiteLLM. Here the model is reached through Bedrock Converse from inside the VPC, so the
provider is the evaluate function instead. What promptfoo tests is the whole assistant,
retrieval and access control included, not the model on its own.

`eval-gate` is the job to make a required status check. It fails whenever an evaluation
did not happen: lint failed, a stage failed, or the pull request came from a fork (forks
get no AWS access). It passes without evaluating only when the change is docs or
`pipeline/bootstrap` alone.

### The rules

| Rule | Why |
|---|---|
| Every `access_control` case passes | A leak is never a trade-off against a better average |
| Every `injection` case passes | Same |
| Pass rate at least the baseline's, minus 2 points | One case in 45 is 2.2 points, so in practice any case that newly fails must be balanced by one that newly passes |
| p90 latency under 20 seconds | The chat-slow alarm fires at 20. API Gateway gives up at 30 |
| Grounding, when the judge is calibrated | No answer judged 1 or 2 out of 5 against the passages it cited |

The comment lists every case that passed in the baseline and fails now, with the answer
the assistant gave and the fact the check found missing. It also names any setting that
differs from the baseline (`chat_model`, `min_best_similarity`) and, when the index was
rebuilt, the chunk count against the previous build.

### Three choices, and why

**Apply when the plan has a configuration change, not only when the pull request touches
`infra/`.** There is one stack. A pull request that raised a threshold and was then closed
leaves the stack on that threshold, and the next pull request would be evaluated against
it. Its plan shows the threshold going back, and applying that is what puts the stack in
step with the code under test. The function code hash, which differs on every build, does
not count as a change. See `plan_summary.py`.

**Re-index when the index differs from this checkout, not when this pull request changes
documents.** The same reason. `reindex.py` keeps a fingerprint of the documents and the
indexing code beside the Terraform state, and rebuilds when it does not match. After the
CSV demo branch has run, the next pull request rebuilds a correct index first.

**The pipeline cannot change the structure of the stack.** Its role
([`bootstrap/pipeline_role.tf`](bootstrap/pipeline_role.tf)) may change function settings,
the model permissions and the dependency layer. A pull request that adds a resource or
changes the network plans, then fails at apply with `AccessDenied`. Apply those from a
laptop.

## Setting it up

1. Deploy with `bash scripts/deploy.sh`. It runs the [`bootstrap`](bootstrap/README.md) too.
2. In GitHub, **Settings > Secrets and variables > Actions > Variables**, add:

   | Variable | Value |
   |---|---|
   | `AWS_ROLE_ARN` | `terraform -chdir=pipeline/bootstrap output -raw pipeline_role_arn` |
   | `AWS_REGION` | `us-east-1` |
   | `TF_STATE_BUCKET` | `terraform -chdir=pipeline/bootstrap output -raw state_bucket` |
   | `TF_VARS` | the whole of your `infra/terraform/terraform.tfvars` |
   | `ENABLE_JUDGE` | `true`, optional |

   They are variables, not secrets: none of them grants anything without a token from a
   workflow in your repository. `TF_VARS` must stay the same as your laptop's
   `terraform.tfvars`. If the two differ, each apply undoes the other's.
3. Record the first baseline, from `main`, with the stack running `main`:

   ```bash
   bash scripts/build-function.sh && bash scripts/deploy-code.sh
   make baseline NOTE="first baseline"
   # without make: bash pipeline/promptfoo/run.sh eval.json
   #               python pipeline/make_baseline.py --from eval.json --note "first baseline"
   git switch -c baseline/first && git add pipeline/baseline.json
   git commit -m "Record the first eval baseline" && git push -u origin baseline/first
   ```

   This needs Node 20 or newer for promptfoo, which `npx` fetches on first use. Open a
   pull request for it. Until a baseline is merged, every gate run fails on the pass-rate
   rule and says so.
4. Make the check required: **Settings > Branches > Add branch protection rule** (or
   **Rules > Rulesets**) for `main`, **Require status checks to pass**, and add
   `eval-gate`. It appears in the list after the workflow has run once.

## Updating the baseline

Only on purpose, and only in a pull request of its own. `make baseline` runs the golden
set through promptfoo, the same runner the gate uses, and writes `pipeline/baseline.json`.
It does nothing else: no commit, no push.
It refuses a run with any `access_control` or `injection` failure. The pull request then
goes through the gate against the new file, and the reviewer sees what moved.

`FROM=eval.json` uses a saved run instead, for example the `eval-result` artifact of a
gate run on the commit you want to measure from.

## The grounding judge

`judge.py` asks Amazon Nova Lite, through Converse, to score each cited answer from 1 to 5
against the passages it cited. It runs when `ENABLE_JUDGE` is `true`, and it gates only
after it has been measured:

```bash
make calibrate-judge      # or: python pipeline/judge_calibrate.py
```

That scores the 40 human-labelled answers in `judge_calibration.json` (20 good, 20 bad)
and writes `judge_calibration_report.json`. Commit the report. The gate lets the judge
block only when agreement is at least 85 percent and the model, the prompt and the labelled
examples are the ones that were measured. Edit any of them and the judge drops back to
advisory until it is calibrated again.

## Demo branches

```bash
make demo-branches        # or: bash pipeline/demo/make-demo-branches.sh
```

Creates four one-commit branches from `main`, each a change that reads as harmless in
review:

| Branch | Change | What the gate should show |
|---|---|---|
| `demo/drop-citations` | The prompt no longer asks for a "Sources:" line | Cited answers become "isn't covered", because the code keeps only passages the answer names |
| `demo/raise-similarity-floor` | `min_best_similarity` 0.25 to 0.45 | Real questions scoring 0.36 to 0.45 fall below the floor |
| `demo/swap-to-lite` | The chat model becomes Nova Lite | Wrong tool calls and invented citations, as measured in `docs/llmops-notes.md` |
| `demo/break-csv-rows` | The CSV reader returns one section per file | The `table_row` cases, and a smaller chunk count |

Push them and open a pull request for each. They share the stack, so they run one after
another. Close them unmerged; the next run puts the stack back.

The second and third change a Terraform default. If your `TF_VARS` sets
`min_best_similarity` or `chat_model_id` explicitly, the default is ignored and the branch
changes nothing.

## Drift (Stop 10)

The golden set describes the questions someone thought of. Production is the questions
people actually ask. [`nightly-drift.yml`](../.github/workflows/nightly-drift.yml) checks
every night whether the two are drifting apart, from Langfuse alone: it needs no AWS
access and never touches the stack.

It compares the last 24 hours of chat traces (evaluation traffic left out) with the 7 days
before, using [`drift.py`](drift.py):

| Signal | What a move usually means |
|---|---|
| Response length (output tokens) | A prompt or model change, or questions of a new kind |
| No-evidence rate | People asking about things the documents do not cover |
| Refused rate | People asking for records they may not see, or a broken lookup |
| Mean best similarity | Questions further from the documents than they used to be |
| Fallback rate | The primary model is throttled or failing |
| p90 latency | Slower search, a slower model, or the database waking more often |
| Topic mix (top source title), as PSI | What people ask about has changed |

A numeric signal alerts when it moves more than 2 standard deviations: of the daily values
when there are at least 3 days of history, otherwise of the same statistic over samples of
reference traffic. The topic mix alerts when the population stability index is above 0.2
and also above what chance alone reaches at that amount of traffic. With ten topics and
forty questions, noise reaches 0.2 by itself, which is why the second condition exists.
Fewer than 20 questions in either window and nothing is judged.

On an alert it posts to the webhook in `DRIFT_WEBHOOK_URL` and the run goes red. Every run
keeps an Evidently report (`drift-report.html`) of the same two windows as an artifact,
with per-column distributions and drift tests.

### The review queue, and closing the loop

Traces with no evidence, or a best similarity below 0.35, are added to the Langfuse
annotation queue `drift-review` (created on the first run, with a boolean score
`promote_to_golden_set`). In Langfuse, **Annotation Queues > drift-review**, read each one.
For a question the assistant should be tested on from now on, set the score to true and
note what a right answer contains. Then:

```bash
python pipeline/promote_case.py <trace-id> --from-secret \
  --must-include "60 days" --source "Remote and Hybrid Work Policy" --tag remote_work --try
```

That checks the trace was marked, maps the employee id back to the demo user's email (from
the seed data), asks the stack the question once (`--try`) so you know whether it passes
today, and commits the case to `lambda/data/evaluation_questions.json` on a branch
`golden/<case id>`. Push it and open a pull request. The eval gate runs the new case with
the others and lists it under "Not in the baseline". Once merged, record a new baseline
(its own pull request, as always) so the case counts from then on.

A question that was masked in the trace (it held a long number or an email) cannot be
replayed as it was. Pass the original wording with `--question`.

### The drift replay

```bash
pip install -r pipeline/requirements-drift.txt
# The demo password is read from `terraform output demo_password`. If you set the
# passwords by hand instead (setup_application = false), export DEMO_PASSWORD first.
python pipeline/simulate_drift.py
```

It signs three demo users in through Cognito, as the web app does, and asks 48 ordinary
questions: that is the reference. Then a burst of 30 remote-work questions mixed with 10
ordinary ones, as if a new remote working rule had just been announced. It prints the time
between the two, and the command to compare them:

```bash
gh workflow run nightly-drift.yml -f current_since=2026-10-06T14:05:00Z
```

The topic mix alert fires: "Remote and Hybrid Work Policy" goes from a few percent of
questions to most of them. The golden set has one remote-work case, so the burst is also
the gap the review queue is for. About 90 questions, 8 minutes, a few cents of Bedrock.

### Setting it up

In **Settings > Secrets and variables > Actions**:

| Name | Kind | Value |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | secret | the same keys as in `northwind-hr/langfuse` |
| `LANGFUSE_SECRET_KEY` | secret | |
| `DRIFT_WEBHOOK_URL` | secret, optional | a Slack incoming webhook, or a Google Chat space webhook. Both take the same message |
| `LANGFUSE_HOST` | variable, optional | default `https://us.cloud.langfuse.com` |

The Langfuse keys are secrets here, unlike the pipeline's other settings: they read every
trace. Scheduled runs only run on the default branch, and pull requests from forks never
see them.

## Running the pieces by hand

With your own AWS credentials, from the repository root:

```bash
python pipeline/warm_up.py
bash pipeline/promptfoo/run.sh eval.json       # or: make eval
npx promptfoo@0.124.0 view                     # promptfoo's web viewer, on the last run
python pipeline/eval_gate.py --run eval.json --out comment.md
python pipeline/reindex.py check --state-bucket "$(terraform -chdir=pipeline/bootstrap output -raw state_bucket)"
make test                 # no AWS
```

## Things to know

- **What the comments show.** The plan comment includes ARNs, so your account id. The gate
  comment includes answers, which can include salaries from the demo data. On a public
  repository both are public. The `eval-result` artifact holds the same, for 14 days.
- **The web app is not the pipeline's to publish.** `app_setup.tf` rebuilds the React app
  when `frontend/` changes, which needs npm and permissions the pipeline role does not
  have. A pull request that changes `frontend/` alone does not start the stack job; one that
  changes `frontend/` together with `lambda/` or `infra/` fails at apply. Publish front-end
  changes with `bash scripts/deploy.sh` from a laptop. Changes to `lambda/db/*.sql` are fine:
  the apply re-runs setup-db, which the role may invoke.
- **One run at a time.** The stack job uses the concurrency group `northwind-hr-stack`.
  GitHub keeps one run waiting behind the active one; a newer run replaces the waiting one,
  which then shows as cancelled. Re-run it.
- **The layer.** Terraform hashes `build/layer.zip`, and a rebuilt zip never has the same
  hash. So the pipeline downloads the layer the functions already run, and builds a new one
  only when the pull request changes `lambda/requirements.txt`.
