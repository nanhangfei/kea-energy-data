#!/usr/bin/env bash
# Stage 5's second exit condition: a failed publication leaves the served
# tables unchanged.
#
# The other two conditions are checkable by looking at the result -- publish
# twice and compare counts, read the gate's output. This one is only checkable
# by BREAKING something, because what it asserts is about the path not taken.
# So this makes a publication fail on purpose, as late as one can fail, and
# proves that nothing before the failure moved.
#
# ON A THROWAWAY POSTGRES, NEVER THE BACKEND'S. The failure is planted by
# altering a table, and the served tables share a database with the
# application; a probe left behind there by a killed run would fail every
# publication after it. So this starts its own postgres:16.15-alpine -- the
# release kea-db runs -- on the pipeline network, migrates it from
# migrations/serving as `make publish-serving MIGRATE=1` would, publishes the current
# lake export into it as a baseline, then publishes again with the failure
# planted. The container and its volume go when the script exits, however it
# exits. There is no --add-host: nothing here has any business reaching the
# backend, and every DSN is overridden to name the throwaway.
#
# HOW THE FAILURE IS PLANTED. A CHECK constraint on data_ops.serving_publication
# that refuses this run's probe release id. Recording the release is the
# publication's LAST statement, so by the time the constraint fires every
# served table has been replaced, the sequences synced, and every decision
# computed and validated, all inside the open transaction. (The previous
# version of this test failed at kea.mart_account_context, before any decision
# ran.) Not a corrupted Parquet file: that would test the exporter's error
# handling, and what is under test here is the publisher's transaction.
#
# kea.mart_load_log is NOT compared. A failed publication writes its rows there
# after its transaction has ended, on purpose -- publish_serving.py says why --
# and this checks that it did.
#
# Lineage is off for both publications: the throwaway is not kea-db, and
# events naming kea-db would tell Marquez it had been updated.
#
# ⚠️ IT PUBLISHES THE WHOLE EXPORT TWICE, reading lake export/ from MinIO, so
# the stack must be up and a `make publish-serving` must have exported.
set -euo pipefail
cd "$(dirname "$0")/../../.."             # -> data/

NET="${COMPOSE_NETWORK:-kea_default}"
NAME="kea-atomic-probe-$$"
STAMP=$(date +%Y%m%dT%H%M%S)
BASE="atomic-base-$STAMP"
PROBE="atomic-probe-$STAMP"
LOG=$(mktemp)

# Throwaway credentials, exported rather than written on a command line. The
# bare `-e NAME` form reads each one from here, and wins over the same name in
# .env -- which is what keeps every connection below off the backend.
secret() { python3 -c 'import secrets; print(secrets.token_hex(16))'; }
POSTGRES_PASSWORD=$(secret)
KEA_LOADER_PASSWORD=$(secret)
KEA_APP_PASSWORD=$(secret)
KEA_MIGRATION_DSN="postgresql://kea:$POSTGRES_PASSWORD@$NAME:5432/kea"
KEA_PUBLISH_DSN="postgresql://kea_loader:$KEA_LOADER_PASSWORD@$NAME:5432/kea"
export POSTGRES_PASSWORD KEA_LOADER_PASSWORD KEA_APP_PASSWORD KEA_MIGRATION_DSN KEA_PUBLISH_DSN

cleanup() {
    docker rm -f -v "$NAME" >/dev/null 2>&1 || true
    rm -f "$LOG"
}
trap cleanup EXIT

PSQL="docker exec -i $NAME psql -X -q -v ON_ERROR_STOP=1 -U kea -d kea"

run() {   # a pipeline script, against the throwaway and nothing else
    docker run --rm --network "$NET" --env-file .env \
        -e KEA_MIGRATION_DSN -e KEA_PUBLISH_DSN -e KEA_LOADER_PASSWORD -e KEA_APP_PASSWORD \
        -e OPENLINEAGE_DISABLED=true \
        -v "$(pwd)":/opt/data:ro -v "$(pwd)/../services/api":/opt/api:ro -e KEA_API_DIR=/opt/api \
        -w /opt/data/pipeline/scripts dataeng/dbt:1.12.0 python3 "$@"
}

