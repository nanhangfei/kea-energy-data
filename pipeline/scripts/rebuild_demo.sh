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
# The served tables live in the backend's database, reached through the Docker
# host; on Linux this --add-host is what defines the name. The publish reads
# migrations/serving beside pipeline/, so it mounts all of data/, and the API's
# pricing engine read-only, as the daily pipeline does.
BACKEND="--add-host=host.docker.internal:host-gateway"
DATA_RUN="docker run --rm -i --network kea_default --env-file .env $BACKEND \
  -v $(pwd):/opt/data:ro -v $(pwd)/../services/api:/opt/api:ro -e KEA_API_DIR=/opt/api \
  -w /opt/data/pipeline/scripts"
SPARK="docker compose exec -T spark-master /opt/spark/bin/spark-submit --master spark://spark-master:7077"
# The pipeline's own Postgres: the source systems and the audit schemas.
PSQL="docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea"

# ⚠️ THE REBUILD RESETS DECISION HISTORY in the database it publishes to (see
# below), so it refuses anything but this machine's database -- before spending
# twenty minutes, not after. To publish to a shared database, point the DSNs at
# it and use `make publish-serving`, which deletes no history.
target=$(grep -E '^KEA_MIGRATION_DSN=' .env | cut -d= -f2- | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.stdin.read().strip()).hostname or "")')
case "$target" in
  host.docker.internal|localhost|127.0.0.1|kea-db) ;;
  *) echo "refusing: KEA_MIGRATION_DSN names '${target}', not this machine's kea-db" >&2; exit 1 ;;
esac

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

echo "== migrate the backend's served schemas"
# Idempotent: applies only what data_ops.schema_migration has not recorded.
$DATA_RUN dataeng/dbt:1.12.0 python3 migrate_serving.py
$DATA_RUN dataeng/dbt:1.12.0 python3 configure_serving_roles.py

# Decision records may belong to an older or larger generation and reference
# rows the compact publish will remove. They are explicitly reset here, as the
# owner; a publish never deletes history. The publish below recreates them.
# Sprint 4's three job tables are listed for the same reason, and each must be:
# hardship_outlook references hardship_assessment, so a TRUNCATE that left it
# out would fail; solar_estimate and ev_scenario reference the accounts and
# plans the publish replaces, so the old rows would fail its deferred key check.
# NO RESTART IDENTITY: the backend's own tables may hold these ids without a
# foreign key, and re-issuing one would quietly re-point them.
echo "== reset old demo decisions"
docker run --rm -i --network kea_default --env-file .env $BACKEND postgres:17.10 \
  sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 "$KEA_MIGRATION_DSN"' <<'SQL'
TRUNCATE TABLE kea.alert, kea.hardship_outlook, kea.hardship_signal,
               kea.hardship_assessment, kea.plan_bill_example,
               kea.plan_recommendation, kea.bill_forecast,
               kea.solar_estimate, kea.ev_scenario;
SQL

# One transaction, the daily pipeline's own code path: the base tables, then
# forecasts, recommendations, the bill-reproduction gate, hardship and pending
# alerts. If any of it fails, no served table changes.
echo "== publish into the backend's database"
$DATA_RUN dataeng/dbt:1.12.0 python3 publish_serving.py --release-id "demo-$(date +%Y%m%dT%H%M%S)"

echo "== refresh the analysis rollups used by Metabase"
$PSQL -q -f - < analysis/metabase/source_views.sql
$SPARK /opt/analysis/metabase/summarise_lake.py

echo "demo rebuild finished in $(( ($(date +%s) - start_ts) / 60 )) minutes"
