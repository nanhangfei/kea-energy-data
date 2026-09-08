#!/usr/bin/env bash
# Stage 5's second exit condition: a mid-load failure leaves the store unchanged.
#
# The other two conditions are checkable by looking at the result -- run the
# load twice and compare counts, read the gate's output. This one is only
# checkable by BREAKING something, because what it asserts is about the path
# not taken. So this makes the load fail on purpose, at a table near the end of
# the order, and proves that nothing before it moved.
#
# HOW THE FAILURE IS INJECTED. A CHECK constraint, added NOT VALID so it
# ignores the rows already there and applies only to what the load inserts.
# That is deliberately not a corrupted Parquet file: rewriting the export would
# test the exporter's error handling, and what is under test here is the
# LOADER's transaction. The constraint is dropped again at the end, including
# when the script exits early.
#
# kea.mart_account_context is the last table in serving.TABLES, so a failure
# there is as late as the run gets -- every other table has already been
# staged, deleted and re-inserted inside the open transaction by the time it
# blows up. If any of that had been committed on its own, this would catch it.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

PSQL="docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d kea -X"
PROBE=_atomicity_probe

counts() {
    $PSQL -A -t -c "
      select 'network',    count(*) from metering.network
      union all select 'icp',          count(*) from metering.icp
      union all select 'plan',         count(*) from catalogue.plan
      union all select 'customer',     count(*) from billing.customer
      union all select 'account',      count(*) from billing.account
      union all select 'bill',         count(*) from billing.bill
      union all select 'bill_line',    count(*) from billing.bill_line
      union all select 'read_hh',      count(*) from metering.meter_read_hh
      union all select 'daily_usage',  count(*) from kea.mart_daily_usage
      union all select 'acct_context', count(*) from kea.mart_account_context
      order by 1"
}
synced() {
    $PSQL -A -t -c "select max(synced_at) from billing.account"
}

cleanup() {
    $PSQL -q -c "ALTER TABLE kea.mart_account_context DROP CONSTRAINT IF EXISTS $PROBE;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== before =="
before=$(counts); before_synced=$(synced)
echo "$before" | sed 's/^/  /'

echo
echo "== injecting a failure at the last table in the load order =="
$PSQL -q -c "ALTER TABLE kea.mart_account_context
             ADD CONSTRAINT $PROBE CHECK (account_number <> 'DEMO-ACCT-229001') NOT VALID;" >/dev/null
echo "  CHECK (account_number <> 'DEMO-ACCT-229001') NOT VALID"

echo
echo "== running the load, which must fail =="
if make load-serving >/tmp/atomic_load.log 2>&1; then
    echo "  THE LOAD SUCCEEDED. The probe did not fire, so this proves nothing." >&2
    exit 1
fi
grep -m1 -E "FAILED" /tmp/atomic_load.log | sed 's/^/  /' || true

echo
echo "== after =="
after=$(counts); after_synced=$(synced)
echo "$after" | sed 's/^/  /'

echo
if [ "$before" != "$after" ]; then
    echo "FAIL: row counts moved across a failed load" >&2
    diff <(echo "$before") <(echo "$after") >&2 || true
    exit 1
fi
if [ "$before_synced" != "$after_synced" ]; then
    echo "FAIL: synced_at moved across a failed load ($before_synced -> $after_synced)" >&2
    exit 1
fi
echo "PASS: every row count and synced_at unchanged across a failed load."
echo "      The store is exactly as it was; nothing was left half-applied."
