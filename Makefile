# Development tasks for specivo-redmine-migration. See README.md.

.PHONY: install test lint fixture-up fixture-seed fixture-files fixture-down fixture-reset fixture-export fixture-test

# PROFILE=pg (default) or PROFILE=mysql.
PROFILE ?= pg
# One fixture per checkout: the compose project is named after this
# directory's path, so two checkouts never share containers or files.
FIXTURE_PROJECT ?= redmine-tool-fixture-$(shell printf '%s' "$(CURDIR)" | shasum | cut -c1-10)
FIXTURE_COMPOSE := docker compose -p $(FIXTURE_PROJECT) -f docker-compose.fixture.yml --profile $(PROFILE)
FIXTURE_APP := $(if $(filter mysql,$(PROFILE)),redmine-mysql,redmine-pg)
FIXTURE_FILES := .fixture-files/$(PROFILE)

REDMINE_TOOL_FIXTURE_PG_PORT ?= 5452
REDMINE_TOOL_FIXTURE_MYSQL_PORT ?= 3309
FIXTURE_DB_URL := $(if $(filter mysql,$(PROFILE)),mysql://redmine:redmine@127.0.0.1:$(REDMINE_TOOL_FIXTURE_MYSQL_PORT)/redmine,postgresql://redmine:redmine@127.0.0.1:$(REDMINE_TOOL_FIXTURE_PG_PORT)/redmine)
# Where fixture-export writes, and the instance name it gives the bundle.
FIXTURE_OUT ?= out/fixture-$(PROFILE)
FIXTURE_INSTANCE ?= pytest-fixture

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --strict src

fixture-up:  ## Start the Redmine 7.0.1 fixture (PROFILE=pg|mysql)
	$(FIXTURE_COMPOSE) up -d --wait

fixture-seed:  ## Fill the fixture with test content
	$(FIXTURE_COMPOSE) exec -T $(FIXTURE_APP) bundle exec rails runner /seed/seed.rb

fixture-files:  ## Copy the fixture's files directory out to .fixture-files/PROFILE
	rm -rf $(FIXTURE_FILES)
	mkdir -p $(dir $(FIXTURE_FILES))
	$(FIXTURE_COMPOSE) cp $(FIXTURE_APP):/usr/src/redmine/files $(FIXTURE_FILES)

fixture-down:  ## Stop the fixture and discard its data and files
	$(FIXTURE_COMPOSE) down -v
	rm -rf $(FIXTURE_FILES)

fixture-reset: fixture-down fixture-up fixture-seed fixture-files  ## Recreate, reseed and copy out the files

fixture-export:  ## Write a bundle from the fixture to FIXTURE_OUT (a directory)
	rm -rf $(FIXTURE_OUT)
	uv run specivo-redmine-migration --db-url "$(FIXTURE_DB_URL)" --files-dir $(FIXTURE_FILES) \
		--out $(FIXTURE_OUT) --instance $(FIXTURE_INSTANCE) --validate $(ARGS)

fixture-test:  ## Run the end-to-end test against the running fixture
	REDMINE_FIXTURE_REQUIRED=1 uv run pytest tests/test_e2e_fixture.py -k $(PROFILE)
