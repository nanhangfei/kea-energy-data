#!/usr/bin/env bash
# Fails when a serving migration that exists on the base was changed, renamed
# or deleted. Added files pass: a schema change is always the next V00N.
#
# migrate_serving.py refuses an applied migration whose checksum changed, but
# only at deploy time, against a live database, after the merge. This catches
# the same mistake in review. CI's `data` job passes the PR's base commit, and
# `make ci-migrations` passes origin/main.
#
# It diffs the merge base against the working tree, so locally an uncommitted
# edit fails too, and commits that reached the base after the branch point are
# not counted against this change. The pattern is migrate_serving.py's own
# glob, so the README beside the migrations can change freely.
set -euo pipefail
cd "$(dirname "$0")/../.."

BASE="${1:-}"
# A push that creates a branch reports an all-zero "before", and a bare run
# passes nothing. With no base there is nothing to compare, so say so and pass.
if [ -z "${BASE//0/}" ]; then
    echo "no base commit given; serving migrations not checked"
    exit 0
fi

base=$(git merge-base "$BASE" HEAD) || { echo "no merge base between $BASE and HEAD" >&2; exit 1; }
changed=$(git diff --name-status --diff-filter=DMRT "$base" -- 'migrations/serving/V[0-9]*__*.sql')
if [ -n "$changed" ]; then
    echo "$changed" >&2
    echo "applied migrations are immutable; add the next V00N instead" >&2
    exit 1
fi
echo "serving migrations: none that exist on $(git rev-parse --short "$base") changed"
