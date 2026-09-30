"""The nightly path from bronze to the served tables: stage, build, gate, export, publish.

THE TASK GROUPS ARE THE MAKEFILE'S STEPS. `build-warehouse` is `make
build-warehouse` -- snowflake_stage, dbt_run, dbt_test, soda_scan_warehouse --
and `publish-serving` is `make publish-serving` -- export_warehouse,
publish_serving, assert_serving, soda_scan_serving -- the same scripts in the
same order -- and `dashboards` is the Analysis step's one nightly need, the
Lake dashboard's rollups. The groups do not prefix the task ids, so every gate,
alert and dashboard that names a task still finds it. `make run-pipeline` triggers this
DAG; the four kea_extract_* DAGs upstream of it are the Lake step, `make
ingest-date`.

WHAT THIS DAG DOES NOT CONTAIN, AND WHY THAT IS THE DESIGN.

It does not own the extracts. Design §9 draws all four inside one pipeline and
then corrects itself, and `_kea_common.py` says the same: that picture survives
as LINEAGE, and Stage 6's pipeline "should be asset-scheduled on what these
emit rather than owning their tasks". One DagRun has one failure state, so a
billing failure inside this DAG would block metering however the edges were
drawn -- which is exactly the property Stage 3 built four separate DAGs to
guarantee.

So this waits on `AssetAll` of the three daily lanes' bronze. It starts when
its inputs EXIST, not when a clock says they probably do.

⚠️ WEATHER IS DELIBERATELY NOT AN INPUT. `kea_extract_weather` is @monthly --
NIWA publishes the full record on a monthly cycle -- and gating a nightly
pipeline on a monthly source would leave it blocked for twenty-nine nights in
thirty. The marts read whatever weather partition is current; a month-old one
is the normal case, not a fault.

THE GATE IS AN EDGE, AND THAT IS THE WHOLE OF CHECKBOX 4.

`dbt_test` sits between the build and every export task. Design §9: "If
dbt_test or soda_scan fails, the export tasks do not run. Postgres keeps
serving yesterday's data, which is correct-but-stale rather than fresh-but-
wrong." Airflow marks the downstream tasks `upstream_failed` and they never
start -- visible in the grid, not merely logged.

`dbt run` and `dbt test` are SEPARATE TASKS for that reason. `dbt build`
interleaves them model by model, so a failing test would leave some models
built and some not, and the run could not say which half failed.

THE PUBLISH IS ONE TASK BECAUSE IT IS ONE TRANSACTION.

§9 also draws the export as a chain of per-table tasks ordered by the foreign
key graph. Stage 5 proved that cannot be the load's shape: emptying
metering.property while metering.icp references it is a foreign key violation,
and no delete order avoids it in general, so the loader stages everything and
commits ONCE with the constraints deferred. `publish_serving.py` widens that
transaction to the Sprint 3 decisions -- forecasts, plan recommendations,
hardship assessments and alerts -- which are computed from the rows it has
just replaced. Splitting any of it across Airflow tasks would give each part
its own commit, and the application would read new bills beside yesterday's
decisions about them. `assert_load_atomic.sh` proves the atomicity. The FK
order lives in `kea_pipeline.serving.TABLES`, inside that transaction.

THE SERVED TABLES LIVE IN THE BACKEND'S DATABASE, `kea-db` in ../infra. This
stack's own Postgres keeps only the sources and the audit schemas. The publish
and the gate after it both connect as kea_loader through KEA_PUBLISH_DSN, the
gate inside a transaction it rolls back. Airflow holds no owner login, so
nothing here applies migrations: the publish refuses a database that is behind
`migrations/serving/`, and migrating is an owner step (`make publish-serving MIGRATE=1`).

BACKFILL IS THIS DAG WITH A CONF, NOT A SECOND CODE PATH.

Design §11: "No separate backfill code path -- a second code path is a second
set of bugs, and it is always the one that is under-tested." `load_date`
defaults to the same fire-time rule every other DAG here uses, and a backfill
overrides it:

    airflow dags trigger kea_daily_pipeline --conf '{"load_date": "2026-08-14"}'

That override matters more than it looks. An asset event carries no load date,
so a cleared extract re-run for an old date would fire this DAG, which would
then compute TODAY from its own fire time and stage the wrong day.
"""

