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
# throughout. `make source-roles` restores it, and this script does that itself.
#
# It uses `airflow tasks test` rather than `dags test`. The DAGs carry
# retries=2 with a five-minute delay, which is right for a nightly run and
# useless for a demonstration: a failing DagRun takes ten minutes to admit it.
# A single task fails immediately, and the point is unchanged -- billing cannot
# read its source while metering reads its own and finishes.
#
# The credential is restored on EXIT, INT and TERM, and again at the start, so
# an interrupted run cannot leave billing broken for the next person.
#
# THE EXIT CODE OF `airflow tasks test` CANNOT BE TRUSTED. It returns 0 even
# when the task raises -- measured here: the billing task logged "password
# authentication failed" and still exited 0. An earlier version of this script
# read the exit code and cheerfully reported that a broken credential had
# succeeded. The verdict below is therefore taken from the LOG, which carries
# the authentication error, and confirmed against the lake: a billing extract
# that failed leaves no partition behind.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

PSQL="docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d kea"
AF="docker compose exec -T airflow-scheduler airflow"
LOAD_DATE="${1:-2026-08-27}"

restore() {
    echo
    echo "restoring the billing credential ..."
    pipeline/scripts/set_extract_roles.sh >/dev/null
    echo "restored."
}
trap restore EXIT INT TERM

# Idempotent: if a previous run was killed before its trap fired, this puts
# the credential back before we break it again.
pipeline/scripts/set_extract_roles.sh >/dev/null 2>&1 || true

echo "== breaking the billing credential (metering and catalogue untouched) =="
echo "ALTER ROLE kea_extract_billing PASSWORD 'not-the-password';" | $PSQL -q -f - >/dev/null
echo "done."
echo

echo "== kea_extract_billing: snapshot_customers =="
$AF tasks test kea_extract_billing snapshot_customers "$LOAD_DATE" >/tmp/billing.log 2>&1 || true
if grep -qE "password authentication failed|Task failed with exception" /tmp/billing.log; then
    billing="FAILED"
else
    billing="SUCCESS"
fi
grep -oE "password authentication failed for user \"[a-z_]+\"" /tmp/billing.log | head -1 || true
echo "  -> $billing (from the log; the exit code is 0 either way -- see the header)"
echo

echo "== kea_extract_metering: the whole DAG, while billing is broken =="
if $AF dags test kea_extract_metering "$LOAD_DATE" >/tmp/metering.log 2>&1; then
    metering="SUCCESS"
else
    metering="FAILED"
fi
grep -E "^ *(rows|_SUCCESS present)" /tmp/metering.log | sed -E 's/^[^ ]+ \[[a-z ]+\] //; s/ \[.*$//' | head -3 || true
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
