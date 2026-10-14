#!/usr/bin/env python3
"""Build a SQLite dictionary database from the CC-CEDICT text file.

Usage (from the project root):
    uv run python -m scripts.import_cedict
    uv run python -m scripts.import_cedict --limit 5000
    uv run python -m scripts.import_cedict --force
"""

from __future__ import annotations

import argparse
import gzip
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

# Reuse the pinyin helpers so the database gets the same marked/toneless forms
# that the search layer computes at query time.
from yabai_cidian.pinyin import numeric_to_marked, to_toneless

# Project root is one directory above scripts/.
ROOT = Path(__file__).resolve().parents[1]
# Source file to fetch. The filename is fixed, so this URL always serves MDBG's
# current CC-CEDICT build (no version/date in the name).
DEFAULT_URL = (
    "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
)
# Where we cache the download, and the database we generate from it.
DEFAULT_CACHE = ROOT / "data" / "cedict.txt.gz"
DEFAULT_DB = ROOT / "data" / "cedict.sqlite"

# Matches one CEDICT entry line: "trad simp [pin1 yin1] /def/def/"
#   1 = traditional  2 = simplified  3 = numeric pinyin  4 = slash-joined defs
LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/$")

# SQL that creates the schema. The DROPs make a rebuild clean (the DB file is
# also deleted before this runs, so they mainly guard against custom --db paths).
SCHEMA = """
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS entries_fts;

CREATE TABLE entries (
    id              INTEGER PRIMARY KEY,  -- auto row id, also used as FTS rowid
    traditional     TEXT NOT NULL,        -- CEDICT field 1
    simplified      TEXT NOT NULL,        -- CEDICT field 2
    pinyin          TEXT NOT NULL,        -- field 3, tone marks (e.g. shàng yī)
    pinyin_numeric  TEXT NOT NULL,        -- field 3, raw (e.g. shang4 yi1)
    pinyin_toneless TEXT NOT NULL,        -- field 3, search key (e.g. shangyi)
    english         TEXT NOT NULL,        -- field 4, definitions joined with "; "
    freq_rank       INTEGER                -- reserved; not filled (CEDICT has none)
);

-- Indexes make lookups on these columns fast.
CREATE INDEX idx_entries_simplified      ON entries(simplified);
CREATE INDEX idx_entries_traditional     ON entries(traditional);
CREATE INDEX idx_entries_pinyin_toneless ON entries(pinyin_toneless);

-- Full-text index over the English definitions. It is "external content": it
-- stores only the index and reads text from `entries`, saving space.
CREATE VIRTUAL TABLE entries_fts USING fts5(
    english,
    content='entries',
    content_rowid='id',
    tokenize='unicode61'
);
"""


def download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` so we cache the file instead of re-fetching it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    # Identify ourselves to the server (harmless, but good etiquette).
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-cidian/0.1"})
    # Read in 1 MiB chunks and write straight through (low memory use).
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def open_source(path: Path):
    """Open a .gz or plain-text CEDICT file as a text stream."""
    if path.suffix == ".gz":
        # errors="replace" so a stray bad byte can't crash the whole import.
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def parse_line(line: str):
    """Parse one CEDICT line into a 6-tuple, or None if it doesn't match."""
    match = LINE_RE.match(line)
    if not match:
        return None  # not a valid entry line (blank/comment/garbage)
    traditional, simplified, pinyin_numeric, raw_defs = match.groups()
    # Definitions are slash-separated; drop empties and join them with "; ".
    english = "; ".join(d for d in raw_defs.split("/") if d)
    # Return values in the exact column order used by the INSERT statement.
    return (
        traditional,
        simplified,
        numeric_to_marked(pinyin_numeric),  # pinyin (tone marks)
        pinyin_numeric,  # pinyin_numeric (raw)
        to_toneless(pinyin_numeric),  # pinyin_toneless (search key)
        english,
    )


def build(source: Path, db_path: Path, limit: int | None) -> None:
    """(Re)build the SQLite database from `source`."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Full wipe: delete the old DB file so every run starts from scratch.
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)  # create the tables and indexes
    # Speed up a bulk load: disable crash-safe journaling while importing.
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")

    insert = (
        "INSERT INTO entries "
        "(traditional, simplified, pinyin, pinyin_numeric, pinyin_toneless, english) "
        "VALUES (?,?,?,?,?,?)"
    )
    rows: list[tuple] = []  # buffer of rows waiting to be written
    kept = 0  # entries accepted so far
    skipped = 0  # lines that didn't match the expected format
    started = time.time()

    with open_source(source) as handle:
        for line in handle:
            # Skip blanks and comment/header lines (they start with "#").
            if not line or line.startswith("#"):
                continue
            row = parse_line(line.rstrip("\n"))
            if row is None:
                skipped += 1
                continue
            rows.append(row)
            kept += 1
            # Flush every 5000 rows: fewer, larger transactions is faster.
            if len(rows) >= 5000:
                connection.executemany(insert, rows)
                rows.clear()
            # Stop early when the caller asked for a limited test import.
            if limit and kept >= limit:
                break
    if rows:
        connection.executemany(insert, rows)  # flush the last partial batch

    # Populate the full-text index from the English column.
    print("building full-text index...")
    connection.execute(
        "INSERT INTO entries_fts(rowid, english) SELECT id, english FROM entries"
    )
    connection.commit()
    count = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
    # VACUUM repacks the file compactly and reclaims unused space.
    connection.execute("VACUUM")
    connection.close()

    print(
        f"done: {count} entries in {time.time() - started:.1f}s "
        f"({skipped} lines skipped)"
    )
    print(f"database: {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    """Parse command-line arguments and run the import."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", type=Path, help="use a local CEDICT file")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, help="only import the first N entries")
    parser.add_argument("--force", action="store_true", help="re-download")
    args = parser.parse_args()

    if args.input:
        source = args.input  # the user pointed us at a local file
    else:
        source = DEFAULT_CACHE
        # Download only if there's no cached copy, or the user forced a refresh.
        if args.force or not source.exists():
            download(args.url, source)

    build(source, args.db, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
