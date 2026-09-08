# data/Makefile — entry points for the Data team's pipeline.
#
# Start with `make help` or `make workflow`. Stage numbers match
# design/kea-data-engineering-stack.md. They describe pipeline ownership;
# first-time runtime order starts Stage 1 before applying the Stage 0 schemas.
#
# Existing low-level target names stay stable for scripts and debugging. The
# demo-* targets are the ordered, human-facing workflows.
.DEFAULT_GOAL := help

.PHONY: help workflow env-check \
	demo-bootstrap demo-refresh demo-dashboards rebuild-demo \
	images up up-streaming down clean logs ps psql \
	serving-schema serving-seed \
	fetch-lcl init-data clean-generated assert-source \
	source-roles assert-isolation demo-independence avsc extract-metering \
	spark-parse assert-bronze assert-idempotent test-conformance \
	load-snowflake dbt-debug dbt-build dbt-test \
	serving-roles export-warehouse load-serving load-backend backend-sync backend-schema assert-load-atomic assert-serving \
	backend-db-check \
	build-forecasts recommend-plans verify-plan-bills assess-hardship \
	create-hardship-alerts backfill-pipeline \
	assert-quality soda-scan soda-scan-serving \
	lint test test-transform test-parse \
	metabase-setup metabase-dashboard source-dashboard lake-summary \
	lake-dashboard stage5-dashboard plans-dashboard verify-dashboards

# DockerOperator bind mounts are resolved by the HOST daemon, not inside
# Airflow, so it needs this directory's path as the host sees it.
export HOST_PROJECT_DIR := $(PWD)
export COMPOSE_NETWORK  := kea_default

GEN         := pipeline/scripts/gen_source
COHORT_SIZE ?= 50

# A load date is a NEW ZEALAND calendar day, never a UTC day. Python is used
# because BSD and GNU date have incompatible relative-date flags.
LOAD_DATE ?= $(shell python3 -c "import datetime,zoneinfo;print(datetime.datetime.now(zoneinfo.ZoneInfo('Pacific/Auckland')).date()-datetime.timedelta(days=1))")

# The Snowflake key lives outside the repo and must be known at mount time.
# Everything else reaches containers through --env-file, which safely handles
# values such as AIRFLOW_CONN_* URIs containing '&'.
KEY_HOST := $(shell [ -f .env ] && grep -E '^SNOWFLAKE_PRIVATE_KEY_HOST=' .env | cut -d= -f2-)
BACKEND_DSN := $(shell [ -f .env ] && grep -E '^KEA_BACKEND_DSN=' .env | cut -d= -f2-)

DOCKER_RUN = docker run --rm --network $(COMPOSE_NETWORK) --env-file .env \
  -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
  $(if $(KEY_HOST),-v $(KEY_HOST):/keys/snowflake.p8:ro,)

DBT  = $(DOCKER_RUN) -v $(PWD)/pipeline/dbt_project:/dbt -w /dbt dataeng/dbt:1.12.0 dbt
SODA = $(DOCKER_RUN) -v $(PWD)/pipeline/soda:/soda -w /soda dataeng/soda:latest
PSQL = docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d kea


## Recommended workflows — start here

help:  ## show targets grouped by workflow and stage
	@awk 'BEGIN{FS=":.*?## "} /^## /{printf "\n\033[1m%s\033[0m\n", substr($$0,4); next} /^[a-z0-9][a-z0-9-]*:.*?## /{printf "  \033[36m%-22s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

workflow:  ## print the safe execution order without running anything
	@printf '%s\n' \
	  'FIRST RUN (empty volumes only)' \
	  '  1. cp .env.example .env, then fill it in' \
	  '  2. make demo-bootstrap' \
	  '  3. make demo-refresh' \
	  '' \
	  'AFTER DATA, TARIFF, OR PIPELINE CHANGES' \
	  '  1. make demo-refresh' \
	  '' \
	  'DASHBOARD-ONLY CHANGES' \
	  '  1. make demo-dashboards' \
	  '' \
	  'LOW-LEVEL DATA FLOW' \
	  '  Stage 2 source -> Stage 3 bronze/parsed -> Stage 4 Snowflake/dbt' \
	  '  -> Stage 5 serving -> Sprint 3 decisions -> dashboards/verification' \
	  '' \
	  'WARNING: demo-bootstrap includes serving-seed and is for empty volumes only.'

