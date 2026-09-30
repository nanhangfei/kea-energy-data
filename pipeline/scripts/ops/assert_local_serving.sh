#!/usr/bin/env bash
# Refuse unless KEA_MIGRATION_DSN names this machine's kea-db.
#
# The demo rebuild clears decision history as the owner (`make publish-serving
# DEMO_RESET=1`). That is right for a local demo database and wrong for a
# shared one, whose history is not this machine's to delete. To publish into a
# shared database, use `make publish-serving` without DEMO_RESET, which deletes
# nothing.
#
# `make rebuild-all` calls this FIRST as well, so a remote DSN fails before
# twenty minutes of rebuild rather than after.
set -euo pipefail
cd "$(dirname "$0")/../../.."             # -> data/

target=$(grep -E '^KEA_MIGRATION_DSN=' .env | cut -d= -f2- | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.stdin.read().strip()).hostname or "")')
case "$target" in
  host.docker.internal|localhost|127.0.0.1|kea-db) ;;
  *) echo "refusing: KEA_MIGRATION_DSN names '${target}', not this machine's kea-db" >&2; exit 1 ;;
esac
