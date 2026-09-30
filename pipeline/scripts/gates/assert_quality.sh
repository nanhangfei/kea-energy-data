#!/usr/bin/env bash
# THE STAGE 7 GATE. Its "Done when" is four claims, and this checks all four:
#
#   an injected defect lands in quarantine WITH ITS REASON,
#   raises an alert,
#   and stops the export
#   -- and the lineage graph covers source to serving.
#
# Three of those are only checkable by BREAKING something, which is the pattern
# this repo already uses: `make ingest-date VERIFY=1` breaks the billing
# credential, `make publish-serving VERIFY=1` plants a failure in a publication.
#
# ⚠️ THIS RUN IS SUPPOSED TO GO RED, and the alert it raises is a real one --
# the on_failure_callback does not know the failure was deliberate. Expect a
# FAILED DagRun in the UI and an entry in the alert sink. Both are the evidence.
#
# ⚠️ IT TAKES ABOUT TEN MINUTES. DEFAULT_ARGS gives every task two retries five
# minutes apart, so a task that is meant to fail has to fail three times before
# Airflow calls it failed. That delay is correct for a transient error and is
# simply the price of proving the gate with the real settings rather than with
# settings invented for the test.
set -euo pipefail
cd "$(dirname "$0")/../../.."             # -> data/

# The served tables are in the backend's database. Read as kea_loader, the
# publisher's own credential, in a read-only session: this only ever looks.
# The SQL travels as a positional argument, so no quoting survives two shells.
served() {
    docker run --rm --network "${COMPOSE_NETWORK:-kea_default}" --env-file .env \
        --add-host=host.docker.internal:host-gateway \
        -e PGOPTIONS='-c default_transaction_read_only=on' postgres:17.10 \
        sh -c 'exec psql -X -tA -v ON_ERROR_STOP=1 -c "$1" "$KEA_PUBLISH_DSN"' sh "$1"
}
AFDB="docker compose exec -T airflow-postgres psql -U airflow -d airflow -X -tA"
# ⚠️ THE PROBE IS APPENDED TO warehouse.yml, NOT DROPPED BESIDE IT. The DAG runs
# `soda scan ... checks/warehouse.yml` -- one file, because a scan takes one
# datasource and pointing it at the directory would run the serving checks
# against Snowflake. So a separate probe file would sit there being ignored, and
# this script would "prove" a gate that never saw it.
CHECKS="pipeline/soda/checks/warehouse.yml"
BACKUP="/tmp/warehouse.yml.$$"
RUN_ID="quality_probe__$(date +%s)"
fails=0

