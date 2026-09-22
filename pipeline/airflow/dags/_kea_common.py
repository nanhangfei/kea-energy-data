"""Shared DAG construction. Underscore-prefixed so the DAG processor skips it.

THREE DAGS, NOT ONE, AND THAT IS THE WHOLE POINT. Stage 3's first criterion is
"four independent extract jobs on their own schedules; a billing failure does
not block metering". Inside one DagRun that is a convention: one run has one
state, so a failed billing task leaves the run red whatever the dependencies
say, and someone re-clearing it re-runs metering too. Separate DAGs make it a
fact -- separate runs, separate schedules, separate connections, separate
alerts.

Design §9 draws all three inside `kea_daily_pipeline`. That picture survives as
LINEAGE; the schedules genuinely separate, and Stage 6's pipeline should be
asset-scheduled on what these emit rather than owning their tasks.

Each task is handed exactly one system's credential, by name. A task that could
reach two of them would make the grants the only thing standing between a
cross-system join and a plausible wrong answer, and grants are a backstop, not
a design.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import pendulum
from docker.types import Mount
from airflow.models import DAG
from airflow.providers.docker.operators.docker import DockerOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import Asset

sys.path.insert(0, "/opt/airflow/lib")
sys.path.insert(0, "/opt/airflow/scripts")

from kea_pipeline import spec, window  # noqa: E402

NZ = pendulum.timezone("Pacific/Auckland")

# WHAT EACH LANE PRODUCES, NAMED SO SOMETHING ELSE CAN WAIT ON IT.
#
# This module's own docstring says Stage 6's pipeline "should be asset-scheduled
# on what these emit rather than owning their tasks", and design §9 says the
# same. These are what it emits.
#
# THE URI IS THE LAKE PATH, NOT THE DAG. An asset is a claim about data --
# "bronze/parsed for this source is current" -- and naming it after the task
# that happened to write it would make the claim about a schedule instead. The
# path is also what a reader can go and look at.
#
# One per SOURCE, not per entity. A source's bronze is current only when all of
# its entities have landed; four separate assets would let kea_daily_pipeline
# start on a billing run that had loaded customers and not invoices.
BRONZE = {
    source: Asset(f"s3://kea-lake/bronze/parsed/{source}")
    for source in ("src_metering", "src_billing", "src_catalogue", "src_weather")
}

#: What the serving store becoming current looks like. Emitted by
#: kea_daily_pipeline's last task, so the forecast refresh -- and Stage 7's Soda
#: scan -- have something to schedule on that is not a clock.
#:
#: A CUSTOM SCHEME, DELIBERATELY. Airflow validates `postgres://` URIs and
#: demands database, schema AND table -- "URI format postgres:// must contain
#: database, schema, and table names", which is what this was until the DAG
#: processor refused to import it. Naming one table would be a lie about what
#: the asset means: the load replaces twenty of them in one transaction, and
#: what became current is the store, not a row somewhere in it.
SERVING = Asset("kea://serving/kea")

#: The NZ day a run covers: the one that just ENDED. Airflow 3's @daily has a
#: zero-length interval, so the fire time is all a run knows -- see
#: kea_extract_metering.py and kea_pipeline.window.load_date_for_fire_time.
#:
#: ⚠️ `run_after`, NOT `data_interval_end`. For a SCHEDULED run the two are the
#: same instant, so this changes nothing about the nightly path. For a MANUALLY
#: TRIGGERED one there is no interval at all, and the old expression failed the
#: task outright:
#:
#:     UndefinedError: 'data_interval_end' is undefined
#:
#: These DAGs could therefore only ever run on their schedule -- re-running a
#: day by hand, which is the most ordinary operator action there is, failed
#: before the extract started. It went unnoticed because all 36 historical runs
#: were scheduled ones, and it surfaced only when a manual trigger was used to
#: generate metrics for the Stage 7 dashboard.
#:
#: run_after is when the run became ELIGIBLE, which is the fire time the
#: NZ-calendar-day rule in kea_pipeline.window is defined against either way.
#: kea_daily_pipeline already uses it, for the same reason.
LOAD_DATE = "{{ kea_load_date(dag_run.run_after) }}"

def alert_on_failure(context) -> None:
    """Announce a failed task. Design 11's one alerting row, as code.

    ON DEFAULT_ARGS, SO EVERY TASK IN EVERY DAG INHERITS IT. The failure that
    matters most -- "dbt test fails: export blocked, alert" -- is one task in
    one DAG, but a per-DAG opt-in is a list someone forgets to add to, and the
    task that gets forgotten is always the new one.

    SLACK FIRST, THE LOCAL SINK OTHERWISE. Both are read at RUN time, so
    pointing this at a real workspace later is a variable and not a code change.
    KEA_ALERT_WEBHOOK reaches the alert-sink service, which keeps what it is
    sent so a test can assert an alert actually fired.

    ⚠️ IT NEVER RAISES. An exception in a failure callback would replace the
    real failure with a confusing one about alerting, and the thing that just
    broke is what someone needs to see. A dead webhook degrades to a log line.
    """
    import json
    import urllib.request

    url = os.environ.get("SLACK_WEBHOOK_URL") or os.environ.get("KEA_ALERT_WEBHOOK")
    ti = context.get("task_instance")
    reason = str(context.get("exception") or "no exception recorded")[:500]
    body = {
        "text": f"kea pipeline FAILED: {getattr(ti, 'dag_id', '?')}.{getattr(ti, 'task_id', '?')}",
        "dag_id": getattr(ti, "dag_id", None),
        "task_id": getattr(ti, "task_id", None),
        "run_id": getattr(ti, "run_id", None),
        "try_number": getattr(ti, "try_number", None),
        "reason": reason,
    }
    if not url:
        print(f"ALERT (no webhook configured): {json.dumps(body)}")
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"alert posted, {r.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"alert could NOT be posted to {url}: {exc}")


DEFAULT_ARGS = {
    "owner": "data",
    "retries": 2,
    "retry_delay": dt.timedelta(minutes=5),
    "depends_on_past": False,
    # Fires after the LAST retry, not on each one -- Airflow calls this when the
    # task instance reaches `failed`, so a task that recovers on retry 2 is not
    # an incident and does not page anyone.
    "on_failure_callback": alert_on_failure,
}

MACROS = {"kea_load_date": lambda fire: window.load_date_for_fire_time(fire).isoformat()}


DBT_IMAGE = "dataeng/dbt:1.12.0"
PSQL_IMAGE = "postgres:17.10"
# Its own image: dbt-snowflake and soda-core-snowflake are unresolvable
# for pip together (ResolutionTooDeep), which is why there are two.
SODA_IMAGE = "dataeng/soda:latest"
HOST_PROJECT_DIR = os.environ.get("HOST_PROJECT_DIR", "/opt/airflow")
NETWORK = os.environ.get("COMPOSE_NETWORK", "kea_default")
KEY_HOST = os.environ.get("SNOWFLAKE_PRIVATE_KEY_HOST", "")


#: Everything the Snowflake-facing containers need. The key is mounted rather
#: than passed: SNOWFLAKE_PRIVATE_KEY_PATH names where it will BE, and the
#: mount below is what puts it there.
SNOWFLAKE_ENV = {
    "SNOWFLAKE_ACCOUNT": os.environ.get("SNOWFLAKE_ACCOUNT", ""),
    "SNOWFLAKE_USER": os.environ.get("SNOWFLAKE_USER", ""),
    "SNOWFLAKE_ROLE": os.environ.get("SNOWFLAKE_ROLE", ""),
    "SNOWFLAKE_WAREHOUSE": os.environ.get("SNOWFLAKE_WAREHOUSE", ""),
    "SNOWFLAKE_DATABASE": os.environ.get("SNOWFLAKE_DATABASE", "KEA"),
    "SNOWFLAKE_PRIVATE_KEY_PATH": "/keys/snowflake.p8",
    "DBT_SCHEMA": os.environ.get("DBT_SCHEMA", "kea"),
}

# Every emitter points at the same Marquez and the same namespace. Airflow's
# provider gets these from AIRFLOW__OPENLINEAGE__*; the containers Airflow
# launches are separate processes and need telling.
LINEAGE_ENV = {
    "OPENLINEAGE_URL": os.environ.get("OPENLINEAGE_URL", "http://marquez:5000"),
    "OPENLINEAGE_NAMESPACE": os.environ.get("OPENLINEAGE_NAMESPACE", "kea_energy"),
}

LAKE_ENV = {
    "MINIO_ENDPOINT": os.environ.get("MINIO_ENDPOINT", "http://minio:9000"),
    "MINIO_ROOT_USER": os.environ.get("MINIO_ROOT_USER", ""),
    "MINIO_ROOT_PASSWORD": os.environ.get("MINIO_ROOT_PASSWORD", ""),
    "KEA_LAKE_BUCKET": os.environ.get("KEA_LAKE_BUCKET", "kea-lake"),
}

#: The served tables live in the backend's database (kea-db, in ../infra), which
#: is not on this network. Containers reach it through the Docker host, as every
#: `make` target does. Docker Desktop resolves host.docker.internal without
#: help; Linux needs this mapping, and it is harmless where it is redundant.
BACKEND_HOSTS = {"host.docker.internal": "host-gateway"}

#: The same database for the tools that take a host and port, not a DSN (Soda).
BACKEND_DB_ENV = {
    "KEA_BACKEND_DB_HOST": os.environ.get("KEA_BACKEND_DB_HOST") or "host.docker.internal",
    "KEA_BACKEND_DB_PORT": os.environ.get("KEA_BACKEND_DB_PORT") or "5432",
}

#: services/api on the HOST, for the Sprint 3 jobs that run the backend's own
#: billing and plan code (KEA_API_DIR). A bind mount source, so a host path.
API_DIR = os.path.normpath(f"{HOST_PROJECT_DIR}/../services/api")


def _key_mount() -> list[Mount]:
    """The Snowflake key, from the HOST path.

    Bind mounts are resolved by the host daemon, not inside Airflow -- the same
    reason HOST_PROJECT_DIR exists. Empty when unset rather than pointing at a
    path that does not exist, so the failure is "no key" at the connection
    instead of a docker error about a missing source.
    """
    return [Mount(source=KEY_HOST, target="/keys/snowflake.p8",
                  type="bind", read_only=True)] if KEY_HOST else []


def _container(dag: DAG, task_id: str, image: str, command, *,
               mounts: list[Mount], environment: dict,
               working_dir: str | None = None,
               outlets: list | None = None,
               extra_hosts: dict | None = None) -> DockerOperator:
    """One throwaway container, shaped exactly like the Spark tasks in
    kea_extract_metering.py -- same network, same tmp-dir handling, same
    auto_remove. Divergence there is how two ways of launching a container
    become two sets of bugs."""
    return DockerOperator(
        task_id=task_id,
        dag=dag,
        image=image,
        api_version="auto",
        auto_remove="success",
        network_mode=NETWORK,
        # The operator's own /tmp mount is unnecessary and fails on Docker
        # Desktop's file sharing.
        mount_tmp_dir=False,
        mounts=mounts,
        environment=environment,
        working_dir=working_dir,
        command=command,
        outlets=outlets or [],
        extra_hosts=extra_hosts,
    )


def _publish(dag: DAG, task_id: str = "publish_serving") -> DockerOperator:
    """One release into the backend's database, the only way any DAG makes one.

    kea_daily_pipeline and kea_forecast_refresh both publish through this, so
    the served tables and the decisions computed from them change together
    whichever DAG ran -- a DAG that loaded one table on its own would put new
    inputs beside yesterday's decisions about them.

    The whole data directory, not just pipeline/: the publish refuses a
    database that is behind migrations/serving/, so it has to see them. The
    backend's services/api is mounted read-only for the Sprint 3 jobs that run
    its own billing and plan code.

    THE RUN ID IS THE RELEASE ID. A retry after a failure publishes; a task
    cleared after a success finds its release already recorded and changes
    nothing. A new run is a new release.
    """
    return _container(
        dag, task_id, DBT_IMAGE,
        ["python3", "publish_serving.py", "--release-id", "{{ run_id }}"],
        mounts=[Mount(source=HOST_PROJECT_DIR, target="/opt/data",
                      type="bind", read_only=True),
                Mount(source=API_DIR, target="/opt/api", type="bind", read_only=True)],
        environment={
            **LAKE_ENV, **LINEAGE_ENV,
            # kea_loader, never the owner. Design §1.1's guarantee that a
            # pipeline bug cannot reach app.* is a property of this credential.
            "KEA_PUBLISH_DSN": os.environ.get("KEA_PUBLISH_DSN", ""),
            "KEA_API_DIR": "/opt/api",
            # dbt's run id, so a serving row traces back to the build that made
            # it -- mart_load_log.warehouse_run_id exists for this.
            "DBT_INVOCATION_ID": "{{ run_id }}",
        },
        working_dir="/opt/data/pipeline/scripts",
        extra_hosts=BACKEND_HOSTS,
    )



def _run_entity(qualified: str, load_date: str, conn_id: str) -> None:
    """Run the extract in-process, with one system's credential and no other.

    The password is fetched from the Airflow connection at RUN time, not at
    parse time, and injected only for the duration of this call -- so a task
    for one system never has another's in its environment, even transiently.
    """
    from airflow.sdk import Connection
    import extract_small

    entity = spec.get(qualified)
    system = entity.source.removeprefix("src_").upper()
    connection = Connection.get(conn_id)

    key = f"KEA_EXTRACT_{system}_PASSWORD"
    previous = os.environ.get(key)
    os.environ[key] = connection.password or ""
    os.environ.setdefault("KEA_SRC_DB_HOST", connection.host or "kea-source-db")
    os.environ.setdefault("KEA_SRC_DB_NAME", connection.schema or "kea")
    try:
        rc = extract_small.main([qualified, load_date])
        if rc != 0:
            raise RuntimeError(f"{qualified} extract returned {rc}")
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def scripted_task(dag: DAG, entity: spec.Entity) -> PythonOperator:
    """One small entity: JDBC -> bronze -> parsed, in the Airflow image.

    Design §5.1 -- everything but interval_reads is a script, because standing
    up a distributed engine for a 73-row table costs more in startup than the
    read costs at all.
    """
    return PythonOperator(
        task_id=f"{entity.mode}_{entity.table}",
        dag=dag,
        python_callable=_run_entity,
        op_kwargs={
            "qualified": entity.qualified,
            "load_date": LOAD_DATE,
            "conn_id": entity.connection,
        },
        templates_dict={},
    )


def scripted_dag(source: str, description: str, tags: list[str],
                 schedule: str = "@daily") -> DAG:
    """A whole source system's DAG, when none of its entities needs Spark.

    `schedule` is a parameter because cadence is a property of the SOURCE, not
    of this template: NIWA publishes monthly, so a daily weather run would land
    twenty-nine identical partitions a month and claim each was a day's news.
    """
    dag = DAG(
        dag_id=f"kea_extract_{source.removeprefix('src_')}",
        description=description,
        schedule=schedule,
        start_date=pendulum.datetime(2024, 9, 18, tz=NZ),
        end_date=pendulum.datetime(2026, 10, 12, tz=NZ),
        catchup=False,
        max_active_runs=1,
        # RUNNING, NOT PAUSED, ON A FRESH STACK. These DAGs sat paused for the
        # whole of Stages 5-7, and the cost was invisible: kea_daily_pipeline
        # waits on AssetAll of the three bronze lanes, so with two producers
        # paused its schedule could never be satisfied. Thirteen runs, not one
        # of them scheduled -- the orchestration Stage 6 built had never once
        # run itself. catchup=False and max_active_runs=1 above are what make
        # this safe: unpausing starts the next interval, not a month of them.
        is_paused_upon_creation=False,
        default_args=DEFAULT_ARGS,
        tags=tags,
        user_defined_macros=MACROS,
        doc_md=__doc__,
    )
    # No edges between them. These entities do not read one another, and a
    # chain would make one slow table delay the rest for no reason -- and turn
    # a single failure into a cascade of skips.
    tasks = [scripted_task(dag, entity) for entity in spec.script_entities(source)]

    # THE ASSET IS EMITTED BY A JOIN, NOT BY EACH TASK. Airflow marks an asset
    # updated when ANY task carrying it as an outlet succeeds, so putting the
    # outlet on all four would announce "billing's bronze is current" the moment
    # customers landed -- and kea_daily_pipeline would start staging a source
    # that was still half written. An EmptyOperator downstream of all of them
    # costs nothing and makes the claim true.
    ready = EmptyOperator(task_id="bronze_ready", dag=dag, outlets=[BRONZE[source]])
    for task in tasks:
        task >> ready
    return dag