counts() {
    $PSQL -tA <<'SQL'
select 'network',        count(*) from metering.network
union all select 'icp',            count(*) from metering.icp
union all select 'plan',           count(*) from catalogue.plan
union all select 'customer',       count(*) from billing.customer
union all select 'account',        count(*) from billing.account
union all select 'bill',           count(*) from billing.bill
union all select 'bill_line',      count(*) from billing.bill_line
union all select 'read_hh',        count(*) from metering.meter_read_hh
union all select 'daily_usage',    count(*) from kea.mart_daily_usage
union all select 'acct_context',   count(*) from kea.mart_account_context
union all select 'forecast',       count(*) from kea.bill_forecast
union all select 'recommendation', count(*) from kea.plan_recommendation
union all select 'bill_example',   count(*) from kea.plan_bill_example
union all select 'assessment',     count(*) from kea.hardship_assessment
union all select 'signal',         count(*) from kea.hardship_signal
union all select 'alert',          count(*) from kea.alert
union all select 'publication',    count(*) from data_ops.serving_publication
union all select 'synced_at',      extract(epoch from max(synced_at))::bigint from billing.account
order by 1;
SQL
}

echo "== a throwaway Postgres 16: $NAME"
docker run -d --rm --name "$NAME" --network "$NET" \
    -e POSTGRES_USER=kea -e POSTGRES_DB=kea -e POSTGRES_PASSWORD -e TZ=UTC \
    postgres:16.15-alpine >/dev/null
# Over TCP, not the socket: during initdb the entrypoint runs a temporary
# server with TCP off, and a socket check would pass against that one.
for _ in $(seq 60); do
    docker exec "$NAME" pg_isready -h 127.0.0.1 -U kea -d kea -q && break
    sleep 1
done
docker exec "$NAME" pg_isready -h 127.0.0.1 -U kea -d kea -q \
    || { echo "the throwaway Postgres did not start" >&2; exit 1; }

echo
echo "== migrating it, setting its role passwords, publishing a baseline"
run serving/migrate_serving.py
run serving/configure_serving_roles.py
if ! run serving/publish_serving.py --release-id "$BASE" >"$LOG" 2>&1; then
    tail -20 "$LOG" >&2
    echo "the baseline publication failed, so nothing has been tested" >&2
    exit 1
fi
echo "  published $BASE"

echo
echo "== before =="
before=$(counts)
echo "$before" | sed 's/^/  /'

echo
echo "== planting a failure at the publication's last statement =="
$PSQL -c "ALTER TABLE data_ops.serving_publication
          ADD CONSTRAINT _atomicity_probe CHECK (release_id <> '$PROBE');"
echo "  CHECK (release_id <> '$PROBE')"

echo
echo "== publishing again, which must fail =="
if run serving/publish_serving.py --release-id "$PROBE" --run-id "$PROBE" >"$LOG" 2>&1; then
    echo "  THE PUBLICATION SUCCEEDED. The probe did not fire, so this proves nothing." >&2
    exit 1
fi
# It has to be the probe that stopped it. A publication that died earlier, for
# any other reason, would leave everything unchanged too -- and prove nothing.
if ! grep -q '_atomicity_probe' "$LOG"; then
    echo "  It failed, but not at the probe:" >&2
    tail -20 "$LOG" >&2
    exit 1
fi
grep -m1 '_atomicity_probe' "$LOG" | sed 's/^ *//; s/^/  /'

echo
echo "== after =="
after=$(counts)
echo "$after" | sed 's/^/  /'
logged=$($PSQL -tA -c "select count(*) from kea.mart_load_log
                        where warehouse_run_id = '$PROBE' and status = 'failed';")

echo
if [ "$before" != "$after" ]; then
    echo "FAIL: something moved across a failed publication" >&2
    diff <(echo "$before") <(echo "$after") >&2 || true
    exit 1
fi
if [ "${logged:-0}" -lt 1 ]; then
    echo "FAIL: the failed publication did not record itself in kea.mart_load_log" >&2
    exit 1
fi
echo "PASS: every served table, every decision table, synced_at and the release"
echo "      history are exactly as the baseline left them, although the failure"
echo "      came at the publication's last statement. kea.mart_load_log recorded it."
