#!/usr/bin/env bash
# Give kea_loader and kea_app a password, so Stage 5 can actually connect.
#
# The exact counterpart of set_extract_roles.sh, at the other end of the
# pipeline. kea_serving_schema.sql creates both roles with LOGIN and NO
# PASSWORD and says why: "No password here; set it out of band from the secret
# store." Nothing had ever set them, so rolpassword was NULL for both and
# neither could log in.
#
# WHY NOT JUST CONNECT AS `kea`. The superuser is already in
# AIRFLOW_CONN_KEA_SERVING and would work today. It would also make design
# §1.1 untrue -- "kea_loader has no grant on app, not even USAGE, and a
# pipeline bug cannot reach it even by fully-qualified name". That guarantee is
# a property of the CREDENTIAL. Loading as the owner keeps the sentence in the
# document and removes the thing it describes.
#
# kea_app is set here too, though nothing in this repo uses it yet: it is the
# agent's read-only role, it has the same NULL password for the same reason,
# and setting one and not the other would leave a trap for whoever wires up the
# API in Stage 6.
#
# Idempotent: re-running rotates nothing unless the .env value changed.
set -euo pipefail
cd "$(dirname "$0")/../.."             # -> data/

PSQL="docker compose exec -T kea-serving-db psql -v ON_ERROR_STOP=1 -U kea -d kea"

[ -f .env ] || { echo "no .env -- copy .env.example and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a

fail=0
for role in loader app; do
    var="KEA_$(echo "$role" | tr '[:lower:]' '[:upper:]')_PASSWORD"
    password="${!var:-}"
    if [ -z "$password" ]; then
        echo "  $var is empty in .env" >&2
        fail=1
        continue
    fi

    # On stdin, never in argv: an argument is visible to `ps` for every user on
    # the box and lands in shell history.
    printf "ALTER ROLE kea_%s PASSWORD %s;\n" \
        "$role" "$(printf "%s" "$password" | sed "s/'/''/g; s/^/'/; s/\$/'/")" \
        | $PSQL -q -f - >/dev/null
    echo "  kea_$role  password set"

    if docker compose exec -T vault sh -c 'vault status >/dev/null 2>&1'; then
        docker compose exec -T \
            -e VAULT_TOKEN="${VAULT_DEV_TOKEN}" -e VAULT_ADDR=http://127.0.0.1:8200 \
            vault vault kv put "secret/kea/serving/$role" password="$password" >/dev/null 2>&1 \
            && echo "    copied to vault at secret/kea/serving/$role (in-memory; dev mode)"
    fi
done

if [ "$fail" -ne 0 ]; then
    cat >&2 <<'EOF'

Set both passwords in .env first. They are yours to choose; generate them with

  python3 -c "import secrets; print(secrets.token_urlsafe(24))"

EOF
    exit 1
fi

echo
echo "Two roles, two credentials. The loader connects as kea_loader: make load-serving"