# ⚠️ THE PROBE RUN STAGES REAL DATA before the gate stops it. Without a load
# date the DAG picks the nightly one, which is right on a per-date span layout.
# The demo layout holds ONE generation at one load date in KEA.RAW, and a
# run for any other date stages its partition on top -- duplicate keys, or the
# stale pre-built partitions -- so pass the generation's own date:
#     make run-pipeline LOAD_DATE=2027-08-28 VERIFY=1
PROBE_LOAD_DATE="${1:-}"
trigger_conf=()
if [ -n "$PROBE_LOAD_DATE" ]; then
    [[ "$PROBE_LOAD_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
        || { echo "load date must be YYYY-MM-DD, got '$PROBE_LOAD_DATE'"; exit 2; }
    trigger_conf=(--conf "{\"load_date\": \"$PROBE_LOAD_DATE\"}")
fi

# A paused DAG accepts the trigger and never starts the run, so the wait in
# step 3 would spin forever. Refuse up front, before anything is poisoned.
paused=$($AFDB -c "select is_paused from dag where dag_id='kea_daily_pipeline';" | tr -d '[:space:]')
[ "$paused" = "f" ] \
    || { echo "kea_daily_pipeline is paused (or missing: '$paused'); unpause it first"; exit 2; }

note() { printf '\n== %s\n' "$1"; }
ok()   { printf '   PASS  %s\n' "$1"; }
bad()  { printf '   FAIL  %s\n' "$1"; fails=$((fails + 1)); }

cleanup() {
    # UNCONDITIONAL. A poisoned check left behind would turn every later run of
    # the pipeline red for a reason nobody would connect to this script.
    [ -f "$BACKUP" ] && mv -f "$BACKUP" "$CHECKS"
}
trap cleanup EXIT

# =====================================================================
note "1. the planted defects are in quarantine, with their reasons"
# Stage 2 injects these deliberately -- three orphan ICPs and two unknown plan
# codes -- and 07_billing_defects.sql announces exactly that. The injector and
# the detector have never spoken to each other, which is what makes this
# evidence rather than a tautology.
#
# ⚠️ ASSERTED IN THE WAREHOUSE, NOT IN SERVING, AND THE REASON IS A REAL GAP
# THIS CHECK FOUND. The warehouse mart holds all five with their reasons. The
# serving copy holds NONE of them -- not because the export is wrong, but
# because a quarantined account is by definition one that cannot satisfy the
# serving schema: billing.account requires BOTH icp_id and plan_id NOT NULL,
# the three orphans have no ICP at all, and the other two are on
# ECO_GONE_VECT_STA, which has zero versions in dim_plan.
#
# So design 6.3's "quarantine rates surface to the product" does not hold end to
# end: is_quarantined and quarantine_reason exist on kea.mart_account_context
# and are, on this data, always false and null. Recorded rather than papered
# over -- see the Stage 7 notes in the stack doc.
warehouse_q=$(docker run --rm --network "${COMPOSE_NETWORK:-kea_default}" --env-file .env \
    -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
    -v "$(grep -E '^SNOWFLAKE_PRIVATE_KEY_HOST=' .env | cut -d= -f2-)":/keys/snowflake.p8:ro \
    dataeng/dbt:1.12.0 python3 -c "
import os, snowflake.connector
c = snowflake.connector.connect(account=os.environ['SNOWFLAKE_ACCOUNT'], user=os.environ['SNOWFLAKE_USER'],
    private_key_file=os.environ['SNOWFLAKE_PRIVATE_KEY_PATH'], role=os.environ['SNOWFLAKE_ROLE'],
    warehouse=os.environ['SNOWFLAKE_WAREHOUSE'], database=os.environ['SNOWFLAKE_DATABASE'])
q = c.cursor()
q.execute('select count_if(is_quarantined), count_if(is_quarantined and reject_reasons is null) '
          'from KEA.KEA_MARTS.MART_ACCOUNT_CONTEXT')
n, blank = q.fetchone(); print(f'{n} {blank}')" 2>/dev/null | tail -1)
set -- $warehouse_q
[ "${1:-0}" -ge 1 ] && ok "${1} accounts quarantined in the warehouse" \
                    || bad "nothing is quarantined -- the injector or the detector is broken"
[ "${2:-1}" = "0" ] && ok "every one carries its reason" \
                    || bad "${2} quarantined accounts have no reason"

served_q=$(served "select count(*) from kea.mart_account_context where is_quarantined;")
[ "$served_q" = "0" ] \
  && ok "0 reach serving -- structurally impossible, see the comment above" \
  || ok "$served_q reach serving (the schema has been relaxed since this was written)"

# =====================================================================
note "2. state before the injected failure"
before_counts=$(served "select count(*) from metering.meter_read_hh;")
before_synced=$(served "select extract(epoch from max(synced_at))::bigint from billing.account;")
echo "   reads=$before_counts synced_at=$before_synced"
curl -s -X POST http://localhost:9099/reset >/dev/null || true

# A Soda check that cannot pass, appended to the gate's own file. It sits after
# dbt_test and before the export, so tripping it is exactly design 10's scenario.
cp "$CHECKS" "$BACKUP"
cat >> "$CHECKS" <<'YAML'

# TEMPORARY, appended and removed by assert_quality.sh.
checks for DIM_ICP:
  - deliberately_impossible = 1:
      deliberately_impossible query: |
        SELECT 0
      name: Quality-gate probe -- always fails, on purpose
YAML

note "3. running the pipeline with the gate poisoned (~10 min: two retries)"
echo "   load date: ${PROBE_LOAD_DATE:-nightly default of the DAG}"
# The `+` form expands to nothing for an empty array under `set -u` on bash 3.2.
docker compose exec -T airflow-scheduler airflow dags trigger kea_daily_pipeline \
    --run-id "$RUN_ID" ${trigger_conf[@]+"${trigger_conf[@]}"} >/dev/null 2>&1 \
    || { echo "could not trigger"; exit 1; }
while :; do
    state=$($AFDB -c "select state from dag_run where run_id='$RUN_ID';" | tr -d '[:space:]')
    case "$state" in
        success|failed) break ;;
    esac
    printf '\r   %s...   ' "${state:-queued}"; sleep 15
done
printf '\r   run finished: %s          \n' "$state"

# =====================================================================
note "4. the gate stopped the export"
[ "$state" = "failed" ] && ok "the DagRun failed, as it must" \
                        || bad "the run reported $state with a check that cannot pass"
gate=$($AFDB -c "select state from task_instance where run_id='$RUN_ID' and task_id='soda_scan_warehouse';" | tr -d '[:space:]')
[ "$gate" = "failed" ] && ok "soda_scan_warehouse failed" || bad "the gate task is '$gate'"

for downstream in export_warehouse publish_serving soda_scan_serving assert_serving; do
    st=$($AFDB -c "select coalesce(state,'never_ran') from task_instance
                   where run_id='$RUN_ID' and task_id='$downstream';" | tr -d '[:space:]')
    case "$st" in
        upstream_failed|never_ran|"") ok "$downstream did not run ($st)" ;;
        *)                            bad "$downstream ran anyway ($st)" ;;
    esac
done

after_counts=$(served "select count(*) from metering.meter_read_hh;")
after_synced=$(served "select extract(epoch from max(synced_at))::bigint from billing.account;")
[ "$before_counts" = "$after_counts" ] && ok "the served tables are unchanged" \
                                       || bad "reads moved: $before_counts -> $after_counts"
# synced_at is the sharper of the two: it moves on EVERY publication, so an
# unchanged value proves none started rather than that one happened to be a no-op.
[ "$before_synced" = "$after_synced" ] && ok "synced_at did not move -- Postgres serves yesterday" \
                                       || bad "synced_at moved: a publication ran"

# =====================================================================
note "5. an alert fired"
alerts=$(curl -s http://localhost:9099/alerts || echo '[]')
if echo "$alerts" | grep -q "soda_scan_warehouse"; then
    ok "the alert sink received one naming soda_scan_warehouse"
else
    bad "no alert names the failed task; sink holds: $(echo "$alerts" | head -c 200)"
fi

# =====================================================================
note "6. the lineage graph covers source to serving"
MQ="docker compose exec -T marquez-db psql -U marquez -d marquez -X -tA"
datasets=$($MQ -c "select count(*) from datasets;" | tr -d '[:space:]')
edges=$($MQ -c "select count(*) from job_versions_io_mapping;" | tr -d '[:space:]')
[ "$datasets" -gt 0 ] && ok "$datasets datasets, $edges edges" \
                      || bad "the graph has no datasets -- job nodes only"

# ⚠️ A CONNECTED PATH, NOT THREE PRESENT NODES. The first version of this check
# counted a source dataset, a serving dataset and some warehouse datasets, and
# passed while the graph was BROKEN IN THE MIDDLE: parse wrote
# `.../load_date=2026-09-02` and the Snowflake stage read
# `.../load_date=2026-09-03`, so Marquez held two unrelated nodes with no edge.
# Both ends existed. Nothing joined them. "Covers source to serving" is a
# statement about reachability, so reachability is what this walks.
reached=$($MQ -c "
with recursive edges as (
    select i.dataset_uuid as src, o.dataset_uuid as dst
    from job_versions_io_mapping i
    join job_versions_io_mapping o on o.job_version_uuid = i.job_version_uuid
    where i.io_type = 'INPUT' and o.io_type = 'OUTPUT'),
reach as (
    select uuid from datasets where name = 'kea.src_metering.interval_reads'
    union
    select e.dst from edges e join reach r on e.src = r.uuid)
select count(*) from reach r join datasets d on d.uuid = r.uuid
 where d.namespace_name like 'postgres%' and d.name like 'kea.kea.mart\\_%';" | tr -d '[:space:]')
[ "${reached:-0}" -ge 1 ] \
  && ok "$reached serving marts are REACHABLE from the source table" \
  || bad "no path from kea.src_metering.interval_reads to any serving mart"

# And the path really does go through the warehouse rather than around it.
via_wh=$($MQ -c "
with recursive edges as (
    select i.dataset_uuid as src, o.dataset_uuid as dst
    from job_versions_io_mapping i
    join job_versions_io_mapping o on o.job_version_uuid = i.job_version_uuid
    where i.io_type = 'INPUT' and o.io_type = 'OUTPUT'),
reach as (
    select uuid from datasets where name = 'kea.src_metering.interval_reads'
    union
    select e.dst from edges e join reach r on e.src = r.uuid)
select count(*) from reach r join datasets d on d.uuid = r.uuid
 where d.namespace_name like 'snowflake%';" | tr -d '[:space:]')
[ "${via_wh:-0}" -ge 1 ] && ok "$via_wh warehouse datasets lie on that path" \
                        || bad "the path does not pass through Snowflake"

# =====================================================================
printf '\n'
if [ "$fails" -ne 0 ]; then
    echo "$fails check(s) FAILED"
    exit 1
fi
echo "Quality gates: quarantine carries reasons, the gate stopped the export,"
echo "an alert fired, and the lineage graph runs source -> warehouse -> serving."
