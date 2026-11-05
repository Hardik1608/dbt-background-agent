# Linux/macOS/WSL convenience targets. On Windows run the same `python -m dbt_agent ...`.
PY      ?= .venv/bin/python
CONFIG  ?= configs/local_agent.yaml configs/box.yaml

.DEFAULT_GOAL := help
.PHONY: help venv install lint format test test-docker image fixture validate oracle bench \
        spider2 adebench smoke clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv (Python 3.11)
	python3.11 -m venv .venv

install: ## Install pinned dependencies and the package (editable)
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pip install -e . --no-deps

lint: ## ruff check + format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format: ## Auto-format
	$(PY) -m ruff format . && $(PY) -m ruff check . --fix

test: ## Unit + dbt integration tests (no Docker, GPU, network or API keys)
	$(PY) -m pytest

test-docker: image ## DockerSandbox isolation tests (Linux + Docker)
	$(PY) -m pytest -m docker tests/docker

image: ## Build the sandbox image
	docker build -f docker/sandbox.Dockerfile -t dbt-agent-sandbox:0.1 .

fixture: ## Build the sales-warehouse base fixture (needs network once for dbt deps)
	$(PY) -m dbt_agent fixture build

validate: fixture ## Validate the 50 tasks: fail before the fix, pass with the reference
	$(PY) -m dbt_agent suite validate --config configs/box.yaml --report reports/suite_validation

oracle: fixture ## The whole pipeline with the scripted oracle agent (expect 100%)
	$(PY) -m dbt_agent bench sales --llm oracle --reviewer none --config configs/box.yaml \
	  --run-name oracle --report reports/sales_oracle

bench: fixture ## The suite with the configured model (export the key named in the config)
	$(PY) -m dbt_agent bench sales --config $(CONFIG) configs/sales_suite.yaml \
	  --run-name local_agent --report reports/sales_local_agent_k1

spider2: ## Spider 2.0-DBT: setup, run, official evaluation
	$(PY) -m dbt_agent bench spider2 setup
	$(PY) -m dbt_agent bench spider2 run --config $(CONFIG) configs/spider2.yaml \
	  --run-name spider2_local_agent
	$(PY) -m dbt_agent bench spider2 evaluate --run-name spider2_local_agent \
	  --report reports/spider2_dbt

adebench: ## ADE-bench through its own harness
	$(PY) -m dbt_agent bench adebench setup
	$(PY) -m dbt_agent bench adebench run --config $(CONFIG) configs/adebench.yaml \
	  --report reports/adebench

smoke: fixture ## CI smoke: two tasks end to end with the oracle in a local sandbox
	$(PY) -m dbt_agent bench sales --llm oracle --reviewer none --sandbox local \
	  --tasks B19 F07 --run-name ci_smoke --report runs/ci_smoke

clean: ## Remove run state and generated data (keeps code and reports)
	rm -rf runs bench_data fixtures/build
