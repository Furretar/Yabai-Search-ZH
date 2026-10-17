#!/usr/bin/env python3
"""Build data/gram_words.sqlite: a Han unigram/bigram -> word inverted index.

For 1-2 character CJK queries the trigram index does not apply, so the app looks
the query up here instead of scanning whole tables. Each row is
(source, gram, word):

    wikt_head   Wiktionary headwords that have a gloss
    wikt_gloss  words whose Wiktionary gloss contains the gram
    moe_head    MOE headwords
    moe_sense   words whose MOE sense contains the gram

A 2-character query is one bigram lookup, a 1-character query one unigram
lookup. The candidates are exact (they contain the query), and the app still
re-checks with LIKE before including them, so results do not change.

Usage (from the project root):
    uv run python -m scripts.import_gram_words
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "gram_words.sqlite"
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


def grams_of(text: str) -> set[str]:
    """Every Han unigram and bigram inside the text's Han runs."""
    grams: set[str] = set()
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
    return grams


def build() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        "CREATE TABLE gram_words ("
        "source TEXT NOT NULL, gram TEXT NOT NULL, word TEXT NOT NULL, "
        "PRIMARY KEY (source, gram, word)) WITHOUT ROWID"
    )
    insert = "INSERT OR IGNORE INTO gram_words (source, gram, word) VALUES (?,?,?)"

    def add_word(source: str, word: str) -> None:
        connection.executemany(
            insert, [(source, gram, word) for gram in grams_of(word)]
        )

    def add_texts(source: str, rows) -> None:
        """Insert grams for (word, text) rows already ordered by word."""
        current = None
        grams: set[str] = set()
        for word, text in rows:
            if word != current:
                if current is not None:
                    connection.executemany(
                        insert, [(source, gram, current) for gram in grams]
                    )
                current = word
                grams = set()
            grams |= grams_of(text or "")
        if current is not None:
            connection.executemany(
                insert, [(source, gram, current) for gram in grams]
            )

    if WIKTIONARY_DB.exists():
        source = sqlite3.connect(WIKTIONARY_DB)
        try:
            for (word,) in source.execute(
                "SELECT DISTINCT word FROM wiktionary "
                "WHERE gloss IS NOT NULL ORDER BY word"
            ):
                add_word("wikt_head", word)
            add_texts(
                "wikt_gloss",
                source.execute(
                    "SELECT word, gloss FROM wiktionary "
                    "WHERE gloss IS NOT NULL ORDER BY word"
                ),
            )
        finally:
            source.close()
    if MOE_DB.exists():
        source = sqlite3.connect(MOE_DB)
        try:
            for (word,) in source.execute("SELECT DISTINCT word FROM moe ORDER BY word"):
                add_word("moe_head", word)
            add_texts(
                "moe_sense",
                source.execute("SELECT word, text FROM senses ORDER BY word"),
            )
        finally:
            source.close()

    connection.commit()
    count = connection.execute("SELECT count(*) FROM gram_words").fetchone()[0]
    connection.close()
    print(
        f"done: {count} rows -> {DB_PATH} "
        f"({DB_PATH.stat().st_size / 1e6:.1f} MB)"
    )


if __name__ == "__main__":
    build()
