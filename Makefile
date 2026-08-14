SHELL := /usr/bin/env bash

CORE_SERVICES := mysql redis api runtime-worker sandbox-worker skill-sync-worker maintenance-worker frontend
ALL_SERVICES := $(CORE_SERVICES) connector-worker browser-worker
PYTEST := test/.venv/bin/pytest
PYTHON := test/.venv/bin/python

.PHONY: help bootstrap up up-all down reset status logs test-env unit frontend-build test compose-check check-secrets release-check

help:
	@echo "MyClaw v$$(tr -d '\n' < VERSION)"
	@echo ""
	@echo "  make up             Bootstrap and start the core local stack"
	@echo "  make up-all         Start core plus Browser and connector workers"
	@echo "  make down           Stop containers and preserve volumes"
	@echo "  make reset          Remove local containers and volumes (destructive)"
	@echo "  make status         Show Compose service status"
	@echo "  make logs           Follow core service logs"
	@echo "  make test-env       Create test/.venv and install test dependencies"
	@echo "  make test           Run backend unit tests and frontend build"
	@echo "  make check-secrets  Scan tracked files and Git history"
	@echo "  make release-check  Run the complete deterministic release gate"

bootstrap:
	@./scripts/bootstrap.sh

up: bootstrap
	docker compose up -d --build $(CORE_SERVICES)
	@echo "MyClaw UI: $$(docker compose port frontend 80 | tail -n 1)"
	@echo "API:        $$(docker compose port api 8000 | tail -n 1)"

up-all: bootstrap
	docker compose up -d --build $(ALL_SERVICES)

down:
	docker compose down --remove-orphans

reset:
	@./scripts/reset_local.sh

status:
	docker compose ps

logs:
	docker compose logs --follow --tail=200 api runtime-worker sandbox-worker frontend

test-env:
	@test -d test/.venv || python3 -m venv test/.venv
	@test/.venv/bin/pip install --upgrade pip
	@test/.venv/bin/pip install -r backend/requirements.txt -r test/requirements.txt

unit:
	@test -x $(PYTEST) || { echo "Missing test/.venv; run 'make test-env' first." >&2; exit 1; }
	@PYTHONPATH=backend $(PYTEST) -q test/unit/backend

frontend-build:
	@./scripts/run_frontend_build.sh

test: unit frontend-build

compose-check:
	@./scripts/check_compose_configs.sh

check-secrets:
	@./scripts/check_repository_secrets.sh --history

release-check:
	@./scripts/release_check.sh
