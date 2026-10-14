#!/usr/bin/env bash
# The five gates `make deploy` passes before it changes anything. Read-only: it
# fetches origin/main and queries two databases, and writes nothing, so it is
# safe to run on its own at any time -- `deploy_gate.sh && make ci` is the whole
# check without the deploy.
#
#   1. .env exists.
#   2. The repo is clean, untracked files included. The publish and the
#      dashboard builders run what is on disk, so a stray file would ship. No
#      deploy reads anything else.
#   3. HEAD is origin/main. Only merged code is deployed, and a rollback is a
#      revert PR, which CI checks like any other change.
#   4. kea-db answers on KEA_MIGRATION_DSN. Only its host is printed.
#   5. No kea_daily_pipeline or kea_forecast_refresh run is queued or running:
#      the deploy and that run would both rebuild the warehouse and publish.
#      Airflow being down passes, because nothing can be in flight and
#      `make up` starts it. This never pauses or unpauses a DAG.
set -euo pipefail
cd "$(dirname "$0")/../../.."

refuse() { echo "deploy refused: $*" >&2; exit 1; }

[ -f .env ] || refuse ".env is missing; cp .env.example .env and fill it in"

dirty=$(git status --porcelain -- .)
if [ -n "$dirty" ]; then
    echo "$dirty" >&2
    refuse "uncommitted changes; commit them to their branch (or git stash -u), then git switch main"
fi

git fetch -q origin main || refuse "cannot fetch origin/main"
head=$(git rev-parse --short=12 HEAD)
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
    || refuse "HEAD is $head, not origin/main ($(git rev-parse --short=12 origin/main)); git switch main && git pull --ff-only"

host=$(sed -n 's/^KEA_MIGRATION_DSN=//p' .env | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.stdin.read().strip()).hostname or "")')
[ -n "$host" ] || refuse "KEA_MIGRATION_DSN is not set in .env"
docker run --rm --env-file .env --add-host=host.docker.internal:host-gateway postgres:17.10 \
    sh -c 'pg_isready -q -d "$KEA_MIGRATION_DSN"' \
    || refuse "kea-db at $host is not answering; start it (make serving-db) and retry"

if docker compose ps --status running --services 2>/dev/null | grep -qx airflow-postgres; then
    busy=$(docker compose exec -T airflow-postgres psql -U airflow -d airflow -X -tA -c "
        select dag_id || ' ' || run_id || ' ' || state from dag_run
         where dag_id in ('kea_daily_pipeline', 'kea_forecast_refresh')
           and state in ('queued', 'running');") \
        || refuse "cannot read Airflow's dag_run table"
    if [ -n "$busy" ]; then
        echo "$busy" >&2
        refuse "a pipeline run is in flight; let it finish, then retry"
    fi
    airflow="no pipeline run in flight"
else
    airflow="Airflow is down, so no run is in flight"
fi

echo "deploy gates passed: HEAD is origin/main ($head), kea-db at $host answers, $airflow"
