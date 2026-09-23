# data/Makefile — entry points for the Data team's pipeline.
#
# Start with `make help` or `make workflow`. Stage numbers match
# design/kea-data-engineering-stack.md and follow first-run order: Stage 0
# starts the stack, Stage 1 loads the source schema and fixtures into it.
#
# Existing low-level target names stay stable for scripts and debugging. The
# demo-* targets are the ordered, human-facing workflows.
.DEFAULT_GOAL := help

.PHONY: help workflow env-check \
	demo-bootstrap demo-refresh demo-dashboards rebuild-demo \
	images up up-streaming down clean logs ps psql \
	source-schema source-seed reseed-fixtures fixture-weather serving-schema \
	fetch-lcl init-data clean-generated assert-source \
	source-roles assert-isolation demo-independence avsc extract-metering \
	spark-parse assert-bronze assert-idempotent test-conformance \
	load-snowflake dbt-debug dbt-build dbt-test \
	export-warehouse migrate-serving configure-serving-roles publish-serving assert-load-atomic assert-serving \
	backfill-pipeline \
	assert-quality soda-scan soda-scan-serving \
	lint test test-transform ci ci-lint-test ci-migrations ci-story-guard ci-dbt-parse \
	ci-serving-db ci-dags ci-dbt-image ci-airflow-image deploy-check deploy \
	metabase-setup metabase-dashboard source-dashboard lake-summary \
	lake-dashboard stage5-dashboard plans-dashboard story-dashboard verify-dashboards

# DockerOperator bind mounts are resolved by the HOST daemon, not inside
# Airflow, so it needs this directory's path as the host sees it.
export HOST_PROJECT_DIR := $(CURDIR)
export COMPOSE_NETWORK  := kea_default

GEN         := pipeline/scripts/gen_source
COHORT_SIZE ?= 50

# A load date is a NEW ZEALAND calendar day, never a UTC day. Python is used
# because BSD and GNU date have incompatible relative-date flags.
LOAD_DATE ?= $(shell python3 -c "import datetime,zoneinfo;print(datetime.datetime.now(zoneinfo.ZoneInfo('Pacific/Auckland')).date()-datetime.timedelta(days=1))")
RELEASE   ?=

# The Snowflake key lives outside the repo and must be known at mount time.
# Everything else reaches containers through --env-file, which safely handles
# values such as AIRFLOW_CONN_* URIs containing '&'.
KEY_HOST := $(shell [ -f .env ] && grep -E '^SNOWFLAKE_PRIVATE_KEY_HOST=' .env | cut -d= -f2-)

# The served tables live in the backend's database (kea-db, ../infra), which
# containers reach as host.docker.internal: Docker Desktop defines that name,
# and on Linux BACKEND_HOST is what does.
BACKEND_HOST = --add-host=host.docker.internal:host-gateway

DOCKER_RUN = docker run --rm --network $(COMPOSE_NETWORK) --env-file .env $(BACKEND_HOST) \
  -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
  $(if $(KEY_HOST),-v $(KEY_HOST):/keys/snowflake.p8:ro,)

DBT  = $(DOCKER_RUN) -v $(CURDIR)/pipeline/dbt_project:/dbt -w /dbt dataeng/dbt:1.12.0 dbt
SODA = $(DOCKER_RUN) -v $(CURDIR)/pipeline/soda:/soda -w /soda dataeng/soda:latest
# The pipeline's own Postgres: the four source systems and the audit schemas.
PSQL = docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea


## Recommended workflows — start here

help:  ## show targets grouped by workflow and stage
	@awk 'BEGIN{FS=":.*?## "} /^## /{printf "\n\033[1m%s\033[0m\n", substr($$0,4); next} /^[a-z0-9][a-z0-9-]*:.*?## /{printf "  \033[36m%-22s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

