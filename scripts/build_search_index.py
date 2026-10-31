"""Build data/search_index.sqlite from the current dictionary files.

The index holds the two lists that are expensive to rebuild at startup:

* every distinct simplified headword, in "*" browse order, and
* every source pinyin reading, in toneless order.

On a small host (the Android tablet) the live build calls the pure-Python
OpenCC about a million times and takes over a minute. Reading the prebuilt
index takes a second or two and uses much less memory.

Run this after importing or changing any dictionary source. If it is missing
or stale the app still works: it falls back to building the lists on first use.

    uv run python scripts/build_search_index.py [--path FILE]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Run as `python scripts/build_search_index.py`, so add the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yabai_search import db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--path",
        type=Path,
        default=db.SEARCH_INDEX_DB_PATH,
        help="output file (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    started = time.perf_counter()
    counts = db.build_search_index(args.path)
    elapsed = time.perf_counter() - started
    print(
        f"{args.path}: {counts['headwords']} headwords, "
        f"{counts['pinyin']} pinyin rows, "
        f"{counts.get('dialect_labels', 0)} dialect labels, "
        f"{counts.get('dialect_pinyin', 0)} dialect readings in {elapsed:.1f} s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
