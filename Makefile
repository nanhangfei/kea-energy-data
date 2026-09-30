# data/Makefile — entry points for the Data team's pipeline.
#
# Start with `make help`: the commands, then the common paths.
#
# THE STEPS, in the order they run -- one command each, each safe to repeat:
#
#   Stack          up                 the local batch stack
#   Sources        init-sources       the source systems: nine fixtures, then the LCL cohort
#   Lake           ingest-date        one load date to bronze and parsed
#   Warehouse      build-warehouse    RAW, dbt, then the Soda warehouse gate
#   Serving        publish-serving    export, one release, then the serving gate and report
#   Analysis       dashboards         Metabase over what the steps produced
#                  evaluate-sprint4   Sprint 4 scored against the planted answers
#                  backtest-reproduce, backtest-forecast
#                                     the offline forecast backtest (Botman et al. 2025)
#   Delivery       ci, deploy         check a change, then ship it to this machine's stack
#   Orchestration  run-pipeline       Lake, Warehouse and Serving again, as Airflow runs
#                                     them -- after Delivery, which puts DAG changes live
#   (streaming)    up STREAMING=1     Kafka and Flink, beside the batch stack
#
# rebuild-all is every step from Stack to Analysis, in sequence.
#
# AIRFLOW USES THE SAME NAMES. The four kea_extract_* DAGs are the Lake step
# split by source system, tagged `ingest-date`; kea_daily_pipeline groups its
# tasks as `build-warehouse`, `publish-serving` and `dashboards` (the Lake
# dashboard's rollups); kea_forecast_refresh is the first two for one mart.
# Each command runs the same scripts as its tasks, in the same order.
#
# OLDER COMMITS AND CODE COMMENTS SAY "STAGE N", numbered in the order the
# platform was built: 0 Stack, 1-2 Sources, 3 Lake, 4 Warehouse, 5 Serving,
# 6 Orchestration, 7 quality gates, 8 Delivery, 9 streaming. The quality gates
# have no step of their own: the Soda warehouse scan ends Warehouse,
# assert_serving and the Soda serving report end Serving, and the quality proof
# is `run-pipeline VERIFY=1` -- where the DAG runs them.
#
# The options are the same words everywhere: LOAD_DATE; FORCE=1 rewrites sealed
# work; REGEN=1 rewrites tracked derived files; VERIFY=1 runs the step's
# deliberate proofs; ONLY= picks some of a command's parts. RESEED, RESET_RAW,
# MIGRATE, ROLES and DEMO_RESET are the destructive or owner-only opt-ins, and
# nothing passes one unless asked.
.DEFAULT_GOAL := help

.PHONY: help env-check \
	rebuild-all \
	up down clean logs ps psql \
	init-sources ingest-date build-warehouse publish-serving _publish-release run-pipeline \
	ci ci-lint-test ci-migrations ci-story-guard ci-dbt-parse ci-serving-db ci-dags ci-backtest-tests deploy \
	dashboards evaluate-sprint4 backtest-reproduce backtest-forecast

# DockerOperator bind mounts are resolved by the HOST daemon, not inside
# Airflow, so it needs this directory's path as the host sees it.
export HOST_PROJECT_DIR := $(CURDIR)
export COMPOSE_NETWORK  := kea_default

GEN := pipeline/scripts/gen_source

# The images, named once -- the same constants _kea_common.py gives the DAGs,
# so a version bump is one line here and one there.
DBT_IMAGE     := dataeng/dbt:1.12.0
SODA_IMAGE    := dataeng/soda:latest
AIRFLOW_IMAGE := dataeng/airflow:3.3.0
PSQL_IMAGE    := postgres:17.10

# A load date is a NEW ZEALAND calendar day, never a UTC day, and yesterday by
# default. The rule is kea_pipeline.window.load_date_for_fire_time -- the one
# the DAGs call -- called here too rather than written a second time, because
# two copies of what a load date means would drift. And COMPUTED ONCE: a
# recursive `?=` re-ran it at every mention, so a run that crossed NZ midnight
# could extract one date and gate the next.
ifeq ($(origin LOAD_DATE),undefined)
LOAD_DATE := $(shell python3 -c "import sys, datetime as dt; sys.path.insert(0, '$(CURDIR)/pipeline/lib'); from kea_pipeline import window; print(window.load_date_for_fire_time(dt.datetime.now(dt.timezone.utc)))")
endif

# LOAD_DATE only when a person or a caller GAVE one, empty otherwise. The steps
# that write a date somewhere nothing else can undo -- staging RAW, the quality
# proof's DagRun -- act on this rather than on the yesterday default, so the
# default is never written by accident: on the demo layout RAW holds one
# generation, and a nightly date staged on top of it duplicates rows.
EXPLICIT_LOAD_DATE = $(if $(filter command line environment,$(origin LOAD_DATE)),$(LOAD_DATE))

# THE OPTIONS, and the commands that read them. Every one is off or empty unless
# given on the command line; ⚠ marks the destructive and owner-only ones.
#
#   LOAD_DATE=D       ingest-date, build-warehouse,  the NZ day (default yesterday;
#                     run-pipeline                   staged only when given)
#   FROM_DATE=D       ingest-date                    FROM_DATE..LOAD_DATE in one
#                                                    partition: the demo generation
#   FROM=D TO=D       run-pipeline                   each date of a range
#   RELEASE=id        publish-serving                the release to publish; required
#   FORCE=1           ingest-date                    rewrite sealed partitions
#   REGEN=1           ingest-date, build-warehouse   rewrite tracked derived files
#   VERIFY=1          ingest-date, publish-serving,  the step's deliberate proofs
#                     run-pipeline
#   ONLY=...          dashboards, ci                 some of the command's parts
#   BUILD=1           up                             rebuild every image
#   STREAMING=1       up                             add Kafka and Flink
#   EVALUATE=1        rebuild-all                    re-score Sprint 4; rewrites
#                                                    two committed files
#   COHORT_SIZE=n     init-sources                   households to generate
#   RESEED=1      ⚠   init-sources                   replace the nine fixtures
#   RESET_RAW=1   ⚠   build-warehouse                drop every RAW table first
#   MIGRATE=1     ⚠   publish-serving                apply pending migrations (owner)
#   ROLES=1       ⚠   publish-serving                set the login passwords (owner)
#   DEMO_RESET=1  ⚠   publish-serving                clear decision history; this
#                                                    machine's kea-db only
#   GATE_ARGS, MIGRATE_ARGS, EVALUATE_ARGS           passed through to assert_bronze,
#                                                    migrate_serving, evaluate_sprint4
RELEASE      ?=
FROM_DATE    ?=
FROM         ?=
TO           ?=
FORCE        ?=
REGEN        ?=
VERIFY       ?=
ONLY         ?=
BUILD        ?=
STREAMING    ?=
EVALUATE     ?=
COHORT_SIZE  ?= 50
RESEED       ?=
RESET_RAW    ?=
MIGRATE      ?=
ROLES        ?=
DEMO_RESET   ?=
GATE_ARGS    ?=
MIGRATE_ARGS ?=
EVALUATE_ARGS ?=