workflow:  ## print the safe execution order without running anything
	@printf '%s\n' \
	  'FIRST RUN (empty volumes only)' \
	  '  0. start the backend database, kea-db (../infra); the served tables live there' \
	  '  1. cp .env.example .env, then fill it in' \
	  '  2. make demo-bootstrap' \
	  '  3. make demo-refresh' \
	  '' \
	  'AFTER DATA, TARIFF, OR PIPELINE CHANGES' \
	  '  1. make demo-refresh' \
	  '  2. make publish-serving RELEASE=<unique-id>  # shared environment' \
	  '' \
	  'AFTER A MERGE TO MAIN (deploy exactly what main holds)' \
	  '  1. commit your work to its branch (or git stash -u)' \
	  '  2. git switch main && git pull --ff-only' \
	  '  3. make deploy    # gates + make ci, dbt build, migrate, publish as git-<sha>, dashboards' \
	  '  4. git switch -   # back to your branch; git stash pop if you stashed' \
	  '' \
	  'DASHBOARD-ONLY CHANGES' \
	  '  1. make demo-dashboards' \
	  '' \
	  'REPUBLISH ONLY (the lake export is already current)' \
	  '  1. make migrate-serving   # only when migrations/serving changed' \
	  '  2. make publish-serving RELEASE=<unique-id>' \
	  '' \
	  'LOW-LEVEL DATA FLOW' \
	  '  Stage 2 source -> Stage 3 bronze/parsed -> Stage 4 Snowflake/dbt' \
	  '  -> Stage 5 publish into the backend database, with the Sprint 3' \
	  '     decisions in the same transaction -> dashboards/verification' \
	  '' \
	  'WARNING: demo-bootstrap includes source-seed and is for empty volumes only.'

env-check:
	@[ -f .env ] || { echo ".env is missing — cp .env.example .env and fill it in."; exit 1; }

# Ordered explicitly as recursive make calls so `make -j` cannot start later
# steps before their prerequisites are ready.
demo-bootstrap: env-check  ## one-time setup: Stage 0 stack, Stage 1 sources/fixtures, backend schema and roles, Metabase
	$(MAKE) images
	$(MAKE) up
	$(MAKE) source-schema
	$(MAKE) source-roles
	$(MAKE) source-seed
	$(MAKE) migrate-serving
	$(MAKE) configure-serving-roles
	$(MAKE) metabase-setup

demo-refresh: env-check  ## normal path: start stack, rebuild Sprint 2/3 data, refresh all dashboards
	$(MAKE) up
	$(MAKE) rebuild-demo
	$(MAKE) metabase-setup
	$(MAKE) demo-dashboards

demo-dashboards: env-check  ## refresh all six code-managed dashboards, then verify every card
	$(MAKE) metabase-dashboard
	$(MAKE) source-dashboard
	$(MAKE) lake-dashboard
	$(MAKE) stage5-dashboard
	$(MAKE) plans-dashboard
	$(MAKE) story-dashboard
	$(MAKE) verify-dashboards

rebuild-demo: env-check  ## Stages 2–5 + Sprint 3: compact 50-household rebuild, target under 30 min
	pipeline/scripts/rebuild_demo.sh


## Stage 0 — local batch stack

# dbt and Soda have separate images because their dependency pins conflict
# with Airflow's. `up` intentionally never rebuilds these images.
images:  ## [Stage 0] build the dataeng/* images; normally once per image change
	docker compose build
	docker build -t dataeng/dbt:1.12.0  pipeline/docker/dbt
	docker build -t dataeng/soda:latest pipeline/docker/soda

up: env-check  ## [Stage 0] start the batch stack; safe to repeat
	docker compose up -d
	@echo ""
	@echo "  airflow  http://localhost:8080  (admin / AIRFLOW_ADMIN_PASSWORD)"
	@echo "  minio    http://localhost:9001    spark    http://localhost:8082"
	@echo "  history  http://localhost:18080  (completed Spark jobs and DAGs)"
	@echo "  marquez  http://localhost:3000    grafana  http://localhost:3001"
	@echo "  metabase http://localhost:3002    pipeline postgres://kea@localhost:5433/kea  (sources)"
	@echo "  served tables: the backend's kea-db, localhost:5432 (../infra; not started here)"


## Stage 1 — source schema and nine fixtures

# The serving DDL is not applied here: the served tables live in the backend's
# database, created by `make migrate-serving` from migrations/serving.
# design/kea_serving_schema.sql stays the readable reference for them.
source-schema:  ## [Stage 1] apply the source DDL to the pipeline Postgres
	$(PSQL) < design/kea_source_schema.sql

source-seed:  ## [Stage 1] load the nine fixtures' source rows ONCE into an empty pipeline database
	pipeline/scripts/seed_fixtures.sh

# After changing a fixture. Removes the generated rows in the same transaction
# (they hang foreign keys off the fixtures'), so follow it with demo-refresh.
reseed-fixtures:  ## [Stage 1] replace the nine fixtures' source rows on a loaded database; then demo-refresh
	pipeline/scripts/seed_fixtures.sh --reseed