from __future__ import annotations

import os
import sys

import pendulum
from airflow.models import DAG
from airflow.sdk import AssetAll, TaskGroup
from docker.types import Mount

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _kea_common import (  # noqa: E402
    BACKEND_DB_ENV, BACKEND_HOSTS, BRONZE, DBT_IMAGE, DEFAULT_ARGS,
    HOST_PROJECT_DIR, LAKE_ENV, MACROS, NZ, LINEAGE_ENV, PSQL_IMAGE, SERVING,
    SNOWFLAKE_ENV, SODA_IMAGE, SPARK_IMAGE, _container, _key_mount, _publish,
)

#: The NZ day this run covers. `params.load_date` wins when a run supplies one,
#: which is what makes a backfill and a nightly run the same code path.
#:
#: `run_after`, NOT `data_interval_end`, AND THE DIFFERENCE IS NOT COSMETIC.
#: The extract DAGs read data_interval_end because they are schedule-driven and
#: Airflow 3 sets all three interval fields to the fire instant. THIS DAG IS
#: ASSET-DRIVEN AND HAS NO INTERVAL AT ALL: data_interval_end is undefined, and
#: passing an Undefined into the macro fails with "'data_interval_end' is
#: undefined" -- which is how this was written first, and it failed on the very
#: first task test rather than silently picking a wrong day.
#:
#: run_after is when the run became ELIGIBLE -- the moment the asset condition
#: was met, or the moment a schedule fired. That is the same "fire time" the
#: NZ-calendar-day rule in kea_pipeline.window is defined against, so the rule
#: itself is unchanged and still has exactly one tested implementation.
LOAD_DATE = "{{ params.load_date or kea_load_date(dag_run.run_after) }}"

