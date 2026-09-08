"""Nightly: src_metering.interval_reads -> bronze -> bronze/parsed.

The metering lane of design §9. Billing and catalogue get their own DAGs on
their own schedules, because Stage 3's first criterion is that "a billing
failure does not block metering" -- and a shared DAG makes that a convention
rather than a fact. Separate DagRuns, separate schedules, separate connections,
separate alerts: a billing failure is then a red run in a different DAG.

THE SCHEDULE IS IN PACIFIC/AUCKLAND, AND THAT IS NOT COSMETIC.

A load date is a New Zealand calendar day (design §4.2). The measurement behind
that rule: the UTC window for 2026-08-25 holds 26,623 rows and none of the
planted 36-day-late revision; the NZ window holds 26,925 and all 48 of it. A
UTC load date still returns thousands of plausible rows, so nothing looks
broken. Setting the timezone here makes each run fire at NZ midnight, so a
completed NZ day is what a run has to work with.

BUT THE FIRE TIME IS NOT THE LOAD DATE, and assuming otherwise is a one-day
gap every day. Airflow 3 maps `@daily` to CronTriggerTimetable, whose interval
is timedelta(0): `data_interval_start`, `data_interval_end` and `run_after` are
all the same instant. There is no covered period to read off -- the run knows
only when it fired. So the DAG steps back one CALENDAR day itself, through the
tested helper in kea_pipeline.window. See LOAD_DATE below.

`logical_date` is never consulted, and neither is the clock. Everything the
tasks need comes from the fire time, which is what makes a backfill the same
code path as a nightly run (design §11) rather than a second one.

WHY DockerOperator AND NOT SparkSubmitOperator. The Airflow image has neither
`java` nor `spark-submit` -- the provider is installed and unusable. Launching
a sibling container is the pattern this compose file already provisions for dbt
and Soda: the daemon socket is mounted and the airflow user is in group 0.
Because the daemon resolves bind mounts on the HOST, the paths below come from
HOST_PROJECT_DIR and not from anything inside this container.

RE-RUNNING A DagRun IS SAFE BY CONSTRUCTION. Both jobs skip a partition that
already carries _SUCCESS unless --force, so a cleared task re-runs without a
manual cleanup step, and the gate's replay proof is the same code path with one
flag added.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import pendulum
from airflow.models import DAG
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount

# The same module the Spark jobs import, so the DAG and the jobs cannot
# disagree about what a load date is.
sys.path.insert(0, "/opt/airflow/lib")
from kea_pipeline import window  # noqa: E402

# Airflow 3's DAG processor imports each file without the dags folder on
# sys.path, so the sibling helper needs saying explicitly -- silent when wrong.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _kea_common import BRONZE  # noqa: E402

NZ = pendulum.timezone("Pacific/Auckland")

SPARK_IMAGE = "dataeng/spark:3.5.9"
HOST_PROJECT_DIR = os.environ.get("HOST_PROJECT_DIR", "/opt/airflow")
NETWORK = os.environ.get("COMPOSE_NETWORK", "kea_default")

#: The NZ day this run covers -- the one that just ENDED.
#:
#: Airflow 3 maps `@daily` to CronTriggerTimetable, whose interval is
#: timedelta(0): data_interval_start, data_interval_end and run_after are all
#: the same instant, the moment the run fires. So a run firing at NZ midnight
#: on the 21st must be told to extract the 20th. Taking the fire time's own
#: date would extract a day that had barely begun, write _SUCCESS over a
#: nearly empty partition, and never come back for it -- a permanent, silent
#: gap, one day wide, every day.
#:
#: The step back is a CALENDAR day, done in kea_pipeline.window and unit
#: tested there. Going back 24 hours instead lands at 23:00 on 2026-09-26,
#: because 2026-09-27 is 23 hours long, which emits that date twice and skips
#: the DST day altogether.
LOAD_DATE = "{{ kea_load_date(data_interval_end) }}"


def _spark_task(dag: DAG, task_id: str, job: str, extra: list[str] | None = None,
                outlets: list | None = None) -> DockerOperator:
    """One spark-submit in a throwaway container against the standing master.

    lib/ and contracts/ are mounted as SIBLINGS under spark_jobs/ because the
    schema loader resolves the contracts relative to its own file. Splitting
    them breaks it, so the layout is asserted here as well as in compose.
    """
    return DockerOperator(
        task_id=task_id,
        dag=dag,
        outlets=outlets or [],
        image=SPARK_IMAGE,
        api_version="auto",
        auto_remove="success",
        network_mode=NETWORK,
        # The operator's own /tmp mount is unnecessary here and fails on
        # Docker Desktop's file sharing.
        mount_tmp_dir=False,
        mounts=[
            Mount(source=f"{HOST_PROJECT_DIR}/pipeline/spark_jobs",
                  target="/opt/spark_jobs", type="bind", read_only=True),
            Mount(source=f"{HOST_PROJECT_DIR}/pipeline/lib",
                  target="/opt/spark_jobs/lib", type="bind", read_only=True),
            Mount(source=f"{HOST_PROJECT_DIR}/pipeline/contracts",
                  target="/opt/spark_jobs/contracts", type="bind", read_only=True),
        ],
        environment={
            "MINIO_ENDPOINT": os.environ.get("MINIO_ENDPOINT", "http://minio:9000"),
            "MINIO_ROOT_USER": os.environ.get("MINIO_ROOT_USER", ""),
            "MINIO_ROOT_PASSWORD": os.environ.get("MINIO_ROOT_PASSWORD", ""),
            "KEA_LAKE_BUCKET": os.environ.get("KEA_LAKE_BUCKET", "kea-lake"),
            # METERING ONLY, and taken from the Airflow CONNECTION rather than
            # an environment variable. Two reasons. The credential then has one
            # home -- `AIRFLOW_CONN_KEA_SRC_METERING` -- instead of the same
            # secret written twice and drifting; and `environment` is a
            # templated field, so this resolves when the task RUNS rather than
            # when the DAG is parsed, which is where connection lookups belong.
            #
            # Naming the metering connection explicitly is the point. This DAG
            # reads one system, and handing it billing's credential would void
            # the argument the four roles exist for: that a cross-system read
            # FAILS rather than silently working.
            "KEA_EXTRACT_METERING_PASSWORD": "{{ conn.kea_src_metering.password }}",
            "KEA_SRC_DB_HOST": "{{ conn.kea_src_metering.host }}",
            "KEA_SRC_DB_NAME": "{{ conn.kea_src_metering.schema or 'kea' }}",
        },
        command=[
            "/opt/spark/bin/spark-submit",
            "--master", "spark://spark-master:7077",
            f"/opt/spark_jobs/{job}",
            LOAD_DATE,
            *(extra or []),
        ],
    )


with DAG(
    dag_id="kea_extract_metering",
    description="src_metering.interval_reads -> bronze -> parsed, one NZ day per run",
    schedule="@daily",
    start_date=pendulum.datetime(2024, 9, 18, tz=NZ),
    # The source's arrivals end 2026-10-11; past that a run would extract an
    # empty window every night and look like a broken feed.
    end_date=pendulum.datetime(2026, 10, 12, tz=NZ),
    catchup=False,
    # See _kea_common.scripted_dag: paused DAGs made the asset gate
    # unsatisfiable, so the daily pipeline never ran on its own schedule.
    is_paused_upon_creation=False,
    # A 754-day backfill would otherwise launch 754 Spark applications at a
    # single 2 GB worker. The lane is sequential anyway: parse reads what
    # extract wrote.
    max_active_runs=1,
    default_args={
        "owner": "data",
        "retries": 2,
        "retry_delay": dt.timedelta(minutes=5),
        "depends_on_past": False,
    },
    tags=["stage3", "metering", "extract"],
    doc_md=__doc__,
    # Exposed as a macro rather than inlined as Jinja, so the rule lives in one
    # tested function instead of a string nothing can check.
    user_defined_macros={
        "kea_load_date": lambda fire: window.load_date_for_fire_time(fire).isoformat()
    },
) as dag:
    extract = _spark_task(dag, "extract_interval_reads", "extract_interval_reads.py")
    # THE OUTLET IS ON PARSE, NOT ON EXTRACT. What anything downstream needs is
    # bronze/parsed -- reshaped and conforming to KEA-047. Announcing the asset
    # when the raw partition landed would start kea_daily_pipeline against rows
    # that have not been through the contract yet.
    parse = _spark_task(dag, "parse_interval_reads", "parse_interval_reads.py",
                        outlets=[BRONZE["src_metering"]])

    # Parse reads the partition extract just wrote. The only edge in the lane,
    # and it is a data dependency rather than a convention -- which is why
    # there is no sleep anywhere in this file.
    extract >> parse
