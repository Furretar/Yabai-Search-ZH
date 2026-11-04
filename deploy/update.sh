#!/usr/bin/env bash
# Pull the latest code on a serving machine and restart its app.
# If the new version does not answer a health check, go back to the old one.
#
# Usage: deploy/update.sh <role>      role = laptop | tablet
# Run it inside that machine's clone (release.sh does this for you).
set -euo pipefail

ROLE="${1:?usage: update.sh laptop|tablet}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f deploy/env.local ]; then
    set -a
    . deploy/env.local
    set +a
fi
PORT="${PORT:-8000}"

OLD="$(git rev-parse HEAD)"

# --ff-only refuses to merge: a serving clone must never carry its own edits.
git pull --ff-only
NEW="$(git rev-parse HEAD)"

# Same interpreter choice as run.sh: .venv first, then system python.
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi

# Update Python packages where uv exists (the tablet manages its own with pip).
if command -v uv >/dev/null 2>&1; then
    uv sync --frozen --no-dev
fi

# Ask the supervisor loop (run.sh) to restart by stopping the app process.
restart() {
    if [ -f deploy/run/gunicorn.pid ] && kill -0 "$(cat deploy/run/gunicorn.pid)" 2>/dev/null; then
        kill "$(cat deploy/run/gunicorn.pid)"
    fi
}

# Wait up to 40 s for /__ping to answer. Python is used instead of curl
# because curl is missing on some Termux installs.
healthy() {
    for _ in $(seq 40); do
        if $PY - "$PORT" "${YABAI_ORIGIN_SECRET:-}" <<'PY' 2>/dev/null
import sys, urllib.request
req = urllib.request.Request(
    f"http://127.0.0.1:{sys.argv[1]}/__ping",
    headers={"X-Origin-Secret": sys.argv[2]},
)
sys.exit(0 if urllib.request.urlopen(req, timeout=3).status == 200 else 1)
PY
        then
            return 0
        fi
        sleep 1
    done
    return 1
}

if [ "$OLD" = "$NEW" ]; then
    echo "[$ROLE] already at $NEW, nothing to restart"
    exit 0
fi

restart
sleep 3
if healthy; then
    echo "[$ROLE] updated ${OLD:0:7} -> ${NEW:0:7}, healthy"
else
    echo "[$ROLE] new version failed its health check, rolling back" >&2
    git reset --hard "$OLD"
    restart
    healthy || echo "[$ROLE] rollback also unhealthy, check the logs" >&2
    exit 1
fi
