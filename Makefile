SHELL := /bin/bash

PYTHON ?= python3
VENV := .venv
VENV_BIN := $(VENV)/bin
APP := app.main:app
DEPS_STAMP := $(VENV)/.deps-installed

.PHONY: help venv install env run run-prod lint test check format health overview schedule session clean-cache

help:
	@echo "Targets:"
	@echo "  make run         - start lap-vision-f1 in reload mode"
	@echo "  make run-prod    - start lap-vision-f1 without reload"
	@echo "  make lint        - run ruff checks"
	@echo "  make test        - run smoke tests"
	@echo "  make check       - run lint and tests"
	@echo "  make format      - format and auto-fix imports"
	@echo "  make health      - call /healthz"
	@echo "  make overview    - call /v1/overview"
	@echo "  make schedule    - call /v1/seasons/\$$YEAR/schedule (default YEAR=2025)"
	@echo "  make session     - call /v1/sessions/load"

$(VENV_BIN)/python:
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)

$(DEPS_STAMP): pyproject.toml | $(VENV_BIN)/python
	@$(VENV_BIN)/python -m pip install --upgrade pip >/dev/null
	@$(VENV_BIN)/pip install -e '.[dev]'
	@touch $(DEPS_STAMP)

venv: $(VENV_BIN)/python

install: $(DEPS_STAMP)

env:
	@test -f .env.local || cp .env.example .env.local

run: install env
	@HOST=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().host)"); \
	PORT=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().port)"); \
	echo "lap-vision-f1 running on http://$$HOST:$$PORT"; \
	$(VENV_BIN)/uvicorn $(APP) --host "$$HOST" --port "$$PORT" --reload

run-prod: install env
	@HOST=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().host)"); \
	PORT=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().port)"); \
	echo "lap-vision-f1 running on http://$$HOST:$$PORT"; \
	$(VENV_BIN)/uvicorn $(APP) --host "$$HOST" --port "$$PORT"

lint: install
	@$(VENV_BIN)/ruff check app tests

test: install
	@$(VENV_BIN)/python -m unittest discover -s tests -p 'test_*.py'

check: lint test

format: install
	@$(VENV_BIN)/ruff format app tests
	@$(VENV_BIN)/ruff check app tests --fix

health: install env
	@PORT=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().port)"); \
	curl -fsS "http://127.0.0.1:$$PORT/healthz"

overview: install env
	@PORT=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().port)"); \
	TOKEN=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().internal_token)"); \
	curl -fsS -H "X-Internal-Token: $$TOKEN" "http://127.0.0.1:$$PORT/v1/overview"

schedule: install env
	@YEAR=$${YEAR:-2025}; \
	PORT=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().port)"); \
	TOKEN=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().internal_token)"); \
	curl -fsS -H "X-Internal-Token: $$TOKEN" "http://127.0.0.1:$$PORT/v1/seasons/$$YEAR/schedule"

session: install env
	@YEAR=$${YEAR:-2025}; \
	EVENT="$${EVENT:-Australia}"; \
	SESSION="$${SESSION:-R}"; \
	PORT=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().port)"); \
	TOKEN=$$($(VENV_BIN)/python -c "from app.config import load_settings; print(load_settings().internal_token)"); \
	curl -fsS -X POST \
		-H "Content-Type: application/json" \
		-H "X-Internal-Token: $$TOKEN" \
		"http://127.0.0.1:$$PORT/v1/sessions/load" \
		-d "{\"year\":$$YEAR,\"event\":\"$$EVENT\",\"session\":\"$$SESSION\"}"

clean-cache:
	@rm -rf var/fastf1-cache var/data-cache
