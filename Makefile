# TibDaftari Backend — common developer targets.
# Windows: use Git Bash / WSL, or run the underlying commands directly (see README).

PY ?= python
PORT ?= 8000

.PHONY: help venv install dev run migrate revision seed country-root superadmin partitions test lint format

help:
	@echo "make install    - create .venv and install dev requirements"
	@echo "make dev        - uvicorn with auto-reload on PORT ($(PORT))"
	@echo "make run        - production entrypoint (migrate + serve)"
	@echo "make migrate    - alembic upgrade head"
	@echo "make revision m=msg - alembic revision --autogenerate"
	@echo "make seed       - geo reference data (countries / regions / districts)"
	@echo "make country-root COUNTRY=UZ NAME='O‘zbekiston Respublikasi' - create the country node of the org tree"
	@echo "make superadmin LOGIN= PASSWORD= NAME= - create the platform superadmin"
	@echo "make partitions - ensure audit_log partitions"
	@echo "make test       - pytest"
	@echo "make lint       - ruff check"

venv:
	$(PY) -m venv .venv

install: venv
	.venv/Scripts/python -m pip install -U pip || .venv/bin/python -m pip install -U pip
	.venv/Scripts/python -m pip install -r requirements-dev.txt || .venv/bin/python -m pip install -r requirements-dev.txt

dev:
	$(PY) -m uvicorn app.main:app --reload --port $(PORT)

run:
	$(PY) -m app

migrate:
	$(PY) -m alembic upgrade head

revision:
	$(PY) -m alembic revision --autogenerate -m "$(m)"

seed:
	$(PY) -m app.cli seed-reference

country-root:
	$(PY) -m app.cli create-country-root --country "$(COUNTRY)" --name "$(NAME)"

superadmin:
	$(PY) -m app.cli create-superadmin --login "$(LOGIN)" --password "$(PASSWORD)" --name "$(NAME)"

partitions:
	$(PY) -m app.cli ensure-partitions

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check app tests

format:
	$(PY) -m ruff format app tests
