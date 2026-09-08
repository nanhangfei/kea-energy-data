#!/usr/bin/env bash
# Rebuild the whole warehouse after uk_to_nz.NZ_SPAN_END or LCL_START moved.
#
#   pipeline/scripts/rebuild_span.sh                 # the whole span
#   pipeline/scripts/rebuild_span.sh 2026-01-01 2026-03-31
#
# ⚠️ WHY NOT backfill_pipeline.sh. That script triggers the WHOLE
# kea_daily_pipeline per date -- stage, dbt, export, serving load -- which is
# right for re-running a few days and wrong for a full rebuild. Every dbt model
# is `+materialized: table`, rebuilt from all of RAW, so running dbt 757 times
# produces the same tables 757 times. Measured, that is the difference between
# about 18 hours and about 7.
#
# The shape is therefore: PER-DATE work per date, ONCE-ONLY work once.
#
#   per date   extract (Spark, ~11 s) + parse (~10 s) + scripted (~4 s)
#              + stage, INCREMENTAL ENTITIES ONLY (~6 s)   = ~31 s
#   once       the fourteen snapshots, then dbt build + test, export and the
#              serving load                                 (~3 minutes)
#
# Staging every entity per date instead costs 40 s rather than 6 -- measured --
# because all fourteen snapshot loads are overwritten by the next date. That
# one flag is 7.2 hours across the span.
#
# ⚠️ IT DROPS AND RECREATES RAW FIRST, and skipping that is the quiet way to get a wrong
# warehouse. Staging is delete-then-insert PER LOAD DATE, so it replaces the
# dates it is given and leaves every other date alone. After a span move the
# old load dates only partly overlap the new ones, so the non-overlapping ones
# survive. DROP also applies current Avro schema evolution, which TRUNCATE
# cannot do. Without both properties, old rows or old columns remain invisible
# to every per-date check and wrong in every aggregate.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

FROM="${1:-}"
TO="${2:-}"

# The span, derived rather than typed: load date D holds readings for D-1, so
# the load dates run from span_start+1 to span_end+1.
if [ -z "$FROM" ]; then
    read -r FROM TO <<<"$(python3 - <<'PY'
import sys, datetime as dt
sys.path.insert(0, "pipeline/scripts/gen_source")
import uk_to_nz as tz
shift = tz.derive_shift_days()
start = tz.nz_local_date_for(tz.LCL_START, shift) + dt.timedelta(days=1)
end   = tz.NZ_SPAN_END + dt.timedelta(days=1)
print(start.isoformat(), end.isoformat())
PY
)"
fi

dates=$(python3 - "$FROM" "$TO" <<'PY'
import sys, datetime as dt
a = dt.date.fromisoformat(sys.argv[1]); b = dt.date.fromisoformat(sys.argv[2])
if b < a: sys.exit("TO is before FROM")
while a <= b:
    print(a.isoformat()); a += dt.timedelta(days=1)
PY
)
n=$(echo "$dates" | wc -l | tr -d ' ')

KEY_HOST=$(grep -E '^SNOWFLAKE_PRIVATE_KEY_HOST=' .env | cut -d= -f2-)
# -i IS NOT OPTIONAL. Without it docker run does not attach stdin, so a
# heredoc piped to `python3 -` reads EOF immediately and the script does
# nothing -- silently, with exit status 0. That is how the first run of this
# file skipped its own RAW reset and started mixing old load dates with new.
DOCKER_RUN="docker run --rm -i --network kea_default --env-file .env \
  -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
  -v ${KEY_HOST}:/keys/snowflake.p8:ro \
  -v $(pwd)/pipeline:/opt/pipeline:ro -w /opt/pipeline/scripts"
SPARK="docker compose exec -T spark-master /opt/spark/bin/spark-submit --master spark://spark-master:7077"

echo "rebuilding $n load dates, $FROM -> $TO"
echo

# ---------------------------------------------------------------- recreate RAW
echo "== recreating RAW from the current Avro contracts"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 - <<'PY'
import os, snowflake.connector, sys
sys.path.insert(0, '/opt/pipeline/lib')
sys.path.insert(0, '/opt/pipeline/scripts')
from kea_pipeline import spec
from load_bronze_to_snowflake import table_name
c = snowflake.connector.connect(
    account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
    private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
    role=os.environ["SNOWFLAKE_ROLE"], warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
    database=os.environ["SNOWFLAKE_DATABASE"].upper())
