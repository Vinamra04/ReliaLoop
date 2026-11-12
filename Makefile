# Thin convenience wrapper. The canonical interface is `python -m tools.cli`,
# which works without make (e.g. native Windows). Targets are added as the
# commands they wrap are built, so every target listed here works.

PY ?= python
CLI := $(PY) -m tools.cli

.DEFAULT_GOAL := help
.PHONY: help init setup lint format typecheck test build up down ps logs traffic chaos clear-chaos dashboards
.PHONY: gen-rules validate test-rules verify-pipeline silence

gen-rules: ## Regenerate Prometheus rules, SLO schema and docs/slo-design.md from slo/*.yaml
	$(CLI) slo generate

validate: ## Validate SLO specs, generated files and Prometheus config (promtool)
	$(CLI) slo validate

test-rules: ## Run promtool rule tests and amtool config and routing tests
	$(CLI) test-rules

verify-pipeline: ## Check the live alerting pipeline end to end
	$(CLI) verify-pipeline

silence: ## Silence a service: make silence SERVICE=orders MINUTES=15
	$(CLI) silence --service $(SERVICE) --minutes $(or $(MINUTES),15)

help: ## Show documented targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

init: ## Create .env, service tokens, webhook credentials file and runtime dirs
	$(CLI) init

setup: ## Install dev dependencies and git hooks into the active environment
	$(PY) -m pip install -r requirements-dev.txt
	$(PY) -m pre_commit install

lint: ## Ruff lint + format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format: ## Auto-format and apply safe lint fixes
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck: ## mypy --strict on agent/ and tools/
	$(PY) -m mypy

test: ## Run the Python unit tests
	$(PY) -m pytest

build: ## Build all application images (including the simulated bad orders releases)
	$(CLI) build

up: ## Start the stack and wait until healthy
	$(CLI) up

down: ## Stop the stack (add VOLUMES=1 to delete this project's data)
	$(CLI) down $(if $(VOLUMES),--volumes,)

ps: ## Show the stack's containers
	$(CLI) ps

logs: ## Show logs (SERVICE=orders to filter)
	$(CLI) logs $(SERVICE)

traffic: ## Set load: make traffic RPS=150 [DURATION=60]
	$(CLI) traffic --rps $(RPS) $(if $(DURATION),--duration $(DURATION),)

chaos: ## Inject a fault: make chaos FAULT=errors [SERVICE=orders] [DURATION=120]
	$(CLI) chaos $(FAULT) $(if $(SERVICE),--service $(SERVICE),) $(if $(DURATION),--duration $(DURATION),)

clear-chaos: ## Clear all injected faults
	$(CLI) chaos-clear

dashboards: ## Regenerate Grafana dashboard JSON from tools/dashboards
	$(CLI) dashboards generate