env-check:
	@[ -f .env ] || { echo ".env is missing — cp .env.example .env and fill it in."; exit 1; }

# Ordered explicitly as recursive make calls so `make -j` cannot start later
# steps before their prerequisites are ready.
demo-bootstrap: env-check  ## one-time setup: Stage 1 stack, Stage 0 schema/fixtures, roles, Metabase
	$(MAKE) images
	$(MAKE) up
	$(MAKE) serving-schema
	$(MAKE) source-roles
	$(MAKE) serving-roles
	$(MAKE) serving-seed
	$(MAKE) metabase-setup

demo-refresh: env-check  ## normal path: start stack, rebuild Sprint 2/3 data, refresh all dashboards
	$(MAKE) up
	$(MAKE) rebuild-demo
	$(MAKE) metabase-setup
	$(MAKE) demo-dashboards

demo-dashboards: env-check  ## refresh all five code-managed dashboards, then verify every card
	$(MAKE) metabase-dashboard
	$(MAKE) source-dashboard
	$(MAKE) lake-dashboard
	$(MAKE) stage5-dashboard
	$(MAKE) plans-dashboard
	$(MAKE) verify-dashboards

rebuild-demo: env-check  ## Stages 2–5 + Sprint 3: compact 50-household rebuild, target under 30 min
	pipeline/scripts/rebuild_demo.sh


## Stage 1 — local batch stack

# dbt and Soda have separate images because their dependency pins conflict
# with Airflow's. `up` intentionally never rebuilds these images.
images:  ## [Stage 1] build the dataeng/* images; normally once per image change
	docker compose build
	docker build -t dataeng/dbt:1.12.0  pipeline/docker/dbt
	docker build -t dataeng/soda:latest pipeline/docker/soda

up: env-check  ## [Stage 1] start the batch stack; safe to repeat
	docker compose up -d
	@echo ""
	@echo "  airflow  http://localhost:8080  (admin / AIRFLOW_ADMIN_PASSWORD)"
	@echo "  minio    http://localhost:9001    spark    http://localhost:8082"
	@echo "  history  http://localhost:18080  (completed Spark jobs and DAGs)"
	@echo "  marquez  http://localhost:3000    grafana  http://localhost:3001"
	@echo "  metabase http://localhost:3002    serving  postgres://kea@localhost:5433/kea"


## Stage 0 — domain schema and nine fixtures (run after Stage 1 is up)

serving-schema:  ## [Stage 0] apply source and serving DDL to the pipeline Postgres
	$(PSQL) < design/kea_source_schema.sql
	$(PSQL) < design/kea_serving_schema.sql

# The application's Postgres is a separate target.  It must be provisioned with
# the same serving schema before load-backend is used.  This check is the explicit,
# fail-closed entry point for verifying that the optional backend DB is reachable.
backend-db-check: env-check  ## [Integration check] verify the optional backend Postgres is reachable; no data is written
	@test -n "$(BACKEND_DSN)" || { echo "KEA_BACKEND_DSN is not set in .env; refusing to touch the application database."; exit 1; }
	$(DOCKER_RUN) -e KEA_BACKEND_DSN='$(BACKEND_DSN)' dataeng/dbt:1.12.0 \
		python3 -c 'import os, urllib.parse, socket; u=urllib.parse.urlparse(os.environ["KEA_BACKEND_DSN"]); socket.create_connection((u.hostname, u.port or 5432), timeout=5).close(); print("backend postgres reachable:", u.hostname, u.port or 5432, u.path.lstrip("/"))'

