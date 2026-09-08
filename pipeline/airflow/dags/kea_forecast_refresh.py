"""Rebuild the forecast inputs alone, without re-running the whole path.

Design §9 lists this as one of the DAGs with "a different cadence":
`kea_forecast_refresh` (mid-cycle, could be more frequent than the main
pipeline). It is the forecast slice of `kea_daily_pipeline` -- one mart, its
safety tests, one export, one load and one output build -- and nothing else.

⚠️ ITS SCHEDULE IS `None`, AND THAT IS THE HONEST ANSWER RATHER THAN A GAP.

"More frequent than the main pipeline" needs something to be more frequent
ABOUT. Every input to `mart_forecast_input` arrives on the nightly extract:
meter reads, invoices, weather. Running this at midday would recompute
identical numbers from identical rows, burn Snowflake credits on a fixed trial,
and put a green run in the grid that means nothing -- which is worse than no
run at all, because someone will eventually trust it as evidence the forecast
is current.

So it is manually triggered, and it earns its cadence the day an intra-day
source exists. What it is genuinely for today:

  * re-running the forecast slice after a model change, without a full
    `dbt run` and a 10.7M-row export
  * rebuilding both sides of the feature: `kea.mart_forecast_input` holds the
    warehouse-approved INPUTS, then `build_bill_forecasts.py` writes the
    customer-facing amounts and confidence bands to `kea.bill_forecast`

    airflow dags trigger kea_forecast_refresh

THE SINGLE-TABLE PATH IS STAGE 5'S, NOT A SECOND ONE. Both scripts take an
optional table argument, and `load_export_to_serving.py` already distinguishes
a one-table run from a full one: `full = len(tables) > 1` is False here, so the
table gets its own strategy rather than the whole-set clear that a replica load
performs. No new code path, which is the same argument §11 makes about backfill.
"""

from __future__ import annotations

import os
import sys

import pendulum
from airflow.models import DAG

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# From the shared module, NOT from kea_daily_pipeline. Importing one DAG file
# into another executes its `with DAG(...)` block as a side effect of the
# import, which registers that DAG from a second file and is exactly the kind of
# thing that works until it does not.
from _kea_common import (  # noqa: E402
    DBT_IMAGE, DEFAULT_ARGS, HOST_PROJECT_DIR, LAKE_ENV, NZ, SNOWFLAKE_ENV,
    _container, _key_mount,
)
from docker.types import Mount  # noqa: E402

MART = "mart_forecast_input"
SERVING_TABLE = "kea.mart_forecast_input"

with DAG(
    dag_id="kea_forecast_refresh",
    description="rebuild mart_forecast_input and land it, without the full pipeline",
    schedule=None,
    start_date=pendulum.datetime(2024, 9, 18, tz=NZ),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["stage6", "forecast"],
    doc_md=__doc__,
) as dag:

    # `-s` selects one model. dbt deps first for the same reason the pipeline
    # does it: without dbt_utils the run fails before touching a model.
    build = _container(
        dag, "dbt_run_forecast_mart", DBT_IMAGE,
        ["sh", "-lc", f"dbt deps && dbt run --profiles-dir . -s {MART}"],
        mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline/dbt_project",
                      target="/dbt", type="bind"), *_key_mount()],
        environment=SNOWFLAKE_ENV,
        working_dir="/dbt",
    )

    # The output job treats is_forecastable as permission to publish a monetary
    # projection, so its focused tests must gate this focused export too.
    test = _container(
        dag, "dbt_test_forecast_mart", DBT_IMAGE,
        ["dbt", "test", "--profiles-dir", ".", "-s", MART],
        mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline/dbt_project",
                      target="/dbt", type="bind"), *_key_mount()],
        environment=SNOWFLAKE_ENV,
        working_dir="/dbt",
    )

    export = _container(
        dag, "export_forecast_mart", DBT_IMAGE,
        ["python3", "export_warehouse_to_lake.py", SERVING_TABLE],
        mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline", target="/opt/pipeline",
                      type="bind", read_only=True), *_key_mount()],
        environment={**SNOWFLAKE_ENV, **LAKE_ENV},
        working_dir="/opt/pipeline/scripts",
    )

    load = _container(
        dag, "load_forecast_mart", DBT_IMAGE,
        ["python3", "load_export_to_serving.py", SERVING_TABLE],
        mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline", target="/opt/pipeline",
                      type="bind", read_only=True)],
        environment={
            **LAKE_ENV,
            "KEA_LOADER_PASSWORD": os.environ.get("KEA_LOADER_PASSWORD", ""),
            "KEA_SERVING_HOST": "kea-serving-db",
            "DBT_INVOCATION_ID": "{{ run_id }}",
        },
        working_dir="/opt/pipeline/scripts",
    )

    forecast = _container(
        dag, "build_bill_forecasts", DBT_IMAGE,
        ["python3", "build_bill_forecasts.py"],
        mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline", target="/opt/pipeline",
                      type="bind", read_only=True)],
        environment={
            "KEA_LOADER_PASSWORD": os.environ.get("KEA_LOADER_PASSWORD", ""),
            "KEA_SERVING_HOST": "kea-serving-db",
        },
        working_dir="/opt/pipeline/scripts",
    )

    build >> test >> export >> load >> forecast
