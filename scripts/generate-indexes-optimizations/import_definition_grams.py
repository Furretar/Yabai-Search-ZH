#!/usr/bin/env python3
"""Build data/definition_grams.sqlite: Han unigrams and bigrams from definitions.

Used as an exact negative filter for definition substring searches. If any
unigram (1-character query) or bigram (longer query) of a query is missing, the
substring cannot occur, so the expensive scan is skipped. It never causes a
false negative: a missing gram means the scan would have found nothing.

Sources are both MOE dictionaries', Unihan's and Chinese Wiktionary's sense texts and English
Wiktionary's glosses, matching _source_definition_entries(). The output is one
`grams` table; the app loads it as an in-memory set once per process.

Usage (from the project root):
    uv run python scripts/generate-indexes-optimizations/import_definition_grams.py
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Derived indexes live in db.GENERATED_DIR, not in data/ itself.
DB_PATH = ROOT / "data" / "generated-indexes-optimizations" / "definition_grams.sqlite"
MOE_DB = ROOT / "data" / "dictionary-databases" / "moe-taiwan" / "moe.sqlite"  # both MOE dictionaries
WIKTIONARY_DB = ROOT / "data" / "dictionary-databases" / "wiktionary.sqlite"
ZHWIKTIONARY_DB = ROOT / "data" / "dictionary-databases" / "zhwiktionary.sqlite"
JPWIKTIONARY_DB = ROOT / "data" / "dictionary-databases" / "jpwiktionary.sqlite"
UNIHAN_DB = ROOT / "data" / "dictionary-databases" / "unihan.sqlite"  # its glosses can name other characters
# Keep in step with db._definition_grams_sources(): every database whose definitions are read below.
SOURCE_FILES = {
    "moe": MOE_DB,
    "wiktionary": WIKTIONARY_DB,
    "zhwiktionary": ZHWIKTIONARY_DB,
    "jpwiktionary": JPWIKTIONARY_DB,
    "unihan": UNIHAN_DB,
}



def moe_sense_tables(source: sqlite3.Connection) -> list[str]:
    """Sense tables of the merged MOE database: concise `senses`, comprehensive `rev_senses`."""
    names = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [table for table in ("senses", "rev_senses") if table in names]


def is_han(char: str) -> bool:
    """True for a Han ideograph, including the supplementary planes."""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x323AF
    )


def add_text(text: str, grams: set[str]) -> None:
    """Add every Han unigram and bigram from `text`."""
    run: list[str] = []

    def flush() -> None:
        for index, char in enumerate(run):
            grams.add(char)
            if index + 1 < len(run):
                grams.add(char + run[index + 1])

    for char in text:
        if is_han(char):
            run.append(char)
        else:
            flush()
            run.clear()
    flush()


def build() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    grams: set[str] = set()

    if MOE_DB.exists():
        source = sqlite3.connect(MOE_DB)
        try:
            for table in moe_sense_tables(source):
                for (text,) in source.execute(f"SELECT text FROM {table} WHERE text <> ''"):
                    add_text(text, grams)
        finally:
            source.close()
    if WIKTIONARY_DB.exists():
        source = sqlite3.connect(WIKTIONARY_DB)
        try:
            for (gloss,) in source.execute(
                "SELECT gloss FROM wiktionary WHERE gloss IS NOT NULL AND gloss <> ''"
            ):
                add_text(gloss, grams)
        finally:
            source.close()
    if ZHWIKTIONARY_DB.exists():
        source = sqlite3.connect(ZHWIKTIONARY_DB)
        try:
            for (text,) in source.execute(
                "SELECT text FROM senses WHERE text <> ''"
            ):
                add_text(text, grams)
        finally:
            source.close()
    if JPWIKTIONARY_DB.exists():
        source = sqlite3.connect(JPWIKTIONARY_DB)
        try:
            for (text,) in source.execute(
                "SELECT text FROM senses WHERE text <> ''"
            ):
                add_text(text, grams)
        finally:
            source.close()

    if UNIHAN_DB.exists():
        source = sqlite3.connect(UNIHAN_DB)
        try:
            for (text,) in source.execute("SELECT text FROM senses WHERE text <> ''"):
                add_text(text, grams)
        finally:
            source.close()

    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("CREATE TABLE grams (gram TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.executemany(
        "INSERT OR IGNORE INTO grams (gram) VALUES (?)",
        ((gram,) for gram in grams),
    )
    # The index is an exact negative filter, so it must never outlive its sources: the app compares these
    # hashes with the current files (db._definition_grams_current) and ignores the index when they differ.
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID")
    for name, path in SOURCE_FILES.items():
        if path.exists():
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (f"source:{name}", hashlib.sha256(path.read_bytes()).hexdigest()),
            )
    connection.commit()
    connection.close()
    print(
        f"done: {len(grams)} grams -> {DB_PATH} "
        f"({DB_PATH.stat().st_size / 1e6:.1f} MB)"
    )


if __name__ == "__main__":
    build()
