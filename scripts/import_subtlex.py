#!/usr/bin/env python3
"""Build data/subtlex.sqlite from SUBTLEX-CH word frequencies.

SUBTLEX-CH (Cai & Brysbaert, 2010) gives the frequency of Chinese words based on
film and TV subtitles. It is released under a CC BY licence (attribute the
authors). The download is a zip with a GB18030-encoded, tab-separated text file
whose rows are already ordered from most to least frequent.

Usage (from the project root):
    uv run python -m scripts.import_subtlex
    uv run python -m scripts.import_subtlex --input data/subtlexchwf.zip
    uv run python -m scripts.import_subtlex --force
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = (
    "https://www.ugent.be/pp/experimentele-psychologie/en/research/documents/"
    "subtlexch/subtlexchwf.zip"
)
DEFAULT_CACHE = ROOT / "data" / "subtlexchwf.zip"
DEFAULT_DB = ROOT / "data" / "subtlex.sqlite"

SCHEMA = """
DROP TABLE IF EXISTS freq;
DROP TABLE IF EXISTS freq_fts;

CREATE TABLE freq (
    word        TEXT PRIMARY KEY,  -- the word (as written in SUBTLEX-CH)
    rank        INTEGER NOT NULL,  -- frequency rank, 1 = most frequent
    count       INTEGER NOT NULL,  -- occurrences in the subtitle corpus
    per_million REAL               -- occurrences per million words
);

CREATE INDEX idx_freq_rank ON freq(rank);

-- Trigram index over the word column, so a 3+ character headword search can use
-- the index instead of scanning every row. The rank is stored (unindexed) so
-- results keep the frequency order without relying on rowid numbering.
CREATE VIRTUAL TABLE freq_fts USING fts5(
    word,
    freq_rank UNINDEXED,
    tokenize='trigram'
);

DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` so we cache the file instead of re-fetching it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-search/0.1"})
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def read_rows(zip_path: Path):
    """Yield (word, count, per_million) from the SUBTLEX-CH-WF text file."""
    with zipfile.ZipFile(zip_path) as archive:
        name = next(n for n in archive.namelist() if n.endswith("SUBTLEX-CH-WF"))
        text = archive.read(name).decode("gb18030", "replace")
    for line in text.splitlines():
        fields = line.split("\t")
        # Skip the two "Total word count" notes and the "Word\tWCount\t..." header.
        if len(fields) < 3 or not fields[0] or fields[0] == "Word":
            continue
        word = fields[0].strip()
        try:
            count = int(fields[1])
            per_million = float(fields[2])
        except ValueError:
            continue  # a header/notes line that slipped through
        yield word, count, per_million


def build(zip_path: Path, db_path: Path) -> None:
    """(Re)build the SQLite frequency database from `zip_path`."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('updated', ?)",
        (time.strftime("%Y-%m-%d"),),
    )

    insert = "INSERT OR IGNORE INTO freq (word, rank, count, per_million) VALUES (?,?,?,?)"
    rows = []
    rank = 0
    for word, count, per_million in read_rows(zip_path):
        rank += 1  # the file is already ordered most-frequent first
        rows.append((word, rank, count, per_million))
        if len(rows) >= 5000:
            connection.executemany(insert, rows)
            rows.clear()
    if rows:
        connection.executemany(insert, rows)

    # Build the trigram index after the rows are in place.
    connection.execute(
        "INSERT INTO freq_fts(word, freq_rank) SELECT word, rank FROM freq"
    )
    connection.commit()
    connection.execute("VACUUM")
    total = connection.execute("SELECT COUNT(*) FROM freq").fetchone()[0]
    connection.close()
    print(f"imported {total} words into {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", type=Path, help="use a local .zip file")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--force", action="store_true", help="re-download")
    args = parser.parse_args()

    if args.input:
        source = args.input
    else:
        source = DEFAULT_CACHE
        if args.force or not source.exists():
            download(args.url, source)

    build(source, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
