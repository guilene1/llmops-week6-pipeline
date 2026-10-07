# Shortcuts for the pipeline. Windows: run these from Git Bash with make installed, or run
# the command each target shows directly.

PYTHON ?= python

.PHONY: help up down eval baseline calibrate-judge test lint trace demo-branches drift replay

help:
	@echo "make up               deploy everything, ready to use (scripts/deploy.sh)"
	@echo "make down             destroy the stack (scripts/deploy.sh destroy)"
	@echo "make eval            run the golden set through promptfoo, write eval.json"
	@echo "make baseline         run it and write pipeline/baseline.json"
	@echo "                      FROM=eval.json to use a saved run, NOTE=\"why\" to record a reason"
	@echo "make calibrate-judge  measure the grounding judge against human labels"
	@echo "make test             the pipeline's tests, the guardrail and tracing tests, no AWS"
	@echo "make trace ID=<id>    print a trace's span tree (or ID=latest)"
	@echo "make lint             ruff, as the pipeline runs it"
	@echo "make demo-branches    create the four demo branches from main"
	@echo "make drift            the nightly drift check, now (SINCE=<ISO time> to set the window)"
	@echo "make replay           the drift replay: reference traffic, then a remote-work burst"

up:
	PYTHON=$(PYTHON) bash scripts/deploy.sh

down:
	bash scripts/deploy.sh destroy

eval:
	PYTHON=$(PYTHON) bash pipeline/promptfoo/run.sh eval.json

# Writes the file and nothing else. Commit it in a pull request of its own.
baseline:
	$(if $(FROM),,PYTHON=$(PYTHON) bash pipeline/promptfoo/run.sh eval.json)
	$(PYTHON) pipeline/make_baseline.py --from $(or $(FROM),eval.json) --note "$(NOTE)"

calibrate-judge:
	$(PYTHON) pipeline/judge_calibrate.py

test:
	$(PYTHON) -m pytest -q pipeline/tests
	cd lambda && $(PYTHON) -m pytest -q tests/test_guardrails.py tests/test_tracing.py

lint:
	ruff check lambda pipeline

trace:
	$(PYTHON) pipeline/trace.py --from-secret $(if $(filter latest,$(ID)),--latest,$(ID))

demo-branches:
	bash pipeline/demo/make-demo-branches.sh

drift:
	$(PYTHON) pipeline/drift.py --from-secret --no-queue $(if $(SINCE),--current-since $(SINCE))

replay:
	$(PYTHON) pipeline/simulate_drift.py
