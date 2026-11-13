#!/usr/bin/env bash
# Publish your committed changes to both serving machines.
# Run this inside the dev clone (the one you edit).
#
# Usage: deploy/release.sh [--data]
#   --data   also copy the databases to both machines (they are not in git): data/*.sqlite, the MOE
#            database and the generated indexes. Refuses to run when an index is stale (see below).
#
# Steps: tests -> git push -> laptop update -> tablet update.
# The machines are updated one at a time and the run stops at the first
# failure, so the other one keeps serving (the Worker fails over to it).
# It does not commit for you; commit first.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Where the other copies live. Override in deploy/release.env.
LAPTOP_DIR="$HOME/Desktop/ai/yabai-laptop"
TABLET_SSH="u0_a249@127.0.0.1"
TABLET_ADB="192.168.188.105:5555"
TABLET_PORT=8022
TABLET_KEY="$HOME/.ssh/termux_key"
TABLET_DIR="yabai-search"
[ -f deploy/release.env ] && . deploy/release.env

SYNC_DATA=0
[ "${1:-}" = "--data" ] && SYNC_DATA=1

if [ -n "$(git status --porcelain)" ]; then
    echo "Uncommitted changes. Commit them first." >&2
    exit 1
fi

if [ "$SYNC_DATA" = 1 ]; then
    # The derived indexes (definition_grams, gram_words, head_grams, ...) are built from the source
    # databases, and several of them are exact negative filters: a stale one hides results that exist.
    # Importers rebuild them when they finish; this makes sure nothing stale ships.
    echo "== indexes are current?"
    if [ -x .venv/bin/python ]; then PYCHECK=.venv/bin/python; else PYCHECK="uv run python"; fi
    $PYCHECK scripts/generate-indexes-optimizations/rebuild_all.py --check || {
        echo "Stale indexes: run scripts/generate-indexes-optimizations/rebuild_all.py, then release again." >&2
        exit 1
    }
fi

echo "== tests"
if [ -x .venv/bin/python ]; then
    .venv/bin/python -m pytest -q
else
    uv run pytest -q
fi

echo "== push"
git push

SSH=(ssh -p "$TABLET_PORT" -i "$TABLET_KEY" "$TABLET_SSH")

echo "== laptop"
if [ "$SYNC_DATA" = 1 ]; then
    # rsync writes each file under a temp name and renames it, so a running
    # app never sees a half-written database.
    rsync -a data/*.sqlite "$LAPTOP_DIR/data/"
    mkdir -p "$LAPTOP_DIR/data/moe-taiwan" "$LAPTOP_DIR/data/generated-indexes-optimizations"
    rsync -a data/moe-taiwan/moe.sqlite "$LAPTOP_DIR/data/moe-taiwan/"
    # The segmenter's word list; its cache (segmenter.pickle, below) only loads beside the same list.
    mkdir -p "$LAPTOP_DIR/data/dl-base-dictionaries"
    rsync -a data/dl-base-dictionaries/cedict_userdict.txt data/dl-base-dictionaries/cedict_userdict.version "$LAPTOP_DIR/data/dl-base-dictionaries/"
    rsync -a data/generated-indexes-optimizations/*.sqlite data/generated-indexes-optimizations/segmenter.pickle "$LAPTOP_DIR/data/generated-indexes-optimizations/"
fi
(cd "$LAPTOP_DIR" && deploy/update.sh laptop)

echo "== tablet"
# The tablet only accepts ADB inbound, so reach SSH through an ADB forward.
if command -v adb >/dev/null 2>&1; then
    adb -s "$TABLET_ADB" forward "tcp:$TABLET_PORT" tcp:"$TABLET_PORT" >/dev/null 2>&1 || true
fi
if [ "$SYNC_DATA" = 1 ]; then
    "${SSH[@]}" "mkdir -p $TABLET_DIR/data/moe-taiwan $TABLET_DIR/data/generated-indexes-optimizations"
    rsync -a -e "ssh -p $TABLET_PORT -i $TABLET_KEY" \
        data/*.sqlite "$TABLET_SSH:$TABLET_DIR/data/"
    rsync -a -e "ssh -p $TABLET_PORT -i $TABLET_KEY" \
        data/moe-taiwan/moe.sqlite "$TABLET_SSH:$TABLET_DIR/data/moe-taiwan/"
    rsync -a -e "ssh -p $TABLET_PORT -i $TABLET_KEY" \
        data/generated-indexes-optimizations/*.sqlite data/generated-indexes-optimizations/segmenter.pickle "$TABLET_SSH:$TABLET_DIR/data/generated-indexes-optimizations/"
fi
"${SSH[@]}" "cd $TABLET_DIR && deploy/update.sh tablet"

echo "== done"
"$ROOT/deploy/check.sh"
