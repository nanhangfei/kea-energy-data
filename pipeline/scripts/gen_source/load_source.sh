#!/usr/bin/env bash
# Load the generated source data into kea-source-db.
#
# COPY runs over `docker compose exec -T ... psql`, so nothing needs a host
# Postgres driver and nothing needs a volume mount. 18.8 M reads stream
# straight from the gzip.
#
# Idempotent. Reference rows upsert on their business key; reads are deleted
# by source_file prefix first, which cannot touch the nine fixtures because
# their source_file names do not begin with 'lcl-'.
set -euo pipefail
cd "$(dirname "$0")/../../.."          # -> data/

# Both overridable, so the whole load can be dry-run against a throwaway
# database and a scratch output directory; the defaults are the real ones.
# KEA_SOURCE_PSQL must bring its own ON_ERROR_STOP.
PSQL="${KEA_SOURCE_PSQL:-docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea}"
HERE=pipeline/scripts/gen_source
RAW="${KEA_GEN_OUT:-raw/lcl}"

step() { printf '\n== %s\n' "$1"; }

# Run one SQL file, show only the lines matching $2, and fail with psql.
# `psql ... | grep ... || true` hid every failed step: grep's status is the
# pipeline's, and `|| true` swallowed the rest, so a step that died on an
# exception still printed its ERROR and the load carried on.
run_sql() {
    local file=$1 pattern=${2:-NOTICE|ERROR} rc
    set +e
    $PSQL -f - < "$HERE/$file" 2>&1 | grep -E "$pattern"
    rc=${PIPESTATUS[0]}
    set -e
    if [ "$rc" -ne 0 ]; then
        printf '!! %s failed (psql exit %s)\n' "$file" "$rc" >&2
        exit "$rc"
    fi
}

step "01 schemas (gen_audit)"
$PSQL -q -f - < "$HERE/01_schemas.sql"

# The demo household's account number, from planted.DEMO_LCLID -- the one
# source of truth -- into the answer key, so 08_payments.sql reads it rather
# than inferring it from flags another account can share.
DEMO_ACCT=$(cd "$HERE" && python3 -c "import planted; print(planted.account_number(planted.DEMO_LCLID))")
$PSQL -q -c "CREATE TABLE IF NOT EXISTS gen_audit.demo_household (acct_no text PRIMARY KEY);
             TRUNCATE gen_audit.demo_household;
             INSERT INTO gen_audit.demo_household VALUES ('$DEMO_ACCT');"

step "02 catalogue"
run_sql 02_catalogue.sql

# All seven staging tables, metering and billing. The billing three were
# missing here for a while and only ever loaded by hand, which meant a
# clean `make init-sources` would have rated against empty staging tables and
# produced zero invoices. 99_assert.sql would have caught it, but only
# after an hour of generating.
step "staging the CSVs"
for t in networks connection_points meters registers \
         customers accounts invoice_periods; do
    $PSQL -q -c "TRUNCATE gen_audit.stg_$t;"
    $PSQL -q -c "\copy gen_audit.stg_$t FROM STDIN WITH (FORMAT csv)" < "$RAW/$t.csv"
    printf '   %-18s %s rows\n' "$t" "$(wc -l < "$RAW/$t.csv" | tr -d ' ')"
done

step "03 reference"
run_sql 03_reference.sql

step "interval reads (this is the slow one)"
$PSQL -q -c "DELETE FROM src_metering.interval_reads WHERE source_file LIKE 'lcl-%';"
gzcat "$RAW/reads.csv.gz" | $PSQL -c "\copy src_metering.interval_reads \
    (icp_identifier, register_code, interval_start, kwh, read_status, \
     version_no, received_at, source_file) FROM STDIN WITH (FORMAT csv)"

step "05 defects"
run_sql 05_defects.sql

# After 05, which records this load's run; the sessions are already inside
# the reads (gen_metering.py added them), so this is only the answer key.
step "09 planted loads"
$PSQL -q -c "TRUNCATE gen_audit.stg_planted_load;"
gzcat "$RAW/planted_loads.csv.gz" | $PSQL -q -c "\copy gen_audit.stg_planted_load \
    (icp_identifier, load_kind, pattern, session_start, session_end, half_hours, kwh) \
    FROM STDIN WITH (FORMAT csv)"
run_sql 09_planted.sql

step "06 revisions"
run_sql 06_revisions.sql

# Rating comes AFTER defects and revisions, not before. A correction that
# lands after the invoices are priced leaves the bill disagreeing with the
# latest version of the reads it was supposedly rated from, and the source
# contradicts itself. The "late" revision is late in ARRIVAL time, which is
# what Stage 3 tests; it is not late relative to this pipeline.
step "04 rate bills (the 500-household profile has ~11,500 invoices)"
run_sql 04_rate_bills.sql 'NOTICE|ERROR|UPDATE'

# AFTER rating, not before: a payment is against an amount, and amounts do
# not exist until 04 has run.
step "08 payments"
run_sql 08_payments.sql

step "07 billing defects (after rating, on purpose)"
run_sql 07_billing_defects.sql

step "99 assertions"
run_sql 99_assert.sql

step "done"
$PSQL -c "SELECT count(*) AS reads, count(DISTINCT icp_identifier) AS icps, \
                 min(interval_start)::date AS from_date, \
                 max(interval_start)::date AS to_date, \
                 count(DISTINCT received_at::date) AS arrival_days \
          FROM src_metering.interval_reads WHERE source_file LIKE 'lcl-%';"
