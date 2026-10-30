#!/usr/bin/env bash
# Show which machine answers on each public hostname and how long it took.
#
# Usage: ORIGIN_SECRET=... deploy/check.sh
# ORIGIN_SECRET is needed for laptop. and tablet. (they reject requests
# without it). It can also come from deploy/env.local as YABAI_ORIGIN_SECRET.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$ROOT/deploy/env.local" ]; then
    set -a
    . "$ROOT/deploy/env.local"
    set +a
fi
SECRET="${ORIGIN_SECRET:-${YABAI_ORIGIN_SECRET:-}}"

probe() {
    local host="$1"
    # Print status, total time, and the two "who served this" headers.
    local out
    out="$(curl -sS -m 10 -o /dev/null -D - \
        -H "X-Origin-Secret: $SECRET" \
        -w 'time=%{time_total}s\n' "https://$host/__ping" 2>&1)" || {
        printf '%-26s UNREACHABLE\n' "$host"
        return
    }
    local status served origin t
    status="$(printf '%s' "$out" | head -1 | tr -d '\r')"
    served="$(printf '%s' "$out" | grep -i '^x-served-by:' | tr -d '\r' | cut -d' ' -f2)"
    origin="$(printf '%s' "$out" | grep -i '^x-origin:' | tr -d '\r' | cut -d' ' -f2)"
    t="$(printf '%s' "$out" | grep '^time=')"
    printf '%-26s %s  served-by=%s  worker-origin=%s  %s\n' \
        "$host" "$status" "${served:--}" "${origin:--}" "$t"
}

probe yabaisearch.org
probe www.yabaisearch.org
probe laptop.yabaisearch.org
probe tablet.yabaisearch.org
probe new.yabaisearch.org
