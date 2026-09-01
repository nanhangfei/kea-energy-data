# data/Makefile — the Data team's pipeline.
#
# Target names are the conventional ones for a stack of this shape — up, down,
# dbt-build, soda-scan. The root Makefile wraps them as `data-*`: prefix at the
# boundary, identical inside.
#
# The stack is self-contained; everything it needs is in this repo. The images
# are tagged `dataeng/*` because they hold no project code, so `up` never
# rebuilds them — that is `make images`, run deliberately. See
# design/kea-data-engineering-stack.md §7.
.PHONY: help env-check images up up-streaming down clean logs ps init-data \
        fetch-lcl test-transform assert-source clean-generated source-dashboard \
        lint test dbt-debug dbt-build dbt-test soda-scan spark-parse \
        serving-schema serving-seed psql metabase-setup metabase-dashboard

# DockerOperator bind mounts are resolved by the HOST daemon, not inside
# Airflow, so it needs this directory's path as the host sees it.
export HOST_PROJECT_DIR := $(PWD)
export COMPOSE_NETWORK  := kea_default

# The one value make itself needs: the Snowflake key lives outside the repo, so
# its host path must be known at mount time. Everything else reaches containers
# through --env-file, which (unlike shell sourcing) copes with the '&' in the
# AIRFLOW_CONN_* URIs.
KEY_HOST := $(shell [ -f .env ] && grep -E '^SNOWFLAKE_PRIVATE_KEY_HOST=' .env | cut -d= -f2-)

DOCKER_RUN = docker run --rm --network $(COMPOSE_NETWORK) --env-file .env \
  -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
  $(if $(KEY_HOST),-v $(KEY_HOST):/keys/snowflake.p8:ro,)

DBT   = $(DOCKER_RUN) -v $(PWD)/pipeline/dbt_project:/dbt -w /dbt dataeng/dbt:1.12.0 dbt
SODA  = $(DOCKER_RUN) -v $(PWD)/pipeline/soda:/soda -w /soda dataeng/soda:latest
PSQL  = docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d kea

help:  ## show this help
	@awk 'BEGIN{FS=":.*?## "} /^## /{printf "\n\033[1m%s\033[0m\n", substr($$0,4); next} /^[a-z][a-z-]*:.*?## /{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

env-check:
	@[ -f .env ] || { echo ".env is missing — cp .env.example .env and fill it in."; exit 1; }

## Stack
# dbt and Soda are NOT in the Airflow image: installing dbt-snowflake and
# soda-core-snowflake alongside Airflow's pinned deps is unresolvable for pip
# (ResolutionTooDeep). Each gets its own image; DockerOperator runs them.
images:  ## build the dataeng/* images (never rebuilt by `up` — see §7)
	docker compose build
	docker build -t dataeng/dbt:1.12.0  pipeline/docker/dbt
	docker build -t dataeng/soda:latest pipeline/docker/soda

up: env-check  ## start the batch stack; streaming is behind a profile until Stage 9
	docker compose up -d
	@echo ""
	@echo "  airflow  http://localhost:8080  (admin / AIRFLOW_ADMIN_PASSWORD)"
	@echo "  minio    http://localhost:9001    spark    http://localhost:8082"
	@echo "  marquez  http://localhost:3000    grafana  http://localhost:3001"
	@echo "  metabase http://localhost:3002  (make metabase-setup on first run)"
	@echo "  serving  postgres://kea@localhost:5433/kea"

up-streaming: env-check  ## Stage 9: batch stack + Kafka, Schema Registry and Flink
	docker compose --profile streaming up -d

down:  ## stop and remove the containers, batch + streaming (keeps volumes)
	docker compose --profile streaming down

clean:  ## like down, but also wipes this project's volumes
	docker compose --profile streaming down -v

logs:  ## tail the stack
	docker compose logs -f

ps:  ## what is running
	docker compose ps

## Source data
## Source data (Stage 2)
GEN         := pipeline/scripts/gen_source
COHORT_SIZE ?= 500

fetch-lcl:  ## download the Low Carbon London dataset (needs KAGGLE_* in .env)
	$(GEN)/fetch_lcl.sh

init-data: | fetch-lcl  ## build the three source databases from LCL
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

test-transform:  ## the UK->NZ transform, no database and no dataset needed
	python3 -m unittest discover -s $(GEN)/tests

clean-generated:  ## remove generated source rows so serving-seed can re-run
	$(PSQL) -q -f - < $(GEN)/00_clean_generated.sql
	@echo "generated rows removed; 'make serving-seed' can now run"

source-dashboard:  ## refresh the summary views and build the Stage 2 Metabase dashboard
	$(PSQL) -q -f - < analysis/metabase/source_views.sql
	python3 analysis/metabase/build_source_dashboard.py

assert-source:  ## the Stage 2 gate
	$(PSQL) -f - < $(GEN)/99_assert.sql

## Serving database
serving-schema:  ## apply the source + serving DDL to kea-serving-db
	$(PSQL) < design/kea_source_schema.sql
	$(PSQL) < design/kea_serving_schema.sql

serving-seed:  ## load the nine hand-written fixtures (what the pipeline will replace)
	@for f in sample/[0-9][0-9]-*.sql; do echo "-> $$f"; $(PSQL) < "$$f" || exit 1; done

psql:  ## open a shell on the serving database
	docker compose exec kea-serving-db psql -U kea -d kea

## Analysis
# Metabase stores its database connections in its own application database, not
# in env vars, so a fresh container would otherwise need someone to click
# through the setup wizard. This provisions it over the API instead, and is safe
# to re-run.
metabase-setup: env-check  ## create the Metabase admin user and connect the serving DB
	python3 pipeline/scripts/metabase_setup.py

# The queries live in analysis/metabase/build_dashboard.py, which is the
# version-controlled record — a Metabase question lives in Metabase's own
# database and does not survive `make clean`. Re-run to rebuild.
metabase-dashboard: env-check  ## (re)build the Kea dashboard and its questions in Metabase
	python3 analysis/metabase/build_dashboard.py

## Pipeline
# Spark runs ONE job here, not a bronze/silver/gold chain: only the 2.45B-row
# meter-read extract needs distributed compute. Billing and catalogue are plain
# scripts. See design/kea-data-engineering-stack.md 1.1.
LOAD_DATE ?= $(shell date -u +%F)

spark-parse:  ## reshape a load date's raw meter reads -> bronze/parsed
	docker compose exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 \
	  /opt/spark_jobs/parse_interval_reads.py $(LOAD_DATE)

dbt-debug: env-check  ## verify the Snowflake connection (key-pair)
	$(DBT) debug --profiles-dir .

dbt-build: env-check  ## dbt deps + build (staging -> silver -> gold -> marts)
	$(DBT) deps && $(DBT) build --profiles-dir .

dbt-test: env-check  ## run dbt tests only
	$(DBT) deps && $(DBT) test --profiles-dir .

soda-scan: env-check  ## runtime quality gate — freshness, volume, orphan rate
	$(SODA) soda scan -d kea -c configuration.yml checks/

## Checks
lint:  ## ruff over the pipeline code
	ruff check pipeline/

test:  ## unit tests for the parse job and the loader
	pytest -q pipeline/tests/
