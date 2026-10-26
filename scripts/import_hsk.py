#!/usr/bin/env python3
"""Build data/hsk.sqlite from the complete HSK vocabulary list.

Source: https://github.com/drkameleon/complete-hsk-vocabulary
The dataset covers the old HSK 2.0 levels, the HSK 3.0 levels, and the newer
2026 syllabus levels. The app displays only the two current lists (3.0 and
2026); HSK 2.0 is ignored. It is released under the MIT licence.

Usage (from the project root):
    uv run python -m scripts.import_hsk
    uv run python -m scripts.import_hsk --force
    uv run python -m scripts.import_hsk --input data/hsk-complete.min.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = (
    "https://raw.githubusercontent.com/drkameleon/"
    "complete-hsk-vocabulary/main/complete.min.json"
)
DEFAULT_CACHE = ROOT / "data" / "hsk-complete.min.json"
DEFAULT_DB = ROOT / "data" / "hsk.sqlite"

SCHEMA = """
DROP TABLE IF EXISTS hsk;

CREATE TABLE hsk (
    simplified  TEXT NOT NULL,
    traditional TEXT,
    old_level   INTEGER,  -- HSK 2.0 level (1-6)
    new_level   INTEGER,  -- HSK 3.0 level (1-7, where 7 means 7-9)
    newest_level INTEGER, -- 2026 syllabus level (1-7)
    PRIMARY KEY (simplified, traditional)
);

CREATE INDEX idx_hsk_simplified ON hsk(simplified);
CREATE INDEX idx_hsk_traditional ON hsk(traditional);

DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Minified level codes: o = old HSK 2.0, n = HSK 3.0, t = 2026 syllabus.
_LEVEL_COLUMNS = {"o": "old_level", "n": "new_level", "t": "newest_level"}


def download(url: str, dest: Path, force: bool = False) -> None:
    """Download the JSON once and reuse the cached copy unless forced."""
    if dest.exists() and not force:
        print(f"using cached {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-search/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def parse_levels(codes: list[str]) -> dict[str, int]:
    """Turn ['t3', 'n4', 'o3'] into {'old_level': 3, ...}."""
    levels: dict[str, int] = {}
    for code in codes:
        if not code or len(code) < 2:
            continue
        column = _LEVEL_COLUMNS.get(code[0])
        if column is None:
            continue
        try:
            number = int(code[1:])
        except ValueError:
            continue
        # Keep the easiest level if the upstream list repeats a word.
        if column not in levels or number < levels[column]:
            levels[column] = number
    return levels


def build(json_path: Path, db_path: Path) -> None:
    """(Re)build the HSK SQLite database from the downloaded JSON."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("expected a JSON list of HSK entries")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")

    rows = []
    for entry in data:
        simplified = (entry.get("s") or "").strip()
        if not simplified:
            continue
        forms = entry.get("f") or []
        traditional = ""
        if forms:
            traditional = (forms[0].get("t") or "").strip()
        levels = parse_levels(entry.get("l") or [])
        rows.append(
            (
                simplified,
                traditional or simplified,
                levels.get("old_level"),
                levels.get("new_level"),
                levels.get("newest_level"),
            )
        )

    connection.executemany(
        "INSERT OR REPLACE INTO hsk "
        "(simplified, traditional, old_level, new_level, newest_level) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('updated', ?)",
        (time.strftime("%Y-%m-%d"),),
    )
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('source', ?)",
        ("https://github.com/drkameleon/complete-hsk-vocabulary",),
    )
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('license', ?)", ("MIT",)
    )
    connection.commit()
    connection.execute("VACUUM")
    total = connection.execute("SELECT COUNT(*) FROM hsk").fetchone()[0]
    connection.close()
    print(f"imported {total} HSK entries into {db_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--force",
        action="store_true",
        help="download the upstream JSON again even if it is cached",
    )
    args = parser.parse_args()
    download(DEFAULT_URL, args.input, force=args.force)
    build(args.input, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
