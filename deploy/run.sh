#!/usr/bin/env bash
# Keep the app running, restarting it whenever it exits.
#
# Usage: deploy/run.sh <role>      role = laptop | tablet | dev
#
# laptop and tablet run behind the shared-secret gate (deploy/guard.py) on
# port 8000. dev runs the plain app on port 8001 (protect it with Cloudflare
# Access instead). Settings per machine go in deploy/env.local (not in git).
set -u

ROLE="${1:?usage: run.sh laptop|tablet|dev}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load per-machine settings such as YABAI_ORIGIN_SECRET and PORT.
if [ -f deploy/env.local ]; then
    set -a
    . deploy/env.local
    set +a
fi

case "$ROLE" in
    dev)
        PORT="${PORT:-8001}"
        APP="yabai_search.main:app"
        ;;
    laptop | tablet)
        PORT="${PORT:-8000}"
        APP="deploy.guard:app"
        ;;
    *)
        echo "unknown role: $ROLE" >&2
        exit 2
        ;;
esac

# The app reports this in its X-Served-By header.
export YABAI_SERVED_BY="$ROLE"

# Pick the interpreter: the project's .venv first (what the laptop services
# use, and it works outside nix develop), then uv, then plain python (Termux).
if [ -x .venv/bin/python ]; then
    LAUNCH=(.venv/bin/python -m uvicorn)
elif command -v uv >/dev/null 2>&1; then
    LAUNCH=(uv run --no-sync uvicorn)
else
    LAUNCH=(python -m uvicorn)
fi

# NixOS has no system libstdc++, so opencc's compiled extension fails to import
# unless a gcc lib folder is on LD_LIBRARY_PATH. Inside `nix develop` the shell
# sets that; a systemd unit does not, and the service then dies at startup
# ("opencc_clib not found in Bazel runfiles"). Pick the first gcc lib folder
# where the import actually works, so a gcc update that changes the store hash
# cannot break the unit again. Non-Nix systems skip this block.
if [ -z "${LD_LIBRARY_PATH:-}" ] && [ -d /nix/store ]; then
    for lib_dir in /nix/store/*-gcc-*-lib/lib; do
        [ -e "$lib_dir/libstdc++.so.6" ] || continue
        if LD_LIBRARY_PATH="$lib_dir" "${LAUNCH[0]}" -c "import opencc" >/dev/null 2>&1; then
            export LD_LIBRARY_PATH="$lib_dir"
            break
        fi
    done
fi

mkdir -p run
CHILD=""
# On stop, take the app down with us.
trap '[ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; exit 0' TERM INT

while true; do
    # Bind to localhost only: cloudflared runs on the same machine.
    "${LAUNCH[@]}" "$APP" --host 127.0.0.1 --port "$PORT" &
    CHILD=$!
    echo "$CHILD" > run/uvicorn.pid
    wait "$CHILD"
    echo "app exited, restarting in 2s" >&2
    sleep 2
done
