#!/usr/bin/env bash
# Prove the four extract credentials are actually isolated.
#
# Stage 3's first checkbox is "four independent extract jobs; a billing
# failure does not block metering". Independence starts at the credential: the
# source DDL says the roles exist "so that a mistake which joins across
# systems at extract time fails rather than silently working". That is a claim
# about what the database REFUSES, and the only way to know it holds is to try
# the refused thing and check it fails.
#
# TWENTY READS. Sixteen that must fail -- each role against the three schemas
# it does not own, and all four against gen_audit -- and four positive controls,
# because a test where everything fails is also passed by a typo in the
# hostname.
#
# gen_audit is in the list for a specific reason. It holds the defect manifest:
# 92,808 rows saying exactly which readings were corrupted and how. It is
# deliberately NOT a src_* schema, because kea_source_schema.sql ends with
# ALTER DEFAULT PRIVILEGES ... GRANT SELECT, so a manifest sitting there would
# be pulled into bronze on the next run and the pipeline would be marking its
# own homework -- "detecting" every defect by reading the answer key.
#
# -h kea-source-db IS LOAD-BEARING, though not for the reason it first looks.
# pg_hba.conf has `local all all trust` before the scram-sha-256 rule, so a
# unix-socket connection inside the container logs in with no password at all.
# That does NOT weaken the sixteen refusals: trust bypasses AUTHENTICATION, and
# the grants are AUTHORIZATION, so a forbidden read still fails over the socket
# (verified).
#
# What it breaks is the four positive controls. Over the socket they pass
# whether or not a password was ever set, so the script would report "isolation
# proven" on a system where the pipeline cannot actually connect -- which is
# precisely the state this repo was in until `make source-roles` existed.
# Forcing TCP is what makes the controls mean something.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

[ -f .env ] || { echo "no .env" >&2; exit 1; }
set -a; . ./.env; set +a

pass=0
fail=0

# Runs a query as one extract role over TCP. Returns psql's exit status.
probe() {
    local system=$1 sql=$2 var
    var="KEA_EXTRACT_$(echo "$system" | tr '[:lower:]' '[:upper:]')_PASSWORD"
    docker compose exec -T \
        -e PGPASSWORD="${!var:-}" kea-source-db \
        psql -h kea-source-db -U "kea_extract_$system" -d kea -At -c "$sql" >/dev/null 2>&1
}

# The read must be REFUSED. Success here is the failure.
must_not_read() {
    local system=$1 schema=$2 table=$3
    if probe "$system" "SELECT 1 FROM $schema.$table LIMIT 1"; then
        echo "  FAIL  kea_extract_$system CAN read $schema.$table -- the systems are not isolated"
        fail=$((fail + 1))
    else
        echo "  ok    kea_extract_$system refused $schema"
        pass=$((pass + 1))
    fi
}

# The positive control. Without these, a broken hostname passes everything.
must_read() {
    local system=$1 schema=$2 table=$3
    if probe "$system" "SELECT 1 FROM $schema.$table LIMIT 1"; then
        echo "  ok    kea_extract_$system reads $schema.$table"
        pass=$((pass + 1))
    else
        echo "  FAIL  kea_extract_$system CANNOT read its own $schema.$table"
        echo "        (no password set? run: make source-roles)"
        fail=$((fail + 1))
    fi
}

echo "Each role reads its own system:"
must_read billing   src_billing   accounts
must_read metering  src_metering  interval_reads
must_read catalogue src_catalogue price_plans
must_read weather   src_weather   daily_observations

echo
echo "And no other system:"
must_not_read billing   src_metering  interval_reads
must_not_read billing   src_catalogue price_plans
must_not_read billing   src_weather   daily_observations
must_not_read metering  src_billing   accounts
must_not_read metering  src_catalogue price_plans
must_not_read metering  src_weather   daily_observations
must_not_read catalogue src_billing   accounts
must_not_read catalogue src_metering  interval_reads
must_not_read catalogue src_weather   daily_observations
must_not_read weather   src_billing   accounts
must_not_read weather   src_metering  interval_reads
must_not_read weather   src_catalogue price_plans

echo
echo "And none of them can read the answer key:"
must_not_read billing   gen_audit defects
must_not_read metering  gen_audit defects
must_not_read catalogue gen_audit defects
must_not_read weather   gen_audit defects

echo
if [ "$fail" -ne 0 ]; then
    echo "ISOLATION NOT PROVEN: $fail of $((pass + fail)) checks failed" >&2
    exit 1
fi
echo "ISOLATION PROVEN: $pass checks, four credentials, four systems"