backend-schema:  ## [Integration setup] apply serving schema to backend once; skip when already present
	@tables=$$(docker exec kea-db psql -U kea -d kea -Atqc "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname in ('common','metering','billing','catalogue','kea') and c.relkind in ('r','p')"); \
	if [ "$$tables" = "0" ]; then \
		echo "backend serving schemas are empty; applying schema"; \
		docker exec -i kea-db psql -v ON_ERROR_STOP=1 -U kea -d kea < design/kea_serving_schema.sql; \
	else \
		echo "backend serving schema already contains $$tables table(s); skipping CREATE statements (no DROP/rebuild is performed)"; \
	fi

serving-seed:  ## [Stage 0] load nine Sprint 2 fixtures ONCE into an empty database
	pipeline/scripts/seed_fixtures.sh


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
	@echo "generated rows removed; 'make serving-seed' can now run"


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

test-conformance:  ## [Stage 3 test] verify parsed Parquet and quarantine routing against KEA-047
	docker compose exec -T spark-master /opt/spark/bin/spark-submit \
	  --master spark://spark-master:7077 \
	  /opt/spark_jobs/tests/lake/test_lake_conformance.py

demo-independence:  ## [Stage 3 demo] break billing credentials and prove metering is unaffected
	pipeline/scripts/demo_independence.sh $(LOAD_DATE)


## Stage 4 — Snowflake warehouse and dbt

load-snowflake: env-check  ## [Stage 4] load one bronze generation into KEA.RAW
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 load_all_bronze_to_snowflake.py $(LOAD_DATE)

dbt-debug: env-check  ## [Stage 4 setup] verify Snowflake key-pair connectivity
	$(DBT) debug --profiles-dir .

dbt-build: env-check  ## [Stage 4] run staging -> silver -> gold -> marts and data tests
	$(DBT) deps && $(DBT) build --profiles-dir .

dbt-test: env-check  ## [Stage 4 test] run dbt data tests without rebuilding models
	$(DBT) deps && $(DBT) test --profiles-dir .


## Stage 5 — export and serving store

serving-roles: env-check  ## [Stage 5 setup] set passwords for kea_loader and kea_app
	pipeline/scripts/set_serving_roles.sh

export-warehouse: env-check  ## [Stage 5] export 20 serving tables from Snowflake to lake export/
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 export_warehouse_to_lake.py $(TABLE)

load-serving: env-check  ## [Stage 5] load lake export/ into Postgres in foreign-key order
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 load_export_to_serving.py $(TABLE)

load-backend: env-check  ## [Stage 5] load the same export into backend Postgres; requires KEA_BACKEND_DSN
	@test -n "$(BACKEND_DSN)" || { echo "KEA_BACKEND_DSN is not set in .env; refusing to touch the application database."; exit 1; }
	$(DOCKER_RUN) -e KEA_SERVING_DSN='$(BACKEND_DSN)' \
	  -v $(PWD)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 load_export_to_serving.py $(TABLE)

backend-sync: env-check  ## [Stage 5] start backend DB, verify schema/connectivity, then sync all exported tables
	docker compose -f ../infra/docker-compose.yml up -d db
	$(MAKE) backend-db-check
	$(MAKE) backend-schema
	$(MAKE) up
	$(MAKE) load-backend

assert-load-atomic:  ## [Stage 5 gate] prove a failed load leaves serving data unchanged
	pipeline/scripts/assert_load_atomic.sh

assert-serving:  ## [Stage 5 gate] verify the loaded output against the nine fixtures
	$(PSQL) < pipeline/scripts/assert_serving.sql


## Sprint 3 — decision outputs after Stage 5

build-forecasts: env-check  ## [Sprint 3] write forecast-ready LCL snapshots to kea.bill_forecast
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 build_bill_forecasts.py $(ARGS)

