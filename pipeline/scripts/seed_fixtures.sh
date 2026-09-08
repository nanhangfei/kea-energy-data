#!/usr/bin/env bash
# Load the nine hand-written fixtures -- once, into an empty serving database.
#
# ⚠️ THIS IS A ONE-SHOT SEED, AND IT ALWAYS WAS. Every id in sample/*.sql is
# hand-assigned with OVERRIDING SYSTEM VALUE -- network 111001, connection point
# 131001, invoice line 241001 -- so a second run is a primary key violation on
# the first row it reaches. That has nothing to do with the pipeline and has
# been true since Stage 0; without this guard it surfaces as
#
#   ERROR:  duplicate key value violates unique constraint "connection_points_pkey"
#
# which reads like a bug rather than "you have already done this".
#
# SINCE STAGE 5 THERE IS A SECOND, INDEPENDENT COLLISION on the serving half.
# The fixtures write BOTH sides of each file: src_* (the pipeline's input) and
# metering/billing/catalogue/kea (the hand-built expected output). The load has
# since replaced that second half with real pipeline output, which carries the
# warehouse's own key_map surrogates -- so `0165841877LCC54` is icp_id 1 rather
# than 131001, and metering.icp.icp_number is UNIQUE. Even clearing the fixture
# serving rows first would not make a re-seed work while the pipeline's output
# is there.
#
# THE SOURCE HALF IS NOT OBSOLETE, and must not be "cleaned up". It is where the
# nine households in the warehouse come from: 42,556 meter reads and thirteen
# DEMO-ACCT-228001 invoices flow from it into dim_icp, fct_meter_read and every
# mart. Delete it and Stage 5's acceptance target has nothing to check against.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

# The database is overridable so the happy path can be exercised against a
# scratch copy. Seeding is the one action here with no dry run and no undo, so
# "it still works on an empty database" has to be testable somewhere other than
# the database everyone is using.
SEED_DB="${SEED_DB:-kea}"
PSQL="docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d $SEED_DB"
PROBE='0165841877LCC54'                # fixture 01's ICP -- the printed invoice

count() { $PSQL -X -tA -c "$1" 2>/dev/null | tr -d '[:space:]'; }

in_source=$(count "select count(*) from src_metering.connection_points
                    where icp_identifier = '$PROBE';")
in_serving=$(count "select count(*) from metering.icp where icp_number = '$PROBE';")

if [ "${in_source:-0}" != "0" ] || [ "${in_serving:-0}" != "0" ]; then
    cat >&2 <<EOF

The fixtures are already loaded, so there is nothing to seed.

  src_metering.connection_points  $PROBE present: ${in_source:-?}
  metering.icp                    $PROBE present: ${in_serving:-?}

sample/*.sql hand-assigns every id, so running it again is a primary key
violation rather than an update -- it is a seed for an EMPTY database, not a
refresh. Depending on what you meant:

  refresh the serving store from the pipeline   make load-serving
  check that output against what the fixtures
    assert                                      make assert-serving
  start over from nothing                       make clean && make up
                                                make serving-schema && make serving-seed

EOF
    exit 1
fi

for f in sample/[0-9][0-9]-*.sql; do
    echo "-> $f"
    $PSQL < "$f"
done
echo
echo "Nine fixtures loaded. They are the acceptance target: make load-serving"
echo "replaces the serving half with the pipeline's own output, and"
echo "make assert-serving checks that output against what they assert."
