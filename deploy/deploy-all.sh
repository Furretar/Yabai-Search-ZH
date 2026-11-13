#!/usr/bin/env bash
# Fast deploy from this dev checkout to every serving copy.
#
# Usage: deploy/deploy-all.sh [--data] [--fast] [--pc]
#   --data   also copy the databases to the laptop and tablet: the MOE database and the
#            generated indexes. Refuses to run when an index is stale (regenerate_everything.py --check).
#   --fast   skip the test suite
#   --pc     restart the Windows scheduled task after the sync
#
# This is the direct path: it rsyncs the working tree, so it does not need a
# commit or push. Use deploy/release.sh when you want the git-based release.
# The Windows PC follows ~/Desktop/ai/yabai-laptop through Syncthing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LAPTOP_DIR="${LAPTOP_DIR:-$HOME/Desktop/ai/yabai-laptop}"
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

# The native opencc wheel needs libstdc++ outside `nix develop`. Check the
# import instead of trusting a non-empty LD_LIBRARY_PATH: a desktop session may
# set one that has nothing to do with libstdc++.
PYTHON=python3
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
if [ -d /nix/store ] && ! "$PYTHON" -c "import opencc" >/dev/null 2>&1; then
    for lib_dir in /nix/store/*-gcc-*-lib/lib; do
        [ -e "$lib_dir/libstdc++.so.6" ] || continue
        if LD_LIBRARY_PATH="$lib_dir:${LD_LIBRARY_PATH:-}"             "$PYTHON" -c "import opencc" >/dev/null 2>&1
        then
            export LD_LIBRARY_PATH="$lib_dir:${LD_LIBRARY_PATH:-}"
            break
        fi
    done
fi

if [ "$SYNC_DATA" = 1 ]; then
    # The derived indexes (definition_grams, gram_words, head_grams, ...) are built from the source
    # databases, and several of them are exact negative filters: a stale one hides results that exist.
    # Importers rebuild them when they finish; this makes sure nothing stale ships.
    echo "== indexes are current?"
    "$PYTHON" scripts/regenerate_everything.py --check || {
        echo "Stale indexes: run scripts/regenerate_everything.py, then deploy again." >&2
        exit 1
    }
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
    # Dictionaries: the plain databases, and the database of each dictionary that has its own folder.
    mkdir -p "$LAPTOP_DIR/data/dictionary-databases/moe-taiwan" "$LAPTOP_DIR/data/dictionary-databases/baidu" \
        "$LAPTOP_DIR/data/generated-indexes-optimizations"
    rsync -a data/dictionary-databases/*.sqlite "$LAPTOP_DIR/data/dictionary-databases/"
    rsync -a data/dictionary-databases/moe-taiwan/moe.sqlite "$LAPTOP_DIR/data/dictionary-databases/moe-taiwan/"
    rsync -a data/dictionary-databases/baidu/baidu.sqlite "$LAPTOP_DIR/data/dictionary-databases/baidu/"
    # The segmenter's word list; its cache (segmenter.pickle, below) only loads beside the same list.
    mkdir -p "$LAPTOP_DIR/data/dl-base-dictionaries"
    rsync -a data/dl-base-dictionaries/cedict_userdict.txt data/dl-base-dictionaries/cedict_userdict.version "$LAPTOP_DIR/data/dl-base-dictionaries/"
    rsync -a data/generated-indexes-optimizations/*.sqlite data/generated-indexes-optimizations/segmenter.pickle "$LAPTOP_DIR/data/generated-indexes-optimizations/"
fi
systemctl --user restart yabai-app@laptop

echo "== tablet"
adb connect "$TABLET_ADB" >/dev/null 2>&1 || true
adb -s "$TABLET_ADB" forward "tcp:$TABLET_PORT" tcp:"$TABLET_PORT" >/dev/null 2>&1 || true
rsync -a --delete -e "ssh -p $TABLET_PORT -i $TABLET_KEY -o StrictHostKeyChecking=no" \
    "${COMMON_EXCLUDES[@]}" "$ROOT/" "$TABLET_SSH:$TABLET_DIR/"
if [ "$SYNC_DATA" = 1 ]; then
    TABLET_RSH="ssh -p $TABLET_PORT -i $TABLET_KEY -o StrictHostKeyChecking=no"
    $TABLET_RSH "$TABLET_SSH" "mkdir -p $TABLET_DIR/data/dictionary-databases/moe-taiwan $TABLET_DIR/data/dictionary-databases/baidu $TABLET_DIR/data/generated-indexes-optimizations"
    rsync -a -e "$TABLET_RSH" data/dictionary-databases/*.sqlite "$TABLET_SSH:$TABLET_DIR/data/dictionary-databases/"
    rsync -a -e "$TABLET_RSH" data/dictionary-databases/moe-taiwan/moe.sqlite "$TABLET_SSH:$TABLET_DIR/data/dictionary-databases/moe-taiwan/"
    rsync -a -e "$TABLET_RSH" data/dictionary-databases/baidu/baidu.sqlite "$TABLET_SSH:$TABLET_DIR/data/dictionary-databases/baidu/"
    rsync -a -e "$TABLET_RSH" data/generated-indexes-optimizations/*.sqlite data/generated-indexes-optimizations/segmenter.pickle "$TABLET_SSH:$TABLET_DIR/data/generated-indexes-optimizations/"
fi
ssh -p "$TABLET_PORT" -i "$TABLET_KEY" -o StrictHostKeyChecking=no \
    "$TABLET_SSH" "pgrep -f '[g]unicorn yabai_site' | xargs -r kill"

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
