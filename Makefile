SHELL := /bin/bash

PYTHON ?= python3
VENV := .venv
VENV_BIN := $(VENV)/bin
APP := app.main:app
DEPS_STAMP := $(VENV)/.deps-installed

TUNNEL_USER ?= ubuntu
TUNNEL_HOST ?= 95.179.154.60
TUNNEL_PORT ?= 22
SOCKS_PORT ?= 1080
MICROSOCKS_PID_FILE := /tmp/lap-vision-f1-microsocks.pid

.PHONY: help venv install env run run-prod lint test check format health overview schedule session clean-cache tunnel

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
	@echo "  make tunnel      - run a local SOCKS5 proxy (microsocks) and reverse-tunnel it"
	@echo "                     to \$$TUNNEL_USER@\$$TUNNEL_HOST:\$$SOCKS_PORT over SSH."
	@echo "                     deploy/deploy_f1.sh already opens this port in ufw for the"
	@echo "                     docker network (PROXY_TUNNEL_PORT, default 1080 - keep it in"
	@echo "                     sync with SOCKS_PORT). Keep this running, then on the server"
	@echo "                     point a relay (e.g. socat) from the docker bridge IP to"
	@echo "                     127.0.0.1:\$$SOCKS_PORT, and set that address via PUT /v1/admin/proxy."
	@echo "                     Vars: TUNNEL_USER, TUNNEL_HOST, TUNNEL_PORT, SOCKS_PORT"

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

tunnel:
	@command -v microsocks >/dev/null 2>&1 || { \
		echo "microsocks not found. Install it with: brew install microsocks"; exit 1; \
	}
	@pkill -f "microsocks -i 127.0.0.1 -p $(SOCKS_PORT)" >/dev/null 2>&1 || true
	@echo "Starting local SOCKS5 proxy on 127.0.0.1:$(SOCKS_PORT) ..."; \
	microsocks -i 127.0.0.1 -p $(SOCKS_PORT) & \
	echo $$! > $(MICROSOCKS_PID_FILE); \
	trap 'kill $$(cat $(MICROSOCKS_PID_FILE)) 2>/dev/null; rm -f $(MICROSOCKS_PID_FILE)' EXIT INT TERM; \
	sleep 1; \
	echo "Reverse-tunneling it to $(TUNNEL_USER)@$(TUNNEL_HOST):$(SOCKS_PORT) (Ctrl+C to stop) ..."; \
	ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
		-R 127.0.0.1:$(SOCKS_PORT):localhost:$(SOCKS_PORT) \
		-p $(TUNNEL_PORT) -N $(TUNNEL_USER)@$(TUNNEL_HOST)
