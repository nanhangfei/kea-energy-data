#!/usr/bin/env bash
# Compact, deterministic Sprint 2 + Sprint 3 demonstration rebuild.
#
# Production/backfill semantics stay in rebuild_span.sh. This profile keeps a
# 50-household cohort, the complete two-year interval span needed to fit the
# weather baseline, every small billing event, and all nine hand-written Sprint
# 2 fixtures. The
# multi-day meter extract is deliberately one sealed demo partition so Spark
# starts once rather than twice for each of 757 dates.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

COHORT_SIZE="${DEMO_COHORT_SIZE:-50}"
KEY_HOST=$(grep -E '^SNOWFLAKE_PRIVATE_KEY_HOST=' .env | cut -d= -f2-)
DOCKER_RUN="docker run --rm -i --network kea_default --env-file .env \
  -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
  -v ${KEY_HOST}:/keys/snowflake.p8:ro \
  -v $(pwd)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts"
ANALYTICS_RUN="$DOCKER_RUN -v $(pwd)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api"
SPARK="docker compose exec -T spark-master /opt/spark/bin/spark-submit --master spark://spark-master:7077"
PSQL="docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d kea"

read -r FROM TO <<<"$(python3 - <<'PY'
import datetime as dt
import sys
sys.path.insert(0, 'pipeline/scripts/gen_source')
import uk_to_nz as tz
to = tz.NZ_SPAN_END + dt.timedelta(days=1)
# The recommendation needs only the trailing year, but the bill forecast must
# fit the household against historical weather first. At ~2.1M source rows the
# complete 50-household span remains comfortably inside the 30-minute target.
start = tz.nz_local_date_for(tz.LCL_START, tz.derive_shift_days())
print(start.isoformat(), to.isoformat())
PY
)"

start_ts=$(date +%s)
echo "demo rebuild: ${COHORT_SIZE} households, meter arrivals ${FROM} -> ${TO}"

echo "== generate and load the compact source cohort"
python3 pipeline/scripts/gen_source/cohort.py --size "$COHORT_SIZE"
python3 pipeline/scripts/gen_source/gen_metering.py --stats
python3 pipeline/scripts/gen_source/gen_metering.py
python3 pipeline/scripts/gen_source/gen_reference.py
python3 pipeline/scripts/gen_source/gen_billing.py
$PSQL -q -f - < pipeline/scripts/gen_source/00_clean_generated.sql
pipeline/scripts/gen_source/load_source.sh

echo "== extract the meter range in one Spark job"
$SPARK /opt/spark_jobs/extract_interval_reads.py "$TO" --from-date "$FROM" --force
$SPARK /opt/spark_jobs/parse_interval_reads.py "$TO" --from-date "$FROM" --force

echo "== extract snapshots and complete small billing event tables once"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 - "$TO" <<'PY'
import sys
sys.path.insert(0, '/opt/pipeline/lib')
import extract_small
from kea_pipeline import spec

load_date = sys.argv[1]
for entity in spec.ENTITIES:
    if entity.qualified in spec.SPARK_ENTITIES:
        continue
    args = [entity.qualified, load_date, '--force']
    if entity.mode == spec.INCREMENTAL:
        args.append('--full-incremental')
    extract_small.main(args)
PY

echo "== recreate RAW from the current Avro contracts and stage one generation"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 - <<'PY'
import os
import snowflake.connector
import sys
sys.path.insert(0, '/opt/pipeline/lib')
sys.path.insert(0, '/opt/pipeline/scripts')
from kea_pipeline import spec
from load_bronze_to_snowflake import table_name

conn = snowflake.connector.connect(
    account=os.environ['SNOWFLAKE_ACCOUNT'], user=os.environ['SNOWFLAKE_USER'],
    private_key_file=os.environ['SNOWFLAKE_PRIVATE_KEY_PATH'],
    role=os.environ['SNOWFLAKE_ROLE'], warehouse=os.environ['SNOWFLAKE_WAREHOUSE'],
    database=os.environ['SNOWFLAKE_DATABASE'].upper())
for entity in spec.ENTITIES:
    table = table_name(entity)
    # A rebuild is also the schema-evolution boundary. TRUNCATE would preserve
    # an old table definition (for example accounts before TERM_MONTHS) and
    # make a current contract impossible to load.
    conn.cursor().execute(f'DROP TABLE IF EXISTS KEA.RAW."{table}"')
    print(f'  {table}: dropped')
conn.close()
PY
$DOCKER_RUN dataeng/dbt:1.12.0 python3 load_all_bronze_to_snowflake.py "$TO"

echo "== dbt build"
docker run --rm --network kea_default --env-file .env \
  -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
  -v "${KEY_HOST}":/keys/snowflake.p8:ro \
  -v "$(pwd)/pipeline/dbt_project":/dbt -w /dbt dataeng/dbt:1.12.0 \
  dbt build --profiles-dir .

echo "== export warehouse -> lake"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 export_warehouse_to_lake.py

# Decision records may belong to an older or larger generation and reference
# rows the compact load will remove. They are explicitly reset here; normal
# nightly loads never do this. The Sprint 3 jobs recreate them below.
echo "== reset old demo decisions and load serving"
# Existing volumes need the one additive schema migration before the export's
# new safety-gate column can land. The statement is safe on every re-run.
$PSQL -q -f - < pipeline/migrations/2026-09-07_bill_forecast_input_gate.sql
$PSQL -q -f - < pipeline/migrations/2026-09-07_plan_bill_examples.sql
$PSQL -q <<'SQL'
TRUNCATE TABLE kea.alert, kea.hardship_signal, kea.hardship_assessment,
               kea.plan_bill_example, kea.plan_recommendation,
               kea.bill_forecast RESTART IDENTITY;
SQL
$DOCKER_RUN dataeng/dbt:1.12.0 python3 load_export_to_serving.py

echo "== build and verify the Sprint 3 decision outputs"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 build_bill_forecasts.py
$ANALYTICS_RUN dataeng/dbt:1.12.0 python3 build_plan_recommendations.py
$ANALYTICS_RUN dataeng/dbt:1.12.0 python3 verify_bill_reproduction.py
$ANALYTICS_RUN dataeng/dbt:1.12.0 python3 build_hardship_assessments.py
$DOCKER_RUN dataeng/dbt:1.12.0 python3 create_hardship_alerts.py

echo "== refresh the analysis rollups used by Metabase"
$PSQL -q -f - < analysis/metabase/source_views.sql
$SPARK /opt/analysis/metabase/summarise_lake.py

echo "demo rebuild finished in $(( ($(date +%s) - start_ts) / 60 )) minutes"
