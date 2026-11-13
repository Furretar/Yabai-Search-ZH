#!/usr/bin/env python3
"""Build the temporary FineWeb2 frequency source for Yabai Search.

The input is the unpruned merged list from the FineWeb2 project (the full 4-shard run):

    combined20_merged_all.tsv
    rank, word, count, df, per_million, forms, variants

Every row becomes a frequency row keyed by the merged simplified form. Each
surface variant in the `variants` column also gets the same rank and count, so
both 中国 and 中國 resolve to the merged entry. Rank is the row number in the
list. Nothing is cut at a top-k boundary.

The script also adds `entries.corpus_freq_rank` to the CEDICT database and
fills it from this list. The existing `entries.freq_rank` column is left alone,
so setting YABAI_FREQ_SOURCE=subtlex restores the SUBTLEX-CH behaviour without
rebuilding anything.

Usage:
    uv run python scripts/create-dict-db/import_corpus_freq.py
    uv run python scripts/create-dict-db/import_corpus_freq.py --input path/to/combined_merged_all.tsv
"""

from __future__ import annotations

import sys
import argparse
import csv
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
# The full 4-shard FineWeb2 run (5.1 billion tokens, 7.8 million documents, 332,959 ranks). NOT
# combined_merged_all.tsv in the same folder: that is the 3 x 1 GB sample run (0.5 billion tokens, 270,160
# ranks, an untuned prior), which ranks rare words much less reliably and splits words such as 不是.
DEFAULT_INPUT = (
    Path.home()
    / "Desktop"
    / "ai"
    / "FineWeb2-Mandarin-Frequency-List"
    / "data"
    / "work"
    / "results"
    / "combined20_merged_all.tsv"
)
DEFAULT_DB = ROOT / "data" / "corpus_freq.sqlite"
CEDICT_DB = ROOT / "data" / "cedict.sqlite"

SCHEMA = """
DROP TABLE IF EXISTS freq;
CREATE TABLE freq (
    word        TEXT PRIMARY KEY,  -- merged form or one of its surface variants
    rank        INTEGER NOT NULL,  -- row number in the combined list, 1 = most frequent
    count       INTEGER NOT NULL,  -- occurrences in the 20 GB sample
    per_million REAL NOT NULL      -- occurrences per million tokens
) WITHOUT ROWID;
CREATE INDEX idx_freq_rank ON freq(rank);
DROP TABLE IF EXISTS freq_fts;
CREATE VIRTUAL TABLE freq_fts USING fts5(
    word,
    freq_rank UNINDEXED,
    tokenize='trigram'
);
DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def read_rows(
    path: Path, min_df: int = 0
) -> tuple[list[tuple], list[tuple]]:
    """Return the primary keys and the variant keys for all rows.

    `min_df` is an optional noise filter: rows seen in fewer than that many
    documents are dropped. The default (0) keeps everything, because the 20 GB
    list's document frequency is already reliable and rare words are legitimate.
    """
    primary: list[tuple] = []
    variants: list[tuple] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if min_df and int(row.get("df") or 0) < min_df:
                continue
            word = row["word"]
            rank = int(row["rank"])
            count = int(row["count"])
            per_million = float(row["per_million"])
            primary.append((word, rank, count, per_million))
            for item in (row.get("variants") or "").split("|"):
                form, _, _ = item.rpartition(":")
                if form:
                    variants.append((form, rank, count, per_million))
    return primary, variants


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cedict", type=Path, default=CEDICT_DB)
    parser.add_argument(
        "--min-df",
        type=int,
        default=0,
        help=(
            "Optional noise filter: drop rows seen in fewer than this many "
            "documents. Default 0 keeps every row. The 20 GB merge already "
            "uses representative document frequency, so this is only for an "
            "experiment; changing it requires rebuilding every derived file."
        ),
    )
    args = parser.parse_args()
    if args.min_df < 0:
        raise SystemExit("--min-df must be >= 0")

    if not args.input.exists():
        raise SystemExit(f"Missing input: {args.input}")
    if not args.cedict.exists():
        raise SystemExit(f"Missing CEDICT database: {args.cedict}")

    started = time.time()
    primary, variants = read_rows(args.input, args.min_df)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.db)
    try:
        connection.executescript(SCHEMA)
        # Primary keys win over variant rows for the same surface form.
        connection.executemany(
            "INSERT OR IGNORE INTO freq (word, rank, count, per_million) VALUES (?,?,?,?)",
            primary,
        )
        connection.executemany(
            "INSERT OR IGNORE INTO freq (word, rank, count, per_million) VALUES (?,?,?,?)",
            variants,
        )
        connection.execute(
            "INSERT INTO freq_fts(word, freq_rank) SELECT word, rank FROM freq"
        )
        total = connection.execute("SELECT COUNT(*) FROM freq").fetchone()[0]
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("updated", time.strftime("%Y-%m-%d")),
                ("source", "FineWeb2 cmn_Hani combined list"),
                (
                    "note",
                    "Rank is the row number in the unpruned merged list; "
                    "variants share their merged entry's rank.",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    # Add the parallel rank column to CEDICT and fill it. The old freq_rank
    # column is deliberately untouched.
    cedict = sqlite3.connect(args.cedict)
    try:
        columns = {
            row[1] for row in cedict.execute("PRAGMA table_info(entries)")
        }
        if "corpus_freq_rank" not in columns:
            cedict.execute("ALTER TABLE entries ADD COLUMN corpus_freq_rank INTEGER")
        cedict.execute("ATTACH DATABASE ? AS freqdb", (str(args.db),))
        cedict.execute("UPDATE entries SET corpus_freq_rank = NULL")
        cedict.execute(
            "UPDATE entries SET corpus_freq_rank = COALESCE("
            "    (SELECT rank FROM freqdb.freq WHERE word = entries.simplified),"
            "    (SELECT rank FROM freqdb.freq WHERE word = entries.traditional)"
            ")"
        )
        filled = cedict.execute(
            "SELECT COUNT(*) FROM entries WHERE corpus_freq_rank IS NOT NULL"
        ).fetchone()[0]
        cedict.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('corpus_freq_rank', '1')"
        )
        cedict.commit()
        cedict.execute("DETACH DATABASE freqdb")
    finally:
        cedict.close()

    print(
        f"wrote {total:,} freq rows from {len(primary):,} merged entries "
        f"to {args.db}"
    )
    print(f"filled corpus_freq_rank for {filled:,} CEDICT entries")
    print(f"elapsed {time.time() - started:.1f}s")


if __name__ == "__main__":
    import _indexes  # rebuilds the derived indexes when the import is done (--no-indexes skips it)

    sys.exit(_indexes.run(main))
