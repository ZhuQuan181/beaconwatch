.PHONY: help up down logs install test test-wire e2e e2e-multi lint clean reset

SHELL := /bin/bash
VENV ?= .venv
PYTHON ?= $(abspath $(VENV)/bin/python)
SRC_PATH := $(CURDIR)/edge/src:$(CURDIR)/server/src

# Compose file lives under server/ (it builds the server image and the
# infra it needs).  The Makefile used to call `docker compose` with no
# -f, which silently resolves to $PWD/docker-compose.yml — a file that
# doesn't exist at the repo root, so `make up` failed immediately.
COMPOSE := docker compose -f server/docker-compose.yml

help:
	@echo "BeaconWatch — real-time Remote ID monitoring"
	@echo "  make up     - start stack (TimescaleDB + EMQX + Redis + server)"
	@echo "  make down   - stop"
	@echo "  make logs   - tail logs"
	@echo "  make install- create .venv and install Python/frontend deps"
	@echo "  make test   - unit tests (edge + server) + wire contract"
	@echo "  make e2e    - real end-to-end: edge -> MQTT -> server -> DB -> HTTP"
	@echo "  make e2e-multi - two API replicas + Redis + MQTT shared subscription"
	@echo "  make lint   - linters"
	@echo "  make clean  - remove local caches/artifacts (keeps database data)"
	@echo "  make reset  - DESTRUCTIVE: stop stack and delete its database volume"

up:
	$(COMPOSE) up -d --build
	@echo "Waiting for the API and database to become ready..."
	@for i in $$(seq 1 30); do \
		curl -sf http://localhost:8000/health/ready >/dev/null && break; \
		sleep 1; \
	done
	@curl -sf http://localhost:8000/health/ready >/dev/null \
		&& echo "API:       http://localhost:8000/docs" \
		|| (echo "API did not come up — check 'make logs'"; exit 1)
	@echo "Dashboard: http://localhost:5173  (cd frontend && npm run dev)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

install:
	uv venv $(VENV)
	uv pip install --python $(PYTHON) -e "./edge[dev]" -e "./server[dev]"
	cd frontend && npm ci

test:
	cd edge && PYTHONPATH=$(SRC_PATH) $(PYTHON) -m pytest -q
	cd server && PYTHONPATH=$(SRC_PATH) $(PYTHON) -m pytest -q
	PYTHONPATH=$(SRC_PATH) $(PYTHON) -m pytest -q tests

# Cross-package contract: does the edge's real output parse at the
# server?  Kept separate so a contract break is obvious in CI output
# instead of being one line among the package suites.
test-wire:
	PYTHONPATH=$(SRC_PATH) $(PYTHON) -m pytest -q tests

# Real chain, no mocks.  Requires the infra from `make up` (or any
# reachable broker + Postgres).  Override hosts via env vars:
#   RID_DB_PORT=5433 RID_MQTT_HOST=127.0.0.1 make e2e
e2e:
	@test -x $(PYTHON) || { echo "run 'make install' first"; exit 1; }
	RID_DB_HOST=$${RID_DB_HOST:-127.0.0.1} \
	RID_DB_PORT=$${RID_DB_PORT:-5433} \
	RID_MQTT_HOST=$${RID_MQTT_HOST:-127.0.0.1} \
	PYTHONPATH=$(SRC_PATH):edge/tests $(PYTHON) scripts/e2e_chain.py

e2e-multi:
	@test -x $(PYTHON) || { echo "run 'make install' first"; exit 1; }
	RID_DB_HOST=$${RID_DB_HOST:-127.0.0.1} \
	RID_DB_PORT=$${RID_DB_PORT:-5433} \
	RID_MQTT_HOST=$${RID_MQTT_HOST:-127.0.0.1} \
	PYTHONPATH=$(SRC_PATH) $(PYTHON) scripts/e2e_multireplica.py

lint:
	$(PYTHON) -m ruff check edge server tests scripts
	cd frontend && npm run typecheck

clean:
	rm -rf frontend/dist frontend/tsconfig.tsbuildinfo
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true

reset:
	@read -p "Delete the BeaconWatch database volume? [y/N] " ok; \
	test "$$ok" = "y" || { echo "cancelled"; exit 1; }
	$(COMPOSE) down -v