for e in spec.ENTITIES:
    t = table_name(e)
    try:
        c.cursor().execute(f'DROP TABLE IF EXISTS KEA.RAW."{t}"')
    except Exception as exc:
        raise SystemExit(f"  {t}: {exc}")
    print(f"  {t}: dropped")
c.close()
PY
echo

# ---------------------------------------------------------------- per date
i=0
start_ts=$(date +%s)
for d in $dates; do
    i=$((i + 1))
    $SPARK /opt/spark_jobs/extract_interval_reads.py "$d" --force >/dev/null 2>&1
    $SPARK /opt/spark_jobs/parse_interval_reads.py   "$d" --force >/dev/null 2>&1
    # Scripted entities: snapshots re-extract cheaply and invoices are
    # incremental, so this is not per-entity guesswork -- extract_small skips
    # what has no rows for the date.
    #
    # ⚠️ --force, FOR THE SAME REASON THE TWO SPARK STEPS ABOVE PASS IT.
    #
    # spec.py's rule is that a sealed partition is immutable: "a re-run is a
    # no-op unless --force is passed explicitly". After a span move that is
    # exactly wrong. Every load date that already had a bronze partition keeps
    # its OLD parquet, the extract returns 0 without touching it, and the
    # loader below stages that stale file into a RAW table this script has just
    # recreated -- so recreating RAW is necessary and NOT sufficient.
    #
    # Measured on the 2027 span move, before this flag was here: 6,649 invoice
    # rows carrying the old shift under load dates 2025-08-02 .. 2026-08-31,
    # 3,210 duplicate INV_IDs, and dbt's staging uniqueness test failing after
    # the loop had already run for six and a half hours. The reads were fine
    # precisely because their two steps did force.
    $DOCKER_RUN dataeng/dbt:1.12.0 python3 -c "
import sys; sys.path.insert(0, '/opt/pipeline/lib')
import extract_small
from kea_pipeline import spec
for e in spec.ENTITIES:
    if e.qualified == 'src_metering.interval_reads':
        continue
    try:
        extract_small.main([e.qualified, '$d', '--force'])
    except SystemExit:
        pass
" >/dev/null 2>&1 || true
    # ⚠️ --incremental-only, and the last date below undoes it.
    #
    # Fourteen of seventeen entities are snapshots, and a snapshot stages with
    # overwrite=True. Staging them per date means 756 loads that exist only to
    # be overwritten by the next day's. Measured: 40.1 s a date with them,
    # 5.8 s without -- 7.2 hours across the span.
    $DOCKER_RUN dataeng/dbt:1.12.0 \
        python3 load_all_bronze_to_snowflake.py "$d" --incremental-only >/dev/null 2>&1

    if [ $((i % 25)) -eq 0 ] || [ "$i" -eq "$n" ]; then
        el=$(( $(date +%s) - start_ts ))
        eta=$(( el * (n - i) / i ))
        printf '  %4d/%d  %s  elapsed %dm  eta %dm\n' "$i" "$n" "$d" $((el/60)) $((eta/60))
    fi
done
echo

# ⚠️ THE LOOP'S RANGE IS THE READING SPAN, AND INCREMENTAL BILLING TABLES DO
# NOT NECESSARILY LIVE IN IT.
#
# FROM/TO are derived from LCL_START and NZ_SPAN_END -- the dates readings
# exist for. `invoices` is incremental on ISSUE_DT and `payments` on PAID_TS;
# either can have rows outside that window at both ends:
#
#   * the nine fixture households bill across 2023-07 .. 2024-09, years before
#     the cohort's span. They are the acceptance target for Stage 5 -- the
#     serving export carries their ENTIRE span for exactly this reason -- and
#     the loop never visits a single one of their dates.
#   * the cohort's last invoices are issued AFTER its last reading: 125 of them
#     on 2027-08-29 and 2027-08-30, past a TO of 2027-08-28.
#
# Measured before this catch-up existed: 153 invoices silently absent after a
# rebuild that reported no error at all. Every fixture bill vanished from
# serving and `make assert-serving` failed eight checks -- which is the only
# reason it was found. Payments need the same catch-up: otherwise a late manual
# payment can be valid source data yet never make it into a full rebuild.
#
# Sparse, so this asks the source which dates exist rather than looping the
# 762 empty days between 2023-07 and the span.
echo "== incremental billing dates outside the reading span (fixtures, and the tail)"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 - "$FROM" "$TO" <<'PYEOF'
import contextlib, io, sys
sys.path.insert(0, '/opt/pipeline/lib')
sys.path.insert(0, '/opt/pipeline/scripts')
import extract_small, load_bronze_to_snowflake as loader
from kea_pipeline import spec