# The flags those options become. FORCE=1 rather than a raw flag, so one
# spelling reaches the Spark jobs and the scripted extractor alike; every one
# of them treats a sealed partition as immutable without it.
FORCE_ARG = $(if $(filter 1,$(FORCE)),--force,)
FROM_ARG  = $(if $(FROM_DATE),--from-date $(FROM_DATE),)
FULL_ARG  = $(if $(FROM_DATE),--full-incremental,)

# The demo generation's span: the cohort's first NZ day to the day after the
# span ends, from the uk_to_nz constants the generator itself uses, so it moves
# when they do. Recursive (=), so only rebuild-all pays for the python.
DEMO_DATES = $(shell python3 -c "import datetime as dt,sys; sys.path.insert(0,'$(GEN)'); import uk_to_nz as tz; print(tz.nz_local_date_for(tz.LCL_START, tz.derive_shift_days()), tz.NZ_SPAN_END + dt.timedelta(days=1))")
DEMO_FROM  = $(word 1,$(DEMO_DATES))
DEMO_TO    = $(word 2,$(DEMO_DATES))

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

# The same OpenLineage target the DAGs give their containers (_kea_common's
# LINEAGE_ENV), so a step run by hand lands in the same Marquez graph as the
# nightly one. An unreachable Marquez is a warning, never a failure.
LINEAGE_ENV = -e OPENLINEAGE_URL=http://marquez:5000 -e OPENLINEAGE_NAMESPACE=kea_energy

DBT    = $(DOCKER_RUN) -v $(CURDIR)/pipeline/dbt_project:/dbt -w /dbt $(DBT_IMAGE) dbt
DBT_OL = $(DOCKER_RUN) $(LINEAGE_ENV) -v $(CURDIR)/pipeline/dbt_project:/dbt -w /dbt $(DBT_IMAGE) dbt-ol
SODA   = $(DOCKER_RUN) -v $(CURDIR)/pipeline/soda:/soda -w /soda $(SODA_IMAGE)
# The pipeline scripts: pipeline/ read-only. The serving scripts need all of
# data/ (the publish refuses a database behind migrations/serving/), and the
# publish the API's pricing engine as well, read-only, as the DAG mounts it.
PIPELINE_RUN = $(DOCKER_RUN) -v $(CURDIR)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts
SERVING_RUN  = $(DOCKER_RUN) -v $(CURDIR):/opt/data:ro -w /opt/data/pipeline/scripts
PUBLISH_RUN  = $(SERVING_RUN) -v $(CURDIR)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api
SPARK_SUBMIT = docker compose exec -T spark-master /opt/spark/bin/spark-submit \
  --master spark://spark-master:7077
# The pipeline's own Postgres: the four source systems and the audit schemas.
PSQL = docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea
# The backend's database, from a throwaway psql. The DSN variable is expanded
# INSIDE the container, from --env-file, so no credential is on a command line.
SERVED_PSQL = docker run --rm -i --network $(COMPOSE_NETWORK) --env-file .env $(BACKEND_HOST) \
  $(PSQL_IMAGE) sh -c


## Start here — every step, Stack to Analysis, in one command

help:  ## show the commands, then the common paths through them
	@awk 'BEGIN{FS=":.*?## "} /^## /{printf "\n\033[1m%s\033[0m\n", substr($$0,4); next} /^[a-z0-9][a-z0-9-]*:.*?## /{printf "  \033[36m%-22s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)
	@printf '%s\n' '' \
	  'COMMON PATHS' \
	  '  First run, and after data, tariff or pipeline changes -- start kea-db (../infra)' \
	  '  first; on a first run also cp .env.example .env, fill it in, and run' \
	  '  snowflake/bootstrap.sql once, by hand:' \
	  '    make rebuild-all                          # EVALUATE=1 also re-scores Sprint 4' \
	  '  After a merge to main, from a clean checkout of it (commit or stash first):' \
	  '    git switch main && git pull --ff-only && make deploy; git switch -' \
	  '  Dashboards only:           make dashboards   # ONLY=<name> for one' \
	  '  Republish only:            make publish-serving RELEASE=<id>   # MIGRATE=1 if migrations moved' \
	  '  Re-score Sprint 4 only:    make evaluate-sprint4, then review and commit results/' \
	  '  One load date, on a per-date layout -- never on the demo layout:' \
	  '    make ingest-date LOAD_DATE=D && make build-warehouse LOAD_DATE=D' \
	  '    make publish-serving RELEASE=<id>' \
	  '    make run-pipeline LOAD_DATE=D             # Lake to Serving, through Airflow instead'

env-check:
	@[ -f .env ] || { echo ".env is missing — cp .env.example .env and fill it in."; exit 1; }

# EVERY STEP, STACK TO ANALYSIS, as the step commands and nothing else, so the demo
# passes every gate a single load date does. Recursive makes, so `make -j`
# cannot start a step before the one it needs. Every step is safe to repeat and
# creates whatever it finds missing, so this is also the first-run command.
#
# The demo is one GENERATION: every arrival from DEMO_FROM to DEMO_TO in one
# partition, staged into RAW alone -- the demo layout. Four sets of options
# make the stage commands build it:
#
#   FROM_DATE     the lake step extracts the whole span into ONE partition, so Spark
#                 starts once rather than twice for each of 757 dates
#   FORCE=1       the source was just regenerated, so the sealed demo partition
#                 is stale by definition
#   RESET_RAW=1   RAW is dropped, and holds this one generation afterwards
#   MIGRATE=1 ROLES=1 DEMO_RESET=1
#                 the served schema brought current, the two login roles'
#                 passwords set, and the old decision history cleared -- on
#                 this machine's kea-db only, which is checked FIRST, so a
#                 remote DSN fails in seconds, not half an hour in
#
# VERIFY is passed empty: the lake step's replay would re-extract one day over the
# demo partition, and no proof is part of a rebuild. One opt-in:
#
#   EVALUATE=1    re-score Sprint 4 against the answers this generation
#                 planted, after the publish. Opt-in because it rewrites two
#                 COMMITTED files, for you to review and commit
#
# The per-date, production-shaped rebuild after a span move is
# ops/rebuild_span.sh.
rebuild-all: env-check  ## every step, Stack to Analysis: one 50-household demo generation through Sources, Lake, Warehouse and Serving, every gate, the dashboards; safe to repeat, the first run too; target under 30 min, EVALUATE=1
# 0 THIS MACHINE'S kea-db, or nothing: step 5 clears its decision history.
	pipeline/scripts/ops/assert_local_serving.sh

# 1 STACK: building any image that is missing.
	$(MAKE) up

# 2 SOURCES: the nine fixtures and a freshly generated cohort, gated.
	$(MAKE) init-sources

# 3 LAKE: the whole span as one partition, rewritten, then the bronze gate.
	$(MAKE) ingest-date LOAD_DATE=$(DEMO_TO) FROM_DATE=$(DEMO_FROM) FORCE=1 VERIFY=

# 4 WAREHOUSE: RAW dropped and restaged from that partition, dbt, the Soda gate.
	$(MAKE) build-warehouse LOAD_DATE=$(DEMO_TO) RESET_RAW=1

# 5 SERVING: export and publish a new demo release, then its gate and report.
	$(MAKE) publish-serving RELEASE=demo-$(shell date +%Y%m%dT%H%M%S) MIGRATE=1 ROLES=1 DEMO_RESET=1 VERIFY=

