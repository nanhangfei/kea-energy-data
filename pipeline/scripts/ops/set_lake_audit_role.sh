#!/usr/bin/env bash
# Give the lake summary a login of its own: kea_lake_audit, which owns the
# lake_audit schema and nothing else.
#
# WHY IT EXISTS. analysis/metabase/summarise_lake.py rebuilds the four rollups
# the Lake dashboard reads, and it used to write them as `kea`, the superuser
# of the pipeline's source database. That was tolerable while only a person ran
# it. The Analysis step now runs in Airflow too (kea_daily_pipeline's
# `dashboards` group), and Airflow is deliberately given no superuser: the four
# extract DAGs hold one system's credential each, so that a cross-system read
# fails. Handing the superuser to a nightly task would void that argument.
#
# WHAT IT CAN DO. Own lake_audit and its tables -- the job drops and recreates
# them on every run, which needs ownership -- and connect. It holds no grant on
# any src_* schema or on gen_audit, so it can read no source system at all.
#
# RUN AS THE SUPERUSER, by `make dashboards`, never by Airflow. Idempotent: the
# role, the schema and the ownership are ensured, and the password is set from
# .env every time, so re-running rotates nothing unless .env changed.
set -euo pipefail
cd "$(dirname "$0")/../../.."             # -> data/

PSQL="docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea"
[ -f .env ] || { echo "no .env -- copy .env.example and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a

if [ -z "${KEA_LAKE_AUDIT_PASSWORD:-}" ]; then
    cat >&2 <<'EOF'
KEA_LAKE_AUDIT_PASSWORD is empty in .env. Generate one with
  python3 -c "import secrets; print(secrets.token_urlsafe(24))"
and put the same value inside AIRFLOW_CONN_KEA_LAKE_AUDIT in that file.
EOF
    exit 1
fi

# One transaction. The password travels on stdin, never in argv, where `ps`
# and shell history would show it.
{
    cat <<'SQL'
SET client_min_messages = warning;
BEGIN;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kea_lake_audit') THEN
        CREATE ROLE kea_lake_audit LOGIN;
    END IF;
END $$;
GRANT CONNECT ON DATABASE kea TO kea_lake_audit;

-- The schema belongs to the role, so the job never needs CREATE on the
-- database: it only ever creates tables inside a schema it owns.
CREATE SCHEMA IF NOT EXISTS lake_audit AUTHORIZATION kea_lake_audit;
ALTER SCHEMA lake_audit OWNER TO kea_lake_audit;

-- Rollups an earlier run built as `kea` are handed over, or the next
-- overwrite could not drop them.
DO $$ DECLARE t record; BEGIN
    FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'lake_audit' AND c.relkind IN ('r', 'p', 'v', 'm')
                AND pg_get_userbyid(c.relowner) <> 'kea_lake_audit'
    LOOP
        EXECUTE format('ALTER TABLE lake_audit.%I OWNER TO kea_lake_audit', t.relname);
    END LOOP;
END $$;
SQL
    printf "ALTER ROLE kea_lake_audit PASSWORD %s;\nCOMMIT;\n" \
        "$(printf "%s" "$KEA_LAKE_AUDIT_PASSWORD" | sed "s/'/''/g; s/^/'/; s/\$/'/")"
} | $PSQL -q -f - >/dev/null

echo "  kea_lake_audit  password set; owns lake_audit and its tables, reads no source system"