lo, hi = sys.argv[1], sys.argv[2]
entities = [e for e in spec.ENTITIES
            if e.mode == spec.INCREMENTAL and e.qualified not in spec.SPARK_ENTITIES]
conn = None
for ent in entities:
    # Entity names and watermark names come only from spec.py, the executable
    # source contract; this is not an interpolated operator input.
    src = extract_small._connect(ent)
    with src.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT {ent.watermark}::date FROM {ent.qualified} "
            f"WHERE {ent.watermark}::date < %s OR {ent.watermark}::date > %s ORDER BY 1",
            (lo, hi),
        )
        dates = [r[0] for r in cur.fetchall()]
    src.close()

    if not dates:
        print(f"  {ent.qualified}: none")
        continue
    if conn is None:
        conn = loader._connect()
    total = 0
    for d in dates:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            extract_small.main([ent.qualified, d.isoformat(), '--force'])
            total += loader.load(ent, d, conn=conn)
    print(f"  {ent.qualified}: {total} rows over {len(dates)} load dates outside {lo}..{hi}")
if conn is not None:
    conn.close()
PYEOF
echo

# The snapshots, once. Skipped per-date above, so without this they never land
# and every dimension in the warehouse is empty -- dbt would build marts with
# no ICPs to join to and no assertion in this script would notice.
echo "== staging the snapshots (once, on the last date)"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 load_all_bronze_to_snowflake.py "$TO"

# VERIFIED, NOT ASSUMED -- the same lesson as the truncate above.
#
# load_all_bronze_to_snowflake returns 0 when an entity is SKIPPED for want of a
# sealed partition; only a real failure returns 1. So all fourteen snapshots can
# skip, this step can print "0/14 landed", and `set -e` sees success. dbt then
# builds every dimension from an empty table and the marts come out empty with
# nothing in this script noticing.
$DOCKER_RUN dataeng/dbt:1.12.0 python3 - <<'PYEOF'
import os, snowflake.connector, sys
sys.path.insert(0, '/opt/pipeline/lib')
sys.path.insert(0, '/opt/pipeline/scripts')
from kea_pipeline import spec
# table_name imported rather than reimplemented: the src_ -> SOURCE__ENTITY
# convention lives in one place and this must not become a second copy of it.
from load_bronze_to_snowflake import table_name
c = snowflake.connector.connect(
    account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
    private_key_file=os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"],
    role=os.environ["SNOWFLAKE_ROLE"], warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
    database=os.environ["SNOWFLAKE_DATABASE"].upper())
empty = []
for e in spec.ENTITIES:
    if e.mode == spec.SNAPSHOT:
        t = table_name(e)
        n, = c.cursor().execute(f'SELECT COUNT(*) FROM KEA.RAW."{t}"').fetchone()
        print(f"  {t:34} {n:>9,}")
        if not n:
            empty.append(t)
c.close()
if empty:
    raise SystemExit("  EMPTY after snapshot staging: " + ", ".join(empty))
PYEOF
echo

# ------------------------------------------------------------- once only
echo "== dbt build (once: every model is materialized: table)"
docker run --rm --network kea_default --env-file .env \
  -e SNOWFLAKE_PRIVATE_KEY_PATH=/keys/snowflake.p8 \
  -v "${KEY_HOST}":/keys/snowflake.p8:ro \
  -v "$(pwd)/pipeline/dbt_project":/dbt -w /dbt dataeng/dbt:1.12.0 \
  dbt build --profiles-dir .

echo "== export warehouse -> lake"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 export_warehouse_to_lake.py

echo "== load lake -> serving"
$DOCKER_RUN dataeng/dbt:1.12.0 python3 load_export_to_serving.py

echo
echo "rebuild finished in $(( ($(date +%s) - start_ts) / 60 )) minutes"
