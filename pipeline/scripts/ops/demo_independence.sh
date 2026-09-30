#!/usr/bin/env bash
# Stage 3, checkbox 1: "a billing failure does not block metering".
#
# THIS IS NOT PROVABLE BY INSPECTION. Reading the DAG files and seeing three
# dag_ids tells you the author intended independence. The only evidence is a
# billing failure that leaves metering green with its partition written, so
# this breaks billing on purpose and shows what happens.
#
# It breaks the CREDENTIAL rather than the config, because that is the failure
# that actually occurs -- a rotated password nobody told the pipeline about --
# and because it needs no container restart, so metering keeps running
# throughout. set_extract_roles.sh restores it, and this script does that itself.
#
# It uses `airflow tasks test` rather than `dags test`, for both lanes. The
# DAGs carry retries=2 with a five-minute delay, which is right for a nightly
# run and useless for a demonstration: a failing DagRun takes ten minutes to
# admit it. And `tasks test` runs each task on the date it is given, where
# `dags test` takes the load date from the moment it runs -- yesterday, which
# is sealed, so metering would skip without connecting and prove nothing. A
# single task fails immediately, and the point is unchanged -- billing cannot
# read its source while metering reads its own and finishes.
#
# The credential is restored on EXIT, INT and TERM, and again at the start, so
# an interrupted run cannot leave billing broken for the next person.
#
# ⚠️ IT RUNS ON A PROBE DATE OF ITS OWN, NEVER A REAL LOAD DATE. A task whose
# partition is already sealed prints "sealed already; nothing to do" and exits
# before it connects -- so a broken credential is never tried, billing reports
# SUCCESS, and nothing is demonstrated. Every date of the span is sealed, and
# `make ingest-date VERIFY=1` has just sealed its own. The probe is the logical
# date 2024-10-02 (load date 2024-10-01): inside the DAGs' start and end dates,
# before anything the lake holds. It refuses if billing's partition for it is
# already sealed, and on exit removes every probe partition it created -- only
# those -- as it restores the credential.
#
# THE EXIT CODE OF `airflow tasks test` CANNOT BE TRUSTED. It returns 0 even
# when the task raises -- measured here: the billing task logged "password
# authentication failed" and still exited 0. An earlier version of this script
# read the exit code and cheerfully reported that a broken credential had
# succeeded. The verdict below is therefore taken from the LOG, which carries
# the authentication error, and confirmed against the lake: a billing extract
# that failed leaves no partition behind.
set -euo pipefail
cd "$(dirname "$0")/../../.."             # -> data/

PSQL="docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea"
AF="docker compose exec -T airflow-scheduler airflow"
PROBE_DATE="2024-10-02"       # the DAGs load 2024-10-01 for it: the day before

# The probe load date's partitions, listed or removed. `list` prints each
# prefix that exists; `clean` removes those not named on stdin -- what was
# there before the demo stays.
probe_lake() {
    docker run --rm -i --network kea_default --env-file .env \
        -v "$(pwd)/pipeline:/opt/pipeline:ro" dataeng/dbt:1.12.0 python3 -c '
import datetime as dt, os, sys
sys.path.insert(0, "/opt/pipeline/lib")
from kea_pipeline import lake, spec
from pyarrow import fs
s3, bucket = lake.s3_filesystem(), os.environ.get("KEA_LAKE_BUCKET", lake.BUCKET)
day = dt.date(2024, 10, 1)
here = [p for e in spec.ENTITIES
        for p in (lake.raw_prefix(e.source, e.table, day), lake.parsed_prefix(e.source, e.table, day),
                  lake.quarantine_prefix(e.source, e.table, day))
        if s3.get_file_info(f"{bucket}/{p}").type != fs.FileType.NotFound]
if sys.argv[1] == "list":
    print("\n".join(here))
else:
    keep = set(sys.stdin.read().split())
    for p in here:
        if p not in keep:
            s3.delete_dir(f"{bucket}/{p}")
            print(f"  removed probe partition {p}")
' "$1"
}

