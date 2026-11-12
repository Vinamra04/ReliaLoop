# Thin convenience wrapper. The canonical interface is `python -m tools.cli`,
# which works without make (e.g. native Windows). Targets are added as the
# commands they wrap are built, so every target listed here works.

PY ?= python
CLI := $(PY) -m tools.cli

.DEFAULT_GOAL := help
.PHONY: help init setup lint format typecheck test

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
