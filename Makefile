.PHONY: help up down logs test e2e lint clean install

help:
	@echo "RID Detection System"
	@echo "  make up     - start full stack"
	@echo "  make down   - stop"
	@echo "  make logs   - tail logs"
	@echo "  make test   - unit tests"
	@echo "  make e2e    - end-to-end test"
	@echo "  make lint   - linters"
	@echo "  make clean  - remove containers + caches"

up:
	docker compose up -d --build
	@echo "Dashboard: http://localhost:5173"

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

install:
	cd edge && pip install -e ".[dev]"
	cd ../server && pip install -e ".[dev]"
	cd ../frontend && npm install

test:
	cd edge && pytest -q
	cd ../server && pytest -q
	cd ../frontend && npm run test:run

e2e:
	docker compose up -d --build
	sleep 10
	cd tests/e2e && python simulate_drone.py
	@echo "Open http://localhost:5173"

lint:
	cd edge && ruff check . && mypy .
	cd ../server && ruff check . && mypy .
	cd ../frontend && npm run lint

clean:
	docker compose down -v
	rm -rf edge/.venv server/.venv frontend/node_modules
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
