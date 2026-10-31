#!/usr/bin/env python3
"""Manual timing/order check for the English relevance tiers.

    uv run python scripts/bench_relevance.py

No assertions and no test-suite timing: run it by hand when tuning tiers.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yabai_search import db  # noqa: E402

QUERIES = ("a", "the", "to", "word", "run")


def main() -> int:
    for query in QUERIES:
        started = time.perf_counter()
        entries, pinyin_match = db.search(
            f'"{query}"', limit=100, part="all", hydrate=0
        )
        elapsed = (time.perf_counter() - started) * 1000
        top = [
            (entry["simplified"], entry.get("tier"))
            for entry in entries[:8]
        ]
        print(
            f"{query!r}: {len(entries)} rows, pinyin_match={pinyin_match}, "
            f"{elapsed:.1f} ms, top={top}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
