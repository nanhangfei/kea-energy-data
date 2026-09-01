#!/usr/bin/env bash
# Load the generated source data into kea-serving-db.
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

PSQL="docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d kea"
HERE=pipeline/scripts/gen_source
RAW=raw/lcl

step() { printf '\n== %s\n' "$1"; }

step "01 schemas (gen_audit)"
$PSQL -q -f - < "$HERE/01_schemas.sql"

step "02 catalogue"
$PSQL -f - < "$HERE/02_catalogue.sql" | grep -E 'NOTICE|ERROR' || true

# All seven staging tables, metering and billing. The billing three were
# missing here for a while and only ever loaded by hand, which meant a
# clean `make init-data` would have rated against empty staging tables and
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
$PSQL -f - < "$HERE/03_reference.sql" | grep -E 'NOTICE|ERROR' || true

step "interval reads (this is the slow one)"
$PSQL -q -c "DELETE FROM src_metering.interval_reads WHERE source_file LIKE 'lcl-%';"
gzcat "$RAW/reads.csv.gz" | $PSQL -c "\copy src_metering.interval_reads \
    (icp_identifier, register_code, interval_start, kwh, read_status, \
     version_no, received_at, source_file) FROM STDIN WITH (FORMAT csv)"

step "05 defects"
$PSQL -f - < "$HERE/05_defects.sql" | grep -E 'NOTICE|ERROR' || true

step "06 revisions"
$PSQL -f - < "$HERE/06_revisions.sql" | grep -E 'NOTICE|ERROR' || true

# Rating comes AFTER defects and revisions, not before. A correction that
# lands after the invoices are priced leaves the bill disagreeing with the
# latest version of the reads it was supposedly rated from, and the source
# contradicts itself. The "late" revision is late in ARRIVAL time, which is
# what Stage 3 tests; it is not late relative to this pipeline.
step "04 rate bills (slow: ~11,500 invoices)"
$PSQL -f - < "$HERE/04_rate_bills.sql" | grep -E 'NOTICE|ERROR|UPDATE' || true

step "07 billing defects (after rating, on purpose)"
$PSQL -f - < "$HERE/07_billing_defects.sql" | grep -E 'NOTICE|ERROR' || true

step "99 assertions"
$PSQL -f - < "$HERE/99_assert.sql" 2>&1 | grep -E 'NOTICE|ERROR' || true

step "done"
$PSQL -c "SELECT count(*) AS reads, count(DISTINCT icp_identifier) AS icps, \
                 min(interval_start)::date AS from_date, \
                 max(interval_start)::date AS to_date, \
                 count(DISTINCT received_at::date) AS arrival_days \
          FROM src_metering.interval_reads WHERE source_file LIKE 'lcl-%';"
