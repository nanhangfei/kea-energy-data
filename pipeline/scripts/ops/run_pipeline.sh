#!/usr/bin/env bash
# Run kea_daily_pipeline for one load date, or each date of a range, and wait.
#
#   pipeline/scripts/ops/run_pipeline.sh 2026-08-25
#   pipeline/scripts/ops/run_pipeline.sh 2026-08-01 2026-08-31
#   make run-pipeline LOAD_DATE=2026-08-25
#   make run-pipeline FROM=2026-08-01 TO=2026-08-31
#
# Exits non-zero if any of the runs it waited for failed, so a make chain stops
# on it. A date whose run already exists is reported and not re-run.
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
cd "$(dirname "$0")/../../.."             # -> data/

FROM="${1:?usage: run_pipeline.sh LOAD_DATE [TO_DATE] (YYYY-MM-DD)}"
TO="${2:-$FROM}"
AF="docker compose exec -T airflow-scheduler airflow"
AFDB="docker compose exec -T airflow-postgres psql -U airflow -d airflow -X -tA"

# A PAUSED DAG ACCEPTS A TRIGGER AND NEVER STARTS THE RUN, so the wait below
# would spin forever. It is paused on purpose on the demo layout -- a
# nightly date staged over the demo's one generation duplicates rows -- so
# refuse here, before queueing anything, rather than hang.
paused=$($AFDB -c "select is_paused from dag where dag_id='kea_daily_pipeline';" | tr -d '[:space:]')
case "$paused" in
    f) ;;
    t) echo "kea_daily_pipeline is paused. It is paused deliberately on the demo"
       echo "layout; unpause it only on a per-date layout."
       exit 2 ;;
    *) echo "kea_daily_pipeline is not registered in Airflow ('$paused'); is the stack up?"
       exit 2 ;;
esac

dates=$(python3 - "$FROM" "$TO" <<'PY'
import sys, datetime as dt
a = dt.date.fromisoformat(sys.argv[1]); b = dt.date.fromisoformat(sys.argv[2])
if b < a: sys.exit("TO is before FROM")
while a <= b:
    print(a.isoformat()); a += dt.timedelta(days=1)
PY
)
n=$(echo "$dates" | wc -l | tr -d ' ')
RUN_PREFIX="kea_bf__"
RUN_IDS=$(for d in $dates; do printf "'%s%s'," "$RUN_PREFIX" "$d"; done | sed 's/,$//')
echo "triggering $n run(s) of kea_daily_pipeline, $FROM -> $TO"

# ⚠️ RUN_PREFIX (set above) IS NOT `backfill__`. Airflow reserves that prefix:
# "A manual DAG run cannot use ID 'backfill__2026-08-24' since it is reserved
# for backfill runs".

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
    pending=$($AFDB -c \
        "select count(*) from dag_run
          where dag_id='kea_daily_pipeline' and run_id in ($RUN_IDS)
            and state in ('queued','running');" | tr -d '[:space:]')
    [ "$pending" = "0" ] && break
    printf "\r  %s still queued or running   " "$pending"
    sleep 20
done
echo
docker compose exec -T airflow-postgres psql -U airflow -d airflow -X -c \
    "select state, count(*) from dag_run
      where dag_id='kea_daily_pipeline' and run_id in ($RUN_IDS)
      group by 1 order by 1;"

# The runs THIS invocation waited for, not every kea_bf__ run ever made: an old
# failure elsewhere must not fail today's range, nor hide one inside it.
failed=$($AFDB -c "select string_agg(run_id, ' ' order by run_id) from dag_run
                    where dag_id='kea_daily_pipeline' and run_id in ($RUN_IDS)
                      and state <> 'success';" | tr -d '\r')
if [ -n "$failed" ]; then
    echo "not successful: $failed"
    exit 1
fi
