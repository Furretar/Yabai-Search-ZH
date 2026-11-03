#!/usr/bin/env bash
# Keep the app running, restarting it whenever it exits.
#
# Usage: deploy/run.sh <role>      role = laptop | tablet | dev
#
# laptop and tablet run behind the shared-secret gate (OriginSecretMiddleware,
# active when YABAI_ORIGIN_SECRET is set) on port 8000. dev runs without that
# gate on port 8001 (protect it with Cloudflare Access instead). Settings per
# machine go in deploy/env.local (not in git); DJANGO_SECRET_KEY is required.
set -u

ROLE="${1:?usage: run.sh laptop|tablet|dev}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load per-machine settings such as YABAI_ORIGIN_SECRET, DJANGO_SECRET_KEY, PORT.
if [ -f deploy/env.local ]; then
    set -a
    . deploy/env.local
    set +a
fi

case "$ROLE" in
    dev)
        PORT="${PORT:-8001}"
        # The dev copy sits behind Cloudflare Access, not the Worker's secret.
        unset YABAI_ORIGIN_SECRET
        ;;
    laptop | tablet)
        PORT="${PORT:-8000}"
        ;;
    *)
        echo "unknown role: $ROLE" >&2
        exit 2
        ;;
esac

# gunicorn runs the Django app; threads (not async) keep searches concurrent.
APP="yabai_site.wsgi:application"
THREADS="${YABAI_THREADS:-8}"

# Accounts replication (accounts/replication.py) is on for laptop and tablet when
# YABAI_REPL_PEER_URL is set in deploy/env.local. dev never replicates.
REPL=0
if [ "$ROLE" != dev ] && [ -n "${YABAI_REPL_PEER_URL:-}" ]; then
    REPL=1
    export YABAI_REPL_NODE="$ROLE"
fi

# The app reports this in its X-Served-By header.
export YABAI_SERVED_BY="$ROLE"

# Pick the interpreter: the project's .venv first (what the laptop services
# use, and it works outside nix develop), then uv, then plain python (Termux).
if [ -x .venv/bin/python ]; then
    LAUNCH=(.venv/bin/python -m gunicorn)
elif command -v uv >/dev/null 2>&1; then
    LAUNCH=(uv run --no-sync gunicorn)
else
    LAUNCH=(python -m gunicorn)
fi

# Plain python for manage.py and the replication commands (same choice as above).
if [ -x .venv/bin/python ]; then
    PYRUN=(.venv/bin/python)
elif command -v uv >/dev/null 2>&1; then
    PYRUN=(uv run --no-sync python)
else
    PYRUN=(python)
fi

# NixOS has no system libstdc++, so opencc's compiled extension fails to import
# unless a gcc lib folder is on LD_LIBRARY_PATH. Inside `nix develop` the shell
# sets that; a systemd unit does not, and the service then dies at startup
# ("opencc_clib not found in Bazel runfiles"). Pick the first gcc lib folder
# where the import actually works, so a gcc update that changes the store hash
# cannot break the unit again. Non-Nix systems skip this block.
if [ -d /nix/store ] && ! "${PYRUN[@]}" -c "import opencc" >/dev/null 2>&1; then
    for lib_dir in /nix/store/*-gcc-*-lib/lib; do
        [ -e "$lib_dir/libstdc++.so.6" ] || continue
        if LD_LIBRARY_PATH="$lib_dir:${LD_LIBRARY_PATH:-}"             "${PYRUN[@]}" -c "import opencc" >/dev/null 2>&1
        then
            export LD_LIBRARY_PATH="$lib_dir:${LD_LIBRARY_PATH:-}"
            break
        fi
    done
fi

mkdir -p deploy/run
CHILD=""
SIDE=()   # background helpers (replication server and pull loop)
# On stop, take the app and the helpers down with us.
trap '[ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; for p in "${SIDE[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done; exit 0' TERM INT

if [ "$REPL" = 1 ]; then
    # Answer the peer from the start, so a machine booting at the same time as us
    # can catch up from our copy while we catch up from theirs.
    "${PYRUN[@]}" -m accounts.replication serve &
    SIDE+=($!)
fi

FIRST=1
while true; do
    if [ "$REPL" = 1 ]; then
        # THE GATE: do not serve until the peer's newer accounts database is in
        # place. The first time we wait for the peer to appear (both machines may be
        # booting); on later restarts (update.sh) we do not wait.
        WAIT=0
        [ "$FIRST" = 1 ] && WAIT="${YABAI_REPL_WAIT:-30}"
        "${PYRUN[@]}" -m accounts.replication catch-up --wait "$WAIT" \
            || echo "replication catch-up failed; serving the local copy" >&2
    fi
    if [ "$REPL" = 1 ] || [ "${YABAI_MIGRATE:-0}" = 1 ]; then
        # After catch-up: the installed copy may carry an older schema.
        "${PYRUN[@]}" manage.py migrate --noinput \
            || echo "migrate failed; check the log" >&2
    fi
    if [ "$REPL" = 1 ] && [ "$FIRST" = 1 ]; then
        # Only now start pulling, so it cannot race the catch-up above.
        "${PYRUN[@]}" -m accounts.replication pull &
        SIDE+=($!)
    fi
    FIRST=0

    # Bind to localhost only: cloudflared runs on the same machine.
    "${LAUNCH[@]}" "$APP" --bind "127.0.0.1:$PORT" --workers 1 \
        --worker-class gthread --threads "$THREADS" --timeout 60 &
    CHILD=$!
    echo "$CHILD" > deploy/run/uvicorn.pid
    wait "$CHILD"
    echo "app exited, restarting in 2s" >&2
    sleep 2
done
