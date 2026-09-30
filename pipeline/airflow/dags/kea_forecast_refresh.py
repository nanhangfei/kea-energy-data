"""Rebuild the forecast inputs alone, without re-running the whole path.

Design §9 lists this as one of the DAGs with "a different cadence":
`kea_forecast_refresh` (mid-cycle, could be more frequent than the main
pipeline). It is the forecast slice of `kea_daily_pipeline` -- one mart, its
safety tests and one export -- followed by the same publish the daily pipeline
ends with, and nothing else.

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
    warehouse-approved INPUTS, and the publish recomputes the customer-facing
    amounts and confidence bands in `kea.bill_forecast` from them

    airflow dags trigger kea_forecast_refresh

IT PUBLISHES A RELEASE, NOT ONE TABLE. Only the forecast mart is rebuilt and
re-exported, but the last step is `_kea_common._publish`, the daily pipeline's
own: every served table is loaded from its sealed export, the Sprint 3
decisions are recomputed from the result, and the release is recorded in
data_ops.serving_publication under this run's id -- in one transaction. The
other tables' exports are the ones the last daily run sealed, so their rows
come back unchanged; what the release changes is the forecast mart and every
decision derived from it, together.

It used to load the one table and then build the forecasts, as two commits and
no publication record. Between them the application could read new inputs
beside old forecasts, and nothing recorded which release it was reading.
The Makefile targets that did the same by hand (load-serving,
build-forecasts) are gone too: every route into the backend is a release.
"""

from __future__ import annotations

import os
import sys

import pendulum
from airflow.models import DAG
from airflow.sdk import TaskGroup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# From the shared module, NOT from kea_daily_pipeline. Importing one DAG file
# into another executes its `with DAG(...)` block as a side effect of the
# import, which registers that DAG from a second file and is exactly the kind of
# thing that works until it does not.
from _kea_common import (  # noqa: E402
    DBT_IMAGE, DEFAULT_ARGS, HOST_PROJECT_DIR, LAKE_ENV, LINEAGE_ENV, NZ,
    SNOWFLAKE_ENV, _container, _key_mount, _publish,
)
from docker.types import Mount  # noqa: E402

MART = "mart_forecast_input"
SERVING_TABLE = "kea.mart_forecast_input"

with DAG(
    dag_id="kea_forecast_refresh",
    description="rebuild mart_forecast_input and publish a release, without the full pipeline",
    schedule=None,
    start_date=pendulum.datetime(2024, 9, 18, tz=NZ),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["orchestration", "build-warehouse", "publish-serving", "forecast"],
    doc_md=__doc__,
) as dag:

    # The same two steps as `make build-warehouse` and `make publish-serving`,
    # sliced to one mart; grouped under their names, task ids unchanged.
    with TaskGroup("build-warehouse", prefix_group_id=False,
                   tooltip="make build-warehouse, one mart: dbt run and test"):
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

        # The forecast job treats is_forecastable as permission to publish a
        # monetary projection, so its focused tests must gate this focused export.
        test = _container(
            dag, "dbt_test_forecast_mart", DBT_IMAGE,
            ["dbt", "test", "--profiles-dir", ".", "-s", MART],
            mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline/dbt_project",
                          target="/dbt", type="bind"), *_key_mount()],
            environment=SNOWFLAKE_ENV,
            working_dir="/dbt",
        )

    with TaskGroup("publish-serving", prefix_group_id=False,
                   tooltip="make publish-serving: export this mart, publish a release"):
        # One table's export, sealed by its own _SUCCESS; the other tables keep
        # the exports the last daily run sealed, which is what the publish reads.
        export = _container(
            dag, "export_forecast_mart", DBT_IMAGE,
            ["python3", "warehouse/export_warehouse_to_lake.py", SERVING_TABLE],
            mounts=[Mount(source=f"{HOST_PROJECT_DIR}/pipeline", target="/opt/pipeline",
                          type="bind", read_only=True), *_key_mount()],
            environment={**SNOWFLAKE_ENV, **LAKE_ENV, **LINEAGE_ENV},
            working_dir="/opt/pipeline/scripts",
        )

        publish = _publish(dag)

    build >> test >> export >> publish