# The fixtures' own daily temperatures, which mart_daily_usage prefers over
# NIWA. Rerun after changing a fixture's weather, and commit the CSV it writes;
# it never touches kea-source-db.
fixture-weather:  ## [Stage 1] regenerate the fixtures' weather seed from sample/*.sql (throwaway Postgres)
	pipeline/scripts/gen_fixture_weather.sh

# The old name, kept only because the repo-root Makefile's data-serving-schema
# still calls it. No `##`, so `make help` does not list it.
serving-schema: source-schema


## Stage 2 — generated source data

fetch-lcl:  ## [Stage 2] download Low Carbon London data; needs KAGGLE_* in .env
	$(GEN)/fetch_lcl.sh

init-data: | fetch-lcl  ## [Stage 2] generate and load source databases; COHORT_SIZE defaults to 50
	@test -d raw/lcl/extracted || (echo "extracting half-hourly blocks..." && \
	  unzip -o -j -q raw/lcl/smart-meters-in-london.zip \
	    'halfhourly_dataset/halfhourly_dataset/block_*.csv' -d raw/lcl/extracted/ && \
	  unzip -o -j -q raw/lcl/smart-meters-in-london.zip \
	    'informations_households.csv' -d raw/lcl/extracted/)
	python3 $(GEN)/cohort.py --size $(COHORT_SIZE)
	python3 $(GEN)/gen_metering.py --stats
	python3 $(GEN)/gen_metering.py
	python3 $(GEN)/gen_reference.py
	python3 $(GEN)/gen_billing.py
	$(GEN)/load_source.sh

assert-source:  ## [Stage 2 gate] verify fixtures, LCL coverage, tariffs, bills and defects
	$(PSQL) -f - < $(GEN)/99_assert.sql

clean-generated:  ## [Stage 2 destructive] remove generated rows; keeps the nine fixtures
	$(PSQL) -q -f - < $(GEN)/00_clean_generated.sql
	@echo "generated rows removed; 'make demo-refresh' loads them back"


## Stage 3 — extract, bronze and parsed lake

source-roles: env-check  ## [Stage 3 setup] set passwords for the four isolated extract roles
	pipeline/scripts/set_extract_roles.sh

assert-isolation: env-check  ## [Stage 3 gate] prove each extract role is isolated from the other sources
	pipeline/scripts/assert_isolation.sh

avsc:  ## [Stage 3] regenerate the sixteen Avro contracts from the source schema
	pipeline/scripts/gen_avsc.sh

extract-metering:  ## [Stage 3] extract one NZ load date to bronze; set LOAD_DATE=YYYY-MM-DD
	docker compose exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 \
	  /opt/spark_jobs/extract_interval_reads.py $(LOAD_DATE) $(EXTRACT_ARGS)

spark-parse:  ## [Stage 3] reshape one bronze load date to bronze/parsed
	docker compose exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 \
	  /opt/spark_jobs/parse_interval_reads.py $(LOAD_DATE)

assert-bronze: assert-isolation  ## [Stage 3 gate] verify bronze conservation, contracts and quarantine
	docker compose exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 \
	  /opt/spark_jobs/assert_bronze.py $(GATE_ARGS)

assert-idempotent:  ## [Stage 3 gate] re-extract a date and verify the content hash is unchanged
	pipeline/scripts/assert_idempotent.sh $(LOAD_DATE)

test-conformance:  ## [Stage 3 test] verify parsed Parquet against KEA-047, quarantine routing and the 46/50-half-hour days
	docker compose exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 \
	  /opt/spark_jobs/tests/lake/test_lake_conformance.py

demo-independence:  ## [Stage 3 demo] break billing credentials and prove metering is unaffected
	pipeline/scripts/demo_independence.sh $(LOAD_DATE)


## Stage 4 — Snowflake warehouse and dbt

load-snowflake: env-check  ## [Stage 4] load one bronze generation into KEA.RAW
	$(DOCKER_RUN) -v $(CURDIR)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 load_all_bronze_to_snowflake.py $(LOAD_DATE)

dbt-debug: env-check  ## [Stage 4 setup] verify Snowflake key-pair connectivity
	$(DBT) debug --profiles-dir .

dbt-build: env-check  ## [Stage 4] run staging -> silver -> gold -> marts and data tests
	$(DBT) deps && $(DBT) build --profiles-dir .

dbt-test: env-check  ## [Stage 4 test] run dbt data tests without rebuilding models
	$(DBT) deps && $(DBT) test --profiles-dir .


