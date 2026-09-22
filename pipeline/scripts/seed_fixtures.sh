#!/usr/bin/env bash
# Load the nine hand-written fixtures' SOURCE rows -- once, into an empty
# pipeline database.
#
# ⚠️ WITHOUT --reseed THIS IS A SEED FOR AN EMPTY DATABASE, and it refuses once
# the fixtures are present. Every id in sample/*.sql is hand-assigned with
# OVERRIDING SYSTEM VALUE -- network 111001, connection point 131001 -- and each
# file deletes its own ids before inserting them. But the generated cohort hangs
# foreign keys off those rows (connection points on fixture 01's network, a
# plan-history row on every account), so a plain second run against a loaded
# database fails part-way, which reads like a bug rather than "you have already
# done this".
#
# --reseed (make reseed-fixtures) is that second run done properly: it removes
# the generated rows first, inside the same transaction, and `make demo-refresh`
# loads them back. They come back with ids from 1,000,000
# (gen_audit.sync_sequences), so none can hold an id a fixture adds later.
#
# EACH FIXTURE WRITES TWO HALVES, AND THIS KEEPS ONE. A file inserts the src_*
# rows (the pipeline's input) and, beside them, the hand-built expected output
# in metering/billing/catalogue/kea. Since 2026-09-12 the served tables live in
# the backend's database and only a publish writes them, so this database has
# no such schemas. The seed creates them from design/kea_serving_schema.sql as
# scaffolding, inside the fixtures' own transaction, and drops them again
# before it commits: the second half lands nowhere. Nothing is lost by that.
# What those rows asserted was lifted into pipeline/scripts/assert_serving.sql,
# which `make assert-serving` runs against what was actually published.
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
PSQL="docker compose exec -T kea-source-db psql -X -v ON_ERROR_STOP=1 -U kea -d $SEED_DB"
PROBE='0165841877LCC54'                # fixture 01's ICP -- the printed invoice

# psql's error is left on the terminal: a failure here -- a mistyped SEED_DB, a
# database without the source schema -- ends the script under pipefail, and it
# should not end it silently.
count() { $PSQL -tA -c "$1" | tr -d '[:space:]'; }

RESEED=false
[ "${1:-}" = "--reseed" ] && RESEED=true

in_source=$(count "select count(*) from src_metering.connection_points
                    where icp_identifier = '$PROBE';")
if [ "${in_source:-0}" != "0" ] && ! $RESEED; then
    cat >&2 <<EOF

The fixtures are already loaded, so there is nothing to seed.

  src_metering.connection_points  $PROBE present: ${in_source}

This is the seed for an EMPTY database. Depending on what you meant:

  replace them after changing a fixture    make reseed-fixtures
                                           then make demo-refresh
  publish the pipeline's output for them   make demo-refresh, or
                                           make publish-serving RELEASE=<id>
  check that output against what the
    fixtures assert                        make assert-serving
  start over from nothing                  make clean && make up
                                           make source-schema && make source-seed

EOF
    exit 1
fi

served=$(count "select string_agg(nspname, ',' order by nspname) from pg_namespace
                where nspname in ('kea', 'billing', 'catalogue', 'metering', 'common');")
if [ -n "$served" ]; then
    cat >&2 <<EOF

This database still holds served schemas: $served

They are the old copy of the served tables, from before those moved to the
backend's database, and the seed's own temporary copy would collide with
them. The file that removes them says what to keep first:

  pipeline/migrations/2026-09-12_drop_serving_copy.sql

EOF
    exit 1
fi

# One session, one transaction: the scaffold, the nine fixtures, the scaffold's
# removal. Each fixture brings its own BEGIN;/COMMIT;, which would commit the
# scaffold part-way through, so those two lines -- exactly one of each per file,
# alone on their line -- are stripped and this supplies the pair. A failure
# anywhere leaves the database exactly as it was.
{
    echo 'BEGIN;'
    if $RESEED; then
        # Generated rows hang foreign keys off the fixtures' (connection points
        # on fixture 01's network, a plan-history row on every account), so a
        # fixture's own DELETE fails while they exist. demo-refresh reloads them.
        echo '\echo -> removing generated rows: 00_clean_generated.sql'
        sed '/^BEGIN;[[:space:]]*$/d; /^COMMIT;[[:space:]]*$/d' \
            pipeline/scripts/gen_source/00_clean_generated.sql
    fi
    echo '\echo -> scaffold: design/kea_serving_schema.sql'
    cat design/kea_serving_schema.sql
    for f in sample/[0-9][0-9]-*.sql; do
        printf '\\echo -> %s\n' "$f"
        sed '/^BEGIN;[[:space:]]*$/d; /^COMMIT;[[:space:]]*$/d' "$f"
    done
    cat <<'SQL'
\echo -> removing the scaffold
-- DROP ... CASCADE would also drop any column ELSEWHERE whose type lives in
-- common, silently. None does today; this makes "none" a checked fact, by
-- counting every column outside the five schemas before and after.
CREATE TEMP TABLE scaffold_kept AS
    SELECT count(*) AS n FROM information_schema.columns
    WHERE table_schema NOT IN ('kea', 'billing', 'catalogue', 'metering', 'common',
                               'pg_catalog', 'information_schema')
      AND table_schema NOT LIKE 'pg\_temp%';
SET client_min_messages = warning;     -- not the cascade's list of every table
DROP SCHEMA kea, billing, catalogue, metering, common CASCADE;
RESET client_min_messages;
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
          AND table_schema NOT LIKE 'pg\_temp%')
       <> (SELECT n FROM scaffold_kept) THEN
        RAISE EXCEPTION 'removing the scaffold dropped columns outside it';
    END IF;
END $$;
COMMIT;
SQL
} | $PSQL -q

echo
echo "Nine fixtures' source rows loaded; their expected-output half was"
echo "scaffolding and is gone. The pipeline carries these households into the"
echo "backend's database -- make demo-refresh, or make publish-serving"
echo "RELEASE=<id> over a current export -- and make assert-serving checks what"
echo "arrives against what the fixtures assert."
if $RESEED; then
    echo
    echo "The generated rows were removed with them: make demo-refresh loads them back."
fi
