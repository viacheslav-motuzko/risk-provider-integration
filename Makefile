# Application code never reads .env; in production the platform injects real environment variables.
-include .env
export

UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help install lock fmt lint typecheck imports test check mutate db-up db-down migrate run up down makemigrations

help:  ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install:  ## Create the environment from uv.lock
	$(UV) sync

lock:  ## Re-resolve dependencies to the newest compatible versions (do this once, early)
	$(UV) lock --upgrade

fmt:  ## Format and apply safe lint fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint:  ## Lint and check formatting
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck:  ## Static type check
	$(UV) run mypy .

imports:  ## Enforce the dependency rules between packages
	$(UV) run lint-imports

test:  ## Run tests (needs Postgres: make db-up)
	@# exit code 5 = no tests collected: tolerated so an empty scaffold passes `make check`
	$(UV) run pytest; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

check: lint typecheck imports test  ## Everything a stage must pass before its summary

mutate:  ## Sabotage each invariant and check that a test fails (needs Postgres; prints a table)
	$(UV) run python tools/mutation_check.py

db-up:  ## Start Postgres and wait until it is healthy
	docker compose up -d --wait db

db-down:  ## Stop Postgres (data volume is kept)
	docker compose stop db

migrate:  ## Apply migrations
	$(UV) run python manage.py migrate

makemigrations:  ## Make migrations
	$(UV) run python manage.py makemigrations

run:  ## Run the integration process locally
	$(UV) run python manage.py run_risk_provider

up:  ## Full stack in containers (stub until stage 5)
	docker compose --profile app up --build

down:  ## Stop and remove the containers
	docker compose --profile app down