## Stage 5 — export, then publish into the backend's database

export-warehouse: env-check  ## [Stage 5] export 20 serving tables from Snowflake to lake export/
	$(DOCKER_RUN) -v $(CURDIR)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 export_warehouse_to_lake.py $(TABLE)

migrate-serving: env-check  ## [Stage 5] apply migrations/serving to the backend database (KEA_MIGRATION_DSN)
	$(DOCKER_RUN) -v $(CURDIR):/opt/data:ro -w /opt/data/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 migrate_serving.py $(ARGS)

configure-serving-roles: env-check  ## [Stage 5 setup] set kea_loader/kea_app passwords in the backend database
	$(DOCKER_RUN) -v $(CURDIR):/opt/data:ro -w /opt/data/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 configure_serving_roles.py

# Neither migrates nor sets passwords. A publish connects as kea_loader, which
# can do neither, and a publish into a shared database must not change its
# schema as a side effect: run migrate-serving deliberately, first. The Sprint 3
# decision jobs run inside it, importing the API's pricing engine from the
# read-only mount so tariff arithmetic has one implementation.
publish-serving: env-check  ## [Stage 5 / Sprint 3] atomically publish base data and decision outputs; RELEASE required
	@test -n "$(RELEASE)" || { echo 'Set RELEASE to a unique release identifier'; exit 1; }
	$(DOCKER_RUN) -v $(CURDIR):/opt/data:ro \
	  -v $(CURDIR)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api \
	  -w /opt/data/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 publish_serving.py --release-id '$(RELEASE)'

assert-load-atomic: env-check  ## [Stage 5 gate] prove a failed publish changes nothing, on a throwaway Postgres
	pipeline/scripts/assert_load_atomic.sh

# As kea_loader, inside a transaction it rolls back: the checks build temporary
# helpers, which a read-only session refuses, and none of it may persist.
assert-serving: env-check  ## [Stage 5 gate] verify the published output against the nine fixtures
	docker run --rm -i --network $(COMPOSE_NETWORK) --env-file .env $(BACKEND_HOST) postgres:17.10 \
	  sh -c 'exec psql -X -v ON_ERROR_STOP=1 -c BEGIN -f - -c ROLLBACK "$$KEA_PUBLISH_DSN"' \
	  < pipeline/scripts/assert_serving.sql


## Stage 6 — Airflow orchestration

backfill-pipeline:  ## [Stage 6] rerun kea_daily_pipeline over FROM=... TO=...
	pipeline/scripts/backfill_pipeline.sh $(FROM) $(TO)


## Stage 7 — runtime quality and observability gates

assert-quality:  ## [Stage 7 gate] prove quarantine, alerts, blocked export and lineage; on a rebuild-demo layout set LOAD_DATE=YYYY-MM-DD
	pipeline/scripts/assert_quality.sh $(if $(filter command line environment,$(origin LOAD_DATE)),$(LOAD_DATE))

soda-scan: env-check  ## [Stage 7 gate] scan Snowflake freshness, volume and orphan rate
	$(SODA) soda scan -d kea -c configuration.yml -v load_date=$(LOAD_DATE) checks/warehouse.yml

soda-scan-serving: env-check  ## [Stage 7 report] scan the published tables in the backend database, as kea_app
	$(SODA) soda scan -d kea_serving -c configuration.yml checks/serving.yml


## Stage 8 — developer tests, CI checks and deploy

test-transform:  ## [Stage 2 unit test] test UK-to-NZ calendar conversion without infrastructure
	python3 -m unittest discover -s $(GEN)/tests

# The ruff version is the image tag and the rules are in ruff.toml, so every
# machine runs the same lint. The mount is read-only, hence --no-cache.
lint:  ## [Stage 8] run Ruff 0.16.3 over all of data/'s Python; rules in ruff.toml
	docker run --rm -v $(CURDIR):/io:ro -w /io ghcr.io/astral-sh/ruff:0.16.3 \
	  check --no-cache pipeline/ analysis/ sample/ load.py

test:  ## [Stage 8] run every host unit test; standard library only, no database, lake or Spark
	python3 -m unittest discover -s pipeline/tests -t pipeline/tests
	python3 -m unittest discover -s $(GEN)/tests

# CI's three data jobs call the ci-* targets, and `make ci` runs them all. None
# reads .env or uses DOCKER_RUN: CI holds no Snowflake or kea-db credentials, so
# each is offline apart from image, pip and dbt-hub pulls.
BASE ?= origin/main

