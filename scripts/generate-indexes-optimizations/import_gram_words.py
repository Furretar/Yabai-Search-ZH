#!/usr/bin/env python3
"""Build data/gram_words.sqlite: a Han unigram/bigram -> word inverted index.

For 1-2 character CJK queries the trigram index does not apply, so the app looks
the query up here instead of scanning whole tables. Each row is
(source, gram, word):

    wikt_head   Wiktionary headwords that have a gloss
    wikt_gloss  words whose Wiktionary gloss contains the gram
    moe_head    MOE headwords
    baidu_head  百度汉语 headwords
    moe_sense   words whose MOE sense contains the gram

A 2-character query is one bigram lookup, a 1-character query one unigram
lookup. The candidates are exact (they contain the query), and the app still
re-checks with LIKE before including them, so results do not change.

Usage (from the project root):
    uv run python scripts/generate-indexes-optimizations/import_gram_words.py
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Derived indexes live in db.GENERATED_DIR, not in data/ itself.
GENERATED = ROOT / "data" / "generated-indexes-optimizations"
DB_PATH = GENERATED / "gram_words.sqlite"
TEMP_PATH = GENERATED / "gram_words.sqlite.tmp"
# One file holds both MOE dictionaries (concise: plain tables, comprehensive: rev_ tables).
MOE_DB = ROOT / "data" / "moe-taiwan" / "moe.sqlite"
MOE_REVISED_DB = MOE_DB
WIKTIONARY_DB = ROOT / "data" / "wiktionary.sqlite"
ZHWIKTIONARY_DB = ROOT / "data" / "zhwiktionary.sqlite"
JPWIKTIONARY_DB = ROOT / "data" / "jpwiktionary.sqlite"
FREQ_DB = ROOT / "data" / "corpus_freq.sqlite"
BAIDU_DB = ROOT / "data" / "baidu.sqlite"
SOURCE_FILES = {
    "moe": MOE_DB,
    "moe_revised": MOE_REVISED_DB,
    "wiktionary": WIKTIONARY_DB,
    "zhwiktionary": ZHWIKTIONARY_DB,
    "jpwiktionary": JPWIKTIONARY_DB,
    "freq": FREQ_DB,
    "baidu": BAIDU_DB,
}


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
    if TEMP_PATH.exists():
        TEMP_PATH.unlink()
    connection = sqlite3.connect(TEMP_PATH)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(
        "CREATE TABLE gram_words ("
        "source TEXT NOT NULL, gram TEXT NOT NULL, word TEXT NOT NULL, "
        "PRIMARY KEY (source, gram, word)) WITHOUT ROWID"
    )
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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
    if MOE_REVISED_DB.exists():
        source = sqlite3.connect(MOE_REVISED_DB)
        try:
            has_revised = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rev_moe'"
            ).fetchone()
            if has_revised:
                for (word,) in source.execute(
                    "SELECT DISTINCT word FROM rev_moe ORDER BY word"
                ):
                    add_word("moe_revised_head", word)
        finally:
            source.close()
    if ZHWIKTIONARY_DB.exists():
        source = sqlite3.connect(ZHWIKTIONARY_DB)
        try:
            for (word,) in source.execute(
                "SELECT DISTINCT word FROM headwords ORDER BY word"
            ):
                add_word("zhwikt_head", word)
        finally:
            source.close()
    if JPWIKTIONARY_DB.exists():
        source = sqlite3.connect(JPWIKTIONARY_DB)
        try:
            for (word,) in source.execute(
                "SELECT DISTINCT word FROM headwords ORDER BY word"
            ):
                add_word("jpwikt_head", word)
        finally:
            source.close()
    if BAIDU_DB.exists():
        source = sqlite3.connect(BAIDU_DB)
        try:
            for (word,) in source.execute("SELECT word FROM headwords ORDER BY word"):
                add_word("baidu_head", word)
        finally:
            source.close()
    if FREQ_DB.exists():
        source = sqlite3.connect(FREQ_DB)
        try:
            for (word,) in source.execute("SELECT word FROM freq ORDER BY rank"):
                add_word("freq_head", word)
        finally:
            source.close()

    for name, path in SOURCE_FILES.items():
        if path.exists():
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (f"source:{name}", hashlib.sha256(path.read_bytes()).hexdigest()),
            )
    connection.commit()
    count = connection.execute("SELECT count(*) FROM gram_words").fetchone()[0]
    connection.close()
    os.replace(TEMP_PATH, DB_PATH)
    print(
        f"done: {count} rows -> {DB_PATH} "
        f"({DB_PATH.stat().st_size / 1e6:.1f} MB)"
    )


if __name__ == "__main__":
    build()
