POETRY := $(shell command -v poetry 2>/dev/null || echo $(HOME)/.local/bin/poetry)

HOST ?= 0.0.0.0
PORT ?= 8000

# scripts/fetch_movies.py — оверрайдь через `make fetch-movies LIMIT=500
# CONCURRENCY=4` и т.п. MIN_VC_* / MIN_VA применяются только к -known
# вариантам (--source=most_voted).
LIMIT ?= 1000
FRAMES ?= 5
CONCURRENCY ?= 8
MIN_VC_MOVIE ?= 1000
MIN_VC_SHOW ?= 200
MIN_VA ?= 6

.PHONY: help install update lock run dev test coverage clean \
        service-install service-uninstall service-restart service-status service-logs \
        lint format typecheck check fix \
        fetch-movies fetch-shows fetch-movies-known fetch-shows-known fetch-all

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
	@echo ""
	@echo "Quiz data ingestion (TMDB → data/{movies,shows}.sqlite3):"
	@echo "  fetch-movies         - top_rated фильмы (классика по оценке)"
	@echo "  fetch-shows          - top_rated сериалы (без аниме)"
	@echo "  fetch-movies-known   - most_voted фильмы (узнаваемое; vc>=$(MIN_VC_MOVIE), va>=$(MIN_VA))"
	@echo "  fetch-shows-known    - most_voted сериалы (vc>=$(MIN_VC_SHOW), va>=$(MIN_VA), без аниме)"
	@echo "  fetch-all            - movies + shows последовательно (top_rated)"
	@echo "  Override: LIMIT=N FRAMES=N CONCURRENCY=N MIN_VC_MOVIE=N MIN_VC_SHOW=N MIN_VA=N"

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

fetch-movies:
	$(POETRY) run python scripts/fetch_movies.py --kind movie \
		--limit $(LIMIT) --frames-per-movie $(FRAMES) --concurrency $(CONCURRENCY)

fetch-shows:
	$(POETRY) run python scripts/fetch_movies.py --kind tv \
		--limit $(LIMIT) --frames-per-movie $(FRAMES) --concurrency $(CONCURRENCY)

fetch-movies-known:
	$(POETRY) run python scripts/fetch_movies.py --kind movie \
		--source most_voted \
		--min-vote-count $(MIN_VC_MOVIE) --min-vote-average $(MIN_VA) \
		--limit $(LIMIT) --frames-per-movie $(FRAMES) --concurrency $(CONCURRENCY)

fetch-shows-known:
	$(POETRY) run python scripts/fetch_movies.py --kind tv \
		--source most_voted \
		--min-vote-count $(MIN_VC_SHOW) --min-vote-average $(MIN_VA) \
		--limit $(LIMIT) --frames-per-movie $(FRAMES) --concurrency $(CONCURRENCY)

fetch-all: fetch-movies fetch-shows