ci:  ## [Stage 8] run everything CI's three data jobs run; needs no .env
	$(MAKE) ci-lint-test
	$(MAKE) ci-migrations
	$(MAKE) ci-story-guard
	$(MAKE) ci-dbt-parse
	$(MAKE) ci-serving-db
	$(MAKE) ci-dags

ci-lint-test:  ## [Stage 8 CI] ruff, then every host unit test
	$(MAKE) lint
	$(MAKE) test

ci-migrations:  ## [Stage 8 CI] fail if a serving migration already on BASE (default origin/main) changed
	pipeline/scripts/check_migrations_immutable.sh '$(BASE)'

ci-story-guard:  ## [Stage 8 CI] the Story page's cards: names unique, no identifier read
	python3 analysis/metabase/build_story_dashboard.py --check

# The same Dockerfiles as `make images`, without `docker compose`, which reads
# .env. Quick once cached.
ci-dbt-image:
	docker build -t dataeng/dbt:1.12.0 pipeline/docker/dbt

ci-airflow-image:
	docker build -t dataeng/airflow:3.3.0 pipeline/airflow

# Parsing needs a profile, not a warehouse: these values only have to exist.
# deps writes the gitignored dbt_packages/; target/ and logs/ stay in /tmp.
CI_DBT = docker run --rm -v $(CURDIR)/pipeline/dbt_project:/dbt -w /dbt \
  -e DO_NOT_TRACK=1 -e DBT_TARGET_PATH=/tmp/target -e DBT_LOG_PATH=/tmp/logs \
  -e SNOWFLAKE_ACCOUNT=ci -e SNOWFLAKE_USER=ci -e SNOWFLAKE_ROLE=ci -e SNOWFLAKE_WAREHOUSE=ci \
  -e SNOWFLAKE_DATABASE=KEA -e SNOWFLAKE_PRIVATE_KEY_PATH=/nonexistent.p8 -e DBT_SCHEMA=ci

ci-dbt-parse: ci-dbt-image  ## [Stage 8 CI] dbt deps, then parse offline: refs, sources, Jinja, YAML, tests
	$(CI_DBT) dataeng/dbt:1.12.0 dbt deps
	$(CI_DBT) --network none dataeng/dbt:1.12.0 dbt parse --profiles-dir . --no-partial-parse

# Migrates an empty Postgres 16 (kea-db's version), publishes into it and
# retries, then removes it and its network whatever happened. pg_isready goes
# over TCP because initdb's temporary server listens on the socket only.
ci-serving-db: ci-dbt-image  ## [Stage 8 CI] serving migrations and a publication on a throwaway Postgres 16
	set -eu; net=kea_ci; db=kea-ci-serving-db; \
	cleanup() { docker rm -f -v $$db >/dev/null 2>&1 || true; docker network rm $$net >/dev/null 2>&1 || true; }; \
	cleanup; trap cleanup EXIT; \
	docker network create $$net >/dev/null; \
	docker run -d --rm --name $$db --network $$net -e POSTGRES_USER=kea -e POSTGRES_DB=kea \
	  -e POSTGRES_PASSWORD=ci -e TZ=UTC postgres:16.15-alpine >/dev/null; \
	for _ in $$(seq 30); do docker exec $$db pg_isready -h 127.0.0.1 -U kea -d kea -q && break; sleep 1; done; \
	docker exec $$db pg_isready -h 127.0.0.1 -U kea -d kea -q || { echo "the throwaway Postgres never came up" >&2; exit 1; }; \
	docker run --rm --network $$net -v $(CURDIR):/opt/data:ro \
	  -v $(CURDIR)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api \
	  -e KEA_TEST_DSN=postgresql://kea:ci@$$db:5432/kea -e PYTHONDONTWRITEBYTECODE=1 \
	  -w /opt/data/pipeline/tests dataeng/dbt:1.12.0 python3 -m unittest -v test_serving_publication

# In the Airflow image, offline and without .env, over the folders compose
# mounts into the dag-processor, read-only.
ci-dags: ci-airflow-image  ## [Stage 8 CI] import every DAG in the Airflow image, offline: six DAGs, no errors
	docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
	  -v $(CURDIR)/pipeline/airflow/dags:/opt/airflow/dags:ro \
	  -v $(CURDIR)/pipeline/lib:/opt/airflow/lib:ro \
	  -v $(CURDIR)/pipeline/scripts:/opt/airflow/scripts:ro \
	  -v $(CURDIR)/pipeline/contracts:/opt/airflow/contracts:ro \
	  -v $(CURDIR)/pipeline/tests:/opt/airflow/tests:ro \
	  --entrypoint python dataeng/airflow:3.3.0 \
	  -m unittest discover -v -s /opt/airflow/tests -p test_dag_import.py

