#!/usr/bin/env bash
# Publish your committed changes to both serving machines.
# Run this inside the dev clone (the one you edit).
#
# Usage: deploy/release.sh [--data]
#   --data   also copy data/*.sqlite to both machines (they are not in git)
#
# Steps: tests -> git push -> laptop update -> tablet update.
# The machines are updated one at a time and the run stops at the first
# failure, so the other one keeps serving (the Worker fails over to it).
# It does not commit for you; commit first.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Where the other copies live. Override in deploy/release.env.
LAPTOP_DIR="$HOME/yabai-laptop"
TABLET_SSH="u0_a249@192.168.188.105"
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
fi
(cd "$LAPTOP_DIR" && deploy/update.sh laptop)

echo "== tablet"
if [ "$SYNC_DATA" = 1 ]; then
    rsync -a -e "ssh -p $TABLET_PORT -i $TABLET_KEY" \
        data/*.sqlite "$TABLET_SSH:$TABLET_DIR/data/"
fi
"${SSH[@]}" "cd $TABLET_DIR && deploy/update.sh tablet"

echo "== done"
"$ROOT/deploy/check.sh"