with DAG(
    dag_id="kea_daily_pipeline",
    description="bronze -> Snowflake -> dbt -> gate -> export -> publish into the backend's Postgres",
    # ASSET-SCHEDULED, NOT 02:00. See the module docstring: this runs when the
    # three daily lanes have actually landed their bronze. A fixed hour is a
    # guess about how long an extract takes, and a wrong guess reads yesterday's
    # partition and reports success.
    schedule=AssetAll(BRONZE["src_metering"], BRONZE["src_billing"],
                      BRONZE["src_catalogue"]),
    start_date=pendulum.datetime(2024, 9, 18, tz=NZ),
    # The extracts stop here because the source's arrivals do. A pipeline that
    # outlived its inputs would simply never fire again -- silent, not loud.
    end_date=pendulum.datetime(2026, 10, 12, tz=NZ),
    catchup=False,
    # NOT OPTIONAL. A month's backfill would otherwise start 31 runs against one
    # Snowflake warehouse and one backend database.
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["orchestration", "run-pipeline", "build-warehouse", "publish-serving", "dashboards"],
    doc_md=__doc__,
    user_defined_macros=MACROS,
    params={"load_date": None},
) as dag:

    # THE WAREHOUSE STEP -- `make build-warehouse`, the same four commands in the
    # same order. A task group only: prefix_group_id=False keeps every task id,
    # which the gates, the alert sink and Grafana all name, exactly as it was.
    with TaskGroup("build-warehouse", prefix_group_id=False,
                   tooltip="make build-warehouse: stage RAW, dbt run, dbt test, Soda gate"):
        # ---------------------------------------------------------------- stage
        stage = _container(
            dag, "snowflake_stage", DBT_IMAGE,
            ["python3", "warehouse/load_all_bronze_to_snowflake.py", LOAD_DATE],
            mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline", target="/opt/pipeline",
                          type="bind", read_only=True), *_key_mount()],
            environment={**SNOWFLAKE_ENV, **LAKE_ENV, **LINEAGE_ENV},
            working_dir="/opt/pipeline/scripts",
        )

        # ------------------------------------------------------------------ dbt
        # dbt_project is NOT read-only: dbt writes dbt_packages/ and target/ into
        # the project it is pointed at.
        dbt_mounts = [Mount(source=f"{HOST_PROJECT_DIR}/pipeline/dbt_project",
                            target="/dbt", type="bind"), *_key_mount()]

        # `dbt deps` first, as `make build-warehouse` does. Without it the build fails on
        # a missing dbt_utils before touching a model. `dbt seed` next, because
        # `dbt run` does not load seeds and dim_icp / mart_daily_usage join two of
        # them -- the fixtures' own weather and the region-to-station map.
        build = _container(
            dag, "dbt_run", DBT_IMAGE,
            ["sh", "-lc",
             "dbt deps && dbt seed --profiles-dir . && dbt-ol run --profiles-dir ."],
            mounts=dbt_mounts, environment={**SNOWFLAKE_ENV, **LINEAGE_ENV},
            working_dir="/dbt",
        )

        test = _container(
            dag, "dbt_test", DBT_IMAGE,
            ["sh", "-lc", "dbt-ol test --profiles-dir ."],
            mounts=dbt_mounts, environment={**SNOWFLAKE_ENV, **LINEAGE_ENV},
            working_dir="/dbt",
        )

        # ----------------------------------------------------------------- gate
        # THE RUNTIME GATE, and the second of the two design 10 names. dbt_test asks
        # whether the models are internally consistent; this asks whether the data
        # they are built from is plausible -- freshness of the load, volume against
        # the cohort mean, and the orphan rate. Soda exits 2 on a failed check, so a
        # failure fails the task and everything downstream never runs.
        #
        # Its checks are deliberately NOT the textbook ones. The source is a fixed
        # historical extract whose readings stop in Aug 2026, so a freshness check on
        # the readings, or a volume check per load date, is red every night forever.
        # checks/warehouse.yml carries the full reasoning.
        soda_warehouse = _container(
            dag, "soda_scan_warehouse", SODA_IMAGE,
            ["soda", "scan", "-d", "kea", "-c", "configuration.yml",
             "-v", f"load_date={LOAD_DATE}", "checks/warehouse.yml"],
            mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline/soda", target="/soda",
                          type="bind", read_only=True), *_key_mount()],
            environment=SNOWFLAKE_ENV,
            working_dir="/soda",
        )

    # THE SERVING STEP -- `make publish-serving`: export, publish, the gate, then
    # the report. Its owner-only options (MIGRATE, ROLES) have no task, because
    # Airflow holds no owner login.
    with TaskGroup("publish-serving", prefix_group_id=False,
                   tooltip="make publish-serving: export, publish, assert_serving, Soda report"):
        # --------------------------------------------------------------- export
        export = _container(
            dag, "export_warehouse", DBT_IMAGE,
            ["python3", "warehouse/export_warehouse_to_lake.py"],
            mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline", target="/opt/pipeline",
                          type="bind", read_only=True), *_key_mount()],
            environment={**SNOWFLAKE_ENV, **LAKE_ENV, **LINEAGE_ENV},
            working_dir="/opt/pipeline/scripts",
        )

        # -------------------------------------------------------------- publish
        # Shared with kea_forecast_refresh (see _kea_common._publish), so a release
        # is made one way whichever DAG makes it. The run id is the release id.
        publish = _publish(dag)

        # --------------------------------------------------------- reconciliation
        # psql in a container, not SQLExecuteQueryOperator. assert_serving.sql opens
        # with `\set ON_ERROR_STOP on` and uses `\echo`; those are psql CLIENT
        # commands and a SQL operator would send them to the server and fail on the
        # first one. This also keeps the file byte-identical to `make publish-serving`'s
        # assert_serving step.
        #
        # AS kea_loader, INSIDE A TRANSACTION IT ROLLS BACK. The gate builds temp
        # objects, which a read-only session refuses; the ROLLBACK discards
        # everything it creates, and on failure psql exits and the session ends
        # with it. The publisher's own credential, so Airflow holds no owner login
        # at all: migrating stays a manual owner step.
        check = _container(
            dag, "assert_serving", PSQL_IMAGE,
            ["sh", "-c", ('exec psql -X -v ON_ERROR_STOP=1 -c BEGIN '
                          '-f /scripts/gates/assert_serving.sql -c ROLLBACK "$KEA_PUBLISH_DSN"')],
            mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline/scripts", target="/scripts",
                          type="bind", read_only=True)],
            environment={"KEA_PUBLISH_DSN": os.environ.get("KEA_PUBLISH_DSN", "")},
            # The store is announced current only once the gate has passed on it.
            outlets=[SERVING],
            extra_hosts=BACKEND_HOSTS,
        )

        # A REPORT, NOT A GATE. It runs after the publish, so nothing downstream of
        # it can be stopped -- the rows are already in the store the agent reads. It
        # answers "is what we just served sound", and the scan that can still refuse
        # to serve is soda_scan_warehouse above. As kea_app, the agent's read-only
        # role, so it sees what the application is allowed to see.
        #
        # LAST, AFTER assert_serving, and that order is the point of it being a
        # report. Before the gate, a finding here would stop the gate from running
        # and SERVING from being announced -- a report acting as a gate. `make
        # publish-serving` runs the same two in the same order.
        soda_serving = _container(
            dag, "soda_scan_serving", SODA_IMAGE,
            ["soda", "scan", "-d", "kea_serving", "-c", "configuration.yml",
             "checks/serving.yml"],
            mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline/soda", target="/soda",
                          type="bind", read_only=True)],
            environment={"KEA_APP_PASSWORD": os.environ.get("KEA_APP_PASSWORD", ""),
                         **BACKEND_DB_ENV},
            working_dir="/soda",
            extra_hosts=BACKEND_HOSTS,
        )

    # THE ANALYSIS STEP -- `make dashboards`, as far as a DAG needs it. Five of
    # the seven dashboards query the served tables live, so the publish above is
    # what refreshes them. The Lake dashboard reads rollups instead -- Parquet
    # on MinIO is nothing Metabase can query -- and without this task they would
    # stay as old as the last `make dashboards`. (The Sources dashboard's views
    # follow the source database, which only a person regenerates.)
    #
    # AS kea_lake_audit, which owns lake_audit and can read no source system:
    # never the superuser, which Airflow does not hold. `make dashboards` creates
    # the role. Last, after the serving report, as in the Makefile.
    with TaskGroup("dashboards", prefix_group_id=False,
                   tooltip="make dashboards: the Lake dashboard's rollups"):
        summarise_lake = _container(
            dag, "summarise_lake", SPARK_IMAGE,
            ["/opt/spark/bin/spark-submit", "--master", "spark://spark-master:7077",
             "/opt/analysis/metabase/summarise_lake.py"],
            mounts=[
                # The same layout as the metering Spark tasks: lib/ and
                # contracts/ as siblings under spark_jobs/, where spark_session
                # and the schema loader find them.
                Mount(source=f"{HOST_PROJECT_DIR}/pipeline/spark_jobs",
                      target="/opt/spark_jobs", type="bind", read_only=True),
                Mount(source=f"{HOST_PROJECT_DIR}/pipeline/lib",
                      target="/opt/spark_jobs/lib", type="bind", read_only=True),
                Mount(source=f"{HOST_PROJECT_DIR}/pipeline/contracts",
                      target="/opt/spark_jobs/contracts", type="bind", read_only=True),
                Mount(source=f"{HOST_PROJECT_DIR}/analysis/metabase",
                      target="/opt/analysis/metabase", type="bind", read_only=True),
            ],
            environment={
                **LAKE_ENV,
                "KEA_LAKE_AUDIT_PASSWORD": "{{ conn.kea_lake_audit.password }}",
                "KEA_SRC_DB_HOST": "{{ conn.kea_lake_audit.host }}",
                "KEA_SRC_DB_NAME": "{{ conn.kea_lake_audit.schema or 'kea' }}",
            },
        )

    stage >> build >> test >> soda_warehouse >> export >> publish >> check >> soda_serving
    soda_serving >> summarise_lake
