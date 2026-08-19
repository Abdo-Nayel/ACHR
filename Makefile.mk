# =============================================================================
# Developer entry points. `make help` lists everything.
#
# Every target runs the same way locally and in CI, so "works on my machine"
# means "works in CI". Anything a human needs to type twice belongs here.
# =============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

BACKEND       := backend
MANAGE        := python $(BACKEND)/manage.py
COMPOSE       := docker compose
DJANGO_SETTINGS_MODULE ?= config.settings.dev
export DJANGO_SETTINGS_MODULE

SCHEMA_OUT    := frontend/packages/domain/openapi.json

.PHONY: help install migrate makemigrations seed run worker beat test lint \
        typecheck schema up down logs shell dbshell fmt check audit \
        coverage reset-db superuser rls-verify

## help: list available targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/## //' | column -t -s ':'

# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------
## install: create the virtualenv and install dev dependencies + git hooks
install:
	python -m venv .venv || true
	. .venv/bin/activate && \
	  pip install --upgrade pip pip-tools && \
	  pip install -r $(BACKEND)/requirements/dev.txt
	pre-commit install || echo "pre-commit not installed; skipping hooks"
	@echo "Done. Activate with: source .venv/bin/activate"

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
## migrate: apply migrations as the OWNER role (erp_migrator), not the app role
migrate:
	POSTGRES_APP_USER=$${POSTGRES_MIGRATION_USER:-erp_migrator} \
	POSTGRES_APP_PASSWORD=$${POSTGRES_MIGRATION_PASSWORD:-erp_migrator} \
	  $(MANAGE) migrate --noinput
	@$(MAKE) --no-print-directory rls-verify

## makemigrations: generate migrations (review them; never blind-commit)
makemigrations:
	$(MANAGE) makemigrations

## rls-verify: fail loudly if any tenant table lacks FORCE ROW LEVEL SECURITY
rls-verify:
	$(MANAGE) check_rls --strict

## seed: load the demo tenants, chart of accounts, roles and sample data
seed:
	$(MANAGE) seed_permissions
	$(MANAGE) seed_system_roles
	$(MANAGE) seed_demo_tenant --name "Acme Trading" --country EG --currency EGP
	$(MANAGE) seed_demo_tenant --name "Globex Industrial" --country AE --currency AED

## reset-db: drop, recreate, migrate and seed. Destroys local data
reset-db:
	$(COMPOSE) down -v
	$(COMPOSE) up -d postgres redis
	@until $(COMPOSE) exec -T postgres pg_isready -U postgres -d erp; do sleep 1; done
	$(MAKE) migrate seed

## superuser: create a platform admin
superuser:
	$(MANAGE) createsuperuser

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
## run: development server on :8000
run:
$(MANAGE) runserver 0.0.0.0:8000

## worker: general Celery worker (default, payments, reports, notifications)
worker:
	celery -A config worker \
	  --loglevel=INFO \
	  --queues=default,payments,reports,notifications \
	  --concurrency=4 \
	  --prefetch-multiplier=1

## worker-payroll: isolated payroll worker (low concurrency, prefetch 1)
worker-payroll:
	celery -A config worker \
	  --loglevel=INFO --queues=payroll --concurrency=2 --prefetch-multiplier=1

## beat: the periodic task scheduler. Run exactly ONE of these
beat:
	celery -A config beat --loglevel=INFO \
	  --scheduler=django_celery_beat.schedulers:DatabaseScheduler

## up: start the whole stack in docker
up:
	$(COMPOSE) up -d --build

## down: stop the stack (volumes preserved)
down:
	$(COMPOSE) down

## logs: tail all container logs
logs:
	$(COMPOSE) logs -f --tail=100

## shell: Django shell_plus with models pre-imported
shell:
	$(MANAGE) shell_plus

## dbshell: psql as the application role, so RLS applies to what you see
dbshell:
	$(MANAGE) dbshell

# -----------------------------------------------------------------------------
# Quality
# -----------------------------------------------------------------------------
## test: full test suite, parallel, with coverage gate
test:
	pytest -q --cov=$(BACKEND)/apps --cov-report=term-missing --cov-fail-under=85 -n auto

## coverage: HTML coverage report
coverage:
	pytest -q --cov=$(BACKEND)/apps --cov-report=html
	@echo "open htmlcov/index.html"

## lint: ruff lint + format check
lint:
	ruff check $(BACKEND)
	ruff format --check $(BACKEND)

## fmt: apply ruff formatting and autofixes
fmt:
	ruff check --fix $(BACKEND)
	ruff format $(BACKEND)

## typecheck: mypy with django-stubs
typecheck:
	mypy $(BACKEND)/apps $(BACKEND)/config

## audit: dependency CVEs + static security scan
audit:
	pip-audit -r $(BACKEND)/requirements/base.txt
	bandit -q -r $(BACKEND)/apps -x '*/tests/*'

## check: everything CI runs, in CI order
check: lint typecheck test audit

# -----------------------------------------------------------------------------
# API contract
# -----------------------------------------------------------------------------
## schema: regenerate the OpenAPI schema consumed by the TypeScript clients
schema:
	@mkdir -p $$(dirname $(SCHEMA_OUT))
	$(MANAGE) spectacular --validate --fail-on-warn --format openapi-json --file $(SCHEMA_OUT)
	@echo "Schema written to $(SCHEMA_OUT)"
	@echo "Now run: pnpm --filter @erp/domain generate"
