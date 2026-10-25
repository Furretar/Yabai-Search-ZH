#!/usr/bin/env bash
# Purge the Cloudflare edge cache for yabaisearch.org after a deploy.
#
# Requires CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID in the environment.
# Get the token from: https://dash.cloudflare.com/profile/api-tokens
#   Permissions: Zone.Cache Purge
# Get the zone ID from the Cloudflare dashboard (Overview page, right sidebar).
#
# Usage: ./scripts/purge_cloudflare_cache.sh
set -euo pipefail

if [[ -z "${CLOUDFLARE_API_TOKEN:-}" || -z "${CLOUDFLARE_ZONE_ID:-}" ]]; then
    echo "Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID first." >&2
    exit 1
fi

curl -sS -X POST \
    "https://api.cloudflare.com/client/v4/zones/${CLOUDFLARE_ZONE_ID}/purge_cache" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"purge_everything":true}' \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('success:', d.get('success')); print(d.get('errors', []))"

echo "Cache purged. Note: the service worker still caches locally; bump"
echo "CACHE_NAME in static/sw.js to force clients to drop old caches."
