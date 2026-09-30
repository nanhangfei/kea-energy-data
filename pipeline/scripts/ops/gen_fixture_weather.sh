#!/usr/bin/env bash
# Regenerate pipeline/dbt_project/seeds/fixture_weather_daily.csv -- the daily
# temperature each fixture was written against -- from sample/*.sql.
#
#     gen_fixture_weather.sh            # rewrite it (make build-warehouse REGEN=1)
#     gen_fixture_weather.sh --check    # exit 1 if the committed csv is stale;
#                                       # writes nothing (make build-warehouse)
#
# WHY THE FIXTURES NEED THEIR OWN WEATHER IN THE WAREHOUSE. Fixtures 02-09
# synthesise a temperature for every day and derive their expected bill
# explanation from it; fixture 03's $61.92 weather attribution is one. That
# temperature lived only in each file's hand-built kea.mart_daily_usage insert,
# the half seed_fixtures.sh drops as scaffolding. So the warehouse rebuilt the
# fixtures' days against NIWA by climate_region, which gave fixture 03 the
# wrong winter and the fixtures outside the four station regions no weather at
# all (issue #143). This seed carries the fixtures' own temperatures across,
# and mart_daily_usage prefers them over NIWA.
#
# GENERATED, NOT HAND-EDITED. sample/*.sql stays the one place a fixture's
# weather is written. The nine files are loaded into a THROWAWAY Postgres --
# never kea-source-db, whose seed is one-shot -- and the temperatures are read
# back out, so the CSV holds exactly what the SQL computes.
#
# DEMO ICPs ONLY. Fixture 01 is a real property's printed invoice. It has no
# synthetic weather, stays on NIWA Auckland, and its ICP must not be written
# into a project file. The LIKE 'DEMO%' filter is that guard; it does not rely
# on fixture 01 happening to carry no temperatures today.
#
# KEYED BY ICP, NOT BY REGION. Fixtures 05 and 08 are both in Otago, with
# overlapping dates and different temperatures.
set -euo pipefail
cd "$(dirname "$0")/../../.."             # -> data/

OUT=pipeline/dbt_project/seeds/fixture_weather_daily.csv
NAME="kea-fixture-weather-$$"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

# A throwaway credential for a container nothing else can reach. The bare
# `-e POSTGRES_PASSWORD` form reads it from here rather than a command line.
POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
export POSTGRES_PASSWORD

mkdir -p "$(dirname "$OUT")"
# Beside the target, so the final mv is a rename. No .csv suffix, so dbt never
# mistakes a leftover for a seed. A check renames nothing, so its copy goes
# outside the tree altogether.
if [ "$CHECK" = 1 ]; then
    TMP=$(mktemp "${TMPDIR:-/tmp}/fixture_weather_daily.XXXXXX")
else
    TMP=$(mktemp "$OUT.XXXXXX")
fi

cleanup() {
    docker rm -f -v "$NAME" >/dev/null 2>&1 || true
    rm -f "$TMP"
}
trap cleanup EXIT

# postgres:17.10, the release kea-source-db runs.
docker run -d --rm --name "$NAME" -e POSTGRES_USER=kea -e POSTGRES_DB=kea \
    -e POSTGRES_PASSWORD -e TZ=UTC postgres:17.10 >/dev/null
# Over TCP, not the socket: during initdb the entrypoint runs a temporary
# server with TCP off, and a socket check would pass against that one.
for _ in $(seq 60); do
    docker exec "$NAME" pg_isready -h 127.0.0.1 -U kea -d kea -q && break
    sleep 1
done
PSQL="docker exec -i $NAME psql -X -q -v ON_ERROR_STOP=1 -U kea -d kea"

# The source schema, the served schema the fixtures' second half inserts into,
# then the nine files in order, each with its own BEGIN/COMMIT. stdout is
# discarded because the fixtures SELECT as they go; errors still reach stderr.
# A check also drops the fixtures' own NOTICEs, since every build-warehouse
# runs one; warnings and errors still come through.
{
    [ "$CHECK" = 1 ] && echo "SET client_min_messages = warning;"
    cat design/kea_source_schema.sql
    cat design/kea_serving_schema.sql
    cat sample/[0-9][0-9]-*.sql
} | $PSQL >/dev/null

$PSQL -c "COPY (
    SELECT i.icp_number, u.usage_date AS date_day, u.temp_mean_c
      FROM kea.mart_daily_usage u
      JOIN metering.icp i ON i.icp_id = u.icp_id
     WHERE u.temp_mean_c IS NOT NULL
       AND i.icp_number LIKE 'DEMO%'
     ORDER BY 1, 2
) TO STDOUT WITH (FORMAT csv, HEADER)" > "$TMP"

rows=$(( $(wc -l < "$TMP") - 1 ))
if [ "$rows" -le 0 ]; then
    echo "no fixture temperatures found -- $OUT left unchanged" >&2
    exit 1
fi

# Byte for byte: the COPY above is ordered, so the same sample/*.sql always
# gives the same file, and any difference is a fixture edited without its seed.
if [ "$CHECK" = 1 ]; then
    if cmp -s "$TMP" "$OUT"; then
        echo "$OUT: current ($rows fixture-days)"
        exit 0
    fi
    echo "$OUT is stale against sample/*.sql (re-run \`make build-warehouse REGEN=1\`):" >&2
    diff "$OUT" "$TMP" | head -20 >&2 || true
    exit 1
fi

echo "per fixture ICP: days, first, last"
$PSQL -tA -F '  ' -c "
    SELECT i.icp_number, count(*), min(u.usage_date), max(u.usage_date)
      FROM kea.mart_daily_usage u
      JOIN metering.icp i ON i.icp_id = u.icp_id
     WHERE u.temp_mean_c IS NOT NULL
       AND i.icp_number LIKE 'DEMO%'
     GROUP BY 1 ORDER BY 1"

mv "$TMP" "$OUT"
echo "$OUT: $rows fixture-days"
