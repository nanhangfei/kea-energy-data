#!/usr/bin/env bash
# Backfill kea_daily_pipeline over a date range.
#
#   pipeline/scripts/backfill_pipeline.sh 2026-08-01 2026-08-31
#   make backfill-pipeline FROM=2026-08-01 TO=2026-08-31
#
# ⚠️ `airflow backfill create` CANNOT BE USED HERE, and the reason is a genuine
# conflict between two of Stage 6's own checkboxes rather than an oversight.
#
#   DagNonPeriodicScheduleException: kea_daily_pipeline has a non-periodic
#   schedule that does not support backfills
#
# The pipeline is asset-scheduled, per design §9 -- it runs when the three
# extract lanes have landed their bronze, not on a timetable. `airflow backfill`
# works by generating runs from a periodic schedule, and an asset-driven DAG has
# none to generate from. Checked with --dry-run, not assumed.
#
# So a range becomes one triggered run per date, each carrying the date in its
# conf. THIS IS STILL "THROUGH THE SAME DAG" -- which is what design §11 asks
# for: "No separate backfill code path -- a second code path is a second set of
# bugs, and it is always the one that is under-tested." The same six tasks, the
# same containers, the same loader. Only the way the date arrives differs, and
# `load_date` is a first-class Param precisely so it can.
#
# max_active_runs=1 on the DAG means these queue and run one at a time however
# many are triggered here, so a month does not start thirty Snowflake sessions.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

FROM="${1:?usage: backfill_pipeline.sh FROM_DATE TO_DATE (YYYY-MM-DD)}"
TO="${2:?usage: backfill_pipeline.sh FROM_DATE TO_DATE (YYYY-MM-DD)}"
AF="docker compose exec -T airflow-scheduler airflow"

dates=$(python3 - "$FROM" "$TO" <<'PY'
import sys, datetime as dt
a = dt.date.fromisoformat(sys.argv[1]); b = dt.date.fromisoformat(sys.argv[2])
if b < a: sys.exit("TO is before FROM")
while a <= b:
    print(a.isoformat()); a += dt.timedelta(days=1)
PY
)
n=$(echo "$dates" | wc -l | tr -d ' ')
echo "triggering $n run(s) of kea_daily_pipeline, $FROM -> $TO"

# ⚠️ NOT `backfill__`. Airflow reserves that prefix: "A manual DAG run cannot
# use ID 'backfill__2026-08-24' since it is reserved for backfill runs".
RUN_PREFIX="kea_bf__"

for d in $dates; do
    # A stable run id per date, so re-running a range collides loudly with the
    # previous attempt instead of quietly creating a second run for the same
    # day. Clear the old run first if that is what you meant.
    #
    # The error is REPORTED, not assumed. An earlier version of this loop ended
    # in `|| echo "already exists"`, which is how three triggers failing on a
    # reserved run-id prefix looked like three days being politely skipped.
    if out=$($AF dags trigger kea_daily_pipeline \
                 --conf "{\"load_date\": \"$d\"}" --run-id "$RUN_PREFIX$d" 2>&1); then
        echo "  queued $d"
    elif grep -qi "already exists\|Unique constraint" <<<"$out"; then
        echo "  skipped $d -- a run for this date already exists; clear it to redo"
    else
        echo "  FAILED  $d"
        grep -viE "^\[|deprecat" <<<"$out" | tail -3 | sed 's/^/      /'
        exit 1
    fi
done

echo
echo "waiting for them to finish (max_active_runs=1, so they run in sequence)"
while :; do
    pending=$(docker compose exec -T airflow-postgres psql -U airflow -d airflow -X -tA -c \
        "select count(*) from dag_run
          where dag_id='kea_daily_pipeline' and run_id like 'kea!_bf!_!_%' escape '!'
            and state in ('queued','running');" | tr -d '[:space:]')
    [ "$pending" = "0" ] && break
    printf "\r  %s still queued or running   " "$pending"
    sleep 20
done
echo
docker compose exec -T airflow-postgres psql -U airflow -d airflow -X -c \
    "select state, count(*) from dag_run
      where dag_id='kea_daily_pipeline' and run_id like 'kea!_bf!_!_%' escape '!'
      group by 1 order by 1;"
