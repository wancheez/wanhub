POETRY := $(shell command -v poetry 2>/dev/null || echo $(HOME)/.local/bin/poetry)

HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: help install update lock run dev test coverage clean \
        service-install service-uninstall service-restart service-status service-logs \
        lint format typecheck check fix

help:
	@echo "Available targets:"
	@echo "  install            - install dependencies (runtime + dev) via poetry"
	@echo "  update             - update dependencies within version constraints"
	@echo "  lock               - regenerate poetry.lock"
	@echo "  run                - run server (no reload)"
	@echo "  dev                - run server with auto-reload"
	@echo "  test               - run pytest"
	@echo "  coverage           - run pytest with coverage report (term + htmlcov/)"
	@echo "  clean              - remove caches and __pycache__"
	@echo ""
	@echo "Quality:"
	@echo "  lint               - ruff check (lint + import sort)"
	@echo "  format             - ruff format (apply)"
	@echo "  fix                - ruff check --fix + ruff format (auto-fix everything)"
	@echo "  typecheck          - mypy on app/"
	@echo "  check              - lint + typecheck + tests (pre-commit gate)"
	@echo ""
	@echo "Service (systemd):"
	@echo "  service-install    - install/enable/start as systemd unit (sudo)"
	@echo "  service-uninstall  - remove the systemd unit (sudo)"
	@echo "  service-restart    - sudo systemctl restart wanhub"
	@echo "  service-status     - systemctl status wanhub"
	@echo "  service-logs       - tail journal logs (Ctrl-C to exit)"

install:
	$(POETRY) install

update:
	$(POETRY) update

lock:
	$(POETRY) lock

run:
	$(POETRY) run uvicorn app.main:app --host $(HOST) --port $(PORT)

dev:
	$(POETRY) run uvicorn app.main:app --host $(HOST) --port $(PORT) --reload

test:
	$(POETRY) run pytest tests/ -v

coverage:
	$(POETRY) run pytest tests/ --cov=app --cov-branch --cov-report=term --cov-report=html

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov

lint:
	$(POETRY) run ruff check app/ tests/
	$(POETRY) run ruff format --check app/ tests/

format:
	$(POETRY) run ruff format app/ tests/

fix:
	$(POETRY) run ruff check --fix app/ tests/
	$(POETRY) run ruff format app/ tests/

typecheck:
	$(POETRY) run mypy app/

check: lint typecheck test

service-install:
	@./scripts/install-service.sh

service-uninstall:
	@./scripts/install-service.sh --uninstall

service-restart:
	sudo systemctl restart wanhub

service-status:
	systemctl status wanhub --no-pager -l

service-logs:
	journalctl -u wanhub -f -n 50
