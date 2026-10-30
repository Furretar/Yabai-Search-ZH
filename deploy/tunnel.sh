#!/usr/bin/env bash
# Keep this machine's Cloudflare tunnel connector running.
#
# Usage: deploy/tunnel.sh <role>      role = laptop | tablet | dev
# Reads deploy/cloudflared/<role>.yml (fill in its tunnel ID first).
set -u

ROLE="${1:?usage: tunnel.sh laptop|tablet|dev}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CHILD=""
trap '[ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; exit 0' TERM INT

while true; do
    cloudflared tunnel --config "deploy/cloudflared/$ROLE.yml" --no-autoupdate run &
    CHILD=$!
    wait "$CHILD"
    echo "cloudflared exited, restarting in 5s" >&2
    sleep 5
done