# `tasks test` REUSES A STORED DagRun for its date, and with it that run's
# run_after -- so a run once left at the probe date (by a `dags test`, say)
# would move every task to the load date of whenever it ran. Refuse rather
# than silently test another day.
stored=$(docker compose exec -T airflow-postgres psql -U airflow -d airflow -X -tA -c \
    "select string_agg(dag_id || ' ' || run_id, ', ') from dag_run
      where dag_id in ('kea_extract_billing', 'kea_extract_metering')
        and logical_date::date = date '$PROBE_DATE';" | tr -d '\r')
if [ -n "$stored" ]; then
    echo "a stored DagRun sits at the probe date, and tasks test would take its load date:" >&2
    echo "  $stored" >&2
    exit 2
fi

before=$(probe_lake list)
if grep -q "src_billing/customers" <<<"$before"; then
    echo "billing's partition for the probe date is sealed, so its task would skip" >&2
    echo "without connecting and prove nothing: $(grep src_billing/customers <<<"$before")" >&2
    exit 2
fi

restore() {
    echo
    echo "restoring the billing credential ..."
    pipeline/scripts/ops/set_extract_roles.sh >/dev/null
    echo "restored."
    echo "removing the probe's partitions ..."
    probe_lake clean <<<"$before"
}
trap restore EXIT INT TERM

# Idempotent: if a previous run was killed before its trap fired, this puts
# the credential back before we break it again.
pipeline/scripts/ops/set_extract_roles.sh >/dev/null 2>&1 || true

echo "== breaking the billing credential (metering and catalogue untouched) =="
echo "ALTER ROLE kea_extract_billing PASSWORD 'not-the-password';" | $PSQL -q -f - >/dev/null
echo "done."
echo

echo "== kea_extract_billing: snapshot_customers =="
$AF tasks test kea_extract_billing snapshot_customers "$PROBE_DATE" >/tmp/billing.log 2>&1 || true
if grep -qE "password authentication failed|Task failed with exception" /tmp/billing.log; then
    billing="FAILED"
else
    billing="SUCCESS"
fi
grep -oE "password authentication failed for user \"[a-z_]+\"" /tmp/billing.log | head -1 || true
echo "  -> $billing (from the log; the exit code is 0 either way -- see the header)"
echo

echo "== kea_extract_metering: every task, while billing is broken =="
: >/tmp/metering.log
for task in extract_interval_reads parse_interval_reads snapshot_networks \
            snapshot_connection_points snapshot_meters snapshot_registers; do
    $AF tasks test kea_extract_metering "$task" "$PROBE_DATE" >>/tmp/metering.log 2>&1 || true
done
# The verdict is what LANDED, not an exit code (see the header): every metering
# entity's parsed partition for the probe date, written with metering's own
# credential while billing's was broken.
landed=$(probe_lake list)
metering="SUCCESS"
for entity in interval_reads networks connection_points meters registers; do
    if ! grep -q "bronze/parsed/src_metering/$entity/load_date=" <<<"$landed" \
       || grep -q "bronze/parsed/src_metering/$entity/" <<<"$before"; then
        metering="FAILED"
        echo "  no new parsed partition for src_metering.$entity"
    fi
done
echo "  -> $metering"
echo

if [ "$billing" = "FAILED" ] && [ "$metering" = "SUCCESS" ]; then
    echo "INDEPENDENCE DEMONSTRATED"
    echo "  billing  could not authenticate to its source and produced nothing"
    echo "  metering ran to completion in the same window, against its own credential"
    echo
    echo "Four DAGs, four credentials, four DagRuns. A shared DAG would have"
    echo "left one run red and re-clearing it would have re-run metering too."
    exit 0
fi
echo "NOT DEMONSTRATED: billing=$billing metering=$metering" >&2
echo "(billing must FAIL and metering must SUCCEED for this to prove anything)" >&2
exit 1
