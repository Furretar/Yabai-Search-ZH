#!/usr/bin/env python3
"""Build data/definition_grams.sqlite: Han unigrams and bigrams from definitions.

Used as an exact negative filter for definition substring searches. If any
unigram (1-character query) or bigram (longer query) of a query is missing, the
substring cannot occur, so the expensive scan is skipped. It never causes a
false negative: a missing gram means the scan would have found nothing.

Sources are MOE's sense texts and Wiktionary's glosses, matching
_source_definition_entries(). The output is one `grams` table; the app loads it
as an in-memory set once per process.

Usage (from the project root):
    uv run python -m scripts.import_definition_grams
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "definition_grams.sqlite"
MOE_DB = ROOT / "data" / "moe.sqlite"
WIKTIONARY_DB = ROOT / "data" / "wiktionary.sqlite"


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
            for (text,) in source.execute("SELECT text FROM senses WHERE text <> ''"):
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

    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("CREATE TABLE grams (gram TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.executemany(
        "INSERT OR IGNORE INTO grams (gram) VALUES (?)",
        ((gram,) for gram in grams),
    )
    connection.commit()
    connection.close()
    print(
        f"done: {len(grams)} grams -> {DB_PATH} "
        f"({DB_PATH.stat().st_size / 1e6:.1f} MB)"
    )


if __name__ == "__main__":
    build()
