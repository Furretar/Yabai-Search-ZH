#!/usr/bin/env bash
# Fast deploy from this dev checkout to every serving copy.
#
# Usage: deploy/deploy-all.sh [--data] [--fast] [--pc]
#   --data   also copy data/*.sqlite to the laptop and tablet
#   --fast   skip the test suite
#   --pc     restart the Windows scheduled task after the sync
#
# This is the direct path: it rsyncs the working tree, so it does not need a
# commit or push. Use deploy/release.sh when you want the git-based release.
# The Windows PC follows ~/yabai-laptop through Syncthing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LAPTOP_DIR="${LAPTOP_DIR:-$HOME/yabai-laptop}"
TABLET_SSH="${TABLET_SSH:-u0_a249@127.0.0.1}"
TABLET_ADB="${TABLET_ADB:-192.168.188.105:5555}"
TABLET_PORT="${TABLET_PORT:-8022}"
TABLET_KEY="${TABLET_KEY:-/tmp/opencode/termux_key}"
TABLET_DIR="${TABLET_DIR:-yabai-search}"
PC_SSH="${PC_SSH:-wyatt2024}"
if [ -f deploy/release.env ]; then
    # shellcheck disable=SC1091
    . deploy/release.env
fi

SYNC_DATA=0
RUN_TESTS=1
RESTART_PC=0
for arg in "$@"; do
    case "$arg" in
        --data) SYNC_DATA=1 ;;
        --fast) RUN_TESTS=0 ;;
        --pc) RESTART_PC=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

# The native opencc wheel needs libstdc++ outside `nix develop`.
if [ -z "${LD_LIBRARY_PATH:-}" ] && [ -d /nix/store ]; then
    for lib_dir in /nix/store/*-gcc-*-lib/lib; do
        if [ -e "$lib_dir/libstdc++.so.6" ]; then
            export LD_LIBRARY_PATH="$lib_dir"
            break
        fi
    done
fi

if [ "$RUN_TESTS" = 1 ] && [ -x .venv/bin/python ]; then
    echo "== tests"
    .venv/bin/python -m pytest -q
fi

# Keep each machine's local settings and generated/runtime state out of the
# copy. data/ is handled separately by --data.
COMMON_EXCLUDES=(
    --exclude=.git
    --exclude=.venv
    --exclude=.direnv
    --exclude=__pycache__
    --exclude='*.pyc'
    --exclude=.pytest_cache
    --exclude=.specify
    --exclude=.opencode
    --exclude=.stfolder
    --exclude=.stignore
    --exclude=.stversions
    --exclude=data
    --exclude=archives
    --exclude=run
    --exclude=deploy/env.local
    --exclude=deploy/release.env
    --exclude=deploy/worker/.wrangler
)

echo "== laptop"
rsync -a --delete "${COMMON_EXCLUDES[@]}" "$ROOT/" "$LAPTOP_DIR/"
if [ "$SYNC_DATA" = 1 ]; then
    rsync -a data/*.sqlite "$LAPTOP_DIR/data/"
fi
systemctl --user restart yabai-app@laptop

echo "== tablet"
adb connect "$TABLET_ADB" >/dev/null 2>&1 || true
adb -s "$TABLET_ADB" forward "tcp:$TABLET_PORT" tcp:"$TABLET_PORT" >/dev/null 2>&1 || true
rsync -a --delete -e "ssh -p $TABLET_PORT -i $TABLET_KEY -o StrictHostKeyChecking=no" \
    "${COMMON_EXCLUDES[@]}" "$ROOT/" "$TABLET_SSH:$TABLET_DIR/"
if [ "$SYNC_DATA" = 1 ]; then
    rsync -a -e "ssh -p $TABLET_PORT -i $TABLET_KEY -o StrictHostKeyChecking=no" \
        data/*.sqlite "$TABLET_SSH:$TABLET_DIR/data/"
fi
ssh -p "$TABLET_PORT" -i "$TABLET_KEY" -o StrictHostKeyChecking=no \
    "$TABLET_SSH" "pgrep -f '[u]vicorn deploy.guard' | xargs -r kill"

wait_for() {
    local name="$1"
    shift
    for _ in $(seq 1 180); do
        if "$@" >/dev/null 2>&1; then
            echo "  $name ok"
            return 0
        fi
        sleep 1
    done
    echo "  $name did not answer within 180 s" >&2
    return 1
}

echo "== health"
wait_for laptop curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8000/__ping
wait_for tablet ssh -p "$TABLET_PORT" -i "$TABLET_KEY" \
    -o StrictHostKeyChecking=no "$TABLET_SSH" \
    "curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8000/__ping"

if [ "$RESTART_PC" = 1 ]; then
    echo "== windows pc"
    ssh -o StrictHostKeyChecking=no "$PC_SSH" \
        "cmd /c \"schtasks /End /TN \\\"Yabai Search\\\" >nul 2>&1 & schtasks /Run /TN \\\"Yabai Search\\\" >nul 2>&1\"" || true
    echo "  pc task restarted (Syncthing still syncs the code)"
fi

echo "== done"
