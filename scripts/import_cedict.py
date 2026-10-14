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

from yabai_dict.pinyin import numeric_to_marked, to_toneless

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = (
    "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
)
DEFAULT_CACHE = ROOT / "data" / "cedict.txt.gz"
DEFAULT_DB = ROOT / "data" / "cedict.sqlite"

LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/$")

SCHEMA = """
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS entries_fts;

CREATE TABLE entries (
    id              INTEGER PRIMARY KEY,
    traditional     TEXT NOT NULL,
    simplified      TEXT NOT NULL,
    pinyin          TEXT NOT NULL,
    pinyin_numeric  TEXT NOT NULL,
    pinyin_toneless TEXT NOT NULL,
    english         TEXT NOT NULL,
    freq_rank       INTEGER
);

CREATE INDEX idx_entries_simplified      ON entries(simplified);
CREATE INDEX idx_entries_traditional     ON entries(traditional);
CREATE INDEX idx_entries_pinyin_toneless ON entries(pinyin_toneless);

CREATE VIRTUAL TABLE entries_fts USING fts5(
    english,
    content='entries',
    content_rowid='id',
    tokenize='unicode61'
);
"""


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "yabai-dict/0.1"})
    with urllib.request.urlopen(request) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def open_source(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def parse_line(line: str):
    match = LINE_RE.match(line)
    if not match:
        return None
    traditional, simplified, pinyin_numeric, raw_defs = match.groups()
    english = "; ".join(d for d in raw_defs.split("/") if d)
    return (
        traditional,
        simplified,
        numeric_to_marked(pinyin_numeric),
        pinyin_numeric,
        to_toneless(pinyin_numeric),
        english,
    )


def build(source: Path, db_path: Path, limit: int | None) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")

    insert = (
        "INSERT INTO entries "
        "(traditional, simplified, pinyin, pinyin_numeric, pinyin_toneless, english) "
        "VALUES (?,?,?,?,?,?)"
    )
    rows: list[tuple] = []
    kept = 0
    skipped = 0
    started = time.time()

    with open_source(source) as handle:
        for line in handle:
            if not line or line.startswith("#"):
                continue
            row = parse_line(line.rstrip("\n"))
            if row is None:
                skipped += 1
                continue
            rows.append(row)
            kept += 1
            if len(rows) >= 5000:
                connection.executemany(insert, rows)
                rows.clear()
            if limit and kept >= limit:
                break
    if rows:
        connection.executemany(insert, rows)

    print("building full-text index...")
    connection.execute(
        "INSERT INTO entries_fts(rowid, english) SELECT id, english FROM entries"
    )
    connection.commit()
    count = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
    connection.execute("VACUUM")
    connection.close()

    print(
        f"done: {count} entries in {time.time() - started:.1f}s "
        f"({skipped} lines skipped)"
    )
    print(f"database: {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input", type=Path, help="use a local CEDICT file")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, help="only import the first N entries")
    parser.add_argument("--force", action="store_true", help="re-download")
    args = parser.parse_args()

    if args.input:
        source = args.input
    else:
        source = DEFAULT_CACHE
        if args.force or not source.exists():
            download(args.url, source)

    build(source, args.db, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
