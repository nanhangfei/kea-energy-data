#!/usr/bin/env bash
# Fetch the Low Carbon London smart-meter dataset into data/raw/.
#
# data/README.md's rule for data/raw/ is "source extracts as received, never
# edited by hand". So this script only ever writes: the archive exactly as
# Kaggle served it, a checksum, and a provenance note. Nothing downstream
# modifies any of it.
#
# No kaggle CLI needed - the API takes HTTP basic auth and curl is already here.
#
#   bash pipeline/scripts/gen_source/fetch_lcl.sh [--force]
set -euo pipefail

SLUG="jeanmidev/smart-meters-in-london"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/raw/lcl"
ZIP="$DEST/smart-meters-in-london.zip"
FORCE="${1:-}"

[ -f .env ] || { echo "data/.env missing - cp .env.example .env"; exit 1; }
KAGGLE_USERNAME=$(grep '^KAGGLE_USERNAME=' .env | cut -d= -f2-)
KAGGLE_KEY=$(grep '^KAGGLE_KEY=' .env | cut -d= -f2-)
if [ -z "$KAGGLE_USERNAME" ] || [ -z "$KAGGLE_KEY" ]; then
  echo "KAGGLE_USERNAME / KAGGLE_KEY not set in data/.env"
  echo "  kaggle.com -> Settings -> API -> Create New Token"
  exit 1
fi

mkdir -p "$DEST"

# Re-runnable: an unchanged archive is left alone. --force fetches to a new
# timestamped name rather than overwriting, so the as-received file is never
# silently replaced.
if [ -f "$ZIP" ] && [ "$FORCE" != "--force" ]; then
  if [ -f "$DEST/MANIFEST.sha256" ] && (cd "$DEST" && shasum -a 256 -c MANIFEST.sha256 --status 2>/dev/null); then
    echo "already present and checksum matches - skipping"
    echo "  $ZIP"
    exit 0
  fi
  echo "archive present but checksum missing or stale; re-fetching"
fi
[ "$FORCE" = "--force" ] && ZIP="$DEST/smart-meters-in-london.$(date -u +%Y%m%dT%H%M%SZ).zip"

echo "fetching $SLUG ..."
curl -fL --progress-bar \
     -u "$KAGGLE_USERNAME:$KAGGLE_KEY" \
     -o "$ZIP" \
     "https://www.kaggle.com/api/v1/datasets/download/$SLUG"

# Kaggle answers an auth failure with 200 and an HTML page, so check the type.
if ! file -b "$ZIP" | grep -qi zip; then
  echo "not a zip - most likely bad credentials. First bytes:"; head -c 200 "$ZIP"; echo
  rm -f "$ZIP"; exit 1
fi

(cd "$DEST" && shasum -a 256 "$(basename "$ZIP")" > MANIFEST.sha256)

cat > "$DEST/PROVENANCE.md" <<EOF
# Low Carbon London smart-meter data

| | |
|---|---|
| Source | https://www.kaggle.com/datasets/$SLUG |
| Retrieved | $(date -u +%Y-%m-%dT%H:%M:%SZ) |
| Archive | $(basename "$ZIP") |
| Size | $(du -h "$ZIP" | cut -f1) |
| SHA-256 | $(cut -d' ' -f1 "$DEST/MANIFEST.sha256") |

Half-hourly electricity readings from ~5,567 London households, Nov 2011 - Feb
2014, from the Low Carbon London trial. Used here as the source-system feed for
Stage 2; see design/kea-data-engineering-stack.md.

It is UK data used for a New Zealand product. The transform that makes that
defensible - preserving local clock time, shifting the seasons, and
regenerating NZ daylight saving - is in
pipeline/scripts/gen_source/uk_to_nz.py, with its reasoning.

As received. Never edited by hand.
EOF

echo
echo "$(du -h "$ZIP" | cut -f1)  $ZIP"
echo "wrote MANIFEST.sha256 and PROVENANCE.md"