# The recommendation and acceptance jobs import the API's pricing engine from
# a read-only mount, so tariff arithmetic has one implementation.
recommend-plans: env-check  ## [Sprint 3] rank 3 plans x 3 terms and store three bill examples per move
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro \
	  -v $(PWD)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api \
	  -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 build_plan_recommendations.py $(ARGS)

verify-plan-bills: env-check  ## [Sprint 3 gate] reproduce one issued bill per eligible account within $1
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro \
	  -v $(PWD)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api \
	  -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 verify_bill_reproduction.py $(ARGS)

assess-hardship: env-check  ## [Sprint 3] write explainable LCL hardship assessments and signals
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro \
	  -v $(PWD)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api \
	  -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 build_hardship_assessments.py $(ARGS)

create-hardship-alerts: env-check  ## [Sprint 3] create pending in-app outreach; never sends messages
	$(DOCKER_RUN) -v $(PWD)/pipeline:/opt/pipeline:ro \
	  -w /opt/pipeline/scripts \
	  dataeng/dbt:1.12.0 python3 create_hardship_alerts.py $(ARGS)


## Stage 6 — Airflow orchestration

backfill-pipeline:  ## [Stage 6] rerun kea_daily_pipeline over FROM=... TO=...
	pipeline/scripts/backfill_pipeline.sh $(FROM) $(TO)


## Stage 7 — runtime quality and observability gates

assert-quality:  ## [Stage 7 gate] prove quarantine, alerts, blocked export and lineage
	pipeline/scripts/assert_quality.sh

soda-scan: env-check  ## [Stage 7 gate] scan Snowflake freshness, volume and orphan rate
	$(SODA) soda scan -d kea -c configuration.yml -v load_date=$(LOAD_DATE) checks/warehouse.yml

soda-scan-serving: env-check  ## [Stage 7 report] scan serving Postgres after loading
	$(SODA) soda scan -d kea_serving -c configuration.yml checks/serving.yml


## Stage 8 — developer tests and CI checks

test-transform:  ## [Stage 2 unit test] test UK-to-NZ calendar conversion without infrastructure
	python3 -m unittest discover -s $(GEN)/tests

test-parse:  ## [Stage 3 unit test] test extraction windows, reshaping and contracts
	python3 -m unittest discover -s pipeline/tests -t pipeline/tests

lint:  ## [Stage 8] run Ruff over pipeline Python
	ruff check pipeline/

test:  ## [Stage 8] run the complete pipeline unit-test suite with pytest
	pytest -q pipeline/tests/


## Stage 9 — streaming profile

up-streaming: env-check  ## [Stage 9] start batch stack plus Kafka, Schema Registry and Flink
	docker compose --profile streaming up -d


## Dashboards — grouped by the stage or sprint they demonstrate

# Metabase stores connections and questions in its own application database;
# setup and every builder are safe to rerun.
metabase-setup: env-check  ## [Analysis setup] create admin and connect the serving database
	python3 analysis/metabase/metabase_setup.py

metabase-dashboard: env-check  ## [Stage 0 / Sprint 2] rebuild the nine-household dashboard
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

stage5-dashboard: env-check  ## [Stage 5] rebuild export/load/serving dashboard
	python3 analysis/metabase/build_stage5_dashboard.py

# Requires at least one recommendation and hardship generation.
plans-dashboard: env-check  ## [Sprint 3] rebuild plans, bill examples, hardship and alerts dashboard
	python3 analysis/metabase/build_plans_dashboard.py

verify-dashboards:  ## [Dashboard gate] execute every card and verify data, axes and layout
	python3 analysis/metabase/verify.py


## Operations and destructive commands

ps:  ## show running pipeline containers
	docker compose ps

logs:  ## follow pipeline container logs
	docker compose logs -f

psql:  ## open an interactive shell on the serving database
	docker compose exec kea-serving-db psql -U kea -d kea

down:  ## stop batch and streaming containers; keep volumes
	docker compose --profile streaming down

clean:  ## DESTRUCTIVE: stop containers and delete this project's volumes
	docker compose --profile streaming down -v
