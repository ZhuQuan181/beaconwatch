.PHONY: help up down logs install test test-wire e2e lint clean

SHELL := /bin/bash

# Compose file lives under server/ (it builds the server image and the
# infra it needs).  The Makefile used to call `docker compose` with no
# -f, which silently resolves to $PWD/docker-compose.yml — a file that
# doesn't exist at the repo root, so `make up` failed immediately.
COMPOSE := docker compose -f server/docker-compose.yml

help:
	@echo "RID Detection System"
	@echo "  make up     - start full stack (TimescaleDB + EMQX + Redis + server)"
	@echo "  make down   - stop"
	@echo "  make logs   - tail logs"
	@echo "  make test   - unit tests (edge + server) + wire contract"
	@echo "  make e2e    - real end-to-end: edge -> MQTT -> server -> DB -> HTTP"
	@echo "  make lint   - linters"
	@echo "  make clean  - remove containers + caches"

up:
	$(COMPOSE) up -d --build
	@echo "Waiting for the API to answer..."
	@for i in $$(seq 1 30); do \
		curl -sf http://localhost:8000/health >/dev/null && break; \
		sleep 1; \
	done
	@curl -sf http://localhost:8000/health >/dev/null \
		&& echo "API:       http://localhost:8000/docs" \
		|| (echo "API did not come up — check 'make logs'"; exit 1)
	@echo "Dashboard: http://localhost:5173  (cd frontend && npm run dev)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

install:
	cd edge && uv pip install -e ".[dev]"
	cd ../server && uv pip install -e ".[dev]"
	cd ../frontend && npm install

test:
	cd edge && .venv/bin/python -m pytest -q
	cd server && .venv/bin/python -m pytest -q
	$(MAKE) test-wire

# Cross-package contract: does the edge's real output parse at the
# server?  Kept separate so a contract break is obvious in CI output
# instead of being one line among 66.
test-wire:
	server/.venv/bin/python -m pytest tests/ -q

# Real chain, no mocks.  Requires the infra from `make up` (or any
# reachable broker + Postgres).  Override hosts via env vars:
#   RID_DB_PORT=5433 RID_MQTT_HOST=127.0.0.1 make e2e
e2e:
	@test -x server/.venv/bin/python || { echo "run 'make install' first"; exit 1; }
	RID_DB_HOST=$${RID_DB_HOST:-127.0.0.1} \
	RID_DB_PORT=$${RID_DB_PORT:-5433} \
	RID_MQTT_HOST=$${RID_MQTT_HOST:-127.0.0.1} \
	server/.venv/bin/python scripts/e2e_chain.py

lint:
	cd edge && .venv/bin/python -m ruff check . 2>/dev/null || true
	cd ../server && .venv/bin/python -m ruff check . 2>/dev/null || true
	cd ../frontend && npm run lint

clean:
	$(COMPOSE) down -v
	rm -rf edge/.venv server/.venv frontend/node_modules
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
