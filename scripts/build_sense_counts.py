"""Build data/sense_counts.sqlite from the current CEDICT database.

The table stores two counts per (word, reading):

* `count` - the filtered sense count used to choose a headword's representative
  reading (blank and cross-reference rows excluded);
* `raw_count` - the count lookup() sorts by (only cross-references excluded).

Run this after importing CEDICT. If the file is missing or stale the app falls
back to the live queries, so nothing breaks.

    uv run python scripts/build_sense_counts.py [--path FILE]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yabai_search import db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--path",
        type=Path,
        default=db.SENSE_COUNTS_DB_PATH,
        help="output file (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    started = time.perf_counter()
    counts = db.build_sense_counts(args.path)
    elapsed = time.perf_counter() - started
    print(f"{args.path}: {counts['rows']} sense counts in {elapsed:.1f} s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
