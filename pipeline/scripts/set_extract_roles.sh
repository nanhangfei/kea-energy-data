#!/usr/bin/env bash
# Give the four extract roles a password, so the pipeline can actually connect.
#
# kea_source_schema.sql creates kea_extract_billing / _metering / _catalogue /
# _weather with LOGIN and NO PASSWORD, deliberately: "set it out of band from
# the secret store, so no credential ever lands in this repo". Nothing has ever
# set them, so today rolpassword IS NULL for all four and pg_hba's remote rule
# is scram-sha-256 -- they cannot log in at all.
#
# WHY THIS MATTERS MORE THAN IT LOOKS. The obvious shortcut is to extract as
# the superuser `kea`, whose password is KEA_SOURCE_PASSWORD and which works
# today. That would quietly make Stage 3's first checkbox untrue. The DDL is
# explicit about what the four roles buy:
#
#   "The pipeline should use them separately, so that a mistake which joins
#    across systems at extract time fails rather than silently working."
#
# A cross-system join at extract time is exactly the kind of error that
# produces a plausible number, and grants are the only thing that turns it
# into an error instead.
#
# ON VAULT. The DDL says "the secret store", and a Vault container is running.
# It is NOT the store here, and pretending otherwise would be worse than not
# using it: `vault` runs in dev mode with no volume, so its contents are
# in-memory and vanish on restart. .env is the store -- gitignored, and where
# every other credential in this stack already lives. A copy is written to
# Vault when it is reachable, so the pattern has something to point at and
# moving the store later is a change to one connection factory.
#
# Idempotent: re-running rotates nothing unless the .env value changed.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

PSQL="docker compose exec -T kea-source-db psql -v ON_ERROR_STOP=1 -U kea -d kea"

[ -f .env ] || { echo "no .env -- copy .env.example and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a

fail=0
for system in billing metering catalogue weather; do
    var="KEA_EXTRACT_$(echo "$system" | tr '[:lower:]' '[:upper:]')_PASSWORD"
    password="${!var:-}"
    if [ -z "$password" ]; then
        echo "  $var is empty in .env" >&2
        fail=1
        continue
    fi

    # Piped in on stdin, never as an argument: a password in argv is visible
    # to `ps` for every user on the box and lands in shell history.
    printf "ALTER ROLE kea_extract_%s PASSWORD %s;\n" \
        "$system" "$(printf "%s" "$password" | sed "s/'/''/g; s/^/'/; s/\$/'/")" \
        | $PSQL -q -f - >/dev/null
    echo "  kea_extract_$system  password set"

    if docker compose exec -T vault sh -c 'vault status >/dev/null 2>&1'; then
        docker compose exec -T \
            -e VAULT_TOKEN="${VAULT_DEV_TOKEN}" -e VAULT_ADDR=http://127.0.0.1:8200 \
            vault vault kv put "secret/kea/extract/$system" password="$password" >/dev/null 2>&1 \
            && echo "    copied to vault at secret/kea/extract/$system (in-memory; dev mode)"
    fi
done

if [ "$fail" -ne 0 ]; then
    cat >&2 <<'EOF'

Set the four passwords in .env first. They are yours to choose; generate them with

  python3 -c "import secrets; print(secrets.token_urlsafe(24))"

and put the same values inside the AIRFLOW_CONN_KEA_SRC_* URIs in that file.
EOF
    exit 1
fi

echo
echo "Four roles, four credentials. Prove the isolation with: make assert-isolation"
