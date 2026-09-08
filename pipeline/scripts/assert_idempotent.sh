#!/usr/bin/env bash
# Stage 3's SECOND exit condition: "re-running a load date changes nothing".
#
# It cannot mean byte-identical. `_extracted_at` records when the pull happened,
# so it legitimately differs between two runs, and Parquet part-files carry a
# fresh UUID each time. The claim has to be written down to be checkable, and
# lake.py writes it down:
#
#   Two materialisations of a load date are identical when their ROW MULTISETS
#   OVER EVERY COLUMN EXCEPT _extracted_at are equal.
#
# So this re-extracts with --force and compares the content hash the extract
# recorded in the partition's manifest. Multiset, not set -- 9,263 duplicate
# deliveries are real source rows, and a comparison that collapsed them would
# call a genuine change no change.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

LOAD_DATE="${1:?usage: assert_idempotent.sh YYYY-MM-DD}"
PREFIX="bronze/src_metering/interval_reads/load_date=${LOAD_DATE}"

manifest_hash() {
    docker compose exec -T minio sh -c \
        "mc alias set L http://localhost:9000 \"\$MINIO_ROOT_USER\" \"\$MINIO_ROOT_PASSWORD\" >/dev/null 2>&1
         mc cat L/kea-lake/${PREFIX}/_MANIFEST.json" 2>/dev/null \
    | python3 -c "import sys, json; d = json.load(sys.stdin); print(d['content_hash'], d['extracted_at'])"
}

read -r before_hash before_at <<<"$(manifest_hash)"
echo "  before   hash=${before_hash}"
echo "           extracted_at=${before_at}"

echo "  re-extracting ${LOAD_DATE} with --force ..."
docker compose exec -T spark-master /opt/spark/bin/spark-submit \
    --master spark://spark-master:7077 \
    /opt/spark_jobs/extract_interval_reads.py "$LOAD_DATE" --force >/dev/null 2>&1

read -r after_hash after_at <<<"$(manifest_hash)"
echo "  after    hash=${after_hash}"
echo "           extracted_at=${after_at}"
echo

if [ "$before_hash" != "$after_hash" ]; then
    echo "NOT IDEMPOTENT: the content hash changed across a replay of ${LOAD_DATE}" >&2
    exit 1
fi
if [ "$before_at" = "$after_at" ]; then
    echo "SUSPECT: extracted_at did not change, so the extract may not have re-run." >&2
    echo "A hash that matches because nothing happened proves nothing." >&2
    exit 1
fi
echo "IDEMPOTENT: ${LOAD_DATE} replayed to an identical content hash,"
echo "            and extracted_at moved, so the work really was redone."