# 6 SPRINT 4, only with EVALUATE=1: re-score it over what step 5 published.
	$(if $(filter 1,$(EVALUATE)),$(MAKE) evaluate-sprint4)

# 7 ANALYSIS: every dashboard, with the rollups they read, every card checked.
	$(MAKE) dashboards ONLY=


## Stack — the local batch stack

# ONE target for the stack, and it is safe to repeat. dbt and Soda have their own
# images because their dependency pins conflict with Airflow's; compose builds
# the rest. Each is built only when it is MISSING, so an everyday `up` costs
# seconds and a fresh clone still starts. BUILD=1 rebuilds every image -- after
# a Dockerfile or pinned-version change, and on every deploy.
#
# STREAMING=1 adds the streaming profile -- Kafka, Schema Registry and two
# Flink nodes -- beside the batch stack. That is all of it to
# run until Sprint 5: nothing produces or consumes yet. `down` and `clean` stop it too.
up: env-check  ## start the batch stack, building any missing image; safe to repeat, BUILD=1 rebuilds them all, STREAMING=1 adds Kafka and Flink
	@for image in $(DBT_IMAGE)=pipeline/docker/dbt $(SODA_IMAGE)=pipeline/docker/soda; do \
	    tag=$${image%%=*}; context=$${image#*=}; \
	    if [ "$(BUILD)" = "1" ] || ! docker image inspect $$tag >/dev/null 2>&1; then \
	      docker build -t $$tag $$context || exit 1; \
	    fi; \
	  done
	docker compose $(if $(filter 1,$(STREAMING)),--profile streaming,) up -d $(if $(filter 1,$(BUILD)),--build,)
	@echo ""
	@echo "  airflow  http://localhost:8080  (admin / AIRFLOW_ADMIN_PASSWORD)"
	@echo "  minio    http://localhost:9001    spark    http://localhost:8082"
	@echo "  history  http://localhost:18080  (completed Spark jobs and DAGs)"
	@echo "  marquez  http://localhost:3000    grafana  http://localhost:3001"
	@echo "  metabase http://localhost:3002    pipeline postgres://kea@localhost:5433/kea  (sources)"
	@echo "  served tables: the backend's kea-db, localhost:5432 (../infra; not started here)"


## Sources — nine fixtures, then the generated cohort

# ONE target for the sources, because together they are one thing: the four source systems as the extracts will find them.
# Safe to repeat -- the schema
# is applied only when missing, the fixtures seeded only when absent, nothing
# is downloaded twice, the generators are deterministic, and the load clears
# the previous cohort before it writes. A failing step stops the chain, so a
# half-loaded cohort is never left behind un-gated.
#
#   RESEED=1         after editing a fixture: replace the nine fixtures' rows.
#                    The generated rows hang foreign keys off them and go in
#                    the same transaction -- and step 5 loads them straight
#                    back, which is what running the two stages as one bought
#   COHORT_SIZE=500  the archived full-scale profile instead of 50 households
#
# The serving DDL is not applied here: the served tables live in the backend's
# database, created by `make publish-serving MIGRATE=1` from migrations/serving.
# design/kea_serving_schema.sql stays the readable reference for them.
#
# ⚠️ EDITING A FIXTURE'S WEATHER NEEDS THE WAREHOUSE STEP AS WELL. The temperatures in
# sample/*.sql also feed a dbt seed, regenerated by `make build-warehouse
# REGEN=1` -- the warehouse is what reads it -- and every plain build-warehouse fails
# until you do. This target does not run it.
init-sources: env-check  ## source schema, nine fixtures, LCL cohort and NIWA weather, then the gate; safe to repeat, RESEED=1, COHORT_SIZE (default 50)
# 1 SCHEMA, only when missing: design/kea_source_schema.sql is plain CREATE
#   TABLE, so a second apply would error.
	@$(PSQL) -tAc "select to_regclass('src_metering.interval_reads') is not null" \
	  | grep -q t && echo "source schema: already applied" \
	  || { echo "source schema: applying design/kea_source_schema.sql"; \
	       $(PSQL) < design/kea_source_schema.sql; }

# 2 FIXTURES, only when absent: `--ensure` makes "already loaded" a success
#   rather than the refusal a bare seed gives. RESEED=1 replaces them.
	pipeline/scripts/ops/seed_fixtures.sh $(if $(filter 1,$(RESEED)),--reseed,--ensure)

# 3 ACQUIRE, in cost order. The 1.2 G archive is fetched only when neither the
#   extracted blocks nor the zip is on disk, and the unzip only when the blocks
#   are not -- which also keeps KAGGLE_* out of the common path, since
#   fetch_lcl.sh exits on a missing .env BEFORE checking whether the archive is
#   already there. To re-fetch deliberately: `$(GEN)/fetch_lcl.sh --force`,
#   which keeps the as-received file and writes a new timestamped one beside it.
	@test -d raw/lcl/extracted || test -f raw/lcl/smart-meters-in-london.zip || \
	  $(GEN)/fetch_lcl.sh
	@test -d raw/lcl/extracted || (echo "extracting half-hourly blocks..." && \
	  unzip -o -j -q raw/lcl/smart-meters-in-london.zip \
	    'halfhourly_dataset/halfhourly_dataset/block_*.csv' -d raw/lcl/extracted/ && \
	  unzip -o -j -q raw/lcl/smart-meters-in-london.zip \
	    'informations_households.csv' -d raw/lcl/extracted/)

# 4 GENERATE. These write CSVs and touch no database at all, which is what lets
#   the load happen in one window at the end. --stats measures the scale factor
#   into scale.json first, on LCL alone; generation refuses to start without it.
	python3 $(GEN)/cohort.py --size $(COHORT_SIZE)
	python3 $(GEN)/gen_metering.py --stats
	python3 $(GEN)/gen_metering.py
	python3 $(GEN)/gen_reference.py
	python3 $(GEN)/gen_billing.py

# 5 LOAD. 00_clean_generated removes the previous run's rows while KEEPING the
#   nine fixtures -- they share src_metering.networks, and a fixture's own
#   DELETE fails while generated rows reference it. load_source.sh then loads
#   the new ones and deletes the interval_reads it replaces.
	$(PSQL) -q -f - < $(GEN)/00_clean_generated.sql
	$(GEN)/load_source.sh

# 6 WEATHER, and it is the one source nothing fetches. load_niwa_weather.py
#   TRUNCATEs and reloads, so repeating is fine, but the DataHub licence is
#   accepted per dataset BY A PERSON -- so raw/niwa is often empty on a fresh
#   clone, and missing zips skip with a note rather than failing a rebuild that
#   has nothing to do with weather.
	@if ls raw/niwa/*.zip >/dev/null 2>&1; then \
	    python3 $(GEN)/load_niwa_weather.py; \
	  else \
	    echo "src_weather: no NIWA zips in raw/niwa -- skipped"; \
	  fi

# 7 THE GATE. 16 numbered checks, 24 with their sub-checks, including that the
#   nine fixtures survived the run and that every identity sequence is ahead of
#   its table -- both of which exist because the generator broke them once.
	$(PSQL) -f - < $(GEN)/99_assert.sql


## Lake — one load date to bronze and parsed

# ONE target for the lake, and it is safe to repeat, because every step declines
# to redo work that is already sealed:
#
#   roles      ALTER ROLE ... PASSWORD, the same password every time
#   contracts  --check only: it fails on drift and writes nothing. Regenerating
#              rewrites tracked .avsc files, which REGEN=1 asks for
#   extract    "_SUCCESS present and --force not given; nothing to do" -- the
#              Spark lane and each of the sixteen scripted entities alike
#   gate       read-only: assert_isolation's 20 checks, then assert_bronze's on
#              the partition this date names -- what this command answers for.
#              The whole lake is `spark-submit assert_bronze.py` with no date
#
# A second run therefore costs the gate and nothing else. The opt-ins:
#   FORCE=1    rewrite this load date's partitions, after the source changed
#              under them -- the gate says so when it has
#   VERIFY=1   the lake's three deliberate proofs, after the gate: the replay
#              (re-extracts the date and compares content hashes -- the one
#              step here that is not free to repeat), the file-level
#              conformance test, and the billing-isolation demonstration
#   FROM_DATE= the demo generation: every arrival from FROM_DATE to LOAD_DATE
#              in ONE partition at LOAD_DATE, the small incremental tables
#              whole. For `make rebuild-all`; never with VERIFY=1, whose
#              replay re-extracts a single day over it
#
# The four extract DAGs run steps 3 and 4 nightly, through the same Spark jobs
# and extract_small. The roles, the contract check and the gate are this
# command's own: Airflow holds its credentials as connections, and the Soda
# warehouse gate is where the nightly path checks what landed.
ingest-date: env-check  ## ONE NZ load date to bronze+parsed, all seventeen entities, then the gate; safe to repeat, LOAD_DATE=YYYY-MM-DD (default yesterday), FROM_DATE=, FORCE=1, VERIFY=1, REGEN=1
	@if [ -n "$(FROM_DATE)" ] && [ "$(VERIFY)" = "1" ]; then \
	    echo "VERIFY=1 with FROM_DATE would replay one day over the multi-day partition; run them apart"; \
	    exit 2; \
	  fi

# 1 CREDENTIALS. The four kea_extract_* roles are created with LOGIN and no
#   password, deliberately, so nothing works until they are set out of band.
	pipeline/scripts/ops/set_extract_roles.sh

# 2 CONTRACTS. Checked by default, never rewritten: the sixteen derived .avsc
#   files are TRACKED, so a routine ingest that rewrote them would leave the
#   tree dirty and `make deploy` refuses a dirty tree. The check is what
#   catches the failure that matters -- a contract drifted from the table it
#   describes makes the conformance check certify nothing.
#
#   REGEN=1 regenerates them instead, and then ingests the date with the new
#   contracts, because a source-schema change needs both: new shapes AND a
#   fresh extract that conforms to them. Review and commit what it writes.
	@if [ "$(REGEN)" = "1" ]; then \
	    pipeline/scripts/lake/gen_avsc.sh; \
	  else \
	    pipeline/scripts/lake/gen_avsc.sh --check; \
	  fi

# 3 THE SPARK LANE -- interval_reads alone (design §5.1). Two steps, and the
#   edge between them is a data dependency: parse reads the partition extract
#   just wrote, which is why there is no sleep anywhere in the chain.
	$(SPARK_SUBMIT) /opt/spark_jobs/extract_interval_reads.py $(LOAD_DATE) $(FROM_ARG) $(FORCE_ARG)
	$(SPARK_SUBMIT) /opt/spark_jobs/parse_interval_reads.py $(LOAD_DATE) $(FROM_ARG) $(FORCE_ARG)

# 4 THE SCRIPTED LANE -- the other sixteen entities, extract and parse in one
#   process each, all in one container. Spark would spend longer starting up
#   than reading a 73-row table.
	$(PIPELINE_RUN) $(DBT_IMAGE) \
	  python3 lake/extract_all_small.py $(LOAD_DATE) $(FORCE_ARG) $(FULL_ARG)

# 5 THE GATE, both halves and in this order: isolation first (20 checks) because
#   a role that can read another system's tables invalidates what bronze then
#   claims, and then the lake's own checks on this date's partition, over the
#   window its manifest records -- one day, or the demo generation's span.
	pipeline/scripts/gates/assert_isolation.sh
	$(SPARK_SUBMIT) /opt/spark_jobs/assert_bronze.py --load-date $(LOAD_DATE) $(GATE_ARGS)

# 6 THE PROOFS, opt-in, and each one breaks or repeats something on purpose.
#   The replay is the lake's second exit condition (see Lake in the stack
#   doc) -- "re-running a load date changes nothing" -- and proving it means
#   re-extracting the date. The
#   conformance test writes its own fixture partition, at load_date=2000-01-01,
#   which the gate never sees. The isolation demo breaks the billing credential,
#   shows metering unaffected, and restores it on exit -- on a probe date of
#   its own, because on a sealed date no task connects and nothing is shown.
	@if [ "$(VERIFY)" = "1" ]; then \
	    pipeline/scripts/gates/assert_idempotent.sh $(LOAD_DATE) && \
	    $(SPARK_SUBMIT) /opt/spark_jobs/tests/lake/test_lake_conformance.py && \
	    pipeline/scripts/ops/demo_independence.sh; \
	  fi


## Warehouse — RAW, dbt and the Soda gate

# ONE target for the warehouse, and it is safe to repeat, because every write in it
# replaces what an earlier run wrote rather than adding to it:
#
#   connection  dbt debug, read-only
#   packages    dbt deps installs what package-lock.yml pins
#   seed        --check only: it fails on a stale fixture-weather csv and
#               writes nothing. Regenerating rewrites a tracked file, which
#               REGEN=1 asks for
#   load        a snapshot entity is TRUNCATE-and-reload; an incremental one
#               deletes this load date's own rows before appending them, so
#               loading a date again is a net no-op and touches no other date
#   build       seeds reload whole, table models rebuild from RAW, and the four
#               key maps are append-only: a re-run meets no new business key,
#               so it mints no id. The orphan streak counts metering load
#               dates, not runs, so a rebuild over the same RAW moves no count
#   gate        the Soda warehouse scan, read-only
#
# Steps 6-8 are kea_daily_pipeline's dbt_run, dbt_test and soda_scan_warehouse,
# as the same commands in the same order. Run and test are separate there so
# the grid says which half failed; here they are separate so both paths build
# exactly the same way.
#
# RAW IS STAGED ONLY WHEN LOAD_DATE IS GIVEN (EXPLICIT_LOAD_DATE). Without it the
# target builds over what RAW already holds, which is what a deploy and a
# republish need, and the Soda gate asks about the latest date RAW holds.
#
#   RESET_RAW=1  drop every RAW table first, so RAW holds only the date staged
#                next -- the schema-evolution boundary a rebuild crosses. Only
#                with LOAD_DATE: without one, RAW would be left empty
build-warehouse: env-check  ## RAW, dbt seed/run/test, then the Soda warehouse gate; safe to repeat, LOAD_DATE= stages that date first, RESET_RAW=1, REGEN=1
	@if [ "$(RESET_RAW)" = "1" ] && [ -z "$(EXPLICIT_LOAD_DATE)" ]; then \
	    echo "RESET_RAW=1 needs LOAD_DATE: it would leave RAW empty"; exit 2; \
	  fi

# 1 CONNECTION, before anything writes. The loader carries on past a failed
#   entity, so a bad key-pair would otherwise surface as seventeen failures
#   rather than this one.
	$(DBT) debug --profiles-dir .

# 2 PACKAGES, also before anything writes: a failed download then leaves RAW
#   untouched rather than newer than the models built from it.
	$(DBT) deps

# 3 FIXTURE WEATHER SEED. Checked by default, never rewritten, for the reason
#   the lake's contracts are: seeds/fixture_weather_daily.csv is TRACKED, and a
#   deploy builds exactly what main holds -- one that regenerated it would, on
#   a main whose fixtures moved without their seed, build a csv main does not
#   have. The check is what catches the failure that matters: mart_daily_usage
#   would explain a fixture's bill against weather its expected answer was not
#   computed from (#143). It loads sample/*.sql into a throwaway Postgres and
#   never touches kea-source-db.
#
#   REGEN=1 regenerates it instead, then builds with it -- the step after
#   editing a fixture's weather. Review and commit what it writes.
	@if [ "$(REGEN)" = "1" ]; then \
	    pipeline/scripts/ops/gen_fixture_weather.sh; \
	  else \
	    pipeline/scripts/ops/gen_fixture_weather.sh --check; \
	  fi

# 4 RESET, only with RESET_RAW=1: every RAW table dropped, so the load below
#   recreates each from its current contract.
	@if [ "$(RESET_RAW)" = "1" ]; then \
	    $(PIPELINE_RUN) $(DBT_IMAGE) python3 warehouse/reset_raw.py; \
	  fi

# 5 LOAD, only when LOAD_DATE was given. All seventeen entities are attempted,
#   a missing partition is a clean skip (weather is monthly), and any failure
#   makes the loader exit non-zero -- which stops make here, so dbt never
#   builds over a half-staged RAW.
	@if [ -n "$(EXPLICIT_LOAD_DATE)" ]; then \
	    $(PIPELINE_RUN) $(DBT_IMAGE) \
	      python3 warehouse/load_all_bronze_to_snowflake.py $(EXPLICIT_LOAD_DATE); \
	  else \
	    echo "KEA.RAW: no LOAD_DATE given -- building over what it already holds"; \
	  fi

# 6 BUILD, as the DAG's dbt_run: seeds first, because `dbt run` does not load
#   them and dim_icp / mart_daily_usage join two -- the fixtures' own weather
#   and the region-to-station map -- then every model, through dbt-ol so the
#   lineage lands in Marquez.
	$(DBT) seed --profiles-dir .
	$(DBT_OL) run --profiles-dir .

# 7 TEST, as the DAG's dbt_test: every data test and every unit test, over the
#   models step 6 built.
	$(DBT_OL) test --profiles-dir .

# 8 THE GATE, as the DAG's soda_scan_warehouse: the staged date reached RAW,
#   volume, cohort coverage and the orphan rate. What dbt_test cannot ask --
#   whether the data the models were built from is plausible.
	$(SODA) soda scan -d kea -c configuration.yml -v load_date=$(EXPLICIT_LOAD_DATE) checks/warehouse.yml


## Serving — export, then publish into the backend's database

# ONE target for serving, and it is safe to repeat: a release id is published
# once. It is asked first, so a release that is already out costs no export
# and changes nothing -- and the gate and the report still run, because "is
# what is served sound" is worth asking again.
#
# The steps are kea_daily_pipeline's export, publish, assert_serving and
# soda_scan_serving, in that order. The owner steps the DAG never takes --
# Airflow holds no owner login -- are opt-ins here:
#
#   MIGRATE=1     apply pending migrations/serving first. A publish never
#                 migrates as a side effect; without it, a schema that is
#                 behind fails at step 3, before the export.
#                 MIGRATE_ARGS=--baseline records V001 on an existing database
#                 instead -- see migrations/serving/README.md first
#   ROLES=1       set the kea_loader/kea_app passwords from .env: once, when a
#                 target database is new. Never in a deploy
#   DEMO_RESET=1  this machine's kea-db only: clear the old decision history
#                 right before the publish recreates it. rebuild-all passes
#                 it; a shared database never gets it
#   VERIFY=1      the atomicity proof, last: a publish planted to fail
#                 changes nothing, on a throwaway Postgres
#
# The Sprint 3 and 4 decision jobs run inside the publish, importing the API's
# pricing engine from the read-only mount so tariff arithmetic has one
# implementation.
publish-serving: env-check  ## export, publish one release, then assert_serving and the Soda serving report; safe to repeat, RELEASE required, MIGRATE=1, ROLES=1, DEMO_RESET=1, VERIFY=1
	@test -n "$(RELEASE)" || { echo 'Set RELEASE to a unique release identifier'; exit 1; }

# 1 SCHEMA, only with MIGRATE=1, as the owner (KEA_MIGRATION_DSN). Idempotent:
#   it applies only what data_ops.schema_migration has not recorded.
	@if [ "$(MIGRATE)" = "1" ]; then \
	    $(SERVING_RUN) $(DBT_IMAGE) python3 serving/migrate_serving.py $(MIGRATE_ARGS); \
	  fi

# 2 SETUP, as the owner, and before step 3 because step 3 connects as
#   kea_loader. ROLES=1 sets the two logins' passwords -- on a new database
#   that login has none until this runs; the same passwords from .env every
#   time. DEMO_RESET=1 is refused here, before any export, unless the database
#   is this machine's: step b below clears history.
	@if [ "$(ROLES)" = "1" ]; then \
	    $(SERVING_RUN) $(DBT_IMAGE) python3 serving/configure_serving_roles.py; \
	  fi
	@if [ "$(DEMO_RESET)" = "1" ]; then pipeline/scripts/ops/assert_local_serving.sh; fi

# 3 STATUS, read-only, as kea_loader: "published" or "pending", and a failure
#   when the schema is behind migrations/serving -- found here, not after ten
#   million rows are exported. Only a pending release is exported and loaded.
	@status=$$($(PUBLISH_RUN) $(DBT_IMAGE) \
	    python3 serving/publish_serving.py --release-id '$(RELEASE)' --status) || exit 1; \
	  if [ "$$status" = "pending" ]; then \
	    $(SERVING_WRITES); \
	  else \
	    echo "$(RELEASE): already published -- nothing exported or loaded; checking what is served"; \
	  fi

# 4 THE GATE, as the DAG's assert_serving: the published output against the
#   nine fixtures, as kea_loader, inside a transaction it rolls back -- the
#   checks build temporary helpers, which a read-only session refuses, and none
#   of it may persist.
	$(SERVED_PSQL) 'exec psql -X -v ON_ERROR_STOP=1 -c BEGIN -f - -c ROLLBACK "$$KEA_PUBLISH_DSN"' \
	  < pipeline/scripts/gates/assert_serving.sql

# 5 THE REPORT, as the DAG's soda_scan_serving, and LAST: the rows are already
#   served, so it can stop nothing, and after the gate it cannot stop the gate
#   either. As kea_app, the agent's read-only role, so it sees what the
#   application is allowed to see. A finding still fails the command.
	$(SODA) soda scan -d kea_serving -c configuration.yml checks/serving.yml

# 6 THE PROOF, only with VERIFY=1: migrates a throwaway Postgres as the
#   deploy would, plants a failure in a publication, and shows nothing changed.
	@if [ "$(VERIFY)" = "1" ]; then pipeline/scripts/gates/assert_load_atomic.sh; fi

# Serving's writes, which publish-serving runs only for a release that is not
# yet published. Not in `make help`: on its own it would skip that check.
#
# Called through a variable, not a literal $(MAKE), on purpose: make runs any
# recipe line that names $(MAKE) directly even under `make -n`, and that line
# would run the status query for real. Indirect, a dry run stays dry.
SERVING_WRITES = $(MAKE) --no-print-directory _publish-release
_publish-release:
# a EXPORT, as the DAG's export_warehouse: every serving table from Snowflake
#   to the lake's export/, each replaced whole and sealed.
	$(PIPELINE_RUN) $(DBT_IMAGE) python3 warehouse/export_warehouse_to_lake.py

# b CLEAR, only with DEMO_RESET=1: the old decision history, as the owner,
#   after the export succeeded and immediately before the publish that
#   recreates it -- so a failed export never leaves the store without them.
	@if [ "$(DEMO_RESET)" = "1" ]; then \
	    $(SERVED_PSQL) 'exec psql -X -q -v ON_ERROR_STOP=1 "$$KEA_MIGRATION_DSN"' \
	      < pipeline/scripts/serving/reset_demo_decisions.sql; \
	  fi

# c PUBLISH, as the DAG's publish_serving: every served table from its sealed
#   export and the decisions computed from them, in ONE transaction, recorded
#   under this release id.
	$(PUBLISH_RUN) $(DBT_IMAGE) python3 serving/publish_serving.py --release-id '$(RELEASE)'


## Analysis — dashboards and the Sprint 4 evaluation

# ONE target for the dashboards, and it is safe to repeat: Metabase keeps its
# connections and questions in its own application database, and the connect
# step and every builder replace what they made last time, found by name.
#
#   connect   the served tables (kea-db, as kea_app) and the pipeline's own
#             database, as Metabase's two data sources
#   rollups   what two dashboards read instead of raw tables, refreshed only
#             when one of them is built: the source views in kea-source-db for
#             `source`, and the lake_audit summary -- Spark over MinIO -- for `lake`
#   build     the dashboards, each by its own builder
#   verify    every card executes, returns rows and uses every column it
#             selects: a publish is not evidence that a panel works
#
# ONLY= rebuilds and verifies just the named ones -- ONLY=story, or
# ONLY="plans sprint4" -- after refreshing only the rollups they read:
#
#   fixtures  the nine households' bills, over the served kea.mart_*    (Serving)
#   source    the generated cohort, over src_*                          (Sources)
#   lake      extract, bronze and parsed                                (Lake)
#   serving   export, load and what crossed, over both databases        (Serving)
#   plans     recommendations, bill examples, hardship and alerts --
#             needs at least one published decision generation          (Serving)
#   story     the one-page walkthrough, identifiers excluded
#   sprint4   detected loads, solar and EV -- what was published beside
#             what the generator planted; identifiers excluded
#   backtest  the forecast backtest (Botman et al. 2025), from the committed
#             results in analysis/forecast_backtest/ -- it reads no table
DASHBOARDS       = fixtures source lake serving plans story sprint4 backtest
BUILDER_fixtures = build_dashboard.py
BUILDER_source   = build_source_dashboard.py
BUILDER_lake     = build_lake_dashboard.py
BUILDER_serving  = build_serving_dashboard.py
BUILDER_plans    = build_plans_dashboard.py
BUILDER_story    = build_story_dashboard.py
BUILDER_sprint4  = build_sprint4_dashboard.py
BUILDER_backtest = build_backtest_dashboard.py
SHOWN            = $(or $(ONLY),$(DASHBOARDS))

dashboards: env-check  ## connect Metabase, refresh the rollups, rebuild the eight dashboards, verify every card; safe to repeat, ONLY=<name ...>
	@for d in $(SHOWN); do case " $(DASHBOARDS) " in *" $$d "*) ;; \
	    *) echo "unknown dashboard '$$d'; one of: $(DASHBOARDS)"; exit 2 ;; esac; done

# 1 CONNECT. Every builder refuses to run until both databases are connected.
	python3 analysis/metabase/metabase_setup.py

# 2 ROLLUPS, only those the chosen dashboards read. The source views are
#   materialised (see the stack doc, 6.5), so nothing refreshes them but this.
#   The lake summary writes as kea_lake_audit, which owns lake_audit and reads
#   no source system -- the same login kea_daily_pipeline's `dashboards` group
#   uses -- so the role is ensured first, as the superuser only make holds.
	$(if $(filter source,$(SHOWN)),$(PSQL) -q -f - < analysis/metabase/source_views.sql)
	$(if $(filter lake,$(SHOWN)),pipeline/scripts/ops/set_lake_audit_role.sh)
	$(if $(filter lake,$(SHOWN)),$(SPARK_SUBMIT) /opt/analysis/metabase/summarise_lake.py)

# 3 BUILD, in the order listed above, stopping at the first that fails.
	$(foreach d,$(SHOWN),python3 analysis/metabase/$(BUILDER_$(d)) &&) true

# 4 VERIFY exactly what was built -- every card, every column.
	python3 analysis/metabase/verify.py $(foreach d,$(SHOWN),$(BUILDER_$(d)))

# The Sprint 4 evaluation: detected loads, the net heating fit, the bill driver
# and the early warning, scored against the answers the generator planted.
#
# ITS OWN COMMAND, and a step of rebuild-all only when asked (EVALUATE=1), for
# three reasons. It needs no Metabase: it reads serving in a READ ONLY
# transaction, the source database's gen_audit and revisions, and two Snowflake
# silver tables. It rewrites two COMMITTED files, results/metrics.csv and
# results/report.md, so running it unasked would leave the tree dirty -- and
# `make deploy` refuses a dirty tree. And it is re-run on its own, after a
# change to the evaluation or its thresholds, over what is already built.
#
# Review and commit the report after it runs. EVALUATE_ARGS passes options
# through (--skip-hardship). The method and thresholds are in
# analysis/sprint4_evaluation/README.md.
evaluate-sprint4: env-check  ## score Sprint 4 against the planted answers; rewrites the committed report in analysis/sprint4_evaluation/results
# 1 THE EVALUATION'S OWN UNIT TESTS, in the image, so a scoring bug stops the
#   run before it rewrites the report.
	$(PUBLISH_RUN) -e PYTHONDONTWRITEBYTECODE=1 $(DBT_IMAGE) \
	  python3 -m unittest discover -s /opt/data/pipeline/tests -t /opt/data/pipeline/tests -p test_evaluate_sprint4.py

# 2 THE SCORE, written to results/ -- the one path this mounts writable.
	mkdir -p analysis/sprint4_evaluation/results
	$(PUBLISH_RUN) -e PYTHONDONTWRITEBYTECODE=1 \
	  -v $(CURDIR)/analysis/sprint4_evaluation/results:/out $(DBT_IMAGE) \
	  python3 ops/evaluate_sprint4.py --out /out $(EVALUATE_ARGS)

# THE FORECAST BACKTEST -- Botman et al. 2025, offline. Each reads the sealed
# lake export and writes only analysis/forecast_backtest/results/, and each runs
# the 43 kNN tests first, so a leakage bug stops it before it scores anything.
# Research, not a pipeline step: nothing downstream reads what it writes. The
# method, the gate and its verdict are in analysis/forecast_backtest/README.md.
# ARGS passes options through (e.g. --variant seasonal).
backtest-reproduce: env-check  ## backtest stage 1: day-ahead kNN quantiles against the paper's baselines
	$(DOCKER_RUN) -v $(CURDIR)/pipeline:/opt/pipeline:ro \
	  -v $(CURDIR)/analysis/forecast_backtest:/out -w /opt/pipeline/scripts \
	  $(DBT_IMAGE) sh -c 'python3 -m unittest discover -s /opt/pipeline/tests -t /opt/pipeline/tests -p test_knn_quantile.py \
	  && python3 backtest_bill_forecast.py reproduce --out /out/results $(ARGS)'

backtest-forecast: env-check  ## backtest stage 2: month-end kNN against hdd-linear-v2, with the gate; one read-only warehouse SELECT
	$(DOCKER_RUN) -v $(CURDIR)/pipeline:/opt/pipeline:ro \
	  -v $(CURDIR)/analysis/forecast_backtest:/out -w /opt/pipeline/scripts \
	  $(DBT_IMAGE) sh -c 'python3 -m unittest discover -s /opt/pipeline/tests -t /opt/pipeline/tests -p test_knn_quantile.py \
	  && python3 backtest_bill_forecast.py monthend --out /out/results $(ARGS)'


## Delivery — CI and deploy

# TWO commands, because they are opposites: `ci` changes nothing and runs in
# GitHub with no credentials; `deploy` changes everything, and runs only here.

# CI's steps, in the order the workflow's three jobs run them. None reads .env
# or uses DOCKER_RUN: CI holds no Snowflake or kea-db credentials, so each is
# offline apart from image, pip and dbt-hub pulls. ONLY= runs a subset --
# `make ci ONLY=test` is the millisecond host suite on its own.
CI_STEPS = lint test migrations story-guard dbt-parse serving-db dags backtest
CI_RUN   = $(or $(ONLY),$(CI_STEPS))
BASE    ?= origin/main

# Parsing needs a profile, not a warehouse: these values only have to exist.
# deps writes the gitignored dbt_packages/; target/ and logs/ stay in /tmp.
CI_DBT = docker run --rm -v $(CURDIR)/pipeline/dbt_project:/dbt -w /dbt \
  -e DO_NOT_TRACK=1 -e DBT_TARGET_PATH=/tmp/target -e DBT_LOG_PATH=/tmp/logs \
  -e SNOWFLAKE_ACCOUNT=ci -e SNOWFLAKE_USER=ci -e SNOWFLAKE_ROLE=ci -e SNOWFLAKE_WAREHOUSE=ci \
  -e SNOWFLAKE_DATABASE=KEA -e SNOWFLAKE_PRIVATE_KEY_PATH=/nonexistent.p8 -e DBT_SCHEMA=ci

# Migrates an empty Postgres 16 (kea-db's version), publishes into it and
# retries, then removes it and its network whatever happened. pg_isready goes
# over TCP because initdb's temporary server listens on the socket only.
CI_SERVING_DB = set -eu; net=kea_ci; db=kea-ci-serving-db; \
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
    -w /opt/data/pipeline/tests $(DBT_IMAGE) python3 -m unittest -v test_serving_publication

# In the Airflow image, offline and without .env, over the folders compose
# mounts into the dag-processor, read-only.
CI_DAGS = docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v $(CURDIR)/pipeline/airflow/dags:/opt/airflow/dags:ro \
  -v $(CURDIR)/pipeline/lib:/opt/airflow/lib:ro \
  -v $(CURDIR)/pipeline/scripts:/opt/airflow/scripts:ro \
  -v $(CURDIR)/pipeline/contracts:/opt/airflow/contracts:ro \
  -v $(CURDIR)/pipeline/tests:/opt/airflow/tests:ro \
  --entrypoint python $(AIRFLOW_IMAGE) \
  -m unittest discover -v -s /opt/airflow/tests -p test_dag_import.py

# THE 43 kNN TESTS RUN NOWHERE ELSE. The host `test` step is stdlib-only by
# contract, so test_knn_quantile.py skips every one of them there -- including
# the leakage tests, which decide whether the backtest means anything at all: a
# model that can see day d scores beautifully and proves nothing. The dbt image
# carries numpy, so they run in it, offline, with no .env and no credentials.
#
# A SKIP FAILS HERE. unittest exits 0 when every test is skipped, so an image
# that lost numpy would turn this green while testing nothing. This runner
# exits 1 on any skip, and on zero tests run.
CI_BACKTEST = docker run --rm --network none -v $(CURDIR)/pipeline:/opt/pipeline:ro \
  -w /opt/pipeline $(DBT_IMAGE) \
  python3 -c "import sys, unittest; \
    suite = unittest.defaultTestLoader.discover('/opt/pipeline/tests', 'test_knn_quantile.py', '/opt/pipeline/tests'); \
    r = unittest.TextTestRunner(verbosity=2).run(suite); \
    print(f'{r.testsRun} run, {len(r.skipped)} skipped'); \
    sys.exit(0 if r.wasSuccessful() and r.testsRun and not r.skipped else 1)"

ci:  ## lint, unit tests, migrations, story guard, dbt parse, serving publication, DAG import, backtest tests; changes nothing, needs no .env, ONLY=<step ...>
	@for s in $(CI_RUN); do case " $(CI_STEPS) " in *" $$s "*) ;; \
	    *) echo "unknown CI step '$$s'; one of: $(CI_STEPS)"; exit 2 ;; esac; done

# 1 LINT. The ruff version is the image tag and the rules are in ruff.toml, so
#   every machine runs the same lint. The mount is read-only, hence --no-cache.
	$(if $(filter lint,$(CI_RUN)),docker run --rm -v $(CURDIR):/io:ro -w /io ghcr.io/astral-sh/ruff:0.16.3 check --no-cache pipeline/ analysis/ sample/ load.py)

# 2 TEST. Every host unit test: standard library only, no database, lake or
#   Spark -- milliseconds.
	$(if $(filter test,$(CI_RUN)),python3 -m unittest discover -s pipeline/tests -t pipeline/tests)
	$(if $(filter test,$(CI_RUN)),python3 -m unittest discover -s $(GEN)/tests)

# 3 MIGRATIONS. Fails if a serving migration already on BASE (default
#   origin/main) was changed, renamed or deleted: applied ones are immutable.
	$(if $(filter migrations,$(CI_RUN)),pipeline/scripts/gates/check_migrations_immutable.sh '$(BASE)')

# 4 STORY GUARD. The identifier-free pages' cards: names unique, no identifier
#   read. A further identifier-free page goes here too.
	$(if $(filter story-guard,$(CI_RUN)),python3 analysis/metabase/build_story_dashboard.py --check)
	$(if $(filter story-guard,$(CI_RUN)),python3 analysis/metabase/build_sprint4_dashboard.py --check)
	$(if $(filter story-guard,$(CI_RUN)),python3 analysis/metabase/build_backtest_dashboard.py --check)

# 5 DBT PARSE, offline: refs, sources, Jinja, YAML, tests. The dbt image is
#   built from the same Dockerfile `make up` uses, without compose, which reads
#   .env; steps 5, 6 and 8 share it, and it is quick once cached.
	$(if $(filter dbt-parse serving-db backtest,$(CI_RUN)),docker build -t $(DBT_IMAGE) pipeline/docker/dbt)
	$(if $(filter dbt-parse,$(CI_RUN)),$(CI_DBT) $(DBT_IMAGE) dbt deps)
	$(if $(filter dbt-parse,$(CI_RUN)),$(CI_DBT) --network none $(DBT_IMAGE) dbt parse --profiles-dir . --no-partial-parse)

# 6 SERVING PUBLICATION: the serving migrations and a publication, on a
#   throwaway Postgres 16.
	$(if $(filter serving-db,$(CI_RUN)),$(CI_SERVING_DB))

# 7 DAG IMPORT: every DAG in the Airflow image, offline -- six DAGs, no errors.
	$(if $(filter dags,$(CI_RUN)),docker build -t $(AIRFLOW_IMAGE) pipeline/airflow)
	$(if $(filter dags,$(CI_RUN)),$(CI_DAGS))

# 8 BACKTEST TESTS: the 43 kNN quantile and leakage tests, in the dbt image,
#   failing on any skip.
	$(if $(filter backtest,$(CI_RUN)),$(CI_BACKTEST))

# THE NAMES .github/workflows/ci.yml CALLS, one per step it runs, and nothing
# else: each is `ci` with ONLY set. They exist only because the workflow names
# them and is not ours to change -- it is outside data/, and its code owner
# reviews any edit. Once it runs `make ci ONLY=<step>` itself, delete them.
ci-lint-test:   ; @$(MAKE) --no-print-directory ci ONLY="lint test"
ci-migrations:  ; @$(MAKE) --no-print-directory ci ONLY=migrations
ci-story-guard: ; @$(MAKE) --no-print-directory ci ONLY=story-guard
ci-dbt-parse:   ; @$(MAKE) --no-print-directory ci ONLY=dbt-parse
ci-serving-db:  ; @$(MAKE) --no-print-directory ci ONLY=serving-db
ci-dags:        ; @$(MAKE) --no-print-directory ci ONLY=dags
ci-backtest-tests: ; @$(MAKE) --no-print-directory ci ONLY=backtest

# CD, by hand on this machine after a merge; `make help` shows the git steps
# around it. The stack's bind mounts point at this checkout, so this is the one
# that deploys. Recursive makes, as in rebuild-all, so `make -j` cannot reorder
# the steps. Never part of a deploy: rebuild-all, source generation, reseeding,
# extracts, lake or RAW rewrites, TRUNCATE, pausing or unpausing a DAG, and
# setting role passwords -- so every destructive option is passed EMPTY below,
# and nothing left in the shell can make a deploy do one of them.
#
# To check without deploying: `pipeline/scripts/gates/deploy_gate.sh && make ci`.
DEPLOY_RELEASE = $(or $(RELEASE),git-$(shell git rev-parse --short=12 HEAD))

deploy:  ## after a merge to main: gates and ci, images, Warehouse over RAW, Serving as git-<sha>, dashboards
# 1 GATES, read-only: .env, a clean tree, HEAD at origin/main, kea-db up, and no
#   pipeline run in flight -- so the restart in step 3 interrupts nothing.
	pipeline/scripts/gates/deploy_gate.sh

# 2 CI, every step, on exactly what main holds.
	$(MAKE) ci ONLY=

# 3 IMAGES AND STACK. The restart loads DAG and lib/ changes now rather than
#   on the next parse.
	$(MAKE) up BUILD=1 STREAMING=
	docker compose restart airflow-dag-processor airflow-scheduler

# 4 WAREHOUSE over RAW as it stands. A failing dbt test or Soda warehouse check
#   stops the deploy here, before kea-db changes.
	$(MAKE) build-warehouse LOAD_DATE= REGEN= RESET_RAW=

# 5 SERVING as this commit's release, migrating first. A Soda serving finding
#   stops the deploy after the release is published and gated, before step 6.
	$(MAKE) publish-serving RELEASE=$(DEPLOY_RELEASE) MIGRATE=1 MIGRATE_ARGS= ROLES= DEMO_RESET= VERIFY=

# 6 DASHBOARDS, every one, over the new release.
	$(MAKE) dashboards ONLY=
	@echo "deployed $(DEPLOY_RELEASE)"


## Orchestration — Lake, Warehouse and Serving under Airflow

# ONE target for orchestration: Lake, Warehouse and Serving as Airflow runs
# them, rather than by hand. One DagRun per load date, carrying the date in its conf -- the
# same DAG, not a second code path (design §11) -- then it waits, and fails if
# any run did.
#
# Safe to repeat: a date keeps one run id, kea_bf__<date>, so a date that has
# run already is reported and skipped; clear its run in Airflow to redo it. It
# refuses a PAUSED DAG rather than queue runs that never start -- and the DAG
# is paused on purpose on the demo layout, where a nightly date staged
# over the one generation duplicates rows.
#
#   LOAD_DATE=D     one date (default yesterday, the DAG's own rule)
#   FROM=D1 TO=D2   each date of a range, one at a time (max_active_runs=1)
#   VERIFY=1        then the quality proof (see Quality gates in the stack doc),
#                   which needs the DAG: an injected defect reaches quarantine
#                   with its reason, raises an alert and stops the export, and
#                   lineage covers source to serving. About ten minutes, and it
#                   FAILS a DagRun on purpose -- expect one in the UI and in
#                   the alert sink
run-pipeline: env-check  ## run kea_daily_pipeline for LOAD_DATE, or each date FROM..TO, and wait; safe to repeat, VERIFY=1 runs the quality proof
	pipeline/scripts/ops/run_pipeline.sh $(if $(FROM),$(FROM) $(or $(TO),$(FROM)),$(LOAD_DATE))
	@if [ "$(VERIFY)" = "1" ]; then pipeline/scripts/gates/assert_quality.sh $(EXPLICIT_LOAD_DATE); fi


## Utilities — the stack's lifecycle, and a look inside

# The repo-root Makefile calls `down`, `clean` and `ps` by name (as data-*).
down:  ## stop batch and streaming containers; keep volumes
	docker compose --profile streaming down

clean:  ## DESTRUCTIVE: stop containers and delete this project's volumes
	docker compose --profile streaming down -v

ps:  ## show running pipeline containers
	docker compose ps

logs:  ## follow pipeline container logs
	docker compose logs -f

# A terminal when there is one; stdin otherwise, so `echo 'select 1' | make psql`
# works too -- compose refuses to allocate a TTY it does not have.
psql:  ## open a psql shell on the pipeline database (sources, audit), or run what is piped in
	docker compose exec $$([ -t 0 ] || echo -T) kea-source-db psql -U kea -d kea
