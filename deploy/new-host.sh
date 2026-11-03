#!/usr/bin/env bash
# Prepare the repository for a new host with as little manual work as possible.
#
# Usage: deploy/new-host.sh <name> ["Display Name"]
#
# <name> is the DNS label and the role passed to deploy/run.sh, e.g.
# `galaxy-tab-s2`. It creates deploy/cloudflared/<name>.yml, a starter
# deploy/env.local (if missing), and adds the name to the Worker's HOSTS list.
# It does not create the Cloudflare tunnel or start anything.
set -euo pipefail

HOST="${1:?usage: new-host.sh <name> [\"Display Name\"]}"
DISPLAY="${2:-$HOST}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

case "$HOST" in
    "" | *[!a-z0-9-]*)
        echo "host name must use lowercase letters, digits and hyphens: $HOST" >&2
        exit 2
        ;;
esac

# 1. Per-host settings. The file is gitignored and must never be synced.
if [ ! -f deploy/env.local ]; then
    cp deploy/env.local.example deploy/env.local
    {
        echo ""
        echo "# Added by deploy/new-host.sh"
        echo "YABAI_SERVED_BY=\"$DISPLAY\""
        echo "YABAI_CONTROL_URL=https://yabaisearch.org/__control/state"
    } >> deploy/env.local
    echo "created deploy/env.local (fill in the secrets and DB path)"
else
    echo "deploy/env.local already exists; leaving it alone"
fi

# 2. Tunnel config. Reuse an existing tunnel when cloudflared knows it.
CONFIG="deploy/cloudflared/$HOST.yml"
if [ -f "$CONFIG" ]; then
    echo "$CONFIG already exists; leaving it alone"
else
    TUNNEL_ID="${TUNNEL_ID:-}"
    if [ -z "$TUNNEL_ID" ] && command -v cloudflared >/dev/null 2>&1; then
        TUNNEL_ID="$(cloudflared tunnel list 2>/dev/null | awk -v n="yabai-$HOST" '$2 == n {print $1; exit}')"
    fi
    if [ -z "$TUNNEL_ID" ]; then
        echo "No tunnel 'yabai-$HOST' found yet. Create it once:"
        echo "  cloudflared tunnel create yabai-$HOST"
        echo "  cloudflared tunnel route dns yabai-$HOST $HOST.yabaisearch.org"
        echo "Then set TUNNEL_ID and re-run, or fill in the placeholders:"
        echo "  TUNNEL_ID=<id> deploy/new-host.sh $HOST \"$DISPLAY\""
        TUNNEL_ID="__TUNNEL_ID__"
    fi
    sed \
        -e "s|__HOST__|$HOST|g" \
        -e "s|__TUNNEL_ID__|$TUNNEL_ID|g" \
        -e "s|__CREDENTIALS_FILE__|$HOME/.cloudflared/$TUNNEL_ID.json|g" \
        deploy/cloudflared/host.yml.example > "$CONFIG"
    echo "created $CONFIG"
fi

# 3. The Worker must know the host name.
deploy/worker/add-host.sh "$HOST"

cat <<EOF

Next steps for '$HOST' (display name: '$DISPLAY'):
  1. Fill deploy/env.local. It needs the same DJANGO_SECRET_KEY,
     YABAI_ORIGIN_SECRET and YABAI_REPL_SECRET as every other host,
     plus DJANGO_DB_PATH and (for a LAN/Tailscale tablet) YABAI_BIND=0.0.0.0
     and DJANGO_ALLOWED_HOSTS.
  2. Start the app and its tunnel:
       deploy/run.sh $HOST
       deploy/tunnel.sh $HOST
     On Termux, add both lines to ~/.termux/boot/start.sh.
  3. Redeploy the Worker and the Durable Object:
       cd deploy/worker && npx wrangler deploy
  4. Check:
       deploy/check.sh
       curl https://yabaisearch.org/__probe
EOF
