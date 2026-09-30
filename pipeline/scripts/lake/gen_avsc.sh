#!/usr/bin/env bash
# Regenerate the sixteen derived Avro contracts. See gen_avsc.py for why they
# are derived and KEA-047 is not.
#
# Runs inside the Airflow image because that is where psycopg2 lives, and
# passes the four extract credentials through -- a table this cannot describe
# is a table the pipeline cannot read, which is worth finding out here.
set -euo pipefail
cd "$(dirname "$0")/../../.."             # -> data/
[ -f .env ] || { echo "no .env" >&2; exit 1; }
set -a; . ./.env; set +a

docker compose exec -T \
    -e KEA_EXTRACT_METERING_PASSWORD="${KEA_EXTRACT_METERING_PASSWORD}" \
    -e KEA_EXTRACT_BILLING_PASSWORD="${KEA_EXTRACT_BILLING_PASSWORD}" \
    -e KEA_EXTRACT_CATALOGUE_PASSWORD="${KEA_EXTRACT_CATALOGUE_PASSWORD}" \
    -e KEA_EXTRACT_WEATHER_PASSWORD="${KEA_EXTRACT_WEATHER_PASSWORD}" \
    airflow-scheduler python /opt/airflow/scripts/lake/gen_avsc.py "$@"