# CD, by hand on this machine after a merge; `make workflow` shows the git steps
# around it. The stack's bind mounts point at this checkout, so this is the one
# that deploys. Recursive makes, as in demo-refresh, so `make -j` cannot reorder
# the steps. Never part of a deploy: rebuild-demo, source generation, reseeding,
# extracts, lake or RAW rewrites, TRUNCATE, pausing or unpausing a DAG, and
# configure-serving-roles.
DEPLOY_RELEASE = $(or $(RELEASE),git-$(shell git rev-parse --short=12 HEAD))

deploy-check:  ## [Stage 8 CD] the deploy's five gates, then make ci; changes nothing
	pipeline/scripts/deploy_gate.sh
	$(MAKE) ci

# The restart loads DAG and lib/ changes now rather than on the next parse; the
# gate found no run in flight, so nothing is interrupted. A failing dbt test
# stops the deploy before kea-db changes. The serving scan is a report, not a
# gate (see the DAG), so it runs last: a finding still fails the deploy, but
# after the dashboards are rebuilt.
deploy: deploy-check  ## [Stage 8 CD] after a merge to main: dbt build, migrate, publish as git-<sha>, dashboards
	$(MAKE) images
	$(MAKE) up
	docker compose restart airflow-dag-processor airflow-scheduler
	$(MAKE) dbt-build
	$(MAKE) export-warehouse
	$(MAKE) migrate-serving
	$(MAKE) publish-serving RELEASE=$(DEPLOY_RELEASE)
	$(MAKE) assert-serving
	$(MAKE) metabase-setup
	$(MAKE) demo-dashboards
	$(MAKE) soda-scan-serving
	@echo "deployed $(DEPLOY_RELEASE)"


## Stage 9 — streaming profile

up-streaming: env-check  ## [Stage 9] start batch stack plus Kafka, Schema Registry and Flink
	docker compose --profile streaming up -d


## Dashboards — grouped by the stage or sprint they demonstrate

# Metabase stores connections and questions in its own application database;
# setup and every builder are safe to rerun.
metabase-setup: env-check  ## [Analysis setup] connect backend serving and local pipeline databases
	python3 analysis/metabase/metabase_setup.py

metabase-dashboard: env-check  ## [Stage 1 / Sprint 2] rebuild the nine-household dashboard
	python3 analysis/metabase/build_dashboard.py

source-dashboard:  ## [Stage 2] refresh source rollups and rebuild the LCL source dashboard
	$(PSQL) -q -f - < analysis/metabase/source_views.sql
	python3 analysis/metabase/build_source_dashboard.py

lake-summary:  ## [Stage 3] rebuild lake_audit rollups from Parquet in MinIO
	docker compose exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 \
	  /opt/analysis/metabase/summarise_lake.py

lake-dashboard: lake-summary  ## [Stage 3] rebuild extract/bronze/parsed dashboard after lake summary
	python3 analysis/metabase/build_stage3_dashboard.py

stage5-dashboard: env-check  ## [Stage 5] rebuild the export/publish dashboard, over both Metabase databases
	python3 analysis/metabase/build_stage5_dashboard.py

# Requires at least one recommendation and hardship generation.
plans-dashboard: env-check  ## [Sprint 3] rebuild plans, bill examples, hardship and alerts dashboard
	python3 analysis/metabase/build_plans_dashboard.py

story-dashboard: env-check  ## [Demonstration] rebuild the one-page Story walkthrough, identifiers excluded
	python3 analysis/metabase/build_story_dashboard.py

verify-dashboards:  ## [Dashboard gate] execute every card and verify data, axes and layout
	python3 analysis/metabase/verify.py


## Operations and destructive commands

ps:  ## show running pipeline containers
	docker compose ps

logs:  ## follow pipeline container logs
	docker compose logs -f

psql:  ## open an interactive shell on the pipeline database (sources, audit)
	docker compose exec kea-source-db psql -U kea -d kea

down:  ## stop batch and streaming containers; keep volumes
	docker compose --profile streaming down

clean:  ## DESTRUCTIVE: stop containers and delete this project's volumes
	docker compose --profile streaming down -v
